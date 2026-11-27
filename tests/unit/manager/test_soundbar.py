#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""io soundbar: the formats' CTA-861 short audio descriptors (checked against edid-decode, which
reads them from an EDID's CTA-861 audio data block), the parameter lgemu's cec.c takes, and the
guest's answer as the command reports it."""

import shutil
import subprocess

import pytest

from beanjar.devices import soundbar
from beanjar.base import Error


def edid_with_sads(raw):
    """A 256-byte EDID: a minimal base block and a CTA-861 extension whose only data block is an
    audio data block with raw (the descriptors), checksums right."""
    base = bytearray(128)
    base[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    base[8:10] = b"\x1e\x6d"  # GSM, as a real set's
    base[18:20] = b"\x01\x03"  # EDID 1.3
    base[126] = 1  # one extension
    base[127] = -sum(base[:127]) & 0xFF
    ext = bytearray(128)
    block = bytes([0x20 | len(raw)]) + raw  # tag 1 audio
    ext[0:4] = bytes([0x02, 0x03, 4 + len(block), 0x40])  # basic audio supported
    ext[4 : 4 + len(block)] = block
    ext[127] = -sum(ext[:127]) & 0xFF
    return bytes(base + ext)


def test_descriptor_bytes():
    """CTA-861 `struct cea_sad` per format: byte 0 code << 3 | channels - 1."""
    for name, code, channels in (
        ("lpcm", 1, 2),
        ("ac3", 2, 6),
        ("eac3", 10, 6),
        ("eac3-atmos", 10, 8),
        ("dts", 7, 6),
        ("aac", 6, 6),
    ):
        sad = soundbar.FORMATS[name][1]
        assert len(sad) == 3
        assert sad[0] >> 3 == code and (sad[0] & 7) + 1 == channels, name
        assert not sad[0] & 0x80 and sad[1] and not sad[1] & 0x80, name
    assert soundbar.FORMATS["eac3-atmos"][1][2] & 1  # Joint Object Coding
    assert not soundbar.FORMATS["eac3"][1][2] & 1


@pytest.mark.skipif(not shutil.which("edid-decode"), reason="edid-decode is not installed")
def test_edid_decode_reads_the_descriptors():
    names = list(soundbar.FORMATS)
    names.remove("eac3")
    raw = soundbar.sads(names) + soundbar.FORMATS["eac3"][1]
    out = subprocess.run(
        ["edid-decode"], input=edid_with_sads(raw), capture_output=True, check=False
    ).stdout.decode()
    audio = out[out.index("Audio Data Block") :]
    for text in (
        "Linear PCM:\n      Max channels: 2\n      Supported sample rates (kHz): 48 44.1 32\n"
        "      Supported sample sizes (bits): 24 20 16",
        "AC-3:\n      Max channels: 6\n      Supported sample rates (kHz): 48 44.1 32\n"
        "      Maximum bit rate: 640 kb/s",
        "Enhanced AC-3 (DD+):\n      Max channels: 8\n      Supported sample rates (kHz): 48 44.1 32\n"
        "      Supports Joint Object Coding",
        "Enhanced AC-3 (DD+):\n      Max channels: 6\n      Supported sample rates (kHz): 48 44.1 32\n",
        "DTS:\n      Max channels: 6\n      Supported sample rates (kHz): 48 44.1\n"
        "      Maximum bit rate: 1536 kb/s",
        "AAC LC:\n      Max channels: 6\n      Supported sample rates (kHz): 48 44.1 32\n"
        "      Maximum bit rate: 640 kb/s",
    ):
        assert text in audio, audio
    # the dummy base block fails conformity; nothing about the audio block may
    out = subprocess.run(
        ["edid-decode", "-c"], input=edid_with_sads(raw), capture_output=True, check=False
    ).stdout.decode()
    problems = out[out.index("Warnings:") :] if "Warnings:" in out else ""
    assert "Audio" not in problems and "SAD" not in problems, problems


def test_parse_formats():
    assert soundbar.parse_formats("AC3, eac3,dts") == ["ac3", "eac3", "dts"]
    for bad, why in (
        ("", "no formats"),
        ("ac3,mp3", "mp3"),
        ("ac3,ac3", "twice"),
        ("eac3,eac3-atmos", "one of"),
    ):
        with pytest.raises(ValueError, match=why):
            soundbar.parse_formats(bad)


def test_param_value_and_back():
    value = soundbar.param_value(2, ["ac3", "eac3", "dts"])
    assert value == "2,150750550700" + "3d06c0"
    port, hex_ = soundbar.parse_param(value + "\n")
    assert port == 2 and soundbar.format_names(hex_) == ["ac3", "eac3", "dts"]
    assert soundbar.format_names("150750aabbcc") == ["ac3", "aabbcc"]
    assert soundbar.parse_param("\n") is None
    with pytest.raises(Error):
        soundbar.param_value(0, ["ac3"])


def guest_output(
    param,
    status,
    port='{"configs":{"system.arcPort":2},"returnValue":true}',
    state=None,
    profile='{"returnValue":false}',
    earc_param=None,
    earc_status=None,
    arc_state='{"returnValue":false}',
):
    """command()'s output: the CEC side (param None: no bus), the eARC side (earc_status None:
    no /dev/earc0), then the luna answers."""
    sep = soundbar.SEPARATOR + "\n"
    state = state or (
        '{"arcProfile":"EAC3","cecReady":true,"returnValue":true,'
        '"arcSADs":"150750550700","arcActivation":true}'
    )
    cec = f"{param}\n{sep}{status}\n" if param is not None else f"nocec\n{sep}"
    earc = f"{earc_param or ''}\n{sep}{earc_status}\n" if earc_status else f"absent\n{sep}"
    return f"{cec}{sep}{earc}{sep}{port}\n{sep}{profile}\n{sep}{state}\n{sep}{arc_state}\n"


def test_parse_attached():
    data = soundbar.parse(
        guest_output("2,1507505507003d06c0", "arc=1 system_audio=1 volume=25 mute=0")
    )
    assert data == {
        "cec": True,
        "earc": False,
        "attached": True,
        "link": "arc",
        "port": 2,
        "sads": "1507505507003d06c0",
        "formats": ["ac3", "eac3", "dts"],
        "status": {"arc": True, "system_audio": True, "volume": 25, "mute": False},
        "earc_status": None,
        "arc_port": 2,
        "tv": {
            "cecReady": True,
            "arcActivation": True,
            "arcSADs": "150750550700",
            "arcProfile": "EAC3",
        },
    }


def test_parse_absent_and_no_bus():
    data = soundbar.parse(guest_output("", "absent", state='{"returnValue":false}'))
    assert not data["attached"] and data["status"] is None and data["tv"] is None
    assert data["cec"] and not data["earc"] and data["arc_port"] == 2 and data["link"] is None
    data = soundbar.parse(guest_output(None, None, port="{}", state="{}"))
    assert not data["cec"] and not data["earc"] and not data["attached"]
    assert data["arc_port"] is None
    with pytest.raises(Error):
        soundbar.parse("garbage")


def test_command_sets_then_reads():
    line = soundbar.command("2,150750", "2,0101")
    assert line.index("echo 2,150750 2>/dev/null >/sys/module/lgemu/parameters/soundbar") < (
        line.index("cat /sys/module/lgemu/parameters/soundbar;")
    )
    assert line.index("echo 2,0101 2>/dev/null >/sys/module/lgemu/parameters/earc_bar") < (
        line.index("cat /sys/module/lgemu/parameters/earc_bar;")
    )
    assert "!= nobus" in line and "!= absent" in line
    for call in ("system.arcPort", "getProfiles", "getCECState", "arccontroller/getARCState"):
        assert call in line
    assert "> /sys" not in soundbar.command() and ">/sys" not in soundbar.command()


def test_arc_port_from_the_system_profile():
    """43.x: no system.arcPort in the config service; the system profile's arc.port."""
    data = soundbar.parse(
        guest_output(
            "",
            "absent",
            port='{"missingConfigs":["system.arcPort"],"returnValue":true}',
            profile='{"returnValue":true,"profiles":{"arc":{"port":3,"support":true}}}',
        )
    )
    assert data["arc_port"] == 3


def test_earc_capability():
    """HDMI 2.1's eARC capabilities data structure as arccontroller parses it: version 1, block
    ID 1 and its length, CTA-861 audio (tag 1) and speaker allocation (tag 4, 3 bytes) blocks."""
    cap = soundbar.earc_capability(["lpcm", "ac3", "eac3-atmos", "dts"])
    assert cap.hex() == "0101112c0907071507505707013d06c0830f0800"  # 7.1: Atmos's 8 channels
    assert cap[2] == len(cap) - 3
    assert soundbar.earc_capability(["lpcm"]).hex() == "01010823090707830100" + "00"
    assert soundbar.speaker_allocation(["lpcm", "ac3"]) == bytes([0x0F, 0, 0])
    assert soundbar.speaker_allocation(["eac3-atmos"]) == bytes([0x0F, 0x08, 0])
    every = list(soundbar.FORMATS)
    every.remove("eac3")
    assert len(soundbar.earc_capability(every * 2)) <= 256  # the most cec.c takes, 12 SADs
    assert soundbar.capability_sads(cap.hex()) == "0907071507505707013d06c0"
    assert soundbar.capability_sads("0201030000") == ""
    assert soundbar.earc_value(3, ["ac3"]) == "3,01010823150750830f" + "0000"
    assert soundbar.earc_value(3, ["ac3"], arc_only=True) == "3,arc"
    with pytest.raises(Error):
        soundbar.earc_value(0, ["ac3"])


@pytest.mark.skipif(not shutil.which("edid-decode"), reason="edid-decode is not installed")
def test_edid_decode_reads_the_capability_blocks():
    """The data blocks inside block ID 1 are CTA-861's: edid-decode reads them in an EDID."""
    cap = soundbar.earc_capability(["lpcm", "ac3", "eac3-atmos", "dts"])
    blocks = cap[3 : 3 + cap[2]]
    base = bytearray(edid_with_sads(b"")[:128])
    ext = bytearray(128)
    ext[0:4] = bytes([0x02, 0x03, 4 + len(blocks), 0x40])
    ext[4 : 4 + len(blocks)] = blocks
    ext[127] = -sum(ext[:127]) & 0xFF
    out = subprocess.run(
        ["edid-decode"], input=bytes(base + ext), capture_output=True, check=False
    ).stdout.decode()
    assert "Enhanced AC-3 (DD+):\n      Max channels: 8" in out, out
    assert "Supports Joint Object Coding" in out
    assert "Speaker Allocation Data Block:" in out
    for speaker in ("FL/FR", "LFE1", "FC", "BL/BR", "LS/RS"):
        assert speaker in out[out.index("Speaker Allocation") :], out


def test_parse_earc():
    """An eARC board without our CEC bus (K8): the formats from the capability, the link's state,
    arccontroller's view."""
    arc_state = (
        '{"arcActivation":"eARC","audioDeviceType":"arc","earcCapability":["DD+","DD","PCM"],'
        '"earcATMOS":true,"advancedEarcCapability":[{"audioCodingType":"DTS",'
        '"maxChannelNumber":6,"hexSAD":"3d06c0"},{"audioCodingType":"PCM","maxChannelNumber":2,'
        '"hexSAD":"090707"}],"returnValue":true}'
    )
    data = soundbar.parse(
        guest_output(
            None,
            None,
            earc_param="3,0101112c0907071507505707013d06c0830f0000",
            earc_status="enabled=1 status=4 latency_req=0",
            arc_state=arc_state,
        )
    )
    assert (data["cec"], data["earc"], data["attached"], data["link"]) == (
        False,
        True,
        True,
        "earc",
    )
    assert data["port"] == 3 and data["formats"] == ["lpcm", "ac3", "eac3-atmos", "dts"]
    assert data["earc_status"] == {"enabled": True, "status": "EARC", "latency_req": 0}
    assert data["tv"]["arccontroller"] == {
        "arcActivation": "eARC",
        "audioDeviceType": "arc",
        "earcCapability": ["DD+", "DD", "PCM"],
        "earcATMOS": True,
        "earcSADs": ["3d06c0", "090707"],
    }
    # both sides (O22): the CEC side's formats; an ARC-only bar's link
    data = soundbar.parse(
        guest_output(
            "2,150750",
            "arc=1 system_audio=1 volume=25 mute=0",
            earc_param="2,arc",
            earc_status="enabled=1 status=1 latency_req=0",
        )
    )
    assert (data["link"], data["formats"], data["earc_status"]["status"]) == (
        "arc",
        ["ac3"],
        "IDLE_2",
    )
    data = soundbar.parse(
        guest_output("", "absent", earc_status="enabled=0 status=0 latency_req=0")
    )
    assert not data["attached"] and data["earc_status"]["status"] == "IDLE_1"


def test_attach_sets_the_board_sides(monkeypatch):
    """attach: an eARC bar where the board has eARC, the CEC side where it has the bus; an
    ARC-only bar needs the bus."""
    calls = []
    board = {"cec": False, "earc": True}

    def fake_run(vm, line, timeout=20.0):
        calls.append(line)
        attached = "earc_bar;" in line and "echo 3," in line
        return {
            **board,
            "attached": attached,
            "arc_port": 3,
        }

    class Vm:
        name = "tv"

    monkeypatch.setattr(soundbar, "_run", fake_run)
    soundbar.attach(Vm(), ["ac3"])
    assert "earc_bar" in calls[-1] and ">/sys/module/lgemu/parameters/soundbar;" not in calls[-1]
    with pytest.raises(Error, match="ARC-only"):
        soundbar.attach(Vm(), ["ac3"], arc_only=True)
    board.update(earc=False)
    with pytest.raises(Error, match="no sound bar connection"):
        soundbar.attach(Vm(), ["ac3"])
