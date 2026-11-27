#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Minimal DHCP server for the mode=mitm network namespace.

host/core/beanjar/network/netns.py runs this inside the rootless namespace that holds QEMU's
TAP. It hands the guest one fixed lease (--lease, default 10.0.2.15) with --router (default
10.0.2.2) as its router and --dns (default 10.0.2.3) as its name server: slirp4netns's, on the
guest's own network, which the namespace reaches by proxy ARP. The server's identifier and the
replies' source is --server (default 10.0.2.254), the TAP's own address and one slirp does not
have, so that the guest's unicast renewals come here rather than being proxy-ARPed to slirp's
DHCP server. connman needs a lease to bring the wired service
to "connected" and run its online check; a bare static address made it fall back to a 169.254
link-local address. A fixed lease is a static address delivered the way connman drives the
interface, as slirp's DHCP does for mode=direct.

Python 3 stdlib only; nothing is installed. The socket binds inside the namespace, where the
unshared user is root, so port 67 is allowed.
"""

import argparse
import socket
import struct
import sys


def ipbytes(addr):
    return socket.inet_aton(addr)


# --- DHCP ---------------------------------------------------------------------------------------
DHCP_MAGIC = bytes((99, 130, 83, 99))


def dhcp_options(data):
    opts = {}
    i = 0
    while i < len(data):
        code = data[i]
        if code == 0:  # pad
            i += 1
            continue
        if code == 255:  # end
            break
        length = data[i + 1]
        opts[code] = data[i + 2 : i + 2 + length]
        i += 2 + length
    return opts


def dhcp_reply(pkt, server, lease, router, dns, lease_time=86400):
    if len(pkt) < 240 or pkt[236:240] != DHCP_MAGIC:
        return None
    xid = pkt[4:8]
    chaddr = pkt[28:44]
    opts = dhcp_options(pkt[240:])
    msgtype = opts.get(53, b"\x00")[0]
    if msgtype == 1:  # DISCOVER -> OFFER
        reply_type = 2
    elif msgtype == 3:  # REQUEST -> ACK
        reply_type = 5
    else:
        return None
    out = bytearray(240)
    out[0] = 2  # BOOTREPLY
    out[1] = 1  # ethernet
    out[2] = 6  # hlen
    out[4:8] = xid
    out[16:20] = ipbytes(lease)  # yiaddr
    out[20:24] = ipbytes(server)  # siaddr
    out[28:44] = chaddr
    out[236:240] = DHCP_MAGIC
    out += bytes((53, 1, reply_type))
    out += bytes((54, 4)) + ipbytes(server)  # server identifier
    out += bytes((51, 4)) + struct.pack(">I", lease_time)  # lease time
    out += bytes((1, 4)) + ipbytes("255.255.255.0")  # subnet mask
    out += bytes((3, 4)) + ipbytes(router)  # router
    out += bytes((6, 4)) + ipbytes(dns)  # DNS server
    out += bytes((255,))
    return bytes(out)


def dhcp_socket(iface):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # Bind to the TAP so the broadcast reply leaves through it rather than by the default route
    # (the uplink).
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode() + b"\0")
    except OSError as e:
        sys.stderr.write("netns_dhcp: SO_BINDTODEVICE %s failed: %s\n" % (iface, e))
    s.bind(("", 67))
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="tap0")
    ap.add_argument("--server", default="10.0.2.254")
    ap.add_argument("--lease", default="10.0.2.15")
    ap.add_argument("--router", default="10.0.2.2")
    ap.add_argument("--dns", default="10.0.2.3")
    ap.add_argument("--lease-time", type=int, default=86400)
    args = ap.parse_args()

    s = dhcp_socket(args.iface)
    sys.stderr.write(
        "netns_dhcp: lease %s (%ds), router %s, DNS %s, server %s on %s\n"
        % (args.lease, args.lease_time, args.router, args.dns, args.server, args.iface)
    )
    sys.stderr.flush()
    while True:
        try:
            data, _ = s.recvfrom(2048)
        except OSError:
            continue
        reply = dhcp_reply(data, args.server, args.lease, args.router, args.dns, args.lease_time)
        if reply:
            try:
                s.sendto(reply, ("255.255.255.255", 68))
            except OSError as e:
                sys.stderr.write("netns_dhcp: DHCP reply failed: %s\n" % e)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
