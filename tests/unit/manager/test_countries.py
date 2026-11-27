#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The country setting (vm.toml firmware.country): the board's countries from the firmware's
configd RMM tables (countries.py), the vm.toml value and configure's check before it saves."""

import json
from unittest import mock

import pytest
from beanjar import base
from beanjar.vm import library, config, settings
from beanjar.board import countries, lls

# a firmware's RMM tables as W22O 13.30's have them, cut down: two blocks, grpCode per record
GROUPS = {
    "groupDVB": {
        "EU": {
            "countryList": [
                {"codeName": "GBR", "shortName": "GB"},
                {"codeName": "DEU", "shortName": "DE"},
                {"codeName": "___", "shortName": "__"},
            ]
        },
        "AJ": {"countryList": [{"codeName": "AUS", "shortName": "AU"}]},
    },
    "groupATSC": {
        "US": {
            "countryList": [
                {"codeName": "USA", "shortName": "US"},
                {"codeName": "CAN", "shortName": "CA"},
                {"codeName": "MEX", "shortName": "MX"},
            ]
        },
        "JP": {"countryList": [{"codeName": "JPN", "shortName": "JP"}]},
    },
}
CODES = {"EU": 4, "AJ": 19, "US": 2, "JP": 18}
TARGET = ("o22", "number1")


class FakeRoot:
    def __init__(self, files):
        self.files = files

    def read(self, path):
        data = self.files.get(path)
        return None if data is None else json.dumps(data).encode()


def files(grp_code=True):
    out = {countries.RMM_GROUPS: GROUPS}
    for block in GROUPS.values():
        for group, entry in block.items():
            for c in entry["countryList"]:
                record = {"codeName": c["codeName"], "grpCodeName": group}
                if grp_code:
                    record["grpCode"] = CODES[group]
                out[countries.RMM_RECORD.format(group=group, country=c["codeName"])] = record
    return out


@pytest.fixture
def firmware():
    """countries' firmware reads stand in: the board's countryGrp= (board_cmdline) and the paks."""
    state = {"grp": 2, "files": files()}
    with (
        mock.patch.object(
            lls,
            "board_cmdline",
            side_effect=lambda *a, **k: f"modelName=X countryGrp={state['grp']}",
        ),
        mock.patch.object(lls, "Root", side_effect=lambda *a, **k: FakeRoot(state["files"])),
    ):
        yield state


def test_board_group(firmware):
    """The board's group is the RMM group whose grpCode is its countryGrp=; its countries are that
    group's alpha-3 codes, sorted, without the "___" placeholder."""
    b = countries.board("fw", TARGET, broadcast="atsc")
    assert b.as_dict() == {
        "group": "US",
        "country_group": 2,
        "broadcast_block": "groupATSC",
        "countries": ["CAN", "MEX", "USA"],
    }
    firmware["grp"] = 4
    assert countries.board("fw", TARGET, broadcast="dvb").countries == ["DEU", "GBR"]
    firmware["grp"] = 18
    assert countries.board("fw", TARGET, broadcast="arib").group == "JP"  # ISDB in groupATSC


def test_check(firmware):
    """A country of the board's group passes; another is refused naming the board's countries, and
    where the firmware has it in another group, that group and its broadcast standard."""
    assert countries.check("fw", TARGET, "CAN", broadcast="atsc").group == "US"
    with pytest.raises(countries.Error) as e:
        countries.check("fw", TARGET, "GBR", broadcast="atsc")
    msg = str(e.value)
    assert "o22-number1 boots its atsc board, RMM group US (countryGrp=2): CAN, MEX, USA" in msg
    assert "GBR is in the firmware's group EU (dvb)" in msg
    assert "a dvb board first (broadcast=dvb" in msg
    firmware["grp"] = 4
    with pytest.raises(countries.Error, match=r"AUS is in the firmware's group AJ \(dvb\)$"):
        countries.check("fw", TARGET, "AUS", broadcast="dvb")  # same standard, another group
    with pytest.raises(countries.Error, match="the firmware's tables have no country 'ZZZ'"):
        countries.check("fw", TARGET, "ZZZ", broadcast="dvb")


