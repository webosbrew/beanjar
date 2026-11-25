# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Lineup files: the broadcast a VM's antenna carries, in TOML (docs/user/tuner.md): its
physical channels, their services, each service's programme events, picture and sound, and on
ARIB a folder of BML documents per service. The generators (tuner_stream.py for ATSC,
tuner_stream_arib.py for ISDB-T) make their streams from a parsed lineup only; the built-in
lineups are the files in lineups/ beside this module, one per broadcast standard.

parse() and load() check a file and give the normalized lineup (a dict): every key filled in,
each picture {"pattern": NAME}, {"image": PATH} or {"video": PATH}, each sound {"tone": HZ} or
{"file": PATH}, each event {"title", "minutes", "text"}, the paths absolute (they are relative to
the file in it, and stay inside its folder) and "key", what the streams depend on: the lineup's
values and the content of every file it names, so an edit to any of them makes the streams again.
install() copies a file and what it names into a directory (a VM's state)."""

import hashlib
import json
import os
import shutil
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import Error as BaseError


class Error(BaseError):
    pass


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_DIR = os.path.join(HERE, "lineups")
BROADCASTS = ("atsc", "arib")
FILE = "lineup.toml"  # an installed lineup's name in its directory
META = ("source", "base", "text", "key")  # a parsed lineup's keys that are not the file's values

# FFmpeg's lavfi sources that take a size, a rate and a duration (s=, r=, d=)
PATTERNS = (
    "colorspectrum",
    "gradients",
    "pal75bars",
    "pal100bars",
    "rgbtestsrc",
    "smptebars",
    "smptehdbars",
    "testsrc",
    "testsrc2",
    "yuvtestsrc",
    "zoneplate",
)

# a BML folder's file types (ARIB STD-B24 vol. 2, the media types of a carousel module's parts)
BML_TYPES = {
    ".bml": "text/X-arib-bml; charset=euc-jp",
    ".png": "image/X-arib-png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mng": "image/X-arib-mng",
    ".aiff": "audio/X-arib-aiff",
}
BML_STARTUP = "startup.bml"
BML_MAX = 255 * 4066  # a module's DDB blocks are numbered in a byte (tuner_stream_arib.BLOCK_SIZE)


def default_path(broadcast):
    """The built-in lineup file of the broadcast standard (atsc or arib)."""
    return os.path.join(DEFAULTS_DIR, f"{broadcast}.toml")


_defaults = {}


def default(broadcast):
    """The built-in lineup of the broadcast standard, parsed (once)."""
    if broadcast not in _defaults:
        _defaults[broadcast] = load(default_path(broadcast), broadcast)
    return _defaults[broadcast]


# ---- the checks ----


class _Where:
    """Where a value is in the file, for an error: rf[0].services[1].events[2]."""

    def __init__(self, source):
        self.source = source

    def fail(self, where, text):
        raise Error(f"{self.source}: {where + ': ' if where else ''}{text}")


def _table(w, where, v):
    if not isinstance(v, dict):
        w.fail(where, "must be a table")
    return v


def _keys(w, where, t, required, optional):
    for k in required:
        if k not in t:
            w.fail(where, f"{k} is missing")
    unknown = sorted(set(t) - set(required) - set(optional))
    if unknown:
        w.fail(
            where,
            f"unknown key {unknown[0]} (the keys: {', '.join(list(required) + list(optional))})",
        )


def _int(w, where, v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int):
        w.fail(where, f"must be a whole number, not {v!r}")
    if not lo <= v <= hi:
        w.fail(where, f"{v} is out of range ({lo} to {hi})")
    return v


def _str(w, where, v, max_len=None, empty=False):
    if not isinstance(v, str) or (not v and not empty):
        w.fail(where, f"must be a{'' if empty else ' non-empty'} string, not {v!r}")
    if max_len is not None and len(v) > max_len:
        w.fail(where, f"{v!r} is longer than {max_len} characters")
    return v


def _bool(w, where, v):
    if not isinstance(v, bool):
        w.fail(where, f"must be true or false, not {v!r}")
    return v


def _path(w, where, v, base):
    """The absolute path of a path in the file: relative to its folder, inside it."""
    _str(w, where, v)
    if os.path.isabs(v):
        w.fail(where, f"{v}: give it relative to the lineup file")
    norm = os.path.normpath(v)
    if norm == ".." or norm.startswith(".." + os.sep):
        w.fail(where, f"{v}: stays inside the lineup file's folder")
    return os.path.join(base, norm)


def _exists(w, where, path, kind):
    ok = os.path.isdir(path) if kind == "folder" else os.path.isfile(path)
    if not ok:
        w.fail(where, f"{path}: no such {kind}")


def _picture(w, where, v, base, check):
    if isinstance(v, str):
        if v not in PATTERNS:
            w.fail(
                where,
                f"no test pattern {v!r} ({', '.join(PATTERNS)}; or {{ image = FILE }}, {{ video = FILE }})",
            )
        return {"pattern": v}
    t = _table(w, where, v)
    if len(t) != 1 or next(iter(t)) not in ("image", "video"):
        w.fail(where, "a test pattern's name, { image = FILE } or { video = FILE }")
    ((kind, rel),) = t.items()
    path = _path(w, f"{where}.{kind}", rel, base)
    if check:
        _exists(w, f"{where}.{kind}", path, "file")
    return {kind: path}


LANGUAGE = {"atsc": "eng", "arib": "jpn"}  # a first sound track's language when it names none
TRACKS = 3  # sound tracks a service has at most


def _language(w, where, v):
    """An ISO 639-2 code: three lower-case letters."""
    if not (isinstance(v, str) and len(v) == 3 and v.isascii() and v.isalpha() and v.islower()):
        w.fail(where, f"{v!r}: an ISO 639-2 language code, three lower-case letters (eng, spa)")
    return v


def _track(w, where, v, base, check, language):
    t = _table(w, where, v)
    kinds = [k for k in ("tone", "file") if k in t]
    if len(kinds) != 1 or set(t) - {"tone", "file", "language"}:
        w.fail(where, "{ tone = HZ } or { file = FILE } (an audio or video file), and a language")
    kind = kinds[0]
    if "language" in t:
        language = _language(w, f"{where}.language", t["language"])
    elif language is None:
        w.fail(where, 'language is missing (a second sound track\'s language: language = "spa")')
    if kind == "tone":
        return {"tone": _int(w, f"{where}.tone", t["tone"], 20, 20000), "language": language}
    path = _path(w, f"{where}.file", t["file"], base)
    if check:
        _exists(w, f"{where}.file", path, "file")
    return {"file": path, "language": language}


def _sound(w, where, v, base, check, broadcast):
    """A service's sound tracks: one table, or a list of up to TRACKS, the first the main one."""
    tracks = v if isinstance(v, list) else [v]
    if not 1 <= len(tracks) <= TRACKS:
        w.fail(where, f"one sound track or a list of 1 to {TRACKS}")
    out = [
        _track(w, f"{where}[{i}]" if isinstance(v, list) else where, t, base, check,
               LANGUAGE[broadcast] if i == 0 else None)
        for i, t in enumerate(tracks)
    ]  # fmt: skip
    _unique(w, where, [t["language"] for t in out], "language")
    return out


def _captions(w, where, v, base, check):
    """A service's closed captions: { file = SCC, language = ... }."""
    t = _table(w, where, v)
    _keys(w, where, t, ("file",), ("language",))
    path = _path(w, f"{where}.file", t["file"], base)
    if check:
        _exists(w, f"{where}.file", path, "file")
    return {"file": path, "language": _language(w, f"{where}.language", t.get("language", "eng"))}


def _events(w, where, v):
    if not isinstance(v, list) or not v:
        w.fail(
            where, "must be a list of one event or more: { title = ..., minutes = ..., text = ... }"
        )
    out = []
    for i, e in enumerate(v):
        at = f"{where}[{i}]"
        _table(w, at, e)
        _keys(w, at, e, ("title", "minutes"), ("text",))
        out.append(
            {
                "title": _str(w, f"{at}.title", e["title"]),
                "minutes": _int(w, f"{at}.minutes", e["minutes"], 1, 24 * 60),
                "text": _str(w, f"{at}.text", e.get("text", ""), empty=True),
            }
        )
    return out


def _unique(w, where, values, what):
    seen = set()
    for v in values:
        if v in seen:
            w.fail(where, f"{what} {v} is given twice")
        seen.add(v)


def _media(w, where, s, base, check, broadcast):
    """A service's picture, label and sound tracks."""
    picture = _picture(w, f"{where}.picture", s.get("picture", "testsrc2"), base, check)
    return {
        "picture": picture,
        # the label goes on a test pattern, not over a picture of the user's, unless asked
        "label": _bool(w, f"{where}.label", s.get("label", "pattern" in picture)),
        "sound": _sound(
            w, f"{where}.sound", s.get("sound", {"tone": 1000}), base, check, broadcast
        ),
    }


MEDIA_KEYS = ("picture", "label", "sound", "events")


def _atsc_rf(w, where, rf, base, check):
    from .tuner_stream import Error as StreamError
    from .tuner_stream import rf_center_hz

    _keys(w, where, rf, ("channel", "tsid", "services"), ())
    ch = _int(w, f"{where}.channel", rf["channel"], 2, 36)
    try:
        rf_center_hz(ch)
    except StreamError as e:
        w.fail(f"{where}.channel", str(e))
    services = rf["services"]
    if not isinstance(services, list) or not 1 <= len(services) <= 16:
        w.fail(f"{where}.services", "one service or more, at most 16 ([[rf.services]])")
    out = []
    for i, s in enumerate(services):
        at = f"{where}.services[{i}]"
        _table(w, at, s)
        _keys(
            w,
            at,
            s,
            ("major", "minor", "name", "source_id"),
            ("long_name", "program", "captions") + MEDIA_KEYS,
        )
        name = _str(w, f"{at}.name", s["name"], 7)  # the TVCT's short_name: 7 UTF-16 units
        out.append(
            {
                "major": _int(w, f"{at}.major", s["major"], 1, 99),
                "minor": _int(w, f"{at}.minor", s["minor"], 1, 999),
                "name": name,
                "long_name": _str(w, f"{at}.long_name", s.get("long_name", name)),
                "source_id": _int(w, f"{at}.source_id", s["source_id"], 1, 0xFFFF),
                "program": _int(w, f"{at}.program", s.get("program", i + 1), 1, 0xFFFF),
                **_media(w, at, s, base, check, "atsc"),
                "captions": None
                if "captions" not in s
                else _captions(w, f"{at}.captions", s["captions"], base, check),
                "events": _events(w, f"{at}.events", s.get("events")),
            }
        )
    _unique(w, f"{where}.services", [s["program"] for s in out], "program")
    return {"channel": ch, "tsid": _int(w, f"{where}.tsid", rf["tsid"], 0, 0xFFFF), "services": out}


def _arib_rf(w, where, rf, base, check):
    _keys(
        w,
        where,
        rf,
        ("channel", "network_id", "tsid", "area_code", "remote_key", "services"),
        ("network_name", "ts_name"),
    )
    ch = _int(w, f"{where}.channel", rf["channel"], 13, 62)  # UHF
    services = rf["services"]
    if not isinstance(services, list) or not 1 <= len(services) <= 8:
        w.fail(f"{where}.services", "one service or more, at most 8 ([[rf.services]])")
    out = []
    for i, s in enumerate(services):
        at = f"{where}.services[{i}]"
        _table(w, at, s)
        _keys(w, at, s, ("service_id", "name"), ("provider", "bml") + MEDIA_KEYS)
        bml = None
        if "bml" in s:
            bml = _path(w, f"{at}.bml", s["bml"], base)
            if check:
                _exists(w, f"{at}.bml", bml, "folder")
                bml_parts(bml, f"{w.source}: {at}.bml")
        out.append(
            {
                "service_id": _int(w, f"{at}.service_id", s["service_id"], 1, 0xFFFF),
                "name": _str(w, f"{at}.name", s["name"]),
                "provider": None
                if "provider" not in s
                else _str(w, f"{at}.provider", s["provider"]),
                **_media(w, at, s, base, check, "arib"),
                "bml": bml,
                "events": _events(w, f"{at}.events", s.get("events")),
            }
        )
    _unique(w, f"{where}.services", [s["service_id"] for s in out], "service_id")
    ts_name = _str(w, f"{where}.ts_name", rf.get("ts_name", out[0]["name"]))
    network_name = _str(w, f"{where}.network_name", rf.get("network_name", ts_name))
    for s in out:
        s["provider"] = s["provider"] or network_name
    return {
        "channel": ch,
        "network_id": _int(w, f"{where}.network_id", rf["network_id"], 1, 0xFFFF),
        "network_name": network_name,
        "tsid": _int(w, f"{where}.tsid", rf["tsid"], 0, 0xFFFF),
        "area_code": _int(w, f"{where}.area_code", rf["area_code"], 0, 0xFFF),
        "remote_key": _int(w, f"{where}.remote_key", rf["remote_key"], 1, 12),
        "ts_name": ts_name,
        "services": out,
    }


def _av(w, data, broadcast):
    """video, audio and loop_seconds: the file's, else the built-in lineup's."""
    if os.path.abspath(w.source) == os.path.abspath(default_path(broadcast)):
        for k in ("video", "audio", "loop_seconds"):
            if k not in data:
                w.fail("", f"{k} is missing (the built-in lineup gives every value)")
        base = data
    else:
        base = default(broadcast)
    video = _table(w, "video", data.get("video", base["video"]))
    _keys(w, "video", video, (), ("size", "fps", "bitrate"))
    video = {**base["video"], **video}
    size = _str(w, "video.size", video["size"])
    try:
        wd, ht = (int(x) for x in size.split("x"))
        assert 16 <= wd <= 1920 and 16 <= ht <= 1080 and wd % 2 == 0 and ht % 2 == 0
    except (ValueError, AssertionError):
        w.fail("video.size", f"{size!r}: WIDTHxHEIGHT, even, at most 1920x1080")
    fps = _str(w, "video.fps", str(video["fps"]))
    try:
        num, _, den = fps.partition("/")
        assert 0 < int(num) / int(den or 1) <= 60
    except (ValueError, AssertionError, ZeroDivisionError):
        w.fail("video.fps", f"{fps!r}: a rate like 30000/1001 or 30")
    audio = _table(w, "audio", data.get("audio", base["audio"]))
    _keys(w, "audio", audio, (), ("bitrate",))
    audio = {**base["audio"], **audio}
    return {
        "video": {
            "size": size,
            "fps": fps,
            "bitrate": _int(w, "video.bitrate", video["bitrate"], 100_000, 4_000_000),
        },
        "audio": {"bitrate": _int(w, "audio.bitrate", audio["bitrate"], 32_000, 448_000)},
        "loop_seconds": _int(
            w, "loop_seconds", data.get("loop_seconds", base["loop_seconds"]), 1, 600
        ),
    }


def parse(text, source, base, broadcast=None, check_files=True):
    """The normalized lineup of TOML text (source: its name in errors; base: the folder its paths
    are relative to); Error if it is not one, or if broadcast is given and it is another standard's.
    check_files: the files it names must be there (not for a lineup whose files are elsewhere)."""
    w = _Where(source)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        w.fail("", f"not TOML: {e}")
    b = data.get("broadcast")
    if b not in BROADCASTS:
        w.fail("broadcast", f"{b!r}: atsc or arib")
    if broadcast is not None and b != broadcast:
        raise Error(f"{source}: the lineup is {b}, the board's broadcast standard is {broadcast}")
    common = ("broadcast", "video", "audio", "loop_seconds", "rf")
    _keys(w, "", data, ("broadcast", "rf"), common[1:] + (("timezone",) if b == "atsc" else ()))
    out = {"broadcast": b, **_av(w, data, b)}
    if b == "atsc":
        tz = _str(w, "timezone", data.get("timezone", "America/New_York"))
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            w.fail("timezone", f"no time zone {tz!r} (an IANA name: America/Chicago)")
        out["timezone"] = tz
    rfs = data["rf"]
    if not isinstance(rfs, list) or not rfs:
        w.fail("rf", "one physical channel or more ([[rf]])")
    make_rf = _atsc_rf if b == "atsc" else _arib_rf
    out["rf"] = [make_rf(w, f"rf[{i}]", _table(w, f"rf[{i}]", rf), base, check_files)
                 for i, rf in enumerate(rfs)]  # fmt: skip
    out["rf"].sort(key=lambda rf: rf["channel"])
    _unique(w, "rf", [rf["channel"] for rf in out["rf"]], "channel")
    if b == "atsc":
        services = [s for rf in out["rf"] for s in rf["services"]]
        _unique(w, "rf", [f"{s['major']}.{s['minor']}" for s in services], "virtual channel")
        _unique(w, "rf", [s["source_id"] for s in services], "source_id")
    out["source"] = source
    out["base"] = base
    out["text"] = text
    out["key"] = _key(out, check_files)
    return out


def load(path, broadcast=None, check_files=True):
    """The normalized lineup of the file at path (parse)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise Error(f"{path}: {e}") from None
    return parse(text, path, os.path.dirname(os.path.abspath(path)), broadcast, check_files)


# ---- what it names ----


def paths(lu):
    """The files and folders the lineup names: [absolute path], in order."""
    out = []
    for rf in lu["rf"]:
        for s in rf["services"]:
            for v in [s["picture"], *s["sound"], s.get("captions") or {}]:
                out += [p for k, p in v.items() if k in ("image", "video", "file")]
            if s.get("bml"):
                out.append(s["bml"])
    return list(dict.fromkeys(out))


def bml_parts(folder, where=None):
    """[(name, content type, bytes)] of a BML folder's files: startup.bml first, then the others by
    name; Error if it has no startup.bml, a file of an unknown type, a subfolder or is too big."""
    where = where or folder
    try:
        names = sorted(os.listdir(folder))
    except OSError as e:
        raise Error(f"{where}: {e}") from None
    if BML_STARTUP not in names:
        raise Error(f"{where}: no {BML_STARTUP} in {folder} (the page the set starts with)")
    out = []
    for name in [BML_STARTUP] + [n for n in names if n != BML_STARTUP]:
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            raise Error(f"{where}: {name}: a module is one folder of files, no subfolders")
        ext = os.path.splitext(name)[1].lower()
        if ext not in BML_TYPES:
            raise Error(f"{where}: {name}: not a file type of BML ({', '.join(BML_TYPES)})")
        if not name.isascii() or any(c in name for c in '"\r\n'):
            raise Error(f"{where}: {name}: a resource name is ASCII")
        with open(path, "rb") as f:
            out.append((name, BML_TYPES[ext], f.read()))
    if sum(len(b) for _n, _t, b in out) > BML_MAX - 4096:
        raise Error(f"{where}: more than {BML_MAX // 1024} KiB: a module's limit")
    return out


_digests = {}


def _digest(path):
    """The SHA-256 of a file, kept while its size and time are the same."""
    st = os.stat(path)
    memo = (st.st_size, st.st_mtime_ns)
    hit = _digests.get(path)
    if hit and hit[0] == memo:
        return hit[1]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    _digests[path] = (memo, h.hexdigest())
    return h.hexdigest()


def _key(lu, check_files):
    """What the streams depend on: the values (paths relative to the file) and the files' content."""
    base = lu["base"]

    content = {}
    if check_files:
        for p in paths(lu):
            files = [os.path.join(p, n) for n in sorted(os.listdir(p))] if os.path.isdir(p) else [p]
            for f in files:
                content[os.path.relpath(f, base)] = _digest(f)
    text = json.dumps([public(lu), content], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def rf(lu, ch):
    """The lineup's physical channel ch; Error if it has none."""
    for r in lu["rf"]:
        if r["channel"] == ch:
            return r
    raise Error(
        f"the lineup has no physical channel {ch} ({', '.join(str(r['channel']) for r in lu['rf'])})"
    )


def install(src, dest, broadcast, text=None):
    """Checks the lineup file src (or TOML text, its paths relative to the current folder) for the
    broadcast standard and copies it, as dest/lineup.toml, with the files and folders it names, at
    their places relative to it, into the directory dest, which it replaces; the lineup there."""
    if text is None:
        lu = load(src, broadcast)
    else:
        lu = parse(text, src, os.getcwd(), broadcast)
    tmp = dest + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        with open(os.path.join(tmp, FILE), "w", encoding="utf-8") as f:
            f.write(lu["text"])
        for p in paths(lu):
            to = os.path.join(tmp, os.path.relpath(p, lu["base"]))
            os.makedirs(os.path.dirname(to), exist_ok=True)
            if os.path.isdir(p):
                shutil.copytree(p, to, dirs_exist_ok=True)
            else:
                shutil.copyfile(p, to)
        load(os.path.join(tmp, FILE), broadcast)  # the copy is whole
        old = dest + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(dest):
            os.replace(dest, old)
        os.replace(tmp, dest)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return load(os.path.join(dest, FILE), broadcast)


def public(lu):
    """The lineup as --json gives it: its values, paths relative to its file."""
    base = lu["base"]

    def rel(v):
        if isinstance(v, dict):
            return {k: rel(x) for k, x in v.items()}
        if isinstance(v, list):
            return [rel(x) for x in v]
        if isinstance(v, str) and os.path.isabs(v) and v.startswith(base + os.sep):
            return os.path.relpath(v, base)
        return v

    return rel({k: v for k, v in lu.items() if k not in META})
