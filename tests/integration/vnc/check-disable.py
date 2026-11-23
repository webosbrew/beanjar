#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""QEMU's display Disable in beanjar-vnc's picture (host/viewer/core/disable.h), against fake-qemu
(the blue picture with a white box in the middle), no VM, no window.

    check-disable.py BEANJAR_VNC

An RFB client on the --local socket notes the corner's colour in every update it gets:

1. A stray Disable, the Scanout again 20 ms later (QEMU's dmabuf release between two flips): no
   update is black. Before the core took Disable as provisional, the picture composed in between was.
2. A Disable alone (the guest's output off): the picture goes black, no sooner than the core's wait
   (DISABLE_WAIT_US, 50 ms) after it was sent, and within a second.
3. The Scanout again: the picture is back.
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import loopback  # noqa: E402
from rfbclient import Rfb  # noqa: E402

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
VNC = sys.argv[1]
W, H = 640, 360
RAW = [0, -313, -224]  # Raw, continuous updates, LastRect
WAIT = 0.050  # DISABLE_WAIT_US
BLUE = (32, 128, 192)
failures = []


def check(ok, what):
    print(f"{'pass' if ok else 'FAIL'}: {what}")
    if not ok:
        failures.append(what)


def wait(pred, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def near(p, q, tol=8):
    return all(abs(a - b) <= tol for a, b in zip(p, q))


def corner_of(fb):
    o = (20 * W + 20) * 4
    return fb[o + 2], fb[o + 1], fb[o]


def sent(d, n):
    """the monotonic time the fake sent its n-th Disable (input.log), or None"""
    lines = [x for x in open(os.path.join(d, "input.log")) if x.startswith("Listener.Disable sent")]
    return float(lines[n].split()[2]) if len(lines) > n else None


def main():
    d = tempfile.mkdtemp(prefix="check-vnc-disable-")
    fake = subprocess.Popen(
        [sys.executable, os.path.join(TOP, "tests/fakes/fake-qemu.py"), d, "--size", f"{W}x{H}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    vnc = None
    client = None
    seen = []  # (monotonic time, corner colour) of every update

    def note(c):
        seen.append((time.monotonic(), corner_of(c.fb)))

    def control(command):
        with open(os.path.join(d, "display-control.new"), "w") as f:
            f.write(command)
        os.rename(os.path.join(d, "display-control.new"), os.path.join(d, "display-control"))

    try:
        if not wait(lambda: os.path.exists(os.path.join(d, "ready")), 10):
            sys.exit("check-vnc-disable: fake-qemu did not start")
        local = os.path.join(d, "rfb.sock")
        log = open(os.path.join(d, "vnc.log"), "w")
        vnc = subprocess.Popen(
            [VNC, f"unix:path={d}/dbus.sock", "--listen", loopback.ADDRESS, "--port",
             str(loopback.port()), "--size", f"{W}x{H}", "--fps", "60", "--no-media",
             "--local", local],
            stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        if not wait(lambda: "beanjar-vnc: ready" in open(log.name).read(), 10):
            sys.exit(f"check-vnc-disable: beanjar-vnc not ready: {open(log.name).read()}")
        client = Rfb(local, RAW, framebuffer=True)
        client.on_update = note
        check(wait(lambda: seen and near(seen[-1][1], BLUE)), "the picture starts blue")

        # 1. stray
        start = len(seen)
        control("stray 20")
        check(wait(lambda: sent(d, 0) is not None), "the fake sent the stray Disable")
        time.sleep(0.5)
        black = [c for _, c in seen[start:] if near(c, (0, 0, 0))]
        check(not black, f"stray Disable: no black update ({len(black)} of {len(seen) - start})")
        check(near(seen[-1][1], BLUE), f"stray Disable: the picture kept: {seen[-1][1]}")

        # 2. real
        start = len(seen)
        control("disable")
        check(wait(lambda: sent(d, 1) is not None), "the fake sent the Disable")
        check(wait(lambda: any(near(c, (0, 0, 0)) for _, c in seen[start:]), 2),
              "real Disable: the picture goes black")  # fmt: skip
        at = next((t for t, c in seen[start:] if near(c, (0, 0, 0))), None)
        if at is not None:
            after = at - sent(d, 1)
            check(WAIT - 0.005 <= after <= 1.0,
                  f"real Disable: black {after * 1000:.0f} ms after it was sent (wait {WAIT * 1000:.0f} ms)")  # fmt: skip

        # 3. back
        control("scanout")
        check(wait(lambda: near(seen[-1][1], BLUE), 2), "the Scanout again: the picture back")
    finally:
        if client:
            client.close()
        if vnc:
            vnc.terminate()
            vnc.wait(10)
        fake.terminate()
        fake.wait(10)
    if failures:
        print(f"FAIL: {len(failures)} check(s)")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
