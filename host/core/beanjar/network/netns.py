# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What runs QEMU for link=nat, link=lan and link=off (qemu.py's NET_LINK and NET_MODE): the launcher calls run() or
off() with QEMU's argv in place of running QEMU itself.

run(): a rootless network namespace, no sudo and no persistent host state, for mode=mitm and
mode=direct alike; direct is the same without mitmweb (no inspector, its relay or password). It makes a directory for
the port-bridge unix sockets (NETNS_SOCK_DIR, which the VM's unit sets to its RuntimeDirectory, or a
new webos-netns-* in TMPDIR), bridges the mitmweb inspector and the loopback-forwarded ports
(NETNS_FWD: layermeta.forwards, e.g. SSAP 3000/3001, root SSH 6622, devmode 9922/9991), all on the VM's own address VM_ADDRESS (127.0.1.<slot>,
qemu.py; the forwards on LISTEN_ADDRESS when vm.toml's listen sets it), into the namespace over those sockets (beanjar-relay, host/helpers/network/relay.c, one
process on each side), runs inner() under `unshare -rn` (a user+net namespace) and gives that
namespace its uplink to the internet with slirp4netns. inner() sets up the TAP, the routing of
the guest's TCP and UDP into mitmweb and DHCP, then runs QEMU. When QEMU exits the namespace is gone; the
helpers are killed and the socket dir removed.

  python3 -m beanjar.network.netns inner QEMU-ARGV...     (what run() runs under unshare -rn)
  python3 -m beanjar.network.netns lan QEMU-ARGV...       (link=lan: what run() runs under unshare -rnm)

What the guest may not reach (the host's loopback, always; without NET_LAN_ACCESS=on the LAN and
the host's own addresses) is refused by policy rules in the inner namespace (BLOCK_PREF below), the
host's addresses kept current by HostWatch outside and HostRules inside. With NET_LG_SERVICES=mock
(the default) the guest's name server is lg_mock.py in the inner namespace (MOCK_DNS below), and
LG's literal addresses and other resolvers are refused at BLOCK_PREF too.

The namespace is a router with no NAT and no netfilter. slirp4netns runs on the guest's own
network, 10.0.2.0/24, as QEMU's slirp does for mode=direct, and the namespace sits between the two
by proxy ARP: the guest's 10.0.2.15 is routed to the TAP, and each side's ARP for the other side's
addresses is answered by the namespace, so slirp sees the guest's packets from 10.0.2.15 unchanged
(a source outside its network it drops). The guest's TCP and UDP, every port, are routed instead
into mitmweb's own TUN device (`--mode tun`, TUN_DEV below) by policy rules; mitmweb forwards each
connection to its real destination itself, and ICMP goes to slirp unchanged. Apart from that the
guest's network is mode=direct's: DHCP (10.0.2.15, router 10.0.2.2), real DNS (slirp4netns's
resolver, 10.0.2.3, through mitmweb). mitmweb is the user's own mitmproxy (mitmtool.py: NETNS_MITMWEB,
PATH or ~/.local/bin, 12.0.1 or newer); without it the guest goes out directly. Env:
NETNS_FWD ("hostport:guestport ..."), NETNS_TAP, NETSTUB_WEB_HOST/NETSTUB_WEB_PORT,
NETSTUB_LOG, NETSTUB_STREAM_LARGE, MITM_FLOWS and MITM_FLOW_BYTES (mitmweb's max_flows and max_flow_bytes, in bytes), NETNS_LOG_DIR (the helpers' logs, default build/),
BEANJAR_MITM_PASSWORD / NETSTUB_WEB_PASSWORD_FILE (the inspector's password, PASSWORD_FILE below),
NETNS_SOCK_DIR, NETSTUB_CA (the proxy's CA, mitmweb's confdir; qemu.py sets it), VM_ADDRESS (qemu.py sets it).
NETNS_MITMWEB (mitmweb's path, mitmtool.py).
The inspector's host side listens on NETSTUB_WEB_HOST (default VM_ADDRESS;
0.0.0.0 opens it to the LAN), behind a password wherever it listens (PASSWORD_FILE below).

link = "lan" (NET_LINK=lan, mode mitm): the same inner namespace and guest, one level further in.
run() runs lan() under `unshare -rnm` (the LAN namespace, a user, network and mount namespace of
its own) and has the CAP_NET_ADMIN helper (NET_LAN_HELPER, host/helpers/network/lan-helper.c) make
a macvlan, lan0, on NET_LAN_PARENT with the VM's MAC (NET_LAN_MAC) straight into it. lan()
configures lan0 from a DHCP lease (busybox udhcpc, with host/helpers/network/lan_dhcp.py as its
event script, released when the VM stops) or the static NET_ADDRESS ("A.B.C.D/NN,GATEWAY", the
gateway also the DNS server), points its own /etc/resolv.conf (a bind mount, in its mount
namespace only) at the lease's DNS, writes the lease to RUN_DIR/lan.json (what status, list and
the GUI show), then runs inner() under `unshare -rn` with slirp4netns as before, now inside the
LAN namespace: the guest's traffic, mitmweb's upstream included, leaves from the VM's own LAN
address and MAC. A relay in the LAN namespace serves the TV-service ports (NETNS_FWD), the
inspector (NETSTUB_WEB_PORT, behind the same password) and VNC (VNC_PORT, beanjar vnc's while it
runs, from its unix socket VNC_SOCK) on that address; the host-side relay keeps the first two on
VM_ADDRESS (the inspector on NETSTUB_WEB_HOST) too, since the host itself cannot reach its own
macvlan children, and beanjar-vnc listens there itself. lan0 lives only in the LAN namespace, so it is
deleted at a stop, and destroyed with the namespace however the VM stops (every helper there dies
with its parent, PR_SET_PDEATHSIG): no interface outlives the VM. After a kill the kernel may keep
that namespace, and so lan0 and the VM's MAC on the parent, until its last TCP sockets are torn
down, minutes at most; the next start waits LAN_MAC_WAIT for it.

