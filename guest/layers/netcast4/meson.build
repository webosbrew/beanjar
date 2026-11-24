# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The netcast4 layer, build/layers/h13-netcast4/netcast4: NetCast 4's one always-used layer
# (docs/dev/architecture/netcast4.md), stacked over its stock root (root.pak), built with the NetCast SDK
# (meson/cross/lgtv-sdk.ini):
#   rootfs/etc/emulator/board.cmdline   the bootloader's parameters (targets.py's Soc.board_src)
netcast4_args = ['--tree', meson.current_source_dir() / 'rootfs', '.']

layers += {'netcast4': custom_target('netcast4-layer',
  output: 'netcast4-layer.stamp',
  depfile: 'netcast4-layer.d',
  command: arm_stage + ['--out', layers_dir / 'netcast4', '--stamp', '@OUTPUT@', '--depfile', '@DEPFILE@']
    + netcast4_args,
  build_by_default: true)}
