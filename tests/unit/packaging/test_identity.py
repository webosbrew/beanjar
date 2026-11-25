#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Package versions from packaging/VERSION and git (packaging/identity.py) and the packages'
compatibility fields (packaging/debian/build.py): ordering, contracts and metadata validation."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
from unittest import mock

import pytest

TOP = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


identity = load("beanjar_identity", TOP / "packaging/identity.py")
debian = load("debian_build", TOP / "packaging/debian/build.py")
HASH = "0123456789ab"


def dpkg_lt(a, b):
    return subprocess.run(["dpkg", "--compare-versions", a, "lt", b]).returncode == 0


def test_build_version():
    assert identity.version_of("0.2.0", 0, "a" * 40) == "0.2.0"
    assert identity.version_of("0.2.0", 5, HASH + "f" * 28) == f"0.2.0+git5.g{HASH}"
    assert identity.SEMVER.fullmatch(identity.base_version(TOP))


def test_contract():
    assert identity.contract("0.2.0") == "0.2"
    assert identity.contract(f"0.2.7+git3.g{HASH}") == "0.2"
    assert identity.next_contract("0.2.7") == "0.3"
    assert identity.contract("1.4.0") == "1"
    assert identity.next_contract("1.4.0") == "2"
    with pytest.raises(identity.Error):
        identity.contract("0.2")


@pytest.mark.skipif(not shutil.which("dpkg"), reason="no dpkg")
def test_debian_ordering():
    """The first versions from packaging/VERSION sort after the last hand-made one, builds after
    a version in commit order before the next one, and the host's guest range takes exactly
    the versions of its contract."""
    order = [
        "0.1.0~pre118",
        "0.2.0",
        f"0.2.0+git5.g{HASH}",
        "0.2.0+git10.g000000000000",
        "0.2.1",
        "0.3.0",
    ]
    for a, b in zip(order, order[1:]):
        assert dpkg_lt(a, b), f"{a} < {b}"
    for v in order:
        assert subprocess.run(["dpkg", "--validate-version", v]).returncode == 0, v
    low, high = identity.contract("0.2.1"), identity.next_contract("0.2.1")
    inside = lambda v: not dpkg_lt(v, low) and dpkg_lt(v, high)
    assert [v for v in order if inside(v)] == order[1:5]
    assert not inside("0.1.9")


def sample(**over):
    data = {
        "version": "0.2.0",
        "commit": "a" * 40,
        "commit_epoch": 1791893281,
        "dirty": False,
        "shallow": False,
    }
    data.update(over)
    return data


def test_round_trip_and_named_errors(tmp_path):
    path = tmp_path / "source-version.toml"
    with pytest.raises(identity.Error, match="source version metadata is missing"):
        identity.read(path)
    identity.write(path, sample())
    assert identity.read(path) == sample()
    assert identity.read(path, guest=True) == sample()
    identity.write(path, sample(version="today"))
    with pytest.raises(identity.Error, match="version is missing or malformed"):
        identity.read(path)
    path.write_text("version = ")
    with pytest.raises(identity.Error, match="invalid source version metadata"):
        identity.read(path)


def test_dirty_and_shallow_refused():
    identity.releasable(sample(), "x")
    with pytest.raises(identity.Error, match="uncommitted"):
        identity.releasable(sample(dirty=True), "x")
    with pytest.raises(identity.Error, match="shallow"):
        identity.releasable(sample(shallow=True), "x")


def test_unchanged_file_not_rewritten(tmp_path):
    path = tmp_path / "s.toml"
    identity.write(path, sample())
    os.utime(path, (1, 1))
    identity.write(path, sample())
    assert path.stat().st_mtime == 1


@pytest.mark.skipif(not shutil.which("git"), reason="no git")
def test_resolve_from_git(tmp_path):
    repo = tmp_path / "repo"
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@t",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
    )
    git = lambda *a: subprocess.run(
        ["git", "-C", str(repo), *a], check=True, env=env, capture_output=True
    )
    (repo / "packaging").mkdir(parents=True)
    git("init", "-q")
    (repo / "f").write_text("1")
    (repo / "packaging/VERSION").write_text("0.2.0\n")
    git("add", "f")
    git("commit", "-qm", "one")
    with (
        mock.patch.dict(os.environ, env),
        pytest.raises(identity.Error, match="not committed"),
    ):
        identity.resolve(repo)
    git("add", "packaging/VERSION")
    git("commit", "-qm", "version")
    with mock.patch.dict(os.environ, env):
        got = identity.resolve(repo)
    assert got["version"] == "0.2.0"
    assert not got["dirty"]
    for n in ("2", "3"):
        (repo / "f").write_text(n)
        git("commit", "-qam", n)
    (repo / "f").write_text("4")
    with mock.patch.dict(os.environ, env):
        got = identity.resolve(repo)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert got["version"] == f"0.2.0+git2.g{head[:12]}"
    assert got["commit"] == head
    assert got["dirty"]
    # a commit by name, as the regression runner resolves it for a VM host: never dirty
    with mock.patch.dict(os.environ, env):
        got = identity.resolve(repo, head[:8])
    assert (got["version"], got["commit"], got["dirty"]) == (
        f"0.2.0+git2.g{head[:12]}",
        head,
        False,
    )
    (repo / "packaging/VERSION").write_text("0.3.0\n")
    git("commit", "-qam", "bump")
    with mock.patch.dict(os.environ, env):
        assert identity.resolve(repo)["version"] == "0.3.0"
        # the VERSION of that commit, not the work tree's
        assert identity.resolve(repo, head)["version"] == f"0.2.0+git2.g{head[:12]}"

    # the same tree without its repository (a VM host's checkout): the stamp the checkout wrote
    tree = tmp_path / "tree"
    (tree / "packaging").mkdir(parents=True)
    (tree / "packaging/VERSION").write_text("0.3.0\n")
    with mock.patch.dict(os.environ, env), pytest.raises(identity.Error):
        identity.resolve(tree)
    (tree / identity.STAMP).write_text(identity.dump(got))
    assert identity.resolve(tree) == got


def test_host_depends_on_its_contracts_guests():
    groups = [
        g.strip()
        for g in debian.host_dependencies("0.2.0+git4.g" + HASH, "libc6", identity).split(",")
    ]
    assert groups[:2] == ["beanjar-guest (>= 0.2)", "beanjar-guest (<< 0.3)"]
    groups = [g.strip() for g in debian.host_dependencies("1.4.2", "libc6", identity).split(",")]
    assert groups[:2] == ["beanjar-guest (>= 1)", "beanjar-guest (<< 2)"]
    assert debian.vnc_dependencies("0.2.0", "libc6").split(",")[0] == "beanjar (= 0.2.0)"


def test_installation_toml():
    text = debian.installation_toml(version="0.2.0", commit="a" * 40)
    import tomllib

    assert tomllib.loads(text) == {"version": "0.2.0", "commit": "a" * 40}
