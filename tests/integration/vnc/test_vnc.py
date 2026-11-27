# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar-vnc (host/vnc) against the stand-in QEMU display (tests/fakes/fake-qemu.py): no VM, no
window. Each check is a program of its own (check-*.py, run with the built beanjar-vnc), as it
starts and ends the server and the stand-ins itself; its exit 77 is a skip."""

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

# meson's old test name: (the check, its arguments past BEANJAR_VNC, seconds)
CHECKS = {
    "check-vnc": ("check-vnc.py", ["viewer_headless"], 90),
    # --video-local's H.264 (host/vnc/video.c) with x264, and NVENC where it opens
    "check-vnc-video": ("check-video.py", [], 90),
    # no frame work without clients, a fresh frame for the first one; its media step needs the
    # media-helper
    "check-vnc-idle": ("check-idle.py", [], 90),
    # the picture's rate from a steady GL source (fake-qemu --dmabuf): skipped without a render node
    "check-vnc-rate": ("check-rate.py", ["--seconds", "4"], 60),
    # Display1.Screenshot thumbnails, cached while nothing changes, and the headless fallback
    "check-vnc-thumbnail": ("check-thumbnail.py", ["viewer_headless"], 90),
    # the guest's capture answered while idle (fake-qemu --dmabuf): skipped without a render node
    "check-vnc-capture": ("check-capture.py", [], 60),
    # partial frames against fake-qemu --bar
    "check-vnc-damage": ("check-damage.py", [], 60),
    # the planes hidden by beanjar-web's beanjar:planes= marker
    "check-vnc-planes": ("check-planes.py", [], 60),
    # QEMU's Disable: a stray one before a scanout keeps the picture, a real one blanks it
    "check-vnc-disable": ("check-disable.py", [], 60),
    # the manager's vnc pipe, remote pipe and remote layout (host/core/beanjar/display/pipe.py)
    "check-vnc-pipe": ("check-pipe.py", [], 60),
}


@pytest.mark.parametrize("name", CHECKS)
def test_vnc(name, artefact, artefacts, run_check):
    script, args, timeout = CHECKS[name]
    vnc = artefact("vnc")
    argv = [sys.executable, HERE / script, vnc] + [
        artefact(a) if a == "viewer_headless" else a for a in args
    ]
    env = {"MEDIAHUB": artefacts["media_helper"]} if artefacts.get("media_helper") else {}
    run_check(argv, timeout, env=env)
