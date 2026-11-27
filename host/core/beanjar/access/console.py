# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Type one line into the serial console of a VM started with CONSOLE_SOCK/CONSOLE_LOG and return
its output (`host/scripts/vm-launcher console 'command' [WAIT]`; SOCK and LOG default to RUN_DIR/console.sock and
LOG_DIR/console.log, both build/ by default):
an echo of an end marker is typed after the line, and the reply is what the console shows until the
marker (at most WAIT seconds), without the typed lines' echo and the shell's job notices.

Input goes through one long-lived connection, the bridge (python3 -m beanjar.access.console bridge IN SOCK,
a session of its own, its pid in SOCK.in.pid): QEMU drops unread bytes when a client disconnects,
and the guest UART only takes a few bytes at a time. The bridge sends what is appended to the file
SOCK.in, and it drains the socket: QEMU blocks the guest UART (and so the kernel console) while it
is full. It ends when QEMU closes the socket.
"""

import os
import re
import secrets
import select
import socket
import subprocess
import sys
import time

from .. import paths


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, TypeError):
        return False
    except PermissionError:
        return True


def _read_pid(path):
    try:
        with open(path) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def ensure_bridge(sock, tree=paths.TREE):
    inp = sock + ".in"
    if _alive(_read_pid(inp + ".pid")):
        return
    open(inp, "w").close()
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(x for x in (tree.pkg_parent, os.environ.get("PYTHONPATH")) if x),
    )
    p = subprocess.Popen(
        [sys.executable, "-m", "beanjar.access.console", "bridge", inp, sock],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    with open(inp + ".pid", "w") as f:
        f.write(f"{p.pid}\n")
    time.sleep(1)


# A shell's notice of a background job that ended ("[1]+  Done   cmd"), which can arrive before the
# command's own output
_JOB_NOTICE = re.compile(r"^\[\d+\][+-]?\s+(Done|Exit \d+|Terminated|Killed|Stopped|Running)\b")


def _reply(text, line, end):
    """The command's output in text: up to the end marker, without the echo of the typed lines (and
    a prompt before them) or job notices."""
    cut = text.find(end)
    if cut >= 0:
        text = text[: text.rfind("\n", 0, cut) + 1]
    out, echoed = [], False
    quoted = '"' + end[:2] + '""' + end[2:] + '"'
    for ln in text.splitlines(keepends=True):
        body = ln.rstrip("\n")
        if not echoed and line and body.endswith(line):
            echoed = True
        elif quoted in body or _JOB_NOTICE.match(body):
            pass
        else:
            out.append(ln)
    return "".join(out)


def send(line, wait=3.0, sock=None, log=None, tree=paths.TREE):
    """Type line on the console, then an echo of an end marker; the console's output (CRs removed,
    without the echoes and job notices) until the marker, or over wait seconds if it does not come."""
    sock = (
        sock
        or os.environ.get("SOCK")
        or os.path.join(tree.top, os.environ.get("RUN_DIR") or "build", "console.sock")
    )
    log = (
        log
        or os.environ.get("LOG")
        or os.path.join(tree.top, os.environ.get("LOG_DIR") or "build", "console.log")
    )
    ensure_bridge(sock, tree)
    try:
        start = os.path.getsize(log)
    except OSError:
        start = 0
    end = "BJEND-" + secrets.token_hex(4)
    with open(sock + ".in", "a") as f:
        f.write(f'{line}\necho "{end[:2]}""{end[2:]}"\n')
    deadline = time.monotonic() + float(wait)
    text = ""
    while True:
        try:
            with open(log, "rb") as f:
                f.seek(start)
                text = f.read().decode(errors="replace").replace("\r", "")
        except OSError:
            pass
        if end in text or time.monotonic() >= deadline:
            return _reply(text, line, end)
        time.sleep(0.05)


def bridge(inp, sock):
    """Relay what is appended to inp into the unix socket sock; discard what comes back."""
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock)
    s.setblocking(False)
    pos, pending = 0, b""
    with open(inp, "rb") as f:
        while True:
            if not pending:
                f.seek(pos)
                pending = f.read()
                pos += len(pending)
            r, w, _ = select.select([s], [s] if pending else [], [], 0.1)
            if r:
                try:
                    if not s.recv(65536):
                        return 0  # QEMU is gone
                except BlockingIOError:
                    pass
                except OSError:
                    return 0
            if w:
                try:
                    pending = pending[s.send(pending) :]
                except BlockingIOError:
                    pass
                except OSError:
                    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == "bridge":
        try:
            return bridge(argv[1], argv[2])
        except OSError:
            return 1
    print(
        "usage: python3 -m beanjar.access.console bridge IN SOCK (host/scripts/vm-launcher console types a line)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
