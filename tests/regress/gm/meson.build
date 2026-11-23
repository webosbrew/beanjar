# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# gal-check, build/tests/regress/gm/gal-check: guest/layers/soc/h13's DILE_GAL checked inside webOS next to the live gm.
# It dlopens libdile_gal.so.1, whose stock half (libhalgal, libkadaptor) needs librt and libpthread
# from the executable, as gm has them.
guest_programs += {
  'tests/regress/gm/gal-check': executable('gal-check', 'gal-check.c',
    link_args: ['-Wl,--no-as-needed', '-ldl', '-lrt', '-lpthread']),
}
