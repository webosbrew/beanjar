#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""RF names and channel plans (beanjar/tuner/band.py), US cable and DVB lineups
(lineup.py), the CVCT of a US cable channel (atsc.py) and the DVB tables
(dvb.py): the NIT's delivery descriptors and logical channel numbers, the SDT, the
EIT schedule and the TDT and TOT; with TSDuck and ffmpeg, a made DVB-T and DVB-C stream."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "host", "core"))
sys.path.insert(0, HERE)

from beanjar.tuner import antenna, atsc, band, lineup, user_stream
from beanjar.tuner import arib as arib
from beanjar.tuner import dvb as dvb

DVB = lineup.default("dvb")
ATSC = lineup.default("atsc")

DVB_MIN = """broadcast = "dvb"

[[rf]]
channel = 30
onid = 0xFF10
tsid = 0x0130

[[rf.services]]
service_id = 0x1001
name = "Eins"
lcn = 5
events = [{ title = "Show", minutes = 30 }]
"""


def parse(text):
    return lineup.parse(text, "t.toml", "/nonexistent", check_files=False)


class BandTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(band.parse("7"), (False, 7))
        self.assertEqual(band.parse("C78"), (True, 78))
        self.assertEqual([band.canonical(x) for x in ("07", "C078", "135")], ["7", "C78", "135"])
        for bad in ("c7", "C", "0", "-1", "7.1", "CC7", "1000", ""):
            with self.subTest(bad=bad), self.assertRaises(band.Error):
                band.parse(bad)
        self.assertEqual(
            sorted(["C7", "33", "C78", "7"], key=band.sort_key), ["7", "33", "C7", "C78"]
        )

    def test_us_plans(self):
        hz = band.center_hz
        # air and cable 7 share 177 MHz; the delivery system tells them apart
        self.assertEqual((hz("atsc", "7"), hz("atsc", "C7")), (177_000_000, 177_000_000))
        self.assertEqual((band.delsys("atsc", "7"), band.delsys("atsc", "C7")), (11, 2))
        # TBL_STD_NADC_F's segments
        for ch, mhz in (
            (1, 75),
            (2, 57),
            (4, 69),
            (5, 79),
            (6, 85),
            (13, 213),
            (14, 123),
            (22, 171),
            (23, 219),
            (78, 549),
            (94, 645),
            (95, 93),
            (99, 117),
            (100, 651),
            (135, 861),
        ):
            self.assertEqual(hz("atsc", f"C{ch}"), mhz * 1_000_000, ch)  # fmt: skip
        for bad in ("C136", "1", "37"):
            with self.subTest(bad=bad), self.assertRaises(band.Error):
                hz("atsc", bad)

    def test_dvb_and_arib_plans(self):
        hz = band.center_hz
        self.assertEqual(hz("dvb", "21"), 474_000_000)
        self.assertEqual(hz("dvb", "69"), 858_000_000)
        self.assertEqual(hz("dvb", "6"), 178_000_000)  # VHF III on the 8 MHz raster
        self.assertEqual(hz("dvb", "C1"), 50_000_000)
        self.assertEqual(hz("dvb", "C38"), 346_000_000)
        self.assertEqual(hz("dvb", "C106"), 890_000_000)
        self.assertEqual(band.delsys("dvb", "21"), band.SYS_DVBT)
        self.assertEqual(band.delsys("dvb", "C38"), band.SYS_DVBC_ANNEX_A)
        self.assertEqual(hz("arib", "27"), 557_142_857)
        for b, bad in (("dvb", "20"), ("dvb", "70"), ("dvb", "C107"), ("arib", "C27")):
            with self.subTest(bad=bad), self.assertRaises(band.Error):
                hz(b, bad)


