# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's picture and remote on stdio, for front ends that are not on this machine's display: the
web viewer (docs/dev/architecture/web-viewer.md) runs these as `beanjar vnc pipe [HOST/]NAME` and
`beanjar remote pipe [HOST/]NAME`, so a VM on another host is reached over the manager's own ssh
and the web server never opens a VM's socket itself.

- vnc_pipe: stdin and stdout relayed to beanjar-vnc's local RFB socket (run/rfb-local.sock, owner
  only, no RFB authentication: the file's permissions are the access control).
- audio_pipe: the same for beanjar-vnc's audio socket (run/audio-local.sock, owner only): the
  client's request line ("opus" or "pcm") goes in, the header and the stream come out
  (host/vnc/audio.h; docs/dev/architecture/vnc.md, "Audio").
- video_pipe: the same for beanjar-vnc's video socket (run/video-local.sock, owner only): the
  request line ("h264", optionally "bitrate=N" in kbit/s) goes in, the header and the H.264 units
  come out (host/vnc/video.h; docs/dev/architecture/vnc.md, "Video").
- remote_pipe: lines on stdin, "down CODE" and "up CODE", as org.beanjar.Display1.Ir calls on the
  VM's bus (the front end that owns the display, beanjar-vnc or the GTK viewer, serves it). CODE is
  an LG IR data code (0x7c, 124), a Japanese remote's extended code (0xdb30, include/webos-ir.h
  WEBOS_IR_EXT) or a key's name in the VM's layout (HOME). "touch KIND SLOT X Y
  [WxH]" (KIND begin, move, end or cancel; X Y in the guest display's pixels, or with WxH in a
  frame of that size, the picture letterboxed into it) is a Display1.Touch call, for a VM whose
  model has a touch screen (the web viewer's touch events, in noVNC's framebuffer). "pointer X Y
  BUTTONS WxH" is a Display1.Pointer call at X Y of a WxH frame (the web viewer's pointer while its
  picture is turned). For a model that turns to portrait it says "rotation N" at its start and at
  each change of the guest's rotation (run/rotation, rotation.py). It answers "ready" once
  connected, "error: ..." for a line it cannot do; at the end of stdin it releases what it holds
  and ends the touches that are down.
- In standby (Quick Start+) the keys go the same way: the micom's always-on part on the host
  (ir_relay.py, behind the front end's ir.sock) wakes the guest on POWER and drops the other keys
  (docs/dev/architecture/suspend.md).
- layout: the VM's remote (beanjar.devices.remote_layouts, the GTK remote's data) as JSON.
"""

import os
import re
import selectors
import socket
import sys
import threading
import time

from ..base import Error
from . import dbus, framing
from .dbus import SCREENSHOT_NAME, SCREENSHOT_PATH

CHUNK = 1 << 16
PRESS_HOLD = 0.1  # seconds a `remote press` holds each key: a short press


def vm_layout(vm):
    """The layout the GTK viewer would show for vm: its firmware's remote, the Japanese one on an
    ARIB board (tuner.vm_broadcast, as the launch writes it to vm-info.toml)."""
    from ..devices import remote_layouts as lib
    from ..firmware import targets
    from ..tuner import antenna

    try:
        target = vm.target()
        info = {"platform": targets.platform(target), "release": target[1]}
    except Error:
        info = {}
    else:
        broadcast = antenna.vm_broadcast(vm)
        if broadcast:
            info["broadcast"] = broadcast
    return lib.LAYOUTS[lib.remote_layout("auto", info)]


def _running(vm):
    if vm.pid() is None:
        raise Error(f"VM {vm.name} is not running")


def _relay(vm, path, what, stdin, stdout):
    """Relays the fds stdin and stdout to the beanjar-vnc socket at path until either side ends."""
    _running(vm)
    pid = vm.vnc_pid()
    if not pid:
        raise Error(f"beanjar-vnc is not serving {vm.name}: beanjar vnc start {vm.name}")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
    except OSError as e:
        s.close()
        raise Error(
            f"beanjar-vnc (pid {pid}) of {vm.name} has no {what} socket ({e.strerror}): it "
            f"predates it; beanjar vnc stop {vm.name}; beanjar vnc start {vm.name}"
        ) from None
    sel = selectors.DefaultSelector()
    sel.register(stdin, selectors.EVENT_READ, "in")
    sel.register(s, selectors.EVENT_READ, "sock")
    try:
        while True:
            for key, _ in sel.select():
                if key.data == "in":
                    data = os.read(stdin, CHUNK)
                    if not data:
                        return 0
                    s.sendall(data)
                else:
                    data = s.recv(CHUNK)
                    if not data:
                        return 0
                    view = memoryview(data)
                    while view:
                        view = view[os.write(stdout, view) :]
    except (BrokenPipeError, ConnectionResetError):
        return 0
    finally:
        sel.close()
        s.close()


def vnc_pipe(vm, stdin=0, stdout=1):
    """stdin and stdout relayed to beanjar-vnc's local RFB socket until either side ends."""
    return _relay(vm, vm.vnc_local_sock, "local", stdin, stdout)


