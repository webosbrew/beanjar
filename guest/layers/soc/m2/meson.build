# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The MStar MSD93F2G "M2" (Munich), W16R's SoC: the values the soc layer (guest/layers/soc/meson.build)
# and the common layer's hal_common are built with for this chip. Read before guest/layers/common; rootfs/ is this chip's part of the soc layer:
#   etc/init/gpu-libs.override, kdrivers.override, prebuiltko.override
#                                       bsppart's kernel-driver, Mali and prebuilt-module jobs,
#                                       nothing to load here
#   etc/emulator/board.cmdline          the bootloader's parameters (M2 MBoot's format)
#
# W16R's board support is not in its rootfs but in bsppart.pak, which init stacks as a lower layer
# above it (targets.py, PAK_LOWERS bsppart:bsp): MStar's libhal_* (libhal_msd93f2g.so.0, alias
# libhal_m2.so.0), libkadaptor (MStar Utopia), libhalgal, the Mali driver (libmali.so, libUMP.so,
# libEGL/libGLESv2 over it, which the gl layer
# replaces), the Mali/UMP modules and the jobs overridden here.

# The stock libraries the shims (guest/layers/soc/src/<name>) replace: name -> [the soname the shim takes,
# the stock file it NEEDs, [other names in usr/lib made links to the shim](, 'bsppart': the stock
# file is bsppart's, not the rootfs's)]. libdile_gal, the graphics stand-in (the user's choice over a model of MStar's GFX/GOP in mstar-utopia): gm, libgm,
# libim and tvservice NEED libdile_gal.so.1, the rootfs's pass-through to bsppart's libhalgal over
# KADP's gfx_*; the shim answers above it, so MStar's 2D engine and display planes are never
# reached from there. W16R's DILE_GAL structures are LG115x's (checked in libhalgal and libkadaptor); libim moves the cursor with SetCursorPosition. Not built:
# libhal_lg115x and libdile_vt (LG115x's; MStar's KADP runs stock on the emulator's MStar drivers,
# guest/kernel/modules/mstar-utopia, but for its XC window calls, below).
# libhal_crypto: bsppart's (SONAME libhal_crypto.so, which libdile_crypto NEEDs) reads the model
# secret through MStar's TEE (MAdp_Secure_GetSecureDataEMMC), which the emulator does not have, so
# libsecretagent's SM_SA_GetNDUID failed and nyx's DeviceInfo made a new random NDUID at every open
#. The shim answers that one read, as on H13 (src/libhal_crypto/hal-crypto.c).
# bsppart's HAL_I2C_* (libhal_i2c.so, libhal_msd93f2g.so) run stock too, over mstar-utopia's HWI2C
# with lgemu.ko's micom and NVM EEPROM.
# libhal_avd: the analog decoder's no-signal answers over bsppart's board HAL (its SONAME libhal_msd93f2g.so,
# which tvservice NEEDs; its other names in usr/lib made links to the shim).
# libkadaptor: MStar's KADP (bsppart's libkadaptor.so.0.1.0, no SONAME: its users NEED libkadaptor.so),
# whose XC window calls run on a user-mode Utopia instance on W16R; the shim passes them to a
# kernel-mode one too, for mstar-utopia.ko's window decoder (src/libkadaptor/kadaptor-xc.c), answers
# tv.capture's questions to that user-mode XC and its DIP (kadaptor-capture.c), and
# answers the DVR key that MStar's TEE would decrypt from the VM's store (kadaptor-pvrkey.c).
hal_stock = {
  'libdile_gal': ['libdile_gal.so.1', 'libdile_gal.so.1.0.2', []],
  'libhal_avd': ['libhal_msd93f2g.so', 'libhal_msd93f2g.so.0.1.0', ['libhal_msd93f2g.so.0', 'libhal_m2.so', 'libhal_m2.so.0'], 'bsppart'],
  'libkadaptor': ['libkadaptor.so', 'libkadaptor.so.0.1.0', [], 'bsppart'],
  'libhal_crypto': ['libhal_crypto.so', 'libhal_crypto.so.0.1.0', ['libhal_crypto.so.0'], 'bsppart'],
}
kadaptor_source = ['kadaptor-xc.c', 'kadaptor-capture.c', 'kadaptor-pvrkey.c']
# Where the media elements (guest/layers/soc/src/video) go: the stock plugins' directory, and which:
# W16R's GStreamer is 1.2.4 (GLib 2.40), and its media elements are MStar's: the gst-omx decoders
# and omxaudiosink of libgstomx.so (by gstomx.conf's sections), mvsink and masink.
gst_plugin_dir = 'usr/lib/gstreamer-1.0'
gst_elements = 'mstar-m2'
# The emulator's frame buffers in tvservice's physical memory (guest/layers/common/hal-common/physmem.c):
# 64 MiB from here. M2's DRAM is two MIUs (bsppart's tee/mmap.ini: MIU_DRAM_LEN0 and _LEN1 512 MiB
# each, MIU_INTERVAL 0x80000000); MIU0 is at 0x20000000 on the bus (the kernel's uImage loads at
# 0x20208000), so MIU1 at 0xA0000000. The first address above MIU0 is free on both counts; the
# RIU registers are below 0x20000000.
soc_phys_base = '0x40000000u'
# The GPU vendor libraries the gl layer replaces with empty stand-ins: none. bsppart's Mali-450
# driver is one library, libmali.so (EGL, GLES 1 and 2, the Wayland EGL platform), which Qt, the
# compositor, its plugins and whrz4tvclient NEED by that name; libEGL.so.1.4, libGLESv2.so.2.0
# and libwayland-egl.so.0.1 are 3 KB wrappers NEEDing it. Only libmali NEEDs libUMP (and imports
# ump_*, GM_GetWindowSurfaceInfo, GM_UpdateWindowWithBackSurfaceInfo, GM_GetWindowProperty), and
# nothing else imports a mali_* or ump_* function, so once libmali is Mesa, libUMP is never loaded
# and needs no stand-in.
gl_vendor_stubs = []
# The vendor's one GL library (guest/layers/soc/h13/meson.build): libmali.so, NEEDing Mesa's libEGL,
# libGLESv2 and libGLESv1_CM, as libmali.so.0.1 with its .so and .so.0 links (bsppart's names).
gl_vendor_gl = 'mali'
gl_vendor_gl_needs = ['EGL', 'GLESv2', 'GLESv1_CM']

# this chip's part of the soc layer, after its base's (guest/layers/soc/meson.build)
soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W16R 05.70.50: bsppart's libmali.so.0.1 prints "OpenGL ES 2.0", "OpenGL ES GLSL ES 1.00"
# (Mali Utgard)
gl_es_version = '2.0'
