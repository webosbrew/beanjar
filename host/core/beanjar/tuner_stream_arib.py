# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A Japanese digital terrestrial (ISDB-T) transport stream with an ARIB BML data broadcast: one
service whose PMT carries MPEG-2 video (also the PCR), AAC (ADTS) audio and a data component, the
DSM-CC data carousel on that component's PID (one DII and the DDBs of module 0, a multipart/mixed
module holding startup.bml), and the SI a set needs around it: NIT with the ISDB-T terrestrial
delivery and TS information descriptors, SDT, EIT present/following, TOT and TDT in JST.

Made like the ATSC streams (tuner_stream.py): ffmpeg encodes the A/V loop, tsp injects the tables
(with --japan: ARIB STD-B24 strings, JST times). TSDuck's own DII model writes the moduleInfo of a
DVB object carousel (BIOP timeouts and taps); ARIB's moduleInfo is descriptors only (STD-B24 vol. 3,
TR-B14), so the DII and DDB sections are made here (TSDuck's XML also caps them at the PSI size)
and tsp only repeats them. Byte layout cross-checked against denpa's
packages/arib synth.ts (MIT) and web-bml's server/decode_ts.ts (MIT); no code taken from either.

The page is EUC-JP with the ARIB BML 1.0 DOCTYPE and `<?bml bml-version="100.0" ?>`, as terrestrial
broadcasts and denpa's test documents are written; whether LG's NetFront would take UTF-8 or no
DOCTYPE is not known, so nothing is left out that a broadcast carries."""

import hashlib
import json
import os
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

from .tuner_stream import PACKET, Error, crc32_mpeg

MUX_RATE = 5_000_000  # bits a second
VERSION = 5  # of the generator: part of a stream's key (stream_key)
SECONDS = 10  # of a made stream, looped
LOOP_SECONDS = 5  # of the A/V loop
FF_PID0 = 0x100  # ffmpeg's elementary PIDs, which tsp remaps

PAT_PID, NIT_PID, SDT_PID, EIT_PID, TOT_PID = 0x00, 0x10, 0x11, 0x12, 0x14
TABLE_TDT, TABLE_TOT = 0x70, 0x73
PMT_PID, VIDEO_PID, AUDIO_PID, DATA_PID = 0x1F0, 0x111, 0x112, 0x740

TAG_VIDEO, TAG_AUDIO, TAG_DATA = 0x00, 0x10, 0x40  # component tags; 0x40 is the entry component
# the video, 720x480 progressive 4:3: video_encode_format 3 (480p) and component_type 0xA1 (480p 4:3)
VIDEO_ENCODE_FORMAT, COMPONENT_VIDEO = 3, 0xA1
DATA_COMPONENT_BML_TERRESTRIAL = 0x000C  # data_component_id, ARIB STD-B10 part 2 annex J
DATA_EVENT_ID = 0xF
DOWNLOAD_ID = (DATA_EVENT_ID << 28) | 1  # top nibble data_event_id; the rest is 1 in practice
TRANSACTION_ID = 0x80000002  # originator 0b10 (network), DII identification 2, update 0
BLOCK_SIZE = 4066  # what broadcasts use: a full DDB section is 4096 bytes
MODULE_ID, MODULE_VERSION = 0x0000, 0
BOUNDARY = "beanjar-bml"

# The service: an ISDB-T multiplex on UHF 27 with one SD service and its BML page. The
# network, TS and area numbers are picked in the terrestrial ranges, not taken from a station.
SERVICE = {
    "uhf": 27,
    "network_id": 0x7FD0,
    "tsid": 0x7FD0,
    "service_id": 0x0400,
    "area_code": 0x5A5,
    "remote_key": 1,
    "network_name": "ビーンジャー",
    "provider": "ビーンジャー",
    "name": "ビーンジャー１",
    "video": {"size": "720x480", "fps": "30000/1001", "bitrate": 2000000},
    "audio": {"bitrate": 128000, "tone_hz": 1000},
    "events": [
        ("データ放送テスト", 60, "BML のデータ放送ページを載せた試験放送です。"),
        ("試験放送", 60, "カラーバーと試験音。"),
    ],
}

PAGE_TEXT = "ビーンジャー データ放送テスト"
PAGE_TEXT_2 = "Beanjar ARIB BML test page"


def startup_bml():
    """The startup document's bytes: EUC-JP, the layout terrestrial pages use (960x540, the body's
    used-key-list), the video full screen and two lines of text on a blue panel over it, colours
    from the default CLUT. A BML document shows the video only through a video object whose data
    names the video component (/00: component tag 0x00 of this service); LG's NetFront reports a
    video object without one as not visible and the set mutes the video while the page shows. The
    object sits in a div: NetFront lays out only the div elements among the body's children."""
    doc = f"""<?xml version="1.0" encoding="EUC-JP" ?>
<!DOCTYPE bml PUBLIC "+//ARIB STD-B24:1999//DTD BML Document for Japanese//JA" "http://www.arib.or.jp/B24/DTD/bml_1_0.dtd">
<?bml bml-version="100.0" ?>
<bml>
<head>
<title>{PAGE_TEXT_2}</title>
<style><![CDATA[
body {{ resolution: 960x540; display-aspect-ratio: 16v9; used-key-list: basic data-button; background-color-index: 8; }}
p {{ font-family: 丸ゴシック; font-size: 36px; line-height: 48px; color-index: 7; }}
]]></style>
</head>
<body>
<div style="left:0px;top:0px;width:960px;height:540px;">
<object type="video/X-arib-mpeg2" data="/{TAG_VIDEO:02x}" streamstatus="play" style="left:0px;top:0px;width:960px;height:540px;"/>
</div>
<div style="left:0px;top:360px;width:960px;height:180px;background-color-index:4;">
<p style="left:48px;top:30px;width:864px;height:48px;">{PAGE_TEXT}</p>
<p style="left:48px;top:96px;width:864px;height:48px;font-size:28px;">{PAGE_TEXT_2}</p>
</div>
</body>
</bml>
"""
    return doc.replace("\n", "\r\n").encode("euc_jp")


