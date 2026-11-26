#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the shipped native launcher bootstrap without touching a real mount."""

import importlib.util
import os
import shutil
from pathlib import Path
import subprocess

import pytest

TOP = Path(__file__).resolve().parents[3]
HELPER = TOP / "guest/layers/gl/native-rootfs/usr/libexec/beanjar/native-weston-launcher"
# The measured end of the stock launcher (W24G 02.68.80 usr/bin/surface-manager.sh).
LAUNCHER = (
    b'#!/bin/sh\nexport WEBOS_COMPOSITOR_OPTIONS="--tty=1"\n\n'
    b"# For PVR GPU\nexport WESTON_FORCE_RENDERER=\n\n"
    b"exec /usr/bin/surface-manager $WEBOS_COMPOSITOR_OPTIONS\n"
)
# The launcher as the helper binds it: LG's punch-through hole clear on, nothing else changed.
HOLE_CLEAR = LAUNCHER.replace(b"WESTON_FORCE_RENDERER=\n", b"WESTON_FORCE_RENDERER=1\n")
spec = importlib.util.spec_from_file_location(
    "gl_integration", TOP / "tests/integration/layers/check-gl-integration.py"
)
integration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integration)


class TestNativeWestonLauncherTest:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.root = tmp_path
        self.launcher = self.root / "usr/bin/surface-manager.sh"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(LAUNCHER)
        self.launcher.chmod(0o755)
        self.runtime = self.root / "run/beanjar/native-weston"
        self.commands = self.root / "commands"
        self.commands.mkdir()
        # Emulate a bind by retaining the source's contents and mount ownership.
        # No root privilege, real mount, firmware or device is needed.
        for name, body in {
            "mountpoint": '[ -f "$2.bound" ]',
            "mount": 'printf "%s\\n" "$*" >> "$MOUNT_LOG"\ncp "$2" "$3"\ntouch "$3.bound"',
        }.items():
            command = self.commands / name
            command.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
            command.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=str(self.commands) + os.pathsep + os.environ["PATH"],
            MOUNT_LOG=str(self.root / "mount.log"),
        )

    def run_helper(self, check=True):
        return subprocess.run(
            ["/bin/sh", str(HELPER), str(self.root)],
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
        )

    def test_launcher_gets_only_the_hole_clear_and_restarts_idempotently(self):
        self.run_helper()
        assert self.launcher.read_bytes() == HOLE_CLEAR
        assert os.access(self.runtime / "surface-manager.sh", os.X_OK)
        mount_log = (self.root / "mount.log").read_bytes()
        assert mount_log.count(b"surface-manager.sh") == 2
        self.run_helper()
        assert (self.root / "mount.log").read_bytes() == mount_log
        assert not (self.runtime / "surface-manager.sh.tmp").exists()

    def test_firmware_weston_ini_is_left_alone(self):
        # The stock gbm-format (abgr8888 on spacebuns) has a Mesa GBM config (Mesa patch 0006)
        ini = self.root / "usr/share/weston/weston.ini"
        ini.parent.mkdir(parents=True)
        stock = b"[core]\ngbm-format=abgr8888\noverride-output-from-env=true\n"
        ini.write_bytes(stock)
        self.run_helper()
        assert ini.read_bytes() == stock
        assert "weston.ini" not in (self.root / "mount.log").read_text()

    def test_mount_failure_can_retry_from_original(self):
        mount = self.commands / "mount"
        original = mount.read_text()
        mount.write_text("#!/bin/sh\nexit 1\n")
        assert self.run_helper(check=False).returncode != 0
        assert self.launcher.read_bytes() == LAUNCHER
        mount.write_text(original)
        self.run_helper()
        assert self.launcher.read_bytes() == HOLE_CLEAR

    @pytest.mark.parametrize(
        "data",
        (
            LAUNCHER.replace(b"WESTON_FORCE_RENDERER=\n", b"WESTON_FORCE_RENDERER=1\n"),
            LAUNCHER.replace(b"export WESTON_FORCE_RENDERER=\n", b""),
            LAUNCHER + b"export WESTON_FORCE_RENDERER=\n",
        ),
        ids=["already-set", "missing", "twice"],
    )
    def test_unexpected_launcher_binds_nothing(self, data):
        self.launcher.write_bytes(data)
        result = self.run_helper(check=False)
        assert result.returncode != 0
        assert "unexpected compositor launcher" in result.stderr
        assert self.launcher.read_bytes() == data
        assert not (self.root / "mount.log").exists()
        assert not (self.runtime / "surface-manager.sh").exists()
        assert not (self.runtime / "surface-manager.sh.tmp").exists()

    def test_existing_foreign_launcher_mount_is_preserved(self):
        Path(str(self.launcher) + ".bound").touch()
        result = self.run_helper(check=False)
        assert result.returncode != 0
        assert "launcher already mounted by another owner" in result.stderr
        assert self.launcher.read_bytes() == LAUNCHER
        assert not (self.root / "mount.log").exists()


