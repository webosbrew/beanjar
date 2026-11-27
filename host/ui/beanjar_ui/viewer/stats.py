# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The status bar's numbers, refreshed once a second.

- CPU: QEMU's utime+stime from /proc/<pid>/stat over wall time (% of one host CPU, as top); the
  vCPUs are the "CPU n/KVM|TCG" threads (-name debug-threads=on), sampled individually for the meter.
- GPU: the busiest DRM engine of the VM's host GL, from the fdinfo of its unit's processes (the
  core's beanjar/display/gpu.py, shared with `beanjar show`); unavailable without engine counters (WSL).
- Memory: the guest's RAM and swap for the meter, the VM's host footprint for the tooltip.
  Guest RAM: QMP qom-get of the balloon's guest-stats (/machine/peripheral/balloon, polled by
  QEMU every qemu.BALLOON_STATS_S), used = stat-total-memory - stat-available-memory (MemTotal -
  MemAvailable; stat-free-memory where the guest has no available figure), guest_ram. Guest swap:
  the last whole "swap total=<kB> free=<kB>" line the initramfs's virtio-meminfo writes every 2 s
  to the webos.memory port, which QEMU keeps in the ring buffer "memory" that QMP ringbuf-read
  drains (guest_swap); unknown when no line came for SWAP_STALE_S. Host footprint (memory_usage):
  in its unit (gpu.unit_cgroup) with the memory controller, memory.current against memory.high
  (the unit's MemoryHigh, which the admission reserves: capacity.reservation_mib), page cache included
  and shown, and memory.swap.current. Without (VM_SYSTEMD=0, or a kernel with
  cgroup_disable=memory, as the Raspberry Pi's): QEMU's VmRSS and VmSwap, as capacity.claim_mib. Host
  RAM without a finite memory.high is against what the start reserved (capacity.reservation_mib:
  QEMU's -m, TCG's cache and the headroom, as claim_mib), host swap against memory.swap.max or the
  host's SwapTotal.
- Net: the tap0 line (or -netdev's ifname=) of /proc/<pid>/net/dev, QEMU's network namespace
  under mode=mitm, shown from the guest's side (tap0's transmit is the guest's download);
  mode=direct (user) and link=off (hubport) have no host counters.
- Disk: QMP query-blockstats on the viewer's own QMP socket, summed over the drives.
- Standby: the guest is suspended (Quick Start+, or the guest suspended manually)
  while its webos.standby port is open, QMP query-chardev's frontend-open of chardev "standby"
  (include/webos-ir.h); power_state is None until reported, then a standby bool.
  on_standby(bool) hears each change. The keys still go to ir.sock then, whose relay wakes the
  guest (beanjar's ir_relay).

- Micom: the last line of micom-status (the guest's micom state at each change: its standby,
  power state, whether webOS's power stack has started (ready) and front LED latches,
  include/webos-ir.h), parse_micom's dict, or None without one;
  front_led() maps it to the LED colour shown on the TV key's power icon.

pid from qemu.pid, QMP from viewer-qmp.sock, both next to the D-Bus socket. A missing source is
unavailable with a reason (the tooltip).
"""

import json
import os
import re
import time
import tomllib

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from beanjar import base  # noqa: E402
from beanjar.vm import library  # noqa: E402
from beanjar.runtime import capacity  # noqa: E402
from beanjar.display import gpu  # noqa: E402

# the directory the core package beanjar is in (host/core in a checkout), for python3 -m beanjar
CORE = os.path.dirname(os.path.dirname(os.path.abspath(library.__file__)))

CPU, GPU, MEM, DISK, NET = range(5)
NAMES = ("CPU", "GPU", "Memory", "Disk", "Net")
QMP_RETRY = 2.0
BALLOON = "/machine/peripheral/balloon"  # qemu.py's virtio-balloon-pci,id=balloon
BALLOON_STALE_S = 10.0  # guest RAM is unknown when the balloon's statistics are older
SWAP_STALE_S = 6.0  # guest swap is unknown when virtio-meminfo wrote no line for as long
STANDBY_LIGHT = 0x6E  # Set_PowerIndicator(0, v): 6E 01 <v> 01
WARM_STANDBY = 2  # the micom's power state (read 0xB1) in warm standby


class Source:
    def __init__(self):
        self.ok = False
        self.moved = False
        # CPU, GPU, memory: percent; Disk: read, write B/s; Net: guest down, up B/s
        self.a = self.b = 0.0
        self.why = "measuring"
        self.network_mode = None  # direct networking is available without traffic counters
        self.cores = ()  # CPU: (vCPU index, percent or None until measured)
        self.gpu = gpu.status_off()
        self.bars = ()  # memory: the meter's bars (guest RAM, and guest swap where it has any)
        # memory: {"guest": guest_ram or None, "guest_why", "swap": guest_swap or None,
        # "swap_why", "host": memory_usage}
        self.memory = None

    def unavailable(self, why):
        self.ok, self.moved, self.a, self.b, self.why = False, False, 0.0, 0.0, why
        self.network_mode = None
        self.cores = ()
        self.gpu = gpu.status_off()
        self.bars = ()
        self.memory = None


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace")
    except OSError:
        return None


def _kib(text):
    """A cgroup byte count ("max": None) or a /proc "<n> kB" in KiB; None if unreadable."""
    try:
        words = text.split()
        return int(words[0]) if words[1:2] == ["kB"] else int(words[0]) // 1024
    except (AttributeError, ValueError, IndexError):
        return None


def _field(text, name):
    """The KiB of a "Name: <n> kB" line of a /proc status or meminfo text, or None."""
    for line in (text or "").splitlines():
        key, _, value = line.partition(":")
        if key == name:
            return _kib(value)
    return None


def reservation(argv):
    """What the start of the QEMU of argv reserved, capacity.reservation_mib (its unit's MemoryHigh
    under KVM): (KiB, how it adds up) from its -m (MiB, or size= with a k/M/G/T suffix) and its
    -accel (tcg: its tb-size, else QEMU's default); (None, "") without -m."""
    mib, env = None, {"ACCEL": "kvm"}
    for option, value in zip(argv, argv[1:]):
        if option == "-m":
            size = next((v[5:] for v in value.split(",") if v.startswith("size=")), None)
            match = re.fullmatch(r"(\d+)([kKmMgGtT]?)[bB]?", size or value.split(",")[0])
            if match:
                shift = {"": 0, "k": -10, "m": 0, "g": 10, "t": 20}[match[2].lower()]
                mib = int(match[1]) << shift if shift >= 0 else int(match[1]) >> -shift
        elif option == "-accel":
            accel, *options = value.split(",")
            env["ACCEL"] = accel
            for o in options:
                if o.startswith("tb-size="):
                    env["TCG_TB_SIZE"] = o[8:]
    if mib is None:
        return None, ""
    try:
        headroom = capacity.memory_headroom_mib()
    except base.Error:
        # Keep sampling the actual unit even if its configured reservation cannot be read.
        return None, ""
    total = capacity.reservation_mib(mib, env, headroom=headroom)
    tb = total - mib - headroom
    parts = f"{mib} MiB guest RAM" + (f", {tb} MiB TCG cache" if tb else "")
    return total * 1024, f"{parts}, {headroom} MiB headroom"


def memory_usage(pid, budget, proc_root="/proc", cgroup_root="/sys/fs/cgroup"):
    """QEMU pid's VM on the host: {"ram": {...}, "swap": {...}}, each with "used" and "total"
    (KiB; None: unavailable, with "why"), "source" ("unit": its control group, "qemu": QEMU's
    status) and "of" (what total is); ram also "cache" (the unit's page cache, KiB, or None) and
    "parts" (how total adds up). budget: reservation's, the scale without a finite memory.high."""
    group = gpu.unit_cgroup(pid, proc_root, cgroup_root)
    current = _read(os.path.join(group, "memory.current")) if group else None
    host_swap = _field(_read(os.path.join(proc_root, "meminfo")), "SwapTotal")
    ram = {"used": None, "total": budget[0], "of": "the start's reservation", "parts": budget[1]}
    ram.update(cache=None, why="")
    swap = {"used": None, "total": host_swap, "of": "the host's swap", "why": ""}
    if current is not None:
        ram["source"] = swap["source"] = "unit"
        ram["used"] = _kib(current)
        high = _kib(_read(os.path.join(group, "memory.high")))
        if high:
            ram["total"], ram["of"] = high, "its MemoryHigh"
            ram["parts"] = budget[1] if budget[0] == high else ""
        stat = _read(os.path.join(group, "memory.stat")) or ""
        ram["cache"] = next(
            (_kib(line[5:]) for line in stat.splitlines() if line.startswith("file ")), None
        )
        swap["used"] = _kib(_read(os.path.join(group, "memory.swap.current")))
        if swap["used"] is None:
            swap["why"] = "not accounted for the VM's unit (no memory.swap.current)"
        limit = _kib(_read(os.path.join(group, "memory.swap.max")))
        if limit is not None and (host_swap is None or limit < host_swap):
            swap["total"], swap["of"] = limit, "its MemorySwapMax"
    else:
        ram["source"] = swap["source"] = "qemu"
        status = _read(os.path.join(proc_root, str(pid), "status"))
        ram["used"], swap["used"] = _field(status, "VmRSS"), _field(status, "VmSwap")
        if ram["used"] is None:
            ram["why"] = f"cannot read /proc/{pid}/status"
        if swap["used"] is None:
            swap["why"] = "not accounted (no VmSwap for QEMU)"
    if ram["total"] is None and not ram["why"]:
        ram["why"] = "no scale: the guest's RAM is unknown (no -m on QEMU's command line)"
    if not host_swap and not swap["why"]:
        swap["why"] = "not in use (the host has no swap)"
    if not swap["total"] and not swap["why"]:
        swap["why"] = "the unit's MemorySwapMax is 0"
    return {"ram": ram, "swap": swap}


UNSET = 1 << 63  # a balloon statistic the guest has not given reads as 2^64 - 1


def guest_ram(reply, now):
    """The guest's RAM from QMP qom-get of the balloon's guest-stats (bytes, "last-update" in
    host seconds): ({"total", "available", "used"} in KiB, "") or (None, why)."""
    if not isinstance(reply, dict) or not isinstance(reply.get("stats"), dict):
        return None, "no balloon statistics"
    stats, updated = reply["stats"], reply.get("last-update") or 0

    def kib(name):
        value = stats.get(name)
        return value // 1024 if isinstance(value, int) and 0 <= value < UNSET else None

    total = kib("stat-total-memory")
    available = kib("stat-available-memory")
    if available is None:
        available = kib("stat-free-memory")
    if not updated or total is None or available is None:
        return None, "waiting for the guest's balloon statistics"
    if now - updated > BALLOON_STALE_S:
        return None, f"no statistics from the guest for {now - updated:.0f} s"
    return {"total": total, "available": available, "used": max(0, total - available)}, ""


SWAP_LINE = re.compile(r"swap total=(-?\d+) free=(-?\d+)")


def guest_swap(text):
    """The last whole line of what ringbuf-read gave (text: complete lines only, the read's
    partial last line kept back by the caller) as {"total", "free", "used"} KiB; None without one.
    A read can begin inside a line the ring overwrote: such a fragment does not match."""
    for line in reversed(text.split("\n")):
        match = SWAP_LINE.fullmatch(line.strip())
        if match:
            total, free = int(match[1]), int(match[2])
            if total < 0 or free < 0:
                return None
            return {"total": total, "free": free, "used": max(0, total - free)}
    return None


def parse_micom(line):
    """A micom-status line ("standby=0 power=3 ready=1 led.6e=010101/2 ...") as {"standby": 0,
    "power": 3, "ready": 1, "led": {0x6e: (b"\\x01\\x01\\x01", 2)}}; None if it is not one."""
    out = {"led": {}}
    try:
        for word in line.split():
            key, _, value = word.partition("=")
            if key in ("standby", "power", "ready"):
                out[key] = int(value)
            elif key.startswith("led."):
                data, _, writes = value.partition("/")
                out["led"][int(key[4:], 16)] = (bytes.fromhex(data), int(writes))
    except ValueError:
        return None
    return out if "standby" in out and "power" in out else None


def standby_light(micom):
    """The standby light setting the firmware last wrote: True, False, or None if never written."""
    data = micom["led"].get(STANDBY_LIGHT, (b"", 0))[0]
    return bool(data[1]) if len(data) >= 2 else None


def panel_off(micom):
    """Whether the micom has the panel off: the SoC suspended (standby=1) or the micom's warm
    standby (power=2), webOS's Active Standby with the SoC running. lgemu's micom has only power
    states 3 (active) and 2; its power off (25 00) ends the VM. False without a micom state."""
    return micom is not None and (bool(micom.get("standby")) or micom.get("power") == WARM_STANDBY)


def front_led(micom):
    """The micom's front LED as (state, tooltip): off, red, or unknown.

    The micom drives the LED; webOS writes its standby light setting with
    Set_PowerIndicator(0, v), command 6E 01 <v> 01. An unwritten setting uses
    the micom's default, on.
    """
    if micom is None:
        return "unknown", "Front LED: unavailable"
    light = standby_light(micom)
    if not panel_off(micom):
        return "off", "Front LED: off (the TV is on)"
    if light is False:
        return "off", "Front LED: off (standby light off)"
    return "red", "Front LED: red (standby)" + ("" if light else ", the micom's default")


def _last_line(path, size=1024):
    """The last complete line of the file at path, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - size))
            text = f.read().decode(errors="replace")
    except OSError:
        return None
    lines = [x for x in text.split("\n")[:-1] if x.strip()]  # a partial last line left out
    return lines[-1] if lines else None


class Stats:
    def __init__(self, directory, on_change, on_standby=None):
        self.pidfile = os.path.join(directory, "qemu.pid")
        self.micom_path = os.path.join(directory, "micom-status")
        self.micom = None
        self.qmp_path = os.path.join(directory, "viewer-qmp.sock")
        self.on_change = on_change
        self.on_standby = on_standby
        self.standby = False
        self.power_state = None
        self.src = [Source() for _ in NAMES]
        self.hz = os.sysconf("SC_CLK_TCK") or 100
        self.pid = 0
        self.vcpus = 0
        self.ticks = self.ticks_at = None
        self.vcpu_prev, self.vcpu_at = {}, None
        self.gpu_sampler = None
        self.budget = None  # reservation of QEMU's command line
        self.guest_ram, self.guest_ram_why = None, "QMP: not connected"
        self.guest_swap, self.swap_at, self.swap_why = None, None, "QMP: not connected"
        self.ring_tail = ""  # ringbuf-read's text after its last newline, for the next read
        self.net_prev = self.net_at = None
        self.disk_prev = self.disk_at = None
        self.refreshes = 0
        # QMP
        self._conn = self._in = self._cancel = None
        self._client = Gio.SocketClient()
        self._qmp = "idle"  # idle, connecting, greeting, capabilities, ready
        self._pending = False
        self._retry_at = 0.0
        self._timer = GLib.timeout_add(1000, self._tick)
        GLib.idle_add(lambda: (self._tick(), False)[1])

    def close(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._qmp_reset("closed", retry=False)

    # ---- /proc ----
    def _qemu_pid(self):
        text = _read(self.pidfile)
        if text is None:
            return 0, "no qemu.pid next to the D-Bus socket\n(a VM not started by the launchers)"
        try:
            pid = int(text.split()[0])
        except (ValueError, IndexError):
            return 0, "unreadable qemu.pid"
        comm = _read(f"/proc/{pid}/comm") or ""
        if not comm.startswith("qemu"):
            return 0, f"stale qemu.pid: process {pid} is not QEMU"
        return pid, ""

    def _vcpu_usage(self, now):
        """Actual vCPU threads, ordered by guest CPU index; replacements need a fresh baseline."""
        current, usage = {}, []
        try:
            tasks = os.listdir(f"/proc/{self.pid}/task")
        except OSError:
            tasks = []
        for task in tasks:
            base = f"/proc/{self.pid}/task/{task}"
            match = re.fullmatch(r"CPU (\d+)/(?:KVM|TCG)", (_read(base + "/comm") or "").strip())
            if not match:
                continue
            text = _read(base + "/stat")
            try:
                fields = text[text.rindex(")") + 2 :].split()
                ticks, started = int(fields[11]) + int(fields[12]), int(fields[19])
            except (AttributeError, ValueError, IndexError):
                continue
            index = int(match[1])
            current[index] = (task, started, ticks)
            prev = self.vcpu_prev.get(index)
            percent = None
            if (
                prev
                and prev[:2] == (task, started)
                and ticks >= prev[2]
                and self.vcpu_at is not None
                and now > self.vcpu_at
            ):
                percent = (ticks - prev[2]) / self.hz / (now - self.vcpu_at) * 100
            usage.append((index, percent))
        self.vcpu_prev, self.vcpu_at = current, now
        self.vcpus = len(current)
        return tuple(sorted(usage))

    def _cpu(self, now):
        src = self.src[CPU]
        stat = _read(f"/proc/{self.pid}/stat")
        try:
            fields = stat[stat.rindex(")") + 2 :].split()
            ticks = int(fields[11]) + int(fields[12])
        except (AttributeError, ValueError, IndexError):
            src.unavailable(f"cannot read /proc/{self.pid}/stat")
            self.ticks = self.ticks_at = None
            self.vcpu_prev, self.vcpu_at = {}, None
            return
        cores = self._vcpu_usage(now)
        if self.ticks_at is not None and now > self.ticks_at:
            src.a = (ticks - self.ticks) / self.hz / (now - self.ticks_at) * 100
            src.moved = ticks != self.ticks
            src.ok = True
            if self.vcpus:
                src.why = (
                    f"QEMU, pid {self.pid}: {src.a:.0f}% of one host CPU (as top)\n"
                    f"{self.vcpus} vCPUs: {100 * self.vcpus}% is all of them busy"
                )
            else:
                src.why = (
                    f"QEMU, pid {self.pid}: {src.a:.0f}% of one host CPU (as top)\n"
                    "vCPU count unknown (no -name debug-threads=on)"
                )
        elif not src.ok:
            src.unavailable(f"QEMU, pid {self.pid}: measuring")
        self.ticks, self.ticks_at = ticks, now
        src.cores = cores

    def _netdev(self):
        cmdline = (_read(f"/proc/{self.pid}/cmdline") or "").split("\0")
        for i, arg in enumerate(cmdline[:-1]):
            if arg == "-netdev":
                return cmdline[i + 1]
        return ""

    def _gpu(self, now):
        src = self.src[GPU]
        if self.gpu_sampler is None:
            layers = None
            try:
                with open(os.path.join(os.path.dirname(self.pidfile), "vm-info.toml"), "rb") as f:
                    layers = tomllib.load(f).get("layers")
            except (OSError, ValueError):
                pass  # an older launch's: no layers, then QEMU's virtio-gpu-gl tells
            self.gpu_sampler = gpu.Sampler(self.pid, layers)
        src.gpu = value = self.gpu_sampler.sample(now)
        src.ok = value["headline"] is not None
        src.a = value["headline"] or 0.0
        src.moved = src.a >= 0.5

    def _memory(self, now):
        if self.budget is None:
            self.budget = reservation((_read(f"/proc/{self.pid}/cmdline") or "").split("\0"))
        host = memory_usage(self.pid, self.budget)
        swap, swap_why = self.guest_swap, self.swap_why
        if swap is not None and (self.swap_at is None or now - self.swap_at > SWAP_STALE_S):
            swap, swap_why = None, f"no report from the guest for {SWAP_STALE_S:.0f} s or more"
        ram = self.guest_ram if self._qmp == "ready" else None
        ram_why = self.guest_ram_why if self._qmp == "ready" else "QMP: not connected"
        src = self.src[MEM]
        memory = {"guest": ram, "guest_why": ram_why, "swap": swap, "swap_why": swap_why}
        memory["host"] = host
        if ram is None or not ram["total"]:
            src.unavailable(ram_why or "unavailable")
            src.memory = memory
            return
        src.ok, src.moved, src.why, src.memory = True, True, "", memory  # RAM is always held
        src.a = ram["used"] / ram["total"] * 100
        src.bars = (src.a,) + (
            (swap["used"] / swap["total"] * 100,) if swap and swap["total"] else ()
        )

    def _net(self, now):
        src = self.src[NET]
        dev = self._netdev()
        src.network_mode = None
        # These backends have no TAP. A host's unrelated tap0 must not supply their counters.
        if dev.startswith("user,") or dev == "user":
            src.unavailable("Direct networking\nTraffic rates unavailable")
            src.network_mode = "direct"
            self.net_prev = None
            return
        if dev.startswith("hubport,") or dev == "hubport":
            src.unavailable("Networking disabled")
            self.net_prev = None
            return
        m = re.search(r"ifname=([^,]+)", dev)
        tap = m.group(1) if m else "tap0"
        counters = None
        for line in (_read(f"/proc/{self.pid}/net/dev") or "").splitlines():
            name, _, rest = line.partition(":")
            if rest and name.strip() == tap:
                f = rest.split()
                # the host's side of the TAP: what it transmits the guest receives
                counters = (int(f[8]), int(f[0]))
        if counters is None:
            src.unavailable(f"no {tap} in /proc/{self.pid}/net/dev")
            self.net_prev = None
            return
        if (
            self.net_prev
            and now > self.net_at
            and counters[0] >= self.net_prev[0]
            and counters[1] >= self.net_prev[1]
        ):
            dt = now - self.net_at
            src.a, src.b = (
                (counters[0] - self.net_prev[0]) / dt,
                (counters[1] - self.net_prev[1]) / dt,
            )
            src.moved = counters != self.net_prev
            src.ok = True
            src.why = (
                f"{tap} in QEMU's network namespace (mode=mitm)\n"
                f"the guest received {counters[0] / 1e6:.1f} MB, sent {counters[1] / 1e6:.1f} MB"
            )
        elif not src.ok:
            src.unavailable(f"{tap}: measuring")
        self.net_prev, self.net_at = counters, now

    # ---- QMP ----
    def _qmp_reset(self, why, retry=True):
        if self._cancel:
            self._cancel.cancel()
        if self._conn:
            try:
                self._conn.close(None)
            except GLib.Error:
                pass
        self._conn = self._in = self._cancel = None
        self._qmp, self._pending = "idle", False
        self._retry_at = time.monotonic() + (QMP_RETRY if retry else 1e9)
        self.disk_prev = None
        self.src[DISK].unavailable(why)
        self.guest_ram, self.guest_ram_why = None, why
        self.guest_swap, self.swap_at, self.swap_why = None, None, why
        self.ring_tail = ""
        self._set_standby(None)

    def _set_standby(self, standby):
        self.power_state = standby
        standby = bool(standby)
        if standby != self.standby:
            self.standby = standby
            if self.on_standby:
                self.on_standby(standby)

    def _qmp_send(self, obj):
        try:
            self._conn.get_output_stream().write_all((json.dumps(obj) + "\n").encode(), None)
        except GLib.Error as e:
            self._qmp_reset(f"QMP: {e.message}")

    def _connected(self, client, result, cancel):
        try:
            conn = client.connect_finish(result)
        except GLib.Error as e:
            if cancel.is_cancelled():
                return
            self._qmp_reset(f"QMP: {e.message}")
            self.on_change()
            return
        self._conn = conn
        self._in = Gio.DataInputStream.new(conn.get_input_stream())
        self._in.set_newline_type(Gio.DataStreamNewlineType.ANY)
        self._qmp = "greeting"
        self._in.read_line_async(GLib.PRIORITY_DEFAULT, cancel, self._line, cancel)

    def _line(self, stream, result, cancel):
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error as e:
            if cancel.is_cancelled():
                return
            self._qmp_reset(f"QMP: {e.message}")
            self.on_change()
            return
        if line is None:
            if not cancel.is_cancelled():
                self._qmp_reset("QMP: QEMU closed viewer-qmp.sock")
                self.on_change()
            return
        self._qmp_line(line)
        if self._in is stream and not cancel.is_cancelled():
            stream.read_line_async(GLib.PRIORITY_DEFAULT, cancel, self._line, cancel)

    def _qmp_line(self, line):
        try:
            o = json.loads(line)
        except ValueError:
            return
        if "QMP" in o:
            self._qmp_send({"execute": "qmp_capabilities", "id": "caps"})
            if self._conn:
                self._qmp = "capabilities"
        elif "error" in o and o.get("id") == "chardev":
            self.power_state = None
            self.on_change()
        elif "error" in o and o.get("id") == "balloon":
            # a VM started before every VM had a balloon
            self.guest_ram = None
            self.guest_ram_why = "no balloon (QMP: " + o["error"].get("desc", "error") + ")"
        elif "error" in o and o.get("id") == "memory":
            # a VM started before the webos.memory port
            self.guest_swap = None
            self.swap_why = "no webos.memory port (QMP: " + o["error"].get("desc", "error") + ")"
        elif "error" in o:
            self.src[DISK].unavailable("QMP: " + o["error"].get("desc", "error"))
            self._pending = False
            if o.get("id") == "caps":
                self._qmp = "greeting"
            self.on_change()
        elif "return" in o:
            if o.get("id") == "caps":
                self._qmp = "ready"
                self.src[DISK].unavailable("QMP: measuring")
                self._disk_ask()
            elif o.get("id") == "stats":
                self._pending = False
                if isinstance(o["return"], list):
                    self._blockstats(o["return"])
            elif o.get("id") == "balloon":
                self.guest_ram, self.guest_ram_why = guest_ram(o["return"], time.time())
            elif o.get("id") == "memory" and isinstance(o["return"], str):
                self._ring(o["return"])
            elif o.get("id") == "chardev" and isinstance(o["return"], list):
                self._set_standby(
                    next(
                        (
                            c.get("frontend-open")
                            for c in o["return"]
                            if c.get("label") == "standby"
                        ),
                        None,
                    )
                )
                self.on_change()
        # events are ignored

    def _blockstats(self, drives):
        src = self.src[DISK]
        rd = sum(d.get("stats", {}).get("rd_bytes", 0) for d in drives)
        wr = sum(d.get("stats", {}).get("wr_bytes", 0) for d in drives)
        now = time.monotonic()
        if (
            self.disk_prev
            and now > self.disk_at
            and rd >= self.disk_prev[0]
            and wr >= self.disk_prev[1]
        ):
            dt = now - self.disk_at
            src.a, src.b = (rd - self.disk_prev[0]) / dt, (wr - self.disk_prev[1]) / dt
            src.moved = (rd, wr) != self.disk_prev
            src.ok = True
            src.why = (
                f"QMP query-blockstats, {len(drives)} drives (viewer-qmp.sock)\n"
                f"read {rd / 1e6:.1f} MB, written {wr / 1e6:.1f} MB"
            )
        elif not src.ok:
            src.unavailable("QMP: measuring")
        self.disk_prev, self.disk_at = (rd, wr), now
        self.on_change()

    def _ring(self, text):
        """What ringbuf-read gave of the webos.memory port: its whole lines now, the rest with the
        next read."""
        text = self.ring_tail + text
        whole, _, self.ring_tail = text.rpartition("\n")
        self.ring_tail = self.ring_tail[-256:]  # longer than any line: not the start of one
        swap = guest_swap(whole) if whole else None
        if swap is not None:
            self.guest_swap, self.swap_at, self.swap_why = swap, time.monotonic(), ""
        elif self.guest_swap is None and not self.swap_why.startswith("no webos.memory"):
            self.swap_why = "waiting for the guest's first report"

    def _disk_ask(self):
        if self._qmp == "ready" and not self._pending:
            self._pending = True
            self._qmp_send({"execute": "query-blockstats", "id": "stats"})
            self._qmp_send({"execute": "query-chardev", "id": "chardev"})
            self._qmp_send(
                {
                    "execute": "qom-get",
                    "arguments": {"path": BALLOON, "property": "guest-stats"},
                    "id": "balloon",
                }
            )
            self._qmp_send(
                {
                    "execute": "ringbuf-read",
                    "arguments": {"device": "memory", "size": 4096, "format": "utf8"},
                    "id": "memory",
                }
            )

    def _disk(self, now):
        if self._qmp == "ready":
            self._disk_ask()
            return
        if self._qmp != "idle" or now < self._retry_at:
            return
        if not os.path.exists(self.qmp_path):
            self.src[DISK].unavailable(
                "no viewer-qmp.sock next to the D-Bus socket\n"
                "(a VM not started by this tree's launchers)"
            )
            self._retry_at = now + QMP_RETRY
            return
        self._cancel = Gio.Cancellable()
        self._qmp = "connecting"
        self._client.connect_async(
            Gio.UnixSocketAddress.new(self.qmp_path), self._cancel, self._connected, self._cancel
        )

    # ---- refresh ----
    def _tick(self):
        now = time.monotonic()
        self.refreshes += 1
        pid, why = self._qemu_pid()
        if pid != self.pid:
            self.pid = pid
            self.ticks = self.ticks_at = self.net_prev = None
            self.vcpu_prev, self.vcpu_at = {}, None
            self.vcpus = 0
            self.gpu_sampler = None
            self.budget = None
            self.src[CPU].ok = self.src[NET].ok = False
        if pid:
            self._cpu(now)
            self._gpu(now)
            self._memory(now)
            self._net(now)
        else:
            self.src[CPU].unavailable(why)
            self.src[GPU].unavailable("off")
            self.src[MEM].unavailable(why)
            self.src[NET].unavailable(why)
        self._disk(now)
        line = _last_line(self.micom_path) if pid else None
        self.micom = parse_micom(line) if line else None
        self.on_change()
        return bool(self._timer)
