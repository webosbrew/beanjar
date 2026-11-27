#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Every file in the tree carries copyright and licence information (REUSE)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(shutil.which("reuse") is None, reason="reuse is not installed")
@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_reuse_lint():
    result = subprocess.run(
        ["reuse", "--root", str(ROOT), "lint"], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
