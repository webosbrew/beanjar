# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A small RFB client for beanjar-vnc's tests and benchmarks (tests/integration/vnc): the handshake on the
--local socket (no authentication), noVNC's encodings or others, continuous updates as noVNC asks
for them, and the updates parsed (Tight JPEG, ZRLE, Raw, Open H.264 and the pseudo-encodings
beanjar-vnc's neatvnc sends); with framebuffer=True, Raw rectangles drawn into a framebuffer."""

import socket
import struct
import threading

# noVNC 1.6's SetEncodings (core/rfb.js _sendEncodings, depth 24) at its quality 3 and compression
# 2: "novnc" as on plain HTTP, "novnc-secure" with Open H.264 (50) first, as in a secure context
NOVNC = [1, 7, -260, 16, 21, 5, 2, 6, 0, -32 + 3, -256 + 2, -223, -224, -258, -261, -308, -309,
         -312, -313, -307, 0xC0A1E5CE - (1 << 32), -316, 0x574D5664, -239]  # fmt: skip
ENCODINGS = {"novnc": NOVNC, "novnc-secure": NOVNC[:1] + [50] + NOVNC[1:], "zrle": [16, -239]}
NAMES = {0: "raw", 7: "tight", 16: "zrle", 50: "open-h264"}


class Rfb:
    """An RFB client on beanjar-vnc's --local socket (no authentication) asking for continuous
    updates as noVNC does, parsing what it gets: the rectangles' encodings and update count."""

    def __init__(self, path, encodings, framebuffer=False):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.updates = 0
        self.bytes = 0
        self.encodings = {}
        self.error = None
        self.cut_texts = []  # ServerCutText received (beanjar-vnc's beanjar:planes= answers)
        self.stop = False
        self.on_update = None  # called with the client after each whole update, under the lock
        self.take(12)
        self.sock.sendall(b"RFB 003.008\n")
        n = self.byte()
        types = self.take(n)
        if 1 not in types:
            raise RuntimeError(f"no None security type: {list(types)}")
        self.sock.sendall(b"\x01")
        if struct.unpack(">I", self.take(4))[0]:
            raise RuntimeError("security failed")
        self.sock.sendall(b"\x01")
        self.w, self.h = struct.unpack(">HH", self.take(4))
        self.take(16)
        self.take(struct.unpack(">I", self.take(4))[0])
        # with framebuffer: B, G, R, X bytes (fake-qemu's a8r8g8b8), Raw rectangles drawn into
        # self.fb; else noVNC's: 32 bpp, depth 24, little-endian, true colour, R G B at 0, 8, 16
        self.fb = bytearray(self.w * self.h * 4) if framebuffer else None
        shifts = (16, 8, 0) if framebuffer else (0, 8, 16)
        pf = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, *shifts)
        self.sock.sendall(b"\x00\0\0\0" + pf)
        self.sock.sendall(struct.pack(">BxH", 2, len(encodings))
                          + b"".join(struct.pack(">i", e) for e in encodings))  # fmt: skip
        self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.w, self.h))
        self.sock.sendall(struct.pack(">BBHHHH", 150, 1, 0, 0, self.w, self.h))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise EOFError
            self.bytes += len(chunk)
            self.buf += chunk

    def take(self, n):
        self.need(n)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def byte(self):
        return self.take(1)[0]

    def compact(self):
        n, shift = 0, 0
        for _ in range(3):
            b = self.byte()
            n |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        return n

    def rect(self):
        x, y, w, h, enc = struct.unpack(">HHHHi", self.take(12))
        self.encodings[enc] = self.encodings.get(enc, 0) + 1
        if enc == 7:
            control = self.byte()
            if control >> 4 == 9:
                self.take(self.compact())
            elif control >> 4 == 8:
                self.take(3)
            else:
                raise RuntimeError(f"Tight basic compression (control {control:#x}) not parsed")
        elif enc == 16:
            self.take(struct.unpack(">I", self.take(4))[0])
        elif enc == 0:
            data = self.take(w * h * 4)
            if self.fb is not None:
                for row in range(h):
                    o = ((y + row) * self.w + x) * 4
                    self.fb[o : o + w * 4] = data[row * w * 4 : (row + 1) * w * 4]
        elif enc == 50:
            n, _flags = struct.unpack(">II", self.take(8))
            self.take(n)
        elif enc == -239:
            self.take(w * h * 4 + (w + 7) // 8 * h)
        elif enc == -308:
            n = self.byte()
            self.take(3 + 16 * n)
        elif enc == -307:
            self.take(struct.unpack(">I", self.take(4))[0])
        elif enc == -261:
            self.take(1)
        elif enc in (-223, -224, -258, -313, -316, -312):
            pass
        else:
            raise RuntimeError(f"rectangle encoding {enc} not parsed")
        return enc

    def run(self):
        try:
            while not self.stop:
                t = self.byte()
                if t == 0:
                    self.take(1)
                    n = struct.unpack(">H", self.take(2))[0]
                    with self.lock:  # a whole update applied at once
                        for _ in range(n):
                            if self.rect() == -224:
                                break
                        self.updates += 1
                        if self.on_update:
                            self.on_update(self)
                elif t == 2:
                    pass
                elif t == 3:
                    self.take(3)
                    text = self.take(abs(struct.unpack(">i", self.take(4))[0]))
                    with self.lock:
                        self.cut_texts.append(text)
                elif t == 150:
                    pass
                elif t == 248:
                    self.take(3)
                    self.take(4)
                    self.take(self.byte())
                elif t == 250:
                    self.take(3)
                else:
                    raise RuntimeError(f"server message {t} not parsed")
        except (EOFError, OSError):
            pass
        except RuntimeError as e:
            self.error = str(e)

    def picture(self):
        """The framebuffer between updates (B, G, R, X), and the updates so far."""
        with self.lock:
            return bytes(self.fb), self.updates

    def cut_text(self, text):
        self.sock.sendall(struct.pack(">BxxxI", 6, len(text)) + text)

    def close(self):
        self.stop = True
        self.sock.close()
        self.thread.join(5)
