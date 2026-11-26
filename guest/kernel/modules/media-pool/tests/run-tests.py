#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The media pool's guest-side tests in a running VM, pass or fail with counts.

  run-tests.py --selftest BIN [--kunit-dir DIR] [--vm [HOST/]NAME] [--loops 100] [--skip-pause]

VM defaults to $VM. Needs the VM's rootssh layer (beanjar ares-shell --root). Runs, in order:
  1. media-pool-selftest (BIN, the 32-bit webOS build) in the guest: its TAP counts;
  2. media-pool.ko unloaded and loaded again LOOPS times with no file open, and LOOPS times with a
     file of the device open (rmmod must fail while it is, then the load goes on);
  3. the doorbell across a pause: QMP stop, 2 s, cont (local VMs: the run directory's QMP socket;
     a remote VM over beanjar's host), then the selftest again (its poll wake-up check);
  4. with KUNIT=1 (or --kunit) the KUnit module (DIR/media-pool-kunit.ko, a debug kernel build's):
     loaded, its TAP read from the kernel log and parsed with the kernel tree's kunit.py;
  5. the kernel log since the start: no WARNING, BUG, KASAN, lockdep or kmemleak report (debugfs
     mounted for kmemleak if it is not).
Prints each step's numbers and PASS or FAIL; exit status 0 only for PASS."""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

BEANJAR = os.environ.get("BEANJAR", "beanjar")
TREE = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../.."))
SPLATS = re.compile(
    r"WARNING:|BUG:|KASAN|kernel BUG|Oops|possible circular locking|inconsistent lock state|"
    r"sleeping function called from invalid context|unreferenced object|DMA-API:|"
    r"suspicious RCU usage|INFO: task .* blocked"
)


def env():
    e = dict(os.environ)
    e["PATH"] = os.path.expanduser("~/.cargo/bin") + os.pathsep + e.get("PATH", "")
    return e


def shell(vm, command, timeout=600, data=None):
    r = subprocess.run(
        [BEANJAR, "ares-shell", "--root", "-d", vm, "-r", command],
        capture_output=True, timeout=timeout, env=env(),
        input=data if data is not None else b"",
    )  # fmt: skip
    return r.returncode, (r.stdout + r.stderr).decode(errors="replace")


def push(vm, path, dest):
    """A file into the guest, over the shell's stdin (a command line holds only a few KiB)."""
    code, out = shell(vm, f"cat > {dest} && chmod 755 {dest}", data=open(path, "rb").read())
    if code:
        raise SystemExit(f"cannot copy {path} to the guest: {out}")


def tap(out):
    passed = len(re.findall(r"^ok \d+", out, re.M))
    failed = re.findall(r"^not ok \d+.*", out, re.M)
    return passed, failed


def qmp(vm, commands):
    """QMP commands to a local VM's run directory socket; a remote one through ssh to its host."""
    host, name = vm.split("/", 1) if "/" in vm else (None, vm)
    if host:
        script = (
            "import json,socket,sys,os\n"
            f"s=socket.socket(socket.AF_UNIX);s.connect(os.path.expanduser('~/.local/share/beanjar/vms/{name}/run/qmp.sock'))\n"
            "f=s.makefile('rw');f.readline()\n"
            "for c in ['qmp_capabilities']+sys.argv[1:]:\n"
            " f.write(json.dumps({'execute':c})+'\\n');f.flush();print(f.readline().strip())\n"
            " import time;time.sleep(2 if c=='stop' else 0)\n"
        )
        r = subprocess.run(
            ["ssh", host, "python3", "-", *commands],
            input=script,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.stdout
    s = socket.socket(socket.AF_UNIX)
    s.connect(os.path.expanduser(f"~/.local/share/beanjar/vms/{name}/run/qmp.sock"))
    f = s.makefile("rw")
    f.readline()
    out = []
    for c in ["qmp_capabilities"] + commands:
        f.write(json.dumps({"execute": c}) + "\n")
        f.flush()
        out.append(f.readline().strip())
        if c == "stop":
            time.sleep(2)
    s.close()
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", required=True)
    ap.add_argument("--kunit-dir")
    ap.add_argument(
        "--module",
        help="media-pool.ko of the running kernel's build (default: in --kunit-dir's parent)",
    )
    ap.add_argument("--vm", default=os.environ.get("VM"))
    ap.add_argument("--loops", type=int, default=100)
    ap.add_argument("--kunit", action="store_true", default=os.environ.get("KUNIT") == "1")
    ap.add_argument("--skip-pause", action="store_true")
    a = ap.parse_args()
    if not a.vm:
        raise SystemExit("no VM: --vm or VM=")
    results, numbers = [], {}

    def check(ok, what):
        results.append(ok)
        print(f"{'ok  ' if ok else 'FAIL'} {what}", flush=True)

    _, start = shell(a.vm, "cat /proc/uptime")
    t0 = float(start.split()[0])
    push(a.vm, a.selftest, "/tmp/media-pool-selftest")
    module = a.module or os.path.join(os.path.dirname(a.kunit_dir or "."), "media-pool.ko")
    push(a.vm, module, "/tmp/media-pool.ko")

    # 1. the ABI
    code, out = shell(a.vm, "/tmp/media-pool-selftest 200")
    passed, failed = tap(out)
    numbers["selftest_passed"], numbers["selftest_failed"] = passed, len(failed)
    for line in failed:
        print("  " + line)
    check(
        code == 0 and passed > 0 and not failed, f"selftest: {passed} passed, {len(failed)} failed"
    )

    # 2. load and unload
    loop = (
        "n=0; bad=0; while [ $n -lt %d ]; do rmmod media_pool || bad=$((bad+1)); "
        "insmod /tmp/media-pool.ko || bad=$((bad+1)); [ -c /dev/lg/webos.media-pool ] || bad=$((bad+1)); "
        "n=$((n+1)); done; echo loops=$n bad=$bad"
    )
    code, out = shell(a.vm, loop % a.loops)
    m = re.search(r"loops=(\d+) bad=(\d+)", out)
    numbers["reload_loops"], numbers["reload_failures"] = (int(m[1]), int(m[2])) if m else (0, -1)
    check(bool(m) and m[2] == "0", f"rmmod/insmod with no file open: {out.strip()[-60:]}")
    held = (
        "n=0; refused=0; bad=0; while [ $n -lt %d ]; do [ -c /dev/lg/webos.media-pool ] || bad=$((bad+1)); "
        "exec 3</dev/lg/webos.media-pool; "
        "rmmod media_pool 2>/dev/null && bad=$((bad+1)) || refused=$((refused+1)); exec 3>&-; "
        "rmmod media_pool || bad=$((bad+1)); insmod /tmp/media-pool.ko || bad=$((bad+1)); "
        "n=$((n+1)); done; echo loops=$n refused=$refused bad=$bad"
    )
    code, out = shell(a.vm, held % a.loops)
    m = re.search(r"loops=(\d+) refused=(\d+) bad=(\d+)", out)
    numbers["held_loops"] = int(m[1]) if m else 0
    numbers["held_refused"] = int(m[2]) if m else 0
    check(
        bool(m) and m[2] == m[1] and m[3] == "0",
        f"rmmod refused while a file is open, then reloaded: {out.strip()[-60:]}",
    )

    # 3. the doorbell across a pause
    if not a.skip_pause:
        qmp(a.vm, ["stop"])
        qmp(a.vm, ["cont"])
        code, out = shell(a.vm, "/tmp/media-pool-selftest 20")
        passed, failed = tap(out)
        woke = re.search(r"^ok \d+ - poll wakes for the host's ring", out, re.M) is not None
        numbers["after_pause_passed"], numbers["after_pause_failed"] = passed, len(failed)
        check(
            code == 0 and woke and not failed,
            f"after stop/cont: doorbell wake-up {'seen' if woke else 'missing'}, "
            f"{passed} passed, {len(failed)} failed",
        )

    # 4. KUnit
    if a.kunit:
        ko = os.path.join(a.kunit_dir or "", "media-pool-kunit.ko")
        if not os.path.exists(ko):
            check(False, f"KUnit: no {ko} (build against a kernel with CONFIG_KUNIT)")
        else:
            push(a.vm, ko, "/tmp/media-pool-kunit.ko")
            code, out = shell(
                a.vm,
                "rmmod media_pool_kunit 2>/dev/null; "
                "insmod /tmp/media-pool-kunit.ko; dmesg; rmmod media_pool_kunit",
            )
            lines = [re.sub(r"^\[[ \d.]+\] ", "", l) for l in out.splitlines()]
            # each KTAP block on its own: the built-in suites at boot (webos-ivshmem), the module's
            starts = [
                i for i, l in enumerate(lines) if l.startswith(("KTAP version", "TAP version"))
            ]
            ran = failed = 0
            ok_all = bool(starts)
            for i, s0 in enumerate(starts):
                block = "\n".join(lines[s0 : starts[i + 1] if i + 1 < len(starts) else None])
                parse = subprocess.run(
                    [sys.executable, os.path.join(TREE, "subprojects/linux-6.18.53/tools/testing/kunit/kunit.py"),
                     "parse"],
                    input=block, capture_output=True, text=True,
                )  # fmt: skip
                text = re.sub(r"\x1b\[[0-9;]*m", "", parse.stdout)
                summary = re.findall(r"Testing complete\. (.*)", text)
                print("  " + (summary[-1] if summary else text[-300:]))
                m = re.search(r"Ran (\d+) tests: passed: (\d+)(?:, failed: (\d+))?", text)
                ran += int(m[1]) if m else 0
                failed += int(m[3] or 0) if m else 1
                ok_all &= bool(m) and not (m[3] and int(m[3]))
            numbers["kunit_ran"], numbers["kunit_failed"] = ran, failed
            check(ok_all, f"KUnit: {ran} tests, {failed} failed")

    # 5. the kernel log
    shell(
        a.vm,
        "grep -q ' /sys/kernel/debug ' /proc/mounts || mount -t debugfs none /sys/kernel/debug;"
        "[ -w /sys/kernel/debug/kmemleak ] && echo scan > /sys/kernel/debug/kmemleak; sleep 6;"
        "[ -r /sys/kernel/debug/kmemleak ] && echo scan > /sys/kernel/debug/kmemleak",
    )
    code, out = shell(a.vm, "dmesg; cat /sys/kernel/debug/kmemleak 2>/dev/null")
    splats = []
    for line in out.splitlines():
        m = re.match(r"\[\s*([\d.]+)\]", line)
        if (not m or float(m[1]) >= t0) and SPLATS.search(line):
            splats.append(line)
    numbers["kernel_log_reports"] = len(splats)
    for line in splats[:20]:
        print("  " + line)
    check(not splats, f"kernel log since the start: {len(splats)} reports")
    print("numbers: " + json.dumps(numbers))
    print("PASS" if all(results) else "FAIL")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
