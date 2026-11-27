# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The manager's light base: what a short-lived query needs without the library module (core.py),
which imports most of the manager. The `_ares` query another machine's remote ares-TOOL asks at
every run (cli.ares_query, ares.remote_vm) is answered from here, and on a board every module
imported costs tens of milliseconds, so this module imports only os and re (and ipaddress for a VM
with listen).

Each fact here has this one definition, which core, keys, remote and ares import: the errors,
where the library and a VM's files are, a VM's run state (its QEMU's pid), its host address and
the address this host reaches its TV-service ports on, and the query's answer (target)."""

import os
import re

# host/core, the directory that holds this package (beanjar/): found here once, so that a module
# moving between subpackages cannot change where it looks.
CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Error(Exception):
    """A refusal or failure to report to the user as it is (core.Error). key: the vm.toml key (or
    "name") it is about, if it is about one setting (the GUI shows which of its pages has it)."""

    def __init__(self, message, key=None):
        super().__init__(message)
        self.key = key


class KeysError(Exception):
    """keys.Error: a VM's keys, ports or addresses (keys.py), the address checks here among them."""


# -- the library and a VM's files --

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
VMS_DIR = "vms"  # <library>/vms/<name>: a VM's folder
CONFIG = "vm.toml"  # in a VM's folder
RUN_DIR = "run"  # in a VM's folder: its run state
PIDFILE = "qemu.pid"  # in run/: QEMU's pid
UNIT_FILE = "unit"  # in run/: the unit it was last started as


def default_library():
    return os.environ.get("BEANJAR_LIBRARY") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "beanjar"
    )


def library_root(root=None):
    """The library's directory: root (--library), else the default one."""
    return os.path.abspath(root or default_library())


def vm_dir(root, name):
    """The folder of VM name of the library at root; Error for an invalid name or no such VM."""
    if not NAME_RE.fullmatch(name or ""):
        raise Error(f"invalid VM name {name!r} (letters, digits, '.', '_', '-')")
    d = os.path.join(root, VMS_DIR, name)
    if not os.path.isfile(os.path.join(d, CONFIG)):
        raise Error(f"no VM named {name}")
    return d


def library_of(vm_dir):
    """The library a VM's folder is in (<library>/vms/<name>), or None (a folder elsewhere)."""
    parent = os.path.dirname(os.path.abspath(vm_dir))
    return os.path.dirname(parent) if os.path.basename(parent) == VMS_DIR else None


def identity_dir(vm_dir):
    """<vm>/identity: the VM's own files (its SSH keys and model key)."""
    return os.path.join(os.path.abspath(vm_dir), "identity")


def read_toml(path, default=None):
    import tomllib

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:  # tomllib.TOMLDecodeError is a ValueError
        raise Error(f"{path}: {e}")


# -- run state --


def read_pid(path):
    try:
        with open(path) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def proc_alive(pid, *names):
    """pid exists and (with names) its comm or command line contains one of them."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if not names:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False  # a zombie is gone as far as anyone is concerned
        with open(f"/proc/{pid}/comm") as f:
            comm = f.read().strip()
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return False
    return any(n in comm or n in cmd for n in names)


def in_unit(pid, unit):
    """pid lives in the control group of unit.service."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            return any(line.rstrip("\n").endswith(f"/{unit}.service") for line in f)
    except OSError:
        return False


def unit_name(unit_file):
    """The systemd unit a VM was last started as (its run/unit), or None (the setsid fallback)."""
    try:
        with open(unit_file) as f:
            return f.read().strip() or None
    except OSError:
        return None


def qemu_pid(pidfile, unit_file):
    """A VM's QEMU's pid if it is running (in its unit, for a VM started as one; core.VM.pid); a
    stale pidfile is removed."""
    pid = read_pid(pidfile)
    if pid is None:
        return None
    unit = unit_name(unit_file)
    if proc_alive(pid, "qemu-system") and (not unit or in_unit(pid, unit)):
        return pid
    try:
        os.unlink(pidfile)
    except OSError:
        pass
    return None


# -- addresses and ports (keys.py) --

# A VM's host ports (qemu.py, netns.py): each on the VM's own address, 127.0.1.<its slot> by default (vm.toml's
# network.slot, core.ensure_slot), at the TV's stock numbers, so VMs run side by side and ares
# reaches each as a TV of its own. Root SSH is the TV's 22 on 6622 (SSH_PORT), an unprivileged port.
ROOTSSH_PORT = "6622"
DEVMODE_PORT = "9922"
SLOTS = range(1, 255)
# The first three octets of a library's VM addresses (config.toml's addresses), <prefix>.<slot>: each
# library its own, so two libraries' VMs run side by side (addresses_of).
DEFAULT_ADDRESSES = "127.0.1"
CLIENT = "id_ed25519"  # in identity/: the VM's client key, root's on its root SSH
DEVMODE = "devmode_rsa"  # in identity/: its developer key, developer mode's (prisoner)


def parse_addresses(value, key="addresses"):
    """config.toml's addresses: three decimal octets inside 127.0.0.0/8, not 127.0.0 (this
    host's own 127.0.0.1). Normalised."""
    text = str(value).strip()
    parts = text.split(".")
    why = f"{key} {value!r}: the first three octets of the VMs' addresses, inside 127.0.0.0/8 and not 127.0.0, e.g. 127.0.2"
    if len(parts) != 3 or not all(p.isdigit() and len(p) <= 3 and int(p) <= 255 for p in parts):
        raise KeysError(why)
    octets = [int(p) for p in parts]
    if octets[0] != 127 or octets[1:] == [0, 0]:
        raise KeysError(why)
    return ".".join(map(str, octets))


