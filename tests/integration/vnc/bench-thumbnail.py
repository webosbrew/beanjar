#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What a thumbnail costs beanjar-vnc (docs/dev/architecture/vnc.md, "Thumbnails"): fake-qemu
--animate at the desktop's size, Display1.Screenshot calls at 384x216 as beanjar-web's cards make.

    bench-thumbnail.py BEANJAR_VNC [--size WxH] [--seconds S] [--rate N]

For each case, after one call, it runs S seconds without calls and S seconds with N calls a second, and prints the
calls' time (median and largest), the counter thumbnail_us a thumbnail made (the GPU's draw and
read back, on the main thread), and beanjar-vnc's CPU and frames a second with and without them:

- cached: no client, the picture still (each call answered from the cache);
- idle: no client, the top quarter new noise every 33 ms (each call composes the scene first);
- client: an RFB client taking Tight (noVNC's list) of the changing picture (from the frames).

No thresholds: numbers for a record, not a test.
"""

import argparse
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import loopback  # noqa: E402
from gi.repository import Gio, GLib  # noqa: E402
from rfbclient import NOVNC, Rfb  # noqa: E402

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
TICK = os.sysconf("SC_CLK_TCK")
NAME, PATH = "org.beanjar.Display1", "/org/beanjar/Display1"


def cpu(pid):
    fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / TICK


def counters(vnc, log_path):
    seen = open(log_path).read().count("beanjar-vnc: stats ")
    vnc.send_signal(signal.SIGUSR1)
    for _ in range(200):
        lines = [x for x in open(log_path).read().splitlines() if "beanjar-vnc: stats " in x]
        if len(lines) > seen:
            return {k: int(v) for k, v in (f.split("=", 1) for f in lines[-1].split()[2:])}
        time.sleep(0.02)
    sys.exit("bench-thumbnail: no stats line")


def window(vnc, log, conn, out, seconds, rate):
    """CPU ms a second, frames a second, the calls' times (ms) and the counters' deltas."""
    c0, t0, start = counters(vnc, log), cpu(vnc.pid), time.monotonic()
    times = []
    while time.monotonic() - start < seconds:
        if rate:
            begin = time.monotonic()
            conn.call_sync(NAME, PATH, NAME, "Screenshot", GLib.Variant("(suu)", (out, 384, 216)),
                           None, Gio.DBusCallFlags.NONE, 20000, None)  # fmt: skip
            times.append((time.monotonic() - begin) * 1000)
            time.sleep(max(0.0, 1 / rate - (time.monotonic() - begin)))
        else:
            time.sleep(0.1)
    elapsed = time.monotonic() - start
    t1, c1 = cpu(vnc.pid), counters(vnc, log)
    delta = {
        k: c1[k] - c0[k] for k in ("renders", "thumbnails", "thumbnails_cached", "thumbnail_us")
    }
    return (t1 - t0) * 1000 / elapsed, delta["renders"] / elapsed, times, delta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vnc")
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--rate", type=float, default=5)
    a = ap.parse_args()
    d = tempfile.mkdtemp(prefix="bench-thumbnail-")
    fake = subprocess.Popen(
        [sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), d, "--size", a.size,
         "--animate"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    vnc = client = conn = None
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(d, "ready")):
                break
            time.sleep(0.05)
        address = f"unix:path={d}/dbus.sock"
        log = os.path.join(d, "vnc.log")
        vnc = subprocess.Popen(
            [a.vnc, address, "--listen", loopback.ADDRESS, "--port", str(loopback.port()),
             "--size", a.size, "--fps", "30", "--no-media", "--native-cursor",
             "--local", os.path.join(d, "rfb.sock")],
            stdout=open(log, "w"), stderr=subprocess.STDOUT,
        )  # fmt: skip
        for _ in range(100):
            if "beanjar-vnc: ready" in open(log).read():
                break
            time.sleep(0.1)
        conn = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        out = os.path.join(d, "thumb.ppm")
        animate = os.path.join(d, "animate")
        for case in ("cached", "idle", "client"):
            if case == "idle":
                open(animate, "w").close()
            if case == "client":
                client = Rfb(os.path.join(d, "rfb.sock"), NOVNC)
            time.sleep(2)
            conn.call_sync(NAME, PATH, NAME, "Screenshot", GLib.Variant("(suu)", (out, 384, 216)),
                           None, Gio.DBusCallFlags.NONE, 20000, None)  # fmt: skip
            base_cpu, base_fps, _, _ = window(vnc, log, conn, out, a.seconds, 0)
            with_cpu, with_fps, times, delta = window(vnc, log, conn, out, a.seconds, a.rate)
            made = delta["thumbnails"]
            per = delta["thumbnail_us"] / made / 1000 if made else 0
            calls = len(times)
            print(f"bench-thumbnail: {case} {a.size}: {calls} calls, median {statistics.median(times):.2f} ms "
                  f"(largest {max(times):.1f}); {made} made at {per:.2f} ms each, "
                  f"{delta['thumbnails_cached']} cached; CPU {base_cpu:.1f} -> {with_cpu:.1f} ms a "
                  f"second ({(with_cpu - base_cpu) / a.rate:.2f} ms a call); frames {base_fps:.1f} -> "
                  f"{with_fps:.1f} a second", flush=True)  # fmt: skip
    finally:
        if client:
            client.close()
        if conn:
            conn.close_sync(None)
        if vnc and vnc.poll() is None:
            vnc.send_signal(signal.SIGTERM)
            vnc.wait(timeout=10)
        fake.send_signal(signal.SIGTERM)
        fake.wait(timeout=10)
        subprocess.run(["rm", "-rf", d])


if __name__ == "__main__":
    main()