def audio_pipe(vm, stdin=0, stdout=1):
    """stdin and stdout relayed to beanjar-vnc's audio socket until either side ends: the request
    line in, the header and the stream out."""
    return _relay(vm, vm.audio_local_sock, "audio", stdin, stdout)


def video_pipe(vm, stdin=0, stdout=1):
    """stdin and stdout relayed to beanjar-vnc's video socket until either side ends: the request
    line in, the header and the H.264 stream out."""
    return _relay(vm, vm.video_local_sock, "video", stdin, stdout)


class FrontEnd:
    """The VM's org.beanjar.Display1, served by the front end that owns its display."""

    def __init__(self, vm, timeout=5.0):
        dbus.gio("the remote")
        self.ms = int(timeout * 1000)
        self.vm = vm
        _running(vm)
        self.conn = dbus.Bus(vm.dbus_sock)
        (owned,) = self._call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            "(s)",
            (SCREENSHOT_NAME,),
            "(b)",
        )
        if not owned:
            self.conn.close()
            raise Error(
                f"no front end owns {vm.name}'s display: beanjar vnc start {vm.name} "
                "(or open the viewer)"
            )

    def _call(self, dest, path, iface, method, signature, values, reply=None):
        """iface.method of dest with values (signature: their GVariant type), dbus.Bus.call's."""
        try:
            return self.conn.call(dest, path, iface, method, signature, values, reply, self.ms)
        except dbus.CallError as e:
            unknown = e.remote in (
                "org.freedesktop.DBus.Error.UnknownMethod",
                "org.freedesktop.DBus.Error.UnknownObject",
            )
            if unknown and dest == SCREENSHOT_NAME:
                raise Error(
                    f"the display's front end of {self.vm.name} has no {iface}.{method}: it "
                    "predates it; update beanjar on that host, then restart beanjar-vnc "
                    "(or reopen the viewer)"
                ) from None
            raise Error(f"the display's front end: {e.message}") from None


class Remote(FrontEnd):
    """The VM's org.beanjar.Display1 for IR button edges."""

    def __init__(self, vm, timeout=5.0):
        super().__init__(vm, timeout)
        self.layout = vm_layout(vm)
        self.held = None

    def code(self, word):
        """A key's IR code: a number (0x7c, 124; a Japanese remote's extended code 0xdb30) or a name of
        the VM's layout (HOME)."""
        from ..devices import remote_layouts

        try:
            code = int(word, 0)
        except ValueError:
            keys = {k.name.upper(): k.code for k in self.layout.keys}
            if word.upper() not in keys:
                raise Error(f"no key {word} on the {self.layout.name} remote") from None
            code = keys[word.upper()]
            if code is None:
                raise Error(f"the key {word} sends nothing on the {self.layout.name} remote")
        if not remote_layouts.code_valid(code):
            raise Error(f"IR code {word} is not 0 to 255 or 0xdb00 to 0xdbff")
        return code

    def edge(self, code, down):
        self._call(
            SCREENSHOT_NAME,
            SCREENSHOT_PATH,
            SCREENSHOT_NAME,
            "Ir",
            "(ub)",
            (code, bool(down)),
        )
        self.held = code if down else None if self.held == code else self.held

    def release(self):
        if self.held is not None:
            self.edge(self.held, False)

    def close(self):
        try:
            self.release()
        finally:
            self.conn.close()


