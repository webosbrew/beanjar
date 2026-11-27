# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""link = "lan" with mode = "mitm" end to end (netns_lan_cases.py), each case a test here. The cases
run together in one namespace of their own, which is the LAN: netns_lan_cases.py under
`unshare -rn`, once per run, reporting each case's outcome."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import netns_lan_cases as cases  # noqa: E402

CASES = sorted(
    t.id().removeprefix("netns_lan_cases.")
    for s in unittest.TestLoader().loadTestsFromModule(cases)
    for t in s
)


@pytest.fixture(scope="module")
def outcomes(top):
    why = cases.unavailable()
    if why:
        pytest.skip(why)
    r = subprocess.run(
        ["unshare", "-rn", sys.executable, str(HERE / "netns_lan_cases.py")],
        stdout=subprocess.PIPE,
        text=True,
        timeout=300,
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                [str(top / "tests/lib"), str(top / "host/core"), os.environ.get("PYTHONPATH", "")]
            ),
        ),
    )
    assert r.returncode == 0, "the harness failed"
    return {
        k.removeprefix("__main__."): v for k, v in json.loads(r.stdout.splitlines()[-1]).items()
    }


@pytest.mark.parametrize("case", CASES)
def test_netns_lan(outcomes, case):
    outcome, detail = outcomes.get(case, ["failed", "did not run"])
    if outcome == "skipped":
        pytest.skip(detail)
    assert outcome == "passed", detail
