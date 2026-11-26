#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The virtual USB gamepad in a running VM, pass or fail with numbers.

  run-gamepad.py NAME|HOST/NAME [--presses 10] [--control a] [--hold 100ms]

Plugs the pad in (beanjar io usb gamepad attach NAME: a wired Xbox 360 controller, 045e:028e,
served on the VM's host) and checks, in the guest (beanjar shell) and in LG's PDM (beanjar
ares-shell --root: the VM needs the rootssh layer):
  1. within 10 s: the USB device 045e:028e in /sys/bus/usb/devices, its interface bound to xpad,
     an input device of it with an event and a js node, and udev's ID_INPUT_JOYSTICK=1 on the
     event node (waited for in the guest);
  2. PDM's getAttachedNonStorageDeviceList lists it once, as deviceType XPAD;
  3. --presses presses of --control, each its own `io usb gamepad press`, arrive on the event node
     as key press and release events of the control's code (BTN_SOUTH for a), all of them, in
     order, and each press command says the guest polled the pad;
  4. after io usb gamepad detach, within 5 s: no 045e:028e in sysfs, no event node, nothing in
     PDM's list.
Prints the numbers and PASS or FAIL; the exit status is 0 only for PASS. The pad is unplugged at
the end whatever happened."""

import argparse
import json
import os
import re
import subprocess
import sys
import time

BEANJAR = os.environ.get("BEANJAR", "beanjar")
VID, PID = "045e", "028e"
EV_KEY = 1
# the evdev key code xpad reports for each control (drivers/input/joystick/xpad.c)
CODES = {
    "a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134, "lb": 0x136, "rb": 0x137,
    "back": 0x13a, "start": 0x13b, "guide": 0x13c, "ls": 0x13d, "rs": 0x13e,
}  # fmt: skip
FIND_WAIT = 10.0
GONE_WAIT = 5.0
CAPTURE = "/tmp/beanjar-gamepad.bin"
PDM_LIST = (
    "luna-send -w 5000 -n 1 luna://com.webos.service.pdm/getAttachedNonStorageDeviceList "
    "'{\"subscribe\":false}'"
)
CAPTURE_PID = "/tmp/beanjar-gamepad.pid"

# key=value lines: the device's sysfs path, its driver, its input's event and js nodes, udev's
# joystick property of the event node; waited for in the guest for up to WAIT seconds until all
# are there (or with GONE=1, until the device is gone), with the wait in waited_ms
PROBE = r"""
probe() {
  dev=; xpad=0; joystick=0; nodes=
  for d in /sys/bus/usb/devices/*; do
    [ -f "$d/idVendor" ] || continue
    [ "$(cat $d/idVendor):$(cat $d/idProduct)" = "VID:PID" ] && dev=$d
  done
  [ -n "$dev" ] || return 0
  intf=$(basename $dev):1.0
  [ -e /sys/bus/usb/drivers/xpad/$intf ] && xpad=1
  for n in $dev/$intf/input/input*/event* $dev/$intf/input/input*/js*; do
    [ -e "$n" ] && nodes="$nodes $(basename $n)"
  done
  for n in $dev/$intf/input/input*/event*; do
    [ -e "$n" ] || continue
    udevadm info /dev/input/$(basename $n) | grep -q "ID_INPUT_JOYSTICK=1" && joystick=1
  done
}
cs() { read u _ < /proc/uptime; echo $(( ${u%.*} * 100 + 1${u#*.} - 100 )); }  # centiseconds
start=$(cs); end=$((start + WAIT * 100))
while :; do
  probe
  now=$(cs)
  if [ "${GONE:-0}" = 1 ]; then [ -z "$dev" ] && break
  else [ -n "$dev" ] && [ $xpad = 1 ] && [ $joystick = 1 ] && case "$nodes" in *event*js*|*js*event*) true;; *) false;; esac && break
  fi
  [ $now -ge $end ] && break
  sleep 0.1
done
echo "waited_ms=$(( (now - start) * 10 ))"
echo "device=$dev"
[ -n "$dev" ] || exit 0
echo "speed=$(cat $dev/speed)"
echo "xpad=$xpad"
echo "joystick=$joystick"
for n in $nodes; do echo "node=$n"; done
""".replace("VID:PID", f"{VID}:{PID}")


def env():
    e = dict(os.environ)
    e["PATH"] = os.path.expanduser("~/.cargo/bin") + os.pathsep + e.get("PATH", "")
    return e


def run(argv, timeout=60):
    p = subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=timeout, env=env()
    )
    return p.returncode, p.stdout, p.stderr


def shell(name, script, timeout=60):
    return run([BEANJAR, "shell", name, "--", "sh", "-c", script], timeout)[1]


def probe(name, wait, gone=False):
    out = {"node": []}
    script = f"WAIT={int(wait)}; GONE={int(gone)}\n" + PROBE
    for line in shell(name, script, timeout=wait + 60).splitlines():
        k, sep, v = line.partition("=")
        if not sep:
            continue
        if k == "node":
            out["node"].append(v)
        else:
            out[k] = v
    return out


def pdm(name):
    """PDM's entries for the pad: [deviceType...]; None if PDM did not answer."""
    rc, out, _err = run(
        [BEANJAR, "ares-shell", "--root", "-d", name, "-r", PDM_LIST],
        timeout=60,
    )  # fmt: skip
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if rc or not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return [
        d.get("deviceType")
        for d in data.get("nonStorageDeviceList", [])
        if d.get("vendorID") == VID and d.get("productID") == PID
    ]


def events(raw):
    """(type, code, value) of evdev events in raw: 16-byte ones (a 32-bit reader) or 24-byte ones,
    whichever splits it into events whose type is a known one."""
    for size in (16, 24):
        if len(raw) % size:
            continue
        out = []
        for i in range(0, len(raw), size):
            e = raw[i + size - 8 : i + size]
            t, c, v = int.from_bytes(e[0:2], "little"), int.from_bytes(e[2:4], "little"), \
                int.from_bytes(e[4:8], "little", signed=True)  # fmt: skip
            out.append((t, c, v))
        if all(t in (0, 1, 3, 4) for t, _c, _v in out):
            return out
    return []


def wait_for(check, wait):
    deadline = time.monotonic() + wait
    t0 = time.monotonic()
    while True:
        result = check()
        if result or time.monotonic() > deadline:
            return result, time.monotonic() - t0
        time.sleep(0.5)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("name", metavar="NAME|HOST/NAME")
    p.add_argument("--presses", type=int, default=10)
    p.add_argument("--control", default="a", choices=sorted(CODES))
    p.add_argument("--hold", default="100ms")
    a = p.parse_args()
    numbers, checks = {}, {}

    t0 = time.monotonic()
    rc, out, err = run([BEANJAR, "io", "usb", "gamepad", "attach", a.name, "--json"], 90)
    numbers["attach_s"] = round(time.monotonic() - t0, 2)
    if rc:
        print(f"attach failed: {err.strip()}")
        print("FAIL")
        return 1
    pad_id = json.loads(out)["id"]
    try:
        # 1. the device, xpad, its nodes and udev
        pr = probe(a.name, FIND_WAIT)
        numbers["guest_found_ms"] = int(pr.get("waited_ms", -1))
        checks["device, xpad, event+js nodes, ID_INPUT_JOYSTICK in 10 s"] = (
            bool(pr.get("device")) and pr.get("xpad") == "1" and pr.get("joystick") == "1"
            and any(n.startswith("event") for n in pr["node"])
            and any(n.startswith("js") for n in pr["node"])
            and numbers["guest_found_ms"] <= FIND_WAIT * 1000
        )  # fmt: skip
        numbers["guest"] = {k: v for k, v in pr.items() if k not in ("node", "waited_ms")} | {
            "nodes": pr["node"]
        }
        event = next((n for n in pr["node"] if n.startswith("event")), None)

        # 2. PDM
        listed, took = wait_for(lambda: pdm(a.name), FIND_WAIT)
        numbers["pdm"] = listed
        checks["PDM lists it once as XPAD"] = listed == ["XPAD"]

        # 3. presses read on the event node
        presses_ok = 0
        down = up = 0
        if event:
            shell(
                a.name,
                f"rm -f {CAPTURE}; (setsid sh -c 'echo $$ > {CAPTURE_PID}; exec timeout 120 cat "
                f"/dev/input/{event} > {CAPTURE}' </dev/null >/dev/null 2>&1 &); sleep 1",
            )
            for _ in range(a.presses):
                rc, out, err = run(
                    [BEANJAR, "io", "usb", "gamepad", "press", a.name, a.control,
                     "--hold", a.hold, "--id", pad_id, "--json"]
                )  # fmt: skip
                if rc == 0:
                    r = json.loads(out)
                    presses_ok += r["polled"] and r["reports"] == 2
            time.sleep(0.5)
            dump = shell(
                a.name,
                f"kill $(cat {CAPTURE_PID}) 2>/dev/null; sleep 0.3; od -A n -t x1 -v {CAPTURE}; "
                f"rm -f {CAPTURE} {CAPTURE_PID}",
            )
            raw = bytes.fromhex("".join(dump.split()))
            keys = [v for t, c, v in events(raw) if t == EV_KEY and c == CODES[a.control]]
            numbers["key_events"] = len(keys)
            down = sum(1 for v in keys if v == 1)
            up = sum(1 for v in keys if v == 0)
            checks["presses in order"] = keys == [1, 0] * a.presses
        numbers["press_commands_polled"] = f"{presses_ok}/{a.presses}"
        numbers["presses_received"] = f"{down}/{a.presses}"
        numbers["releases_received"] = f"{up}/{a.presses}"
        checks["every press command was polled"] = presses_ok == a.presses
        checks[f"{a.presses} of {a.presses} presses and releases"] = (
            down == a.presses and up == a.presses
        )
    finally:
        # 4. unplugged
        rc, _out, err = run([BEANJAR, "io", "usb", "gamepad", "detach", a.name, "--id", pad_id])
        checks["detach"] = rc == 0
        if rc:
            print(f"detach failed: {err.strip()}")

    pr = probe(a.name, GONE_WAIT, gone=True)
    numbers["guest_gone_ms"] = int(pr.get("waited_ms", -1))
    checks["gone from sysfs in 5 s"] = not pr.get("device")
    listed, took = wait_for(lambda: pdm(a.name) == [], GONE_WAIT)
    checks["gone from PDM in 5 s"] = bool(listed)
    numbers["pdm_gone_s"] = round(took, 1)

    for k, v in numbers.items():
        print(f"{k}: {v}")
    for k, v in checks.items():
        print(f"{'ok  ' if v else 'FAIL'} {k}")
    ok = all(checks.values())
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
