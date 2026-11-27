#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The guest payload ships every kernel module the host loads from it: beanjar-guest and the host sync
script take their list from targets.payload_modules(), which names lgemu and every SocModule of targets.py
(what compat.module_params looks for as build/<module>.ko)."""

import sys
from pathlib import Path

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar.firmware import targets


def test_every_targets_module_is_shipped():
    shipped = set(targets.payload_modules())
    for chip, soc in targets.SOCS.items():
        for module in soc.modules:
            assert module.name in shipped, f"{chip}: {module.name}.ko"
    assert "lgemu" in shipped
    assert "dtv_soc" in shipped
    # lgfs.ko loads from the initramfs module tree (PERIPHERAL_MODULES), not the payload
    assert "lgfs" not in shipped


def test_senders_use_the_one_list():
    build = (TOP / "packaging/debian/build.py").read_text()
    assert "targets.payload_modules()" in build
    # the maintainer's host sync script is not part of every checkout
    scripts = sorted((TOP / "host/scripts").glob("sync-*.sh"))
    sync = scripts[0].read_text() if scripts else None
    if sync is not None:
        assert "targets.payload_modules()" in sync
    for name in ("lg115x", "btmtk_usb", "webos_tv"):
        if sync is not None:
            assert f"/{name}.ko" not in sync
        assert f'"{name}"' not in build
