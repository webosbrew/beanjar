# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The Realtek RTD2872 "K5LP" (U-Boot's chip=K5LPA0), W19P's SoC: the values the soc layer
# (guest/layers/soc/meson.build) and the common layer's hal_common are built with for this chip.
# Read before guest/layers/common; rootfs/ is this chip's
# part of the soc layer:
#   etc/init/gpu-libs.override          bsppart's Mali Bifrost job (modules for Realtek's 4.4
#                                       kernel), nothing to load here; bsppart's kdrivers is a
#                                       no-op already ("kernel has kdriver built-in"), and its
#                                       webos-tv finds webos_tv loaded (guest/kernel/modules/webos_tv)
#   etc/init/emulator-jail-nodes.conf   the nodes of the jailer's "k5lp" branch
#   etc/emulator/board.cmdline          the bootloader's parameters (Realtek U-Boot's format)
#
# W19P's board support is bsppart-dvb.pak's (targets.py, PAK_LOWERS bsppart:bsp): Realtek's
# libkadaptor (KADP, run stock over guest/kernel/modules/rtk), the libhal_*.so.1 over it (libhal_rtd2872
# and 20 split ones), libhalgal, the Mali Bifrost driver. The I2C devices are on standard Linux
# adapters (lgemu.ko's i2c_buses, targets.py), which the rootfs's libdile_i2c opens as /dev/i2c-N.

# The stock libraries the shims (guest/layers/soc/src/<name>) replace (guest/layers/soc/m2/meson.build).
# libdile_gal: W19P's rootfs libdile_gal.so.1.0.2 is the pass-through to bsppart's libhalgal
# (HAL_GAL_* over KADP's /dev/gdma_hal_gal0 and the SE engine) with W18R's 37 functions; the shim
# answers above it, so Realtek's GDMA is never reached from there. The capability is 76 bytes
# (the release's: guest/layers/webos/goldilocks2). DILE_GAL_Finalize stays the stock one's.
# libdile_crypto: W19P's DILE_CRYPTO_ReadMskToSecureStore tries libsestore (SESTORE_ReadData), then
# bsppart's libhal_crypto, which reads through the TEE's secure store; the emulator holds the model
# secret in neither, so libsecretagent's SM_SA_GetNDUID failed and nyx's DeviceInfo made a new random
# NDUID at every open. The shim answers that read, as on K7LP and M3.
# libhal_vdec_direct, libhal_audio_direct: bsppart's direct decoder and audio output (no SONAME; their
# users NEED libhal_vdec_direct.so and libhal_audio_direct.so), under the rootfs's stock
# libdile_vdec_direct and libdile_audio_direct, which NDL DirectMedia's first API (W19P's only one)
# opens. Stock, they feed the video CPU's decoder by RPC (a black picture here) and the audio
# DSP, whose open crashes; the shims send both streams to the host (src/libhal_direct).
# dile_crypto_dbgdata: W19P's tvservice keeps its debug status as 32 bytes at EEPROM 0x1C6 that
# DILE_CRYPTO_DecryptDbgData decrypts through bsppart's HAL and an OP-TEE TA (guest/kernel/modules/rtk/tee-ta.c has none for it);
# the shim's cipher is the identity and beanjar programs the clear block (src/libdile_crypto/dbg-data.c).
dile_crypto_dbgdata = true
hal_stock = {
  'libdile_gal': ['libdile_gal.so.1', 'libdile_gal.so.1.0.2', []],
  'libdile_crypto': ['libdile_crypto.so.0', 'libdile_crypto.so.0.1.1', []],
  'libhal_vdec_direct': ['libhal_vdec_direct.so', 'libhal_vdec_direct.so.1.0.0', ['libhal_vdec_direct.so.1'], 'bsppart'],
  'libhal_audio_direct': ['libhal_audio_direct.so', 'libhal_audio_direct.so.1.0.0', ['libhal_audio_direct.so.1'], 'bsppart'],
}
# Where the media elements (guest/layers/soc/src/video) go: the stock plugins' directory, and which:
# Realtek's gst-omx decoders (libgstomx.so, by gstomx.conf's sections), dvovideosink and
# rtkalsasink, against W19P's GStreamer and GLib.
gst_plugin_dir = 'usr/lib/gstreamer-1.0'
gst_elements = 'rtk-k5lp'
# The emulator's frame buffers in tvservice's physical memory (guest/layers/common/hal-common/physmem.c):
# 64 MiB from here, above the PAs rtk.ko hands out (RBUS at 0x18000000, the DCUs 0x20000000 to
# 0x60000000: targets.py). Only the libdile_vt shim allocates there, which k5lp does not build.
soc_phys_base = '0x60000000u'
# The GPU vendor libraries the gl layer replaces with empty stand-ins: none. bsppart's Mali
# Bifrost driver is one library, libmali.so.1.0 (no SONAME: its users record libmali.so; links
# libmali.so and libmali.so.1), NEEDing libgm and Wayland; libEGL.so.1.5, libGLESv2.so.2.0,
# libGLESv1_CM.so.1.1 and libwayland-egl.so.1.0 are 6 KB wrappers NEEDing it. Nothing imports a
# mali_* symbol and nothing NEEDs or names libmali.so.1, so M2's pattern applies with one change.
gl_vendor_stubs = []
# The vendor's one GL library: libmali.so as libmali.so.0.1 with its .so and .so.0 links, as on
# M2. W19P's libmali also exports wl_egl_window_*, which the compositor's client QPA, libcbe and
# libwt import from it (none of them NEEDs libwayland-egl), so it NEEDs Mesa's libwayland-egl too.
gl_vendor_gl = 'mali'
gl_vendor_gl_needs = ['EGL', 'GLESv2', 'GLESv1_CM', 'wayland-egl']

# this chip's part of the soc layer, after its base's (guest/layers/soc/meson.build)
soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W19P 05.40.97 and 04.70.20: libmali.so.1.0 prints "OpenGL ES 3.2 v1.r11p0-01rel0"
# (the DDK's level; the fitted part is not named)
gl_es_version = '3.2'
