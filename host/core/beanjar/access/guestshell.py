# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""`beanjar shell`: shells and commands inside webOS over the webos.shell virtio-serial port, as
root, without SSH, developer mode or a network.

The guest's agent (guest/initramfs/src/virtio-shell.c) runs each session's command with
webos-enter, on a terminal or with pipes; the frames are include/webos-shell.h's. QEMU's socket
for the port takes one client, so the relay (`python3 -m beanjar.access.guestshell PID LISTEN_FD
QEMU_SOCK`, a child of the launcher like the IR relay) holds it and serves RUN_DIR/shell.sock to
any number of clients, each with sessions numbered its own way, which it renumbers for the guest.
It ends when PID (the launcher's, QEMU's after its exec) does. Whoever can open shell.sock (its
owner, mode 0600) is root in the VM, as with the serial console.

A client (run, `beanjar shell`) opens one session: a VM of another host is reached through a
forward of the host's shared ssh connection to that host's shell.sock (run_remote)."""

import os
import secrets
import select
import signal
import socket
import struct
import sys
import time

MAGIC = 0x4C485357  # WEBOS_SHELL_MAGIC, "WSHL"
VERSION = 1
MAX = 65536
WINDOW = 256 << 10
HEADER = struct.Struct("<IHHII")  # magic, type, reserved, session, len
OPEN_HEADER = struct.Struct("<HHHH")  # flags, rows, cols, term_len
HELLO, OPEN, STDIN, STDOUT, STDERR, WINSIZE, SIGNAL, ACK, EXIT, CLOSE = range(1, 11)
TTY = 1
SIGNALS = (1, 2, 3, 9, 15)  # HUP INT QUIT KILL TERM: the same numbers on every POSIX host
SOCK = "shell.sock"  # in RUN_DIR: the relay's
QEMU_NAME = "shell-qemu.sock"  # in RUN_DIR: QEMU's, for the port
RECORD_PREFIX = "beanjar-shell-"  # a remote session's forward record: beanjar-shell-PID-RANDOM
LOST = 255  # the status of a session that ended without one (as ssh's)


class ProtocolError(Exception):
    pass


def frame(kind, session, payload=b""):
    if len(payload) > MAX:
        raise ValueError(f"frame of {len(payload)} bytes")
    return HEADER.pack(MAGIC, kind, 0, session, len(payload)) + payload


def word(n):
    return struct.pack("<I", n)


def unword(payload):
    return struct.unpack_from("<I", payload)[0] if len(payload) >= 4 else 0


def hello(nonce=0):
    return frame(HELLO, 0, struct.pack("<II", VERSION, nonce))


def open_payload(argv, tty, rows=24, cols=80, term=""):
    """OPEN's payload: the command's words (none: a login shell), with a terminal of rows x cols
    and term when tty."""
    t = (term if tty else "").encode()
    words = [w.encode() if isinstance(w, str) else w for w in argv]
    if any(b"\0" in w for w in words):
        raise ValueError("a word with a NUL")
    body = OPEN_HEADER.pack(TTY if tty else 0, rows, cols, len(t)) + t
    body += b"".join(w + b"\0" for w in words)
    if len(body) > MAX:
        raise ValueError("the command is too long")
    return body


def parse_open(payload):
    """(tty, rows, cols, term, argv) of OPEN's payload, as the agent reads it."""
    if len(payload) < OPEN_HEADER.size:
        raise ProtocolError("a short OPEN")
    flags, rows, cols, n = OPEN_HEADER.unpack_from(payload)
    rest = payload[OPEN_HEADER.size :]
    if n > len(rest) or (len(rest) > n and rest[-1] != 0):
        raise ProtocolError("a malformed OPEN")
    term, words = rest[:n].decode(), rest[n:]
    argv = [w.decode(errors="surrogateescape") for w in words.split(b"\0")[:-1]]
    return bool(flags & TTY), rows, cols, term, argv


class Reader:
    """Frames of a byte stream, as (type, session, payload). With synced False, everything before
    the first HELLO is passed over; bytes without the magic are passed over to the next one."""

    def __init__(self, synced=True):
        self.buf = bytearray()
        self.synced = synced

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= HEADER.size:
            magic, kind, _, session, n = HEADER.unpack_from(self.buf)
            if magic != MAGIC or n > MAX:
                at = self.buf.find(struct.pack("<I", MAGIC), 1)
                del self.buf[: at if at > 0 else len(self.buf) - 3]
                continue
            if len(self.buf) < HEADER.size + n:
                break
            payload = bytes(self.buf[HEADER.size : HEADER.size + n])
            del self.buf[: HEADER.size + n]
            if kind == HELLO:
                self.synced = True
            if self.synced:
                out.append((kind, session, payload))
        return out


