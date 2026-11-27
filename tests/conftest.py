# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What every test tier shares (docs/dev/guides/testing.md).

tests/unit needs nothing built; tests/integration finds the host build's outputs through
artefacts.json, which Meson writes into the build directory (tests/meson.build) and points
BEANJAR_TEST_ARTEFACTS at; run directly, the tests read build/host's. A missing artefact
skips the test that needs it, with the reason.

Under `meson test` (BEANJAR_TEST_MESON=1) a run whose every test skipped exits 77, so that
Meson shows it as skipped, as the scripts' own exit 77 did before.
"""

import collections
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOP = Path(__file__).resolve().parent.parent
for p in (TOP / "host/core", TOP / "tests/lib"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

ARTEFACTS = Path(
    os.environ.get("BEANJAR_TEST_ARTEFACTS") or TOP / "build/host/tests/artefacts.json"
)


@pytest.fixture(scope="session")
def top():
    """The checkout."""
    return TOP


@pytest.fixture(scope="session")
def artefacts():
    """The host build's outputs by name (tests/meson.build); skips without a host build."""
    if not ARTEFACTS.is_file():
        pytest.skip(
            f"no host build: {ARTEFACTS} (meson setup build/host; meson compile -C build/host)"
        )
    return json.loads(ARTEFACTS.read_text())


@pytest.fixture(scope="session")
def artefact(artefacts):
    """artefact(name): the path of one built output; skips when this build has none of it."""

    def get(name):
        path = artefacts.get(name, "")
        if not path:
            pytest.skip(f"this build has no {name}")
        if not os.path.exists(path):
            pytest.skip(f"{name} is not built: {path}")
        return path

    return get


def _run_check(argv, timeout, env=None, cwd=None):
    """Run a check program; its exit 77 is a skip (the reason from its last output line), any
    other failure fails the test with the end of its output."""
    try:
        r = subprocess.run(
            [str(a) for a in argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            env=None if env is None else {**os.environ, **env},
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else e.stdout or ""
        pytest.fail(f"{Path(str(argv[0])).name}: timed out after {timeout} s\n{out[-4000:]}")
    sys.stdout.write(r.stdout)
    if r.returncode == 77:
        lines = [x for x in r.stdout.splitlines() if x.strip()]
        pytest.skip(lines[-1].strip() if lines else "skipped (exit 77)")
    if r.returncode:
        pytest.fail(f"exit status {r.returncode}\n{r.stdout[-4000:]}", pytrace=False)
    return r


@pytest.fixture(scope="session")
def run_check():
    """run_check(argv, timeout, env=None, cwd=None): a check program as one test (_run_check)."""
    return _run_check


_outcomes = collections.Counter()


def pytest_runtest_logreport(report):
    if report.when == "call" or report.outcome != "passed":
        _outcomes[report.outcome] += 1


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("BEANJAR_TEST_MESON") and exitstatus == 0 and set(_outcomes) == {"skipped"}:
        session.exitstatus = 77
