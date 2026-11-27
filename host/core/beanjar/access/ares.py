# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""`beanjar ares-TOOL [--root] -d NAME [ARGS...]`: a webOS SDK tool run against one VM
(host/core/README.md, "ares tools").

The tool is either implementation, the Rust ares-cli (webosbrew/ares-cli-rs) or the official
Node.js one (webos-tools/cli), unchanged, always on this machine. It runs with HOME set to a
private directory that holds a device list naming only this VM, so it sees one device, the default
one:

- `.webos/ose/novacom-devices.json`: the Rust tools' list ($HOME/.webos/ose);
- `.webos/tv/novacom-devices.json`: the official tools' ($APPDATA or $HOME, then the dataDir of
  their config.json, .webos/tv; APPDATA is unset in the child so it cannot point elsewhere);
- `.ssh/devmode_rsa`: a copy of the VM's developer key, which both resolve
  `privateKey: {"openSsh": NAME}` against ($HOME/.ssh/NAME).

The device is the developer-mode user prisoner on port 9922. With `--root` it is root on the VM's
root SSH instead (guest 22, forwarded by a layer whose layer.toml forwards it, rootssh or
hbchannel, on SSH_PORT or 6622), with `.ssh/id_ed25519`, a copy of the VM's client key; a local
VM's HOME for that is run/ares-home-root/, so that runs of both kinds never share a device list.

Beanjar takes `-d NAME` (`--device`) and `--root` itself and does not pass them on; ARES_DEVICE, which the Rust
tools read as -d, is unset in the child too. Neither implementation checks the device's host key
(the Rust one gives libssh /dev/null as known_hosts, the official one sets no host verifier), so
none is pinned here.

A local VM's HOME is run/ares-home/ of the VM, its device at the VM's address, and the tool is
exec'd. A VM on another host (remote.py) is reached through an ssh port forward: the host's
`beanjar _ares local/NAME [--root] --json` (target) gives its state, layers, address, the port to
forward to and its key, over the host's shared connection (remote.py's ControlMaster), to which a
forward from 127.0.0.1:PORT here to the VM's developer-mode port there (with --root its root SSH
port, and the client key) is added (ssh -O forward; the connection is started first if it is not
running); the device list names 127.0.0.1:PORT, and the tool runs here as a child. Its arguments
are this machine's paths as they are. The forward is cancelled (ssh -O cancel) and the temporary
HOME removed when the command ends, on every exit path (an error, Ctrl-C, SIGTERM or SIGHUP). When
beanjar is killed outright (SIGKILL) both are left: the forward stays in the master, listening
here, until the next remote run cancels it (sweep_homes, from the record in the HOME it removes)
or the master exits, PERSIST seconds after its last use, which any other call to the host
postpones."""

import fcntl
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import termios

from .. import base
from ..base import Error
from ..guestside import layermeta
from ..vm import keys, settings
from . import remote

TOOL_RE = re.compile(r"ares-[a-z-]+")
HOME_DIR = "ares-home"  # under the VM's run directory
ROOT_HOME_DIR = "ares-home-root"  # the same for --root
KEY_NAME = keys.DEVMODE  # the key's name in the device lists and in .ssh
ROOT_KEY_NAME = keys.CLIENT  # the same for --root
ROOT_OPTION = base.ROOT_OPTION
PROFILES = ("ose", "tv")  # the Rust tools' list, the official tools' (their config.json profile)
DEVICE_OPTIONS = ("-d", "--device")
FORWARD_HOST = "127.0.0.1"  # where a remote VM's forward listens here
FORWARD_TIMEOUT = 30  # seconds the forward may take to be up
FORWARD_TRIES = 3  # ports tried (a free port may be taken before ssh binds it)


def device_entry(name, host, port, profile, root=False):
    """The one device of a device list: the VM in developer mode (prisoner), or root with root,
    at host:port. Valid for both implementations: the Rust serde type needs profile, name, host,
    port and username; the official schema (files/schema/NovacomDevices.schema) takes these and
    more, and its profile must be its config.json's."""
    return {
        "profile": profile,
        "name": name,
        "default": True,
        "description": f"Beanjar VM {name}",
        "host": host,
        "port": int(port),
        "username": "root" if root else "prisoner",
        "privateKey": {"openSsh": ROOT_KEY_NAME if root else KEY_NAME},
        "passphrase": "",  # devmode_rsa and id_ed25519 are unencrypted
        "files": "sftp",
    }