link = "lan" with mode direct (NET_LINK=lan, NET_MODE=direct): the same LAN namespace and lan0, but
no address, DHCP client, slirp4netns, inner namespace or relay in it. lan() joins lan0 and the
guest's TAP with tc (join_commands: each one's ingress redirected to the other's egress, before
this namespace's IP stack) and runs QEMU there, its NIC already with lan0's MAC: the guest asks the
LAN for its own lease, every port of it is on that address, and nothing of Beanjar's sees its
traffic, so NET_LG_SERVICES=mock cannot apply (qemu.py refuses it). LeaseWatch reads the LAN's
DHCPACK to the guest off the TAP for RUN_DIR/lan.json. This host cannot reach the guest (a
macvlan's parent never reaches its children). macvtap is not used: its /dev/tapN is the host
root's, mode 0600, which the rootless namespace cannot open.

off(): the TV with no cable plugged in. The guest keeps its NIC (virtio-net, the board MAC), as a TV
keeps its Ethernet port, but the link is down from its first look, so connman sees a wired
interface with no carrier. The netdev is a hub port alone on its hub, so there is nothing on the
other side even if the guest could raise the link: no egress, structurally. QEMU has no
command-line option for a NIC that starts with its link down, so QEMU starts paused (-S) with a
private QMP socket, the link is set down (QMP set_link on net0) and QEMU resumed.
"""

import ctypes
import ipaddress
import json
import os
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from .. import base, paths
from ..runtime import hostswap
from ..vm import settings
from . import mitmtool


class Error(Exception):
    pass


# The guest's network, mode=direct's (QEMU's slirp defaults). slirp4netns serves it (the router,
# its DNS forwarder, and .100, its own address for the namespace, on up0). DHCP_SERVER is the
# TAP's only address, the DHCP server's identifier: an address slirp does not have, so the guest's
# unicast renewals reach this namespace's DHCP server rather than being proxy-ARPed to slirp's.
CIDR = "10.0.2.0/24"
GUEST = "10.0.2.15"
ROUTER = "10.0.2.2"
DNS = "10.0.2.3"
DHCP_SERVER = "10.0.2.254"

# The inspector: mitmweb's own `--mode tun:TUN_DEV`. mitmweb makes the TUN device itself in this
# namespace (its user namespace's CAP_NET_ADMIN is enough) and reads each connection's destination
# from its packets, so no mitmproxy internals are patched for that. All of the guest's TCP and UDP,
# every port (only what arrives on the TAP: mitmweb's own upstream connections are not, and leave
# by the main table's default route), is routed into the device by policy rules (TUN_PROTOCOLS) to
# TUN_TABLE, whose one route is the device; ICMP is not matched and goes to slirp. The replies come
# out of the device and are forwarded to the TAP like any other packet. mitm_addon.py raises
# mitmproxy's UDP idle timeout (its TUN_UDP_TIMEOUT: an idle UDP flow closed in Python while the
# TUN side keeps the 5-tuple lost the guest's DNS). mitmweb is the user's own (mitmtool.py); what
# this start found (its path, version and, when it cannot serve, the problem) goes to
# RUN_DIR/MITM_INFO_FILE.
TUN_DEV = "mitm0"
TUN_TABLE = "101"
MITM_INFO_FILE = "mitmweb.json"


# What the guest may not reach (docs/dev/architecture/network.md, "What the guest reaches"): policy
# rules in the inner namespace, before the interception (BLOCK_PREF < INTERCEPT_PREF) and without an
# iif, so they hold for the guest's forwarded packets and for mitmweb's own upstream connections
# alike, in mode=mitm and mode=direct. "prohibit": the guest's connection fails at once (ICMP
# administratively prohibited), a local connect with EACCES. The namespace's own addresses (the
# TAP's, up0's, its loopback: the relays, mitmweb's web port) are the local table's (pref 0), before
# these. The guest's own network (CIDR: the guest, slirp's router and DNS) is never in them.
#   LOOPBACK_NETS  always, whatever NET_LAN_ACCESS says (slirp4netns's --disable-host-loopback
#                  already refuses the router's 10.0.2.2; these refuse the rest, defence in depth)
#   LAN_NETS       unless NET_LAN_ACCESS=on (link=lan always has it): the private ranges, shared
#                  address space (CGNAT, VPNs such as Tailscale), link-local and multicast
#   the host's own addresses (HOST_FILE), likewise: slirp4netns opens its sockets in the host's
#                  namespace, where they are local. Those inside LAN_NETS are covered already;
#                  the rest (a public address on the host) get a rule each (HOST_PREF), kept
#                  current while the VM runs (HostWatch), never a snapshot from the start only.
#                  An address on the host's loopback interface outside 127.0.0.0/8 (WSL's DNS
#                  10.255.255.254 on lo) is the host's loopback: refused even with LAN access.
# IPv6 needs nothing: slirp4netns runs without --enable-ipv6 and the namespace forwards no IPv6, so
# the guest has link-local IPv6 on the TAP at most.
BLOCK_PREF = "90"
HOST_PREF = "91"
LOOPBACK_NETS = ("127.0.0.0/8", "0.0.0.0/8")
LAN_NETS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "224.0.0.0/4",
)
HOST_FILE = "host-addresses"  # in NETNS_RUNDIR: the host's addresses the rules refuse, one a line
HOST_POLL = 30  # s between HostWatch's reads when no address event arrived
HOST_POLL_BLIND = 2  # s between them once its monitor is gone


def lan_access(env):
    """Whether the guest may reach the LAN and the host's own non-loopback addresses: NET_LAN_ACCESS
    on (qemu.py: vm.toml's network.lan_access; always with link=lan)."""
    return env.get("NET_LAN_ACCESS") == "on"


# LG's online services (vm.toml's network.lg_services, NET_LG_SERVICES; docs/dev/architecture/
# network.md, "LG services"). With mock (the default; anything but "real") the guest's names are
# resolved by lg_mock.py in this namespace, at MOCK_DNS (the TAP's own address, which DHCP then
# hands out as the name server): it answers NXDOMAIN for the refused classes of lg_hosts.py, resolves
# LG's online check and the answered classes (SDP and LG's web hosts) to itself and answers them on
# ports 80 and 443 (lg_sdp.py, its certificates from the proxy's CA), and forwards the rest to
# slirp4netns's resolver (DNS). Policy rules at BLOCK_PREF, beside the loopback and LAN ones, refuse what would get
# around it: the classes' literal addresses (no iif: mitmweb's upstream too), and the guest's own
# DNS to any other server (DNS_BYPASS: plain DNS and DNS over TLS). In mode=mitm the resolver's
# upstream queries are routed into mitmweb (intercept_rules), so the inspector still shows them.
# None of it needs mitmweb: it holds in mode=direct and when mitmweb is missing or fails.
MOCK_DNS = DHCP_SERVER
LG_CERTS = "lgmock-certs"  # in the socket directory: lg_mock.py's minted certificates and key
DNS_BYPASS = (("udp", "53"), ("tcp", "53"), ("tcp", "853"))


def lg_mock(env):
    """Whether LG's services are mocked: NET_LG_SERVICES anything but real (qemu.py sets it from
    vm.toml's network.lg_services, default mock)."""
    return env.get("NET_LG_SERVICES") != "real"


def guest_dns(env):
    """The name server the namespace's DHCP hands the guest: MOCK_DNS with LG's services mocked,
    else slirp4netns's resolver (DNS)."""
    return MOCK_DNS if lg_mock(env) else DNS


def _lg_hosts(tree=paths.TREE):
    """host/helpers/network/lg_hosts.py, the table of LG's host classes, as a module."""
    import importlib.util

    path = tree.src("host", "helpers", "network", "lg_hosts.py")
    spec = importlib.util.spec_from_file_location("beanjar_lg_hosts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lg_rules(tap, addresses, action="add"):
    """The ip commands of mock mode's refusals at BLOCK_PREF: each of addresses (lg_hosts'
    refused_addresses), and the guest's DNS (arriving on tap) to any server but MOCK_DNS, which is
    the local table's, before them."""
    out = [["rule", action, "pref", BLOCK_PREF, "to", f"{a}/32", "prohibit"] for a in addresses]
    out += [
        ["rule", action, "pref", BLOCK_PREF, "iif", tap, "ipproto", p, "dport", port, "prohibit"]
        for p, port in DNS_BYPASS
    ]
    return out


def block_nets(lan):
    """The destinations BLOCK_PREF's rules refuse, LAN access (lan) or not: CIDR strings, the
    guest's network (CIDR) cut out of the range that holds it."""
    own = ipaddress.ip_network(CIDR)
    out = []
    for n in LOOPBACK_NETS + (() if lan else LAN_NETS):
        n = ipaddress.ip_network(n)
        if own.subnet_of(n):
            out += sorted(n.address_exclude(own))
        else:
            out.append(n)
    return [str(n) for n in out]


def block_rules(lan, action="add"):
    """The ip commands of block_nets(lan): one prohibit rule each at BLOCK_PREF."""
    return [["rule", action, "pref", BLOCK_PREF, "to", n, "prohibit"] for n in block_nets(lan)]


def host_blocks(entries, lan):
    """Of the host's addresses (entries: (address, on a loopback interface)), those HOST_PREF's
    rules must refuse, LAN access (lan) or not: IPv4, outside block_nets(lan) (which covers them)
    and the guest's network (slirp's there, not the host's); the loopback interface's always, the
    others without LAN access only. Sorted strings."""
    own = ipaddress.ip_network(CIDR)
    covered = [ipaddress.ip_network(n) for n in LOOPBACK_NETS + (() if lan else LAN_NETS)]
    out = set()
    for a, loopback in entries:
        try:
            a = ipaddress.IPv4Address(a)
        except ValueError:
            continue
        if a in own or any(a in n for n in covered) or (lan and not loopback):
            continue
        out.add(a)
    return [str(a) for a in sorted(out)]


def host_rules(addresses, action):
    """The ip commands that refuse each of addresses (host_blocks' strings) at HOST_PREF."""
    return [["rule", action, "pref", HOST_PREF, "to", f"{a}/32", "prohibit"] for a in addresses]


def parse_addresses(text):
    """The IPv4 addresses of `ip -j -4 addr show`'s output: [(address, on a loopback interface)]."""
    try:
        links = json.loads(text or "[]")
    except ValueError:
        return []
    out = []
    for link in links:
        if not isinstance(link, dict):
            continue
        loopback = "LOOPBACK" in (link.get("flags") or ()) or link.get("ifname") == "lo"
        for i in link.get("addr_info", ()):
            if isinstance(i, dict) and i.get("family") == "inet" and i.get("local"):
                out.append((i["local"], loopback))
    return out


def host_addresses(env):
    """This namespace's IPv4 addresses (the host's), or None if `ip` could not list them."""
    r = subprocess.run(
        ["ip", "-j", "-4", "addr", "show"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return parse_addresses(r.stdout) if r.returncode == 0 else None


def write_host_file(path, entries):
    """HOST_FILE: one address a line, "ADDRESS" or "ADDRESS lo" (on a loopback interface),
    replaced at once (inner() reads it while it is written)."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write("".join(a + (" lo" if lo else "") + "\n" for a, lo in entries))
    os.replace(tmp, path)


def read_host_file(path, lan):
    """What HOST_PREF's rules refuse of HOST_FILE's addresses (host_blocks; a missing file: none)."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    return host_blocks(
        [(w[0], w[1:] == ["lo"]) for w in (line.split() for line in lines) if w], lan
    )


class HostWatch:
    """The host's own addresses for the inner namespace's HOST_PREF rules (link=nat): HOST_FILE
    written now and again whenever they change. `ip -o monitor address` says when (step reads it again at once),
    and every HOST_POLL s they are read again anyway, should the monitor be gone."""

    def __init__(self, env, path):
        self.env, self.path, self.written = env, path, None
        self.refresh()
        self.monitor = subprocess.Popen(
            ["ip", "-o", "monitor", "address"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            preexec_fn=_die_with_parent,
        )
        self.last = time.monotonic()

    def refresh(self):
        found = host_addresses(self.env)
        if found is None:
            if self.written is None:
                raise Error("netns: could not list the host's addresses (ip -j -4 addr show)")
            return  # keep the last list rather than none
        entries = sorted(set(found))
        if entries != self.written:
            write_host_file(self.path, entries)
            self.written = entries
        self.last = time.monotonic()

    def step(self, timeout):
        """Wait up to timeout s for an address event; read the addresses again after one, or once
        HOST_POLL s have passed without."""
        out = self.monitor.stdout if self.monitor and self.monitor.poll() is None else None
        if out is not None and select.select([out], [], [], timeout)[0]:
            if not os.read(out.fileno(), 65536):
                self.monitor.wait()  # EOF: the monitor is gone; HOST_POLL from now on
            self.refresh()
            return
        if out is None:
            time.sleep(timeout)
        if time.monotonic() - self.last >= (HOST_POLL if out is not None else HOST_POLL_BLIND):
            self.refresh()


def mitm_info(run_dir):
    """What the VM's mitmweb is (RUN_DIR/MITM_INFO_FILE: version, path, problem), or None."""
    try:
        with open(os.path.join(run_dir, MITM_INFO_FILE)) as f:
            info = json.load(f)
        return info if isinstance(info, dict) else None
    except (OSError, ValueError, TypeError):
        return None


# The inspector's password (mitmweb's web_password). BEANJAR_MITM_PASSWORD supplies one; otherwise
# each start makes a random one. It travels only in an owner-only file, PASSWORD_FILE in RUN_DIR,
# whose path is NETSTUB_WEB_PASSWORD_FILE: never in argv (systemd-run --setenv, mitmweb's command
# line) or in a log. mitm_addon.py reads it into mitmweb; the manager (inspector.py) reads it to
# show it and to log in.
PASSWORD_ENV = "BEANJAR_MITM_PASSWORD"
PASSWORD_FILE = "mitmweb.password"

# beanjar vnc (core.Library.vnc): with link=lan it also listens on VNC_SOCK in the socket dir, which
# the LAN namespace's relay serves on the LAN address at VNC_PORT. A connection there while it is
# not running is closed.
VNC_PORT = 5900
VNC_SOCK = "vnc.sock"


LAN_IF = "lan0"  # the macvlan in the LAN namespace (lan-helper.c's IFNAME)
LAN_FILE = "lan.json"  # RUN_DIR/lan.json: the VM's LAN address while it runs (VM.lan_address)
LAN_LEASE_WAIT = 30  # s for a DHCP lease
LAN_MAC_WAIT = 60  # s for a previous run's lan0 to let go of the VM's MAC


def _die_with_parent():
    """preexec_fn: SIGKILL when the parent dies (prctl PR_SET_PDEATHSIG), so that nothing of a
    namespace outlives the process that made it (and holds its lan0)."""
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)


def lan_info(run_dir):
    """The LAN lease of the VM whose run directory is run_dir (RUN_DIR/lan.json), or None."""
    try:
        with open(os.path.join(run_dir, LAN_FILE)) as f:
            info = json.load(f)
        ipaddress.IPv4Address(info["address"])
        return info
    except (OSError, ValueError, KeyError, TypeError):
        return None


def lan_direct(env):
    """Whether env is link=lan with mode=direct: the guest's TAP joined to lan0, nothing between."""
    return settings.lan_direct(env.get("NET_LINK"), env.get("NET_MODE"))


def join_commands(lan_if, tap):
    """link=lan with mode=direct: the tc commands (argv lists) that join lan_if (the macvlan, the
    VM's MAC) and tap (QEMU's NIC, the same MAC) into one wire. Every frame arriving on either is
    redirected, at ingress, to the other's egress (clsact, matchall, mirred), so the frames pass
    unchanged and the LAN namespace's own IP stack never sees them: no address, route or policy
    rule of it applies to the guest."""
    return [
        ["tc", "qdisc", "add", "dev", lan_if, "clsact"],
        ["tc", "qdisc", "add", "dev", tap, "clsact"],
        *(
            ["tc", "filter", "add", "dev", a, "ingress", "matchall"]
            + ["action", "mirred", "egress", "redirect", "dev", b]
            for a, b in ((lan_if, tap), (tap, lan_if))
        ),
    ]


def guest_lease(frame, mac):
    """The lease in frame (an Ethernet frame) if it is a DHCPACK from the LAN's server to the client
    with mac (aa:bb:..): {address, prefix, router, dns, lease}; else None. link=lan with mode=direct
    learns the guest's address this way, reading only, for RUN_DIR/lan.json."""
    try:
        if len(frame) < 14 + 20 + 8 + 240 or frame[12:14] != b"\x08\x00":
            return None
        ip = frame[14:]
        ihl = (ip[0] & 0x0F) * 4
        if ip[0] >> 4 != 4 or ip[9] != 17 or ip[6] & 0x3F or ip[7]:  # IPv4, UDP, not a fragment
            return None
        udp = ip[ihl:]
        if udp[0:4] != b"\x00\x43\x00\x44":  # 67 -> 68
            return None
        bootp = udp[8:]
        if bootp[0] != 2 or bootp[28:34] != bytes.fromhex(mac.replace(":", "")):
            return None
        if bootp[236:240] != bytes((99, 130, 83, 99)):
            return None
        opts, i, data = {}, 0, bootp[240:]
        while i < len(data) and data[i] != 255:
            if data[i] == 0:
                i += 1
                continue
            opts[data[i]] = data[i + 2 : i + 2 + data[i + 1]]
            i += 2 + data[i + 1]
        if opts.get(53) != b"\x05":
            return None
        address = socket.inet_ntoa(bootp[16:20])
        mask = opts.get(1, b"")
        prefix = (
            ipaddress.IPv4Network(f"0.0.0.0/{socket.inet_ntoa(mask)}").prefixlen
            if len(mask) == 4
            else 32
        )
        router = opts.get(3, b"")
        dns = opts.get(6, b"")
        lease = opts.get(51, b"")
        if address == "0.0.0.0":
            return None
        return {
            "address": address,
            "prefix": prefix,
            "router": socket.inet_ntoa(router[:4]) if len(router) >= 4 else None,
            "dns": [socket.inet_ntoa(dns[i : i + 4]) for i in range(0, len(dns) - 3, 4)],
            "lease": int.from_bytes(lease, "big") if len(lease) == 4 else 0,
        }
    except (IndexError, ValueError):
        return None


def write_lan_info(path, lease):
    """RUN_DIR/lan.json (lan_info), atomically."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(lease, f)
    os.replace(tmp, path)


def write_password(path, password="", source=PASSWORD_ENV):
    """Write password (a new random one if empty; source: where a given one came from) to path,
    owner-only, replacing what was there."""
    password = password or secrets.token_urlsafe(18)
    if any(c in password for c in "\r\n"):
        raise Error(f"{source} must be one line")
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(password + "\n")
    os.replace(tmp, path)
    return path


def _sbin_path(env):
    """ip lives in /sbin or /usr/sbin, which a non-login ssh shell (a Pi over `ssh host
    ...`) leaves out of PATH."""
    path = env.get("PATH", os.defpath)
    if "/usr/sbin" not in path.split(":"):
        env["PATH"] = path + ":/usr/sbin:/sbin"
    return env


class _Stop(Exception):
    def __init__(self, sig):
        self.sig = sig


def _raise_on(*sigs):
    """Raise _Stop on the first of sigs only: a second signal arriving while the finally block
    starts (before it ignores them) must not abort the cleanup."""
    raised = []

    def handler(sig, _frame):
        if not raised:
            raised.append(sig)
            raise _Stop(sig)

    for s in sigs:
        signal.signal(s, handler)


def _kill(procs):
    for p in procs:
        if p.poll() is None:
            try:
                p.terminate()
            except ProcessLookupError:
                pass
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def _relay(tree, pairs, env, log_path, who):
    """Start beanjar-relay (host/helpers/network/relay.c) for pairs (LISTEN=CONNECT), its stderr in
    log_path; return it once every listener is bound, or raise Error with its reason."""
    with open(log_path, "w") as log:
        p = subprocess.Popen(
            [tree.relay(), *pairs],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log,
            preexec_fn=_die_with_parent,
        )
    with p.stdout:
        ok = select.select([p.stdout], [], [], 10)[0] and p.stdout.readline() == b"ready\n"
    if not ok:
        _kill([p])
        try:
            with open(log_path) as f:
                why = f.read().strip().splitlines()[-1:]
        except OSError:
            why = []
        raise Error(f"{who}: port relay failed: {why[0] if why else 'no reply'} (see {log_path})")
    return p


def inspected(env):
    """Whether the namespace runs the inspector: NET_MODE mitm (the default), not direct."""
    return env.get("NET_MODE") != "direct"


def relay_pairs(env, rundir):
    """The host side's relay (LISTEN=CONNECT, beanjar-relay): the inspector on NETSTUB_WEB_HOST and
    NETSTUB_WEB_PORT (mode=mitm only), the layers' forwards (NETNS_FWD) on LISTEN_ADDRESS, else
    VM_ADDRESS (qemu.py)."""
    listen = env.get("LISTEN_ADDRESS") or env["VM_ADDRESS"]
    pairs = []
    if inspected(env):
        pairs.append(
            f"tcp:{env['NETSTUB_WEB_HOST']}:{env['NETSTUB_WEB_PORT']}=unix:{rundir}/mitmweb.sock"
        )
    return pairs + [
        f"tcp:{listen}:{hp}=unix:{rundir}/fwd-{hp}.sock"
        for hp in (f.split(":", 1)[0] for f in env.get("NETNS_FWD", "").split())
    ]


def lan_relay_pairs(env, rundir):
    """link=lan's relay in the LAN namespace (LISTEN=CONNECT, beanjar-relay): the inspector on
    NETSTUB_WEB_PORT, VNC on VNC_PORT and the layers' forwards (NETNS_FWD), on every address there
    (lo and lan0)."""
    pairs = [
        f"tcp:0.0.0.0:{env['NETSTUB_WEB_PORT']}=unix:{rundir}/mitmweb.sock",
        f"tcp:0.0.0.0:{VNC_PORT}=unix:{rundir}/{VNC_SOCK}",
    ]
    return pairs + [
        f"tcp:0.0.0.0:{hp}=unix:{rundir}/fwd-{hp}.sock"
        for hp in (f.split(":", 1)[0] for f in env.get("NETNS_FWD", "").split())
    ]


def _status(rc):
    return 128 - rc if rc < 0 else rc


def _netns(pid):
    try:
        return os.readlink(f"/proc/{pid}/ns/net")
    except OSError:
        return None


def _wait_netns(child):
    """Wait until child (a Popen of unshare) has its own network namespace."""
    mine = _netns(os.getpid())
    for _ in range(100):
        if _netns(child.pid) != mine:
            break
        time.sleep(0.05)
    if _netns(child.pid) in (mine, None):
        raise Error("netns: namespace did not appear")


def _uplink(child, env, logdir):
    """slirp4netns for child's namespace (inner()'s): the guest's network's router (.2), DNS (.3)
    and NAT, on interface up0, from this namespace (the host's, or link=lan's LAN namespace)."""
    with open(os.path.join(logdir, "netns-uplink.log"), "w") as log:
        return subprocess.Popen(
            [
                "slirp4netns",
                "--configure",
                "--mtu=65520",
                "--disable-host-loopback",
                f"--cidr={CIDR}",
                str(child.pid),
                "up0",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            preexec_fn=_die_with_parent,
        )


def run(argv, env, tree=paths.TREE):
    """Run QEMU (argv) in the namespace of link=nat (mode mitm or direct) or link=lan; QEMU's exit
    status."""
    env = _sbin_path(dict(env))
    lan = env.get("NET_LINK") == "lan"
    mitm = inspected(env)
    if lan_direct(env):
        tools = ("unshare", "ip", "tc")  # no uplink, DHCP client or relay of ours
    else:
        tools = ("unshare", "ip", "slirp4netns") + (("busybox",) if lan else ())
    for tool in tools:
        if not shutil.which(tool, path=env["PATH"]):
            raise Error(f"netns: {tool} not found (this VM's link and mode need it)")
    if lan:
        for k in ("NET_LAN_PARENT", "NET_LAN_MAC", "NET_LAN_HELPER"):
            if not env.get(k):
                raise Error(f"netns: {k} unset (qemu.py sets it for link=lan)")
    # Fail early and clearly if unprivileged user namespaces are unavailable, rather than needing root.
    if (
        subprocess.run(["unshare", "-rn", "true"], env=env, stderr=subprocess.DEVNULL).returncode
        != 0
    ):
        raise Error(
            "netns: rootless 'unshare -rn' unavailable on this host (every link=nat and link=lan VM"
            " needs unprivileged user namespaces; link=off needs none)"
        )
    if env.get("NETNS_SOCK_DIR"):
        rundir = env["NETNS_SOCK_DIR"]
        os.makedirs(rundir, exist_ok=True)
    else:
        rundir = tempfile.mkdtemp(prefix="webos-netns-", dir=env.get("TMPDIR") or "/tmp")
    env["NETNS_RUNDIR"] = rundir
    # every host port on the VM's own address (qemu.py: VM_ADDRESS, 127.0.1.<slot>), the
    # inspector's too unless NETSTUB_WEB_HOST says otherwise
    address = env.get("VM_ADDRESS")
    if not address:
        raise Error("netns: VM_ADDRESS unset (the VM's host address, qemu.py)")
    web_port = env["NETSTUB_WEB_PORT"] = env.get("NETSTUB_WEB_PORT") or "18083"
    web_host = env["NETSTUB_WEB_HOST"] = env.get("NETSTUB_WEB_HOST") or address
    logdir = env.get("NETNS_LOG_DIR") or tree.build()
    # the inspector's password: the launcher's file (launch.py, from BEANJAR_MITM_PASSWORD), else
    # BEANJAR_MITM_PASSWORD, else a new random one; only the file's path goes on to the namespace
    given = env.pop(PASSWORD_ENV, "")
    password = None
    if mitm:
        password = env.get("NETSTUB_WEB_PASSWORD_FILE")
        if given or not password or not os.path.isfile(password):
            password = write_password(
                os.path.join(env.get("RUN_DIR") or rundir, PASSWORD_FILE), given
            )
        env["NETSTUB_WEB_PASSWORD_FILE"] = password
    else:
        env.pop("NETSTUB_WEB_PASSWORD_FILE", None)
    # link=lan is on the LAN by definition; link=nat reaches it only with NET_LAN_ACCESS=on
    if lan:
        env["NET_LAN_ACCESS"] = "on"
    helpers, ns, watch = [], None, None
    _raise_on(signal.SIGTERM, signal.SIGINT)
    try:
        # The inspector lives inside the namespace; only its web port is exposed on the host. The
        # forwarded ports: LISTEN_ADDRESS (default VM_ADDRESS):<hostport> -> shared unix socket -> (inner) guest:<guestport>.
        # stdin stays ours, so an interactive serial console (on stdio) still reads the terminal.
        penv = dict(
            env,
            PYTHONPATH=os.pathsep.join(x for x in (tree.pkg_parent, env.get("PYTHONPATH")) if x),
        )
        pairs = relay_pairs(env, rundir)
        if pairs:
            helpers.append(
                _relay(tree, pairs, penv, os.path.join(logdir, "netns-relay.log"), "netns")
            )
        if mitm:
            _announce_inspector(
                env,
                f"traffic inspector (mitmweb): http://{web_host}:{web_port} (password in {password})",
            )
        if not lan:
            # the host's own addresses, for inner()'s HOST_PREF rules: written before it starts,
            # kept current while it runs (link=lan's namespace has none of the host's)
            watch = HostWatch(env, os.path.join(rundir, HOST_FILE))
            helpers.append(watch.monitor)
        print(
            "network: the host's loopback unreachable; "
            + (
                "the guest itself on the LAN, as any device there (this host unreachable)"
                if lan_direct(env)
                else "the LAN and the host's other addresses reachable (LAN access)"
                if lan_access(env)
                else "the LAN and the host's addresses unreachable (no LAN access)"
            ),
            file=sys.stderr,
            flush=True,
        )
        ns = subprocess.Popen(
            [
                "unshare",
                "-rn",
                *(["-m", "--propagation", "private"] if lan else []),
                sys.executable,
                "-m",
                "beanjar.network.netns",
                "lan" if lan else "inner",
                *argv,
            ],
            env=penv,
            preexec_fn=_die_with_parent,
        )
        # Wait until the child has its own network namespace, then attach the uplink to it:
        # slirp4netns is a user-mode NAT on the guest's network (interface up0, router .2, DNS
        # .3), with the host's loopback services unreachable (--disable-host-loopback). For
        # link=lan the uplink is the macvlan instead, and lan() attaches slirp4netns itself.
        _wait_netns(ns)
        if lan:
            made = subprocess.run(
                [
                    env["NET_LAN_HELPER"],
                    "macvlan",
                    env["NET_LAN_PARENT"],
                    env["NET_LAN_MAC"],
                    str(ns.pid),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if made.returncode != 0:
                raise Error(f"netns: no macvlan on {env['NET_LAN_PARENT']}: {made.stdout.strip()}")
            return _status(ns.wait())
        helpers.append(_uplink(ns, env, logdir))
        while ns.poll() is None:
            if watch is None:
                ns.wait()
            else:
                watch.step(1.0)
        return _status(ns.returncode)
    except _Stop as e:
        return 128 + e.sig
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # cleanup runs to the end
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if ns is not None and ns.poll() is None:
            ns.terminate()  # inner() stops QEMU and its helpers
            try:
                ns.wait(timeout=25)
            except subprocess.TimeoutExpired:
                ns.kill()
                ns.wait()
        _kill(helpers)
        shutil.rmtree(rundir, ignore_errors=True)
        if password:
            try:
                os.unlink(password)
            except OSError:
                pass


def _ip(env, *args):
    subprocess.run(["ip", *args], env=env, check=True)


def _ip_batch(env, commands, force=False):
    """Run commands (ip's argument lists) in one `ip -batch`; Error if one fails (force: run the
    rest anyway, and only say so)."""
    if not commands:
        return
    r = subprocess.run(
        ["ip", *(["-force"] if force else []), "-batch", "-"],
        env=env,
        input="".join(" ".join(c) + "\n" for c in commands),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if r.returncode != 0:
        if not force:
            raise Error(f"netns inner: ip -batch failed: {r.stdout.strip()}")
        print(f"netns inner: ip -batch: {r.stdout.strip()}", file=sys.stderr, flush=True)


class HostRules:
    """inner()'s HOST_PREF rules, kept to what HOST_FILE (HostWatch, outside) says: sync() reads it
    again when it changed and adds and deletes the difference."""

    def __init__(self, env, path, lan):
        self.env, self.path, self.lan, self.stamp, self.current = env, path, lan, None, []

    def sync(self):
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            stamp = None
        if stamp == self.stamp:
            return
        self.stamp = stamp
        wanted = read_host_file(self.path, self.lan)
        gone = [a for a in self.current if a not in wanted]
        new = [a for a in wanted if a not in self.current]
        if self.current or new:
            # the new ones first: an address never goes unblocked between the two
            _ip_batch(self.env, host_rules(new, "add"), force=bool(self.current))
            _ip_batch(self.env, host_rules(gone, "del"), force=True)
            if new or gone:
                print(
                    f"netns inner: host addresses refused: {', '.join(wanted) or 'none'}",
                    file=sys.stderr,
                    flush=True,
                )
        self.current = wanted


def _sysctl(key, value):
    with open("/proc/sys/net/ipv4/" + key, "w") as f:
        f.write(value + "\n")


def _can_connect(port):
    try:
        socket.create_connection(("127.0.0.1", int(port)), 0.2).close()
        return True
    except OSError:
        return False


class Proxy:
    """mitmweb as _start_proxy started it: process, and ready once it listens (its addon's ready
    file and its web port), which the launcher does not wait for (_wait_guest polls it)."""

    def __init__(self, process, ready_file, web_port, log, info=None):
        self.process, self.ready_file, self.web_port, self.log = process, ready_file, web_port, log
        self.info = info or {}  # what RUN_DIR/MITM_INFO_FILE says of it
        self.started = time.monotonic()
        self.ready = False

    def _device(self):
        """Whether mitmweb's TUN device exists in this namespace yet."""
        try:
            socket.if_nametoindex(TUN_DEV)
            return True
        except OSError:
            return False

    def poll(self):
        """True once it is ready, False if it exited or has not been ready for PROXY_WAIT s, None
        while it is still starting."""
        if self.ready:
            return True
        if self.process.poll() is not None:
            return False
        if os.path.exists(self.ready_file) and _can_connect(self.web_port) and self._device():
            self.ready = True
            return True
        if time.monotonic() - self.started >= PROXY_WAIT:
            return False
        return None


# How long mitmweb may take to listen (s): on an RK3399 board with mitmproxy on NFS its first start
# took 10 s. The guest's TCP and UDP wait (held, _hold) until then.
PROXY_WAIT = 60
# The policy rules' priorities: the interception (_intercept) before the hold (_hold).
INTERCEPT_PREF = "100"
HOLD_PREF = "101"


def mitmweb_argv(mitmweb, tree, env, confdir, web_port):
    """mitmweb's command line: its TUN device TUN_DEV, with mitm_addon.py."""
    return [
        mitmweb,
        "--mode",
        f"tun:{TUN_DEV}",
        "--web-host",
        "127.0.0.1",
        "--web-port",
        web_port,
        "--no-web-open-browser",
        "--set",
        f"confdir={confdir}",
        "--set",
        "ssl_insecure=true",
        "--set",
        # every TCP port is carried, server-first protocols too (SSH, SMTP...), which lazy would
        # hold until the client spoke: eager dials upstream as the guest connects
        "connection_strategy=eager",
        "--set",
        "show_ignored_hosts=true",
        "--set",
        "termlog_verbosity=warn",
        "--set",
        "flow_detail=0",
        "--set",
        f"max_flows={env.get('MITM_FLOWS') or settings.MITM_FLOWS}",  # mitm_addon.py's FlowLimit
        "--set",
        f"max_flow_bytes={env.get('MITM_FLOW_BYTES') or 0}",
        "-s",
        tree.src("host", "helpers", "network", "mitm_addon.py"),
    ]


# mitmweb's sandbox (mitmweb_sandbox): a bubblewrap mount namespace, so that what reaches its API
# (the password, or a script on its origin) cannot have it load or touch the user's files. It sees
# the system (/usr, /etc, read-only), its own installation and interpreter (the pipx venv,
# read-only), mitm_addon.py and the password file (read-only), and writes only its confdir (the
# library's CA, where it keeps its certificates), MITM_STATE_DIR and NETSTUB_LOG. /proc (its own pid
# namespace), /dev (with /dev/net/tun for its TUN device) and /tmp are private. Not $HOME, not the
# VM's shares, not the run or socket directories. It keeps CAP_NET_ADMIN of the namespace's
# capabilities, which `--mode tun` needs to make and configure TUN_DEV; the network namespace is the
# one it starts in, so tun mode works as before. No user namespace of its own (bwrap runs as the
# namespace's root): capabilities in a nested one would not reach this network namespace. The
# environment is cleared but for SANDBOX_ENV. mitm_addon.py's Freeze adds the other half: the
# options that load code or files, open listeners or change the password are frozen once it runs.
MITM_STATE_DIR = "mitmweb.d"  # in NETNS_RUNDIR: mitmweb's ready file (NETNS_MITM_READY)
SANDBOX_ENV = (
    "NETSTUB_LOG",
    "NETSTUB_STREAM_LARGE",
    "NETSTUB_WEB_PASSWORD_FILE",
    "NETNS_MITM_READY",
    "NET_LG_SERVICES",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)
SANDBOX_ROOT_DIRS = ("bin", "sbin", "lib", "lib32", "lib64", "libx32")  # links into /usr if merged


def _prefix(executable):
    """An executable's installation: the directory above its bin/ (a venv, pipx's, a pip --user
    install's ~/.local, /usr), symlinks resolved."""
    return os.path.dirname(os.path.dirname(os.path.realpath(executable)))


def _under(path, top):
    return path == top or path.startswith(top.rstrip("/") + "/")


def mitmweb_sandbox(bwrap, mitmweb, env, confdir, addon, state):
    """bwrap's command line up to mitmweb's own (mitmweb_argv, with mitmweb's real path): mitmweb
    in the sandbox described above, writing confdir, state (MITM_STATE_DIR) and NETSTUB_LOG.
    Raises Error if mitmweb's installation is the home directory itself, which would bind all of it."""
    home = os.path.realpath(os.path.expanduser("~"))
    tops = []
    for exe in (mitmweb, mitmtool.python(mitmweb)):
        top = _prefix(exe) if exe else None
        if not top or top == "/" or _under(top, "/usr") or top in tops:
            continue  # the system's, bound below
        if home != "/" and _under(home, top):
            raise Error(f"netns inner: {exe} is installed in {top}, which holds the home directory")
        tops.append(top)
    cmd = [
        bwrap,
        # its own pid namespace (for /proc) and IPC; it dies with its parent
        "--unshare-pid",
        "--unshare-ipc",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CAP_NET_ADMIN",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for name in SANDBOX_ROOT_DIRS:
        path = "/" + name
        if os.path.islink(path):
            cmd += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            cmd += ["--ro-bind", path, path]
    cmd += ["--ro-bind", "/etc", "/etc"]
    resolv = os.path.realpath("/etc/resolv.conf")
    if not _under(resolv, "/etc") and not _under(resolv, "/usr"):
        cmd += ["--ro-bind-try", resolv, resolv]  # WSL's /mnt/wsl, systemd-resolved's /run
    cmd += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--dev-bind",
        "/dev/net/tun",
        "/dev/net/tun",
        "--tmpfs",
        "/tmp",  # first: what is bound below may be in the host's /tmp
    ]
    for top in tops:
        cmd += ["--ro-bind", top, top]
    # the addon and the table it reads (lg_hosts.py, beside it)
    ro = [addon, os.path.join(os.path.dirname(addon), "lg_hosts.py")]
    ro += [env.get("NETSTUB_WEB_PASSWORD_FILE")]
    rw = [confdir, state, env.get("NETSTUB_LOG")]
    for option, paths_ in (("--ro-bind", ro), ("--bind", rw)):
        for path in filter(None, paths_):
            # at the path mitmweb is given (a library reached through a link included)
            cmd += [option, os.path.realpath(path), os.path.abspath(path)]
    cmd += [
        "--chdir",
        "/",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
    ]
    # its log written as it goes: a stop kills it (bwrap does not pass SIGTERM on), so nothing
    # buffered would reach the log
    cmd += ["--setenv", "PYTHONUNBUFFERED", "1"]
    for key in SANDBOX_ENV:
        if env.get(key):
            cmd += ["--setenv", key, env[key]]
    prefix = _prefix(mitmweb)
    if prefix in tops and not os.path.exists(os.path.join(prefix, "pyvenv.cfg")):
        cmd += ["--setenv", "PYTHONUSERBASE", prefix]  # a pip --user install: HOME is not its home
    return cmd


def _write_info(env, rundir, info):
    """RUN_DIR/MITM_INFO_FILE: what this start's mitmweb is (mitm_info reads it)."""
    base = env.get("RUN_DIR") or rundir
    if not base:
        return
    path = os.path.join(base, MITM_INFO_FILE)
    try:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(info, f)
        os.replace(tmp, path)
    except OSError as e:
        print(f"netns inner: could not write {path}: {e}", file=sys.stderr)


def _announce_inspector(env, line):
    """Print the inspector's address line, unless its mitmweb cannot be used: the namespace then
    says the inspector is off and why (_start_proxy), and an address would only mislead."""
    if mitmtool.check(env).usable:
        print(line, file=sys.stderr, flush=True)


def _start_proxy(env, tree, rundir, logdir, web_port):
    """Start the optional inspector, without waiting for it: a Proxy, or None if it cannot start
    (no mitmweb, or one older than mitmtool.MIN_VERSION: the reason is printed and recorded in
    RUN_DIR/MITM_INFO_FILE). Proxy failures leave the routed network usable."""
    tool = mitmtool.check(env)
    info = {"version": tool.version, "path": tool.path, "problem": tool.problem}
    _write_info(env, rundir, info)
    if not tool.usable:
        print(
            f"netns inner: traffic inspector off, forwarding directly: {tool.problem}",
            file=sys.stderr,
            flush=True,
        )
        return None
    print(f"netns inner: mitmweb {tool.version} ({tool.path})", file=sys.stderr, flush=True)
    proxy = None
    try:
        # mitmweb on its TUN device, with the proxy's CA (NETSTUB_CA) as its confdir. Upstream
        # certificates are not verified (ssl_insecure): the proxy is a debug tool, and LG's servers
        # (*.lgtvsdp.com) come from a private root no proxy-side store has. A TLS handshake failure
        # may fail one request; subsequent connections pass through undecrypted. The failed
        # request is not replayed. Ordinary connection failures remain network failures.
        ca = subprocess.run(
            [tree.src("host", "helpers", "network", "tlsca.py")],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        env["NETSTUB_LOG"] = env.get("NETSTUB_LOG") or tree.build("netstub.log")
        mitm_log = os.path.join(logdir, "netstub-mitm.log")
        state = os.path.join(rundir, MITM_STATE_DIR)
        os.makedirs(state, mode=0o700, exist_ok=True)
        env["NETNS_MITM_READY"] = ready_file = os.path.join(state, "ready")
        if os.path.exists(ready_file):
            os.unlink(ready_file)
        for f in (mitm_log, env["NETSTUB_LOG"]):
            open(f, "a").close()
            os.chmod(f, 0o600)
        bwrap = mitmtool.sandbox(env)
        if not bwrap:
            raise OSError(mitmtool.SANDBOX_PROBLEM)
        confdir, mitmweb = os.path.dirname(ca), os.path.realpath(tool.path)
        addon = tree.src("host", "helpers", "network", "mitm_addon.py")
        with open(mitm_log, "w") as log:
            proxy = subprocess.Popen(
                mitmweb_sandbox(bwrap, mitmweb, env, confdir, addon, state)
                + mitmweb_argv(mitmweb, tree, env, confdir, web_port),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                preexec_fn=_die_with_parent,
            )
        return Proxy(proxy, ready_file, web_port, mitm_log, info)
    except (OSError, subprocess.CalledProcessError, Error) as exc:
        if proxy is not None:
            _kill([proxy])
        print(f"netns inner: mitmweb unavailable: {exc}; forwarding directly", file=sys.stderr)
        _write_info(env, rundir, dict(info, problem=f"mitmweb did not start: {exc}"))
        return None


# All of the guest's TCP and UDP, every port (DNS, NTP and the rest included); other protocols
# (ICMP: ping, traceroute's replies) are not matched and are forwarded to slirp, since mitmweb's
# TUN stack handles only TCP and UDP.
TUN_PROTOCOLS = ("tcp", "udp")


def intercept_rules(tap, action, dns_from=None):
    """The ip commands of _intercept: one policy rule per protocol (TUN_PROTOCOLS), for packets
    arriving on tap only, to TUN_TABLE. Packets to the namespace's own addresses (DHCP, the
    relays' replies, mock mode's resolver) meet the local table first (pref 0). dns_from (mock
    mode's MOCK_DNS): that resolver's own upstream queries, from that address to port 53, go to
    TUN_TABLE too, so mitmweb sees the guest's lookups as before."""
    out = [
        ["rule", action, "pref", INTERCEPT_PREF, "iif", tap, "ipproto", p, "lookup", TUN_TABLE]
        for p in TUN_PROTOCOLS
    ]
    if dns_from:
        out += [
            ["rule", action, "pref", INTERCEPT_PREF, "from", f"{dns_from}/32", "ipproto", p]
            + ["dport", "53", "lookup", TUN_TABLE]
            for p in TUN_PROTOCOLS
        ]
    return out


def hold_rules(tap, action):
    """The ip commands of _hold: the same selectors, blackholed."""
    return [
        ["rule", action, "pref", HOLD_PREF, "iif", tap, "ipproto", p, "blackhole"]
        for p in TUN_PROTOCOLS
    ]


def tun_route(action):
    """TUN_TABLE: every destination by mitmweb's device. The kernel drops it with the device when
    mitmweb exits; the rules then find no route there and fall through to the main table, the
    guest going straight out."""
    return ["route", action, "default", "dev", TUN_DEV, "table", TUN_TABLE]


def _intercept(env, tap, action):
    """Only the ingress policy rules divert traffic; the private table is inert without them."""
    if action == "add":
        _ip(env, *tun_route("replace"))
    for rule in intercept_rules(tap, action, MOCK_DNS if lg_mock(env) else None):
        _ip(env, *rule)


def _hold(env, tap, action):
    """While mitmweb starts, the guest's TCP and UDP are dropped (a blackhole rule), not refused or
    let through: a connection the guest opens then is not reset or failed, its SYN is sent again
    (TCP's retransmission: after 1, 3, 7, 15 s ...) and reaches mitmweb once it listens; a lost
    DNS query is asked again. Letting it through directly instead would have it reset when the
    interception starts, as its later packets would then be delivered to a mitmweb that has no
    such connection."""
    for rule in hold_rules(tap, action):
        _ip(env, *rule)


def _wait_guest(qemu, proxy, env, tap, hosts=None):
    """Keep the guest alive: start intercepting once its proxy (a Proxy) is ready, and withdraw
    the hold, or only the hold if it never gets ready; withdraw interception as soon as it exits;
    keep the host's addresses refused (hosts, a HostRules)."""
    pending = proxy
    while True:
        if hosts is not None:
            hosts.sync()
        if pending is not None:
            ready = pending.poll()
            if ready:
                _intercept(env, tap, "add")  # before the hold goes: no SYN slips past
                _hold(env, tap, "del")
                print(
                    f"netns inner: mitmweb ready {time.monotonic() - pending.started:.1f} s after "
                    "its start; intercepting",
                    file=sys.stderr,
                    flush=True,
                )
                pending = None
            elif ready is False:
                _kill([pending.process])
                _hold(env, tap, "del")
                _write_info(
                    env,
                    env.get("NETNS_RUNDIR"),
                    dict(pending.info, problem=f"mitmweb did not start (see {pending.log})"),
                )
                print(
                    f"netns inner: mitmweb unavailable (see {pending.log}); forwarding directly",
                    file=sys.stderr,
                    flush=True,
                )
                pending = proxy = None
        elif proxy is not None and proxy.process.poll() is not None:
            _intercept(env, tap, "del")
            print("netns inner: mitmweb exited; forwarding directly", file=sys.stderr)
            _write_info(
                env,
                env.get("NETNS_RUNDIR"),
                dict(proxy.info, problem=f"mitmweb exited (see {proxy.log})"),
            )
            proxy = None
        try:
            return _status(qemu.wait(timeout=0.2))
        except subprocess.TimeoutExpired:
            pass


def _discovery(env, mode, logdir):
    """Discovery has its own system Python and lifetime, independent of mitmweb."""
    log_path = os.path.join(logdir, "netns-discovery-" + mode + ".log")
    with open(log_path, "w") as log:
        helper = subprocess.Popen(
            ["/usr/bin/python3", "-m", "beanjar.network.discovery", mode],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log,
            preexec_fn=_die_with_parent,
        )
    with helper.stdout:
        ready = (
            select.select([helper.stdout], [], [], 10)[0] and helper.stdout.readline() == b"ready\n"
        )
    if not ready or helper.poll() is not None:
        _kill([helper])
        raise Error(f"netns {mode}: discovery helper failed (see {log_path})")
    return helper


def _lg_mock(env, tree, logdir, rundir):
    """Mock mode's resolver, online check and SDP responder (host/helpers/network/lg_mock.py) on
    MOCK_DNS, its upstream slirp4netns's resolver (DNS), its HTTPS certificates issued by the proxy's
    CA (NETSTUB_CA, which qemu.py sets in both modes when LG's services are mocked) and kept in the
    socket directory, started and ready (its "ready" line), else Error: a guest whose names it
    cannot resolve would only go without DNS, but one without it is no mock."""
    log_path = os.path.join(logdir, "netns-lgmock.log")
    tls = []
    if env.get("NETSTUB_CA"):
        # the CA made if it is missing, as for mitmweb (_start_proxy; tlsca.py locks it)
        subprocess.run(
            [tree.src("host", "helpers", "network", "tlsca.py")],
            env=env,
            stdout=subprocess.DEVNULL,
            check=True,
        )
        tls = ["--ca", env["NETSTUB_CA"], "--certs", os.path.join(rundir, LG_CERTS)]
    with open(log_path, "w") as log:
        helper = subprocess.Popen(
            [
                sys.executable,
                tree.src("host", "helpers", "network", "lg_mock.py"),
                "--listen",
                MOCK_DNS,
                "--upstream",
                DNS,
            ]
            + tls
            # the VM's country (qemu.plan from COUNTRY): where the mock's SDP tells the TV it is
            + (["--country", env["LG_MOCK_COUNTRY"]] if env.get("LG_MOCK_COUNTRY") else []),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log,
            preexec_fn=_die_with_parent,
        )
    with helper.stdout:
        ready = (
            select.select([helper.stdout], [], [], 10)[0] and helper.stdout.readline() == b"ready\n"
        )
    if not ready or helper.poll() is not None:
        _kill([helper])
        raise Error(f"netns inner: LG services mock failed (see {log_path})")
    print(
        "netns inner: LG services mocked: telemetry and cloud hosts refused, online check, SDP and "
        f"LG's web hosts answered by {MOCK_DNS}" + ("" if tls else " (no HTTPS: no NETSTUB_CA)"),
        file=sys.stderr,
        flush=True,
    )
    return helper


def inner(argv, tree=paths.TREE):
    """Inside the namespace (run() runs this under unshare -rn and attaches the uplink, up0): the
    TAP, proxy ARP between it and the uplink, the routing of TCP and UDP into mitmweb, DHCP, the
    port bridges into the guest, then QEMU in the foreground. The helpers are killed when QEMU
    exits."""
    env = _sbin_path(dict(os.environ))
    rundir = env.get("NETNS_RUNDIR")
    if not rundir:
        raise Error("netns inner: NETNS_RUNDIR not set")
    tap = env.get("NETNS_TAP") or "tap0"
    web_port = env.get("NETSTUB_WEB_PORT") or "18083"
    logdir = env.get("NETNS_LOG_DIR") or tree.build()
    helpers, qemu = [], None
    _raise_on(signal.SIGTERM, signal.SIGINT)
    try:
        # the TAP: QEMU's NIC. Its one address is the DHCP server's; the guest is routed to it.
        _ip(env, "link", "set", "lo", "up")
        _ip(env, "tuntap", "add", tap, "mode", "tap")
        _ip(env, "addr", "add", f"{DHCP_SERVER}/32", "dev", tap)
        _ip(env, "link", "set", tap, "up")
        _ip(env, "route", "add", f"{GUEST}/32", "dev", tap)

        def default_route():
            return subprocess.run(
                ["ip", "route", "show", "default"], env=env, stdout=subprocess.PIPE, text=True
            ).stdout.strip()

        # the uplink (run() attaches it once this namespace exists)
        for _ in range(100):
            if default_route():
                break
            time.sleep(0.1)
        if not default_route():
            raise Error(
                f"netns inner: slirp4netns uplink did not come up (see {logdir}/netns-uplink.log)"
            )

        # routing: the guest's packets are forwarded to slirp unchanged, and slirp's back to the
        # guest, each side's ARP for the other's addresses answered here (proxy ARP). Its TCP and
        # UDP go to mitmweb's TUN device instead once it is ready (_intercept, _wait_guest).
        # what the guest may not reach (BLOCK_PREF, HOST_PREF), before it can send anything
        lan = lan_access(env)
        _ip_batch(env, block_rules(lan))
        mock = lg_mock(env)
        env["NET_LG_SERVICES"] = "mock" if mock else "real"  # for mitm_addon.py too
        if mock:
            _ip_batch(env, lg_rules(tap, _lg_hosts(tree).refused_addresses()))
        hosts = HostRules(env, os.path.join(rundir, HOST_FILE), lan)
        hosts.sync()
        # every refused connection told at once: without this the kernel's ICMP rate limit (one a
        # second) leaves a guest's second refused SYN in a second unanswered, to time out instead
        _sysctl("icmp_ratelimit", "0")
        _sysctl("ip_forward", "1")
        for dev in (tap, "up0"):
            _sysctl(f"conf/{dev}/proxy_arp", "1")
        # DHCP (fixed lease; router and DNS slirp's, the server the TAP's own address)
        with open(os.path.join(logdir, "netns-helper.log"), "a") as log:
            helpers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        tree.src("host", "helpers", "network", "netns_dhcp.py"),
                        "--iface",
                        tap,
                        "--server",
                        DHCP_SERVER,
                        "--lease",
                        GUEST,
                        "--router",
                        ROUTER,
                        "--dns",
                        guest_dns(env),
                    ],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    preexec_fn=_die_with_parent,
                )
            )

        # mock mode's resolver and online check (lg_mock.py), before the guest can ask
        if mock:
            helpers.append(_lg_mock(env, tree, logdir, rundir))
        # Runtime files stay private even when the optional inspector is absent.
        os.umask(0o077)
        # mitmweb starts beside QEMU: the guest's TCP and UDP are held until it listens (_hold,
        # _wait_guest), long before the guest's network is up on any board measured
        proxy = _start_proxy(env, tree, rundir, logdir, web_port) if inspected(env) else None
        if proxy is not None:
            helpers.append(proxy.process)
            _hold(env, tap, "add")
        # the inspector's socket for the host-side relay, and the loopback-forwarded ports into the
        # guest. The unix sockets live on the shared filesystem, so the host-side relay (run(),
        # outside the namespace) reaches them; this side connects on to the guest. Replaces slirp
        # hostfwd, which a TAP netdev does not provide.
        pairs = [f"unix:{rundir}/mitmweb.sock=tcp:127.0.0.1:{web_port}"] if inspected(env) else []
        pairs += [
            f"unix:{rundir}/fwd-{hp}.sock=tcp:{GUEST}:{gp}"
            for hp, gp in (f.split(":", 1) for f in env.get("NETNS_FWD", "").split())
        ]
        if pairs:
            helpers.append(
                _relay(
                    tree, pairs, env, os.path.join(logdir, "netns-relay-inner.log"), "netns inner"
                )
            )
        if env.get("NETNS_LAN_ADDRESS"):
            helpers.append(_discovery(env, "inner", logdir))
        # QEMU in the foreground, with the memfd of its swap disk if it has one (hostswap.py)
        fds = hostswap.open_fds(argv)
        try:
            qemu = subprocess.Popen(argv, env=env, preexec_fn=_die_with_parent, pass_fds=fds)
        finally:
            hostswap.close_fds(fds)
        return _wait_guest(qemu, proxy, env, tap, hosts)
    except _Stop as e:
        return 128 + e.sig
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # cleanup runs to the end
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if qemu is not None and qemu.poll() is None:
            qemu.terminate()
            try:
                qemu.wait(timeout=20)
            except subprocess.TimeoutExpired:
                qemu.kill()
        _kill(helpers)


def enter_argv(pid, argv):
    """argv run in the network namespace of a running VM's QEMU (pid: inner()'s namespace, and
    its user namespace, which owns it), as `nsenter` does it; else Error (QEMU is in this
    namespace: link=off). What runs there is the guest's neighbour on the TAP and reaches what the
    namespace does, nothing of the host's but what it has open already; the guest reaches it at
    DHCP_SERVER, the TAP's address (the way it reaches lg_mock.py), on a port it listens on
    there. scripts/regress --tv's test server runs so (tests/regress/http-token.py)."""
    try:
        theirs = os.readlink(f"/proc/{pid}/ns/net")
    except OSError as e:
        raise Error(f"netns: no process {pid}: {e.strerror}") from None
    if theirs == os.readlink("/proc/self/ns/net"):
        raise Error(f"netns: process {pid} is in this network namespace (the VM has link=off)")
    if not shutil.which("nsenter"):
        raise Error("netns: nsenter not found (util-linux)")
    return ["nsenter", "-t", str(pid), "-U", "-n", "--preserve-credentials", "--", *argv]


def _lan_static(env, address):
    """NET_ADDRESS's static "A.B.C.D/NN,GATEWAY" on lan0: the lease lan() would have had, the
    gateway as router and DNS server."""
    iface, gateway = (x.strip() for x in address.split(","))
    iface = ipaddress.IPv4Interface(iface)
    _ip(env, "addr", "add", str(iface), "dev", LAN_IF)
    _ip(env, "route", "add", "default", "via", gateway, "dev", LAN_IF)
    return {
        "address": str(iface.ip),
        "prefix": iface.network.prefixlen,
        "router": gateway,
        "dns": [gateway],
        "lease": 0,
    }


def dhcp_argv(env, script):
    """The DHCP client of link=lan in the LAN namespace: it sends NET_LAN_HOSTNAME, when set, as
    its hostname (option 12), which the LAN's router may register as a name."""
    argv = ["busybox", "udhcpc", "-f", "-R", "-i", LAN_IF, "-t", "5", "-T", "2", "-A", "5"]
    if env.get("NET_LAN_HOSTNAME"):
        argv += ["-x", f"hostname:{env['NET_LAN_HOSTNAME']}"]
    return argv + ["-s", script]


def _sysctl6(key, value):
    """An IPv6 sysctl of this namespace; nothing if the kernel has no IPv6."""
    try:
        with open("/proc/sys/net/ipv6/" + key, "w") as f:
            f.write(value + "\n")
    except FileNotFoundError:
        pass


class LeaseWatch:
    """link=lan with mode=direct: reads the frames on tap (a packet socket: it sees both ways,
    before the tc redirect takes them) for the LAN server's DHCPACKs to the guest's MAC, and writes
    each lease to path (RUN_DIR/lan.json, source "guest") for status, list and the GUI. It only
    reads; a guest with a static address set on the TV has no lan.json."""

    def __init__(self, tap, mac, path, parent):
        self.mac, self.path, self.parent = mac, path, parent
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        self.sock.bind((tap, 0))
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                frame = self.sock.recv(65536)
            except OSError:
                return
            lease = guest_lease(frame, self.mac)
            if lease:
                lease.update(parent=self.parent, mac=self.mac, source="guest")
                try:
                    write_lan_info(self.path, lease)
                except OSError:
                    pass


def _lan_direct(argv, env, info):
    """link=lan with mode=direct, in the LAN namespace once lan0 is up: the TAP, joined to lan0 by
    tc (join_commands), the lease watch, then QEMU, whose Popen it returns. The guest does its own
    DHCP on the LAN with its own MAC (lan0's); this namespace has no address on either."""
    tap = env.get("NETNS_TAP") or "tap0"
    mac = env.get("NET_LAN_MAC", "")
    # every multicast group to the guest (mDNS, SSDP): a macvlan otherwise passes only those on its
    # own list, and the guest's IGMP joins never reach that list
    _ip(env, "link", "set", LAN_IF, "allmulticast", "on")
    shown = subprocess.run(
        ["ip", "-o", "link", "show", LAN_IF], env=env, stdout=subprocess.PIPE, text=True
    ).stdout.split()
    mtu = shown[shown.index("mtu") + 1] if "mtu" in shown else "1500"
    _ip(env, "tuntap", "add", tap, "mode", "tap")
    _ip(env, "link", "set", tap, "mtu", mtu, "up")
    for cmd in join_commands(LAN_IF, tap):
        r = subprocess.run(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        if r.returncode != 0:
            raise Error(
                f"netns lan: could not join {tap} to {LAN_IF} ({' '.join(cmd)}: {r.stdout.strip()});"
                " link=lan with mode=direct needs tc and the kernel's sch_ingress (clsact),"
                " cls_matchall and act_mirred"
            )
    LeaseWatch(tap, mac, info, env.get("NET_LAN_PARENT"))
    print(
        f"network: the guest on the LAN of {env.get('NET_LAN_PARENT')} with MAC {mac}, its own"
        " lease and ports; nothing of Beanjar's in its traffic, and this host cannot reach it",
        file=sys.stderr,
        flush=True,
    )
    fds = hostswap.open_fds(argv)
    try:
        return subprocess.Popen(argv, env=env, preexec_fn=_die_with_parent, pass_fds=fds)
    finally:
        hostswap.close_fds(fds)


def lan(argv, tree=paths.TREE):
    """The LAN namespace (link=lan; run() runs this under `unshare -rnm` and has lan0 made in it):
    lan0's address (a DHCP lease or NET_ADDRESS), DNS, RUN_DIR/lan.json, inner() with its uplink
    and a relay of the TV-service ports and the inspector on the LAN address; all of it undone
    when QEMU exits."""
    env = _sbin_path(dict(os.environ))
    rundir = env.get("NETNS_RUNDIR")
    if not rundir:
        raise Error("netns lan: NETNS_RUNDIR not set")
    logdir = env.get("NETNS_LOG_DIR") or tree.build()
    info = os.path.join(env.get("RUN_DIR") or rundir, LAN_FILE)
    address = env.get("NET_ADDRESS") or "dhcp"
    env["NETSTUB_WEB_PORT"] = env.get("NETSTUB_WEB_PORT") or "18083"
    helpers, child = [], None
    _raise_on(signal.SIGTERM, signal.SIGINT)
    try:

        def has_lan():
            return (
                subprocess.run(
                    ["ip", "link", "show", LAN_IF],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                == 0
            )

        for _ in range(100):  # run() has the helper make it once this namespace exists
            if has_lan():
                break
            time.sleep(0.1)
        if not has_lan():
            raise Error(f"netns lan: no {LAN_IF} from the helper")
        _ip(env, "link", "set", "lo", "up")
        direct = lan_direct(env)
        if direct:
            # lan0 has the guest's MAC: this namespace must not speak on it itself (IPv6
            # link-local, router and neighbour solicitations), only carry the guest's frames
            for dev in ("default", LAN_IF):
                _sysctl6(f"conf/{dev}/disable_ipv6", "1")
        # A macvlan comes up only while no other on the parent has its MAC: a previous run's lan0
        # outlives its namespace's processes until the kernel lets that namespace go (its TCP
        # sockets' teardown can take minutes after a kill), so wait for it, up to LAN_MAC_WAIT.
        end = time.monotonic() + LAN_MAC_WAIT
        while True:
            up = subprocess.run(
                ["ip", "link", "set", LAN_IF, "up"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if up.returncode == 0:
                break
            if "in use" not in up.stdout or time.monotonic() > end:
                raise Error(
                    f"netns lan: {LAN_IF} on {env.get('NET_LAN_PARENT')} did not come up: {up.stdout.strip()}"
                    + (
                        " (the VM's MAC is still another interface's there: a previous run's, which the"
                        " kernel has not let go yet; try again later)"
                        if "in use" in up.stdout
                        else ""
                    )
                )
            time.sleep(1)
        if direct:
            child = _lan_direct(argv, env, info)
            return _status(child.wait())
        if address == "dhcp":
            lease_file = os.path.join(rundir, "lan-lease.json")
            with open(os.path.join(logdir, "netns-dhcp.log"), "w") as log:
                dhcp = subprocess.Popen(
                    dhcp_argv(env, tree.src("host", "helpers", "network", "lan_dhcp.py")),
                    env=dict(env, LAN_LEASE_FILE=lease_file),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    preexec_fn=_die_with_parent,
                )
            helpers.append(dhcp)
            end = time.monotonic() + LAN_LEASE_WAIT
            while not os.path.exists(lease_file):
                if dhcp.poll() is not None or time.monotonic() > end:
                    raise Error(
                        f"netns lan: no DHCP lease on {env.get('NET_LAN_PARENT')} "
                        f"(see {logdir}/netns-dhcp.log)"
                    )
                time.sleep(0.2)
            with open(lease_file) as f:
                lease = json.load(f)
        else:
            lease = _lan_static(env, address)
        # the lease's DNS for this namespace's own lookups (slirp4netns's forwarder reads it)
        if lease.get("dns"):
            resolv = os.path.join(rundir, "resolv.conf")
            with open(resolv, "w") as f:
                f.write("".join(f"nameserver {d}\n" for d in lease["dns"]))
            if (
                subprocess.run(["mount", "--bind", resolv, "/etc/resolv.conf"], env=env).returncode
                != 0
            ):
                print(
                    "netns lan: could not use the lease's DNS; the host's /etc/resolv.conf stays",
                    file=sys.stderr,
                    flush=True,
                )
        lease.update(
            parent=env.get("NET_LAN_PARENT"),
            mac=env.get("NET_LAN_MAC"),
            source="dhcp" if address == "dhcp" else "static",
        )
        write_lan_info(info, lease)
        print(
            f"network: LAN address {lease['address']}/{lease['prefix']} on {lease['parent']} "
            f"({lease['source']}), MAC {lease['mac']}",
            file=sys.stderr,
            flush=True,
        )
        env["NETNS_LAN_ADDRESS"] = lease["address"]
        env["NET_LAN_ACCESS"] = "on"  # on the LAN by definition; the loopback rules stay
        child = subprocess.Popen(
            ["unshare", "-rn", sys.executable, "-m", "beanjar.network.netns", "inner", *argv],
            env=env,
            preexec_fn=_die_with_parent,
        )
        _wait_netns(child)
        helpers.append(_uplink(child, env, logdir))
        # the TV-service ports, the inspector (its password as on the host side) and VNC on the
        # LAN address (every address of this namespace: lo and lan0)
        helpers.append(
            _relay(
                tree,
                lan_relay_pairs(env, rundir),
                env,
                os.path.join(logdir, "netns-relay-lan.log"),
                "netns lan",
            )
        )
        _announce_inspector(
            env,
            f"traffic inspector (mitmweb) on the LAN: http://{lease['address']}:{env['NETSTUB_WEB_PORT']}",
        )
        print(
            f"VNC (beanjar vnc, while it runs) on the LAN: vnc://{lease['address']}:{VNC_PORT}",
            file=sys.stderr,
            flush=True,
        )
        helpers.append(_discovery(env, "outer", logdir))
        return _status(child.wait())
    except _Stop as e:
        return 128 + e.sig
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # cleanup runs to the end
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if child is not None and child.poll() is None:
            child.terminate()  # inner() stops QEMU and its helpers
            try:
                child.wait(timeout=25)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        _kill(helpers)  # udhcpc releases the lease (-R) as it exits
        # lan0 now, rather than with the namespace, which sockets of the guest's can hold for minutes
        # (the next start's lan0 needs the MAC)
        subprocess.run(
            ["ip", "link", "del", LAN_IF],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            os.unlink(info)
        except OSError:
            pass


def _qmp(path, qemu, before_cont=None):
    """set_link net0 down and cont over QEMU's QMP socket at path; QEMU (a Popen) paused until then."""
    for _ in range(300):
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(path)
            break
        except OSError:
            s.close()
            if qemu.poll() is not None:
                raise Error(f"net-off: QEMU exited ({_status(qemu.returncode)})") from None
            time.sleep(0.1)
    else:
        raise Error("net-off: no QMP socket")
    with s, s.makefile("rw") as f:
        f.readline()  # greeting

        def cmd(command, **args):
            f.write(json.dumps({"execute": command, "arguments": args}) + "\n")
            f.flush()
            while True:
                reply = json.loads(f.readline())
                if "return" in reply:
                    return
                if "error" in reply:
                    raise Error(f"net-off: {command}: {reply['error'].get('desc')}")

        cmd("qmp_capabilities")
        cmd("set_link", name="net0", up=False)
        if before_cont is None:
            cmd("cont")
        else:
            before_cont()


def off(argv, env, before_cont=None):
    """Run QEMU (argv) for link=off; its exit status."""
    d = tempfile.mkdtemp(prefix="webos-netoff-", dir=env.get("TMPDIR") or "/tmp")
    qemu = None
    _raise_on(signal.SIGTERM, signal.SIGINT)
    try:
        fds = hostswap.open_fds(argv)  # its swap disk's memfd, if it has one
        try:
            qemu = subprocess.Popen(
                [*argv, "-S", "-qmp", f"unix:{d}/qmp.sock,server=on,wait=off"],
                env=env,
                pass_fds=fds,
            )
        finally:
            hostswap.close_fds(fds)
        try:
            _qmp(os.path.join(d, "qmp.sock"), qemu, before_cont)
        except (Error, OSError, ValueError) as e:
            print(e, file=sys.stderr)
            raise Error("net-off: could not set the link down; QEMU stopped") from None
        print("network: off (NIC present, link down)", file=sys.stderr, flush=True)
        rc = qemu.wait()
        qemu = None
        return _status(rc)
    except _Stop as e:
        return 128 + e.sig
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # cleanup runs to the end
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if qemu is not None and qemu.poll() is None:
            qemu.terminate()
            try:
                qemu.wait(timeout=20)
            except subprocess.TimeoutExpired:
                qemu.kill()
        shutil.rmtree(d, ignore_errors=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2 or argv[0] not in ("inner", "lan"):
        print("usage: python3 -m beanjar.network.netns inner|lan QEMU-ARGV...", file=sys.stderr)
        return 2
    try:
        return (inner if argv[0] == "inner" else lan)(argv[1:])
    except (Error, base.Error, OSError, subprocess.CalledProcessError) as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
