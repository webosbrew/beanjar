#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar-vnc's CPU per video frame: builds compared in alternating runs, numbers only.

    bench-vnc.py LABEL=BEANJAR_VNC [LABEL=BEANJAR_VNC ...] [--encoder x264] [--size 1280x720]
                 [--seconds 10] [--rounds 3] [--source animate|dmabuf] [--csv PATH]
                 [--env LABEL:NAME=VALUE ...] [--logs DIR] [--vnc-arg=ARG ...]
                 [--source-arg=ARG ...] [--clients video|rfb|video+rfb] [--rfb-encodings NAME]

The source is tests/fakes/fake-qemu.py: --animate (a memfd, its top quarter new grey noise every
33 ms) or --dmabuf (GBM buffers flipped at 60 a second; needs a render node with GBM). Each run
starts beanjar-vnc on the source with --video-local and one video client asking for
"h264 bitrate=10000" that reads everything; after 3 s of warm-up it measures for --seconds: the
process's CPU time (all threads, /proc/PID/stat), its main thread's (the compose, the read backs,
the GPU conversion), the units the client got. The runs alternate between the builds, the order
reversed each round. One CSV row a run, on stdout or to --csv, then the median of each build's
runs. No thresholds: it prints, it does not judge.
"""

import argparse
import csv
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import loopback  # noqa: E402
import videostream as vs  # noqa: E402
from rfbclient import ENCODINGS, NAMES, Rfb  # noqa: E402

TICK = os.sysconf("SC_CLK_TCK")


def cpu(pid, tid=None):
    """utime + stime in seconds, of the process or of one of its threads."""
    path = f"/proc/{pid}/stat" if tid is None else f"/proc/{pid}/task/{tid}/stat"
    fields = open(path).read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / TICK


class Reader:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
        self.sock.sendall(b"h264 bitrate=10000\n")
        self.data = bytearray()
        self.stop = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                chunk = self.sock.recv(1 << 20)
            except TimeoutError:
                continue
            except OSError:
                return
            if not chunk:
                return
            self.data += chunk

    def units(self):
        try:
            info, body = vs.header(bytes(self.data))
        except ValueError:
            return None, 0
        return info, len(vs.units(body)[0])

    def close(self):
        self.stop = True
        self.thread.join(5)
        self.sock.close()


def stats(p, log_path):
    """beanjar-vnc's SIGUSR1 counters (docs/dev/architecture/vnc.md, "Counters")."""
    before = open(log_path).read().count("beanjar-vnc: stats ")
    p.send_signal(signal.SIGUSR1)
    for _ in range(100):
        lines = [x for x in open(log_path).read().splitlines() if "beanjar-vnc: stats " in x]
        if len(lines) > before:
            return dict(f.split("=", 1) for f in lines[-1].split()[2:])
        time.sleep(0.02)
    raise RuntimeError("no stats line")


