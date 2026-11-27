#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Summarise what the guest sent, from a pcap of its NIC (python3, stdlib only).

    NET_DUMP=build/net.pcap host/scripts/vm-launcher start ...    (QEMU filter-dump, beanjar/runtime/qemu.py)
    tests/regress/netstub/pcap-summary.py build/net.pcap [--lg]

Lists the guest's DNS queries, TCP connection attempts (SYN) and other UDP destinations, with
the names the DNS answers gave each address. filter-dump records the frames between the guest's
NIC and its network backend, before anything leaves the host, so this is everything the guest tried.

--lg: exit 1 if the guest contacted LG: looked up an LG name through DNS, sent anything to LG's
networks (156.147.0.0/16, 165.244.0.0/16; sdx can set LG's DNS servers), or opened a connection to
an address outside 10.0.2.0/24 that a DNS answer gave for an LG name.
"""

import ipaddress
import re
import struct
import sys
from collections import Counter

LG_NAME = re.compile(
    r"(^|\.)(lge\.com|lg\.com|lgtvsdp\.com|lgappstv\.com|lgsmartad\.com|"
    r"lgssp\.com|lgthinq\.com|lgsmartplatform\.com|lgecloud\.com|"
    r"lgtvcommon\.com|lgsvl\.com|lge\.co\.kr)\.?$",
    re.I,
)
LG_NETS = [ipaddress.ip_network(n) for n in ("156.147.0.0/16", "165.244.0.0/16")]
SLIRP = ipaddress.ip_network("10.0.2.0/24")


def frames(path, require_complete=False):
    with open(path, "rb") as f:
        data = f.read()
    magic = data[:4]
    endian = {
        b"\xd4\xc3\xb2\xa1": "<",
        b"\xa1\xb2\xc3\xd4": ">",
        b"\x4d\x3c\xb2\xa1": "<",
        b"\xa1\xb2\x3c\x4d": ">",
    }.get(magic)
    if not endian:
        sys.exit("not a pcap file: %s" % path)
    if len(data) < 24:
        sys.exit("truncated pcap header: %s" % path)
    major, minor, _, _, snaplen, linktype = struct.unpack(endian + "HHIIII", data[4:24])
    if (major, minor) != (2, 4) or not snaplen or linktype != 1:
        sys.exit("unsupported pcap header (need Ethernet pcap 2.4): %s" % path)
    p = 24
    while p < len(data):
        if len(data) - p < 16:
            sys.exit("truncated pcap record header: %s" % path)
        sec, _, incl, orig = struct.unpack(endian + "IIII", data[p : p + 16])
        if incl > snaplen or incl > orig or incl > len(data) - p - 16:
            sys.exit("truncated or invalid pcap record: %s" % path)
        if require_complete and incl < orig:
            sys.exit("pcap record shortened by snaplen; cannot check all traffic: %s" % path)
        yield sec, data[p + 16 : p + 16 + incl]
        p += 16 + incl


def dns_name(msg, p, depth=0):
    labels = []
    while p < len(msg) and depth < 20:
        n = msg[p]
        if n == 0:
            return ".".join(labels), p + 1
        if n & 0xC0 == 0xC0:
            name, _ = dns_name(msg, ((n & 0x3F) << 8) | msg[p + 1], depth + 1)
            return ".".join(labels + ([name] if name else [])), p + 2
        labels.append(msg[p + 1 : p + 1 + n].decode("ascii", "replace"))
        p += 1 + n
    return ".".join(labels), p


def dns_parse(msg):
    """(is_response, [question names], [(name, ipv4)] answers)"""
    flags, qd, an = struct.unpack(">HHH", msg[2:8])
    p, qs, ans = 12, [], []
    for _ in range(qd):
        name, p = dns_name(msg, p)
        qs.append(name)
        p += 4
    for _ in range(an):
        name, p = dns_name(msg, p)
        rtype, _, _, rdlen = struct.unpack(">HHIH", msg[p : p + 10])
        p += 10
        if rtype == 1 and rdlen == 4:
            ans.append((name, str(ipaddress.IPv4Address(msg[p : p + 4]))))
        p += rdlen
    return bool(flags & 0x8000), qs, ans


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check_lg = "--lg" in sys.argv
    if len(args) != 1:
        sys.exit(__doc__)
    queries, syns, udp, names = Counter(), Counter(), Counter(), {}
    for _, fr in frames(args[0], require_complete=check_lg):
        if len(fr) < 34 or fr[12:14] != b"\x08\x00":
            continue
        ip = fr[14:]
        ihl = (ip[0] & 15) * 4
        proto = ip[9]
        src, dst = str(ipaddress.IPv4Address(ip[12:16])), str(ipaddress.IPv4Address(ip[16:20]))
        l4 = ip[ihl:]
        guest = src.startswith("10.0.2.") and src not in ("10.0.2.2", "10.0.2.3", "10.0.2.100")
        try:
            if proto == 17 and len(l4) >= 8:
                sport, dport = struct.unpack(">HH", l4[:4])
                if 53 in (sport, dport):
                    resp, qs, ans = dns_parse(l4[8:])
                    if resp:
                        for n, a in ans:  # the question name too: the answer may be a CNAME's
                            names.setdefault(a, set()).update([n] + qs)
                    elif guest:
                        for q in qs:
                            queries[(q, dst)] += 1
                elif guest and dport not in (67, 68):
                    udp["%s:%d" % (dst, dport)] += 1
            elif proto == 6 and guest and len(l4) >= 14:
                dport = struct.unpack(">H", l4[2:4])[0]
                if l4[13] & 0x12 == 0x02:  # SYN without ACK
                    syns[(dst, dport)] += 1
        except (struct.error, IndexError, ValueError):
            continue

    def label(a):
        return ",".join(sorted(names.get(a, ()))) or "-"

    bad = []
    print("DNS queries (name, server, count):")
    for (q, srv), n in sorted(queries.items()):
        lg = bool(LG_NAME.search(q)) or any(ipaddress.IPv4Address(srv) in net for net in LG_NETS)
        print("  %-50s %-15s %3d%s" % (q, srv, n, "  LG" if lg else ""))
        if lg:
            bad.append("DNS query for %s" % q)
    print("TCP connection attempts (address:port, names, count):")
    for (a, port), n in sorted(syns.items()):
        addr = ipaddress.IPv4Address(a)
        lg = addr not in SLIRP and (
            any(addr in net for net in LG_NETS) or any(LG_NAME.search(x) for x in names.get(a, ()))
        )
        print("  %-21s %-45s %3d%s" % ("%s:%d" % (a, port), label(a), n, "  LG" if lg else ""))
        if lg:
            bad.append("TCP to %s:%d (%s)" % (a, port, label(a)))
    print("other UDP (address:port, names, count):")
    for d, n in sorted(udp.items()):
        lg = any(ipaddress.IPv4Address(d.split(":")[0]) in net for net in LG_NETS)
        print("  %-21s %-45s %3d%s" % (d, label(d.split(":")[0]), n, "  LG" if lg else ""))
        if lg:
            bad.append("UDP to %s" % d)
    if check_lg:
        print("LG contact: %s" % ("; ".join(bad) if bad else "none"))
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
