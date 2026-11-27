#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar-vnc without clients does no frame work (host/vnc/main.c update_idle, the core's
wv_idle), against the stand-in QEMU display (tests/fakes/fake-qemu.py --animate: a noise band
written into a shared surface, UpdateMap every 33 ms), no VM, no window.

    check-idle.py BEANJAR_VNC

1. With no client, QEMU's updates are acknowledged but not copied: in 2 s the core skips about 60
   (its "skipped" counter) and copies none (its "frames").
2. An RFB client on the local socket gets its first frame within 0.5 s, already with the noise
   band (the picture brought up to date from the shared surface), and the updates are copied
   while it stays.
3. Idle again after it leaves; org.beanjar.Display1.Screenshot while idle composes one frame on
   demand, up to date (the band differs from the RFB client's).
4. With --video-local (x264), a video client's first unit is a keyframe within 0.5 s, decoded by
   ffmpeg (when it is there) with the band; neatvnc is not fed for it (vnc_frames do not grow
   beyond what the RFB client got).
5. SIGTERM: the counters add up.
Steps 1 to 5 run twice: the band in a shared surface (UpdateMap), then in 2D Update messages
(fake-qemu --2d), which the core copies into its own copy of the surface while idle.
6. Media (with the media-helper and ffmpeg; else skipped): a clip played through
   tests/fakes/fake-media.py into a video window while nobody looks has its pictures picked on
   time but none uploaded (the helper's report: shown=0); an RFB client's frames draw them.
"""

import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import loopback  # noqa: E402
import videostream as vs  # noqa: E402

VNC = sys.argv[1]
W, H = 640, 360
BLUE = (32, 128, 192)

SCREENSHOT = """
import sys
from gi.repository import Gio, GLib
conn = Gio.DBusConnection.new_for_address_sync(
    sys.argv[1],
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
    None, None)
conn.call_sync("org.beanjar.Display1", "/org/beanjar/Display1", "org.beanjar.Display1",
               "Screenshot", GLib.Variant("(suu)", (sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))),
               None, 0, 10000, None)
"""


def fail(message):
    sys.exit(f"check-idle: {message}")


def counters(log):
    """The core's summary line's numbers (frames=, skipped=, vnc_frames=, ...)."""
    line = next((x for x in open(log).read().splitlines() if " skipped=" in x), "")
    return {k: int(v) for k, v in (w.split("=", 1) for w in line.split() if "=" in w)
            if v.isdigit()}  # fmt: skip


def band_is_noise(pixel_at):
    """Whether the top quarter holds the noise band: grey values that vary, not the blue."""
    samples = [pixel_at(x, y) for x in range(5, W, 37) for y in range(3, H // 4 - 2, 11)]
    greys = [p for p in samples if p[0] == p[1] == p[2]]
    return len(greys) > len(samples) * 0.8 and len({p[0] for p in greys}) > 10


class RFB:
    """A minimal RFB client on beanjar-vnc's local socket: None, raw, 32-bit RGBX."""

    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.connect(path)
        self.s.settimeout(10)
        if not self.rx(12).startswith(b"RFB 003."):
            fail("no RFB greeting")
        self.s.sendall(b"RFB 003.008\n")
        if list(self.rx(self.rx(1)[0])) != [1]:
            fail("the local socket does not offer None alone")
        self.s.sendall(b"\x01")
        if struct.unpack(">I", self.rx(4))[0]:
            fail("None refused")
        self.s.sendall(b"\x01")
        self.w, self.h = struct.unpack(">HH", self.rx(4))
        self.rx(16)
        self.rx(struct.unpack(">I", self.rx(4))[0])
        self.s.sendall(struct.pack(">Bxxx", 0) + struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255,
                                                             255, 255, 0, 8, 16))  # fmt: skip
        self.s.sendall(struct.pack(">BxHi", 2, 1, 0))  # raw only
        self.pixels = bytearray(self.w * self.h * 4)

    def rx(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                fail(f"RFB closed ({len(b)} of {n} bytes)")
            b += c
        return b

    def frame(self, incremental=0):
        self.s.sendall(struct.pack(">BBHHHH", 3, incremental, 0, 0, self.w, self.h))
        while True:
            kind = self.rx(1)[0]
            if kind == 0:
                break
            fail(f"server message {kind}")
        self.rx(1)
        for _ in range(struct.unpack(">H", self.rx(2))[0]):
            x, y, rw, rh, enc = struct.unpack(">HHHHi", self.rx(12))
            if enc != 0:
                continue  # a pseudo-encoding's rectangle (none asked for)
            data = self.rx(rw * rh * 4)
            for row in range(rh):
                o = ((y + row) * self.w + x) * 4
                self.pixels[o : o + rw * 4] = data[row * rw * 4 : (row + 1) * rw * 4]

    def pixel(self, x, y):
        o = (y * self.w + x) * 4
        return tuple(self.pixels[o : o + 3])

    def close(self):
        self.s.close()


def run(two_d):
    """Steps 1 to 5 with the band in a shared surface (UpdateMap), or in 2D Update messages."""
    say = (lambda m: print(f"2d: {m}")) if two_d else print
    probe = subprocess.run([VNC, "--help"], capture_output=True, text=True).stdout
    video = "--video-local" in probe
    tmp = tempfile.mkdtemp(prefix="check-idle-")
    fake = subprocess.Popen([sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), tmp,
                             "--size", f"{W}x{H}", "--animate", *(["--2d"] if two_d else [])])  # fmt: skip
    vnc = None
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(tmp, "ready")):
                break
            time.sleep(0.05)
        else:
            fail("fake-qemu did not start")
        port = loopback.port()
        local, vsock = os.path.join(tmp, "rfb-local.sock"), os.path.join(tmp, "video-local.sock")
        log = os.path.join(tmp, "vnc.log")
        bus = f"unix:path={tmp}/dbus.sock"
        vnc = subprocess.Popen(
            [VNC, bus, "--listen", loopback.ADDRESS, "--port", str(port), "--local", local, "--size",
             f"{W}x{H}", "--no-media", "--name", "check-idle",
             *(["--video-local", vsock, "--video-encoder", "x264"] if video else [])],
            stdout=open(log, "w"), stderr=subprocess.STDOUT,
        )  # fmt: skip
        for _ in range(100):
            if "beanjar-vnc: ready" in open(log).read():
                break
            if vnc.poll() is not None:
                fail(f"beanjar-vnc exited: {open(log).read()}")
            time.sleep(0.1)
        else:
            fail("beanjar-vnc not ready")

        # 1. no client: updates skipped
        open(os.path.join(tmp, "animate"), "w").close()
        time.sleep(2.0)

        # 2. an RFB client: a fresh first frame at once
        t0 = time.monotonic()
        client = RFB(local)
        client.frame()
        took = time.monotonic() - t0
        if not band_is_noise(client.pixel):
            client.frame(1)  # neatvnc's held frame first; the fresh one right after
            took = time.monotonic() - t0
        if took > 0.5 or not band_is_noise(client.pixel):
            fail(f"the RFB client's first fresh frame: {took:.2f} s, band {client.pixel(10, 10)}")
        first_band = bytes(client.pixels[: W * 4 * 8])
        for _ in range(15):
            client.frame(1)
        client.close()
        say(f"rfb: a frame with the noise band {took * 1000:.0f} ms after connecting")

        # 3. idle again; a screenshot composed on demand
        time.sleep(1.5)
        ppm = os.path.join(tmp, "shot.ppm")
        r = subprocess.run([sys.executable, "-c", SCREENSHOT, bus, ppm, str(W), str(H)],
                           capture_output=True, text=True, timeout=30)  # fmt: skip
        if r.returncode or not os.path.exists(ppm):
            fail(f"Screenshot: {r.stderr}")
        data = open(ppm, "rb").read()
        pixels = data.split(b"\n", 3)[3]
        shot = lambda x, y: tuple(pixels[(y * W + x) * 3 : (y * W + x) * 3 + 3])  # noqa: E731
        if not band_is_noise(shot) or shot(W - 5, H - 5) != BLUE:
            fail(f"the screenshot while idle: band {shot(10, 10)}, corner {shot(W - 5, H - 5)}")
        if pixels[: W * 3 * 8] == bytes(b for i, b in enumerate(first_band) if i % 4 != 3):
            fail("the screenshot while idle shows the RFB client's old band")
        say("screenshot: composed on demand while idle, up to date")

        # 4. a video client alone
        if video:
            t0 = time.monotonic()
            first, units = None, []
            with socket.socket(socket.AF_UNIX) as c:
                c.connect(vsock)
                c.sendall(b"h264 bitrate=1500\n")
                c.settimeout(5)
                data = b""
                while not units and time.monotonic() - t0 < 5:
                    data += c.recv(1 << 16)
                    if len(data) > vs.HEADER:
                        units = vs.units(vs.header(data)[1])[0]
                first = time.monotonic() - t0
            if not units or not units[0][0] or first > 0.5:
                fail(f"the video client's first unit: {first:.2f} s, {len(units)} units")
            if vs.have_ffmpeg():
                frames, errors = vs.decode(units[0][2], W, H)
                if (
                    errors
                    or not frames
                    or not band_is_noise(lambda x, y: vs.pixel(frames[0], W, x, y))
                ):
                    fail(f"the video client's keyframe: {errors or 'no noise band'}")
            say(f"video: a keyframe {first * 1000:.0f} ms after connecting, with the band")
            time.sleep(1.0)  # idle again

        # 5. the counters
        animate = os.path.join(tmp, "animate")
        os.unlink(animate)
        vnc.send_signal(signal.SIGTERM)
        if vnc.wait(timeout=10):
            fail(f"beanjar-vnc exited {vnc.returncode}")
        n = counters(log)
        vnc = None
        # idle: 2 + 1.5 (+ 1) s at 30 updates a second skipped; copied only while a client was there
        if n.get("skipped", 0) < 80 or n.get("frames", 999) > n["skipped"]:
            fail(f"counters: {n}")
        # neatvnc fed: the start's frame and the RFB client's (about 1 s), none for video alone
        if n.get("vnc_frames", 999) > 60:
            fail(f"vnc_frames {n.get('vnc_frames')}: frames composed while idle or for video")
        say(f"counters: skipped={n['skipped']} frames={n['frames']} vnc_frames={n['vnc_frames']}")
    finally:
        if vnc and vnc.poll() is None:
            vnc.kill()
            vnc.wait()
        fake.send_signal(signal.SIGTERM)
        fake.wait(timeout=10)
        subprocess.run(["rm", "-rf", tmp])


