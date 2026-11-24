#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""status show's audio object: /proc/lgemu/sound_out, the TV's setting and the media helper's
streams decoded per line of firmware, from the values recorded on running sets
(docs/research/tv-sound-outputs.md, "Output selection recorded by the emulation")."""

from beanjar import soundout

AC3_STREAM = (
    "1 audio adecsink0\taudio/x-ac3, framed=(boolean)true, rate=(int)48000, channels=(int)6\n"
)
VIDEO_STREAM = "0 video vdecsink0\tvideo/x-h264, width=(int)1920\n"


def table(*lines, generation=3, writes=9, open_="", connected=""):
    return "\n".join(
        [f"generation {generation}", f"writes {writes}", f"open={open_}", f"connected={connected}"]
        + [f"{n}={' '.join(str(x) for x in v)}" for n, v in lines]
    )


def five(*v):
    return list(v) + [0] * (5 - len(v))


def model(text, streams=""):
    return soundout.decode(soundout.parse_table(text), soundout.parse_streams(streams))


def test_parse_table():
    t = soundout.parse_table(
        table(
            ("Sndout OPTIC Outputtype", five(2, 2, 0, 0, 1)),
            ("Sndout Optic LightOnOff", five(1)),
            open_="2",
            connected="2:16:5 2:16:6",
        )
    )
    assert t["generation"] == 3 and t["writes"] == 9
    assert t["open"] == [2] and t["connected"] == [[2, 16, 5], [2, 16, 6]]
    assert list(t["values"]) == ["Sndout OPTIC Outputtype", "Sndout Optic LightOnOff"]
    assert t["values"]["Sndout OPTIC Outputtype"] == [2, 2, 0, 0, 1]
    assert soundout.parse_table("cat: /proc/lgemu/sound_out: No such file\n") is None


UMI_BOOT = [
    (n, five(2, 1, 0, 0, 1))
    for n in (
        "Sndout OPTIC Outputtype",
        "Sndout ARC Outputtype",
        "Sndout EARC Outputtype",
        "Sndout WOWCAST Outputtype",
    )
] + [("Sndout Optic LightOnOff", five(1))]


def test_webos7_boot_speaker():
    """W22O 13.30.56 at boot (tv_external_speaker): output 1 open, every Outputtype PCM."""
    m = model(table(*UMI_BOOT, open_="1"))
    assert m["path"] == "outputtype"
    assert m["outputs"] == [
        {
            "name": "speaker",
            "id": 1,
            "codec": "pcm",
            "channels": None,
            "bypass": False,
            "derived": False,
        }
    ]
    assert m["mode"] is None  # PCM values are what every mode writes without a compressed stream


def optical(v):
    return table(
        ("Sndout OPTIC Outputtype", v), *UMI_BOOT[1:], open_="2", connected="2:16:5 2:16:6 2:16:7"
    )


def test_webos7_optical_modes_with_an_ac3_clip():
    """external_optical with an AC-3 clip: pcm 2 1 0 0 1, auto 2 2 0 0 1, passThrough 2 2 0 0 0."""
    for v, mode, codec, bypass in (
        (five(2, 1, 0, 0, 1), None, "pcm", False),
        (five(2, 2, 0, 0, 1), "auto", "ac3", False),
        (five(2, 2, 0, 0, 0), "passthrough", "ac3", True),
    ):
        m = model(optical(v), AC3_STREAM)
        out = m["outputs"][0]
        assert (m["mode"], out["name"], out["id"]) == (mode, "optical", 2)
        assert (out["codec"], out["bypass"], out["derived"]) == (codec, bypass, False)
        assert out["channels"] is None  # the firmware wrote 0


def test_webos7_codecs_and_channels():
    m = model(optical(five(2, 5, 6, 0, 0)))
    assert (m["outputs"][0]["codec"], m["outputs"][0]["channels"]) == ("eac3", 6)
    assert model(optical(five(2, 4, 0, 0, 1)))["outputs"][0]["codec"] == "dts"
    assert model(optical(five(2, 99, 0, 0, 1)))["outputs"][0]["codec"] == "99"


def test_webos7_arc_and_earc():
    """external_arc: output 0x20 open; ARC OnOff 0 without a sound bar; eARC on: EARC's values."""
    arc = table(*UMI_BOOT, ("Sndout ARC OnOff", five(0)), open_="32", connected="32:16:5")
    m = model(arc)
    assert m["arc"] is False
    assert (m["outputs"][0]["name"], m["outputs"][0]["codec"]) == ("arc", "pcm")
    earc = table(
        ("Sndout EARC Outputtype", five(2, 7, 8, 0, 0)),
        *UMI_BOOT[:2],
        ("Sndout EARC OnOff", five(1)),
        open_="32",
    )
    m = model(earc)
    out = m["outputs"][0]
    assert (out["name"], out["codec"], out["channels"], out["bypass"]) == ("earc", "mat", 8, True)
    assert m["earc"] is True and model(arc)["earc"] is None
    # W22H 43.22.73 with an eARC bar, an AC-3 clip, passThrough: E-AC-3 encoded, as in auto
    w22h = table(
        ("Sndout EARC Outputtype", five(2, 5, 0, 0, 1)),
        *UMI_BOOT[:2],
        ("Sndout EARC OnOff", five(1)),
        open_="32",
    )
    m = model(w22h)
    assert (m["outputs"][0]["name"], m["outputs"][0]["codec"], m["mode"]) == (
        "earc",
        "eac3",
        "auto",
    )
    text = soundout.lines(dict(m, selected={"output": "external_arc", "digital": "passThrough"}))
    assert "audio mode: auto, eARC on" in text and "audio out earc: eac3" in text


