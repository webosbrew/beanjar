# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""scripts/regress's memory per lane: what a lane's QEMU held at its peak, a store of those peaks,
what a lane books against its host's MemAvailable, and the host's swap guard
(docs/dev/guides/build.md, "Memory per lane").

Each lane reads its QEMU's VmHWM (the peak resident set: guest RAM touched, shared memfd RAM
included, and QEMU's own) from /proc/<pid>/status just before the VM stops. The run keeps the
largest seen per target, mode and host architecture in PEAKS (build/regress/peaks.json on the
machine that runs scripts/regress), with the run, host and date it came from.

A headless lane whose target has a peak there, measured at the RAM it is given now, books
peak x PEAK_FACTOR + PEAK_MARGIN_MB, no more than its RAM; any other lane, and every graphics lane,
books its full RAM. Why those numbers:
  - headless guests touch 33-81 % of their RAM at most, graphics ones 94-96 % and cost the host
    more than their RAM (end-of-lane guest /proc/meminfo over 553 lanes): graphics gains nothing.
  - PEAK_FACTOR: a target's touched memory differs from run to run by up to about 10 % (k25lp-queue
    1238-1261 MB, k24n-queue 965-1240 MB with a later firmware step; QEMU's VmHWM of
    the same target 2-5 % apart between board runs); 15 % covers that.
  - PEAK_MARGIN_MB: what a lane costs the host beyond its QEMU's resident set: the launcher's helpers
    (console bridge, relays), the test tools and the page cache of the guest's images. On the ROCK Pi
    boards (no swap, one lane at a time) the host's MemAvailable at the lane's end was 70-97 MB lower
    than at its start beyond the QEMU's VmHWM (six targets, 2 and 3 GB); 128 MB covers that.

Each lane is also admitted on its host's state just before it starts (HostGate):
  - memory: the host's MemAvailable now, less what each running lane has booked and its QEMU does not
    yet hold (booking - VmRSS), must cover the lane's booking and the headroom; else it waits for a
    running lane to end. The slots were planned on MemAvailable at the run's start; this catches
    what else took memory since, and matters most where there is no swap (the ROCK Pi boards).
  - swap: a host whose swap use has grown by more than SWAP_GROWTH_MB since the run began starts no
    further lane while its running lanes finish (a stopped QEMU's swapped pages go with it); if it is
    still over once none runs, it takes no more lanes. In about 100 earlier runs on the Pi the growth
    within a run was at most 252 MB (its swap sat at 1-2 GB from before the runs throughout); twice
    that is a host short of memory, not noise.

The host's own reserve is HEADROOM_MB (1 GiB) as before, except on a host without swap, where it is
an eighth of its RAM, at least RESERVE_MIN_MB (512 MB on a 3.85 GB ROCK Pi 4 board, whose own use is
about 250 MB). There nothing pages out to slow a lane, the admission above and the OOM check below
stand guard, and two lanes on a board booked 3009 MB against a 3094 MB room (k24-ombre and
o18k-goldilocks2, 512 MB reserve) left MemAvailable at 1184 MB at its lowest, with no OOM kill.

An OOM kill on the host during a lane (/proc/vmstat's oom_kill went up) fails the lane, with the
victim and its RSS from the kernel log where the user may read it. A lane whose own QEMU was the
victim, or that cannot tell whose it was, has its target's stored peak raised to its full RAM, so
that later runs book it all; a later lane's smaller peak never lowers it (delete the entry from the
store to measure again).
"""

import contextlib
import fcntl
import json
import math
import os
import re
import threading
import time

PEAK_FACTOR = 1.15
PEAK_MARGIN_MB = 128
SWAP_GROWTH_MB = 512
RESERVE_MIN_MB = (
    512  # a host without swap keeps an eighth of its RAM, at least this, at most the cap
)
SCHEMA = 1

# a lane's QEMU's status, as the lane reads it before the VM stops ($RUN_DIR/qemu.pid; the pid must
# still be a QEMU); empty output when there is none
STATUS_CMD = (
    'p=$(cat "$RUN_DIR/qemu.pid" 2>/dev/null) && [ -n "$p" ] && '
    'case "$(cat /proc/$p/comm 2>/dev/null)" in qemu-system*) '
    "grep -E '^(Name|VmHWM|VmRSS):' /proc/$p/status;; esac; true"
)
SWAP_CMD = "grep -E '^(SwapTotal|SwapFree):' /proc/meminfo"


def kb_fields(text):
    """{field: kB} of /proc status or meminfo lines ("VmHWM:  123 kB")."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)\s*kB\s*$", line.strip())
        if m:
            out[m[1]] = int(m[2])
    return out


