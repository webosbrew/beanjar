#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""lls.Root reads a super.pak firmware (W24 ombre on) through its logical images, as it reads the
named paks of an imported one: the rootfs, the release's pak lowers (bsppart's bsp/) and the
root's binds into a pak mounted elsewhere (otycabi's nyx modules), with unsquashfs at each
image's offset."""

import shutil
import subprocess
from pathlib import Path

import pytest
from beanjar.board import lls
from beanjar import superpak

pytestmark = pytest.mark.skipif(
    not (shutil.which("mksquashfs") and shutil.which("unsquashfs")), reason="needs squashfs-tools"
)

TARGET = ("k8lpn2", "ombre")
FSTAB = "etc/systemd/system/mount-readonly-volatile.d/nyx.fstab"
TREES = {
    "rootfs": {
        "usr/bin/nyx-option-gen": b"root generator",
        "usr/bin/both": b"from the rootfs",
        FSTAB: b"/mnt/otycabi/usr/lib/nyx/modules /usr/lib/nyx/modules none bind 0 0\n",
    },
    "bsppart": {"bsp/usr/bin/both": b"from bsppart", "bsp/usr/bin/hw-option-gen": b"bsp generator"},
    "otycabi": {"usr/lib/nyx/modules/nyxLlsDb.module": b"lls module"},
    "otncabi": {"x": b"n"},
    "fonts": {"x": b"f"},
}


def squashfs(tmp, name, files):
    src = Path(tmp) / f"{name}-src"
    for path, data in files.items():
        (src / path).parent.mkdir(parents=True, exist_ok=True)
        (src / path).write_bytes(data)
    img = Path(tmp) / f"{name}.img"
    subprocess.run(
        ["mksquashfs", str(src), str(img), "-quiet", "-noappend", "-no-progress", "-all-root"],
        check=True, stdout=subprocess.DEVNULL,
    )  # fmt: skip
    data = img.read_bytes()
    return data + bytes(-len(data) % 512)


def super_firmware(tmp):
    """the measured dpmeta.pak linear map over a super.pak of TREES' images"""
    d = Path(tmp) / "33.20.23.01-HE_DTV_W24P_AFADATAA"
    d.mkdir()
    table, offset, data = [], 1024, bytes(1024)
    for index, (name, files) in enumerate(TREES.items()):
        payload = squashfs(tmp, name, files)
        table += [
            f"{name}-linear,,,ro, 0 {len(payload) // 512} linear /dev/mmcblk0p27 {offset // 512}",
            f"{name},,,ro, 0 1 webos-verity /dev/dm-{index * 2}",
        ]
        data += payload
        offset += len(payload)
    (d / "super.pak").write_bytes(data)
    text = ' dm-mod.waitfor="/dev/mmcblk0p27" dm-mod.create="' + ";".join(table)
    text += '" root=/dev/dm-1 ro rootfstype=squashfs '
    (d / "dpmeta.pak").write_bytes(text.encode() + b"\0")
    return d


def imported_firmware(tmp, fw):
    """fw's images as the named paks a VM's firmware directory has (firmware.place)"""
    d = Path(tmp) / "imported" / fw.name
    d.mkdir(parents=True)
    for name, image in superpak.images(fw).items():
        superpak.copy(image, d / f"{name}.pak")
    return d


def test_super_pak_root_reads_its_logical_images(tmp_path):
    fw = super_firmware(tmp_path)
    assert not (fw / "rootfs.pak").exists()
    root = lls.Root(str(fw), TARGET)
    assert root.read("/usr/bin/nyx-option-gen") == b"root generator"
    # the release's pak lower is above the rootfs
    assert root.read("/usr/bin/both") == b"from bsppart"
    assert root.read("/usr/bin/hw-option-gen") == b"bsp generator"
    # the root's bind of /mnt/otycabi, a pak mounted elsewhere
    assert root.binds() == [("/mnt/otycabi/usr/lib/nyx/modules", "/usr/lib/nyx/modules")]
    assert root.read(lls.NYX_LLS) == b"lls module"
    assert root.entry("/usr/bin/nyx-option-gen")[1:] == ("", "-")
    assert root.read("/usr/bin/missing") is None


def test_super_pak_root_matches_the_imported_paks(tmp_path):
    """the files a run extracts, and so lls.cache_dir's key, are those of the imported firmware"""
    fw = super_firmware(tmp_path)
    imported = imported_firmware(tmp_path, fw)
    out = {}
    for name, d in (("super", fw), ("imported", imported)):
        root = lls.Root(str(d), TARGET)
        out[name] = lls.extract(root, (), ("/usr/bin/both", lls.NYX_LLS), str(tmp_path / name))
    assert out["super"] == out["imported"]
    assert (tmp_path / "super" / "usr/bin/both").read_bytes() == b"from bsppart"


def test_unsupported_dpmeta_is_an_lls_error(tmp_path):
    fw = super_firmware(tmp_path)
    (fw / "dpmeta.pak").write_bytes(b"root=/dev/sda1\0")
    with pytest.raises(lls.Error, match="unsupported dpmeta"):
        lls.Root(str(fw), TARGET)
