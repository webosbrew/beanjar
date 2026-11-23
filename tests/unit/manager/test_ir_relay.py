#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The micom's IR receiver on ir.sock (host/core/beanjar/ir_relay.py): awake, records pass to
QEMU's socket unchanged; in standby POWER and the local key pulse the wake line and every key-down
and its key-up are dropped. A fake QEMU socket and a fake standby; no VM.
`pytest tests/unit/manager/test_ir_relay.py`."""

import os
import socket
import threading

import pytest

from beanjar import ir_relay


def ir(down, code):
    return bytes((0x49, int(down), 0x04, code))


def local(down, code=0xEA):
    return bytes((0x4B, int(down), 0, code))


class Standby:
    def __init__(self):
        self.standby = False
        self.wakes = 0
        self.f = ir_relay.Filter(lambda: self.standby, self.wake)

    def wake(self):
        self.wakes += 1


@pytest.fixture
def st():
    return Standby()


def test_awake_passes_everything(st):
    data = ir(1, 0x08) + ir(0, 0x08) + local(1) + local(0) + ir(1, 0x7C) + ir(0, 0x7C)
    assert st.f.feed(data) == data
    assert st.wakes == 0


def ext(down, n):
    return bytes((0x45, int(down), 0x04, n))


def test_extended_records(st):
    """A Japanese remote's extended code: passed awake; in standby dropped with its key-up, no wake
    (0x08 there is N, not POWER)."""
    data = ext(1, 0x30) + ext(0, 0x30)
    assert st.f.feed(data[:5]) + st.f.feed(data[5:]) == data
    st.standby = True
    assert st.f.feed(ext(1, 0x08)) == b""
    st.standby = False
    assert st.f.feed(ext(0, 0x08)) == b""
    assert st.wakes == 0


def test_split_records(st):
    data = ir(1, 0x7C) + ir(0, 0x7C)
    assert st.f.feed(data[:3]) + st.f.feed(data[3:6]) + st.f.feed(data[6:]) == data


def test_standby_power_and_local_key_wake(st):
    st.standby = True
    assert st.f.feed(ir(1, 0x08)) == b""
    assert st.wakes == 1
    st.standby = False  # awake by the key-up: it is the dropped down's
    assert st.f.feed(ir(0, 0x08)) == b""
    st.standby = True
    assert st.f.feed(local(1) + local(0)) == b""
    assert st.wakes == 2


def test_standby_other_keys_dropped(st):
    st.standby = True
    assert st.f.feed(ir(1, 0x02) + ir(0, 0x02)) == b""
    assert st.f.feed(bytes((0x49, 1, 0x05, 0x08))) == b""  # another custom code
    assert st.wakes == 0


def test_up_of_a_key_down_before_standby_passes(st):
    assert st.f.feed(ir(1, 0x7C)) == ir(1, 0x7C)
    st.standby = True
    assert st.f.feed(ir(0, 0x7C)) == ir(0, 0x7C)


def test_out_of_step_bytes_pass(st):
    assert st.f.feed(b"\x00" + ir(1, 0x7C)) == b"\x00" + ir(1, 0x7C)


def test_relays_to_qemu_socket(tmp_path):
    d = str(tmp_path)
    ir_sock = os.path.join(d, "ir.sock")
    qemu = socket.socket(socket.AF_UNIX)
    qemu.bind(ir_relay.qemu_path(ir_sock))
    qemu.listen(1)
    client, server = socket.socketpair()
    standby = [False]
    wakes = []
    f = ir_relay.Filter(lambda: standby[0], lambda: wakes.append(1))
    stop = threading.Event()
    t = threading.Thread(
        target=ir_relay.serve,
        args=(server, ir_relay.qemu_path(ir_sock), f, lambda: not stop.is_set()),
    )
    t.start()
    conn, _ = qemu.accept()
    conn.settimeout(5)
    client.sendall(ir(1, 0x7C) + ir(0, 0x7C))
    got = b""
    while len(got) < 8:
        got += conn.recv(64)
    assert got == ir(1, 0x7C) + ir(0, 0x7C)
    standby[0] = True
    client.sendall(ir(1, 0x08) + ir(0, 0x08) + local(1))
    client.close()  # the client leaves: so does the relay's connection to QEMU
    assert conn.recv(64) == b""
    t.join(5)
    assert not t.is_alive()
    assert len(wakes) == 2
    conn.close()
    qemu.close()
    server.close()
