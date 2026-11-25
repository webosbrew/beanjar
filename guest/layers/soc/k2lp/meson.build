# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The Realtek RTD2999L "K2LP" (U-Boot's chip=K2LP), W16P's SoC: the generation before K3LP, whose
# soc layer (base: guest/layers/soc/k3lp is read first) it shares: the same stock libdile_gal and
# libdile_crypto, Realtek's KADP in bsppart, and a PowerVR DDK 1.6 whose users link the standard
# EGL/GLES ABI. rootfs/ holds W16P's board (etc/emulator/board.cmdline) and its gpu-libs job
# (Realtek's 3.10.19 kernel's modules).

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# W16P 05.70.50: libGLESv2.so.2.0 prints "OpenGL ES 3.1 build 1.6@3626709" (PowerVR Rogue G6110)
gl_es_version = '3.1'