RESOURCE_LIST_TYPE = "application/x-arib-resourcelist"


def resource_list(entries):
    """The binary resource list part's body: a count, then one entry a resource (offset of the
    part's header lines in the module, their length, the body's length, a 4+12 bit type field we
    leave 0, the name). LG's NetFront finds a named resource of a multipart module (startup.bml)
    only through this list, and hybridtv-manager's BML_MULTIPART_GetRscInfo reads the same layout."""
    out = bytes([len(entries)])
    for name, offset, header, length in entries:
        name = name.encode()
        out += (
            bytes([13 + len(name)])  # the entry's length after this byte
            + _be(offset, 4)
            + _be(header, 2)
            + _be(length, 4)
            + _be(0, 2)  # type
            + bytes([len(name)])
            + name
        )
    return out


def module():
    """Module 0: a multipart/mixed entity (ARIB STD-B24 vol. 2, 9.3) with a resource list first and
    startup.bml. The boundary is quoted: the sets' NetFront BML browser reads an unquoted one wrong
    (it repeats its first character) and then finds no part. Without the resource list it finds
    the module but not startup.bml in it; both end in "unavailable document"."""
    top = f'Content-Type: multipart/mixed; boundary="{BOUNDARY}"\r\n\r\n'.encode()
    delim = f"--{BOUNDARY}\r\n".encode()
    list_head = f"Content-Type: {RESOURCE_LIST_TYPE}\r\n\r\n".encode()
    doc_head = b"Content-Type: text/X-arib-bml; charset=euc-jp\r\nContent-Location: startup.bml\r\n"
    doc = startup_bml()
    list_len = len(resource_list([("startup.bml", 0, 0, 0)]))
    # the document's header lines start after the list part and the next delimiter
    doc_at = len(top) + len(delim) + len(list_head) + list_len + 2 + len(delim)
    rlist = resource_list([("startup.bml", doc_at, len(doc_head), len(doc))])
    return (
        top
        + delim
        + list_head
        + rlist
        + b"\r\n"
        + delim
        + doc_head
        + b"\r\n"
        + doc
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )


