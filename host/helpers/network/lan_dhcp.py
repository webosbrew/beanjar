#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""busybox udhcpc's event script for link = "lan" (host/core/beanjar/network/netns.py, lan()).

udhcpc runs it inside the VM's rootless LAN namespace with the event as its argument and the lease
in its environment ($interface, $ip, $mask, $router, $dns, $lease). On bound and renew it puts the
address on the interface and the default route on the first router, and writes the lease to
LAN_LEASE_FILE as JSON (address, prefix, router, dns, lease seconds), replacing it atomically; on
deconfig, leasefail and nak it takes the address away and removes the file. Nothing outside the
namespace is touched. Python 3 stdlib and iproute2's ip only."""

import ipaddress
import json
import os
import subprocess
import sys


def ip(*args):
    subprocess.run(["ip", *args], check=True)


def main(argv):
    event = argv[1] if len(argv) > 1 else ""
    env = os.environ
    iface = env.get("interface", "")
    out = env.get("LAN_LEASE_FILE")
    if not iface or not out:
        print("lan_dhcp: run by udhcpc, with LAN_LEASE_FILE set", file=sys.stderr)
        return 2
    if event in ("deconfig", "leasefail", "nak"):
        subprocess.run(["ip", "addr", "flush", "dev", iface], check=False)
        try:
            os.unlink(out)
        except FileNotFoundError:
            pass
        return 0
    if event not in ("bound", "renew"):
        return 0
    prefix = ipaddress.IPv4Network(
        f"0.0.0.0/{env.get('mask') or env.get('subnet') or '24'}"
    ).prefixlen
    address = str(ipaddress.IPv4Address(env["ip"]))
    routers = env.get("router", "").split()
    dns = env.get("dns", "").split()
    if event == "bound":
        ip("addr", "flush", "dev", iface)
    ip("addr", "replace", f"{address}/{prefix}", "dev", iface)
    if routers:
        ip("route", "replace", "default", "via", routers[0], "dev", iface)
    lease = {
        "address": address,
        "prefix": prefix,
        "router": routers[0] if routers else None,
        "dns": dns,
        "lease": int(env.get("lease") or 0),
    }
    tmp = f"{out}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(lease, f)
    os.replace(tmp, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
