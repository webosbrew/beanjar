#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""tests/regress/lib/camclass.py: the camera app's failures named from log lines, on the lines
docs/research/virtual-camera.md and the research issue record (`pytest tests/unit/regress/test_camclass.py`)."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "regress", "lib")
)
import camclass

# A failing launch: Load, then Play before the load completed, then the event thread's
# onLoadComplete with next_state:kInit (Play, ret:true runs on the event thread).
RACE = [
    "libcbe CameraPlayerInterface Load, ret:true",
    "libcbe CameraPlayerInterface Play, need loaded state_:",
    "libcbe CameraPlayerInterface onLoadComplete, next_state:kInit",
    "libcbe CameraPlayerInterface Play, ret:true",
]
# A working launch: onLoadComplete before Load, ret:true, then PrintDecodedBuffer.
WORKING = [
    "libcbe CameraPlayerInterface onLoadComplete, next_state:kPlaying",
    "libcbe CameraPlayerInterface Load, ret:true",
    "libcbe CameraPlayerInterface PrintDecodedBuffer",
]
USER_MEDIA = "WebAppMgr getUserMedia"
GET_PROPERTIES = "camera2 getProperties"


class Classify(unittest.TestCase):
    def test_race(self):
        cls, evidence = camclass.classify([USER_MEDIA, GET_PROPERTIES] + RACE)
        self.assertEqual(cls, camclass.RACE)
        self.assertEqual(evidence, RACE[:3])
        self.assertEqual(camclass.STATUS[cls], 3)

    def test_working_launch_is_fed(self):
        cls, evidence = camclass.classify([USER_MEDIA] + WORKING)
        self.assertEqual(cls, camclass.FED)
        self.assertEqual(camclass.STATUS[cls], 0)
        self.assertEqual(evidence, WORKING[2:])

    def test_stand_in_decoder_line_is_fed(self):
        cls, _ = camclass.classify(["omxmjpegdec0: host decoding: 40 pictures pushed of 40 inputs"])
        self.assertEqual(cls, camclass.FED)

    def test_need_loaded_before_load_is_not_the_race(self):
        # the order is the evidence: Play's complaint must follow Load, ret:true
        lines = [RACE[1], RACE[0], RACE[2]]
        self.assertEqual(camclass.classify(lines)[0], camclass.OTHER)

    def test_need_loaded_without_load_complete_is_not_the_race(self):
        self.assertEqual(camclass.classify(RACE[:2])[0], camclass.OTHER)

    def test_getproperties_timeout(self):
        cls, evidence = camclass.classify([USER_MEDIA, GET_PROPERTIES, GET_PROPERTIES])
        self.assertEqual(cls, camclass.GETPROPS)
        self.assertIn(GET_PROPERTIES, evidence)
        self.assertEqual(camclass.STATUS[cls], 1)

    def test_app_slow(self):
        cls, _ = camclass.classify([])
        self.assertEqual(cls, camclass.SLOW)
        self.assertEqual(camclass.classify([GET_PROPERTIES])[0], camclass.SLOW)
        self.assertEqual(camclass.STATUS[camclass.SLOW], 1)

    def test_unclassified_keeps_the_lines_seen(self):
        lines = [USER_MEDIA, "libcbe CameraPlayerInterface Load, ret:true"]
        cls, evidence = camclass.classify(lines)
        self.assertEqual(cls, camclass.OTHER)
        self.assertEqual(evidence, lines[1:])

    def test_blank_lines_ignored(self):
        self.assertEqual(camclass.classify(["", "  "] + RACE)[0], camclass.RACE)


if __name__ == "__main__":
    unittest.main()
