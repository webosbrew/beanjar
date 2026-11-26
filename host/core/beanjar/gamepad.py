# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A virtual gamepad in a running VM: `beanjar io usb gamepad attach|press|detach NAME`.

The pad is a wired Xbox 360 controller (045e:028e) with that controller's own descriptors, made
by beanjar-virtpad (host/helpers/virtpad) on the VM's host: `attach` adds a usb-redir device
(usb.attach: run/usb-ID.sock) and starts the program against its socket, in a session of its own,
so it stays after the command; the guest's own xpad driver binds the pad. The program listens on
run/usb-ID-pad.sock for presses (`press`: each control pressed and released in turn) and is
recorded in the unit's RuntimeDirectory (gamepad-ID.json: its pid and sockets). When the guest
restarts, QEMU does too and puts the device back (lifecycle.py); the program connects again to
the same socket. `detach` (or `io usb detach NAME ID`) ends the program, which unplugs the pad,
and removes the device. When the VM stops, the program ends on its own after RETRY seconds.

A press reaches the guest only while its driver polls the pad, which xpad does while the pad's
input device is open (an app or a reader on /dev/input/event*): with nobody reading, the presses
go nowhere, as on a real pad, and `press` says so (polled false, no reports)."""

import json
import os
import signal
import socket
import subprocess
import time

from .base import Error

PROGRAM = "beanjar-virtpad"
DEVICE = "045e:028e"
PRODUCT = "Controller"  # its iProduct; xpad names it "Microsoft X-Box 360 pad"
# the controls press takes (host/helpers/virtpad/pad.cpp)
CONTROLS = (
    "a", "b", "x", "y", "lb", "rb", "lt", "rt", "back", "start", "guide", "ls", "rs",
    "up", "down", "left", "right",
)  # fmt: skip
HOLD = 0.1  # seconds each control is held, and released after
HOLD_MAX = 60.0
RETRY = 180  # seconds the program waits for QEMU's socket again (a restart) before it ends
ATTACH_WAIT = 30.0  # seconds attach waits for the guest to have the pad
STOP_WAIT = 5.0
POLL = 0.25
RECORD = "gamepad-{}.json"


def program():
    from . import paths

    return paths.TREE.virtpad()


def check_controls(names):
    """names as press takes them, lower case; Error for one that is not a control."""
    out = [n.lower() for n in names]
    bad = [n for n in out if n not in CONTROLS]
    if bad:
        raise Error(f"no such control: {', '.join(bad)} (one of {', '.join(CONTROLS)})")
    if not out:
        raise Error("no control to press")
    return out


def control_path(vm, id_):
    return os.path.join(vm.run_dir, f"usb-{id_}-pad.sock")


def log_path(vm, id_):
    return os.path.join(vm.run_dir, f"usb-{id_}-pad.log")


def _record_path(vm, id_):
    from . import usb

    return os.path.join(usb.record_dir(vm), RECORD.format(id_))


def _alive(pid):
    """Whether pid is a running beanjar-virtpad (not a reused pid)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv0 = f.read().split(b"\0", 1)[0]
    except OSError:
        return False
    return os.path.basename(argv0).decode(errors="replace") == PROGRAM


def records(vm):
    """{id: record} of vm's pads whose program runs; records of ended ones are removed."""
    from . import usb

    d = usb.record_dir(vm)
    out = {}
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return out
    for name in names:
        if not (name.startswith("gamepad-") and name.endswith(".json")):
            continue
        path = os.path.join(d, name)
        try:
            with open(path) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            rec = None
        if isinstance(rec, dict) and _alive(rec.get("pid") or 0):
            out[rec["id"]] = rec
        else:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    return out


def find(vm, id_=None):
    """vm's pad id_, or its only pad: its record."""
    pads = records(vm)
    if id_ is not None:
        if id_ not in pads:
            raise Error(f"VM {vm.name} has no gamepad {id_} (beanjar io usb list)")
        return pads[id_]
    if not pads:
        raise Error(f"VM {vm.name} has no gamepad: beanjar io usb gamepad attach {vm.name}")
    if len(pads) > 1:
        raise Error(f"VM {vm.name} has gamepads {', '.join(sorted(pads))}: say which with --id")
    return next(iter(pads.values()))


