# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# usr/lib/<soname> of hal_stock['libsestore'] (libsestore.so.0): replaces the stock symlink and NEEDs the
# stock file (unchanged). SESTORE_ReadData is a client of the provisioned kernel store on every
# target with this shim, exact names only: an item the VM does not provide is "no such item" under
# either missing policy (sestore.c). The other SESTORE_* symbols resolve to LG's library.
sestore_stock = shared_library('libsestore', hal_link_stub,
  name_prefix: '', name_suffix: hal_stock['libsestore'][1].substring(11))
sestore = shared_library('sestore', 'sestore.c',
  include_directories: include_directories('../../../../../include'),
  soversion: hal_stock['libsestore'][0].split('.so.')[1],
  link_args: ['-Wl,--no-as-needed', sestore_stock.full_path()],
  link_depends: sestore_stock)
hal_libs += {hal_stock['libsestore'][0]: sestore}
hal_libs_stock += {hal_stock['libsestore'][0]: hal_stock['libsestore'][1]}
