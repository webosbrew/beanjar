# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A sound bar on a running VM's HDMI CEC bus: `beanjar io soundbar attach|detach|show NAME`.

The bar is lgemu's (guest/kernel/modules/lgemu/cec.c): an audio system at logical address 5 on
the TV's ARC input, which answers the ARC handshake, System Audio Mode and Request Short Audio
Descriptor with the descriptors it was given. The host sets it through lgemu's `soundbar`
parameter, "PORT,SADS", over the VM's shell port (guestshell.py): PORT is the TV's own ARC input
(the config service's system.arcPort, or on the 43.x builds the system profile's arc.port, which
eim reads too), SADS the CTA-861 short audio
descriptors of the formats asked for, three bytes each in hex, as eim reports them in arcSADs.
It is not a setting: a restart of the VM or of its guest has no bar, as a bar unplugged.

The firmwares whose CEC is libcecp on /dev/cec0 with lgemu's adapter have it (the O22 family and
the lm21 boards, targets with lgemu's cec=1); the others report that they have no such bus.
The TV talks to it only with its HDMI-CEC (SIMPLINK) setting on (other/simplinkEnable), and
starts ARC over CEC only with eARC Support off (sound/eArcSupport; with it on arccontroller waits
for the eARC driver): both are the user's, as on a set."""

import json
import re
import shlex

from .core import Error

PARAMS = "/sys/module/lgemu/parameters"
SEPARATOR = "--- beanjar ---"
ARC_PORT = (
    "luna://com.webos.service.config/getConfigs",
    {"configNames": ["system.arcPort"]},
)
# the 43.x builds' eim reads it from the system profile's arc.port instead
ARC_PROFILE = ("luna://com.webos.service.systemprofile/getProfiles", {"names": ["arc"]})
CEC_STATE = ("luna://com.webos.service.eim/cec/getCECState", {})
MAX_SADS = 12  # cec.c's BAR_MAX_SADS

# CTA-861 short audio descriptors, three bytes each (CTA-861-H 7.5.2, `struct cea_sad`):
# byte 0 the format code in bits 6-3 and the channels minus one in bits 2-0, byte 1 the sample
# rates (bit 0 32 kHz, 1 44.1 kHz, 2 48 kHz, ...), byte 2 per format: LPCM's sample sizes (bit 0
# 16, 1 20, 2 24 bit), AC-3's and DTS's maximum bit rate in 8 kbit/s, E-AC-3's bit 0 Joint Object
# Coding (Dolby Atmos), the bit arccontroller and eim read as ATMOS.
FORMATS = {
    "lpcm": ("LPCM, 2 channels, 32/44.1/48 kHz, 16/20/24 bit", bytes([0x09, 0x07, 0x07])),
    "ac3": ("AC-3, 5.1, 32/44.1/48 kHz, 640 kbit/s", bytes([0x15, 0x07, 0x50])),
    "eac3": ("E-AC-3, 5.1, 32/44.1/48 kHz", bytes([0x55, 0x07, 0x00])),
    "eac3-atmos": ("E-AC-3 with Atmos (JOC), 7.1, 32/44.1/48 kHz", bytes([0x57, 0x07, 0x01])),
    "dts": ("DTS, 5.1, 44.1/48 kHz, 1536 kbit/s", bytes([0x3D, 0x06, 0xC0])),
    "aac": ("AAC LC, 5.1, 32/44.1/48 kHz, 640 kbit/s", bytes([0x35, 0x07, 0x50])),
}
DEFAULT_FORMATS = ("lpcm", "ac3", "eac3", "dts")
EXCLUSIVE = ({"eac3", "eac3-atmos"},)


def parse_formats(text):
    """The format names of --formats (comma-separated, FORMATS' keys), in order; ValueError."""
    names = [w.strip().lower() for w in str(text).split(",") if w.strip()]
    if not names:
        raise ValueError("no formats")
    unknown = [n for n in names if n not in FORMATS]
    if unknown:
        raise ValueError(f"{', '.join(unknown)}: formats are {', '.join(FORMATS)}")
    if len(set(names)) != len(names):
        raise ValueError("a format given twice")
    for group in EXCLUSIVE:
        if len(group & set(names)) > 1:
            raise ValueError(f"one of {' or '.join(sorted(group))}")
    return names


def sads(names):
    """The descriptors of formats names, as bytes."""
    return b"".join(FORMATS[n][1] for n in names)


def format_names(sad_hex):
    """The FORMATS names of a descriptor list in hex, a descriptor not in FORMATS as its hex."""
    by_bytes = {v[1]: k for k, v in FORMATS.items()}
    raw = bytes.fromhex(sad_hex)
    return [by_bytes.get(raw[i : i + 3], raw[i : i + 3].hex()) for i in range(0, len(raw), 3)]


