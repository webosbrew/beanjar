# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The I2C EEPROM image and the low-level storage (nvm.py, lls.py).

NvmTest programs and syncs the I2C EEPROM image from a made-up ELF file's records."""

import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

from beanjar import paths
from beanjar.board import lls, nvm
from beanjar.firmware import targets
from launchkit import H13_AFRO


def elf32(objects, data_vaddr=0x10000):
    """A minimal ELF32 little-endian executable defining the data objects {name: bytes} in one
    loadable segment, with a dynamic symbol table (what nvm.elf_objects reads)."""
    data = b"".join(objects.values())
    names = b"\0" + b"".join(n.encode() + b"\0" for n in objects)
    ehsize, phentsize, shentsize = 52, 32, 40
    data_off = ehsize + phentsize
    dynstr_off = data_off + len(data)
    dynsym_off = dynstr_off + len(names)
    syms, name_at, value = [bytes(16)], 1, data_vaddr
    for n, blob in objects.items():
        syms.append(struct.pack("<IIIBBH", name_at, value, len(blob), 0x11, 0, 1))  # GLOBAL OBJECT
        name_at += len(n) + 1
        value += len(blob)
    symtab = b"".join(syms)
    sh_off = dynsym_off + len(symtab)
    sections = [
        bytes(shentsize),
        struct.pack("<IIIIIIIIII", 0, 1, 3, data_vaddr, data_off, len(data), 0, 0, 4, 0),  # .data
        struct.pack("<IIIIIIIIII", 0, 3, 2, 0, dynstr_off, len(names), 0, 0, 1, 0),  # .dynstr
        struct.pack("<IIIIIIIIII", 0, 11, 2, 0, dynsym_off, len(symtab), 2, 1, 4, 16),
    ]  # .dynsym
    header = (
        b"\x7fELF\x01\x01\x01"
        + bytes(9)
        + struct.pack(
            "<HHIIIIIHHHHHH",
            2,
            40,
            1,
            0,
            ehsize,
            sh_off,
            0x5000000,
            ehsize,
            phentsize,
            1,
            shentsize,
            len(sections),
            0,
        )
    )
    phdr = struct.pack(
        "<IIIIIIII", 1, data_off, data_vaddr, data_vaddr, len(data), len(data) + 64, 6, 4
    )
    return header + phdr + data + names + symtab + b"".join(sections)


