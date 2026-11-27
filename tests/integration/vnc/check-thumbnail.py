#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar-vnc's thumbnails (docs/dev/architecture/vnc.md, "Thumbnails") against fake-qemu --animate
at 1280x720, no VM, no window.

    check-thumbnail.py BEANJAR_VNC VIEWER_HEADLESS

Display1.Screenshot calls at 384x216, as beanjar-web's cards ask through `beanjar screenshot
--size`; the counters thumbnails, thumbnails_cached and thumbnail_us. Each check prints its
numbers and passes or fails:

1. With no client (nothing composed): a 384x216 P6 of fake-qemu's picture (the white box in the
   middle, the blue around it), made once (thumbnails + 1).
2. Asked again with nothing changed: the same bytes, from the cache (thumbnails_cached + 1, no
   thumbnail made).
3. The top quarter new noise every 33 ms: a new thumbnail, its top rows grey, averaged over the
   noise (their standard deviation under 30, where one sample a pixel gives 37 to 74), the box
   still white. Stopped: made once more (the updates skipped while idle), then from the cache.
4. With an RFB client taking frames (composed for it): the thumbnail of the moving picture.
5. At the desktop's size, 1280x720: not a thumbnail, the core's own screenshot (no thumbnail
   made).
6. beanjar-vnc gone: Display1 has no owner (the manager then runs the headless viewer once,
   host/core/beanjar/vm/library.py screenshot), and the headless viewer gives a 384x216 picture.
7. A GL guest (fake-qemu --dmabuf) that draws only after beanjar-vnc's first render, with no
   client, with memory frames and with GBM frames: its updates acknowledged without a copy, yet
   a Screenshot at 384x216 and at 1280x720 gives the picture, upright (the bar at the top).
   Skipped without a render node.
8. The same GL guest, a client attached while it draws frame A, then gone; the guest draws B
   while beanjar-vnc is idle: a Screenshot at 384x216 and at 1280x720 shows B, and QEMU's
   longest wait for an answer does not grow. Memory and GBM frames; skipped without a render
   node.
"""

import os
import signal
import subprocess
import sys
import tempfile
import time

import numpy

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import loopback  # noqa: E402
from rfbclient import Rfb  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
VNC, HEADLESS = sys.argv[1], sys.argv[2]
W, H, TW, TH = 1280, 720, 384, 216
NAME = "org.beanjar.Display1"
PATH = "/org/beanjar/Display1"
RAW = [0, -313, -224]


def fail(message):
    sys.exit(f"check-thumbnail: {message}")


def read_ppm(path):
    data = open(path, "rb").read()
    magic, size, maxval, pixels = data.split(b"\n", 3)
    w, h = (int(v) for v in size.split())
    if magic != b"P6" or maxval != b"255" or len(pixels) != w * h * 3:
        fail(f"{path}: not an 8-bit P6 of its size")
    return w, h, numpy.frombuffer(pixels, numpy.uint8).reshape(h, w, 3).astype(int), data


def bus(address):
    return Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )


def owned(conn):
    return conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "NameHasOwner", GLib.Variant("(s)", (NAME,)), GLib.VariantType("(b)"),
                          Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]  # fmt: skip


def shot(conn, path, w=TW, h=TH):
    """A Screenshot call: the picture and the call's time in ms."""
    start = time.monotonic()
    conn.call_sync(NAME, PATH, NAME, "Screenshot", GLib.Variant("(suu)", (path, w, h)), None,
                   Gio.DBusCallFlags.NONE, 20000, None)  # fmt: skip
    ms = (time.monotonic() - start) * 1000
    return (*read_ppm(path), ms)


def counters(vnc, log_path):
    seen = open(log_path).read().count("beanjar-vnc: stats ")
    vnc.send_signal(signal.SIGUSR1)
    for _ in range(100):
        lines = [x for x in open(log_path).read().splitlines() if "beanjar-vnc: stats " in x]
        if len(lines) > seen:
            return {k: int(v) for k, v in (f.split("=", 1) for f in lines[-1].split()[2:])}
        time.sleep(0.02)
    fail("no stats line")


def picture_ok(px, rows=range(TH)):
    """None if px is fake-qemu's picture, letterbox-free at 384x216, in the given rows: the white
    box (x 3/8 to 5/8, y 3/8 to 5/8, a pixel in from its edges), blue (32, 128, 192) around."""
    x0, x1, y0, y1 = TW * 3 // 8, TW * 5 // 8, TH * 3 // 8, TH * 5 // 8
    box = px[y0 + 2 : y1 - 2, x0 + 2 : x1 - 2]
    if numpy.abs(box - 255).max() > 6:
        return f"the box is not white (off by {numpy.abs(box - 255).max()})"
    blue = px[[y for y in rows if not y0 - 2 <= y < y1 + 2], : x0 - 2]
    if len(blue) and numpy.abs(blue - (32, 128, 192)).max() > 6:
        return f"the background is not blue (off by {numpy.abs(blue - (32, 128, 192)).max()})"
    return None


def bar_at(px, w):
    """Which of fake-qemu --dmabuf --buffers 3's bars a picture w wide shows: the cyan in its
    top eighth at the left (0), the middle (1) or the right (2), or None. Only if the bottom
    eighth is blue there, so an upside-down picture gives None."""
    top, bottom = px[len(px) // 16], px[len(px) - 1 - len(px) // 16]
    xs = [(w - w // 8) * i // 2 + w // 16 for i in range(3)]
    if any(numpy.abs(bottom[x] - (32, 128, 192)).max() > 6 for x in xs):
        return None
    cyan = [top[x][0] <= 40 and top[x][1] >= 215 and top[x][2] >= 215 for x in xs]
    return cyan.index(True) if cyan.count(True) == 1 else None


def check_idle_after_client(frames):
    """A client sees the GL guest's frame A, leaves; the guest draws B while beanjar-vnc is idle
    (its updates acknowledged without a copy): a Screenshot at 384x216 and at 1280x720 must show
    B. fake-qemu --dmabuf --buffers 3 flips three buffers, the bar at the left, the middle or the
    right; DIR/dmabuf.stats counts the frames, so the last one's buffer is known.
    Skipped without a render node or libgbm."""
    d = tempfile.mkdtemp(prefix="check-thumbnail-after-")
    fake = subprocess.Popen(
        [sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), d, "--size", f"{W}x{H}",
         "--dmabuf", "--buffers", "3", "--rate", "5"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    vnc = conn = client = None
    animate = os.path.join(d, "animate")
    stats = os.path.join(d, "dmabuf.stats")

    def guest_frames():
        try:
            return int(open(stats).read().split()[0].split("=")[1])
        except (FileNotFoundError, IndexError, ValueError):
            return 0

    def draw(avoid=()):
        """A few frames, the guest then still, until the last one's buffer is none of avoid:
        that buffer."""
        for attempt in range(12):
            open(animate, "w").close()
            start = guest_frames()
            while guest_frames() < start + 1 + attempt % 3:  # rounds of different lengths
                time.sleep(0.02)
            os.unlink(animate)
            time.sleep(0.6)  # the frames in flight answered
            last = (guest_frames() - 1) % 3
            if last not in avoid:
                return last
        fail(f"the guest's frames kept ending on buffer {avoid}: {open(stats).read()!r}")

    try:
        for _ in range(100):
            if os.path.exists(os.path.join(d, "ready")):
                break
            if fake.poll() == 77:
                print(f"after a client, {frames} frames: skipped (no render node or libgbm)")
                return
            time.sleep(0.05)
        else:
            fail("fake-qemu --dmabuf did not start")
        address = f"unix:path={d}/dbus.sock"
        log = open(os.path.join(d, "vnc.log"), "w")
        vnc = subprocess.Popen(
            [VNC, address, "--listen", loopback.ADDRESS, "--port", str(loopback.port()),
             "--size", f"{W}x{H}", "--fps", "30", "--no-media", "--native-cursor",
             "--frames", frames, "--local", os.path.join(d, "rfb.sock")],
            stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        for _ in range(100):
            if "beanjar-vnc: ready" in open(log.name).read():
                break
            if vnc.poll() is not None:
                fail(f"beanjar-vnc exited: {open(log.name).read()}")
            time.sleep(0.1)
        conn = bus(address)
        for _ in range(100):
            if owned(conn):
                break
            time.sleep(0.05)
        out = os.path.join(d, "shot.ppm")
        # A: with a client, its frames copied
        client = Rfb(os.path.join(d, "rfb.sock"), RAW)
        time.sleep(0.3)
        want_a = draw()
        a = bar_at(shot(conn, out, W, H)[2], W)
        client.close()
        client = None
        time.sleep(0.5)
        # B: drawn while idle
        # the first frames after the client left may still be copied (the core goes idle with
        # the next render): some, then B on the buffer neither A nor those ended on
        settled = draw()
        c0 = counters(vnc, log.name)
        want_b = draw(avoid=(want_a, settled))
        c1 = counters(vnc, log.name)
        skipped, copies = c1["skipped"] - c0["skipped"], c1["copies"] - c0["copies"]
        reply_max = c1["reply_max_us"]
        thumb = bar_at(shot(conn, out)[2], TW)
        full = bar_at(shot(conn, out, W, H)[2], W)
        c2 = counters(vnc, log.name)
        ok = a == want_a and skipped >= 1 and copies == 0 and thumb == want_b and full == want_b
        ok = ok and c2["reply_max_us"] == reply_max
        print(f"after a client, {frames} frames: with the client the bar {a} (A: {want_a}); "
              f"{skipped} updates skipped while idle, {copies} copied; then the bar {thumb} in "
              f"the thumbnail and {full} at 1280x720 (B: {want_b}); QEMU's longest wait "
              f"{c2['reply_max_us']} us (before {reply_max}): {'pass' if ok else 'FAIL'}")  # fmt: skip
        if not ok:
            fail("a Screenshot after the client left shows an old frame")
    finally:
        if client:
            client.close()
        if conn:
            conn.close_sync(None)
        if vnc and vnc.poll() is None:
            vnc.send_signal(signal.SIGTERM)
            vnc.wait(timeout=10)
        if fake.poll() is None:
            fake.send_signal(signal.SIGTERM)
            fake.wait(timeout=10)
        subprocess.run(["rm", "-rf", d])


def check_idle_dmabuf(frames):
    """A GL guest whose first frame comes after beanjar-vnc's first render, with no client
    (memory frames, or GBM frames): its updates are acknowledged without a copy, and a
    Screenshot must still give the picture, at 384x216 (beanjar-vnc's thumbnail) and at the desktop's size (the
    core's). Skipped without a render node or libgbm (fake-qemu exits 77)."""
    d = tempfile.mkdtemp(prefix="check-thumbnail-dmabuf-")
    fake = subprocess.Popen(
        [sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), d, "--size", f"{W}x{H}",
         "--dmabuf", "--rate", "30"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    vnc = conn = None
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(d, "ready")):
                break
            if fake.poll() == 77:
                print("idle GL guest: skipped (no render node or libgbm)")
                return
            time.sleep(0.05)
        else:
            fail("fake-qemu --dmabuf did not start")
        address = f"unix:path={d}/dbus.sock"
        log = open(os.path.join(d, "vnc.log"), "w")
        vnc = subprocess.Popen(
            [VNC, address, "--listen", loopback.ADDRESS, "--port", str(loopback.port()),
             "--size", f"{W}x{H}", "--fps", "30", "--no-media", "--native-cursor",
             "--frames", frames, "--local", os.path.join(d, "rfb.sock")],
            stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        for _ in range(100):
            if "beanjar-vnc: ready" in open(log.name).read():
                break
            if vnc.poll() is not None:
                fail(f"beanjar-vnc exited: {open(log.name).read()}")
            time.sleep(0.1)
        conn = bus(address)
        for _ in range(100):
            if owned(conn):
                break
            time.sleep(0.05)
        # the guest's frames from now on, after the first render: acknowledged, not copied
        open(os.path.join(d, "animate"), "w").close()
        time.sleep(0.5)
        c = counters(vnc, log.name)
        if c["skipped"] < 5 or c["copies"]:
            fail(f"the GL guest's updates were not skipped while idle: {c}")
        out = os.path.join(d, "shot.ppm")
        try:
            w, h, px, _, ms = shot(conn, out)
            fw, fh, full, _, _ = shot(conn, out, W, H)
        except GLib.Error as e:
            fail(f"a Screenshot of an idle GL guest: {e.message}")
        why = (w, h) != (TW, TH) and f"{w}x{h}" or (fw, fh) != (W, H) and f"{fw}x{fh}"
        if not why:
            # the box's middle white, the left edge at mid height blue, and one bar cyan in the
            # top eighth over blue in the bottom one: upright, as a real GL guest's
            if numpy.abs(px[TH // 2, TW // 2] - 255).max() > 6:
                why = f"the box's middle is {px[TH // 2, TW // 2]}"
            elif numpy.abs(px[TH // 2, 4] - (32, 128, 192)).max() > 6:
                why = f"the left edge is {px[TH // 2, 4]}"
            elif bar_at(px, TW) is None:
                why = f"no bar at the thumbnail's top: {px[TH // 16, :: TW // 8].tolist()}"
            elif bar_at(full, W) is None:
                why = f"no bar at the screenshot's top: {full[H // 16, :: W // 8].tolist()}"
        print(f"idle GL guest, {frames} frames: {c['skipped']} updates skipped, no copy; a {w}x{h} thumbnail in "
              f"{ms:.1f} ms and a {fw}x{fh} screenshot; {why or 'the picture'}: "
              f"{'pass' if not why else 'FAIL'}")  # fmt: skip
        if why:
            fail("a Screenshot of an idle GL guest")
    finally:
        if conn:
            conn.close_sync(None)
        if vnc and vnc.poll() is None:
            vnc.send_signal(signal.SIGTERM)
            vnc.wait(timeout=10)
        if fake.poll() is None:
            fake.send_signal(signal.SIGTERM)
            fake.wait(timeout=10)
        subprocess.run(["rm", "-rf", d])


def main():
    d = tempfile.mkdtemp(prefix="check-thumbnail-")
    fake = subprocess.Popen(
        [sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), d, "--size", f"{W}x{H}",
         "--animate"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    vnc = None
    client = None
    conn = None
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(d, "ready")):
                break
            time.sleep(0.05)
        else:
            fail("fake-qemu did not start")
        address = f"unix:path={d}/dbus.sock"
        log = open(os.path.join(d, "vnc.log"), "w")
        vnc = subprocess.Popen(
            [VNC, address, "--listen", loopback.ADDRESS, "--port", str(loopback.port()),
             "--size", f"{W}x{H}", "--fps", "30", "--no-media", "--native-cursor",
             "--local", os.path.join(d, "rfb.sock")],
            stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        for _ in range(100):
            if "beanjar-vnc: ready" in open(log.name).read():
                break
            if vnc.poll() is not None:
                fail(f"beanjar-vnc exited: {open(log.name).read()}")
            time.sleep(0.1)
        conn = bus(address)
        for _ in range(100):
            if owned(conn):
                break
            time.sleep(0.05)
        else:
            fail(f"nobody owns {NAME}")
        out = os.path.join(d, "thumb.ppm")

        # 1. idle
        c0 = counters(vnc, log.name)
        w, h, px, first, ms = shot(conn, out)
        c1 = counters(vnc, log.name)
        why = (w, h) != (TW, TH) and f"{w}x{h}" or picture_ok(px)
        made = c1["thumbnails"] - c0["thumbnails"]
        ok = not why and made == 1
        print(f"idle: {w}x{h} in {ms:.1f} ms, {made} made, {why or 'the picture'}: "
              f"{'pass' if ok else 'FAIL'}")  # fmt: skip
        if not ok:
            fail("the idle thumbnail")

        # 2. nothing changed
        w, h, px, again, ms = shot(conn, out)
        c2 = counters(vnc, log.name)
        made, cached = c2["thumbnails"] - c1["thumbnails"], c2["thumbnails_cached"] - c1[
            "thumbnails_cached"]  # fmt: skip
        ok = again == first and made == 0 and cached == 1
        print(
            f"unchanged: {ms:.1f} ms, {made} made, {cached} from the cache, "
            f"{'the same bytes' if again == first else 'other bytes'}: {'pass' if ok else 'FAIL'}"
        )
        if not ok:
            fail("the cached thumbnail")

        # 3. the top quarter changing
        open(os.path.join(d, "animate"), "w").close()
        time.sleep(0.5)
        w, h, px, moving, ms = shot(conn, out)
        c3 = counters(vnc, log.name)
        made = c3["thumbnails"] - c2["thumbnails"]
        top = px[2 : TH // 4 - 2]
        grey = numpy.abs(top[..., 0] - top[..., 2]).max()
        std = float(top[..., 0].std())
        why = picture_ok(px, range(TH // 4 + 2, TH))
        ok = not why and made == 1 and moving != first and grey <= 2 and std < 30
        print(f"moving: {ms:.1f} ms, {made} made, the top quarter grey (channels within {grey}), "
              f"its standard deviation {std:.1f}; {why or 'the rest the picture'}: "
              f"{'pass' if ok else 'FAIL'}")  # fmt: skip
        if not ok:
            fail("the thumbnail of a changing picture")

        # stopped after updates skipped while idle: made once more, then from the cache
        time.sleep(0.5)
        os.unlink(os.path.join(d, "animate"))
        time.sleep(0.3)
        c4 = counters(vnc, log.name)
        stopped = shot(conn, out)[3]
        still = shot(conn, out)[3]
        c5 = counters(vnc, log.name)
        made = c5["thumbnails"] - c4["thumbnails"]
        cached = c5["thumbnails_cached"] - c4["thumbnails_cached"]
        ok = made == 1 and cached == 1 and still == stopped
        print(f"stopped: {made} made, then {cached} from the cache: {'pass' if ok else 'FAIL'}")
        if not ok:
            fail("the thumbnail after the picture stopped")
        open(os.path.join(d, "animate"), "w").close()

        # 4. with an RFB client: from the composed frames
        client = Rfb(os.path.join(d, "rfb.sock"), RAW)
        time.sleep(0.5)
        r0 = counters(vnc, log.name)
        times = []
        for _ in range(5):
            w, h, px, data, ms = shot(conn, out)
            times.append(ms)
            time.sleep(0.1)
        r1 = counters(vnc, log.name)
        made = r1["thumbnails"] - r0["thumbnails"]
        why = picture_ok(px, range(TH // 4 + 2, TH))
        frames = r1["renders"] - r0["renders"]
        ok = not why and made >= 4 and frames >= 10
        print(f"with a client: {made} of 5 made while {frames} frames were composed, "
              f"{min(times):.1f} to {max(times):.1f} ms; {why or 'the picture'}: "
              f"{'pass' if ok else 'FAIL'}")  # fmt: skip
        if not ok:
            fail("the thumbnail beside a client")
        os.unlink(os.path.join(d, "animate"))

        # 5. at the desktop's size: the core's screenshot
        w, h, px, data, ms = shot(conn, os.path.join(d, "full.ppm"), W, H)
        r2 = counters(vnc, log.name)
        made = r2["thumbnails"] - r1["thumbnails"]
        ok = (w, h) == (W, H) and made == 0
        print(f"full size: {w}x{h}, {made} thumbnails made: {'pass' if ok else 'FAIL'}")
        if not ok:
            fail("the full-size screenshot")
        print(
            f"cost: {r2['thumbnail_us'] / max(r2['thumbnails'], 1) / 1000:.2f} ms a thumbnail "
            f"made in beanjar-vnc ({r2['thumbnails']} made, {r2['thumbnails_cached']} cached)"
        )

        # 6. beanjar-vnc gone: the manager's fallback
        client.close()
        client = None
        vnc.send_signal(signal.SIGTERM)
        vnc.wait(timeout=10)
        vnc = None
        for _ in range(100):
            if not owned(conn):
                break
            time.sleep(0.05)
        else:
            fail(f"{NAME} still owned after beanjar-vnc exited")
        headless = os.path.join(d, "headless.ppm")
        r = subprocess.run(
            [HEADLESS, address, "--headless", "--no-audio", "--size", f"{TW}x{TH}", "--screenshot",
             headless, "--screenshot-after", "300", "--seconds", "20"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=dict(os.environ, SDL_VIDEODRIVER="offscreen"), timeout=40,
        )  # fmt: skip
        if r.returncode or not os.path.isfile(headless):
            fail(f"the headless viewer: {r.stderr.decode(errors='replace')[-300:]}")
        w, h, px, data = read_ppm(headless)
        ok = (w, h) == (TW, TH)
        print(f"without beanjar-vnc: {NAME} has no owner; the headless viewer's {w}x{h}: "
              f"{'pass' if ok else 'FAIL'}")  # fmt: skip
        if not ok:
            fail("the fallback")
    finally:
        if client:
            client.close()
        if conn:
            conn.close_sync(None)
        if vnc and vnc.poll() is None:
            vnc.send_signal(signal.SIGTERM)
            vnc.wait(timeout=10)
        fake.send_signal(signal.SIGTERM)
        fake.wait(timeout=10)
        subprocess.run(["rm", "-rf", d])
    for frames in ("memory", "gbm"):
        check_idle_dmabuf(frames)
        check_idle_after_client(frames)
    print("ok")


if __name__ == "__main__":
    main()
