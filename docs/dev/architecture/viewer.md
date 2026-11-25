# the viewer as a GTK 4 application

Design from 2026-09-28. The desktop viewer (`host/viewer`) moves from an SDL2 window to a GTK 4
window. The guest picture is drawn in a `GtkGLArea`, and the chrome is made of real GTK widgets:
the status bar, the remote (in a window of its own) and the menu. The headless path used by the
tests stays a small C program without GTK. It replaces the earlier SDL chrome; the stats sources, launcher changes and UX rules below are
the specification implemented here.

## Decision: Python (PyGObject) for the UI, and a C core as a shared library

The work divides into two kinds:

- **The frame path.** This is everything that already exists in C and runs at frame rate or on
  other threads:
  - the D-Bus display listener: dma-buf import, `ScanoutMap` read-back, 2D updates, copying into
    the cache before the reply
  - composing the OSD over the video windows, and LG's cursor
  - scheduling decoded media pictures, EGL import of DRM frames and A/V sync
  - guest audio and media audio (SDL audio threads; for a front end that serves the sound
    itself, the mix on the core's audio clock, below)
  - input calls to QEMU.
- **The chrome.** This is window management, the status bar, the remote, menus, tooltips, the
  QMP/`/proc` stats and option parsing. None of it is on the frame path.

The chosen split is `libbeanjar-viewer-core.so` (C) for the first kind and the Python package
`beanjar_ui.viewer` (GTK 4 through PyGObject) for the second. The reasons:

- **The Pi does not need GTK development packages.** The C core does not include a single GTK
  header. It needs EGL, GLES, GIO and SDL2 (audio only); FFmpeg belongs to the per-VM media helper.
  The GTK side needs only the runtime packages `python3-gi` and `gir1.2-gtk-4.0`. A native GTK
  program would need `libgtk-4-dev` and its whole chain of `-dev` packages on every host that
  builds it.
- **It embeds in the manager.** The manager is GTK 4 with PyGObject (`), and the
  viewer's window content is a Python `Gtk.Widget` (`beanjar_ui.viewer.view.TvView` inside
  `beanjar_ui.viewer.window.ViewerPane`), so the manager can import it as a pane. A C GTK widget would
  need a GObject-introspection typelib (g-ir-scanner at build time) to be used from Python.
- **The UI is small in Python.** The status bar, the 48-button remote, the menu and the stats
  (`/proc` plus QMP through `Gio.SocketClient` and the stdlib `json`) are short Python. With the
  stats in Python, the C/json-glib stats module and its `libjson-glib-dev` dependency are dropped.
- **The frame path stays in C.** The listener, `media.c`, `audio.c`, `video.c` and
  `cursor.c` compose and present frames. Since 2026-10-09, decoding and media-audio resampling
  run in the per-VM helper described below.

What it costs, and how the cost is contained:

- **A generated ctypes boundary.** It is kept flat: about 25 functions taking ints, doubles and
  strings, declared in `core.h`; `tools/gen-ctypes.py` generates `beanjar_ui/viewer/_core_abi.py`
  from those declarations (`--check` fails when it is stale). `wv_abi()` returns a
  version number, and Python refuses a mismatched library.
- **Two languages in one tool.** The rule is simple: nothing on the frame path is Python. Python
  calls into C once per GTK render and per input event. C calls back into Python only for
  "redraw", "make the GL context current" and "quit".
- **The display runs on the GLib main loop.** QEMU's display callbacks, the 4 ms poll source and
  GTK share the default `GMainContext`. The guest AudioOutListener has its own thread/context,
  so graphics cannot hold up its Writes ([audio ownership](viewer-audio.md#listener-ownership)).
  The core's own threads (audio reception, decoded-media readers and playback) never call Python.

Pure native code (C with GTK 4) was the alternative. It would have one language and no ctypes, but
it needs `libgtk-4-dev` everywhere, it cannot be embedded in the manager without introspection, and
the chrome would be about three times as much code. Rust and C++ would mean rewriting the frame path.

## Versions and backends

| | x86 PC (Debian 13 since 2026-09-28, WSLg) | Pi 5 (Debian 13) |
|---|---|---|
| GTK | 4.18.6 (4.8.3 on Debian 12 before); `gir1.2-gtk-4.0`, `python3-gi` installed | 4.18.6; `gir1.2-gtk-4.0` installed by 2026-09-28 (no `-dev`, none needed) |
| PyGObject | 3.50 (3.42 before) | 3.50 (`python3-gi` installed) |
| libadwaita | 1.7.6, `gir1.2-adw-1` | 1.7.6, `gir1.2-adw-1` |
| Session | WSLg: Wayland (`WAYLAND_DISPLAY=wayland-0`) and X11 `:0` | none (tty; headless use only) |

- **GL.** GTK uses EGL on Wayland. On X11, GTK 4.8 used **GLX** unless `GDK_DEBUG=gl-egl`
  (measured: under Xvfb with `GDK_BACKEND=x11` the area's context was not EGL), so the launcher
  set that flag for GTK older than 4.12. GTK 4.18 uses EGL on X11 by itself, and the flag is gone
  (2026-09-28, without it: EGL on Xvfb on both machines, and on the Pi 148 dma-buf imports in 15 s
  from the KVM VM, V3D). The area asks for GLES (`set_allowed_apis(GLES)`, GTK 4.12), so the
  core's GLES 2 shaders and client-side vertex arrays are valid unchanged. On WSL, Mesa 25
  (Debian 13) picks llvmpipe unless `GALLIUM_DRIVER=d3d12`, which the launcher sets when there is
  `/dev/dxg` and no `/dev/dri` (measured: `D3D12 (Intel(R) UHD Graphics 770)` with it; the
  llvmpipe viewer took 59 % of a core). On a GLX context anyway,
  `eglGetCurrentDisplay()` is `EGL_NO_DISPLAY`: the core says so, a dmabuf scanout ends the
  viewer with that reason, and the Unix.Map and 2D paths (what WSL uses) still work.
- **dma-buf import.** `GdkDmabufTexture` arrived in GTK 4.14 (Debian 12's 4.8 lacked it). The
  core imports QEMU's scanout dma-bufs (and the decoder's DRM PRIME frames) with
  `EGL_EXT_image_dma_buf_import` on the current EGL display, which is GTK's. It copies them into
  its cache texture as before. Keeping this copy means QEMU can reuse its buffer as soon as the reply is
  sent, whatever GTK does with its own frame. On 4.14+ the OSD could be handed to GTK as a
  `GdkDmabufTexture` and offloaded, but the OSD is composited over video, so there is no gain yet.
- **The GL context from D-Bus callbacks.** `ScanoutDMABUF`, `UpdateDMABUF`, `ScanoutMap`,
  `UpdateMap`, `Scanout` and `Update` arrive outside GTK's render. Before any GL call the core
  calls a make-current callback, and Python implements it as `GLArea.make_current()`. This keeps
  GDK's idea of the current context right. The display is connected only after the area is
  realized.
- **`Disable` is provisional.** Under load QEMU sends a stray `Disable` between two page flips
  (its dmabuf release, [black flashes](../../research/pi-black-flashes.md)). So `Disable` only
  arms a deadline, `DISABLE_WAIT_US` (50 ms, `core/disable.h`), and keeps the picture and held
  input. A `ScanoutDMABUF`, `ScanoutMap` or `Scanout` before the deadline cancels it. The poll
  (`wv_poll`) applies a `Disable` that is still pending when the deadline passes: the picture
  goes black and the keys and buttons held through the display are released. A real `Disable`
  therefore shows black 50 to 54 ms late, and the stray one never reaches the picture.
  `wv_counter` has `disables` (received) and `disables_dropped` (cancelled by a scanout), and
  beanjar-vnc's `SIGUSR1` stats line prints them.
- **Rendering.** GTK binds the area's framebuffer and sets the viewport before `render`.
  `wv_render(w, h)` reads the bound framebuffer (`GL_FRAMEBUFFER_BINDING`), which is not 0 under
  GTK, and draws there. It rebinds that framebuffer after using its own cache framebuffer. Frames
  are paced by GTK's frame clock: the core's poll source invalidates the area (`queue_render`) when
  a scanout, cursor, video window or media frame changed, at most at the [output rate](#output-rate).
- **Latency.** The SDL viewer drew immediately with swap interval 0. GTK presents on the next
  frame-clock tick, which adds up to one refresh (16.7 ms at 60 Hz) of video latency against audio.
  Media is timed in `media_update()`, which picks the frame due now. A compensation (picking the
  frame due at the next vblank, from `GdkFrameClock`'s predicted presentation time) is possible
  later, if the A/V probe shows it matters. The headless path, which is what `check-media.sh`
  measures, is unchanged.

## Output rate

The VM's picture is made at most as often as the board's panel refreshes, as a TV's display engine
composes video and OSD at the panel's rate: 60 times a second on a 60 Hz panel, 120 on a 120 Hz
one, 144 or 240 on such panels; there is no ceiling but the panel's (and the host's, below). The
guest's UI itself never draws more than 60 frames a second, on its own clock (its flips complete
at a 60 Hz vblank, [build-and-boot](build-and-boot.md#the-displays-refresh)), so above 60 the UI
changes at most 60 times a second whatever the output's rate, and the video planes are what come
at the panel's rate: a 120 fps clip on a 120 Hz panel shows every picture, a 60 fps one each for
two, and on a 144 Hz panel each picture lasts the pictures its time covers.

- **The panel's refresh** is the board's, as the firmware decodes it: prepare
  (`host/core/beanjar/prepare.py`) keeps `tv.hw.json`'s `panelOutputFrameRate`, which the
  firmware's own `hw-option-gen` makes from the board's `hwopt=` when the EEPROM's records are made
  (`lls.panel_rate`, webOS 5 on; the digit and its table are the release's: digit 4, 60 or 120 Hz,
  on webOS 5 and 6, digit 9 later, with 100, 120, 144, 165, 180 and 240 Hz). Releases before
  webOS 5 have no such run and get 60. The launch writes it to `vm-info.toml` as `panel_hz`, which
  the core reads beside the D-Bus socket in `wv_start`; without it, 60.
- **The core paces** (`core/pace.h`): option `output-hz` (0 unpaced, default the panel's) sets
  the rate, and `wv_poll`, so the poll source's `redraw` callback and viewer-headless's loop,
  report a change only when a picture is due. Due times lie on a grid of the period (1 s / rate,
  in microseconds: no rate is assumed to be a multiple of 60) from the last picture drawn
  (`wv_render`, `wv_frame*`), so a poll's lateness is made up by the next picture and the rate
  holds; after a pause the grid restarts no sooner than half a period after the last picture, so
  nothing bursts. The poll runs every half period, at most every 4 ms as before and at least every
  1 ms, GLib's timer step (`pace_poll_ms`: 4 ms up to 125 Hz, 3 at 144, 2 at 240; above 500 Hz
  pictures come on 1 ms polls and only the mean rate holds). Media pictures are picked every poll
  by their pts (`media_update`); one replaced before it was drawn counts as `picked` but not
  `shown` in the `present` reports. Counters `panel_hz` and `output_hz`.
- **The GTK viewer** presents on its frame clock, so at the host display's refresh, and no more
  often than the output rate: a 60 Hz desktop shows a 120 Hz panel's video at 60, a 144 Hz desktop
  a 120 Hz panel's at 120 and a 240 Hz panel's at 144. The frame due at the next vblank (`GdkFrameClock`'s predicted presentation
  time, under Latency above) is not used: the media picture drawn is the one due at the poll
  before the draw.
- **viewer-headless** draws at the output rate (it drew at every 2 ms poll that found a change).
- **beanjar-vnc** paces its own frames by the same rule and lowers its rate when the host cannot
  keep it ([vnc.md](vnc.md#frame-rate)); it sets `output-hz` 0.

Measured 2026-11-12 with viewer-headless (30 s, `--no-audio`) on a looping 1280x720 H.264 clip
with its frame number burned in (`<video>` in a 960x540 window, the test app of
`tests/regress/video/app`), pictures from the exit line's `pictures=`, the media's from its
`present` report:

| | panel | clip | pictures a second | received | shown | picked, not shown | late drops |
|---|---|---|---|---|---|---|---|
| Pi 5, KVM, W22O (`o22-mullet`) | 120 Hz | 120 fps | 116.8 | 3598 | 3494 | 94 | 7 |
| the same | 120 Hz | 60 fps | 67.8 (the UI's bar too) | 1799 | 1792 | 4 | 0 |
| Pi 5, KVM, W22P (`k8lp-mullet`) | 60 Hz | 120 fps | 60.0 | 3597 | 1793 | 1791 | 10 |
| x86 PC, TCG, W22O | 120 Hz | 120 fps | 105.9 | 3536 | 3163 | 348 | 22 |

On a 60 Hz panel a 120 fps clip shows every other picture, as on such a TV. The PC's TCG guest
delivers fewer pictures on time and its D3D12 draws are slower: the misses there are the host's.
The GTK window was not measured at a real display's refresh (Xvfb has no vblank).

## Components

```
beanjar-viewer (Python launcher, build/viewer/beanjar-viewer)
 ├─ --headless / --replay / --screenshot* / --osd-alpha / --no-osd / --capture-sink  →  exec viewer-headless
 └─ otherwise  →  beanjar_ui.viewer (GTK 4)
      ├─ main.py     options, Gtk.Application, signals
      ├─ window.py   ViewerWindow (+ ViewerPane/RemoteWindow re-exports)
      ├─ pane.py     ViewerPane = TvView over StatusBar
      ├─ remote_window.py  RemoteWindow (the remote, a separate window)
      ├─ keys.py     capture-phase key handling for both windows
      ├─ view.py     TvView (Gtk.GLArea + pointer controllers + cursor)
      ├─ remote.py   RemotePanel: the 48-button table as widgets
      ├─ remote_layouts.py  the remotes' data (keys, colours, IR codes), no GTK: also the web viewer's
      ├─ statusbar.py, statusfmt.py, stats.py   CPU / Disk / Network icons, numbers in tooltips
      ├─ preview.py  --remote-preview
      ├─ ui.py       loads the layouts: beanjar-viewer.gresource, or --ui-dir
      └─ core.py     ctypes binding of libbeanjar-viewer-core.so
host/ui/data/viewer/*.blp   the layouts (Blueprint), built into beanjar-viewer.gresource
host/ui/bin/beanjar-viewer   the launcher (published as build/viewer/beanjar-viewer)
libbeanjar-viewer-core.so (C, host/viewer/core/): core.c (listener, scene, API)
                             input.c (incl. the evdev key map) cursor.c video.c media.c
                             audio.c pcm-out.c sdl-audio.c peer.c capture.c (sources.txt lists them all)
viewer-headless (C, SDL offscreen): core/headless.c (+ SDL scancode names for --replay)
```

### The C core API (`core.h`)

- **Setup.** `wv_new`, `wv_option(name, value)` takes the CLI names: `size`, `straight-alpha`,
  `cursor-socket`, `video-socket`, `capture-socket`, `media-socket`, `no-media`, `hwdec`, `media-wav`,
  `media-latency`, `media-volume` (0 to 2, 1.0 standard), `media-mute`,
  `no-audio`, `audio-wav`, `sdl-audio`, `headless`, `osd-alpha`, `no-osd`, `no-cursor`, `no-ir`,
  `output-hz`
  ([Output rate](#output-rate)).
  `wv_callbacks(make_current, redraw, quit, data)`. `wv_start()` needs the GL context current. It
  creates the GL objects, connects the display, and opens the cursor, video, media and audio
  sources. `wv_attach()` adds the 4 ms poll source to the default main context, for GTK.
  `wv_poll()` is one poll step, for the headless loop. `wv_attach()` also owns `org.beanjar.Display1` on the VM's bus, see
  [Screenshots from a script](#screenshots-from-a-script).
- **Drawing.** `wv_render(w, h)` draws into the bound framebuffer. `wv_view(lw, lh)` gives the
  logical size of the area, for input. `wv_picture(&x, &y, &w, &h)` returns the letterboxed picture
  in logical pixels. `wv_screenshot(path, w, h)` renders offscreen at w×h, so screenshots are
  guest-only by construction. With `--osd-alpha` it also writes the `.osd.pam`.
  `wv_frame(pixels, w, h)` composes the same scene offscreen into a caller's RGBA buffer, top row
  first, and counts as the render the `redraw` callback asked for, which is the core's "frame
  changed" signal. [beanjar-vnc](vnc.md) is built on these two.
- **LG's cursor for a front end that draws it itself** (`WV_ABI` 5). With `no-cursor` the scene is
  drawn without LG's cursor, and neither the pointer nor libim moving the cursor marks it changed;
  a change of the cursor's picture, hotspot, visibility or scale still does (the guest's capture
  keeps the cursor). `wv_cursor(rgba, &w, &h, &hot_x, &hot_y, &view_w, &view_h)` returns 0 while
  it is hidden, else a serial that changes with those; with a buffer it also copies the picture
  (straight-alpha RGBA) and gives the size it covers in the view, scaled like the TV picture.
  [beanjar-vnc](vnc.md#the-cursor) sends it as the clients' cursor. The GTK viewer does not use it.
- **Standby** (`WV_ABI` 7). `wv_standby(on)` says the TV's panel is off. While it is on the
  core skips LG's cursor in every render and `wv_cursor` returns 0: the guest never sends a hide
  (s2idle freezes it; in Active Standby it keeps running and still does not). After the wake the
  cursor is what the guest last sent. The counter `standby` reads it back. The GTK pane sets it
  from `stats.panel_off()`: QMP's `standby` chardev open (the SoC suspended) or `micom-status`
  `power=2` (the micom's warm standby, Active Standby after Power Off). lgemu's micom has only
  power states 3 (active) and 2; its power off (`25 00`) ends the VM.
- **No version check** (`WV_ABI` 9). `wv_check` (ABI 8), which compared the run's versions
  with the viewer's, is gone: versions are information only. The display, cursor, video and media
  protocols identify themselves (`WOSCUR1`, `WOSVID3`, the media helper's HELLO/ACK)
  (build-and-boot.md, "Host and guest compatibility").
- **The mix for a front end** (`WV_ABI` 10). `wv_audio_sink(fn, data)`, before `wv_start`, gives
  the sound to the front end instead of SDL: `pcm-out.c`'s thread, a 10 ms clock, mixes the media
  audio (`media_fill`, media.c's mixer, which then starts no clock thread of its own) with the
  guest's (audio.c's Writes through a tap, linearly resampled to 48 kHz through a FIFO that absorbs
  QEMU's timing) and calls `fn(data, pcm, 480)` with 48 kHz s16 stereo, the one call the core makes
  from a thread of its own. `wv_audio_wanted(on)` says whether anyone listens; while not, the
  media mix is still consumed on time and the rest skipped. [beanjar-vnc](vnc.md#audio) serves it
  as Opus or PCM. The GTK viewer and viewer-headless do not use it: their guest and media audio
  stay on two SDL devices, mixed by the desktop's sound server.
- **A front end's own screenshots** (`WV_ABI` 13). `wv_screenshot_hook(fn, data)` makes the
  `Screenshot` calls on `org.beanjar.Display1` queue for the front end, which takes the oldest with
  `wv_screenshot_request(&w, &h)` (its path) and answers it with `wv_screenshot_reply(failed)`,
  then or later; `failed` < 0 hands it to the core's own `wv_screenshot`. The counter `changes`
  counts the scene's changes. [beanjar-vnc's thumbnails](vnc.md#thumbnails) are built on them.
- **Planes shown** (`WV_ABI` 16). `wv_planes(hidden)` sets the planes this front end
  leaves out of its picture, `WV_PLANE_VIDEO`, `WV_PLANE_UI` and `WV_PLANE_CURSOR` (`hide-planes`
  at start: `video,ui,cursor`); a negative value only reads them. `draw()` skips them in
  `wv_render`, `wv_frame*` and `wv_screenshot`, and `wv_cursor` gives no cursor while it is hidden.
  A hidden video plane leaves the background's black in the UI's hole; a hidden UI leaves the
  video windows alone. It is a view setting: the guest's capture and the bus's `Screenshot` calls
  (`beanjar screenshot`) always draw every plane. The GTK window's More menu has a check item for
  each, for that window and its session; beanjar-vnc's are shared by its clients
  ([vnc.md](vnc.md#planes)).
- **Guest capture.** `capture.c` connects to `capture.sock` next to the display
  socket (`--capture-socket` overrides it). A target with `v4l2_capture` exposes
  `webos.capture` for its video60 driver. Requests render the real TV scene on the GL owner
  thread and return bounded RGBA pixels, excluding GTK controls. The guest
  converts these to its mapped capture format. Disconnects and timeouts fail
  capture; no screenshot file or retained-frame fallback is used. `capture.sock` is the capture
  relay's (`host/core/beanjar/capture_relay.py`), which holds QEMU's port and passes each request
  to the front end that is there. With none, it starts a headless viewer for the captures alone
  (`viewer-headless --capture-sink`: idle, no output drawn, no audio, no IR) and stops it 10 s
  after the last request, or as soon as the GTK viewer or beanjar-vnc connects; `beanjar view`,
  `vnc start` and `screenshot` stop it first, since it holds the display's one-client sockets.
- **Input.** `wv_key_evdev(code, down)` uses the PC keyboard map: the remote's own functions
  (Super/Home, Escape, the volume keys) go as the remote's IR buttons, the rest to QEMU's keyboard.
  `wv_ir(code, down)` sends a remote button, LG's IR data code, over `ir.sock` (the `webos.ir` port,
  `include/webos-ir.h`) to the guest's micom; the remote uses it. `wv_motion(x, y)`, `wv_leave()`,
  `wv_button(b, down, x, y)`, `wv_wheel(steps)` and `wv_release()` complete the set.
  `wv_pointer_blank()` hides the host pointer whenever it is inside the displayed TV picture,
  independently of guest cursor visibility or channel connection. Controls, remote and
  letterbox bars retain their normal pointer. Only `webos.cursor` supplies cursor pixels;
  QEMU listener cursor messages are acknowledged and discarded.
- **Exit.** `wv_signals()` installs the SIGINT/SIGTERM handling: the first signal is a clean quit,
  a second signal or 5 s of grace is `_exit`. `wv_finish()` releases the held keys, waits up to
  3 s for pending input calls, prints the exit counters, closes everything, and returns the failure
  status.

## Input

- **Keys.** A `Gtk.EventControllerKey` sits on each toplevel (TV window and popped-out remote) in
  the **capture** phase and returns TRUE for every key.
  - GTK's own handling never sees them: focus movement with Tab and arrows, button activation with
    Space and Enter, mnemonics, F10 and the window shortcuts.
  - The key's hardware keycode minus 8 is its evdev code on both X11 and Wayland. That code maps to
    a QEMU number with the same table as the SDL one: Home and Super send `KEY_LEFTMETA` (TV Home),
    and Escape sends KEY_BACK. The keyboard's volume and mute keys are added.
  - GTK's autorepeat is filtered by the core's held-key table, as SDL's `repeat` flag was.
  - The Menu key (evdev 127) toggles the remote and is not sent.
  - Losing focus on a window (`notify::is-active`) releases everything held.
  - Keyboard shortcuts that the desktop compositor itself takes still cannot reach the viewer.
    Requesting `gdk_toplevel_inhibit_system_shortcuts` is left for later, as an explicit menu
    toggle.
- **Pointer.** Three controllers sit on the GLArea only, so events never come from the chrome:
  `Gtk.EventControllerMotion` (motion, leave), `Gtk.GestureClick` with `button=0`
  (left/middle/right press and release, with GTK's implicit grab standing in for SDL's capture),
  and `Gtk.EventControllerScroll` (vertical, discrete). The core's `position()` already drops clicks
  in the black bars and clamps drags. Clicks on the status bar and remote go to their own widgets,
  so they never reach the guest.
- **Touch.** On a VM with a touch screen (`vm-info.toml`'s `touch`) a `Gtk.EventControllerLegacy`
  in the capture phase takes a touchscreen's touches before the gestures, each contact a slot of
  its own, to `wv_touch`. PyGObject (3.50) passes its `event` signal's GdkEvent as `None`, so the
  handler reads `get_current_event()`. The Mouse choice (`win.mouse-touch`, a check button in the popover
  of the arrow linked to the Remote toggle, shown only there) makes the mouse a finger too: the same controller then takes the left button's press,
  its motion while held and its release as a touch's begin, moves and end in a slot of its own,
  through `wv_touch` (so a turned picture maps them as it maps real touches). A click is a tap
  and a drag a swipe; hovering sends nothing (a finger can't hover; a right or middle press carries
  its own position, and a drag with one held still moves the pointer), the wheel and the right
  and middle buttons stay the pointer's.
  It starts as the pointer and lasts the viewer's session: the viewer has no per-VM settings
  store.
- **Cursor.** Over the picture, the area uses a 1×1 zero-alpha `Gdk.MemoryTexture` cursor,
  regardless of the guest cursor's visibility or connection. Elsewhere it is unset, so black
  bars and controls show the normal arrow. The core draws only the real `webos.cursor` image,
  and none while the panel is off (standby, Active Standby): the pane sets the core's standby flag along with its opacity 0 and black
  `.standby` class.
  On the Pi's native Wayland session viewed through WayVNC and RealVNC, GTK's named `none`
  cursor was observed with `pointer_blank=1`, yet a desktop arrow remained. An explicit
  transparent texture removed that arrow; the user confirmed the webOS pointer and normal
  toolbar cursor still worked. This qualifies the workaround without assigning the defect
  to a particular component of the remote desktop path.

## Decoded media from the VM helper

`host/helpers/media-helper` owns every compressed video and media-audio decoder for its VM.
Its `host/media/decode/` engine (caps, session, codec, frame, export, decoder) parses caps, decodes packets and resamples media audio to 48 kHz
interleaved float stereo. The guest elementary-stream and scaler-window protocols are unchanged.
`host/viewer/core/media.c` receives decoded output, schedules pictures and PCM against the pipeline
clock, uploads/imports video textures and mixes the PCM for playback. Guest OSD and video remain
separate surfaces; the main GL thread composes them with the cursor.

`include/host-media.h` defines versioned local `SOCK_SEQPACKET` messages. File descriptors travel
atomically with their descriptor via `SCM_RIGHTS`. Software pictures preserve the existing
YUV420/422/444 or NV12 shader inputs in sealed immutable memfds; the viewer maps them read-only.
The helper performs required 10-bit reduction for GLES2. PCM uses the same immutable storage.
A DRM_PRIME descriptor describes actual objects, layers, per-plane object indices, offsets,
strides and modifiers. The current renderer imports one layer with up to three planes; an
unsupported or failed import requests a real helper-side download.

The helper keeps decoding and retiring due output without a viewer. It retains a real current
picture, so attaching or relaunching a viewer, including while paused, does not begin decoding
in the middle of a GOP. Each connection can lease four outputs. Lack of credits drops complete
decoded output; it never discards compressed reference packets to keep a viewer moving. Clock
snapshots contain the helper's minimum receipt offset from guest monotonic time, plus the
monotonic-to-pipeline-clock delta. Viewer receipt latency is not used to estimate that offset.
Session and generation fields fence OPEN/CLOSE and FLUSH or media-kind changes respectively.

A hardware lease retains the actual decoder AVFrame until RELEASE. The main GL thread retires
the EGL image and completes GPU work with `glFinish` before releasing an imported picture.
Orderly shutdown keeps release-only socket duplicates alive until queued and current frames
have retired. Socket EOF, closing a DMA-BUF FD and `POLLOUT` alone do not establish GPU
completion. An unclean disconnect therefore quarantines at most four hardware references per
port until helper exit and permanently switches that port to genuine download into immutable
software storage, including after OPEN, CAPS and FLUSH. The extra decoder-surface budget covers
the quarantine, and repeated disconnects cannot grow it. This conservative path does not claim
that any particular consumer driver publishes usable reservation fences.

The helper accepts `--hwdec auto|none|TYPE`; the launcher passes `MEDIA_HWDEC` as its initial
policy. Viewer `--hwdec` remains a preference sent to the helper and acknowledged for the next
codec-open boundary; an active decoder continues unchanged. With decoding on, a shown video
window without a decoded picture is black, as on a TV. `--no-media` turns decoding off for this
viewer (the helper goes on decoding) and draws such windows as the diagnostic checkerboard.
`MEDIAHUB=0` omits the helper and writes `media = "off"` to the
launch's `vm-info.toml`, which the viewer core reads beside the D-Bus socket and takes as
`--no-media`; there is no second decoder in the viewer.

Decoder diagnostics and counters are in the VM's `run/media/media-helper.log` (`decode` reports;
rotated to `media-helper.log.1` at 4 MiB).
Presentation counts, shown PTS span, audio timing and viewer CPU belong to `viewer.log`
(`present` reports). Tests retain both sources. VIDEO/BLENDED capture reads a front end's
composed surfaces; with no viewer or beanjar-vnc, the capture relay's on-demand headless viewer
composes them (Guest capture, above).

## Audio

Audio keeps using SDL, with only the audio subsystem initialized.
- `audio.c` (the QEMU AudioOutListener queue with its prefill, cap and underrun logic) and the
  media mixer's callback are proven code, and SDL audio runs happily inside a GTK process: on WSLg
  it uses PulseAudio, and on the Pi PipeWire's pulse or ALSA.
- GTK has no audio output API. PulseAudio simple, or GStreamer's appsrc, would mean rewriting both
  queues and their measurements, for no gain.

## Chrome

### Status bar (`statusbar.py`, `stats.py`)

- **Layout** (`ui/statusbar.blp`, `ui/statusfield.blp`). A horizontal `Gtk.Box` along the bottom.
  The CPU meter, Disk and Network icons
  are grouped on the right, with rates in tooltips; activity uses the success colour and
  unavailable counters are dimmed. Direct networking has a wired-network icon and explains
  that traffic rates are unavailable; it does not appear disconnected. The bar uses
  libadwaita's header bar colours (CSS variables), as a `ToolbarView` bottom bar would be;
  the Remote toggle and the menu moved to the window's header bar
  ("The window"), and its message label gave way to toasts. It stays inside the pane, under the
  picture, rather than being the `ToolbarView`'s bottom bar, so the pane (picture and bar) stays
  embeddable.
- **The set's own key** comes first in the header bar on a model with a local key
  (`vm-info.toml` `local_key`, decoded from the board's `ToolOpt=` as the firmware does): a onekey model's key, or a joystick's five ways in a popover (its direction codes
  inferred, not verified), sent with `wv_local_key` as
  local key edges for lgemu's micom to time (host/viewer/README.md, "The TV's own key and front LED"),
  and its power icon follows the micom's front LED state (`micom-status`): red (`#e53935`) in
  standby with the standby light on, normal icon colour when off or unknown, with no glow.
  The joystick's header button has the same tint; its popover controls keep their normal colours.
  `stats.front_led()` maps the state; `pane.py` applies `led-red`, `led-off` or `led-unknown`
  to the key controls and appends the LED tooltip to their action descriptions. No separate LED
  remains by the title or in the status bar.
- **The header bar** has, after them, the remote's Back, Home and Settings keys as one linked
  group: the normal remote's BACK (0x28), HOME (0x7C) and MENU (0x43) buttons, sent as IR codes
  through `wv_ir` like the remote window's keys (down on mouse-down, up on mouse-up, so a long
  press holds). At the end are Save Screenshot (guest-only, through `wv_screenshot`, PNG in
  `~/Pictures` or `~`; a toast says where), Fullscreen (`win.fullscreen`, as F11), the Remote toggle,
  which on a touch screen's model has an arrow linked to it whose popover holds Mouse as a finger
  (one input control), and last, before the window buttons, a More menu (shown on a rotatable
  model only: Portrait). The Remote toggle's icon, `remote-control-symbolic`, is the viewer's own (`ui/remote-control-symbolic.svg`
  in the bundle's `icons/`; Adwaita has no remote). There is no other menu (until 2026-09-29 one held
  Fullscreen, Save Screenshot and Quit; Quit is the window's close button). Hiding the bar is
  `--no-statusbar`.
- **None of it takes focus**, so it never holds keyboard focus away from the guest.
- **The sources**, refreshed once a second:
  - CPU is the utime+stime delta of `/proc/<qemu.pid>/stat`. vCPUs are counted from
    `task/*/comm`, which needs `-name debug-threads=on`. The tray-style CPU meter shows a bar
    for each vCPU thread, using its own `task/*/stat` CPU-time delta. New or replaced threads
    need a fresh baseline. Without per-vCPU counters it shows one aggregate bar. Bars saturate
    at 100%; the tooltip retains total QEMU CPU use (including non-vCPU threads) and individual
    vCPU percentages. Unavailable samples remain dim rather than showing invented activity.
  - Net is the `tap0` line (or the `ifname=` of `-netdev`) of `/proc/<pid>/net/dev`. With
    `mode = "direct"` it reports the mode without rates; `link = "off"` appears offline. Those backends
    never use an unrelated host TAP's counters.
  - Disk is QMP `query-blockstats` over `viewer-qmp.sock`, using `Gio.SocketClient` with async line
    reads and stdlib `json`.
  - Memory is the guest's: RAM in use from the virtio-balloon's statistics (QMP `qom-get` of
    `/machine/peripheral/balloon` `guest-stats`, which QEMU polls every 2 s), swap from the
    `webos.memory` port's last whole line (QMP `ringbuf-read` of the `memory` ring buffer; the
    initramfs's `virtio-meminfo` writes it, docs/dev/architecture/build-and-boot.md, "Guest memory
    channel"), both on the same connection and tick as Disk. The tooltip adds the VM's host
    footprint (its unit's cgroup or QEMU's `/proc/<pid>/status`).
  - `qemu.pid` and `viewer-qmp.sock` are looked up next to the D-Bus socket.
  - Power is QMP `query-chardev`'s `frontend-open` for the `standby` chardev. Until it is
    reported, or when the connection or query fails, the LED is unavailable.
- **`--no-statusbar`** hides the bar.

### Remote (`remote.py`)

- **Widgets.** The layout is the existing table from `build/remote-reference.html`: 48 buttons, in
  CSS pixels, on a 188-wide panel. Each button is a real widget (a `Gtk.Box` with a `Gtk.Label` or
  a symbolic `Gtk.Image`). `RemotePanel` positions them in its `size_allocate` at scale s, which is
  the panel height / 652.
  - The body (`BODY`) follows LG's own remotes rather than the reference's pill: a slim, nearly
    rectangular bar, corner radii 16 at the top and 10 at the bottom, running 72 px past the colour
    keys into a chin where LG prints its logo (panel 188×652). The keys keep their places.
  - Colours, radii and font sizes come from one generated CSS provider, rebuilt when s changes.
  - The layouts themselves are data in `remote_layouts.py`, a module without GTK that `remote.py`
    re-exports, so the manager's `beanjar remote layout` and the [web viewer](web-viewer.md) use
    the same tables (`as_json`).
  - The colours are a small named palette at the top of `remote_layouts.py` (`BODY_FACE`, `BODY_RIM`,
    `KEY_DARK`, `KEY_LIGHT`, `LABEL_ON_DARK`, `LABEL_ON_LIGHT`, `PRINT`, `CHIN_PRINT`,
    `POWER_RED`, the four colour keys, `NETFLIX_RED`, `AMAZON_BLUE`), sampled from a photo of an
    LG remote and rounded to clean hex values; the table and the key groups are in
    host/viewer/README.md ("TV remote"). A near-black body; black keys with white labels for the
    digits and the SAP/SLEEP/STOP row; light grey keys with dark labels for the top row, the
    rockers and the keys between them, the streaming keys (brand-coloured text), the D-pad group,
    BACK/EXIT and the playback row; a red power key.
  - Icons are Adwaita symbolic icons (`system-shutdown`, `go-home`, `pan-*`, `media-*`,
    `audio-volume-muted`, `emblem-system`, …), with a text fallback when the theme lacks one.
  - The body and the printed labels (VOL, CH, QUICK ACCESS, MAGIC LINK) are widgets too
    (boxes with CSS radii, labels), drawn in child order.
- **Pressing.** A `Gtk.GestureClick` per button:
  - press sends the button's IR code down (`core.ir`), and release or cancel sends it up, so
    holding a button is a long press (the guest's micom repeats it);
  - the held button gets the `held` style class;
  - keys WT1H lacks are dimmed and insensitive, with a tooltip saying why.
- **Its own window, always.** The remote is only ever the separate `Gtk.Window` titled "Remote"
  (`RemoteWindow`), made when first shown and kept, or hidden. It is never docked into the TV
  window: the docked sidebar (the TV window growing and shrinking by its width, its 2× cap, the
  dock control) was removed by user decision on 2026-09-28. The TV window holds the picture and
  the status bar only and never changes size for the remote.
  - The header bar's Remote toggle and the Menu key show and hide it; `--remote` shows it once the
    TV window is mapped. It is transient for the TV window, so it stays above it: as an unrelated
    toplevel it went behind the TV window at the first click on the picture (WSLg), still shown
    but out of sight, and the next click on the toggle "hid" it with nothing seen to happen. The
    toggle is active exactly while the window is visible and not minimized (`Gtk.Window:suspended`),
    kept in step on `notify::visible` and `notify::suspended`, whatever showed or hid it.
  - It is 1.125× as tall as the TV picture, at 1.125× to 2.25× the reference (the normal remote
    212×734 to 423×1467; `remote.window_scale`), and follows the picture's size (the view's
    `resize`). So it is taller than the picture beside it: 734 px beside the default 540 px picture
    (the least, 1.125×), 810 px beside 720 px, 1125 px beside 1000 px, and 2.25× from a 1304 px
    picture up. Nothing fits it to the display; above a 1067 px picture it is taller than a
    1200 px screen. Earlier on 2026-09-29 it was 3/4 as tall at 0.75× to 1.5× (141×489 to
    282×978), too small to use; before that 2× the reference, or as tall as the display allows.
  - On X11 it is placed right of the TV window (16 px gap, top edges aligned) when first shown:
    GTK 4 has no call to position a toplevel, so `XMoveWindow` goes through ctypes on a connection
    of the viewer's own. On Wayland no client can place a toplevel, and the compositor decides.
    A Pi trial on 2026-10-07 confirmed that XWayland allows adjacent placement, but the user
    chose to retain native Wayland and accept its placement limitation. The remote stays a
    separate window; the viewer does not force XWayland or dock it into the picture window.
  - It looks and handles like the physical remote.
  - It is undecorated (`decorated=False`: no title bar, no frame, no CSD shadow). The window and
    the panel have no background (`.remote-window`, `.remote-panel.floating`), so only the body is
    drawn. The corners are transparent where a compositor is running (WSLg, any desktop);
    under a bare X server such as Xvfb they are black.
  - Its input region is the body's shape: `RemotePanel.body_rects()` gives one rectangle per pixel
    row through the rounded corners, and `gdk_surface_set_input_region` gets them after every
    allocation and on every map. That call goes through ctypes with a cairo region from
    `libcairo.so.2`, because PyGObject needs `python3-gi-cairo` for a `cairo.Region` and the Pi
    has not got it. On X11 this is the SHAPE extension's input shape, checked on Xvfb. On Wayland
    it is `wl_surface.set_input_region`.
  - A `Gtk.GestureDrag` on the window moves it: past the drag threshold, on anything that is not
    a key or a control (the chin included; nothing while the chin's menu is open), it claims the
    sequence and calls `Gdk.Toplevel.begin_move` with the
    gesture's device, button, start point and event time, as `GtkWindowHandle` does. The window
    manager or compositor then moves it (on X11 `_NET_WM_MOVERESIZE`; without a window manager GDK
    moves it itself, which the check uses). Presses on a key are the key's.
  - With no title bar, the window's own controls sit on the chin, in place of LG's logo, and are
    styled like it: faint grey glyphs in a thin ring, transparent, brightening on hover, so they
    read as print on the body and not as TV keys. They send nothing to the guest.
    - **close** (x) closes the window: a plain `Gtk.Button` on GTK's own `window.close` action,
      so it is the window's close request, the same as closing it from the desktop or turning the
      Remote toggle off (the window hidden and kept, the toggle off, the toggle or the Menu key
      opening it again).
    - **menu** is a `Gtk.MenuButton` popping up with `remote_menu` (`remote-window.blp`): Normal
      Remote / Service Remote (`remote.layout`) and Place Next to TV (`remote.place`: the X11
      placement again; the action is disabled and the item hidden, `hidden-when: action-disabled`,
      where the display is not X11). No Hide Remote (the x beside it closes) and no Save Screenshot
      (the TV window's header bar has it). The `remote` action group is the window's. While the popover is open the capture key controller lets keys through to it
      (`ViewerPane.menu_open()` covers both menus), so Escape closes the menu only.
    - No keep-on-top item: GTK 4 has no call for it, and Wayland no protocol a client may use.
  - Escape and a close request from the desktop hide it too. Escape is
    the one key the remote's window keeps for itself. It is chrome with nothing else to dismiss it
    by keyboard, and the guest's back key is on the remote as BACK. In the TV window Escape still
    goes to the guest.
  - The window is resizable only through the desktop (no frame to grab). The panel keeps its
    aspect ratio and the input region follows the body.
  - The window has the same capture key controller as the TV window, so keys typed there go to
    the guest. The Menu key's auto-repeat is told apart by time (a press within 1 s of the last
    unreleased one), with the state in the pane: the window hides or shows under the key, so the
    repeats and the release may land in the other window, or the release nowhere.
- **`--remote-preview OUT.ppm [--remote-press KEY]`** renders the panel alone, with no VM, at 2×
  (376×1304) through `Gtk.WidgetPaintable` → `gsk_renderer_render_texture` → PPM. PyGObject 3.42
  cannot carry a `GskRenderNode`, so those few calls go through ctypes into `libgtk-4.so.1`. It
  needs a display (`xvfb-run -a` for scripts). The chin's controls are drawn, inert. The SDL rasteriser and its bitmap font (`remote.c`,
  `raster.c`) are deleted.

### The window

- **Contents.** An `Adw.ApplicationWindow` (2026-09-29; the viewer is an `Adw.Application` and
  prefers the dark style): an `Adw.ToolbarView` whose top bar is a compact `Adw.HeaderBar` (an
  `Adw.WindowTitle`; the Remote toggle; the menu button). The titles come from what the launcher
  wrote at the VM's start into `vm-info.toml` beside the sockets (`paths.vm_info`, every launch: the
  manager's and `host/scripts/vm-launcher`'s, so `regress --viewer` too): the platform and the version LG markets
  the release as (`Release.platform`, `Release.version`), board (OTA model code, from the
  firmware's name), SoC, release codename and firmware version; and a library VM's name, its
  directory's (`<vm>/run/` holds the sockets). The header's title is the platform ("webOS 4.5"; the
  name alone for a platform without a known version), its subtitle the VM's name, board and
  firmware version ("tv1 · W19P 04.10.20.01"); the window manager's title has it all in one line,
  "tv1 - webOS 4.5 (goldilocks2) - W19P k5lp 04.10.20.01 - Beanjar Viewer" (`main.titles`), and whose
  content is an `Adw.ToastOverlay` around the `ViewerPane`, a vertical box: the `TvView` over
  the status bar, with no padding, the bars around the picture black. The remote is not in it.
  The header's buttons, like the status bar, never take focus. The pane gets the window's
  controls through `ViewerPane.attach` (the toggle it keeps in step with the remote's window,
  the menu button, the toast overlay, the fullscreen toggle).
- **The remote's window** is unchanged under libadwaita: it is a plain undecorated
  `Gtk.Window` whose transparent background the viewer's CSS sets, and the body is drawn by
  `remote.py`; rendered before and after the move, the remote is identical, pixel for pixel.

### Layouts (Blueprint)

The static part of every window is a Blueprint file in `host/ui/data/viewer/`, a GTK template of the
Python class that does the work (`@Gtk.Template`, `Gtk.Template.Child`, `Gtk.Template.Callback`):

| File | Template | What it holds |
|---|---|---|
| `window.blp` | `ViewerWindow` (`Adw.ApplicationWindow`) | the `Adw.ToolbarView` with the header bar (the remote's keys, title, Save Screenshot, Fullscreen, Remote toggle), More, the toast overlay, the window's signals |
| `pane.blp` | `ViewerPane` (`Gtk.Box`) | the vertical box |
| `statusbar.blp` | `ViewerStatusBar` | the three fields |
| `statusfield.blp` | `ViewerStatusField` | a field's LED and label (`title` is a property) |
| `remote-window.blp` | `ViewerRemoteWindow` (`Gtk.Window`) | undecorated, its class, the drag gesture, its signals |

What stays in Python, and why:
- **The remote's 48 keys** are generated from the key table in `remote.py` (`BUTTONS`): each has a
  position, size, face colour, radii, glyph and key code, and `RemotePanel` places them itself in
  `do_size_allocate`, scaled with the panel, with CSS generated per scale. Written out in
  Blueprint they would be 48 blocks of the same data with none of the geometry, which lives in
  the allocation code anyway, and the table is also what a click and `--remote-press` look up.
- **Widgets built with arguments**: the `TvView` (a `Gtk.GLArea` subclass drawn by the C core,
  created with the core), the `RemotePanel` and the `StatusBar` (created with the socket
  directory, or not at all with `--no-statusbar`) are put into the templates' containers.
- The key controller, the input region and X11 placement of the remote's window, the window actions, the CSS and the size
  fitting.

Properties are written in the templates as the Python set them before, including `css-classes`
where the Python replaced a widget's classes (a `Gtk.Box`'s `vertical`, a `Gtk.Window`'s
`background`) rather than adding to them (`styles`). The widget trees, with every readable
property, and the rendered windows were compared before and after the move, on GTK 4.8 and 4.18:
identical, pixel for pixel.

`ui.py` loads the templates before the modules that use them are imported: from
`beanjar-viewer.gresource` next to the package (`$BEANJAR_VIEWER_GRESOURCE` in the tests; the
checkout's `build/viewer/` when the package runs from `host/viewer`), or from `--ui-dir DIR`:
its `.ui` files, or its `.blp` files compiled at start with blueprint-compiler, so
`--ui-dir host/ui/data/viewer` tries an edited layout without a build.
- **Size.** The initial size is `--size` (1280×720) for the picture, plus the chrome. The picture
  keeps its aspect ratio with black bars, as before.
- **Fullscreen** is from the header bar or F11, which the key controller takes (once per press, not
  its auto-repeat) and does not send, as it does not send the Menu key (before 2026-09-29
  F11 went to the guest). Fullscreen hides the header bar and the status bar
  (`ViewerWindow.set_chrome`), so the picture fills the screen.
- **Closing** the TV window quits, and so does the display going away.

## Headless and test paths

- **`viewer-headless`.** This is today's `viewer.c` main loop minus the windowed parts, using
  the core. It covers `--headless`, `--replay` (SDL scancode names, as before), `--screenshot`,
  `--screenshot-every`, `--osd-alpha`, `--no-osd`, `--seconds`, `--sdl-audio` and
  `--capture-sink` (Guest capture, above). SDL's offscreen driver still provides the EGL context.
  - The Python launcher execs it, keeping the same pid, for any of these options. It does this
    before importing `gi`, so hosts without GTK (the Pi today) run every test unchanged.
  - Screenshots use `wv_screenshot`, which renders offscreen at the drawable size and gives the
    same pixels as before.
  - `--replay` without `--headless` used to open a visible window. It now always runs headless.
- **Where the tests stand:**
  - `tests/unit/viewer/check-cursor.c` keeps the core's cursor checks (packet parsing, BGRA, visibility,
    heartbeat reuse, disconnect) and `wv_pointer_blank()`.
  - The GTK cursor, the layout and the chrome clicks are checked by `tests/integration/viewer/check-gtk.py`
    under `xvfb-run -a`, against `tests/fakes/fake-qemu.py`. That is a stand-in D-Bus display with
    a private bus, a 1920×1080 scanout, a visible LG cursor and a log of every input call. Its
    standby check reads the X server's pixels (XGetImage) as well as the core's render: LG's
    cursor is gone from both in standby and back after the wake. On Xvfb (GTK's GL renderer) the
    pane's opacity 0 already hides the whole GL area, so there the core's render is what shows the
    fix (2026-10-13). The
    check drives the window with real X events (XTest through ctypes). It is a Meson test in the
    `viewer` suite, and is skipped when Gtk 4 cannot be imported.
  - `check-viewer.py`, `check-media.sh`, `check-video-app.sh` and `check-volume.sh` run through
    the launcher into the headless program, unchanged.

## Screenshots from a script

`beanjar screenshot NAME FILE [--frame WxH | --last]` saves the TV picture of a running VM as a PNG whatever
the viewer window's size or position. It cannot go through QEMU: `screendump` fails on
`-display dbus,gl=on` ("no surface"), and the video windows and LG's cursor are composited by the
front end, not by QEMU. A second display client cannot run beside the viewer either (the cursor,
video, IR, media and capture chardevs take one client). So whoever owns the VM's display takes the
picture:

- **A front end is running.** `wv_attach()` (the GTK viewer and [beanjar-vnc](vnc.md) both call
  it) requests the well-known name `org.beanjar.Display1` on the VM's own bus (`run/dbus.sock`, the
  connection that QEMU's display and the core already use) with `DO_NOT_QUEUE`, and exports object
  `/org/beanjar/Display1` with the method `Screenshot(s path, u width, u height)` (the naming follows
  QEMU's `org.qemu.Display1`). One front end owns the name, as one front end serves a VM; a second
  one logs "screenshots: not served" and carries on. The method runs `wv_screenshot` on the GLib
  main loop, writing PATH (absolute) as a PPM, with LG's cursor drawn even in a front end that shows
  it itself (`no-cursor`, beanjar-vnc's `--native-cursor`), and returns empty, or fails with a D-Bus
  error carrying the reason (`org.beanjar.Display1.Error.NoPicture`, `.InvalidArgs`, `.Failed`).
  A front end that has been idle since before its first copy of the guest's frame (beanjar-vnc
  with no client, which acknowledges QEMU's updates without copying them) copies it first: until
  2026-10-28 such a call failed with `NoPicture` for as long as no client came.
  Once there is a picture, every screenshot's draw first copies a frame skipped while idle
  (`refresh`), so it is the guest's current one, not the one a client last saw.
  No `WV_ABI` change. A script can call it without the CLI, then convert the PPM itself:

  ```sh
  gdbus call --address unix:path=$LIB/vms/tv1/run/dbus.sock --dest org.beanjar.Display1 \
    --object-path /org/beanjar/Display1 --method org.beanjar.Display1.Screenshot /tmp/tv1.ppm 1280 720
  ```

- **Neither is running.** `viewer-headless --screenshot OUT.ppm --screenshot-after 1500 --size WxH`
  waits for the first scanout, takes the picture 1.5 s later (for the video windows and the
  cursor) and exits; `--seconds` bounds the wait. Nothing stays running.

The CLI converts the PPM to PNG and writes FILE by rename. The default size is 1280x720, not the
scanout's own size: that is the viewer's picture area at its default size, so it is what Save
Screenshot gives with an unresized window, and it is the same for every firmware. A screenshot of
640x360 or less is a thumbnail: the manager keeps the last one in the VM's directory
(`thumbnail.png`), and `--last` gives it, also once the VM is off (beanjar-web's Off cards). Exit
status 0 done, 1 failed (VM off, no picture within 30 s, a front end that predates the name, no
thumbnail kept), 2 usage.

## Remote buttons from a script

The same object has `Ir(u code, b down)`: a TV remote button edge, LG's IR data code (0 to 255,
`include/webos-ir.h`), sent with `wv_ir` to the guest's micom like the GTK remote's buttons. Over
VNC only Home, Back, the volume keys and Mute have a PC key; every other button reaches the guest
this way. As with `wv_ir`, one button is down at a time (pressing another releases it), so holding
a button is a long press. The core watches the bus name of the client that pressed: when that
client leaves the bus with its button still down (a killed `beanjar remote pipe`, a dropped ssh),
the button is released and the viewer logs `ir: :1.N left with 0xCODE down: released`. A code over
255 fails with `org.beanjar.Display1.Error.InvalidArgs`. No `WV_ABI` change.

`beanjar remote press [HOST/]NAME KEY...` presses keys of the VM's remote by name (HOME, UP, ENTER)
or code, and `beanjar remote pipe [HOST/]NAME` takes `down CODE` and `up CODE` lines on stdin, which
is what the [web viewer](web-viewer.md)'s remote uses. Both need a front end (beanjar-vnc or this
viewer) to own the name.

```sh
gdbus call --address unix:path=$LIB/vms/tv1/run/dbus.sock --dest org.beanjar.Display1 \
  --object-path /org/beanjar/Display1 --method org.beanjar.Display1.Ir 0x7c true
```

## The pointer from a script

The same object has `Pointer(u x, u y, u buttons)`: QEMU's tablet to x, y in the guest's display
(scanout) pixels, with no viewport in between (`input_abs`), then the buttons to the mask (bit 0
left, 1 middle, 2 right), as a VNC pointer event: what changed is pressed or released
(`input_button_edge`). A position outside the display or a mask over 7 fails with `.InvalidArgs`,
and with no scanout yet with `.NoPicture`. As with `Ir`, buttons that a client holds are released
when it leaves the bus (`pointer: :1.N left with button B down: released`). The picture does not
follow it as it follows the window's pointer: LG's cursor is drawn where the guest puts it. No
`WV_ABI` change. `beanjar remote click` and `move` map a screenshot's point to display pixels and
add a wake movement ([manager](../../../host/core/README.md#pointer)).

## The touch screen from a script

`Touch(u kind, u slot, u x, u y)` is a finger on a model's touch screen (kind 0 begin, 1 move,
2 end, 3 cancel; slots 0 to 9; display pixels): QEMU's
`org.qemu.Display1.MultiTouch.SendEvent` (`input_touch_abs`). The core drops what QEMU would
misread (a move or end of a slot that has no finger, a second begin) and sends a cancel as an end,
since QEMU 10 treats a cancel as a move. One client's touches at a time: a begin from another
client ends the first one's; a client that leaves the bus has its fingers lifted. The GTK viewer's
own touchscreen events go the same way through `wv_touch`, in window coordinates through the
viewport. On a VM without a touch screen QEMU has no MultiTouch device and the call
does nothing in the guest; `beanjar remote tap` refuses it first
([manager](../../../host/core/README.md#touch)).

## A turned picture

A set that turns to portrait (`vm-info.toml`'s `portrait`, the StanbyME) is shown turned as the set
is: the pane reads the guest's rotation from `run/rotation` every 0.5 s and calls `wv_turn` with
`rotation.picture_turn` of it ([manager](../../../host/core/README.md#rotation)). The core then
draws the scene unturned into a texture of the area's size with width and height swapped, and
turns it into the GLArea with a matrix on the texture coordinates; the logical coordinates of
`wv_motion`, `wv_button` and `wv_touch` are mapped back into the unturned picture, so the tablet
and the touch screen keep panel pixels, and `wv_picture` gives the picture as shown. Screenshots,
`wv_frame` (beanjar-vnc) and `Display1.Screenshot` stay the panel's unturned pixels. The window
turns its picture area with the picture unless it is maximized or fullscreen, and the header's
Portrait check item in the header's More menu (a library VM only; the button's label is Portrait,
its tooltip the state) is a two-state toggle: it runs `beanjar sensor set rotate NAME 270` from landscape and
`beanjar sensor set rotate NAME 0` from any other angle (90 and 180 come only from the CLI); its state follows
the guest's report. The GTK viewer has no preview or thumbnail of a VM's screen (the manager's VM
list shows none), so there is nothing else to turn.
`WV_ABI` 15.

## Signals

`wv_signals()` keeps the existing policy: the first SIGINT/SIGTERM sets the quit flag, which the
poll source turns into `Gtk.Application.quit()`, followed by `wv_finish()`. A second signal, or 5 s
without a clean exit (`alarm`), calls `_exit`. Python's own SIGINT handler is reset to `SIG_DFL`
first, so PyGObject does not install its fallback over the core's.

## Packaging and build (Meson)

The build stays in `host/viewer/meson.build`, in the host build.

- `libbeanjar-viewer-core.so` is a `shared_library`. It needs gio-unix, egl, glesv2, sdl2 (audio),
  threads and m. It has no libavcodec, libavutil or libswresample dependency; the private FFmpeg
  belongs to `host/helpers/media-helper/meson.build`.
- `viewer-headless` is an `executable` linked to it, with an rpath of `$ORIGIN`.
- `beanjar-viewer` is the Python launcher, and `beanjar_ui/viewer/` is the package. Both are published
  as-is.
- **The layouts:** blueprint-compiler (Debian's `blueprint-compiler`, `docs/dev/guides/build.md`)
  compiles `ui/*.blp` to GtkBuilder XML, and `gnome.compile_resources` bundles them into
  `beanjar-viewer.gresource`, with the application icon (`host/ui/data/icon.svg`, as
  `org.webosbrew.beanjar.viewer`, the application ID; non-unique, one viewer per VM).
- **Published to `build/viewer/`:** `beanjar-viewer`, `viewer-headless`,
  `libbeanjar-viewer-core.so`, `beanjar_ui/viewer/` and `beanjar-viewer.gresource`. The launcher puts its
  own directory on `sys.path` and loads the library next to it (`BEANJAR_VIEWER_CORE` overrides
  that).
- **GTK 4 for Python is needed to build the layouts**, not the rest: blueprint-compiler runs with
  `/usr/bin/python3` and checks each class and property against the installed typelibs, so a
  layout using something newer than the host's GTK (4.18 on both machines) fails to build. Without it Meson says so and builds no bundle; the launcher
  still runs headless.
- **Packages:** `python3-gi gir1.2-gtk-4.0`, present on both machines (the Pi's since
  2026-09-28); `blueprint-compiler` (0.16.0, on both since 2026-09-29); `glib-compile-resources` (`libglib2.0-dev-bin`, with `libglib2.0-dev`).
  `check-gtk` also needs `xvfb` and libXtst (`libxtst6`), present on both. `libjson-glib-dev` is
  not needed.

## Launcher

The launcher (`host/core/beanjar/qemu.py`) adds three
things on both machines:
- `-qmp unix:$VIEWER_QMP_SOCK,server=on,wait=off`, which defaults to `build/viewer-qmp.sock` next to
  the D-Bus socket
- `-name webos,debug-threads=on`, for the vCPU count
- `-pidfile $QEMU_PIDFILE`, which defaults to `build/qemu.pid`, so every start path writes it, not
  only `host/scripts/vm-launcher start`.

## Verification (2026-09-28)

- **Host tests** (x86 and Pi): `meson test --suite viewer` passes, `check-cursor` and
  `check-gtk`, all 26 checks. `check-gtk` also passes on WSLg's XWayland (`DISPLAY=:0`), except
  that the real desktop pointer's motion over the window showed up in the input log.
- **Headless path unchanged:** `tests/regress/input/check-viewer.py` passes against the x86 VM
  (replay 18/18). `tests/regress/video/check-media.sh build/media-test/h264-aac-720p.mp4` passes all its
  checks (735 pictures, h264 hw, lateness avg 1.9 ms, audio slip avg 0.5 ms). On the Pi,
  headless against the KVM VM imports dmabufs (V3D, EGL), with 63 imports in 6 s.
- **Media in the GTK window** (Xvfb, SDL disk audio, the same clip with `VIEWER=none`): 795 of
  803 frames shown, h264 hw, lateness avg 2.1 ms, decoded video under the OSD in the window's
  screenshots. Audio slip avg 20 ms through SDL's disk driver, which paces itself, so this is not
  comparable with the headless clock thread. A/V with the frame clock on a real display is not
  measured yet (`WEBOS_MEDIA_PROBE=1` with `av-sync.mp4`).
- **Stats against the x86 VM (TCG, NET=mitm):** CPU 55–60 % against `top`'s 57–67 %. Disk
  W 10.7 + 57.8 MB/s during `dd` of 64 MB. Net ↓ ~10 MB/s during a 20 MB `curl`, which reported
  8.8 MB/s. On the Pi (KVM), CPU 8–10 % against `top`'s 11.5 %, and QMP read 204.6 MB.
- **WSLg (Wayland, real window, scripted), before docking was removed:** dock and undock (1280 ↔
  1499 wide, the picture staying 1280×720), pop out (376×1192) and close back, maximize without
  growing, restore to the previous size, resize, Save Screenshot. It was not possible to inject
  keyboard and pointer input into a Wayland window, so that was checked on XWayland and Xvfb only.
  WSLg's PulseAudio server refused connections at the time (`pactl info` too), so sound was not
  heard. On Debian 13 (2026-09-28) it accepts them: `pactl info` reaches WSLg's server
  (`RDPSink`), and the viewer's SDL audio opens on it.
- **The layouts in Blueprint:** `check-gtk` (41 checks) passes on x86 (GTK 4.8) and the Pi
  (4.18) with the templates from the build's bundle; a grab of the window with the docked remote
  (since removed), of the popped-out remote and of the undocked window under Xvfb is
  pixel-identical to one of the previous hand-built window on both.
- **The remote as its own window only (2026-09-28):** `check-gtk` (49 checks) passes on x86 and
  the Pi under Xvfb. On WSLg's XWayland against the running x86 VM, scripted with XTest (the
  keyboard given to a window with `XSetInputFocus`, as WSLg does not focus X windows for
  injected input): the Remote toggle shows the window 16 px right of the TV window and hides it;
  the Menu key hides and shows it; its RIGHT and LEFT keys and keys typed in it reach the guest;
  the hide control, Escape and a close request hide it; the TV window stays 1280×745 throughout.
  A drag on the body calls `begin_move`, but the compositor moves the window with the real
  pointer, not XTest's, so the move itself was not seen there. On Wayland the viewer starts with
  `--remote` through the manager (`beanjar viewer open default --remote`).
- **The slim body and its chin (2026-09-28):** `check-gtk` (57 checks) passes on x86 and the Pi
  under Xvfb: the input shape follows the small corners and the chin, a drag on the chin moves the
  window, the chin's hide and menu controls and all three menu items work and send nothing to the
  guest, and Escape with the menu open closes only the menu. On WSLg's Wayland the menu shows Hide
  Remote and Save Screenshot only (Place Next to TV hidden); on its XWayland the chin and menu
  checks pass, with the window-manager differences noted above.
- **SIGTERM:** a clean exit in 47 ms, with the counters printed. When the display goes away (the
  stand-in QEMU is killed), the window closes.

## Deleted

- The SDL windowed path: the SDL window, `cursor_host`'s SDL cursors and the SDL remote window.
- `remote.c`/`remote.h` and `raster.c`/`raster.h`: the CPU rasteriser and its font.
- `stats.c`/`stats.h`: moved to Python.
- `viewer.c`: split into `core.c` (the core) and `headless.c`.
- The SDL windowed viewer is not kept: the GTK window replaces it on both machines, and only the
  headless SDL program remains.

SDL remains only for the headless program's offscreen GL context and for audio.

## Embedding in the manager

`beanjar_ui.viewer.window.ViewerPane(core, stats_dir, remote=, statusbar=, on_quit=)` is a `Gtk.Box`
(a template: `beanjar_ui.viewer.ui` must find the viewer's bundle)
holding the view and the status bar (the remote's window is its own toplevel, made by the pane), over a `beanjar_ui.viewer.core.Core` with its
options set; `ViewerWindow` is a thin `Adw.ApplicationWindow` around it that adds the header bar
(given to the pane with `attach`), the key controller, the header bar's actions and `--size` fitting (`tests/integration/viewer/check-gtk.py` builds one the
same way). The manager would also need the key controller on its own window. The manager
can put a `ViewerPane` in its own window. Two limits apply:
- only one viewer per process, because the core's signal handling and the SDL audio subsystem are
  process-wide;
- only one viewer per VM, because `cursor.sock` takes one client.
