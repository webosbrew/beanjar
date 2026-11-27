#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The status bar's indicator state and tooltips (beanjar_ui/viewer/statusfmt.py), no GTK or display."""

import os
import sys
import json
import tempfile
from unittest.mock import patch

import pytest


for package in ("ui", "core"):
    sys.path.append(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "host", package)
    )
from beanjar_ui.viewer import stats, statusfmt as f  # noqa: E402


def test_statusfmt():

    # Reservation expectations use a known host policy rather than the developer's config.
    headroom_patch = patch.object(stats.capacity, "memory_headroom_mib", return_value=1024)
    headroom_patch.start()
    bad = []

    def check(cond, what):
        print(("ok   " if cond else "FAIL ") + what)
        if not cond:
            bad.append(what)

    def src(ok=True, moved=False, a=0.0, b=0.0, why="measuring"):
        s = stats.Source()
        s.ok, s.moved, s.a, s.b, s.why = ok, moved, a, b, why
        return s

    check(
        [f.rate(x) for x in (0, 12, 1500, 2.5e6, 3e9)]
        == ["0", "12B/s", "1.5K/s", "2.5M/s", "3.0G/s"],
        "rate units",
    )
    check(f.state(src(ok=False)) == f.OFF, "unavailable is off")
    check(f.state(src()) == f.IDLE, "no movement is idle")
    check(f.state(src(moved=True)) == f.ACTIVE, "movement is active")
    check(f.tooltip(stats.CPU, src(a=33.6)) == "CPU\n34% of one host CPU", "cpu tooltip")
    gpu = src(a=42, moved=True)
    gpu.gpu = {
        "enabled": True,
        "available": True,
        "driver": "v3d",
        "engines": {"render": 42.0, "bin": 3.0},
        "headline": 42.0,
        "resident_memory_kib": 317440,
        "total_memory_kib": 409600,
    }
    check(
        f.tooltip(stats.GPU, gpu) == "GPU (v3d): 42%\nrender: 42%\nbin: 3%\nMemory: 310 MiB",
        "gpu tooltip has headline, engines, driver and resident memory",
    )
    gpu.gpu = dict(gpu.gpu, engines={}, headline=None)
    check(f.tooltip(stats.GPU, gpu) == "GPU (v3d): measuring", "gpu tooltip before a second sample")
    gpu.unavailable("unused")
    gpu.gpu["enabled"] = True
    check(
        f.tooltip(stats.GPU, gpu) == "GPU: on, usage unavailable", "enabled GPU can be unmeasurable"
    )
    check(f.tooltip(stats.GPU, stats.Source()) == "GPU: off", "disabled GPU is off")
    gpu.gpu = dict(
        gpu.gpu, available=True, driver="v3d", engines={"render": 0.3, "bin": 0.0}, headline=0.3
    )
    first = f.tooltip(stats.GPU, gpu)
    gpu.gpu = dict(gpu.gpu, engines={"render": 0.0, "bin": 0.2}, headline=0.2)
    check(
        first == f.tooltip(stats.GPU, gpu) == "GPU (v3d): 0%\nbin: 0%\nrender: 0%",
        "idle gpu tooltip keeps its engines in one order from sample to sample",
    )

    # Memory: the guest's RAM (balloon statistics) and swap (webos.memory's lines) on the meter, and
    # the host's view of the VM in the tooltip, from a stand-in /proc and cgroup tree
    MIB = 1024 * 1024
    UNIT = "user.slice/user-1000.slice/user@1000.service/beanjar.slice/beanjar-vm-x-y.service"
    check(
        stats.reservation(["qemu", "-accel", "kvm", "-m", "3072"])
        == (4096 * 1024, "3072 MiB guest RAM, 1024 MiB headroom"),
        "KVM reservation is the guest RAM and the start's headroom, as the unit's MemoryHigh",
    )
    check(
        stats.reservation(["qemu", "-accel", "tcg,thread=multi,tb-size=256", "-m", "size=2G"])
        == ((2048 + 256 + 1024) * 1024, "2048 MiB guest RAM, 256 MiB TCG cache, 1024 MiB headroom"),
        "TCG reservation adds the translation cache, as the admission counts it",
    )
    check(stats.reservation(["qemu"]) == (None, ""), "no -m, no reservation")
    with patch.object(
        stats.capacity, "memory_headroom_mib", side_effect=stats.base.Error("bad config")
    ):
        check(
            stats.reservation(["qemu", "-accel", "kvm", "-m", "2560"]) == (None, ""),
            "invalid host policy leaves reservation unknown without stopping stats sampling",
        )
    with patch.object(stats.capacity, "memory_headroom_mib", return_value=896):
        check(
            stats.reservation(["qemu", "-accel", "kvm", "-m", "2560"])
            == (3456 * 1024, "2560 MiB guest RAM, 896 MiB headroom"),
            "custom headroom has no negative TCG cache in KVM tooltip",
        )
        check(
            stats.reservation(["qemu", "-accel", "tcg,tb-size=256", "-m", "2560"])
            == (3712 * 1024, "2560 MiB guest RAM, 256 MiB TCG cache, 896 MiB headroom"),
            "custom headroom preserves the actual TCG cache in tooltip",
        )
    budget = (4096 * 1024, "3072 MiB guest RAM, 1024 MiB headroom")

    UNSET = (1 << 64) - 1
    BALLOON = {
        "stats": {
            "stat-total-memory": 1990 * MIB,
            "stat-available-memory": 1178 * MIB,
            "stat-free-memory": 400 * MIB,
            "stat-swap-in": UNSET,
        },
        "last-update": 1000,
    }
    check(
        stats.guest_ram(BALLOON, 1002)
        == ({"total": 1990 * 1024, "available": 1178 * 1024, "used": 812 * 1024}, ""),
        "guest RAM in use is the balloon's total less its available memory",
    )
    check(
        stats.guest_ram(
            {
                "stats": dict(BALLOON["stats"], **{"stat-available-memory": UNSET}),
                "last-update": 1000,
            },
            1002,
        )[0]["used"]
        == 1590 * 1024,
        "without an available figure, the guest's free memory",
    )
    check(
        stats.guest_ram(dict(BALLOON, **{"last-update": 0}), 5)
        == (None, "waiting for the guest's balloon statistics"),
        "no statistics before the guest's first answer",
    )
    check(
        stats.guest_ram(BALLOON, 1030) == (None, "no statistics from the guest for 30 s"),
        "old statistics are not shown as current",
    )
    check(
        stats.guest_swap("swap total=614396 free=600000\nswap total=614396 free=602396")
        == {"total": 614396, "free": 602396, "used": 12000},
        "guest swap is the last whole line",
    )
    check(
        stats.guest_swap("p total=614396 free=600000") is None
        and stats.guest_swap("total=1 free=1") is None,
        "a line the ring cut at its start is no report",
    )

    def tree(root, unit=True, memory=True, swap=True, host_swap=6144):
        proc, cg = os.path.join(root, "proc"), os.path.join(root, "cgroup")
        os.makedirs(os.path.join(proc, "10"))
        os.makedirs(os.path.join(cg, UNIT))
        with open(os.path.join(proc, "10", "cgroup"), "w") as fh:
            fh.write(f"0::/{UNIT}\n" if unit else "0::/user.slice/session-2.scope\n")
        with open(os.path.join(proc, "10", "status"), "w") as fh:
            fh.write("Name:\tqemu\nVmRSS:\t 1884480 kB\nVmSwap:\t    2048 kB\n")
        with open(os.path.join(proc, "meminfo"), "w") as fh:
            fh.write(f"MemTotal: 8255488 kB\nSwapTotal: {host_swap * 1024} kB\n")
        files = {"cgroup.procs": "10\n"}
        if memory:
            files.update(
                {
                    "memory.current": f"{2157 * MIB}\n",
                    "memory.high": "max\n",
                    "memory.stat": f"anon {1514 * MIB}\nfile {348 * MIB}\nkernel 0\n",
                }
            )
            if swap:
                files.update({"memory.swap.current": f"{12 * MIB}\n", "memory.swap.max": "max\n"})
        for name, text in files.items():
            with open(os.path.join(cg, UNIT, name), "w") as fh:
                fh.write(text)
        return proc, cg

    def mem_src(host, ram=None, swap=None, swap_why="", ram_why="waiting"):
        s = stats.Source()
        if ram:
            s.ok, s.moved, s.a = True, True, ram["used"] / ram["total"] * 100
        else:
            s.unavailable(ram_why)
        s.memory = {"guest": ram, "guest_why": ram_why, "swap": swap, "swap_why": swap_why}
        s.memory["host"] = host
        return s

    RAM = stats.guest_ram(BALLOON, 1002)[0]
    SWAP = {"total": 614396, "free": 602108, "used": 12288}
    HOST_UNIT = (
        "Host: 2157 of 4096 MiB (53%), the VM's unit, against the start's reservation\n"
        "(3072 MiB guest RAM, 1024 MiB headroom)\nPage cache: 348 MiB of it\n"
        "Host swap: 12 of 6144 MiB (0%), the VM's unit, against the host's swap"
    )
    with tempfile.TemporaryDirectory() as root:
        proc, cg = tree(root)
        value = stats.memory_usage(10, budget, proc, cg)
        check(
            (value["ram"]["used"], value["ram"]["total"], value["ram"]["cache"])
            == (2157 * 1024, 4096 * 1024, 348 * 1024)
            and value["ram"]["source"] == "unit",
            "unit RAM is memory.current against the start's reservation, page cache from memory.stat",
        )
        check(
            f.tooltip(stats.MEM, mem_src(value, RAM, SWAP))
            == "Memory (guest)\nRAM: 812 of 1990 MiB (41%) in use\nSwap: 12 of 600 MiB (2%) in use\n"
            + HOST_UNIT,
            "memory tooltip: guest RAM and swap, then the host footprint and its scales",
        )
        check(
            f.tooltip(stats.MEM, mem_src(value, RAM, {"total": 0, "free": 0, "used": 0})).split(
                "\n"
            )[2]
            == "Swap: none",
            "a guest without swap says so",
        )
        check(
            f.tooltip(
                stats.MEM, mem_src(value, RAM, None, "no report from the guest for 6 s or more")
            ).split("\n")[2]
            == "Swap: unknown (no report from the guest for 6 s or more)",
            "a guest whose reports stopped has unknown swap, not the last figure",
        )
        check(
            f.tooltip(stats.MEM, mem_src(value, None, None, "QMP: x", "no balloon (QMP: x)"))
            == "Memory: no balloon (QMP: x)\nSwap: unknown (QMP: x)\n" + HOST_UNIT,
            "without guest statistics the meter is dim and the host footprint stays in the tooltip",
        )
        with open(os.path.join(cg, UNIT, "memory.high"), "w") as fh:
            fh.write(f"{4096 * MIB}\n")
        with open(os.path.join(cg, UNIT, "memory.swap.max"), "w") as fh:
            fh.write(f"{1024 * MIB}\n")
        value = stats.memory_usage(10, budget, proc, cg)
        check(
            value["ram"]["of"] == "its MemoryHigh"
            and value["swap"]["total"] == 1024 * 1024
            and value["swap"]["of"] == "its MemorySwapMax",
            "a finite memory.high and memory.swap.max are the unit's scales",
        )
    with tempfile.TemporaryDirectory() as root:
        value = stats.memory_usage(10, budget, *tree(root, swap=False))
        check(
            f.tooltip(stats.MEM, mem_src(value, RAM, SWAP)).endswith(
                "\nHost swap: not accounted for the VM's unit (no memory.swap.current)"
            ),
            "unaccounted host swap says so",
        )
    for kwargs in ({"memory": False}, {"unit": False}):  # cgroup_disable=memory, VM_SYSTEMD=0
        with tempfile.TemporaryDirectory() as root:
            value = stats.memory_usage(10, budget, *tree(root, **kwargs))
            check(
                (value["ram"]["used"], value["swap"]["used"], value["ram"]["source"])
                == (1884480, 2048, "qemu")
                and "\nHost: 1840 of 4096 MiB (45%), QEMU's resident memory, against"
                in f.tooltip(stats.MEM, mem_src(value, RAM, SWAP)),
                f"without the unit's memory controller ({kwargs}), QEMU's VmRSS and VmSwap",
            )
    with tempfile.TemporaryDirectory() as root:
        value = stats.memory_usage(10, budget, *tree(root, host_swap=0))
        check(
            f.tooltip(stats.MEM, mem_src(value, RAM, SWAP)).endswith(
                "\nHost swap: not in use (the host has no swap)"
            ),
            "a host without swap says so",
        )
    with tempfile.TemporaryDirectory() as root:
        value = stats.memory_usage(10, (None, ""), *tree(root, memory=False))
        check(
            "\nHost: no scale: the guest's RAM is unknown (no -m on QEMU's command line)\n"
            in f.tooltip(stats.MEM, mem_src(value, RAM, SWAP)),
            "host RAM without a scale says why",
        )

    check(f.tooltip(stats.DISK, src(a=1.2e6, b=0)) == "Disk\nRead 1.2M/s\nWrite 0", "disk tooltip")
    check(
        f.tooltip(stats.NET, src(a=120e3, b=8e3))
        == "Network (guest)\nDownload 120.0K/s\nUpload 8.0K/s",
        "net tooltip",
    )
    check(
        f.tooltip(stats.NET, src(ok=False, why="no host counters with mode=direct"))
        == "Network: no host counters with mode=direct",
        "unavailable tooltip carries the reason",
    )
    check(
        f.ICONS[stats.NET][f.OFF] != f.ICONS[stats.NET][f.ACTIVE], "network icon changes with state"
    )

    with tempfile.TemporaryDirectory() as directory:
        changes, standby = [], []
        sample = stats.Stats(directory, lambda: changes.append(True), standby.append)
        try:
            check(sample.power_state is None, "power is unknown before a standby report")
            for value in (False, True, False):
                sample._qmp_line(
                    json.dumps(
                        {"id": "chardev", "return": [{"label": "standby", "frontend-open": value}]}
                    )
                )
                check(
                    sample.power_state is value and sample.standby is value,
                    f"reported standby {value} updates power state",
                )
            check(
                standby == [True, False] and len(changes) == 3,
                "standby and wake callbacks preserve input routing and refresh the LED",
            )
            sample._qmp_line('{"id":"chardev","return":[]}')
            check(sample.power_state is None, "missing standby port is unknown, not on")
            sample._qmp_line('{"id":"chardev","return":[{"label":"standby","frontend-open":true}]}')
            sample._qmp_line('{"id":"chardev","error":{"desc":"unavailable"}}')
            check(
                sample.power_state is None and sample.standby,
                "failed power query clears the LED without changing known wake routing",
            )
            sample._qmp_reset("disconnected")
            check(
                sample.power_state is None and not sample.standby,
                "QMP disconnect clears reported power state",
            )

            # webos.memory's ring: whole lines only, a line split across two reads joined
            sample._qmp_line(json.dumps({"id": "memory", "return": "ee=1\nswap total=614396 fr"}))
            check(
                sample.guest_swap is None
                and sample.swap_why == "waiting for the guest's first report",
                "a read without a whole line is no report",
            )
            sample._qmp_line(json.dumps({"id": "memory", "return": "ee=602108\nswap total=6"}))
            check(
                sample.guest_swap == {"total": 614396, "free": 602108, "used": 12288},
                "a line split across two reads is read whole",
            )
            sample._qmp_line(json.dumps({"id": "balloon", "error": {"desc": "no balloon"}}))
            sample._qmp_line(json.dumps({"id": "memory", "error": {"desc": "no chardev"}}))
            check(
                sample.guest_ram is None
                and sample.guest_ram_why == "no balloon (QMP: no balloon)"
                and sample.src[stats.DISK].why != "QMP: no balloon",
                "a VM without the balloon or the port leaves the disk numbers alone",
            )
            sample.guest_swap = {"total": 614396, "free": 602108, "used": 12288}
            sample.swap_at = stats.time.monotonic() - stats.SWAP_STALE_S - 1
            sample.pid, sample._qmp, sample.guest_ram = (
                os.getpid(),
                "ready",
                stats.guest_ram(BALLOON, BALLOON["last-update"] + 1)[0],
            )
            with patch.object(stats, "memory_usage", return_value=None):
                sample._memory(stats.time.monotonic())
            memory = sample.src[stats.MEM]
            check(
                memory.ok and memory.bars == (memory.a,) and memory.memory["swap"] is None,
                "stale guest swap is unknown and draws no swap bar",
            )
            sample.swap_at = stats.time.monotonic()
            with patch.object(stats, "memory_usage", return_value=None):
                sample._memory(stats.time.monotonic())
            check(len(memory.bars) == 2, "a guest with swap draws a second bar")
            sample.guest_swap = {"total": 0, "free": 0, "used": 0}
            with patch.object(stats, "memory_usage", return_value=None):
                sample._memory(stats.time.monotonic())
            check(len(memory.bars) == 1, "a guest without swap draws the RAM bar alone")
            sample._qmp, sample.pid = "idle", 0

            # Direct/off backends must never borrow counters from an unrelated host tap0.
            host_tap = "tap0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0"
            with (
                patch.object(sample, "_netdev", return_value="user,id=net0"),
                patch.object(stats, "_read", return_value=host_tap) as read,
            ):
                sample._net(1.0)
                network = sample.src[stats.NET]
                check(
                    not read.called and not network.ok and f.state(network) == f.DIRECT,
                    "direct networking is distinct without invented or unrelated traffic counters",
                )
                check(
                    f.ICONS[stats.NET][f.DIRECT] != f.ICONS[stats.NET][f.OFF],
                    "direct network icon differs from offline",
                )
                check(
                    f.tooltip(stats.NET, network)
                    == "Network: Direct networking\nTraffic rates unavailable",
                    "direct network tooltip explains available networking without rates",
                )
            with (
                patch.object(sample, "_netdev", return_value="hubport,id=net0,hubid=0"),
                patch.object(stats, "_read", return_value=host_tap) as read,
            ):
                sample._net(2.0)
                check(
                    not read.called and f.state(network) == f.OFF and network.network_mode is None,
                    "disabled network is offline even if a host tap exists",
                )
            with (
                patch.object(sample, "_netdev", return_value="tap,id=net0,ifname=tap0"),
                patch.object(stats, "_read", return_value=host_tap),
            ):
                sample._net(3.0)
                host_tap = host_tap.replace("1000", "1200").replace("2000", "2400")
                with patch.object(stats, "_read", return_value=host_tap):
                    sample._net(4.0)
                check(
                    f.state(network) == f.ACTIVE and (network.a, network.b) == (400, 200),
                    "TAP mode keeps measured guest traffic rates",
                )
            network.unavailable("VM stopped")
            check(
                f.state(network) == f.OFF, "stopped VM cannot retain the direct network indicator"
            )

            # Use the host's real /proc stat layout. A QEMU probe verified CPU n/TCG names
            # and that thread utime, stime and start time use the same fields.
            template = open("/proc/self/stat").read()
            prefix, fields = (
                template[: template.rindex(")") + 2],
                template[template.rindex(")") + 2 :].split(),
            )

            def procstat(ticks, started=1):
                values = fields.copy()
                values[11], values[12], values[19] = str(ticks), "0", str(started)
                return prefix + " ".join(values)

            sample.pid = os.getpid()
            base = f"/proc/{sample.pid}"
            hz = sample.hz
            files = {
                base + "/stat": procstat(10 * hz),
                base + "/task/201/comm": "CPU 1/TCG\n",
                base + "/task/201/stat": procstat(3 * hz),
                base + "/task/202/comm": "CPU 0/KVM\n",
                base + "/task/202/stat": procstat(2 * hz),
                base + "/task/203/comm": "IO worker\n",
            }
            with (
                patch.object(stats.os, "listdir", return_value=["201", "203", "202"]),
                patch.object(stats, "_read", side_effect=files.get),
            ):
                sample._cpu(1.0)
                cpu = sample.src[stats.CPU]
                check(
                    cpu.cores == ((0, None), (1, None)),
                    "first sample preserves actual vCPU order without inventing usage",
                )
                files[base + "/stat"] = procstat(12 * hz)
                files[base + "/task/201/stat"] = procstat(3 * hz + hz // 2)
                files[base + "/task/202/stat"] = procstat(3 * hz)
                sample._cpu(2.0)
                check(
                    cpu.cores == ((0, 100.0), (1, (hz // 2) / hz * 100)) and cpu.a == 200.0,
                    "vCPU bars measure each thread, while total includes other QEMU work",
                )
                check(
                    "vCPU 0: 100%" in f.tooltip(stats.CPU, cpu),
                    "CPU tooltip includes individually measured cores",
                )
                files[base + "/task/202/stat"] = procstat(100 * hz, started=2)
                sample._cpu(3.0)
                check(cpu.cores[0] == (0, None), "replaced vCPU thread starts a fresh baseline")
                del files[base + "/task/202/stat"]
                sample._cpu(4.0)
                check(cpu.cores == ((1, 0.0),), "vanished thread contributes no stale bar")
                files[base + "/stat"] = None
                sample._cpu(5.0)
                check(
                    not cpu.ok and not cpu.cores and not sample.vcpu_prev,
                    "lost process clears per-core usage and baselines",
                )
        finally:
            sample.close()
    headroom_patch.stop()
    assert not bad
