#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The histogram step of scripts/regress --tv without a VM: its region map against the set's
measured totals and lgemu's table, its picture on the sample grid, the judge on probe output made
from the expected bins, the plan from the expected table, and the generator's histogram row."""

import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
REGRESS = os.path.join(TOP, "tests", "regress")
sys.path.insert(0, os.path.join(TOP, "host", "core"))
sys.path.insert(0, os.path.join(REGRESS, "lib"))
import tvsuite  # noqa: E402
import vthist  # noqa: E402

loader = importlib.machinery.SourceFileLoader(
    "tv_expect_gen", os.path.join(REGRESS, "tv-expect-gen.py")
)
gen = importlib.util.module_from_spec(importlib.util.spec_from_loader("tv_expect_gen", loader))
loader.exec_module(gen)

LGEMU = os.path.join(TOP, "guest", "kernel", "modules", "lgemu", "capture-histogram.c")
PAT = tvsuite.pattern()["histogram"]


def probe_text(preset_bins=None, init="ok", finalize="0", extra=""):
    """What hist-probe.c prints for a screen that shows PAT exactly (preset_bins: {preset: blocks}
    to print instead)."""
    lines = [f"crashes_before=", f"vt_devmode=made", f"hist.init={init}"]
    if init == "ok":
        for p in vthist.PRESET_ORDER:
            lines += [f"hist.p{p}.set=0", f"hist.p{p}.get=0", f"hist.p{p}.wait_ms=210"]
            lines.append(f"hist.p{p}.arrived=yes")
            blocks = (preset_bins or {}).get(p) or [
                (i, bins) for i, _, bins in vthist.expected(PAT, p)
            ]
            for i, (index, bins) in enumerate(blocks):
                chans = " ".join(
                    f"{c}=" + ",".join(f"{b}x{n}" for b, n in sorted(bins[k].items()))
                    for k, c in enumerate("RGB")
                )
                lines.append(f"hist.p{p}.b{i}={index} {chans}")
        lines.append(f"hist.finalize={finalize}")
    lines += ["vt_devmode_after=no", "crashes=" + extra, "TV FACTS END"]
    return "\n".join(lines) + "\n"


class RegionTest(unittest.TestCase):
    def test_regions_give_the_measured_totals(self):
        for p, regions in enumerate(vthist.REGIONS):
            self.assertEqual(len(regions), vthist.BLOCK_COUNT[p])
            for i, rect in enumerate(regions):
                self.assertTrue(all(v % vthist.PITCH == 0 for v in rect), (p, i))
                x0, x1, y0, y1 = vthist.cells(rect)
                self.assertEqual((x1 - x0) * (y1 - y0), vthist.TOTALS[p][i], (p, i))

    def test_regions_are_lgemus(self):
        """The judge's map (the doc's) and the emulation's table are the same rectangles."""
        text = open(LGEMU).read()
        body = text.split("regions[4][HIST_BLOCKS][4] = {", 1)[1].split("};", 1)[0]
        presets = re.split(r"/\* preset \d \*/", body)[1:]
        lg = [
            [tuple(int(v) for v in m) for m in re.findall(r"\{(\d+), (\d+), (\d+), (\d+)\}", s)]
            for s in presets
        ]
        self.assertEqual(lg, [[vthist.cells(r) for r in rs] for rs in vthist.REGIONS])

    def test_probe_knows_the_totals(self):
        src = open(os.path.join(REGRESS, "histogram", "hist-probe.c")).read()
        m = re.search(r"total0\[4\] = \{([^}]*)\}", src)
        self.assertEqual([int(v) for v in m.group(1).split(",")], [t[0] for t in vthist.TOTALS])
        order = re.search(r"order\[4\] = \{([^}]*)\}", src).group(1)
        self.assertEqual(tuple(int(v) for v in order.split(",")), vthist.PRESET_ORDER)


