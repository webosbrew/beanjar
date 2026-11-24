# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Host checks for the memory contracts consumed by the shared SoC modules.

The common defconfig keeps arch/arm64/Kconfig's default ARM64_4K_PAGES
(the expanded shared kernel config confirms it). Configured
MStar records are packed as mmapger_layout() in guest/kernel/modules/mstar-utopia/memory.c, including direct
parent aliases; firmware-derived records instead retain their own overlaps and
absent entries. Realtek DCUs have independent allocation bitmaps, so overlapping
DCUs would allocate the same physical pages twice. These are emulator contracts,
not a claim that the windows match a retail board's physical layout.
"""

import struct

PAGE = 4096
U32 = 0xFFFFFFFF
# mstar-utopia.ko's mmap_names: the record names each of its kernel name tables has
MMAP_NAMES = {"m3": 95, "lm18a": 102}


def parameters(words):
    # Kernel scalar parameters use their last value; array parameters also replace
    # the preceding value when repeated, as boot.conf's ordered module arguments do.
    return dict(word.split("=", 1) for word in words if "=" in word)


def number(p, key, default=0, maximum=U32):
    try:
        value = int(p.get(key, str(default)), 0)
    except ValueError:
        raise ValueError(f"{key}: expected an integer") from None
    if not 0 <= value <= maximum:
        raise ValueError(f"{key}: outside 0..{maximum:#x}")
    return value


def window(base, size, label, limit=1 << 32):
    if not base or not size or base % PAGE or size % PAGE or base + size > limit:
        raise ValueError(f"{label}: expected a nonzero page-aligned range within {limit:#x}")
    return base, base + size


def mstar(p, data=None):
    interval = number(p, "miu_interval", maximum=(1 << 64) - 1)
    length = number(p, "miu_len", maximum=(1 << 64) - 1)
    count = number(p, "mmap_ids")
    names = p.get("mmap_names", "m3")
    if names not in MMAP_NAMES:
        raise ValueError(f"mmap_names: one of {', '.join(MMAP_NAMES)}")
    if not 0 < length <= interval <= U32 or 2 * length > U32 or interval + length > 1 << 32:
        raise ValueError("MIUs: lengths/interval must fit mmapger info.bin u32 fields")
    if not count or count > U32 // 20:
        raise ValueError("mmap_ids: expected a positive representable record count")
    for key in ("miu0_bus", "miu1_bus"):
        base = number(p, key)
        if not base or base + length > 1 << 32:
            raise ValueError(f"{key}: MIU bus range exceeds 32-bit addresses")
    for key in ("lx_mem_id", "lx_mem2_id", "secure_shm_id", "gop_fb"):
        raw = p.get(key, "-1" if key in ("secure_shm_id", "gop_fb") else "0")
        try:
            value = int(raw, 0)
        except ValueError:
            raise ValueError(f"{key}: expected a record ID") from None
        if value == -1 and key in ("secure_shm_id", "gop_fb"):
            continue
        if not 0 <= value < count:
            raise ValueError(f"{key}: record ID outside mmap_ids={count}")
        if key == "secure_shm_id" and (value != 7 or count > MMAP_NAMES.get(names, 0)):
            # mmapger_name(): W18R's table (95) or W18A's (102); SECURE_SHM is index 7 in both.
            raise ValueError("secure_shm_id: must name SECURE_SHM (7) in the kernel name table")
    if p.get("mmap_file"):
        if number(p, "mmap_slot") or p.get("mmap_len") or p.get("mmap_in"):
            raise ValueError("mmap_file: cannot combine derived and configured records")
        if data is None:
            return None  # derived file is checked when prepared, before sharing
        return binary_records(data, count, length)
    slot = number(p, "mmap_slot")
    if not slot or slot % PAGE:
        raise ValueError("mmap_slot: expected a positive page-aligned length")
    lengths = [slot] * count
    nested = {}
    for key, width in (("mmap_len", 2), ("mmap_in", 3)):
        seen = set()
        entries = p.get(key, "").split(",") if p.get(key) else []
        if len(entries) > 16:
            raise ValueError(f"{key}: kernel accepts at most 16 entries")
        for item in entries:
            try:
                values = [int(v, 0) for v in item.split(":")]
            except ValueError:
                raise ValueError(f"{key}={item}: expected integer fields") from None
            if len(values) != width or any(not 0 <= v <= U32 for v in values):
                raise ValueError(f"{key}={item}: expected {width} u32 fields")
            child = values[0]
            if child >= count or child in seen:
                raise ValueError(f"{key}={item}: invalid or repeated record ID")
            seen.add(child)
            if key == "mmap_len":
                if not values[1] or values[1] % PAGE:
                    raise ValueError(f"{key}={item}: expected a positive page-aligned length")
                lengths[child] = values[1]
            else:
                parent, offset = values[1:]
                if parent >= count or child == parent or offset % PAGE:
                    raise ValueError(f"{key}={item}: invalid parent or unaligned offset")
                nested[child] = (parent, offset)
    offsets = [0, 0]
    miu = 0
    rows = [None] * count
    for i, size in enumerate(lengths):
        if i in nested:
            continue
        if offsets[0] + size > length:
            miu = 1
        if offsets[miu] + size > length:
            raise ValueError(f"mmap record {i}: configured records exceed two MIUs")
        rows[i] = (offsets[miu], size, miu)
        offsets[miu] += size
    for child, (parent, offset) in nested.items():
        if parent in nested or offset + lengths[child] > lengths[parent]:
            raise ValueError(f"mmap_in record {child}: must fit a non-nested parent {parent}")
        addr, _, miu = rows[parent]
        rows[child] = (addr + offset, lengths[child], miu)
    # Siblings may overlap within their explicitly declared parent (SS1A's
    # VD_3DCOMB/TSP and VQ/VBI). Rejecting those aliases would change the layout.
    return rows


def binary_records(data, count, length):
    """mmapger_file_layout(): five little-endian u32s per ID, with sparse aliases.

    Zero lengths are valid absent firmware records. Unlike configured allocations,
    firmware offsets/lengths need not be page aligned; the kernel does not require it.
    """
    if len(data) != count * 20:
        raise ValueError(
            f"mmap file: expected {count} records ({count * 20} bytes), got {len(data)}"
        )
    rows = []
    for i, (available, addr, gap, size, kind) in enumerate(struct.iter_unpack("<5I", data)):
        if kind & ~5 or available > addr or gap > addr - available or addr + size > length:
            raise ValueError(f"mmap file record {i}: invalid type, available/gap or MIU range")
        rows.append((addr, size, kind & 1))
    return rows


def realtek(p):
    rbus = window(number(p, "rbus_phys"), number(p, "rbus_size"), "RBUS")
    windows = []
    for i in (1, 2):
        base, size = number(p, f"dcu{i}_base"), number(p, f"dcu{i}_size")
        if i == 2 and not size:
            continue  # optional DCU2, as dcu_setup() specifies
        span = window(base, size, f"DCU{i}")
        for label, other in [("RBUS", rbus)] + windows:
            if span[0] < other[1] and other[0] < span[1]:
                raise ValueError(f"DCU{i}: overlaps {label}")
        windows.append((f"DCU{i}", span))
    return windows


def validate(module, words, data=None):
    p = parameters(words)
    if module == "mstar-utopia":
        return mstar(p, data)
    if module == "rtk":
        return realtek(p)
    return None
