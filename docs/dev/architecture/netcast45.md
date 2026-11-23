# NetCast 4.5 (LM14) on the emulator

LG's NetCast 4.5 is not webOS: BusyBox init, LG's process manager (procmgr) and one large
application, RELEASE, drawing through LG's GX layer. The firmware here is LM14's US 04.11.18
(`HE_LCD_NC5U_AAADAIAA`, MStar Napoli with an external URSA9 FRC). It boots as the target
`lm14-netcast45`; it is still being brought up (`targets.BRING_UP`). The stock OSD and NetCast
dashboard have the user's visual acceptance; the dashboard responds to remote navigation. This file is what was decided.

## Decisions

1. **Replace at the vendor graphics-library boundary.** procmgr, RELEASE, libafopenapi and every
   other LG service stay stock. The emulator replaces the vendor's graphics library, as the gl
   layer replaces libmali on webOS and SimpleSmart's layer its libhalgal; nothing firmware-derived
   goes into a layer. The stock library's GE (MStar's 2D engine) command-queue wait is therefore
   not modelled by this design.
2. **Phase 1, libgal.** Our `libgal.so` exports the selected firmware's 27 `GAL_GX_*` with the
   ABI procmgr, crashmgr and libafopenapi use. Software surfaces live in the GOP pool, shared by
   every process; scanout is the project's existing GOP node and presenter (SimpleSmart's).
3. **Phase 2, EGL.** Mesa from the gl layer replaces libMali/libEGL/libGLESv2, with an EGL
   window system that takes HOA_GX window ids through the same `HOA_GX_*` calls the stock
   `hoagx` EGL imports (fxui, GDK, chrome). Implemented below.
4. **Input is the board's.** Keys and power go through the real I2C master the micom hangs on:
   mstar-utopia models LM14's user-mode HWI2C registers (`lm14_hwi2c`), wired to lgemu's
   devices; nothing is reported ready that no device answered.

## The target

`guest/targets/lm14-netcast45.toml`: model NC5U with its own `[soc]` (chip names follow LG's
platform name, as the firmware states it: `ATSC-LM14`, `liblm14`), and
`guest/targets/release/netcast45.toml`, platform "NetCast" 4.5. Recognition (`paths.firmware_release`):
a sysvinit `root.pak` and `mode.pak`'s `RELEASE.cfg`, as SimpleSmart; both lines' RELEASE.cfgs set
`bSupportNC45`, so the vendor pak's graphics library tells them apart (SimpleSmart's is
`libhalgal.so`, NetCast 4.5's `libgal.so`).

- Guest build: `build/guest-lm14-netcast45`, set up with `meson/cross/lgtv-sdk.ini` (the NetCast
  "lgtv" Buildroot SDK of openlgtv/buildroot-nc4, glibc 2.12; the firmware's glibc is 2.16). It
  builds `guest/layers/netcast45`, the kernel and the initramfs; it links nothing of the firmware and
  needs only its paks.
- mstar-utopia: the study's LM14 parameters, the mmapger records derived at each start
  (`SocModule.derived = "mstar-mmap"`: `host/helpers/mstar/mmap-table.py --cache` reads the vendor pak's
  libmadp into `build/mstar/<firmware>/mmap.bin`, which the launcher puts in the host share;
  a host given the cache needs no pyelftools). Cache reuse checks source
  hash, MIU geometry, record IDs, binary ranges and output digest; older metadata without them
  must first be regenerated on the host with pyelftools, then synced. The record IDs are derived
  with the records, from libmadp's code: `mmap_ids`, GetMMAP's count (77 in 04.11.18; 78 in
  04.11.22, 04.12.36 and the 2015 03.01.38, which add a record at index 7 and move every later
  one up); `lx_mem_id` and `lx_mem2_id`, the two records `MAdp_MPool_Init` asks GetMMAP for (0
  and 76 in 04.11.18); `gop_fb`, the record whose block `gfx_Init` gets last before
  `gfx_CreatePool` makes its GOP pool (60 in 04.11.18, 0x2a448000, 300 MiB).
- The VM's display is 1920x1080 (`Release.screen`, the emulator's choice): the panel is UHD
  (RELEASE.cfg `eDispResolution` 3840x2160) behind the URSA9 FRC.

