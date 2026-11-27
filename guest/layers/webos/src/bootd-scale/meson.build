# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# usr/lib/libbootd-scale.so: bootd's fallback timers and Luna reply waits of 5 s and more
# stretched by BOOTD_TIMEOUT_SCALE (bootd-scale.c), in every release's webos layer
# (guest/layers/webos/meson.build).
# Not a shim under a stock soname: guest/initramfs/rootfs/init LD_PRELOADs it into bootd from the
# override it writes for webos.time_scale=N, N > 1 (the launcher's TCG factor,
# host/core/beanjar/runtime/qemu.py TIME_SCALE); nothing else loads it.
bootd_scale = shared_library('bootd-scale', 'bootd-scale.c',
  gnu_symbol_visibility: 'hidden',
  link_args: ['-ldl'],
  build_by_default: false)
