# SimpleSmart (SS1A) on the emulator

LG's SimpleSmart line is NetCast, not webOS: sysvinit, LG's own process manager (procmgr) and one
large application, RELEASE, drawing its UI through DirectFB and LG's GX layer. The firmware here
is `HE_LCD_SS1A_AFAAAIAA` 04.10.06.01 (MStar M2, the US LH5700 49" set its `RELEASE.cfg`
describes). It boots as the target `m2-simplesmart` to its stock GUI in the existing viewer. The
reviewed design this follows is the one approved before the work started; this file is what was decided and
measured while building it.

## The target

`guest/targets/m2-simplesmart.toml`: model SS1A and its `[soc]` (SS1A's use of the M2 chip; W16R's
`guest/targets/soc/m2.toml` is unchanged), and `guest/targets/release/simplesmart.toml` (stated
whole, nothing of the webOS releases'). The files' comments give the evidence for each field.

- Recognition (`paths.firmware_release`): no `rootfs.pak` and no `starfish-release`; a SimpleSmart
  firmware is a sysvinit `root.pak` (`etc/inittab`) with a model pak `mode.pak` whose `RELEASE.cfg`
  names the OTA ID (`szOtaid`). The model code is the `_LCD_` part of the name (`SS1A`).
- Paks: epk2extract's four-letter names. Init mounts only the root (p18, LZO) and lginit (p19);
  stock `lginit_app` mounts every other pak from `/dev/mmcblk0pN` with its own mount tables. Those
  paks are attached writable with `snapshot=on`: the set's eMMC partitions are writable devices and
  `lginit_app`'s mount(2) passes no `MS_RDONLY`, which a read-only disk refuses. No `tplist`,
  `preload`, `emanual` or `cert` pak ships; their mounts fail as on a set with empty partitions.
- Partition map: `lginit_app`'s own `partinfo` (65 entries, equal to `part.pak`).
- 24 disks do not fit the virt machine's root PCI bus beside the other devices:
  `qemu.pci_budget` puts every virtio-blk disk behind one PCI bridge when the root bus would
  overflow (the webOS targets fit and are unchanged).
- Layers: one always-used layer, `guest/layers/simplesmart` (`Release.always_layers`); the guest build
  of the target builds only it, the kernel and the initramfs (`meson.build`, `simplesmart`).
- The kernel gets `CONFIG_SQUASHFS_ZLIB` (every SS1A pak but the root is zlib).

## Boot

`guest/initramfs/rootfs/init` with `INIT_SYSTEM=sysvinit`: the firmware's `/sbin/init` as PID 1 of the
firmware namespace, on hvc0 like webOS. Before it starts, the firmware's own
`makedevs -D /etc/device_table` runs from its root (on a set the nodes come from
`kdrv_msd93f2g.ko` on devtmpfs; `lginit`'s own makedevs runs inside the read-only lginit squashfs
and makes nothing). No PmLog, trust stores, pointer, IR or media pumps.

**Kernel modules.** Every module the firmware loads (MStar's kdriver, Mali, UMP, Broadcom Wi-Fi,
TFAT/TNTFS, USB and block drivers) is an ARMv7 build for LG's 3.10.23 kernel and can never load
here. Decision (the user's): inside the firmware's root, `insmod`, `rmmod` and `modprobe` are one
logging no-op that succeeds (`guest/layers/simplesmart/rootfs/sbin/insmod`); no allow-list. RELEASE and
the rc scripts call them by path; `lginit` (static) loads nothing itself (traced live). Our arm64
modules (`lgemu.ko`, `mstar-utopia.ko`) load from the initramfs before the firmware starts.

**Board.** `guest/layers/simplesmart/rootfs/etc/emulator/board.cmdline`, keys and formats from SS1A's
MBoot (`boot.pak`), values from its `RELEASE.cfg`; the file marks which values are the emulator's.
ToolOpt is seven words (MBoot's format). SS1A's stock `lginit_app` mounts a proc of its own over
`/proc`, which hides a bind over `/proc/cmdline`, so for this release (`Release.board_in_kernel`)
the launcher puts the board line on the kernel's command line itself, first: `libafcommon`
(`AF_UTIL_GetMacAddressFromCmdline`) reads only the first 512 bytes and RELEASE 1024. With the
board line elsewhere, `appctrl` and `upctrl` crashed printing a NULL `macadr` and RELEASE's
hwopt search missed. The VM's MAC and serial replace `macadr=` and `serialNum=`.
The board's model is the firmware's broadcast standard's: `targets.firmware_broadcast` reads it
from `RELEASE.cfg`'s `modelName` (`ATSC-M2`, `DVB-M2`), and a DVB build (05.00.06, EU) boots
`board-dvb.cmdline` (`broadcast_boards`), the European LH570V of its `mspec.EU.LCD`; the US
builds the US LH5700 of `board.cmdline`. Everything else on the two lines is the same.

**RAM** is the 1 GiB of SS1A's two 512 MiB MIUs (`vend.pak`'s `mmap.ini`), not an hwopt digit.
**The display** is the panel's size (hwopt digit 3, `DILE_BOARDINFO_GetDisplayResolution`):
1920x1080; the GOP places its planes in panel pixels (`Release.screen = "panel"`).

**Micom.** lgemu's micom model answers SS1A as it does webOS: power-on mode 0x20 (remote key),
power state 3. RELEASE also reads 0xF9 (unanswered, tolerated). `lgemu.micom_trace=1` logs every
read and its answer.

## Memory

The target's `[soc]` mstar-utopia parameters: 90 mmapger records (SS1A's `MAdp_SYS_GetMMAP` takes 0-89),
4 MiB each so that all lie in MIU0, and the records SS1A's `MAdp_MPool_Init` does not map nested in
POOL0 (88) as on the board (`mmap_in`: VD_3DCOMB, TSP_BUF, VBI_1, VQ_BUF, VBI_2, TTX_BUF at MBoot's
offsets): KADP's `PA2KSEG1` finds only addresses inside a mapped record. GOP_FRAMEBUFFER (27) is
the board's 48 MiB: procmgr's and DirectFB's surfaces take more than 40 MiB at start (32 MiB ran
out at the twelfth surface).

