#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A stand-in for the manager's CLI in check-web.py: the commands beanjar-web runs, for one VM,
local/tv, whose display is tests/fakes/fake-qemu.py's in $FAKE_DIR, with the beanjar-vnc that
check-web.py runs there ($FAKE_VNC_PID). The pipes and the layout are the manager's own code
(beanjar/pipe.py), on a stand-in VM. Each call is appended to $FAKE_DIR/calls.log; a file
$FAKE_DIR/refuse makes vnc start fail with its text, as the manager refuses beside the viewer;
$FAKE_DIR/no-vms makes the list empty. Start/stop change state files only, never VMs;
action-hold delays completion, refuse-start/refuse-stop contain a refusal, and stop-message
contains successful stop diagnostics (including the manager's fallback message). A file reason
is the text local/tv runs for: in its list row, and its stop refused without --force. Of the VMs
that are off, k8hp-ombre has a kept thumbnail (screenshot --last), w22o-number1 none."""

import json
import os
import sys
import tempfile
import time

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
from beanjar import base, core, framing, pipe, remote_layouts  # noqa: E402

D = os.environ["FAKE_DIR"]
args = sys.argv[1:]
with open(os.path.join(D, "calls.log"), "a") as f:
    f.write(" ".join(args) + "\n")


class VM:
    name = "tv"
    dbus_sock = os.path.join(D, "dbus.sock")
    vnc_local_sock = os.path.join(D, "rfb-local.sock")
    audio_local_sock = os.path.join(D, "audio-local.sock")
    video_local_sock = os.path.join(D, "video-local.sock")

    def pid(self):
        return os.getpid()

    def vnc_pid(self):
        return int(os.environ["FAKE_VNC_PID"])

    def target(self):
        raise base.Error("no firmware here")

    def touch(self):
        return True  # a model with a touch screen: the page sends its touches

    run_dir = D  # run/rotation: the guest's rotation as rotate writes it here

    def portrait(self):
        return os.path.exists(os.path.join(D, "portrait"))  # a model that turns: check-viewer's


def fail(message):
    print(
        json.dumps({"error": message}) if "--json" in args else f"beanjar: {message}",
        file=sys.stderr,
    )
    sys.exit(1)


try:
    words = [w for w in args if w != "--json"]
    force = "--force" in words
    words = [w for w in words if w != "--force"]
    reason_file = os.path.join(D, "reason")
    reason = None
    if os.path.exists(reason_file):
        reason = {"text": open(reason_file).read().strip(), "set": "2026-11-06T10:00:00+0000",
                  "user": "me", "by": "check-web"}  # fmt: skip
    if words[:1] == ["list"] and os.path.exists(os.path.join(D, "no-vms")):
        print("[]")  # the empty state
    elif words[:1] == ["list"]:
        # local/tv is the one VM behind this stand-in; the others make the list look like a real
        # one (two hosts, mixed states)
        def vm(name, state, host=None, fw="05.06.10.01-HE_DTV_WT1H_AFAAABAA", platform="webOS 1",
               mem=2048, uptime=None, viewer=None, vnc=None, gpu=None, detail=""):  # fmt: skip
            state_file = os.path.join(D, f"state-{host or 'local'}-{name}")
            if os.path.exists(state_file):
                state = open(state_file).read()
            return {"name": name, "host": host, "state": state, "detail": detail, "platform": platform,
                    "firmware": fw, "display": "dbus", "mem": mem, "smp": 4, "uptime": uptime,
                    "pid": 1 if uptime else None, "viewer": viewer, "vnc": vnc,
                    "gpu": {"headline": gpu}, "library": "/private/lib",
                    "touch": name == "tv",
                    "portrait": (name == "tv" and VM().portrait()) or name == "k8hp-ombre",
                    "reason": reason if name == "tv" else None,
                    "tags": ["keep", "camera"] if name == "tv" else []}  # fmt: skip

        print(json.dumps([
            vm("tv", "Running", uptime=754.2, vnc=int(os.environ["FAKE_VNC_PID"])),
            vm("k8hp-ombre", "Off", fw="23.21.20.01-HE_DTV_W22H_AFABJAPU", platform="webOS 24"),
            vm("w22o-number1", "Off", fw="13.30.56.01-HE_DTV_W22O_AFABATPU", platform="webOS 23",
               mem=2560),
            vm("w22o-queue", "Running", "pi", "33.10.40.01-HE_DTV_W22O_AFABATPU", "webOS 25",
               uptime=8122, vnc=4321, gpu=12.4),
            vm("wt1h-afro", "Running", "pi", uptime=95000, viewer=999),
            vm("w19p", "Starting", "pi", "04.10.20.01-HE_DTV_W19P_AFADABAA", "webOS 4.5",
               mem=1536, uptime=12),
            vm("w16r", "Off", "pi", "03.30.82.01-HE_DTV_W16R_AFADABAA", "webOS 3"),
            vm("unavailable", "Error", "pi", platform="webOS 26", detail="Firmware directory is unavailable"),
        ]))  # fmt: skip
    elif len(words) == 2 and words[0] in ("start", "stop"):
        action, spec = words
        if spec not in ("local/tv", "pi/w16r"):
            fail(f"unknown VM {spec}")
        with open(os.path.join(D, "action-pids"), "a") as f:
            f.write(f"{os.getpid()}\n")
        refusal = os.path.join(D, f"refuse-{action}")
        if os.path.exists(refusal):
            fail(open(refusal).read().strip())
        if action == "stop" and spec == "local/tv" and reason and not force:
            fail(f"VM tv runs for a reason: {reason['text']}; stop it anyway with --force")
        state_file = os.path.join(D, "state-" + spec.replace("/", "-"))
        with open(state_file, "w") as f:
            f.write("Starting" if action == "start" else "Stopping")
        while os.path.exists(os.path.join(D, "action-hold")):
            time.sleep(0.05)
        with open(state_file, "w") as f:
            f.write("Running" if action == "start" else "Off")
        message = os.path.join(D, "stop-message")
        if action == "stop" and os.path.exists(message):
            print(open(message).read(), file=sys.stderr)
        print(json.dumps({"name": spec, "state": "running" if action == "start" else "off"}))
    elif words[:3] == ["vnc", "start", "local/tv"]:
        refuse = os.path.join(D, "refuse")
        if os.path.exists(refuse):
            fail(open(refuse).read().strip())
        fail(f"beanjar-vnc is already serving tv (pid {VM().vnc_pid()})")
    elif words == ["vnc", "pipe", "local/tv"]:
        sys.exit(pipe.vnc_pipe(VM()))
    elif words == ["vnc", "audio-pipe", "local/tv"]:
        sys.exit(pipe.audio_pipe(VM()))
    elif words == ["vnc", "video-pipe", "local/tv"]:
        sys.exit(pipe.video_pipe(VM()))
    elif words[:4] == ["sensor", "set", "rotate", "local/tv"] and len(words) == 5:
        # as the guest reports a new setting (rotation.rotate waits for that line)
        with open(os.path.join(D, "rotation"), "a") as f:
            f.write(f"rotation={words[4]}\n")
        print(
            json.dumps({"name": "tv", "rotation": int(words[4]), "previous": None, "changed": True})
        )
    elif words == ["sensor", "show", "rotate", "local/k8hp-ombre"]:
        # off: no report, the angle it was left at
        print(json.dumps({"name": "k8hp-ombre", "portrait": True, "rotation": None, "saved": 90}))
    elif words == ["sensor", "show", "rotate", "local/tv"]:
        # the status: the guest's last report (rotation.status), 0 before the first
        lines = open(os.path.join(D, "rotation")).read().split() if VM().portrait() else []
        angle = int(lines[-1].partition("=")[2]) if lines else 0
        print(json.dumps({"name": "tv", "portrait": VM().portrait(), "rotation": angle}))
    elif words == ["remote", "pipe", "local/tv"]:
        sys.exit(pipe.remote_pipe(VM()))
    elif words == ["remote", "layout", "local/tv"]:
        print(json.dumps(remote_layouts.as_json(pipe.vm_layout(VM()))))
    elif words[:2] == ["screenshot", "local/k8hp-ombre"] and words[2:] == ["-", "--last"]:
        # the thumbnail the manager kept when it stopped: a grey 384x216
        sys.stdout.buffer.write(framing.ppm_to_png(b"P6\n384 216\n255\n" + b"\x40" * 384 * 216 * 3))
    elif words[:2] == ["screenshot", "local/w22o-number1"] and words[2:] == ["-", "--last"]:
        fail("VM w22o-number1 has no thumbnail yet")
    elif words[:3] == ["screenshot", "local/tv", "-"]:
        width, height = (1280, 720)
        if words[3:]:
            if words[3:] != ["--frame", "384x216"]:
                fail(f"unexpected screenshot size: {words[3:]}")
            width, height = 384, 216
        if width == 384:
            with open(os.path.join(D, "thumbnail-pids"), "a") as f:
                f.write(f"{os.getpid()}\n")
            while os.path.exists(os.path.join(D, "thumbnail-hold")):
                time.sleep(0.05)
            if os.path.exists(os.path.join(D, "thumbnail-refuse")):
                fail("thumbnail capture refused")
        with tempfile.TemporaryDirectory() as tmp:
            ppm = os.path.join(tmp, "shot.ppm")
            core.Library._screenshot_ask(VM.dbus_sock, ppm, width, height, 10)
            sys.stdout.buffer.write(framing.ppm_to_png(open(ppm, "rb").read()))
    else:
        fail(f"fake-beanjar: unexpected {args}")
except base.Error as e:
    fail(str(e))
