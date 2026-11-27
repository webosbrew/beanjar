#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The shipped FirstUse unit chooses an available interpreter; subscriptions terminate; a state's
first boot records data-migrator's version instead of migrating."""

import importlib.machinery
import importlib.util
import io
import shlex
import subprocess
from pathlib import Path
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


def test_no_marker_shipped():
    # a new state boots in FirstUse as a new set does; bootd writes ran-firstuse on firstUseDone
    assert not (LAYER / "var/luna/preferences/ran-firstuse").exists()
    assert not list(LAYER.rglob("emulator-ran-firstuse*"))


def done_env(
    root, country="GBR", service="GBR", minimal=True, writes=True, init="systemd", migrator="exited"
):
    """Stubs for emulator-firstuse-done under EMULATOR_ROOT ROOT: luna-send answers settings'
    country and smart service country, bootd's status (minimal-boot-done as MINIMAL) and, for
    firstUseDone as com.palm.app.firstuse, writes ran-firstuse as bootd does; every call is
    logged. INIT: systemd (emulator-firstuse and its marks, WRITES; systemctl: webos-mbd.target
    active, data-migrator-init.service MIGRATOR: exited, running or absent) or upstart (with
    emulator-service-country)."""
    prefs = root / "var/luna/preferences"
    prefs.mkdir(parents=True)
    bin_dir, calls = root / "bin", root / "calls"
    bin_dir.mkdir()
    if init == "systemd":
        (root / "run/systemd/system").mkdir(parents=True)
        (root / "usr/sbin").mkdir(parents=True)
        (root / "usr/sbin/emulator-firstuse").write_text("")
        if writes:
            (prefs / "emulator-firstuse").write_text("")
            (prefs / "emulator-firstuse-lastinput").write_text("")
    else:
        (root / "etc/init").mkdir(parents=True)
        (root / "etc/init/emulator-service-country.conf").write_text("")
    flag = "true" if minimal else "false"
    stubs = {
        "luna-send": f"""echo "luna-send $*" >> "{calls}"
case "$*" in
*'"country"'*) echo '{{"returnValue": true, "settings": {{"country": "{country}"}}}}' ;;
*smartServiceCountryCode3*) echo '{{"returnValue":true,"settings":{{"smartServiceCountryCode3":"{service}"}}}}' ;;
*getBootStatus*) echo '{{"returnValue":true,"signals":{{"minimal-boot-done": {flag}}}}}' ;;
*'-a com.palm.app.firstuse'*firstUseDone*) : > "{prefs}/ran-firstuse"; echo '{{"returnValue":true}}' ;;
esac
""",
        "systemctl": f"""echo "systemctl $*" >> "{calls}"
case "$*" in
*webos-mbd.target*) printf 'LoadState=loaded\nActiveState=active\n' ;;
*data-migrator-init*)
\tcase {migrator} in
\texited) printf 'LoadState=loaded\nActiveState=inactive\nExecMainExitTimestampMonotonic=146905442\n' ;;
\trunning) printf 'LoadState=loaded\nActiveState=active\nExecMainExitTimestampMonotonic=0\n' ;;
\t*) printf 'LoadState=not-found\n' ;;
\tesac ;;
esac
""",
        "logger": "exit 0\n",
        "sleep": "exit 0\n",
        "sync": "exit 0\n",
        "date": f'n=$(cat "{root}/now" 2>/dev/null || echo 1000)\necho $((n + 100)) > "{root}/now"\necho "$n"\n',
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\n" + body)
        stub.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "EMULATOR_ROOT": str(root)}
    return env, calls, prefs


