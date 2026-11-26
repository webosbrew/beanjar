#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The prebuilt programs: packaging/vendor.toml's pins, meson/vendor.py's download check and
unpacking (with a local tarball, no network), and how packaging/debian/build.py ships the trees and
merges their Depends."""

import hashlib
import importlib.util
import io
from pathlib import Path
import shutil
import sys
import tarfile

import pytest

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "meson"))
import vendor  # noqa: E402


def load_build():
    spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    return build


def test_pins():
    pins = vendor.pins()
    assert set(pins) == {"qemu", "qemu-user", "ffmpeg", "ffmpeg-helper", "neatvnc", "testguest"}
    for program, pin in pins.items():
        assert pin["tag"] == f"{program}-{pin['version']}"
        assert pin["repository"] == "mariotaku/beanjar-vendor"
        for arch in vendor.guests(pin) or vendor.ARCHS:
            int(pin[arch], 16)
    assert pins["testguest"]["guest"] == "armel"
    assert pins["qemu-user"]["guest"] == ["arm64", "amd64"]
    name, url = vendor.asset(pins["qemu"], "qemu", "arm64")
    assert name == f"qemu-{pins['qemu']['version']}-arm64.tar.xz"
    # the '+' of a version is %2B in the URL, in the tag and in the file name
    assert "+" not in url and url.startswith(
        "https://github.com/mariotaku/beanjar-vendor/releases/download/qemu-"
    )


def tarball(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as t:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type, info.linkname = tarfile.SYMTYPE, "libfoo.so.1.2"
                t.addfile(info)
            else:
                info.size = len(content)
                t.addfile(info, io.BytesIO(content))
    return data.getvalue()


def test_tree_checks_and_unpacks(tmp_path, monkeypatch):
    pin = tmp_path / "vendor.toml"
    blob = tarball(
        {
            "BUILDINFO": b"Program: foo\nVersion: 1.0+beanjar1\nArchitecture: amd64\n"
            b"Sources:\n a.tar.gz 00\nDepends: libc6 (>= 2.38)\n",
            "lib/libfoo.so.1.2": b"\x7fELF",
            "lib/libfoo.so.1": None,
        }
    )
    digest = hashlib.sha256(blob).hexdigest()
    pin.write_text(
        f'repository = "x/y"\n[foo]\ntag = "foo-1.0+beanjar1"\namd64 = "{digest}"\n'
        f'arm64 = "{"0" * 64}"\n'
    )
    cache = tmp_path / "cache"
    (cache / "download").mkdir(parents=True)
    (cache / "download/foo-1.0+beanjar1-amd64.tar.xz").write_bytes(blob)
    tree = vendor.tree("foo", "amd64", pin, cache)
    assert tree == cache / "foo-1.0+beanjar1-amd64"
    assert (tree / "lib/libfoo.so.1").readlink() == Path("libfoo.so.1.2")
    info = vendor.buildinfo(tree / "BUILDINFO")
    assert info["Depends"] == "libc6 (>= 2.38)" and info["Sources"] == "\na.tar.gz 00"
    # unpacked once: a second call takes the tree as it is
    (tree / "marker").write_text("")
    assert (vendor.tree("foo", "amd64", pin, cache) / "marker").exists()
    # a tarball that is not the pinned one is never unpacked: it is downloaded again, and what
    # the download brings must have the pinned hash too
    shutil.rmtree(tree)
    (cache / "download/foo-1.0+beanjar1-amd64.tar.xz").write_bytes(blob + b"x")
    monkeypatch.setattr(vendor.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(blob + b"y"))
    with pytest.raises(vendor.Error, match="pins"):
        vendor.tree("foo", "amd64", pin, cache)
    assert not tree.exists()
    monkeypatch.setattr(vendor.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(blob))
    assert vendor.tree("foo", "amd64", pin, cache) == tree
    with pytest.raises(vendor.Error, match="no prebuilt"):
        vendor.tree("foo", "riscv64", pin, cache)


def test_guest_pin(tmp_path, capsys):
    """A guest = "arm64" pin (qemu-user's qemu-arm, which runs in the arm64 guest) has an arm64
    tarball only, which every host architecture takes; `vendor.py pinned` lists what is pinned."""
    pin = tmp_path / "vendor.toml"
    blob = tarball(
        {"BUILDINFO": b"Program: bar\nVersion: 2.0+beanjar1\nArchitecture: arm64\n", "bin/x": b"x"}
    )
    digest = hashlib.sha256(blob).hexdigest()
    pin.write_text(
        f'repository = "x/y"\n[bar]\ntag = "bar-2.0+beanjar1"\nguest = "arm64"\narm64 = "{digest}"\n'
    )
    cache = tmp_path / "cache"
    (cache / "download").mkdir(parents=True)
    (cache / "download/bar-2.0+beanjar1-arm64.tar.xz").write_bytes(blob)
    for host in ("amd64", "arm64"):
        assert vendor.tree("bar", host, pin, cache) == cache / "bar-2.0+beanjar1-arm64"
    pin.write_text(f'repository = "x/y"\n[bar]\ntag = "bar-2.0+beanjar1"\narm64 = "{digest}"\n')
    with pytest.raises(vendor.Error, match="no SHA-256 for amd64"):
        vendor.pins(pin)
    vendor.main(["pinned", "qemu-user", "qemu-user:amd64", "qemu", "unpinned"])
    assert capsys.readouterr().out.split() == ["qemu-user", "qemu-user:amd64", "qemu"]


def test_override(tmp_path, monkeypatch):
    """BEANJAR_VENDOR_<PROGRAM> takes a tarball in place of the release, pinned or not; its
    BUILDINFO must name the program."""
    pin = tmp_path / "vendor.toml"
    pin.write_text('repository = "x/y"\n')
    cache = tmp_path / "cache"
    local = tmp_path / "test-guest-1+beanjar1-armel.tar.xz"
    local.write_bytes(
        tarball(
            {"BUILDINFO": b"Program: test-guest\nArchitecture: armel\n", "rootfs.squashfs": b"r"}
        )
    )
    monkeypatch.setenv("BEANJAR_VENDOR_TEST_GUEST", str(local))
    tree = vendor.tree("test-guest", "amd64", pin, cache)
    assert tree == cache / "test-guest-1+beanjar1-armel"
    assert (tree / "rootfs.squashfs").read_bytes() == b"r"
    monkeypatch.setenv("BEANJAR_VENDOR_TEST_GUEST", str(tmp_path / "none.tar.xz"))
    with pytest.raises(vendor.Error, match="no tarball"):
        vendor.tree("test-guest", "amd64", pin, cache)
    monkeypatch.delenv("BEANJAR_VENDOR_TEST_GUEST")
    with pytest.raises(vendor.Error, match="pins no test-guest"):
        vendor.tree("test-guest", "amd64", pin, cache)


def test_merge_depends():
    build = load_build()
    merged = build.merge_depends(
        "libc6 (>= 2.34), libopus0, beanjar (= 1.2)",
        "libavcodec61 (>= 7:7.1.5), libc6 (>= 2.38), libopus0 (>= 1.1), libneatvnc1, "
        "libaml1 (>= 1.0), libpostproc58, libswscale8 (>= 7:7.1), libavif16",
    )
    assert merged == "libc6 (>= 2.38), libopus0 (>= 1.1), beanjar (= 1.2), libavif16"


def test_stage_libraries_and_qemu(tmp_path):
    build = load_build()
    elf = b"\x7fELF" + bytes(14) + (62).to_bytes(2, "little")  # x86-64
    hp = tmp_path / "publish"
    lib = hp / "ffmpeg/lib"
    (lib / "pkgconfig").mkdir(parents=True)
    (lib / "libavcodec.so.61.19.101").write_bytes(elf)
    (lib / "libavcodec.so.61").symlink_to("libavcodec.so.61.19.101")
    (lib / "libavcodec.so").symlink_to("libavcodec.so.61")
    out = tmp_path / "stage/usr/lib/beanjar/ffmpeg/lib"
    build.stage_libraries(hp / "ffmpeg", out)
    assert sorted(p.name for p in out.iterdir()) == ["libavcodec.so.61", "libavcodec.so.61.19.101"]
    assert (out / "libavcodec.so.61").readlink() == Path("libavcodec.so.61.19.101")

    q = hp / "qemu"
    for relative in ("bin/qemu-system-aarch64", "libexec/vhost-user-gpu"):
        (q / relative).parent.mkdir(parents=True, exist_ok=True)
        (q / relative).write_bytes(elf)
    (q / "share/qemu").mkdir(parents=True)
    (q / "share/qemu/efi-virtio.rom").write_bytes(b"rom")
    (q / "share/doc/qemu").mkdir(parents=True)
    (q / "share/doc/qemu/COPYING").write_text("GPL")
    (q / "BUILDINFO").write_text(
        "Program: qemu\nVersion: 10.0.2+beanjar1\nArchitecture: amd64\n"
        "Depends: libc6 (>= 2.38), libepoxy0 (>= 1.3), libegl1, libopengl0\n"
    )
    work = tmp_path / "work"
    root, depends = build.stage_qemu(work, hp, "amd64")
    assert depends == "libc6 (>= 2.38), libepoxy0 (>= 1.3), libegl1, libopengl0"
    for path in (
        "usr/libexec/beanjar/qemu-system-aarch64",
        "usr/libexec/beanjar/vhost-user-gpu",
        "usr/share/beanjar/qemu/efi-virtio.rom",
        "usr/share/doc/beanjar-qemu/COPYING",
        "usr/share/doc/beanjar-qemu/BUILDINFO",
    ):
        assert (root / path).is_file(), path
    copyright = (root / "usr/share/doc/beanjar-qemu/copyright").read_text()
    assert "release qemu-10.0.2+beanjar1" in copyright
    with pytest.raises(SystemExit, match="not qemu for arm64"):
        build.stage_qemu(tmp_path / "other", hp, "arm64")
