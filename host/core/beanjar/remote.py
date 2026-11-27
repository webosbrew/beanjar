# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Other machines' VMs: the CLI runs the remote machine's own `beanjar ... --json` over plain SSH
(host/core/README.md, "Several machines"; docs/dev/architecture/manager.md, "Several machines").

No server, no daemon and no protocol of ours: a host is a name and an ssh:// URI in this machine's
config.toml ([hosts.NAME]); a call is the system `ssh` running `beanjar COMMAND ...` there, its
stdout parsed here, its stderr passed on. The one function that touches the network is
`transport`; the tests replace it."""

import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from typing import NamedTuple

from . import base, hostconfig
from .base import ARES_QUERY, LOCAL, split_spec  # noqa: F401 (remote's names for them)
from .base import Error

HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_USER = r"[A-Za-z0-9_][A-Za-z0-9._-]*"
_HOST = r"[A-Za-z0-9][A-Za-z0-9._-]*|\[[0-9A-Fa-f:.]+\]"
URI_RE = re.compile(rf"ssh://(?:(?P<user>{_USER})@)?(?P<host>{_HOST})(?::(?P<port>[0-9]{{1,5}}))?")

CONNECT_TIMEOUT = 5  # seconds to reach a host
# a host gone silent (down, its sshd stuck): the connection, a shared master included, ends after
# ALIVE_INTERVAL * ALIVE_COUNT seconds without an answer, so no call waits on it forever
ALIVE_INTERVAL = 5
ALIVE_COUNT = 3
VERSION_TIMEOUT = 10  # seconds the version probe of a forwarded command may take (identity)
PERSIST = 60  # seconds a shared connection stays open after its last use
LIST_TIMEOUT = 30  # seconds a remote list (or a name probe) may take: then the host is left out
REMOTE_PROGRAM = "beanjar"


class HostError(Error):
    """A host that could not be asked, or answered wrongly: not a VM's error."""

    def __init__(self, host, reason):
        super().__init__(f"host {host}: {reason}")
        self.host = host
        self.reason = reason


class Target(NamedTuple):
    user: str | None
    host: str
    port: int | None


def parse_uri(uri):
    """ssh://[user@]host[:port] as a Target; host may be an ~/.ssh/config alias or [IPv6]."""
    m = URI_RE.fullmatch(uri or "")
    if not m:
        raise Error(
            f"invalid host URI {uri!r}: ssh://[user@]host[:port] (host may be an alias from "
            "~/.ssh/config)"
        )
    port = int(m["port"]) if m["port"] else None
    if port is not None and not 1 <= port <= 65535:
        raise Error(f"invalid host URI {uri!r}: port {port} is not 1 to 65535")
    host = m["host"]
    if host.startswith("["):
        host = host[1:-1]
    return Target(m["user"], host, port)


def check_name(name):
    if name == LOCAL:
        raise Error(f"{LOCAL!r} is this machine: pick another host name")
    if not HOST_RE.fullmatch(name or ""):
        raise Error(
            f"invalid host name {name!r} (1 to 32 letters, digits, '_' and '-', starting with a "
            "letter or digit)"
        )


def hosts():
    """The configured hosts, {name: uri} in file order, each checked."""
    table = hostconfig.global_config().get("hosts", {})
    path = hostconfig.global_config_path()
    if not isinstance(table, dict):
        raise Error(f"{path}: hosts must be a table of [hosts.NAME] tables")
    found = {}
    for name, entry in table.items():
        try:
            check_name(name)
            if not isinstance(entry, dict) or not isinstance(entry.get("uri"), str):
                raise Error('it needs a uri = "ssh://..."')
            parse_uri(entry["uri"])
        except Error as e:
            raise Error(f"{path}: [hosts.{name}]: {e}") from None
        found[name] = entry["uri"]
    return found


def add_host(name, uri):
    check_name(name)
    parse_uri(uri)
    cfg = base.read_toml(hostconfig.global_config_path(), {})
    table = cfg.setdefault("hosts", {})
    if name in table:
        raise Error(
            f"host {name} exists ({table[name].get('uri')}): beanjar host remove {name} first"
        )
    table[name] = {"uri": uri}
    hostconfig.write_global_config(cfg)


def remove_host(name):
    cfg = base.read_toml(hostconfig.global_config_path(), {})
    table = cfg.get("hosts", {})
    if name not in table:
        raise Error(f"no host named {name} (beanjar host list)")
    del table[name]
    if not table:
        cfg.pop("hosts")
    hostconfig.write_global_config(cfg)


