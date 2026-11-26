# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# usr/lib/libluna-scale.so: the Luna reply timeouts a daemon keeps in an exported variable (sdx's) and
# sdx's curl timeouts stretched by LUNA_TIMEOUT_SCALE (luna-scale.c), in every release's webos layer
# (guest/layers/webos/meson.build).
# Not a shim under a stock soname: guest/initramfs/rootfs/init LD_PRELOADs it into those daemons for
# webos.time_scale=N, N > 1 (host/core/beanjar/qemu.py time_scale); nothing else loads it.
luna_scale = shared_library('luna-scale', 'luna-scale.c',
  gnu_symbol_visibility: 'hidden',
  link_args: ['-ldl'],
  build_by_default: false)
