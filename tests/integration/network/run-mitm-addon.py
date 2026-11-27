#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run test_mitm_addon.py with mitmproxy importable: under the Python of the user's own mitmweb
(host/core/beanjar/network/mitmtool.py finds it; a pipx install's venv interpreter, from its shebang),
with the system's pytest after that venv's own packages. Exits 77 (a Meson skip), saying why,
without a usable mitmweb."""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "host/core"))
from beanjar.network import mitmtool  # noqa: E402

tool = mitmtool.check()
python = mitmtool.python(tool.path) if tool.usable else None
if not python:
    why = tool.problem or f"no Python found in the shebang of {tool.path}"
    print(f"mitm_addon.py's tests skipped: {why}")
    sys.exit(77)
print(f"mitmproxy {tool.version}: {python}", flush=True)
system = "/usr/lib/python3/dist-packages"
program = f"import sys; sys.path.append({system!r}); import pytest; sys.exit(pytest.main())"
argv = [python, "-c", program, "-q", "--no-header", "-p", "no:cacheprovider"]
os.execv(python, argv + [str(HERE / "test_mitm_addon.py"), *sys.argv[1:]])
