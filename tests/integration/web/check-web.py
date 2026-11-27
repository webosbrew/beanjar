#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar-web (host/web) end to end, against beanjar-vnc on the stand-in QEMU display
(tests/fakes/fake-qemu.py) and a stand-in manager CLI (fake-beanjar.py, whose pipes are the
manager's own code), with an aiohttp client: no VM, no browser.

    check-web.py BEANJAR_VNC

1. The token: no page without it, a wrong one refused, the right one in the URL becomes a cookie.
2. /api/vms is the manager's list; POST /vm/local/tv/vnc accepts a running beanjar-vnc and passes
   the manager's refusal on (409, its message). Start/stop run against fake state files for
   local and remote VMs: prompt 202, pending operation, duplicate 409 without a second command,
   state changes in the list, refusals (409 with the manager's message), fallback diagnostics;
   POST only, authenticated, cross-site POSTs refused in every mode. Pending CLI processes end
   at shutdown; a restart shows the manager's real state without stale pending metadata.
3. /vm/local/tv/rfb: an RFB handshake in WebSocket binary messages (security type None), and a
   raw frame with fake-qemu's picture; a WebSocket from another origin is refused.
3b. Two shared RFB clients receive pixel updates during each other's connect/disconnect/reconnect,
    with incremental requests and continuous updates. Fixed two-second windows print each
    client's count and longest gap; a smaller SetDesktopSize is refused without changing either
    stream's geometry. The server's resize reply and dimensions are printed too.
4. /vm/local/tv/remote: "ready", button edges as IR records in fake-qemu's log, a bad message
   answered, and a button still held when the socket closes released.
5. /vm/local/tv/audio?format=pcm and ?format=opus: `vnc audio-pipe`'s stream as binary messages,
   the format's header and fake-qemu's tone (Opus decoded with libopus); another format is 400.
5b. /vm/local/tv/video?bitrate=800: `vnc video-pipe`'s stream (x264 here) as binary messages,
   the header and a keyframe with SPS and PPS first, decoded by ffmpeg (when it is there) to
   fake-qemu's picture; a bitrate out of range is 400.
5c. The RFB pause: /rfb?page=P gets no update while /video?page=P plays (fake-qemu --animate),
   with and without continuous updates, keys and pointer still reaching fake-qemu; the whole
   screen when the video ends; another page's video ignored; a bad page id is 400. beanjar-vnc
   is told (the relay's marker on its --local socket) and feeds neatvnc nothing meanwhile
   (vnc_feeds in its "RFB client paused" and "resumed" lines) while the video goes on; the same
   marker over its TCP listener does nothing.
6. layout.json is the GTK remote's data (remote_layouts.as_json); screenshot.png is a 1280x720 PNG;
   the pages, Debian's noVNC and its Bootstrap Icons are served.
7. SIGTERM ends it, with no pipe left running.
8. The token file: made 0600 at the first run, the same token at the second (on `localhost`, a
   host name, which warns of plain HTTP).
9. --auth password: the login page open, the rest sent to it or refused, a wrong password refused
   a second later, the right one a cookie that opens the list and the WebSockets; Log Out.
10. --auth none: no cookie needed on 127.0.0.1, still no cross-origin WebSocket; refused on
    0.0.0.0 before it listens.
11. HTTPS with a self-signed certificate made here (skipped without openssl): the URL, a Secure
    cookie, the RFB greeting over wss.
12. start, status and stop as the systemd user unit $BEANJAR_WEB_UNIT, a name of the test's own
    (skipped without a working `systemctl --user`).
13. A loopback reverse proxy's forwarded HTTPS origin for POST and WebSockets, mismatches
    refused and Secure/HttpOnly/Strict cookies for token and password. Headers from non-loopback
    peers are ignored (the helper tested directly). none also accepts a local proxy's matching
    Origin while still refusing a mismatched one (the proxy's Access policy supplies the lock).
"""

import asyncio
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from unittest.mock import Mock

try:
    import aiohttp
except ImportError:
    print("python3-aiohttp is not installed: skipped")
    sys.exit(77)

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
from beanjar.devices import remote_layouts  # noqa: E402

sys.path.insert(0, os.path.join(TOP, "tests", "lib"))
import audiostream  # noqa: E402
import videostream  # noqa: E402
import loopback  # noqa: E402

sys.path.insert(0, os.path.join(TOP, "host", "web"))
from beanjar_web import access, server  # noqa: E402

VNC = sys.argv[1]
VIDEO = "--video-local" in subprocess.run([VNC, "--help"], capture_output=True, text=True).stdout
W, H = 640, 360
WEB = os.path.join(TOP, "host/web/bin/beanjar-web")
FAKE = f"{sys.executable} {os.path.join(HERE, '..', '..', 'fakes', 'fake-beanjar.py')}"


def fail(message):
    sys.exit(f"check-web: {message}")


def log_lines(path):
    with open(path) as f:
        return f.read().splitlines()


async def wait_log(path, lines, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if log_lines(path) == lines:
            return
        await asyncio.sleep(0.1)
    fail(f"input.log is {log_lines(path)}, not {lines}")


class Stream:
    """RFB's bytes over WebSocket binary messages."""

    def __init__(self, ws):
        self.ws, self.data = ws, b""

    async def rx(self, n):
        while len(self.data) < n:
            msg = await asyncio.wait_for(self.ws.receive(), 10)
            if msg.type != aiohttp.WSMsgType.BINARY:
                fail(f"rfb: {msg.type} {msg.data!r} {msg.extra!r}")
            self.data += msg.data
        out, self.data = self.data[:n], self.data[n:]
        return out

    async def tx(self, data):
        await self.ws.send_bytes(data)


async def check_rfb(session, url):
    async with session.ws_connect(f"{url}/vm/local/tv/rfb") as ws:
        s = Stream(ws)
        if not (await s.rx(12)).startswith(b"RFB 003."):
            fail("no RFB greeting over /rfb")
        await s.tx(b"RFB 003.008\n")
        types = list(await s.rx((await s.rx(1))[0]))
        if types != [1]:
            fail(f"/rfb offers {types}, not None alone")
        await s.tx(b"\x01")
        if struct.unpack(">I", await s.rx(4))[0] != 0:
            fail("security None refused")
        await s.tx(b"\x01")
        w, h = struct.unpack(">HH", await s.rx(4))
        await s.rx(16)
        await s.rx(struct.unpack(">I", await s.rx(4))[0])
        if (w, h) != (W, H):
            fail(f"/rfb desktop {w}x{h}")
        pf = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
        await s.tx(struct.pack(">Bxxx", 0) + pf + struct.pack(">BxHi", 2, 1, 0))
        pixels = bytearray(w * h * 4)
        for _ in range(30):
            await s.tx(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
            if (await s.rx(1))[0] != 0:
                fail("unexpected server message")
            await s.rx(1)
            for _ in range(struct.unpack(">H", await s.rx(2))[0]):
                x, y, rw, rh, enc = struct.unpack(">HHHHi", await s.rx(12))
                if enc != 0:
                    fail(f"encoding {enc}")
                data = await s.rx(rw * rh * 4)
                for row in range(rh):
                    o = ((y + row) * w + x) * 4
                    pixels[o : o + rw * 4] = data[row * rw * 4 : (row + 1) * rw * 4]
            white = pixels[((h * 3 // 8 + 2) * w + w * 3 // 8 + 2) * 4 :][:3]
            corner = pixels[((h - 1) * w + w - 1) * 4 :][:3]
            if white == b"\xff\xff\xff" and corner == bytes((32, 128, 192)):
                break
            await asyncio.sleep(0.2)
        else:
            fail(f"the frame over /rfb has {white.hex()} and {corner.hex()}")
    print("rfb: None alone over the WebSocket, fake-qemu's picture")
    try:
        async with session.ws_connect(
            f"{url}/vm/local/tv/rfb", headers={"Origin": "http://evil.example"}
        ):
            fail("a WebSocket from another origin was accepted")
    except aiohttp.WSServerHandshakeError as e:
        if e.status != 403:
            fail(f"another origin: {e.status}")
    print("rfb: a WebSocket from another origin refused")


async def check_remote(session, url, input_log):
    open(input_log, "w").close()
    async with session.ws_connect(f"{url}/vm/local/tv/remote") as ws:
        if (await asyncio.wait_for(ws.receive_str(), 10)) != "ready":
            fail("the remote did not say ready")
        await ws.send_str("down 0x40")
        await ws.send_str("up 0x40")
        await ws.send_str("sideways 7")
        answer = await asyncio.wait_for(ws.receive_str(), 10)
        if not answer.startswith("error:"):
            fail(f"a bad message answered {answer!r}")
        await ws.send_str("down 68")  # 0x44, ENTER: held when the socket closes
        await wait_log(input_log, ["IR.Down 0x40", "IR.Up 0x40", "IR.Down 0x44"])
    await wait_log(input_log, ["IR.Down 0x40", "IR.Up 0x40", "IR.Down 0x44", "IR.Up 0x44"])
    print("remote: ready, edges as IR, a bad message answered, the held key released at close")


async def check_audio(session, url):
    for fmt in ("pcm", "opus"):
        async with session.ws_connect(f"{url}/vm/local/tv/audio?format={fmt}") as ws:
            data, messages = b"", 0
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                msg = await asyncio.wait_for(ws.receive(), 10)
                if msg.type != aiohttp.WSMsgType.BINARY:
                    fail(f"audio {fmt}: {msg.type} {msg.data!r} {msg.extra!r}")
                data += msg.data
                messages += 1
        try:
            body = audiostream.header(data, fmt)
            if fmt == "opus":
                print(audiostream.check_lowdelay(body))
            pcm = audiostream.to_pcm(body, fmt)
            print(audiostream.check_tone(pcm, f"audio {fmt}") + f", {messages} messages")
        except ValueError as e:
            fail(str(e))
    async with session.get(f"{url}/vm/local/tv/audio?format=flac") as r:
        if r.status != 400:
            fail(f"audio in another format: {r.status}")
    print("audio: another format refused (400)")


async def video_bytes(session, url, query):
    """(what the video WebSocket with query brought in 1.5 s, the number of messages)"""
    async with session.ws_connect(f"{url}/vm/local/tv/video?{query}") as ws:
        data, messages = b"", 0
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            try:
                msg = await asyncio.wait_for(ws.receive(), end - time.monotonic())
            except asyncio.TimeoutError:
                break
            if msg.type != aiohttp.WSMsgType.BINARY:
                fail(f"video: {msg.type} {msg.data!r} {msg.extra!r}")
            data += msg.data
            messages += 1
    return data, messages


async def check_video(session, url):
    data, messages = await video_bytes(session, url, "bitrate=800")
    try:
        info, body = videostream.header(data)
    except ValueError as e:
        fail(f"video: {e}")
    units, _rest = videostream.units(body)
    if info != (W, H, 60, 1, 800, "x264"):
        fail(f"video: header {info}")
    if not units or not units[0][0] or not {7, 8, 5} <= set(videostream.nal_types(units[0][2])):
        fail("video: the first unit is not a keyframe with SPS and PPS")
    if videostream.have_ffmpeg():
        frames, errors = videostream.decode(b"".join(au for *_x, au in units), W, H)
        if errors or len(frames) != len(units):
            fail(f"video: {len(units)} units, {len(frames)} frames decoded: {errors}")
        # inside the white box, away from LG's cursor (drawn at the middle)
        box = videostream.pixel(frames[0], W, W * 5 // 8 - 8, H * 5 // 8 - 8)
        if not videostream.close_to(box, (255, 255, 255)):
            fail(f"video: {box} in the box, not fake-qemu's white")
    print(f"video: header {info}, a keyframe first, {len(units)} units in {messages} messages")
    for bad in ("5", "60000", "fast"):
        async with session.get(f"{url}/vm/local/tv/video?bitrate={bad}") as r:
            if r.status != 400:
                fail(f"video at bitrate {bad}: {r.status}")
    print("video: a bitrate out of range refused (400)")


class NoVnc:
    """An RFB client over /rfb that asks for updates as noVNC 1.6 does: Raw, optionally the
    ContinuousUpdates pseudo-encoding (then EnableContinuousUpdates at the first
    EndOfContinuousUpdates, and an incremental request after each update once a later one says
    they stopped), else an incremental request after each update. It records each update."""

    def __init__(self, ws, continuous, desktop=False):
        self.s, self.continuous = Stream(ws), continuous
        self.desktop = desktop
        self.desktop_events = []  # (time, reason, status, width, height, screens)
        self.updates = []  # (monotonic time, bytes, pixels covered)
        self.cu_supported = self.cu_enabled = False
        self.eocu = 0

    async def handshake(self):
        s = self.s
        await s.rx(12)
        await s.tx(b"RFB 003.008\n")
        await s.rx((await s.rx(1))[0])
        await s.tx(b"\x01")
        await s.rx(4)
        await s.tx(b"\x01")
        self.w, self.h = struct.unpack(">HH", await s.rx(4))
        await s.rx(16)
        await s.rx(struct.unpack(">I", await s.rx(4))[0])
        pf = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
        encs = [0] + ([-313] if self.continuous else [])
        if self.desktop:
            encs += [-223, -308]  # DesktopSize / ExtendedDesktopSize, as noVNC advertises
        await s.tx(struct.pack(">Bxxx", 0) + pf
                   + struct.pack(">BxH", 2, len(encs)) + struct.pack(f">{len(encs)}i", *encs))  # fmt: skip
        await s.tx(struct.pack(">BBHHHH", 3, 0, 0, 0, self.w, self.h))

    async def run(self):
        s = self.s
        while True:
            t = (await s.rx(1))[0]
            if t == 150:  # EndOfContinuousUpdates
                first, self.cu_supported = not self.cu_supported, True
                self.cu_enabled = first
                self.eocu += 1
                if first:
                    await s.tx(struct.pack(">BBHHHH", 150, 1, 0, 0, self.w, self.h))
                continue
            if t != 0:
                fail(f"rfb pause: server message {t}")
            await s.rx(1)
            size, covered = 4, 0
            for _ in range(struct.unpack(">H", await s.rx(2))[0]):
                x, y, rw, rh, enc = struct.unpack(">HHHHi", await s.rx(12))
                if self.desktop and enc in (-223, -308):
                    screens = []
                    if enc == -308:
                        count = (await s.rx(4))[0]
                        for _ in range(count):
                            screens.append(struct.unpack(">IHHHHI", await s.rx(16)))
                    self.desktop_events.append((time.monotonic(), x, y, rw, rh, screens))
                    if enc == -223 or x != 1 or y == 0:
                        self.w, self.h = rw, rh
                    continue
                if enc != 0:
                    fail(f"rfb pause: encoding {enc}")
                if self.desktop and (x + rw > self.w or y + rh > self.h):
                    fail(f"rfb rectangle {x},{y} {rw}x{rh} outside {self.w}x{self.h}")
                await s.rx(rw * rh * 4)
                size += 12 + rw * rh * 4
                covered += rw * rh
            self.updates.append((time.monotonic(), size, covered))
            if not self.cu_enabled:
                await s.tx(struct.pack(">BBHHHH", 3, 1, 0, 0, self.w, self.h))

    def since(self, t):
        return [u for u in self.updates if u[0] >= t]


async def check_two_viewers(session, url, tmp):
    """Two shared RFB clients keep receiving pixels across joins/leaves and resize requests."""
    animate = os.path.join(tmp, "animate")
    open(animate, "w").close()
    window = 2.0
    try:
        for continuous in (False, True):
            mode = "continuous" if continuous else "requests"
            clients = {}
            generation = 0

            async def connect(name):
                nonlocal generation
                generation += 1
                ws = await session.ws_connect(
                    f"{url}/vm/local/tv/rfb?page=two-{mode}-{name}-{generation}"
                )
                client = NoVnc(ws, continuous, desktop=True)
                clients[name] = (client, ws, None)
                await asyncio.wait_for(client.handshake(), 5)
                if (client.w, client.h) != (W, H):
                    fail(f"two viewers {mode} {name}: initial desktop {client.w}x{client.h}")
                task = asyncio.create_task(client.run())
                clients[name] = (client, ws, task)

            async def disconnect(name):
                _client, ws, task = clients.pop(name)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await ws.close()

            async def measure(phase, names, action=None):
                began = time.monotonic()
                if action:
                    await action()
                if time.monotonic() - began >= window:
                    fail(f"two viewers {mode} {phase}: action exceeded {window:.1f} s window")
                await asyncio.sleep(max(0, began + window - time.monotonic()))
                counts = []
                for name in names:
                    client, _ws, task = clients[name]
                    if task.done():
                        task.result()
                        fail(f"two viewers {mode} {phase}: {name} reader ended")
                    if continuous and not client.cu_enabled:
                        fail(f"two viewers {mode} {phase}: {name} continuous updates not enabled")
                    frames = [u for u in client.since(began) if u[0] < began + window and u[2]]
                    times = [began, *(u[0] for u in frames), began + window]
                    gap = max(b - a for a, b in zip(times, times[1:]))
                    if len(frames) < 5 or gap > 1.0:
                        fail(
                            f"two viewers {mode} {phase}: {name} {len(frames)} frames, max gap {gap:.3f} s"
                        )
                    if (client.w, client.h) != (W, H):
                        fail(
                            f"two viewers {mode} {phase}: {name} desktop changed to {client.w}x{client.h}"
                        )
                    counts.append(
                        f"{name}={len(frames)} frames, max_gap={gap:.3f}s, {client.w}x{client.h}"
                    )
                print(f"two viewers {mode} {phase} PASS ({window:.1f}s): " + "; ".join(counts))

            try:
                await connect("A")
                await measure("A alone", ("A",))
                await measure("B connects", ("A", "B"), lambda: connect("B"))
                b = clients["B"][0]
                layouts = [e for e in b.desktop_events if e[5]]
                if not layouts:
                    fail(
                        f"two viewers {mode}: no ExtendedDesktopSize advertisement; events={b.desktop_events}"
                    )
                screen_id, _x, _y, _w, _h, flags = layouts[-1][5][0]
                requested = (W // 2, H // 2)
                before_resize = time.monotonic()

                async def resize():
                    # Debian noVNC's RFB.messages.setDesktopSize wire format, one screen.
                    await b.s.tx(
                        struct.pack(
                            ">BBHHBBIHHHHI",
                            251,
                            0,
                            *requested,
                            1,
                            0,
                            screen_id,
                            0,
                            0,
                            *requested,
                            flags,
                        )
                    )

                await measure("B requests smaller desktop", ("A", "B"), resize)
                result = [e for e in b.desktop_events if e[0] >= before_resize]
                print(
                    f"two viewers {mode} resize request={requested[0]}x{requested[1]}: "
                    f"replies={[(e[1], e[2], e[3], e[4]) for e in result]}"
                )
                if not any(e[1] == 1 for e in result):
                    fail(f"two viewers {mode}: no response to SetDesktopSize")
                await measure("B disconnects", ("A",), lambda: disconnect("B"))
                await measure("B reconnects", ("A", "B"), lambda: connect("B"))
                await measure("A disconnects", ("B",), lambda: disconnect("A"))
                await measure("A reconnects", ("A", "B"), lambda: connect("A"))
            finally:
                for name in list(clients):
                    await disconnect(name)
    finally:
        os.unlink(animate)


def pause_lines(tmp):
    """beanjar-vnc's "RFB client paused|resumed (... vnc_feeds=N)" lines: (word, N)."""
    out = []
    for line in log_lines(os.path.join(tmp, "vnc.log")):
        if line.startswith("beanjar-vnc: RFB client ") and "vnc_feeds=" in line:
            out.append((line.split()[3], int(line.rsplit("vnc_feeds=", 1)[1].rstrip(")"))))
    return out


def tcp_marker(port):
    """The pause marker from a client on beanjar-vnc's TCP listener (security None)."""
    with socket.create_connection((loopback.ADDRESS, port), timeout=5) as c:

        def rx(n):
            data = b""
            while len(data) < n:
                data += c.recv(n - len(data))
            return data

        rx(12)
        c.sendall(b"RFB 003.008\n")
        if 1 not in rx(rx(1)[0]):
            fail("tcp marker: no security type None")
        c.sendall(b"\x01")
        rx(4)
        c.sendall(b"\x01")
        rx(20)
        rx(struct.unpack(">I", rx(4))[0])
        c.sendall(struct.pack(">BxxxI", 6, 17) + b"beanjar:rfb-pause")
        time.sleep(0.5)


async def check_rfb_pause(session, url, tmp, vnc_port):
    """/rfb?page=P sends no picture while /video?page=P plays, takes input meanwhile, and gets the
    whole screen when the video closes; another page's video does not pause it. With and without
    continuous updates (noVNC uses them with neatvnc)."""
    input_log = os.path.join(tmp, "input.log")
    animate = os.path.join(tmp, "animate")
    open(animate, "w").close()
    try:
        for continuous in (False, True):
            mode = "continuous updates" if continuous else "update requests"
            page = f"check{int(continuous)}{os.getpid()}"
            async with session.ws_connect(f"{url}/vm/local/tv/rfb?page={page}") as ws:
                c = NoVnc(ws, continuous)
                await c.handshake()
                task = asyncio.ensure_future(c.run())
                try:
                    await asyncio.sleep(1.5)
                    if len(c.since(time.monotonic() - 1)) < 5:
                        fail(f"rfb pause ({mode}): {len(c.updates)} updates before the video")
                    if continuous and not c.cu_enabled:
                        fail("rfb pause: continuous updates were not enabled")
                    async with session.ws_connect(
                        f"{url}/vm/local/tv/video?bitrate=800&page=other{page}"
                    ) as other:
                        await asyncio.wait_for(other.receive(), 10)
                        await asyncio.sleep(1.0)
                        if len(c.since(time.monotonic() - 0.8)) < 5:
                            fail(f"rfb pause ({mode}): another page's video paused it")
                    async with session.ws_connect(
                        f"{url}/vm/local/tv/video?bitrate=800&page={page}"
                    ) as video:
                        await asyncio.wait_for(video.receive(), 10)
                        units = [0]

                        async def drain():
                            async for _m in video:
                                units[0] += 1

                        vtask = asyncio.ensure_future(drain())
                        await asyncio.sleep(1.0)  # what was in flight
                        u0 = units[0]
                        t0 = time.monotonic()
                        open(input_log, "w").close()
                        await c.s.tx(
                            struct.pack(">BBxxI", 4, 1, 0x61) + struct.pack(">BBxxI", 4, 0, 0x61)
                        )
                        await c.s.tx(struct.pack(">BBHH", 5, 0, 100, 50))
                        await wait_log(input_log, ["Keyboard.Press 30", "Keyboard.Release 30",
                                                   "Mouse.SetAbsPosition 300 150"])  # fmt: skip
                        await asyncio.sleep(max(0, t0 + 3 - time.monotonic()))
                        during = c.since(t0)
                        if during:
                            fail(f"rfb pause ({mode}): {len(during)} updates "
                                 f"({sum(u[1] for u in during)} bytes) under the video")  # fmt: skip
                        if continuous and c.eocu != 2:
                            fail(f"rfb pause: {c.eocu} EndOfContinuousUpdates, not 2")
                        vunits = units[0] - u0
                        if vunits < 20:
                            fail(f"rfb pause ({mode}): {vunits} video messages under the pause")
                        vtask.cancel()
                        await asyncio.gather(vtask, return_exceptions=True)
                        t1 = time.monotonic()
                    await asyncio.sleep(1.0)
                    marks = pause_lines(tmp)[-2:]
                    words, feeds = [w for w, _n in marks], {n for _w, n in marks}
                    if words != ["paused", "resumed"] or len(feeds) != 1:
                        fail(f"rfb pause ({mode}): beanjar-vnc's pause lines {pause_lines(tmp)}, "
                             "not paused then resumed with no feed between")  # fmt: skip
                    after = c.since(t1)
                    # the whole screen first, or second: with update requests one asked before
                    # the pause is still pending at neatvnc (nothing was fed), and the frame fed
                    # at the resume answers it with what changed, before the whole screen
                    full = [u for u in after[:2] if u[2] >= c.w * c.h]
                    if not full or (continuous and after[0] is not full[0]):
                        fail(f"rfb pause ({mode}): after the video {after[:2]}, not the whole "
                             "screen first")  # fmt: skip
                    if full[0][0] - t1 > 0.5 or len(after) < 5:
                        fail(f"rfb pause ({mode}): the whole screen after "
                             f"{full[0][0] - t1:.2f} s, {len(after)} updates in 1 s")  # fmt: skip
                    print(f"rfb pause ({mode}): no update in 3 s under the page's video, a key "
                          f"and a move through, no frame fed to neatvnc, {vunits} video "
                          f"messages; the whole screen {full[0][0] - t1:.2f} s after it, then "
                          f"{len(after)} updates in 1 s; another page's video ignored")  # fmt: skip
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        before = pause_lines(tmp)
        await asyncio.get_running_loop().run_in_executor(None, tcp_marker, vnc_port)
        if pause_lines(tmp) != before:
            fail(f"rfb pause: the marker over TCP paused: {pause_lines(tmp)[len(before) :]}")
        print("rfb pause: the marker over TCP does nothing")
    finally:
        os.unlink(animate)
    async with session.get(f"{url}/vm/local/tv/rfb?page=x") as r:
        if r.status != 400:
            fail(f"/rfb with a bad page id: {r.status}")


async def checks(url, tmp, token, vnc_port):
    async with aiohttp.ClientSession() as bare:
        for action in ("start", "stop"):
            async with bare.post(f"{url}/vm/local/tv/{action}") as r:
                if r.status != 403:
                    fail(f"{action} without token: {r.status}")
        async with bare.get(f"{url}/") as r:
            if r.status != 403:
                fail(f"/ without the token: {r.status}")
        async with bare.get(f"{url}/?token=wrong") as r:
            if r.status != 403:
                fail(f"/ with a wrong token: {r.status}")
    jar = aiohttp.CookieJar(unsafe=True)  # cookies for 127.0.0.1
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.get(f"{url}/?token={token}", allow_redirects=False) as r:
            if (
                r.status != 302
                or "beanjar_web" not in r.cookies
                or "token" in r.headers["Location"]
            ):
                fail(f"the token's redirect: {r.status} {r.headers}")
            if "HttpOnly" not in r.headers["Set-Cookie"]:
                fail(f"the cookie is not HttpOnly: {r.headers['Set-Cookie']}")
        async with session.get(f"{url}/") as r:
            if r.status != 200 or "Beanjar" not in await r.text():
                fail(f"/ with the cookie: {r.status}")
        print("token: refused without it or wrong; the URL's becomes an HttpOnly cookie")

        async with session.get(f"{url}/api/vms") as r:
            rows = await r.json()
            tv = [v for v in rows if v["name"] == "tv" and v["host"] is None]
            if [(v["state"], v["vnc"], v["mem"], v["smp"]) for v in tv] != [
                ("Running", True, 2048, 4)
            ]:
                fail(f"/api/vms: {rows}")
            if (
                any("library" in v or "pid" in v for v in rows)
                or len({v["host"] for v in rows}) != 2
            ):
                fail(f"/api/vms passes on more than the rows show, or lost a host: {rows[0]}")
        async with session.post(f"{url}/vm/local/tv/vnc") as r:
            if r.status != 200 or (await r.json()) != {"started": False}:
                fail(f"vnc with beanjar-vnc running: {r.status} {await r.text()}")
        refuse = os.path.join(tmp, "refuse")
        with open(refuse, "w") as f:
            f.write("a viewer is already open for tv (pid 1)\n")
        async with session.post(f"{url}/vm/local/tv/vnc") as r:
            body = await r.json()
            if r.status != 409 or body != {"error": "a viewer is already open for tv (pid 1)"}:
                fail(f"vnc beside the viewer: {r.status} {body}")
        os.unlink(refuse)
        print("vnc: a running one accepted, the manager's refusal passed on (409)")
        await check_thumbnails(session, url, tmp)
        await check_actions(session, url, tmp)

        await check_rfb(session, url)
        await check_two_viewers(session, url, tmp)
        await check_remote(session, url, os.path.join(tmp, "input.log"))
        await check_audio(session, url)
        if VIDEO:
            await check_video(session, url)
            await check_rfb_pause(session, url, tmp, vnc_port)
        else:
            print("video: beanjar-vnc is built without --video-local: not checked")

        async with session.get(f"{url}/vm/local/tv/layout.json") as r:
            if (await r.json()) != json.loads(
                json.dumps(remote_layouts.as_json(remote_layouts.NORMAL))
            ):
                fail("layout.json is not the GTK remote's normal layout")
        async with session.get(f"{url}/vm/local/tv/screenshot.png") as r:
            png = await r.read()
            if r.content_type != "image/png" or struct.unpack(">II", png[16:24]) != (1280, 720):
                fail(f"screenshot.png: {r.status} {r.content_type} {png[:24]!r}")
        print("layout.json is remote_layouts.as_json(NORMAL); screenshot.png is 1280x720")
        for path in (
            "/vm/local/tv/",
            "/remote.html?vm=local/tv",
            "/viewer.js",
            "/remote.js",
            "/audio.js",
            "/audio-worklet.js",
            "/video.js",
            "/icons/volume-up.svg",
            "/tv-remote.svg",
            "/style.css",
            "/novnc/core/rfb.js",
            "/icons/house.svg",
        ):
            async with session.get(url + path) as r:
                if r.status != 200:
                    fail(f"{path}: {r.status}")
        async with session.get(f"{url}/vm/local/x%20y/") as r:
            if r.status != 400:
                fail(f"an invalid VM name: {r.status}")
        print("pages: the viewer, the remote, their scripts, noVNC and the icons served")


async def check_thumbnails(session, url, tmp):
    # The page's list poll supplies eligibility for every card, including concurrent pages.
    async with session.get(f"{url}/api/vms") as r:
        if r.status != 200:
            fail(f"thumbnail eligibility list: {r.status}")
    lists_before = log_lines(os.path.join(tmp, "calls.log")).count("list --json")

    def captures():
        return [
            line
            for line in log_lines(os.path.join(tmp, "calls.log"))
            if line.startswith("screenshot ")
        ]

    before = captures()
    for spec in ("pi/wt1h-afro", "pi/w19p"):
        async with session.get(f"{url}/vm/{spec}/screenshot.png?thumbnail=1") as r:
            if r.status != 204:
                fail(f"thumbnail without a running VNC: {spec}: {r.status}")
    if captures() != before:
        fail("a thumbnail without running beanjar-vnc invoked screenshot")
    # off: the last screen the manager kept, or none
    async with session.get(f"{url}/vm/local/k8hp-ombre/screenshot.png?thumbnail=1") as r:
        png = await r.read()
        if r.status != 200 or struct.unpack(">II", png[16:24]) != (384, 216):
            fail(f"the kept thumbnail of a VM that is off: {r.status} {png[:24]!r}")
        if r.headers.get("X-Beanjar-Rotation") != "90":
            fail(f"the kept thumbnail is not turned to the saved rotation: {dict(r.headers)}")
    async with session.get(f"{url}/vm/local/w22o-number1/screenshot.png?thumbnail=1") as r:
        if r.status != 204:
            fail(f"a VM that is off without a kept thumbnail: {r.status}")
    if captures()[len(before) :] != [
        "screenshot local/k8hp-ombre - --last",
        "screenshot local/w22o-number1 - --last",
    ]:
        fail(f"off VMs' thumbnails: {captures()[len(before) :]}")
    before = captures()

    async def thumbnail():
        async with session.get(f"{url}/vm/local/tv/screenshot.png?thumbnail=1") as r:
            png = await r.read()
            if (
                r.status != 200
                or r.content_type != "image/png"
                or struct.unpack(">II", png[16:24]) != (384, 216)
            ):
                fail(f"thumbnail: {r.status} {r.content_type} {png[:24]!r}")
            return png

    one, two = await asyncio.gather(thumbnail(), thumbnail())
    three = await thumbnail()
    if one != two or two != three:
        fail("thumbnail cache returned different pictures within its window")
    if captures()[len(before) :] != ["screenshot local/tv - --frame 384x216"]:
        fail(f"thumbnail requests did not share a single small capture: {captures()}")
    hold = os.path.join(tmp, "thumbnail-hold")
    animate = os.path.join(tmp, "animate")
    refuse = os.path.join(tmp, "thumbnail-refuse")
    try:
        # Make the real fake-display picture change, so the completed refresh is observable.
        open(animate, "w").close()
        open(hold, "w").close()
        await asyncio.sleep(5.1)
        began = time.monotonic()
        old = await asyncio.wait_for(asyncio.gather(*(thumbnail() for _ in range(4))), 0.5)
        took = time.monotonic() - began
        if any(png != one for png in old):
            fail("stale requests did not return the existing picture while capture was held")
        for _ in range(100):
            if len(captures()[len(before) :]) == 2:
                break
            await asyncio.sleep(0.02)
        else:
            fail("stale picture did not start one background capture")
        if await thumbnail() != one or len(captures()[len(before) :]) != 2:
            fail("another page waited for or duplicated the held background capture")
        os.unlink(hold)
        for _ in range(100):
            newer = await thumbnail()
            if newer != one:
                break
            await asyncio.sleep(0.02)
        else:
            fail("the completed background capture was not published")
        print(
            f"thumbnails: four stale pages served in {took:.3f}s while one capture was held; refreshed image published"
        )

        # A failed refresh keeps serving the last good image, without a retry per page.
        open(refuse, "w").close()
        await asyncio.sleep(5.1)
        if await thumbnail() != newer:
            fail("failed refresh discarded the last good picture")
        for _ in range(100):
            if len(captures()[len(before) :]) == 3:
                break
            await asyncio.sleep(0.02)
        else:
            fail("no refresh attempted after the next interval")
        await asyncio.sleep(0.15)
        for _ in range(3):
            if await thumbnail() != newer:
                fail("a refresh error escaped instead of the last good picture")
        if len(captures()[len(before) :]) != 3:
            fail("failed refresh was retried for every page")
        # Off cards, including a negative --last result, are fetched once per state.
        for spec, status in (("local/k8hp-ombre", 200), ("local/w22o-number1", 204)):
            async with session.get(f"{url}/vm/{spec}/screenshot.png?thumbnail=1") as r:
                if r.status != status:
                    fail(f"repeated Off thumbnail {spec}: {r.status}")
        last_calls = [c for c in captures() if c.endswith("--last")]
        if last_calls != [
            "screenshot local/k8hp-ombre - --last",
            "screenshot local/w22o-number1 - --last",
        ]:
            fail(f"Off thumbnails were fetched more than once: {last_calls}")
    finally:
        for path in (hold, animate, refuse):
            if os.path.exists(path):
                os.unlink(path)
    lists_after = log_lines(os.path.join(tmp, "calls.log")).count("list --json")
    if lists_after != lists_before:
        fail(f"previews ran extra manager lists: {lists_after - lists_before}")
    # A new list's ineligible state takes precedence over a previously cached PNG.
    state = os.path.join(tmp, "state-local-tv")
    with open(state, "w") as f:
        f.write("Starting")
    try:
        async with session.get(f"{url}/api/vms") as r:
            if r.status != 200:
                fail(f"new eligibility list: {r.status}")
        before_ineligible = captures()
        async with session.get(f"{url}/vm/local/tv/screenshot.png?thumbnail=1") as r:
            if r.status != 204:
                fail(f"ineligible VM served a cached PNG: {r.status}")
        if captures() != before_ineligible:
            fail("ineligible VM started a capture")
    finally:
        os.unlink(state)
    print(
        "thumbnails: no screenshot without running VNC; an off VM's the kept one (--last); "
        "concurrent and repeated requests share one 384x216 capture and the page's list"
    )


async def vm_row(session, url, spec):
    host, name = spec.split("/")
    async with session.get(f"{url}/api/vms") as r:
        rows = await r.json()
    return next(v for v in rows if (v["host"] or "local") == host and v["name"] == name)


async def wait_operation(session, url, spec, status):
    for _ in range(100):
        async with session.get(f"{url}/vm/{spec}/operation") as r:
            body = await r.json()
            if r.status != 202:
                if r.status != status:
                    fail(f"{spec} operation: {r.status} {body}")
                return body
        await asyncio.sleep(0.05)
    fail(f"{spec}: operation did not finish")


async def check_cross_posts(session, url):
    for path in (
        "/vm/local/tv/start",
        "/vm/local/tv/stop",
        "/vm/local/tv/vnc",
        "/login",
        "/logout",
    ):
        for headers in (
            {"Origin": "http://evil.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            async with session.post(url + path, headers=headers, allow_redirects=False) as r:
                if r.status != 403:
                    fail(f"cross-site POST {path} {headers}: {r.status}")


async def check_actions(session, url, tmp):
    calls_file = os.path.join(tmp, "calls.log")
    before = log_lines(calls_file)
    await check_cross_posts(session, url)
    for action in ("start", "stop"):
        async with session.get(f"{url}/vm/local/tv/{action}") as r:
            if r.status != 405:
                fail(f"GET {action}: {r.status}")
        async with session.post(f"{url}/vm/local/x%20y/{action}") as r:
            if r.status != 400:
                fail(f"POST invalid VM {action}: {r.status}")
    if before != log_lines(calls_file):
        fail("a cross-site, GET or invalid-name action invoked the manager")
    async with session.get(f"{url}/vm/local/tv/operation") as r:
        if r.status != 404:
            fail(f"an operation before any request: {r.status}")

    hold = os.path.join(tmp, "action-hold")
    fallback = "VM tv: webOS refused the power-off signal; syncing its disks and stopping it"
    with open(os.path.join(tmp, "stop-message"), "w") as f:
        f.write(fallback)
    for spec in ("local/tv", "pi/w16r"):
        for action, state in (("stop", "Off"), ("start", "Running")):
            open(hold, "w").close()
            before = log_lines(calls_file)
            began = time.monotonic()
            async with session.post(f"{url}/vm/{spec}/{action}", headers={"Origin": url}) as r:
                body = await r.json()
                elapsed = time.monotonic() - began
                if r.status != 202 or body["status"] != "pending" or elapsed > 1:
                    fail(f"{action} accepted: {r.status} {body} in {elapsed:.3f} s")
                if r.headers.get("Location") != f"/vm/{spec}/operation":
                    fail(f"{action}: no operation Location")
            async with session.get(f"{url}/vm/{spec}/operation") as r:
                if r.status != 202:
                    fail(f"pending operation: {r.status}")
            vm = await vm_row(session, url, spec)
            if vm["operation"]["action"] != action or vm["operation"]["status"] != "pending":
                fail(f"pending operation not in list: {vm}")
            for duplicate in ("start", "stop"):
                async with session.post(f"{url}/vm/{spec}/{duplicate}") as r:
                    body = await r.json()
                    if r.status != 409 or "still in progress" not in body.get("error", ""):
                        fail(f"duplicate {duplicate}: {r.status} {body}")
            os.unlink(hold)
            result = await wait_operation(session, url, spec, 200)
            if result["status"] != "succeeded" or (
                action == "stop" and fallback not in result["message"]
            ):
                fail(f"{action} result: {result}")
            vm = await vm_row(session, url, spec)
            if vm["state"] != state or vm["operation"] != result:
                fail(f"{action} state not visible in /api/vms: {vm}")
            new_actions = [
                c for c in log_lines(calls_file)[len(before) :] if c.startswith(("start ", "stop "))
            ]
            if new_actions != [f"{action} --json {spec}"]:
                fail(f"duplicate or incorrect manager action: {new_actions}")
            print(
                f"{spec} {action}: accepted in {elapsed:.3f} s, duplicate start/stop 409; list {state}"
            )
    os.unlink(os.path.join(tmp, "stop-message"))

    for action, message in (
        ("start", "not enough memory to start tv"),
        ("start", "max_running reached"),
        ("start", "VM tv is already running"),
        ("stop", "VM tv cannot take the power-off signal: still starting"),
    ):
        refusal = os.path.join(tmp, f"refuse-{action}")
        with open(refusal, "w") as f:
            f.write(message)
        async with session.post(f"{url}/vm/local/tv/{action}") as r:
            if r.status != 202:
                fail(f"{action} refusal not accepted for background execution: {r.status}")
        result = await wait_operation(session, url, "local/tv", 409)
        if result.get("error") != message:
            fail(f"manager refusal changed: {result}")
        vm = await vm_row(session, url, "local/tv")
        if vm["operation"] != result or vm["state"] != "Running":
            fail(f"refusal missing from list or changed VM state: {vm}")
        os.unlink(refusal)
    print("start/stop: manager refusals returned as 409 and shown in list; stop fallback retained")

    # a VM that runs for a reason: in the list; stop refused without force, done with ?force=1
    with open(os.path.join(tmp, "reason"), "w") as f:
        f.write("bisecting the camera")
    async with session.post(f"{url}/vm/local/tv/stop") as r:
        if r.status != 202:
            fail(f"stop of a VM with a reason not accepted: {r.status}")
    result = await wait_operation(session, url, "local/tv", 409)
    if "bisecting the camera" not in result.get("error", ""):
        fail(f"stop without force did not say the reason: {result}")
    vm = await vm_row(session, url, "local/tv")  # the list asked again after the operation
    if (vm.get("reason") or {}).get("text") != "bisecting the camera":
        fail(f"reason missing from the list: {vm}")
    if vm.get("tags") != ["keep", "camera"]:
        fail(f"tags missing from the list: {vm}")
    before = log_lines(calls_file)
    async with session.post(f"{url}/vm/local/tv/stop?force=1") as r:
        if r.status != 202:
            fail(f"forced stop not accepted: {r.status}")
    result = await wait_operation(session, url, "local/tv", 200)
    new_actions = [c for c in log_lines(calls_file)[len(before) :] if c.startswith("stop ")]
    if result["status"] != "succeeded" or new_actions != ["stop --json --force local/tv"]:
        fail(f"forced stop: {result} {new_actions}")
    os.unlink(os.path.join(tmp, "reason"))
    async with session.post(f"{url}/vm/local/tv/start") as r:
        if r.status != 202:
            fail(f"start after the forced stop: {r.status}")
    await wait_operation(session, url, "local/tv", 200)
    print("reason: in the list, stop refused without force, stop?force=1 runs stop --force")


async def pending_at_shutdown(url, tmp, token):
    open(os.path.join(tmp, "action-hold"), "w").close()
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        await session.get(f"{url}/?token={token}")
        thumbnail_url = f"{url}/vm/local/tv/screenshot.png?thumbnail=1"
        async with session.get(thumbnail_url) as r:
            if r.status != 200:
                fail(f"thumbnail before shutdown: {r.status}")
        await asyncio.sleep(5.1)
        open(os.path.join(tmp, "thumbnail-hold"), "w").close()
        before = len(log_lines(os.path.join(tmp, "thumbnail-pids")))
        async with session.get(thumbnail_url) as r:
            if r.status != 200:
                fail(f"stale thumbnail before shutdown: {r.status}")
        for _ in range(100):
            if len(log_lines(os.path.join(tmp, "thumbnail-pids"))) > before:
                break
            await asyncio.sleep(0.02)
        else:
            fail("background thumbnail did not enter the fake manager before shutdown")
        async with session.post(f"{url}/vm/pi/w16r/stop") as r:
            if r.status != 202:
                fail(f"shutdown operation: {r.status}")
        for _ in range(100):
            vm = await vm_row(session, url, "pi/w16r")
            if vm["state"] == "Stopping" and vm["operation"]["status"] == "pending":
                return
            await asyncio.sleep(0.05)
        fail("the pending stop did not enter the fake manager")


async def check_restart(url, token):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        await session.get(f"{url}/?token={token}")
        vm = await vm_row(session, url, "pi/w16r")
        if vm["state"] != "Off" or vm["operation"] is not None:
            fail(f"restart retained operation instead of manager state: {vm}")
        async with session.get(f"{url}/vm/pi/w16r/operation") as r:
            if r.status != 404:
                fail(f"restart retained operation endpoint: {r.status}")
    print("restart: no stale operation, manager's Off state shown")


def check_origin_helper():
    from aiohttp.test_utils import make_mocked_request

    def request(peer, extra):
        transport = Mock()
        transport.get_extra_info.side_effect = lambda name, default=None: (
            (peer, 12345) if name == "peername" else default
        )
        return make_mocked_request(
            "POST", "/", headers={"Host": "internal.example", **extra}, transport=transport
        )

    forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "bj.example.com"}
    instance = server.Server([], access.Access("token", "test-secret"), "", "")
    for peer, wanted in (
        ("127.0.0.1", "https://bj.example.com"),
        ("::1", "https://bj.example.com"),
        ("198.51.100.9", "http://internal.example"),
        ("2001:db8::9", "http://internal.example"),
    ):
        req = request(peer, forwarded)
        if server.expected_origin(req) != wanted:
            fail(f"forwarded origin from {peer}: {server.expected_origin(req)}, expected {wanted}")
        response = aiohttp.web.Response()
        instance.set_session(response, req)
        if bool(response.cookies[server.COOKIE]["secure"]) != wanted.startswith("https:"):
            fail(f"forwarded Secure cookie from {peer}")
    for ignored in (
        {"CF-Visitor": '{"scheme":"https"}'},
        {"X-Forwarded-Host": "bj.example.com"},
        {"Forwarded": 'proto=https;host="bj.example.com"'},
    ):
        if server.expected_origin(request("127.0.0.1", ignored)) != "http://internal.example":
            fail(f"an unsupported forwarded authority was trusted: {ignored}")
    for bad in (
        {"X-Forwarded-Proto": "https,http"},
        {"X-Forwarded-Proto": "ftp"},
        {"X-Forwarded-Proto": ""},
        {**forwarded, "X-Forwarded-Host": "bj.example.com/path"},
    ):
        try:
            server.expected_origin(request("127.0.0.1", bad))
        except aiohttp.web.HTTPBadRequest:
            pass
        else:
            fail(f"an invalid forwarded origin was accepted: {bad}")
    print(
        "proxy helper: IPv4/IPv6 loopback trusted; non-loopback origin and cookies ignore forwarding; malformed headers refused"
    )


async def check_proxy(url, mode, secret=None):
    """Simulate cloudflared over local HTTP; supply its browser cookie explicitly (Secure)."""
    public = "https://bj.example.com"
    proxy = {
        "Host": "bj.example.com",
        "X-Forwarded-Proto": "https",
        "CF-Visitor": '{"scheme":"https"}',
    }
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        if mode == "token":
            response = await session.get(
                f"{url}/?token={secret}", headers=proxy, allow_redirects=False
            )
            expected = 302
        elif mode == "password":
            response = await session.post(
                f"{url}/login",
                headers={**proxy, "Origin": public},
                data={"password": secret},
                allow_redirects=False,
            )
            expected = 303
        cookies = {}
        if mode != "none":
            async with response as r:
                cookie = r.cookies.get(server.COOKIE)
                if (
                    r.status != expected
                    or cookie is None
                    or not all(cookie[key] for key in ("secure", "httponly"))
                    or cookie["samesite"] != "Strict"
                ):
                    fail(f"{mode} proxy session: {r.status}, cookie attributes incomplete")
                cookies = {"Cookie": f"{server.COOKIE}={cookie.value}"}
        else:
            async with session.get(url + "/", headers=proxy) as r:
                if r.status != 200 or server.COOKIE in r.cookies:
                    fail(f"none proxy page: {r.status}")
        for host_headers in (
            {"Host": "bj.example.com"},
            {"Host": "internal.example", "X-Forwarded-Host": "bj.example.com"},
        ):
            headers = {**proxy, **host_headers, "Origin": public, **cookies}
            async with session.post(f"{url}/vm/local/tv/vnc", headers=headers) as r:
                if r.status != 200 or (await r.json()) != {"started": False}:
                    fail(f"{mode} proxy POST: {r.status}")
            async with session.ws_connect(f"{url}/vm/local/tv/rfb", headers=headers) as ws:
                if not (await Stream(ws).rx(12)).startswith(b"RFB 003."):
                    fail(f"{mode} proxy WebSocket: no RFB greeting")
            bad = {**headers, "Origin": "https://evil.example"}
            async with session.post(f"{url}/vm/local/tv/vnc", headers=bad) as r:
                if r.status != 403:
                    fail(f"{mode} proxy cross-site POST: {r.status}")
            try:
                async with session.ws_connect(f"{url}/vm/local/tv/rfb", headers=bad):
                    fail(f"{mode} proxy cross-site WebSocket accepted")
            except aiohttp.WSServerHandshakeError as e:
                if e.status != 403:
                    fail(f"{mode} proxy cross-site WebSocket: {e.status}")
        if mode != "none":
            async with session.post(
                f"{url}/vm/local/tv/vnc", headers={**proxy, "Origin": public}
            ) as r:
                if r.status != 403:
                    fail(f"{mode} proxy skipped session authentication: {r.status}")
    cookie_note = (
        "no local authentication"
        if mode == "none"
        else "Secure/HttpOnly/Strict cookie, authentication required"
    )
    print(f"proxy {mode}: matching POST/RFB accepted, mismatches 403, {cookie_note}")


async def check_password(url, password):
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        await check_cross_posts(session, url)
        async with session.get(f"{url}/login") as r:
            if r.status != 200 or 'name="password"' not in await r.text():
                fail(f"/login: {r.status} {await r.text()}")
        async with (
            session.get(f"{url}/style.css") as r,
            session.get(f"{url}/icons/shield-lock.svg") as i,
        ):
            if (r.status, i.status) != (200, 200):
                fail(f"the login page's style and icon: {r.status} {i.status}")
        async with session.get(f"{url}/", allow_redirects=False) as r:
            if r.status != 302 or r.headers["Location"] != "/login":
                fail(f"/ before logging in: {r.status} {r.headers.get('Location')}")
        async with session.get(f"{url}/api/vms") as r:
            if r.status != 403:
                fail(f"/api/vms before logging in: {r.status}")
        for action in ("start", "stop"):
            async with session.post(f"{url}/vm/local/tv/{action}") as r:
                if r.status != 403:
                    fail(f"{action} before logging in: {r.status}")
        try:
            async with session.ws_connect(f"{url}/vm/local/tv/remote"):
                fail("a WebSocket before logging in was accepted")
        except aiohttp.WSServerHandshakeError as e:
            if e.status != 403:
                fail(f"a WebSocket before logging in: {e.status}")
        began = time.monotonic()
        async with session.post(f"{url}/login", data={"password": password + "x"},
                                allow_redirects=False) as r:  # fmt: skip
            took = time.monotonic() - began
            if (
                r.status != 303
                or r.headers["Location"] != "/login?failed=1"
                or "beanjar_web" in r.cookies
            ):
                fail(f"a wrong password: {r.status} {r.headers}")
            if took < 0.9:
                fail(f"a wrong password answered in {took:.2f} s")
        async with session.post(f"{url}/login", data={"password": password},
                                allow_redirects=False) as r:  # fmt: skip
            if r.status != 303 or r.headers["Location"] != "/" or "beanjar_web" not in r.cookies:
                fail(f"the right password: {r.status} {r.headers}")
            if "HttpOnly" not in r.headers["Set-Cookie"] or password in r.headers["Set-Cookie"]:
                fail(f"the session cookie: {r.headers['Set-Cookie']}")
        print(
            f"password: the login page; a wrong one refused after {took:.1f} s, the right one a cookie"
        )
        async with session.get(f"{url}/") as r:
            if r.status != 200 or 'id="logout"' not in await r.text():
                fail(f"/ logged in: {r.status}")
        async with session.get(f"{url}/api/session") as r:
            if (await r.json()) != {"auth": "password"}:
                fail("/api/session does not say password")
        async with session.get(f"{url}/api/vms") as r:
            if r.status != 200 or not await r.json():
                fail(f"/api/vms logged in: {r.status}")
        async with session.ws_connect(f"{url}/vm/local/tv/rfb") as ws:
            if not (await Stream(ws).rx(12)).startswith(b"RFB 003."):
                fail("no RFB greeting logged in")
        async with session.ws_connect(f"{url}/vm/local/tv/remote") as ws:
            if (await asyncio.wait_for(ws.receive_str(), 10)) != "ready":
                fail("the remote did not say ready logged in")
        async with session.ws_connect(f"{url}/vm/local/tv/audio?format=pcm") as ws:
            msg = await asyncio.wait_for(ws.receive(), 10)
            if msg.type != aiohttp.WSMsgType.BINARY:
                fail(f"audio logged in: {msg.type}")
        print("password: logged in, the list, /rfb, /remote and /audio open")
        async with session.post(f"{url}/logout", allow_redirects=False) as r:
            if r.status != 303 or r.headers["Location"] != "/login":
                fail(f"/logout: {r.status}")
        async with session.get(f"{url}/", allow_redirects=False) as r:
            if r.status != 302:
                fail(f"/ after logging out: {r.status}")
        print("password: Log Out ends the session")


async def check_none(url):
    async with aiohttp.ClientSession() as bare:
        await check_cross_posts(bare, url)
        async with bare.get(f"{url}/") as r:
            if r.status != 200:
                fail(f"--auth none: / {r.status}")
        async with bare.get(f"{url}/api/vms") as r:
            if r.status != 200:
                fail(f"--auth none: /api/vms {r.status}")
        try:
            async with bare.ws_connect(
                f"{url}/vm/local/tv/rfb", headers={"Origin": "http://evil.example"}
            ):
                fail("--auth none: a WebSocket from another origin was accepted")
        except aiohttp.WSServerHandshakeError as e:
            if e.status != 403:
                fail(f"--auth none, another origin: {e.status}")
    print("none: open on 127.0.0.1 without a cookie; a cross-origin WebSocket still refused")
    await check_proxy(url, "none")


async def check_tls(url, cert, token):
    import ssl

    context = ssl.create_default_context(cafile=cert)
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(
        cookie_jar=jar, connector=aiohttp.TCPConnector(ssl=context)
    ) as session:
        async with session.get(f"{url}/?token={token}", allow_redirects=False) as r:
            if r.status != 302 or "Secure" not in r.headers["Set-Cookie"]:
                fail(f"https: the token's redirect: {r.status} {r.headers}")
        async with session.get(f"{url}/") as r:
            if r.status != 200:
                fail(f"https: / {r.status}")
        async with session.ws_connect(f"{url.replace('https', 'wss')}/vm/local/tv/rfb") as ws:
            if not (await Stream(ws).rx(12)).startswith(b"RFB 003."):
                fail("https: no RFB greeting over wss")
    print("https: the certificate verified, a Secure cookie, RFB over wss")


class Web:
    """beanjar-web serve ARGS with the stand-in manager; its first line, its stderr in a file."""

    def __init__(self, tmp, env, *args):
        self.err = os.path.join(tmp, "web.err")
        with open(self.err, "w") as err:
            self.proc = subprocess.Popen(
                [sys.executable, WEB, *args, "--beanjar", FAKE],
                stdout=subprocess.PIPE, stderr=err, text=True, env=env,
            )  # fmt: skip
        self.line = self.proc.stdout.readline()
        if not self.line.startswith("beanjar-web: http"):
            self.stop()
            fail(f"beanjar-web {' '.join(args)} printed {self.line!r}: {self.stderr()}")
        self.url = self.line.split()[1].split("?")[0].rstrip("/")

    def stderr(self):
        return open(self.err).read()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            rc = self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            for child in children(self.proc.pid):
                os.kill(child, signal.SIGKILL)
            self.proc.kill()
            self.proc.wait()
            fail("beanjar-web did not end at SIGTERM")
        return rc


def check_unit(tmp, env):
    """start, status, stop as a unit of the test's own name."""
    if (
        not shutil.which("systemd-run")
        or subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True
        ).returncode
    ):
        print("unit: skipped: no working systemctl --user")
        return
    unit = f"check-web-{os.getpid()}"
    env = dict(env, BEANJAR_WEB_UNIT=unit, BEANJAR_LINGER="0")
    token_file = os.path.join(tmp, "unit.token")

    def web(*args):
        return subprocess.run([sys.executable, WEB, *args], capture_output=True, text=True, env=env)

    try:
        r = web("start", "--port", "0", "--token-file", token_file, "--beanjar", FAKE)
        if r.returncode or f"started (systemd user unit {unit}" not in r.stdout:
            fail(f"start: {r.returncode} {r.stdout} {r.stderr}")
        token = open(token_file).read().strip()
        again = web("start", "--port", "0", "--token-file", token_file, "--beanjar", FAKE)
        if again.returncode != 1 or "already running" not in again.stderr:
            fail(f"a second start: {again.returncode} {again.stdout} {again.stderr}")
        st = web("status")
        opens = [line.split()[-1] for line in st.stdout.splitlines() if " open " in line]
        if st.returncode or len(opens) != 1 or not opens[0].endswith(f"/?token={token}"):
            fail(f"status: {st.returncode} {st.stdout}")
        if "listening on 127.0.0.1, port" not in st.stdout:
            fail(f"status says no address: {st.stdout}")

        async def opened(url):
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
                async with s.get(url) as r:
                    return r.status, await r.text()

        status, page = asyncio.run(opened(opens[0]))
        if status != 200 or "Beanjar" not in page:
            fail(f"the unit's URL: {status}")
        print(f"unit: start ({unit}), a second start refused, status's URL opens the list")
        r = web("stop")
        if r.returncode or "stopped" not in r.stdout:
            fail(f"stop: {r.returncode} {r.stdout} {r.stderr}")
        st = web("status")
        if st.returncode != 3 or "not running" not in st.stdout:
            fail(f"status after stop: {st.returncode} {st.stdout}")
        print("unit: stop, then status says not running (3)")
    finally:
        subprocess.run(["systemctl", "--user", "stop", f"{unit}.service"], capture_output=True)
        subprocess.run(
            ["systemctl", "--user", "reset-failed", f"{unit}.service"], capture_output=True
        )
    left = subprocess.run(["systemctl", "--user", "list-units", "--all", "--no-legend",
                           f"{unit}*"], capture_output=True, text=True).stdout.strip()  # fmt: skip
    if left:
        fail(f"the unit is left: {left}")


def tls_files(tmp):
    """A throwaway self-signed certificate for 127.0.0.1, or None without openssl."""
    if not shutil.which("openssl"):
        return None
    cert, key = os.path.join(tmp, "cert.pem"), os.path.join(tmp, "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True,
    )  # fmt: skip
    return cert, key


def children(pid):
    out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(pid)], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def main():
    check_origin_helper()
    for path, package in (
        ("/usr/share/novnc/core/rfb.js", "novnc"),
        ("/usr/share/bootstrap-icons/svg/house.svg", "bootstrap-icons"),
    ):
        if not os.path.isfile(path):
            print(f"Debian's {package} is not installed: skipped")
            sys.exit(77)
    tmp = tempfile.mkdtemp(prefix="check-web-")
    fake = subprocess.Popen(
        [
            sys.executable,
            os.path.join(TOP, "tests/fakes/fake-qemu.py"),
            tmp,
            "--audio",
            "--animate",
        ]
    )
    vnc = web = None
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(tmp, "ready")):
                break
            time.sleep(0.05)
        else:
            fail("fake-qemu did not start")
        port = loopback.port()
        vlog = open(os.path.join(tmp, "vnc.log"), "w")
        vnc = subprocess.Popen(
            [VNC, f"unix:path={tmp}/dbus.sock", "--listen", loopback.ADDRESS, "--port", str(port),
             "--local", os.path.join(tmp, "rfb-local.sock"),
             "--audio-local", os.path.join(tmp, "audio-local.sock"), "--size", f"{W}x{H}", "--no-media",
             "--name", "tv", *(["--video-local", os.path.join(tmp, "video-local.sock"),
                               "--video-encoder", "x264"] if VIDEO else [])],
            stdout=vlog, stderr=subprocess.STDOUT,
        )  # fmt: skip
        for _ in range(100):
            if "beanjar-vnc: ready" in open(vlog.name).read():
                break
            if vnc.poll() is not None:
                fail(f"beanjar-vnc exited: {open(vlog.name).read()}")
            time.sleep(0.1)
        env = dict(os.environ, FAKE_DIR=tmp, FAKE_VNC_PID=str(vnc.pid),
                   XDG_CONFIG_HOME=os.path.join(tmp, "config"))  # fmt: skip
        token_file = os.path.join(tmp, "config", "beanjar", "web.token")
        web = Web(tmp, env, "--port", "0")
        if not web.line.startswith("beanjar-web: http://127.0.0.1:") or "?token=" not in web.line:
            fail(f"beanjar-web printed {web.line!r}")
        token = open(token_file).read().strip()
        if os.stat(token_file).st_mode & 0o777 != 0o600 or f"?token={token}" not in web.line:
            fail(f"the token file: {oct(os.stat(token_file).st_mode)} {web.line!r}")
        asyncio.run(checks(web.url, tmp, token, port))
        asyncio.run(check_proxy(web.url, "token", token))
        calls = log_lines(os.path.join(tmp, "calls.log"))
        if not {"vnc pipe local/tv", "remote pipe local/tv", "vnc audio-pipe local/tv"} <= set(
            calls
        ):
            fail(f"the manager was not asked for the pipes: {calls}")
        # the web viewer's own VNC start takes any free TCP port, not 5900 (a desktop VNC may hold it)
        starts = [c for c in calls if c.startswith("vnc start")]
        if not starts or any(" --port 0" not in c for c in starts):
            fail(f"vnc start without --port 0: {starts}")
        asyncio.run(pending_at_shutdown(web.url, tmp, token))
        if web.stop() != 0:
            fail(f"beanjar-web exited {web.proc.returncode}")
        action_pids = log_lines(os.path.join(tmp, "action-pids"))
        thumbnail_pids = log_lines(os.path.join(tmp, "thumbnail-pids"))
        for pid in map(int, action_pids + thumbnail_pids):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            fail(f"manager action process {pid} survived shutdown")
        os.unlink(os.path.join(tmp, "action-hold"))
        os.unlink(os.path.join(tmp, "thumbnail-hold"))
        with open(os.path.join(tmp, "state-pi-w16r"), "w") as f:
            f.write("Off")  # the manager is authoritative after a web restart
        print("beanjar-web: ended by SIGTERM, every pipe ended")

        web = Web(tmp, env, "--port", "0", "--listen", "localhost")
        asyncio.run(check_restart(web.url, token))
        rc = web.stop()
        if f"?token={token}" not in web.line:
            fail(f"a second run printed {web.line!r}, not the token file's")
        if rc != 0:
            fail(f"the second run exited {rc}")
        if "warning: plain HTTP on localhost" not in web.stderr():
            fail(f"no plain-HTTP warning on localhost: {web.stderr()!r}")
        print(
            "token: made 0600 in the config directory, the same at the next run; plain HTTP warned"
        )

        password_file = os.path.join(tmp, "config", "beanjar", "web.password")
        web = Web(tmp, env, "--port", "0", "--auth", "password")
        password = open(password_file).read().strip()
        if os.stat(password_file).st_mode & 0o777 != 0o600 or len(password) != 20:
            fail(f"the password file: {oct(os.stat(password_file).st_mode)} {len(password)}")
        if "?token" in web.line:
            fail(f"--auth password printed a token: {web.line!r}")
        asyncio.run(check_password(web.url, password))
        asyncio.run(check_proxy(web.url, "password", password))
        web.stop()

        web = Web(tmp, env, "--port", "0", "--auth", "none")
        asyncio.run(check_none(web.url))
        web.stop()
        r = subprocess.run([sys.executable, WEB, "--port", "0", "--auth", "none", "--listen",
                            "0.0.0.0", "--beanjar", FAKE], capture_output=True, text=True,
                           env=env, timeout=30)  # fmt: skip
        if r.returncode != 2 or "--auth none only on a loopback address" not in r.stderr:
            fail(f"--auth none on 0.0.0.0: {r.returncode} {r.stdout} {r.stderr}")
        print("none: refused on 0.0.0.0, before it listens")

        files = tls_files(tmp)
        if files:
            web = Web(tmp, env, "--port", "0", "--certificate", files[0], "--key", files[1])
            if not web.line.startswith("beanjar-web: https://127.0.0.1:"):
                fail(f"https: printed {web.line!r}")
            asyncio.run(check_tls(web.url, files[0], token))
            web.stop()
        else:
            print("https: skipped: openssl is not installed")
        web = None

        check_unit(tmp, env)
    finally:
        if web and web.proc.poll() is None:
            for child in children(web.proc.pid):
                os.kill(child, signal.SIGKILL)
            web.proc.kill()
            web.proc.wait()
        if vnc:
            vnc.send_signal(signal.SIGTERM)
            vnc.wait(timeout=10)
        fake.send_signal(signal.SIGTERM)
        fake.wait(timeout=10)
        subprocess.run(["rm", "-rf", tmp])
    print("ok")


if __name__ == "__main__":
    main()
