#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Sequential TCG measurements with private sockets, volatile state and VM cleanup."""

import argparse
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROFILES = {
    "baseline": (4, "max", None),
    "one": (1, "max", None),
    "two": (2, "max", None),
    "nosve": (4, "max,sve=off,sme=off", None),
    "one-nosve": (1, "max,sve=off,sme=off", None),
    "lean": (4, "max,sve=off,sme=off,pauth=off", None),
    "nopauth": (4, "max,pauth=off", None),
    "nopmu": (4, "max,pauth=off,pmu=off", None),
    "nopmu-nosve": (4, "max,pauth=off,pmu=off,sve=off,sme=off", None),
    "impdef": (4, "max,pauth-impdef=on", None),
    "one-lean": (1, "max,sve=off,sme=off,pauth=off", None),
    "cache512": (4, "max", "512"),
    "a53": (4, "cortex-a53", None),
    "serial": (4, "max", None),
}


def measure(out, profile, services, io=False, kernel=None):
    smp, cpu, cache = PROFILES[profile]
    sock = out / "console.sock"
    sock.unlink(missing_ok=True)
    env = dict(os.environ)
    for key in ("QEMU_EXTRA", "TCG_TB_SIZE", "PMLOG_DEBUG", "NET_DUMP"):
        env.pop(key, None)
    env.update(
        LAYERS="normal,headless",
        STATE="none",
        NET_LINK="off",
        MEM="2048",
        SMP=str(smp),
        TCG_CPU=cpu,
        TCG_THREADS="multi",
        PMLOG="",
        CONSOLE_SOCK=str(sock),
        CONSOLE_LOG=str(out / "serial.log"),
        WEBOS_LOG=str(out / "webos.log"),
        CURSOR_SOCK=str(out / "cursor.sock"),
        RUN_DIR=str(out),
    )  # the guest's shares: out/share (beanjar/guestside/share.py)
    if services:
        target = subprocess.run(
            [ROOT / "host/scripts/vm-launcher", "config", "TARGET"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        if not (ROOT / "build/layers" / target / "tcg-perf/usr/sbin/tcg-luna-perf").exists():
            raise RuntimeError("run sh tests/regress/tcg/build.sh first")
        env["LAYERS"] += ",tcg-perf"
    if io:
        fixture = ROOT / "build/tcg-perf/io.ext4"
        if not fixture.exists():
            raise RuntimeError("run sh tests/regress/tcg/build.sh --io first")
        env["QEMU_EXTRA"] = (
            f"-drive if=none,id=ioperf,format=raw,readonly=on,file={fixture} "
            "-device virtio-blk-pci,drive=ioperf,serial=ioperf"
        )
    if kernel:  # a later -kernel overrides the launcher's
        env["QEMU_EXTRA"] = (env.get("QEMU_EXTRA", "") + f" -kernel {kernel}").strip()
    if profile == "serial":
        env["TCG_THREADS"] = "single"
    if cache:
        env["TCG_TB_SIZE"] = cache
    start = time.monotonic()
    data = bytearray()
    with (out / "qemu.log").open("w") as log:
        command = [str(ROOT / "host/scripts/vm-launcher"), "run"]
        if not services:
            command.append("webos.init=0")
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = start + 120
            while not sock.exists():
                if proc.poll() is not None:
                    raise RuntimeError((out / "qemu.log").read_text())
                if time.monotonic() > deadline:
                    raise TimeoutError("console socket")
                time.sleep(0.05)
            with socket.socket(socket.AF_UNIX) as serial:
                serial.connect(str(sock))
                serial.settimeout(1)

                def wait(marker, seconds):
                    deadline = time.monotonic() + seconds
                    while marker not in data:
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"waiting for {marker!r}; see {out}")
                        try:
                            chunk = serial.recv(65536)
                            if not chunk:
                                raise RuntimeError("QEMU closed console")
                            data.extend(chunk)
                        except socket.timeout:
                            pass

                wait(b"debug shell", 120)
                shell_time = time.monotonic() - start
                data.clear()
                # what the guest runs, into its read-only share (/host/<path>)
                subprocess.run(
                    [
                        str(ROOT / "host/scripts/vm-launcher"),
                        "put",
                        "tests/regress/tcg/services.sh",
                        "tests/regress/tcg/io.sh",
                        "build/tcg-perf/perf-arm",
                        "build/tcg-perf/io-perf",
                    ],
                    cwd=ROOT,
                    env=env,
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                if services:
                    command = "webos-enter sh /mnt/host/tests/regress/tcg/services.sh 30"
                elif io:
                    command = "sh /host/tests/regress/tcg/io.sh"
                else:
                    command = "for i in 1 2 3; do /host/build/tcg-perf/perf-arm || break; done"
                serial.sendall((command + "; echo PERF_DONE\n").encode())
                wait(b"\r\nPERF_DONE", 300)
            output = data.decode(errors="replace")
            if services:
                if (
                    "M1 PASS" not in output
                    or "M1 FAIL" in output
                    or output.count("service30 start=") != 3
                    or output.count("persistent1000 seconds=") != 3
                ):
                    raise RuntimeError(f"service checks failed; see {out}")
            elif io:
                if output.count("bytes=25198624 seconds=") != 12:
                    raise RuntimeError(f"I/O checks failed; see {out}")
            else:
                if (
                    output.count("checksum=3416809127") != 3
                    or output.count("checksum=622976") != 3
                    or len(re.findall(r"syscall [0-9.]", output)) != 3
                ):
                    raise RuntimeError(f"benchmark failed; see {out}")
            result = dict(
                profile=profile,
                smp=smp,
                cpu=cpu,
                cache_mib=cache,
                shell_seconds=shell_time,
                elapsed_seconds=time.monotonic() - start,
                output=output,
            )
            (out / "result.json").write_text(json.dumps(result, indent=2))
            summary = {key: value for key, value in result.items() if key != "output"}
            summary["measurements"] = re.findall(
                r"(?:integer|memory|syscall|service30|persistent1000|read|io) [^\r\n]+", output
            )
            print(json.dumps(summary), flush=True)
        finally:
            (out / "output.log").write_bytes(data)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--services", action="store_true")
    mode.add_argument("--io", action="store_true")
    parser.add_argument("--kernel", help="kernel Image to boot instead of build/Image")
    parser.add_argument("profiles", nargs="+", choices=PROFILES)
    args = parser.parse_args()
    out = ROOT / "build/tcg-perf" / time.strftime("%Y%m%dT%H%M%S")
    out.mkdir(parents=True, exist_ok=False)
    for n, profile in enumerate(args.profiles):
        # Repeat a profile by naming it again; interleave (a b a b) to spread drift over both.
        case = out / (profile if args.profiles.count(profile) == 1 else f"{n:02d}-{profile}")
        case.mkdir()
        measure(case, profile, args.services, args.io, args.kernel)


if __name__ == "__main__":
    main()