def control_dir():
    """The private directory of the shared connections' sockets: under $XDG_RUNTIME_DIR, else
    one of ours in the temporary directory."""
    run = os.environ.get("XDG_RUNTIME_DIR")
    if run and os.path.isdir(run):
        path = os.path.join(run, "beanjar-ssh")
    else:
        path = os.path.join(tempfile.gettempdir(), f"beanjar-ssh-{os.getuid()}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.stat(path)
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Error(f"{path} is not private to you: remove it")
    return path


def ssh_argv(uri, remote_argv, control=None, tty=False):
    """The ssh command line that runs `beanjar REMOTE_ARGV` on the host of uri (shell_argv)."""
    return shell_argv(uri, shlex.join([REMOTE_PROGRAM, *remote_argv]), control, tty)


def shell_argv(uri, command, control=None, tty=False):
    """The ssh command line that runs the shell command on the host of uri: no prompts, a
    connect timeout, and one connection shared by the calls of the next PERSIST seconds. No
    terminal (-T), or with tty one (-t: an interactive tool on this side's terminal)."""
    t = parse_uri(uri)
    argv = [
        "ssh",
        "-t" if tty else "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o",
        f"ServerAliveInterval={ALIVE_INTERVAL}",
        "-o",
        f"ServerAliveCountMax={ALIVE_COUNT}",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPersist={PERSIST}",
        "-o",
        f"ControlPath={control or control_dir()}/%C",
    ]
    if t.port:
        argv += ["-p", str(t.port)]
    if t.user:
        argv += ["-l", t.user]
    return argv + ["--", t.host, command]


def control_argv(uri, op, *args, control=None):
    """The ssh command line that asks the shared connection to the host of uri (shell_argv's) to
    op: check, forward or cancel (args: -L SPEC)."""
    t = parse_uri(uri)
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ControlPath={control or control_dir()}/%C",
        "-O",
        op,
        *args,
    ]
    if t.port:
        argv += ["-p", str(t.port)]
    if t.user:
        argv += ["-l", t.user]
    return argv + ["--", t.host]


