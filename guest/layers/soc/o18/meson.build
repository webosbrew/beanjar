# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The LG O18 (LG1211; the firmware's machine name o18), W19O's SoC: M16P3's soc layer (base:
# guest/layers/soc/m16p3 is read first) with W19O's own board. W19O 05.50.00's bsppart has the same
# hal_stock libraries and jobs as W19H's (gpu-libs loads the G71's kds, ump and mali_kbase;
# kdrivers-galcore chip/o18/cpc/galcore.ko), so M16P's no-op overrides hold; its GStreamer and LG
# media plugin files are W19H's (no media elements yet). rootfs/ holds only the board
# (etc/emulator/board.cmdline).

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa gives a
# context: W19O 05.50.00's libmali.so.0.1 (Bifrost, Mali-G71) prints "OpenGL ES 3.2 v1.r4p0-01rel0"
gl_es_version = '3.2'
