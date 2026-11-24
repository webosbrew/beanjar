# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The MStar M3R (board chip MSD94BEXM8), W19R's SoC: M3's soc layer (base: guest/layers/soc/m3 is
# read first) with W19R's own board and libraries. Its rootfs/ holds only the board
# (etc/emulator/board.cmdline); the jobs and jail nodes are M3's.

# The board HAL is libhal_msd94bexm8 (bsppart), which tvservice NEEDs by its SONAME; the other
# shims' stock files are M3's names (W19R's libdile_gal.so.1.0.2, libdile_crypto.so.0.1.1,
# bsppart's libkadaptor, libhal_vdec_direct and libhal_audio_direct).
hal_stock += {
  'libhal_avd': ['libhal_msd94bexm8.so', 'libhal_msd94bexm8.so.0.1.0', ['libhal_msd94bexm8.so.0', 'libhal_m2.so', 'libhal_m2.so.0'], 'bsppart'],
}
# W19R's GStreamer is 1.14.1 (GLib 2.48), with M3's MStar media elements
gst_elements = 'mstar-m3r'

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W19R 05.40.97's bsppart Mali-470 (Utgard), "OpenGL ES 2.0" as M3's
gl_es_version = '2.0'