def test_webos5_spdif_outputtype():
    """webOS 5 and 6: Sndout Spdif OutputType [optical, ARC] as the HAL's SPDIF_MODE_T; the
    output numbers are not decoded."""
    for optic, mode in (
        (1, "pcm"),
        (2, "auto"),
        (3, "auto"),
        (6, "forced-ac3"),
        (7, "passthrough"),
        (8, "passthrough"),
    ):
        m = model(table(("Sndout Spdif OutputType", five(optic, 15)), open_="2"))
        assert (m["path"], m["mode"], m["outputs"]) == ("spdif-outputtype", mode, None)


def test_realtek_w19p():
    """W19P: optical 0x101 / 0x102 / 0x10c for pcm / auto / passThrough; ARC by the source 3."""

    def rtk(mode, source=None, mute=0):
        rows = [("AudioConfig Mute 3", five(mute)), ("AudioConfig SPDIF Mode", five(mode))]
        if source is not None:
            rows += [
                ("SPDIFO Source", five(source)),
                ("AudioConfig ARC", five(0)),
                ("VFE ARC Enable", five(0)),
            ]
        return table(*rows)

    m = model(rtk(257))
    assert (m["path"], m["mode"]) == ("realtek", "pcm")
    assert m["outputs"] == [
        {
            "name": "optical",
            "id": None,
            "codec": "pcm",
            "channels": None,
            "bypass": False,
            "derived": True,
        }
    ]
    m = model(rtk(258), AC3_STREAM)
    assert m["mode"] == "auto"
    assert (m["outputs"][0]["codec"], m["outputs"][0]["bypass"]) == (None, None)  # auto: not known
    m = model(rtk(268), AC3_STREAM + VIDEO_STREAM)
    out = m["outputs"][0]
    assert (m["mode"], out["codec"], out["channels"], out["bypass"], out["derived"]) == (
        "passthrough",
        "ac3",
        6,
        True,
        True,
    )
    assert model(rtk(268))["outputs"][0]["codec"] is None  # no stream open
    assert model(rtk(0x10A))["outputs"][0]["codec"] == "ac3"  # forced AC-3
    m = model(rtk(257, source=3))
    assert (m["outputs"][0]["name"], m["arc"]) == ("arc", False)
    assert model(rtk(257, mute=1))["outputs"] == []


def test_two_audio_streams_give_no_bypass_codec():
    two = AC3_STREAM + "2 audio adecsink1\taudio/mpeg, mpegversion=(int)4, channels=(int)2\n"
    m = model(table(("AudioConfig Mute 3", five(0)), ("AudioConfig SPDIF Mode", five(0x10C))), two)
    assert m["outputs"][0]["codec"] is None


