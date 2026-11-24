# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The LG M16PP lite (the firmware's machine name m16pplite; lxboot's chip=M16P), W18M's SoC: M16PP's
# soc layer (base: guest/layers/soc/m16pp is read first) with W18M's own board. W18M 05.50.60 has
# W18H's kernel version (4.4.84-150.glacier.2), the same libmali file and KADP values
# (docs/research/m16-kid24u-m23-groundwork.md); its broadcast line is ARIB only (the -arib paks).
# rootfs/ holds only the board (etc/emulator/board.cmdline).

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa gives a
# context: W18M's libmali is W18H's file, "OpenGL ES 3.2 v1.r12p0-04rel0"
gl_es_version = '3.2'
