#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""LG linear-container import: raw byte identity, refusal boundaries, official FS reader."""

import shutil
from pathlib import Path
from unittest import mock

import pytest
from beanjar import base
from beanjar.firmware import install, probe, superpak

HERE = Path(__file__).resolve().parent


class Pak:
    def __init__(self, tmp):
        self.src = Path(tmp) / "source"
        self.src.mkdir()
        self.dest = Path(tmp) / "dest"
        self.dest.mkdir()
        # Real SquashFS fixture, padded within a nonzero-offset block range.
        root = (HERE.parents[1] / "fakes" / "trust-rootfs.pak").read_bytes()
        self.payloads = {
            "rootfs": root + bytes(1536 - len(root)),
            "fonts": b"F" * 512,
            "otncabi": b"N" * 512,
            "otycabi": b"Y" * 512,
            "bsppart": b"B" * 512,
        }
        self.table = []
        self.positions = {}
        offset = 1024
        data = bytes(offset)
        for index, (name, payload) in enumerate(self.payloads.items()):
            self.positions[name] = offset
            self.table += [
                f"{name}-linear,,,ro, 0 {len(payload) // 512} linear /dev/mmcblk0p27 {offset // 512}",
                f"{name},,,ro, 0 1 webos-verity /dev/dm-{index * 2}",
            ]
            data += payload + bytes(512)
            offset += len(payload) + 512
        (self.src / "super.pak").write_bytes(data)
        self.metadata()

    def metadata(self):
        text = ' dm-mod.waitfor="/dev/mmcblk0p27" dm-mod.create="' + ";".join(self.table)
        text += '" root=/dev/dm-1 ro rootfstype=squashfs '
        (self.src / "dpmeta.pak").write_bytes(text.encode() + b"\0" + b"binary trailer")


@pytest.fixture
def pak(tmp_path):
    return Pak(tmp_path)


def test_raw_ranges_and_source_preservation(pak):
    original = (pak.src / "super.pak").read_bytes()
    images = superpak.images(pak.src)
    assert set(images) == set(pak.payloads)
    for name, image in images.items():
        assert image.offset == pak.positions[name]
        superpak.copy(image, pak.dest / (name + ".pak"))
        assert (pak.dest / (name + ".pak")).read_bytes() == pak.payloads[name]
    assert (pak.src / "super.pak").read_bytes() == original
    assert not (pak.src / "rootfs.pak").exists()


@pytest.mark.skipif(not shutil.which("unsquashfs"), reason="official unsquashfs required")
def test_release_read_from_offset(pak):
    family, source = probe.firmware_release(pak.src)
    assert family == "afro"
    assert "super.pak" in source


def test_import_and_storage_estimate(pak):
    (pak.src / "license.pak").write_bytes(b"LICENSE")
    names = tuple(pak.payloads) + ("license",)
    with mock.patch.object(install.targets, "pak_files", return_value=names):
        install.check_paks(pak.src, ("test", "test"))
        assert install.paks_size(pak.src, ("test", "test")) == (
            sum(map(len, pak.payloads.values())) + 7
        )
        assert install.copy_size(pak.src, ("test", "test"), pak.dest) == sum(
            map(len, pak.payloads.values())
        )
        install.place(pak.src, pak.dest, ("test", "test"), "link", lambda _: None)
        for name, payload in pak.payloads.items():
            assert (pak.dest / (name + ".pak")).read_bytes() == payload
        assert (pak.dest / "license.pak").read_bytes() == b"LICENSE"
        (pak.src / "license.pak").unlink()
        with pytest.raises(base.Error, match="lacks license.pak"):
            install.check_paks(pak.src, ("test", "test"))


def test_refuses_invalid_maps(pak):
    original = pak.table[:]
    variants = [
        (0, original[0].replace(" 3 linear", " 999999 linear"), "exceeds"),
        (2, original[2].rsplit(" ", 1)[0] + " 2", "overlapping"),
        (1, original[1].replace("dm-0", "dm-2"), "integrity"),
        (0, original[0].replace("p27", "p28"), "mismatched"),
        (0, original[0].replace("rootfs-linear", "evil-linear"), "unsupported"),
        (0, original[0].replace(",,,ro,", ",,,rw,"), "linear"),
        (2, original[2].replace("fonts-linear", "rootfs-linear"), "duplicate"),
    ]
    for index, replacement, error in variants:
        pak.table = original[:]
        pak.table[index] = replacement
        pak.metadata()
        with pytest.raises(ValueError, match=error):
            superpak.images(pak.src)


def test_missing_metadata_and_ambiguous_image(pak):
    (pak.src / "dpmeta.pak").unlink()
    with pytest.raises(ValueError, match="requires readable"):
        superpak.images(pak.src)
    pak.metadata()
    (pak.src / "rootfs.pak").write_bytes(b"conflicting")
    with pytest.raises(ValueError, match="ambiguous"):
        superpak.images(pak.src)


@pytest.mark.parametrize("machine", ["NT25L", None, "K8LPN2"], ids=["NT25L", "none", "K8LPN2"])
def test_w24p_requires_actual_machine(pak, machine):
    # The stock profile registry names both K8LPN2 and NT25L for W24P.
    name = "33.20.23.01-HE_DTV_W24P_AFADATAA"
    identity = '{"machine": "' + machine + '"}' if machine else "{}"
    with (
        mock.patch.object(probe, "firmware_release", return_value=("ombre", "fixture")),
        mock.patch.object(probe, "_squashfs_cat", return_value=identity),
    ):
        if machine == "K8LPN2":
            assert probe.firmware_target(str(pak.src), name) == ("k8lpn2", "ombre")
        else:
            with pytest.raises(ValueError, match="is not K8LPN2"):
                probe.firmware_target(str(pak.src), name)


def test_truncated_copy_removes_partial_image(pak):
    image = superpak.images(pak.src)["rootfs"]
    with (pak.src / "super.pak").open("r+b") as f:
        f.truncate(image.offset + 10)
    target = pak.dest / "rootfs.pak"
    with pytest.raises(ValueError, match="truncated"):
        superpak.copy(image, target)
    assert not target.exists()
