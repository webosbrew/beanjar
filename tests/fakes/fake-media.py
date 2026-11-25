#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A stand-in for the guest's media pipeline, to check helper decoding and viewer presentation without a VM
(include/webos-media.h).

    fake-media.py PREFIX TABLE.sock FILE.mp4 [--seconds S] [--pause AT:FOR] [--seek AT:TO]
                  [--rect X,Y,W,H] [--lead MS]

It serves what QEMU's chardevs would: TABLE.sock (webos.video: one MAIN window on VDEC 1 at RECT)
and PREFIX-0.sock / PREFIX-1.sock (webos.media.0 video, .1 audio). Then it plays FILE's H.264 or
HEVC video and AAC audio (the samples read from the MP4 as ffprobe lists them, codec_data as
qtdemux's caps carry it) like vdecsink/adecsink would: preroll, PLAYING with a base time on a
pipeline clock that is this host's CLOCK_MONOTONIC, each buffer sent LEAD ahead of its running
time, pause (STATE with the pause's running time, resumed with a new base time), a flushing seek
(FLUSH, buffers from the keyframe before the target as decode-only, running time from 0 again),
EOS, CLOSE. It launches the real media-helper between these compressed guest sockets and
the public decoded sockets. Point the viewer at the public sockets:

    beanjar-viewer unix:path=build/dbus.sock --headless --seconds 30 --no-audio \\
        --video-socket TABLE.sock --media-socket PREFIX --media-wav out.wav ...

It prints what it sent, with times, for comparing with the viewer's log.
"""

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import tempfile
import time

MAGIC = 0x324D5357
NONE = -(1 << 63)
OPEN, CAPS, BUFFER, STATE, FLUSH, EOS, CLOSE = range(1, 8)
KEYFRAME, DECODE_ONLY = 1, 2


def check(words):
    x = 0x9E3779B9
    for w in words:
        x ^= w & 0xFFFFFFFF
        x = (x * 0x01000193) & 0xFFFFFFFF
        x ^= x >> 15
    return x


def mono():
    return time.monotonic_ns()


class Port:
    def __init__(self, path):
        if os.path.exists(path):
            os.unlink(path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(1)
        self.path = path
        self.conn = None
        self.seq = 0
        self.lock = threading.Lock()

    def accept(self):
        self.conn, _ = self.server.accept()

    def send(self, type_, payload=b"", clock=True):
        with self.lock:
            self.seq += 1
            now = mono()
            c = now if clock else NONE
            words = [
                MAGIC,
                type_,
                len(payload),
                self.seq,
                now & 0xFFFFFFFF,
                (now >> 32) & 0xFFFFFFFF,
                c & 0xFFFFFFFF,
                (c >> 32) & 0xFFFFFFFF,
            ]
            h = struct.pack(
                "<IHHIIqqII", MAGIC, type_, 0, len(payload), self.seq, now, c, check(words), 0
            )
            self.conn.sendall(h + payload)


def table_thread(path, rect, stop):
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    conn, _ = srv.accept()
    seq = 0
    x, y, w, h = rect
    while not stop.is_set():
        seq += 1
        # struct webos_video_context: MAIN, open, connected, not muted, the window, VDEC port 1
        ctx = struct.pack(
            "<48s5I2i12I", b"MAIN", 1, 1, 0, 1, 0, x, y, w, h, 0, 0, 0, 0, 3, 1, 0, 0, 0, 0
        )
        body = ctx + b"\0" * (124 * 7)
        pkt = b"WOSVID3\0" + struct.pack("<7I", 1028, seq, 0, 8, 1920, 1080, 1) + body
        try:
            conn.sendall(pkt)
        except OSError:
            break
        stop.wait(1)
    conn.close()
    srv.close()


def extradata_hex(s):
    out = []
    for line in s.strip().splitlines():
        part = line.split(":", 1)[1][:40]
        out.append(part.replace(" ", ""))
    return "".join(out)


def probe(path):
    # Target-native tools for cross-arch runs (references must come from the
    # same FFmpeg build that decodes: FFMPEG=/path/to/target/ffmpeg); PATH
    # tools otherwise.
    ffprobe = os.environ.get("FFPROBE", "ffprobe")
    info = json.loads(
        subprocess.check_output(
            [ffprobe, "-v", "error", "-show_streams", "-show_data", "-of", "json", path]
        )
    )
    pk = json.loads(
        subprocess.check_output(
            [
                ffprobe,
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
            }[s["codec_name"]]
            fr = s.get("avg_frame_rate", "0/1")
            caps = (
                f"{name}, alignment=(string)au, codec_data=(buffer){cd}, width=(int){s['width']}, "
                f"height=(int){s['height']}, framerate=(fraction){fr}, pixel-aspect-ratio=(fraction)1/1"
            )
            streams[s["index"]] = ("video", caps)
        elif s["codec_type"] == "audio" and s["codec_name"] == "aac":
            caps = (
                f"audio/mpeg, mpegversion=(int)4, framed=(boolean)true, stream-format=(string)raw, "
                f"codec_data=(buffer){cd}, rate=(int){s['sample_rate']}, channels=(int){s['channels']}"
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
    return streams, packets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix")
    ap.add_argument("table")
    ap.add_argument("file")
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--pause", default="")
    ap.add_argument("--seek", default="")
    ap.add_argument("--rect", default="320,180,960,540")
    ap.add_argument("--lead", type=int, default=300)
    ap.add_argument(
        "--helper",
        default=os.environ.get("MEDIAHUB")
        or os.path.join(os.path.dirname(__file__), "../../build/media-helper/media-helper"),
    )
    ap.add_argument("--helper-log", help="default: media-helper.log beside PREFIX")
    ap.add_argument("--hwdec", default=os.environ.get("MEDIA_HWDEC", "auto"))
    a = ap.parse_args()
    streams, packets = probe(a.file)
    with open(a.file, "rb") as f:
        data = f.read()
    lead = a.lead * 1000000
    if not os.access(a.helper, os.X_OK):
        ap.error(
            "an executable media-helper is required; direct compressed viewer sockets are unsupported"
        )
    stop = threading.Event()
    temporary = tempfile.TemporaryDirectory(prefix="fake-media-")
    guest_prefix = os.path.join(temporary.name, "media")
    guest_table = os.path.join(temporary.name, "video.sock")
    threading.Thread(
        target=table_thread,
        args=(guest_table, [int(v) for v in a.rect.split(",")], stop),
        daemon=True,
    ).start()
    ports = {"video": Port(guest_prefix + "-0.sock"), "audio": Port(guest_prefix + "-1.sock")}
    helper_log = open(
        a.helper_log or os.path.join(os.path.dirname(a.prefix), "media-helper.log"), "w"
    )
    helper = subprocess.Popen(
        [
            a.helper,
            "--pid",
            str(os.getpid()),
            "--hwdec",
            a.hwdec,
            "--video",
            guest_table + ":" + a.table,
            "--media",
            guest_prefix + ":" + a.prefix,
            "--ports",
            "2",
        ],
        stdout=helper_log,
        stderr=helper_log,
    )
    try:
        print("waiting for the media helper", flush=True)
        for p in ports.values():
            p.accept()
        t0 = mono()
        log = lambda *m: print("%7.3f" % ((mono() - t0) / 1e9), *m, flush=True)
        for kind, caps in streams.values():
            port = ports[kind]
            port.send(
                OPEN,
                struct.pack(
                    "<IiiI32s",
                    1 if kind == "video" else 2,
                    os.getpid(),
                    1,
                    0,
                    b"fake-" + kind.encode(),
                ),
                clock=False,
            )
            port.send(CAPS, caps.encode() + b"\0", clock=False)
            log("open", kind, caps[:60])

        def state(playing, base, pause_rt):
            for p in ports.values():
                p.send(STATE, struct.pack("<IIqqd", playing, 0, base, pause_rt, 1.0))

        def buffer(kind, pts, dts, dur, pos, size, key, seg_start, flags=0):
            ns = lambda t: NONE if t is None else int(round((t - seg_start) * 1e9))
            b = struct.pack(
                "<qqqqII",
                ns(pts),
                ns(dts),
                NONE if dur is None else int(dur * 1e9),
                NONE if pts is None else int(pts * 1e9),
                flags | (KEYFRAME if key else 0),
                0,
            )
            ports[kind].send(BUFFER, b + data[pos : pos + size])

        pause_at, pause_for = [float(v) for v in a.pause.split(":")] if a.pause else (None, 0)
        seek_at, seek_to = [float(v) for v in a.seek.split(":")] if a.seek else (None, 0)
        seg_start = 0.0
        # preroll: the first buffer of each stream, paused
        state(0, 0, NONE)
        i = 0
        base = mono() + 500000000
        state(1, base, NONE)
        log("playing, base", base)
        order = sorted(
            range(len(packets)),
            key=lambda k: packets[k][2] if packets[k][2] is not None else packets[k][1],
        )
        seq = [packets[k] for k in order]
        while i < len(seq):
            kind, pts, dts, dur, pos, size, key = seq[i]
            now = mono()
            rt_now = now - base
            if pause_at is not None and rt_now >= pause_at * 1e9 + seg_start * 0:
                pause_rt = now - base
                state(0, base, pause_rt)
                log("paused at rt %.3f" % (pause_rt / 1e9))
                time.sleep(pause_for)
                base = mono() - pause_rt
                state(1, base, NONE)
                log("resumed, base", base)
                pause_at = None
                continue
            if seek_at is not None and rt_now >= seek_at * 1e9:
                pause_rt = mono() - base
                state(0, base, pause_rt)
                for p in ports.values():
                    p.send(FLUSH)
                log("seek to %.3f" % seek_to)
                seg_start = seek_to
                # from the keyframe before the target
                keys = [
                    k
                    for k, s in enumerate(seq)
                    if s[0] == "video" and s[6] and (s[1] or 0) <= seek_to
                ]
                i = keys[-1] if keys else 0
                seek_at = None
                # preroll then play from running time 0
                base = mono() + 300000000
                state(1, base, NONE)
                log("playing after seek, base", base)
                continue
            ts = dts if dts is not None else pts
            rt = int((ts - seg_start) * 1e9)
            due = base + rt - lead
            if due > now:
                time.sleep(min((due - now) / 1e9, 0.05))
                continue
            flags = 0
            if pts is not None and pts < seg_start:
                if kind == "audio":
                    i += 1
                    continue
                flags = DECODE_ONLY
            buffer(kind, pts, dts, dur, pos, size, key, seg_start, flags)
            i += 1
            if a.seconds and (mono() - t0) / 1e9 > a.seconds:
                break
        # EOS when the last is due
        end = max((p[1] or 0) + (p[3] or 0) for p in seq) - seg_start
        while mono() < base + int(end * 1e9):
            time.sleep(0.01)
        for p in ports.values():
            p.send(EOS)
        log("eos")
        time.sleep(1.0)
        for p in ports.values():
            p.send(CLOSE)
        log("close")
        stop.set()
        time.sleep(0.3)

    finally:
        stop.set()
        for p in ports.values():
            if p.conn:
                p.conn.close()
            p.server.close()
        helper.terminate()
        try:
            helper.wait(5)
        except subprocess.TimeoutExpired:
            helper.kill()
            helper.wait()
        helper_log.close()
        temporary.cleanup()


if __name__ == "__main__":
    main()
