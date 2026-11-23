# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The micom's infrared receiver on the host: ir.sock, where the front ends and tests write
the remote's and the set's own key edges (include/webos-ir.h), relayed to QEMU's webos.ir port.

    python3 -m beanjar.ir_relay PID LISTEN_FD QEMU_SOCK QMP_SOCK

The micom model is in the guest (lgemu's i2c.c) and sleeps with it while the SoC is suspended
(Quick Start+, docs/dev/architecture/suspend.md): the IR pump that feeds it is frozen. A TV's micom
stays on and wakes the SoC on the remote's POWER key and on the set's own key. This is that
always-on part, for every client of ir.sock alike. With the guest in standby (poweroff.qmp_status: the
standby chardev's frontend open), a key-down of POWER (LG's custom code, 0x08) or of a local key
pulses the micom's wake line, QMP system_powerdown, instead of reaching the port; any other key-down
is dropped, as the micom ignores it; the key-up of a dropped key-down is dropped too. Records left
in the port would reach the set after the next wake: on W22O queue a POWER and a local key sent
during a suspend were handed to tvservice after the wake. Awake, every record passes unchanged.

The launcher listens on ir.sock (LISTEN_FD) before QEMU starts; QEMU's chardev for the port is
QEMU_SOCK (qemu_path). One client at a time, as QEMU's chardev took them: each gets its own
connection to QEMU, closed when the client leaves, which the guest sees as the port's hang-up (a
held key comes up). It ends when PID (the launcher's, QEMU's after its exec) does.
"""

import os
import select
import socket
import sys
import time

QEMU_NAME = "ir-qemu.sock"
IR_MAGIC, EXT_MAGIC, LOCAL_MAGIC = 0x49, 0x45, 0x4B  # WEBOS_IR_MAGIC, _IR_EXT_, _LOCAL_
CUSTOM_LG, POWER = 0x04, 0x08  # WEBOS_IR_CUSTOM_LG, LG's KEY_POWER
CHUNK = 4096


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
                say(f"standby: key 0x{rec[3]:02x} wakes the SoC (system_powerdown)")
                self.wake()
            else:
                say(f"standby: key 0x{rec[3]:02x} dropped, as the micom ignores it")
            self.dropped.add(key)
            return b""
        if key in self.dropped:
            self.dropped.discard(key)
            return b""
        return rec


def qmp_standby(qmp_sock):
    from . import poweroff

    return poweroff.qmp_status(qmp_sock) == "standby"


def qmp_wake(qmp_sock):
    from . import launch

    try:
        reply = launch.qmp(qmp_sock, '{"execute": "system_powerdown"}')
    except launch.Error as e:
        say(f"QMP system_powerdown: {e}")
        return
    if "error" in reply:
        say(f"QMP system_powerdown: {reply['error'].get('desc', reply['error'])}")


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


def serve(conn, qemu_sock, f, running):
    """Relays one client until it or QEMU leaves."""
    q = connect(qemu_sock, running)
    if q is None:
        return
    with q:
        while running():
            ready = select.select([conn, q], [], [], 1.0)[0]
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
    if len(argv) != 4:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    pid, fd, qemu_sock, qmp_sock = int(argv[0]), int(argv[1]), argv[2], argv[3]
    listener = socket.socket(fileno=fd)
    running = lambda: alive(pid)
    while running():
        if not select.select([listener], [], [], 1.0)[0]:
            continue
        conn, _ = listener.accept()
        with conn:
            try:
                f = Filter(lambda: qmp_standby(qmp_sock), lambda: qmp_wake(qmp_sock))
                serve(conn, qemu_sock, f, running)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
