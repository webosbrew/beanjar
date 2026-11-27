# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The screen rotation of a model that turns to portrait (targets.Soc.portrait: the StanbyME).

On the set the panel itself turns, and webOS reads which way from the set's accelerometer: its
motion sensor service, com.webos.service.msc, samples the BMA422 (lgemu accel.c here; sensor())
and turns Home, the running apps and the compositor's views by the orientation it derives, which
it publishes (getOrientationInfo, the compositor's own subscription). webOS's option.screenRotation
stays off.

`beanjar sensor set rotate NAME ANGLE` turns the set itself: it sets the sensor's gravity vector (gravity())
over the console and keeps the angle beside the VM's state disks (SAVED), which the next start
gives the sensor again (launch_params()). What webOS makes of it is the firmware's: N21D's webOS 24
(ombre) msc refuses reversed_landscape and keeps the orientation it had at 180, its webOS 6
(kisscurl) takes it (REFUSED).

The guest is the truth: the initramfs's guest/initramfs/rootfs/bin/beanjar-rotation subscribes to
msc's getOrientationInfo and writes each orientation msc reports as its angle (ORIENTATIONS) to the
webos.rotation port, a line "rotation=N" (N 0, 90, 180 or 270), into run/rotation (QEMU truncates
it at each start). rotate waits for that line; the viewers read its last line and turn the picture:
the guest's framebuffer stays the panel's landscape, as the real panel's does, and a viewer shows it
turned as webOS turned its UI (picture_turn), its touches and clicks mapped back into panel pixels.
"""

import os
import time

from .. import base
from ..base import Error
from ..firmware import targets

ANGLES = (0, 90, 180, 270)
STATUS = "rotation"  # run/rotation: msc's orientation as an angle, a line at each report
WAIT = 30  # seconds rotate waits for the guest to report the new orientation (TCG: slower)
SETTLE = 3  # seconds rotate watches a turn the firmware refuses, for a report it does not expect
SAVED = "sensor-rotation"  # in the VM's state directory: the angle the set stands at (sensor())
ACCEL_MG = "/sys/module/lgemu/parameters/accel_mg"  # the sensor's vector, in the initramfs
# The accelerometer's gravity vector, mg along the chip's x, y, z, for the set turned to each angle
# (90 is the set a quarter counter-clockwise). msc's calOrient4BAM422 reads -y as landscape, -x as
# portrait, +y as reversed_landscape and +x as reversed_portrait, and the compositor draws portrait
# turned 270, reversed_landscape 180 and reversed_portrait 90 (StarfishImageSplash.qml), so:
# g = (sin a, -cos a, 0) g.
GRAVITY = {0: (0, -1000, 0), 90: (1000, 0, 0), 180: (0, 1000, 0), 270: (-1000, 0, 0)}
# msc's orientations and the angle the compositor turns the UI by for each (StarfishImageSplash.qml);
# beanjar-rotation's table, which reports them.
ORIENTATIONS = {"landscape": 0, "reversed_portrait": 90, "reversed_landscape": 180, "portrait": 270}
# The angles a release's msc does not take, keeping the orientation it had: N21D's webOS 24
# checkOrientation ("don't support reversed_landscape"); its webOS 6 has no such check.
REFUSED = {"ombre": (180,)}


def picture_turn(angle):
    """How far a viewer turns the panel's picture clockwise for the guest's rotation angle: as far
    as the set itself is turned. The compositor draws the UI turned clockwise by the angle into the
    landscape framebuffer (measured: at 90 the text runs down the panel), which is upright on a
    panel turned that far counter-clockwise; so 90 is the set turned a quarter counter-clockwise,
    270 a quarter clockwise."""
    return (360 - angle) % 360


def clockwise(angle):
    """The angle after the set is turned a further quarter clockwise (0, 270, 180, 90, 0)."""
    return (angle - 90) % 360


def parse(text):
    """The angle of the last "rotation=N" line of text (run/rotation), None if there is none."""
    for line in reversed(text.splitlines()):
        key, sep, value = line.strip().partition("=")
        if sep and key == "rotation" and value.isdigit() and int(value) in ANGLES:
            return int(value)
    return None


def current(run_dir):
    """The guest's rotation as it last reported it (run/rotation), None before the first report
    (not started, msc not up yet, a model that does not turn)."""
    try:
        with open(os.path.join(run_dir, STATUS), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            return parse(f.read().decode(errors="replace"))
    except OSError:
        return None


def angle_value(word):
    """A CLI angle: 0, 90, 180 or 270 (off is 0)."""
    word = str(word).strip()
    if word == "off":
        return 0
    if word.isdigit() and int(word) in ANGLES:
        return int(word)
    raise ValueError("the angle is 0, 90, 180 or 270")


def gravity(angle):
    """The sensor's gravity vector (x, y, z in mg) for the set turned to angle."""
    return GRAVITY[angle]


def sensor(target):
    """Whether the target's board has the accelerometer its firmware turns the UI by (lgemu's
    i2c_dev has an accel device: targets.SOCS lm21u)."""
    return any(
        p.startswith("i2c_dev=") and "accel:" in p for p in (targets.soc(target).lgemu or ())
    )


def saved(state_dir):
    """The angle the set was last turned to (SAVED), 0 (standing) when it never was."""
    try:
        with open(os.path.join(state_dir, SAVED)) as f:
            return angle_value(f.read())
    except (OSError, ValueError):
        return 0


def save(state_dir, angle):
    os.makedirs(state_dir, exist_ok=True)
    tmp = os.path.join(state_dir, SAVED + ".tmp")
    with open(tmp, "w") as f:
        f.write(f"{angle}\n")
    os.replace(tmp, os.path.join(state_dir, SAVED))


def launch_params(target, state_dir):
    """lgemu's parameters for a start: the sensor at the saved angle, () without a sensor."""
    if not sensor(target):
        return ()
    return ("accel_mg=" + ",".join(str(v) for v in gravity(saved(state_dir))),)


def portrait(vm):
    """Whether vm's model turns to portrait (VM.portrait: targets.Soc.portrait of its target)."""
    return vm.portrait()


def require(vm):
    """Error unless vm's model turns and the VM runs with its rotation port."""
    if not portrait(vm):
        board = ""
        try:
            board = f" ({vm.target()[0]})"
        except Error:
            pass
        raise Error(
            f"VM {vm.name}: its model{board} does not turn to portrait; only a model whose "
            "firmware supports it (the StanbyME) can be rotated"
        )
    if not sensor(vm.target()):
        raise Error(f"VM {vm.name}: its board has no accelerometer to turn")
    state, detail = vm.state()
    if state != base.RUNNING or detail or not vm.pid():
        raise Error(f"VM {vm.name}: rotate needs it running ({state} {detail})".rstrip())
    if not os.path.exists(os.path.join(vm.run_dir, STATUS)):
        raise Error(
            f"VM {vm.name}: it was started without the rotation port (an older launch): restart it"
        )


def refused(target, angle):
    """Whether the target's firmware keeps its orientation when the set is turned to angle
    (REFUSED)."""
    return angle in REFUSED.get(target[1], ())


def rotate(vm, angle, wait=WAIT, settle=SETTLE):
    """Turns vm's set to angle (its accelerometer) and waits until the guest reports msc's new
    orientation. {"name", "rotation", "previous", "changed", "refused"}: rotation is what the guest
    reports, which for an angle its firmware refuses stays what it was."""
    require(vm)
    previous = current(vm.run_dir)
    target = vm.target()
    turn_sensor(vm, angle)
    if refused(target, angle):
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline and current(vm.run_dir) == previous:
            time.sleep(0.2)
        now = current(vm.run_dir)
        return {
            "name": vm.name,
            "rotation": now,
            "previous": previous,
            "changed": previous != now,
            "refused": True,
        }
    deadline = time.monotonic() + wait
    while current(vm.run_dir) != angle:
        if time.monotonic() >= deadline:
            raise Error(
                f"VM {vm.name}: the sensor took the turn, but the guest has not reported msc at "
                f"{angle} in {wait} s (run/{STATUS}; /run/beanjar-rotation.log in the initramfs)"
            )
        time.sleep(0.2)
    return {
        "name": vm.name,
        "rotation": angle,
        "previous": previous,
        "changed": previous != angle,
        "refused": False,
    }


def turn_sensor(vm, angle):
    """Sets the running VM's accelerometer to the set turned to angle, in the initramfs (lgemu's
    accel_mg), and keeps the angle for its next start."""
    from .. import paths
    from ..access import console

    x, y, z = gravity(angle)
    try:
        out = console.send(
            f"echo {x},{y},{z} > {ACCEL_MG} && cat {ACCEL_MG}",
            wait=5,
            sock=os.path.join(vm.run_dir, "console.sock"),
            log=vm.console_log,
            tree=paths.Tree(vm.library.repo),
        )
    except OSError as error:
        raise Error(f"rotate: console transport failed: {error}") from None
    if f"{x},{y},{z}" not in out.split():
        raise Error(f"VM {vm.name}: the sensor did not take {x},{y},{z} ({ACCEL_MG}): {out!r}")
    save(vm.state_dir, angle)


def status(vm):
    """{"name", "portrait", "rotation", "saved"}: rotation is the guest's last report, None if none
    yet (or not running); saved is the angle the set stands at, kept for its next start (SAVED), which
    is what a VM that is off will show, None for a model that does not turn."""
    turns = portrait(vm)
    return {
        "name": vm.name,
        "portrait": turns,
        "rotation": current(vm.run_dir) if turns and vm.pid() else None,
        "saved": saved(vm.state_dir) if turns else None,
    }