def remote_pipe(vm, stdin=None, stdout=None):
    """remote pipe's loop: one edge a line until the end of stdin, then what is held released."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout

    def say(text):
        stdout.write(text + "\n")
        stdout.flush()

    remote = Remote(vm)
    touch = None
    pointer_ = None
    lock = threading.Lock()
    said = say

    def say(text):  # noqa: F811 (the rotation watcher's thread says too)
        with lock:
            said(text)

    watching = threading.Event()
    if rotation_reported(vm):
        threading.Thread(target=watch_rotation, args=(vm, say, watching), daemon=True).start()
    try:
        say("ready")
        for raw in stdin:
            words = raw.decode(errors="replace").split()
            if not words:
                continue
            try:
                if words[0] == "touch":
                    frame = (
                        re.fullmatch(r"([0-9]{1,5})x([0-9]{1,5})", words[5])
                        if len(words) == 6
                        else None
                    )
                    if (
                        len(words) not in (5, 6)
                        or not all(w.isdigit() for w in words[2:5])
                        or (len(words) == 6 and not (frame and all(int(v) for v in frame.groups())))
                    ):
                        raise Error(f"not 'touch KIND SLOT X Y [WxH]': {' '.join(words)}")
                    if touch is None:
                        if not has_touch(vm):
                            raise Error(f"{vm.name}'s model has no touch screen")
                        touch = Touch(vm, size=False)
                    kind, slot, x, y = words[1], *(int(w) for w in words[2:5])
                    if frame:
                        touch.send_frame(kind, slot, x, y, tuple(int(v) for v in frame.groups()))
                    else:
                        touch.send(kind, slot, x, y)
                    continue
                if words[0] == "pointer":
                    frame = (
                        re.fullmatch(r"([0-9]{1,5})x([0-9]{1,5})", words[4])
                        if len(words) == 5
                        else None
                    )
                    if (
                        not frame
                        or not all(w.isdigit() for w in words[1:4])
                        or not all(int(v) for v in frame.groups())
                        or int(words[3]) > 7
                    ):
                        raise Error(f"not 'pointer X Y BUTTONS WxH': {' '.join(words)}")
                    if pointer_ is None:
                        pointer_ = Pointer(vm)
                    size = tuple(int(v) for v in frame.groups())
                    bx, by, bw, bh = framing.picture_box(size, pointer_.source)
                    x = min(max(int(words[1]), bx), bx + bw - 1)
                    y = min(max(int(words[2]), by), by + bh - 1)
                    pointer_.send(
                        *framing.frame_to_source(x, y, size, pointer_.source), int(words[3])
                    )
                    continue
                if len(words) != 2 or words[0] not in ("down", "up"):
                    raise Error(f"not 'down CODE' or 'up CODE': {' '.join(words)}")
                remote.edge(remote.code(words[1]), words[0] == "down")
            except Error as e:
                if str(e).startswith("the display's front end"):
                    raise
                say(f"error: {e}")
    finally:
        watching.set()
        try:
            if touch is not None:
                touch.close()
            if pointer_ is not None:
                pointer_.close()
        finally:
            remote.close()
    return 0


ROTATION_POLL = 0.5  # seconds between remote pipe's looks at run/rotation


def rotation_reported(vm):
    """Whether remote pipe reports vm's rotation: a model that turns to portrait."""
    return bool(getattr(vm, "portrait", lambda: False)())


