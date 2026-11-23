# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The TV picture of the VM Summary: a 16:9 frame with the selected VM's screen, and where its
pictures come from.

TvPicture shows one of three states: live (the running VM's screen), last (the last picture kept,
dimmed, with a "Last picture" badge: the VM is off) or none (a TV outline, "No picture yet"). It is
fixed-size beside the tiles on a wide window and fills the width, keeping 16:9, on a narrow one. A
click runs on_click (the Summary's: connect or start).

Pictures takes them with the command the web list uses: `beanjar screenshot NAME - --frame 384x216`
for a running VM (a thumbnail, which the library also keeps for --last), `beanjar screenshot NAME -
--last` for one that is off (once per state). Each runs in a thread, never on the main loop, at most
one at a time per VM; the live one at most every REFRESH_S seconds and only when the caller allows it
(the Summary visible, the window active, no viewer open). A portrait model's picture is turned the
way its viewers and the web list turn it (rotation.picture_turn of the guest's last rotation report
while it runs, of the angle its set was left at when it is off; `beanjar sensor show rotate NAME --json`
reports the same)."""

import os
import subprocess
import sys
import threading
import time

from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Gtk

REFRESH_S = 5.0  # between two live pictures of a VM
SIZE = "384x216"  # the web list's thumbnail size (beanjar_web/server.py refresh_thumbnail)
WIDE = (240, 135)  # the picture beside four tiles in a row
MEDIUM = (256, 144)  # beside the tiles in a 2x2 grid

CSS = b"""
.beanjar-tv { border-radius: 12px; background: #1b1b1f; }
.beanjar-tv.clickable:hover { box-shadow: 0 0 0 2px @accent_bg_color; }
.beanjar-tv.dim picture { opacity: 0.45; }
.beanjar-tv.empty { background: alpha(currentColor, 0.06); }
.beanjar-tv.empty.clickable:hover { background: alpha(currentColor, 0.12); }
.beanjar-tv-badge { background: rgba(0,0,0,0.78); border: 1px solid rgba(255,255,255,0.25);
  color: white; border-radius: 999px; padding: 4px 12px; font-weight: 700; }
"""


class Frame(Gtk.Widget):
    """One child in a 16:9 box: size (width, height) fixed, or None: as wide as it is given, 9/16
    of that high (height-for-width)."""

    __gtype_name__ = "BeanjarTvFrame"

    def __init__(self, child):
        super().__init__()
        self.size = WIDE
        self.child = child
        child.set_parent(self)

    def set_size(self, size):
        if size != self.size:
            self.size = size
            self.queue_resize()

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        if self.size:
            n = self.size[0 if orientation == Gtk.Orientation.HORIZONTAL else 1]
            return n, n, -1, -1
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 160, 240, -1, -1
        n = round((for_size if for_size > 0 else 240) * 9 / 16)
        return n, n, -1, -1

    def do_size_allocate(self, width, height, baseline):
        self.child.allocate(width, height, baseline, None)

    def do_snapshot(self, snapshot):
        self.snapshot_child(self.child, snapshot)

    def do_dispose(self):
        if self.child is not None:
            self.child.unparent()
            self.child = None


class TvPicture(Gtk.Box):
    """The picture, with its state (live, last or none), its badge and a click (on_click)."""

    def __init__(self):
        super().__init__(valign=Gtk.Align.START)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.overlay = Gtk.Overlay(overflow=Gtk.Overflow.HIDDEN)
        self.overlay.add_css_class("beanjar-tv")
        self.picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        self.overlay.set_child(self.picture)
        self.outline = Gtk.Image(
            icon_name="video-display-symbolic",
            pixel_size=40,
            opacity=0.35,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            can_target=False,
        )
        self.overlay.add_overlay(self.outline)
        self.badge = Gtk.Label(
            halign=Gtk.Align.START,
            valign=Gtk.Align.END,
            margin_start=8,
            margin_bottom=8,
            can_target=False,
        )
        self.badge.add_css_class("beanjar-tv-badge")
        self.badge.add_css_class("caption")
        self.overlay.add_overlay(self.badge)
        self.frame = Frame(self.overlay)
        self.frame.set_hexpand(True)
        self.append(self.frame)
        self.on_click = None
        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.overlay.add_controller(click)
        self.kind = None
        self.show("none", None)

    def set_size(self, size):
        """size: (width, height) beside the tiles, or None: the full width."""
        self.frame.set_size(size)
        self.set_halign(Gtk.Align.FILL if size is None else Gtk.Align.END)
        self.set_hexpand(size is None)

    def show(self, kind, texture):
        """kind: "live" (a running VM's screen), "last" (dimmed, "Last picture") or "none" (the
        outline, "No picture yet"); texture: the picture (None for none)."""
        if texture is None:
            kind = "none"
        self.kind = kind
        self.picture.set_paintable(texture)
        self.outline.set_visible(kind == "none")
        for cls, on in (("dim", kind == "last"), ("empty", kind == "none")):
            (self.overlay.add_css_class if on else self.overlay.remove_css_class)(cls)
        badge = {"last": "Last picture", "none": "No picture yet"}.get(kind)
        self.badge.set_visible(bool(badge))
        self.badge.set_text(badge or "")

    def set_action(self, tooltip):
        """What a click does (its tooltip), or None: nothing."""
        self.overlay.set_tooltip_text(tooltip)
        clickable = tooltip is not None
        (self.overlay.add_css_class if clickable else self.overlay.remove_css_class)("clickable")
        self.overlay.set_cursor(Gdk.Cursor.new_from_name("pointer") if clickable else None)

    def _clicked(self, gesture, _n, _x, _y):
        if self.on_click and self.overlay.get_tooltip_text():
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.on_click()


class Entry:
    """A VM's pictures: the last one taken (texture), its mode ("live" or "last"), and its refresh."""

    def __init__(self):
        self.texture = None
        self.mode = None  # the mode its texture is from
        self.want = None  # the mode last asked for
        self.loaded = False  # "last": fetched for this state already
        self.checked = 0.0  # monotonic time of the last live picture's attempt
        self.pending = False
        self.generation = 0
        self.error = None


def turn(pixbuf, angle):
    """pixbuf turned as a portrait model's viewers show it: rotation.picture_turn(angle) clockwise,
    which is angle counter-clockwise (GdkPixbuf.PixbufRotation counts counter-clockwise)."""
    if angle in (90, 180, 270):
        return pixbuf.rotate_simple(GdkPixbuf.PixbufRotation(angle))
    return pixbuf


class Pictures(GObject.Object):
    """The selected VM's pictures, per VM; changed(name) when one has a new picture. command(name,
    words): the beanjar argv for a screenshot (the manager's: its library, its checkout);
    rotation(name, live): the angle to turn by (0, 90, 180, 270). Both run in a worker thread."""

    __gtype_name__ = "BeanjarPictures"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, command, rotation, env=None):
        super().__init__()
        self.command = command
        self.rotation = rotation
        self.env = env
        self.entries = {}
        self.runs = 0  # screenshots started (the tests)

    def entry(self, name):
        return self.entries.setdefault(name, Entry())

    def picture(self, name):
        """(kind, texture) to show for VM name: its last picture and whether it is live."""
        e = self.entries.get(name)
        if e is None or e.texture is None:
            return "none", None
        return ("last" if e.want == "last" else "live"), e.texture

    def update(self, name, mode, allowed=True):
        """VM name is in mode: "live" (it runs and has a screen), "last" (off) or None (neither:
        keep what there is). allowed: a live picture may be taken now (its page is seen)."""
        e = self.entry(name)
        if mode != e.want:
            e.want = mode
            e.loaded = False
            e.checked = 0.0
            e.generation += 1
        if mode is None or e.pending:
            return
        if mode == "last":
            if not e.loaded:
                self._fetch(name, e, ["--last"])
            return
        if allowed and time.monotonic() - e.checked >= REFRESH_S:
            self._fetch(name, e, ["--frame", SIZE])
        elif not allowed and e.texture is None and not e.loaded:
            self._fetch(name, e, ["--last"], live=True)  # something until it may take one

    def forget(self, name):
        self.entries.pop(name, None)

    def _fetch(self, name, e, words, live=None):
        e.pending = True
        e.loaded = True
        if live is None:
            live = words[0] != "--last"
            e.checked = time.monotonic() if live else e.checked
        generation = e.generation
        argv = self.command(name, words)
        self.runs += 1

        def work():
            pixbuf, error = None, None
            try:
                p = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=30,
                    env=self.env,
                )
                if p.returncode == 0 and p.stdout.startswith(b"\x89PNG"):
                    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
                    loader.write(p.stdout)
                    loader.close()
                    pixbuf = turn(loader.get_pixbuf(), self.rotation(name, live))
                else:
                    error = p.stderr.decode(errors="replace").strip() or f"exit {p.returncode}"
            except (OSError, subprocess.TimeoutExpired, GLib.Error) as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - a worker must always report back
                error = f"{type(exc).__name__}: {exc}"
            GLib.idle_add(self._fetched, name, generation, pixbuf, error)

        threading.Thread(target=work, name="beanjar-gui-picture", daemon=True).start()

    def _fetched(self, name, generation, pixbuf, error):
        e = self.entries.get(name)
        if e is None:
            return False
        e.pending = False
        if generation != e.generation:
            return False  # the VM changed state while it was taken
        e.error = error
        if pixbuf is not None:
            e.texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            e.mode = e.want
        elif e.want == "last":
            e.texture = None  # no thumbnail kept: nothing to show
        self.emit("changed", name)
        return False


def beanjar_argv(library, name, words):
    """The argv of `beanjar --library L [--repo R] screenshot NAME - WORDS` (as gui.Operation
    runs the CLI: this Python, the package beside the manager's)."""
    return [
        sys.executable,
        "-m",
        "beanjar",
        "--library",
        library.root,
        *(["--repo", library.known_repo] if library.known_repo else []),
        "screenshot",
        name,
        "-",
        *words,
    ]


def cli_env(pkg_parent):
    env = dict(os.environ)
    path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + path if path else "")
    return env