class TestGlIntegrationBoundaryTest:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.layer = tmp_path / "gl"
        self.layer.mkdir()
        for name in (
            "libEGL.so.1",
            "libGLESv2.so.2",
            "libGLESv1_CM.so.1",
            "libgbm.so.1",
            "libdrm.so.2",
        ):
            path = self.layer / "usr/lib" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        for relative in integration.PERMISSIONS:
            target = self.layer / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(integration.ROOTFS / relative, target)

    def native(self, target):
        shutil.copytree(integration.NATIVE_ROOTFS, self.layer, dirs_exist_ok=True)
        if target == integration.NATIVE_CURSOR_TARGET:
            shutil.copytree(integration.NATIVE_CURSOR_ROOTFS, self.layer, dirs_exist_ok=True)
            (self.layer / integration.CURSOR_LIBRARY).touch()
        if target == integration.NATIVE_TV_TARGET:
            shutil.copytree(integration.NATIVE_TV_ROOTFS, self.layer, dirs_exist_ok=True)
        profile = self.layer / "usr/share/beanjar/native-weston-target"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(target + "\n")

    def test_native_profile_and_owned_output_helper(self):
        self.native("o22n2-spacebuns")
        integration.check(self.layer, "native", target="o22n2-spacebuns")
        with pytest.raises(ValueError, match="target marker"):
            integration.check(self.layer, "native", target="o22n2-ombre")
        extra = self.layer / "etc/output-policy"
        extra.write_text("mode=768x2160\n")
        with pytest.raises(ValueError, match="outside owned bootstrap"):
            integration.check(self.layer, "native", target="o22n2-spacebuns")

    def test_native_helper_mutation_is_rejected(self):
        self.native("o22n2-spacebuns")
        helper = self.layer / integration.NATIVE_BOOTSTRAP[0]
        helper.write_text(helper.read_text().replace("RENDERER=1", "RENDERER=2"))
        with pytest.raises(ValueError, match="changed native launcher bootstrap"):
            integration.check(self.layer, "native", target="o22n2-spacebuns")

    def test_native_tv_head_words_are_spacebuns_only(self):
        self.native("o22n2-spacebuns")
        words = self.layer / integration.KERNEL_CMDLINE
        assert b"virtio_gpu.overlay_plane=1" in words.read_bytes()
        words.write_text("virtio_gpu.tv_connector=1\n")
        with pytest.raises(ValueError, match="changed native TV head kernel words"):
            integration.check(self.layer, "native", target="o22n2-spacebuns")
        words.unlink()
        with pytest.raises(ValueError, match="changed native TV head kernel words"):
            integration.check(self.layer, "native", target="o22n2-spacebuns")

    def test_native_spacebuns_requires_unchanged_cursor_route(self):
        self.native("o22n2-spacebuns")
        library = self.layer / integration.CURSOR_LIBRARY
        library.unlink()
        with pytest.raises(ValueError, match="lost measured cursor bridge"):
            integration.check(self.layer, "native", target="o22n2-spacebuns")
        library.touch()
        launcher = self.layer / integration.NATIVE_CURSOR[0]
        launcher.chmod(0o644)
        with pytest.raises(ValueError, match="launcher is not executable"):
            integration.check(self.layer, "native", target="o22n2-spacebuns")
        launcher.chmod(0o755)
        launcher.write_text(launcher.read_text().replace("${LD_PRELOAD:+:$LD_PRELOAD}", ""))
        with pytest.raises(ValueError, match="changed native cursor launch contract"):
            integration.check(self.layer, "native", target="o22n2-spacebuns")

    @pytest.mark.parametrize(
        "target",
        ("k8hp-spacebuns", "o22n2-ombre", "k8hp-queue"),
        ids=["k8hp-spacebuns", "o22n2-ombre", "k8hp-queue"],
    )
    def test_other_native_targets_do_not_acquire_cursor_bridge(self, target):
        self.native(target)
        integration.check(self.layer, "native", target=target)
        library = self.layer / integration.CURSOR_LIBRARY
        library.touch()
        with pytest.raises(ValueError, match="other native target acquired cursor bridge"):
            integration.check(self.layer, "native", target=target)
        library.unlink()
        shutil.copytree(integration.NATIVE_CURSOR_ROOTFS, self.layer, dirs_exist_ok=True)
        with pytest.raises(ValueError, match="other native target acquired cursor bridge"):
            integration.check(self.layer, "native", target=target)
        for relative in integration.NATIVE_CURSOR:
            (self.layer / relative).unlink()

    @pytest.mark.parametrize(
        "relative", integration.ADAPTERS + ("usr/lib/libkms-native-input-qt.so",)
    )
    def test_native_spacebuns_still_rejects_scanout_and_input_preloads(self, relative):
        self.native("o22n2-spacebuns")
        path = self.layer / relative
        path.touch()
        with pytest.raises(ValueError, match="native compositor acquired adapter"):
            integration.check(self.layer, "native", target="o22n2-spacebuns")
        path.unlink()

    def test_native_accepts_only_the_known_jail_preload(self):
        self.native("o22n2-spacebuns")
        dropin = self.layer / integration.JAIL_DROPIN
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_bytes(b"[Service]\n" + integration.JAIL_PRELOAD)
        integration.check(self.layer, "native", target="o22n2-spacebuns")
        for body in (
            b"[Service]\nEnvironment=LD_PRELOAD=/usr/lib/libevil.so\n",
            b"[Service]\n" + integration.JAIL_PRELOAD + integration.JAIL_PRELOAD,
            b"[Service]\n" + integration.JAIL_PRELOAD + b"Environment=DISPLAY_ID=1\n",
        ):
            dropin.write_bytes(body)
            with pytest.raises(ValueError, match="policy overridden"):
                integration.check(self.layer, "native", target="o22n2-spacebuns")
        dropin.unlink()
        other = self.layer / "etc/other.conf"
        other.write_bytes(integration.JAIL_PRELOAD)
        with pytest.raises(ValueError, match="policy overridden"):
            integration.check(self.layer, "native", target="o22n2-spacebuns")

    def test_adapted_queue_and_ombre_contract(self):
        shutil.copytree(integration.ROOTFS, self.layer, dirs_exist_ok=True)
        for name in ("libegl-kms.so", "libkms-cursor-compat.so", "libstarfishsurface.so.0"):
            (self.layer / "usr/lib" / name).touch()
        for target in ("k8hp-queue", "o22n2-ombre"):
            integration.check(self.layer, "adapted", target=target)
        profile = self.layer / "usr/share/beanjar/native-weston-target"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text("o22n2-spacebuns\n")
        with pytest.raises(ValueError, match="native target marker"):
            integration.check(self.layer, "adapted", target="k8hp-queue")


