#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build and run the Mali native pixmap check on the host's Mesa (software, surfaceless)."""

import os
import pathlib
import subprocess
import tempfile

import pytest


def test_mali_pixmap():
    root = pathlib.Path(__file__).resolve().parents[3]
    if not all(
        pathlib.Path(p).exists() for p in ("/usr/include/EGL/eglext.h", "/usr/include/GLES2/gl2.h")
    ):
        pytest.skip("EGL and GLES development headers unavailable")
    with tempfile.TemporaryDirectory(prefix="beanjar-mali-pixmap-") as directory:
        test = pathlib.Path(directory) / "check-mali-pixmap"
        subprocess.run(
            [
                "cc",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-g",
                str(root / "tests/unit/gl/check-mali-pixmap.c"),
                "-lEGL",
                "-lGLESv2",
                "-ldl",
                "-lpthread",
                "-o",
                str(test),
            ],
            check=True,
        )
        env = dict(os.environ, LIBGL_ALWAYS_SOFTWARE="1")
        result = subprocess.run([str(test)], capture_output=True, text=True, timeout=60, env=env)
        if result.returncode == 2:
            pytest.skip(result.stdout.strip() or "no surfaceless GLES on this host")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == "ok"