def ask(path, line, timeout):
    """The program's one-line answer to line on its control socket: the text after "ok"; Error
    for its "error TEXT"."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(line.encode() + b"\n")
        answer = s.makefile("r").readline().strip()
    except OSError as e:
        raise Error(f"the gamepad's program does not answer on {path}: {e.strerror or e}") from None
    finally:
        s.close()
    if answer.startswith("ok"):
        return answer[2:].strip()
    raise Error(answer.removeprefix("error ").strip() or "the gamepad's program closed")


def state(path, timeout=5.0):
    return json.loads(ask(path, "state", timeout))


def _tail(path):
    try:
        with open(path, errors="replace") as f:
            lines = [x.strip() for x in f.read().splitlines() if x.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""


def _stop(pid, wait=STOP_WAIT):
    """SIGTERM to the program (it unplugs the pad first), SIGKILL if it outlives wait."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + wait
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stop(vm, id_):
    """Ends the program of vm's pad id_, if one runs (usb.detach calls it for any device)."""
    rec = records(vm).get(id_)
    if rec:
        _stop(rec["pid"])
    try:
        os.unlink(_record_path(vm, id_))
    except FileNotFoundError:
        pass


def attach(vm, id_=None, spawn=subprocess.Popen, wait=ATTACH_WAIT):
    """Plugs a pad into vm: {name, id, socket, control, device, product}."""
    from . import usb

    prog = program()
    if not os.access(prog, os.X_OK):
        raise Error(
            f"no {PROGRAM} at {prog}: build it (meson compile -C build/host; it needs "
            "libusbredirparser-dev) or install beanjar"
        )
    data = usb.attach(vm, id_)
    id_ = data["id"]
    ctl = control_path(vm, id_)
    if len(os.fsencode(ctl)) > usb.SUN_PATH_MAX:
        usb.detach(vm, id_)
        raise Error(f"{ctl}: longer than a unix socket path may be ({usb.SUN_PATH_MAX} bytes)")
    log = log_path(vm, id_)
    proc = None
    try:
        with open(log, "ab") as out:
            proc = spawn(
                [prog, "--unix", data["socket"], "--control", ctl, "--retry", str(RETRY)],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                close_fds=True,
                start_new_session=True,
            )
        rec = {"id": id_, "pid": proc.pid, "socket": data["socket"], "control": ctl}
        path = _record_path(vm, id_)
        with open(path + ".tmp", "w") as f:
            json.dump(rec, f)
        os.replace(path + ".tmp", path)
        deadline = time.monotonic() + wait
        while True:
            if proc.poll() is not None:
                raise Error(
                    f"{PROGRAM} exited (status {proc.returncode}) before {vm.name} had the pad"
                    + (f": {t}" if (t := _tail(log)) else "")
                    + f" (log: {log})"
                )
            if any(d["id"] == id_ and d["attached"] for d in usb.devices(vm)):
                break
            if time.monotonic() > deadline:
                raise Error(f"{vm.name} did not get the pad in {wait:g} s (log: {log})")
            time.sleep(POLL)
    except BaseException:
        if proc is not None and proc.poll() is None:
            _stop(proc.pid)
        try:
            os.unlink(_record_path(vm, id_))
        except FileNotFoundError:
            pass
        try:
            usb.detach(vm, id_)
        except Error:
            pass
        raise
    return {"name": vm.name, "id": id_, "socket": data["socket"], "control": ctl,
            "device": DEVICE, "product": PRODUCT}  # fmt: skip


def press(vm, controls, hold=HOLD, id_=None):
    """Presses and releases each of controls in turn on vm's pad, each held hold seconds and
    released as long: {name, id, controls, hold, reports, polled}."""
    controls = check_controls(controls)
    if not 0.001 <= hold <= HOLD_MAX:
        raise Error(f"--hold: from 1ms to {HOLD_MAX:g}s")
    rec = find(vm, id_)
    ms = max(1, round(hold * 1000))
    answer = ask(rec["control"], f"press {ms} {' '.join(controls)}", 2 * len(controls) * hold + 10)
    reports, polled = answer.split()
    return {"name": vm.name, "id": rec["id"], "controls": controls, "hold": ms / 1000,
            "reports": int(reports), "polled": polled == "1"}  # fmt: skip


def detach(vm, id_=None):
    """Unplugs vm's pad id_ (or its only one): the program ends, then the device goes."""
    from . import usb

    rec = find(vm, id_)
    usb.detach(vm, rec["id"])  # ends the program first (stop)
    return {"name": vm.name, "id": rec["id"]}
