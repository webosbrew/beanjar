# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Initial O22 profile: standard EGL through the stock Mali soname.
# The stock cursor initializer waits for actual GAL surfaces before input reads.
# Its 1056-byte surfaces and 76-byte capability match the software GAL backend.
# Keep all other stock HAL/media code until runtime establishes its contracts.
# libdile_crypto: O22's DILE_CRYPTO_ReadMskToSecureStore returns -1 when DILE_CRYPTO_Initialize's
# SESTORE_Init fails, as it does in a VM, so SM_SA_GetNDUID failed and nyx made a random NDUID at
# every open. The shim reads as stock does after that, the item of the board's region
# (libdile_tvregion), then the plain one, as on K7LP (O22N and O22N2 inherit it).
# libaibsp, libaibspLib: LG's AI picture libraries drive an NPU and display registers the VM lacks.
# nn-proxy's AIBSP_Initialize failed at /dev/video241 and its teardown crashed; pqcontroller's
# workers retried /dev/mem forever and W24G's AIBSP_PQ_Enable read a failed mmap. Every imported
# entry succeeds as a no-op. Spacebuns has both too.
# The GBM formats the Mali driver makes window configs of (gl_vendor_gbm_formats; the gl layer's
# Mesa lists their configs first): W24G 33.31.68's bsppart libmali.so.0.46.0, gbm_get_configs, makes
# them of XBGR8888 and ABGR8888 only, so LG's Qt EGLFS Starfish plugin, which takes the first
# alpha-8 config and creates its surface in the output's abgr8888, gets a matching one. The other
# O22-family chips' Mali drivers are taken to be the same (not read).
gl_vendor_gbm_formats = ['XB24', 'AB24']
hal_stock = {
  'libdile_gal': ['libdile_gal.so.1', 'libdile_gal.so.1.0.2', []],
  'libdile_crypto': ['libdile_crypto.so.0', 'libdile_crypto.so.0.1.1', []],
  'libaibsp': ['libaibsp.so.1', 'libaibsp.so.1.0.0', []],
  'libaibspLib': ['libaibspLib.so.1', 'libaibspLib.so.1.0.0', []],
}
# libhalgal: captureservice's GRAPHIC capture (and queue's NYX GAL, through the unversioned name)
# calls HAL_GAL_* in bsppart's libhalgal.so.2, whose allocation fails on the emulator, so
# executeOneShot answered ERROR_INTERNAL. W22O's .2.0.1 of number1, ponytail and queue and W24G
# ombre's export the same 44 HAL_GAL_* (mullet's lacks CaptureFrameBufferToFile); the callers use
# u16 dimensions and the 1056-byte surface, which the software engine serves: libdile_gal's shim's,
# or, in a firmware without a legacy DILE_GAL library (W23O to W25O queue, W24G spacebuns), the
# shim's own copy (halgal_stock_fails, guest/layers/soc/meson.build), since the stock HAL's
# HAL_GAL_CreateSurface fails there as well. The stock file stays the dependency for the other
# exports.
halgal_stock_fails = true
hal_stock += {
  'libhalgal': ['libhalgal.so.2', 'libhalgal.so.2.0.1',
               ['libhalgal.so', 'libhalgal.so.0'], 'bsppart'],
}
# The media elements under the stock modern LX names: the stock ones need /dev/vo and an AIMC
# audio device, so the sink failed to start and the AAC decoder crashed on a NULL AIMC handle.
gst_plugin_dir = 'usr/lib/gstreamer-1.0'
# From ombre on, the stock elements follow the release, not the chip (their build ids): every
# O22-family ombre has W24G ombre's (W22O's and W23O's are builds of the same source), every ponytail
# W22O ponytail's video elements and every queue W22O queue's (gst-inspect of those); a later
# release takes queue's (webos_reached: the release order, docs/dev/architecture/build-and-boot.md).
# Before ombre each chip has its own (o22n).
gst_elements = 'lx-o22'
if 'queue' in webos_reached
  gst_elements = 'lx-o22-queue'
