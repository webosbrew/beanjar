# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Uncompressed copies of the firmware's SquashFS paks, for ROOTFS=uncompressed (qemu.py). Under
TCG, LZO decompression runs as emulated guest-kernel code: a cold read of the 25 MB WebKit library
took ~500 ms from the stock image and ~85 ms uncompressed. The copy is
still a SquashFS image on virtio-blk, mounted by the unchanged initramfs; only its data and metadata
blocks are stored uncompressed.

  host/scripts/vm-launcher firmware-cache [PAK...]     (default: rootfs); prints each image's path
  python3 -m beanjar.firmware.cache [PAK...]    the same

An experiment of the checkout's VM (host/scripts/vm-launcher with ROOTFS=uncompressed): the VM manager has no such
option and keeps no image (the gain measured was about 1 s of a TCG boot).

Nothing is extracted to a directory: unsquashfs -pf exports the stock image's metadata and
contents to a pseudo file, which mksquashfs -pf packs with -no-compression (same compressor id
and block size as the source). This needs squashfs-tools 4.6 or later. Explicit root options
preserve the root directory's owners, mode and timestamp. Before the image is used, its
unsquashfs -lln listing must match (except directory sizes), and a second pseudo export must
be byte-identical to the source export, including contents, hard links and extended attributes.

Cache: build/firmware-cache/<pak>-<first 16 hex of the source's sha256>.img, plus <pak>.src
("size mtime sha256" of the source) so the hash is only recomputed when the source changes.
Firmware-derived: build/ is not tracked. FW as in paths.Tree.firmware; FW_CACHE puts the cache
elsewhere. The host build runs this too (target firmware-cache), so the images are ready before
the first ROOTFS=uncompressed boot.
"""

import difflib
import filecmp
import hashlib
import os
import subprocess
import sys
import tempfile

from .. import paths


class Error(Exception):
    pass


def default_cache(tree=paths.TREE):
    return tree.build("firmware-cache")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(4 << 20):
            h.update(b)
    return h.hexdigest()


def digest(fw, cache, pak, record=True):
    """The source pak's sha256, from <pak>.src while its size and mtime are the same, else computed
    (and recorded in <pak>.src unless record is false)."""
    src = os.path.join(fw, pak + ".pak")
    if not os.path.isfile(src):
        raise Error(f"missing {src}")
    st = os.stat(src)
    stamp = f"{st.st_size} {int(st.st_mtime)}"
    try:
        with open(os.path.join(cache, pak + ".src")) as f:
            size, mtime, sum_ = f.read().split()
        if f"{size} {mtime}" == stamp:
            return sum_
    except (OSError, ValueError):
        pass
    sum_ = _sha256(src)
    if record:  # whole or not at all: another VM may read it meanwhile
        tmp = os.path.join(cache, f"{pak}.src.tmp{os.getpid()}")
        with open(tmp, "w") as f:
            f.write(f"{stamp} {sum_}\n")
        os.replace(tmp, os.path.join(cache, pak + ".src"))
    return sum_


def image_path(fw, cache, pak, record=True):
    """Where the uncompressed image of fw's pak is (or goes) in cache."""
    return os.path.join(cache, f"{pak}-{digest(fw, cache, pak, record)[:16]}.img")


def _superblock(src):
    out = subprocess.run(
        ["unsquashfs", "-s", src], stdout=subprocess.PIPE, text=True, check=True
    ).stdout
    info = {"exports": "-no-exports"}
    for line in out.splitlines():
        if line.startswith("Compression "):
            info["comp"] = line[len("Compression ") :]
        elif line.startswith("Block size "):
            info["bsize"] = line[len("Block size ") :]
        if line == "Filesystem is exportable via NFS":
            info["exports"] = "-exports"
    info["mkfs_time"] = subprocess.run(
        ["unsquashfs", "-mkfs-time", src], stdout=subprocess.PIPE, text=True, check=True
    ).stdout.strip()
    return info


