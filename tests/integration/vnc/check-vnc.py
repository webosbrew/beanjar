#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar-vnc against the stand-in QEMU display (tests/fakes/fake-qemu.py), no VM, no window.

    check-vnc.py BEANJAR_VNC VIEWER_HEADLESS

1. viewer-headless renders the fake display's picture to PPM at 960x540: the core's own picture,
   with LG's cursor and without it (--no-cursor).
   By default (composed) beanjar-vnc's frame must match the one with the cursor and no VNC cursor
   is sent; with --native-cursor (LG's cursor as the clients' own), which the checks below use,
   every frame must match the one without.
2. beanjar-vnc serves the same display on a loopback address of its own (tests/lib/loopback.py). A minimal RFB client (raw
   encoding only) reads a full frame, which must match the core's picture, then sends keys as
   keysyms (Super: the remote's Home over IR; "a": the keyboard) and as QEMU extended key events
   (Volume Up: IR), the pointer (a move and a left click, scaled to the 1920x1080 tablet) and a
   wheel click. fake-qemu's input.log must show each.
3. With vncdotool installed, it captures a frame too, which must match.
4. The same over the --websocket listener (as noVNC connects): a frame that matches, and a key.
   LG's cursor as a VNC cursor (the Cursor pseudo-encoding): a client asking for it gets the
   cursor once, scaled like the picture (16x16 at 1920x1080 is 8x8 at 960x540); pointer moves,
   frame requests and the guest's unchanged heartbeat packets send no further update. A padded
   cursor (cursor-control "padded") arrives cropped to its visible box with the hotspot moved; a
   hidden one as a transparent 1x1 cursor; a translucent one ("translucent", alpha 64) keeps its
   alpha as VMware's alpha cursor, where neatvnc has it, and is masked out entirely by the Cursor
   pseudo-encoding. Over WebSocket a client asking as noVNC does gets VMware's cursor, or without
   it the Cursor pseudo-encoding with red and blue swapped. beanjar-vnc's vnc_cursors counts
   exactly those. In a larger desktop a cursor over 128 pixels is shrunk to fit 128x128.
   org.beanjar.Display1.Ir (the core's, on the VM's bus) sends a remote button's edges over IR; a
   button held by a client that leaves the bus is released.
5. SIGTERM ends beanjar-vnc cleanly (exit 0, frames rendered).
6. With --password-file (pycryptodomex for the client's side): only RSA-AES is offered; a
   wrong password is refused; the right one gets a matching frame and a key through, with AES-128
   and AES-256. It refuses --websocket with a password (neatvnc has no RSA-AES over WebSocket).
   --unix serves the same behind the same password on a unix socket, removed at exit.
   --local serves it without authentication (security type None alone) on an owner-only (0600)
   unix socket, beside the password's listeners, removed at exit.
7. It refuses an address other than loopback without a password, and names a port that is taken.
8. Audio, against a second fake-qemu with --audio (its 1 kHz tone at 44.1 kHz): viewer-headless's
   --audio-wav records the tone unchanged (the GTK viewer's audio path). beanjar-vnc's
   --audio-local socket is owner-only (0600); "pcm" gets the PCM header and the tone resampled to
   48 kHz at 192000 bytes a second; "opus" gets the Opus header and 20 ms packets (about 50 a
   second) that libopus decodes to the tone; both at once, each its own; a client that never reads
   holds nobody else up and is disconnected; a bad request is refused. SIGTERM removes the socket.
"""

import base64
import hashlib
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import audiostream  # noqa: E402
import loopback  # noqa: E402

ADDRESS = loopback.ADDRESS

VNC, HEADLESS = sys.argv[1], sys.argv[2]
W, H = 960, 540
IR_HOME, IR_VOLUMEUP = 0x7C, 0x02
QNUM_VOLUMEUP = 0xB0  # E0 30


def fail(message):
    sys.exit(f"check-vnc: {message}")


class AuthFailed(Exception):
    pass


def read_ppm(path):
    with open(path, "rb") as f:
        data = f.read()
    # wv_screenshot's header, "P6\n W H\n255\n": the pixels follow the third newline (a split on
    # whitespace would also eat pixel bytes that are spaces)
    magic, size, maxval, pixels = data.split(b"\n", 3)
    w, h = (int(v) for v in size.split())
    if magic != b"P6" or maxval != b"255" or len(pixels) < w * h * 3:
        fail(f"{path}: not an 8-bit P6")
    return w, h, pixels[: w * h * 3]


def differing(a, b):
    """Pixels whose channels differ by more than 2 (GL rounding)."""
    return sum(
        1 for i in range(0, len(a), 3) if max(abs(a[i + k] - b[i + k]) for k in range(3)) > 2
    )


class WebSocket:
    """RFB's byte stream in WebSocket binary messages (RFC 6455), as noVNC sends it: sendall/recv.
    Like noVNC 1.6 it offers no subprotocol."""

    def __init__(self, port):
        self.s = socket.create_connection((ADDRESS, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(
            (
                f"GET /websockify HTTP/1.1\r\nHost: {ADDRESS}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            c = self.s.recv(1)
            if not c:
                fail(f"websocket handshake closed: {head!r}")
            head += c
        if not head.startswith(b"HTTP/1.1 101"):
            fail(f"websocket handshake refused: {head!r}")
        self.data = b""

    def sendall(self, data):
        mask = os.urandom(4)
        n = len(data)
        size = bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
        self.s.sendall(
            bytes([0x82]) + size + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        )

    def _exact(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                return b
            b += c
        return b

    def recv(self, n):
        while not self.data:
            head = self._exact(2)
            if len(head) < 2:
                return b""
            size = head[1] & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._exact(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._exact(8))[0]
            payload = self._exact(size)
            if head[0] & 0x0F == 8:
                return b""
            if head[0] & 0x0F in (0, 1, 2):
                self.data += payload
        out, self.data = self.data[:n], self.data[n:]
        return out

    def close(self):
        self.s.close()


RSA_AES, RSA_AES256 = 5, 129
VMWARE_CURSOR = 0x574D5664  # VMware's cursor pseudo-encoding (type 1: alpha)
RSA_KEY = []  # the client's RSA key, made once


class RsaAes:
    """RFB's byte stream after the RSA-AES handshake (as neatvnc's src/stream/rsa-aes.c): each
    message is a 2-byte length, the AES-EAX ciphertext and a 16-byte MAC. The nonce is a 16-byte
    little-endian message counter per direction; the length is the associated data."""

    def __init__(self, s, send_key, recv_key):
        self.s, self.keys, self.counts, self.data = s, (send_key, recv_key), [0, 0], b""

    def _cipher(self, way):
        from Cryptodome.Cipher import AES

        nonce = self.counts[way].to_bytes(16, "little")
        self.counts[way] += 1
        return AES.new(self.keys[way], AES.MODE_EAX, nonce=nonce, mac_len=16)

    def sendall(self, data):
        head = struct.pack(">H", len(data))
        c = self._cipher(0)
        c.update(head)
        body, mac = c.encrypt_and_digest(data)
        self.s.sendall(head + body + mac)

    def _exact(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                return None
            b += c
        return b

    def recv(self, n):
        while not self.data:
            head = self._exact(2)
            if head is None:
                return b""
            size = struct.unpack(">H", head)[0]
            body, mac = self._exact(size), self._exact(16)
            if body is None or mac is None:
                return b""
            c = self._cipher(1)
            c.update(head)
            self.data = c.decrypt_and_verify(body, mac)
        out, self.data = self.data[:n], self.data[n:]
        return out

    def close(self):
        self.s.close()


class RFB:
    def __init__(
        self,
        port,
        websocket=False,
        password=None,
        security=RSA_AES256,
        user="check-vnc",
        cursor=False,
    ):
        # (w, h, hot_x, hot_y, RGBX bytes, mask bytes) received; for VMware's alpha cursor
        # (cursor="vmware", asked for before the Cursor pseudo-encoding, as noVNC does) RGBA bytes
        # and no mask
        self.cursors = []
        if isinstance(port, str):  # a unix socket's path (--unix)
            self.s = socket.socket(socket.AF_UNIX)
            self.s.settimeout(10)
            self.s.connect(port)
        else:
            self.s = (
                WebSocket(port)
                if websocket
                else socket.create_connection((ADDRESS, port), timeout=10)
            )
        if not self.rx(12).startswith(b"RFB 003."):
            fail("not an RFB server")
        self.s.sendall(b"RFB 003.008\n")
        self.types = list(self.rx(self.rx(1)[0]))
        if password is None:
            if 1 not in self.types:
                fail(f"no None security type: {self.types}")
            self.s.sendall(b"\x01")
        else:
            if security not in self.types:
                fail(f"no security type {security}: {self.types}")
            self.s.sendall(bytes([security]))
            self.rsa_aes(security, user, password)
        if struct.unpack(">I", self.rx(4))[0] != 0:
            reason = self.rx(struct.unpack(">I", self.rx(4))[0]).decode()
            self.close()
            raise AuthFailed(reason)
        self.s.sendall(b"\x01")  # shared
        self.w, self.h = struct.unpack(">HH", self.rx(4))
        pf = self.rx(16)
        self.name = self.rx(struct.unpack(">I", self.rx(4))[0]).decode()
        # 32 bpp little endian, red at bit 0: R, G, B, X bytes
        self.s.sendall(
            struct.pack(">Bxxx", 0)
            + struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
        )
        # raw, and QEMU's extended key events; with cursor the Cursor pseudo-encoding
        encodings = (
            [0, -258] + ([VMWARE_CURSOR] if cursor == "vmware" else []) + ([-239] if cursor else [])
        )
        self.s.sendall(
            struct.pack(">BxH", 2, len(encodings)) + struct.pack(f">{len(encodings)}i", *encodings)
        )
        del pf

    def rsa_aes(self, security, user, password):
        """The client's side of RSA-AES (RA2 / RA2_256), as TigerVNC and noVNC speak it, then the
        user name and password."""
        from Cryptodome.Cipher import PKCS1_v1_5
        from Cryptodome.PublicKey import RSA

        digest, size = (hashlib.sha256, 32) if security == RSA_AES256 else (hashlib.sha1, 16)
        bits = struct.unpack(">I", self.rx(4))[0]
        n = (bits + 7) // 8
        server_key = self.rx(2 * n)
        server = RSA.construct(
            (int.from_bytes(server_key[:n], "big"), int.from_bytes(server_key[n:], "big"))
        )
        if not RSA_KEY:
            RSA_KEY.append(RSA.generate(2048))
        key = RSA_KEY[0]
        m = key.size_in_bytes()
        client_key = key.n.to_bytes(m, "big") + key.e.to_bytes(m, "big")
        self.s.sendall(struct.pack(">I", m * 8) + client_key)
        # as long as the modulus (PKCS #1); neatvnc 0.9 dropped its leading zero bytes
        length = struct.unpack(">H", self.rx(2))[0]
        if length != m:
            fail(f"RSA-AES: the server's challenge is {length} bytes, not {m}")
        ciphertext = self.rx(length)
        server_random = PKCS1_v1_5.new(key).decrypt(ciphertext, None)
        if not server_random or len(server_random) != size:
            fail("RSA-AES: the server's random does not decrypt")
        client_random = os.urandom(size)
        challenge = PKCS1_v1_5.new(server).encrypt(client_random)
        self.s.sendall(struct.pack(">H", len(challenge)) + challenge)
        # the client sends with SHA(ServerRandom || ClientRandom), the server with the reverse
        self.s = RsaAes(
            self.s,
            digest(server_random + client_random).digest()[:size],
            digest(client_random + server_random).digest()[:size],
        )
        server_part = struct.pack(">I", n * 8) + server_key
        client_part = struct.pack(">I", m * 8) + client_key
        if self.rx(len(digest().digest())) != digest(server_part + client_part).digest():
            fail("RSA-AES: the server's hash does not match")
        self.s.sendall(digest(client_part + server_part).digest())
        subtype = self.rx(1)[0]
        if subtype == 1:  # user name and password
            self.s.sendall(
                bytes([len(user)]) + user.encode() + bytes([len(password)]) + password.encode()
            )
        elif subtype == 2:  # password only
            self.s.sendall(bytes([len(password)]) + password.encode())
        else:
            fail(f"RSA-AES: unknown credential subtype {subtype}")

    def rx(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                fail(f"connection closed ({len(b)} of {n} bytes)")
            b += c
        return b

    def frame(self):
        """A full frame as RGB bytes, top row first."""
        self.s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.w, self.h))
        rgbx = None
        while rgbx is None:
            kind = self.rx(1)[0]
            if kind != 0:
                fail(f"unexpected server message {kind}")
            self.rx(1)
            buffer = bytearray(self.w * self.h * 4)
            got = False
            for _ in range(struct.unpack(">H", self.rx(2))[0]):
                x, y, w, h, encoding = struct.unpack(">HHHHi", self.rx(12))
                if encoding == -258:  # extended key events acknowledged: no pixels
                    continue
                if encoding == -239:  # a cursor: pixels, then a bit mask, rows padded to bytes
                    self.cursors.append((w, h, x, y, self.rx(w * h * 4), self.rx((w + 7) // 8 * h)))
                    continue
                if encoding == VMWARE_CURSOR:  # a type, a padding byte, R, G, B, A pixels
                    kind = self.rx(2)[0]
                    if kind != 1:
                        fail(f"VMware cursor of type {kind}, not 1 (alpha)")
                    self.cursors.append((w, h, x, y, self.rx(w * h * 4), None))
                    continue
                if encoding != 0:
                    fail(f"unexpected encoding {encoding}")
                data = self.rx(w * h * 4)
                for row in range(h):
                    o = ((y + row) * self.w + x) * 4
                    buffer[o : o + w * 4] = data[row * w * 4 : (row + 1) * w * 4]
                got = True
            if got:
                rgbx = buffer
            else:  # answered with pseudo-rectangles only: ask again
                self.s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.w, self.h))
        return bytes(b for i in range(0, len(rgbx), 4) for b in rgbx[i : i + 3])

    def keysym(self, keysym, down):
        self.s.sendall(struct.pack(">BBxxI", 4, down, keysym))

    def keycode(self, qnum, down):
        self.s.sendall(struct.pack(">BBHII", 255, 0, down, 0, qnum))

    def pointer(self, mask, x, y):
        self.s.sendall(struct.pack(">BBHH", 5, mask, x, y))

    def close(self):
        self.s.close()


def wait_log(path, lines, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with open(path) as f:
            text = f.read().splitlines()
        if all(line in text for line in lines):
            return
        time.sleep(0.1)
    fail(f"input.log lacks {[line for line in lines if line not in text]}; has {text}")


IR_CALL = """
import sys
from gi.repository import Gio, GLib
conn = Gio.DBusConnection.new_for_address_sync(
    sys.argv[1],
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
    None, None)
for edge in sys.argv[2:]:
    down, code = edge[0] == "+", int(edge[1:], 0)
    try:
        conn.call_sync("org.beanjar.Display1", "/org/beanjar/Display1", "org.beanjar.Display1",
                       "Ir", GLib.Variant("(ub)", (code, down)), None, 0, 5000, None)
    except GLib.Error as e:
        print(e.message)
"""


def ir_call(bus, *edges):
    """Display1.Ir edges (+CODE down, -CODE up) from a bus client of its own, which then leaves."""
    r = subprocess.run(
        [sys.executable, "-c", IR_CALL, bus, *edges], capture_output=True, text=True, timeout=30
    )
    if r.returncode:
        fail(f"Display1.Ir: {r.stderr}")
    return r.stdout


SHOT_CALL = """
import sys
from gi.repository import Gio, GLib
conn = Gio.DBusConnection.new_for_address_sync(
    sys.argv[1],
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
    None, None)
conn.call_sync("org.beanjar.Display1", "/org/beanjar/Display1", "org.beanjar.Display1",
               "Screenshot", GLib.Variant("(suu)", (sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))),
               None, 0, 20000, None)
"""


def check_full_screenshot(bus, tmp, client, core):
    """The core copies the guest's frame at the stream's size (960x540 here), so a Screenshot at
    the guest's 1920x1080 needs a copy of its own: it must have the guest's detail (its 2x2 box
    average is the stream's frame), and the frames after it the stream's size again."""
    path = os.path.join(tmp, "full.ppm")
    r = subprocess.run(
        [sys.executable, "-c", SHOT_CALL, bus, path, "1920", "1080"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode:
        fail(f"Display1.Screenshot 1920x1080: {r.stderr}")
    w, h, pixels = read_ppm(path)
    if (w, h) != (1920, 1080):
        fail(f"Screenshot 1920x1080 is {w}x{h}")
    import numpy

    full = numpy.frombuffer(pixels, numpy.uint8).reshape(1080, 1920, 3).astype(numpy.int32)
    half = (full[0::2, 0::2] + full[1::2, 0::2] + full[0::2, 1::2] + full[1::2, 1::2] + 2) // 4
    ref = numpy.frombuffer(core[: W * H * 3], numpy.uint8).reshape(H, W, 3).astype(numpy.int32)
    # a Screenshot has LG's cursor even where the stream's frame has none (--native-cursor): the
    # 16x16 cursor is 8x8 halved, so every other pixel must match
    ys, xs = numpy.nonzero(numpy.abs(half - ref).max(axis=2) > 2)
    if len(ys) and (ys.max() - ys.min() >= 10 or xs.max() - xs.min() >= 10):
        fail(
            f"the 1920x1080 screenshot, halved, differs from the stream's frame: {len(ys)} pixels"
            f" within x {xs.min()}-{xs.max()}, y {ys.min()}-{ys.max()}"
        )
    for _ in range(20):
        served = client.frame()
        if not differing(served, core):
            break
        time.sleep(0.2)
    else:
        fail(f"the frame after a full-size screenshot differs in {differing(served, core)} pixels")
    print("screenshot: 1920x1080 from the guest's frame; the stream's frames the same after it")


def check_ir(bus, input_log):
    """Display1.Ir: edges as IR records; a code over 255 refused; a held button released when the
    client that holds it leaves the bus."""
    open(input_log, "w").close()
    ir_call(bus, "+0x40", "-0x40")
    wait_log(input_log, ["IR.Down 0x40", "IR.Up 0x40"])
    if "0 to 255" not in ir_call(bus, "+256"):
        fail("Display1.Ir accepted a code over 255")
    open(input_log, "w").close()
    ir_call(bus, "+0x41")  # down, then the client leaves
    wait_log(input_log, ["IR.Down 0x41", "IR.Up 0x41"])
    print("Display1.Ir: edges reach IR; a client's held button is released when it leaves")


def start(bus, tmp, port, ws_port=0, env=None, listen=ADDRESS, extra=("--native-cursor",)):
    log = open(os.path.join(tmp, f"vnc-{port}.log"), "w")
    argv = [
        VNC,
        bus,
        "--listen",
        listen,
        "--port",
        str(port),
        "--size",
        f"{W}x{H}",
        "--no-media",
        "--name",
        "check-vnc",
        *extra,
    ]
    if ws_port:
        argv += ["--websocket", str(ws_port)]
    p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, env=env)
    return p, log


def wait_ready(p, log):
    for _ in range(100):
        text = open(log.name).read()
        if "beanjar-vnc: ready" in text:
            return text
        if p.poll() is not None:
            fail(f"beanjar-vnc exited {p.returncode}: {text}")
        time.sleep(0.1)
    fail(f"beanjar-vnc not ready: {open(log.name).read()}")


def check_cursor(port, log):
    """LG's cursor as the client's own: sent once, and again only when it changes."""
    client = RFB(port, cursor=True)
    for _ in range(20):
        client.frame()
        if client.cursors:
            break
        time.sleep(0.1)
    if len(client.cursors) != 1:
        fail(f"{len(client.cursors)} cursor updates at connection, not 1")
    w, h, hx, hy, pixels, mask = client.cursors[0]
    if (
        (w, h, hx, hy) != (8, 8, 0, 0)
        or pixels[:3] != bytes((220, 80, 20))
        or any(b != 0xFF for b in mask)
    ):
        fail(
            f"cursor {w}x{h} hotspot {hx},{hy} colour {tuple(pixels[:3])} mask {mask.hex()}: not LG's 16x16 at half size"
        )
    print(f"cursor: {w}x{h} hotspot {hx},{hy}, LG's 16x16 scaled like the picture")
    sent = log_cursors(log)
    # pointer moves and frame requests, across the guest's heartbeats (one a second, unchanged)
    end = time.monotonic() + 2.5
    moves = 0
    while time.monotonic() < end:
        client.pointer(0, 100 + moves % 300, 100 + moves % 200)
        moves += 1
        client.frame()
        time.sleep(0.05)
    if len(client.cursors) != 1 or log_cursors(log) != sent:
        fail(
            f"an unchanged cursor was sent again: {len(client.cursors) - 1} updates received, "
            f"{log_cursors(log) - sent} sent, over {moves} pointer moves and frames"
        )
    print(f"cursor: no update over {moves} pointer moves and frame requests and 2 heartbeats")

    def next_cursor(command):
        with open(os.path.join(os.path.dirname(log), "cursor-control"), "w") as f:
            f.write(command)
        before = len(client.cursors)
        for _ in range(40):
            client.frame()
            if len(client.cursors) > before:
                return client.cursors[-1]
            time.sleep(0.05)
        fail(f"no cursor update after {command}")

    w, h, hx, hy, pixels, mask = next_cursor("padded")
    # the 20x30 box at 10,12 of 64x64, hotspot 14,15: cropped, then half size
    if (w, h, hx, hy) != (10, 15, 2, 1) or any(
        mask[r * 2] != 0xFF or mask[r * 2 + 1] != 0xC0 for r in range(h)
    ):
        fail(
            f"padded cursor arrived as {w}x{h} hotspot {hx},{hy} mask {mask.hex()}, not 10x15 hotspot 2,1 opaque"
        )
    print(f"cursor: a padded one cropped to {w}x{h} hotspot {hx},{hy}")
    w, h, hx, hy, pixels, mask = next_cursor("legacy-hidden")
    if (w, h) != (1, 1) or mask[0] & 0x80:
        fail(f"hidden cursor arrived as {w}x{h} mask {mask.hex()}, not a transparent 1x1")
    print("cursor: hidden is a transparent 1x1")
    time.sleep(1.2)  # a hidden heartbeat: nothing new
    client.frame()
    if len(client.cursors) != 3:
        fail(f"{len(client.cursors)} cursor updates, not 3")
    client.close()


def check_alpha_cursor(port, log):
    """A translucent cursor (LG's 16x16 at alpha 64): VMware's alpha cursor keeps its alpha; the
    Cursor pseudo-encoding's 1-bit mask (alpha above half) drops all of it."""
    with open(os.path.join(os.path.dirname(log), "cursor-control"), "w") as f:
        f.write("translucent")
    received = {}
    for kind in ("vmware", True):
        client = RFB(port, cursor=kind)
        for _ in range(40):
            client.frame()
            if (
                client.cursors
                and client.cursors[-1][:2] == (8, 8)
                and client.cursors[-1][4][3] < 255
            ):
                break
            time.sleep(0.05)
        received[kind] = client.cursors[-1]
        client.close()
    w, h, hx, hy, pixels, mask = received[True]
    if (w, h) != (8, 8) or any(mask):
        fail(
            f"translucent cursor with the Cursor encoding: {w}x{h} mask {mask.hex()}, not 8x8 empty"
        )
    w, h, hx, hy, pixels, mask = received["vmware"]
    if mask is not None:
        print("cursor: neatvnc has no VMware cursor; a translucent one is lost to the 1-bit mask")
        return
    off = [
        tuple(pixels[i : i + 4])
        for i in range(0, len(pixels), 4)
        if not (
            abs(pixels[i + 3] - 64) <= 2
            and all(abs(pixels[i + k] - c) <= 4 for k, c in enumerate((220, 80, 20)))
        )
    ]
    if (w, h, hx, hy) != (8, 8, 0, 0) or off:
        fail(
            f"translucent VMware cursor {w}x{h} hotspot {hx},{hy}: {len(off)} pixels not "
            f"(220, 80, 20) at alpha 64, e.g. {off[:2]}"
        )
    print(
        f"cursor: a translucent one as VMware's alpha cursor, {w}x{h} at alpha 64 (the mask drops it)"
    )


def check_large(bus, tmp):
    """A cursor larger than 128 in the desktop is shrunk to fit 128x128 (noVNC's CSS cursor)."""
    port = loopback.port()
    vnc, log = start(
        bus, tmp, port, extra=["--native-cursor", "--size", "2880x1620"]
    )  # 1.5 times 1920x1080
    try:
        wait_ready(vnc, log)
        with open(os.path.join(tmp, "cursor-control"), "w") as f:
            f.write("large")
        client = RFB(port, cursor=True)
        for _ in range(40):
            client.frame()
            if client.cursors and client.cursors[-1][1] > 1:
                break
            time.sleep(0.1)
        w, h, hx, hy, pixels, mask = client.cursors[-1]
        # 200x240 is 300x360 in this desktop: shrunk to 107x128
        if (w, h, hx, hy) != (107, 128, 53, 64):
            fail(
                f"a 300x360 cursor arrived as {w}x{h} hotspot {hx},{hy}, not 107x128 hotspot 53,64"
            )
        client.close()
        print(f"cursor: a 300x360 one shrunk to {w}x{h} hotspot {hx},{hy}")
    finally:
        vnc.send_signal(signal.SIGTERM)
        if vnc.wait(timeout=10) != 0:
            fail(f"beanjar-vnc --size 2880x1620 exited {vnc.returncode}: {open(log.name).read()}")
        log.close()


def log_cursors(path):
    return sum(
        1 for line in open(path).read().splitlines() if line.startswith("beanjar-vnc: cursor ")
    )


def expected_gl(extra):
    """The renderer beanjar-vnc's GL line should name (main.c gl_driver): llvmpipe with
    --gl-adapter software; on WSL (/dev/dxg) Mesa's d3d12 unless the environment chose."""
    if "software" in extra:
        return "llvmpipe"
    if os.path.exists("/dev/dxg") and not (
        os.environ.get("GALLIUM_DRIVER") or os.environ.get("LIBGL_ALWAYS_SOFTWARE")
    ):
        return "D3D12"
    return None


def check_composed(bus, core, tmp, extra=()):
    """The default: LG's cursor in the picture, no VNC cursor; the GL it was composed with
    reported, and the picture the same on it (extra: --gl-adapter software, llvmpipe)."""
    port = loopback.port()
    vnc, log = start(bus, tmp, port, extra=extra)
    try:
        text = wait_ready(vnc, log)
        gl = [x for x in text.splitlines() if x.startswith("GL ")]
        want = expected_gl(extra)
        if not gl or (want and want not in gl[0]):
            fail(f"composed: GL {gl[0] if gl else 'not reported'}, expected {want}")
        client = RFB(port, cursor=True)
        for _ in range(20):
            served = client.frame()
            if not differing(served, core):
                break
            time.sleep(0.2)
        else:
            fail(f"composed frame differs from the core's in {differing(served, core)} pixels")
        if client.cursors:
            fail("the composed default sent a VNC cursor")
        client.close()
        print(
            "composed: the frame has LG's cursor, as the core draws it; no VNC cursor "
            f"({gl[0].split('; ')[1]})"
        )
    finally:
        vnc.send_signal(signal.SIGTERM)
        if vnc.wait(timeout=10) != 0:
            fail(f"beanjar-vnc (composed) exited {vnc.returncode}: {open(log.name).read()}")
        log.close()


def check_password(bus, core, input_log, tmp):
    """--password-file: RSA-AES only, a wrong password refused, the right one let in."""
    try:
        import Cryptodome  # noqa: F401
    except ImportError:
        print("pycryptodomex not installed: RSA-AES not checked")
        return
    port, ws_port = loopback.port(), loopback.port()
    password = "correct horse"
    password_file = os.path.join(tmp, "vnc.password")
    with open(password_file, "w") as f:
        f.write(password + "\n")
    extra = ("--native-cursor", "--password-file", password_file)
    vnc, log = start(bus, tmp, port, ws_port, extra=extra)
    rc = vnc.wait(timeout=10)
    text = open(log.name).read()
    if rc == 0 or "--websocket cannot be used with a password" not in text:
        fail(f"--websocket with a password: exit {rc}: {text}")
    print("refused: --websocket with a password")
    unix = os.path.join(tmp, "vnc.sock")
    local = os.path.join(tmp, "rfb-local.sock")
    vnc, log = start(bus, tmp, port, extra=(*extra, "--unix", unix, "--local", local))
    try:
        text = wait_ready(vnc, log)
        if (
            f"listening vnc://{ADDRESS}:{port}" not in text
            or "listening ws://" in text
            or "password required" not in text
        ):
            fail(f"beanjar-vnc's start lines: {text}")
        for security in (RSA_AES, RSA_AES256):
            try:
                RFB(port, password="wrong", security=security)
            except AuthFailed as e:
                print(f"password: a wrong one refused over security type {security} ({e})")
            else:
                fail(f"a wrong password was accepted over security type {security}")
        for security in (RSA_AES256, RSA_AES):
            client = RFB(port, password=password, security=security)
            if sorted(client.types) != [RSA_AES, RSA_AES256]:
                fail(f"with a password the server offers {client.types}, not RSA-AES alone")
            if (client.w, client.h, client.name) != (W, H, "check-vnc"):
                fail(f"server init {client.w}x{client.h} {client.name!r}")
            for _ in range(20):
                served = client.frame()
                if not differing(served, core):
                    break
                time.sleep(0.2)
            else:
                fail(f"RSA-AES frame differs from the core's in {differing(served, core)} pixels")
            open(input_log, "w").close()
            client.keysym(ord("c"), 1)
            client.keysym(ord("c"), 0)
            wait_log(input_log, ["Keyboard.Press 46", "Keyboard.Release 46"])
            client.close()
            time.sleep(0.2)  # gone before the next one
            print(
                f"password: the right one gets a matching frame and a key through, security type {security}"
            )
        if f"listening unix:{unix}" not in text:
            fail(f"beanjar-vnc's start lines lack the unix socket: {text}")
        try:
            RFB(unix, password="wrong")
        except AuthFailed:
            pass
        else:
            fail("a wrong password was accepted on the unix socket")
        client = RFB(unix, password=password)
        if sorted(client.types) != [RSA_AES, RSA_AES256] or (client.w, client.h) != (W, H):
            fail(f"unix socket: types {client.types}, {client.w}x{client.h}")
        client.frame()
        client.close()
        print("password: the unix socket (--unix) offers RSA-AES alone and lets the right one in")
        if f"listening unix:{local} (local, no password)" not in text:
            fail(f"beanjar-vnc's start lines lack the local socket: {text}")
        mode = os.stat(local).st_mode & 0o777
        if mode != 0o600:
            fail(f"the local socket has mode {mode:o}, not 600")
        client = RFB(local)
        if client.types != [1] or (client.w, client.h) != (W, H):
            fail(f"local socket: types {client.types}, {client.w}x{client.h}, not None alone")
        for _ in range(20):
            served = client.frame()
            if not differing(served, core):
                break
            time.sleep(0.2)
        else:
            fail(f"local socket frame differs from the core's in {differing(served, core)} pixels")
        open(input_log, "w").close()
        client.keysym(ord("d"), 1)
        client.keysym(ord("d"), 0)
        wait_log(input_log, ["Keyboard.Press 32", "Keyboard.Release 32"])
        client.close()
        print("local: the --local socket (0600) offers None alone, a matching frame, a key")
        if "authentication failed" not in open(log.name).read():
            fail("beanjar-vnc logged no failed authentication")
    finally:
        vnc.send_signal(signal.SIGTERM)
        if vnc.wait(timeout=10) != 0:
            fail(f"beanjar-vnc with a password exited {vnc.returncode}: {open(log.name).read()}")
        log.close()
    if os.path.exists(unix) or os.path.exists(local):
        fail("beanjar-vnc left its unix sockets behind")


def check_refusals(bus, tmp):
    """No password on an address other than loopback; a port another server holds."""
    port = loopback.port()
    vnc, log = start(bus, tmp, port, listen="0.0.0.0")
    rc = vnc.wait(timeout=10)
    text = open(log.name).read()
    if (
        rc == 0
        or "not a loopback address" not in text
        or "--password-file" not in text
        or "listening" in text
    ):
        fail(f"0.0.0.0 without a password: exit {rc}: {text}")
    print("refused: 0.0.0.0 without a password")
    vnc, log = start(bus, tmp, port, extra=("--unix", os.path.join(tmp, "refused.sock")))
    rc = vnc.wait(timeout=10)
    text = open(log.name).read()
    if rc == 0 or "--unix needs a --password-file" not in text:
        fail(f"--unix without a password: exit {rc}: {text}")
    print("refused: --unix without a password")
    with socket.socket() as held:
        held.bind((ADDRESS, 0))
        held.listen()
        port = held.getsockname()[1]
        vnc, log = start(bus, tmp, port)
        rc = vnc.wait(timeout=10)
    text = open(log.name).read()
    if rc == 0 or f"cannot listen on {ADDRESS} port {port}" not in text or "--port" not in text:
        fail(f"a port in use: exit {rc}: {text}")
    print(f"refused: a port in use ({text.strip().splitlines()[-1]})")


def audio_client(path, fmt, seconds):
    """(header, body, seconds read) of a stream fmt read from the socket at path."""
    with socket.socket(socket.AF_UNIX) as c:
        c.connect(path)
        c.sendall(fmt.encode() + b"\n")
        c.settimeout(5)
        data = b""
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            chunk = c.recv(65536)
            if not chunk:
                break
            data += chunk
        took = time.monotonic() - start
    try:
        body = audiostream.header(data, fmt)
    except ValueError as e:
        fail(f"audio {fmt}: {e}")
    return body, took


def check_audio(tmp):
    d = os.path.join(tmp, "audio")
    fake = subprocess.Popen(
        [sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), d, "--audio"]
    )
    vnc = None
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(d, "ready")):
                break
            time.sleep(0.05)
        else:
            fail("fake-qemu --audio did not start")
        bus = f"unix:path={d}/dbus.sock"
        # the GTK viewer's path (audio.c: Init, Writes, the WAV) is untouched by the tap
        wav = os.path.join(d, "headless.wav")
        r = subprocess.run(
            [HEADLESS, bus, "--seconds", "2", "--size", f"{W}x{H}", "--no-media", "--audio-wav", wav],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        if r.returncode or not os.path.exists(wav):
            fail(f"viewer-headless --audio-wav failed ({r.returncode}): {r.stderr}")
        with wave.open(wav) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (44100, 2, 2):
                fail(f"viewer-headless's WAV is {w.getparams()}")
            pcm = w.readframes(w.getnframes())
        _secs, l_db, _r, freq, same = audiostream.analyse(pcm)
        if abs(freq * 44100 / 48000 - 1000) > 10 or abs(l_db - audiostream.TONE_RMS_DB) > 0.5:
            fail(f"viewer-headless's WAV: {freq * 44100 / 48000:.0f} Hz, {l_db:.1f} dBFS")
        print(f"audio: viewer-headless --audio-wav records the tone (44.1 kHz, {l_db:.1f} dBFS)")

        sock = os.path.join(d, "audio-local.sock")
        port = loopback.port()
        vnc, log = start(bus, d, port, extra=("--audio-local", sock))
        text = wait_ready(vnc, log)
        if f"listening unix:{sock} (audio" not in text:
            fail(f"no audio listening line: {text}")
        mode = os.stat(sock).st_mode & 0o777
        if mode != 0o600:
            fail(f"the audio socket has mode {mode:o}, not 600")
        time.sleep(0.5)
        body, took = audio_client(sock, "pcm", 2)
        rate = len(body) / took
        if abs(rate - 192000) > 192000 * 0.1:
            fail(f"audio pcm: {rate:.0f} bytes a second")
        try:
            print(audiostream.check_tone(body, "audio pcm") + f", {rate:.0f} B/s")
            body, took = audio_client(sock, "opus", 2)
            pkts, _rest = audiostream.packets(body)
            if abs(len(pkts) / took - 100) > 15:
                fail(f"audio opus: {len(pkts)} packets in {took:.1f} s")
            print(audiostream.check_lowdelay(body))
            print(audiostream.check_tone(audiostream.to_pcm(body, "opus"), "audio opus")
                  + f", {len(pkts)} packets, {len(body) * 8 / took / 1000:.0f} kbit/s")  # fmt: skip
        except ValueError as e:
            fail(str(e))

        # both formats at once, and a client that requests PCM and never reads
        stalled = socket.socket(socket.AF_UNIX)
        stalled.connect(sock)
        stalled.sendall(b"pcm\n")
        results = {}

        def read(fmt):
            results[fmt] = audio_client(sock, fmt, 3)

        threads = [threading.Thread(target=read, args=(f,)) for f in ("pcm", "opus")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            for fmt in ("pcm", "opus"):
                body, _took = results[fmt]
                pcm = audiostream.to_pcm(body, fmt)
                print(
                    audiostream.check_tone(
                        pcm, f"audio {fmt} beside the other and a stalled client"
                    )
                )
        except ValueError as e:
            fail(str(e))
        stalled.settimeout(5)
        end = time.monotonic() + 5
        got = 0
        while time.monotonic() < end:  # its queue, then the end: disconnected as too slow
            chunk = stalled.recv(65536)
            if not chunk:
                break
            got += len(chunk)
        else:
            fail("the stalled audio client was not disconnected")
        stalled.close()
        if got > 192000:
            fail(f"the stalled audio client had {got} bytes queued, more than a second")
        print(f"audio: a client that never reads is disconnected ({got} bytes were queued for it)")

        with socket.socket(socket.AF_UNIX) as c:
            c.connect(sock)
            c.sendall(b"flac\n")
            c.settimeout(5)
            if c.recv(16):
                fail("a bad audio request got an answer")
        print("audio: a bad request is refused")

        vnc.send_signal(signal.SIGTERM)
        rc = vnc.wait(timeout=10)
        log.close()
        text = open(log.name).read()
        if rc != 0 or os.path.exists(sock):
            fail(
                f"beanjar-vnc exited {rc}, the audio socket {'left' if os.path.exists(sock) else 'gone'}: {text}"
            )
        line = next((x for x in text.splitlines() if "beanjar-vnc: audio clients=" in x), "")
        if "slow_disconnects=1" not in line or "refused=1" not in line:
            fail(f"audio counters: {line!r}")
        print(line)
        vnc = None
    finally:
        if vnc and vnc.poll() is None:
            vnc.kill()
            vnc.wait()
        fake.send_signal(signal.SIGTERM)
        fake.wait(timeout=10)


def main():
    tmp = tempfile.mkdtemp(prefix="check-vnc-")
    fake = subprocess.Popen([sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), tmp])
    vnc = None
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(tmp, "ready")):
                break
            time.sleep(0.05)
        else:
            fail("fake-qemu did not start")
        bus = f"unix:path={tmp}/dbus.sock"
        pictures = []
        for name, extra in (("core.ppm", []), ("bare.ppm", ["--no-cursor"])):
            reference = os.path.join(tmp, name)
            r = subprocess.run(
                [
                    HEADLESS,
                    bus,
                    "--seconds",
                    "2",
                    "--size",
                    f"{W}x{H}",
                    "--no-media",
                    "--no-audio",
                    "--screenshot",
                    reference,
                    *extra,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if r.returncode or not os.path.exists(reference):
                fail(f"viewer-headless failed ({r.returncode}): {r.stderr}")
            rw, rh, picture = read_ppm(reference)
            if (rw, rh) != (W, H):
                fail(f"reference is {rw}x{rh}")
            pictures.append(picture)
        with_cursor, core = pictures  # --native-cursor's frames have no cursor
        if not differing(with_cursor, core):
            fail("the core's picture is the same with and without LG's cursor")
        check_composed(bus, with_cursor, tmp)
        check_composed(bus, with_cursor, tmp, ("--gl-adapter", "software"))

        port, ws_port = loopback.port(), loopback.port()
        log = open(os.path.join(tmp, "vnc.log"), "w")
        vnc = subprocess.Popen(
            [
                VNC,
                bus,
                "--listen",
                ADDRESS,
                "--port",
                str(port),
                "--websocket",
                str(ws_port),
                "--size",
                f"{W}x{H}",
                "--no-media",
                "--name",
                "check-vnc",
                "--native-cursor",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        for _ in range(100):
            try:
                socket.create_connection((ADDRESS, port), timeout=1).close()
                break
            except OSError:
                if vnc.poll() is not None:
                    fail(f"beanjar-vnc exited {vnc.returncode}: {open(log.name).read()}")
                time.sleep(0.1)
        time.sleep(0.5)  # the scanout's frame after the start's black one

        client = RFB(port)
        if (client.w, client.h, client.name) != (W, H, "check-vnc"):
            fail(f"server init {client.w}x{client.h} {client.name!r}")
        for _ in range(20):
            served = client.frame()
            if not differing(served, core):
                break
            time.sleep(0.2)
        else:
            fail(f"VNC frame differs from the core's in {differing(served, core)} pixels")
        print(f"frame: {W}x{H}, matches the core's render without the cursor")
        check_full_screenshot(bus, tmp, client, core)

        input_log = os.path.join(tmp, "input.log")
        open(input_log, "w").close()
        client.keysym(0xFFEB, 1)
        client.keysym(0xFFEB, 0)  # Super_L: Home
        client.keysym(ord("a"), 1)
        client.keysym(ord("a"), 0)  # the keyboard's A (qnum 0x1e)
        client.keycode(QNUM_VOLUMEUP, 1)
        client.keycode(QNUM_VOLUMEUP, 0)
        client.pointer(0, 300, 200)
        client.pointer(1, 300, 200)
        client.pointer(0, 300, 200)
        client.pointer(8, 300, 200)
        client.pointer(0, 300, 200)  # wheel up: QEMU button 3
        wait_log(
            input_log,
            [
                f"IR.Down {IR_HOME:#04x}",
                f"IR.Up {IR_HOME:#04x}",
                "Keyboard.Press 30",
                "Keyboard.Release 30",
                f"IR.Down {IR_VOLUMEUP:#04x}",
                f"IR.Up {IR_VOLUMEUP:#04x}",
                "Mouse.SetAbsPosition 600 400",
                "Mouse.Press 0",
                "Mouse.Release 0",
                "Mouse.Press 3",
                "Mouse.Release 3",
            ],
        )
        print("input: keysym and extended key events, pointer, click and wheel reach the core")
        check_ir(bus, input_log)
        client.close()

        try:
            from vncdotool import api
            from vncdotool.client import VNCDoToolFactory
        except ImportError:
            print("vncdotool not installed: its capture not checked")
        else:

            class Factory(VNCDoToolFactory):
                # vncdotool drops an update holding only the extended key event pseudo-rectangle,
                # which neatvnc sends in answer to the first request, and then waits forever
                qemu_extended_key = False

            png = os.path.join(tmp, "vncdotool.png")
            c = api.connect(f"{ADDRESS}::{port}", timeout=10, factory_class=Factory)
            try:
                c.captureScreen(png)
            finally:
                c.disconnect()
                api.shutdown()
            from PIL import Image

            image = Image.open(png).convert("RGB")
            if image.size != (W, H):
                fail(f"vncdotool captured {image.size}")
            n = differing(image.tobytes(), core)
            if n:
                fail(f"vncdotool's frame differs from the core's in {n} pixels")
            print(f"vncdotool: {W}x{H} captured, matches the core's render")

        client = RFB(ws_port, websocket=True)
        if (client.w, client.h, client.name) != (W, H, "check-vnc"):
            fail(f"websocket server init {client.w}x{client.h} {client.name!r}")
        served = client.frame()
        if differing(served, core):
            fail(f"websocket frame differs from the core's in {differing(served, core)} pixels")
        open(input_log, "w").close()
        client.keysym(ord("b"), 1)
        client.keysym(ord("b"), 0)
        wait_log(input_log, ["Keyboard.Press 48", "Keyboard.Release 48"])
        client.close()
        print("websocket: frame matches the core's render, a key reaches the core")
        # As noVNC asks: VMware's alpha cursor, LG's (220, 80, 20) as it is, where neatvnc has
        # it; else the Cursor pseudo-encoding, whose pixels noVNC reads as BGRX (beanjar-vnc's
        # set_cursor), so over WebSocket with red and blue swapped
        client = RFB(ws_port, websocket=True, cursor="vmware")
        for _ in range(20):
            client.frame()
            if client.cursors:
                break
            time.sleep(0.1)
        if not client.cursors:
            fail("no websocket cursor")
        vmware = client.cursors[-1][5] is None
        want = bytes((220, 80, 20, 255)) if vmware else bytes((20, 80, 220))
        if client.cursors[-1][4][: len(want)] != want:
            fail(
                f"websocket cursor {client.cursors[-1][:4]} ({'VMware' if vmware else 'Cursor'}) "
                f"is {client.cursors[-1][4][:4]!r}, not {want!r}"
            )
        client.close()
        print(
            "websocket: the cursor as VMware's alpha cursor, colours as they are"
            if vmware
            else "websocket: the cursor (no VMware cursor in neatvnc) has red and blue swapped, for noVNC"
        )

        check_cursor(port, log.name)
        check_alpha_cursor(port, log.name)

        vnc.send_signal(signal.SIGTERM)
        rc = vnc.wait(timeout=10)
        log.close()
        text = open(log.name).read()
        if rc != 0 or "vnc_frames=" not in text:
            fail(f"beanjar-vnc exited {rc}: {text}")
        # the start's hidden one, LG's, the padded one, the hidden one, the translucent one
        if f"vnc_cursors={log_cursors(log.name)}" not in text or log_cursors(log.name) != 5:
            fail(f"beanjar-vnc sent {log_cursors(log.name)} cursors, not 5: {text}")
        print(next(line for line in text.splitlines() if "vnc_frames=" in line))
        vnc = None

        check_password(bus, core, input_log, tmp)
        check_refusals(bus, tmp)
        check_large(bus, tmp)
        check_audio(tmp)
    finally:
        if vnc and vnc.poll() is None:
            vnc.kill()
            vnc.wait()
        fake.send_signal(signal.SIGTERM)
        fake.wait(timeout=10)
        subprocess.run(["rm", "-rf", tmp])
    print("ok")


if __name__ == "__main__":
    main()
