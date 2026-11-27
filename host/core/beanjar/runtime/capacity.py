# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What a VM may take of the host before it starts: the memory admission (the RAM
and headroom the running VMs claim against what the host has) and the listen addresses' ports."""

import os

from ..base import Error
from . import hostconfig, hostprocs, vmunits

# The start's admission (Library.start): a VM starts if the host's MemAvailable, less what the
# library's other starting and running VMs may still claim, covers its RAM (-m, VM.guest_ram: the
# TV's, VM.mem, and under qemu-user that userland's own on top) and
# memory_headroom_mib more, left to the host, its other VMs' growth and a GUI. The default is
# 1 GiB; a host may tune it with beanjar host config. A VM's unit
# is held to the same sum under KVM (unit.start: MemoryHigh), so that is what it reserves. Under TCG
# QEMU also holds its translation-block cache (1 GiB by default), which counts too, and the unit
# has no MemoryHigh: throttling QEMU at it stalled a 2 GiB VM in D state (reservation_mib).
START_HEADROOM_MIB = 1024


ADMISSION_LOCK = ".admission.lock"


def memory_headroom_mib():
    """This host's per-VM memory allowance, defaulting to 1 GiB."""
    value = hostconfig.global_config().get("memory_headroom_mib")
    return START_HEADROOM_MIB if value is None else value


def _meminfo_mib(field):
    """One /proc/meminfo field in MiB (BEANJAR_MEMINFO: a stand-in file), or None."""
    try:
        with open(os.environ.get("BEANJAR_MEMINFO") or "/proc/meminfo") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def mem_available_mib():
    """The host's MemAvailable in MiB, or None."""
    return _meminfo_mib("MemAvailable")


def mem_total_mib():
    """The host's MemTotal in MiB, or None."""
    return _meminfo_mib("MemTotal")


RAM_STEP_MIB = 256


def host_ram_cap_mib(library=None):
    """The most RAM a new VM can be given and still be admitted (admission) on this host with
    nothing else running: MemAvailable plus what the library's running and starting VMs hold
    (their RSS, so a busy host does not shrink it), less reservation_mib's headroom and, under
    TCG, translation cache; never over MemTotal less the headroom; rounded down to RAM_STEP_MIB.
    None if the host's memory is not known."""
    avail, total = mem_available_mib(), mem_total_mib()
    if avail is None or total is None:
        return None
    held = 0
    if library is not None:
        for o in library.vms():
            if o.lock_owner() is not None or o.alive():
                pid = o.pid()
                held += rss_mib(pid) if pid else 0
    over = reservation_mib(0)  # headroom, and under TCG the translation cache
    cap = min(avail + held, total) - over
    return max(cap, 0) // RAM_STEP_MIB * RAM_STEP_MIB


def reservation_mib(mem, env=None, *, headroom=None):
    """What a VM of mem MiB of RAM may hold on the host: under KVM its unit's MemoryHigh
    (unit.resources), RAM plus the headroom. Under TCG (env: its launcher environment, qemu.vm_accel)
    also the TB cache, since QEMU's own memory comes on top of the guest's; no MemoryHigh there."""
    from . import qemu

    env = os.environ if env is None else env
    extra = qemu.tcg_tb_mib(env) if qemu.vm_accel(env) == "tcg" else 0
    return int(mem) + extra + (memory_headroom_mib() if headroom is None else headroom)


def _unit_mib(value):
    """A systemd byte count (MemoryHigh=, MemoryCurrent=) in MiB; None for infinity or not set."""
    try:
        return int(value) // (1024 * 1024)
    except (TypeError, ValueError):
        return None


