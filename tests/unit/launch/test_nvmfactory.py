# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The EEPROM fields a NetCast 4.5 factory leaves, read out of RELEASE's code (nvmfactory.py), and
the release's debug status values (nvm.sync_debug_status).

A made-up ELF file holds the three writers as LM14's 04.11.22 RELEASE has them, instruction for
instruction (DDI_NVM_FinishFirstBootUp, DDI_NVM_InitPreserveDB, OSA_MD_GetDebugStatus), with its
defaults table; variants whose code differs must be refused."""

import struct
import unittest

from beanjar import nvm, nvmfactory, targets

BASE = 0x10000
READ, WRITE, FINISH, PRESERVE, DEBUG = (BASE, BASE + 4, BASE + 0x10, BASE + 0x100, BASE + 0x200)
TABLE = BASE + 0x400  # the preserve DB's defaults (RELEASE: r4 + 0x5c0)
R4 = TABLE - 0x5C0


def movw(rd, imm):
    return 0xE3000000 | (imm >> 12) << 16 | rd << 12 | imm & 0xFFF


def movt(rd, imm):
    return 0xE3400000 | (imm >> 12) << 16 | rd << 12 | imm & 0xFFF


def bl(pc, target):
    return 0xEB000000 | ((target - pc - 8) >> 2) & 0xFFFFFF


def finish(marker=0x20100118, length=4):
    w = [
        movw(3, 0xEF90),
        0xE52DE004,  # push {lr}
        movt(3, 0x192),
        0xE3A02F46,  # mov r2, #0x118
        0xE24DD00C,  # sub sp, sp, 12
        movt(2, marker >> 16),
        0xE5933008,  # ldr r3, [r3, #8]
        0xE58D2000,  # str r2, [sp]
        0xE3530000,  # cmp r3, #0
        0x0A000006,  # beq
        0xE3A00010,  # mov r0, #16
        0xE3A01000 | length,  # mov r1, #length
        0xE1A0200D,  # mov r2, sp
        None,  # bl DDI_NVM_Write
        0xE3A00000,
        0xE28DD00C,
        0xE8BD8000,
        0xE3E00000,  # mvn r0, #0
        0xEAFFFFFB,
    ]
    assert marker & 0xFFFF == 0x118
    return [bl(FINISH + 4 * i, WRITE) if x is None else x for i, x in enumerate(w)]


def preserve():
    w = [
        0xE92D4010,
        movw(4, R4 & 0xFFFF),
        0xE24DD008,
        movt(4, R4 >> 16),
        0xE1A0200D,  # mov r2, sp
        movw(0, 0x734),
        0xE3A01004,
        ("bl", READ),
        0xE59425C4,  # ldr r2, [r4, #0x5c4]
        0xE59D3000,  # ldr r3, [sp]
        0xE1520003,  # cmp r2, r3
        0xE2842D17,  # add r2, r4, #0x5c0
        0x0A000019,  # beq
        0xE3A01001,
        0xE2822008,  # add r2, r2, #8
        movw(0, 0x738),
        ("bl", WRITE),
        0xE2842D17,
        0xE3A01001,
        0xE2822009,
        movw(0, 0x739),
        ("bl", WRITE),
        0xE2842D17,
        0xE3A01004,
        0xE282200C,
        movw(0, 0x73C),
        ("bl", WRITE),
        0xE2842E5D,  # add r2, r4, #0x5d0
        0xE3A01001,
        0xE3A00D1D,  # mov r0, #0x740
        ("bl", WRITE),
        0xE2842D17,
        0xE3A01004,
        0xE0822001,  # add r2, r2, r1
        movw(0, 0x734),
        ("bl", WRITE),
        0xE3A00000,
        0xE28DD008,
        0xE8BD8010,
    ]
    return [bl(PRESERVE + 4 * i, x[1]) if isinstance(x, tuple) else x for i, x in enumerate(w)]


def debug(highest=2):
    w = [
        0xE92D4010,
        movw(4, 0xB9F0),
        movt(4, 0x137),
        movw(0, 0x18A),
        0xE3A01001,
        0xE2842E16,  # add r2, r4, #0x160
        ("bl", READ),
        0xE5940160,  # ldr r0, [r4, #0x160]
        0xE3500000 | highest,  # cmp r0, #highest
        0x9A000006,  # bls
        0xE8BD8010,
    ]
    return [bl(DEBUG + 4 * i, x[1]) if isinstance(x, tuple) else x for i, x in enumerate(w)]


def ldr_pc(rd, pc, at):
    """ldr rd, [pc, #at - pc - 8]"""
    off = at - pc - 8
    return (0xE59F0000 if off >= 0 else 0xE51F0000) | rd << 12 | abs(off)


def finish_nc4():
    """NetCast 4's (H13 04.28.15): the state's address from a literal, a conditional mvn."""
    w = [
        ("ldr", 2, 0x44),
        0xE3A03F46,  # mov r3, #0x118
        0xE52DE004,  # push {lr}
        movt(3, 0x2010),
        0xE24DD00C,  # sub sp, sp, #12
        0xE58D3000,  # str r3, [sp]
        0xE5923008,  # ldr r3, [r2, #8]
        0xE3530000,  # cmp r3, #0
        0x03E00000,  # mvneq r0, #0
        0x0A000004,  # beq
        0xE3A00010,  # mov r0, #16
        0xE3A01004,  # mov r1, #4
        0xE1A0200D,  # mov r2, sp
        ("bl", WRITE),
        0xE3A00000,
        0xE28DD00C,
        0xE8BD8000,  # ldmfd sp!, {pc}
        0x019CEE38,  # the literal (orrseq as an instruction)
    ]
    return _place(FINISH, w)


def preserve_nc4():
    """NetCast 4's: r4 from a literal, the mark read into sp + 4, a literal pool at the end."""
    w = [
        0xE92D4010,
        0xE3A01004,  # mov r1, #4
        ("ldr", 4, 0xBC),
        0xE24DD008,
        0xE08D2001,  # add r2, sp, r1
        movw(0, 0x734),
        ("bl", READ),
        0xE59425C4,  # ldr r2, [r4, #0x5c4]
        0xE59D3004,  # ldr r3, [sp, #4]
        0xE1520003,  # cmp r2, r3
        0xE2842D17,  # add r2, r4, #0x5c0
        0x0A000019,  # beq
        0xE2822008,
        0xE3A01001,
        movw(0, 0x738),
        ("bl", WRITE),
        0xE2842D17,
        0xE3A01001,
        0xE2822009,
        movw(0, 0x739),
        ("bl", WRITE),
        0xE2842D17,
        0xE3A01004,
        0xE282200C,
        movw(0, 0x73C),
        ("bl", WRITE),
        0xE3A01001,
        0xE2842E5D,  # add r2, r4, #0x5d0
        0xE3A00D1D,  # mov r0, #0x740
        ("bl", WRITE),
        0xE2842D17,
        movw(0, 0x734),
        0xE2822004,  # add r2, r2, #4
        0xE3A01004,
        ("bl", WRITE),
        0xE3A00000,
        0xE28DD008,
        0xE8BD8010,
    ]
    w += [0xE1A00000] * (0xBC // 4 - len(w)) + [R4]  # the pool: r4's literal
    return _place(PRESERVE, w)


def debug_nc4(low=3, n=2):
    """NetCast 4's: the range checked as sub r3, r0, #low; cmp r3, #n."""
    w = [
        0xE92D4010,
        0xE24DD008,
        movw(0, 0x18A),
        0xE28D2007,  # add r2, sp, #7
        0xE3A01001,
        ("bl", READ),
        0xE5DD0007,  # ldrb r0, [sp, #7]
        ("ldr", 4, 0x5C),
        0xE2403000 | low,  # sub r3, r0, #low
        0xE3530000 | n,  # cmp r3, #n
        0xE5840160,  # str r0, [r4, #0x160]
        0x9A000007,  # bls
        0xE8BD8010,
    ]
    w += [0xE1A00000] * (0x5C // 4 - len(w)) + [0x0137B9F0]
    return _place(DEBUG, w)


def _place(func, w):
    out = []
    for i, x in enumerate(w):
        pc = func + 4 * i
        if isinstance(x, tuple) and x[0] == "bl":
            out.append(bl(pc, x[1]))
        elif isinstance(x, tuple):
            out.append(ldr_pc(x[1], pc, func + x[2]))
        else:
            out.append(x)
    return out


def release(funcs=None, table=None):
    """The made-up RELEASE: one loadable segment from BASE with the code and the table, and a
    symbol table of the functions."""
    funcs = funcs or {
        "DDI_NVM_Read": (READ, [0xE12FFF1E]),
        "DDI_NVM_Write": (WRITE, [0xE12FFF1E]),
        "DDI_NVM_FinishFirstBootUp": (FINISH, finish()),
        "DDI_NVM_InitPreserveDB": (PRESERVE, preserve()),
        "OSA_MD_GetDebugStatus": (DEBUG, debug()),
    }
    image = bytearray(0x500)
    for addr, words in funcs.values():
        image[addr - BASE : addr - BASE + 4 * len(words)] = struct.pack(f"<{len(words)}I", *words)
    table = table or struct.pack("<IIBBHIB", 0xAAAAAAAA, 7, 0, 0, 0, 7, 0)
    image[TABLE - BASE : TABLE - BASE + len(table)] = table
    names = b"\0" + b"".join(n.encode() + b"\0" for n in funcs)
    syms, at = [bytes(16)], 1
    for n, (addr, words) in funcs.items():
        syms.append(struct.pack("<IIIBBH", at, addr, 4 * len(words), 0x12, 0, 1))  # GLOBAL FUNC
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


class FactoryTest(unittest.TestCase):
    def test_fields(self):
        f = nvmfactory.fields(release())
        self.assertEqual(f["marker"], (0x10, bytes.fromhex("18011020")))
        self.assertEqual(
            f["preserve"],
            [
                (0x738, b"\0"),
                (0x739, b"\0"),
                (0x73C, struct.pack("<I", 7)),
                (0x740, b"\0"),
                (0x734, struct.pack("<I", 7)),
            ],
        )
        self.assertEqual(f["debug"], (0x18A, 2))

    def test_image(self):
        image = nvmfactory.image(release(), nvm.SIZE, 0)
        self.assertEqual(len(image), nvm.SIZE)
        self.assertEqual(image[0x10:0x14], bytes.fromhex("18011020"))
        self.assertEqual(
            image[0x734:0x741], bytes.fromhex("0700000000 00 ffff 07000000 00".replace(" ", ""))
        )
        self.assertEqual(image[0x18A], 0)
        written = (
            {0x18A} | set(range(0x10, 0x14)) | set(range(0x734, 0x73A)) | set(range(0x73C, 0x741))
        )
        self.assertTrue(all(b == 0xFF for i, b in enumerate(image) if i not in written))
        with self.assertRaises(nvmfactory.Error):
            nvmfactory.image(release(), nvm.SIZE, 3)

    def test_refused(self):
        """A build whose writers are missing or differ is refused, not given other bytes."""
        good = {
            "DDI_NVM_Read": (READ, [0xE12FFF1E]),
            "DDI_NVM_Write": (WRITE, [0xE12FFF1E]),
            "DDI_NVM_FinishFirstBootUp": (FINISH, finish()),
            "DDI_NVM_InitPreserveDB": (PRESERVE, preserve()),
            "OSA_MD_GetDebugStatus": (DEBUG, debug()),
        }
        missing = dict(good)
        del missing["DDI_NVM_InitPreserveDB"]
        two_bytes = dict(good, DDI_NVM_FinishFirstBootUp=(FINISH, finish(length=2)))
        no_compare = dict(good, OSA_MD_GetDebugStatus=(DEBUG, debug()[:8] + [0xE8BD8010]))
        for funcs in (missing, two_bytes, no_compare):
            with self.assertRaises(nvmfactory.Error):
                nvmfactory.fields(release(funcs))
        self.assertEqual(
            nvmfactory.fields(release(dict(good, OSA_MD_GetDebugStatus=(DEBUG, debug(3)))))[
                "debug"
            ],
            (0x18A, 3),
        )

    def test_netcast4(self):
        """NetCast 4's writers (H13): literals, a conditional mvn and a range from 3 decode to the
        same fields, the debug status up to 5."""
        funcs = {
            "DDI_NVM_Read": (READ, [0xE12FFF1E]),
            "DDI_NVM_Write": (WRITE, [0xE12FFF1E]),
            "DDI_NVM_FinishFirstBootUp": (FINISH, finish_nc4()),
            "DDI_NVM_InitPreserveDB": (PRESERVE, preserve_nc4()),
            "OSA_MD_GetDebugStatus": (DEBUG, debug_nc4()),
        }
        f = nvmfactory.fields(release(funcs))
        self.assertEqual(f["marker"], (0x10, bytes.fromhex("18011020")))
        self.assertEqual([o for o, _ in f["preserve"]], [0x738, 0x739, 0x73C, 0x740, 0x734])
        self.assertEqual(f["preserve"][-1][1], struct.pack("<I", 7))
        self.assertEqual(f["debug"], (0x18A, 5))
        n = targets.release(("h13", "netcast4")).nvm
        self.assertEqual(
            (n.factory, n.debug_status_offset, n.debug_status_values),
            (("lgap", "/RELEASE"), 0x18A, (3, 4, 5)),
        )
        image = nvmfactory.image(release(funcs), nvm.SIZE, 3)
        self.assertEqual((image[0x18A], image[0x739]), (3, 0))
        # another conditional instruction is still refused
        bad = list(funcs["DDI_NVM_FinishFirstBootUp"][1])
        bad[8] = 0x03A00001  # moveq r0, #1
        with self.assertRaises(nvmfactory.Error):
            nvmfactory.fields(release(dict(funcs, DDI_NVM_FinishFirstBootUp=(FINISH, bad))))

    def test_release(self):
        """NetCast 4.5 keeps the debug status at 0x18A as 0 DEBUG, 1 EVENT, 2 RELEASE; every start
        brings a programmed chip to the effective mode (DEBUG_MODES' 3, 4, 5)."""
        n = targets.release(("lm14", "netcast45")).nvm
        self.assertEqual(
            (n.factory, n.debug_status_offset, n.debug_status_values),
            (("lgap", "/RELEASE"), 0x18A, (0, 1, 2)),
        )
        chip = bytearray(b"\xff" * nvm.SIZE)
        for mode, value in ((3, 0), (4, 1), (5, 2), (3, 0)):
            self.assertTrue(nvm.sync_debug_status(chip, n, mode, lambda _: None))
            self.assertEqual(chip[0x18A], value)
            self.assertFalse(nvm.sync_debug_status(chip, n, mode, lambda _: None))


if __name__ == "__main__":
    unittest.main()
