#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar-vnc's --video-local (host/vnc/video.h) against the stand-in QEMU display
(tests/fakes/fake-qemu.py --animate), no VM, no window, no GPU (x264).

    check-video.py BEANJAR_VNC

1. The socket is owner-only (0600) and named in a "listening" line.
2. "h264 bitrate=1500" gets the header (640x360, the fps, H.264, the bitrate, "x264"), then a
   keyframe first, with SPS, PPS and an IDR slice. A still picture costs little: a few units in
   2.5 s (at least one a second). ffmpeg decodes every unit to a 640x360 frame with fake-qemu's
   picture (the white box in the middle, its blue around it).
3. A busy picture (the noise band) is encoded at about the frame rate, every unit decoded.
4. A second client's stream starts with a keyframe, forced at once: the first client gets one at
   the same time.
5. A client that stops reading for 1.5 s at 20 Mbit/s loses units, then resumes at a keyframe:
   its stream decodes without an error.
6. A client that never reads holds nobody up and is disconnected after 3 s.
7. A bad request is refused; with the last client gone the encoder is closed. SIGUSR1 prints
   the counters (video units, renders) and the run goes on. SIGTERM removes
   the socket, and the counters add up.
8. NVENC (--video-encoder nvenc), where it opens (skipped otherwise): its header and a decodable
   stream with the same picture.
9. VA-API (--video-encoder vaapi; on WSL with --vaapi-adapter $BEANJAR_TEST_VAAPI_ADAPTER when
   set, e.g. NVIDIA), where it opens (skipped otherwise): the same.
10. Colours, at an odd size (643x361, coded 642x360): fake-qemu's colour bars (--bars) through
   x264 and VA-API (where it opens), each with RGB to NV12 on the GPU (the default; the log says
   so and frames are converted) and with swscale (BEANJAR_VNC_SWSCALE=1, the fallback). The
   decoded Y, U and V in the middle of each bar are within COLOUR_TOLERANCE code values of the
   bar's colour in BT.601 limited range; the largest difference is printed for each run.