def watch_rotation(vm, say, stop):
    """remote pipe's "rotation N" lines: the guest's rotation setting as it reports it (run/rotation,
    rotation.py; 0 before its first report), once at the start and at each change, until stop."""
    from ..devices import rotation

    last = None
    while True:
        angle = rotation.current(vm.run_dir) or 0
        if angle != last:
            last = angle
            try:
                say(f"rotation {angle}")
            except (OSError, ValueError):
                return
        if stop.wait(ROTATION_POLL):
            return


def press(vm, keys, hold=PRESS_HOLD):
    """Each key pressed in turn: down, hold seconds, up."""
    remote = Remote(vm)
    try:
        codes = [remote.code(k) for k in keys]
        for code in codes:
            remote.edge(code, True)
            time.sleep(hold)
            remote.edge(code, False)
    finally:
        remote.close()
    return codes


# Pointer input (beanjar remote click/move): the tablet through the front end's
# org.beanjar.Display1.Pointer, in the guest display's pixels, from a point of a screenshot's frame
# (default 1280x720, the picture letterboxed into it). See host/core/README.md, "Pointer".
POINTER_BUTTONS = {"left": 1, "middle": 2, "right": 4}  # Display1.Pointer's mask bits
POINTER_FRAME = (1280, 720)  # screenshot's and elements' default frame
POINTER_WAKE = 4  # display pixels of the wake movement beside the target
POINTER_SETTLE = 0.3  # seconds between the arrival and the press: the cursor shown, hover taken
POINTER_HOLD = 0.1  # seconds a click holds the button


def wake_point(sx, sy, source, step=POINTER_WAKE):
    """A display pixel step beside (sx, sy), inside the display: the wake movement's start, a
    position that differs from the target so the guest sees motion (evdev drops a repeated absolute
    position: a move to where the pointer already is reaches the guest as nothing, so LG's cursor
    would not show; a click needs no wake movement, the bridge sends cursor-on and the position with
    a press)."""
    wx = sx + step if sx + step < source[0] else sx - step
    return max(wx, 0), sy


class Pointer(FrontEnd):
    """The tablet through the front end: moves and button masks in the guest display's pixels."""

    def __init__(self, vm, timeout=5.0):
        super().__init__(vm, timeout)
        self.buttons = 0
        try:
            self.source = dbus.display_size(self.conn, self.ms)
        except Error:
            self.conn.close()
            raise

    def send(self, sx, sy, buttons):
        self._call(
            SCREENSHOT_NAME,
            SCREENSHOT_PATH,
            SCREENSHOT_NAME,
            "Pointer",
            "(uuu)",
            (sx, sy, buttons),
        )
        self.buttons = buttons
        self.at = (sx, sy)

    def close(self):
        try:
            if self.buttons:
                self.send(*self.at, 0)
        finally:
            self.conn.close()


def pointer(
    vm, action, x, y, frame=POINTER_FRAME, button="left", settle=POINTER_SETTLE, hold=POINTER_HOLD
):
    """action "move" or "click" at x, y of a frame of frame = (width, height) (a screenshot's,
    the TV picture letterboxed into it): a wake movement from beside the target, the target, then
    for a click settle seconds, the button down for hold seconds and up. Returns the mapping."""
    if action not in ("move", "click"):
        raise Error(f"no pointer action {action}")
    if button not in POINTER_BUTTONS:
        raise Error(f"no button {button}: left, middle or right")
    p = Pointer(vm)
    try:
        sx, sy = framing.frame_to_source(x, y, frame, p.source)
        p.send(*wake_point(sx, sy, p.source), 0)
        p.send(sx, sy, 0)
        if action == "click":
            time.sleep(settle)
            p.send(sx, sy, POINTER_BUTTONS[button])
            time.sleep(hold)
            p.send(sx, sy, 0)
    finally:
        p.close()
    return {
        "action": action,
        "x": x,
        "y": y,
        "frame_width": frame[0],
        "frame_height": frame[1],
        "button": button if action == "click" else None,
        "display_x": sx,
        "display_y": sy,
    } | framing.frame_geometry(frame, p.source)