## The vendor pak and libgal

lginit and lginit_app export `LD_PRELOAD=/lg/res/lglib/liblibm.so:/mnt/vendor/libgal.so` to every
process, and RELEASE also `NEED`s `libgal.so`; the stock lginit_app mounts the vendor pak at
`/mnt/vendor` from `mmcblk0p36`. So:

- **init mounts the vendor pak** (`Pak("vend", "vendor", "/mnt/vendor")`), and a generic rule in
  `guest/initramfs/rootfs/init` lays a layer's files under a PAKS mount point over the pak (a read-only
  overlay, the highest layer on top, as for the rootfs).
- **Partition 36 has no node** (`Release.partitions` leaves `vendor` out), so lginit_app's own
  mount of it fails as for the partitions the firmware already finds absent: it logs `mounting
  fail on vendor` and `_sys_mount_tbl_init : mount fail (vendor -> /mnt/vendor,squashfs)` and goes
  on. Measured: the first 420 normalised lines of the boot log are otherwise those of a boot with
  the node (dbus GUIDs and mpool addresses aside); RELEASE references no p36 or vendor path
  statically (only a generic `/dev/mmcblk0p%d` for its read-write partitions).
- **libgal** is `guest/layers/simplesmart/src/libhalgal/halgal.c` built with `GAL_GX=1`
  (`guest/layers/netcast45/meson.build`): `GAL_GX_*` names, without the functions SimpleSmart's HAL added
  (`SS_ONLY`), and the MStar system bring-up the stock `GAL_GX_Init` does once per process
  (`MDrv_MMIO_Init`, `MsOS_Init`, `MAdp_MPool_Init`, `MDrv_SEM_Init`, `MDrv_MIU_Init`,
  `MDrv_SYS_Init`, weak). SimpleSmart's libhalgal is byte-identical after stripping. The layer
  installs it as `mnt/vendor/libgal.so.0.0.0`, which the vendor pak's `libgal.so` and `libgal.so.0`
  links reach.

The ABI is that of SimpleSmart's HAL, of which `GAL_GX` is the NetCast ancestor (stock libgal
disassembly): the same per-function `gfx_*` mapping, the
1056-byte surface (offset, then the pool address `gfx_CreateSurface` returns, u16 pitch, bpp,
width, height, format, palette, length), the 72-byte capability, 36-byte blit and 12-byte draw
settings, the same argument orders, 0 for success, -1 for failure, -2 for a zero-sized surface.
procmgr maps `/dev/fb0` from file offset 0 (pool-relative), which the GOP node accepts.

Measured on the Pi: procmgr's `PM_GX_Initialize` initialises GAL (`[libgal] Init`), creates its
surfaces in the pool and sets the cursor plane (controller 2); crashmgr's
`HOA_GX_UseGFXHWDirectly` succeeds; procmgr no longer logs `_gbGraphicInit is FALSE` once it is
initialised. RELEASE maps our `/mnt/vendor/libgal.so.0.0.0`; the GOP node counted none of
libmadp's private `/dev/fb0` pool ioctls (`0x4628`-`0x462c`), so liblm14's weak `GAL_GX_*` copies
and their `gfx_*` path are not reached.

## Appctrl startup and pointer coordinates

The target pre-mounts `open.pak` at `/usr/local` and leaves partition 26's node absent,
as for the vendor and lglib paks. The guest derives the active `svclist_gip.rc` in
tmpfs and binds it over the read-only original. LG executables and the firmware
collection stay unchanged. The study records the measured notification race and
startup experiments.

The derived service list changes only appctrl's executable path to the layer's
`appctrl-launch`; its stock PRE ordering, arguments, wait policy and respawn policy
remain intact. `prepare-services` prepares the bind mount. The launcher immediately
executes unchanged stock appctrl with its original PID and normalized stock
`argv[0]`, adding the process-specific preload `libappctrl-ready.so`. The shim's
constructor restores the original `LD_PRELOAD` environment so appctrl's children
do not inherit the shim.