"""

import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import loopback  # noqa: E402
import videostream as vs  # noqa: E402

VNC = sys.argv[1]
W, H, FPS = 640, 360, 30
BLUE, WHITE = (32, 128, 192), (255, 255, 255)


def fail(message):
    sys.exit(f"check-video: {message}")


class Client:
    """A video client reading in a thread of its own; pause() stops it reading for a while."""

    def __init__(self, path, request=b"h264 bitrate=1500\n"):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
        self.sock.sendall(request)
        self.data = bytearray()
        self.arrivals = []  # (monotonic time, bytes so far)
        self.paused = threading.Event()
        self.paused.set()
        self.closed = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        self.sock.settimeout(0.2)
        while not self.closed:
            self.paused.wait()
            try:
                chunk = self.sock.recv(1 << 16)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            self.data += chunk
            self.arrivals.append((time.monotonic(), len(self.data)))
        self.closed = True

    def pause(self, seconds):
        self.paused.clear()
        time.sleep(seconds)
        self.paused.set()

    def stop(self):
        self.closed = True
        self.thread.join(5)
        self.sock.close()

    def stream(self):
        """(header fields, [(key, ts, au)])"""
        try:
            info, body = vs.header(bytes(self.data))
        except ValueError as e:
            fail(str(e))
        return info, vs.units(body)[0]


def check_picture(frames, what):
    for i, f in enumerate(frames):
        mid, corner = vs.pixel(f, W, W // 2, H // 2), vs.pixel(f, W, W - 8, H - 8)
        if not (vs.close_to(mid, WHITE) and vs.close_to(corner, BLUE)):
            fail(f"{what}: frame {i} has {mid} in the middle, {corner} in a corner")


def decoded(units, what, picture=True):
    stream = b"".join(au for _k, _t, au in units)
    frames, errors = vs.decode(stream, W, H)
    if errors:
        fail(f"{what}: ffmpeg: {errors}")
    if len(frames) != len(units):
        fail(f"{what}: {len(units)} units decoded to {len(frames)} frames")
    if picture:
        check_picture(frames, what)
    return frames


def start(fake_dir, sock, extra=(), size=(W, H), env=None):
    log = open(os.path.join(fake_dir, f"vnc-{len(os.listdir(fake_dir))}.log"), "w")
    p = subprocess.Popen(
        [VNC, f"unix:path={fake_dir}/dbus.sock", "--listen", loopback.ADDRESS, "--port",
         str(loopback.port()), "--size", f"{size[0]}x{size[1]}", "--fps", str(FPS), "--no-media",
         "--native-cursor", "--video-local", sock, *extra],
        stdout=log, stderr=subprocess.STDOUT, env=env,
    )  # fmt: skip
    for _ in range(100):
        text = open(log.name).read()
        if "beanjar-vnc: ready" in text:
            return p, log, text
        if p.poll() is not None:
            fail(f"beanjar-vnc exited {p.returncode}: {text}")
        time.sleep(0.1)
    fail(f"beanjar-vnc not ready: {open(log.name).read()}")


def animate(d, on):
    path = os.path.join(d, "animate")
    if on:
        open(path, "w").close()
    elif os.path.exists(path):
        os.unlink(path)


def check_x264(d):
    sock = os.path.join(d, "video-local.sock")
    vnc, log, text = start(d, sock, ("--video-encoder", "x264"))
    try:
        if f"listening unix:{sock} (video, h264" not in text:
            fail(f"no video listening line: {text}")
        mode = os.stat(sock).st_mode & 0o777
        if mode != 0o600:
            fail(f"the video socket has mode {mode:o}, not 600")
        time.sleep(0.5)

        # 2. a still picture
        c = Client(sock)
        time.sleep(2.5)
        c.stop()
        info, units = c.stream()
        if info != (W, H, FPS, 1, 1500, "x264"):
            fail(f"header {info}")
        if not units or not units[0][0]:
            fail("the first unit is not a keyframe")
        types = vs.nal_types(units[0][2])
        if not {7, 8, 5} <= set(types):
            fail(f"the first keyframe's NAL units are {types}, not SPS, PPS and IDR")
        if not 2 <= len(units) <= 10:
            fail(f"a still picture gave {len(units)} units in 2.5 s")
        codec = vs.codec_string(units[0][2])
        decoded(units, "still")
        print(f"still: header {info}, keyframe first ({codec}), {len(units)} units in 2.5 s, "
              f"each decoded to fake-qemu's picture")  # fmt: skip

        # 3. a busy one
        animate(d, True)
        time.sleep(0.3)
        c = Client(sock)
        time.sleep(2)
        c.stop()
        _info, units = c.stream()
        span = (units[-1][1] - units[0][1]) / 1e6 if len(units) > 1 else 0
        rate = (len(units) - 1) / span if span else 0
        if rate < FPS * 0.6:
            fail(f"a busy picture gave {len(units)} units, {rate:.1f} a second")
        decoded(units, "busy")
        kbit = sum(len(au) for *_x, au in units) * 8 / span / 1000
        print(f"busy: {len(units)} units, {rate:.1f} a second, {kbit:.0f} kbit/s, all decoded")

        # 4. a second client: a keyframe for it, forced at once (the first sees it too)
        a = Client(sock)
        time.sleep(1.2)
        joined = time.monotonic()
        b = Client(sock)
        time.sleep(1.0)
        b.stop()
        a.stop()
        _i, ua = a.stream()
        _i, ub = b.stream()
        if not ub or not ub[0][0]:
            fail("the second client's first unit is not a keyframe")
        if sum(1 for k, *_r in ua if k) < 2 or ub[0][1] not in [t for k, t, _au in ua if k]:
            fail("the first client did not get the keyframe forced for the second")
        first = next(t for t, n in b.arrivals if n > vs.HEADER)
        if first - joined > 0.5:
            fail(f"the second client's keyframe came after {first - joined:.2f} s")
        decoded(ua, "first client")
        decoded(ub, "second client")
        print(f"second client: keyframe after {(first - joined) * 1000:.0f} ms, forced for both")

        # 5. a client that falls behind: units dropped up to a keyframe, the stream decodable
        c = Client(sock, b"h264 bitrate=20000\n")
        time.sleep(0.8)
        c.pause(1.5)
        time.sleep(1.5)
        c.stop()
        _i, units = c.stream()
        gaps = [(i, units[i][1] - units[i - 1][1]) for i in range(1, len(units))]
        i, gap = max(gaps, key=lambda g: g[1])
        if gap < 400000 or not units[i][0]:
            fail(f"no gap at a keyframe in the slow client's stream (largest {gap / 1000:.0f} ms, "
                 f"keyframe {units[i][0]})")  # fmt: skip
        decoded(units, "slow client")
        print(
            f"slow client: {gap / 1000:.0f} ms of units dropped, resumed at a keyframe, decodable"
        )

        # 6. a client that never reads, beside one that does
        stalled = socket.socket(socket.AF_UNIX)
        stalled.connect(sock)
        stalled.sendall(b"h264 bitrate=20000\n")
        c = Client(sock, b"h264 bitrate=20000\n")
        time.sleep(4.5)
        c.stop()
        stalled.settimeout(5)
        got, end = 0, time.monotonic() + 5
        while time.monotonic() < end:
            chunk = stalled.recv(1 << 16)
            if not chunk:
                break
            got += len(chunk)
        else:
            fail("the stalled video client was not disconnected")
        stalled.close()
        _i, units = c.stream()
        span = (units[-1][1] - units[0][1]) / 1e6
        if (len(units) - 1) / span < FPS * 0.6:
            fail(f"the reader beside a stalled client got {len(units)} units in {span:.1f} s")
        decoded(units, "beside the stalled client", picture=False)
        print(f"stalled client: disconnected ({got} bytes were queued); the other got "
              f"{len(units)} units")  # fmt: skip
        animate(d, False)

        # 7. bad requests; the encoder closed with the last client
        for bad in (b"vp8\n", b"jpeg quality=70\n", b"h264 bitrate=5\n", b"h264 fast\n"):
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(sock)
                s.sendall(bad)
                s.settimeout(5)
                if s.recv(64):
                    fail(f"the bad request {bad!r} got an answer")
        time.sleep(0.5)
        text = open(log.name).read()
        if not text.rstrip().endswith(REFUSAL):
            fail(f"bad requests: {text[-400:]}")
        if text.count("video encoder closed: no clients") < 5:
            fail("the encoder is not closed when the last client leaves")
        print("requests: bad ones refused; the encoder closed with the last client")

        # SIGUSR1: the counters on one line, the run goes on
        vnc.send_signal(signal.SIGUSR1)
        time.sleep(0.5)
        stats = [x for x in open(log.name).read().splitlines() if "beanjar-vnc: stats " in x]
        fields = dict(f.split("=", 1) for f in stats[-1].split()[2:]) if stats else {}
        if vnc.poll() is not None or not fields or int(fields.get("video_units", 0)) < 1:
            fail(f"SIGUSR1 stats: {stats[-1:] or 'no line'}")
        if int(fields["renders"]) < 1 or int(fields["t_us"]) < 1:
            fail(f"SIGUSR1 stats: {stats[-1]}")
        print(stats[-1])

        vnc.send_signal(signal.SIGTERM)
        rc = vnc.wait(timeout=10)
        log.close()
        text = open(log.name).read()
        if rc != 0 or os.path.exists(sock):
            fail(
                f"beanjar-vnc exited {rc}, the socket {'left' if os.path.exists(sock) else 'gone'}"
            )
        line = next((x for x in text.splitlines() if "beanjar-vnc: video clients=" in x), "")
        if not line.startswith("beanjar-vnc: video clients=7 refused=4 encoder=x264") or (
            "slow_disconnects=1" not in line or " dropped=0 " in line
        ):
            fail(f"video counters: {line!r}")
        print(line)
        vnc = None
    finally:
        animate(d, False)
        if vnc and vnc.poll() is None:
            vnc.kill()
            vnc.wait()


REFUSAL = "beanjar-vnc: video client refused: not 'h264' with bitrate=N (kbit/s, 100 to 50000)"


def check_gpu(d, name, extra=()):
    """A GPU encoder's stream (nvenc, vaapi), or skipped where it does not open."""
    codec = f"h264_{name}"
    sock = os.path.join(d, f"video-{name}.sock")
    vnc, log, _text = start(d, sock, ("--video-encoder", name, *extra))
    try:
        time.sleep(0.5)
        c = Client(sock, b"h264\n")
        # NVENC's first open (CUDA's start) can take seconds on a loaded machine
        end = time.monotonic() + 15
        while time.monotonic() < end and len(c.data) < vs.HEADER + 3 * vs.UNIT_HEAD:
            if c.closed or f"video: {codec}:" in open(log.name).read():
                break
            time.sleep(0.1)
        animate(d, True)  # a busy picture too
        time.sleep(1)
        animate(d, False)
        c.stop()
        if not c.data:
            text = open(log.name).read()
            if f"video: {codec}:" in text or f"no {codec}" in text:
                reason = [x for x in text.splitlines() if codec in x][-1]
                print(f"{name}: skipped, it does not open here ({reason.split(': ', 2)[-1]})")
                return
            fail(f"{name}: no stream and no reason: {text}")
        info, units = c.stream()
        if info != (W, H, FPS, 1, 10000, name):
            fail(f"{name} header {info}")
        if not units or not units[0][0] or not {7, 8, 5} <= set(vs.nal_types(units[0][2])):
            fail(f"{name}: the first unit is not a keyframe with SPS and PPS")
        decoded(units, name)
        print(f"{name}: header {info}, {vs.codec_string(units[0][2])}, {len(units)} units decoded "
              f"to fake-qemu's picture")  # fmt: skip
    finally:
        vnc.send_signal(signal.SIGTERM)
        vnc.wait(timeout=10)
        log.close()


