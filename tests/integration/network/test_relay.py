#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The mode=mitm port relay (host/helpers/network/relay.c, beanjar-relay; RELAY names the binary,
default build/network/beanjar-relay) against local servers: tcp->unix and unix->tcp, many connections at once, half-close, 20 MB each
way intact, the usage without arguments, a listener that cannot bind, a target that refuses, and SIGTERM (listeners and
connections closed, its unix sockets removed). `meson test -C build/host --suite manager`, or `pytest tests/integration/network/test_relay.py`."""

import hashlib
import os
import select
import signal
import socket
import subprocess
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
RELAY = os.environ.get("RELAY") or os.path.join(TOP, "build", "network", "beanjar-relay")
pytestmark = pytest.mark.skipif(not os.access(RELAY, os.X_OK), reason=f"no beanjar-relay: {RELAY}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def recv_all(s):
    out = []
    while True:
        b = s.recv(1 << 16)
        if not b:
            return b"".join(out)
        out.append(b)


class Server:
    """A server on a unix path or a tcp port; handler(conn) runs in a thread per connection."""

    def __init__(self, handler, path=None):
        if path:
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.bind(path)
        else:
            self.sock = socket.socket()
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("127.0.0.1", 0))
            self.port = self.sock.getsockname()[1]
        self.sock.listen(128)
        self.handler = handler
        self.threads = []
        self.t = threading.Thread(target=self._accept, daemon=True)
        self.t.start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            t = threading.Thread(target=self._run, args=(c,), daemon=True)
            self.threads.append(t)
            t.start()

    def _run(self, c):
        with c:
            try:
                self.handler(c)
            except OSError:
                pass

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def echo(c):
    while True:
        b = c.recv(1 << 16)
        if not b:
            return
        c.sendall(b)


def digest_then_reply(c):
    """Read to EOF (the client's half-close), then send the digest and close."""
    c.sendall(hashlib.sha256(recv_all(c)).hexdigest().encode())


class Harness:
    """The relays and servers one test starts, and their directory; close() ends them."""

    def __init__(self, directory):
        self.dir = str(directory)
        self.procs, self.servers = [], []

    def close(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()  # its own clean exit: a coverage build's counters are written
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
            p.wait()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()
        for s in self.servers:
            s.close()

    def server(self, handler, path=None):
        s = Server(handler, path)
        self.servers.append(s)
        return s

    def relay(self, *pairs, expect_ready=True):
        p = subprocess.Popen(
            [RELAY, *pairs],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.procs.append(p)
        r = select.select([p.stdout], [], [], 10)[0]
        line = p.stdout.readline() if r else b""
        if expect_ready:
            assert line == b"ready\n", p.stderr.read1() if p.poll() is not None else "no ready line"
        return p, line

    def tcp_to_unix(self, handler):
        path = os.path.join(self.dir, "s.sock")
        self.server(handler, path)
        port = free_port()
        self.relay(f"tcp:127.0.0.1:{port}=unix:{path}")
        return lambda: socket.create_connection(("127.0.0.1", port), 5)

    def unix_to_tcp(self, handler):
        srv = self.server(handler)
        path = os.path.join(self.dir, "l.sock")
        self.relay(f"unix:{path}=tcp:127.0.0.1:{srv.port}")

        def connect():
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(5)
            s.connect(path)
            return s

        return connect

    def roundtrip(self, connect, data=b"hello relay\n"):
        with connect() as s:
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            return recv_all(s)


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def test_tcp_to_unix(h):
    assert h.roundtrip(h.tcp_to_unix(echo)) == b"hello relay\n"


def test_unix_to_tcp(h):
    assert h.roundtrip(h.unix_to_tcp(echo)) == b"hello relay\n"


def test_half_close(h):
    # The server answers only after the client's EOF, over the half-closed connection.
    for connect in (h.tcp_to_unix(digest_then_reply), h.unix_to_tcp(digest_then_reply)):
        data = os.urandom(100000)
        assert h.roundtrip(connect, data) == hashlib.sha256(data).hexdigest().encode()


def test_half_close_server_side(h):
    # The server sends and shuts down its side; the client can still send and get an answer.
    def srv(c):
        c.sendall(b"greeting")
        c.shutdown(socket.SHUT_WR)
        recv_all(c)

    connect = h.tcp_to_unix(srv)
    with connect() as s:
        assert recv_all(s) == b"greeting"
        s.sendall(b"still open")  # the relay did not close the other direction


def test_upload_delivered_after_client_left(h):
    # The client sends, half-closes, gets its answer and closes while the server has not read
    # yet: what the relay still holds must reach the server all the same.
    data = os.urandom(8 * 1024 * 1024)
    got = []

    def srv(c):
        c.sendall(b"bye")
        c.shutdown(socket.SHUT_WR)
        time.sleep(0.5)
        body = recv_all(c)
        if body:  # not the relay's own probe, which connects and closes
            got.append(hashlib.sha256(body).digest())

    for connect in (h.tcp_to_unix(srv), h.unix_to_tcp(srv)):
        got.clear()
        with connect() as s:
            s.settimeout(30)
            w = threading.Thread(target=lambda: (s.sendall(data), s.shutdown(socket.SHUT_WR)))
            w.start()
            assert recv_all(s) == b"bye"
            w.join()
        for _ in range(200):
            if got:
                break
            time.sleep(0.05)
        assert got == [hashlib.sha256(data).digest()]


def test_many_connections(h):
    connect = h.tcp_to_unix(echo)
    results, errors = {}, []
    socks = [connect() for _ in range(64)]  # all open at once

    def run(i, s):
        try:
            data = f"conn {i} ".encode() * 1000
            with s:
                s.sendall(data)
                s.shutdown(socket.SHUT_WR)
                results[i] = recv_all(s) == data
        except OSError as e:
            errors.append(e)

    ts = [threading.Thread(target=run, args=(i, s)) for i, s in enumerate(socks)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert errors == []
    assert sorted(results) == list(range(64))
    assert all(results.values())


def test_large_both_ways(h):
    size = 20 * 1024 * 1024
    up = os.urandom(size)
    down = os.urandom(size)

    def srv(c):
        got = hashlib.sha256()

        def rd():
            while True:
                b = c.recv(1 << 16)
                if not b:
                    break
                got.update(b)

        t = threading.Thread(target=rd)
        t.start()
        c.sendall(down)
        c.shutdown(socket.SHUT_WR)
        t.join()
        srv.digest = got.hexdigest()

    for connect in (h.tcp_to_unix(srv), h.unix_to_tcp(srv)):
        srv.digest = None
        with connect() as s:
            s.settimeout(60)
            w = threading.Thread(target=lambda: (s.sendall(up), s.shutdown(socket.SHUT_WR)))
            w.start()
            back = recv_all(s)
            w.join()
        for _ in range(100):
            if srv.digest:
                break
            time.sleep(0.05)
        assert hashlib.sha256(back).digest() == hashlib.sha256(down).digest()
        assert srv.digest == hashlib.sha256(up).hexdigest()


def test_bind_failure(h):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        path = os.path.join(h.dir, "x.sock")
        p, line = h.relay(
            f"unix:{path}=tcp:127.0.0.1:1",
            f"tcp:127.0.0.1:{busy.getsockname()[1]}=unix:{path}",
            expect_ready=False,
        )
        assert line == b""
        assert p.wait(10) == 1
        assert b"cannot listen on tcp:127.0.0.1:" in p.stderr.read()
        assert not os.path.exists(path), "the unix listener it made is removed"
    finally:
        busy.close()


def test_usage(h):
    p, line = h.relay(expect_ready=False)
    assert line == b""
    assert p.wait(10) == 2
    assert p.stderr.read().startswith(b"usage: beanjar-relay LISTEN=CONNECT...")


def test_bad_pair(h):
    p, line = h.relay("tcp:127.0.0.1=unix:/x", expect_ready=False)
    assert p.wait(10) == 1
    assert b"bad address" in p.stderr.read()


def wait_listening(port, up, timeout=10):
    """Poll until a connect to the relay's port is accepted (up) or refused (not up)."""
    end = time.monotonic() + timeout
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), 2).close()
            if up:
                return
        except ConnectionRefusedError:
            if not up:
                return
        assert time.monotonic() < end, "port stayed " + ("refused" if up else "open")
        time.sleep(0.05)


def test_target_refused(h):
    """No service behind the relay: the client gets "connection refused", not an accepted close."""
    port = free_port()
    path = os.path.join(h.dir, "none.sock")
    p, _ = h.relay(f"tcp:127.0.0.1:{port}=unix:{path}")
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), 5)
    assert p.poll() is None


