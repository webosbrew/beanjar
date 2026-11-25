# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""tests/regress/tuner/run-watch.py: the media logs read through `beanjar log show`, so that a VM
on another host (HOST/NAME) is measured from here; no VM, no beanjar."""

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

TOP = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("run_watch", TOP / "tests/regress/tuner/run-watch.py")
rw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rw)

SESSIONS = (
    "2026-11-18T10:00:00Z media 2: open video tuner-vdec0\n"
    "2026-11-18T10:00:00Z media 3: open audio tuner-adec0\n"
    "2026-11-18T10:00:09Z media 4: open video app\n"
    "2026-11-18T10:01:00Z media 5: open video tuner-vdec0\n"
)
HELPER = (
    "decode 5 video session=1 codec=mpeg2 decoded=10\n"
    "decode 3 audio session=1 codec=mp2 decoded=7\n"
    "decode 2 video session=1 codec=mpeg2 decoded=999\n"
    "decode 5 video session=1 codec=mpeg2 decoded=40\n"
    "decode 5 video session=1 codec=mpeg2 dropped=1\n"
    "decode 3 audio session=1 codec=mp2 decoded=31\n"
)


def done(stdout, rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


class Parse(unittest.TestCase):
    def test_sessions_last_open_of_each_kind(self):
        self.assertEqual(rw.sessions(SESSIONS), {"video": 5, "audio": 3})
        self.assertEqual(rw.sessions(""), {})

    def test_decoded_last_count_of_the_port(self):
        self.assertEqual(rw.decoded(HELPER, "video", 5), 40)
        self.assertEqual(rw.decoded(HELPER, "audio", 3), 31)
        self.assertEqual(rw.decoded(HELPER, "video", None), 0)
        self.assertEqual(rw.decoded("", "video", 5), 0)

    def test_decoded_stays_on_its_line(self):
        # a count on the next line is not this line's
        text = "decode 5 video session=1 opened\ndecode 6 video session=1 decoded=9\n"
        self.assertEqual(rw.decoded(text, "video", 5), 0)


class Log(unittest.TestCase):
    def test_reads_through_beanjar_log_show(self):
        reply = json.dumps({"name": "tv", "file": "run/media/media-helper.log", "text": HELPER})
        with mock.patch.object(rw.subprocess, "run", return_value=done(reply)) as run:
            self.assertEqual(rw.log("rk-1/tv", "run/media/media-helper.log"), HELPER)
        argv = run.call_args.args[0]
        self.assertEqual(
            argv[1:], ["log", "show", "rk-1/tv", "run/media/media-helper.log", "--json"]
        )

    def test_no_log_is_empty(self):
        for result in (done("", 1), done("not json"), done("{}")):
            with (
                self.subTest(result=result),
                mock.patch.object(rw.subprocess, "run", return_value=result),
            ):
                self.assertEqual(rw.log("tv", "run/media/media-sessions.log"), "")

    def test_helper_counts(self):
        reply = json.dumps({"text": HELPER})
        with mock.patch.object(rw.subprocess, "run", return_value=done(reply)) as run:
            self.assertEqual(rw.helper_counts("pi/tv", {"video": 5, "audio": 3}), (40, 31))
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