def test_board_refusals(firmware):
    """Refused where the data does not say: no grpCode (before webOS 5), no RMM table, no group of
    the board's countryGrp=, a group in the other block, an Upstart release, not webOS."""
    firmware["files"] = files(grp_code=False)
    with pytest.raises(countries.Error, match="have no grpCode"):
        countries.board("fw", TARGET, broadcast="atsc")
    firmware["files"] = {}
    with pytest.raises(countries.Error, match="no country table"):
        countries.board("fw", TARGET, broadcast="atsc")
    firmware["files"] = files()
    firmware["grp"] = 99
    with pytest.raises(countries.Error, match="no RMM group of the firmware has the board's"):
        countries.board("fw", TARGET, broadcast="atsc")
    firmware["grp"] = 2
    with pytest.raises(countries.Error, match="is in the firmware's groupATSC block, not dvb's"):
        countries.board("fw", TARGET, broadcast="dvb")
    with pytest.raises(countries.Error, match="needs webOS 5 or later"):
        countries.board("fw", ("k5lp", "goldilocks2"))
    with pytest.raises(countries.Error, match="webOS only"):
        countries.board("fw", ("lm14", "netcast45"))


def test_parse():
    assert countries.parse(" can ") == "CAN"
    for bad in ("US", "CANA", "C4N", ""):
        with pytest.raises(countries.Error, match="alpha-3"):
            countries.parse(bad)


def test_vmconfig_country(tmp_path):
    """firmware.country: an upper-case alpha-3 string, the launcher's COUNTRY; not checked against
    the firmware on read (every read stays cheap: configure and the launcher check it)."""
    base_doc = {"firmware": {"directory": "firmware"}}
    assert "country" not in config.decode(base_doc)
    cfg = config.decode({"firmware": {"directory": "firmware", "country": "CAN"}})
    assert cfg["country"] == "CAN"
    assert settings.launcher_env(cfg)["COUNTRY"] == "CAN"
    for bad, why in (("can", "alpha-3 code in upper case"), (5, "must be a string")):
        with pytest.raises(base.Error, match=why):
            config.decode({"firmware": {"directory": "firmware", "country": bad}})
    assert "country" in settings.SETTING_HELP and settings.SETTING_HELP["country"][3]


@pytest.fixture
def vm(tmp_path):
    fw = tmp_path / "fw"
    fw.mkdir()
    (fw / "rootfs.pak").touch()
    (fw / "manifest.toml").write_text('name = "x"\nchip = "o22"\nrelease = "number1"\n')
    lib = library.Library(str(tmp_path / "lib"), repo=str(tmp_path))
    vm_dir = tmp_path / "lib" / "vms" / "tv"
    vm_dir.mkdir(parents=True)
    toml = vm_dir / "vm.toml"
    toml.write_text(f'[firmware]\ndirectory = "{fw}"\n')
    return lib, toml


def test_configure_country(vm):
    """configure upper-cases the value and checks it against the board of the VM's broadcast before
    it saves; a refusal writes nothing and names the key; "" unsets it; a change of broadcast checks
    the saved country again."""
    lib, toml = vm
    calls = []

    def check(fw, target, country, tree=None, broadcast=None, model=None):
        calls.append((target, country, broadcast))
        if not (country == "CAN" and broadcast == "atsc"):
            raise countries.Error(f"country {country!r} is not one of this board's: CAN, MEX, USA")

    with mock.patch.object(countries, "check", check):
        lib.configure("tv", prepare=False, country="can")
        assert lib.vm("tv").config["country"] == "CAN"
        assert 'country = "CAN"' in toml.read_text()
        assert calls == [(("o22", "number1"), "CAN", "atsc")]
        before = toml.read_text()
        with pytest.raises(base.Error, match="'GBR' is not one of this board's") as e:
            lib.configure("tv", prepare=False, country="gbr")
        assert e.value.key == "country" and toml.read_text() == before
        with pytest.raises(base.Error, match="alpha-3") as e:
            lib.configure("tv", prepare=False, country="UK")
        assert e.value.key == "country"
        with pytest.raises(base.Error, match="'CAN' is not one of this board's"):
            lib.configure("tv", prepare=False, broadcast="dvb")  # CAN is not the DVB board's
        assert toml.read_text() == before
        calls.clear()
        lib.configure("tv", prepare=False, mem=2048)  # nothing of the board changes: no check
        assert calls == []
        lib.configure("tv", prepare=False, country="")
        assert "country" not in lib.vm("tv").config
