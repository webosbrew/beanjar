# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The LG O20N (the kdriver family o20, with E60N beside it), W21O's SoC, on webOS 6 (kisscurl):
# LG's kdrivers again (lg115x.ko, guest/targets/soc/o20n.toml), under a full KADP as M16P's, with the
# DILE libraries and systemd jobs of the O22 generation. rootfs/ is this chip's part of the soc layer:
#   etc/emulator/board-atsc.cmdline     the bootloader's parameters, the US board
#   etc/systemd/system/*.service.d      the stock jobs that load the vendor's kernel modules
#                                       (Mali, Vivante, the HDMI 2.1 and codec drivers), nothing to
#                                       load here (lg115x.ko stands in for the built-in kdrivers)
#
# The board support is bsppart's: libhal_lg115x, libkadaptor (KADP over lg115x.ko's /proc/lg and
# /dev/lg nodes), libhalgal and the Mali driver; the rootfs has the O22 generation's DILE libraries
# (libdile_gal.so.1.0.2, libdile_crypto.so.0.1.1, libdile_vt, libaibsp, libaibspLib).
hal_stock = {
  'libdile_gal': ['libdile_gal.so.1', 'libdile_gal.so.1.0.2', []],
  'libdile_crypto': ['libdile_crypto.so.0', 'libdile_crypto.so.0.1.1', []],
  'libaibsp': ['libaibsp.so.1', 'libaibsp.so.1.0.0', []],
  'libaibspLib': ['libaibspLib.so.1', 'libaibspLib.so.1.0.0', []],
}
halgal_stock_fails = true
hal_stock += {
  'libhalgal': ['libhalgal.so.2', 'libhalgal.so.2.0.1',
               ['libhalgal.so', 'libhalgal.so.0'], 'bsppart'],
}
gst_plugin_dir = ''
soc_phys_base = '0xA0000000u' # emulator scratch arena, not a retail memory layout
gl_vendor_stubs = []
gl_vendor_gl = 'mali'
gl_vendor_gl_needs = ['EGL', 'GLESv2', 'GLESv1_CM']
soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W21O 03.70.05's bsppart libmali.so.0.1 prints "OpenGL ES 3.2 v1.r9p0-01rel0"
gl_es_version = '3.2'
