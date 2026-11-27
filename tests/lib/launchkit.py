# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What the launch tests (tests/unit/launch, tests/integration/launch) share: the fake checkout
(fake_tree), the fake partition map, the sestore snapshot, boot.conf reader and the constants the
tests of the launcher's areas have in common."""

import base64
import os
import shlex
import shutil
import struct
import subprocess
from pathlib import Path

from beanjar.firmware import cache as firmware_cache
from beanjar import paths
from beanjar.board import partmap

TOP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VM = os.path.join(TOP, "host", "scripts", "vm-launcher")
# fake-epk2extract's rootfs.pak, an afro rootfs
TRUST_PAK = os.path.join(TOP, "tests", "fakes", "trust-rootfs.pak")
# a model key of the right shape (host/core/beanjar/vm/modelkey.py)
KEY = base64.b64encode(bytes(range(32)))
FW_NAME = paths.FW_NAME  # a firmware name the launcher resolves to (h13, afro)
H13_AFRO = ("h13", "afro")


def fake_tree(root):
    """A checkout with what the launcher looks for (empty files), a firmware of four paks with a
    current ROOTFS=uncompressed cache of rootfs and fonts, and a state directory: (top, fw, state)."""
    top, fw, state = (
        os.path.join(root, "top"),
        os.path.join(root, FW_NAME),
        os.path.join(root, "state"),
    )
    for d in (
        "build/layers/h13-afro/webos",  # the always-used layers, each a read-only disk
        "build/layers/h13-afro/soc",
        "build/layers/h13-afro/common",
        "build/layers/h13-afro/rootssh",
        "build/layers/h13-afro/devmode",
        "build/layers/h13-afro/hbchannel",
        "build/network",
        "build/qemu/bin",
        "build/qemu/libexec",
        "guest/layers/soc/h13/rootfs/etc/emulator",
    ):
        os.makedirs(os.path.join(top, d))
    for f in ("build/Image", "build/initramfs.cpio.gz"):
        open(os.path.join(top, f), "w").close()
    os.makedirs(os.path.join(top, "identity"))  # a VM's identity directory with a model key
    from beanjar.vm import sestore

    sestore.set_model(os.path.join(top, "identity"), KEY)
    for f in (
        "build/network/beanjar-relay",
        "build/qemu/bin/qemu-system-aarch64",
        "build/qemu/libexec/vhost-user-gpu",
    ):
        with open(os.path.join(top, f), "w") as out:
            out.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(top, f), 0o755)
    with open(
        os.path.join(top, "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline"), "w"
    ) as f:
        f.write(
            "model=X ethaddr=00:11:22:33:44:55\nboard ethaddr=a8:23:fe:12:34:56 serial=1\n"  # the first
            "hwopt=202212001001002000\n"
        )  # DDR '1': WT1H's 1.25 GB, -m 1280
    os.makedirs(os.path.join(fw, "cache"))
    shutil.copyfile(TRUST_PAK, os.path.join(fw, "rootfs.pak"))  # its release: afro
    for p in ("tvservice-dvb", "otncabi-dvb", "fonts"):
        with open(os.path.join(fw, p + ".pak"), "w") as f:
            f.write("hsqs " + p)
    for p in ("rootfs", "fonts"):
        img = firmware_cache.image_path(fw, os.path.join(fw, "cache"), p)  # records <pak>.src
        open(img, "w").close()
    return top, fw, state


def fake_map(names=None, sizes=None):
    """A partmap.Map with a partition of each name in names (default: afro's partitions, in its
    table's order), 1 MiB each unless sizes says, partinfo 0x40000, then reserved."""
    names = names or (
        "secureboot",
        "partinfo",
        "mapbak",
        "authcxt",
        "nvram",
        "sestore",
        "sedata",
        "sedata",
        "rootfs",
        "tvservice",
        "otncabi",
        "otycabi",
        "fonts",
        "smartkey",
        "db8",
        "data",
        "apps",
    )
    sizes = dict({"partinfo": 0x40000, "mapbak": 0x40000}, **(sizes or {}))
    entries, off = [], 0
    for n in names:
        entries.append(partmap.Entry(n, off, sizes.get(n, 1 << 20), "", 0, 0, 1, 1, 0))
        off += entries[-1].size
    entries.append(partmap.Entry("reserved", off, 0x1B800000, "", 0, 0, 0, 0, 1))
    return partmap.Map(partmap.MAGIC, 0, 0, ("h13_emmc", off, 0, 0, 0, 0, 1), entries)


def fake_partmap_build(path, m=None):
    """A stand-in for partmap.build: writes fake_map()'s partinfo disk image to path."""

    def build(fw, target, cache, tree=None, updatable=False):
        with open(path, "wb") as f:
            f.write(partmap.image(partmap.encode(m or fake_map())))
        return path

    return build


def store_snapshot(layer):
    data = Path(layer, "etc/emulator/sestore.bin").read_bytes()
    policy, count = struct.unpack_from("<II", data)
    assert policy in (0, 1)
    offset = 8
    items = {}
    for _ in range(count):
        name, length = struct.unpack_from("<65s3xI", data, offset)
        offset += 72
        name = name.split(b"\0", 1)[0].decode("ascii")
        items[name] = data[offset : offset + length]
        offset += length
    assert offset == len(data)
    return ("fallback" if policy else "error"), items


def net(old):
    """The launcher's NET_LINK and NET_MODE for one of the three combinations before them (mitm,
    direct, off), whose plans launch-argv.json pins unchanged."""
    return {"NET_LINK": "off"} if old == "off" else {"NET_LINK": "nat", "NET_MODE": old}


BOOT_VARS = (
    "TARGET",
    "INIT_SYSTEM",
    "ROOTFS",
    "PAKS",
    "PAK_LOWERS",
    "LAYERS",
    "STATE_DISKS",
    "NVM_DISKS",
    "NVM_EEPROM",
    "PMLOG_MODE",
    "PMLOG_JOB",
    "PARTINFO_DISK",
    "PART_NODES",
    "TRUST_BUNDLES",
    "TRUST_HASH_DIR",
    "BOARD_CMDLINE",
    "SOC_MODULES",
    "MODULE_PARAMS_mstar_utopia",
    "MODULE_PARAMS_lg115x",
)


def boot_conf(path, extra=()):
    """boot.conf's variables (BOOT_VARS and extra) as guest/initramfs/rootfs/init sees them: the file sourced by
    sh (unset: empty)."""
    names = BOOT_VARS + tuple(extra)
    script = f". {shlex.quote(path)}; " + "; ".join(f'printf "%s\\n" "${{{v}-}}"' for v in names)
    r = subprocess.run(["sh", "-euc", script], stdout=subprocess.PIPE, text=True, check=True)
    return dict(zip(names, r.stdout.split("\n")))


def text(path):
    with open(path) as f:
        return f.read()