def data_component_info(auto_start=True):
    """additional_arib_bxml_info of the data component (STD-B24 vol. 2, 9.3): transmission format
    00, entry point, auto start (else the d button starts it), resolution 0011 = 960x540; default
    version, independent, style for TV; then data_event_id, event_section_flag, on-demand
    retrieval, not storable, start priority."""
    return bytes([0x33 if auto_start else 0x23, 0x70, (DATA_EVENT_ID << 4) | 0x08, 0xA0])


def _be(n, size):
    return n.to_bytes(size, "big")


def dii_message(mod):
    """The DII's DSM-CC message (the section payload after the 8-byte header): ARIB moduleInfo is
    a Type descriptor (0x01) and no BIOP fields."""
    content_type = b"multipart/mixed"
    info = bytes([0x01, len(content_type)]) + content_type
    body = (
        _be(DOWNLOAD_ID, 4)
        + _be(BLOCK_SIZE, 2)
        + b"\x00\x00"  # windowSize, ackPeriod
        + _be(0, 4)  # tCDownloadWindow
        + _be(0, 4)  # tCDownloadScenario
        + _be(0, 2)  # compatibilityDescriptor: none
        + _be(1, 2)  # numberOfModules
        + _be(MODULE_ID, 2)
        + _be(len(mod), 4)
        + bytes([MODULE_VERSION, len(info)])
        + info
        + _be(0, 2)  # privateDataLength
    )
    header = b"\x11\x03" + _be(0x1002, 2) + _be(TRANSACTION_ID, 4) + b"\xff\x00" + _be(len(body), 2)
    return header + body


def ddb_messages(mod):
    """The DDB messages of the module, one a block (each its own section, section_number = block)."""
    out = []
    for n, at in enumerate(range(0, len(mod), BLOCK_SIZE)):
        block = mod[at : at + BLOCK_SIZE]
        out.append(
            b"\x11\x03"
            + _be(0x1003, 2)
            + _be(DOWNLOAD_ID, 4)
            + b"\xff\x00"
            + _be(len(block) + 6, 2)
            + _be(MODULE_ID, 2)
            + bytes([MODULE_VERSION, 0xFF])
            + _be(n, 2)
            + block
        )
    return out


def uhf_center_hz(ch):
    """The Japanese UHF channel's centre frequency (13 to 62: 473 + 6 (ch - 13) + 1/7 MHz)."""
    if not 13 <= ch <= 62:
        raise Error(f"no Japanese UHF channel {ch} (13 to 62)")
    return (473 + 6 * (ch - 13)) * 1000000 + 142857


JST = timezone(timedelta(hours=9))


def _jst(t, fmt="%Y-%m-%d %H:%M:%S"):
    """The time string TSDuck takes for an ISDB time field: with --japan, XML times are JST, which
    is also what the field carries (ARIB STD-B10)."""
    return datetime.fromtimestamp(int(t), JST).strftime(fmt)


def events(now):
    """(start, minutes, title, text) of the present and following events: the cycle repeated from
    local (JST) midnight."""
    day = datetime.fromtimestamp(now, JST).replace(hour=0, minute=0, second=0, microsecond=0)
    t, cycle, out, i = day.timestamp(), SERVICE["events"], [], 0
    while len(out) < 2:
        title, minutes, text = cycle[i % len(cycle)]
        if t + minutes * 60 > now:
            out.append((int(t), minutes, title, text))
        t += minutes * 60
        i += 1
    return out


def _q(s):
    return quoteattr(str(s))


def _hex(data):
    return data.hex().upper()