def write_private(path, data):
    """data in path, owner-only, replaced whole (a concurrent run never reads half a file)."""
    part = f"{path}.{os.getpid()}"
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(data)
    os.replace(part, path)


def private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def write_home(home, name, host, port, key, root=False):
    """The tools' HOME in home, written now: the device lists naming VM name at host:port, and
    key (bytes) as its developer key (with root, root and its client key)."""
    private_dir(home)
    for sub in (".webos", ".ssh"):
        private_dir(os.path.join(home, sub))
    for profile in PROFILES:
        d = os.path.join(home, ".webos", profile)
        private_dir(d)
        data = json.dumps([device_entry(name, host, port, profile, root)], indent=4) + "\n"
        write_private(os.path.join(d, "novacom-devices.json"), data.encode())
    write_private(os.path.join(home, ".ssh", ROOT_KEY_NAME if root else KEY_NAME), key)
    return home


def prepare_home(vm, root=False):
    """A local VM's private HOME for the tools, written now (cheap, and always the VM's current
    address, port and key): its path."""
    with open(os.path.join(vm.identity_dir, ROOT_KEY_NAME if root else KEY_NAME), "rb") as f:
        key = f.read()
    home = os.path.join(vm.run_dir, ROOT_HOME_DIR if root else HOME_DIR)
    port = layermeta.root_port(vm.config.get("layers"), vm.config.get("SSH_PORT")) if root else None
    return write_home(home, vm.name, vm.service_host(), port or keys.DEVMODE_PORT, key, root)


def root_refusal(name, layers):
    """The Error for --root on a VM without root SSH: which layers to add, and how."""
    meta = layermeta.load()
    choices = []
    for n in layermeta.root_layers(meta):
        needs = [r for r in meta[n].requires if r not in (layers or [])]
        choices.append(f"{n} (with {', '.join(needs)})" if needs else n)
    wanted = ",".join(dict.fromkeys([*(layers or []), "rootssh"]))
    return Error(
        f"VM {name} has no root SSH: --root needs a layer that forwards the guest's port 22, "
        f"{' or '.join(choices)}. Stop the VM, add one, e.g. "
        f"`beanjar config set {name} layers={wanted}`, and start it again"
    )


def child_env(home, environ=None):
    """The tool's environment: this one with HOME the VM's, APPDATA and ARES_DEVICE unset."""
    env = dict(os.environ if environ is None else environ)
    env["HOME"] = home
    for k in ("APPDATA", "ARES_DEVICE"):
        env.pop(k, None)
    return env


def find_tool(tool, environ=None):
    """The tool's path: on PATH, else in ~/.cargo/bin (the Rust tools' `cargo install`)."""
    environ = os.environ if environ is None else environ
    found = shutil.which(tool, path=environ.get("PATH", os.defpath))
    if found:
        return found
    cargo = os.path.join(os.path.expanduser("~"), ".cargo", "bin", tool)
    if os.path.isfile(cargo) and os.access(cargo, os.X_OK):
        return cargo
    raise Error(
        f"{tool}: not found on PATH or in ~/.cargo/bin (install the Rust ares-cli or the webOS "
        "SDK's CLI)"
    )


def check(name, layers, running, address, has_key, root=False, port=None):
    """Refuses a VM the tools cannot reach: without developer mode (with root, without root SSH
    on port), not running, no address, no developer key (with root, no client key)."""
    if root:
        if port is None:
            raise root_refusal(name, layers)
    elif "devmode" not in (layers or []):
        wanted = ",".join([*(layers or []), "devmode"])
        raise Error(
            f"VM {name} has no devmode layer: the ares tools reach a TV in developer mode. "
            f"Stop the VM, add it with `beanjar config set {name} layers={wanted}`, and start "
            "it again"
        )
    if not running:
        raise Error(f"VM {name} is not running")
    if address is None:
        raise Error(f"VM {name} has no address")
    if not has_key:
        if root:
            raise Error(f"VM {name} has no client key ({keys.CLIENT}): start it to make one")
        raise Error(f"VM {name} has no developer key ({keys.DEVMODE}): start it to make one")


