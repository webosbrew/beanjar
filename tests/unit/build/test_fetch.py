#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""meson/fetch.py: one unpacked tree per wrap, redone when the wrap or a patch changes, and safe
when several builds run it at once."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FETCH = ROOT / "meson" / "fetch.py"

# Stands in for `meson subprojects download`: slowly unpacks a tree, then "applies the patch".
FAKE_MESON = """#!/bin/sh
top="$4"
tree="$top/subprojects/pkg-1"
echo x >> "$top/downloads"
mkdir -p "$tree"
sleep 0.3
echo "all: ;" > "$tree/Makefile"
sleep 0.3
echo patched >> "$tree/patched"
echo "pkg -> done"
"""


def setup(tmp):
    sub = tmp / "subprojects"
    (sub / "packagefiles").mkdir(parents=True)
    (sub / "pkg.wrap").write_text(
        "[wrap-file]\\ndirectory = pkg-1\\ndiff_files = pkg.diff\\n".replace("\\n", "\n")
    )
    (sub / "packagefiles" / "pkg.diff").write_text("patch one\n")
    meson = tmp / "fake-meson"
    meson.write_text(FAKE_MESON)
    meson.chmod(0o755)
    return meson


def start(tmp, meson, build):
    build.mkdir(exist_ok=True)
    return subprocess.Popen(
        [
            sys.executable,
            str(FETCH),
            "--source-root",
            str(tmp),
            "--stamp",
            str(build / "stamp"),
            "--depfile",
            str(build / "depfile"),
            "pkg",
            "--",
            str(meson),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_concurrent_runs_unpack_and_patch_once(tmp_path):
    meson = setup(tmp_path)
    procs = [start(tmp_path, meson, tmp_path / f"b{i}") for i in range(3)]
    for p in procs:
        out, err = p.communicate()
        assert p.returncode == 0, out + err
    tree = tmp_path / "subprojects" / "pkg-1"
    assert (tree / "Makefile").is_file()
    assert (tree / "patched").read_text() == "patched\n"
    assert len((tmp_path / "downloads").read_text().split()) == 1
    assert (tree / ".wrap-key").read_text().strip() == (
        tmp_path / "b0" / "stamp"
    ).read_text().strip()
    assert (tmp_path / "b2" / "stamp").is_file()


def test_changed_patch_unpacks_again_under_concurrency(tmp_path):
    meson = setup(tmp_path)
    p = start(tmp_path, meson, tmp_path / "b0")
    assert p.wait() == 0
    (tmp_path / "subprojects" / "packagefiles" / "pkg.diff").write_text("patch two\n")
    procs = [start(tmp_path, meson, tmp_path / f"b{i}") for i in (1, 2)]
    assert [q.wait() for q in procs] == [0, 0]
    tree = tmp_path / "subprojects" / "pkg-1"
    assert len((tmp_path / "downloads").read_text().split()) == 2
    assert (tree / "patched").read_text() == "patched\n"
    assert (tree / "Makefile").is_file()