def run_done(env):
    return subprocess.run(
        ["sh", str(LAYER / "usr/sbin/emulator-firstuse-done")],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


@pytest.mark.parametrize("init", ["systemd", "upstart"])
def test_done_calls_first_use_done(tmp_path, init):
    # country, FirstUse's writes and minimal-boot-done there: the call, as the FirstUse app
    env, calls, prefs = done_env(tmp_path, init=init)
    result = run_done(env)
    assert result.returncode == 0, result.stdout
    assert (prefs / "ran-firstuse").exists()
    lines = calls.read_text().splitlines()
    assert lines[-1] == (
        "luna-send -a com.palm.app.firstuse -n 1 -w 180000 "
        "luna://com.webos.bootManager/firstUseDone {}"
    )
    assert any("getBootStatus" in ln for ln in lines[:-1])
    # systemd: data-migrator-init.service, which reads ran-firstuse, has run first
    assert any("data-migrator-init" in ln for ln in lines) == (init == "systemd")
    assert any(("smartServiceCountryCode3" in ln) == (init == "upstart") for ln in lines)
    assert "FirstUse done" in result.stdout


def test_done_leaves_a_finished_state_alone(tmp_path):
    env, calls, prefs = done_env(tmp_path)
    (prefs / "ran-firstuse").write_text("kept")
    assert run_done(env).returncode == 0
    assert not calls.exists()
    assert (prefs / "ran-firstuse").read_text() == "kept"


@pytest.mark.parametrize(
    "missing, kw",
    [
        ("country in settings", {"country": "other"}),
        ("emulator-firstuse marks", {"writes": False}),
        ("smart service country", {"service": "other", "init": "upstart"}),
        ("webos-mbd.target and data-migrator-init", {"migrator": "running"}),
    ],
)
def test_done_waits_bounded(tmp_path, missing, kw):
    # each wait ends after EMULATOR_FIRSTUSE_WAIT; the call is made all the same
    env, calls, prefs = done_env(tmp_path, **kw)
    env["EMULATOR_FIRSTUSE_WAIT"] = "900"
    result = run_done(env)
    assert f"no {missing} within 900 s" in result.stdout
    assert calls.read_text().splitlines()[-1].endswith("firstUseDone {}")
    assert (prefs / "ran-firstuse").exists()


def test_done_waits_for_minimal_boot_done(tmp_path):
    # never called before minimal-boot-done (webOS 5 on: bootd would drop boot-done for good)
    env, calls, prefs = done_env(tmp_path, minimal=False)
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            ["sh", str(LAYER / "usr/sbin/emulator-firstuse-done")],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    assert not (prefs / "ran-firstuse").exists()
    assert "getBootStatus" in calls.read_text()


def test_done_wiring():
    unit = (LAYER / "etc/systemd/system/emulator-firstuse-done.service").read_text()
    assert "ConditionPathExists=!/var/luna/preferences/ran-firstuse" in unit
    assert "ExecStart=/usr/sbin/emulator-firstuse-done" in unit
    assert "After=settings-service.service" in unit
    wants = (
        LAYER / "etc/systemd/system/settings-service.service.d/emulator-firstuse.conf"
    ).read_text()
    assert "Wants=emulator-firstuse.service emulator-firstuse-done.service" in wants
    job = (LAYER / "etc/init/emulator-firstuse-done.conf").read_text()
    lines = [ln.strip() for ln in job.splitlines() if not ln.startswith("#")]
    assert "start on started bootd" in lines
    assert "exec /usr/sbin/emulator-firstuse-done" in lines


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


def run_main(current, tmp_path, country=None, written=None):
    """main() on an AU set whose current zone is current, the VM's country (etc/emulator/country)
    country; the zone it writes."""
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
    written = {} if written is None else written

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
    if country:
        Path(temporary, "country").write_text(country + "\n")
    # the firmware's full list (/etc/palm/countryList.json), of which settings answers the board's group
    Path(temporary, "countryList.json").write_text(
        '{"countryList": [{"groupCode": "AJ", "countries": [{"alpha3": "AUS", "shortName": "AU"},'
        ' {"alpha3": "NZL", "shortName": "NZ"}]}]}'
    )
    with (
        mock.patch.multiple(
            helper,
            MARK=temporary + "/mark",
            LASTINPUT_MARK=temporary + "/lastinput",
            OOBE=temporary + "/no-oobe",
            COUNTRY=temporary + "/country",
            COUNTRY_LIST=temporary + "/countryList.json",
            call=call,
            first=mock.Mock(),
            log=mock.Mock(),
            keep_service_country=mock.Mock(),
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


def test_first_use_of_the_vm_country(tmp_path):
    # the VM's country (vm.toml firmware.country), which settings' list for the board's group does
    # not name: FirstUse's smart service country and time zone are its, from the full list
    written = {}
    zone, _ = run_main({"ZoneID": "Etc/UTC"}, tmp_path / "a", country="NZL", written=written)
    assert zone == {"CountryCode": "NZ", "ZoneID": "x"}
    assert {
        "category": "option",
        "settings": {"smartServiceCountryCode2": "NZ", "smartServiceCountryCode3": "NZL"},
    } in written["setSystemSettings"]


def service_country_first(stored, vm=None, lls=None, default=None, tmp_path=None):
    """service_country_first() with smartServiceCountryCode3 stored, the VM's country vm, LLS's
    countryName lls and configd's system.defaultCountry default; what it writes."""
    written = []

    def call(uri, payload):
        method = uri.rsplit("/", 1)[1]
        if method == "getSystemSettings":
            return {"settings": {"smartServiceCountryCode3": stored}}
        if method == "setSystemSettings":
            written.append(payload["settings"])
            return {}
        if method == "getData":
            return {"dbgroups": [{"dbid": "factory", "items": {"countryName": lls}}]}
        if method == "getConfigs":
            return {"configs": {"system.defaultCountry": default} if default else {}}
        raise AssertionError(uri)

    lst = tmp_path / "countryList.json"
    lst.write_text(
        '{"countryList": [{"groupCode": "US", "countries": [{"alpha3": "USA", "shortName": "US"},'
        ' {"alpha3": "CAN", "shortName": "CA"}, {"alpha3": "MEX", "shortName": "MX"}]}]}'
    )
    with mock.patch.multiple(
        helper,
        call=call,
        log=mock.Mock(),
        vm_country=mock.Mock(return_value=vm),
        COUNTRY_LIST=str(lst),
    ):
        helper.service_country_first()
    return written


def test_service_country_first(tmp_path):
    # stored before settings' own country, from what devicereset will choose: the VM's, else
    # LLS's factory countryName (not "---"), else configd's default
    us = {"smartServiceCountryCode2": "US", "smartServiceCountryCode3": "USA"}
    ca = {"smartServiceCountryCode2": "CA", "smartServiceCountryCode3": "CAN"}
    mx = {"smartServiceCountryCode2": "MX", "smartServiceCountryCode3": "MEX"}
    assert service_country_first("other", "CAN", "MEX", "USA", tmp_path=tmp_path) == [ca]
    assert service_country_first("other", None, "MEX", "USA", tmp_path=tmp_path) == [mx]
    assert service_country_first("other", None, "---", "USA", tmp_path=tmp_path) == [us]
    # over sdx's Others row as over the stock "other"
    assert service_country_first("US7", None, "---", "USA", tmp_path=tmp_path) == [us]
    # a stored country stays; no country known yet: nothing
    assert service_country_first("CAN", None, None, "USA", tmp_path=tmp_path) == []
    assert service_country_first("other", None, "---", None, tmp_path=tmp_path) == []


def keep(stored, cache, sdx, tmp_path, wait=30):
    """keep_service_country("US", "USA") where settings answers the stored values in turn (the
    last repeated), the file cache holds cache and sdx started at sdx (uptime, None: not
    running; uptime is 100); what it writes and how many polls it made."""
    written, polls = [], []
    stored = list(stored)

    def settings(category, keys):
        polls.append(1)
        value = stored.pop(0) if len(stored) > 1 else stored[0]
        return {"smartServiceCountryCode3": value}

    def set_settings(category, values):
        written.append(values)

    clock = [0.0]

    def sleep(s):
        clock[0] += s

    with (
        mock.patch.multiple(
            helper,
            settings=settings,
            set_settings=set_settings,
            log=mock.Mock(),
            cached_country=mock.Mock(return_value=cache),
            sdx_start=mock.Mock(return_value=sdx),
            uptime=mock.Mock(return_value=100.0),
            KEEP_WAIT=wait,
        ),
        mock.patch.object(helper.time, "sleep", sleep),
        mock.patch.object(helper.time, "time", lambda: clock[0]),
    ):
        helper.keep_service_country("US", "USA")
    return written, len(polls)


def test_keep_service_country(tmp_path):
    us = {"smartServiceCountryCode2": "US", "smartServiceCountryCode3": "USA"}
    # sdx's first-run Others row landing after the country: stored again, once
    assert keep(["USA", "USA", "US7"], "OTHER", 50.0, tmp_path) == ([us], 3)
    # sdx started after the file cache held the country (it read it), or does not run yet: done
    assert keep(["USA"], "USA", 150.0, tmp_path) == ([], 1)
    assert keep(["USA"], "USA", None, tmp_path) == ([], 1)
    # sdx started before: it may still store its row; waited for, up to KEEP_WAIT
    assert keep(["USA"], "USA", 50.0, tmp_path, wait=30) == ([], 6)


def test_sdx_start(tmp_path):
    # field 22 of /proc/<pid>/stat over the clock tick; a command with ") " in its name is safe
    proc = tmp_path / "proc"
    for pid, comm, start in (("1", "init", 5), ("42", "sdx", 4200), ("7", "a) b", 9)):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "comm").write_text(comm + "\n")
        fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 10
        (proc / pid / "stat").write_text("%s (%s) %s\n" % (pid, comm, " ".join(fields)))
    (proc / "self").mkdir()
    real_listdir, real_open = helper.os.listdir, open

    def listdir(path):
        return real_listdir(str(proc)) if path == "/proc" else real_listdir(path)

    def fake_open(path, *a, **k):
        if str(path).startswith("/proc/"):
            path = str(proc) + str(path)[len("/proc") :]
        return real_open(path, *a, **k)

    with (
        mock.patch.object(helper.os, "listdir", listdir),
        mock.patch("builtins.open", fake_open),
        mock.patch.object(helper.os, "sysconf", return_value=100),
    ):
        assert helper.sdx_start() == 42.0


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
    """Without platform_upgrade/version (a blank state started in Normal by the headless layer),
    data-migrator.service records the version (writeVersion) and runs no /run, as FirstUse's boot
    does on a set; with it, the stock script runs, untouched (a VM that keeps its state). Both
    start only once the hub lists surfacemanager's showSpinner and the list has stopped changing."""
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


HEADLESS = TOP / "guest/layers/headless/rootfs"


@pytest.mark.parametrize("normal, existing", [(True, False), (True, True), (False, False)])
def test_headless_marker(tmp_path, normal, existing):
    # headless with the normal layer: ran-firstuse before bootd (FirstUse's UI cannot run there);
    # without the normal layer the set stays in FirstUse; an existing marker is kept
    marker = tmp_path / "var/luna/preferences/ran-firstuse"
    if normal:
        (tmp_path / "usr/sbin").mkdir(parents=True)
        (tmp_path / "usr/sbin/emulator-firstuse-done").write_text("")
    if existing:
        marker.parent.mkdir(parents=True)
        marker.write_text("kept")
    subprocess.run(
        ["/bin/sh", str(HEADLESS / "usr/sbin/emulator-headless-firstuse")],
        env={"PATH": "/usr/bin:/bin", "EMULATOR_ROOT": str(tmp_path)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    assert marker.exists() == normal
    if normal:
        assert marker.read_text() == ("kept" if existing else "")


def test_headless_marker_wiring():
    unit = (HEADLESS / "etc/systemd/system/emulator-headless-firstuse.service").read_text()
    assert "After=mount-readwrite.service" in unit and "Before=bootd.service" in unit
    wants = (
        HEADLESS / "etc/systemd/system/mount-readwrite.service.d/emulator-headless-firstuse.conf"
    ).read_text()
    assert "Wants=emulator-headless-firstuse.service" in wants
    job = (HEADLESS / "etc/init/emulator-headless-firstuse.conf").read_text()
    lines = [ln.strip() for ln in job.splitlines() if not ln.startswith("#")]
    assert "start on starting bootd or starting utp" in lines
    assert "task" in lines
    assert "exec /usr/sbin/emulator-headless-firstuse" in lines
