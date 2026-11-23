# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""tests/regress/netcast/run.py's judge without a VM: a black screen, the MyApps tray and the
empty tile skeleton are not the dashboard; a busy full screen is."""

import importlib.util
import os
import random
import tempfile
import unittest

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
spec = importlib.util.spec_from_file_location(
    "netcast_check", os.path.join(TOP, "tests", "regress", "netcast", "run.py")
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class Judge(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def png(self, name, draw):
        im = Image.new("RGB", (1280, 720))
        draw(im, ImageDraw.Draw(im))
        path = os.path.join(self.dir.name, name + ".png")
        im.save(path)
        return path

    def verdict(self, path, platform="NetCast", accel="kvm"):
        return check.judge(*check.measure(path), platform, accel)

    def test_black(self):
        path = self.png("black", lambda im, d: None)
        self.assertEqual(check.measure(path), (0, 1))
        self.assertFalse(self.verdict(path))
        self.assertFalse(self.verdict(path, accel="tcg"))

    def test_tray(self):
        # the bottom tray over black: an eighth of the screen
        path = self.png("tray", lambda im, d: d.rectangle((0, 630, 1279, 719), fill=(90, 10, 40)))
        self.assertFalse(self.verdict(path))
        self.assertFalse(self.verdict(path, accel="tcg"))

    def test_skeleton(self):
        # the dashboard's grey tiles with nothing in them: most of the screen, few colours
        def tiles(im, d):
            for box in ((534, 114, 1272, 482), (6, 410, 532, 482), (6, 484, 1272, 633)):
                d.rectangle(box, fill=(74, 75, 80), outline=(40, 40, 40))

        path = self.png("skeleton", tiles)
        nonblack, _colours = check.measure(path)
        self.assertGreater(nonblack, 0.25)
        self.assertFalse(self.verdict(path))
        # under TCG the skeleton is what a working VM shows: drawn, not black
        self.assertTrue(self.verdict(path, accel="tcg"))

    def test_dashboard(self):
        # tiles full of pictures: most of the screen, thousands of colours
        rnd = random.Random(304)

        def busy(im, d):
            for y in range(0, 720, 4):
                for x in range(0, 1280, 4):
                    if y > 100:
                        c = (rnd.randrange(32, 256), rnd.randrange(256), rnd.randrange(256))
                        d.rectangle((x, y, x + 3, y + 3), fill=c)

        path = self.png("dashboard", busy)
        self.assertTrue(self.verdict(path))

    def test_differs(self):
        a = self.png("a", lambda im, d: d.rectangle((0, 0, 9, 9), fill=(255, 0, 0)))
        b = self.png("b", lambda im, d: d.rectangle((0, 0, 9, 4), fill=(255, 0, 0)))
        self.assertEqual(check.differs(a, a), 0)
        self.assertEqual(check.differs(a, b), 50)


class Locale(unittest.TestCase):
    """The country group the browser last reported, as webos.log has it."""

    LINE = (
        b"000087.048810:lb6wk   >lb6wk   ] ~~~ [libtv](BROWSER_GetLocaleGroup) locale group : %d,"
        b" locale string : %s\n"
    )

    def test_reported(self):
        log = b"noise\n" + self.LINE % (1, b"KR") + b"more\n" + self.LINE % (4, b"EU")
        self.assertEqual(check.reported_group(log), "EU")
        self.assertEqual(check.reported_group(self.LINE % (1, b"KR")), "KR")
        self.assertIsNone(check.reported_group(b"no browser yet\n"))


class FirmwareDir(unittest.TestCase):
    """Where the locale step reads the firmware's RELEASE.cfg: a VM made from a .epk has its own
    firmware/, one made from a collection name or a directory boots that one in place."""

    def test_own(self):
        for fw in (None, ""):
            info = {"dir": "/vms/a", "fw": fw}
            self.assertEqual(str(check.firmware_dir(info)), "/vms/a/firmware")
        self.assertEqual(str(check.firmware_dir({"dir": "/vms/a"})), "/vms/a/firmware")

    def test_in_place(self):
        info = {"dir": "/vms/a", "fw": "/srv/fw/04.11.18.01-HE_LCD_NC5U_AAADAIAA"}
        self.assertEqual(str(check.firmware_dir(info)), "/srv/fw/04.11.18.01-HE_LCD_NC5U_AAADAIAA")
        self.assertEqual(str(check.firmware_dir({"dir": "/vms/a", "fw": "../fw/x"})), "/vms/fw/x")


if __name__ == "__main__":
    unittest.main()
