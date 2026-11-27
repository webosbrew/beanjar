# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The open test guest's target (guest/targets/virt-testguest.toml): a release without LG's board
data, whose VM is prepared without firmware code and boots with no partition map or EEPROM."""

import os

from beanjar.board import prepare
from beanjar.firmware import probe, targets

TESTGUEST = ("virt", "testguest")


def firmware(tmp_path):
    """A firmware directory as scripts/testguest-setup writes it (the rootfs a stand-in)."""
    fw = tmp_path / "image"
    fw.mkdir()
    (fw / "rootfs.pak").write_bytes(b"hsqs")
    (fw / "manifest.toml").write_text('name = "testguest"\nchip = "virt"\nrelease = "testguest"\n')
    return str(fw)


def test_target_row():
    r = targets.release(TESTGUEST)
    assert targets.platform(TESTGUEST) == "Test"
    assert not targets.board_data(TESTGUEST)
    assert targets.board_data(("h13", "afro"))
    assert r.partmap is None and r.nvm is None
    assert r.always_layers == ("testguest",) and r.init_system == "sysvinit"
    assert targets.pak_files(TESTGUEST) == ("rootfs",)
    assert targets.takes_power_off(TESTGUEST)


def test_manifest_names_the_target(tmp_path):
    assert probe.firmware_target(firmware(tmp_path)) == TESTGUEST


def test_prepare_without_board_data(tmp_path):
    fw = firmware(tmp_path)
    vm = tmp_path / "vm"
    (vm / "run").mkdir(parents=True)
    job = prepare.Job({"FW": fw, "RUN_DIR": str(vm / "run")})
    got = prepare.ensure(job, say=lambda *a: None)
    assert got.partmap is None
    assert got.chip.image is None
    assert not os.path.exists(os.path.join(job.dir, prepare.PARTINFO))
    assert prepare.stale(job) is None  # current: nothing missing without a map
