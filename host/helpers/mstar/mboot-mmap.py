#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Extract an MStar webOS board's mmapger records out of its MBoot (boot.pak).

On these boards libkadaptor's MAdp_SYS_GetMMAP reads the kernel's /proc/mmapger/mmap.bin, and the
kernel's mmapger serves only the array MBoot hands it in ATAG_CUSTOM (0x5441000a): MAX_MMAP_SIZE
(96) records of five u32s {available, addr, gap_chk, len, memory_type}, in the kernel's mmapger
record order (LG's GPL kernels: drivers/mstar2/drv/system_LG/mmapger.c, parse_tag_custom). MBoot
compiles in one such table per memory configuration. The board's is the one that agrees with every
record of the firmware's own TEE memory map (bsppart's bsp/tee/mmap.ini, an MStar mmap header whose
records name their mmapger record, "/* E_MMAP_ID_X = NAME */"); no table or several is an error.
The records are the table's up to its last nonzero one; the rest must be zero.

No firmware-derived data belongs in the source tree. The output belongs in build/.
"""

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

_installed = Path(__file__).resolve().parents[2]
sys.path.insert(
    0,
    str(
        _installed / "core"
        if (_installed / "installation.toml").is_file()
        else Path(__file__).resolve().parents[3] / "host/core"
    ),
)
from beanjar.firmware.memory import binary_records

ROWS = 96  # MAX_MMAP_SIZE of tag_custom
ROW = 20
# The kernel's mmapger record names (MMAPGerTag[]) up to the last the TEE's maps name; the same in
# LG's GPL kernels for M2 (kernel-DM2) and M3 (kernel-404-glacier) and in SimpleSmart's kernel.
NAMES = (
    "DUMMY_MEM", "LINUX_MEM", "EMAC_MEM", "XIP_MEM", "NFDRM_NUTTX_MEM", "NFDRM_HW_AES_BUF",
    "HW_SECURE_BUFFER", "SECURE_SHM", "SECURE_UPDATE_AREA", "SECURE_TSP", "BIN_MEM",
    "MAD_BASE_BUFFER", "VD_3DCOMB", "G3D_TEXTURE0_BUF", "LINUX_LOW_MEM_RESERVE", "VE_FRAMEBUFFER",
    "TTX_BUF", "PVR_DOWNLOAD_BUFFER", "PVR_UPLOAD_BUFFER", "TSP_BUF", "VQ_BUF", "MAILBOX", "DIP",
    "MPOOL", "PNG_OUTPUT_BUF", "PNG_INPUT_BUF", "VDEC_GN_IAP", "GOP_FRAMEBUFFER", "GOP_REGDMA",
    "GFX_VQ_BUFFER", "VBI_1_BUFFER", "VBI_2_BUFFER", "LD_BUFFER", "PIU_BUF", "DUMMY",
    "DUAL_STREAM_DUMY", "VDEC_CPU", "SVD_CPU", "VDEC_BITSTREAM", "VDEC_SUB_BITSTREAM",
    "VDEC_N1_BITSTREAM", "VDEC_N2_BITSTREAM", "VDEC_FRAMEBUFFER", "VDEC_SUB_FRAMEBUFFER",
    "NFDRM_VDEC_SHARE_MEM",
)  # fmt: skip
# where a firmware directory has the TEE's mmap.ini: (pak glob, path in it)
INI_SOURCES = (("bsppart*.pak", "bsp/tee/mmap.ini"), ("vend.pak", "mmap.ini"))


class Ambiguous(ValueError):
    pass


def ini_records(text):
    """{mmapger id: (available, addr, gap_chk, len, miu)} of the mmap header text's records that
    name their mmapger record, and its MIU geometry (MIU_DRAM_LEN0, MIU_DRAM_LEN1, MIU_INTERVAL)."""
    defs = dict(re.findall(r"^#define\s+(\w+)\s+(0x[0-9A-Fa-f]+)\s*$", text, re.MULTILINE))
    types = dict(re.findall(r"^#define\s+(\w+)_MEMORY_TYPE\s+\(([^)]*)\)", text, re.MULTILINE))
    try:
        geometry = tuple(
            int(defs[k], 16) for k in ("MIU_DRAM_LEN0", "MIU_DRAM_LEN1", "MIU_INTERVAL")
        )
    except KeyError as e:
        raise Ambiguous(f"mmap.ini: no {e.args[0]}") from None
    out = {}
    for ini, name in re.findall(r"^/\*\s*(E_\w+)\s*=\s*(\w+)\s*\*/", text, re.MULTILINE):
        if name not in NAMES:
            raise Ambiguous(f"mmap.ini: {ini} names {name}, not a known mmapger record")
        try:
            values = tuple(
                int(defs[f"{ini}_{k}"], 16) for k in ("AVAILABLE", "ADR", "GAP_CHK", "LEN")
            )
            kind = types[ini].replace(" ", "").split("|")
        except KeyError:
            raise Ambiguous(f"mmap.ini: {ini} is incomplete") from None
        if kind[0] not in ("MIU0", "MIU1"):
            raise Ambiguous(f"mmap.ini: {ini} in {kind[0]}")
        rid = NAMES.index(name)
        if rid in out:
            raise Ambiguous(f"mmap.ini: two records of {name}")
        out[rid] = values + (kind[0] == "MIU1",)
    if not out:
        raise Ambiguous("mmap.ini names no mmapger record")
    return out, geometry


def _row(data, base, i):
    return struct.unpack_from("<5I", data, base + i * ROW)


def _valid(rows):
    """Whether rows are a tag_custom mmapger array: types of MIU0/1 and UNCACHE only, available
    not past addr and gap_chk their difference, zero rows only after the last nonzero one."""
    last = max((i for i, r in enumerate(rows) if any(r)), default=-1)
    if last < 0 or any(any(r) for r in rows[last + 1 :]):
        return 0
    for available, addr, gap, size, kind in rows[: last + 1]:
        if kind & ~5 or available > addr or gap != addr - available:
            return 0
    return last + 1


def extract(boot, ini):
    """(rows, geometry): the records of the MBoot image boot's table that agrees with every record
    of the mmap header text ini, up to its last nonzero one; ini's MIU geometry."""
    want, geometry = ini_records(ini)
    bases = None
    for rid, (available, addr, gap, size, _) in want.items():
        pattern = struct.pack("<4I", available, addr, gap, size)
        found, at = set(), boot.find(pattern)
        while at >= 0:
            if at >= rid * ROW and at - rid * ROW + ROWS * ROW <= len(boot):
                found.add(at - rid * ROW)
            at = boot.find(pattern, at + 1)
        bases = found if bases is None else bases & found
    tables = []
    for base in sorted(bases):
        rows = [_row(boot, base, i) for i in range(ROWS)]
        count = _valid(rows)
        if count and all((rows[rid][4] & 1) == miu for rid, (*_, miu) in want.items()):
            tables.append(rows[:count])
    if len(tables) != 1:
        raise Ambiguous(f"{len(tables)} MBoot tables agree with every mmap.ini record")
    return tables[0], geometry


