#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The capture relay on capture.sock (host/core/beanjar/capture_relay.py): the guest's requests go
to the front end that is there, else to an on-demand sink, which warms up, stops when idle and
gives way to a front end. A fake QEMU socket, fake front ends and a fake sink (a Python process
that answers like the headless viewer); no VM. `pytest tests/unit/manager/test_capture_relay.py`."""

import errno
import os
import socket
import struct
import sys
import threading
import time

import pytest

from beanjar import capture_relay as cr

H = struct.Struct("<6Iii")
W, HT = 4, 2

# The fake sink: connects to capture.sock, says hello, answers "no picture" WARM times and then a
# picture of the value FILL; ends on SIGTERM. argv: capture.sock WARM FILL
SINK = r"""
import os, signal, socket, struct, sys, time
H = struct.Struct("<6Iii")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
path, warm, fill = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(path)
s.sendall(H.pack(0x50414357, 0, 0, 0, 0, 0, 0, 0))
while True:
    h = b""
    while len(h) < 32:
        d = s.recv(32 - len(h))
        if not d:
            sys.exit(0)
        h += d
    m, seq, mode, w, ht, _, _, _ = H.unpack(h)
    if warm > 0:
        warm -= 1
        s.sendall(H.pack(m, seq, mode, w, ht, 0, -61, 0))
    else:
        s.sendall(H.pack(m, seq, mode, w, ht, w * ht * 4, 0, 0) + bytes([fill]) * (w * ht * 4))
"""


def recv_exact(s, n):
    out = b""
    while len(out) < n:
        d = s.recv(n - len(out))
        if not d:
            raise EOFError
        out += d
    return out


def read_msg(s):
    """(header, payload) of the next message on s."""
    h = H.unpack(recv_exact(s, 32))
    return h, recv_exact(s, h[5]) if h[5] else b""


def request(seq, mode=3, w=W, h=HT):
    return H.pack(cr.MAGIC, seq, mode, w, h, 0, 0, 0)


class Rig:
    """A relay on a temporary capture.sock and a fake QEMU, running in a thread."""

    def __init__(self, tmp, sink=None):
        self.dir = str(tmp)
        self.sock = os.path.join(self.dir, "capture.sock")
        self.qemu_listen = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.qemu_listen.bind(cr.qemu_path(self.sock))
        self.qemu_listen.listen(1)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.sock)
        listener.listen(8)
        argv = [sys.executable, "-c", SINK, self.sock, *map(str, sink)] if sink else None
        self.on = True
        self.relay = cr.Relay(listener, cr.qemu_path(self.sock), self.dir, argv, lambda: self.on)
        self.thread = threading.Thread(target=self.relay.run, daemon=True)
        self.thread.start()
        self.qemu_listen.settimeout(5)
        self.qemu, _ = self.qemu_listen.accept()
        self.qemu.settimeout(5)
        self.hello()

    def hello(self):
        h, _ = read_msg(self.qemu)
        assert h[1] == 0 and h[6] == 0  # the relay's own hello to the guest

    def ask(self, seq, **kw):
        """A request; its answer, skipping hellos."""
        self.qemu.sendall(request(seq, **kw))
        while True:
            h, payload = read_msg(self.qemu)
            if h[1]:
                return h, payload

    def front_end(self, fill=7, answer=True):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.sock)
        s.settimeout(5)
        s.sendall(H.pack(cr.MAGIC, 0, 0, 0, 0, 0, 0, 0))
        if answer:

            def serve():
                try:
                    while True:
                        h, _ = read_msg(s)
                        m, seq, mode, w, ht = h[:5]
                        n = w * ht * 4
                        s.sendall(H.pack(m, seq, mode, w, ht, n, 0, 0) + bytes([fill]) * n)
                except (EOFError, OSError):
                    pass

            threading.Thread(target=serve, daemon=True).start()
        return s

    def sink_pid(self):
        try:
            with open(os.path.join(self.dir, cr.SINK_PIDFILE)) as f:
                return int(f.read())
        except FileNotFoundError:
            return None

    def close(self):
        self.on = False
        self.thread.join(10)
        self.qemu.close()
        self.qemu_listen.close()


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(cr, "IDLE_S", 1.0)
    monkeypatch.setattr(cr, "REPLY_S", 1.0)


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    with open(f"/proc/{pid}/stat") as f:
        return f.read().rsplit(")", 1)[1].split()[0] == "Z"


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_front_end_answers(tmp_path, fast):
    """A front end's answer, pixels and all, reaches the guest; no sink is started."""
    rig = Rig(tmp_path, sink=(0, 9))
    try:
        fe = rig.front_end(fill=7)
        h, payload = rig.ask(1)
        assert h[1] == 1 and h[6] == 0 and payload == bytes([7]) * (W * HT * 4)
        assert rig.sink_pid() is None
        fe.close()
    finally:
        rig.close()


def test_no_sink_no_front_end(tmp_path, fast):
    """Without a sink (no dbus display) and no front end: -ENODEV at once."""
    rig = Rig(tmp_path)
    try:
        t = time.monotonic()
        h, _ = rig.ask(5)
        assert h[1] == 5 and h[6] == -errno.ENODEV and h[5] == 0
        assert time.monotonic() - t < 0.5
    finally:
        rig.close()


