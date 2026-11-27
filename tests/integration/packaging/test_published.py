# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What the build publishes (publish_dir): packaging/shippable.py, nothing of one install in the
initramfs and the layers (no key, secret or netproxy layer; skipped, 77, until the guest build has
published an initramfs), and check-gl-ffi.py, the GL layer's libffi ABI for its Wayland client."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def test_shippable(artefact, run_check):
    run_check(
        [sys.executable, HERE.parents[2] / "packaging/shippable.py", artefact("publish_dir")], 60
    )


def test_gl_ffi(artefact, run_check):
    run_check(
        [
            sys.executable,
            HERE / "check-gl-ffi.py",
            artefact("publish_dir"),
            "--target",
            "k8lp-mullet",
        ],
        60,
    )
