#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the shipped config helper without sound hardware or real mounts."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TOP = Path(__file__).resolve().parents[3]
ROOTFS = TOP / "guest/layers/webos/spacebuns/audio-rootfs"
HELPER = "etc/emulator/pulseaudio-mic-provider.sh"
MODULE_VIEW = "etc/emulator/pulseaudio-module-view.sh"
DROPIN = "etc/systemd/system/pulseaudio.service.d/zz-emulator-mic-provider.conf"
FIXTURE = Path(__file__).parent / "fixtures/spacebuns-system.pa"
PTTS = TOP / "guest/layers/webos/jhericurl/rootfs/etc/emulator/pulseaudio-ptts.sh"


@pytest.fixture
def sandbox():
    """A temporary directory every user can reach: the non-owner reads run as nobody, who cannot
    pass pytest's own tmp_path (under a 0700 directory of the user's)."""
    root = Path(tempfile.mkdtemp(prefix="beanjar-spacebuns-"))
    root.chmod(0o755)
    yield root
    shutil.rmtree(root, ignore_errors=True)


class TestProviderTests:
    @pytest.fixture(autouse=True)
    def setup(self, sandbox):
        self.root = sandbox
        self.config = self.root / "system.pa"
        self.config.write_bytes(FIXTURE.read_bytes())
        self.sound = self.root / "sound"
        self.sound.mkdir()
        self.runtime = self.root / "runtime"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.mounts = self.root / "mounts"
        # Symlink models the shared inode identity of a bind mount. This fixture
        # never invokes mount, changes privileges or accesses device nodes.
        self.command(
            "mount",
            'printf "bind\\n" >> "$TEST_MOUNTS"\n'
            'test "$1" = --bind || exit 9\n'
            'case "$2" in */ptts.pa) suffix=ptts ;; *) suffix=mic ;; esac\n'
            'mv "$3" "$3.under-$suffix" && ln -s "$2" "$3"\n',
        )
        self.command("umount", 'rm "$1" && mv "$1.under-mic" "$1"\n')
        self.env = dict(
            os.environ, PATH=str(self.bin) + ":" + os.environ["PATH"], TEST_MOUNTS=str(self.mounts)
        )

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def run_helper(self, success=True):
        result = subprocess.run(
            ["sh", str(ROOTFS / HELPER), str(self.config), str(self.runtime), str(self.sound)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        if success:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
            assert "emulator-pulseaudio-mic-provider:" in result.stderr
        return result

    def inherited_ptts(self):
        # Run the genuine inherited helper with only its three filesystem paths
        # redirected to the sandbox, before running the new appended pre-step.
        script = PTTS.read_text().replace("/etc/pulse/system.pa", str(self.config))
        script = script.replace("/sys/class/sound/pcmC0D7p", str(self.sound / "pcmC0D7p"))
        script = script.replace("/run/emulator-system.pa", str(self.root / "ptts.pa"))
        subprocess.run(["sh", "-c", script], env=self.env, check=True, capture_output=True)

    def test_stock_playback_and_inherited_ptts_survive_restarts(self):
        self.inherited_ptts()
        before = self.config.read_text()
        assert "load-module module-null-sink sink_name=ptts\n" in before
        self.run_helper()
        expected = before.replace(
            "load-module module-mic-capture\n",
            "# beanjar-no-card1: load-module module-mic-capture\n",
        )
        assert self.config.read_text() == expected
        inode = self.config.stat().st_ino
        modified = self.config.stat().st_mtime_ns
        for _ in range(3):
            assert self.run_helper().stdout == ""
        assert self.config.stat().st_ino == inode
        assert self.config.stat().st_mtime_ns == modified
        assert self.mounts.read_text().splitlines() == ["bind", "bind"]

    def test_card_present_preserves_stock_bytes_and_arguments(self):
        (self.sound / "card1").mkdir()
        self.config.write_text(
            self.config.read_text().replace(
                "load-module module-mic-capture\n",
                "load-module module-mic-capture mic_source_name=real ref_dev=spk\n",
            )
        )
        before = self.config.read_bytes()
        self.run_helper()
        assert self.config.read_bytes() == before
        assert not self.mounts.exists()

    def test_provider_transitions_restore_original_arguments_without_stacked_mounts(self):
        self.config.write_text(
            self.config.read_text() + "\tload-module\tmodule-mic-capture ref_dev=mix\n"
            "load-module module-mic-capture-extra keep=1\n"
            "#load-module module-mic-capture keep=2\n"
        )
        before = self.config.read_bytes()
        self.run_helper()
        absent = self.config.read_bytes()
        (self.sound / "card1").mkdir()
        self.run_helper()
        assert self.config.read_bytes() == before
        (self.sound / "card1").rmdir()
        self.run_helper()
        assert self.config.read_bytes() == absent
        assert self.mounts.read_text().splitlines() == ["bind", "bind", "bind"]
        assert b"load-module module-mic-capture-extra keep=1\n" in absent
        assert b"#load-module module-mic-capture keep=2\n" in absent

    def test_missing_config_or_sysfs_is_explicit_failure(self):
        self.config.unlink()
        self.run_helper(False)
        self.config.write_bytes(FIXTURE.read_bytes())
        self.sound.rmdir()
        self.run_helper(False)
        assert not self.mounts.exists()

    def test_runtime_and_generation_write_failures_are_explicit(self):
        self.runtime.write_text("not a directory")
        self.run_helper(False)
        self.runtime.unlink()
        self.command("sed", "exit 28\n")
        self.run_helper(False)
        assert self.config.read_bytes() == FIXTURE.read_bytes()
        assert not self.mounts.exists()
        assert list(self.runtime.iterdir()) == []

    def test_generated_mode_and_legacy_binding_repair(self):
        self.run_helper()
        output = self.runtime / "system.pa"
        assert stat.S_IMODE(output.stat().st_mode) == 0o644
        # An already bound legacy 0600 file is repaired on an unchanged restart.
        output.chmod(0o600)
        self.run_helper()
        assert stat.S_IMODE(output.stat().st_mode) == 0o644
        assert self.mounts.read_text().splitlines() == ["bind"]

    @pytest.mark.skipif(
        os.geteuid() != 0,
        reason="Non-owner read requires uid switching; run this fixture with sudo.",
    )
    def test_actual_non_owner_reader(self):
        self.run_helper()
        output = self.runtime / "system.pa"
        code = (
            "import os, pathlib, sys; "
            "assert os.getuid() == 65534; "
            "data = pathlib.Path(sys.argv[1]).read_bytes(); "
            'assert b"load-module module-alsa-sink device=hw:0,6" in data; '
            'print("non-owner reader PASS")'
        )

        def drop_user():
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)

        result = subprocess.run(
            ["/usr/bin/python3", "-B", "-c", code, str(output)],
            capture_output=True,
            text=True,
            preexec_fn=drop_user,
        )
        assert result.returncode == 0, result.stderr
        assert "non-owner reader PASS" in result.stdout
        # Prove the reader discriminates the original regression, not just bytes.
        output.chmod(0o600)
        denied = subprocess.run(
            ["/usr/bin/python3", "-B", "-c", code, str(output)],
            capture_output=True,
            text=True,
            preexec_fn=drop_user,
        )
        assert denied.returncode != 0
        assert "PermissionError" in denied.stderr

    def test_chmod_failure_is_explicit_without_binding(self):
        self.command("chmod", "exit 1\n")
        result = self.run_helper(False)
        assert "cannot set configuration read permissions" in result.stderr
        assert not self.mounts.exists()
        assert self.config.read_bytes() == FIXTURE.read_bytes()

    def test_temporary_and_final_write_failures_are_explicit(self):
        before = self.config.read_bytes()
        self.command("mktemp", "exit 28\n")
        self.run_helper(False)
        (self.bin / "mktemp").unlink()
        self.command("mv", "exit 28\n")
        result = self.run_helper(False)
        assert "cannot write runtime configuration" in result.stderr
        assert self.config.read_bytes() == before
        assert not self.mounts.exists()

    def test_transition_mount_failure_preserves_inherited_ptts_and_retries(self):
        self.inherited_ptts()
        inherited = self.config.read_bytes()
        self.run_helper()
        (self.sound / "card1").mkdir()
        mount = (self.bin / "mount").read_text()
        self.command("mount", "exit 32\n")
        self.run_helper(False)
        assert self.config.read_bytes() == inherited
        (self.sound / "card1").rmdir()
        (self.bin / "mount").write_text(mount)
        self.run_helper()
        assert "# beanjar-no-card1:" in self.config.read_text()
        assert "load-module module-null-sink sink_name=ptts" in self.config.read_text()

    def test_failed_mount_is_not_reported_as_success_and_can_retry(self):
        mount = (self.bin / "mount").read_text()
        self.command("mount", "exit 32\n")
        result = self.run_helper(False)
        assert "cannot bind" in result.stderr
        assert result.stdout == ""
        assert self.config.read_bytes() == FIXTURE.read_bytes()
        (self.bin / "mount").write_text(mount)
        self.run_helper()
        assert "# beanjar-no-card1:" in self.config.read_text()

    def test_unbind_failure_is_explicit_and_keeps_previous_config(self):
        self.run_helper()
        (self.sound / "card1").mkdir()
        previous = self.config.read_bytes()
        self.command("umount", "exit 32\n")
        result = self.run_helper(False)
        assert "cannot unbind previous runtime configuration" in result.stderr
        assert self.config.read_bytes() == previous
        assert result.stdout == ""

    def test_without_mic_module_does_not_mount(self):
        self.config.write_text(
            self.config.read_text().replace("load-module module-mic-capture\n", "")
        )
        before = self.config.read_bytes()
        self.run_helper()
        assert self.config.read_bytes() == before
        assert not self.mounts.exists()

    def test_dropin_appends_after_inherited_pre_step_and_target_scope(self):
        dropin = (ROOTFS / DROPIN).read_text()
        assert [line for line in dropin.splitlines() if line and not line.startswith("#")] == [
            "[Service]",
            "ExecStartPre=/bin/sh /" + HELPER,
            "ExecStartPre=/bin/sh /" + MODULE_VIEW,
            "Environment=PULSE_DLPATH=/run/beanjar-pulse/modules",
        ]
        assert Path(DROPIN).name > "emulator-ptts.conf"
        build = (TOP / "guest/layers/webos/spacebuns/meson.build").read_text()
        assert (
            "if soc == 'o22n2' and webos == 'spacebuns'\n  webos_layer_args += ['--tree', meson.current_source_dir() / 'audio-rootfs', '.']"
            in build
        )

    def test_flat_staging_and_component_publication(self):
        layer = self.root / "layer"
        subprocess.run(
            [
                sys.executable,
                str(TOP / "meson/stage.py"),
                "--out",
                str(layer),
                "--stamp",
                str(self.root / "stage.stamp"),
                "--depfile",
                str(self.root / "stage.d"),
                "--tree",
                str(ROOTFS),
                ".",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(TOP / "meson/components.py"),
                "--dest",
                str(self.root / "components"),
                "--soc",
                "o22n2",
                "--release",
                "spacebuns",
                "--layer",
                "webos=" + str(layer),
                "--stamp",
                str(self.root / "publish.stamp"),
            ],
            check=True,
            capture_output=True,
        )
        release = self.root / "components/release/spacebuns"
        selections = json.loads((release / "selections.json").read_text())
        assert list(selections) == ["o22n2-spacebuns"]
        variant = release / "variants" / selections["o22n2-spacebuns"]
        for relative in (HELPER, MODULE_VIEW, DROPIN):
            assert (variant / relative).read_bytes() == (ROOTFS / relative).read_bytes()


class TestModuleViewTests:
    @pytest.fixture(autouse=True)
    def setup(self, sandbox):
        self.root = sandbox
        self.stock = self.root / "stock"
        self.stock.mkdir()
        self.sound = self.root / "sound"
        self.sound.mkdir()
        self.runtime = self.root / "runtime"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + ":" + os.environ["PATH"])
        for name in (
            "module-alsa-sink.so",
            "module-mic-capture.so",
            "module-mic-capture.la",
            "module-mic-capture.so.0",
            "libcompanion.so",
            "module-mic-capture-extra.so",
        ):
            (self.stock / name).write_text("unchanged stock bytes: " + name)
        (self.stock / "subdirectory").mkdir()

    def run_helper(self, success=True):
        result = subprocess.run(
            ["sh", str(ROOTFS / MODULE_VIEW), str(self.stock), str(self.runtime), str(self.sound)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        if success:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
            assert "emulator-pulseaudio-module-view:" in result.stderr
        return result

    def view(self):
        return self.runtime / "modules"

    def test_unavailable_provider_is_absent_without_changing_other_modules(self):
        before = {p.name: p.read_bytes() for p in self.stock.iterdir() if p.is_file()}
        self.run_helper()
        assert {p.name for p in self.view().iterdir()} == {
            "module-alsa-sink.so",
            "libcompanion.so",
            "module-mic-capture-extra.so",
            "subdirectory",
        }
        for p in self.view().iterdir():
            assert p.is_symlink()
            assert p.resolve() == self.stock / p.name
        assert before == {p.name: p.read_bytes() for p in self.stock.iterdir() if p.is_file()}
        assert stat.S_IMODE(self.view().stat().st_mode) == 0o755

    def test_card_presence_and_transitions_update_module_availability(self):
        self.run_helper()
        old = self.view().resolve()
        (self.sound / "card1").mkdir()
        self.run_helper()
        assert {p.name for p in self.view().iterdir()} == {p.name for p in self.stock.iterdir()}
        assert not old.exists()
        (self.sound / "card1").rmdir()
        self.run_helper()
        assert not (self.view() / "module-mic-capture.so").exists()
        assert len(list(self.runtime.iterdir())) == 2  # symlink and its one generation

    def test_unchanged_restart_and_stock_module_updates(self):
        self.run_helper()
        target, inode = self.view().resolve(), self.view().lstat().st_ino
        self.run_helper()
        assert self.view().resolve() == target
        assert self.view().lstat().st_ino == inode
        (self.stock / "libcompanion.so").unlink()
        (self.stock / "new-module.so").write_text("new stock module")
        self.run_helper()
        assert not (self.view() / "libcompanion.so").exists()
        assert (self.view() / "new-module.so").read_text() == "new stock module"
        assert not target.exists()

    def test_failed_construction_and_publication_preserve_previous_view(self):
        self.run_helper()
        old = self.view().resolve()
        (self.sound / "card1").mkdir()
        for command in ("mktemp", "chmod", "ln", "mv"):
            stub = self.bin / command
            stub.write_text("#!/bin/sh\nexit 28\n")
            stub.chmod(0o755)
            self.run_helper(False)
            stub.unlink()
            assert self.view().resolve() == old
            assert not (self.view() / "module-mic-capture.so").exists()
            assert len(list(self.runtime.iterdir())) == 2
        self.run_helper()
        assert (self.view() / "module-mic-capture.so").exists()

    def test_missing_sysfs_modules_and_empty_modules_fail_explicitly(self):
        self.sound.rmdir()
        self.run_helper(False)
        assert not self.runtime.exists()
        self.sound.mkdir()
        self.stock.rename(self.root / "stock-away")
        self.run_helper(False)
        self.stock.mkdir()
        self.run_helper(False)
        assert not self.view().exists()
        assert list(self.runtime.iterdir()) == []

    def test_foreign_runtime_view_is_not_replaced_or_removed(self):
        self.runtime.mkdir()
        self.view().symlink_to(self.stock)
        self.run_helper(False)
        assert self.view().resolve() == self.stock
        assert (self.stock / "module-mic-capture.so").exists()

    @pytest.mark.skipif(
        os.geteuid() != 0, reason="Non-owner read requires uid switching; run with sudo."
    )
    def test_actual_non_owner_can_read_module_view(self):
        self.run_helper()

        def drop_user():
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)

        result = subprocess.run(
            [
                "/usr/bin/python3",
                "-B",
                "-c",
                "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
                str(self.view() / "module-alsa-sink.so"),
            ],
            capture_output=True,
            text=True,
            preexec_fn=drop_user,
        )
        assert result.returncode == 0, result.stderr
        assert "unchanged stock bytes:" in result.stdout
