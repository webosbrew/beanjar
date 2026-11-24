# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# mtk.ko, MediaTek's kernel drivers for its MTAL userland (libmtal.so): built as lgemu.ko is
# (docs/dev/guides/build.md, "Kernel module lgemu"), carried in the initramfs, and loaded by /init
# when boot.conf's SOC_MODULES names it (the chip table, guest/targets/soc/). Published as
# build/mtk.ko too. Its SIF (I2C) buses carry lgemu.ko's devices: it is built after lgemu.ko, with
# its Module.symvers and lgemu-i2c.h, as mstar-utopia.ko is.
mtk_dir = kernel_root / 'mtk'
mtk_inputs = kbuild + [files('Kbuild', 'mtk-main.c', 'mtk-mtal.c', 'mtk-cb.c', 'mtk.h'), lgemu_h]
mtk_ko = custom_target('mtk',
  output: 'mtk.ko',
  depfile: 'mtk.ko.d',
  depends: [sources['linux']['stamp'], kernel_outputs, lgemu_ko],
  depend_files: mtk_inputs,
  command: kernel_shared + ['--dir', mtk_dir, '--after', kernel_root, '--after', lgemu_dir, '--depfile', '@DEPFILE@',
            '--input', mtk_inputs, '--file', mtk_dir / 'mtk.ko', '@OUTPUT@', '--',
            kbuild, 'module', sources['linux']['dir'], kernel_obj, get_option('arm64_cross_compile'),
            meson.current_source_dir(), mtk_dir / 'obj', mtk_dir / 'mtk.ko', lgemu_symvers],
  console: true,
  build_by_default: true)
published += [['file', mtk_ko.full_path(), 'mtk.ko']]
publish_depends += mtk_ko