def lan_direct_refusal(name, spec):
    """The Error for a local ares-TOOL on a VM with link lan and mode direct: this host cannot
    reach its own macvlan children."""
    return Error(
        f"VM {name} is link=lan with mode=direct: the guest is on the LAN itself, where this host "
        f"cannot reach it (a macvlan's parent never reaches its children). Run the tool on another "
        f"machine of the LAN: beanjar ares-TOOL -d {spec} there, or the SDK's own tools at its LAN "
        "address"
    )


def check_vm(vm, root=False):
    if settings.lan_direct(vm.config.get("link"), vm.config.get("mode")):
        raise lan_direct_refusal(vm.name, f"HOST/{vm.name}")
    layers = vm.config.get("layers")
    check(
        vm.name,
        layers,
        vm.pid() is not None,
        vm.service_host(),
        os.path.isfile(os.path.join(vm.identity_dir, ROOT_KEY_NAME if root else KEY_NAME)),
        root,
        layermeta.root_port(layers, vm.config.get("SSH_PORT")) if root else None,
    )


def split_device(args):
    """(the VM as -d NAME names it, whether --root is given, the tool's arguments without
    either). The forms -d NAME, --device NAME, -d=NAME and --device=NAME, once, and --root,
    before any `--`; None if absent."""
    device, root, rest, i = None, False, [], 0
    while i < len(args):
        word = args[i]
        if word == "--":
            rest += args[i:]
            break
        if word == ROOT_OPTION:
            root = True
            i += 1
            continue
        opt, eq, value = word.partition("=")
        if opt in DEVICE_OPTIONS:
            if device is not None:
                raise base.Error("give -d NAME once")
            if not eq:
                if i + 1 >= len(args):
                    raise base.Error(f"{word} needs a VM name")
                value = args[i + 1]
                i += 1
            if not value:
                raise base.Error(f"{word} needs a VM name")
            device = value
        else:
            rest.append(word)
        i += 1
    return device, root, rest


def stdin_empty(fd=0):
    """Whether fd has nothing to forward and never will: /dev/null, or a pipe or socket at end of
    file (its writer is gone and nothing is queued). A terminal, a file, or a pipe that is still
    open (data may come) is not."""
    try:
        st = os.fstat(fd)
    except OSError:
        return True  # closed
    if stat.S_ISCHR(st.st_mode):
        try:
            return os.path.samestat(st, os.stat(os.devnull))
        except OSError:
            return False
    if not (stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode)):
        return False
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    events = poller.poll(0)
    if not events:
        return False  # open, nothing yet
    queued = struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
    return queued == 0


# The guest's SSH server (dropbear) refuses a command line of this many bytes or more and drops the
# connection, which the tool reports as "Socket error: disconnected" (8972 bytes work, 8992
# fail, with this process's "exec </dev/null; " prefix counted).
MAX_COMMAND = 9000


def check_command_length(command):
    """Raises the Error for an `ares-shell -r` command too long for the guest's SSH server."""
    size = len(command.encode())
    if size >= MAX_COMMAND:
        raise Error(
            f"ares-shell -r: the command is {size} bytes, and the guest's SSH server (dropbear) "
            f"drops a command of {MAX_COMMAND} bytes or more; copy the file or script into the "
            f"guest with ares-push and run it there"
        )


def detach_stdin(tool, args, empty=None):
    """args of `ares-shell -r CMD` with CMD given /dev/null as its stdin when this process has no
    stdin to forward. The tool forwards an ended stdin as EOF, so the remote command's stdin
    hangs up at once, and a program that watches it (luna-send exits silently when it hangs up,
    before its reply) misbehaves. Other tools, the interactive shell, and a stdin that has data
    or may still get some are unchanged."""
    if tool != "ares-shell":
        return args
    for i, word in enumerate(args):
        if word in ("-r", "--run") and i + 1 < len(args):
            at, command, prefix = i + 1, args[i + 1], ""
        elif word.startswith("--run="):
            at, command, prefix = i, word[6:], "--run="
        else:
            continue
        if empty is None:
            empty = stdin_empty()
        if empty:
            command = f"exec </dev/null; {command}"
        check_command_length(command)
        if not empty:
            return args
        out = list(args)
        out[at] = f"{prefix}{command}"
        return out
    return args


def run_local(lib, name, tool, args, root=False):
    """Runs tool against VM name of lib (as root with root) in place of this process (exec: the
    terminal, the streams and the exit status are the tool's)."""
    vm = lib.vm(name)
    check_vm(vm, root)
    path = find_tool(tool)
    args = detach_stdin(tool, args)
    env = child_env(prepare_home(vm, root))
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execve(path, [tool, *args], env)
    except OSError as e:
        raise Error(f"{path}: {e.strerror}") from None


