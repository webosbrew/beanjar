#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Print measurements from alternating color-conversion runs, without verdicts."""

import argparse
import csv
import io
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--encoders", nargs="+", default=["x264"])
    parser.add_argument("--frames", type=int, default=240)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--resume", action="store_true", help="retain completed CSV rows")
    args = parser.parse_args()
    checked = set()
    completed = set()
    if args.resume and args.output.exists():
        with args.output.open(newline="") as previous:
            for row in csv.DictReader(previous):
                key = (row["encoder"], int(row["width"]), int(row["height"]))
                completed.add((*key, int(row["repeat"]), row["mode"]))
                if row["mode"] == "gpu":
                    checked.add(key)
    with args.output.open("a" if completed else "w", newline="") as output:
        writer = None
        for encoder in args.encoders:
            for width, height in [(1280, 720), (1920, 1080)]:
                for repeat in range(args.rounds):
                    # Reverse order each round to expose drift. CPU-only baseline
                    # runs at the beginning and end of each resolution/encoder.
                    modes = ["read-sws", "gpu"] if repeat % 2 == 0 else ["gpu", "read-sws"]
                    if repeat == 0:
                        modes.insert(0, "cpu")
                    if repeat == args.rounds - 1:
                        modes.append("cpu")
                    for mode in modes:
                        if (encoder, width, height, repeat, mode) in completed:
                            continue
                        env = os.environ.copy()
                        key = (encoder, width, height)
                        if mode != "gpu" or key in checked:
                            env["BENCH_SKIP_CHECK"] = "1"
                        else:
                            checked.add(key)
                        command = [
                            str(args.binary.resolve()),
                            str(width),
                            str(height),
                            str(args.frames),
                            mode,
                            encoder,
                        ]
                        process = subprocess.Popen(
                            command,
                            env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        print(
                            f"pid={process.pid} machine={args.machine} {width}x{height} "
                            f"encoder={encoder} mode={mode} repeat={repeat}",
                            flush=True,
                        )
                        try:
                            stdout, stderr = process.communicate(timeout=180)
                        except BaseException:
                            process.kill()
                            process.communicate()
                            raise
                        sys.stderr.write(stderr)
                        if process.returncode:
                            raise RuntimeError(f"benchmark exited {process.returncode}")
                        row = next(csv.DictReader(io.StringIO(stdout)))
                        row = {"machine": args.machine, "repeat": repeat, **row}
                        if writer is None:
                            writer = csv.DictWriter(output, fieldnames=row)
                            if not completed:
                                writer.writeheader()
                        writer.writerow(row)
                        output.flush()
                        print(",".join(map(str, row.values())), flush=True)


if __name__ == "__main__":
    main()
