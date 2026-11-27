#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Extra media-helper decode coverage: AV1 pixels, Opus PCM, B-frame order, HDR metadata.

Run MEDIAHUB=/path/to/media-helper python3 tests/integration/media-helper/test_media_extra.py -v
from the repository root. Each test generates its own small fixture with local
ffmpeg/ffprobe into a TemporaryDirectory and drives the real media-helper binary
over UNIX sockets, mirroring tests/integration/media-helper/test_media_helper.py (which this
file does not import). The helper runs with --hwdec none (software decode).

No viewer, GPU, or display is needed.
"""

import array
import ctypes as C
import fcntl
import importlib.util
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
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
HM_MAGIC, HELLO, ACK, SNAPSHOT, VIDEO, PCM, RELEASE = 0x31444D57, 1, 2, 3, 4, 5, 6
NONE = -(1 << 63)
FRAME_BYTES = 64 * 48 * 3 // 2  # 64x48 yuv420p
FRAME_NS = 40_000_000  # 25 fps
# Target-native tools for cross-arch runs (references must come from the
# same FFmpeg build that decodes: FFMPEG=/path/to/target/ffmpeg).
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


def extradata_hex(s):
    out = []
    for line in s.strip().splitlines():
        part = line.split(":", 1)[1][:40]
        out.append(part.replace(" ", ""))
    return "".join(out)


def probe_all(path):
    """FAKE.probe plus AV1 video and Opus audio caps (same tuple shapes)."""
    info = json.loads(
        subprocess.check_output(
            [FFPROBE, "-v", "error", "-show_streams", "-show_data", "-of", "json", path]
        )
    )
    pk = json.loads(
        subprocess.check_output(
            [
                FFPROBE,
                "-v",
                "error",
                "-show_entries",
                "packet=stream_index,pts_time,dts_time,duration_time,size,pos,flags",
                "-of",
                "json",
                path,
            ]
        )
    )
    streams = {}
    for s in info["streams"]:
        cd = extradata_hex(s.get("extradata", "")) if s.get("extradata") else ""
        if s["codec_type"] == "video":
            name = {
                "h264": "video/x-h264, stream-format=(string)avc",
                "hevc": "video/x-h265, stream-format=(string)hvc1",
                "av1": "video/x-av1, stream-format=(string)obu-stream",
            }[s["codec_name"]]
            fr = s.get("avg_frame_rate", "0/1")
            caps = (
                f"{name}, alignment=(string)au, codec_data=(buffer){cd}, width=(int){s['width']}, "
                f"height=(int){s['height']}, framerate=(fraction){fr}, pixel-aspect-ratio=(fraction)1/1"
            )
            streams[s["index"]] = ("video", caps)
        elif s["codec_type"] == "audio" and s["codec_name"] in ("aac", "opus"):
            if s["codec_name"] == "aac":
                caps = (
                    f"audio/mpeg, mpegversion=(int)4, framed=(boolean)true, stream-format=(string)raw, "
                    f"codec_data=(buffer){cd}, rate=(int){s['sample_rate']}, channels=(int){s['channels']}"
                )
            else:
                caps = (
                    f"audio/x-opus, rate=(int){s['sample_rate']}, channels=(int){s['channels']}, "
                    f"codec_data=(buffer){cd}"
                )
            streams[s["index"]] = ("audio", caps)
    packets = []
    for p in pk["packets"]:
        if p["stream_index"] not in streams:
            continue
        f = lambda k: float(p[k]) if p.get(k) not in (None, "N/A") else None
        packets.append(
            (
                streams[p["stream_index"]][0],
                f("pts_time"),
                f("dts_time"),
                f("duration_time"),
                int(p["pos"]),
                int(p["size"]),
                "K" in p["flags"],
            )
        )
    return streams, packets, info["streams"]


def build_clip(directory, name, video_args, audio_args, src="testsrc2=size=64x48:rate=25"):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            src,
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=997:sample_rate=48000",
            "-t",
            "1.6",
        ]
        + video_args
        + audio_args
        + [path],
        check=True,
    )
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-i",
            path,
            "-map",
            "0:v",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "yuv420p",
            os.path.join(directory, "video.yuv"),
        ],
        check=True,
    )
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-i",
            path,
            "-map",
            "0:a",
            "-f",
            "f32le",
            "-ac",
            "2",
            "-ar",
            "48000",
            os.path.join(directory, "audio.f32"),
        ],
        check=True,
    )
    return path


class ExtraHarness:
    PORTS = (0, 1)
    HELPER_ARGS = ()  # more of the helper's options (SoftwareOnlyTest: --software-only)

    def setUp(self):
        if not os.access(HUB, os.X_OK):
            self.skipTest(f"no {HUB}")
        if ("FFMPEG" not in os.environ and not shutil.which("ffmpeg")) or (
            "FFPROBE" not in os.environ and not shutil.which("ffprobe")
        ):
            self.skipTest("ffmpeg/ffprobe unavailable")
        self.dir = tempfile.TemporaryDirectory(prefix="media-extra-")
        self.addCleanup(self.dir.cleanup)
        self.qemu, self.guest, self.viewers = {}, {}, []
        names = ["qemu-video.sock"] + [f"qemu-media-{p}.sock" for p in self.PORTS]
        for name in names:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(s.close)
            s.bind(os.path.join(self.dir.name, name))
            s.listen(1)
            self.qemu[name] = s
        self.logfile = open(os.path.join(self.dir.name, "helper.log"), "w+")
        self.addCleanup(self.logfile.close)
        d = self.dir.name
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
                str(max(self.PORTS) + 1),
                *self.HELPER_ARGS,
            ],
            stdout=self.logfile,
            stderr=self.logfile,
        )
        self.addCleanup(self.stop_helper)
        for name, s in self.qemu.items():
            s.settimeout(5)
            self.guest[name], _ = s.accept()
            self.addCleanup(self.guest[name].close)

    def stop_helper(self):
        self.hub.terminate()
        self.hub.wait(5)
        if self._outcome and not self._outcome.success:
            self.logfile.seek(0)
            print(self.logfile.read())

    def viewer(self, port=0, preference=b"none"):
        v = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(v.close)
        v.connect(os.path.join(self.dir.name, f"media-{port}.sock"))
        v.send(bytes(Descriptor(magic=HM_MAGIC, version=1, type=HELLO, preference=preference)))
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

    def packet(self, port, packet, data):
        _, pts, dts, dur, pos, size, key = packet
        ns = lambda t: NONE if t is None else round(t * 1e9)
        self.send(
            port,
            BUFFER,
            struct.pack("<qqqqII", ns(pts), ns(dts), ns(dur), ns(pts), int(key), 0)
            + data[pos : pos + size],
        )

    def check_video(self, d, objects, reference, last_pts):
        """Assert one exported picture's pixels; return its pts. Releases the lease."""
        self.assertEqual(d.format, 1)
        if last_pts[0] is not None:
            self.assertGreaterEqual(
                d.pts, last_pts[0], f"pictures out of order: {d.pts} after {last_pts[0]}"
            )
        index = round(d.pts / FRAME_NS)
        self.assertEqual(
            objects[0],
            reference[index * FRAME_BYTES : (index + 1) * FRAME_BYTES],
            f"picture at pts {d.pts} differs from ffmpeg reference frame {index}",
        )
        last_pts[0] = d.pts

    def feed_video(self, port, video, data, reference, check=None):
        """Feed DTS-ordered packets with a present base and check every picture
        the helper exports: each must match the ffmpeg reference at its pts,
        in non-decreasing pts order. The helper retires due pictures into its
        single current slot (decoder.c), so only a subsequence of the decoded
        frames is exported; every received picture is checked and released.
        Ends at the EOS-drained final picture, which must be the last frame."""
        v, ack = self.viewer(port)
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        last_pts = [None]
        got = []

        def handle(d, objects):
            if check is not None:
                check(d, objects)
            else:
                self.check_video(d, objects, reference, last_pts)
            got.append(d.pts)

        for p in video:
            self.packet(port, p, data)
            while True:
                try:
                    d, objects = receive(v, 0.05)
                except socket.timeout:
                    break
                if d.type == VIDEO:
                    try:
                        handle(d, objects)
                    finally:
                        release(v, d)
            time.sleep(0.004)
        self.send(port, EOS)
        # Drain the EOS flush: the last picture out must be the last display
        # frame. With B-frames the decode-order tail packet carries an earlier
        # pts, so derive this from the reference frame count, not the packets.
        last_pts_value = (len(reference) // FRAME_BYTES - 1) * FRAME_NS
        end = time.monotonic() + 20
        while (not got or got[-1] != last_pts_value) and time.monotonic() < end:
            try:
                d, objects = receive(v, max(0.1, end - time.monotonic()))
            except socket.timeout:
                continue
            if d.type != VIDEO:
                continue
            try:
                handle(d, objects)
            finally:
                release(v, d)
        self.assertTrue(got, "helper exported no pictures")
        self.assertEqual(
            got[-1],
            last_pts_value,
            f"final picture pts {got[-1]} is not the last frame {last_pts_value}",
        )
        return v, got

    def clip(self, name, video_args, audio_args):
        directory = os.path.join(self.dir.name, "fixture")
        path = build_clip(directory, name, video_args, audio_args)
        with open(path, "rb") as f:
            data = f.read()
        streams, packets, _ = probe_all(path)
        with open(os.path.join(directory, "video.yuv"), "rb") as f:
            video_ref = f.read()
        with open(os.path.join(directory, "audio.f32"), "rb") as f:
            audio_ref = f.read()
        return path, streams, packets, data, video_ref, audio_ref


class ExtraTest(ExtraHarness, unittest.TestCase):
    """Single-window decode coverage: AV1, Opus, B-frames, HDR."""

    def test_av1_exact_pixels(self):
        _, streams, packets, data, video_ref, _ = self.clip(
            "av1.mp4",
            ["-c:v", "libaom-av1", "-cpu-used", "8", "-crf", "30", "-pix_fmt", "yuv420p"],
            ["-c:a", "aac", "-ac", "2"],
        )
        caps = next(c for k, c in streams.values() if k == "video")
        video = [p for p in packets if p[0] == "video"]
        self.assertTrue(video)
        self.start_stream(0, 1, caps, time.monotonic_ns() - 3_000_000_000)
        _, got = self.feed_video(0, video, data, video_ref)
        self.assertGreater(len(got), 1, "too few exported pictures to judge AV1 decode")

    def test_opus_exact_pcm(self):
        _, streams, packets, data, _, audio_ref = self.clip(
            "opus.mp4",
            ["-c:v", "libx264", "-threads", "1", "-bf", "0", "-g", "250", "-pix_fmt", "yuv420p"],
            ["-c:a", "libopus", "-ac", "2"],
        )
        caps = next(c for k, c in streams.values() if k == "audio")
        v, ack = self.viewer(1)
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        # Future base retains all PCM while this test checks and releases each lease.
        self.start_stream(1, 2, caps, time.monotonic_ns() + 10_000_000_000)
        received = 0
        actual_pcm, expected_pcm = bytearray(), bytearray()
        for p in (p for p in packets if p[0] == "audio"):
            self.packet(1, p, data)
            d, objects = self.next_frame(v, PCM)
            self.assertEqual((d.format, d.width, d.height), (6, 0, 0))
            if d.pts >= 0:
                offset = round(d.pts * 48000 / 1e9) * 8
                expected = audio_ref[offset : offset + d.samples * 8]
                actual = objects[0][: len(expected)]
                actual_pcm.extend(actual)
                expected_pcm.extend(expected)
                received += len(actual) // 8
            release(v, d)
        self.assertGreaterEqual(received, 48000)
        self.send(1, EOS)
        self.assertEqual(
            bytes(actual_pcm),
            bytes(expected_pcm),
            f"Opus PCM differs from independent reference "
            f"({len(actual_pcm) // 8} samples compared)",
        )

    def test_bframe_pts_order(self):
        _, streams, packets, data, video_ref, _ = self.clip(
            "bframes.mp4",
            ["-c:v", "libx264", "-threads", "1", "-bf", "2", "-g", "25", "-pix_fmt", "yuv420p"],
            ["-c:a", "aac", "-ac", "2"],
        )
        caps = next(c for k, c in streams.values() if k == "video")
        video = [p for p in packets if p[0] == "video"]
        ordered = all(
            a[2] is not None and b[2] is not None and a[2] <= b[2] for a, b in zip(video, video[1:])
        )
        self.assertTrue(ordered, "fixture packets are not in DTS order")
        reordered = any(p[1] != p[2] for p in video if p[1] is not None and p[2] is not None)
        self.assertTrue(reordered, "fixture has no B-frame pts/dts reordering to check")
        self.start_stream(0, 1, caps, time.monotonic_ns() - 3_000_000_000)
        _, got = self.feed_video(0, video, data, video_ref)
        self.assertEqual(got, sorted(got), "exported pictures are not in pts order")

    def test_close_releases_decoder(self):
        """CLOSE with nothing queued ends the session's codec (its frame threads) at once, not at the
        next session (frame.c's end_session)."""
        _, streams, packets, data, video_ref, _ = self.clip(
            "close.mp4",
            ["-c:v", "libx264", "-threads", "1", "-g", "25", "-pix_fmt", "yuv420p"],
            ["-c:a", "aac", "-ac", "2"],
        )
        caps = next(c for k, c in streams.values() if k == "video")
        threads = lambda: len(os.listdir(f"/proc/{self.hub.pid}/task"))
        idle = threads()
        self.start_stream(0, 1, caps, time.monotonic_ns() - 3_000_000_000)
        self.feed_video(0, [p for p in packets if p[0] == "video"], data, video_ref)
        if (os.cpu_count() or 1) > 1:
            self.assertGreater(threads(), idle, "no frame threads while decoding")
        self.send(0, CLOSE)
        end = time.monotonic() + 5
        while threads() > idle and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(threads(), idle, "the closed session's decoder threads are still there")

    def test_hdr_metadata(self):
        directory = os.path.join(self.dir.name, "fixture")
        path = build_clip(
            directory,
            "hdr.mp4",
            [
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
                "-x264-params",
                "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
            ],
            ["-c:a", "aac", "-ac", "2"],
        )
        with open(path, "rb") as f:
            data = f.read()
        streams, packets, raw = probe_all(path)
        video_stream = next(s for s in raw if s["codec_type"] == "video")
        self.assertEqual(
            video_stream.get("color_space"),
            "bt2020nc",
            f"fixture is not BT.2020-tagged: {video_stream.get('color_space')}",
        )
        expected_cs = {"bt2020nc": 9}[video_stream["color_space"]]
        expected_range = {"tv": 1, "pc": 2, "unknown": 0}[
            video_stream.get("color_range", "unknown")
        ]
        caps = next(c for k, c in streams.values() if k == "video")
        video = [p for p in packets if p[0] == "video"]
        with open(os.path.join(directory, "video.yuv"), "rb") as f:
            video_ref = f.read()

        def check(d, objects):
            self.assertEqual(
                (d.colorspace, d.range),
                (expected_cs, expected_range),
                f"descriptor colorspace={d.colorspace} range={d.range}, "
                f"ffprobe declares {video_stream.get('color_space')}/"
                f"{video_stream.get('color_range')}",
            )
            self.assertEqual(d.format, 1)

        self.start_stream(0, 1, caps, time.monotonic_ns() - 3_000_000_000)
        self.feed_video(0, video, data, video_ref, check=check)


class DualWindowTest(ExtraHarness, unittest.TestCase):
    """Two video streams on ports 0 and 2 decode and export independently."""

    PORTS = (0, 1, 2)

    def test_two_video_windows(self):
        clips = []
        for name, src in (
            ("win-a.mp4", "testsrc2=size=64x48:rate=25"),
            ("win-b.mp4", "testsrc=size=64x48:rate=25"),
        ):
            directory = os.path.join(self.dir.name, name + ".d")
            path = build_clip(
                directory,
                name,
                [
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
                ],
                ["-c:a", "aac", "-ac", "2"],
                src=src,
            )
            with open(path, "rb") as f:
                data = f.read()
            streams, packets, _ = probe_all(path)
            with open(os.path.join(directory, "video.yuv"), "rb") as f:
                video_ref = f.read()
            clips.append((streams, packets, data, video_ref))
        started = []
        for port, (streams, packets, data, video_ref) in zip((0, 2), clips):
            caps = next(c for k, c in streams.values() if k == "video")
            video = [p for p in packets if p[0] == "video"]
            self.assertTrue(video, f"port {port} fixture has no video packets")
            self.start_stream(port, 1, caps, time.monotonic_ns() - 3_000_000_000)
            started.append((port, video, data, video_ref))
        # Port 0 first, while port 2's stream stays open but unfed.
        v0, got = self.feed_video(*started[0])
        self.assertGreater(len(got), 1, "port 0 exported too few pictures")
        # Closing port 0 must not disturb port 2: its whole decode happens after.
        # (One viewer per port: drop port 0's feed connection before reconnecting.)
        v0.close()
        self.send(0, CLOSE)
        # The helper notices the dropped feed connection on its poll round;
        # retry until the port accepts its next viewer.
        v, end = None, time.monotonic() + 5
        while v is None and time.monotonic() < end:
            try:
                v, _ = self.viewer(0)
            except (ConnectionResetError, socket.timeout, AssertionError):
                time.sleep(0.1)
        self.assertIsNotNone(v, "port 0 accepted no viewer after close")
        closed, end = None, time.monotonic() + 5
        while closed is None and time.monotonic() < end:
            try:
                d, _ = receive(v, max(0.1, end - time.monotonic()))
            except socket.timeout:
                continue
            if d.type == SNAPSHOT and not (d.flags & 1):
                closed = d
        self.assertIsNotNone(closed, "port 0 did not report close")
        v.close()
        _, got = self.feed_video(*started[1])
        self.assertGreater(len(got), 1, "port 2 exported too few pictures after port 0 close")


class SoftwareOnlyTest(ExtraHarness, unittest.TestCase):
    """--software-only (a VM's hwdec=none): software decoding locked against the viewers'
    preferences, and the short codec list (host/media/decode/caps.c, caps_software_only)."""

    HELPER_ARGS = ("--software-only",)
    LOCKED = 4  # HM_PREFERENCE_LOCKED (include/host-media.h)

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

    def test_viewer_preference_locked(self):
        """A viewer asking for hardware (or auto) gets the locked status and none kept; none
        is acknowledged as before."""
        for preference in (b"cuda", b"vaapi", b"auto"):
            v, ack = self.viewer(0, preference)
            if ack.flags & 16:
                self.skipTest("helper explicitly reports decoder unavailable")
            self.assertEqual(ack.status, self.LOCKED, preference)
            self.assertEqual(ack.preference, b"none", preference)
            v.close()
            time.sleep(0.05)
        _, ack = self.viewer(0)
        self.assertEqual(ack.status, 1)
        self.assertEqual(ack.preference, b"none")
        self.assertIn("media 0: viewer's hwdec cuda refused: software only", self.log())
        self.assertIn("software decoding only", self.log())

    def test_hardware_viewer_still_gets_software_pictures(self):
        """H.264 decodes in software, pixel for pixel, for a viewer whose hardware preference
        was refused; no device is opened."""
        _, streams, packets, data, video_ref, _ = self.clip(
            "h264.mp4",
            ["-c:v", "libx264", "-threads", "1", "-bf", "0", "-g", "25", "-pix_fmt", "yuv420p"],
            ["-c:a", "aac", "-ac", "2"],
        )
        caps = next(c for k, c in streams.values() if k == "video")
        video = [p for p in packets if p[0] == "video"]
        v, ack = self.viewer(0, b"cuda")
        if ack.flags & 16:
            self.skipTest("helper explicitly reports decoder unavailable")
        self.assertEqual(ack.status, self.LOCKED)
        v.close()
        time.sleep(0.05)  # the helper sees the close before the next viewer connects
        self.start_stream(0, 1, caps, time.monotonic_ns() - 3_000_000_000)
        _, got = self.feed_video(0, video, data, video_ref)
        self.assertGreater(len(got), 1)
        log = self.log()
        self.assertIn("media 0: decoder h264 (software)", log)
        self.assertNotIn("hw device", log)

    def test_refused_codecs(self):
        """WMV, VC-1, MJPEG, H.263, DTS and WMA caps open no decoder."""
        for caps, name in (
            ("video/x-wmv, wmvversion=(int)3, width=(int)64, height=(int)48", "wmv3"),
            ("video/x-wmv, wmvversion=(int)3, format=(string)WVC1", "vc1"),
            ("image/jpeg, width=(int)64, height=(int)48", "mjpeg"),
            ("video/x-h263", "h263"),
            ("audio/x-dts, rate=(int)48000, channels=(int)2", "dts"),
            ("audio/x-wma, wmaversion=(int)2, rate=(int)48000, channels=(int)2", "wmav2"),
        ):
            with self.subTest(name):
                kind = 2 if caps.startswith("audio/") else 1
                self.start_stream(0, kind, caps, time.monotonic_ns())
                self.assertTrue(
                    self.wait_log(f"media 0: {name} refused: hwdec=none decodes a short list"),
                    self.log()[-2000:],
                )
                self.send(0, CLOSE)
        self.assertNotIn("decoder wmv3", self.log())

    def test_av1_through_dav1d(self):
        """AV1 still decodes, through libdav1d."""
        ExtraTest.test_av1_exact_pixels(self)
        self.assertIn("media 0: decoder libdav1d (software)", self.log())

    def test_opus(self):
        ExtraTest.test_opus_exact_pcm(self)


class SoftwareOnlyArgsTest(unittest.TestCase):
    """--software-only needs --hwdec none and the sandbox: refused at the start otherwise."""

    def test_refused_combinations(self):
        if not os.access(HUB, os.X_OK):
            self.skipTest(f"no {HUB}")
        with tempfile.TemporaryDirectory(prefix="media-extra-") as d:
            base = [HUB, "--pid", str(os.getpid()), "--video", f"{d}/q.sock:{d}/v.sock"]
            base += ["--media", f"{d}/qm:{d}/m", "--software-only"]
            for extra in (
                ["--hwdec", "auto"],
                ["--hwdec", "cuda"],
                ["--hwdec", "none", "--no-sandbox"],
            ):
                with self.subTest(extra):
                    r = subprocess.run(base + extra, capture_output=True, text=True, timeout=10)
                    self.assertEqual(r.returncode, 2, r.stderr)
                    self.assertIn("--software-only needs --hwdec none", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
