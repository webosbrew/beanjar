#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A stand-in for QEMU's D-Bus display, for viewer tests without a VM.

    fake-qemu.py DIR [--size WxH] [--bars] [--audio] [--animate [--2d] [--full | --bar]]
                 [--dmabuf [--rate N] [--buffers K]] [--reuse-bus]

Starts a private dbus-daemon at DIR/dbus.sock, owns org.qemu with /org/qemu/Display1/Console_0
(Console.RegisterListener, Keyboard and an absolute Mouse), sends each registered listener one
2D Scanout of a known picture (a8r8g8b8), and serves DIR/cursor.sock with a visible LG cursor
(repeated every second, as libdile_gal's heartbeat), and DIR/ir.sock (the webos.ir port's socket).
Every Keyboard/Mouse call is appended to DIR/input.log as "Interface.Method args", every IR
record as "IR.Down 0x44" or "IR.Up 0x44" (a local key's "Local.Down 0xea"). DIR/ready appears once the bus name is owned. SIGTERM
or SIGINT stops it and its dbus-daemon. The file DIR/display-control sends a Disable (display_control). With --reuse-bus it uses the bus already at DIR/dbus.sock and
leaves it running, as a VM's unit keeps its bus across the QEMUs of a guest restart.

The picture: opaque RGB (32, 128, 192) with a white 1/4-size rectangle in the middle. With --bars,
instead, BARS in that order: vertical bars of equal width (the last one takes the rest), for the
colour checks of beanjar-vnc's video (tests/integration/vnc/check-video.py).

With --animate, while the file DIR/animate exists, the picture is in a shared memfd given to each
listener once (ScanoutMap, as QEMU's Unix.Map), and every 33 ms its top quarter is new random grey
noise (UpdateMap): a busy screen for the video encoder's and the idle tests. With --full the
whole picture is new noise every 33 ms (as video playback changes every pixel). With --bar a band
of BAR rows instead moves down the picture, BAR_STEP rows a time, in a grey that changes each time,
and when DIR/animate goes the picture is restored as it was, only the band's rows updated: the
partial updates of beanjar-vnc's damage test (tests/integration/vnc/check-damage.py). With --2d as well, the
band goes in 2D Update messages, the pixels in the message, as QEMU sends them to a listener that
cannot share memory (no ScanoutMap).

With --dmabuf the picture is a GL guest's instead, as QEMU hands it over with virgl: K (--buffers,
default 3) linear XRGB8888 GBM buffers on the first render node, each filled once with the
picture and a bar at a different place, flipped in turn at N frames a second (--rate, default 60)
while the file DIR/animate exists. Each frame is a ScanoutDMABUF of the next buffer (QEMU awaits
no answer) and an UpdateDMABUF of the whole picture, whose answer is awaited before the next
frame, as QEMU holds the guest's GL until it (graphic_hw_gl_block): a frame due meanwhile is late
and goes when the answer comes. The scanout says y0_top false, as virgl's do, with memory row 0
the top of the picture. DIR/dmabuf.stats has
"frames=N late=N waited_us=N t_us=N" (the monotonic clock), rewritten after every answer.
Without a render node or libgbm it exits 77.

With --audio it also exports /org/qemu/Display1/Audio, as QEMU's -audiodev dbus: a listener given
to RegisterOutListener gets Init (44.1 kHz s16 LE stereo, QEMU's fixed audiodev), SetEnabled and,
on QEMU's timing (as many frames as are due, every 10 ms), Writes of a continuous 1 kHz sine at
-12 dBFS (TONE_HZ, TONE_LEVEL), the same in both channels.
"""

import math
import os
import signal
import socket
import struct
import subprocess
import sys
import time

from gi.repository import Gio, GLib

CONSOLE = "/org/qemu/Display1/Console_0"
XML = """<node>
<interface name='org.qemu.Display1.Console'>
  <method name='RegisterListener'><arg type='h' direction='in'/></method>
  <property name='Width' type='u' access='read'/>
  <property name='Height' type='u' access='read'/>
</interface>
<interface name='org.qemu.Display1.Keyboard'>
  <method name='Press'><arg type='u' direction='in'/></method>
  <method name='Release'><arg type='u' direction='in'/></method>
</interface>
<interface name='org.qemu.Display1.Mouse'>
  <method name='Press'><arg type='u' direction='in'/></method>
  <method name='Release'><arg type='u' direction='in'/></method>
  <method name='SetAbsPosition'><arg type='u' direction='in'/><arg type='u' direction='in'/></method>
  <property name='IsAbsolute' type='b' access='read'/>
</interface>
<interface name='org.qemu.Display1.MultiTouch'>
  <method name='SendEvent'><arg type='u' direction='in'/><arg type='t' direction='in'/>
    <arg type='d' direction='in'/><arg type='d' direction='in'/></method>
</interface>
</node>"""
AUDIO_XML = """<node>
<interface name='org.qemu.Display1.Audio'>
  <method name='RegisterOutListener'><arg type='h' direction='in'/></method>
</interface>
</node>"""
AUDIO_RATE = 44100
TONE_HZ = 1000
TONE_LEVEL = 0.25  # -12 dBFS
BACKGROUND = (32, 128, 192)
A8R8G8B8 = 0x20028888
BAR, BAR_STEP = 20, 7  # --bar's band: its rows, and how far it moves each time


BARS = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (255, 0, 0),
        (0, 0, 255), (32, 128, 192), (200, 60, 30), (16, 16, 16)]  # fmt: skip


def picture(w, h):
    if "--bars" in sys.argv:
        bar = w // len(BARS)
        row = b"".join(
            bytes((c[2], c[1], c[0], 255)) * (bar if i < len(BARS) - 1 else w - bar * i)
            for i, c in enumerate(BARS)
        )
        return row * h
    row_bg = bytes((BACKGROUND[2], BACKGROUND[1], BACKGROUND[0], 255)) * w
    x0, x1 = w * 3 // 8, w * 5 // 8
    row_mid = (
        bytes((BACKGROUND[2], BACKGROUND[1], BACKGROUND[0], 255)) * x0
        + b"\xff" * 4 * (x1 - x0)
        + bytes((BACKGROUND[2], BACKGROUND[1], BACKGROUND[0], 255)) * (w - x1)
    )
    y0, y1 = h * 3 // 8, h * 5 // 8
    return b"".join(row_mid if y0 <= y < y1 else row_bg for y in range(h))


PADDED = []  # "padded" (cursor-control): the cursor as a 64x64 picture, mostly transparent
LARGE = []  # "large": a 200x240 opaque cursor (256x256 picture), hotspot 100,120
TRANSLUCENT = []  # "translucent": the plain 16x16 cursor at alpha 64 (and shown, not padded)


def cursor_frame(visible):
    if LARGE:
        w = h = 256
        row = lambda y: b"".join(
            bytes((20, 80, 220, 255)) if x < 200 and y < 240 else b"\0" * 4 for x in range(w)
        )
        pixels = b"".join(row(y) for y in range(h))
        hot = (100, 120)
    elif PADDED:
        # a 20x30 opaque box at 10,12 in a transparent 64x64, the hotspot at 14,15 inside it
        w = h = 64
        row = lambda y: b"".join(
            bytes((20, 80, 220, 255)) if 10 <= x < 30 and 12 <= y < 42 else b"\0" * 4
            for x in range(w)
        )
        pixels = b"".join(row(y) for y in range(h))
        hot = (14, 15)
    else:
        w = h = 16
        pixels = bytes((20, 80, 220, 64 if TRANSLUCENT else 255)) * (w * h)  # BGRA on the wire
        hot = (0, 0)
    header = b"WOSCUR1\0" + struct.pack(
        "<IIiiIIIIIIII", 56 + len(pixels), visible, 960, 540, 1920, 1080, w, h, w, h, *hot
    )
    return header + pixels


def tone_second():
    """One second of the tone, 44.1 kHz s16 LE stereo: a whole number of periods, so it loops."""
    import math

    out = bytearray()
    for i in range(AUDIO_RATE):
        v = int(round(32767 * TONE_LEVEL * math.sin(2 * math.pi * TONE_HZ * i / AUDIO_RATE)))
        out += struct.pack("<hh", v, v)
    return bytes(out)


def serve_audio(bus, d, log):
    """--audio: QEMU's D-Bus audio, writing the tone to each registered out listener."""
    second = tone_second()
    listeners = []
    held_registrations = []
    path = "/org/qemu/Display1/AudioOutListener"
    iface = "org.qemu.Display1.AudioOutListener"

    def call(conn, method, value):
        conn.call(None, path, iface, method, value, None, Gio.DBusCallFlags.NONE, 1000, None,
                  None)  # fmt: skip

    def ready(_src, result):
        conn = Gio.DBusConnection.new_finish(result)
        call(conn, "Init", GLib.Variant("(tybbuyuub)",
                                        (1, 16, True, False, AUDIO_RATE, 2, 4, AUDIO_RATE * 4, False)))  # fmt: skip
        call(conn, "SetEnabled", GLib.Variant("(tb)", (1, True)))
        listeners.append([conn, time.monotonic(), 0])
        log.write("Audio.RegisterOutListener\n")

    def tick():
        now = time.monotonic()
        for entry in list(listeners):
            conn, start, sent = entry
            if conn.is_closed():
                listeners.remove(entry)
                continue
            due = int((now - start) * AUDIO_RATE) - sent
            if due <= 0:
                continue
            o = (sent % AUDIO_RATE) * 4
            chunk = (second + second)[o : o + due * 4]
            data = GLib.Variant.new_from_bytes(
                GLib.VariantType.new("ay"), GLib.Bytes.new(chunk), True
            )
            call(conn, "Write", GLib.Variant.new_tuple(GLib.Variant("t", 1), data))
            entry[2] = sent + due
        return True

    def method(conn, sender, path_, iface_, name, params, invocation):
        fds = invocation.get_message().get_unix_fd_list()
        fd = fds.get(params.unpack()[0])
        if os.path.exists(os.path.join(d, "audio-register-hold")):
            # An unresponsive QEMU must not make listener shutdown wait for the RPC timeout.
            held_registrations.append((invocation, fd))
            open(os.path.join(d, "audio-register-held"), "w").close()
            return
        invocation.return_value(None)
        stream = Gio.Socket.new_from_fd(fd).connection_factory_create_connection()
        Gio.DBusConnection.new(stream, Gio.dbus_generate_guid(),
                               Gio.DBusConnectionFlags.AUTHENTICATION_SERVER, None, None, ready)  # fmt: skip

    info = Gio.DBusNodeInfo.new_for_xml(AUDIO_XML)
    bus.register_object("/org/qemu/Display1/Audio", info.interfaces[0], method, None, None)
    GLib.timeout_add(10, tick)


class Gbm:
    """Linear XRGB8888 buffer objects on the first render node, through libgbm (ctypes)."""

    XRGB8888 = 0x34325258
    USE_LINEAR = 1 << 4
    USE_RENDERING = 1 << 2
    TRANSFER_WRITE = 1 << 1

    def __init__(self):
        import ctypes
        import glob

        nodes = sorted(glob.glob("/dev/dri/renderD*"))
        try:
            lib = ctypes.CDLL("libgbm.so.1")
        except OSError:
            lib = None
        if not nodes or not lib:
            print("fake-qemu: --dmabuf needs a render node and libgbm", file=sys.stderr)
            sys.exit(77)
        self.ct, self.lib = ctypes, lib
        vp, u32 = ctypes.c_void_p, ctypes.c_uint32
        lib.gbm_create_device.restype = vp
        lib.gbm_create_device.argtypes = [ctypes.c_int]
        lib.gbm_bo_create.restype = vp
        lib.gbm_bo_create.argtypes = [vp, u32, u32, u32, u32]
        lib.gbm_bo_get_fd.argtypes = [vp]
        lib.gbm_bo_get_stride.restype = u32
        lib.gbm_bo_get_stride.argtypes = [vp]
        lib.gbm_bo_get_modifier.restype = ctypes.c_uint64
        lib.gbm_bo_get_modifier.argtypes = [vp]
        lib.gbm_bo_map.restype = vp
        lib.gbm_bo_map.argtypes = [vp, u32, u32, u32, u32, u32, ctypes.POINTER(u32),
                                   ctypes.POINTER(vp)]  # fmt: skip
        lib.gbm_bo_unmap.argtypes = [vp, vp]
        self.fd = os.open(nodes[0], os.O_RDWR | os.O_CLOEXEC)
        self.dev = lib.gbm_create_device(self.fd)
        if not self.dev:
            print(f"fake-qemu: no GBM device on {nodes[0]}", file=sys.stderr)
            sys.exit(77)

    def buffer(self, w, h, rows):
        """A buffer object filled with rows (a function of y: w * 4 bytes, B, G, R, X): its
        dma-buf fd, stride and modifier."""
        ct, lib = self.ct, self.lib
        bo = lib.gbm_bo_create(self.dev, w, h, self.XRGB8888, self.USE_LINEAR | self.USE_RENDERING)
        if not bo:
            bo = lib.gbm_bo_create(self.dev, w, h, self.XRGB8888, self.USE_LINEAR)
        if not bo:
            print("fake-qemu: gbm_bo_create failed", file=sys.stderr)
            sys.exit(77)
        stride, data = ct.c_uint32(), ct.c_void_p()
        ptr = lib.gbm_bo_map(bo, 0, 0, w, h, self.TRANSFER_WRITE, ct.byref(stride), ct.byref(data))
        if not ptr:
            print("fake-qemu: gbm_bo_map failed", file=sys.stderr)
            sys.exit(77)
        for y in range(h):
            ct.memmove(ptr + y * stride.value, rows(y), w * 4)
        lib.gbm_bo_unmap(bo, data)
        return lib.gbm_bo_get_fd(bo), lib.gbm_bo_get_stride(bo), lib.gbm_bo_get_modifier(bo)


def dmabuf_buffers(w, h):
    """--dmabuf's buffers: (fd, stride, modifier) each; exits 77 where GBM cannot make them."""
    count = int(sys.argv[sys.argv.index("--buffers") + 1]) if "--buffers" in sys.argv else 3
    gbm = Gbm()
    base = picture(w, h)
    bar = b"\xff\xff\x00\xff"  # B, G, R, X: cyan
    bufs = []
    for i in range(count):
        x0 = (w - w // 8) * i // max(count - 1, 1)
        x1 = x0 + w // 8

        def rows(y, x0=x0, x1=x1):
            row = base[y * w * 4 : (y + 1) * w * 4]
            if y < h // 8:  # the bar, at this buffer's place in the top eighth
                row = row[: x0 * 4] + bar * (x1 - x0) + row[x1 * 4 :]
            return row

        bufs.append(gbm.buffer(w, h, rows))
    return bufs


def serve_dmabuf(d, w, h, peers, bufs):
    """--dmabuf: GBM buffers flipped at --rate, each UpdateDMABUF's answer awaited (as QEMU)."""
    rate = int(sys.argv[sys.argv.index("--rate") + 1]) if "--rate" in sys.argv else 60
    count = len(bufs)
    st = {
        "frames": 0,
        "late": 0,
        "waited_us": 0,
        "pending": None,
        "due": False,
        "n": 0,
        "period": -1,
    }
    period = 1.0 / rate
    start = time.monotonic()
    stats = os.path.join(d, "dmabuf.stats")

    def write_stats():
        tmp = stats + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"frames={st['frames']} late={st['late']} waited_us={st['waited_us']} "
                    f"t_us={int(time.monotonic() * 1e6)}\n")  # fmt: skip
        os.replace(tmp, stats)

    def answered(conn, result, sent):
        try:
            conn.call_finish(result)
        except GLib.Error as e:
            print(f"fake-qemu: UpdateDMABUF: {e.message}", file=sys.stderr)
        st["waited_us"] += int((time.monotonic() - sent) * 1e6)
        st["pending"] = None
        st["frames"] += 1
        write_stats()
        if st["due"]:  # late: it goes now
            st["due"] = False
            frame()

    def frame():
        if not peers or not os.path.exists(os.path.join(d, "animate")):
            return
        if st["pending"]:
            if not st["due"]:
                st["late"] += 1
            st["due"] = True
            return
        conn = peers[-1]
        if conn.is_closed():
            peers.remove(conn)
            return
        fd, stride, mod = bufs[st["n"] % count]
        st["n"] += 1
        fds = Gio.UnixFDList.new_from_array([os.dup(fd)])
        conn.call_with_unix_fd_list(
            None, "/org/qemu/Display1/Listener", "org.qemu.Display1.Listener", "ScanoutDMABUF",
            GLib.Variant("(huuuutb)", (0, w, h, stride, Gbm.XRGB8888, mod, False)), None,
            Gio.DBusCallFlags.NO_AUTO_START, -1, fds, None, None)  # fmt: skip
        sent = time.monotonic()
        st["pending"] = sent
        conn.call(None, "/org/qemu/Display1/Listener", "org.qemu.Display1.Listener",
                  "UpdateDMABUF", GLib.Variant("(iiii)", (0, 0, w, h)), None,
                  Gio.DBusCallFlags.NONE, 10000, None,
                  lambda c, r: answered(c, r, sent))  # fmt: skip

    def tick():
        # one frame a period: the timeout is in whole ms, so it may come back before the
        # boundary, and a second frame() in the same period would count a false late frame
        k = int((time.monotonic() - start) / period)
        if k != st["period"]:
            st["period"] = k
            frame()
        nxt = start + (k + 1) * period
        GLib.timeout_add(max(1, math.ceil((nxt - time.monotonic()) * 1000)), tick)
        return False

    write_stats()
    tick()


def main():
    d = sys.argv[1]
    w, h = 1920, 1080
    if "--size" in sys.argv:
        w, h = (int(v) for v in sys.argv[sys.argv.index("--size") + 1].split("x"))
    os.makedirs(d, exist_ok=True)
    bufs = dmabuf_buffers(w, h) if "--dmabuf" in sys.argv else None  # before the bus: may exit
    # what the launcher writes beside the sockets (paths.write_vm_info), with the run's [beanjar]
    # versions (information only)
    top = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(top, "packaging", "VERSION")) as f:
        version = f.read().strip()
    with open(os.path.join(d, "vm-info.toml"), "w") as f:
        f.write(
            f'platform = "webOS"\n\n[beanjar]\nhost_version = "{version}"\nguest_version = "{version}"\n'
        )
    sock = os.path.join(d, "dbus.sock")
    reuse = "--reuse-bus" in sys.argv
    own = (
        ("cursor.sock", "ir.sock", "ready")
        if reuse
        else ("dbus.sock", "cursor.sock", "ir.sock", "ready")
    )
    for name in own:
        try:
            os.unlink(os.path.join(d, name))
        except FileNotFoundError:
            pass
    daemon = None
    if not reuse:
        daemon = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", "--nopidfile", f"--address=unix:path={sock}"],
            stdout=subprocess.DEVNULL,
        )
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    log = open(os.path.join(d, "input.log"), "a", buffering=1)
    loop = GLib.MainLoop()
    bus = Gio.DBusConnection.new_for_address_sync(
        f"unix:path={sock}",
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )
    info = Gio.DBusNodeInfo.new_for_xml(XML)
    peers = []
    # built once, from bytes (PyGObject would convert a bytes "ay" element by element)
    pixels = GLib.Variant.new_from_bytes(
        GLib.VariantType.new("ay"), GLib.Bytes.new(picture(w, h)), True
    )
    args = GLib.Variant.new_tuple(
        GLib.Variant("u", w),
        GLib.Variant("u", h),
        GLib.Variant("u", w * 4),
        GLib.Variant("u", A8R8G8B8),
        pixels,
    )

    def delivered(c, r, method_name="Scanout"):
        try:
            c.call_finish(r)
            log.write(f"Listener.{method_name} ack\n")
            print(f"fake-qemu: {method_name} delivered", file=sys.stderr)
        except GLib.Error as e:
            log.write(f"Listener.{method_name} error\n")
            print(f"fake-qemu: {method_name}: {e.message}", file=sys.stderr)

    def scanout(conn):
        conn.call(
            None,
            "/org/qemu/Display1/Listener",
            "org.qemu.Display1.Listener",
            "Scanout",
            args,
            None,
            Gio.DBusCallFlags.NONE,
            10000,
            None,
            delivered,
        )

    # --animate: the picture in a shared memfd, as QEMU's Unix.Map (2D, or a GL scanout read back
    # on WSL), each listener given it once with ScanoutMap, then UpdateMap for the noise band
    mapped = set()
    bar = {"y": None, "n": 0}

    def update_map(y0, rows):
        for conn in peers:
            if id(conn) not in mapped:
                mapped.add(id(conn))
                fds = Gio.UnixFDList.new_from_array([os.dup(memfd)])
                conn.call_with_unix_fd_list(
                    None, "/org/qemu/Display1/Listener", "org.qemu.Display1.Listener.Unix.Map",
                    "ScanoutMap", GLib.Variant("(huuuuu)", (0, 0, w, h, w * 4, A8R8G8B8)), None,
                    Gio.DBusCallFlags.NONE, 10000, fds, None, None)  # fmt: skip
            conn.call(None, "/org/qemu/Display1/Listener", "org.qemu.Display1.Listener.Unix.Map",
                      "UpdateMap", GLib.Variant("(iiii)", (0, y0, w, rows)), None,
                      Gio.DBusCallFlags.NONE, 10000, None, None)  # fmt: skip

    def animate_bar():
        on = os.path.exists(os.path.join(d, "animate"))
        old = bar["y"]
        if not peers or (not on and old is None):
            return True
        base = picture(w, h)
        if old is not None:  # the band's rows as they were
            os.pwrite(memfd, base[old * w * 4 : (old + BAR) * w * 4], old * w * 4)
        if not on:
            bar["y"] = None
            update_map(old, BAR)
            return True
        n = bar["n"] = bar["n"] + 1
        y = (n * BAR_STEP) % (h - BAR)
        grey = (n * 53) % 200 + 40
        os.pwrite(memfd, bytes((grey, grey, grey, 255)) * (w * BAR), y * w * 4)
        bar["y"] = y
        lo = y if old is None else min(old, y)
        hi = y + BAR if old is None else max(old, y) + BAR
        update_map(lo, hi - lo)
        return True

    def animate():
        if "--bar" in sys.argv:
            return animate_bar()
        if not peers or not os.path.exists(os.path.join(d, "animate")):
            return True
        band = h if "--full" in sys.argv else h // 4
        grey = os.urandom(w * band)
        noise = bytearray(w * band * 4)
        noise[0::4] = grey
        noise[1::4] = grey
        noise[2::4] = grey
        noise[3::4] = b"\xff" * (w * band)
        if "--2d" in sys.argv:
            band_args = GLib.Variant.new_tuple(
                GLib.Variant("i", 0), GLib.Variant("i", 0), GLib.Variant("i", w),
                GLib.Variant("i", band), GLib.Variant("u", w * 4), GLib.Variant("u", A8R8G8B8),
                GLib.Variant.new_from_bytes(GLib.VariantType.new("ay"), GLib.Bytes.new(bytes(noise)), True),
            )  # fmt: skip
            for conn in peers:
                conn.call(None, "/org/qemu/Display1/Listener", "org.qemu.Display1.Listener",
                          "Update", band_args, None, Gio.DBusCallFlags.NONE, 10000, None, None)  # fmt: skip
            return True
        os.pwrite(memfd, bytes(noise), 0)
        update_map(0, band)
        return True

    if "--dmabuf" in sys.argv:
        serve_dmabuf(d, w, h, peers, bufs)
    elif "--animate" in sys.argv:
        memfd = os.memfd_create("fake-qemu-surface")
        os.pwrite(memfd, picture(w, h), 0)
        GLib.timeout_add(33, animate)

    def peer_ready(_src, result):
        conn = Gio.DBusConnection.new_finish(result)
        peers.append(conn)
        if "--dmabuf" not in sys.argv:
            scanout(conn)

    def method(conn, sender, path, iface, name, params, invocation):
        short = iface.rsplit(".", 1)[1]
        if short == "Console" and name == "RegisterListener":
            fds = invocation.get_message().get_unix_fd_list()
            fd = fds.get(params.unpack()[0])
            invocation.return_value(None)
            stream = Gio.Socket.new_from_fd(fd).connection_factory_create_connection()
            Gio.DBusConnection.new(
                stream,
                Gio.dbus_generate_guid(),
                Gio.DBusConnectionFlags.AUTHENTICATION_SERVER,
                None,
                None,
                peer_ready,
            )
            return
        log.write(f"{short}.{name} {' '.join(str(v) for v in params.unpack())}\n")
        invocation.return_value(None)

    def get_property(conn, sender, path, iface, prop):
        if prop in ("Width", "Height"):
            return GLib.Variant("u", w if prop == "Width" else h)
        return GLib.Variant("b", True)

    for iface in info.interfaces:
        bus.register_object(CONSOLE, iface, method, get_property, None)
    if "--audio" in sys.argv:
        serve_audio(bus, d, log)
    bus.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "RequestName",
        GLib.Variant("(su)", ("org.qemu", 4)),
        None,
        Gio.DBusCallFlags.NONE,
        5000,
        None,
    )

    # webos.cursor
    csock_path = os.path.join(d, "cursor.sock")
    csock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    csock.bind(csock_path)
    csock.listen(1)
    csock.setblocking(False)
    clients = []
    legacy_visible = 1
    legacy_silent = False

    def accept(*_):
        try:
            c, _ = csock.accept()
            c.setblocking(True)
            if not legacy_silent:
                c.sendall(cursor_frame(legacy_visible))
            clients.append(c)
        except (BlockingIOError, BrokenPipeError):
            pass
        return True

    def heartbeat():
        if legacy_silent:
            return True
        for c in list(clients):
            try:
                c.sendall(cursor_frame(legacy_visible))
            except OSError:
                clients.remove(c)
        return True

    cursor_watch = GLib.io_add_watch(
        csock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, accept
    )
    GLib.timeout_add(1000, heartbeat)

    def cursor_control():
        nonlocal legacy_visible, legacy_silent
        path = os.path.join(d, "cursor-control")
        try:
            command = open(path).read().strip()
            os.unlink(path)
        except FileNotFoundError:
            return True
        log.write(f"CursorControl {command}\n")
        if command == "legacy-show":
            legacy_visible = 1
            heartbeat()
        if command == "legacy-silent":
            legacy_silent = True
            for client in clients:
                client.close()
            clients.clear()
        if command == "large":  # and shown
            LARGE.append(1)
            legacy_visible = 1
            heartbeat()
        if command == "translucent":  # not padded
            TRANSLUCENT.append(1)
            PADDED.clear()
            legacy_visible = 1
            heartbeat()
        if command == "padded":
            PADDED.append(1)
            heartbeat()
        if command == "legacy-hidden":
            legacy_visible = 0
            heartbeat()
        elif command == "native-only":
            GLib.source_remove(cursor_watch)
            csock.close()
            os.unlink(csock_path)
            for client in clients:
                client.close()
            clients.clear()

        def send(name, value):
            for peer in peers:
                peer.call(
                    None,
                    "/org/qemu/Display1/Listener",
                    "org.qemu.Display1.Listener",
                    name,
                    value,
                    None,
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                    lambda conn, result, method=name: delivered(conn, result, method),
                )

        if command in ("legacy-hidden", "native-only", "native-show"):
            payload = struct.pack("=I", 0xFFF02850) * (16 * 16)
            send("CursorDefine", GLib.Variant("(iiiiay)", (16, 16, 4, 6, payload)))
            send("MouseSet", GLib.Variant("(iii)", (100, 100, 1)))
        elif command == "native-hide":
            send("MouseSet", GLib.Variant("(iii)", (100, 100, 0)))
        elif command == "native-invalid":
            for width, height, hx, hy, data in (
                (0, 16, 0, 0, b""),
                (65536, 65536, 0, 0, b""),
                (16, 16, 16, 0, b""),
                (16, 16, 0, 0, b"\0"),
            ):
                send("CursorDefine", GLib.Variant("(iiiiay)", (width, height, hx, hy, data)))
        return True

    GLib.timeout_add(50, cursor_control)

    # DIR/display-control: "disable", a Disable to each listener (the guest's output off); "stray MS",
    # a Disable and the 2D Scanout again MS ms later (QEMU's dmabuf release between two flips,
    # docs/research/pi-black-flashes.md); "scanout", the Scanout again. A Disable is logged as
    # "Listener.Disable sent T COMMAND" (T: monotonic seconds).
    def display_control():
        path = os.path.join(d, "display-control")
        try:
            command = open(path).read().split()
            os.unlink(path)
        except FileNotFoundError:
            return True

        def rescan():
            for conn in peers:
                scanout(conn)
            return False

        if command[:1] == ["scanout"]:
            rescan()
            return True
        for conn in peers:
            conn.call(None, "/org/qemu/Display1/Listener", "org.qemu.Display1.Listener",
                      "Disable", None, None, Gio.DBusCallFlags.NONE, 1000, None, None)  # fmt: skip
        log.write(f"Listener.Disable sent {time.monotonic():.6f} {' '.join(command)}\n")
        if command[:1] == ["stray"]:
            GLib.timeout_add(int(command[1]), rescan)
        return True

    GLib.timeout_add(10, display_control)

    # webos.ir: each 4-byte record (include/webos-ir.h) is logged as IR.Down/IR.Up <code> (an
    # extended one as 0xdbNN), or IR.Bad <bytes>; one client at a time, as QEMU's chardev
    isock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    isock.bind(os.path.join(d, "ir.sock"))
    isock.listen(1)
    isock.setblocking(False)

    def ir_accept(*_):
        try:
            c, _ = isock.accept()
        except BlockingIOError:
            return True
        buf = bytearray()

        def ir_read(*_):
            data = c.recv(64)
            if not data:
                c.close()
                return False
            buf.extend(data)
            while len(buf) >= 4:
                magic, down, custom, code = buf[:4]
                del buf[:4]
                if magic == 0x4B and custom == 0 and down <= 1:  # the set's own key
                    log.write(f"Local.{'Down' if down else 'Up'} 0x{code:02x}\n")
                elif magic == 0x45 and custom == 0x04 and down <= 1:  # an extended code
                    log.write(f"IR.{'Down' if down else 'Up'} 0xdb{code:02x}\n")
                elif magic != 0x49 or custom != 0x04 or down > 1:
                    log.write(f"IR.Bad {magic:02x}{down:02x}{custom:02x}{code:02x}\n")
                else:
                    log.write(f"IR.{'Down' if down else 'Up'} 0x{code:02x}\n")
            return True

        GLib.io_add_watch(
            c.fileno(), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP, ir_read
        )
        return True

    GLib.io_add_watch(isock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, ir_accept)

    def stop(*_):
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    open(os.path.join(d, "ready"), "w").close()
    try:
        loop.run()
    finally:
        if daemon:
            daemon.terminate()
            daemon.wait()
        for name in own:
            try:
                os.unlink(os.path.join(d, name))
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