def status_of(kind, payload):
    """A session's exit status from its EXIT (the command's) or CLOSE (LOST)."""
    return unword(payload) & 0xFF if kind == EXIT else LOST


# -- the relay --


def say(line):
    print(f"{time.strftime('%H:%M:%S')} shell relay: {line}", flush=True)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Peer:
    """A connection: a reader of its frames and what is still to be sent to it."""

    def __init__(self, sock, synced=True):
        sock.setblocking(False)
        self.sock, self.reader, self.out = sock, Reader(synced), bytearray()
        self.sessions = {}  # a client's: its session -> the guest's

    def send(self, data):
        self.out += data

    def flush(self):
        if self.out:
            try:
                n = self.sock.send(self.out)
            except (BlockingIOError, InterruptedError):
                return
            del self.out[:n]


class Relay:
    """shell.sock's clients to QEMU's socket for the port, one connection to it at a time."""

    def __init__(self, listener, qemu_sock, running=lambda: True, log=say):
        listener.setblocking(False)
        self.listener, self.qemu_sock, self.running, self.log = listener, qemu_sock, running, log
        self.guest = None
        self.nonce = 0
        self.ready = False
        self.clients = []
        self.routes = {}  # the guest's session -> (client, its session)
        self.next = 0

    def connect_guest(self, sock=None):
        """A connection to QEMU's socket (sock, or a new one to qemu_sock): whether there is one."""
        if sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.qemu_sock)
            except OSError:
                sock.close()
                return False
        self.guest = Peer(sock, synced=False)
        self.say_hello()
        return True

    def say_hello(self):
        self.nonce = secrets.randbits(32) or 1
        self.ready = False
        self.guest.send(hello(self.nonce))

    def lose_all(self, reason):
        for client, cid in self.routes.values():
            client.sessions.pop(cid, None)
            client.send(frame(CLOSE, cid, reason.encode()))
        self.routes.clear()

    def on_guest(self, kind, gid, payload):
        if kind == HELLO:
            nonce = struct.unpack_from("<II", payload)[1] if len(payload) >= 8 else 0
            if nonce and nonce == self.nonce:
                self.ready = True
                self.log("the guest's agent answered")
            elif not nonce:
                if self.routes:
                    self.log("the guest's agent started again: its sessions are gone")
                self.lose_all("the VM's shell agent started again (did the VM restart?)")
                self.say_hello()
            return
        route = self.routes.get(gid)
        if route is None or kind not in (STDOUT, STDERR, ACK, EXIT, CLOSE):
            return
        client, cid = route
        client.send(frame(kind, cid, payload))
        if kind in (EXIT, CLOSE):
            del self.routes[gid]
            client.sessions.pop(cid, None)

    def new_gid(self):
        while True:
            self.next = self.next % 0xFFFFFFFF + 1
            if self.next not in self.routes:
                return self.next

    def on_client(self, client, kind, cid, payload):
        if kind == OPEN:
            why = None
            if cid in client.sessions:
                why = "the session is open already"
            elif not self.ready:
                why = (
                    "the VM's shell agent has not answered: the VM is still starting, or its "
                    "initramfs predates beanjar shell"
                )
            if why:
                client.send(frame(CLOSE, cid, why.encode()))
                return
            gid = self.new_gid()
            client.sessions[cid] = gid
            self.routes[gid] = (client, cid)
            self.guest.send(frame(OPEN, gid, payload))
            return
        gid = client.sessions.get(cid)
        if gid is None or kind not in (STDIN, WINSIZE, SIGNAL, ACK, CLOSE):
            return
        if self.guest:
            self.guest.send(frame(kind, gid, payload))
        if kind == CLOSE:
            del client.sessions[cid]
            self.routes.pop(gid, None)

    def drop_client(self, client):
        for gid in client.sessions.values():
            self.routes.pop(gid, None)
            if self.guest:
                self.guest.send(frame(CLOSE, gid))
        client.sessions.clear()
        self.clients.remove(client)
        client.sock.close()

    def drop_guest(self):
        self.log("QEMU's socket closed")
        self.guest.sock.close()
        self.guest = None
        self.ready = False
        self.lose_all("the VM's shell port closed (did the VM stop?)")

    def step(self, timeout=1.0):
        """One round: what is readable read, what is pending sent."""
        rd = [self.listener] + [c.sock for c in self.clients]
        wr = [c.sock for c in self.clients if c.out]
        if self.guest:
            rd.append(self.guest.sock)
            if self.guest.out:
                wr.append(self.guest.sock)
        r = select.select(rd, wr, [], timeout)[0]
        if self.listener in r:
            try:
                conn, _ = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                conn = None
            if conn:
                client = Peer(conn)
                client.send(hello())
                self.clients.append(client)
        if self.guest and self.guest.sock in r:
            try:
                data = self.guest.sock.recv(MAX)
            except (BlockingIOError, InterruptedError):
                data = None
            except OSError:
                data = b""
            if data == b"":
                self.drop_guest()
            elif data:
                for kind, gid, payload in self.guest.reader.feed(data):
                    self.on_guest(kind, gid, payload)
        for client in list(self.clients):
            if client.sock not in r:
                continue
            try:
                data = client.sock.recv(MAX)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                data = b""
            if not data:
                self.drop_client(client)
                continue
            for kind, cid, payload in client.reader.feed(data):
                self.on_client(client, kind, cid, payload)
        for peer in [self.guest, *self.clients]:
            if peer is None or not peer.out:
                continue
            try:
                peer.flush()
            except OSError:
                if peer is self.guest:
                    self.drop_guest()
                else:
                    self.drop_client(peer)

    def serve(self):
        last_try = 0.0
        while self.running():
            if self.guest is None and time.monotonic() - last_try >= 0.2:
                last_try = time.monotonic()
                if self.connect_guest():
                    self.log("connected to the port")
            self.step(0.2 if self.guest is None else 1.0)