elif 'ponytail' in webos_reached
  gst_elements = 'lx-o22-ponytail'
elif 'ombre' in webos_reached
  gst_elements = 'lx-o22n2'
endif
# W22O's ponytail and queue audio builds dropped DTS; W23O's, W24G's and W24O's of those releases
# keep it (dts-seamless and dts_audiodec, gst-inspect of W24G 33.31 and 43.21 and W23O 44.00). O22N3's
# and O24N's ponytail audio builds are W22O's; their Japanese queue builds (43.21.96, 44.00.67) have
# W22O queue's twelve decoder factories, no dts_audiodec.
if soc in ['o22n', 'o22n2', 'o24'] and gst_elements in ['lx-o22-ponytail', 'lx-o22-queue']
  gst_elements += '-dts'
endif
# libsestore (guest/layers/soc/meson.build adds it wherever nyx's secure store reads it): queue's
# libsecretagent reads the model secret through nyx (nyxCrypto, nyxSecureStore), which asks
# libsestore for the SDP_MODEL item with no DILE call between, so the libdile_crypto shim never saw
# it: SA_getDeviceID failed and sdx sent LG's login without X-Device-ID. It reads the item of the
# board's region, then the plain one, the names as the VM's store has them.
# libkadaptor (bsppart's KADP, libkadaptor.so.2.0.1 from mullet to spacebuns; its .so.0 names,
# links to the stock file there, are made links to the shim). Everywhere: KADP's Widevine OEMCrypto
# reaches a TEE the VM lacks, and mullet's and number1's have no failure path there: Cobalt's EME
# setup crashed in wvca_open_session (W22O number1; ponytail's and queue's KADP fail on their own).
# The shim answers OEMCrypto_Initialize INIT_FAILED when KADP's TEE client finds no TEE, which
# the CE CDM takes as Widevine unavailable (kadaptor-oemcrypto.c).
# And after LG's login, sdx sets the server time on the secure clock (libsecuretime, nyxDrm,
# libdrmddi, LGSOC_DRM_SetSecureClock), which ends in PlayReady's TA. With no TEE that call failed,
# sdx stayed unauthenticated and eula-service never started: the User Agreements had no terms.
# The shim answers the TA call TEEC_ERROR_NOT_SUPPORTED, which KADP takes as success, wherever the
# firmware's secure clock goes through nyx's DRM module (nyxDrm.module: queue and spacebuns).
hal_stock += {'libkadaptor': ['libkadaptor.so.2', 'libkadaptor.so.2.0.1',
  ['libkadaptor.so.0', 'libkadaptor.so.0.1.0'], 'bsppart']}
kadaptor_source = ['kadaptor-oemcrypto.c']
fw_questions += 'path usr/lib/nyx/modules/nyxDrm.module'
if fw_path.get('usr/lib/nyx/modules/nyxDrm.module', false)
  kadaptor_source += 'kadaptor-securetime.c'
endif
soc_phys_base = '0xA0000000u' # emulator scratch arena, not a retail memory layout
gl_vendor_stubs = []
gl_vendor_gl = 'mali'
gl_vendor_gl_soname = 'libmali.so.0'
gl_vendor_gl_needs = ['EGL', 'GLESv2', 'GLESv1_CM', 'wayland-egl']
# libgm-wayland (hybridtv-manager's data-broadcast window) makes an EGLImage of a Mali native pixmap
# on each update (W22O 43.21.89): the vendor library takes them (guest/layers/gl/src/mali-pixmap.c).
gl_vendor_mali_pixmap = true
soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W22O 04.40.93 and 13.30.56: libmali.so.0.28.0 prints "OpenGL ES 3.2"; 23.25.55,
# 33.31.61 and 43.21.74: libmali.so.0.46.0 "OpenGL ES 3.2"
gl_es_version = '3.2'
