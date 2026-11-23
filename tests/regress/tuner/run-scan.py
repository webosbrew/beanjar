#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""An automatic channel scan on a running VM with our tuner, pass or fail with numbers.

  run-scan.py NAME [--timeout S]

Takes the VM's channel map (beanjar io tuner list NAME) and the lineup's services on it as what the scan
must find, waits for legacy-broadcast to run, runs tests/regress/tuner/scan.sh in the guest as root
(beanjar ares-shell --root: the VM needs the rootssh layer), and checks its answers: every mapped
virtual channel in iepg/getChannelList, no other, and a current event for each in
getCurrentEventList with the title the lineup has on air now, after scan.sh has watched the first
channel of each RF in Live TV (a set reads only the tuned RF's guide). On a just-booted VM scan.sh
waits, bounded, until the broadcast service opens and startAutoScan answers (ready_s). Prints the
numbers and PASS or FAIL; the exit status is 0 only for PASS."""

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(TOP, "host", "core"))

from beanjar import tuner_stream  # noqa: E402

BEANJAR = os.environ.get("BEANJAR", "beanjar")


def env():
    e = dict(os.environ)
    e["PATH"] = os.path.expanduser("~/.cargo/bin") + os.pathsep + e.get("PATH", "")
    return e


def shell(name, command, timeout):
    r = subprocess.run(
        [BEANJAR, "ares-shell", "--root", "-d", name, "-r", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env(),
        stdin=subprocess.DEVNULL,
    )
    return r.stdout


def expected(channels, now):
    """{"major.minor": current title} of the lineup's services on the mapped channels."""
    out = {}
    for ch in channels:
        for s in tuner_stream.channel(ch)["services"]:
            on_air = [e for e in tuner_stream.events(s, now, now + 1)]
            out[f"{s['major']}.{s['minor']}"] = on_air[0][2] if on_air else None
    return out


def parse(text):
    """KEY=VALUE lines before '--- raw': {key: [values]}."""
    out = {}
    for line in text.split("--- raw")[0].splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(k.strip(), []).append(v.strip())
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds the scan may take")
    a = ap.parse_args()
    st = json.loads(
        subprocess.run(
            [BEANJAR, "io", "tuner", "list", a.name, "--json"],
            capture_output=True,
            text=True,
            check=True,
            env=env(),
        ).stdout
    )
    mapped = [c["channel"] for c in st["channels"] if c["mapped"]]
    if not mapped:
        print("FAIL: no channel mapped (beanjar io tuner map NAME RF...)")
        return 1
    for _ in range(90):  # legacy-broadcast runs a while after boot
        if "up" in shell(a.name, "pidof legacy-broadcast-atsc >/dev/null && echo up", 60):
            break
        time.sleep(10)
    else:
        print("FAIL: legacy-broadcast-atsc is not running")
        return 1
    subprocess.run(
        [
            BEANJAR,
            "ares-push",
            "--root",
            "-d",
            a.name,
            os.path.join(HERE, "scan.sh"),
            "/tmp/scan.sh",
        ],
        capture_output=True,
        check=True,
        env=env(),
        timeout=120,
    )
    out = shell(a.name, f"sh /tmp/scan.sh {a.timeout}", a.timeout + 300)
    now = time.time()
    r = parse(out)
    want = expected(mapped, now)
    got_channels, ids = {}, {}
    for v in r.get("channel", []):
        number, cname, cid = (v.split(" ", 2) + ["", ""])[:3]
        got_channels[number.replace("-", ".")] = cname
        ids[cid] = number.replace("-", ".")
    on_air = {}
    for v in r.get("event", []):
        cid, _, title = v.partition(" ")
        on_air.setdefault(ids.get(cid, cid), set()).add(title)
    missing = sorted(set(want) - set(got_channels))
    extra = sorted(set(got_channels) - set(want))
    no_event = sorted(n for n, title in want.items() if title not in on_air.get(n, ()))
    numbers = {
        "mapped_rf": mapped,
        "ready_s": int(r.get("ready_s", ["-1"])[0]),
        "start_tries": int(r.get("start_tries", ["0"])[0]),
        "scan_s": int(r.get("scan_s", ["-1"])[0]),
        "skipped": r.get("skipped", []),
        "watched": r.get("watched", []),
        "channels_found": len(got_channels),
        "channels_expected": len(want),
        "events_listed": int(r.get("events", ["0"])[0] or 0),
        "missing": missing,
        "extra": extra,
        "without_current_event": no_event,
    }
    print(json.dumps(numbers))
    if r.get("error"):
        print(f"FAIL: {r['error'][0]}")
        print(out[-3000:])
        return 1
    if r.get("timeout"):
        print("FAIL: the scan did not finish in time")
        return 1
    if missing or extra or no_event:
        print("FAIL")
        print(out[-3000:])
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
