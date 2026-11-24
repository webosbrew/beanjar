#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Real helper IPC/decode tests without a VM or a GPU.

Run MEDIAHUB=/path/to/media-helper python3 tests/integration/media-helper/test_media_helper.py.
FFmpeg/ffprobe generate the compressed fixture and independent pixel/PCM references.
These tests qualify software decode and ownership, not DMA-BUF consumer fencing.
"""

import array
import ctypes as C
import fcntl
import importlib.util
import json
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HUB = os.environ.get("MEDIAHUB") or os.path.join(
    os.path.dirname(__file__), "../../../build/media-helper/media-helper"
)
SPEC = importlib.util.spec_from_file_location(
    "fake_media", os.path.join(os.path.dirname(__file__), "../../fakes/fake-media.py")
)
FAKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FAKE)
MAGIC, OPEN, CAPS, BUFFER, STATE, FLUSH, EOS, CLOSE = 0x324D5357, 1, 2, 3, 4, 5, 6, 7
DECODE, PICTURE, POOL, NV12, YUY2, LAST = 8, 9, 10, 0x3231564E, 0x32595559, 1
SHARED = 1 << 1  # webos_media_picture.flags; DECODE's WEBOS_MEDIA_DECODE_SHARED is 1
POOL_SIZE, GRANULE, RING = 128 << 20, 64 << 10, 64  # include/webos-media.h, "The pool"
LIVE, RETIRING = 1, 2
HM_MAGIC, HELLO, ACK, SNAPSHOT, VIDEO, PCM, RELEASE = 0x31444D57, 1, 2, 3, 4, 5, 6
NONE = -(1 << 63)
# Target-native tools for cross-arch runs (see fake-media.py probe()).
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")


class Open(C.Structure):
    _fields_ = [
        ("kind", C.c_uint32),
        ("pid", C.c_int32),
        ("channel", C.c_int32),
        ("pad", C.c_uint32),
        ("element", C.c_char * 28),
        ("output", C.c_uint32),
    ]


class State(C.Structure):
    _fields_ = [
        ("playing", C.c_uint32),
        ("reserved", C.c_uint32),
        ("base", C.c_int64),
        ("pause", C.c_int64),
        ("rate", C.c_double),
    ]


class Object(C.Structure):
    _fields_ = [("size", C.c_uint64), ("modifier", C.c_uint64)]


class Plane(C.Structure):
    _fields_ = [("object", C.c_uint32), ("stride", C.c_uint32), ("offset", C.c_uint64)]


class Layer(C.Structure):
    _fields_ = [("fourcc", C.c_uint32), ("planes", C.c_uint32), ("plane", Plane * 4)]


class Descriptor(C.Structure):
    _fields_ = [
        ("magic", C.c_uint32),
        ("version", C.c_uint32),
        ("type", C.c_uint32),
        ("flags", C.c_uint32),
        ("generation", C.c_uint64),
        ("token", C.c_uint64),
        ("offset", C.c_int64),
        ("delta", C.c_int64),
        ("open", Open),
        ("state", State),
        ("session", C.c_uint32),
        ("status", C.c_uint32),
        ("format", C.c_uint32),
        ("width", C.c_uint32),
        ("height", C.c_uint32),
        ("objects", C.c_uint32),
        ("layers", C.c_uint32),
        ("colorspace", C.c_uint32),
        ("range", C.c_uint32),
        ("samples", C.c_uint32),
        ("pts", C.c_int64),
        ("object", Object * 4),
        ("layer", Layer * 4),
        ("preference", C.c_char * 32),
    ]


assert C.sizeof(Descriptor) == 560


def message(kind, payload=b"", seq=1):
    now = time.monotonic_ns()
    words = [
        MAGIC,
        kind,
        len(payload),
        seq,
        now & 0xFFFFFFFF,
        now >> 32,
        now & 0xFFFFFFFF,
        now >> 32,
    ]
    return (
        struct.pack("<IHHIIqqII", MAGIC, kind, 0, len(payload), seq, now, now, FAKE.check(words), 0)
        + payload
    )


def receive(sock, timeout=3):
    sock.settimeout(timeout)
    data, ancillary, flags, _ = sock.recvmsg(560, socket.CMSG_SPACE(4 * 4))
    fds = []
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.extend(array.array("i", value))
    try:
        if len(data) != 560 or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise AssertionError("incomplete helper descriptor")
        d = Descriptor.from_buffer_copy(data)
        assert (d.magic, d.version) == (HM_MAGIC, 1)
        assert len(fds) == d.objects
        objects = []
        for fd, obj in zip(fds, d.object):
            assert os.fstat(fd).st_size == obj.size
            seals = fcntl.fcntl(fd, fcntl.F_GET_SEALS)
            assert seals & 15 == 15, "software storage is not immutable"
            objects.append(os.pread(fd, obj.size, 0))
        return d, objects
    finally:
        for fd in fds:
            os.close(fd)


def release(sock, d):
    sock.send(
        bytes(
            Descriptor(
                magic=HM_MAGIC, version=1, type=RELEASE, generation=d.generation, token=d.token
            )
        )
    )


def generate_fixture(directory):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "clip.mp4")
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=997:sample_rate=48000",
            "-t",
            "1.6",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-bf",
            "0",
            "-g",
            "250",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            path,
        ],
        check=True,
    )
    with open(os.path.join(directory, "packets.json"), "w") as f:
        json.dump(FAKE.probe(path), f)
    for name, args in (
        ("video.yuv", ["-map", "0:v", "-f", "rawvideo", "-pix_fmt", "yuv420p"]),
        ("audio.f32", ["-map", "0:a", "-f", "f32le", "-ac", "2", "-ar", "48000"]),
    ):
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-i", path] + args + [os.path.join(directory, name)],
            check=True,
        )


def recv_fd_msg(sock):
    """One ivshmem server message: (value, fd or -1)."""
    data, anc, _, _ = sock.recvmsg(8, socket.CMSG_SPACE(4))
    fd = -1
    for level, kind, value in anc:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fd = array.array("i", value[:4])[0]
    return struct.unpack("<q", data)[0], fd


class Qemu:
    """QEMU's ivshmem-doorbell and the guest kernel's view of the pool (media-pool.ko): connects
    to the helper's server, maps the pool, and writes what the guest kernel writes."""

    def __init__(self, path):
        import mmap

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(path)
        assert recv_fd_msg(self.sock) == (0, -1)  # version
        self.id, _ = recv_fd_msg(self.sock)
        v, self.shm = recv_fd_msg(self.sock)
        assert v == -1 and self.shm >= 0
        self.host_peer, self.host_ev = recv_fd_msg(self.sock)
        v, self.own_ev = recv_fd_msg(self.sock)
        assert v == self.id
        self.size = os.fstat(self.shm).st_size
        self.mem = mmap.mmap(self.shm, self.size)

    def close(self):
        self.mem.close()
        for fd in (self.shm, self.host_ev, self.own_ev):
            os.close(fd)
        self.sock.close()

    def u32(self, off, value=None):
        if value is None:
            return struct.unpack_from("<I", self.mem, off)[0]
        struct.pack_into("<I", self.mem, off, value & 0xFFFFFFFF)

    def port(self, p):
        return 64 + p * 96

    def extents(self, p):
        return [struct.unpack_from("<IIII", self.mem, self.port(p) + 16 * e) for e in range(4)]

    def claim(self, p, value):
        self.u32(self.port(p) + 80, value)

    def exports(self, p, e, value):
        self.u32(self.port(p) + 64 + 4 * e, value)

    def ring_host(self):
        os.write(self.host_ev, struct.pack("<Q", 1))

    def rung(self, timeout=1.0):
        """Whether the host rang the guest (its vector's eventfd), taken."""
        import select

        r, _, _ = select.select([self.own_ev], [], [], timeout)
        if r:
            os.read(self.own_ev, 8)
        return bool(r)

    def file_bytes(self):
        return os.fstat(self.shm).st_blocks * 512


