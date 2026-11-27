#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A front end keeps its window across a guest restart (host/core/beanjar/runtime/lifecycle.py).

    check-restart.py VIEWER_HEADLESS

A guest restart ends QEMU, and the VM's unit starts a new one on the same private bus, with
run/restarting there in between. Against tests/fakes/fake-qemu.py --reuse-bus on a bus of this
script's own, viewer-headless (the core the GTK viewer and beanjar-vnc share):

  - with the marker: the first QEMU (a plain picture) ends, a second one (--bars) comes 2 s later;
    the viewer says it connected to the new QEMU, runs to its --seconds and its last screenshot
    is the second QEMU's bars;
  - without it: the QEMU ends and the viewer ends too, within a few seconds, as when a VM stops."""

import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def wait_file(path, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return False


def fake(run, *extra):
    q = subprocess.Popen(
        [
            sys.executable,
            os.path.join(HERE, "..", "..", "fakes", "fake-qemu.py"),
            run,
            "--size",
            "640x360",
            "--reuse-bus",
            *extra,
        ]
    )
    if not wait_file(os.path.join(run, "ready")):
        q.kill()
        raise SystemExit("FAIL: fake-qemu did not start")
    return q


def stop(q):
    q.terminate()
    q.wait(10)


def read_ppm(path):
    with open(path, "rb") as f:
        data = f.read()
    parts = data.split(maxsplit=4)
    w, h = int(parts[1]), int(parts[2])
    return w, h, parts[4][-w * h * 3 :]


def distinct_colours(path):
    """How many different colours the middle row of the screenshot has."""
    w, h, px = read_ppm(path)
    row = px[(h // 2) * w * 3 : (h // 2 + 1) * w * 3]
    return len({row[i : i + 3] for i in range(0, len(row), 3)})


def viewer_run(viewer, run, seconds, shot):
    return subprocess.Popen(
        [viewer, f"unix:path={os.path.join(run, 'dbus.sock')}", "--seconds", str(seconds),
         "--no-audio", "--no-media", "--no-cursor", "--size", "640x360",
         "--screenshot", shot],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=dict(os.environ, SDL_VIDEODRIVER="offscreen"),
    )  # fmt: skip


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    viewer = sys.argv[1]
    tmp = tempfile.mkdtemp(prefix="check-restart-")
    run = os.path.join(tmp, "run")
    os.makedirs(run)
    sock = os.path.join(run, "dbus.sock")
    bus = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--nopidfile", f"--address=unix:path={sock}"],
        stdout=subprocess.DEVNULL,
    )
    failed = []
    procs = []
    try:
        if not wait_file(sock):
            raise SystemExit("FAIL: no dbus-daemon")
        marker = os.path.join(run, "restarting")

        # a restart
        q = fake(run)
        procs.append(q)
        shot = os.path.join(tmp, "restart.ppm")
        v = viewer_run(viewer, run, 9, shot)
        procs.append(v)
        time.sleep(2)
        open(marker, "w").close()
        stop(q)
        time.sleep(2)
        q = fake(run, "--bars")
        procs.append(q)
        os.unlink(marker)
        out, _ = v.communicate(timeout=60)
        colours = distinct_colours(shot) if os.path.exists(shot) else 0
        print(f"restart: viewer exit {v.returncode}, middle row colours {colours}")
        if v.returncode or "display: connected to the new QEMU" not in out or colours < 6:
            failed.append(f"restart: exit {v.returncode}, colours {colours}: {out[-2000:]}")
        stop(q)

        # a stop
        q = fake(run)
        procs.append(q)
        v = viewer_run(viewer, run, 30, os.path.join(tmp, "stop.ppm"))
        procs.append(v)
        time.sleep(2)
        stop(q)
        t = time.monotonic()
        out, _ = v.communicate(timeout=20)
        took = time.monotonic() - t
        print(f"stop: the viewer ended {took:.1f} s after its QEMU")
        if took > 5 or "connected to the new QEMU" in out:
            failed.append(f"stop: ended after {took:.1f} s: {out[-2000:]}")
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        bus.terminate()
        bus.wait()
        if os.environ.get("KEEP"):
            print("kept", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    for f in failed:
        print("FAIL: " + f)
    print("PASS" if not failed else "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
