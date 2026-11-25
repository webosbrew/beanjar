# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The Realtek RTD2871F "K3LP" (U-Boot's chip=K3LPA0), W17P's SoC: the values the soc layer
# (guest/layers/soc/meson.build) and the common layer's hal_common are built with for this chip.
# Read before guest/layers/common; rootfs/ is this chip's part of the soc layer:
#   etc/init/gpu-libs.override          bsppart's PowerVR job (pvrsrvkm.ko and drm_nulldisp.ko
#                                       for Realtek's 4.4.3 kernel, then pvrsrvctl), nothing to
#                                       load here; bsppart's kdrivers is a no-op already ("kernel
#                                       has kdriver built-in")
#   etc/init/emulator-jail-nodes.conf   the nodes of the jailer's "k3lp" branch
#   etc/emulator/board.cmdline          the bootloader's parameters (Realtek U-Boot's format)
#
# W17P's board support is bsppart.pak's (targets.py, PAK_LOWERS bsppart:bsp), as K5LP's: Realtek's
# libkadaptor (KADP, run stock over guest/kernel/modules/rtk), the libhal_*.so.1 over it
# (libhal_rtd2871f and 30 split ones), libhalgal, the PowerVR Rogue GE7800 DDK. The I2C devices
# are on standard Linux adapters (lgemu.ko's i2c_buses, targets.py), which KADP's RHAL_I2C opens
# as /dev/i2c-N.

# The stock libraries the shims (guest/layers/soc/src/<name>) replace (guest/layers/soc/m2/meson.build).
# libdile_gal: W17P's rootfs libdile_gal.so.1.0.2 is the pass-through to bsppart's libhalgal, as
# K5LP's; the shim answers above it, so Realtek's GDMA is never reached from there. The capability
# is the release's (guest/layers/webos/dreadlocks2).
# libdile_crypto: DILE_CRYPTO_ReadMskToSecureStore reads the model secret through bsppart's
# libhal_crypto and the KCPU's secure store, which the emulator does not have; the shim answers
# that read, as on K5LP.
hal_stock = {
  'libdile_gal': ['libdile_gal.so.1', 'libdile_gal.so.1.0.2', []],
  'libdile_crypto': ['libdile_crypto.so.0', 'libdile_crypto.so.0.1.1', []],
}
# No media elements yet: W17P's GStreamer (dreadlocks2's) has no Realtek element set here.
gst_plugin_dir = ''
gst_elements = ''
# The emulator's frame buffers in tvservice's physical memory (guest/layers/common/hal-common/physmem.c):
# 64 MiB from here, above the PAs rtk.ko hands out (RBUS at 0x18000000, the DCUs 0x20000000 to
# 0x60000000: targets.py), as K5LP's. Only the libdile_vt shim allocates there, which k3lp does
# not build.
soc_phys_base = '0x60000000u'
# PowerVR: the users of bsppart's DDK link the standard EGL/GLES ABI (libEGL.so.1, libGLESv2.so.2,
# libGLESv1_CM.so.1, libgbm.so.1, libwayland-egl), so Mesa supplies those libraries directly, as on
# K8LP; no vendor stand-in.
gl_vendor_stubs = []
gl_vendor_gl = ''
gl_vendor_gl_soname = ''
gl_standard_abi = true

# this chip's part of the soc layer, after its base's (guest/layers/soc/meson.build)
soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W17P 06.10.75: libGLESv2.so.2.0 prints "OpenGL ES 3.2 build 1.6@3956470"
# (PowerVR Rogue GE7800)
gl_es_version = '3.2'
