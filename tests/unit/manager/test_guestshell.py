#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""`beanjar shell` (host/core/beanjar/guestshell.py, commands/shell.py): the frames of
include/webos-shell.h, the relay's sessions between clients and the guest, a client's session end
to end against a fake guest agent, and the command line. No VM.
`pytest tests/unit/manager/test_guestshell.py`."""

import os
import socket
import struct
import threading

import pytest

from beanjar import guestshell as gs
from beanjar.commands import parser
from beanjar.commands.shell import command_words


def test_frames_round_trip_in_pieces():
    data = gs.hello(7) + gs.frame(gs.STDOUT, 3, b"abc") + gs.frame(gs.EXIT, 3, gs.word(3))
    r = gs.Reader()
    got = []
    for i in range(len(data)):
        got += r.feed(data[i : i + 1])
    assert got == [
        (gs.HELLO, 0, struct.pack("<II", gs.VERSION, 7)),
        (gs.STDOUT, 3, b"abc"),
        (gs.EXIT, 3, gs.word(3)),
    ]


def test_header_is_the_c_one():
    assert gs.HEADER.size == 16 and gs.OPEN_HEADER.size == 8
    assert gs.frame(gs.ACK, 1, gs.word(5))[:4] == b"WSHL"


def test_unsynced_reader_waits_for_hello_and_skips_garbage():
    r = gs.Reader(synced=False)
    tail = gs.frame(gs.STDOUT, 1, b"old")[5:]  # the tail of a frame from before
    assert r.feed(tail + gs.frame(gs.STDOUT, 1, b"stale")) == []
    assert r.feed(b"\xff\x00junk" + gs.hello(1) + gs.frame(gs.STDOUT, 2, b"x")) == [
        (gs.HELLO, 0, struct.pack("<II", gs.VERSION, 1)),
        (gs.STDOUT, 2, b"x"),
    ]


def test_reader_resyncs_after_an_oversized_length():
    bad = struct.pack("<IHHII", gs.MAGIC, gs.STDOUT, 0, 1, gs.MAX + 1)
    assert gs.Reader().feed(bad + gs.frame(gs.STDERR, 1, b"ok")) == [(gs.STDERR, 1, b"ok")]


def test_open_payload():
    p = gs.open_payload(["sh", "-c", "echo hi"], True, 50, 132, "xterm")
    assert gs.parse_open(p) == (True, 50, 132, "xterm", ["sh", "-c", "echo hi"])
    assert gs.parse_open(gs.open_payload([], False)) == (False, 24, 80, "", [])
    # without a terminal there is no terminal type
    assert gs.parse_open(gs.open_payload(["id"], False, term="xterm"))[3] == ""
    with pytest.raises(ValueError):
        gs.open_payload(["a\0b"], False)
    with pytest.raises(gs.ProtocolError):
        gs.parse_open(gs.open_payload(["id"], False)[:-1])


def test_status():
    assert gs.status_of(gs.EXIT, gs.word(3)) == 3
    assert gs.status_of(gs.EXIT, gs.word(130)) == 130
    assert gs.status_of(gs.CLOSE, b"gone") == gs.LOST


@pytest.mark.parametrize(
    "command,force,never,isatty,want",
    [
        ([], False, False, True, True),
        ([], False, False, False, False),
        (["id"], False, False, True, False),
        (["top"], True, False, False, True),
        ([], False, True, True, False),
    ],
)
def test_want_tty(command, force, never, isatty, want):
    assert gs.want_tty(command, force, never, isatty) is want


def test_want_tty_both():
    with pytest.raises(ValueError):
        gs.want_tty([], True, True, True)


@pytest.mark.parametrize(
    "argv,tty,no_tty,name,words",
    [
        (["shell", "tv"], False, False, "tv", []),
        (["shell", "tv", "--", "ls", "-l"], False, False, "tv", ["ls", "-l"]),
        (["shell", "-t", "tv", "--", "top"], True, False, "tv", ["top"]),
        (["shell", "-T", "pi/tv", "id", "-u"], False, True, "pi/tv", ["id", "-u"]),
        (["shell", "tv", "sh", "-c", "a -- b"], False, False, "tv", ["sh", "-c", "a -- b"]),
    ],
)
def test_command_line(argv, tty, no_tty, name, words):
    a = parser.build().parse_args(argv)
    assert (a.tty, a.no_tty, a.name, a.remote) == (tty, no_tty, name, "shell")
    assert command_words(a) == words


class FakeGuest(threading.Thread):
    """The agent's side of QEMU's socket: answers HELLO; a session echoes its stdin to stdout,
    and at its end writes "err" to stderr and exits 3 (with no words) or exits with its first
    word."""

    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock, self.opened, self.closed = sock, [], []
        self.lock = threading.Lock()

    def send(self, data):
        with self.lock:
            self.sock.sendall(data)

    def run(self):
        r = gs.Reader(synced=False)
        while data := self.sock.recv(65536):
            for kind, sid, payload in r.feed(data):
                if kind == gs.HELLO:
                    self.send(gs.frame(gs.HELLO, 0, payload))
                elif kind == gs.OPEN:
                    argv = gs.parse_open(payload)[4]
                    self.opened.append((sid, argv))
                    if argv and argv[0].isdigit():
                        self.send(gs.frame(gs.EXIT, sid, gs.word(int(argv[0]))))
                elif kind == gs.STDIN and payload:
                    self.send(gs.frame(gs.ACK, sid, gs.word(len(payload))))
                    for i in range(0, len(payload), gs.MAX):
                        self.send(gs.frame(gs.STDOUT, sid, payload[i : i + gs.MAX]))
                elif kind == gs.STDIN:
                    self.send(gs.frame(gs.STDERR, sid, b"err") + gs.frame(gs.EXIT, sid, gs.word(3)))
                elif kind == gs.CLOSE:
                    self.closed.append(sid)


@pytest.fixture
def relay(tmp_path):
    path = str(tmp_path / "shell.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(4)
    host_end, guest_end = socket.socketpair()
    guest = FakeGuest(guest_end)
    guest.start()
    stop = threading.Event()
    log = []
    relay = gs.Relay(listener, "unused", lambda: not stop.is_set(), log.append)
    relay.connect_guest(host_end)
    t = threading.Thread(target=relay.serve, daemon=True)
    t.start()
    yield path, guest, relay
    stop.set()
    t.join(5)
    guest_end.close()


def client(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    s.settimeout(5)
    return s


def frames_until(sock, reader, n):
    got = []
    while len(got) < n:
        got += reader.feed(sock.recv(65536))
    return got


def wait_for(cond):
    import time

    for _ in range(500):
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def test_relay_renumbers_and_gives_up_a_leaving_clients_sessions(relay):
    path, guest, r = relay
    wait_for(lambda: r.ready)
    a, b = client(path), client(path)
    ra, rb = gs.Reader(False), gs.Reader(False)
    assert frames_until(a, ra, 1)[0][0] == gs.HELLO
    assert frames_until(b, rb, 1)[0][0] == gs.HELLO
    # both clients call their session 1; the guest sees two
    a.sendall(gs.frame(gs.OPEN, 1, gs.open_payload([], False)))
    b.sendall(gs.frame(gs.OPEN, 1, gs.open_payload([], False)))
    wait_for(lambda: len(guest.opened) == 2)
    assert guest.opened[0][0] != guest.opened[1][0]
    a.sendall(gs.frame(gs.STDIN, 1, b"to a"))
    b.sendall(gs.frame(gs.STDIN, 1, b"to b"))
    assert (gs.STDOUT, 1, b"to a") in frames_until(a, ra, 2)
    assert (gs.STDOUT, 1, b"to b") in frames_until(b, rb, 2)
    a.close()
    wait_for(lambda: guest.closed)
    b.sendall(gs.frame(gs.STDIN, 1))
    assert frames_until(b, rb, 2) == [(gs.STDERR, 1, b"err"), (gs.EXIT, 1, gs.word(3))]
    b.close()


def test_relay_loses_sessions_when_the_agent_starts_again(relay):
    path, guest, r = relay
    wait_for(lambda: r.ready)
    c = client(path)
    rc = gs.Reader(False)
    frames_until(c, rc, 1)
    c.sendall(gs.frame(gs.OPEN, 5, gs.open_payload([], False)))
    wait_for(lambda: guest.opened)
    guest.send(gs.hello(0))  # the agent started again
    kind, sid, why = frames_until(c, rc, 1)[0]
    assert (kind, sid) == (gs.CLOSE, 5) and b"started again" in why
    wait_for(lambda: r.ready)  # it answered the relay's new HELLO
    c.sendall(gs.frame(gs.OPEN, 6, gs.open_payload(["0"], False)))
    assert frames_until(c, rc, 1) == [(gs.EXIT, 6, gs.word(0))]
    c.close()


def test_relay_refuses_sessions_before_the_agent_answers(tmp_path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = str(tmp_path / "s.sock")
    listener.bind(path)
    listener.listen(1)
    r = gs.Relay(listener, str(tmp_path / "none.sock"), log=lambda _: None)
    c = client(path)
    r.step(0.5)
    c.sendall(gs.frame(gs.OPEN, 1, gs.open_payload([], False)))
    rc = gs.Reader(False)
    r.step(0.5)
    r.step(0.5)
    got = frames_until(c, rc, 2)
    assert got[1][:2] == (gs.CLOSE, 1) and b"not answered" in got[1][2]
    c.close()


def test_session_end_to_end(relay):
    """stdin of several windows through, stdout and stderr apart, the exit status."""
    path, _, r = relay
    wait_for(lambda: r.ready)
    data = os.urandom(3 * gs.WINDOW + 12345)
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()

    def feed():
        with os.fdopen(in_w, "wb") as f:
            f.write(data)

    out = bytearray()

    def drain():
        with os.fdopen(out_r, "rb") as f:
            while chunk := f.read(65536):
                out.extend(chunk)

    threads = [threading.Thread(target=feed), threading.Thread(target=drain)]
    for t in threads:
        t.start()
    with client(path) as sock:
        sock.settimeout(None)
        s = gs.Session(sock, ["cat"], False, stdin=in_r, stdout=out_w, stderr=err_w)
        assert s.run() == 3
    os.close(out_w)
    os.close(err_w)
    for t in threads:
        t.join(10)
    assert bytes(out) == data
    with os.fdopen(err_r, "rb") as f:
        assert f.read() == b"err"
    os.close(in_r)


def test_session_without_a_relay(tmp_path):
    a, b = socket.socketpair()
    b.close()
    s = gs.Session(a, [], False)
    assert s.run(timeout=1) == gs.LOST
    assert "no shell relay" in s.notes[0]
    a.close()


def test_remote_path():
    assert gs.remote_path({"pid": 5, "dir": "/lib/vms/tv"}) == "/lib/vms/tv/run/shell.sock"
    assert gs.remote_path({"pid": None, "dir": "/lib/vms/tv"}) is None


def test_run_remote_forwards_to_the_hosts_relay(monkeypatch, tmp_path):
    """HOST/NAME: the VM's run directory from the host's `info`, a forward of the shared ssh
    connection to its shell.sock, the session on the forward's port, the forward ended after."""
    from beanjar import ares, remote

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    calls = []

    class Forward:
        def __init__(self, host, uri, address, to_port, record):
            calls.append(("forward", host, uri, address, to_port))

        def __enter__(self):
            return server.getsockname()[1]

        def __exit__(self, *_):
            calls.append(("cancel",))

    def fake_run(sock, argv, tty):
        peer, _ = server.accept()
        with peer:
            calls.append(("run", argv, tty, sock.getpeername() == peer.getsockname()))
        return 4

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(remote, "hosts", lambda: {"pi": "ssh://pi"})
    monkeypatch.setattr(remote, "call_json", lambda h, u, argv: {"pid": 9, "dir": "/srv/vms/tv"})
    monkeypatch.setattr(ares, "Forward", Forward)
    monkeypatch.setattr(gs, "run", fake_run)
    assert gs.run_remote("pi", "tv", ["id"], False) == 4
    assert calls == [
        ("forward", "pi", "ssh://pi", "/srv/vms/tv/run/shell.sock", None),
        ("run", ["id"], False, True),
        ("cancel",),
    ]
    assert not [e for e in os.listdir(tmp_path) if e.startswith(gs.RECORD_PREFIX)]
    server.close()


