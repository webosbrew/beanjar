# Beanjar Viewer

Beanjar's desktop viewer (application ID `org.webosbrew.beanjar.viewer`): a GTK 4 window for QEMU's
D-Bus display (docs/dev/architecture/viewer.md). Run it **on the QEMU host**:
imported dmabuf file descriptors cannot cross SSH or another machine. The guest picture is drawn
in a `GtkGLArea` by the viewer's C core (`libbeanjar-viewer-core.so`: the D-Bus listener, the OSD
over the video windows, LG's cursor, decoded-media presentation, audio, input); the window around it (libadwaita),
a status bar with CPU/Disk/Net and the TV remote in a window of its own, is Python with PyGObject
(`beanjar_ui/viewer/`). Guest
audio and the media audio play through SDL audio. The same core without a window,
`viewer-headless` (SDL's offscreen driver), serves the tests and scripts.

## Build and run

On Debian, install `libsdl2-dev libglib2.0-dev libegl-dev libgles-dev pkg-config`
and a C compiler. The per-VM media helper decodes with the private FFmpeg that the
host build downloads (`packaging/vendor.toml`, docs/dev/guides/build.md, "The prebuilt programs");
the viewer has no FFmpeg dependency. SDL 2.26 or newer is required. The window needs GTK 4 and libadwaita for
Python: `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 blueprint-compiler` (GTK 4.12 and libadwaita 1.7 or newer; Debian 13
on both machines: 4.18 and 1.7.6), at build time too, for its layouts (below); without it the viewer builds and runs headless only. The viewer is part
of the host build (`meson.build` here, docs/dev/guides/build.md), which publishes `build/viewer/`:
`beanjar-viewer` (the launcher), `beanjar_ui/viewer/`, `beanjar-viewer.gresource` (the layouts),
`libbeanjar-viewer-core.so` and `viewer-headless` (each replaced by a rename, so a running
viewer is not disturbed):

```sh
scripts/build.sh host
LAYERS=normal,gl DISPLAY_BACKEND=dbus host/scripts/vm-launcher start
build/viewer/beanjar-viewer "unix:path=$PWD/build/dbus.sock" --remote
```

Run the last command in the host desktop session (Wayland or X11). The VM itself remains
headless. The guest needs the updated initramfs and soc layer and the shared launcher
arguments, which attach a virtio keyboard, an absolute tablet and the remote's `webos.ir` port. `beanjar-viewer` runs `viewer-headless` in its place for
`--headless`, `--replay`, `--screenshot`, `--screenshot-every`, `--osd-alpha` and `--no-osd`
(below), before anything imports GTK, so hosts without GTK run every test.

The window is a libadwaita window in the dark style: a compact header bar with the VM's name and
firmware (from the library VM's directory; "webOS" for the checkout's own VM) between the remote's
**Back**, **Home** and **Settings** keys (sent as the remote's IR buttons) and **Save Screenshot**
(the guest picture only, as PNG in `~/Pictures` or `~`), **Fullscreen** and the **Remote** toggle;
the picture; the status bar. Messages (the screenshot saved) are toasts over
the picture. F11 toggles fullscreen, which hides the header bar and the status bar; like the Menu
key, F11 is not sent to the guest. Nothing in the chrome takes keyboard focus.

The picture area starts at 1280x720 (`--size 960x540` changes it) with the status bar under it;
the window can be resized freely. The TV image always keeps its aspect ratio, with black bars
where necessary. Input coordinates use the displayed image rectangle, including HiDPI scaling.
Clicks in the bars are ignored; an active drag clamps to the image edges. Resizing the viewer
does not change the guest resolution.

- Mouse movement, left/middle/right buttons and vertical wheel go to the tablet.
- Every key but F11 and Menu goes to the guest, taken before GTK's own handling (a capture-phase key
  controller on each of the viewer's windows), so Tab, Space, Enter, F10 or Alt never move focus,
  press a button or open a menu. The keys for the remote's own functions are the remote's buttons, sent as its
  IR (see "PC keyboard" under TV remote): Home or Super is HOME, Escape is BACK, the volume and
  mute keys are VOL and MUTE. Every other key is a keyboard key.
- The PC Menu key (Application key) toggles the TV remote; see TV remote.
- Arrows and Enter navigate and select as on a TV with a USB keyboard. Common letter, digit,
  punctuation and modifier keys use physical US keyboard positions (the evdev code of the key: the
  hardware keycode - 8).
- Held keys and buttons are released on focus loss and exit. Desktop shortcuts intercepted by the
  compositor or window manager cannot reach the viewer; Home avoids the usual Super-key conflict.
- The viewer draws LG's cursor itself (libim's stock artwork, animations, hotspot and
  visibility) on top of the OSD and the video, scaled with the picture (device pixels, so HiDPI
  too) and clipped to it. While the host pointer is over the TV image the cursor is drawn at that
  pointer, which is the absolute position the tablet sends, so it moves with no guest round trip;
  otherwise it stays where libim last put it. Screenshots include it. It is not drawn while the
  TV is in standby (see **Standby** below).
- Over the displayed TV picture, the desktop pointer is a transparent 1×1 texture cursor, even when
  webOS hides its cursor or the cursor channel is disconnected. The normal host pointer remains
  over black bars, controls and the remote, and returns when the pointer leaves the picture.
  Only `webos.cursor` supplies displayed cursor pixels; QEMU `CursorDefine`/`MouseSet` calls are
  acknowledged and ignored.

RealVNC Viewer can display a local fallback dot when the remote cursor is invisible.
To suppress that dot, in the connection's **Properties → Expert**, set `DotWhenNoCursor=FALSE` and keep
`UseLocalCursor=TRUE`; the latter's `FALSE` mode displays both local and remote cursors.
These are client settings documented in the
[RealVNC parameter reference](https://help.realvnc.com/hc/en-us/articles/360002254618-RealVNC-Viewer-Parameter-Reference).

The cursor is the guest's cursor plane: LG's gm puts libim's frames on it, and the soc layer's
libdile_gal (guest/layers/soc/h13) sends its picture, visibility and libim's position through the
`webos.cursor` virtio-serial port to `build/cursor.sock`. The viewer uses the socket next to the
D-Bus socket, or `--cursor-socket PATH`; the launcher accepts `CURSOR_SOCK`.
Only one viewer can own this cursor connection. Stereo cursor modes are not
implemented. No firmware cursor assets are bundled with the viewer.

## Layouts

The static layout of the windows is in Blueprint files, `ui/*.blp`: the window (an
`Adw.ApplicationWindow`: the header bar, the toast overlay), the pane (the TV picture over the
status bar, and the menu), the status bar and one of its fields, and the remote's
window. Each is the template of a class in `beanjar_ui/viewer/` (`@Gtk.Template`);
the Python fills in the TV picture, the remote's keys (generated from the key table in
`remote.py`), the status bar's numbers and all behaviour. The host build compiles them with
blueprint-compiler (Debian's, docs/dev/guides/build.md) into `beanjar-viewer.gresource`, which the viewer
loads at start; blueprint-compiler checks every widget and property against the installed GTK, so
a layout that needs a newer GTK than the host's does not build.

To try out an edited layout without a build, give the viewer the directory of `.blp` files; it
compiles them at start with blueprint-compiler:

```sh
build/viewer/beanjar-viewer "unix:path=$PWD/build/dbus.sock" --remote --ui-dir host/ui/data/viewer
```

`--ui-dir` also takes a directory of compiled `.ui` files (all of them), e.g. compiled by hand:
`blueprint-compiler batch-compile /tmp/ui host/ui/data/viewer host/ui/data/viewer/*.blp`.
`meson compile -C build/host` puts the edit in `build/viewer/beanjar-viewer.gresource` for good.

## Status bar

Along the bottom, refreshed once a second; each field's LED is
lit when its counter moved, and hovering a field says where the number comes from, or why there
is none:

- **CPU**: QEMU's CPU time from `/proc/<pid>/stat`, as a percentage of one host CPU (as `top`);
  the tooltip has the vCPU count (the launchers name QEMU's threads, `-name debug-threads=on`).
- **GPU**: a meter beside the CPU's, the busiest engine of the VM's host GL (a gl-layer guest on
  `-display dbus,gl=on` or `egl-headless` with a render node), from the kernel's DRM fdinfo of the
  processes in the VM's unit (`host/core/beanjar/gpu.py`); the tooltip has every engine and the
  resident memory. A driver without engine counters (WSL's d3d12 through `/dev/dxg`) leaves it on,
  usage unavailable.
- **Memory**: one meter of the guest's own memory: a bar for its RAM in use (MemTotal less
  MemAvailable, from the virtio-balloon's statistics: QMP `qom-get` of
  `/machine/peripheral/balloon` `guest-stats`) and, where the guest has swap (webOS 6 and later),
  a second bar for its swap in use (SwapTotal and SwapFree, which the initramfs's
  `virtio-meminfo` writes to the `webos.memory` port, read with QMP `ringbuf-read`). The tooltip
  has both, "Swap: none" for a guest without swap, and then what the VM holds on the host: its
  unit's `memory.current` against what the admission reserved for it (the unit's `memory.high`,
  else `capacity.reservation_mib` of QEMU's `-m` and `-accel`), with the page cache of it, and
  `memory.swap.current` against the host's swap; without the unit's memory controller
  (`VM_SYSTEMD=0`, or a kernel booted with `cgroup_disable=memory` as the Raspberry Pi's) QEMU's
  `VmRSS` and `VmSwap` from `/proc/<pid>/status`.
- Meter colours: CPU green, GPU purple, memory blue (libadwaita's palette).
- **Disk**: read and write rates of all drives, from QMP `query-blockstats` on the viewer's own
  QMP socket, `viewer-qmp.sock`.
- **Net**: the guest's download and upload rates, from `tap0` in QEMU's network namespace
  (`/proc/<pid>/net/dev`, mode=mitm); mode=direct and link=off have no host-side counters.

The pid is the launcher's `qemu.pid`; it and `viewer-qmp.sock` are looked up next to the D-Bus
socket (the launcher, `host/core/beanjar/qemu.py`, writes both on every start path; `QEMU_PIDFILE`,
`VIEWER_QMP_SOCK`). A VM started otherwise shows `—` with the reason. It is coloured as a
libadwaita bottom bar (the header bar's colours), with dimmed labels and the success colour for a
lit LED. The Remote toggle and the window's buttons are in the header bar. Nothing in the bar takes keyboard
focus. `--no-statusbar` hides it.

**Standby** (Quick Start+, docs/dev/architecture/suspend.md): the same QMP connection asks `query-chardev`
each second; while the guest holds its `webos.standby` port open (chardev `standby`), the TV is in
standby: the remotes' POWER is QMP `system_powerdown`, the micom's wake line, instead of an IR key
(the micom reports no keys in standby). The panel is off then, and also in the micom's warm
standby (`micom-status` `power=2`: webOS's Active Standby after Power Off, the SoC running): the
picture is black and LG's cursor is not drawn, as the guest never sends its hide. After the wake
it is what the guest last sent.

## The TV's own key and front LED

On a model with a local key (the set's own button), the header bar starts with it, left of Back.
Its power icon mirrors the set's front LED: red (`#e53935`) when lit, the normal icon colour when
off or unavailable, with no glow. The joystick's header button uses the same tint. There is no
separate LED by the title or in the status bar. The launcher decodes the
model's `tv.model.localKeyType` from the board's `ToolOpt=` as the firmware itself does
(`lls.local_key`) and writes it to `vm-info.toml` as `local_key`:

- webOS 7 on: with the firmware's `etc/factorymanager/db/optionTable.json` (the item's word,
  offset, width and value names);
- webOS 5 and 6 (jhericurl, kisscurl): with factorymanager's tool option layout
  (`factorymanager.db.tooloption.json`, `toolOpt4.localKey`) and the names of its
  `LOCALKEY_TYPE_STRING_ENUM`;
- webOS 3.4 to 4.5: with where tvservice reads it, `nvm.local_key` of the release file in `guest/targets/release/` (from
  tvservice's DWARF types); webOS 1 and 2 have no such option and map the key as a joystick only.

Decoded on the default targets (2026-10-13): onekey on W18R, W19P and every webOS 5 and later target;
joystick on WT1H, W15M, W16R and W17H. None decodes ninekey or sevenkey, and no firmware here has
a value for "no key"; the key is hidden only where nothing is decoded.

- **onekey** (the W22O C2): one button. A click is a short press, holding it a long press. The
  viewer sends only the button's edges (`wv_local_key`, records with `WEBOS_LOCAL_MAGIC` on
  `ir.sock`, include/webos-ir.h); lgemu's micom tells a short press from a long one (1 s,
  `lgemu.local_long_ms`), as the set's micom does, and reports them as its local key events, not
  as IR (guest/kernel/modules/lgemu/i2c.c, "the local key"). With the set on, a short press opens
  the firmware's one-key menu (`com.webos.app.tvhotkey`: Power Off, Inputs, volume and channel;
  "Press: Move, Press and Hold: OK") and moves its focus; a long press chooses the focused item.
- **joystick, ninekey, sevenkey**: a popover with up, left, press, right and down, the micom's codes
  0x00-0x03 and 0xEA. The codes are channel up and down and volume up and down (920-923, measured
  on W17H), and the press opens the Local Control Panel; held, it powers the set off. Which
  physical direction sends which code is not in the firmware: up and down as channel, right and
  left as volume is inferred from LG's usual joystick layout and not verified on a set. The webOS 1
  to 4 joysticks (WT1H, W15M, W16R, W17H) use it.
- **In standby**: in the micom's warm standby with the SoC running (webOS's Active Standby) the
  key goes to the micom, which reports it (unlike the remote's keys); with the SoC suspended
  (Quick Start+) it is the micom's wake line, QMP `system_powerdown`, as the remote's POWER.
- **The front LED colour on the TV key** reads
  `micom-status` next to `qemu.pid` each second: the guest's micom state, a line at each change (its standby, power state and the last payload of each LED command
  the firmware wrote, `beanjar_ui/viewer/stats.py`, `front_led()`). Off while the set is on; red in standby
  (warm standby or suspended) with the standby light setting on; off when that setting is off.
  An unwritten setting uses the micom's default, on. CSS classes `led-red`, `led-off` and
  `led-unknown` on the key control carry the state; only `led-red` tints its power icon.
  The tooltip keeps the key's action description and appends the LED state, including
  `Front LED: unavailable` when there is no micom state (no QEMU).

## TV remote

`beanjar_ui/viewer/remote.py` provides firmware-specific LG remotes and the shared
service remote; their data (keys, colours, IR codes) is `beanjar/remote_layouts.py`, without
GTK, which the web viewer's remote also draws. For an instance with `vm-info.toml`, the chin menu offers only
its **webOS Remote**, **NetCast Remote**, **SimpleSmart Remote** or **Japanese Remote** (a webOS
board whose broadcast is ARIB, `vm-info.toml`'s `broadcast`), plus **Service Remote**. With no firmware metadata, standalone previews retain all choices.
The choice lasts for the viewer's run; there is no viewer settings file.
`--remote-layout auto|normal|netcast|simplesmart|japan|service` selects the initial
layout; an incompatible explicit choice is refused when the firmware is known.

The header controls and Home/Super shortcuts follow the firmware, even while
its service remote is selected. NetCast's dashboard control uses `0x42`, its
apps-grid icon and NetCast label; webOS and SimpleSmart use their existing Home
key. The legacy platforms label the settings control Menu. SimpleSmart keeps
the normal IR mapping previously used for its successful launcher test
([SimpleSmart](../../docs/dev/architecture/simplesmart.md)).

- **webOS**: the layout of `build/remote-reference.html` (48 buttons, positions, sizes, colours and
  corner radii as given, in CSS pixels, on a 188-wide panel; 188x652).
- **NetCast**: the numeric/D-pad layout with NetCast, Q.MENU, RATIO, AV MODE,
  SIMPLINK and teletext keys. Codes come from LM14 US 04.11.18 RELEASE
  (`); unresolved APP/* and ENERGY SAVING labels are inactive.
- **Japanese**: the webOS remote with a Japanese (ISDB) remote's keys, for an ARIB board (188x700):
  the broadcast types (TER, BS, CS, BS4K) under POWER, the number pad 1-12 in place of 1-9, LIST,
  0 and FLASHBACK, and d (data broadcasting), DISPLAY and 3-DIGIT above the volume row. LG's
  physical Japanese remote has not been found in public sources, so the keys are placed on the
  normal remote rather than copied from one.
- **Service**: LG's factory service remote, "Factory SVC Remocon" MKJ39170828, measured from a
  photo of it (188x764; 62 buttons, the names printed on its body, the boxed direct-input keys and
  "Factory SVC Remocon" on the chin).

All are scaled with the panel; icons are the theme's symbolic icons (Adwaita), with a text
fallback. The body is shaped like LG's own remotes: a slim, nearly rectangular bar with small
corner radii (16/10 normal, 5 service) and a chin at the bottom where LG prints its logo.

The colours follow photos of the real remotes, sampled and rounded to a small palette (the
constants at the top of `remote_layouts.py`; every colour on the bodies and keys is one of them):

| Swatch | Hex | Used for |
|---|---|---|
| `BODY_FACE` | `#222222` | the normal body, chin included |
| `BODY_RIM` | `#444444` | the normal body's glossy edge; the chin controls' rings |
| `KEY_DARK` | `#111111` | INPUT, digits, LIST, 0, FLASHBK, SAP, SLEEP, STOP; the service remote's black keys |
| `KEY_LIGHT` | `#CCCCCC` | CAPTION/?/SEARCH/TV, VOL and CH rockers, FAV/INFO/MUTE, NETFLIX/HOME/prime video, the D-pad group (MENU, arrows, OK, LIVE ZOOM), BACK/EXIT, the playback row; the service remote's P-ONLY, ETC, P-CHECK-S, grey round keys, playback keys, X STUDIO, D-pad disc, rockers |
| `LABEL_ON_DARK` | `#EEEEEE` | labels on dark keys and the power keys |
| `LABEL_ON_LIGHT` | `#333333` | labels on light keys, VOL and CH on the rockers |
| `PRINT` | `#888888` | QUICK ACCESS and MAGIC LINK printed on the normal body; a chin control under the pointer; the thin ring of the service remote's black keys |
| `CHIN_PRINT` | `#666666` | the chin's controls, as faint as LG's printed logo |
| `POWER_RED` | `#EE3344` | the normal power key; the service remote's red key |
| `RED` `GREEN` `YELLOW` `BLUE` | `#EE3333` `#11AA66` `#EEAA77` `#3388DD` | the normal colour keys; `RED` is also ETC's text |
| `NETFLIX_RED`, `AMAZON_BLUE` | `#CC1111`, `#1166AA` | the brand text on the streaming keys |
| `SVC_FACE`, `SVC_RIM` | `#3A3A3A`, `#555555` | the service body and its edge, the input box's border |
| `SVC_PRINT` | `#BBBBBB` | the names printed on the service body and chin |
| `POWER_TEAL` | `#117799` | the service power key |
| `SVC_GREEN` `SVC_YELLOW` `SVC_BLUE` | `#99DDAA` `#EEDD66` `#44BBEE` | the service remote's paler colour keys |

A held key is its face mixed 25% toward black (light) or 30% toward white (dark); dimmed keys
(below) are drawn at 40% opacity.

- It is always a window of its own, "Remote", or hidden; it is never docked into the TV window,
  which keeps the picture and the status bar only and never changes size for it. The header
  bar's Remote toggle and the PC Menu key show and hide it; `--remote` shows it at start. On X11
  it first appears right of the TV window; on Wayland the compositor places it.
- The window stays above the TV window and is 1.125x as tall as the picture (1.125x to 2.25x the
  layout: the normal remote 212x734 to 423x1467), following the picture's size: taller than the
  picture beside it (734 px beside the default 540 px picture, 1125 px beside a 1000 px one). The Remote toggle
  is on exactly while the remote is on screen, however it was shown, hidden or minimized. It is
  shaped like the physical
  remote: no title bar or frame, transparent around the body, and clicks in the transparent
  corners go to whatever is under them (the window's input region is the body). Drag the body,
  chin included, anywhere but a key or control to move it. Switching layouts resizes the window
  to the other remote's proportions and its input region to the other body.
- The chin holds the window's own controls, drawn faint like a printed logo so they are not taken
  for TV keys; they never send anything to the guest. The right one (x) closes the remote's window, as closing it from the desktop or
  turning the Remote toggle off does (the toggle or the Menu key opens it again). The left
  one opens its menu: **Normal Remote** / **Service Remote** (a radio choice) and
  **Place Next to TV** (X11 only; hidden on Wayland, where the compositor places windows). While
  the menu is open the keyboard is the menu's.
- Escape (not sent to the guest while that window has focus; the remote's BACK is the guest's
  back key) and closing the window from the desktop hide it too.
  Transparency needs a compositor: under a bare X server (Xvfb) the corners are drawn black.
- Mouse-down on a button sends the button down, mouse-up (anywhere; the press holds the pointer)
  its release, so holding a button is a long press: the micom repeats it (below). The held button
  is drawn lighter (a light one darker).
- Keys typed while the remote window has focus still go to the guest, except Escape. Focus loss,
  hiding, switching layouts and exit release what is held.
- Buttons WT1H has no key for are drawn dimmed and send nothing (a click prints why).
- `--remote-preview OUT.ppm [--remote-layout normal|netcast|simplesmart|japan|service] [--remote-press KEY] [--seconds N]`
  renders only the remote, with no VM, at 2x (376x1304 normal, 376x1528 service), to `OUT.ppm`
  (KEY drawn held). It needs a display: `xvfb-run -a` in scripts (with `GDK_BACKEND=x11` where
  `WAYLAND_DISPLAY` is set).

### Key codes

Every button is a button of an LG infrared remote: NEC coding, custom (address) code 0x04, and a
data code per button. The data codes are LG's (openlgtv's OPENRELEASE `include/keys.h` lists
them); on WT1H, tvservice maps each to a Linux key with its own table (`_gKeyTable`, 317 16-bit entries indexed by the data code), and the
compositor's QPA (`libqstarfishegl.so`, two tables: one of 76 LG entries and a factory table of
`Key_webOS_Factory*` keys, then Qt's default evdev map) makes the Qt key; a Linux code in none is dropped. The
Linux column is measured: every row was sent through the micom and seen on tvservice's `LGE RCU`
(`tests/regress/input/check-ir.py --keys`). The Qt column is the QPA's tables; the colour keys were seen in
a Qt app (`tests/regress/input/check-viewer.py`).

Normal remote:

| data-key | IR | Linux key | Qt key on WT1H |
|---|---|---|---|
| POWER | 0x08 | KEY_POWER 116 | Key_webOS_PowerOnOff (tvservice powers the set off: the VM powers off) |
| INPUT_HUB | 0x0B | KEY_VIDEO_NEXT 241 | Key_webOS_InputSource |
| CC | 0x39 | 0x31f | Key_Subtitle |
| GUIDE ("?") | 0xAB | KEY_PROGRAM 362 | Key_webOS_TVGuide |
| LIVETV ("TV") | 0x0F | KEY_TV 377 | Key_webOS_InputTV |
| 1-9, 0 | 0x11-0x19, 0x10 | KEY_1..KEY_9, KEY_0 | Key_1..Key_9, Key_0 |
| LIST | 0x53 | 0x313 | Key_webOS_ProgramList |
| FLASHBACK | 0x1A | 0x304 | Key_webOS_ChannelBack |
| VOLUMEUP / VOLUMEDOWN | 0x02 / 0x03 | KEY_VOLUMEUP 115 / KEY_VOLUMEDOWN 114 | Key_VolumeUp / Key_VolumeDown |
| CHANNELUP / CHANNELDOWN | 0x00 / 0x01 | KEY_CHANNELUP 402 / KEY_CHANNELDOWN 403 | Key_webOS_ChannelUp / Down |
| FAVORITES | 0x1E | KEY_FAVORITES 364 | Key_webOS_Favorite |
| INFO | 0xAA | KEY_INFO 358 | Key_webOS_Info |
| MUTE | 0x09 | KEY_MUTE 113 | Key_VolumeMute |
| HOME | 0x7C | 0x305 | Key_Super_L |
| MENU | 0x43 | KEY_MENU 139 | Key_webOS_Settings |
| UP / DOWN / LEFT / RIGHT | 0x40 / 0x41 / 0x07 / 0x06 | KEY_UP 103 / KEY_DOWN 108 / KEY_LEFT 105 / KEY_RIGHT 106 | Key_Up ... |
| ENTER (OK) | 0x44 | KEY_ENTER 28 | Key_Return |
| BACK | 0x28 | KEY_PREVIOUS 412 | Key_webOS_Back |
| EXIT | 0x5B | KEY_EXIT 174 | Key_webOS_Exit |
| SAP | 0x0A | 0x31e | Key_webOS_FactoryMultiSoundSetting |
| TIMER ("SLEEP") | 0x0E | 0x31b | Key_Sleep |
| STOP / PAUSE / PLAY | 0xB1 / 0xBA / 0xB0 | KEY_STOP 128 / KEY_PAUSE 119 / KEY_PLAY 207 | Key_MediaStop / Pause / Play |
| REWIND / FASTFORWARD | 0x8F / 0x8E | KEY_REWIND 168 / KEY_FASTFORWARD 208 | Key_AudioRewind / Key_AudioForward |
| RED / GREEN / YELLOW / BLUE | 0x72 / 0x71 / 0x63 / 0x61 | KEY_RED..KEY_BLUE 398-401 | Key_webOS_Red ... Blue |
| SEARCH, NETFLIX, AMAZON, LIVE_ZOOM | none | | dimmed |

The dimmed keys belong to later remotes: `_gKeyTable` has no entry for their codes, and WT1H's QPA
has no search, Key_webOS_Netflix, Key_webOS_Amazon or Key_webOS_LiveZoom mapping.

Service remote, its own keys (POWER, INPUT, CAP, MPX, the colour keys, PLAY, PAUSE, MENU, EXIT,
the D-pad, VOL, MUTE, CH and the digits send the codes above; ⏮/⏭ are REWIND 0x8F and
FASTFORWARD 0x8E):

| Key | IR | Linux key | Qt key on WT1H |
|---|---|---|---|
| P-ONLY | 0xFE | 0x387 | Key_webOS_FactoryPowerOnly |
| ARC | 0x79 | 0x31d | Key_webOS_AspectRatio |
| PSM | 0x4D | 0x320 | Key_webOS_FactoryPictureMode |
| P-CHECK-S: P / S | 0xFC / 0xFD | 0x384 / 0x386 | Key_webOS_FactoryPictureCheck / SoundCheck |
| SSM | 0x52 | 0x321 | Key_webOS_FactorySoundMode |
| PIP | 0x60 | 0x31a | Key_webOS_FactoryPip |
| TEXT | 0x20 | 0x30b | Key_webOS_Teletext |
| TILT | 0xF9 | 0x393 | Key_webOS_FactoryTilt |
| EYE | 0x95 | 0x381 | Key_webOS_FactoryEyeQ |
| ADJ | 0xFF | 0x388 | Key_webOS_FactoryAdjust |
| IN START / IN STOP | 0xFB / 0xFA | 0x383 / 0x382 | Key_webOS_FactoryInStart / InStop |
| TV (by RGB) | 0xD6 | 0x3b3 | Key_webOS_FactoryTv |
| AV1 / AV2 | 0x5A / 0xD0 | 0x3b6 / 0x3b7 | Key_webOS_FactoryVideo1 / Video2 |
| COMP1 | 0xBF | 0x3b9 | Key_webOS_FactoryComponent1 |
| HDMI1 / HDMI2 / HDMI3 | 0xCE / 0xCC / 0xE9 | 0x3bf / 0x3c0 / 0x3c1 | Key_webOS_FactoryHdmi1..3 |
| ETC, B-TOOTH, X STUDIO | unknown | | dimmed: no code established for WT1H |
| SIMPLINK, AV3 | 0x7E, 0xD1 | 0x30a, 0x3b8 | dimmed: no Qt key on WT1H |
| RGB, COMP2, HDMI4/DVI | 0xD5, 0xD4, 0xDA | (0x3bc, 0x3ba, 0x3c2) | dimmed: tvservice drops them, the model has no such input |

Japanese remote, its own keys (the others send the normal remote's codes; 10 is 0x10, KEY_0). The
codes are the firmware's key tables' (`docs/research/arib-data-broadcasting.md`, "JP remote
infrared codes"); the Linux keys were seen on W22O 43.21.89 (o22 queue, Japanese board):
micomservice's `input keycode` / `output keycode` and the compositor's `IM_KEY_INPUT` from `LGE
RCU` in `/var/log/messages`.

| Key | IR | Linux key | Name in the firmware |
|---|---|---|---|
| DATA ("d") | 0x9F | 885 | Key_webOS_BMLData (kept for country JPN only) |
| DISPLAY | 0xAC | 886 | Key_webOS_JapanDisplay |
| THREE_DIGIT | 0x23 | 884 | Key_webOS_3DigitInput |
| TERRESTRIAL / BS / CS / BS4K | 0xDB30 / 0xDB00 / 0xDB10 / 0xDB40 | 871 / 832 / 845 / 1055 | the broadcast-type keys |
| 11 / 12 | 0xDB3B / 0xDB3C (as sent: type + 0x0B / 0x0C) | TER 882 / 883, BS 843 / 844, ... | Key_webOS_TER_11 and so on |

The broadcast-type keys and 11 and 12 are extended codes: two bytes, 0xDB N, where a data code is
the code and its complement (`include/webos-ir.h`, `WEBOS_IR_EXT`); the host writes them as
0xDB00 | N, which `beanjar remote press` and `remote pipe` take as numbers too (`0xdb30`). As on
the real remote, 11 and 12 are sent for the broadcast type selected last, terrestrial before any
(the viewer core, `ir.h`): the keys carry terrestrial's codes, and after BS the 12 key sends
0xDB0C. 1-10 send the ordinary digit codes. Digit keys are pressed by code from the CLI, since a
number there is a code: `remote press NAME 0x11` for 1.

The printed names that are not keys (INFO/STILL/HDMI HOT/USB HOT over the colour keys, PIP PR-,
PIP PR+, SWAP and PIP INPUT under them, TIME SHIFT by the arrows, PAGE by CH) are older chassis'
second functions of those keys; the keys send their own codes. tvservice drops the direct-input
keys of inputs the model lacks before injecting anything (`_MICOM_CheckInvalidDiscreteKeyValue`,
from the tool options' input counts: here one component input, three HDMI, no RGB-PC).

What WT1H does with them in a normal boot (2026-09-28): the factory keys reach inputcommon's
FactoryMode (P-ONLY, IN START, IN STOP, ADJ, TILT, P/S-CHECK, HDMI check, PIP), which forwards
them to `com.webos.service.tvwin/createKeyEvent`; ls-hubd starts tvwin, LG's factory UI, on demand.
tvwin draws its menus (the In-Start password prompt) with gm on a window of its own, which the
emulator does not display (only the compositor's picture is the OSD plane), so IN START, ADJ, EYE,
PSM and P-CHECK show nothing. AV1 switched to the AV input ("No Signal, please check the input connection", then "Open
input list?"); TV came back to Live TV.

### Delivery path

The viewer's core (`ir.c`) writes each button edge as a 4-byte record (`include/webos-ir.h`:
`'I'`, down/up, custom code, data code; a Japanese remote's extended code is an `'E'` record with
N as its code) to `ir.sock` next to the D-Bus socket (`--ir-socket`;
the launcher's `IR_SOCK`), the chardev of the `webos.ir` virtio-serial port. In the guest, the
initramfs's IR pump (`guest/initramfs/src/virtio-ir.c`) copies the port to lgemu.ko's micom model
(guest/kernel/modules/lgemu/i2c.c), which queues the micom's own events, and MICOM_Task reads them with
its 40 ms poll over the I2C bus (WT1H: I2C ch 2, slave 0x52, command 0xC0, 3 bytes): `02 code ~code` for a button down, `0A code ~code` for its release, and
while it is held `03 code ~code` (an extended code `DB N` in place of `code ~code`), first 500 ms after the press, then every 108 ms (the NEC repeat
frame rate). tvservice checks the code, maps it, and injects the Linux key on its own uinput
device `LGE RCU` (bus USB, 0001:0001, version 4, EV_KEY for 0..0x4ff and EV_REL; value 1 press,
2 repeat, 0 release), which libim reads, as on the set. It also only hands keys to the UI while
the micom reports the power state on (0xB1 = 3). Repeats are tvservice's to filter: it passes
them for OK, the arrows, VOL, PAUSE, REWIND, FASTFORWARD, CH and the colour keys and logs the rest
as not registered. The port works with a stock QEMU (the Pi's): no QEMU key codes are involved.

`tests/regress/input/check-ir.py` checks the path on a running VM (a tap, a long press with repeats, and
with `--keys` every key of a layout).

### PC keyboard

| PC key | Sent as |
|---|---|
| Super (either), Home | the remote's HOME, IR 0x7C (Key_Super_L: the launcher) |
| Escape | the remote's BACK, IR 0x28 |
| Volume up / down, Mute | the remote's VOL+, VOL-, MUTE, IR 0x02 / 0x03 / 0x09 |
| Menu (Application key) | not sent: shows and hides the remote |
| every other key | QEMU's virtio keyboard (D-Bus `Keyboard`), as a keyboard |

The keyboard is not a remote in the guest: nothing grabs or remaps it, and libim reads "QEMU
Virtio Keyboard" as a HID keyboard ("HID Keyboard detected"), as a USB keyboard on a TV: letters
for text entry, arrows, Enter, Backspace. Only the remote's own functions go the remote's way, so
that Home, Back and the volume behave as the remote's buttons do (Home opens the launcher, the
volume goes through tvservice to PulseAudio).

## Audio

With `DISPLAY_BACKEND=dbus`, the launcher gives the guest a playback-only
`virtio-sound-pci` backed by QEMU's D-Bus audio (`AUDIO=dbus`, the default with that display):
44.1 kHz s16 stereo, fixed, 441 frames (10 ms) per message. `audio.c` registers an
`org.qemu.Display1.AudioOutListener` on its own peer connection and queues the
PCM to SDL.

- The SDL queue starts playing at 40 ms (prefill). QEMU has no backpressure, so
  past 150 ms (cap) whole 10 ms chunks are dropped until the queue is back at
  60 ms (target); a stalled viewer costs one skip, not growing latency.
- A queue that runs dry while the stream is enabled counts an underrun and waits
  for the prefill again. `SetEnabled(false)` (the guest stopped the stream) lets
  the queue drain, so the end of a sound is not cut off.
- `SetVolume` is applied (per channel, mute) to s16 and f32; virtio-sound only
  ever sends full volume. The guest's volume is set in PulseAudio (`tv.sound` stub).
- `--no-audio` skips the listener. `--audio-wav FILE` records exactly what QEMU
  sent, before volume. `--headless` records and counts but plays nothing, unless
  `--sdl-audio` is given as well. Only SDL's audio subsystem is initialised, also inside
  the GTK window (on WSLg it reaches WSLg's PulseAudio server, `PULSE_SERVER`).
- This device and the media mix's are opened from different threads (main, and a media
  stream's reader). SDL 2 does not guard its audio init or device open against that, so both
  go through `core/sdl-audio.c`, which serialises them: unguarded, a guest already playing
  media when the viewer connects crashed it in `SDL_InitSubSystem`.
- A VM without D-Bus audio prints `audio: none (...)` and the viewer carries on.
  If the audio connection closes, the viewer keeps showing the display.
- On exit it prints `audio: writes= frames= (s) rms= dBFS max_gap= ms
  enabled_changes= volume_changes= dropped= underruns= wav_bytes=`.

Headless checks (`tests/regress/audio/check-audio.py`: header, FFT peak, RMS, duration,
silence gaps; `--make-tone` writes a test WAV):

```sh
build/viewer/beanjar-viewer "unix:path=$PWD/build/dbus.sock" --headless --seconds 12 \
  --audio-wav build/viewer-audio.wav &
python3 tests/regress/audio/check-audio.py --make-tone build/share/out/tone-1k-3s.wav --seconds 3   # the VM's output share
host/scripts/vm-launcher console 'webos-enter aplay -q -D hw:0,0 /mnt/host/out/tone-1k-3s.wav' 5
python3 tests/regress/audio/check-audio.py build/viewer-audio.wav --seconds 3 --min-rms -20
# SDL path, with SDL's disk driver instead of a sound card:
SDL_AUDIODRIVER=disk SDL_DISKAUDIOFILE=build/sdl.raw SDL_DISKAUDIODELAY=12 \
  build/viewer/beanjar-viewer ... --headless --sdl-audio --seconds 12
python3 tests/regress/audio/check-audio.py build/sdl.raw --raw 44100:2:s16 --seconds 3 --min-rms -20
```

SDL 2's disk driver sleeps a whole number of milliseconds per 512-frame buffer
(11 ms by default, 11.6 ms is real time), so it consumes 5 % fast and the viewer
correctly reports underruns; `SDL_DISKAUDIODELAY=12` makes it slightly slow,
which the queue absorbs over a short test. Capture (microphone) is not
implemented: QEMU 10.0's D-Bus capture is unsafe without an In listener, and
the guest card has no capture stream (`streams=1`).

## Video

`video.c` (in `core/`, like every C file below) reads the guest's media-window table through the VM helper at `build/video.sock`
next to the D-Bus socket (`--video-socket PATH`; the launcher takes `VIDEO_SOCK`). The helper
owns the QEMU-side `qemu-video.sock` for the `webos.video` virtio-serial port. The writer is LG's tvservice, through the video scaler HAL of `guest/layers/soc/h13`:
one context per scaler window, `MAIN` or `SUB`, from videosinkmanager, tv.display, tv.avblock and
tv.power's AV mute, with the scaler input it is connected to. The wire format is in
`include/webos-video.h`: the whole table of contexts in every frame, repeated every second.

`core/media.c` presents, `core/media-ipc.c` transports and `core/media-frame.c` owns received
frames for the decoded output from the per-VM `media-helper` at `build/media-<n>.sock`
(`--media-socket PREFIX`, default `<dir of the D-Bus socket>/media`; the launcher takes
`MEDIA_SOCK`). The helper owns QEMU's `webos.media.0`..`7` compressed ports and all FFmpeg
video/audio decoding and resampling. Guest caps, compressed buffers and lifecycle still use
`include/webos-media.h`; the helper/viewer boundary uses the versioned `SOCK_SEQPACKET` decoded
protocol in `include/host-media.h`, with timestamped pictures/PCM and FDs passed by `SCM_RIGHTS`.
Native software planes and PCM use immutable sealed memfds; DRM_PRIME pictures carry real
object/layer/plane descriptors. Scheduling, GL conversion/composition and PCM playback remain
in the viewer.

The helper continues decoding without a viewer and retains a current picture for attachment
or relaunch, including while paused. Four output leases bound viewer retention. The
transport and lifetime contract (docs/dev/architecture/video.md)
describes clock/generation snapshots, complete-output drops, GPU-complete releases, and bounded
hardware quarantine with real download fallback after an unclean disconnect. `MEDIAHUB=0`
turns decoding off (its `vm-info.toml` has `media = "off"`, which the core takes as `--no-media`);
raw compressed sockets cannot be fed directly to this viewer.

- A connected, not muted context with a window shows the decoded picture of the video stream on
  its input (a VDEC window's port is the stream's `vdec-ch`; `MAIN` also takes the only video
  stream there is), stretched to the window, scaled like the OSD and under it, so it shows only
  where the OSD is alpha 0. Without a picture (none yet, between streams, a codec the helper
  cannot decode) the window is black, as on a TV. Only with decoding off (`--no-media`, a
  `MEDIAHUB=0` launch) does it get the milestone 1 checkerboard
  (60 guest pixels per square, light grey and dark blue), marking video that is not decoded.
  Live TV's windows (the tuner's decoders, external inputs) come as muted. `fullScreen` windows
  are 1920x1080.
- Pictures and samples are presented at the guest pipeline's clock (base time + running time),
  mapped to the host's monotonic clock; a paused pipeline freezes the picture, a seek shows the
  prerolled frame, the last picture stays after EOS until the window goes.
- The helper defaults to `--hwdec auto` (the launcher's `MEDIA_HWDEC` setting), selecting an
  available FFmpeg device for the codec and falling back to software. `none` selects software;
  `TYPE` restricts the device type. Viewer `--hwdec auto|none|TYPE` sends a preference which the
  helper acknowledges for the next codec-open boundary; an active GOP continues unchanged.
  Hardware availability and import support depend on the actual host and codec.
- Media audio arrives already decoded/resampled as 48 kHz float stereo and plays through its own SDL device,
  next to the guest's PulseAudio stream (the OS mixes the two); `--media-latency MS` is the output
  latency to compensate (default: two SDL buffers). Headless without `--sdl-audio`, or when no
  device opens, a thread consumes it in real time instead, as it does with `--media-silent` in a
  window too (heard nowhere: `scripts/regress --tv --viewer`'s decode step). `--media-wav OUT.wav`
  records the mix (48 kHz float stereo). Media audio does not go through the guest's volume:
  compressed data always flows at standard volume, and the viewer's virtual AMP (`--media-volume`,
  `--media-mute`) is the only gain on its final mix.
- The helper's `RUN_DIR/media-helper.log` (rotated to `media-helper.log.1` at 4 MiB,
  rate-limited per line kind and port: docs/dev/architecture/video.md) records codec selection,
  lifecycle and decoder statistics (`decode N video session=S: h264 sw packets= decoded= dropped= errors= decode=...`).
  The viewer records window changes and presentation reports (`present N video session=S:
  received= shown= picked= late_drops= lateness avg= max= ms pts=...`, `present N audio
  session=S: frames= slip avg= max= ms resyncs= nudges= underruns=...`). S is the helper's
  session number for the port (it moves on at every OPEN and CLOSE), so one stream's decode and
  present lines join on port and session even when streams of other codecs reuse the port.
  Exit counters `video_packets= video_changes= video_draws= video_pictures=` and viewer CPU time
  remain viewer measurements. Tests read both logs rather than treating presentation counters
  as decoder work.
- `WEBOS_MEDIA_PROBE=1` logs when a white frame is picked for display and when a beep's first
  sample is due at the output (the A/V sync clip in `).

Checks: `tests/regress/video/check-media.sh CLIP [SCRIPT]` (a clip through LG's pipeline in the windowed
test app, with pause and seek; `VIEWER=none` plays it with the viewer already running, e.g. a
windowed one with sound) and `tests/fakes/fake-media.py` (fake guest compressed streams through
an owned real helper, with separate `media-helper.log`; no VM needed besides a display to
connect the viewer to).

## Rendering

The window's `GtkGLArea` asks for a GLES context, which GTK 4.18 makes with EGL on Wayland
and X11 alike (dmabufs are imported on EGL only; on a GLX context, `GDK_DEBUG=gl-glx`, the
Unix.Map path below still works). On WSL (`/dev/dxg`, no `/dev/dri`) the viewer sets
`GALLIUM_DRIVER=d3d12` unless it is set: Mesa 25 otherwise draws with llvmpipe. The core draws the scene into the area's framebuffer in GTK's `render`; D-Bus scanouts
arrive between renders, and the core asks the area to make its context current first
(`GtkGLArea.make_current`), then asks for a redraw. Frames are paced by GTK's frame clock.

The listener imports scanouts with `EGL_EXT_image_dma_buf_import` and their DRM
modifiers on the same GPU as QEMU. Each update is copied on the GPU into a
viewer-owned texture before acknowledging QEMU, so resize/expose redraws cannot
race with reuse of a shared scanout. Old EGL images and descriptors are released.
There is no CPU readback in normal rendering. Basic 32-bit pixman Scanout/Update
is supported for the boot display before the GL scanout appears.

The listener also implements `org.qemu.Display1.Listener.Unix.Map` and lists it in its
`Interfaces` property. `ScanoutMap` passes a memfd, which the viewer maps read-only;
each `UpdateMap` uploads the rectangle from it with `glTexSubImage2D` (whole rows of
`a8r8g8b8` as `GL_BGRA_EXT` when `GL_EXT_texture_format_BGRA8888` is there, converted
otherwise) before replying, since QEMU reads its next frame into the same memory once
the reply is in. Stock QEMU uses it only for 2D surfaces. The patched QEMU
(beanjar-vendor's, `build/qemu`) also sends GL scanouts this way when its EGL display cannot
export dmabufs, as on WSL2 (below); the surface is `a8r8g8b8`, so the OSD keeps its
alpha and the video shows through as with a dmabuf.

### WSL2 (WSLg)

WSL2 has no `/dev/dri`, so the dmabuf path cannot work at either end. With the patched
QEMU (beanjar-vendor's, which the host build publishes as `build/qemu`) virgl renders
on the Windows GPU through Mesa's d3d12 driver, QEMU reads each GL update back into
shared memory, and the viewer draws it in a WSLg window:

```sh
scripts/build.sh host                     # the viewer, and the patched QEMU published
LAYERS=normal,gl DISPLAY_BACKEND=dbus NET_LINK=off host/scripts/vm-launcher start
build/viewer/beanjar-viewer "unix:path=$PWD/build/dbus.sock"
host/scripts/vm-launcher stop
```

The OSD is composited as premultiplied alpha by default (Qt Quick convention), over the
video windows.
`--straight-alpha` is available for sources using straight alpha; the firmware's
full alpha convention remains a separate investigation. The renderer honors the
scanout's orientation.

D-Bus input calls are asynchronous, with errors reported. A display disconnect
closes the viewer; restart it after restarting QEMU. It does not start/stop VMs.
The D-Bus interface contract is documented in the installed QEMU manual,
`/usr/share/doc/qemu-system-common/interop/dbus-display.html`.

## Screenshots

`beanjar screenshot NAME FILE` (host/core/README.md) asks the running window to save a PNG through
`org.beanjar.Display1.Screenshot` on the VM's bus, which a viewer window or beanjar-vnc serves
([design](../../docs/dev/architecture/viewer.md#screenshots-from-a-script)). Without a front end it runs
`--headless --screenshot OUT.ppm --screenshot-after MS`, which takes the picture MS after the first
scanout and exits.

## Headless validation

`viewer-headless` (run by `beanjar-viewer` for these options) has no window and no chrome;
SDL's offscreen driver creates an EGL context without opening a desktop window:

```sh
build/viewer/beanjar-viewer "unix:path=$PWD/build/dbus.sock" \
  --headless --seconds 5 --size 960x540 \
  --replay tests/regress/input/pointer.replay --screenshot build/viewer.ppm
python3 tests/regress/input/check-viewer.py [RUN_DIR [LOG_DIR]]   # a library VM: its run/ and logs/
python3 tests/regress/input/check-ir.py [--keys [service]] [RUN_DIR [LOG_DIR]]
```

The integration test requires Normal/gl booted and runs a temporary QML probe
inside the VM. It covers SDL events through D-Bus and virtio into the pointer bridge's device and
the virtio keyboard, the remote's buttons through `webos.ir` and the micom into tvservice's
`LGE RCU`, app-level motion/click/wheel/key delivery (a keyboard letter and the colour keys),
focus-loss release, resizing and black bar click rejection. `check-ir.py` checks the remote's
path alone (see TV remote). It leaves captures in `build/`. Stop the VM after testing,
or stop the probe using its `/run/viewer-probe.pid` from the initramfs shell.

Replay lines are `milliseconds operation arguments`, relative to the first
scanout. Operations: `motion X Y`, `down BUTTON`, `up BUTTON` (SDL buttons 1/2/3),
`wheel STEPS`, `keydown NAME`, `keyup NAME` (SDL scancode names; the PC keyboard, so Home is
the remote's HOME), `irdown CODE`, `irup CODE` (a remote button, its LG IR data code, e.g. 0x72, or an extended code, 0xdb30),
`localdown CODE`, `localup CODE` (the set's own key, its micom local key code: 0xea the one key or a
joystick's centre, 0x00-0x03 a joystick's directions; include/webos-ir.h), `blur`, and `resize WIDTH HEIGHT`; `--replay` always runs headless. `--screenshot` writes the final
scaled picture (rendered offscreen at the drawable size, guest only) as PPM. Headless runs until
`--seconds N`, SIGTERM/SIGINT or the display going away.
`--screenshot-every MS` also writes `OUT-<ms after the first scanout>.ppm` during the run. The
window's own equivalent is `--capture OUT.ppm --capture-every MS` (the same files, the composite
at 960x540 whatever the window's size), since `--screenshot` selects the headless program. `--capture-control FILE` (with
`--capture-every MS` in a window, `--screenshot-every MS` headless) captures only while FILE
exists, into the OUT.ppm its first line names, at 480x270, the ms counted from when it appeared:
one viewer for many recordings (scripts/regress's decode step).
`--hide-planes video,ui,cursor` (any of them, comma-separated) leaves those planes out of the
picture and its screenshots, headless or in the window, whose More menu shows or hides each (UI,
Video, Cursor) for that window only; the guest's capture and `beanjar screenshot` always have all
of them. A hidden video plane is black in the UI's hole; a hidden UI leaves the video alone.
`--osd-alpha` writes the guest's OSD as received, at scanout size and with its alpha, next to
each screenshot as `OUT.ppm.osd.pam` (`tests/regress/video/check-hole.py` reads both). `--no-osd` draws
the video plane alone.

Window and cursor regressions, on a private Xvfb display with no VM and no desktop interaction:

```sh
meson test -C build/host --suite viewer   # check-cursor, check-gtk
```

`check-cursor` checks the cursor channel: BGRA conversion, guest visibility, that unchanged
heartbeat images are not uploaded again, and that the pointer is to be transparent only over the
displayed image independent of guest visibility, shape and connection (never over bars or
controls). QEMU cursor messages must be acknowledged without drawing. GTK checks also cover
letterbox edges, stationary-pointer resize, enter/leave and blur. `check-gtk` (`tests/integration/viewer/check-gtk.py`, skipped without GTK 4 for Python) runs the GTK
window against `tests/fakes/fake-qemu.py`, a stand-in D-Bus display that logs every input call,
and drives it with XTest: the layout, the pane being the picture and the bar only, `--remote`
showing the remote's window at start right of the TV window, status bar clicks that hide and show
it and never reach the guest, the TV window never changing size, the transparent pointer over the
picture and the arrow over the bar, keys reaching the guest (Tab, Space, F10, Return and letters
to the keyboard; Super, Home, Escape and the volume keys as the remote's IR buttons, which
`fake-qemu.py` logs from its `ir.sock`), the Menu key hiding and showing the remote, and a
guest-only screenshot. For the remote's window: a click and a long press send the button's IR
code down and up, undecorated, its X input
shape is the body's outline, small corners and chin included (a press in a corner reaches the
picture below), a drag on the body or the chin calls `begin_move` and moves it (no window manager:
GDK moves it itself), keys typed in it reach the guest, the Menu key, the chin's close control,
Escape and a close request hide it without sending anything, the chin's menu opens with its
five items, each of which works (Escape closes only the menu), with nothing sent to the guest,
and Service Remote switches the layout (window proportions, input region, IN START sending 0xFB, a
dimmed key sending nothing) and Normal Remote back. At exit the viewer prints `ir_sent=` and `ir_errors=` (remote
button edges), `cursor_draws=` (frames drawn with LG's cursor) and `cursor_blank_sets=` (times the pointer was made transparent); the GTK window adds `keys=` and
`remote_presses=`.

SIGINT/SIGTERM: the first one ends the viewer cleanly (held keys released, counters printed); a
second one, or 5 s without the clean exit, ends it at once.
