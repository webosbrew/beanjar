# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The environment of every unit test: nothing of the machine it runs on.

HOME and the XDG config, data, cache, state and runtime directories are empty temporary ones, and
the variables that point beanjar at a library, a host or a settings file are removed, so the hosts,
libraries and settings registered on a developer's machine cannot make a test pass (or fail) that a
clean machine, like the CI, would treat the other way. Done at import, before any test module is
collected, because modules read these when they are imported. BEANJAR_TEST_* (what the test tiers
use among themselves, tests/conftest.py) stay.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_home = Path(tempfile.mkdtemp(prefix="beanjar-unit-home-"))
atexit.register(shutil.rmtree, _home, ignore_errors=True)

for _name in [
    n
    for n in os.environ
    if n.startswith(("BEANJAR_", "XDG_")) and not n.startswith("BEANJAR_TEST_")
]:
    del os.environ[_name]

os.environ["HOME"] = str(_home)
for _var, _sub in (
    ("XDG_CONFIG_HOME", ".config"),
    ("XDG_DATA_HOME", ".local/share"),
    ("XDG_CACHE_HOME", ".cache"),
    ("XDG_STATE_HOME", ".local/state"),
    ("XDG_RUNTIME_DIR", "run"),
):
    _dir = _home / _sub
    _dir.mkdir(parents=True, mode=0o700)
    os.environ[_var] = str(_dir)
