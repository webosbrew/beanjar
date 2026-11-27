# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What a running VM's TV sends out as audio, for `beanjar status show`'s `audio` object.

Three sources, read when asked:
- the guest's /proc/lgemu/sound_out (guest/kernel/modules/lgemu/snd.c): what the firmware
  selected for its sound outputs, as it reached the hardware (the card's Sndout controls on the
  webOS 5 and later lines, the HAL calls' numbers on the older ones), read over the VM's shell
  port (guestshell.py);
- the TV's own setting (the settings service's sound category: soundOutput, soundOutputDigital),
  read over the same session, reported as `selected` beside the hardware's, so the two can
  disagree;
- the media helper's open sessions (RUN_DIR/media/media-streams, host/helpers/media-helper): the
  elementary streams the guest's audio sinks send, with their caps.

decode() turns the table into one model per line of firmware (docs/research/tv-sound-outputs.md,
"Output selection recorded by the emulation" for the numbers):
- `outputs`: the outputs the hardware has open, each with the codec, channel count and whether the
  stream goes out bit-exact (bypass); `derived` when that comes from the mode and the open stream
  rather than from a value the firmware wrote; None when the table does not tell (MStar, whose
  HAL keeps the connections in user space; the webOS 5 and 6 controls, whose output numbers were
  not checked on a running set);
- `mode`: pcm, auto, forced-ac3, forced-eac3 or passthrough, as far as the hardware's values tell
  (None when they do not: on webOS 7 and later pcm, auto and passThrough write the same values
  while no compressed stream plays);
- `arc`: whether ARC is on (None where the line has no such value);
- `earc`: whether eARC is on (webOS 7 and later's Sndout EARC OnOff, which audiod writes while
  arccontroller has an eARC connection; None elsewhere).
A codec is never guessed: auto's result is reported only where the firmware itself writes it
(the Outputtype controls, a forced mode); a bypass takes the open audio stream's codec, marked
derived, and only when there is exactly one."""

import json
import os
import re
import shlex

SOUND_OUT = "/proc/lgemu/sound_out"
SETTINGS = (
    "luna://com.webos.settingsservice/getSystemSettings",
    {"category": "sound", "keys": ["soundOutput", "soundOutputDigital"]},
)
SEPARATOR = "--- beanjar settings ---"

# webOS 7 and later (libumi's UMI_SNDOUT_OUTPUT_CODEC_T in the Outputtype controls)
UMI_CODECS = {
    1: "pcm",
    2: "ac3",
    3: "aac",
    4: "dts",
    5: "eac3",
    6: "truehd",
    7: "mat",
    8: "dts-hd",
    9: "aac-lc",
    10: "dts-hd-ma",
    11: "dts-x",
}
# libumi's output numbers in Sndout Open, Close and Connect (W22O 13.30.56)
UMI_OUTPUTS = {
    1: "speaker",
    2: "optical",
    4: "lg-sound-sync",
    8: "bt-soundbar",
    0x10: "headphone",
    0x20: "arc",
    0x40: "wisa",
}
# the Outputtype control of each digital output
UMI_OUTPUTTYPE = {
    "optical": "Sndout OPTIC Outputtype",
    "arc": "Sndout ARC Outputtype",
    "earc": "Sndout EARC Outputtype",
}
# the starfish HAL_AUDIO_SPDIF_MODE_T, written by webOS 5 and 6 in Sndout Spdif OutputType
HAL_SPDIF_MODES = {
    1: "pcm",
    2: "auto",
    3: "auto",
    4: "auto",
    5: "auto",
    6: "forced-ac3",
    7: "passthrough",
    8: "passthrough",
}
# Realtek W19P AudioConfig msg_id 2, the low byte (0x100 + mode)
RTK_MODES = {
    1: "pcm",
    2: "auto",
    3: "auto",
    4: "auto",
    5: "auto",
    0xA: "forced-ac3",
    0xB: "forced-eac3",
    0xC: "passthrough",
    0xD: "passthrough",
    0xE: "passthrough",
    0xF: "passthrough",
}
# MStar W18R SPDIF_SetMode's mode byte
MSTAR_MODES = {0: "pcm", 3: "auto", 4: "auto"}
# LX WT1H KADP_AUDIO_SetSpdifOutput (the HAL mode minus one)
LX_MODES = {0: "pcm", 1: "auto"}
FORCED = {"forced-ac3": "ac3", "forced-eac3": "eac3"}

# GStreamer caps names of the streams the sinks send, as codec names
CAPS_CODECS = {
    "audio/x-ac3": "ac3",
    "audio/ac3": "ac3",
    "audio/x-eac3": "eac3",
    "audio/x-dts": "dts",
    "audio/x-true-hd": "truehd",
    "audio/x-raw": "pcm",
    "audio/x-opus": "opus",
    "audio/x-vorbis": "vorbis",
    "audio/x-flac": "flac",
    "audio/x-ac4": "ac4",
}


def command():
    """The guest command: the table, then the TV's setting (luna-send's answer). luna-send gets
    /dev/null: it exits silently, before its reply, when its stdin hangs up (the session's
    ends at once)."""
    url, params = SETTINGS
    return (
        f"cat {SOUND_OUT}; echo {shlex.quote(SEPARATOR)}; "
        f"luna-send -n 1 -w 5000 {url} {shlex.quote(json.dumps(params))} </dev/null"
    )


def split_output(text):
    """(table text, settings text) of command()'s output."""
    table, _, settings = text.partition(SEPARATOR)
    return table, settings


