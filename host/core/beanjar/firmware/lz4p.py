# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""LG's LZ4P paks as a SquashFS image the guest kernel mounts: NetCast 4's lgapp (lgap.pak, RELEASE).

An LZ4P image is LG's block wrapper ("LZ4P" magic, raw LZ4 blocks) around a filesystem image. In
NetCast 4's lgapp that is a cramfs of LG's kernel's CONFIG_CRAMFS_LINEAR_XIP (lginit_app mounts the
partition as "lgapp_xip", a filesystem of LG's kernel): RELEASE is stored uncompressed from a page
boundary for the kernel to map in place, and it is larger than a cramfs inode's 24-bit size field
holds (25027700 bytes in 04.28.15, the field says 8250484). Mainline cramfs reads neither.

So the launcher unpacks the pak with epk2extract, which decodes LZ4P and extracts LG's XIP cramfs
(its uncramfs), and packs the tree with mksquashfs (official squashfs-tools), all files root's as on
the set. Nothing of our own decodes or parses either format. The image is kept in the firmware
cache (firmware.cache.default_cache or FW_CACHE) as <pak>-<first 16 hex of the source's sha256>.sqfs, made
once per firmware; the scratch tree is removed.

  python3 -m beanjar.firmware.lz4p PAK OUT    write PAK's SquashFS image to OUT
"""

import os
import shutil
import subprocess
import sys
import tempfile

from .. import paths
from . import cache as firmware_cache

MAGIC = b"LZ4P"


class Error(Exception):
    pass


def is_lz4p(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == MAGIC
    except OSError:
        return False


def epk2extract():
    """The epk2extract program, as firmware.find_epk2extract finds it without a library."""
    found = os.environ.get("BEANJAR_EPK2EXTRACT") or shutil.which("epk2extract")
    if found:
        return found
    local = os.path.expanduser("~/.local/bin/epk2extract")
    if os.access(local, os.X_OK):
        return local
    raise Error("epk2extract not found (PATH, ~/.local/bin, or BEANJAR_EPK2EXTRACT)")


def convert(src, out, work, log=sys.stderr):
    """Write src's (an LZ4P pak) SquashFS image to out, unpacking in the directory work."""
    # epk2extract writes beside its input: <name>.unlz4, then <name>.unlz4.uncramfs/; its input is
    # a link in work, so nothing is written beside the firmware
    os.symlink(os.path.abspath(src), os.path.join(work, "pak"))
    r = subprocess.run(
        [epk2extract(), "pak"], cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    tree = os.path.join(work, "pak.unlz4.uncramfs")
    if r.returncode or not os.path.isdir(tree):
        tail = r.stdout.decode(errors="replace").strip().splitlines()[-3:]
        raise Error(f"epk2extract did not unpack {src} to a cramfs tree: {' / '.join(tail)}")
    subprocess.run(
        [
            "mksquashfs",
            tree,
            out,
            "-noappend",
            "-quiet",
            "-no-progress",
            "-all-root",
            "-comp",
            "lzo",
        ],
        stdout=log,
        check=True,
    )


def image_path(fw, cache, pak, record=True):
    """Where the SquashFS image of fw's LZ4P pak is (or goes) in cache."""
    return os.path.join(cache, f"{pak}-{firmware_cache.digest(fw, cache, pak, record)[:16]}.sqfs")


def build(fw, cache, pak, log=sys.stderr):
    """The SquashFS image of fw's LZ4P pak in cache, made unless it is there: its path. Made under
    its lock (<image>.lock), so VMs of one firmware starting at once make it once."""
    os.makedirs(cache, exist_ok=True)
    img = image_path(fw, cache, pak)
    if os.path.isfile(img):
        return img
    with paths.file_lock(img + ".lock"):
        if os.path.isfile(img):
            return img
        src = os.path.join(fw, pak + ".pak")
        print(f"building {img} from {src}", file=log, flush=True)
        tmp = f"{img}.tmp{os.getpid()}"
        try:
            with tempfile.TemporaryDirectory(prefix=".lz4p-", dir=cache) as work:
                convert(src, tmp, work, log)
            os.replace(tmp, img)
        except subprocess.CalledProcessError as e:
            raise Error(f"mksquashfs failed: {e}") from None
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return img


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python3 -m beanjar.firmware.lz4p PAK OUT", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(
        prefix="lz4p-", dir=os.path.dirname(os.path.abspath(argv[1]))
    ) as work:
        convert(argv[0], argv[1], work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
