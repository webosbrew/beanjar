#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The media helper's confinement, for real: its own (check-media-sandbox, host/media/sandbox.c).
No VM, no GPU."""

import signal
import subprocess

import pytest


def test_self_confinement(artefact, tmp_path):
    """After hm_sandbox: its directory writable, a file outside unreadable (Landlock), no TCP, no
    fork, no signal out, a call off the list ENOSYS, and execve ends the process."""
    rw = tmp_path / "rw"
    rw.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("not the helper's\n")
    r = subprocess.run(
        [artefact("check_media_sandbox"), rw, outside],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode == 77:
        pytest.skip(r.stdout.strip().splitlines()[-1])
    lines = r.stdout.splitlines()
    assert "checks: ok" in lines, r.stdout
    assert r.returncode == -signal.SIGSYS, (r.returncode, r.stdout, r.stderr)
    assert (rw / "written").exists()