def parse_table(text):
    """/proc/lgemu/sound_out as a dict: generation, writes, open (ints), connected (lists of
    ints), values (name: five ints, in the order of the first write). None when it is not the
    table (no generation line: a guest without lgemu)."""
    out = {"generation": None, "writes": None, "open": [], "connected": [], "values": {}}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("generation "):
            out["generation"] = int(line.split()[1])
        elif line.startswith("writes "):
            out["writes"] = int(line.split()[1])
        elif line.startswith("open="):
            out["open"] = [int(w, 0) for w in line[5:].split()]
        elif line.startswith("connected="):
            out["connected"] = [[int(x, 0) for x in w.split(":")] for w in line[10:].split()]
        elif "=" in line:
            name, _, values = line.rpartition("=")
            try:
                out["values"][name] = [int(w, 0) for w in values.split()]
            except ValueError:
                continue
    return out if out["generation"] is not None else None


def parse_settings(text):
    """The TV's soundOutput and soundOutputDigital from luna-send's answer, None when it gave
    none."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        answer, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    settings = answer.get("settings") if isinstance(answer, dict) else None
    if not isinstance(settings, dict):
        return None
    return {
        "output": settings.get("soundOutput"),
        "digital": settings.get("soundOutputDigital"),
    }


def parse_streams(text):
    """The media helper's open sessions (its --streams file): a list of {port, kind, element,
    caps, codec, channels, rate}."""
    out = []
    for line in text.splitlines():
        head, _, caps = line.partition("\t")
        words = head.split()
        if len(words) < 2 or not words[0].isdigit():
            continue
        out.append(
            {
                "port": int(words[0]),
                "kind": words[1],
                "element": words[2] if len(words) > 2 else "",
                "caps": caps,
                **caps_format(caps),
            }
        )
    return out


def caps_format(caps):
    """codec, channels and rate of a GStreamer caps string (None for what it does not say)."""
    name = caps.split(",", 1)[0].strip()
    codec = CAPS_CODECS.get(name)
    if name == "audio/mpeg":
        version = caps_int(caps, "mpegversion")
        codec = {1: "mp3", 2: "aac", 4: "aac"}.get(version) if version else None
        if version == 1 and caps_int(caps, "layer") in (1, 2):
            codec = "mp2"
    elif codec is None and name.startswith("audio/"):
        codec = name[6:]
    return {"codec": codec, "channels": caps_int(caps, "channels"), "rate": caps_int(caps, "rate")}


def caps_int(caps, field):
    m = re.search(rf"(?:^|[,\s]){re.escape(field)}=\(int\)(\d+)", caps)
    return int(m.group(1)) if m else None


def one_audio_stream(streams):
    """The open audio stream when there is exactly one (with caps), else None."""
    audio = [s for s in streams or () if s["kind"] == "audio" and s["caps"]]
    return audio[0] if len(audio) == 1 else None


def digital_format(mode, streams):
    """An output's codec, channels and bypass from the mode alone (the HAL lines and webOS 5/6):
    pcm, a forced encoding, or a bypass of the one open audio stream; derived, as no value the
    firmware wrote names the codec. auto: nothing (what it does per input codec is the DSP's)."""
    if mode == "pcm":
        return {"codec": "pcm", "channels": None, "bypass": False, "derived": True}
    if mode in FORCED:
        return {"codec": FORCED[mode], "channels": None, "bypass": False, "derived": True}
    if mode == "passthrough":
        stream = one_audio_stream(streams)
        return {
            "codec": stream["codec"] if stream else None,
            "channels": stream["channels"] if stream else None,
            "bypass": True,
            "derived": True,
        }
    return {"codec": None, "channels": None, "bypass": None, "derived": True}


def first(values, name, index=0):
    v = values.get(name)
    return v[index] if v and len(v) > index else None


def on(value):
    return None if value is None else bool(value)


def decode(table, streams=()):
    """The model of a parsed table (parse_table) and the open streams (parse_streams)."""
    model = {"path": None, "outputs": None, "mode": None, "arc": None, "earc": None}
    if table is None:
        return model
    values = table["values"]
    if any(n in values for n in UMI_OUTPUTTYPE.values() if n != "Sndout EARC Outputtype"):
        return decode_umi(table, model)
    if "Sndout Spdif OutputType" in values:
        return decode_spdif_outputtype(table, model, streams)
    if any(n.startswith(("AudioConfig ", "SPDIFO ", "VFE ARC")) for n in values):
        return decode_realtek(values, model, streams)
    if any(
        n in values
        for n in ("SPDIF SetMode", "SetSpdifOutputType", "SetHdmiArcOutputType", "ARC Enable")
    ):
        return decode_mstar(values, model)
    if any(n.startswith("AMST ") for n in values):
        return decode_lx(values, model, streams)
    return model


def decode_umi(table, model):
    """webOS 7 and later: the outputs open (libumi's numbers) and each digital output's
    Outputtype {2, codec, channels, 0, mix}, mix 0 the bit-exact bypass."""
    values = table["values"]
    model["path"] = "outputtype"
    earc = model["earc"] = on(first(values, "Sndout EARC OnOff"))
    model["arc"] = on(first(values, "Sndout ARC OnOff"))
    outputs, modes = [], set()
    for number in table["open"]:
        name = UMI_OUTPUTS.get(number)
        if name == "arc" and earc:
            name = "earc"
        entry = {"name": name, "id": number}
        control = UMI_OUTPUTTYPE.get(name)
        v = values.get(control) if control else None
        if v and len(v) >= 5 and v[1]:
            entry.update(
                codec=UMI_CODECS.get(v[1], str(v[1])),
                channels=v[2] or None,
                bypass=v[4] == 0,
                derived=False,
            )
            if v[4] == 0:
                modes.add("passthrough")
            elif v[1] != 1:
                modes.add("auto")
        elif name == "speaker":
            entry.update(codec="pcm", channels=None, bypass=False, derived=False)
        else:
            entry.update(codec=None, channels=None, bypass=None, derived=False)
        outputs.append(entry)
    model["outputs"] = outputs
    # pcm, auto and passThrough write the same values while no compressed stream plays: the
    # mode shows only once the firmware writes a codec other than PCM or the bypass
    model["mode"] = modes.pop() if len(modes) == 1 else None
    return model


def decode_spdif_outputtype(table, model, streams):
    """webOS 5 and 6: Sndout Spdif OutputType [optical mode, ARC mode], the HAL's SPDIF_MODE_T.
    The output numbers of Sndout Open on these lines were not checked on a running set: the
    outputs stay None."""
    values = table["values"]
    model["path"] = "spdif-outputtype"
    model["mode"] = HAL_SPDIF_MODES.get(first(values, "Sndout Spdif OutputType"))
    model["arc"] = on(first(values, "Sndout ARC OnOff"))
    return model


def decode_realtek(values, model, streams):
    """W19P (libhal_rtd2872): AudioConfig's S/PDIF mode (0x100 + mode, sent for the connected
    digital output), its ARC switch and the S/PDIF port's mute (device 3, also ARC's), the S/PDIF
    out source (3 ARC). The digital output is derived from the port's mute and the source."""
    model["path"] = "realtek"
    raw = first(values, "AudioConfig SPDIF Mode")
    model["mode"] = RTK_MODES.get(raw & 0xFF) if raw is not None else None
    arc = first(values, "AudioConfig ARC")
    model["arc"] = on(arc if arc is not None else first(values, "VFE ARC Enable"))
    mute = first(values, "AudioConfig Mute 3")
    if mute is None:
        return model
    outputs = []
    if mute == 0:
        name = "arc" if first(values, "SPDIFO Source") == 3 else "optical"
        outputs.append({"name": name, "id": None, **digital_format(model["mode"], streams)})
    for entry in outputs:
        entry["derived"] = True
    model["outputs"] = outputs
    return model


def decode_mstar(values, model):
    """W18R (libhal_msd94bcg over Utopia): SPDIF_SetMode's mode (the last of the S/PDIF and the
    ARC call) and the ARC enable. The connections stay in the HAL: no outputs."""
    model["path"] = "mstar"
    model["mode"] = MSTAR_MODES.get(first(values, "SPDIF SetMode"))
    model["arc"] = on(first(values, "ARC Enable"))
    return model


def decode_lx(values, model, streams):
    """WT1H (lg115x): the audio master's S/PDIF mode and the mute of its output 3, which
    external_speaker (optical and ARC as one output) unmutes."""
    model["path"] = "lx"
    model["mode"] = LX_MODES.get(first(values, "AMST SpdifOutput"))
    mute = first(values, "AMST Mute 3")
    if mute is not None:
        model["outputs"] = (
            [{"name": "spdif", "id": 3, **digital_format(model["mode"], streams)}]
            if mute == 0
            else []
        )
    return model


def report(table_text, settings_text, streams_text):
    """status show's audio object from the three sources' text (None for one not read)."""
    table = parse_table(table_text) if table_text is not None else None
    streams = parse_streams(streams_text) if streams_text is not None else []
    out = {
        "selected": parse_settings(settings_text) if settings_text is not None else None,
        **decode(table, streams),
        "streams": [s for s in streams if s["kind"] == "audio"],
        "generation": table["generation"] if table else None,
        "writes": table["writes"] if table else None,
        "values": table["values"] if table else None,
    }
    return out


def read(vm, timeout=10.0):
    """The audio object of a running library VM: the guest's table and setting over its shell
    port, the media helper's streams from its run directory; with `error` when the guest did not
    answer (still starting, a beanjar without the shell relay)."""
    from . import guestshell, qemu
    from .base import Error

    streams_text = None
    try:
        with open(os.path.join(vm.run_dir, qemu.MEDIA_DIR, qemu.MEDIA_STREAMS)) as f:
            streams_text = f.read()
    except OSError:
        pass
    error, text = None, None
    try:
        sock = guestshell.local_socket(vm)
        with sock:
            status, text = guestshell.output(sock, ["sh", "-c", command()], timeout)
        if status == guestshell.LOST:
            error, text = "the VM's shell did not answer", None
    except Error as e:
        error = str(e)
    table_text, settings_text = split_output(text) if text is not None else (None, None)
    out = report(table_text, settings_text, streams_text)
    if error is None and table_text is not None and out["generation"] is None:
        error = f"{SOUND_OUT} is not there (no lgemu module)"
    out["error"] = error
    return out


def lines(audio):
    """status show's text for the audio object."""
    if audio is None:
        return []
    if audio.get("error") and audio.get("generation") is None:
        return [f"audio: {audio['error']}"]
    sel = audio.get("selected") or {}
    out = [
        "audio selected: "
        + (f"{sel.get('output')} ({sel.get('digital')})" if sel else "not known"),
        f"audio mode: {audio.get('mode') or 'not known'}"
        + (f", ARC {'on' if audio['arc'] else 'off'}" if audio.get("arc") is not None else "")
        + (f", eARC {'on' if audio['earc'] else 'off'}" if audio.get("earc") is not None else ""),
    ]
    outputs = audio.get("outputs")
    if outputs is None:
        out.append("audio outputs: not known on this firmware line")
    elif not outputs:
        out.append("audio outputs: no digital output open")
    for o in outputs or ():
        fmt = o.get("codec") or "codec not known"
        if o.get("channels"):
            fmt += f" {o['channels']} ch"
        if o.get("bypass"):
            fmt += ", bypass"
        if o.get("derived"):
            fmt += " (derived)"
        out.append(f"audio out {o.get('name') or o.get('id')}: {fmt}")
    for s in audio.get("streams") or ():
        fmt = s.get("codec") or "?"
        if s.get("channels"):
            fmt += f" {s['channels']} ch"
        if s.get("rate"):
            fmt += f" {s['rate']} Hz"
        out.append(f"audio stream {s['port']}: {fmt} ({s['element']})")
    return out
