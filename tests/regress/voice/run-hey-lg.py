#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The "Hey LG" wake word on a running VM of a model with the far-field microphone, pass or fail
with numbers, judged by LG's own result file.

  run-hey-lg.py NAME [--clip FILE] [--sensitivity N] [--timeout S]

FILE (default hey-lg-slt.flac beside this script, make-hey-lg.sh's synthetic one) is the phrase:
any format ffmpeg reads, with silence around it. The check is LG's wake-word test hook (TAS) in
com.webos.service.trigger.thinq (W22O 43.21.89; docs/research/microphone-emulation.md), with the
engine in LG's assemble mode. That is a test route, not a set's normal one: a shipped set loses the
wake-word model of its apps partition on its first boot (trigger_thinq's platform-upgrade cleanup)
and gets it back only from LG's server, with the set's own key, which a VM does not have. In
assemble mode the engine reads the firmware's own model in /usr/share/voiceinput/triggerThinq/
models/snsr instead.

1. tas/readyTest, then tas/runTest 2 {status: on}: LG's assemble mode on ("[TAS] assemble mode :
   ON"), so CTriggerEngineTHF::loadModels takes the /usr/share model ("Assemble mode is running.
   SNSR DB path is changed.").
2. tas/runTest 3 {language: en-US}: LG's "TV menu language change" test, which turns the engine's
   activation on. It stands in for what a set gets from LG's server: the language list
   voiceconductor takes from sdx's provisioning, which the VM's LG mock does not give
   (supportLanguage), and the user's wake-up setting (settingStatus).
3. tas/runTest 4 {triggerWord: hiLG, sensoryPoint: N} (default 7, the en-US default of the
   firmware's thf_config).
4. tas/runTest 5 {inputPcm}: the phrase, 16 kHz, as the five interleaved channels of the far-field
   front end (the hook takes channel 1), fed to the engine (CThinqManager::_tasFeedData). Each
   detection appends "BEGIN,END" (the engine's sample counter) to /tmp/voiceinput/trigger_result.txt.
5. tas/runTest 2 {status: off} and tas/endTest: assemble mode off and out of the test mode (it
   turns the wake-up setting off again, as it was).

PASS when the feed reaches 100 % and the result file has a detection. Prints the numbers: the
phrase's length, the feed's progress, the detections (and their length in seconds at 16 kHz). The
exit status is 0 only for PASS."""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BEANJAR = os.environ.get("BEANJAR", "beanjar")
SERVICE = "luna://com.webos.service.trigger.thinq"
SNSR = "/usr/share/voiceinput/triggerThinq/models/snsr"  # the firmware's, read in assemble mode
RESULT = "/tmp/voiceinput/trigger_result.txt"
PCM = "/tmp/hey-lg.5ch.pcm"
# trigger_thinq's PmLog lines (/var/log/messages, rotated to messages.N.gz) of LG's assemble mode:
# the TAS hook's switch and the engine's model load from /usr/share
ASSEMBLE = ("[TAS] assemble mode : ON", "Assemble mode is running. SNSR DB path is changed.")
RATE = 16000
CHANNELS = 5  # the far-field front end's, which tas/runTest 5 takes apart (its channel 1)


def shell(name, command, timeout=30, data=None):
    """(status, stdout) of command in the VM's webOS, as root (beanjar shell)."""
    r = subprocess.run(
        [BEANJAR, "shell", name, "--", "sh", "-c", command],
        input=data,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return r.returncode, r.stdout.decode(errors="replace")


def luna(name, method, params, timeout=10):
    """The first answer of SERVICE/method, as a dict ({} for none)."""
    cmd = f"luna-send -n 1 -w {timeout * 1000} {SERVICE}/{method} '{json.dumps(params)}' </dev/null"
    _, out = shell(name, cmd, timeout + 20)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def log_counts(name):
    """How many times each ASSEMBLE line is in the VM's PmLog so far."""
    cat = "{ zcat /var/log/messages.*.gz 2>/dev/null; cat /var/log/messages; }"
    _, out = shell(name, f"{cat} | grep -F trigger_thinq")
    return [out.count(line) for line in ASSEMBLE]


def five_channels(clip):
    """The clip as 16 kHz S16_LE, its mono samples on each of five interleaved channels."""
    mono = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            clip,
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    mono = mono[: len(mono) - len(mono) % 2]
    return b"".join(mono[i : i + 2] * CHANNELS for i in range(0, len(mono), 2)), len(mono) // 2


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("name")
    ap.add_argument("--clip", default=os.path.join(HERE, "hey-lg-slt.flac"))
    ap.add_argument("--sensitivity", type=int, default=7)
    ap.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args()
    fails = []

    pcm, frames = five_channels(a.clip)
    print(f"clip: {a.clip}: {frames / RATE:.3f} s")
    _, listing = shell(a.name, f"ls {SNSR} 2>/dev/null")
    models = [n for n in listing.split() if n.endswith(".snsr")]
    print(f"firmware model: {', '.join(models) or 'none'}")
    if not models:
        print(f"FAIL: no wake-word model in {SNSR} (this firmware ships none)")
        return 1

    print("route: LG's TAS test hook, assemble mode (a set's model comes from LG's server)")
    before = log_counts(a.name)
    luna(a.name, "tas/readyTest", {})
    luna(a.name, "tas/runTest", {"testNumber": 2, "status": "on", "subscribe": True})
    luna(a.name, "tas/runTest", {"testNumber": 3, "language": "en-US", "subscribe": True})
    deadline = time.monotonic() + 30
    act = {}
    while time.monotonic() < deadline:
        act = luna(a.name, "getActivation", {})
        if act.get("activationState"):
            break
        time.sleep(1)
    print(
        "activation: "
        + ", ".join(
            f"{k} {act.get(k)}"
            for k in ("activationState", "settingStatus", "supportLanguage", "systemStatus")
        )
    )
    if not act.get("activationState"):
        print("FAIL: the engine's activation did not turn on")
        return 1
    seen = [n - b for n, b in zip(log_counts(a.name), before)]
    for line, n in zip(ASSEMBLE, seen):
        print(f"log: {n} x {line!r}")
    if not all(seen):
        fails.append("no assemble-mode model load in trigger_thinq's log")
    luna(
        a.name,
        "tas/runTest",
        {"testNumber": 4, "triggerWord": "hiLG", "sensoryPoint": a.sensitivity, "subscribe": True},
    )
    time.sleep(2)  # the engine applies the operating point on its own thread

    status, _ = shell(a.name, f"cat > {PCM}", data=pcm)
    if status:
        print(f"FAIL: cannot write {PCM} in the VM")
        return 1
    params = json.dumps({"testNumber": 5, "inputPcm": PCM, "subscribe": True})
    run = (
        f"rm -f {RESULT} /tmp/hey-lg.out; luna-send -i {SERVICE}/tas/runTest '{params}' "
        "</dev/null >/tmp/hey-lg.out 2>&1 & p=$!; n=0; "
        f"while [ $n -lt {int(a.timeout)} ] && ! grep -q resultFile /tmp/hey-lg.out; do "
        "sleep 1; n=$((n + 1)); done; sleep 1; kill $p; cat /tmp/hey-lg.out; "
        f'echo "--- result"; cat {RESULT} 2>/dev/null; rm -f {PCM} /tmp/hey-lg.out'
    )
    _, out = shell(a.name, run, a.timeout + 30)
    answers, result = out.split("--- result", 1) if "--- result" in out else (out, "")
    progress = 0
    for line in answers.splitlines():
        try:
            progress = max(progress, int(json.loads(line).get("progress", 0)))
        except (ValueError, AttributeError):
            pass
    print(f"feed: {progress} %")
    if progress < 100:
        fails.append(f"the feed stopped at {progress} %")
    detections = []
    for line in result.split():
        begin, _, end = line.partition(",")
        if begin.isdigit() and end.isdigit():
            detections.append((int(begin), int(end)))
    for begin, end in detections:
        print(f"detection: samples {begin}-{end} ({(end - begin) / RATE:.3f} s)")
    print(f"detections: {len(detections)}")
    if not detections:
        fails.append(f"no detection in {RESULT}")
    luna(a.name, "tas/runTest", {"testNumber": 2, "status": "off", "subscribe": True})
    luna(a.name, "tas/endTest", {})  # out of LG's test mode
    print("PASS" if not fails else "FAIL: " + "; ".join(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