class PictureTest(unittest.TestCase):
    def test_picture_on_the_grid(self):
        cells = vthist.picture_cells(PAT)
        self.assertEqual(cells[(0, 0)], (255, 255, 255))
        self.assertEqual(cells[(80, 0)], (255, 0, 0))
        self.assertEqual(cells[(79, 45)], (0, 0, 255))
        self.assertEqual(cells[(159, 89)], (128, 128, 128))

    def test_patch_off_the_grid_refused(self):
        bad = dict(PAT, patches=[dict(PAT["patches"][0], rect=[0, 0, 0.33, 0.5])])
        with self.assertRaises(ValueError):
            vthist.picture_cells(bad)

    def test_expected_preset0_block0(self):
        """Preset 0's block 0, 468-1428 x 0-1080: 41 columns left of the middle, 39 right of it."""
        index, total, bins = vthist.expected(PAT, 0)[0]
        self.assertEqual((index, total), (0, 7200))
        self.assertEqual(bins[0], {255: 41 * 45 + 39 * 45, 0: 41 * 45, 128: 39 * 45})
        self.assertEqual(bins[1], {255: 41 * 45, 0: 39 * 45 + 41 * 45, 128: 39 * 45})

    def test_expected_counts_add_up(self):
        for p in range(4):
            for i, total, bins in vthist.expected(PAT, p):
                self.assertEqual([sum(b.values()) for b in bins], [total] * 3)


class JudgeTest(unittest.TestCase):
    def verdicts(self, items):
        return {c: s for c, s, _ in items}

    def test_exact_picture_passes(self):
        items = tvsuite.judge_histogram(probe_text())
        self.assertEqual(
            self.verdicts(items),
            {
                "histogram.calls": "PASS",
                "histogram.layout": "PASS",
                "histogram.picture": "PASS",
                "histogram.crash": "PASS",
            },
            items,
        )
        self.assertIn("62 blocks", dict((c, w) for c, _, w in items)["histogram.layout"])

    def test_one_point_off_fails_the_picture(self):
        blocks = [(i, bins) for i, _, bins in vthist.expected(PAT, 2)]
        r = dict(blocks[5][1][0])
        k = next(iter(r))
        r[k] -= 1
        r[k + 1] = r.get(k + 1, 0) + 1
        blocks[5] = (5, [r, blocks[5][1][1], blocks[5][1][2]])
        items = tvsuite.judge_histogram(probe_text({2: blocks}))
        v = self.verdicts(items)
        self.assertEqual((v["histogram.picture"], v["histogram.layout"]), ("FAIL", "PASS"))
        self.assertIn("preset 2 block 5 R", dict((c, w) for c, _, w in items)["histogram.picture"])

    def test_wrong_layout_fails(self):
        blocks = [(i, bins) for i, _, bins in vthist.expected(PAT, 0)]  # preset 0's for preset 3
        v = self.verdicts(tvsuite.judge_histogram(probe_text({3: blocks})))
        self.assertEqual(v["histogram.layout"], "FAIL")

    def test_init_refused(self):
        items = tvsuite.judge_histogram(probe_text(init="null"))
        self.assertEqual(items[0][:2], ("histogram.calls", "FAIL"))

    def test_preset_missing_and_finalize(self):
        text = "\n".join(
            ln for ln in probe_text(finalize="2").splitlines() if not ln.startswith("hist.p3.")
        )
        items = tvsuite.judge_histogram(text)
        why = dict((c, w) for c, _, w in items)["histogram.calls"]
        self.assertIn("preset 3: not read", why)
        self.assertIn("finalize gave 2", why)

    def test_crash_reported(self):
        v = self.verdicts(tvsuite.judge_histogram(probe_text(extra="vtCaptureTestSuite.123")))
        self.assertEqual(v["histogram.crash"], "FAIL")


