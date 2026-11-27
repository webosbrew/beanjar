#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run on the VM host with Normal/gl booted and the SDL viewer built, no other viewer attached.

    python3 tests/regress/input/check-viewer.py [RUN_DIR [LOG_DIR]]    (default build build)

Exercises SDL replay -> D-Bus -> virtio tablet/keyboard -> LG evdev -> Qt app, and the remote's
buttons: replay -> ir.sock -> webos.ir -> micom -> tvservice's "LGE RCU" -> Qt app.
Puts its guest scripts into the VM's share (`host/scripts/vm-launcher put`); the guest writes the event captures
to RUN_DIR/share/out. Leaves a rendered PPM in build/. The test client stays alive until the VM
stops, or start-viewer-probe.sh is run again.
"""

from pathlib import Path
import re
import struct
import subprocess
import os
import sys
import time

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar.guestside import share

BUILD = TOP / "build"
RUN = Path(sys.argv[1]) if len(sys.argv) > 1 else BUILD
LOGS = Path(sys.argv[2]) if len(sys.argv) > 2 else RUN
OUT = RUN / "share/out"  # the guest's /host/out (beanjar/guestside/share.py)
ready = OUT / "viewer-probe.ready"
ready.unlink(missing_ok=True)
for kind in ("pointer", "keyboard", "remote"):
    (OUT / f"viewer-{kind}.bin").unlink(missing_ok=True)
subprocess.run(
    [
        str(TOP / "host/scripts/vm-launcher"),
        "put",
        "tests/regress/input/start-viewer-probe.sh",
        "tests/regress/input/run-pointer-probe.sh",
        "tests/regress/input/pointer.qml",
    ],
    check=True,
    cwd=TOP,
    stdout=subprocess.DEVNULL,
    env={**os.environ, "RUN_DIR": str(RUN)},
)


def console(command):
    subprocess.run(
        [str(TOP / "host/scripts/vm-launcher"), "console", command, "1"],
        check=True,
        env={**os.environ, "SOCK": str(RUN / "console.sock"), "LOG": str(LOGS / "console.log")},
    )


def await_file(path, timeout=15):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise RuntimeError(f"guest did not produce {path.name}")
        time.sleep(0.1)


console("sh /host/tests/regress/input/start-viewer-probe.sh")
await_file(ready)
time.sleep(2)
result = subprocess.run(
    [
        str(BUILD / "viewer/beanjar-viewer"),
        f"unix:path={RUN}/dbus.sock",
        "--headless",
        "--size",
        "1280x720",
        "--seconds",
        "7",
        "--replay",
        str(TOP / "tests/regress/input/letterbox.replay"),
        "--screenshot",
        str(BUILD / "viewer-pointer.ppm"),
    ],
    check=True,
    capture_output=True,
    text=True,
)
print(result.stderr, end="")
cursor = re.search(r"cursor_packets=(\d+) cursor_shown=(\d+) cursor_hidden=(\d+)", result.stderr)
assert cursor and all(int(v) > 0 for v in cursor.groups()), result.stderr
images = re.search(r"cursor_images=(\d+)", result.stderr)
assert images and int(images[1]) > 1, "missing libim animation frames"
# cat writes events as they arrive; the viewer has awaited every D-Bus reply.
time.sleep(0.5)


def guest_file(name):
    """A file the guest wrote to its share: a regular one only, read without following a link it
    made there (share.read); anything else is an error, not a hang or another file's bytes."""
    try:
        return share.read(str(OUT), name)
    except share.Error as e:
        sys.exit(f"guest output: {e}")


def events(kind):
    data = guest_file(f"viewer-{kind}.bin")
    return [e[2:] for e in struct.iter_unpack("<qqHHi", data)]


pointer = events("pointer")
keys = events("keyboard")
remote = events("remote")
buttons = [e for e in pointer if e[0] == 1 and 272 <= e[1] <= 274]
assert buttons == [(1, 272, 1), (1, 272, 0)] * 3, buttons
# W21A's lginput2 sends cursor-on (0x4ae) as a press/release pulse.
# It enables native M-RCU pointer visibility before the coordinate packet.
wakeup = [e for e in pointer if e[:2] == (1, 0x4AE)]
if wakeup:
    assert wakeup == [(1, 0x4AE, 1), (1, 0x4AE, 0)] * 3, wakeup
# REL_WHEEL (2, 8): +1, -1, then two +1 notches (the viewer sends one per step), so direction and
# count both survive SDL -> D-Bus -> virtio-tablet -> the M-RCU bridge.
assert [e for e in pointer if e[0] == 2] == [(2, 8, 1), (2, 8, -1), (2, 8, 1), (2, 8, 1)], pointer
for axis, expected in ((0, [400, 600, 1400]), (1, [300, 440, 800])):
    values = [e[2] for e in pointer if e[:2] == (3, axis)]
    assert len(values) == len(expected) and all(
        abs(a - b) <= 1 for a, b in zip(values, expected)
    ), values
assert [e for e in keys if e[0] == 1] == [(1, 30, 1), (1, 30, 0)], keys
# the remote's RED, GREEN, YELLOW, BLUE (KEY_RED..KEY_BLUE, 398-401) and BACK (KEY_PREVIOUS 412),
# injected by tvservice from the micom's reports
assert [e for e in remote if e[0] == 1] == [
    (1, c, v) for c in (398, 399, 400, 401, 412) for v in (1, 0)
], remote
# Check black bars and image dimensions without a Pillow dependency.
with (BUILD / "viewer-pointer.ppm").open("rb") as f:
    assert f.readline() == b"P6\n"
    assert f.readline() == b"960 720\n"
    assert f.readline() == b"255\n"
    pixels = f.read()
assert len(pixels) == 960 * 720 * 3
assert not any(pixels[: 90 * 960 * 3]) and not any(pixels[630 * 960 * 3 :]), "letterbox not black"
assert any(pixels[90 * 960 * 3 : 630 * 960 * 3]), "empty display"
log = OUT / "viewer-app-events.log"
log.unlink(missing_ok=True)
console(
    "webos-enter tail -n +$(($(cat /run/viewer-log-lines) + 1)) /var/log/messages | grep POINTER_ >/host/out/viewer-app-events.log"
)
await_file(log)
# Wait for console redirection and grep to finish writing.
time.sleep(1)
text = guest_file(log.name).decode(errors="replace")
assert text.count("POINTER_CLICK") == 3, text
assert "POINTER_MOVE" in text, text
wheel = [int(v) for v in re.findall(r"POINTER_WHEEL (-?\d+)", text)]
assert wheel == [120, -120, 120, 120] and sum(wheel) == 240, wheel
assert "POINTER_KEY_DOWN 65" in text and "POINTER_KEY_UP 65" in text, text
# Qt::Key_webOS_Red..Blue 0x01200011..14. BACK (Qt::Key_webOS_Back) is the system's: over this bare
# qml-runner probe the compositor brings up the launcher instead of delivering it.
for qt in (0x01200011, 0x01200012, 0x01200013, 0x01200014):
    assert f"POINTER_KEY_DOWN {qt}" in text, (hex(qt), text)
print(
    "PASS: scaled/letterboxed viewer, pointer coordinates, click, wheel, focus release, keyboard, the remote's"
    " IR buttons through the micom, Qt delivery and libim cursor animation/visibility"
)
