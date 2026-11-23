# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The MStar Monaco (LGE5331, LG's LM15U platform), W15U's SoC: the values the soc layer
# (guest/layers/soc/meson.build) and the common layer's hal_common are built with for this chip.
# rootfs/ is this chip's part of the soc layer:
#   etc/init/kdrivers.override, gpu-libs.override
#                                       the kdriver (kdrv_lm15u.ko, MStar ALSA) and Mali Midgard
#                                       jobs, nothing to load here
#   etc/emulator/board.cmdline          the bootloader's parameters (W15U's MBoot format)
#
# W15U is W15M's beehive userland over a W16R-like MStar vendor side, all of it in the rootfs (no
# bsppart): MStar's Utopia2K user side linked into libkadaptor.so.1, which runs stock on the
# emulator's MStar drivers (guest/kernel/modules/mstar-utopia, Monaco's parameters in
# guest/targets/soc/lm15u.toml), LG's beehive HAL names with .so.1 sonames (libhal_lm15u.so.1,
# libhal_crypto.so.1, libhalgal.so.1), and the Mali Midgard driver (libmali.so.0.1).

# The stock libraries the shims (guest/layers/soc/src/<name>) replace: name -> [the soname the
# shim takes, the stock file it NEEDs, [other names in usr/lib made links to the shim]].
# libdile_gal, the graphics stand-in: gm, libgm, libim and tvservice NEED libdile_gal.so.1
# (1.0.2, W15M's exports: the study compared them), the pass-through to libhalgal.so.1 over KADP's
# MApi_GFX/GOP; the shim answers above it, so MStar's 2D engine and display planes are never
# reached from there.
# libkadaptor: MStar's KADP (the rootfs's libkadaptor.so.1.0.0, SONAME libkadaptor.so.1; LG's HAL
# libraries NEED that name, libmsil_lm15u and others libkadaptor.so, linked to the shim), whose XC
# window calls run on a user-mode Utopia instance as on W16R (MApi_XC_Init 0x389980 opens both
# kinds; every window call takes the user-mode one): the shim passes them to a kernel-mode one too,
# for mstar-utopia.ko's window decoder (src/libkadaptor/kadaptor-xc.c). Without it no video
# window reaches the viewer (no SetWindow came to the kernel while a video played).
hal_stock = {
  'libdile_gal': ['libdile_gal.so.1', 'libdile_gal.so.1.0.2', []],
  'libkadaptor': ['libkadaptor.so.1', 'libkadaptor.so.1.0.0', ['libkadaptor.so']],
}
kadaptor_source = ['kadaptor-xc.c']
# Where the media elements (guest/layers/soc/src/video) go: the stock plugins' directory, and which:
# W15U's GStreamer is 1.2.4 over GLib 2.34.3, and its media elements are MStar's: the gst-omx
# decoders and omxaudiosink of libgstomx.so (by gstomx.conf's sections) and mvsink, W16R's set.
gst_plugin_dir = 'usr/lib/gstreamer-1.0'
gst_elements = 'mstar-lm15u'
# NDL's Esplayer (libndl-directmedia2.so.1, webOS 2's NDL) drives MStar's OpenMAX IL core directly
# (RPATH /usr/lib/openmax, no tunnels: decoders and renderers, the client keeping time): the core
# stand-in replaces this file (src/omx_core).
omx_core = 'usr/lib/openmax/libOMX.MStar.so.1.0.0'
# The emulator's frame buffers in tvservice's physical memory (guest/layers/common/hal-common/physmem.c):
# 64 MiB from here. Monaco's DRAM is two 1 GiB MIUs (tee/mmap.ini: MIU_DRAM_LEN0 and _LEN1,
# MIU_INTERVAL 0x80000000); MIU0 is at 0x20000000 on the bus, so the first address above it is
# 0x60000000, below MIU1's.
soc_phys_base = '0x60000000u'
# The GPU vendor libraries the gl layer replaces: W15U's Mali Midgard driver is one library,
# libmali.so.0.1 (EGL, GLES 1, 2 and 3, the Wayland EGL platform), with libEGL.so.1.4,
# libGLESv1_CM.so.1.1, libGLESv2.so.2.0 and libwayland-egl.so.0.1 as 3 KB wrappers NEEDing it, as
# on M2; no UMP library in usr/lib.
gl_vendor_stubs = []
gl_vendor_gl = 'mali'
gl_vendor_gl_needs = ['EGL', 'GLESv2', 'GLESv1_CM']
# Mali's window buffers (guest/layers/gl/src/egl-window-formats.c): W15U's libmali.so.0.1 sends a
# window's buffers to the compositor over MStar's wl_gpu (create_buffer, "nuiiiu"), whose format
# codes are RGBA8888, RGBA4444, RGBA5551 and RGB565 only: no XRGB, so a window is never opaque by
# its format, though its 8-8-8-0 configs are window-capable (config_compatible_with_format takes
# RGBA8888 for both). SDL 2.0.2 asks for no alpha; Mesa would give it an XRGB8888 window, opaque
# over the video. Keeping 8-8-8-0 configs off windows gives such a client RGBA8888, as on the set:
# NDL's sample clears its window to transparent and the video shows under it.
gl_vendor_window_rgba = [[8, 8, 8, 8], [5, 6, 5, 0], [5, 5, 5, 1], [4, 4, 4, 4]]

# this chip's part of the soc layer, after its base's (guest/layers/soc/meson.build)
soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W15U 04.06.25: libmali.so.0.1 (Midgard r4p1-00rel0) is an OpenGL ES 3.0 driver
gl_es_version = '3.0'