def test_run_remote_not_running(monkeypatch):
    from beanjar import remote
    from beanjar.base import Error

    monkeypatch.setattr(remote, "hosts", lambda: {"pi": "ssh://pi"})
    monkeypatch.setattr(remote, "call_json", lambda h, u, argv: {"pid": None, "dir": "/x"})
    with pytest.raises(Error, match="not running"):
        gs.run_remote("pi", "tv", [], True)


def test_output_keeps_stdout_and_ends_stdin(relay):
    """output(): no stdin (its end is sent at once), stdout kept, stderr dropped, the status."""
    path, _, r = relay
    wait_for(lambda: r.ready)
    with client(path) as sock:
        sock.settimeout(None)
        assert gs.output(sock, ["0"], timeout=5) == (0, "")
    with client(path) as sock:
        sock.settimeout(None)
        assert gs.output(sock, [], timeout=5) == (3, "")


def test_output_gives_up_at_its_deadline():
    a, b = socket.socketpair()
    b.sendall(gs.hello(0))  # a relay that answers, then a command that never ends
    status, text = gs.output(a, ["cat"], timeout=0.3)
    assert (status, text) == (gs.LOST, "")
    a.close()
    b.close()


def test_stdin_empty(tmp_path):
    empty, full = tmp_path / "empty", tmp_path / "full"
    empty.write_bytes(b"")
    full.write_bytes(b"data")
    r, w = os.pipe()
    with open(os.devnull, "rb") as null, open(empty, "rb") as e, open(full, "rb") as f:
        assert gs.stdin_empty(null.fileno())
        assert gs.stdin_empty(e.fileno())
        assert not gs.stdin_empty(f.fileno())
        assert not gs.stdin_empty(r), "a pipe may yet bring data: forwarded as it comes"
    os.close(r)
    os.close(w)
    assert gs.stdin_empty(r), "a closed descriptor"


