#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""mDNS translation preserves compressed names and mDNS class flags."""

import struct

import dns.exception
import dns.message
import dns.rrset
import pytest
from beanjar.network.discovery_dns import _classes, endpoint_ttls, guest_hosts, rewrite

GUEST = "10.0.2.15"
LAN = "192.0.2.42"


def fixture(ttl=120):
    message = dns.message.make_query("_sample._tcp.local.", "PTR")
    message.id = 37
    message.flags = 0x8400
    message.answer.extend(
        [
            dns.rrset.from_text("_sample._tcp.local.", ttl, "IN", "PTR", "TV._sample._tcp.local."),
            dns.rrset.from_text("TV._sample._tcp.local.", ttl, "IN", "SRV", "0 0 43210 tv.local."),
            dns.rrset.from_text("TV._sample._tcp.local.", ttl, "IN", "TXT", '"identity=unchanged"'),
        ]
    )
    message.additional.extend(
        [
            dns.rrset.from_text("tv.local.", ttl, "IN", "A", GUEST),
            dns.rrset.from_text("tv.local.", ttl, "IN", "AAAA", "fe80::1234"),
            dns.rrset.from_text("other.local.", ttl, "IN", "AAAA", "2001:db8::1"),
        ]
    )
    wire = bytearray(message.to_wire())
    for offset in _classes(wire):
        struct.pack_into("!H", wire, offset, struct.unpack_from("!H", wire, offset)[0] | 0x8000)
    return bytes(wire)


WIRE = fixture()


def decode(wire):
    wire = bytearray(wire)
    flags = []
    for offset in _classes(wire):
        value = struct.unpack_from("!H", wire, offset)[0]
        flags.append(value & 0x8000)
        struct.pack_into("!H", wire, offset, value & 0x7FFF)
    return dns.message.from_wire(bytes(wire), one_rr_per_rrset=True), flags


def test_compressed_records_and_flags():
    original = fixture()
    assert b"\xc0" in original
    result, endpoints = rewrite(original, GUEST, LAN)
    message, flags = decode(result)
    before, _ = decode(original)
    assert endpoints == {("tcp", 43210)}
    assert message.id == before.id
    assert message.flags == before.flags
    assert message.question == before.question
    assert message.answer == before.answer
    assert flags == [0x8000] * 6
    assert message.additional[0][0].address == LAN
    assert message.additional[1][0].address == "2001:db8::1"
    assert all(rr.ttl == 120 for rr in message.answer + message.additional)


def test_independent_compressed_wire():
    # Header: one question, one answer. The question name starts at byte
    # 12; the answer's c00c pointer reuses it. Their class fields are at
    # offsets 24 and 30, following the name and two-byte type respectively.
    wire = bytes.fromhex(
        "1234 8400 0001 0001 0000 0000"
        "02 7476 05 6c6f63616c 00 0001 8001"
        "c00c 0001 8001 00000078 0004 0a00020f"
    )
    result, endpoints = rewrite(wire, GUEST, LAN)
    assert endpoints == set()
    assert result[:12] == wire[:12]
    assert result[12:24] == wire[12:24]
    assert result[24:26] == bytes.fromhex("8001")
    assert result[26:30] == bytes.fromhex("c00c0001")
    assert result[30:38] == bytes.fromhex("8001000000780004")
    assert result[38:] == bytes((192, 0, 2, 42))
    # Decode using independently known positions, without the production
    # class walker (which is intentionally not used in this fixture).
    normalized = bytearray(result)
    normalized[24:26] = normalized[30:32] = bytes.fromhex("0001")
    parsed = dns.message.from_wire(bytes(normalized))
    assert parsed.question[0].name.to_text() == "tv.local."
    assert parsed.answer[0].name.to_text() == "tv.local."
    assert parsed.answer[0][0].address == LAN
    assert parsed.answer[0].ttl == 120


def test_goodbye():
    wire, endpoints = rewrite(fixture(0), GUEST, LAN)
    message, _ = decode(wire)
    assert endpoints == set()
    assert all(rr.ttl == 0 for rr in message.answer + message.additional)
    assert message.additional[0][0].address == LAN


def test_split_response_does_not_establish_endpoint():
    message, _ = decode(fixture())
    message.additional.clear()
    wire, endpoints = rewrite(message.to_wire(), GUEST, LAN)
    assert endpoints == set()
    assert decode(wire)[0].answer == message.answer


def test_cached_host_for_split_records():
    assert guest_hosts(fixture(), GUEST) == {"tv.local.": 120}
    assert guest_hosts(fixture(0), GUEST) == {"tv.local.": 0}
    message, _ = decode(fixture())
    message.additional.pop(0)
    wire, endpoints = rewrite(message.to_wire(), GUEST, LAN, known_hosts={"TV.LOCAL."})
    assert endpoints == {("tcp", 43210)}
    assert len(decode(wire)[0].additional) == 1


def test_udp_and_unrelated_target():
    message, _ = decode(fixture())
    message.answer.append(
        dns.rrset.from_text("TV._sample._udp.local.", 120, "IN", "SRV", "0 0 9999 tv.local.")
    )
    message.answer.append(
        dns.rrset.from_text("Other._sample._tcp.local.", 120, "IN", "SRV", "0 0 8888 other.local.")
    )
    _, endpoints = rewrite(message.to_wire(), GUEST, LAN)
    assert endpoints == {("tcp", 43210), ("udp", 9999)}


def test_endpoint_uses_srv_ttl():
    message, _ = decode(fixture())
    message.additional[0].ttl = 5
    message.answer[1].ttl = 7200
    message.answer[2].ttl = 1
    assert endpoint_ttls(message.to_wire(), GUEST) == {("tcp", 43210): 7200}


def test_shared_endpoint_uses_longest_srv_ttl():
    message, _ = decode(fixture())
    message.answer[1].ttl = 30
    message.answer.append(
        dns.rrset.from_text("Second._sample._tcp.local.", 9000, "IN", "SRV", "0 0 43210 tv.local.")
    )
    message.answer.append(
        dns.rrset.from_text("Gone._sample._tcp.local.", 0, "IN", "SRV", "0 0 9999 tv.local.")
    )
    assert endpoint_ttls(message.to_wire(), GUEST) == {("tcp", 43210): 9000}


def test_edns_metadata():
    message, _ = decode(fixture())
    message.use_edns(payload=4096)
    result, endpoints = rewrite(message.to_wire(), GUEST, LAN)
    parsed = dns.message.from_wire(result)
    assert parsed.payload == 4096
    assert endpoints == {("tcp", 43210)}


@pytest.mark.parametrize(
    "damaged",
    [
        b"",
        WIRE[:11],
        WIRE[:-1],
        WIRE + b"extra",
        WIRE[:12] + b"\xc0\xff" + WIRE[14:],
    ],
    ids=["empty", "short_header", "truncated", "trailing_bytes", "bad_pointer"],
)
def test_malformed(damaged):
    with pytest.raises((ValueError, dns.exception.DNSException)):
        rewrite(damaged, GUEST, LAN)
