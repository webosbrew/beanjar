#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Development VMs use managed directories."""

from pathlib import Path
from unittest import mock

import pytest

from beanjar.vm import development, config
from beanjar import paths
from beanjar.runtime import qemu


class Dev:
    def __init__(self, root):
        self.root = Path(root)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.tree = paths.Tree(self.repo)
        self.env = {"BEANJAR_LIBRARY": str(self.root / "data")}
        self.library, self.directory = development.locations(self.env, self.tree)
        self.config = {
            "fw": str(self.root / "firmware"),
            "mac": "02:11:22:33:44:55",
            "serial": "TEST123",
        }


@pytest.fixture
def dev(tmp_path):
    return Dev(tmp_path)


def test_defaults_are_managed_and_read_only(dev):
    env = development.environment(dev.env, dev.tree)
    assert env["IDENTITY_DIR"] == dev.directory + "/identity"
    assert env["STATE"] == dev.directory + "/state"
    assert env["RUN_DIR"] == dev.directory + "/run"
    assert env["NETSTUB_CA"] == dev.library + "/ca"
    assert not (Path(dev.library).exists())
    _, other = development.locations(dict(dev.env, INSTANCE="other"), dev.tree)
    assert other != dev.directory


def test_explicit_paths_survive(dev):
    env = development.environment(
        dict(dev.env, RUN_DIR="/run/custom", STATE="none", IDENTITY_DIR="/custom/identity"),
        dev.tree,
    )
    assert env["STATE"] == "none"
    assert env["RUN_DIR"] == "/run/custom"
    assert env["NETSTUB_CA"] == "/custom/identity/ca"


def test_real_config_preparation_and_reuse(dev):
    firmware = dev.root / "firmware"
    firmware.mkdir()
    (firmware / "manifest.toml").write_text('chip="h13"\nrelease="afro"\n')
    board = dev.repo / "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline"
    board.parent.mkdir(parents=True)
    board.write_text("ethaddr=02:11:22:33:44:55 serialNum=TEST123\n")
    env = dict(
        dev.env,
        FW=str(firmware),
        NET_LINK="nat",
        NET_MODE="direct",
        MAC=dev.config["mac"],
        SERIAL=dev.config["serial"],
    )
    # the default display follows the host's accelerator (KVM: egl-headless); pinned to TCG's
    with mock.patch.object(qemu, "default_accel", return_value="tcg"):
        resolved = development.prepare(env, dev.tree)
    cfg = config.read(Path(dev.directory) / "vm.toml", {})
    assert cfg["mac"] == dev.config["mac"]
    assert cfg["serial"] == dev.config["serial"]
    assert cfg["layers"] == ["headless"]
    assert cfg["display"] == "none"
    assert resolved["STATE"] == dev.directory + "/state"
    assert not ((dev.repo / "local").exists())
    assert not ((Path(dev.directory) / "compatibility.toml").exists()), "no per-VM marker"
    with mock.patch.object(qemu, "default_accel", return_value="tcg"):
        assert development.prepare(env, dev.tree) == resolved


def test_empty_setting_overrides_the_stored_one(dev):
    """A caller that gives a setting empty (scripts/regress's lanes, LAUNCHER_DEFAULTED) gets the
    launcher's default, not what the VM's vm.toml kept from the run that made it."""
    firmware = dev.root / "firmware"
    firmware.mkdir()
    (firmware / "manifest.toml").write_text('chip="h13"\nrelease="afro"\n')
    board = dev.repo / "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline"
    board.parent.mkdir(parents=True)
    board.write_text("ethaddr=02:11:22:33:44:55 serialNum=TEST123\n")
    env = dict(dev.env, FW=str(firmware), NET_LINK="nat", NET_MODE="direct")
    made = development.prepare(dict(env, DISPLAY_BACKEND="dbus", MEM="2048"), dev.tree)
    assert (made["AUDIO"], made["MEM"]) == ("dbus", "2048")
    later = development.prepare(dict(env, DISPLAY_BACKEND="none", AUDIO="", MEM=""), dev.tree)
    assert (later["DISPLAY_BACKEND"], later["AUDIO"], later["MEM"]) == ("none", "", "")
    assert development.prepare(env, dev.tree)["AUDIO"] == "dbus"  # not given: the stored one


def test_empty_setting_makes_a_new_vm_with_the_default(dev):
    """A new development VM given MEM and SMP empty (scripts/regress's first start of a target) is made
    with the launcher's defaults; int('') failed it (lm21a-kisscurl)."""
    firmware = dev.root / "firmware"
    firmware.mkdir()
    (firmware / "manifest.toml").write_text('chip="h13"\nrelease="afro"\n')
    board = dev.repo / "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline"
    board.parent.mkdir(parents=True)
    board.write_text("ethaddr=02:11:22:33:44:55 serialNum=TEST123\n")
    made = development.prepare(
        dict(
            dev.env,
            FW=str(firmware),
            NET_LINK="nat",
            NET_MODE="direct",
            MEM="",
            SMP="",
            AUDIO="",
        ),
        dev.tree,
    )
    assert (made["MEM"], made["SMP"], made["AUDIO"]) == ("", "", "")
    assert development.prepare(dict(dev.env, FW=str(firmware)), dev.tree)["SMP"] == "4"


def test_slots(dev):
    """Each development VM (each INSTANCE) has a slot of its own, taken at creation in the slot
    domain of the library it is in (the user's library and its development libraries); the
    caller's SLOT never replaces it. A dry argv shows the one it would be made with."""
    firmware = dev.root / "firmware"
    firmware.mkdir()
    (firmware / "manifest.toml").write_text('chip="h13"\nrelease="afro"\n')
    board = dev.repo / "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline"
    board.parent.mkdir(parents=True)
    board.write_text("ethaddr=02:11:22:33:44:55 serialNum=TEST123\n")
    user = Path(dev.env["BEANJAR_LIBRARY"]) / "vms/tv1"  # a VM of the user's library
    user.mkdir(parents=True)
    config.write(str(user / "vm.toml"), {"fw": "/x", "slot": 1})
    env = dict(dev.env, FW=str(firmware), NET_LINK="nat", NET_MODE="direct")
    assert development.configured_environment(env, dev.tree)["SLOT"] == "2"
    assert development.prepare(dict(env, SLOT="9"), dev.tree)["SLOT"] == "2"
    other = development.prepare(dict(env, INSTANCE="other"), dev.tree)
    assert other["SLOT"] == "3"
    assert development.configured_environment(env, dev.tree)["SLOT"] == "2"
    assert config.read(Path(dev.directory) / "vm.toml")["slot"] == 2
