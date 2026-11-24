# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The LG M23 (LG1315, lxboot's chip=M23), W23M's SoC: O22's soc layer (base: guest/layers/soc/o22 is
# read first). W23M's bsppart per release is O22's of the same release in what the layer relies on:
# libhal_lg115x imports the same KADP functions (each resolved in W23M's own libkadaptor), libhalgal
# exports the same 44 HAL_GAL_* (queue's is O22 queue's file), libmali is O22's file per release
# (0.28.0 on number1, 0.46.0 from ombre on), and the stock units the o22 layer overrides exist under
# the same names (gpu-libs, kdrivers-vdec_cmn; kdrivers-venc_hxenc on number1 only). W23M queue
# 43.22.75 has no legacy DILE library at all (no libdile_gal, no libdile_crypto), as O22N's and
# O24's queues: O22's halgal_stock_fails path takes it (docs/research/m16-kid24u-m23-groundwork.md).
# rootfs/ holds the boards:
#   etc/emulator/board-atsc.cmdline   a US W23M model's bootloader parameters (the default)
#   etc/emulator/board-dvb.cmdline    the same hardware's DVB board

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa gives a
# context: W23M's libmali is O22's file per release, "OpenGL ES 3.2" (o22's table)
gl_es_version = '3.2'
