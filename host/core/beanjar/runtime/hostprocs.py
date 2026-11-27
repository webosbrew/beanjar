# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Host processes and ports: who listens where, a free port, a process's times and
environment, reaping children, and a command whose output is streamed."""

import ipaddress
import os
import socket
import subprocess
import sys

from ..base import Error, proc_alive


def tcp_listeners(port):
    """(address, inode) of the host's TCP listeners on port (/proc/net/tcp and tcp6)."""
    found = []
    for table in ("tcp", "tcp6"):
        try:
            with open(f"/proc/net/{table}") as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            address, hexport = fields[1].split(":")
            if fields[3] != "0A" or int(hexport, 16) != port:  # 0A: LISTEN
                continue
            raw = bytes.fromhex(address)  # 32-bit words, each in the host's byte order
            raw = b"".join(
                int.from_bytes(raw[i : i + 4], sys.byteorder).to_bytes(4, "big")
                for i in range(0, len(raw), 4)
            )
            ip = ipaddress.ip_address(raw)
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            found.append((ip, int(fields[9])))
    return found


def socket_owner(inode):
    """ "comm (pid N)" of the process holding a socket, or None when it is not ours to see."""
    target = f"socket:[{inode}]"
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
            if any(os.readlink(f"/proc/{pid}/fd/{fd}") == target for fd in fds):
                with open(f"/proc/{pid}/comm") as f:
                    return f"{f.read().strip()} (pid {pid})"
        except OSError:
            continue
    return None


def port_conflict(address, port):
    """Who listens where binding address:port would collide (the same address, or either side on all
    addresses), as a sentence; None when no one does or address is a name."""
    try:
        want = ipaddress.ip_address(address)
    except ValueError:
        return None
    for ip, inode in tcp_listeners(port):
        if ip == want or ip.is_unspecified or want.is_unspecified:
            where = "*" if ip.is_unspecified else f"[{ip}]" if ip.version == 6 else str(ip)
            holder = socket_owner(inode) or "a process of another user"
            return f"port {port} is taken: {holder} listens on {where}:{port}"
    return None


def free_port(address):
    """A TCP port nothing listens on at address (the kernel's pick: bound to port 0, then released)."""
    try:
        family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
    except ValueError:
        family = socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        try:
            s.bind((address, 0))
        except OSError as e:
            raise Error(f"no free VNC port on {address}: {e.strerror}") from None
        return s.getsockname()[1]


def proc_times(pid):
    """(utime+stime in clock ticks, start time in ticks since boot), or None."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def proc_env(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            items = f.read().split(b"\0")
    except OSError:
        return {}
    return dict(i.decode(errors="replace").split("=", 1) for i in items if b"=" in i)


def catches_signal(pid, signum, proc_root="/proc"):
    """Whether process pid has a handler for signal signum (its SigCgt mask): one it is safe to
    send a signal whose default action would end it."""
    try:
        with open(os.path.join(proc_root, str(pid), "status")) as f:
            for line in f:
                if line.startswith("SigCgt:"):
                    return bool(int(line.split()[1], 16) >> (signum - 1) & 1)
    except (OSError, ValueError, IndexError):
        pass
    return False


def uptime_of(pid):
    t = proc_times(pid)
    if t is None:
        return None
    try:
        with open("/proc/uptime") as f:
            now = float(f.read().split()[0])
    except OSError:
        return None
    return max(0.0, now - t[1] / os.sysconf("SC_CLK_TCK"))


def child_exited(pid):
    """Whether this process's child pid has ended (reaping it). A pid that is not our child: whether
    it is gone."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return not proc_alive(pid)
    return done == pid


def reap(pid):
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def run_streamed(cmd, out=None, cwd=None, env=None):
    """Run cmd in a session of its own (a VM it starts outlives us, and a terminal's Ctrl-C does
    not reach it half-way), passing each output line to out (default: stdout)."""
    out = out or (lambda line: print(line, flush=True))
    p = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        for raw in p.stdout:
            out(raw.decode(errors="replace").rstrip("\n"))
    finally:
        p.stdout.close()
        rc = p.wait()
    return rc
