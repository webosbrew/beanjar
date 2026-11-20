# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Replace the DILE soname, NEEDing the unchanged stock file for every other crypto operation.
dile_crypto_stock = shared_library('libdile_crypto', hal_link_stub,
  name_prefix: '', name_suffix: hal_stock['libdile_crypto'][1].substring(15))
dile_crypto = shared_library('dile_crypto', 'dile-crypto.c',
  soversion: hal_stock['libdile_crypto'][0].split('.so.')[1],
  link_args: ['-Wl,--no-as-needed', dile_crypto_stock.full_path(), '-ldl'],
  link_depends: dile_crypto_stock)
hal_libs += {hal_stock['libdile_crypto'][0]: dile_crypto}
hal_libs_stock += {hal_stock['libdile_crypto'][0]: hal_stock['libdile_crypto'][1]}
