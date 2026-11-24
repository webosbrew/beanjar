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
  mksquashfs, once per firmware, into the VM's firmware cache (`host/core/beanjar/lz4p.py`); that
  image is the lgapp disk.
- **base and patch** (`/mnt/nsu/base`, `/mnt/nsu/patch`) and their union: lginit mounts a `unionfs`
  (`dirs=/mnt/nsu/patch:/mnt/nsu/base=ro`) at `/mnt/nsu/union`, which our kernel has not, and then
  binds its directories where the programs look (`/mnt/addon`, `/mnt/lg/res/xml`, `/mnt/browser`,
  ...). Init makes the same read-only union with overlayfs (`Release.unions`, boot.conf's `UNIONS`);
  lginit's own mount of it fails, its binds succeed.
- **lglib** (`/mnt/lg/res/lglib`), for the layer's libraries over the vendor's (as NetCast 4.5).

`/dev/shm` is created before lginit runs (devtmpfs hides the packed directory), where lginit
mounts its ramfs.

Measured on the PC (TCG): lginit, lginit_app, procmgr, RELEASE and the whole service list run,
with fxui's Home, lb6wk and chrome among them. Our arm64 kernel refuses the firmware's own 32-bit
modules (`pvrsrvkm.ko`, the kdrv modules): "Invalid architecture in ELF header", which LG's
programs log and go on from.
