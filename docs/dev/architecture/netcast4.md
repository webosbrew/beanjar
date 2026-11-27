# NetCast 4 (H13) on the emulator

LG's NetCast 4 on its H13 SoC (LG1154), the 2013 line before webOS: LG's 3.4 kernel, glibc 2.14.1,
LG's process manager (procmgr) and one large application, RELEASE. The firmware here is the
European FHD build 04.28.15 (`HE_DTV_NC4H_AFAAABAA`, RELEASE.cfg `DVB-LG115X`, a 47 inch LCD). It
boots as the target `h13-netcast4`; it is still being brought up (`targets.BRING_UP`). The facts
it rests on are in [the groundwork](../../research/nc4h-groundwork.md). This file is what was
decided.

## The target

`guest/targets/h13-netcast4.toml` (model NC4H, its own `[soc]`: lg115x.ko with H13 B0's chip
identity and the I2C devices of this firmware's RELEASE.cfg) and `guest/targets/release/netcast4.toml`,
platform "NetCast" 4. Recognition (`paths.firmware_release`): a sysvinit `root.pak` and `mode.pak`'s
`RELEASE.cfg`, as the other NetCast-line targets, and no vendor pak: NetCast 4's GAL_GX `libgal.so`
is in the lglib pak. NetCast is native-only (`vmsettings.NATIVE_ONLY`): on an x86 PC the VM runs the
arm64 guest kernel under TCG, with the 32-bit userland in AArch32.

The guest build is `build/guest-h13-netcast4`, set up with `meson/cross/lgtv-sdk.ini` as NetCast 4.5's
(`-Dsoc=h13 -Dwebos=netcast4`). It builds the netcast4 layer, the kernel and the initramfs.

## Boot chain

LG's kernel has `CONFIG_LG_INIT`: it mounts the `lginit=` partition at `/mnt/lg/lginit` itself and
runs its `lginit` as PID 1, before `/sbin/init` (LG's `init/main.c`). root.pak's BusyBox inittab and
rcS run only on a stand-alone "autorun" boot. So the release row names the program
(`Release.init = "/mnt/lg/lginit/lginit"`, boot.conf's `INIT`), which `webos-start` runs as PID 1 of
the firmware's namespace instead of `/sbin/init`. lginit mounts proc, sysfs, devpts and its ramfs
and tmpfs mounts, then runs `lginit_app`, which mounts the paks from `/dev/mmcblk0pN` (the partition
map is its own `partinfo`, 64 entries, `part.pak` byte for byte) and starts procmgr with
`svclist_swue.rc`; procmgr starts RELEASE and the services.

Init mounts what our kernel cannot take from a node, or what a layer lies over, and leaves those
partitions without a node (lginit_app's own mount of them fails and it goes on, as for the
partitions the firmware leaves absent):

- **lgapp** (`/mnt/lg/lgapp`, RELEASE): the pak is an LZ4P-wrapped XIP cramfs, which lginit_app
  mounts as `lgapp_xip`, a filesystem of LG's kernel. Its RELEASE is stored uncompressed for the
  kernel to map in place and is 25027700 bytes, more than a cramfs inode's 24-bit size field says.
  The launcher unpacks the pak with epk2extract (LZ4P and LG's XIP cramfs) and packs the tree with
  mksquashfs, once per firmware, into the VM's firmware cache (`host/core/beanjar/firmware/lz4p.py`); that
  image is the lgapp disk.
- **base and patch** (`/mnt/nsu/base`, `/mnt/nsu/patch`) and their union: lginit mounts a `unionfs`
  (`dirs=/mnt/nsu/patch:/mnt/nsu/base=ro`) at `/mnt/nsu/union`, which our kernel has not, and then
  binds its directories where the programs look (`/mnt/addon`, `/mnt/lg/res/xml`, `/mnt/browser`,
  ...). Init makes the same read-only union with overlayfs (`Release.unions`, boot.conf's `UNIONS`);
  lginit's own mount of it fails, its binds succeed.
- **lglib** (`/mnt/lg/res/lglib`), for the layer's libraries over the vendor's (as NetCast 4.5).

`/dev/shm` is created before lginit runs (devtmpfs hides the packed directory), where lginit
mounts its ramfs.

NetCast 4 has no `/etc/device_table`: its root packs its device nodes in `/dev` (919 of them, 81 in
`/dev/lg`: LG's kdrivers, `iic*`, `sys0`, `be0`, `afe0`, `gfx0`, ...), which devtmpfs hides too.
Init copies the packed `/dev/lg` nodes of the majors lg115x.ko registers (31) onto devtmpfs before
lginit runs; nodes of drivers the emulator has not stay absent. Without them RELEASE reached none of
its I2C devices (the micom, the EEPROM) and none of lg115x.ko's other drivers: the micom never saw a
read (`run/micom-status` stayed `ready=0`) and no remote key reached the firmware.

## Console

lginit_app's terminal manager (tmanager, LG's libdbgfrwk) gives every process it starts a pty of
its own and forwards their output only to a terminal attached to it; on its own console it shows
its first lines alone. LG's root login attaches one: lginit_app writes a `/root/.profile` that runs
`dbgfrwk elogin`, whose tmanager command (`/tmgr/elogin <tty>`) opens the client's terminal and
sends every pty's output there. Init does the same for the webOS console once lginit_app and
`/usr/local/bin/dbgfrwk` are there (`dbgfrwk elogin` with hvc0 as its standard streams; the client
stays, relaying input), so the firmware's output is in `logs/webos.log` from about two seconds
after lginit_app starts.

Measured on the PC (TCG): lginit, lginit_app, procmgr, RELEASE and the whole service list run,
with fxui's Home, lb6wk and chrome among them. Our arm64 kernel refuses the firmware's own 32-bit
modules (`pvrsrvkm.ko`, the kdrv modules): "Invalid architecture in ELF header", which LG's
programs log and go on from.

## Graphics

The boundary is NetCast 4.5's ([netcast45.md](netcast45.md)): our libgal replaces LG's `GAL_GX_*`
(procmgr and RELEASE stay stock), Mesa replaces the PowerVR libraries behind NetCast 4.5's hoagx
libEGL, and lg115x.ko gives a scanout-only frame buffer node. Nothing models LG's 2D engine or its
fbdev.

- **The frame buffer node** (`gop=1`, `guest/kernel/modules/lg115x/gop.c`): `/dev/lg/lx-gop`,
  the emulator's GOP contract (`include/mstar-gop.h`) over lgemu.ko's graphics memory: a 192 MiB
  pool reported at the physical address 0x70000000 (a mapping at an offset from it is one at that
  address), surfaces allocated in it, and the planes that say which surface each OSD layer shows.
  It draws nothing. Init binds it over the firmware's `/dev/fb0` and `/dev/gfx` (packed as a link
  to `/dev/lg/gfx0`); the initramfs's presenter (`gop-present`) composes the visible planes onto
  virtio-gpu's display, as for SimpleSmart and NetCast 4.5.
- **libgal** (`guest/layers/simplesmart/src/libhalgal`, built with `GAL_GX=1` in
  `guest/layers/netcast4/meson.build`): the layer's `mnt/lg/res/lglib/libgal.so.0.0.0` lies over the
  lglib pak's, whose `libgal.so` links reach it. RELEASE `NEED`s `libgal.so`; every process gets it
  through lginit_app's `LD_PRELOAD=/lg/res/lglib/liblibm.so:/mnt/lg/res/lglib/libgal.so`.
- **procmgr's KADP_* calls** (`guest/layers/netcast4/src/kadp.c`, in the same library). procmgr
  draws its OSD through `GAL_GX_*`, but its cursor (`PM_CURSOR_DRAW_*`) and its display size
  (`PM_DISPLAY_GetDisplayResolutionCore`) call 17 `KADP_*` functions of libkadaptor directly:
  `KADP_FBDEV_*` for fb3 (a 128x128 double-buffered layer: GetCfg's max_xres/max_yres per layer,
  GetSurface, GetDrawScreenIdx, UpdateScreenByIndex, SetVisible, MoveCursor with six s16 in panel
  pixels: size, hotspot, position), `KADP_GFX_*` (AllocSurfaceEx of the 48-byte KADP surface,
  SimpleFillRect, SimpleStretchBlit) and `KADP_PNG_DecodeImage` (into the surface found by its
  physical address). Ours serve them on GAL surfaces, fb3's two buffers on GAL controller 2 placed
  with `GAL_GX_MoveCursor`. **They reach procmgr only through the `LD_PRELOAD`:** libkadaptor
  exports the same names, and only the preloaded library comes before it. Every other process
  (mediaframework's GStreamer sinks call `KADP_GFX_*` too) has each call passed on to libkadaptor's
  (`dlsym(RTLD_NEXT)`), unchanged; `KADP_InitSystem` is not taken over.
- **What procmgr shows.** Its OSD is a pair of 1280x720 ARGB8888 surfaces on controller 1, which it
  flips (`GAL_GX_SetFrameBuffer`) and shows scaled to the 1920x1080 panel
  (`SetFrameBufferProperty`: view 1280x720, display 1920x1080); client windows reach the screen
  only through it (no fbdev flip). RELEASE draws its own UI (the first-use wizard, its banners)
  with GAL blits into that pair.
- **EGL** (`guest/layers/netcast45/src/libegl-hoagx/egl.c`, unchanged): NC4H's libEGL is a PowerVR
  wrapper whose window system (libpvrNC4_WSEGL) takes HOA_GX window ids through the same
  `HOA_GX_GetWindowSurfaceInfo` (1064-byte record) and `HOA_GX_UpdateWindow` as NetCast 4.5's, and
  NC4H's liblgopenapi and libafopenapi export those and `HOA_GX_GetWindowResolution`. The layer's
  `libEGL.so.1.4`, Mesa's `libGLESv2.so.2.0` and `libGLESv1_CM.so.1.1` take the vendor's names in
  the lglib pak; `libIMGegl.so` and `libsrv_um.so` are stubs without code (fxui `NEED`s them and
  imports nothing of them). fxui links EGL, chrome dlopens it, lb6wk gets it through cairo.
  libafopenapi's `HOA_GX_GetDeviceCapability` maps the pool from `/dev/gfx` (open, then mmap at the
  pool's address): without that node `HOA_GX_GetWindowSurfaceInfo` failed and no frame reached a
  window. Measured under TCG: fxui's first 1920x1080 frame took 116 ms to read back and 65 ms to
  copy into the window's two surfaces.

Measured on the PC (TCG), on a new VM: RELEASE reads the micom's power state about 16 s after
start (`ready=1`), reads the EEPROM (DDM_VerifyTask logs the new chip's debug level 3 and
INSTOP_INFO 0) and draws its live-TV OSD, "Not Programmed" over the black video plane, then LG's
no-signal screen saver (the LG cube with "Not Programmed"). Without the `/dev/lg` nodes the same VM
stayed black (procmgr set only its cursor plane); a VM whose eMMC NVM earlier boots had written
showed RELEASE's first-use wizard ("Welcome! Press the WHEEL(OK) button"), a full-panel 1280x720
OSD whose top and bottom bands are a translucent black shade over the video plane.

## Secure boot and the micom

RELEASE's partition verification is the one deliberate exception to running LG's programs
unchanged: the netcast4 layer's libgal sets RELEASE's exported `gFullVerify` to 1
(`guest/layers/netcast4/src/verify.c`), and nothing else of RELEASE is touched. The evidence, from
the firmware's output on the console and RELEASE's code (04.28.15):

- **Without it the set powers itself off about 4 s after RELEASE starts**: micom write `25 00 43`,
  which `DDI_MICOM_SendSystemPowerControlCommand` sends for `API_DDM_POWER_Shutdown_ByVerifyFail`
  (0x43 is "VERIFY FAIL" in `API_DDM_POWER_ShutdownSystem`'s reason table). Its only caller is
  `DDM_VerifyTask` (init task tVerifyInit), which `MAIN_Root` always starts, the last of its init
  tasks, with no condition on the debug status, the command line, NFS, update mode, an environment
  variable or a file. It checks the signatures of base, patch, opensrc, lglib, emanual, estreamer
  and extra with `sb_verify_application` and shuts the set down after a second failure ("base verify
  failed in 2nd check ... OTP will be set and System will be shutdown").
- **The check needs per-set key material.** `sb_verify_application` and `sb_key_dec` are RELEASE's
  own, statically linked code (only `KADP_SE_*` come from libkadaptor), so there is no library
  boundary at which to answer "verified". It reads `sbkey=` from `/proc/cmdline` (missing or 0:
  failure), maps that physical address through `/dev/mem`, decrypts 1 KiB with `sb_key_dec` and
  wants the magic 0xe1761312, then a signature block at each partition's end (magic 0x36485434).
  The bootloader (boot.pak: "Secureboot Init", "sbkey=0x%x", "fullverify") passes the address of
  the blob it decrypted from the set's `secureboot` partition. RELEASE has no "secure boot off"
  path; the OTP flag only selects a full check over a sampled one. The project does not depend on
  per-set key material.
- **The one stock exit.** With `gFullVerify` nonzero the task logs "Verify end", sets `gVerifyEnd`
  and returns. Nothing in RELEASE writes it (SWUE has its own copy). Our libgal sets it at RELEASE's
  first `GAL_GX_Init` (halgal.c's `gal_gx_process_init`), which runs in tGFXInit, the first init
  task, before `MAIN_Root` creates tVerifyInit. Not in a constructor: RELEASE's start-up zeroes its
  bss after the preloaded libraries' constructors (a 1 set there read 0 about 300 ms later).
- **The micom.** At start RELEASE reads 0x9A (power-on mode, answered 0x20) and 0x80
  (RTC), writes its locale (4B), key-lock time (72), baud rate (32 07), child lock (4D), CEC sync
  (4F), LED mode (77) and "application started" (2A), and reads a few things lgemu's micom does not
  answer, which RELEASE retries three times and goes on from: D1 and D0 (the echo of GPIO output
  ports 0x2103 and 0x2102 after 3A <v> and 36/37), 51 (RS-232 key lock, 0 or 1), A8 (logo light,
  2 bytes) and F9 (RGB sensor).
- **How remote keys reach the UI.** `MICOM_Task` polls the micom every 40 ms; until
  `MAIN_StartApplication` enables polling (`API_DDM_MICOM_NotifyToMicomPowerOnInitEnd`) it writes the
  dummy `FF FF` instead. Then it reads 0xC0, three bytes: type 2 an IR key, 3 a local key; the check
  wants byte1 + byte2 = 0xFF, so lgemu's queue (`02 <code> <~code>`) fits. The code maps through
  RELEASE's IR table (0x44 OK to UI key 28, 0x7C HOME, 0x28 BACK, 0x5B EXIT) to the UI's callback
  and to its uinput device "LGE RCU" (`MICOM_UINPUT_Init`).
- **The first-use wizard.** `MAIN_StartApplication` creates it when NAND NVM registry byte 0x521 is 0
  and EEPROM 0x739 (INSTOP_INFO, FactoryDB 49) is 1. The new chip (`beanjar/board/nvmfactory.py`, the same
  fields as NetCast 4.5's, debug status 3 to 5 at 0x18A) has INSTOP_INFO 0. Its welcome page takes OK
  (UI key 28) once its app-store storage popup (which locks the keys for up to 40 s) closes.

Measured on a new VM: the remote's keys reach RELEASE (lgemu's micom trace: HOME is read as
`02 7c 83` then `0a 7c 83` from 0xC0, each followed by a power-state read), and HOME takes the
no-signal OSD away, but the screen then stays black: FX HOME was not seen.

## Open

- **Home and navigation.** After HOME nothing is drawn (procmgr shows no window). Not studied yet:
  which app HOME starts on NC4H and why its window is not shown; once, about 180 s after start, the
  set restarted (SIGHUP, cause not found; the micom trace was off).
- **The NetCast regress** (`tests/regress/netcast/run.py`) shares the "NetCast" platform entry with
  NetCast 4.5; NC4H has no thresholds of its own yet (its home step waits for FX HOME).
- **Other NC4 builds**: only the EU FHD build 04.28.15 (`HE_DTV_NC4H_AFAAABAA`) has run; UHD and OLED
  variants of the line are not tried.