# -- a remote VM --


def target(vm, root=False):
    """base.target of VM vm: what another machine's ares-TOOL needs of it (this beanjar's answer to
    remote.ARES_QUERY, which remote_vm reads; cli.ares_query gives it without this module)."""
    return base.target(vm.name, vm.dir, vm.config, root)


def remote_vm(host, uri, name, root=False):
    """(its layers, whether it runs, its address on host, its key as bytes or None, the port to
    forward to or None, its own LAN address with link lan and mode direct: "" while unknown, None
    for any other) of VM name on host: target there, one call over the host's shared connection.
    A host whose beanjar predates the query is told to update it."""
    words = [remote.ARES_QUERY, remote.own(name), *([ROOT_OPTION] if root else []), "--json"]
    rc, out, err = remote.call(host, uri, words, timeout=remote.LIST_TIMEOUT)
    if rc == 2:
        raise remote.HostError(
            host,
            f"its beanjar has no `{remote.ARES_QUERY}` query, which ares-TOOL -d {host}/NAME "
            f"needs: update beanjar on {host}",
        )
    if rc:
        raise remote.HostError(host, remote.remote_error(err) or f"beanjar exited with status {rc}")
    try:
        got = json.loads(out)
        key, port = got["key"], got["port"]
        lan = got.get("lan")
        if (
            not (key is None or isinstance(key, str))
            or not (port is None or isinstance(port, int))
            or not (lan is None or isinstance(lan, str))
        ):
            raise TypeError
        return got["layers"], bool(got["running"]), got["address"], key and key.encode(), port, lan
    except (ValueError, KeyError, TypeError):
        raise remote.HostError(
            host,
            f"its beanjar's `{remote.ARES_QUERY}` answer is not what this one reads: update it",
        ) from None


def free_port():
    """A port of FORWARD_HOST that nothing listens on now."""
    with socket.socket() as s:
        s.bind((FORWARD_HOST, 0))
        return s.getsockname()[1]


FORWARD_RECORD = ".forward"  # in a remote run's HOME: what to cancel should beanjar die (SIGKILL)
CLEANUP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def cancel(uri, spec):
    """Cancels the forward spec of the shared connection to the host of uri, the signals that end
    a run held off meanwhile (a second Ctrl-C does not leave it half done). A connection that is
    gone has no forward left to cancel: failures are not reported."""
    held = signal.pthread_sigmask(signal.SIG_BLOCK, CLEANUP_SIGNALS)
    try:
        remote.control(uri, "cancel", "-L", spec)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)


class Forward:
    """`with Forward(host, uri, address, to_port, record) as port`: FORWARD_HOST:port here reaches
    address:to_port on host until the block ends (with to_port None, the unix socket address). The forward is added to the host's shared
    connection (remote.py; started first if it is not running: ssh -O forward) and cancelled at
    the end (ssh -O cancel). record, a file, names it first, for sweep_homes to cancel it should
    beanjar be killed outright."""

    def __init__(self, host, uri, address, to_port, record=None):
        self.host, self.uri, self.address, self.to_port = host, uri, address, to_port
        self.record = record
        self.spec = None

    def __enter__(self):
        reason = None
        for _ in range(FORWARD_TRIES):  # a free port may be taken before the master binds it
            port = free_port()
            spec = f"{FORWARD_HOST}:{port}:{self.target()}"
            if self.record:
                write_private(self.record, json.dumps({"uri": self.uri, "spec": spec}).encode())
            remote.ensure_master(self.host, self.uri)
            self.spec = spec  # cancelled even if the request is cut short
            try:
                rc, err = remote.control(self.uri, "forward", "-L", spec)
            except BaseException:
                self.__exit__()
                raise
            if rc == 0:
                return port
            self.spec = None
            reason = "; ".join(line.strip() for line in err.splitlines() if line.strip())
        raise remote.HostError(
            self.host,
            f"forward to {self.target()}: {reason or 'ssh -O forward failed'}",
        )

    def target(self):
        return self.address if self.to_port is None else f"{self.address}:{self.to_port}"

    def restore(self):
        """The same forward again, after the shared connection that had it ended (the master
        started again first): whether it is back (False: its port is taken here now)."""
        if self.spec is None:
            return False
        remote.ensure_master(self.host, self.uri)
        return remote.control(self.uri, "forward", "-L", self.spec)[0] == 0

    def __exit__(self, *_exc):
        spec, self.spec = self.spec, None
        if spec:
            cancel(self.uri, spec)


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


