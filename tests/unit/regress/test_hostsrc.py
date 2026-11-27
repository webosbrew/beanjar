# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""tests/regress/lib/hostsrc.py's host-side commands, run here against a stand-in home directory
and temporary repositories (no ssh)."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "tests" / "regress" / "lib"))
import hostsrc  # noqa: E402


@pytest.fixture
def env(tmp_path):
    config = tmp_path / "gitconfig"
    config.write_text(
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgSign = false\n"
        "[user]\n\tname = t\n\temail = t@example.invalid\n"
    )
    return dict(os.environ, GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM="1")


def git(env, repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def on_host(env, home, cmd):
    """cmd as the VM host runs it: from its home directory."""
    return subprocess.run(
        ["sh", "-c", cmd],
        cwd=home,
        env=dict(env, HOME=str(home)),
        capture_output=True,
        text=True,
        check=False,
    )


def files(root):
    return sorted(
        str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file() or p.is_symlink()
    )


def write(repo, tree):
    for name, text in tree.items():
        p = Path(repo) / name
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def test_checkout_is_exact_and_keeps_what_is_the_hosts(tmp_path, env):
    src, home = tmp_path / "src", tmp_path / "home"
    home.mkdir()
    git(env, tmp_path, "init", "-q", str(src))
    write(
        src,
        {
            "a.txt": "a1\n",
            "b.txt": "b\n",
            "same.txt": "same\n",
            "subprojects/packagefiles/p/meson.build": "x\n",
            "subprojects/q.wrap": "w\n",
        },
    )
    git(env, src, "add", "-A")
    git(env, src, "commit", "-q", "-m", "one")
    c1 = git(env, src, "rev-parse", "HEAD")
    write(src, {"a.txt": "a2\n", "b.txt": None, "d.txt": "d\n"})
    git(env, src, "add", "-A")
    git(env, src, "commit", "-q", "-m", "two")
    c2 = git(env, src, "rev-parse", "HEAD")

    assert on_host(env, home, hostsrc.ensure_cmd()).returncode == 0
    assert on_host(env, home, hostsrc.ensure_cmd()).returncode == 0  # again: no change
    assert on_host(env, home, hostsrc.has_cmd(c1)).returncode != 0
    bare = home / hostsrc.BARE
    git(env, src, "push", "-q", str(bare), f"+{c1}:{hostsrc.REF_PREFIX}{c1}")
    assert on_host(env, home, hostsrc.has_cmd(c1)).returncode == 0

    # what an earlier rsync left, and what is the host's own
    dest = home / "beanjar"
    write(
        dest,
        {
            "stale.txt": "old sync\n",
            ".git/HEAD": "ref: refs/heads/main\n",
            "x/__pycache__/m.pyc": "c",
            "build/Image": "k",
            "scratch/s": "s",
            "subprojects/q/unpacked.c": "u",
            "subprojects/packagecache/q.tar": "t",
            "subprojects/packagefiles/p/extra": "e",
        },
    )
    r = on_host(env, home, hostsrc.checkout_cmd("beanjar", c1))
    assert r.returncode == 0, r.stderr
    own = [
        ".git/HEAD",
        "build/Image",
        "scratch/s",
        "subprojects/packagecache/q.tar",
        "subprojects/q/unpacked.c",
    ]
    want = ["a.txt", "b.txt", "same.txt", "subprojects/packagefiles/p/meson.build"]
    assert files(dest) == sorted(own + want + ["subprojects/q.wrap"])
    old = time.time() - 3600
    os.utime(dest / "same.txt", (old, old))

    git(env, src, "push", "-q", str(bare), f"+{c2}:{hostsrc.REF_PREFIX}{c2}")
    (dest / "a.txt").write_text("edited on the host\n")
    r = on_host(env, home, hostsrc.checkout_cmd("beanjar", c2))
    assert r.returncode == 0, r.stderr
    assert "b.txt" not in files(dest)  # deleted between the commits: gone
    assert (dest / "a.txt").read_text() == "a2\n"
    assert (dest / "d.txt").read_text() == "d\n"
    assert int(os.stat(dest / "same.txt").st_mtime) == int(old)  # unchanged: not rewritten
    assert (dest / "build/Image").read_text() == "k"
    # the source commit's identity, at the top of the tree: the build has no history there
    r = on_host(env, home, hostsrc.checkout_cmd("beanjar", c2, 'version = "0.1.0"\n'))
    assert r.returncode == 0, r.stderr
    assert (dest / hostsrc.STAMP).read_text() == 'version = "0.1.0"\n'
    r = on_host(env, home, hostsrc.checkout_cmd("beanjar", c2))
    assert hostsrc.STAMP not in files(dest)  # not the commit's: cleaned


def test_lock_is_exclusive(tmp_path, env):
    home = tmp_path
    first = subprocess.Popen(
        ["sh", "-c", hostsrc.lock_cmd("beanjar", "run one")],
        cwd=home,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout.readline().strip() == "LOCKED"
        second = on_host(env, home, hostsrc.lock_cmd("beanjar", "run two"))
        assert second.returncode == 1
        assert second.stdout.split()[:1] == ["BUSY"]
        assert "run one" in second.stdout
    finally:
        first.stdin.close()
        first.wait(10)
    third = subprocess.Popen(
        ["sh", "-c", hostsrc.lock_cmd("beanjar", "run three")],
        cwd=home,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert third.stdout.readline().strip() == "LOCKED"
    third.stdin.close()
    third.wait(10)


def test_push_argv():
    argv = hostsrc.push_argv("/top", "pi", "f" * 40, "refs/hosts/" + "c" * 40)
    assert argv[-2:] == ["pi:beanjar-src.git", f"+{'f' * 40}:refs/hosts/{'c' * 40}"]
    assert "core.sshCommand=ssh -o BatchMode=yes -o ConnectTimeout=10" in argv
