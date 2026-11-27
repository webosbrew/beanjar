#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The micom's IR receiver on ir.sock (host/core/beanjar/devices/ir_relay.py): awake, records pass to
QEMU's socket unchanged; in standby POWER and the local key pulse the wake line and every key-down
and its key-up are dropped. A fake QEMU socket and a fake standby; no VM.
`pytest tests/unit/manager/test_ir_relay.py`."""

import os
import socket
import threading

import pytest
from beanjar.devices import ir_relay


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


class Vm:
    """A fake VM for PowerDown: its run state, the guest's standby and SoC-down ports, the
    micom-status standby, and the QMP commands it got."""

    def __init__(self):
        self.status, self.standby, self.suspended, self.micom = "running", False, False, False
        self.commands = []
        self.busy = False  # QMP held by another client
        self.t = 0.0
        self.down = ir_relay.PowerDown(self.state, self.command, lambda: self.micom, self.clock)

    def clock(self):
        return self.t

    def state(self):
        return None if self.busy else (self.status, self.standby, self.suspended)

    def command(self, name):
        if self.busy:
            return False
        self.commands.append(name)
        if name == "stop":
            self.status = "paused"
        elif name == "cont":
            self.status = "running"
        return True

    def tick(self):
        self.t += ir_relay.POLL
        self.down.tick()

    def suspend(self):
        """The guest's way down: the micom's standby, then its SoC down."""
        self.micom = self.standby = True
        self.tick()
        self.suspended = True
        self.tick()

    def resume(self):
        self.suspended = False
        self.tick()
        self.micom = self.standby = False
        self.tick()


@pytest.fixture
def vm():
    return Vm()


def test_power_down_stops_once_the_soc_is_down(vm):
    vm.micom = vm.standby = True
    vm.tick()
    assert vm.commands == []  # in standby, but the guest is not down yet
    vm.suspended = True
    vm.tick()
    assert vm.commands == ["stop"] and vm.down.stopped
    vm.tick()
    assert vm.commands == ["stop"]


def test_power_down_no_qmp_while_awake(vm):
    asked = []
    vm.down.state = lambda: asked.append(1)
    for _ in range(5):
        vm.tick()
    assert asked == []


def test_power_down_looks_every_poll(vm):
    vm.micom = vm.standby = vm.suspended = True
    vm.down.tick()
    vm.status = "running"  # someone continued it
    vm.down.stopped = False
    vm.down.handled = False
    vm.down.tick()  # at the same time: no second look
    assert vm.commands == ["stop"]


def test_wake_continues_what_it_stopped(vm):
    vm.suspend()
    vm.down.wake()
    assert vm.commands == ["stop", "system_powerdown", "cont"]
    assert vm.status == "running" and not vm.down.stopped
    vm.tick()  # the guest has not shown the wake yet: not stopped again
    assert vm.commands == ["stop", "system_powerdown", "cont"]
    vm.resume()
    vm.suspend()  # the next standby is stopped again
    assert vm.commands[-1] == "stop"


def test_wake_before_the_stop_keeps_it_running(vm):
    """A key while the guest goes down: the wake, and no stop for that time down."""
    vm.micom = vm.standby = True
    vm.tick()
    vm.down.wake()
    vm.suspended = True
    vm.tick()
    assert vm.commands == ["system_powerdown"]


def test_a_vm_paused_otherwise_is_left_paused(vm):
    vm.status = "paused"
    vm.suspend()
    vm.down.wake()
    assert vm.commands == ["system_powerdown"]
    assert vm.status == "paused"


def test_continued_by_someone_else(vm):
    vm.suspend()
    vm.status = "running"  # a QMP cont of its own
    vm.tick()
    assert not vm.down.stopped
    vm.down.wake()
    assert vm.commands == ["stop", "system_powerdown"]


def test_qmp_busy_retries(vm):
    vm.micom = vm.standby = vm.suspended = True
    vm.busy = True
    vm.tick()
    assert vm.commands == []
    vm.busy = False
    vm.tick()
    assert vm.commands == ["stop"]


def test_cont_failed_is_tried_again(vm):
    vm.suspend()
    vm.busy = True
    vm.down.wake()
    assert vm.down.stopped  # QMP did not take it
    vm.busy = False
    vm.down.wake()
    assert vm.commands == ["stop", "system_powerdown", "cont"]


def test_status_reads_standby_while_stopped():
    from beanjar.runtime import poweroff

    assert poweroff.status_of(("running", True, False)) == "standby"
    assert poweroff.status_of(("running", True, True)) == "standby"
    assert poweroff.status_of(("paused", True, True)) == "standby"
    assert poweroff.status_of(("paused", True, False)) == "paused"  # paused for another reason
    assert poweroff.status_of(("paused", False, False)) == "paused"
    assert poweroff.status_of(("running", False, False)) == "running"
    assert poweroff.status_of(None) is None


def test_micom_standby(tmp_path):
    from beanjar.runtime import poweroff

    p = tmp_path / "micom-status"
    assert not poweroff.micom_standby(str(p))
    p.write_text("standby=0 power=3 ready=1\nstandby=1 power=2 ready=1\n")
    assert poweroff.micom_standby(str(p))
    p.write_text("standby=1 power=2 ready=1\nstandby=0 power=3 ready=1\n")
    assert not poweroff.micom_standby(str(p))
