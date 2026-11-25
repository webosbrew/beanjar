#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's own broadcast, without a VM or tools: the lineup file format (beanjar/tuner_lineup.py)
and its errors, the built-in lineups as files, a lineup set and reset in a VM's state; a user's
transport stream (beanjar/tuner_ts.py): its errors and warnings on small synthesised streams, and
the loop over it (tuner.RecordedStream: cuts, continuity counters and the discontinuity
indicator over the wrap); the CLI's lineup and --stream for a VM on another host."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "host", "core"))

from beanjar import cli, remote, tuner, tuner_lineup, tuner_stream, tuner_ts
from beanjar import tuner_stream_arib as arib

ATSC_MIN = """
broadcast = "atsc"

[[rf]]
channel = 20
tsid = 2020

[[rf.services]]
major = 20
minor = 1
name = "MINE"
source_id = 2001
events = [{ title = "Show", minutes = 30 }]
"""

ARIB_MIN = """
broadcast = "arib"

[[rf]]
channel = 30
network_id = 0x7FD1
tsid = 0x7FD1
area_code = 0x5A5
remote_key = 3

[[rf.services]]
service_id = 0x0408
name = "テスト"
bml = "page"
events = [{ title = "番組", minutes = 60, text = "説明" }]
"""


def parse(text, base="/nonexistent", **kw):
    return tuner_lineup.parse(text, "t.toml", base, **kw)


