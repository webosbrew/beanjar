# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The X.509 certificates a firmware's kernel has built in: what its webos-verity target checks a
verity table's signature against (the guest kernel's patch 0030, dm-webos-verity.c).

LG builds the keys into the kernel's .builtin_trusted_keys, one per platform: W22O 43.21.89's
kernel has a self-signed "LGE_TV" certificate whose key signs its appackage.pak's table, W22H
43.22.73's another "LGE_TV" (and the "LG webOS TV Verity Root CA") whose key signs its own
(docs/research/w22o-queue-no-signal-black.md). So nothing is kept in the tree: the certificates
come from the VM's own firmware, its kernel.pak, and go to the guest as one DER list, which its
initramfs loads into the target's keyring before webOS starts.

The kernel image is kernel.pak itself, gzip's (W22H) or LG's LZ4P wrapper (W22O), which
epk2extract decodes (lz4p.py's program; nothing of ours decodes LZ4). The certificates are the DER
SEQUENCEs in it shaped as an X.509 v3 Certificate (a TBSCertificate with its explicit version 2,
the signature algorithm and the signature's BIT STRING, ending where the outer length says), as
the kernel's own system_certificate_list holds them; the guest kernel's X.509 parser takes or
refuses each.
"""

import os
import struct
import subprocess
import tempfile
import zlib

from . import lz4p

GZIP_MAGIC = b"\x1f\x8b"
# the start of an X.509 v3 TBSCertificate: SEQUENCE (two length bytes), [0] EXPLICIT INTEGER 2
_TBS = b"\xa0\x03\x02\x01\x02"


class Error(Exception):
    pass


def _seq(data, i):
    """(content start, end) of the DER SEQUENCE at i with a two-byte length, or None."""
    if data[i : i + 2] != b"\x30\x82" or i + 4 > len(data):
        return None
    end = i + 4 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    return (i + 4, end) if end <= len(data) else None


def certificates(image):
    """The DER X.509 certificates in image (bytes), in order."""
    out = []
    i = image.find(b"\x30\x82")
    while i >= 0:
        cert = _seq(image, i)
        tbs = cert and _seq(image, cert[0])
        alg = tbs and image[tbs[1] : tbs[1] + 1] == b"\x30" and tbs[1] + 2 <= cert[1]
        if alg and image[tbs[0] : tbs[0] + len(_TBS)] == _TBS:
            # the algorithm SEQUENCE (short length), then the BIT STRING up to the end
            j = tbs[1] + 2 + image[tbs[1] + 1]
            bits = image[j : j + 1] == b"\x03"
            n = image[j + 1] if bits else 0
            if bits and n == 0x82:
                n = 4 + struct.unpack(">H", image[j + 2 : j + 4])[0]
            elif bits and n == 0x81:
                n = 3 + image[j + 2]
            elif bits:
                n += 2
            if bits and j + n == cert[1]:
                out.append(image[i : cert[1]])
                i = image.find(b"\x30\x82", cert[1])
                continue
        i = image.find(b"\x30\x82", i + 1)
    return out


def kernel_image(pak):
    """The kernel image of a kernel.pak: gunzipped, LZ4P decoded by epk2extract, or as it is."""
    with open(pak, "rb") as f:
        data = f.read()
    if data.startswith(GZIP_MAGIC):
        try:
            return zlib.decompressobj(31).decompress(data)
        except zlib.error as e:
            raise Error(f"{pak}: gzip: {e}") from None
    if not data.startswith(lz4p.MAGIC):
        return data
    try:
        tool = lz4p.epk2extract()
    except lz4p.Error as e:
        raise Error(str(e)) from None
    with tempfile.TemporaryDirectory(prefix=".kernelcerts-") as work:
        # epk2extract writes <input>.unlz4 beside its input: a link in work
        os.symlink(os.path.abspath(pak), os.path.join(work, "kernel"))
        r = subprocess.run(
            [tool, "kernel"], cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        out = os.path.join(work, "kernel.unlz4")
        if r.returncode or not os.path.isfile(out):
            tail = r.stdout.decode(errors="replace").strip().splitlines()[-3:]
            raise Error(f"epk2extract did not decode {pak}: {' / '.join(tail)}")
        with open(out, "rb") as f:
            return f.read()


def write(fw, out):
    """Write the built-in certificates of the firmware directory fw's kernel.pak to out, one DER
    list; how many. Error if it has no kernel.pak or no certificate."""
    pak = os.path.join(fw, "kernel.pak")
    if not os.path.isfile(pak):
        raise Error(f"{fw}: no kernel.pak, whose certificates webos-verity checks against")
    certs = certificates(kernel_image(pak))
    if not certs:
        raise Error(f"{pak}: no built-in X.509 certificate")
    with open(out, "wb") as f:
        f.write(b"".join(certs))
    return len(certs)
