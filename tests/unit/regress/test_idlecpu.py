#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The idle-cpu step without a VM: tests/regress/idle-cpu.sh's snapshots and thread samples taken
from made-up /proc trees, and tests/regress/lib/idlecpu.py's parsing and judging of them
(`pytest tests/unit/regress/test_idlecpu.py`)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "regress", "lib")
)
import idlecpu

LIB = Path(__file__).resolve().parents[2] / "regress" / "idle-cpu.sh"


def stat_line(pid, comm, state, ticks):
    """A /proc stat line: utime ticks - ticks // 4, stime ticks // 4."""
    rest = "1 1 1 0 -1 4194560 10 0 0 0"
    tail = " ".join(["0"] * 37)
    return f"{pid} ({comm}) {state} {rest} {ticks - ticks // 4} {ticks // 4} {tail}\n"


class Proc:
    """A /proc tree in a temporary directory: processes {pid: (comm, {tid: thread})}, a thread
    {comm, ticks, vol, invol, state, wchan, syscall}; a process's ticks are its threads' sum plus
    those of threads that ended (extra)."""

    def __init__(self, root):
        self.root = Path(root)

    def write(self, uptime, procs):
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        (self.root / "uptime").write_text(f"{uptime:.2f} 1000.00\n")
        for pid, (comm, threads, *extra) in procs.items():
            d = self.root / str(pid)
            total = sum(t["ticks"] for t in threads.values()) + (extra[0] if extra else 0)
            d.mkdir()
            (d / "stat").write_text(stat_line(pid, comm, "S", total))
            for tid, t in threads.items():
                td = d / "task" / str(tid)
                td.mkdir(parents=True)
                (td / "stat").write_text(stat_line(tid, t["comm"], t.get("state", "S"), t["ticks"]))
                (td / "status").write_text(
                    f"Name:\t{t['comm']}\nState:\tS (sleeping)\n"
                    f"voluntary_ctxt_switches:\t{t.get('vol', 0)}\n"
                    f"nonvoluntary_ctxt_switches:\t{t.get('invol', 0)}\n"
                )
                (td / "wchan").write_text(t.get("wchan", "0"))  # no newline, as the kernel's
                (td / "syscall").write_text(t.get("syscall", "running") + " 0x0 0x0\n")


def sh(call, proc):
    r = subprocess.run(
        ["sh", "-c", f'. "$0"; {call}', str(LIB)],
        capture_output=True,
        text=True,
        check=True,
        env=dict(os.environ, PROC=str(proc)),
    )
    return r.stdout


def th(comm, ticks, vol=0, invol=0, **kw):
    return dict(comm=comm, ticks=ticks, vol=vol, invol=invol, **kw)


