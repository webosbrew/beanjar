#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""An automatic channel scan on a running VM with our tuner, pass or fail with numbers.

  run-scan.py NAME [--timeout S]

Takes the VM's channel map (beanjar io tuner list NAME) and the services of the lineup in effect on
it (beanjar io tuner lineup show NAME: the VM's own, or the built-in one of its board's standard,
US ATSC, the Japanese ISDB-T one on an ARIB board or the European DVB one on a DVB board) as what
the scan must find, on the air and on cable: the scan keeps the digital passes, terrestrial and
(where the board has cable: ATSC, DVB) cable, so that a cable channel found by an earlier scan is
not kept when its RF is unmapped, and skips the others (analogue, satellite),
waits for its legacy-broadcast (legacy-broadcast-atsc, -arib or -dvb) to be steady (the same
process for a minute, registered with the bus and answering; a first boot respawns it), runs tests/regress/tuner/scan.sh in the guest as root
(beanjar ares-shell --root: the VM needs the rootssh layer), and checks its answers: every mapped
virtual channel in iepg/getChannelList, no other, and a current event for each in
getCurrentEventList with the title the lineup has on air now, after scan.sh has watched the
channels in Live TV (a set reads only the tuned RF's guide). A channel is named as the set numbers
it, with C before a cable one's: ATSC 7.1 and C78.1, ARIB 011, DVB its logical channel number (1,
C10). A Japanese set lists its satellite presets (BS, CS) beside what it found: only digital
terrestrial and cable channels (channelId 1_... and 3_...) count. On a just-booted VM scan.sh
waits, bounded, until the broadcast service opens and startAutoScan answers (ready_s). Prints the
numbers and PASS or FAIL; the exit status is 0 only for PASS."""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(TOP, "host", "core"))

from beanjar.tuner import arib as arib
from beanjar.tuner import atsc, band
from beanjar.tuner import dvb as dvb

BEANJAR = os.environ.get("BEANJAR", "beanjar")


def env():
    e = dict(os.environ)
    e["PATH"] = os.path.expanduser("~/.cargo/bin") + os.pathsep + e.get("PATH", "")
    return e


def shell(name, command, timeout):
    r = subprocess.run(
        [BEANJAR, "ares-shell", "--root", "-d", name, "-r", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env(),
        stdin=subprocess.DEVNULL,
    )
    return r.stdout


# a channelId starts with its source type: 1 digital terrestrial, 3 digital cable
SOURCE_AIR, SOURCE_CABLE = "1", "3"
# the channels watched for their guide: a Japanese set's digital terrestrial ones (its satellite
# presets are not); a DVB set's digital ones, each service (no RF-first pattern of its ids)
WATCH = {
    "arib": '"channelId":"1_[^"]*"',
    "dvb": '"channelId":"[13]_[^"]*"',
}
# the scan passes kept (scan.sh's PASSES): the digital ones of the board's standard. A pass that is
# skipped keeps the channels an earlier scan found in it.
PASSES = {"atsc": "TERRESTRIAL_DIGITAL CABLE_DIGITAL", "dvb": "TERRESTRIAL_DIGITAL CABLE_DIGITAL"}


def label(number, cable):
    """A found or expected channel's name: its number as the set gives it, C before a cable one."""
    return ("C" if cable else "") + number


def expected(channels, now, lineup):
    """{"major.minor": current title} of the lineup's services on the mapped channels (lineup: the
    values of beanjar io tuner lineup show NAME --json), "C78.1" on cable; for ARIB {"NNN": title},
    the three-digit number a Japanese set gives a service (remote control key times ten plus the
    service's number within the TS); for DVB the logical channel number ("C10" on cable). The
    channels of a stream of the user's are not the lineup's: not expected."""
    out = {}
    for rf in lineup["rf"]:
        if rf["rf"] not in channels:
            continue
        cable = band.parse(rf["rf"])[0]
        for s in rf["services"]:
            if lineup["broadcast"] == "arib":
                number = rf["remote_key"] * 10 + (s["service_id"] & 7) + 1
                out[f"{number:03d}"] = arib.events(s, now)[0][2]
            elif lineup["broadcast"] == "dvb":
                on_air = dvb.present_following(s, now, lineup["timezone"])
                out[label(str(s["lcn"]), cable)] = on_air[0][2] if on_air else None
            else:
                on_air = atsc.events(s, now, now + 1, lineup["timezone"])
                out[label(f"{s['major']}.{s['minor']}", cable)] = on_air[0][2] if on_air else None
    return out


