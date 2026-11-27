# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""netns.py's inspector: the policy rules and route into mitmweb's TUN device, mitmweb's command
line, the run's mitmweb.json, and the start without a usable mitmweb."""

import os
import socket
from types import SimpleNamespace
from unittest import mock

import pytest
from beanjar.network import mitmtool, netns


class Tree:
    def src(self, *p):
        return "/src/" + "/".join(p)


def test_rules_take_all_tcp_and_udp_from_the_tap_only():
    rules = netns.intercept_rules("tap0", "add")
    assert rules == [
        ["rule", "add", "pref", "100", "iif", "tap0", "ipproto", "tcp", "lookup", "101"],
        ["rule", "add", "pref", "100", "iif", "tap0", "ipproto", "udp", "lookup", "101"],
    ]
    # every rule is the guest's (iif the TAP): mitmweb's own upstream is not matched
    assert all(r[r.index("iif") + 1] == "tap0" for r in rules)
    # nothing else (ICMP) is: it is forwarded to slirp
    assert {r[r.index("ipproto") + 1] for r in rules} == {"tcp", "udp"}
    assert netns.hold_rules("tap0", "add") == [
        ["rule", "add", "pref", "101", "iif", "tap0", "ipproto", "tcp", "blackhole"],
        ["rule", "add", "pref", "101", "iif", "tap0", "ipproto", "udp", "blackhole"],
    ]
    assert netns.tun_route("replace") == [
        "route", "replace", "default", "dev", "mitm0", "table", "101",
    ]  # fmt: skip


def test_intercept_adds_the_route_before_the_rules():
    calls = []
    real = {"NET_LG_SERVICES": "real"}
    with mock.patch.object(netns, "_ip", lambda env, *a: calls.append(list(a))):
        netns._intercept(real, "tap0", "add")
    assert calls[0] == netns.tun_route("replace")
    assert calls[1:] == netns.intercept_rules("tap0", "add")
    calls.clear()
    with mock.patch.object(netns, "_ip", lambda env, *a: calls.append(list(a))):
        netns._intercept(real, "tap0", "del")
    assert calls == netns.intercept_rules("tap0", "del")  # the route goes with the device


def test_mock_routes_its_resolvers_upstream_into_mitmweb():
    """lg_services=mock (the default): the guest's names are lg_mock.py's at MOCK_DNS (the local
    table's), so its own upstream queries are what mitmweb is given to show."""
    assert netns.intercept_rules("tap0", "add", "10.0.2.254")[2:] == [
        ["rule", "add", "pref", "100", "from", "10.0.2.254/32", "ipproto", "tcp", "dport", "53",
         "lookup", "101"],
        ["rule", "add", "pref", "100", "from", "10.0.2.254/32", "ipproto", "udp", "dport", "53",
         "lookup", "101"],
    ]  # fmt: skip
    for env in ({}, {"NET_LG_SERVICES": "mock"}):
        calls = []
        with mock.patch.object(netns, "_ip", lambda env, *a: calls.append(list(a))):
            netns._intercept(env, "tap0", "add")
        assert calls[1:] == netns.intercept_rules("tap0", "add", netns.MOCK_DNS)


def test_argv():
    env = {"MITM_FLOWS": "50"}
    u = netns.mitmweb_argv("/opt/pipx/bin/mitmweb", Tree(), env, "/ca", "18083")
    assert u[:3] == ["/opt/pipx/bin/mitmweb", "--mode", "tun:mitm0"]
    assert "--listen-host" not in u
    assert u[-2:] == ["-s", "/src/host/helpers/network/mitm_addon.py"]
    assert u.count("-s") == 1
    assert "connection_strategy=eager" in u  # server-first protocols on any port
    for a in ("confdir=/ca", "ssl_insecure=true", "max_flows=50", "max_flow_bytes=0"):
        assert a in u
    assert u[u.index("--web-host") + 1] == "127.0.0.1" and u[u.index("--web-port") + 1] == "18083"


