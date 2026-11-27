#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Development VMs use managed directories; offline migration retains exact originals."""

from pathlib import Path
from unittest import mock

import pytest

from beanjar import development, paths, vmconfig
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

    def legacy(self):
        local = self.repo / "local"
        (local / "ca").mkdir(parents=True)
        (local / "id_ed25519").write_bytes(b"fixture-private-key\n")
        (local / "id_ed25519").chmod(0o600)
        (local / "model-secret").write_bytes(b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n")
        (local / "model-secret").chmod(0o600)
        (local / "ca/ca.key").write_bytes(b"fixture-ca-key\n")
        (local / "ca/.lock").touch()
        state = self.repo / "build/state/default"
        state.mkdir(parents=True)
        with (state / "nvram.img").open("wb") as f:
            f.write(b"fixture-state")
            f.truncate(8 << 20)
        return local, state

    def migrate(self):
        with (
            mock.patch.object(development, "_offline"),
            mock.patch.object(development, "_config", return_value=self.config),
        ):
            return development.migrate(self.env, self.tree)


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


def test_preserves_keys_ca_state_and_identity(dev):
    local, state = dev.legacy()
    assert dev.migrate() == dev.directory
    dest = Path(dev.directory)
    assert (dest / "identity/id_ed25519").read_bytes() == (local / "id_ed25519").read_bytes()
    assert (dest / "identity/model-secret").read_bytes() == (local / "model-secret").read_bytes()
    from beanjar import sestore

    assert sestore.load(dest / "identity")[1][sestore.MODEL] == (
        local / "model-secret"
    ).read_bytes().rstrip(b"\n")
    assert (dest / "identity/id_ed25519").stat().st_mode & 0o777 == 0o600
    assert (Path(dev.library) / "ca/ca.key").read_bytes() == (local / "ca/ca.key").read_bytes()
    assert not ((Path(dev.library) / "ca/.lock").exists())
    assert (dest / "state/nvram.img").read_bytes() == (state / "nvram.img").read_bytes()
    stored = vmconfig.read(dest / "vm.toml", {})
    assert {key: stored[key] for key in dev.config} == dev.config
    (dest / "state/nvram.img").write_bytes(b"changed")
    assert (state / "nvram.img").stat().st_size == 8 << 20
    with pytest.raises(development.Error):
        dev.migrate()


def test_existing_ca_and_symlinks_refused(dev):
    local, _ = dev.legacy()
    ca = Path(dev.library) / "ca"
    ca.mkdir(parents=True)
    (ca / "ca.key").write_bytes(b"existing")
    with pytest.raises(development.Error):
        dev.migrate()
    assert (ca / "ca.key").read_bytes() == b"existing"
    assert not (Path(dev.directory).exists())
    (ca / "ca.key").unlink()
    ca.rmdir()
    (local / "unsafe").symlink_to(dev.root)
    with pytest.raises(development.Error):
        dev.migrate()
    assert not (Path(dev.directory).exists())


def test_running_legacy_vm_refused(dev):
    dev.legacy()
    with (
        mock.patch.object(development.base, "read_pid", return_value=123),
        mock.patch.object(development.base, "proc_alive", return_value=True),
    ):
        with pytest.raises(development.Error):
            development.migrate(dev.env, dev.tree)
    assert not (Path(dev.directory).exists())


def test_prepare_blocks_implicit_key_replacement(dev):
    dev.legacy()
    with pytest.raises(development.Error, match="migrate-local"):
        development.prepare(dev.env, dev.tree)
    assert not (Path(dev.directory).exists())


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
    cfg = vmconfig.read(Path(dev.directory) / "vm.toml", {})
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


def test_migration_keeps_board_identity_and_absolute_firmware(dev, monkeypatch):
    """A migrated VM keeps the board.cmdline's MAC and serial (a comment naming
    webos.ethaddr= is not the MAC) and stores the firmware as an absolute path, which
    firmware.vm_firmware would otherwise resolve against the VM's folder."""
    dev.legacy()
    firmware = dev.root / "firmware"
    firmware.mkdir()
    (firmware / "manifest.toml").write_text('chip="h13"\nrelease="afro"\n')
    board = dev.repo / "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline"
    board.parent.mkdir(parents=True)
    board.write_text(
        "# The emulator's MAC; a VM's own replaces it (webos.ethaddr=)\n"
        "ethaddr=a8:23:fe:12:34:56\nserialNum=000RAXX00001\n"
    )
    monkeypatch.chdir(dev.root)
    with mock.patch.object(development, "_offline"):
        development.migrate(dict(dev.env, FW="./firmware"), dev.tree)
    cfg = vmconfig.read(Path(dev.directory) / "vm.toml", {})
    assert cfg["mac"] == "a8:23:fe:12:34:56"
    assert cfg["serial"] == "000RAXX00001"
    assert cfg["fw"] == str(firmware)


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
    vmconfig.write(str(user / "vm.toml"), {"fw": "/x", "slot": 1})
    env = dict(dev.env, FW=str(firmware), NET_LINK="nat", NET_MODE="direct")
    assert development.configured_environment(env, dev.tree)["SLOT"] == "2"
    assert development.prepare(dict(env, SLOT="9"), dev.tree)["SLOT"] == "2"
    other = development.prepare(dict(env, INSTANCE="other"), dev.tree)
    assert other["SLOT"] == "3"
    assert development.configured_environment(env, dev.tree)["SLOT"] == "2"
    assert vmconfig.read(Path(dev.directory) / "vm.toml")["slot"] == 2


def test_failed_sparse_copy_leaves_originals_and_destination_untouched(dev):
    local, state = dev.legacy()
    with (
        mock.patch.object(development, "_offline"),
        mock.patch.object(development, "_config", return_value=dev.config),
        mock.patch.object(development.subprocess, "run", side_effect=OSError("copy failed")),
    ):
        with pytest.raises(OSError):
            development.migrate(dev.env, dev.tree)
    assert (local / "id_ed25519").exists()
    assert (state / "nvram.img").exists()
    assert not (Path(dev.directory).exists())
    assert not ((Path(dev.library) / "ca").exists())


def test_other_instance_reuses_identical_ca_without_overwrite(dev):
    local, _ = dev.legacy()
    dev.migrate()
    env = dict(dev.env, INSTANCE="other")
    with (
        mock.patch.object(development, "_offline"),
        mock.patch.object(development, "_config", return_value=dev.config),
    ):
        other = Path(development.migrate(env, dev.tree))
    assert str(other) != dev.directory
    assert (other / "identity/id_ed25519").read_bytes() == (local / "id_ed25519").read_bytes()
    assert (Path(dev.library) / "ca/ca.key").read_bytes() == (local / "ca/ca.key").read_bytes()
