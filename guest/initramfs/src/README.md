# Guest input

The guest gets four kinds of input from the host, each the way a TV gets it:

- **The TV remote** (infrared on a real set) comes through the micom, as on a TV. The viewer sends
  each button's LG IR code over the `webos.ir` virtio-serial port; `virtio-ir.c`, the IR pump the
  initramfs starts, copies the port's stream to `/dev/lgemu-ir` (the kernel cannot read a virtio
  port itself), and the micom model in lgemu.ko (guest/kernel/modules/lgemu/i2c.c) reports it to
  tvservice's MICOM_Task over the I2C bus; tvservice injects the Linux key on its own uinput
  device, `LGE RCU` (`MICOM_UINPUT_Init`). Errors go to `/run/virtio-ir.log`. See
  `host/viewer/README.md`, "TV remote".
- **The PC keyboard** is QEMU's `virtio-keyboard-pci`, left as it is: libim reads "QEMU Virtio
  Keyboard" as a HID keyboard ("HID Keyboard detected"), as a USB keyboard on a TV. The viewer
  sends the remote's own functions (Home, Back, volume) as the remote's IR buttons instead.
- **The Magic Remote's pointer** is `virtio-pointer.c`, below: a bridge program, built
  statically for arm64 by the guest build (`meson.build`) into the initramfs (as the IR pump is),
  which starts it before webOS. Errors go to `/run/virtio-pointer.log`.

- **The touch screen** of a model that has one (the StanbyME) is `virtio-touch.c`, below: QEMU's
  `virtio-multitouch-pci` given to webOS as LG's panel. Errors go to `/run/virtio-touch.log`.

Both launchers attach `virtio-keyboard-pci`, `virtio-tablet-pci` and the `webos.ir` port through
`host/core/beanjar/runtime/qemu.py`, and `virtio-multitouch-pci` for a touch model (`targets.Soc.touch`).

## Device names

