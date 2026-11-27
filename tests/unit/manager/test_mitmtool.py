# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""mitmtool.py: finding the user's mitmweb, telling its version and the 12.0.1 floor."""

import os
from unittest import mock

from beanjar.network import mitmtool
from beanjar.runtime import mediasandbox


def script(path, text="#!/bin/sh\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def test_find(tmp_path):
    assert mitmtool.find({"NETNS_MITMWEB": "/x/mitmweb"}) == "/x/mitmweb"
    exe = script(tmp_path / "bin" / "mitmweb")
    assert mitmtool.find({"PATH": str(tmp_path / "bin")}) == str(exe)
    with mock.patch.dict(os.environ, {"HOME": str(tmp_path / "home")}):
        assert mitmtool.find({"PATH": str(tmp_path / "none")}) is None
        # pipx's default, which a systemd user unit's PATH often lacks
        local = script(tmp_path / "home" / ".local" / "bin" / "mitmweb")
        assert mitmtool.find({"PATH": "/nonexistent"}) == str(local)


def test_parse():
    assert mitmtool.parse("12.3.1") == (12, 3, 1)
    assert mitmtool.parse("12.0.0.dev1") == (12, 0, 0)
    assert mitmtool.parse("") == ()
    assert mitmtool.parse("12.0") < mitmtool.parse(mitmtool.MIN_VERSION) <= mitmtool.parse("12.0.1")
    assert mitmtool.parse("12.10.0") > mitmtool.parse("12.9.9")


def test_version_from_metadata_without_running_it(tmp_path):
    exe = script(tmp_path / "venv" / "bin" / "mitmweb", "#!/bin/sh\nexit 1\n")
    (tmp_path / "venv/lib/python3.13/site-packages/mitmproxy-12.2.3.dist-info").mkdir(parents=True)
    assert mitmtool.version(str(exe)) == "12.2.3"
    # pipx: ~/.local/bin/mitmweb is a link into the venv
    link = tmp_path / "local" / "bin" / "mitmweb"
    link.parent.mkdir(parents=True)
    link.symlink_to(exe)
    assert mitmtool.version(str(link)) == "12.2.3"


def test_version_from_the_command(tmp_path):
    exe = script(tmp_path / "mitmweb", "#!/bin/sh\necho 'Mitmproxy: 12.3.1'\necho 'Python: 3.13'\n")
    assert mitmtool.version(str(exe)) == "12.3.1"
    assert mitmtool.version(str(tmp_path / "missing")) == ""


# Whether bubblewrap is installed on this machine is not what test_check tests
@mock.patch.object(mediasandbox, "find", return_value="/usr/bin/bwrap")
def test_check(_sandbox, tmp_path):
    missing = mitmtool.check({"NETNS_MITMWEB": str(tmp_path / "none")})
    assert not missing.usable and mitmtool.INSTALL in missing.problem
    assert missing.brief() == "mitmproxy not installed"
    with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
        none = mitmtool.check({"PATH": str(tmp_path / "empty")})
    assert none.path == "" and none.problem.startswith("mitmweb not found")
    assert none.brief() == "mitmproxy not installed"
    for v, usable in (("11.1.3", False), ("12.0.0", False), ("12.0.1", True), ("12.3.1", True)):
        exe = script(tmp_path / v / "mitmweb", f"#!/bin/sh\necho 'Mitmproxy: {v}'\n")
        tool = mitmtool.check({"NETNS_MITMWEB": str(exe)})
        assert (tool.version, tool.usable) == (v, usable), tool
        assert tool.describe() == (f"mitmproxy {v}" if usable else tool.problem)
        assert tool.brief() == ("" if usable else f"mitmproxy {v} too old")
        if not usable:
            assert "too old" in tool.problem and mitmtool.INSTALL in tool.problem
    silent = script(tmp_path / "silent" / "mitmweb")
    unknown = mitmtool.check({"NETNS_MITMWEB": str(silent)})
    assert "could not tell the version" in unknown.problem
    assert unknown.brief() == "mitmweb unusable"


def test_python(tmp_path):
    py = script(tmp_path / "venv" / "bin" / "python")
    exe = script(tmp_path / "venv" / "bin" / "mitmweb", f"#!{py}\nimport x\n")
    assert mitmtool.python(str(exe)) == str(py)
    env_shebang = script(tmp_path / "other" / "mitmweb", "#!/usr/bin/env sh\n")
    assert mitmtool.python(str(env_shebang)).endswith("/sh")
    assert mitmtool.python(str(tmp_path / "missing")) is None


def test_check_needs_bubblewrap(tmp_path):
    exe = script(tmp_path / "mitmweb", "#!/bin/sh\necho 'Mitmproxy: 12.3.1'\n")
    with mock.patch.object(mediasandbox, "find", return_value=None):
        tool = mitmtool.check({"NETNS_MITMWEB": str(exe)})
    assert not tool.usable and tool.problem == mitmtool.SANDBOX_PROBLEM
    assert tool.brief() == "bubblewrap not installed"
    fake = script(tmp_path / "bin" / "bwrap")
    assert mediasandbox.find({"PATH": str(tmp_path / "bin")}) == str(fake)
