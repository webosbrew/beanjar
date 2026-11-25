#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A shown video window without a decoded picture: black while decoding is on, as on a TV; the
checkerboard only with decoding off (host/viewer/core/video.c).

    check-video-plane.py VIEWER_HEADLESS

Against tests/fakes/fake-qemu.py and a stand-in webos.video port (one shown MAIN window on VDEC 0
at 480,270 960x540), viewer-headless --no-osd takes one screenshot per case:

  - decoding on, no helper connected yet: black;
  - decoding on, a helper connected that decodes (its ACK without decoder-unavailable): black;
  - --no-media: the checkerboard;
  - vm-info.toml with media = "off" (a launch without the helper, MEDIAHUB=0): the checkerboard;
  - a helper that reports decoder-unavailable (built without FFmpeg): the checkerboard.

Every case must also have drawn the window (video_draws > 0) without a picture (video_pictures 0)."""

import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIGHT, BLUE = (217, 217, 217), (31, 31, 102)  # video.c's squares
HM_MAGIC, HM_VERSION, HM_ACK, HM_DECODER_UNAVAILABLE = 0x31444D57, 1, 2, 16
HM_SIZE = 560  # include/host-media.h


def video_packet():
    """One WOSVID3 frame (include/webos-video.h): screen 1920x1080, MAIN shown on VDEC port 0."""
    ctx = (
        b"MAIN".ljust(48, b"\0")
        + struct.pack("<IIIII", 1, 1, 0, 1, 0)  # registered connected av_mute has_rect full_screen
        + struct.pack("<iiII", 480, 270, 960, 540)
        + struct.pack("<IIIIII", 1920, 1080, 30000, 0, 3, 0)  # stream, fps, scan, input VDEC 0
        + struct.pack("<IIII", 0, 0, 0, 0)  # no crop: the whole source
    )
    contexts = ctx + b"\0" * (124 * 7)
    head = b"WOSVID3\0" + struct.pack("<IIIIIII", 1028, 1, 1, 0, 1920, 1080, 1)
    return head + contexts


def serve(sock, handler, stop):
    sock.settimeout(0.1)
    conns = []
    while not stop.is_set():
        try:
            c, _ = sock.accept()
        except socket.timeout:
            continue
        conns.append(c)
        threading.Thread(target=handler, args=(c,), daemon=True).start()
    for c in conns:
        c.close()


def video_handler(c):
    try:
        c.sendall(video_packet())
    except OSError:
        pass


def helper_handler(flags):
    def handle(c):
        try:
            hello = c.recv(HM_SIZE)
            if len(hello) != HM_SIZE:
                return
            ack = struct.pack("<IIII", HM_MAGIC, HM_VERSION, HM_ACK, flags).ljust(HM_SIZE, b"\0")
            c.send(ack)
            while c.recv(HM_SIZE):
                pass
        except OSError:
            pass

    return handle


def read_ppm(path):
    with open(path, "rb") as f:
        data = f.read()
    m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255\s", data)
    w, h = int(m.group(1)), int(m.group(2))
    return w, h, data[m.end() :]


def window_shares(path):
    """(black, checkerboard) shares of the window's pixels (480,270 960x540 of 1920x1080)."""
    w, h, px = read_ppm(path)
    x0, y0, x1, y1 = w // 4 + 2, h // 4 + 2, w * 3 // 4 - 2, h * 3 // 4 - 2
    n = black = pattern = 0
    for y in range(y0, y1, 2):
        for x in range(x0, x1, 2):
            p = px[(y * w + x) * 3 : (y * w + x) * 3 + 3]
            n += 1
            black += max(p) < 8
            pattern += any(all(abs(p[i] - c[i]) <= 12 for i in range(3)) for c in (LIGHT, BLUE))
    return black / n, pattern / n


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    viewer = sys.argv[1]
    tmp = tempfile.mkdtemp(prefix="check-video-plane-")
    run = os.path.join(tmp, "run")
    qemu = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "..", "..", "fakes", "fake-qemu.py"), run]
    )
    stop = threading.Event()
    threads = []
    failed = []
    try:
        for _ in range(200):
            if os.path.exists(os.path.join(run, "ready")):
                break
            time.sleep(0.05)
        else:
            print("FAIL: fake-qemu did not start")
            return 1
        info = open(os.path.join(run, "vm-info.toml")).read()
        vs = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        vs.bind(os.path.join(run, "video.sock"))
        vs.listen(4)
        threads.append(threading.Thread(target=serve, args=(vs, video_handler, stop), daemon=True))
        threads[-1].start()
        cases = (
            ("decoding on, no helper yet", [], None, None, "black"),
            ("decoding on, helper decodes", [], None, 0, "black"),
            ("--no-media", ["--no-media"], None, None, "checkerboard"),
            ("vm-info media = off", [], 'media = "off"\n', None, "checkerboard"),
            ("helper without a decoder", [], None, HM_DECODER_UNAVAILABLE, "checkerboard"),
        )
        for n, (what, extra, prepend, helper, want) in enumerate(cases):
            with open(os.path.join(run, "vm-info.toml"), "w") as f:
                f.write((prepend or "") + info)
            hs = hstop = None
            if helper is not None:
                hs = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                hpath = os.path.join(run, "media-0.sock")
                hs.bind(hpath)
                hs.listen(2)
                hstop = threading.Event()
                t = threading.Thread(
                    target=serve, args=(hs, helper_handler(helper), hstop), daemon=True
                )
                t.start()
            shot = os.path.join(tmp, f"shot-{n}.ppm")
            r = subprocess.run(
                [
                    viewer,
                    f"unix:path={os.path.join(run, 'dbus.sock')}",
                    "--seconds",
                    "2",
                    "--no-audio",
                    "--no-osd",
                    "--no-cursor",
                    "--size",
                    "640x360",
                    "--screenshot",
                    shot,
                    *extra,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
                env=dict(os.environ, SDL_VIDEODRIVER="offscreen"),
            )
            if hstop:
                hstop.set()
                t.join()
                hs.close()
                os.unlink(hpath)
            m = re.search(r"video_draws=(\d+) video_pictures=(\d+)", r.stdout)
            if r.returncode or not m or not os.path.exists(shot):
                failed.append(f"{what}: viewer exit {r.returncode}: {r.stdout[-2000:]}")
                continue
            draws, pictures = int(m.group(1)), int(m.group(2))
            black, pattern = window_shares(shot)
            got = "black" if black > 0.99 else "checkerboard" if pattern > 0.95 else "other"
            print(
                f"{what}: window black {black:.3f}, checkerboard {pattern:.3f} ({got}); "
                f"draws {draws}, pictures {pictures}"
            )
            if got != want or not draws or pictures:
                failed.append(f"{what}: want {want}, got {got}, draws {draws}, pictures {pictures}")
            if want == "checkerboard" and prepend and "decoding off" not in r.stdout:
                failed.append(f"{what}: no 'decoding off' line in the viewer's output")
    finally:
        stop.set()
        for t in threads:
            t.join()
        qemu.terminate()
        try:
            qemu.wait(10)
        except subprocess.TimeoutExpired:
            qemu.kill()
            qemu.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    for f in failed:
        print(f"FAIL: {f}")
    if failed:
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
