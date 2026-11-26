#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""factory.py: the apps partition's pre-built data of a model with the microphone, the firmware's
own wake-word model and THF config under preload/voiceinput/thinq/snsr in a small ext4 image,
owned by root; none for a model without the microphone."""

import os
import shutil
import subprocess

import pytest

from beanjar import factory, lls

TOOLS = all(shutil.which(t) for t in ("mksquashfs", "unsquashfs", "mke2fs", "debugfs", "unshare"))


def rootfs(tmp_path, names):
    """A squashfs rootfs with names in the model directory and the firmware's mke2fs.conf."""
    top = tmp_path / "rootfs"
    snsr = top / factory.SNSR
    snsr.mkdir(parents=True)
    for n in names:
        (snsr / n).write_bytes(n.encode() * 100)
    (top / "etc").mkdir()
    (top / "etc/mke2fs.conf").write_text(
        "[defaults]\n\tbase_features = sparse_super,large_file,filetype,resize_inode,dir_index\n"
        "[fs_types]\n\text4 = {\n\t\tfeatures = has_journal,extent,flex_bg,64bit\n\t}\n"
    )
    pak = tmp_path / "rootfs.pak"
    subprocess.run(
        ["mksquashfs", str(top), str(pak), "-quiet", "-noappend", "-all-root"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    root = lls.Root.__new__(lls.Root)
    root.layers = [((str(pak), 0), "")]
    root.mounts = {}
    root._binds = []
    return root


def test_needed():
    assert factory.needed(("o22", "queue"))
    assert not factory.needed(("k8hp", "ombre"))
    assert not factory.needed(("m14", "beehive"))


@pytest.mark.skipif(not TOOLS, reason="needs squashfs-tools, e2fsprogs and unshare")
def test_apps_image(tmp_path, monkeypatch):
    names = ["thf_config_v1.05_all.json", "trigger_lge_en-US_v1.2_hi_lg_f54306d5.snsr"]
    root = rootfs(tmp_path, names)
    monkeypatch.setattr(factory, "firmware_root", lambda *a: root)
    out = str(tmp_path / "appstore.img")
    files = factory.apps_image("FW", ("o22", "queue"), None, out)
    assert files == [f"{factory.PRELOAD_SNSR}/{n}" for n in names]
    assert os.path.getsize(out) == factory.SIZE
    listing = subprocess.run(
        ["debugfs", "-R", f"ls -l /{factory.PRELOAD_SNSR}", out],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for n in names:
        line = next(x for x in listing.splitlines() if x.split()[-1:] == [n])
        inode, mode, _, uid, gid, size = line.split()[:6]
        assert (mode, uid, gid, int(size)) == ("100644", "0", "0", len(n) * 100)
    got = subprocess.run(
        ["debugfs", "-R", f"cat /{factory.PRELOAD_SNSR}/{names[1]}", out],
        capture_output=True,
        check=True,
    ).stdout
    assert got == names[1].encode() * 100
    # the firmware's own mke2fs.conf: its features, not the host's defaults
    header = subprocess.run(["dumpe2fs", "-h", out], capture_output=True, text=True).stdout
    features = next(x for x in header.splitlines() if x.startswith("Filesystem features"))
    assert "metadata_csum" not in features and "64bit" in features


@pytest.mark.skipif(not TOOLS, reason="needs squashfs-tools, e2fsprogs and unshare")
def test_no_model(tmp_path, monkeypatch):
    root = rootfs(tmp_path, ["readme.txt"])
    monkeypatch.setattr(factory, "firmware_root", lambda *a: root)
    with pytest.raises(factory.Error, match="no wake-word model"):
        factory.apps_image("FW", ("o22", "queue"), None, str(tmp_path / "a.img"))