def test_argv_default_flows():
    from beanjar.vm import settings

    u = netns.mitmweb_argv("/m", Tree(), {}, "/ca", "1")
    assert f"max_flows={settings.MITM_FLOWS}" in u and settings.MITM_FLOWS == 5000


def test_info_file(tmp_path):
    info = {"version": "12.3.1", "path": "/m", "problem": ""}
    netns._write_info({"RUN_DIR": str(tmp_path)}, "/unused", info)
    assert netns.mitm_info(str(tmp_path)) == info
    assert netns.mitm_info(str(tmp_path / "none")) is None
    netns._write_info({}, None, info)  # nowhere to write: nothing, no error


def test_ready_waits_for_the_device(tmp_path):
    ready = tmp_path / "ready"
    ready.write_text("1\n")
    process = SimpleNamespace(poll=lambda: None)
    p = netns.Proxy(process, str(ready), "1", "log")
    with (
        mock.patch.object(netns, "_can_connect", return_value=True),
        mock.patch.object(socket, "if_nametoindex", side_effect=OSError),
    ):
        assert p.poll() is None
    with (
        mock.patch.object(netns, "_can_connect", return_value=True),
        mock.patch.object(socket, "if_nametoindex", return_value=7),
    ):
        assert p.poll() is True


def test_unit_keeps_the_mitmweb_override():
    from beanjar.runtime import unit

    assert unit.env_for({"NETNS_MITMWEB": "/x/mitmweb"}) == {"NETNS_MITMWEB": "/x/mitmweb"}


def test_without_mitmweb_routes_nothing(tmp_path, capsys):
    """No mitmweb: no Proxy, so inner() adds no hold and no rule (the guest goes straight out),
    and the reason, with how to install it, is logged and recorded."""
    env = {"PATH": str(tmp_path), "RUN_DIR": str(tmp_path)}
    with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
        assert netns._start_proxy(env, Tree(), str(tmp_path), str(tmp_path), "1") is None
    err = capsys.readouterr().err
    assert "forwarding directly" in err and mitmtool.INSTALL in err
    info = netns.mitm_info(str(tmp_path))
    assert info["path"] == "" and mitmtool.INSTALL in info["problem"]


def test_too_old_routes_nothing(tmp_path, capsys):
    exe = tmp_path / "mitmweb"
    exe.write_text("#!/bin/sh\necho 'Mitmproxy: 11.1.3'\n")
    exe.chmod(0o755)
    env = {"NETNS_MITMWEB": str(exe), "RUN_DIR": str(tmp_path)}
    assert netns._start_proxy(env, Tree(), str(tmp_path), str(tmp_path), "1") is None
    assert "NETNS_MITM_READY" not in env  # never started
    assert "11.1.3" in capsys.readouterr().err
    info = netns.mitm_info(str(tmp_path))
    assert info["version"] == "11.1.3" and "too old" in info["problem"]


def venv(tmp_path, name="venv"):
    """A pipx-like venv: bin/mitmweb whose shebang names bin/python, a link to the system's."""
    top = tmp_path / name
    (top / "bin").mkdir(parents=True)
    (top / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (top / "bin" / "python").symlink_to("/usr/bin/python3")
    exe = top / "bin" / "mitmweb"
    exe.write_text(f"#!{top}/bin/python\n")
    exe.chmod(0o755)
    return exe


def binds(cmd, option):
    """The (source, destination) pairs of one bwrap option (--bind, --ro-bind ...)."""
    return [(cmd[i + 1], cmd[i + 2]) for i, a in enumerate(cmd) if a == option]


def setenv(cmd):
    return {cmd[i + 1]: cmd[i + 2] for i, a in enumerate(cmd) if a == "--setenv"}


def sandbox(tmp_path, exe, **env):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "NETSTUB_LOG": str(tmp_path / "netstub.log"),
        "NETSTUB_WEB_PASSWORD_FILE": str(tmp_path / "run" / "mitmweb.password"),
        "NETNS_MITM_READY": str(tmp_path / "sock" / "mitmweb.d" / "ready"),
        "BEANJAR_MITM_PASSWORD": "secret",
        "SSH_AUTH_SOCK": "/run/user/1/ssh",
        **env,
    }
    with mock.patch.dict(os.environ, {"HOME": str(home)}):
        return netns.mitmweb_sandbox(
            "/usr/bin/bwrap",
            str(exe),
            env,
            str(tmp_path / "lib" / "ca"),
            "/src/host/helpers/network/mitm_addon.py",
            str(tmp_path / "sock" / "mitmweb.d"),
        )


