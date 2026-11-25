# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The Realtek RTD2999L "K2L" (U-Boot's chip=K2L), W16K's SoC: K2LP's sibling, whose soc layer
# (base: guest/layers/soc/k2lp, itself on k3lp) it shares: the same stock libraries, KADP layout and
# PowerVR Rogue G6110. rootfs/ holds W16K's board (etc/emulator/board.cmdline) and its gpu-libs job
# (Realtek's 3.10.19 kernel's modules).

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# W16K 05.70.55: the PowerVR Rogue G6110's "OpenGL ES 3.1 build 1.6@3626709", as W16P's
gl_es_version = '3.1'
