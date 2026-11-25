#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Writes tests/regress/tv-expect/<target>.toml, the TV feature suite's expected table (tests/regress/lib/tvsuite.py),
from each supported target's firmware (as scripts/regress picks it: the host's firmware collections, firmware_dirs,
the default firmware first), read with unsquashfs from its paks:

  tests/regress/tv-expect-gen.py [--target TARGET]... [--fw NAME]... [--table decode]... [-o DIR]

For each target: the release (etc/starfish-release), the broadcast standard of its paks, connman's online
check (etc/connman/main.conf), for every check of luna-info whether the firmware has it, with
the evidence, the stock app of each of the apps step's roles (apps_of), and for the decode step
each case's codecs as the stock decoder elements, umediaserver's capability table and (MSE) the
web engine claim them (decode_of; and whether the engine ends an MSE stream without audio at all,
mse_audio_eos_of), and for the smp step whether the StarfishMediaAPIs library exports what
tests/regress/smp/smpcheck.c looks up (smp_of). A check is expected (present) unless the firmware shows it absent:
  service  no role (usr/share/ls2/roles, luna-service2/roles.d: allowedNames) or services.d file
           (Name=) provides the name; the tv.* services are tvservice's (targets.TVSERVICE's pak) where
           the firmware has that pak; a com.palm.* name the hub maps (ls-hubd has "com.palm.") is
           its com.webos.* namesake's
  method   the method's name is not a string of the service's executable (nor of the libraries it
           NEEDs from the firmware)
  key      getSystemInfo: the key is not a string of that executable; getSystemSettings: no
           etc/palm/defaultSettings.json record of the category has it
  nyx      nyx-cmd's plugin (usr/lib/nyx/nyxcmd/libnyxcmdModule<M>.so) has no such query; for
           DeviceInfo, the module's device_info_query sends the query type to its default case
           (NYX_ERROR_NOT_IMPLEMENTED), or the module's command is not in the firmware (hcitool)