@pytest.mark.parametrize(
    "argv, tty, empty, want",
    [
        (["luna-send", "x"], False, True, gs.NULL_STDIN + ["luna-send", "x"]),
        (["luna-send", "x"], False, False, ["luna-send", "x"]),
        (["luna-send", "x"], True, True, ["luna-send", "x"]),
        ([], False, True, []),
    ],
)
def test_session_argv(argv, tty, empty, want):
    assert gs.session_argv(argv, tty, empty) == want


@pytest.mark.parametrize("null", [True, False])
def test_run_gives_the_command_null_stdin(relay, null):
    """`beanjar shell VM -- CMD </dev/null`: CMD's stdin is /dev/null in the guest, not a pipe
    closed at once (which LG's luna-send takes for its end, answering nothing); data piped in is
    forwarded as before."""
    path, guest, r = relay
    wait_for(lambda: r.ready)
    saved = os.dup(0)
    pr, pw = os.pipe()
    try:
        if null:
            src = os.open(os.devnull, os.O_RDONLY)
        else:
            os.write(pw, b"in")
            os.close(pw)
            pw, src = None, pr
        os.dup2(src, 0)
        os.close(src)
        with client(path) as sock:
            sock.settimeout(None)
            gs.run(sock, ["7"], False)
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        for fd in (pr, pw):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    argv = guest.opened[-1][1]
    assert argv == (gs.NULL_STDIN + ["7"] if null else ["7"])