def test_refused_then_served(h):
    """The port opens once the service is there, and shuts again when it goes away."""
    port = free_port()
    path = os.path.join(h.dir, "late.sock")
    h.relay(f"tcp:127.0.0.1:{port}=unix:{path}")
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), 5)
    srv = h.server(echo, path)
    wait_listening(port, True)
    assert h.roundtrip(lambda: socket.create_connection(("127.0.0.1", port), 5)) == b"hello relay\n"
    srv.close()
    # the next connection finds the service gone; later ones are refused
    try:
        socket.create_connection(("127.0.0.1", port), 2).close()
    except OSError:
        pass
    wait_listening(port, False)


def test_unix_listener_follows_target(h):
    """A unix listener exists only while its tcp target accepts connections."""
    port = free_port()
    path = os.path.join(h.dir, "gate.sock")
    h.relay(f"unix:{path}=tcp:127.0.0.1:{port}")
    assert not os.path.exists(path)
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(5)
    try:
        end = time.monotonic() + 10
        while not os.path.exists(path):
            assert time.monotonic() < end, "socket never appeared"
            time.sleep(0.05)
    finally:
        s.close()


def test_stale_socket_replaced(h):
    srv = h.server(echo)
    path = os.path.join(h.dir, "stale.sock")
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(path)  # a socket file nobody listens on
    stale.close()
    h.relay(f"unix:{path}=tcp:127.0.0.1:{srv.port}")
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(path)
    with s:
        s.sendall(b"x")
        s.shutdown(socket.SHUT_WR)
        assert recv_all(s) == b"x"


def test_sigterm(h):
    srv = h.server(echo)
    upath = os.path.join(h.dir, "u.sock")
    tpath = os.path.join(h.dir, "t.sock")
    h.server(echo, tpath)
    port = free_port()
    p, _ = h.relay(f"unix:{upath}=tcp:127.0.0.1:{srv.port}", f"tcp:127.0.0.1:{port}=unix:{tpath}")
    open_conn = socket.create_connection(("127.0.0.1", port), 5)
    open_conn.sendall(b"ping")
    assert open_conn.recv(4) == b"ping"
    assert os.path.exists(upath)
    p.send_signal(signal.SIGTERM)
    assert p.wait(10) == 0
    assert not os.path.exists(upath), "its unix socket is removed"
    assert os.path.exists(tpath), "the target's socket is not its to remove"
    open_conn.settimeout(5)
    with open_conn:
        assert recv_all(open_conn) == b"", "open connections are closed"
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), 5)