def test_mstar_w18r():
    """W18R: SPDIF SetMode 0 pcm, 4 non-PCM (auto); the outputs stay in the HAL."""
    boot = table(("SPDIF SetMode", five(0)), ("SetSpdifOutputType", five(0)))
    m = model(boot)
    assert (m["path"], m["mode"], m["outputs"], m["arc"]) == ("mstar", "pcm", None, None)
    auto = table(
        ("SPDIF SetMode", five(4)),
        ("SetSpdifOutputType", five(0)),
        ("ARC Enable", five(0)),
        ("SetAudioParam2 161", five(0)),
        ("SetAudioParam2 160", five(0)),
        ("SetHdmiArcOutputType", five(1)),
    )
    m = model(auto)
    assert (m["mode"], m["outputs"], m["arc"]) == ("auto", None, False)
    assert model(table(("SPDIF SetMode", five(3))))["mode"] == "auto"


def test_lx_wt1h():
    """WT1H: external_speaker unmutes the master's output 3; SpdifOutput 1 auto, 0 pcm."""
    m = model(table(("AMST Mute 3", five(0)), ("AMST SpdifOutput", five(1))))
    assert (m["path"], m["mode"]) == ("lx", "auto")
    assert (m["outputs"][0]["name"], m["outputs"][0]["codec"]) == ("spdif", None)
    m = model(table(("AMST Mute 3", five(0)), ("AMST SpdifOutput", five(0))))
    assert (m["mode"], m["outputs"][0]["codec"], m["outputs"][0]["derived"]) == ("pcm", "pcm", True)
    assert model(table(("AMST Mute 3", five(1)), ("AMST SpdifOutput", five(0))))["outputs"] == []
    assert model(table(("AMST SpdifOutput", five(0))))["outputs"] is None


def test_streams_and_caps():
    streams = soundout.parse_streams(AC3_STREAM + VIDEO_STREAM + "junk\n3 audio x\t\n")
    assert [s["port"] for s in streams] == [1, 0, 3]
    assert (streams[0]["codec"], streams[0]["channels"], streams[0]["rate"]) == ("ac3", 6, 48000)
    assert soundout.caps_format("audio/mpeg, mpegversion=(int)4, rate=(int)44100")["codec"] == "aac"
    assert soundout.caps_format("audio/mpeg, mpegversion=(int)1, layer=(int)3")["codec"] == "mp3"
    assert soundout.caps_format("audio/x-eac3, channels=(int)8")["channels"] == 8
    assert soundout.caps_format("audio/x-wma")["codec"] == "x-wma"


def test_settings_answer():
    text = '{\n    "subscribed": false,\n    "settings": {"soundOutput": "external_optical", "soundOutputDigital": "passThrough"},\n    "returnValue": true\n}\n'
    assert soundout.parse_settings(text) == {"output": "external_optical", "digital": "passThrough"}
    assert soundout.parse_settings('{"returnValue": false, "errorText": "x"}') is None
    assert soundout.parse_settings("luna-send: not found") is None


def test_report_and_text():
    out = soundout.command()
    assert soundout.SOUND_OUT in out and "getSystemSettings" in out
    guest = (
        optical(five(2, 2, 0, 0, 0))
        + "\n"
        + soundout.SEPARATOR
        + '\n{"settings": {"soundOutput": "external_optical", "soundOutputDigital": "passThrough"}}'
    )
    tbl, settings = soundout.split_output(guest)
    audio = soundout.report(tbl, settings, AC3_STREAM + VIDEO_STREAM)
    assert audio["selected"] == {"output": "external_optical", "digital": "passThrough"}
    assert audio["mode"] == "passthrough" and audio["generation"] == 3
    assert [s["port"] for s in audio["streams"]] == [1]
    text = soundout.lines(dict(audio, error=None))
    assert "audio selected: external_optical (passThrough)" in text
    assert "audio out optical: ac3, bypass" in text
    assert "audio stream 1: ac3 6 ch 48000 Hz (adecsink0)" in text
    assert soundout.lines({"error": "x is not running", "generation": None}) == [
        "audio: x is not running"
    ]
    empty = soundout.report(None, None, None)
    assert (empty["outputs"], empty["selected"], empty["streams"]) == (None, None, [])
