# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The viewer's front ends (host/viewer, host/ui's GTK viewer) against the stand-in QEMU display
(tests/fakes/fake-qemu.py), with this build's programs: no VM. Each check a program of its own;
its exit 77 is a skip."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def test_gtk(artefact, artefacts, run_check):
    """The GTK viewer's window on a private Xvfb display (docs/dev/architecture/viewer.md); skipped (77)
    without GTK 4 for Python."""
    # GTK_A11Y=none: GTK 4.18 would otherwise D-Bus-activate an at-spi bus on the session bus,
    # which outlives the test.
    env = {"GTK_A11Y": "none"}
    if artefacts.get("viewer_gresource"):
        env["BEANJAR_VIEWER_GRESOURCE"] = artefacts["viewer_gresource"]
    run_check(
        [
            artefact("xvfb_run"),
            "-a",
            "-s",
            "-screen 0 1920x1200x24",
            artefact("gtk_python"),
            HERE / "check-gtk.py",
            artefact("viewer_core"),
        ],
        120,
        env=env,
    )


def test_qemu_listener_close(artefact, run_check):
    """The tree's QEMU frees a D-Bus display listener whose connection closes; its own private bus."""
    run_check(
        [
            artefact("gtk_python"),
            HERE / "check-qemu-listener-close.py",
            artefact("qemu"),
            artefact("qemu_display_xml"),
        ],
        60,
    )


def test_audio_context(artefact, run_check):
    """Incoming guest audio stays independent of the display's default main context."""
    run_check(
        [artefact("gtk_python"), HERE / "check-audio-context.py", artefact("check_audio_context")],
        25,
    )


def test_attach(artefact, artefacts, run_check):
    """The C front ends attach whatever versions vm-info.toml records; beanjar-vnc only where
    it is built."""
    programs = [artefact("viewer_headless")] + ([artefacts["vnc"]] if artefacts.get("vnc") else [])
    run_check([sys.executable, HERE / "check-attach.py", *programs], 60)


def test_video_plane(artefact, run_check):
    """A shown video window without a picture: black with decoding on, the checkerboard only with
    decoding off."""
    run_check([sys.executable, HERE / "check-video-plane.py", artefact("viewer_headless")], 90)


def test_planes(artefact, run_check):
    """--hide-planes: the video, the UI and the cursor left out of a front end's picture."""
    run_check([sys.executable, HERE / "check-planes.py", artefact("viewer_headless")], 120)


def test_restart(artefact, run_check):
    """A guest restart: the core waits for the unit's next QEMU on the same bus."""
    run_check([sys.executable, HERE / "check-restart.py", artefact("viewer_headless")], 90)