def address(slot, prefix=None):
    """The host address of the VM in slot (1 to 254): <prefix>.<slot> (its library's addresses,
    default 127.0.1), on the loopback, which Linux routes for all of 127.0.0.0/8."""
    try:
        n = int(slot)
    except (TypeError, ValueError):
        n = None
    if n not in SLOTS or isinstance(slot, bool):
        raise KeysError(f"slot {slot!r}: a number from {SLOTS[0]} to {SLOTS[-1]}")
    return f"{parse_addresses(prefix) if prefix else DEFAULT_ADDRESSES}.{n}"


def service_host(listen, address):
    """Where this host reaches the VM's TV-service ports: listen when it is one address,
    else (unset, or 0.0.0.0, which covers it) the VM's own address."""
    if not listen:
        return address
    import ipaddress

    return address if ipaddress.IPv4Address(listen).is_unspecified else listen


def slot_domain(root):
    """(the domain's directory, [its libraries]) of the library at root."""
    root = os.path.abspath(root)
    base = os.path.dirname(root) if os.path.basename(root).startswith("development-") else root
    try:
        dev = sorted(
            os.path.join(base, n) for n in os.listdir(base) if n.startswith("development-")
        )
    except OSError:
        dev = []
    return base, [base] + [d for d in dev if d != base]


def addresses_of(root):
    """The first three octets of the VM addresses of the library at root and of the development
    libraries in it (its slot domain's config.toml, addresses), or the default, 127.0.1 (also for
    None: a VM folder outside any library)."""
    cfg = (read_toml(os.path.join(slot_domain(root)[0], "config.toml"), {}) or {}) if root else {}
    try:
        return parse_addresses(cfg.get("addresses") or DEFAULT_ADDRESSES)
    except KeysError as e:
        raise Error(f"{slot_domain(root)[0]}/config.toml: {e}", "addresses") from None


def vm_address(vm_dir, slot):
    """The host address of the VM in vm_dir with slot: its library's addresses and the slot."""
    return address(slot, addresses_of(library_of(vm_dir)))


def vm_host_address(vm_dir, cfg):
    """The host address of the VM in vm_dir with settings cfg (its library's addresses and its
    slot), on which its ports are; None without a slot (core.VM.address)."""
    slot = cfg.get("slot")
    return vm_address(vm_dir, slot) if slot is not None else None


def vm_service_host(vm_dir, cfg):
    """Where this host reaches the TV-service ports of the VM in vm_dir with settings cfg
    (service_host): listen when that is one address, else its own address; None without a slot
    (core.VM.service_host)."""
    addr = vm_host_address(vm_dir, cfg)
    return addr and service_host(cfg.get("listen"), addr)


# -- the `_ares` query (ares.py) --

ARES_QUERY = "_ares"  # the query another machine's ares-TOOL -d HOST/NAME asks a host's beanjar
ROOT_OPTION = "--root"  # ares-TOOL's and the query's: root on root SSH, not developer mode
LOCAL = "local"  # the name of this machine in HOST/NAME, reserved (remote.py)
# The settings target reads (vmconfig.peek)
TARGET_SETTINGS = ("layers", "slot", "listen", "SSH_PORT", "link", "mode")
LAN_FILE = "lan.json"  # in a VM's run directory: its LAN address while it runs (netns.LAN_FILE)


def lan_direct_address(vm_dir, cfg):
    """With link lan and mode direct (the guest itself on the LAN, which this host cannot reach):
    the guest's LAN address as its run directory's lan.json has it, "" while none is known; None
    for any other link and mode."""
    if cfg.get("link") != "lan" or cfg.get("mode") != "direct":
        return None
    import json

    try:
        with open(os.path.join(vm_dir, RUN_DIR, LAN_FILE)) as f:
            address = json.load(f)["address"]
        return address if isinstance(address, str) else ""
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def split_spec(spec):
    """NAME or HOST/NAME as (host or None, name)."""
    host, sep, name = spec.rpartition("/")
    return (host if sep else None), name


def target(name, vm_dir, cfg, root=False):
    """What another machine's ares-TOOL needs of VM name of this one, in vm_dir with settings cfg
    (this beanjar's answer to ARES_QUERY, which ares.remote_vm reads): its layers, whether it runs,
    where this host reaches it (its address, or listen's when that names one), the port to forward
    to (developer mode's; with root its root SSH port, None without a layer that forwards it) and
    the key the tool logs in with (its developer key; with root its client key), as text, None if
    it has none yet. With link lan and mode direct, "lan" is the guest's own LAN address (""
    while none is known), which the other machine's tool connects to itself: this host cannot
    reach its own macvlan children, so a forward through it would lead nowhere."""
    layers = cfg.get("layers")
    try:
        with open(os.path.join(identity_dir(vm_dir), CLIENT if root else DEVMODE)) as f:
            key = f.read()
    except FileNotFoundError:
        key = None
    if root:
        from . import layermeta

        port = layermeta.root_port(layers, cfg.get("SSH_PORT"))
    else:
        port = DEVMODE_PORT
    lan = lan_direct_address(vm_dir, cfg)
    if lan is not None and port and root:
        port = 22  # the guest's own root SSH port, not the host port a forward would have
    run = os.path.join(vm_dir, RUN_DIR)
    return {
        "name": name,
        "layers": layers,
        "running": qemu_pid(os.path.join(run, PIDFILE), os.path.join(run, UNIT_FILE)) is not None,
        "address": vm_service_host(vm_dir, cfg),
        "port": port and int(port),
        "key": key,
        "lan": lan,
    }
