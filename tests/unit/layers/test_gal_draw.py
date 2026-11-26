#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build and run the check of the soc layer's DILE_GAL 2D engine (gal-draw.c) on the host, also as
32-bit ARM code under qemu-user when the webOS SDK is installed (the guest's compiler; static, as
qemu-user finds no loader of the SDK's)."""

import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
CHECK = ROOT / "tests/unit/layers/check-gal-draw.c"
SDK_CC = pathlib.Path("/opt/arm-webos-linux-gnueabi_sdk-buildroot/bin/arm-webos-linux-gnueabi-gcc")


def build_and_run(cc, run_prefix=(), extra=()):
    with tempfile.TemporaryDirectory(prefix="beanjar-gal-draw-") as directory:
        test = pathlib.Path(directory) / "check-gal-draw"
        subprocess.run(
            [
                str(cc),
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-g",
                *extra,
                str(CHECK),
                "-o",
                str(test),
            ],
            check=True,
        )
        result = subprocess.run(
            [*run_prefix, str(test)], capture_output=True, text=True, timeout=60, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == "ok"


def test_gal_draw_host():
    build_and_run("cc")


def test_gal_draw_arm():
    qemu = shutil.which("qemu-arm") or shutil.which("qemu-arm-static")
    if not SDK_CC.exists() or not qemu:
        pytest.skip("no webOS SDK or qemu-arm")
    build_and_run(SDK_CC, (qemu,), ("-static",))
