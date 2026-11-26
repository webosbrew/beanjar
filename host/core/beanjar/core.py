# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The VM manager's library: a directory of VMs, each with its own firmware.

VM edits use TOML Kit; of the core only dbus.py imports gi (Gio, for the VM's bus), and the windows
are beanjar_ui's (host/ui). Layout:

  <library>/config.toml          repo = the Beanjar checkout, auto_reconnect, epk2extract, mac_prefix,
                                 lan_parent (the wired interface of link = "lan"), addresses (the first
                                 three octets of its VMs' addresses, default 127.0.1)
  <library>/ca/                  the mode=mitm proxy's CA, shared by the library's VMs (keys.py)
  <library>/.slots.lock          held while a VM takes its slot (slot_lock)
  <library>/.admission.lock      held while a start is admitted and takes its start.lock (admission_lock)
  <library>/vms/<name>/          vm.toml (SETTINGS), state/, run/, logs/ (launch.py --dir); identity/ (0700:
                                 the VM's SSH keys, keys.py; sestore.toml, its secure-store items); firmware/, the
                                 VM's firmware (its paks, manifest.toml and cache/, the partition map:
                                 firmware.py), or, with fw an external directory, firmware-cache/ (its map);
                                 start.lock, {"pid": the starting process}: the VM is started once
  <library>/vms/.new-*           scratch of a New VM being made (newvm.py)
  <library>/vms/.clone-*         a clone being assembled here, or staged here for a host (clone.py)

The machine's settings, every library's: $XDG_CONFIG_HOME/beanjar/config.toml, max_running (how many
VMs may run at once: running_refusal), with .running.lock beside it (running_lock), and
firmware_dirs (the host's firmware collections: paths.firmware_dirs).

VM files use vmconfig; library settings and firmware manifests retain their flat schema.

A VM runs as the launcher (launch.py, `vm start --dir`) started it: the transient systemd user
service named in run/unit (beanjar-vm-<library tag>-<name> in beanjar.slice: vm_unit_name, unit.py), or without a user systemd a session of its own,
so it outlives the manager. Its state is always read back from the unit, run/qemu.pid, logs/console.log and QMP,
never from a parent's memory.

This module is the library and its VMs (Library, Vm); what they are made of is in modules of its
own: vmsettings (vm.toml's settings, validate, launcher_env), tomlfile (the files' headers,
write_toml), vmidentity (MAC, serial, keys, slots), vmunits (the systemd units, reasons),
hostconfig (this machine's settings), hostprocs (ports, processes), poweroff (QMP status, the
guest's power-off), framing (screenshots) and capacity (memory admission, listen ports).
"""

import copy
import json
import os
import platform as platform_module
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import (
    base,
    capture_relay,
    countries,
    cpus,
    dbus,
    nvm,
    targets,
    paths,
    vmconfig,
    capacity,
    framing,
    hostconfig,
    hostprocs,
    poweroff,
    tomlfile,
    vmidentity,
    vmsettings,
    vmunits,
)
from .dbus import SCREENSHOT_NAME, SCREENSHOT_PATH  # noqa: F401 (the library's names for them)
from .base import (  # noqa: F401 (the library's names for them)
    NAME_RE,
    Error,
    addresses_of,
    default_library,
    identity_dir,
    in_unit,
    library_of,
    proc_alive,
    read_pid,
    read_toml,
    slot_domain,
    vm_address,
)


OFF, STARTING, RUNNING, STOPPING = "Off", "Starting", "Running", "Stopping"


OWN_FW = "firmware"  # the VM's own firmware, <vm>/firmware: vm.toml without fw (firmware.py)


EXTERNAL_CACHE = "firmware-cache"  # <vm>/firmware-cache: the partition map of an external fw


def default_repo():
    return paths.TREE.top


def vm_firmware(vm_dir, fw=None):
    """The firmware of the VM in vm_dir, as vm.toml's fw names it: (FW, FW_CACHE). No fw (or one that
    is the VM's own firmware/): <vm>/firmware, whose cache/ has what the launcher derives from it,
    the partition map (partmap.py). Any other directory (an absolute path, or one relative to the
    VM's folder) is used in place and never written: its map goes into the VM's firmware-cache/."""
    own = os.path.join(vm_dir, OWN_FW)
    d = os.path.normpath(os.path.join(vm_dir, os.path.expanduser(str(fw)))) if fw else own
    return (
        (own, os.path.join(own, "cache")) if d == own else (d, os.path.join(vm_dir, EXTERNAL_CACHE))
    )


# The settings a VM's prepared board data depends on (prepare.Job): a change prepares it again.
BOARD_SETTINGS = ("fw", "broadcast", "model", "debug_mode", "serial", "country")


def prepare_board_data(lib, vm_dir, out=None, force=False):
    """The launcher's prepare (prepare.py) of the VM in vm_dir: its board data worked out again if it
    is missing or stale (force: always), each line of what it says to out; Error if it fails."""
    rc = hostprocs.run_streamed(
        [*lib.launcher(), "prepare", "--dir", vm_dir, *(["--force"] if force else [])],
        out,
        cwd=lib.repo,
    )
    if rc != 0:
        raise Error(f"the VM's board data could not be prepared (launcher exit {rc})")


def vm_launcher_env(path):
    """The launcher's environment of the VM whose vm.toml is at path (launch.py, --dir): FW and
    FW_CACHE as vm_firmware has them."""
    cfg = vmconfig.read(path)
    if cfg is None:
        raise Error(f"{path}: no such file")
    try:
        env = vmsettings.launcher_env(cfg)
    except Error as e:
        raise Error(f"{path}: {e}", e.key) from None
    vm_dir = os.path.dirname(os.path.abspath(path))
    env["FW"], env["FW_CACHE"] = vm_firmware(vm_dir, env.get("FW"))
    return env


RUNNING_LOCK = ".running.lock"


def home_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home + os.sep) else path


def running_label(unit, props):
    """A running VM as a refusal names it: a library VM's name and library (from its RUN_DIR,
    <library>/vms/<name>/run, without reading the library), else its unit and RUN_DIR."""
    run_dir = vmunits.unit_run_dir(props) or ""
    vm_dir = os.path.dirname(run_dir)
    if os.path.basename(run_dir) == "run" and os.path.basename(os.path.dirname(vm_dir)) == "vms":
        return (
            f"{os.path.basename(vm_dir)} of {home_path(os.path.dirname(os.path.dirname(vm_dir)))}"
        )
    return unit + (f" ({home_path(run_dir)})" if run_dir else "")


def running_refusal(name, run_dir, unit=None, units=None, limit=None):
    """None if VM name (its RUN_DIR run_dir, its unit unit) may start beside the VMs that run on this
    host (vm_units: every library's, the development VMs', the regress harness's), else why not:
    max_running of them already run. A unit that runs this VM (a restart) is not counted. limit:
    max_running, if read already; units: a vm_units() snapshot."""
    limit = hostconfig.max_running() if limit is None else limit
    if limit is None:
        return None
    units = vmunits.vm_units() if units is None else units
    run_dir = os.path.abspath(run_dir)
    others = sorted(
        (u, p) for u, p in units.items() if u != unit and vmunits.unit_run_dir(p) != run_dir
    )
    if len(others) < limit:
        return None
    return (
        f"cannot start VM {name}: {len(others)} VM{'s' if len(others) != 1 else ''} already running "
        f"and the limit is {limit} (max_running): {', '.join(running_label(u, p) for u, p in others)}. "
        f"Stop one, or raise the limit: beanjar host config set max_running={len(others) + 1}"
    )


class running_lock:
    """Held while a start checks max_running and starts its unit (launch.py Bg.start), so that two
    starts at the same moment cannot both take the last place: fcntl.flock on .running.lock beside
    the machine's config.toml. Only while there is a limit (.limit, max_running as read)."""

    def __enter__(self):
        import fcntl

        self.f = None
        self.limit = hostconfig.max_running()
        if self.limit is not None:
            os.makedirs(hostconfig.global_config_dir(), exist_ok=True)
            self.f = open(os.path.join(hostconfig.global_config_dir(), RUNNING_LOCK), "a")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.f:
            self.f.close()


def unit_vm(props):
    """The library VM a unit (vm_units' props) runs: its RUN_DIR is <library>/vms/<name>/run of a
    library (a vms/ folder, which config.toml need not exist for); else None (a regress target's runtime directory, say)."""
    run_dir = vmunits.unit_run_dir(props)
    if not run_dir or os.path.basename(run_dir) != "run":
        return None
    vm_dir = os.path.dirname(run_dir)
    vms_dir = os.path.dirname(vm_dir)
    root = os.path.dirname(vms_dir)
    if os.path.basename(vms_dir) != "vms" or not os.path.isfile(os.path.join(vm_dir, "vm.toml")):
        return None
    try:
        return Library(root).vm(os.path.basename(vm_dir))
    except (Error, OSError):
        return None


# The ways Library.stop stops a VM (host/core/README.md, "Stopping a VM"): the power-off signal
# (webOS powers the set off), and unplug (QEMU killed at once, as
# a set losing its power).
POWER_OFF, UNPLUG = "power-off", "unplug"


# A call refused as busy (power_off_busy) is repeated this many times in all, this many seconds apart.
POWER_OFF_TRIES, POWER_OFF_RETRY_S = 4, 3.5


# The launcher's stop --power-off exits with these when QEMU is still running: the signal was sent
# and QEMU did not exit within the timeout (the guest may be shutting down: a later stop waits
# again), or webOS refused the signal (power_off_refused), so none is pending.
STOP_TIMED_OUT, STOP_REFUSED = 4, 5


def fmt_duration(seconds):
    if seconds is None:
        return ""
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def read_model_key(source):
    """The model key in the file source, checked as host/core/beanjar/modelkey.py does (44 characters of
    canonical base64 for 32 bytes): its bytes, or an Error (key model_key) that never has the value."""
    from . import modelkey

    try:
        with open(os.path.expanduser(source), "rb") as f:
            return modelkey.validate(f.read(128))
    except ValueError as e:
        raise Error(f"{source}: {e}", "model_key") from None
    except OSError as e:
        raise Error(f"{source}: {e.strerror}", "model_key") from None


class VM:
    def has_model_key(self):
        from . import sestore

        try:
            return sestore.has_model(self.identity_dir)
        except sestore.Error as error:
            raise Error(str(error)) from None

    def model_key_item(self):
        """The secure-store item its model key goes in (sestore.model_item): the board's regional
        one where its firmware's readers ask for that (targets.regional_model_key), else the plain."""
        from . import sestore

        target = self.target()
        try:
            broadcast = targets.board_broadcast(
                target, self.firmware_dir(), self.config.get("broadcast"), self.config.get("model")
            )
        except targets.Error as error:
            raise Error(str(error), "broadcast") from None
        return sestore.model_item(targets.regional_model_key(target), broadcast)

    def __init__(self, library, name):
        self.library = library
        self.name = name
        self.dir = os.path.join(library.vms_dir, name)
        self.run_dir = os.path.join(self.dir, base.RUN_DIR)
        self.logs_dir = os.path.join(self.dir, "logs")
        self.state_dir = os.path.join(self.dir, "state")
        self.identity_dir = identity_dir(self.dir)
        self.pidfile = os.path.join(self.run_dir, base.PIDFILE)
        self.unit_file = os.path.join(self.run_dir, base.UNIT_FILE)
        self.viewer_pidfile = os.path.join(self.run_dir, "viewer.pid")
        self.vnc_pidfile = os.path.join(self.run_dir, "vnc.pid")  # beanjar-vnc (Library.vnc)
        self.vnc_file = os.path.join(self.run_dir, "vnc.json")  # where it listens (VM.vnc_info)
        # the capture relay's on-demand render sink, a headless viewer (capture_relay.py)
        self.capture_sink_pidfile = os.path.join(self.run_dir, capture_relay.SINK_PIDFILE)
        self.capture_sink_hold = os.path.join(self.run_dir, capture_relay.SINK_HOLD)
        # beanjar-vnc's RFB without authentication, owner-only (0600): beanjar vnc pipe's
        self.vnc_local_sock = os.path.join(self.run_dir, "rfb-local.sock")
        # beanjar-vnc's mixed audio, owner-only (0600): beanjar vnc audio-pipe's
        self.audio_local_sock = os.path.join(self.run_dir, "audio-local.sock")
        # beanjar-vnc's picture as H.264, owner-only (0600): beanjar vnc video-pipe's
        self.video_local_sock = os.path.join(self.run_dir, "video-local.sock")
        # its password, owner-only, kept with the VM's identity (keys.ensure_vnc_password)
        self.vnc_password_file = os.path.join(self.identity_dir, "vnc.password")
        self.stopping_file = os.path.join(self.run_dir, "stopping")
        # QEMU's pid, when the power-off signal was sent to it (launch.py Bg.power_off)
        self.power_off_file = os.path.join(self.run_dir, poweroff.POWER_OFF_SENT)
        self.lock_path = os.path.join(self.dir, "start.lock")
        self.console_log = os.path.join(self.logs_dir, "console.log")
        self.qmp_sock = os.path.join(self.run_dir, "qmp.sock")
        self.dbus_sock = os.path.join(self.run_dir, "dbus.sock")
        # the last thumbnail taken (Library.screenshot), kept after the VM stops (beanjar-web's
        # Off card shows it)
        self.thumbnail_file = os.path.join(self.dir, "thumbnail.png")

    # -- configuration --
    @property
    def config_path(self):
        return os.path.join(self.dir, base.CONFIG)

    @property
    def config(self):
        """vm.toml's settings (a copy the caller may change). Read once per VM object while the file
        stays the same (its inode, size and mtime): a refresh asks a VM for it a dozen times."""
        try:
            st = os.stat(self.config_path)
            sig = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        except OSError:
            sig = None
        cached = getattr(self, "_config", None)
        if sig is None or cached is None or cached[0] != sig:
            cached = self._config = (sig, vmconfig.read(self.config_path, {}))
        return copy.deepcopy(cached[1])

    @property
    def config_unchecked(self):
        """vm.toml's settings as config reads them, but without the check of broadcast against the
        firmware: for the commands that fix a bad value (configure, settings show). Not cached."""
        return vmconfig.read(self.config_path, {}, check_broadcast=False)

    def save_config(self, cfg):
        vmconfig.write(self.config_path, cfg, tomlfile.VM_HEADER)

    @property
    def display(self):
        return self.config.get("display", "none")

    def firmware_dir(self):
        """Its firmware/, or the external directory vm.toml's fw names (vm_firmware)."""
        return vm_firmware(self.dir, self.config.get("fw"))[0]

    def firmware_external(self):
        """Whether it boots an external directory (fw) instead of its own firmware/."""
        return self.firmware_dir() != os.path.join(self.dir, OWN_FW)

    def target(self):
        """The firmware target (chip, release) it boots (paths.firmware_target: its firmware's manifest,
        else the external directory's name); Error if it is not one this tree boots."""
        try:
            return paths.firmware_target(self.firmware_dir())
        except ValueError as e:
            raise Error(str(e), "fw") from None

    def launch_env(self):
        """The environment a launch of it sees: the manager's, with vm.toml's settings (ACCEL, MEM, ...)."""
        try:
            return dict(os.environ, **vmsettings.launcher_env(self.config))
        except Error:
            return dict(os.environ)

    def mem(self):
        """Its RAM in MiB: vm.toml's mem, else its firmware's model's (nvm.board_ram: the DDR size of
        the board.cmdline's hwopt=, what the launcher gives a VM without MEM); None if its firmware
        is not one this tree boots."""
        if self.config.get("mem"):
            return self.config["mem"]
        try:
            target = self.target()
            model = self.config.get("model")
            broadcast = targets.board_broadcast(
                target, self.firmware_dir(), self.config.get("broadcast"), model
            )
            return nvm.board_ram(target, paths.Tree(self.library.repo), broadcast, model)
        except (Error, targets.Error, OSError):
            return None

    def userland(self):
        """The userland (native or qemu-user) its next start resolves to on this host
        (qemu.vm_userland), None for one that does not resolve."""
        from . import qemu

        return qemu.vm_userland(self.launch_env())

    def guest_ram(self):
        """The guest's RAM in MiB (QEMU's -m): its TV's (mem) with qemu-user's overhead on top
        when its userland resolves to qemu-user (vmsettings.guest_ram_mib); None if mem is not
        known. What the host must have for it (capacity) is this."""
        mem = self.mem()
        return None if mem is None else vmsettings.guest_ram_mib(mem, self.userland())

    def host_swap(self):
        """The size in MiB of its swap disk in host RAM (hostswap.py): under qemu-user
        vmsettings.qemu_user_swap_mib of its TV's RAM, else 0; 0 too if mem is not known. Not
        counted by the admission (capacity), only by its unit's MemoryHigh."""
        mem = self.mem()
        return 0 if mem is None else vmsettings.qemu_user_swap_mib(mem, self.userland())

    def platform_title(self):
        """Its platform with its version as the viewer's title shows it ("webOS 4.5": its target's,
        targets.platform_title); empty if its firmware is not one this tree boots."""
        try:
            return targets.platform_title(self.target())
        except Error:
            return ""

    def touch(self):
        """Whether its model has a touch screen (targets.Soc.touch of its target); False if its firmware
        is not one this tree boots."""
        try:
            return targets.soc(self.target()).touch
        except Error:
            return False

    def portrait(self):
        """Whether its model turns to portrait (targets.Soc.portrait of its target: beanjar sensor set rotate,
        the viewers' turned picture); False if its firmware is not one this tree boots."""
        try:
            return targets.soc(self.target()).portrait
        except Error:
            return False

    def firmware_name(self):
        """The firmware as the list names it: its manifest's name (what it was made from), else
        the directory's name."""
        d = self.firmware_dir()
        m = read_toml(os.path.join(d, "manifest.toml"), None)
        return str((m or {}).get("name") or os.path.basename(d.rstrip("/")))

    # -- run state --
    def unit_name(self):
        """The systemd unit the VM was last started as (run/unit), or None (the setsid fallback)."""
        return base.unit_name(self.unit_file)

    def unit(self):
        """The unit's properties (unit_show) if the VM was started as one, else {}."""
        name = self.unit_name()
        return dict(vmunits.unit_show(name), Unit=name) if name else {}

    def unit_active(self, units=None):
        """Its unit's ActiveState, None if it was not started as one; from units (a vm_units()
        snapshot, in which a unit that is not up is inactive) if given, else asked of systemd."""
        name = self.unit_name()
        if not name:
            return None
        if units is not None:
            return units.get(name, {}).get("ActiveState", "inactive")
        return vmunits.unit_show(name).get("ActiveState")

    def unit_up(self, units=None):
        return self.unit_active(units) in vmunits.UNIT_UP

    def reason(self):
        """Why it runs (unit_reason), None if no reason was given or it is not running."""
        return vmunits.unit_reason(self.unit_name())

    def set_reason(self, text, by=None):
        """Set or clear ("") why it runs (set_unit_reason); the VM must run as a unit."""
        unit = self.unit_name()
        if not self.alive():
            raise Error(f"VM {self.name} is off: a reason is for a running VM")
        if not (unit and self.unit_up()):
            raise Error(
                f"VM {self.name} runs without a systemd unit: a reason lasts as long as the "
                "VM's unit, so it needs one"
            )
        return vmunits.set_unit_reason(unit, text, by)

    def pid(self):
        """QEMU's pid if it is running (in its unit, for a VM started as one); a stale pidfile is
        removed."""
        return base.qemu_pid(self.pidfile, self.unit_file)

    def viewer_pid(self):
        return self._frontend_pid(self.viewer_pidfile, "beanjar-viewer")

    def vnc_pid(self):
        return self._frontend_pid(self.vnc_pidfile, "beanjar-vnc")

    def capture_sink_pid(self):
        """The capture relay's sink's pid while it runs (the relay keeps its pidfile)."""
        pid = read_pid(self.capture_sink_pidfile)
        return pid if proc_alive(pid, "viewer-headless") else None

    def vnc_info(self):
        """beanjar-vnc while it serves this VM, else None: {pid, url, password_file}, the url with
        link lan the VM's LAN address (port 5900, in the LAN namespace, which has its own), and then local_url, this host's way in (its own
        address), as for the inspector. The password stays in its owner-only file."""
        pid = self.vnc_pid()
        if not pid:
            return None
        try:
            with open(self.vnc_file) as f:
                where = json.load(f)
        except (OSError, ValueError):
            where = {}
        host = where.get("listen") or self.address() or ""
        url = (
            f"vnc://{f'[{host}]' if ':' in host else host}:{where.get('port') or Library.VNC_PORT}"
        )
        out = {"pid": pid, "url": url}
        lan = where.get("lan") and self.lan_address()
        if lan:
            from . import netns

            out = {"pid": pid, "url": f"vnc://{lan}:{netns.VNC_PORT}", "local_url": url}
        out["password_file"] = (
            self.vnc_password_file if os.path.isfile(self.vnc_password_file) else None
        )
        return out

    def ensure_vnc_password(self):
        """The VM's VNC password file (identity/vnc.password), made with a random password the
        first time it is needed (keys.ensure_vnc_password); its path."""
        from . import keys

        try:
            return keys.ensure_vnc_password(vmidentity.make_identity_dir(self.dir))
        except (keys.Error, OSError) as e:
            raise Error(f"{self.name}: VNC password: {e}") from None

    def _frontend_pid(self, pidfile, name):
        """A display front end's pid if it runs; a stale pidfile is removed."""
        pid = read_pid(pidfile)
        if pid is None:
            return None
        if proc_alive(pid, name):
            return pid
        try:
            os.unlink(pidfile)
        except OSError:
            pass
        return None

    def _stopper(self):
        pid = read_pid(self.stopping_file)
        if pid is not None and proc_alive(pid):
            return pid
        if pid is not None:
            try:
                os.unlink(self.stopping_file)
            except OSError:
                pass
        return None

    def state(self, qmp=False, units=None):
        """(state, detail): Off, Starting, Running or Stopping; with qmp, a QEMU run state other
        than "running" (paused, shutdown, ...) is the detail. units: a vm_units() snapshot to read
        the unit's state from instead of asking systemd (the window, which must not block on it)."""
        pid = self.pid()
        starting = pid is None and self.lock_owner(units) is not None
        if pid is None:
            if starting:
                return STARTING, "launching"
            # QEMU is gone but its unit's helpers are still being stopped
            return (STOPPING, "") if self.unit_active(units) == "deactivating" else (OFF, "")
        if self._stopper() or self.unit_active(units) == "deactivating":
            return STOPPING, ""
        detail = ""
        if qmp:
            st = poweroff.qmp_status(self.qmp_sock)
            if st and st != "running":
                detail = st
        return (RUNNING if poweroff.console_booted(self.console_log) else STARTING), detail

    def alive(self):
        return self.state()[0] != OFF

    def power_off_refusal(self):
        """Why the guest cannot take the power-off signal now (Library.stop's POWER_OFF), or None if
        it can: its target has tvpowerd (targets.takes_power_off), QEMU runs it (not paused, not in
        Quick Start+'s standby: qmp_status), it has booted to the initramfs shell, and its
        firmware has started its power stack (micom_ready). It reads QMP and a file and types
        nothing on the console: the GUI's dialog asks it on the main loop."""
        try:
            target = self.target()
        except Error as e:
            return str(e)
        if not targets.takes_power_off(target):
            return f"{targets.platform(target)} has no power-off signal"
        pid = self.pid()
        if not pid:
            return "it is not running"
        if read_pid(self.power_off_file) == pid:
            return None  # sent already, and webOS shutting down: a stop waits for it again
        if not poweroff.console_booted(self.console_log):
            return "it is still booting"
        st = poweroff.qmp_status(self.qmp_sock)
        if st == "standby":
            return "it is in standby"
        if st not in (None, "running"):  # None: QMP busy with another client
            return f"QEMU is {st}"
        if targets.platform(target) == "webOS" and not poweroff.micom_ready(self.run_dir):
            return "webOS is still starting"
        return None

    # -- the start lock: one start of the VM at a time --
    def lock_owner(self, units=None):
        """The pid that started the VM while its start lock is valid (that process is alive, or
        the VM runs: the lock is kept until it stops); a stale lock is removed and None returned."""
        data = tomlfile.read_json(self.lock_path, None) if os.path.exists(self.lock_path) else None
        if not data:
            return None
        if proc_alive(data.get("pid")) or self.pid() or self.unit_up(units):
            return data.get("pid") or -1
        self.release()
        return None

    def acquire(self):
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                if self.lock_owner() is not None:
                    raise Error(f"VM {self.name} is already being started or running")
                continue
            with os.fdopen(fd, "w") as f:
                json.dump({"pid": os.getpid()}, f)
            return
        raise Error(f"could not take {self.lock_path}")

    def release(self):
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass

    def uptime(self):
        pid = self.pid()
        return hostprocs.uptime_of(pid) if pid else None

    def address(self):
        """Its host address (its library's addresses and its slot), on which its ports are; None
        without a slot."""
        return base.vm_host_address(self.dir, self.config)

    def service_host(self):
        """Where this host reaches its TV-service ports (keys.service_host): listen when that is one
        address, else its own address; None without a slot."""
        return base.vm_service_host(self.dir, self.config)

    def forwards(self, cfg=None):
        """[(host, guest, layer)]: its forwarded ports, as qemu.plan forwards them
        (layermeta.forwards): those of its layers and its target's always-used ones (SSAP 3000 and 3001, root SSH 6622 or
        SSH_PORT, developer mode 9922 and 9991), then its own forward= ones; Error (key forward) if
        one of those clashes with the others or with beanjar's own ports. cfg: settings to use in
        place of its vm.toml's (configure's, before they are saved)."""
        from . import layermeta

        cfg = self.config if cfg is None else cfg
        try:
            always = list(
                targets.always_layers(
                    paths.firmware_target(vm_firmware(self.dir, cfg.get("fw"))[0])
                )
            )
        except (Error, ValueError):
            always = []
        names = list(cfg.get("layers") or []) + always
        try:
            return layermeta.forwards(
                names,
                layermeta.load(),
                cfg.get("SSH_PORT"),
                layermeta.parse_forward(cfg.get("forward", "")),
            )
        except layermeta.Error as e:
            raise Error(str(e), "forward") from None

    def listeners(self):
        """[(address, port)] its forwarded ports (forwards()) listen on when it runs: listen, else
        its own address; with link lan its own address (the host side of its relay; on the LAN they
        are on its LAN address); none with link off, and none with link lan and mode direct (the
        guest's own ports on its own LAN address, nothing on this host)."""
        addr = self.address()
        link = self.config.get("link", "nat")
        if addr is None or link == "off" or vmsettings.lan_direct(link, self.config.get("mode")):
            return []
        listen = (self.config.get("listen") if link == "nat" else None) or addr
        ports = []
        for host, _guest, _name in self.forwards():
            if (listen, host) not in ports:
                ports.append((listen, host))
        return ports

    def lan_hostname(self):
        """The hostname it sends in DHCP with link lan (its name as a DNS label), else None."""
        return vmsettings.dns_label(self.name) if self.config.get("link") == "lan" else None

    def lan_address(self):
        """Its address on the LAN while it runs with link lan (the lease netns.lan wrote), else None."""
        if self.config.get("link") != "lan" or not self.pid():
            return None
        from . import netns

        info = netns.lan_info(self.run_dir)
        return info and info["address"]

    def summary(self):
        c = self.config
        result = {k: c.get(k) for k in vmsettings.SETTINGS}
        result["mitm_flows"] = c.get("mitm_flows", vmsettings.MITM_FLOWS)
        result["mitm_flow_bytes"] = c.get("mitm_flow_bytes", "0")
        result["tags"] = c.get("tags", [])
        result["slot"], result["address"] = c.get("slot"), self.address()
        result["lan_address"] = self.lan_address()
        result["lan_hostname"] = self.lan_hostname()
        try:
            result["platform"] = targets.platform(self.target())
        except Error:
            result["platform"] = None
        return result


class Library:
    def __init__(self, root=None, repo=None):
        self.root = base.library_root(root)
        self.vms_dir = os.path.join(self.root, base.VMS_DIR)
        os.makedirs(self.vms_dir, exist_ok=True)
        self.config_path = os.path.join(self.root, "config.toml")
        self.config = read_toml(self.config_path, {})
        # --repo names the checkout for this run only; the stored one changes through
        # `beanjar library config set repo=DIR` (set_option), never as a side effect
        self.override = os.path.abspath(repo) if repo else None
        # VMs made before slots get theirs once (a vm.toml that does not read is left to its own errors)
        for name in self.names():
            try:
                if (
                    vmconfig.read(os.path.join(self.vms_dir, name, "vm.toml"), {}).get("slot")
                    is None
                ):
                    vmidentity.ensure_slot(os.path.join(self.vms_dir, name))
            except Error:
                pass

    @property
    def known_repo(self):
        """The checkout this run uses, or None: --repo, else the package's own files when
        installed, else the one stored in config.toml, else the running tree (never written)."""
        if self.override:
            return self.override
        if paths.TREE.installed:
            # Packaged runs use package-owned tools even for an existing checkout library.
            return paths.TREE.top
        # first use from a checkout: the tree the running code comes from, in memory only
        return self.config.get("repo") or default_repo() or None

    @property
    def repo(self):
        found = self.known_repo
        if not found:
            raise Error(
                "no Beanjar checkout is known for this library: run "
                "`beanjar library config set repo=DIR`, or pass --repo DIR for one command"
            )
        return found

    @property
    def addresses(self):
        return addresses_of(self.root)

    @property
    def mac_prefix(self):
        return vmidentity.parse_mac_prefix(
            self.config.get("mac_prefix") or vmidentity.DEFAULT_MAC_PREFIX
        )

    def new_identity(self, skip=None):
        """A new VM's (mac, serial): the library's mac_prefix and random octets, a random serial,
        neither one another VM of the library has (skip: a VM's name, not counted)."""
        macs, serials = vmidentity.identities(self.vms_dir, skip)
        return vmidentity.new_mac(self.mac_prefix, macs), vmidentity.new_serial(serials)

    def set_option(self, key, value):
        """One setting of config.toml (e.g. the GUI's auto_reconnect, the CLI's library config)."""
        if key not in vmsettings.CONFIG_KEYS:
            raise Error(
                f"unknown library setting {key} (one of {', '.join(vmsettings.CONFIG_KEYS)})"
            )
        if key == "mac_prefix":
            value = vmidentity.parse_mac_prefix(value)
        elif key == "auto_reconnect" and not isinstance(value, bool):
            if str(value).lower() not in ("true", "false"):
                raise Error("auto_reconnect is true or false", key)
            value = str(value).lower() == "true"
        elif key in ("repo", "epk2extract"):
            value = os.path.abspath(os.path.expanduser(str(value)))
        elif key == "lan_parent":
            value = str(value).strip()
            if value:
                value = vmidentity.parse_lan_parent(value)
        elif key == "addresses":
            from . import keys

            base, libraries = slot_domain(self.root)
            if base != self.root:
                raise Error(
                    f"addresses is the library's ({base}), with its development libraries", key
                )
            try:
                value = keys.parse_addresses(value)
            except keys.Error as e:
                raise Error(str(e), key) from None
            if value != self.addresses:
                # every VM's ports, pins (identity/known_hosts, rewritten at its next start) and
                # forwards follow it: none of the domain's VMs may be up
                up = [
                    vm.name
                    for root in libraries
                    for vm in (self if root == self.root else Library(root)).vms()
                    if vm.alive()
                ]
                if up:
                    raise Error(
                        f"addresses changes only while every VM of the library is off: stop {', '.join(up)}",
                        key,
                    )
        cfg = dict(read_toml(self.config_path, {}), **{key: value})
        if key == "lan_parent" and not value:  # empty: unset
            del cfg[key]
            self.config.pop(key, None)
        else:
            self.config[key] = value
        tomlfile.write_toml(self.config_path, cfg, tomlfile.CONFIG_HEADER, vmsettings.CONFIG_KEYS)

    # -- tools --
    def launcher(self):
        """The launcher's command (the checkout's host/scripts/vm-launcher; BEANJAR_LAUNCHER: a stand-in), which
        takes `start|stop|status --dir VM-DIR [words]`."""
        fake = os.environ.get("BEANJAR_LAUNCHER")
        return (
            [fake]
            if fake
            else [sys.executable, paths.Tree(self.repo).src("host", "scripts", "vm-launcher")]
        )

    def viewer(self):
        return os.environ.get("BEANJAR_VIEWER") or paths.Tree(self.repo).build(
            "viewer", "beanjar-viewer"
        )

    def vnc_server(self):
        """beanjar-vnc (host/vnc; BEANJAR_VNC: a stand-in)."""
        return os.environ.get("BEANJAR_VNC") or paths.Tree(self.repo).build("vnc", "beanjar-vnc")

    def vnc_has_video(self):
        """Whether beanjar-vnc serves --video-local (it is built without it when libavcodec or
        libswscale is missing): its --help names it."""
        try:
            r = subprocess.run(
                [self.vnc_server(), "--help"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "--video-local" in r.stdout

    # -- VMs --
    def names(self):
        try:
            return sorted(
                n
                for n in os.listdir(self.vms_dir)
                if not n.startswith(".")
                and os.path.isfile(os.path.join(self.vms_dir, n, "vm.toml"))
            )
        except FileNotFoundError:
            return []

    def vms(self):
        return [VM(self, n) for n in self.names()]

    def vm(self, name):
        base.vm_dir(self.root, name)  # Error for an invalid name or no such VM
        return VM(self, name)

    def layers(self, target):
        """The layers of the checkout for a firmware target (chip, release; a VM's own, VM.target(); None:
        the source layers only), by name: {"built": the guest can take it
        (build/layers/<chip>-<release>/<name>, or an optional guest/layers/<name> without a meson.build,
        which guest/initramfs/rootfs/init copies as it is), "path": the directory guest/initramfs/rootfs/init takes (relative
        to the checkout), "description": its layer.toml's or "", "requires", "conflicts": the layers its
        layer.toml names (conflicts declared on either side), "versions", "default_version": a
        versioned layer's (layermeta.Versions; (), None for a layer built once; "built" and "path"
        are its default's)}; the layers a VM always gets included."""
        from . import guestlayers, layermeta, share

        meta = layermeta.load()

        def info(name, **kw):
            m = meta.get(name)
            v = m.versions if m else None
            return dict(
                kw,
                description=m.description if m else "",
                requires=m.requires if m else (),
                conflicts=layermeta.conflicts(meta, name),
                versions=v.available if v else (),
                default_version=v.default if v else None,
            )

        def resolve(tree, name):
            v = meta[name].versions if name in meta else None
            if v is None:
                return share.resolve_layer(tree, target, name)[0]
            try:
                return guestlayers.select(
                    tree.build("components"), target, guestlayers.versioned(name, v.default)
                )
            except (ValueError, OSError):
                return None

        tree = paths.Tree(self.repo)
        if target and (tree.installed or os.path.isdir(tree.build("components"))):
            names = set(targets.always_layers(target))
            for root in (tree.src("guest", "layers"), tree.build("components")):
                try:
                    names.update(
                        n
                        for n in os.listdir(root)
                        if not n.startswith(".") and os.path.isdir(os.path.join(root, n))
                    )
                except FileNotFoundError:
                    pass
            names.discard("release")
            out = {}
            for name in sorted(names):
                directory = resolve(tree, name)
                out[name] = info(
                    name,
                    built=directory is not None,
                    path=os.path.relpath(directory or tree.src("guest", "layers", name), self.repo),
                )
            return out
        out = {}
        subs = [os.path.join("guest", "layers")] + (
            [os.path.join("build", "layers", targets.name(target))] if target else []
        )
        for sub in subs:
            d = os.path.join(self.repo, sub)
            try:
                names = os.listdir(d)
            except FileNotFoundError:
                continue
            for n in names:
                if n.startswith(".") or not os.path.isdir(os.path.join(d, n)):
                    continue
                # guest/layers/soc and guest/layers/webos hold a source directory per chip or release
                built = sub != os.path.join("guest", "layers") or (
                    n not in targets.ALWAYS_LAYERS
                    and not os.path.isfile(os.path.join(d, n, "meson.build"))
                )
                row = out.setdefault(n, info(n, built=False))
                row["built"] = row["built"] or built
                row["path"] = os.path.join(sub, n)  # build/layers/<target>/<name> wins
        return out

    # -- operations --
    def vm_config(
        self,
        mem=None,
        smp=None,
        layers=None,
        link=None,
        mode=None,
        display=None,
        audio=None,
        tags=None,
    ):
        """A new VM's settings (vm.toml's but fw), the defaults filled in, validated. No mem: the VM
        has its firmware's model's RAM (nvm.board_ram) at each launch. tags: its tags (as_tags)."""
        cfg = {
            **({"mem": int(mem)} if mem else {}),
            "smp": int(smp or 4),
            "layers": vmsettings.as_layers(
                layers if layers is not None else vmsettings.DEFAULT_LAYERS
            ),
            "link": link or "nat",
            "mode": mode or "mitm",
            "display": display or "dbus",
        }
        cfg["audio"] = audio or ("dbus" if cfg["display"] == "dbus" else "none")
        if tags:
            cfg["tags"] = vmsettings.as_tags(tags)
        vmsettings.validate(cfg)
        return cfg

    def set_tags(self, name, tags):
        """Make tags (as_tags; none: no tags) the tags of VM name, whether it runs or not: they are
        labels, not settings its launch reads. Its tags."""
        vm = self.vm(name)
        tags = vmsettings.as_tags(tags)
        with vmidentity.slot_lock(self.root):  # with another change of vm.toml at once
            cfg = vm.config
            if tags != cfg.get("tags", []):
                if tags:
                    cfg["tags"] = tags
                else:
                    cfg.pop("tags", None)
                vm.save_config(cfg)
        return tags

    def create(self, name, **kw):
        """A new VM: newvm.create (fw, the settings; out)."""
        from . import newvm

        return newvm.create(self, name, **kw)

    def prepare(self, name, out=None, force=False):
        """Work out the VM's board data from its firmware again if what it has is missing or stale
        (force: whatever it has), as its start would (prepare.py); the VM must be off."""
        vm = self.vm(name)
        if vm.alive():
            raise Error(f"VM {name} is not off; prepare it while it is off")
        prepare_board_data(self, vm.dir, out, force)
        return vm

    def configure(self, name, out=None, prepare=True, **values):
        """Change settings (SETTINGS' keys) of a VM that is off; None leaves one as it is. A change
        of what its board data comes from (the firmware, broadcast, model, debug mode or serial) has it
        prepared again (prepare.py; unless prepare is False: the caller does, as the GUI does in the
        background, a Settings dialog that waits for it would freeze); out(line) gets what that says."""
        vm = self.vm(name)
        if vm.alive():
            raise Error(f"VM {name} is not off; settings change only while it is off")
        cfg = vm.config_unchecked  # a bad broadcast is checked below, so it can be changed here
        for k, v in values.items():
            if v is None:
                continue
            if k not in vmsettings.SETTINGS:
                raise Error(f"unknown setting {k} (one of {', '.join(vmsettings.SETTINGS)})")
            if k == "fw":
                # an external directory, used in place; "" (or the VM's own firmware/) is its own
                v = str(v).strip() and os.path.abspath(os.path.expanduser(str(v).strip()))
                if not v or v == os.path.join(vm.dir, OWN_FW):
                    cfg.pop("fw", None)
                    continue
            if k in ("mem", "smp"):
                try:
                    v = int(v)
                except ValueError:
                    raise Error(f"{k} must be a positive number", k) from None
            if k == "mitm_flows":
                try:
                    # empty: unset, the default
                    v = int(v) if str(v).strip() else vmsettings.MITM_FLOWS
                except ValueError:
                    raise Error("mitm_flows must be a number, 0 for no limit", k) from None
                if v == vmsettings.MITM_FLOWS:
                    cfg.pop(k, None)  # the default, not written out
                    continue
            if k == "mitm_flow_bytes":
                try:
                    n = vmsettings.byte_size(v)
                except ValueError as e:
                    raise Error(f"mitm_flow_bytes={e}", k) from None
                if not n:
                    cfg.pop(k, None)  # no limit, the default
                    continue
                v = str(v).strip().upper()
            if k == "mac":
                v = vmidentity.parse_mac(v)
            elif k == "serial":
                v = vmidentity.parse_serial(v)
            elif k == "userland" and v == vmsettings.DEFAULT_USERLAND:
                cfg.pop(k, None)  # auto is the default, not written out
                continue
            elif k == "guest_kernel" and v == vmsettings.DEFAULT_GUEST_KERNEL:
                cfg.pop(k, None)  # arm64 is the default, not written out
                continue
            elif k == "free_page_reporting" and v == "off":
                cfg.pop(k, None)  # off is the default, not written out
                continue
            elif k == "updatable" and v == "off":
                cfg.pop(k, None)  # off is the default, not written out
                continue
            elif k == "lan_access" and v in ("", "off"):
                cfg.pop(k, None)  # off is the default, not written out
                continue
            elif k == "lg_services" and v in ("", vmsettings.DEFAULT_LG_SERVICES):
                cfg.pop(k, None)  # mock is the default, not written out
                continue
            elif k == "gpu" and v in ("", "auto"):
                cfg.pop(k, None)  # auto is the default, not written out
                continue
            elif k == "broadcast" and not str(v).strip():
                cfg.pop(k, None)  # the firmware's own, else the chip's default board
                continue
            elif k == "model":
                if not str(v).strip():
                    cfg.pop(k, None)  # the board of the broadcast standard
                    continue
                v = str(v).strip().upper()
            elif k == "country":
                if not str(v).strip():
                    cfg.pop(k, None)  # the board's own; a TV state keeps the one it has
                    continue
                try:
                    v = countries.parse(v)
                except countries.Error as e:
                    raise Error(str(e), k) from None
            elif k == "debug_mode" and v in ("", vmsettings.DEFAULT_DEBUG_MODE):
                cfg.pop(k, None)  # debug is the default, not written out
                continue
            elif k == "forward":
                from . import layermeta

                try:
                    v = layermeta.forward_text(layermeta.parse_forward(v))
                except layermeta.Error as e:
                    raise Error(str(e), k) from None
                if not v:
                    cfg.pop(k, None)  # empty: unset, no ports of its own (the default)
                    continue
            elif k in ("listen", "address"):
                # empty: unset (listen: back to the VM's own address; address: dhcp)
                if not str(v).strip():
                    cfg.pop(k, None)
                    continue
                from . import keys

                try:
                    v = keys.listen_address(v) if k == "listen" else keys.lan_address(v)
                except keys.Error as e:
                    raise Error(str(e), k) from None
            cfg[k] = vmsettings.as_layers(v) if k == "layers" else v
        vmsettings.validate(cfg)
        if cfg.get("forward"):
            vm.forwards(cfg)  # its own ports against its layers' and beanjar's (layermeta.forwards)
        d, _cache = vm_firmware(vm.dir, cfg.get("fw"))
        if not paths.root_pak(d):
            raise Error(f"firmware {d}: no rootfs.pak or root.pak", "fw")
        if cfg.get("broadcast") or cfg.get("model"):
            # before anything is saved
            vmconfig.check_firmware_broadcast(d, cfg.get("broadcast"), cfg.get("model"))
        if cfg.get("country") and any(
            values.get(k) is not None for k in ("country", "fw", "broadcast", "model")
        ):
            # the board's country group, after the broadcast standard that picks the board
            try:
                target = paths.firmware_target(d)
                countries.check(
                    d,
                    target,
                    cfg["country"],
                    paths.Tree(self.repo),
                    targets.board_broadcast(target, d, cfg.get("broadcast"), cfg.get("model")),
                    cfg.get("model"),
                )
            except (ValueError, OSError, targets.Error) as e:
                raise Error(f"country: {e}", "country") from None
            except countries.Error as e:
                raise Error(str(e), "country") from None
        if values.get("fw") is not None:
            # the platform is the firmware's (its target's)
            try:
                paths.firmware_target(d)
            except ValueError as e:
                raise Error(str(e), "fw") from None
        userland = cfg.get("userland", vmsettings.DEFAULT_USERLAND)
        guest_kernel = cfg.get("guest_kernel", vmsettings.DEFAULT_GUEST_KERNEL)
        if guest_kernel != vmsettings.DEFAULT_GUEST_KERNEL:
            refusal = vmsettings.guest_kernel_host_refusal(guest_kernel, platform_module.machine())
            if refusal:
                raise Error(refusal, "guest_kernel")
            refusal = vmsettings.guest_kernel_package_refusal(guest_kernel, paths.Tree(self.repo))
            if refusal:
                raise Error(refusal, "guest_kernel")
        if userland == "qemu-user" or guest_kernel != vmsettings.DEFAULT_GUEST_KERNEL:
            # refused on some platforms (vmsettings.NATIVE_ONLY)
            try:
                platform = targets.platform(paths.firmware_target(d))
            except ValueError as e:
                raise Error(str(e), "fw") from None
            refusal = vmsettings.userland_refusal(platform, userland, guest_kernel)
            if refusal:
                raise Error(refusal, refusal.split("=", 1)[0])
        vm.save_config(cfg)
        if prepare and any(values.get(k) is not None for k in BOARD_SETTINGS):
            prepare_board_data(self, vm.dir, out)
        return vm

    def set_model_key(self, name, source):
        """The VM's model key in sestore.toml from the file source, checked as
        host/core/beanjar/modelkey.py does (44 characters of canonical base64 for 32 bytes), as the
        item its firmware's readers ask for (VM.model_key_item); source None removes all model
        items (the guest follows its missing policy). The VM must be off."""
        vm = self.vm(name)
        if vm.alive():
            raise Error(f"VM {name} is not off; its model key changes only while it is off")
        vmidentity.make_identity_dir(vm.dir)
        from . import sestore

        value = None if source is None else read_model_key(source)
        item = sestore.MODEL if value is None else vm.model_key_item()
        try:
            sestore.set_model(vm.identity_dir, value, item)
        except sestore.Error as error:
            raise Error(str(error), "model_key") from None
        return vm

    def start(self, name, out=None, args=(), reason=None, by=None):
        """Take the VM's start lock and boot it with the launcher in a session of its own; returns
        when the launcher does (the initramfs shell is up, or it failed). out(line) gets its output.
        Other VMs may run: each binds its ports on its own address (its slot); it is refused only if
        the host's free memory does not cover it (admit), or the machine's max_running VMs already run
        (running_refusal). reason: why it runs (VM.set_reason, with the label by), which needs
        a user systemd (the VM's unit holds it)."""
        vm = self.vm(name)
        if vm.alive():
            raise Error(f"VM {name} is already {vm.state()[0].lower()}")
        reason = vmunits.reason_text(reason)
        if reason:
            vmunits.reason_text(by, "label", vmunits.REASON_BY_MAX)
            from . import runner

            if runner.choose() != runner.SYSTEMD:
                raise Error(
                    "--reason needs a user systemd: a reason lasts as long as the VM's unit, and "
                    "without one the VM would run in a session of its own"
                )
        # the machine's max_running, before anything changes (the launcher asks again, under
        # running_lock, as it starts the unit)
        why = running_refusal(name, vm.run_dir, vmunits.vm_unit_name(vm.dir))
        if why:
            raise Error(why)

        if vm.config.get("link", "nat") == "lan":
            if vmsettings.lan_direct("lan", vm.config.get("mode")) and (
                vm.config.get("lg_services", vmsettings.DEFAULT_LG_SERVICES) != "real"
            ):
                raise Error(vmsettings.LAN_DIRECT_LG, "lg_services")
            parent = vmidentity.lan_parent_of(self.root)
            if not parent:
                raise Error(
                    "link=lan needs the library's lan_parent, its wired interface "
                    "(beanjar library config set lan_parent=eth0)",
                    "link",
                )
            vmidentity.parse_lan_parent(parent)  # still there, and not wireless
        # a target that has never booted (targets.untested_warning) starts, with a warning
        try:
            warning = targets.untested_warning(vm.target())
        except Error:
            warning = None
        if warning:
            (out or print)(f"warning: {warning}")
        with capacity.admission_lock(self):
            capacity.admit(vm)
            why = capacity.listen_conflict(vm)
            if why:
                raise Error(why, "listen")
            vm.acquire()
        try:
            rc = hostprocs.run_streamed(
                [*self.launcher(), "start", "--dir", vm.dir, *args], out, cwd=self.repo
            )
        except BaseException:
            if not vm.pid():
                vm.release()
            raise
        if rc != 0 or not vm.pid():
            vm.release()
            raise Error(f"VM {name} did not start (launcher exit {rc}; see {vm.logs_dir}/qemu.out)")
        if reason:
            try:
                vm.set_reason(reason, by)
            except Error as e:
                (out or print)(f"warning: {name} runs, but without its reason: {e}")
        return vm

    def stop(self, name, how=POWER_OFF, fallback=True, out=None, force=False):
        """Stop VM name (host/core/README.md, "Stopping a VM"). how: POWER_OFF, the power-off
        signal: the guest's logs saved, tvpowerd's forced powerOff sent into webOS, and QEMU left
        to exit by itself within POWER_OFF_TIMEOUT s (the launcher's stop --power-off). A guest
        that cannot take it now (VM.power_off_refusal) or that has not powered off in time is
        stopped the old way instead, and out says so: its logs saved (unless they were), its disks
        synced, QEMU's unit stopped (SIGTERM, SIGKILL after 20 s). Without fallback an Error says
        why and the VM is left as it is. UNPLUG: SIGKILL at once, without logs or a sync. A VM
        running for a reason (VM.reason) is refused, its reason said, unless force."""
        if how not in (POWER_OFF, UNPLUG):
            raise ValueError(f"how: {how!r}")
        vm = self.vm(name)
        pid = vm.pid()
        unit = vm.unit_name()
        say = out or (lambda line: print(line, file=sys.stderr, flush=True))
        reason = vm.reason()
        if reason and not force:
            raise Error(
                f"VM {name} runs for a reason: {vmunits.reason_line(reason)}; stop it anyway with --force, "
                f"or clear the reason first (beanjar status unset {name} reason)"
            )
        if reason:
            say(f"{name}: stopping it although it runs for a reason: {vmunits.reason_line(reason)}")
        # stopping: what follows, a guest that powers itself off included, is this stop's
        try:
            with open(vm.stopping_file, "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
        try:
            env = None
            sync_stop = how == POWER_OFF  # the launcher's stop: logs, sync, SIGTERM, SIGKILL
            if how == POWER_OFF:
                why = vm.power_off_refusal()
                if why and not fallback:
                    raise Error(f"VM {name} cannot take the power-off signal: {why}")
                if why:
                    say(f"{name}: no power-off signal ({why}); syncing its disks and stopping it")
                else:
                    argv = ["stop", "--dir", vm.dir, "--power-off", f"{poweroff.POWER_OFF_TIMEOUT}"]
                    rc = hostprocs.run_streamed([*self.launcher(), *argv], out, cwd=self.repo)
                    sync_stop = bool(vm.pid() or vm.unit_up())
                    if sync_stop:
                        late = (
                            f"VM {name}: webOS refused the power-off signal"
                            if rc == STOP_REFUSED
                            else f"VM {name} has not powered off {poweroff.POWER_OFF_TIMEOUT} s after the signal"
                        )
                        if not fallback:
                            raise Error(f"{late}; it still runs")
                        say(f"{late}; syncing its disks and stopping it")
                        env = dict(os.environ, SAVE_LOGS="0")  # saved before the signal
            if how == UNPLUG and unit and vm.unit_up():
                # Everything in the unit at once; systemd then runs ExecStopPost and removes the
                # unit's runtime directory (mode=mitm's sockets).
                vmunits.systemctl("kill", "--signal=SIGKILL", f"{unit}.service")
                for _ in range(50):
                    if not vm.unit_up():
                        break
                    time.sleep(0.1)
                env = dict(os.environ, SAVE_LOGS="0")
            elif how == UNPLUG and pid:
                try:
                    pgid = os.getpgid(pid)
                except ProcessLookupError:
                    pgid = None
                # mode=mitm's wrapper removes its socket directory on exit, which SIGKILL skips.
                netns_dir = hostprocs.proc_env(pid).get("NETNS_RUNDIR")
                if pgid and pgid > 1 and pgid != os.getpgid(0):
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
                for _ in range(50):
                    if not proc_alive(pid, "qemu-system"):
                        break
                    time.sleep(0.1)
                if netns_dir and os.path.basename(netns_dir).startswith("webos-netns-"):
                    shutil.rmtree(netns_dir, ignore_errors=True)
                env = dict(os.environ, SAVE_LOGS="0")
            if how == UNPLUG or sync_stop:
                # The launcher's stop: logs saved and the guest synced (not after an unplug), QEMU,
                # the console bridge and the private bus ended, pidfiles removed.
                rc = hostprocs.run_streamed(
                    [*self.launcher(), "stop", "--dir", vm.dir], out, cwd=self.repo, env=env
                )
        finally:
            try:
                os.unlink(vm.stopping_file)
            except OSError:
                pass
        # The viewer and beanjar-vnc leave by themselves when the display goes; one that does not
        # is closed.
        for pid_of, comm, close in (
            (vm.viewer_pid, "beanjar-viewer", self.unview),
            (vm.vnc_pid, "beanjar-vnc", self.unvnc),
        ):
            vpid = pid_of()
            if vpid:
                for _ in range(30):
                    if not proc_alive(vpid, comm):
                        break
                    time.sleep(0.2)
                if pid_of():
                    close(name)
        self._vnc_cleanup(vm)  # the files of a beanjar-vnc that left by itself
        if vm.pid() or vm.unit_up():
            raise Error(f"VM {name} is still running (launcher exit {rc})")
        vm.release()
        return vm

    def wipe(self, name, parts=None, out=None):
        vm = self.vm(name)
        if vm.alive():
            raise Error(f"VM {name} is not off; stop it before wiping")
        parts = list(parts or [])
        from . import qemu

        known = targets.state_parts(vm.target())
        bad = [p for p in parts if p not in known]
        if bad:
            raise Error(f"unknown partition {', '.join(bad)} (one of {', '.join(known)})")
        try:
            qemu.reset_state(vm.state_dir, parts, out=out or print)
        except (qemu.Error, OSError) as e:
            raise Error(str(e)) from None
        return vm

    def renew_identity(self, name, out=None):
        """A new identity for a VM that is off: its state disks wiped (all of them, as wipe does,
        so no service keeps the old NDUID), then a new MAC (the library's prefix) and serial in
        vm.toml, every key pair of identity/ and its VNC password made anew (beanjar/keys.py), and its
        DVR key (sestore.toml) dropped for the next start to make anew. The model key, the user's
        authorized_keys and the library's CA are kept. Returns (vm, mac, serial)."""
        out = out or print
        vm = self.vm(name)
        if vm.alive():
            raise Error(f"VM {name} is not off; stop it before giving it a new identity")
        self.wipe(name, out=out)
        cfg = vm.config
        macs, serials = vmidentity.identities(self.vms_dir, name)
        mac, serial = vmidentity.next_identity(self.mac_prefix, macs, serials, cfg)
        cfg.update(mac=mac, serial=serial)
        vm.save_config(cfg)
        out(f"mac {mac}, serial {serial}")
        prepare_board_data(self, vm.dir, out)  # the new chip has the new serial
        from . import keys, sestore

        try:
            made = vmidentity.renew_keys(
                vmidentity.make_identity_dir(vm.dir), name, vm.service_host()
            )
            keys.ensure(
                vm.identity_dir, address=vm.service_host()
            )  # an authorized_keys that was missing
            sestore.drop_dvr_key(vm.identity_dir)  # the next start makes the new set's own
        except (keys.Error, sestore.Error) as e:
            raise Error(str(e)) from None
        out(f"new keys in {vm.identity_dir}: {', '.join(made)}")
        pid = vm.vnc_pid()
        if pid:
            out(
                f"beanjar-vnc (pid {pid}) keeps the old password until it restarts: "
                f"beanjar vnc stop {name}; beanjar vnc start {name}"
            )
        return vm, mac, serial

    def set_vnc_password(self, name, password=None, out=None):
        """The VM's VNC password (identity/vnc.password) set to password, checked
        (keys.check_vnc_password), or with None made anew at random. A running beanjar-vnc keeps the
        one it started with; out(line), if given, is told so. Returns the VM."""
        from . import keys

        vm = self.vm(name)
        try:
            keys.set_vnc_password(vmidentity.make_identity_dir(vm.dir), password)
        except (keys.Error, OSError) as e:
            raise Error(str(e)) from None
        pid = vm.vnc_pid()
        if pid and out:
            out(
                f"beanjar-vnc (pid {pid}) keeps the old password until it restarts: "
                f"beanjar vnc stop {name}; beanjar vnc start {name}"
            )
        return vm

    VNC_PORT = 5900  # beanjar-vnc's, on the VM's address

    # The display's front ends: the GTK viewer (view) or beanjar-vnc (vnc), one at a time, since
    # both take QEMU's single-client chardevs (cursor, video, IR, media, capture).
    # Screenshots (docs/dev/architecture/viewer.md, "Screenshots from a script"): whoever owns the VM's
    # display takes the picture. The GTK viewer and beanjar-vnc own SCREENSHOT_NAME on the VM's bus
    # and answer its Screenshot method; with neither running, a headless viewer is run once.
    SCREENSHOT_SIZE = (1280, 720)
    THUMBNAIL_MAX = (640, 360)  # a screenshot no larger is a thumbnail, kept (VM.thumbnail_file)

    def screenshot(self, name, path, size=None, timeout=30.0, out=None):
        """The TV picture of running VM name (the guest's scanout, its video windows and LG's cursor
        in, no chrome) as a PNG file at path, size = (width, height), default SCREENSHOT_SIZE,
        1280x720: the viewer's picture area at its default size, which Save Screenshot gives; the
        TV picture is letterboxed into it. Returns (path, (width, height), who), who "viewer", "vnc"
        or "headless"."""
        vm = self.vm(name)
        width, height = size or self.SCREENSHOT_SIZE
        if not (64 <= width <= 8192 and 64 <= height <= 8192):
            raise Error(f"invalid screenshot size {width}x{height}: 64 to 8192 each way")
        if vm.display != "dbus":
            raise Error(f"VM {name} has display = {vm.display}; a screenshot needs dbus")
        if vm.pid() is None:
            raise Error(f"VM {name} is not running")
        path = os.path.abspath(path)
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            raise Error(f"{folder}: no such directory")
        with tempfile.TemporaryDirectory(prefix="beanjar-shot-", dir=folder) as tmp:
            ppm = os.path.join(tmp, "shot.ppm")
            asked = self._screenshot_ask(vm.dbus_sock, ppm, width, height, timeout)
            who = "viewer" if vm.viewer_pid() else "vnc" if vm.vnc_pid() else "front end"
            if not asked:
                for pid, label in ((vm.viewer_pid(), "viewer"), (vm.vnc_pid(), "beanjar-vnc")):
                    if pid:
                        raise Error(
                            f"the {label} of {name} (pid {pid}) does not own {SCREENSHOT_NAME}: "
                            "it predates screenshots (or another client owns the name); close it and open it again"
                        )
                if out:
                    out(f"no viewer or beanjar-vnc for {name}: running a headless viewer once")
                who = "headless"
                self._stop_capture_sink(vm)
                try:
                    self._screenshot_headless(vm, ppm, width, height, timeout)
                finally:
                    self._release_capture_sink(vm)
            with open(ppm, "rb") as f:
                png = framing.ppm_to_png(f.read(), self._display_size(vm))
            part = os.path.join(tmp, "shot.png")
            with open(part, "wb") as f:
                f.write(png)
            if width <= self.THUMBNAIL_MAX[0] and height <= self.THUMBNAIL_MAX[1]:
                kept = vm.thumbnail_file + ".part"
                shutil.copyfile(part, kept)
                os.replace(kept, vm.thumbnail_file)
            os.replace(part, path)
        return path, (width, height), who

    def last_thumbnail(self, name):
        """The last thumbnail of VM name (a screenshot no larger than THUMBNAIL_MAX), running or
        not, as PNG bytes."""
        vm = self.vm(name)
        try:
            with open(vm.thumbnail_file, "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise Error(f"VM {name} has no thumbnail yet") from None

    @staticmethod
    def _display_size(vm):
        """(width, height) of vm's display, recorded in its screenshots (PNG_SOURCE_KEY); None
        when QEMU does not say."""
        try:
            bus = dbus.Bus(vm.dbus_sock)
        except Error:
            return None
        try:
            return dbus.display_size(bus)
        except Error:
            return None
        finally:
            bus.close()

    @staticmethod
    def _screenshot_ask(bus, ppm, width, height, timeout):
        """Calls Screenshot on SCREENSHOT_NAME on the VM's bus (its dbus.sock path): True when the
        front end has written the PPM, False if the name has no owner."""
        return dbus.screenshot(bus, ppm, width, height, timeout)

    def _screenshot_headless(self, vm, ppm, width, height, timeout):
        argv = [
            self.viewer(),
            f"unix:path={vm.dbus_sock}",
            "--headless",
            "--no-audio",
            "--size",
            f"{width}x{height}",
            "--screenshot",
            ppm,
            "--screenshot-after",
            str(framing.SCREENSHOT_SETTLE_MS),
            "--seconds",
            f"{timeout:g}",
        ]
        if not os.access(argv[0], os.X_OK):
            raise Error(f"no viewer program at {argv[0]} (scripts/build.sh host)")
        env = dict(os.environ, SDL_VIDEODRIVER="offscreen")
        try:
            r = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self.repo,
                timeout=timeout + 15,
            )
        except subprocess.TimeoutExpired:
            raise Error("the headless viewer did not finish") from None
        if r.returncode or not os.path.isfile(ppm):
            tail = r.stderr.decode(errors="replace").strip().splitlines()[-1:]
            raise Error(
                f"no picture within {timeout:g} s (the guest has not drawn yet?)"
                + (f": {tail[0]}" if tail else "")
            )

    def view(self, name, args=()):
        vm = self._frontend_check(name, "the viewer")
        return self._frontend_open(
            vm, self.viewer(), "viewer", vm.viewer_pidfile, [f"unix:path={vm.dbus_sock}", *args]
        )

    def vnc(self, name, args=(), listen=None, port=None, wait=20.0):
        """beanjar-vnc (docs/dev/architecture/vnc.md) on listen (default the VM's own address, 127.0.1.N)
        at port (VNC_PORT; 0: any free port, which VM.vnc_info and run/vnc.json report), always behind the VM's own password (RSA-AES): the owner-only
        VM.vnc_password_file in its identity, made random the first time and kept
        (set_vnc_password), which beanjar-vnc reads (--password-file). With link lan and mode mitm it is also on
        the VM's LAN address, port 5900: beanjar-vnc listens on the socket dir's netns.VNC_SOCK too,
        which the LAN namespace's relay serves there; listen is refused there (the VM's addresses
        are its own) and port moves only this host's listener (a desktop VNC may hold *:5900). A
        port another listener holds is an Error naming it. Waits until it listens; returns
        (pid, VM.vnc_info(), or None if it is not listening yet). It also serves RFB without
        authentication on VM.vnc_local_sock, an owner-only (0600) unix socket, for this host's
        own clients (vnc_pipe, the web viewer): the file's permissions are the access control. Its
        mixed audio is on VM.audio_local_sock, owner-only too (audio_pipe), and its picture as
        H.264 on VM.video_local_sock (video_pipe), unless beanjar-vnc is built without it."""
        from . import netns

        vm = self._frontend_check(name, "beanjar-vnc")
        env = hostprocs.proc_env(vm.pid())
        lan = (env.get("NET_LINK") or vm.config.get("link")) == "lan"
        # link lan with mode direct has no LAN namespace relay to serve it there (the guest alone
        # is on the LAN): this host's listener only, as with nat
        lan = lan and not vmsettings.lan_direct("lan", env.get("NET_MODE") or vm.config.get("mode"))
        if lan and listen:
            raise Error(
                f"VM {name} has link lan: VNC is on its own addresses (the LAN one and "
                f"{vm.address()}); --listen is for link nat"
            )
        address = listen or vm.address()
        if not address:
            raise Error(f"VM {name} has no address (slot)")
        port = self.VNC_PORT if port is None else port
        if not 0 <= port <= 65535:
            raise Error(f"invalid VNC port: --port {port}")
        if port == 0:
            port = hostprocs.free_port(address)
        conflict = hostprocs.port_conflict(address, port)
        if conflict:
            raise Error(f"{conflict}; choose another with beanjar vnc start {name} --port N")
        argv = [f"unix:path={vm.dbus_sock}", "--listen", address, "--port", str(port)]
        if lan:
            sockdir = env.get("NETNS_RUNDIR")
            if not sockdir:
                raise Error(f"VM {name} has link lan but no socket dir (NETNS_RUNDIR): restart it")
            argv += ["--unix", os.path.join(sockdir, netns.VNC_SOCK)]
        argv += ["--local", vm.vnc_local_sock, "--audio-local", vm.audio_local_sock]
        if self.vnc_has_video():
            argv += ["--video-local", vm.video_local_sock]
        argv += ["--password-file", vm.ensure_vnc_password()]
        with open(vm.vnc_file, "w") as f:
            json.dump({"listen": address, "port": port, "lan": lan}, f)
        log = os.path.join(vm.logs_dir, "vnc.log")
        start = os.path.getsize(log) if os.path.exists(log) else 0
        # on big.LITTLE hosts beanjar-vnc and QEMU's main thread on the big cores (cpus.py)
        cpus.thread_on_big(vm.pid())
        pid = self._frontend_open(
            vm,
            self.vnc_server(),
            "vnc",
            vm.vnc_pidfile,
            [*argv, "--name", name, *args],
            on_big=True,
        )
        # its "listening" lines, then "ready"; or its error, when it exits first
        end = time.monotonic() + wait
        while True:
            with open(log, errors="replace") as f:
                f.seek(start)
                text = f.read()
            if "beanjar-vnc: ready" in text:
                return pid, vm.vnc_info()
            # by its exit status, not its name: through taskset (cpus.on_big) the child is taskset
            # until it execs beanjar-vnc, and mid-exec its cmdline reads empty
            if hostprocs.child_exited(pid):
                self._vnc_cleanup(vm)
                said = [
                    line.split(": ", 1)[1]
                    for line in text.splitlines()
                    if line.startswith("beanjar-vnc: ")
                ]
                raise Error(f"beanjar-vnc did not start: {said[-1] if said else 'see ' + log}")
            if time.monotonic() > end:
                return pid, None
            time.sleep(0.1)

    @staticmethod
    def _vnc_cleanup(vm):
        for path in (
            vm.vnc_pidfile,
            vm.vnc_file,
            vm.vnc_local_sock,
            vm.audio_local_sock,
            vm.video_local_sock,
        ):
            try:
                os.unlink(path)
            except OSError:
                pass

    def _frontend_check(self, name, what):
        vm = self.vm(name)
        if vm.display != "dbus":
            raise Error(f"VM {name} has display = {vm.display}; {what} needs dbus")
        if vm.pid() is None:
            raise Error(f"VM {name} is not running")
        vpid = vm.viewer_pid()
        if vpid:
            raise Error(f"a viewer is already open for {name} (pid {vpid})")
        vpid = vm.vnc_pid()
        if vpid:
            raise Error(f"beanjar-vnc is already serving {name} (pid {vpid})")
        self._stop_capture_sink(vm)
        return vm

    @staticmethod
    def _stop_capture_sink(vm, wait=6.0):
        """Stops the capture relay's sink before a front end takes the display: it holds the
        display's one-client sockets (capture_relay.py). Until _release_capture_sink, or this
        process's end, the relay starts no other (the hold names this pid); then only when a
        capture comes with no front end."""
        try:
            with open(vm.capture_sink_hold, "w") as f:
                f.write(f"{os.getpid()}\n")
        except OSError:
            pass
        pid = vm.capture_sink_pid()
        if not pid:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        end = time.monotonic() + wait
        while time.monotonic() < end and proc_alive(pid, "viewer-headless"):
            time.sleep(0.05)

    @staticmethod
    def _release_capture_sink(vm):
        """The front end has its pidfile (or is done): the relay's own check takes over."""
        try:
            os.unlink(vm.capture_sink_hold)
        except OSError:
            pass

    def _frontend_open(self, vm, program, kind, pidfile, argv, env=None, on_big=False):
        if not os.access(program, os.X_OK):
            raise Error(f"no {kind} program at {program} (scripts/build.sh host)")
        os.makedirs(vm.logs_dir, exist_ok=True)
        cmd = [program, *argv]
        with open(os.path.join(vm.logs_dir, f"{kind}.log"), "ab") as log:
            p = subprocess.Popen(
                cpus.on_big(cmd) if on_big else cmd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=self.repo,
                start_new_session=True,
                env=env,
            )
        with open(pidfile, "w") as f:
            f.write(f"{p.pid}\n")
        self._release_capture_sink(vm)
        return p.pid

    def unview(self, name, wait=6.0):
        vm = self.vm(name)
        return self._frontend_close(vm.viewer_pid(), "beanjar-viewer", vm.viewer_pidfile, wait)

    def unvnc(self, name, wait=6.0):
        vm = self.vm(name)
        closed = self._frontend_close(vm.vnc_pid(), "beanjar-vnc", vm.vnc_pidfile, wait)
        self._vnc_cleanup(vm)
        return closed

    @staticmethod
    def _frontend_close(pid, comm, pidfile, wait):
        if not pid:
            return False
        os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            hostprocs.reap(pid)
            if not proc_alive(pid, comm):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.2)
            hostprocs.reap(pid)
        try:
            os.unlink(pidfile)
        except OSError:
            pass
        return True

    def delete(self, name):
        vm = self.vm(name)
        if vm.alive():
            raise Error(f"VM {name} is not off; stop it first")
        if vm.viewer_pid():
            self.unview(name)
        if vm.vnc_pid():
            self.unvnc(name)
        shutil.rmtree(vm.dir)

    def rename(self, name, new):
        """The VM name, off and with no viewer, beanjar-vnc or unit up, renamed new in place: the
        same TV under another name. Everything it is stays in its folder and moves with it (vm.toml
        with its MAC, serial, slot and settings; identity/; state/; firmware/; logs/), and nothing
        outside the folder records the name; what is derived from it follows at the next start (the
        unit's name, link lan's DHCP hostname). Returns the VM under its new name.

        Crash safety: the one rename of vms/NAME to vms/NEW is the commit. Before it, under the VM's
        start lock (no start meanwhile) and the slot domain's lock (no create or clone of NEW
        meanwhile), what may change first is changed so that it is right for either name: run/unit,
        the unit it last ran as, is removed (stale while it is off), and the keys' public comments
        are made to name NEW (keys.relabel; cosmetic, and put back if the rename fails). Interrupted
        before the rename, it is still NAME, whole; after it, NEW, whole; a start lock left by a
        killed run names a dead process and is removed as stale by the next command."""
        from . import keys, newvm

        vm = self.vm(name)
        if os.path.basename(self.root).startswith("development-"):
            raise Error(
                f"{name} is a development VM, named after its checkout and INSTANCE "
                "(host/scripts/vm-launcher): it cannot be renamed"
            )
        if new == name:
            raise Error(f"VM {name} is called {new} already", "name")
        newvm.check_name(self, new)
        st = vm.state()[0]
        if st != OFF:
            raise Error(f"VM {name} is {st.lower()}; stop it before renaming it")
        self._rename_check(vm, name)
        vm.acquire()  # no start while it moves (it moves with the folder)
        dest = VM(self, new)
        done = False
        try:
            if vm.pid() is not None:
                raise Error(f"VM {name} is running; stop it before renaming it")
            self._rename_check(vm, name)
            for unit in dict.fromkeys(
                u
                for u in (
                    vm.unit_name(),
                    vmunits.vm_unit_name(vm.dir),
                    vmunits.vm_unit_name(dest.dir),
                )
                if u
            ):
                active = vmunits.unit_show(unit).get("ActiveState")
                if active in vmunits.UNIT_UP:
                    raise Error(
                        f"unit {unit} is still {active}: stop it before renaming {name} "
                        f"(systemctl --user stop {unit})"
                    )
            with vmidentity.slot_lock(self.root):
                if os.path.lexists(dest.dir):
                    raise Error(f"VM {new} exists", "name")
                try:
                    os.unlink(vm.unit_file)
                except FileNotFoundError:
                    pass
                relabelled = os.path.isdir(vm.identity_dir)
                try:
                    if relabelled:
                        keys.relabel(vm.identity_dir, new)
                    os.rename(vm.dir, dest.dir)
                except BaseException:
                    if relabelled and os.path.isdir(vm.identity_dir):
                        try:
                            keys.relabel(vm.identity_dir, name)
                        except (keys.Error, OSError):
                            pass
                    raise
                done = True
        except keys.Error as e:
            raise Error(str(e)) from None
        except OSError as e:
            raise Error(f"cannot rename {name} to {new}: {e.strerror or e}") from None
        finally:
            (dest if done else vm).release()
        return dest

    @staticmethod
    def _rename_check(vm, name):
        """Rename's refusal of a VM with a front end open (the viewer, beanjar-vnc) or being stopped."""
        pid = vm.viewer_pid()
        if pid:
            raise Error(f"a viewer is open for {name} (pid {pid}): beanjar viewer close {name}")
        pid = vm.vnc_pid()
        if pid:
            raise Error(f"beanjar-vnc is serving {name} (pid {pid}): beanjar vnc stop {name}")
        pid = vm._stopper()
        if pid:
            raise Error(f"VM {name} is being stopped (pid {pid})")


def main_error(e):
    print(f"beanjar: {e}", file=sys.stderr)
    return 1