def run_one(binary, env, d, size, encoder, seconds, log_path, extra, clients="video",
            rfb_encodings="novnc"):  # fmt: skip
    sock = os.path.join(d, "video.sock")
    local = os.path.join(d, "rfb.sock")
    log = open(log_path, "w")
    p = subprocess.Popen(
        [binary, f"unix:path={d}/dbus.sock", "--listen", loopback.ADDRESS, "--port",
         str(loopback.port()), "--size", size, "--fps", "60", "--no-media", "--native-cursor",
         "--video-local", sock, "--video-encoder", encoder, "--local", local, *extra],
        stdout=log, stderr=subprocess.STDOUT, env=env,
    )  # fmt: skip
    print(f"bench-vnc: beanjar-vnc pid {p.pid}", file=sys.stderr)
    reader = rfb = None
    try:
        for _ in range(200):
            if "beanjar-vnc: ready" in open(log_path).read():
                break
            if p.poll() is not None:
                raise RuntimeError(f"beanjar-vnc exited {p.returncode}: {open(log_path).read()}")
            time.sleep(0.05)
        else:
            raise RuntimeError("beanjar-vnc not ready")
        if "video" in clients:
            reader = Reader(sock)
        if "rfb" in clients:
            rfb = Rfb(local, ENCODINGS[rfb_encodings])
        time.sleep(3)
        u0 = reader.units()[1] if reader else 0
        r0 = (rfb.updates, rfb.bytes) if rfb else (0, 0)
        s0 = stats(p, log_path)
        c0, m0, t0 = cpu(p.pid), cpu(p.pid, p.pid), time.monotonic()
        time.sleep(seconds)
        u1 = reader.units()[1] if reader else 0
        r1 = (rfb.updates, rfb.bytes) if rfb else (0, 0)
        c1, m1, t1 = cpu(p.pid), cpu(p.pid, p.pid), time.monotonic()
        s1 = stats(p, log_path)
        info = reader.units()[0] if reader else None
        if rfb and rfb.error:
            raise RuntimeError(f"RFB client: {rfb.error}")
        rects = dict(rfb.encodings) if rfb else {}
    finally:
        if reader:
            reader.close()
        if rfb:
            rfb.close()
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        log.close()
    text = open(log_path).read()
    path = (
        "gpu"
        if "kbit/s, RGB to NV12 on the GPU" in text
        else "swscale"
        if "kbit/s, RGB to YUV by swscale" in text
        else "?"
    )
    span = t1 - t0
    renders = (
        int(s1["renders"]) - int(s0["renders"]) - (int(s1["unchanged"]) - int(s0["unchanged"]))
    )
    # the frames: the video units, else the frames composed and handed on (not unchanged)
    frames = u1 - u0 if reader else renders
    return {
        "clients": clients if "rfb" not in clients else f"{clients}:{rfb_encodings}",
        "encoder": info[5] if info else "none",
        "size": size,
        "path": path,
        "seconds": f"{span:.2f}",
        "frames": frames,
        "fps": f"{frames / span:.2f}",
        "cpu_ms_per_frame": f"{(c1 - c0) * 1000 / frames:.2f}" if frames else "",
        "main_ms_per_frame": f"{(m1 - m0) * 1000 / frames:.2f}" if frames else "",
        "cpu_percent": f"{(c1 - c0) * 100 / span:.1f}",
        "renders": renders,
        "feeds": int(s1["feeds"]) - int(s0["feeds"]),
        "rfb_updates": r1[0] - r0[0],
        "rfb_kbit_s": f"{(r1[1] - r0[1]) * 8 / span / 1000:.0f}",
        "rfb_rects": " ".join(f"{NAMES.get(k, k)}:{v}" for k, v in sorted(rects.items())),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("builds", nargs="+", help="LABEL=PATH to beanjar-vnc")
    parser.add_argument("--encoder", default="x264")
    parser.add_argument("--size", default="1280x720")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--source", choices=["animate", "dmabuf"], default="animate")
    parser.add_argument("--csv")
    parser.add_argument("--env", action="append", default=[], help="LABEL:NAME=VALUE")
    parser.add_argument("--logs", help="a directory to keep beanjar-vnc's logs in")
    parser.add_argument("--vnc-arg", action="append", default=[], help="passed to beanjar-vnc")
    parser.add_argument("--source-arg", action="append", default=[], help="passed to fake-qemu")
    parser.add_argument("--clients", choices=["video", "rfb", "video+rfb"], default="video")
    parser.add_argument("--rfb-encodings", choices=sorted(ENCODINGS), default="novnc")
    args = parser.parse_args()
    builds = [b.split("=", 1) for b in args.builds]
    envs = {label: dict(os.environ) for label, _b in builds}
    for e in args.env:
        label, kv = e.split(":", 1)
        name, value = kv.split("=", 1)
        envs[label][name] = value
    d = tempfile.mkdtemp(prefix="bench-vnc-")
    fake = subprocess.Popen(
        [sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), d, "--size", args.size,
         f"--{args.source}", *args.source_arg],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    print(f"bench-vnc: fake-qemu pid {fake.pid}", file=sys.stderr)
    out = open(args.csv, "w", newline="") if args.csv else sys.stdout
    writer = None
    rows = []
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(d, "ready")):
                break
            if fake.poll() is not None:
                sys.exit(f"bench-vnc: fake-qemu exited {fake.returncode}")
            time.sleep(0.05)
        open(os.path.join(d, "animate"), "w").close()
        for r in range(args.rounds):
            order = builds if r % 2 == 0 else builds[::-1]
            for label, binary in order:
                row = {"build": label, "round": r}
                row.update(
                    run_one(binary, envs[label], d, args.size, args.encoder, args.seconds,
                            os.path.join(d, f"vnc-{label}-{r}.log"), args.vnc_arg, args.clients,
                            args.rfb_encodings)
                )  # fmt: skip
                rows.append(row)
                if writer is None:
                    writer = csv.DictWriter(out, fieldnames=list(row))
                    writer.writeheader()
                writer.writerow(row)
                out.flush()
    finally:
        fake.send_signal(signal.SIGTERM)
        fake.wait(timeout=10)
        if args.logs:
            os.makedirs(args.logs, exist_ok=True)
            for name in os.listdir(d):
                if name.endswith(".log"):
                    shutil.move(os.path.join(d, name), os.path.join(args.logs, name))
        subprocess.run(["rm", "-rf", d])
        if args.csv:
            out.close()
    for label, _b in builds:
        mine = [x for x in rows if x["build"] == label and x["cpu_ms_per_frame"]]
        if not mine:
            continue
        med = {
            k: statistics.median(float(x[k]) for x in mine)
            for k in ("fps", "cpu_ms_per_frame", "main_ms_per_frame", "cpu_percent")
        }
        print(f"bench-vnc: {label} ({mine[0]['path']}, {args.encoder} {args.size}, "
              f"{len(mine)} runs): median {med['fps']:.2f} fps, {med['cpu_ms_per_frame']:.2f} ms "
              f"CPU a frame, main thread {med['main_ms_per_frame']:.2f} ms, "
              f"{med['cpu_percent']:.1f}% CPU", file=sys.stderr)  # fmt: skip


if __name__ == "__main__":
    main()
