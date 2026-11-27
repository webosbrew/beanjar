#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check the TV remote's path on a running VM (run on the QEMU host, no viewer attached):
ir.sock -> webos.ir -> the micom model (guest/layers/soc/h13) -> tvservice's MICOM_Task -> its own
uinput device "LGE RCU".

    python3 tests/regress/input/check-ir.py [--keys [service]] [RUN_DIR [LOG_DIR]]
        (default build build; a library VM: ~/.local/share/beanjar/vms/<vm>/run and .../logs)

The guest must have its output share (/host/out) mounted and tvservice running. Two buttons, LG's IR codes (custom
0x04): HOME tapped (0x7C, tvservice's KEY 0x305: press, release) and UP held for 1.3 s (0x40,
KEY_UP 103: press, autorepeat events at the micom's repeat rate after its first-repeat delay,
release). HOME opens the launcher and UP moves its focus. --keys instead taps every key of the
viewer's normal remote but POWER (beanjar_ui/viewer/remote.py: the codes the remote window sends) and
checks the Linux key tvservice injects for each (its _gKeyTable); the
firmware acts on each (menus open, channels change, the volume goes up and down again).
--keys service does the same with the service remote, except POWER, P-ONLY and IN STOP (power
only mode and the factory in-stop change the set's factory state). ir.sock takes one client:
stop the viewer first.
"""

import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar import share

SWEEP = None
args = sys.argv[1:]
if args[:1] == ["--keys"]:
    SWEEP, args = ("service", args[2:]) if args[1:2] == ["service"] else ("normal", args[1:])
RUN = Path(args[0]) if args else TOP / "build"
LOGS = Path(args[1]) if len(args) > 1 else RUN
OUT = str(RUN / "share/out")  # the guest's /host/out (beanjar/share.py)
capture = RUN / "share/out/ir-input-test.bin"
capture.unlink(missing_ok=True)


def captured():
    """The guest's capture: a regular file of its share, read without following a link it made
    there (share.read); a FIFO or a link is an error, not a hang or another file's bytes."""
    try:
        return share.read(OUT, capture.name)
    except share.Error as e:
        sys.exit(f"guest capture: {e}")


KEY_UP, KEY_HOME_LG = 103, 0x305
# LG IR data code -> the Linux key tvservice injects (its _gKeyTable), normal remote keys
LINUX = {
    0x08: 116,
    0x0B: 241,
    0x39: 0x31F,
    0xAB: 362,
    0x0F: 377,
    0x10: 11,
    **{0x10 + d: 1 + d for d in range(1, 10)},
    0x53: 0x313,
    0x1A: 0x304,
    0x02: 115,
    0x03: 114,
    0x00: 402,
    0x01: 403,
    0x1E: 364,
    0xAA: 358,
    0x09: 113,
    0x7C: 0x305,
    0x43: 139,
    0x40: 103,
    0x07: 105,
    0x44: 28,
    0x06: 106,
    0x28: 412,
    0x41: 108,
    0x5B: 174,
    0x0A: 0x31E,
    0x0E: 0x31B,
    0xB1: 128,
    0x8F: 168,
    0xB0: 207,
    0xBA: 119,
    0x8E: 208,
    0x72: 398,
    0x71: 399,
    0x63: 400,
    0x61: 401,
    # the service remote's own
    0xFE: 0x387,
    0x79: 0x31D,
    0x4D: 0x320,
    0xFC: 0x384,
    0xFD: 0x386,
    0x52: 0x321,
    0x60: 0x31A,
    0x20: 0x30B,
    0xF9: 0x393,
    0x95: 0x381,
    0xFF: 0x388,
    0xFB: 0x383,
    0xFA: 0x382,
    0xD5: 0x3BC,
    0xD6: 0x3B3,
    0x5A: 0x3B6,
    0xD0: 0x3B7,
    0xBF: 0x3B9,
    0xD4: 0x3BA,
    0xCE: 0x3BF,
    0xCC: 0x3C0,
    0xE9: 0x3C1,
    0xDA: 0x3C2,
}
TAP_S, GAP_S = 0.15, 0.45
if SWEEP:
    from beanjar.devices.remote_layouts import LAYOUTS

    skip = {"POWER", "P_ONLY", "IN_STOP"}
    sweep = [
        (k.name, k.code) for k in LAYOUTS[SWEEP].keys if k.code is not None and k.name not in skip
    ]
    seconds = round(len(sweep) * (TAP_S + GAP_S)) + 6
else:
    seconds = 6
command = (
    r"""ev=$(awk '/N: Name="LGE RCU"/ {f=1} f && /^H:/ {for(i=2;i<=NF;i++) if($i ~ /^event/) """
    rf"""{{print $i; exit}}}}' /proc/bus/input/devices); timeout {seconds} cat /dev/input/$ev """
    r"""> /host/out/ir-input-test.bin 2>/run/ir-input-test.log &"""
)
env = {"SOCK": str(RUN / "console.sock"), "LOG": str(LOGS / "console.log")}
subprocess.run(
    [str(TOP / "host/scripts/vm-launcher"), "console", command, "1"],
    check=True,
    env={**os.environ, **env},
)
# The serial bridge polls its input file; returning from `host/scripts/vm-launcher console` does not guarantee that the
# guest has run the command yet.
deadline = time.monotonic() + 10
while not capture.exists() and time.monotonic() < deadline:
    time.sleep(0.1)
if not capture.exists():
    sys.exit("guest capture did not start; inspect the console")
time.sleep(0.3)


def record(down, code):
    return struct.pack("4B", 0x49, 1 if down else 0, 0x04, code)


if SWEEP:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect(str(RUN / "ir.sock"))
        for name, code in sweep:
            sock.sendall(record(True, code))
            time.sleep(TAP_S)
            sock.sendall(record(False, code))
            time.sleep(GAP_S)
    time.sleep(7)  # the capture's timeout
    keys = [e[2:] for e in struct.iter_unpack("<qqHHi", captured()) if e[2] == 1]
    want = [(1, LINUX[code], v) for _name, code in sweep for v in (1, 0)]
    wrong = [f"{name} 0x{code:02x}" for name, code in sweep if (1, LINUX[code], 1) not in keys]
    print(f"LGE RCU: {len(keys)} key events for {len(sweep)} taps; missing: {wrong or 'none'}")
    assert keys == want, [(k, w) for k, w in zip(keys, want) if k != w][:5]
    skipped = sorted(k.name for k in LAYOUTS[SWEEP].keys if k.name in skip)
    print(
        f"PASS: all {len(sweep)} keys of the {SWEEP} remote but {', '.join(skipped)} reach"
        " tvservice's LGE RCU as their Linux keys (press, release)"
    )
    sys.exit(0)

with socket.socket(socket.AF_UNIX) as sock:
    sock.connect(str(RUN / "ir.sock"))
    sock.sendall(record(True, 0x7C))
    time.sleep(0.15)
    sock.sendall(record(False, 0x7C))
    time.sleep(1.0)
    sock.sendall(record(True, 0x40))
    time.sleep(1.3)
    sock.sendall(record(False, 0x40))
    time.sleep(0.5)
time.sleep(5)  # the capture's timeout
events = [e[2:] for e in struct.iter_unpack("<qqHHi", captured())]
keys = [e for e in events if e[0] == 1]
home = [e for e in keys if e[1] == KEY_HOME_LG]
up = [e for e in keys if e[1] == KEY_UP]
repeats = sum(1 for e in up if e[2] == 2)
print(
    f"LGE RCU: {len(events)} events; HOME {home}; UP press/release "
    f"{[e for e in up if e[2] != 2]}, {repeats} repeats"
)
assert home == [(1, KEY_HOME_LG, 1), (1, KEY_HOME_LG, 0)], home
assert up[0] == (1, KEY_UP, 1) and up[-1] == (1, KEY_UP, 0), up
# 1.3 s held: the first repeat after 0.5 s, then every 108 ms (sampled by MICOM_Task's 40 ms poll)
assert 4 <= repeats <= 10, repeats
assert all(e == (1, KEY_UP, 2) for e in up[1:-1]), up
assert all(e == (0, 0, 0) for e in events if e[0] != 1), "only EV_KEY and SYN_REPORT"
print("PASS: ir.sock -> micom -> tvservice's LGE RCU: HOME tap, UP long press with repeats")
