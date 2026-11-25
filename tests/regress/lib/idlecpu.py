# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""scripts/regress's idle-cpu step: the guest's CPU per process at idle after Home, judged on the
host (docs/dev/guides/build.md, "Bring-up checklist"). tests/regress/guest.sh idle-cpu gathers
snapshots of every process's and thread's utime+stime and context switches, SECONDS apart; its
idle-threads step samples the flagged processes' threads (state, wchan, syscall). This module parses
both and judges: a process that holds above its limit in every window, or that has a thread waking
more than WAKEUPS times a second in every window, is flagged.

Why per process and not the load average: real LG sets sit at load 15 to 34 while 76 to 90 % idle
(their many sleep-and-poll threads count in it), so the load says nothing about a spin.

The limits come from measured idle CPU (% of one core, after Home):
  real sets             surface-manager 15-20, the broadcast service about 1
  Pi KVM, 34 targets    pqcontroller up to 4, captureservice 2, tvservice 2-4 (o18, m16p3),
                        servicemanager 10-11 on three ponytail boards (an open question, not a spin)
  spins found           k25lp's legacy-broadcast-atsc 86 (tFEMain's busy loop on a tuner property),
                        k2l's tvservice 128 under qemu-user (an always-readable demux; 23 on its
                        siblings), o18's tvservice tHDMISignal: 640-800 wakeups/s (a 1 ms GPIO poll)
                        at only a few % of CPU, against at most 142/s for a stock thread (W19P's
                        CCThreadProc, 5 ms nanosleep)
So LIMIT is 25 %: more than twice the busiest healthy process under KVM, a third of the busy loop
seen. surface-manager draws, and a real set's runs at 15-20 %: ALLOWANCE gives it 50. The o18 poll
costs too little CPU to show; WAKEUPS (500/s for one thread) is what finds it.