def _tables(now):
    """{name: TSDuck XML body} of the stream's tables at now."""
    s, t = SERVICE, {}
    sid, tsid, onid = s["service_id"], s["tsid"], s["network_id"]
    t["pat"] = (
        f'<PAT version="0" transport_stream_id="{tsid}" network_PID="0x{NIT_PID:X}">\n'
        f'  <service service_id="{sid}" program_map_PID="0x{PMT_PID:X}"/>\n</PAT>\n'
    )
    t["pmt"] = (
        f'<PMT version="0" service_id="{sid}" PCR_PID="0x{VIDEO_PID:X}">\n'
        f'  <component stream_type="0x02" elementary_PID="0x{VIDEO_PID:X}">\n'
        f'    <stream_identifier_descriptor component_tag="0x{TAG_VIDEO:02X}"/>\n'
        # ARIB STD-B10: the video's format, which a set takes before the decoder's; 3 is 480p
        '    <video_decode_control_descriptor still_picture="false" sequence_end_code="false" '
        f'video_encode_format="{VIDEO_ENCODE_FORMAT}"/>\n  </component>\n'
        f'  <component stream_type="0x0F" elementary_PID="0x{AUDIO_PID:X}">\n'
        f'    <stream_identifier_descriptor component_tag="0x{TAG_AUDIO:02X}"/>\n  </component>\n'
        f'  <component stream_type="0x0D" elementary_PID="0x{DATA_PID:X}">\n'
        f'    <stream_identifier_descriptor component_tag="0x{TAG_DATA:02X}"/>\n'
        f'    <data_component_descriptor data_component_id="0x{DATA_COMPONENT_BML_TERRESTRIAL:04X}">'
        f"<additional_data_component_info>{_hex(data_component_info())}</additional_data_component_info>"
        "</data_component_descriptor>\n  </component>\n</PMT>\n"
    )
    t["nit"] = (
        f'<NIT version="0" network_id="{onid}">\n'
        f"  <network_name_descriptor network_name={_q(s['network_name'])}/>\n"
        # terrestrial (broadcasting identifier 3), standard broadcast
        '  <system_management_descriptor broadcasting_flag="0" broadcasting_identifier="3" '
        'additional_broadcasting_identification="0x01"/>\n'
        f'  <transport_stream transport_stream_id="{tsid}" original_network_id="{onid}">\n'
        f'    <ISDB_terrestrial_delivery_system_descriptor area_code="0x{s["area_code"]:03X}" '
        'guard_interval="1/8" transmission_mode="mode3">'
        f'<frequency value="{uhf_center_hz(s["uhf"])}"/></ISDB_terrestrial_delivery_system_descriptor>\n'
        f'    <TS_information_descriptor remote_control_key_id="{s["remote_key"]}" ts_name={_q(s["name"])}>'
        f'<transmission_type transmission_type_info="0x0F"><service id="{sid}"/></transmission_type>'
        "</TS_information_descriptor>\n"
        f'    <service_list_descriptor><service service_id="{sid}" service_type="0x01"/></service_list_descriptor>\n'
        "  </transport_stream>\n</NIT>\n"
    )
    t["sdt"] = (
        f'<SDT version="0" transport_stream_id="{tsid}" original_network_id="{onid}">\n'
        f'  <service service_id="{sid}" EIT_present_following="true" running_status="running">\n'
        f'    <service_descriptor service_type="0x01" service_provider_name={_q(s["provider"])} '
        f"service_name={_q(s['name'])}/>\n  </service>\n</SDT>\n"
    )
    eit = f'<EIT type="pf" version="0" service_id="{sid}" transport_stream_id="{tsid}" original_network_id="{onid}">\n'
    for k, (start, minutes, title, text) in enumerate(events(now)):
        eit += (
            f'  <event event_id="{(start // 60) & 0xFFFF}" start_time="{_jst(start)}" '
            f'duration="{minutes // 60:02d}:{minutes % 60:02d}:00" '
            f'running_status="{"running" if k == 0 else "not-running"}">\n'
            f'    <short_event_descriptor language_code="jpn"><event_name>{escape(title)}</event_name>'
            f"<text>{escape(text)}</text></short_event_descriptor>\n"
            # the event's video and audio, as broadcasts describe them (STD-B10 6.2.3, 6.2.26)
            f'    <component_descriptor stream_content="1" component_type="0x{COMPONENT_VIDEO:02X}" '
            f'component_tag="0x{TAG_VIDEO:02X}" language_code="jpn"/>\n'
            f'    <audio_component_descriptor component_type="0x03" component_tag="0x{TAG_AUDIO:02X}" '
            'stream_type="0x0F" quality_indicator="1" sampling_rate="7" ISO_639_language_code="jpn"/>\n'
            "  </event>\n"
        )
    t["eit"] = eit + "</EIT>\n"
    t["tot"] = f'<TOT UTC_time="{_jst(now)}"/>\n'
    t["tdt"] = f'<TDT UTC_time="{_jst(now)}"/>\n'
    return t


