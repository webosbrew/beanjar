# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The panel's refresh of a VM's board (lls.panel_rate, nvm.new_chip, prepare): the firmware's own
hw-option-gen decode (tv.hw.json's panelOutputFrameRate), 60 Hz where it is not run."""

import json
from unittest import mock

import pytest

from beanjar.board import lls, nvm, prepare


def hw(tmp_path, value, key="panelOutputFrameRate"):
    with open(tmp_path / "tv.hw.json", "w") as f:
        json.dump({"tv.hw": {key: value, "SoCOutputFrameRate": "60Hz"}}, f)
    return str(tmp_path)


@pytest.mark.parametrize(
    "value, hz",
    [("60Hz", 60), ("120Hz", 120), ("100Hz", 100), ("144Hz", 144), ("invalid_enum_7", None),
     ("0Hz", None), ("", None), (120, None)],
)  # fmt: skip
def test_panel_rate_decoded(tmp_path, value, hz):
    assert lls.panel_rate(hw(tmp_path, value)) == hz


def test_panel_rate_absent(tmp_path):
    assert lls.panel_rate(str(tmp_path)) is None  # no JSON
    assert lls.panel_rate(hw(tmp_path, "120Hz", key="SoCChipType")) is None  # no field
    (tmp_path / "tv.hw.json").write_text("{")
    assert lls.panel_rate(str(tmp_path)) is None  # not JSON


def test_new_chip_carries_the_panel_rate(tmp_path):
    (tmp_path / "eeprom.img").write_bytes(b"\xff" * 16)
    (tmp_path / "records.txt").write_text("probe output\n")
    hw(tmp_path, "120Hz")
    target = ("k8hp", "mullet")
    with mock.patch.object(lls, "cache_dir", return_value=str(tmp_path)):
        chip = nvm.new_chip("/x/fw", target, None, fw_name="FW", say=lambda *a: None)
    assert chip.panel_hz == 120
    # a chip without the decode (before webOS 5, or a test's) has none
    assert nvm.Chip(None, None, None).panel_hz is None


def test_prepared_default_is_60():
    job = mock.Mock(dir="/nonexistent")
    assert prepare.Prepared(job, {}).panel_hz == 60
    assert prepare.Prepared(job, {"panel_hz": 120}).panel_hz == 120
