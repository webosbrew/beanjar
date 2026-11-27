# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The gl layer's GBM cursor transport (guest/layers/gl/src/kms-cursor-compat.c) against a local DRM
backend. check-cursor-compat.py loads the library it builds with ctypes, so it runs as its own
process: a crash there fails this test instead of ending the run."""

import sys
from pathlib import Path


def test_cursor_compat(run_check):
    run_check([sys.executable, Path(__file__).with_name("check-cursor-compat.py")], timeout=120)