class LineupTest(unittest.TestCase):
    def test_us_cable(self):
        lu = ATSC
        rf = atsc.channel(lu, "C78")
        self.assertEqual((rf["channel"], rf["medium"], rf["modulation"]), (78, "cable", "qam256"))
        self.assertNotIn("modulation", atsc.channel(lu, "7"))
        # air and cable side by side, the same virtual channel on each
        text = (
            ATSC["text"]
            .replace("channel = 78\n", "channel = 7\n")
            .replace("major = 78", "major = 7")
            .replace('medium = "cable"\n', 'medium = "cable"\nmodulation = "qam64"\n')
        )
        both = lineup.parse(text, "t.toml", lineup.DEFAULTS_DIR)
        self.assertEqual([r["rf"] for r in both["rf"]], ["7", "14", "33", "C7"])
        twice = text + text[text.index('[[rf]]\nchannel = 7\nmedium = "cable"') :].replace(
            "channel = 7\n", "channel = 9\n", 1
        ).replace("source_id = 7801", "source_id = 7901")
        with self.assertRaises(lineup.Error) as e:
            lineup.parse(twice, "t.toml", lineup.DEFAULTS_DIR)
        self.assertIn("cable virtual channel 7.1 is given twice", str(e.exception))

    def test_dvb(self):
        lu = parse(DVB_MIN)
        rf = lu["rf"][0]
        self.assertEqual((rf["rf"], rf["system"], rf["network_id"], rf["network_name"]),
                         ("30", "t", 0xFF10, "Eins"))  # fmt: skip
        svc = rf["services"][0]
        self.assertEqual(
            (svc["lcn"], svc["provider"], svc["sound"][0]["language"]), (5, "Eins", "eng")
        )
        self.assertEqual((lu["timezone"], lu["country"]), ("Europe/Berlin", "DEU"))
        cable = parse(DVB_MIN.replace("channel = 30", 'channel = 30\nsystem = "c"'))["rf"][0]
        self.assertEqual((cable["rf"], cable["modulation"], cable["symbol_rate"]),
                         ("C30", "qam256", 6900000))  # fmt: skip
        cases = [
            (DVB_MIN.replace("channel = 30", 'channel = 30\nsystem = "t2"'), "DVB-T2 is not made yet"),
            (DVB_MIN.replace("channel = 30", 'channel = 30\nsymbol_rate = 6875000'), "a DVB-C channel's"),
            (DVB_MIN.replace("channel = 30", "channel = 20"), "no DVB-T channel 20"),
            (DVB_MIN.replace("lcn = 5\n", ""), "lcn is missing"),
            (DVB_MIN.replace("lcn = 5", "lcn = 1000"), "1000 is out of range"),
            ('country = "de"\n' + DVB_MIN, "an ISO 3166 alpha-3 code"),
            ('timezone = "Mars/Base"\n' + DVB_MIN, "no time zone"),
            (DVB_MIN + DVB_MIN.split('broadcast = "dvb"\n')[1].replace("channel = 30", "channel = 31")
             .replace("tsid = 0x0130", "tsid = 0x0131"), "DVB-T lcn 5 is given twice"),
            (DVB_MIN + DVB_MIN.split('broadcast = "dvb"\n')[1].replace("channel = 30", "channel = 31")
             .replace("lcn = 5", "lcn = 6"), "onid and tsid (65296, 304) is given twice"),
            (DVB_MIN.replace('name = "Eins"', 'name = "Eins"\nmajor = 1'), "unknown key major"),
        ]  # fmt: skip
        for text, want in cases:
            with self.subTest(want=want), self.assertRaises(lineup.Error) as e:
                parse(text)
            self.assertIn(want, str(e.exception))
        # an LCN once per system: the same on DVB-T and DVB-C is two lists on a set
        both = DVB_MIN + DVB_MIN.split('broadcast = "dvb"\n')[1].replace(
            "channel = 30", 'channel = 30\nsystem = "c"'
        ).replace("tsid = 0x0130", "tsid = 0x0230")
        self.assertEqual([r["rf"] for r in parse(both)["rf"]], ["30", "C30"])


def hz(text):
    """A frequency as TSDuck's XML writes it: 474,000,000."""
    return int(text.replace(",", ""))


def attrs(xml, tag):
    """The attributes of each element tag in the TSDuck XML body."""
    root = ET.fromstring(f"<tsduck>{xml}</tsduck>")
    return [e.attrib for e in root.iter(tag)]


