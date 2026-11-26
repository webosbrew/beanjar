# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's identity as a TV and a host: its MAC and serial (new, parsed, unique in the
library), its keys, its slot (the address it listens on), and the identity directory."""

import os
import re
import secrets
import sys

from . import vmconfig
from .base import (
    Error,
    addresses_of,
    identity_dir,
    library_of,
    read_toml,
    slot_domain,
)
from . import tomlfile


# The TV's identity: the board MAC and serial number, one of each per
# VM (vm.toml's mac and serial), made at create. The MAC is the library's mac_prefix (config.toml,
# three octets; LG's OUI by default, a locally administered 02:... is the user's choice) and three
# random octets; the serial is random in the shape of board.cmdline's serialNum=501RAXX00001.
DEFAULT_MAC_PREFIX = "a8:23:fe"


MAC_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")


MAC_PREFIX_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){2}")


SERIAL_RE = re.compile(r"[0-9A-Z]{1,16}")  # lxboot's serialNum= holds at most 16 characters


def _unicast(octets, what, key):
    if int(octets[:2], 16) & 1:
        raise Error(
            f"{what} {octets}: the first octet is a multicast one (its lowest bit set)", key
        )


def parse_mac(v, key="mac"):
    """A MAC as vm.toml keeps it: six hex octets with colons, lower case, unicast."""
    mac = str(v).strip().lower().replace("-", ":")
    if not MAC_RE.fullmatch(mac):
        raise Error(f"mac {v!r}: six hex octets, e.g. {DEFAULT_MAC_PREFIX}:12:34:56", key)
    _unicast(mac, "mac", key)
    return mac


IFNAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,15}")


SYS_NET = "/sys/class/net"


def parse_lan_parent(v, sys_net=None):
    """config.toml's lan_parent: the host's wired interface that link = "lan" VMs join (e.g. eth0),
    named, never guessed (a VPN may share the LAN's subnet). It must exist and not be wireless:
    macvlan and macvtap do not work on Wi-Fi."""
    name = str(v).strip()
    sys_net = sys_net or SYS_NET
    if not IFNAME_RE.fullmatch(name) or name in (".", ".."):
        raise Error(f"lan_parent {v!r}: a network interface name, e.g. eth0", "lan_parent")
    if not os.path.isdir(os.path.join(sys_net, name)):
        raise Error(f"lan_parent {name}: no such network interface on this host", "lan_parent")
    if os.path.exists(os.path.join(sys_net, name, "wireless")):
        raise Error(
            f"lan_parent {name} is wireless; link=lan needs a wired interface", "lan_parent"
        )
    if name == "lo":
        raise Error("lan_parent lo: the loopback is not a LAN", "lan_parent")
    return name


def parse_mac_prefix(v):
    """config.toml's mac_prefix: three hex octets (an OUI, or a locally administered 02:...)."""
    prefix = str(v).strip().lower().replace("-", ":")
    if not MAC_PREFIX_RE.fullmatch(prefix):
        raise Error(
            f"mac_prefix {v!r}: three hex octets, e.g. {DEFAULT_MAC_PREFIX} (LG) or 02:00:00",
            "mac_prefix",
        )
    _unicast(prefix, "mac_prefix", "mac_prefix")
    return prefix


def parse_serial(v):
    serial = str(v).strip().upper()
    if not SERIAL_RE.fullmatch(serial):
        raise Error(f"serial {v!r}: 1 to 16 letters and digits, e.g. 501RAXX00001", "serial")
    return serial


def new_mac(prefix, taken=()):
    """prefix and three random octets, none of the MACs in taken."""
    while True:
        mac = prefix + "".join(f":{b:02x}" for b in secrets.token_bytes(3))
        if mac not in taken:
            return mac


def new_serial(taken=()):
    """Three digits, four letters, five digits, as 501RAXX00001; none of the serials in taken."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    while True:
        serial = (
            "".join(secrets.choice("0123456789") for _ in range(3))
            + "".join(secrets.choice(letters) for _ in range(4))
            + "".join(secrets.choice("0123456789") for _ in range(5))
        )
        if serial not in taken:
            return serial


# The TV's LGUDID, LG's unique device ID (sm's deviceid/getIDs, nyx DeviceInfo), which a set's
# factory writes once to the macadr partition (macadr.py). Here it is the identity directory's
# lgudid.toml: the LGUDID and the random UUID it was made from (macadr.new_ids, of the MAC the VM
# had then), made with the VM; kept by wipe and snapshot reset (the identity is never in a
# snapshot) and by a change of MAC, as a set's is; new for identity reset and clone.
LGUDID = "lgudid.toml"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LGUDID_HEADER = (
    "# The TV's LGUDID, which the guest finds in its macadr partition, and the random UUID it was\n"
    "# made from. Made with the VM; identity reset and clone make new ones.\n"
)


def read_lgudid(d):
    """(lgudid, namespace) of the identity directory d, or None if it has none (or not one)."""
    try:
        t = read_toml(os.path.join(d, LGUDID))
    except Error:
        return None
    if not isinstance(t, dict):
        return None
    ids = (t.get("lgudid"), t.get("namespace"))
    return ids if all(isinstance(v, str) and UUID_RE.fullmatch(v) for v in ids) else None


def write_lgudid(d, mac):
    """New ids in d (macadr.new_ids of mac), owner-only; returns (lgudid, namespace)."""
    from . import macadr

    namespace, lgudid = macadr.new_ids(mac)
    path = os.path.join(d, LGUDID)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f'{LGUDID_HEADER}lgudid = "{lgudid}"\nnamespace = "{namespace}"\n')
    os.replace(tmp, path)
    return lgudid, namespace


def ensure_lgudid(d, mac, warn=None):
    """(lgudid, namespace) of d, made (of mac) if it has none or not one; warn(line), if given, is
    told when they are."""
    ids = read_lgudid(d)
    if ids:
        return ids
    os.makedirs(d, mode=0o700, exist_ok=True)
    ids = write_lgudid(d, mac)
    if warn:
        warn(f"warning: {d} had no usable {LGUDID}; made a new LGUDID, {ids[0]}")
    return ids


def lan_parent_of(root):
    """The library's lan_parent (its slot domain's config.toml, as addresses_of), or None."""
    cfg = (read_toml(os.path.join(slot_domain(root)[0], "config.toml"), {}) or {}) if root else {}
    return cfg.get("lan_parent") or None


def mac_prefix_of(root):
    """The mac_prefix of the library at root (its config.toml; None: no library), or the default."""
    cfg = (read_toml(os.path.join(root, "config.toml"), {}) or {}) if root else {}
    return parse_mac_prefix(cfg.get("mac_prefix") or DEFAULT_MAC_PREFIX)


def identities(vms_dir, skip=None):
    """({mac}, {serial}) of the VMs in vms_dir but skip (a VM's folder name)."""
    macs, serials = set(), set()
    try:
        names = os.listdir(vms_dir)
    except OSError:
        return macs, serials
    for n in names:
        if n == skip or n.startswith("."):
            continue
        try:
            c = vmconfig.read(os.path.join(vms_dir, n, "vm.toml"), {}) or {}
        except Error:
            continue
        macs.add(str(c.get("mac", "")).lower())
        serials.add(str(c.get("serial", "")).upper())
    return macs, serials


def next_identity(prefix, macs, serials, cfg=None):
    """A new identity's (mac, serial): prefix and random octets, a random serial, none of macs and
    serials (the library's other VMs') nor the ones cfg (a vm.toml's settings) has now. Used by
    identity reset and clone."""
    cfg = cfg or {}
    mac = new_mac(prefix, set(macs) | {str(cfg.get("mac", "")).lower()})
    serial = new_serial(set(serials) | {str(cfg.get("serial", "")).upper()})
    return mac, serial


def renew_keys(d, label, mac, address=None):
    """A new identity's files in the identity directory d: every key pair made anew (keys.renew;
    known_hosts rewritten with address), a new random VNC password and a new LGUDID (of mac, the new
    identity's MAC). The model key
    (sestore.toml), tee-huk and authorized_keys are left as they are. Returns the names made
    (renewed() lists every file it may replace). Used by identity reset and clone; raises
    keys.Error."""
    from . import keys

    made = keys.renew(d, label=label, address=address)
    keys.set_vnc_password(d, None)
    write_lgudid(d, mac)
    return made + [keys.VNC_PASSWORD, LGUDID]


def renewed():
    """The names of the files of an identity directory that a new identity replaces (renew_keys):
    keys.renewed()'s and the LGUDID. Clone leaves them out of the copy."""
    from . import keys

    return keys.renewed() + [LGUDID]


def ensure_identity(vm_dir, say=None):
    """A VM without a mac or serial in its vm.toml gets them now, written back, with a warning:
    the guest's NDUID (derived from the MAC) is then not the one its state was made with. One
    without a slot gets one (ensure_slot)."""
    path = os.path.join(vm_dir, "vm.toml")
    if os.path.isfile(path):
        ensure_slot(vm_dir, say)
    cfg = vmconfig.read(path)
    if cfg is None or (cfg.get("mac") and cfg.get("serial")):
        return cfg
    root = library_of(vm_dir)
    macs, serials = (
        identities(os.path.join(root, "vms"), skip=os.path.basename(os.path.abspath(vm_dir)))
        if root
        else (set(), set())
    )
    made = []
    if not cfg.get("mac"):
        cfg["mac"] = new_mac(mac_prefix_of(root), macs)
        made.append(f"mac {cfg['mac']}")
    if not cfg.get("serial"):
        cfg["serial"] = new_serial(serials)
        made.append(f"serial {cfg['serial']}")
    vmconfig.write(path, cfg, tomlfile.VM_HEADER)
    (say or (lambda line: print(line, file=sys.stderr, flush=True)))(
        f"warning: {path} had no {' or '.join(m.split()[0] for m in made)}; made {', '.join(made)}. "
        "The TV's NDUID follows the MAC: state made before may hold another one"
    )
    return cfg


# A VM's slot N (vm.toml's network.slot) is its host address, keys.address(N) = 127.0.1.N, on which
# it binds every host port at the TV's stock numbers (keys.py). Assigned when the VM is made: the
# lowest one no VM of its slot domain holds, kept while the VM exists. The slot domain is a library
# and the development libraries in it (<library>/development-*, development.py: the checkout's VM
# and its INSTANCEs), so a checkout VM and a library VM never share an address.
SLOT_LOCK = ".slots.lock"


def slots_taken(root, skip=None):
    """{slot: VM directory} of the VMs of root's slot domain but skip (a VM's directory)."""
    skip = os.path.abspath(skip) if skip else None
    taken = {}
    for lib in slot_domain(root)[1]:
        vms_dir = os.path.join(lib, "vms")
        try:
            names = sorted(os.listdir(vms_dir))
        except OSError:
            continue
        for n in names:
            d = os.path.join(vms_dir, n)
            if n.startswith(".") or d == skip:
                continue
            try:
                slot = (vmconfig.read(os.path.join(d, "vm.toml"), {}) or {}).get("slot")
            except Error:
                continue
            if slot is not None:
                taken.setdefault(slot, d)
    return taken


def free_slot(root, skip=None):
    """The lowest slot no VM of root's slot domain holds (but skip)."""
    from . import keys

    taken = slots_taken(root, skip)
    for slot in keys.SLOTS:
        if slot not in taken:
            return slot
    raise Error(
        f"no free slot: the {len(taken)} VMs of {slot_domain(root)[0]} hold all of them", "slot"
    )


class slot_lock:
    """The slot domain's lock (fcntl.flock on <domain>/.slots.lock): held while a slot is chosen and
    the VM that takes it is written, so two VMs made at once never take the same one."""

    def __init__(self, root):
        self.path = os.path.join(slot_domain(root)[0], SLOT_LOCK)

    def __enter__(self):
        import fcntl

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.f = open(self.path, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.f.close()


def ensure_slot(vm_dir, say=None):
    """A VM without a slot in its vm.toml (one made before slots) gets one now, written back once:
    the lowest free one of its slot domain (a folder outside a library is alone in its domain: slot
    1). Returns its slot."""
    path = os.path.join(vm_dir, "vm.toml")
    cfg = vmconfig.read(path)
    if cfg is None:
        raise Error(f"{path}: no such file")
    if cfg.get("slot") is not None:
        return cfg["slot"]
    from . import keys

    root = library_of(vm_dir)
    if root is None:
        cfg["slot"] = keys.SLOTS[0]
        vmconfig.write(path, cfg, tomlfile.VM_HEADER)
    else:
        with slot_lock(root):
            cfg = vmconfig.read(path)
            if cfg.get("slot") is not None:
                return cfg["slot"]
            cfg["slot"] = free_slot(root, skip=vm_dir)
            vmconfig.write(path, cfg, tomlfile.VM_HEADER)
    (say or (lambda line: print(line, file=sys.stderr, flush=True)))(
        f"note: {path} had no slot; it has slot {cfg['slot']} now, its ports on {keys.address(cfg['slot'], addresses_of(root))}"
    )
    return cfg["slot"]


def ca_dir(vm_dir):
    """The mode=mitm proxy's CA of the VM in vm_dir: its library's <library>/ca, shared by the
    library's VMs (beanjar/keys.py ensure_ca); a VM folder outside a
    library has its own, <vm>/identity/ca."""
    lib = library_of(vm_dir)
    return os.path.join(lib, "ca") if lib else os.path.join(identity_dir(vm_dir), "ca")


def make_identity_dir(vm_dir):
    """<vm>/identity, made or brought to mode 0700 (it holds keys)."""
    d = identity_dir(vm_dir)
    os.makedirs(d, mode=0o700, exist_ok=True)
    if os.stat(d).st_mode & 0o777 != 0o700:
        os.chmod(d, 0o700)
    return d
