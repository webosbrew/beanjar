# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What a VM's unit does when its guest restarts or the host's disk fills.

QEMU runs with `-action reboot=shutdown` and a QMP monitor of the unit's own (qemu.plan, when
LIFECYCLE_SOCK is set): a guest restart (webOS's `reboot -f`, PSCI, QMP system_reset) ends QEMU, and
unit.run starts the launcher again in the same unit, a cold start like a real TV's. The exit status
is 0 for a restart and a power-off alike; the SHUTDOWN event's reason tells them apart. The same
monitor sees BLOCK_IO_ERROR: a write that hit ENOSPC on the host (werror=enospc, QEMU's default,
pauses the VM) makes QEMU quit instead of staying paused.

  Monitor(path, say)   listens on path before the launcher starts; QEMU connects as the client
                       (`-chardev socket,...,path=` without server), so it lands in the backlog
                       and the connection never races QEMU's start

Files (paths here, read by the viewer core, usb.py and unit.py):
  RUN_DIR/restarting   exists from the SHUTDOWN of a restart until the new QEMU's monitor is up:
                       a front end that loses its display then waits for the next QEMU
  RUN_DIR/restarts     how many times this unit started QEMU again (0 at the unit's start)
  STATE/usb.json       the USB devices attached (usb.py), put back on every new QEMU: STATE is the
                       unit's RuntimeDirectory, the run directory without one (the setsid fallback)
  RUN_DIR/state/.fresh  with STATE=none, the run's one temporary state (fresh_state): unit.run makes
                       it empty at its start and passes it to every launch as FRESH_STATE, so the
                       disks and the EEPROM a guest restart leaves are the next QEMU's, as a set
                       keeps what it wrote across a reboot; the run's end removes it (unit.run, and
                       ExecStopPost's `post` after a SIGKILL). It is on the run directory's disk,
                       not in the RuntimeDirectory: that is a tmpfs, whose pages the unit's memory
                       limit would count against the VM
"""

import json
import os
import shutil
import socket
import threading

RESTARTING = "restarting"
RESTARTS = "restarts"
USB_RECORD = "usb.json"
SOCK = "lifecycle.sock"
# SHUTDOWN reasons that are a reset turned into an exit by -action reboot=shutdown
RESET_REASONS = ("guest-reset", "host-qmp-system-reset")


def fresh_state(env, top):
    """The run's temporary state directory (RUN_DIR/state/.fresh, RUN_DIR relative to the tree top
    as qemu.plan takes it) when env's STATE is none, else None."""
    if env.get("STATE") != "none":
        return None
    return os.path.join(top, env.get("RUN_DIR") or "build", "state", ".fresh")


def discard_fresh_state(env, top):
    """Remove the run's temporary state (fresh_state), if it has one; the path removed, else None."""
    path = fresh_state(env, top)
    if path is None or not os.path.lexists(path):
        return None
    shutil.rmtree(path, ignore_errors=True)
    return path


def state_dir(run_dir, runtime_dir=None):
    """Where usb.json lives: the unit's RuntimeDirectory, else the run directory."""
    return runtime_dir if runtime_dir and os.path.isdir(runtime_dir) else run_dir


def restarting(run_dir):
    return os.path.exists(os.path.join(run_dir, RESTARTING))


def restarts(run_dir):
    try:
        with open(os.path.join(run_dir, RESTARTS)) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _write(path, text):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def set_restarts(run_dir, n):
    _write(os.path.join(run_dir, RESTARTS), f"{n}\n")


def mark_restarting(run_dir, on):
    path = os.path.join(run_dir, RESTARTING)
    if on:
        _write(path, "")
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def read_usb(state):
    """usb.json's devices, in the order they were attached: [{id, kind, image?, readonly?, socket?}]."""
    try:
        with open(os.path.join(state, USB_RECORD)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return (
        [d for d in data if isinstance(d, dict) and d.get("id")] if isinstance(data, list) else []
    )


def write_usb(state, devices):
    path = os.path.join(state, USB_RECORD)
    if not devices:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    _write(path, json.dumps(devices))


def replay_commands(devices):
    """The QMP commands that put the recorded USB devices on a new QEMU, in their order: a stick's
    block node and usb-storage device, a usb-redir device's listening socket and device (its client,
    `io usb attach --device`'s, connects again by itself)."""
    from ..devices import usb

    cmds = []
    for d in devices:
        if d.get("kind") == "storage":
            cmds += [
                usb.blockdev_add(d["id"], d["image"], bool(d.get("readonly"))),
                usb.storage_add(d["id"]),
            ]
        elif d.get("socket"):
            cmds += [usb.chardev_add(d["id"], d["socket"]), usb.device_add(d["id"])]
    return cmds


class Monitor:
    """One QEMU's lifecycle monitor. After start(): reset is the SHUTDOWN reason if it was a restart
    (RESET_REASONS), disk_full the BLOCK_IO_ERROR data of a write that hit ENOSPC (QEMU was then
    told to quit), connected whether QEMU connected. on_reset() runs at a restart's SHUTDOWN, before
    QEMU exits; on_ready() once the monitor is up and the recorded USB devices (devices()) are back."""

    def __init__(self, path, say, devices=list, on_reset=None, on_ready=None):
        self.path, self.say, self.devices = path, say, devices
        self.on_reset, self.on_ready = on_reset, on_ready
        self.reset = None
        self.disk_full = None
        self.connected = False
        self.conn = None
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(1)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self, timeout=5):
        """After QEMU has exited: the reader's last events in, the socket gone."""
        try:
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()
        self.thread.join(timeout)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def _send(self, f, cmd):
        f.write(json.dumps(cmd) + "\n")
        f.flush()

    def _run(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return  # closed before QEMU connected: it did not start
        self.conn, self.connected = conn, True
        with conn, conn.makefile("rw") as f:
            try:
                f.readline()  # the greeting
                pending = [{"execute": "qmp_capabilities"}]
                devices = self.devices()
                pending += replay_commands(devices)
                for cmd in pending:
                    self._send(f, cmd)
                answers = 0
                ready = False
                for line in f:
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if "return" in msg or "error" in msg:
                        if "error" in msg and 0 < answers < len(pending):
                            cmd = pending[answers]
                            self.say(
                                f"beanjar: USB device back after the restart: {cmd['execute']}: "
                                f"{msg['error'].get('desc', msg['error'])}"
                            )
                        answers += 1
                        if answers == len(pending) and not ready:
                            ready = True
                            if devices:
                                self.say(f"beanjar: USB devices put back: {len(devices)}")
                            if self.on_ready:
                                self.on_ready()
                        continue
                    self._event(f, msg)
            except OSError:
                pass

    def _event(self, f, msg):
        name, data = msg.get("event"), msg.get("data") or {}
        if name == "SHUTDOWN" and data.get("reason") in RESET_REASONS:
            self.reset = data["reason"]
            if self.on_reset:
                self.on_reset()
        elif name == "BLOCK_IO_ERROR" and data.get("nospace") and data.get("action") == "stop":
            if self.disk_full is None:
                self.disk_full = data
                self.say(
                    f"beanjar: the host's disk is full: writing {data.get('device') or data.get('node-name')} "
                    f"failed ({data.get('reason', 'no space left on device')}); QEMU quits instead of "
                    "pausing the VM"
                )
                self._send(f, {"execute": "quit"})
