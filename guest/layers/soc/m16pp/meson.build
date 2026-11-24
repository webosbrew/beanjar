# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The LG M16PP (lxboot's chip=M16P, the firmware's machine name m16pp; LG1313's C0 revision), W18H's
# SoC: M16P's soc layer (base: guest/layers/soc/m16p is read first) with W18H's own media elements
# and board. W18H 05.50.70's bsppart has the five hal_stock libraries of M16P with the same names
# and SONAMEs, and its jobs gpu-libs, kdrivers-galcore, kdrivers-hdmi20, kdrivers-vdec_cmn,
# kdrivers-venc_hxenc and the rootfs's firmware_vdec are M16P's (paths under its own 4.4.84 kernel),
# so M16P's no-op overrides hold. rootfs/ holds only the board (etc/emulator/board.cmdline).

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# W18H's GStreamer is 1.12.0 over GLib 2.48 (W17H: 1.8.0, 2.40), with the same LG element names
# (omx_lxvideodec, omx_lxvideosink, audio_dec, audio_sink): M16P's set built over W18H's headers
gst_elements = 'lx-m16pp'
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W18H 05.50.70's libmali.so.0.1 (another build than W17H's) prints "OpenGL ES
# 3.2 v1.r12p0-04rel0"
gl_es_version = '3.2'
