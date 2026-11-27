# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The OLED Flex's back cover lighting as its gaming LED micom received it, for `beanjar status
show`'s `lighting` object.

Two sources, read when asked over the VM's shell port (guestshell.py), on a VM whose board has
the micom (lgemu's i2c_dev names ledmicom: targets.led_micom_dev, the tool type LX3Q):
- the guest's /proc/lgemu/led_micom (guest/kernel/modules/lgemu/ledmicom.c): what the stock
  lightingcontroller wrote to the micom, per command byte its count, the order of the last write
  and its payload;
- the TV's own settings (the settings service's category other: lightingEnable, lightingMode,
  lightingBrightness), reported as `selected` beside the micom's, so the two can disagree.

decode() turns the table into the lighting the micom shows (docs/research/led-lighting.md, "The
packets on the I2C bus"): `on` (a mode command written after the last off, 0x21 02), `brightness` (1 to 10), `mode`
(the mode command written last) and, for a static mode, its `color` (RRGGBB). A value the micom
was never sent is None. A VM without the micom has no `lighting` object (None)."""

import json
import os
import shlex

LED_MICOM = "/proc/lgemu/led_micom"
I2C_DEV = "/sys/module/lgemu/parameters/i2c_dev"
SETTINGS = (
    "luna://com.webos.settingsservice/getSystemSettings",
    {"category": "other", "keys": ["lightingEnable", "lightingMode", "lightingBrightness"]},
)
SEPARATOR = "--- beanjar settings ---"
NO_MICOM = "no ledmicom"

CMD_OFF = 0x21  # 02: the LEDs off (setLightingEnable off, sent twice; at a start with it off)
CMD_BRIGHTNESS = 0x2A
# the command bytes that set what the LEDs show, and the lightingMode each one is
MODES = {
    0x22: "videoSyncMode",
    0x23: "soundSyncMode",
    0x24: "dynamic",
    0x25: "peaceful",
    0x26: "staticMode1",
    0x27: "staticMode2",
    0x28: "staticMode3",
    0x29: "staticMode4",
    0x2B: "welcomeCeremony",
    0x2C: "factory",
}
STATIC = range(0x26, 0x2A)
OFF = 0x02
# lightingEnable on sends the current mode's command: the LEDs are on from a mode command written
# after the last off (W22O 33.31.68, measured: on gave 0x24, off 0x21 02)
LIT = (0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29)


def command():
    """The guest command: the table and the TV's settings where lgemu has the micom, else
    NO_MICOM. luna-send gets /dev/null: it exits silently, before its reply, when its stdin hangs
    up (the session's ends at once)."""
    url, params = SETTINGS
    return (
        f"if grep -q ledmicom {I2C_DEV} 2>/dev/null; then cat {LED_MICOM}; "
        f"echo {shlex.quote(SEPARATOR)}; "
        f"luna-send -n 1 -w 5000 {url} {shlex.quote(json.dumps(params))} </dev/null; "
        f'else echo "no ""ledmicom"; fi'
    )


def parse_table(text):
    """/proc/lgemu/led_micom as a dict: writes, reads (ints) and commands ({byte: {count, order,
    data (bytes), truncated}}). None when it is not the table (no writes line)."""
    out = {"writes": None, "reads": None, "commands": {}}
    for line in text.splitlines():
        words = line.split()
        if len(words) == 2 and words[0] in ("writes", "reads") and words[1].isdigit():
            out[words[0]] = int(words[1])
        elif len(words) >= 9 and words[0] == "cmd" and words[8] == "data":
            # cmd 0x21 count 2 order 7 len 1 data 02 [truncated]
            payload = words[9] if len(words) > 9 and words[9] != "truncated" else ""
            try:
                out["commands"][int(words[1], 16)] = {
                    "count": int(words[3]),
                    "order": int(words[5]),
                    "data": bytes.fromhex(payload),
                    "truncated": words[-1] == "truncated",
                }
            except ValueError:
                continue
    return out if out["writes"] is not None else None


def parse_settings(text):
    """The TV's lightingEnable, lightingMode and lightingBrightness from luna-send's answer, None
    when it gave none."""
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
        "enable": settings.get("lightingEnable"),
        "mode": settings.get("lightingMode"),
        "brightness": settings.get("lightingBrightness"),
    }


def decode(table):
    """The lighting the micom shows from its table: on, brightness, mode, color (None for what it
    was never sent)."""
    out = {"on": None, "brightness": None, "mode": None, "color": None}
    if not table:
        return out
    cmds = table["commands"]
    off = cmds.get(CMD_OFF)
    off_order = off["order"] if off and off["data"][:1] == bytes([OFF]) else 0
    lit_order = max((cmds[c]["order"] for c in cmds if c in LIT), default=0)
    if off_order or lit_order:
        out["on"] = lit_order > off_order
    bright = cmds.get(CMD_BRIGHTNESS)
    if bright and bright["data"]:
        out["brightness"] = bright["data"][0]
    last = max((c for c in cmds if c in MODES), key=lambda c: cmds[c]["order"], default=None)
    if last is not None:
        out["mode"] = MODES[last]
        if last in STATIC and len(cmds[last]["data"]) >= 3:
            out["color"] = cmds[last]["data"][:3].hex()
    return out


def report(table_text, settings_text):
    """status show's lighting object from the two sources' text (None for one not read)."""
    table = parse_table(table_text) if table_text is not None else None
    return {
        "selected": parse_settings(settings_text) if settings_text is not None else None,
        **decode(table),
        "writes": table["writes"] if table else None,
        "commands": [
            {
                "command": f"0x{c:02x}",
                "count": v["count"],
                "order": v["order"],
                "data": v["data"].hex(),
                "truncated": v["truncated"],
            }
            for c, v in sorted(table["commands"].items())
        ]
        if table
        else None,
    }


def has_micom(vm_dir):
    """Whether the VM in vm_dir has the micom: its prepared board data gives lgemu the device
    (prepare.py, targets.led_micom_dev). False where it has none, or no prepared data."""
    from ..board import prepare

    try:
        with open(os.path.join(vm_dir, prepare.DIR, prepare.RECORD)) as f:
            words = json.load(f).get("derived_params", {}).get("lgemu", [])
    except (OSError, ValueError, AttributeError):
        return False
    return any(w.startswith("i2c_dev=") and "ledmicom:" in w for w in words)


def read(vm, timeout=10.0):
    """The lighting object of a running library VM, None where its board has no micom (has_micom);
    with `error` when the guest did not answer (still starting, a beanjar without the shell
    relay)."""
    from ..access import guestshell
    from ..base import Error

    if not has_micom(vm.dir):
        return None

    error, text = None, None
    try:
        sock = guestshell.local_socket(vm)
        with sock:
            status, text = guestshell.output(sock, ["sh", "-c", command()], timeout)
        if status == guestshell.LOST:
            error, text = "the VM's shell did not answer", None
    except Error as e:
        error = str(e)
    if text is not None and text.strip() == NO_MICOM:
        return None
    table_text, _, settings_text = (
        text.partition(SEPARATOR) if text is not None else (None, "", None)
    )
    out = report(table_text, settings_text if text is not None else None)
    if error is None and out["writes"] is None:
        error = f"{LED_MICOM} is not there (an older lgemu module)"
    out["error"] = error
    return out


def lines(lighting):
    """status show's text for the lighting object."""
    if lighting is None:
        return []
    if lighting.get("error") and lighting.get("writes") is None:
        return [f"lighting: {lighting['error']}"]
    sel = lighting.get("selected") or {}
    out = [
        "lighting selected: "
        + (
            f"{sel.get('enable')}, {sel.get('mode')}, brightness {sel.get('brightness')}"
            if sel
            else "not known"
        )
    ]
    if not lighting.get("writes"):
        out.append("lighting micom: nothing sent yet")
        return out
    on = lighting.get("on")
    state = "on" if on else "off" if on is not None else "not sent"
    mode = lighting.get("mode") or "no mode sent"
    if lighting.get("color"):
        mode += f" #{lighting['color']}"
    bright = lighting.get("brightness")
    out.append(
        f"lighting micom: {state}, {mode}, brightness "
        + (str(bright) if bright is not None else "not sent")
        + f" ({lighting['writes']} writes)"
    )
    return out