class FormatTest(unittest.TestCase):
    def test_builtin_lineups(self):
        atsc = tuner_lineup.default("atsc")
        self.assertEqual([rf["channel"] for rf in atsc["rf"]], [7, 14, 33])
        svc = atsc["rf"][0]["services"][0]
        self.assertEqual(
            {k: svc[k] for k in ("major", "minor", "name", "source_id", "program")},
            {"major": 7, "minor": 1, "name": "BJ7", "source_id": 7001, "program": 1},
        )
        self.assertEqual((svc["picture"], svc["sound"], svc["label"]),
                         ({"pattern": "testsrc2"}, {"tone": 440}, True))  # fmt: skip
        self.assertEqual(svc["events"][0]["minutes"], 60)
        arib_lu = tuner_lineup.default("arib")
        rf = arib_lu["rf"][0]
        self.assertEqual(
            (rf["channel"], rf["network_id"], rf["tsid"], rf["area_code"], rf["remote_key"]),
            (27, 0x7FD0, 0x7FD0, 0x5A5, 1),
        )
        self.assertEqual(rf["services"][0]["service_id"], 0x0400)
        self.assertTrue(os.path.isfile(os.path.join(rf["services"][0]["bml"], "startup.bml")))
        # the built-in lineups are the tuner's
        self.assertEqual(tuner.ATSC.channels, [7, 14, 33])
        self.assertEqual(tuner.ATSC.services(7), ["7.1 BJ7", "7.2 BJ7-2"])
        self.assertEqual(tuner.ARIB.services(27), ["1 ビーンジャー１"])

    def test_round_trip(self):
        """A built-in lineup installed and read back is the same lineup: the same key, values and
        files."""
        for b in ("atsc", "arib"):
            with tempfile.TemporaryDirectory() as d:
                dest = os.path.join(d, "lineup")
                got = tuner_lineup.install(tuner_lineup.default_path(b), dest, b)
                want = tuner_lineup.default(b)
                self.assertEqual(got["key"], want["key"], b)
                self.assertEqual(tuner_lineup.public(got), tuner_lineup.public(want))
                self.assertEqual(got["text"], want["text"])
                for p in tuner_lineup.paths(got):
                    self.assertTrue(p.startswith(dest + os.sep), p)

    def test_defaults_and_values(self):
        lu = parse(ATSC_MIN)
        (svc,) = lu["rf"][0]["services"]
        self.assertEqual(svc["long_name"], "MINE")
        self.assertEqual(svc["program"], 1)
        self.assertEqual(svc["picture"], {"pattern": "testsrc2"})
        self.assertEqual(svc["events"], [{"title": "Show", "minutes": 30, "text": ""}])
        self.assertEqual(lu["timezone"], "America/New_York")
        self.assertEqual(lu["video"], tuner_lineup.default("atsc")["video"])  # the built-in's
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "page"))
            with open(os.path.join(d, "page", "startup.bml"), "wb") as f:
                f.write(b"<bml/>")
            lu = parse(ARIB_MIN, d)
            rf = lu["rf"][0]
            self.assertEqual((rf["ts_name"], rf["network_name"]), ("テスト", "テスト"))
            self.assertEqual(rf["services"][0]["provider"], "テスト")
            self.assertEqual(rf["services"][0]["bml"], os.path.join(d, "page"))
            self.assertEqual(tuner_lineup.public(lu)["rf"][0]["services"][0]["bml"], "page")
            # the key follows a file's content
            key = lu["key"]
            with open(os.path.join(d, "page", "startup.bml"), "wb") as f:
                f.write(b"<bml>other</bml>")
            os.utime(os.path.join(d, "page", "startup.bml"), ns=(1, 1))
            self.assertNotEqual(parse(ARIB_MIN, d)["key"], key)

    def test_errors(self):
        cases = [
            ("broadcast = 'dvb'\n[[rf]]\n", "broadcast: 'dvb': atsc or arib"),
            (ATSC_MIN.replace("tsid = 2020", "tsid = 2020\ncolour = 1"), "rf[0]: unknown key colour"),
            (ATSC_MIN.replace("source_id = 2001\n", ""), "rf[0].services[0]: source_id is missing"),
            (ATSC_MIN.replace("channel = 20", "channel = 40"), "rf[0].channel: 40 is out of range"),
            (ATSC_MIN.replace('"MINE"', '"TOOLONGNAME"'), "is longer than 7 characters"),
            (ATSC_MIN.replace("events = [{ title = \"Show\", minutes = 30 }]", "events = []"),
             "events: must be a list of one event or more"),
            (ATSC_MIN + 'picture = "life"\n', "no test pattern 'life'"),
            (ATSC_MIN + 'picture = { image = "../x.png" }\n', "stays inside the lineup file's folder"),
            (ATSC_MIN + 'picture = { image = "/x.png" }\n', "give it relative to the lineup file"),
            (ATSC_MIN + 'picture = { video = "clip.mp4" }\n', "clip.mp4: no such file"),
            (ATSC_MIN + "sound = { tone = 5 }\n", "sound.tone: 5 is out of range"),
            (ATSC_MIN + ATSC_MIN.split("broadcast = \"atsc\"\n")[1], "channel 20 is given twice"),
            ('timezone = "Mars/Base"\n' + ATSC_MIN, "no time zone"),
            ("broadcast = 'atsc'\nrf = [", "not TOML"),
            (ARIB_MIN.replace("remote_key = 3", "remote_key = 13").replace('bml = "page"\n', ""),
             "remote_key: 13 is out of range"),
            (ARIB_MIN, "page: no such folder"),
        ]  # fmt: skip
        for text, want in cases:
            with self.subTest(want=want), self.assertRaises(tuner_lineup.Error) as e:
                parse(text)
            self.assertIn(want, str(e.exception))
        with self.assertRaises(tuner_lineup.Error) as e:
            parse(ATSC_MIN, broadcast="arib")
        self.assertIn(
            "the lineup is atsc, the board's broadcast standard is arib", str(e.exception)
        )
        # a lineup whose files are elsewhere (a host fed from another machine)
        self.assertEqual(parse(ARIB_MIN, check_files=False)["rf"][0]["channel"], 30)

    def test_bml_folder_needs_startup(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "page"))
            with open(os.path.join(d, "page", "menu.bml"), "wb") as f:
                f.write(b"<bml/>")
            with self.assertRaises(tuner_lineup.Error) as e:
                parse(ARIB_MIN, d)
            self.assertIn("no startup.bml", str(e.exception))

    def test_arib_tables_of_several_services(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "page"))
            with open(os.path.join(d, "page", "startup.bml"), "wb") as f:
                f.write(b"<bml/>")
            text = ARIB_MIN + (
                '\n[[rf.services]]\nservice_id = 0x0409\nname = "二"\n'
                'events = [{ title = "x", minutes = 30 }]\n'
            )
            lu = parse(text, d)
            rf = lu["rf"][0]
            mods = [arib.service_module(s) for s in rf["services"]]
            self.assertIsNotNone(mods[0])
            self.assertIsNone(mods[1])
            t = arib._tables(rf, 1_800_000_000, mods)
            self.assertIn('service_id="1032" program_map_PID="0x1F0"', t["pat"])
            self.assertIn('service_id="1033" program_map_PID="0x1F1"', t["pat"])
            self.assertIn("0x740", t["pmt0"])
            self.assertNotIn("data_component", t["pmt1"])
            self.assertIn('PCR_PID="0x121"', t["pmt1"])
            self.assertIn('remote_control_key_id="3"', t["nit"])
            self.assertIn(f'value="{arib.uhf_center_hz(30)}"', t["nit"])
            self.assertEqual(t["eit"].count("<EIT "), 2)