def scenes():
    """Three snapshots 30 s apart of a guest with: a healthy tvservice (3 % of a core, a 5 ms
    nanosleep thread at 200 wakeups/s), a surface-manager at 40 % (under its allowance), a
    broadcast service whose tFEMain spins (90 %, almost no voluntary switch), a process with a
    1 ms poll thread (4 % CPU, 900 wakeups/s), a comm with spaces and parentheses, and a process
    that starts during the second window (no number for the first)."""
    out = []
    for i in range(3):
        n = i * 30
        procs = {
            200: (
                "tvservice",
                {
                    200: th("tvservice", 10),
                    201: th("CCThreadProc", 50 + 60 * n // 30, vol=200 * n),
                    202: th("tAUDATV", 20 + 30 * n // 30, vol=28 * n),
                },
            ),
            300: (
                "surface-manager",
                {300: th("surface-manager", 100 + 1200 * n // 30, vol=50 * n)},
            ),
            400: (
                "legacy-broadcas",
                {
                    400: th("legacy-broadcas", 5),
                    401: th("tFEMain", 1000 + 2700 * n // 30, vol=2 * n, invol=40 * n),
                },
            ),
            500: (
                "hawk poll",
                {500: th("hawk poll", 3), 501: th("tHDMISignal", 120 * n // 30, vol=900 * n)},
            ),
            600: ("odd (name) x", {600: th("odd (name) x", 20 + 15 * n // 30, vol=n)}),
        }
        if i:
            procs[700] = ("late", {700: th("late", 900 * i)})
        out.append((1000.0 + n, procs))
    return out


class Snapshots(unittest.TestCase):
    """idle-cpu.sh on a made-up /proc, then idlecpu.py on its output."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        proc = Proc(os.path.join(cls.tmp.name, "proc"))
        text = "IDLECPU aarch64 30 2\n"
        for i, (uptime, procs) in enumerate(scenes()):
            proc.write(uptime, procs)
            text += sh(f"idle_snap {i}", proc.root)
        cls.text = text + "IDLECPU END\n"
        cls.proc = proc

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_snapshot_lines(self):
        self.assertIn("SNAP 0 1000.00", self.text)
        self.assertIn("P 200 60 20 S tvservice", self.text)  # 10 + 50 + 20 ticks: 60 user, 20 sys
        self.assertIn("T 400 401 750 250 S tFEMain", self.text)
        self.assertIn("V 200 201 0", self.text)
        self.assertIn("V 200 201 12000", self.text)
        self.assertIn("N 400 401 2400", self.text)
        self.assertIn("P 600 15 5 S odd (name) x", self.text)

    def test_parse(self):
        data = idlecpu.parse(self.text)
        self.assertEqual((data["arch"], data["window"], len(data["snaps"])), ("aarch64", 30, 3))
        up, procs, threads = data["snaps"][1]
        self.assertEqual(up, 1030.0)
        self.assertEqual(procs[600], (35, "odd (name) x"))
        self.assertEqual(threads[501], [500, 120, "tHDMISignal", 27000, 0])
        self.assertIsNone(idlecpu.parse(self.text.replace("IDLECPU END", "")))

    def test_measure_and_judge(self):
        r = idlecpu.measure(idlecpu.parse(self.text), "kvm")
        self.assertFalse(r["emulated"])
        self.assertEqual(r["windows"], [30.0, 30.0])
        cpu = {p["comm"]: p["cpu"] for p in r["processes"]}
        self.assertEqual(cpu["tvservice"], [3.0, 3.0])
        self.assertEqual(cpu["surface-manager"], [40.0, 40.0])
        self.assertEqual(cpu["legacy-broadcas"], [90.0, 90.0])
        self.assertNotIn("late", cpu)  # not there for the first window
        flagged = {p["comm"]: p for p in r["flagged"]}
        self.assertEqual(set(flagged), {"legacy-broadcas", "hawk poll"})
        fe = flagged["legacy-broadcas"]["threads"][0]
        self.assertEqual(
            (fe["comm"], fe["cpu"], fe["wakeups"]), ("tFEMain", [90.0, 90.0], [2.0, 2.0])
        )
        self.assertEqual(fe["preempted"], [40.0, 40.0])
        self.assertEqual(fe["kind"], "busy loop")
        hawk = flagged["hawk poll"]
        self.assertIn("thread tHDMISignal wakes 900/s", hawk["why"])
        self.assertEqual(hawk["threads"][0]["kind"], "short-sleep poll")
        self.assertEqual(r["busiest_thread_wakeups"]["thread"], "tHDMISignal")
        status, why = idlecpu.judge(r)
        self.assertEqual(status, idlecpu.FLAG)
        self.assertIn("legacy-broadcas 90/90 %: 90-90 % of a core (limit 25)", why)
        self.assertIn("[30+30 s]", why)
        self.assertEqual(idlecpu.cell(status, r), f"{status}(legacy-broadcas,hawk poll)")

    def test_emulated_limits(self):
        data = idlecpu.parse(self.text)
        data["arch"] = "x86_64"  # webOS under qemu-user
        r = idlecpu.measure(data, "kvm")
        self.assertTrue(r["emulated"])
        self.assertEqual(r["limits"]["default"], 100.0)
        # 90 % is under the emulated limit; the poll's wakeups are not scaled
        self.assertEqual([p["comm"] for p in r["flagged"]], ["hawk poll"])
        status, why = idlecpu.judge(r)
        self.assertEqual(status, "WARN")
        self.assertIn("emulated CPU: limits x4", why)

    def test_tcg_wakeup_limit(self):
        r = idlecpu.measure(idlecpu.parse(self.text), "tcg")
        self.assertTrue(r["emulated"])
        self.assertEqual(r["limits"]["wakeups"], idlecpu.WAKEUPS_TCG)
        # CCThreadProc's 200/s stays under TCG's lower limit
        self.assertNotIn("tvservice", [p["comm"] for p in r["flagged"]])

    def test_one_burst_is_not_a_flag(self):
        data = idlecpu.parse(self.text)
        (_, p1, k1), (_, p2, k2) = data["snaps"][1:]
        # the spin and the poll stop in the second window: their numbers there stay at the first's
        for pid in (400, 500):
            p2[pid] = p1[pid]
        for tid in (400, 401, 500, 501):
            k2[tid] = list(k1[tid])
        r = idlecpu.measure(data, "kvm")
        self.assertEqual(r["flagged"], [])
        status, why = idlecpu.judge(r)
        self.assertEqual(status, "PASS")
        self.assertTrue(why.startswith("legacy-broadcas 45.0, surface-manager 40.0"), why)
        self.assertEqual(idlecpu.cell(status, r), "PASS legacy-broadcas:45%")

    def test_threads(self):
        r = idlecpu.measure(idlecpu.parse(self.text), "kvm")
        # the spinning thread sits in ioctl (arm EABI 54) or runs; the poller in nanosleep (162)
        procs = scenes()[2][1]
        procs[400][1][401].update(state="R", wchan="0", syscall="running")
        procs[500][1][501].update(state="S", wchan="hrtimer_nanosleep", syscall="162")
        self.proc.write(1100.0, procs)
        text = sh("idle_threads 400,500 3", self.proc.root)
        self.assertIn("IDLETHREADS ", text)
        self.assertIn("X 3 500 501 S hrtimer_nanosleep 162", text)
        self.assertIn("X 1 400 401 R 0 running", text)
        text = text.replace(text.split("\n", 1)[0], "IDLETHREADS aarch64 3")
        idlecpu.add_threads(r, text)
        t = {p["comm"]: p["threads"][0] for p in r["flagged"]}
        self.assertEqual(t["hawk poll"]["wchan"], "hrtimer_nanosleep (3/3)")
        self.assertEqual(t["hawk poll"]["syscall"], "nanosleep (3/3)")
        self.assertEqual(t["legacy-broadcas"]["state"], "R (3/3)")
        self.assertEqual(t["legacy-broadcas"]["syscall"], "running (3/3)")


class Names(unittest.TestCase):
    def test_syscall_names(self):
        self.assertEqual(idlecpu.syscall_name("54", "aarch64"), "ioctl")
        self.assertEqual(idlecpu.syscall_name("142", "armv7l"), "_newselect")
        self.assertEqual(idlecpu.syscall_name("202", "x86_64"), "futex")
        self.assertEqual(idlecpu.syscall_name("9999", "x86_64"), "9999")
        self.assertEqual(idlecpu.syscall_name("-1", "aarch64"), "none")
        self.assertEqual(idlecpu.syscall_name("running", "aarch64"), "running")

    def test_emulated(self):
        self.assertFalse(idlecpu.emulated("aarch64", "kvm"))
        self.assertTrue(idlecpu.emulated("aarch64", "tcg"))
        self.assertTrue(idlecpu.emulated("x86_64", "kvm"))

    def test_incomplete(self):
        self.assertEqual(idlecpu.judge(None)[0], "FAIL")


if __name__ == "__main__":
    unittest.main()
