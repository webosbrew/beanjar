# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The sandbox lls._run runs the firmware's generators in, without firmware: a host script stands
in for qemu-arm inside the real bwrap. The program's shell finds beanjar's stand-ins and no host
program; what it leaves running dies with it; a hung program is stopped with its stderr shown."""

import os
import pathlib
import shutil
import subprocess
import tempfile
import time
from unittest import mock

import pytest
from beanjar import paths
from beanjar.board import lls

pytestmark = pytest.mark.skipif(
    not shutil.which("bwrap")
    or subprocess.run(["bwrap", "--bind", "/", "/", "true"], check=False).returncode,
    reason="bwrap cannot run here",
)


@pytest.fixture
def work():
    """Not under /tmp, which the sandbox replaces with the run's own: where lls works, build/lls."""
    os.makedirs(paths.TREE.cache("lls"), exist_ok=True)
    d = tempfile.mkdtemp(prefix="test-sandbox-", dir=paths.TREE.cache("lls"))
    yield pathlib.Path(d)
    shutil.rmtree(d, ignore_errors=True)


def sandbox(work, script):
    """(run, marker): run(**kw) is lls._run with qemu-arm replaced by script (sh, its
    arguments ignored); marker is a path the script's background children carry in argv."""
    sysroot, tmp = work / "root", work / "tmp"
    sysroot.mkdir()
    tmp.mkdir()
    (work / "cmdline").write_text("debugMode=3\n")
    marker = str(work / "sandbox-child")
    os.symlink(shutil.which("sleep"), marker)
    qemu = work / "qemu-arm"
    qemu.write_text("#!/bin/sh\n" + script.replace("CHILD", marker))
    qemu.chmod(0o755)
    which = shutil.which

    def run(**kw):
        with mock.patch.object(
            lls.shutil, "which", lambda t: str(qemu) if t == "qemu-arm" else which(t)
        ):
            return lls._run(str(sysroot), str(work / "cmdline"), str(tmp), ["/prog"], **kw)

    return run, marker


def alive(marker):
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln.startswith(marker)]


def test_only_the_stand_ins(work):
    run, _ = sandbox(
        work,
        "echo PATH=$PATH\n"
        "nyx-cmd OSInfo query webos_release; echo rc=$?\n"
        "nyx-cmd OSInfo query webos_release_platformcode; echo rc=$?\n"
        "nyx-cmd OSInfo query core_os_name; echo rc=$?\n"
        "initctl emit --no-wait nyxconfig-data-ready; echo rc=$?\n"
        "ls /; echo rc=$?\n",
    )
    bindir = str(work / "bin")
    lls._helpers(bindir, {"webos_release": (0, "6.5.2\n"), "webos_release_platformcode": (1, "")})
    rc, out, err = run(bindir=bindir)
    assert rc == 0
    assert out.splitlines() == [
        "PATH=" + bindir,
        "6.5.2",
        "rc=0",
        "rc=1",  # the firmware's nyx-cmd failed for this one: so does the stand-in
        "rc=1",  # never asked of the firmware's: refused
        "rc=0",
        "rc=127",  # no host program
    ]
    assert "not found" in err


def test_no_nyx_cmd_without_answers(work):
    run, _ = sandbox(work, "nyx-cmd OSInfo query webos_release; echo rc=$?\n")
    rc, out, _ = run()  # the default: bin beside the root, initctl only
    assert (rc, out) == (0, "rc=127\n")
    assert sorted(os.listdir(work / "bin")) == ["initctl"]


def test_private_dev(work):
    # the reported hang in a chroot: syslog() blocked on a /dev/log socket nothing reads
    name = "beanjar-test-" + work.name
    run, _ = sandbox(
        work,
        "for f in /dev/shm/* /dev/shm/.[!.]*; do test -e $f && echo $f; done\n"
        "test -e /dev/log || test -L /dev/log; echo log=$?\n"
        f"test -c /dev/null && test -c /dev/urandom && : > /dev/shm/{name}; echo dev=$?\n",
    )
    rc, out, _ = run()
    assert (rc, out.split()) == (0, ["log=1", "dev=0"])
    assert not os.path.exists("/dev/shm/" + name)


def test_children_die_with_it(work):
    # the reported hang: a helper leaves a child holding the output pipes
    run, marker = sandbox(work, "CHILD 300 &\nCHILD 301 </dev/null >/dev/null 2>&1 &\nexit 3\n")
    t = time.monotonic()
    rc, _, _ = run()
    assert rc == 3
    assert time.monotonic() - t < 10
    assert not alive(marker)


def test_hung_program_stopped(work):
    run, marker = sandbox(work, "echo generator stderr tail >&2\nCHILD 300 &\nexec CHILD 301\n")
    t = time.monotonic()
    with pytest.raises(lls.Error, match="no exit in 2 s.*generator stderr tail"):
        run(timeout=2)
    assert time.monotonic() - t < 10
    time.sleep(0.5)
    assert not alive(marker)
