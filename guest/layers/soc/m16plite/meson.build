# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The LG M16P lite (lxboot's chip=M16P, the firmware's machine name m16plite), W17M's SoC: M16P's soc
# layer (base: guest/layers/soc/m16p is read first) with W17M's own board. W17M 06.10.55's bsppart
# is M16P's with its HAL libraries rebuilt (libhal_lg115x imports the same KADP functions as W17H's,
# each resolved in W17M's own libkadaptor), and the same Mali r12p0 libmali.so.0.1, kbase and kernel
# modules (docs/research/m16-kid24u-m23-groundwork.md); so M16P's shims, media elements and gl values
# hold. rootfs/ holds only the board (etc/emulator/board.cmdline).

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W17M 06.10.55's libmali.so.0.1 is W17H's file (the same md5) and prints "OpenGL
# ES 3.2 v1.r12p0-04rel0"
gl_es_version = '3.2'
