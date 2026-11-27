#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The common layer's settings step (usr/libexec/beanjar/emulator-settings), run with a stand-in
luna-send: once per state and concern (the auto power-off timers, Audio Guidance), only the keys
the firmware defines and not already off, after the set's country; both init systems run it."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOP = Path(__file__).resolve().parents[3]
ROOTFS = TOP / "guest/layers/common/rootfs"
SCRIPT = ROOTFS / "usr/libexec/beanjar/emulator-settings"

# luna-send -n 1 URI PAYLOAD, answering from state.json as settingsservice and the hub do, and
# logging each call to calls.json
LUNA_SEND = """#!%s
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
state_path = os.path.join(here, "state.json")
state = json.load(open(state_path))
uri, payload = sys.argv[3], json.loads(sys.argv[4])
method = uri.rsplit("/", 1)[1]
with open(os.path.join(here, "calls.json"), "a") as log:
    log.write(json.dumps([method, payload]) + "\\n")
if state["unanswered"]:
    state["unanswered"] -= 1
    reply = {"returnValue": False, "errorCode": -1, "errorText": "Service does not exist"}
elif method == "setSystemSettings":
    if payload["category"] in state["refuse"]:
        json.dump(state, open(state_path, "w"))
        print(json.dumps({"returnValue": False, "errorText": "refused"}))
        sys.exit(0)
    state["have"].setdefault(payload["category"], {}).update(payload["settings"])
    reply = {"returnValue": True}
elif payload["category"] == "option" and payload["keys"] == ["country"]:
    countries = state["countries"]
    reply = {"returnValue": True, "settings": {"country": countries.pop(0) if len(countries) > 1 else countries[0]}}
else:
    stock = state["have"].get(payload["category"], {})
    found = dict((k, stock[k]) for k in payload["keys"] if k in stock)
    missing = [k for k in payload["keys"] if k not in stock]
    if not found:
        # WT1H's settings, none of the keys
        reply = {"errorText": "no return keys from DB", "category": payload["category"], "method": method, "returnValue": False}
    else:
        reply = {"returnValue": not missing, "settings": found}
        if missing:
            reply["errorKey"] = missing
json.dump(state, open(state_path, "w"))
print(json.dumps(reply, separators=(",", ":") if state["compact"] else (", ", ": ")))
"""

# W24P's stock values (defaultSettings.json, a North American country)
W24P = {
    "time": {
        "autoOff15Min": "on",
        "noSignalAutoOff": "on",
        "autoOff2HourOnTimer": "on",
        "automaticStandBy": "4",
        "dpmsAutoOff": "off",
        "autoClock": "on",
    },
    "timer": {"autoPowerOff2Hour": "on", "autoPowerOff": "off"},
    "option": {"audioGuidance": "on", "audioGuidanceSpeed": "fast"},
}
# WT1H's: no dpmsAutoOff, no timer autoPowerOff2Hour, audioGuidance "off" everywhere
WT1H = {
    "time": {
        "autoOff15Min": "on",
        "noSignalAutoOff": "on",
        "autoOff2HourOnTimer": "on",
        "automaticStandBy": "0",
    },
    "timer": {"autoPowerOff": "off"},
    "option": {"audioGuidance": "off"},
}
OFF = {
    "time": {
        "autoOff15Min": "off",
        "noSignalAutoOff": "off",
        "autoOff2HourOnTimer": "off",
        "automaticStandBy": "0",
        "dpmsAutoOff": "off",
    },
    "timer": {"autoPowerOff2Hour": "off"},
    "option": {"audioGuidance": "off"},
}
TIMERS_OFF = [
    {
        "category": "time",
        "settings": {
            "autoOff15Min": "off",
            "noSignalAutoOff": "off",
            "autoOff2HourOnTimer": "off",
            "automaticStandBy": "0",
        },
    },
    {"category": "timer", "settings": {"autoPowerOff2Hour": "off"}},
]
GUIDANCE_OFF = {"category": "option", "settings": {"audioGuidance": "off"}}
MARKS = {"emulator-autooff", "emulator-audioguidance"}


