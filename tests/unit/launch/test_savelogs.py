# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Saving the guest's logs out of RUN_DIR/share/out (share.save, share.harvest, launch.Bg.save_logs)
when the guest has made links, FIFOs and sockets there: the guest is root and its out share is
read-write 9p with security_model=none, so they are real host ones. Nothing outside the share is
written into, nothing but regular files and directories is saved, and what is left out is listed
in the saved directory's skipped.txt."""

import os
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from beanjar import launch, share
from launchkit import TOP, VM

HOST_LOGS = {
    "webos.log": "host webos log\n",
    "console.log": "host console\n",
    "qemu.out": "qemu\n",
    "netstub.log": "netstub\n",
}


class Fixture:
    def __init__(self, tmp):
        self.tmp = tmp
        self.run_dir = tmp / "vm" / "run"
        self.log_dir = tmp / "vm" / "log"
        self.saved = tmp / "vm" / "logs" / "saved"
        self.out = tmp / "vm" / "run" / "share" / "out"
        self.logs = self.out / "logs"
        self.logs.mkdir(parents=True)
        self.log_dir.mkdir(parents=True)
        for name, text in HOST_LOGS.items():
            (self.log_dir / name).write_text(text)
        # what the host must never write into or lose: a user's file and directory
        self.home = tmp / "home"
        self.home.mkdir()
        self.bashrc = self.home / ".bashrc"
        self.bashrc.write_text("# the user's\n")
        self.secret_dir = tmp / "elsewhere"
        self.secret_dir.mkdir()
        (self.secret_dir / "keep").write_text("keep\n")
        (self.secret_dir / ".done").write_text("")

    def snapshot(self):
        """Every path outside the share with its kind and contents, to compare before and after."""
        seen = {}
        for root in (self.home, self.secret_dir):
            for r, ds, fs in os.walk(root):
                for n in ds + fs:
                    p = os.path.join(r, n)
                    st = os.lstat(p)
                    seen[p] = (
                        stat.S_IFMT(st.st_mode),
                        Path(p).read_bytes() if stat.S_ISREG(st.st_mode) else None,
                    )
        return seen

    def bg(self):
        """A launch.Bg for the fixture's directories, without its unit or environment."""
        bg = object.__new__(launch.Bg)
        bg.env = {}
        bg.run_dir, bg.log_dir, bg.saved = str(self.run_dir), str(self.log_dir), str(self.saved)
        bg.pidfile = str(self.run_dir / "qemu.pid")
        bg.sock = str(self.run_dir / "console.sock")
        bg.log = str(self.log_dir / "console.log")
        return bg

    def save_logs(self, monkeypatch, guest):
        """Bg.save_logs with a guest that answers save-logs.sh NAME with guest(NAME); the name."""
        names = []

        def send(line, _wait, _sock, _log):
            name = re.fullmatch(r"sh /host/guest/scripts/save-logs\.sh (\S+)", line).group(1)
            names.append(name)
            guest(name)
            return ""

        monkeypatch.setattr(launch.base, "read_pid", lambda _p: 1)
        monkeypatch.setattr(launch.unit, "alive", lambda _p: True)
        monkeypatch.setattr(launch.unit, "is_socket", lambda _p: True)
        monkeypatch.setattr(launch.console, "send", send)
        monkeypatch.setattr(launch.time, "sleep", lambda _s: pytest.fail("waited for the guest"))
        self.bg().save_logs()
        return names[0]


@pytest.fixture
def fx(tmp_path):
    return Fixture(tmp_path)