class Rings:
    """The guest's ends of a session's rings (struct webos_media_control at off)."""

    def __init__(self, q, off):
        self.q, self.off, self.tail, self.head = q, off, 0, 0

    def pictures(self):
        out = []
        head = self.q.u32(self.off)
        while self.tail != head:
            e = self.off + 64 + 48 * (self.tail % RING)
            out.append(struct.unpack_from("<IIqIIIIIIII", self.q.mem, e))
            self.tail += 1
        self.q.u32(self.off + 4, self.tail)
        return out

    def wait_pictures(self, n, timeout=5):
        """Pictures until the LAST answer to input n; sleeping with the waiting flag (the host
        must ring)."""
        out, end = [], time.monotonic() + timeout
        while time.monotonic() < end:
            self.q.u32(self.off + 8, 1)
            got = self.pictures()
            if not got:
                self.q.rung(0.2)
                continue
            self.q.u32(self.off + 8, 0)
            out += got
            if any(p[0] == n and p[1] & LAST for p in got):
                return out
        raise AssertionError(f"no LAST answer to {n} on the ring")

    def release(self, slot, input, ring=True):
        e = self.off + 64 + 48 * RING + 8 * (self.head % RING)
        struct.pack_into("<II", self.q.mem, e, slot, input)
        self.head += 1
        self.q.u32(self.off + 16, self.head)
        if ring:
            self.q.ring_host()


