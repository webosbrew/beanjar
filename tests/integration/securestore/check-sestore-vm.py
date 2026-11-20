#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Genuine kernel and ARM32 clients in an isolated firmware-free QEMU guest."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar import sestore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", type=Path, default=TOP / "build/guest-o22-queue")
    args = parser.parse_args()
    build = args.build.resolve()
    sdk = Path("/opt/arm-webos-linux-gnueabi_sdk-buildroot/bin/arm-webos-linux-gnueabi-gcc")
    needed = [
        build / "guest/kernel/Image",
        build / "guest/kernel/gen_init_cpio",
        build / "guest/kernel/modules/lgemu/lgemu.ko",
        build / "guest/initramfs/src/sestore-load",
        build / "guest/initramfs/busybox/busybox",
        sdk,
    ]
    if not all(path.is_file() for path in needed) or not shutil.which("qemu-system-aarch64"):
        print(
            "SKIP: build kernel, module, boot helper and busybox; install the target SDK and QEMU."
        )
        return 77
    (TOP / "scratch/sestore-device").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TOP / "scratch/sestore-device") as temporary:
        root = Path(temporary)
        probe = root / "probe"
        sources = [
            TOP / "tests/integration/securestore/device-probe.c",
            TOP / "guest/layers/soc/src/libsestore/sestore.c",
            TOP / "guest/layers/soc/src/libhal_crypto/hal-crypto.c",
            TOP / "guest/layers/soc/src/libdile_crypto/dile-crypto.c",
            TOP / "tests/integration/securestore/jail-probe.c",
        ]
        subprocess.run(
            [
                str(sdk),
                "-static",
                "-std=gnu99",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I" + str(TOP / "include"),
                *map(str, sources),
                "-ldl",
                "-o",
                str(probe),
            ],
            check=True,
        )
        files = {
            "/bin/busybox": needed[4],
            "/bin/sestore-load": needed[3],
            "/probe": probe,
            "/lgemu.ko": needed[2],
        }
        for policy in ("fallback", "error"):
            source = root / (policy + ".bin")
            source.write_bytes(
                sestore.bundle(
                    policy,
                    {
                        sestore.MODEL: b"A" * 43 + b"=",
                        "3382732D": bytes([0, 255, 128, 10]),
                        sestore.YOUTUBE: b"dummy;key\0",
                        "empty": b"",
                    },
                )
            )
            files["/" + policy + ".bin"] = source
        init = root / "init"
        init.write_text("""#!/bin/busybox sh
/bin/busybox --install -s /bin
export PATH=/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
failed() { echo "SESTORE TEST FAILED"; poweroff -f; }
mkdir -p /jail/dev/lg
for policy in fallback error; do
    insmod /lgemu.ko || failed
    /probe unready || failed
    /bin/sestore-load /$policy.bin || failed
    mkdir -p /blocked/dev/lg
    echo untouched > /blocked/dev/lg/beanjar-sestore
    chmod 444 /blocked/dev/lg/beanjar-sestore
    /probe blocked || failed
    mount --bind /dev/lg /jail/dev/lg || failed
    [ "$policy" = fallback ] && code=1 || code=0
    /probe loaded $code || failed
    umount /jail/dev/lg || failed
    rm -rf /jail/dev/lg || failed
    /probe expose || failed
    mkdir -p /jail/etc/emulator/sestore
    echo old > /jail/etc/emulator/youtube.key
    echo old > /jail/etc/emulator/sestore/C1FFB834
    echo old > /jail/etc/emulator/sestore/.missing
    /probe loaded $code || failed
    rm /jail/dev/lg/beanjar-sestore || failed
    rmmod lgemu || failed
done
echo "SESTORE DEVICE TEST PASSED"
poweroff -f
""")
        files["/init"] = init
        entries = [
            "dir /bin 0755 0 0",
            "dir /dev 0755 0 0",
            "dir /proc 0755 0 0",
            "dir /sys 0755 0 0",
            "nod /dev/console 0600 0 0 c 5 1",
            "slink /bin/sh busybox 0777 0 0",
        ]
        entries += [
            f"file {name} {path} {'0600' if name.endswith('.bin') else '0755'} 0 0"
            for name, path in files.items()
        ]
        listing = root / "initramfs.list"
        listing.write_text("\n".join(entries) + "\n")
        archive = root / "initramfs.cpio"
        with archive.open("wb") as output:
            subprocess.run([str(needed[1]), str(listing)], stdout=output, check=True)
        argv = [
            "qemu-system-aarch64",
            "-machine",
            "virt",
            "-cpu",
            "cortex-a76",
            "-m",
            "256M",
            "-smp",
            "2",
            "-display",
            "none",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-no-reboot",
            "-nic",
            "none",
            "-kernel",
            str(needed[0]),
            "-initrd",
            str(archive),
            "-append",
            "console=ttyAMA0 panic=-1",
        ]
        try:
            result = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90, text=True
            )
        except subprocess.TimeoutExpired as error:
            text = (
                (error.stdout or b"").decode(errors="replace")
                if isinstance(error.stdout, bytes)
                else error.stdout or ""
            )
            (TOP / "scratch/sestore-device/vm.log").write_text(text)
            print("FAIL: device guest timed out; log: scratch/sestore-device/vm.log")
            return 1
        (TOP / "scratch/sestore-device/vm.log").write_text(result.stdout)
        lines = [
            line
            for line in result.stdout.splitlines()
            if "PASS" in line or "FAILED" in line or "Assertion" in line
        ]
        print("\n".join(lines))
        return 0 if result.returncode == 0 and "SESTORE DEVICE TEST PASSED" in result.stdout else 1


if __name__ == "__main__":
    raise SystemExit(main())
