#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""tests/regress/lib/lanemem.py without a VM: a QEMU's /proc status, the host's meminfo and kernel
log, the store of peaks, the bookings and the lanes' admission to their host
(`pytest tests/unit/regress/test_lanemem.py`)."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "regress", "lib")
)
import lanemem

STATUS = """Name:\tqemu-system-aar
Umask:\t0022
State:\tS (sleeping)
VmPeak:\t 4523012 kB
VmSize:\t 4390212 kB
VmHWM:\t  801234 kB
VmRSS:\t  774000 kB
RssAnon:\t  120000 kB
RssShmem:\t  640000 kB
"""

MEMINFO = """MemTotal:        3943260 kB
MemFree:         1720000 kB
MemAvailable:    3703808 kB
SwapCached:            0 kB
SwapTotal:       6291452 kB
SwapFree:        4205000 kB
"""

OOM_LOG = """qemu-system-aar invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE|__GFP_COMP), order=0, oom_score_adj=0
oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/user.slice,task=qemu-system-aar,pid=4321,uid=1000
Out of memory: Killed process 4321 (qemu-system-aar) total-vm:4523012kB, anon-rss:1200000kB, file-rss:20480kB, shmem-rss:1500000kB, UID:1000 pgtables:3000kB oom_score_adj:0
"""


class ParseTest(unittest.TestCase):
    def test_qemu_peak(self):
        self.assertEqual(lanemem.qemu_peak(STATUS), {"peak_mb": 783, "rss_mb": 756})
        self.assertIsNone(lanemem.qemu_peak(""))  # no QEMU (it had gone)
        self.assertEqual(lanemem.qemu_peak("VmHWM:\t1024 kB\n"), {"peak_mb": 1, "rss_mb": None})

    def test_status_cmd(self):
        # the command on a made-up RUN_DIR: this shell is no QEMU, so it says nothing
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "qemu.pid"), "w") as f:
                f.write(f"{os.getpid()}\n")
            out = subprocess.run(
                ["sh", "-c", lanemem.STATUS_CMD],
                env=dict(os.environ, RUN_DIR=d),
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(out, "")
            os.unlink(os.path.join(d, "qemu.pid"))
            out = subprocess.run(
                ["sh", "-c", lanemem.STATUS_CMD],
                env=dict(os.environ, RUN_DIR=d),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual((out.returncode, out.stdout), (0, ""))

    def test_swap(self):
        self.assertEqual(lanemem.swap_used_mb(MEMINFO), 2037)
        self.assertEqual(lanemem.swap_used_mb("SwapTotal: 0 kB\nSwapFree: 0 kB\n"), 0)  # a board
        self.assertIsNone(lanemem.swap_used_mb("MemTotal: 1 kB\n"))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "meminfo-start.txt")
            with open(p, "w") as f:
                f.write(
                    "2026-11-25T10:00:00Z\n              total   used\nMem:  3850  225\n" + MEMINFO
                )
            self.assertEqual(lanemem.swap_of_file(p), 2037)
            self.assertIsNone(lanemem.swap_of_file(os.path.join(d, "none")))

    def test_state(self):
        text = MEMINFO + "lane h13-afro 614400\nlane o18k-goldilocks2 \n"
        self.assertEqual(
            lanemem.parse_state(text),
            {
                "total_mb": 3850,
                "avail_mb": 3617,
                "swap_mb": 2037,
                "swap_total_mb": 6143,
                "rss_mb": {"h13-afro": 600},
            },
        )
        cmd = lanemem.state_cmd(["h13-afro"])
        self.assertIn("build/instances/h13-afro/qemu.pid", cmd)
        out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=True).stdout
        self.assertIsNotNone(lanemem.parse_state(out)["avail_mb"])  # this machine's

    def test_oom(self):
        self.assertEqual(
            lanemem.oom_kills(OOM_LOG),
            [{"victim": "qemu-system-aar", "pid": 4321, "rss_mb": 2656}],
        )
        self.assertEqual(lanemem.oom_kills(""), [])
        self.assertIn("--since @1700000000", lanemem.oom_log_cmd(1700000000.7))
        out = subprocess.run(
            ["sh", "-c", lanemem.OOM_COUNT_CMD], capture_output=True, text=True, check=True
        ).stdout
        self.assertTrue(out.strip().isdigit())


