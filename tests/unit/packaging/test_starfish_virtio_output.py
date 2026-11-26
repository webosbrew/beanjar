#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The Qt EGLFS Starfish output helper, run under a root prefix: no mount, no firmware."""

from pathlib import Path
import subprocess

import pytest

TOP = Path(__file__).resolve().parents[3]
HELPER = TOP / "guest/layers/soc/eglfs-starfish/usr/libexec/beanjar/starfish-virtio-output"
# The launcher's configuration as in W24G 33.20.23 and 33.31.68 (and O24's, O22N3's).
PRODUCT = (
    b"export X=1\nexport WEBOS_COMPOSITOR_DISPLAYS=2\nexport QT_QPA_EGLFS_FB=/dev/fb0:/dev/fb1\n"
)
EGLFS = (
    b'WEBOS_COMPOSITOR_DISPLAY_CONFIG="$(printf \\\n'
    b'\'[{"device":"/dev/dri/card0","outputs":[{"name":"%s","format":"abgr8888","primary":true}\' \\\n'
    b'"$primary_name")"\n'
    b'    \'%s,{"name":"%s","geometry":"%s","format":"abgr8888"}]}]\' \\\n'
    b"export QT_QPA_EGLFS_KMS_ATOMIC=1\n"
)


class TestStarfishVirtioOutput:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.root = tmp_path
        self.conf = self.root / "etc/surface-manager.d"
        self.conf.mkdir(parents=True)
        (self.conf / "product.env").write_bytes(PRODUCT)
        (self.conf / "eglfs_starfish.env").write_bytes(EGLFS)
        self.runtime = self.root / "run/beanjar/starfish"

    def run_helper(self, check=True):
        return subprocess.run(
            ["/bin/sh", str(HELPER), str(self.root)], capture_output=True, check=check
        )

    def test_one_display_and_stock_output_formats(self):
        self.run_helper()
        assert (self.runtime / "product.env").read_bytes() == PRODUCT.replace(
            b"DISPLAYS=2", b"DISPLAYS=1"
        )
        # abgr8888 has a Mesa GBM config (Mesa patch 0006) and the plane scans it out
        assert (self.conf / "eglfs_starfish.env").read_bytes() == EGLFS
        assert not (self.runtime / "eglfs_starfish.env").exists()
        assert not list(self.runtime.glob("*.tmp"))

    def test_unexpected_displays_fail(self):
        (self.conf / "product.env").write_bytes(PRODUCT.replace(b"DISPLAYS=2", b"DISPLAYS=3"))
        assert self.run_helper(check=False).returncode != 0
