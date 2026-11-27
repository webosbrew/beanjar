#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The manager's stdio pipes (host/core/beanjar/display/pipe.py) against beanjar-vnc and the stand-in QEMU
display (tests/fakes/fake-qemu.py), no VM, no window.

    check-pipe.py BEANJAR_VNC

beanjar-vnc serves fake-qemu's display with --local, as beanjar vnc start runs it. A stand-in VM
object (its run directory is fake-qemu's) then goes through:
1. vnc_pipe: an RFB handshake over the relayed fds (security type None alone) and a raw frame with
   fake-qemu's picture; without beanjar-vnc running it says to start it.
2. remote_pipe: "down"/"up" lines by key name and by code become IR records in fake-qemu's log, a
   bad line is answered with an error and the pipe goes on, and the button held at the end of stdin
   is released.
3. audio_pipe: beanjar-vnc runs with --audio-local and fake-qemu with --audio; "pcm" and "opus"
   through the relayed fds get their header and fake-qemu's tone (Opus decoded with libopus), and
   the pipe ends with stdin; a beanjar-vnc without the audio socket is named as predating it.
4. video_pipe: beanjar-vnc runs with --video-local (x264): "h264 bitrate=800" through the
   relayed fds gets the header and a keyframe with SPS and PPS first; the pipe ends with stdin. A
   beanjar-vnc without the video socket is named as predating it. Skipped where beanjar-vnc is
   built without it.
5. press: a key pressed and released; a dimmed key (no IR code) refused.
6. vm_layout and as_json: the viewer's layout for the firmware, as the GTK remote's data.
"""

import io
import os
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
from beanjar import base  # noqa: E402
from beanjar.devices import remote_layouts  # noqa: E402
from beanjar.display import pipe  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import audiostream  # noqa: E402
import loopback  # noqa: E402
import videostream  # noqa: E402

VNC = sys.argv[1]
W, H = 640, 360


def fail(message):
    sys.exit(f"check-pipe: {message}")


class StandInVM:
    """What pipe.py uses of core.VM, over fake-qemu's directory."""

    def __init__(self, d, vnc_pid):
        self.name = "tv"
        self.dbus_sock = os.path.join(d, "dbus.sock")
        self.vnc_local_sock = os.path.join(d, "rfb-local.sock")
        self.audio_local_sock = os.path.join(d, "audio-local.sock")
        self.video_local_sock = os.path.join(d, "video-local.sock")
        self._vnc = vnc_pid

    def pid(self):
        return os.getpid()

    def vnc_pid(self):
        return self._vnc

    def target(self):
        raise base.Error("no firmware here")


def wait_log(path, lines, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with open(path) as f:
            text = f.read().splitlines()
        if text == lines:
            return
        time.sleep(0.1)
    fail(f"input.log is {text}, not {lines}")


def check_vnc_pipe(vm):
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    result = []
    t = threading.Thread(target=lambda: result.append(pipe.vnc_pipe(vm, in_r, out_w)))
    t.start()

    def rx(n):
        b = b""
        while len(b) < n:
            c = os.read(out_r, n - len(b))
            if not c:
                fail(f"vnc pipe closed ({len(b)} of {n} bytes)")
            b += c
        return b

    if not rx(12).startswith(b"RFB 003."):
        fail("no RFB greeting through the pipe")
    os.write(in_w, b"RFB 003.008\n")
    types = list(rx(rx(1)[0]))
    if types != [1]:
        fail(f"the local socket offers {types}, not None alone")
    os.write(in_w, b"\x01")
    if struct.unpack(">I", rx(4))[0] != 0:
        fail("security None refused")
    os.write(in_w, b"\x01")
    w, h = struct.unpack(">HH", rx(4))
    rx(16)
    rx(struct.unpack(">I", rx(4))[0])
    if (w, h) != (W, H):
        fail(f"desktop {w}x{h}")
    os.write(
        in_w,
        struct.pack(">Bxxx", 0)
        + struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16),
    )
    os.write(in_w, struct.pack(">BxHi", 2, 1, 0))  # raw only
    pixels = bytearray(w * h * 4)
    for _ in range(30):
        os.write(in_w, struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
        if rx(1)[0] != 0:
            fail("unexpected server message")
        rx(1)
        for _ in range(struct.unpack(">H", rx(2))[0]):
            x, y, rw, rh, enc = struct.unpack(">HHHHi", rx(12))
            if enc != 0:
                fail(f"encoding {enc}")
            data = rx(rw * rh * 4)
            for row in range(rh):
                o = ((y + row) * w + x) * 4
                pixels[o : o + rw * 4] = data[row * rw * 4 : (row + 1) * rw * 4]
        centre = pixels[((h * 3 // 8 + 2) * w + w * 3 // 8 + 2) * 4 :][:3]  # in the white box
        corner = pixels[((h - 1) * w + w - 1) * 4 :][:3]
        if centre == b"\xff\xff\xff" and corner == bytes((32, 128, 192)):
            break
        time.sleep(0.2)
    else:
        fail(
            f"the frame through the pipe has {centre.hex()} in the centre, {corner.hex()} in a corner"
        )
    os.close(in_w)  # stdin's end: the pipe ends
    t.join(10)
    if t.is_alive() or result != [0]:
        fail(f"vnc_pipe did not end with stdin: {result}")
    for fd in (in_r, out_r, out_w):
        os.close(fd)
    print("vnc pipe: None alone, fake-qemu's picture, ends with stdin")
    try:
        pipe.vnc_pipe(StandInVM(os.path.dirname(vm.dbus_sock), None))
    except base.Error as e:
        if "beanjar vnc start tv" not in str(e):
            fail(f"without beanjar-vnc: {e}")
    else:
        fail("vnc_pipe without beanjar-vnc did not fail")


def check_audio_pipe(vm):
    for fmt in ("pcm", "opus"):
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        result = []
        t = threading.Thread(target=lambda: result.append(pipe.audio_pipe(vm, in_r, out_w)))
        t.start()
        os.write(in_w, fmt.encode() + b"\n")
        data = b""
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            chunk = os.read(out_r, 65536)
            if not chunk:
                fail(f"audio pipe {fmt} closed after {len(data)} bytes")
            data += chunk
        os.close(in_w)  # stdin's end: the pipe ends
        t.join(10)
        if t.is_alive() or result != [0]:
            fail(f"audio_pipe did not end with stdin: {result}")
        for fd in (in_r, out_r, out_w):
            os.close(fd)
        try:
            pcm = audiostream.to_pcm(audiostream.header(data, fmt), fmt)
            print(audiostream.check_tone(pcm, f"audio pipe {fmt}") + ", ends with stdin")
        except ValueError as e:
            fail(str(e))
    old = StandInVM(os.path.dirname(vm.dbus_sock), vm.vnc_pid())
    old.audio_local_sock += ".missing"
    try:
        pipe.audio_pipe(old)
    except base.Error as e:
        if "predates it" not in str(e):
            fail(f"without the audio socket: {e}")
    else:
        fail("audio_pipe without the audio socket did not fail")
    print("audio pipe: a beanjar-vnc without the audio socket is named as predating it")


def check_video_pipe(vm):
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    result = []
    t = threading.Thread(target=lambda: result.append(pipe.video_pipe(vm, in_r, out_w)))
    t.start()
    os.write(in_w, b"h264 bitrate=800\n")
    data = b""
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        chunk = os.read(out_r, 65536)
        if not chunk:
            fail(f"video pipe closed after {len(data)} bytes")
        data += chunk
        if (
            len(data) > videostream.HEADER + videostream.UNIT_HEAD
            and videostream.units(data[videostream.HEADER :])[0]
        ):
            break
    os.close(in_w)  # stdin's end: the pipe ends
    t.join(10)
    if t.is_alive() or result != [0]:
        fail(f"video_pipe did not end with stdin: {result}")
    for fd in (in_r, out_r, out_w):
        os.close(fd)
    try:
        info, body = videostream.header(data)
    except ValueError as e:
        fail(f"video pipe: {e}")
    units, _rest = videostream.units(body)
    if info != (W, H, 60, 1, 800, "x264"):
        fail(f"video pipe: header {info}")
    if not units or not units[0][0] or not {7, 8, 5} <= set(videostream.nal_types(units[0][2])):
        fail("video pipe: the first unit is not a keyframe with SPS and PPS")
    print(f"video pipe: header {info}, a keyframe first, ends with stdin")
    old = StandInVM(os.path.dirname(vm.dbus_sock), vm.vnc_pid())
    old.video_local_sock += ".missing"
    try:
        pipe.video_pipe(old)
    except base.Error as e:
        if "predates it" not in str(e):
            fail(f"without the video socket: {e}")
    else:
        fail("video_pipe without the video socket did not fail")
    print("video pipe: a beanjar-vnc without the video socket is named as predating it")


def check_remote(vm, input_log):
    open(input_log, "w").close()
    out = io.StringIO()
    stdin = io.BytesIO(b"down HOME\nup home\n\nsideways 1\ndown 0x40\n")
    pipe.remote_pipe(vm, stdin, out)
    said = out.getvalue().splitlines()
    if said[0] != "ready" or len(said) != 2 or not said[1].startswith("error: not 'down CODE'"):
        fail(f"remote pipe said {said}")
    wait_log(input_log, ["IR.Down 0x7c", "IR.Up 0x7c", "IR.Down 0x40", "IR.Up 0x40"])
    print("remote pipe: names and codes as IR edges, a bad line answered, the held one released")
    open(input_log, "w").close()
    if pipe.press(vm, ["ENTER"], 0.05) != [0x44]:
        fail("press ENTER is not 0x44")
    wait_log(input_log, ["IR.Down 0x44", "IR.Up 0x44"])
    try:
        pipe.press(vm, ["NETFLIX"])
    except base.Error as e:
        if "sends nothing" not in str(e):
            fail(f"press NETFLIX: {e}")
    else:
        fail("press of a dimmed key did not fail")
    print("remote press: ENTER pressed; a dimmed key refused")


def check_layout(vm):
    layout = pipe.vm_layout(vm)
    if layout is not remote_layouts.NORMAL:
        fail(f"layout {layout.name}, not normal")
    data = remote_layouts.as_json(layout)
    if [(k["name"], k["code"]) for k in data["keys"]] != [(k.name, k.code) for k in layout.keys]:
        fail("as_json's keys are not the layout's")
    netcast = remote_layouts.LAYOUTS[remote_layouts.remote_layout("auto", {"platform": "NetCast"})]
    if netcast.name != "netcast":
        fail("NetCast's platform does not choose the NetCast remote")
    print(f"layout: {data['name']}, {len(data['keys'])} keys as the GTK remote's")


def main():
    tmp = tempfile.mkdtemp(prefix="check-pipe-")
    fake = subprocess.Popen(
        [sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), tmp, "--audio"]
    )
    vnc = None
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(tmp, "ready")):
                break
            time.sleep(0.05)
        else:
            fail("fake-qemu did not start")
        port = loopback.port()
        log = open(os.path.join(tmp, "vnc.log"), "w")
        local = os.path.join(tmp, "rfb-local.sock")
        video = (
            "--video-local"
            in subprocess.run([VNC, "--help"], capture_output=True, text=True).stdout
        )
        extra = ["--video-local", os.path.join(tmp, "video-local.sock"), "--video-encoder", "x264"]
        vnc = subprocess.Popen(
            [VNC, f"unix:path={tmp}/dbus.sock", "--listen", loopback.ADDRESS, "--port", str(port),
             "--local", local, "--audio-local", os.path.join(tmp, "audio-local.sock"),
             "--size", f"{W}x{H}", "--no-media", "--name", "check-pipe", *(extra if video else [])],
            stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        for _ in range(100):
            if "beanjar-vnc: ready" in open(log.name).read():
                break
            if vnc.poll() is not None:
                fail(f"beanjar-vnc exited: {open(log.name).read()}")
            time.sleep(0.1)
        else:
            fail("beanjar-vnc not ready")
        vm = StandInVM(tmp, vnc.pid)
        check_vnc_pipe(vm)
        check_audio_pipe(vm)
        if video:
            check_video_pipe(vm)
        else:
            print("video pipe: beanjar-vnc is built without --video-local: not checked")
        check_remote(vm, os.path.join(tmp, "input.log"))
        check_layout(vm)
    finally:
        if vnc:
            vnc.send_signal(signal.SIGTERM)
            vnc.wait(timeout=10)
        fake.send_signal(signal.SIGTERM)
        fake.wait(timeout=10)
        subprocess.run(["rm", "-rf", tmp])
    print("ok")


if __name__ == "__main__":
    main()
