# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""netns.py's link=lan with mode=direct: the tc commands that join the guest's TAP and the macvlan,
and the lease watch's reading of the LAN server's DHCPACK (guest_lease). The whole path runs end to
end in tests/integration/network/netns_lan_cases.py (LanDirect)."""

import socket
import struct

from beanjar import netns

MAC = "a8:23:fe:12:34:56"


def ack(
    mac=MAC,
    msgtype=5,
    yiaddr="192.168.1.77",
    ports=(67, 68),
    op=2,
    options=None,
):
    """An Ethernet frame with a DHCP reply from 192.168.1.1 to mac."""
    bootp = bytearray(240)
    bootp[0:4] = bytes((op, 1, 6, 0))
    bootp[16:20] = socket.inet_aton(yiaddr)
    bootp[28:34] = bytes.fromhex(mac.replace(":", ""))
    bootp[236:240] = bytes((99, 130, 83, 99))
    if options is None:
        options = (
            bytes((1, 4)) + socket.inet_aton("255.255.255.0")
            + bytes((3, 4)) + socket.inet_aton("192.168.1.1")
            + bytes((6, 8)) + socket.inet_aton("192.168.1.1") + socket.inet_aton("1.1.1.1")
            + bytes((51, 4)) + (86400).to_bytes(4, "big")
        )  # fmt: skip
    bootp += bytes((53, 1, msgtype)) + options + bytes((255,))
    udp = struct.pack("!HHHH", *ports, 8 + len(bootp), 0) + bootp
    ip = (
        bytes((0x45, 0)) + struct.pack("!H", 20 + len(udp)) + bytes(4) + bytes((64, 17, 0, 0))
        + socket.inet_aton("192.168.1.1") + socket.inet_aton("255.255.255.255")
    )  # fmt: skip
    eth = bytes.fromhex(mac.replace(":", "")) + bytes.fromhex("020000000001") + b"\x08\x00"
    return eth + ip + udp


def test_guest_lease():
    assert netns.guest_lease(ack(), MAC) == {
        "address": "192.168.1.77",
        "prefix": 24,
        "router": "192.168.1.1",
        "dns": ["192.168.1.1", "1.1.1.1"],
        "lease": 86400,
    }
    # options the server leaves out
    assert netns.guest_lease(ack(options=b""), MAC) == {
        "address": "192.168.1.77",
        "prefix": 32,
        "router": None,
        "dns": [],
        "lease": 0,
    }


def test_guest_lease_other_frames():
    """Only an ACK, from the server's port to the client's, to the guest's MAC, with an address."""
    for frame in (
        ack(msgtype=2),  # an OFFER
        ack(msgtype=6),  # a NAK
        ack(mac="02:00:00:00:00:09"),  # another client's
        ack(ports=(68, 67)),  # a client's request
        ack(op=1),
        ack(yiaddr="0.0.0.0"),
        ack()[:100],  # cut short
        ack()[:12] + b"\x86\xdd" + ack()[14:],  # not IPv4
        b"",
    ):
        assert netns.guest_lease(frame, MAC) is None


def test_join_commands():
    """clsact on both, and each one's ingress redirected to the other's egress, every frame."""
    assert netns.join_commands("lan0", "tap0") == [
        ["tc", "qdisc", "add", "dev", "lan0", "clsact"],
        ["tc", "qdisc", "add", "dev", "tap0", "clsact"],
        ["tc", "filter", "add", "dev", "lan0", "ingress", "matchall",
         "action", "mirred", "egress", "redirect", "dev", "tap0"],
        ["tc", "filter", "add", "dev", "tap0", "ingress", "matchall",
         "action", "mirred", "egress", "redirect", "dev", "lan0"],
    ]  # fmt: skip


def test_lan_direct():
    assert netns.lan_direct({"NET_LINK": "lan", "NET_MODE": "direct"})
    assert not netns.lan_direct({"NET_LINK": "lan", "NET_MODE": "mitm"})
    assert not netns.lan_direct({"NET_LINK": "lan"})
    assert not netns.lan_direct({"NET_LINK": "nat", "NET_MODE": "direct"})
    assert not netns.inspected({"NET_LINK": "lan", "NET_MODE": "direct"})
