# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The Realtek "K7LP" (RTD2875, U-Boot's chip=K7LPA0, "LGE K7 Board"), W21P's SoC. Its base is K6LP
# (base: guest/layers/soc/k6lp is read first): the DILE_GAL shim (W21P's rootfs libdile_gal.so.1.0.2
# exports W20L's 29 functions and NEEDs bsppart's libhalgal.so), the media elements' place, the frame
# buffers' place and the gl layer's Mali. rootfs/ is this chip's part of the soc layer, after
# K6LP's:
#   etc/systemd/system/gpu-libs.service.d/emulator-gpu.conf  bsppart's Mali modules (W21P's
#                                set) not loaded; the unit succeeds
#   etc/emulator/board.cmdline   the bootloader's parameters, W21P's own (decision C)
#
# The gl layer: bsppart's Mali Bifrost (libmali.so.0.17.0, r17p0) has a SONAME, libmali.so.0,
# which the compositor's QPA, libQt5Gui, libgm-wayland and the GL users NEED (K6LP's had none);
# it exports wl_egl_window_* as K6LP's does.
gl_vendor_gl_soname = 'libmali.so.0'
# W21P's DILE model-key read is libsestore's only, by the board's region (libdile_tvregion: "_a",
# "_d", "_r", then the plain item), after a DILE_CRYPTO_Initialize that fails in a VM: K6LP's
# libdile_crypto shim takes that path (src/libdile_crypto); the stock file handles all other
# DILE_CRYPTO calls.
# The media elements: K6LP's, against W21P's GStreamer 1.16.2 (the decoders' third src memory,
# DMABuf, and dvovideosink's NV12-only template among the differences).
gst_elements = 'rtk-k7lp'

# this chip's part of the soc layer, after its base's (guest/layers/soc/meson.build)
soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W21P 03.70.25: libmali.so.0.17.0 prints "OpenGL ES 3.2 v1.r17p0-01rel0" (the DDK's
# level; the fitted part is not named)
gl_es_version = '3.2'
