# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# usr/lib/libpmlog-keep-all.so: PmLogDaemon's whitelist off (pmlog-keep-all.c), found by symbol,
# for the releases whose PmLog mode is "preload" (host/core/beanjar/firmware/targets.py, RELEASES): the
# webos layers of webOS 1 and 2 put it in; webOS 3 turns LG's devlog switch on instead
# (guest/layers/webos/dreadlocks). Not a shim under a stock soname: guest/initramfs/rootfs/init LD_PRELOADs it into PmLogDaemon
# from the override it writes for webos.pmlog=full; nothing else loads it. Built only when a
# release layer takes it.
pmlog_keep_all = shared_library('pmlog-keep-all', 'pmlog-keep-all.c',
  gnu_symbol_visibility: 'hidden',
  link_args: ['-ldl'],
  build_by_default: false)
