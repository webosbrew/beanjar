# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The antenna of a VM whose board has our tuner (targets: the dtv_soc module): which physical
channels carry a signal, and the transport stream of the one the set is tuned to.

`beanjar io tuner list NAME` lists the lineup's physical channels (Lineup: the lineup in effect,
the VM's own, `io tuner lineup set`, kept in its state's tuner-lineup/, else the built-in one of
the board's broadcast standard, builtin-lineups/atsc.toml, arib.toml or dvb.toml; lineup.py reads
them) and
those of the user's streams, which of them are mapped and what the set is tuned to; `beanjar io
tuner map NAME RF...` and `unmap NAME RF...|all` change the map, which is kept in the VM's state
(tuner.json) for its next starts. An RF is named by its channel number, or C and the number on
cable ("7", "C78": tuner_band), and each has its centre frequency and delivery system: a US air
channel 8VSB, a US cable one QAM annex B, so air 7 and cable C7, both on 177 MHz, are two RFs. A mapped channel's stream is made (Lineup.make, into the cache's
tuner/) when it is mapped; `map NAME RF --stream FILE` instead copies a transport stream of the
user's into the state's tuner-streams/ (checked by tuner_ts.inspect) and loops it as it is
(RecordedStream), only its clock tables set to now.

Two halves, which may run on two machines:

- The generator (Player, Sender) makes the streams of the wanted channels with TSDuck and ffmpeg
  and sends the map of those that are ready and, while frontend 0 is tuned to one of them, that
  channel's stream, looped at its mux rate, without its null packets and with its time tables
  (ATSC STT, ISDB and DVB TDT and TOT) set to now (include/webos-tuner.h's frames). Each RF of the
  map carries its own delivery system, and a stream goes only to a tune of that frequency and
  delivery system.
- The feeder (Tuner) runs beside QEMU: the launcher starts it (`python -m beanjar.tuner.antenna PID
  RUN_DIR STATE_DIR CACHE BROADCAST`, the board's standard) beside a VM that has the webos.tuner
  port (qemu.py). It connects to QEMU's socket for the port (RUN_DIR's tuner.sock), writes what it
  does to RUN_DIR's tuner-status.json and ends with QEMU (PID). It listens on RUN_DIR's tuner-feed.sock for a feed:
  a generator elsewhere. While one is connected it tells it the channels the VM's map wants (a
  MAP frame from the feeder's side) and the guest's tunes, and passes the feed's MAP and TS
  frames on to the guest, whole: it needs neither TSDuck nor ffmpeg. Without a feed it runs the
  generator itself, on this host's tools and cache.

A VM on another host (`beanjar io tuner map HOST/NAME RF...`) is fed from this machine: the host's
beanjar keeps the map without making streams (--fed; of an own lineup only the file, of a user's
stream only its name), this machine keeps the VM's own lineup and streams with their files
(remote_dir), and a feed process here (`python -m beanjar.tuner.antenna feed HOST URI NAME SOCKET CACHE
DIR`, started detached by start_feed) reaches its
tuner-feed.sock through a forward of the host's shared ssh connection (ares.Forward), so only the
tuned channel crosses the network, encrypted, over the connection beanjar already has. The feed
lives as long as that connection to the feeder: it ends when the VM stops (the feeder ends with
QEMU), when unmap leaves nothing mapped (stop_feed), or on SIGTERM; a VM started again is fed again
by the next map. Its record directory (FEED_PREFIX, ares.sweep_homes) names it for the next map,
unmap and the sweep that cancels the forward of one killed outright."""

import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

from .. import base
from . import band, lineup as tuner_lineup, atsc, arib, dvb, user_stream
from ..base import Error

MAGIC = 0x4E555457
MAP, TS, TUNE = 1, 2, 3
HEADER = struct.Struct("<IIII")
STATE = "tuner.json"
STATUS = "tuner-status.json"
SOCK = "tuner.sock"
FEED_SOCK = "tuner-feed.sock"  # in RUN_DIR: where a generator elsewhere connects
TICK = 0.02  # seconds between sends
TOLERANCE_HZ = 1_000_000  # dtv_soc.c's LOCK_TOLERANCE_HZ
SYS_ISDBT, SYS_ATSC = band.SYS_ISDBT, band.SYS_ATSC
CHUNK_PACKETS = 512  # include/webos-tuner.h's WEBOS_TUNER_TS_MAX
MAP_MAX = 128  # include/webos-tuner.h's WEBOS_TUNER_MAP_MAX
LIMITS = {MAP: MAP_MAX * 8, TS: CHUNK_PACKETS * atsc.PACKET, TUNE: 64}  # payload bytes
FEED_PREFIX = "beanjar-tuner-"  # a feed's record directory: beanjar-tuner-PID-RANDOM
FEED_INFO = "feed.json"
FEED_WAIT = 15.0  # seconds a feed waits for the feeder's first frame
FEED_START_WAIT = 30.0  # seconds start_feed waits for the feed to be connected


def frame(kind, freq, payload=b""):
    return HEADER.pack(MAGIC, kind, freq, len(payload)) + payload


def map_frame(channels):
    """The map: [(centre frequency, delivery system)]."""
    return frame(MAP, 0, b"".join(struct.pack("<II", f, d) for f, d in channels))


def tune_frame(freq, delsys):
    return frame(TUNE, freq, struct.pack("<I", delsys))


def map_channels(payload):
    """The (centre frequency, delivery system) pairs of a MAP frame's payload, sorted."""
    return sorted(struct.iter_unpack("<II", payload[: len(payload) // 8 * 8]))


def ts_frames(freq, packets):
    """TS frames of the packets (bytes, a multiple of 188), at most CHUNK_PACKETS each."""
    step = CHUNK_PACKETS * atsc.PACKET
    return b"".join(frame(TS, freq, packets[i : i + step]) for i in range(0, len(packets), step))


class Lineup:
    """A broadcast's physical channels with a signal, by name ("7", "C78": tuner_band): their
    centre frequencies, delivery systems and services, how a channel's stream is made into a cache
    and which of its packets carry the time, rewritten to now as they are sent (clock: {PID:
    rewrite(packet, now)}). spec: the parsed lineup file (tuner_lineup), own: a VM's own rather
    than the built-in one."""

    def __init__(self, name, rate, services, make, clock, spec, own=False):
        self.name, self.rate = name, rate
        self.channels = [rf["rf"] for rf in spec["rf"]]  # in tuner_band.sort_key order
        self.system = {rf["rf"]: rf.get("system") for rf in spec["rf"]}
        self.services = services  # channel -> ["name", ...]
        self.make = make  # (cache, channel, now) -> the stream's path
        self.clock = clock
        self.spec, self.own = spec, own
        self.systems = SYSTEMS[name]

    def center_hz(self, ch):
        """The centre frequency of RF ch of the band (any of it, not only the lineup's); Error for
        an RF the band does not have."""
        try:
            return band.center_hz(self.name, ch)
        except band.Error as e:
            raise Error(str(e)) from None

    def delsys(self, ch):
        return band.delsys(self.name, ch, self.system.get(ch))

    def tuning(self, ch):
        """(centre frequency, delivery system) of RF ch."""
        return self.center_hz(ch), self.delsys(ch)

    def check(self, ch):
        if ch not in self.channels:
            raise Error(f"the lineup has no RF {ch} ({', '.join(self.channels)})")

    def check_band(self, ch):
        """Error for an RF the band does not have."""
        self.center_hz(ch)


# the delivery systems a board of each standard tunes (tuner_band)
SYSTEMS = {
    "atsc": {band.SYS_ATSC, band.SYS_DVBC_ANNEX_B},
    "arib": {band.SYS_ISDBT},
    "dvb": {band.SYS_DVBT, band.SYS_DVBT2, band.SYS_DVBC_ANNEX_A},
}


def fed_make(cache, ch, now):
    raise atsc.Error(
        "the lineup's files are on the machine that feeds this VM: map the channel from there "
        "(beanjar io tuner map HOST/NAME RF...)"
    )


def _services(spec, label):
    def of(ch):
        return [label(rf, s) for rf in spec["rf"] if rf["rf"] == ch for s in rf["services"]]

    return of


def from_spec(spec, own=False, fed=False):
    """The Lineup of a parsed lineup file (tuner_lineup.load); fed: its files are with a feed on
    another machine, which makes its streams. The generators are looked up when called, so a test
    can stand in for them."""
    b = spec["broadcast"]
    if b == "arib":
        return Lineup(
            "arib",
            arib.MUX_RATE,
            _services(spec, lambda rf, s: f"{rf['remote_key']} {s['name']}"),
            fed_make if fed else lambda cache, ch, now: arib.make_cached(cache, spec, ch, now),
            {arib.TOT_PID: lambda p, now: arib.time_rewrite(p, now)},
            spec,
            own,
        )
    if b == "dvb":
        return Lineup(
            "dvb",
            dvb.MUX_RATE,
            _services(spec, lambda rf, s: f"{s['lcn']} {s['name']}"),
            fed_make if fed else lambda cache, ch, now: dvb.make_cached(cache, spec, ch, now),
            {dvb.TOT_PID: lambda p, now: dvb.time_rewrite(p, now)},
            spec,
            own,
        )
    return Lineup(
        "atsc",
        atsc.MUX_RATE,
        _services(spec, lambda rf, s: f"{s['major']}.{s['minor']} {s['name']}"),
        fed_make if fed else lambda cache, ch, now: atsc.make(cache, spec, ch, now),
        {atsc.BASE_PID: lambda p, now: atsc.stt_rewrite(p, now)},
        spec,
        own,
    )


# the built-in lineups by broadcast standard (targets.BROADCASTS): builtin-lineups/atsc.toml, arib.toml
# and dvb.toml
ATSC = from_spec(tuner_lineup.default("atsc"))
ARIB = from_spec(tuner_lineup.default("arib"))
DVB = from_spec(tuner_lineup.default("dvb"))
LINEUPS = {"atsc": ATSC, "arib": ARIB, "dvb": DVB}


def lineup(broadcast):
    """The built-in lineup of the broadcast standard (None: the board names none, the US one)."""
    return LINEUPS.get(broadcast, ATSC)


def lineup_of_systems(systems):
    """The built-in lineup whose board tunes the delivery systems, else the US one."""
    return next((x for x in LINEUPS.values() if systems and systems <= x.systems), ATSC)


# ---- a VM's own lineup and streams, in its state (or, for a VM on another host, in the
# directory of this machine that feeds it: remote_dir) ----

LINEUP_DIR = "tuner-lineup"  # the own lineup: lineup.toml and the files it names
FED_MARK = "fed"  # in LINEUP_DIR: its files are with a feed on another machine
STREAMS_DIR = "tuner-streams"  # the user's streams: rfNN.ts


def remote_dir(root, host, name):
    """Where this machine keeps the own lineup and streams of VM name on host, which it feeds."""
    return os.path.join(root, "remote", host, name)


_own = {}  # lineup file: ((size, mtime), Lineup)


def own_lineup(state_dir, broadcast=None):
    """The VM's own lineup (Lineup), None if it has none; Error if it is not one, or not of the
    broadcast standard (when given)."""
    d = os.path.join(state_dir, LINEUP_DIR)
    path = os.path.join(d, tuner_lineup.FILE)
    try:
        st = os.stat(path)
    except OSError:
        return None
    fed = os.path.exists(os.path.join(d, FED_MARK))
    memo = (st.st_size, st.st_mtime_ns, fed)
    hit = _own.get(path)
    if hit is None or hit[0] != memo:
        hit = (memo, from_spec(tuner_lineup.load(path, check_files=not fed), True, fed))
        _own[path] = hit
    lu = hit[1]
    if broadcast is not None and lu.name != broadcast:
        raise Error(
            f"the VM's own lineup is {lu.name} and its board's broadcast standard is {broadcast}: "
            "beanjar io tuner lineup reset NAME, or set one of its standard"
        )
    return lu


def vm_lineup(state_dir, broadcast=None):
    """The lineup in effect: the VM's own if it has one of its board's standard, else the built-in
    one of the standard."""
    base = lineup(broadcast)
    try:
        own = own_lineup(state_dir, base.name)
    except Error:
        own = None
    return own or base


def lineup_for_systems(state_dir, systems):
    """A feed's lineup for the delivery systems of its feeder's map: the own one in state_dir (None:
    none) if its board tunes them, else the built-in one."""
    try:
        own = own_lineup(state_dir) if state_dir else None
    except Error:
        own = None
    if own is not None and systems and systems <= own.systems:
        return own
    return lineup_of_systems(systems)


def streams(state_dir):
    """{channel: record} of the user's streams in the VM's state (tuner_ts.inspect's record, with
    "path"; a fed one, played by a feed on another machine, has "fed" and no file here)."""
    try:
        with open(os.path.join(state_dir, STATE)) as f:
            got = json.load(f).get("streams", {})
    except (OSError, ValueError):
        return {}
    out = {}
    for ch, rec in got.items():
        rec = dict(rec)
        if not rec.get("fed"):
            rec["path"] = os.path.join(state_dir, STREAMS_DIR, rec["file"])
        out[ch] = rec
    return out


class FrameReader:
    """Takes a stream of frames apart: feed(bytes) gives the whole frames in them so far, (kind,
    freq_hz, payload); bytes that do not start a frame of a known kind and size are skipped."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= HEADER.size:
            magic, kind, freq, length = HEADER.unpack_from(self.buf)
            if magic != MAGIC or length > LIMITS.get(kind, -1):
                del self.buf[:1]
                continue
            if len(self.buf) < HEADER.size + length:
                break
            out.append((kind, freq, bytes(self.buf[HEADER.size : HEADER.size + length])))
            del self.buf[: HEADER.size + length]
        return out


class TuneReader:
    """What a generator hears: feed() bytes (the frames in them), tuned the last TUNE's frequency
    and delsys its delivery system, wanted the last MAP's (frequency, delivery system) pairs (a
    feeder's: the channels its VM's map wants)."""

    def __init__(self):
        self.frames = FrameReader()
        self.tuned = None  # None: not heard yet; 0: tuned to nothing
        self.delsys = 0
        self.wanted = None  # None: no MAP heard

    def feed(self, data):
        frames = self.frames.feed(data)
        for kind, freq, payload in frames:
            if kind == TUNE:
                self.tuned = freq
                self.delsys = struct.unpack_from("<I", payload)[0] if len(payload) >= 4 else 0
            elif kind == MAP:
                self.wanted = map_channels(payload)
        return frames


class Stream:
    """A made stream looped at its mux rate: next(seconds) the packets (nulls dropped) that the
    time takes, with the time tables' time made now's (clock: {PID: rewrite(packet, now)}, the
    lineup's: the ATSC STT, the ISDB TDT and TOT), so the set's clock follows the host's instead of
    repeating with the loop."""

    def __init__(self, path, rate=atsc.MUX_RATE, clock=None):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        self.count = len(self.data) // atsc.PACKET
        self.rate = rate
        self.clock = ATSC.clock if clock is None else clock
        self.at = 0
        self.owed = 0.0

    def next(self, seconds, now=None):
        self.owed += seconds * self.rate / 8 / atsc.PACKET
        n = int(self.owed)
        self.owed -= n
        out = []
        for _ in range(n):
            p = self.data[self.at * atsc.PACKET : (self.at + 1) * atsc.PACKET]
            self.at = (self.at + 1) % self.count
            pid = ((p[1] & 0x1F) << 8) | p[2]
            if pid == atsc.NULL_PID:
                continue
            rewrite = self.clock.get(pid)
            if rewrite is not None:
                p = rewrite(p, now if now is not None else time.time())
            out.append(p)
        return b"".join(out)


class RecordedStream(Stream):
    """A user's stream (tuner_ts.inspect's record rec) looped at its own rate, mapped rather than
    read, so a long recording costs no memory. So that the set keeps its picture over the wrap:
    each PID plays from its first PES or section start to before its last (rec's cuts: no part of
    one is cut off), its continuity counters run on over the wrap, and after each wrap the first
    PCR of each PCR PID has the discontinuity indicator set (its time base starts again)."""

    def __init__(self, path, rec, clock=None):
        import mmap

        self.path = path
        with open(path, "rb") as f:
            self.data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        self.count = len(self.data) // atsc.PACKET
        self.rate = rec["rate"]
        self.clock = ATSC.clock if clock is None else clock
        self.cuts = {int(pid): cut for pid, cut in rec.get("cuts", {}).items()}
        self.pcr_pids = set(rec.get("pcr_pids", ()))
        self.at = 0
        self.owed = 0.0
        self.cc = {}  # PID: the next continuity counter
        self.discontinuity = set()  # PCR PIDs whose next PCR follows a wrap
        self.loops = 0

    def next(self, seconds, now=None):
        self.owed += seconds * self.rate / 8 / atsc.PACKET
        n = int(self.owed)
        self.owed -= n
        out = []
        size = atsc.PACKET
        for _ in range(n):
            i = self.at
            p = self.data[i * size : (i + 1) * size]
            self.at = (i + 1) % self.count
            if self.at == 0:
                self.loops += 1
                self.discontinuity = set(self.pcr_pids)
            pid = ((p[1] & 0x1F) << 8) | p[2]
            if pid == atsc.NULL_PID:
                continue
            cut = self.cuts.get(pid)
            if cut is not None and not cut[0] <= i < cut[1]:
                continue
            b = bytearray(p)
            cc = self.cc.get(pid, b[3] & 0xF)
            if b[3] & 0x10:  # a payload: the counter moves on
                b[3] = (b[3] & 0xF0) | cc
                self.cc[pid] = (cc + 1) & 0xF
            else:
                b[3] = (b[3] & 0xF0) | ((cc - 1) & 0xF)
            if pid in self.discontinuity and b[3] & 0x20 and b[4] and b[5] & 0x10:
                b[5] |= 0x80  # discontinuity_indicator, on the PCR's packet
                self.discontinuity.discard(pid)
            p = bytes(b)
            rewrite = self.clock.get(pid)
            if rewrite is not None:
                p = rewrite(p, now if now is not None else time.time())
            out.append(p)
        return b"".join(out)


# ---- the map, kept in the VM's state ----


def mapped(state_dir):
    """The physical channels mapped for the VM ("7", "C78"), air before cable, by number."""
    try:
        with open(os.path.join(state_dir, STATE)) as f:
            return sorted(set(json.load(f).get("channels", [])), key=band.sort_key)
    except (OSError, ValueError):
        return []


def save_map(state_dir, channels, streams=None):
    """Keeps the map: the mapped channels and (unless None: as they are) the user's streams,
    {channel: record}."""
    os.makedirs(state_dir, exist_ok=True)
    try:
        with open(os.path.join(state_dir, STATE)) as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    data = {"channels": sorted(set(channels), key=band.sort_key)}
    if streams is None:
        kept = old.get("streams") or {}
    else:
        kept = {ch: {k: v for k, v in rec.items() if k != "path"} for ch, rec in streams.items()}
    if kept:
        data["streams"] = kept
    tmp = os.path.join(state_dir, STATE + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.write("\n")
    os.replace(tmp, os.path.join(state_dir, STATE))


def channels_of(wanted, lu=ATSC, extra=()):
    """The physical channels, of the lineup and extra (the user's streams' channels), among wanted
    [(frequency, delivery system)] (within TOLERANCE_HZ), sorted; pairs of none are left out."""
    out = []
    for ch in sorted(set(lu.channels) | set(extra), key=band.sort_key):
        f, d = lu.tuning(ch)
        if any(abs(f - wf) <= TOLERANCE_HZ and d == wd for wf, wd in wanted):
            out.append(ch)
    return out


def cache_dir(tree):
    return tree.cache("tuner")


def prune_cache(tree, library):
    """Keeps the stream cache to what a lineup in use refers to: the streams and loops of the
    built-in lineups and of the own lineup of any VM of the library (or its mirror of a VM on
    another host) stay; those of a lineup no one has any more (an edited or reset one) are removed,
    and made again if a lineup of that content comes back. The names removed."""
    dirs = [vm.state_dir for vm in library.vms()]
    remote = os.path.join(library.root, "remote")
    try:
        dirs += [
            os.path.join(remote, h, n)
            for h in sorted(os.listdir(remote))
            for n in sorted(os.listdir(os.path.join(remote, h)))
        ]
    except OSError:
        pass
    keep = {lu.spec["key"][:8] for lu in LINEUPS.values()}
    for d in dirs:
        try:
            own = own_lineup(d)
        except (Error, OSError, tuner_lineup.Error):
            own = None
        if own is not None:
            keep.add(own.spec["key"][:8])
    return atsc.prune(cache_dir(tree), keep)


def has_tuner(target):
    """Whether the target's board has our tuner: dtv_soc among its modules."""
    from .. import targets

    return any(m.name == "dtv_soc" for m in targets.soc(target).modules)


def vm_broadcast(vm):
    """The broadcast standard of the VM's board (targets.board_broadcast), None if it names none or
    cannot be worked out."""
    from .. import targets

    try:
        return targets.board_broadcast(
            vm.target(), vm.firmware_dir(), vm.config.get("broadcast"), vm.config.get("model")
        )
    except (Error, targets.Error, OSError):
        return None


def stream_services(rec):
    return [f"program {p}" for p in rec.get("programs", [])]


def status(vm):
    """{"name", "tuner", "broadcast", "lineup", "channels": [{"channel", "center_hz", "delsys",
    "mapped", "services", "stream"}], "tuned_hz", "tuned_delsys", "sent_packets", "connected", "fed",
    "feed_socket"} of the VM: the lineup in effect (lineup: "own" or "built-in") on its board's
    broadcast, and the channels of the user's streams (stream: the file's name, else None); fed
    while a generator elsewhere feeds it, feed_socket where one connects."""
    st = {}
    try:
        with open(os.path.join(vm.run_dir, STATUS)) as f:
            st = json.load(f)
    except (OSError, ValueError):
        pass
    on = mapped(vm.state_dir)
    lu = vm_lineup(vm.state_dir, vm_broadcast(vm))
    recorded = streams(vm.state_dir)
    return {
        "name": vm.name,
        "tuner": has_tuner(vm.target()),
        "broadcast": lu.name,
        "lineup": "own" if lu.own else "built-in",
        "channels": [
            {
                "channel": ch,
                "center_hz": lu.center_hz(ch),
                "delsys": lu.delsys(ch),
                "mapped": ch in on,
                "services": stream_services(recorded[ch]) if ch in recorded else lu.services(ch),
                "stream": recorded[ch]["name"] if ch in recorded else None,
            }
            for ch in sorted(set(lu.channels) | set(recorded), key=band.sort_key)
        ],
        "tuned_hz": st.get("tuned_hz"),
        "tuned_delsys": st.get("tuned_delsys"),
        "sent_packets": st.get("sent_packets", 0),
        "connected": st.get("connected", False),
        "fed": st.get("fed", False),
        "feed_socket": os.path.join(vm.run_dir, FEED_SOCK),
    }


def check_channels(channels, lu=ATSC, recorded=()):
    for ch in channels:
        if ch not in recorded:
            lu.check(ch)


def make(cache, channels, hint="", lu=ATSC):
    """Makes the streams of channels of the lineup into cache; Error, with hint, if not."""
    now = time.time()
    for ch in channels:
        try:
            lu.make(cache, ch, now)
        except (atsc.Error, Error, OSError) as e:
            raise Error(f"tuner: {e}{hint}") from None


MAKE_HINT = "; or map them from a machine that has the tools: beanjar io tuner map HOST/NAME RF..."


def inspect_stream(path, lu, ch):
    """tuner_ts.inspect of a user's stream for the lineup's standard and RF ch."""
    lu.check_band(ch)
    return user_stream.inspect(path, lu.name, lu.center_hz(ch), ch)


def put_stream(state_dir, ch, path, rec):
    """Copies the stream at path into state_dir as RF ch's ("7", "C78"); the record, with its
    file."""
    d = os.path.join(state_dir, STREAMS_DIR)
    os.makedirs(d, exist_ok=True)
    name = f"rf{ch.zfill(2)}.ts"
    tmp = os.path.join(d, name + ".tmp")
    shutil.copyfile(path, tmp)
    os.replace(tmp, os.path.join(d, name))
    return {k: v for k, v in rec.items() if k != "warnings"} | {"file": name}


def drop_streams(state_dir, recorded, channels):
    """Removes the files of the user's streams of channels; recorded without them."""
    for ch in channels:
        rec = recorded.pop(ch, None)
        if rec and rec.get("file"):
            try:
                os.unlink(os.path.join(state_dir, STREAMS_DIR, rec["file"]))
            except FileNotFoundError:
                pass
    return recorded


def change(vm, tree, add=(), remove=(), clear=False, fed=False, stream=None):
    """Maps the physical channels add (making their streams, unless fed: a feed from another
    machine makes them) and unmaps remove (or all with clear); the VM's status after, with
    "warnings". stream: add's one channel plays this file instead (copied into the VM's state; with
    fed, the file's name: a feed on another machine plays it); a channel mapped without one goes
    back to the lineup's stream. Unmapping a stream's channel removes its file."""
    if not has_tuner(vm.target()):
        raise Error(f"VM {vm.name}: its board has no tuner of ours (no dtv_soc)")
    lu = vm_lineup(vm.state_dir, vm_broadcast(vm))
    recorded = streams(vm.state_dir)
    warnings = []
    if stream is not None:
        if len(add) != 1:
            raise Error("io tuner map --stream: one physical channel, the stream's")
        (ch,) = add
        if fed:
            lu.check_band(ch)
            rec = {"name": os.path.basename(stream), "fed": True}
        else:
            rec = inspect_stream(stream, lu, ch)
            warnings = rec["warnings"]
            rec = put_stream(vm.state_dir, ch, stream, rec)
        drop_streams(vm.state_dir, recorded, [ch])
        recorded[ch] = rec
    else:
        check_channels(add, lu)
        check_channels(remove, lu, recorded)
        if not fed:
            make(cache_dir(tree), add, MAKE_HINT, lu)
        recorded = drop_streams(vm.state_dir, recorded, list(add))
    channels = [] if clear else mapped(vm.state_dir)
    gone = list(recorded) if clear else [c for c in remove if c in recorded]
    recorded = drop_streams(vm.state_dir, recorded, gone)
    channels = [c for c in channels if c not in remove] + list(add)
    save_map(vm.state_dir, channels, recorded)
    return status(vm) | {"warnings": warnings}


def prune(vm, lu):
    """Unmaps the channels that neither lu nor a user's stream has; the ones unmapped."""
    recorded = streams(vm.state_dir)
    on = mapped(vm.state_dir)
    keep = [c for c in on if c in lu.channels or c in recorded]
    if keep != on:
        save_map(vm.state_dir, keep)
    return [c for c in on if c not in keep]


def lineup_show(vm):
    """{"name", "broadcast", "lineup": "own"|"built-in", "file", "text", "values"} of the lineup
    in effect."""
    lu = vm_lineup(vm.state_dir, vm_broadcast(vm))
    spec = lu.spec
    return {
        "name": vm.name,
        "broadcast": lu.name,
        "lineup": "own" if lu.own else "built-in",
        "file": spec["source"],
        "text": spec["text"],
        "values": tuner_lineup.public(spec),
    }


def lineup_set(vm, tree, path, text=None, fed=False):
    """Checks the lineup file at path (or TOML text) against the VM's board and makes the streams of
    its mapped channels (unless fed), then copies it and the files it names into the VM's state, for
    its next starts; the lineup shown after, with "unmapped": the channels it lacks, unmapped.
    fed: only the file; a feed on another machine has its files and makes its streams."""
    if not has_tuner(vm.target()):
        raise Error(f"VM {vm.name}: its board has no tuner of ours (no dtv_soc)")
    board = vm_broadcast(vm)
    broadcast = lineup(board).name
    dest = os.path.join(vm.state_dir, LINEUP_DIR)
    if fed:
        spec = tuner_lineup.parse(text, path, os.getcwd(), broadcast, check_files=False)
        os.makedirs(vm.state_dir, exist_ok=True)
        tmp = dest + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        with open(os.path.join(tmp, tuner_lineup.FILE), "w", encoding="utf-8") as f:
            f.write(spec["text"])
        with open(os.path.join(tmp, FED_MARK), "w") as f:
            f.write("")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
    else:
        spec = (
            tuner_lineup.load(path, broadcast)
            if text is None
            else tuner_lineup.parse(text, path, os.getcwd(), broadcast)
        )
        recorded = streams(vm.state_dir)
        new = from_spec(spec)
        wanted = [c for c in mapped(vm.state_dir) if c in new.channels and c not in recorded]
        make(cache_dir(tree), wanted, MAKE_HINT, new)
        os.makedirs(vm.state_dir, exist_ok=True)
        tuner_lineup.install(path, dest, broadcast, text)
    unmapped = prune(vm, vm_lineup(vm.state_dir, board))
    prune_cache(tree, vm.library)
    return lineup_show(vm) | {"unmapped": unmapped}


def lineup_reset(vm, tree):
    """Back to the built-in lineup: the own one removed (and its streams from the cache, if no other
    VM has that lineup); the lineup shown after, with "unmapped"."""
    shutil.rmtree(os.path.join(vm.state_dir, LINEUP_DIR), ignore_errors=True)
    lu = vm_lineup(vm.state_dir, vm_broadcast(vm))
    unmapped = prune(vm, lu)
    prune_cache(tree, vm.library)
    return lineup_show(vm) | {"unmapped": unmapped}


# ---- the generator: needs TSDuck and ffmpeg, or the streams already in its cache ----


class Player:
    """The streams of the wanted channels: a user's stream (streams() gives them: {channel:
    record}) looped as it is, else the lineup's, made into cache when it is not there (a new EIT
    block, a cache cleared, a changed lineup) and looped; a channel whose stream cannot be had is
    left out, its reason logged once."""

    def __init__(self, cache, log, lu=ATSC, streams=None):
        self.cache, self.log, self.lineup = cache, log, lu
        self.streams_of = streams or dict
        self.streams = {}  # (centre Hz, delivery system) -> Stream
        self.failed = {}  # channel -> the reason last logged

    def ready(self, channels):
        """{(centre Hz, delivery system): Stream} of channels whose streams are there."""
        out = {}
        now = time.time()
        lu = self.lineup
        recorded = self.streams_of()
        for ch in channels:
            rec = recorded.get(ch)
            try:
                f = lu.tuning(ch)
                if rec is not None and rec.get("fed"):
                    raise Error(f"{rec['name']} is played by the machine that feeds this VM")
                path = rec["path"] if rec is not None else lu.make(self.cache, ch, now)
                old = self.streams.get(f)
                if (
                    old is not None
                    and old.path == path
                    and isinstance(old, RecordedStream) == (rec is not None)
                ):
                    s = old
                elif rec is not None:
                    s = RecordedStream(path, rec, lu.clock)
                else:
                    s = Stream(path, lu.rate, lu.clock)
            except (atsc.Error, Error, OSError, ValueError) as e:
                if self.failed.get(ch) != str(e):
                    self.log(f"rf{ch}: {e}")
                self.failed[ch] = str(e)
                continue
            self.failed.pop(ch, None)
            out[f] = s
        self.streams = out
        return out


class Sender:
    """The generator's half of a connection: tick() sends, through send, the map of the wanted
    channels whose streams are ready (each second, when it changed), each with its delivery system,
    and the tuned one's packets since the last tick while the guest tunes its frequency and
    delivery system; counted(n) is told the packets sent."""

    def __init__(self, player, send, log, counted=None):
        self.player, self.send, self.log = player, send, log
        self.counted = counted or (lambda n: None)
        self.map_sent = None
        self.last = None
        self.last_check = None

    def tick(self, wanted, tuned, delsys, now):
        """wanted: the physical channels; tuned, delsys: the guest's tune; now: monotonic."""
        if self.last_check is None or now - self.last_check >= 1.0:
            self.last_check = now
            freqs = sorted(self.player.ready(wanted))
            if freqs != self.map_sent:
                self.send(map_frame(freqs))
                self.map_sent = freqs
                self.log(f"map: {freqs}")
        s = find(self.player.streams, tuned, delsys)
        if s is not None and self.last is not None:
            packets = s.next(min(now - self.last, 0.2))
            if packets:
                self.send(ts_frames(tuned, packets))
                self.counted(len(packets) // atsc.PACKET)
        self.last = now


# ---- the feeder beside QEMU ----


class Tuner:
    def __init__(self, run_dir, state_dir, cache, broadcast=None):
        self.run_dir, self.state_dir, self.cache = run_dir, state_dir, cache
        self.broadcast = broadcast
        self.lineup = vm_lineup(state_dir, broadcast)
        self.player = Player(cache, self.log, self.lineup, lambda: streams(self.state_dir))
        self.reader = TuneReader()
        self.sent = 0
        self.connected = False
        self.qemu = None  # QEMU's socket while connected
        self.qemu_lock = threading.Lock()  # one whole frame at a time to QEMU
        self.feed = None  # the feed's socket while one is connected
        self.feed_lock = threading.Lock()  # one whole frame at a time to the feed
        self.feed_map = None  # the feed's last MAP frame
        self.feed_wanted = None  # the frequencies last told the feed
        self.lock = threading.Lock()
        self.listener = None

    def log(self, text):
        print(f"{time.strftime('%H:%M:%S')} tuner: {text}", flush=True)

    def count(self, n):
        with self.lock:
            self.sent += n

    def refresh(self):
        """The lineup in effect again: the VM's own may have been set or reset."""
        lu = vm_lineup(self.state_dir, self.broadcast)
        if lu is not self.lineup:
            self.log(f"{'own' if lu.own else 'built-in'} {lu.name} lineup")
            self.lineup = self.player.lineup = lu

    def wanted(self):
        """[(frequency, delivery system)] of the mapped channels."""
        recorded = streams(self.state_dir)
        out = []
        for ch in mapped(self.state_dir):
            if ch in self.lineup.channels or ch in recorded:
                try:
                    out.append(self.lineup.tuning(ch))
                except Error:
                    continue  # an RF of another band: a lineup of another standard was mapped
        return out

    def to_qemu(self, data):
        with self.qemu_lock:
            if self.qemu is not None:
                self.qemu.sendall(data)

    def to_feed(self, feed, data):
        """Sends data to the feed; a feed that cannot take it is dropped."""
        try:
            with self.feed_lock:
                feed.sendall(data)
        except OSError:
            self.drop_feed(feed)

    def write_status(self):
        feed = self.feed
        tmp = os.path.join(self.run_dir, STATUS + ".tmp")
        with open(tmp, "w") as f:
            json.dump(
                {
                    "connected": self.connected,
                    "fed": feed is not None,
                    "map": map_channels(self.feed_map[HEADER.size :])
                    if feed is not None and self.feed_map
                    else sorted(self.player.streams),
                    "tuned_hz": self.reader.tuned,
                    "tuned_delsys": self.reader.delsys,
                    "sent_packets": self.sent,
                },
                f,
            )
        os.replace(tmp, os.path.join(self.run_dir, STATUS))

    # -- a feed: a generator elsewhere --

    def listen(self, path=None):
        """Listens for feeds on RUN_DIR's tuner-feed.sock (or path), in a thread."""
        path = path or os.path.join(self.run_dir, FEED_SOCK)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        os.chmod(path, 0o600)
        self.listener.listen(1)
        threading.Thread(target=self._accept, daemon=True).start()
        return path

    def _accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.attach_feed(conn)

    def attach_feed(self, conn):
        """A new feed, which replaces any other: told what the map wants and the last tune; its
        frames go on to the guest."""
        with self.lock:
            old, self.feed = self.feed, conn
            self.feed_map = None
            self.feed_wanted = self.wanted()
            tuned, delsys = self.reader.tuned, self.reader.delsys
        if old is not None:
            self.drop_feed(old)
        self.log("feed connected")
        hello = map_frame(self.feed_wanted)
        if tuned is not None:
            hello += tune_frame(tuned, delsys)
        threading.Thread(target=self._from_feed, args=(conn,), daemon=True).start()
        self.to_feed(conn, hello)

    def drop_feed(self, feed):
        with self.lock:
            mine = self.feed is feed
            if mine:
                self.feed = None
                self.feed_map = None
        try:
            feed.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        feed.close()
        if mine:
            self.log("feed ended")

    def _from_feed(self, feed):
        """The feed's MAP and TS frames, on to the guest whole, until it ends."""
        reader = FrameReader()
        try:
            while data := feed.recv(65536):
                for kind, freq, payload in reader.feed(data):
                    if self.feed is not feed or kind == TUNE:
                        continue
                    f = frame(kind, freq, payload)
                    if kind == MAP:
                        self.feed_map = f
                    else:
                        self.count(len(payload) // atsc.PACKET)
                    try:
                        self.to_qemu(f)
                    except OSError:
                        pass  # QEMU's connection ended: the session sees it
        except OSError:
            pass
        self.drop_feed(feed)

    # -- QEMU --

    def session(self, sock):
        """One connection to QEMU's socket, until it ends."""
        self.reader = TuneReader()
        stop = threading.Event()
        with self.qemu_lock:
            self.qemu = sock
        self.connected = True
        if self.feed is not None and self.feed_map:
            self.to_qemu(self.feed_map)  # a feed outlives a connection to QEMU: its map again

        def receive():
            while not stop.is_set():
                try:
                    data = sock.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                with self.lock:
                    before = self.reader.tuned
                    frames = self.reader.feed(data)
                    feed = self.feed
                if self.reader.tuned != before:
                    self.log(
                        f"tuned to {self.reader.tuned} Hz (delivery system {self.reader.delsys})"
                    )
                if feed is not None:
                    tunes = b"".join(frame(k, f, p) for k, f, p in frames if k == TUNE)
                    if tunes:
                        self.to_feed(feed, tunes)
            stop.set()

        t = threading.Thread(target=receive, daemon=True)
        t.start()
        local = None  # this host's own generator, while no feed is connected
        fed = None  # the feed this session last saw
        last_check = 0.0
        try:
            while not stop.is_set():
                now = time.monotonic()
                feed = self.feed
                if now - last_check >= 1.0:
                    self.refresh()
                if feed is not None:
                    local = None
                    fed = feed
                    if now - last_check >= 1.0:
                        last_check = now
                        wanted = self.wanted()
                        if wanted != self.feed_wanted:
                            self.feed_wanted = wanted
                            self.to_feed(feed, map_frame(wanted))
                        self.write_status()
                else:
                    if fed is not None or local is None:
                        fed = None
                        local = Sender(self.player, self.to_qemu, self.log, self.count)
                    with self.lock:
                        tuned, delsys = self.reader.tuned, self.reader.delsys
                    local.tick(mapped(self.state_dir), tuned, delsys, now)
                    if now - last_check >= 1.0:
                        last_check = now
                        self.write_status()
                time.sleep(TICK)
        except OSError as e:
            self.log(f"connection: {e}")
        finally:
            stop.set()
            with self.qemu_lock:
                self.qemu = None
            self.connected = False
            self.write_status()


def find(streams, tuned, delsys):
    """The stream of the mapped channel of delivery system delsys whose centre frequency is within
    TOLERANCE_HZ of tuned, or None."""
    for (f, d), s in streams.items():
        if tuned and d == delsys and abs(f - tuned) <= TOLERANCE_HZ:
            return s
    return None


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def main(argv):
    if argv and argv[0] == "feed":
        return feed_main(argv[1:])
    pid, run_dir, state_dir, cache = int(argv[0]), argv[1], argv[2], argv[3]
    tuner = Tuner(run_dir, state_dir, cache, argv[4] if len(argv) > 4 and argv[4] else None)
    path = os.path.join(run_dir, SOCK)
    try:
        feeds_at = tuner.listen()
    except OSError as e:
        feeds_at = f"none ({e})"  # this host's own streams alone
    tuner.log(f"QEMU {pid}, {path}; {tuner.lineup.name} lineup; feeds on {feeds_at}")
    try:
        while _alive(pid):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(path)
            except OSError:
                sock.close()
                time.sleep(0.5)
                continue
            tuner.log("connected")
            tuner.session(sock)
            sock.close()
            time.sleep(0.5)
    finally:
        if tuner.listener is not None:
            tuner.listener.close()
        try:
            os.unlink(os.path.join(run_dir, FEED_SOCK))
        except OSError:
            pass
    tuner.log("QEMU ended")
    return 0


# ---- a feed: the generator for a VM on another host ----


def feed_session(sock, player, log, on_status=None, wait=FEED_WAIT, content=None):
    """Serves the feeder at the other end of sock (the channels its MAP frames want, of the lineup
    of their delivery systems: the VM's own in content, this machine's directory for it, if its
    board tunes them, else the built-in one; the tune its TUNE frames pass on) until it ends;
    Error if it says nothing within wait seconds (no feeder there). on_status(dict) is told what it
    does each second."""
    reader = TuneReader()
    heard = threading.Event()
    stop = threading.Event()
    lock = threading.Lock()
    sent = [0]

    def receive():
        while True:
            try:
                data = sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            with lock:
                before = reader.tuned
                reader.feed(data)
                if reader.wanted is not None:
                    heard.set()
            if reader.tuned != before:
                log(f"tuned to {reader.tuned} Hz (delivery system {reader.delsys})")
        stop.set()

    def counted(n):
        sent[0] += n

    threading.Thread(target=receive, daemon=True).start()
    sender = Sender(player, sock.sendall, log, counted)
    deadline = time.monotonic() + wait
    last_status = 0.0
    try:
        while not stop.is_set():
            now = time.monotonic()
            if not heard.is_set():
                if now > deadline:
                    raise Error(f"no tuner answered in {wait:g} s")
                time.sleep(TICK)
                continue
            with lock:
                wanted, tuned, delsys = reader.wanted, reader.tuned, reader.delsys
                if wanted:
                    player.lineup = lineup_for_systems(content, {d for _f, d in wanted})
            sender.tick(channels_of(wanted, player.lineup, player.streams_of()), tuned, delsys, now)
            if on_status and now - last_status >= 1.0:
                last_status = now
                on_status(
                    {
                        "connected": True,
                        "map": sender.map_sent,
                        "tuned_hz": tuned,
                        "sent_packets": sent[0],
                    }
                )
            time.sleep(TICK)
    except OSError as e:
        log(f"connection: {e}")
    if not heard.is_set():
        raise Error(
            "the tuner closed the connection: the VM is not running, or its beanjar has no feeds (update it)"
        )
    return sent[0]


def feeds_parent():
    from .. import ares

    return ares.home_parent() or tempfile.gettempdir()


def feeds(parent=None):
    """The feeds running on this machine: [{"host", "name", "pid", "connected", ..., "dir"}]."""
    parent = parent or feeds_parent()
    out = []
    for entry in sorted(os.listdir(parent)):
        if not entry.startswith(FEED_PREFIX):
            continue
        d = os.path.join(parent, entry)
        try:
            with open(os.path.join(d, FEED_INFO)) as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict) or not _alive(info.get("pid", 0) or 0):
            continue
        info["dir"] = d
        out.append(info)
    return out


def find_feed(host, name, parent=None):
    return next((f for f in feeds(parent) if f["host"] == host and f["name"] == name), None)


def feed_argv(host, uri, name, address, cache, content=None):
    return [
        sys.executable,
        "-m",
        "beanjar.tuner.antenna",
        "feed",
        host,
        uri,
        name,
        address,
        cache,
    ] + ([content] if content else [])


def start_feed(host, uri, name, address, cache, spawn=subprocess.Popen, parent=None,
               wait=FEED_START_WAIT, content=None):  # fmt: skip
    """A feed of VM name on host from this machine, started if none runs: its feed.json once it
    is connected. Error if it ends first or does not connect within wait seconds (its log's last
    lines)."""
    from .. import ares

    running = find_feed(host, name, parent)
    if running:
        return running
    ares.sweep_homes(parent or ares.home_parent())
    log_path = os.path.join(cache, "feeds", f"{host}-{name}.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(x for x in (base.CORE, env.get("PYTHONPATH")) if x)
    with open(log_path, "ab") as out:
        proc = spawn(
            feed_argv(host, uri, name, address, cache, content),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            close_fds=True,
            start_new_session=True,
        )
    deadline = time.monotonic() + wait
    while True:
        info = find_feed(host, name, parent)
        if info and info.get("pid") == proc.pid and info.get("connected"):
            return info
        if proc.poll() is not None or time.monotonic() > deadline:
            if proc.poll() is None:
                proc.terminate()
            try:
                with open(log_path, errors="replace") as f:
                    tail = [line.strip() for line in f.read().splitlines()[-3:] if line.strip()]
            except OSError:
                tail = []
            raise Error(
                f"tuner: the feed of {host}/{name} did not connect"
                + (f": {tail[-1]}" if tail else "")
                + f" (log: {log_path})"
            )
        time.sleep(0.2)


def stop_feed(host, name, parent=None, wait=5.0):
    """Ends the feed of VM name on host from this machine, if one runs: whether one did."""
    info = find_feed(host, name, parent)
    if not info:
        return False
    try:
        os.kill(info["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + wait
    while _alive(info["pid"]) and time.monotonic() < deadline:
        time.sleep(0.1)
    return True


def feed_main(argv, connect=socket.create_connection):
    """python -m beanjar.tuner.antenna feed HOST URI NAME SOCKET CACHE [DIR]: feeds VM name on host through
    a forward of the shared ssh connection to SOCKET there (its tuner-feed.sock), with the streams
    made into CACHE here (of the VM's own lineup and the user's streams in DIR, remote_dir), until the feeder ends the connection or SIGTERM or SIGHUP; status 0 when
    the connection ended, 1 when it could not be made."""
    import shutil

    from .. import ares

    host, uri, name, address, cache = argv[:5]
    content = argv[5] if len(argv) > 5 else None

    def log(text):
        print(f"{time.strftime('%H:%M:%S')} feed {host}/{name}: {text}", flush=True)

    home = tempfile.mkdtemp(prefix=f"{FEED_PREFIX}{os.getpid()}-", dir=feeds_parent())
    info = {"host": host, "name": name, "pid": os.getpid(), "connected": False}

    def put(st):
        info.update(st)
        ares.write_private(os.path.join(home, FEED_INFO), json.dumps(info).encode())

    old = {s: signal.signal(s, ares.raise_terminated) for s in (signal.SIGTERM, signal.SIGHUP)}
    status = 0
    try:
        put({})
        with ares.Forward(
            host, uri, address, None, os.path.join(home, ares.FORWARD_RECORD)
        ) as port:
            log(f"forward 127.0.0.1:{port} to {address}")
            sock = connect((ares.FORWARD_HOST, port))
            sock.settimeout(None)
            try:
                player = Player(cache, log, streams=lambda: streams(content) if content else {})
                sent = feed_session(sock, player, log, put, content=content)
                log(f"the tuner ended the connection; {sent} packets sent")
            finally:
                sock.close()
    except ares.Terminated:
        log("stopped")
    except (Error, OSError) as e:
        log(str(e))
        status = 1
    finally:
        shutil.rmtree(home, ignore_errors=True)
        for s, handler in old.items():
            signal.signal(s, handler)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
