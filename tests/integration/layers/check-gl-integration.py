#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check the staged renderer boundary before packaging a GL component.

Spacebuns' stock Weston owns GBM and its output policy. The adapted samples
(ombre Qt and queue Weston) retain their existing compositor overrides.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

TOP = Path(__file__).resolve().parents[3]
ROOTFS = TOP / "guest/layers/gl/rootfs"
NATIVE_ROOTFS = TOP / "guest/layers/gl/native-rootfs"
NATIVE_CURSOR_ROOTFS = TOP / "guest/layers/gl/native-cursor-rootfs"
NATIVE_CURSOR_TARGET = "o22n2-spacebuns"
# Spacebuns' kernel words: one TV connector whose CRTC has an overlay plane, as a set's
NATIVE_TV_ROOTFS = TOP / "guest/layers/gl/native-tv-rootfs"
NATIVE_TV_TARGET = "o22n2-spacebuns"
KERNEL_CMDLINE = "usr/share/beanjar/kernel.cmdline"
NATIVE_CURSOR = (
    "usr/libexec/beanjar/native-cursor-launch",
    "etc/systemd/system/surface-manager-daemon.service.d/emulator-native-cursor.conf",
)
CURSOR_LIBRARY = "usr/lib/libkms-cursor-compat.so"
NATIVE_BOOTSTRAP = (
    "usr/libexec/beanjar/native-weston-format",
    "etc/systemd/system/surface-manager-daemon.service.d/emulator-native-format.conf",
)
# The jail hooks' preload for sam: the stock libsystrim first, then the two hooks.
JAIL_DROPIN = "etc/systemd/system/sam.service.d/zz-beanjar-jail.conf"
JAIL_PRELOAD = (
    b"Environment=LD_PRELOAD=/usr/lib/libsystrim.so.3"
    b":/usr/lib/libjail-gpu.so:/usr/lib/libjail-trust.so\n"
)
PERMISSIONS = (
    "etc/init/gl-devperms.conf",
    "etc/systemd/system/basic.target.d/emulator-gl-devperms.conf",
    "etc/systemd/system/emulator-gl-devperms.service",
)
ADAPTERS = (
    "usr/lib/libegl-kms.so",
    "usr/lib/libkms-native-scanout.so",
    "usr/lib/libkms-native-input.so",
    "usr/lib/libstarfishsurface.so.0",
)


