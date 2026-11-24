# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The Realtek "KF23F" (U-Boot's chip=KF23FA0), W23L's SoC. Its base is K8LPN (base: guest/layers/soc/
# k8lpn is read first): W23P's HAL and media boundary. rootfs/ is this chip's part of the soc layer:
#   etc/emulator/board-atsc.cmdline   the bootloader's parameters, a US W23L model's
#   etc/emulator/board-dvb.cmdline    the same hardware's DVB board
#
# The gl layer: unlike K8LPN's PowerVR, W23L's bsppart has a Mali Bifrost r17p0 (libmali.so.0.17.0,
# SONAME libmali.so.0, gpu-libs.service loading mali_kbase, mali_fb and dma-buf-test-exporter), the
# DDK of K7LP's W21P; so K7LP's Mali stand-in, not K8LP's standard EGL ABI. K7LP's gpu-libs.service
# override (no Mali modules in a VM) reaches this chip through the bases.
gl_vendor_stubs = []
gl_vendor_gl = 'mali'
gl_vendor_gl_soname = 'libmali.so.0'
gl_standard_abi = false

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W23L 13.50.75: libmali.so.0.17.0 prints "OpenGL ES 3.2 v1.r17p0-01rel0" (Bifrost; the
# fitted part is not named)
gl_es_version = '3.2'
