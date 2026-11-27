#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Extract LM14's pure MAdp_SYS_GetMMAP switch into mmapger records.

The stock leaf stores constant address/length pairs. This recognises that compiler
pattern only, failing on different code, calls, relocated literals or bad ranges.
No firmware-derived data belongs in the source tree. The output belongs in build/.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import sys

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


class Ambiguous(ValueError):
    pass


def _extract(data):
    from elftools.elf.elffile import ELFFile

    elf = ELFFile(io.BytesIO(data))
    if elf.elfclass != 32 or not elf.little_endian or elf["e_machine"] != "EM_ARM":
        raise Ambiguous("expected little-endian ELF32 ARM")
    dynsym = elf.get_section_by_name(".dynsym")
    if dynsym is None:
        raise Ambiguous("missing dynamic symbols")
    syms = [
        s
        for s in dynsym.iter_symbols()
        if s.name == "MAdp_SYS_GetMMAP" and s["st_shndx"] != "SHN_UNDEF"
    ]
    if len(syms) != 1 or syms[0]["st_value"] & 3:
        raise Ambiguous("expected one ARM GetMMAP definition")
    start, size = syms[0]["st_value"], syms[0]["st_size"]
    if not 64 <= size <= 65536:
        raise Ambiguous("unbounded GetMMAP definition")
    end = start + size
    segments = [s for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
    relocated = {}
    for section in elf.iter_sections():
        if section["sh_type"] in ("SHT_REL", "SHT_RELA"):
            for r in section.iter_relocations():
                relocated[r["r_offset"]] = (r["r_info_type"], r["r_info_sym"])

    def word(addr, relative=False):
        reloc = relocated.get(addr)
        if (
            not start <= addr <= end - 4
            or addr & 3
            or (reloc and not (relative and reloc == (23, 0)))
        ):  # R_ARM_RELATIVE link VA
            raise Ambiguous(f"outside leaf or relocated word at {addr:#x}")
        for s in segments:
            if s["p_vaddr"] <= addr <= s["p_vaddr"] + s["p_filesz"] - 4:
                return struct.unpack_from("<I", data, s["p_offset"] + addr - s["p_vaddr"])[0]
        raise Ambiguous("unmapped leaf word")

    prefix = (0xE52DB004, 0xE28DB000, 0xE24DD014, 0xE50B0008, 0xE50B100C, 0xE50B2010, 0xE51B3008)
    if tuple(word(start + 4 * i) for i in range(7)) != prefix:
        raise Ambiguous("unknown GetMMAP prologue")
    cmp = word(start + 28)
    if cmp & 0xFFFFFF00 != 0xE3530000 or word(start + 32) != 0x979FF103:
        raise Ambiguous("unknown switch bound or dispatch")
    count = (cmp & 255) + 1
    if count > 256:
        raise Ambiguous("too many records")
    table = start + 40
    if table + count * 4 > end:
        raise Ambiguous("truncated jump table")

    def branch(pc, op):
        if op & 0xFF000000 != 0xEA000000:
            raise Ambiguous(f"expected unconditional branch at {pc:#x}")
        delta = op & 0xFFFFFF
        if delta & 0x800000:
            delta -= 1 << 24
        return pc + 8 + delta * 4

    def constant(pc):
        op = word(pc)
        if op & 0xFFFFF000 == 0xE3A02000:  # mov r2, ARM rotated immediate
            value, rotate = op & 255, ((op >> 8) & 15) * 2
            return ((value >> rotate) | (value << (32 - rotate))) & 0xFFFFFFFF
        if op & 0xFFFFF000 == 0xE59F2000:  # ldr r2, [pc, #literal]
            return word(pc + 8 + (op & 0xFFF))
        raise Ambiguous(f"nonconstant output at {pc:#x}")

    epilogue = (0xE1A00003, 0xE28BD000, 0xE8BD0800, 0xE12FFF1E)

    def false_return(pc):
        if word(pc) != 0xE3A03000:
            raise Ambiguous("unknown false return value")
        target = branch(pc + 4, word(pc + 4))
        if tuple(word(target + 4 * i) for i in range(4)) != epilogue:
            raise Ambiguous("unknown false return epilogue")

    false_return(branch(start + 36, word(start + 36)))

    def row(pc):
        values = []
        for offset in (12, 16):
            if word(pc) != 0xE51B3000 | offset:
                if not values and word(pc) == 0xE3A03000:
                    false_return(pc)
                    return (0, 0)
                raise Ambiguous(f"unknown output pointer at {pc:#x}")
            values.append(constant(pc + 4))
            if word(pc + 8) != 0xE5832000:
                raise Ambiguous("unknown constant store")
            pc += 12
        target = branch(pc, word(pc))
        if tuple(word(target + 4 * i) for i in range(5)) != (
            0xE3A03001,
            0xE1A00003,
            0xE28BD000,
            0xE8BD0800,
            0xE12FFF1E,
        ):
            raise Ambiguous("unknown true return")
        return tuple(values)

    return [row(word(table + 4 * i, relative=True)) for i in range(count)]


def extract(data):
    from elftools.common.exceptions import ELFError

    try:
        return _extract(data)
    except (ELFError, struct.error, IndexError) as e:
        raise Ambiguous(f"malformed ELF: {e}") from e


def _rotated(op):
    """The value of an ARM data-processing rotated immediate."""
    value, rotate = op & 255, ((op >> 8) & 15) * 2
    return ((value >> rotate) | (value << (32 - rotate))) & 0xFFFFFFFF


def _calls(data, caller, callees):
    """The calls in the defined function caller to the imported functions callees, in code order:
    [(callee, its constant first argument r0, else None)]. The constant is the stock pattern's: bl
    to the callee's PLT stub, whose last write to r0 within the six instructions before is
    mov r0, #imm."""
    from elftools.elf.elffile import ELFFile

    elf = ELFFile(io.BytesIO(data))
    segments = [s for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]

    def word(addr):
        for s in segments:
            if s["p_vaddr"] <= addr <= s["p_vaddr"] + s["p_filesz"] - 4:
                return struct.unpack_from("<I", data, s["p_offset"] + addr - s["p_vaddr"])[0]
        raise Ambiguous(f"unmapped word at {addr:#x}")

    dynsym = elf.get_section_by_name(".dynsym")
    relplt = elf.get_section_by_name(".rel.plt")
    plt = elf.get_section_by_name(".plt")
    if dynsym is None or relplt is None or plt is None:
        raise Ambiguous("missing dynamic symbols or PLT")
    slots = {}  # GOT slot -> callee (R_ARM_JUMP_SLOT)
    for r in relplt.iter_relocations():
        name = dynsym.get_symbol(r["r_info_sym"]).name
        if r["r_info_type"] == 22 and name in callees:
            if name in slots.values():
                raise Ambiguous(f"expected one PLT slot of {name}")
            slots[r["r_offset"]] = name
    stubs = {}  # PLT stub -> callee
    for pc in range(plt["sh_addr"], plt["sh_addr"] + plt["sh_size"] - 8, 4):
        a, b, c = word(pc), word(pc + 4), word(pc + 8)
        if (a & 0xFFFFF000, b & 0xFFFFF000, c & 0xFFFFF000) == (0xE28FC000, 0xE28CC000, 0xE5BCF000):
            got = pc + 8 + _rotated(a) + _rotated(b) + (c & 0xFFF)
            if got in slots:
                stubs[pc] = slots[got]
    missing = set(callees) - set(stubs.values())
    if missing:
        raise Ambiguous(f"no PLT stub of {', '.join(sorted(missing))}")
    defs = [s for s in dynsym.iter_symbols() if s.name == caller and s["st_shndx"] != "SHN_UNDEF"]
    if len(defs) != 1 or defs[0]["st_value"] & 3 or not 0 < defs[0]["st_size"] <= 65536:
        raise Ambiguous(f"expected one ARM definition of {caller}")
    start = defs[0]["st_value"]
    out = []
    for pc in range(start, start + defs[0]["st_size"], 4):
        op = word(pc)
        if op & 0xFF000000 != 0xEB000000:
            continue
        delta = op & 0xFFFFFF
        if delta & 0x800000:
            delta -= 1 << 24
        callee = stubs.get(pc + 8 + delta * 4)
        if callee is None:
            continue
        arg = None
        for back in range(pc - 4, max(start, pc - 24) - 4, -4):
            prev = word(back)
            if prev & 0xFFFFF000 == 0xE3A00000:  # mov r0, #imm
                arg = _rotated(prev)
                break
            # another write to r0: data processing with Rd r0 (not a compare), a load, a call
            if (
                (prev & 0x0C00F000 == 0 and (prev >> 21) & 15 not in (8, 9, 10, 11))
                or prev & 0x0C10F000 == 0x04100000
                or prev & 0x0F000000 == 0x0B000000
            ):
                break
        out.append((callee, arg))
    return out


# record_ids' keys, in the order of their module parameters
IDS = ("mmap_ids", "lx_mem_id", "lx_mem2_id", "gop_fb")


def record_ids(data, count):
    """The mmapger record IDs mstar-utopia.ko takes from the firmware (its module parameters) out
    of libmadp's code, count the records of GetMMAP: LX_MEM and LX_MEM2, the first two records
    MAdp_MPool_Init asks GetMMAP for (it checks them against SYS_INFO), and GOP_FRAMEBUFFER, the
    record whose block gfx_Init gets (MAdp_MPool_GetBlockPa) last before it makes the GOP's pool
    (gfx_CreatePool)."""
    from elftools.common.exceptions import ELFError

    try:
        mmap = _calls(data, "MAdp_MPool_Init", ("MAdp_SYS_GetMMAP",))[:2]
        gfx = _calls(data, "gfx_Init", ("MAdp_MPool_GetBlockPa", "gfx_CreatePool"))
    except (ELFError, struct.error, IndexError) as e:
        raise Ambiguous(f"malformed ELF: {e}") from e
    if len(mmap) != 2 or None in (a for _, a in mmap):
        raise Ambiguous("MAdp_MPool_Init: expected two GetMMAP calls of constant records")
    (_, lx_mem), (_, lx_mem2) = mmap
    pool = next((i for i, (c, _) in enumerate(gfx) if c == "gfx_CreatePool"), None)
    blocks = [a for c, a in gfx[:pool] if c == "MAdp_MPool_GetBlockPa"] if pool else []
    if not blocks or blocks[-1] is None:
        raise Ambiguous(
            "gfx_Init: expected a GetBlockPa of a constant record before gfx_CreatePool"
        )
    gop_fb = blocks[-1]
    ids = {"mmap_ids": count, "lx_mem_id": lx_mem, "lx_mem2_id": lx_mem2, "gop_fb": gop_fb}
    if not all(0 <= v < count for k, v in ids.items() if k != "mmap_ids"):
        raise Ambiguous(f"record IDs outside GetMMAP's {count} records: {ids}")
    return ids


def live_rows(path):
    rows = []
    try:
        for index, line in enumerate(path.read_text().splitlines()):
            fields = line.split()
            if len(fields) != 3 or int(fields[0]) != index:
                raise Ambiguous("live dump IDs must be consecutive from zero")
            rows.append(tuple(int(v, 16) for v in fields[1:]))
    except ValueError as e:
        raise Ambiguous(f"invalid live dump: {e}") from e
    return rows


def records(rows, interval, length):
    if not 0 < length <= interval <= 0xFFFFFFFF:
        raise Ambiguous("invalid MIU geometry")
    out = bytearray()
    for addr, size in rows:
        miu = int(addr >= interval)
        offset = addr - miu * interval
        if offset > length or size > length - offset:
            raise Ambiguous("GetMMAP record outside MIUs")
        out += struct.pack("<5I", offset, offset, 0, size, 4 | miu)
    return bytes(out)


REPO = Path(__file__).resolve().parents[3]


def cached(fw, name, interval, length):
    """(build/mstar/<name>/mmap.bin, its record IDs) of the firmware directory fw (its vendor pak's
    libmadp.so: extract's records, record_ids' IDs), reused while the source hash, MIU geometry,
    IDs and output digest agree. Older metadata without IDs or an output digest is regenerated on
    a host with pyelftools. A host with the cache copied from another needs no pyelftools."""
    import subprocess
    from beanjar import paths

    tree = paths.Tree(_installed if (_installed / "installation.toml").is_file() else REPO)
    r = subprocess.run(
        ["unsquashfs", "-cat", "-no-wildcards", str(Path(fw) / "vend.pak"), "libmadp.so"],
        stdout=subprocess.PIPE,
    )
    if r.returncode != 0 or not r.stdout:
        raise Ambiguous(f"no libmadp.so in {fw}/vend.pak")
    data = r.stdout
    out = Path(tree.cache("mstar", name, "mmap.bin"))
    want = {
        "source": "MAdp_SYS_GetMMAP",
        "sha256": hashlib.sha256(data).hexdigest(),
        "miu_interval": interval,
        "miu_length": length,
    }
    try:
        have = json.loads(out.with_suffix(".json").read_text())
        if (
            isinstance(have, dict)
            and all(have.get(k) == v for k, v in want.items())
            and out.is_file()
        ):
            encoded = out.read_bytes()
            ids = have.get("ids")
            if (
                isinstance(ids, dict)
                and set(ids) == set(IDS)
                and all(isinstance(v, int) and v >= 0 for v in ids.values())
                and ids["mmap_ids"] == have.get("count")
                and have.get("output_sha256") == hashlib.sha256(encoded).hexdigest()
            ):
                binary_records(encoded, ids["mmap_ids"], length)
                return out, ids
    except (OSError, ValueError):
        pass
    rows = extract(data)
    ids = record_ids(data, len(rows))
    encoded = records(rows, interval, length)
    binary_records(encoded, len(rows), length)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encoded)
    out.with_suffix(".json").write_text(
        json.dumps(
            {
                **want,
                "count": len(rows),
                "ids": ids,
                "output_sha256": hashlib.sha256(encoded).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    return out, ids


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("library", type=Path, help="libmadp.so, or with --cache the firmware directory")
    p.add_argument("--output", type=Path)
    p.add_argument(
        "--cache",
        action="store_true",
        help="write/reuse build/mstar/<name>/mmap.bin; print its path, then its record IDs as"
        " mstar-utopia.ko's parameters on one line",
    )
    p.add_argument("--name", help="the firmware name for --cache (default: the directory's)")
    p.add_argument("--miu-interval", type=lambda x: int(x, 0), required=True)
    p.add_argument("--miu-length", type=lambda x: int(x, 0), required=True)
    p.add_argument("--verify-live", type=Path)
    p.add_argument("--count", type=int, help="required configured mmap_ids count")
    args = p.parse_args()
    if args.cache:
        out, ids = cached(
            args.library,
            args.name or args.library.resolve().name,
            args.miu_interval,
            args.miu_length,
        )
        if args.count is not None and ids["mmap_ids"] != args.count:
            raise Ambiguous(f"GetMMAP has {ids['mmap_ids']} records; expected {args.count}")
        print(out)
        print(" ".join(f"{k}={v}" for k, v in ids.items()))
        return
    if not args.output:
        p.error("--output is required without --cache")
    data = args.library.read_bytes()
    rows = extract(data)
    if args.count is not None and len(rows) != args.count:
        raise Ambiguous(f"GetMMAP has {len(rows)} records; configured mmap_ids is {args.count}")
    if args.verify_live:
        live = live_rows(args.verify_live)
        if rows != live:
            raise Ambiguous("static table differs from live GetMMAP")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = records(rows, args.miu_interval, args.miu_length)
    binary_records(encoded, len(rows), args.miu_length)
    args.output.write_bytes(encoded)
    args.output.with_suffix(".json").write_text(
        json.dumps(
            {
                "source": "MAdp_SYS_GetMMAP",
                "sha256": hashlib.sha256(data).hexdigest(),
                "count": len(rows),
                "miu_interval": args.miu_interval,
                "miu_length": args.miu_length,
                "output_sha256": hashlib.sha256(encoded).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"{len(rows)} firmware-derived mmapger records")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as e:
        raise SystemExit(f"mmap-table: {e}")
