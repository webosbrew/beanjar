# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The Elements layer (More > Show planes > Elements; host/viewer/README.md): what screen
recognition reads in the TV picture, drawn over it. Each element gets a box in its kind's colour
and a label with its kind and text; a line in the corner has the count, the measured reading rate
and the age of the result shown. The reading is recognition.py's (beanjar.elements, the one
`beanjar elements` does); this widget only draws, and takes no input."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Gtk, Pango, PangoCairo  # noqa: E402

from .recognition import Recognizer, overlay_items, status_line  # noqa: E402

# A tick looks for a new frame this often (the recognizer's throttle keeps readings below 15/s);
# the status line's age is redrawn every AGE_MS.
TICK_MS = 50
AGE_MS = 500
LABEL_MAX = 40  # characters of an element's text in its label

COLOURS = {  # kind: (r, g, b), libadwaita's palette
    "button": (1.0, 0.47, 0.0),
    "icon": (0.21, 0.52, 0.89),
    "text": (0.2, 0.82, 0.48),
    "progress": (0.75, 0.38, 0.85),
}


class ElementsLayer(Gtk.DrawingArea):
    """Over the TvView (in the pane's Gtk.Overlay). set_active(True) starts reading frames of
    core; say(text) reports a failure to start (go-textshot not installed). turned() is whether
    the picture is shown turned (portrait), when the boxes are not drawn."""

    def __init__(self, core, view, say=None, turned=None):
        super().__init__()
        self.core, self.view = core, view
        self.say = say or (lambda _t: None)
        self.turned = turned or (lambda: False)
        self.set_can_target(False)
        self.set_can_focus(False)
        self.set_visible(False)
        self.set_draw_func(self._draw)
        self.recognizer = Recognizer(
            grab=lambda path, w, h: self.core.screenshot(path, w, h),
            source=self._source,
            post=lambda fn: GLib.idle_add(lambda: (fn(), False)[1]),
            on_result=lambda _r: self.queue_draw(),
        )
        self.active = False
        self._ticks = ()
        self._changes = None  # the core's scene changes at the last frame taken

    def _source(self):
        w, h = self.core.counter("width") or 0, self.core.counter("height") or 0
        return (w, h) if w > 0 and h > 0 else None

    def set_active(self, on):
        """Shows the layer and starts reading (True), or hides it and stops; returns the state,
        False when go-textshot is not there."""
        on = bool(on)
        if on == self.active:
            return on
        if on:
            from beanjar.base import Error

            try:
                self.recognizer.start()
            except Error as e:
                self.say(str(e).splitlines()[0])
                return False
            self._changes = None
            self._ticks = (
                GLib.timeout_add(TICK_MS, self._tick),
                GLib.timeout_add(AGE_MS, lambda: (self.queue_draw(), True)[1]),
            )
        else:
            for t in self._ticks:
                GLib.source_remove(t)
            self._ticks = ()
            self.recognizer.stop()
        self.active = on
        self.set_visible(on)
        self.queue_draw()
        return on

    def _tick(self):
        """A frame for the recognizer when the picture changed since the last one taken (a still
        picture is read once), not while a reading is busy (the throttle drops it)."""
        valid, _rect = self.core.picture()
        if not valid:
            return True
        changes = self.core.counter("changes")
        if changes == self._changes and self.recognizer.result is not None:
            return True
        self.view.make_current()
        if self.recognizer.offer():
            self._changes = changes
            self.queue_draw()
        return True

    def _draw(self, _area, cr, _width, _height):
        now = GLib.get_monotonic_time() / 1e6
        rec = self.recognizer
        result = rec.result
        valid, rect = self.core.picture()
        if result and "data" in result and valid and not self.turned():
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string("Sans 9"))
            for kind, text, (x, y, w, h) in overlay_items(result["data"], rect):
                r, g, b = COLOURS.get(kind, (1, 1, 1))
                cr.set_source_rgba(r, g, b, 0.95)
                cr.set_line_width(2)
                cr.rectangle(x, y, w, h)
                cr.stroke()
                label = kind + (" " + _short(text) if text else "")
                layout.set_text(label, -1)
                tw, th = layout.get_pixel_size()
                ly = y - th - 2 if y - th - 2 >= rect[1] else y + 1
                cr.set_source_rgba(0, 0, 0, 0.7)
                cr.rectangle(x, ly, tw + 4, th + 1)
                cr.fill()
                cr.set_source_rgba(r, g, b, 1)
                cr.move_to(x + 2, ly)
                PangoCairo.show_layout(cr, layout)
        line = status_line(result, rec.meter.rate(), now, busy=rec.throttle.busy)
        if self.turned():
            line += " · boxes not drawn in portrait"
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string("Monospace Bold 10"))
        layout.set_text(line, -1)
        tw, th = layout.get_pixel_size()
        x0, y0 = (rect[0] + 8, rect[1] + 8) if valid else (8, 8)
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.rectangle(x0 - 4, y0 - 2, tw + 8, th + 4)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 1)
        cr.move_to(x0, y0)
        PangoCairo.show_layout(cr, layout)

    def last(self):
        """The result shown ({"taken", "took", "data", "png"}), or None: for --grab's files."""
        r = self.recognizer.result
        return r if r and "data" in r else None


def _short(text):
    text = " ".join(text.split())
    return text if len(text) <= LABEL_MAX else text[: LABEL_MAX - 1] + "…"
