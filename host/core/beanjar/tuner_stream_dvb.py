# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""DVB transport streams, one per physical channel of a DVB lineup (tuner_lineup.py: the built-in
lineups/dvb.toml or a VM's own): DVB-T on the air, DVB-C on cable. Each service's PMT carries
MPEG-2 video (also its PCR) and MPEG-1 layer II sound tracks, each with its ISO 639 language.
Around them the SI (EN 300 468) a European set scans and fills its guide from:

- NIT actual: the network's name and, for the TS, its service list, its delivery descriptor
  (terrestrial: centre frequency, 8 MHz; cable: frequency, modulation, symbol rate) and each
  service's logical channel number (the EACEM descriptor, after its private data specifier);
- SDT actual: each service's name and provider, with EIT schedule and present/following;
- EIT actual present/following and schedule: the lineup's programme cycle from now to a day
  ahead, normalized by TSDuck into the sections and segments of TS 101 211 (a W21P DVB board fills
  its guide only from the schedule: docs/research/tuner-emulation.md);
- TDT and TOT in UTC, the TOT with the local time offset of the lineup's country and time zone.
  The tuner (tuner.py) rewrites their time to now as it sends them (time_rewrite).

Made like the ARIB streams (tuner_stream_arib.py): ffmpeg encodes the A/V loop, tsp injects the
tables at their rates. A stream holds the present and following events of its services, so it is
made again when a present event ends (stream_key)."""

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

from . import tuner_band
from .tuner_stream import FF_PID0, NULL_PID, PACKET, Error, channel, crc32_mpeg, events, ff_remap
from .tuner_stream_arib import time_rewrite as _time_rewrite

MUX_RATE = 5_000_000  # bits a second
VERSION = 1  # of the generator: part of a stream's key (stream_key)
SECONDS = 10  # of a made stream, looped
SCHEDULE_HOURS = 24  # of the EIT schedule, from now

PAT_PID, NIT_PID, SDT_PID, EIT_PID, TOT_PID = 0x00, 0x10, 0x11, 0x12, 0x14
TABLE_TDT, TABLE_TOT = 0x70, 0x73
PMT_PID, VIDEO_PID, AUDIO_PID = 0x1F0, 0x111, 0x112  # the first service's


def pmt_pid(i):
    return PMT_PID + i


def video_pid(i):
    return VIDEO_PID + 0x10 * i


def audio_pid(i, k=0):
    """Service i's sound track k."""
    return AUDIO_PID + 0x10 * i + k


def _q(s):
    return quoteattr(str(s))


