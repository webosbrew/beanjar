#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The mode=mitm namespace (host/core/beanjar/netns.py) end to end, with a fake guest in QEMU's
place (fake-tap-guest: a network namespace of its own on the other end of the TAP) and servers on
this host as the internet. Without a VM, and without internet access.

One netns.run() run with mode=mitm, as the launcher makes it: the relays, `unshare -rn`, slirp4netns, the
proxy-ARP routing, DHCP and mitmweb in tun mode (the user's own, host/core/beanjar/mitmtool.py),
every TCP and UDP port of the guest routed into its TUN device. The guest checks its lease (router
and DNS slirp's, the server identifier the TAP's own address) and a unicast renewal; a
server-first and a client-first TCP service on this host carried unchanged (eager connections);
a closed port accepted and closed rather than refused; the host's loopback unreachable; HTTP and
HTTPS on any port decrypted with our CA's leaf and UDP carried, all in the inspector with their
real destination; a connection to the namespace's own address refused rather than looping. The
forwarded port reaches the guest through the relays. No `nft` is run (a failing one is first on
PATH). Everything started is gone afterwards. That run has LAN access (NET_LAN_ACCESS=on), since
its fake internet is this host's own address; shorter runs (ReachRun) check what the guest reaches
in mode=direct and mode=mitm without it, and with it: this host's address refused, its loopback
always, the forwarded port still served.

Skips (exit 77, a meson SKIP) without unprivileged user namespaces, slirp4netns or ip; the mitm runs
also without a usable mitmweb (mitmproxy 12.0.1 or newer: pipx install mitmproxy). `meson test -C build/host --suite
network`, or `BEANJAR_RELAY=... pytest tests/integration/network/test_netns.py`."""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest
import urllib.error
import urllib.request

import testunits

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PKG = os.path.join(TOP, "host", "core")
sys.path.insert(0, PKG)
from beanjar import mitmtool, netns  # noqa: E402

MITMWEB = mitmtool.check()
PASSWORD = "test-netns-inspector-pw"
SBIN = os.environ.get("PATH", os.defpath) + ":/usr/sbin:/sbin"


def unavailable():
    for tool in ("unshare", "ip", "slirp4netns"):
        if not shutil.which(tool, path=SBIN):
            return f"{tool} not found"
    if not os.access("/dev/net/tun", os.R_OK | os.W_OK):
        return "no /dev/net/tun"
    if subprocess.run(["unshare", "-rn", "true"], stderr=subprocess.DEVNULL).returncode != 0:
        return "no unprivileged user namespaces"
    return None


pytestmark = pytest.mark.skipif(bool(unavailable()), reason=unavailable() or "")


# the VM's host address (qemu.py's VM_ADDRESS, 127.0.1.<slot>): the forwards and the inspector bind there
VM_ADDRESS = "127.0.1.250"


def free_port():
    with socket.socket() as s:
        s.bind((VM_ADDRESS, 0))
        return s.getsockname()[1]


def host_address():
    """This host's address towards the internet (no packet is sent), which slirp reaches as any
    other; None if it only has loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            addr = s.getsockname()[0]
        except OSError:
            return None
    return None if addr.startswith("127.") else addr


def connect_when_up(host, port, timeout=15):
    """Connect to a forwarded port. The relays open it only while the guest's side answers, a moment
    after the guest listens, so a refusal is retried until timeout."""
    end = time.monotonic() + timeout
    while True:
        try:
            return socket.create_connection((host, port), 5)
        except ConnectionRefusedError:
            if time.monotonic() > end:
                raise
            time.sleep(0.1)


class Server:
    """A TCP server on host:0: 'banner' speaks first, 'echo' answers what it is sent."""

    def __init__(self, host, kind):
        self.sock = socket.create_server((host, 0))
        self.port = self.sock.getsockname()[1]
        self.kind = kind
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                c.settimeout(5)
                try:
                    if self.kind == "banner":
                        c.sendall(b"SSH-2.0-fake\r\n")
                    else:
                        c.sendall(b"echo:" + c.recv(100))
                except OSError:
                    pass

    def close(self):
        self.sock.close()


class HttpServer:
    """An HTTP server on host:0, HTTPS with a self-signed certificate if tls: what the
    interception of every port reaches upstream."""

    def __init__(self, host, tmp, tls=False):
        import http.server
        import socketserver
        import ssl

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"upstream-ok\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        class Httpd(http.server.ThreadingHTTPServer):
            def server_bind(self):  # without HTTPServer's getfqdn: a reverse lookup, seconds
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = "upstream.test", self.server_address[1]

        self.httpd = Httpd((host, 0), Handler)
        if tls:
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=upstream.test", "-keyout", f"{tmp}/up.key", "-out", f"{tmp}/up.pem"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )  # fmt: skip
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(f"{tmp}/up.pem", f"{tmp}/up.key")
            plain = self.httpd.get_request

            def get_request():  # the handshake in the request's thread, not the accept loop's
                sock, peer = plain()
                sock.settimeout(5)
                return ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), peer

            self.httpd.get_request = get_request
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class UdpEcho:
    def __init__(self, host):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                data, peer = self.sock.recvfrom(4096)
                self.sock.sendto(b"udp:" + data, peer)
            except OSError:
                return

    def close(self):
        self.sock.close()


