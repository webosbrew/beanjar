# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The MStar LM18A (board chip MSD96ALVX; MBoot's chip=LM18AA0), W18A's SoC: M3's soc layer (base:
# guest/layers/soc/m3 is read first) with W18A's own board, board HAL and GPU. W18A 05.50.70 is the
# same release as W18R (goldilocks-gorongosa 4.4.3, GStreamer 1.12.0, GLib 2.48, the same rootfs
# libdile_gal.so.1.0.2 and libdile_crypto.so.0.1.1, bsppart's libkadaptor, libhal_vdec_direct and
# libhal_audio_direct), so M3's shims and media elements apply. rootfs/ holds W18A's own:
#   etc/emulator/board.cmdline          the bootloader's parameters (W18A MBoot's format)
#   etc/init/gpu-libs.override, kdrivers.override
#                                       W18A's Mali-G51 and kdriver jobs (which start tee-supplicant)
#   etc/init/emulator-jail-nodes.conf   the nodes of the jailer's "lm18a" branch (OP-TEE's too)

# The board HAL is libhal_msd96alvx (bsppart), which tvservice NEEDs by its SONAME; bsppart links
# libhal_m2.so, .so.0 and libhal_msd93xbg.so to it, as W18R's to libhal_msd94bcg.
hal_stock += {
  'libhal_avd': ['libhal_msd96alvx.so', 'libhal_msd96alvx.so.0.1.0', ['libhal_msd96alvx.so.0', 'libhal_m2.so', 'libhal_m2.so.0', 'libhal_msd93xbg.so'], 'bsppart'],
}

# The GPU is a Mali-G51 (Bifrost r6p0): bsppart's libmali.so.0.1 (SONAME libmali.so.0, NEEDing
# libgm and Wayland; it defines the ump_* functions itself) prints "OpenGL ES 3.2 v1.r6p0-01rel0"
# and "OpenGL ES GLSL ES 3.20". libEGL.so.1.4, libGLESv2.so.2.0, libGLESv1_CM.so.1.1,
# libwayland-egl.so.0.1 and libUMP.so.0.1 are wrappers NEEDing libmali.so.0, and nothing NEEDs
# libUMP. The gl layer's libmali stands in under that SONAME, as on LM21A.
gl_vendor_gl_soname = 'libmali.so.0'
gl_es_version = '3.2'

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
