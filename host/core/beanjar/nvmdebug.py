# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The debug status of a webOS 4.x EEPROM (W18R, W19P): a block of 32 bytes that tvservice's
DIL_NVM_Load_DebugStatus reads (DIL_NVM_Read(offset, 32)) and decrypts with
DILE_CRYPTO_DecryptDbgData. On the emulator that cipher is the identity (the soc layer's
libdile_crypto shim, guest/layers/soc/src/libdile_crypto/dbg-data.c), so the EEPROM holds the
clear block, which this module makes in the firmware's own encoding (DIL_NVM_Store_DebugStatus):

  32 random bytes R (RAND_bytes), the status at R[R[31] mod 31], then the fold R[16..31] ^= R[0..15]

W18H's tvservice (05.50.70, LG's M16PP board) has no cipher there: its Load copies the block it
read and folds and indexes the copy, so its EEPROM holds the same clear block on the set too.

Load undoes it: the fold (an involution), then the status is B[B[31] mod 31]. The index is never
31, so B[31] survives the placement. Statuses are tvservice's 3 DEBUG, 4 EVENT, 5 RELEASE (the
debugMode= numbers, nvm.DEBUG_MODES); OSA_MD_GetDebugStatus reads any other value as 5.

W19P's Load also reads one plain byte (U-Boot's nDebugStatus, sysdb+0x194: 0x1A8) and keeps the
decrypted status only when that byte is 0xFF, the status is 5, or (byte & 0x0F) == status
(0x9f4e18-0x9f4e30 in 05.40.97's tvservice); otherwise it falls back to RELEASE. That byte is the
release's Nvm.debug_status_offset, synced as a plain byte (nvm.sync_debug_status).

fields() reads the layout out of the build's own tvservice: the function by its symbol, its
Thumb-2 code decoded just enough to find the reads' constant offsets and lengths, the buffer
DILE_CRYPTO_DecryptDbgData writes, and the fold and index code over that buffer. Error when the
code is not of that shape, so a build whose layout differs is refused rather than given a block
it would read as something else (as beanjar/nvmfactory.py does for NetCast)."""

import os
import struct

from . import nvmfactory

LOAD = "DIL_NVM_Load_DebugStatus"
READ = "DIL_NVM_Read"
SIZE = 32
HALF = 16
INDEX = 31  # the byte whose value mod 31 is the status's index
MODULUS = 31
STATUSES = (3, 4, 5)

# the fold and index of Load, in this order, after the decrypt (Thumb-2 halfword pairs, each as
# (mask1, value1, mask2, value2): the registers are the build's own): the reciprocal of 31
# (movw/movt rX, 0x08421085), veor q8, q8, q9 (the fold of the two halves), umull and
# rsb rd, rn, rn, lsl #5 (B[31] mod 31). W18R's and W19P's use r0 to r3, W18H's ip and r4.
FOLD_CODE = (
    (0xFFFF, 0xF241, 0xF0FF, 0x0085),
    (0xFFFF, 0xF6C0, 0xF0FF, 0x0042),
    (0xFFFF, 0xFF40, 0xFFFF, 0x01F2),
    (0xFFF0, 0xFBA0, 0x00F0, 0x0000),
    (0xFFF0, 0xEBC0, 0xF0F0, 0x1040),
)
VEOR, UMULL = 2, 3
# W19P's check of the plain byte: and.w r3, r3, #15
PLAIN_MASK_CODE = (0xF003, 0x030F)


class Error(Exception):
    pass


def encode(status, rand=os.urandom):
    """The 32-byte clear block of status (3, 4 or 5), as DIL_NVM_Store_DebugStatus makes it before
    it encrypts: rand(32) with the status at R[31] mod 31, then folded."""
    if status not in STATUSES:
        raise Error(f"debug status {status}: not one of {STATUSES}")
    r = bytearray(rand(SIZE))
    if len(r) != SIZE:
        raise Error("not 32 random bytes")
    r[r[INDEX] % MODULUS] = status
    return bytes(fold(r))


def fold(block):
    """block with its upper half XORed with its lower half (its own inverse)."""
    b = bytearray(block)
    for i in range(HALF):
        b[HALF + i] ^= b[i]
    return bytes(b)


def decode(block):
    """The status DIL_NVM_Load_DebugStatus reads out of the clear block (any byte value: the caller
    tells a valid one, STATUSES)."""
    if len(block) != SIZE:
        raise Error(f"a debug block is {SIZE} bytes, not {len(block)}")
    b = fold(block)
    return b[b[INDEX] % MODULUS]


# ---------------------------------------------------------------------------- the firmware's code
def _expand_imm(i, imm3, imm8):
    """Thumb-2's ThumbExpandImm of the 12 bits i:imm3:imm8."""
    imm12 = i << 11 | imm3 << 8 | imm8
    if imm12 >> 10 == 0:
        kind = imm12 >> 8 & 3
        return (imm8, imm8 << 16 | imm8, imm8 << 24 | imm8 << 8, imm8 * 0x01010101)[kind]
    value, rot = 0x80 | imm8 & 0x7F, imm12 >> 7
    return (value >> rot | value << (32 - rot)) & 0xFFFFFFFF


def _steps(code, base):
    """[(pc, hw1, hw2 or None)]: the Thumb instructions of code at address base."""
    out, i = [], 0
    while i + 2 <= len(code):
        hw1 = struct.unpack_from("<H", code, i)[0]
        if hw1 >> 11 in (0x1D, 0x1E, 0x1F) and i + 4 <= len(code):
            out.append((base + i, hw1, struct.unpack_from("<H", code, i + 2)[0]))
            i += 4
        else:
            out.append((base + i, hw1, None))
            i += 2
    return out


def _bl_target(pc, hw1, hw2):
    """The target of a BL or BLX at pc, None for any other instruction."""
    if hw1 >> 11 != 0x1E or hw2 & 0xC000 != 0xC000:
        return None
    s, j1, j2 = hw1 >> 10 & 1, hw2 >> 13 & 1, hw2 >> 11 & 1
    i1, i2 = 1 - (j1 ^ s), 1 - (j2 ^ s)
    off = s << 24 | i1 << 23 | i2 << 22 | (hw1 & 0x3FF) << 12 | (hw2 & 0x7FF) << 1
    if s:
        off -= 1 << 25
    if hw2 & 0x1000:  # BL
        return pc + 4 + off
    return (pc + 4 & ~3) + off  # BLX: to ARM code


def calls(steps):
    """[(pc, target, {reg: value})]: the BL/BLX calls in code order, with the argument registers
    r0-r3 known to hold a constant (an int) or an address on the stack (("sp", offset)) then. Only
    moves of a constant and adds to sp are followed; any other instruction but a store forgets
    r0-r3, so a value given is never wrong (one may be missing)."""
    regs, out = {}, []
    for pc, hw1, hw2 in steps:
        if hw2 is None:
            if hw1 & 0xF800 == 0x2000:  # movs rd, #imm8
                regs[hw1 >> 8 & 7] = hw1 & 0xFF
            elif hw1 & 0xF800 == 0xA800:  # add rd, sp, #imm8 * 4
                regs[hw1 >> 8 & 7] = ("sp", (hw1 & 0xFF) * 4)
            elif hw1 & 0xE000 == 0x6000 and not hw1 & 0x0800:  # str/strb rt, [rn, #imm]
                pass
            elif hw1 & 0xF000 in (0x8000, 0x9000) and not hw1 & 0x0800:  # strh, str sp-relative
                pass
            else:
                regs = {}
            continue
        target = _bl_target(pc, hw1, hw2)
        rd = hw2 >> 8 & 0xF
        imm = _expand_imm(hw1 >> 10 & 1, hw2 >> 12 & 7, hw2 & 0xFF)
        if target is not None:
            out.append((pc, target, dict(regs)))
            regs = {}
        elif hw1 & 0xFBEF == 0xF04F and not hw2 & 0x8000:  # mov.w rd, #const
            regs[rd] = imm
        elif hw1 & 0xFBFF == 0xF10D and not hw2 & 0x8000:  # add.w rd, sp, #const
            regs[rd] = ("sp", imm)
        elif hw1 & 0xFBF0 in (0xF240, 0xF2C0) and not hw2 & 0x8000:  # movw, movt rd, #imm16
            imm16 = (hw1 & 0xF) << 12 | (hw1 >> 10 & 1) << 11 | (hw2 >> 12 & 7) << 8 | hw2 & 0xFF
            low = regs.get(rd)
            if hw1 & 0xFBF0 == 0xF240:
                regs[rd] = imm16
            elif isinstance(low, int):
                regs[rd] = imm16 << 16 | low & 0xFFFF
            else:
                regs.pop(rd, None)
        elif hw1 & 0xFE10 == 0xF800:  # a store (strb.w, strh.w, str.w)
            pass
        else:
            regs = {}
    return out


def fields(data):
    """{"offset", "plain"} of tvservice's bytes data: the clear block's EEPROM offset (its length
    is SIZE) and the plain cross-check byte's offset (None where Load reads none). Error when the
    code is not as the module's docstring says."""
    try:
        funcs, loads = nvmfactory._elf(data, (LOAD, READ))
        func, size = funcs[LOAD]
        steps = _steps(nvmfactory._read(data, loads, func, size), func)
    except (nvmfactory.Error, struct.error, ValueError) as e:
        raise Error(str(e)) from None
    read = funcs[READ][0]
    c = calls(steps)
    reads = [(pc, r) for pc, t, r in c if t == read]
    if not reads or not isinstance(reads[0][1].get(0), int) or reads[0][1].get(1) != SIZE:
        raise Error(f"{LOAD}: its first {READ} is not of {SIZE} bytes at a constant offset")
    first_pc, first = reads[0]
    offset, buf = first[0], first.get(2)
    # the decrypt: the next call that takes the block read (r0) and gives another stack buffer (r1)
    decrypt = [
        (pc, r)
        for pc, t, r in c
        if pc > first_pc and t != read and r.get(0) == buf and isinstance(r.get(1), tuple)
    ]
    if not isinstance(buf, tuple):
        raise Error(f"{LOAD}: its {READ} is not into a stack buffer")
    # the fold and the index, after the decrypt (or, without one, after the read)
    start = decrypt[0][0] if decrypt else first_pc
    words = [(a, b) for pc, a, b in steps if pc > start and b is not None]
    marks, at = [], 0
    for m1, v1, m2, v2 in FOLD_CODE:
        found = next(
            (i for i in range(at, len(words)) if words[i][0] & m1 == v1 and words[i][1] & m2 == v2),
            None,
        )
        if found is None:
            raise Error(f"{LOAD}: no fold and index by {MODULUS} after the decrypt")
        marks.append(found)
        at = found + 1
    span = words[:at]
    vldr = {
        (b >> 12 & 0xF) + 16: (b & 0xFF) * 4 for a, b in span if a == 0xEDDD and b & 0xF00 == 0xB00
    }
    if decrypt:
        clear = decrypt[0][1][1][1]
    else:
        # W18H: no call between the read and the fold, the fold over a copy of the block (the
        # copy's source, the read buffer, is taken into a register: add rd, sp, #buf)
        veor_pc = [pc for pc, a, b in steps if pc > first_pc and b is not None][marks[VEOR]]
        if any(first_pc < pc < veor_pc for pc, _, _ in c) or 18 not in vldr:
            raise Error(f"{LOAD}: no call that takes the block it read into another buffer")
        if not any(
            first_pc < pc < veor_pc
            and b is None
            and a & 0xF800 == 0xA800
            and (a & 0xFF) * 4 == buf[1]
            for pc, a, b in steps
        ):
            raise Error(f"{LOAD}: its fold is not over a copy of the block it read")
        clear = vldr[18]
    halves = {16: clear + HALF, 17: clear + HALF + 8, 18: clear, 19: clear + 8}
    if any(vldr.get(d) != off for d, off in halves.items()):
        raise Error(f"{LOAD}: its fold is not of the clear block's two halves")
    # ldrb.w rt, [sp, #imm] between the fold (veor) and the division (umull)
    veor, umull = marks[VEOR], marks[UMULL]
    index_loads = [b & 0xFFF for a, b in words[veor:umull] if a == 0xF89D]
    if index_loads != [clear + INDEX]:
        raise Error(f"{LOAD}: its index is not the clear block's byte {INDEX}")
    # the plain byte: a 1-byte read at a constant offset, checked by its low nibble
    plain = {r[0] for _, r in reads[1:] if r.get(1) == 1 and isinstance(r.get(0), int)}
    if len(plain) > 1:
        raise Error(f"{LOAD}: reads more than one plain byte")
    if plain and PLAIN_MASK_CODE not in [(a, b) for _, a, b in steps if b is not None]:
        raise Error(f"{LOAD}: its plain byte is not compared by its low nibble")
    return {"offset": offset, "plain": plain.pop() if plain else None}