`LGE RCU` is the remote's device on lk5900, up7560 and oledc5 too: bus USB (0003), vendor/product 0001, version 0004, EV_KEY for every code through
0x4ff and EV_REL with no relative axes. On WT1H tvservice creates it with exactly these values
and is its only creator; the codes above KEY_MAX 0x2ff need the kernel patch (docs/dev/guides/build.md,
"Guest kernel": LG's own kernels have KEY_MAX 0x500). Those TVs also expose `LGE M-RCU - Builtin
[n]`: [0]/[1] on lk5900 and [0]/[1]/[2] on up7560 and oledc5, with selected keys, REL_WHEEL and
ABS_X/ABS_Y; WT1H's lginput2 creates [0]..[2].

The devices of a running WT1H guest (Pi, 2026-09-28), with the processes that hold `/dev/uinput`
open: QEMU Virtio Keyboard, QEMU Virtio Tablet, `LGE M-RCU - Builtin [3]` (virtio-pointer),
CHECK INPUT (libim, in surface-manager), `LGE RCU` (tvservice), `LGE M-RCU - Builtin [0..2]`
(lginput2), Bluetooth-manager, LGE NLP (nlp), LGE AllJoyn Remote (ajservice). Until 2026-09-28 a
second `LGE RCU`, the virtio keyboard bridge (`virtio-remote`), stood beside tvservice's; it is
gone, and with it the stand-in keys it mapped.

Stock libim also contains named paths for LGE RCU, LGE M-RCU - Builtin,
Smart Remote RCU Input, LGE Smart Remote - TouchPad, LG Magic gamepad, LGE NLP,
LGE WiDi, LGE M-RCU - For GAME, LGE Finger Gesture, and the LGE Tellurium Input
Generator Keyboard/Mouse/Touch names. It has generic HID keyboard/mouse
detection too. Exact matching conditions for the other named paths have not been established
here.

## M-RCU pointer bridge and viewer

`virtio-pointer.c` creates `LGE M-RCU - Builtin [3]` before webOS starts. WT1H's
lginput2 creates indices 0..2; libim accepts 0..4, so index 3 avoids a collision.
The bridge grabs `QEMU Virtio Tablet`, rescales its absolute axes into the TV's
1920x1080 pixel coordinates, and forwards left/middle/right buttons and wheel.
It advertises only those supported capabilities, not a complete Magic Remote.
SYN_DROPPED reconciles axes and held buttons; source loss releases buttons.
W21A additionally selects `POINTER_WAKEUP=1` in its boot manifest. The bridge's
`--mrcu-wakeup` mode advertises and sends native cursor-on key `0x4ae` as a
press/release pulse and SYN_REPORT before each motion packet and each button press,
matching lginput2's custom-event sender, and then both axes in full: libim resets its
cursor position when cursor-on shows a hidden cursor, and the input core drops an
absolute value equal to the last one, so the bridge first sets its own device's last
value out of range (EVIOCSABS, no event). Without this, a click where the pointer
already was, after libim had hidden the cursor, reached native apps at (0,0).
Such a click now lands on its target, but the cursor it shows is drawn at the screen
centre until the next move (libim's reset appears to land after that position).
W21A's libim recognizes the builtin M-RCU but leaves its cursor disabled when only
coordinates arrive. This mode supplies its input
protocol; it does not change Bluetooth pairing state. Other profiles retain the
existing coordinate/button/wheel bridge.
Unlike the remote, this is not LG's path: on a set the Magic Remote's pointer reaches lginput2
over Bluetooth and lginput2's own M-RCU devices carry it; here it is a fourth M-RCU device.

The default matches WT1H's fixed OSD size. A custom guest can pass
`webos.input_size=WIDTHxHEIGHT` on its kernel command line, but it must also
configure the compositor's logical resolution to match. The viewer window size
is independent and can be changed freely.

## Touch screen

The StanbyME's panel is a USB HID touchscreen (the firmware's udev rules tag 1fd2:0205
`ID_TOUCHSCREEN`). Its reader is libim inside surface-manager, which takes a touch device by its
evdev name only: one containing `LGE Touch Device`, `eGalaxTouch` or `FlatFrog`. Any other name
falls through to its keyboard test and is closed ("Close device (EV_KEY type, but no KEY_Q)").
libim reads `ABS_MT_SLOT` (slots 0 to 9), `ABS_MT_TRACKING_ID` (-1 ends a contact) and
`ABS_MT_POSITION_X/Y`, ignores `BTN_TOUCH` and `ABS_X/ABS_Y`, and scales positions by the screen
size over the `EVIOCGABS` maximum of the position axes. It reads the input ID only as
information (no vendor or product test); PDM counts panel disconnects through `ID_TOUCHSCREEN`,
which nothing needs for touch to work.

`virtio-touch.c` starts before webOS when the machine has a `QEMU Virtio MultiTouch` (init looks
in `/sys/class/input`). It grabs that device and creates `LGE Touch Device` on `/dev/uinput`:
type B multitouch, `INPUT_PROP_DIRECT`, the source's position ranges (QEMU's 0 to 32767), 10
slots, USB 1fd2:0205 as the panel. It reports each frame as hid-multitouch does on a set: every
changed slot's `ABS_MT_SLOT`, `ABS_MT_TRACKING_ID` (its own counter) and position, then
`BTN_TOUCH` and the oldest contact as `ABS_X/ABS_Y`, then `SYN_REPORT`.

New contacts do not reuse their source slot. The kernel's input core drops an `EV_ABS` value
equal to the one it holds for that slot (`input_handle_abs_event`), and libim does not keep a
slot's position from one contact to the next: a new contact in a slot whose last contact ended at
the same spot reached libim with no position and did nothing (a second tap on the same icon,
measured by dumping the device). So the bridge works around the kernel's filter: a new contact
goes to a free slot whose last position differs on both axes, the least recently used one. A real
HID panel would hit the same thing only when a new contact in the same slot lands on exactly the
same raw coordinates as the last one ended, which a fine, noisy sensor makes rare (inferred; LG's
own panel was not measured), where QEMU gives every touch at a viewer's pixel the same value. Positions are the panel's own, in its native orientation:
rotation is the compositor's (`updateOutputGeometry`), not the bridge's.

QEMU gives button events to the first input handler that takes buttons, and the tablet and the
multitouch device both do; which comes first depends on the order the guest's drivers start. For
a touch model the launcher binds the tablet to the display (`display=gpu,head=0`), so the
console's mouse buttons stay the pointer's and its touches (multitouch events, which the tablet
does not take) go to the touch screen. QEMU's `BTN_TOUCH` of a touch may land on the tablet,
whose bridge ignores it; the touch bridge makes its own.

Measured on W21U 03.70.10 under TCG (2026-11-04): surface-manager logs `event4(67): LGE Touch
Device detected` and `Close device (EV_KEY type, but no KEY_Q) : QEMU Virtio MultiTouch`; a tap
on Home's settings icon opens Quick Settings, one on the Apps tile launches its app, and the
mouse's buttons still arrive on `LGE M-RCU - Builtin [3]`.

## Tests

On the VM host, with the VM booted (Normal) and no viewer attached (each socket takes one client):

- `python3 tests/regress/input/check-ir.py [--keys [service]] [RUN_DIR [LOG_DIR]]`: the remote's path,
  ir.sock to tvservice's `LGE RCU` (a tap, a long press with repeats; `--keys`: every key of a
  remote layout, checked against tvservice's key table).
- `python3 tests/regress/input/check-viewer.py [RUN_DIR [LOG_DIR]]`: the headless viewer's replay
  (`letterbox.replay`) through D-Bus, virtio and `webos.ir` into a Qt probe app: pointer, a
  keyboard letter and the remote's colour keys.

RUN_DIR/LOG_DIR default to `build`; for a library VM they are its `run/` and `logs/`.
