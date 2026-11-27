#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Inspector readiness must come from the running VM and a real mitmweb response."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from unittest import mock

import pytest

from beanjar.network import inspector


def test_runtime_endpoint_overrides_edited_config_and_wildcard_binding(tmp_path):
    directory = str(tmp_path)
    marker = Path(directory) / "ready"
    marker.touch()
    vm = mock.Mock(config={"link": "nat", "mode": "direct"})
    vm.pid.return_value = 123
    env = {
        "NET_LINK": "nat",
        "NET_MODE": "mitm",
        "NETNS_MITM_READY": str(marker),
        "NETSTUB_WEB_HOST": "0.0.0.0",
        "NETSTUB_WEB_PORT": "12345",
    }
    with mock.patch.object(inspector.hostprocs, "proc_env", return_value=env):
        state = inspector.snapshot(vm)
        assert (state.mode, state.url, state.ready) == ("mitm", "http://127.0.0.1:12345/", False)
        # by default the inspector is on the VM's own address, at the stock port
        del env["NETSTUB_WEB_HOST"], env["NETSTUB_WEB_PORT"]
        env["VM_ADDRESS"] = "127.0.1.3"
        assert inspector.snapshot(vm).url == "http://127.0.1.3:18083/"
        marker.unlink()
        assert inspector.snapshot(vm).url == ""
    vm.pid.return_value = None
    assert inspector.snapshot(vm).mode == "direct"
    vm.config["mode"] = "mitm"
    assert inspector.snapshot(vm).url == ""


def test_inspector_off_says_why(tmp_path):
    """No usable mitmweb at start (netns.py's run/mitmweb.json): the status says why and how to
    install it, and mitm show reports the found path and version."""
    vm = mock.Mock(config={"link": "nat", "mode": "mitm"}, run_dir=str(tmp_path))
    vm.name = "tv"
    vm.pid.return_value = 123
    env = {"NET_LINK": "nat", "NET_MODE": "mitm", "VM_ADDRESS": "127.0.1.3"}
    problem = "mitmproxy 11.1.3 at /m is too old. Install mitmproxy 12.0.1 or newer: pipx install mitmproxy"
    (tmp_path / "mitmweb.json").write_text(
        f'{{"version": "11.1.3", "path": "/m", "problem": "{problem}"}}'
    )
    with mock.patch.object(inspector.hostprocs, "proc_env", return_value=env):
        assert inspector.snapshot(vm).status == f"Inspector off: {problem}"
        data = inspector.show(vm)
    assert data["mitmweb"] == {"version": "11.1.3", "path": "/m", "problem": problem}
    assert not data["ready"]
    (tmp_path / "mitmweb.json").unlink()
    with mock.patch.object(inspector.hostprocs, "proc_env", return_value=env):
        assert inspector.snapshot(vm).status == "Inspector is not ready"


def test_password_from_the_vm_file_and_login_url(tmp_path):
    directory = str(tmp_path)
    marker = Path(directory) / "ready"
    marker.touch()
    secret = Path(directory) / "mitmweb.password"
    secret.write_text("a b/c+d\n")
    vm = mock.Mock(config={"link": "nat", "mode": "mitm"})
    vm.pid.return_value = 123
    env = {
        "NET_LINK": "nat",
        "NET_MODE": "mitm",
        "NETNS_MITM_READY": str(marker),
        "VM_ADDRESS": "127.0.1.3",
        "NETSTUB_WEB_PASSWORD_FILE": str(secret),
    }
    with mock.patch.object(inspector.hostprocs, "proc_env", return_value=env):
        state = inspector.snapshot(vm)
    assert (state.password, state.password_file) == ("a b/c+d", str(secret))
    assert state.login_url == "http://127.0.1.3:18083/?token=a%20b%2Fc%2Bd"
    assert "a b/c+d" not in repr(state)
    # a VM without the file (or one started before passwords) still has its address
    secret.unlink()
    with mock.patch.object(inspector.hostprocs, "proc_env", return_value=env):
        state = inspector.snapshot(vm)
    assert (state.password, state.login_url) == ("", "http://127.0.1.3:18083/")


def test_link_lan_url_is_the_lan_address_and_this_host_uses_the_vm_address(tmp_path):
    directory = str(tmp_path)
    marker = Path(directory) / "ready"
    marker.touch()
    Path(directory, "pw").write_text("pw\n")
    vm = mock.Mock(config={"link": "lan", "mode": "mitm"}, run_dir=directory)
    vm.pid.return_value = 123
    env = {
        "NET_LINK": "lan",
        "NET_MODE": "mitm",
        "NETNS_MITM_READY": str(marker),
        "VM_ADDRESS": "127.0.1.3",
        "NETSTUB_WEB_PASSWORD_FILE": str(Path(directory, "pw")),
    }
    with mock.patch.object(inspector.hostprocs, "proc_env", return_value=env):
        # no lease written yet: only the VM's own address
        state = inspector.snapshot(vm)
        assert (state.url, state.local_url, state.here_url) == (
            "http://127.0.1.3:18083/",
            "",
            "http://127.0.1.3:18083/",
        )
        Path(directory, "lan.json").write_text('{"address": "192.168.1.157", "prefix": 24}')
        state = inspector.snapshot(vm)
    assert (state.url, state.local_url) == (
        "http://192.168.1.157:18083/",
        "http://127.0.1.3:18083/",
    )
    assert (state.login_url, state.here_login_url) == (
        "http://192.168.1.157:18083/?token=pw",
        "http://127.0.1.3:18083/?token=pw",
    )
    # the probe goes to the address this host can reach
    with mock.patch.object(inspector.http.client, "HTTPConnection") as connection:
        connection.return_value.request.side_effect = OSError()
        assert not (inspector.probe(state).ready)
    connection.assert_called_once_with("127.0.1.3", 18083, timeout=1)


