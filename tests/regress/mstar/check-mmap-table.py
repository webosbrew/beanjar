#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check the extractor against a live stock GetMMAP dump and code drift.

Usage: check-mmap-table.py LIBMADP LIVE_DUMP
Inputs are generated locally; no firmware bytes or expected rows are tracked.
"""

import importlib.util
import io
import struct
import sys
from pathlib import Path

from elftools.elf.elffile import ELFFile

path = Path(__file__).resolve().parents[3] / "host/helpers/mstar/mmap-table.py"
spec = importlib.util.spec_from_file_location("mmap_table", path)
table = importlib.util.module_from_spec(spec)
spec.loader.exec_module(table)
data = Path(sys.argv[1]).read_bytes()
live = table.live_rows(Path(sys.argv[2]))
assert table.extract(data) == live, "static rows differ from live stock function"
elf = ELFFile(io.BytesIO(data))
sym = next(
    s for s in elf.get_section_by_name(".dynsym").iter_symbols() if s.name == "MAdp_SYS_GetMMAP"
)
start = sym["st_value"]
segment = next(
    s
    for s in elf.iter_segments()
    if s["p_type"] == "PT_LOAD" and s["p_vaddr"] <= start < s["p_vaddr"] + s["p_filesz"]
)
offset = segment["p_offset"] + start - segment["p_vaddr"]


def rejects(fn):
    try:
        fn()
    except table.Ambiguous:
        return
    raise AssertionError("ambiguous input was accepted")


# Replace an instruction, rather than change an expected output to hide drift.
for at in (offset, offset + 32):
    changed = bytearray(data)
    struct.pack_into("<I", changed, at, 0xEB000000)  # BL is outside the leaf pattern
    rejects(lambda: table.extract(changed))
rejects(lambda: table.extract(data[: offset + 48]))
rejects(lambda: table.records([(0x40000001, 0)], 0x80000000, 0x40000000))
rejects(lambda: table.records([(0xBFFFF000, 8192)], 0x80000000, 0x40000000))
rejects(lambda: table.records(live, 0, 0x40000000))
encoded = table.records(live, 0x80000000, 0x40000000)
assert len(encoded) == len(live) * 20
print(f"{len(live)} live rows agree; code drift, truncation and invalid MIU ranges rejected")