class BookingTest(unittest.TestCase):
    def test_booking(self):
        peak = {"peak_mb": 700, "ram_mb": 3072}
        # 700 x 1.15 + 128
        self.assertEqual(lanemem.booking(3072, "headless", peak), (933, "peak"))
        self.assertEqual(lanemem.booking(3072, "graphics", peak), (3072, "ram"))  # never graphics
        self.assertEqual(lanemem.booking(3072, "headless", None), (3072, "ram"))  # not measured
        self.assertEqual(lanemem.booking(2048, "headless", peak), (2048, "ram"))  # at another RAM
        # capped at the RAM: a peak near it (or an OOM's, the RAM itself)
        self.assertEqual(
            lanemem.booking(1536, "headless", {"peak_mb": 1400, "ram_mb": 1536}), (1536, "ram")
        )


class ReserveTest(unittest.TestCase):
    def test_reserve(self):
        self.assertEqual(lanemem.reserve_mb(3850, 0, 1024), 512)  # a ROCK Pi 4 board: no swap
        self.assertEqual(lanemem.reserve_mb(6000, 0, 1024), 750)  # an eighth
        self.assertEqual(lanemem.reserve_mb(16000, 0, 1024), 1024)  # never more than the cap
        self.assertEqual(lanemem.reserve_mb(1500, 0, 1024), 512)  # never less than 512
        self.assertEqual(lanemem.reserve_mb(8062, 6143, 1024), 1024)  # the Pi: swap, as before
        self.assertEqual(lanemem.reserve_mb(None, None, 1024), 1024)  # unknown (a dry run)


class PeaksTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "regress", "peaks.json")
        self.store = lanemem.Peaks(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_keeps_the_largest(self):
        self.assertEqual(self.store.load(), {})
        self.assertIsNone(self.store.get("h13-afro", "headless", "aarch64"))
        self.store.record("h13-afro", "headless", "aarch64", 650, 3072, "r1", "rock1", "2026-11-25")
        self.store.record("h13-afro", "headless", "aarch64", 700, 3072, "r2", "rock2", "2026-11-26")
        kept = self.store.record("h13-afro", "headless", "aarch64", 600, 3072, "r3", "rock1")
        self.assertEqual(
            kept,
            {
                "peak_mb": 700,
                "ram_mb": 3072,
                "run": "r2",
                "host": "rock2",
                "date": "2026-11-26",
                "lanes": 3,
            },
        )
        # each mode and architecture its own
        self.store.record("h13-afro", "graphics", "aarch64", 2900, 3072, "r4", "pi")
        self.store.record("h13-afro", "headless", "x86_64", 900, 3072, "r5", "pc")
        self.assertEqual(self.store.get("h13-afro", "headless", "aarch64")["peak_mb"], 700)
        self.assertEqual(self.store.get("h13-afro", "graphics", "aarch64")["peak_mb"], 2900)
        self.assertEqual(self.store.get("h13-afro", "headless", "x86_64")["peak_mb"], 900)
        with open(self.path) as f:
            data = json.load(f)
        self.assertEqual(data["schema"], 1)
        self.assertEqual(sorted(data["peaks"]["h13-afro"]), ["graphics", "headless"])
        self.assertEqual(
            [n for n in os.listdir(os.path.dirname(self.path)) if n.endswith(".tmp")], []
        )

    def test_another_ram_starts_over(self):
        self.store.record("k24-queue", "headless", "aarch64", 1500, 2048, "r1", "pi")
        kept = self.store.record("k24-queue", "headless", "aarch64", 1200, 2560, "r2", "pi")
        self.assertEqual((kept["peak_mb"], kept["ram_mb"], kept["lanes"]), (1200, 2560, 1))

    def test_oom_raises_to_the_ram(self):
        self.store.record("o18k-goldilocks2", "headless", "aarch64", 1000, 3072, "r1", "rock1")
        kept = self.store.record(
            "o18k-goldilocks2", "headless", "aarch64", None, 3072, "r2", "rock1", oom=True
        )
        self.assertEqual((kept["peak_mb"], kept["oom"], kept["run"]), (3072, "r2", "r2"))
        kept = self.store.record(
            "o18k-goldilocks2", "headless", "aarch64", 990, 3072, "r3", "rock1"
        )
        # a later lane never lowers it
        self.assertEqual((kept["peak_mb"], kept["oom"]), (3072, "r2"))
        self.assertEqual(lanemem.booking(3072, "headless", kept), (3072, "ram"))

    def test_unreadable_store(self):
        os.makedirs(os.path.dirname(self.path))
        for text in ("{not json", '{"schema": 99, "peaks": {"x": {}}}', "[]"):
            with open(self.path, "w") as f:
                f.write(text)
            self.assertEqual(self.store.load(), {})
        # and a record replaces it whole
        self.store.record("x", "headless", "aarch64", 1, 1536, "r", "h")
        self.assertEqual(self.store.get("x", "headless", "aarch64")["peak_mb"], 1)

    def test_side_by_side(self):
        # lanes of runs side by side record under the lock: none is lost
        threads = [
            threading.Thread(
                target=self.store.record,
                args=(f"t{i % 4}", "headless", "aarch64", 100 + i, 2048, f"r{i}", "h"),
            )
            for i in range(24)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(
            sum(self.store.get(f"t{i}", "headless", "aarch64")["lanes"] for i in range(4)), 24
        )
        self.assertEqual(self.store.get("t3", "headless", "aarch64")["peak_mb"], 123)


class GateTest(unittest.TestCase):
    def gate(self, state, base=0):
        said = []
        g = lanemem.HostGate(
            {"h": base}, lambda host, lanes: state(lanes), 1024, poll=0.05, say=said.append
        )
        return g, said

    def test_admits_what_fits(self):
        g, said = self.gate(lambda lanes: {"avail_mb": 3600, "swap_mb": 0, "rss_mb": {}})
        self.assertTrue(g.admit("h", "a", 1000))
        self.assertEqual(g.running, {"h": {"a": 1000}})
        g.done("h", "a")
        self.assertEqual(g.running, {"h": {}})
        self.assertEqual(said, [])

    def test_counts_what_running_lanes_have_not_taken_yet(self):
        # a board, 3600 MB available: lane a booked 1500 and holds 400 so far, so 1100 more are owed:
        # 3600 - 1100 = 2500 free, short of 1500 + 1024 for b, until a ends
        rss = {"a": 400}
        g, said = self.gate(
            lambda lanes: {"avail_mb": 3600, "swap_mb": 0, "rss_mb": {t: rss[t] for t in lanes}}
        )
        self.assertTrue(g.admit("h", "a", 1500))
        started = []
        t = threading.Thread(target=lambda: started.append(g.admit("h", "b", 1500)))
        t.start()
        time.sleep(0.2)
        self.assertEqual(started, [])
        self.assertIn("b waits: MemAvailable less the running lanes' bookings is 2500 MB", said[0])
        g.done("h", "a")
        t.join(5)
        self.assertEqual(started, [True])
        # once a has taken all it booked, b would have fitted beside it
        g2, _ = self.gate(lambda lanes: {"avail_mb": 3600, "swap_mb": 0, "rss_mb": {"a": 1500}})
        self.assertTrue(g2.admit("h", "a", 1500))
        self.assertTrue(g2.admit("h", "b", 1000))

    def test_an_idle_host_starts_one(self):
        g, said = self.gate(lambda lanes: {"avail_mb": 2000, "swap_mb": 0, "rss_mb": {}})
        self.assertTrue(g.admit("h", "a", 3072))
        self.assertIn("starting it as the only lane", said[0])

    def test_unknown_state_admits(self):
        g, _ = self.gate(lambda lanes: None)
        self.assertTrue(g.admit("h", "a", 3072))
        self.assertTrue(g.admit("h", "b", 3072))

    def test_swap_growth(self):
        swap = [2000]
        g, said = self.gate(
            lambda lanes: {"avail_mb": 9000, "swap_mb": swap[0], "rss_mb": {}}, base=2000
        )
        self.assertTrue(g.admit("h", "a", 1000))
        swap[0] = 2000 + lanemem.SWAP_GROWTH_MB + 1
        started = []
        t = threading.Thread(target=lambda: started.append(g.admit("h", "b", 1000)))
        t.start()
        time.sleep(0.2)
        self.assertEqual(started, [])  # waits for a
        swap[0] = 2100  # a's QEMU's swapped pages went with it
        g.done("h", "a")
        t.join(5)
        self.assertEqual(started, [True])
        # still grown with nothing running: the host is closed
        g.done("h", "b")
        swap[0] = 3000
        self.assertFalse(g.admit("h", "c", 1000))
        self.assertIn("swap grew 1000 MB", g.closed["h"])
        swap[0] = 2000
        self.assertFalse(g.admit("h", "d", 1000))  # stays closed
        self.assertTrue(any("it takes no more lanes" in s for s in said))


if __name__ == "__main__":
    unittest.main()