def relay_main(argv):
    """PID LISTEN_FD QEMU_SOCK: serves the listening socket LISTEN_FD while PID lives."""
    pid, fd, qemu_sock = int(argv[0]), int(argv[1]), argv[2]
    listener = socket.socket(fileno=fd)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        Relay(listener, qemu_sock, lambda: alive(pid)).serve()
    except KeyboardInterrupt:
        pass
    return 0


# -- a client: one session --


def terminal_size(fds=(0, 1)):
    for fd in fds:
        try:
            size = os.get_terminal_size(fd)
            if size.lines and size.columns:
                return size.lines, size.columns
        except OSError:
            continue
    return 24, 80


def write_all(fd, data):
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [])
            continue
        view = view[n:]


class Session:
    """A session on a connection to a relay (sock), with this process's stdio."""

    def __init__(self, sock, argv, tty, stdin=0, stdout=1, stderr=2, err=None):
        self.sock, self.argv, self.tty = sock, argv, tty
        self.stdin, self.stdout, self.stderr = stdin, stdout, stderr
        self.err = err or self.note
        self.notes = []
        self.reader = Reader(synced=False)
        self.credit = WINDOW
        self.stdin_open = True
        self.interrupts = 0

    def note(self, text):
        """A message for stderr, written when the session is over (the terminal is ours again)."""
        self.notes.append(text)

    def send(self, kind, payload=b""):
        self.sock.sendall(frame(kind, 1, payload))

    def wait_hello(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.sock], [], [], left)[0]:
                return "no answer from the VM's shell relay"
            data = self.sock.recv(MAX)
            if not data:
                return (
                    "the VM has no shell relay: it was started by a beanjar that predates "
                    "beanjar shell (stop and start it)"
                )
            for kind, _, payload in self.reader.feed(data):
                if kind == HELLO:
                    return None

    def on_signal(self, signum):
        """A signal to this process: the status to end with, or None to go on."""
        if signum == signal.SIGWINCH:
            if self.tty:
                rows, cols = terminal_size()
                self.send(WINSIZE, struct.pack("<HH", rows, cols))
            return None
        if signum == signal.SIGHUP or (signum == signal.SIGTERM and self.tty):
            self.send(CLOSE)
            return 128 + signum
        if signum == signal.SIGINT:
            self.interrupts += 1
            if self.interrupts >= 3:  # the command does not end: give it up
                self.send(CLOSE)
                return 128 + signum
        if signum in SIGNALS:
            self.send(SIGNAL, word(signum))
        return None

    def on_frame(self, kind, payload):
        if kind in (STDOUT, STDERR):
            try:
                write_all(self.stdout if kind == STDOUT or self.tty else self.stderr, payload)
            except BrokenPipeError:
                self.send(CLOSE)
                return 128 + signal.SIGPIPE
            self.send(ACK, word(len(payload)))
        elif kind == ACK:
            self.credit = min(WINDOW, self.credit + unword(payload))
        elif kind == EXIT:
            return status_of(kind, payload)
        elif kind == CLOSE:
            self.err(payload.decode(errors="replace") or "the session ended without a status")
            return LOST
        return None

    def run(self, wake=None, timeout=10.0, deadline=None):
        """Opens the session and runs it to its end: its exit status. wake, a file descriptor that
        signal.set_wakeup_fd writes signal numbers to; deadline, a time.monotonic() past which
        the session is given up (LOST)."""
        why = self.wait_hello(timeout)
        if why:
            self.err(why)
            return LOST
        rows, cols = terminal_size() if self.tty else (0, 0)
        term = os.environ.get("TERM") or "vt100"
        self.send(OPEN, open_payload(self.argv, self.tty, rows, cols, term))
        if not self.stdin_open and not self.tty:
            self.send(STDIN)  # no stdin: its end now
        while True:
            rd = [self.sock] + ([wake] if wake is not None else [])
            if self.stdin_open and self.credit > 0:
                rd.append(self.stdin)
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                self.send(CLOSE)
                self.err("the command did not end in time")
                return LOST
            try:
                r = select.select(rd, [], [], left)[0]
            except InterruptedError:
                continue
            if wake is not None and wake in r:
                try:
                    signums = os.read(wake, 64)
                except BlockingIOError:
                    signums = b""
                for signum in signums:
                    status = self.on_signal(signum)
                    if status is not None:
                        return status
            if self.sock in r:
                try:
                    data = self.sock.recv(MAX)
                except ConnectionError:
                    data = b""
                if not data:
                    self.err("the connection to the VM's shell relay was lost")
                    return LOST
                for kind, _, payload in self.reader.feed(data):
                    status = self.on_frame(kind, payload)
                    if status is not None:
                        return status
            if self.stdin in r:
                try:
                    data = os.read(self.stdin, min(self.credit, MAX))
                except OSError:
                    data = b""
                if data:
                    self.credit -= len(data)
                    self.send(STDIN, data)
                else:
                    self.stdin_open = False
                    if not self.tty:
                        self.send(STDIN)  # its end


