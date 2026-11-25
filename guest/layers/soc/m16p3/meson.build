# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The LG M16P3 (LG1314, "M19" to its kernel; the firmware's machine name m16p3), W19H's SoC: M16PP's
# soc layer (base: guest/layers/soc/m16pp is read first) with W19H's own board. W19H 05.50.00's
# bsppart has M16P's hal_stock libraries with the same names and SONAMEs, and its jobs gpu-libs,
# kdrivers-galcore (now chip/m19/cpc/galcore.ko), kdrivers-hdmi20, kdrivers-vdec_cmn,
# kdrivers-venc_hxenc and the rootfs's firmware_vdec are M16P's (paths under its own 4.4.84 kernel),
# so M16P's no-op overrides hold. rootfs/ holds only the board (etc/emulator/board.cmdline).

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# W19H's GStreamer is 1.14.1 over GLib 2.48, and its LG elements are other plugin files without the
# -1.0 suffix: libgstlxvideodec.so and libgstlxvideosink.so (omx_lxvideodec, omx_lxvideosink as
# before), libgstlxaudiodec.so and libgstlxaudiosink.so (lxaudiodec with audio_dec and per-codec
# *_audiodec elements, lxaudiosink with audio_sink), in place of W18H's libgstaudiodec-1.0.so and
# libgstaudiosink-1.0.so. The media elements are not built for it yet: the stock plugins stay, and
# without the media elements the direct-media HAL shims are not built either
# (guest/layers/soc/meson.build).
gst_plugin_dir = ''
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa gives a
# context: W19H 05.50.00's libmali.so.0.1 (another build than W18H's) prints "OpenGL ES 3.2
# v1.r12p0-04rel0"
gl_es_version = '3.2'
