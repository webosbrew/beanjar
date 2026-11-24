# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The EEPROM fields a NetCast 4.5 set leaves its factory with, read out of the firmware's own
RELEASE (Nvm.factory: the pak and path of it), for a VM's new chip (nvm.new_chip): every byte
0xFF but

  the first-boot marker  DDI_NVM_FinishFirstBootUp's DDI_NVM_Write(offset, 4, &marker), which
                         MAIN_StartApplication makes after the set's first power-on; without it
                         every boot is a factory first power-on (UI_SUMODE_DoPowerOnlyStartApp)
  the preserve DB        DDI_NVM_InitPreserveDB's writes when its mark does not match, with the
                         defaults its own table holds (the mark, InstopReserve, INSTOP_INFO, the
                         baud, 1stBootAfterInstop): INSTOP_INFO stays the default 0, not the 1
                         SUMODE_DoInStop leaves, so no first-use wizard runs (its completion state
                         is in the eMMC registry ring, which the emulator does not prepare)
  the debug status       OSA_MD_GetDebugStatus's DDI_NVM_Read(offset, 1, ...): one byte, the
                         values up to its `cmp r0, #N` (above it reads as N, RELEASE)

The functions are found by their symbols and their few instructions decoded (ARM: mov/movw/movt/
mvn, add/sub with an immediate, ldr of a literal, str to [sp], bl); Error when a function is missing
or its code is not of that shape, so a build whose layout differs is refused rather than given wrong
bytes. NetCast 4's RELEASE (H13) has the same fields in code of another shape: literal pools within
the functions' sizes, a conditional mvn on the early return, the debug status's range checked as
`sub r3, r0, #LOW; cmp r3, #N` (values LOW to LOW + N)."""

import struct

FINISH = "DDI_NVM_FinishFirstBootUp"
PRESERVE = "DDI_NVM_InitPreserveDB"
DEBUG = "OSA_MD_GetDebugStatus"
READ = "DDI_NVM_Read"
WRITE = "DDI_NVM_Write"


class Error(Exception):
    pass


def _elf(data, wanted=(FINISH, PRESERVE, DEBUG, READ, WRITE)):
    """(functions {name: (address, size)} of the names wanted, loads [(vaddr, offset, filesz)]) of
    an ELF32 LE file; Error when one of them is not defined."""
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise Error("not a 32-bit little-endian ELF file")
    e_phoff, e_shoff = struct.unpack_from("<II", data, 28)
    e_phentsize, e_phnum, e_shentsize, e_shnum = struct.unpack_from("<HHHH", data, 42)
    loads = []
    for i in range(e_phnum):
        p_type, p_offset, p_vaddr, _, p_filesz = struct.unpack_from(
            "<IIIII", data, e_phoff + i * e_phentsize
        )
        if p_type == 1:
            loads.append((p_vaddr, p_offset, p_filesz))
    sections = [
        struct.unpack_from("<IIIIIIIIII", data, e_shoff + i * e_shentsize) for i in range(e_shnum)
    ]
    wanted = set(wanted)
    found = {}
    for table in [s for s in sections if s[1] in (2, 11)]:  # SHT_SYMTAB, SHT_DYNSYM
        _, _, _, _, sym_off, sym_size, strtab_index, _, _, sym_entsize = table
        str_off = sections[strtab_index][4]
        for off in range(sym_off, sym_off + sym_size, sym_entsize):
            st_name, st_value, st_size, st_info, _, st_shndx = struct.unpack_from(
                "<IIIBBH", data, off
            )
            if st_info & 0xF != 2 or st_shndx == 0:  # a defined STT_FUNC
                continue
            name = data[str_off + st_name : data.index(b"\0", str_off + st_name)].decode()
            if name in wanted:
                found.setdefault(name, (st_value & ~1, st_size))
    missing = sorted(wanted - set(found))
    if missing:
        raise Error(f"no {', '.join(missing)}")
    return found, loads


def _read(data, loads, addr, size):
    for vaddr, offset, filesz in loads:
        if vaddr <= addr and addr + size <= vaddr + filesz:
            return data[offset + addr - vaddr : offset + addr - vaddr + size]
    raise Error(f"0x{addr:x}: in no loaded part of the file")


def _rotated(op):
    imm, rot = op & 0xFF, (op >> 8 & 0xF) * 2
    return (imm >> rot | imm << (32 - rot)) & 0xFFFFFFFF


def calls(data, loads, func, size):
    """[(target, {reg: value}, stack)]: the bl calls of the function at func (size bytes) in code
    order, with the registers r0-r4 known to hold a constant then (an address: base + offset) and
    the word last stored at [sp]; ("cmp", {"imm": N, "low": LOW}, stack) for a cmp rn, #N whose rn
    is an unknown value minus LOW (0: the value itself). Error on a conditional instruction other
    than a branch or a mvn (an early return's value)."""
    regs, stack, out, pool = {13: "sp"}, None, [], set()
    code = _read(data, loads, func, size)
    for i in range(0, size, 4):
        op = struct.unpack_from("<I", code, i)[0]
        pc = func + i
        if pc in pool:  # a literal of an ldr above, not code
            continue
        if op & 0x0FF00000 == 0x03500000:  # cmp rn, #imm
            rn = regs.get(op >> 16 & 0xF)
            low = -rn[2] if isinstance(rn, tuple) and rn[0] == "rel" else 0
            out.append(("cmp", {"imm": _rotated(op), "low": low}, stack))
            continue
        if op >> 28 != 0xE:  # conditional: a branch to a shared tail, or an early return's value
            if op & 0x0FF00000 == 0x03E00000:  # mvn<cond> rd, #imm
                regs.pop(op >> 12 & 0xF, None)
            elif op & 0x0F000000 != 0x0A000000:
                raise Error(f"0x{pc:x}: a conditional instruction other than a branch")
            continue
        rd = op >> 12 & 0xF
        if rd == 13 and op & 0x0C000000 == 0:
            continue  # sp's own adjustments: "sp" is the stack pointer of the moment
        if op & 0x0F000000 == 0x0B000000:  # bl
            off = op & 0xFFFFFF
            off = (off - (1 << 24) if off & 0x800000 else off) * 4
            out.append((pc + 8 + off, dict(regs), stack))
            regs = {k: v for k, v in regs.items() if 4 <= k <= 11 or k == 13}  # AAPCS
        elif op & 0x0FF00000 == 0x03A00000:  # mov rd, #imm
            regs[rd] = _rotated(op)
        elif op & 0x0FF00000 == 0x03E00000:  # mvn rd, #imm
            regs[rd] = ~_rotated(op) & 0xFFFFFFFF
        elif op & 0x0FF00000 == 0x03000000:  # movw
            regs[rd] = (op >> 4 & 0xF000) | (op & 0xFFF)
        elif op & 0x0FF00000 == 0x03400000:  # movt
            low = regs.get(rd, 0)
            regs[rd] = ((op >> 4 & 0xF000) | (op & 0xFFF)) << 16 | (
                low & 0xFFFF if isinstance(low, int) else 0
            )
        elif op & 0x0FE00000 in (0x02800000, 0x02400000):  # add/sub rd, rn, #imm
            rn, imm = op >> 16 & 0xF, _rotated(op)
            base = regs.get(rn)
            sign = 1 if op & 0x0FE00000 == 0x02800000 else -1
            if isinstance(base, int):
                regs[rd] = (base + sign * imm) & 0xFFFFFFFF
            elif base == "sp" or (isinstance(base, tuple) and base[0] == "sp"):
                regs[rd] = ("sp", (0 if base == "sp" else base[1]) + sign * imm)
            elif base is None and not op & (1 << 20):  # (not adds/subs) of an unknown value
                regs[rd] = ("rel", rn, sign * imm)
            else:
                regs.pop(rd, None)
        elif op & 0x0FE00FF0 == 0x00800000 and isinstance(regs.get(op >> 16 & 0xF), int):
            rn, rm = op >> 16 & 0xF, op & 0xF  # add rd, rn, rm
            if isinstance(regs.get(rm), int):
                regs[rd] = (regs[rn] + regs[rm]) & 0xFFFFFFFF
            else:
                regs.pop(rd, None)
        elif op & 0x0FFF0FFF == 0x058D0000:  # str rd, [sp]
            stack = regs.get(rd)
        elif op & 0x0FFF0000 == 0x01A00000:  # mov rd, rm
            if (op & 0xFF0) == 0 and (op & 0xF) in regs:
                regs[rd] = regs[op & 0xF]
            else:
                regs.pop(rd, None)
        elif op & 0x0F7F0000 == 0x051F0000:  # ldr rd, [pc, #+-imm]: a literal
            at = pc + 8 + (op & 0xFFF if op & (1 << 23) else -(op & 0xFFF))
            regs[rd] = struct.unpack("<I", _read(data, loads, at, 4))[0]
            if func <= at < func + size:
                pool.add(at)
        elif op & 0x0C000000 == 0x04000000 and op & (1 << 20):  # ldr rd, [...]
            regs.pop(rd, None)
        elif rd < 5 and op & 0x0C000000 == 0x00000000 and op & 0x01900000 != 0x01100000:
            regs.pop(rd, None)  # another data processing write to rd (not tst/teq/cmp/cmn)
    return out


def fields(data):
    """{"marker": (offset, bytes), "preserve": [(offset, bytes)...], "debug": (offset, release)}
    out of RELEASE's bytes data. Error when the code is not as described above."""
    funcs, loads = _elf(data)
    read, write = funcs[READ][0], funcs[WRITE][0]

    def nvm_calls(name):
        return [
            (t, r, s) for t, r, s in calls(data, loads, *funcs[name]) if t in (read, write, "cmp")
        ]

    # the marker: DDI_NVM_Write(offset, 4, sp) with the word just stored at [sp]
    c = [(r, s) for t, r, s in nvm_calls(FINISH) if t == write]
    if len(c) != 1 or c[0][0].get(1) != 4 or c[0][0].get(2) != "sp" or not isinstance(c[0][1], int):
        raise Error(f"{FINISH}: not one DDI_NVM_Write(offset, 4, &word)")
    marker = (c[0][0][0], struct.pack("<I", c[0][1]))
    # the preserve DB: DDI_NVM_Read(mark, 4, sp), then the writes of the defaults table
    c = nvm_calls(PRESERVE)
    writes = [r for t, r, _ in c if t == write]
    if not c or c[0][0] != read or c[0][1].get(1) != 4 or not writes:
        raise Error(f"{PRESERVE}: not a read of its mark and writes of its defaults")
    preserve = []
    for r in writes:
        if not all(isinstance(r.get(k), int) for k in (0, 1, 2)) or r[1] > 16:
            raise Error(f"{PRESERVE}: a write whose offset, length or source is not a constant")
        preserve.append((r[0], bytes(_read(data, loads, r[2], r[1]))))
    if not any(o == c[0][1].get(0) for o, _ in preserve):
        raise Error(f"{PRESERVE}: its mark at 0x{c[0][1].get(0, 0):x} is not written")
    # the debug status: DDI_NVM_Read(offset, 1, ...), then cmp r0, #RELEASE
    c = nvm_calls(DEBUG)
    if len(c) < 2 or c[0][0] != read or c[0][1].get(1) != 1 or c[1][0] != "cmp":
        raise Error(f"{DEBUG}: not a 1-byte DDI_NVM_Read and a compare with its highest value")
    debug = (c[0][1][0], c[1][1]["low"] + c[1][1]["imm"])
    return {"marker": marker, "preserve": preserve, "debug": debug}


def image(data, size, debug_status):
    """A new chip of size bytes (0xFF) with the factory fields of RELEASE's bytes data and the
    debug status number debug_status (0 DEBUG .. the release's highest, RELEASE)."""
    f = fields(data)
    out = bytearray(b"\xff" * size)
    for offset, value in [f["marker"], *f["preserve"]]:
        out[offset : offset + len(value)] = value
    offset, release = f["debug"]
    if not 0 <= debug_status <= release:
        raise Error(f"debug status {debug_status}: 0 to {release}")
    out[offset] = debug_status
    return bytes(out)
