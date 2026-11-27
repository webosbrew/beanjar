# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The macadr partition (macadr.py): the LGUDID record and MAC image a set's factory writes, the
VM's LGUDID in its identity (vmidentity.py), the prepared image (prepare.py) and its disk and node
(targets.py, partmap.py, qemu.py)."""

import hashlib
import os
import struct
import tempfile
import types
import unittest
import uuid

from beanjar.board import macadr, partmap, prepare
from beanjar.firmware import targets
from beanjar import vmidentity

from launchkit import fake_map

MAC = "a8:23:fe:12:34:56"
O22_QUEUE = ("o22", "queue")


def lg_format(raw):
    """SM_UTIL_FormatingUUID: "%08x-%04x-%04x-%02x%02x-" and six "%02x" of the 16 bytes, the first
    field a little-endian u32, the next two little-endian u16s, the rest bytes in order."""
    a, b, c = struct.unpack_from("<IHH", raw)
    return f"{a:08x}-{b:04x}-{c:04x}-{raw[8]:02x}{raw[9]:02x}-" + "".join(
        f"{x:02x}" for x in raw[10:16]
    )


class RecordTest(unittest.TestCase):
    def test_new_ids_as_the_factory(self):
        """The LGUDID is the UUIDv5 of a random namespace and the MAC's 17 characters."""
        ns, lgudid = macadr.new_ids(MAC)
        self.assertEqual(uuid.UUID(ns).version, 4)
        self.assertEqual(lgudid, str(uuid.uuid5(uuid.UUID(ns), "A8:23:FE:12:34:56")))
        self.assertNotEqual(macadr.new_ids(MAC), (ns, lgudid))

    def test_record_as_libsecretagent_reads_it(self):
        ns, lgudid = macadr.new_ids(MAC)
        rec = macadr.record(lgudid, ns, MAC)
        self.assertEqual(len(rec), 92)
        self.assertEqual(struct.unpack_from("<IH", rec), (0x4455474C, 0x4449))  # "LGUD", "ID"
        self.assertEqual(struct.unpack_from("<H", rec, 6)[0], 1)
        self.assertEqual(rec[24:41], b"A8:23:FE:12:34:56")
        self.assertEqual(rec[41:44], bytes(3))
        self.assertEqual(rec[60:92], hashlib.sha256(rec[8:60]).digest())
        self.assertEqual(lg_format(rec[44:60]), lgudid, "what getIDs prints")
        self.assertEqual(lg_format(rec[8:24]), ns)
        self.assertEqual(
            macadr.decode_record(rec),
            {"lgudid": lgudid, "namespace": ns, "mac": "A8:23:FE:12:34:56"},
        )
        broken = bytearray(rec)
        broken[50] ^= 1
        self.assertIsNone(macadr.decode_record(bytes(broken)), "the hash covers the LGUDID")
        self.assertIsNone(macadr.decode_record(bytes(92)))
        self.assertIsNone(macadr.decode_record(b"\xff" * 92))

    def test_mac_image(self):
        """libwca-support's 142-byte image: header, the MAC eight times, 0xFF, its sum twice."""
        img = macadr.mac_image(MAC)
        self.assertEqual(len(img), 142)
        self.assertEqual(img[:12], bytes(7) + b"\x84" + bytes(4))
        for i in range(8):
            self.assertEqual(img[12 + 8 * i : 20 + 8 * i], bytes.fromhex("a823fe123456") + bytes(2))
        self.assertEqual(img[76:140], b"\xff" * 64)
        self.assertEqual(img[140], sum(img[:140]) % 256)
        self.assertEqual(img[141], img[140])
        self.assertEqual(macadr.decode_mac(img), MAC)
        with self.assertRaises(macadr.Error):
            macadr.mac_image("a8:23:fe:12:34")

    def test_image(self):
        ns, lgudid = macadr.new_ids(MAC)
        data = macadr.image(0x40000, lgudid, ns, MAC, 0x3FC00)
        self.assertEqual(len(data), 0x40000)
        self.assertEqual(data[:142], macadr.mac_image(MAC))
        self.assertEqual(data[0x3FC00 : 0x3FC00 + 92], macadr.record(lgudid, ns, MAC))
        self.assertFalse(any(data[142:0x3FC00]) or any(data[0x3FC00 + 92 :]), "the rest zeros")
        with self.assertRaises(macadr.Error):
            macadr.image(0x3FC00, lgudid, ns, MAC, 0x3FC00)


