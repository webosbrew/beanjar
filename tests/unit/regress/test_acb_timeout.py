# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""tests/regress/video/acb-timeout.sh: a run in which AcbService's pipeline timed out has no
result for a test of an app video's area; the log lines before a mark are not that run's."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "tests/regress/video/acb-timeout.sh"
FINE = "2026-11-21T10:00:00Z [10.0] user.info AcbService [] ACB setCustomDisplayWindow done\n"
TIMED_OUT = (
    "2026-11-21T10:00:10Z [20.0] user.warning AcbService [] ACB eventType: timeout, "
    "Timeout To Receive Luna API Response\n"
)
OTHER = "2026-11-21T10:00:11Z [21.0] user.warning other [] Timeout To Receive Luna API Response\n"


class AcbTimeout(unittest.TestCase):
    def check(self, text, *args):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "messages")
            Path(log).write_text(text)
            r = subprocess.run(
                ["sh", str(SCRIPT), *args],
                env=dict(os.environ, LOG=log),
                capture_output=True,
                text=True,
                check=False,
            )
        return r.returncode, r.stdout.strip()

    def test_no_timeout(self):
        self.assertEqual(self.check(FINE + OTHER), (0, "ACB OK"))

    def test_timeout_is_no_result(self):
        rc, out = self.check(FINE + TIMED_OUT)
        self.assertEqual(rc, 3)
        self.assertTrue(out.startswith("ACB TIMEOUT 1:"), out)

    def test_mark_skips_earlier_lines(self):
        self.assertEqual(self.check(FINE + TIMED_OUT, "--mark"), (0, "3"))
        self.assertEqual(self.check(FINE + TIMED_OUT, "3"), (0, "ACB OK"))
        self.assertEqual(self.check(FINE + TIMED_OUT + TIMED_OUT, "3")[0], 3)


if __name__ == "__main__":
    unittest.main()
