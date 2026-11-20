# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The guest path's command (lls._guest_command, run by the preparation guest's shell) without a
guest: host stand-ins for chroot, a short limit. Each program runs bounded in the chroot on the
firmware's PATH; one stopped at the limit ends the command, and lls._guest_request makes that an
Error with the program's stderr."""

import os
import shutil
import subprocess
import time
from unittest import mock

import pytest

from beanjar import lls


def test_command(tmp_path):
    sysroot = str(tmp_path / "root")
    text = lls._guest_command(
        sysroot, [sysroot + "/usr/bin/nyx-option-gen", "-A", "x y"], {"LD_PRELOAD": "/p.so"}
    )
    assert " PATH=" + lls.GUEST_PATH + " MALLOC_PERTURB_=255 " in text
    assert " GLIBC_TUNABLES=glibc.malloc.tcache_count=0\n" in text
    assert "export LD_PRELOAD=/p.so\n" in text
    assert f"timeout {lls.GUEST_TIMEOUT} chroot /root " in text
    assert text.endswith("\nbounded /usr/bin/nyx-option-gen -A 'x y'\n")


def run(tmp_path, monkeypatch, program):
    """The command bounded program, in the host's sh, chroot a stand-in that runs its program."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "chroot").write_text('#!/bin/sh\nshift\nexec "$@"\n')
    (bindir / "chroot").chmod(0o755)
    monkeypatch.setattr(lls, "GUEST_TIMEOUT", 2)
    monkeypatch.setattr(lls, "GUEST_PATH", f"{bindir}:/usr/bin:/bin")
    text = lls._guest_command("/none", program)
    return subprocess.run(["sh", "-c", text], capture_output=True, text=True, timeout=20)


def test_exit_status_passes(tmp_path, monkeypatch):
    r = run(tmp_path, monkeypatch, ["sh", "-c", "echo out; exit 3"])
    assert (r.returncode, r.stdout) == (3, "out\n")


@pytest.mark.skipif(not shutil.which("timeout"), reason="no timeout")
def test_hung_program_stopped(tmp_path, monkeypatch):
    t = time.monotonic()
    r = run(tmp_path, monkeypatch, ["sh", "-c", "echo its tail >&2; exec sleep 30"])
    assert time.monotonic() - t < 10
    assert r.returncode == 124
    assert r.stderr.splitlines()[-1] == lls.GUEST_STOPPED + "sh: no exit in 2 s, stopped"
    assert "its tail" in r.stderr


def guest_request(tmp_path, rc, stderr):
    """lls._guest_request with QEMU replaced: the request ends with status rc and stderr."""
    sysroot, tmp = tmp_path / "root", tmp_path / "tmp"
    sysroot.mkdir()
    tmp.mkdir()
    (tmp_path / "cmdline").write_text("x\n")

    def fake_run(cmd, **kw):
        request = next(a for a in cmd if "mount_tag=prepare-request" in a)
        request = request.split("path=", 1)[1].split(",", 1)[0]
        for name, text in (("status", f"{rc}\n"), ("stdout", ""), ("stderr", stderr)):
            with open(os.path.join(request, name), "w") as f:
                f.write(text)
        return subprocess.CompletedProcess(cmd, 0, "")

    tree = mock.Mock()
    tree.qemu.return_value = ("qemu", [])
    tree.build.side_effect = lambda *p: "/".join(p)
    with mock.patch.object(lls.subprocess, "run", fake_run):
        return lls._guest_request(str(sysroot), str(tmp_path / "cmdline"), str(tmp), "", tree)


def test_stopped_is_an_error(tmp_path):
    with pytest.raises(
        lls.Error, match=r"^nyx-option-gen: no exit in 30 s, stopped; its stderr: x$"
    ):
        guest_request(
            tmp_path, 124, "x\n" + lls.GUEST_STOPPED + "nyx-option-gen: no exit in 30 s, stopped\n"
        )


def test_program_status_returned(tmp_path):
    assert guest_request(tmp_path, 124, "a program's own 124\n") == (
        124,
        "",
        "a program's own 124\n",
    )
