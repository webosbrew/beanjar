# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The MStar M2R (board chip MSD93XBG; MBoot's chip=M2RA0), W17R's SoC: M2's soc layer (base:
# guest/layers/soc/m2 is read first) with W17R's own board and libraries. Its rootfs/ holds only
# the board (etc/emulator/board.cmdline); the kernel-driver and Mali jobs are M2's overrides
# (bsppart's kdrivers.conf loads kdrv_msd93xbg.ko and the MStar ALSA modules, gpu-libs.conf the
# Mali r6p1 ones); its wifidrv-mtk and bluetoothdrv-mtk jobs load nothing without a Wi-Fi combo's
# builtinWifiPID.

# The board HAL is libhal_msd93xbg (bsppart; SONAME libhal_msd93xbg.so, which tvservice NEEDs, with
# M2's names libhal_msd93f2g.so and libhal_m2.so linked to it). W17R's bsppart also has DILE's direct
# HALs as W18R's (libhal_vdec_direct and libhal_audio_direct, the same HAL_* exports but
# _HAL_VDEC_DIRECT_GetStreamId): NDL DirectMedia runs over them, so the shims send both streams to
# the host as on M3. The other shims are M2's (libdile_gal.so.1.0.2, bsppart's libkadaptor.so.0.1.0
# and libhal_crypto.so.0.1.0, whose HAL_CRYPTO_ReadMskToSecureStore is still exported).
hal_stock += {
  'libhal_avd': ['libhal_msd93xbg.so', 'libhal_msd93xbg.so.0.1.0', ['libhal_msd93xbg.so.0', 'libhal_m2.so', 'libhal_m2.so.0', 'libhal_msd93f2g.so'], 'bsppart'],
  'libhal_vdec_direct': ['libhal_vdec_direct.so', 'libhal_vdec_direct.so.0.1.0', ['libhal_vdec_direct.so.0'], 'bsppart'],
  'libhal_audio_direct': ['libhal_audio_direct.so', 'libhal_audio_direct.so.0.1.0', ['libhal_audio_direct.so.0'], 'bsppart'],
}
# W17R's GStreamer is 1.8.0 (GLib 2.40), with MStar's media elements
gst_elements = 'mstar-m2r'

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W17R 06.10.60's bsppart Mali (Utgard, r6p1), "OpenGL ES 2.0" as M2's
gl_es_version = '2.0'
