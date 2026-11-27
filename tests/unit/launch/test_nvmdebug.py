# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The webOS 4.x debug status block (nvmdebug.py) and its sync (nvm.sync_debug_block).

A made-up ELF file holds DIL_NVM_Load_DebugStatus's reads, decrypt call, fold and index as W18R's
05.50.70 and W19P's 05.40.97 tvservice have them, and W18H's 05.50.70 copy without a decrypt,
instruction for instruction (Thumb-2), with none of the rest; variants whose code differs must be refused."""

import ctypes
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from beanjar.board import nvm, nvmdebug
from beanjar.firmware import targets

TOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))

BASE = 0x10000
READ, DECRYPT, LOAD = BASE, BASE + 0x10, BASE + 0x100


def bl(pc, target):
    """Thumb-2 BL from pc to target, as two halfwords."""
    off = target - (pc + 4)
    s = off >> 24 & 1
    i1, i2 = off >> 23 & 1, off >> 22 & 1
    j1, j2 = 1 - i1 ^ s, 1 - i2 ^ s
    return [0xF000 | s << 10 | off >> 12 & 0x3FF, 0xD000 | j1 << 13 | j2 << 11 | off >> 1 & 0x7FF]


def load(size=32, index_at=87, plain=False, mask=True, fold=True):
    """DIL_NVM_Load_DebugStatus's halfwords: the block read into sp+88, the decrypt into sp+56,
    the plain read (W19P) and the fold and index over sp+56 (index_at: sp+56+31 on both builds)."""
    out = []

    def add(*hws):
        out.extend(hws)

    def call(target):
        out.extend(bl(LOAD + 2 * len(out), target))

    add(0xAA16, 0x2100 | size)  # add r2, sp, #88; movs r1, #size
    add(0xF44F, 0x70E3)  # mov.w r0, #0x1c6
    call(READ)
    add(0x2800, 0xD162)  # cmp r0, #0; bne
    add(0xA90E, 0xA816)  # add r1, sp, #56; add r0, sp, #88
    call(DECRYPT)
    if plain:
        add(
            0xF10D, 0x023E, 0x2101, 0xF44F, 0x70D4
        )  # add.w r2, sp, #62; movs r1, #1; mov.w r0, #424
        call(READ)
    add(0xEDDD, 0x0B12, 0xEDDD, 0x1B14)  # vldr d16, [sp, #72]; vldr d17, [sp, #80]
    add(0xF241, 0x0085, 0xF6C0, 0x0042)  # movw r0, #0x1085; movt r0, #0x842
    add(0xEDDD, 0x2B0E, 0xEDDD, 0x3B10)  # vldr d18, [sp, #56]; vldr d19, [sp, #64]
    if fold:
        add(0xFF40, 0x01F2)  # veor q8, q8, q9
    add(0xEDCD, 0x0B12, 0xEDCD, 0x1B14)  # vstr d16, d17
    add(0xF89D, 0x3000 | index_at)  # ldrb.w r3, [sp, #index_at]
    add(0xFBA3, 0x0100, 0x1A5A, 0xEB01, 0x0252, 0x0912, 0xEBC2, 0x1242)  # umull ... rsb
    if plain and mask:
        add(0xF003, 0x030F)  # and.w r3, r3, #15
    add(0x4770)  # bx lr
    return out


def load_copy(copy=True, call_between=False):
    """W18H's Load (05.50.70): the block read into sp+88 and copied, without a cipher, to sp+56
    (add r4, sp, #88; add.w lr, sp, #56; ldmia/stmia), the fold and index over the copy in ip and
    r4 rather than r0 to r3."""
    out = []

    def add(*hws):
        out.extend(hws)

    add(0xAA16, 0x2120)  # add r2, sp, #88; movs r1, #32
    add(0xF44F, 0x70E3)  # mov.w r0, #0x1c6
    out.extend(bl(LOAD + 2 * len(out), READ))
    add(0x4605, 0x2800, 0xD13C)  # mov r5, r0; cmp r0, #0; bne
    if call_between:
        out.extend(bl(LOAD + 2 * len(out), DECRYPT))
    if copy:
        add(0xAC16)  # add r4, sp, #88
    add(0xF10D, 0x0E38, 0xCC0F)  # add.w lr, sp, #56; ldmia r4!, {r0-r3}
    add(0xF241, 0x0C85, 0xF6C0, 0x0C42)  # movw ip, #0x1085; movt ip, #0x842
    add(0xE8AE, 0x000F)  # stmia.w lr!, {r0-r3}
    add(0xEDDD, 0x2B0E, 0xEDDD, 0x3B10)  # vldr d18, [sp, #56]; vldr d19, [sp, #64]
    add(0xE894, 0x000F, 0xE88E, 0x000F)  # ldmia.w r4; stmia.w lr
    add(0xAA1E)  # add r2, sp, #120
    add(0xEDDD, 0x0B12, 0xEDDD, 0x1B14)  # vldr d16, [sp, #72]; vldr d17, [sp, #80]
    add(0xFF40, 0x01F2)  # veor q8, q8, q9
    add(0xEDCD, 0x0B12, 0xEDCD, 0x1B14)  # vstr d16, d17
    add(0xF89D, 0x4057)  # ldrb.w r4, [sp, #87]
    add(0xFBA4, 0x010C, 0x1A63, 0xEB01, 0x0353, 0x091B, 0xEBC3, 0x1343)  # umull ... rsb
    add(0x4770)  # bx lr
    return out


def tvservice(code):
    """The made-up tvservice: one loadable segment from BASE, a symbol table of the functions."""
    funcs = {
        "DIL_NVM_Read": (READ, [0x4770]),
        "DIL_NVM_Load_DebugStatus": (LOAD, code),
    }
    image = bytearray(0x100 + 2 * len(code))
    for addr, hws in funcs.values():
        image[addr - BASE : addr - BASE + 2 * len(hws)] = struct.pack(f"<{len(hws)}H", *hws)
    names = b"\0" + b"".join(n.encode() + b"\0" for n in funcs)
    syms, at = [bytes(16)], 1
    for n, (addr, hws) in funcs.items():
        syms.append(struct.pack("<IIIBBH", at, addr | 1, 2 * len(hws), 0x12, 0, 1))  # Thumb
        at += len(n) + 1
    symtab = b"".join(syms)
    ehsize, phentsize, shentsize = 52, 32, 40
    code_off = ehsize + phentsize
    str_off = code_off + len(image)
    sym_off = str_off + len(names)
    sh_off = sym_off + len(symtab)
    sections = [
        bytes(shentsize),
        struct.pack("<IIIIIIIIII", 0, 1, 6, BASE, code_off, len(image), 0, 0, 4, 0),
        struct.pack("<IIIIIIIIII", 0, 3, 0, 0, str_off, len(names), 0, 0, 1, 0),
        struct.pack("<IIIIIIIIII", 0, 2, 0, 0, sym_off, len(symtab), 2, 1, 4, 16),
    ]
    header = b"\x7fELF\x01\x01\x01" + bytes(9)
    header += struct.pack(
        "<HHIIIIIHHHHHH", 2, 40, 1, 0, ehsize, sh_off, 0x5000000, ehsize, phentsize, 1,
        shentsize, len(sections), 0,
    )  # fmt: skip
    phdr = struct.pack("<IIIIIIII", 1, code_off, BASE, BASE, len(image), len(image), 7, 4)
    return header + phdr + bytes(image) + names + symtab + b"".join(sections)


W18R = ("m3", "goldilocks")
W19P = ("k5lp", "goldilocks2")


def accepted(block, plain):
    """The status W19P's Load keeps (None: its RELEASE fallback): the decoded block's, when the
    plain byte is 0xFF, the status is 5, or the byte's low nibble is the status."""
    status = nvmdebug.decode(block)
    return status if plain == 0xFF or status == 5 or plain & 0x0F == status else None


class EncodingTest(unittest.TestCase):
    def test_round_trip(self):
        """Every status reads back as itself, whatever the random bytes."""
        for seed in range(256):
            for status in (3, 4, 5):
                block = nvmdebug.encode(
                    status, lambda n, s=seed: bytes((s + 7 * i) & 0xFF for i in range(n))
                )
                self.assertEqual(len(block), 32)
                self.assertEqual(nvmdebug.decode(block), status)
        for status in (3, 4, 5):
            self.assertEqual(nvmdebug.decode(nvmdebug.encode(status)), status)

    def test_layout(self):
        """Store's order: the status at R[31] mod 31 of the random bytes, then the fold."""
        r = bytes(range(100, 132))  # R[31] = 131, 131 mod 31 = 7
        placed = bytearray(r)
        placed[7] = 4
        want = bytes(placed[:16]) + bytes(a ^ b for a, b in zip(placed[16:], placed[:16]))
        self.assertEqual(nvmdebug.encode(4, lambda n: r), want)
        self.assertEqual(nvmdebug.fold(nvmdebug.fold(want)), want)

    def test_blank(self):
        """A blank chip's block reads as no valid status (0xFF), which tvservice reads as 5."""
        self.assertNotIn(nvmdebug.decode(b"\xff" * 32), nvmdebug.STATUSES)
        with self.assertRaises(nvmdebug.Error):
            nvmdebug.encode(2)
        with self.assertRaises(nvmdebug.Error):
            nvmdebug.decode(b"\0" * 16)


class FieldsTest(unittest.TestCase):
    def test_fields(self):
        self.assertEqual(nvmdebug.fields(tvservice(load())), {"offset": 0x1C6, "plain": None})
        self.assertEqual(
            nvmdebug.fields(tvservice(load(plain=True))), {"offset": 0x1C6, "plain": 0x1A8}
        )
        # W18H: no cipher, the fold over a copy of the block read, in other registers
        self.assertEqual(nvmdebug.fields(tvservice(load_copy())), {"offset": 0x1C6, "plain": None})

    def test_refused(self):
        """A build whose Load reads, folds or indexes otherwise is refused."""
        for code in (
            load(size=16),  # another block length
            load(index_at=86),  # the index from byte 30
            load(fold=False),  # no fold
            load(plain=True, mask=False),  # the plain byte compared otherwise
            load_copy(copy=False),  # W18H's fold over a buffer the block was not copied from
            load_copy(call_between=True),  # a call between its read and its fold
        ):
            with self.assertRaises(nvmdebug.Error):
                nvmdebug.fields(tvservice(code))
        with self.assertRaises(nvmdebug.Error):
            nvmdebug.fields(b"not an ELF file")

    def test_release(self):
        """W18R and W19P keep the block at 0x1C6; W19P's plain copy at 0x1A8 is its plain byte."""
        w18r, w19p = targets.release(W18R).nvm, targets.release(W19P).nvm
        self.assertEqual((w18r.debug_block_offset, w18r.debug_status_offset), (0x1C6, None))
        self.assertEqual((w19p.debug_block_offset, w19p.debug_status_offset), (0x1C6, 0x1A8))
        self.assertIsNone(targets.release(("h13", "afro")).nvm.debug_block_offset)
        nvm.check_debug_block(tvservice(load()), w18r)
        nvm.check_debug_block(tvservice(load(plain=True)), w19p)
        for data, n in ((tvservice(load(plain=True)), w18r), (tvservice(load()), w19p)):
            with self.assertRaisesRegex(nvm.Error, "not the release's"):
                nvm.check_debug_block(data, n)
        with self.assertRaisesRegex(nvm.Error, "debug status block"):
            nvm.check_debug_block(tvservice(load(fold=False)), w18r)


class SyncTest(unittest.TestCase):
    def test_sync(self):
        """Every start brings the block (and W19P's plain byte) to the effective mode, once."""
        say = lambda _: None  # noqa: E731
        for target in (W18R, W19P):
            n = targets.release(target).nvm
            chip = bytearray(b"\xff" * nvm.SIZE)
            for mode in (3, 4, 5, 3):
                self.assertTrue(nvm.sync_debug_block(chip, n, mode, say))
                block = bytes(chip[0x1C6 : 0x1C6 + 32])
                self.assertEqual(nvmdebug.decode(block), mode)
                self.assertFalse(nvm.sync_debug_block(chip, n, mode, say))
                self.assertEqual(bytes(chip[0x1C6 : 0x1C6 + 32]), block, "not made again")
                nvm.sync_debug_status(chip, n, mode, say)
                if target == W19P:
                    self.assertEqual(chip[0x1A8], mode)
                    self.assertEqual(accepted(block, chip[0x1A8]), mode)
            outside = set(range(0x1C6, 0x1C6 + 32)) | ({0x1A8} if target == W19P else set())
            self.assertTrue(all(b == 0xFF for i, b in enumerate(chip) if i not in outside))
        # W19P: a plain byte left at another status is refused by its tvservice
        self.assertIsNone(accepted(nvmdebug.encode(3), 4))
        self.assertEqual(accepted(nvmdebug.encode(3), 0xFF), 3)
        # a release without the block: nothing
        blank = bytearray(nvm.SIZE)
        self.assertFalse(nvm.sync_debug_block(blank, targets.release(("h13", "afro")).nvm, 3))
        self.assertEqual(blank, bytearray(nvm.SIZE))


class CipherTest(unittest.TestCase):
    """The soc layer's debug data cipher (dbg-data.c), built for the host: the identity, so the
    block beanjar writes is what tvservice decodes, and what tvservice's Store writes reads back."""

    def test_identity(self):
        cc = shutil.which("cc")
        if not cc:
            self.skipTest("no host C compiler")
        src = os.path.join(TOP, "guest/layers/soc/src/libdile_crypto/dbg-data.c")
        with tempfile.TemporaryDirectory(prefix="beanjar-dbgdata-") as top:
            lib = os.path.join(top, "dbg-data.so")
            subprocess.run([cc, "-shared", "-fPIC", "-O2", src, "-o", lib], check=True)
            dll = ctypes.CDLL(lib)
            for name in ("DILE_CRYPTO_DecryptDbgData", "DILE_CRYPTO_EncryptDbgData"):
                call = getattr(dll, name)
                call.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
                call.restype = ctypes.c_int
                for status in (3, 4, 5):
                    block = nvmdebug.encode(status)
                    out = ctypes.create_string_buffer(32)
                    self.assertEqual(call(block, out), 0)
                    self.assertEqual(out.raw, block)
                    self.assertEqual(nvmdebug.decode(out.raw), status)
                self.assertEqual(call(None, None), -1)


if __name__ == "__main__":
    unittest.main()
