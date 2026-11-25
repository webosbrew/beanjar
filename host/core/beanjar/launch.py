# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The host runtime's command line: boot, stop and drive webOS VMs (host/scripts/vm-launcher, or python3 -m
beanjar.launch with host/core on PYTHONPATH).

  host/scripts/vm-launcher run [words...]        boot in the foreground, serial console on stdio (Ctrl-A c: the
                                   QEMU monitor; Ctrl-A x quits); the environment is qemu.py's
  host/scripts/vm-launcher argv [words...]       print the command `run` would run, without running or building
                                   anything (a dry run)
  host/scripts/vm-launcher start [words...]      (re)boot the checkout VM in the background; waits for the
                                   initramfs debug shell
  host/scripts/vm-launcher stop                  stop it: the guest's logs saved, the guest synced, QEMU ended
  host/scripts/vm-launcher stop --power-off SECONDS   the logs saved, then webOS's own power-off (tvpowerd's forced powerOff); exit 4 if QEMU
                                   still runs SECONDS after the call, 5 if webOS refuses it (an explicit
                                   returnValue false, an error, no console): `beanjar stop` decides
                                   what then
  host/scripts/vm-launcher start|stop|status --dir VM-DIR [words...]    one VM of the manager's layout (below)
  host/scripts/vm-launcher console 'command' [WAIT]   type a line on the serial console, print its output (up to WAIT s)
  host/scripts/vm-launcher qmp [--dir VM-DIR] 'JSON'   send one QMP command to RUN_DIR/qmp.sock (QMP_SOCK), print
                                   the reply (exit 1 on an error reply), e.g. '{"execute":"query-status"}';
                                   a screendump's filename is written by QEMU, on the VM's host
  host/scripts/vm-launcher put [--dir VM-DIR] PATH...  give a running VM files of the checkout (share.PUT_ALLOWED:
                                   tests/, guest/scripts/, the guest build's test programs and layers) at
                                   /host/PATH (/mnt/host/PATH in webOS), until its next start
  host/scripts/vm-launcher put-out [--dir VM-DIR] SRC DEST   copy the host file SRC into the VM's out share
                                   at DEST (/host/out/DEST, /mnt/host/out/DEST in webOS), never
                                   through a link the guest made there (share.place)
  host/scripts/vm-launcher get-out [--dir VM-DIR] SRC [DEST]   copy the regular file SRC of the VM's out
                                   share to DEST (a file or directory; default -, standard output),
                                   never through a link the guest made there, never from a FIFO
                                   (share.take); exit 1 if it is missing or not a regular file
  host/scripts/vm-launcher rm-out [--dir VM-DIR] PATH...   remove files of the VM's out share,
                                   never through a link the guest made there (share.remove)
  host/scripts/vm-launcher netns-exec [--dir VM-DIR] COMMAND...   run COMMAND in the running VM's
                                   network namespace (link nat: netns.enter_argv), the guest's
                                   neighbour there; the guest reaches what it serves at 10.0.2.254
                                   (netns.DHCP_SERVER), the host never (scripts/regress --tv's server)
  host/scripts/vm-launcher logs [--dir VM-DIR]   save the guest's finished logs (check-m1.sh's) to SAVED_LOG_DIR now
  host/scripts/vm-launcher prepare [--dir VM-DIR] [--force]   work out the VM's board data from its
                                   firmware (prepare.py: partition map, new EEPROM chip, local key,
                                   the modules' parameters) if missing or stale; start does it too
  host/scripts/vm-launcher reset-state [STATE [partition...]]   wipe a state directory (qemu.reset_state)
  host/scripts/vm-launcher firmware-cache [PAK...]    the checkout VM's ROOTFS=uncompressed images (fwcache.py)
  host/scripts/vm-launcher firmware-facts TARGET [--build DIR] [--check]   write (or check) the target's
                                   guest/targets/facts/TARGET.txt from its firmware (fwfacts.py)
  host/scripts/vm-launcher config FW|FW_NAME     print the firmware directory or name (paths.Tree.firmware)
  host/scripts/vm-launcher config FW --target CHIP-RELEASE   the target's firmware instead, not FW's (paths.Tree.target_firmware;
                                   what firmware-facts reads)
  host/scripts/vm-launcher config RENDERNODE     print the render node a TCG VM's GPU would use, or surfaceless
  host/scripts/vm-launcher config TARGET           print FW's (chip, release) as <chip>-<release>, e.g. h13-afro (paths.firmware_target)
  host/scripts/vm-launcher config PAKS           print the pak files FW's target needs, e.g. rootfs.pak (targets.pak_files)
  host/scripts/vm-launcher config TARGETS          print the targets this tree boots, one per line (targets.all_targets)
  host/scripts/vm-launcher config ADDRESS [--dir VM-DIR]   print the VM's host address, <addresses>.<its slot>, where
                                   its ports are (the development VM's, INSTANCE's, unless --dir)
                                   (qemu.tcg_rendernode; the host build's `qemu` option asks it)

The kernel cmdline words go to the guest. ACCEL (tcg or kvm) picks the machine; the default is kvm
on an aarch64 host whose /dev/kvm the user can open (the Pi) and tcg elsewhere (qemu.py).

The guest sees only its runtime tree, RUN_DIR/share (share.py): the per-VM layer, a read-only
host/ that each launch assembles (the layers, binds.conf, guest/scripts/{save-logs,check-m1,time-boot}.sh
and what `put` adds) and a read-write out/ (/host/out; /mnt/host/out in webOS) for what it writes.

start/stop (the checkout VM): before stopping a running VM, the guest's logs are saved to
build/logs/<UTC time>-<tag>/ (guest/scripts/save-logs.sh, in the guest: /var/log with PmLog's messages,
dmesg, boot status, jobs; written to out/logs/<name>, finished with .done, then copied from there,
its regular files and directories only, and removed: share.save), with the host-side console logs
and the mode=mitm proxy log; then the guest's filesystems are synced (the state disks) and QEMU is
stopped. Every other finished log directory in out/logs (a check-m1.sh run's) is saved to
SAVED_LOG_DIR too, at each stop and start. The VM runs as the transient
systemd user service UNIT (unit.py): QEMU and every helper in one control group that ends when QEMU
does, a guest power-off included, and that `stop` ends as a whole.
  systemctl --user status beanjar.slice      every VM's processes, by library (beanjar-<tag>.slice)
  journalctl --user -u beanjar-vm-<tag>-<name>   one VM's output (<tag>: its library's, vmunits.library_tag)
Without a usable user systemd (or with VM_SYSTEMD=0) the same processes run in a session of their own
(RUN_DIR/launcher.pid), and `stop` kills that session. Which of the two is the runner's choice
(runner.py: BEANJAR_RUNNER, or the machine's runner setting, may name one).
Env: SAVE_LOGS  0 to skip saving the logs on stop; LOG_TAG names them (default "stop")
     BUILD  0 to boot what is built as it is, instead of running scripts/build.sh guest first
            (the default on the Pi, which does not build the guest)
     UNIT   the unit's name (default the development VM's, beanjar-vm-<tag>-<vm>; --dir: the VM's,
            vmunits.vm_unit_name). It runs in beanjar-<tag>.slice, or for a name not of that form in
            beanjar.slice (vmunits.unit_slice). A unit of that name that runs another RUN_DIR is left
            alone; stop also stops the unit run/unit names, the one the VM was started as.
     INSTANCE  selects a managed development VM with its own vm.toml, identity, state,
            run and logs directories in a development library under the user's data.
            Unset (or "default") selects the default development VM. Each has a slot of its
            own (vm.toml's network.slot), so its ports are on its own address, 127.0.1.<slot>
            (`config ADDRESS`), and instances run side by side.
     RUN_DIR  where the VM's runtime files go (default: its managed run directory): qemu.pid, console.sock, qmp.sock,
            viewer-qmp.sock, dbus.sock, dbus-daemon.pid, cursor.sock, video.sock, ir.sock,
            media-<n>.sock, unit (the unit's name) or launcher.pid (the fallback's session), and
            share/ (the guest's shares: share.py)
     LOG_DIR  where the host-side logs go (default: the managed VM logs directory): console.log, webos.log, qemu.out,
            netstub.log and the mode=mitm helpers' logs
     SAVED_LOG_DIR  where logs saved at stop end up (default: the managed VM logs/saved directory)
Serial console: RUN_DIR/console.sock (`console`; SOCK/LOG for another RUN_DIR, or --dir), log
LOG_DIR/console.log. webOS console (hvc0): LOG_DIR/webos.log.

--dir VM-DIR: a VM kept in a directory of its own (the VM manager's layout, host/core/README.md).
Its vm.toml holds the settings, which are the launcher's environment (vmsettings.SETTINGS; an upper-case
key is a launcher variable passed through as it is, but ROOTFS and UNCOMPRESSED_PAKS, the checkout
VM's experiments). The firmware is the VM's own, <vm-dir>/firmware (FW), with its partition map
in firmware/cache (FW_CACHE). A fw in vm.toml names another directory instead (absolute, or
relative to the VM directory), which is used in place and never written: its map goes into
<vm-dir>/firmware-cache (core.vm_firmware). Everything else is
in the VM directory: state/ (STATE), run/ (RUN_DIR), logs/ (LOG_DIR), logs/saved/ (SAVED_LOG_DIR) and
identity/ (IDENTITY_DIR, mode 0700: the VM's own files, its SSH keys (beanjar/keys.py) and model key,
from which each launch assembles run/share/vm, the guest's read-only share "vm"; the checkout VM's
is a managed development VM). A key missing there is made again at start, with a warning. The mode=mitm proxy's CA is
the library's, <vm-dir>/../../ca (NETSTUB_CA; the development library's is ca/), made again at start,
with a warning, if it is missing; only its certificate goes into run/share/vm.
It boots what is built (BUILD=0), refuses to start a VM that runs, and runs as beanjar-vm-<tag>-<vm>,
<tag> its library's (vmunits.vm_unit_name). A
vm.toml without mac or serial (the TV's identity, MAC and SERIAL) gets them at start, written back,
with a warning (vmidentity.ensure_identity).
`status` prints "off", "starting <pid>" or "running <pid>" and exits 3 if off.

start (either kind) refuses, before anything changes, a VM beyond the machine's max_running (`beanjar
host config set max_running=N`): every beanjar VM up on this host counts (the units in beanjar.slice,
every library's and the regress harness's), but the one being restarted. It asks again under the
lock beside that setting while it starts the unit, so two starts at once cannot both take the last place.
"""

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import tomllib

from . import (
    compat,
    console,
    core,
    fwcache,
    fwfacts,
    hostswap,
    keys,
    netns,
    nvm,
    targets,
    paths,
    poweroff,
    prepare,
    qemu,
    rotation,
    runner,
    share,
    tuner,
    unit,
    vmidentity,
    vmsettings,
    vmunits,
)

TREE = paths.TREE
# Every runtime path of a --dir VM follows its directory, whatever the caller's environment; the
# checkout VM's uncompressed rootfs (ROOTFS) is not a --dir VM's.
RUNTIME_VARS = (
    "QEMU_PIDFILE",
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
    "NETSTUB_LOG",
    "NETNS_LOG_DIR",
    "FW_CACHE",
    "ROOTFS",
    "UNCOMPRESSED_PAKS",
    "SOCK",
    "LOG",
    "IDENTITY_DIR",
    "NETSTUB_CA",
    "INSTANCE",
    "SLOT",
    "VM_ADDRESS",
    "NETSTUB_WEB_PASSWORD_FILE",
    "FRESH_STATE",
)


class Error(Exception):
    pass


def say(line):
    print(line, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ instances of the checkout VM
def instance_env(env, tree=TREE):
    """Default runtime and identity paths of a managed development VM."""
    from . import development

    try:
        return development.environment(env, tree)
    except development.Error as error:
        raise Error(str(error)) from None


# ------------------------------------------------------------------ run (the foreground launcher)
def helper_environment(env):
    """env for a `python3 -m beanjar.X` helper: this package on its PYTHONPATH."""
    helper_env = dict(env)
    helper_env["PYTHONPATH"] = os.pathsep.join(
        x for x in (os.path.dirname(os.path.dirname(__file__)), helper_env.get("PYTHONPATH")) if x
    )
    return helper_env


# The media helper's environment (media_helper_environment): what it and the libraries it loads read,
# never the launcher's whole one (an agent socket, a session bus, a token exported in the starting
# shell), since it parses what the guest sends. It reads no variable itself (MEDIA_HWDEC comes as
# --hwdec) and runs nothing. Its FFmpeg's hardware decoders (codec.c, av_hwdevice_ctx_create with
# no device named) find their devices without the environment: CUDA through libcuda (on WSL in
# /usr/lib/wsl/lib, which WSL's own /etc/ld.so.conf.d entry puts on the loader's path, not
# LD_LIBRARY_PATH), VA-API and DRM at /dev/dri's nodes, V4L2 request (aarch64 boards) through
# udev's /dev/media and /dev/video nodes. What the environment may still choose or point to:
MEDIA_HELPER_ENV = frozenset(
    {
        "PATH",
        "LANG",
        "LANGUAGE",
        # a driver or libva outside the loader's path
        "LD_LIBRARY_PATH",
        # the drivers' caches (Mesa's shader cache, CUDA's ComputeCache) and the Vulkan loader's
        # driver and layer manifests
        "HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CONFIG_DIRS",
        "XDG_DATA_HOME",
        "XDG_DATA_DIRS",
    }
)
# locale; VA-API's driver (LIBVA_DRIVER_NAME, LIBVA_DRIVERS_PATH); Mesa's VA-API driver and its
# GPU (GALLIUM_DRIVER=d3d12 and MESA_D3D12_DEFAULT_ADAPTER_NAME on WSL); the Vulkan loader
# (VK_DRIVER_FILES, VK_ICD_FILENAMES); CUDA's devices (CUDA_VISIBLE_DEVICES)
MEDIA_HELPER_ENV_PREFIXES = ("LC_", "LIBVA_", "MESA_", "GALLIUM_", "VK_", "CUDA_")


def media_helper_environment(env):
    """The part of env the media helper gets: MEDIA_HELPER_ENV and MEDIA_HELPER_ENV_PREFIXES."""
    helper_env = {
        k: v
        for k, v in env.items()
        if k in MEDIA_HELPER_ENV or k.startswith(MEDIA_HELPER_ENV_PREFIXES)
    }
    helper_env.setdefault("PATH", os.defpath)
    return helper_env


def start_media_helper(p):
    """The media hub (p.hub_argv), a child that outlives the exec of QEMU and ends with this pid
    (QEMU's then), on media_helper_environment(p.env). It owns its logs (--log, --decode-log:
    bounded, rotated); RUN_DIR/media-helper.out has only its stdout and stderr: fatal errors and
    what comes before its logs open."""
    argv = [str(os.getpid()) if a == "PID" else a for a in p.hub_argv]
    pool = None
    if p.pool_socket:
        # the pool's ivshmem server socket, listening before the helper and QEMU start: QEMU's
        # ivshmem-doorbell connects at realize and waits there for the helper's setup
        pool = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(p.pool_socket)
        except FileNotFoundError:
            pass
        pool.bind(p.pool_socket)
        pool.listen(4)
        argv += ["--pool-fd", str(pool.fileno())]
    try:
        with open(os.path.join(p.run_dir, "media-helper.out"), "ab") as out:
            subprocess.Popen(
                argv,
                env=media_helper_environment(p.env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                close_fds=True,
                pass_fds=(pool.fileno(),) if pool else (),
            )
    finally:
        if pool:
            pool.close()


def start_capture_relay(p):
    """The capture relay (capture_relay.py) on p.capture_relay's capture.sock, the owner's alone, a
    child that ends with this pid."""
    path, qemu_sock, sink = p.capture_relay
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock.bind(path)
    os.chmod(path, 0o600)  # the owner's, as QEMU's own socket was
    sock.listen(8)
    try:
        with open(os.path.join(p.run_dir, "capture-relay.log"), "ab") as out:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "beanjar.capture_relay",
                    str(os.getpid()),
                    str(sock.fileno()),
                    qemu_sock,
                    p.run_dir,
                    *(sink or ()),
                ],
                env=helper_environment(p.env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                close_fds=True,
                pass_fds=(sock.fileno(),),
            )
    finally:
        sock.close()


def start_shell_relay(p):
    """The shell relay (guestshell.py) on p.shell_relay's shell.sock, the owner's alone (whoever
    opens it is root in the VM), a child that ends with this pid."""
    path, qemu_sock = p.shell_relay
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock.bind(path)
    os.chmod(path, 0o600)
    sock.listen(8)
    try:
        with open(os.path.join(p.run_dir, "shell-relay.log"), "ab") as out:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "beanjar.guestshell",
                    str(os.getpid()),
                    str(sock.fileno()),
                    qemu_sock,
                ],
                env=helper_environment(p.env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                close_fds=True,
                pass_fds=(sock.fileno(),),
            )
    finally:
        sock.close()


def run(words, env=None):
    """Boot in the foreground: exec QEMU (mode=direct), or run it in netns.py's wrapper."""
    env = dict(os.environ if env is None else env)
    guest_preflight(env)
    p = qemu.plan(env, words)
    qemu.make_disks(p)
    nodes = qemu.part_nodes(p)
    try:
        qemu.prepare_nvm(p, say)
    except (OSError, nvm.Error) as e:
        raise Error(f"EEPROM: {e}") from None
    os.makedirs(p.identity_dir, exist_ok=True)
    try:
        keys.ensure(p.identity_dir, warn=say, address=p.address)
        keys.ensure_ca(p.ca_dir, warn=say)
    except keys.Error as e:
        raise Error(str(e)) from None
    # the TEE's hardware-unique key, the VM's own (rtk.ko's stand-in TAs derive their keys from it)
    tee = targets.tee_module(p.target)
    vm = {tee: ("tee_huk=" + keys.ensure_tee_huk(p.identity_dir, warn=say),)} if tee else {}
    # an ARIB set's stub card, whose ID follows the TV's serial (webos_tv's card_id=)
    card = targets.card_module(p.target, p.broadcast)
    if card:
        vm[card] = vm.get(card, ()) + ("card_id=" + targets.card_id(p.env.get("SERIAL", "")),)
    # the StanbyME's accelerometer stands as the set was last turned (rotation.py, beanjar sensor set rotate)
    accel = rotation.launch_params(p.target, p.state_dir)
    if accel:
        vm["lgemu"] = accel
    qemu.guest_layer(p)
    try:
        # the modules' parameters and host share files from the firmware: the prepared board data's
        derived, files = p.prepared.derived_params, p.prepared.derived_files
        targets.validate_memory(p.target, derived, vm)
    except targets.Error as e:
        raise Error(str(e)) from None
    # every parameter boot.conf passes is one the guest's module declares (compat.py), before boot
    try:
        compat.module_params(
            p.tree, targets.module_params(p.target, derived, vm, p.broadcast), vm_name(p.run_dir)
        )
    except compat.Error as e:
        raise Error(str(e)) from None
    share.assemble(
        p.tree,
        p.target,
        p.run_dir,
        p.guest_layers,
        say,
        derived,
        nodes,
        vm,
        files,
        p.fw,
        p.broadcast,
        p.qemu_arm if p.userland == "qemu-user" else None,
        p.qemu_aarch64 if p.userland == "qemu-user" else None,
        (p.emmc_overlay, p.emmc_table, p.emmc_part_nodes) if p.updatable else None,
    )
    info = paths.vm_info(p.target, p.fw, p.fw_name)  # the viewer's window title
    info["layers"] = list(p.layers)  # the viewer's GPU meter: the gl layer is the guest on host GL
    if p.broadcast:
        info["broadcast"] = p.broadcast  # the board's standard: an ARIB board's Japanese remote
    if targets.soc(p.target).touch:
        # the viewer's touches go to the guest's touch screen (qemu.input_devices)
        info["touch"] = True
    if targets.soc(p.target).portrait:
        # the viewer turns the picture as the guest's rotation says (run/rotation) and offers to
        # turn the set (beanjar sensor set rotate)
        info["portrait"] = True
    if p.prepared.local_key:
        info["local_key"] = p.prepared.local_key  # the viewer's local key, prepared
    # the panel's refresh in Hz, prepared: the viewer's and beanjar-vnc's output rate
    info["panel_hz"] = p.prepared.panel_hz
    if not p.hub_argv:
        info["media"] = "off"  # no helper decodes: the viewer cores run as with --no-media
    info["beanjar"] = compat.run_info(p.tree)  # the versions, for logs and messages only
    paths.write_vm_info(p.run_dir, info)
    if p.gpu_backend_argv:
        # the GPU process (beanjar.gpu_backend): the vhost-user socket listens before QEMU starts;
        # the child takes QEMU's connection and becomes the backend, which ends with it
        gpu = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(p.gpu_socket)
        except FileNotFoundError:
            pass
        gpu.bind(p.gpu_socket)
        gpu.listen(1)
        with open(os.path.join(p.run_dir, "gpu-backend.out"), "ab") as out:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "beanjar.gpu_backend",
                    str(os.getpid()),
                    str(gpu.fileno()),
                    *p.gpu_backend_argv,
                ],
                env=helper_environment(p.env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                close_fds=True,
                pass_fds=(gpu.fileno(),),
            )
        gpu.close()
    if p.hub_argv:
        start_media_helper(p)
    if p.ir_relay:
        # the micom's IR receiver (ir_relay.py): ir.sock listens before QEMU starts, and the child,
        # which outlives the exec below and ends with this pid, relays it to QEMU's webos.ir port
        ir_sock, qemu_ir, qmp_sock = p.ir_relay
        ir = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(ir_sock)
        except FileNotFoundError:
            pass
        ir.bind(ir_sock)
        os.chmod(ir_sock, 0o600)  # the owner's, as QEMU's own socket was
        ir.listen(4)
        with open(os.path.join(p.run_dir, "ir-relay.log"), "ab") as out:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "beanjar.ir_relay",
                    str(os.getpid()),
                    str(ir.fileno()),
                    qemu_ir,
                    qmp_sock,
                ],
                env=helper_environment(p.env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                close_fds=True,
                pass_fds=(ir.fileno(),),
            )
        ir.close()
    if p.capture_relay:
        # the capture relay (capture_relay.py): capture.sock listens before QEMU starts, and the
        # child, which outlives the exec below and ends with this pid, serves QEMU's webos.capture
        # port from the front ends or its on-demand sink
        start_capture_relay(p)
    if p.shell_relay:
        # `beanjar shell`'s relay (guestshell.py): shell.sock listens before QEMU starts, and the
        # child, which outlives the exec below and ends with this pid, serves it on QEMU's
        # webos.shell port
        start_shell_relay(p)
    if tuner.has_tuner(p.target):
        # the antenna (tuner.py), a child that outlives the exec below and ends with this pid
        helper_env = helper_environment(p.env)
        with open(os.path.join(p.run_dir, "tuner.log"), "ab") as out:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "beanjar.tuner",
                    str(os.getpid()),
                    p.run_dir,
                    p.state_dir,
                    tuner.cache_dir(p.tree),
                    p.broadcast or "",
                ],
                env=helper_env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                close_fds=True,
            )
    setup = None

    def setup_display():
        nonlocal setup
        if p.display_setup:
            helper_env = helper_environment(p.env)
            setup = subprocess.Popen(
                [sys.executable, "-m", "beanjar.display_setup", str(os.getpid()), *p.display_setup],
                env=helper_env,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )

    try:
        if p.wrap == "off":
            # Link-down must complete before the display helper resumes the VM.
            return netns.off(p.argv, p.env, before_cont=setup_display if p.display_setup else None)
        setup_display()
        if p.wrap == "netns":
            return netns.run(p.argv, p.env)
        sys.stdout.flush()
        hostswap.open_fds(p.argv)  # its swap disk's memfd, inherited, if it has one
        os.execvpe(p.argv[0], p.argv, p.env)
    finally:
        if setup is not None:
            if setup.poll() is None:
                setup.terminate()
            setup.wait()


def prepare_vm(vm_dir=None, force=False):
    """`prepare`: the board data of the VM in vm_dir (a library VM; None: the checkout's development
    VM, as the environment resolves it), worked out again if missing or stale, or always (force)."""
    try:
        if vm_dir:
            prepare.vm(os.path.abspath(vm_dir), say, TREE, force)
        else:
            job = prepare.Job(dict(os.environ), TREE)
            prepare.make(job, say) if force else prepare.ensure(job, say)
    except (prepare.Error, nvm.Error) as e:
        raise Error(str(e)) from None
    return 0


def vm_name(run_dir):
    """The VM a run directory is of: VM-DIR/run's VM-DIR name (the checkout's build/: the checkout's)."""
    return os.path.basename(os.path.dirname(os.path.abspath(run_dir)))


def guest_preflight(env, name=None):
    """compat.components for the firmware of env (FW): Error, before anything of the VM is touched,
    if the guest payload lacks one its target needs."""
    try:
        fw, fw_name = TREE.firmware(env.get("FW"))
        target = paths.firmware_target(fw, fw_name)
    except ValueError as e:
        raise Error(str(e)) from None
    name = name or vm_name(os.path.join(TREE.top, env.get("RUN_DIR") or "build"))
    try:
        compat.components(TREE, target, name)
    except compat.Error as e:
        raise Error(str(e)) from None


def argv(words, env=None):
    env = dict(os.environ if env is None else env)
    p = qemu.plan(env, words, dry=True)
    if p.wrap:
        print(
            f"# NET_LINK={env.get('NET_LINK') or 'nat'} NET_MODE={env.get('NET_MODE') or 'mitm'}: run by netns.py ({p.wrap})"
        )
    for k in (
        "GALLIUM_DRIVER",
        "VIRGL_USE_INTEGER",
        "NETNS_FWD",
        "NETNS_TAP",
        "NETSTUB_LOG",
        "LISTEN_ADDRESS",
        "NET_LAN_ACCESS",
        "NET_LG_SERVICES",
    ):
        if p.env.get(k) != env.get(k):
            print(f"# {k}={shlex.quote(p.env[k])}")
    print(shlex.join(p.argv))
    return 0


# ------------------------------------------------------------------ start/stop in the background
def settings_vars(vm_dir):
    """The launcher variables vm_dir's vm.toml sets (core.vm_launcher_env), none without one."""
    path = os.path.join(vm_dir, "vm.toml")
    if not os.path.isfile(path):
        return ()
    try:
        return tuple(core.vm_launcher_env(path))
    except core.Error:
        return ()


class Bg:
    """One VM's runtime directories and unit, from its environment."""

    def __init__(self, env):
        self.env = env
        run_dir = os.path.join(TREE.top, env.get("RUN_DIR") or "build")
        log_dir = os.path.join(TREE.top, env.get("LOG_DIR") or "build")
        os.makedirs(run_dir, exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)
        self.run_dir, self.log_dir = os.path.abspath(run_dir), os.path.abspath(log_dir)
        self.saved = env.get("SAVED_LOG_DIR") or TREE.build("logs")
        self.pidfile = os.path.join(self.run_dir, "qemu.pid")
        self.sock = os.path.join(self.run_dir, "console.sock")
        self.log = os.path.join(self.log_dir, "console.log")
        env.update(SOCK=self.sock, LOG=self.log, RUN_DIR=self.run_dir, LOG_DIR=self.log_dir)
        self.unit = env.get("UNIT") or vmunits.vm_unit_name(os.path.dirname(self.run_dir))
        self.use_unit = unit.available(env)
        self.paths = unit.Paths(dict(env, QEMU_PIDFILE=self.pidfile))
        self.password_file = os.path.join(
            self.run_dir, netns.PASSWORD_FILE
        )  # the inspector's (netns.py)
        # Beyond the unit's own allowlist (unit.env_for), the VM's vm.toml settings: any upper-case
        # key there is a launcher variable passed through as it is (vmsettings.SETTINGS).
        self.keep = settings_vars(os.path.dirname(self.run_dir))

    def recorded_unit(self):
        """The unit the VM was last started as (run/unit), which a start under another UNIT leaves."""
        try:
            with open(os.path.join(self.run_dir, "unit")) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def units(self):
        """UNIT, and the unit run/unit names if another."""
        return list(dict.fromkeys(u for u in (self.unit, self.recorded_unit()) if u))

    def unit_state(self, name=None):
        """ActiveState of the unit (UNIT, or name) if it is this VM's (its description names RUN_DIR), else ""."""
        if not self.use_unit:
            return ""
        u = vmunits.unit_show(name or self.unit)
        return (
            u.get("ActiveState", "") if u.get("Description") == f"webOS VM {self.run_dir}" else ""
        )

    def running(self):
        """The VM's processes are up, by any runner (the unit active or activating, or the
        session's leader alive)."""
        return any(r.running(self) for r in runner.every())

    def save_logs(self):
        """Through the serial console, if the VM is up (guest/scripts/save-logs.sh, in the guest)."""
        if (
            self.env.get("SAVE_LOGS", "1") != "1"
            or not unit.alive(core.read_pid(self.pidfile))
            or not unit.is_socket(self.sock)
        ):
            return
        name = (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            + "-"
            + (self.env.get("LOG_TAG") or "stop")
        )
        console.send(f"sh /host/guest/scripts/save-logs.sh {name}", 1, self.sock, self.log)
        # The guest's /host/out/logs: everything there is the guest's, links included, so only
        # share.finished and share.save look at it, and the launcher's own logs go into the saved
        # copy (share.save), never into the share.
        logs = share.logs_dir(self.run_dir)
        end = time.monotonic() + 60
        while (why := share.finished(logs, name)) is None:
            if time.monotonic() >= end:
                say("logs: no reply from the guest within 60 s")
                return
            time.sleep(1)
        if why:
            say(f"logs: {why}; not saved")
            return
        host = {f: os.path.join(self.log_dir, f) for f in share.HOST_LOGS}
        saved = share.save(logs, name, self.saved, host, say)
        if saved:
            print(f"logs saved: {saved}", flush=True)

    def power_off(self, timeout):
        """webOS's own power-off: tvpowerd's powerOff with the force
        reason cpuCommand (poweroff.POWER_OFF_CALL), typed on the serial console. The set's shutdown
        ends with reboot(2), which the guest's webos-start turns into the machine's power-off, so
        QEMU exits by itself. Returns 0 once it has (or was not running); core.STOP_REFUSED at once
        if there is no console or the call's reply says webOS cannot take it (poweroff.power_off_refused:
        an explicit "returnValue": false or an error), when nothing is pending; else
        core.STOP_TIMED_OUT if QEMU still runs timeout s after the call. No reply at all is not a
        refusal: webOS has powered off after a call that printed none, so QEMU is waited for."""
        pid = core.read_pid(self.pidfile)
        if not unit.alive(pid) or not unit.comm(pid).startswith("qemu-system"):
            return 0
        sent = os.path.join(self.run_dir, poweroff.POWER_OFF_SENT)
        t0 = time.monotonic()
        if core.read_pid(sent) == pid:  # a stop before sent it: wait again
            print("power-off: sent before; waiting for QEMU to exit", flush=True)
        elif not unit.is_socket(self.sock):
            say("power-off: no serial console")
            return core.STOP_REFUSED
        else:
            # the platform this launch runs (its vm-info.toml): the test guest's call is its own
            try:
                with open(os.path.join(self.run_dir, paths.VM_INFO), "rb") as f:
                    platform = tomllib.load(f).get("platform")
            except (OSError, ValueError):
                platform = None
            call = poweroff.power_off_call(platform)
            with open(sent, "w") as f:
                f.write(f"{pid}\n")
            for attempt in range(1, core.POWER_OFF_TRIES + 1):
                text = console.send(call, 10, self.sock, self.log)
                reply = re.findall(r"\{[^{}]*returnValue[^{}]*\}", text)  # among the kernel's lines
                if call == poweroff.POWER_OFF_CALL:
                    print(
                        f"power-off: sent; tvpowerd: {reply[-1] if reply else 'no reply'}",
                        flush=True,
                    )
                else:
                    print(f"power-off: sent ({call})", flush=True)
                if not poweroff.power_off_busy(text) or attempt == core.POWER_OFF_TRIES:
                    break
                say(
                    f"power-off: tvpowerd is busy; retrying in {core.POWER_OFF_RETRY_S:g} s "
                    f"({attempt}/{core.POWER_OFF_TRIES - 1})"
                )
                time.sleep(core.POWER_OFF_RETRY_S)
            if poweroff.power_off_refused(text):
                try:
                    os.unlink(sent)
                except OSError:
                    pass
                say("power-off: webOS refused the call" + (f": {reply[-1]}" if reply else ""))
                return core.STOP_REFUSED
        while unit.alive(pid) and unit.comm(pid).startswith("qemu-system"):
            if time.monotonic() - t0 >= timeout:
                return core.STOP_TIMED_OUT
            time.sleep(0.5)
        print(f"power-off: QEMU exited {time.monotonic() - t0:.0f} s after the call", flush=True)
        return 0

    def harvest(self):
        """The guest's other finished logs (check-m1.sh's), into SAVED_LOG_DIR."""
        for d in share.harvest(self.run_dir, self.saved, say):
            print(f"logs saved: {d}", flush=True)

    def stop(self):
        """Stop the VM by every runner (runner.every: its unit, its session; SIGKILL if it does not
        exit), whichever it was started with."""
        for r in runner.every():
            r.stop(self)
        self.harvest()
        for f in (self.password_file, os.path.join(self.run_dir, poweroff.POWER_OFF_SENT)):
            try:  # the password: netns.py removes it, unless killed
                os.unlink(f)
            except FileNotFoundError:
                pass
        # A private bus left behind (its VM's unit or session ends it).
        bus = core.read_pid(os.path.join(self.run_dir, "dbus-daemon.pid"))
        if bus is not None:
            if unit.comm(bus) == "dbus-daemon":
                try:
                    os.kill(bus, 15)
                except OSError:
                    pass
            for n in ("dbus-daemon.pid", "dbus.sock"):
                try:
                    os.unlink(os.path.join(self.run_dir, n))
                except FileNotFoundError:
                    pass

    def refuse_beyond_limit(self, limit=None):
        """Error if the machine's max_running VMs run already, this one not counted (core.running_refusal)."""
        why = core.running_refusal(
            os.path.basename(os.path.dirname(self.run_dir)), self.run_dir, self.unit, limit=limit
        )
        if why:
            raise Error(why)

    def start(self, words):
        env = self.env
        try:
            how = runner.get(runner.choose(env))
        except core.Error as e:
            raise Error(str(e)) from None
        if self.use_unit and vmunits.unit_show(self.unit).get("ActiveState") in unit.UP:
            desc = vmunits.unit_show(self.unit).get("Description", "")
            raise Error(f"{self.unit}.service runs another VM ({desc}); stop it first")
        for f in (
            self.sock,
            os.path.join(self.run_dir, "qmp.sock"),
            self.pidfile,
            self.log,
            os.path.join(self.log_dir, "webos.log"),
            os.path.join(self.log_dir, "netstub.log"),
            os.path.join(self.run_dir, "unit"),
            os.path.join(self.run_dir, poweroff.POWER_OFF_SENT),
            self.password_file,
        ):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        if (
            not TREE.installed
            and env.get("BUILD", "0" if qemu.default_accel() == "kvm" else "1") != "0"
        ):
            if (
                subprocess.run(
                    [TREE.src("scripts", "build.sh"), "guest"], stdout=subprocess.DEVNULL
                ).returncode
                != 0
            ):
                raise Error("scripts/build.sh guest failed")
        # The launcher's paths (qemu.py); the build/ defaults are the same files.
        for k, v in (
            ("CURSOR_SOCK", "cursor.sock"),
            ("VIDEO_SOCK", "video.sock"),
            ("IR_SOCK", "ir.sock"),
            ("MEDIA_SOCK", "media"),
            ("CAPTURE_SOCK", "capture.sock"),
            ("VIEWER_QMP_SOCK", "viewer-qmp.sock"),
            ("QMP_SOCK", "qmp.sock"),
        ):
            env[k] = env.get(k) or os.path.join(self.run_dir, v)
        env["DBUS_ADDR"] = (
            env.get("DBUS_ADDR") or f"unix:path={os.path.join(self.run_dir, 'dbus.sock')}"
        )
        env["NETSTUB_LOG"] = env.get("NETSTUB_LOG") or os.path.join(self.log_dir, "netstub.log")
        env["NETNS_LOG_DIR"] = env.get("NETNS_LOG_DIR") or self.log_dir
        # The inspector's password is not one the unit's environment takes (unit.env_for; systemd-run
        # --setenv is argv): a BEANJAR_MITM_PASSWORD is handed to netns.py in an owner-only file
        # instead, and without one netns.py makes a random one for this start.
        given = env.get(netns.PASSWORD_ENV, "")
        if given:
            try:
                env["NETSTUB_WEB_PASSWORD_FILE"] = netns.write_password(self.password_file, given)
            except netns.Error as e:
                raise Error(str(e)) from None
        env.update(
            CONSOLE_SOCK=self.sock,
            CONSOLE_LOG=self.log,
            WEBOS_LOG=os.path.join(self.log_dir, "webos.log"),
            QEMU_PIDFILE=self.pidfile,
        )
        out = os.path.join(self.log_dir, "qemu.out")
        open(out, "w").close()
        # The machine's max_running, asked again (start_check asked before anything changed) under
        # its lock until the unit is up, where the next start's count sees it.
        with core.running_lock() as lock:
            self.refuse_beyond_limit(lock.limit)
            try:
                how.start(self, env, words, mem=qemu.vm_guest_ram(env))
            except core.Error as e:
                raise Error(str(e)) from None
        # Until the initramfs shell is up, the launcher fails, or 120 s pass.
        time.sleep(1)
        for _ in range(60):
            if booted(self.log) or not self.running():
                break
            time.sleep(2)
        with open(out, "rb") as f:
            sys.stdout.buffer.write(f.read())
        sys.stdout.flush()
        if not self.running():
            raise Error(f"the VM is not running (see {how.where(self)})")


def booted(log):
    try:
        with open(log, "rb") as f:
            return b"debug shell" in f.read()
    except OSError:
        return False


def start_check(env=None):
    """Refuse a start beyond the machine's max_running before anything changes (Bg.refuse_beyond_limit)."""
    env = dict(os.environ if env is None else env)
    run_dir = os.path.abspath(os.path.join(TREE.top, env.get("RUN_DIR") or "build"))
    unit_name = env.get("UNIT") or vmunits.vm_unit_name(os.path.dirname(run_dir))
    why = core.running_refusal(os.path.basename(os.path.dirname(run_dir)), run_dir, unit_name)
    if why:
        raise Error(why)


def bg_start(words, env=None):
    b = Bg(dict(os.environ if env is None else env))
    b.save_logs()
    b.stop()
    b.start(words)
    return 0


def regress_lane_env(env, tree=TREE):
    """The environment of the regress lane of env's INSTANCE (scripts/regress: its run and logs
    directories under build/instances/<INSTANCE>, its unit beanjar-vm-regress-<INSTANCE>), None
    unless INSTANCE names one that exists and env sets no RUN_DIR of its own. A lane runs under
    those, not under the development VM's, so a stop by INSTANCE alone would miss it."""
    iid = env.get("INSTANCE")
    if not iid or iid == "default" or env.get("RUN_DIR"):
        return None
    run = tree.build("instances", iid)
    if not os.path.isdir(os.path.join(tree.top, run)):
        return None
    return dict(env, RUN_DIR=run, LOG_DIR=run, UNIT=vmunits.regress_unit_name(iid))


def bg_stop(env=None, power_off=None):
    """Stop the VM: its logs saved, then with power_off (seconds) the power-off signal (Bg.power_off),
    else the guest synced and QEMU ended (Bg.stop). Exit status core.STOP_TIMED_OUT (4) if the guest
    does not power off in time, core.STOP_REFUSED (5) if webOS refuses the signal; the VM is left
    running then."""
    b = Bg(dict(os.environ if env is None else env))
    b.save_logs()
    if power_off is not None:
        rc = b.power_off(power_off)
        if rc == core.STOP_TIMED_OUT:
            say(f"power-off: QEMU still runs {power_off:g} s after the call")
        if rc:
            return rc
    b.stop()
    return 0


# ------------------------------------------------------------------ --dir: one VM of the manager's layout
def vm_alive(vm_dir):
    """QEMU's pid, if the VM's pidfile names a live QEMU (in its unit, for a VM started as one)."""
    run_dir = os.path.join(vm_dir, "run")
    pid = core.read_pid(os.path.join(run_dir, "qemu.pid"))
    if not unit.alive(pid) or not unit.comm(pid).startswith("qemu-system"):
        return None
    try:
        with open(os.path.join(run_dir, "unit")) as f:
            u = f.read().strip()
    except OSError:
        u = None
    if u and not core.in_unit(pid, u):
        return None
    return pid


def vm_status(vm_dir):
    pid = vm_alive(vm_dir)
    if pid:
        return (
            "running" if booted(os.path.join(vm_dir, "logs", "console.log")) else "starting"
        ), pid
    return "off", None


def vm_env(vm_dir, env):
    """The environment of the VM in vm_dir: env without the runtime paths, vm.toml's settings, and
    the VM directory's paths."""
    env = {k: v for k, v in env.items() if k not in RUNTIME_VARS}
    try:
        env.update(core.vm_launcher_env(os.path.join(vm_dir, "vm.toml")))
    except core.Error as e:
        raise Error(str(e)) from None
    run_dir, log_dir = os.path.join(vm_dir, "run"), os.path.join(vm_dir, "logs")
    env.update(
        RUN_DIR=run_dir,
        LOG_DIR=log_dir,
        SAVED_LOG_DIR=os.path.join(log_dir, "saved"),
        UNIT=vmunits.vm_unit_name(vm_dir),
        IDENTITY_DIR=core.identity_dir(vm_dir),
        NETSTUB_CA=vmidentity.ca_dir(vm_dir),
        ADDRESS_PREFIX=core.addresses_of(core.library_of(vm_dir)),
        NET_LAN_PARENT=vmidentity.lan_parent_of(core.library_of(vm_dir)) or "",
        NET_LAN_HOSTNAME=vmsettings.dns_label(os.path.basename(vm_dir)),
    )
    return env


def vm(cmd, vm_dir, words, env=None, power_off=None):
    env = dict(os.environ if env is None else env)
    if not os.path.isdir(vm_dir):
        raise Error(f"no VM directory {vm_dir}")
    vm_dir = os.path.abspath(vm_dir)
    if cmd == "status":
        st, pid = vm_status(vm_dir)
        print(f"{st} {pid}" if pid else st)
        return 0 if pid else 3
    if cmd == "stop":
        run_dir, log_dir = os.path.join(vm_dir, "run"), os.path.join(vm_dir, "logs")
        env = {k: v for k, v in env.items() if k not in RUNTIME_VARS}
        return bg_stop(
            dict(
                env,
                RUN_DIR=run_dir,
                LOG_DIR=log_dir,
                SAVED_LOG_DIR=os.path.join(log_dir, "saved"),
                UNIT=vmunits.vm_unit_name(vm_dir),
            ),
            power_off,
        )
    if not os.path.isfile(os.path.join(vm_dir, "vm.toml")):
        raise Error(f"no {vm_dir}/vm.toml")
    pid = vm_alive(vm_dir)
    if pid:
        raise Error(f"{vm_dir} is already running (pid {pid}); stop it first")
    start_check(dict(env, RUN_DIR=os.path.join(vm_dir, "run"), UNIT=vmunits.vm_unit_name(vm_dir)))
    try:
        fw = core.vm_launcher_env(os.path.join(vm_dir, "vm.toml"))["FW"]
    except core.Error as e:
        raise Error(str(e)) from None
    guest_preflight(dict(env, FW=fw), os.path.basename(vm_dir))
    try:
        vmidentity.ensure_identity(
            vm_dir, say
        )  # a vm.toml without mac, serial or slot gets them now
        address = core.vm_address(
            vm_dir, core.vmconfig.read(os.path.join(vm_dir, "vm.toml"))["slot"]
        )
        keys.ensure(
            vmidentity.make_identity_dir(vm_dir), warn=say, address=address
        )  # a missing key is made again
        keys.ensure_ca(vmidentity.ca_dir(vm_dir), warn=say)  # the library's CA
    except (core.Error, keys.Error) as e:
        raise Error(str(e)) from None
    env = vm_env(vm_dir, env)
    for d in ("state", "run", "logs"):
        os.makedirs(os.path.join(vm_dir, d), exist_ok=True)
    env.update(STATE=os.path.join(vm_dir, "state"), BUILD=env.get("BUILD") or "0")
    bg_start(words, env)
    if not vm_alive(vm_dir):
        raise Error(f"the VM did not start (see {env['LOG_DIR']}/qemu.out)")
    return 0


# ------------------------------------------------------------------ netns-exec
def netns_exec(vm_dir, command):
    """Run command (argv) in the network namespace of the VM's running QEMU (vm_dir's, else the
    checkout VM's RUN_DIR/qemu.pid): it replaces this process (netns.enter_argv)."""
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise Error("netns-exec: no command")
    if vm_dir:
        run_dir = os.path.join(os.path.abspath(vm_dir), "run")
    else:
        run_dir = os.path.join(TREE.top, os.environ.get("RUN_DIR") or "build")
    pid = core.read_pid(os.path.join(run_dir, "qemu.pid"))
    if not unit.alive(pid) or not unit.comm(pid).startswith("qemu-system"):
        raise Error(f"netns-exec: the VM of {run_dir} is not running")
    try:
        argv = netns.enter_argv(pid, command)
    except netns.Error as e:
        raise Error(str(e)) from None
    sys.stdout.flush()
    os.execvp(argv[0], argv)


# ------------------------------------------------------------------ qmp: one command
def qmp(sock, command, timeout=10.0):
    """Send command (a QMP JSON object, as text) over the QMP socket sock; its reply (a dict)."""
    try:
        cmd = json.loads(command)
    except ValueError as e:
        raise Error(f"not JSON: {e}") from None
    if not isinstance(cmd, dict) or "execute" not in cmd:
        raise Error('not a QMP command (an object with "execute")')
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(sock)
    except OSError as e:
        s.close()
        raise Error(f"{sock}: {e.strerror or e}") from None
    with s, s.makefile("rw") as f:

        def reply():
            while True:
                line = f.readline()
                if not line:
                    raise Error("QEMU closed the QMP connection")
                r = json.loads(line)
                if "return" in r or "error" in r:
                    return r

        f.readline()  # greeting
        f.write('{"execute":"qmp_capabilities"}\n')
        f.flush()
        r = reply()
        if "error" in r:
            return r
        f.write(json.dumps(cmd) + "\n")
        f.flush()
        return reply()


# ------------------------------------------------------------------ the command line
def parser():
    p = argparse.ArgumentParser(
        prog="host/scripts/vm-launcher",
        description="Boot, stop and drive webOS VMs (beanjar/launch.py)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, text in (
        ("run", "boot in the foreground"),
        ("argv", "print the QEMU command of run"),
    ):
        s = sub.add_parser(name, help=text)
        s.add_argument("words", nargs=argparse.REMAINDER, help="kernel cmdline words")
    for name, text in (
        ("start", "(re)boot in the background"),
        ("stop", "stop: logs saved, guest synced"),
        ("status", "off, starting <pid> or running <pid> (--dir)"),
    ):
        s = sub.add_parser(name, help=text)
        s.add_argument(
            "--dir", help="a VM directory (the manager's layout) instead of the checkout VM"
        )
        if name == "start":
            s.add_argument("words", nargs=argparse.REMAINDER, help="kernel cmdline words")
        if name == "stop":
            s.add_argument(
                "--power-off",
                type=float,
                metavar="SECONDS",
                help="after the logs, webOS's own power-off instead of the sync and SIGTERM; exit "
                "status 4 if QEMU still runs SECONDS after the call",
            )
    s = sub.add_parser("console", help="type a line on the serial console")
    s.add_argument("--dir", help="a VM directory: its run/console.sock and logs/console.log")
    s.add_argument("line")
    s.add_argument("wait", nargs="?", type=float, default=3.0)
    s = sub.add_parser("qmp", help="send one QMP command, print the reply")
    s.add_argument("--dir", help="a VM directory: its run/qmp.sock")
    s.add_argument("command", help='a QMP command, e.g. \'{"execute":"query-status"}\'')
    s = sub.add_parser("put", help="give a running VM files of the checkout (at /host/PATH)")
    s.add_argument("--dir", help="a VM directory: its run/share")
    s.add_argument("paths", nargs="+")
    s = sub.add_parser(
        "put-out", help="copy a host file into the VM's out share (at /host/out/DEST)"
    )
    s.add_argument("--dir", help="a VM directory: its run/share")
    s.add_argument("src")
    s.add_argument("dest", help="the path in the out share")
    s = sub.add_parser(
        "get-out", help="copy a regular file of the VM's out share (/host/out/SRC) to DEST"
    )
    s.add_argument("--dir", help="a VM directory: its run/share")
    s.add_argument("src", help="the path in the out share")
    s.add_argument("dest", nargs="?", default="-", help="a file or directory; - (default): stdout")
    s = sub.add_parser("rm-out", help="remove files of the VM's out share (/host/out/PATH)")
    s.add_argument("--dir", help="a VM directory: its run/share")
    s.add_argument("paths", nargs="+", help="paths in the out share")
    s = sub.add_parser(
        "netns-exec", help="run a command in the running VM's network namespace (link nat)"
    )
    s.add_argument("--dir", help="a VM directory: its run/qemu.pid")
    s.add_argument("command", nargs=argparse.REMAINDER, help="the command and its arguments")
    s = sub.add_parser("logs", help="save the guest's finished logs to SAVED_LOG_DIR")
    s.add_argument("--dir", help="a VM directory: its run/share and logs/saved")
    s = sub.add_parser(
        "prepare",
        help="work out the VM's board data from its firmware (prepare.py) if it is missing or stale",
    )
    s.add_argument("--dir", help="a VM directory (default: the checkout's development VM)")
    s.add_argument("--force", action="store_true", help="whatever it has")
    s = sub.add_parser("reset-state", help="wipe a state directory")
    s.add_argument("state", nargs="?")
    s.add_argument("parts", nargs="*")
    s = sub.add_parser("firmware-cache", help="the ROOTFS=uncompressed images")
    s.add_argument("paks", nargs="*")
    s = sub.add_parser(
        "firmware-facts",
        help="write a target's firmware facts, what its guest build asks of its firmware (fwfacts.py)",
    )
    fwfacts.add_arguments(s)
    s = sub.add_parser(
        "config",
        help="print FW, FW_NAME, TARGET, PAKS, TARGETS, RELEASES, FIRMWARE_DIRS, RENDERNODE, IDENTITY or ADDRESS",
    )
    s.add_argument(
        "var",
        choices=(
            "FW",
            "FW_NAME",
            "TARGET",
            "PAKS",
            "TARGETS",
            "RELEASES",
            "FIRMWARE_DIRS",
            "RENDERNODE",
            "IDENTITY",
            "ADDRESS",
            "MAC",
            "SERIAL",
        ),
    )
    s.add_argument(
        "--target", help="FW, FW_NAME: the firmware of this target (<chip>-<release>), not FW's"
    )
    s.add_argument(
        "--dir", help="IDENTITY, ADDRESS: of this VM directory, not the development VM's"
    )
    sub.add_parser(
        "migrate-local", help="copy legacy checkout keys, CA and state to the development VM"
    )
    return p


def main(argv_=None):
    a = parser().parse_args(argv_)
    try:
        from . import development

        if a.cmd == "migrate-local":
            print("migrated development VM: " + development.migrate(dict(os.environ)))
            print("original local/ and state files retained")
            return 0
        if not getattr(a, "dir", None) and a.cmd not in (
            "config",
            "firmware-cache",
            "firmware-facts",
        ):
            original = dict(os.environ)
            resolved = (
                development.configured_environment(original)
                if a.cmd == "argv"
                else instance_env(original)
            )
            if a.cmd == "start":
                start_check(resolved)  # before the development VM is made or restarted
            if a.cmd in ("run", "start") and not original.get("IDENTITY_DIR"):
                # before the development VM is made or touched: the guest has what its target needs
                _library, dev_vm = development.locations(original)
                guest_preflight(resolved, os.path.basename(dev_vm))
                resolved = development.prepare(original)
            os.environ.update(resolved)
        if a.cmd == "prepare":
            return prepare_vm(a.dir, a.force)
        if a.cmd == "run":
            return run(a.words)
        if a.cmd == "argv":
            return argv(a.words)
        if a.cmd in ("start", "stop", "status"):
            power_off = getattr(a, "power_off", None)
            if a.dir:
                return vm(a.cmd, a.dir, getattr(a, "words", []), power_off=power_off)
            if a.cmd == "status":
                _library, directory = development.locations(os.environ)
                return vm("status", directory, []) if os.path.isdir(directory) else 3
            if a.cmd == "start":
                return bg_start(a.words)
            lane = regress_lane_env(original)
            if lane is not None and Bg(dict(lane)).running():
                return bg_stop(lane, power_off)  # the INSTANCE's regress lane runs, not its dev VM
            return bg_stop(power_off=power_off)
        if a.cmd == "console":
            sock = log = None
            if a.dir:
                sock, log = (
                    os.path.join(a.dir, "run", "console.sock"),
                    os.path.join(a.dir, "logs", "console.log"),
                )
            else:
                sock = os.environ.get("SOCK") or os.path.join(
                    TREE.top, os.environ.get("RUN_DIR") or "build", "console.sock"
                )
                log = os.environ.get("LOG") or os.path.join(
                    TREE.top, os.environ.get("LOG_DIR") or "build", "console.log"
                )
            sys.stdout.write(console.send(a.line, a.wait, sock, log))
            return 0
        if a.cmd == "qmp":
            if a.dir:
                sock = os.path.join(os.path.abspath(a.dir), "run", "qmp.sock")
            else:
                sock = os.environ.get("QMP_SOCK") or os.path.join(
                    TREE.top, os.environ.get("RUN_DIR") or "build", "qmp.sock"
                )
            r = qmp(sock, a.command)
            print(json.dumps(r))
            return 1 if "error" in r else 0
        if a.cmd == "netns-exec":
            return netns_exec(a.dir, a.command)
        if a.cmd in ("put", "put-out", "get-out", "rm-out", "logs"):
            if a.dir:
                vm_dir = os.path.abspath(a.dir)
                run_dir, saved = os.path.join(vm_dir, "run"), os.path.join(vm_dir, "logs", "saved")
            else:
                run_dir = os.path.join(TREE.top, os.environ.get("RUN_DIR") or "build")
                saved = os.environ.get("SAVED_LOG_DIR") or TREE.build("logs")
            if a.cmd == "put":
                host = share.dirs(run_dir)[2]
                for rel in share.put(TREE, host, [os.path.abspath(p) for p in a.paths]):
                    print(f"/host/{rel}")
            elif a.cmd == "put-out":
                share.place(share.dirs(run_dir)[3], a.dest, a.src)
                print(f"/host/out/{a.dest}")
            elif a.cmd == "get-out":
                share.take(share.dirs(run_dir)[3], a.src, a.dest)
            elif a.cmd == "rm-out":
                for rel in a.paths:
                    share.remove(share.dirs(run_dir)[3], rel)
            else:
                for d in share.harvest(run_dir, saved, say):
                    print(f"logs saved: {d}")
            return 0
        if a.cmd == "reset-state":
            qemu.reset_state(
                a.state
                or os.environ.get("STATE")
                or os.path.join(TREE.top, os.environ.get("RUN_DIR") or "build", "state", "default"),
                a.parts,
            )
            return 0
        if a.cmd == "firmware-cache":
            return fwcache.main(a.paks)
        if a.cmd == "firmware-facts":
            try:
                return fwfacts.run(a)
            except fwfacts.Error as e:
                raise Error(str(e)) from None
        if a.cmd == "config" and a.target:
            target = targets.by_name(a.target)
            if a.var not in ("FW", "FW_NAME") or target is None:
                raise Error(
                    f"--target {a.target}: only for FW or FW_NAME, of a target this tree boots (config TARGETS)"
                )
            try:
                fw, name = TREE.target_firmware(target)
            except ValueError as e:
                raise Error(str(e)) from None
            print(fw if a.var == "FW" else name)
            return 0
        if a.cmd == "config" and a.var == "RENDERNODE":
            print(qemu.tcg_rendernode())
            return 0
        if a.cmd == "config" and a.var in ("TARGET", "PAKS"):
            try:
                fw, fw_name = TREE.firmware(os.environ.get("FW"))
                target = paths.firmware_target(fw, fw_name)
            except ValueError as e:
                raise Error(str(e)) from None
            print(
                targets.name(target)
                if a.var == "TARGET"
                else "\n".join(f + ".pak" for f in targets.pak_files(target, fw))
            )
            return 0
        if a.cmd == "config" and a.var == "TARGETS":
            print("\n".join(targets.name(p) for p in targets.all_targets()))
            return 0
        if a.cmd == "config" and a.var == "RELEASES":
            print("\n".join(targets.WEBOS_ORDER))  # the webOS releases, oldest first
            return 0
        if a.cmd == "config" and a.var == "FIRMWARE_DIRS":
            # the host's firmware collections, one per line (scripts/regress looks a run's firmware up in the VM host's)
            try:
                print("\n".join(TREE.firmware_dirs()))
            except ValueError as e:
                raise Error(str(e)) from None
            return 0
        if a.cmd == "config" and a.var == "ADDRESS":
            if a.dir:
                directory = os.path.abspath(a.dir)
                vmidentity.ensure_slot(directory, say)
            else:
                _library, directory = development.locations(os.environ)
                if not os.path.isfile(os.path.join(directory, "vm.toml")):
                    raise Error(
                        "no development VM yet (INSTANCE's): it gets its slot at its first start"
                    )
            print(
                core.vm_address(
                    directory, core.vmconfig.read(os.path.join(directory, "vm.toml"))["slot"]
                )
            )
            return 0
        if a.cmd == "config" and a.var in ("MAC", "SERIAL"):
            # the guest's: the environment's, else the development VM's (INSTANCE's) vm.toml; empty
            # before its first start (the board's then)
            if a.dir:
                env = core.vm_launcher_env(os.path.join(a.dir, "vm.toml"))
            else:
                env = development.configured_environment(dict(os.environ))
            print(os.environ.get(a.var) or env.get(a.var) or "")
            return 0
        if a.cmd == "config" and a.var == "IDENTITY":
            if getattr(a, "dir", None):
                print(core.identity_dir(a.dir))
            else:
                print(os.path.join(TREE.top, instance_env(dict(os.environ))["IDENTITY_DIR"]))
            return 0
        if a.cmd == "config":
            fw, name = TREE.firmware(os.environ.get("FW"))
            print(fw if a.var == "FW" else name)
            return 0
    except (
        Error,
        compat.Error,
        development.Error,
        core.Error,
        keys.Error,
        qemu.Error,
        netns.Error,
        share.Error,
        OSError,
        subprocess.CalledProcessError,
    ) as e:
        print(f"vm {a.cmd}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
