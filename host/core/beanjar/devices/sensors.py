# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The board's sensors that the host sets on a running VM: the ambient light sensor, the
SoC's temperature and the ToF motion sensor, all in lgemu.ko and set through its module parameters over the console,
as `beanjar sensor set rotate` sets the accelerometer (rotation.py).

`beanjar sensor set light NAME CH0 [CH1]` sets the light sensor's two channels, raw counts of the chip
(0 to 65535; one value sets both): pqcontroller reports ch0 as luminance and ch1 as
visibleLuminance, and with energy saving on auto dims the backlight by them. The sensor exists on
a board whose tool option digitalEye says the set has one (targets.light_dev); lgemu's i2c_dev in the
guest says whether this VM has it. `beanjar sensor set temperature NAME CELSIUS` sets what
/proc/thermalinfo/soc_temperature reads, which fancontroller reports each second (NL_CHIP_THERMAL);
the chips whose firmware has fancontroller have it (targets.Soc.thermal). Neither is kept across a
restart: a start has lgemu's defaults (light.c, thermal.c).

`beanjar sensor set presence NAME present|absent|MM` sets who is in front of the ToF motion sensor: a
person who keeps moving at MM millimetres (100 to 5000; present is 1500), or nobody (absent).
msc reports motion while someone moves (getMotionStatus), and only drives the sensor in Always
Ready (Life On Screen with the motion sensor on). The sensor exists on a board whose tool option
supportMotionDetectSensor names the TMF8821 (targets.tof_dev). Nobody is there at a start (tof.c).
"""

import os
import re

from .. import base
from ..base import Error

PARAMS = "/sys/module/lgemu/parameters"
COUNT_MAX = 0xFFFF
TEMP_MIN, TEMP_MAX = -40, 200  # thermal.c's THERMAL_MIN, THERMAL_MAX
LIGHT_RE = re.compile(r"^light=(\d+),(\d+)$", re.MULTILINE)
TEMP_RE = re.compile(r"^soc_temperature=(-?\d+)$", re.MULTILINE)
PRESENCE_RE = re.compile(r"^presence=(\d+)$", re.MULTILINE)
PRESENT_MM = 1500  # tof.c's TOF_PRESENT_MM
MM_MIN, MM_MAX = 100, 5000  # tof.c's TOF_MIN_MM, TOF_MAX_MM


def count_value(word):
    """A CLI light channel: raw counts, 0 to 65535."""
    word = str(word).strip()
    if not word.isdigit() or int(word) > COUNT_MAX:
        raise ValueError(f"a channel is raw counts, 0 to {COUNT_MAX}")
    return int(word)


def celsius_value(word):
    """A CLI temperature: whole degrees Celsius, -40 to 200."""
    word = str(word).strip()
    if not re.fullmatch(r"-?\d+", word) or not TEMP_MIN <= int(word) <= TEMP_MAX:
        raise ValueError(f"the temperature is whole degrees Celsius, {TEMP_MIN} to {TEMP_MAX}")
    return int(word)


def presence_value(word):
    """A CLI presence: "present" (PRESENT_MM), "absent" (0) or millimetres, 100 to 5000."""
    word = str(word).strip().lower()
    if word == "present":
        return PRESENT_MM
    if word == "absent":
        return 0
    if not word.isdigit() or not MM_MIN <= int(word) <= MM_MAX:
        raise ValueError(f"presence is present, absent or millimetres, {MM_MIN} to {MM_MAX}")
    return int(word)


def presence_command(mm=None):
    """The console line that reports the ToF motion sensor's person (presence=MM, 0 for nobody, or
    notof without the sensor), after setting it to mm when given."""
    set_ = f"echo {mm} > {PARAMS}/presence && " if mm is not None else ""
    return (
        f"if grep -q tof {PARAMS}/i2c_dev 2>/dev/null; then {set_}"
        f'echo "presence=$(cat {PARAMS}/presence)"; else echo "no""tof"; fi'
    )


def parse_presence(out):
    """The millimetres of the console's report (0 for nobody), None for no sensor; Error for
    neither."""
    m = None
    for m in PRESENCE_RE.finditer(out):
        pass
    if m:
        return int(m.group(1))
    if re.search(r"^notof$", out, re.MULTILINE):
        return None
    raise Error(f"no answer about the motion sensor: {out!r}")


def light_command(channels=None):
    """The console line that reports the light sensor (light=CH0,CH1, or nolight without one),
    after setting it to channels (ch0, ch1) when given."""
    set_ = f"echo {channels[0]},{channels[1]} > {PARAMS}/light && " if channels else ""
    return (
        f"if grep -q light {PARAMS}/i2c_dev 2>/dev/null; then {set_}"
        f'echo "light=$(cat {PARAMS}/light)"; else echo "no""light"; fi'
    )


def temperature_command(celsius=None):
    """The console line that reports the SoC's temperature as fancontroller reads it
    (soc_temperature=N, or nothermal without /proc/thermalinfo), after setting it when given."""
    set_ = f"echo {celsius} > {PARAMS}/soc_temp && " if celsius is not None else ""
    return (
        f"if [ -r /proc/thermalinfo/soc_temperature ]; then {set_}"
        'echo "soc_temperature=$(cat /proc/thermalinfo/soc_temperature)"; '
        'else echo "no""thermal"; fi'
    )


def parse_light(out):
    """(ch0, ch1) of the console's report, None for no sensor; Error for neither."""
    m = None
    for m in LIGHT_RE.finditer(out):
        pass
    if m:
        return int(m.group(1)), int(m.group(2))
    if re.search(r"^nolight$", out, re.MULTILINE):
        return None
    raise Error(f"no answer about the light sensor: {out!r}")


