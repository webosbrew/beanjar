# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Translate guest mDNS advertisements using dnspython's DNS wire codec.

The small wire walk locates class fields only: mDNS uses their top bit for
cache-flush/QU, which ordinary DNS libraries otherwise interpret as a class.
"""

import ipaddress
import struct

import dns.message
import dns.name
import dns.rdata
import dns.rdatatype


def _classes(wire):
    """Return class offsets, rejecting truncated fields before decoding names."""
    if len(wire) < 12:
        raise ValueError("truncated DNS header")
    counts = struct.unpack_from("!4H", wire, 4)
    pos = 12
    offsets = []

    def name_end(pos):
        while True:
            if pos >= len(wire):
                raise ValueError("truncated DNS name")
            size = wire[pos]
            pos += 1
            if size & 0xC0 == 0xC0:
                if pos >= len(wire):
                    raise ValueError("truncated DNS pointer")
                return pos + 1
            if size & 0xC0:
                raise ValueError("invalid DNS label")
            if not size:
                return pos
            pos += size
            if pos > len(wire):
                raise ValueError("truncated DNS label")

    for section, count in enumerate(counts):
        for _ in range(count):
            pos = name_end(pos)
            size = 4 if section == 0 else 10
            if pos + size > len(wire):
                raise ValueError("truncated DNS record")
            offsets.append(pos + 2)
            if section == 0:
                pos += size
            else:
                length = struct.unpack_from("!H", wire, pos + 8)[0]
                pos += size + length
                if pos > len(wire):
                    raise ValueError("truncated DNS rdata")
    if pos != len(wire):
        raise ValueError("trailing DNS bytes")
    return offsets


def _decode(wire):
    offsets = _classes(wire)
    normalized = bytearray(wire)
    flags = []
    for offset in offsets:
        if struct.unpack_from("!H", wire, offset - 2)[0] == dns.rdatatype.OPT:
            continue
        value = struct.unpack_from("!H", wire, offset)[0]
        flags.append(value & 0x8000)
        struct.pack_into("!H", normalized, offset, value & 0x7FFF)
    return dns.message.from_wire(bytes(normalized), one_rr_per_rrset=True), flags


def guest_hosts(wire: bytes, guest_ip: str) -> dict[str, int]:
    """Return canonical guest A owners and their TTLs, including goodbyes."""
    guest_ip = str(ipaddress.IPv4Address(guest_ip))
    message, _ = _decode(wire)
    hosts = {}
    for rr in message.answer + message.authority + message.additional:
        if rr.rdtype == dns.rdatatype.A and any(
            getattr(data, "address", None) == guest_ip for data in rr
        ):
            name = rr.name.canonicalize().to_text()
            hosts[name] = min(hosts.get(name, rr.ttl), rr.ttl)
    return hosts


def _guest_names(records, guest_ip, known_hosts):
    guest_names = {
        rr.name
        for rr in records
        if rr.rdtype == dns.rdatatype.A
        and any(getattr(data, "address", None) == guest_ip for data in rr)
    }
    guest_names.update(dns.name.from_text(name).canonicalize() for name in (known_hosts or ()))
    return guest_names


def _endpoint_ttls(records, guest_names):
    endpoints = {}
    for rr in records:
        if rr.rdtype != dns.rdatatype.SRV or not rr.ttl:
            continue
        labels = [label.lower() for label in rr.name.labels]
        transport = None
        if len(labels) >= 3 and labels[-2:] == [b"local", b""]:
            if labels[-3] in (b"_tcp", b"_udp"):
                transport = labels[-3][1:].decode("ascii")
        if transport:
            for data in rr:
                if data.target in guest_names and 0 < data.port <= 65535:
                    endpoint = (transport, data.port)
                    endpoints[endpoint] = max(endpoints.get(endpoint, 0), rr.ttl)
    return endpoints


def endpoint_ttls(
    wire: bytes, guest_ip: str, known_hosts: set[str] | None = None
) -> dict[tuple[str, int], int]:
    """Return admitted endpoints and the longest advertising SRV TTL for each."""
    guest_ip = str(ipaddress.IPv4Address(guest_ip))
    message, _ = _decode(wire)
    records = message.answer + message.authority + message.additional
    return _endpoint_ttls(records, _guest_names(records, guest_ip, known_hosts))


def rewrite(
    wire: bytes, guest_ip: str, lan_ip: str, known_hosts: set[str] | None = None
) -> tuple[bytes, set[tuple[str, int]]]:
    """Rewrite guest IPv4 records and return proven advertised service ports.

    Malformed packets raise ValueError or dnspython's DNSException. Only targets
    with a guest A record in this packet or known_hosts establish ownership.
    Callers must expire known_hosts according to the A records' TTLs.
    """
    guest_ip = str(ipaddress.IPv4Address(guest_ip))
    lan_ip = str(ipaddress.IPv4Address(lan_ip))
    message, flags = _decode(wire)
    sections = [message.question, message.answer, message.authority, message.additional]
    flag_by_id = {}
    for record, flag in zip((rr for section in sections for rr in section), flags, strict=True):
        flag_by_id[id(record)] = flag
    records = [rr for section in sections[1:] for rr in section]
    guest_names = _guest_names(records, guest_ip, known_hosts)
    endpoints = set(_endpoint_ttls(records, guest_names))
    for section in sections[1:]:
        kept = []
        for rr in section:
            if rr.rdtype == dns.rdatatype.AAAA and rr.name in guest_names:
                continue
            if rr.rdtype == dns.rdatatype.A:
                changed = [
                    dns.rdata.from_text(rr.rdclass, rr.rdtype, lan_ip)
                    if getattr(data, "address", None) == guest_ip
                    else data
                    for data in rr
                ]
                ttl = rr.ttl
                rr.clear()
                for data in changed:
                    rr.add(data, ttl)
            kept.append(rr)
        section[:] = kept
    # Keep classes normalized during encoding, so compressed domain-name RDATA
    # uses the typed IN codec. Restore mDNS bits directly in the resulting wire.
    result = bytearray(message.to_wire(max_size=65535))
    retained_flags = [flag_by_id[id(rr)] for section in sections for rr in section]
    class_offsets = [
        offset
        for offset in _classes(result)
        if struct.unpack_from("!H", result, offset - 2)[0] != dns.rdatatype.OPT
    ]
    for offset, flag in zip(class_offsets, retained_flags, strict=True):
        value = struct.unpack_from("!H", result, offset)[0]
        struct.pack_into("!H", result, offset, value | flag)
    return bytes(result), endpoints