# ---- streams ----


def ts_packet(pid, payload=b"", start=False, cc=0, tsc=0, pcr=None, disc=False):
    head = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF])
    adapt = b""
    if pcr is not None:
        base, ext = pcr // 300, pcr % 300
        field = bytes([
            (base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF, (base >> 1) & 0xFF,
            ((base & 1) << 7) | 0x7E | (ext >> 8), ext & 0xFF,
        ])  # fmt: skip
        adapt = bytes([7, 0x10 | (0x80 if disc else 0)]) + field
    afc = (2 if adapt else 0) | (1 if payload or not adapt else 0)
    head += bytes([(tsc << 6) | (afc << 4) | (cc & 0xF)])
    body = adapt + ((b"\x00" + payload) if start else payload)
    return head + body + b"\xff" * (184 - len(body))


def psi(table_id, body):
    length = len(body) + 4
    sec = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF]) + body
    return sec + tuner_stream.crc32_mpeg(sec).to_bytes(4, "big")


PAT = psi(0x00, b"\x00\x01\xc1\x00\x00" + b"\x00\x01\xe1\x00")  # program 1, PMT 0x100


def nit(tag, freq_field):
    desc = bytes([tag, len(freq_field)]) + freq_field
    entry = b"\x00\x01\x00\x01" + bytes([0xF0 | (len(desc) >> 8), len(desc) & 0xFF]) + desc
    body = (
        b"\x00\x01\xc1\x00\x00"
        + b"\xf0\x00"
        + bytes([0xF0 | (len(entry) >> 8), len(entry)])
        + entry
    )
    return psi(0x40, body)


