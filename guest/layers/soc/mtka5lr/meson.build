# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The MediaTek MT5882 (LG's mtka5lr platform), W15L's SoC: the values the soc layer
# (guest/layers/soc/meson.build) and the common layer's hal_common are built with for this chip.
# rootfs/ is this chip's part of the soc layer:
#   etc/init/kdrivers.override, gpu-libs.override, prebuiltko.override
#                                       the kdriver (kdrv_mtk5882.ko, snd-mtk.ko), Mali-450 and
#                                       prebuilt Wi-Fi/Bluetooth jobs, nothing to load here
#   etc/emulator/board.cmdline          the bootloader's parameters
#
# W15L is the beehive userland over MediaTek's vendor side, all of it in the rootfs (no bsppart):
# MediaTek's MTAL user side, libmtal.so (also libkadaptor.so.0, which libhal_mtka5lr.so.0 NEEDs),
# runs stock on the emulator's MediaTek drivers (guest/kernel/modules/mtk: /dev/mtal and its
# companions), LG's beehive HAL names with .so.0 sonames (libhal_mtka5lr.so.0, libhalgal.so.0,
# libhal_crypto.so.0), and the Mali-450 driver (libmali.so.0.1 with libUMP.so.0.1).
# docs/research/mediatek-mtka5lr.md.

# The stock libraries the shims (guest/layers/soc/src/<name>) replace: name -> [the soname the
# shim takes, the stock file it NEEDs, [other names in usr/lib made links to the shim]].
# libdile_gal, the graphics stand-in: gm, libgm, libim and tvservice NEED libdile_gal.so.1
# (1.0.2, a pass-through to libhalgal.so.0 over MTAL's MTGFX/MTOSD/MTIMAGE); the shim answers above
# it, so MediaTek's 2D engine and OSD planes are never reached from there.
hal_stock = {
  'libdile_gal': ['libdile_gal.so.1', 'libdile_gal.so.1.0.2', []],
}
# No media elements yet (the stock GStreamer OpenMAX elements take libmtal.so as their IL core).
gst_plugin_dir = ''
# The emulator's frame buffers in tvservice's physical memory (guest/layers/common/hal-common/physmem.c):
# 64 MiB from here. MT5882's DRAM starts at 0 (the kernel's uImage loads at 0x7FC0; env.o's memory
# plan splits 313 MiB for the kernel, FBM, frame buffers and TrustZone after it) and its registers
# are at 0xF0000000 (mach-mt53xx's IO base), so 0x80000000 is above any DRAM size W15L's hwopt has
# (DDR_SIZE_TYPE_T up to 3 GB is not this board's: 1.5 GB) and below the registers.
soc_phys_base = '0x80000000u'
# The GPU vendor libraries the gl layer replaces: W15L's Mali-450 driver is libmali.so.0.1 (EGL,
# GLES 1.1 and 2.0, r5p0 with UMP); libEGL.so.1.4 (147 KB, eglBindWaylandDisplayWL over libgm),
# libGLESv1_CM.so.1.1 and libGLESv2.so.2.0 NEED it, which the gl layer's Mesa libraries of those
# names replace. Only libmali NEEDs libUMP, so once libmali is Mesa, libUMP is never loaded and
# needs no stand-in (as on M2).
gl_vendor_stubs = []
gl_vendor_gl = 'mali'
gl_vendor_gl_needs = ['EGL', 'GLESv2', 'GLESv1_CM']

# this chip's part of the soc layer, after its base's (guest/layers/soc/meson.build)
soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W15L 03.31.00: libmali.so.0.1 is "mali450-r5p0-linux-webos-ump-gles11-gles20"
# (Mali Utgard: OpenGL ES 2.0)
gl_es_version = '2.0'