def finished_dir(d, files=None):
    d.mkdir(parents=True)
    for rel, text in (files or {"dmesg.txt": "dmesg\n"}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    (d / ".done").write_text("")


def skipped(d):
    return [
        line.split("\t")
        for line in (d / share.SKIPPED).read_text().splitlines()
        if not line.startswith("#")
    ]


def test_stop_host_log_link(fx, monkeypatch):
    """(a) The guest's webos.log a link to a user's file: the file is not written; the host's
    webos.log is a regular file of the saved directory, the guest's listed as shadowed."""
    before = fx.snapshot()

    def guest(name):
        finished_dir(fx.logs / name)
        os.symlink(fx.bashrc, fx.logs / name / "webos.log")

    name = fx.save_logs(monkeypatch, guest)
    assert fx.snapshot() == before
    d = fx.saved / name
    for f, text in HOST_LOGS.items():
        assert not (d / f).is_symlink() and (d / f).read_text() == text
    assert (d / "dmesg.txt").read_text() == "dmesg\n"
    assert ["webos.log", "symlink, shadowed by the host's own"] in skipped(d)
    assert not os.path.lexists(fx.logs / name)


def test_stop_log_dir_link(fx, monkeypatch, capsys):
    """(b) logs/NAME a link to a directory elsewhere (with a .done): left alone, nothing saved."""
    before = fx.snapshot()
    name = fx.save_logs(monkeypatch, lambda n: os.symlink(fx.secret_dir, fx.logs / n))
    assert fx.snapshot() == before
    assert not os.path.lexists(fx.saved / name)
    assert os.path.islink(fx.logs / name)
    assert "a symlink, not a directory" in capsys.readouterr().err


def test_stop_logs_link(fx, monkeypatch, capsys):
    """out/logs itself a link to a directory elsewhere: left alone, nothing saved."""
    os.rmdir(fx.logs)
    os.symlink(fx.secret_dir, fx.logs)
    before = fx.snapshot()
    name = fx.save_logs(monkeypatch, lambda n: finished_dir(fx.secret_dir / n))
    # the guest's own writes through its link are the guest's; the host adds nothing to them
    assert sorted(os.listdir(fx.secret_dir / name)) == [".done", "dmesg.txt"]
    outside = {
        k: v for k, v in fx.snapshot().items() if not k.startswith(str(fx.secret_dir / name))
    }
    assert outside == before
    assert not os.path.lexists(fx.saved / name)
    assert "not a directory" in capsys.readouterr().err


def test_stop_done_link(fx, monkeypatch, capsys):
    """(c) .done a link (to a regular file): refused, nothing written into the guest's directory."""
    before = fx.snapshot()

    def guest(name):
        (fx.logs / name).mkdir()
        os.symlink(fx.bashrc, fx.logs / name / ".done")

    name = fx.save_logs(monkeypatch, guest)
    assert fx.snapshot() == before
    assert not os.path.lexists(fx.saved / name)
    assert sorted(os.listdir(fx.logs / name)) == [".done"]
    assert ".done: a symlink, not a file" in capsys.readouterr().err


def plant_specials(d, target):
    """A nested link, a FIFO and a socket in d/var-log."""
    os.symlink(target, d / "var-log" / "sub" / "passwd")
    os.symlink(target.parent, d / "var-log" / "dirlink")
    os.mkfifo(d / "var-log" / "fifo")
    cwd = os.getcwd()  # a socket's path is short: bound where it is made
    os.chdir(d / "var-log")
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.bind("sock")
    finally:
        os.chdir(cwd)


def test_stop_specials(fx, monkeypatch):
    """(d) A nested link, a FIFO and a socket in var-log: not saved (no FIFO opened), listed."""
    before = fx.snapshot()

    def guest(name):
        finished_dir(fx.logs / name, {"var-log/messages": "m\n", "var-log/sub/x": "x\n"})
        plant_specials(fx.logs / name, fx.bashrc)

    name = fx.save_logs(monkeypatch, guest)
    assert fx.snapshot() == before
    d = fx.saved / name
    found = sorted(
        os.path.relpath(os.path.join(r, n), d / "var-log")
        for r, ds, fs in os.walk(d / "var-log")
        for n in ds + fs
    )
    assert found == ["messages", "sub", "sub/x"]
    assert sorted(skipped(d)) == [
        ["var-log/dirlink", "symlink", f"-> {fx.home}"],
        ["var-log/fifo", "fifo"],
        ["var-log/sock", "socket"],
        ["var-log/sub/passwd", "symlink", f"-> {fx.bashrc}"],
    ]
    assert not os.path.lexists(fx.logs / name)


def test_stop_normal(fx, monkeypatch):
    """(e) An ordinary log directory: saved as it is, with the host's logs, no skipped.txt."""
    files = {"dmesg.txt": "d\n", "var-log/messages": "m\n", "device-counters/outer/a.tsv": "a\n"}
    name = fx.save_logs(monkeypatch, lambda n: finished_dir(fx.logs / n, files))
    d = fx.saved / name
    for rel, text in files.items():
        assert (d / rel).read_text() == text
    for f, text in HOST_LOGS.items():
        assert (d / f).read_text() == text
    assert (d / ".done").is_file()
    assert not (d / share.SKIPPED).exists()
    assert not os.path.lexists(fx.logs / name)
    # a second one of the same name: .2
    fx.save_logs(monkeypatch, lambda n: finished_dir(fx.logs / n, files))
    assert (fx.saved / f"{name}.2" / "dmesg.txt").read_text() == "d\n"


def test_harvest(fx):
    """(f) At start (share.harvest): the same cases."""
    before = fx.snapshot()
    finished_dir(fx.logs / "a", {"dmesg.txt": "a\n", "var-log/messages": "m\n"})
    os.symlink(fx.secret_dir, fx.logs / "b")
    (fx.logs / "c").mkdir()
    os.symlink(fx.bashrc, fx.logs / "c" / ".done")
    finished_dir(fx.logs / "d", {"var-log/messages": "m\n", "var-log/sub/x": "x\n"})
    plant_specials(fx.logs / "d", fx.bashrc)
    finished_dir(fx.logs / "e")
    os.symlink(fx.bashrc, fx.logs / "e" / "webos.log")
    (fx.logs / "f").mkdir()  # not finished: left for later
    os.mkfifo(fx.logs / "g")
    got = share.harvest(str(fx.run_dir), str(fx.saved))
    assert got == [str(fx.saved / n) for n in ("a", "d", "e")]
    assert fx.snapshot() == before
    assert (fx.saved / "a" / "var-log" / "messages").read_text() == "m\n"
    assert not (fx.saved / "a" / share.SKIPPED).exists()
    assert sorted(os.listdir(fx.saved / "d" / "var-log")) == ["messages", "sub"]
    assert len(skipped(fx.saved / "d")) == 4
    assert not os.path.lexists(fx.saved / "e" / "webos.log")
    assert skipped(fx.saved / "e") == [["webos.log", "symlink, shadowed by the host's own"]]
    assert sorted(os.listdir(fx.logs)) == ["b", "c", "f", "g"]
    assert sorted(os.listdir(fx.saved)) == ["a", "d", "e"]


def test_harvest_logs_link(fx):
    """out/logs a link to a directory elsewhere with a finished one in it: left alone."""
    os.rmdir(fx.logs)
    os.symlink(fx.home, fx.logs)
    finished_dir(fx.home / "x")
    before = fx.snapshot()
    assert share.harvest(str(fx.run_dir), str(fx.saved)) == []
    assert fx.snapshot() == before


def test_place(fx):
    """share.place (vm-launcher put-out): a host file into the out share, never through a link."""
    before = fx.snapshot()
    src = fx.tmp / "app.ipk"
    src.write_bytes(b"ipk")
    # an ordinary place: its directories made
    assert share.place(str(fx.out), "regress/tones/a.wav", str(src)) == str(
        fx.out / "regress/tones/a.wav"
    )
    assert (fx.out / "regress/tones/a.wav").read_bytes() == b"ipk"
    # a link at the name is replaced, the file it names not written
    os.symlink(fx.bashrc, fx.out / "regress" / "tvsuite.ipk")
    share.place(str(fx.out), "regress/tvsuite.ipk", str(src))
    assert not (fx.out / "regress/tvsuite.ipk").is_symlink()
    assert (fx.out / "regress/tvsuite.ipk").read_bytes() == b"ipk"
    # a link on the way, a FIFO at the name's directory, a path that leaves the share: refused
    os.symlink(fx.home, fx.out / "elsewhere")
    os.mkfifo(fx.out / "fifo")
    for rel in ("elsewhere/.bashrc", "elsewhere/new/x", "fifo/x", "../x", "/x", "a//b", "a/./b"):
        with pytest.raises(share.Error):
            share.place(str(fx.out), rel, str(src))
    assert fx.snapshot() == before
    assert not os.path.lexists(fx.tmp / "vm" / "run" / "share" / "x")
    assert sorted(n for n in os.listdir(fx.out / "regress") if n.startswith(".")) == []


def test_put_out_command(fx):
    """`host/scripts/vm-launcher put-out --dir VM SRC DEST`."""
    src = fx.tmp / "tone.wav"
    src.write_bytes(b"RIFF")
    os.symlink(fx.bashrc, fx.out / "tone.wav")
    r = subprocess.run(
        [sys.executable, VM, "put-out", "--dir", str(fx.tmp / "vm"), str(src), "tone.wav"],
        check=False,
        capture_output=True,
        text=True,
        cwd=TOP,
    )
    assert (r.returncode, r.stdout) == (0, "/host/out/tone.wav\n"), r.stderr
    assert (fx.out / "tone.wav").read_bytes() == b"RIFF"
    assert fx.bashrc.read_text() == "# the user's\n"


def test_read_and_take(fx):
    """share.read, share.take (vm-launcher get-out): a regular file of the out share only, never
    through a link the guest made there, never blocking on its FIFO."""
    (fx.out / "regress").mkdir()
    (fx.out / "regress" / "boot.txt").write_text("booted\n")
    assert share.read(str(fx.out), "regress/boot.txt") == b"booted\n"
    dest = fx.tmp / "results"
    dest.mkdir()
    assert share.take(str(fx.out), "regress/boot.txt", str(dest)) == str(dest / "boot.txt")
    assert (dest / "boot.txt").read_text() == "booted\n"
    share.take(str(fx.out), "regress/boot.txt", str(dest / "again.txt"))
    assert (dest / "again.txt").read_text() == "booted\n"
    # a link at the name, a link on the way, a FIFO, a directory: refused, nothing read or made
    os.symlink(fx.bashrc, fx.out / "regress" / "link.txt")
    os.symlink(fx.home, fx.out / "home")
    os.mkfifo(fx.out / "regress" / "fifo.txt")
    for rel in ("regress/link.txt", "home/.bashrc", "regress/fifo.txt", "regress", "../x", "/x"):
        with pytest.raises(share.Error) as e:
            share.read(str(fx.out), rel)
        assert not isinstance(e.value, share.MissingError), rel
        with pytest.raises(share.Error):
            share.take(str(fx.out), rel, str(dest / "taken"))
        assert not (dest / "taken").exists()
    with pytest.raises(share.MissingError):
        share.read(str(fx.out), "regress/none.txt")
    with pytest.raises(share.MissingError):
        share.read(str(fx.out), "none/boot.txt")


def test_remove(fx):
    """share.remove (vm-launcher rm-out): a file or link of the out share unlinked, never anything
    through a linked directory on the way."""
    before = fx.snapshot()
    (fx.out / "regress").mkdir()
    (fx.out / "regress" / "boot.rc").write_text("0\n")
    os.symlink(fx.bashrc, fx.out / "regress" / "link.rc")
    assert share.remove(str(fx.out), "regress/boot.rc")
    assert share.remove(str(fx.out), "regress/link.rc")
    assert not share.remove(str(fx.out), "regress/boot.rc")
    assert not share.remove(str(fx.out), "none/boot.rc")
    assert os.listdir(fx.out / "regress") == []
    with pytest.raises(share.Error):
        share.remove(str(fx.out), "regress")
    # the guest's regress is a link to a directory of the user's: nothing removed there
    os.rmdir(fx.out / "regress")
    os.symlink(fx.secret_dir, fx.out / "regress")
    with pytest.raises(share.Error):
        share.remove(str(fx.out), "regress/keep")
    assert fx.snapshot() == before


def test_get_out_and_rm_out_commands(fx):
    """`host/scripts/vm-launcher get-out --dir VM SRC [DEST]` and `rm-out --dir VM PATH...`."""

    def vm(*args):
        return subprocess.run(
            [sys.executable, VM, args[0], "--dir", str(fx.tmp / "vm"), *args[1:]],
            check=False,
            capture_output=True,
            text=True,
            cwd=TOP,
            timeout=30,
        )

    (fx.out / "step.txt").write_text("output\n")
    r = vm("get-out", "step.txt")
    assert (r.returncode, r.stdout) == (0, "output\n"), r.stderr
    r = vm("get-out", "step.txt", str(fx.tmp / "copy.txt"))
    assert r.returncode == 0 and (fx.tmp / "copy.txt").read_text() == "output\n", r.stderr
    os.symlink(fx.bashrc, fx.out / "secret.txt")
    os.mkfifo(fx.out / "fifo.txt")
    for name in ("secret.txt", "fifo.txt", "none.txt"):
        r = vm("get-out", name)
        assert (r.returncode, r.stdout) == (1, ""), name
        assert "the user's" not in r.stderr
    r = vm("rm-out", "step.txt", "secret.txt", "none.txt")
    assert r.returncode == 0, r.stderr
    assert sorted(os.listdir(fx.out)) == ["fifo.txt", "logs"]
    assert fx.bashrc.read_text() == "# the user's\n"
