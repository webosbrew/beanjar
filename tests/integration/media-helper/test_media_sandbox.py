#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The media helper's confinement, for real: its own (check-media-sandbox, host/media/sandbox.c)
and the launcher's bubblewrap around it (mediasandbox.command), with the helper running as a VM
runs it: sockets and QEMU's pidfd passed open, its directory the only one it writes. No VM, no GPU.

bwrap's half skips where bubblewrap is missing or cannot make its namespaces."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar.runtime import launch, mediasandbox


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


def _children(pid):
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(x) for x in f.read().split()]
    except OSError:
        return []


def _helper_pid(top, exe):
    """The process under top (bwrap) running exe."""
    todo = [top]
    while todo:
        pid = todo.pop()
        try:
            if os.path.realpath(f"/proc/{pid}/exe") == os.path.realpath(exe):
                return pid
        except OSError:
            pass
        todo += _children(pid)
    return None


def test_bwrap(tmp_path):
    """The helper in its bubblewrap: it serves its sockets and reaches QEMU's in its directory,
    sees nothing else of the run directory (a QMP socket there, a file beside it), not $HOME, has
    its own network namespace, and ends when the process its pidfd names does (across the pid
    namespace)."""
    hub = os.environ.get("MEDIAHUB") or str(TOP / "build/media-helper/media-helper")
    if not os.access(hub, os.X_OK):
        pytest.skip(f"no {hub}")
    env = dict(os.environ)
    bwrap = mediasandbox.find(env)
    if not bwrap:
        pytest.skip("bubblewrap not installed")
    why = mediasandbox.problem(bwrap)
    if why:
        pytest.skip(f"bubblewrap cannot run here: {why}")
    run = tmp_path / "run"
    media = run / "media"
    media.mkdir(parents=True, mode=0o700)
    (run / "mitmweb.json").write_text('{"password": "secret"}')
    (tmp_path / "secret").write_text("not the helper's\n")
    qmp = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    qmp.bind(str(run / "qmp.sock"))
    qmp.listen(1)
    # QEMU's side of the ports, in the helper's directory
    qemu = {}
    for name in ("qemu-video.sock", "qemu-media-0.sock"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(media / name))
        s.listen(1)
        s.settimeout(10)
        qemu[name] = s
    viewers = []
    for name, kind in (("video.sock", socket.SOCK_STREAM), ("media-0.sock", socket.SOCK_SEQPACKET)):
        s = socket.socket(socket.AF_UNIX, kind)
        s.bind(str(run / name))
        s.listen(4)
        viewers.append(s)
    qemu_stand_in = subprocess.Popen(["sleep", "600"])
    pidfd = os.pidfd_open(qemu_stand_in.pid)
    argv = [
        hub,
        "--pid-fd",
        str(pidfd),
        "--log",
        str(media / "media-sessions.log"),
        "--decode-log",
        str(media / "media-helper.log"),
        "--streams",
        str(media / "media-streams"),
        "--video",
        f"{media}/qemu-video.sock:{run}/video.sock",
        "--media",
        f"{media}/qemu-media:{run}/media",
        "--ports",
        "1",
        "--hwdec",
        "none",
        "--viewer-fds",
        ",".join(str(s.fileno()) for s in viewers),
    ]
    helper_env = launch.media_helper_environment(env)
    cmd = mediasandbox.command(bwrap, hub, [media], helper_env, "none") + argv
    out = open(tmp_path / "media-helper.out", "wb")
    box = subprocess.Popen(
        cmd,
        env=mediasandbox.environment(helper_env),
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=out,
        pass_fds=[s.fileno() for s in viewers] + [pidfd],
    )
    os.close(pidfd)
    for s in viewers:
        s.close()
    try:
        # it reaches QEMU's sockets in its directory and serves the viewers' outside it
        guest = {name: s.accept()[0] for name, s in qemu.items()}
        v = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        v.connect(str(run / "video.sock"))
        v.close()
        pid = None
        for _ in range(50):
            pid = _helper_pid(box.pid, hub)
            if pid:
                break
            time.sleep(0.1)
        assert pid, "no helper under bwrap"
        root = f"/proc/{pid}/root"
        # its directory and nothing else of the run directory, the test's or the home directory
        assert os.path.exists(f"{root}{media}/media-helper.log")
        assert not os.path.exists(f"{root}{run}/qmp.sock")
        assert not os.path.exists(f"{root}{run}/mitmweb.json")
        assert not os.path.exists(f"{root}{tmp_path}/media-helper.out")
        assert not os.path.exists(f"{root}{tmp_path}/secret")
        # of the home directory only the way to what is bound (a checkout's helper and FFmpeg)
        home = os.path.realpath(os.path.expanduser("~"))
        if os.path.exists(f"{root}{home}"):
            bound = {
                os.path.relpath(os.path.realpath(p), home).split("/")[0]
                for p in (hub, os.path.join(os.path.dirname(hub), "..", "ffmpeg-helper"), media)
            }
            assert set(os.listdir(f"{root}{home}")) <= bound
        assert not os.path.exists(f"{root}/dev/dri") and not os.path.exists(f"{root}/dev/dxg")
        assert os.readlink(f"/proc/{pid}/ns/net") != os.readlink("/proc/self/ns/net")
        assert os.readlink(f"/proc/{pid}/ns/pid") != os.readlink("/proc/self/ns/pid")
        # QEMU's end ends the helper
        qemu_stand_in.kill()
        qemu_stand_in.wait()
        box.wait(10)
        for c in guest.values():
            c.close()
    finally:
        qemu_stand_in.kill()
        qemu_stand_in.wait()
        if box.poll() is None:
            box.kill()
            box.wait()
        out.close()
        qmp.close()
        for s in qemu.values():
            s.close()
    log = (media / "media-sessions.log").read_text()
    assert "sandbox: no_new_privs" in log, log
    assert "hub: the VM ended" in log, log
