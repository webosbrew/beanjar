#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The checkout's build lock (docs/dev/guides/build.md, "The build lock"): build/.build.lock, an
flock that a command writing the build's outputs holds exclusive (scripts/build.sh) and one only
reading them holds shared (scripts/regress, for its whole run; it builds first under the exclusive
lock, then holds it shared):

  scripts/buildlock.py exclusive|shared -- COMMAND [ARGS...]

holds the lock and execs COMMAND, which keeps it (and BUILD_LOCKED=1 in its environment) to its end.
Each holder writes "pid PID MODE: COMMAND" into build/.build.lock.who. A command that has to wait
prints, once, who holds it (the holders /proc/locks names, with their .who lines) and then waits.

No holder waits for itself:
  BUILD_LOCKED=1  in the environment: the caller holds the lock in the mode the command needs (an
                  outer `flock build/.build.lock` that sets it, or a holder's own children: the
                  command exec'd here and what scripts/regress builds with); nothing is taken.
  an ancestor     that holds the lock (an outer `flock build/.build.lock CMD`, whose CMD did not set
                  BUILD_LOCKED) counts as the caller holding it, found in /proc/locks; holding it
                  shared, it cannot be taken exclusive below it, which fails at once instead of
                  waiting for ever.
"""

import contextlib
import fcntl
import os
import sys

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(TOP, "build", ".build.lock")
ENV = "BUILD_LOCKED"
MODES = {"exclusive": fcntl.LOCK_EX, "shared": fcntl.LOCK_SH}


class Held(Exception):
    """The lock cannot be had without waiting for ever: an ancestor holds it shared and the
    command needs it exclusive."""


def _parent(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def ancestors(pid=None):
    """This process's ancestors' pids, nearest first (none where there is no /proc)."""
    out, pid = [], _parent(pid or os.getpid())
    while pid > 1 and pid not in out:
        out.append(pid)
        pid = _parent(pid)
    return out


def holders(path=LOCK, locks="/proc/locks"):
    """{pid: "exclusive"|"shared"} of the flock holders of path, from /proc/locks ({} without it)."""
    try:
        st = os.stat(path)
        with open(locks) as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    out = {}
    for line in lines:
        # "1: FLOCK  ADVISORY  WRITE 1234 00:2f:5678 0 EOF"; "1: -> FLOCK ..." waits, holds nothing
        f = line.split()
        if len(f) < 6 or f[1] != "FLOCK" or f[5] != dev:
            continue
        try:
            out[int(f[4])] = "exclusive" if f[3] == "WRITE" else "shared"
        except ValueError:
            pass
    return out


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def who(path=LOCK):
    """Who holds the lock, as a waiter says it: each holder's .who line, or its command line."""
    try:
        with open(path + ".who") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
    except OSError:
        lines = []
    held = holders(path)
    if not held:
        return "; ".join(lines[-3:]) or "unknown"
    said = []
    for pid, mode in sorted(held.items()):
        mine = [x for x in lines if x.startswith(f"pid {pid} ")]
        said.append(mine[-1] if mine else f"pid {pid} {mode}: {_cmdline(pid) or '?'}")
    return "; ".join(said)


def _record(path, mode, command):
    line = f"pid {os.getpid()} {mode}: {command}\n"
    try:
        # exclusive: the only holder, the file is its own; shared: one line more (the waiter reads
        # the live holders' lines only)
        flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if mode == "exclusive" else os.O_APPEND)
        fd = os.open(path + ".who", flags, 0o644)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError:
        pass


class Lock:
    """The build lock in a mode, held from acquire() to release(). held: not taken here because
    the caller holds it (BUILD_LOCKED, an ancestor)."""

    def __init__(self, mode, command=None, path=LOCK, say=None):
        if mode not in MODES:
            raise ValueError(f"mode {mode}: exclusive or shared")
        self.mode, self.path, self.fd, self.held = mode, path, None, False
        self.command = command or " ".join(sys.argv)
        self.say = say or (lambda text: print(text, file=sys.stderr, flush=True))

    def acquire(self):
        if os.environ.get(ENV):
            self.held = True
            return self
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.flock(fd, MODES[self.mode] | fcntl.LOCK_NB)
        except BlockingIOError:
            held = holders(self.path)
            mine = [p for p in ancestors() if p in held]
            if mine:
                os.close(fd)
                if self.mode == "exclusive" and held[mine[0]] == "shared":
                    raise Held(
                        f"{self.path} is held shared by this command's caller (pid {mine[0]}: "
                        f"{_cmdline(mine[0]) or '?'}), and this needs it exclusive"
                    )
                self.held = True
                return self
            self.say(
                f"waiting for {os.path.relpath(self.path)} ({self.mode}): held by {who(self.path)}"
            )
            try:
                fcntl.flock(fd, MODES[self.mode])
            except BaseException:
                os.close(fd)
                raise
        self.fd = fd
        _record(self.path, self.mode, self.command)
        return self

    def downgrade(self):
        """Exclusive to shared, for the run after a build. flock converts by letting go and taking
        again, so another command's exclusive lock may come in between: it waits for that."""
        if self.mode == "shared":
            return
        self.mode = "shared"
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            self.say(f"waiting for {os.path.relpath(self.path)} (shared): held by {who(self.path)}")
            fcntl.flock(self.fd, fcntl.LOCK_SH)
        _record(self.path, self.mode, self.command)

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def main(argv):
    if len(argv) < 3 or argv[0] not in MODES or argv[1] != "--":
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    command = argv[2:]
    try:
        lock = Lock(argv[0], " ".join(command), path=LOCK).acquire()
    except Held as e:
        print(f"buildlock: {e}", file=sys.stderr)
        return 1
    if lock.fd is not None:
        os.set_inheritable(lock.fd, True)  # the command holds it from here, through the exec
    os.environ[ENV] = "1"
    try:
        os.execvp(command[0], command)
    except OSError as e:
        print(f"buildlock: {command[0]}: {e}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
