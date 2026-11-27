#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The bring-up checklist's judging (tests/regress/lib/checklist.py) without a VM: facts as
tests/regress/guest.sh's checklist step prints them (`meson test -C build/host --suite regress`,
or `pytest tests/unit/regress/test_checklist.py`)."""

import os
import sys
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "regress", "lib")
)
import checklist  # noqa: E402

# A healthy W16R (m2-dreadlocks) as the guest reported it on the Pi
GOOD = """\
kernel=6.18.53
tainted=4096
module=lgemu loaded 6.18.53
module=mstar-utopia loaded 6.18.53
dmesg=init: pulseaudio main process (2047) terminated with status 1
sysinfo={ "returnValue": true, "modelName": "43LH604V-ZB", "tvSystemName": "dvb", "contiLangCountry": "EU", "boardType": "M2_DVB_EU" }
settings={ "settings": { "country": "GBR", "countryGroup": "EU", "smartServiceCountryCode2": "EU7", "smartServiceCountryCode3": "EU7" }, "returnValue": true }
bootstatus={"firstAppLaunched":true,"returnValue":true,"bootStatus":"normal","firstAppId":"com.webos.app.livetv"}
sysprops=[ { "com.palm.properties.machineName": "m2" }, { "com.palm.properties.storageCapacity": "3833593856" }, { "com.palm.properties.storageFreeSpace": "1919180800" } ]
tvconfig_continentname=DE
tvconfig_rmmgroup=EU
tvconfig_rmmcountry=GBR
cmdline_model=43LH604V-ZB
lastinput=0x1b48 01001320
mrw_status=mount-readwrite stop/waiting
mrw_flag=/tmp/mount-readwrite.conf.done yes
pm_freeze_timeout=none 20000
preload=unused yes
core_pattern=core
CHECKLIST FACTS END
"""


def edit(text, **changes):
    """GOOD with lines replaced (name=None drops every line of that name; a list adds several)."""
    out = []
    for line in text.splitlines():
        name = line.partition("=")[0]
        if name in changes:
            continue
        out.append(line)
    for name, v in changes.items():
        for value in [] if v is None else v if isinstance(v, list) else [v]:
            out.insert(-1, f"{name}={value}")
    return "\n".join(out) + "\n"


def items(text, broadcast="dvb"):
    return {i: (s, why) for i, s, why in checklist.judge(text, broadcast)}


def test_from_paks():
    assert (
        checklist.targets.broadcast_of_paks(["rootfs.pak", "tvservice-dvb.pak", "otncabi-dvb.pak"])
        == "dvb"
    )
    assert (
        checklist.targets.broadcast_of_paks(["bsppart-arib.pak", "otncabi-arib.pak", "fonts.pak"])
        == "arib"
    )
    assert (
        checklist.targets.broadcast_of_paks(["tvservice-dvb", "rootfs"]) == "dvb"
    )  # targets.pak_files
    assert checklist.targets.broadcast_of_paks(["rootfs.pak", "fonts.pak"]) is None
    assert checklist.targets.broadcast_of_paks(["a-dvb.pak", "b-atsc.pak"]) is None


def test_from_firmware_dir(tmp_path):
    for n in ("rootfs.pak", "otycabi-atsc.pak", "notes-dvb.txt"):
        (tmp_path / n).touch()
    assert checklist.broadcast_of_firmware(tmp_path, ("tvservice-dvb",)) == "atsc"
    assert checklist.broadcast_of_firmware("/nonexistent", ("tvservice-dvb",)) == "dvb"
    assert checklist.broadcast_of_firmware(None, ("tvservice-dvb",)) == "dvb"
    # the firmware's model_id.json (a super.pak firmware's paks name none)
    with mock.patch.object(checklist.targets.probe, "model_id", return_value={"region": "ARIB"}):
        assert checklist.broadcast_of_firmware(tmp_path, ("rootfs", "otncabi")) == "arib"


def test_webos5():
    """W20L (k6lp-jhericurl) as the guest reported it: bootd answers firstAppId "" and its log
    has the launch; FirstUse's service country is the country itself; mount-readwrite is a
    systemd unit."""
    w20l = edit(
        GOOD,
        sysinfo='{"returnValue": true, "modelName": "32LX6900PJA"}',
        settings='{"settings": {"countryGroup": "JP", "country": "JPN", "smartServiceCountryCode3": '
        '"JPN", "smartServiceCountryCode2": "JP"}, "returnValue": true}',
        bootstatus='{"firstAppLaunched":true,"returnValue":true,"firstAppId":""}',
        firstapp_launch="com.webos.app.livetv",
        tvconfig_continentname=None,
        tvconfig_rmmgroup=None,
        tvconfig_rmmcountry=None,
        cmdline_model="32LX6900PJA",
        lastinput=None,
        mrw_status="systemd active/success",
        mrw_flag="mount-readwrite.service yes",
        **{"module": ["lgemu loaded 6.18.53"]},
    )
    got = items(w20l, "arib")
    assert got["first-app"][0] == "PASS", got["first-app"]
    assert got["region"][0] == "PASS", got["region"]
    assert got["mount-readwrite"][0] == "PASS", got["mount-readwrite"]
    assert items(edit(w20l, firstapp_launch=None), "arib")["first-app"][0] == "FAIL"
    assert (
        items(
            w20l.replace('"JPN", "smartServiceCountryCode2"', '"USA", "smartServiceCountryCode2"'),
            "arib",
        )["region"][0]
        == "FAIL"
    )


def test_global_firmware():
    """W21P's paks name no broadcast standard (otncabi-global): the board's, nyx-option-gen's system, is
    judged; without it the region is unknown."""
    assert items(edit(GOOD, nyx_system="DVB"), None)["region"][0] == "PASS"
    assert items(edit(GOOD, nyx_system="ARIB"), None)["region"][0] == "FAIL"
    assert items(GOOD, None)["region"][0] == "WARN"
    assert items(edit(GOOD, nyx_system="ARIB"), "dvb")["region"][0] == "PASS"  # the paks' first


def test_good():
    got = checklist.judge(GOOD, "dvb")
    assert [i for i, _, _ in got] == list(checklist.ITEMS)
    assert {i: s for i, s, _ in got} == {i: "PASS" for i in checklist.ITEMS}, got
    assert checklist.verdict(got) == "PASS"
    assert checklist.short(got) == "PASS"


def test_incomplete():
    got = checklist.judge(GOOD.replace("CHECKLIST FACTS END", ""), "dvb")
    assert {s for _, s, _ in got} == {"FAIL"}


def test_first_app():
    no_app = '{"firstAppLaunched":false,"bootStatus":"normal","returnValue":true}'
    assert items(edit(GOOD, bootstatus=no_app))["first-app"][0] == "FAIL"
    for blank in ("ffffffff", "00000000", "unread"):
        s, why = items(edit(GOOD, lastinput=f"0x1b48 {blank}"))["first-app"]
        assert s == "FAIL", why
    # webOS 1 and 2: no offset in the table, the first app alone
    assert items(edit(GOOD, lastinput=None))["first-app"][0] == "PASS"
    assert items(edit(GOOD, bootstatus=None))["first-app"][0] == "FAIL"


def test_region_mismatch():
    kr = edit(GOOD, tvconfig_continentname="KR")
    s, why = items(kr)["region"]
    assert s == "FAIL"
    assert "continent KR" in why
    assert "country USA" in items(GOOD.replace('"GBR"', '"USA"'))["region"][1]
    assert items(GOOD.replace('"dvb"', '"atsc"'))["region"][0] == "FAIL"
    assert (
        items(
            GOOD.replace(
                '"EU7", "smartServiceCountryCode3": "EU7"',
                '"KR7", "smartServiceCountryCode3": "KR7"',
            )
        )["region"][0]
        == "FAIL"
    )
    # the same facts on an ATSC firmware: every European value is wrong
    assert items(GOOD, "atsc")["region"][0] == "FAIL"
    # an ARIB set in Japan
    jp = (
        GOOD.replace('"GBR"', '"JPN"')
        .replace('"EU"', '"JA"')
        .replace("EU7", "JA7")
        .replace('"dvb"', '"arib"')
        .replace("=DE", "=JP")
        .replace("=EU", "=JA")
        .replace("=GBR", "=JPN")
    )
    assert items(jp, "arib")["region"][0] == "PASS", items(jp, "arib")["region"]


def test_service_country_other():
    s, why = items(
        GOOD.replace('"smartServiceCountryCode3": "EU7"', '"smartServiceCountryCode3": "other"')
    )["region"]
    assert s == "FAIL"
    assert "still other" in why


def test_region_broadcast_unknown_or_no_country():
    assert items(GOOD, None)["region"][0] == "WARN"
    assert items(edit(GOOD, settings='{"returnValue":false}'))["region"][0] == "FAIL"
    # webOS 1 and 2: no tvconfig selectors, no tvSystemName
    old = edit(
        GOOD,
        tvconfig_continentname=None,
        tvconfig_rmmgroup=None,
        tvconfig_rmmcountry=None,
        sysinfo='{"returnValue":true,"modelName":"43LH604V-ZB"}',
    )
    assert items(old)["region"][0] == "PASS"


def test_mount_readwrite():
    assert (
        items(edit(GOOD, mrw_flag="/tmp/mount-readwrite.conf.done no"))["mount-readwrite"][0]
        == "FAIL"
    )
    assert items(edit(GOOD, pm_freeze_timeout="7000 20000"))["mount-readwrite"][0] == "FAIL"
    assert items(edit(GOOD, pm_freeze_timeout="7000 missing"))["mount-readwrite"][0] == "FAIL"
    assert items(edit(GOOD, pm_freeze_timeout="7000 7000"))["mount-readwrite"][0] == "PASS"
    assert items(edit(GOOD, preload="needed no"))["mount-readwrite"][0] == "WARN"
    failed = edit(GOOD, dmesg="init: mount-readwrite main process (60) terminated with status 1")
    assert items(failed)["mount-readwrite"][0] == "FAIL"


def test_tvservice():
    respawn = edit(
        GOOD,
        dmesg=[
            "init: tvservice main process (130) terminated with status 255",
            "init: tvservice main process ended, respawning",
        ],
    )
    s, why = items(respawn)["tvservice"]
    assert s == "FAIL"
    assert "+1 more" in why
    rdx = "/var/log/reports/librdx/RDXD1_x__Linux Crash: {} crashes at location liblibc.so.6(gsignal+0x3c) ____x.781.gz"
    assert items(edit(GOOD, crash=rdx.format("tvservice")))["tvservice"][0] == "FAIL"
    s, why = items(edit(GOOD, crash=[rdx.format("com.webos.app.inputcommon")] * 2))["tvservice"]
    assert s == "WARN"
    assert "2 crash report(s): com.webos.app.inputcommon" in why
    s, why = items(edit(GOOD, dmesg="init: WebAppMgr main process (644) killed by SEGV signal"))[
        "tvservice"
    ]
    assert (s, "WebAppMgr" in why) == ("WARN", True)
    assert items(edit(GOOD, core="/tmp/core"))["tvservice"][0] == "WARN"


def test_sysprops():
    s, why = items(GOOD)["sysprops"]
    assert (s, why) == ("PASS", "3 properties, storageCapacity 3833593856")
    missing = (
        '{"returnValue":false,"errorCode":-1,"errorText":"required system resource is missing"}'
    )
    s, why = items(edit(GOOD, sysprops=missing))["sysprops"]
    assert (s, "required system resource is missing" in why) == ("FAIL", True)
    gap = GOOD.replace(
        "sysprops=",
        "main_board=BOARD_A5LR_DVB_EU\nnyx_board_type=Error 8 in executing query\nsysprops=",
    )
    s, why = items(edit(gap, sysprops=missing))["sysprops"]
    assert (s, "BOARD_A5LR_DVB_EU" in why) == ("WARN", True)
    other = gap.replace("BOARD_A5LR_DVB_EU", "BOARD_H15_DVB_EU")
    assert items(edit(other, sysprops=missing))["sysprops"][0] == "FAIL"
    one = '[ { "com.palm.properties.machineName": "m2" } ]'
    assert items(edit(GOOD, sysprops=one))["sysprops"][0] == "FAIL"
    assert items(edit(GOOD, sysprops=""))["sysprops"][0] == "FAIL"
    assert items(edit(GOOD, sysprops=None))["sysprops"][0] == "FAIL"


def test_eeprom():
    empty = GOOD.replace('"modelName": "43LH604V-ZB"', '"modelName": ""')
    assert items(empty)["eeprom"][0] == "FAIL"
    assert items(edit(GOOD, cmdline_model="55UJ750V-ZA"))["eeprom"][0] == "FAIL"
    assert items(edit(GOOD, sysinfo=None))["eeprom"][0] == "FAIL"


def test_modules():
    assert (
        items(edit(GOOD, module=["lgemu loaded 6.18.53", "mstar-utopia absent 6.18.53"]))[
            "modules"
        ][0]
        == "FAIL"
    )
    s, why = items(edit(GOOD, module=["lgemu loaded 6.18.52", "mstar-utopia loaded 6.18.53"]))[
        "modules"
    ]
    assert s == "FAIL"
    assert "lgemu built for 6.18.52" in why
    assert items(edit(GOOD, module=None))["modules"][0] == "FAIL"
    stale = edit(GOOD, dmesg="lgemu: i2c_adapters: cannot register (-22)")
    assert items(stale)["modules"][0] == "FAIL"
    assert items(edit(GOOD, tainted="4098"))["modules"][0] == "FAIL"


def test_short():
    got = [("first-app", "PASS", ""), ("region", "WARN", "x"), ("tvservice", "FAIL", "y")]
    assert checklist.verdict(got) == "FAIL"
    assert checklist.short(got) == "FAIL(region,tvservice)"
    assert checklist.verdict([]) == "PASS"