class Run:
    """One netns.run() run (mode=mitm, LAN access) with its fake guest, and what was observed of it."""

    def __init__(self, addr, tmp):
        self.addr = addr
        self.tmp = str(tmp)
        t = self.tmp
        self.servers = [
            Server(self.addr, "banner"),
            Server(self.addr, "echo"),
            Server("127.0.0.1", "banner"),
            HttpServer(self.addr, t),
            HttpServer(self.addr, t, tls=True),
            UdpEcho(self.addr),
        ]
        banner, echo, loopback, web, tls, udp = (s.port for s in self.servers)
        closed = free_port()
        # a failing nft first on PATH, recording that it ran
        os.makedirs(f"{t}/bin")
        with open(f"{t}/bin/nft", "w") as f:
            f.write(f"#!/bin/sh\ntouch {t}/nft-ran\nexit 1\n")
        os.chmod(f"{t}/bin/nft", 0o755)
        cafile = f"{t}/ca/ca.pem"
        spec = {
            "banner": {"kind": "tcp", "host": self.addr, "port": banner},
            "echo": {"kind": "tcp", "host": self.addr, "port": echo, "send": "ping"},
            "closed": {"kind": "tcp", "host": self.addr, "port": closed},
            "loopback": {"kind": "tcp", "host": "10.0.2.2", "port": loopback},
            "http": {
                "kind": "tcp",
                "host": self.addr,
                "port": 80,
                "send": "GET /probe HTTP/1.1\r\nHost: intercept.test\r\nConnection: close\r\n\r\n",
                "timeout": 30,  # a connection held while mitmweb starts waits (netns._hold)
            },
            "https": {
                "kind": "https",
                "host": self.addr,
                "port": 443,
                "sni": "intercept.test",
                "cafile": cafile,
                "timeout": 30,
            },
            # an HTTP(S) server on another port, and UDP: intercepted too
            "web": {
                "kind": "tcp",
                "host": self.addr,
                "port": web,
                "send": "GET /web HTTP/1.1\r\nHost: web.test\r\nConnection: close\r\n\r\n",
                "timeout": 30,
            },
            "tls": {
                "kind": "https",
                "host": self.addr,
                "port": tls,
                "sni": "upstream.test",
                "cafile": cafile,
                "timeout": 30,
            },
            "udp": {"kind": "udp", "host": self.addr, "port": udp, "send": "ping", "timeout": 10},
            "self": {
                "kind": "tcp",
                "host": "10.0.2.254",
                "port": 81,  # 80 is the LG mock's online check (lg_mock.py)
                "send": "GET / HTTP/1.1\r\nHost: 10.0.2.254\r\nConnection: close\r\n\r\n",
                "timeout": 30,
            },
        }
        with open(f"{t}/spec.json", "w") as f:
            json.dump(spec, f)
        self.fwd, guest_port, self.web = free_port(), 2222, free_port()
        env = dict(
            os.environ,
            PATH=f"{t}/bin:{SBIN}",
            PYTHONPATH=os.pathsep.join(filter(None, (PKG, os.environ.get("PYTHONPATH")))),
            NETNS_SOCK_DIR=f"{t}/run",
            NETNS_LOG_DIR=t,
            NETSTUB_LOG=f"{t}/netstub.log",
            NETSTUB_CA=f"{t}/ca",
            NETNS_TAP="tap0",
            NETSTUB_WEB_PORT=str(self.web),
            NETNS_FWD=f"{self.fwd}:{guest_port}",
            VM_ADDRESS=VM_ADDRESS,
            FAKE_GUEST_SPEC=f"{t}/spec.json",
            FAKE_GUEST_RESULTS=f"{t}/results.json",
            FAKE_GUEST_STOP=f"{t}/stop",
            FAKE_GUEST_PORT=str(guest_port),
            BEANJAR_MITM_PASSWORD=PASSWORD,
            NETNS_MITMWEB=MITMWEB.path,
            NET_LAN_ACCESS="on",  # the fake internet is this host's own address
        )
        with open(f"{t}/out.log", "w") as out:
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; from beanjar import netns; "
                    "sys.exit(netns.run(sys.argv[1:], os.environ))",
                    sys.executable,
                    os.path.join(TOP, "tests", "fakes", "fake-tap-guest"),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        end = time.monotonic() + 90
        while not os.path.exists(f"{t}/results.json"):
            if self.proc.poll() is not None or time.monotonic() > end:
                self.stop()
                raise AssertionError("the fake guest gave no results:\n" + self.logs())
            time.sleep(0.2)
        with open(f"{t}/results.json") as f:
            self.results = json.load(f)
        # what the host sees while the guest runs: the forward and the inspector's flows
        try:
            with connect_when_up(VM_ADDRESS, self.fwd) as c:
                self.forwarded = c.recv(100)
        except OSError as e:
            self.forwarded = repr(e).encode()
        try:  # nothing of the VM's on another address
            socket.create_connection(("127.0.0.1", self.fwd), 5).close()
            self.elsewhere = True
        except OSError:
            self.elsewhere = False
        # the inspector: its password file, and mitmweb behind it
        self.password_file = f"{t}/run/mitmweb.password"
        self.password_mode = os.stat(self.password_file).st_mode & 0o777
        with open(self.password_file) as f:
            self.password = f.read().strip()
        url = f"http://{VM_ADDRESS}:{self.web}/flows"
        self.anonymous = self.status(urllib.request.Request(url))
        self.wrong = self.status(urllib.request.Request(url + "?token=wrong"))
        self.token = self.status(urllib.request.Request(url + "?token=" + PASSWORD))
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"Authorization": f"Bearer {PASSWORD}"}), timeout=10
        ) as r:
            self.flows = json.load(r)
        # the options API: what loads code refused, the rest as before (the XSRF check is a
        # double submit: any value in both the cookie and the header)
        never = f"{t}/never-loaded.py"  # empty: a missing file would be refused anyway
        open(never, "w").close()
        self.put_scripts = self.put_options({"scripts": [never]})
        self.put_intercept = self.put_options({"intercept": "~d never.test"})
        # mitmweb's view of the filesystem (its sandbox, netns.mitmweb_sandbox)
        self.mitmweb_view = self.sandbox_view()
        # netns.run's own process was started with BEANJAR_MITM_PASSWORD (as a shell would start
        # it); what it starts must not have it
        self.environ_leaks = [
            pid
            for pid, env in self.environs()
            if PASSWORD.encode() in env and pid != str(self.proc.pid)
        ]
        self.rc = self.stop()

    @staticmethod
    def status(request):
        try:
            with urllib.request.urlopen(request, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def put_options(self, body):
        request = urllib.request.Request(
            f"http://{VM_ADDRESS}:{self.web}/options",
            data=json.dumps(body).encode(),
            method="PUT",
            headers={
                "Authorization": f"Bearer {PASSWORD}",
                "Content-Type": "application/json",
                "Cookie": "_mitmproxy_xsrf=x",
                "X-XSRFToken": "x",
            },
        )
        return self.status(request)

    def sandbox_view(self):
        """What mitmweb's process sees (through /proc/PID/root): {path: exists} for the run's
        files it must not see and the ones it must, or None if no mitmweb was found."""
        for pid, _env in self.environs():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
            except OSError:
                continue
            if argv[0].endswith(b"/bwrap") or not any(a.endswith(b"/mitmweb") for a in argv):
                continue  # its bwrap, or another of the run's processes
            root = f"/proc/{pid}/root"
            home = os.path.expanduser("~")
            paths = {
                "checkout": f"{TOP}/AGENTS.md",
                "addon": f"{TOP}/host/helpers/network/mitm_addon.py",
                "out.log": f"{self.tmp}/out.log",
                "spec": f"{self.tmp}/spec.json",
                "ca": f"{self.tmp}/ca/ca.pem",
                "ready": f"{self.tmp}/run/{netns.MITM_STATE_DIR}/ready",
                "sockets": f"{self.tmp}/run/mitmweb.sock",
                "home": home,
            }
            view = {k: os.path.exists(root + p) for k, p in paths.items()}
            view["home entries"] = sorted(os.listdir(root + home)) if view["home"] else []
            return view
        return None

    def environs(self):
        """(pid, environ) of every process of the run."""
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    env = f.read()
            except OSError:
                continue
            if self.tmp.encode() in env:
                yield pid, env

    def stop(self):
        open(f"{self.tmp}/stop", "w").close()
        try:
            return self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            testunits.end(self.proc.pid, group=True)
            self.proc.wait()
            return None

    def logs(self):
        out = []
        for name in ("out.log", "netns-uplink.log", "netns-helper.log", "netstub-mitm.log"):
            try:
                with open(os.path.join(self.tmp, name)) as f:
                    out.append(f"--- {name}\n{f.read()[-3000:]}")
            except OSError:
                pass
        return "\n".join(out)

    def close(self):
        for s in self.servers:
            s.close()

    def result(self, name):
        return self.results[name]

    def destinations(self):
        return {
            tuple(f["server_conn"]["address"])
            for f in self.flows
            if f.get("server_conn", {}).get("address")
        }


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if not MITMWEB.usable:
        pytest.skip(MITMWEB.problem)
    addr = host_address()
    if not addr:
        pytest.skip("no non-loopback host address for the fake internet")
    r = Run(addr, tmp_path_factory.mktemp("test-netns"))
    yield r
    r.close()


class ReachRun:
    """One netns.run() run (mode, NET_LAN_ACCESS lan_access) with its fake guest probing this
    host's own address (TCP and UDP servers on it) and its loopback (10.0.2.2), and the forwarded
    port seen from the host."""

    def __init__(self, addr, tmp, mode, lan_access, lg="mock"):
        self.tmp = t = str(tmp)
        self.servers = [Server(addr, "banner"), UdpEcho(addr), Server("127.0.0.1", "banner")]
        banner, udp, loopback = (x.port for x in self.servers)
        spec = {
            "banner": {"kind": "tcp", "host": addr, "port": banner, "timeout": 10},
            "udp": {"kind": "udp", "host": addr, "port": udp, "send": "ping", "timeout": 5},
            "loopback": {"kind": "tcp", "host": "10.0.2.2", "port": loopback, "timeout": 10},
            # lg_services: the namespace's resolver (refused and answered names, nothing leaves),
            # its online check, and with the mock the resolver bypass and a literal LG address
            "lg-refused": dns_probe("10.0.2.254", "su.lge.com"),
            "lg-online-name": dns_probe("10.0.2.254", "lgtvonline.lge.com"),
            "lg-online": {
                "kind": "tcp",
                "host": "10.0.2.254",
                "port": 80,
                "send": "GET / HTTP/1.1\r\nHost: lgtvonline.lge.com\r\n\r\n",
                "timeout": 10,
            },
        }
        if lg == "mock":
            # SDP: resolved to the namespace, answered over HTTPS with a certificate of the CA
            spec["lg-sdp-name"] = dns_probe("10.0.2.254", "us.lgtvsdp.com")
            spec["lg-sdp"] = {
                "kind": "https",
                "host": "10.0.2.254",
                "port": 443,
                "sni": "us.lgtvsdp.com",
                "cafile": f"{t}/ca/ca.pem",
                "timeout": 10,
            }
            spec["lg-bypass"] = dns_probe(netns.DNS, "su.lge.com")
            spec["lg-address"] = {
                "kind": "tcp",
                "host": "156.147.69.32",
                "port": 8080,
                "timeout": 10,
            }
        with open(f"{t}/spec.json", "w") as f:
            json.dump(spec, f)
        self.fwd, guest_port = free_port(), 2222
        env = dict(
            os.environ,
            PATH=SBIN,
            PYTHONPATH=os.pathsep.join(filter(None, (PKG, os.environ.get("PYTHONPATH")))),
            NETNS_SOCK_DIR=f"{t}/run",
            NETNS_LOG_DIR=t,
            NETSTUB_LOG=f"{t}/netstub.log",
            NETSTUB_CA=f"{t}/ca",
            NETNS_TAP="tap0",
            NETSTUB_WEB_PORT=str(free_port()),
            NETNS_FWD=f"{self.fwd}:{guest_port}",
            NET_MODE=mode,
            NET_LAN_ACCESS=lan_access,
            NET_LG_SERVICES=lg,
            VM_ADDRESS=VM_ADDRESS,
            FAKE_GUEST_SPEC=f"{t}/spec.json",
            FAKE_GUEST_RESULTS=f"{t}/results.json",
            FAKE_GUEST_STOP=f"{t}/stop",
            FAKE_GUEST_PORT=str(guest_port),
            NETNS_MITMWEB=MITMWEB.path or "",
        )
        with open(f"{t}/out.log", "w") as out:
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; from beanjar import netns; "
                    "sys.exit(netns.run(sys.argv[1:], os.environ))",
                    sys.executable,
                    os.path.join(TOP, "tests", "fakes", "fake-tap-guest"),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        end = time.monotonic() + 90
        while not os.path.exists(f"{t}/results.json"):
            if self.proc.poll() is not None or time.monotonic() > end:
                self.stop()
                raise AssertionError("the fake guest gave no results:\n" + Run.logs(self))
            time.sleep(0.2)
        with open(f"{t}/results.json") as f:
            self.results = json.load(f)
        self.rules = self.inner_rules()
        try:
            with connect_when_up(VM_ADDRESS, self.fwd) as c:
                self.forwarded = c.recv(100)
        except OSError as e:
            self.forwarded = repr(e).encode()
        self.mitm_files = sorted(n for n in os.listdir(f"{t}/run") if n.startswith("mitmweb"))
        self.rc = self.stop()
        with open(f"{t}/out.log") as f:
            self.out = f.read()

    def inner_rules(self):
        """`ip rule` of the inner namespace (netns.inner's, where QEMU would run)."""
        for pid, _env in Run.environs(self):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
            except OSError:
                continue
            if b"beanjar.netns" not in argv or b"inner" not in argv:
                continue
            r = subprocess.run(
                ["nsenter", "-t", pid, "-U", "-n", "--preserve-credentials", "ip", "rule"],
                env=dict(os.environ, PATH=SBIN),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if r.returncode == 0 and "lookup local" in r.stdout:
                return r.stdout
        return ""

    def stop(self):
        return Run.stop(self)

    def close(self):
        for x in self.servers:
            x.close()


def reach_run(tmp_path_factory, mode, lan_access, lg="mock"):
    if mode == "mitm" and not MITMWEB.usable:
        pytest.skip(MITMWEB.problem)
    addr = host_address()
    if not addr:
        pytest.skip("no non-loopback host address")
    return ReachRun(
        addr, tmp_path_factory.mktemp(f"test-netns-{mode}-{lan_access}-{lg}"), mode, lan_access, lg
    )


@pytest.fixture(
    scope="module",
    params=[("direct", "off", "mock"), ("direct", "on", "mock"), ("mitm", "off", "mock"),
            ("direct", "off", "real")],
)  # fmt: skip
def reach(request, tmp_path_factory):
    r = reach_run(tmp_path_factory, *request.param)
    r.mode, r.lan_access, r.lg = request.param
    yield r
    r.close()


def dns_query(name, qid=0x1234):
    """A DNS query for name's A record: bytes below 0x80 only, so the fake guest's str.encode()
    sends it unchanged."""
    q = bytes((qid >> 8, qid & 0xFF, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    for label in name.split("."):
        q += bytes([len(label)]) + label.encode()
    return q + bytes((0, 0, 1, 0, 1))


def dns_probe(server, name):
    return {
        "kind": "udp",
        "host": server,
        "port": 53,
        "send": dns_query(name).decode(),
        "timeout": 5,
    }


def test_reach_lg_services(reach):
    """lg_services=mock, in mode=direct and mode=mitm: a telemetry name NXDOMAIN, the online check
    resolved to the namespace and answered as LG's server does, SDP resolved there and answered
    over HTTPS with a certificate of the library's CA, a resolver other than the
    namespace's refused, a literal LG address refused at once. lg_services=real: none of it, the
    guest's resolver slirp's."""
    r = reach.results
    if reach.lg == "real":
        assert r["dhcp"]["dns"] == netns.DNS, r["dhcp"]
        assert not r["lg-refused"].get("data") and not r["lg-online"].get("data"), r
        assert "156.147.69.32" not in reach.rules and "dport 53" not in reach.rules, reach.rules
        return
    assert r["dhcp"]["dns"] == netns.MOCK_DNS, r["dhcp"]
    refused = r["lg-refused"]["data"].encode("latin-1")
    assert refused[:2] == b"\x12\x34" and refused[3] & 0xF == 3, r["lg-refused"]  # NXDOMAIN
    online = r["lg-online-name"]["data"].encode("latin-1")
    assert online[3] & 0xF == 0 and online[-4:] == socket.inet_aton(netns.MOCK_DNS), online
    sys.path.insert(0, os.path.join(TOP, "host", "helpers", "network"))
    import lg_mock

    assert r["lg-online"]["data"].encode("latin-1") == lg_mock.ONLINE_RESPONSE, r["lg-online"]
    sdp = r["lg-sdp-name"]["data"].encode("latin-1")
    assert sdp[3] & 0xF == 0 and sdp[-4:] == socket.inet_aton(netns.MOCK_DNS), sdp
    assert r["lg-sdp"].get("issuer") == "Beanjar netstub local CA", r["lg-sdp"]
    assert r["lg-sdp"]["data"].startswith("HTTP/1.1 404 "), r["lg-sdp"]  # /probe: unknown
    assert not r["lg-bypass"].get("data"), r["lg-bypass"]
    address = r["lg-address"]
    assert address.get("error") == "OSError" and address["seconds"] < 3, address  # prohibit
    for rule in ("to 156.147.69.32 prohibit", "iif tap0 ipproto udp dport 53 prohibit"):
        assert f"90:\tfrom all {rule}" in reach.rules, reach.rules


def test_reach_exit_and_cleanup(reach):
    assert reach.rc == 0, reach.out
    assert not os.path.exists(f"{reach.tmp}/run"), "socket dir left behind"
    assert [pid for pid, _ in Run.environs(reach)] == [], "processes of the run still alive"


def test_reach_loopback_never(reach):
    assert not reach.results["loopback"].get("data"), reach.results


def test_reach_rules(reach):
    assert "90:\tfrom all to 127.0.0.0/8 prohibit" in reach.rules, reach.rules
    assert ("to 192.168.0.0/16 prohibit" in reach.rules) == (reach.lan_access == "off"), reach.rules
    assert "to 10.0.2.0/24" not in reach.rules


def test_reach_host_address(reach):
    """This host's own (private) address: refused at once without LAN access, reached with it."""
    tcp, udp = reach.results["banner"], reach.results["udp"]
    if reach.lan_access == "on":
        assert tcp.get("data") == "SSH-2.0-fake\r\n", reach.results
        assert udp.get("data") == "udp:ping", reach.results
    else:
        assert not tcp.get("data") and not udp.get("data"), reach.results
        if reach.mode == "direct":
            # prohibit: ICMP administratively prohibited, EHOSTUNREACH at once, not a timeout
            assert tcp.get("error") == "OSError", tcp
            assert tcp["seconds"] < 3, tcp


def test_reach_forwarded_port(reach):
    assert reach.forwarded == b"guest-banner\n"


def test_reach_direct_has_no_inspector(reach):
    if reach.mode != "direct":
        pytest.skip("mode=mitm")
    assert reach.mitm_files == [], reach.mitm_files
    assert "traffic inspector" not in reach.out, reach.out
    assert "the host's loopback unreachable" in reach.out


def test_exit_and_cleanup(run):
    assert run.rc == 0, run.logs()
    assert not os.path.exists(f"{run.tmp}/run"), "socket dir left behind"
    assert [pid for pid, _ in run.environs()] == [], "processes of the run still alive"


def test_inspector_password(run):
    assert run.password == PASSWORD
    assert run.password_mode == 384
    assert (run.anonymous, run.wrong, run.token) == (403, 403, 200)
    assert run.environ_leaks == [], "the password in a process environment"
    assert not os.path.exists(run.password_file)
    for name in ("out.log", "netstub-mitm.log", "netstub.log"):
        with open(os.path.join(run.tmp, name)) as f:
            assert PASSWORD not in f.read(), name


def test_inspector_options_frozen(run):
    """PUT /options with scripts is refused (mitm_addon.Freeze); intercept still changes."""
    assert (run.put_scripts, run.put_intercept) == (400, 200)
    with open(os.path.join(run.tmp, "netstub-mitm.log")) as f:
        assert "refused a change of scripts" in f.read()


def test_inspector_sandboxed(run):
    """mitmweb sees its addon, CA and ready file, and none of the checkout, the run's other files,
    its sockets or the home directory beyond its own installation."""
    view = run.mitmweb_view
    assert view is not None, "no mitmweb process found"
    assert (view["addon"], view["ca"], view["ready"]) == (True, True, True), view
    assert not any(view[k] for k in ("checkout", "out.log", "spec", "sockets")), view
    # of the home directory, only the way to what is bound: the venv (pipx's) and the addon
    home = os.path.expanduser("~")
    bound = (mitmtool.python(MITMWEB.path) or "", os.path.realpath(MITMWEB.path), TOP)
    ways = {p[len(home) + 1 :].split("/")[0] for p in bound if p.startswith(home + "/")}
    assert set(view["home entries"]) <= ways, view


def test_no_nft(run):
    assert not os.path.exists(f"{run.tmp}/nft-ran")


def test_dhcp(run):
    d = run.result("dhcp")
    assert (d["offer"], d["ack"], d["mask"], d["router"], d["dns"], d["server"]) == (
        "10.0.2.15",
        "10.0.2.15",
        "255.255.255.0",
        "10.0.2.2",
        "10.0.2.254",  # lg_services=mock (the default): lg_mock.py's resolver, the TAP's address
        "10.0.2.254",
    ), d
    assert (d["renew"], d["renew_server"]) == ("10.0.2.15", "10.0.2.254"), d


def test_other_tcp_through_slirp(run):
    assert run.result("banner").get("data") == "SSH-2.0-fake\r\n", run.results
    assert run.result("echo").get("data") == "echo:ping", run.results


def test_closed_port_accepted_then_closed(run):
    r = run.result("closed")
    # mitmweb's TUN stack accepts every SYN, then closes once its upstream is refused
    assert r.get("data") == "" or r.get("error"), run.results
    assert r.get("error") != "ConnectionRefusedError", run.results
    assert r["seconds"] < 5, r


def test_host_loopback_unreachable(run):
    assert not run.result("loopback").get("data"), run.results


def test_http_without_upstream_closed(run):
    # nothing listens on the fake internet's port 80: eager mitmweb closes the connection (the
    # refused upstream is no HTTP flow: the web and tls probes show the flows)
    data = run.result("http").get("data", "")
    assert data == "" or run.result("http").get("error"), run.results


def test_https_without_upstream_closed(run):
    r = run.result("https")
    assert "data" not in r or not r["data"], r


def test_other_ports(run):
    """HTTP and HTTPS on other ports decrypted, UDP carried, all in the inspector."""
    web, tls, udp = run.result("web"), run.result("tls"), run.result("udp")
    assert web.get("data", "").split(" ")[1:2] == ["200"], web
    assert udp.get("data") == "udp:ping", udp
    seen = run.destinations()
    assert tls.get("issuer") == "Beanjar netstub local CA", tls
    assert tls.get("data", "").split(" ")[1:2] == ["200"], tls
    assert {(run.addr, s.port) for s in run.servers[3:6]} <= seen, seen
    kinds = {f["type"] for f in run.flows}
    assert {"http", "udp"} <= kinds, kinds


def test_own_address_refused_not_looping(run):
    r = run.result("self")
    # the namespace's own address is the local table's (pref 0), before the interception
    assert r.get("error") == "ConnectionRefusedError", r
    assert r["seconds"] < 5, r


def test_forwarded_port(run):
    assert run.forwarded == b"guest-banner\n"
    assert not run.elsewhere, "the forward is on the VM's address only"


def test_mitm_log(run):
    with open(f"{run.tmp}/netstub-mitm.log") as f:
        log = f.read()
    assert "Traceback" not in log
    assert "UDP_TIMEOUT" not in log  # the addon raised it