class PlanTest(unittest.TestCase):
    def test_plan(self):
        row = {"histogram": {"present": True, "suite": "/usr/bin/vtCaptureTestSuite"}}
        self.assertEqual(tvsuite.histogram_plan(row), ("/usr/bin/vtCaptureTestSuite", []))
        suite, items = tvsuite.histogram_plan(row, "headless")
        self.assertEqual((suite, items[0][1]), (None, "SKIP"))
        suite, items = tvsuite.histogram_plan({"histogram": {"present": False, "source": "stub"}})
        self.assertEqual((suite, items[0][1]), (None, "SKIP"))
        self.assertIn("stub", items[0][2])
        self.assertEqual(tvsuite.histogram_plan({})[1][0][1], "FAIL")

    def test_tracked_table(self):
        """Every target file has the row; the firmwares the doc names: -56 and later have the
        calls, -54's stub (k8hp ombre) and older (mullet, number1) do not."""
        for t in tvsuite.expect_targets():
            self.assertIn("histogram", tvsuite.load_expect(t), t)
        want = {
            "o22-ponytail": True,
            "o22-queue": True,
            "k8hp-ponytail": True,
            "k8hp-queue": True,
            "k8hp-ombre": False,
            "o22-mullet": False,
            "o22-number1": False,
        }
        for t, present in want.items():
            h = tvsuite.load_expect(t)["histogram"]
            self.assertEqual(h["present"], present, t)
            if present:
                self.assertEqual(h["suite"], "/usr/bin/vtCaptureTestSuite")


STUB_SO = r"""
__attribute__((used)) const unsigned char _ZN11VtHistogram13initHistogramEv[8] = {%s};
int vtCapture_initHistogram(void) { return 0; }
"""


