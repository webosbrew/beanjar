# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""rtk.ko's stand-in secure-store TA (guest/kernel/modules/rtk/tee-ta.c) in Python: the reference the tee
step (tvsuite.judge_tee) checks the guest's answers against, from the VM's hardware-unique key
(identity/tee-huk, beanjar.vm.keys.ensure_tee_huk). Every key is HMAC-SHA256(HUK, label); a keystream
block is HMAC-SHA256(key, nonce || le32(block)), XORed over the data. Nothing here knows a key of a
set or a firmware: what the guest answers either follows from the VM's own HUK or the check fails."""

import hashlib
import hmac
import struct

MAGIC = b"EMSS"
BLOB_HEAD, BLOB_TAG = 24, 32  # "EMSS", le32 length, nonce[16]; HMAC-SHA256
BLOB_EXTRA = 176  # LG's blob is the data and 176 bytes: ours too, zeros before the tag
BOX_HEAD = 32  # the 0x20/0x40 container's: nonce[16], tag[16]
STORE_SLOT = 0x1C  # the store image's MAC slot (32 bytes), zeros in its MAC


def _mac(key, *parts):
    h = hmac.new(key, digestmod=hashlib.sha256)
    for p in parts:
        h.update(p)
    return h.digest()


def key_of(huk, label):
    return _mac(huk, label.encode())


def stream(key, nonce, data):
    out = bytearray(data)
    for off in range(0, len(out), 32):
        block = _mac(key, nonce, struct.pack("<I", off // 32))
        for i in range(min(32, len(out) - off)):
            out[off + i] ^= block[i]
    return bytes(out)


def wrap(huk, data, nonce):
    """MakeKey / MakeClearKey's blob of data with this nonce (the TA's is random)."""
    head = MAGIC + struct.pack("<I", len(data)) + nonce
    body = (
        head
        + stream(key_of(huk, "sstore wrap enc"), nonce, data)
        + bytes(BLOB_EXTRA - BLOB_HEAD - BLOB_TAG)
    )
    return body + _mac(key_of(huk, "sstore wrap mac"), body)


def unwrap(huk, blob):
    """GetSecureKey / GetSocSstoreKey: the data of a blob made with this HUK, or None."""
    if len(blob) < BLOB_EXTRA or blob[:4] != MAGIC:
        return None
    n = struct.unpack("<I", blob[4:8])[0]
    if n != len(blob) - BLOB_EXTRA:
        return None
    body = blob[:-BLOB_TAG]
    if not hmac.compare_digest(_mac(key_of(huk, "sstore wrap mac"), body), blob[-BLOB_TAG:]):
        return None
    return stream(key_of(huk, "sstore wrap enc"), blob[8:24], blob[BLOB_HEAD : BLOB_HEAD + n])


def cipher_key(huk, header, inputs):
    """The key of an Encryption/Decryption call: its 20-byte header's key id and mode, its inputs."""
    return _mac(key_of(huk, "sstore cipher"), header[:8], inputs)


def cipher(huk, header, inputs, data):
    """Commands 0x2 and 0x1: the same keystream both ways."""
    return stream(cipher_key(huk, header, inputs), b"", data)


def box_open(huk, header, inputs, box):
    """Command 0x40 of a 0x20 container: (its first 32 bytes, the plaintext), or None if the tag is
    not this HUK's."""
    if len(box) < BOX_HEAD:
        return None
    key = cipher_key(huk, header, inputs)
    nonce, tag, ct = box[:16], box[16:32], box[32:]
    if not hmac.compare_digest(_mac(key, b"tag", nonce, ct)[:16], tag):
        return None
    return box[:BOX_HEAD], stream(key, nonce, ct)


def store_mac(huk, image):
    """Command 0x8: the store image's MAC, its slot at +0x1c as zeros."""
    return _mac(
        key_of(huk, "sstore store mac"), image[:STORE_SLOT], bytes(32), image[STORE_SLOT + 32 :]
    )


def hmac_of(huk, sub, p1, p2):
    """Commands 0x80 / 0x100: the HMAC of p1 || p2 under the sub-op."""
    return _mac(key_of(huk, "sstore hmac"), struct.pack("<I", sub), p1, p2)


def new_data(huk, value, data):
    """Command 0x800: le32(len) and the data under the value's key."""
    return struct.pack("<I", len(data)) + stream(key_of(huk, f"sstore new data {value}"), b"", data)
