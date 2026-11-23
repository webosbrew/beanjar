#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The virtual USB camera in a running VM, pass or fail with numbers.

  run-camera.py NAME|HOST/NAME [--seconds 12] [--format JPEG|YUV] [--width 1280 --height 720]
                [--fps 30] [--pattern smpte] [--keep]

Plugs the camera in (beanjar io usb attach NAME --camera: the camera runs on this machine, also for a
VM on another host), runs tests/regress/virtcam/camera.sh in the guest as root (beanjar ares-shell --root:
the VM needs the rootssh layer) and checks what it saw: the device in sysfs at high speed with
uvcvideo bound and V4L2 nodes, PDM listing it as a camera, LG's camera service listing it and
calling it supported, and a preview through the service (open, setFormat, startPreview) for
--seconds at the service's own frame rate (its log's fps lines; the first, which covers the start,
is left out) of at least 80% of the rate asked for, or of what the camera can do at that size (the
C920 lists 1280x720 YUY2 at 10 fps at most). The rate is counted in the preview's shared memory by
tests/regress/virtcam/shm-frames.c, built here with the webOS SDK (WEBOS_CC) and copied in with ares-push; without the SDK
the service's own fps log lines are taken, which it writes only now and then. The camera's own
counters (frames sent, late wake-ups, skipped and dropped packets over the preview) come from its
stderr. With --keep the camera stays plugged in until Ctrl-C (for the camera app); else it is
unplugged. Prints the numbers and PASS or FAIL; the exit status is 0 only for PASS.

  run-camera.py NAME|HOST/NAME --app [APPID] [--seconds 40] [--min-fps 15] [--keep]