def stream(path, seconds=2.0, rate=188 * 8 * 100, tsc_at=None, pat=True, pcr=True, extra=()):
    """A small stream: 100 packets a second at rate, PAT every 10, PCR on 0x100 every 5, video
    payload on 0x100 starting a PES every 10, and extra packets (PSI) first."""
    pkts = list(extra)
    n = int(seconds * rate / 8 / 188)
    per_tick = 27_000_000 * 188 * 8 // rate
    for i in range(len(pkts), n):
        if pat and i % 10 == 0:
            pkts.append(ts_packet(0, PAT, start=True, cc=i // 10))
        elif pcr and i % 5 == 1:
            pkts.append(ts_packet(0x100, b"", pcr=i * per_tick, cc=i))
        else:
            tsc = 2 if tsc_at is not None and i >= tsc_at else 0
            pkts.append(ts_packet(0x101, b"v" * 10, start=i % 10 == 2, cc=i, tsc=tsc))
    with open(path, "wb") as f:
        f.write(b"".join(pkts))
    return pkts


class StreamCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "rec.ts")

    def check(self, broadcast="atsc", ch=20):
        center = tuner_stream.rf_center_hz(ch) if broadcast == "atsc" else arib.uhf_center_hz(ch)
        return tuner_ts.inspect(self.path, broadcast, center, ch)

    def error(self, want, **kw):
        with self.assertRaises(tuner.Error) as e:
            self.check(**kw)
        self.assertIn(want, str(e.exception))

    def test_good(self):
        stream(self.path)
        rec = self.check()
        self.assertEqual((rec["packets"], rec["rate"], rec["programs"]), (200, 188 * 8 * 100, [1]))
        self.assertEqual(rec["pcr_pids"], [0x100])
        self.assertEqual(rec["seconds"], 2.0)
        self.assertEqual(rec["cuts"][str(0x101)], [2, 192])  # the first and last PES starts
        self.assertIn("neither a NIT nor ATSC PSIP", rec["warnings"][0])
        self.assertIn("no STT", rec["warnings"][1])

    def test_not_packets(self):
        stream(self.path)
        with open(self.path, "ab") as f:
            f.write(b"\x47" * 10)
        self.error("not a whole number of 188-byte packets")
        with open(self.path, "wb") as f:
            f.write(b"\x47" * 192 * 10)
        self.error("192-byte M2TS")

    def test_unsynced(self):
        pkts = stream(self.path)
        pkts[7] = b"\x00" + pkts[7][1:]
        with open(self.path, "wb") as f:
            f.write(b"".join(pkts))
        self.error("packet 7 (byte 1316) has no sync byte 0x47")

    def test_scrambled(self):
        stream(self.path, tsc_at=50)  # at 0.5 s
        self.error("scrambled (transport_scrambling_control 2 on PID 0x0101 at 0.5 s)")
        self.error("B-CAS")
        stream(self.path, seconds=8, tsc_at=700)  # at 7 s: past the first seconds
        self.assertIn("scrambled packets from 7 s on", self.check()["warnings"][-1])

    def test_no_pat_no_pcr(self):
        stream(self.path, pat=False)
        self.error("no PAT")
        stream(self.path, pcr=False)
        self.error("no PCR")

    def test_standard_and_frequency(self):
        # ISDB-T delivery: area/guard/mode, then UHF 30 in 1/7 MHz
        isdbt = nit(0xFA, b"\x5a\x56" + round(arib.uhf_center_hz(30) * 7 / 1e6).to_bytes(2, "big"))
        tot = bytes([0x70, 0x70, 0x05]) + arib.jst_time(1_800_000_000)
        extra = [ts_packet(0x10, isdbt, start=True), ts_packet(0x14, tot, start=True)]
        stream(self.path, extra=extra)
        self.assertEqual(self.check("arib", 30)["warnings"], [])
        self.assertEqual(self.check("arib", 30)["standard"], ["arib"])
        w = self.check("arib", 31)["warnings"]
        self.assertIn("delivery descriptor gives 575.143 MHz and RF 31 is 581.143 MHz", w[0])
        w = self.check("atsc", 20)["warnings"]
        self.assertIn("tables are ISDB-T (NIT) and the board's broadcast standard is atsc", w[0])
        dvbt = nit(0x5A, (474_000_000 // 10).to_bytes(4, "big") + b"\x00" * 7)
        stream(self.path, extra=[ts_packet(0x10, dvbt, start=True)])
        w = self.check("arib", 30)["warnings"]
        self.assertIn("tables are DVB (NIT)", w[0])
        self.assertIn("gives 474.000 MHz", w[1])
        self.assertIn("no TDT or TOT", w[2])


class LoopTest(unittest.TestCase):
    def test_wrap(self):
        """Over the wrap: each PID from its first start to before its last, the counters on, and
        the first PCR after the wrap marked as a discontinuity."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rec.ts")
            pkts = stream(path, seconds=1.0)
            rec = tuner_ts.inspect(path, "atsc")
            s = tuner.RecordedStream(path, rec, clock={})
            out = s.next(3.05)  # three loops and a bit
            self.assertEqual(s.loops, 3)
            got = [out[i : i + 188] for i in range(0, len(out), 188)]

            def pid_of(p):
                return ((p[1] & 0x1F) << 8) | p[2]

            def kept(i):
                cut = rec["cuts"].get(str(pid_of(pkts[i])))
                return cut is None or cut[0] <= i < cut[1]

            loop = [i for i in range(len(pkts)) if kept(i)]
            self.assertEqual(len(got), 3 * len(loop) + len([i for i in loop if i < 5]))
            ccs = {}
            for i, p in enumerate(got):
                if p[3] & 0x10:
                    pid = pid_of(p)
                    if pid in ccs:
                        self.assertEqual(p[3] & 0xF, (ccs[pid] + 1) & 0xF, f"packet {i}")
                    ccs[pid] = p[3] & 0xF
            disc = [i for i, p in enumerate(got) if p[3] & 0x20 and p[5] & 0x10 and p[5] & 0x80]
            first_pcr = loop.index(1)  # the file's first PCR packet
            self.assertEqual(disc, [k * len(loop) + first_pcr for k in (1, 2, 3)])
            # after each wrap the video starts with a PES start
            first_video = next(n for n, i in enumerate(loop) if pid_of(pkts[i]) == 0x101)
            for k in (1, 2):
                self.assertTrue(got[k * len(loop) + first_video][1] & 0x40)

    def test_first_loop_unchanged_but_cuts(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rec.ts")
            pkts = stream(path, seconds=1.0)
            rec = tuner_ts.inspect(path, "atsc")
            s = tuner.RecordedStream(path, rec, clock={})
            out = s.next(0.99)
            got = [out[i : i + 188] for i in range(0, len(out), 188)]
            want = []
            for i, p in enumerate(pkts[:99]):
                cut = rec["cuts"].get(str(((p[1] & 0x1F) << 8) | p[2]))
                if cut is None or cut[0] <= i < cut[1]:
                    want.append(p)
            self.assertEqual(len(got), len(want))

            # the PAT's packets, whose counters run on, are as they were
            def pat(ps):
                return [p for p in ps if p[1] & 0x1F == 0 and p[2] == 0]

            self.assertEqual(pat(got), pat(want))


# ---- a VM's lineup and streams ----


class VM:
    name = "tv"

    def __init__(self, d):
        self.state_dir = os.path.join(d, "state")
        self.run_dir = os.path.join(d, "run")

    def target(self):
        return ("o22", "queue")


class VmTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.vm = VM(tmp.name)
        self.tree = mock.Mock()
        self.tree.cache.return_value = os.path.join(tmp.name, "cache")
        self.made = []
        for name, value in (
            ("has_tuner", lambda target: True),
            ("vm_broadcast", lambda vm: "atsc"),
            ("make", lambda cache, channels, hint="", lu=None: self.made.append((lu.own, list(channels)))),
        ):  # fmt: skip
            p = mock.patch.object(tuner, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_set_show_reset(self):
        src = os.path.join(self.dir, "mine")
        os.makedirs(src)
        with open(os.path.join(src, "lineup.toml"), "w") as f:
            f.write(ATSC_MIN + 'picture = { image = "art/logo.png" }\n')
        os.makedirs(os.path.join(src, "art"))
        with open(os.path.join(src, "art", "logo.png"), "wb") as f:
            f.write(b"png")
        tuner.save_map(self.vm.state_dir, [7, 14])
        data = tuner.lineup_set(self.vm, self.tree, os.path.join(src, "lineup.toml"))
        self.assertEqual((data["lineup"], data["unmapped"]), ("own", [7, 14]))
        self.assertEqual(tuner.mapped(self.vm.state_dir), [])
        shutil.rmtree(src)  # the VM no longer depends on the user's files
        shown = tuner.lineup_show(self.vm)
        self.assertEqual(
            shown["values"]["rf"][0]["services"][0]["picture"], {"image": "art/logo.png"}
        )
        self.assertIn('name = "MINE"', shown["text"])
        st = tuner.status(self.vm)
        self.assertEqual(
            [(c["channel"], c["services"]) for c in st["channels"]], [(20, ["20.1 MINE"])]
        )
        self.assertEqual(st["lineup"], "own")
        tuner.change(self.vm, self.tree, add=[20])
        self.assertEqual(self.made[-1], (True, [20]))
        with self.assertRaises(tuner.Error):
            tuner.change(self.vm, self.tree, add=[7])  # not the own lineup's
        data = tuner.lineup_reset(self.vm)
        self.assertEqual((data["lineup"], data["unmapped"]), ("built-in", [20]))
        self.assertEqual(tuner.lineup_show(self.vm)["text"], tuner_lineup.default("atsc")["text"])

    def test_set_of_another_standard(self):
        with self.assertRaises(tuner.Error) as e:
            tuner.lineup_set(self.vm, self.tree, "-", ARIB_MIN)
        self.assertIn(
            "the lineup is arib, the board's broadcast standard is atsc", str(e.exception)
        )

    def test_fed_lineup_keeps_only_the_file(self):
        with mock.patch.object(tuner, "vm_broadcast", lambda vm: "arib"):
            data = tuner.lineup_set(
                self.vm, self.tree, "-", ARIB_MIN, fed=True
            )  # page/ is elsewhere
            self.assertEqual(data["lineup"], "own")
            lu = tuner.vm_lineup(self.vm.state_dir, "arib")
            self.assertEqual(lu.channels, [30])
            with self.assertRaises(tuner_stream.Error) as e:
                lu.make("/nonexistent", 30, 0)
            self.assertIn("machine that feeds this VM", str(e.exception))

    def test_stream_map_and_unmap(self):
        rec_path = os.path.join(self.dir, "rec.ts")
        stream(rec_path)
        data = tuner.change(self.vm, self.tree, add=[20], stream=rec_path)
        self.assertIn("no STT", " ".join(data["warnings"]))
        (c,) = [c for c in data["channels"] if c["channel"] == 20]
        self.assertEqual((c["mapped"], c["stream"], c["services"]), (True, "rec.ts", ["program 1"]))
        copied = os.path.join(self.vm.state_dir, tuner.STREAMS_DIR, "rf20.ts")
        self.assertTrue(os.path.isfile(copied))
        os.unlink(rec_path)  # played from the VM's state
        recorded = tuner.streams(self.vm.state_dir)
        self.assertEqual(recorded[20]["path"], copied)
        player = tuner.Player("/nonexistent", print, tuner.ATSC, lambda: recorded)
        ready = player.ready([20])
        self.assertIsInstance(ready[tuner_stream.rf_center_hz(20)], tuner.RecordedStream)
        self.assertEqual(
            tuner.channels_of([tuner_stream.rf_center_hz(20)], tuner.ATSC, recorded), [20]
        )
        tuner.change(self.vm, self.tree, remove=[20])
        self.assertFalse(os.path.exists(copied))
        self.assertEqual(tuner.streams(self.vm.state_dir), {})
        with self.assertRaises(tuner.Error):
            tuner.change(self.vm, self.tree, add=[37], stream=rec_path)

    def test_bad_stream_is_not_kept(self):
        rec_path = os.path.join(self.dir, "rec.ts")
        stream(rec_path, tsc_at=0)
        with self.assertRaises(tuner.Error) as e:
            tuner.change(self.vm, self.tree, add=[20], stream=rec_path)
        self.assertIn("scrambled", str(e.exception))
        self.assertEqual(tuner.mapped(self.vm.state_dir), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.vm.state_dir, tuner.STREAMS_DIR, "rf20.ts"))
        )


class RemoteTest(unittest.TestCase):
    """lineup set and map --stream for HOST/NAME: the files stay here (the feed's directory for
    the VM), the host gets the lineup's TOML and the stream's name (--fed)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        env = {
            "XDG_CONFIG_HOME": os.path.join(tmp.name, "config"),
            "BEANJAR_LIBRARY": os.path.join(tmp.name, "lib"),
            "XDG_RUNTIME_DIR": tmp.name,
        }
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        mock.patch.object(remote, "transport", self.transport).start()
        self.made = mock.patch.object(tuner, "make").start()
        self.start = mock.patch.object(tuner, "start_feed").start()
        mock.patch.object(tuner, "stop_feed").start()
        self.addCleanup(mock.patch.stopall)
        remote.add_host("pi", "ssh://pi.example")

    def transport(self, argv, stdin=None, on_stderr=None, timeout=None):
        words = argv[argv.index("--") + 2].split()[1:]
        self.calls.append((words, stdin))
        channels = [{"channel": 20, "center_hz": tuner_stream.rf_center_hz(20), "mapped": True,
                     "services": [], "stream": None}]  # fmt: skip
        data = {
            "name": "tv", "tuner": True, "broadcast": "atsc", "delsys": 11, "lineup": "own",
            "channels": channels, "tuned_hz": None, "sent_packets": 0, "connected": True,
            "fed": False, "feed_socket": "/l/tv/run/tuner-feed.sock",
        }  # fmt: skip
        if words[2] == "lineup":
            data = {"name": "tv", "broadcast": "atsc", "lineup": "own", "text": ATSC_MIN,
                    "values": tuner_lineup.public(parse(ATSC_MIN)), "unmapped": []}  # fmt: skip
        return 0, json.dumps(data).encode(), ""

    def beanjar(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--repo", self.dir, *argv])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def mirror(self):
        return tuner.remote_dir(os.path.join(self.dir, "lib"), "pi", "tv")

    def test_lineup_set(self):
        path = os.path.join(self.dir, "mine.toml")
        with open(path, "w") as f:
            f.write(ATSC_MIN)
        rc, out, err = self.beanjar("io", "tuner", "lineup", "set", "pi/tv", path)
        self.assertEqual(rc, 0, err)
        words, stdin = self.calls[-1]
        self.assertEqual(
            words, ["io", "tuner", "lineup", "set", "local/tv", "-", "--fed", "--json"]
        )
        self.assertEqual(stdin.decode(), ATSC_MIN)
        self.assertTrue(
            os.path.isfile(os.path.join(self.mirror(), tuner.LINEUP_DIR, "lineup.toml"))
        )
        self.assertEqual(self.made.call_args.args[1], [20])  # the mapped channel, made here
        self.assertIn("its own ATSC lineup: RF20 (1 service)", out)
        rc, _out, err = self.beanjar("io", "tuner", "lineup", "reset", "pi/tv")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.mirror(), tuner.LINEUP_DIR)))

    def test_map_stream(self):
        rec_path = os.path.join(self.dir, "rec.ts")
        stream(rec_path)
        rc, _out, err = self.beanjar("io", "tuner", "map", "pi/tv", "20", "--stream", rec_path)
        self.assertEqual(rc, 0, err)
        self.assertIn("no STT", err)
        words, _stdin = self.calls[-1]
        self.assertEqual(
            words, ["io", "tuner", "map", "local/tv", "20", "--fed", "--stream", "rec.ts", "--json"]
        )
        self.assertTrue(os.path.isfile(os.path.join(self.mirror(), tuner.STREAMS_DIR, "rf20.ts")))
        self.assertEqual(self.start.call_args.kwargs["content"], self.mirror())
        self.made.assert_not_called()
        rc, _out, err = self.beanjar(
            "io", "tuner", "map", "pi/tv", "20", "21", "--stream", rec_path
        )
        self.assertEqual(rc, 2)
        self.assertIn("one physical channel", err)
        self.assertEqual(self.beanjar("io", "tuner", "unmap", "pi/tv", "all")[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.mirror(), tuner.STREAMS_DIR, "rf20.ts")))


if __name__ == "__main__":
    unittest.main()