class NvmTest(unittest.TestCase):
    """nvm.py: the I2C EEPROM a factory leaves, from the firmware's default records and the board;
    later starts bring the tool options and the serial in line (the libhal_lg115x shim's
    eeprom_program, _sync_tool_options and _sync_serial before)."""

    MODEL = (
        struct.pack("<I", 0x20130002)
        + b"WEBOS1".ljust(13, b"\0")
        + b"SKJY1107".ljust(17, b"\0")
        + b"\xff\0ZZZ\0ZZZ\0"
    )
    OPTIONS = bytes.fromhex(
        "02001320e28000000000000000010000000000000004a201010000000000000000000000000000010000"
        "000000000000000000"
    )
    CONTINENT = struct.pack("<I", 0x20130004) + bytes(36)
    LASTINPUT = struct.pack("<I", 0x20130001) + bytes(40)
    FACTORY = (
        struct.pack("<I", 0xDC158B78) + bytes(0x98) + b"\0" + b"\x05" * 0x43
    )  # substrate (+0x9C) off

    def setUp(self):
        self.records = {
            "gModelInfoDB": self.MODEL,
            "gToolOptionDB": self.OPTIONS,
            "gContinentOptDB": self.CONTINENT,
            "gLastInputInfoDB": self.LASTINPUT,
            "gFactoryDB": self.FACTORY,
        }
        self.said = []

    def test_elf_objects(self):
        blob = elf32(self.records)
        self.assertEqual(nvm.elf_objects(blob, tuple(self.records)), self.records)
        self.assertEqual(
            nvm.elf_objects(blob, ("gModelInfoDB", "other")), {"gModelInfoDB": self.MODEL}
        )
        with self.assertRaisesRegex(nvm.Error, "not a 32-bit"):
            nvm.elf_objects(b"\x7fELF\x02" + bytes(60), ("gModelInfoDB",))

    def test_program_webos1(self):
        n = targets.release(H13_AFRO).nvm
        board = {"ToolOpt": "162:4626:171:64792:2598:2312:1167", "modelName": "42LB650V-ZN"}
        image = nvm.program(self.records, n, board, "501RAXX00001", self.said.append)
        self.assertEqual(len(image), nvm.SIZE)
        model = image[0x924 : 0x924 + len(self.MODEL)]
        self.assertEqual(model[:4], self.MODEL[:4])
        self.assertEqual(model[4:17], b"42LB650V-ZN\0\0")
        self.assertEqual(model[17:34], b"501RAXX00001".ljust(17, b"\0"))
        self.assertEqual(model[34:], self.MODEL[34:])
        options = image[0x4E4 : 0x4E4 + len(self.OPTIONS)]
        self.assertEqual(
            struct.unpack_from("<7H", options, 4), (162, 4626, 171, 64792, 2598, 2312, 1167)
        )
        self.assertEqual(options[:4] + options[18:], self.OPTIONS[:4] + self.OPTIONS[18:])
        # the factory's FactoryDB, substrate mode off (0x330)
        self.assertEqual(image[0x294 : 0x294 + len(self.FACTORY)], self.FACTORY)
        self.assertEqual(image[0x330], 0)
        # everything else is a new chip's 0xFF; webOS 1 has no ContinentOptionDB programmed
        rest = (
            image[:0x294]
            + image[0x294 + len(self.FACTORY) : 0x4E4]
            + image[0x4E4 + len(self.OPTIONS) : 0x924]
            + image[0x924 + len(self.MODEL) :]
        )
        self.assertEqual(set(rest), {0xFF})
        self.assertIn("model '42LB650V-ZN', serial '501RAXX00001'", self.said[-1])

    def test_program_webos3(self):
        """32-bit words from +8, the ContinentOptionDB; a short ToolOpt= keeps the rest."""
        n = targets.release(("m2", "dreadlocks")).nvm
        options = struct.pack("<I", 0x20150625) + bytes(72)
        records = dict(self.records, gToolOptionDB=options)
        image = nvm.program(records, n, {"ToolOpt": "36074:1"}, "", self.said.append)
        self.assertEqual(struct.unpack_from("<3I", image, 0x4E4 + 4), (0, 36074, 1))
        self.assertEqual(image[0x1BD4 : 0x1BD4 + 40], self.CONTINENT)
        self.assertEqual(image[0x1B48 : 0x1B48 + 44], self.LASTINPUT)  # the factory's last input
        self.assertEqual(
            image[0x924 + 17 : 0x924 + 34], self.MODEL[17:34]
        )  # no serial: the default's
        self.assertIn("ToolOpt= has 2 of 9 words", self.said[0])
        with self.assertRaisesRegex(nvm.Error, "no default"):
            nvm.program({"gModelInfoDB": self.MODEL}, n, {}, "", self.said.append)
        with self.assertRaisesRegex(nvm.Error, "no default gLastInputInfoDB"):
            nvm.program(dict(records, gLastInputInfoDB=None), n, {}, "", self.said.append)
        with self.assertRaisesRegex(nvm.Error, "no default gFactoryDB"):
            nvm.program(dict(records, gFactoryDB=None), n, {}, "", self.said.append)

    def test_program_webos35(self):
        """dreadlocks2's ModelInfoDB (56 bytes, mark 0x20160630, model[24] at +4, serial[17] at
        +28), ContinentOptionDB at 0x1C54 and Last_Input_info DB at 0x1BC8; the syncs follow it."""
        n = targets.release(("m16p", "dreadlocks2")).nvm
        model = (
            struct.pack("<I", 0x20160630)
            + b"WEBOS3.5".ljust(24, b"\0")
            + b"SKJY1107".ljust(17, b"\0")
            + b"\xff\0\0"
            + b"ZZZ\0"
            + bytes(4)
        )
        records = dict(
            self.records,
            gModelInfoDB=model,
            gToolOptionDB=struct.pack("<I", 0x20150625) + bytes(72),
        )
        image = bytearray(
            nvm.program(records, n, {"modelName": "55UJ750V-ZA"}, "705RAXX00001", self.said.append)
        )
        self.assertEqual(
            image[0x924 : 0x924 + 56],
            struct.pack("<I", 0x20160630)
            + b"55UJ750V-ZA".ljust(24, b"\0")
            + b"705RAXX00001".ljust(17, b"\0")
            + model[45:],
        )
        self.assertEqual(image[0x1C54 : 0x1C54 + 40], self.CONTINENT)
        self.assertEqual(image[0x1BC8 : 0x1BC8 + 44], self.LASTINPUT)
        self.assertTrue(nvm.sync_serial(image, n, "705RAXX00002", self.said.append))
        self.assertTrue(nvm.sync_model(image, n, "65UJ750V-ZA", self.said.append))
        self.assertEqual(
            image[0x924 + 4 : 0x924 + 45],
            b"65UJ750V-ZA".ljust(24, b"\0") + b"705RAXX00002".ljust(17, b"\0"),
        )

    def test_program_webos4(self):
        """goldilocks: the model's input-port bytes at ToolOptionDB +48..+73, programmed and synced."""
        n = targets.release(("m3", "goldilocks")).nvm
        ports = targets.soc(("m3", "goldilocks")).inputs
        self.assertEqual(len(ports), 26)
        model = struct.pack("<I", 0x20160630) + bytes(52)
        options = (
            struct.pack("<I", 0x20150625) + bytes(44) + b"\x01" + bytes(14) + b"\x01" + bytes(12)
        )
        records = dict(self.records, gModelInfoDB=model, gToolOptionDB=options)
        image = bytearray(
            nvm.program(
                records,
                n,
                {"ToolOpt": "6566:12615713:0:0:34085:0:0:0:0"},
                "",
                self.said.append,
                ports=ports,
            )
        )
        self.assertEqual(struct.unpack_from("<2I", image, 0x4E4 + 8), (6566, 12615713))
        self.assertEqual(image[0x4E4 + 48 : 0x4E4 + 74], ports)
        image[0x4E4 + 48 : 0x4E4 + 74] = bytes(26)
        self.assertTrue(
            nvm.sync_tool_options(
                image, n, {"ToolOpt": "6566:12615713:0:0:34085:0:0:0:0"}, self.said.append, ports
            )
        )
        self.assertEqual(image[0x4E4 + 48 : 0x4E4 + 74], ports)
        self.assertFalse(
            nvm.sync_tool_options(
                image, n, {"ToolOpt": "6566:12615713:0:0:34085:0:0:0:0"}, self.said.append, ports
            )
        )

    def test_program_six_words(self):
        """goldilocks2: Realtek's U-Boot passes ToolOption1..6 of 9; the rest keep the default
        record's, and six given words are enough to sync."""
        n = targets.release(("k5lp", "goldilocks2")).nvm
        model = struct.pack("<I", 0x20160630) + bytes(52)
        options = struct.pack("<I", 0x20150625) + bytes(36) + struct.pack("<I", 7) + bytes(32)
        records = dict(self.records, gModelInfoDB=model, gToolOptionDB=options)
        image = bytearray(nvm.program(records, n, {"ToolOpt": "1:2:3:4:5:6"}, "", self.said.append))
        self.assertEqual(struct.unpack_from("<9I", image, 0x4E4 + 8), (1, 2, 3, 4, 5, 6, 0, 0, 7))
        self.assertFalse([s for s in self.said if "of 6 words" in s])
        self.assertTrue(
            nvm.sync_tool_options(image, n, {"ToolOpt": "1:2:3:4:5:16"}, self.said.append)
        )
        self.assertEqual(struct.unpack_from("<9I", image, 0x4E4 + 8), (1, 2, 3, 4, 5, 16, 0, 0, 7))

    def test_program_continent(self):
        """The model's area: the default record's mark, AREA_OPTION2, and the table entry's words."""
        n = targets.release(("m2", "dreadlocks")).nvm
        entry = struct.pack("<I15H2x", 0x1146190, *range(1, 16))
        table = bytes(36) * 50 + entry
        records = dict(
            self.records,
            gToolOptionDB=struct.pack("<I", 0x20150625) + bytes(72),
            _aDefineContinent=table,
        )
        image = nvm.program(records, n, {}, "", self.said.append, (50, 24, 0))
        self.assertEqual(
            image[0x1BD4 : 0x1BD4 + 40],
            struct.pack("<IHH15H2x", 0x20130004, 0x0C32, 0, *range(1, 16)),
        )
        self.assertIn("continent 50", self.said[-1])
        with self.assertRaisesRegex(nvm.Error, "no _aDefineContinent entry 51"):
            nvm.program(records, n, {}, "", self.said.append, (51, 24, 0))

    def test_prepare(self):
        """A blank image becomes the VM's prepared chip (new_chip: programmed at creation); a
        programmed one follows ToolOpt= and the VM's serial and is otherwise left as tvservice
        wrote it. The start's prepare reads nothing of the firmware."""
        with tempfile.TemporaryDirectory(prefix="beanjar-nvm-") as top:
            cmdline = os.path.join(top, "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline")
            os.makedirs(os.path.dirname(cmdline))
            with open(cmdline, "w") as f:
                f.write(
                    "# ToolOpt=1:1\nmodelName=42LB650V-ZN serialNum=501RAXX00001 debugMode=3\n"
                    "ToolOpt=162:4626:171:64792:2598:2312:1167\n"
                )
            img, tree = os.path.join(top, "eeprom.img"), paths.Tree(top)
            with open(img, "wb") as f:
                f.truncate(nvm.SIZE)
            with mock.patch.object(nvm, "default_records", return_value=self.records):
                chip = nvm.new_chip("fw", H13_AFRO, None, tree, self.said.append)
            self.assertEqual(chip.note, None)
            no_firmware = (
                mock.patch.object(nvm, "default_records", side_effect=AssertionError("read")),
                mock.patch.object(lls, "cache_dir", side_effect=AssertionError("made")),
            )
            with no_firmware[0], no_firmware[1]:
                nvm.prepare(img, H13_AFRO, chip, None, tree, self.said.append)
            with open(img, "rb") as f:
                first = bytearray(f.read())
            self.assertEqual(bytes(first), chip.image)
            self.assertEqual(first[0x924 + 17 : 0x924 + 29], b"501RAXX00001")
            self.assertEqual(first[0x1A1], 3, "the board's debug status")
            # tvservice's own writes stay; the serial follows SERIAL, the words ToolOpt=
            first[0x100] = 0x5A
            first[0x4E4 + 4 : 0x4E4 + 6] = b"\0\0"
            first[0x924 + 4 : 0x924 + 17] = b"OLDMODEL".ljust(13, b"\0")
            with open(img, "wb") as f:
                f.write(first)
            with no_firmware[0], no_firmware[1]:
                nvm.prepare(img, H13_AFRO, chip, "ABC123", tree, self.said.append)
            with open(img, "rb") as f:
                second = f.read()
            self.assertEqual(second[0x100], 0x5A)
            self.assertEqual(second[0x1A1], 3)
            self.assertEqual(struct.unpack_from("<H", second, 0x4E4 + 4)[0], 162)
            self.assertEqual(second[0x924 + 17 : 0x924 + 34], b"ABC123".ljust(17, b"\0"))
            self.assertEqual(second[0x924 + 4 : 0x924 + 17], b"42LB650V-ZN\0\0")
            self.assertEqual(
                self.said[-3:],
                [
                    "EEPROM tool options set from board.cmdline: 162:4626:171:64792:2598:2312:1167",
                    "EEPROM model name set from board.cmdline: 42LB650V-ZN",
                    "EEPROM serial set: ABC123",
                ],
            )
            # a firmware without the records gives no chip: a blank image stays blank, said
            with mock.patch.object(nvm, "default_records", side_effect=nvm.Error("no tvservice")):
                none = nvm.new_chip("fw", H13_AFRO, None, tree, self.said.append)
            self.assertEqual((none.image, none.note), (None, "no tvservice"))
            with open(img, "wb") as f:
                f.truncate(nvm.SIZE)
            nvm.prepare(img, H13_AFRO, none, None, tree, self.said.append)
            with open(img, "rb") as f:
                self.assertFalse(any(f.read()))
            self.assertIn("not programmed: no tvservice", self.said[-1])
            # a release whose EEPROM lowlevelstorage owns syncs nothing in a programmed chip ...
            k6 = ("k6lp", "jhericurl")
            said = len(self.said)
            with open(img, "wb") as f:
                f.write(first)
            with no_firmware[0], no_firmware[1]:
                nvm.prepare(img, k6, nvm.Chip(None, None, None), "ABC123", tree, self.said.append)
            with open(img, "rb") as f:
                self.assertEqual(f.read(), first)
            self.assertEqual(len(self.said), said)
            # ... and its new chip is LLS's records (lls.cache_dir), keyed by the firmware's name
            made = b"\xff" * 0x200 + b"\x27\x02\x19\x20" + b"\xff" * (nvm.SIZE - 0x204)
            cdir = os.path.join(top, "lls-entry")
            os.makedirs(cdir)
            with open(os.path.join(cdir, "eeprom.img"), "wb") as f:
                f.write(made)
            with open(os.path.join(cdir, "records.txt"), "w") as f:
                f.write("probe output\n")
            with mock.patch.object(lls, "cache_dir", return_value=cdir) as cache_dir:
                chip = nvm.new_chip("/x/fw", k6, None, tree, self.said.append, "FWNAME")
            self.assertEqual(cache_dir.call_args[0][:3], ("/x/fw", "FWNAME", k6))
            self.assertEqual(chip, nvm.Chip(made, "probe output\n", None))
            with open(img, "wb") as f:
                f.truncate(nvm.SIZE)
            with no_firmware[0], no_firmware[1]:
                nvm.prepare(img, k6, chip, None, tree, self.said.append)
            with open(img, "rb") as f:
                self.assertEqual(f.read(), made)
            self.assertIn("lowlevelstorage's default records", self.said[-1])
            # failure refuses the VM's preparation; a start without a chip refuses, image blank
            with (
                mock.patch.object(lls, "cache_dir", side_effect=lls.Error("no qemu-arm")),
                self.assertRaisesRegex(nvm.Error, "EEPROM initialization failed: no qemu-arm"),
            ):
                nvm.new_chip("fw", k6, None, tree, self.said.append)
            with open(img, "wb") as f:
                f.truncate(nvm.SIZE)
            with self.assertRaisesRegex(nvm.Error, "EEPROM initialization failed"):
                nvm.prepare(img, k6, nvm.Chip(None, None, None), None, tree, self.said.append)
            with open(img, "rb") as f:
                self.assertFalse(any(f.read()))


