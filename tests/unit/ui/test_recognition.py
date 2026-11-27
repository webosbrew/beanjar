#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The viewer's Elements layer without GTK or a display (beanjar_ui/viewer/recognition.py): the
throttle below 15 readings a second, dropping frames while busy, readings to boxes in the view,
the status line, and the layer switched on and off around a fake reader."""

import os
import sys
import threading

import pytest

for package in ("ui", "core"):
    sys.path.append(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "host", package)
    )
from beanjar_ui.viewer import main  # noqa: E402
from beanjar_ui.viewer import recognition as r  # noqa: E402


def test_throttle_below_15_per_second():
    t = r.Throttle()
    assert t.interval > 1 / 15
    taken, now = 0, 0.0
    while now < 10.0:  # an instant reader offered a frame every millisecond for 10 s
        if t.take(now):
            taken += 1
            t.done()
        now += 0.001
    assert taken / 10.0 < 15


def test_throttle_drops_while_busy():
    t = r.Throttle()
    assert t.take(0.0)
    assert not t.take(1.0) and not t.take(2.0)  # still reading: both frames dropped
    assert t.dropped == 2
    t.done()
    assert t.take(2.0)  # the next frame after the reading ends


def test_rate_meter():
    m = r.RateMeter(window=4)
    assert m.rate() is None
    for s in (0.0, 2.0, 4.0, 6.0, 8.0):
        m.tick(s)
    assert m.rate() == pytest.approx(0.5)


DATA = {
    "width": 1280,
    "height": 720,
    "elements": [
        {"kind": "button", "text": "OK", "box": [100, 50, 300, 90], "value": None, "unit": ""},
        {"kind": "progress", "text": "", "box": [0, 700, 1280, 720], "value": 40, "unit": "%"},
    ],
}


def test_overlay_items_scale_and_offset():
    # the picture shown at 640x360 from (10, 20): half size, shifted
    items = r.overlay_items(DATA, (10, 20, 640, 360))
    assert items[0] == ("button", "OK", (60.0, 45.0, 100.0, 20.0))
    assert items[1] == ("progress", "40%", (10.0, 370.0, 640.0, 10.0))


def test_overlay_items_letterboxed_frame():
    # a 4:3 display in the 1280x720 frame: the picture is the frame's middle 960x720
    data = {
        "width": 1280,
        "height": 720,
        "elements": [dict(DATA["elements"][0], box=[160, 0, 1120, 720])],
    }
    assert r.overlay_items(data, (0, 0, 800, 600)) == [("button", "OK", (0.0, 0.0, 800.0, 600.0))]
    assert r.overlay_items(data, (0, 0, 0, 0)) == []


def test_status_line():
    assert "first frame" in r.status_line(None, None, 0, busy=True)
    line = r.status_line({"taken": 10.0, "data": DATA}, 0.25, 12.5)
    assert line == "Elements: 2 · 0.25/s · 2.5 s old"
    assert r.status_line({"taken": 0, "error": "go-textshot timed out"}, None, 1) == (
        "Elements: go-textshot timed out"
    )


PPM = b"P6 2 2 255 " + bytes(12)


class Loop:
    """The main loop: posted callbacks run when drained."""

    def __init__(self):
        self.posted, self.lock = [], threading.Lock()

    def post(self, fn):
        with self.lock:
            self.posted.append(fn)

    def drain(self):
        with self.lock:
            fns, self.posted = self.posted, []
        for fn in fns:
            fn()


def make(read, clock):
    loop, results, grabs = Loop(), [], []

    def grab(path, w, h):
        grabs.append((w, h))
        with open(path, "wb") as f:
            f.write(PPM)
        return True

    rec = r.Recognizer(
        grab, lambda: (1920, 1080), loop.post, results.append, read=read, clock=clock
    )
    return rec, loop, results, grabs


def test_recognizer_one_reading_at_a_time():
    gate, seen = threading.Event(), []
    now = [100.0]

    def read(png, _binary):
        with open(png, "rb") as f:
            seen.append(f.read(8))
        gate.wait(5)
        return {"width": 2, "height": 2, "elements": []}

    rec, loop, results, grabs = make(read, lambda: now[0])
    rec.start()
    assert rec.offer()
    now[0] += 1
    assert not rec.offer()  # busy: dropped
    assert rec.throttle.dropped == 1
    gate.set()
    for _ in range(100):
        if loop.posted:
            break
        threading.Event().wait(0.01)
    loop.drain()
    assert grabs == [r.FRAME_SIZE]
    assert seen == [b"\x89PNG\r\n\x1a\n"]  # the frame went to the reader as PNG
    assert results and results[0]["data"]["elements"] == []
    assert results[0]["png"].startswith(b"\x89PNG")
    now[0] += 1
    assert rec.offer()  # free again
    rec.stop()


def test_recognizer_error_and_stop():
    def read(_png, _binary):
        raise RuntimeError("go-textshot exited with status 2\nmore")

    rec, loop, results, _grabs = make(read, lambda: 5.0)
    rec.start()
    tmp = rec.tmp
    assert rec.offer()
    for _ in range(100):
        if loop.posted:
            break
        threading.Event().wait(0.01)
    loop.drain()
    assert results[0]["error"] == "go-textshot exited with status 2"
    assert rec.meter.rate() is None
    rec.stop()
    assert not os.path.exists(tmp)
    assert not rec.offer()  # off: no frames taken


def test_recognizer_start_needs_textshot(monkeypatch):
    from beanjar.base import Error
    from beanjar.display import elements

    monkeypatch.setenv("BEANJAR_TEXTSHOT", "/nonexistent/textshot")
    rec = r.Recognizer(lambda *_: True, lambda: None, lambda fn: fn(), lambda _r: None)
    with pytest.raises(Error, match="not installed"):
        rec.start()
    assert rec.tmp is None and elements.INSTALL_HINT


def test_viewer_options():
    o = main.parse(["unix:path=/x", "--elements", "--grab", "out/g.png", "--grab-every", "500"])
    assert o["elements"] and o["grab"] == "out/g.png" and o["grab_every"] == 500
    with pytest.raises(SystemExit):
        main.parse(["unix:path=/x", "--grab", "g.png"])
    assert main.grab_name("out/g.png", 1500) == "out/g-001500.png"
    assert main.grab_name("g", 2, ".elements.json") == "g-000002.elements.json"
