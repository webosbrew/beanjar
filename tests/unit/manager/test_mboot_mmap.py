#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""host/helpers/mstar/mboot-mmap.py: the one MBoot mmapger table a firmware's TEE memory map agrees
with. The fixtures are cut from W16R 05.70.50's facts: its boot.pak's third table (file offset
0x16bfe8, of three) and the records of its bsppart's bsp/tee/mmap.ini that name an mmapger record."""

import importlib.util
import json
import shutil
import struct
import subprocess
from pathlib import Path
from unittest import mock

import pytest

TOP = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "mboot_mmap", TOP / "host/helpers/mstar/mboot-mmap.py"
)
mboot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mboot)

MIU = 0x20000000
INTERVAL = 0x80000000

# W16R's board table, the rows the tests read: {id: (available, addr, gap_chk, len, type)}; the
# others of its 89 records are left zero here, record 88 (HWI2C_DMA) is its last.
W16R = {
    1: (0x00200000, 0x00200000, 0, 0x18000000, 4),  # LINUX_MEM
    4: (0x1E700000, 0x1E700000, 0, 0x01000000, 4),  # NFDRM_NUTTX_MEM
    7: (0x1FC00000, 0x1FC00000, 0, 0x00200000, 4),  # SECURE_SHM
    11: (0x18F9A000, 0x18F9A000, 0, 0x00E00000, 4),  # MAD_BASE_BUFFER
    42: (0x01300000, 0x01300000, 0, 0x05600000, 5),  # VDEC_FRAMEBUFFER, MIU1
    44: (0x0C300000, 0x0C300000, 0, 0x00100000, 5),  # NFDRM_VDEC_SHARE_MEM, MIU1
    67: (0x0C900000, 0x0C900000, 0, 0x13700000, 5),  # LX_MEM2, MIU1
    88: (0x1FF00000, 0x1FF00000, 0, 0x00100000, 4),  # HWI2C_DMA
}

INI_HEAD = """\
#define MIU_DRAM_LEN                0x0040000000
#define MIU_DRAM_LEN0               0x0020000000
#define MIU_DRAM_LEN1               0x0020000000
#define MIU_INTERVAL                0x0080000000
"""


def ini_record(ini, name, row, miu=None):
    available, addr, gap, size, kind = row
    miu = ("MIU1" if kind & 1 else "MIU0") if miu is None else miu
    return f"""
/* {ini} = {name}  */
#define {ini}_AVAILABLE                             {available:#012x}
#define {ini}_ADR                                   {addr:#012x}
#define {ini}_GAP_CHK                               {gap:#012x}
#define {ini}_LEN                                   {size:#012x}
#define {ini}_MEMORY_TYPE                           ({miu} | TYPE_NONE | UNCACHE)
"""


# W16R's TEE map, the records named above (MAD_SE and MAD_DEC, offsets into MAD_BASE_BUFFER, have
# "MAD_BASE_BUFFER+0x14000" as their name and are not mmapger records)
INI = (
    INI_HEAD
    + ini_record("E_MMAP_ID_MAD_R2", "MAD_BASE_BUFFER", W16R[11])
    + "\n/* E_MMAP_ID_MAD_SE = MAD_BASE_BUFFER+0x14000  */\n"
    + "#define E_MMAP_ID_MAD_SE_ADR                                   0x0018fae000\n"
    + ini_record("E_MMAP_ID_NUTTX_MEM", "NFDRM_NUTTX_MEM", W16R[4])
    + ini_record("E_SECURE_SHM", "SECURE_SHM", W16R[7])
    + ini_record("E_MMAP_ID_VDEC_FRAMEBUFFER", "VDEC_FRAMEBUFFER", W16R[42])
    + ini_record("E_MMAP_ID_VDEC_SHARE_MEM", "NFDRM_VDEC_SHARE_MEM", W16R[44])
)


def table(rows, change=None):
    out = bytearray(mboot.ROWS * mboot.ROW)
    for rid, row in {**rows, **(change or {})}.items():
        struct.pack_into("<5I", out, rid * mboot.ROW, *row)
    return bytes(out)


def image(*tables):
    """An MBoot-like image: code-like filler around the tables."""
    filler = bytes(range(256)) * 3
    return filler + b"".join(t + filler for t in tables)


# W16R's other two tables put MAD_BASE_BUFFER at 0x0c000000 and 0x19b9a000
DECOY_A = table(W16R, {11: (0x0C000000, 0x0C000000, 0, 0x00E00000, 4)})
DECOY_B = table(W16R, {11: (0x19B9A000, 0x19B9A000, 0, 0x00E00000, 4)})
BOARD = table(W16R)


def test_w16r_board_table():
    rows, geometry = mboot.extract(image(DECOY_A, DECOY_B, BOARD), INI)
    assert geometry == (MIU, MIU, INTERVAL)
    assert len(rows) == 89  # mmap_ids
    assert rows[1] == W16R[1] and rows[67] == W16R[67] and rows[42] == W16R[42]
    assert rows[0] == (0, 0, 0, 0, 0)
    data = mboot.records(rows, geometry, MIU)
    assert len(data) == 89 * 20
    assert struct.unpack_from("<5I", data, 67 * 20) == W16R[67]


def test_ini_names_records():
    want, geometry = mboot.ini_records(INI)
    assert sorted(want) == [4, 7, 11, 42, 44]
    assert want[42] == W16R[42][:4] + (True,)
    assert geometry == (MIU, MIU, INTERVAL)