def check(layer, integration, build=None, target=None):
    for name in (
        "libEGL.so.1",
        "libGLESv2.so.2",
        "libGLESv1_CM.so.1",
        "libgbm.so.1",
        "libdrm.so.2",
    ):
        if not (layer / "usr/lib" / name).is_file():
            raise ValueError(f"missing renderer library {name}")
    if (layer / "usr/lib/libwayland-server.so.0").exists():
        raise ValueError("GL layer replaces the stock Wayland server")
    for relative in PERMISSIONS:
        if (layer / relative).read_bytes() != (ROOTFS / relative).read_bytes():
            raise ValueError(f"changed device-permission contract: {relative}")

    profile = layer / "usr/share/beanjar/native-weston-target"
    if integration == "native":
        configured_target = profile.read_text().strip()
        if not configured_target or (target is not None and configured_target != target):
            raise ValueError("native compositor target marker differs from build")
        cursor_bridge = configured_target == NATIVE_CURSOR_TARGET
        if cursor_bridge:
            if not (layer / CURSOR_LIBRARY).is_file():
                raise ValueError("native Spacebuns lost measured cursor bridge")
            for relative in NATIVE_CURSOR:
                if (layer / relative).read_bytes() != (
                    NATIVE_CURSOR_ROOTFS / relative
                ).read_bytes():
                    raise ValueError(f"changed native cursor launch contract: {relative}")
            if not (layer / NATIVE_CURSOR[0]).stat().st_mode & 0o111:
                raise ValueError("native cursor launcher is not executable")
        else:
            for relative in NATIVE_CURSOR + (CURSOR_LIBRARY,):
                if (layer / relative).exists():
                    raise ValueError(f"other native target acquired cursor bridge: {relative}")
        words = layer / KERNEL_CMDLINE
        if configured_target == NATIVE_TV_TARGET:
            if (
                not words.is_file()
                or words.read_bytes() != (NATIVE_TV_ROOTFS / KERNEL_CMDLINE).read_bytes()
            ):
                raise ValueError("changed native TV head kernel words")
        elif words.exists():
            raise ValueError("other native target acquired TV head kernel words")
        for name in ADAPTERS + (
            "usr/lib/libkms-native-input-qt.so",
            "etc/default/egl-kms",
            "usr/libexec/beanjar/weston-setup",
            "etc/init/surface-manager.override",
        ):
            if (layer / name).exists():
                raise ValueError(f"native compositor acquired adapter: {name}")
        dropins = layer / "etc/systemd/system/surface-manager-daemon.service.d"
        expected_dropins = ["emulator-native-format.conf"]
        if cursor_bridge:
            expected_dropins.append("emulator-native-cursor.conf")
        if sorted(path.name for path in dropins.iterdir()) != sorted(expected_dropins):
            raise ValueError("native compositor acquired unrelated service dropins")
        for relative in NATIVE_BOOTSTRAP:
            if (layer / relative).read_bytes() != (NATIVE_ROOTFS / relative).read_bytes():
                raise ValueError(f"changed native format bootstrap: {relative}")
        if not (layer / NATIVE_BOOTSTRAP[0]).stat().st_mode & 0o111:
            raise ValueError("native format bootstrap is not executable")
        if list(layer.rglob("weston.ini")):
            raise ValueError("native GL layer ships a firmware configuration")
        for path in list((layer / "etc").rglob("*")) + list((layer / "usr/libexec").rglob("*")):
            forbidden = (b"WESTON_DISABLE_ATOMIC", b"DISPLAY_ID")
            if not (cursor_bridge and path == layer / NATIVE_CURSOR[0]):
                forbidden += (b"LD_PRELOAD",)
            content = path.read_bytes() if path.is_file() else b""
            if path == layer / JAIL_DROPIN:
                # Exactly the one known preload line; anything else in the file is still checked.
                content = content.replace(JAIL_PRELOAD, b"", 1)
            if any(word in content for word in forbidden):
                raise ValueError(f"native compositor policy overridden by {path}")
            if (
                path.is_file()
                and path != layer / NATIVE_BOOTSTRAP[0]
                and b"768x2160" in path.read_bytes()
            ):
                raise ValueError(f"native output configuration outside owned bootstrap: {path}")
        if build:
            dependencies = json.loads((build / "meson-info/intro-dependencies.json").read_text())
            if any(item["name"].startswith("Qt") for item in dependencies):
                raise ValueError("native GL configuration acquired a Qt dependency")
            targets = json.loads((build / "meson-info/intro-targets.json").read_text())
            if any(item["name"] == "kms-native-input-qt" for item in targets):
                raise ValueError("native GL configuration builds the Qt companion")
            if any(
                item["name"]
                in ("egl-kms", "kms-native-scanout", "kms-native-input", "starfishsurface")
                for item in targets
            ):
                raise ValueError("native GL configuration builds a scanout/input adapter")
            if any(item["name"] == "kms-cursor-compat" for item in targets) != cursor_bridge:
                raise ValueError("native cursor build target differs from scoped profile")
    else:
        if profile.exists():
            raise ValueError("adapted compositor acquired native target marker")
        for relative in NATIVE_BOOTSTRAP + NATIVE_CURSOR:
            if (layer / relative).exists():
                raise ValueError(f"adapted compositor acquired native bootstrap: {relative}")
        # Existing queue and Qt profiles must preserve the entire supplied adapter
        # configuration, including the helper that gates initramfs Weston setup.
        for source in ROOTFS.rglob("*"):
            if (
                source.is_file()
                and (layer / source.relative_to(ROOTFS)).read_bytes() != source.read_bytes()
            ):
                raise ValueError(f"adapted compositor configuration changed: {source}")
        # These are present in both measured queue and ombre artifacts; the
        # later ombre-only input bridge is not a queue baseline requirement.
        for name in (
            "usr/lib/libegl-kms.so",
            "usr/lib/libkms-cursor-compat.so",
            "usr/lib/libstarfishsurface.so.0",
        ):
            if not (layer / name).is_file():
                raise ValueError(f"adapted compositor lost adapter: {name}")

    mali = layer / "usr/lib/libmali.so.0"
    # The measured native Spacebuns consumer needs this SONAME/provider route;
    # older adapted Mali profiles have different SONAME/dependency contracts.
    if integration == "native" and mali.is_file():
        dynamic = subprocess.check_output(["readelf", "-d", str(mali)], text=True)
        for name in ("libmali.so.0", "libEGL.so.1", "libGLESv2.so.2", "libwayland-egl.so.1"):
            if f"[{name}]" not in dynamic:
                raise ValueError(f"Mali SONAME/provider dependency missing: {name}")
    # Every Mali carries GLES 1 in its one library; LG's GLES 1 wrapper NEEDs only libmali.
    if mali.is_file():
        dynamic = subprocess.check_output(["readelf", "-d", str(mali)], text=True)
        if "[libGLESv1_CM.so.1]" not in dynamic:
            raise ValueError("Mali stand-in lacks the GLES 1 provider libGLESv1_CM.so.1")
    # With the vendor's window-config rule (gl_vendor_window_rgba) the vendor library is every
    # libEGL name, and reaches Mesa's libEGL by its own name, never by libEGL.so.1 (itself).
    egl = layer / "usr/lib/libEGL.so.1"
    if egl.is_symlink() and not os.readlink(egl).startswith("libEGL."):
        target = os.readlink(egl)
        for name in ("libEGL.so", "libEGL.so.1.4"):
            if os.readlink(layer / "usr/lib" / name) != target:
                raise ValueError(f"{name} is not the vendor library {target}")
        if not (layer / "usr/lib/libEGL-mesa.so.1").is_file():
            raise ValueError("vendor libEGL without Mesa's libEGL-mesa.so.1")
        dynamic = subprocess.check_output(["readelf", "-d", str(egl)], text=True)
        if "[libEGL-mesa.so.1]" not in dynamic or "soname: [libEGL.so.1]" not in dynamic:
            raise ValueError("vendor libEGL must be SONAME libEGL.so.1 NEEDing libEGL-mesa.so.1")
    print(f"{integration} compositor integration verified: {layer}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layer", type=Path)
    parser.add_argument("--integration", choices=("native", "adapted"), required=True)
    parser.add_argument("--build", type=Path)
    parser.add_argument("--target")
    args = parser.parse_args()
    check(args.layer, args.integration, args.build, args.target)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
