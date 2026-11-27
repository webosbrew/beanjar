#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The adaptive partition, appackage.pak: queue's optional pak, imported and listed in the
manifest like the other paks where the firmware has it, and nowhere else; and the firmware
kernel's built-in certificates its webos-verity table is checked against (kernelcerts.py)."""

import gzip
import shutil
import subprocess
from pathlib import Path

import pytest
from beanjar import firmware, kernelcerts, prepare, targets

QUEUE = ("o22", "queue")
# W22O 43.21.89's paks as the shared collection keeps them (ARIB build), without appackage.pak
PAKS = [
    "rootfs", "bsppart", "otncabi-arib", "otycabi-arib", "fonts-arib", "license", "lxboot",
    "kernel", "tzfw", "intmicom",
]  # fmt: skip


def queue_firmware(tmp, name, adaptive):
    d = Path(tmp) / name
    d.mkdir()
    for p in PAKS + (["appackage"] if adaptive else []):
        (d / (p + ".pak")).write_bytes(p.encode())
    return str(d)


def test_optional_pak(tmp_path):
    """queue attaches appackage.pak where the firmware has it, read by no target that has none."""
    with_ap = queue_firmware(tmp_path, "with", True)
    without = queue_firmware(tmp_path, "without", False)
    assert "appackage" in targets.pak_files(QUEUE, with_ap)
    assert "appackage" not in targets.pak_files(QUEUE, without)
    assert targets.adaptive_partition(QUEUE, with_ap)
    assert not targets.adaptive_partition(QUEUE, without)
    ap = next(p for p in targets.paks(QUEUE, with_ap) if p.file == "appackage")
    assert (ap.serial, ap.mount, ap.optional) == ("appackage", None, True)
    # a release that ships none (number1, the W22O builds before qilian) does not name it
    assert "appackage" not in targets.pak_files(("o22", "number1"), with_ap)


@pytest.mark.parametrize("adaptive", [True, False])
def test_import_and_manifest(tmp_path, adaptive):
    """The import (firmware.place) takes appackage.pak with the other paks, the manifest's paks
    (firmware.paks_in) list it, and a firmware without it is complete as it was."""
    src = queue_firmware(tmp_path, "src", adaptive)
    firmware.check_paks(src, QUEUE)  # not required: no refusal either way
    dest = tmp_path / "own"
    dest.mkdir()
    firmware.place(src, str(dest), QUEUE, "link", lambda _: None)
    want = sorted(p + ".pak" for p in PAKS + (["appackage"] if adaptive else []))
    assert sorted(f.name for f in dest.iterdir()) == want
    assert sorted(firmware.paks_in(str(dest), QUEUE)) == want
    assert (dest / "appackage.pak").exists() == adaptive
    if adaptive:
        assert (dest / "appackage.pak").samefile(Path(src) / "appackage.pak")


def openssl_cert(tmp, cn):
    """a self-signed DER X.509 certificate of an RSA key, CN cn (openssl)"""
    out = Path(tmp) / f"{cn}.der"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(out) + ".key",
         "-subj", f"/C=KR/O=LG Electronics/CN={cn}", "-days", "1", "-outform", "DER",
         "-out", str(out)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    return out.read_bytes()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_kernel_certificates(tmp_path):
    """The certificates are found among a kernel image's other bytes, in order, a gzipped
    kernel.pak is unpacked, and a kernel.pak with none (or no kernel.pak) is an error."""
    a, b = openssl_cert(tmp_path, "LGE_TV"), openssl_cert(tmp_path, "Verity Root CA")
    # a SEQUENCE that is no certificate before them, as a kernel image has
    image = (
        b"\x7fELF" + bytes(100) + b"\x30\x82\x00\x10" + bytes(16) + a + bytes(7) + b + b"\x30\x82"
    )
    assert kernelcerts.certificates(image) == [a, b]
    fw = tmp_path / "fw"
    fw.mkdir()
    (fw / "kernel.pak").write_bytes(gzip.compress(image))
    out = tmp_path / "certs"
    assert kernelcerts.write(str(fw), str(out)) == 2
    assert out.read_bytes() == a + b
    (fw / "kernel.pak").write_bytes(gzip.compress(bytes(4096)))
    with pytest.raises(kernelcerts.Error, match="no built-in X.509 certificate"):
        kernelcerts.write(str(fw), str(out))
    (fw / "kernel.pak").unlink()
    with pytest.raises(kernelcerts.Error, match="no kernel.pak"):
        kernelcerts.write(str(fw), str(out))