class LlsTest(unittest.TestCase):
    """beanjar/board/lls.py: the probe's lines and where initPlugInDB puts each record."""

    def test_large_page_host(self):
        # Pi's 16 KiB host kernel cannot map the stock ARM library under qemu-user.
        # Select the guest before looking for host qemu-arm/bwrap binaries.
        with (
            mock.patch.object(os, "sysconf", return_value=16384),
            mock.patch.object(lls, "_run_guest", return_value=(0, "records", "")) as guest,
            mock.patch.object(shutil, "which", side_effect=AssertionError("host execution")),
        ):
            self.assertEqual(lls._run("root", "cmdline", "tmp", ["probe"]), (0, "records", ""))
        guest.assert_called_once_with("root", "cmdline", "tmp", ["probe"], paths.TREE, None)

    LINES = (
        "db system 512 640 0 8 0 4 2702192011223344 27021920\n"
        "db factory 1248 4096 1 6 0 4 31071920aabb 31071920\n"
    )

    def test_place(self):
        recs = lls.parse_records("noise\n" + self.LINES)
        self.assertEqual(
            [(d["name"], d["offset"], d["region"], d["flag"], d["size"]) for d in recs],
            [("system", 512, 640, 0, 8), ("factory", 1248, 4096, 1, 6)],
        )
        image = lls.place(recs)
        self.assertEqual(len(image), lls.SIZE)
        self.assertEqual(image[512:520], bytes.fromhex("2702192011223344"))
        self.assertEqual(image[1248:1254], bytes.fromhex("31071920aabb"))
        # the rest of each region and of the chip is an erased chip's
        self.assertEqual(set(image[:512] + image[520:1248] + image[1254:]), {0xFF})

    def test_mark_written_last(self):
        """The record's mark bytes are 0xFF in the record write and the mark is written after it:
        the image has getValidMarkMagic's bytes, whatever getInitData put there."""
        recs = lls.parse_records("db audio 16 32 0 6 2 4 aabbccddeeff 01020304\n")
        self.assertEqual(lls.place(recs)[16:22], bytes.fromhex("aabb01020304"))

    def test_checks(self):
        for line, why in (
            ("db a 0 4 0 8 0 4 0011223344556677 00112233", "region"),
            ("db a 0 16 0 4 2 4 00112233 00112233", "mark at"),
            ("db a 32760 16 0 4 0 4 00112233 00112233", "past the chip"),
            ("db a 0 16 0 4 0 4 001122 00112233", "not its size"),
        ):
            with self.assertRaisesRegex(lls.Error, why):
                lls.place(lls.parse_records(line))
        with self.assertRaisesRegex(lls.Error, "overlaps"):
            lls.place(
                lls.parse_records(
                    "db a 0 16 0 4 0 4 00112233 00112233\ndb b 8 16 0 4 0 4 00112233 00112233"
                )
            )
        with self.assertRaisesRegex(lls.Error, "no DBs"):
            lls.parse_records("")

    def test_items(self):
        """An item (the system DB's debugstatus) is written over its DB's record, inside it."""
        recs = lls.parse_records(self.LINES)
        items = lls.parse_items("item system debugstatus 4 2 abcd\n")
        image = lls.place_items(lls.place(recs), recs, items)
        self.assertEqual(image[512:520], bytes.fromhex("27021920abcd3344"))
        for line in (
            "item system debugstatus 0 2 abcd",
            "item system debugstatus 7 2 abcd",
            "item nodb x 4 2 abcd",
        ):
            with self.assertRaisesRegex(lls.Error, "not in the record"):
                lls.place_items(lls.place(recs), recs, lls.parse_items(line))

    def test_debug_mode(self):
        """The VM's debug_mode: the board's one debugMode= word becomes the mode's number (kernel
        word), lls's cmdline and the debugstatus argument follow it, and a chip with a programmed
        EEPROM gets only its debugstatus item."""
        target = ("h13", "afro")
        self.assertEqual(nvm.debug_mode_number("release"), 5)
        self.assertEqual(nvm.debug_mode_number("event"), 4)
        self.assertEqual(nvm.debug_mode_number("debug"), 3)
        self.assertIsNone(nvm.debug_mode_number(None))
        with self.assertRaises(nvm.Error):
            nvm.debug_mode_number("quiet")
        board = nvm.board_words(target)
        self.assertEqual(nvm.board_words(target, debug_mode=None), board)
        for number in (3, 4, 5):
            words = nvm.board_words(target, debug_mode=number)
            self.assertEqual(
                [w for w in words if w.startswith("debugMode=")], [f"debugMode={number}"]
            )
            self.assertEqual(len(words), len(board))
            self.assertEqual(
                lls._board_items(lls.board_cmdline(target, debug_mode=number))[:2],
                ["debugstatus", str(number)],
            )
        # the VM's country (vm.toml firmware.country): the factory DB's countryName, last
        line = lls.board_cmdline(target)
        self.assertEqual(
            lls._board_items(line, "CAN")[-4:], ["string", "factory", "countryName", "CAN"]
        )
        self.assertNotIn("countryName", lls._board_items(line))
        with self.assertRaisesRegex(nvm.Error, "debugMode"):
            nvm.with_debug_mode(["a=b"], 5)
        # placing the item: only the system DB's debugstatus bytes change
        recs = lls.parse_records("db system 16 64 0 48 0 4 " + "00" * 44 + "aabbccdd 01020304\n")
        chip = lls.place(recs)
        item = lls.parse_items("item system debugstatus 4 36 " + "5a" * 36)
        new = lls.place_items(chip, recs, item)
        self.assertEqual(new[:20], chip[:20])
        self.assertEqual(new[20:56], b"\x5a" * 36)
        self.assertEqual(new[56:], chip[56:])

    def test_sync_debug_status(self):
        """webOS 1 to 3.9 keep the debug status as a plain byte (0x1A1): every start brings it to the
        effective mode (the VM's debug_mode, else the board's 3); W18R has no plain byte (its
        block is sync_debug_block's: test_nvmdebug.py)."""
        n = targets.release(("h13", "afro")).nvm
        at = n.debug_status_offset
        self.assertEqual(at, 0x1A1)
        self.assertIsNone(targets.release(("m3", "goldilocks")).nvm.debug_status_offset)
        say = lambda _: None  # noqa: E731
        chip = bytearray(b"\xff" * nvm.SIZE)
        for mode in (3, 4, 5, 3):
            self.assertTrue(nvm.sync_debug_status(chip, n, mode, say))
            self.assertEqual(chip[at], mode)
            self.assertFalse(nvm.sync_debug_status(chip, n, mode, say))
        none = targets.release(("m3", "goldilocks")).nvm
        blank = bytearray(nvm.SIZE)
        self.assertFalse(nvm.sync_debug_status(blank, none, 5))
        self.assertEqual(blank, bytearray(nvm.SIZE))

    def test_prepare_lls_debug_mode(self):
        """A programmed LLS chip follows debug_mode once: the sidecar says what was applied; a blank
        chip is made with it. Unset (None) goes back to the board's own."""
        target = ("h13", "afro")
        board = int(nvm.board_params(target)["debugMode"])
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "eeprom.img")
            with open(img, "wb") as f:
                f.write(b"\x01" * nvm.SIZE)
            calls = []

            def fake(image, records):
                calls.append(records)
                return b"\x02" * nvm.SIZE

            def chip(mode):
                # the VM's prepared chip: LLS's records for its effective debug mode
                return nvm.Chip(None, f"records for {mode}", None)

            with mock.patch.object(lls, "debug_status_item", fake):
                nvm.prepare_lls(img, target, chip(board), debug_mode=None)
                self.assertEqual(calls, [])  # as the board says: nothing
                nvm.prepare_lls(img, target, chip(5), debug_mode=5)
                nvm.prepare_lls(img, target, chip(5), debug_mode=5)
                self.assertEqual(calls, ["records for 5"])
                self.assertEqual(nvm.applied_debug_mode(img, target, paths.TREE, None), 5)
                nvm.prepare_lls(img, target, chip(board), debug_mode=None)
                self.assertEqual(calls, ["records for 5", f"records for {board}"])
                self.assertFalse(os.path.exists(img + ".debugmode"), "back to the board's own")
                nvm.prepare_lls(img, target, chip(board), debug_mode=None)
                self.assertEqual(calls, ["records for 5", f"records for {board}"])

    def test_board_items(self):
        """The probe's items: the debugstatus of debugMode=, the factory jig's model name, serial
        and tool options, and the In-Stop done that every set leaves the factory with, a
        JSON boolean as devicereset's setInstopCompleted writes it."""
        args = lls._board_items("debugMode=3 modelName=OLED55C2 serialNum=123ABC ToolOpt=7:8")
        self.assertEqual(args[:2], ["debugstatus", "3"])
        self.assertEqual(
            args[2:],
            [
                "string",
                "factory",
                "modelname",
                "OLED55C2",
                "string",
                "factory",
                "serialnumber",
                "123ABC",
                "string",
                "factory",
                "toolOption1Value",
                "7",
                "string",
                "factory",
                "toolOption2Value",
                "8",
                "bool",
                "system",
                "instop_completed",
                "true",
            ],
        )
        with self.assertRaisesRegex(lls.Error, "debugMode"):
            lls._board_items("modelName=x")

    def test_elf_deps(self):
        """The interpreter and NEEDED of a 32-bit ELF, read from its dynamic section."""
        strtab = b"\0libc.so.6\0libfoo.so.1\0"
        interp = b"/lib/ld-linux.so.3\0"
        dyn = struct.pack("<iIiIiIiI", 5, 0x1000 + 0x100, 1, 1, 1, 11, 0, 0)
        ph = 52
        data = bytearray(0x200)
        data[:4] = b"\x7fELF"
        data[4], data[5] = 1, 1
        struct.pack_into("<I", data, 28, ph)
        struct.pack_into("<HH", data, 42, 32, 3)
        # PT_LOAD of the whole file at 0x1000, PT_DYNAMIC at 0x140, PT_INTERP at 0x180
        struct.pack_into("<IIIIII", data, ph, 1, 0, 0x1000, 0x1000, 0x200, 0x200)
        struct.pack_into("<IIIIII", data, ph + 32, 2, 0x140, 0x1140, 0x1140, len(dyn), len(dyn))
        struct.pack_into(
            "<IIIIII", data, ph + 64, 3, 0x180, 0x1180, 0x1180, len(interp), len(interp)
        )
        data[0x100 : 0x100 + len(strtab)] = strtab
        data[0x140 : 0x140 + len(dyn)] = dyn
        data[0x180 : 0x180 + len(interp)] = interp
        self.assertEqual(
            lls.elf_deps(bytes(data)), ("/lib/ld-linux.so.3", ["libc.so.6", "libfoo.so.1"])
        )
