# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# usr/lib/libmemchute-scale.so: memchute's libfm heartbeat (startHeartBeat's timeout) stretched by
# MEMCHUTE_HEARTBEAT_SCALE (memchute-scale.c), in every release's webos layer
# (guest/layers/webos/meson.build).
# Not a shim under a stock soname: guest/initramfs/rootfs/init LD_PRELOADs it into memchute from the
# drop-in it writes for webos.time_scale=N, N > 1 (the launcher's TCG factor,
# host/core/beanjar/runtime/qemu.py TIME_SCALE); nothing else loads it.
memchute_scale = shared_library('memchute-scale', 'memchute-scale.c',
  gnu_symbol_visibility: 'hidden',
  link_args: ['-ldl'],
  build_by_default: false)