getSystemInfo's UHD, where tvservicemock answers it from configd's tv.hw.panelResolution, also
has the value it should answer: the panel the firmware's own hw-option-gen gives the board (uhd_of).
Absence is only ever claimed on such evidence; a string that is there proves nothing more than
that the check runs, so an API the firmware lacks in some other way shows as a FAIL to look at.
The source of each row is in the file; SKIP rows cite it.
"""

import argparse
import fnmatch
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "lib"))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
import tvsuite  # noqa: E402
import checklist  # noqa: E402
from beanjar import superpak, targets  # noqa: E402
from beanjar.tomlfile import toml_key, toml_value  # noqa: E402

OBJDUMP = "/opt/arm-webos-linux-gnueabi_sdk-buildroot/bin/arm-webos-linux-gnueabi-objdump"


def regress_module():
    loader = importlib.machinery.SourceFileLoader(
        "regress", os.path.join(TOP, "scripts", "regress")
    )
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("regress", loader))
    loader.exec_module(mod)
    return mod


class Firmware:
    """A firmware directory's paks, read with unsquashfs (the rootfs first). A super.pak firmware
    keeps its rootfs, otncabi and the like as the logical images of super.pak (beanjar.superpak:
    dpmeta.pak's linear map); each is a pak here under its own name (rootfs.pak, otncabi.pak, ...),
    read with unsquashfs -o at the image's offset."""

    def __init__(self, d):
        self.dir = d
        logical = superpak.images(d)
        self._src = {f"{n}.pak": (i.path, i.offset) for n, i in logical.items()}
        names = {p for p in os.listdir(d) if p.endswith(".pak") and p != "super.pak"}
        names |= set(self._src)
        self.paks = ["rootfs.pak"] + sorted(names - {"rootfs.pak"})
        self._lists, self._cache = {}, {}

    def source(self, pak):
        """(file, byte offset) of a pak's squashfs: the pak file, or a super.pak image."""
        return self._src.get(pak) or (os.path.join(self.dir, pak), 0)

    def files(self, pak):
        """{path: symlink target or None} of a pak's files ({} when it is not squashfs)."""
        if pak not in self._lists:
            src, offset = self.source(pak)
            r = subprocess.run(
                ["unsquashfs", "-lls", "-o", str(offset), src], capture_output=True, text=True
            )
            out = {}
            for line in r.stdout.splitlines():
                m = re.match(
                    r"^([dl-])\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+squashfs-root/(\S.*?)(?: -> (.*))?$",
                    line,
                )
                if m and m.group(1) != "d":
                    out[m.group(2)] = m.group(3) if m.group(1) == "l" else None
            self._lists[pak] = out
        return self._lists[pak]

    def find(self, path, paks=None):
        """(pak, path) of a file, symlinks followed, or None."""
        path = path.lstrip("/")
        for pak in paks or self.paks:
            f = self.files(pak)
            for _ in range(8):
                if path not in f:
                    break
                if f[path] is None:
                    return pak, path
                t = f[path]
                path = (
                    os.path.normpath(os.path.join(os.path.dirname(path), t)).lstrip("/")
                    if not t.startswith("/")
                    else t.lstrip("/")
                )
        return None

    def read(self, pak, path):
        key = (pak, path)
        if key not in self._cache:
            src, offset = self.source(pak)
            r = subprocess.run(
                ["unsquashfs", "-cat", "-o", str(offset), src, path], capture_output=True
            )
            self._cache[key] = r.stdout if r.returncode == 0 else b""
        return self._cache[key]

    def glob(self, pattern, pak="rootfs.pak"):
        return sorted(p for p in self.files(pak) if fnmatch.fnmatch(p, pattern))


def providers(fw):
    """{service name pattern: [(executable, source file)]} from the role and service files."""
    out = {}
    for path in fw.glob("usr/share/ls2/roles/*/*.json") + fw.glob(
        "usr/share/luna-service2/roles.d/*.json"
    ):
        try:
            d = json.loads(fw.read("rootfs.pak", path))
        except ValueError:
            continue
        role = d.get("role", d) if isinstance(d, dict) else {}
        exe = role.get("exeName")
        for n in role.get("allowedNames") or []:
            if exe and isinstance(n, str):
                out.setdefault(n, []).append((exe, path))
    for path in fw.glob("usr/share/luna-service2/services.d/*.service"):
        text = fw.read("rootfs.pak", path).decode(errors="replace")
        name = re.search(r"^Name=(\S+)", text, re.M)
        exe = re.search(r"^Exec=\"?([^\s\"]+)", text, re.M)
        if name and exe:
            for n in name.group(1).split(";"):
                out.setdefault(n, []).append((exe.group(1), path))
    return out


def provider(fw, provs, service, tvservice_pak):
    """([(pak, path)] of the executables that may provide a service, source), or (None, why it is
    absent)."""
    if service.startswith("com.webos.service.tv.") and tvservice_pak:
        loc = fw.find(targets.TVSERVICE[1], [tvservice_pak])
        if loc:
            return [loc], f"{tvservice_pak}:{targets.TVSERVICE[1]} (tvservice)"
    # the name itself, else the most specific pattern (luna-send's and node's bare "*" provide nothing)
    pats = sorted(
        (p for p in provs if p != "*" and fnmatch.fnmatchcase(service, p)),
        key=lambda p: (p != service, -len(p.split("*")[0])),
    )
    hits = list(provs[pats[0]]) if pats else []
    if not hits and service.startswith("com.palm."):
        hub = fw.find("usr/sbin/ls-hubd")
        last = service.rsplit(".", 1)[1]
        twins = [
            (exe, f"{src} (com.palm. names mapped by usr/sbin/ls-hubd)")
            for pat, lst in provs.items()
            if pat.startswith("com.webos.") and pat.endswith("." + last)
            for exe, src in lst
        ]
        if hub and b"com.palm.\0" in fw.read(*hub):
            hits = twins
    if not hits:
        return (
            None,
            f"no role or services.d file provides {service} (usr/share/ls2/roles, "
            f"usr/share/luna-service2/roles.d, services.d)",
        )
    locs = [fw.find(exe) for exe, _ in hits]
    if not any(locs):
        return None, f"{hits[0][1]} names {hits[0][0]}, which is not in the firmware"
    srcs = {}
    for exe, src in hits:
        srcs.setdefault(exe, []).append(src)
    return list(dict.fromkeys(x for x in locs if x)), "; ".join(
        f"{', '.join(v)} -> {k}" for k, v in srcs.items()
    )


SYSTEM_LIBS = re.compile(
    r"^lib(c|m|dl|rt|pthread|stdc\+\+|gcc_s|glib-2\.0|gobject-2\.0|gio-2\.0|z|crypto|ssl)\.so"
)


def needed(data):
    try:
        from elftools.elf.elffile import ELFFile

        elf = ELFFile(io.BytesIO(data))
        dyn = elf.get_section_by_name(".dynamic")
        return [t.needed for t in dyn.iter_tags() if t.entry.d_tag == "DT_NEEDED"] if dyn else []
    except Exception:
        return []


def binaries(fw, pak, path):
    """[(label, bytes)] of an executable and the firmware's own libraries it NEEDs."""
    data = fw.read(pak, path)
    out = [(f"{pak}:/{path}", data)]
    for lib in needed(data):
        if SYSTEM_LIBS.match(lib):
            continue
        loc = fw.find(f"usr/lib/{lib}") or fw.find(f"lib/{lib}")
        if loc:
            out.append((f"{loc[0]}:/{loc[1]}", fw.read(*loc)))
    return out


def labels(bins):
    """The executables of bins by name, their libraries counted."""
    exes = list(dict.fromkeys(label for label, _ in bins if "/lib/" not in label))
    libs = len(bins) - len(exes)
    return ", ".join(exes) + (
        f" (and {libs} librar{'y' if libs == 1 else 'ies'} of the firmware they NEED)"
        if libs
        else ""
    )


def has_string(bins, s):
    b = s.encode()
    return next((label for label, data in bins if b in data), None)


REGISTER = {"LSRegister": (0, 1), "LSRegisterPubPriv": (0, 1), "LSRegisterPalmService": (0, 1)}
CATEGORY = {
    "LSRegisterCategory": (2,),
    "LSRegisterCategoryAppend": (2,),
    "LSPalmServiceRegisterCategory": (2, 3),
}
_ARM_INSN = re.compile(r"^\s*([0-9a-f]+):\s+((?:[0-9a-f]{2,8} ?)+)\s+(\S+)\s*(.*?)\s*(?:;.*)?$")


def _elf_reader(data):
    from elftools.elf.elffile import ELFFile

    elf = ELFFile(io.BytesIO(data))
    segs = [
        (g["p_vaddr"], g["p_offset"], g["p_filesz"])
        for g in elf.iter_segments()
        if g["p_type"] == "PT_LOAD"
    ]

    def at(va, n):
        for v, o, size in segs:
            if v <= va < v + size:
                return data[o + va - v : o + va - v + n]
        return b""

    return at


class Code:
    """A 32-bit ARM ELF's code as objdump disassembles it, with the constants each call to a PLT
    function gets in its argument registers (calls())."""

    def __init__(self, data):
        self.data = data
        self.at = _elf_reader(data)

    def cstr(self, va):
        b = self.at(va, 256)
        return b.split(b"\0", 1)[0].decode(errors="replace") if b else None

    def word(self, va):
        b = self.at(va, 4)
        return int.from_bytes(b, "little") if len(b) == 4 else None

    def symbol(self, name):
        from elftools.elf.elffile import ELFFile

        sym = ELFFile(io.BytesIO(self.data)).get_section_by_name(".dynsym")
        s = next((x for x in sym.iter_symbols() if x.name == name), None) if sym else None
        return (s["st_value"] & ~1, s["st_size"]) if s else None

    def disassemble(self, start=None, stop=None):
        path = "/tmp/tv-expect-gen-code.elf"
        with open(path, "wb") as f:
            f.write(self.data)
        span = (
            [f"--start-address={start:#x}", f"--stop-address={stop:#x}"]
            if start is not None
            else []
        )
        dis = subprocess.run([OBJDUMP, "-d", *span, path], capture_output=True, text=True).stdout
        os.unlink(path)
        return dis

    def calls(self, start=None, stop=None):
        """[(address, PLT function, {register: value})] of each call to a PLT function, the
        registers' values the constants the function put there (movw/movt, literal pools, adr,
        pc-relative adds, a constant base plus an offset; ("mem", address) for a load from a
        constant address); a register whose value is not such a constant is left out."""
        out, regs = [], {}
        for line in self.disassemble(start, stop).splitlines():
            if line.endswith(">:"):
                regs = {}
                continue
            m = _ARM_INSN.match(line)
            if not m:
                continue
            addr, raw, op, args = int(m.group(1), 16), m.group(2).split(), m.group(3), m.group(4)
            thumb = len(raw[0]) == 4
            op = op.split(".")[0]
            bare = re.sub(r"\s*@.*$", "", args)
            a = [x.strip() for x in re.split(r",(?![^\[]*\])", bare)] if bare else []
            dst = a[0] if a else None
            try:
                if op in ("movw", "mov", "movs") and len(a) == 2 and a[1].startswith("#"):
                    regs[dst] = int(a[1][1:], 0) & 0xFFFFFFFF
                elif op == "movt" and dst in regs and isinstance(regs[dst], int):
                    regs[dst] = (regs[dst] & 0xFFFF) | (int(a[1][1:], 0) << 16)
                elif op in ("mov", "movs") and len(a) == 2 and a[1] in regs:
                    regs[dst] = regs[a[1]]
                elif op == "ldr" and "[pc" in args:
                    lit = re.search(r"@ \(?([0-9a-f]+)", line)
                    regs[dst] = self.word(int(lit.group(1), 16)) if lit else None
                elif op == "adr" or (
                    op in ("add", "addw") and len(a) == 3 and a[1] == "pc" and a[2].startswith("#")
                ):
                    lit = re.search(r"\(adr \w+, ([0-9a-f]+)\)|^\w+, ([0-9a-f]+)", args)
                    regs[dst] = int(next(g for g in lit.groups() if g), 16) if lit else None
                elif (
                    op == "add" and len(a) == 2 and a[1] == "pc" and isinstance(regs.get(dst), int)
                ):
                    regs[dst] = (regs[dst] + addr + (4 if thumb else 8)) & 0xFFFFFFFF
                elif (
                    op == "add"
                    and len(a) == 3
                    and "pc" in a[1:]
                    and isinstance(regs.get(next(x for x in a[1:] if x != "pc")), int)
                ):
                    other = next(x for x in a[1:] if x != "pc")
                    regs[dst] = (regs[other] + addr + (4 if thumb else 8)) & 0xFFFFFFFF
                elif (
                    op in ("add", "adds", "addw")
                    and len(a) == 3
                    and isinstance(regs.get(a[1]), int)
                    and a[2].startswith("#")
                ):
                    regs[dst] = (regs[a[1]] + int(a[2][1:], 0)) & 0xFFFFFFFF
                elif op == "ldr" and re.fullmatch(
                    r"\[(\w+)(?:, #(-?\w+))?\]", a[1] if len(a) > 1 else ""
                ):
                    mm = re.fullmatch(r"\[(\w+)(?:, #(-?\w+))?\]", a[1])
                    base = regs.get(mm.group(1))
                    regs[dst] = (
                        ("mem", base + int(mm.group(2) or "0", 0))
                        if isinstance(base, int)
                        else None
                    )
                elif op in ("bl", "blx"):
                    callee = re.search(r"<(\w+)@plt>", args)
                    if callee:
                        out.append((addr, callee.group(1), dict(regs)))
                    for r in ("r0", "r1", "r2", "r3", "ip", "lr"):
                        regs.pop(r, None)
                elif (
                    dst
                    and re.fullmatch(r"r\d+|ip|lr|sl|fp", dst)
                    and not op.startswith(("st", "cmp", "tst", "b", "push", "pop"))
                ):
                    regs.pop(dst, None)
            except (ValueError, TypeError, StopIteration):
                regs.pop(dst, None)
        return out


def ls_registrations(data, label="binary"):
    """{service name: {bus: {category: set of methods}}} of what an executable registers with the
    bus as its code says (Code.calls): each LSRegister/LSRegisterPubPriv/LSRegisterPalmService call
    (the name, r0; the handle's address, r1; LSRegisterPubPriv's bus, r2: "pub", "prv", else "any")
    and each LSRegisterCategory/...Append/LSPalmServiceRegisterCategory call (the handle loaded from
    that address, r0; the category, r1; the LSMethod tables, r2 (and r3)). A handle or table that is
    not such a constant is not followed: a name whose categories cannot be read is left out."""
    code = Code(data)

    def table(va):
        names = set()
        for i in range(512):
            p = code.word(va + 12 * i)
            n = code.cstr(p) if p else None
            if not n:
                break
            names.add(n)
        return names

    slots, cats = {}, []
    for _, fn, regs in code.calls():
        if fn in REGISTER:
            name, handle = regs.get("r0"), regs.get("r1")
            bus = (
                {0: "prv", 1: "pub"}.get(regs.get("r2"), "any")
                if fn == "LSRegisterPubPriv"
                else "any"
            )
            if isinstance(name, int) and isinstance(handle, int) and code.cstr(name):
                slots.setdefault(handle, set()).add((code.cstr(name), bus))
        elif fn in CATEGORY:
            h, c = regs.get("r0"), regs.get("r1")
            cat = code.cstr(c) if isinstance(c, int) and c else "/"
            for r in CATEGORY[fn]:
                t = regs.get(f"r{r}")
                if isinstance(h, tuple) and isinstance(t, int) and t:
                    cats.append((h[1], cat, table(t)))
    out = {}
    for slot, cat, methods in cats:
        for name, bus in slots.get(slot, ()):
            out.setdefault(name, {}).setdefault(bus, {}).setdefault(cat, set()).update(methods)
    return out


_REGS = {}


def registered(fw, bins, service, src):
    """(names, {category: methods}, where) of what the service's executables register under its
    name (ls_registrations) on either hub, or None where their code does not show it."""
    names = [service]
    if service.startswith("com.palm.") and "ls-hubd" in src:
        names.append(re.sub(r"^.*/", "", service))  # the twin's last part, matched below
    tables, where = {}, []
    for label, data in bins:
        if "/lib/" in label:
            continue
        if label not in _REGS:
            _REGS[label] = ls_registrations(data, label)
        for name, buses in _REGS[label].items():
            last = service.rsplit(".", 1)[1]
            if name != service and not (
                len(names) > 1 and name.startswith("com.webos.") and name.endswith("." + last)
            ):
                continue
            for bus, cats in buses.items():
                for cat, methods in cats.items():
                    tables.setdefault(cat, set()).update(methods)
                where.append(f"{label} ({name}, {bus})")
    if not where:
        return None
    return names, tables, ", ".join(dict.fromkeys(where))


def settings_keys(fw):
    """{category: set of keys} of etc/palm/defaultSettings.json's records."""
    loc = fw.find("etc/palm/defaultSettings.json")
    out = {}
    if not loc:
        return out
    text = fw.read(*loc).decode(errors="replace")
    dec, i = json.JSONDecoder(), 0
    while True:
        while i < len(text) and text[i] not in "{[":
            i += 1
        if i >= len(text):
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        for rec in obj if isinstance(obj, list) else [obj]:
            if isinstance(rec, dict) and isinstance(rec.get("value"), dict):
                out.setdefault(rec.get("category", ""), set()).update(rec["value"])
    return out


def jump_table(data, func):
    """{case index: target} and the default target of a function's switch on its second argument
    (r1), in the forms LG's compilers made: ARM `addls pc, pc, r1, lsl #2` over a branch table,
    Thumb `tbb [pc, r1]` and `tbh [pc, r1, lsl #1]` (K8HP's), Thumb `ldr.w rX, [rY, r1, lsl #2]`
    over offsets; None if none."""
    from elftools.elf.elffile import ELFFile

    elf = ELFFile(io.BytesIO(data))
    sym = elf.get_section_by_name(".dynsym")
    s = next((x for x in sym.iter_symbols() if x.name == func), None) if sym else None
    if s is None:
        return None
    start = s["st_value"] & ~1

    def at(va, n):
        for seg in elf.iter_segments():
            if (
                seg["p_type"] == "PT_LOAD"
                and seg["p_vaddr"] <= va < seg["p_vaddr"] + seg["p_filesz"]
            ):
                off = seg["p_offset"] + va - seg["p_vaddr"]
                return data[off : off + n]
        return b""

    path = "/tmp/tv-expect-gen.elf"
    with open(path, "wb") as f:
        f.write(data)
    dis = subprocess.run(
        [
            OBJDUMP,
            "-d",
            f"--start-address={start:#x}",
            f"--stop-address={start + s['st_size']:#x}",
            path,
        ],
        capture_output=True,
        text=True,
    ).stdout
    os.unlink(path)
    lines = [
        (int(m.group(1), 16), m.group(2))
        for m in re.finditer(r"^\s*([0-9a-f]+):\s+(?:[0-9a-f]{4,8} ?)+\s+(.*)$", dis, re.M)
    ]
    n = default = None
    for i, (a, ins) in enumerate(lines):
        m = re.match(r"cmp\s+r1, #(\d+)", ins)
        if m:
            n = int(m.group(1))
            continue
        if n is None:
            continue
        m = re.match(r"bhi(?:\.[nw])?\s+([0-9a-f]+)", ins)
        if m:
            default = int(m.group(1), 16)
        if re.match(r"addls\s+pc, pc, r1, lsl #2", ins):

            def branch(va):
                w = int.from_bytes(at(va, 4), "little")
                off = ((w & 0xFFFFFF) ^ 0x800000) - 0x800000
                return va + 8 + 4 * off

            default = branch(a + 4)
            return {k: branch(a + 8 + 4 * k) for k in range(n + 1)}, default
        if re.match(r"tbb\s+\[pc, r1\]", ins):
            base = a + 4
            return {k: base + 2 * at(base + k, 1)[0] for k in range(n + 1)}, default
        if re.match(r"tbh\s+\[pc, r1, lsl #1\]", ins):
            base = a + 4
            return {
                k: base + 2 * int.from_bytes(at(base + 2 * k, 2), "little") for k in range(n + 1)
            }, default
        if re.match(r"ldr\.w\s+\w+, \[\w+, r1, lsl #2\]", ins):
            m = re.search(
                r"adr\s+\w+, ([0-9a-f]+)", " ".join(x for _, x in lines[max(0, i - 2) : i])
            )
            base = int(m.group(1), 16) if m else None
            if base is None:
                return None
            return {
                k: base + int.from_bytes(at(base + 4 * k, 4), "little", signed=True)
                for k in range(n + 1)
            }, default
    return None


def nyx_rows(fw, pak_order):
    """{check id: (present, source)} of nyx-cmd's OSInfo and DeviceInfo queries."""
    rows = {}
    for module, keys in (("OSInfo", tvsuite.NYX_OS_INFO), ("DeviceInfo", tvsuite.NYX_DEVICE_INFO)):
        plugin = fw.find(f"usr/lib/nyx/nyxcmd/libnyxcmdModule{module}.so")
        text = fw.read(*plugin) if plugin else b""
        mod = fw.find(f"usr/lib/nyx/modules/nyx{module}Main.module", pak_order)
        mdata = fw.read(*mod) if mod else b""
        table = jump_table(mdata, "device_info_query") if module == "DeviceInfo" and mdata else None
        # the answer for a query type the module does not implement: the case of the battery's two
        # (nyx-cmd's DeviceInfo answers "Query not implemented" for such a type)
        nyi = table[0].get(0) if table and table[0].get(0) == table[0].get(1) else None
        for idx, key in enumerate(keys):
            cid = f"nyx.{module}.{key}"
            if key.encode() not in text:
                if module == "DeviceInfo" and key in (
                    "batt_ch",
                    "batt_rsp",
                    "last_reset_type",
                    "manufacture_date",
                    "msn",
                    "uptime",
                ):
                    continue  # never a query of nyx-cmd's
                if plugin:
                    rows[cid] = (False, f"rootfs.pak:/{plugin[1]} has no query {key}")
                continue
            if not mod:
                rows[cid] = (True, f"rootfs.pak:/{plugin[1]} (no nyx{module}Main.module found)")
                continue
            src = f"{mod[0]}:/{mod[1]}"
            if nyi is not None and table[0].get(idx) == nyi:
                rows[cid] = (
                    False,
                    f"{src}: device_info_query's case NYX_DEVICE_INFO_{key.upper()} ({idx}) is "
                    f"that of BATT_CH and BATT_RSP (0, 1; a TV has no battery): not implemented",
                )
            elif (
                module == "DeviceInfo"
                and key == "bt_addr"
                and b"hcitool" in mdata
                and not any(
                    fw.find(p) for p in ("usr/bin/hcitool", "usr/sbin/hcitool", "bin/hcitool")
                )
            ):
                rows[cid] = (False, f"{src} runs hcitool, which the firmware does not have")
            else:
                rows[cid] = (True, src + (f" (case {idx})" if table else ""))
    return rows


def kadp_teardown(fw, pak_order):
    """The lines MStar KADP prints when a process ends, where its own .fini_array runs
    MsOS_MPool_Close (which closes the /dev/semutex fd) before MDrv_MMIO_Close, whose take of
    _MMIO_Mutex then fails in userspace and is printed (W16R): (lines, source), or None. Lines
    and order from the library's code: .fini_array (run last to first), the calls of each entry,
    MDrv_MMIO_Close's puts/printf and their constant arguments, MDrv_MMIO_Init's mutex name, and
    MsOS_ObtainMutex's and MsOS_ReleaseMutex's "Mutex Name: %s"."""
    from elftools.elf.elffile import ELFFile

    loc = None
    for pak in pak_order + [p for p in fw.paks if p not in pak_order]:
        cands = [
            f
            for f, t in fw.files(pak).items()
            if t is None and re.search(r"/libkadaptor\.so[.0-9]*$", f)
        ]
        if cands:
            loc = (pak, sorted(cands)[0])
            break
    if not loc:
        return None
    code = Code(fw.read(*loc))
    close, init, mpool = (
        code.symbol(n) for n in ("MDrv_MMIO_Close", "MDrv_MMIO_Init", "MsOS_MPool_Close")
    )
    if not (close and init and mpool):
        return None
    sec = ELFFile(io.BytesIO(code.data)).get_section_by_name(".fini_array")
    if sec is None:
        return None
    entries = [int.from_bytes(sec.data()[i : i + 4], "little") for i in range(0, sec.data_size, 4)]
    first, starts = {}, sorted({e & ~1 for e in entries})
    for i, e in enumerate(entries):
        end = next((x for x in starts if x > (e & ~1)), (e & ~1) + 0x20)
        for _, fn, _ in code.calls(e & ~1, min(end, (e & ~1) + 0x20))[:1]:
            first.setdefault(fn, i)
    if not (
        "MsOS_MPool_Close" in first
        and "MDrv_MMIO_Close" in first
        and first["MsOS_MPool_Close"] > first["MDrv_MMIO_Close"]
    ):
        return None
    lines = []
    for _, fn, regs in code.calls(close[0], close[0] + close[1]):
        r = {k: (code.cstr(v) if isinstance(v, int) and v > 0xFFFF else v) for k, v in regs.items()}
        if fn == "puts" and isinstance(r.get("r0"), str):
            lines.append(r["r0"])
        elif fn == "printf" and isinstance(r.get("r0"), str) and "Mutex taking timeout" in r["r0"]:
            try:
                lines.append(r["r0"] % (r["r1"], r["r2"], r["r3"]))
            except (TypeError, KeyError):
                return None
    name = next(
        (
            code.cstr(regs["r1"])
            for _, fn, regs in code.calls(init[0], init[0] + init[1])
            if fn == "MsOS_CreateMutex" and isinstance(regs.get("r1"), int)
        ),
        None,
    )
    if (
        not any("Mutex taking timeout" in x for x in lines)
        or not name
        or b"Mutex Name: %s" not in code.data
    ):
        return None
    lines = [x.strip() for x in lines] + [f"Mutex Name: {name}"]
    order = ", ".join(
        f"{i}: {fn}"
        for fn, i in sorted(first.items(), key=lambda x: -x[1])
        if fn in ("MsOS_MPool_Close", "MDrv_MMIO_Close")
    )
    return lines, (
        f"{loc[0]}:/{loc[1]}: .fini_array runs ({order}) MsOS_MPool_Close, which closes the "
        f"/dev/semutex fd, before MDrv_MMIO_Close ({close[0]:#x}), whose take of {name} then fails "
        f"in userspace and is printed: the firmware's own, at every KADP process's exit"
    )


TEE_DEV = b"/dev/tee"  # OP-TEE's client devices, /dev/tee%zu and /dev/teepriv%zu
# tee-supplicant's TAs, <uuid>.ta: Realtek's firmware keeps them in usr/lib, MStar's in lib
TA_DIRS = ("usr/lib/optee_armtz", "lib/optee_armtz")
TEE_INIT = "optee-init.sh"  # the firmware's own start of its TEE (MStar's: chmod /dev/tee0, tee-supplicant)
EXEC_DIRS = ("bin/", "sbin/", "lib/", "usr/bin/", "usr/sbin/", "usr/lib/", "usr/libexec/", "bsp/")


def tee_init_of(fw, pak_order):
    """["<pak>:/<path>"] of the firmware's own TEE start scripts (TEE_INIT) that open /dev/tee0: the
    firmware expects its kernel to make the node."""
    import tempfile

    found = []
    for pak in pak_order:
        files = [f for f in fw.files(pak) if os.path.basename(f) == TEE_INIT]
        if not files:
            continue
        with tempfile.TemporaryDirectory() as d:
            lst = os.path.join(d, "list")
            with open(lst, "w") as f:
                f.write("".join(x + "\n" for x in files))
            subprocess.run(
                [
                    "unsquashfs",
                    "-q",
                    "-n",
                    "-f",
                    "-d",
                    os.path.join(d, "x"),
                    "-o",
                    str(fw.source(pak)[1]),
                    "-ef",
                    lst,
                    fw.source(pak)[0],
                ],
                capture_output=True,
            )
            for f in files:
                try:
                    with open(os.path.join(d, "x", f), "rb") as h:
                        if b"/dev/tee0" in h.read():
                            found.append(f"{pak}:/{f}")
                except OSError:
                    continue
    return found


def tee_of(fw, pak_order, target):
    """The TEE's clients in the firmware (the tee step): every ELF file of the paks in pak_order under
    EXEC_DIRS whose bytes name /dev/tee (libteec's probe of /dev/tee%zu, tee-supplicant's
    /dev/teepriv%zu), and the TAs tee-supplicant would load (TA_DIRS' <uuid>.ta). None found is the
    evidence for the step's SKIP."""
    import tempfile

    clients, tas, scanned = [], [], 0
    for pak in pak_order:
        files = [f for f, t in fw.files(pak).items() if t is None and f.startswith(EXEC_DIRS)]
        tas += [
            os.path.basename(f)[:-3]
            for f in files
            if f.startswith(tuple(d + "/" for d in TA_DIRS)) and f.endswith(".ta")
        ]
        if not files:
            continue
        with tempfile.TemporaryDirectory() as d:
            lst = os.path.join(d, "list")
            with open(lst, "w") as f:
                f.write("".join(x + "\n" for x in files))
            subprocess.run(
                [
                    "unsquashfs",
                    "-q",
                    "-n",
                    "-f",
                    "-d",
                    os.path.join(d, "x"),
                    "-o",
                    str(fw.source(pak)[1]),
                    "-ef",
                    lst,
                    fw.source(pak)[0],
                ],
                capture_output=True,
            )
            for f in files:
                try:
                    with open(os.path.join(d, "x", f), "rb") as h:
                        data = h.read()
                except OSError:
                    continue
                if data[:4] != b"\x7fELF":
                    continue
                scanned += 1
                if TEE_DEV in data:
                    clients.append(f"{pak}:/{f}")
    where = ", ".join(pak_order)
    src = (
        f"ELF files naming {TEE_DEV.decode()}: {', '.join(clients)}"
        if clients
        else f"no TEE client: none of the {scanned} ELF files under {', '.join(EXEC_DIRS)} of {where} names "
        f"{TEE_DEV.decode()}"
    )
    # whether the VM is expected to have the node, and TAs behind it: the chip's module with tee=1
    # (rtk.ko's stand-in TAs), else lgemu.ko's tee=1 with none where the rootfs has OP-TEE's client
    # (targets.tee_device, the launcher's rule)
    module = targets.tee_module(targets.by_name(target))
    bare = not module and targets.TEE_CLIENT in fw.files("rootfs.pak")
    init = tee_init_of(fw, pak_order)
    if module:
        node = f"{module}.ko makes /dev/tee0 (tee=1 in the chip's guest/targets/soc row)"
    elif bare:
        node = (
            f"lgemu.ko makes /dev/tee0 with no TA (tee=1: the rootfs has /{targets.TEE_CLIENT} and "
            "no module of the chip's guest/targets/soc row has tee=1)"
        )
    else:
        node = "no module of the chip's guest/targets/soc row has tee=1, so the VM has no /dev/tee0"
    if init:
        node += f"; the firmware's own {', '.join(init)} opens /dev/tee0"
    row = dict(
        clients=clients,
        tas=sorted(set(tas)),
        source=src,
        node=bool(module or bare),
        node_source=node,
    )
    if bare:
        row["node_tas"] = False
    return row


AUDIO_METHODS = ("getVolume", "setMuted", "volumeUp", "volumeDown")
# Chromium's libcbe.so: in usr/lib, from webOS 12 (spacebuns) in the com.palm.webruntime package
CBE = ("usr/lib/libcbe.so", "usr/palm/services/com.palm.webruntime/lib/libcbe.so")
WEB_ENGINES = CBE + ("usr/lib/libQt5WebKit.so.5",)
WEBAUDIO = b"AudioContext"


def audio_of(fw, provs, tvpak):
    """The audio step's row: the Audio API (com.webos.audio's getVolume, setMuted, volumeUp and
    volumeDown, registered or named by its executables), the native player it plays through
    (usr/bin/paplay, PulseAudio's), and WebAudio in the web engine (its library names
    AudioContext). Absent ones are cited."""
    svc = tvsuite.AUDIO_SERVICE.split("//", 1)[1]
    locs, src = provider(fw, provs, svc, tvpak)
    if not locs:
        api = dict(present=False, source=src)
    else:
        bins = list(dict((b[0], b) for pak, path in locs for b in binaries(fw, pak, path)).values())
        reach = registered(fw, bins, svc, src)
        missing = [
            m
            for m in AUDIO_METHODS
            if not (reach and m in reach[1].get("/", set())) and not has_string(bins, m)
        ]
        player = fw.find("usr/bin/paplay")
        if missing:
            api = dict(present=False, source=f"{svc}: no {', '.join(missing)} in {labels(bins)}")
        elif not player:
            api = dict(
                present=False, source="no usr/bin/paplay (PulseAudio's player) in the firmware"
            )
        else:
            api = dict(
                present=True,
                source=f"{svc} by {src}; {', '.join(AUDIO_METHODS)} "
                + (f"in the / table {reach[2]} registers" if reach else f"in {labels(bins)}")
                + f"; player {player[0]}:/{player[1]}",
            )
    engine = next((fw.find(e) for e in WEB_ENGINES if fw.find(e)), None)
    if not engine:
        web = dict(present=False, source=f"no web engine ({', '.join(WEB_ENGINES)})")
    elif WEBAUDIO in fw.read(*engine):
        web = dict(present=True, source=f"{engine[0]}:/{engine[1]} names {WEBAUDIO.decode()}")
        web.update(webaudio_level(fw, engine))
    else:
        web = dict(
            present=False, source=f"{engine[0]}:/{engine[1]} does not name {WEBAUDIO.decode()}"
        )
    # the read-back: getVolume of the Audio API, or, where audiod has a master category (webOS 5
    # on: its root getVolume answers the scenario's volume, without the mute), master/getVolume
    perms = "usr/share/luna-service2/api-permissions.d/com.webos.service.audio.api.json"
    loc = fw.find(perms)
    master = bool(loc) and b"master/getVolume" in fw.read(*loc)
    readback = dict(
        uri=tvsuite.AUDIO_MASTER if master else tvsuite.AUDIO_SERVICE + "/getVolume",
        source=(
            f"rootfs.pak:/{perms} lists master/getVolume"
            if master
            else f"no master/getVolume in {'rootfs.pak:/' + perms if loc else 'the firmware (no ' + perms + ')'}"
        ),
    )
    aplay, alsa_hw = alsa_of(fw)
    return dict(
        service=tvsuite.AUDIO_SERVICE,
        api=api,
        webaudio=web,
        readback=readback,
        aplay=aplay,
        alsa_hw=alsa_hw,
    )


ASOUND_CONF = "etc/asound.conf"
# Chromium 68 and 79's alsa_output.cc strings: its default device (kDefaultDevice) is the string
# just before this one; W20L's is "default", W21P's LG's "feedback"
ALSA_OUTPUT_ANCHOR = b"\0Failed querying delay: "
PALM_POLICY = "usr/lib/pulse-*/modules/module-palm-policy.so"
# audiod's policy volume of a PulseAudio virtual sink (0..100, the row of palm-policy's dB table),
# sent at its start ("volume command received, sink is N, requested volume is L"): compiled into
# audiod, not located in it; read from W20L's and W21P's boot logs (pfeedback 80, pdefaultapp 100)
# and matched by a webOS 6 set's WebAudio sink-input on pfeedback (48063, -8.08 dB)
POLICY_LEVEL = {"pfeedback": 80}


def palm_policy_db(fw, level):
    """(dB, source) of palm-policy's level: its table of 101 floats (level 0, -200 = mute, to 100,
    0 dB), or None where the firmware has no such module or table."""
    import struct

    for pak in fw.paks:
        for path in fw.glob(PALM_POLICY, pak):
            data = fw.read(pak, path)
            i = data.find(struct.pack("<f", -200.0))
            while i >= 0:
                t = struct.unpack_from("<101f", data, i) if i + 404 <= len(data) else ()
                if t and t[100] == 0.0 and all(a < b for a, b in zip(t, t[1:])):
                    return round(
                        t[level], 2
                    ), f"{pak}:/{path} dB table at file offset {i:#x}, level {level}"
                i = data.find(struct.pack("<f", -200.0), i + 1)
    return None


def webaudio_level(fw, engine):
    """The WebAudio stream's level against paplay's (pcm_output, 0 dB): the ALSA device the
    engine's ALSA output opens by default (LG's asound.conf's pulse device for it), at audiod's policy
    level of that sink in palm-policy's table; 0 dB where the engine opens ALSA's default."""
    data = fw.read(*engine)
    i = data.find(ALSA_OUTPUT_ANCHOR)
    dev = None
    if i > 0:
        j = i
        while j > 0 and data[j - 1 : j] == b"\0":
            j -= 1
        k = data.rfind(b"\0", 0, j) + 1
        if data[k:j] in (b"default", b"feedback"):
            dev = data[k:j].decode()
    if dev in (None, "default"):
        why = (
            f"{engine[0]}:/{engine[1]}'s ALSA output opens default"
            if dev
            else f"{engine[0]}:/{engine[1]} names no LG device for its ALSA output"
        )
        return dict(level_db=0.0, level_source=f"{why} (pdefaultapp, at 0 dB as pcm_output)")
    loc = fw.find(ASOUND_CONF)
    text = fw.read(*loc).decode("latin-1") if loc else ""
    m = re.search(r"^\s*pcm\.%s\s*\{([^}]*)\}" % re.escape(dev), text, re.M)
    sink = m and re.search(r"^\s*device\s+(\S+)", m.group(1), re.M)
    sink = sink and sink.group(1)
    level = POLICY_LEVEL.get(sink, 100)
    db = palm_policy_db(fw, level)
    if not sink or not db:
        return dict(
            level_db=0.0,
            level_source=f"{engine[0]}:/{engine[1]}'s ALSA output opens {dev}, "
            f"but {'no pulse device for it in ' + ASOUND_CONF if not sink else 'no palm-policy table'}",
        )
    return dict(
        level_db=db[0],
        level_source=f"{engine[0]}:/{engine[1]}'s ALSA output opens {dev}; "
        f"{loc[0]}:/{ASOUND_CONF}: pcm.{dev} is pulse {sink}; audiod's policy "
        f"level of {sink} {level}: {db[0]:+.2f} dB ({db[1]})",
    )


SYSTEM_PA = "etc/pulse/system.pa"


def alsa_of(fw):
    """The audio step's ALSA rows: aplay (alsa-utils') to ALSA's default where the firmware's
    asound.conf makes pcm.!default the pulse plugin, and aplay to the PCM of PulseAudio's
    pcm_output (system.pa's module-alsa-sink device=hw:C,D), absent ones cited."""
    player = fw.find("usr/bin/aplay")
    if not player:
        why = "no usr/bin/aplay (alsa-utils' player) in the firmware"
        return dict(present=False, source=why), dict(present=False, source=why)
    who = f"player {player[0]}:/{player[1]}"
    loc = fw.find(ASOUND_CONF)
    text = fw.read(*loc).decode("latin-1") if loc else ""
    m = re.search(r"^\s*pcm\.!default\s*\{([^}]*)\}", text, re.M)
    if not loc:
        aplay = dict(
            present=False, source=f"no {ASOUND_CONF}: ALSA's default is not the pulse plugin"
        )
    elif not m or not re.search(r"^\s*type\s+pulse\b", m.group(1), re.M):
        aplay = dict(
            present=False, source=f"{loc[0]}:/{ASOUND_CONF}: pcm.!default is not of type pulse"
        )
    else:
        dev = re.search(r"^\s*device\s+(\S+)", m.group(1), re.M)
        aplay = dict(
            present=True,
            source=f"{loc[0]}:/{ASOUND_CONF}: pcm.!default type pulse"
            + (f", device {dev.group(1)}" if dev else "")
            + f"; {who}",
        )
    loc = fw.find(SYSTEM_PA)
    text = fw.read(*loc).decode("latin-1") if loc else ""
    hw = None
    for line in text.splitlines():
        if re.match(r"\s*load-module\s+module-alsa-sink\b", line) and re.search(
            r"\bsink_name=pcm_output\b", line
        ):
            d = re.search(r"\bdevice=(hw:\d+,\d+)\b", line)
            hw = d and d.group(1)
            break
    if not hw:
        alsa_hw = dict(
            present=False,
            source=(
                f"{loc[0]}:/{SYSTEM_PA} loads no module-alsa-sink pcm_output on a hw:C,D"
                if loc
                else f"no {SYSTEM_PA}"
            ),
        )
    else:
        alsa_hw = dict(
            present=True,
            device=hw,
            source=f"{loc[0]}:/{SYSTEM_PA}: pcm_output is module-alsa-sink device={hw}; {who}",
        )
    return aplay, alsa_hw


# The decode step (tvsuite.DECODE_CASES): what the firmware's media stack claims for each codec,
# from the elements the stock pipeline builds: gst-omx's codec sections in /etc/xdg/gstomx.conf (MStar, Realtek), else LG's decoder
# bin's sink pad caps (vdecsink on h13/m14, omx_lxvideodec on m16p, lxvideodec on o22) and its
# audio decoder's (adecsink, audio_dec, lgaudiodec on o22); where umediaserver has a codec
# capability table, it must list the codec too. From webOS 8 on, Realtek's sections carry the
# decoder instance (omxh264dec1). AV1 is keyed by the host's decoder for it, libdav1d.
GSTOMX_CONF = "etc/xdg/gstomx.conf"
CODEC_JSON = "etc/umediaserver/device_codec_capability_config.json"
LG_VIDEO_DEC = (
    "mnt/lg/res/lglib/gstreamer-1.0/libgstlxvdecbin-1.0.so",
    "usr/lib/gstreamer-1.0/libgstlxvdecbin-1.0.so",
    "usr/lib/gstreamer-1.0/libgstlxvideodec-1.0.so",
    "usr/lib/gstreamer-1.0/libgstlxvideodec.so",
)
LG_AUDIO_DEC = (
    "mnt/lg/res/lglib/gstreamer-1.0/libgstadecsink-1.0.so",
    "usr/lib/gstreamer-1.0/libgstadecsink-1.0.so",
    "usr/lib/gstreamer-1.0/libgstaudiodec-1.0.so",
    "usr/lib/gstreamer-1.0/libgstlgaudiodec.so",
)
# codec: (gst-omx sections, the caps media type in LG's pad template, umediaserver's names)
DECODE_CODECS = {
    "h264": (("omxh264dec",), b"video/x-h264", ("H.264",)),
    "hevc": (("omxhevcdec", "omxh265dec"), b"video/x-h265", ("HEVC", "H.265")),
    "vp9": (("omxvp9dec",), b"video/x-vp9", ("VP9",)),
    "mpeg2video": (("omxmpeg2videodec", "omxmpegvideodec"), b"video/mpeg", ("MPEG2",)),
    "aac": (("omxaacdec",), b"audio/mpeg", ("AAC",)),
    "libdav1d": (("omxav1dec",), b"video/x-av1", ("AV1",)),
    "opus": (("omxopusdec",), b"audio/x-opus", ("OPUS", "Opus")),
}
AUDIO_CODECS = {"aac", "opus"}
DEMUX = {
    "file.h264": "libgstisomp4.so",
    "file.hevc": "libgstisomp4.so",
    "file.vp9": "libgstmatroska.so",
    "file.mpeg2": "libgstmpegtsdemux.so",
    "file.vp9-opus": "libgstmatroska.so",
    "file.av1": "libgstmatroska.so",
}


def codec_claims(fw):
    """{codec: (claimed, source)} of DECODE_CODECS from the firmware's decoder elements and
    umediaserver's capability table."""
    out = {}
    conf = fw.find(GSTOMX_CONF)
    sections = (
        set(re.findall(r"^\[(\S+)\]", fw.read(*conf).decode("latin-1"), re.M)) if conf else set()
    )
    loc = fw.find(CODEC_JSON)
    names = None
    if loc:
        try:
            d = json.loads(fw.read(*loc))
            names = {c.get("name") for k in ("videoCodecs", "audioCodecs") for c in d.get(k) or []}
        except ValueError:
            names = None
    for codec, (secs, caps, json_names) in DECODE_CODECS.items():
        audio = codec in AUDIO_CODECS
        if conf:
            hit = sorted(
                x for x in sections if any(re.fullmatch(re.escape(n) + r"\d*", x) for n in secs)
            )
            ok, src = (
                bool(hit),
                (
                    f"{conf[0]}:/{GSTOMX_CONF} [{hit[0]}]"
                    if hit
                    else f"no [{'] or ['.join(secs)}] in {conf[0]}:/{GSTOMX_CONF}"
                ),
            )
        else:
            lib = next(
                (fw.find(x) for x in (LG_AUDIO_DEC if audio else LG_VIDEO_DEC) if fw.find(x)), None
            )
            if not lib:
                ok, src = (
                    False,
                    "no decoder element (gstomx.conf, "
                    + ", ".join(LG_AUDIO_DEC if audio else LG_VIDEO_DEC)
                    + ")",
                )
            else:
                data = fw.read(*lib)
                # the pad template's media types: ";"-separated, ", field" after the type
                i = data.find(caps + b";")
                i = i if i >= 0 else data.find(caps + b",")
                ok = i >= 0
                src = (
                    f"{lib[0]}:/{lib[1]} pad caps name {caps.decode()} at {i:#x}"
                    if ok
                    else f"{lib[0]}:/{lib[1]}: no {caps.decode()} in its pad caps"
                )
        if ok and names is not None:
            hit = [n for n in json_names if n in names]
            ok = bool(hit)
            src += (
                f"; {loc[0]}:/{CODEC_JSON} lists {hit[0]}"
                if hit
                else f"; but {loc[0]}:/{CODEC_JSON} lists no {' or '.join(json_names)}"
            )
        out[codec] = (ok, src)
    return out


# MSE in the web engine (WEB_ENGINES): its JavaScript API (Chromium's and W15M's MediaSource,
# WT1H's prefixed WebKitMediaSource; addSourceBuffer either way), and for each source buffer
# (MSE_BUFFERS: the MIME type and codec string tests/regress/video/make-clips.sh --decode gives the
# page) whether the engine takes it. Chromium (CBE) decides that in media::StreamParserFactory: its
# table of SupportedTypeInfo {type, parser factory, CodecInfo list}, each CodecInfo's first member
# the codec pattern base::MatchPattern compares the codec string with; a type it does not list
# (webOS 4.5 has no audio/webm) or a codec none of its patterns matches is refused by
# isTypeSupported and addSourceBuffer (mse_parsers); so is AV1 where the engine has it only for an
# app that opts in (av1_opt_in). QtWebKit has no such table: there the
# container's MIME type and the codec's name (MSE_STRINGS) must be strings of the engine, and the
# AAC buffers are not looked up (every engine with MSE takes audio/mp4 AAC).
MSE_API = (b"addSourceBuffer",)
MSE_BUFFERS = {
    "mse.h264": (("video/mp4", "avc1.4d401f"), ("audio/mp4", "mp4a.40.2")),
    "mse.hevc": (("video/mp4", "hvc1.1.6.L93.B0"), ("audio/mp4", "mp4a.40.2")),
    "mse.vp9": (("video/webm", "vp9"),),
    "mse.vp9-opus": (("video/webm", "vp9"), ("audio/webm", "opus")),
    "mse.av1": (("video/webm", "av01.0.04M.08"), ("audio/webm", "opus")),
}
MSE_STRINGS = {
    "avc1.4d401f": (b"avc1",),
    "hvc1.1.6.L93.B0": (b"hvc1", b"hev1"),
    "vp9": (b"vp09", b"vp9"),
    "opus": (b"opus",),
    "av01.0.04M.08": (b"av01",),
}


def mse_parsers(data):
    """{MIME type: [codec pattern (None: the type's implied codec)]} of the audio/ and video/ mp4
    and webm entries of Chromium's StreamParserFactory table in an engine binary, None where it has
    none. An entry is a pointer to the type's string, then one into code (the parser factory) and
    one to a null-terminated list of CodecInfo pointers, each CodecInfo's first word its pattern;
    from webOS 24 (Chromium 120) the type is a string view (its length after the pointer) and the
    list a span (its count after the pointer, no null)."""
    from elftools.elf.elffile import ELFFile

    try:
        segs = [
            (g["p_vaddr"], g["p_offset"], g["p_filesz"], g["p_flags"])
            for g in ELFFile(io.BytesIO(data)).iter_segments()
            if g["p_type"] == "PT_LOAD"
        ]
    except Exception:
        return None
    code = Code(data)

    def va_of(off):
        return next((v + off - o for v, o, n, _ in segs if o <= off < o + n), None)

    def in_code(va):
        return any(v <= (va & ~1) < v + n for v, _, n, f in segs if f & 1)

    def codecs(at, count=None):
        out = []
        for i in range(count or 64):
            info = code.word(at + 4 * i)
            if info is None:
                return None
            if info == 0 and count is None:
                return out or None
            pattern = code.word(info)
            name = code.cstr(pattern) if pattern else None
            if pattern is None or (pattern and not re.fullmatch(r"[\w.*?-]+", name or "")):
                return None
            out.append(name)
        return out if count else None

    table = {}
    for m in re.finditer(rb"(?<=\0)(?:audio|video)/(?:mp4|webm)(?=\0)", data):
        va = va_of(m.start())
        if va is None:
            continue
        for r in re.finditer(re.escape(va.to_bytes(4, "little")), data):
            at = va_of(r.start()) if r.start() % 4 == 0 else None
            if at is None:
                continue
            view = code.word(at + 4) == len(m.group())  # {pointer, length}
            if view:
                at += 4
                count = code.word(at + 12)
                if not count or count > 64:
                    continue
            if not in_code(code.word(at + 4) or 0):
                continue
            c = codecs(code.word(at + 8) or 0, count if view else None)
            if c is not None:
                table.setdefault(m.group().decode(), c)
    return table or None


# AV1 behind LG's MediaExtension (WebAppMgr hands the renderer appinfo.json's "mediaExtension";
# its parser takes "mse": {"enableAV1": ...}). In webOS 5 (Chromium 68) the switch it sets is a
# flag in .bss, so 0, that mime_util's av01 parsing and the stream parser's AV1 entry both test
# first: AV1 is refused (canPlayType "", isTypeSupported false) unless the app opts in. The
# parser logs it as AV1_OPT_IN. From webOS 6 the switch is MediaPlatformPrefs's
# is_av1_decoder_enabled_ (logged as AV1_PREF), which its constructor sets: on unless an app
# turns it off. tests/regress/tvapp (TVAPP_INFO) sets no mediaExtension.
AV1_OPT_IN = b"mse_enable_av1=%d"
AV1_PREF = b" : is_av1_decoder_enabled_="
TVAPP_INFO = os.path.join(HERE, "tvapp", "appinfo.json")


def av1_opt_in(data):
    """None, or why a page of the test app gets no AV1 from the engine (data): the engine has
    webOS 5's opt-in AV1 switch and the test app does not opt in."""
    if AV1_OPT_IN not in data or AV1_PREF in data:
        return None
    with open(TVAPP_INFO) as f:
        ext = json.load(f).get("mediaExtension") or {}
    if (ext.get("mse") or {}).get("enableAV1"):
        return None
    return (
        "AV1 only for an app that opts in (LG MediaExtension mse.enableAV1, logged as "
        f"{AV1_OPT_IN.decode()}; off by default), and {os.path.relpath(TVAPP_INFO, TOP)} does not"
    )


def mse_of(fw):
    """{case: (claimed, source)} of the MSE cases: the engine names the MSE API and takes each
    source buffer's type and codec (Chromium: mse_parsers; QtWebKit: names the type and codec)."""
    engine = next((fw.find(e) for e in WEB_ENGINES if fw.find(e)), None)
    if not engine:
        return {c: (False, f"no web engine ({', '.join(WEB_ENGINES)})") for c in MSE_BUFFERS}
    data = fw.read(*engine)
    who = f"{engine[0]}:/{engine[1]}"
    api = next((x for x in (b"WebKitMediaSource", b"MediaSource") if x in data), None)
    if not api or not all(x in data for x in MSE_API):
        why = f"{who} names no MSE API (MediaSource and addSourceBuffer)"
        return {c: (False, why) for c in MSE_BUFFERS}
    parsers = None
    if engine[1] in CBE:
        parsers = mse_parsers(data)
        if parsers is None:
            raise SystemExit(f"{who}: no media::StreamParserFactory table found (mse_parsers)")
    av1_off = av1_opt_in(data)
    out = {}
    for case, buffers in MSE_BUFFERS.items():
        found, why = [api.decode()], None
        for mime, codec in buffers:
            if parsers is not None:
                patterns = parsers.get(mime)
                hit = next((p for p in patterns or () if p and fnmatch.fnmatchcase(codec, p)), None)
                if patterns is None:
                    why = f"{who}: its MSE stream parsers (StreamParserFactory) take no {mime}"
                elif not hit:
                    why = (
                        f"{who}: its MSE stream parser for {mime} takes no {codec} "
                        f"(only {', '.join(p for p in patterns if p)})"
                    )
                elif codec.startswith("av01") and av1_off:
                    why = f"{who}: {av1_off}"
                else:
                    found.append(f"{mime} {codec} ({hit})")
            elif mime != "audio/mp4":
                strings = MSE_STRINGS[codec]
                hit = next((c for c in strings if c in data), None)
                if mime.encode() not in data:
                    why = f"{who} names no {mime}"
                elif not hit:
                    why = f"{who} names no {' or '.join(c.decode() for c in strings)} codec string"
                else:
                    found += [mime, hit.decode()]
            if why:
                break
        if parsers is not None:
            found[0] += " (StreamParserFactory)"
        out[case] = (False, why) if why else (True, f"{who}: {', '.join(found)}")
    return out


# MSE without an audio track in the engine's own renderer: Chromium 38's TvRenderer (W16R's
# libcbe.so) pushes the pipeline's end of stream (MediaAPIsWrapper::PushEOS), and reports the end,
# only once ReceivedEndOfStream finds the audio stream's end flag, which AudioBufferReady sets on
# the audio stream's last buffer and nothing sets without an audio stream; the EL stream's flag
# beside it is checked only if there is one. So such a stream plays to its last frame and the
# page gets "waiting", not "ended".
TV_RENDERER_EOS = "_ZN5media10TvRenderer19ReceivedEndOfStreamEv"
TV_RENDERER_AUDIO = "_ZN5media10TvRenderer16AudioBufferReadyENS_13DecoderStreamILNS_13DemuxerStream4TypeE1EE6StatusERK13scoped_refptrINS_11AudioBufferEE"


def mse_audio_eos_of(fw):
    """None, or the source when the web engine ends an MSE stream only after its audio stream's end
    of stream (TV_RENDERER_EOS: a load of the flag AudioBufferReady sets, then a cbz to the
    function's return of 0, with no test for an audio stream before it)."""
    loc = next((fw.find(e) for e in CBE if fw.find(e)), None)
    if not loc:
        return None
    code = Code(fw.read(*loc))
    eos, audio = code.symbol(TV_RENDERER_EOS), code.symbol(TV_RENDERER_AUDIO)
    if not eos or not audio:
        return None
    flags = set(
        re.findall(
            r"strb(?:\.w)?\s+r\d+, \[r\d+, #(\d+)\]",
            code.disassemble(audio[0], audio[0] + audio[1]),
        )
    )
    lines = code.disassemble(eos[0], eos[0] + eos[1]).splitlines()
    for i, line in enumerate(lines):
        m = re.search(r"ldrb(?:\.w)?\s+r\d+, \[r\d+, #(\d+)\]", line)
        if m and m.group(1) in flags and i + 1 < len(lines) and re.search(r"\scbz\s", lines[i + 1]):
            return (
                f"{loc[0]}:/{loc[1]}: media::TvRenderer::ReceivedEndOfStream @{eos[0]:#x} pushes the end of "
                f"stream only after the audio stream's ([this+{int(m.group(1)):#x}], set by AudioBufferReady "
                f"@{audio[0]:#x}), which a stream without audio never has"
            )
    return None


def decode_of(fw, mse):
    """The decode step's row: {case: {present, source}} per tvsuite.DECODE_CASES, absent where the
    container's demuxer, the video or audio decoder's claim, or (MSE) the engine's is missing; mse is
    mse_of's claims."""
    claims = codec_claims(fw)
    audio_eos = mse_audio_eos_of(fw)
    row = {}
    for case, path, vc, ac in tvsuite.DECODE_CASES:
        why, src = [], []
        if path == "file":
            demux = fw.find("usr/lib/gstreamer-1.0/" + DEMUX[case])
            (src if demux else why).append(
                f"{demux[0]}:/{demux[1]}" if demux else f"no usr/lib/gstreamer-1.0/{DEMUX[case]}"
            )
        else:
            ok, s = mse.get(case, (False, "no MSE evidence"))
            (src if ok else why).append(s)
        for c in (vc, ac):
            if c:
                ok, s = claims[c]
                (src if ok else why).append(s)
        row[case] = dict(present=not why, source="; ".join(why or src))
        if path == "mse" and not ac and audio_eos:
            # the page plays it to its end and gets no "ended" (nor does the host the stream's end)
            row[case]["ended"] = dict(expected=False, source=audio_eos)
    return row


def exports(data):
    """The names an ELF's .dynsym defines (not its undefined imports), None if it cannot be read."""
    try:
        from elftools.elf.elffile import ELFFile

        sym = ELFFile(io.BytesIO(data)).get_section_by_name(".dynsym")
        return (
            {s.name for s in sym.iter_symbols() if s.name and s["st_shndx"] != "SHN_UNDEF"}
            if sym
            else set()
        )
    except Exception:
        return None


def smp_of(fw):
    """The smp step's row: whether the firmware's StarfishMediaAPIs library (the one
    tests/regress/smp/smpcheck.c dlopens) exports a name of each of the app's SYM() lookups."""
    lib, needs = tvsuite.smp_needs()
    loc = fw.find(f"usr/lib/{lib}") or fw.find(f"lib/{lib}")
    if not loc:
        return dict(present=False, source=f"no usr/lib/{lib} or lib/{lib} in {', '.join(fw.paks)}")
    label = f"{loc[0]}:/{loc[1]}"
    defined = exports(fw.read(*loc))
    if defined is None:
        return dict(present=True, source=f"{label}: its dynamic symbols could not be read")
    missing = tvsuite.smp_missing(defined, needs)
    if missing:
        return dict(
            present=False, missing=missing, source=f"{label} exports no {', '.join(missing)}"
        )
    return dict(
        present=True,
        source=f"{label} exports all {len(needs)} StarfishMediaAPIs symbols the app looks up",
    )


def release_of(fw):
    loc = fw.find("etc/starfish-release")
    text = fw.read(*loc).decode() if loc else ""
    m = re.search(r"^(\S+) release (\S+) \(([^)]*)\)", text, re.M)
    return dict(name=m.group(1), version=m.group(2), codename=m.group(3)) if m else {}


def online_of(fw):
    loc = fw.find("etc/connman/main.conf")
    text = fw.read(*loc).decode(errors="replace") if loc else ""
    url = re.search(r"^Ipv4StatusUrl=(\S+)", text, re.M)
    dns = re.search(r"^TakeDnsIpResolutionAsOnline=(\S+)", text, re.M)
    return dict(
        url=url.group(1) if url else "http://ipv4.connman.net/online/status.html",
        dns_as_online=bool(dns) and dns.group(1).lower() in ("true", "1", "yes"),
        source="rootfs.pak:/etc/connman/main.conf" if loc else "connmand's default",
    )


def process_of(fw, pak, app_dir, main):
    """{"name", "source"} of the process a native app's crash reports carry where sam's main is a
    launcher script of the app's that execs one binary of its own (the browser's run_webbrowser:
    `exec ${DEPLOY_DIR}/chrome ...`), else None (main itself is the process)."""
    if not main or "/" in main or ":" in main:
        return None
    text = fw.read(pak, f"{app_dir}/{main}").decode("utf-8", "replace")
    if not text.startswith("#!"):
        return None
    names = sorted(set(re.findall(r"^\s*exec\s+\S*/([\w.+-]+)\s", text, re.M)))
    if len(names) != 1:
        return None
    return dict(name=names[0], source=f"{pak}:/{app_dir}/{main}: exec .../{names[0]}")


def apps_of(fw, pak_order):
    """{role: row} of the apps step (tvsuite.APP_ROLES): the first of the role's app ids with a
    usr/palm/applications/<id>/appinfo.json in the target's paks (its title, type and main), webOS 1
    and 2's launcher (the compositor's StarfishLauncher QML, opened by HOME) for a Home without an
    app, or absent with the paths searched."""
    out = {}
    for role, ids, what in tvsuite.APP_ROLES:
        row = None
        for app_id in ids:
            path = f"usr/palm/applications/{app_id}/appinfo.json"
            loc = fw.find(path, pak_order)
            if not loc:
                continue
            try:
                info = json.loads(fw.read(*loc))
            except ValueError:
                info = {}
            row = dict(
                present=True,
                id=app_id,
                what=what,
                title=info.get("title"),
                type=info.get("type"),
                main=info.get("main"),
                source=f"{loc[0]}:/{loc[1]}",
            )
            proc = process_of(fw, loc[0], os.path.dirname(loc[1]), info.get("main"))
            if proc:
                row["process"] = proc
            row["headless"] = tvsuite.headless_of(row)
            if info.get("requiredEULA"):
                # sam's prelaunch stage: an app that needs terms the set's user has not agreed
                # to opens LG's terms screen instead, which returns to the app once agreed
                screen = fw.find(
                    f"usr/palm/applications/{tvsuite.TERMS_SCREEN}/appinfo.json", pak_order
                )
                row["eula"] = dict(
                    terms=info["requiredEULA"],
                    screen=tvsuite.TERMS_SCREEN if screen else None,
                    source=f"{loc[0]}:/{path}: requiredEULA {info['requiredEULA']}; "
                    + (
                        f"the terms screen {screen[0]}:/{screen[1]}"
                        if screen
                        else f"no {tvsuite.TERMS_SCREEN} in the paks"
                    ),
                )
            if role == "home" and info.get("nativeLifeCycleInterfaceVersion") == 2:
                # sam's native life cycle 2: closeByAppId asks the app, and the Home app (webOS 4.5
                # on) keeps its process; judge_app then wants it still up, same pid
                row["stays"] = dict(
                    source=f"{loc[0]}:/{path}: nativeLifeCycleInterfaceVersion 2, "
                    f"the Home role: Home stays up after closeByAppId"
                )
            break
        if row is None and role == "home":
            loc = fw.find(tvsuite.LAUNCHER_QML, pak_order)
            if loc:
                row = dict(
                    present=True,
                    id=None,
                    kind="key",
                    what="the launcher (StarfishLauncher, HOME)",
                    source=f"no Home app; the compositor's launcher {loc[0]}:/{loc[1]}",
                )
        if row is None:
            row = dict(
                present=False,
                what=what,
                source="no "
                + " or ".join(f"usr/palm/applications/{i}/appinfo.json" for i in ids)
                + f" in {', '.join(pak_order)}",
            )
        out[role] = row
    return out


# getSystemInfo's UHD where tvservicemock answers it (the lowlevelstorage releases, webOS 5 on):
# requestUHD reads configd's tv.hw.panelResolution, which the firmware's hw-option-gen writes from
# the board's line at boot (tvconfig-gen), and cb_requestUHD answers "true" for "UD" (K8HP's
# PmLog: "cb_requestUHD panelresolution:UD", then "UHD:true"). Panel values seen on our boards:
# UD, FHD, HD; any other one is left for a person to look at.
UHD_CONFIG = "tv.hw.panelResolution"
UHD_PANELS = {"UD": "true", "FHD": "false", "HD": "false"}


def uhd_of(target, fw_dir, bins):
    """{value, value_source} of sysinfo.UHD where its provider reads tv.hw.panelResolution for it
    (value None, with the reason, where the board's panel cannot be had); None where it does not
    (tvservice's releases: tvsuite.uhd_of_model)."""
    where = has_string(bins, "requestUHD") and has_string(bins, UHD_CONFIG)
    if not where:
        return None
    from beanjar import lls

    p = targets.by_name(target)
    line = targets.board_broadcast(p, fw_dir)
    board = f"the {line} board" if line else "the board"
    reads = f"{where} answers UHD from {UHD_CONFIG} (requestUHD; UD: true)"
    try:
        hw, _, _ = lls.tvconfig(fw_dir, p, [lls.board_cmdline(p, broadcast=line)])[0]
    except lls.Error as e:
        return dict(
            value=None, value_source=f"{reads}; the firmware's hw-option-gen did not run: {e}"
        )
    panel = (hw or {}).get("panelResolution")
    if panel not in UHD_PANELS:
        return dict(
            value=None,
            value_source=f"{reads}; the firmware's hw-option-gen gives panelResolution {panel!r} "
            f"for {board}, not one of {', '.join(UHD_PANELS)}",
        )
    return dict(
        value=UHD_PANELS[panel],
        value_source=f"{reads}; the firmware's hw-option-gen gives panelResolution {panel} for "
        f"{board}'s line (beanjar.lls.tvconfig)",
    )


def target_row(target, fw_name, fw_dir):
    fw = Firmware(fw_dir)
    p = targets.by_name(target)
    pak_files = [f + ".pak" if not f.endswith(".pak") else f for f in targets.pak_files(p, fw_dir)]
    pak_order = ["rootfs.pak"] + [f for f in pak_files if f in fw.paks and f != "rootfs.pak"]
    rel = release_of(fw)
    major = tvsuite.webos_major(rel["version"])
    tvpak = targets.TVSERVICE[0] + ".pak"
    tvpak = tvpak if tvpak in fw.paks else None
    provs = providers(fw)
    settings = settings_keys(fw)
    checks, hosts = {}, {}
    for cid, spec in tvsuite.catalogue().items():
        svc = tvsuite.SERVICES[spec["service"]]
        if svc not in hosts:
            locs, src = provider(fw, provs, svc, tvpak)
            hosts[svc] = (
                list(
                    dict((b[0], b) for pak, path in locs for b in binaries(fw, pak, path)).values()
                )
                if locs
                else None,
                src,
            )
        bins, src = hosts[svc]
        ref = tvsuite.reference(spec["api"], major)
        note = tvsuite.REFERENCE_NOTES.get(cid)
        cite = "; ".join(x for x in (ref, f"note: {note}" if note else None) if x)
        if bins is None:
            checks[cid] = dict(present=False, source=f"{src}" + (f"; {cite}" if cite else ""))
            continue
        method = spec["method"].rsplit("/", 1)[-1]
        category = "/" + spec["method"].rsplit("/", 1)[0] if "/" in spec["method"] else "/"
        # what the executables register under the service's name (a com.palm. name the hub maps:
        # its com.webos. namesake's), on either hub where there are two (tv.sh calls both)
        reach = registered(fw, bins, svc, src)
        if reach is not None:
            names, tables, where_from = reach
            if method not in tables.get(category, set()):
                checks[cid] = dict(
                    present=False,
                    source=f"{svc}/{spec['method']}: not in the method tables "
                    f"{where_from} registers for category {category} ({', '.join(sorted(tables)) or 'none'}"
                    f": {', '.join(sorted(tables.get(category, ())))})"
                    + (f"; {cite}" if cite else ""),
                )
                continue
        reg_src = f"{method!r} in the {category} table {reach[2]} registers" if reach else None
        where = has_string(bins, method)
        if not where:
            checks[cid] = dict(
                present=False,
                source=f"{svc}/{spec['method']}: no string {method!r} in "
                f"{labels(bins)}" + (f"; {cite}" if cite else ""),
            )
            continue
        key = spec.get("key")
        if spec["method"] == "getSystemSettings":
            cat = spec["category"]
            if key not in settings.get(cat, set()):
                checks[cid] = dict(
                    present=False,
                    source=f"no {cat or '(no category)'} record of "
                    f"rootfs.pak:/etc/palm/defaultSettings.json has {key}"
                    + (f"; {cite}" if cite else ""),
                )
                continue
            checks[cid] = dict(
                present=True,
                source=f"rootfs.pak:/etc/palm/defaultSettings.json ({cat or '-'}.{key})",
            )
            continue
        if key:
            kw = has_string(bins, key)
            if not kw:
                checks[cid] = dict(
                    present=False,
                    source=f"getSystemInfo key {key}: no string {key!r} in "
                    f"{labels(bins)}" + (f"; {cite}" if cite else ""),
                )
                continue
            where = kw
        checks[cid] = dict(
            present=True,
            source=f"{svc} by {src}; "
            + (reg_src if reg_src and not key else f"{method!r} in {where}"),
        )
        if cid == "sysinfo.UHD":
            checks[cid].update(uhd_of(target, fw.dir, bins) or {})
    teardown = kadp_teardown(fw, pak_order)
    checks["nyx.DeviceInfo.output"] = (
        dict(present=True, accept=teardown[0], source=teardown[1])
        if teardown
        else dict(present=True, source="nyx-cmd prints its value only")
    )
    checks["nyx.OSInfo.output"] = dict(present=True, source="nyx-cmd prints its value only")
    for cid, (present, src) in nyx_rows(fw, pak_order).items():
        checks[cid] = dict(present=present, source=src)
        if cid in tvsuite.DESIGN_ABSENT:
            # expected not available whatever the module does: no adapter in the emulator
            checks[cid] = dict(
                present=False, design=True, source=f"{tvsuite.DESIGN_REASON} ({src})"
            )
    # the firmware's model_id.json; a firmware before it (jhericurl) names it only in its paks
    broadcast = targets.firmware_broadcast(fw_dir) or targets.broadcast_of_paks(list(fw.paks))
    return dict(
        firmware=fw_name,
        release=rel,
        webos=major,
        broadcast=broadcast,
        online=online_of(fw),
        checks=checks,
        apps=apps_of(fw, pak_order),
        tee=tee_of(fw, pak_order, target),
        audio=audio_of(fw, provs, tvpak),
        decode=decode_of(fw, mse_of(fw)),
        smp=smp_of(fw),
    )


# --- the target's file: tests/regress/tv-expect/<target>.toml ---

HEADER = """What the TV feature suite (tests/regress/lib/tvsuite.py) expects of target {target}, firmware
{firmware}. Generated by tests/regress/tv-expect-gen.py from that firmware.
present = false: the firmware does not have the check, app or case; the suite SKIPs it and cites
its source. A check's source names an entry of [sources], the evidence, once for all checks citing it.
Hand edits are fine for fixes across many targets (sed, scripts), but the generator is the source
of truth: make the same change there, or regenerate the file."""

# the tables in this order, the rest by name after them; [sources] last
TABLES = ("release", "online", "checks", "apps", "tee", "audio", "decode", "smp")
LINE = 100  # a longer list has one item a line


def source_id(text):
    """A short, stable name for an evidence string: its first words (paths as their last part)
    and a hash of all of it."""
    name = ""
    for w in re.findall(r"[A-Za-z0-9_]+", re.sub(r"\S*/(\S+)", r"\1", text))[:4]:
        if name and len(name) + 1 + len(w) > 32:
            break
        name = f"{name}-{w}" if name else w[:32]
    return f"{name or 'source'}-{hashlib.sha1(text.encode()).hexdigest()[:6]}"


def toml_inline(v):
    """A TOML value on one line: inline tables with their keys sorted, None values left out."""
    if isinstance(v, dict):
        items = [f"{toml_key(k)} = {toml_inline(v[k])}" for k in sorted(v) if v[k] is not None]
        return "{ " + ", ".join(items) + " }" if items else "{}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_inline(i) for i in v) + "]"
    if isinstance(v, float):
        return repr(v)
    return toml_value(v)