The shim intercepts only the synchronous `int HOA_PROC_SetReady(void)` call.
It resolves `lg.service.special.broadcast` to its owning PID with D-Bus
`GetConnectionUnixProcessID`, then polls stock procmgr's
`HOA_PROC_GetProcessState` until RELEASE has reached LOAD (`0x20`), READY
(`0x41`) or foreground (`0x42`). RELEASE arms its notification subscription
before sending the registration request that establishes LOAD. The shim then calls
the original function through `RTLD_NEXT` and preserves its actual return value.
The stock function supplies the genuine readiness notification; neither RELEASE's
flag nor a synthetic event is used.

This narrower gate preserves appctrl's early registration, threads, receivers
and initialization checks. Delaying the entire executable delivered readiness
but failed bounded NetCast, HOME and navigation checks in both traced and
untraced packaged boots. Preserving earlier initialization is the reason for
choosing the Ready-call gate, not an established explanation of those focus
failures. In the packaged Ready-call gate boot, the launcher held the real call
until RELEASE reached `0x20`, then invoked stock SetReady. A subsequent read-only
check found RELEASE's appctrl-ready flag equal to 1. Cold and delayed key tests
still produced black captures. The failed packaged boots also logged shared-lock
initialization assertions, and RELEASE's actual namespace lacked `/dev/shm`.
The stock focus warnings also occurred in the working prototype and do not
establish the black-screen cause. The notification race is resolved in that
measurement. The later mount-point restoration below recovered UI launch.

For NetCast 4.5, init restores `/webos/dev/shm` as a mode-0777 directory,
matching the selected packed root, after devtmpfs hides the firmware's `/dev`.
Stock rcS then calls `mountvirtfs`, which mounts its own tmpfs at `/dev/shm` with
the default mode 1777. This follows the existing restoration of `/dev/pts` and
`/dev/devtmp` mount points. A fresh untraced boot confirmed the stock mode-1777
tmpfs mount and a successful full-namespace POSIX shared-memory probe. Shared-lock
initialization assertions ceased, and stock services and UI launch recovered: NetCast opened the MyApps
tray and Picture settings; HOME opened FX HOME and RIGHT moved focus. Pixel
comparison showed changes confined to the focus region, with all dashboard
widgets retained. Repeated and idle captures were identical to the first RIGHT
capture and matched the working prototype. These results establish storage and
functional UI recovery. The user accepted this packaged boot visually.

For this target init starts the existing `virtio-pointer` bridge at 1280×720 before
LG services. It grabs QEMU's tablet, rescales its advertised axes and creates the
recognized M-RCU input device. procmgr scales those logical coordinates to its
1920×1080 panel. GAL retains the vendor's behavior for out-of-panel positions:
negative coordinates become zero; only invalid hotspots are refused.

## Console

LM14's `/sbin/init` is BusyBox (SimpleSmart's is sysvinit 2.86), which opens an inittab line's
first field as its terminal: the stock `si::sysinit:/etc/init.d/rcS` would run rcS on `/dev/si`,
which no node is, and rcS never ran in the target. init gives each such id the firmware's console
(`/dev/si -> hvc0`), only when `/sbin/init` is BusyBox.

## Input: HWI2C

LM14's MStar build drives HWI2C from user mode (libdrvHWI2C), not through UTPA2's kernel
commands SimpleSmart's and W16R's use. `MAdp_IIC_Read`/`Write` take the HAL's channel (RELEASE.cfg's
minus one): channels 0, 2, 4, 5 are HWI2C ports 1, 18, 33, 41; channels 1, 6, 7, 8 are software
I2C on GPIO pins (`MApi_SWI2C`). The micom (`[i2c]MICOM:1:0x52`) is on channel 0, port 1; the NVM
EEPROM (`NVM:2:0xA0`) is on channel 1, software I2C, which is not modelled.