def dsmcc_section(table_id, table_id_ext, version, number, last, payload):
    """A DSM-CC section (ISO/IEC 13818-6 9.2.2): section syntax 1, private indicator 0, CRC_32.
    Made here rather than by TSDuck, whose XML allows such a long section only 1024 bytes (the PSI
    limit); a DDB with ARIB's 4066-byte blocks needs up to 4096."""
    body = _be(table_id_ext, 2) + bytes([0xC1 | (version & 0x1F) << 1, number, last]) + payload
    head = bytes([table_id]) + _be(0xB000 | (len(body) + 4), 2)
    sec = head + body
    return sec + _be(crc32_mpeg(sec), 4)


def carousel():
    """{name: sections} of the data PID: the DII and the DDBs of module 0."""
    mod = module()
    ddb = ddb_messages(mod)
    return {
        "dii": dsmcc_section(0x3B, TRANSACTION_ID & 0xFFFF, 0, 0, 0, dii_message(mod)),
        "ddb": b"".join(
            dsmcc_section(0x3C, MODULE_ID, MODULE_VERSION, n, len(ddb) - 1, m)
            for n, m in enumerate(ddb)
        ),
    }


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode:
        raise Error(f"{' '.join(cmd[:2])} failed: {(r.stderr or r.stdout).strip()[-400:]}")


def _make_loop(path):
    """The A/V loop: colour bars labelled with the service, MPEG-2 video and AAC (ADTS) audio."""
    v, a = SERVICE["video"], SERVICE["audio"]
    vb = str(v["bitrate"])
    _run(
        ["ffmpeg", "-v", "error", "-y"]
        + ["-f", "lavfi", "-i", f"smptebars=s={v['size']}:r={v['fps']}:d={LOOP_SECONDS}"]
        + [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={a['tone_hz']}:sample_rate=48000:duration={LOOP_SECONDS}",
        ]
        + ["-map", "0:v", "-map", "1:a"]
        + ["-c:v", "mpeg2video", "-b:v", vb, "-minrate", vb, "-maxrate", vb, "-bufsize", "1835008"]
        + [
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
        ]
        + ["-c:a", "aac", "-b:a", str(a["bitrate"]), "-ar", "48000", "-ac", "2"]
        + ["-f", "mpegts", "-mpegts_start_pid", str(FF_PID0), "-pcr_period", "40"]
        + ["-muxrate", str(MUX_RATE), path]
    )


