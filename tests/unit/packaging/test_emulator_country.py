#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The common layer's emulator-country: the VM's country set through LG's settings once per value,
the smart service country where FirstUse's is stored, the mark once settings and LLS hold it."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from unittest import mock

TOP = Path(__file__).resolve().parents[3]
ROOTFS = TOP / "guest/layers/common/rootfs"
loader = importlib.machinery.SourceFileLoader(
    "emulator_country", str(ROOTFS / "usr/libexec/beanjar/emulator-country")
)
helper = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
dont_write, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ in the layer
loader.exec_module(helper)
sys.dont_write_bytecode = dont_write

LIST = (
    '{"countryList": [{"groupCode": "US", "countries": [{"alpha3": "USA", "shortName": "US"}]},'
    ' {"groupCode": "CA", "countries": [{"alpha3": "CAN", "shortName": "CA"}]}]}'
)


ZONES = [
    {"CountryCode": "US", "ZoneID": "America/New_York"},
    {"CountryCode": "CA", "ZoneID": "America/Vancouver"},
    {"CountryCode": "CA", "ZoneID": "America/Toronto"},
]


def run(tmp_path, country="CAN", mark=None, ssc="other", lls=None, reverts=0):
    """main() with the settings and LLS of a set whose country is USA (devicereset's: LLS follows a
    settings change unless lls says what it holds; reverts: that many checks see devicereset's
    first-boot pass put USA back); (exit status, the setSystemSettings payloads, the mark)."""
    tmp_path.mkdir(exist_ok=True)
    if country:
        (tmp_path / "country").write_text(country + "\n")
    if mark:
        (tmp_path / "mark").write_text(mark + "\n")
    (tmp_path / "countryList.json").write_text(LIST)
    state = {"country": "USA", "smartServiceCountryCode3": ssc, "reverts": reverts}
    written = []

    def call(uri, payload):
        method = uri.rsplit("/", 1)[1]
        if method == "setSystemSettings":
            written.append(payload["settings"])
            state.update(payload["settings"])
            return {"returnValue": True}
        if method == "getPreferenceValues":
            return {"timeZone": ZONES}
        if method == "getPreferences":
            return {"timeZone": ZONES[0]}  # the US zone FirstUse chose
        if method == "getSystemSettingValues":
            return {"defaultTimeZones": [{"countryCode": "CA", "defaultZoneId": "America/Toronto"}]}
        if method == "setPreferences":
            written.append(payload)
            return {"returnValue": True}
        if method == "getData":
            held = lls or state["country"]
            return {"dbgroups": [{"dbid": "factory", "items": {"countryName": held}}]}
        if payload["keys"] == ["country"] and state["reverts"]:
            state["reverts"] -= 1
            state["country"] = "USA"
        return {"settings": {k: state[k] for k in payload["keys"]}}

    with (
        mock.patch.multiple(
            helper,
            FILE=str(tmp_path / "country"),
            MARK=str(tmp_path / "mark"),
            LISTS=(str(tmp_path / "countryList.json"),),
            call=call,
            wait_country=mock.Mock(),
            log=mock.Mock(),
        ),
        mock.patch.object(helper.time, "sleep"),
        mock.patch.object(helper.subprocess, "call"),
    ):
        rc = helper.main()
    m = tmp_path / "mark"
    return rc, written, m.read_text() if m.exists() else None


def test_fresh_state(tmp_path):
    # before FirstUse's store: the country alone (emulator-firstuse stores the service country)
    assert run(tmp_path) == (0, [{"country": "CAN"}], "CAN\n")


def test_state_past_first_use(tmp_path):
    # a stored service country follows, with the alpha-2 of the firmware's list, and the time
    # zone as the settings app sets it: the country's default where the current is not its
    rc, written, mark = run(tmp_path, ssc="USA")
    assert (rc, mark) == (0, "CAN\n")
    assert written == [
        {"country": "CAN"},
        {"smartServiceCountryCode2": "CA", "smartServiceCountryCode3": "CAN"},
        {"timeZone": ZONES[2]},
        {"timeZone": ZONES[2]},
    ]


def test_once_per_value(tmp_path):
    # applied already: the TV's own later change stays; no file: nothing
    assert run(tmp_path / "a", mark="CAN") == (0, [], "CAN\n")
    assert run(tmp_path / "b", country=None) == (0, [], None)
    # a new value is applied over the mark of the old
    assert run(tmp_path / "c", mark="MEX")[2] == "CAN\n"


def test_set_again_after_devicereset(tmp_path):
    # devicereset's first pass puts LLS's country back over an early write: set again
    rc, written, mark = run(tmp_path, reverts=1)
    assert (rc, written, mark) == (0, [{"country": "CAN"}, {"country": "CAN"}], "CAN\n")


def test_lls_disagrees(tmp_path):
    # LLS never follows: no mark, the next boot tries again
    rc, written, mark = run(tmp_path, lls="USA")
    assert (rc, mark) == (1, None)
    assert written == [{"country": "CAN"}]


def test_unit():
    unit = (ROOTFS / "etc/systemd/system/emulator-country.service").read_text()
    assert "ConditionPathExists=/etc/emulator/country" in unit
    assert "After=settings-service.service" in unit
    wants = ROOTFS / "etc/systemd/system/settings-service.service.wants/emulator-country.service"
    assert wants.is_symlink() and str(wants.readlink()) == "../emulator-country.service"
