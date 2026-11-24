#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""io soundbar: the formats' CTA-861 short audio descriptors (checked against edid-decode, which
reads them from an EDID's CTA-861 audio data block), the parameter lgemu's cec.c takes, and the
guest's answer as the command reports it."""

import shutil
import subprocess

import pytest

from beanjar import soundbar
from beanjar.core import Error


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
):
    sep = soundbar.SEPARATOR + "\n"
    state = state or (
        '{"arcProfile":"EAC3","cecReady":true,"returnValue":true,'
        '"arcSADs":"150750550700","arcActivation":true}'
    )
    return f"{param}\n{sep}{status}\n{sep}{port}\n{sep}{profile}\n{sep}{state}\n"


def test_parse_attached():
    data = soundbar.parse(
        guest_output("2,1507505507003d06c0", "arc=1 system_audio=1 volume=25 mute=0")
    )
    assert data == {
        "cec": True,
        "attached": True,
        "port": 2,
        "sads": "1507505507003d06c0",
        "formats": ["ac3", "eac3", "dts"],
        "status": {"arc": True, "system_audio": True, "volume": 25, "mute": False},
        "arc_port": 2,
        "tv": {
            "cecReady": True,
            "arcActivation": True,
            "arcSADs": "150750550700",
            "arcProfile": "EAC3",
        },
    }


def test_parse_absent_and_no_cec():
    data = soundbar.parse(guest_output("", "absent", state='{"returnValue":false}'))
    assert not data["attached"] and data["status"] is None and data["tv"] is None
    assert data["cec"] and data["arc_port"] == 2
    sep = soundbar.SEPARATOR + "\n"
    data = soundbar.parse(f"nocec\n{sep}{{}}\n{sep}{{}}\n{sep}\n")
    assert not data["cec"] and not data["attached"] and data["arc_port"] is None
    with pytest.raises(Error):
        soundbar.parse("garbage")


def test_command_sets_then_reads():
    line = soundbar.command("2,150750")
    assert line.index("echo 2,150750 > /sys/module/lgemu/parameters/soundbar") < line.index(
        "cat /sys/module/lgemu/parameters/soundbar;"
    )
    assert "system.arcPort" in line and "getProfiles" in line and "getCECState" in line


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
    assert "> /sys" not in soundbar.command()