class GeneratorTest(unittest.TestCase):
    def lib(self, d, init):
        src, so = os.path.join(d, "v.c"), os.path.join(d, "v.so")
        with open(src, "w") as f:
            f.write(STUB_SO % ", ".join(str(b) for b in init))
        subprocess.run(["cc", "-shared", "-fPIC", "-o", so, src], check=True)
        return open(so, "rb").read()

    def fw(self, data, suite=True, control=b"Package: x\nVersion: 1.0.0-56-r7\n"):
        class Fw:
            paks = ["rootfs.pak"]

            def files(self, pak):
                return {"usr/lib/opkg/info/lib32-screen-capture-webos.control": None}

            def find(self, path):
                if path == gen.VTCAPTURE and data is not None:
                    return ("rootfs.pak", "usr/lib/libvtcapture.so.1.0.0")
                if path == gen.VTCAPTURE_SUITE and suite:
                    return ("rootfs.pak", path)
                return None

            def read(self, pak, path):
                return control if path.endswith(".control") else data

        return Fw()

    def test_rows(self):
        with tempfile.TemporaryDirectory() as d:
            stub = self.lib(d, gen.HIST_STUB + b"\0" * 4)
            real = self.lib(d, b"\x2d\xe9\xf0\x4f\x00\x00\x00\x00")
        row = gen.histogram_of(self.fw(stub, control=b"Version: 1.0.0-54-r7\n"))
        self.assertFalse(row["present"])
        self.assertIn("stub returning 7 (screen-capture-webos 1.0.0-54-r7)", row["source"])
        row = gen.histogram_of(self.fw(real))
        self.assertEqual((row["present"], row["suite"]), (True, "/usr/bin/vtCaptureTestSuite"))
        self.assertIn("1.0.0-56-r7", row["source"])
        row = gen.histogram_of(self.fw(real, suite=False))
        self.assertEqual((row["present"], row["suite"]), (True, None))
        self.assertFalse(gen.histogram_of(self.fw(None))["present"])

    def test_new_table_added_before_sources(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.toml")
            with open(path, "w") as f:
                f.write('firmware = "FW"\n\n[smp]\npresent = true\n\n[sources]\na = "b"\n')
            real = gen.TABLE_ROWS["histogram"]
            gen.TABLE_ROWS["histogram"] = lambda fw: {"present": False, "source": "none"}
            try:
                gen.replace_tables(d, "t", "FW", None, ["histogram"])
                gen.replace_tables(d, "t", "FW", None, ["histogram"])  # then replaced in place
            finally:
                gen.TABLE_ROWS["histogram"] = real
            text = open(path).read()
        self.assertEqual(text.count("[histogram]"), 1)
        self.assertLess(text.index("[histogram]"), text.index("[sources]"))


# A stand-in for libvtcapture: each block's R, G and B in bin 0, as many as the C2's totals
FAKE_LIB = r"""
#include <stdint.h>
#include <string.h>
static const unsigned totals[4][16] = {%(totals)s};
static const int counts[4] = {16, 16, 16, 14};
static int preset = -1, h;
void *vtCapture_initHistogram(int (*policy)(void)) { return policy() ? &h : 0; }
int vtCapture_setPresetHistogram(void *x, int p) { (void)x; preset = p; return 0; }
int vtCapture_getHistogram(void *x, void *buf)
{
    (void)x;
    uint8_t *b = buf;
    for (int i = 0; i < counts[preset]; i++, b += 1540) {
        uint32_t index = (uint32_t)i;  /* the library numbers the blocks, as the stock one does */
        memcpy(b, &index, 4);
        memset(b + 4, 0, 1536);
        for (int ch = 0; ch < 3; ch++) {
            uint16_t n = totals[preset][i];
            memcpy(b + 4 + ch * 512, &n, 2);
        }
    }
    return 0;
}
int vtCapture_finalizeHistogram(void *x) { (void)x; return 0; }
"""
# the suite needs the library (its main never runs: the probe exits first)
FAKE_SUITE = (
    "int vtCapture_finalizeHistogram(void *);\n"
    "int main(int argc, char **argv) { (void)argv; return argc > 99 ? vtCapture_finalizeHistogram(0) : 42; }\n"
)


class ProbeTest(unittest.TestCase):
    """hist-probe.c built for this machine: preloaded into a process without libvtcapture (the
    timeout on the way to the suite) it does nothing; into one with it, it prints what vthist reads."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        src = os.path.join(REGRESS, "histogram", "hist-probe.c")
        cls.probe = os.path.join(d, "hist-probe.so")
        flags = ["-shared", "-fPIC", "-Wall", "-Wextra", "-Werror"]
        subprocess.run(["cc", *flags, src, "-o", cls.probe, "-ldl", "-Wl,-z,defs"], check=True)
        totals = ", ".join("{" + ", ".join(map(str, t)) + "}" for t in vthist.TOTALS)
        with open(os.path.join(d, "fake.c"), "w") as f:
            f.write(FAKE_LIB % {"totals": totals})
        with open(os.path.join(d, "suite.c"), "w") as f:
            f.write(FAKE_SUITE)
        lib = os.path.join(d, "libvtcapture.so")
        subprocess.run(["cc", "-shared", "-fPIC", os.path.join(d, "fake.c"), "-o", lib], check=True)
        cls.suite = os.path.join(d, "suite")
        subprocess.run(
            [
                "cc",
                os.path.join(d, "suite.c"),
                "-o",
                cls.suite,
                f"-L{d}",
                "-lvtcapture",
                f"-Wl,-rpath,{d}",
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_with_probe(self, argv):
        env = dict(os.environ, HIST_PROBE="1", HIST_WAIT_MS="500", LD_PRELOAD=self.probe)
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)

    def test_other_processes_run_on(self):
        r = self.run_with_probe(["timeout", "10", "sh", "-c", "exit 3"])
        self.assertEqual((r.returncode, r.stdout), (3, ""), r.stderr)

    def test_through_timeout_into_the_suite(self):
        r = self.run_with_probe(["timeout", "20", self.suite])
        self.assertEqual(r.returncode, 0, r.stderr)
        f = vthist.parse(r.stdout)
        self.assertEqual((f["init"], f["finalize"]), ("ok", "0"))
        items = dict((c, s) for c, s, _ in vthist.judge(r.stdout, PAT))
        self.assertEqual(
            (items["histogram.calls"], items["histogram.layout"], items["histogram.picture"]),
            ("PASS", "PASS", "FAIL"),  # all in bin 0: not the picture
        )


if __name__ == "__main__":
    unittest.main()
