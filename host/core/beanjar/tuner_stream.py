# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The broadcast the host's tuner sends: one ATSC transport stream per physical channel of
an ATSC lineup (tuner_lineup.py: the built-in lineups/atsc.toml or a VM's own), with the PSIP
tables a US set scans and builds its guide from (PAT, PMT, MGT, TVCT, RRT, STT, EIT-0 to EIT-3
with their ETTs) and each service's picture and sound.

Made with TSDuck (tsp, tstabcomp; upstream's Debian 13 package) and ffmpeg: ffmpeg encodes the
A/V loop (MPEG-2 video, AC-3 audio) once per lineup and channel, tsp injects the tables at their usual
repetition rates (PAT and PMT every 100 ms, MGT 120 ms, TVCT 340 ms, STT 850 ms, EIT-0 500 ms,
ETT-0 1 s, the later blocks 3 s). A stream holds the guide of the four 3-hour blocks from the one
it is made in, so it is made again when a new block starts or the lineup changes (stream_path's
key). The tuner
(tuner.py) loops the file and rewrites the STT's time as it sends it (stt_rewrite), so the set's
clock follows the host's.

A service may have up to three AC-3 sound tracks, each a complete main service of its own language
(the PMT's ISO 639 and AC-3 descriptors, the TVCT's service location), and closed captions from an
SCC file: CEA-608 CC1 as A/53 cc_data in the pictures' user data, named by the caption service
descriptor on the video in the PMT and in each EIT event. FFmpeg makes them: its SCC demuxer reads
the file, the pairs go into an intra-only carrier of the picture (with_cc), and the loop's encoder
writes them again from its decoder's A53 side data (_caption_carrier).

TSDuck writes an ATSC EIT's start_time string straight into the GPS-seconds field, so the times
given to it are UTC plus the GPS-UTC offset (gps_naive). validate() checks a made stream
independently of TSDuck: every long section's CRC32, each table's repetition and that the MGT
lists the versions the EIT and ETT sections carry."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import zlib
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import quoteattr
from zoneinfo import ZoneInfo


class Error(Exception):
    pass


GPS_EPOCH = 315964800  # 1980-01-06 in Unix seconds
GPS_UTC_OFFSET = 18  # leap seconds since then
BASE_PID = 0x1FFB  # ATSC's PSIP base PID: MGT, TVCT, RRT, STT
NULL_PID = 0x1FFF
TABLE_STT = 0xCD
PACKET = 188
MUX_RATE = 5_000_000  # bits a second
SECONDS = 20  # of a made stream, looped
VERSION = 5  # of the generator: part of a stream's key
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"  # fonts-dejavu-core


def eit_pid(k):
    return 0x1D00 + k


def ett_pid(k):
    return 0x1E00 + k


def pmt_pid(i):
    return 0x20 + i


def video_pid(i):
    return 0x31 + 4 * i


def audio_pid(i, k=0):
    """Service i's sound track k: the main one at 0x34 + 4i, the others in the gap below it."""
    return 0x34 + 4 * i if k == 0 else 0x31 + 4 * i + k


FF_PID0 = 0x100  # ffmpeg's elementary PIDs, which tsp remaps


def streams(services):
    """ffmpeg's output streams, in order (its PIDs from FF_PID0 on): (service, None) for a
    service's picture, (service, k) for its sound track k."""
    out = []
    for i, s in enumerate(services):
        out += [(i, None)] + [(i, k) for k in range(len(s["sound"]))]
    return out


def ff_remap(services, pid_of):
    """(the ffmpeg PIDs to keep, tsp remap's FROM=TO list) to pid_of(service, track or None)."""
    keep, remap = [], []
    for n, (i, k) in enumerate(streams(services)):
        keep.append(FF_PID0 + n)
        remap.append(f"{FF_PID0 + n}={pid_of(i, k)}")
    return keep, remap


def ff_programs(services, title=None):
    """ffmpeg's -program options: one a service, with its picture and sound tracks."""
    progs, at = [], 0
    for i, s in enumerate(services):
        n = 1 + len(s["sound"])
        sts = ":".join(f"st={at + j}" for j in range(n))
        progs += ["-program", (f"title={title(s)}:" if title else "") + sts]
        at += n
    return progs


def caption_descriptor(c):
    """ATSC's caption_service_descriptor (A/65 6.9.2) of a service's captions: line 21 CC1."""
    return (
        f'<caption_service_descriptor><service language="{c["language"]}" digital_cc="false" '
        'line21_field="false" caption_service_number="0" easy_reader="false" '
        'wide_aspect_ratio="false"/></caption_service_descriptor>'
    )


def rf_center_hz(ch):
    """The US (ATSC) physical channel's centre frequency in Hz."""
    if 2 <= ch <= 4:
        return (57 + 6 * (ch - 2)) * 1000000
    if 5 <= ch <= 6:
        return (79 + 6 * (ch - 5)) * 1000000
    if 7 <= ch <= 13:
        return (177 + 6 * (ch - 7)) * 1000000
    if 14 <= ch <= 36:
        return (473 + 6 * (ch - 14)) * 1000000
    raise Error(f"no US physical channel {ch} (2 to 36)")


def channel(lu, ch):
    """The lineup's physical channel ch; Error if it has none."""
    for rf in lu["rf"]:
        if rf["channel"] == ch:
            return rf
    raise Error(
        f"the lineup has no physical channel {ch} ({', '.join(str(r['channel']) for r in lu['rf'])})"
    )


def gps_seconds(unix):
    return int(unix) + GPS_UTC_OFFSET - GPS_EPOCH


def gps_naive(unix):
    """The time string TSDuck turns into a GPS-seconds field."""
    return datetime.fromtimestamp(unix + GPS_UTC_OFFSET, UTC).strftime("%Y-%m-%d %H:%M:%S")


def block0(now):
    """The Unix start of the 3-hour EIT-0 block that now is in."""
    return (gps_seconds(now) // 10800) * 10800 + GPS_EPOCH - GPS_UTC_OFFSET


def events(svc, t_from, t_to, timezone="America/New_York"):
    """(start, minutes, title, text) of the service's events overlapping [t_from, t_to), its cycle
    repeated from midnight in the lineup's time zone."""
    tz = ZoneInfo(timezone)
    out = []
    day = datetime.fromtimestamp(t_from, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    day -= timedelta(days=1)
    for _ in range(4):
        t, end = day.timestamp(), (day + timedelta(days=1)).timestamp()
        while t < end:
            for e in svc["events"]:
                title, minutes, text = e["title"], e["minutes"], e["text"]
                if t < t_to and t + minutes * 60 > t_from:
                    out.append((int(t), minutes, title, text))
                t += minutes * 60
                if t >= end:
                    break
        day += timedelta(days=1)
    return out


def event_id(start):
    return (gps_seconds(start) // 60) & 0x3FFF


def _q(s):
    return quoteattr(str(s))


def _tables(rf, now, timezone):
    """{name: TSDuck XML body} of the physical channel's tables at now (without the MGT)."""
    t = {}
    t["pat"] = (
        f'<PAT version="0" transport_stream_id="{rf["tsid"]}">\n'
        + "".join(
            f'  <service service_id="{s["program"]}" program_map_PID="0x{pmt_pid(i):X}"/>\n'
            for i, s in enumerate(rf["services"])
        )
        + "</PAT>\n"
    )
    for i, s in enumerate(rf["services"]):
        cc = s.get("captions")
        pmt = (
            f'<PMT service_id="{s["program"]}" PCR_PID="0x{video_pid(i):X}">\n'
            f'  <component stream_type="0x02" elementary_PID="0x{video_pid(i):X}">'
            + (f"\n    {caption_descriptor(cc)}\n  " if cc else "")
            + "</component>\n"
        )
        # each track a complete main service (bsmod 0) of its own language (A/52 annex A)
        for k, tr in enumerate(s["sound"]):
            pmt += (
                f'  <component stream_type="0x81" elementary_PID="0x{audio_pid(i, k):X}">\n'
                '    <registration_descriptor format_identifier="0x41432D33"/>\n'
                '    <ATSC_AC3_audio_stream_descriptor sample_rate_code="0" bsid="8" bit_rate_code="10" '
                f'surround_mode="0" bsmod="0" num_channels="2" full_svc="true" mainid="{k}" priority="1" '
                f'asvcflags="0" language="{tr["language"]}"/>\n'
                f'    <ISO_639_language_descriptor><language code="{tr["language"]}" audio_type="0"/></ISO_639_language_descriptor>\n'
                "  </component>\n"
            )
        t[f"pmt{i}"] = pmt + "</PMT>\n"
    tvct = f'<TVCT version="0" transport_stream_id="{rf["tsid"]}">\n'
    for i, s in enumerate(rf["services"]):
        tvct += (
            f'  <channel short_name={_q(s["name"])} major_channel_number="{s["major"]}" '
            f'minor_channel_number="{s["minor"]}" modulation_mode="8-VSB" carrier_frequency="0" '
            f'channel_TSID="{rf["tsid"]}" program_number="{s["program"]}" ETM_location="0" '
            f'service_type="dtv" source_id="{s["source_id"]}">\n'
            f'    <service_location_descriptor PCR_PID="0x{video_pid(i):X}">\n'
            f'      <component stream_type="0x02" elementary_PID="0x{video_pid(i):X}"/>\n'
            + "".join(
                f'      <component stream_type="0x81" elementary_PID="0x{audio_pid(i, k):X}" '
                f'ISO_639_language_code="{tr["language"]}"/>\n'
                for k, tr in enumerate(s["sound"])
            )
            + "    </service_location_descriptor>\n"
            "    <extended_channel_name_descriptor><long_channel_name_text>"
            f'<string language="eng" text={_q(s["long_name"])}/></long_channel_name_text>'
            "</extended_channel_name_descriptor>\n"
            "  </channel>\n"
        )
    t["tvct"] = tvct + "</TVCT>\n"
    t["rrt"] = (
        '<RRT rating_region="1">\n  <rating_region_name><string language="eng" '
        'text="US (50 states + possessions)"/></rating_region_name>\n'
        '  <dimension graduated_scale="true">\n    <dimension_name><string language="eng" '
        'text="Entire Audience"/></dimension_name>\n'
        '    <value><abbrev_rating_value><string language="eng" text="None"/></abbrev_rating_value>'
        '<rating_value><string language="eng" text="None"/></rating_value></value>\n'
        "  </dimension>\n</RRT>\n"
    )
    t["stt"] = (
        f'<STT system_time="{gps_seconds(now)}" GPS_UTC_offset="{GPS_UTC_OFFSET}" DS_status="false"/>\n'
    )
    b0 = block0(now)
    for k in range(4):
        t0, t1 = b0 + 10800 * k, b0 + 10800 * (k + 1)
        eit, ett = "", ""
        for s in rf["services"]:
            eit += f'<ATSC_EIT source_id="{s["source_id"]}" version="@V@">\n'
            for start, minutes, title, text in events(s, t0, t1, timezone):
                eid = event_id(start)
                eit += (
                    f'  <event event_id="{eid}" start_time="{gps_naive(start)}" ETM_location="1" '
                    f'length_in_seconds="{minutes * 60}"><title_text><string language="eng" '
                    f"text={_q(title)}/></title_text>"
                    + (caption_descriptor(s["captions"]) if s.get("captions") else "")
                    + "</event>\n"
                )
                etm = (s["source_id"] << 16) | (eid << 2) | 2
                ett += (
                    f'<ETT version="@V@" ETT_table_id_extension="0" ETM_id="0x{etm:08X}">'
                    f'<extended_text_message><string language="eng" text={_q(text)}/>'
                    "</extended_text_message></ETT>\n"
                )
            eit += "</ATSC_EIT>\n"
        # a block's version follows its content, so a set takes a new guide as new
        for name, body in ((f"eit{k}", eit), (f"ett{k}", ett)):
            t[name] = body.replace("@V@", str(zlib.crc32(body.encode()) % 32))
    return t


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise Error(f"{' '.join(cmd[:2])} failed: {(r.stderr or r.stdout).strip()[-400:]}")


def _write_tables(rf, d, now, timezone):
    os.makedirs(d, exist_ok=True)
    tables = _tables(rf, now, timezone)

    def put(name, body):
        with open(os.path.join(d, name + ".xml"), "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tsduck>\n' + body + "</tsduck>\n")

    for name, body in tables.items():
        put(name, body)
    sizes, versions = {}, {}
    for n in ["tvct", "rrt"] + [f"eit{k}" for k in range(4)] + [f"ett{k}" for k in range(4)]:
        _run(["tstabcomp", "-o", os.path.join(d, n + ".bin"), os.path.join(d, n + ".xml")])
        sizes[n] = os.path.getsize(os.path.join(d, n + ".bin"))
        # the MGT lists each table at the version the table itself carries (_tables)
        v = re.search(r'version="(\d+)"', tables[n]) if n[:3] in ("eit", "ett") else None
        versions[n] = int(v.group(1)) if v else 0
    mgt = '<MGT version="0">\n'
    mgt += f'  <table type="TVCT-current" PID="0x{BASE_PID:X}" version_number="0" number_bytes="{sizes["tvct"]}"/>\n'
    mgt += f'  <table type="RRT-1" PID="0x{BASE_PID:X}" version_number="0" number_bytes="{sizes["rrt"]}"/>\n'
    for k in range(4):
        mgt += f'  <table type="EIT-{k}" PID="0x{eit_pid(k):X}" version_number="{versions[f"eit{k}"]}" number_bytes="{sizes[f"eit{k}"]}"/>\n'
    for k in range(4):
        mgt += f'  <table type="ETT-{k}" PID="0x{ett_pid(k):X}" version_number="{versions[f"ett{k}"]}" number_bytes="{sizes[f"ett{k}"]}"/>\n'
    put("mgt", mgt + "</MGT>\n")


def _label_text(text):
    """text for drawtext inside single quotes: the characters the filter graph or drawtext would
    take as their own made look-alikes."""
    return text.replace("'", "\u2019").replace("\\", "/").replace("%", "\uff05")


def av_inputs(services, v, dur, label_of):
    """ffmpeg's inputs and maps of the services' pictures and sounds, dur seconds each: a test
    pattern or a tone is a lavfi source; an image, or a video or audio file (looped), an input
    whose picture is scaled into the size (letterboxed) at the rate. A service with label has
    label_of(service) drawn in the middle. (args, maps): the maps a video stream and an audio
    stream a sound track of each service, in order (streams())."""
    args, maps, graphs = [], [], []
    n = 0
    for i, s in enumerate(services):
        pic, snd = s["picture"], s["sound"]
        draw = ""
        if s["label"]:
            draw = (
                f",drawtext=fontfile={FONT}:text='{_label_text(label_of(s))}':"
                "fontsize=44:fontcolor=white:box=1:boxcolor=black@0.6:x=(w-tw)/2:y=h/2-th"
            )
        if "pattern" in pic:
            args += [
                "-f",
                "lavfi",
                "-i",
                f"{pic['pattern']}=s={v['size']}:r={v['fps']}:d={dur}{draw}",
            ]
            maps += ["-map", f"{n}:v"]
        else:
            if "image" in pic:
                args += ["-loop", "1", "-framerate", v["fps"], "-t", str(dur), "-i", pic["image"]]
            else:
                args += ["-stream_loop", "-1", "-t", str(dur), "-i", pic["video"]]
            w, h = v["size"].split("x")
            graphs.append(
                f"[{n}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={v['fps']},format=yuv420p{draw}[v{i}]"
            )
            maps += ["-map", f"[v{i}]"]
        n += 1
        for tr in snd:
            if "tone" in tr:
                args += [
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={tr['tone']}:sample_rate=48000:duration={dur}",
                ]
                maps += ["-map", f"{n}:a"]
            else:
                args += ["-stream_loop", "-1", "-t", str(dur), "-i", tr["file"]]
                maps += ["-map", f"{n}:a:0"]
            n += 1
    return args + (["-filter_complex", ";".join(graphs)] if graphs else []), maps


def cc_pairs(scc, frames, fps):
    """The CEA-608 byte pairs of an SCC file for each of frames pictures at fps (a Fraction), one
    pair a picture (608's rate on field 1), None where it has none. FFmpeg's SCC demuxer reads the
    file: each packet is its line's cc_data triplets (0xFC, then the pair) at the line's time;
    a line's pairs go out on the pictures from that time on, after the previous line's."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "packet=pts_time,size", "-of", "json", scc],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", scc, "-map", "0:s:0", "-c", "copy", "-f", "data", "-"],
        capture_output=True,
        check=False,
    )
    if probe.returncode or raw.returncode:
        raise Error(
            f"{scc}: not a caption file FFmpeg reads (SCC): {(probe.stderr or raw.stderr.decode(errors='replace')).strip()[-300:]}"
        )
    out, at, cur = [None] * frames, 0, 0
    for pkt in json.loads(probe.stdout).get("packets", []):
        size = int(pkt["size"])
        data, at = raw.stdout[at : at + size], at + size
        cur = max(cur, round(float(pkt["pts_time"]) * fps))
        for j in range(0, size - 2, 3):
            if cur >= frames:
                raise Error(f"{scc}: its captions run past the loop ({frames} pictures)")
            out[cur] = data[j + 1 : j + 3]
            cur += 1
    return out


def a53_user_data(pair):
    """The picture user data (A/53 part 4, 6.2.3) carrying one field-1 608 pair (or 608's null
    pair) and field 2's null pair: ATSC_identifier 'GA94', user_data_type_code 3, cc_data()."""
    pair = pair or b"\x80\x80"
    return b"\x00\x00\x01\xb2GA94\x03" + bytes([0x40 | 2, 0xFF, 0xFC]) + pair + b"\xfd\x80\x80\xff"


def with_cc(es, pairs):
    """The MPEG-2 video elementary stream es (decode order is display order: intra pictures only)
    with each picture's user data from pairs put after its headers, before its first slice."""
    out, i, pos, f, pending = bytearray(), 0, 0, 0, False
    while (j := es.find(b"\x00\x00\x01", pos)) >= 0 and j + 3 < len(es):
        code = es[j + 3]
        if code == 0x00:  # picture_start_code
            pending = True
        elif pending and 0x01 <= code <= 0xAF:  # its first slice
            out += es[i:j] + a53_user_data(pairs[f] if f < len(pairs) else None)
            i, f, pending = j, f + 1, False
        pos = j + 3
    return bytes(out + es[i:]), f


def _caption_carrier(lu, s, path):
    """The service's picture (labelled as av_inputs makes it), with its captions as A/53 cc_data,
    as intra-only high-quality MPEG-2 at path: the loop's input for that picture, whose decoder
    gives ffmpeg the captions as each frame's A53 side data, which its MPEG-2 encoder writes again
    into the loop's pictures (-a53cc, on by default)."""
    from fractions import Fraction

    v, dur = lu["video"], lu["loop_seconds"]
    fps = Fraction(v["fps"])
    inputs, maps = av_inputs([s], v, dur, lambda x: f"{x['major']}.{x['minor']} {x['name']}")
    es = path + ".es"
    _run(
        ["ffmpeg", "-v", "error", "-y"]
        + inputs
        + maps[:2]
        + ["-c:v", "mpeg2video", "-q:v", "2", "-g", "1", "-bf", "0", "-pix_fmt", "yuv420p"]
        + ["-f", "mpeg2video", es]
    )
    with open(es, "rb") as f:
        data = f.read()
    os.unlink(es)
    frames = round(dur * fps)
    data, pictures = with_cc(data, cc_pairs(s["captions"]["file"], frames, fps))
    if not pictures:
        raise Error(f"{s['name']}: no pictures in the captions' carrier")
    with open(path, "wb") as f:
        f.write(data)


def _make_loop(lu, rf, path):
    """The A/V loop of the physical channel: each service's picture, labelled with the virtual
    channel where the lineup says so, with its captions, and its sound tracks, MPEG-2 and AC-3 at
    the mux rate."""
    v, a, dur = lu["video"], lu["audio"], lu["loop_seconds"]
    services, carriers = [], []
    for i, s in enumerate(rf["services"]):
        if s.get("captions"):
            carrier = f"{path}.cc{i}.m2v"
            _caption_carrier(lu, s, carrier)
            carriers.append(carrier)
            s = {**s, "picture": {"video": carrier}, "label": False}
        services.append(s)
    try:
        cmd = ["ffmpeg", "-v", "error", "-y"]
        inputs, maps = av_inputs(
            services, v, dur, lambda s: f"{s['major']}.{s['minor']} {s['name']}"
        )
        cmd += inputs
        progs = ff_programs(services, lambda s: s["name"])
        vb = str(v["bitrate"])
        cmd += (
            maps
            + progs
            + [
                "-c:v",
                "mpeg2video",
                "-b:v",
                vb,
                "-minrate",
                vb,
                "-maxrate",
                vb,
                "-bufsize",
                "1835008",
                "-g",
                "15",
                "-bf",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-flags",
                "+cgop",
                "-sc_threshold",
                "1000000000",
                "-c:a",
                "ac3",
                "-b:a",
                str(a["bitrate"]),
                "-ar",
                "48000",
                "-ac",
                "2",
                "-f",
                "mpegts",
                "-mpegts_start_pid",
                str(FF_PID0),
                "-pcr_period",
                "40",
                "-muxrate",
                str(MUX_RATE),
                path,
            ]
        )
        _run(cmd)
    finally:
        for c in carriers:
            if os.path.exists(c):
                os.unlink(c)


def _tsp(lu, rf, d, loop, out):
    repeat = max(1, -(-SECONDS // lu["loop_seconds"]))
    a = ["tsp", "--bitrate", str(MUX_RATE), "-I", "file", loop, "--repeat", str(repeat)]
    keep, remap = ff_remap(
        rf["services"], lambda i, k: video_pid(i) if k is None else audio_pid(i, k)
    )
    keep.append(NULL_PID)
    a += ["-P", "filter", "--stuffing"] + sum([["--pid", str(p)] for p in sorted(keep)], [])
    a += ["-P", "continuity", "--fix", "-P", "remap", "--no-psi"] + remap

    def inject(pid, files):
        return ["-P", "inject", "--stuffing", "--pid", f"0x{pid:X}"] + [
            f"{os.path.join(d, n)}.xml={ms}" for n, ms in files
        ]

    a += inject(0, [("pat", 100)])
    for i in range(len(rf["services"])):
        a += inject(pmt_pid(i), [(f"pmt{i}", 100)])
    a += inject(BASE_PID, [("mgt", 120), ("tvct", 340), ("rrt", 60000), ("stt", 850)])
    for k in range(4):
        a += inject(eit_pid(k), [(f"eit{k}", 500 if k == 0 else 3000)])
        a += inject(ett_pid(k), [(f"ett{k}", 1000 if k == 0 else 3000)])
    _run(a + ["-O", "file", out])


def tools():
    """The programs a stream needs that this host lacks."""
    return [t for t in ("tsp", "tstabcomp", "ffmpeg") if not shutil.which(t)]


def stream_key(lu, ch, now):
    """What a physical channel's stream depends on: the generator, the lineup (its values and its
    files' content: its key) and the EIT block."""
    text = json.dumps([VERSION, lu["key"], ch, block0(now)], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def stream_prefix(lu, ch):
    """A channel's streams' names start with the lineup's key: VMs of other lineups share the cache
    without removing each other's."""
    return f"rf{ch:02d}-{lu['key'][:8]}-"


def stream_path(cache, lu, ch, now):
    return os.path.join(cache, f"{stream_prefix(lu, ch)}{stream_key(lu, ch, now)}.ts")


_OWNER = re.compile(r"^(?:rf|loop|isdbt)\d+-([0-9a-f]{8})-.*\.ts$")


def cache_owner(name):
    """The first 8 digits of the lineup key a cached file (a stream or a loop, by its name) was made
    for; None for any other file."""
    m = _OWNER.match(name)
    return m.group(1) if m else None


def prune(cache, keep):
    """Removes the streams and loops in the directory cache that were made for a lineup whose key
    (8 digits, cache_owner) is not in keep; the names removed. Files of other names stay."""
    gone = []
    try:
        names = sorted(os.listdir(cache))
    except FileNotFoundError:
        return gone
    for name in names:
        owner = cache_owner(name)
        if owner is not None and owner not in keep:
            try:
                os.unlink(os.path.join(cache, name))
            except FileNotFoundError:
                continue
            gone.append(name)
    return gone


def make(cache, lu, ch, now):
    """The path of the lineup's physical channel's stream for the EIT block of now, made into the
    directory cache if it is not there yet (validated before it is kept)."""
    path = stream_path(cache, lu, ch, now)
    if os.path.exists(path):
        return path
    missing = tools()
    if missing:
        raise Error(f"no {', '.join(missing)} on this host: install TSDuck and ffmpeg")
    rf = channel(lu, ch)
    os.makedirs(cache, exist_ok=True)
    work = path + ".work"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        loop_key = hashlib.sha256(
            json.dumps([VERSION, lu["key"], ch], sort_keys=True).encode()
        ).hexdigest()[:16]
        loop = os.path.join(cache, f"loop{ch:02d}-{lu['key'][:8]}-{loop_key}.ts")
        if not os.path.exists(loop):
            _make_loop(lu, rf, loop + ".tmp")
            os.replace(loop + ".tmp", loop)
        _write_tables(rf, work, now, lu["timezone"])
        _tsp(lu, rf, work, loop, work + "/out.ts")
        report = validate(work + "/out.ts")
        if report["bad_crc"] or not report["ok_crc"] or report["mgt_mismatch"]:
            raise Error(f"rf{ch}: the made stream does not validate: {report}")
        for table in ("PAT", "TVCT", "MGT", "STT", "EIT"):
            if table not in report["tables"]:
                raise Error(f"rf{ch}: the made stream has no {table}")
        os.replace(work + "/out.ts", path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    # the older blocks of this channel of the lineup go
    for name in os.listdir(cache):
        full = os.path.join(cache, name)
        if name.endswith(".ts") and full != path and name.startswith(stream_prefix(lu, ch)):
            os.unlink(full)
    return path


def crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


NAMES = {
    0x00: "PAT",
    0x02: "PMT",
    0xC7: "MGT",
    0xC8: "TVCT",
    0xCA: "RRT",
    0xCB: "EIT",
    0xCC: "ETT",
    0xCD: "STT",
}


def validate(path):
    """{"ok_crc", "bad_crc", "tables": {name: count}, "mgt_mismatch", "packets"} of the stream's
    PSI/PSIP sections, reassembled here, with the CRC32 of each long section checked independently
    of TSDuck; mgt_mismatch lists "PID 0x....: MGT vN, sections v..." for each EIT/ETT PID whose
    sections carry another version than the MGT lists (a set may filter on the MGT's)."""
    data = open(path, "rb").read()
    buf, ok, bad, tables = {}, 0, 0, {}
    listed, carried = {}, {}  # PID: the MGT's version; PID: {the sections' versions}

    def flush(pid):
        nonlocal ok, bad
        b = buf[pid]
        while len(b) >= 3 and b[0] != 0xFF:
            n = 3 + (((b[1] & 0xF) << 8) | b[2])
            if len(b) < n:
                break
            sec, b = b[:n], b[n:]
            if sec[1] & 0x80:
                if crc32_mpeg(sec) == 0:
                    ok += 1
                else:
                    bad += 1
            name = NAMES.get(sec[0], f"0x{sec[0]:02X}")
            tables[name] = tables.get(name, 0) + 1
            if sec[0] == 0xC7 and len(sec) >= 15:  # MGT: tables_defined, then an entry each
                at = 11
                for _ in range(int.from_bytes(sec[9:11], "big")):
                    if at + 11 > len(sec) - 4:
                        break
                    listed[((sec[at + 2] & 0x1F) << 8) | sec[at + 3]] = sec[at + 4] & 0x1F
                    at += 11 + (((sec[at + 9] & 0xF) << 8) | sec[at + 10])
            elif sec[0] in (0xCB, 0xCC):  # EIT, ETT
                carried.setdefault(pid, set()).add((sec[5] >> 1) & 0x1F)
        buf[pid] = b if b and b[0] != 0xFF else b""

    for off in range(0, len(data) - PACKET + 1, PACKET):
        p = data[off : off + PACKET]
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if p[0] != 0x47 or not (pid < 0x30 or pid >= 0x1D00) or pid == NULL_PID or not p[3] & 0x10:
            continue
        pl = p[4 + (1 + p[4] if p[3] & 0x20 else 0) :]
        if p[1] & 0x40:
            if not pl:
                continue
            ptr = pl[0]
            if pid in buf:
                buf[pid] += pl[1 : 1 + ptr]
                flush(pid)
            buf[pid] = pl[1 + ptr :]
        elif pid in buf:
            buf[pid] += pl
        else:
            continue
        flush(pid)
    mismatch = [
        f"PID 0x{p:04X}: MGT v{listed[p]}, sections v{','.join(map(str, sorted(v)))}"
        for p, v in sorted(carried.items())
        if p in listed and v != {listed[p]}
    ]
    return {
        "ok_crc": ok,
        "bad_crc": bad,
        "tables": tables,
        "mgt_mismatch": mismatch,
        "packets": len(data) // PACKET,
    }


def stt_rewrite(packet, now):
    """The packet with its STT's system_time set to now and the CRC made again; the packet itself
    when it carries no whole STT section (the STT is short: one packet)."""
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    if pid != BASE_PID or not packet[1] & 0x40 or not packet[3] & 0x10:
        return packet
    at = 4 + (1 + packet[4] if packet[3] & 0x20 else 0)
    at += 1 + packet[at]  # the pointer field
    if at + 3 > PACKET or packet[at] != TABLE_STT:
        return packet
    n = 3 + (((packet[at + 1] & 0xF) << 8) | packet[at + 2])
    if at + n > PACKET or n < 17:
        return packet
    sec = bytearray(packet[at : at + n])
    sec[9:13] = gps_seconds(now).to_bytes(4, "big")  # after table_id ... protocol_version
    sec[-4:] = crc32_mpeg(bytes(sec[:-4])).to_bytes(4, "big")
    return packet[:at] + bytes(sec) + packet[at + n :]
