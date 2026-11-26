# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The webos layer, build/layers/<chip>-<release>/webos, always used: how the release's LG userland
# is put together. Its contents are
# webos_layer_args, which guest/layers/webos/<release>/meson.build and those of its bases (a file 'base',
# docs/dev/guides/build.md "Layer bases") fill in, the base's first: each adds its rootfs/ tree and files, so
# a release's own replace its base's.
# Every release: bootd's timer scaling for TCG (guest/layers/webos/src/bootd-scale), loaded only when
# guest/initramfs/rootfs/init's override asks for it.
webos_layer_args += ['--file', bootd_scale, 'usr/lib/libbootd-scale.so', '755']
# Every release: memchute's heartbeat scaling for TCG (guest/layers/webos/src/memchute-scale), loaded
# only when guest/initramfs/rootfs/init's drop-in asks for it.
webos_layer_args += ['--file', memchute_scale, 'usr/lib/libmemchute-scale.so', '755']
# Every release: the Luna reply and curl timeouts of sdx (guest/layers/webos/src/luna-scale), loaded only when
# guest/initramfs/rootfs/init asks for it.
webos_layer_args += ['--file', luna_scale, 'usr/lib/libluna-scale.so', '755']
layers += {'webos': custom_target('webos-layer',
  output: 'webos-layer.stamp',
  depfile: 'webos-layer.d',
  command: arm_stage + ['--out', layers_dir / 'webos', '--stamp', '@OUTPUT@', '--depfile', '@DEPFILE@']
    + webos_layer_args,
  build_by_default: true)}