def parse_temperature(out):
    """The temperature of the console's report, None without /proc/thermalinfo; Error for neither."""
    m = None
    for m in TEMP_RE.finditer(out):
        pass
    if m:
        return int(m.group(1))
    if re.search(r"^nothermal$", out, re.MULTILINE):
        return None
    raise Error(f"no answer about the SoC's temperature: {out!r}")


def _console(vm, line):
    from ..access import console
    from .. import paths

    state, detail = vm.state()
    if state != base.RUNNING or detail or not vm.pid():
        raise Error(f"VM {vm.name}: it is not running ({state} {detail})".rstrip())
    try:
        return console.send(
            line,
            wait=5,
            sock=os.path.join(vm.run_dir, "console.sock"),
            log=vm.console_log,
            tree=paths.Tree(vm.library.repo),
        )
    except OSError as error:
        raise Error(f"console transport failed: {error}") from None


def light(vm, channels=None):
    """{"name", "sensor", "light"}: the VM's light sensor's channels [ch0, ch1] after setting them
    to channels when given; Error if it has none and channels were given."""
    now = parse_light(_console(vm, light_command(channels)))
    if now is None and channels:
        raise Error(f"VM {vm.name}: its board has no ambient light sensor (digitalEye off)")
    if channels and now != tuple(channels):
        raise Error(f"VM {vm.name}: the sensor did not take {channels[0]},{channels[1]}: {now}")
    return {"name": vm.name, "sensor": now is not None, "light": list(now) if now else None}


def temperature(vm, celsius=None):
    """{"name", "thermalinfo", "soc_temperature"}: what the VM's /proc/thermalinfo/soc_temperature
    reads after setting it to celsius when given; Error if it has none and celsius was given."""
    now = parse_temperature(_console(vm, temperature_command(celsius)))
    if now is None and celsius is not None:
        raise Error(f"VM {vm.name}: its firmware reads no /proc/thermalinfo (no fancontroller)")
    if celsius is not None and now != celsius:
        raise Error(f"VM {vm.name}: /proc/thermalinfo/soc_temperature reads {now}, not {celsius}")
    return {"name": vm.name, "thermalinfo": now is not None, "soc_temperature": now}


def presence(vm, mm=None):
    """{"name", "sensor", "presence"}: the millimetres of the person in front of the VM's ToF motion
    sensor (0: nobody) after setting them to mm when given; Error if it has none and mm was given."""
    now = parse_presence(_console(vm, presence_command(mm)))
    if now is None and mm is not None:
        raise Error(f"VM {vm.name}: its board has no ToF motion sensor (supportMotionDetectSensor)")
    if mm is not None and now != mm:
        raise Error(f"VM {vm.name}: the sensor did not take {mm} mm: {now}")
    return {"name": vm.name, "sensor": now is not None, "presence": now}
