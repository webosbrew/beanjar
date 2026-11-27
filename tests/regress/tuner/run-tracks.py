#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A service's second sound track and its closed captions on a running US VM with our tuner, pass
or fail with numbers.

  run-tracks.py NAME [--channel BJ7] [--shots DIR]

After a scan (run-scan.py) of the built-in lineup, whose 7.1 BJ7 has an English and a Spanish AC-3
track and CEA-608 captions (host/core/beanjar/tuner/builtin-lineups/atsc.toml): turns the set's captions on (the
system setting caption/captionEnable), watches the channel in Live TV and checks

- the audio language list of Live TV's pipeline (legacybroadcast.broadcast/getAudioLanguageList)
  has the lineup's languages, in its order;
- setAudioLanguage to the second one makes it the current one and the audio decoder's filter
  moves to that track's PID (dtv_soc's last started PIDs, /proc/dtv_soc), and back;
- the video decoder sends the pictures' user data (dtv_soc's vdec userdata count grows) and the
  service says the channel has captions (legacybroadcast.subtitle/getSubtitleState).

With --shots, keeps screenshots there: captions on, on the second track, captions off. Prints the
numbers as JSON and PASS or FAIL; the exit status is 0 only for PASS. NAME may be HOST/NAME."""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(TOP, "host", "core"))

from beanjar.tuner import lineup, atsc

BEANJAR = os.environ.get("BEANJAR", "beanjar")

# The guest's half: one script, so the Live TV watch it opens stays subscribed throughout. It
# prints KEY=VALUE lines; "phase=NAME" lines are where the host takes a screenshot (it answers by
# creating /tmp/tracks-go).
GUEST = r"""
L="luna-send -n 1 -w 30000"
U=luna://com.webos.service.utp.broadcast
mono() { cut -d. -f1 /proc/uptime; }
# the PIDs whose filters started in the next 5 s (the guide's section filters restart all the time,
# so the audio filter's start is looked for among them)
started() { for i in 1 2 3 4 5 6 7 8 9 10; do sed -n 's/.*the last PIDs //p' /proc/dtv_soc; sleep 0.5; done |
	tr ' ' '\n' | sort -u | tr '\n' ' '; }
wait_go() { rm -f /tmp/tracks-go; echo "phase=$1"; t=$(mono);
	while [ ! -e /tmp/tracks-go ] && [ $(($(mono) - t)) -lt 120 ]; do sleep 1; done; }
caption() { $L luna://com.webos.settingsservice/setSystemSettings \
	"{\"category\":\"caption\",\"settings\":{\"captionEnable\":\"$1\",\"captionAnalog\":\"cc1\",\"captionDigital\":\"svc1\"}}" \
	</dev/null | tr -d '\n '; }
echo "caption_on=$(caption on)"
echo "caption_get=$($L luna://com.webos.settingsservice/getSystemSettings \
	'{"category":"caption","keys":["captionEnable","captionAnalog","captionDigital"]}' </dev/null | tr -d '\n ')"
CH=$($L luna://com.webos.service.iepg/getChannelList \
	'{"channelGroup":"All","channelMode":"Tuner","dataType":0,"sort":0}' </dev/null | tr -d '\n')
CID=$(echo "$CH" | sed 's/},{"channelNumber"/}\n{"channelNumber"/g' |
	sed -n 's/.*"channelName":"@NAME@".*"channelId":"\([^"]*\)".*/\1/p' | head -1)
echo "channel_id=$CID"
[ -n "$CID" ] || { echo "error=no channel @NAME@"; exit 1; }
AM=luna://com.webos.service.applicationmanager
$L $AM/closeByAppId '{"id":"com.webos.app.livetv"}' </dev/null >/dev/null
sleep 5
$L $AM/launch '{"id":"com.webos.app.livetv"}' </dev/null >/dev/null
sleep 10
luna-send -i $U/open '{"subscribe":true,"options":{"appId":"com.webos.app.livetv","windowId":"","purpose":"WATCH"}}' \
	</dev/null > /tmp/tracks-watch.out 2>&1 &
