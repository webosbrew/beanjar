# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""ctypes binding of libbeanjar-viewer-core.so (host/viewer/core/core.h).

Signatures and ABI come from the generated beanjar_ui/viewer/_core_abi.py
(host/viewer/tools/gen-ctypes.py); this module keeps the object lifetime and the
Python-side call shapes."""

import ctypes
import os

from beanjar_ui.viewer._core_abi import _CB, _I, _SIGNATURES, ABI

LIBRARY = "libbeanjar-viewer-core.so"


def library_path():
    """BEANJAR_VIEWER_CORE, else the library published next to beanjar_ui (build/viewer), else the
    core's tree's (beanjar.paths: build/viewer of the checkout, /usr/lib/beanjar/viewer installed)."""
    env = os.environ.get("BEANJAR_VIEWER_CORE")
    if env:
        return env
    beside = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), LIBRARY
    )
    if os.path.isfile(beside):
        return beside
    from beanjar import paths

    return paths.TREE.build("viewer", LIBRARY)


# wv_touch's kinds (input.h's INPUT_TOUCH_*, QEMU's) and its slots
TOUCH_BEGIN, TOUCH_UPDATE, TOUCH_END, TOUCH_CANCEL = range(4)
TOUCH_SLOTS = 10

_lib = None


def lib():
    global _lib
    if _lib is None:
        path = library_path()
        l = ctypes.CDLL(path)
        for name, (res, args) in _SIGNATURES.items():
            f = getattr(l, name)
            f.restype = res
            f.argtypes = args
        if l.wv_abi() != ABI:
            raise RuntimeError(f"{path}: core ABI {l.wv_abi()}, this package expects {ABI}")
        _lib = l
    return _lib


def install_signals():
    lib().wv_signals()


class Core:
    """One viewer core. Callbacks run on the GLib main loop (the default context). After
    finish() every call is a no-op returning None (GTK may still render or deliver events while
    the window closes)."""

    def __init__(self):
        self._l = lib()
        self._v = self._l.wv_new()
        self._callbacks = None

    def _call(self, name, *args):
        return getattr(self._l, name)(self._v, *args) if self._v is not None else None

    def option(self, name, value=None):
        if self._call("wv_option", name.encode(), None if value is None else str(value).encode()):
            raise ValueError(f"invalid viewer option {name} {value!r}")

    def callbacks(self, make_current, redraw, quit):
        # keep the ctypes thunks alive as long as the core
        self._callbacks = (
            _CB(lambda _: make_current()),
            _CB(lambda _: redraw()),
            _CB(lambda _: quit()),
        )
        self._call("wv_callbacks", *self._callbacks, None)

    def start(self):
        self._call("wv_start")

    def attach(self):
        self._call("wv_attach")

    def view(self, width, height):
        self._call("wv_view", int(width), int(height))

    def render(self, width, height):
        self._call("wv_render", int(width), int(height))

    def turn(self, degrees):
        """Shows the picture turned clockwise by degrees (0, 90, 180, 270): a set turned to
        portrait (wv_turn); input coordinates are mapped back."""
        self._call("wv_turn", int(degrees))

    def picture(self):
        x, y, w, h = _I(), _I(), _I(), _I()
        valid = self._call(
            "wv_picture", ctypes.byref(x), ctypes.byref(y), ctypes.byref(w), ctypes.byref(h)
        )
        return bool(valid), (x.value, y.value, w.value, h.value)

    def screenshot(self, path, width, height):
        return self._call("wv_screenshot", str(path).encode(), int(width), int(height)) == 0

    def key_evdev(self, code, down):
        return bool(self._call("wv_key_evdev", code, 1 if down else 0))

    def ir(self, code, down):
        """A TV remote button (LG's IR data code) down or up, to the guest's micom."""
        self._call("wv_ir", code, 1 if down else 0)

    def local_key(self, code, down):
        """The set's own key (its micom code, include/webos-ir.h WEBOS_LOCAL_*) down or up."""
        self._call("wv_local_key", code, 1 if down else 0)

    def motion(self, x, y):
        self._call("wv_motion", int(x), int(y))

    def leave(self):
        self._call("wv_leave")

    def button(self, button, down, x, y):
        self._call("wv_button", button, 1 if down else 0, int(x), int(y))

    def wheel(self, steps):
        self._call("wv_wheel", int(steps))

    def touch(self, kind, slot, x, y):
        """kind TOUCH_BEGIN, TOUCH_UPDATE, TOUCH_END or TOUCH_CANCEL; slot 0 to TOUCH_SLOTS - 1."""
        self._call("wv_touch", int(kind), int(slot), int(x), int(y))

    def release(self):
        self._call("wv_release")

    def pointer_blank(self):
        return bool(self._call("wv_pointer_blank"))

    def pointer_blank_set(self):
        self._call("wv_pointer_blank_set")

    def planes(self, hidden=-1):
        """The planes this viewer hides (WV_PLANE_* bits: 1 video, 2 UI, 4 cursor), set to hidden
        unless it is negative (wv_planes); returns the planes hidden now."""
        r = self._call("wv_planes", int(hidden))
        return 0 if r is None else r

    def standby(self, standby):
        self._call("wv_standby", 1 if standby else 0)

    def counter(self, name):
        return self._call("wv_counter", name.encode())

    def finish(self, extra=""):
        """Stops everything and prints the counters; True if the run failed."""
        v, self._v = self._v, None
        return bool(self._l.wv_finish(v, extra.encode())) if v is not None else False