## Display

Stock path: RELEASE (Nano-X and DirectFB) and the other processes draw into surfaces they get from
procmgr's GX manager through `libgmopenapi`; procmgr allocates them with `HAL_GAL_CreateSurface`
and puts windows on the GOP with `HAL_GAL_SetFrameBuffer`. The vendor's `libhalgal.so` drove
MStar's GE and GOP through `libkadaptor`'s `gfx_*`. Option (c) of the design: ours replaces it.

- **The GOP node** (`include/mstar-gop.h`; mstar-utopia's `gop_fb=27`): the GOP_FRAMEBUFFER
  record's pages of mstar-utopia's physical store, a first-fit allocator, and the four GOP
  controllers' planes with a generation the presenter waits on. Init binds it over the firmware
  namespace's `/dev/fb0` (lginit links `/dev/gfx` to it); the initramfs's `/dev/fb0` stays
  virtio-gpu's. Any other ioctl is `-ENOTTY`.
- **mmap convention, measured:** the stock users map the pool from its start two ways and add a
  surface's offset: `libgmopenapi` at file offset 0, procmgr at the capability's
  `physicalAddrOfBase` (the pool's PA). The node accepts exactly these: an offset below the pool's
  size is pool-relative, one in [PA, PA + size) is a PA. With PA offsets only, `libgmopenapi`'s map
  failed and RELEASE's Nano-X wrote through a NULL frame.
- **Surface offset 0 is "no surface"** to `libgmopenapi`'s window lookup: the allocator never
  hands out the pool's first page (RELEASE's screensaver blitted through a NULL window surface).
- **Controllers are GOP numbers:** procmgr shows the OSD on 0 and the cursor on 2
  (`maxNumOfFBHWControllers` stays the stock 2).
- **`libhalgal.so`** (`guest/layers/simplesmart/src/libhalgal`, in the layer's `/lib`, ahead of
  `/mnt/vendor` on the stock `LD_LIBRARY_PATH`): the 35 `HAL_GAL_*` symbols of the stock library,
  SS1A's structure sizes (surface 1056, capability 72, blit settings 36, draw settings 12), drawing
  in software with `guest/layers/soc/src/libdile_gal/gal-draw.c` (DirectFB's blend semantics), PNG
  decoding with the firmware's own libpng12 (no JPEG: the capability offers PNG only).
  `HAL_GAL_GetFrameBufferList` returns a buffer malloc'd per call, which the caller frees
  (RELEASE's `PQFRWK_CheckOSDSize`; a static one aborted RELEASE in `free`). Mirror, 3D,
  stereoscope, rotation and cursor moves are kept or refused (`HAL_GAL_NOT_CALLABLE`), not faked.
- **The presenter** (`guest/initramfs/src/gop-present.c`): composes the visible planes in GOP order,
  each scaled from its view to its display rectangle, straight alpha over black (the video plane
  without signal), into the scanout, at most 60 times a second on a new generation and twice a
  second besides (DirectFB may write a shown surface without the HAL). With virgl it scans out a 3D
  resource in R8G8B8A8 and sends it with a transfer: a dumb buffer's XRGB never reached the Pi's
  GLES host (the viewer received only black). SIGUSR1 dumps the composed picture to
  `/run/gop-present.ppm`.

## The GUI

In no-signal mode RELEASE's UI main loop waits for a message: with a fresh state the screen stays
black (the video plane) until a key comes. Keys reach RELEASE as on webOS's tvservice: its
`MICOM_Task` polls the micom's event queue (read 0xC0), which lgemu's IR receiver fills from the
viewer's `webos.ir` port through the initramfs's `virtio-ir` pump (started for SimpleSmart too; no
pointer or media pumps). HOME (0x7C) opens SimpleSmart's stock launcher bar, in English: Netflix,
Amazon, VUDU, LG Content Store, Inputs, Guide, My Media, Channel List, Screen Share, Settings. It
stayed up, unchanged and without a restart, for the three minutes it was watched.

Not modelled (out of this target's scope): networking, media, the cursor plane's position, Wi-Fi.
SS1A's networkmanager crash-loops (a NULL function pointer under `nm_is_ethernet_linked`, from
`nm_get_wl_ifname`; procmgr restarts it about twice a second) without affecting the GUI.

To boot it on a Pi with the viewer (the firmware `04.10.06.01-HE_LCD_SS1A_AFAAAIAA` in the Pi's
firmware collections, `firmware_dirs`):

    INSTANCE=ss FW=04.10.06.01-HE_LCD_SS1A_AFAAAIAA NET_LINK=off DISPLAY_BACKEND=dbus host/scripts/vm-launcher start
    build/viewer/beanjar-viewer unix:path=$PWD/build/instances/ss/dbus.sock --no-media

then HOME on the viewer's remote (its keyboard's Super key), or
`tests/regress/ir-key.py --sock build/instances/ss/ir.sock 0x7c` with no viewer attached
(`ir.sock` takes one client). `targets.BRING_UP` keeps SS1A out of `scripts/regress`'s default set:
its checks are webOS's.

## Bring-up notes

The firmware writes symbolized crash reports to `/mnt/lg/cmn_data/exclog/` (the `data` disk);
`debugfs -R "rdump /exclog DIR" state/default/data.img` reads them on the host. Its console output
is quiet at debug mode 3 and it lowers the kernel's console log level; `ignore_loglevel` on the
kernel command line keeps lgemu's messages visible.