# fake-qemu's --bars (tests/fakes/fake-qemu.py BARS), at an odd size
BARS = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (255, 0, 0),
        (0, 0, 255), (32, 128, 192), (200, 60, 30), (16, 16, 16)]  # fmt: skip
BARS_SIZE = (643, 361)  # coded 642x360: NV12 rows of 644 bytes, padded
COLOUR_TOLERANCE = 3  # code values: the conversion's rounding and the encoder's on a flat area


def bt601(rgb):
    """Y, U, V of an RGB colour: BT.601, limited range (host/vnc/yuv.c's and swscale's)."""
    r, g, b = (c / 255 for c in rgb)
    return (16 + 65.481 * r + 128.553 * g + 24.966 * b,
            128 - 37.797 * r - 74.203 * g + 112.0 * b,
            128 + 112.0 * r - 93.786 * g - 18.214 * b)  # fmt: skip


def bar_errors(frame, w, h):
    """The largest |decoded - expected| of Y, U, V over the bars' middles (8x8 Y, 4x4 chroma)."""
    bar = BARS_SIZE[0] // len(BARS)
    u0, v0 = w * h, w * h + (w // 2) * (h // 2)
    worst = [0.0, 0.0, 0.0]
    for i, colour in enumerate(BARS):
        cx, cy = bar * i + bar // 2, h // 2
        ys = [frame[(cy + j) * w + cx + k] for j in range(-4, 4) for k in range(-4, 4)]
        cs = [((cy // 2 + j) * (w // 2) + cx // 2 + k) for j in range(-2, 2) for k in range(-2, 2)]
        got = (sum(ys) / len(ys), sum(frame[u0 + o] for o in cs) / len(cs),
               sum(frame[v0 + o] for o in cs) / len(cs))  # fmt: skip
        for p, (g, e) in enumerate(zip(got, bt601(colour))):
            worst[p] = max(worst[p], abs(g - e))
    return worst


def check_colours(top):
    """10. the colours of both conversions, x264 and VA-API (where it opens), at an odd size."""
    d = tempfile.mkdtemp(prefix="check-video-bars-")
    w, h = BARS_SIZE[0] & ~1, BARS_SIZE[1] & ~1
    fake = subprocess.Popen(
        [sys.executable, os.path.join(top, "tests/fakes/fake-qemu.py"), d, "--size",
         f"{BARS_SIZE[0]}x{BARS_SIZE[1]}", "--bars"]
    )  # fmt: skip
    failures = []
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(d, "ready")):
                break
            time.sleep(0.05)
        else:
            fail("fake-qemu --bars did not start")
        adapter = os.environ.get("BEANJAR_TEST_VAAPI_ADAPTER")
        for encoder in ("x264", "vaapi"):
            extra = ["--video-encoder", encoder]
            if encoder == "vaapi" and adapter:
                extra += ["--vaapi-adapter", adapter]
            for path in ("gpu", "swscale"):
                env = dict(os.environ)
                env.pop("BEANJAR_VNC_SWSCALE", None)
                if path == "swscale":
                    env["BEANJAR_VNC_SWSCALE"] = "1"
                sock = os.path.join(d, f"video-{encoder}-{path}.sock")
                vnc, log, _text = start(d, sock, extra, BARS_SIZE, env)
                try:
                    c = Client(sock, b"h264 bitrate=5000\n")
                    time.sleep(2.0)
                    c.stop()
                finally:
                    vnc.send_signal(signal.SIGTERM)
                    vnc.wait(timeout=10)
                    log.close()
                text = open(log.name).read()
                if not c.data and encoder == "vaapi":
                    reason = [x for x in text.splitlines() if "h264_vaapi" in x][-1:]
                    print(f"colours {encoder} {path}: skipped, it does not open here "
                          f"({reason[0].split(': ', 2)[-1] if reason else 'no stream'})")  # fmt: skip
                    break
                info, units = c.stream()
                if info[:2] != (w, h):
                    failures.append(f"{encoder} {path}: header {info}, not {w}x{h}")
                    continue
                said = {"gpu": "kbit/s, RGB to NV12 on the GPU",
                        "swscale": "kbit/s, RGB to YUV by swscale"}[path]  # fmt: skip
                converted = "frames converted to NV12 on the GPU" in text
                if said not in text or converted != (path == "gpu"):
                    failures.append(f"{encoder} {path}: the log has no '{said}' or the GPU "
                                    f"converted={converted}")  # fmt: skip
                frames, errors = vs.decode_yuv(b"".join(au for *_x, au in units), w, h)
                if errors or not frames:
                    failures.append(f"{encoder} {path}: ffmpeg: {errors or 'no frames'}")
                    continue
                worst = [max(e) for e in zip(*(bar_errors(f, w, h) for f in frames))]
                ok = max(worst) <= COLOUR_TOLERANCE
                print(f"colours {encoder} {path}: {len(frames)} frames {w}x{h}, largest difference "
                      f"Y {worst[0]:.2f} U {worst[1]:.2f} V {worst[2]:.2f} "
                      f"(tolerance {COLOUR_TOLERANCE}): {'pass' if ok else 'FAIL'}")  # fmt: skip
                if not ok:
                    failures.append(f"{encoder} {path}: colours off by {max(worst):.2f}")
    finally:
        fake.send_signal(signal.SIGTERM)
        fake.wait(timeout=10)
        subprocess.run(["rm", "-rf", d])
    if failures:
        fail("colours: " + "; ".join(failures))


def main():
    if not vs.have_ffmpeg():
        print("check-video: no ffmpeg to decode the stream: skipped")
        sys.exit(77)
    probe = subprocess.run([VNC, "--help"], capture_output=True, text=True)
    if "--video-local" not in probe.stdout:
        print("check-video: beanjar-vnc is built without --video-local: skipped")
        sys.exit(77)
    tmp = tempfile.mkdtemp(prefix="check-video-")
    fake = subprocess.Popen(
        [sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), tmp, "--size",
         f"{W}x{H}", "--animate"]
    )  # fmt: skip
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(tmp, "ready")):
                break
            time.sleep(0.05)
        else:
            fail("fake-qemu did not start")
        check_x264(tmp)
        check_gpu(tmp, "nvenc")
        adapter = os.environ.get("BEANJAR_TEST_VAAPI_ADAPTER")
        check_gpu(tmp, "vaapi", ("--vaapi-adapter", adapter) if adapter else ())
    finally:
        fake.send_signal(signal.SIGTERM)
        fake.wait(timeout=10)
        subprocess.run(["rm", "-rf", tmp])
    check_colours(TOP)
    print("ok")


if __name__ == "__main__":
    main()
