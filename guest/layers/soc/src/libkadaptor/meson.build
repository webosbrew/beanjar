# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# usr/lib/<soname> of hal_stock['libkadaptor']: replaces the stock symlink under the name its users
# NEED and NEEDs the stock file, unchanged; the chip table's kadaptor_source picks what is wrapped.
#   kadaptor-xc.c          M2 (bsppart's MStar KADP, libkadaptor.so, no SONAME) and LM15U (the rootfs's,
#                          libkadaptor.so.1): their KADP runs the XC scaler's window calls on a user-mode
#                          Utopia XC instance, so they are passed on to a kernel-mode one as well, where
#                          mstar-utopia.ko reports the windows.
#   kadaptor-capture.c     M2 (beside kadaptor-xc.c): tv.capture's XC status, display DE window and DIP
#                          answers over that user-mode XC.
#   kadaptor-pvrkey.c      M2 (beside kadaptor-xc.c) and M3: the DVR's secure key (MAdp_Secure_GetSecureDataEMMC's
#                          "/tmp/dvr_key", M3's "/tmp/96B14BF8"), which MStar's TEE decrypts on the TV, from the
#                          VM's store.
#   kadaptor-oemcrypto.c   O22 and its successors (libkadaptor.so.2): Widevine's OEMCrypto_Initialize, answered
#                          INIT_FAILED where KADP's TEE client finds no TEE, as the CE CDM accepts.
#   kadaptor-securetime.c  O22 with nyxDrm (queue, spacebuns), beside it: the secure clock's PlayReady TA call,
#                          which has no TEE here, answered "not supported" as KADP accepts.
kadaptor_base = 'libkadaptor'
kadaptor_soname = hal_stock['libkadaptor'][0]
kadaptor_stock = shared_library(kadaptor_base, hal_link_stub,
  name_prefix: '', name_suffix: hal_stock['libkadaptor'][1].replace(kadaptor_base + '.', ''))
kadaptor_kwargs = {}
if kadaptor_soname.contains('.so.')
  kadaptor_kwargs += {'soversion': kadaptor_soname.split('.so.')[1]}
endif
kadaptor = shared_library('kadaptor', kadaptor_source,
  kwargs: kadaptor_kwargs,
  link_args: ['-Wl,--no-as-needed', kadaptor_stock.full_path(), '-ldl', '-lpthread'],
  link_depends: kadaptor_stock)
hal_libs += {hal_stock['libkadaptor'][0]: kadaptor}
hal_libs_stock += {hal_stock['libkadaptor'][0]: hal_stock['libkadaptor'][1]}
