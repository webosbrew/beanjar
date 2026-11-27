#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""meson/vendor.py's pins with several guest architectures (qemu-user: arm64 for the arm64 guest
kernel, amd64 for the experimental x86-64 one): PROGRAM is the first, PROGRAM:ARCH another, and
BEANJAR_VENDOR_<PROGRAM>_<ARCH> overrides that one."""

import hashlib
import io
import sys
import tarfile
from pathlib import Path

import pytest

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "meson"))
import vendor  # noqa: E402


def tarball(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def blob(arch):
    return tarball(
        {
            "BUILDINFO": f"Program: bar\nVersion: 2.0+beanjar1\nArchitecture: {arch}\n".encode(),
            "bin/qemu-arm": arch.encode(),
        }
    )


def test_guest_list(tmp_path, capsys, monkeypatch):
    blobs = {arch: blob(arch) for arch in ("arm64", "amd64")}
    pin = tmp_path / "vendor.toml"
    pin.write_text(
        'repository = "x/y"\n[bar]\ntag = "bar-2.0+beanjar1"\nguest = ["arm64", "amd64"]\n'
        + "".join(f'{a} = "{hashlib.sha256(b).hexdigest()}"\n' for a, b in blobs.items())
    )
    cache = tmp_path / "cache"
    (cache / "download").mkdir(parents=True)
    for arch, data in blobs.items():
        (cache / f"download/bar-2.0+beanjar1-{arch}.tar.xz").write_bytes(data)
    for host in ("amd64", "arm64"):
        assert vendor.tree("bar", host, pin, cache) == cache / "bar-2.0+beanjar1-arm64"
        assert vendor.tree("bar:amd64", host, pin, cache) == cache / "bar-2.0+beanjar1-amd64"
    assert (cache / "bar-2.0+beanjar1-amd64/bin/qemu-arm").read_bytes() == b"amd64"
    with pytest.raises(vendor.Error, match="no guest tarball for riscv64"):
        vendor.tree("bar:riscv64", "amd64", pin, cache)
    # each guest architecture needs its hash
    pin.write_text(
        'repository = "x/y"\n[bar]\ntag = "bar-2.0+beanjar1"\nguest = ["arm64", "amd64"]\n'
    )
    with pytest.raises(vendor.Error, match="no SHA-256 for arm64"):
        vendor.pins(pin)
    pin.write_text('repository = "x/y"\n[bar]\ntag = "bar-2.0+beanjar1"\nguest = 1\n')
    with pytest.raises(vendor.Error, match="guest is the guest's architectures"):
        vendor.pins(pin)


def test_guest_override(tmp_path, capsys, monkeypatch):
    """PROGRAM:ARCH takes BEANJAR_VENDOR_<PROGRAM>_<ARCH>, whose BUILDINFO must name that
    architecture; `vendor.py pinned` lists an overridden program, pinned or not."""
    pin = tmp_path / "vendor.toml"
    pin.write_text('repository = "x/y"\n')
    cache = tmp_path / "cache"
    local = tmp_path / "qemu-user-2.0+beanjar1-amd64.tar.xz"
    local.write_bytes(
        tarball({"BUILDINFO": b"Program: qemu-user\nArchitecture: amd64\n", "bin/qemu-arm": b"q"})
    )
    monkeypatch.setenv("BEANJAR_VENDOR_QEMU_USER_AMD64", str(local))
    assert vendor.tree("qemu-user:amd64", "amd64", pin, cache) == (
        cache / "qemu-user-2.0+beanjar1-amd64"
    )
    monkeypatch.delenv("BEANJAR_VENDOR_QEMU_USER_AMD64")
    monkeypatch.setenv("BEANJAR_VENDOR_QEMU_USER_ARM64", str(local))
    with pytest.raises(vendor.Error, match="architecture 'amd64', not 'arm64'"):
        vendor.tree("qemu-user:arm64", "amd64", pin, cache)
    monkeypatch.delenv("BEANJAR_VENDOR_QEMU_USER_ARM64")
    monkeypatch.setattr(vendor, "PIN", pin)
    monkeypatch.setattr(vendor, "pins", lambda path=pin: {})
    vendor.main(["pinned", "qemu-user", "qemu-user:amd64"])
    assert capsys.readouterr().out.split() == []
    monkeypatch.setenv("BEANJAR_VENDOR_QEMU_USER_AMD64", str(local))
    vendor.main(["pinned", "qemu-user", "qemu-user:amd64"])
    assert capsys.readouterr().out.split() == ["qemu-user:amd64"]
