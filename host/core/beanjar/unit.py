# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The processes of one VM - the launcher and QEMU, the mode=mitm helpers (unshare, slirp4netns,
the port relays, mitmweb, the DHCP server) and the private D-Bus of DISPLAY_BACKEND=dbus - as one transient
systemd user service, so that they start, stop and die together (launch.py drives it):

  available()          a user systemd can run the VM (VM_SYSTEMD=0: never)
  start(unit, env, words)   start UNIT.service running `python3 -m beanjar.unit run [words]`
  python3 -m beanjar.unit run [words]   the service's main process (ExecStart); with no user
                       systemd, launch.py runs it in a session of its own (setsid) instead
  python3 -m beanjar.unit sync          ExecStop: flush the guest's page cache (the state disks)
  python3 -m beanjar.unit post          ExecStopPost: remove what the processes leave behind

The unit is a service, not a scope: systemd forks it, so it belongs to no shell or terminal; it
ends when its main process does, so when QEMU exits (a guest power-off, a crash) the whole control
group goes with it (KillMode=control-group), private bus included; ExecStop and ExecStopPost run on
every stop; and systemd records MainPID, the exit status and the result. A guest restart also ends
QEMU (-action reboot=shutdown), but then `run` starts the launcher again in the same unit, with the
same bus and RuntimeDirectory: a cold start, as a TV's (lifecycle.py). With STATE=none the run has
one temporary state that every start of it uses, made empty at the run's start and removed at its
end (lifecycle.fresh_state), so a guest restart keeps what the guest wrote. Its resources: CPUWeight
below the host's own processes, and under KVM MemoryHigh at the VM's RAM and the start's headroom,
what the library's admission counts as the VM's (none under TCG: resources).

Its slice: core.unit_slice, beanjar-<library tag>.slice for beanjar-vm-<tag>-<name>, in beanjar.slice,
so `systemctl --user status beanjar.slice` shows every VM of every library and `beanjar list --all`
finds them (core.vm_units). systemd makes a slice when a unit first uses it and keeps it, empty
and harmless, until it is stopped or the user manager ends: one per library.

Properties: of the caller's environment only the launcher's knobs pass through (env_for); the working directory
is the checkout; the description is "webOS VM <RUN_DIR>", which launch.py checks before it touches
a unit of that name; the output of the launcher and QEMU goes to the journal (journalctl --user -u
UNIT) and to LOG_DIR/qemu.out. `systemctl --user stop UNIT` runs ExecStop (the guest sync, at most
~16 s), then SIGTERMs everything in the unit and SIGKILLs what is left 20 s later
(TimeoutStopSec). RuntimeDirectory (/run/user/<uid>/UNIT) holds the mode=mitm port-bridge sockets
(netns.py, NETNS_SOCK_DIR: its netns/), the reason (reason.json) and what lifecycle.py keeps across a
guest restart; systemd removes it on every stop, SIGKILL included.

