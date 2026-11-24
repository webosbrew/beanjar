# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""LG's SDP server and LG's other web hosts, answered in the VM's network namespace for
lg_services = "mock" (the default): the HTTP and HTTPS side of lg_mock.py.

lg_mock.py resolves every name of an "answer" class of lg_hosts.py (SDP, and LG's content, store
and account hosts) to its own address; the guest's connections to it on port 443 and 80 land here,
in every network mode and whether mitmweb runs or not. Nothing is forwarded: each request gets the
first answer of lg_answers.py (data only) whose host class, method and path match, else
lg_answers.DEFAULT (404, an empty JSON object), and is logged so the paths a firmware asks for can be
added.

TLS: a certificate for the name the client asks for (SNI), issued by the library's interception CA
(tlsca.py's, which guest/initramfs/rootfs/init adds to the guest's ca-certificates, sdp-ca.pem and
NSS in both modes when LG's services are mocked), minted with the system's openssl on first use and
cached in --certs; clients without SNI get one certificate for all of the answered domains.
Clients with their own roots (Node, which none of the answered hosts serves) fail the handshake,
logged as tls-failed; nothing goes to LG either way.

Answers carry X-Server-Time (Unix milliseconds, the host's clock) on success as LG's server does:
the guest's time source "sdp" sets the clock from it (etc/palm/defaultPreferences.txt timeSources).

Python 3 stdlib only.
"""

import http.server
import json
import os
import re
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse
import warnings

sys.dont_write_bytecode = True  # no __pycache__ in the source tree
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lg_answers
import lg_hosts
import tlsca

HANDSHAKE_TIMEOUT = 15  # s
IDLE_TIMEOUT = 60  # s between requests on a kept-alive connection
MAX_BODY = 4 << 20  # request bytes read and dropped at most; a larger one closes the connection
NAME_CHARS = re.compile(r"^[a-z0-9.-]{1,253}$")


def answered(name):
    """Whether lg_services = "mock" answers the DNS name here (its class's mock is "answer")."""
    return lg_hosts.mock_action(name) == "answer"


def default_names():
    """The names of the certificate for clients that send no SNI: each answered pattern without a
    "*", and every name one label below it."""
    names = []
    for c in lg_hosts.CLASSES:
        if c["mock"] != "answer":
            continue
        for p in c["names"]:
            if "*" not in p:
                names += [p, "*." + p]
    return names


class Certs:
    """Per-name TLS contexts with certificates of the CA in ca_dir, minted into cache_dir."""

    def __init__(self, ca_dir, cache_dir):
        self.ca_dir, self.dir = ca_dir, cache_dir
        os.makedirs(cache_dir, mode=0o700, exist_ok=True)
        self.key = os.path.join(cache_dir, "leaf.key")
        if not os.path.isfile(self.key):
            tlsca.new_key(self.key + ".tmp")
            os.replace(self.key + ".tmp", self.key)
        self.lock = threading.Lock()
        self.contexts = {}
        self.sni = threading.local()  # the name the handshake in this thread asked for
        self.default = self._context("_default", default_names())

    @staticmethod
    def server_context():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # the guest's clients go back to OpenSSL 1.0.1 (WT1H): TLS 1.0 and its ciphers allowed
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # TLSv1, deprecated but wanted
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.set_alpn_protocols(["http/1.1"])
        return ctx

    def _context(self, label, names):
        pem = os.path.join(self.dir, label + ".pem")
        if not os.path.isfile(pem):
            tlsca.leaf(self.ca_dir, names, self.key, pem)
        ctx = self.server_context()
        ctx.load_cert_chain(pem, self.key)
        return ctx

    def context(self, name):
        """The context for the SNI name: its own certificate if it is an answered name, else the
        default one."""
        name = lg_hosts.normal(name)
        if not name or not NAME_CHARS.match(name) or not answered(name):
            return self.default
        with self.lock:
            ctx = self.contexts.get(name)
            if ctx is None:
                ctx = self.contexts[name] = self._context(name, [name])
            return ctx

    def outer(self):
        """The listening context: the default certificate, the per-name one chosen by SNI."""
        ctx = self.server_context()
        ctx.load_cert_chain(os.path.join(self.dir, "_default.pem"), self.key)

        def pick(sock, name, _ctx):
            self.sni.name = name or ""
            if name:
                sock.context = self.context(name)
            return None

        ctx.sni_callback = pick
        return ctx


def _substitute(value, env):
    if isinstance(value, str):
        for k, v in env.items():
            value = value.replace("${" + k + "}", v)
        return value
    if isinstance(value, list):
        return [_substitute(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, env) for k, v in value.items()}
    return value


# Where the TV is, as LG's server tells it from the request's address: the VM's country (lg_mock.py
# --country, the launcher's from vm.toml firmware.country), (alpha-2, alpha-3, ric), or None
LOCATION = None


def location(value):
    """LOCATION of lg_mock.py's --country value ALPHA3,ALPHA2,RMM-GROUP (the RMM group gives the
    ric of a country lg_answers.COUNTRIES lacks); ValueError if it is not one."""
    parts = [p.strip().upper() for p in value.split(",")]
    if (
        len(parts) != 3
        or not re.fullmatch(r"[A-Z]{3}", parts[0])
        or not re.fullmatch(r"[A-Z]{2}", parts[1])
    ):
        raise ValueError(f"--country {value!r}: ALPHA3,ALPHA2,GROUP such as CAN,CA,US")
    three, two, group = parts
    ric = lg_answers.COUNTRIES.get(two, (None, lg_answers.RIC_BY_GROUP.get(group, "KIC")))[1]
    return two, three, ric


def country(headers, host):
    """(alpha-2, alpha-3, ric) of the requesting TV: X-Device-Country (alpha-3) looked up in
    lg_answers.COUNTRIES or naming the VM's own country, else where the TV is (LOCATION: the VM's
    country, as LG's server answers from the TV's address; a TV whose service country is still its
    group's Others row, us7.lgtvsdp.com, is told that country, not US), else the SDP host's country
    prefix (us.lgtvsdp.com), else the default."""
    three = (headers.get("X-Device-Country") or "").strip().upper()
    for two, (three_, ric) in lg_answers.COUNTRIES.items():
        if three_ == three:
            return two, three_, ric
    if LOCATION:
        return LOCATION
    label = re.sub(r"^(qt2|st2|dv2|qt3|qt4|qt5)-", "", (host or "").split(".")[0]).upper()
    label = re.sub(r"\d+$", "", label)
    if label in lg_answers.COUNTRIES:
        return (label,) + tuple(lg_answers.COUNTRIES[label])
    two = lg_answers.DEFAULT_COUNTRY
    return (two,) + tuple(lg_answers.COUNTRIES[two])


def _compile():
    routes = []
    for r in lg_answers.ROUTES:
        routes.append(
            (
                r,
                re.compile(r["path"]),
                set(r.get("methods", ("GET", "POST"))),
                set(r.get("classes", ("sdp",))),
            )
        )
    return routes


ROUTES = _compile()


def lookup(method, host, path):
    """The answer (an lg_answers.ROUTES entry, or lg_answers.DEFAULT) for a request, and whether
    a route matched."""
    c = lg_hosts.lookup(host)
    cls = c["class"] if c else ""
    path = path.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r"/{2,}", "/", path)
    for r, pattern, methods, classes in ROUTES:
        if cls in classes and method in methods and pattern.search(path):
            return r, True
    return lg_answers.DEFAULT, False


def render(answer, headers, host, now=None):
    """(status, [(header, value)], body bytes) of an answer for a request with these headers to
    host. ${now_ms}, ${now_s}, ${country}, ${country3} and ${ric} in its body and headers are
    substituted."""
    now = time.time() if now is None else now
    two, three, ric = country(headers, host)
    env = {
        "now_ms": "%d" % int(now * 1000),
        "now_s": "%d" % int(now),
        "country": two,
        "country3": three,
        "ric": ric,
    }
    status = answer.get("status", 200)
    if "text" in answer:
        body = _substitute(answer["text"], env).encode()
        ctype = answer.get("type", "text/html;charset=UTF-8")
    else:
        body = json.dumps(_substitute(answer.get("body", {}), env), separators=(",", ":")).encode()
        ctype = answer.get("type", "application/json;charset=UTF-8")
    out = [("Content-Type", ctype)]
    if 200 <= status < 300:
        out.append(("X-Server-Time", env["now_ms"]))
    for k, v in answer.get("headers", ()):
        out.append((k, _substitute(v, env)))
    return status, out, body


class Handler(http.server.BaseHTTPRequestHandler):
    """One connection's requests (HTTP/1.1, kept alive). server.note(line) logs; server.online
    is the plain-HTTP answer for hosts that are not answered here (the online check)."""

    protocol_version = "HTTP/1.1"
    server_version = "Apache"
    sys_version = ""
    timeout = IDLE_TIMEOUT

    def log_message(self, *args):
        pass

    def version_string(self):
        return self.server_version

    def _drain(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            total = 0
            while True:
                line = self.rfile.readline(1024)
                if not line:
                    return False
                try:
                    n = int(line.split(b";")[0].strip() or b"0", 16)
                except ValueError:
                    return False
                total += n
                if total > MAX_BODY:
                    return False
                if n == 0:
                    while self.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
                        pass
                    return True
                self.rfile.read(n + 2)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return False
        if n < 0 or n > MAX_BODY:
            return False
        while n > 0:
            chunk = self.rfile.read(min(n, 65536))
            if not chunk:
                return False
            n -= len(chunk)
        return True

    def _answer(self):
        if not self._drain():
            self.close_connection = True
            return
        host = lg_hosts.normal((self.headers.get("Host") or "").rsplit(":", 1)[0])
        if not self.server.tls and not answered(host):
            # port 80 of anything else (lgtvonline.lge.com, or by address): LG's online check
            self.wfile.write(self.server.online)
            self.close_connection = True
            return
        if self.server.tls and not host:
            host = lg_hosts.normal(getattr(self.connection, "lg_sni", ""))
        answer, known = lookup(self.command, host, self.path)
        status, headers, body = render(answer, self.headers, host)
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        keys = ",".join(sorted({k for k, _ in urllib.parse.parse_qsl(query, True)}))
        self.server.note(
            ("answered" if known else "unknown")
            + f" {self.command} {'https' if self.server.tls else 'http'}://{host}{path}"
            + (f"?{keys}" if keys else "")
            + f" {status}"
            + (f" ({answer.get('name')})" if known else "")
        )
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _answer


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, note, online, certs=None):
        self.note, self.online, self.certs = note, online, certs
        self.tls = certs.outer() if certs else None
        super().__init__(address, Handler)

    def finish_request(self, request, client_address):
        if self.tls:
            request.settimeout(HANDSHAKE_TIMEOUT)
            self.certs.sni.name = ""
            try:
                request = self.tls.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError) as e:
                name = self.certs.sni.name or "(no SNI)"
                self.note(f"tls-failed {name}: {getattr(e, 'reason', None) or e}")
                return
            request.lg_sni = self.certs.sni.name  # for requests without a Host header
            request.settimeout(None)
        try:
            Handler(request, client_address, self)
        except (ssl.SSLError, ConnectionError, socket.timeout, TimeoutError):
            pass
        finally:
            if self.tls:  # wrap_socket detached the socket socketserver closes after this
                request.close()