def rss_mib(pid):
    """The resident memory of pid in MiB (VmRSS), 0 if it cannot be read."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def claim_mib(vm):
    """What vm, starting or running, may still take of the host's memory beyond what it holds (and
    MemAvailable already lacks): its bound (its unit's MemoryHigh, else reservation_mib of its RAM; under TCG, no MemoryHigh)
    less its use (the unit's MemoryCurrent, else QEMU's RSS); 0 for a VM that is off. A qemu-user
    VM's MemoryHigh has its swap in host RAM on top (unit.resources), which, as for the admission,
    is not claimed: it is taken only as the guest swaps."""
    if vm.lock_owner() is None and not vm.alive():
        return 0
    cap = used = None
    name = vm.unit_name()
    if name and vm.unit_up():
        rc, out = vmunits.systemctl(
            "show", "--property=MemoryHigh", "--property=MemoryCurrent", f"{name}.service"
        )
        props = (
            dict(line.split("=", 1) for line in out.splitlines() if "=" in line) if rc == 0 else {}
        )
        cap, used = _unit_mib(props.get("MemoryHigh")), _unit_mib(props.get("MemoryCurrent"))
        if cap is not None:
            cap -= vm.host_swap()
    if cap is None:
        mem = vm.guest_ram()
        if mem is None:
            return 0
        cap = reservation_mib(mem, vm.launch_env())
    if used is None:
        pid = vm.pid()
        used = rss_mib(pid) if pid else 0
    return max(0, cap - used)


def admission(vm):
    """None if the host has the memory to start vm (MemAvailable, less the claims of the library's
    other starting and running VMs (claim_mib), covers its RAM (VM.guest_ram: under qemu-user more
    than the TV's mem) and the host's memory_headroom_mib), else why not; None too if either is not
    known. Library.start asks under admission_lock."""
    mem, avail = vm.guest_ram(), mem_available_mib()
    if mem is None or avail is None:
        return None
    tv = vm.mem()
    others = [(o.name, claim_mib(o)) for o in vm.library.vms() if o.name != vm.name]
    claimed = sum(c for _, c in others)
    headroom = memory_headroom_mib()
    need = reservation_mib(mem, vm.launch_env(), headroom=headroom)
    if avail - claimed >= need:
        return None
    tb = need - int(mem) - headroom
    held = ", ".join(f"{n} {c} MiB" for n, c in others if c)
    return (
        f"not enough free memory to start VM {vm.name}: it needs {mem} MiB"
        + (f" ({tv} for the TV and {mem - tv} for qemu-user's overhead)" if mem != tv else "")
        + f" and {headroom} MiB of headroom"
        + (f" and {tb} MiB of TCG translation cache" if tb else "")
        + f", and the host has {avail} MiB available"
        + (f", {claimed} MiB of it claimed by VMs starting or running ({held})" if claimed else "")
        + " (stop another VM, or give this one less memory)"
    )


class admission_lock:
    """The library's admission lock (fcntl.flock on <library>/.admission.lock): held while a start
    is admitted and takes its start.lock, so that a second start at the same moment counts the
    first one's claim."""

    def __init__(self, library):
        self.path = os.path.join(library.root, ADMISSION_LOCK)

    def __enter__(self):
        import fcntl

        self.f = open(self.path, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.f.close()


def admit(vm):
    why = admission(vm)
    if why:
        raise Error(why, "mem")


def _overlap(a, b):
    """Two listeners on a and b of one port collide: the same address, or either on all of them."""
    return a == b or "0.0.0.0" in (a, b)


def listen_conflict(vm):
    """None if vm's forwarded ports (VM.listeners: 3000, 3001, 6622, 9922, 9991 and its forward= on
    listen or its own address) are free, else why not: another VM of the library, starting or
    running, that listens on the same port where they overlap (the ports are the TV's, fixed), or another listener of the host (port_conflict). Library.start asks
    under admission_lock."""
    mine = vm.listeners()
    if not mine:
        return None
    for other in vm.library.vms():
        if other.name == vm.name or (other.lock_owner() is None and not other.alive()):
            continue
        try:
            others = other.listeners()
        except Error:
            others = []  # its own forward= is broken: its start says so, not this one's
        for addr, port in mine:
            for theirs, p in others:
                if p == port and _overlap(addr, theirs):
                    return (
                        f"cannot start VM {vm.name}: VM {other.name} listens on {theirs}:{port}, where its "
                        f"{addr}:{port} would be (listen; the TV's ports are fixed: stop {other.name}, "
                        f"or give one of them another listen)"
                    )
    for addr, port in mine:
        why = hostprocs.port_conflict(addr, port)
        if why:
            return f"cannot start VM {vm.name}: its service port {addr}:{port} (listen): {why}"
    return None
