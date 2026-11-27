# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Rootless IPv4 discovery bridge between the LAN and fixed guest namespaces.

Only packets received from the guest admit endpoints. The private Unix socket
carries discovery datagrams and fixed-destination service connections.
"""

import base64
import hashlib
import http.client
import http.server
import json
import os
import re
import select
import socket
import struct
import sys
import threading
import time
from urllib.parse import urlsplit

import dns.exception
import dns.rdatatype

from . import discovery_dns

GUEST = "10.0.2.15"
TAP = "10.0.2.254"
GROUPS = {1900: "239.255.255.250", 5353: "224.0.0.251"}
MAX_ENDPOINTS = 64
MAX_BODY = 2 * 1024 * 1024
MAX_SENT = 512
IP_PKTINFO = getattr(socket, "IP_PKTINFO", 8)
WORKERS = threading.BoundedSemaphore(64)


def worker(fn, *args):
    if not WORKERS.acquire(False):
        return False

    def run():
        try:
            fn(*args)
        except (OSError, ValueError, http.client.HTTPException, dns.exception.DNSException):
            pass
        finally:
            WORKERS.release()

    threading.Thread(target=run, daemon=True).start()
    return True


def exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise OSError("discovery channel closed")
        data.extend(part)
    return bytes(data)


def send(sock, obj):
    data = json.dumps(obj, separators=(",", ":")).encode()
    if len(data) > 100000:
        raise ValueError("discovery frame too large")
    sock.sendall(struct.pack("!I", len(data)) + data)


def receive(sock):
    size = struct.unpack("!I", exact(sock, 4))[0]
    if size > 100000:
        raise ValueError("discovery frame too large")
    return json.loads(exact(sock, size))


def encoded(data):
    return base64.b64encode(data).decode("ascii")


def decoded(data):
    wire = base64.b64decode(data, validate=True)
    if len(wire) > 65507:
        raise ValueError("datagram too large")
    return wire


def multicast(port, address):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    sock.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        socket.inet_aton(GROUPS[port]) + socket.inet_aton(address),
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255 if port == 5353 else 2)
    return sock


def datagram(sock):
    """Keep multicast and private replies distinct on wildcard group sockets."""
    data, ancillary, _flags, peer = sock.recvmsg(65535, 128)
    destination = None
    for level, kind, value in ancillary:
        if level == socket.IPPROTO_IP and kind == IP_PKTINFO and len(value) >= 12:
            destination = socket.inet_ntoa(value[8:12])
    return data, peer, destination in GROUPS.values()


def isolated_query(port, data, source_port):
    if port != 5353 or source_port != 5353:
        return True
    message, flags = discovery_dns._decode(data)
    return any(flags[: len(message.question)])


def pump(left, right):
    with left, right:
        left.settimeout(None)
        right.settimeout(None)
        end = time.monotonic() + 300
        while time.monotonic() < end:
            ready, _, _ = select.select([left, right], [], [], 30)
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                (right if source is left else left).sendall(data)
                end = time.monotonic() + 300


class Inner:
    def __init__(self, path):
        self.path = path
        self.groups = {port: multicast(port, TAP) for port in GROUPS}
        self.events = None
        self.lock = threading.Lock()

    def event_loop(self, port, sock):
        while True:
            data, peer, multicast_destination = datagram(sock)
            if peer[0] != GUEST:
                continue
            query = (
                data.startswith(b"M-SEARCH ")
                if port == 1900
                else len(data) >= 12 and not data[2] & 0x80
            )
            advertisement = (
                data.startswith(b"NOTIFY ")
                if port == 1900
                else len(data) >= 12 and bool(data[2] & 0x80)
            )
            if not query and not advertisement:
                continue
            with self.lock:
                if self.events:
                    try:
                        send(
                            self.events,
                            {
                                "port": port,
                                "data": encoded(data),
                                "query": query,
                                "source_port": peer[1],
                                "multicast": multicast_destination,
                            },
                        )
                    except OSError:
                        self.events = None

    def connection(self, conn):
        with conn:
            conn.settimeout(10)
            request = receive(conn)
            kind = request["kind"]
            if kind == "events":
                conn.settimeout(None)
                with self.lock:
                    self.events = conn
                while conn.recv(1):
                    pass
                with self.lock:
                    if self.events is conn:
                        self.events = None
            elif kind == "tcp":
                port = int(request["port"])
                if not 0 < port < 65536:
                    return
                backend = socket.create_connection((GUEST, port), 5)
                send(conn, {"ok": True})
                pump(conn, backend)
            elif kind in ("query", "udp"):
                port = int(request["port"])
                data = decoded(request["data"])
                if kind == "query" and port not in GROUPS:
                    return
                if not 0 < port < 65536:
                    return
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    udp.bind((TAP, 0))
                    udp.settimeout(0.5)
                    udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(TAP))
                    udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
                    udp.setsockopt(
                        socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255 if port == 5353 else 2
                    )
                    udp.sendto(data, (GROUPS[port] if kind == "query" else GUEST, port))
                    if kind == "udp":
                        udp.settimeout(None)
                        conn.settimeout(10)
                        while True:
                            ready, _, _ = select.select([conn, udp], [], [], 30)
                            if not ready:
                                return
                            for source in ready:
                                if source is conn:
                                    udp.sendto(decoded(receive(conn)["data"]), (GUEST, port))
                                else:
                                    reply, peer = udp.recvfrom(65535)
                                    if peer == (GUEST, port):
                                        send(conn, {"data": encoded(reply)})
                    end = time.monotonic() + 5
                    count = 0
                    while time.monotonic() < end and count < 128:
                        try:
                            reply, peer = udp.recvfrom(65535)
                        except socket.timeout:
                            continue
                        if peer[0] == GUEST:
                            send(conn, {"data": encoded(reply)})
                            count += 1
            elif kind in ("multicast", "lan-datagram", "guest-reply"):
                port = int(request["port"])
                if port not in GROUPS:
                    return
                destination_port = int(request.get("destination_port", port))
                if not 0 < destination_port < 65536:
                    return
                unicast = kind == "guest-reply" or not request.get("multicast", True)
                destination = (GUEST, destination_port) if unicast else (GROUPS[port], port)
                self.groups[port].sendto(decoded(request["data"]), destination)

    def run(self):
        for port, sock in self.groups.items():
            threading.Thread(target=self.event_loop, args=(port, sock), daemon=True).start()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(self.path)
            os.chmod(self.path, 0o600)
            server.listen(32)
            print("ready", flush=True)
            while True:
                conn, _ = server.accept()
                if not worker(self.connection, conn):
                    conn.close()


class Backend(http.client.HTTPConnection):
    def __init__(self, owner, port):
        super().__init__(GUEST, port, timeout=10)
        self.owner = owner

    def connect(self):
        self.sock = self.owner.channel("tcp", port=self.port)
        if not receive(self.sock).get("ok"):
            raise OSError("guest connection failed")


class Outer:
    def __init__(self, path, address, static=()):
        self.path, self.address = path, address
        self.static = set(static)
        self.endpoints = {}
        self.lock = threading.RLock()
        self.hosts = {}
        self.descriptions = {}
        self.description_resources = {}
        self.ssdp = {}
        self.mdns = {}
        self.ad_lock = threading.Lock()
        self.udp_sessions = {}
        self.sent = {}
        self.groups = {port: multicast(port, address) for port in GROUPS}

    def channel(self, kind, **args):
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(10)
        try:
            sock.connect(self.path)
            send(sock, dict(kind=kind, **args))
            return sock
        except Exception:
            sock.close()
            raise

    def endpoint(self, transport, port, http=False, ttl=1800):
        if transport == "tcp" and port in self.static:
            return True
        key = (transport, port)
        with self.lock:
            existing = self.endpoints.get(key)
            if existing is not None:
                now = time.monotonic()
                if existing[1] > now:
                    existing[1] = max(existing[1], now + max(ttl, 1))
                    existing[2] |= http
                    return True
                # Retire under the same lock used by listener cleanup. A refresh
                # must never resurrect a listener whose expiry already passed.
                existing[0].close()
                del self.endpoints[key]
            if len(self.endpoints) >= MAX_ENDPOINTS:
                return False
            sock = socket.socket(
                socket.AF_INET, socket.SOCK_STREAM if transport == "tcp" else socket.SOCK_DGRAM
            )
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.address, port))
                if transport == "tcp":
                    sock.listen(16)
                sock.settimeout(1)
            except OSError:
                sock.close()
                return False
            entry = [sock, time.monotonic() + max(ttl, 1), http]
            self.endpoints[key] = entry
            threading.Thread(target=self.serve_endpoint, args=(key, entry), daemon=True).start()
            return True

    def serve_endpoint(self, key, entry):
        transport, port = key
        sock = entry[0]
        try:
            while True:
                with self.lock:
                    if self.endpoints.get(key) is not entry:
                        return
                    if time.monotonic() >= entry[1]:
                        sock.close()
                        del self.endpoints[key]
                        return
                try:
                    if transport == "tcp":
                        client, _ = sock.accept()
                        if not worker(self.tcp_client, client, port, entry):
                            client.close()
                    else:
                        data, peer = sock.recvfrom(65535)
                        worker(self.udp_client, sock, data, peer, port)
                except socket.timeout:
                    continue
                except OSError:
                    return
        finally:
            with self.lock:
                sock.close()
                if self.endpoints.get(key) is entry:
                    del self.endpoints[key]

    def tcp_client(self, client, port, entry):
        if entry[2]:
            owner = self

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *_args):
                    pass

                def handle_one_request(self):
                    self.connection.settimeout(10)
                    super().handle_one_request()

                def proxy(self):
                    backend = Backend(owner, port)
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if length < 0 or length > MAX_BODY or self.headers.get("Transfer-Encoding"):
                            self.send_error(413)
                            return
                        headers = {
                            k: v.replace(owner.address, GUEST)
                            for k, v in self.headers.items()
                            if k.lower() not in HOP
                        }
                        headers["Accept-Encoding"] = "identity"
                        body = self.rfile.read(length) if length else None
                        backend.request(
                            self.command, self.path.replace(owner.address, GUEST), body, headers
                        )
                        response = backend.getresponse()
                        headers = response.getheaders()
                        content_type = response.getheader("Content-Type", "").lower()
                        rewrite = any(t in content_type for t in ("xml", "text/", "json"))
                        with owner.lock:
                            rewrite |= (
                                owner.description_resources.get((port, self.path), 0)
                                > time.monotonic()
                            )
                        if (
                            rewrite
                            and response.getheader("Content-Encoding", "identity").lower()
                            != "identity"
                        ):
                            self.send_error(502)
                            return
                        payload = (
                            response.read(MAX_BODY + 1)
                            if rewrite and self.command != "HEAD"
                            else None
                        )
                        if payload is not None and len(payload) > MAX_BODY:
                            self.send_error(502)
                            return
                        if payload is not None:
                            owner.admit_urls(payload)
                            payload = replace_address(payload, owner.address)
                        for name, value in headers:
                            if name.lower() in ("location", "application-url"):
                                owner.admit_urls(value.encode())
                        self.send_response(response.status, response.reason)
                        for name, value in headers:
                            if name.lower() in HOP or (
                                rewrite
                                and name.lower() in ("content-length", "etag", "content-md5")
                            ):
                                continue
                            self.send_header(name, value.replace(GUEST, owner.address))
                        if payload is not None:
                            self.send_header("Content-Length", str(len(payload)))
                        elif (
                            self.command != "HEAD" and response.getheader("Content-Length") is None
                        ):
                            self.send_header("Connection", "close")
                            self.close_connection = True
                        self.end_headers()
                        if self.command != "HEAD":
                            if payload is not None:
                                self.wfile.write(payload)
                            else:
                                while chunk := response.read(65536):
                                    self.wfile.write(chunk)
                    except (
                        OSError,
                        ValueError,
                        http.client.HTTPException,
                        dns.exception.DNSException,
                    ):
                        self.close_connection = True
                    finally:
                        backend.close()

                do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_SUBSCRIBE = (
                    do_UNSUBSCRIBE
                ) = do_NOTIFY = proxy

            try:
                Handler(client, (self.address, 0), None)
            finally:
                client.close()
        else:
            with client:
                with self.channel("tcp", port=port) as backend:
                    if receive(backend).get("ok"):
                        pump(client, backend)

    def udp_client(self, sock, data, peer, port):
        key = (peer, port)
        with self.lock:
            session = self.udp_sessions.get(key)
            if session:
                with session[1]:
                    send(session[0], {"data": encoded(data)})
                return
            if len(self.udp_sessions) >= 32:
                return
            channel = self.channel("udp", port=port, data=encoded(data))
            channel.settimeout(30)
            session = (channel, threading.Lock())
            self.udp_sessions[key] = session
        try:
            with channel:
                while True:
                    reply = receive(channel)
                    sock.sendto(decoded(reply["data"]), peer)
        finally:
            with self.lock:
                if self.udp_sessions.get(key) is session:
                    self.udp_sessions.pop(key, None)

    def admit_urls(self, data, ttl=1800):
        ports = set()
        for match in re.finditer(rb'https?://10\.0\.2\.15(?::([0-9]{1,5}))?(?=[/\s<>"\']|$)', data):
            port = int(match[1] or 80)
            if not 0 < port < 65536 or not self.endpoint("tcp", port, http=True, ttl=ttl):
                raise ValueError("guest endpoint unavailable")
            ports.add(("tcp", port))
        return ports

    def register_description(self, port, path, ttl):
        """Only verified guest LOCATION resources qualify without a MIME type."""
        now = time.monotonic()
        key = (port, path)
        with self.lock:
            self.description_resources = {
                resource: expiry
                for resource, expiry in self.description_resources.items()
                if expiry > now
            }
            if key not in self.description_resources and len(self.description_resources) >= 128:
                raise ValueError("too many guest descriptions")
            self.description_resources[key] = max(self.description_resources.get(key, 0), now + ttl)

    def retire_unreferenced(self, candidates, now):
        """A goodbye withdraws only endpoints with no live discovery reference."""
        referenced = set()
        for expiry, ports in self.ssdp.values():
            if expiry > now:
                referenced.update(ports)
        for endpoint, expiry in self.mdns.values():
            if expiry > now:
                referenced.add(endpoint)
        with self.lock:
            for key in candidates - referenced:
                if key in self.endpoints:
                    self.endpoints[key][1] = now

    def advertisement(self, port, wire):
        if port == 1900:
            now = time.monotonic()
            self.ssdp = {key: value for key, value in self.ssdp.items() if value[0] > now}
            identity = re.search(rb"(?im)^usn:[ \t]*([^\r\n]+)", wire)
            identity = identity[1] if identity else None
            if re.search(rb"(?im)^nts:[ \t]*ssdp:byebye[ \t]*\r?$", wire):
                registration = self.ssdp.pop(identity, None)
                if registration:
                    self.retire_unreferenced(registration[1], now)
                return replace_address(wire, self.address)
            age = re.search(rb"(?i)max-age\s*=\s*([0-9]+)", wire)
            ttl = int(age[1]) if age else 1800
            ports = self.admit_urls(wire, ttl)
            location = re.search(rb"(?im)^location:[ \t]*(http://[^\r\n ]+)", wire)
            if location:
                url = location[1].decode("ascii")
                parsed = urlsplit(url)
                resource_path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
                cached = self.descriptions.get(url, (0, set()))
                if parsed.hostname == GUEST and cached[0] < time.monotonic():
                    backend = Backend(self, parsed.port or 80)
                    try:
                        backend.request(
                            "GET",
                            resource_path,
                        )
                        response = backend.getresponse()
                        body = response.read(MAX_BODY + 1)
                        if len(body) > MAX_BODY:
                            raise ValueError("guest description too large")
                        descriptor_ports = self.admit_urls(body, ttl)
                        ports |= descriptor_ports
                        for name, value in response.getheaders():
                            if name.lower() in ("location", "application-url"):
                                found = self.admit_urls(value.encode(), ttl)
                                ports |= found
                                descriptor_ports |= found
                        if len(self.descriptions) >= 128:
                            self.descriptions.clear()
                        self.descriptions[url] = (time.monotonic() + 30, descriptor_ports)
                    finally:
                        backend.close()
                elif parsed.hostname == GUEST:
                    ports |= cached[1]
                    for transport, endpoint in cached[1]:
                        if not self.endpoint(transport, endpoint, http=True, ttl=ttl):
                            raise ValueError("guest endpoint unavailable")
                if parsed.hostname == GUEST:
                    self.register_description(parsed.port or 80, resource_path, ttl)
            if identity and (len(self.ssdp) < 256 or identity in self.ssdp):
                old = self.ssdp.get(identity)
                if old:
                    ports |= old[1]
                self.ssdp[identity] = (now + ttl, ports)
            return replace_address(wire, self.address)
        now = time.monotonic()
        original, _ = discovery_dns._decode(wire)
        self.mdns = {name: value for name, value in self.mdns.items() if value[1] > now}
        for rr in original.answer + original.authority + original.additional:
            if rr.ttl:
                continue
            names = [rr.name.canonicalize().to_text()]
            if rr.rdtype == dns.rdatatype.PTR:
                names += [data.target.canonicalize().to_text() for data in rr]
            for name in names:
                registration = self.mdns.pop(name, None)
                if registration:
                    self.retire_unreferenced({registration[0]}, now)
        self.hosts = {name: end for name, end in self.hosts.items() if end > now}
        for name, ttl in discovery_dns.guest_hosts(wire, GUEST).items():
            if ttl:
                if len(self.hosts) < 256 or name in self.hosts:
                    self.hosts[name] = now + ttl
            else:
                self.hosts.pop(name, None)
        endpoint_ttls = discovery_dns.endpoint_ttls(wire, GUEST, set(self.hosts))
        wire, endpoints = discovery_dns.rewrite(wire, GUEST, self.address, set(self.hosts))
        message, _ = discovery_dns._decode(wire)
        for transport, endpoint in endpoints:
            if not self.endpoint(transport, endpoint, ttl=endpoint_ttls[(transport, endpoint)]):
                raise ValueError("guest endpoint unavailable")
        for rr in message.answer + message.authority + message.additional:
            if rr.rdtype == dns.rdatatype.SRV and rr.ttl:
                labels = [label.lower() for label in rr.name.labels]
                transport = "tcp" if b"_tcp" in labels else "udp" if b"_udp" in labels else None
                for data in rr:
                    key = (transport, data.port)
                    name = rr.name.canonicalize().to_text()
                    if key in endpoints and (len(self.mdns) < 256 or name in self.mdns):
                        self.mdns[name] = (key, now + rr.ttl)
        return wire

    def publish(self, port, data, peer=None):
        with self.ad_lock:
            wire = self.advertisement(port, data)
        digest = (port, hashlib.sha256(wire).digest())
        self.sent_packet(digest, remember=True)
        self.groups[port].sendto(wire, peer or (GROUPS[port], port))

    def query(self, port, data, peer):
        with self.channel("query", port=port, data=encoded(data)) as channel:
            while True:
                reply = receive(channel)
                self.publish(port, decoded(reply["data"]), peer)

    def sent_packet(self, digest, remember=False):
        with self.lock:
            now = time.monotonic()
            self.sent = {key: end for key, end in self.sent.items() if end > now}
            present = digest in self.sent
            if remember:
                if len(self.sent) >= MAX_SENT:
                    self.sent.pop(next(iter(self.sent)))
                self.sent[digest] = now + 2
            return present

    def into_guest(self, port, data, multicast_destination=True, destination_port=None):
        args = {"port": port, "data": encoded(data), "multicast": multicast_destination}
        if destination_port is not None:
            args["destination_port"] = destination_port
        with self.channel("lan-datagram", **args):
            pass

    def guest_query(self, port, data, source_port):
        if not isolated_query(port, data, source_port):
            self.groups[port].sendto(data, (GROUPS[port], port))
            return
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind((self.address, 0))
            udp.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.address)
            )
            udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
            udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255 if port == 5353 else 2)
            udp.settimeout(0.5)
            udp.sendto(data, (GROUPS[port], port))
            end = time.monotonic() + 5
            count = 0
            while time.monotonic() < end and count < 128:
                try:
                    reply, peer = udp.recvfrom(65535)
                except socket.timeout:
                    continue
                if peer[0] == self.address:
                    continue
                self.into_guest(port, reply, False, source_port)
                count += 1

    def handle_event(self, message):
        port, data = int(message["port"]), decoded(message["data"])
        if port not in GROUPS:
            return
        if message.get("query"):
            worker(self.guest_query, port, data, int(message["source_port"]))
        else:
            self.publish(port, data)

    def events(self):
        while True:
            try:
                with self.channel("events") as channel:
                    channel.settimeout(None)
                    while True:
                        message = receive(channel)
                        try:
                            self.handle_event(message)
                        except Exception:
                            pass
            except OSError:
                time.sleep(1)

    def lan_packet(self, port, data, peer, multicast_destination):
        digest = (port, hashlib.sha256(data).digest())
        if peer[0] == self.address or self.sent_packet(digest):
            return
        if port == 1900:
            if data.startswith(b"M-SEARCH "):
                worker(self.query, port, data, peer)
            elif data.startswith(b"NOTIFY ") or data.startswith(b"HTTP/1.1 200 "):
                self.into_guest(port, data, multicast_destination)
        elif len(data) >= 12:
            if data[2] & 0x80:
                self.into_guest(port, data, multicast_destination)
            elif isolated_query(port, data, peer[1]):
                worker(self.query, port, data, peer)
            else:
                with self.channel("multicast", port=port, data=encoded(data)):
                    pass

    def run(self):
        threading.Thread(target=self.events, daemon=True).start()
        print("ready", flush=True)
        while True:
            for sock in select.select(list(self.groups.values()), [], [], 1)[0]:
                data, peer, multicast_destination = datagram(sock)
                try:
                    self.lan_packet(sock.getsockname()[1], data, peer, multicast_destination)
                except (OSError, ValueError, dns.exception.DNSException):
                    pass


HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def replace_address(data, address):
    return re.sub(rb"(?<![0-9.])10\.0\.2\.15(?![0-9.])", address.encode(), data)


def main():
    os.umask(0o077)
    mode = sys.argv[1]
    path = os.path.join(os.environ["NETNS_RUNDIR"], "discovery.sock")
    if mode == "inner":
        Inner(path).run()
    elif mode == "outer":
        static = [int(pair.split(":")[0]) for pair in os.environ.get("NETNS_FWD", "").split()]
        Outer(path, os.environ["NETNS_LAN_ADDRESS"], static).run()
    else:
        raise ValueError("expected inner or outer")


if __name__ == "__main__":
    main()