def media():
    """Step 6: decoded pictures are uploaded only when drawn."""
    helper = os.environ.get("MEDIAHUB") or os.path.join(TOP, "build/media-helper/media-helper")
    if not os.access(helper, os.X_OK) or not vs.have_ffmpeg():
        print("media: skipped (no media-helper or no ffmpeg)")
        return
    tmp = tempfile.mkdtemp(prefix="check-idle-media-")
    procs = []
    try:
        clip = os.path.join(tmp, "clip.mp4")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc=size=320x180:rate=30", "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000",
                        "-t", "12", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac",
                        clip], check=True)  # fmt: skip
        procs.append(subprocess.Popen([sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"),
                                       tmp, "--size", f"{W}x{H}"]))  # fmt: skip
        for _ in range(100):
            if os.path.exists(os.path.join(tmp, "ready")):
                break
            time.sleep(0.05)
        prefix, table = os.path.join(tmp, "media"), os.path.join(tmp, "table.sock")
        procs.append(subprocess.Popen(
            [sys.executable, os.path.join(TOP, "tests/fakes/fake-media.py"), prefix, table, clip,
             "--seconds", "11", "--helper", helper],
            stdout=open(os.path.join(tmp, "fake-media.log"), "w"), stderr=subprocess.STDOUT))  # fmt: skip
        for _ in range(100):
            if os.path.exists(table):
                break
            time.sleep(0.05)
        local, log = os.path.join(tmp, "rfb-local.sock"), os.path.join(tmp, "vnc.log")
        port = loopback.port()
        vnc = subprocess.Popen(
            [VNC, f"unix:path={tmp}/dbus.sock", "--listen", loopback.ADDRESS, "--port", str(port),
             "--local", local, "--size", f"{W}x{H}", "--video-socket", table, "--media-socket",
             prefix, "--name", "check-idle-media",
             # llvmpipe: on d3d12 (WSL) the first frame with a video window takes 20 to 30 ms
             # (its first drawing), which can drop a picture (docs/dev/architecture/vnc.md, GL)
             "--gl-adapter", "software"],
            stdout=open(log, "w"), stderr=subprocess.STDOUT)  # fmt: skip
        procs.append(vnc)

        def report(tag=":"):
            lines = [x for x in open(log).read().splitlines() if x.startswith("present 0 video")]
            lines = [x for x in lines if f"session=1{tag}" in x]
            if not lines:
                return None
            return {k: int(v) for k, v in (w.split("=", 1) for w in lines[-1].split() if "=" in w)
                    if v.isdigit()}  # fmt: skip

        time.sleep(7.0)  # the helper reports every 5 s
        idle = report()
        if not idle or idle["picked"] < 60 or idle["shown"] != 0:
            fail(f"media while idle: {idle} (pictures picked on time, none uploaded)")
        client = RFB(local)
        for _ in range(20):  # whole frames: the video is under fake-qemu's opaque OSD
            client.frame(0)
            time.sleep(0.05)
        client.close()
        vnc.send_signal(signal.SIGTERM)
        if vnc.wait(timeout=10):
            fail(f"media: beanjar-vnc exited {vnc.returncode}")
        end = report(" (exit)")
        # late drops (the core's queue full: its main loop behind) are reported, not judged: they
        # depend on the machine's load, as when tests run in parallel, not on being idle
        if not end or end["shown"] < 3:
            fail(f"media: at exit {end} (pictures drawn for the client)")
        print(f"media: {idle['picked']} pictures picked while idle, none uploaded; "
              f"{end['shown']} drawn for an RFB client, {end['late_drops']} dropped late")  # fmt: skip
    finally:
        for p in reversed(procs):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        subprocess.run(["rm", "-rf", tmp])


def main():
    run(False)
    run(True)
    media()
    print("ok")


if __name__ == "__main__":
    main()
