#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The ARIB data broadcast (BML) page over Live TV on a running VM of this machine with an ARIB
board and our tuner, pass or fail with numbers.

  run-bml.py NAME [--timeout S] [--shot FILE]

After a scan (run-scan.py), with the ISDB-T channel mapped: brings Live TV to the front without
closing it, and launches it again while Home covers it (Home comes over Live TV once after a
boot, and the set runs the page only with Live TV in front). Then waits, up to S seconds (default 300), for the generator's page
(host/core/beanjar/tuner_stream_arib.py: a blue panel over the lower third, white text on it, the
video above it): screenshots are measured, nothing is read from the guest's logs. Then presses
d twice: the first hides the page, the second shows it again.

Prints the numbers as JSON and PASS or FAIL; the exit status is 0 only for PASS:
  page_s     seconds from the first launch until the page is seen
  launches   Live TV launches it took (Home in front again counts one)
  panel      share of the panel's area that is the page's blue
  text       share of the panel's area that is white (the two text lines)
  video_lit  share of the area above the panel that is not dark (the video through the page)
  d_hide_s, d_show_s  seconds until the first d hid the page, the second showed it again"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

BEANJAR = os.environ.get("BEANJAR", "beanjar")
LIVETV = "com.webos.app.livetv"
# the page as a 1280x720 screenshot shows it: the panel is 960x540's y 360..540
PANEL = (0, 480, 1280, 720)
ABOVE = (0, 0, 1280, 470)
MIN_PANEL, MIN_TEXT, MIN_LIT = 0.6, 0.01, 0.5
LAUNCH_GAP = 20  # seconds between launches of Live TV


def env():
    e = dict(os.environ)
    e["PATH"] = os.path.expanduser("~/.cargo/bin") + os.pathsep + e.get("PATH", "")
    e.setdefault("QT_QPA_PLATFORM", "offscreen")
    return e


def luna(name, uri, payload, timeout=60):
    r = subprocess.run(
        [
            BEANJAR,
            "ares-shell",
            "--root",
            "-d",
            name,
            "-r",
            f"luna-send -n 1 -w 30000 {uri} '{json.dumps(payload)}'",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env(),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {}


def foreground(name):
    return luna(name, "luna://com.webos.service.applicationmanager/getForegroundAppInfo", {}).get(
        "appId", ""
    )


def launch(name):
    luna(name, "luna://com.webos.service.applicationmanager/launch", {"id": LIVETV})


def press(name, key):
    subprocess.run(
        [BEANJAR, "remote", "press", name, key], capture_output=True, env=env(), check=True
    )


def shot(name, path):
    subprocess.run([BEANJAR, "screenshot", name, path], capture_output=True, env=env(), check=True)


def measure(path):
    """(panel, text, video_lit) of a 1280x720 screenshot."""
    from PIL import Image

    im = Image.open(path).convert("RGB").resize((1280, 720))

    def shares(box, *tests):
        px = list(im.crop(box).getdata())
        return [sum(1 for p in px if t(p)) / len(px) for t in tests]

    blue = lambda p: p[0] < 40 and p[1] < 40 and p[2] > 200  # noqa: E731
    white = lambda p: min(p) > 200  # noqa: E731
    lit = lambda p: (p[0] * 299 + p[1] * 587 + p[2] * 114) / 1000 > 48  # noqa: E731
    panel, text = shares(PANEL, blue, white)
    (above,) = shares(ABOVE, lit)
    return panel, text, above


def wait_page(name, path, shown, timeout, step=3):
    """Seconds until the page is shown (shown True) or gone (False), or None."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        shot(name, path)
        panel, text, _ = measure(path)
        if (panel >= MIN_PANEL and text >= MIN_TEXT) == shown:
            return round(time.monotonic() - t0, 1)
        time.sleep(step)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--shot", help="keep the screenshot of the page here")
    a = ap.parse_args()
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        # a set just started shows nothing in front until Home is up
        for _ in range(60):
            if foreground(a.name):
                break
            time.sleep(5)
        t0, launches, page_s, last = time.monotonic(), 0, None, -LAUNCH_GAP
        while time.monotonic() - t0 < a.timeout:
            fg = foreground(a.name)
            # another app in front (Home), not a moment without one; a launch takes a while
            if fg not in ("", LIVETV) and time.monotonic() - last >= LAUNCH_GAP:
                launch(a.name)
                launches, last = launches + 1, time.monotonic()
            shot(a.name, path)
            panel, text, _ = measure(path)
            if panel >= MIN_PANEL and text >= MIN_TEXT:
                page_s = round(time.monotonic() - t0, 1)
                break
            time.sleep(5)
        numbers = {"page_s": page_s, "launches": launches}
        if page_s is not None:
            time.sleep(5)  # the video behind the page settles
            shot(a.name, path)
            panel, text, above = measure(path)
            if a.shot:
                shot(a.name, a.shot)
            numbers.update(panel=round(panel, 2), text=round(text, 3), video_lit=round(above, 2))
            press(a.name, "DATA")
            numbers["d_hide_s"] = wait_page(a.name, path, False, 30)
            press(a.name, "DATA")
            numbers["d_show_s"] = wait_page(a.name, path, True, 60)
    finally:
        os.unlink(path)
    print(json.dumps(numbers))
    fails = []
    if page_s is None:
        fails.append(f"no page in {a.timeout} s")
    else:
        if numbers["video_lit"] < MIN_LIT:
            fails.append("the video above the page is dark")
        if numbers["d_hide_s"] is None:
            fails.append("d did not hide the page")
        if numbers["d_show_s"] is None:
            fails.append("d did not show the page again")
    if fails:
        print("FAIL: " + "; ".join(fails))
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