# legacy-broadcast, set up on a just-booted VM, can exit and be respawned for a while (a scan then
# fails with "legacybroadcast.channel is not running"): steady is the same process ids for STEADY_S
# in a row while it is registered with the bus and answers a call, bounded by STEADY_MAX_S in all
STEADY_S = 60
STEADY_MAX_S = 900
STEADY_PROBE = (
    "p=$(pidof {service}); echo $p; "
    'ls-monitor -l 2>/dev/null | grep -q "^$p .*com.webos.service.legacybroadcast.broadcast" && echo registered; '
    "luna-send -n 1 -w 5000 luna://com.webos.service.legacybroadcast.broadcast/getAudioLanguageList "
    '\'{{"broadcastId":"-"}}\' </dev/null 2>&1 | grep -q returnValue && echo answers'
)


def steady(name, service, hold=STEADY_S, limit=STEADY_MAX_S):
    """Seconds it took until the service had run unchanged for hold seconds and answered, or None."""
    t0 = time.monotonic()
    last, since = None, t0
    while time.monotonic() - t0 < limit:
        try:
            out = shell(name, STEADY_PROBE.format(service=service), 60)
        except subprocess.TimeoutExpired:
            out = ""
        now = time.monotonic()
        words = out.split()
        pids = [w for w in words if w[0].isdigit()]
        state = (
            tuple(sorted(pids)) if pids and "answers" in words and "registered" in words else None
        )
        if state is None or state != last:
            last, since = state, now
        elif now - since >= hold:
            return int(now - t0)
        time.sleep(5)
    return None


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
    mapped = [c["channel"] for c in st["channels"] if c["mapped"] and not c.get("stream")]
    streams = [c["channel"] for c in st["channels"] if c["mapped"] and c.get("stream")]
    lineup = json.loads(
        subprocess.run(
            [BEANJAR, "io", "tuner", "lineup", "show", a.name, "--json"],
            capture_output=True,
            text=True,
            check=True,
            env=env(),
        ).stdout
    )["values"]
    broadcast = st.get("broadcast", "atsc")
    service = f"legacy-broadcast-{broadcast}"
    if not mapped and not streams:
        print("FAIL: no channel mapped (beanjar io tuner map NAME RF...)")
        return 1
    ready = steady(a.name, service)
    if ready is None:
        print(f"FAIL: {service} is not steady (not running, or respawning)")
        return 1
    print(f"broadcast_steady_s={ready}")
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
    watch = f" '{WATCH[broadcast]}'" if broadcast in WATCH else ""
    passes = PASSES.get(broadcast, "TERRESTRIAL_DIGITAL")
    out = shell(a.name, f"PASSES='{passes}' sh /tmp/scan.sh {a.timeout}{watch}", a.timeout + 300)
    now = time.time()
    r = parse(out)
    want = expected(mapped, now, lineup)
    got_channels, ids = {}, {}
    for v in r.get("channel", []):
        # NUMBER NAME CHANNELID, the name may have spaces (DVB: "Beanjar Eins")
        number, _, rest = v.partition(" ")
        cname, _, cid = rest.rpartition(" ")
        source = cid.split("_")[0]
        if source not in (SOURCE_AIR, SOURCE_CABLE):
            continue  # a satellite preset of a Japanese set
        n = label(number.replace("-", "."), source == SOURCE_CABLE)
        got_channels[n] = cname
        ids[cid] = n
    on_air = {}
    for v in r.get("event", []):
        cid, _, title = v.partition(" ")
        # an ARIB event names its channel by the tail of the channelId (ONID_SVCID_TSID)
        n = ids.get(cid) or next((x for i, x in ids.items() if i.endswith("_" + cid)), cid)
        on_air.setdefault(n, set()).add(title)
    missing = sorted(set(want) - set(got_channels))
    extra = sorted(set(got_channels) - set(want))
    # a user's stream's services are its own: found, not expected
    from_streams, extra = (extra, []) if streams else ([], extra)
    no_event = sorted(n for n, title in want.items() if title not in on_air.get(n, ()))
    numbers = {
        "broadcast": broadcast,
        "mapped_rf": mapped,
        "passes": passes.split(),
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
        "stream_rf": streams,
        "from_streams": from_streams,
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
