# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""netns.py's reachability rules: what the guest may not reach (the host's loopback always; the LAN
and the host's own addresses without LAN access), the host-address list HostWatch writes and
HostRules applies, and the relays of mode=direct, which has no inspector."""

import ipaddress
import json
import os
from unittest import mock

import pytest
from beanjar.network import netns


def nets(lan):
    return [ipaddress.ip_network(n) for n in netns.block_nets(lan)]


def blocked(address, lan):
    return any(ipaddress.ip_address(address) in n for n in nets(lan))


@pytest.mark.parametrize("lan", [False, True])
def test_loopback_always_refused(lan):
    for a in ("127.0.0.1", "127.0.1.5", "127.255.255.254", "0.0.0.0", "0.1.2.3"):
        assert blocked(a, lan), a


@pytest.mark.parametrize("lan", [False, True])
def test_the_guests_own_network_never(lan):
    # the guest, slirp's router and DNS, the namespace's DHCP server: all of 10.0.2.0/24
    for a in ("10.0.2.15", "10.0.2.2", "10.0.2.3", "10.0.2.254", "10.0.2.0", "10.0.2.255"):
        assert not blocked(a, lan), a


def test_without_lan_access_the_lan_is_refused():
    for a in (
        "10.0.0.1",
        "10.0.1.255",
        "10.0.3.0",
        "10.255.255.255",
        "172.16.0.1",
        "172.17.0.1",  # docker0
        "172.31.255.255",
        "192.168.1.1",
        "100.64.0.1",  # shared address space (CGNAT, Tailscale)
        "100.127.255.255",
        "169.254.169.254",
        "224.0.0.251",  # mDNS
        "239.255.255.250",  # SSDP
    ):
        assert blocked(a, False), a
    for a in ("8.8.8.8", "1.1.1.1", "172.32.0.1", "100.128.0.1", "192.169.0.1", "93.184.215.14"):
        assert not blocked(a, False), a


def test_with_lan_access_only_loopback():
    assert netns.block_nets(True) == ["127.0.0.0/8", "0.0.0.0/8"]
    for a in ("192.168.1.1", "10.0.0.1", "172.17.0.1", "169.254.1.1", "239.255.255.250"):
        assert not blocked(a, True), a


def test_ranges_do_not_overlap_the_guests_network():
    own = ipaddress.ip_network(netns.CIDR)
    for n in nets(False):
        assert not n.overlaps(own), n


def test_rules_are_prohibit_before_the_interception():
    rules = netns.block_rules(False)
    assert rules[0] == ["rule", "add", "pref", "90", "to", "127.0.0.0/8", "prohibit"]
    assert all(r[-1] == "prohibit" and "iif" not in r for r in rules)
    assert int(netns.BLOCK_PREF) < int(netns.HOST_PREF) < int(netns.INTERCEPT_PREF)
    assert int(netns.HOST_PREF) < int(netns.HOLD_PREF)
    assert netns.block_rules(True, "del")[1] == [
        "rule", "del", "pref", "90", "to", "0.0.0.0/8", "prohibit",
    ]  # fmt: skip


def test_host_blocks_only_what_the_ranges_miss():
    host = [
        ("192.168.1.5", False),  # LAN_NETS has it
        ("203.0.113.7", False),
        ("8.8.4.4", False),
        ("203.0.113.7", False),
        ("127.0.0.1", True),  # LOOPBACK_NETS has it
        ("100.100.1.1", False),
        (
            "10.0.2.15",
            False,
        ),  # slirp's network, not the host's: refusing it would cut the guest off
        ("fe80::1", False),
        ("garbage", False),
        ("10.255.255.254", True),  # WSL's DNS on lo: the host's loopback
    ]
    assert netns.host_blocks(host, False) == ["8.8.4.4", "203.0.113.7"]
    # with LAN access only the loopback interface's, which no range covers then
    assert netns.host_blocks(host, True) == ["10.255.255.254"]
    assert netns.host_rules(["203.0.113.7"], "add") == [
        ["rule", "add", "pref", "91", "to", "203.0.113.7/32", "prohibit"]
    ]


def test_parse_addresses():
    out = json.dumps(
        [
            {
                "ifname": "lo",
                "flags": ["LOOPBACK", "UP"],
                "addr_info": [
                    {"family": "inet", "local": "127.0.0.1"},
                    {"family": "inet", "local": "10.255.255.254"},
                ],
            },
            {
                "ifname": "eth0",
                "flags": ["BROADCAST", "UP"],
                "addr_info": [
                    {"family": "inet", "local": "203.0.113.7", "prefixlen": 24},
                    {"family": "inet", "local": "192.168.1.5"},
                ],
            },
            {"ifname": "wg0"},
        ]
    )
    assert netns.parse_addresses(out) == [
        ("127.0.0.1", True),
        ("10.255.255.254", True),
        ("203.0.113.7", False),
        ("192.168.1.5", False),
    ]
    assert netns.parse_addresses("") == []
    assert netns.parse_addresses("not json") == []


def test_host_file_round_trip(tmp_path):
    path = str(tmp_path / netns.HOST_FILE)
    netns.write_host_file(path, [("10.255.255.254", True), ("203.0.113.7", False)])
    with open(path) as f:
        assert f.read() == "10.255.255.254 lo\n203.0.113.7\n"
    # without LAN access 10.0.0.0/8's rule covers the loopback interface's address
    assert netns.read_host_file(path, False) == ["203.0.113.7"]
    assert netns.read_host_file(path, True) == ["10.255.255.254"]
    assert netns.read_host_file(str(tmp_path / "none"), False) == []


class FakeMonitor:
    def __init__(self):
        self.r, self.w = os.pipe()
        self.stdout = os.fdopen(self.r, "rb", buffering=0)
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def wait(self):
        self.alive = False
        return 0


def test_host_watch_follows_the_hosts_addresses(tmp_path):
    """HostWatch writes the list before the namespace starts and again after an address event,
    not only from a snapshot; HostRules adds the new address before deleting the old."""
    path = str(tmp_path / netns.HOST_FILE)
    addresses = [[("127.0.0.1", True), ("192.168.1.5", False), ("203.0.113.7", False)]]
    monitor = FakeMonitor()
    with (
        mock.patch.object(netns, "host_addresses", lambda env: addresses[0]),
        mock.patch.object(netns.subprocess, "Popen", lambda *a, **k: monitor),
    ):
        watch = netns.HostWatch({}, path)
        assert netns.read_host_file(path, False) == ["203.0.113.7"]
        calls = []
        rules = netns.HostRules({}, path, False)
        with mock.patch.object(
            netns, "_ip_batch", lambda env, cmds, force=False: calls.append((cmds, force))
        ):
            rules.sync()
            assert calls == [(netns.host_rules(["203.0.113.7"], "add"), False), ([], True)]
            calls.clear()
            rules.sync()  # unchanged: nothing run
            assert calls == []
            # the host's public address changes: an event, and the file follows at once
            addresses[0] = [("127.0.0.1", True), ("198.51.100.9", False)]
            os.write(monitor.w, b"2: eth0 inet 198.51.100.9/24 ...\n")
            watch.step(1.0)
            assert netns.read_host_file(path, False) == ["198.51.100.9"]
            rules.sync()
            assert calls == [
                (netns.host_rules(["198.51.100.9"], "add"), True),
                (netns.host_rules(["203.0.113.7"], "del"), True),
            ]
            # the monitor gone: read again on a short poll instead
            monitor.alive = False
            addresses[0] = [("127.0.0.1", True)]
            watch.last -= netns.HOST_POLL_BLIND
            with mock.patch.object(netns.time, "sleep", lambda s: None):
                watch.step(0.0)
            assert netns.read_host_file(path, False) == []
    os.close(monitor.w)
    monitor.stdout.close()


def test_host_watch_refuses_to_start_blind(tmp_path):
    with (
        mock.patch.object(netns, "host_addresses", lambda env: None),
        pytest.raises(netns.Error, match="could not list the host's addresses"),
    ):
        netns.HostWatch({}, str(tmp_path / netns.HOST_FILE))


def test_initial_rules_fail_closed():
    """The first HOST_PREF rules (and the BLOCK_PREF ones) must go in, or the guest does not start:
    ip -batch without -force, its failure an Error."""
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw["input"]))
        return mock.Mock(returncode=1, stdout="RTNETLINK answers: Operation not permitted")

    with mock.patch.object(netns.subprocess, "run", run):
        with pytest.raises(netns.Error, match="ip -batch failed"):
            netns._ip_batch({}, netns.block_rules(False))
    assert seen[0][0] == ["ip", "-batch", "-"]
    assert seen[0][1].splitlines()[0] == "rule add pref 90 to 127.0.0.0/8 prohibit"


def test_lg_mock_is_the_default():
    assert netns.lg_mock({})
    assert netns.lg_mock({"NET_LG_SERVICES": "mock"})
    assert not netns.lg_mock({"NET_LG_SERVICES": "real"})


def test_guest_dns_is_the_mock_unless_real():
    """The name server the namespace's DHCP hands out: lg_mock.py's (the TAP's own address) in mock
    mode, slirp4netns's resolver with real LG services, as before."""
    assert netns.guest_dns({}) == netns.MOCK_DNS == netns.DHCP_SERVER
    assert netns.guest_dns({"NET_LG_SERVICES": "real"}) == netns.DNS == "10.0.2.3"


def test_lg_rules_refuse_literal_addresses_and_other_resolvers():
    """lg_services=mock: the table's literal addresses refused for everyone (no iif: mitmweb's
    upstream too), the guest's DNS to any server but the namespace's own refused, all at
    BLOCK_PREF, before the interception. The resolver's address is the local table's (pref 0),
    which comes before these."""
    addresses = netns._lg_hosts().refused_addresses()
    assert {"156.147.69.32", "165.244.62.249"} <= set(addresses)  # update, remotediag
    rules = netns.lg_rules("tap0", addresses)
    assert rules[: len(addresses)] == [
        ["rule", "add", "pref", "90", "to", f"{a}/32", "prohibit"] for a in addresses
    ]
    assert rules[len(addresses) :] == [
        ["rule", "add", "pref", "90", "iif", "tap0", "ipproto", "udp", "dport", "53", "prohibit"],
        ["rule", "add", "pref", "90", "iif", "tap0", "ipproto", "tcp", "dport", "53", "prohibit"],
        ["rule", "add", "pref", "90", "iif", "tap0", "ipproto", "tcp", "dport", "853", "prohibit"],
    ]
    assert int(netns.BLOCK_PREF) < int(netns.INTERCEPT_PREF)
    assert netns.lg_rules("tap0", [], "del")[0][:2] == ["rule", "del"]
    # the resolver's own address is not refused: the local table (pref 0) delivers it first
    assert not any(r[r.index("to") + 1] == f"{netns.MOCK_DNS}/32" for r in rules if "to" in r)


def test_lan_access():
    assert netns.lan_access({"NET_LAN_ACCESS": "on"})
    assert not netns.lan_access({"NET_LAN_ACCESS": "off"})
    assert not netns.lan_access({})


def test_direct_relays_have_no_inspector():
    env = dict(VM_ADDRESS="127.0.1.2", NETSTUB_WEB_HOST="127.0.1.2", NETSTUB_WEB_PORT="18083")
    fwd = dict(env, NETNS_FWD=" 6622:22 3000:3000")
    assert netns.relay_pairs(dict(fwd, NET_MODE="direct"), "/r") == [
        "tcp:127.0.1.2:6622=unix:/r/fwd-6622.sock",
        "tcp:127.0.1.2:3000=unix:/r/fwd-3000.sock",
    ]
    assert netns.relay_pairs(dict(fwd, NET_MODE="mitm"), "/r")[0] == (
        "tcp:127.0.1.2:18083=unix:/r/mitmweb.sock"
    )
    assert netns.relay_pairs(dict(env, NET_MODE="direct"), "/r") == []


def test_enter_argv_joins_the_vms_namespace(monkeypatch):
    """vm-launcher netns-exec: nsenter into QEMU's user and network namespaces (inner()'s), where
    the guest reaches what listens on the TAP's address and the host does not."""
    links = {"/proc/42/ns/net": "net:[2]", "/proc/self/ns/net": "net:[1]"}
    monkeypatch.setattr(netns.os, "readlink", links.__getitem__)
    monkeypatch.setattr(netns.shutil, "which", lambda name: "/usr/bin/" + name)
    assert netns.enter_argv(42, ["python3", "server.py"]) == [
        "nsenter",
        "-t",
        "42",
        "-U",
        "-n",
        "--preserve-credentials",
        "--",
        "python3",
        "server.py",
    ]


def test_enter_argv_refuses_the_hosts_namespace():
    """link=off: QEMU is in this namespace, which is the host's: nothing to enter."""
    with pytest.raises(netns.Error, match="link=off"):
        netns.enter_argv(os.getpid(), ["true"])


def test_netns_exec_needs_a_running_vm(tmp_path):
    from beanjar.runtime import launch

    (tmp_path / "run").mkdir()
    with pytest.raises(launch.Error, match="not running"):
        launch.netns_exec(str(tmp_path), ["--", "true"])
    with pytest.raises(launch.Error, match="no command"):
        launch.netns_exec(str(tmp_path), ["--"])