class IdentityTest(unittest.TestCase):
    def test_made_once_kept_renewed(self):
        """ensure makes the LGUDID once and keeps it; a new identity (renew_keys) has another, and
        clone leaves the old one out of its copy (renewed)."""
        with tempfile.TemporaryDirectory() as d:
            said = []
            ids = vmidentity.ensure_lgudid(d, MAC, said.append)
            self.assertIn("made a new LGUDID", said[0])
            self.assertEqual(os.stat(os.path.join(d, vmidentity.LGUDID)).st_mode & 0o777, 0o600)
            self.assertEqual(vmidentity.ensure_lgudid(d, "02:00:00:00:00:01", said.append), ids)
            self.assertEqual(vmidentity.read_lgudid(d), ids)
            self.assertEqual(len(said), 1)
            self.assertEqual(ids[0], str(uuid.uuid5(uuid.UUID(ids[1]), "A8:23:FE:12:34:56")))
            self.assertIn(vmidentity.LGUDID, vmidentity.renewed())
            new = vmidentity.write_lgudid(d, MAC)
            self.assertNotEqual(new, ids)
            self.assertEqual(vmidentity.read_lgudid(d), new)
            with open(os.path.join(d, vmidentity.LGUDID), "w") as f:
                f.write("lgudid = 'not one'\n")
            self.assertIsNone(vmidentity.read_lgudid(d))
            with open(os.path.join(d, vmidentity.LGUDID), "w") as f:
                f.write("not toml [")
            self.assertIsNone(vmidentity.read_lgudid(d))
            self.assertNotEqual(vmidentity.ensure_lgudid(d, MAC), new)


class TargetsTest(unittest.TestCase):
    def test_releases(self):
        """goldilocks2 on read the LGUDID from p10's 0x3fc00; afro (WT1H) has no reader. Each such
        target's own assembled map has the partition at that node."""
        self.assertIsNone(targets.release(("h13", "afro")).lgudid)
        self.assertEqual(targets.release(O22_QUEUE).lgudid, targets.Lgudid("macadr", 10, 0x3FC00))
        self.assertEqual(targets.partition_disks(O22_QUEUE)["macadr"], targets.MACADR_DISK)
        for target in targets.all_targets():
            r = targets.release(target)
            if not r.lgudid or not isinstance(r.partmap, targets.PartmapLayout):
                continue
            names = [p.name for p in r.partmap.parts]
            self.assertEqual(
                names.index(r.lgudid.partition) + 1, r.lgudid.number, targets.name(target)
            )

    def test_node(self):
        """The release's LGUDID partition gets its node, the macadr disk."""
        names = ["p%d" % i for i in range(1, 10)] + ["macadr"]
        m = fake_map(names + list(targets.partition_disks(O22_QUEUE)))
        self.assertIn((10, targets.MACADR_DISK), partmap.nodes(m, O22_QUEUE))
        self.assertEqual(partmap.disk_sizes(m, O22_QUEUE)[targets.MACADR_DISK], 1 << 20)


class PrepareTest(unittest.TestCase):
    def job(self, top, ident):
        return types.SimpleNamespace(target=O22_QUEUE, identity_dir=ident, mac=MAC)

    def test_image(self):
        """prepare's macadr image: the map's size, the identity's LGUDID (made if missing), the
        MAC; refused where the map has the partition at another node than the firmware opens."""
        with tempfile.TemporaryDirectory() as top:
            ident = os.path.join(top, "identity")
            names = ["p%d" % i for i in range(1, 10)] + ["macadr", "partinfo"]
            info = os.path.join(top, "partinfo.img")
            with open(info, "wb") as f:
                f.write(partmap.image(partmap.encode(fake_map(names, {"macadr": 0x40000}))))
            out = os.path.join(top, "macadr.img")
            said = []
            prepare.macadr_image(self.job(top, ident), info, out, said.append)
            lgudid, ns = vmidentity.read_lgudid(ident)
            with open(out, "rb") as f:
                data = f.read()
            self.assertEqual(data, macadr.image(0x40000, lgudid, ns, MAC))
            self.assertEqual(len(said), 1, "made, said")
            prepare.macadr_image(self.job(top, ident), info, out, said.append)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), data, "the same LGUDID again")
            with open(info, "wb") as f:
                f.write(
                    partmap.image(partmap.encode(fake_map(["macadr"] + names[:-2] + ["partinfo"])))
                )
            with self.assertRaisesRegex(prepare.Error, "p1; the firmware opens /dev/mmcblk0p10"):
                prepare.macadr_image(self.job(top, ident), info, out)


if __name__ == "__main__":
    unittest.main()