@pytest.mark.parametrize(
    "tables",
    [
        (DECOY_A, DECOY_B),  # none agrees
        (BOARD, DECOY_A, BOARD),  # two agree
        # the board's addresses with MIU0 type bits where the TEE map says MIU1
        (table(W16R, {42: W16R[42][:4] + (4,)}),),
    ],
)
def test_one_table_or_none(tables):
    with pytest.raises(mboot.Ambiguous):
        mboot.extract(image(*tables), INI)


@pytest.mark.parametrize(
    "rows",
    [
        {**W16R, 95: (0, 0x1000, 0x800, 0, 4)},  # gap_chk not the difference: no table
        {**W16R, 3: (0, 0x1000, 0x1000, 0x1000, 2)},  # a cache type the module has not
    ],
)
def test_invalid_table(rows):
    with pytest.raises(mboot.Ambiguous):
        mboot.extract(image(table(rows)), INI)


def test_trailing_rows_zero():
    # zero records between nonzero ones are absent records; the records end at the last nonzero
    rows = dict(W16R)
    rows[95] = (0x1000, 0x1000, 0, 0x1000, 4)
    got, _ = mboot.extract(image(table(rows)), INI)
    assert len(got) == 96


@pytest.mark.parametrize(
    "ini",
    [
        INI + ini_record("E_MMAP_ID_X", "NOT_A_RECORD", W16R[4]),
        INI.replace("#define MIU_INTERVAL ", "#define MIU_INTERVALX "),
        INI + ini_record("E_MMAP_ID_NUTTX2", "NFDRM_NUTTX_MEM", W16R[4]),  # twice
        INI + ini_record("E_MMAP_ID_VDEC_CPU", "VDEC_CPU", (0, 0, 0, 0x300000, 5), "MIU2"),
        INI_HEAD,
    ],
)
def test_ini_rejected(ini):
    with pytest.raises(mboot.Ambiguous):
        mboot.ini_records(ini)


def test_geometry_must_be_the_module_s():
    rows, geometry = mboot.extract(image(BOARD), INI)
    with pytest.raises(mboot.Ambiguous):
        mboot.records(rows, geometry, 0x40000000)
    with pytest.raises(ValueError):  # a record past the MIU
        mboot.records(rows, (0x10000000, 0x10000000, INTERVAL), 0x10000000)


@pytest.mark.skipif(not shutil.which("mksquashfs"), reason="needs squashfs-tools")
def test_cached_firmware_directory(tmp_path):
    fw = tmp_path / "05.70.50.01-HE_DTV_W16R_AFAAABAA"
    tee = tmp_path / "bsppart" / "bsp" / "tee"
    tee.mkdir(parents=True)
    (tee / "mmap.ini").write_text(INI)
    fw.mkdir()
    subprocess.run(
        [
            "mksquashfs",
            str(tmp_path / "bsppart"),
            str(fw / "bsppart.pak"),
            "-quiet",
            "-no-progress",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    (fw / "boot.pak").write_bytes(image(DECOY_A, BOARD))
    with mock.patch.object(mboot, "REPO", tmp_path):
        out, count = mboot.cached(fw, fw.name, INTERVAL, MIU)
        assert count == 89
        original = out.read_bytes()
        meta = json.loads(out.with_suffix(".json").read_text())
        assert meta["source"] == "MBoot" and meta["count"] == 89
        with mock.patch.object(mboot, "extract", side_effect=AssertionError("reused")):
            assert mboot.cached(fw, fw.name, INTERVAL, MIU) == (out, 89)
        out.write_bytes(original[:-20] + bytes(20))  # a changed cache is made again
        assert mboot.cached(fw, fw.name, INTERVAL, MIU)[1] == 89
        assert out.read_bytes() == original
        with pytest.raises(mboot.Ambiguous):  # the module's MIU interval must be the board's
            mboot.cached(fw, fw.name, 0x40000000, MIU)
    (fw / "boot.pak").unlink()
    with pytest.raises(mboot.Ambiguous, match="no boot.pak"):
        mboot.firmware_inputs(fw)


def test_target_wiring():
    from beanjar import targets

    m = targets.SocModule("mstar-utopia", ("miu_interval=0x80000000", "miu_len=0x20000000"))
    m = m._replace(derived="mstar-mboot")
    soc = targets.soc(("m3", "goldilocks"))._replace(modules=(m,))
    with mock.patch.object(targets, "soc", return_value=soc):
        # the import keeps the MBoot the records are read from
        assert "boot" in targets.pak_files(("m3", "goldilocks"))
        result = mock.Mock(returncode=0, stdout="/cache/mmap.bin\nmmap_ids=89\n", stderr="")
        with mock.patch.object(targets.subprocess, "run", return_value=result) as run:
            assert targets.derived_params(("m3", "goldilocks"), "fw", "name") == {
                "mstar-utopia": ("mmap_ids=89", "mmap_file=/host/mstar-mmap.bin")
            }
        argv = run.call_args[0][0]
        assert argv[1] == targets.MBOOT_MMAP and "--cache" not in argv
    # W16R's (M2) records are its MBoot's; M3's stay configured
    assert "boot" in targets.pak_files(("m2", "dreadlocks"))
    assert "boot" not in targets.pak_files(("m3", "goldilocks"))
