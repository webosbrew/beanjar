#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar/display/gpu.py against a stand-in /proc and cgroup tree: no GPU, no VM."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from beanjar.display import gpu

UNIT = "/user.slice/user-1000.slice/user@1000.service/beanjar.slice/beanjar-vm-x-y.service"
# v3d on the Pi 5 (kernel 6.18): QEMU's client in two fds, and a second, idle one
V3D = """drm-driver:\tv3d
drm-client-id:\t{id}
drm-engine-bin:\t{bin} ns
drm-engine-render:\t{render} ns
drm-engine-tfu:\t0 ns
drm-engine-csd:\t0 ns
drm-engine-cache_clean:\t0 ns
drm-engine-cpu:\t0 ns
drm-total-memory:\t{total} KiB
drm-shared-memory:\t0
drm-resident-memory:\t{resident} KiB
"""


class Tree:
    """A /proc and /sys/fs/cgroup of processes in a control group, their fds and fdinfo."""

    def __init__(self, root, cgroup=UNIT):
        self.proc, self.cg = os.path.join(root, "proc"), os.path.join(root, "cgroup")
        self.cgroup = cgroup
        self.members = []

    def process(self, pid, fds, cmdline=b"qemu-system-aarch64\0"):
        for d in ("fd", "fdinfo"):
            os.makedirs(os.path.join(self.proc, str(pid), d), exist_ok=True)
        Path(self.proc, str(pid), "cgroup").write_text(f"0::{self.cgroup}\n")
        Path(self.proc, str(pid), "cmdline").write_bytes(cmdline)
        for fd, text in fds.items():
            Path(self.proc, str(pid), "fd", str(fd)).touch()
            Path(self.proc, str(pid), "fdinfo", str(fd)).write_text(text)
        self.members += [pid] if pid not in self.members else []
        os.makedirs(os.path.join(self.cg, self.cgroup.lstrip("/")), exist_ok=True)
        Path(self.cg, self.cgroup.lstrip("/"), "cgroup.procs").write_text(
            "".join(f"{p}\n" for p in self.members)
        )

    def snapshot(self, pid):
        with patch.object(gpu.os, "readlink", return_value="/dev/dri/renderD128"):
            return gpu.snapshot(pid, self.proc, self.cg)


def v3d(id, bin=0, render=0, total=0, resident=0):
    return V3D.format(id=id, bin=bin, render=render, total=total, resident=resident)


def test_v3d_clients_and_delta(tmp_path):
    t = Tree(str(tmp_path))
    qemu = v3d(8444, 20_000_000, 100_000_000, 409600, 317440)
    t.process(10, {3: qemu, 4: qemu, 5: v3d(8445)})
    before = t.snapshot(10)
    assert sorted(before["clients"]) == ["8444", "8445"]
    assert (before["resident"], before["total"]) == (317440, 409600)
    qemu = v3d(8444, 30_000_000, 150_000_000, 409600, 317440)
    t.process(10, {3: qemu, 4: qemu})
    value = gpu.delta(before, t.snapshot(10), 0.1)
    assert value["engines"]["render"] == pytest.approx(50.0)
    assert value["engines"]["bin"] == pytest.approx(10.0)
    assert value["headline"] == pytest.approx(50.0)
    assert gpu.label(value) == "on (v3d) render 50% bin 10%, 310 MiB"
    assert gpu.short(value) == "50%"


def test_unit_processes_only(tmp_path):
    t = Tree(str(tmp_path / "unit"))
    t.process(10, {3: v3d(1, render=1)})
    t.process(11, {3: v3d(2, render=1)})  # a helper in the VM's unit
    assert sorted(t.snapshot(10)["clients"]) == ["1", "2"]
    t = Tree(
        str(tmp_path / "session"), "/user.slice/user-1000.slice/session-2.scope"
    )  # VM_SYSTEMD=0
    t.process(10, {3: v3d(1, render=1)})
    t.process(11, {3: v3d(2, render=1)})  # say, the viewer in the same session
    assert list(t.snapshot(10)["clients"]) == ["1"]


