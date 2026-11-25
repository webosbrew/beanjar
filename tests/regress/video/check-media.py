#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check a tests/regress/video/check-media.sh run (video milestone 2): the viewer's log, its captures
(OUT/shot-<ms>.ppm, 960x540) and the media audio it recorded (OUT/media.wav, 48 kHz float stereo).

    check-media.py OUT [--rect X,Y,W,H] [--no-audio] [--no-pause]

Prints PASS/FAIL lines and exits non-zero on a failure:
  - decoding: a video decoder opened and pictures were shown (the log's "media N video" report);
  - the window: captures whose window rect (guest pixels, default 320,180 960x540, scaled to the
    capture) holds a picture: not the milestone 1 pattern, not black, and colourful (the test
    clips are testsrc2 patterns: at least 20 % saturated samples);
  - the pause: two consecutive captures with the same picture in the rect while the stream was
    paused, and pictures changing again after it;
  - the audio: samples recorded above -40 dBFS, and the output's slip against the pipeline clock;
  - timing: the video's lateness and the audio's slip as the viewer measured them;
  - flush (seek) and end of stream seen.
"""

import argparse
import glob
import math
import os
import re
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import tvsuite  # noqa: E402  (sound_status, judge_media_level: the TV's volume scales the recording)


def ppm(path):
    d = open(path, "rb").read()
    parts = d.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    return w, h, parts[3]


def region(path, rect):
    w, h, px = ppm(path)
    x, y, rw, rh = rect
    sx, sy = w / 1920, h / 1080
    x0, y0, x1, y1 = (
        int(x * sx) + 2,
        int(y * sy) + 2,
        int((x + rw) * sx) - 2,
        int((y + rh) * sy) - 2,
    )
    out = []
    for yy in range(y0, y1, 3):
        for xx in range(x0, x1, 3):
            out.append(px[(yy * w + xx) * 3 : (yy * w + xx) * 3 + 3])
    return b"".join(out)


def classify(sample):
    n = len(sample) // 3
    pattern = black = colour = 0
    for i in range(n):
        r, g, b = sample[3 * i : 3 * i + 3]
        if max(r, g, b) - min(r, g, b) > 80:
            colour += 1
        if (abs(r - 217) < 6 and abs(g - 217) < 6 and abs(b - 217) < 6) or (
            abs(r - 31) < 6 and abs(g - 31) < 6 and abs(b - 102) < 6
        ):
            pattern += 1
        if r < 12 and g < 12 and b < 12:
            black += 1
    return pattern / max(n, 1), black / max(n, 1), colour / max(n, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--rect", default="320,180,960,540")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument(
        "--sound-status", help="the run's sound-status file (the TV's volume; none: unity)"
    )
    ap.add_argument("--no-pause", action="store_true")
    a = ap.parse_args()
    rect = [int(v) for v in a.rect.split(",")]
    log = open(os.path.join(a.out, "viewer.log"), errors="replace").read()
    helper = open(os.path.join(a.out, "media-helper.log"), errors="replace").read()
    fails = 0

    def result(ok, msg):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + msg)
        fails += not ok

    dec = re.findall(r"media \d+: decoder (\S+) \((\w+)\)", helper)
    print("decoders:", ", ".join(f"{n} ({k})" for n, k in dec) or "none")
    vids = re.findall(
        r"present \d+ video[^:]*: received=(\d+) shown=(\d+) picked=\d+ late_drops=(\d+) lateness avg=([\d.]+) max=([\d.]+)",
        log,
    )
    shown = max((int(v[1]) for v in vids), default=0)
    result(
        shown >= 50
        and bool(re.search(r"decode \d+ video[^:]*: \S+ \S+ packets=\d+ decoded=[1-9]\d*", helper)),
        f"video pictures shown: {shown}; helper decoders: {dec}",
    )
    lates = [float(v[3]) for v in vids if int(v[1]) > 0]
    if lates:
        print(
            f"video lateness (display after due, per 5 s report): avg {sum(lates) / len(lates):.1f} ms, "
            f"max avg {max(lates):.1f} ms"
        )

    shots = sorted(glob.glob(os.path.join(a.out, "shot-*.ppm")))
    regions = [region(p, rect) for p in shots]
    pictures = []
    for p, r in zip(shots, regions):
        pat, blk, col = classify(r)
        pictures.append(pat < 0.05 and blk < 0.9 and col > 0.2)  # the test clips are colourful
    result(
        sum(pictures) >= 3,
        f"captures with a decoded picture at {a.rect}: {sum(pictures)} of {len(shots)}",
    )
    same = [
        i
        for i in range(1, len(regions))
        if pictures[i] and pictures[i - 1] and regions[i] == regions[i - 1]
    ]
    moving = [
        i
        for i in range(1, len(regions))
        if pictures[i] and pictures[i - 1] and regions[i] != regions[i - 1]
    ]
    print(
        f"consecutive captures: {len(moving)} changed, {len(same)} identical (at s {[i for i in same]})"
    )
    if not a.no_pause:
        paused = re.search(r"media \d+: video paused base_time=[\d.]+ s pause_rt=([\d.]+)", helper)
        result(
            bool(paused) and bool(same) and bool(moving),
            f"pause: stream paused at rt {paused.group(1) if paused else '?'} s, picture frozen in "
            f"{len(same)} capture pair(s), moving in {len(moving)}",
        )
        result("flush video" in helper, "seek: video flushed")
    print("end of stream: " + ("seen" if "end of stream (video)" in helper else "not seen"))

    if not a.no_audio:
        auds = re.findall(
            r"present \d+ audio[^:]*: frames=(\d+) slip avg=([\d.]+) max=([\d.]+) ms resyncs=(\d+) nudges=(\d+) underruns=(\d+)",
            log,
        )
        frames = max((int(x[0]) for x in auds), default=0)
        slips = [float(x[1]) for x in auds if int(x[0]) > 0]
        result(
            frames > 48000
            and bool(
                re.search(r"decode \d+ audio[^:]*: \S+ \S+ packets=\d+ decoded=[1-9]\d*", helper)
            ),
            f"audio decoded and played: {frames / 48000:.1f} s; helper decoders: {dec}",
        )
        if slips:
            result(
                sum(slips) / len(slips) < 15,
                f"audio slip against the pipeline clock: avg {sum(slips) / len(slips):.1f} ms "
                f"(per 5 s report, max avg {max(slips):.1f} ms)",
            )
        wav = os.path.join(a.out, "media.wav")
        if os.path.exists(wav):
            d = open(wav, "rb").read()[44:]
            n = len(d) // 4
            samples = struct.unpack("<%df" % n, d[: n * 4])
            loud = [s for s in samples if s != 0.0]
            rms = math.sqrt(sum(s * s for s in loud) / len(loud)) if loud else 0
            db = 20 * math.log10(rms) if rms > 0 else -math.inf
            try:
                with open(a.sound_status, errors="replace") as f:
                    status = tvsuite.sound_status(f.read())
            except (OSError, TypeError):
                status = None
            ok, why = tvsuite.judge_media_level(db if db > -math.inf else None, status, -40.0)
            result(
                ok,
                f"media audio recorded: {len(loud) / 2 / 48000:.1f} s non-silent of {n / 2 / 48000:.1f} s, "
                f"{why}",
            )
    cpu = re.search(r"media: viewer cpu .*", log)
    if cpu:
        print(cpu.group(0))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
