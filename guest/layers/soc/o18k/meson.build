# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The LG O18K (the firmware's machine name o18k; LG1211, O18 to its kernel), W19K's SoC: O18's 8K
# sibling, O18's soc layer (base: guest/layers/soc/o18 is read first) with W19K's own board. W19K
# 05.50.00 adds a Realtek codec host reached over USB (rtk-codec-host loads rtk_codec.ko,
# rtk_codec_install.ko and usb_dtcp.ko; externaldecodercontroller) to O18's jobs and libraries;
# neither is modelled: the module loads fail and the controller finds no dongle. rootfs/ holds only
# the board (etc/emulator/board.cmdline).

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# W19K's libmali is O18's Bifrost r4p0 ("OpenGL ES 3.2 v1.r4p0-01rel0")
gl_es_version = '3.2'
