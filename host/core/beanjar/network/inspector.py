# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Read the running VM's MITM endpoint and probe it without changing proxy state; `beanjar mitm
show`, `beanjar mitm connect` (show, connect_local, connect_remote) and `beanjar mitm clear`."""

import dataclasses
import http.client
import json
import os
import secrets
import sys
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from .. import base, hostprocs, vmsettings


@dataclass(frozen=True)
class State:
    mode: str
    status: str
    # where to reach it: with link lan the VM's LAN address, which the host itself cannot reach
    # (macvlan), so local_url is this host's way in, the VM's own address; '' with link nat
    url: str = ""
    ready: bool = False
    # the inspector's password (netns.py writes it, owner-only, at password_file); never in repr
    password: str = dataclasses.field(default="", repr=False)
    password_file: str = ""
    local_url: str = ""

    @property
    def here_url(self):
        """The URL that works from this host: local_url, else url."""
        return self.local_url or self.url

    def _login(self, url):
        """url, logging in by itself: mitmweb takes the password as ?token= and sets a cookie."""
        if not url or not self.password:
            return url
        return f"{url}?token={quote(self.password, safe='')}"

    @property
    def login_url(self):
        return self._login(self.url)

    @property
    def here_login_url(self):
        return self._login(self.here_url)


def read_password(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, TypeError, ValueError):
        return ""


def snapshot(vm):
    if vm is None:
        return State("mitm", "Not running")
    pid = vm.pid()
    env = hostprocs.proc_env(pid) if pid else {}
    mode = vmsettings.guest_net(
        env.get("NET_LINK") or vm.config.get("link"), env.get("NET_MODE") or vm.config.get("mode")
    )
    if mode != "mitm":
        return State(mode, "Not running")
    if not pid:
        return State(mode, "Not running")
    # The namespace wrapper passes its readiness marker and endpoint to QEMU.
    marker = env.get("NETNS_MITM_READY")
    if not marker or not os.path.isfile(marker):
        # why not, when this start's mitmweb could not serve (netns.py's RUN_DIR/mitmweb.json)
        from . import netns

        info = netns.mitm_info(vm.run_dir) or {}
        if info.get("problem"):
            return State(mode, f"Inspector off: {info['problem']}")
        return State(mode, "Inspector is not ready")
    # the relay's host side (netns.py): NETSTUB_WEB_HOST, by default the VM's address
    host = env.get("NETSTUB_WEB_HOST") or env.get("VM_ADDRESS") or ""
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    port = env.get("NETSTUB_WEB_PORT") or "18083"
    try:
        port = int(port)
        if not 0 < port < 65536 or not host or any(c in host for c in "/?#@"):
            raise ValueError()
    except ValueError:
        return State(mode, "Invalid inspector address")
    address = f"[{host}]" if ":" in host else host
    url = f"http://{address}:{port}/"
    try:
        urlsplit(url).hostname
    except ValueError:
        return State(mode, "Invalid inspector address")
    local_url = ""
    if env.get("NET_LINK") == "lan":
        # netns.lan() serves it on the LAN address too, at the same port (lan.json has the address)
        from . import netns

        lan = netns.lan_info(vm.run_dir)
        if lan:
            url, local_url = f"http://{lan['address']}:{port}/", url
    password_file = env.get("NETSTUB_WEB_PASSWORD_FILE") or ""
    return State(
        mode,
        "Checking inspector…",
        url,
        password=read_password(password_file),
        password_file=password_file,
        local_url=local_url,
    )


def probe(state):
    """Call off the UI thread. HEAD avoids fetching flows or page content, and needs no password
    (mitmweb answers it 405 without checking one, which spares an argon2 check every 5 s). From
    this host, so at local_url for a link lan VM."""
    url = urlsplit(state.here_url)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=1)
    try:
        connection.request("HEAD", "/")
        response = connection.getresponse()
        # mitmweb responds 405 to HEAD; an ordinary listener is not enough.
        if not response.getheader("Server", "").lower().startswith("mitmproxy"):
            return dataclasses.replace(state, status="Inspector unavailable", ready=False)
        if response.status in (200, 405):
            return dataclasses.replace(state, status="Running", ready=True)
        if response.status in (401, 403):
            return dataclasses.replace(state, status="Authentication required", ready=True)
        return dataclasses.replace(
            state, status=f"Inspector returned HTTP {response.status}", ready=False
        )
    except (OSError, http.client.HTTPException, ValueError):
        return dataclasses.replace(state, status="Inspector unavailable", ready=False)
    finally:
        connection.close()


def log_tail(vm, limit=24 * 1024):
    if vm is None:
        return ""
    try:
        with open(os.path.join(vm.logs_dir, "netstub-mitm.log"), "rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            data = stream.read().decode(errors="replace")
        return data.split("\n", 1)[-1] if size > limit else data
    except OSError:
        return ""


# -- beanjar mitm show and connect --

MITM_PREFIX = "beanjar-mitm-"  # a remote connect's record directory (ares.sweep_homes sweeps it)


def show(vm, password=False):
    """`mitm show`'s answer: the VM's mode, the inspector's status as probe gives it, whether it is
    ready, its URL (with link lan the LAN one, and local_url, this host's way in) and its password
    file; the password itself only with password."""
    state = snapshot(vm)
    if state.url:
        state = probe(state)
    data = {
        "name": vm.name,
        "mode": state.mode,
        "status": state.status,
        "ready": state.ready,
        "url": state.url or None,
        "local_url": state.local_url or None,
        "password_file": state.password_file or None,
    }
    # what this start's mitmweb is (netns.py's RUN_DIR/mitmweb.json): its version and path, and
    # why it does not serve (problem: none found, too old, or it failed) or None
    from . import netns

    info = netns.mitm_info(vm.run_dir) if state.status != "Not running" else None
    data["mitmweb"] = (
        {k: info.get(k) or None for k in ("version", "path", "problem")} if info else None
    )
    if password:
        data["password"] = state.password or None
    return data


def check_ready(name, data):
    """Refuses a VM whose inspector cannot be reached: not mode mitm, not running, not ready."""
    if data.get("mode") != "mitm":
        raise base.Error(
            f"VM {name} runs without the traffic inspector (network mode {data.get('mode')}, not "
            "mitm)"
        )
    if data.get("status") == "Not running":
        raise base.Error(f"VM {name} is not running")
    if not data.get("ready"):
        raise base.Error(f"VM {name}'s traffic inspector: {data.get('status')}")


CHECK = 5  # seconds between a held connect's looks at the inspector
RESHOW = 10  # seconds between asking for its state while it does not answer (a host: one call)
ASK_TIMEOUT = 5  # seconds one request to mitmweb may take
SESSION_PATH = "/state"  # a small page behind the login: mitmweb's session cookie, or the password
XSRF = "_mitmproxy_xsrf"  # mitmweb's XSRF cookie (tornado's xsrf_cookies, with xsrf_cookie_name)


def ask(url, method="GET", path=SESSION_PATH, cookie=None, password=None, xsrf=False):
    """One request to mitmweb at url: ("ok", its session cookie if it set one), ("denied", None)
    (403: no login, or a cookie of a mitmweb that has since restarted), ("refused", None) (nothing
    listens: a forward that is gone), ("down", None) otherwise. With cookie mitmweb checks no
    password; with password (Bearer) it runs one argon2 check and sets that cookie. xsrf: a non-GET
    needs tornado's XSRF pair, the cookie and X-XSRFToken equal, whatever the token is."""
    target = urlsplit(url)
    headers = {}
    cookies = [cookie] if cookie else []
    if password:
        headers["Authorization"] = f"Bearer {password}"
    if xsrf:
        token = secrets.token_hex(16)
        cookies.append(f"{XSRF}={token}")
        headers["X-XSRFToken"] = token
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=ASK_TIMEOUT)
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        response.read()
        if response.status in (401, 403):
            return "denied", None
        if response.status != 200:
            return "down", None
        for header in response.headers.get_all("Set-Cookie") or []:
            first = header.split(";", 1)[0].strip()
            if first.startswith("mitmproxy-auth"):
                return "ok", first
        return "ok", None
    except ConnectionRefusedError:
        return "refused", None
    except (OSError, http.client.HTTPException, ValueError):
        return "down", None
    finally:
        connection.close()


def clear(url, password):
    """Empties the flow list of the mitmweb at url (POST /clear, as its page's Clear does)."""
    status, _cookie = ask(url, "POST", "/clear", password=password, xsrf=True)
    return status == "ok"


def say(name, text):
    print(f"beanjar: {name}: {text}", file=sys.stderr, flush=True)


def until_eof():
    """An Event set when stdin ends (read by a thread of its own, so the caller can do other
    things meanwhile)."""
    import threading

    ended = threading.Event()

    def read():
        try:
            while sys.stdin.buffer.read1(65536):
                pass
        except (OSError, ValueError):
            pass
        ended.set()

    threading.Thread(target=read, daemon=True).start()
    return ended


def hold(name, data, fetch, link):
    """`mitm connect`'s answer, kept true until stdin ends: {url, password} as one JSON line on
    stdout, and another whenever they change (the VM restarted: a new mitmweb with a new password,
    or for a remote VM a forward that had to move). Every CHECK s it asks mitmweb, with the session
    cookie its first login set (no argon2 check after that one); when that fails it asks fetch()
    for the VM's state (show's dict; at most every RESHOW s) and has link(data) give the URL from
    here, and link.restore() a forward that is gone. Stderr says when the inspector is gone and
    when it is back."""
    url = link(data)
    password = data["password"]
    shown = (url, password)
    print(json.dumps({"url": url, "password": password}), flush=True)
    ended = until_eof()
    cookie, down, said, asked = None, False, None, 0.0
    import time

    while not ended.wait(CHECK):
        status, got = ask(url, cookie=cookie, password=None if cookie else password)
        if status == "ok":
            cookie = got or cookie
            if (url, password) != shown:
                shown = (url, password)
                print(json.dumps({"url": url, "password": password}), flush=True)
                say(name, "the inspector is back, with a new password (a new line on stdout)")
            elif said:
                say(name, "the inspector is back")
            down, said = False, None
            continue
        cookie = None
        if status == "refused" and link.restore and link.restore():
            continue  # the shared connection had ended, taking the forward along: back now
        if down and time.monotonic() - asked < RESHOW:
            continue
        down, asked = True, time.monotonic()
        try:
            data = fetch()
            check_ready(name, data)
            url, password = link(data), data["password"]
        except base.Error as e:
            if str(e) != said:
                said = str(e)
                say(name, f"{said}: waiting for it")


class Local:
    """A VM of this machine: its inspector at its own address, no forward."""

    restore = None

    def __call__(self, data):
        return data["local_url"] or data["url"]


def connect_local(vm):
    """`mitm connect NAME`: the inspector of a VM of this machine, at its own address (no forward:
    this host reaches it)."""
    data = show(vm, password=True)
    check_ready(vm.name, data)
    hold(vm.name, data, lambda: show(vm, password=True), Local())
    return 0


def remote_show(host, uri, name):
    """`mitm show local/NAME --password --json` on host: one call over its shared connection."""
    from .. import remote

    data = remote.call_json(host, uri, ["mitm", "show", remote.own(name), "--password"])
    if not isinstance(data, dict) or not all(
        isinstance(data.get(k), t) for k, t in (("mode", str), ("status", str), ("ready", bool))
    ):
        raise remote.HostError(host, "its beanjar mitm show is not what this one reads: update it")
    return data


def remote_target(host, data):
    """The inspector's address and port on host from its mitm show (local_url with link lan)."""
    from .. import remote

    try:
        target = urlsplit(data.get("local_url") or data["url"])
        address, port = target.hostname, target.port
        if not address or not port or not isinstance(data.get("password"), str):
            raise ValueError
    except (TypeError, ValueError):
        raise remote.HostError(host, "its beanjar mitm show gave no inspector address") from None
    return address, port


class Remote:
    """A VM of host: its inspector through a forward from 127.0.0.1:PORT here on the host's shared
    connection (ares.Forward), recorded in home for ares.sweep_homes; a new forward when the
    inspector's address there changes, the same one again when the connection ended."""

    def __init__(self, host, uri, home):
        self.host, self.uri, self.home = host, uri, home
        self.forward = self.port = None

    def __call__(self, data):
        from .. import ares

        address, port = remote_target(self.host, data)
        if self.forward and (self.forward.address, self.forward.to_port) != (address, port):
            self.close()
        if self.forward is None:
            forward = ares.Forward(
                self.host, self.uri, address, port, os.path.join(self.home, ares.FORWARD_RECORD)
            )
            self.port = forward.__enter__()
            self.forward = forward
        return f"http://{ares.FORWARD_HOST}:{self.port}/"

    def restore(self):
        """The forward again on a new shared connection: whether it is back. If not (its port is
        taken here, the host is not reached) it is dropped, and the next link(data) makes one."""
        from .. import remote

        if self.forward is None:
            return False
        try:
            if self.forward.restore():
                return True
        except remote.HostError:
            pass
        self.close()
        return False

    def close(self):
        forward, self.forward = self.forward, None
        if forward:
            forward.__exit__()


def remote_session(host, name, use):
    """use(data, link, fetch) for VM name on host (link a Remote, fetch its mitm show), refused (check_ready) if its inspector cannot be
    reached; the forward cancelled at the end, also on SIGTERM, SIGHUP or Ctrl-C, and recorded
    for ares.sweep_homes should this process be killed outright: use's status."""
    import shutil
    import signal
    import tempfile

    from .. import ares, remote

    uri = remote.hosts()[host]
    data = remote_show(host, uri, name)
    check_ready(name, data)
    remote_target(host, data)
    old = {s: signal.signal(s, ares.raise_terminated) for s in (signal.SIGTERM, signal.SIGHUP)}
    home = link = None
    try:
        ares.sweep_homes(ares.home_parent())
        home = tempfile.mkdtemp(prefix=f"{MITM_PREFIX}{os.getpid()}-", dir=ares.home_parent())
        link = Remote(host, uri, home)
        return use(data, link, lambda: remote_show(host, uri, name))
    except ares.Terminated as e:
        return e.status
    finally:
        if link:
            link.close()
        if home:
            shutil.rmtree(home, ignore_errors=True)
        for s, handler in old.items():
            signal.signal(s, handler)


def connect_remote(host, name):
    """`mitm connect HOST/NAME`: hold() with the inspector on host through a forward (Remote)."""

    def use(data, link, fetch):
        hold(name, data, fetch, link)
        return 0

    return remote_session(host, name, use)


def cleared(name, url, password):
    if not clear(url, password):
        raise base.Error(f"VM {name}'s traffic inspector did not clear its flows")
    return {"name": name, "cleared": True}


def clear_local(vm):
    """`mitm clear NAME`: the flows of a VM of this machine, emptied."""
    data = show(vm, password=True)
    check_ready(vm.name, data)
    return cleared(vm.name, Local()(data), data["password"])


def clear_remote(host, name):
    """`mitm clear HOST/NAME`: through a forward as connect's, for as long as the request takes."""
    answer = []

    def use(data, link, _fetch):
        answer.append(cleared(name, link(data), data["password"]))
        return 0

    remote_session(host, name, use)
    return answer[0] if answer else None