checks LG's camera app on its default path instead: tests/regress/virtcam/camera-app.sh removes LG's
raw-format override, so the app asks for JPEG, which LG's camera player decodes with appsrc !
omxmjpegdec ! appsink; the stand-in decoder sends each frame to the media helper, which decodes it,
and pushes the picture to the appsink. It checks that the decoder found the appsink and chose a
format, that the host answered, that at least 95% of the decoder's inputs went downstream as
pictures, at no less than --min-fps over the steady part (the rate lines after the first), and that
WebAppMgr kept its pid. --min-fps is not the camera's rate: on a TCG guest QEMU loses whole frames
on the way in, which the preview mode measures."""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CAMERA_APP = "com.webos.app.camera"
RATE = re.compile(
    r"(\S+): host decoding: (\d+) pictures pushed of (\d+) inputs in ([\d.]+) s .*?"
    r"([\d.]+) ms per input \(([\d.]+) sending, ([\d.]+) reads\), (\d+) unanswered, flow (\S+)"
)
BEANJAR = os.environ.get("BEANJAR", "beanjar")
VID, PID = "046d", "082d"
STATS = re.compile(
    r"last ([\d.]+) s: frames (\d+) \(([\d.]+)/s\), packets (\d+), late wake-ups (\d+), "
    r"skipped (\d+), dropped (\d+)"
)


def env():
    e = dict(os.environ)
    e["PATH"] = os.path.expanduser("~/.cargo/bin") + os.pathsep + e.get("PATH", "")
    return e


SDK_CC = "/opt/arm-webos-linux-gnueabi_sdk-buildroot/bin/arm-webos-linux-gnueabi-gcc"
GUEST_TOOL = "/tmp/beanjar-shm-frames"
# camera-app.sh's pid file, and stopping a run of it left behind (with its log follower)
APP_STOP = (
    'p=$(cat /tmp/beanjar-camera-app.pid 2>/dev/null) && [ -n "$p" ] && '
    '{ pkill -P "$p"; kill "$p"; }; rm -f /tmp/beanjar-camera-app.pid; true'
)


def shm_tool(out):
    """shm-frames.c built for the guest with the webOS SDK (ARM, the guest's glibc) as file out;
    False without the SDK (then the service's fps lines only)."""
    cc = os.environ.get("WEBOS_CC", SDK_CC)
    if not os.access(cc, os.X_OK):
        return False
    subprocess.run([cc, "-O2", "-s", "-o", out, os.path.join(HERE, "shm-frames.c")], check=True)
    return True


def parse(text):
    """KEY=VALUE lines before '--- raw': {key: [values]}."""
    out = {}
    for line in text.split("--- raw")[0].splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(k.strip(), []).append(v.strip())
    return out


def one(d, key):
    return (d.get(key) or [""])[0]


def app_checks(a, nums):
    """The camera app on the default path: camera-app.sh's lines, checked."""
    script = open(os.path.join(HERE, "camera-app.sh")).read()
    command = f"set -- {a.app} {a.seconds} {1 if a.keep else 0}\n" + script
    try:
        r = subprocess.run(
            [BEANJAR, "ares-shell", "--root", "-d", a.name, "-r", command],
            capture_output=True, text=True, timeout=a.seconds + 120, env=env(),
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        # the script runs on in the guest and would close the next run's app: stop it (its pid
        # file; the next run's script stops a leftover one too)
        subprocess.run(
            [BEANJAR, "ares-shell", "--root", "-d", a.name, "-r", APP_STOP],
            capture_output=True, timeout=60, env=env(), stdin=subprocess.DEVNULL,
        )  # fmt: skip
        raise
    out = r.stdout
    print(out.rstrip())
    d = parse(out)
    logs = d.get("log", [])
    rates = [m.groups() for m in map(RATE.search, logs) if m]
    steady = rates[1:]
    pictures = sum(int(x[1]) for x in steady)
    inputs = sum(int(x[2]) for x in steady)
    seconds = sum(float(x[3]) for x in steady)
    fps = pictures / seconds if seconds else 0.0
    per_input = [float(x[4]) for x in steady]
    sending = [float(x[5]) for x in steady]
    reads = [float(x[6]) for x in steady]
    unanswered = int(rates[-1][7]) if rates else 0
    fmt = next((x for x in logs if "pictures as" in x), "")
    nums.update(
        app_seconds=a.seconds, rate_lines=len(rates), steady_seconds=round(seconds, 1),
        pictures=pictures, inputs=inputs, picture_fps=round(fps, 2),
        ms_per_input=round(sum(per_input) / len(per_input), 1) if per_input else None,
        ms_sending=round(sum(sending) / len(sending), 1) if sending else None,
        reads_per_input=round(sum(reads) / len(reads)) if reads else None,
        unanswered=unanswered, override_removed=one(d, "override_removed") == "1",
    )  # fmt: skip
    wam = (one(d, "wam_before"), one(d, "wam_after"))
    return [
        ("camera service lists the camera", bool(one(d, "camera_id")), one(d, "camera_id")),
        ("camera access granted", '"returnValue":true' in one(d, "permission").replace(" ", ""),
         one(d, "permission")[:120]),
        ("app launched", '"returnValue":true' in one(d, "launch").replace(" ", ""),
         one(d, "launch")[:120]),
        ("the decoder found a consumer that needs pixels", any("reaches" in x for x in logs),
         next((x for x in logs if "reaches" in x), "no line")[-120:]),
        ("a picture format chosen", bool(fmt), fmt[-160:]),
        ("the host answered", bool(steady) and unanswered == 0,
         f"{len(rates)} rate lines, {unanswered} inputs unanswered"),
        ("95% of inputs pushed as pictures", inputs > 0 and pictures >= 0.95 * inputs,
         f"{pictures} of {inputs} in {seconds:.1f} s"),
        (f"pictures >= {a.min_fps:g}/s", fps >= a.min_fps, f"{fps:.2f}/s"),
        ("WebAppMgr did not restart", bool(wam[0]) and wam[0] == wam[1], f"{wam[0]} -> {wam[1]}"),
        ("no new WebAppMgr crash report", one(d, "crashes_before") == one(d, "crashes_after"),
         f"{one(d, 'crashes_before')} -> {one(d, 'crashes_after')}"),
    ]  # fmt: skip


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--seconds", type=int, default=12)
    ap.add_argument("--format", default="JPEG", choices=["JPEG", "YUV"])
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--pattern", default="smpte")
    ap.add_argument("--keep", action="store_true", help="leave the camera plugged in until Ctrl-C")
    ap.add_argument("--app", nargs="?", const=CAMERA_APP, help="LG's camera app")
    ap.add_argument("--min-fps", type=float, default=15.0)
    a = ap.parse_args()
    if a.app and a.seconds == 12:
        a.seconds = 40

    errlog = tempfile.TemporaryFile(mode="w+")
    plug = subprocess.Popen(
        [BEANJAR, "io", "usb", "attach", a.name, "--camera", "--pattern", a.pattern, "--json"],
        stdout=subprocess.PIPE,
        stderr=errlog,
        text=True,
        env=env(),
        start_new_session=True,
    )
    got = {}

    def first_line():
        line = plug.stdout.readline()
        if line.strip():
            got.update(json.loads(line))

    t = threading.Thread(target=first_line, daemon=True)
    t.start()
    t.join(60)
    checks = []
    nums = {}
    try:
        if not got:
            errlog.seek(0)
            print(errlog.read()[-2000:], file=sys.stderr)
            checks.append(("plugged in", False, "no answer from beanjar io usb attach"))
            raise SystemExit
        print(f"plugged in: {got}")
        if a.app:
            checks += app_checks(a, nums)
            raise SystemExit
        script = open(os.path.join(HERE, "camera.sh")).read()
        args = [VID, PID, a.seconds, a.width, a.height, a.format, a.fps]
        # the tool is copied in with ares-push: a command line is limited (the guest's SSH server
        # drops one of 9000 bytes or more)
        with tempfile.TemporaryDirectory() as d:
            tool = shm_tool(os.path.join(d, "shm-frames"))
            if tool:
                subprocess.run(
                    [BEANJAR, "ares-push", "--root", "-d", a.name, os.path.join(d, "shm-frames"),
                     GUEST_TOOL],
                    capture_output=True, timeout=120, env=env(), stdin=subprocess.DEVNULL,
                    check=True,
                )  # fmt: skip
                subprocess.run(
                    [BEANJAR, "ares-shell", "--root", "-d", a.name, "-r", f"chmod 755 {GUEST_TOOL}"],
                    capture_output=True, timeout=60, env=env(), stdin=subprocess.DEVNULL,
                    check=True,
                )  # fmt: skip
        command = (
            (f"export SHM_FRAMES={GUEST_TOOL}\n" if tool else "")
            + "set -- " + " ".join(str(x) for x in args) + "\n" + script
        )  # fmt: skip
        before = time.monotonic()
        r = subprocess.run(
            [BEANJAR, "ares-shell", "--root", "-d", a.name, "-r", command],
            capture_output=True,
            text=True,
            timeout=a.seconds + 180,
            env=env(),
            stdin=subprocess.DEVNULL,
        )
        out = r.stdout
        if not out.strip():
            print(f"camera.sh: no output (status {r.returncode}): {r.stderr.strip()[-500:]}")
        print(out.split("--- raw")[0].rstrip())
        d = parse(out)
        drivers = d.get("driver", [])
        fps = [float(x) for x in one(d, "service_fps").split()]
        steady = fps[1:] if len(fps) > 1 else fps
        mean = sum(steady) / len(steady) if steady else 0.0
        # the rate the camera offers at this size: the C920's YUY2 1280x720 is 10 fps at most
        want = a.fps if a.format == "JPEG" or a.width * a.height <= 640 * 480 else 10
        seconds = float(one(d, "preview_seconds") or 0)
        shm_fps = float(one(d, "shm_fps") or 0)
        shm_frames = int(one(d, "shm_frames") or 0)
        shm_jpeg = int(one(d, "shm_jpeg") or 0)
        # the rate: the frames in the preview's shared memory when counted, else the service's log
        rate, source = (shm_fps, "shared memory") if "shm_fps" in d else (mean, "service log")
        checks += [
            ("device in sysfs", bool(one(d, "usb_device")), one(d, "usb_device")),
            ("high speed", one(d, "usb_speed") == "480", one(d, "usb_speed")),
            ("uvcvideo bound", any(x.endswith(" uvcvideo") for x in drivers), "; ".join(drivers)),
            ("V4L2 nodes", bool(one(d, "video_nodes")), one(d, "video_nodes")),
            ("PDM lists a camera", int(one(d, "pdm_cam") or 0) > 0, one(d, "pdm_cam")),
            ("camera service lists it", bool(one(d, "camera_id")), one(d, "camera_id")),
            ("camera service: supported", one(d, "supported") == "1", one(d, "name")),
            ("open", bool(one(d, "handle")), one(d, "handle")),
            ("setFormat", one(d, "set_format") == "1", f"{a.format} {a.width}x{a.height}@{a.fps}"),
            ("startPreview", bool(one(d, "preview_key")), one(d, "preview_key")),
            ("preview for 10 s or more", seconds >= 10, f"{seconds:.1f} s"),
            (
                f"frame rate >= 80% of {want} ({source})",
                rate >= 0.8 * want,
                f"{shm_frames} frames, {shm_fps:.2f} fps in shared memory; service log "
                f"{' '.join(map(str, fps)) or 'none'}",
            ),
        ]
        if a.format == "JPEG" and "shm_jpeg" in d:
            checks.append(
                ("frames are whole JPEGs", shm_jpeg > 0 and one(d, "shm_bad") == "0",
                 f"{shm_jpeg} checked, {one(d, 'shm_bad')} bad, last {one(d, 'shm_last_length')} B")
            )  # fmt: skip
        nums.update(
            preview_seconds=seconds, service_fps=fps, service_fps_mean=round(mean, 2),
            shm_frames=shm_frames, shm_fps=shm_fps,
            guest_seconds=round(time.monotonic() - before, 1),
        )  # fmt: skip
    except SystemExit:
        pass
    finally:
        if a.keep and got:
            print("camera plugged in; Ctrl-C unplugs it", flush=True)
            try:
                plug.wait()
            except KeyboardInterrupt:
                pass
        if plug.poll() is None:
            os.killpg(plug.pid, signal.SIGTERM)
        try:
            plug.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(plug.pid, signal.SIGKILL)
            plug.wait()
        errlog.seek(0)
        camera = errlog.read()
    # the camera's counters while the preview ran: its 10 s lines with frames in them
    windows = [tuple(float(x) for x in m.groups()) for m in STATS.finditer(camera)]
    streaming = [w for w in windows if w[1] > 0]
    if streaming:
        span = sum(w[0] for w in streaming)
        nums.update(
            camera_seconds=round(span, 1),
            camera_frames=int(sum(w[1] for w in streaming)),
            camera_fps=round(sum(w[1] for w in streaming) / span, 2) if span else 0,
            camera_packets=int(sum(w[3] for w in streaming)),
            camera_late_wakeups=int(sum(w[4] for w in streaming)),
            camera_skipped=int(sum(w[5] for w in streaming)),
            camera_dropped=int(sum(w[6] for w in streaming)),
        )
    for line in camera.splitlines():
        if "ended after" in line:
            print(line)
    ok = bool(checks) and all(c[1] for c in checks)
    for name, passed, detail in checks:
        print(f"{'ok  ' if passed else 'FAIL'} {name}: {detail}")
    print("numbers: " + json.dumps(nums))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