class Terminated(BaseException):
    """SIGTERM or SIGHUP while a remote VM's tool runs: the cleanup runs, then status 128+N."""

    def __init__(self, signum):
        super().__init__(signum)
        self.status = 128 + signum


def raise_terminated(signum, _frame):
    raise Terminated(signum)


def run_child(path, tool, args, env):
    """Runs tool as a child: its exit status (128+N for its signal N). While it runs, Ctrl-C is
    the tool's alone (it gets it from the terminal; this process waits for it to end)."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        proc = subprocess.Popen([tool, *args], executable=path, env=env)
    except OSError as e:
        raise Error(f"{path}: {e.strerror}") from None
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        rc = proc.wait()
    except BaseException:  # Terminated: the tool too
        stop(proc)
        raise
    finally:
        signal.signal(signal.SIGINT, old)
    return 128 - rc if rc < 0 else rc


def home_parent():
    """Where a remote VM's temporary HOME goes: $XDG_RUNTIME_DIR if there is one (this user's
    alone), else the temporary directory."""
    run = os.environ.get("XDG_RUNTIME_DIR")
    return run if run and os.path.isdir(run) else None


HOME_PREFIX = "beanjar-ares-"  # a remote VM's temporary HOME: beanjar-ares-PID-RANDOM
# the directories that record a forward: these HOMEs, `mitm connect`'s (inspector.py), `usb
# plug`'s (usb.py), a remote VM's tuner feed's (antenna.py) and `shell`'s (guestshell.py)
RECORD_PREFIXES = (HOME_PREFIX, "beanjar-mitm-", "beanjar-usb-", "beanjar-tuner-", "beanjar-shell-")


def sweep_homes(parent):
    """Removes the temporary HOMEs of runs that died without cleaning up (SIGKILL), and the record
    directories of `mitm connect`s that did: those whose PID is gone. A HOME holds a developer key,
    and the forward either left in the shared connection is cancelled."""
    parent = parent or tempfile.gettempdir()
    for entry in os.listdir(parent):
        prefix = next((p for p in RECORD_PREFIXES if entry.startswith(p)), None)
        pid = entry.removeprefix(prefix or "").split("-")[0]
        if not prefix or not pid.isdigit():
            continue
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            home = os.path.join(parent, entry)
            try:
                with open(os.path.join(home, FORWARD_RECORD)) as f:
                    left = json.load(f)
                cancel(left["uri"], left["spec"])
            except (OSError, ValueError, KeyError, TypeError):
                pass  # none recorded, or not readable: nothing to cancel
            shutil.rmtree(home, ignore_errors=True)
        except OSError:
            pass  # someone else's process: alive


def run_remote(host, name, tool, args, root=False):
    """Runs tool here against VM name on host, through a forward of its developer-mode port (with
    root, of its root SSH port): the tool's exit status."""
    uri = remote.hosts()[host]
    layers, running, address, key, port, lan = remote_vm(host, uri, name, root)
    check(name, layers, running, address, key is not None, root, port)
    if lan == "":
        raise Error(
            f"VM {name} is link=lan with mode=direct and its LAN address is not known yet (its "
            "DHCP lease): try again once its network is up"
        )
    path = find_tool(tool)
    args = detach_stdin(tool, args)
    old = {s: signal.signal(s, raise_terminated) for s in (signal.SIGTERM, signal.SIGHUP)}
    home = None
    try:
        sweep_homes(home_parent())
        home = tempfile.mkdtemp(prefix=f"{HOME_PREFIX}{os.getpid()}-", dir=home_parent())
        if lan:
            # link=lan with mode=direct: the guest itself, at its LAN address, without a forward
            write_home(home, name, lan, port, key, root)
            return run_child(path, tool, args, child_env(home))
        with Forward(host, uri, address, port, os.path.join(home, FORWARD_RECORD)) as here:
            write_home(home, name, FORWARD_HOST, here, key, root)
            return run_child(path, tool, args, child_env(home))
    except Terminated as e:
        return e.status
    finally:
        if home:
            shutil.rmtree(home, ignore_errors=True)
        for s, handler in old.items():
            signal.signal(s, handler)
