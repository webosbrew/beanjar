#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The gles step of scripts/regress --tv without a VM: tests/gl/gles-version's output as tv.sh gles
prints it, judged against the TV's version and the measured host table."""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
sys.path.insert(0, os.path.join(TOP, "tests", "regress", "lib"))
import tvsuite  # noqa: E402

PROBE = os.path.join(TOP, "guest", "layers", "gl", "test", "gles-version.c")
INTEL = "D3D12 (Intel(R) UHD Graphics 770)"
HOSTS = {INTEL: ("3.0", "measured"), "V3D 7.1.10.2": ("3.1", "measured")}
LAUNCH = "kvm: on\ngl renderer: D3D12 (Intel(R) UHD Graphics 770)\n"


def output(cap="3.2", level="3.0", host=INTEL, exts=("GL_OES_copy_image",), made_above=False):
    """tv.sh gles's lines for a guest that makes contexts up to level."""
    lines = [f"cap={cap}", "probe_rc=0", "config: EGL_OPENGL_ES3_BIT yes"]
    for v in tvsuite.GLES_LEVELS:
        if tvsuite._level(v) <= tvsuite._level(level) or made_above:
            got = max(v, level, key=tvsuite._level) if v != "2.0" else "2.0"
            lines.append(
                f'request {v}: GL_VERSION "OpenGL ES {got} Mesa 26.2.3", GLSL "OpenGL ES GLSL ES '
                f'{got.replace(".", "")}0", #version 300 es compiles'
            )
        else:
            lines.append(f"request {v}: refused, EGL_BAD_CONFIG")
    lines += [
        f"highest: {level}",
        f"renderer: virgl ({host})",
        "extensions: " + " ".join(exts),
        "TV FACTS END",
    ]
    return "\n".join(lines) + "\n"


def verdicts(items):
    return {c: s for c, s, _ in items}


def why(items, check):
    return next(w for c, _, w in items if c == check)


class GlesTest(unittest.TestCase):
    def test_measured_host_passes(self):
        items = tvsuite.judge_gles(output(), LAUNCH, HOSTS)
        self.assertEqual(
            verdicts(items),
            {"gles.renderer-log": "PASS", "gles.cap": "PASS", "gles.host": "PASS"},
            items,
        )
        self.assertIn("expected ES 3.0", why(items, "gles.host"))
        self.assertIn("KHR_blend_equation_advanced", why(items, "gles.host"))
        self.assertNotIn("OES_copy_image", why(items, "gles.host"))

    def test_capped_by_the_tv(self):
        """A TV at 2.0 on a 3.1 host: 2.0 expected, and 2.0 is what the cap gives."""
        items = tvsuite.judge_gles(
            output(cap="2.0", level="2.0", host="V3D 7.1.10.2"), LAUNCH, HOSTS
        )
        self.assertEqual(verdicts(items)["gles.host"], "PASS", items)
        self.assertIn("expected ES 2.0", why(items, "gles.host"))

    def test_below_the_measured_level_fails(self):
        items = tvsuite.judge_gles(output(level="3.0", host="V3D 7.1.10.2"), LAUNCH, HOSTS)
        self.assertEqual(verdicts(items)["gles.host"], "FAIL")
        self.assertIn("expected ES 3.1", why(items, "gles.host"))

    def test_unmeasured_host_skips_with_numbers(self):
        items = tvsuite.judge_gles(output(level="3.1", host="Mali-T860 (Panfrost)"), LAUNCH, HOSTS)
        self.assertEqual(verdicts(items)["gles.host"], "SKIP")
        w = why(items, "gles.host")
        self.assertIn("guest ES 3.1 (TV 3.2) on 'Mali-T860 (Panfrost)'", w)
        self.assertIn("1 extensions", w)

    def test_over_the_cap_fails(self):
        items = tvsuite.judge_gles(output(cap="3.0", level="3.1"), LAUNCH, HOSTS)
        self.assertEqual(verdicts(items)["gles.cap"], "FAIL")

    def test_renderer_log(self):
        self.assertEqual(
            verdicts(tvsuite.judge_gles(output(), "", HOSTS))["gles.renderer-log"], "FAIL"
        )
        unknown = "gl renderer: unknown (eglinfo not installed)\n"
        self.assertEqual(
            verdicts(tvsuite.judge_gles(output(), unknown, HOSTS))["gles.renderer-log"], "FAIL"
        )

    def test_no_context(self):
        items = tvsuite.judge_gles(
            "cap=3.2\nFAIL: no surfaceless EGL display (0x3001)\n", LAUNCH, HOSTS
        )
        self.assertEqual(items[-1][:2], ("gles.cap", "FAIL"))
        self.assertIn("no surfaceless EGL display", items[-1][2])

    def test_no_cap_file(self):
        items = tvsuite.judge_gles(output(cap=""), LAUNCH, HOSTS)
        self.assertEqual(items[-1][:2], ("gles.cap", "FAIL"))


class TableTest(unittest.TestCase):
    def test_tracked_hosts(self):
        hosts = tvsuite.gles_hosts()
        self.assertTrue(hosts)
        for renderer, (es, source) in hosts.items():
            self.assertIn(es, tvsuite.GLES_LEVELS, renderer)
            self.assertTrue(source.startswith("measured "), renderer)

    def test_probe_prints_what_the_judge_reads(self):
        src = open(PROBE).read()
        for word in (
            "highest: ",
            "renderer: ",
            "extensions: ",
            "request %d.%d: ",
            'GL_VERSION \\"',
        ):
            self.assertIn(word, src)

    @unittest.skipUnless(
        os.path.exists(os.path.join(TOP, "subprojects", "mesa-26.2.3", "src", "mesa", "main")),
        "no Mesa source unpacked",
    )
    def test_es32_names_are_mesas(self):
        """Every name is an ES extension of Mesa's table whose flag compute_version_es2's ES 3.2
        rule tests."""
        main = os.path.join(TOP, "subprojects", "mesa-26.2.3", "src", "mesa", "main")
        table = open(os.path.join(main, "extensions_table.h")).read()
        version = open(os.path.join(main, "version.c")).read()
        es2 = version.split("compute_version_es2(", 1)[1]
        rule = es2.split("const bool ver_3_2 =", 1)[1].split(";", 1)[0]
        flags = set(re.findall(r"extensions->(\w+)", rule))
        for name in tvsuite.ES32_EXTENSIONS:
            m = re.search(rf"^EXT\({name[3:]}\s*,\s*(\w+)", table, re.M)
            self.assertTrue(m, name)
            self.assertIn(m.group(1), flags, name)


if __name__ == "__main__":
    unittest.main()