def test_profile_preload_and_stock_launcher_contract(tmp_path):
    root = tmp_path
    library = root / "cursor.so"
    stock = root / "stock.so"
    source = root / "empty.c"
    source.write_text("int fixture(void) { return 0; }\n")
    subprocess.run(["cc", "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    shutil.copy2(library, stock)
    profile = root / "target"
    launcher = root / "launch"
    launcher.write_text(
        (integration.NATIVE_CURSOR_ROOTFS / integration.NATIVE_CURSOR[0])
        .read_text()
        .replace("/usr/share/beanjar/native-weston-target", str(profile))
        .replace("/usr/lib/libkms-cursor-compat.so", str(library))
    )
    # The measured stock surface-manager.sh appends libsystrim before
    # exec. Use a harmless host DSO to exercise that exact env behavior.
    compositor = root / "stock-launch"
    compositor.write_text(
        "#!/bin/sh\n"
        "export LD_PRELOAD=$LD_PRELOAD:" + str(stock) + "\n"
        'printf "%s\\n" "$LD_PRELOAD" "$WEBOS_COMPOSITOR_GEOMETRY" "$@"\n'
        "exit 17\n"
    )
    compositor.chmod(0o755)
    for target in ("o22n2-spacebuns", "k8hp-spacebuns", "o22n2-ombre", "k8hp-queue"):
        profile.write_text(target + "\n")
        for inherited in (None, str(stock), str(stock) + " " + str(stock)):
            env = dict(os.environ, WEBOS_COMPOSITOR_GEOMETRY="1920x1080+0+0r0s1")
            env.pop("LD_PRELOAD", None)
            if inherited is not None:
                env["LD_PRELOAD"] = inherited
            result = subprocess.run(
                [
                    "/bin/sh",
                    str(launcher),
                    str(compositor),
                    "--socket=wayland-0",
                    "argument with spaces",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 17, result.stderr
            prefix = inherited or ""
            if target == integration.NATIVE_CURSOR_TARGET:
                prefix = str(library) + (":" + prefix if prefix else "")
            assert result.stdout.splitlines() == [
                prefix + ":" + str(stock),
                env["WEBOS_COMPOSITOR_GEOMETRY"],
                "--socket=wayland-0",
                "argument with spaces",
            ]
            assert result.stderr == ""
    profile.write_text("o22n2-spacebuns\n")
    library.unlink()
    result = subprocess.run(
        ["/bin/sh", str(launcher), str(compositor)], capture_output=True, timeout=5
    )
    assert result.returncode != 0
    assert result.stdout == b""
