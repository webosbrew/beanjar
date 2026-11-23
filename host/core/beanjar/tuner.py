# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The antenna of a VM whose board has our tuner (targets: the dtv_soc module): which physical
channels carry a signal, and the transport stream of the one the set is tuned to.

`beanjar io tuner list NAME` lists the lineup's physical channels (tuner_stream.LINEUP), which of them
are mapped and what the set is tuned to; `beanjar io tuner map NAME RF...` and `unmap NAME RF...|all` change
the map, which is kept in the VM's state (tuner.json) for its next starts. A mapped channel's
stream is made (tuner_stream.make, into the cache's tuner/) when it is mapped.

Two halves, which may run on two machines:

- The generator (Player, Sender) makes the streams of the wanted channels with TSDuck and ffmpeg
  and sends the map of those that are ready and, while frontend 0 is tuned to one of them, that
  channel's stream, looped at its mux rate, without its null packets and with the STT's time set
  to now (include/webos-tuner.h's frames).
- The feeder (Tuner) runs beside QEMU: the launcher starts it (`python -m beanjar.tuner PID
  RUN_DIR STATE_DIR CACHE`) beside a VM that has the webos.tuner port (qemu.py). It connects to
  QEMU's socket for the port (RUN_DIR's tuner.sock), writes what it does to RUN_DIR's
  tuner-status.json and ends with QEMU (PID). It listens on RUN_DIR's tuner-feed.sock for a feed:
  a generator elsewhere. While one is connected it tells it the channels the VM's map wants (a
  MAP frame from the feeder's side) and the guest's tunes, and passes the feed's MAP and TS
  frames on to the guest, whole: it needs neither TSDuck nor ffmpeg. Without a feed it runs the
  generator itself, on this host's tools and cache.

A VM on another host (`beanjar io tuner map HOST/NAME RF...`) is fed from this machine: the host's
beanjar keeps the map without making streams (--fed), and a feed process here (`python -m
beanjar.tuner feed HOST URI NAME SOCKET CACHE`, started detached by start_feed) reaches its
tuner-feed.sock through a forward of the host's shared ssh connection (ares.Forward), so only the
tuned channel crosses the network, encrypted, over the connection beanjar already has. The feed
lives as long as that connection to the feeder: it ends when the VM stops (the feeder ends with
QEMU), when unmap leaves nothing mapped (stop_feed), or on SIGTERM; a VM started again is fed again
by the next map. Its record directory (FEED_PREFIX, ares.sweep_homes) names it for the next map,
unmap and the sweep that cancels the forward of one killed outright."""

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

from . import tuner_stream
from .base import Error

MAGIC = 0x4E555457
MAP, TS, TUNE = 1, 2, 3
HEADER = struct.Struct("<IIII")
STATE = "tuner.json"
STATUS = "tuner-status.json"
SOCK = "tuner.sock"
FEED_SOCK = "tuner-feed.sock"  # in RUN_DIR: where a generator elsewhere connects
TICK = 0.02  # seconds between sends
TOLERANCE_HZ = 1_000_000  # dtv_soc.c's LOCK_TOLERANCE_HZ
SYS_ATSC = 11  # enum fe_delivery_system
CHUNK_PACKETS = 512  # include/webos-tuner.h's WEBOS_TUNER_TS_MAX
MAP_MAX = 128  # include/webos-tuner.h's WEBOS_TUNER_MAP_MAX
LIMITS = {MAP: MAP_MAX * 8, TS: CHUNK_PACKETS * tuner_stream.PACKET, TUNE: 64}  # payload bytes
FEED_PREFIX = "beanjar-tuner-"  # a feed's record directory: beanjar-tuner-PID-RANDOM
FEED_INFO = "feed.json"
FEED_WAIT = 15.0  # seconds a feed waits for the feeder's first frame
FEED_START_WAIT = 30.0  # seconds start_feed waits for the feed to be connected


def frame(kind, freq, payload=b""):
    return HEADER.pack(MAGIC, kind, freq, len(payload)) + payload


def map_frame(freqs, delsys=SYS_ATSC):
    """The map: the centre frequencies, each of the delivery system (the lineup's are ATSC)."""
    return frame(MAP, 0, b"".join(struct.pack("<II", f, delsys) for f in freqs))


def tune_frame(freq, delsys):
    return frame(TUNE, freq, struct.pack("<I", delsys))


def map_freqs(payload):
    """The centre frequencies of a MAP frame's payload, sorted."""
    return sorted(f for f, _d in struct.iter_unpack("<II", payload[: len(payload) // 8 * 8]))


def ts_frames(freq, packets):
    """TS frames of the packets (bytes, a multiple of 188), at most CHUNK_PACKETS each."""
    step = CHUNK_PACKETS * tuner_stream.PACKET
    return b"".join(frame(TS, freq, packets[i : i + step]) for i in range(0, len(packets), step))


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
    and delsys its delivery system, wanted the last MAP's frequencies (a feeder's: the channels
    its VM's map wants)."""

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
                self.wanted = map_freqs(payload)
        return frames


class Stream:
    """A made stream looped at its mux rate: next(seconds) the packets (nulls dropped) that the
    time takes, with the STT's time made now's."""

    def __init__(self, path, rate=tuner_stream.MUX_RATE):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        self.count = len(self.data) // tuner_stream.PACKET
        self.rate = rate
        self.at = 0
        self.owed = 0.0

    def next(self, seconds, now=None):
        self.owed += seconds * self.rate / 8 / tuner_stream.PACKET
        n = int(self.owed)
        self.owed -= n
        out = []
        for _ in range(n):
            p = self.data[self.at * tuner_stream.PACKET : (self.at + 1) * tuner_stream.PACKET]
            self.at = (self.at + 1) % self.count
            pid = ((p[1] & 0x1F) << 8) | p[2]
            if pid == tuner_stream.NULL_PID:
                continue
            if pid == tuner_stream.BASE_PID:
                p = tuner_stream.stt_rewrite(p, now if now is not None else time.time())
            out.append(p)
        return b"".join(out)


# ---- the map, kept in the VM's state ----


def mapped(state_dir):
    """The physical channels mapped for the VM, sorted."""
    try:
        with open(os.path.join(state_dir, STATE)) as f:
            return sorted(set(json.load(f).get("channels", [])))
    except (OSError, ValueError):
        return []


def save_map(state_dir, channels):
    os.makedirs(state_dir, exist_ok=True)
    tmp = os.path.join(state_dir, STATE + ".tmp")
    with open(tmp, "w") as f:
        json.dump({"channels": sorted(set(channels))}, f)
        f.write("\n")
    os.replace(tmp, os.path.join(state_dir, STATE))


def channels_of(freqs):
    """The lineup's physical channels whose centre frequencies are among freqs (within
    TOLERANCE_HZ), sorted; frequencies of none are left out."""
    out = set()
    for rf in tuner_stream.LINEUP["rf"]:
        c = tuner_stream.rf_center_hz(rf["channel"])
        if any(abs(c - f) <= TOLERANCE_HZ for f in freqs):
            out.add(rf["channel"])
    return sorted(out)


def cache_dir(tree):
    return tree.cache("tuner")


def has_tuner(target):
    """Whether the target's board has our tuner: dtv_soc among its modules."""
    from . import targets

    return any(m.name == "dtv_soc" for m in targets.soc(target).modules)


def status(vm):
    """{"name", "tuner", "channels": [{"channel", "center_hz", "mapped", "services"}], "tuned_hz",
    "sent_packets", "connected", "fed", "feed_socket"} of the VM: fed while a generator elsewhere
    feeds it, feed_socket where one connects."""
    st = {}
    try:
        with open(os.path.join(vm.run_dir, STATUS)) as f:
            st = json.load(f)
    except (OSError, ValueError):
        pass
    on = mapped(vm.state_dir)
    return {
        "name": vm.name,
        "tuner": has_tuner(vm.target()),
        "channels": [
            {
                "channel": rf["channel"],
                "center_hz": tuner_stream.rf_center_hz(rf["channel"]),
                "mapped": rf["channel"] in on,
                "services": [f"{s['major']}.{s['minor']} {s['name']}" for s in rf["services"]],
            }
            for rf in tuner_stream.LINEUP["rf"]
        ],
        "tuned_hz": st.get("tuned_hz"),
        "sent_packets": st.get("sent_packets", 0),
        "connected": st.get("connected", False),
        "fed": st.get("fed", False),
        "feed_socket": os.path.join(vm.run_dir, FEED_SOCK),
    }


def check_channels(channels):
    for ch in channels:
        try:
            tuner_stream.channel(ch)
        except tuner_stream.Error as e:
            raise Error(str(e)) from None


def make(cache, channels, hint=""):
    """Makes the streams of channels into cache (tuner_stream.make); Error, with hint, if not."""
    now = time.time()
    for ch in channels:
        try:
            tuner_stream.make(cache, ch, now)
        except tuner_stream.Error as e:
            raise Error(f"tuner: {e}{hint}") from None


def change(vm, tree, add=(), remove=(), clear=False, fed=False):
    """Maps the physical channels add (making their streams, unless fed: a feed from another
    machine makes them) and unmaps remove (or all with clear); the VM's status after."""
    if not has_tuner(vm.target()):
        raise Error(f"VM {vm.name}: its board has no tuner of ours (no dtv_soc)")
    check_channels(list(add) + list(remove))
    if not fed:
        make(
            cache_dir(tree),
            add,
            "; or map them from a machine that has the tools: beanjar io tuner map HOST/NAME RF...",
        )
    channels = [] if clear else mapped(vm.state_dir)
    channels = [c for c in channels if c not in remove] + list(add)
    save_map(vm.state_dir, channels)
    return status(vm)


# ---- the generator: needs TSDuck and ffmpeg, or the streams already in its cache ----


class Player:
    """The streams of the wanted channels, made into cache when they are not there (a new EIT
    block, a cache cleared) and looped; a channel whose stream cannot be made is left out, its
    reason logged once."""

    def __init__(self, cache, log):
        self.cache, self.log = cache, log
        self.streams = {}  # centre Hz -> Stream
        self.failed = {}  # channel -> the reason last logged

    def ready(self, channels):
        """{centre Hz: Stream} of channels whose streams are made."""
        out = {}
        now = time.time()
        for ch in channels:
            f = tuner_stream.rf_center_hz(ch)
            try:
                path = tuner_stream.make(self.cache, ch, now)
            except tuner_stream.Error as e:
                if self.failed.get(ch) != str(e):
                    self.log(f"rf{ch}: {e}")
                self.failed[ch] = str(e)
                continue
            self.failed.pop(ch, None)
            old = self.streams.get(f)
            out[f] = old if old is not None and old.path == path else Stream(path)
        self.streams = out
        return out


class Sender:
    """The generator's half of a connection: tick() sends, through send, the map of the wanted
    channels whose streams are ready (each second, when it changed) and the tuned one's packets
    since the last tick; counted(n) is told the packets sent."""

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
        s = find(self.player.streams, tuned) if delsys == SYS_ATSC else None
        if s is not None and self.last is not None:
            packets = s.next(min(now - self.last, 0.2))
            if packets:
                self.send(ts_frames(tuned, packets))
                self.counted(len(packets) // tuner_stream.PACKET)
        self.last = now


# ---- the feeder beside QEMU ----


class Tuner:
    def __init__(self, run_dir, state_dir, cache):
        self.run_dir, self.state_dir, self.cache = run_dir, state_dir, cache
        self.player = Player(cache, self.log)
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

    def wanted(self):
        return [tuner_stream.rf_center_hz(ch) for ch in mapped(self.state_dir)]

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
                    "map_hz": map_freqs(self.feed_map[HEADER.size :])
                    if feed is not None and self.feed_map
                    else sorted(self.player.streams),
                    "tuned_hz": self.reader.tuned,
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
                        self.count(len(payload) // tuner_stream.PACKET)
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


def find(streams, tuned):
    """The stream of the mapped centre frequency within TOLERANCE_HZ of tuned, or None."""
    for f, s in streams.items():
        if tuned and abs(f - tuned) <= TOLERANCE_HZ:
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
    tuner = Tuner(run_dir, state_dir, cache)
    path = os.path.join(run_dir, SOCK)
    try:
        feeds_at = tuner.listen()
    except OSError as e:
        feeds_at = f"none ({e})"  # this host's own streams alone
    tuner.log(f"QEMU {pid}, {path}; feeds on {feeds_at}")
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


def feed_session(sock, player, log, on_status=None, wait=FEED_WAIT):
    """Serves the feeder at the other end of sock (the channels its MAP frames want, the tune
    its TUNE frames pass on) until the connection ends; Error if it says nothing within wait
    seconds (no feeder there). on_status(dict) is told what it does each second."""
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
            sender.tick(channels_of(wanted), tuned, delsys, now)
            if on_status and now - last_status >= 1.0:
                last_status = now
                on_status(
                    {
                        "connected": True,
                        "map_hz": sender.map_sent,
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
    from . import ares

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


def feed_argv(host, uri, name, address, cache):
    return [sys.executable, "-m", "beanjar.tuner", "feed", host, uri, name, address, cache]


def start_feed(host, uri, name, address, cache, spawn=subprocess.Popen, parent=None,
               wait=FEED_START_WAIT):  # fmt: skip
    """A feed of VM name on host from this machine, started if none runs: its feed.json once it
    is connected. Error if it ends first or does not connect within wait seconds (its log's last
    lines)."""
    from . import ares

    running = find_feed(host, name, parent)
    if running:
        return running
    ares.sweep_homes(parent or ares.home_parent())
    log_path = os.path.join(cache, "feeds", f"{host}-{name}.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    env = dict(os.environ)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = os.pathsep.join(x for x in (here, env.get("PYTHONPATH")) if x)
    with open(log_path, "ab") as out:
        proc = spawn(
            feed_argv(host, uri, name, address, cache),
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
    """python -m beanjar.tuner feed HOST URI NAME SOCKET CACHE: feeds VM name on host through a
    forward of the shared ssh connection to SOCKET there (its tuner-feed.sock), with the streams
    made into CACHE here, until the feeder ends the connection or SIGTERM or SIGHUP; status 0 when
    the connection ended, 1 when it could not be made."""
    import shutil

    from . import ares

    host, uri, name, address, cache = argv

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
                sent = feed_session(sock, Player(cache, log), log, put)
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