def test_sandbox_sees_only_what_mitmweb_needs(tmp_path):
    exe = venv(tmp_path / "home" / ".local" / "share" / "pipx" / "venvs", "mitmproxy")
    cmd = sandbox(tmp_path, exe)
    assert cmd[0] == "/usr/bin/bwrap"
    for flag in ("--unshare-pid", "--die-with-parent", "--clearenv"):
        assert flag in cmd
    # no user or network namespace of its own: CAP_NET_ADMIN in the VM's, for its TUN device
    assert "--unshare-user" not in cmd and "--unshare-net" not in cmd
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert cmd[cmd.index("--cap-add") + 1] == "CAP_NET_ADMIN" and cmd.count("--cap-add") == 1
    assert ("/dev/net/tun", "/dev/net/tun") in binds(cmd, "--dev-bind")
    for private in ("--proc", "--dev", "--tmpfs"):
        assert cmd.count(private) == 1
    assert cmd[cmd.index("--tmpfs") + 1] == "/tmp"
    ro = [s for s, d in binds(cmd, "--ro-bind")]
    rw = [s for s, d in binds(cmd, "--bind")]
    assert all(s == d for s, d in binds(cmd, "--ro-bind") + binds(cmd, "--bind"))
    top = str(exe.parent.parent)
    system = {"/usr", "/etc"} | {
        "/" + d
        for d in netns.SANDBOX_ROOT_DIRS
        if os.path.isdir("/" + d) and not os.path.islink("/" + d)
    }
    addon = "/src/host/helpers/network/mitm_addon.py"
    table = "/src/host/helpers/network/lg_hosts.py"  # the addon's LG host classes
    assert set(ro) == system | {top, addon, table, str(tmp_path / "run" / "mitmweb.password")}
    assert set(rw) == {
        str(tmp_path / "lib" / "ca"),
        str(tmp_path / "sock" / "mitmweb.d"),
        str(tmp_path / "netstub.log"),
    }
    # of the home directory only the venv; of the run and socket directories only those files
    home = str(tmp_path / "home")
    assert [p for p in ro + rw if p.startswith(home)] == [top]
    assert str(tmp_path / "run") not in ro + rw and str(tmp_path / "sock") not in ro + rw
    # what is bound comes after the private /tmp (it may be in the host's)
    tmpfs = cmd.index("--tmpfs")
    assert all(cmd.index(p) > tmpfs for p in rw)
    env = setenv(cmd)
    assert env["HOME"] == "/tmp" and env["PATH"] == "/usr/bin:/bin"
    assert "BEANJAR_MITM_PASSWORD" not in env and "SSH_AUTH_SOCK" not in env
    assert set(env) <= set(netns.SANDBOX_ENV) | {"HOME", "PATH", "PYTHONUNBUFFERED"}
    assert env["NETNS_MITM_READY"].endswith("/mitmweb.d/ready")
    assert "PYTHONUSERBASE" not in env  # a venv


def test_sandbox_binds_at_the_paths_mitmweb_is_given(tmp_path):
    """A library reached through a link: each file is bound from where it is to the path in
    mitmweb's environment and command line."""
    real = tmp_path / "real"
    (real / "run").mkdir(parents=True)
    (tmp_path / "lib").symlink_to(real)
    password = tmp_path / "lib" / "run" / "mitmweb.password"
    cmd = sandbox(tmp_path, venv(tmp_path), NETSTUB_WEB_PASSWORD_FILE=str(password))
    assert (str(real / "run" / "mitmweb.password"), str(password)) in binds(cmd, "--ro-bind")
    assert (str(real / "ca"), str(tmp_path / "lib" / "ca")) in binds(cmd, "--bind")


