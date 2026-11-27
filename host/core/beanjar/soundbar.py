# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A sound bar on a running VM's HDMI ARC input: `beanjar io soundbar attach|detach|show NAME`.

The bar has two sides in lgemu, both set over the VM's shell port (guestshell.py) and neither kept
across a restart of the VM or of its guest (a bar unplugged):
- its CEC side (guest/kernel/modules/lgemu/cec.c), an audio system at logical address 5 on the
  TV's ARC input, which answers the ARC handshake, System Audio Mode and Request Short Audio
  Descriptor with the descriptors it was given: lgemu's `soundbar` parameter, "PORT,SADS", PORT
  the TV's own ARC input (the config service's system.arcPort, or on the 43.x builds the system
  profile's arc.port, which eim reads too), SADS the CTA-861 short audio descriptors of the
  formats, three bytes each in hex, as eim reports them in arcSADs;
- its eARC side (lgemu's earc.c, /dev/earc0), which arccontroller polls with the user's eARC
  Support setting on: lgemu's `earc_bar` parameter, "PORT,CAPABILITY", the HDMI 2.1 eARC
  capabilities data structure of the same formats in hex, or "PORT,arc" for a bar without eARC,
  whose discovery fails (IDLE2) so that arccontroller lets ARC start over CEC.

The firmwares whose CEC is libcecp on /dev/cec0 with lgemu's adapter have the CEC side (the O22
family and the lm21 boards, targets with lgemu's cec=1), those with eARC in their profile the
eARC side (the K8 family and O22's, earc=1); a bar needs one of them, and an ARC-only bar the CEC
bus. The TV talks CEC to it only with its HDMI-CEC (SIMPLINK) setting on (other/simplinkEnable);
it enables eARC only with eARC Support on (sound/eArcSupport), and starts ARC over CEC with it off
or with an ARC-only bar: all the user's, as on a set."""

import json
import re
import shlex

from .base import Error

PARAMS = "/sys/module/lgemu/parameters"
SEPARATOR = "--- beanjar ---"
ARC_PORT = (
    "luna://com.webos.service.config/getConfigs",
    {"configNames": ["system.arcPort"]},
)
# the 43.x builds' eim reads it from the system profile's arc.port instead
ARC_PROFILE = ("luna://com.webos.service.systemprofile/getProfiles", {"names": ["arc"]})
CEC_STATE = ("luna://com.webos.service.eim/cec/getCECState", {})
ARC_STATE = ("luna://com.webos.service.arccontroller/getARCState", {})
MAX_SADS = 12  # cec.c's BAR_MAX_SADS
# earc.c's connection status, by arccontroller's names (LG's videodev2-ext.h: IDLE1 to EARC)
EARC_STATUS = {0: "IDLE_1", 1: "IDLE_2", 2: "DISC_1", 3: "DISC_2", 4: "EARC"}

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


def speaker_allocation(names):
    """The CTA-861 speaker allocation (the payload of its data block, three bytes) of a bar with
    formats names: its largest channel count's layout (CTA-861-H 7.5.3): 2.0 FL/FR (byte 0 bit 0);
    5.1 adds LFE1, FC and BL/BR (bits 1 to 3); 7.1 adds LS/RS (byte 1 bit 3)."""
    channels = max((FORMATS[n][1][0] & 7) + 1 for n in names)
    if channels <= 2:
        return bytes([0x01, 0, 0])
    return bytes([0x0F, 0x08 if channels > 6 else 0, 0])


def earc_capability(names):
    """The bar's eARC capabilities data structure (HDMI 2.1, as arccontroller's
    EarcCapability::setEarcCurrentCapability reads it): version 1, then block ID 1 and its
    length, holding a CTA-861 audio data block (tag 1, the formats' descriptors) and a speaker
    allocation data block (tag 4, three bytes); arccontroller stops at the ID 0 after it."""
    raw = sads(names)
    blocks = bytes([1 << 5 | len(raw)]) + raw + bytes([4 << 5 | 3]) + speaker_allocation(names)
    return bytes([1, 1, len(blocks)]) + blocks


def capability_sads(cap_hex):
    """The descriptors (hex) of a capability's audio data blocks, those arccontroller parses."""
    cap = bytes.fromhex(cap_hex)
    out = b""
    if len(cap) < 3 or cap[0] != 1:
        return ""
    pos = 1
    while pos + 1 < len(cap) and cap[pos] & 0x1F:
        end = pos + 2 + cap[pos + 1]
        if cap[pos] & 0x1F == 1:
            i = pos + 2
            while i < min(end, len(cap)):
                tag, n = cap[i] >> 5, cap[i] & 0x1F
                if tag == 1:
                    out += cap[i + 1 : i + 1 + n - n % 3]
                i += 1 + n
        pos = end
    return out.hex()


def earc_value(port, names, arc_only=False):
    """lgemu's earc_bar parameter for a bar on ARC input port with formats names: its capability,
    or "arc" for a bar without eARC."""
    if not 1 <= int(port) <= 15:
        raise Error(f"the ARC input {port} is not an HDMI input number")
    return f"{int(port)},arc" if arc_only else f"{int(port)},{earc_capability(names).hex()}"


def parse_earc_param(text):
    """(port, capability hex or "arc") of earc_bar's text, None when no bar."""
    text = text.strip()
    if not text:
        return None
    port, _, rest = text.partition(",")
    return int(port), rest


EARC_STATUS_RE = re.compile(r"^enabled=(\d) status=(\d) latency_req=(\d+)$")


def parse_earc_status(text):
    """earc_status's {enabled, status, latency_req}, None for absent."""
    m = EARC_STATUS_RE.match(text.strip())
    if not m:
        return None
    return {
        "enabled": m.group(1) == "1",
        "status": EARC_STATUS.get(int(m.group(2)), m.group(2)),
        "latency_req": int(m.group(3)),
    }


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


def command(value=None, earc=None):
    """The guest command: (after setting the parameters to value and earc, those given) the CEC
    bar's parameter and status ("nocec" for a guest without its bus: no adapter), the eARC bar's parameter
    and the link's status ("absent" without /dev/earc0), then the TV's ARC input (the config
    service's, the system profile's), its CEC state and arccontroller's ARC state (luna-send's
    answers), SEPARATOR between them."""
    sep = shlex.quote(SEPARATOR)
    set_ = f"echo {shlex.quote(value)} 2>/dev/null >{PARAMS}/soundbar; " if value else ""
    set_earc = f"echo {shlex.quote(earc)} 2>/dev/null >{PARAMS}/earc_bar; " if earc else ""
    return (
        f'if [ -e {PARAMS}/soundbar ] && [ "$(cat {PARAMS}/soundbar_status)" != nobus ]; then '
        f"{set_}"
        f"cat {PARAMS}/soundbar; echo {sep}; cat {PARAMS}/soundbar_status; "
        f"else echo no''cec; echo {sep}; fi; echo {sep}; "
        f'if [ -e {PARAMS}/earc_status ] && [ "$(cat {PARAMS}/earc_status)" != absent ]; then '
        f"{set_earc}cat {PARAMS}/earc_bar; echo {sep}; cat {PARAMS}/earc_status; "
        f"else echo absent; echo {sep}; fi; echo {sep}; "
        f"{luna(ARC_PORT)}; echo {sep}; {luna(ARC_PROFILE)}; echo {sep}; {luna(CEC_STATE)}; "
        f"echo {sep}; {luna(ARC_STATE)}"
    )


def json_answer(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def parse(out):
    """command()'s output as {"cec", "earc", "attached", "link", "port", "sads", "formats",
    "status", "earc_status", "arc_port", "tv"}; Error for an output that is not one."""
    parts = out.split(SEPARATOR + "\n")
    if len(parts) != 8:
        raise Error(f"no answer about the sound bar: {out!r}")
    cec = parts[0].strip() != "nocec"
    earc = parts[2].strip() != "absent"
    param = parse_param(parts[0]) if cec else None
    earc_param = parse_earc_param(parts[2]) if earc else None
    config = json_answer(parts[4]) or {}
    arc_port = (config.get("configs") or {}).get("system.arcPort")
    if not isinstance(arc_port, int):
        profile = json_answer(parts[5]) or {}
        arc_port = ((profile.get("profiles") or {}).get("arc") or {}).get("port")
    state = json_answer(parts[6]) or {}
    tv = {}
    if state.get("returnValue"):
        tv.update(
            cecReady=state.get("cecReady"),
            arcActivation=state.get("arcActivation"),
            arcSADs=state.get("arcSADs"),
            arcProfile=state.get("arcProfile"),
        )
    arc_state = json_answer(parts[7]) or {}
    if arc_state.get("returnValue"):
        tv["arccontroller"] = {
            k: arc_state.get(k)
            for k in ("arcActivation", "audioDeviceType", "earcCapability", "earcATMOS")
        }
        tv["arccontroller"]["earcSADs"] = [
            c.get("hexSAD") for c in arc_state.get("advancedEarcCapability") or ()
        ]
    port, sad_hex, link = None, None, None
    if earc_param:  # its port, its formats from the capability, unless the CEC side has them
        port, link = earc_param[0], "arc" if earc_param[1] == "arc" else "earc"
        sad_hex = capability_sads(earc_param[1]) if link == "earc" else None
    if param:
        port, sad_hex, link = param[0], param[1], link or "arc"
    attached = link is not None
    return {
        "cec": cec,
        "earc": earc,
        "attached": attached,
        "link": link,
        "port": port,
        "sads": sad_hex,
        "formats": format_names(sad_hex) if sad_hex else None,
        "status": parse_status(parts[1]) if param else None,
        "earc_status": parse_earc_status(parts[3]) if earc else None,
        "arc_port": arc_port if isinstance(arc_port, int) else None,
        "tv": tv or None,
    }


def _run(vm, line, timeout=20.0):
    from . import guestshell

    with guestshell.local_socket(vm) as sock:
        status, text = guestshell.output(sock, ["sh", "-c", line], timeout)
    if status == guestshell.LOST:
        raise Error(f"VM {vm.name}: its shell did not answer")
    return parse(text)


def _need_bus(vm, data):
    if not data["cec"] and not data["earc"]:
        raise Error(
            f"VM {vm.name}: its board has no sound bar connection of ours: neither lgemu's CEC "
            "adapter (the libcecp firmwares of the O22 family and the lm21 boards) nor its eARC "
            "transmitter (the K8 family and O22's)"
        )


def show(vm):
    """The VM's sound bar and the TV's view of it."""
    return {"name": vm.name, **_run(vm, command())}


def attach(vm, names, arc_only=False):
    """Plug a sound bar with formats names into the VM's ARC input: an eARC bar where the board
    has eARC, else (and with arc_only) one with ARC only."""
    data = _run(vm, command())
    _need_bus(vm, data)
    if arc_only and not data["cec"]:
        raise Error(
            f"VM {vm.name}: an ARC-only bar talks CEC, and its board has no CEC bus of ours "
            "(only eARC)"
        )
    if data["attached"]:
        raise Error(f"VM {vm.name} has a sound bar: detach it first (beanjar io soundbar detach)")
    if data["arc_port"] is None:
        raise Error(f"VM {vm.name}: its firmware names no ARC input (system.arcPort, arc.port)")
    port = data["arc_port"]
    value = param_value(port, names) if data["cec"] else None
    earc = earc_value(port, names, arc_only) if data["earc"] else None
    data = _run(vm, command(value, earc))
    if not data["attached"]:
        raise Error(f"VM {vm.name}: lgemu did not take the sound bar")
    return {"name": vm.name, **data}


def detach(vm):
    """Switch the VM's sound bar off and unplug it."""
    data = _run(vm, command())
    _need_bus(vm, data)
    if not data["attached"]:
        raise Error(f"VM {vm.name} has no sound bar")
    off = _run(
        vm,
        command("off" if data["cec"] else None, "off" if data["earc"] else None),
    )
    return {"name": vm.name, **off}