WATCH=$!
sleep 5
W=$(sed -n 's/.*"broadcastId": *"\([^"]*\)".*/\1/p' /tmp/tracks-watch.out | head -1)
echo "broadcast_id=$W"
echo "change=$($L $U/changeChannel "{\"broadcastId\":\"$W\",\"channelId\":\"$CID\"}" </dev/null | tr -d '\n ')"
sleep 15
LB=luna://com.webos.service.legacybroadcast
# Live TV's own pipeline, which plays the channel, by legacy-broadcast's id (the subtitle service
# names it when asked of another); utp's audio methods are Live TV's only
P=$($L $LB.subtitle/getSubtitleState '{"broadcastId":"-"}' </dev/null |
	sed -n 's/.*"activePipelineId": *"\([^"]*\)".*/\1/p')
echo "pipeline_id=$P"
B="{\"broadcastId\":\"$P\"}"
ud0=$(sed -n 's/.*vdec channel .* streaming .* userdata \([0-9]*\).*/\1/p' /proc/dtv_soc | head -1)
LIST=$($L $LB.broadcast/getAudioLanguageList "$B" </dev/null | tr -d '\n')
echo "audio_list=$LIST"
# the set's own ids of the tracks, in the list's order
IDS=$(echo "$LIST" | grep -o '"audioLanguageId": *[0-9]*' | sed 's/.*: *//')
FIRST=$(echo "$IDS" | sed -n 1p)
SECOND=$(echo "$IDS" | sed -n 2p)
echo "subtitle_state=$($L $LB.subtitle/getSubtitleState "$B" </dev/null | tr -d '\n ')"
echo "pids_before=$(sed -n 's/.*the last PIDs //p' /proc/dtv_soc)"
wait_go captions
ud1=$(sed -n 's/.*vdec channel .* streaming .* userdata \([0-9]*\).*/\1/p' /proc/dtv_soc | head -1)
echo "userdata=$ud0 $ud1"
echo "set_second=$($L $LB.broadcast/setAudioLanguage "{\"broadcastId\":\"$P\",\"audioLanguageId\":${SECOND:-1}}" </dev/null | tr -d '\n ')"
echo "pids_after=$(started)"
echo "audio_list_after=$($L $LB.broadcast/getAudioLanguageList "$B" </dev/null | tr -d '\n')"
wait_go second
echo "set_first=$($L $LB.broadcast/setAudioLanguage "{\"broadcastId\":\"$P\",\"audioLanguageId\":${FIRST:-0}}" </dev/null | tr -d '\n ')"
echo "pids_back=$(started)"
echo "caption_off=$(caption off)"
sleep 5
wait_go off
kill $WATCH 2>/dev/null
echo "done=1"
"""


def env():
    e = dict(os.environ)
    e["PATH"] = os.path.expanduser("~/.cargo/bin") + os.pathsep + e.get("PATH", "")
    e.setdefault("QT_QPA_PLATFORM", "offscreen")
    return e


def shell(name, command, timeout=60):
    return subprocess.run(
        [BEANJAR, "ares-shell", "--root", "-d", name, "-r", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env(),
        stdin=subprocess.DEVNULL,
        check=False,
    ).stdout


def answer(text):
    """The JSON object in a luna-send answer line, or {}."""
    try:
        return json.loads(text[text.index("{") :])
    except ValueError:
        return {}


def languages(reply):
    """[(audioLanguageId, a three-letter code or the name)] of getAudioLanguageList's answer."""
    out = []
    for t in reply.get("audioLanguageList", []):
        code = t.get("audioLanguageCode") or t.get("languageCode") or t.get("audioLanguageName")
        out.append((t.get("audioLanguageId", t.get("audioLanguageIndex")), str(code).lower()))
    return out


