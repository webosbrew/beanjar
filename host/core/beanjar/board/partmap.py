# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""LG's eMMC partition map for a firmware, as the VM's partinfo disk holds it.

The map is the Linux kernel's struct partmap_info of LG's block/partitions/lgemmc.c (W20L's
linux-4.4.3, lines 30-75): magic 0x20120716, cur_epk_ver, old_epk_ver, npartition (u8), the
device_info at 0x10, then 0x60-byte entries (name[32], offset u64, size u64, filename[32],
filesize, sw_ver, used, valid, mask_flags); the object LG's programs carry is 12368 bytes, 128
entries. Entry i is /dev/mmcblk0p(i+1); the last one (reserved) is neither printed nor a node. On
the eMMC the 12368 bytes are followed by their CRC-32/MPEG-2 (poly 0x04C11DB7, init 0xFFFFFFFF,
not reflected, no final xor), little-endian: what update writes and linkemulator checks, and what
a real set's partinfo partition holds.

Where a release's map comes from (targets.Release.partmap):
  PartmapImage(pak)        the table at the start of a shipped partition-map pak (W21A)
  PartmapFile(pak, path)    the `partinfo` object of the ELF file path in the pak, as it is: LG's
                            own default table (lginit_app2 of the tvservice pak, WT1H to W19P)
  PartmapLayout(...)        assembled here from the release row's entries, the header (magic,
                            versions, device) from a PartmapFile (W20L: see-init's stale table),
                            the sizes of paks and state disks it names; offsets ours, consecutive

The partinfo disk's image (image()): the 12368 bytes, the CRC, zeros to the size of the map's
partinfo entry. build() keeps it in the firmware cache (fwcache.default_cache or FW_CACHE):
<cache>/partinfo-<16 hex>.img, keyed by the source paks and the release row.

  python3 -m beanjar.board.partmap [FW]    print the map of FW (default: as the launcher's FW)
"""

import hashlib
import os
import struct
import subprocess
import sys
from collections import namedtuple

from ..firmware import cache as firmware_cache, targets
from .. import paths
from . import nvm

MAGIC = 0x20120716
TABLE_SIZE = 12368  # 0x50 + 128 * 0x60, the object in LG's programs
SLOTS = 128
KERNEL_SLOTS = 64  # lgemmc.c's PARTITION_MAX: what the kernel reads (0x1850 bytes)
ENTRY = 0x60
HEADER = struct.Struct("<IIIB3x")
DEVICE = struct.Struct("<32sQQIIiI")
PART = struct.Struct("<32sQQ32sIIBBxxI")
ALIGN = 1 << 20  # an assembled table's paks: their size rounded up to this (ours)
# firmware.updatable only: extra room on each inactive A/B-bank pak partition, so a larger variant
# firmware's pak (the updater checks pak.imageSize <= partition.size in /usr/sbin/update's
# SWUPDATE_FILE_UpdateSoftware) still fits a partition sized from THIS VM's own, smaller pak. A real
# set's partitions are fixed and over-provisioned; we have no retail o22 table, so this is headroom,
# not a claim about retail geometry. One ALIGN keeps the partition ALIGN-aligned.
UPDATABLE_HEADROOM = ALIGN

Entry = namedtuple("Entry", "name offset size filename filesize sw_ver used valid flags")
Map = namedtuple("Map", "magic cur_ver old_ver device entries")  # entries: all npartition of them


class Error(Exception):
    pass


def crc32_mpeg2(data):
    """CRC-32/MPEG-2 of data (the map's tail on the eMMC)."""
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


def _str(b):
    return b.split(b"\0", 1)[0].decode("latin-1")


def decode(blob):
    """The Map of a table (12368 bytes or more: a partinfo disk's start). Error if it is none."""
    if len(blob) < TABLE_SIZE:
        raise Error(f"{len(blob)} bytes: no partition map")
    magic, cur, old, n = HEADER.unpack_from(blob, 0)
    if magic != MAGIC:
        raise Error(f"magic 0x{magic:08x}, not 0x{MAGIC:08x}")
    if not 1 < n <= KERNEL_SLOTS + 1:
        raise Error(f"npartition {n}")
    dev = DEVICE.unpack_from(blob, 0x10)
    entries = []
    for i in range(n):
        name, off, size, fn, fsize, ver, used, valid, flags = PART.unpack_from(
            blob, 0x50 + i * ENTRY
        )
        entries.append(Entry(_str(name), off, size, _str(fn), fsize, ver, used, valid, flags))
    return Map(magic, cur, old, (_str(dev[0]),) + dev[1:], entries)


def encode(m):
    """The 12368 bytes of Map m."""
    out = bytearray(TABLE_SIZE)
    HEADER.pack_into(out, 0, m.magic, m.cur_ver, m.old_ver, len(m.entries))
    DEVICE.pack_into(out, 0x10, m.device[0].encode(), *m.device[1:])
    for i, e in enumerate(m.entries):
        PART.pack_into(
            out,
            0x50 + i * ENTRY,
            e.name.encode(),
            e.offset,
            e.size,
            e.filename.encode(),
            e.filesize,
            e.sw_ver,
            e.used,
            e.valid,
            e.flags,
        )
    return bytes(out)


def partitions(m):
    """[(N, Entry)]: the partitions the kernel registers and prints, mmcblk0pN (all but the last)."""
    return [(i + 1, e) for i, e in enumerate(m.entries[:-1])]


def proc_text(m):
    """/proc/partinfo as LG's kernel prints it (lgemmc.c proc_partinfo_show, lines 86-93)."""
    return "dev:    offset   size  name\n" + "".join(
        f'part{n}: {e.offset:08x} {e.size:08x} "{e.name}"\n' for n, e in partitions(m)
    )


def first(m, name):
    """(N, Entry) of the first partition named name (every consumer takes the first), or None."""
    return next(((n, e) for n, e in partitions(m) if e.name == name), None)


# ---------------------------------------------------------------------------- the firmware's map
def _cat(fw, pak, path, tree):
    r = subprocess.run(
        ["unsquashfs", "-cat", "-no-wildcards", os.path.join(fw, pak + ".pak"), path.lstrip("/")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if r.returncode:
        raise Error(f"{pak}.pak:{path}: {r.stderr.decode(errors='replace').strip() or 'not read'}")
    return r.stdout


def file_table(fw, source, tree=paths.TREE):
    """The 12368 bytes of the `partinfo` object of a PartmapFile's ELF file."""
    try:
        obj = nvm.elf_objects(_cat(fw, source.pak, source.path, tree), ("partinfo",)).get(
            "partinfo"
        )
    except (nvm.Error, struct.error, ValueError) as e:
        raise Error(f"{source.pak}.pak:{source.path}: {e}") from None
    if obj is None or len(obj) != TABLE_SIZE:
        raise Error(f"{source.pak}.pak:{source.path}: no {TABLE_SIZE}-byte partinfo object")
    decode(obj)
    return obj


def _size(fw, target, size):
    """An assembled entry's size: a number, ("pak", file): the pak rounded up to ALIGN, or ("disk",
    serial): the target's state disk of that serial."""
    if isinstance(size, int):
        return size
    kind, what = size
    if kind == "pak":
        src = os.path.join(fw, what + ".pak")
        if not os.path.isfile(src):
            raise Error(f"missing {src}")
        return -(-os.path.getsize(src) // ALIGN) * ALIGN
    if kind == "disk":
        for name, s in targets.state_disks(target):
            if name == what:
                return targets.size_bytes(s)
        raise Error(f"{targets.name(target)}: no state disk {what}")
    raise Error(f"entry size {size!r}")


def _inactive_pak_headroom(part, updatable):
    """UPDATABLE_HEADROOM for an inactive-bank (used=0) pak-sized entry when updatable, else 0: the
    only entries the stock updater writes and size-checks a flashed pak against. Active-bank (used=1)
    entries and fixed-size entries are left exactly as they are, so backing-disk sizes and node
    indices do not move."""
    if updatable and part.used == 0 and not isinstance(part.size, int) and part.size[0] == "pak":
        return UPDATABLE_HEADROOM
    return 0


def layout_table(fw, target, layout, tree=paths.TREE, updatable=False):
    """The 12368 bytes of an assembled table (PartmapLayout): the header and device of its header
    file's table, its entries at consecutive offsets from 0, then reserved (layout.reserved: its
    size, used, valid, flags) at the end, which is also the device's size.

    updatable (firmware.updatable only): each inactive A/B-bank pak partition gets UPDATABLE_HEADROOM
    extra room so a larger variant firmware's pak still fits (see _inactive_pak_headroom). Off, the
    table is byte-for-byte what a non-updatable VM has."""
    head = decode(file_table(fw, layout.header, tree))
    entries, off = [], 0
    for p in layout.parts:
        size = _size(fw, target, p.size) + _inactive_pak_headroom(p, updatable)
        entries.append(Entry(p.name, off, size, p.filename, 0, p.sw_ver, p.used, p.valid, p.flags))
        off += size
    size, used, valid, flags = layout.reserved
    entries.append(Entry("reserved", off, size, "", 0, 0, used, valid, flags))
    if len(entries) > KERNEL_SLOTS + 1:
        raise Error(f"{len(entries)} entries: more than the kernel's {KERNEL_SLOTS} slots")
    device = (head.device[0], off) + head.device[2:]
    return encode(Map(head.magic, head.cur_ver, head.old_ver, device, entries))


def table(fw, target, tree=paths.TREE, updatable=False):
    """The 12368-byte map of the firmware directory fw of target, from its release's source.

    updatable only matters for an assembled (PartmapLayout) source: it widens the inactive A/B bank
    (layout_table). A retail table (PartmapImage/PartmapFile) is real geometry and used as is."""
    src = targets.release(target, fw).partmap
    if isinstance(src, targets.PartmapImage):
        with open(os.path.join(fw, src.pak + ".pak"), "rb") as f:
            blob = f.read(TABLE_SIZE)
        decode(blob)
        return blob
    if isinstance(src, targets.PartmapFile):
        return file_table(fw, src, tree)
    return layout_table(fw, target, src, tree, updatable)


def image(blob):
    """The partinfo disk's bytes for the table blob: it, its CRC, zeros to the partinfo entry's size."""
    entry = first(decode(blob), "partinfo")
    if entry is None:
        raise Error("the map has no partinfo partition")
    data = blob[:TABLE_SIZE] + struct.pack("<I", crc32_mpeg2(blob[:TABLE_SIZE]))
    if entry[1].size < len(data):
        raise Error(f"partinfo partition of {entry[1].size} bytes")
    return data.ljust(entry[1].size, b"\0")


def _key(fw, target, cache, updatable=False):
    """What the cached image depends on: the source paks' sha256 (recorded as fwcache does), the
    assembled table's pak sizes and the release row, and updatable (the inactive bank's headroom), so
    the updatable and non-updatable images never share a cache slot."""
    src = targets.release(target, fw).partmap
    h = hashlib.sha256(repr(src).encode())
    h.update(b"updatable" if updatable else b"")
    if isinstance(src, (targets.PartmapFile, targets.PartmapImage)):
        h.update(firmware_cache.digest(fw, cache, src.pak).encode())
    else:
        h.update(firmware_cache.digest(fw, cache, src.header.pak).encode())
        h.update(repr(targets.state_disks(target)).encode())
        for p in src.parts:
            if not isinstance(p.size, int) and p.size[0] == "pak":
                f = os.path.join(fw, p.size[1] + ".pak")
                h.update(f"{p.size[1]} {os.path.getsize(f) if os.path.isfile(f) else -1}".encode())
    return h.hexdigest()[:16]


def image_path(fw, target, cache, updatable=False):
    os.makedirs(cache, exist_ok=True)
    return os.path.join(cache, f"partinfo-{_key(fw, target, cache, updatable)}.img")


def build(fw, target, cache, tree=paths.TREE, updatable=False):
    """The path of fw's partinfo disk image in cache, made if missing (under its lock, <image>.lock:
    VMs of one firmware cache start at once).

    updatable (firmware.updatable only) builds a separate image with the inactive A/B bank widened
    (table); the default image is unchanged, so a non-updatable VM's partinfo is byte-for-byte as
    before."""
    path = image_path(fw, target, cache, updatable)
    if os.path.isfile(path):
        return path
    with paths.file_lock(path + ".lock"):
        if not os.path.isfile(path):
            data = image(table(fw, target, tree, updatable))
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
    return path


def read(path):
    """The Map on a partinfo disk image (a VM's own, which the guest may have rewritten)."""
    with open(path, "rb") as f:
        return decode(f.read(TABLE_SIZE))


def same_layout(a, b):
    """True if Maps a and b have the same partitions (name, offset, size at each index): what a
    guest's rewrite (flags, file sizes, versions) leaves alone."""
    return [(e.name, e.offset, e.size) for e in a.entries] == [
        (e.name, e.offset, e.size) for e in b.entries
    ]


def nodes(m, target):
    """[(N, serial)]: the /dev/mmcblk0pN nodes the guest gets, each the disk of serial (the release's
    partitions and its LGUDID's, targets.partition_disks: name -> disk serial), at the first partition
    of that name. Error if the map lacks one."""
    out = []
    for name, serial in targets.partition_disks(target).items():
        f = first(m, name)
        if f is None:
            raise Error(f"{targets.name(target)}: the map has no partition {name}")
        out.append((f[0], serial))
    return sorted(out)


def disk_sizes(m, target):
    """{serial: bytes}: the size of each disk the map's partitions back, the largest of its names'
    entries (partinfo and mapbak share one)."""
    out = {}
    for name, serial in targets.partition_disks(target).items():
        f = first(m, name)
        if f is not None:
            out[serial] = max(out.get(serial, 0), f[1].size)
    return out


SECTOR = 512


def device_size(m):
    """The whole eMMC's size in bytes: the last entry (reserved) starts at the end of every
    partition, which is the device size (layout_table); for a firmware's own table its reserved is
    last too. Falls back to the header's device size where that is larger."""
    end = m.entries[-1].offset if m.entries else 0
    return max(end, m.device[1])


def dm_segments(m, target, overlay):
    """[(start, length, serial, src)] in 512-byte sectors, a dm-linear table for the whole eMMC as
    /dev/mmcblk0 (firmware.updatable). The whole address range [0, device_size) maps into the
    scratch overlay disk at the same absolute offset, so the stock updater's raw writes — which go
    to the inactive A/B bank at LG's absolute byte offsets — land in the overlay and are discarded,
    never touching a real backing disk.

    The updater only writes (and reads back) the inactive bank through the whole-disk node; it reads
    partinfo and the active partitions through their own /dev/mmcblk0pN nodes, which are unchanged.
    The active partitions are NOT folded into this device: device-mapper claims each backing device
    exclusively (block/bdev.c bd_may_claim), and those disks are already mounted (the squashfs paks)
    or are mounted/swapped later by webOS (the ext4 state disks, authcxt, efs), so stacking dm over
    them would fail with EBUSY or block webOS's own mount. A whole-disk overlay avoids that and still
    lets the updater complete its writes and the verify/swap chain. nodes(m, target) is validated so
    a broken map is still caught. Error on an unaligned device size."""
    nodes(
        m, target
    )  # the map must name every partition the release backs (same check as the nodes)
    total = device_size(m)
    if total % SECTOR:
        raise Error(f"device size {total} is not a multiple of {SECTOR}")
    return [(0, total // SECTOR, overlay, 0)]


def emmc_table_words(m, target, overlay):
    """The dm-linear table of dm_segments as boot.conf words, one per segment: start:len:serial:src
    (sectors). guest/initramfs/rootfs/bin/beanjar-emmc resolves each serial to its virtio disk and
    builds the dmsetup table from them."""
    return [f"{s}:{l}:{serial}:{src}" for s, l, serial, src in dm_segments(m, target, overlay)]


def overlay_nodes(m, target):
    """[(N, start, length)] in 512-byte sectors: the /dev/mmcblk0pN nodes for the partitions the
    stock updater touches that have NO backing disk of their own (firmware.updatable). Each is a
    dm-linear slice of the SAME scratch overlay the whole-disk /dev/mmcblk0 maps (dm_segments), at
    the partition's absolute offset and size — so a write at that offset through /dev/mmcblk0 and a
    read of /dev/mmcblk0pN are the same overlay bytes, and both are discarded at power-off.

    These are exactly the kernel-registered partitions that nodes(m, target) does NOT back with a
    real disk: the inactive A/B bank entries the updater writes, the crc32 record partition
    (crc32info) its SWUPDATE_Partition_SetCrc32 opens as /dev/mmcblk0p<idx>, and any gap/no-consumer
    partition. The partitions that already have a real backing disk (the active bank,
    partinfo/mapbak, the mounted state/pak disks) keep those nodes and are deliberately NOT listed
    here, so they are never remapped onto the overlay. Error on an unaligned offset or size.

    nodes(m, target) is validated first, so a broken map is caught the same way as the whole-disk
    table."""
    backed = {n for n, _ in nodes(m, target)}
    out = []
    for n, e in partitions(m):
        if n in backed:
            continue
        if e.offset % SECTOR or e.size % SECTOR:
            raise Error(
                f"partition {n} ({e.name}): offset {e.offset} size {e.size} "
                f"not a multiple of {SECTOR}"
            )
        out.append((n, e.offset // SECTOR, e.size // SECTOR))
    return out


def emmc_part_words(m, target):
    """The overlay-backed per-partition nodes of overlay_nodes as boot.conf words, one per node:
    N:start:length (sectors). guest/initramfs/rootfs/bin/beanjar-emmc makes each /dev/mmcblk0pN a
    dm-linear slice of the overlay disk (boot.conf's EMMC_OVERLAY) at that absolute start."""
    return [f"{n}:{start}:{length}" for n, start, length in overlay_nodes(m, target)]


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        prog="beanjar.board.partmap", description="a firmware's eMMC partition map"
    )
    p.add_argument(
        "fw", nargs="?", help="firmware directory or name (default: FW, as the launcher)"
    )
    p.add_argument("--image", help="decode this partinfo disk image instead")
    p.add_argument("--proc", action="store_true", help="print it as /proc/partinfo")
    a = p.parse_args(argv)
    try:
        if a.image:
            m = read(a.image)
        else:
            fw, name = paths.TREE.firmware(a.fw or os.environ.get("FW"))
            m = decode(table(fw, paths.firmware_target(fw, name)))
    except (Error, ValueError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    if a.proc:
        sys.stdout.write(proc_text(m))
        return 0
    print(
        f"magic 0x{m.magic:08x} cur 0x{m.cur_ver:08x} old 0x{m.old_ver:08x} npartition {len(m.entries)} "
        f"device {m.device[0]} size 0x{m.device[1]:x}"
    )
    for i, e in enumerate(m.entries):
        print(
            f"p{i + 1:<3} 0x{e.offset:09x} 0x{e.size:08x} {e.name:12} {e.filename:20} {e.filesize:#x} "
            f"{e.sw_ver:#x} {e.used} {e.valid} {e.flags}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
