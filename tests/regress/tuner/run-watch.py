#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Live TV's picture and sound on a running VM with our tuner, pass or fail with numbers.

  run-watch.py NAME [--seconds S] [--shot FILE]

After a scan (run-scan.py), with a channel mapped: closes and launches Live TV, which tunes the
channel it watched last, waits until the video decoder (dtv_soc's /dev/video20) streams, and measures for S
seconds (default 20): the pictures the guest's decoder read the headers of
(/proc/dtv_soc), the pictures and audio frames the host's media decoder decoded of the tuner's
sessions (the VM's run/media/media-sessions.log names them, run/media/media-helper.log counts them), and
whether the TV picture is a picture and not black or the "No Signal" screen (a screenshot with
light in most of it). Prints the numbers as JSON and PASS or FAIL; the exit status is 0 only for
PASS.

NAME may be HOST/NAME, a VM on another host: everything goes through beanjar (ares-shell, log show,
screenshot), so that host needs only its beanjar; the screenshot comes back here and its light is
measured here."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

BEANJAR = os.environ.get("BEANJAR", "beanjar")
LIVETV = "com.webos.app.livetv"
AM = "luna-send -n 1 -w 30000 luna://com.webos.service.applicationmanager/"
MIN_FPS = 10  # the test channels are 29.97 frames a second; TCG decodes headers in time


def env():
    e = dict(os.environ)
    e["PATH"] = os.path.expanduser("~/.cargo/bin") + os.pathsep + e.get("PATH", "")
    e.setdefault("QT_QPA_PLATFORM", "offscreen")
    return e


def shell(name, command, timeout=60):
    r = subprocess.run(
        [BEANJAR, "ares-shell", "--root", "-d", name, "-r", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env(),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return r.stdout


def foreground(name):
    """The app in front, "" for none."""
    m = re.search(
        r'"appId":"([^"]*)"',
        shell(
            name,
            "luna-send -n 1 -w 10000 "
            "luna://com.webos.service.applicationmanager/getForegroundAppInfo '{}' | tr -d ' '",
        ),
    )
    return m.group(1) if m else ""


def vdec(name):
    """The streaming video decoder of /proc/dtv_soc: (pictures, 'WxH'), or None."""
    m = re.search(
        r"vdec channel \d+ streaming .*? pictures (\d+) .*? picinfo (\d+x\d+)",
        shell(name, "cat /proc/dtv_soc"),
    )
    return (int(m.group(1)), m.group(2)) if m else None


def log(name, file):
    """The text of the VM's host-side log file (run/...), on whichever host it runs; "" for none."""
    r = subprocess.run(
        [BEANJAR, "log", "show", name, file, "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env(),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if r.returncode:
        return ""
    try:
        return json.loads(r.stdout)["text"]
    except (ValueError, KeyError, TypeError):
        return ""


def sessions(text):
    """{kind: media port} of the tuner's last sessions, from media-sessions.log's text."""
    out = {}
    for m in re.finditer(r"media (\d+): open (video|audio) tuner-", text):
        out[m.group(2)] = int(m.group(1))
    return out


def decoded(text, kind, port):
    """The last count of port's decoder of kind in media-helper.log's text."""
    n = 0
    for m in re.finditer(rf"decode {port} {kind} session=\d+[^\n]*? decoded=(\d+)", text):
        n = int(m.group(1))
    return n


def helper_counts(name, s):
    """(video, audio) decoded so far by the media helper of the sessions s."""
    text = log(name, "run/media/media-helper.log")
    return decoded(text, "video", s.get("video")), decoded(text, "audio", s.get("audio"))


def lit(path):
    """The share of the picture's pixels that are not dark."""
    from PIL import Image

    im = Image.open(path).convert("L").resize((160, 90))
    px = list(im.getdata())
    return sum(1 for p in px if p > 48) / len(px)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name", help="the VM: NAME, or HOST/NAME for another host's")
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--shot", help="keep the screenshot here")
    a = ap.parse_args()
    # a just-booted set brings Home to the front once it is up, over a Live TV launched before
    for _ in range(60):
        if foreground(a.name):
            break
        time.sleep(5)
    # a fresh Live TV: one left in front by run-scan.py's scan.sh, whose own watch it ended, does
    # not tune again when launched
    shell(
        a.name,
        AM
        + f'closeByAppId \'{{"id":"{LIVETV}"}}\'; sleep 5; '
        + AM
        + f'launch \'{{"id":"{LIVETV}"}}\'',
        120,
    )
    t0 = time.monotonic()
    while not vdec(a.name):
        if time.monotonic() - t0 > 180:
            print(json.dumps({"start_s": None}))
            print("FAIL: the video decoder did not start in 180 s")
            return 1
        time.sleep(3)
    start_s = round(time.monotonic() - t0)
    time.sleep(5)  # past the first sequence header and the host's first pictures
    s = sessions(log(a.name, "run/media/media-sessions.log"))
    v0, a0 = helper_counts(a.name, s)
    p0, t1 = vdec(a.name), time.monotonic()
    time.sleep(a.seconds)
    p1, t2 = vdec(a.name), time.monotonic()
    v1, a1 = helper_counts(a.name, s)
    span = t2 - t1
    if a.shot:
        shot = a.shot
    else:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            shot = f.name
    # Home may come over Live TV once after a boot, even after the watch started: bring Live TV
    # back to the front (a launch, not a close) and look again, for up to a minute
    t3 = time.monotonic()
    while True:
        subprocess.run(
            [BEANJAR, "screenshot", a.name, shot], capture_output=True, env=env(), check=True
        )
        light = lit(shot)
        if light >= 0.5 or time.monotonic() - t3 > 60:
            break
        if foreground(a.name) != LIVETV:
            shell(a.name, AM + f'launch \'{{"id":"{LIVETV}"}}\'')
        time.sleep(5)
    if not a.shot:
        os.unlink(shot)
    numbers = {
        "start_s": start_s,
        "picinfo": p1[1] if p1 else None,
        "guest_pictures_per_s": round(((p1 or (0,))[0] - (p0 or (0,))[0]) / span, 1),
        "sessions": s,
        "host_video_decoded_per_s": round((v1 - v0) / span, 1),
        "host_audio_decoded_per_s": round((a1 - a0) / span, 1),
        "lit": round(light, 2),
    }
    print(json.dumps(numbers))
    fails = []
    if numbers["guest_pictures_per_s"] < MIN_FPS:
        fails.append("the guest's decoder read too few pictures")
    if numbers["host_video_decoded_per_s"] < MIN_FPS:
        fails.append("the host decoded too few pictures")
    if numbers["host_audio_decoded_per_s"] <= 0:
        fails.append("the host decoded no sound")
    if light < 0.5:
        fails.append("the picture is dark")
    if fails:
        print("FAIL: " + "; ".join(fails))
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
