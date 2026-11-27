# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The macadr eMMC partition as a set's factory leaves it: the board MAC and the LGUDID.

LG's sets keep two factory records in the macadr partition (p10 from WT1H's table on, 0x40000
bytes; docs/research/lgudid.md):

  at 0         the MAC image libwca-support's factory set_mac_address writes (142 bytes): a
               12-byte header (byte 7 is 0x84), the six MAC bytes and two zeros eight times,
               0xFF to byte 139, then the sum of bytes 0..139 (mod 256) twice
  at 0x3fc00   the LGUDID record (92 bytes, Release.lgudid's offset) libsecretagent's
               SM_SA_GetLGUDID and securitymanager read: "LGUDID", u16 version 1, a random
               UUID (the namespace), the MAC as 17 characters, three zero bytes, the LGUDID, and
               the SHA-256 of bytes 8..59. The UUIDs are stored as LG's struct: the first three
               fields little-endian, the last eight bytes as written. Without a valid record each
               process makes a UUIDv5 of the namespace and the MAC, prints both and writes them,
               which fails where there is no partition: a new LGUDID every time

The rest of the partition is zeros. The VM's LGUDID and namespace are its identity's
(vm.identity.LGUDID); prepare.py builds the image, which the launcher attaches as the partition.

  python3 -m beanjar.board.macadr IMAGE [OFFSET]    print the MAC and the LGUDID record of an image
"""

import hashlib
import struct
import sys
import uuid

MAGIC = b"LGUDID"
VERSION = 1
RECORD = 92
HASHED = slice(8, 60)
MAC_IMAGE = 142
MAC_HEADER = bytes(7) + b"\x84" + bytes(4)
MAC_COPIES = 8
DEFAULT_OFFSET = 0x3FC00


class Error(Exception):
    pass


def _mac_bytes(mac):
    parts = str(mac).split(":")
    if len(parts) != 6 or not all(len(p) == 2 for p in parts):
        raise Error(f"MAC {mac!r}: six hex octets with colons")
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError:
        raise Error(f"MAC {mac!r}: six hex octets with colons") from None


def mac_text(mac):
    """The MAC as LG's records hold it: 17 characters, upper case, colons."""
    return ":".join(f"{b:02X}" for b in _mac_bytes(mac))


def new_ids(mac):
    """(namespace, lgudid) as a set's factory makes them: a random UUID, and the UUIDv5 of it and
    the MAC's text."""
    namespace = uuid.uuid4()
    return str(namespace), str(uuid.uuid5(namespace, mac_text(mac)))


def _lg_uuid(value):
    """LG's 16 bytes of the UUID text value (SM_UTIL_FormatingUUID's order)."""
    u = uuid.UUID(value)
    return u.bytes_le


def _from_lg(raw):
    return str(uuid.UUID(bytes_le=bytes(raw)))


def record(lgudid, namespace, mac):
    """The 92-byte LGUDID record."""
    rec = bytearray(RECORD)
    rec[0:6] = MAGIC
    struct.pack_into("<H", rec, 6, VERSION)
    rec[8:24] = _lg_uuid(namespace)
    rec[24:41] = mac_text(mac).encode()
    rec[44:60] = _lg_uuid(lgudid)
    rec[60:92] = hashlib.sha256(rec[HASHED]).digest()
    return bytes(rec)


def decode_record(rec):
    """{lgudid, namespace, mac} of a valid record, or None: what libsecretagent accepts (the magic
    and the hash; it reads neither the version nor the MAC)."""
    rec = bytes(rec[:RECORD])
    if len(rec) < RECORD or rec[0:6] != MAGIC:
        return None
    if hashlib.sha256(rec[HASHED]).digest() != rec[60:92]:
        return None
    return {
        "lgudid": _from_lg(rec[44:60]),
        "namespace": _from_lg(rec[8:24]),
        "mac": rec[24:41].decode("latin-1"),
    }


def mac_image(mac):
    """The 142-byte MAC image at the partition's start (libwca-support set_eth_mac_address_emmc)."""
    img = bytearray(b"\xff" * MAC_IMAGE)
    img[0:12] = MAC_HEADER
    entry = _mac_bytes(mac) + bytes(2)
    for i in range(MAC_COPIES):
        img[12 + 8 * i : 20 + 8 * i] = entry
    s = sum(img[:140]) & 0xFF
    img[140] = img[141] = s
    return bytes(img)


def decode_mac(img):
    """The MAC of a valid MAC image (its checksum right), lower case, or None."""
    img = bytes(img[:MAC_IMAGE])
    if len(img) < MAC_IMAGE or img[0:12] != MAC_HEADER:
        return None
    s = sum(img[:140]) & 0xFF
    if img[140] != s or img[141] != s:
        return None
    return ":".join(f"{b:02x}" for b in img[12:18])


def image(size, lgudid, namespace, mac, offset=DEFAULT_OFFSET):
    """The partition's bytes: the MAC image, zeros, the LGUDID record at offset."""
    if offset < MAC_IMAGE or offset + RECORD > size:
        raise Error(f"an LGUDID record at {offset:#x} does not fit a {size}-byte macadr partition")
    data = bytearray(size)
    data[0:MAC_IMAGE] = mac_image(mac)
    data[offset : offset + RECORD] = record(lgudid, namespace, mac)
    return bytes(data)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not 1 <= len(argv) <= 2:
        print("usage: python3 -m beanjar.board.macadr IMAGE [OFFSET]", file=sys.stderr)
        return 2
    offset = int(argv[1], 0) if len(argv) > 1 else DEFAULT_OFFSET
    with open(argv[0], "rb") as f:
        head = f.read(MAC_IMAGE)
        f.seek(offset)
        rec = f.read(RECORD)
    print(f"mac: {decode_mac(head) or 'none'}")
    r = decode_record(rec)
    if r is None:
        print(f"LGUDID record at {offset:#x}: none")
    else:
        print(f"LGUDID: {r['lgudid']} (namespace {r['namespace']}, mac {r['mac']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