Before the model the HWI2C banks were plain register storage, so every HWI2C transfer reported
success: the flag the driver polls and the acknowledgement bit read back what it wrote. In 150
seconds RELEASE read the micom 2515 times, each "successfully" with whatever the data register
held, and wrote 0x48 on channel 4 and read 0xC0 on channel 5 the same way; the software-I2C
channels failed (`0xff`).

`lm14_hwi2c=1` traps the two register pages of the six ports (0x1f223000, 0x1f243000) and
models the master byte by byte from the selected HAL (layout in mstar-utopia/hwi2c.c): START, STOP,
send, receive with ACK or NACK, the interrupt flag, the state. An address `i2c=` places no device
at (SlaveCfg = port index << 8 | 8-bit slave; `micom:0x0052`) is not acknowledged. Messages go
to lgemu's devices whole: a write at its STOP or repeated START, a read when its address is sent
(32 bytes fetched, handed out by the receives). `tests/regress/mstar/check-lm14-hwi2c.c` is the primary
test.

With it, the micom is asked 0x9A (power-on mode, answered: remote key); RELEASE's `MICOM_Task`
polls 0xC0 for three bytes about six times a second, as on webOS, and lgemu's micom answers it
from its IR queue: a viewer key arrives as `02 <code> 83` (press) and `0a <code> 83` (release),
after which RELEASE reads 0xB1 (power state). 0xF9, 0xD0, 0x51 and 0x80 (RTC) are polled too and
not answered (lgemu's micom has webOS's command set); RELEASE goes on without them. The writes
are acknowledged. Channels 4 and 5 are not acknowledged now. The OSD was gated by input: before
the first key procmgr set only the cursor plane.

## Display

procmgr shows the OSD on GOP controller 1 (SimpleSmart's is 0), a 1920x1080 ARGB8888 surface
pair it flips between, and the cursor on controller 2; the presenter composes whichever planes
are visible, in GOP order. Captured from the plane on the probe: VOL+ shows LG's volume dial,
INFO the channel banner (`TV 2-0 No Program Information.`). Early HOME probes showed
a `Loading...` spinner before the plane hid again; the accepted stock dashboard is
GIP/FX home, reached through NetCast `0x42`.

## Phase 2: EGL over Mesa

The stock EGL is Mali DDK r3p2-01rel3 with LG's `hoagx` window system: `EGL_DEFAULT_DISPLAY` is
the only display, an HOA_GX window id is the native window, `eglCreateWindowSurface` wraps the
window's two GX surfaces (`HOA_GX_GetWindowSurfaceInfo`) for Mali, and the swap calls
`HOA_GX_UpdateWindow(window, NULL)`; procmgr flips and shows the window. Users: fxui (FX home and
apps), lb6wk (LGBrowser6: GTK 3 and cairo's EGL backend), chrome (dlopens `libEGL.so.1` and
`libGLESv2.so.2`), the Flash runtime of the AIR addon.

- **Mesa from the gl layer's stack** (`guest/layers/gl/mesa`), built for this target with the NetCast SDK:
  gallium virgl and softpipe, GLES 1 and 2, no window system (`MESA_PLATFORMS=`, the surfaceless
  platform only), its libEGL renamed `libEGLmesa.so.1` (`-Degl-lib-suffix=mesa`, so that no
  dlopen of `libEGL.so.1` can find Mesa's by its soname), libdrm linked in statically and not
  exported (chrome carries its own old `libdrm.so.2` in `/webbrowser/lib`, which it finds first).
  The SDK's 3.0 kernel headers lack `SYS_kcmp`; the build gets the ARM EABI number (378).
  The webOS targets' Mesa options are unchanged.
- **Our libEGL** (`guest/layers/netcast45/src/libegl-hoagx/egl.c`): EGL 1.4's entry points; the display is
  Mesa's surfaceless one; a window surface is a Mesa pbuffer of the window's size
  (`HOA_GX_GetWindowResolution`); a config is offered for windows only when it is 8-bit RGB(A),
  which goes into the window's ARGB8888 surfaces; `eglSwapBuffers` reads the frame back
  (`glReadPixels`) and writes it, flipped and swizzled, into both of the window's GX surfaces
  through the pool mapping libafopenapi made (record +4), then calls `HOA_GX_UpdateWindow`. Every
  other entry point is Mesa's. Pixmaps and other native displays fail with the EGL error.
- **Both surfaces get the frame.** procmgr's update paths use the pair differently (one copies
  `surface[idx]` into the other and flips, another flips and sets the result as a frame buffer),
  so writing both is right whichever is shown; the cost is a second copy and possible tearing in
  the buffer on screen while it is written.
- **An invisible window is not a failed swap.** procmgr's core task refuses to update a window
  whose visibility is false ("visibility is FALSE. So this window can't be updated"), so
  `HOA_GX_UpdateWindow` returns -1. The stock libEGL only marks the surface to re-check the
  window's size at the next frame (`__egl_mali_post_to_window_surface`); ours does the same (it
  logs a size change; resizing the pbuffer is not implemented). Returning `EGL_BAD_NATIVE_WINDOW`
  there made cairo exit lb6wk at its first frame.
