# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What the tuner's stream generators share (atsc.py, arib.py, dvb.py): the MPEG transport stream's
packet size and PIDs, CRC-32/MPEG, the lineup's physical channel and service events, ffmpeg's
inputs and PID remapping for a channel's services, and the rewriting of a TDT/TOT's time as
the stream is sent. No generator imports another."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class Error(Exception):
    pass


NULL_PID = 0x1FFF
TOT_PID = 0x14  # the TDT's and TOT's PID (DVB and ARIB)
TABLE_TDT, TABLE_TOT = 0x70, 0x73


PACKET = 188


FF_PID0 = 0x100  # ffmpeg's elementary PIDs, which tsp remaps


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"  # fonts-dejavu-core


JST = timezone(timedelta(hours=9))


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


def channel(lu, name):
    """The lineup's physical channel named name ("7", "C78"); Error if it has none."""
    for rf in lu["rf"]:
        if rf["rf"] == name:
            return rf
    raise Error(f"the lineup has no RF {name} ({', '.join(r['rf'] for r in lu['rf'])})")


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


def jst_time(now, tz=JST):
    """The 40-bit JST_time field of a TDT or TOT (ARIB STD-B10): the MJD, then hours, minutes and
    seconds in BCD; with tz UTC, DVB's UTC_time (EN 300 468), the same layout."""
    t = datetime.fromtimestamp(int(now), tz)
    mjd = (t.date() - date(1858, 11, 17)).days

    def bcd(n):
        return ((n // 10) << 4) | (n % 10)

    return mjd.to_bytes(2, "big") + bytes([bcd(t.hour), bcd(t.minute), bcd(t.second)])


def time_rewrite(packet, now, tz=JST):
    """The packet with the JST_time of each whole TDT and TOT section in it set to now (and the
    TOT's CRC made again); the packet itself when it starts no section on the TOT PID. tz UTC:
    DVB's TDT and TOT, whose UTC_time has the same place and layout."""
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    if pid != TOT_PID or not packet[1] & 0x40 or not packet[3] & 0x10:
        return packet
    at = 4 + (1 + packet[4] if packet[3] & 0x20 else 0)
    if at >= PACKET:
        return packet
    at += 1 + packet[at]  # the pointer field
    out = bytearray(packet)
    stamp = jst_time(now, tz)
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