def param_value(port, names):
    """lgemu's soundbar parameter for a bar on ARC input port with formats names."""
    if not 1 <= int(port) <= 15:
        raise Error(f"the ARC input {port} is not an HDMI input number")
    if len(names) > MAX_SADS:
        raise Error(f"at most {MAX_SADS} formats")
    return f"{int(port)},{sads(names).hex()}"


def parse_param(text):
    """(port, SADs hex) of the parameter's text, None when no bar."""
    text = text.strip()
    if not text:
        return None
    port, _, hex_ = text.partition(",")
    return int(port), hex_


STATUS_RE = re.compile(r"^arc=(\d) system_audio=(\d) volume=(\d+) mute=(\d)$")


def parse_status(text):
    """soundbar_status's {arc, system_audio, volume, mute}, None for absent."""
    m = STATUS_RE.match(text.strip())
    if not m:
        return None
    return {
        "arc": m.group(1) == "1",
        "system_audio": m.group(2) == "1",
        "volume": int(m.group(3)),
        "mute": m.group(4) == "1",
    }


def luna(url_params):
    url, params = url_params
    return f"luna-send -n 1 -w 5000 {url} {shlex.quote(json.dumps(params))} </dev/null"


def command(value=None):
    """The guest command: (after setting the parameter to value) the parameter, the bar's status,
    then the TV's ARC input (the config service's, the system profile's) and its CEC state
    (luna-send's answers), SEPARATOR between them; "nocec"
    for a guest without the parameter."""
    sep = shlex.quote(SEPARATOR)
    set_ = f"echo {shlex.quote(value)} > {PARAMS}/soundbar && " if value is not None else ""
    return (
        f"if [ -e {PARAMS}/soundbar ]; then {set_}"
        f"cat {PARAMS}/soundbar; echo {sep}; cat {PARAMS}/soundbar_status; "
        f"else echo no''cec; fi; echo {sep}; {luna(ARC_PORT)}; echo {sep}; {luna(ARC_PROFILE)}; "
        f"echo {sep}; {luna(CEC_STATE)}"
    )


def json_answer(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def parse(out):
    """command()'s output as {"cec", "attached", "port", "sads", "formats", "status", "arc_port",
    "tv"}; Error for an output that is not one."""
    parts = out.split(SEPARATOR + "\n")
    if parts and parts[0].strip() == "nocec":
        parts = ["", "", *parts[1:]]
        cec = False
    else:
        cec = True
    if len(parts) != 5:
        raise Error(f"no answer about the sound bar: {out!r}")
    param = parse_param(parts[0]) if cec else None
    config = json_answer(parts[2]) or {}
    arc_port = (config.get("configs") or {}).get("system.arcPort")
    if not isinstance(arc_port, int):
        profile = json_answer(parts[3]) or {}
        arc_port = ((profile.get("profiles") or {}).get("arc") or {}).get("port")
    state = json_answer(parts[4]) or {}
    tv = None
    if state.get("returnValue"):
        tv = {
            "cecReady": state.get("cecReady"),
            "arcActivation": state.get("arcActivation"),
            "arcSADs": state.get("arcSADs"),
            "arcProfile": state.get("arcProfile"),
        }
    return {
        "cec": cec,
        "attached": param is not None,
        "port": param[0] if param else None,
        "sads": param[1] if param else None,
        "formats": format_names(param[1]) if param else None,
        "status": parse_status(parts[1]) if param else None,
        "arc_port": arc_port if isinstance(arc_port, int) else None,
        "tv": tv,
    }


def _run(vm, line, timeout=20.0):
    from . import guestshell

    with guestshell.local_socket(vm) as sock:
        status, text = guestshell.output(sock, ["sh", "-c", line], timeout)
    if status == guestshell.LOST:
        raise Error(f"VM {vm.name}: its shell did not answer")
    return parse(text)


def _need_cec(vm, data):
    if not data["cec"]:
        raise Error(
            f"VM {vm.name}: its board has no CEC bus of ours (lgemu's cec adapter, the libcecp "
            "firmwares of the O22 family and the lm21 boards)"
        )


def show(vm):
    """The VM's sound bar and the TV's view of it."""
    return {"name": vm.name, **_run(vm, command())}


def attach(vm, names):
    """Plug a sound bar with formats names into the VM's ARC input."""
    data = _run(vm, command())
    _need_cec(vm, data)
    if data["attached"]:
        raise Error(f"VM {vm.name} has a sound bar: detach it first (beanjar io soundbar detach)")
    if data["arc_port"] is None:
        raise Error(f"VM {vm.name}: its firmware names no ARC input (system.arcPort, arc.port)")
    data = _run(vm, command(param_value(data["arc_port"], names)))
    if not data["attached"]:
        raise Error(f"VM {vm.name}: lgemu did not take the sound bar")
    return {"name": vm.name, **data}


def detach(vm):
    """Switch the VM's sound bar off and unplug it."""
    data = _run(vm, command())
    _need_cec(vm, data)
    if not data["attached"]:
        raise Error(f"VM {vm.name} has no sound bar")
    return {"name": vm.name, **_run(vm, command("off"))}