def toml_pair(k, v):
    """k = v, a list too long for a line as one item a line."""
    line = f"{toml_key(k)} = {toml_inline(v)}"
    if isinstance(v, (list, tuple)) and v and len(line) > LINE:
        return f"{toml_key(k)} = [\n" + "".join(f"    {toml_inline(i)},\n" for i in v) + "]"
    return line


def toml_table(name, table):
    """[name] with a line for each key: scalars, lists, and each subtable as an inline table."""
    out = [f"\n[{name}]"]
    out += [toml_pair(k, table[k]) for k in sorted(table) if table[k] is not None]
    return out


def expect_text(target, row):
    """A target's row of the expected table as its TOML file: the facts, a table for each part
    of it (one line a check, decode case or app part), and the checks' evidence in [sources]."""
    row = dict(row)
    checks, sources = {}, {}
    for cid, spec in (row.get("checks") or {}).items():
        spec = dict(spec)
        if "source" in spec:
            sid = source_id(spec["source"])
            if sources.setdefault(sid, spec["source"]) != spec["source"]:
                raise ValueError(f"{target}: source id {sid} for two strings")
            spec["source"] = sid
        checks[cid] = spec
    if "checks" in row:
        row["checks"] = checks
    out = [
        "# " + line if line else "#"
        for line in HEADER.format(target=target, firmware=row.get("firmware", "?")).splitlines()
    ]
    tables = [k for k in TABLES if isinstance(row.get(k), dict)]
    tables += sorted(k for k, v in row.items() if isinstance(v, dict) and k not in TABLES)
    for k in ("firmware", "webos", "broadcast"):
        if row.get(k) is not None:
            out.append(toml_pair(k, row[k]))
    out += [
        toml_pair(k, v)
        for k, v in sorted(row.items())
        if k not in tables and k not in ("firmware", "webos", "broadcast") and v is not None
    ]
    for k in tables:
        if k == "apps":  # an app has many parts: a table each
            for role in sorted(row[k]):
                out += toml_table(f"apps.{toml_key(role)}", row[k][role])
        else:
            out += toml_table(k, row[k])
    if sources:
        out += toml_table("sources", sources)
    return "\n".join(out) + "\n"


