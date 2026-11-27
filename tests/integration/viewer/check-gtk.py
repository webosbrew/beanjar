#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The GTK viewer's window, input routing and chrome, without a VM (docs/dev/architecture/viewer.md).

    xvfb-run -a tests/integration/viewer/check-gtk.py [CORE.so]    (pytest tests/integration/viewer -k gtk)

Runs tests/fakes/fake-qemu.py (a stand-in D-Bus display with a known picture, a visible LG cursor
and a log of every input call) and the viewer's GTK front end in this process on X11, and drives
it with real X input events (XTest):

- layout: the picture area gets --size; the status bar sits under it; the pane is only the picture
  and the bar, and the TV window never changes size for the remote; the header bar shows the
  VM's name and firmware, with the remote's Back, Home and Settings keys (linked), Save
  Screenshot, Fullscreen and the Remote toggle (its icon the viewer's own), no menu;
- --remote: the remote's window is shown at start, placed right of the TV window (X11, no WM),
  transient for it, 1.125x as tall as the picture at 1.125x to 2.25x its layout (taller than the
  picture), following the picture;
- the Remote toggle is on exactly while the remote is on screen: hidden and shown directly, and
  minimized and restored (a window manager's _NET_WM_STATE_HIDDEN, faked);
- clicks on the header bar's Remote toggle hide and show that window and never reach the guest;
- the header bar's Back, Home and Settings send the remote's BACK, HOME and MENU IR codes (down,
  then up; a long press holds it), nothing else;
- the host pointer is always transparent over the displayed picture and not over the bar;
- in standby (QMP's standby chardev open) and in the micom's warm standby (micom-status power=2,
  webOS's Active Standby) the picture is black on screen and LG's cursor is not drawn, by the core
  or on screen; after the wake it is drawn as the guest last sent it;
- keys go to the guest (GTK takes none of them): Tab, Space, F10, Return and letters to the
  keyboard, Super, Home, Escape and the volume keys as the remote's IR buttons (HOME, BACK, VOL,
  MUTE on ir.sock); the Menu key hides and shows the remote's window and is not sent; F11
  toggles fullscreen (not sent), and fullscreen hides the header bar and the status bar;
- the remote's window: a button sends its IR code (down, then up; a long press holds it); the
  window is undecorated, its input region is the body's outline, chin and small corners included
  (a press in a transparent corner falls through to the picture), a drag on the body or its chin
  calls begin_move and, with no window manager (GDK then moves the window itself), moves it; keys
  typed in it reach the guest; the Menu key, the chin's close control, Escape and a close request
  hide it (none sent), the toggle and the Menu key show it again; the chin's menu opens with Normal
  Remote, Service Remote and Place Next to TV, Place Next to TV works, Escape closes the menu only, and the
  chin's controls send nothing to the guest; the menu's Service Remote switches to the service
  layout (the window resized to it, the input region its body, IN START sending 0xFB, a dimmed key
  nothing) and webOS Remote back;
- the header bar's Save Screenshot is the guest picture only, at the picture
  area's size, and says so in a toast;
- the set's own key (a onekey model, local_key): left of Back, its click a local key record
  (Local.Down then Local.Up 0xea on ir.sock), a long press held until mouse-up; its power icon
  follows the micom's front LED (red in standby with the standby light on, normal colour when
  off or unknown), with no separate title or status-bar LED; a joystick model shows the five ways in a popover
  instead, each its micom code; a model without a local key shows neither. With
  BEANJAR_HEADER_SHOTS=DIR it saves the header bar (onekey, LED off and red) and the joystick
  popover there as PNG;
- the Mouse toggle (a model with a touch screen; hidden on one without): the pointer at start; on,
  a left-button drag is a swipe (MultiTouch begin, moves, end in one slot, no pointer button) and a
  click on a turned picture a tap at the panel's pixels, the right button still the pointer's;
  off again, a click is the pointer's left button.

Exit status 77 (skip) without GTK 4 and libadwaita for Python or without an X display.
"""

import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types

TOP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(TOP, "host/ui"))
sys.path.insert(1, os.path.join(TOP, "host/core"))
os.environ["GDK_BACKEND"] = "x11"
os.environ.pop("WAYLAND_DISPLAY", None)  # never the desktop's compositor
os.environ.setdefault(
    "BEANJAR_VIEWER_CORE",
    sys.argv[1]
    if len(sys.argv) > 1
    else os.path.join(TOP, "build/viewer/libbeanjar-viewer-core.so"),
)
try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("GdkX11", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Adw, Gdk, GdkX11, GLib, Graphene, Gtk
except (ImportError, ValueError) as e:
    print(f"SKIP: no GTK 4 for Python: {e}")
    sys.exit(77)
if not os.environ.get("DISPLAY"):
    print("SKIP: no X display (run under xvfb-run -a)")
    sys.exit(77)

tmp = tempfile.mkdtemp(prefix="check-gtk.")
# Save Screenshot writes to the Pictures folder: make that one of the test's own
# (before GLib first reads the user dirs).
os.makedirs(os.path.join(tmp, "config"))
with open(os.path.join(tmp, "config", "user-dirs.dirs"), "w") as f:
    f.write(f'XDG_PICTURES_DIR="{tmp}/pictures"\n')
os.makedirs(os.path.join(tmp, "pictures"))
os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")

from beanjar_ui.viewer import core as corelib  # noqa: E402
from beanjar_ui.viewer import remote as remotelib  # noqa: E402
from beanjar_ui.viewer import stats as statslib  # noqa: E402
from beanjar_ui.viewer import ui as uilib  # noqa: E402
from beanjar_ui.viewer.window import RemoteWindow, ViewerPane, ViewerWindow  # noqa: E402

# ---- XTest ----
x11 = ctypes.CDLL("libX11.so.6")
xtst = ctypes.CDLL("libXtst.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XDefaultRootWindow.restype = ctypes.c_ulong
x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
x11.XStringToKeysym.restype = ctypes.c_ulong
x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
x11.XKeysymToKeycode.restype = ctypes.c_ubyte
x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XFlush.argtypes = [ctypes.c_void_p]
x11.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
x11.XTranslateCoordinates.argtypes = [
    ctypes.c_void_p,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_ulong),
]
x11.XInternAtom.restype = ctypes.c_ulong
x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
x11.XChangeProperty.argtypes = [
    ctypes.c_void_p,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_int,
]
x11.XDeleteProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
x11.XCreateSimpleWindow.restype = ctypes.c_ulong
x11.XCreateSimpleWindow.argtypes = [
    ctypes.c_void_p,
    ctypes.c_ulong,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_ulong,
    ctypes.c_ulong,
]
xext = ctypes.CDLL("libXext.so.6")


class XRectangle(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
    ]


xext.XShapeGetRectangles.restype = ctypes.POINTER(XRectangle)
xext.XShapeGetRectangles.argtypes = [
    ctypes.c_void_p,
    ctypes.c_ulong,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
]
x11.XFree.argtypes = [ctypes.c_void_p]
SHAPE_INPUT = 2
for f in ("XTestFakeMotionEvent", "XTestFakeButtonEvent", "XTestFakeKeyEvent"):
    getattr(xtst, f).argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
xtst.XTestFakeMotionEvent.argtypes = [
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_ulong,
]
dpy = x11.XOpenDisplay(None)
root = x11.XDefaultRootWindow(dpy)


def translate(widget, other, x, y):
    r = widget.translate_coordinates(other, x, y)  # (x, y), or (ok, x, y) in other bindings
    return r[-2], r[-1]


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
    ]


x11.XGetImage.restype = ctypes.POINTER(XImage)
x11.XGetImage.argtypes = [
    ctypes.c_void_p,
    ctypes.c_ulong,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_ulong,
    ctypes.c_int,
]
x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]


def screen_rgb(widget, x, y, w, h):
    """What the X server shows at widget-local (x, y, w, h): RGB bytes, w*3 per row (24-bit TrueColor)."""
    rx, ry = screen_point(widget, x, y)
    x11.XSync(dpy, 0)
    image = x11.XGetImage(dpy, root, rx, ry, w, h, 0xFFFFFFFF, 2)  # AllPlanes, ZPixmap
    if not image:
        return b""
    im = image.contents
    raw = ctypes.string_at(im.data, im.bytes_per_line * h)
    out = bytearray()
    for row in range(h):
        line = raw[row * im.bytes_per_line :]
        for col in range(w):
            b, g, r = line[col * 4 : col * 4 + 3]  # 32 bpp BGRX, little endian
            out += bytes((r, g, b))
    x11.XFree(im.data)
    x11.XFree(image)
    return bytes(out)


def screen_point(widget, x, y):
    """Widget-local logical point -> root window coordinates."""
    native = widget.get_native()
    wx, wy = translate(widget, native, x, y)
    sx, sy = native.get_surface_transform()
    xid = GdkX11.X11Surface.get_xid(native.get_surface())
    rx, ry, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
    x11.XTranslateCoordinates(
        dpy, xid, root, 0, 0, ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(child)
    )
    return round(rx.value + sx + wx), round(ry.value + sy + wy)


def move(widget, x, y):
    px, py = screen_point(widget, x, y)
    xtst.XTestFakeMotionEvent(dpy, -1, px, py, 0)
    x11.XFlush(dpy)


def click(widget, x=None, y=None, button=1):
    if x is None:
        x, y = widget.get_width() / 2, widget.get_height() / 2
    move(widget, x, y)
    xtst.XTestFakeButtonEvent(dpy, button, 1, 0)
    xtst.XTestFakeButtonEvent(dpy, button, 0, 30)
    x11.XFlush(dpy)


def press_drag(widget, x, y, dx, dy):
    """Press at (x, y), move by (dx, dy) in steps, release; a generator of waits."""
    move(widget, x, y)
    yield 100
    xtst.XTestFakeButtonEvent(dpy, 1, 1, 0)
    x11.XFlush(dpy)
    yield 100
    px, py = screen_point(widget, x, y)
    for i in range(1, 11):
        xtst.XTestFakeMotionEvent(dpy, -1, px + dx * i // 10, py + dy * i // 10, 0)
        x11.XFlush(dpy)
        yield 40
    xtst.XTestFakeButtonEvent(dpy, 1, 0, 0)
    x11.XFlush(dpy)
    yield 300


def input_shape(widget):
    """The X input shape of widget's toplevel: [(x, y, w, h)] in window coordinates."""
    xid = GdkX11.X11Surface.get_xid(widget.get_native().get_surface())
    count, ordering = ctypes.c_int(), ctypes.c_int()
    rects = xext.XShapeGetRectangles(
        dpy, xid, SHAPE_INPUT, ctypes.byref(count), ctypes.byref(ordering)
    )
    out = [(rects[i].x, rects[i].y, rects[i].width, rects[i].height) for i in range(count.value)]
    if rects:
        x11.XFree(rects)
    return out


def inside(rects, x, y):
    return any(rx <= x < rx + rw and ry <= y < ry + rh for rx, ry, rw, rh in rects)


def x_atom(name):
    return x11.XInternAtom(dpy, name.encode(), 0)


def x_prop(xid, name, kind, values):
    data = (ctypes.c_ulong * len(values))(*values)
    x11.XChangeProperty(dpy, xid, x_atom(name), kind, 32, 0, data, len(values))  # PropModeReplace
    x11.XFlush(dpy)


XA_ATOM, XA_WINDOW = 4, 33
wm_check = []


def wm_hint(on):
    """Tell GDK a window manager supports _NET_WM_STATE_HIDDEN (on), or take that back: GDK only
    reads a window's minimized state from _NET_WM_STATE when one does."""
    if on:
        if not wm_check:
            wm_check.append(x11.XCreateSimpleWindow(dpy, root, 0, 0, 1, 1, 0, 0, 0))
        x_prop(wm_check[0], "_NET_SUPPORTING_WM_CHECK", XA_WINDOW, wm_check)
        x_prop(root, "_NET_SUPPORTING_WM_CHECK", XA_WINDOW, wm_check)
        x_prop(
            root,
            "_NET_SUPPORTED",
            XA_ATOM,
            [x_atom("_NET_WM_STATE"), x_atom("_NET_WM_STATE_HIDDEN")],
        )
    else:
        for name in ("_NET_SUPPORTING_WM_CHECK", "_NET_SUPPORTED"):
            x11.XDeleteProperty(dpy, root, x_atom(name))
        x11.XFlush(dpy)


def minimize(window, on):
    """What a window manager does to minimize window, or restore it: its _NET_WM_STATE."""
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    x_prop(xid, "_NET_WM_STATE", XA_ATOM, [x_atom("_NET_WM_STATE_HIDDEN")] if on else [])


def focus(window):
    """Give window the keyboard, as a window manager would (there is none)."""
    x11.XSetInputFocus(
        dpy, GdkX11.X11Surface.get_xid(window.get_surface()), 1, 0
    )  # RevertToPointerRoot
    x11.XFlush(dpy)


def key(name, hold=20):
    """Press and release; hold: ms between them (a person's is ~100)."""
    code = x11.XKeysymToKeycode(dpy, x11.XStringToKeysym(name.encode()))
    assert code, name
    xtst.XTestFakeKeyEvent(dpy, code, 1, 0)
    xtst.XTestFakeKeyEvent(dpy, code, 0, hold)
    x11.XFlush(dpy)


# ---- the run ----
fake = subprocess.Popen([sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), tmp])
for _ in range(100):
    if os.path.exists(os.path.join(tmp, "ready")):
        break
    time.sleep(0.05)
else:
    fake.kill()
    sys.exit("fake-qemu did not start")
LOG = os.path.join(tmp, "input.log")


def log_lines():
    with open(LOG) as f:
        return f.read().splitlines()


failures = []


def icon_names(patterns):
    """Every "...-symbolic" icon name in the files matching patterns (TOP-relative globs)."""
    text = "".join(open(p).read() for g in patterns for p in glob.glob(os.path.join(TOP, g)))
    return sorted(set(re.findall(r'"([a-z0-9-]+-symbolic)"', text)))


def check(cond, what):
    print(("PASS: " if cond else "FAIL: ") + what, flush=True)
    if not cond:
        failures.append(what)


MICOM_LED_CASES = (
    ("standby=0 power=3 led.6e=010101/1", "off", "Front LED: off (the TV is on)"),
    ("standby=1 power=2 led.6e=010101/1", "red", "Front LED: red (standby)"),
    ("standby=0 power=2", "red", "Front LED: red (standby), the micom's default"),
    ("standby=1 power=2 led.6e=010001/2", "off", "Front LED: off (standby light off)"),
    ("standby=1 power=0 led.6e=010101/3", "red", "Front LED: red (standby)"),
    ("standby=1 power=2 led.6e=01/1", "red", "Front LED: red (standby), the micom's default"),
    ("standby=0 power=3 led.6e=010001/2", "off", "Front LED: off (the TV is on)"),
    ("invalid micom status", "unknown", "Front LED: unavailable"),
)


def check_key_led(button, want, label):
    classes = {c for c in button.get_css_classes() if c.startswith("led-")}
    check(
        button.has_css_class("tv-key") and classes == {f"led-{want}"},
        f"TV key carries only LED state {want} ({button.get_css_classes()})",
    )
    check(
        button.get_tooltip_text().endswith(f" · {label}"),
        f"TV key tooltip retains the action and micom LED text ({button.get_tooltip_text()})",
    )
    icons = [
        w
        for w in descendants(button)
        if isinstance(w, Gtk.Image)
        and w.get_icon_name() == "beanjar-power-symbolic"
        and w.get_ancestor(Gtk.Popover) is None
    ]
    check(len(icons) == 1, "TV key retains its native power icon")
    if icons:
        expected = button.get_color()
        if want == "red":
            expected = Gdk.RGBA()
            expected.parse("#e53935")
        check(
            icons[0].get_color().equal(expected),
            f"TV key power icon has {'red LED' if want == 'red' else 'normal'} colour "
            f"({icons[0].get_color().to_string()})",
        )


corelib.install_signals()
core = corelib.Core()
core.option("address", f"unix:path={tmp}/dbus.sock")
core.option("size", "960x540")
core.option("no-audio")
core.option("no-media")
app = Adw.Application(application_id=None)
state = {}


def transparent_pointer(view):
    cursor = view.get_cursor()
    if cursor is None:
        return False
    texture = cursor.get_texture()
    if texture is None or (texture.get_width(), texture.get_height()) != (1, 1):
        return False
    pixels = Gdk.pixbuf_get_from_texture(texture)
    return (
        cursor.get_hotspot_x() == cursor.get_hotspot_y() == 0
        and pixels is not None
        and pixels.get_has_alpha()
        and bytes(pixels.get_pixels()) == bytes(4)
    )


def steps(win, pane):
    view, bar, toggle = pane.view, pane.bar, win.remote_button
    # 1. picture and layout
    for _ in range(100):
        if core.counter("frames") and win._fitted:
            break
        yield 50
    yield 300
    check(core.counter("frames") >= 1, "the fake scanout arrived")
    check(
        (view.get_width(), view.get_height()) == (960, 540),
        f"picture area is --size 960x540 (got {view.get_width()}x{view.get_height()})",
    )
    vx, vy = translate(view, pane, 0, view.get_height())
    bx, by = translate(bar, pane, 0, 0)
    # translate_coordinates works on content boxes: the bar's 1px border and 1px padding
    check(
        0 <= by - vy <= 3,
        f"the status bar is right under the picture (bar top {by}, picture bottom {vy})",
    )
    cpu_x, _ = translate(bar.cpu, bar, 0, 0)
    check(
        cpu_x > bar.get_width() / 2 and not any(f.get_focusable() for f in bar.fields),
        "activity icons are on the right, without keyboard focus",
    )
    check(
        not hasattr(bar, "power")
        and not any(w.has_css_class("power-led") for w in descendants(bar)),
        "the status bar has no separate power LED",
    )
    check_key_led(win.local_key_button, "unknown", "Front LED: unavailable")
    # The stats' one-second tick would replace the hand-set CPU source with "no QEMU process"
    # (there is none) while the checks wait: no tick until they are done.
    GLib.source_remove(pane.stats._timer)
    cpu_source = pane.stats.src[0]
    cpu_source.ok, cpu_source.a, cpu_source.cores = True, 180, ((0, 100), (1, 50), (2, 25), (3, 0))
    bar.update()
    yield 100
    check(
        not bar.cpu.icon.get_visible()
        and bar.cpu.meter.values == (100, 50, 25, 0)
        and bar.cpu.meter.get_height() == 16
        and not bar.cpu.meter.get_focusable(),
        "CPU icon is a live meter with one bar per vCPU and no keyboard focus",
    )
    check("vCPU 1: 50%" in bar.cpu.get_tooltip_text(), "CPU meter retains detailed usage tooltip")
    gpu_source = pane.stats.src[statslib.GPU]
    gpu_source.ok, gpu_source.a = True, 37
    gpu_source.gpu = {
        "enabled": True,
        "available": True,
        "driver": "v3d",
        "engines": {"render": 37.0, "bin": 2.0},
        "headline": 37.0,
        "resident_memory_kib": 317440,
        "total_memory_kib": 409600,
    }
    bar.update()
    check(
        bar.gpu.meter.values == (37,)
        and not bar.gpu.icon.get_visible()
        and "render: 37%" in bar.gpu.get_tooltip_text()
        and "Memory: 310 MiB" in bar.gpu.get_tooltip_text(),
        "GPU meter beside the CPU's, with every engine and the memory in its tooltip",
    )
    memory_source = pane.stats.src[statslib.MEM]
    memory_source.ok, memory_source.moved, memory_source.a = True, True, 41
    memory_source.bars = (41, 2)
    host = {
        "ram": {
            "used": 1910784,
            "total": 4194304,
            "of": "its MemoryHigh",
            "parts": "",
            "cache": 348160,
            "why": "",
            "source": "unit",
        },
        "swap": {
            "used": 125952,
            "total": 6291456,
            "of": "the host's swap",
            "why": "",
            "source": "unit",
        },
    }
    memory_source.memory = {
        "guest": {"total": 2037760, "available": 1206272, "used": 831488},
        "guest_why": "",
        "swap": {"total": 614396, "free": 602108, "used": 12288},
        "swap_why": "",
        "host": host,
    }
    cpu_source.moved = gpu_source.moved = True
    bar.update()
    yield 50
    colours = [f.meter.get_color().to_string() for f in (bar.cpu, bar.gpu, bar.memory)]
    order, child = [], bar.get_first_child()
    while child:
        order, child = order + [child], child.get_next_sibling()
    order = order[-5:]
    check(
        order == [bar.cpu, bar.gpu, bar.memory, bar.disk, bar.net]
        and bar.memory.meter.values == (41, 2)
        and not bar.memory.icon.get_visible()
        and not bar.memory.meter.get_focusable(),
        "one memory meter, guest RAM and swap bars, beside the CPU and GPU ones",
    )
    check(
        len(set(colours)) == 3 and all(f.has_css_class("active") for f in (bar.gpu, bar.memory)),
        f"CPU, GPU and memory meters each have their own colour: {colours}",
    )
    check(
        bar.memory.get_tooltip_text()
        == "Memory (guest)\nRAM: 812 of 1990 MiB (41%) in use\nSwap: 12 of 600 MiB (2%) in use\n"
        "Host: 1866 of 4096 MiB (46%), the VM's unit, against its MemoryHigh\n"
        "Page cache: 340 MiB of it\n"
        "Host swap: 123 of 6144 MiB (2%), the VM's unit, against the host's swap",
        "memory tooltip: the guest's RAM and swap, then the host footprint",
    )
    memory_source.bars = (41,)
    memory_source.memory["swap"] = {"total": 0, "free": 0, "used": 0}
    bar.update()
    check(
        bar.memory.meter.values == (41,) and "\nSwap: none\n" in bar.memory.get_tooltip_text(),
        "a guest without swap has the RAM bar alone and says so",
    )
    memory_source.unavailable("waiting for the guest's balloon statistics")
    bar.update()
    check(
        bar.memory.meter.values == (None,)
        and bar.memory.has_css_class("off")
        and bar.memory.get_tooltip_text() == "Memory: waiting for the guest's balloon statistics",
        "memory without guest statistics is dim and says why",
    )
    gpu_source.unavailable("unused")
    gpu_source.gpu["enabled"] = True
    bar.update()
    check(
        bar.gpu.meter.values == (None,)
        and bar.gpu.get_tooltip_text() == "GPU: on, usage unavailable",
        "unmeasurable enabled GPU is dim without a stale percentage",
    )
    cpu_source.unavailable("no QEMU process")
    bar.update()
    check(
        bar.cpu.meter.values == (None,), "unavailable CPU has a dim meter without stale core bars"
    )
    pane.stats._timer = GLib.timeout_add(1000, pane.stats._tick)
    for standby in (False, True, False):
        pane.stats._qmp_line(
            json.dumps(
                {"id": "chardev", "return": [{"label": "standby", "frontend-open": standby}]}
            )
        )
        yield 50
        check(
            pane.stats.power_state == standby and view.get_opacity() == (0.0 if standby else 1.0),
            f"guest standby {standby} updates the reported power state and picture",
        )
        check_key_led(win.local_key_button, "unknown", "Front LED: unavailable")
    pane.stats._qmp_reset("test disconnect")
    bar.update()
    check(
        pane.stats.power_state is None and view.get_opacity() == 1.0,
        "lost QMP connection clears standby and restores the picture",
    )
    shot = os.path.join(tmp, "shot.ppm")
    check(view.screenshot(shot), "screenshot written")
    data = open(shot, "rb").read()
    m = re.match(rb"P6\n(\d+) (\d+)\n255\n", data)
    w, h = int(m[1]), int(m[2])
    px = lambda x, y: tuple(data[m.end() + (y * w + x) * 3 : m.end() + (y * w + x) * 3 + 3])  # noqa: E731
    check((w, h) == (960, 540), f"screenshot is the picture area only: {w}x{h}")
    check(
        px(5, 5) == (32, 128, 192) and px(w - 5, h - 5) == (32, 128, 192),
        f"screenshot corners are the guest picture (no chrome): {px(5, 5)} {px(w - 5, h - 5)}",
    )
    check(
        px(w // 2 + 40, h // 2 + 40) == (255, 255, 255),
        "screenshot centre is the guest's white box",
    )

    check(
        [c.__class__.__name__ for c in children(pane)] == ["Overlay", "StatusBar"]
        and pane.picture_overlay.get_child() is view,
        f"the pane is the picture (its layers over it) over the bar only "
        f"({[c.__class__.__name__ for c in children(pane)]})",
    )
    keys = (win.back_button, win.home_button, win.settings_button)
    buttons = keys + (win.screenshot_button, win.fullscreen_button, toggle)
    check(
        not any(b.get_focusable() for b in buttons)
        and all(
            k.get_parent().has_css_class("linked") and k.get_parent() is keys[0].get_parent()
            for k in keys
        )
        and (win.screenshot_button.get_action_name(), win.fullscreen_button.get_action_name())
        == ("win.screenshot", "win.fullscreen")
        and not win.lookup_action("quit"),
        "the header bar has the linked remote keys, Save Screenshot, Fullscreen and the Remote toggle,"
        " none focusable, no menu",
    )
    theme = Gtk.IconTheme.get_for_display(win.get_display())
    icons = [b.get_icon_name() for b in buttons]
    check(
        all(theme.has_icon(i) for i in icons)
        and toggle.get_icon_name() == "remote-control-symbolic",
        f"every header bar icon is in the theme, the remote's the viewer's own ({icons})",
    )
    used = icon_names(
        [
            "host/ui/data/viewer/*.blp",
            "host/ui/beanjar_ui/viewer/*.py",
            "host/core/beanjar/devices/remote_layouts.py",  # the remotes' glyphs
        ]
    )
    missing = [n for n in used if not theme.has_icon(n)]
    check(
        len(used) > 20 and not missing,
        f"every icon the viewer names is in the theme ({len(used)} names; missing {missing})",
    )
    # the desktop's icon theme not installed (the Pi's 'PiXtrix'): Adwaita instead; one installed kept
    settings = Gtk.Settings.get_for_display(win.get_display())
    settings.set_property("gtk-icon-theme-name", "NoSuchTheme-beanjar")
    lost = [n for n in used if not theme.has_icon(n)]
    replaced = uilib.theme_fallback(win.get_display())
    missing = [n for n in used if not theme.has_icon(n)]
    check(
        lost
        and replaced == "NoSuchTheme-beanjar"
        and theme.get_theme_name() == "Adwaita"
        and not missing,
        f"a theme that is not installed falls back to Adwaita, and the icons resolve ({len(lost)} lost before)",
    )
    settings.set_property("gtk-icon-theme-name", "hicolor")
    check(
        uilib.theme_fallback(win.get_display()) is None and theme.get_theme_name() == "hicolor",
        "an installed theme (hicolor) is left alone",
    )
    settings.set_property("gtk-icon-theme-name", "Adwaita")
    check(
        theme.has_icon("org.webosbrew.beanjar.viewer")
        and Gtk.Window.get_default_icon_name() == "org.webosbrew.beanjar.viewer"
        and win.get_title() == "tv1 - Beanjar Viewer",
        f"the application icon is in the theme and the windows' default; the title ({win.get_title()})",
    )
    check(
        (win.title.get_title(), win.title.get_subtitle()) == ("tv1", "FW-NAME"),
        f"the header bar shows the VM's name and firmware ({win.title.get_title()}, {win.title.get_subtitle()})",
    )
    check(
        Adw.StyleManager.get_default().get_color_scheme() == Adw.ColorScheme.PREFER_DARK,
        "the viewer prefers dark",
    )
    win_size = (win.get_width(), win.get_height())

    # 2. --remote: shown at start in its own window, right of the TV window
    for _ in range(40):
        if pane.remote_window is not None and pane.remote_window.placed is not None:
            break
        yield 50
    yield 300
    rwin, panel = pane.remote_window, pane.remote
    check(
        rwin is not None
        and rwin.get_visible()
        and toggle.get_active()
        and panel.get_root() is rwin,
        "--remote showed the remote in its own window",
    )
    at = lambda: screen_point(rwin, 0, 0)  # noqa: E731
    tv_right = screen_point(win, win.get_width(), 0)[0]
    check(
        rwin.placed and at()[0] >= tv_right,
        f"placed right of the TV window ({at()} vs right edge {tv_right})",
    )
    check((win.get_width(), win.get_height()) == win_size, "the TV window kept its size")
    check(
        rwin.get_transient_for() is win,
        "the remote's window is transient for the TV window (kept above it)",
    )
    # its size: 1.125x the picture's height, at 1.125x to 2.25x the layout; here (540) the least,
    # 212x734: taller than the picture, within the 1920x1200 screen
    normal = remotelib.NORMAL
    size = lambda: (rwin.get_width(), rwin.get_height())  # noqa: E731
    fit = lambda s: (round(remotelib.REMOTE_W * s), round(normal.height * s))  # noqa: E731
    check(
        size() == fit(1.125) == (212, 734) and view.get_height() < rwin.get_height() <= 1200,
        f"the remote is 1.125x its layout beside a 540 px picture, taller than it ({size()})",
    )
    check(
        remotelib.window_scale(540) == 1.125
        and remotelib.window_scale(1000) == 1.125 * 1000 / normal.height
        and remotelib.window_scale(4000) == 2.25,
        "window_scale: 1.125x the picture, 1.125x to 2.25x",
    )
    # the Remote toggle: hides and shows it, never reaches the guest
    move(view, 100, 100)
    yield 200
    before = len(log_lines())
    click(toggle)
    yield 500
    check(not rwin.get_visible() and not toggle.get_active(), "the Remote toggle hid the remote")
    click(toggle)
    yield 600
    after = log_lines()[before:]
    check(
        rwin.get_visible() and toggle.get_active() and pane.remote_window is rwin,
        "the Remote toggle showed the same remote window again",
    )
    check(
        not any(l.startswith("Mouse.") for l in after),
        f"the toggle's clicks never reached the guest ({len(after)} input calls: {after[:3]})",
    )
    check(
        (win.get_width(), win.get_height()) == win_size and view.get_width() == 960,
        f"the TV window and picture kept their size ({win.get_width()}x{win.get_height()})",
    )
    # the header bar's remote keys: their IR codes, down then up; a long press holds
    before = len(log_lines())
    for button in keys:
        click(button)
        yield 300
    got = [l for l in log_lines()[before:] if l.startswith(("Keyboard.", "Mouse.", "IR."))]
    expect = [f"IR.{e} 0x{c:02x}" for c in (0x28, 0x7C, 0x43) for e in ("Down", "Up")]
    check(got == expect, f"Back, Home and Settings are the remote's BACK, HOME and MENU ({got})")
    before = len(log_lines())
    move(win.home_button, win.home_button.get_width() / 2, win.home_button.get_height() / 2)
    xtst.XTestFakeButtonEvent(dpy, 1, 1, 0)
    x11.XFlush(dpy)
    yield 700
    held = [l for l in log_lines()[before:] if l.startswith("IR.")]
    xtst.XTestFakeButtonEvent(dpy, 1, 0, 0)
    x11.XFlush(dpy)
    yield 300
    got = [l for l in log_lines()[before:] if l.startswith("IR.")]
    check(
        held == ["IR.Down 0x7c"]
        and got == ["IR.Down 0x7c", "IR.Up 0x7c"]
        and pane.key_held is None,
        f"a long press on Home holds it until mouse-up ({held} then {got})",
    )
    # the set's own key (onekey): left of Back; a click is one local key edge pair, a hold held
    group = win.local_key_group
    check(
        group.get_visible()
        and win.local_key_button.get_visible()
        and not win.joystick_button.get_visible()
        and screen_point(group, 0, 0)[0] < screen_point(win.back_button, 0, 0)[0]
        and not win.local_key_button.get_focusable(),
        "a onekey model shows its one key, not focusable, left of Back",
    )
    check(
        not hasattr(win, "front_led")
        and not any(
            w.has_css_class("front-led") or w.__class__.__name__ == "FrontLed"
            for w in descendants(win)
        )
        and children(win.title_label.get_parent()) == [win.title_label, win.subtitle_label],
        "the title contains only its two labels, with no separate LED dot",
    )
    before = len(log_lines())
    click(win.local_key_button)
    yield 300
    got = [
        l for l in log_lines()[before:] if l.startswith(("Keyboard.", "Mouse.", "IR.", "Local."))
    ]
    check(
        got == ["Local.Down 0xea", "Local.Up 0xea"],
        f"the TV's key is a local key press, not IR ({got})",
    )
    before = len(log_lines())
    move(
        win.local_key_button,
        win.local_key_button.get_width() / 2,
        win.local_key_button.get_height() / 2,
    )
    xtst.XTestFakeButtonEvent(dpy, 1, 1, 0)
    x11.XFlush(dpy)
    yield 700
    held = [l for l in log_lines()[before:] if l.startswith("Local.")]
    xtst.XTestFakeButtonEvent(dpy, 1, 0, 0)
    x11.XFlush(dpy)
    yield 300
    got = [l for l in log_lines()[before:] if l.startswith("Local.")]
    check(
        held == ["Local.Down 0xea"]
        and got == ["Local.Down 0xea", "Local.Up 0xea"]
        and pane.local_held is None,
        f"a long press on the TV's key holds it until mouse-up, for the micom to time ({held} then {got})",
    )
    # The key's power icon mirrors the micom's LED (micom-status's last line).
    status = os.path.join(tmp, "micom-status")
    with open(status, "w") as f:
        f.write(
            "standby=0 power=3\nstandby=0 power=3 led.6e=010101/1\nstandby=1 power=2 led.6e=0101"
        )
    check(
        statslib._last_line(status) == "standby=0 power=3 led.6e=010101/1",
        "micom-status: the last complete line is the state",
    )
    shots = os.environ.get("BEANJAR_HEADER_SHOTS")

    def shot_of(widget, name, area=None):
        """widget as it is on screen, inside its window (the window's own background and the
        top bar's under it), cropped to area (x, y, w, h in the window), PNG in shots."""
        native = widget.get_native()
        x, y = translate(widget, native, 0, 0)
        x, y, w, h = area or (x, y, widget.get_width(), widget.get_height())
        # at the paintable's own size, which has the window's shadow margins: at the window's size
        # it would be scaled down and blur everything a little
        paint = Gtk.WidgetPaintable.new(native)
        pw, ph = paint.get_intrinsic_width(), paint.get_intrinsic_height()
        snapshot = Gtk.Snapshot()
        paint.snapshot(snapshot, pw, ph)
        rect = Graphene.Rect().init(
            x + (pw - native.get_width()) / 2, y + (ph - native.get_height()) / 2, w, h
        )
        texture = native.get_renderer().render_texture(snapshot.to_node(), rect)
        os.makedirs(shots, exist_ok=True)
        texture.save_to_png(os.path.join(shots, name))

    def header_shot(name):
        """The header bar, the full width of the window."""
        if not shots:
            return
        bar = next(w for w in descendants(win) if isinstance(w, Adw.HeaderBar))
        x, y = translate(bar, win, 0, 0)
        shot_of(win, name, (x, y, bar.get_width(), bar.get_height()))

    # Stats reads micom-status only while QEMU runs: this process stands in for it meanwhile
    pane.stats._qemu_pid = lambda: (os.getpid(), "")
    for line, want, label in MICOM_LED_CASES:
        with open(status, "a") as f:
            f.write("\n" + line + "\n")
        yield 1300
        check(
            statslib.front_led(pane.stats.micom) == (want, label), f"front LED mapping for '{line}'"
        )
        off = "standby=1" in line or "power=2" in line
        check(
            view.get_opacity() == (0.0 if off else 1.0) and core.counter("standby") == int(off),
            f"micom '{line}': the picture is {'off' if off else 'on'}, LG's cursor "
            f"{'not ' if off else ''}drawn",
        )
        for key_button in (win.local_key_button, win.joystick_button):
            check_key_led(key_button, want, label)
        check(
            win.joystick_press.get_child().get_color().equal(win.joystick_press.get_color()),
            "the header LED tint does not reach the joystick popover's press icon",
        )
        if line == "standby=0 power=3 led.6e=010101/1":
            header_shot("header-onekey-led-off.png")
        elif line == "standby=1 power=2 led.6e=010101/1":
            header_shot("header-onekey-led-red.png")
            if shots:
                # the light style (a desktop that prefers light), and the viewer's default width
                styles = Adw.StyleManager.get_default()
                styles.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
                yield 400
                check_key_led(win.local_key_button, want, label)
                check_key_led(win.joystick_button, want, label)
                header_shot("header-onekey-led-red-light.png")
                styles.set_color_scheme(Adw.ColorScheme.PREFER_DARK)
                default = win.get_default_size()
                win.set_default_size(default[0] + 1280 - view.get_width(), default[1])
                for _ in range(20):
                    yield 100
                    if view.get_width() == 1280:
                        break
                header_shot("header-1280-onekey-led-red.png")
                styles.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
                yield 400
                header_shot("header-1280-onekey-led-red-light.png")
                styles.set_color_scheme(Adw.ColorScheme.PREFER_DARK)
                win.set_default_size(*default)
                for _ in range(20):
                    yield 100
                    if view.get_width() == 960:
                        break
                # a joystick model's five ways in a popover (the same window, switched for the picture)
                win.local_key_button.set_visible(False)
                win.joystick_button.set_visible(True)
                win.joystick_popover.popup()
                yield 400
                header_shot("header-joystick.png")
                shot_of(win.joystick_popover, "joystick-popover.png")
                win.joystick_popover.popdown()
                win.joystick_button.set_visible(False)
                win.local_key_button.set_visible(True)
                yield 200
    del pane.stats._qemu_pid
    yield 1300
    for key_button in (win.local_key_button, win.joystick_button):
        check_key_led(key_button, "unknown", "Front LED: unavailable")
    header_shot("header-onekey-led-unknown.png")
    # the remote follows the picture's size: a taller TV window, a taller remote, and back
    default = win.get_default_size()
    win.set_default_size(default[0] + 640, default[1] + 460)
    for _ in range(20):
        yield 100
        if view.get_height() == 1000 and rwin.get_height() > fit(1.125)[1]:
            break
    check(
        view.get_height() == 1000
        and size() == fit(1.125 * 1000 / normal.height)
        and abs(rwin.get_height() - 1.125 * view.get_height()) <= 1
        and rwin.get_height() <= 1200,
        f"beside a 1000 px picture the remote is 1.125x as tall, within the screen ({size()})",
    )
    win.set_default_size(*default)
    for _ in range(20):
        yield 100
        if view.get_height() == 540 and size() == fit(1.125):
            break
    check(
        (win.get_width(), win.get_height()) == win_size and size() == fit(1.125),
        f"and back with the TV window's size ({size()})",
    )

    # 3. pointer: transparent over the picture while LG's cursor shows, the arrow over the bar
    move(view, view.get_width() / 2, view.get_height() / 2)
    yield 300
    check(transparent_pointer(view), "explicit 1x1 zero-alpha host cursor over the picture")
    cursor, blanks = view.get_cursor(), core.counter("cursor_blank_sets")
    view._update_cursor()
    check(
        view.get_cursor() == cursor and core.counter("cursor_blank_sets") == blanks,
        "unchanged pointer state reuses the transparent cursor",
    )
    check(
        any(l.startswith("Mouse.SetAbsPosition") for l in log_lines()),
        "pointer motion reached the tablet",
    )
    move(bar, 10, bar.get_height() / 2)
    yield 300
    check(view.get_cursor() is None, "desktop arrow over the status bar")

    # 4. keys in the TV window: all to the guest; the Menu key hides and shows the remote
    focus(win)
    move(view, view.get_width() / 2, view.get_height() / 2)
    yield 300
    before = len(log_lines())
    for name in (
        "Tab",
        "space",
        "F10",
        "Super_L",
        "Home",
        "Escape",
        "Return",
        "a",
        "XF86AudioRaiseVolume",
        "XF86AudioLowerVolume",
        "XF86AudioMute",
    ):
        key(name)
        yield 60
    yield 400
    presses = [l.split()[1] for l in log_lines()[before:] if l.startswith("Keyboard.Press")]
    releases = [l for l in log_lines()[before:] if l.startswith("Keyboard.Release")]
    expect = ["15", "57", "68", "28", "30"]
    check(
        presses == expect, f"keyboard keys reach the guest as QEMU numbers {expect} (got {presses})"
    )
    check(len(releases) == len(expect), f"every key released ({len(releases)})")
    irs = [l for l in log_lines()[before:] if l.startswith("IR.")]
    expect = [
        f"IR.{e} 0x{c:02x}" for c in (0x7C, 0x7C, 0x28, 0x02, 0x03, 0x09) for e in ("Down", "Up")
    ]
    check(
        irs == expect,
        f"Super, Home, Escape and the volume keys are the remote's HOME, BACK, VOL and MUTE"
        f" ({irs})",
    )
    check(
        rwin.get_visible() and toggle.get_active(),
        "Space/Return/Escape in the TV window left the remote",
    )
    before = len(log_lines())
    key("Menu", 120)
    yield 500
    check(not rwin.get_visible() and not toggle.get_active(), "Menu key hid the remote")
    check(
        not any(l.startswith("Keyboard.") for l in log_lines()[before:]),
        "Menu key not sent to the guest",
    )
    key("Menu", 120)
    yield 600
    check(rwin.get_visible() and toggle.get_active(), "Menu key showed the remote again")
    check((win.get_width(), win.get_height()) == win_size, "the TV window kept its size")
    # F11: fullscreen (no window manager here: the request is not granted, so only the call and the
    # chrome are checked), never the guest
    calls = []
    real, pane.on_fullscreen = pane.on_fullscreen, lambda: calls.append(1)
    before = len(log_lines())
    key("F11", 120)
    yield 400
    pane.on_fullscreen = real
    check(
        calls == [1] and not any(l.startswith("Keyboard.") for l in log_lines()[before:]),
        f"F11 toggles fullscreen, once, and is not sent ({calls})",
    )
    win.set_chrome(False)
    yield 300
    check(
        not win.toolbar.get_reveal_top_bars()
        and not bar.get_visible()
        and (view.get_width(), view.get_height()) != (960, 540),
        "fullscreen chrome: no header bar, no status bar",
    )
    win.set_chrome(True)
    yield 300
    check(
        win.toolbar.get_reveal_top_bars() and bar.get_visible() and view.get_height() == 540,
        "and both back after it",
    )

    # 5. a remote button: its IR code down and up (5 = 0x15); a long press
    five = panel.key_widget("5")
    ir5 = ["IR.Down 0x15", "IR.Up 0x15"]
    before = len(log_lines())
    click(five)
    yield 400
    got = [l for l in log_lines()[before:] if l.startswith(("Keyboard.", "IR."))]
    check(got == ir5, f"remote button 5 sent its IR code ({got})")
    before = len(log_lines())
    move(five, five.get_width() / 2, five.get_height() / 2)
    xtst.XTestFakeButtonEvent(dpy, 1, 1, 0)
    x11.XFlush(dpy)
    yield 700
    held = [l for l in log_lines()[before:] if l.startswith(("Keyboard.", "IR."))]
    xtst.XTestFakeButtonEvent(dpy, 1, 0, 0)
    x11.XFlush(dpy)
    yield 300
    got = [l for l in log_lines()[before:] if l.startswith(("Keyboard.", "IR."))]
    check(
        held == ir5[:1] and got == ir5,
        f"a long press holds the button until mouse-up ({held} then {got})",
    )

    # 6. borderless, shaped, dragged by the body
    check(not rwin.get_decorated(), "the remote's window is undecorated")
    shape = input_shape(rwin)
    pt = lambda x, y: translate(panel, rwin, *panel.to_panel(x, y))  # noqa: E731
    bx, by, bw, bh, rt, rb = remotelib.BODY
    cx, cy = pt(94, 300)
    kx, ky = pt(6, 6)
    # the outline: just inside and outside each small corner, the chin, and below it
    ins = {
        "top-left": pt(bx + rt * 0.4, by + rt * 0.4),
        "top-right": pt(bx + bw - rt * 0.4, by + rt * 0.4),
        "bottom-left": pt(bx + rb * 0.4, by + bh - rb * 0.4),
        "bottom-right": pt(bx + bw - rb * 0.4, by + bh - rb * 0.4),
        "chin": pt(bx + 10, 630),
        "top edge": pt(94, by + 1),
        "bottom edge": pt(94, by + bh - 1),
    }
    outs = {
        "top-left": pt(bx + 1, by + 1),
        "top-right": pt(bx + bw - 1, by + 1),
        "bottom-left": pt(bx + 1, by + bh - 1),
        "bottom-right": pt(bx + bw - 1, by + bh - 1),
        "below": pt(94, by + bh + 2),
        "left of": pt(bx - 2, 300),
    }
    wrong = [f"{k} in" for k, p in ins.items() if not inside(shape, *p)]
    wrong += [f"{k} out" for k, p in outs.items() if inside(shape, *p)]
    check(
        rt <= 20 and rb <= 12 and bh >= 640,
        f"the body is slim and nearly square-cornered with a chin ({remotelib.BODY})",
    )
    check(
        len(shape) > 2
        and inside(shape, cx, cy)
        and not inside(shape, kx, ky)
        and not inside(shape, 1, 1)
        and not inside(shape, rwin.get_width() - 2, rwin.get_height() - 2)
        and not wrong,
        f"input region is the body's outline, corners and chin ({len(shape)} rects; wrong: {wrong})",
    )
    # over the picture (no WM: the test moves it), so a press in a corner lands on the picture
    tx, ty = screen_point(view, 100, 20)
    x11.XMoveWindow(dpy, GdkX11.X11Surface.get_xid(rwin.get_surface()), tx, ty)
    x11.XFlush(dpy)
    yield 400
    pos, moves = at(), rwin.moves
    before = len(log_lines())
    yield from press_drag(panel, *panel.to_panel(5, 5), 60, 40)
    check(
        rwin.moves == moves
        and at() == pos
        and any(l.startswith("Mouse.Press") for l in log_lines()[before:]),
        f"a press in a transparent corner falls through to the picture (moves {rwin.moves - moves},"
        f" at {at()} vs {pos})",
    )
    before = len(log_lines())
    yield from press_drag(panel, *panel.to_panel(10, 300), 60, 40)
    moved = at()
    check(rwin.moves == moves + 1, f"a drag on the body called begin_move ({rwin.moves - moves})")
    check(
        abs(moved[0] - pos[0] - 60) <= 4 and abs(moved[1] - pos[1] - 40) <= 4,
        f"the window moved with the drag, no WM: GDK's own move ({pos} -> {moved})",
    )
    check(
        not [l for l in log_lines()[before:] if l.startswith(("Keyboard.", "Mouse.", "IR."))],
        f"the drag sent nothing to the guest ({log_lines()[before:][:3]})",
    )
    before = len(log_lines())
    click(five)
    yield 400
    got = [l for l in log_lines()[before:] if l.startswith(("Keyboard.", "IR."))]
    check(
        got == ir5 and rwin.moves == moves + 1,
        f"remote button 5 sent its IR code after the drag, no move ({got})",
    )
    # the chin on the screen: the remote is nearly as tall as the screen, and the TV window's
    # header bar put the picture (and so the remote, above) lower
    screen_h = rwin.get_display().get_monitors().get_item(0).get_geometry().height
    x11.XMoveWindow(
        dpy,
        GdkX11.X11Surface.get_xid(rwin.get_surface()),
        at()[0],
        max(0, min(at()[1], screen_h - rwin.get_height() - 8)),
    )
    x11.XFlush(dpy)
    yield 400
    pos = at()
    before = len(log_lines())
    yield from press_drag(panel, *panel.to_panel(24, 630), -50, -30)
    moved = at()
    check(
        rwin.moves == moves + 2
        and abs(moved[0] - pos[0] + 50) <= 4
        and abs(moved[1] - pos[1] + 30) <= 4,
        f"a drag on the chin moved the window ({pos} -> {moved})",
    )
    check(
        not [l for l in log_lines()[before:] if l.startswith(("Keyboard.", "Mouse.", "IR."))],
        "the chin drag sent nothing to the guest",
    )

    # 7. keys typed in the remote's window reach the guest; the Menu key there hides it
    focus(rwin)
    move(panel, *panel.to_panel(10, 300))
    yield 300
    before = len(log_lines())
    key("a")
    yield 300
    got = [l for l in log_lines()[before:] if l.startswith("Keyboard.")]
    check(
        got == ["Keyboard.Press 30", "Keyboard.Release 30"],
        f"a key typed in the remote's window reached the guest ({got})",
    )
    # the pointer over the picture: with no WM, the keyboard reverts there once the remote hides
    # (a WM would focus the TV window), so the Menu key's release is not lost
    move(view, view.get_width() / 2, view.get_height() / 2)
    yield 200
    before = len(log_lines())
    key("Menu", 120)
    yield 500
    check(
        not rwin.get_visible() and not toggle.get_active(), "Menu key in the remote's window hid it"
    )
    check(
        not any(l.startswith("Keyboard.") for l in log_lines()[before:]),
        "that Menu key was not sent",
    )
    focus(win)
    move(view, view.get_width() / 2, view.get_height() / 2)
    yield 300
    key("Menu", 120)
    yield 600
    check(
        rwin.get_visible() and toggle.get_active(), "the Menu key in the TV window showed it again"
    )
    shape = input_shape(rwin)
    check(
        len(shape) > 2 and inside(shape, cx, cy) and not inside(shape, kx, ky),
        f"shown again, the input region is still the body ({len(shape)} rects)",
    )

    # 8. the chin's controls: below the colour keys, the menu and close; nothing reaches the guest
    close, menu = panel.controls["close"], panel.controls["menu"]
    blue = panel.key_widget("BLUE")
    chin_top = translate(blue, panel, 0, blue.get_height())[1]
    body_bottom = panel.to_panel(0, remotelib.BODY[1] + remotelib.BODY[3])[1]
    placed = [translate(c, panel, 0, 0)[1] for c in (menu, close)]
    check(
        sorted(panel.controls) == ["close", "menu"]
        and close.get_visible()
        and menu.get_visible()
        and close.get_width() > 10
        and all(chin_top < y < body_bottom - 10 for y in placed),
        f"two controls on the chin, menu and close (tops {placed}, chin {chin_top:.0f}..{body_bottom:.0f})",
    )
    guest_start = len(log_lines())
    click(close)
    yield 500
    check(
        not rwin.get_visible() and not toggle.get_active() and pane.remote_window is rwin,
        "the close control closed the remote (toggle off, the window kept)",
    )
    click(toggle)
    yield 600
    check(rwin.get_visible() and toggle.get_active(), "the Remote toggle opened it again")
    focus(rwin)

    def menu_ready():
        """The menu is open and laid out: a popover is mapped as soon as it is shown, but its
        items are allocated only at its surface's next layout, and each layout's chin has its
        own menu control and popover. A click before then hits a 0x0 item."""
        popover = menu.get_popover()
        if not (panel.menu_open() and popover.get_mapped() and popover.get_surface()):
            return False
        labels = [w for w in descendants(popover) if isinstance(w, Gtk.Label) and w.get_mapped()]
        return bool(labels) and all(w.get_width() > 0 and w.get_height() > 0 for w in labels)

    def open_menu():
        click(menu)
        for _ in range(50):
            yield 100
            if menu_ready():
                break
        # GDK's map and move of the popover reach the X server before the test's XTest clicks
        Gdk.Display.get_default().sync()

    def laid_out(layout):
        """The panel shows layout at the window's new size: set_layout builds new key widgets,
        allocated only once the window has taken the layout's size."""
        if panel.layout is not layout or panel.menu_open():
            return False
        w, h = rwin.get_default_size()
        keys = [panel.key_widget(k.name) for k in layout.keys]
        return (
            abs(rwin.get_width() - w) <= 2
            and abs(rwin.get_height() - h) <= 2
            and all(k.get_width() > 0 and k.get_height() > 0 for k in keys)
        )

    def switch(label, layout):
        """Choose the menu's item label and wait until layout is laid out on screen."""
        click(item(label))
        for _ in range(50):
            yield 100
            if laid_out(layout):
                break
        Gdk.Display.get_default().sync()

    def item(label):
        """The menu's item labelled label (a widget in the popover), or None."""
        todo = [menu.get_popover()]
        while todo:
            w = todo.pop()
            if isinstance(w, Gtk.Label) and w.get_label() == label and w.get_mapped():
                return w
            todo += children(w)
        return None

    yield from open_menu()
    model = menu.get_menu_model()
    labels = [
        model.get_item_link(i, "section").get_item_attribute_value(j, "label").unpack()
        for i in range(model.get_n_items())
        for j in range(model.get_item_link(i, "section").get_n_items())
    ]
    check(
        panel.menu_open()
        and labels
        == [layout.title for layout in remotelib.LAYOUTS.values()] + ["Place Next to TV"],
        f"the menu control opened the remote's menu ({labels})",
    )
    check(
        all(item(l) for l in labels), "all remote and placement items shown (Place Next to TV: X11)"
    )
    key("Escape")
    yield 500
    check(
        not panel.menu_open() and rwin.get_visible(),
        "Escape with the menu open closed the menu only",
    )
    tx, ty = screen_point(view, 40, 40)
    x11.XMoveWindow(dpy, GdkX11.X11Surface.get_xid(rwin.get_surface()), tx, ty)
    x11.XFlush(dpy)
    yield 400
    yield from open_menu()
    click(item("Place Next to TV"))
    yield 600
    check(
        at()[0] >= tv_right and rwin.placed,
        f"the menu's Place Next to TV put it right of the TV window ({at()})",
    )
    got = [
        l
        for l in log_lines()[guest_start:]
        if l.startswith(("Keyboard.", "Mouse.Press", "Mouse.Release", "IR."))
    ]
    check(not got, f"the chin's controls and menu sent nothing to the guest ({got[:3]})")
    before = len(log_lines())
    click(win.screenshot_button)
    yield 800
    pics = os.listdir(os.path.join(tmp, "pictures"))
    got = [
        l
        for l in log_lines()[before:]
        if l.startswith(("Keyboard.", "Mouse.Press", "Mouse.Release", "IR."))
    ]
    check(
        len(pics) == 1 and pics[0].endswith(".png") and not got,
        f"the header bar's Save Screenshot saved the guest picture, sending nothing ({pics})",
    )
    check(
        pane.said and pane.said[-1].startswith("Saved ") and pics[0] in pane.said[-1],
        f"and a toast says where ({pane.said[-1:]})",
    )

    # 9. the service remote from the chin's menu: its layout, window size and shape; back again
    normal_h = rwin.get_height()
    yield from open_menu()
    yield from switch("Service Remote", remotelib.SERVICE)
    svc = remotelib.SERVICE
    check(
        panel.layout is svc and not panel.menu_open(),
        f"the menu's Service Remote switched ({panel.layout.name})",
    )
    ratio = rwin.get_height() / rwin.get_width()
    check(
        abs(ratio - svc.height / remotelib.REMOTE_W) < 0.05,
        f"the window took the service remote's proportions ({rwin.get_width()}x{rwin.get_height()})",
    )
    yield 300
    shape = input_shape(rwin)
    sbx, sby, sbw, sbh = svc.body[:4]
    bottom = translate(panel, rwin, *panel.to_panel(94, sby + sbh - 3))
    check(
        len(shape) > 2
        and inside(shape, *bottom)
        and not inside(shape, *translate(panel, rwin, *panel.to_panel(1, 1))),
        f"the input region is the service remote's body ({len(shape)} rects)",
    )
    before = len(log_lines())
    click(panel.key_widget("IN_START"))
    yield 400
    click(panel.key_widget("ETC"))
    yield 400
    got = [l for l in log_lines()[before:] if l.startswith(("Keyboard.", "IR."))]
    check(
        got == ["IR.Down 0xfb", "IR.Up 0xfb"], f"IN START sent 0xFB, the dimmed ETC nothing ({got})"
    )
    menu = panel.controls["menu"]  # each layout has its own chin controls
    yield from open_menu()
    yield from switch("NetCast Remote", remotelib.NETCAST)
    check(
        panel.layout is remotelib.NETCAST and not panel.menu_open(), "the NetCast remote switched"
    )
    before = len(log_lines())
    click(panel.key_widget("HOME"))
    yield 400
    click(panel.key_widget("QMENU"))
    yield 400
    click(panel.key_widget("APP"))
    yield 400
    got = [l for l in log_lines()[before:] if l.startswith(("Keyboard.", "IR."))]
    # Selected RELEASE's MICOM table: NetCast/MyApps 0x42, quick menu 0x45.
    check(
        got == ["IR.Down 0x42", "IR.Up 0x42", "IR.Down 0x45", "IR.Up 0x45"],
        f"NetCast and Q.MENU sent their codes, the unresolved APP key nothing ({got})",
    )
    menu = panel.controls["menu"]
    yield from open_menu()
    yield from switch("webOS Remote", remotelib.NORMAL)
    check(
        panel.layout is remotelib.NORMAL and abs(rwin.get_height() - normal_h) <= 2,
        f"webOS Remote switched back ({panel.layout.name}, {rwin.get_height()} vs {normal_h})",
    )
    focus(rwin)
    move(panel, *panel.to_panel(10, 300))
    yield 300
    before = len(log_lines())
    key("Escape")
    yield 600
    check(
        not rwin.get_visible() and not toggle.get_active(), "Escape in the remote's window hid it"
    )
    check(
        not any(l.startswith("Keyboard.") for l in log_lines()[before:]), "that Escape was not sent"
    )
    click(toggle)
    yield 600
    rwin.close()
    yield 600
    check(
        not rwin.get_visible() and not toggle.get_active() and pane.remote_window is rwin,
        "a close request hid the remote (the window kept)",
    )
    click(toggle)
    yield 600
    check(
        rwin.get_visible() and toggle.get_active(), "the toggle showed it after the close request"
    )
    # the toggle mirrors the window however it is shown or hidden: directly, or minimized by the
    # desktop (a window manager's _NET_WM_STATE_HIDDEN, set here by hand: there is none)
    rwin.set_visible(False)
    yield 300
    hidden = toggle.get_active()
    rwin.present()
    yield 500
    check(
        not hidden and toggle.get_active() and pane.remote_shown(),
        f"hidden and shown other than by the toggle, the toggle followed ({hidden}, {toggle.get_active()})",
    )
    wm_hint(True)
    minimize(rwin, True)
    yield 400
    minimized = (toggle.get_active(), pane.remote_shown(), rwin.get_visible())
    minimize(rwin, False)
    yield 400
    wm_hint(False)
    check(
        minimized == (False, False, True) and toggle.get_active() and pane.remote_shown(),
        f"minimized, the toggle is off; restored, on ({minimized}, {toggle.get_active()})",
    )
    check((win.get_width(), win.get_height()) == win_size, "the TV window never changed size")

    # webos.cursor is the only displayed cursor source. QEMU cursor methods
    # remain callable/acknowledged but cannot change rendering or pointer policy.
    def cursor_command(command):
        with open(os.path.join(tmp, "cursor-control"), "w") as out:
            out.write(command)

    def ack_count():
        return sum(line.startswith("Listener.") for line in log_lines())

    def cursor_acks():
        return [
            line
            for line in log_lines()
            if line.startswith(("Listener.CursorDefine", "Listener.MouseSet"))
        ]

    def screenshot_bytes():
        check(view.screenshot(shot), "cursor source screenshot written")
        return open(shot, "rb").read()

    # Outside the picture, real channel position applies (no host override).
    move(bar, 10, bar.get_height() / 2)
    yield 250
    real_image = screenshot_bytes()
    header = re.match(rb"P6\n(\d+) (\d+)\n255\n", real_image)
    width = int(header[1])
    start = header.end() + (271 * width + 481) * 3
    check(
        tuple(real_image[start : start + 3]) == (220, 80, 20),
        "real webos.cursor pixels render at its channel position",
    )

    # Standby: the frozen guest never hides LG's cursor, so the core skips it while the
    # pane says standby, and draws what the guest last sent after the wake. On screen (the X
    # server's pixels, not the core's offscreen render) the cursor is gone too.
    def on_screen(name):
        pixels = screen_rgb(view, 0, 0, view.get_width(), view.get_height())
        if shots and pixels:
            os.makedirs(shots, exist_ok=True)
            w, h = view.get_width(), view.get_height()
            Gdk.MemoryTexture.new(
                w, h, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(pixels), w * 3
            ).save_to_png(os.path.join(shots, name))
        pt = lambda x, y: tuple(pixels[(y * view.get_width() + x) * 3 :][:3])  # noqa: E731
        return pt(481, 271), pt(5, 5)

    def set_standby(on):
        pane.stats._qmp_line(
            json.dumps({"id": "chardev", "return": [{"label": "standby", "frontend-open": on}]})
        )

    awake_cursor, awake_picture = on_screen("standby-before.png")
    check(
        awake_cursor == (220, 80, 20) and awake_picture == (32, 128, 192),
        f"on screen: LG's cursor over the picture before standby ({awake_cursor}, {awake_picture})",
    )
    set_standby(True)
    yield 300
    draws = core.counter("cursor_draws")
    asleep = screenshot_bytes()
    check(
        core.counter("standby") == 1 and tuple(asleep[start : start + 3]) != (220, 80, 20),
        f"standby: the core does not draw LG's cursor ({tuple(asleep[start : start + 3])})",
    )
    move(view, view.get_width() / 2, view.get_height() / 2)
    yield 250
    move(bar, 10, bar.get_height() / 2)
    yield 250
    check(
        core.counter("cursor_draws") == draws, "standby: pointer motion does not draw LG's cursor"
    )
    asleep_cursor, asleep_picture = on_screen("standby-on.png")
    check(
        asleep_cursor == asleep_picture == (0, 0, 0),
        f"on screen: standby is black, without LG's cursor ({asleep_cursor}, {asleep_picture})",
    )
    set_standby(False)
    yield 300
    check(
        core.counter("standby") == 0
        and screenshot_bytes() == real_image
        and core.counter("cursor_draws") > draws,
        "after the wake LG's cursor is drawn as the guest last sent it",
    )
    woke_cursor, woke_picture = on_screen("standby-after.png")
    check(
        woke_cursor == (220, 80, 20) and woke_picture == (32, 128, 192),
        f"on screen: LG's cursor is back after the wake ({woke_cursor}, {woke_picture})",
    )
    # Active Standby (Power Off with Quick Start+): the micom's warm standby, power=2 in
    # micom-status, with the SoC running, so QMP's standby chardev stays closed.
    pane.stats._qemu_pid = lambda: (os.getpid(), "")
    for line, off in (("standby=0 power=2", True), ("standby=0 power=3", False)):
        with open(status, "a") as f:
            f.write(line + "\n")
        yield 1300
        image = screenshot_bytes()
        cursor_px = tuple(image[start : start + 3])
        check(
            not pane.stats.standby
            and core.counter("standby") == int(off)
            and (cursor_px != (220, 80, 20) if off else image == real_image),
            f"micom '{line}': LG's cursor {'not ' if off else ''}drawn ({cursor_px})",
        )
        shown_cursor, shown_picture = on_screen(f"active-standby-{'on' if off else 'off'}.png")
        want = ((0, 0, 0), (0, 0, 0)) if off else ((220, 80, 20), (32, 128, 192))
        check(
            (shown_cursor, shown_picture) == want,
            f"on screen, micom '{line}': {shown_cursor}, {shown_picture}",
        )
    del pane.stats._qemu_pid
    yield 1300
    before = ack_count()
    cursor_command("native-show")
    for _ in range(20):
        yield 50
        if ack_count() >= before + 2:
            break
    check(
        ack_count() == before + 2 and all(line.endswith(" ack") for line in cursor_acks()),
        "QEMU CursorDefine/MouseSet acknowledged while real channel connected",
    )
    check(screenshot_bytes() == real_image, "QEMU visible image cannot replace real channel pixels")
    move(view, view.get_width() / 2, view.get_height() / 2)
    yield 250
    check(
        core.pointer_blank() and transparent_pointer(view),
        "entering the picture hides host pointer",
    )
    cursor_command("native-hide")
    yield 250
    check(
        core.pointer_blank() and transparent_pointer(view),
        "QEMU MouseSet off cannot restore the host arrow or hide real cursor",
    )
    move(bar, 10, bar.get_height() / 2)
    yield 250
    check(screenshot_bytes() == real_image, "QEMU off leaves real cursor rendering unchanged")

    cursor_command("legacy-hidden")
    yield 300
    hidden_image = screenshot_bytes()
    check(hidden_image != real_image, "real channel hidden state removes its pixels")
    move(view, view.get_width() / 2, view.get_height() / 2)
    yield 250
    check(
        core.pointer_blank() and transparent_pointer(view),
        "webOS intentionally hidden cursor does not restore a host arrow",
    )
    cursor_command("native-show")
    yield 250
    check(screenshot_bytes() == hidden_image, "QEMU cursor cannot draw over hidden webOS cursor")

    # Letterbox bounds are exclusive on the right/bottom; resizing must update
    # pointer policy without requiring another guest cursor packet or motion.
    core.view(960, 720)  # 1920x1080 picture: (0,90), 960x540.
    for x, y, blank in (
        (0, 89, False),
        (0, 90, True),
        (959, 629, True),
        (960, 629, False),
        (959, 630, False),
        (-1, 90, False),
    ):
        core.motion(x, y)
        check(bool(core.pointer_blank()) == blank, f"letterbox pointer geometry {x},{y}: {blank}")
    core.motion(200, 20)
    check(not core.pointer_blank(), "host arrow in top letterbox")
    core.view(960, 540)
    check(core.pointer_blank(), "resize brings stationary pointer inside picture")
    core.view(960, 720)
    check(not core.pointer_blank(), "resize returns stationary pointer to letterbox")
    core.motion(200, 240)
    check(core.pointer_blank(), "picture interior hides pointer with hidden guest cursor")
    core.release()
    check(core.pointer_blank(), "blur releases input without restoring host arrow inside picture")
    core.view(960, 540)
    check(core.pointer_blank(), "resize after blur preserves actual pointer geometry")
    core.motion(200, 20)
    core.view(960, 720)
    check(not core.pointer_blank(), "letterbox restores host arrow after blur")
    core.view(960, 540)
    check(core.pointer_blank(), "stationary pointer returns to picture after blur")
    core.leave()
    check(not core.pointer_blank(), "leave restores normal host pointer")
    core.view(view.get_width(), view.get_height())
    move(view, view.get_width() / 2, view.get_height() / 2)
    yield 250

    cursor_command("legacy-silent")
    yield 300
    check(
        core.pointer_blank() and transparent_pointer(view),
        "silent channel does not restore host pointer",
    )
    cursor_command("native-only")  # Disconnect/unlink real cursor socket.
    yield 300
    draws = core.counter("cursor_draws")
    check(screenshot_bytes() == hidden_image, "disconnected channel has no QEMU cursor fallback")
    check(core.counter("cursor_draws") == draws, "QEMU messages cannot execute a cursor draw")
    check(
        core.pointer_blank() and transparent_pointer(view),
        "missing shape/connection still hides pointer over picture",
    )
    before = ack_count()
    cursor_command("native-invalid")
    for _ in range(20):
        yield 50
        if ack_count() >= before + 4:
            break
    check(
        ack_count() == before + 4 and all(line.endswith(" ack") for line in cursor_acks()),
        "ignored QEMU pixel/hotspot payloads acknowledged without state",
    )
    check(
        screenshot_bytes() == hidden_image and core.counter("cursor_draws") == draws,
        "ignored QEMU cursor data never draws",
    )
    check(
        all(
            core.counter(name) == -1
            for name in (
                "native_cursor_images",
                "native_cursor_defines",
                "native_mouse_sets",
                "native_cursor_draws",
            )
        ),
        "native rendering state/counters removed",
    )
    move(bar, 10, bar.get_height() / 2)
    yield 250
    check(
        view.get_cursor() is None and not core.pointer_blank(), "controls keep normal host pointer"
    )
    check(rwin.get_cursor() is None, "remote keeps normal host pointer")

    # 9. the Mouse toggle (a model with a touch screen): pointer at start; in touch mode the left
    # button's press, drag and release are one finger's begin, moves and end in the guest display's
    # pixels, no pointer button; the right button and hovering stay the pointer's; back to pointer
    mb = win.mouse_button

    def tap_mouse():
        """A click on the Mouse choice, which is in the popover of the arrow beside Remote."""
        win.input_button.popup()
        yield 500
        click(mb)
        yield 300
        win.input_button.popdown()
        yield 300

    check(
        mb.get_visible() and win.input_button.get_visible() and not mb.get_active()
        and not view.mouse_touch and win.remote_button.get_parent() is win.input_button.get_parent(),
        "a touch screen's model has the Mouse choice in the arrow linked to the Remote toggle, "
        "the pointer at start",
    )  # fmt: skip
    yield from tap_mouse()
    check(
        view.mouse_touch and mb.get_active() and mb.get_tooltip_text().startswith("Mouse: touch"),
        f"the Mouse toggle makes the mouse a finger ({mb.get_tooltip_text()!r})",
    )
    _ok, (px, py, pw, ph) = core.picture()
    before = len(log_lines())
    yield from press_drag(view, px + pw * 0.25, py + ph * 0.5, round(pw * 0.25), 0)
    got = [l.split() for l in log_lines()[before:] if l.startswith("MultiTouch.SendEvent")]
    kinds = [g[1] for g in got]
    buttons = [l for l in log_lines()[before:] if l.startswith(("Mouse.Press", "Mouse.Release"))]
    check(
        kinds[:1] == ["0"] and kinds[-1:] == ["2"] and kinds.count("1") >= 5
        and len(set(g[2] for g in got)) == 1 and not buttons
        and abs(float(got[0][3]) - 1920 * 0.25) <= 8 and abs(float(got[0][4]) - 540) <= 8
        and abs(float(got[-1][3]) - 1920 * 0.5) <= 8,
        f"a drag in touch mode is a swipe: begin, {kinds.count('1')} moves, end in slot "
        f"{got[0][2] if got else '?'} from {got[0][3:5] if got else '?'} to "
        f"{got[-1][3:5] if got else '?'}, no pointer button ({buttons})",
    )  # fmt: skip
    # a finger can't hover: moving the mouse with no button held sends nothing
    before = len(log_lines())
    for fx in (0.3, 0.4, 0.6):
        move(view, px + pw * fx, py + ph * 0.4)
        yield 100
    yield 200
    lines = log_lines()[before:]
    check(
        not any(l.startswith(("Mouse.", "MultiTouch.")) for l in lines),
        f"in touch mode hovering sends nothing to the guest ({lines})",
    )
    before = len(log_lines())
    click(view, px + pw * 0.5, py + ph * 0.5, button=3)
    yield 300
    lines = log_lines()[before:]
    at = [l.split() for l in lines if l.startswith("Mouse.SetAbsPosition")]
    check(
        "Mouse.Press 2" in lines and "Mouse.Release 2" in lines and at
        and abs(int(at[0][1]) - 1920 * 0.5) <= 8 and abs(int(at[0][2]) - 1080 * 0.5) <= 8
        and not any(l.startswith("MultiTouch.") for l in lines),
        f"in touch mode the right button is still the pointer's, at the spot clicked "
        f"({at[0][1:3] if at else '?'}, expected 960,540)",
    )  # fmt: skip
    yield from tap_mouse()
    before = len(log_lines())
    click(view, px + pw * 0.5, py + ph * 0.5)
    yield 300
    lines = log_lines()[before:]
    check(
        not view.mouse_touch and not mb.get_active() and "Mouse.Press 0" in lines
        and not any(l.startswith("MultiTouch.") for l in lines),
        "toggled back, a click is the pointer's left button again",
    )  # fmt: skip

    # 9b. More's planes: each check item hides its plane in this window's core and shows it again
    planes_ok = win.more_button.get_visible()
    for name, bit in (("ui", 2), ("video", 1), ("cursor", 4)):
        a = win.lookup_action(f"plane-{name}")
        planes_ok &= a.get_state().get_boolean() and not core.planes() & bit
        a.change_state(GLib.Variant.new_boolean(False))
        planes_ok &= not a.get_state().get_boolean() and core.planes() == bit
        a.change_state(GLib.Variant.new_boolean(True))
        planes_ok &= a.get_state().get_boolean() and core.planes() == 0
    check(planes_ok, "More's UI, Video and Cursor items hide and show their planes in the core")

    # 9c. More's Elements layer: screen recognition (beanjar.display.elements, here a stand-in textshot that
    # prints one button in the middle of the 1280x720 frame) boxed over the picture; off again, the
    # layer is hidden and stops reading; without textshot it stays off and says why
    from gi.repository import GdkPixbuf

    from beanjar_ui.viewer import main as mainlib

    fake = os.path.join(tmp, "textshot")
    element = {"id": 0, "key": "button:OK", "kind": "button", "x0": 400, "y0": 180, "x1": 880,
               "y1": 540, "score": 0.9, "text": "OK", "style": {}}  # fmt: skip
    with open(fake, "w") as f:
        f.write(f"#!/bin/sh\necho '{json.dumps([element])}'\n")
    os.chmod(fake, 0o755)
    os.environ["BEANJAR_TEXTSHOT"] = fake
    os.environ["BEANJAR_TEXTSHOT_SERVE"] = "none"  # the stand-in CLI per frame, no server
    layer = pane.elements
    ea = win.lookup_action("layer-elements")
    items = [c.get_label() for c in descendants(win.more_button.get_popover())
             if isinstance(c, Gtk.CheckButton)]  # fmt: skip
    check(
        "Elements" in items and not ea.get_state().get_boolean() and not layer.get_visible(),
        "More has an Elements item, off at start",
    )
    ea.change_state(GLib.Variant.new_boolean(True))
    for _ in range(100):
        if layer.last():
            break
        yield 50
    yield 200
    shown = layer.last()
    check(
        ea.get_state().get_boolean() and layer.get_visible() and not layer.get_can_target()
        and shown is not None and shown["data"]["elements"][0]["text"] == "OK"
        and shown["data"]["width"] == 1280 and shown["png"].startswith(b"\x89PNG"),
        "Elements on: the layer reads a 1280x720 frame through beanjar.display.elements, takes no input",
    )  # fmt: skip
    grabbed = os.path.join(tmp, "elements.png")
    ok = mainlib.grab(pane, grabbed)
    edge = None
    if ok:
        pix = GdkPixbuf.Pixbuf.new_from_file(grabbed)
        _valid, (px, py, pw, ph) = core.picture()
        x, y = int(px + 400 * pw / 1280), int(py + 360 * ph / 720)  # the box's left edge, middle
        row, n = pix.get_rowstride(), pix.get_n_channels()
        data = pix.get_pixels()
        edge = tuple(data[y * row + x * n : y * row + x * n + 3])
    check(
        edge is not None and edge[0] > 200 and 80 < edge[1] < 160 and edge[2] < 60,
        f"the grab (--grab) has the button's box in the button colour over the picture (got {edge})",
    )
    ea.change_state(GLib.Variant.new_boolean(False))
    yield 100
    check(
        not ea.get_state().get_boolean() and not layer.get_visible()
        and layer.recognizer.tmp is None and not layer._ticks,
        "Elements off: the layer is hidden and stops reading",
    )  # fmt: skip
    os.environ["BEANJAR_TEXTSHOT"] = os.path.join(tmp, "no-textshot")
    said = len(pane.said)
    ea.change_state(GLib.Variant.new_boolean(True))
    check(
        not ea.get_state().get_boolean() and not layer.get_visible()
        and any("not installed" in s for s in pane.said[said:]),
        "Elements without go-textshot stays off and says it is not installed",
    )  # fmt: skip
    del os.environ["BEANJAR_TEXTSHOT"], os.environ["BEANJAR_TEXTSHOT_SERVE"]

    # 10. a set turned to portrait (run/rotation, the guest's setting 90): the picture shown turned
    # 270 clockwise, the window's picture area turned with it, the pointer mapped back to the
    # panel's pixels (the panel's top-left corner at the picture's bottom-left); back at 0
    landscape = (view.get_width(), view.get_height())
    if os.environ.get("BEANJAR_ROTATE_SHOTS"):  # the header of a rotatable touch-screen model
        shots = os.environ["BEANJAR_ROTATE_SHOTS"]
        header_shot("header-touch-rotatable.png")
        shots = None
    rb = win.rotate_button
    check(
        win.more_button.get_visible() and rb.get_label() == "Portrait"
        and rb.get_ancestor(Gtk.Popover) is win.more_button.get_popover(),
        "a rotatable model has Portrait in the header's More menu",
    )  # fmt: skip
    check(
        isinstance(rb, Gtk.ToggleButton) and not rb.get_active()
        and rb.get_tooltip_text() == "Landscape" and pane.rotate_target() == 270,
        f"Rotate at landscape: an untoggled toggle, tooltip {rb.get_tooltip_text()!r}, goes to 270",
    )  # fmt: skip
    with open(os.path.join(tmp, "rotation"), "w") as f:
        f.write("rotation=90\n")
    yield 1500
    check(
        rb.get_active() and rb.get_tooltip_text() == "Portrait" and pane.rotate_target() == 0,
        f"Rotate at 90: toggled, tooltip {rb.get_tooltip_text()!r}, goes to landscape (0)",
    )  # fmt: skip
    _ok, (px, py, pw, ph) = core.picture()
    check(
        view.get_height() > view.get_width() and ph > pw,
        f"rotation 90: portrait picture area {view.get_width()}x{view.get_height()}, picture {pw}x{ph}",
    )
    move(view, px + pw * 0.25, py + ph * 0.1)
    yield 300
    at = [l.split() for l in log_lines() if l.startswith("Mouse.SetAbsPosition")][-1]
    check(
        abs(int(at[1]) - 1920 * 0.9) <= 8 and abs(int(at[2]) - 1080 * 0.25) <= 8,
        f"the pointer on the turned picture is at the panel's {at[1]},{at[2]} (1728,270)",
    )
    # the mouse's finger on the turned picture lands on the panel's pixels too (wv_touch's mapping)
    yield from tap_mouse()
    before = len(log_lines())
    click(view, px + pw * 0.25, py + ph * 0.1)
    yield 300
    got = [l.split() for l in log_lines()[before:] if l.startswith("MultiTouch.SendEvent")]
    check(
        [g[1] for g in got] == ["0", "2"]
        and abs(float(got[0][3]) - 1920 * 0.9) <= 8 and abs(float(got[0][4]) - 1080 * 0.25) <= 8,
        f"a tap in touch mode on the turned picture is at the panel's {got[0][3:5] if got else '?'} "
        "(1728,270)",
    )  # fmt: skip
    yield from tap_mouse()
    with open(os.path.join(tmp, "rotation"), "a") as f:
        f.write("rotation=0\n")
    yield 1500
    check(
        (view.get_width(), view.get_height()) == landscape,
        f"rotation 0: the picture area back to {landscape} ({view.get_width()}x{view.get_height()})",
    )
    for angle, active, target in ((270, True, 0), (180, True, 0), (0, False, 270)):
        with open(os.path.join(tmp, "rotation"), "a") as f:
            f.write(f"rotation={angle}\n")
        yield 1200
        check(
            rb.get_active() == active and pane.rotate_target() == target
            and rb.get_tooltip_text() == ("Portrait" if active else "Landscape"),
            f"Rotate at {angle}: toggled {rb.get_active()}, goes to {pane.rotate_target()} ({target})",
        )  # fmt: skip
    win.close()


def descendants(widget):
    out = []
    for c in children(widget):
        out += [c] + descendants(c)
    return out


def children(widget):
    out, c = [], widget.get_first_child()
    while c is not None:
        out.append(c)
        c = c.get_next_sibling()
    return out


def activate(app):
    pane = ViewerPane(
        core,
        tmp,
        remote=True,
        statusbar=True,
        local_key="onekey",
        touch=True,
        portrait=True,
        vm_name="tv1",
    )
    win = ViewerWindow(
        app, pane, (960, 540), title="tv1", subtitle="FW-NAME", window_title="tv1 - Beanjar Viewer"
    )
    win.present()
    it = steps(win, pane)

    def run():
        try:
            delay = next(it)
        except StopIteration:
            return False
        except Exception as e:  # a broken step fails the test rather than hanging it
            import traceback

            traceback.print_exc()
            failures.append(f"exception {e}")
            win.close()
            return False
        GLib.timeout_add(delay, run)
        return False

    GLib.timeout_add(100, run)
    GLib.timeout_add(90000, lambda: (failures.append("timeout"), win.close(), False)[2])
    state["pane"] = pane


app.connect("activate", activate)
app.run([])
# Firmware-aware menus and header input: construct the actual widgets, without
# mapping another GLArea or connecting a second display client.
from beanjar_ui.viewer.main import remote_layout  # noqa: E402

from beanjar import paths  # noqa: E402


class RecordingCore:
    def __init__(self):
        self.events = []

    def ir(self, code, down):
        self.events.append((code, down))

    def local_key(self, code, down):
        self.events.append(("local", code, down))

    def release(self):
        pass

    def planes(self, hidden=-1):
        return 0


# the set's own key: a joystick model's five ways in a popover, each its micom code; none without
recorder = RecordingCore()
p = ViewerPane(recorder, None, statusbar=False, local_key="joystick")
w = ViewerWindow(None, p, (320, 180))
check(
    w.local_key_group.get_visible()
    and w.joystick_button.get_visible()
    and not w.local_key_button.get_visible(),
    "a joystick model shows the joystick's popover button, not the one key",
)
for line, want, label in MICOM_LED_CASES:
    p.stats = types.SimpleNamespace(micom=statslib.parse_micom(line))
    p.update_key_leds()
    check_key_led(w.joystick_button, want, label)
p.stats = None
for button in (w.joystick_up, w.joystick_down, w.joystick_left, w.joystick_right, w.joystick_press):
    controllers = button.observe_controllers()
    for i in range(controllers.get_n_items()):
        gesture = controllers.get_item(i)
        if (
            isinstance(gesture, Gtk.GestureClick)
            and gesture.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        ):
            gesture.emit("pressed", 1, 0.0, 0.0)
            gesture.emit("released", 1, 0.0, 0.0)
check(
    recorder.events
    == [("local", c, d) for c in (0x00, 0x01, 0x03, 0x02, 0xEA) for d in (True, False)],
    f"the joystick's up, down, left, right and press are its micom codes ({recorder.events})",
)
w.destroy()
p = ViewerPane(RecordingCore(), None, statusbar=False)
w = ViewerWindow(None, p, (320, 180))
check(
    not w.local_key_group.get_visible() and not p.key_leds and not hasattr(w, "front_led"),
    "a model without a local key shows no TV key and has no LED control",
)
w.set_mouse_touch(True)
check(
    not w.mouse_button.get_visible()
    and not w.input_button.get_visible()
    and not w.rotate_button.get_visible()
    and not p.view.mouse_touch,
    "a model without a touch screen shows no Mouse toggle, and the mouse stays the pointer",
)
w.destroy()

for target, expected in (
    (("h13", "afro"), remotelib.NORMAL),
    (("lm14", "netcast45"), remotelib.NETCAST),
    (("m2", "simplesmart"), remotelib.SIMPLESMART),
):
    info = paths.vm_info(target, "", "")
    chosen = remote_layout("auto", info)
    check(chosen == expected.name, f"{info['platform']}: firmware selects {expected.title}")
    recorder = RecordingCore()
    p = ViewerPane(recorder, None, statusbar=False, remote_layout=chosen, firmware_layout=chosen)
    w = ViewerWindow(None, p, (320, 180))
    r = RemoteWindow(p)
    choices = r.menu_model.get_item_link(0, "section")
    offered = [
        choices.get_item_attribute_value(i, "target", None).get_string()
        for i in range(choices.get_n_items())
    ]
    check(
        offered == [expected.name, "service"],
        f"{info['platform']}: compatible remote menu {offered}",
    )
    p.set_remote_layout("service")
    check(p.system_layout is expected, f"{info['platform']}: service remote preserves system keys")
    for button in (w.back_button, w.home_button, w.settings_button):
        controllers = button.observe_controllers()
        for i in range(controllers.get_n_items()):
            gesture = controllers.get_item(i)
            if (
                isinstance(gesture, Gtk.GestureClick)
                and gesture.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
            ):
                gesture.emit("pressed", 1, 0.0, 0.0)
                gesture.emit("released", 1, 0.0, 0.0)
    expected_events = [
        (expected.keys[remotelib.find(name, expected)].code, down)
        for name in ("BACK", "HOME", "MENU")
        for down in (True, False)
    ]
    check(
        recorder.events == expected_events,
        f"{info['platform']}: header sends firmware keys {recorder.events}",
    )
    if expected is remotelib.NETCAST:
        check(
            w.home_button.get_icon_name() == "view-grid-symbolic"
            and w.home_button.get_tooltip_text() == "NetCast (Super)",
            "NetCast: dashboard header control",
        )
    check(
        w.settings_button.get_tooltip_text()
        == ("Settings" if expected is remotelib.NORMAL else "Menu"),
        f"{info['platform']}: firmware menu label",
    )
    incompatible = "netcast" if chosen == "normal" else "normal"
    try:
        remote_layout(incompatible, info)
    except SystemExit:
        check(True, f"{info['platform']}: rejects incompatible CLI layout")
    else:
        check(False, f"{info['platform']}: accepted incompatible CLI layout")
    # A direct programmatic request must also obey the same menu restriction.
    try:
        p.set_remote_layout(incompatible)
    except ValueError:
        check(True, f"{info['platform']}: rejects incompatible layout switch")
    else:
        check(False, f"{info['platform']}: accepted incompatible layout switch")
    r.destroy()
    w.destroy()

fake.terminate()
fake.wait()
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"FAIL: {len(failures)} check(s)")
    sys.exit(1)
print(
    "PASS: GTK viewer layout, chrome clicks, pointer, keys, the remote's window, guest-only screenshot"
)