def test_sandbox_interpreter_outside_the_venv(tmp_path):
    """A venv whose Python is not the system's (uv's, say) has that interpreter's tree bound too."""
    exe = venv(tmp_path)
    py = tmp_path / "pythons" / "3.13" / "bin" / "python3.13"
    py.parent.mkdir(parents=True)
    py.write_text("")
    py.chmod(0o755)
    (exe.parent / "python").unlink()
    (exe.parent / "python").symlink_to(py)
    ro = [s for s, d in binds(sandbox(tmp_path, exe), "--ro-bind")]
    assert str(tmp_path / "venv") in ro and str(tmp_path / "pythons" / "3.13") in ro


def test_sandbox_user_install(tmp_path):
    """pip --user: mitmweb in ~/.local/bin, Python the system's; ~/.local is bound and is the
    user base, since HOME is not the user's in the sandbox."""
    exe = tmp_path / "home" / ".local" / "bin" / "mitmweb"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/usr/bin/python3\n")
    exe.chmod(0o755)
    cmd = sandbox(tmp_path, exe)
    assert str(tmp_path / "home" / ".local") in [s for s, d in binds(cmd, "--ro-bind")]
    assert setenv(cmd)["PYTHONUSERBASE"] == str(tmp_path / "home" / ".local")


def test_sandbox_refuses_the_whole_home(tmp_path):
    exe = tmp_path / "home" / "bin" / "mitmweb"  # its prefix would be the home directory
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/usr/bin/python3\n")
    exe.chmod(0o755)
    with pytest.raises(netns.Error, match="home directory"):
        sandbox(tmp_path, exe)


def test_system_mitmweb_binds_nothing_more(tmp_path):
    ro = {s for s, d in binds(sandbox(tmp_path, "/usr/bin/true"), "--ro-bind")}
    assert not [p for p in ro if p.startswith(str(tmp_path / "home"))]


def proxy_mocks(tmp_path, exe, bwrap):
    ca = mock.Mock(stdout=f"{tmp_path}/ca/ca.pem")
    return (
        mock.patch.object(netns.subprocess, "run", return_value=ca),
        mock.patch.object(netns.subprocess, "Popen", return_value=mock.Mock()),
        mock.patch.object(mitmtool, "check", return_value=mitmtool.Tool(str(exe), "12.3.1", "")),
        mock.patch.object(mitmtool, "sandbox", return_value=bwrap),
    )


def test_start_runs_mitmweb_in_its_sandbox(tmp_path):
    exe = venv(tmp_path)
    tree = mock.Mock()
    tree.src.side_effect = lambda *p: "/src/" + "/".join(p)
    tree.build.return_value = str(tmp_path / "netstub.log")
    run, popen, check, bwrap = proxy_mocks(tmp_path, exe, "/usr/bin/bwrap")
    env = {"RUN_DIR": str(tmp_path)}
    with run, popen as started, check, bwrap:
        proxy = netns._start_proxy(env, tree, str(tmp_path), str(tmp_path), "18083")
    argv = started.call_args.args[0]
    assert argv[0] == "/usr/bin/bwrap"
    mitmweb = argv.index(str(exe))
    assert argv[mitmweb:] == netns.mitmweb_argv(str(exe), tree, env, f"{tmp_path}/ca", "18083")
    assert proxy.ready_file == str(tmp_path / netns.MITM_STATE_DIR / "ready")
    assert os.path.isdir(tmp_path / netns.MITM_STATE_DIR)


def test_start_without_bwrap_is_no_inspector(tmp_path):
    exe = venv(tmp_path)
    tree = mock.Mock()
    tree.build.return_value = str(tmp_path / "netstub.log")
    run, popen, check, bwrap = proxy_mocks(tmp_path, exe, None)
    with run, popen as started, check, bwrap:
        env = {"RUN_DIR": str(tmp_path)}
        assert netns._start_proxy(env, tree, str(tmp_path), str(tmp_path), "1") is None
    started.assert_not_called()
    assert "bubblewrap" in netns.mitm_info(str(tmp_path))["problem"]