class Capture(Session):
    """A session whose stdout is kept (stdout) and whose stderr goes to notes, stdin empty."""

    def __init__(self, sock, argv):
        super().__init__(sock, argv, False, stdin=-1)
        self.stdin_open = False
        self.stdout = bytearray()

    def on_frame(self, kind, payload):
        if kind in (STDOUT, STDERR):
            if kind == STDOUT:
                self.stdout += payload
            self.send(ACK, word(len(payload)))
            return None
        return super().on_frame(kind, payload)


def output(sock, argv, timeout=10.0):
    """(exit status, stdout as text) of argv run without a terminal on sock, a connection to a
    relay; LOST when it did not end within timeout seconds (the hello's wait included)."""
    session = Capture(sock, argv)
    deadline = time.monotonic() + timeout
    try:
        status = session.run(timeout=timeout, deadline=deadline)
    except (BrokenPipeError, ConnectionError):
        return LOST, session.stdout.decode(errors="replace")
    return status, session.stdout.decode(errors="replace")


HANDLED = (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP, signal.SIGWINCH)
# the command's own stdin made /dev/null in the guest, as this side's is: LG's luna-send quits
# without its answer, status 0, when its stdin is a pipe already at its end (`true | luna-send`)
NULL_STDIN = ["/bin/sh", "-c", 'exec "$@" </dev/null', "sh"]


def stdin_empty(fd=0):
    """Whether fd holds nothing to read, known without reading it: closed, /dev/null or an empty
    regular file."""
    import stat

    try:
        st = os.fstat(fd)
    except OSError:
        return True
    if stat.S_ISREG(st.st_mode):
        return st.st_size == 0
    if stat.S_ISCHR(st.st_mode):
        try:
            return st.st_rdev == os.stat(os.devnull).st_rdev
        except OSError:
            return False
    return False


