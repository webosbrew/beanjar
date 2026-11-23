# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The set's sensors, set from outside: sensor show|set light, presence, temperature, rotate."""

import argparse

from ..core import Error
from .common import emit


def cmd_rotate(lib, a):
    from .. import rotation

    vm = lib.vm(a.name)
    if a.angle is None:
        data = rotation.status(vm)
        if not data["portrait"]:
            text = f"{a.name}: its model does not turn to portrait"
        elif data["rotation"] is None:
            text = f"{a.name}: rotation not reported yet" + ("" if vm.pid() else " (not running)")
        else:
            text = f"{a.name}: rotation {data['rotation']}"
        return emit(a, data, text)
    data = rotation.rotate(vm, a.angle)
    text = f"{a.name}: rotation {data['rotation']}" + ("" if data["changed"] else " (unchanged)")
    if data["refused"]:
        text += f"; the set stands at {a.angle}, which its firmware does not turn to"
    return emit(a, data, text)


def cmd_light(lib, a):
    from .. import sensors

    channels = None
    if a.values:
        if len(a.values) > 2:
            raise Error("light: at most two values, CH0 and CH1")
        channels = (a.values[0], a.values[-1])
    data = sensors.light(lib.vm(a.name), channels)
    if not data["sensor"]:
        text = f"{a.name}: no ambient light sensor (its board's digitalEye is off)"
    else:
        text = f"{a.name}: light {data['light'][0]},{data['light'][1]}"
    return emit(a, data, text)


def cmd_presence(lib, a):
    from .. import sensors

    data = sensors.presence(lib.vm(a.name), a.mm)
    if not data["sensor"]:
        text = f"{a.name}: no ToF motion sensor (its board's supportMotionDetectSensor is not ToF)"
    elif data["presence"]:
        text = f"{a.name}: someone moving at {data['presence']} mm"
    else:
        text = f"{a.name}: nobody"
    return emit(a, data, text)


def cmd_temperature(lib, a):
    from .. import sensors

    data = sensors.temperature(lib.vm(a.name), a.celsius)
    if not data["thermalinfo"]:
        text = f"{a.name}: no /proc/thermalinfo (its firmware has no fancontroller)"
    else:
        text = f"{a.name}: SoC {data['soc_temperature']} C"
    return emit(a, data, text)


def rotation_angle(text):
    """rotate's ANGLE: 0, 90, 180 or 270 (off is 0), rotation.angle_value."""

    from .. import rotation

    try:
        return rotation.angle_value(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r}: {e}") from None


def light_count(text):
    """light's CH0/CH1: raw counts, 0 to 65535 (sensors.count_value)."""

    from .. import sensors

    try:
        return sensors.count_value(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r}: {e}") from None


def presence_mm(text):
    """presence's argument: present, absent or millimetres (sensors.presence_value)."""

    from .. import sensors

    try:
        return sensors.presence_value(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r}: {e}") from None


def celsius(text):
    """temperature's CELSIUS: -40 to 200 (sensors.celsius_value)."""

    from .. import sensors

    try:
        return sensors.celsius_value(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r}: {e}") from None


SENSORS = {
    # kind: (its value's metavar, what set does, what show gives)
    "light": (
        "CH0 [CH1]",
        (
            "set the ambient light on the set's light sensor (its board's digitalEye tool option): "
            "CH0 and CH1 are the chip's two channels in raw counts, 0 to 65535 (one value sets "
            "both), which pqcontroller reports as luminance and visibleLuminance and, with energy "
            "saving on auto, dims the backlight by (dark 0; 6000, the default at a start, keeps the "
            "user's backlight). Not kept across a restart"
        ),
        "the light the set's light sensor has",
    ),
    "presence": (
        "present|absent|MM",
        (
            "set who is in front of the set's ToF motion sensor (its board's "
            "supportMotionDetectSensor tool option): present (someone moving at 1500 mm), MM (at that "
            "many millimetres, 100 to 5000) or absent; msc reports the motion in Always Ready, and "
            "the no-motion timers act on it. Not kept across a restart (absent at a start)"
        ),
        "who the set's ToF motion sensor has in front of it",
    ),
    "temperature": (
        "CELSIUS",
        (
            "set the SoC's temperature, whole degrees Celsius (-40 to 200), as "
            "/proc/thermalinfo/soc_temperature reads it, which fancontroller reports each second; "
            "only firmwares with fancontroller have it. Not kept across a restart (45 at a start)"
        ),
        "the SoC's temperature as the set reads it",
    ),
    "rotate": (
        "ANGLE",
        (
            "turn a StanbyME (a model that turns to portrait) to ANGLE (0, 90, 180 or 270; 90 is the "
            "set a quarter counter-clockwise; off is 0): the gravity vector of its accelerometer, "
            "kept for the next start, which webOS turns its UI by; waits until the guest reports the "
            "orientation webOS took (webOS 24 keeps its orientation at 180), and the viewers turn the "
            "picture to match"
        ),
        "the rotation the guest last reported",
    ),
}


def register(cli):
    command, group, vm_name = cli.command, cli.group, cli.vm_name
    sensor = group(
        "sensor",
        "the sensors a set reads, set from outside: light, temperature, presence, rotation",
    )
    funcs = {"light": cmd_light, "presence": cmd_presence, "temperature": cmd_temperature}
    funcs["rotate"] = cmd_rotate
    shown = {"values": [], "mm": None, "celsius": None, "angle": None}
    for verb in ("show", "set"):
        g = group(
            verb,
            f"{verb} a sensor's value: " + ", ".join(SENSORS),
            under=sensor,
            dest="area",
            metavar="SENSOR",
        )
        for kind, (metavar, set_help, show_help) in SENSORS.items():
            value = f" {metavar}" if verb == "set" else ""
            s = command(
                g,
                kind,
                set_help if verb == "set" else show_help,
                funcs[kind],
                usage=f"beanjar sensor {verb} {kind} NAME{value} [--json]",
            )
            vm_name(s)
            if verb == "show":
                s.set_defaults(**shown)
            elif kind == "light":
                s.add_argument("values", nargs="+", type=light_count, metavar="CH")
            elif kind == "presence":
                s.add_argument("mm", type=presence_mm, metavar="present|absent|MM")
            elif kind == "temperature":
                s.add_argument("celsius", type=celsius, metavar="CELSIUS")
            else:
                s.add_argument("angle", type=rotation_angle, metavar="ANGLE")
