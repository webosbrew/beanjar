#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build and run native KMS presentation boundary tests without a device."""

import os
import pathlib
import shlex
import subprocess
import tempfile

import pytest


def test_native_scanout():
    root = pathlib.Path(__file__).resolve().parents[3]
    flags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "libdrm"], capture_output=True, text=True
    )
    if flags.returncode:
        pytest.skip("libdrm development headers unavailable")
    with tempfile.TemporaryDirectory(prefix="beanjar-native-kms-") as directory:
        test = pathlib.Path(directory) / "check-native-scanout"
        subprocess.run(
            [
                "cc",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-g",
                str(root / "tests/unit/gl/check-native-scanout.c"),
                *shlex.split(flags.stdout),
                "-ldl",
                "-lpthread",
                "-o",
                str(test),
            ],
            check=True,
        )
        subprocess.run([str(test)], check=True, timeout=20)
        # Exercise the production constructor guard and self-preload removal too;
        # the C boundary fixture intentionally replaces the DRM device queries.
        library = pathlib.Path(directory) / "libkms-native-scanout.so"
        subprocess.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(root / "guest/layers/gl/src/kms-native-scanout.c"),
                *shlex.split(flags.stdout),
                "-ldl",
                "-lpthread",
                "-o",
                str(library),
            ],
            check=True,
        )
        source = pathlib.Path(directory) / "scope.c"
        source.write_text(
            "#include <stdio.h>\n#include <stdlib.h>\n"
            'int main(void) { const char *names[] = {"QT_QPA_EGLFS_INTEGRATION", '
            '"WEBOS_COMPOSITOR_DISPLAYS", "WEBOS_COMPOSITOR_EXTRA_WINDOWS", '
            '"LD_PRELOAD", "WEBOS_CURSOR_VISIBLE_EVENT"}; for (unsigned i=0;i<5;i++) { const char *v=getenv(names[i]); '
            'puts(v?v:""); } return 0; }\n'
        )
        probe = pathlib.Path(directory) / "scope-probe"
        compositor = pathlib.Path(directory) / "surface-manager"
        subprocess.run(["cc", str(source), "-o", str(probe)], check=True)
        compositor.write_bytes(probe.read_bytes())
        compositor.chmod(0o755)
        env = dict(
            os.environ,
            LD_PRELOAD=f"{library}:libm.so.6",
            BEANJAR_NATIVE_KMS="1",
            QT_QPA_EGLFS_INTEGRATION="original",
            WEBOS_COMPOSITOR_DISPLAYS="2",
            WEBOS_COMPOSITOR_EXTRA_WINDOWS="original-windows",
            WEBOS_CURSOR_VISIBLE_EVENT="imspecialkeys",
        )

        def output(binary):
            return subprocess.run(
                [str(binary)], env=env, check=True, capture_output=True, text=True, timeout=5
            ).stdout.splitlines()

        assert output(probe) == [
            "original",
            "2",
            "original-windows",
            env["LD_PRELOAD"],
            "imspecialkeys",
        ]
        assert output(compositor) == ["eglfs_kms_webos", "1", "[]", "libm.so.6", ""]
        # view: the adapter alone, the compositor's own integration and configuration kept
        env["BEANJAR_NATIVE_KMS"] = "view"
        assert output(compositor) == [
            "original",
            "2",
            "original-windows",
            "libm.so.6",
            "imspecialkeys",
        ]
        env.pop("BEANJAR_NATIVE_KMS")
        assert output(compositor) == [
            "original",
            "2",
            "original-windows",
            env["LD_PRELOAD"],
            "imspecialkeys",
        ]
    print("native scanout: production executable/opt-in guard and preload inheritance PASS")