def session_argv(argv, tty, empty):
    """The words to open: a command without a terminal whose stdin here is empty (stdin_empty) gets
    /dev/null there rather than a pipe closed at once; a login shell and a terminal stay as asked."""
    return NULL_STDIN + list(argv) if argv and not tty and empty else argv


def run(sock, argv, tty):
    """The session of argv (none: a login shell) on sock, a connection to a relay, with this
    process's stdio (with tty, on a terminal of the size of ours, ours raw meanwhile): its exit
    status. The signals that reach this process go on to the command (SIGWINCH, a new size)."""
    import termios
    import tty as ttymod

    saved = None
    if tty and os.isatty(0):
        saved = termios.tcgetattr(0)
        ttymod.setraw(0)
    rfd, wfd = os.pipe()
    os.set_blocking(rfd, False)
    os.set_blocking(wfd, False)
    old_wake = signal.set_wakeup_fd(wfd, warn_on_full_buffer=False)
    old = {s: signal.signal(s, lambda *_: None) for s in HANDLED}
    empty = not tty and stdin_empty(0)
    session = Session(sock, session_argv(argv, tty, empty), tty)
    session.stdin_open = not empty
    try:
        return session.run(wake=rfd)
    except (BrokenPipeError, ConnectionError):
        session.note("the connection to the VM's shell relay was lost")
        return LOST
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)
        signal.set_wakeup_fd(old_wake)
        os.close(rfd)
        os.close(wfd)
        if saved is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
        for text in session.notes:
            print(f"beanjar shell: {text}", file=sys.stderr, flush=True)


def want_tty(command, force, never, stdin_tty):
    """Whether the session gets a terminal: -t forces it, -T never; else a login shell (no
    command) with a terminal on stdin, as ssh does."""
    if force and never:
        raise ValueError("-t and -T: one or the other")
    if force or never:
        return force
    return not command and stdin_tty


def local_socket(vm):
    """A connection to the relay of VM vm (a library VM here)."""
    from ..base import Error

    if not vm.pid():
        raise Error(f"{vm.name} is not running")
    path = os.path.join(vm.run_dir, SOCK)
    if not os.path.exists(path):
        raise Error(
            f"{vm.name} has no shell relay: it was started by a beanjar that predates beanjar "
            "shell (stop and start it)"
        )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except OSError as e:
        sock.close()
        raise Error(f"{path}: {e.strerror or e}") from None
    return sock


def remote_path(info):
    """Where the relay's socket is on the VM's host, from its `beanjar show --json`; None when the
    VM is not running there."""
    import posixpath

    from .. import base

    if not info.get("pid") or not info.get("dir"):
        return None
    return posixpath.join(info["dir"], base.RUN_DIR, SOCK)


def run_remote(host, name, argv, tty):
    """The session on VM name of host, through a forward of the host's shared ssh connection to
    its relay's socket: its exit status."""
    import shutil
    import tempfile

    from ..base import Error
    from . import ares, remote

    uri = remote.hosts()[host]
    info = remote.call_json(host, uri, ["show", remote.own(name)])
    path = remote_path(info if isinstance(info, dict) else {})
    if path is None:
        raise Error(f"{host}/{name} is not running")
    old = {s: signal.signal(s, ares.raise_terminated) for s in (signal.SIGTERM, signal.SIGHUP)}
    home = None
    try:
        ares.sweep_homes(ares.home_parent())
        home = tempfile.mkdtemp(prefix=f"{RECORD_PREFIX}{os.getpid()}-", dir=ares.home_parent())
        with ares.Forward(host, uri, path, None, os.path.join(home, ares.FORWARD_RECORD)) as port:
            for s, handler in old.items():
                signal.signal(s, handler)  # the session's own, from here
            sock = socket.create_connection((ares.FORWARD_HOST, port))
            with sock:
                return run(sock, argv, tty)
    except ares.Terminated as e:
        return e.status
    finally:
        if home:
            shutil.rmtree(home, ignore_errors=True)
        for s, handler in old.items():
            signal.signal(s, handler)


if __name__ == "__main__":
    sys.exit(relay_main(sys.argv[1:]))
