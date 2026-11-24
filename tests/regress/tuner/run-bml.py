#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The ARIB data broadcast (BML) page over Live TV on a running VM of this machine with an ARIB
board and our tuner, pass or fail with numbers.

  run-bml.py NAME [--timeout S] [--shot FILE] [--menu-shot FILE]

After a scan (run-scan.py), with the ISDB-T channel mapped and a front end that takes remote
keys (beanjar vnc start NAME, or the viewer): brings Live TV to the front without
closing it, and launches it again while Home covers it (Home comes over Live TV once after a
boot, and the set runs the page only with Live TV in front). Then waits, up to S seconds (default 300), for the generator's page
(host/core/beanjar/tuner_stream_arib.py: a blue panel over the lower third, white text on it, the
video above it): screenshots are measured, nothing is read from the guest's logs. Then presses
d twice: the first hides the page, the second shows it again. Then the blue key, which opens the
page's second document (menu.bml): the video scaled into a window of a quarter of the screen near
the top left (tuner_stream_arib.WINDOW), the page's navy around it; the blue key again goes back.

Prints the numbers as JSON and PASS or FAIL; the exit status is 0 only for PASS:
  page_s     seconds from the first launch until the page is seen
  launches   Live TV launches it took (Home in front again counts one)
  panel      share of the panel's area that is the page's blue
  text       share of the panel's area that is white (the two text lines)
  video_lit  share of the area above the panel that is not dark (the video through the page)
  d_hide_s, d_show_s  seconds until the first d hid the page, the second showed it again
  menu_s     seconds from the blue key until the menu is seen
  page_out   share of the screen outside the video window that is the menu's navy
  page_in    share of the window that is navy (the page drawn over the video)
  window_lit share of the window that is not dark
  bars       the colour bars seen in order across the window (7: the whole picture is in it;
             a picture left full screen behind the window shows about half of them)
  bars_x     the first bar's left and the last bar's right edge in the 1280-wide screenshot
  back_s     seconds from the second blue key until the first page is seen again"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "host", "core")
)

from beanjar import tuner_stream_arib as arib

BEANJAR = os.environ.get("BEANJAR", "beanjar")
LIVETV = "com.webos.app.livetv"
# the page as a 1280x720 screenshot shows it: the panel is 960x540's y 360..540
PANEL = (0, 480, 1280, 720)
ABOVE = (0, 0, 1280, 470)
MIN_PANEL, MIN_TEXT, MIN_LIT = 0.6, 0.01, 0.5
# the menu's video window, from the page's 960x540 to the 1280x720 screenshot
WINDOW = tuple(round(v * 4 / 3) for v in arib.WINDOW)
# the bars' dark bottom rows leave about 2/3 of a full picture lit, and a 4:3 picture may be
# pillarboxed in the 16:9 window
MIN_PAGE_OUT, MAX_PAGE_IN, MIN_WINDOW_LIT = 0.6, 0.1, 0.35
# SMPTE bars at 75%, left to right (ffmpeg smptebars), and the height across the window to read
BARS = [
    (191, 191, 191),
    (191, 191, 0),
    (0, 191, 191),
    (0, 191, 0),
    (191, 0, 191),
    (191, 0, 0),
    (0, 0, 191),
]
BARS_Y = 0.3
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


def is_navy(p):
    """The menu's background, default CLUT 19 (0, 85, 170)."""
    return p[0] < 40 and 55 < p[1] < 115 and 130 < p[2] < 210


def measure_menu(path):
    """{page_out, page_in, window_lit, bars, bars_x} of a 1280x720 screenshot of the menu."""
    from PIL import Image

    im = Image.open(path).convert("RGB").resize((1280, 720))
    x, y, w, h = WINDOW
    px = im.load()
    out_n = out_navy = in_n = in_navy = in_lit = 0
    for j in range(0, 720, 2):
        for i in range(0, 1280, 2):
            p = px[i, j]
            inside = x + 6 <= i < x + w - 6 and y + 6 <= j < y + h - 6
            near = x - 6 <= i < x + w + 6 and y - 6 <= j < y + h + 6
            if inside:
                in_n += 1
                in_navy += is_navy(p)
                in_lit += (p[0] * 299 + p[1] * 587 + p[2] * 114) / 1000 > 48
            elif not near:
                out_n += 1
                out_navy += is_navy(p)
    # the bars along one line: runs of one bar colour at least 1/4 of a full bar's width
    row = [px[i, y + int(h * BARS_Y)] for i in range(x, x + w)]

    def bar(p):
        d, k = min((sum((a - b) ** 2 for a, b in zip(p, c)), k) for k, c in enumerate(BARS))
        return k if d < 60**2 * 3 else None

    runs, start = [], 0
    for i in range(1, len(row) + 1):
        if i == len(row) or bar(row[i]) != bar(row[start]):
            if bar(row[start]) is not None and i - start >= w // 28:
                runs.append((bar(row[start]), x + start, x + i))
            start = i
    seen, want = [], 0
    for k, a, b in runs:
        if k == want:
            seen.append((a, b))
            want += 1
    return {
        "page_out": round(out_navy / out_n, 2),
        "page_in": round(in_navy / in_n, 2),
        "window_lit": round(in_lit / in_n, 2),
        "bars": len(seen),
        "bars_x": [seen[0][0], seen[-1][1]] if seen else None,
    }


def wait_menu(name, path, timeout, step=3):
    """Seconds until the menu is shown, or None."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        shot(name, path)
        if measure_menu(path)["page_out"] >= MIN_PAGE_OUT:
            return round(time.monotonic() - t0, 1)
        time.sleep(step)
    return None


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
    ap.add_argument("--menu-shot", help="keep the screenshot of the menu (video window) here")
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
            press(a.name, "BLUE")
            numbers["menu_s"] = wait_menu(a.name, path, 60)
            if numbers["menu_s"] is not None:
                time.sleep(5)  # the video settles in its window
                shot(a.name, path)
                numbers.update(measure_menu(path))
                if a.menu_shot:
                    shot(a.name, a.menu_shot)
                press(a.name, "BLUE")
                numbers["back_s"] = wait_page(a.name, path, True, 60)
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
        if numbers.get("menu_s") is None:
            fails.append("the blue key did not open the menu")
        else:
            if numbers["page_in"] > MAX_PAGE_IN or numbers["window_lit"] < MIN_WINDOW_LIT:
                fails.append("the video window is not open in the menu")
            if numbers["bars"] < len(BARS):
                fails.append("the picture is not scaled into the video window")
            if numbers["back_s"] is None:
                fails.append("the blue key did not go back to the first page")
    if fails:
        print("FAIL: " + "; ".join(fails))
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