# Touch input (beanjar remote tap/swipe, remote pipe's touch lines): the guest's touch screen, for a
# VM whose model has one (targets.Soc.touch), through the front end's org.beanjar.Display1.Touch, in
# the guest display's pixels. See host/core/README.md, "Touch".
TOUCH_KINDS = {"begin": 0, "move": 1, "end": 2, "cancel": 3}  # Display1.Touch's (QEMU's) kinds
TOUCH_SLOTS = 10  # QEMU's and LG libim's
TOUCH_HOLD = 0.1  # seconds a tap holds the contact
SWIPE_TIME = 0.3  # seconds a swipe takes
SWIPE_STEPS = 10  # moves along it


def has_touch(vm):
    """Whether the VM's model has a touch screen (targets.Soc.touch of its firmware's target)."""
    return vm.touch()


class Touch(FrontEnd):
    """The touch screen through the front end: contacts of slots in the guest display's pixels."""

    def __init__(self, vm, timeout=5.0, size=True):
        super().__init__(vm, timeout)
        self.down = {}  # slot -> (x, y) where it is
        self.source = None
        if size:
            try:
                self.source = dbus.display_size(self.conn, self.ms)
            except Error:
                self.conn.close()
                raise

    def send(self, kind, slot, sx, sy):
        if kind not in TOUCH_KINDS:
            raise Error(f"no touch kind {kind}: begin, move, end or cancel")
        if not 0 <= slot < TOUCH_SLOTS:
            raise Error(f"touch slot {slot} is not 0 to {TOUCH_SLOTS - 1}")
        self._call(
            SCREENSHOT_NAME,
            SCREENSHOT_PATH,
            SCREENSHOT_NAME,
            "Touch",
            "(uuuu)",
            (TOUCH_KINDS[kind], slot, sx, sy),
        )
        if kind in ("end", "cancel"):
            self.down.pop(slot, None)
        else:
            self.down[slot] = (sx, sy)

    def send_frame(self, kind, slot, x, y, frame):
        """send at x, y of a frame of frame = (width, height) (a viewer's picture, the TV picture
        letterboxed into it): a begin outside the picture is an Error, a move or end outside it
        is clamped to its edge, as the viewer's own touches are."""
        if self.source is None:
            self.source = dbus.display_size(self.conn, self.ms)
        if kind != "begin":
            bx, by, bw, bh = framing.picture_box(frame, self.source)
            x = min(max(x, bx), bx + bw - 1)
            y = min(max(y, by), by + bh - 1)
        self.send(kind, slot, *framing.frame_to_source(x, y, frame, self.source))

    def release(self):
        for slot, (sx, sy) in list(self.down.items()):
            self.send("end", slot, sx, sy)

    def close(self):
        try:
            self.release()
        finally:
            self.conn.close()


def touch(vm, points, frame=POINTER_FRAME, hold=TOUCH_HOLD, duration=SWIPE_TIME, steps=SWIPE_STEPS):
    """One finger on the touch screen at points of a frame (a screenshot's, the TV picture
    letterboxed into it): one point, a tap held hold seconds; two, a swipe from the first to the
    second in steps moves over duration seconds. Returns the mapping."""
    if not has_touch(vm):
        raise Error(f"{vm.name}'s model has no touch screen")
    t = Touch(vm)
    try:
        src = [framing.frame_to_source(x, y, frame, t.source) for x, y in points]
        t.send("begin", 0, *src[0])
        if len(src) == 1:
            time.sleep(hold)
        else:
            (x0, y0), (x1, y1) = src
            for i in range(1, steps + 1):
                time.sleep(duration / steps)
                t.send("move", 0, x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)
        t.send("end", 0, *t.down[0])
    finally:
        t.close()
    return {
        "action": "tap" if len(points) == 1 else "swipe",
        "points": [list(p) for p in points],
        "frame_width": frame[0],
        "frame_height": frame[1],
        "display_points": [list(p) for p in src],
    } | framing.frame_geometry(frame, t.source)
