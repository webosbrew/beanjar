# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The partition map (partmap.py): its table, its disks and the nodes qemu.py gives them."""

import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock


from beanjar.firmware import targets
from beanjar import paths
from beanjar.runtime import qemu
from beanjar.board import partmap

import testunits

from launchkit import fake_map, fake_partmap_build, fake_tree, H13_AFRO, net

HERE = os.path.dirname(os.path.abspath(__file__))


class PartmapTest(unittest.TestCase):
    """partmap.py: LG's partition map (struct partmap_info of lgemmc.c) and the VM's partinfo disk;
    qemu.py's disks and nodes from it. The firmware cases run where the firmwares are (the host's
    collections, testunits.HOST_FIRMWARE_DIRS), else they are skipped."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-partmap-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_crc(self):
        """CRC-32/MPEG-2: its check value, and the tail a real set's partinfo partition has
        (UP7560's dump: 0x3a16a19a over its 12368 bytes; the bytes are not kept here)."""
        self.assertEqual(partmap.crc32_mpeg2(b"123456789"), 0x0376E6E7)

    def test_encode_decode(self):
        m = fake_map()
        blob = partmap.encode(m)
        self.assertEqual(len(blob), partmap.TABLE_SIZE)
        self.assertEqual(struct.unpack_from("<IIIB", blob), (0x20120716, 0, 0, len(m.entries)))
        self.assertEqual(blob[0x50:0x60].rstrip(b"\0"), b"secureboot")
        self.assertEqual(partmap.decode(blob), m)
        with self.assertRaisesRegex(partmap.Error, "magic"):
            partmap.decode(bytes(partmap.TABLE_SIZE))

    def test_proc_text(self):
        """lgemmc.c's proc_partinfo_show: the header, then part%d: %8.8llx %8.8llx "%s" for all but the
        last entry; more than 8 digits where the number needs them."""
        m = fake_map(("a", "b"), {"a": 0x40000, "b": 0x123456789})
        self.assertEqual(
            partmap.proc_text(m),
            'dev:    offset   size  name\npart1: 00000000 00040000 "a"\n'
            'part2: 00040000 123456789 "b"\n',
        )

    def test_image(self):
        blob = partmap.encode(fake_map())
        img = partmap.image(blob)
        self.assertEqual(len(img), 0x40000)
        self.assertEqual(img[: partmap.TABLE_SIZE], blob)
        self.assertEqual(
            struct.unpack_from("<I", img, partmap.TABLE_SIZE)[0], partmap.crc32_mpeg2(blob)
        )
        self.assertEqual(set(img[partmap.TABLE_SIZE + 4 :]), {0})

    def test_nodes(self):
        """A name's node is its first partition (sedata's A copy); mapbak shares partinfo's disk; a
        name the map lacks is refused."""
        m = fake_map()
        nodes = dict(
            (serial, n) for n, serial in partmap.nodes(m, H13_AFRO) if serial != "partinfo"
        )
        self.assertEqual((nodes["sedata"], nodes["cmn_data"], nodes["appstore"]), (7, 16, 17))
        self.assertEqual([n for n, s in partmap.nodes(m, H13_AFRO) if s == "partinfo"], [2, 3])
        self.assertEqual(partmap.disk_sizes(m, H13_AFRO)["partinfo"], 0x40000)
        with self.assertRaisesRegex(partmap.Error, "no partition smartkey"):
            partmap.nodes(
                fake_map(
                    (
                        "partinfo",
                        "mapbak",
                        "authcxt",
                        "nvram",
                        "sestore",
                        "sedata",
                        "rootfs",
                        "tvservice",
                        "otncabi",
                        "otycabi",
                        "fonts",
                        "db8",
                        "data",
                        "apps",
                    )
                ),
                H13_AFRO,
            )

    def test_same_layout(self):
        m = fake_map()
        flags = m._replace(entries=[e._replace(used=0, filesize=7) for e in m.entries])
        self.assertTrue(partmap.same_layout(m, flags))
        renamed = m._replace(
            entries=[e._replace(name="x" if e.name == "db8" else e.name) for e in m.entries]
        )
        self.assertFalse(partmap.same_layout(m, renamed))

    def test_device_size(self):
        """The whole eMMC's size is the reserved entry's offset (the end of every partition)."""
        m = fake_map()
        self.assertEqual(partmap.device_size(m), m.entries[-1].offset)
        self.assertEqual(partmap.device_size(m), sum(e.size for e in m.entries[:-1]))

    def test_dm_segments(self):
        """The dm-linear table of the whole-disk /dev/mmcblk0 (firmware.updatable): the whole address
        range maps into the scratch overlay at its own offset, so the updater's raw writes to the
        inactive bank are discarded and no real (mounted) backing disk is stacked under dm."""
        m = fake_map()
        segs = partmap.dm_segments(m, H13_AFRO, "ov")
        total = partmap.device_size(m) // partmap.SECTOR
        self.assertEqual(segs, [(0, total, "ov", 0)])
        # the words carry the same, start:len:serial:src
        self.assertEqual(partmap.emmc_table_words(m, H13_AFRO, "ov"), [f"0:{total}:ov:0"])
        # a map missing a partition the release backs is still caught (nodes' check)
        short = fake_map(tuple(n for n in (e.name for e in m.entries[:-1]) if n != "nvram"))
        with self.assertRaisesRegex(partmap.Error, "no partition nvram"):
            partmap.dm_segments(short, H13_AFRO, "ov")

    def test_dm_segments_unaligned(self):
        """A whole-disk size that is not a multiple of a sector is refused."""
        m = fake_map(sizes={"nvram": (1 << 20) + 1})
        self.assertNotEqual(partmap.device_size(m) % partmap.SECTOR, 0)
        with self.assertRaisesRegex(partmap.Error, "device size"):
            partmap.dm_segments(m, H13_AFRO, "ov")

    def test_overlay_nodes(self):
        """The overlay-backed per-partition nodes (firmware.updatable): exactly the kernel-registered
        partitions nodes() does not back with a real disk — secureboot (p1) and the inactive sedata
        bank (p8, the second sedata entry) in afro's fake map — each a slice of the overlay at the
        partition's absolute offset and size, in sectors. The active bank, partinfo/mapbak and the
        state disks (all backed) are not listed, so they are never remapped onto the overlay."""
        m = fake_map()
        backed = {n for n, _ in partmap.nodes(m, H13_AFRO)}
        self.assertNotIn(1, backed)  # secureboot: no disk of ours
        self.assertNotIn(8, backed)  # the inactive A/B sedata bank
        self.assertIn(7, backed)  # the active sedata bank keeps its real disk
        ents = {n: e for n, e in partmap.partitions(m)}
        self.assertEqual(
            partmap.overlay_nodes(m, H13_AFRO),
            [
                (1, ents[1].offset // partmap.SECTOR, ents[1].size // partmap.SECTOR),
                (8, ents[8].offset // partmap.SECTOR, ents[8].size // partmap.SECTOR),
            ],
        )
        # the words carry the same, N:start:length
        self.assertEqual(
            partmap.emmc_part_words(m, H13_AFRO),
            [
                f"1:0:{ents[1].size // partmap.SECTOR}",
                f"8:{ents[8].offset // partmap.SECTOR}:{ents[8].size // partmap.SECTOR}",
            ],
        )
        # a slice's start matches the whole-disk address: a write there through /dev/mmcblk0 and a
        # read of the pN node are the same overlay bytes
        for n, start, length in partmap.overlay_nodes(m, H13_AFRO):
            self.assertEqual(start, ents[n].offset // partmap.SECTOR)
        # a map missing a partition the release backs is still caught (nodes' check)
        short = fake_map(tuple(n for n in (e.name for e in m.entries[:-1]) if n != "nvram"))
        with self.assertRaisesRegex(partmap.Error, "no partition nvram"):
            partmap.overlay_nodes(short, H13_AFRO)

    def test_overlay_nodes_unaligned(self):
        """A partition whose offset or size is not a multiple of a sector is refused."""
        m = fake_map(sizes={"secureboot": (1 << 20) + 1})
        with self.assertRaisesRegex(partmap.Error, "not a multiple"):
            partmap.overlay_nodes(m, H13_AFRO)

    def test_shipped_image(self):
        """W21A ships partinfo.pak; its table is preserved, and malformed input is refused."""
        target = ("lm21a", "kisscurl")
        path = os.path.join(self.tmp, "partinfo.pak")
        blob = partmap.encode(fake_map())
        with open(path, "wb") as f:
            f.write(partmap.image(blob))
        self.assertEqual(partmap.table(self.tmp, target), blob)
        with open(path, "wb") as f:
            f.write(blob[:-1])
        with self.assertRaisesRegex(partmap.Error, "no partition map"):
            partmap.table(self.tmp, target)
        with open(path, "wb") as f:
            f.write(bytes(partmap.TABLE_SIZE))
        with self.assertRaisesRegex(partmap.Error, "magic"):
            partmap.table(self.tmp, target)

    def test_jhericurl_layout(self):
        """W20L's assembled table: the indices its fstabs, jail confs and scripts pin, partinfo at
        lgemmc.c's 0x100000, the paks' sizes rounded up to 1 MiB, the state disks' sizes, the header
        from its see-init table (here a made-up one). W21P's is the same with its own paks."""
        for target in (("k6lp", "jhericurl"), ("k7lp", "kisscurl"), ("k8lp", "mullet")):
            with self.subTest(target=targets.name(target)):
                self.assembled_layout(target)
        self.assertEqual(
            sorted(
                {
                    p.size[1]
                    for p in targets.release(("k7lp", "kisscurl")).partmap.parts
                    if not isinstance(p.size, int) and p.size[0] == "pak"
                }
            ),
            [
                "boot",
                "bspfw",
                "bsppart",
                "fonts",
                "intmicom",
                "kernel",
                "license",
                "otncabi-global",
                "otycabi-global",
                "rootfs",
                "tzfw",
            ],
        )

    def test_w22p_layout(self):
        # W22P's stock fstab retains p57-p59 state and p32 fonts; its pak is fonts-global.
        target = ("k8lp", "mullet")
        self.assembled_layout(target)
        self.assertEqual(targets.of_firmware("W22P", "mullet"), target)
        self.assertTrue(targets.in_bring_up(target))
        row = targets.release(target)
        self.assertEqual((row.version, row.init_system, row.cap_last_cap), ("7", "systemd", 37))
        self.assertEqual(next(p.file for p in row.paks if p.serial == "fonts"), "fonts-global")

    def test_w22l_panel_coordinates(self):
        # W22L's DILE_BOARDINFO runtime probe returns panel1366x768, graphic1280x720.
        rtk = targets.soc(("k8ap", "mullet")).modules[0]
        self.assertIn("vsc_screen=1366,768", rtk.params)
        self.assertIn("vt=vivtdev:1280:720:60", rtk.params)

    def test_w22l_h_layout(self):
        for code, chip, state_first, fonts, delsys in (
            ("W22L", "k8ap", 57, "fonts-global", "3+16+1+2+5+6+18,3+16+1+2+5+6+18"),
            ("W22H", "k8hp", 56, "fonts-global", "3+16+1+2+5+6+18,3+16+1+2+5+6+18"),
        ):
            target = (chip, "mullet")
            with self.subTest(code=code):
                self.assembled_layout(target, state_first=state_first)
                self.assertEqual(targets.of_firmware(code, "mullet"), target)
                self.assertTrue(targets.in_bring_up(target))
                self.assertEqual(
                    next(p.file for p in targets.release(target).paks if p.serial == "fonts"), fonts
                )
                tuner = targets.board_module_params(target, "webos_tv")
                self.assertIn("delsys=" + delsys, tuner)
                self.assertIn("version=2.1.0", tuner)
        # W22H's Japanese board keeps the ISDB-T and ISDB-S tuners
        self.assertIn(
            "delsys=9+8,8", targets.board_module_params(("k8hp", "mullet"), "webos_tv", "arib")
        )

    def test_w22h_upgrade_layout(self):
        # Own W22H ponytail/queue fstabs retain p56-p58; ponytail restores fonts-global.
        for release, version, fonts in (
            ("number1", "23", "fonts"),
            ("ombre", "24", "fonts"),
            ("ponytail", "25", "fonts-global"),
            ("queue", "26", "fonts-global"),
        ):
            target = ("k8hp", release)
            with self.subTest(release=release):
                self.assembled_layout(target, state_first=56)
                self.assertEqual(targets.of_firmware("W22H", release), target)
                self.assertTrue(targets.in_bring_up(target))
                self.assertEqual(
                    (targets.release(target).version, targets.release(target).init_system),
                    (version, "systemd"),
                )
                self.assertEqual(
                    next(p.file for p in targets.release(target).paks if p.serial == "fonts"), fonts
                )

    def test_k8lpn2_stock_pins(self):
        """The measured stock fstab pins survive the VM's raw-image allocation."""
        target = ("k8lpn2", "ombre")
        layout = targets.release(target).partmap
        for part in layout.parts:
            if not isinstance(part.size, int) and part.size[0] == "pak":
                with open(os.path.join(self.tmp, part.size[1] + ".pak"), "wb") as f:
                    f.truncate(4096)
        with mock.patch.object(partmap, "file_table", return_value=partmap.encode(fake_map())):
            m = partmap.decode(partmap.table(self.tmp, target))
        entries = dict(partmap.partitions(m))
        with open(os.path.join(HERE, "data", "stock-fstab", "k8lpn2-ombre.fstab")) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                device, mount_point, *_ = line.split()
                index = int(device.removeprefix("/dev/mmcblk0p"))
                name = os.path.basename(mount_point)
                self.assertEqual(entries[index].name, "apps" if name == "appstore" else name)
        self.assertNotIn("platform-plugins", dict(partmap.nodes(m, target)).values())

    def test_untested_stock_pins(self):
        """An untested target's stock fstab pins (data/stock-fstab/<target>.fstab, read from its
        firmware when the target was added) name the partitions of its VM's map, or the disks
        that back them (Release.partitions: LM21's data partition is the cmn_data disk)."""
        d = os.path.join(HERE, "data", "stock-fstab")
        for target in targets.all_targets():
            path = os.path.join(d, targets.name(target) + ".fstab")
            if targets.tested(target):
                continue
            with self.subTest(target=targets.name(target)):
                self.assertTrue(os.path.isfile(path), path)
                layout = targets.release(target).partmap
                tmp = os.path.join(self.tmp, targets.name(target))
                os.makedirs(tmp)
                for part in layout.parts:
                    if not isinstance(part.size, int) and part.size[0] == "pak":
                        with open(os.path.join(tmp, part.size[1] + ".pak"), "wb") as f:
                            f.truncate(4096)
                with mock.patch.object(
                    partmap, "file_table", return_value=partmap.encode(fake_map())
                ):
                    entries = dict(partmap.partitions(partmap.decode(partmap.table(tmp, target))))
                with open(path) as f:
                    for line in f:
                        if line.startswith("#") or not line.strip():
                            continue
                        device, mount_point, *_ = line.split()
                        index = int(device.removeprefix("/dev/mmcblk0p"))
                        name = os.path.basename(mount_point)
                        part = entries[index].name
                        disk = dict(targets.release(target).partitions or {}).get(part, part)
                        self.assertIn("apps" if name == "appstore" else name, (part, disk), line)

    def test_o22n2_stock_pins(self):
        """The measured stock fstab pins survive the VM's raw-image allocation."""
        self.o22n2_stock_pins("ombre")

    def test_spacebuns_stock_pins(self):
        """Spacebuns' own stock fstabs and global logical-image names are retained."""
        self.o22n2_stock_pins("spacebuns")
        row = targets.release(("o22n2", "spacebuns"))
        self.assertEqual(
            [p.file for p in row.paks],
            ["rootfs", "bsppart", "otncabi", "otycabi", "fonts", "license"],
        )
        self.assertEqual(row.nvm.lls, "/usr/sbin/lowlevelstorage")

    def o22n2_stock_pins(self, release):
        target = ("o22n2", release)
        layout = targets.release(target).partmap
        for part in layout.parts:
            if not isinstance(part.size, int) and part.size[0] == "pak":
                with open(os.path.join(self.tmp, part.size[1] + ".pak"), "wb") as f:
                    f.truncate(4096)
        with mock.patch.object(partmap, "file_table", return_value=partmap.encode(fake_map())):
            m = partmap.decode(partmap.table(self.tmp, target))
        entries = dict(partmap.partitions(m))
        with open(os.path.join(HERE, "data", "stock-fstab", "o22n2-" + release + ".fstab")) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                device, mount_point, *_ = line.split()
                index = int(device.removeprefix("/dev/mmcblk0p"))
                name = os.path.basename(mount_point)
                self.assertEqual(entries[index].name, "apps" if name == "appstore" else name)
        self.assertNotIn("platform-plugins", dict(partmap.nodes(m, target)).values())

    def test_w20h_layout(self):
        # W20H's stock mount-readwrite fstab pins DB8/common data/apps to p56-p58.
        self.assembled_layout(("k6hp", "jhericurl"), state_first=56)

    def assembled_layout(self, target, state_first=57):
        fw = os.path.join(self.tmp, "fw-" + targets.name(target))
        os.makedirs(fw)
        layout = targets.release(target).partmap
        for p in layout.parts:
            if not isinstance(p.size, int) and p.size[0] == "pak":
                with open(os.path.join(fw, p.size[1] + ".pak"), "wb") as f:
                    f.truncate(3 << 20 if p.size[1] == "rootfs" else 1000)
        head = partmap.encode(
            fake_map(("partinfo",))._replace(device=("h13_emmc", 0xE4800000, 0, 0, 0, 0, 1))
        )
        with mock.patch.object(partmap, "file_table", return_value=head):
            m = partmap.decode(partmap.table(fw, target))
        names = {n: e.name for n, e in partmap.partitions(m)}
        for n, name in (
            (14, "cert"),
            (19, "sestore"),
            (20, "wossestore"),
            (23, "sedata"),
            (29, "bsppart"),
            (30, "otncabi"),
            (31, "otycabi"),
            (32, "fonts"),
            (34, "license"),
            (46, "dbboot"),
            (50, "uhdcp"),
            (state_first, "db8"),
            (state_first + 1, "cmn_data"),
            (state_first + 2, "apps"),
        ):
            self.assertEqual(names[n], name, f"p{n}")
        self.assertEqual(len(m.entries), state_first + 3)
        self.assertEqual(partmap.first(m, "partinfo")[1].offset, 0x100000)
        self.assertEqual(partmap.first(m, "rootfs")[1].size, 3 << 20)
        self.assertEqual(partmap.first(m, "apps")[1].size, 2 << 30)
        self.assertEqual((m.device[0], m.device[1]), ("h13_emmc", m.entries[-1].offset))
        for a, b in zip(m.entries, m.entries[1:]):
            self.assertEqual(a.offset + a.size, b.offset)
        partmap.nodes(m, target)

    def test_updatable_inactive_headroom(self):
        """firmware.updatable: each inactive A/B-bank pak partition (used=0) gets UPDATABLE_HEADROOM
        more room, so a larger variant firmware's pak still fits the stock updater's
        pak.imageSize <= partition.size check. Active-bank (used=1) entries, fixed-size entries and
        the non-updatable table are byte-for-byte untouched; backing-disk sizes and node indices do
        not move."""
        target = ("o22", "mullet")
        fw = os.path.join(self.tmp, "fw")
        os.makedirs(fw)
        layout = targets.release(target).partmap
        for p in layout.parts:
            if not isinstance(p.size, int) and p.size[0] == "pak":
                with open(os.path.join(fw, p.size[1] + ".pak"), "wb") as f:
                    f.truncate(3 << 20 if p.size[1] == "rootfs" else 1000)
        head = partmap.encode(
            fake_map(("partinfo",))._replace(device=("h13_emmc", 0xE4800000, 0, 0, 0, 0, 1))
        )
        with mock.patch.object(partmap, "file_table", return_value=head):
            plain_blob = partmap.table(fw, target)
            wide_blob = partmap.table(fw, target, updatable=True)
            # the default is non-updatable: the flag off is byte-for-byte the plain table
            self.assertEqual(partmap.table(fw, target, updatable=False), plain_blob)
        plain, wide = partmap.decode(plain_blob), partmap.decode(wide_blob)
        self.assertNotEqual(plain_blob, wide_blob)
        self.assertEqual(len(plain.entries), len(wide.entries))
        inactive_paks = {
            p.name
            for p in layout.parts
            if p.used == 0 and not isinstance(p.size, int) and p.size[0] == "pak"
        }
        n_inactive = 0
        for a, b in zip(plain.entries, wide.entries):
            self.assertEqual((a.name, a.used, a.flags), (b.name, b.used, b.flags))
            if a.used == 0 and a.name in inactive_paks and a.name != "reserved":
                self.assertEqual(b.size, a.size + partmap.UPDATABLE_HEADROOM)
                n_inactive += 1
            else:
                self.assertEqual(b.size, a.size)
        # every inactive pak bank is widened (here: the ten of o22-mullet)
        self.assertEqual(n_inactive, len(inactive_paks))
        # both tables are still consecutive from 0
        for m in (plain, wide):
            self.assertEqual(m.entries[0].offset, 0)
            for x, y in zip(m.entries, m.entries[1:]):
                self.assertEqual(x.offset + x.size, y.offset)
        # the device grows by exactly the headroom handed out, no more
        self.assertEqual(
            partmap.device_size(wide),
            partmap.device_size(plain) + n_inactive * partmap.UPDATABLE_HEADROOM,
        )
        # the inactive tzfw bank, the one that failed the check, now holds more than a 1 MiB pak
        tzfw = [e for e in wide.entries if e.name == "tzfw" and e.used == 0]
        self.assertEqual(len(tzfw), 1)
        self.assertEqual(tzfw[0].size, partmap.ALIGN + partmap.UPDATABLE_HEADROOM)
        # active-bank (used=1) tzfw keeps its node index and size (its offset may shift, since an
        # earlier inactive bank grew ahead of it)
        self.assertEqual(partmap.first(wide, "tzfw")[0], partmap.first(plain, "tzfw")[0])
        self.assertEqual(partmap.first(wide, "tzfw")[1].size, partmap.first(plain, "tzfw")[1].size)
        # backing-disk sizes and node indices are identical in both maps
        self.assertEqual(partmap.disk_sizes(wide, target), partmap.disk_sizes(plain, target))
        self.assertEqual(partmap.nodes(wide, target), partmap.nodes(plain, target))

    def firmware(self, name):
        fw = testunits.host_firmware(name)
        if not fw:
            self.skipTest(f"no {name} in the host's firmware collections")
        return fw

    def test_firmware_tables(self):
        """Up to W19P the map is lginit_app2's partinfo object as it is (W19P's: 58 partitions,
        partinfo p2, db8 p56 as its fstab); every node a target's release names is in its map."""
        fw = self.firmware("05.40.97.01-HE_DTV_W19P_AFADABAA")
        blob = partmap.table(fw, ("k5lp", "goldilocks2"))
        m = partmap.decode(blob)
        self.assertEqual(len(m.entries), 59)
        self.assertEqual(dict(partmap.nodes(m, ("k5lp", "goldilocks2")))[56], "db8")
        self.assertEqual(partmap.first(m, "partinfo")[0], 2)
        self.assertEqual(blob, partmap.encode(m))
        cache = os.path.join(self.tmp, "cache")
        img = partmap.build(fw, ("k5lp", "goldilocks2"), cache)
        self.assertEqual(partmap.read(img), m)
        self.assertEqual(partmap.build(fw, ("k5lp", "goldilocks2"), cache), img)

    def test_plan_disks(self):
        """The eMMC disks: sized as their partitions, partinfo seeded with the map, the nodes from the
        VM's own copy; a copy whose partitions differ, and a disk smaller than its partition, refused."""
        root = os.path.join(self.tmp, "t")
        top, fw, state = fake_tree(root)
        env = dict(FW=fw, STATE=state, **net("direct"), SLOT="1")
        build = fake_partmap_build(os.path.join(self.tmp, "map.img"))
        with mock.patch.object(partmap, "build", build):
            p = qemu.plan(env, [], accel="tcg", tree=paths.Tree(top), say=lambda _: None)
        qemu.make_disks(p)
        with open(os.path.join(state, "partinfo.img"), "rb") as f:
            self.assertEqual(f.read(), partmap.image(partmap.encode(fake_map())))
        self.assertEqual(os.path.getsize(os.path.join(state, "sedata.img")), 1 << 20)
        nodes = qemu.part_nodes(p)
        self.assertIn((5, "nvram"), nodes)
        self.assertIn("-device virtio-blk-pci,drive=sestore,serial=sestore", " ".join(p.argv))
        # the guest rewrote its map with other partitions
        other = fake_map(tuple(e.name for e in fake_map().entries[:-1]) + ("extra",))
        with open(os.path.join(state, "partinfo.img"), "wb") as f:
            f.write(partmap.image(partmap.encode(other)))
        with self.assertRaisesRegex(qemu.Error, "not the firmware's map"):
            qemu.part_nodes(p)
        # an NVM disk smaller than its partition
        with open(os.path.join(state, "nvram.img"), "r+b") as f:
            f.truncate(1 << 20)
        big = fake_partmap_build(
            os.path.join(self.tmp, "map2.img"), fake_map(sizes={"nvram": 16 << 20})
        )
        # (a start reads the map the VM's board data was prepared with: prepared again here)
        shutil.rmtree(p.prepared.dir)
        with mock.patch.object(partmap, "build", big), self.assertRaisesRegex(qemu.Error, "nvram"):
            qemu.plan(env, [], accel="tcg", tree=paths.Tree(top), say=lambda _: None)

    def test_plan_updatable(self):
        """firmware.updatable on (UPDATABLE=on): one scratch overlay disk sized to the whole eMMC,
        attached snapshot=on, plus partinfo attached snapshot=on, and the dm-linear table on the
        plan. Off, nothing of it; the overlay's base is created the whole-disk size."""
        root = os.path.join(self.tmp, "t")
        top, fw, state = fake_tree(root)
        base = dict(FW=fw, STATE=state, **net("direct"), SLOT="1")
        build = fake_partmap_build(os.path.join(self.tmp, "map.img"))
        with mock.patch.object(partmap, "build", build):
            off = qemu.plan(base, [], accel="tcg", tree=paths.Tree(top), say=lambda _: None)
            on = qemu.plan(
                dict(base, UPDATABLE="on"),
                [],
                accel="tcg",
                tree=paths.Tree(top),
                say=lambda _: None,
            )
        self.assertFalse(off.updatable)
        self.assertNotIn("emmc-overlay", " ".join(off.argv))
        self.assertIn(
            "-drive if=none,id=partinfo,format=raw,file="
            + os.path.join(state, "partinfo.img")
            + ",discard=unmap,detect-zeroes=unmap -device",
            " ".join(off.argv),
        )
        # on: the overlay disk, and partinfo now snapshot=on
        self.assertTrue(on.updatable)
        self.assertEqual(on.emmc_overlay, "emmc-overlay")
        argv = " ".join(on.argv)
        self.assertIn(
            "-drive if=none,id=emmc-overlay,format=raw,snapshot=on,file="
            + os.path.join(state, "emmc-overlay.img")
            + ",discard=unmap,detect-zeroes=unmap -device "
            "virtio-blk-pci,drive=emmc-overlay,serial=emmc-overlay",
            argv,
        )
        self.assertIn(
            "if=none,id=partinfo,format=raw,file="
            + os.path.join(state, "partinfo.img")
            + ",discard=unmap,detect-zeroes=unmap,snapshot=on",
            argv,
        )
        # the table matches partmap.emmc_table_words for the firmware's map
        m = partmap.read(on.partmap)
        self.assertEqual(on.emmc_table, partmap.emmc_table_words(m, on.target, "emmc-overlay"))
        # the overlay-backed per-partition nodes match partmap.emmc_part_words; off carries none
        self.assertEqual(on.emmc_part_nodes, partmap.emmc_part_words(m, on.target))
        self.assertEqual(off.emmc_part_nodes, [])
        # the overlay base is created at the whole-disk size, sparse
        qemu.make_disks(on)
        overlay = os.path.join(state, "emmc-overlay.img")
        self.assertEqual(os.path.getsize(overlay), partmap.device_size(m))
        self.assertEqual(os.stat(overlay).st_blocks, 0)
