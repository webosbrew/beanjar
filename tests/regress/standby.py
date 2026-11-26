#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Quick Start+'s standby of a running checkout VM, from its host (scripts/regress's standby step;
docs/dev/architecture/suspend.md):

    standby.py wait TIMEOUT    wait until the guest is in standby: "STANDBY <seconds>"
    standby.py cpu SECONDS     QEMU's CPU use over SECONDS: "CPU <percent of one host CPU>"
    standby.py wake-early TIMEOUT  test hook: the power key the moment the standby shows (the
                               suspend's entry, before the s2idle: the micom latches it and wakes the
                               SoC at the suspend's noirq entry, i2c.c), then as wake
    standby.py wake TIMEOUT    the remote's POWER on ir.sock (the host's micom part, ir_relay.py,
                               pulses the wake line and continues the VM it stopped), then wait
                               until the standby ends: "WOKEN <seconds>"

The guest is in standby while its webos.standby port is open: QMP query-chardev's frontend-open of
chardev "standby" (include/webos-ir.h), also while the host has the VM stopped with the SoC down
(chardev "suspended"). QMP is RUN_DIR/qmp.sock, the remote RUN_DIR/ir.sock (RUN_DIR default build), QEMU's
pid RUN_DIR/qemu.pid. Exits 1 on a timeout.
"""

import json
import os
import socket
import sys
import time

TOP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
RUN = os.path.join(TOP, os.environ.get("RUN_DIR") or "build")


def qmp(*cmds):
    """The returns of cmds ({"execute": ...}) on one QMP connection."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(os.path.join(RUN, "qmp.sock"))
        f = s.makefile("rw")
        json.loads(f.readline())
        out = []
        for cmd in ({"execute": "qmp_capabilities"},) + cmds:
            f.write(json.dumps(cmd) + "\n")
            f.flush()
            while True:
                reply = json.loads(f.readline())
                if "return" in reply or "error" in reply:
                    break
            out.append(reply.get("return"))
        return out[1:]


def standby():
    try:
        (chardevs,) = qmp({"execute": "query-chardev"})
    except (OSError, ValueError):
        return None
    return any(c.get("label") == "standby" and c.get("frontend-open") for c in chardevs or [])


def wait_for(want, timeout):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if standby() == want:
            return time.monotonic() - start
        time.sleep(0.1)
    return None


def power_key():
    """The remote's POWER, down and up, on ir.sock (include/webos-ir.h records)."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(os.path.join(RUN, "ir.sock"))
        s.sendall(bytes((0x49, 1, 0x04, 0x08)))
        time.sleep(0.1)
        s.sendall(bytes((0x49, 0, 0x04, 0x08)))
        time.sleep(0.2)  # the relay reads the records before the hang-up


def cpu(seconds):
    with open(os.path.join(RUN, "qemu.pid")) as f:
        pid = int(f.read().split()[0])

    def ticks():
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])  # utime, stime

    t0, c0 = time.monotonic(), ticks()
    time.sleep(seconds)
    return 100.0 * (ticks() - c0) / os.sysconf("SC_CLK_TCK") / (time.monotonic() - t0)


def main(argv):
    if len(argv) != 2 or argv[0] not in ("wait", "cpu", "wake", "wake-early"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    what, n = argv[0], float(argv[1])
    if what == "cpu":
        print(f"CPU {cpu(n):.1f}")
        return 0
    if what in ("wake", "wake-early"):
        start = time.monotonic()
        if what == "wake-early":
            while not standby():
                if time.monotonic() - start > n:
                    print(f"no standby within {n:.0f} s")
                    return 1
                time.sleep(0.005)
        elif not standby():
            print("not in standby")
            return 1
        start = time.monotonic()
        power_key()
        t = wait_for(False, n)
        if t is None:
            print(f"still in standby {n:.0f} s after the power key")
            return 1
        print(f"WOKEN {time.monotonic() - start:.2f}")
        return 0
    t = wait_for(True, n)
    if t is None:
        print(f"no standby within {n:.0f} s")
        return 1
    print(f"STANDBY {t:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