def _utc(t, fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.fromtimestamp(int(t), UTC).strftime(fmt)


def present_following(svc, now, timezone):
    """(start, minutes, title, text) of the service's present and following events."""
    return [e for e in events(svc, now, now + 48 * 3600, timezone) if e[0] + e[1] * 60 > now][:2]


def local_offset(timezone, now):
    """(offset now in minutes, the next change as Unix time, the offset after it) of the time zone;
    a zone without changes gives a change a year on to the same offset."""
    tz = ZoneInfo(timezone)

    def off(t):
        return int(datetime.fromtimestamp(t, tz).utcoffset().total_seconds() // 60)

    cur = off(now)
    step = 3600
    t = int(now) - int(now) % step
    for _ in range(366 * 24):
        t += step
        if off(t) != cur:
            lo = t - step  # the change is within the hour before t: to the second
            while t - lo > 1:
                mid = (lo + t) // 2
                lo, t = (mid, t) if off(mid) == cur else (lo, mid)
            return cur, t, off(t)
    return cur, int(now) + 365 * 86400, cur


def _delivery(rf):
    hz = tuner_band.center_hz("dvb", rf["rf"])
    if rf["system"] == "c":
        modulation = {"qam64": "64-QAM", "qam256": "256-QAM"}[rf["modulation"]]
        return (
            f'<cable_delivery_system_descriptor frequency="{hz}" FEC_outer="RS" '
            f'modulation="{modulation}" symbol_rate="{rf["symbol_rate"]}" FEC_inner="none"/>'
        )
    return (
        f'<terrestrial_delivery_system_descriptor centre_frequency="{hz}" bandwidth="8MHz" '
        'priority="HP" no_time_slicing="true" no_MPE_FEC="true" constellation="64-QAM" '
        'hierarchy_information="0" code_rate_HP_stream="2/3" code_rate_LP_stream="2/3" '
        'guard_interval="1/4" transmission_mode="8k" other_frequency="false"/>'
    )


def _tables(lu, rf, now):
    """{name: TSDuck XML body} of the physical channel's tables at now."""
    t, services = {}, rf["services"]
    tsid, onid, tz = rf["tsid"], rf["onid"], lu["timezone"]
    t["pat"] = (
        f'<PAT version="0" transport_stream_id="{tsid}" network_PID="0x{NIT_PID:X}">\n'
        + "".join(
            f'  <service service_id="{s["service_id"]}" program_map_PID="0x{pmt_pid(i):X}"/>\n'
            for i, s in enumerate(services)
        )
        + "</PAT>\n"
    )
    for i, s in enumerate(services):
        pmt = (
            f'<PMT version="0" service_id="{s["service_id"]}" PCR_PID="0x{video_pid(i):X}">\n'
            f'  <component stream_type="0x02" elementary_PID="0x{video_pid(i):X}"/>\n'
        )
        for k, tr in enumerate(s["sound"]):
            pmt += (
                f'  <component stream_type="0x03" elementary_PID="0x{audio_pid(i, k):X}">\n'
                f'    <ISO_639_language_descriptor><language code="{tr["language"]}" audio_type="0"/>'
                "</ISO_639_language_descriptor>\n  </component>\n"
            )
        t[f"pmt{i}"] = pmt + "</PMT>\n"
    t["nit"] = (
        f'<NIT version="0" network_id="{rf["network_id"]}">\n'
        f"  <network_name_descriptor network_name={_q(rf['network_name'])}/>\n"
        f'  <transport_stream transport_stream_id="{tsid}" original_network_id="{onid}">\n'
        "    <service_list_descriptor>"
        + "".join(
            f'<service service_id="{s["service_id"]}" service_type="0x01"/>' for s in services
        )
        + "</service_list_descriptor>\n"
        f"    {_delivery(rf)}\n"
        '    <private_data_specifier_descriptor private_data_specifier="eacem"/>\n'
        "    <eacem_logical_channel_number_descriptor>"
        + "".join(
            f'<service service_id="{s["service_id"]}" logical_channel_number="{s["lcn"]}" '
            'visible_service="true"/>'
            for s in services
        )
        + "</eacem_logical_channel_number_descriptor>\n"
        "  </transport_stream>\n</NIT>\n"
    )
    t["sdt"] = (
        f'<SDT version="0" transport_stream_id="{tsid}" original_network_id="{onid}">\n'
        + "".join(
            f'  <service service_id="{s["service_id"]}" EIT_schedule="true" '
            'EIT_present_following="true" running_status="running">\n'
            f'    <service_descriptor service_type="0x01" service_provider_name={_q(s["provider"])} '
            f"service_name={_q(s['name'])}/>\n  </service>\n"
            for s in services
        )
        + "</SDT>\n"
    )
    # all the events from the present one to a day ahead; TSDuck's normalization makes the p/f
    # and the schedule sections of them
    eit = ""
    for s in services:
        language = s["sound"][0]["language"]
        eit += (
            f'<EIT type="0" version="0" service_id="{s["service_id"]}" '
            f'transport_stream_id="{tsid}" original_network_id="{onid}">\n'
        )
        for start, minutes, title, text in events(s, now, now + SCHEDULE_HOURS * 3600, tz):
            eit += (
                f'  <event event_id="{(start // 60) & 0xFFFF}" start_time="{_utc(start)}" '
                f'duration="{minutes // 60:02d}:{minutes % 60:02d}:00" '
                f'running_status="{"running" if start <= now < start + minutes * 60 else "not-running"}">\n'
                f'    <short_event_descriptor language_code="{language}"><event_name>{escape(title)}'
                f"</event_name><text>{escape(text)}</text></short_event_descriptor>\n"
                "  </event>\n"
            )
        eit += "</EIT>\n"
    t["eit"] = eit
    cur, change, nxt = local_offset(tz, now)
    t["tot"] = (
        f'<TOT UTC_time="{_utc(now)}">\n'
        "  <local_time_offset_descriptor>"
        f'<region country_code="{lu["country"]}" country_region_id="0" '
        f'local_time_offset="{cur}" time_of_change="{_utc(change)}" '
        f'next_time_offset="{nxt}"/></local_time_offset_descriptor>\n'
        "</TOT>\n"
    )
    t["tdt"] = f'<TDT UTC_time="{_utc(now)}"/>\n'
    return t


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode:
        raise Error(f"{' '.join(cmd[:2])} failed: {(r.stderr or r.stdout).strip()[-400:]}")


def _make_loop(lu, rf, path):
    """The A/V loop: each service's picture and sound, MPEG-2 video and MPEG-1 layer II audio; a
    program each, so each video carries its own PCR."""
    from .tuner_stream import av_inputs, ff_programs

    v, a, dur = lu["video"], lu["audio"], lu["loop_seconds"]
    vb = str(v["bitrate"])
    inputs, maps = av_inputs(rf["services"], v, dur, lambda s: f"{s['lcn']} {s['name']}")
    progs = ff_programs(rf["services"]) if len(rf["services"]) > 1 else []
    _run(
        ["ffmpeg", "-v", "error", "-y"]
        + inputs
        + maps
        + progs
        + ["-c:v", "mpeg2video", "-b:v", vb, "-minrate", vb, "-maxrate", vb, "-bufsize", "1835008"]
        + ["-g", "12", "-bf", "2", "-pix_fmt", "yuv420p", "-flags", "+cgop"]
        + ["-sc_threshold", "1000000000"]
        + ["-c:a", "mp2", "-b:a", str(a["bitrate"]), "-ar", "48000", "-ac", "2"]
        + ["-f", "mpegts", "-mpegts_start_pid", str(FF_PID0), "-pcr_period", "40"]
        + ["-muxrate", str(MUX_RATE), path]
    )


def _tsp(lu, rf, d, loop, out, now):
    """The loop with the tables injected at their rates: PAT and PMT 100 ms, NIT 2 s (at most 10 s),
    SDT 1 s (at most 2 s), EIT 1 s (p/f at most 2 s), TDT 1 s and TOT 2 s (at most 30 s)."""
    services = rf["services"]
    repeat = max(1, -(-SECONDS // lu["loop_seconds"]))
    a = ["tsp", "--bitrate", str(MUX_RATE), "-I", "file", loop, "--repeat", str(repeat)]
    keep, remap = ff_remap(services, lambda i, k: video_pid(i) if k is None else audio_pid(i, k))
    a += ["-P", "filter", "--stuffing"] + [x for p in keep for x in ("--pid", str(p))]
    a += ["--pid", str(NULL_PID), "-P", "continuity", "--fix", "-P", "remap", "--no-psi"] + remap

    def inject(pid, files):
        return ["-P", "inject", "--stuffing", "--pid", f"0x{pid:X}"] + [
            f"{os.path.join(d, n)}={ms}" for n, ms in files
        ]

    a += inject(PAT_PID, [("pat.xml", 100)])
    for i in range(len(services)):
        a += inject(pmt_pid(i), [(f"pmt{i}.xml", 100)])
    a += inject(NIT_PID, [("nit.xml", 2000)])
    a += inject(SDT_PID, [("sdt.xml", 1000)])
    a += ["-P", "inject", "--stuffing", "--pid", f"0x{EIT_PID:X}", "--eit-normalization"]
    a += ["--eit-actual", "--eit-base-date", _utc(now, "%Y/%m/%d %H:%M:%S"), f"{d}/eit.xml=1000"]
    a += inject(TOT_PID, [("tdt.xml", 1000), ("tot.xml", 2000)])
    _run(a + ["-O", "file", out])


def tools():
    """The programs a stream needs that this host lacks."""
    return [t for t in ("tsp", "ffmpeg") if not shutil.which(t)]


NAMES = {
    0x00: "PAT",
    0x02: "PMT",
    0x40: "NIT",
    0x42: "SDT",
    0x4E: "EIT-pf",
    0x70: "TDT",
    0x73: "TOT",
}
NEEDED = ("PAT", "PMT", "NIT", "SDT", "EIT-pf", "EIT-schedule", "TDT", "TOT")


def validate(path):
    """{"ok_crc", "bad_crc", "tables": {name: count}} of the stream's PSI/SI sections, reassembled
    here, with each long section's CRC32 checked independently of TSDuck; EIT schedule sections
    (0x50 to 0x5F) count as "EIT-schedule"."""
    from .tuner_ts import _Sections

    with open(path, "rb") as f:
        data = f.read()
    pids = {PAT_PID, NIT_PID, SDT_PID, EIT_PID, TOT_PID} | {pmt_pid(i) for i in range(8)}
    secs = {p: _Sections() for p in pids}
    for off in range(0, len(data) - PACKET + 1, PACKET):
        p = data[off : off + PACKET]
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if pid in secs:
            secs[pid].add(p)
    ok = bad = 0
    tables = {}
    for s in (x for t in secs.values() for x in t.out):
        if s[1] & 0x80:
            if crc32_mpeg(s) == 0:
                ok += 1
            else:
                bad += 1
        name = "EIT-schedule" if 0x50 <= s[0] <= 0x5F else NAMES.get(s[0], f"0x{s[0]:02X}")
        tables[name] = tables.get(name, 0) + 1
    return {"ok_crc": ok, "bad_crc": bad, "tables": tables}


def make(path, lu, ch, now):
    """Makes the stream of the lineup's physical channel named ch into path for the time now (EIT,
    TDT, TOT), validated; returns path."""
    missing = tools()
    if missing:
        raise Error(f"no {', '.join(missing)} on this host: install TSDuck and ffmpeg")
    rf = channel(lu, ch)
    work = path + ".work"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        for name, body in _tables(lu, rf, now).items():
            with open(os.path.join(work, name + ".xml"), "w", encoding="utf-8") as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tsduck>\n' + body + "</tsduck>\n")
        _make_loop(lu, rf, os.path.join(work, "loop.ts"))
        _tsp(lu, rf, work, os.path.join(work, "loop.ts"), os.path.join(work, "out.ts"), now)
        report = validate(os.path.join(work, "out.ts"))
        missing = [t for t in NEEDED if t not in report["tables"]]
        if report["bad_crc"] or not report["ok_crc"] or missing:
            raise Error(f"rf{ch}: the made stream does not validate (missing {missing}): {report}")
        os.replace(os.path.join(work, "out.ts"), path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return path


def stream_key(lu, ch, now):
    """What the stream depends on: the generator, the lineup (its values and its files' content:
    its key) and the present and following events of the channel's services, so it is made again
    when a present event ends (and its schedule, a day from then, with it)."""
    rf = channel(lu, ch)
    pf = [[e[:2] for e in present_following(s, now, lu["timezone"])] for s in rf["services"]]
    text = json.dumps([VERSION, lu["key"], ch, pf], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def stream_path(cache, lu, ch, now):
    return os.path.join(cache, f"dvb{ch.zfill(2)}-{lu['key'][:8]}-{stream_key(lu, ch, now)}.ts")


def make_cached(cache, lu, ch, now):
    """The path of physical channel ch's stream for now, made into the directory cache if it is not
    there yet; older streams of the channel of the lineup go (other lineups' stay: VMs share the
    cache). Error for a channel that carries nothing."""
    path = stream_path(cache, lu, ch, now)
    if os.path.exists(path):
        return path
    os.makedirs(cache, exist_ok=True)
    make(path + ".tmp", lu, ch, now)
    os.replace(path + ".tmp", path)
    prefix = f"dvb{ch.zfill(2)}-{lu['key'][:8]}-"
    for name in os.listdir(cache):
        if name.startswith(prefix) and os.path.join(cache, name) != path:
            os.unlink(os.path.join(cache, name))
    return path


def time_rewrite(packet, now):
    """The packet with the UTC_time of each whole TDT and TOT section in it set to now (the TOT's
    CRC made again)."""
    return _time_rewrite(packet, now, UTC)