class HubBase(unittest.TestCase):
    """The hub's tests; collected through HubTest (and TinyPoolTest's own), not by themselves."""

    __test__ = False
    POOL = POOL_SIZE

    def log_args(self, d):
        """The helper's own logs (--log, --decode-log): none, its lines on stderr (helper.log)."""
        return []

    def setUp(self):
        if not os.access(HUB, os.X_OK):
            self.skipTest(f"no {HUB}")
        self.dir = tempfile.TemporaryDirectory(prefix="media-helper-")
        self.addCleanup(self.dir.cleanup)
        self.qemu, self.guest, self.viewers = {}, {}, []
        for name in ("qemu-video.sock", "qemu-media-0.sock", "qemu-media-1.sock"):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(s.close)
            s.bind(os.path.join(self.dir.name, name))
            s.listen(1)
            self.qemu[name] = s
        self.logfile = open(os.path.join(self.dir.name, "helper.log"), "w+")
        self.addCleanup(self.logfile.close)
        d = self.dir.name
        # the pool's server socket, as the launcher makes it before the helper and QEMU start
        self.pool_path = os.path.join(d, "media-pool")
        self.pool_sock = os.path.join(d, "media-pool.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.pool_sock)
        listener.listen(4)
        self.hub = subprocess.Popen(
            [
                HUB,
                "--pid",
                str(os.getpid()),
                "--hwdec",
                "none",
                "--video",
                f"{d}/qemu-video.sock:{d}/video.sock",
                "--media",
                f"{d}/qemu-media:{d}/media",
                "--ports",
                "2",
                "--pool",
                self.pool_path,
                "--pool-fd",
                str(listener.fileno()),
                "--pool-size",
                str(self.POOL),
                "--streams",
                os.path.join(d, "media-streams"),
                *self.log_args(d),
            ],
            stdout=self.logfile,
            stderr=self.logfile,
            pass_fds=(listener.fileno(),),
        )
        listener.close()
        self.addCleanup(self.stop_helper)
        for name, s in self.qemu.items():
            s.settimeout(5)
            self.guest[name], _ = s.accept()
            self.addCleanup(self.guest[name].close)

    def stop_helper(self):
        self.hub.terminate()
        self.hub.wait(5)
        if os.environ.get("HELPER_LOG") or (self._outcome and not self._outcome.success):
            self.logfile.seek(0)
            print(self.logfile.read())

    def viewer(self, port=0):
        v = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(v.close)
        v.connect(os.path.join(self.dir.name, f"media-{port}.sock"))
        v.send(bytes(Descriptor(magic=HM_MAGIC, version=1, type=HELLO, preference=b"none")))
        ack, _ = receive(v)
        self.assertEqual(ack.type, ACK)
        return v, ack

    def send(self, port, kind, payload=b""):
        self.guest[f"qemu-media-{port}.sock"].sendall(message(kind, payload))

    def start_stream(self, port, kind, caps, base):
        self.send(port, OPEN, struct.pack("<IiiI28sI", kind, 42, 1, 0, b"test-out", 0))
        self.send(port, CAPS, caps.encode() + b"\0")
        self.send(port, STATE, struct.pack("<IIqqd", 1, 0, base, NONE, 1.0))

    def next_frame(self, v, kind=VIDEO, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            d, data = receive(v, end - time.monotonic())
            if d.type == kind:
                return d, data
        self.fail("no decoded output")

    def fixture(self):
        directory = os.environ.get("MEDIA_FIXTURE")
        if not directory:
            if "FFMPEG" not in os.environ and not shutil.which("ffmpeg"):
                self.skipTest(
                    "FFmpeg/ffprobe tools unavailable; supply MEDIA_FIXTURE generated with --generate-fixture DIR"
                )
            if "FFPROBE" not in os.environ and not shutil.which("ffprobe"):
                self.skipTest(
                    "FFmpeg/ffprobe tools unavailable; supply MEDIA_FIXTURE generated with --generate-fixture DIR"
                )
            directory = os.path.join(self.dir.name, "fixture")
            generate_fixture(directory)
        path = os.path.join(directory, "clip.mp4")
        with open(path, "rb") as f:
            data = f.read()
        with open(os.path.join(directory, "packets.json")) as f:
            description = json.load(f)
        return path, description, data

    def packet(self, port, packet, data):
        _, pts, dts, dur, pos, size, key = packet
        ns = lambda t: NONE if t is None else round(t * 1e9)
        self.send(
            port,
            BUFFER,
            struct.pack("<qqqqII", ns(pts), ns(dts), ns(dur), ns(pts), int(key), 0)
            + data[pos : pos + size],
        )

    def test_snapshot_and_lifecycle(self):
        self.start_stream(
            0, 1, "video/x-h264, stream-format=(string)byte-stream", time.monotonic_ns()
        )
        time.sleep(0.05)
        v, ack = self.viewer()
        self.assertTrue(ack.flags & 1)
        self.assertEqual(ack.open.pid, 42)
        self.assertEqual(ack.status, 2 if not ack.flags & 16 else 3)
        d, _ = receive(v)
        old = d.generation
        self.send(0, FLUSH)
        d, _ = receive(v)
        self.assertGreater(d.generation, old)
        self.assertEqual(d.state.playing, 0)
        self.send(0, CLOSE)
        d, _ = receive(v)
        self.assertFalse(d.flags & 1)

    def test_streams_file_lists_the_open_sessions(self):
        """--streams: a line per open session (port, kind, element, caps), gone at CLOSE."""
        path = os.path.join(self.dir.name, "media-streams")

        def lines():
            with open(path) as f:
                return f.read().splitlines()

        def wait(want):
            for _ in range(300):
                if lines() == want:
                    return
                time.sleep(0.01)
            self.assertEqual(lines(), want)

        wait([])
        caps = "audio/x-ac3, framed=(boolean)true, rate=(int)48000, channels=(int)6"
        self.start_stream(1, 2, caps, time.monotonic_ns())
        wait([f"1 audio test-out\t{caps}"])
        self.send(1, CAPS, b"audio/x-ac3,\nchannels=(int)2\0")
        wait(["1 audio test-out\taudio/x-ac3,?channels=(int)2"])
        self.send(1, CLOSE)
        wait([])

    def test_no_viewer_never_blocks_the_guest(self):
        g = self.guest["qemu-media-1.sock"]
        g.settimeout(5)
        payload = struct.pack("<qqqqII", 0, 0, 40, 0, 1, 0) + bytes(65536)
        start = time.monotonic()
        for n in range(400):
            g.sendall(message(BUFFER, payload, n + 1))
        self.assertLess(time.monotonic() - start, 5)

    def test_video_table_replayed(self):
        frame = b"WOSVID2\0" + bytes(900 - 8)
        self.guest["qemu-video.sock"].sendall(b"junk" + frame)
        time.sleep(0.05)
        v = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(v.close)
        v.connect(os.path.join(self.dir.name, "video.sock"))
        v.settimeout(5)
        data = b""
        while len(data) < len(frame):
            data += v.recv(len(frame) - len(data))
        self.assertEqual(data, frame)

    def test_real_h264_no_viewer_reconnect_and_slow_viewer(self):
        v, ack = self.viewer()
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        v.close()
        time.sleep(0.05)
        path, (streams, packets), data = self.fixture()
        caps = next(c for k, c in streams.values() if k == "video")
        video = [p for p in packets if p[0] == "video"]
        with open(os.path.join(os.path.dirname(path), "video.yuv"), "rb") as f:
            reference = f.read()
        self.start_stream(0, 1, caps, time.monotonic_ns() - 3_000_000_000)
        for p in video[:20]:
            self.packet(0, p, data)
            time.sleep(0.008)
        time.sleep(0.1)
        v, _ = self.viewer()
        first, objects = self.next_frame(v)
        self.assertEqual(first.format, 1)
        index = round(first.pts / 40_000_000)
        self.assertGreater(index, 0, "late attachment must receive a current decoded picture")
        self.assertEqual(objects[0], reference[index * 4608 : (index + 1) * 4608])
        release(v, first)
        self.send(0, STATE, struct.pack("<IIqqd", 0, 0, 0, first.pts, 1.0))
        paused, _ = receive(v)
        self.assertEqual(
            (paused.type, paused.state.playing, paused.state.pause), (SNAPSHOT, 0, first.pts)
        )
        v.close()
        time.sleep(0.05)
        # Paused reconnect without any compressed packet: the retained real current frame.
        v, _ = self.viewer()
        again, objects = self.next_frame(v)
        self.assertEqual(again.pts, first.pts)
        self.assertEqual(objects[0], reference[index * 4608 : (index + 1) * 4608])
        self.assertEqual(again.state.playing, 0)
        release(v, again)
        self.send(
            0, STATE, struct.pack("<IIqqd", 1, 0, time.monotonic_ns() - 3_000_000_000, NONE, 1.0)
        )
        # Hold every credit while the decoder consumes the rest of this same long GOP.
        for p in video[20:]:
            self.packet(0, p, data)
            time.sleep(0.008)
        self.send(0, EOS)
        held = []
        while True:
            try:
                d, _ = receive(v, 0.2)
            except socket.timeout:
                break
            if d.type == VIDEO:
                held.append(d)
        self.assertLessEqual(len(held), 4)
        self.assertTrue(held)
        for d in held:
            release(v, d)
        final, objects = self.next_frame(v)
        self.assertEqual(final.pts, round(video[-1][1] * 1e9))
        self.assertEqual(objects[0], reference[-4608:])
        release(v, final)
        # A flushing seek invalidates the current picture, preserves the session,
        # and resumes cleanly from the fixture's actual keyframe.
        self.send(0, FLUSH)
        while True:
            flushed, _ = receive(v)
            if flushed.type == SNAPSHOT and flushed.generation != final.generation:
                break
        self.assertEqual(flushed.session, final.session)
        self.assertEqual(flushed.state.playing, 0)
        self.send(0, STATE, struct.pack("<IIqqd", 1, 0, time.monotonic_ns(), NONE, 1.0))
        self.packet(0, video[0], data)
        self.send(0, EOS)  # Drain frame-thread latency for this one-picture segment.
        sought, objects = self.next_frame(v)
        self.assertEqual(sought.generation, flushed.generation)
        self.assertEqual(sought.pts, 0)
        self.assertEqual(objects[0], reference[:4608])
        release(v, sought)

    def test_aac_decode_pcm_reference(self):
        v, ack = self.viewer(1)
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        path, (streams, packets), data = self.fixture()
        caps = next(c for k, c in streams.values() if k == "audio")
        with open(os.path.join(os.path.dirname(path), "audio.f32"), "rb") as f:
            reference = f.read()
        # Future base retains all PCM while this test checks and releases each lease.
        self.start_stream(1, 2, caps, time.monotonic_ns() + 10_000_000_000)
        received = 0
        actual_pcm, expected_pcm, chunks = bytearray(), bytearray(), []
        for p in (p for p in packets if p[0] == "audio"):
            self.packet(1, p, data)
            d, objects = self.next_frame(v, PCM)
            self.assertEqual((d.format, d.width, d.height), (6, 0, 0))
            if d.pts >= 0:
                offset = round(d.pts * 48000 / 1e9) * 8
                expected = reference[offset : offset + d.samples * 8]
                actual = objects[0][: len(expected)]
                actual_pcm.extend(actual)
                expected_pcm.extend(expected)
                chunks.append(dict(pts=d.pts, samples=d.samples, reference_offset=offset))
                received += len(actual) // 8
            release(v, d)
        self.assertGreaterEqual(received, 48000)
        self.send(1, EOS)
        if actual_pcm != expected_pcm:
            actual_values = array.array("f", actual_pcm)
            expected_values = array.array("f", expected_pcm)
            pairs = list(zip(actual_values, expected_values))
            finite = [(a, b) for a, b in pairs if math.isfinite(a) and math.isfinite(b)]
            errors = [abs(a - b) for a, b in finite]
            ordered = lambda value: (
                (~value & 0xFFFFFFFF) if value & 0x80000000 else value | 0x80000000
            )
            actual_bits = struct.unpack("<%dI" % len(actual_values), actual_pcm)
            expected_bits = struct.unpack("<%dI" % len(expected_values), expected_pcm)
            ulps = [abs(ordered(a) - ordered(b)) for a, b in zip(actual_bits, expected_bits)]
            metrics = dict(
                samples=len(pairs),
                nonfinite=len(pairs) - len(finite),
                differing=sum(a != b for a, b in pairs),
                max_absolute=max(errors, default=0),
                max_relative=max((abs(a - b) / abs(b) for a, b in finite if b), default=0),
                max_ulp=max(ulps, default=0),
                rms_error=math.sqrt(sum(e * e for e in errors) / max(1, len(errors))),
                rms_reference=math.sqrt(sum(b * b for _, b in finite) / max(1, len(finite))),
            )
            artifact_dir = os.environ.get("MEDIA_ARTIFACTS")
            if artifact_dir:
                os.makedirs(artifact_dir, exist_ok=True)
                for name, values in (("actual.f32", actual_pcm), ("expected.f32", expected_pcm)):
                    with open(os.path.join(artifact_dir, name), "wb") as f:
                        f.write(values)
                with open(os.path.join(artifact_dir, "pcm-comparison.json"), "w") as f:
                    json.dump(dict(metrics=metrics, chunks=chunks), f, indent=2)
            self.fail(
                "PCM differs from independent reference: " + json.dumps(metrics, sort_keys=True)
            )

    def fast_clip(self):
        """A 1 s 120 fps H.264 clip without B-frames: its caps and video packets."""
        if not shutil.which(FFMPEG) or not shutil.which(FFPROBE):
            self.skipTest("FFmpeg/ffprobe tools unavailable")
        path = os.path.join(self.dir.name, "fast.mp4")
        subprocess.run(
            [
                FFMPEG,
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=64x48:rate=120",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-bf",
                "0",
                "-g",
                "250",
                "-pix_fmt",
                "yuv420p",
                path,
            ],
            check=True,
        )
        streams, packets = FAKE.probe(path)
        with open(path, "rb") as f:
            data = f.read()
        caps = next(c for k, c in streams.values() if k == "video")
        return caps, [p for p in packets if p[0] == "video"], data

    def collect(self, v, frames, stop):
        """A fake viewer that releases every picture at once and records its pts."""
        while not stop.is_set():
            try:
                d, _ = receive(v, 0.1)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            if d.type == VIDEO:
                frames.append(d.pts)
                release(v, d)

    def decode_report(self, port, why):
        """The helper's decode report line for this port, once written."""
        end = time.monotonic() + 5
        while time.monotonic() < end:
            self.logfile.seek(0)
            for line in self.logfile.read().splitlines():
                if line.startswith(f"decode {port} ") and why in line:
                    return line
            time.sleep(0.05)
        self.fail(f"no decode report{why}")

    def test_paced_full_queue_waits_until_due(self):
        """A full output queue waits for space until a picture is due: a paced 120 fps
        clip with the normal 300 ms lead exports every picture, none dropped."""
        v, ack = self.viewer()
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        caps, video, data = self.fast_clip()
        self.assertEqual(len(video), 120)
        frames, stop = [], threading.Event()
        consumer = threading.Thread(target=self.collect, args=(v, frames, stop))
        consumer.start()
        self.addCleanup(consumer.join)
        self.addCleanup(stop.set)
        start = time.monotonic_ns()
        self.start_stream(0, 1, caps, start + 300_000_000)
        for p in video:
            delay = start + round(p[1] * 1e9) - time.monotonic_ns()
            if delay > 0:
                time.sleep(delay / 1e9)
            self.packet(0, p, data)
        self.send(0, EOS)
        end = time.monotonic() + 3
        while len(set(frames)) < len(video) and time.monotonic() < end:
            time.sleep(0.05)
        stop.set()
        consumer.join()
        self.assertEqual(sorted(set(frames)), sorted(round(p[1] * 1e9) for p in video))
        self.send(0, CLOSE)
        self.assertIn(" dropped=0 ", self.decode_report(0, " (closed)"))

    def test_late_pictures_still_dropped(self):
        """A picture already late when the output queue is full is dropped, not waited for:
        a queue held full by far-future pictures cannot stall the decoder."""
        caps, video, data = self.fast_clip()
        self.start_stream(0, 1, caps, time.monotonic_ns() - 10_000_000_000)
        held = 25  # FRAME_QUEUE plus the first picture, which is shown at once
        for n, p in enumerate(video):
            _, _, _, dur, pos, size, key = p
            pts = (100 + n) * 8_000_000_000 if n < held else n * 8_000_000
            self.send(
                0,
                BUFFER,
                struct.pack("<qqqqII", pts, NONE, round(dur * 1e9), pts, int(key), 0)
                + data[pos : pos + size],
            )
        self.send(0, EOS)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            self.logfile.seek(0)
            if "media 0: end of stream (video)" in self.logfile.read():
                break
            time.sleep(0.05)
        else:
            self.fail("the decoder stalled behind a full queue")
        self.send(0, CLOSE)
        line = self.decode_report(0, " (closed)")
        self.assertIn(" decoded=120 ", line)
        # The future pictures fill the queue for good; every later picture is late and dropped.
        self.assertIn(" dropped=95 ", line)

    def guest_answers(self, port, inputs, timeout=5):
        """PICTURE messages from the helper on the guest's port until the LAST answer to each
        of inputs: [(picture fields, bytes)]."""
        g = self.guest[f"qemu-media-{port}.sock"]
        g.settimeout(timeout)
        data, out, waiting = b"", [], set(inputs)
        if not inputs:  # what is there now (POOL answers), within timeout
            try:
                data = g.recv(1 << 20)
            except (socket.timeout, TimeoutError):
                return out
            waiting = None
        end = time.monotonic() + timeout
        while waiting or waiting is None:
            if waiting is None:
                waiting = set()
            elif time.monotonic() > end:
                self.fail(f"no answer to {sorted(waiting)}")
            else:
                data += g.recv(1 << 20)
            while len(data) >= 40:
                magic, kind, _, size, seq, mono, clock, check, _ = struct.unpack_from(
                    "<IHHIIqqII", data
                )
                words = [magic, kind, size, seq, mono & 0xFFFFFFFF, (mono >> 32) & 0xFFFFFFFF]
                words += [clock & 0xFFFFFFFF, (clock >> 32) & 0xFFFFFFFF]
                self.assertEqual((magic, check), (MAGIC, FAKE.check(words)))
                self.assertIn(kind, (PICTURE, POOL))
                if len(data) < 40 + size:
                    break
                if kind == POOL:
                    self.pool_answers.append(struct.unpack_from("<II", data, 40))
                    data = data[40 + size :]
                    continue
                fields = struct.unpack_from("<IIqIIIIIIII", data, 40)
                out.append((fields, data[40 + 48 : 40 + size]))
                if fields[1] & LAST:
                    waiting.discard(fields[0])
                data = data[40 + size :]
        self.assertEqual(data, b"")
        return out

    def decode_jpegs(self, chroma, size, tag=""):
        """JPEGs of testsrc2 at size with chroma subsampling (yuvj420p, ...), and each decoded
        to its native planes by FFmpeg."""
        if not shutil.which(FFMPEG):
            self.skipTest("no ffmpeg")
        d = os.path.join(self.dir.name, chroma + tag)
        os.makedirs(d)
        w, h = size
        subprocess.run(
            [
                FFMPEG,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={w}x{h}:rate=30",
                "-frames:v",
                "3",
                "-pix_fmt",
                chroma,
                "-c:v",
                "mjpeg",
                f"{d}/%d.jpg",
            ],
            check=True,
        )
        frames = []
        for i in range(1, 4):
            jpeg = open(f"{d}/{i}.jpg", "rb").read()
            raw = subprocess.run(
                [
                    FFMPEG,
                    "-v",
                    "error",
                    "-i",
                    f"{d}/{i}.jpg",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    chroma,
                    "-",
                ],
                check=True,
                capture_output=True,
            ).stdout
            frames.append((jpeg, raw))
        return frames

    @staticmethod
    def expected_nv12(raw, chroma, w, h):
        """The helper's NV12 (include/webos-media.h's layout) from native planes: the chroma
        sample at each 4:2:0 position."""
        lw, lh = {"yuvj420p": (1, 1), "yuvj422p": (1, 0), "yuvj444p": (0, 0)}[chroma]
        cw0, ch0 = -(-w >> lw), -(-h >> lh)
        u0 = w * h
        v0 = u0 + cw0 * ch0
        stride, rows = (w + 3) & ~3, (h + 1) & ~1
        out = bytearray(stride * rows + stride * (rows // 2))
        for r in range(h):
            out[r * stride : r * stride + w] = raw[r * w : r * w + w]
        for r in range((h + 1) // 2):
            sr = min((r << 1) >> lh, ch0 - 1)
            for x in range((w + 1) // 2):
                sc = min((x << 1) >> lw, cw0 - 1)
                o = stride * rows + r * stride + 2 * x
                out[o] = raw[u0 + sr * cw0 + sc]
                out[o + 1] = raw[v0 + sr * cw0 + sc]
        return bytes(out)

    @staticmethod
    def expected_yuy2(raw, chroma, w, h):
        """The helper's YUY2: each pair with the chroma sample at its 4:2:2 position."""
        lw, lh = {"yuvj420p": (1, 1), "yuvj422p": (1, 0), "yuvj444p": (0, 0)}[chroma]
        cw0, ch0 = -(-w >> lw), -(-h >> lh)
        u0 = w * h
        v0 = u0 + cw0 * ch0
        stride = (w * 2 + 3) & ~3
        out = bytearray(stride * h)
        for r in range(h):
            sr = min(r >> lh, ch0 - 1)
            for x in range(0, w, 2):
                sc = min((x >> 1 << 1) >> lw, cw0 - 1)
                o = r * stride + x * 2
                out[o] = raw[r * w + x]
                out[o + 1] = raw[u0 + sr * cw0 + sc]
                out[o + 2] = raw[r * w + min(x + 1, w - 1)]
                out[o + 3] = raw[v0 + sr * cw0 + sc]
        return bytes(out)

    def test_decode_for_the_guest(self):
        """A DECODE session: each JPEG comes back as NV12 or YUY2 with a LAST answer, EOS is
        answered, a flushed input is not, and no viewer is shown the session."""
        v, ack = self.viewer()
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        for fmt, chroma, (w, h) in (
            (NV12, "yuvj422p", (66, 50)),
            (NV12, "yuvj420p", (64, 48)),
            (NV12, "yuvj444p", (34, 22)),
            (YUY2, "yuvj422p", (66, 50)),
            (YUY2, "yuvj420p", (64, 48)),
        ):
            with self.subTest(chroma=chroma, format=hex(fmt)):
                frames = self.decode_jpegs(chroma, (w, h), hex(fmt))
                g = self.guest["qemu-media-0.sock"]
                seq = 0

                def send(kind, payload=b""):
                    nonlocal seq
                    seq += 1
                    g.sendall(message(kind, payload, seq))
                    return seq

                send(OPEN, struct.pack("<IiiI28sI", 1, 42, -1, 0, b"omxmjpegdec0", 0))
                send(DECODE, struct.pack("<II", fmt, 0))
                send(CAPS, f"image/jpeg, width=(int){w}, height=(int){h}".encode() + b"\0")
                for i, (jpeg, raw) in enumerate(frames):
                    pts = 1000000000 + i * 33333333
                    n = send(BUFFER, struct.pack("<qqqqII", pts, NONE, 33333333, pts, 1, 0) + jpeg)
                    answers = self.guest_answers(0, [n])
                    if i == 0:
                        # A picture came back, so the helper has taken DECODE. Until then the
                        # viewer may legitimately have been shown OPEN (the helper's worker
                        # publishes a session before DECODE reaches it): drop what is queued.
                        v.setblocking(False)
                        try:
                            while True:
                                receive(v, 0)
                        except (BlockingIOError, socket.timeout, TimeoutError):
                            pass
                    pictures = [a for a in answers if a[0][3]]
                    self.assertEqual(len(pictures), 1, answers)
                    (inp, flags, got_pts, pf, pw, ph, s0, s1, o0, o1, _), data = pictures[0]
                    self.assertEqual((inp, pf, pw, ph, got_pts), (n, fmt, w, h, pts))
                    if fmt == NV12:
                        stride, rows = (w + 3) & ~3, (h + 1) & ~1
                        self.assertEqual((s0, s1, o0, o1), (stride, stride, 0, stride * rows))
                        want = self.expected_nv12(raw, chroma, w, h)
                    else:
                        self.assertEqual((s0, s1, o0, o1), ((w * 2 + 3) & ~3, 0, 0, 0))
                        want = self.expected_yuy2(raw, chroma, w, h)
                    self.assertEqual(len(data), len(want))
                    # FFmpeg's command line may pick another IDCT than the helper's decoder
                    worst = max(abs(a - b) for a, b in zip(data, want))
                    self.assertLessEqual(worst, 4, f"{chroma}: pixels differ by {worst}")
                    self.assertTrue(answers[-1][0][1] & LAST)
                n = send(EOS)
                self.assertTrue(self.guest_answers(0, [n])[-1][0][1] & LAST)
                send(CLOSE)
        # The viewer never saw a session open (only state updates without HM_OPEN).
        v.settimeout(0.2)
        try:
            while True:
                d, _ = receive(v, 0.2)
                self.assertFalse(d.flags & 1 and d.open.element == b"omxmjpegdec0")
        except (socket.timeout, TimeoutError):
            pass
        self.logfile.seek(0)
        self.assertIn("pictures go back to the guest", self.logfile.read())

    def log(self):
        self.logfile.seek(0)
        return self.logfile.read()

    def wait_log(self, text, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if text in self.log():
                return True
            time.sleep(0.05)
        return False

    def pool_session(self, port, claim=1, w=64, h=48):
        """QEMU on the pool, the port claimed, OPEN, DECODE (shared) and CAPS; (qemu, POOL answer,
        send, frames)."""
        q = Qemu(self.pool_sock)
        self.addCleanup(q.close)
        self.assertEqual(q.host_peer, 0)
        frames = self.decode_jpegs("yuvj420p", (w, h), f"pool{port}")
        g = self.guest[f"qemu-media-{port}.sock"]
        seq = [0]

        def send(kind, payload=b""):
            seq[0] += 1
            g.sendall(message(kind, payload, seq[0]))
            return seq[0]

        if claim is not None:
            q.claim(port, claim)
        self.pool_answers = []
        send(OPEN, struct.pack("<IiiI28sI", 1, 42, -1, 0, b"omxmjpegdec1", 0))
        send(DECODE, struct.pack("<II", NV12, 1))
        send(CAPS, f"image/jpeg, width=(int){w}, height=(int){h}".encode() + b"\0")
        end = time.monotonic() + 5
        while not self.pool_answers and time.monotonic() < end:
            self.guest_answers(port, [], timeout=0.5)
        self.assertEqual(len(self.pool_answers), 1)
        return q, self.pool_answers[0], send, frames


class HubTest(HubBase):
    __test__ = True

    def test_pool_pictures(self):
        """A DECODE session on the pool: POOL names the session's control extent; pictures
        come on its ring into slots of an extent granted to the port, with the guest rung; each
        slot stays the guest's until it is on the release ring (a release naming another input
        is ignored), and with every slot taken a picture is dropped but still answered."""
        v, ack = self.viewer(1)
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        w, h = 64, 48
        q, (coff, csize), send, frames = self.pool_session(1, w=w, h=h)
        self.assertGreaterEqual(coff, GRANULE)
        self.assertEqual(csize, GRANULE)
        rings = Rings(q, coff)
        stride, rows = (w + 3) & ~3, (h + 1) & ~1
        size = stride * rows + stride * rows // 2

        def picture(i):
            jpeg, raw = frames[i % len(frames)]
            pts = 1000000000 + i * 33333333
            n = send(BUFFER, struct.pack("<qqqqII", pts, NONE, 33333333, pts, 1, 0) + jpeg)
            got = rings.wait_pictures(n)
            self.assertTrue(got[-1][1] & LAST)
            return n, raw, [p for p in got if p[3]]

        held, slots = [], set()
        for i in range(4):  # the four slots
            n, raw, pictures = picture(i)
            self.assertEqual(len(pictures), 1)
            fields = pictures[0]
            self.assertTrue(fields[1] & SHARED)
            slot = fields[10]
            self.assertNotIn(slot, slots)
            live = [x for x in q.extents(1) if x[2] == LIVE]
            self.assertTrue(any(o <= slot and slot + size <= o + s for o, s, _, _ in live))
            slots.add(slot)
            held.append((slot, n))
            want = self.expected_nv12(raw, "yuvj420p", w, h)
            got = q.mem[slot : slot + size]
            self.assertLessEqual(max(abs(a - b) for a, b in zip(got, want)), 4)
        t0 = time.monotonic()
        n, _, pictures = picture(4)  # no slot free: dropped after the wait
        self.assertEqual(pictures, [])
        self.assertGreaterEqual(time.monotonic() - t0, 0.09)
        slot, n0 = held[0]
        rings.release(slot, n0 + 1000)  # not its input: ignored
        self.assertEqual(picture(5)[2], [])
        rings.release(slot, n0)
        n, _, pictures = picture(6)
        self.assertEqual(len(pictures), 1)
        self.assertEqual(pictures[0][10], slot)
        self.assertEqual(self.guest_answers(1, [], timeout=0.2), [])  # nothing inline
        send(CLOSE)
        self.assertTrue(self.wait_log("session=1 (closed)"))
        log = self.log()
        self.assertIn("(NV12, shared)", log)
        self.assertIn("slot-drops=2", log)

    def test_pool_given_back(self):
        """CLOSE gives the session's extents back: RETIRING while the guest still maps one (its
        export count), then punched out of the file and FREE once the count drops and the guest
        rings. A claim that ends (the guest's file closed) does the same without a CLOSE."""
        v, ack = self.viewer(1)
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        q, (coff, _), send, frames = self.pool_session(1, w=640, h=480)
        rings = Rings(q, coff)
        idle = None
        jpeg = frames[0][0]
        n = send(BUFFER, struct.pack("<qqqqII", 0, NONE, 1, 0, 1, 0) + jpeg)
        pic = [p for p in rings.wait_pictures(n) if p[3]][0]
        busy = q.file_bytes()
        live = [(e, x) for e, x in enumerate(q.extents(1)) if x[2] == LIVE]
        self.assertEqual(len(live), 2)  # control and slots
        e_pic = [e for e, x in live if x[0] <= pic[10] < x[0] + x[1]][0]
        q.exports(1, e_pic, 1)  # the guest maps the picture
        send(CLOSE)
        self.assertTrue(self.wait_log("session=1 (closed)"))
        time.sleep(0.2)
        ext = q.extents(1)
        self.assertEqual(ext[e_pic][2], RETIRING)
        self.assertTrue(all(x[2] == 0 for e, x in enumerate(ext) if e != e_pic))  # control freed
        self.assertGreater(q.file_bytes(), GRANULE)
        q.exports(1, e_pic, 0)
        q.ring_host()
        end = time.monotonic() + 3
        while q.extents(1)[e_pic][2] and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(q.extents(1)[e_pic][2], 0)
        after = q.file_bytes()
        self.assertLessEqual(after, 2 * 4096 + GRANULE)
        self.assertEqual(q.mem[pic[10] : pic[10] + 4096], bytes(4096))  # punched: zeros
        print(f"pool file: {busy >> 10} KiB during, {after >> 10} KiB after", file=sys.stderr)
        # a claim that ends: the session's pool goes back without CLOSE
        q.claim(1, 3)
        send(OPEN, struct.pack("<IiiI28sI", 1, 42, -1, 0, b"omxmjpegdec1", 0))
        self.pool_answers = []
        send(DECODE, struct.pack("<II", NV12, 1))
        self.guest_answers(1, [], timeout=1)
        self.assertEqual(self.pool_answers[0][1], GRANULE)
        q.claim(1, 4)
        q.ring_host()
        self.assertTrue(self.wait_log("claim ended"))
        end = time.monotonic() + 3
        while any(x[2] for x in q.extents(1)) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertFalse(any(x[2] for x in q.extents(1)))

    def test_pool_hostile_guest(self):
        """What the guest writes is untrusted: releases naming no slot of the session (the
        directory, another port's extent, beyond the pool), a release ring head far ahead, garbage
        tails and counts. The helper drops and logs them, keeps serving, and frees nothing
        another session uses."""
        v, ack = self.viewer(1)
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        q, (coff, _), send, frames = self.pool_session(1)
        rings = Rings(q, coff)
        jpeg = frames[0][0]
        n = send(BUFFER, struct.pack("<qqqqII", 0, NONE, 1, 0, 1, 0) + jpeg)
        pic = [p for p in rings.wait_pictures(n) if p[3]][0]
        for slot in (0, 64, coff, POOL_SIZE - 4096, 0xFFFFFFFF, pic[10] + 1):
            rings.release(slot, n)
        # a head far ahead, a tail far behind, a count on the control extent that is not 0
        q.u32(coff + 16, 0xDEADBEEF)
        q.u32(coff + 4, 0x7FFFFFFF)
        q.ring_host()
        self.assertTrue(self.wait_log("names no slot of the session"))
        rings.head = 0xDEADBEEF
        rings.tail = q.u32(coff)
        q.u32(coff + 4, rings.tail)
        rings.release(pic[10], n)  # the real one, after all that
        n = send(BUFFER, struct.pack("<qqqqII", 1, NONE, 1, 1, 1, 0) + jpeg)
        got = [p for p in rings.wait_pictures(n) if p[3]]
        self.assertEqual(len(got), 1)
        self.assertEqual(self.hub.poll(), None)
        send(CLOSE)
        self.assertTrue(self.wait_log("session=1 (closed)"))
        self.assertEqual(self.hub.poll(), None)

    def test_pool_unclaimed(self):
        """DECODE asks for shared pictures, but the guest has not claimed the port: POOL says
        none and the pictures come inline."""
        v, ack = self.viewer(1)
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        q, answer, send, frames = self.pool_session(1, claim=None)
        self.assertEqual(answer[1], 0)
        n = send(BUFFER, struct.pack("<qqqqII", 0, NONE, 1, 0, 1, 0) + frames[0][0])
        answers = self.guest_answers(1, [n])
        self.assertTrue(any(len(a[1]) for a in answers))
        self.assertIn("not claimed the port", self.log())


class TinyPoolTest(HubBase):
    """A pool with no room beyond a session's control (the helper's --pool-size)."""

    POOL = 2 * GRANULE

    def test_pool_full_inline(self):
        """The pool has no room for the session's slots: its pictures come inline on the port
        from then on, with "pool full" logged once; nothing is dropped."""
        v, ack = self.viewer(1)
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        q, (coff, csize), send, frames = self.pool_session(1)
        self.assertEqual(csize, GRANULE)
        rings = Rings(q, coff)
        for i in range(3):
            n = send(BUFFER, struct.pack("<qqqqII", i, NONE, 1, i, 1, 0) + frames[0][0])
            answers = self.guest_answers(1, [n])
            self.assertEqual(len([a for a in answers if a[0][3] and a[1]]), 1)
            self.assertEqual(rings.pictures(), [])
        self.assertEqual(self.log().count("pool full"), 1)


class GuestLogTest(HubBase):
    """What a guest can make the helper log: whole lines, printable, bounded."""

    def test_caps_not_a_string(self):
        """CAPS without its NUL, the next message right behind it: the log says so and reads
        nothing past the payload (the next header's magic, WSM2)."""
        self.send(0, OPEN, struct.pack("<IiiI28sI", 1, 42, 1, 0, b"test-out", 0))
        self.guest["qemu-media-0.sock"].sendall(message(CAPS, b"video/x-h264") + message(FLUSH))
        self.assertTrue(self.wait_log("media 0: caps of 12 bytes, not a string"), self.log())
        self.assertNotIn("WSM2", self.log())

    def test_guest_strings_printable(self):
        """An element name and caps with a newline and a terminal escape: one line each, the
        bytes as \\xNN."""
        self.send(0, OPEN, struct.pack("<IiiI28sI", 1, 42, 1, 0, b"evil\nforged\x1b[2J", 0))
        self.send(0, CAPS, b"video/x-h264\nforged caps\x07\0")
        self.assertTrue(self.wait_log("caps (video) video/x-h264\\x0aforged caps\\x07"), self.log())
        log = self.log()
        self.assertIn("media 0: open video evil\\x0aforged\\x1b[2J", log)
        self.assertNotIn("\x1b", log)
        self.assertNotIn("\x07", log)
        self.assertFalse([l for l in log.splitlines() if l.startswith("forged")], log)

    def test_flush_flood_limited(self):
        """A guest looping FLUSH: a burst of lines per site, then one line counting the rest."""
        self.start_stream(0, 1, "video/x-h264, stream-format=(string)byte-stream", 0)
        self.guest["qemu-media-0.sock"].sendall(b"".join(message(FLUSH) for _ in range(2000)))
        self.assertTrue(self.wait_log("lines of port 0 suppressed: media %d: flush"), self.log())
        log = self.log()
        self.assertLess(log.count("media 0: flush"), 400, log[-2000:])
        self.assertEqual(self.hub.poll(), None)


class LogFileTest(HubBase):
    """The launcher's way: --log (the sessions, stamped) and --decode-log (the engine's) are files
    the helper owns; stderr keeps neither."""

    def log_args(self, d):
        self.sessions = os.path.join(d, "media-sessions.log")
        self.decode = os.path.join(d, "media-helper.log")
        return ["--log", self.sessions, "--decode-log", self.decode]

    def test_owned_logs(self):
        self.start_stream(
            0, 1, "video/x-h264, stream-format=(string)byte-stream", time.monotonic_ns()
        )
        self.send(0, CLOSE)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            with open(self.decode, errors="replace") as f:
                decode = f.read()
            if "media 0: close video" in decode:
                break
            time.sleep(0.05)
        self.assertIn("media 0: open video test-out (pid 42", decode)
        self.assertIn("decode 0 video session=1 (closed)", decode)
        with open(self.sessions, errors="replace") as f:
            sessions = f.read()
        self.assertRegex(sessions, r"(?m)^\d{4}-\d\d-\d\dT[\d:.]+Z media 0: open video test-out")
        self.assertNotIn("media 0:", self.log())

    def test_file_size_limit(self):
        """RLIMIT_FSIZE above the pool file and the logs' caps, SIGXFSZ ignored: a write past it
        fails (EFBIG) instead of ending the helper."""
        with open(f"/proc/{self.hub.pid}/limits") as f:
            limit = [l.split() for l in f if l.startswith("Max file size")][0]
        self.assertEqual(int(limit[3]), self.POOL + (64 << 20))
        self.assertEqual(int(limit[4]), self.POOL + (64 << 20))
        with open(f"/proc/{self.hub.pid}/status") as f:
            ignored = [int(l.split()[1], 16) for l in f if l.startswith("SigIgn:")][0]
        self.assertTrue(ignored & (1 << (signal.SIGXFSZ - 1)))


# TinyPoolTest and the log tests run only their own tests: not the ones they inherit from HubBase
for _cls in (TinyPoolTest, GuestLogTest, LogFileTest):
    for _name in [n for n in vars(HubBase) if n.startswith("test_")]:
        setattr(_cls, _name, None)
    _cls.__test__ = True


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--generate-fixture":
        generate_fixture(sys.argv[2])
    else:
        unittest.main(verbosity=2)
