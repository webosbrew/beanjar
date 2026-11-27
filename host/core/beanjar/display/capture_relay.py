# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The guest's capture port on the host: capture.sock, where the display front ends answer the
guest's captures of the TV picture (include/webos-capture.h), relayed to QEMU's webos.capture
port, with an on-demand render sink for when no front end is there.

    python3 -m beanjar.display.capture_relay PID LISTEN_FD QEMU_SOCK RUN_DIR [VIEWER BUS_ADDRESS]

The composed picture (the UI, the host-decoded video windows, LG's cursor) exists only in a display
front end: the GTK viewer, beanjar-vnc, a headless viewer. Each connects to capture.sock as before
and says hello; this relay holds QEMU's socket (QEMU_SOCK, qemu_path) instead of the front end, so
the guest always has a provider (its bridge, guest/initramfs/src/virtio-capture.c, sends a request
only after a hello) and a capture needs no front end:

- A request goes to the front end that connected last and said hello. The sink is used only when
  no other front end is connected.
- With none, the relay starts the sink: VIEWER (beanjar-viewer) --headless --capture-sink on the
  VM's display bus (BUS_ADDRESS), which connects here like any front end. Not while the GTK viewer
  or beanjar-vnc has started (RUN_DIR's viewer.pid, vnc.pid) and is still connecting, or a beanjar
  command is starting one (SINK_HOLD names its pid): the request waits for it. Until the sink's
  first picture, its "no picture" (-ENODATA: no scanout yet, a video window not decoded yet) is
  asked again every RETRY_S.
- The sink is stopped IDLE_S after the last request, and at once when another front end connects
  (it takes over: the sink holds the display's one-client sockets, cursor and the media hub's).
  beanjar view, vnc start and screenshot stop it too (core.py, SINK_PIDFILE). Nothing but this
  relay, idle in select, runs while nobody captures.
- A request no provider answers within REPLY_S (below the guest's 3 s) is answered with an error:
  -ENODEV without a provider, -ETIMEDOUT with one; a late answer is dropped.

Without VIEWER (no dbus display) there is no sink: a request with no front end gets -ENODEV at
once. The relay ends when PID (the launcher's, QEMU's after its exec) does, and stops the sink.
"""

import collections
import errno
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import time

from .. import base

QEMU_NAME = "capture-qemu.sock"
SINK_PIDFILE = "capture-sink.pid"
# a front end on its way (core.py): the pid of the beanjar command that starts it; while that runs,
# no sink is started, so that the front end finds the display's sockets free
SINK_HOLD = "capture-sink.hold"
SINK_LOG = "capture-sink.log"
FRONT_ENDS = (("viewer.pid", "beanjar-viewer"), ("vnc.pid", "beanjar-vnc"))

MAGIC = 0x50414357  # WEBOS_CAPTURE_MAGIC
VIDEO, OSD = 2, 4  # the modes, WEBOS_CAPTURE_VIDEO to _OSD
MAX_W, MAX_H = 1920, 1080
HEADER = struct.Struct("<6Iii")  # magic, sequence, mode, width, height, size, status, reserved

IDLE_S = 10.0  # the sink's life after the last request; above eplmanager's 3 s logo check
REPLY_S = 2.5  # a request's answer, below the guest bridge's 3 s
HELLO_S = 1.0  # the guest's hello repeated while idle (QEMU may accept before the guest listens)
RETRY_S = 0.05  # a warming sink's "no picture" asked again
SINK_FAIL_S = 5.0  # a sink that fails (a status other than 0) sooner than this...
SINK_BACKOFF_S = 10.0  # ...and is not started again for this long
SINK_STOP_S = 5.0  # SIGTERM, then SIGKILL


class ProtocolError(Exception):
    pass


def qemu_path(capture_sock):
    """QEMU's socket for the webos.capture port, beside capture.sock."""
    return os.path.join(os.path.dirname(capture_sock), QEMU_NAME)


def say(line):
    print(f"capture_relay: {line}", flush=True)


def header(seq=0, mode=0, width=0, height=0, size=0, status=0):
    return HEADER.pack(MAGIC, seq, mode, width, height, size, status, 0)


def unpack(data):
    """(magic, seq, mode, width, height, size, status, reserved) of a header."""
    return HEADER.unpack(data[: HEADER.size])


def check_request(h):
    """A guest request, as the front ends check it (host/viewer/core/capture.c)."""
    magic, seq, mode, width, height, size, status, reserved = h
    if magic != MAGIC or not seq or size or status or reserved:
        raise ProtocolError("invalid request")
    if not (0 < width <= MAX_W and 0 < height <= MAX_H):
        raise ProtocolError(f"invalid request size {width}x{height}")


def check_answer(h):
    """A front end's hello (sequence 0) or answer, as the guest's bridge checks it."""
    magic, seq, mode, width, height, size, status, reserved = h
    if magic != MAGIC or reserved or status > 0 or status < -4095:
        raise ProtocolError("invalid header")
    if not seq:
        if size or mode or width or height or status not in (0, -errno.ENODEV):
            raise ProtocolError("invalid hello")
    elif size != (0 if status else width * height * 4) or size > MAX_W * MAX_H * 4:
        raise ProtocolError("invalid answer size")


class Peer:
    """One socket: framed reads (a header and its payload) and queued non-blocking writes."""

    def __init__(self, sock, name, payload=True):
        sock.setblocking(False)
        self.sock, self.name, self.payload = sock, name, payload
        self.buf = bytearray()
        self.out = collections.deque()
        self.pid = None
        self.sink = False  # the relay's own sink
        self.ready = False  # said hello
        self.pictured = False  # answered with a picture
        self.answered = 0  # requests it answered
        self.at = time.monotonic()

    def fileno(self):
        return self.sock.fileno()

    def send(self, data):
        self.out.append(memoryview(data))

    def flush(self):
        """Writes what it can; False when the peer is gone."""
        while self.out:
            try:
                n = self.sock.send(self.out[0])
            except (BlockingIOError, InterruptedError):
                return True
            except OSError:
                return False
            if n < len(self.out[0]):
                self.out[0] = self.out[0][n:]
                return True
            self.out.popleft()
        return True

    def read(self, check):
        """The complete messages ([(header, payload)]) after a read; None when the peer is gone.
        check(header) raises ProtocolError for an invalid one."""
        try:
            data = self.sock.recv(1 << 20)
        except (BlockingIOError, InterruptedError):
            return []
        except OSError:
            return None
        if not data:
            return None
        self.buf += data
        out = []
        while len(self.buf) >= HEADER.size:
            h = unpack(self.buf)
            check(h)
            need = HEADER.size + (h[5] if self.payload else 0)
            if len(self.buf) < need:
                break
            out.append((h, bytes(self.buf[HEADER.size : need])))
            del self.buf[:need]
        return out

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def peer_pid(sock):
    try:
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
    except OSError:
        return None


def _die_with_parent():
    """The sink's preexec: SIGTERM when the relay ends, however it ends (PR_SET_PDEATHSIG)."""
    import ctypes

    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):
        pass


class Request:
    def __init__(self, h, now):
        self.h, self.at, self.deadline = h, now, now + REPLY_S
        self.peer = None  # the provider it was sent to
        self.retry = 0.0  # not sent again before

    @property
    def seq(self):
        return self.h[1]

    def error(self, status):
        _, seq, mode, width, height, _, _, _ = self.h
        return header(seq, mode, width, height, 0, status)


class Relay:
    """The relay's state; run() is the loop. sink_argv None: no sink."""

    def __init__(self, listener, qemu_sock, run_dir, sink_argv=None, running=lambda: True):
        listener.setblocking(False)
        self.listener, self.qemu_sock, self.run_dir = listener, qemu_sock, run_dir
        self.sink_argv, self.running = sink_argv, running
        self.qemu = None
        self.qemu_retry = 0.0
        self.hello_at = 0.0
        self.providers = []
        self.req = None
        self.last_request = 0.0
        self.sink = None  # the sink's Popen
        self.sink_at = 0.0
        self.sink_stopping = None  # when the SIGTERM went
        self.backoff = 0.0
        self.pidfile = os.path.join(run_dir, SINK_PIDFILE)

    # QEMU's side: the guest's requests in, the providers' answers and the relay's hello out

    def connect_qemu(self, now):
        if self.qemu or now < self.qemu_retry:
            return
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.qemu_sock)
        except OSError:
            s.close()
            self.qemu_retry = now + 0.2
            return
        self.qemu = Peer(s, "qemu", payload=False)
        self.hello_at = 0.0
        say("connected to QEMU's webos.capture")

    def drop_qemu(self):
        self.qemu.close()
        self.qemu = None
        self.req = None
        self.qemu_retry = time.monotonic() + 0.2

    def guest_request(self, h, now):
        if self.req:
            say(f"request {self.req.seq} replaced by {h[1]}")
        self.req = Request(h, now)
        self.last_request = now
        self.route(now)

    def answer(self, data):
        if self.qemu:
            self.qemu.send(data)
        self.req = None
        self.hello_at = time.monotonic() + HELLO_S

    # the providers: the front ends and the sink

    def provider(self):
        """The provider for a request: the last connected other front end that said hello, else the
        sink."""
        ready = [p for p in self.providers if p.ready]
        others = [p for p in ready if not p.sink]
        return (others or ready or [None])[-1]

    def route(self, now):
        req = self.req
        if not req or req.peer or now < req.retry:
            return
        p = self.provider()
        if p:
            req.peer = p
            p.send(HEADER.pack(*req.h))
            return
        if not self.sink_argv:
            self.answer(req.error(-errno.ENODEV))
            return
        self.want_sink(now)

    def front_end_starting(self):
        """The GTK viewer or beanjar-vnc runs but has not connected (or said hello) yet."""
        if any(not p.sink for p in self.providers):
            return True
        if alive(base.read_pid(os.path.join(self.run_dir, SINK_HOLD)) or 0):
            return True
        for name, comm in FRONT_ENDS:
            pid = base.read_pid(os.path.join(self.run_dir, name))
            if pid and base.proc_alive(pid, comm):
                return True
        return False

    def accept(self):
        try:
            s, _ = self.listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        p = Peer(s, "front end")
        p.pid = peer_pid(s)
        p.sink = bool(self.sink and p.pid == self.sink.pid)
        if p.sink:
            p.name = "sink"
        self.providers.append(p)
        say(f"{p.name} connected (pid {p.pid})")
        if not p.sink and self.sink:
            self.stop_sink("a front end connected")

    def drop_provider(self, p, why):
        say(f"{p.name} (pid {p.pid}) gone after {p.answered} answers: {why}")
        p.close()
        self.providers.remove(p)
        if self.req and self.req.peer is p:
            self.req.peer = None
            self.route(time.monotonic())

    def provider_message(self, p, h, payload, now):
        _, seq, mode, width, height, _, status, _ = h
        if not seq:
            was = p.ready
            p.ready = not status
            if p.ready and not was:
                self.route(now)
            return
        req = self.req
        if not req or req.peer is not p or req.h[1:5] != h[1:5]:
            return  # an answer to a request already answered or replaced
        if p.sink and not p.pictured and status == -errno.ENODATA and now + RETRY_S < req.deadline:
            req.peer, req.retry = None, now + RETRY_S  # warming up: no scanout or video yet
            return
        if not status and not p.pictured:
            p.pictured = True
            if p.sink:
                say(f"sink's first picture {now - self.sink_at:.3f} s after its start")
        p.answered += 1
        self.answer(HEADER.pack(*h) + payload)

    # the sink

    def want_sink(self, now):
        if self.sink or now < self.backoff or self.front_end_starting():
            return
        try:
            log = open(os.path.join(self.run_dir, SINK_LOG), "ab")
        except OSError as e:
            say(f"no sink: {e}")
            self.backoff = now + SINK_BACKOFF_S
            return
        with log:
            try:
                self.sink = subprocess.Popen(
                    self.sink_argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    close_fds=True,
                    preexec_fn=_die_with_parent,
                )
            except OSError as e:
                say(f"no sink: {e}")
                self.backoff = now + SINK_BACKOFF_S
                return
        self.sink_at, self.sink_stopping = now, None
        try:
            with open(self.pidfile, "w") as f:
                f.write(f"{self.sink.pid}\n")
        except OSError:
            pass
        say(f"sink started (pid {self.sink.pid})")

    def stop_sink(self, why):
        if not self.sink or self.sink_stopping:
            return
        say(f"stopping the sink (pid {self.sink.pid}): {why}")
        self.sink_stopping = time.monotonic()
        try:
            self.sink.send_signal(signal.SIGTERM)
        except OSError:
            pass

    def check_sink(self, now):
        if not self.sink:
            return
        code = self.sink.poll()
        if code is None:
            if self.sink_stopping and now - self.sink_stopping > SINK_STOP_S:
                self.sink.kill()
            elif not self.sink_stopping and not self.req and now - self.last_request >= IDLE_S:
                self.stop_sink(f"idle {IDLE_S:g} s")
            return
        if self.sink_stopping or not code:
            # by the relay, or by a beanjar command for a front end (core.py), on SIGTERM
            say(f"sink stopped (status {code})")
        else:
            say(f"the sink failed (status {code})")
            if now - self.sink_at < SINK_FAIL_S:
                self.backoff = now + SINK_BACKOFF_S
        self.sink = None
        try:
            os.unlink(self.pidfile)
        except OSError:
            pass
        for p in [p for p in self.providers if p.sink]:
            self.drop_provider(p, "the sink ended")
        if self.req and not self.req.peer:
            self.route(now)

    # the loop

    def step(self, timeout=1.0):
        now = time.monotonic()
        self.connect_qemu(now)
        self.check_sink(now)
        if self.req:
            if now >= self.req.deadline:
                status = -errno.ETIMEDOUT if self.req.peer else -errno.ENODEV
                say(f"request {self.req.seq}: no answer in {REPLY_S:g} s ({status})")
                self.answer(self.req.error(status))
            else:
                self.route(now)
        if self.qemu and not self.req and now >= self.hello_at:
            self.qemu.send(header())
            self.hello_at = now + HELLO_S
        peers = ([self.qemu] if self.qemu else []) + self.providers
        for p in peers:
            if not p.flush():
                self.drop_qemu() if p is self.qemu else self.drop_provider(p, "write failed")
        peers = ([self.qemu] if self.qemu else []) + self.providers
        waits = [timeout, self.hello_at - now if self.qemu and not self.req else timeout]
        if self.req:
            waits.append(self.req.deadline - now)
            if not self.req.peer and self.req.retry > now:
                waits.append(self.req.retry - now)
        if self.sink:
            waits.append(0.2)  # its exit, its idle
        if not self.qemu:
            waits.append(0.2)
        wait = max(0.0, min(waits))
        readable, writable, _ = select.select(
            [self.listener, *peers], [p for p in peers if p.out], [], wait
        )
        now = time.monotonic()
        for p in readable:
            if p is self.listener:
                self.accept()
                continue
            try:
                msgs = p.read(check_request if p is self.qemu else check_answer)
            except ProtocolError as e:
                msgs, why = None, str(e)
            else:
                why = "closed"
            if msgs is None:
                if p is self.qemu:
                    say(f"QEMU's socket {why}")
                    self.drop_qemu()
                elif p in self.providers:
                    self.drop_provider(p, why)
                continue
            for h, payload in msgs:
                if p is self.qemu:
                    self.guest_request(h, now)
                elif p in self.providers:
                    self.provider_message(p, h, payload, now)
        for p in writable:
            if p in self.providers and not p.flush():
                self.drop_provider(p, "write failed")
            elif p is self.qemu and self.qemu and not p.flush():
                self.drop_qemu()

    def close(self):
        if self.sink:
            self.stop_sink("the VM ended")
            try:
                self.sink.wait(SINK_STOP_S)
            except subprocess.TimeoutExpired:
                self.sink.kill()
                self.sink.wait()
            self.check_sink(time.monotonic())
        for p in list(self.providers):
            p.close()
        if self.qemu:
            self.qemu.close()

    def run(self):
        try:
            while self.running():
                self.step()
        finally:
            self.close()


def alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (4, 6):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    pid, fd, qemu_sock, run_dir = int(argv[0]), int(argv[1]), argv[2], argv[3]
    sink = [argv[4], argv[5], "--headless", "--capture-sink"] if len(argv) == 6 else None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    relay = Relay(socket.socket(fileno=fd), qemu_sock, run_dir, sink, lambda: alive(pid))
    relay.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
