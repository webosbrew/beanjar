# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The gl layer as staged (check-gl-integration.py): the native configuration bootstrap and the
adapter configuration unchanged for existing compositors. Run by the guest build lane (its
artefacts.json: gl_layer, gl_integration, target, build_root; guest/layers/gl/meson.build), skipped
elsewhere."""

import sys
from pathlib import Path


def test_gl_integration(artefact, artefacts, run_check):
    run_check(
        [
            sys.executable,
            Path(__file__).with_name("check-gl-integration.py"),
            artefact("gl_layer"),
            "--integration",
            artefacts["gl_integration"],
            "--target",
            artefacts["target"],
            "--build",
            artefacts["build_root"],
        ],
        30,
    )
