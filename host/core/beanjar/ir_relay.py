# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The micom's infrared receiver on the host: ir.sock, where the front ends and tests write
the remote's and the set's own key edges (include/webos-ir.h), relayed to QEMU's webos.ir port.

    python3 -m beanjar.ir_relay PID LISTEN_FD QEMU_SOCK QMP_SOCK MICOM_STATUS

The micom model is in the guest (lgemu's i2c.c) and sleeps with it while the SoC is suspended
(Quick Start+, docs/dev/architecture/suspend.md): the IR pump that feeds it is frozen. A TV's micom
stays on and wakes the SoC on the remote's POWER key and on the set's own key. This is that
always-on part, for every client of ir.sock alike. With the guest in standby (poweroff.qmp_status: the
standby chardev's frontend open), a key-down of POWER (LG's custom code, 0x08) or of a local key
pulses the micom's wake line, QMP system_powerdown, instead of reaching the port; any other key-down
is dropped, as the micom ignores it; the key-up of a dropped key-down is dropped too. Records left
in the port would reach the set after the next wake: on W22O queue a POWER and a local key sent
during a suspend were handed to tvservice after the wake. Awake, every record passes unchanged.

It also powers the SoC down, as a TV's micom does in standby (PowerDown): once the guest's kernel
reports its suspend has reached s2idle (lgemu.ko holds webos.suspended open, poweroff.qmp_state),
the VM is stopped with QMP stop, and its clocks with it, so CLOCK_MONOTONIC leaves the standby out
as on a TV; the wake is system_powerdown, then cont. It looks at QMP every POLL s only while the
micom is in standby (MICOM_STATUS, the run's micom-status file, says standby=1), and stops the VM
once per time the SoC is down: not again after a wake, which the guest may take a moment to show.

The launcher listens on ir.sock (LISTEN_FD) before QEMU starts; QEMU's chardev for the port is
QEMU_SOCK (qemu_path). One client at a time, as QEMU's chardev took them: each gets its own
connection to QEMU, closed when the client leaves, which the guest sees as the port's hang-up (a
held key comes up). It ends when PID (the launcher's, QEMU's after its exec) does.
"""

import json
import os
import select
import socket
import sys
import time

QEMU_NAME = "ir-qemu.sock"
IR_MAGIC, EXT_MAGIC, LOCAL_MAGIC = 0x49, 0x45, 0x4B  # WEBOS_IR_MAGIC, _IR_EXT_, _LOCAL_
CUSTOM_LG, POWER = 0x04, 0x08  # WEBOS_IR_CUSTOM_LG, LG's KEY_POWER
CHUNK = 4096
POLL = 0.5  # s between QMP looks while the micom is in standby
VGPU_CHARDEV = "vgpu"  # qemu.py's chardev of vhost-user-gpu's socket


def qemu_path(ir_sock):
    """QEMU's socket for the webos.ir port, beside ir.sock."""
    return os.path.join(os.path.dirname(ir_sock), QEMU_NAME)


def say(line):
    print(f"ir_relay: {line}", flush=True)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wakes(rec):
    """Whether the micom wakes the SoC on this key: the remote's POWER, the set's own key."""
    return rec[0] == LOCAL_MAGIC or (rec[0] == IR_MAGIC and rec[2] == CUSTOM_LG and rec[3] == POWER)


class Filter:
    """One client's stream: feed() returns what passes to QEMU. standby() tells whether the guest is
    suspended, wake() pulses the wake line."""

    def __init__(self, standby, wake):
        self.standby, self.wake = standby, wake
        self.buf = b""
        self.dropped = set()  # (magic, custom, code) of key-downs dropped in standby

    def feed(self, data):
        self.buf += data
        out = bytearray()
        while self.buf:
            if self.buf[0] not in (IR_MAGIC, EXT_MAGIC, LOCAL_MAGIC):
                out += self.buf[:1]  # out of step: the guest skips to the next record too
                self.buf = self.buf[1:]
                continue
            if len(self.buf) < 4:
                break
            rec, self.buf = self.buf[:4], self.buf[4:]
            out += self.record(rec)
        return bytes(out)

    def record(self, rec):
        key = (rec[0], rec[2], rec[3])
        if rec[1]:
            if not self.standby():
                return rec
            if wakes(rec):
                say(f"standby: key 0x{rec[3]:02x} wakes the SoC")
                self.wake()
            else:
                say(f"standby: key 0x{rec[3]:02x} dropped, as the micom ignores it")
            self.dropped.add(key)
            return b""
        if key in self.dropped:
            self.dropped.discard(key)
            return b""
        return rec


class PowerDown:
    """The SoC powered down in standby: the VM stopped while the guest's SoC is down, continued at
    the wake (docs/dev/architecture/suspend.md). state() is poweroff.qmp_state's (run state,
    standby, suspended) or None, command(name) runs a QMP command and says whether it did,
    standby() whether the micom is in standby (micom-status), which gates the QMP looks; tick() is
    called often, and looks every POLL s."""

    def __init__(self, state, command, standby=lambda: True, clock=time.monotonic, stoppable=None):
        self.state, self.command, self.standby, self.clock = state, command, standby, clock
        self.stoppable = stoppable or (lambda: True)
        self.stopped = False  # this relay stopped the VM and has not continued it
        self.handled = False  # this time the SoC is down is done with: stopped once, or woken
        self.last = None

    def tick(self):
        now = self.clock()
        if self.last is not None and now - self.last < POLL:
            return
        self.last = now
        if not self.stopped and not self.standby():
            self.handled = False
            return
        st = self.state()
        if st is None:
            return  # QMP busy: the next look
        status, _, suspended = st
        if self.stopped:
            if status == "running":
                self.stopped = False  # continued by someone else
            return
        if not suspended:
            self.handled = False
            return
        if status == "running" and not self.handled:
            self.handled = True
            if not self.stoppable():
                say("the SoC is down: VM left running (its GPU is vhost-user-gpu)")
            elif self.command("stop"):
                self.stopped = True
                say("the SoC is down: VM stopped")

    def wake(self):
        """The micom's wake line: system_powerdown, then cont if this relay stopped the VM; the SoC
        is not stopped again until it has been up."""
        self.handled = True
        self.command("system_powerdown")
        if self.stopped and self.command("cont"):
            self.stopped = False
            say("the SoC wakes: VM continued")


def qmp_command(qmp_sock, name):
    from . import launch

    try:
        reply = launch.qmp(qmp_sock, json.dumps({"execute": name}))
    except launch.Error as e:
        say(f"QMP {name}: {e}")
        return False
    if "error" in reply:
        say(f"QMP {name}: {reply['error'].get('desc', reply['error'])}")
        return False
    return True


def power_down(qmp_sock, micom_status):
    from . import poweroff

    vgpu = []  # once known: whether the GPU is vhost-user-gpu (its chardev vgpu)

    def stoppable():
        # QEMU 10.0.2's vhost-user-gpu backend loses the display at a stop and cont, and has
        # aborted after a few (docs/dev/architecture/gpu-process.md, "Limits")
        if not vgpu:
            labels = poweroff.qmp_chardevs(qmp_sock)
            if labels is None:
                return False  # QMP busy: not this time
            vgpu.append(VGPU_CHARDEV in labels)
        return not vgpu[0]

    return PowerDown(
        lambda: poweroff.qmp_state(qmp_sock),
        lambda name: qmp_command(qmp_sock, name),
        lambda: poweroff.micom_standby(micom_status),
        stoppable=stoppable,
    )


def qmp_standby(qmp_sock):
    from . import poweroff

    return poweroff.qmp_status(qmp_sock) == "standby"


def connect(path, running):
    """A connection to QEMU's socket, waiting for QEMU to listen; None if PID ends first."""
    while running():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(path)
            return s
        except OSError:
            s.close()
            time.sleep(0.2)
    return None


def serve(conn, qemu_sock, f, running, tick=lambda: None):
    """Relays one client until it or QEMU leaves; tick() between."""
    q = connect(qemu_sock, running)
    if q is None:
        return
    with q:
        while running():
            ready = select.select([conn, q], [], [], POLL)[0]
            tick()
            if conn in ready:
                data = conn.recv(CHUNK)
                if not data:
                    return
                out = f.feed(data)
                if out:
                    q.sendall(out)
            if q in ready:
                data = q.recv(CHUNK)
                if not data:
                    return
                conn.sendall(data)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    pid, fd, qemu_sock, qmp_sock, micom_status = int(argv[0]), int(argv[1]), *argv[2:]
    listener = socket.socket(fileno=fd)
    running = lambda: alive(pid)
    down = power_down(qmp_sock, micom_status)
    while running():
        ready = select.select([listener], [], [], POLL)[0]
        down.tick()
        if not ready:
            continue
        conn, _ = listener.accept()
        with conn:
            try:
                f = Filter(lambda: qmp_standby(qmp_sock), down.wake)
                serve(conn, qemu_sock, f, running, down.tick)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