def _lln(img):
    """unsquashfs -lln, directory sizes (column 3 of "d" lines) blanked."""
    out = subprocess.run(
        ["unsquashfs", "-lln", "-full-precision", "-UTC", img],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout
    lines = []
    for line in out.splitlines():
        if line.startswith("d"):
            f = line.split()
            if len(f) > 2:
                f[2] = "-"
            line = " ".join(f)
        lines.append(line)
    return lines


def _root_options(pseudo):
    """mksquashfs options from the root record of unsquashfs's pseudo export."""
    with open(pseudo, "rb") as f:
        root = f.readline().decode("ascii").split()
    if len(root) != 6 or root[:2] != ["/", "D"]:
        raise Error("unsquashfs pseudo export has no root directory record")
    _, _, mtime, mode, uid, gid = root
    return ["-root-time", mtime, "-root-mode", mode, "-root-uid", uid, "-root-gid", gid]


def _same(a, b, what):
    if a != b:
        sys.stderr.writelines(
            list(
                difflib.unified_diff(
                    [x + "\n" for x in a], [x + "\n" for x in b], "stock", "uncompressed", n=0
                )
            )[:12]
        )
        raise Error(f"{what} differs")


def build(fw, cache, pak, tree=paths.TREE, log=sys.stderr):
    """The uncompressed image of fw's pak in cache, built and verified unless it is there: its path.
    Built under its lock (<image>.lock), so VMs of one firmware cache starting at once build it once."""
    os.makedirs(cache, exist_ok=True)
    img = image_path(fw, cache, pak)
    if os.path.isfile(img):
        return img
    with paths.file_lock(img + ".lock"):
        return img if os.path.isfile(img) else _build(fw, cache, pak, img, log)


def _build(fw, cache, pak, img, log):
    src = os.path.join(fw, pak + ".pak")
    print(f"building {img} from {src}", file=log, flush=True)
    tmp = img + ".tmp"
    if os.path.exists(tmp):
        os.unlink(tmp)
    try:
        sb = _superblock(src)
        with tempfile.TemporaryDirectory(prefix=".squashfs-", dir=cache) as work:
            pseudo, verified = os.path.join(work, "source.pf"), os.path.join(work, "copy.pf")
            empty = os.path.join(work, "empty")
            os.mkdir(empty)
            subprocess.run(["unsquashfs", "-pf", pseudo, src], stdout=log, check=True)
            subprocess.run(
                [
                    "mksquashfs",
                    empty,
                    tmp,
                    "-pf",
                    pseudo,
                    "-noappend",
                    "-quiet",
                    "-no-progress",
                    "-comp",
                    sb["comp"],
                    "-b",
                    sb["bsize"],
                    "-no-compression",
                    sb["exports"],
                    "-mkfs-time",
                    sb["mkfs_time"],
                    *_root_options(pseudo),
                ],
                stdout=log,
                check=True,
            )
            _same(_lln(src), _lln(tmp), "unsquashfs -lln")
            subprocess.run(["unsquashfs", "-pf", verified, tmp], stdout=log, check=True)
            if not filecmp.cmp(pseudo, verified, shallow=False):
                raise Error("unsquashfs pseudo exports differ (metadata or contents)")
            os.replace(tmp, img)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # Older builds of the same pak are superseded.
    for n in os.listdir(cache):
        old = os.path.join(cache, n)
        if (
            n.startswith(pak + "-")
            and n.endswith(".img")
            and old != img
            and n[len(pak) + 1 : -4].isalnum()
        ):
            os.unlink(old)
    return img


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        prog="beanjar.firmware.cache", description="uncompressed copies of the firmware's paks"
    )
    p.add_argument("paks", nargs="*", default=["rootfs"])
    a = p.parse_args(argv)
    fw, _name = paths.TREE.firmware(os.environ.get("FW"))
    cache = os.environ.get("FW_CACHE") or default_cache()
    try:
        for pak in a.paks:
            print(build(fw, cache, pak), flush=True)
    except (Error, subprocess.CalledProcessError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