Under emulation (a guest kernel of another architecture, whose webOS runs in qemu-user, or TCG)
the same work costs several times the CPU (o18's tvservice about 11 % under qemu-user against 2-4 %
on KVM; W19P's tvservice 40-100 % under TCG): there the CPU limits are EMULATED_FACTOR times higher
and a flag is only ever WARN. The wakeup limit is not scaled: emulation lowers a poll's rate
(WAKEUPS_TCG).
"""

import collections
import re

HZ = 100  # USER_HZ, the unit of /proc's utime and stime on every Linux architecture
WINDOW = 30  # s a window (two of them): a spin is steady, the second window rules out a burst
WINDOWS = 2
FLOOR = 0.5  # % of a core in some window, for a process to be recorded
LIMIT = 25.0  # % of one core, a process's limit under KVM (module docstring)
ALLOWANCE = {"surface-manager": 50.0}  # comm: its own limit (real sets: 15-20 %)
WAKEUPS = 500.0  # voluntary context switches a second, a thread's limit
# Emulation slows a poll's wakeups rather than adding to them: o18's 1 ms poll woke about 800/s
# under qemu-user but only 290-596/s under TCG, where W19P's busiest stock thread woke 142/s.
# So the wakeup limit is never scaled up, and TCG's is lower.
WAKEUPS_TCG = 250.0
# process comm: the wakeup limit of each of its threads, where a real set's are as many. lginput2
# polls the input devices: on a real UP7560 (webOS 6) at idle its threads woke about 1980 times a
# second in all (voluntary switches over 30 s), and 1840-1960/s in one thread on k24 and k24n
# under KVM; it uses 1.6 % of a core there.
WAKEUP_ALLOWANCE = {"lginput2": 4000.0}
EMULATED_FACTOR = 4  # the limits under qemu-user or TCG (and a flag there only WARNs)
FLAG = "WARN"  # what a flag makes the item under KVM
SAMPLES = 10  # idle-threads' samples of a flagged process's threads
TOP_THREADS = 5  # threads listed for a flagged process (those at FLOOR % or THREAD_WAKEUPS_FLOOR)
THREAD_WAKEUPS_FLOOR = 50.0

# the syscalls a waiting or looping thread is likely in, by the number /proc/TID/syscall gives:
# arm (EABI; a 32-bit webOS process under an arm64 kernel reports its compat number) and x86_64
# (the guest kernel of qemu-user: the host syscall qemu-user is in for the ARM thread)
SYSCALLS = {
    "arm": {
        3: "read",
        4: "write",
        54: "ioctl",
        114: "wait4",
        142: "_newselect",
        158: "sched_yield",
        162: "nanosleep",
        168: "poll",
        177: "rt_sigtimedwait",
        240: "futex",
        252: "epoll_wait",
        265: "clock_nanosleep",
        275: "mq_timedreceive",
        285: "accept",
        292: "recvfrom",
        297: "recvmsg",
        335: "pselect6",
        336: "ppoll",
        346: "epoll_pwait",
        407: "clock_nanosleep_time64",
        413: "pselect6_time64",
        414: "ppoll_time64",
        422: "futex_time64",
        441: "epoll_pwait2",
    },
    "x86_64": {
        0: "read",
        1: "write",
        7: "poll",
        16: "ioctl",
        23: "select",
        24: "sched_yield",
        35: "nanosleep",
        43: "accept",
        45: "recvfrom",
        47: "recvmsg",
        61: "wait4",
        128: "rt_sigtimedwait",
        202: "futex",
        230: "clock_nanosleep",
        232: "epoll_wait",
        243: "mq_timedreceive",
        270: "pselect6",
        271: "ppoll",
        281: "epoll_pwait",
        441: "epoll_pwait2",
    },
}


def parse(text):
    """{arch, window, snaps: [(uptime, {pid: (ticks, comm)}, {tid: [pid, ticks, comm, vol, invol]})]}
    of idle-cpu's output; None when it is incomplete (no IDLECPU END)."""
    if "IDLECPU END" not in text:
        return None
    arch, window, snaps = "", 0, []
    for line in text.splitlines():
        f = line.split(" ")
        tag = f[0]
        try:
            if tag == "IDLECPU" and len(f) >= 4:
                arch, window = f[1], int(f[2])
            elif tag == "SNAP":
                snaps.append((float(f[2]), {}, {}))
            elif not snaps:
                continue
            elif tag == "P" and len(f) >= 6:
                snaps[-1][1][int(f[1])] = (int(f[2]) + int(f[3]), " ".join(f[5:]))
            elif tag == "T" and len(f) >= 7:
                snaps[-1][2][int(f[2])] = [int(f[1]), int(f[3]) + int(f[4]), " ".join(f[6:]), 0, 0]
            elif tag in ("V", "N") and len(f) == 4:
                t = snaps[-1][2].get(int(f[2]))
                if t is not None:
                    t[3 if tag == "V" else 4] = int(f[3])
        except ValueError:
            continue
    return {"arch": arch, "window": window, "snaps": snaps}


def emulated(arch, accel):
    """Whether the run's guest CPU is emulated: TCG, or webOS's ARM code under qemu-user (an x86
    guest kernel)."""
    return accel == "tcg" or not (arch.startswith(("aarch64", "arm")))


def wakeup_limit(accel, comm=None):
    """A thread's wakeup limit in process comm: WAKEUPS (TCG: WAKEUPS_TCG), or its allowance."""
    base = WAKEUPS_TCG if accel == "tcg" else WAKEUPS
    return max(base, WAKEUP_ALLOWANCE.get(comm, 0.0))


def limit(comm, emu):
    return ALLOWANCE.get(comm, LIMIT) * (EMULATED_FACTOR if emu else 1)


def windows(data):
    """[(seconds, {pid: (pct, comm)}, {tid: (pid, pct, vol/s, invol/s, comm)})] for each pair of
    consecutive snapshots; a task in only one of the two has no number there."""
    out = []
    for (t0, p0, k0), (t1, p1, k1) in zip(data["snaps"], data["snaps"][1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        procs = {
            pid: (100.0 * (ticks - p0[pid][0]) / (dt * HZ), comm)
            for pid, (ticks, comm) in p1.items()
            if pid in p0 and p0[pid][1] == comm and ticks >= p0[pid][0]
        }
        threads = {
            tid: (
                pid,
                100.0 * (ticks - k0[tid][1]) / (dt * HZ),
                max(0, vol - k0[tid][3]) / dt,
                max(0, invol - k0[tid][4]) / dt,
                comm,
            )
            for tid, (pid, ticks, comm, vol, invol) in k1.items()
            if tid in k0 and k0[tid][0] == pid and ticks >= k0[tid][1]
        }
        out.append((dt, procs, threads))
    return out


def measure(data, accel="kvm"):
    """The step's numbers from parse()'s data: {arch, emulated, windows: [s...], processes: [{pid,
    comm, cpu: [% per window], limit}] (those at FLOOR or above in some window, busiest first),
    flagged: [{pid, comm, cpu, why, threads: [...]}], busiest_thread_wakeups}."""
    emu = emulated(data["arch"], accel)
    wl = wakeup_limit(accel)
    ws = windows(data)
    # in every window: a process's or a thread's number is only judged where it has one throughout
    pids = set.intersection(*(set(w[1]) for w in ws)) if ws else set()
    tids = set.intersection(*(set(w[2]) for w in ws)) if ws else set()
    procs, flagged = [], []
    for pid in pids:
        comm = ws[0][1][pid][1]
        cpu = [round(w[1][pid][0], 2) for w in ws]
        p = {"pid": pid, "comm": comm, "cpu": cpu, "limit": limit(comm, emu)}
        if max(cpu) >= FLOOR:
            procs.append(p)
        whys, pwl = [], wakeup_limit(accel, comm)
        if all(c >= p["limit"] for c in cpu):
            whys.append(f"{min(cpu):.0f}-{max(cpu):.0f} % of a core (limit {p['limit']:.0f})")
        for tid in sorted(t for t in tids if ws[0][2][t][0] == pid):
            rates = [w[2][tid][2] for w in ws]
            if min(rates) >= pwl:
                whys.append(f"thread {ws[0][2][tid][4]} wakes {min(rates):.0f}/s (limit {pwl:.0f})")
        if whys:
            flagged.append(dict(p, why="; ".join(whys), threads=top_threads(ws, pid, pwl)))
    procs.sort(key=lambda p: -sum(p["cpu"]))
    flagged.sort(key=lambda p: -sum(p["cpu"]))
    # the busiest waker of all, to keep WAKEUPS honest against what healthy targets do
    wake = max(
        ((min(w[2][tid][2] for w in ws), tid) for tid in tids),
        default=None,
    )
    return {
        "arch": data["arch"],
        "accel": accel,
        "emulated": emu,
        "windows": [round(w[0], 2) for w in ws],
        "limits": {
            "default": LIMIT * (EMULATED_FACTOR if emu else 1),
            "allowance": {k: limit(k, emu) for k in ALLOWANCE},
            "wakeups": wl,
            "wakeup_allowance": {k: wakeup_limit(accel, k) for k in WAKEUP_ALLOWANCE},
            "floor": FLOOR,
        },
        "processes": procs,
        "flagged": flagged,
        "busiest_thread_wakeups": (
            {
                "rate": round(wake[0], 1),
                "thread": ws[0][2][wake[1]][4],
                "process": ws[0][1].get(ws[0][2][wake[1]][0], (0, "?"))[1],
            }
            if wake
            else None
        ),
    }


def _threads_of(ws, pid):
    return {tid: t[4] for w in ws for tid, t in w[2].items() if t[0] == pid}


def top_threads(ws, pid, wl=WAKEUPS):
    """A process's TOP_THREADS busiest threads over all windows, and any thread over WAKEUPS:
    [{tid, comm, cpu: [%...], wakeups: [/s...], preempted: [/s...], kind}]."""
    rows = []
    for tid, comm in _threads_of(ws, pid).items():
        if not all(tid in w[2] for w in ws):
            continue
        cpu = [round(w[2][tid][1], 2) for w in ws]
        vol = [round(w[2][tid][2], 1) for w in ws]
        inv = [round(w[2][tid][3], 1) for w in ws]
        rows.append(
            {
                "tid": tid,
                "comm": comm,
                "cpu": cpu,
                "wakeups": vol,
                "preempted": inv,
                "kind": kind(cpu, vol, wl),
            }
        )
    # the busiest first, then the most wakeful; threads that neither work nor wake left out
    rows.sort(key=lambda t: (-sum(t["cpu"]), -sum(t["wakeups"])))
    busy = [t for t in rows if max(t["cpu"]) >= FLOOR or max(t["wakeups"]) >= THREAD_WAKEUPS_FLOOR]
    keep = busy[:TOP_THREADS] + [t for t in busy[TOP_THREADS:] if min(t["wakeups"]) >= wl]
    return keep or rows[:1]


def kind(cpu, wakeups, wl=WAKEUPS):
    """What a thread's numbers look like: a busy loop holds the CPU and seldom sleeps; a short-sleep
    poll sleeps very often."""
    if min(wakeups) >= wl:
        return "short-sleep poll"
    if min(cpu) >= 10 and max(wakeups) < 10 * min(cpu):
        return "busy loop"
    return ""


def parse_threads(text):
    """{tid: [(state, wchan, syscall), ...]} of idle-threads' output; arch from its head."""
    arch, out = "", collections.defaultdict(list)
    for line in text.splitlines():
        f = line.split(" ")
        if f[0] == "IDLETHREADS" and len(f) >= 2:
            arch = f[1]
        elif f[0] == "X" and len(f) == 6:
            try:
                out[int(f[2])].append((f[3], f[4], f[5]))
            except ValueError:
                continue
    return arch, dict(out)


def syscall_name(nr, arch):
    if not re.fullmatch(r"-?\d+", nr):
        return nr  # running, ?
    if nr == "-1":
        return "none"
    table = SYSCALLS["x86_64" if arch.startswith("x86_64") else "arm"]
    return table.get(int(nr), nr)


def _common(values):
    """'most (n/total)', or '' with none."""
    if not values:
        return ""
    v, n = collections.Counter(values).most_common(1)[0]
    return f"{v} ({n}/{len(values)})"


def add_threads(result, text):
    """idle-threads' samples into each flagged process's threads: state, wchan and syscall, the
    most common of each with its count."""
    arch, samples = parse_threads(text)
    for p in result["flagged"]:
        for t in p["threads"]:
            s = samples.get(t["tid"], [])
            t["state"] = _common([x[0] for x in s])
            t["wchan"] = _common([x[1] for x in s])
            t["syscall"] = _common([syscall_name(x[2], arch) for x in s])
    return result


def judge(result):
    """(status, reason) of measure()'s result: PASS with nothing flagged; a flag FLAG under KVM and
    WARN under emulation."""
    if result is None:
        return "FAIL", "the guest's idle CPU snapshots are incomplete"
    if not result["windows"]:
        return "FAIL", "no idle CPU window measured"
    span = "+".join(f"{w:.0f}" for w in result["windows"])
    emu = f" (emulated CPU: limits x{EMULATED_FACTOR})" if result["emulated"] else ""
    if result["flagged"]:
        bad = "; ".join(
            f"{p['comm']} {'/'.join(f'{c:.0f}' for c in p['cpu'])} %: {p['why']}"
            + (f", {_thread_word(p['threads'][0])}" if p["threads"] else "")
            for p in result["flagged"]
        )
        return ("WARN" if result["emulated"] else FLAG), f"{bad} [{span} s{emu}]"
    return "PASS", f"{headline(result)} [{span} s{emu}]"


def _thread_word(t):
    return (
        f"busiest thread {t['comm']} {max(t['cpu']):.0f} %, {max(t['wakeups']):.0f} wakeups/s"
        + (f" ({t['kind']})" if t.get("kind") else "")
    )


def headline(result, n=3):
    """The busiest processes, "tvservice 3.1, surface-manager 2.0, ..." (% of a core, the mean of
    the windows); "all under 0.5 %" with none recorded."""
    procs = result["processes"][:n]
    if not procs:
        return f"all under {FLOOR} %"
    return ", ".join(f"{p['comm']} {sum(p['cpu']) / len(p['cpu']):.1f}" for p in procs) + " %"


def cell(status, result):
    """The table's cell: the busiest process and its % (mean), or the flagged ones."""
    if result is None or not result.get("windows"):
        return status
    if result["flagged"]:
        return f"{status}(" + ",".join(p["comm"] for p in result["flagged"]) + ")"
    top = result["processes"][:1]
    if not top:
        return f"{status} <{FLOOR}%"
    p = top[0]
    return f"{status} {p['comm']}:{sum(p['cpu']) / len(p['cpu']):.0f}%"
