# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The Elements layer's recognition (host/viewer/README.md, "Elements layer"): frames of the
TV picture read by beanjar.elements, the reading `beanjar elements` does, off the main loop.

No GTK here: the layer's widget is elements_layer.py. A frame is taken on the main loop (the
core's screenshot, a GL read back) at most MAX_RATE times a second and only while no reading is
in flight; frames that come meanwhile are dropped, never queued. The reading (PPM to PNG and
go-textshot) runs in a thread, and its result comes back on the main loop."""

import collections
import os
import shutil
import tempfile
import threading
import time

# Below 15 readings a second, whatever go-textshot could do: the layer is a debugging aid.
MAX_RATE = 14
# The frame `beanjar elements NAME` reads (Library.SCREENSHOT_SIZE), so the boxes are its boxes.
FRAME_SIZE = (1280, 720)


class Throttle:
    """When to take a frame: not while a reading is busy, and not sooner than 1 / rate after the
    last one was taken. A frame offered while busy is dropped (counted)."""

    def __init__(self, rate=MAX_RATE):
        self.interval = 1.0 / rate
        self.last = None  # when the last frame was taken
        self.busy = False
        self.dropped = 0

    def take(self, now):
        if self.busy:
            self.dropped += 1
            return False
        if self.last is not None and now - self.last < self.interval:
            return False
        self.last, self.busy = now, True
        return True

    def done(self):
        self.busy = False


class RateMeter:
    """Readings finished per second over the last few (None before two)."""

    def __init__(self, window=8):
        self.times = collections.deque(maxlen=window)

    def tick(self, now):
        self.times.append(now)

    def rate(self):
        if len(self.times) < 2 or self.times[-1] <= self.times[0]:
            return None
        return (len(self.times) - 1) / (self.times[-1] - self.times[0])


def picture_box(frame, picture):
    """(x, y, w, h) of the TV picture in a frame of frame = (width, height): the core letterboxes
    the guest's display into a screenshot as it does into the view, so the shown picture's aspect
    (picture = (width, height)) places it (beanjar.framing.picture_box)."""
    from beanjar import framing

    return framing.picture_box(frame, picture)


def overlay_items(data, rect):
    """The boxes of a reading (data: beanjar.elements.read_screen's) in the view's coordinates,
    the picture shown at rect = (x, y, w, h): [(kind, text, (x, y, w, h)), ...] in reading order.
    The label is the element's text, its value and unit for a progress bar."""
    rx, ry, rw, rh = rect
    if rw <= 0 or rh <= 0 or not data:
        return []
    bx, by, bw, bh = picture_box((data["width"], data["height"]), (rw, rh))
    if bw <= 0 or bh <= 0:
        return []
    sx, sy = rw / bw, rh / bh
    items = []
    for e in data["elements"]:
        x0, y0, x1, y1 = e["box"]
        text = e.get("text") or ""
        if e.get("value") is not None:
            text = (text + " " if text else "") + f"{e['value']:g}{e.get('unit') or ''}"
        items.append(
            (
                e["kind"],
                text,
                (rx + (x0 - bx) * sx, ry + (y0 - by) * sy, (x1 - x0) * sx, (y1 - y0) * sy),
            )
        )
    return items


def status_line(result, rate, now, busy=False):
    """The layer's own line: elements, the measured reading rate and the shown result's age."""
    if result is None:
        return "Elements: reading the first frame…" if busy else "Elements: no frame yet"
    if result.get("error"):
        return f"Elements: {result['error']}"
    parts = [f"Elements: {len(result['data']['elements'])}"]
    parts.append(f"{rate:.2f}/s" if rate else "–/s")
    parts.append(f"{max(0.0, now - result['taken']):.1f} s old")
    return " · ".join(parts)


class Recognizer:
    """Frames read one at a time. grab(path, width, height) writes the frame as PPM on the main
    loop (True on success); source() is the guest display's (width, height) or None; post(fn)
    runs fn on the main loop; on_result(result) gets {"taken", "took", "data", "png"} or
    {"taken", "error"}. read is beanjar.elements.read_screen's signature."""

    def __init__(self, grab, source, post, on_result, read=None, clock=time.monotonic):
        self.grab, self.source, self.post, self.on_result = grab, source, post, on_result
        self.given = self.read = read
        self.clock = clock
        self.throttle = Throttle()
        self.meter = RateMeter()
        self.result = None
        self.binary = None
        self.tmp = None
        self.generation = 0  # stop() makes a reading in flight stale

    def start(self):
        """Resolves go-textshot first: an Error (not installed) leaves the layer off."""
        from beanjar import elements

        if self.given is None:  # resolved at each start: it may have been installed or removed
            self.binary = elements.executable()
            self.read = elements.read_screen
        self.tmp = tempfile.mkdtemp(prefix="beanjar-viewer-elements-")
        self.result = None
        self.meter = RateMeter()
        self.throttle = Throttle()

    def stop(self):
        self.generation += 1
        tmp, self.tmp = self.tmp, None
        if tmp and not self.throttle.busy:
            shutil.rmtree(tmp, ignore_errors=True)
        elif tmp:
            self._stale_tmp = tmp  # removed when the reading in flight ends

    def offer(self):
        """A new frame may be there (the main loop's tick): take it unless busy or too soon.
        Returns whether a reading started."""
        if self.tmp is None or not self.throttle.take(self.clock()):
            return False
        n = self.throttle.last
        ppm = os.path.join(self.tmp, f"frame-{int(n * 1000)}.ppm")
        if not self.grab(ppm, *FRAME_SIZE):
            self.throttle.done()
            return False
        threading.Thread(
            target=self._read,
            args=(ppm, n, self.source(), self.generation),
            name="elements",
            daemon=True,
        ).start()
        return True

    def _read(self, ppm, taken, source, generation):
        from beanjar import framing

        png = ppm[:-4] + ".png"
        try:
            with open(ppm, "rb") as f:
                data = framing.ppm_to_png(f.read(), source)
            with open(png, "wb") as f:
                f.write(data)
            reading = self.read(png, self.binary)
            result = {"taken": taken, "took": self.clock() - taken, "data": reading, "png": data}
        except Exception as e:  # noqa: BLE001 - any failure is the layer's message
            result = {"taken": taken, "error": str(e).splitlines()[0] if str(e) else repr(e)}
        finally:
            for p in (ppm, png):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        self.post(lambda: self._done(result, generation))

    def _done(self, result, generation):
        self.throttle.done()
        stale = getattr(self, "_stale_tmp", None)
        if stale:
            shutil.rmtree(stale, ignore_errors=True)
            self._stale_tmp = None
        if generation != self.generation:
            return
        if "error" not in result:
            self.meter.tick(self.clock())
        self.result = result
        self.on_result(result)