class TestEmulatorSettingsTest:
    shell = ["/bin/sh"]

    @pytest.fixture(autouse=True)
    def tmp_factory(self, tmp_path_factory):
        self.tmp_factory = tmp_path_factory

    def run_step(
        self, have, countries=("USA",), marked=(), unanswered=0, compact=True, refuse=(), status=0
    ):
        """The step on a firmware whose settings are have: (its writes, the marks after, have
        after)."""
        temporary = str(self.tmp_factory.mktemp("settings"))
        bin_dir = Path(temporary, "bin")
        bin_dir.mkdir()
        stub = bin_dir / "luna-send"
        stub.write_text(LUNA_SEND % sys.executable)
        stub.chmod(0o755)
        for name in ("sleep", "sync"):
            (bin_dir / name).write_text("#!/bin/sh\n")
            (bin_dir / name).chmod(0o755)
        state = {
            "have": json.loads(json.dumps(have)),
            "countries": list(countries),
            "unanswered": unanswered,
            "compact": compact,
            "refuse": list(refuse),
        }
        (bin_dir / "state.json").write_text(json.dumps(state))
        marks = Path(temporary, "preferences")
        marks.mkdir()
        for name in marked:
            (marks / name).touch()
        env = {
            "PATH": "%s:%s" % (bin_dir, os.defpath),
            "MARKDIR": str(marks),
            "WAIT": "20",
        }
        run = subprocess.run(self.shell + [str(SCRIPT)], env=env, stdout=subprocess.PIPE)
        assert run.returncode == status, run.stdout
        calls = bin_dir / "calls.json"
        lines = calls.read_text().splitlines() if calls.exists() else []
        written = [p for m, p in map(json.loads, lines) if m == "setSystemSettings"]
        after = json.loads((bin_dir / "state.json").read_text())["have"]
        return written, set(os.listdir(marks)), after

    @pytest.mark.parametrize("compact", [True, False], ids=["compact", "spaced"])
    def test_stock_settings_off(self, compact):
        written, marks, after = self.run_step(W24P, compact=compact)
        assert written == TIMERS_OFF + [GUIDANCE_OFF]
        assert marks == MARKS
        assert after["time"]["autoClock"] == "on"
        assert after["option"]["audioGuidanceSpeed"] == "fast"

    def test_keys_the_firmware_lacks_are_skipped(self):
        written, marks, after = self.run_step(WT1H)
        assert written == [
            {
                "category": "time",
                "settings": {
                    "autoOff15Min": "off",
                    "noSignalAutoOff": "off",
                    "autoOff2HourOnTimer": "off",
                },
            }
        ]
        assert marks == MARKS
        assert "autoPowerOff2Hour" not in after["timer"]
        no_guidance = dict(W24P, option={"audioGuidanceSpeed": "fast"})
        written, marks, after = self.run_step(no_guidance)
        assert written == TIMERS_OFF
        assert marks == MARKS
        assert "audioGuidance" not in after["option"]

    def test_already_off_and_marked(self):
        assert self.run_step(OFF)[:2] == ([], MARKS)
        assert self.run_step(W24P, marked=MARKS)[:2] == ([], MARKS)

    def test_each_concern_has_its_own_mark(self):
        # a state the power-off step ran on before Audio Guidance joined it
        written, marks, after = self.run_step(W24P, marked=["emulator-autooff"])
        assert written == [GUIDANCE_OFF]
        assert marks == MARKS
        assert after["time"]["autoOff15Min"] == "on"
        # Audio Guidance the user turned back on after its mark stays on
        written, marks, after = self.run_step(W24P, marked=["emulator-audioguidance"])
        assert written == TIMERS_OFF
        assert after["option"]["audioGuidance"] == "on"

    def test_a_refused_write_leaves_its_concern_for_the_next_boot(self):
        written, marks, after = self.run_step(W24P, refuse=["option"], status=1)
        assert written == TIMERS_OFF + [GUIDANCE_OFF]
        assert marks == {"emulator-autooff"}
        assert after["option"]["audioGuidance"] == "on"

    def test_waits_for_settings_and_country(self):
        written, marks, _ = self.run_step(W24P, countries=("other", "other", "CAN"), unanswered=3)
        assert len(written) == 3
        assert marks == MARKS

    def test_no_country_leaves_it_for_the_next_boot(self):
        assert self.run_step(W24P, countries=("other",))[:2] == ([], set())

    def test_no_settings_leaves_it_for_the_next_boot(self):
        assert self.run_step(W24P, unanswered=100)[:2] == ([], set())

    def test_jobs(self):
        job = (ROOTFS / "etc/init/emulator-settings.conf").read_text()
        assert "\nstart on settingsservice-ready\n" in job
        assert "\nexec /usr/libexec/beanjar/emulator-settings\n" in job
        unit = (ROOTFS / "etc/systemd/system/emulator-settings.service").read_text()
        assert "\nAfter=settings-service.service\n" in unit
        assert "\nExecStart=/usr/libexec/beanjar/emulator-settings\n" in unit
        wants = (
            ROOTFS / "etc/systemd/system/settings-service.service.wants/emulator-settings.service"
        )
        assert os.readlink(wants) == "../emulator-settings.service"
        assert os.access(SCRIPT, os.X_OK)


@pytest.mark.skipif(not shutil.which("busybox"), reason="no busybox")
class TestBusyboxEmulatorSettingsTest(TestEmulatorSettingsTest):
    # the firmware's shell is BusyBox ash
    shell = [str(shutil.which("busybox")), "sh"]