def _tsp(d, loop, out, now):
    """The loop with the tables injected at their rates: PAT/PMT 100 ms, NIT and SDT 1 s (ARIB:
    at most 10 s and 2 s), EIT p/f 500 ms, TOT and TDT 1 s (at most 30 s), DII 500 ms, DDB 250 ms."""
    repeat = max(1, -(-SECONDS // LOOP_SECONDS))
    a = ["tsp", "--japan", "--bitrate", str(MUX_RATE), "-I", "file", loop, "--repeat", str(repeat)]
    a += ["-P", "filter", "--stuffing", "--pid", str(FF_PID0), "--pid", str(FF_PID0 + 1)]
    a += ["--pid", "0x1FFF", "-P", "continuity", "--fix"]
    a += ["-P", "remap", "--no-psi", f"{FF_PID0}={VIDEO_PID}", f"{FF_PID0 + 1}={AUDIO_PID}"]

    def inject(pid, files):
        return ["-P", "inject", "--stuffing", "--pid", f"0x{pid:X}"] + [
            f"{os.path.join(d, n)}={ms}" for n, ms in files
        ]

    a += inject(PAT_PID, [("pat.xml", 100)])
    a += inject(PMT_PID, [("pmt.xml", 100)])
    a += inject(NIT_PID, [("nit.xml", 1000)])
    a += inject(SDT_PID, [("sdt.xml", 1000)])
    # TSDuck puts both events in one section; ARIB, like DVB, has present in section 0 and
    # following in section 1, which the normalisation makes
    a += ["-P", "inject", "--stuffing", "--pid", f"0x{EIT_PID:X}", "--eit-normalization"]
    a += ["--eit-actual-pf", "--eit-base-date", _jst(now, "%Y/%m/%d %H:%M:%S"), f"{d}/eit.xml=500"]
    a += inject(TOT_PID, [("tot.xml", 1000), ("tdt.xml", 1000)])
    a += inject(DATA_PID, [("dii.bin", 500), ("ddb.bin", 250)])
    _run(a + ["-O", "file", out])


def tools():
    """The programs a stream needs that this host lacks."""
    return [t for t in ("tsp", "ffmpeg") if not shutil.which(t)]


def make(path, now):
    """Makes the stream into path for the time now (EIT p/f, TOT, TDT); returns path."""
    missing = tools()
    if missing:
        raise Error(f"no {', '.join(missing)} on this host: install TSDuck and ffmpeg")
    work = path + ".work"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        for name, body in _tables(now).items():
            with open(os.path.join(work, name + ".xml"), "w", encoding="utf-8") as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tsduck>\n' + body + "</tsduck>\n")
        for name, sections in carousel().items():
            with open(os.path.join(work, name + ".bin"), "wb") as f:
                f.write(sections)
        _make_loop(os.path.join(work, "loop.ts"))
        _tsp(work, os.path.join(work, "loop.ts"), os.path.join(work, "out.ts"), now)
        os.replace(os.path.join(work, "out.ts"), path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return path


def stream_key(now):
    """What the stream depends on: the generator, the service, its page and the present and
    following events (EIT p/f), so it is made again when the present event ends."""
    text = json.dumps(
        [VERSION, SERVICE, startup_bml().hex(), [e[:2] for e in events(now)]], sort_keys=True
    )
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def stream_path(cache, ch, now):
    return os.path.join(cache, f"isdbt{ch:02d}-{stream_key(now)}.ts")


def make_cached(cache, ch, now):
    """The path of physical channel ch's stream for now, made into the directory cache if it is not
    there yet; older streams of the channel go. Error for a channel that carries nothing."""
    if ch != SERVICE["uhf"]:
        raise Error(f"the lineup has no physical channel {ch} ({SERVICE['uhf']})")
    path = stream_path(cache, ch, now)
    if os.path.exists(path):
        return path
    os.makedirs(cache, exist_ok=True)
    make(path + ".tmp", now)
    os.replace(path + ".tmp", path)
    for name in os.listdir(cache):
        if name.startswith(f"isdbt{ch:02d}-") and os.path.join(cache, name) != path:
            os.unlink(os.path.join(cache, name))
    return path


def jst_time(now):
    """The 40-bit JST_time field of a TDT or TOT (ARIB STD-B10): the MJD, then hours, minutes and
    seconds in BCD."""
    t = datetime.fromtimestamp(int(now), JST)
    mjd = (t.date() - date(1858, 11, 17)).days

    def bcd(n):
        return ((n // 10) << 4) | (n % 10)

    return mjd.to_bytes(2, "big") + bytes([bcd(t.hour), bcd(t.minute), bcd(t.second)])


def time_rewrite(packet, now):
    """The packet with the JST_time of each whole TDT and TOT section in it set to now (and the
    TOT's CRC made again); the packet itself when it starts no section on the TOT PID."""
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    if pid != TOT_PID or not packet[1] & 0x40 or not packet[3] & 0x10:
        return packet
    at = 4 + (1 + packet[4] if packet[3] & 0x20 else 0)
    if at >= PACKET:
        return packet
    at += 1 + packet[at]  # the pointer field
    out = bytearray(packet)
    stamp = jst_time(now)
    while at + 3 <= PACKET and out[at] != 0xFF:
        n = 3 + (((out[at + 1] & 0xF) << 8) | out[at + 2])
        if at + n > PACKET:
            break
        if out[at] == TABLE_TDT and n >= 8:
            out[at + 3 : at + 8] = stamp
        elif out[at] == TABLE_TOT and n >= 14:
            out[at + 3 : at + 8] = stamp
            out[at + n - 4 : at + n] = crc32_mpeg(bytes(out[at : at + n - 4])).to_bytes(4, "big")
        at += n
    return bytes(out)
