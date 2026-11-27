#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The RPC answer table of a Realtek firmware, for rtk.ko (guest/kernel/modules/rtk).

KADP's RPC client stubs in libkadaptor (AUDIO_RPC_ToAgent_*_0, VIDEO_RPC_*_ToAgent_*_0 and the
other callers of client_call) each malloc the result buffer, pass a constant procedure number to
client_call, and XDR-decode the reply in place. rtk.ko answers every request with S_OK and needs
to know only the exceptions: result sizes other than 4 bytes (rpc_reply=P:N:BYTES), the Create
procedures that return {S_OK, id} (rpc_create=P:N), and the value-dependent answers
(rpc_answer=P:N:WORD:VALUE:RESULT). This script reads them out of the library.

    rpc-table.py FIRMWARE_DIR|LIBKADAPTOR [--cache] [--json FILE] [--name NAME]
    rpc-table.py --self-test [--firmwares DIR]

A firmware directory is unpacked (bsppart*.pak's bsp/usr/lib, or rootfs/usr/lib) into a temporary
directory, which is removed afterwards. Prints the module parameters on stdout, the counts on stderr.
--cache writes build/rtk/<name>/{rpc-table.json,params} (build/ is not tracked) and reuses them while
the library's sha256 is unchanged. Nothing firmware-derived goes into tracked files.

Needs python3-pyelftools and python3-capstone, except for a --cache hit. Exits non-zero on anything
ambiguous.
"""

import argparse
import bisect
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

# Imported when a library is analysed (analyse): a --cache hit needs neither, so a host with the
# cache copied from another runs without them.
capstone = carm = ELFFile = None


def _import_analysers():
    global capstone, carm, ELFFile
    try:
        import capstone as cs
        from capstone import arm
        from elftools.elf.elffile import ELFFile as elffile
    except ImportError as e:
        sys.exit(
            f"rpc-table: {e}: install python3-pyelftools and python3-capstone (or bring "
            "build/rtk/<firmware> from a host that has them)"
        )
    capstone, carm, ELFFile = cs, arm, elffile


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SELF_TEST = (
    "05.40.97.01-HE_DTV_W19P_AFADABAA",
    "04.63.25.01-HE_DTV_W20L_AFAAJAAA",
    "43.22.40.01-HE_DTV_W22H_AFABJAPU",
)
# what every one of them must give
SELF_TEST_PARAMS = ("rpc_create=100:10,201:1", "rpc_answer=201:16:0:113:0x10000003")

# rtk.ko's limits (guest/kernel/modules/rtk/rtk-rpc.c): the parameter arrays' sizes and RPC_MAX_RESULT
MAX_REPLY, MAX_CREATE, MAX_ANSWER, MAX_RESULT = 128, 16, 16, 4096

PROGRAMS = {
    "AUDIO": 201,
    "VIDEO": 100,
    "SECURE": 501,
}  # AUDIO_SYSTEM, VIDEO_SYSTEM, SECURE_SYSTEM (the KCPU's TeeManager on W16K, W16P and W17P: a
# fourth ring, rpc12 to rpc15, which the clnt's mode does not name: only AUDIO and VIDEO are
# checked against it)
# clnt mode bits (prepareCLNT's second argument): BLOCK_MODE 1, USE_INTR_BUF 2, SEND_AUDIO_CPU 4
BLOCK_MODE = 1
SEND_AUDIO_CPU = 4  # else the video CPU (KADP's video clnts pass mode 3)

# AudioConfig's AUDIO_CONFIG_CMD_GET_TASK_STARTED (msg_id 113) answers 0x10000003
ANSWER_STUB = "AUDIO_RPC_ToAgent_AudioConfig_0"
ANSWER_WORD, ANSWER_VALUE, ANSWER_RESULT = 0, 113, 0x10000003
ANSWER_SENDER = "KADP_AUDIO_SendConfig"  # the other way to it (W19P's RHAL_AUDIO_InitializeModule)

HELPERS = (
    "malloc",
    "calloc",
    "client_call",
    "rtk_xdrmem_create",
    "prepareCLNT",
    "prepareCLNT2",
    "prepareRPCCall",
    "__timedwaitReply",
)


class Ambiguous(Exception):
    pass


# --- ELF ---


class Lib:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        import io

        elf = ELFFile(io.BytesIO(self.data))
        if elf["e_machine"] != "EM_ARM" or elf.elfclass != 32:
            raise Ambiguous(f"{path}: not an ELF32 ARM library")
        self.segments = [
            (s["p_vaddr"], s["p_offset"], s["p_filesz"])
            for s in elf.iter_segments()
            if s["p_type"] == "PT_LOAD"
        ]
        self.sections = {s.name: (s["sh_addr"], s["sh_size"]) for s in elf.iter_sections()}
        dynsym = elf.get_section_by_name(".dynsym")
        self.funcs = {}  # name -> (start, size, thumb)
        for sym in dynsym.iter_symbols():
            if (
                sym["st_info"]["type"] == "STT_FUNC"
                and sym["st_value"]
                and sym["st_shndx"] != "SHN_UNDEF"
            ):
                v = sym["st_value"]
                self.funcs[sym.name] = (v & ~1, sym["st_size"], bool(v & 1))
        self.by_addr = {}  # call target -> name
        for name, (start, _, _) in self.funcs.items():
            self.by_addr.setdefault(start, name)
        self.ranges = sorted((s, s + n, name) for name, (s, n, _) in self.funcs.items() if n)
        self.starts = [r[0] for r in self.ranges]
        self._plt(elf, dynsym)
        self.cs_thumb = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
        self.cs_arm = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
        for cs in (self.cs_thumb, self.cs_arm):
            cs.detail = True

    def read(self, addr, size):
        for vaddr, off, filesz in self.segments:
            if vaddr <= addr and addr + size <= vaddr + filesz:
                return self.data[off + addr - vaddr : off + addr - vaddr + size]
        return None

    def word(self, addr):
        b = self.read(addr, 4)
        return struct.unpack("<I", b)[0] if b else None

    def _plt(self, elf, dynsym):
        """PLT entries: 'add ip, pc, #A; add ip, ip, #B; ldr pc, [ip, #C]!' -> the GOT slot's
        symbol, both the ARM entry and a Thumb 'bx pc; b' prefix before it."""
        slots = {}
        for name in (".rel.plt", ".rela.plt"):
            sec = elf.get_section_by_name(name)
            if sec:
                for rel in sec.iter_relocations():
                    slots[rel["r_offset"]] = dynsym.get_symbol(rel["r_info_sym"]).name
        self.plt = {}
        if ".plt" not in self.sections:
            return
        start, size = self.sections[".plt"]
        cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
        cs.detail = True
        cs.skipdata = True  # the Thumb 'bx pc; b' prefixes do not decode as ARM
        insns = list(cs.disasm(self.read(start, size), start))
        for i in range(len(insns) - 2):
            a, b, c = insns[i : i + 3]
            if (
                (a.mnemonic, b.mnemonic) != ("add", "add")
                or c.id == 0
                or c.mnemonic != "ldr"
                or not a.op_str.startswith("ip, pc, #")
                or not b.op_str.startswith("ip, ip, #")
                or not c.op_str.startswith("pc, [ip, #")
            ):
                continue
            got = a.address + 8 + arm_imm(a.bytes) + arm_imm(b.bytes) + arm_imm(c.bytes)
            if got not in slots:
                raise Ambiguous(f"PLT entry {a.address:#x}: GOT slot {got:#x} has no relocation")
            self.plt[a.address] = slots[got]
            if self.read(a.address - 4, 2) == b"\x78\x47":  # Thumb 'bx pc'
                self.plt[a.address - 4] = slots[got]

    def target_name(self, addr):
        return self.by_addr.get(addr) or self.plt.get(addr)

    def targets(self, name):
        """every call target that reaches name: its definition and its PLT entries"""
        t = {a for a, n in self.plt.items() if n == name}
        if name in self.funcs:
            t.add(self.funcs[name][0])
        return t

    def containing(self, addr):
        i = bisect.bisect_right(self.starts, addr) - 1
        if i >= 0 and self.ranges[i][0] <= addr < self.ranges[i][1]:
            return self.ranges[i][2]
        return None

    def disasm(self, name):
        start, size, thumb = self.funcs[name]
        return list((self.cs_thumb if thumb else self.cs_arm).disasm(self.read(start, size), start))


def arm_imm(b):
    """the immediate of an ARM 'add rd, rn, #imm' (rotated imm8) or 'ldr rt, [rn, #+-imm12]'"""
    w = struct.unpack("<I", bytes(b))[0]
    if (w >> 26) & 3 == 1:
        return (w & 0xFFF) if w & (1 << 23) else -(w & 0xFFF)
    rot = 2 * ((w >> 8) & 0xF)
    return ((w & 0xFF) >> rot | (w & 0xFF) << (32 - rot)) & 0xFFFFFFFF


# --- raw BL/BLX scan: every call site of a set of targets, in Thumb and ARM encodings ---


def call_sites(lib, targets):
    start, size = lib.sections[".text"]
    text = lib.read(start, size)
    sites = set()
    hw = struct.unpack(f"<{size // 2}H", text[: size // 2 * 2])
    for i in range(len(hw) - 1):
        h1, h2 = hw[i], hw[i + 1]
        if h1 & 0xF800 != 0xF000 or h2 & 0xC000 != 0xC000:
            continue
        s = (h1 >> 10) & 1
        i1 = 1 - (((h2 >> 13) & 1) ^ s)
        i2 = 1 - (((h2 >> 11) & 1) ^ s)
        off = (s << 24) | (i1 << 23) | (i2 << 22) | ((h1 & 0x3FF) << 12) | ((h2 & 0x7FF) << 1)
        if s:
            off -= 1 << 25
        pc = start + 2 * i + 4
        tgt = pc + off if h2 & 0x1000 else (pc & ~3) + off
        if tgt in targets:
            sites.add(start + 2 * i)
    for i in range(0, size - 3, 4):
        w = struct.unpack_from("<I", text, i)[0]
        if w >> 25 == 0x7D:  # blx imm
            off = ((w & 0xFFFFFF) << 2) | ((w >> 23) & 2)
        elif w & 0x0F000000 == 0x0B000000 and w >> 28 != 0xF:
            off = (w & 0xFFFFFF) << 2
        else:
            continue
        if off & (1 << 25):
            off -= 1 << 26
        if start + i + 8 + off in targets:
            sites.add(start + i)
    return sites


# --- a forward register tracker over one function ---


class Regs:
    """constants ('imm', v) and stack addresses ('sp', offset from the entry sp)"""

    def __init__(self, lib, thumb):
        self.lib, self.thumb = lib, thumb
        self.v = {}
        self.sp = 0

    def get(self, reg):
        return self.v.get(reg)

    def imm(self, reg):
        x = self.v.get(reg)
        return x[1] if x and x[0] == "imm" else None

    def step(self, insn):
        """update for insn; returns the call target if it is a bl/blx to an address"""
        m, ops = insn.mnemonic.split(".")[0], insn.operands
        regs = lambda: [insn.reg_name(o.reg) for o in ops if o.type == carm.ARM_OP_REG]
        written = [insn.reg_name(r) for r in insn.regs_access()[1]]
        if m in ("bl", "blx") and ops and ops[0].type == carm.ARM_OP_IMM:
            for r in ("r0", "r1", "r2", "r3", "r12", "ip", "lr"):
                self.v.pop(r, None)
            return ops[0].imm
        if m in ("push", "stmdb") and "sp" in written:
            self.sp -= 4 * (len(ops) - (0 if m == "push" else 1))
            return None
        if m in ("sub", "subs", "sub.w", "subw") and len(ops) >= 2 and regs()[:1] == ["sp"]:
            if ops[-1].type == carm.ARM_OP_IMM and (len(ops) == 2 or regs()[1:2] == ["sp"]):
                self.sp -= ops[-1].imm
                return None
        new = None
        if m in ("mov", "movs", "movw", "mvn", "mvns") and len(ops) == 2:
            if ops[1].type == carm.ARM_OP_IMM:
                new = ("imm", (~ops[1].imm if m.startswith("mvn") else ops[1].imm) & 0xFFFFFFFF)
            elif ops[1].type == carm.ARM_OP_REG and m.startswith("mov") and not ops[1].shift.type:
                src = insn.reg_name(ops[1].reg)
                new = ("sp", self.sp) if src == "sp" else self.v.get(src)
        elif m == "movt" and len(ops) == 2:
            lo = self.imm(insn.reg_name(ops[0].reg))
            if lo is not None:
                new = ("imm", (lo & 0xFFFF) | (ops[1].imm << 16))
        elif m in ("add", "adds", "addw") and len(ops) == 3 and ops[2].type == carm.ARM_OP_IMM:
            src = insn.reg_name(ops[1].reg)
            base = ("sp", self.sp) if src == "sp" else self.v.get(src)
            if base:
                new = (base[0], (base[1] + ops[2].imm) & (0xFFFFFFFF if base[0] == "imm" else -1))
        elif m in ("add", "adds") and len(ops) == 2 and ops[1].type == carm.ARM_OP_IMM:
            base = self.v.get(insn.reg_name(ops[0].reg))
            if base:
                new = (base[0], base[1] + ops[1].imm)
        elif (
            m == "ldr"
            and len(ops) == 2
            and ops[1].type == carm.ARM_OP_MEM
            and insn.reg_name(ops[1].mem.base) == "pc"
            and not ops[1].mem.index
        ):
            addr = ((insn.address + (4 if self.thumb else 8)) & ~3) + ops[1].mem.disp
            w = self.lib.word(addr)
            if w is not None:
                new = ("imm", w)
        for r in written:
            if r in ("sp", "pc"):
                continue
            self.v.pop(r, None)
        if new is not None and ops and ops[0].type == carm.ARM_OP_REG:
            self.v[insn.reg_name(ops[0].reg)] = new
        return None


# --- the analysis ---


def analyse_stub(lib, name):
    """one client_call caller: malloc size, procedure, XDR size and decoder"""
    r = Regs(lib, lib.funcs[name][2])
    out = {"malloc": [], "client_call": [], "xdrmem": [], "decoder": None, "waits": False}
    pending_decoder = False
    for insn in lib.disasm(name):
        before = dict(r.v)
        tgt = r.step(insn)
        if tgt is None:
            continue
        callee = lib.target_name(tgt)
        imm = lambda reg: before[reg][1] if before.get(reg, ("",))[0] == "imm" else None
        if pending_decoder:
            out["decoder"] = callee or f"{tgt:#x}"
            pending_decoder = False
        if callee == "malloc":
            out["malloc"].append(imm("r0"))
        elif callee == "calloc":
            a, b = imm("r0"), imm("r1")
            out["malloc"].append(a * b if a is not None and b is not None else None)
        elif callee == "client_call":
            out["client_call"].append(imm("r1"))
        elif callee == "rtk_xdrmem_create":
            out["xdrmem"].append(imm("r2"))
            pending_decoder = True
        elif callee == "__timedwaitReply":
            out["waits"] = True
    return out


def analyse_callers(lib, stubs):
    """the clnt each stub call site passes (r1), where a prepareCLNT/prepareCLNT2 in the same
    function set it up on the stack: -> {stub: [(mode, program) or None per site]}"""
    stub_targets = {}
    for s in stubs:
        for t in lib.targets(s):
            stub_targets[t] = s
    prep = lib.targets("prepareCLNT") | lib.targets("prepareCLNT2")
    sites = call_sites(lib, set(stub_targets))
    funcs = {lib.containing(a) for a in sites}
    seen = {s: [] for s in stubs}
    outside = [a for a in sites if lib.containing(a) is None]
    for f in sorted(x for x in funcs if x):
        r = Regs(lib, lib.funcs[f][2])
        clnts = {}
        copied = None
        for insn in lib.disasm(f):
            before = dict(r.v)
            m = insn.mnemonic.split(".")[0]
            if m[:3] in ("ldm", "stm") and m != "stmdb" and insn.operands:
                # a clnt struct copied by value: ldm from one stack slot, stm to another
                b = insn.reg_name(insn.operands[0].reg)
                base = ("sp", r.sp) if b == "sp" else before.get(b)
                if m.startswith("ldm"):
                    copied = clnts.get(base[1]) if base and base[0] == "sp" else None
                elif copied and base and base[0] == "sp":
                    clnts[base[1]] = copied
            tgt = r.step(insn)
            if tgt is None:
                continue
            if tgt in prep:
                c, mode, prog = before.get("r0"), before.get("r1"), before.get("r2")
                if c and c[0] == "sp":
                    clnts[c[1]] = (
                        mode[1] if mode and mode[0] == "imm" else None,
                        prog[1] if prog and prog[0] == "imm" else None,
                    )
            elif tgt in stub_targets:
                c = before.get("r1")
                seen[stub_targets[tgt]].append(clnts.get(c[1]) if c and c[0] == "sp" else None)
    return seen, outside


def find_lib(src, tmp):
    """a firmware directory's libkadaptor, unpacked into tmp; or src itself"""
    if os.path.isfile(src):
        return src
    for pak in sorted(glob.glob(os.path.join(src, "bsppart*.pak"))):
        d = os.path.join(tmp, os.path.basename(pak))
        subprocess.run(
            ["unsquashfs", "-q", "-n", "-d", d, pak, "bsp/usr/lib"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        libs = [
            p
            for p in glob.glob(os.path.join(d, "bsp/usr/lib/libkadaptor.so*"))
            if not os.path.islink(p)
        ]
        if len(libs) > 1:
            raise Ambiguous(f"{pak}: several libkadaptor: {libs}")
        if libs:
            return libs[0]
    libs = [
        p
        for p in glob.glob(os.path.join(src, "rootfs/usr/lib/libkadaptor.so*"))
        if not os.path.islink(p)
    ]
    if len(libs) == 1:
        return libs[0]
    raise Ambiguous(f"{src}: no single libkadaptor in bsppart*.pak or rootfs/usr/lib ({libs})")


def analyse(path):
    _import_analysers()
    lib = Lib(path)
    errors, notes = [], []
    for h in ("client_call", "rtk_xdrmem_create"):
        if not lib.targets(h):
            raise Ambiguous(f"{path}: no {h}")
    if not lib.targets("malloc"):
        raise Ambiguous(f"{path}: no malloc PLT entry")

    # every client_call caller is a stub; one outside the dynamic symbols cannot be named
    cc = lib.targets("client_call")
    sites = call_sites(lib, cc)
    callers = {}
    for a in sorted(sites):
        f = lib.containing(a)
        if f is None:
            raise Ambiguous(f"client_call called at {a:#x}, outside every dynamic symbol")
        callers.setdefault(f, []).append(a)
    for h in HELPERS:
        callers.pop(h, None)
    stubs = sorted(callers)

    rows = []
    for s in stubs:
        a = analyse_stub(lib, s)
        if len(a["client_call"]) != len(callers[s]):
            raise Ambiguous(
                f"{s}: {len(callers[s])} client_call sites in the raw scan, "
                f"{len(a['client_call'])} in the disassembly"
            )
        if len(a["client_call"]) != 1 or a["client_call"][0] is None:
            raise Ambiguous(f"{s}: procedure not one constant: {a['client_call']}")
        sizes = set(a["malloc"])
        if None in sizes or len(sizes) > 1:
            raise Ambiguous(f"{s}: malloc size not one constant: {a['malloc']}")
        n = sizes.pop() if sizes else 0
        x = a["xdrmem"][0] if len(a["xdrmem"]) == 1 else None
        if len(a["xdrmem"]) > 1 or (a["xdrmem"] and x is None):
            raise Ambiguous(f"{s}: XDR decode size not one constant: {a['xdrmem']}")
        m = re.match(r"(AUDIO|VIDEO|SECURE)_", s)
        rows.append(
            {
                "stub": s,
                "prefix_program": PROGRAMS[m.group(1)] if m else None,
                "procedure": a["client_call"][0],
                "result": n,
                "xdr": x,
                "decoder": a["decoder"],
                "waits": a["waits"],
            }
        )

    # the program: the name's prefix, checked against the clnt at the call sites
    seen, outside = analyse_callers(lib, stubs)
    if outside:
        notes.append(f"{len(outside)} stub call sites outside the dynamic symbols (not attributed)")
    for row in rows:
        sites = seen[row["stub"]]
        known = [x for x in sites if x and x[1] is not None]
        progs = {p for _, p in known}
        cpus = {201 if m & SEND_AUDIO_CPU else 100 for m, _ in known if m is not None}
        row["call_sites"] = len(sites)
        row["sites_blocking"] = sum(1 for m, _ in known if m is not None and m & BLOCK_MODE)
        row["sites_nonblocking"] = sum(1 for m, _ in known if m is not None and not m & BLOCK_MODE)
        row["sites_unknown"] = len(sites) - row["sites_blocking"] - row["sites_nonblocking"]
        row["caller_programs"] = sorted(progs)
        p = row["prefix_program"]
        if p is not None and progs - {p}:
            errors.append(f"{row['stub']}: prefix says program {p}, callers pass {sorted(progs)}")
        if p is None:
            if len(progs) != 1:
                # harmless while its answer is the default 4 bytes of S_OK
                msg = f"{row['stub']}: program unresolved (callers: {sorted(progs)})"
                if row["result"] in (0, 4) and not re.search(r"_Create_0$", row["stub"]):
                    notes.append(msg + f", result {row['result']}: the default answer fits")
                else:
                    errors.append(msg)
                row["program"], row["create"] = None, False
                continue
            p = progs.pop()
        if p in (100, 201) and cpus - {p}:
            errors.append(f"{row['stub']}: program {p} sent to the CPU of {sorted(cpus)}")
        row["program"] = p
        row["create"] = (
            bool(re.search(r"_Create_0$", row["stub"]))
            and row["decoder"] == "xdr_RPCRES_LONG"
            and row["result"] == 8
        )
        if re.search(r"_Create_0$", row["stub"]) and not row["create"]:
            notes.append(
                f"{row['stub']}: named Create but decodes {row['decoder']} "
                f"into {row['result']} bytes, not a Create"
            )
        if row["xdr"] is not None and row["xdr"] != row["result"]:
            notes.append(
                f"{row['stub']}: XDR size {row['xdr']} != malloc {row['result']} "
                "(the reply is the malloc size)"
            )
        if row["result"] == 0 and row["waits"]:
            notes.append(f"{row['stub']}: waits for a reply but has no result buffer")
    if errors:
        raise Ambiguous("\n".join(errors))

    # one size per (program, procedure)
    keys = {}
    for row in rows:
        if row["program"] is not None:
            keys.setdefault((row["program"], row["procedure"]), []).append(row)
    for (p, n), rs in sorted(keys.items()):
        if len({r["result"] for r in rs}) > 1:
            errors.append(
                f"{p}:{n}: several result sizes: "
                + ", ".join(f"{r['stub']} {r['result']}" for r in rs)
            )
        if len({r["create"] for r in rs}) > 1:
            errors.append(f"{p}:{n}: Create and not: " + ", ".join(r["stub"] for r in rs))
    if errors:
        raise Ambiguous("\n".join(errors))

    reply, create = [], []
    for (p, n), rs in sorted(keys.items()):
        size, is_create = rs[0]["result"], rs[0]["create"]
        if size > MAX_RESULT:
            raise Ambiguous(f"{p}:{n}: result {size} > rtk.ko's RPC_MAX_RESULT {MAX_RESULT}")
        if is_create:
            create.append(f"{p}:{n}")
        elif size not in (0, 4):
            reply.append(f"{p}:{n}:{size}")
        # 0: no result buffer; rtk.ko sends nothing to context 0 whatever the size

    # the fixed answer: AudioConfig's procedure, and the literals in its init caller
    answer = []
    ac = [r for r in rows if r["stub"] == ANSWER_STUB]
    if len(ac) != 1:
        raise Ambiguous(f"{len(ac)} {ANSWER_STUB}")
    checked = []  # "library function" that asks for GET_TASK_STARTED and expects 0x10000003
    for other in sorted(glob.glob(os.path.join(os.path.dirname(path), "*.so*"))):
        if os.path.islink(other) or open(other, "rb").read(4) != b"\x7fELF":
            continue
        try:
            o = lib if other == path else Lib(other)
        except Ambiguous:
            continue
        # a function of a library that uses AudioConfig holding both constants (W19P:
        # RHAL_AUDIO_InitializeModule, through a static helper); the bytes of 0x10000003 as a
        # literal word or a Thumb 'movt rd, #0x1000' pick the candidates
        if not (o.targets(ANSWER_STUB) | o.targets(ANSWER_SENDER)):
            continue
        for f, (start, size, thumb) in sorted(o.funcs.items()):
            code = o.read(start, size + 64) or b""
            if (
                size
                and (b"\x03\x00\x00\x10" in code or b"\xc1\xf2" in code)
                and {ANSWER_VALUE, ANSWER_RESULT} <= literals(o, f)
            ):
                checked.append(f"{os.path.basename(other)} {f}")
    answer.append(
        f"{ac[0]['program']}:{ac[0]['procedure']}:{ANSWER_WORD}:{ANSWER_VALUE}:{ANSWER_RESULT:#x}"
    )
    if checked:
        notes.append("rpc_answer: the GET_TASK_STARTED poll is in " + ", ".join(checked))
    else:
        notes.append(
            "rpc_answer unverified: no function of a library using AudioConfig holds "
            f"{ANSWER_VALUE} and {ANSWER_RESULT:#x} (emitted anyway: msg_id {ANSWER_VALUE} "
            "is GET_TASK_STARTED whoever asks)"
        )

    for what, lst, lim in (
        ("rpc_reply", reply, MAX_REPLY),
        ("rpc_create", create, MAX_CREATE),
        ("rpc_answer", answer, MAX_ANSWER),
    ):
        if len(lst) > lim:
            raise Ambiguous(f"{len(lst)} {what} entries > rtk.ko's array size {lim}")

    params = []
    for what, lst in (("rpc_reply", reply), ("rpc_create", create), ("rpc_answer", answer)):
        if lst:
            params.append(f"{what}={','.join(lst)}")
    sizes = {}
    for row in rows:
        sizes[row["result"]] = sizes.get(row["result"], 0) + 1
    return {
        "library": os.path.basename(path),
        "sha256": lib.sha256,
        "counts": {
            "stubs": len(rows),
            "audio": sum(r["program"] == 201 for r in rows),
            "video": sum(r["program"] == 100 for r in rows),
            "unprefixed": sum(r["prefix_program"] is None for r in rows),
            "keys": len(keys),
            "result_sizes": {str(k): v for k, v in sorted(sizes.items())},
            "creates": len(create),
            "replies": len(reply),
            "answers": len(answer),
            "call_sites": sum(r["call_sites"] for r in rows),
            "sites_blocking": sum(r["sites_blocking"] for r in rows),
            "sites_nonblocking": sum(r["sites_nonblocking"] for r in rows),
            "sites_unknown": sum(r["sites_unknown"] for r in rows),
        },
        "params": params,
        "notes": notes,
        "stubs": rows,
    }


def literals(lib, name):
    """the constants a function loads: immediates and its literal-pool words"""
    out = set()
    r = Regs(lib, lib.funcs[name][2])
    for insn in lib.disasm(name):
        r.step(insn)
        for v in r.v.values():
            if v[0] == "imm":
                out.add(v[1])
    return out


# --- output ---


def summary(res, name):
    c = res["counts"]
    s = [
        f"{name}: {res['library']} sha256 {res['sha256'][:16]}",
        f"  stubs {c['stubs']} (audio {c['audio']}, video {c['video']}, unprefixed "
        f"{c['unprefixed']}), keys {c['keys']}, result sizes {c['result_sizes']}",
        f"  rpc_reply {c['replies']}, rpc_create {c['creates']}, rpc_answer {c['answers']}; "
        f"call sites {c['call_sites']} (blocking {c['sites_blocking']}, nonblocking "
        f"{c['sites_nonblocking']}, clnt not local {c['sites_unknown']})",
    ]
    s += [f"  note: {n}" for n in res["notes"]]
    s += [f"  {p.split('=')[0]}: {len(p)} bytes" for p in res["params"]]
    return "\n".join(s)


def run(src, name, cache):
    installed = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if os.path.isfile(os.path.join(installed, "installation.toml")):
        root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        cdir = os.path.join(root, "beanjar", "rtk", name) if cache else None
    else:
        cdir = os.path.join(REPO, "build", "rtk", name) if cache else None
    tmp = tempfile.mkdtemp(prefix="rtk-gen-")
    try:
        path = find_lib(src, tmp)
        if cdir:
            sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
            try:
                with open(os.path.join(cdir, "rpc-table.json")) as f:
                    res = json.load(f)
                if res["sha256"] == sha:
                    return res
            except (OSError, ValueError, KeyError):
                pass
        res = analyse(path)
    finally:
        shutil.rmtree(tmp)
    if cdir:
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "rpc-table.json"), "w") as f:
            json.dump(res, f, indent=1)
        with open(os.path.join(cdir, "params"), "w") as f:
            f.write(" ".join(res["params"]) + "\n")
    return res


def self_test_firmware(base, name):
    """The directory of the firmware name: in base, else in the host's collections (beanjar's
    probe.find_firmware)."""
    if base:
        return os.path.join(base, name)
    sys.path.insert(0, os.path.join(REPO, "host", "core"))
    from beanjar.firmware import probe
    from beanjar.runtime import hostconfig

    return probe.find_firmware(name) or os.path.join(hostconfig.firmware_dirs()[0], name)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source", nargs="?", help="a firmware directory or a libkadaptor")
    ap.add_argument("--name", help="the firmware name for --cache (default: the directory's)")
    ap.add_argument("--cache", action="store_true", help="write/reuse build/rtk/<name>/")
    ap.add_argument("--json", help="write the full table here")
    ap.add_argument("--self-test", action="store_true", help="run on W19P, W20L and W22H")
    ap.add_argument(
        "--firmwares",
        help="with --self-test: the firmwares' directory (default: the host's collections, "
        "beanjar host config's firmware_dirs)",
    )
    a = ap.parse_args()
    try:
        if a.self_test:
            ok = True
            for fw in SELF_TEST:
                try:
                    res = run(self_test_firmware(a.firmwares, fw), fw, a.cache)
                    print(summary(res, fw))
                    bad = [p for p in SELF_TEST_PARAMS if p not in res["params"]]
                    if bad:
                        print(f"  FAILED: expected {bad}")
                        ok = False
                except (Ambiguous, OSError, subprocess.CalledProcessError) as e:
                    print(f"{fw}: FAILED: {e}")
                    ok = False
            return 0 if ok else 1
        if not a.source:
            ap.error("a source, or --self-test")
        name = a.name or os.path.basename(os.path.normpath(a.source))
        if a.cache and os.path.isfile(a.source) and not a.name:
            ap.error("--cache on a library needs --name")
        res = run(a.source, name, a.cache)
    except Ambiguous as e:
        print(f"rpc-table: {e}", file=sys.stderr)
        return 1
    print(summary(res, name), file=sys.stderr)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=1)
    print(" ".join(res["params"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