def test_counters_and_regions(tmp_path):
    before = {
        "driver": "i915",
        "clients": {"1": {"render": 0}, "2": {"render": 5}},
        "capacity": {"render": 2},
        "total": None,
        "resident": None,
    }
    after = {
        "driver": "i915",
        "clients": {"1": {"render": 100_000_000}, "3": {"render": 100_000_000}},
        "capacity": {"render": 2},
        "total": 2048,
        "resident": None,
    }
    # a closed client is gone, a new one counts from 0; capacity 2 halves the percentage
    value = gpu.delta(before, after, 0.1)
    assert value["headline"] == pytest.approx(100.0)
    assert gpu.label(value) == "on (i915) render 100%, 2 MiB"
    first = gpu.delta(None, after, 0)
    assert gpu.label(first) == "on (i915) measuring, 2 MiB"
    assert gpu.short(first) == "on"
    t = Tree(str(tmp_path))
    t.process(
        10,
        {
            3: "drm-driver:\tamdgpu\ndrm-client-id:\t7\n"
            "drm-engine-gfx:\t5 ns\ndrm-engine-capacity-gfx:\t1\n"
            "drm-total-vram:\t4 MiB\ndrm-total-gtt:\t1048576\n"
            "drm-resident-vram:\t1024 KiB\n"
        },
    )
    s = t.snapshot(10)
    assert (s["clients"], s["capacity"]) == ({"7": {"gfx": 5}}, {"gfx": 1})
    assert (s["total"], s["resident"]) == (5120, 1024)


def test_idle_label_is_stable():
    value = gpu.status_off()
    value.update(enabled=True, available=True, driver="v3d", resident_memory_kib=317440)
    # every engine shows 0%: no engine is named, whichever is a hair busier this sample
    for engines in ({"render": 0.3, "bin": 0.0}, {"render": 0.0, "bin": 0.2}, {"render": 0.0}):
        value.update(engines=engines, headline=max(engines.values()))
        assert gpu.label(value) == "on (v3d) 0%, 310 MiB"
        assert gpu.short(value) == "0%"
        assert [n for n, _ in gpu.engines(value)] == sorted(engines)  # by name
    value.update(engines={"render": 0.4, "bin": 1.6}, headline=1.6)
    assert gpu.label(value) == "on (v3d) bin 2%, 310 MiB"


def test_unit_cgroup(tmp_path):
    t = Tree(str(tmp_path / "unit"))
    t.process(10, {})
    group = os.path.join(t.cg, UNIT.lstrip("/"))
    assert gpu.unit_cgroup(10, t.proc, t.cg) == group
    t = Tree(str(tmp_path / "session"), "/user.slice/user-1000.slice/session-2.scope")
    t.process(10, {})
    assert gpu.unit_cgroup(10, t.proc, t.cg) is None


def test_unavailable_and_off():
    d3d12 = {"driver": "", "clients": {}, "capacity": {}, "total": None, "resident": None}
    value = gpu.delta(d3d12, d3d12, 0.1)
    assert value["enabled"]
    assert gpu.label(value) == "on (usage unavailable)"
    assert gpu.short(value) == "on"
    assert gpu.label(gpu.status_off()) == "off"
    assert gpu.short(gpu.status_off()) == "off"


def test_enabled(tmp_path):
    proc = str(tmp_path)
    os.makedirs(os.path.join(proc, "12"))
    cmdline = Path(proc, "12", "cmdline")
    cmdline.write_bytes(
        b"qemu\0-device\0virtio-gpu-gl-pci\0-display\0dbus,gl=on,rendernode=/dev/dri/renderD128\0"
    )
    assert gpu.enabled(12, ["normal", "gl"], proc)
    assert gpu.enabled(12, None, proc)  # no layers: the device tells
    assert not gpu.enabled(12, ["normal"], proc)
    cmdline.write_bytes(b"qemu\0-display\0egl-headless,rendernode=surfaceless\0")
    assert gpu.enabled(12, ["gl"], proc)
    cmdline.write_bytes(b"qemu\0-display\0none\0")
    assert not gpu.enabled(12, ["gl"], proc)
    assert not gpu.enabled(None, ["gl"], proc)


def test_measure_waits_only_for_counters():
    with patch.object(gpu.time, "sleep") as sleep:
        assert gpu.measure([(None, None), (None, ["gl"])]) == [gpu.status_off()] * 2
        sleep.assert_not_called()
