# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# rtk.ko, Realtek's kernel drivers for its KADP userland (libkadaptor):
# built as lgemu.ko is (docs/dev/guides/build.md, "Kernel module lgemu"), carried in the initramfs, and loaded
# by /init when boot.conf's SOC_MODULES names it (the chip table,
# guest/targets/soc/). Published as build/rtk.ko too. Its internal micom (sys-misc's UCOM) is lgemu.ko's micom: it
# is built after lgemu.ko, with its Module.symvers and lgemu-i2c.h, as mstar-utopia.ko is.
rtk_dir = kernel_root / 'rtk'
rtk_inputs = kbuild + [files('Kbuild', 'rtk-main.c', 'rtk-memory.c', 'rtk-rpc.c', 'rtk-vsc.c',
    'rtk-vt.c', 'rtk-graphics.c', 'rtk-control.c', 'rtk-internal.h', 'tee-ta.c'), lgemu_h]
rtk_ko = custom_target('rtk',
  output: 'rtk.ko',
  depfile: 'rtk.ko.d',
  depends: [sources['linux']['stamp'], kernel_outputs, lgemu_ko],
  depend_files: rtk_inputs,
  command: kernel_shared + ['--dir', rtk_dir, '--after', kernel_root, '--after', lgemu_dir, '--depfile', '@DEPFILE@',
            '--input', rtk_inputs, '--file', rtk_dir / 'rtk.ko', '@OUTPUT@', '--',
            kbuild, 'module', sources['linux']['dir'], kernel_obj, get_option('arm64_cross_compile'),
            meson.current_source_dir(), rtk_dir / 'obj', rtk_dir / 'rtk.ko', lgemu_symvers],
  console: true,
  build_by_default: true)
published += [['file', rtk_ko.full_path(), 'rtk.ko']]
publish_depends += rtk_ko