def qemu_peak(text):
    """{peak_mb, rss_mb} (MiB, rounded up) of a QEMU's /proc status (STATUS_CMD's output), or None."""
    f = kb_fields(text)
    if "VmHWM" not in f:
        return None
    return {
        "peak_mb": -(-f["VmHWM"] // 1024),
        "rss_mb": -(-f["VmRSS"] // 1024) if "VmRSS" in f else None,
    }


def swap_used_mb(text):
    """Swap in use (MiB) of /proc/meminfo text (or SWAP_CMD's output); None without the fields."""
    f = kb_fields(text)
    if "SwapTotal" not in f or "SwapFree" not in f:
        return None
    return (f["SwapTotal"] - f["SwapFree"]) // 1024


def swap_of_file(path):
    """swap_used_mb of a file (a lane's host/meminfo-<when>.txt), None if it is not there."""
    try:
        with open(path) as f:
            return swap_used_mb(f.read())
    except OSError:
        return None


def reserve_mb(total_mb, swap_total_mb, cap):
    """What a host keeps of its MemAvailable for itself beside the lanes' bookings: cap
    (scripts/regress's HEADROOM_MB), or on a host without swap an eighth of its RAM (total_mb), no
    less than RESERVE_MIN_MB and no more than cap (module docstring). Unknown: cap."""
    if not total_mb or swap_total_mb is None or swap_total_mb > 0:
        return cap
    return min(cap, max(RESERVE_MIN_MB, total_mb // 8))


def booking(ram_mb, mode, peak=None):
    """(MiB a lane books, the rule: "peak" or "ram"). peak: the store's entry for the lane's target,
    mode and host architecture (Peaks.get), used only for a headless lane and only if it was
    measured at this RAM."""
    if mode != "headless" or not peak or peak.get("ram_mb") != ram_mb:
        return ram_mb, "ram"
    booked = math.ceil(peak["peak_mb"] * PEAK_FACTOR) + PEAK_MARGIN_MB
    if booked >= ram_mb:
        return ram_mb, "ram"
    return booked, "peak"


class Peaks:
    """The store of measured peaks: a JSON file
        {"schema": 1, "peaks": {TARGET: {MODE: {ARCH: {"peak_mb", "ram_mb", "run", "host", "date",
                                                      "lanes", "oom"?}}}}}
    peak_mb is the largest VmHWM seen; run, host and date are where and when it was; lanes counts the
    lanes recorded. An entry measured at another RAM is replaced by the first at the new one. Writes
    are under an flock of PATH.lock and replace the file whole, so runs side by side may record."""

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}  # an unreadable store books every lane its full RAM
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return {}
        return data.get("peaks") or {}

    def get(self, target, mode, arch, peaks=None):
        p = self.load() if peaks is None else peaks
        return p.get(target, {}).get(mode, {}).get(arch)

    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def record(self, target, mode, arch, peak_mb, ram_mb, run, host, date=None, oom=False):
        """Keep peak_mb for (target, mode, arch) if it is the largest at ram_mb; the entry kept.
        oom: the lane saw an OOM kill of its QEMU (or of whose it cannot tell): the peak is the full
        ram_mb, and the entry's "oom" names the run."""
        if oom:
            peak_mb = max(peak_mb or 0, ram_mb)
        date = date or time.strftime("%Y-%m-%d", time.gmtime())
        with self._locked():
            peaks = self.load()
            slot = peaks.setdefault(target, {}).setdefault(mode, {})
            old = slot.get(arch)
            if old and old.get("ram_mb") == ram_mb:
                lanes = old.get("lanes", 1) + 1
                if peak_mb > old["peak_mb"]:
                    new = {
                        "peak_mb": peak_mb,
                        "ram_mb": ram_mb,
                        "run": run,
                        "host": host,
                        "date": date,
                    }
                else:
                    new = dict(old)
                new["lanes"] = lanes
            else:
                new = {"peak_mb": peak_mb, "ram_mb": ram_mb, "run": run, "host": host, "date": date}
                new["lanes"] = 1
            if oom:
                new["oom"] = run
            slot[arch] = new
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w") as f:
                json.dump({"schema": SCHEMA, "peaks": peaks}, f, indent=1, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.path)
            return new


class HostGate:
    """The admission of each lane to its host (module docstring): admit(host, target, booked) before
    a lane starts there, done(host, target) once it has ended. read(host, targets): the host's state
    now, {"avail_mb", "swap_mb", "rss_mb": {target: its QEMU's VmRSS in MiB}} for the lanes running
    there (None or a missing field: unknown, which does not hold a lane back); base: {host: its swap
    use at the run's start}; headroom_mb: what MemAvailable keeps for the host beside the bookings
    (MiB, or {host: MiB})."""

    def __init__(self, base, read, headroom_mb, threshold=SWAP_GROWTH_MB, poll=10, say=print):
        self.base, self.read, self.headroom = base, read, headroom_mb
        self.threshold, self.poll, self.say = threshold, poll, say
        self.running = {}  # {host: {target: MiB booked}}
        self.closed = {}  # {host: why it takes no more lanes}
        self.cond = threading.Condition()

    def check(self, host, booked):
        """(the swap growth in MiB, MemAvailable in MiB less what the running lanes have booked and
        not yet used, or None): from one read of the host."""
        with self.cond:
            running = dict(self.running.get(host, {}))
        state = self.read(host, sorted(running)) or {}
        swap, base = state.get("swap_mb"), self.base.get(host)
        grown = 0 if swap is None or base is None else swap - base
        avail = state.get("avail_mb")
        if avail is None:
            return grown, None
        rss = state.get("rss_mb") or {}
        owed = sum(max(0, b - (rss.get(t) or 0)) for t, b in running.items())
        return grown, avail - owed

    def admit(self, host, target, booked):
        """True once the lane may start on host (counted running); False if the host takes no more.
        It waits while the host's swap has grown by more than the threshold, or while its free
        MemAvailable (check) does not hold booked plus the headroom, as long as a lane runs there; a
        host with none running starts the lane on memory (as the slots' "at least one") and is
        closed on swap."""
        name = host or "this machine"
        said = None
        while True:
            with self.cond:
                if host in self.closed:
                    return False
            grown, free = self.check(host, booked)
            swap_ok = grown <= self.threshold
            reserve = self.headroom.get(host) if isinstance(self.headroom, dict) else self.headroom
            mem_ok = free is None or free >= booked + reserve
            with self.cond:
                if host in self.closed:
                    return False
                idle = not self.running.get(host)
                if swap_ok and (mem_ok or idle):
                    if not mem_ok:
                        self.say(
                            f"{name}: {target}: MemAvailable less the running lanes' bookings is "
                            f"{free} MB, short of its {booked} MB and {reserve} MB headroom; "
                            "starting it as the only lane"
                        )
                    elif said:
                        self.say(f"{name}: {target}: starting ({said} no longer holds)")
                    self.running.setdefault(host, {})[target] = booked
                    return True
                if not swap_ok and idle:
                    self.closed[host] = (
                        f"swap grew {grown} MB since the run began (more than {self.threshold} MB) "
                        "with no lane running"
                    )
                    self.say(f"{name}: {self.closed[host]}: it takes no more lanes")
                    return False
                why = (
                    f"swap grew {grown} MB since the run began (more than {self.threshold} MB)"
                    if not swap_ok
                    else f"MemAvailable less the running lanes' bookings is {free} MB, short of "
                    f"{booked} MB and {reserve} MB headroom"
                )
                if why.split(" ")[0] != (said or "").split(" ")[0]:
                    self.say(f"{name}: {target} waits: {why}")
                said = why
                n = len(self.running.get(host, {}))
                self.cond.wait_for(
                    lambda n=n: len(self.running.get(host, {})) < n, timeout=self.poll
                )

    def done(self, host, target):
        with self.cond:
            self.running.get(host, {}).pop(target, None)
            self.cond.notify_all()


# the host's state for HostGate.read: MemAvailable, swap, and each named lane's QEMU's VmRSS
# (build/instances/<target>/qemu.pid in the VM host's checkout)
def state_cmd(lanes):
    return (
        "grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo; "
        + "".join(
            f"p=$(cat build/instances/{t}/qemu.pid 2>/dev/null) && "
            f"echo \"lane {t} $(awk '/^VmRSS:/{{print $2}}' /proc/$p/status 2>/dev/null)\"; "
            for t in lanes
        )
        + "true"
    )


def parse_state(text):
    """HostGate.read's dict of state_cmd's output."""
    f = kb_fields(text)
    rss = {}
    for line in text.splitlines():
        w = line.split()
        if len(w) == 3 and w[0] == "lane" and w[2].isdigit():
            rss[w[1]] = int(w[2]) // 1024
    return {
        "total_mb": f["MemTotal"] // 1024 if "MemTotal" in f else None,
        "avail_mb": f["MemAvailable"] // 1024 if "MemAvailable" in f else None,
        "swap_mb": swap_used_mb(text),
        "swap_total_mb": f["SwapTotal"] // 1024 if "SwapTotal" in f else None,
        "rss_mb": rss,
    }


# OOM kills during a lane: /proc/vmstat's oom_kill counter (readable by anyone) at its start and its
# end, and the kernel log's lines for the details where the user may read it (journalctl -k: the adm
# or systemd-journal group; dmesg: kernel.dmesg_restrict=0)
OOM_COUNT_CMD = "awk '$1 == \"oom_kill\" {print $2}' /proc/vmstat"


def oom_log_cmd(since):
    """The kernel's OOM-kill lines since the epoch second since (empty where the log is closed)."""
    return (
        f"{{ journalctl -k -q --no-pager -o cat --since @{int(since)} 2>/dev/null || "
        f"dmesg 2>/dev/null; }} | grep -E 'Out of memory|oom-kill|Killed process'; true"
    )


def oom_kills(text):
    """[{victim, pid, rss_mb}] of the kernel's "Killed process" lines (rss: anon + file + shmem)."""
    out = []
    for m in re.finditer(r"Killed process (\d+) \(([^)]*)\)([^\n]*)", text):
        rss = sum(int(v) for v in re.findall(r"(?:anon|file|shmem)-rss:(\d+)kB", m[3]))
        out.append({"victim": m[2], "pid": int(m[1]), "rss_mb": rss // 1024 if rss else None})
    return out