def control(uri, op, *args):
    """Runs control_argv: (status, stderr text). It talks to the local master alone, so it is
    quick; in a session of its own, so that the terminal's Ctrl-C does not cut it short."""
    try:
        p = subprocess.run(
            control_argv(uri, op, *args),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CONNECT_TIMEOUT,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return 255, f"ssh -O {op}: timed out after {CONNECT_TIMEOUT} s"
    return p.returncode, p.stderr


def ensure_master(name, uri):
    """The shared connection to host name, started if it is not running (a command run over it,
    which leaves it for PERSIST seconds)."""
    if control(uri, "check")[0] != 0:
        shell(name, uri, "true", timeout=LIST_TIMEOUT)


def transport(argv, stdin=None, on_stderr=None, timeout=None):
    """Runs argv: (status, stdout bytes, stderr text). stdin (bytes) is written to it; on_stderr,
    if given, is called with each stderr line as it comes. A status from a signal is negative."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lines = []

    def pump():
        for raw in proc.stderr:
            line = raw.decode(errors="replace")
            lines.append(line)
            if on_stderr:
                on_stderr(line.rstrip("\n"))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    fired = []
    timer = threading.Timer(timeout, lambda: (fired.append(1), proc.kill())) if timeout else None
    if timer:
        timer.start()
    try:
        if stdin is not None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        out = proc.stdout.read()
        rc = proc.wait()
    finally:
        if timer:
            timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    reader.join()
    if fired:
        lines.append(f"timed out after {timeout} s\n")
    return rc, out, "".join(lines)


def remote_error(text):
    """The message in a beanjar's stderr: its JSON {"error"} object, else its text lines."""
    messages = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "error" in obj:
                messages.append(str(obj["error"]))
                continue
        except ValueError:
            pass
        messages.append(line.removeprefix("beanjar: "))
    return "; ".join(messages)


def call(name, uri, argv, stdin=None, on_stderr=None, timeout=None):
    """Runs `beanjar ARGV` on host name (uri): (status, stdout bytes, stderr text) of the remote
    beanjar. A host that cannot be reached, or has no beanjar, is a HostError."""
    rc, out, err = transport(ssh_argv(uri, argv), stdin, on_stderr, timeout)
    if rc == 255:
        seen = on_stderr is not None and err.strip()
        raise HostError(
            name,
            "ssh failed (see above)"
            if seen
            else (err.strip().removeprefix("ssh: ") or "ssh failed"),
        )
    if rc == 127:
        raise HostError(
            name,
            "no beanjar command there: install beanjar on it (and have it on the PATH of a "
            "non-interactive ssh login)",
        )
    if rc < 0:
        raise HostError(name, remote_error(err) or f"the ssh was killed (signal {-rc})")
    return rc, out, err


def shell(name, uri, command, stdin=None, timeout=None):
    """The stdout of a shell command run on host name (a helper step: a temporary directory, a
    copy); its failure, or the host's, is a HostError."""
    rc, out, err = transport(shell_argv(uri, command), stdin, None, timeout)
    if rc:
        reason = err.strip().removeprefix("ssh: ") or f"exited with status {rc}"
        raise HostError(name, reason if rc == 255 else f"{command.split()[0]}: {reason}")
    return out


def attach(name, uri, argv, tty=False):
    """Runs `beanjar ARGV` on host name with this process's stdin, stdout and stderr (a terminal
    with tty): its exit status, as ssh gives it."""
    return subprocess.call(ssh_argv(uri, argv, tty=tty))


def own(vm):
    """VM as a host's beanjar must be told it: local/VM, its own VM. A bare name there would also
    be looked for on that host's own hosts, and found there it would be another machine's."""
    return f"{LOCAL}/{vm}"


def call_json(name, uri, argv, timeout=LIST_TIMEOUT):
    """The JSON a successful `beanjar ARGV --json` on the host prints. Anything else is a
    HostError: a failed command (a usage error: an older beanjar, to update), output that is not
    JSON."""
    rc, out, err = call(name, uri, [*argv, "--json"], timeout=timeout)
    if rc == 2:
        reason = remote_error(err) or "usage error"
        raise HostError(name, f"{reason} (is it an older beanjar? update it on the host)")
    if rc != 0:
        raise HostError(name, remote_error(err) or f"beanjar exited with status {rc}")
    try:
        return json.loads(out)
    except ValueError:
        raise HostError(
            name, "its beanjar printed no JSON (is it an older beanjar? update it on the host)"
        ) from None


def identity(name, uri):
    """Host name's `beanjar --version --json` object (commands.common.version_info; an older beanjar
    gives {"name", "version"} only), or None when it cannot be asked or answers with no object.
    Kept for PERSIST seconds in control_dir(), so that a run of commands asks once; a host that
    does not answer within VERSION_TIMEOUT is kept as None too, so that it delays one command a
    minute at most."""
    path = os.path.join(control_dir(), f"version-{name}.json")
    try:
        if time.time() - os.stat(path).st_mtime < PERSIST:
            with open(path) as f:
                cached = json.load(f)
            if isinstance(cached, dict) and cached.get("uri") == uri:
                return cached.get("info")
    except (OSError, ValueError):
        pass
    try:
        info = call_json(name, uri, ["--version"], timeout=VERSION_TIMEOUT)
    except Error:
        info = None
    if not isinstance(info, dict):
        info = None
    try:
        with open(path + ".tmp", "w") as f:
            json.dump({"uri": uri, "info": info}, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass  # a cache only
    return info


def list_rows(name, uri, show_all=False):
    """The VMs of host name, as its `beanjar list --local --json` rows, each with host = name: its
    own VMs, not those of the hosts it has itself (this machine may have them as its hosts)."""
    data = call_json(name, uri, ["list", "--local", *(["--all"] if show_all else [])])
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise HostError(name, "its beanjar list is not an array of VMs")
    return [dict(r, host=name) for r in data]


def has_vm(name, uri, vm):
    """Whether host name has a VM called vm, of its own (read with `config show local/VM`, which
    changes nothing)."""
    rc, _out, err = call(name, uri, ["config", "show", own(vm), "--json"], timeout=LIST_TIMEOUT)
    if rc == 0:
        return True
    reason = remote_error(err)
    if rc == 1 and reason.startswith("no VM named"):
        return False
    raise HostError(name, reason or f"beanjar exited with status {rc}")


def locate(spec, local_has, warn, pinned=False):
    """Where the VM called spec (NAME or HOST/NAME) is: (host, name), host None for this machine.

    local/NAME and HOST/NAME say where. A bare NAME is looked for on this machine and on every
    configured host, so that a duplicate is never hidden: found in one place, it is there; in
    several, an error naming the HOST/NAME forms; nowhere, the usual "no VM named". A host that
    cannot be asked is warned about (warn(text)) and left out. local_has(name) is whether this
    machine's library has the VM. With no hosts configured nothing is asked. pinned is for a command
    line that names its library (--library): a bare NAME is then that library's VM and nobody is
    asked, as for local/NAME."""
    host, name = split_spec(spec)
    if host is not None:
        if not base.NAME_RE.fullmatch(name):
            raise Error(f"invalid VM name {name!r} (letters, digits, '.', '_', '-')")
        if host == LOCAL:
            return None, name
        if host not in hosts():
            raise Error(f"no host named {host} (beanjar host list)")
        return host, name
    configured = {} if pinned else hosts()
    if not configured or not base.NAME_RE.fullmatch(name):
        return None, name  # a name the library refuses says so itself
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(len(configured)) as pool:
        futures = {h: pool.submit(has_vm, h, uri, name) for h, uri in configured.items()}
        where = [LOCAL] if local_has(name) else []
        unasked = []
        for h, future in futures.items():
            try:
                if future.result():
                    where.append(h)
            except HostError as e:
                unasked.append(h)
                warn(str(e))
    if len(where) > 1:
        forms = ", ".join(f"{h}/{name}" for h in where)
        raise Error(f"VM {name} is on {' and '.join(where)}: say which one: {forms}")
    if not where:
        raise Error(
            f"no VM named {name}" + (f" (could not ask {', '.join(unasked)})" if unasked else "")
        )
    return (None if where[0] == LOCAL else where[0]), name


def rewrite(argv, nwords, old, new):
    """The remote beanjar's arguments from this command line argv: without the global options
    (--library and --repo name this machine's paths), the first word after the command's nwords
    command words that is old, the VM as typed, replaced by local/new, the VM as that host's own."""
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 1 if "=" in argv[i] else 2
    rest = list(argv[i:])
    for j in range(nwords, len(rest)):
        if rest[j] == old:
            rest[j] = own(new)
            break
    return rest
