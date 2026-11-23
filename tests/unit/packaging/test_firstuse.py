#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The shipped FirstUse unit chooses an available interpreter; subscriptions terminate; a state's
first boot records data-migrator's version instead of migrating."""

import importlib.machinery
import importlib.util
import io
from pathlib import Path
import shlex
import subprocess
from unittest import mock

import pytest

TOP = Path(__file__).resolve().parents[3]
LAYER = TOP / "guest/layers/normal"
loader = importlib.machinery.SourceFileLoader("firstuse", str(LAYER / "usr/sbin/emulator-firstuse"))
helper = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(helper)


@pytest.mark.parametrize(
    "available, expected",
    [
        (["python3", "python2"], "python3"),
        (["python3"], "python3"),
        (["python2"], "python2"),
    ],
    ids=["python3-and-python2", "python3-only", "python2-only"],
)
def test_unit_interpreter_selection(available, expected, tmp_path):
    unit = (LAYER / "etc/systemd/system/emulator-firstuse.service").read_text()
    command = shlex.split(
        next(line.partition("=")[2] for line in unit.splitlines() if line.startswith("ExecStart="))
    )
    for name in available:
        stub = tmp_path / name
        stub.write_text('#!/bin/sh\nprintf "%s %s\\n" "' + name + '" "$1"\n')
        stub.chmod(0o755)
    result = subprocess.run(
        command,
        env={"PATH": str(tmp_path)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert result.stdout.strip() == expected + " /usr/sbin/emulator-firstuse"


def test_marker_made_on_an_existing_state(tmp_path):
    # /var is the state's own partition, so the layer's ran-firstuse never reaches a state
    # that was first booted without the normal layer; the early unit's script makes it
    script = LAYER / "usr/sbin/emulator-ran-firstuse"
    marker = tmp_path / "var/luna/preferences/ran-firstuse"
    for existing in (False, True):
        if existing:
            marker.write_text("kept")
        subprocess.run(
            ["/bin/sh", str(script)],
            env={"PATH": "/usr/bin:/bin", "EMULATOR_ROOT": str(tmp_path)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        assert marker.exists()
        assert marker.read_text() == ("kept" if existing else "")
    unit = (LAYER / "etc/systemd/system/emulator-ran-firstuse.service").read_text()
    assert "Before=bootd.service" in unit
    wants = (
        LAYER / "etc/systemd/system/mount-readwrite.service.d/emulator-ran-firstuse.conf"
    ).read_text()
    assert "Wants=emulator-ran-firstuse.service" in wants


def test_marker_job_on_upstart():
    # upstart releases: a task job that holds bootd and utp back until the marker exists
    job = (LAYER / "etc/init/emulator-ran-firstuse.conf").read_text()
    lines = [ln.strip() for ln in job.splitlines() if not ln.startswith("#")]
    assert "start on starting bootd or starting utp" in lines
    assert "task" in lines
    assert "exec /usr/sbin/emulator-ran-firstuse" in lines


# W23P's /etc/palm/defaultTimeZone.json (an excerpt) and systemservice's AU zones
DEFAULTS = {
    "returnValue": True,
    "defaultTimeZones": [
        {"countryCode": "RU", "country": "Russia", "defaultZoneId": "Europe/Moscow"},
        {"countryCode": "AU", "country": "Australia", "defaultZoneId": "Australia/Sydney"},
    ],
}
AU = [
    {"CountryCode": "AU", "ZoneID": "Australia/Perth", "City": "Perth"},
    {"CountryCode": "AU", "ZoneID": "Australia/Adelaide", "City": "Adelaide"},
    {"CountryCode": "AU", "ZoneID": "Australia/Sydney", "City": "Sydney"},
]


def test_default_zone_of_several():
    with mock.patch.object(helper, "call", return_value=DEFAULTS) as call:
        assert helper.default_zone("AU", AU) == AU[2]
    call.assert_called_once_with(
        "luna://com.webos.settingsservice/getSystemSettingValues",
        {"category": "time", "key": "defaultTimeZone"},
    )
    # a country the table leaves out, or a default the list lacks: its first zone
    nz = [dict(z, CountryCode="NZ") for z in AU]
    with mock.patch.object(helper, "call", return_value=DEFAULTS):
        assert helper.default_zone("NZ", nz) == nz[0]
        assert helper.default_zone("AU", AU[:2]) == AU[0]
    # a firmware without the table: no guess
    with (
        mock.patch.object(helper, "call", return_value={"returnValue": True}),
        pytest.raises(RuntimeError, match="no defaultTimeZones"),
    ):
        helper.default_zone("AU", AU)


def run_main(current, tmp_path):
    """main() on an AU set whose current zone is current; the zone it writes."""
    replies = {
        "getSystemSettings": {
            "settings": {
                "country": "AUS",
                "localeCountryGroup": "AJ",
                "japanCitySelection": "",
                "deviceName": "[LG] webOS TV",
            }
        },
        "getSystemSettingValues": None,  # by key below
        "getFactoryOpt": {"modelName": "OLED55C3PSA"},
        "getPreferenceValues": {"timeZone": AU + [{"CountryCode": "NZ", "ZoneID": "x"}]},
        "getPreferences": {"timeZone": current},
        "setSystemSettings": {},
        "setPreferences": {},
    }
    written = {}

    def call(uri, payload):
        method = uri.rsplit("/", 1)[1]
        if method == "getSystemSettingValues":
            if payload.get("key") == "country":
                return {"countryList": [{"countries": [{"alpha3": "AUS", "shortName": "AU"}]}]}
            return DEFAULTS
        if method in ("setPreferences", "setSystemSettings"):
            written.setdefault(method, []).append(payload)
        return replies[method]

    tmp_path.mkdir()
    temporary = str(tmp_path)
    with (
        mock.patch.multiple(
            helper,
            MARK=temporary + "/mark",
            LASTINPUT_MARK=temporary + "/lastinput",
            OOBE=temporary + "/no-oobe",
            call=call,
            first=mock.Mock(),
            log=mock.Mock(),
        ),
        mock.patch.object(helper.subprocess, "call"),
    ):
        assert helper.main() == 0
        assert Path(temporary, "mark").exists()
    time = [
        p["settings"]["timeZone"] for p in written["setSystemSettings"] if p["category"] == "time"
    ]
    return time[0], written.get("setPreferences", [])


def test_first_use_of_several_zones(tmp_path):
    # the current zone is not the country's: its default, written to both
    zone, prefs = run_main({"ZoneID": "Etc/UTC"}, tmp_path / "a")
    assert (zone, prefs) == (AU[2], [{"timeZone": AU[2]}])
    # one of the country's already chosen stands
    zone, prefs = run_main(AU[0], tmp_path / "b")
    assert (zone, prefs) == (AU[0], [])


def last_input(current, tmp_path, oobe=True, marked=False):
    """last_input() with lastInputApp current; the settings it writes."""
    written = []

    def call(uri, payload):
        method = uri.rsplit("/", 1)[1]
        if method == "setSystemSettings":
            written.append(payload)
            return {}
        assert payload == {"category": "general", "keys": ["lastInputApp"]}
        return {"settings": {"lastInputApp": current}}

    tmp_path.mkdir()
    temporary = str(tmp_path)
    with (
        mock.patch.multiple(
            helper,
            LASTINPUT_MARK=temporary + "/lastinput",
            OOBE=temporary + "/oobe",
            call=call,
            log=mock.Mock(),
        ),
        mock.patch.object(helper.subprocess, "call"),
    ):
        if oobe:
            Path(temporary, "oobe").mkdir()
        if marked:
            Path(temporary, "lastinput").touch()
        helper.last_input()
        assert Path(temporary, "lastinput").exists()
    return written


def test_last_input(tmp_path):
    # oobe's write: only over the stock "none", once, and only where oobe ships
    livetv = {
        "lastInputApp": "com.webos.app.livetv",
        "physicalLastInputApp": "com.webos.app.livetv",
    }
    assert last_input("none", tmp_path / "a") == [{"category": "general", "settings": livetv}]
    assert last_input("com.webos.app.hdmi1", tmp_path / "b") == []
    assert last_input("none", tmp_path / "c", marked=True) == []
    assert last_input("none", tmp_path / "d", oobe=False) == []


def test_subscription_eof():
    process = mock.Mock(stdout=io.BytesIO(b'{"settings":{"country":"other"}}\n'))
    with mock.patch.object(helper.subprocess, "Popen", return_value=process):
        with pytest.raises(RuntimeError, match="ended"):
            helper.first("luna://test", {}, lambda reply: reply["settings"]["country"] != "other")
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with()


def test_subscription_matching_reply():
    process = mock.Mock(stdout=io.BytesIO(b'{"ready":false}\n{"ready":true}\n'))
    with mock.patch.object(helper.subprocess, "Popen", return_value=process):
        assert helper.first("luna://test", {}, lambda reply: reply["ready"]) == {"ready": True}
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with()


HUB = "luna://com.webos.service.bus/signal/registerServiceCategory"


def migrator_env(root, hub_replies, step=1, readiness=True):
    """Stubs for emulator-data-migrator under EMULATOR_ROOT ROOT: a data-migrator binary with (or,
    READINESS false, without) its surfacemanager readiness item; luna-send answers the hub's
    method-list call with HUB_REPLIES in turn (the last one repeated) and logs every other call;
    date advances STEP seconds per call; sleep returns at once."""
    bin_dir, calls = root / "bin", root / "calls"
    bin_dir.mkdir()
    binary = root / "usr/sbin/data-migrator"
    binary.parent.mkdir(parents=True)
    item = b"/signal/registerServiceCategory\0" if readiness else b""
    binary.write_bytes(b"\x7fELF\0" + item + b"showSpinner\0")
    replies = root / "replies"
    replies.write_text("\n".join(hub_replies) + "\n")
    stubs = {
        "luna-send": f"""case "$*" in
*{HUB}*)
\techo hub >> "{calls}"
\thead -n 1 "{replies}"
\t[ "$(wc -l < "{replies}")" -gt 1 ] && sed -i 1d "{replies}"
\texit 0 ;;
esac
echo "luna-send $*" >> "{calls}"
""",
        "logger": f'echo "logger $*" >> "{calls}"\n',
        "sleep": "exit 0\n",
        "date": f'n=$(cat "{root}/now" 2>/dev/null || echo 1000)\necho $((n + {step})) > "{root}/now"\necho "$n"\n',
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\n" + body)
        stub.chmod(0o755)
    stock = root / "etc/systemd/system/scripts/data-migrator.sh"
    stock.parent.mkdir(parents=True)
    stock.write_text(f'#!/bin/sh\necho "stock" >> "{calls}"\n')
    stock.chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin", "EMULATOR_ROOT": str(root)}, calls


def test_data_migrator_first_boot(tmp_path):
    """Without platform_upgrade/version (a blank state), data-migrator.service records the
    version (writeVersion) and runs no /run, as FirstUse's boot does on a set; with it, the stock
    script runs, untouched (a VM that keeps its state). Both start only once the hub lists
    surfacemanager's showSpinner and the list has stopped changing."""
    dropin = (
        LAYER / "etc/systemd/system/data-migrator.service.d/emulator-firstuse.conf"
    ).read_text()
    starts = [ln.partition("=")[2] for ln in dropin.splitlines() if ln.startswith("ExecStart=")]
    assert starts == ["", "/usr/sbin/emulator-data-migrator"]
    script = LAYER / "usr/sbin/emulator-data-migrator"
    replies = ["{}", '{"/":["a"]}', '{"/":["a","showSpinner"]}', '{"/":["a","showSpinner","b"]}']
    env, calls = migrator_env(tmp_path, replies)

    subprocess.run(["sh", str(script)], env=env, check=True)
    lines = calls.read_text().splitlines()
    # the list changes three times, then stays the same for two more polls
    assert lines[:6] == ["hub"] * 6
    assert lines[6].startswith("logger ") and "settled after" in lines[6]
    assert lines[7] == "luna-send -n 1 -f luna://com.webos.service.datamigrator/writeVersion {}"
    assert lines[8].startswith("logger ")
    assert "stock" not in lines

    calls.unlink()
    version = tmp_path / "mnt/lg/cmn_data/platform_upgrade/version"
    version.parent.mkdir(parents=True)
    version.write_text('{"previous_version":"11","current_version":"11"}')
    subprocess.run(["sh", str(script)], env=env, check=True)
    lines = calls.read_text().splitlines()
    assert lines[-1] == "stock"
    assert "hub" in lines and "settled after" in lines[-2]


def test_data_migrator_wait_times_out(tmp_path):
    """A surfacemanager that never lists showSpinner (or a hub that answers {}) delays
    data-migrator by the bounded wait only; then it starts as before."""
    script = LAYER / "usr/sbin/emulator-data-migrator"
    env, calls = migrator_env(tmp_path, ["{}"], step=10)
    subprocess.run(["sh", str(script)], env=env, check=True)
    lines = calls.read_text().splitlines()
    polls = lines.count("hub")
    assert 5 < polls <= 13
    assert "not settled after 120 s" in lines[polls]
    assert lines[polls + 1].endswith("/writeVersion {}")


@pytest.mark.parametrize(
    "readiness, replies, logged",
    [
        (False, ['{"/":["showSpinner"]}'], None),
        (
            True,
            ['{"returnValue": false, "errorCode": 1, "errorText": "Invalid method"}'],
            "refused",
        ),
    ],
)
def test_data_migrator_no_wait(tmp_path, readiness, replies, logged):
    """A data-migrator without the readiness item, or a hub that refuses the method list, starts at
    once: no polling beyond that one answer."""
    script = LAYER / "usr/sbin/emulator-data-migrator"
    env, calls = migrator_env(tmp_path, replies, readiness=readiness)
    subprocess.run(["sh", str(script)], env=env, check=True)
    lines = calls.read_text().splitlines()
    assert lines.count("hub") == (1 if logged else 0)
    if logged:
        assert logged in lines[1]
    assert any(ln.endswith("/writeVersion {}") for ln in lines)
