# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The MStar M5621 "M3" (board chip MSD94BCG), W18R's SoC: the values the soc layer
# (guest/layers/soc/meson.build) and the common layer's hal_common are built with for this chip.
# Read before guest/layers/common; rootfs/ is this chip's
# part of the soc layer:
#   etc/init/gpu-libs.override, kdrivers.override
#                                       bsppart's Mali and MStar kernel-driver jobs, nothing to
#                                       load here (its webos-tv job finds webos_tv loaded:
#                                       guest/kernel/modules/webos_tv)
#   etc/init/emulator-jail-nodes.conf   the nodes of the jailer's "m3" branch
#   etc/emulator/board.cmdline          the bootloader's parameters (M3 MBoot's format)
#
# As on M2, the board support is bsppart-dvb.pak's (targets.py, PAK_LOWERS bsppart:bsp): MStar's
# libhal_msd94bcg.so, libkadaptor (KADP over guest/kernel/modules/mstar-utopia), libhalgal, the Mali-470
# driver. The I2C devices are on standard Linux adapters (lgemu.ko's i2c_buses, targets.py), which
# the rootfs's libdile_i2c opens as /dev/i2c-N.

# The stock libraries the shims (guest/layers/soc/src/<name>) replace (guest/layers/soc/m2/meson.build).
# libdile_gal: W18R's rootfs libdile_gal.so.1.0.2 is the pass-through to bsppart's libhalgal as on
# M2, with two more functions (Get/SetFBHWControllerSharpness, which libgm imports) and a 76-byte
# capability (the release's: guest/layers/webos/goldilocks).
# libhal_avd: the analog decoder's no-signal answers over bsppart's board HAL (its SONAME libhal_msd94bcg.so,
# which tvservice NEEDs; its other names in usr/lib made links to the shim).
# libdile_crypto: W18R's DILE_CRYPTO_ReadMskToSecureStore tries libsestore (SESTORE_ReadData), then
# bsppart's HAL, which reads through MStar's TEE; the emulator has neither, so SM_SA_GetNDUID failed
# and nyx made a random NDUID at every open. sdx, securitymanager and libsecretagent all
# read the model secret through DILE, so the shim answers there, as on K7LP.
# libhal_vdec_direct, libhal_audio_direct: bsppart's direct decoder and audio output (SONAMEs
# libhal_vdec_direct.so and libhal_audio_direct.so) under the rootfs's stock libdile_vdec_direct and
# libdile_audio_direct, which NDL DirectMedia's first API (W18R's only one) opens; the shims send
# both streams to the host, as on K5LP (src/libhal_direct).
# libkadaptor: MStar's KADP (bsppart's libkadaptor.so.0.1.0, no SONAME: its users NEED libkadaptor.so).
# The board HAL reads the DVR's key through it (MAdp_Secure_GetSecureDataEMMC("/tmp/96B14BF8")),
# which MStar's TEE decrypts on the TV; the shim answers it from the VM's store, as on M2
# (src/libkadaptor/kadaptor-pvrkey.c).
# dile_crypto_dbgdata: W18R's tvservice keeps its debug status as 32 bytes at EEPROM 0x1C6 that
# DILE_CRYPTO_DecryptDbgData decrypts through bsppart's HAL and MStar's TEE (the mailbox's Customer AES command, which guest/kernel/modules/mstar-utopia/mailbox.c does not implement);
# the shim's cipher is the identity and beanjar programs the clear block (src/libdile_crypto/dbg-data.c).
dile_crypto_dbgdata = true
hal_stock = {
  'libdile_gal': ['libdile_gal.so.1', 'libdile_gal.so.1.0.2', []],
  'libdile_crypto': ['libdile_crypto.so.0', 'libdile_crypto.so.0.1.1', []],
  'libhal_avd': ['libhal_msd94bcg.so', 'libhal_msd94bcg.so.0.1.0', ['libhal_msd94bcg.so.0', 'libhal_m2.so', 'libhal_m2.so.0', 'libhal_msd93xbg.so'], 'bsppart'],
  'libkadaptor': ['libkadaptor.so', 'libkadaptor.so.0.1.0', [], 'bsppart'],
  'libhal_vdec_direct': ['libhal_vdec_direct.so', 'libhal_vdec_direct.so.0.1.0', ['libhal_vdec_direct.so.0'], 'bsppart'],
  'libhal_audio_direct': ['libhal_audio_direct.so', 'libhal_audio_direct.so.0.1.0', ['libhal_audio_direct.so.0'], 'bsppart'],
}
kadaptor_source = ['kadaptor-pvrkey.c']
# Where the media elements (guest/layers/soc/src/video) go: the stock plugins' directory, and which:
# W18R's GStreamer is 1.12.0 (GLib 2.48), and its media elements are MStar's: the gst-omx decoders
# and omxaudiosink of libgstomx.so (by gstomx.conf's sections), mvsink and masink.
gst_plugin_dir = 'usr/lib/gstreamer-1.0'
gst_elements = 'mstar-m3'
# The emulator's frame buffers in tvservice's physical memory (guest/layers/common/hal-common/physmem.c):
# 64 MiB from here. M3's layout header (bsppart's tee/mmap.ini) is M2's: MIU0 at 0x20000000 on the
# bus, MIU1 at 0xA0000000; the first address above MIU0's 512 MiB, as on M2.
soc_phys_base = '0x40000000u'
# The GPU vendor libraries the gl layer replaces with empty stand-ins: none. bsppart's Mali-470
# r6p1 driver is one library, libmali.so.0.1 (no SONAME), NEEDing libUMP, libgm and Wayland;
# libEGL.so.1.4, libGLESv2.so.2.0, libGLESv1_CM.so.1.1 and libwayland-egl.so.0.1 are 3 KB wrappers
# NEEDing it. Only libmali NEEDs libUMP, so M2's pattern applies unchanged.
gl_vendor_stubs = []
# The vendor's one GL library: libmali.so, NEEDing Mesa's libEGL, libGLESv2 and libGLESv1_CM, as libmali.so.0.1
# with its .so and .so.0 links (bsppart's names), as on M2.
gl_vendor_gl = 'mali'
gl_vendor_gl_needs = ['EGL', 'GLESv2', 'GLESv1_CM']

# this chip's part of the soc layer, after its base's (guest/layers/soc/meson.build)
soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W18R 05.50.70: bsppart's libmali.so.0.1 prints "OpenGL ES 2.0", "OpenGL ES GLSL ES 1.00"
# (Mali Utgard)
gl_es_version = '2.0'