class TablesTest(unittest.TestCase):
    NOW = 1_800_000_000  # 2027-01-15 08:00:00 UTC: 09:00 in Berlin

    def test_cvct_on_cable(self):
        t = atsc._tables(atsc.channel(ATSC, "C78"), self.NOW, ATSC["timezone"])
        self.assertTrue(t["tvct"].startswith('<CVCT version="0" transport_stream_id="1078">'))
        (ch,) = attrs(t["tvct"], "channel")
        self.assertEqual((ch["major_channel_number"], ch["modulation_mode"]), ("78", "256-QAM"))
        air = atsc._tables(atsc.channel(ATSC, "7"), self.NOW, ATSC["timezone"])
        self.assertTrue(air["tvct"].startswith("<TVCT "))
        self.assertEqual({c["modulation_mode"] for c in attrs(air["tvct"], "channel")}, {"8-VSB"})
        qam64 = dict(atsc.channel(ATSC, "C78"), modulation="qam64")
        t = atsc._tables(qam64, self.NOW, ATSC["timezone"])
        self.assertEqual(attrs(t["tvct"], "channel")[0]["modulation_mode"], "64-QAM")

    def test_dvb_t(self):
        t = dvb._tables(DVB, atsc.channel(DVB, "21"), self.NOW)
        (d,) = attrs(t["nit"], "terrestrial_delivery_system_descriptor")
        self.assertEqual((d["centre_frequency"], d["bandwidth"]), ("474000000", "8MHz"))
        self.assertEqual(attrs(t["nit"], "private_data_specifier_descriptor")[0]["private_data_specifier"],
                         "eacem")  # fmt: skip
        root = ET.fromstring(f"<tsduck>{t['nit']}</tsduck>")
        got = [(s.get("service_id"), s.get("logical_channel_number"))
               for s in root.find(".//eacem_logical_channel_number_descriptor")]  # fmt: skip
        self.assertEqual(got, [("8193", "1"), ("8194", "2")])
        self.assertIn('network_PID="0x10"', t["pat"])
        sdt = attrs(t["sdt"], "service")
        self.assertEqual(
            {(s["EIT_schedule"], s["EIT_present_following"]) for s in sdt}, {("true", "true")}
        )
        self.assertEqual([s["service_name"] for s in attrs(t["sdt"], "service_descriptor")],
                         ["Beanjar Eins", "Beanjar Zwei"])  # fmt: skip
        # MPEG-1 layer II tracks with their languages
        self.assertEqual(
            [(c["stream_type"], c["elementary_PID"]) for c in attrs(t["pmt0"], "component")],
            [("0x02", "0x111"), ("0x03", "0x112"), ("0x03", "0x113")],
        )
        self.assertEqual([x["code"] for x in attrs(t["pmt0"], "language")], ["deu", "eng"])
        # the guide: the present event and a day on, each service; one running
        root = ET.fromstring(f"<tsduck>{t['eit']}</tsduck>")
        for eit in root.iter("EIT"):
            starts = [datetime.strptime(e.get("start_time"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                      .timestamp() for e in eit.iter("event")]  # fmt: skip
            self.assertLessEqual(starts[0], self.NOW)
            self.assertGreaterEqual(starts[-1], self.NOW + 22 * 3600)
            running = [e for e in eit.iter("event") if e.get("running_status") == "running"]
            self.assertEqual(len(running), 1)
        # 09:00 in Berlin: Testbild (60), Nachrichten (30), Spielfilm (90) from midnight, so 09:00
        # starts the cycle's fifth hour: Testbild again
        first = root.find("EIT").find("event").find("short_event_descriptor")
        self.assertEqual(first.find("event_name").text, "Testbild")
        self.assertEqual(first.get("language_code"), "deu")
        # the TOT: Berlin is UTC+1 in January, +2 from the last Sunday of March
        (r,) = attrs(t["tot"], "region")
        self.assertEqual((r["country_code"], r["local_time_offset"], r["next_time_offset"]),
                         ("DEU", "60", "120"))  # fmt: skip
        self.assertEqual(r["time_of_change"], "2027-03-28 01:00:00")
        self.assertEqual(attrs(t["tdt"], "TDT")[0]["UTC_time"], "2027-01-15 08:00:00")

    def test_dvb_c(self):
        t = dvb._tables(DVB, atsc.channel(DVB, "C38"), self.NOW)
        (d,) = attrs(t["nit"], "cable_delivery_system_descriptor")
        self.assertEqual((d["frequency"], d["modulation"], d["symbol_rate"]),
                         ("346000000", "256-QAM", "6900000"))  # fmt: skip
        self.assertEqual(attrs(t["nit"], "terrestrial_delivery_system_descriptor"), [])

    def test_offset_without_changes(self):
        self.assertEqual(dvb.local_offset("UTC", self.NOW), (0, self.NOW + 365 * 86400, 0))

    def test_time_rewrite_is_utc(self):
        old = arib.jst_time(1_700_000_000, UTC)
        p = bytes([0x47, 0x40, 0x14, 0x10, 0x00, 0x70, 0x70, 0x05]) + old
        p += b"\xff" * (188 - len(p))
        q = dvb.time_rewrite(p, self.NOW)
        self.assertEqual(q[8:13], bytes.fromhex("EFEC") + bytes([0x08, 0x00, 0x00]))  # 08:00 UTC
        lu = antenna.DVB
        self.assertEqual(lu.clock[dvb.TOT_PID](p, self.NOW), q)

    def test_stream_key_follows_the_present_event(self):
        a = dvb.stream_key(DVB, "21", self.NOW)
        self.assertEqual(dvb.stream_key(DVB, "21", self.NOW + 60), a)
        self.assertNotEqual(dvb.stream_key(DVB, "21", self.NOW + 3 * 3600), a)
        self.assertTrue(dvb.stream_path("/c", DVB, "C38", self.NOW).startswith("/c/dvbC38-"))
        self.assertEqual(atsc.cache_owner(os.path.basename(dvb.stream_path("/c", DVB, "21", 0))),
                         DVB["key"][:8])  # fmt: skip


class CableDeliveryTest(unittest.TestCase):
    def test_inspect_reads_the_cable_frequency(self):
        """A user's DVB-C recording: its cable delivery descriptor's BCD frequency checked
        against the RF's."""
        from test_tuner_lineup import nit, stream, ts_packet

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rec.ts")
            dvbc = nit(0x44, bytes.fromhex("03460000") + b"\xff\xf2\x05\x06\x90\x00\x0f")
            tdt = bytes([0x70, 0x70, 0x05]) + arib.jst_time(1_800_000_000, UTC)
            stream(
                path, extra=[ts_packet(0x10, dvbc, start=True), ts_packet(0x14, tdt, start=True)]
            )
            rec = user_stream.inspect(path, "dvb", band.center_hz("dvb", "C38"), "C38")
            self.assertEqual((rec["standard"], rec["warnings"]), (["dvb"], []))
            w = user_stream.inspect(path, "dvb", band.center_hz("dvb", "C39"), "C39")["warnings"]
            self.assertIn("gives 346.000 MHz and RF C39 is 354.000 MHz", w[0])


@unittest.skipIf(
    dvb.tools() or not shutil.which("tstables"), "needs TSDuck (tsp, tstables) and ffmpeg"
)
class StreamTest(unittest.TestCase):
    """The built-in lineup's streams, made and read back by TSDuck."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="dvb-")
        cls.now = time.time()
        cls.ts = {ch: dvb.make_cached(cls.dir, DVB, ch, cls.now) for ch in ("21", "C38")}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def tables(self, ch, pid):
        xml = subprocess.run(
            ["tstables", "--pid", str(pid), "--max-tables", "4", "--xml-output", "-", self.ts[ch]],
            capture_output=True,
            check=True,
        ).stdout
        return ET.fromstring(xml)

    def test_made(self):
        for ch, n in (("21", 2), ("C38", 1)):
            r = dvb.validate(self.ts[ch])
            self.assertEqual(r["bad_crc"], 0)
            for t in dvb.NEEDED:
                self.assertIn(t, r["tables"], (ch, t))
            sdt = self.tables(ch, dvb.SDT_PID).find("SDT")
            self.assertEqual(len(sdt.findall("service")), n)
        nit = self.tables("21", dvb.NIT_PID).find("NIT")
        d = nit.find(".//terrestrial_delivery_system_descriptor")
        self.assertEqual(hz(d.get("centre_frequency")), 474_000_000)
        nit = self.tables("C38", dvb.NIT_PID).find("NIT")
        self.assertEqual(hz(nit.find(".//cable_delivery_system_descriptor").get("frequency")),
                         346_000_000)  # fmt: skip
        # a user's stream check takes the made stream as the board's standard, on its RF
        rec = user_stream.inspect(self.ts["21"], "dvb", band.center_hz("dvb", "21"), "21")
        self.assertEqual((rec["standard"], rec["warnings"], rec["programs"]),
                         (["dvb"], [], [0x2001, 0x2002]))  # fmt: skip

    def test_schedule_reaches_a_day_ahead(self):
        out = subprocess.run(
            ["tstables", "--pid", str(dvb.EIT_PID), "--all-sections", self.ts["21"]],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("EIT schedule Actual", out)
        starts = {
            line.split("Start UTC: ")[1] for line in out.splitlines() if "Start UTC: " in line
        }
        last = max(datetime.strptime(x, "%Y/%m/%d %H:%M:%S").replace(tzinfo=UTC) for x in starts)
        self.assertGreater(last.timestamp(), self.now + 22 * 3600)


if __name__ == "__main__":
    unittest.main()
