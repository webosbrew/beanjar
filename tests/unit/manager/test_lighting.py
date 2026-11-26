#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""status show's lighting object: /proc/lgemu/led_micom (ledmicom.c's table) and the TV's
lighting settings decoded into what the OLED Flex's LEDs show (docs/research/led-lighting.md)."""

import json

from beanjar import lighting, prepare

# W22O 33.31.68's lightingcontroller, measured: at its start (lightingEnable off) the welcome
# ceremony, off twice and the brightness; lightingEnable on, the mode's command (dynamic);
# staticMode1, its colour (staticModeColor1 35)
TABLE = """writes 6
reads 1
cmd 0x21 count 2 order 3 len 1 data 02
cmd 0x24 count 1 order 5 len 1 data 00
cmd 0x26 count 1 order 6 len 3 data e000ff
cmd 0x2a count 1 order 4 len 1 data 08
cmd 0x2b count 1 order 1 len 1 data 03
"""
SETTINGS = (
    '{"subscribed": false, "category": "other", "method": "getSystemSettings", "settings": '
    '{"lightingEnable": "on", "lightingMode": "staticMode1", "lightingBrightness": 8}, '
    '"returnValue": true}'
)


def test_parse_table():
    t = lighting.parse_table(
        TABLE + "cmd 0x22 count 1 order 7 len 128 data " + "00" * 128 + " truncated\n"
    )
    assert (t["writes"], t["reads"]) == (6, 1)
    assert t["commands"][0x21] == {"count": 2, "order": 3, "data": b"\x02", "truncated": False}
    assert t["commands"][0x26]["data"] == bytes.fromhex("e000ff")
    assert t["commands"][0x22]["truncated"] and len(t["commands"][0x22]["data"]) == 128
    assert lighting.parse_table("cat: /proc/lgemu/led_micom: No such file") is None


def test_decode():
    model = lighting.decode(lighting.parse_table(TABLE))
    # the static colour came last: that mode, with its colour
    assert model == {"on": True, "brightness": 8, "mode": "staticMode1", "color": "e000ff"}
    later = TABLE + "cmd 0x25 count 1 order 9 len 1 data 00\n"
    model = lighting.decode(lighting.parse_table(later))
    assert (model["mode"], model["color"]) == ("peaceful", None)
    # lightingEnable off: 0x21 02 after the last mode command
    off = lighting.decode(lighting.parse_table(later + "cmd 0x21 count 4 order 10 len 1 data 02\n"))
    assert (off["on"], off["mode"]) == (False, "peaceful")
    # at a start with it off: the welcome ceremony, then off
    start = "writes 3\nreads 0\ncmd 0x21 count 2 order 3 len 1 data 02\ncmd 0x2b count 1 order 1 len 1 data 03\n"
    assert lighting.decode(lighting.parse_table(start))["on"] is False
    empty = lighting.decode(lighting.parse_table("writes 0\nreads 0\n"))
    assert empty == {"on": None, "brightness": None, "mode": None, "color": None}


def test_report_and_lines():
    out = lighting.report(TABLE, SETTINGS)
    assert out["selected"] == {"enable": "on", "mode": "staticMode1", "brightness": 8}
    assert out["writes"] == 6
    assert out["commands"][0] == {
        "command": "0x21",
        "count": 2,
        "order": 3,
        "data": "02",
        "truncated": False,
    }
    out["error"] = None
    assert lighting.lines(out) == [
        "lighting selected: on, staticMode1, brightness 8",
        "lighting micom: on, staticMode1 #e000ff, brightness 8 (6 writes)",
    ]
    none = lighting.report("writes 0\nreads 0\n", "")
    none["error"] = None
    assert lighting.lines(none) == [
        "lighting selected: not known",
        "lighting micom: nothing sent yet",
    ]
    assert lighting.lines(None) == []
    assert lighting.lines({"error": "the VM's shell did not answer", "writes": None}) == [
        "lighting: the VM's shell did not answer"
    ]


def test_command():
    cmd = lighting.command()
    assert "grep -q ledmicom /sys/module/lgemu/parameters/i2c_dev" in cmd
    assert "cat /proc/lgemu/led_micom" in cmd
    assert '"category": "other"' in cmd


def test_has_micom(tmp_path):
    """Only a VM whose prepared board data gives lgemu the micom has a lighting object."""
    assert not lighting.has_micom(str(tmp_path))
    d = tmp_path / prepare.DIR
    d.mkdir()
    record = d / prepare.RECORD
    record.write_text(json.dumps({"derived_params": {"lgemu": ["i2c_dev=light2:1:0x10"]}}))
    assert not lighting.has_micom(str(tmp_path))
    record.write_text(json.dumps({"derived_params": {"lgemu": ["i2c_dev=ledmicom:0:0x09"]}}))
    assert lighting.has_micom(str(tmp_path))