def test_sink_on_demand_warms_and_idles(tmp_path, fast):
    """No front end: the sink starts, its first "no picture" answers are asked again, the guest
    gets the picture; idle, the sink is stopped and its pidfile removed."""
    rig = Rig(tmp_path, sink=(3, 9))
    try:
        h, payload = rig.ask(1)
        assert h[6] == 0 and payload == bytes([9]) * (W * HT * 4)
        pid = rig.sink_pid()
        assert pid and not gone(pid)
        h, _ = rig.ask(2)
        assert h[6] == 0
        assert rig.sink_pid() == pid  # the same sink serves the next request
        assert wait_for(lambda: rig.sink_pid() is None, 4.0)
        assert wait_for(lambda: gone(pid))
        # a later request starts a new one
        h, _ = rig.ask(3)
        assert h[6] == 0 and rig.sink_pid() not in (None, pid)
    finally:
        rig.close()
    assert rig.sink_pid() is None  # the relay's end stops its sink


def test_front_end_takes_over(tmp_path, fast):
    """A front end connecting while the sink runs stops the sink and gets the next requests; with
    it gone again, the sink comes back."""
    rig = Rig(tmp_path, sink=(0, 9))
    try:
        h, payload = rig.ask(1)
        assert payload[:1] == b"\x09"
        pid = rig.sink_pid()
        fe = rig.front_end(fill=7)
        assert wait_for(lambda: gone(pid))
        h, payload = rig.ask(2)
        assert h[6] == 0 and payload[:1] == b"\x07"
        assert rig.sink_pid() is None
        fe.close()
        time.sleep(0.2)
        h, payload = rig.ask(3)
        assert h[6] == 0 and payload[:1] == b"\x09"
    finally:
        rig.close()


def test_silent_provider_times_out(tmp_path, fast):
    """A front end that never answers: -ETIMEDOUT within REPLY_S, its late answer dropped."""
    rig = Rig(tmp_path, sink=(0, 9))
    try:
        fe = rig.front_end(answer=False)
        t = time.monotonic()
        h, _ = rig.ask(4)
        assert h[1] == 4 and h[6] == -errno.ETIMEDOUT
        assert 0.9 < time.monotonic() - t < 2.0
        req, _ = read_msg(fe)
        fe.sendall(H.pack(*req[:5], W * HT * 4, 0, 0) + bytes(W * HT * 4))  # late
        h, _ = rig.ask(5)  # the late answer was not taken for this one
        assert h[1] == 5
        assert rig.sink_pid() is None  # a front end is there: no sink
        fe.close()
    finally:
        rig.close()


def test_front_end_starting_holds_the_sink(tmp_path, fast):
    """beanjar-vnc started but not yet connected (vnc.pid): no sink; the request waits for it."""
    rig = Rig(tmp_path, sink=(0, 9))
    with open(os.path.join(rig.dir, "vnc.pid"), "w") as f:
        f.write(f"{os.getpid()}\n")
    real = cr.base.proc_alive
    cr.base.proc_alive = lambda pid, *names: pid == os.getpid()
    try:
        threading.Timer(0.3, rig.front_end, kwargs={"fill": 5}).start()
        h, payload = rig.ask(1)
        assert h[6] == 0 and payload[:1] == b"\x05"
        assert rig.sink_pid() is None
    finally:
        cr.base.proc_alive = real
        rig.close()


def test_invalid_answer_drops_the_front_end(tmp_path, fast):
    rig = Rig(tmp_path, sink=(0, 9))
    try:
        fe = rig.front_end(answer=False)
        time.sleep(0.1)
        fe.sendall(H.pack(0x1234, 0, 0, 0, 0, 0, 0, 0))
        assert wait_for(lambda: not rig.relay.providers)
        h, payload = rig.ask(1)  # the sink serves it
        assert h[6] == 0 and payload[:1] == b"\x09"
    finally:
        rig.close()


def test_hold_keeps_the_sink_off(tmp_path, fast):
    """A beanjar command starting a front end (capture-sink.hold names its live pid): no sink;
    with the hold gone, the sink serves again."""
    rig = Rig(tmp_path, sink=(0, 9))
    hold = os.path.join(rig.dir, cr.SINK_HOLD)
    with open(hold, "w") as f:
        f.write(f"{os.getpid()}\n")
    try:
        h, _ = rig.ask(1)
        assert h[6] == -errno.ENODEV and rig.sink_pid() is None
        os.unlink(hold)
        h, payload = rig.ask(2)
        assert h[6] == 0 and payload[:1] == b"\x09"
    finally:
        rig.close()


def test_sink_stopped_from_outside_comes_back(tmp_path, fast):
    """A sink stopped by a beanjar command (SIGTERM, status 0) young: no back-off, the next request
    starts another."""
    rig = Rig(tmp_path, sink=(0, 9))
    try:
        rig.ask(1)
        pid = rig.sink_pid()
        os.kill(pid, 15)
        assert wait_for(lambda: rig.sink_pid() is None)
        h, payload = rig.ask(2)
        assert h[6] == 0 and rig.sink_pid() not in (None, pid)
    finally:
        rig.close()


def test_no_busy_wait(tmp_path, fast, monkeypatch):
    """While a request waits for a slow front end, the relay sleeps in select: a few dozen steps in
    REPLY_S (1 s here), not thousands."""
    steps = []
    real = cr.Relay.step
    monkeypatch.setattr(cr.Relay, "step", lambda self, *a: (steps.append(1), real(self, *a))[1])
    rig = Rig(tmp_path, sink=(0, 9))
    try:
        fe = rig.front_end(answer=False)
        time.sleep(0.1)
        n = len(steps)
        h, _ = rig.ask(1)
        assert h[6] == -errno.ETIMEDOUT
        assert len(steps) - n < 50
        fe.close()
    finally:
        rig.close()