Env: RUN_DIR, LOG_DIR (absolute), RUN (the launcher instead of `python3 -m beanjar.launch run`;
the tests' fake QEMU), DISPLAY_BACKEND, SOCK/LOG (the serial console, for sync), QEMU_PIDFILE
(default RUN_DIR/qemu.pid), VM_SYSTEMD (0: never a unit).
"""

import functools
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

from . import console, lifecycle, paths
from .base import read_pid
from .vmunits import linger, systemctl, unit_slice

# seconds a restart waits for the last QEMU's helpers to end (unit.reap_orphans)
ORPHAN_WAIT = 10
UP = ("active", "activating", "deactivating", "reloading")
# The unit's environment (env_for): only what the launcher and the processes it runs in the unit read,
# never the caller's whole environment (a password exported in the starting shell went into
# systemd-run's argv and the unit). Each variable for its reader:
ENV_NAMES = frozenset(
    {
        # the processes themselves: Python (this package on PYTHONPATH), the helpers, QEMU's libraries
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "PYTHONPATH",
        "LD_LIBRARY_PATH",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        # this module (run, sync, post) and the development VM's paths (launch.py, development.py)
        "RUN",
        "RUN_DIR",
        "LOG_DIR",
        "SAVED_LOG_DIR",
        "SOCK",
        "LOG",
        "QEMU_PIDFILE",
        "VM_SYSTEMD",
        "UNIT",
        "INSTANCE",
        "BEANJAR_LIBRARY",
        # qemu.py's plan (its docstring), paths.py's QEMU
        "FW",
        "FW_CACHE",
        "SLOT",
        "ADDRESS_PREFIX",
        "VM_ADDRESS",
        "LAYERS",
        "SSH_PORT",
        "LISTEN_ADDRESS",
        "FORWARD",
        "FREE_PAGE_REPORTING",
        "USERLAND",
        "GUEST_KERNEL",
        "QEMU_ARM",
        "QEMU_AARCH64",
        "QEMU_X86_64",
        "GPU_DEVICE",
        "VHOST_USER_GPU",
        "DEBUG_MODE",
        "MEM",
        "SMP",
        "NET",
        "NET_LINK",
        "NET_MODE",
        "NET_ADDRESS",
        "NET_LAN_ACCESS",
        "NET_LG_SERVICES",
        "NET_DUMP",
        "MITM_FLOWS",
        "MITM_FLOW_BYTES",
        "ROOTFS",
        "UNCOMPRESSED_PAKS",
        "STATE",
        "MAC",
        "SERIAL",
        "BROADCAST",
        "IDENTITY_DIR",
        "QEMU",
        "QEMU_EXTRA",
        "ACCEL",
        "DISPLAY_BACKEND",
        "GPU_RES",
        "RENDERNODE",
        "AUDIO",
        "CONSOLE_SOCK",
        "CONSOLE_LOG",
        "WEBOS_LOG",
        "QMP_SOCK",
        "VIEWER_QMP_SOCK",
        "CURSOR_SOCK",
        "VIDEO_SOCK",
        "IR_SOCK",
        "MEDIA_SOCK",
        "CAPTURE_SOCK",
        "DBUS_ADDR",
        "PMLOG",
        "PMLOG_DEBUG",
        "KERNEL_ARGS",
        "TCG_CPU",
        "TCG_THREADS",
        "TCG_TB_SIZE",
        "MEDIAHUB",
        "MEDIA_HWDEC",
        # netns.py and the mode=mitm helpers (netns.py's docstring, mitm_addon.py, tlsca.py)
        "NETNS_FWD",
        "NETNS_TAP",
        "NETNS_LOG_DIR",
        "NETNS_SOCK_DIR",
        "NETNS_MITMWEB",
        "NET_LAN_PARENT",
        "NET_LAN_MAC",
        "NET_LAN_HOSTNAME",
        "NET_LAN_HELPER",
        "NETSTUB_CA",
        "NETSTUB_LOG",
        "NETSTUB_STREAM_LARGE",
        "NETSTUB_WEB_HOST",
        "NETSTUB_WEB_PORT",
        "NETSTUB_WEB_PASSWORD_FILE",
        "BEANJAR_LAN_HELPER",
        "BEANJAR_RELAY",
    }
)
# Families the host's libraries read: locale, Mesa and its GL/EGL (GALLIUM_DRIVER, MESA_D3D12_DEFAULT_ADAPTER_NAME, LIBGL_ALWAYS_SOFTWARE), VA-API, Vulkan and CUDA (the media
# helper's FFmpeg hardware decoders).
ENV_PREFIXES = ("LC_", "MESA_", "GALLIUM_", "LIBGL_", "LIBVA_", "VK_", "CUDA_")


def env_for(env, keep=()):
    """The part of env the unit gets: ENV_NAMES, ENV_PREFIXES and the names in keep (the caller's
    own: a VM's vm.toml keys, launch.Bg). Anything else - a password, a token - stays behind."""
    keep = set(keep)
    return {
        k: v
        for k, v in env.items()
        if (k in ENV_NAMES or k in keep or k.startswith(ENV_PREFIXES))
        and k.replace("_", "a").isalnum()
    }


def comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except (OSError, TypeError):
        return ""


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Paths:
    """The runtime files of a VM, from its environment (RUN_DIR, LOG_DIR, QEMU_PIDFILE, SOCK, LOG)."""

    def __init__(self, env, tree=paths.TREE):
        self.run_dir = env.get("RUN_DIR") or tree.build()
        self.log_dir = env.get("LOG_DIR") or tree.build()
        self.pidfile = env.get("QEMU_PIDFILE") or os.path.join(self.run_dir, "qemu.pid")
        self.sock = env.get("SOCK") or os.path.join(self.run_dir, "console.sock")
        self.log = env.get("LOG") or os.path.join(self.log_dir, "console.log")

    def qemu_pid(self):
        """The pid in the pidfile, if it is a live QEMU."""
        pid = read_pid(self.pidfile)
        return pid if alive(pid) and comm(pid).startswith("qemu-system") else None


def available(env=os.environ):
    if env.get("VM_SYSTEMD", "1") == "0" or not shutil.which("systemd-run"):
        return False
    # "degraded" (some other unit failed) still runs units; "offline"/no bus does not.
    rc, out = systemctl("is-system-running")
    return rc is not None and out.strip() in ("running", "degraded", "starting")


def _python_env(env, tree):
    return dict(
        env, PYTHONPATH=os.pathsep.join(x for x in (tree.pkg_parent, env.get("PYTHONPATH")) if x)
    )


# The unit's CPU share against the host's own processes (default 100): under contention a desktop,
# the viewer and the manager come first; VMs share among themselves alike.
CPU_WEIGHT = 50


def resources(mem, env=None):
    """The unit's resource properties: CPUWeight, and under KVM with the VM's RAM known (mem, MiB)
    MemoryHigh at that and the start's headroom (core.reservation_mib, what the admission counts for
    it). Under TCG (env: the launch's environment, qemu.vm_accel) no MemoryHigh: QEMU's TB cache and GL
    buffers come on top of the RAM, and a 2 GiB VM at 3.27 GB anon stalled in D state throttled at
    it; the admission counts the TB cache instead. The
    user manager delegates cpu and memory to user units on Debian 13 (DelegateControllers of
    user@.service: cpu memory pids). MemoryHigh, not MemoryMax: above it the kernel reclaims and
    throttles the unit rather than OOM-killing QEMU, and a TCG VM with gl comes close (W18R, 1536
    MiB: QEMU's RSS 2.1 GiB, the unit's peak 2.4 of 2.5 GiB at boot). A qemu-user VM's swap disk
    in host RAM (hostswap.py, qemu.vm_host_swap) is on top: its memfd's pages are shmem, charged to
    the cgroup of QEMU, which writes them, and reclaimable only to the host's swap, so a MemoryHigh
    without them would throttle QEMU as the guest swaps. The admission does not count it: it is
    not taken up front, only as the guest swaps (capacity.claim_mib leaves it out too)."""
    from . import qemu
    from .capacity import reservation_mib

    env = os.environ if env is None else env
    props = [f"CPUWeight={CPU_WEIGHT}"]
    if mem and qemu.vm_accel(env) == "kvm":
        props.append(f"MemoryHigh={reservation_mib(mem, env) + qemu.vm_host_swap(env)}M")
    return props


def start(unit, env, words=(), tree=paths.TREE, mem=None, keep=()):
    """Start UNIT.service with env_for(env, keep) as its environment (as a setsid child would have
    it); mem: the guest's RAM in MiB (qemu.vm_guest_ram), which bounds the unit's memory
    (resources). The user lingers first (core.linger), so the VM outlives the session that
    started it."""
    linger()
    env = _python_env(env_for(env, keep), tree)
    me = f"{shlex.quote(sys.executable)} -m beanjar.unit"
    cmd = [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        f"--slice={unit_slice(unit)}",
        f"--description=webOS VM {env['RUN_DIR']}",
        f"--working-directory={tree.top}",
        "--property=Type=exec",
        "--property=KillMode=control-group",
        "--property=TimeoutStopSec=20",
        f"--property=SyslogIdentifier={unit}",
        f"--property=RuntimeDirectory={unit}",
        "--property=RuntimeDirectoryMode=0700",
        f"--property=ExecStop={me} sync",
        f"--property=ExecStopPost={me} post",
        *(f"--property={p}" for p in resources(mem, env)),
    ]
    cmd += [f"--setenv={k}={v}" for k, v in env.items()]
    return (
        subprocess.run(
            [*cmd, "--", sys.executable, "-m", "beanjar.unit", "run", *words],
            stdin=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def spawn(env, words=(), tree=paths.TREE, keep=()):
    """The fallback without a user systemd: `run` in a session of its own, with the unit's
    environment (env_for); its pid."""
    p = subprocess.Popen(
        [sys.executable, "-m", "beanjar.unit", "run", *words],
        env=_python_env(env_for(env, keep), tree),
        cwd=tree.top,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return p.pid


BUS_CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={sock}</listen>
  <allow_anonymous/>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def start_bus(run_dir):
    """The private D-Bus of DISPLAY_BACKEND=dbus, at RUN_DIR/dbus.sock: the dbus-daemon process.

    The bus must also offer ANONYMOUS auth: under mode=mitm QEMU runs inside a rootless user+net
    namespace where it is uid 0, so its D-Bus EXTERNAL credential (0) does not match the peer uid
    the daemon sees for it (our real uid), and EXTERNAL is rejected. With <allow_anonymous/> QEMU's
    GDBus client falls back to ANONYMOUS. The <auth> list is left unset so the daemon still offers
    the default mechanisms too: host-side GL clients run in the init namespace and use EXTERNAL (the
    viewer) or DBUS_COOKIE_SHA1 (tests/regress/virgl/dbus-grab.py, whose manual socket stream cannot pass
    SO_PEERCRED for EXTERNAL). A plain --session bus offers no ANONYMOUS; restricting <auth> to
    EXTERNAL+ANONYMOUS drops DBUS_COOKIE_SHA1 and breaks dbus-grab, so keep the defaults and only
    add anonymous."""
    sock = os.path.join(run_dir, "dbus.sock")
    try:
        os.unlink(sock)
    except FileNotFoundError:
        pass
    conf = os.path.join(run_dir, "dbus-session.conf")
    with open(conf, "w") as f:
        f.write(BUS_CONF.format(sock=sock))
    bus = subprocess.Popen(
        ["dbus-daemon", f"--config-file={conf}", "--nofork", "--nopidfile"],
        stdin=subprocess.DEVNULL,
    )
    with open(os.path.join(run_dir, "dbus-daemon.pid"), "w") as f:
        f.write(f"{bus.pid}\n")
    end = time.monotonic() + 5
    while not os.path.exists(sock) and time.monotonic() < end and bus.poll() is None:
        time.sleep(0.1)
    return bus


def wait_for_line(path, prefix, timeout):
    """A line of the file at path starts with prefix, within timeout seconds."""
    end = time.monotonic() + timeout
    while True:
        try:
            with open(path, "rb") as f:
                if any(line.startswith(prefix) for line in f):
                    return True
        except OSError:
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)


def is_socket(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def sync_guest(p, tree=paths.TREE):
    """Flush the guest's page cache to the state disks before QEMU is stopped. sync(2) covers every
    mounted filesystem, including webOS's, from the initramfs shell. ext4's journal keeps the disks
    consistent without it; this keeps the last ~30 s of writes as well."""
    if not p.qemu_pid() or not is_socket(p.sock):
        return
    console.send('sync && echo "GUEST""-SYNCED"', 0, p.sock, p.log, tree)
    if not wait_for_line(p.log, b"GUEST-SYNCED", 15):
        print("sync: no reply from the guest within 15 s", file=sys.stderr)


def kill_session(pid, sig=signal.SIGTERM):
    """Signal the process group pid leads (a setsid session), and pid itself."""
    if pid != os.getpgid(0):
        try:
            os.killpg(pid, sig)
        except OSError:
            pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def cleanup(p):
    """What the VM's processes leave: pidfiles, the bus socket, the console bridge."""
    if not p.qemu_pid():
        _rm(p.pidfile)
    for n in ("dbus-daemon.pid", "dbus.sock", "launcher.pid"):
        _rm(os.path.join(p.run_dir, n))
    # The console bridge (a session of its own) ends with the console socket, but make sure.
    bridge = read_pid(p.sock + ".in.pid")
    if bridge:
        kill_session(bridge)
        _rm(p.sock + ".in.pid")


def _rm(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def launcher_cmd(env, words):
    if env.get("RUN"):
        return [env["RUN"], *words]
    return [sys.executable, "-m", "beanjar.launch", "run", *words]


def keep_logs(env, p):
    """The console logs of the QEMU that ended, as *.prev.log: the next QEMU truncates them."""
    for path in (p.log, env.get("WEBOS_LOG") or os.path.join(p.log_dir, "webos.log")):
        if os.path.exists(path):
            os.replace(path, path.removesuffix(".log") + ".prev.log")


def set_subreaper():
    """Orphans of this process's children (the helpers the launcher leaves when it execs QEMU)
    become ours, so that a restart can wait for them (reap_orphans)."""
    import ctypes

    try:
        ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        pass


def children(keep=()):
    """This process's live children's pids, but keep."""
    me = os.getpid()
    try:
        with open(f"/proc/{me}/task/{me}/children") as f:
            pids = [int(x) for x in f.read().split()]
    except OSError:
        return []
    return [pid for pid in pids if pid not in keep]


def reap_orphans(keep=(), timeout=ORPHAN_WAIT):
    """Wait up to timeout seconds for the last QEMU's helpers (the media hub, the tuner, the GPU
    backend, the network helpers, all ending with it) to end, then SIGKILL what is left: an old
    helper on its way out would otherwise unlink the new one's sockets. The list of those killed."""
    end = time.monotonic() + timeout
    while True:
        left = []
        for pid in children(keep):  # the ended ones collected (not the bus: its Popen waits)
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    continue
            except ChildProcessError:
                continue
            left.append(pid)
        if not left or time.monotonic() >= end:
            break
        time.sleep(0.1)
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (OSError, ChildProcessError):
            pass
    return left


def run(words, tree=paths.TREE):
    """The unit's main process: the private bus, then the launcher, its output to LOG_DIR/qemu.out
    and to our stdout (the journal under systemd), its exit status ours. A guest restart ends QEMU
    (lifecycle.py): then the launcher runs again, in the same unit with the same bus and
    RuntimeDirectory, after the last QEMU's helpers have ended. A SIGTERM (a stop) is held
    until the launcher's output ends, so a stop still records QEMU's last words; then we end by that
    SIGTERM, which systemd counts as a clean stop (an exit status of 143 would read as a failure)."""
    env = dict(os.environ)
    p = Paths(env, tree)
    bus = start_bus(p.run_dir) if env.get("DISPLAY_BACKEND") == "dbus" else None
    # systemd removes its RuntimeDirectory at every stop, SIGKILL included, where netns.py's own
    # temporary directory would stay behind. A directory of its own in it: netns.py removes it
    # when QEMU ends, and a guest restart keeps the rest (reason.json, usb.json).
    if env.get("RUNTIME_DIRECTORY"):
        env["NETNS_SOCK_DIR"] = os.path.join(env["RUNTIME_DIRECTORY"], "netns")
    state = lifecycle.state_dir(p.run_dir, env.get("RUNTIME_DIRECTORY"))
    lifecycle.write_usb(state, [])  # a new start has nothing plugged (the setsid fallback's run/)
    lifecycle.mark_restarting(p.run_dir, False)
    lifecycle.set_restarts(p.run_dir, 0)
    # STATE=none: one temporary state for the whole run, kept across guest restarts (a set keeps
    # what it wrote when it reboots: firmware that reboots once on its first boot, on purpose,
    # comes up the second time) and removed when the run ends. A leftover of a run that ended
    # without its cleanup is not this run's: start from empty.
    fresh = lifecycle.fresh_state(env, tree.top)
    if fresh:
        lifecycle.discard_fresh_state(env, tree.top)
        os.makedirs(fresh)
        env["FRESH_STATE"] = fresh
    sock = os.path.join(env.get("RUNTIME_DIRECTORY") or p.run_dir, lifecycle.SOCK)
    set_subreaper()
    rc = 1
    stopped = []
    signal.signal(signal.SIGTERM, lambda *_: stopped.append(True))
    lock = threading.Lock()
    try:
        with open(os.path.join(p.log_dir, "qemu.out"), "wb") as out:

            def say(text):
                chunk = (text + "\n").encode()
                with lock:
                    out.write(chunk)
                    out.flush()
                    try:
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                    except OSError:
                        pass

            restarts = 0
            reset_at = None  # when the guest asked for the last restart (its SHUTDOWN)

            def on_reset():
                nonlocal reset_at
                reset_at = time.monotonic()
                lifecycle.mark_restarting(p.run_dir, True)

            def on_ready():
                lifecycle.mark_restarting(p.run_dir, False)
                if reset_at is not None:
                    # what a restart costs over a reset in place: the old QEMU's end, its
                    # helpers', the launcher's preparation and the new QEMU's start
                    say(
                        f"beanjar: the new QEMU runs {time.monotonic() - reset_at:.1f} s after "
                        "the guest's restart"
                    )

            while True:
                monitor = lifecycle.Monitor(
                    sock,
                    say,
                    devices=lambda: lifecycle.read_usb(state),
                    on_reset=on_reset,
                    on_ready=on_ready,
                ).start()
                env["LIFECYCLE_SOCK"] = sock
                proc = subprocess.Popen(
                    launcher_cmd(env, words),
                    env=env,
                    cwd=tree.top,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                for chunk in iter(functools.partial(proc.stdout.read1, 65536), b""):
                    with lock:
                        out.write(chunk)
                        out.flush()
                        try:
                            sys.stdout.buffer.write(chunk)
                            sys.stdout.buffer.flush()
                        except OSError:
                            pass
                proc.stdout.close()
                rc = proc.wait()
                rc = 128 - rc if rc < 0 else rc
                monitor.close()
                if monitor.disk_full is not None:
                    rc = rc or 1  # a failure: the VM could not go on
                    break
                if stopped or rc != 0 or monitor.reset is None:
                    break
                started = time.monotonic()
                lifecycle.mark_restarting(p.run_dir, True)
                killed = reap_orphans(keep=(bus.pid,) if bus else ())
                if stopped:
                    break
                keep_logs(env, p)
                restarts += 1
                lifecycle.set_restarts(p.run_dir, restarts)
                say(
                    f"beanjar: the guest restarted ({monitor.reset}): starting the VM again, cold "
                    f"(restart {restarts}; the last QEMU's helpers ended in "
                    f"{time.monotonic() - started:.1f} s"
                    + (f", {len(killed)} killed" if killed else "")
                    + ")"
                )
    finally:
        lifecycle.mark_restarting(p.run_dir, False)
        # QEMU has exited: the VM is over, so is its bus (under systemd the unit's end does this too).
        if bus is not None and bus.poll() is None:
            bus.terminate()
            try:
                bus.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bus.kill()
        cleanup(p)
        lifecycle.discard_fresh_state(env, tree.top)
    if stopped:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)
    return rc


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    if cmd == "run":
        return run(argv[1:])
    if cmd == "sync":
        sync_guest(Paths(os.environ))
        return 0
    if cmd == "post":
        cleanup(Paths(os.environ))
        # a run killed before its own cleanup (SIGKILL) leaves its temporary state
        lifecycle.discard_fresh_state(os.environ, paths.TREE.top)
        return 0
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
