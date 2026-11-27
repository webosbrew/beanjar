#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The host's tuner (beanjar/tuner/antenna.py, atsc.py) without a VM: the wire frames of
include/webos-tuner.h, the guest's tune frames taken apart at any split, a stream looped at its mux
rate without nulls and with the STT set to now (its CRC made again), the map kept in the VM's
state, the US channel frequencies, the guide's events and the independent section check; the
split between the feeder beside QEMU and a generator elsewhere (a feed) over sockets, with no
TSDuck, ffmpeg or ssh, and the CLI's remote map and unmap against a fake host; the lineup per
broadcast standard (the Japanese ISDB-T one: its delivery system in the map, a stream only to an
ISDB-T tune, its TDT and TOT set to now)."""

import contextlib
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "host", "core"))

from beanjar.access import ares, remote
from beanjar import cli
from beanjar.tuner import antenna, band, lineup, atsc
from beanjar.tuner import arib as arib

ATSC = lineup.default("atsc")


def section(table_id, body, long=True):
    """A PSI section with its CRC (long) of table_id and body (after the 3-byte header)."""
    length = len(body) + (4 if long else 0)
    head = bytes([table_id, (0xB0 if long else 0x30) | (length >> 8), length & 0xFF])
    sec = head + body
    return sec + (atsc.crc32_mpeg(sec).to_bytes(4, "big") if long else b"")


def packet(pid, payload=b"", start=True):
    head = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF, 0x10])
    data = (b"\x00" if start else b"") + payload
    return head + data + b"\xff" * (184 - len(data))


def stt(gps):
    # table_id_extension 0, version 0xC1, section 0, last 0, protocol 0, time, offset 18, DS 0
    return section(0xCD, b"\x00\x00\xc1\x00\x00\x00" + gps.to_bytes(4, "big") + b"\x12\x00\x00")


class WireTest(unittest.TestCase):
    def test_map_and_ts_frames(self):
        f = antenna.map_frame([(177000000, 11), (177000000, 2)])
        self.assertEqual(struct.unpack_from("<IIII", f), (0x4E555457, 1, 0, 16))
        self.assertEqual(struct.unpack_from("<IIII", f, 16), (177000000, 11, 177000000, 2))
        pkts = packet(0) * 1100
        frames = antenna.ts_frames(177000000, pkts)
        lens, at = [], 0
        while at < len(frames):
            magic, kind, freq, n = struct.unpack_from("<IIII", frames, at)
            self.assertEqual((magic, kind, freq), (0x4E555457, 2, 177000000))
            lens.append(n)
            at += 16 + n
        self.assertEqual(lens, [512 * 188, 512 * 188, 76 * 188])

    def test_tune_reader_any_split(self):
        data = b"xx" + struct.pack("<IIIII", 0x4E555457, 3, 0, 4, 0)
        data += struct.pack("<IIIII", 0x4E555457, 3, 177000000, 4, 11)
        for step in (1, 3, 7, len(data)):
            r = antenna.TuneReader()
            for i in range(0, len(data), step):
                r.feed(data[i : i + step])
            self.assertEqual((r.tuned, r.delsys), (177000000, 11))

    def test_find_tolerance(self):
        streams = {(177000000, 11): "s7", (177000000, 2): "c7"}
        self.assertEqual(antenna.find(streams, 177000000, 11), "s7")
        self.assertEqual(antenna.find(streams, 177310000, 11), "s7")
        self.assertEqual(antenna.find(streams, 177000000, 2), "c7")  # cable C7, same centre
        self.assertIsNone(antenna.find(streams, 177000000, 3))
        self.assertIsNone(antenna.find(streams, 183000000, 11))
        self.assertIsNone(antenna.find(streams, 0, 11))


class StreamTest(unittest.TestCase):
    def test_pacing_nulls_and_stt(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.ts")
            pkts = [packet(0, section(0x00, b"\x03\xef\xc1\x00\x00\x00\x01\xe0\x20"))]
            pkts += [packet(0x1FFF)] * 8
            pkts += [packet(0x1FFB, stt(1000))]
            with open(path, "wb") as f:
                f.write(b"".join(pkts))
            s = antenna.Stream(path, rate=188 * 8 * 100)  # 100 packets a second
            out = s.next(0.1, now=atsc.GPS_EPOCH + 5000 - 18)  # 10 packets: one loop
            got = [out[i : i + 188] for i in range(0, len(out), 188)]
            self.assertEqual(len(got), 2)  # the nulls dropped
            self.assertEqual(got[0], pkts[0])
            sec = got[1][5 : 5 + 20]
            self.assertEqual(int.from_bytes(sec[9:13], "big"), 5000)
            self.assertEqual(atsc.crc32_mpeg(sec), 0)
            self.assertEqual(s.next(0.005), b"")  # half a packet owed
            self.assertEqual(len(s.next(0.005)), 188)

    def test_validate(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.ts")
            good = section(0x00, b"\x03\xef\xc1\x00\x00\x00\x01\xe0\x20")
            bad = bytearray(good)
            bad[-1] ^= 1
            with open(path, "wb") as f:
                f.write(packet(0, good) + packet(0, bytes(bad)) + packet(0x1FFB, stt(7)))
            r = atsc.validate(path)
            self.assertEqual((r["ok_crc"], r["bad_crc"]), (2, 1))
            self.assertEqual(r["tables"], {"PAT": 2, "STT": 1})

    def test_mgt_versions(self):
        def mgt(v):
            entry = (
                b"\x01\x00" + bytes([0xFD, 0x00, 0xE0 | v]) + (100).to_bytes(4, "big") + b"\xf0\x00"
            )
            return section(0xC7, b"\x00\x00\xc1\x00\x00\x00\x00\x01" + entry + b"\xf0\x00")

        def eit(v):
            return section(0xCB, b"\x1b\x59" + bytes([0xC1 | v << 1]) + b"\x00\x00\x00\x00")

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.ts")
            for listed, carried, want in (
                (5, 5, []),
                (28, 3, ["PID 0x1D00: MGT v28, sections v3"]),
            ):
                with open(path, "wb") as f:
                    f.write(packet(0x1FFB, mgt(listed)) + packet(0x1D00, eit(carried)))
                r = atsc.validate(path)
                self.assertEqual((r["bad_crc"], r["mgt_mismatch"]), (0, want))


class LineupTest(unittest.TestCase):
    def test_second_track_and_captions_in_the_tables(self):
        """The built-in 7.1: two AC-3 tracks (English, Spanish) with their languages in the PMT's
        ISO 639 and AC-3 descriptors and the TVCT's service location, and line 21 captions named
        by the caption service descriptor on the video in the PMT and in each EIT event."""
        rf = atsc.channel(ATSC, "7")
        t = atsc._tables(rf, 1_800_000_000, ATSC["timezone"])
        pmt = t["pmt0"]
        for pid, lang, mainid in ((0x34, "eng", 0), (0x32, "spa", 1)):
            self.assertIn(f'<component stream_type="0x81" elementary_PID="0x{pid:X}">', pmt)
            self.assertIn(f'mainid="{mainid}" priority="1" asvcflags="0" language="{lang}"', pmt)
            self.assertIn(f'<language code="{lang}" audio_type="0"/>', pmt)
            self.assertIn(f'elementary_PID="0x{pid:X}" ISO_639_language_code="{lang}"', t["tvct"])
        self.assertLess(pmt.index("0x34"), pmt.index("0x32"))  # the main track first
        cc = '<caption_service_descriptor><service language="eng" digital_cc="false" line21_field="false"'
        self.assertIn('elementary_PID="0x31">\n    ' + cc, pmt)
        self.assertNotIn("caption", t["pmt1"])  # 7.2 has none
        self.assertNotIn("0x32", t["pmt1"])
        eit = t["eit0"].split("<ATSC_EIT ")
        self.assertEqual(eit[1].count("<event "), eit[1].count(cc))
        self.assertNotIn(cc, eit[2])
        # ffmpeg's streams in order and where tsp puts them
        self.assertEqual(
            atsc.streams(rf["services"]), [(0, None), (0, 0), (0, 1), (1, None), (1, 0)]
        )
        keep, remap = atsc.ff_remap(
            rf["services"],
            lambda i, k: atsc.video_pid(i) if k is None else atsc.audio_pid(i, k),
        )
        self.assertEqual(remap, ["256=49", "257=52", "258=50", "259=53", "260=56"])
        self.assertEqual(atsc.ff_programs(rf["services"]),
                         ["-program", "st=0:st=1:st=2", "-program", "st=3:st=4"])  # fmt: skip
        # no PID twice, in any lineup of 16 services of 3 tracks
        pids = [atsc.video_pid(i) for i in range(16)] + [
            atsc.audio_pid(i, k) for i in range(16) for k in range(3)]  # fmt: skip
        self.assertEqual(len(set(pids)), len(pids))

    def test_captions_go_into_each_pictures_user_data(self):
        """A/53 cc_data after each picture's headers, before its first slice: one field-1 pair a
        picture, 608's null pair where the captions have none."""
        seq = b"\x00\x00\x01\xb3" + b"\x00" * 8 + b"\x00\x00\x01\xb5" + b"\x14" * 6
        pic = b"\x00\x00\x01\x00\x00\x0f\xff\xf8" + b"\x00\x00\x01\xb5\x8f\xff"
        sl = b"\x00\x00\x01\x01\x12\x34" + b"\x00\x00\x01\x02\x56"
        es = seq + pic + sl + pic + sl + pic + sl
        out, n = atsc.with_cc(es, [b"\x94\x2c", None])
        self.assertEqual(n, 3)
        ud = [atsc.a53_user_data(p) for p in (b"\x94\x2c", None, None)]
        self.assertEqual(ud[0], b"\x00\x00\x01\xb2GA94\x03\x42\xff\xfc\x94\x2c\xfd\x80\x80\xff")
        self.assertEqual(ud[1][-8:], b"\xff\xfc\x80\x80\xfd\x80\x80\xff")
        self.assertEqual(out, seq + pic + ud[0] + sl + pic + ud[1] + sl + pic + ud[2] + sl)

    def test_frequencies(self):
        hz = band.center_hz
        self.assertEqual(hz("atsc", "2"), 57000000)
        self.assertEqual(hz("atsc", "7"), 177000000)
        self.assertEqual(hz("atsc", "14"), 473000000)
        self.assertEqual(hz("atsc", "33"), 587000000)
        with self.assertRaises(band.Error):
            hz("atsc", "37")
        with self.assertRaises(atsc.Error):
            atsc.channel(ATSC, "8")

    def test_events_cover_now(self):
        svc = atsc.channel(ATSC, "7")["services"][0]
        now = 1_800_000_000
        for t in range(now, now + 86400, 1800):
            on_air = atsc.events(svc, t, t + 1)
            self.assertEqual(len(on_air), 1, t)
            start, minutes, _title, _text = on_air[0]
            self.assertTrue(start <= t < start + minutes * 60)

    def test_map_kept(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(antenna.mapped(d), [])
            antenna.save_map(d, ["14", "C7", "7", "14"])
            self.assertEqual(antenna.mapped(d), ["7", "14", "C7"])


def fake_make(cache, lu, ch, now):
    """tuner_stream.make without the tools: the stream if it is in cache."""
    path = atsc.stream_path(cache, lu, ch, now)
    if os.path.exists(path):
        return path
    raise atsc.Error("no tsp, tstabcomp, ffmpeg on this host: install TSDuck and ffmpeg")


def put_stream(cache, ch):
    os.makedirs(cache, exist_ok=True)
    pkts = [packet(0x31, b"video")] * 40 + [packet(0x1FFF)] * 10
    with open(atsc.stream_path(cache, ATSC, ch, time.time()), "wb") as f:
        f.write(b"".join(pkts))


class Guest:
    """QEMU's end of the tuner port, as the guest's pump would answer: the frames it gets."""

    def __init__(self, sock):
        self.sock = sock
        self.reader = antenna.FrameReader()
        self.maps = []
        self.ts = 0
        self.ts_freqs = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        with contextlib.suppress(OSError):
            self._take()

    def _take(self):
        while data := self.sock.recv(65536):
            with self.lock:
                for kind, freq, payload in self.reader.feed(data):
                    if kind == antenna.MAP:
                        self.maps.append(antenna.map_channels(payload))
                    elif kind == antenna.TS:
                        self.ts += len(payload) // 188
                        self.ts_freqs.add(freq)

    def tune(self, freq, delsys=antenna.SYS_ATSC):
        self.sock.sendall(antenna.tune_frame(freq, delsys))


def until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


class FeedTest(unittest.TestCase):
    """The feeder beside QEMU (no tools, no streams) and a feed elsewhere with rf7's stream."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        for d in ("run", "state", "host-cache", "pc-cache"):
            os.makedirs(os.path.join(self.tmp, d))
        put_stream(os.path.join(self.tmp, "pc-cache"), "7")
        patch = mock.patch.object(atsc, "make", fake_make)
        patch.start()
        self.addCleanup(patch.stop)
        antenna.save_map(os.path.join(self.tmp, "state"), ["7", "14"])
        self.feeder = antenna.Tuner(
            os.path.join(self.tmp, "run"),
            os.path.join(self.tmp, "state"),
            os.path.join(self.tmp, "host-cache"),
        )
        self.feeder.log = lambda text: None
        self.feeder.player.log = self.feeder.log
        self.path = self.feeder.listen()
        self.addCleanup(self.feeder.listener.close)
        host, guest = socket.socketpair()
        self.guest = Guest(guest)
        session = threading.Thread(target=self.feeder.session, args=(host,), daemon=True)
        session.start()

        def end():
            guest.shutdown(socket.SHUT_RDWR)
            session.join(5)
            host.close()
            guest.close()

        self.addCleanup(end)

    def status(self):
        with open(os.path.join(self.tmp, "run", antenna.STATUS)) as f:
            return json.load(f)

    def test_feeder_alone_has_no_signal(self):
        until(lambda: self.guest.maps)
        self.assertEqual(self.guest.maps[0], [])  # no tools here, nothing made
        until(lambda: os.path.exists(os.path.join(self.tmp, "run", antenna.STATUS)))
        self.assertFalse(self.status()["fed"])

    def test_a_feed_brings_the_tuned_channel_and_ends_cleanly(self):
        rf7, rf14 = antenna.ATSC.tuning("7"), antenna.ATSC.tuning("14")
        until(lambda: self.guest.maps)
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(self.path)
        logs = []
        result = {}

        def feed():
            result["sent"] = antenna.feed_session(
                conn, antenna.Player(os.path.join(self.tmp, "pc-cache"), logs.append), logs.append
            )

        t = threading.Thread(target=feed, daemon=True)
        t.start()
        # the feed's map: of the wanted rf7 and rf14, the one it has a stream of
        until(lambda: self.guest.maps[-1] == [rf7])
        self.assertEqual(self.guest.ts, 0)  # nothing tuned yet
        self.guest.tune(rf7[0], band.SYS_DVBC_ANNEX_B)  # cable C7 on the same centre: no
        time.sleep(0.3)
        self.assertEqual(self.guest.ts, 0)
        self.guest.tune(*rf7)
        until(lambda: self.guest.ts >= 50)
        self.assertEqual(self.guest.ts_freqs, {rf7[0]})
        until(lambda: self.status()["fed"] and self.status()["sent_packets"] > 0)
        self.assertEqual(self.status()["map"], [list(rf7)])
        self.assertEqual(self.status()["tuned_hz"], rf7[0])
        # the map changes on the feeder's host: the feed follows
        antenna.save_map(os.path.join(self.tmp, "state"), ["14"])
        until(lambda: self.guest.maps[-1] == [])
        put_stream(os.path.join(self.tmp, "pc-cache"), "14")
        until(lambda: self.guest.maps[-1] == [rf14])
        # the feeder ends the feed (its VM stopped): the feed returns, the feeder is alone again
        self.feeder.drop_feed(self.feeder.feed)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertGreater(result["sent"], 0)
        until(lambda: not self.status()["fed"])
        conn.close()

    def test_a_new_feed_replaces_the_old(self):
        a = socket.socket(socket.AF_UNIX)
        a.connect(self.path)
        until(lambda: self.feeder.feed is not None)
        b = socket.socket(socket.AF_UNIX)
        b.connect(self.path)
        self.assertEqual(a.recv(4096)[:4], struct.pack("<I", antenna.MAGIC))  # its hello
        until(lambda: a.recv(4096) == b"")  # then the end
        hello = antenna.TuneReader()
        while hello.wanted is None:
            hello.feed(b.recv(4096))
        self.assertEqual(hello.wanted, [antenna.ATSC.tuning("7"), antenna.ATSC.tuning("14")])
        a.close()
        b.close()
        until(lambda: self.feeder.feed is None)

    def test_a_feed_with_no_feeder_fails(self):
        a, b = socket.socketpair()
        b.close()
        with self.assertRaises(antenna.Error):
            antenna.feed_session(a, antenna.Player(self.tmp, print), lambda t: None, wait=1)
        a.close()


class FeedProcessTest(unittest.TestCase):
    """feed_main, start_feed and stop_feed with a fake forward and process."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        patch = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp})
        patch.start()
        self.addCleanup(patch.stop)

    def test_feed_main_forwards_to_the_socket_and_cleans_up(self):
        forwards = []

        class Forward:
            def __init__(self, host, uri, address, to_port, record):
                forwards.append((host, uri, address, to_port, os.path.basename(record)))
                self.record = record

            def __enter__(self):
                ares.write_private(self.record, b"{}")
                return 4242

            def __exit__(self, *exc):
                forwards.append("cancelled")

        a, b = socket.socketpair()
        seen = {}

        def connect(addr):
            seen["addr"] = addr
            seen["dirs"] = [e for e in os.listdir(self.tmp) if e.startswith(antenna.FEED_PREFIX)]
            b.sendall(antenna.map_frame([]))
            threading.Timer(0.3, b.close).start()
            return a

        with mock.patch.object(ares, "Forward", Forward), \
             contextlib.redirect_stdout(io.StringIO()):  # fmt: skip
            rc = antenna.feed_main(["pi", "ssh://pi", "tv", "/l/tv/run/tuner-feed.sock", self.tmp],
                                 connect=connect)  # fmt: skip
        self.assertEqual(rc, 0)
        self.assertEqual(seen["addr"], ("127.0.0.1", 4242))
        self.assertEqual(
            forwards[0], ("pi", "ssh://pi", "/l/tv/run/tuner-feed.sock", None, ares.FORWARD_RECORD)
        )
        self.assertEqual(forwards[-1], "cancelled")
        self.assertEqual(len(seen["dirs"]), 1)
        self.assertEqual([e for e in os.listdir(self.tmp) if e.startswith(antenna.FEED_PREFIX)], [])

    def test_start_reuses_a_running_feed_and_stop_ends_it(self):
        spawned = []

        class Proc:
            pid = os.getpid()

            def poll(self):
                return None

        def spawn(argv, **kw):
            spawned.append(argv)
            d = tempfile.mkdtemp(prefix=f"{antenna.FEED_PREFIX}{os.getpid()}-", dir=self.tmp)
            info = {"host": "pi", "name": "tv", "pid": os.getpid(), "connected": True}
            with open(os.path.join(d, antenna.FEED_INFO), "w") as f:
                json.dump(info, f)
            return Proc()

        info = antenna.start_feed("pi", "ssh://pi", "tv", "/l/s.sock", self.tmp, spawn=spawn)
        self.assertEqual(info["pid"], os.getpid())
        self.assertEqual(spawned[0][-6:], ["feed", "pi", "ssh://pi", "tv", "/l/s.sock", self.tmp])
        antenna.start_feed("pi", "ssh://pi", "tv", "/l/s.sock", self.tmp, spawn=spawn)
        self.assertEqual(len(spawned), 1)  # it runs: not a second one
        self.assertIsNone(antenna.find_feed("pi", "other"))
        alive = mock.patch.object(antenna, "_alive", side_effect=[True, False])
        with mock.patch.object(antenna.os, "kill") as kill, alive:
            self.assertTrue(antenna.stop_feed("pi", "tv"))
        kill.assert_called_once_with(os.getpid(), antenna.signal.SIGTERM)

    def test_start_reports_a_feed_that_ends(self):
        class Proc:
            pid = 1 << 22
            returncode = 1

            def poll(self):
                return 1

        def spawn(argv, stdout=None, **kw):
            stdout.write(b"12:00:00 feed pi/tv: no tuner answered in 15 s\n")
            return Proc()

        with self.assertRaises(antenna.Error) as e:
            antenna.start_feed("pi", "ssh://pi", "tv", "/l/s.sock", self.tmp, spawn=spawn)
        self.assertIn("no tuner answered", str(e.exception))


class RemoteCliTest(unittest.TestCase):
    """beanjar io tuner map and unmap HOST/NAME: the host keeps the map (--fed), a feed here."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.checkout = tmp.name
        env = {
            "XDG_CONFIG_HOME": os.path.join(tmp.name, "config"),
            "BEANJAR_LIBRARY": os.path.join(tmp.name, "lib"),
            "XDG_RUNTIME_DIR": tmp.name,
        }
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.calls = []
        self.mapped = []
        self.connected = True
        for name, fake in (("transport", self.transport),):
            patch = mock.patch.object(remote, name, fake)
            patch.start()
            self.addCleanup(patch.stop)
        self.made = mock.patch.object(antenna, "make").start()
        self.start = mock.patch.object(antenna, "start_feed").start()
        self.stop = mock.patch.object(antenna, "stop_feed").start()
        self.addCleanup(mock.patch.stopall)
        remote.add_host("pi", "ssh://pi.example")

    def transport(self, argv, stdin=None, on_stderr=None, timeout=None):
        words = argv[argv.index("--") + 2].split()[1:]
        if words[:1] == ["--version"]:
            return 1, b"", "an older beanjar"  # remote.identity: no drift to report
        self.calls.append(words)
        verb = words[2]  # io tuner VERB local/NAME [RF...]
        rest = [w for w in words[4:] if not w.startswith("-")]
        if verb == "map":
            self.mapped = sorted(set(self.mapped + rest))
        elif verb == "unmap":
            self.mapped = [] if rest == ["all"] else [c for c in self.mapped if c not in rest]
        data = {
            "name": "tv",
            "tuner": True,
            "broadcast": "atsc",
            "channels": [
                {"channel": c, "center_hz": antenna.ATSC.center_hz(c), "delsys": antenna.ATSC.delsys(c),
                 "mapped": c in self.mapped, "services": []}
                for c in ("7", "14", "33", "C78")
            ],
            "tuned_hz": None,
            "sent_packets": 0,
            "connected": self.connected,
            "fed": False,
            "feed_socket": "/l/tv/run/tuner-feed.sock",
        }  # fmt: skip
        return 0, json.dumps(data).encode(), ""

    def beanjar(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--repo", self.checkout, *argv])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_map_makes_here_and_starts_the_feed(self):
        rc, out, err = self.beanjar("io", "tuner", "map", "pi/tv", "07", "14", "C78")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls[0], ["io", "tuner", "list", "local/tv", "--json"])  # its lineup
        self.assertEqual(
            self.calls[-1], ["io", "tuner", "map", "local/tv", "7", "14", "C78", "--fed", "--json"]
        )
        self.assertEqual(self.made.call_args.args[1], ["7", "14", "C78"])
        host, uri, name, address, _cache = self.start.call_args.args
        self.assertEqual((host, uri, name, address), ("pi", "ssh://pi.example", "tv",
                                                       "/l/tv/run/tuner-feed.sock"))  # fmt: skip
        self.assertIn("RF 7 ", out)
        self.assertIn("RF C78   549.00 MHz  QAM", out)

    def test_map_of_a_stopped_vm_starts_no_feed(self):
        self.connected = False
        rc, _out, err = self.beanjar("io", "tuner", "map", "pi/tv", "7")
        self.assertEqual(rc, 0, err)
        self.start.assert_not_called()
        self.assertIn("not running", err)

    def test_unmap_to_nothing_stops_the_feed(self):
        self.mapped = ["7", "14"]
        self.assertEqual(self.beanjar("io", "tuner", "unmap", "pi/tv", "7")[0], 0)
        self.stop.assert_not_called()
        self.assertEqual(self.beanjar("io", "tuner", "unmap", "pi/tv", "all", "--json")[0], 0)
        self.assertEqual(
            self.calls[-1], ["io", "tuner", "unmap", "local/tv", "all", "--fed", "--json"]
        )
        self.stop.assert_called_once_with("pi", "tv")
        self.made.assert_not_called()

    def test_list_and_bad_channels(self):
        self.assertEqual(self.beanjar("io", "tuner", "list", "pi/tv")[0], 0)
        self.assertEqual(self.calls[-1], ["io", "tuner", "list", "local/tv", "--json"])
        self.calls.clear()
        rc, _o, err = self.beanjar("io", "tuner", "map", "pi/tv", "8")
        self.assertEqual(rc, 1)
        self.assertIn("the lineup has no RF 8", err)
        self.assertEqual(self.calls, [["io", "tuner", "list", "local/tv", "--json"]])  # no map
        self.calls.clear()
        rc, _o, err = self.beanjar("io", "tuner", "map", "pi/tv", "c7")
        self.assertEqual(rc, 2)
        self.assertIn("C and a cable channel number (C78)", err)
        self.assertEqual(self.calls, [])


class FrameTest(unittest.TestCase):
    def test_reader_frames_and_wanted(self):
        data = b"junk" + antenna.map_frame([(473000000, 11), (177000000, 2)])
        data += antenna.ts_frames(1, packet(0))
        data += antenna.tune_frame(177000000, 11)
        r = antenna.TuneReader()
        kinds = []
        for i in range(0, len(data), 5):
            kinds += [k for k, _f, _p in r.feed(data[i : i + 5])]
        self.assertEqual(kinds, [antenna.MAP, antenna.TS, antenna.TUNE])
        self.assertEqual(r.wanted, [(177000000, 2), (473000000, 11)])
        self.assertEqual((r.tuned, r.delsys), (177000000, 11))

    def test_channels_of(self):
        wanted = [(473000000, 11), (177200000, 11), (999000000, 11), (549000000, 2)]
        self.assertEqual(antenna.channels_of(wanted), ["7", "14", "C78"])
        # cable C7 is on air 7's centre: the delivery system tells them apart
        self.assertEqual(antenna.channels_of([(177000000, 2)], extra=["C7"]), ["C7"])
        self.assertEqual(antenna.channels_of([(549000000, 11)]), [])


def tdt(field):
    return bytes([0x70, 0x70, 0x05]) + field


def tot(field):
    sec = bytes([0x73, 0x70, 0x0B]) + field + b"\xf0\x00"  # no descriptors, then the CRC
    return sec + atsc.crc32_mpeg(sec).to_bytes(4, "big")


class AribTest(unittest.TestCase):
    """The ISDB-T lineup of an ARIB board, with no tools."""

    def test_lineups(self):
        self.assertIs(antenna.lineup("arib"), antenna.ARIB)
        self.assertIs(antenna.lineup("atsc"), antenna.ATSC)
        self.assertIs(antenna.lineup(None), antenna.ATSC)
        self.assertIs(antenna.lineup("dvb"), antenna.DVB)
        self.assertEqual((antenna.ARIB.delsys("27"), antenna.ARIB.channels), (8, ["27"]))
        self.assertEqual(antenna.ARIB.center_hz("27"), 557142857)
        self.assertEqual(antenna.lineup_of_systems({8}), antenna.ARIB)
        self.assertEqual(antenna.lineup_of_systems({11, 2}), antenna.ATSC)
        self.assertEqual(antenna.lineup_of_systems({3}), antenna.DVB)
        self.assertEqual(antenna.lineup_of_systems(set()), antenna.ATSC)
        with self.assertRaises(antenna.Error):
            antenna.ARIB.check("28")
        antenna.check_channels(["7", "14", "C78"], antenna.ATSC)
        with self.assertRaises(antenna.Error):
            antenna.check_channels(["7"], antenna.ARIB)
        with self.assertRaises(antenna.Error):
            antenna.ARIB.check_band("C30")
        # the firmware's DTV_FREQUENCY is its table's kHz times 1000: within the tolerance
        self.assertEqual(antenna.channels_of([(557143000, 8)], antenna.ARIB), ["27"])
        self.assertEqual(antenna.channels_of([(557143000, 8)]), [])

    def test_each_channel_has_its_own_picture_and_tone(self):
        services = [s for rf in ATSC["rf"] for s in rf["services"]]
        self.assertGreater(len(services), 1)
        for key in ("picture", "sound"):
            values = [json.dumps(s[key]) for s in services]
            self.assertEqual(len(set(values)), len(values), key)
        # the stream's key follows a service's picture and tone: the lineup's
        now = time.time()
        a = atsc.stream_key(ATSC, "7", now)
        for old, new in (('"testsrc2"', '"zoneplate"'), ("tone = 440", "tone = 123")):
            text = ATSC["text"].replace(old, new, 1)
            other = lineup.parse(text, "t.toml", lineup.DEFAULTS_DIR)
            self.assertNotEqual(atsc.stream_key(other, "7", now), a, new)

    def test_map_frame_and_reader(self):
        f = antenna.map_frame([(557142857, antenna.SYS_ISDBT)])
        self.assertEqual(struct.unpack_from("<II", f, 16), (557142857, 8))
        r = antenna.TuneReader()
        r.feed(f)
        self.assertEqual(r.wanted, [(557142857, 8)])
        r.feed(antenna.map_frame([]))
        self.assertEqual(r.wanted, [])

    def test_status_follows_the_board(self):
        class VM:
            name, state_dir, run_dir = "jp", "/nonexistent", "/nonexistent"

            def target(self):
                return ("o22", "queue")

        with mock.patch.object(antenna, "vm_broadcast", return_value="arib"):
            st = antenna.status(VM())
        self.assertEqual(st["broadcast"], "arib")
        self.assertEqual([(c["channel"], c["delsys"]) for c in st["channels"]], [("27", 8)])
        self.assertEqual(st["channels"][0]["center_hz"], 557142857)
        self.assertIn("ビーンジャー１", st["channels"][0]["services"][0])
        with mock.patch.object(antenna, "vm_broadcast", return_value="atsc"):
            got = [(c["channel"], c["delsys"]) for c in antenna.status(VM())["channels"]]
            self.assertEqual(got, [("7", 11), ("14", 11), ("33", 11), ("C78", 2)])
        with mock.patch.object(antenna, "vm_broadcast", return_value="dvb"):
            got = [
                (c["channel"], c["center_hz"], c["delsys"])
                for c in antenna.status(VM())["channels"]
            ]
            self.assertEqual(got, [("21", 474000000, 3), ("C38", 346000000, 1)])

    def test_tdt_and_tot_set_to_now(self):
        old = arib.jst_time(1_700_000_000)
        p = packet(arib.TOT_PID, tdt(old) + tot(old))
        now = 1_800_000_123  # 2027-01-15 08:02:03 UTC: 17:02:03 JST
        q = arib.time_rewrite(p, now)
        self.assertEqual(len(q), 188)
        field = bytes.fromhex("EFEC") + bytes([0x17, 0x02, 0x03])  # MJD 61420, then BCD
        self.assertEqual(q[5:13], tdt(field))
        sec = q[13 : 13 + 14]
        self.assertEqual(sec[3:8], field)
        self.assertEqual(atsc.crc32_mpeg(sec), 0)  # the CRC made again
        self.assertEqual(q[27:], p[27:])
        other = packet(0x11, tdt(old))
        self.assertEqual(arib.time_rewrite(other, now), other)  # only the TOT PID

    def test_stream_rewrites_the_lineups_time(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.ts")
            old = arib.jst_time(1_700_000_000)
            pkts = [packet(arib.TOT_PID, tdt(old)), packet(0x1FFB, stt(1000))]
            with open(path, "wb") as f:
                f.write(b"".join(pkts))
            s = antenna.Stream(path, rate=188 * 8 * 100, clock=antenna.ARIB.clock)
            out = s.next(0.02, now=1_800_000_000)
            self.assertEqual(out[5:13], tdt(arib.jst_time(1_800_000_000)))
            self.assertEqual(out[188:], pkts[1])  # no STT of ISDB's

    def test_sender_sends_to_an_isdbt_tune_only(self):
        rf27 = antenna.ARIB.center_hz("27")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "isdbt27.ts")
            with open(path, "wb") as f:
                f.write(packet(0x111, b"video") * 50)
            lu = antenna.Lineup(
                "arib", 188 * 8 * 1000, antenna.ARIB.services, lambda cache, ch, now: path,
                antenna.ARIB.clock, antenna.ARIB.spec,
            )  # fmt: skip
            sent = []
            sender = antenna.Sender(antenna.Player(d, print, lu), sent.append, lambda t: None)
            sender.tick(["27"], rf27, antenna.SYS_ATSC, 0.0)
            sender.tick(["27"], rf27, antenna.SYS_ATSC, 0.1)
            frames = antenna.FrameReader().feed(b"".join(sent))
            self.assertEqual([k for k, _f, _p in frames], [antenna.MAP])
            self.assertEqual(struct.unpack_from("<II", frames[0][2]), (rf27, 8))
            sent.clear()
            sender.tick(["27"], rf27, antenna.SYS_ISDBT, 0.2)
            frames = antenna.FrameReader().feed(b"".join(sent))
            self.assertEqual({(k, f) for k, f, _p in frames}, {(antenna.TS, rf27)})

    def test_feed_takes_the_lineup_of_the_map(self):
        rf27 = antenna.ARIB.center_hz("27")
        a, b = socket.socketpair()
        players = []

        class Player(antenna.Player):
            def ready(self, channels):
                players.append((self.lineup.name, channels))
                return {}

        def feeder():
            b.sendall(antenna.map_frame([(rf27, antenna.SYS_ISDBT)]))
            until(lambda: players)
            b.close()

        threading.Thread(target=feeder, daemon=True).start()
        antenna.feed_session(a, Player("/nonexistent", print), lambda t: None, wait=5)
        a.close()
        self.assertEqual(players[0], ("arib", ["27"]))


if __name__ == "__main__":
    unittest.main()
