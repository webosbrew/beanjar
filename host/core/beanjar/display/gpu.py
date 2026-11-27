# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's host GPU use, from the kernel's DRM fdinfo (Documentation/gpu/drm-usage-stats.rst).

A VM's GPU is on when its QEMU gives a guest with the gl layer host GL: -display dbus,gl=on or
egl-headless, with a render node. Its use is read from the /dev/dri descriptors of the VM's processes
(its unit's control group, else QEMU alone): each client (drm-client-id; QEMU holds one client in two
fds) counts once, an engine's use is its busy time over the wall time between two samples, and the
headline is the busiest engine. A driver without engine counters (WSL's d3d12, through /dev/dxg)
leaves the use unavailable. The viewer's own context is in no VM's unit, and never counted.
"""

import os
import re
import time

SAMPLE_INTERVAL = 0.1  # info and list: the wait between their two samples
_ENGINE = re.compile(r"drm-engine-(?!capacity-)(.+)")
_CAPACITY = re.compile(r"drm-engine-capacity-(.+)")
_MEMORY = re.compile(r"drm-(total|resident)-(.+)")  # per region: v3d's "memory", amdgpu's "vram"…
_UNITS = {"": 1 / 1024, "KiB": 1, "MiB": 1024}  # to KiB; no unit is bytes


def _read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r") as f:
            return f.read()
    except OSError:
        return None


def enabled(pid, layers, proc_root="/proc"):
    """QEMU pid gives a guest with the gl layer (layers; None: a virtio-gpu-gl device) host GL."""
    data = _read(os.path.join(proc_root, str(pid), "cmdline"), binary=True) if pid else None
    argv = data.decode(errors="replace").split("\0") if data else []
    if not (
        "gl" in layers if layers is not None else any(a.startswith("virtio-gpu-gl") for a in argv)
    ):
        return False
    for option, value in zip(argv, argv[1:]):
        if option == "-display":
            backend, *options = value.split(",")
            return any(o.startswith("rendernode=") for o in options) and (
                backend == "egl-headless" or (backend == "dbus" and "gl=on" in options)
            )
    return False


def unit_cgroup(pid, proc_root="/proc", cgroup_root="/sys/fs/cgroup"):
    """The control group directory of QEMU pid's VM unit (a beanjar.slice service), or None outside
    one (VM_SYSTEMD=0: QEMU shares a session's group with whatever else runs there)."""
    text = _read(os.path.join(proc_root, str(pid), "cgroup")) or ""
    path = next((line[3:] for line in text.splitlines() if line.startswith("0::")), "")
    if "/beanjar.slice/" in path and path.endswith(".service"):
        return os.path.join(cgroup_root, path.lstrip("/"))
    return None


def processes(pid, proc_root="/proc", cgroup_root="/sys/fs/cgroup"):
    """The processes of QEMU pid's VM unit (unit_cgroup), or pid alone outside one."""
    group = unit_cgroup(pid, proc_root, cgroup_root)
    members = _read(os.path.join(group, "cgroup.procs")) if group else None
    return [int(w) for w in (members or str(pid)).split() if w.isdigit()]


def _amount(value):
    number, _, unit = value.partition(" ")
    try:
        return int(number), unit.strip()
    except ValueError:
        return None, None


def snapshot(pid, proc_root="/proc", cgroup_root="/sys/fs/cgroup"):
    """The DRM clients of QEMU pid's VM: {"driver", "clients": {id: {engine: busy ns}},
    "capacity": {engine: units}, "total" and "resident": KiB or None}."""
    seen = {}
    for process in processes(pid, proc_root, cgroup_root):
        fd_dir = os.path.join(proc_root, str(process), "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if not os.readlink(os.path.join(fd_dir, fd)).startswith("/dev/dri/"):
                    continue
            except OSError:
                continue
            text = _read(os.path.join(proc_root, str(process), "fdinfo", fd)) or ""
            keys = {
                k: v.strip()
                for k, sep, v in (line.partition(":") for line in text.splitlines())
                if sep and k.startswith("drm-")
            }
            if keys:  # a client without an id (an old kernel) is its fd's own
                seen.setdefault(keys.get("drm-client-id") or f"{process}/{fd}", keys)
    result = {"driver": "", "clients": {}, "capacity": {}, "total": None, "resident": None}
    for client, keys in seen.items():
        result["driver"] = result["driver"] or keys.get("drm-driver", "")
        engines = result["clients"][client] = {}
        for key, value in keys.items():
            amount, unit = _amount(value)
            if amount is None:
                continue
            if (m := _ENGINE.fullmatch(key)) and unit == "ns":
                engines[m[1]] = amount
            elif m := _CAPACITY.fullmatch(key):
                result["capacity"][m[1]] = max(amount, 1)
            elif (m := _MEMORY.fullmatch(key)) and unit in _UNITS:
                result[m[1]] = (result[m[1]] or 0) + round(amount * _UNITS[unit])
    return result


def status_off():
    return {
        "enabled": False,
        "available": False,  # the driver counts engine time
        "driver": "",
        "engines": {},  # engine: percent of its capacity
        "headline": None,  # the busiest engine's percent; None: unavailable, or the first sample
        "resident_memory_kib": None,
        "total_memory_kib": None,
    }


def delta(before, after, seconds):
    """The status from two snapshots seconds apart (before None: after is the first)."""
    result = status_off()
    result.update(
        enabled=True,
        driver=after["driver"],
        available=any(after["clients"].values()),
        resident_memory_kib=after["resident"],
        total_memory_kib=after["total"],
    )
    if before is None or seconds <= 0 or not result["available"]:
        return result
    busy = {}
    for client, engines in after["clients"].items():
        old = before["clients"].get(client, {})  # a new client: opened since, from 0
        for name, ns in engines.items():
            busy[name] = busy.get(name, 0) + max(ns - old.get(name, 0), 0)
    result["engines"] = {
        name: ns / 1e9 / seconds * 100 / after["capacity"].get(name, 1) for name, ns in busy.items()
    }
    result["headline"] = max(result["engines"].values())
    return result


class Sampler:
    """One QEMU process's GPU status, from its previous sample to the current one."""

    def __init__(self, pid, layers, proc_root="/proc", cgroup_root="/sys/fs/cgroup"):
        self.pid, self.proc_root, self.cgroup_root = pid, proc_root, cgroup_root
        self.on = enabled(pid, layers, proc_root)  # QEMU's command line does not change
        self.previous = self.at = None

    def sample(self, now=None):
        if not self.on:
            return status_off()
        now = time.monotonic() if now is None else now
        current = snapshot(self.pid, self.proc_root, self.cgroup_root)
        result = delta(self.previous, current, now - (self.at or now))
        self.previous, self.at = current, now
        return result


def measure(targets, interval=SAMPLE_INTERVAL):
    """The status of each (QEMU pid or None, layers), from two samples interval seconds apart:
    one wait for them all, and none without a VM whose GPU is on."""
    samplers = [Sampler(pid, layers) for pid, layers in targets]
    first = [s.sample() for s in samplers]
    if not any(v["available"] for v in first):
        return first
    time.sleep(interval)
    return [s.sample() if v["available"] else v for s, v in zip(samplers, first)]


def memory(value):
    """A status's memory: resident, else total."""
    kib = value["resident_memory_kib"]
    kib = value["total_memory_kib"] if kib is None else kib
    if kib is None:
        return ""
    return f"{kib / 1024:.0f} MiB" if kib >= 1024 else f"{kib} KiB"


def engines(value):
    """A status's engines as (name, percent), busiest first by the percent shown (rounded), then
    by name: engines that read alike keep their order from one sample to the next."""
    return sorted(value["engines"].items(), key=lambda e: (-round(e[1]), e[0]))


def label(value):
    """One line: off; on (usage unavailable); on (v3d) render 12% bin 2%, 310 MiB; with every
    engine idle (0% shown) on (v3d) 0%, naming none."""
    if not value["enabled"]:
        return "off"
    if not value["available"]:
        return "on (usage unavailable)"
    words = [f"on ({value['driver']})" if value["driver"] else "on"]
    if value["headline"] is None:
        words.append("measuring")
    else:
        busy = [(n, p) for n, p in engines(value) if round(p)]  # the idle engines left out
        words += [f"{name} {percent:.0f}%" for name, percent in busy] or ["0%"]
    amount = memory(value)
    return " ".join(words) + (f", {amount}" if amount else "")


def short(value):
    """list's column: the headline percent, on (no usage, or not yet measured) or off."""
    if value["headline"] is not None:
        return f"{value['headline']:.0f}%"
    return "on" if value["enabled"] else "off"
