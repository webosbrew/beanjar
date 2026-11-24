#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The ISDB-T stream with a BML data broadcast (beanjar/tuner_stream_arib.py): the carousel
messages' ARIB layout and the page without tools; then, with TSDuck and ffmpeg, a made stream read
back by TSDuck: the PMT's streams and descriptors, the SI around it, the DII and DDB sections
repeated on the data PID and module 0 rebuilt from them holding startup.bml byte for byte.

TSDuck 3.45 reads a DII only as a DVB object carousel's (BIOP moduleInfo), so tsdsmcc gets nothing
from an ARIB carousel; the module is rebuilt here from the sections TSDuck demuxes instead."""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "host", "core"))

from beanjar import tuner_stream
from beanjar import tuner_stream_arib as arib


def parse_dii(msg):
    """{download_id, block_size, modules: [(id, size, version, info)]} of a DII message (ARIB)."""
    assert msg[:4] == b"\x11\x03\x10\x02", msg[:4].hex()
    n = int.from_bytes(msg[10:12], "big")
    body = msg[12 : 12 + n]
    out = {
        "download_id": int.from_bytes(body[0:4], "big"),
        "block_size": int.from_bytes(body[4:6], "big"),
    }
    at = 16  # downloadId, blockSize, windowSize, ackPeriod, tCDownloadWindow, tCDownloadScenario
    at += 2 + int.from_bytes(body[at : at + 2], "big")  # compatibilityDescriptor
    count = int.from_bytes(body[at : at + 2], "big")
    at += 2
    mods = []
    for _ in range(count):
        mid, size, ver, ilen = (
            int.from_bytes(body[at : at + 2], "big"),
            int.from_bytes(body[at + 2 : at + 6], "big"),
            body[at + 6],
            body[at + 7],
        )
        mods.append((mid, size, ver, body[at + 8 : at + 8 + ilen]))
        at += 8 + ilen
    out["modules"] = mods
    out["private_data_length"] = int.from_bytes(body[at : at + 2], "big")
    assert at + 2 == len(body)
    return out


def parse_ddb(msg):
    """(download_id, module_id, version, block_number, data) of a DDB message."""
    assert msg[:4] == b"\x11\x03\x10\x03", msg[:4].hex()
    n = int.from_bytes(msg[10:12], "big")
    body = msg[12 : 12 + n]
    return (
        int.from_bytes(msg[4:8], "big"),
        int.from_bytes(body[0:2], "big"),
        body[2],
        int.from_bytes(body[4:6], "big"),
        body[6:],
    )


def multipart(entity):
    """[(headers, body)] of a multipart/mixed entity, the boundary from its Content-Type."""
    head, _, rest = entity.partition(b"\r\n\r\n")
    boundary = head.split(b"boundary=")[1].split(b";")[0].strip().strip(b'"')
    parts = []
    for chunk in rest.split(b"--" + boundary)[1:]:
        if chunk.startswith(b"--"):
            break
        h, _, body = chunk[2:].partition(b"\r\n\r\n")
        pairs = (line.split(b":", 1) for line in h.split(b"\r\n"))
        headers = {k.strip().lower(): v.strip() for k, v in pairs}
        parts.append((headers, body[:-2] if body.endswith(b"\r\n") else body))
    return parts


class CarouselTest(unittest.TestCase):
    def test_page(self):
        page = arib.startup_bml()
        text = page.decode("euc_jp")
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="EUC-JP" ?>'))
        self.assertIn("+//ARIB STD-B24:1999//DTD BML Document for Japanese//JA", text)
        self.assertIn("resolution: 960x540", text)
        self.assertIn("used-key-list:", text)
        self.assertIn(arib.PAGE_TEXT, text)
        ET.fromstring(text[text.index("<bml>") :])  # well-formed past the prolog

    def test_module(self):
        # the sets' BML browser parses only a quoted boundary right
        head = arib.module().split(b"\r\n\r\n")[0]
        self.assertTrue(head.endswith(f'boundary="{arib.BOUNDARY}"'.encode()))
        mod = arib.module()
        ((list_headers, rlist), (headers, body)) = multipart(mod)
        self.assertEqual(list_headers[b"content-type"], arib.RESOURCE_LIST_TYPE.encode())
        self.assertEqual(headers[b"content-location"], b"startup.bml")
        self.assertTrue(headers[b"content-type"].startswith(b"text/X-arib-bml"))
        self.assertEqual(body, arib.startup_bml())
        # the list as the set's NetFront reads it: the body is at offset + header + 2 (the blank
        # line), and the header lines are the part's
        self.assertEqual(rlist[0], 1)
        size, offset, header, length = rlist[1], *struct.unpack(">IHI", rlist[2:12])
        namelen = rlist[14] & 0x7F
        self.assertEqual(size, 13 + namelen)
        self.assertEqual(len(rlist), 2 + size)
        self.assertEqual(rlist[15 : 15 + namelen], b"startup.bml")
        self.assertEqual(mod[offset + header + 2 : offset + header + 2 + length], body)
        self.assertTrue(mod[offset:].startswith(b"Content-Type: text/X-arib-bml"))
        self.assertEqual(mod[offset + header : offset + header + 2], b"\r\n")

    def test_dii(self):
        mod = arib.module()
        d = parse_dii(arib.dii_message(mod))
        self.assertEqual(d["download_id"] >> 28, arib.DATA_EVENT_ID)
        self.assertEqual(d["block_size"], 4066)
        self.assertEqual(d["modules"], [(0, len(mod), 0, b"\x01\x0fmultipart/mixed")])
        self.assertEqual(d["private_data_length"], 0)

    def test_ddb_blocks(self):
        mod = bytes(range(256)) * 40  # 10240 bytes: 3 blocks
        got = [parse_ddb(m) for m in arib.ddb_messages(mod)]
        self.assertEqual([g[3] for g in got], [0, 1, 2])
        self.assertEqual([len(g[4]) for g in got], [4066, 4066, 2108])
        self.assertEqual(b"".join(g[4] for g in got), mod)
        # a full block fills a 4096-byte section: header 8, message header 12, 6, data, CRC 4
        self.assertEqual(8 + len(arib.ddb_messages(mod)[0]) + 4, 4096)

    def test_data_component(self):
        self.assertEqual(arib.data_component_info(), bytes([0x33, 0x70, 0xF8, 0xA0]))
        self.assertEqual(arib.data_component_info(auto_start=False)[0], 0x23)

    def test_uhf(self):
        self.assertEqual(arib.uhf_center_hz(13), 473142857)
        self.assertEqual(arib.uhf_center_hz(27), 557142857)
        with self.assertRaises(tuner_stream.Error):
            arib.uhf_center_hz(63)


def sections(data):
    """The sections, one after another, of a TSDuck binary section file."""
    out, at = [], 0
    while at + 3 <= len(data):
        n = 3 + (((data[at + 1] & 0xF) << 8) | data[at + 2])
        out.append(data[at : at + n])
        at += n
    return out


@unittest.skipIf(
    arib.tools() or not shutil.which("tstables"), "needs TSDuck (tsp, tstables) and ffmpeg"
)
class StreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="arib-")
        cls.ts = arib.make(os.path.join(cls.dir, "bml.ts"), time.time())
        xml = subprocess.run(
            ["tstables", "--japan", "--xml-output", "-", cls.ts], capture_output=True, check=True
        ).stdout
        cls.tables = ET.fromstring(xml)
        secs = os.path.join(cls.dir, "data.bin")
        subprocess.run(
            [
                "tstables",
                "--all-sections",
                "--pid",
                f"0x{arib.DATA_PID:X}",
                "--binary-output",
                secs,
                cls.ts,
            ],
            capture_output=True,
            check=True,
        )
        with open(secs, "rb") as f:
            cls.data = sections(f.read())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def table(self, tag):
        found = self.tables.findall(tag)
        self.assertTrue(found, f"no {tag} in the stream")
        return found[0]

    def test_pmt(self):
        pat = self.table("PAT")
        self.assertEqual(int(pat.get("network_PID"), 0), arib.NIT_PID)
        svc = pat.find("service")
        self.assertEqual(int(svc.get("program_map_PID"), 0), arib.PMT_PID)
        pmt = self.table("PMT")
        self.assertEqual(int(pmt.get("PCR_PID"), 0), arib.VIDEO_PID)
        comps = {int(c.get("elementary_PID"), 0): c for c in pmt.findall("component")}
        self.assertEqual(
            {p: int(c.get("stream_type"), 0) for p, c in comps.items()},
            {arib.VIDEO_PID: 0x02, arib.AUDIO_PID: 0x0F, arib.DATA_PID: 0x0D},
        )
        data = comps[arib.DATA_PID]
        tag = data.find("stream_identifier_descriptor")
        self.assertEqual(int(tag.get("component_tag"), 0), 0x40)
        dc = data.find("data_component_descriptor")
        self.assertEqual(int(dc.get("data_component_id"), 0), 0x000C)
        info = bytes.fromhex("".join(dc.find("additional_data_component_info").text.split()))
        self.assertEqual(info[0] & 0x30, 0x30, "entry point and auto start")
        self.assertEqual(info[0] & 0x0F, 3, "960x540")
        self.assertEqual(info[2] >> 4, arib.DATA_EVENT_ID)

    def test_si(self):
        nit = self.table("NIT")
        ts = nit.find("transport_stream")
        dl = ts.find("ISDB_terrestrial_delivery_system_descriptor")
        self.assertEqual(int(dl.find("frequency").get("value").replace(",", "")), 557142857)
        self.assertIsNotNone(ts.find("TS_information_descriptor"))
        sdt = self.table("SDT")
        self.assertEqual(
            sdt.find("service/service_descriptor").get("service_name"), arib.SERVICE["name"]
        )
        eit = self.table("EIT")
        self.assertEqual(eit.get("type"), "pf")
        self.assertEqual(len(eit.findall("event")), 2)
        self.table("TOT")
        self.table("TDT")

    def test_carousel(self):
        dii = [s for s in self.data if s[0] == 0x3B]
        ddb = [s for s in self.data if s[0] == 0x3C]
        for s in dii + ddb:
            self.assertEqual(tuner_stream.crc32_mpeg(s), 0)
        # 10 s at DII 500 ms and DDB 250 ms
        self.assertGreaterEqual(len(dii), 15)
        self.assertGreaterEqual(len(ddb), 30)
        info = parse_dii(dii[0][8:-4])
        self.assertEqual(int.from_bytes(dii[0][3:5], "big"), arib.TRANSACTION_ID & 0xFFFF)
        ((mid, size, version, _info),) = info["modules"]
        blocks = {}
        for s in ddb:
            did, m, v, n, data = parse_ddb(s[8:-4])
            self.assertEqual((did, m, v), (info["download_id"], mid, version))
            self.assertEqual(s[6], n & 0xFF)  # section_number
            blocks[n] = data
        self.assertEqual(sorted(blocks), list(range(-(-size // info["block_size"]))))
        mod = b"".join(blocks[n] for n in sorted(blocks))
        self.assertEqual(len(mod), size)
        self.assertEqual(mod, arib.module())
        (_rlist, (headers, body)) = multipart(mod)
        self.assertEqual(headers[b"content-location"], b"startup.bml")
        self.assertEqual(body, arib.startup_bml())


if __name__ == "__main__":
    unittest.main()