def test_status_gives_the_lan_url_and_this_hosts():
    from beanjar.cli import vms

    lan = inspector.State(
        "mitm",
        "",
        "http://192.168.1.157:18083/",
        password="pw",
        password_file="/f",
        local_url="http://127.0.1.3:18083/",
    )
    nat = inspector.State("mitm", "", "http://127.0.1.3:18083/", password="pw", password_file="/f")
    with mock.patch.object(inspector, "snapshot", side_effect=[lan, nat]):
        assert vms.inspector_info(None) == {
            "url": "http://192.168.1.157:18083/",
            "local_url": "http://127.0.1.3:18083/",
            "password_file": "/f",
        }
        assert vms.inspector_info(None) == {"url": "http://127.0.1.3:18083/", "password_file": "/f"}


def test_live_response_and_dead_or_unrelated_listener():
    class Handler(BaseHTTPRequestHandler):
        server_version = "mitmproxy 12.2.3"
        sys_version = ""

        def do_HEAD(self):
            self.send_response(405)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    state = inspector.State(
        "mitm", "", f"http://127.0.0.1:{server.server_port}/", password="pw", password_file="/f"
    )
    try:
        assert inspector.probe(state).ready
        assert (inspector.probe(state).password, inspector.probe(state).password_file) == (
            "pw",
            "/f",
        )
        Handler.server_version = "Unrelated application"
        assert not (inspector.probe(state).ready)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert not (inspector.probe(state).ready)


def test_logs_are_bounded_and_missing_logs_are_normal(tmp_path):
    directory = str(tmp_path)
    vm = mock.Mock(logs_dir=directory)
    assert inspector.log_tail(vm) == ""
    Path(directory, "netstub-mitm.log").write_text("discarded line\nlast line\n")
    assert inspector.log_tail(vm, 15) == "last line\n"


class FakeMitmweb(BaseHTTPRequestHandler):
    """mitmweb's rules as far as connect and clear meet them (mitmproxy 12.2.3, tornado): the
    session cookie, else the Bearer password (which sets the cookie); a POST also needs the XSRF
    cookie and an equal X-XSRFToken header, whatever the token."""

    cleared = 0

    def log_message(self, *_args):
        pass

    def cookies(self):
        pairs = (
            c.strip().split("=", 1) for c in self.headers.get("Cookie", "").split(";") if "=" in c
        )
        return dict(pairs)

    def login(self):
        if self.cookies().get("mitmproxy-auth-18083") == "yes":
            return True
        if self.headers.get("Authorization") == "Bearer pw":
            return "mitmproxy-auth-18083=yes"
        return False

    def answer(self, status, cookie=None):
        self.send_response(status)
        if isinstance(cookie, str):
            self.send_header("Set-Cookie", f"{cookie}; HttpOnly; Path=/")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        login = self.login()
        self.answer(200 if login else 403, login)

    def do_POST(self):
        token = self.cookies().get("_mitmproxy_xsrf")
        if not token or token != self.headers.get("X-XSRFToken"):
            return self.answer(403)
        login = self.login()
        if login and self.path == "/clear":
            FakeMitmweb.cleared += 1
        self.answer(200 if login else 403, login)


@pytest.fixture
def fake_mitmweb():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMitmweb)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()
    thread.join()


def test_session(fake_mitmweb):
    """The password logs in once; the cookie it set does from then on; another is refused."""
    server, url = fake_mitmweb
    assert inspector.ask(url, password="pw") == ("ok", "mitmproxy-auth-18083=yes")
    assert inspector.ask(url, cookie="mitmproxy-auth-18083=yes") == ("ok", None)
    assert inspector.ask(url, cookie="mitmproxy-auth-18083=old")[0] == "denied"
    assert inspector.ask(url, password="old")[0] == "denied"
    port = server.server_address[1]
    server.shutdown()
    server.server_close()
    assert inspector.ask(f"http://127.0.0.1:{port}/")[0] == "refused"


def test_clear(fake_mitmweb):
    """POST /clear with the XSRF pair; without it, or the password, it is refused."""
    _, url = fake_mitmweb
    FakeMitmweb.cleared = 0
    assert inspector.clear(url, "pw")
    assert FakeMitmweb.cleared == 1
    assert not (inspector.clear(url, "wrong"))
    assert inspector.ask(url, "POST", "/clear", password="pw")[0] == "denied"
    assert FakeMitmweb.cleared == 1