def certified_firmware(tmp, name, image):
    """a W22O queue firmware directory with appackage.pak and a gzipped kernel.pak of image"""
    fw = Path(queue_firmware(tmp, name, True))
    (fw / "kernel.pak").write_bytes(gzip.compress(image))
    return fw


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_import_keeps_certificates(tmp_path):
    """Import reads the certificates from kernel.pak once and keeps them beside the paks; an entry
    that has the file passes it on without kernel.pak or epk2extract; a firmware without the
    adaptive partition keeps none; an adaptive one with neither file nor kernel.pak is refused."""
    cert = openssl_cert(tmp_path, "LGE_TV")
    fw = certified_firmware(tmp_path, "src", b"\x7fELF" + bytes(8) + cert)
    dest = tmp_path / "own"
    dest.mkdir()
    said = []
    assert firmware.place_kernel_certs(str(fw), str(dest), QUEUE, "link", said.append) == (
        firmware.KERNEL_CERTS
    )
    assert (dest / firmware.KERNEL_CERTS).read_bytes() == cert
    # an entry: the stored file, kernel.pak not needed (a W22O one needs epk2extract)
    entry = Path(queue_firmware(tmp_path, "entry", True))
    (entry / firmware.KERNEL_CERTS).write_bytes(cert)
    (entry / "kernel.pak").write_bytes(lz4p_wrapped())
    use = tmp_path / "use"
    use.mkdir()
    firmware.place_kernel_certs(str(entry), str(use), QUEUE, "link", said.append)
    assert (use / firmware.KERNEL_CERTS).read_bytes() == cert
    # no adaptive partition: nothing kept
    plain = Path(queue_firmware(tmp_path, "plain", False))
    assert firmware.place_kernel_certs(str(plain), str(use), QUEUE, "link", said.append) is None
    # adaptive, but neither the file nor a readable kernel.pak
    bare = Path(queue_firmware(tmp_path, "bare", True))
    (bare / "kernel.pak").unlink()
    with pytest.raises(firmware.Error, match="no kernel.pak"):
        firmware.place_kernel_certs(str(bare), str(use), QUEUE, "link", said.append)


def lz4p_wrapped():
    from beanjar import lz4p

    return lz4p.MAGIC + bytes(64)


class FakeJob(prepare.Job):
    def __init__(self, fw):
        self.fw, self.fw_name, self.target = str(fw), fw.name, QUEUE


def test_prepare_copies_stored_certificates(tmp_path):
    """Prepare copies the file the manifest names and never decodes kernel.pak (an LZ4P one here
    would need epk2extract); without the file it fails naming the fix, with no fallback."""
    fw = Path(queue_firmware(tmp_path, "fw", True))
    (fw / "kernel.pak").write_bytes(lz4p_wrapped())
    new = tmp_path / "new"
    new.mkdir()
    record = {"derived_files": {}}
    with pytest.raises(prepare.Error, match="import it again"):
        prepare._verity_certs(FakeJob(fw), str(new), record)
    (fw / firmware.KERNEL_CERTS).write_bytes(b"DER")
    (fw / "manifest.toml").write_text(f'kernel_certs = "{firmware.KERNEL_CERTS}"\n')
    prepare._verity_certs(FakeJob(fw), str(new), record)
    assert (new / prepare.VERITY_CERTS).read_bytes() == b"DER"
    assert record["derived_files"] == {prepare.VERITY_CERTS: prepare.VERITY_CERTS}
    # a manifest that names a path outside the directory is no file
    (fw / "manifest.toml").write_text('kernel_certs = "../x"\n')
    with pytest.raises(prepare.Error, match="import it again"):
        prepare._verity_certs(FakeJob(fw), str(new), record)
    # a firmware without the adaptive partition needs none
    plain = Path(queue_firmware(tmp_path, "plain", False))
    prepare._verity_certs(FakeJob(plain), str(new), {"derived_files": {}})