- **Where the libraries go.** lginit_app mounts the lglib pak at `/mnt/lg/res/lglib` from
  `mmcblk0p24`; init mounts it instead, with the layer over it, and the partition has no node (as
  the vendor pak). The layer's `libEGL.so.1.4`, `libGLESv2.so.2.0`, `libGLESv1_CM.so.1.1` and
  `libMali.so.0.0` (no code: it NEEDs the other two) take the vendor's names there, so every user
  finds them however it searches (chrome has no `LD_LIBRARY_PATH`); Mesa's own libraries are in
  `/usr/lib`, a default directory.
- **The render node** is mode 0666 in the firmware's namespace (init), as the firmware packs
  `/dev/mali` (0777): lb6wk runs as uid 502 and otherwise fell back to softpipe.

Measured in the target: fxui (windows 27, 28, 70), lb6wk (16) and chrome (17) create their surfaces
and run; no `eglInitialize` failure is logged and the restart loops stopped (in about four
minutes fxui started three times and lb6wk four, chrome's abort loop went from 91 aborts to
none). With the render node open, lb6wk and chrome render on virgl. The first frames' readback
took 61 ms for 1920x1080 (fxui) and 87 ms, then 33 ms, for 1280x720 (lb6wk), the copies into
the two surfaces 10, 12 and 3 ms.

## Board line and pointer

LM14's MBoot appends `macadr=` (from the macadr partition, else its default ethaddr) to the
kernel line; the layer's `board.cmdline` has it, and the launcher puts the VM's MAC there.
Without it libafcommon's `AF_UTIL_GetMacAddressFromCmdline` formatted a NULL string: appctrl and
upctrl died with SIGSEGV about every 100 seconds (libdbgfrwk's fault handler then exits 255), and
appctrl's app launches went with them. With it neither restarts.

procmgr draws the pointer into a 128x128 surface on GOP controller 2 and places it with
`GAL_GX_MoveCursor(index, cursor)`, the cursor record being SimpleSmart's (position, coordinate
space, drawn size, hotspot), which the stock libgal passes to `gfx_SetWindowPosition`. Our libgal
(GAL_GX build) makes the plane show the drawn part at 1:1 with its hotspot at the position, in
output pixels; SimpleSmart's keeps its no-op. Before, the plane had no display rectangle and the
presenter stretched the arrow over the whole output.

## Viewer remote

The GTK viewer selects its NetCast layout from `vm-info.toml`. Its remote menu
offers NetCast and the shared service remote; an explicit incompatible
`--remote-layout` is refused. The header's dashboard button uses an apps-grid
icon and the NetCast label. It and the Home/Super shortcuts use this target's
NetCast code `0x42`, including while the service remote is selected. The
settings button is labelled Menu. Remote codes come from the selected RELEASE
table recorded in the study; unestablished labels are inactive. The separate
HOME code `0x7c` is not this dashboard shortcut.
