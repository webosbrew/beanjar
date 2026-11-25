#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The OpenGL ES cap: every chip states its TV's version, and the guest Mesa patch's reader and
minimum (guest/layers/gl/patches/mesa/0005-*), built from the patch's own lines over a fake file."""

import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SOC = ROOT / "guest/layers/soc"
PATCH = ROOT / "guest/layers/gl/patches/mesa/0005-mesa-gles-version-at-most-the-tv-s.patch"
VERSIONS = ("2.0", "3.0", "3.1", "3.2")
# The chips below ES 3.2: their firmware's GL library prints that version (the soc tables cite it)
BELOW_32 = {
    "h13": "3.0",
    "k2l": "3.1",
    "k2lp": "3.1",
    "lm15u": "3.0",
    "m14": "2.0",
    "m2": "2.0",
    "m2r": "2.0",
    "m3": "2.0",
    "m3r": "2.0",
    "mtka5lr": "2.0",
}


def chip_versions():
    out = {}
    for table in sorted(SOC.glob("*/meson.build")):
        found = re.findall(r"^gl_es_version = '([^']*)'$", table.read_text(), re.MULTILINE)
        out[table.parent.name] = found
    return out


def test_every_chip_states_one_version():
    versions = chip_versions()
    assert versions
    for chip, found in versions.items():
        assert len(found) == 1, f"{chip}: gl_es_version stated {len(found)} times"
        assert found[0] in VERSIONS, chip
        assert found[0] == BELOW_32.get(chip, "3.2"), chip


def test_netcast45_states_es2():
    text = (ROOT / "guest/layers/netcast45/meson.build").read_text()
    assert "configuration: {'GLES': '2.0'}" in text


def test_patch_is_applied_by_the_wrap():
    assert PATCH.name in (ROOT / "subprojects/mesa.wrap").read_text()


def _added(start, end):
    """The patch's added lines from the one starting with start to the one starting with end."""
    lines = [
        l[1:]
        for l in PATCH.read_text().splitlines()
        if l.startswith("+") and not l.startswith("+++")
    ]
    i = next(n for n, l in enumerate(lines) if l.startswith(start))
    j = next(n for n, l in enumerate(lines) if n > i and l.startswith(end))
    return "\n".join(lines[i : j + 1])


HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef unsigned GLuint;
#define simple_mtx_lock(m) ((void)(m))
#define simple_mtx_unlock(m) ((void)(m))
static int override_lock;
static FILE *fopen_test(const char *path, const char *mode)
{
    (void)path;
    const char *p = getenv("CAP_FILE");
    return p ? fopen(p, mode) : NULL;
}
#define fopen(p, m) fopen_test(p, m)
@CAP@
#undef fopen
static GLuint capped(GLuint version)
{
@MIN@
}
int main(int argc, char **argv)
{
    (void)argc;
    printf("%u\n", capped((GLuint)atoi(argv[1])));
    return 0;
}
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("gles-cap")
    src = d / "harness.c"
    # gles_cap(), from its file's name to the function's closing brace, and the minimum
    cap = _added("#define GLES_CAP_FILE", "}")
    minimum = _added("      GLuint cap = gles_cap();", "      return cap")
    src.write_text(HARNESS.replace("@CAP@", cap).replace("@MIN@", minimum))
    exe = d / "harness"
    subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", str(src), "-o", str(exe)], check=True)
    return exe


@pytest.mark.parametrize(
    "text, native, expect",
    [
        (None, 32, 32),  # no file: the driver's own
        ("2.0\n", 31, 20),  # the TV below the driver: the TV's
        ("3.0\n", 31, 30),
        ("3.2\n", 31, 31),  # the TV above the driver: the driver's, never raised
        ("3.2\n", 32, 32),
        ("3.0\n", 30, 30),
        # the layer's file: long comment lines first (longer than any fixed line buffer)
        ("# " + "x" * 200 + "\n# another comment\n2.0\n", 32, 20),
        ("\n# c\n\n3.0\n", 32, 30),
        ("junk\n2.0\n", 32, 32),  # the first other line is not a version: no cap
        ("1.1\n", 32, 32),  # not an ES 2+ version: no cap
        ("", 32, 32),
    ],
)
def test_cap(harness, tmp_path, text, native, expect):
    env = {}
    if text is not None:
        f = tmp_path / "gles-version"
        f.write_text(text)
        env["CAP_FILE"] = str(f)
    out = subprocess.run(
        [str(harness), str(native)], env=env, capture_output=True, text=True, check=True
    )
    assert int(out.stdout) == expect