def write_expect(directory, target, row):
    """Writes tests/regress/tv-expect/<target>.toml (in directory), through a temporary file."""
    path = os.path.join(directory, f"{target}.toml")
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(expect_text(target, row))
    os.replace(tmp, path)
    return path


# --table: the tables that can be made on their own (their checks cite no [sources] entry)
TABLE_ROWS = {"decode": lambda fw: decode_of(fw, mse_of(fw))}


def replace_tables(directory, target, fw_name, fw, names):
    """Rewrites only the named tables (TABLE_ROWS) of a target's existing file in directory, made
    from the firmware the file names."""
    path = os.path.join(directory, f"{target}.toml")
    with open(path) as f:
        text = f.read()
    named = re.search(r'^firmware = "(.*)"$', text, re.M)
    if not named or named.group(1) != fw_name:
        raise SystemExit(f"{path} is of firmware {named and named.group(1)}, not {fw_name}: --fw")
    for name in names:
        block = "\n".join(toml_table(name, TABLE_ROWS[name](fw))) + "\n"
        text, n = re.subn(
            rf"\n\[{re.escape(name)}\]\n.*?(?=\n\[|\Z)", lambda m: block, text, flags=re.S
        )
        if n != 1:
            raise SystemExit(f"{path}: no [{name}] table to replace")
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", action="append", default=[])
    ap.add_argument("--fw", action="append", default=[])
    ap.add_argument("-o", default=tvsuite.EXPECT_DIR, help="the directory of the target files")
    ap.add_argument(
        "--table",
        action="append",
        choices=sorted(TABLE_ROWS),
        default=[],
        help="regenerate only this table of each target's existing file, the rest left as it is",
    )
    a = ap.parse_args(argv)
    regress = regress_module()
    sel = regress.select(
        [x for p in a.target for x in p.split(",")],
        a.fw,
        regress.find_firmwares(regress.firmware_dirs()),
    )
    os.makedirs(a.o, exist_ok=True)
    for target, fw_name in sel:
        fw_dir = next(
            os.path.join(d, fw_name)
            for d in regress.firmware_dirs()
            if os.path.isdir(os.path.join(d, fw_name))
        )
        print(f"{target}: {fw_name}", file=sys.stderr)
        if a.table:
            replace_tables(a.o, target, fw_name, Firmware(fw_dir), a.table)
        else:
            write_expect(a.o, target, target_row(target, fw_name, fw_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
