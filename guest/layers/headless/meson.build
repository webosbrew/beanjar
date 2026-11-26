# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The headless layer, build/layers/<chip>-<release>/headless (LAYERS=...,headless): no graphics; it
# stands in for what needs a GPU. Built per target, as gl is,
# since a systemd release's stand-in is a program. It carries both inits' forms side by side;
# each init ignores the other's tree:
#   rootfs/etc/init/                 Upstart (webOS 1 to 4): the compositor job that emits
#                                    lsm-ready and stays up, the jobs that need it left manual
#   rootfs/usr/bin/com.webos.app.inputcommon   Live TV, which sends bootd's banner-ready and ends
#   rootfs/etc/systemd/system/       systemd: webapp-mgr not started (needs a GPU)
#   rootfs/usr/sbin/emulator-headless-firstuse  with the normal layer, ran-firstuse before bootd
#                                    (FirstUse's UI cannot run headless), from a task job
#                                    (Upstart) or a unit before bootd.service (systemd)
#   usr/bin/surface-manager          systemd releases only (the release's init_system): the
#                                    compositor stand-in src/surface-manager.c, under the stock
#                                    unit and LG's role for that path
headless_args = ['--tree', meson.current_source_dir() / 'rootfs', '.']
if get_variable('init_system', 'upstart') == 'systemd'
  headless_compositor = executable('surface-manager', 'src/surface-manager.c',
    link_args: ['-ldl'])
  headless_args += ['--file', headless_compositor, 'usr/bin/surface-manager', '755']
endif
layers += {'headless': custom_target('headless-layer',
  output: 'headless-layer.stamp',
  depfile: 'headless-layer.d',
  command: arm_stage + ['--out', layers_dir / 'headless', '--stamp', '@OUTPUT@', '--depfile', '@DEPFILE@']
    + headless_args,
  build_by_default: true)}
