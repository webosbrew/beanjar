#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The guest's name server, LG's online check and LG's SDP and web hosts, for lg_services = "mock"
(the default).

host/core/beanjar/netns.py runs this inside the VM's network namespace, on --listen (the TAP's own
address, netns.MOCK_DNS, which its DHCP server then hands out as the guest's name server), in
mode=mitm and mode=direct alike and whether mitmweb runs or not:

- DNS on UDP and TCP port 53. A name of a refused class in lg_hosts.py (LG's telemetry and cloud
  hosts) is answered NXDOMAIN; LG's online check (the "online" class, lgtvonline.lge.com) and the
  answered classes (SDP and LG's other web hosts) resolve to --listen itself; every other query is
  forwarded unchanged to --upstream (slirp4netns's resolver, which asks the host's) from --listen,
  and its answer returned unchanged. netns.py's policy rules refuse the guest's DNS to any other
  server, so this is the guest's only resolver.
- HTTP on port 80 of --listen: the answered hosts' answers by their Host header (lg_sdp.py), and
  ONLINE_RESPONSE, what LG's own server answers, to every other request.
- HTTPS on port 443 of --listen, with --ca (the library's interception CA) and --certs (where the
  certificates minted from it are kept): the answered hosts' answers (lg_sdp.py).

Each refused or answered name and request is logged to stderr, the same line once a minute at most
(netns-lgmock.log). "ready" on stdout once every socket listens; nothing is written there after it.

Python 3 stdlib and the system's openssl only; nothing is installed. It binds inside the
namespace, where the unshared user is root, so ports 53, 80 and 443 are allowed.
"""

import argparse
import os
import socket
import socketserver
import struct
import sys
import threading
import time

sys.dont_write_bytecode = True  # no __pycache__ in the source tree
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lg_hosts
import lg_sdp

# What http://lgtvonline.lge.com/ answers to GET / (fetched 2026-11-21), its status, headers and
# body byte for byte, less the ones that vary between LG's servers or by the hour (Date,
# Last-Modified, ETag) and with the connection closed after it (LG's keeps it alive). LG's connman
# (WT1H, W18R, W22H main.conf: Ipv4StatusUrl) takes a 200 as online (docs/research/
# network-ssh-audio.md) and has no X-ConnMan-Status check, but the header is sent as LG sends it.
ONLINE_BODY = (
    b'<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" '
    b'"http://www.w3.org/TR/html4/loose.dtd">\n'
    b"<html>\n<head>\n"
    b'    <meta http-equiv="Content-Type" content="text/html; charset=EUC-KR">\n'
    b'    <meta http-equiv="Cache-Control" content="no-cache; no-store; no-save"/>\n'
    b"    <title>LDAS</title>\n\n</head>\n<body>\nLDAS Service html Test\n</body>\n</html>\n\n"
)
ONLINE_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/html\r\n"
    + b"Content-Length: %d\r\n"
    % len(ONLINE_BODY)
    + b"Connection: close\r\n"
    b"Server: Apache\r\n"
    b"Accept-Ranges: bytes\r\n"
    b"X-ConnMan-Status: online\r\n"
    b"\r\n" + ONLINE_BODY
)
assert len(ONLINE_BODY) == 347  # LG's Content-Length

TTL = 60  # s, of the online check's answer
UPSTREAM_TIMEOUT = 4  # s per try; a forwarded query is tried twice over UDP
NXDOMAIN, NOERROR, SERVFAIL, FORMERR = 3, 0, 2, 1
QTYPE_A, QTYPE_ANY, QCLASS_IN = 1, 255, 1


def question(msg):
    """(name, qtype, qclass, end of the question) of a DNS query with one question, else None."""
    if len(msg) < 12 or msg[2] & 0x80:  # a response, not a query
        return None
    if struct.unpack(">H", msg[4:6])[0] != 1:
        return None
    labels, i = [], 12
    while True:
        if i >= len(msg):
            return None
        n = msg[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0 or i + 1 + n > len(msg):  # no compression in a question of its own
            return None
        labels.append(msg[i + 1 : i + 1 + n].decode("ascii", "replace"))
        i += 1 + n
    if i + 4 > len(msg):
        return None
    qtype, qclass = struct.unpack(">HH", msg[i : i + 4])
    return ".".join(labels), qtype, qclass, i + 4


def reply(msg, end, rcode, answer=None):
    """A response to the query msg (its question up to end): rcode, and answer's IPv4 address as
    the one A record, if given."""
    flags = struct.unpack(">H", msg[2:4])[0]
    flags = 0x8000 | (flags & 0x7900) | 0x0080 | rcode  # QR, the opcode and RD kept, RA
    head = msg[:2] + struct.pack(">HHHHH", flags, 1, 1 if answer else 0, 0, 0)
    out = head + msg[12:end]
    if answer:
        out += struct.pack(">HHHIH", 0xC00C, QTYPE_A, QCLASS_IN, TTL, 4)
        out += socket.inet_aton(answer)
    return out


class Mock:
    def __init__(self, listen, upstream, upstream_port=53):
        self.listen, self.upstream = listen, (upstream, upstream_port)
        self.logged, self.lock = {}, threading.Lock()

    def note(self, what, name, cls):
        self.line(f"{what} {name} ({cls})")

    def line(self, text):
        """text logged to stderr, the same text once a minute at most."""
        now = time.monotonic()
        with self.lock:
            if now - self.logged.get(text, -1e9) < 60:
                return
            self.logged[text] = now
        print(time.strftime("%Y-%m-%dT%H:%M:%S ") + text, file=sys.stderr, flush=True)

    def local(self, msg):
        """The answer of this resolver itself to msg, or None to forward it."""
        q = question(msg)
        if q is None:
            return None
        name, qtype, qclass, end = q
        cls = lg_hosts.lookup(name)
        if cls is None or cls["mock"] == "reach":
            return None
        if cls["mock"] == "refuse":
            self.note("refused", lg_hosts.normal(name), cls["class"])
            return reply(msg, end, NXDOMAIN)
        if cls["class"] == "sdp" and lg_sdp.first_use_pending({}, name):
            # the SDP host of a TV with no smart service country yet (us7.nextlgsdp.com): a
            # resolver failure, as on a set without a network, not a cached "no such name"
            self.note("withheld", lg_hosts.normal(name), cls["class"])
            return reply(msg, end, SERVFAIL)
        # the online check and the answered classes (lg_sdp.py): this address for A, no record of
        # other types (no IPv6 here)
        self.note("answered", lg_hosts.normal(name), cls["class"])
        a = qclass == QCLASS_IN and qtype in (QTYPE_A, QTYPE_ANY)
        return reply(msg, end, NOERROR, self.listen if a else None)

    def failure(self, msg):
        q = question(msg)
        if q is None:
            return reply(msg, 12, FORMERR)[:12] if len(msg) >= 12 else None
        return reply(msg, q[3], SERVFAIL)

    def forward_udp(self, msg):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((self.listen, 0))  # from this address: netns.py's rules route it into mitmweb
            s.settimeout(UPSTREAM_TIMEOUT)
            for _ in range(2):
                s.sendto(msg, self.upstream)
                try:
                    while True:
                        data, peer = s.recvfrom(65535)
                        if peer == self.upstream and data[:2] == msg[:2]:
                            return data
                except TimeoutError:
                    continue
        return None

    def forward_tcp(self, msg):
        try:
            with socket.create_connection(
                self.upstream, UPSTREAM_TIMEOUT, source_address=(self.listen, 0)
            ) as s:
                s.sendall(struct.pack(">H", len(msg)) + msg)
                f = s.makefile("rb")
                head = f.read(2)
                if len(head) < 2:
                    return None
                data = f.read(struct.unpack(">H", head)[0])
                return data or None
        except OSError:
            return None

    def answer(self, msg, tcp=False):
        """The response to the guest's query msg, or None for none."""
        out = self.local(msg)
        if out is not None:
            return out
        out = (self.forward_tcp(msg) if tcp else None) or self.forward_udp(msg)
        return out if out is not None else self.failure(msg)

    def serve_udp(self, sock):
        while True:
            try:
                msg, peer = sock.recvfrom(65535)
            except OSError:
                return  # closed
            threading.Thread(target=self._udp_one, args=(sock, msg, peer), daemon=True).start()

    def _udp_one(self, sock, msg, peer):
        out = self.answer(msg)
        if out:
            try:
                sock.sendto(out, peer)
            except OSError:
                pass


def tcp_dns_handler(mock):
    class Handler(socketserver.StreamRequestHandler):
        timeout = 30

        def handle(self):
            while True:
                head = self.rfile.read(2)
                if len(head) < 2:
                    return
                msg = self.rfile.read(struct.unpack(">H", head)[0])
                out = mock.answer(msg, tcp=True)
                if not out:
                    return
                self.wfile.write(struct.pack(">H", len(out)) + out)

    return Handler


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--listen", required=True, help="the address to serve on (the TAP's)")
    p.add_argument("--upstream", required=True, help="the resolver to forward to (slirp's)")
    p.add_argument("--dns-port", type=int, default=53)
    p.add_argument("--http-port", type=int, default=80)
    p.add_argument("--https-port", type=int, default=443)
    p.add_argument("--ca", help="the interception CA's directory (ca.pem, ca.key): HTTPS on")
    p.add_argument("--certs", help="where the minted certificates are kept (with --ca)")
    p.add_argument(
        "--country",
        help="where the TV is, ALPHA3,ALPHA2,RMM-GROUP (the VM's country): what the SDP answers "
        "tell a TV whose request names no country of their table (lg_sdp.LOCATION)",
    )
    a = p.parse_args(argv)
    if a.ca and not a.certs:
        p.error("--ca needs --certs")
    if a.country:
        try:
            lg_sdp.LOCATION = lg_sdp.location(a.country)
        except ValueError as e:
            p.error(str(e))
    mock = Mock(a.listen, a.upstream)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind((a.listen, a.dns_port))
    tcp = Server((a.listen, a.dns_port), tcp_dns_handler(mock))
    servers = [tcp, lg_sdp.Server((a.listen, a.http_port), mock.line, ONLINE_RESPONSE)]
    if a.ca:
        certs = lg_sdp.Certs(a.ca, a.certs)
        servers.append(lg_sdp.Server((a.listen, a.https_port), mock.line, None, certs))
    threading.Thread(target=lambda: mock.serve_udp(udp), daemon=True).start()
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    print(
        f"lg_mock: DNS on {a.listen}:{a.dns_port} (upstream {a.upstream}), online check and LG's "
        f"web hosts on {a.listen}:{a.http_port}"
        + (f", LG's HTTPS hosts on {a.listen}:{a.https_port}" if a.ca else ", no HTTPS (no --ca)"),
        file=sys.stderr,
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
