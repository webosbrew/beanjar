#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Every target has a TV feature suite expectation file (tests/regress/tv-expect/<target>.toml, written by
tests/regress/tv-expect-gen.py from the target's firmware), or is listed here with the reason it has none (a platform other than webOS has no TV suite)."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
from beanjar.firmware import targets  # noqa: E402

EXPECT_DIR = os.path.join(TOP, "tests", "regress", "tv-expect")

# Targets with no file, and why. The TV suite runs only on webOS (scripts/regress skips the other platforms'
# tv steps), so those targets need no file. A target leaves this list when its firmware joins the collection
# and tv-expect-gen.py writes its file (the test fails for a listed target that has one).
NO_FILE = {}


def webos_targets():
    return {targets.name(p) for p in targets.all_targets() if targets.platform(p) == "webOS"}


class TvExpectFilesTest(unittest.TestCase):
    def test_every_target_has_a_file(self):
        have = {f[:-5] for f in os.listdir(EXPECT_DIR) if f.endswith(".toml")}
        want = webos_targets()
        missing = sorted(want - have - set(NO_FILE))
        self.assertFalse(
            missing,
            "targets with no tests/regress/tv-expect file (run tests/regress/tv-expect-gen.py --target T): "
            + ", ".join(missing),
        )

    def test_no_file_list_is_current(self):
        have = {f[:-5] for f in os.listdir(EXPECT_DIR) if f.endswith(".toml")}
        want = webos_targets()
        self.assertFalse(set(NO_FILE) - want, "NO_FILE names a target that does not exist")
        self.assertFalse(
            sorted(set(NO_FILE) & have), "NO_FILE lists a target that has a file now: remove it"
        )
        self.assertFalse(sorted(have - want), "a tv-expect file of a target that does not exist")


if __name__ == "__main__":
    unittest.main()