def pids(text):
    return sorted({int(p, 16) for p in text.split()})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name", help="the VM: NAME, or HOST/NAME for another host's")
    ap.add_argument("--channel", default="BJ7", help="the service's short name (default BJ7)")
    ap.add_argument("--shots", help="keep screenshots in this folder")
    a = ap.parse_args()
    lu = lineup.default("atsc")
    found = [(rf, i, s) for rf in lu["rf"] for i, s in enumerate(rf["services"])
             if s["name"] == a.channel]  # fmt: skip
    if not found:
        print(f"FAIL: the built-in lineup has no service {a.channel}")
        return 1
    _rf, svc_i, svc = found[0]
    want = [t["language"] for t in svc["sound"]]
    want_pids = [atsc.audio_pid(svc_i, k) for k in range(len(want))]
    if len(want) < 2 or not svc.get("captions"):
        print(f"FAIL: {a.channel} has no second sound track or no captions in the lineup")
        return 1
    if a.shots:
        os.makedirs(a.shots, exist_ok=True)

    script = GUEST.replace("@NAME@", a.channel)
    p = subprocess.Popen(
        [BEANJAR, "ares-shell", "--root", "-d", a.name, "-r", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env(),
        stdin=subprocess.DEVNULL,
    )
    got, shots = {}, []
    for line in p.stdout:
        line = line.rstrip("\n")
        key, _, value = line.partition("=")
        if not _:
            continue
        got[key] = value
        if key == "phase":
            if a.shots:
                shot = os.path.join(a.shots, f"{len(shots) + 1:02d}-{value}.png")
                subprocess.run(
                    [BEANJAR, "screenshot", a.name, shot],
                    capture_output=True,
                    env=env(),
                    check=False,
                )
                shots.append(shot)
            shell(a.name, "touch /tmp/tracks-go")
    p.wait(timeout=600)

    before = answer(got.get("audio_list", ""))
    after = answer(got.get("audio_list_after", ""))
    listed = languages(before)
    ud = [int(x) for x in got.get("userdata", "").split()] or [0, 0]
    sub = answer(got.get("subtitle_state", ""))
    numbers = {
        "channel_id": got.get("channel_id"),
        "languages": [c for _i, c in listed],
        "ids": [i for i, _c in listed],
        "current_before": before.get("currentAudioLanguageId"),
        "current_after": after.get("currentAudioLanguageId"),
        "pids_started_after": [hex(p) for p in pids(got.get("pids_after", ""))],
        "pids_started_back": [hex(p) for p in pids(got.get("pids_back", ""))],
        "userdata_events": ud[-1] - ud[0] if len(ud) == 2 else None,
        "closed_caption_exist": sub.get("isClosedCaptionExist", sub.get("closedCaptionExist")),
        "shots": shots,
    }
    print(json.dumps(numbers))
    if os.environ.get("TRACKS_RAW"):
        print(json.dumps(got, indent=1))
    fails = []
    if len(listed) != len(want) or not all(
        w in c or c.startswith(w[:2]) for w, (_i, c) in zip(want, listed, strict=False)
    ):
        fails.append(f"the audio list is {numbers['languages']}, the lineup's {want}")
    if hex(want_pids[1]) not in numbers["pids_started_after"]:
        fails.append(f"no audio filter started on PID {want_pids[1]:#x}")
    if hex(want_pids[0]) not in numbers["pids_started_back"]:
        fails.append(f"no audio filter started again on PID {want_pids[0]:#x}")
    if numbers["current_after"] == numbers["current_before"]:
        fails.append("the current audio language did not change")
    if not numbers["userdata_events"]:
        fails.append("the video decoder sent no user data")
    if not numbers["closed_caption_exist"]:
        fails.append("the service sees no captions")
    if "done" not in got:
        fails.append("the guest's script did not finish")
    if fails:
        print("FAIL: " + "; ".join(fails))
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