def records(rows, geometry, length):
    """The mmap.bin bytes of rows, checked against the module's MIU length."""
    len0, len1, _ = geometry
    if len0 != length or len1 != length:
        raise Ambiguous(f"mmap.ini MIUs {len0:#x}/{len1:#x}, not the module's {length:#x}")
    out = b"".join(struct.pack("<5I", *r) for r in rows)
    binary_records(out, len(rows), length)
    return out


def _cat(pak, path):
    r = subprocess.run(
        ["unsquashfs", "-cat", "-no-wildcards", str(pak), path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return r.stdout if r.returncode == 0 and r.stdout else None


def firmware_inputs(fw):
    """(boot.pak bytes, mmap.ini text) of the firmware directory fw."""
    fw = Path(fw)
    try:
        boot = (fw / "boot.pak").read_bytes()
    except OSError as e:
        raise Ambiguous(f"no boot.pak in {fw}: {e.strerror}") from None
    for pattern, path in INI_SOURCES:
        for pak in sorted(fw.glob(pattern)):
            data = _cat(pak, path)
            if data:
                return boot, data.decode("latin-1")
    raise Ambiguous(f"no TEE mmap.ini in {fw}")


REPO = Path(__file__).resolve().parents[3]


def cached(fw, name, interval, length):
    """(build/mstar/<name>/mmap.bin, its record count) of the firmware directory fw, reused while
    boot.pak's and mmap.ini's hashes, the MIU geometry and the output digest agree."""
    from beanjar import paths

    tree = paths.Tree(_installed if (_installed / "installation.toml").is_file() else REPO)
    boot, ini = firmware_inputs(fw)
    out = Path(tree.cache("mstar", name, "mmap.bin"))
    want = {
        "source": "MBoot",
        "boot_sha256": hashlib.sha256(boot).hexdigest(),
        "ini_sha256": hashlib.sha256(ini.encode("latin-1")).hexdigest(),
        "miu_interval": interval,
        "miu_length": length,
    }
    try:
        have = json.loads(out.with_suffix(".json").read_text())
        encoded = out.read_bytes()
        if (
            isinstance(have, dict)
            and all(have.get(k) == v for k, v in want.items())
            and isinstance(have.get("count"), int)
            and have.get("output_sha256") == hashlib.sha256(encoded).hexdigest()
        ):
            binary_records(encoded, have["count"], length)
            return out, have["count"]
    except (OSError, ValueError):
        pass
    rows, geometry = extract(boot, ini)
    if geometry[2] != interval:
        raise Ambiguous(f"mmap.ini MIU_INTERVAL {geometry[2]:#x}, not the module's {interval:#x}")
    encoded = records(rows, geometry, length)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encoded)
    out.with_suffix(".json").write_text(
        json.dumps(
            {**want, "count": len(rows), "output_sha256": hashlib.sha256(encoded).hexdigest()},
            indent=2,
        )
        + "\n"
    )
    return out, len(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "firmware", type=Path, help="the firmware directory (its boot.pak and TEE mmap.ini)"
    )
    p.add_argument("--name", help="the firmware name (default: the directory's)")
    p.add_argument("--miu-interval", type=lambda x: int(x, 0), required=True)
    p.add_argument("--miu-length", type=lambda x: int(x, 0), required=True)
    args = p.parse_args()
    out, count = cached(
        args.firmware,
        args.name or args.firmware.resolve().name,
        args.miu_interval,
        args.miu_length,
    )
    print(out)
    print(f"mmap_ids={count}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as e:
        raise SystemExit(f"mboot-mmap: {e}")
