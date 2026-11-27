# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The LG remotes as GTK widgets (host/viewer/README.md, "TV remote").

Firmware layouts and the shared service remote, chosen from the chin menu:
- "normal": build/remote-reference.html's layout, 48 buttons in CSS pixels on a 188x652 panel;
- "netcast": the selected NetCast firmware's dashboard and legacy keys;
- "simplesmart": the existing SimpleSmart IR mapping, with HOME rather than NetCast;
- "service": LG's factory service remote, "Factory SVC Remocon" MKJ39170828, from a photo of it,
  on a 188x764 panel.
Each is drawn at scale s = the panel's height / the layout's height (or its width / 188). The
body is a slim, nearly rectangular bar like LG's own remotes, with a chin at the bottom where LG
prints its logo; the chin holds the remote window's own controls (a menu and close), drawn
low-contrast like a logo so they are not taken for TV keys.

Every key is a button of the remote's infrared: its LG IR data code (NEC custom code 0x04) goes to
the guest's micom (core.ir, include/webos-ir.h), which reports it to tvservice as LG's micom does;
tvservice maps it to a Linux key with its own table. Mouse-down sends the button down, mouse-up its
release, so holding one is a long press (the micom repeats it). Keys with no IR code WT1H knows, or
whose Linux key WT1H's compositor has no Qt key for, are dimmed and send nothing. In the remote's
window (window.RemoteWindow) the panel is transparent around the body; body_rects() is the body's
shape for the window's input region.
"""

import math
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
# the layouts' data, without GTK (the web viewer's too)
from beanjar.devices.remote_layouts import *  # noqa: E402, F401, F403
from beanjar.devices.remote_layouts import _light  # noqa: E402
from gi.repository import Gdk, Gtk  # noqa: E402


def _mix(a, b, t):
    ch = [round(((a >> s) & 255) * (1 - t) + ((b >> s) & 255) * t) for s in (16, 8, 0)]
    return (ch[0] << 16) | (ch[1] << 8) | ch[2]


def _font_px(text, cap, width, s):
    """Cap height -> font size (DejaVu Sans Bold: capitals are ~0.73 em), shrunk so the longest
    line fits the key (~0.68 em a character)."""
    longest = max(len(line) for line in text.split("\n"))
    return max(4.0, min(cap * s / 0.73, (width - 3) * s / (0.68 * longest)))


class RemotePanel(Gtk.Widget):
    """A remote's body and keys, scaled to the allocation. send(code, down) sends a button's IR
    code; show_held(name) is for the preview (drawn held, nothing sent). set_layout(name) switches
    between LAYOUTS."""

    def __init__(self, send=None, layout="normal"):
        super().__init__()
        self._send = send
        self.presses = 0
        self._held = -1
        self._scale = None
        self._css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1
        )
        self.add_css_class("remote-panel")
        self._parts = []  # (widget, x, y, w, h) in CSS px
        self._keys = []
        self._images = []  # (image, key height in CSS px)
        self.controls = {}
        self._menu = None
        self._geometry = (0.0, 0.0, 1.0)  # ox, oy, s of the last allocation
        self.on_layout = None  # called after each allocation (the window's shape)
        self.layout = None
        self.set_layout(layout)

    @property
    def keys(self):
        return self.layout.keys

    # ---- building ----
    def set_layout(self, name):
        """Builds the keys, labels and controls of LAYOUTS[name] (a held key is released)."""
        layout = LAYOUTS[name]
        if self.layout is layout:
            return
        self.release()
        for widget, *_ in self._parts:
            widget.unparent()
        self._parts, self._keys, self._images, self.controls = [], [], [], {}
        self.layout = layout
        self._scale = None
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        bx, by, bw, bh = layout.body[:4]
        self._add(Gtk.Box(css_classes=["remote-body"], can_target=False), bx, by, bw, bh)
        self._add(
            Gtk.Box(css_classes=["remote-edge"], can_target=False), bx + 1, by + 1, bw - 2, bh - 2
        )
        for i, (x, y, w, h, *_rest) in enumerate(layout.decor):
            self._add(
                Gtk.Box(css_classes=["remote-decor", f"remote-decor-{i}"], can_target=False),
                x,
                y,
                w,
                h,
            )
        for i, k in enumerate(layout.keys):
            key = Gtk.Box(
                css_classes=["rk", f"rk-{k.name}"], halign=Gtk.Align.FILL, valign=Gtk.Align.FILL
            )
            key.set_tooltip_text(
                k.name.replace("_", " ").title()
                if k.code is not None
                else f"{k.name.replace('_', ' ')}: no verified key code; not sent"
            )
            if k.code is None:
                key.add_css_class("dim")
            child, text, cap = None, k.text, k.cap
            if k.glyph:
                icon, fallback = GLYPHS[k.glyph]
                if theme.has_icon(icon):
                    child = Gtk.Image.new_from_icon_name(icon)
                    self._images.append((child, min(k.w, k.h)))
                else:
                    text, cap = fallback, SMALL
            if child is None and text:
                child = Gtk.Label(label=text, justify=Gtk.Justification.CENTER)
            if child is not None:
                child.set_halign(Gtk.Align.CENTER)
                child.set_valign(Gtk.Align.CENTER)
                child.set_hexpand(True)
                child.set_can_target(False)
                key.append(child)
            gesture = Gtk.GestureClick()
            gesture.set_button(Gdk.BUTTON_PRIMARY)
            gesture.connect("pressed", lambda _g, _n, _x, _y, i=i: self.press(i))
            gesture.connect("released", lambda *_: self.release())
            gesture.connect("end", lambda *_: self.release())
            key.add_controller(gesture)
            self._add(key, k.x, k.y, k.w, k.h)
            self._keys.append(key)
        for x, y, text in layout.labels:
            label = Gtk.Label(
                label=text,
                css_classes=["remote-label"],
                can_target=False,
                justify=Gtk.Justification.CENTER,
            )
            if any(
                k.x <= x <= k.x + k.w and k.y <= y <= k.y + k.h and _light(k.face)
                for k in layout.keys
            ):
                label.add_css_class("on-light")  # VOL and CH, printed on the light rockers
            lines = text.count("\n") + 1
            self._add(label, x - 30, y - 5 * lines, 60, 10 * lines)
        for i, (x, y, text, cap) in enumerate(layout.chin):
            self._add(
                Gtk.Label(
                    label=text, css_classes=["remote-chin", f"remote-chin-{i}"], can_target=False
                ),
                x - 60,
                y - 6,
                120,
                12,
            )
        for name, x, y, size, icon, fallback, tip in layout.controls:
            if name == "menu":
                ctl = Gtk.MenuButton(direction=Gtk.ArrowType.UP, always_show_arrow=False)
                ctl.set_menu_model(self._menu)
            else:  # the close request of the window the panel is in
                ctl = Gtk.Button(action_name="window.close")
            ctl.set_focusable(False)
            ctl.set_focus_on_click(False)
            ctl.set_tooltip_text(tip)
            ctl.set_css_classes(["remote-ctl", f"remote-ctl-{name}"])
            if theme.has_icon(icon):
                image = Gtk.Image.new_from_icon_name(icon)
                self._images.append((image, size / 0.6 * 0.7))
                ctl.set_child(image)
            else:
                ctl.set_child(Gtk.Label(label=fallback))
            self._add(ctl, x, y, size, size)
            self.controls[name] = ctl
        self.queue_resize()

    def set_floating(self, menu=None):
        """In the remote's window (menu given): transparent around the body; the chin's menu
        control opens menu (a Gio.MenuModel whose actions the window provides). Without (the
        preview): a dark background, the menu control inert. The close control is always the
        window's own window.close action."""
        floating = menu is not None
        self._menu = menu
        self.controls["menu"].set_menu_model(self._menu)
        (self.add_css_class if floating else self.remove_css_class)("floating")
        self.queue_allocate()

    def menu_open(self):
        """Whether the chin's menu is open (it gets the keyboard then)."""
        popover = self.controls["menu"].get_popover()
        return bool(popover and popover.get_visible())

    def is_control(self, widget):
        """Whether widget is (inside) a key or a control: presses there are not window drags."""
        stop = set(self._keys) | set(self.controls.values())
        while widget is not None and widget is not self:
            if widget in stop:
                return True
            widget = widget.get_parent()
        return False

    def key_widget(self, name):
        """The widget of the current layout's key NAME."""
        return self._keys[find(name, self.layout)]

    def to_panel(self, x, y):
        """CSS px of the layout -> this panel's coordinates, as last allocated."""
        ox, oy, s = self._geometry
        return ox + x * s, oy + y * s

    def body_rects(self):
        """The body's shape (small rounded corners) as rectangles (x, y, w, h) in panel coordinates, whole pixels:
        one per pixel row through the corners, one for the straight middle."""
        ox, oy, s = self._geometry
        bx, by, bw, bh, rt, rb = self.layout.body
        x0, y0 = round(ox + bx * s), round(oy + by * s)
        x1, y1 = round(ox + (bx + bw) * s), round(oy + (by + bh) * s)
        rt, rb = round(rt * s), round(rb * s)

        def inset(r, i):  # row i (0 = the outermost) of a corner of radius r
            dy = r - (i + 0.5)
            return round(r - math.sqrt(max(0.0, r * r - dy * dy)))

        rects = [(x0 + inset(rt, i), y0 + i, x1 - x0 - 2 * inset(rt, i), 1) for i in range(rt)]
        rects.append((x0, y0 + rt, x1 - x0, max(0, y1 - y0 - rt - rb)))
        rects += [(x0 + inset(rb, i), y1 - 1 - i, x1 - x0 - 2 * inset(rb, i), 1) for i in range(rb)]
        return [r for r in rects if r[2] > 0 and r[3] > 0]

    def _add(self, widget, x, y, w, h):
        widget.set_parent(self)
        self._parts.append((widget, x, y, w, h))

    def do_dispose(self):
        self.release()
        for widget, *_ in self._parts:
            widget.unparent()
        self._parts = []
        Gtk.StyleContext.remove_provider_for_display(Gdk.Display.get_default(), self._css)

    # ---- keys ----
    def press(self, i):
        k = self.layout.keys[i]
        if k.code is None:
            print(f"remote: {k.name} has no verified key code; not sent", file=sys.stderr)
            return
        self.release()
        if self._send:
            self._send(k.code, True)
        self._held = i
        self._keys[i].add_css_class("held")
        self.presses += 1

    def release(self):
        if self._held < 0:
            return
        i, self._held = self._held, -1
        self._keys[i].remove_css_class("held")
        if self._send:
            self._send(self.layout.keys[i].code, False)

    def show_held(self, name):
        """--remote-press: draw NAME held, send nothing."""
        i = find(name, self.layout)
        if i < 0:
            raise ValueError(f"unknown remote key {name}")
        self._keys[i].add_css_class("held")

    # ---- layout ----
    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.WIDTH_FOR_HEIGHT

    def do_measure(self, orientation, for_size):
        height = self.layout.height
        if orientation == Gtk.Orientation.HORIZONTAL:
            nat = round(for_size * REMOTE_W / height) if for_size > 0 else REMOTE_W
            return min(60, nat), nat, -1, -1
        nat = round(for_size * height / REMOTE_W) if for_size > 0 else height
        return min(190, nat), nat, -1, -1

    def do_size_allocate(self, width, height, _baseline):
        s = min(width / REMOTE_W, height / self.layout.height)
        ox, oy = (width - REMOTE_W * s) / 2, (height - self.layout.height * s) / 2
        quantised = round(s * 40) / 40
        if quantised != self._scale:
            self._scale = quantised
            self._style(quantised)
        self._geometry = (ox, oy, s)
        for widget, x, y, w, h in self._parts:
            if not widget.get_visible():
                continue
            rect = Gdk.Rectangle()
            rect.x, rect.y = round(ox + x * s), round(oy + y * s)
            rect.width = max(1, round((x + w) * s + ox) - rect.x)
            rect.height = max(1, round((y + h) * s + oy) - rect.y)
            mw = widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            mh = widget.measure(Gtk.Orientation.VERTICAL, rect.width)[0]
            rect.width, rect.height = max(rect.width, mw), max(rect.height, mh)
            widget.size_allocate(rect, -1)
        if self.on_layout:
            self.on_layout()

    def _style(self, s):
        """Colours, radii and font sizes at scale s, in one provider."""
        layout = self.layout
        px = lambda v: f"{v * s:.2f}px"  # noqa: E731
        rt, rb = layout.body[4:]
        css = [
            ".remote-panel { background-color: #1a1a1c; }",
            ".remote-panel.floating { background-color: transparent; }",
            # the chin's controls: printed on the body like the logo, not keys
            ".remote-ctl, .remote-ctl > button { padding: 0; margin: 0; min-width: 0; min-height: 0;"
            " border: none; box-shadow: none; outline: none; border-radius: 9999px;"
            " background: transparent;"
            f" color: #{CHIN_PRINT:06x}; }}",
            f".remote-ctl > button, button.remote-ctl {{ border: {max(1.0, s):.2f}px solid #{layout.rim:06x}; }}",
            ".remote-ctl:hover, .remote-ctl > button:hover, .remote-ctl > button:checked"
            f" {{ color: #{PRINT:06x}; background: rgba(255, 255, 255, 0.05); }}",
            ".remote-ctl:active, .remote-ctl > button:active { background: rgba(255, 255, 255, 0.1); }",
            f".remote-ctl label {{ font-family: 'DejaVu Sans', sans-serif; font-weight: bold;"
            f" font-size: {SMALL * s / 0.73:.2f}px; }}",
            f".remote-body {{ background-color: #{layout.rim:06x}; border-radius: {px(rt)} {px(rt)} {px(rb)} {px(rb)}; }}",
            f".remote-edge {{ background-color: #{layout.face:06x}; border-radius: {px(rt - 1)} {px(rt - 1)}"
            f" {px(rb - 1)} {px(rb - 1)}; }}",
            ".rk { padding: 0; margin: 0; min-width: 0; min-height: 0; }",
            ".rk label { font-family: 'DejaVu Sans', sans-serif; font-weight: bold; padding: 0; margin: 0;"
            " min-width: 0; min-height: 0; }",
            ".rk.dim { opacity: 0.4; }",
            ".rk:hover { filter: brightness(1.12); }",
            f".remote-label {{ color: #{layout.print:06x}; font-family: 'DejaVu Sans', sans-serif; font-weight: bold;"
            f" font-size: {SMALL * s / 0.73:.2f}px; }}",
            f".remote-label.on-light {{ color: #{LABEL_ON_LIGHT:06x}; }}",
        ]
        for i, (x, y, w, h, radius, face, border) in enumerate(layout.decor):
            css.append(
                f".remote-decor-{i} {{ border-radius: {px(radius)};"
                + (f" background-color: #{face:06x};" if face is not None else "")
                + (
                    f" border: {max(1.0, s):.2f}px solid #{border:06x};"
                    if border is not None
                    else ""
                )
                + " }"
            )
        for i, (x, y, text, cap) in enumerate(layout.chin):
            css.append(
                f".remote-chin-{i} {{ color: #{layout.print:06x}; font-family: 'DejaVu Sans', sans-serif;"
                f" font-size: {cap * s / 0.73:.2f}px; }}"
            )
        for k in layout.keys:
            held = _mix(k.face, 0 if _light(k.face) else 0xFFFFFF, 0.25 if _light(k.face) else 0.3)
            radius = " ".join(px(r) for r in k.radii)
            font = f" font-size: {_font_px(k.text, k.cap, k.w, s):.2f}px;" if k.text else ""
            ring = (
                f" box-shadow: inset 0 0 0 {max(1.0, 0.6 * s):.2f}px #{k.ring:06x};"
                if k.ring is not None
                else ""
            )
            css.append(
                f".rk-{k.name} {{ background-color: #{k.face:06x}; border-radius: {radius};"
                f" color: #{k.ink:06x};{ring} }}"
            )
            css.append(f".rk-{k.name} label {{ color: #{k.ink:06x};{font} }}")
            css.append(f".rk-{k.name}.held {{ background-color: #{held:06x}; }}")
        self._css.load_from_string("\n".join(css))
        for image, size in self._images:
            image.set_pixel_size(max(6, round(size * 0.6 * s)))


def window_scale(picture_h, layout=NORMAL):
    """The remote window's scale beside a TV picture picture_h CSS px tall: the remote's height
    WINDOW_SHARE of the picture's, kept between WINDOW_MIN (its labels easily legible, its keys
    easy to hit) and WINDOW_MAX."""
    return max(WINDOW_MIN, min(WINDOW_MAX, WINDOW_SHARE * picture_h / layout.height))
