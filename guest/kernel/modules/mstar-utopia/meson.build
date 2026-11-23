# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# mstar-utopia.ko, MStar's kernel drivers for its Utopia userland (KADP): built as lgemu.ko is (docs/dev/guides/build.md, "Kernel module lgemu"),
# carried in the initramfs, and loaded by /init when boot.conf's SOC_MODULES names it (the chip
# table, guest/targets/soc/). Published as build/mstar-utopia.ko too.
mstar_utopia_dir = kernel_root / 'mstar-utopia'
mstar_utopia_inputs = kbuild + [files('Kbuild', 'main.c', 'memory.c', 'mmio.c', 'semutex.c', 'semutex-new.c',
                             'mailbox.c', 'gop.c', 'utopia.c', 'hwi2c.c', 'swi2c.c', 'gpio.c', 'ir.c', 'diagnostics.c',
                             'private.h', '../../../../include/webos-video.h',
                             '../../../../include/mstar-gop.h'), lgemu_h]
mstar_utopia_ko = custom_target('mstar-utopia',
  output: 'mstar-utopia.ko',
  depfile: 'mstar-utopia.ko.d',
  depends: [sources['linux']['stamp'], kernel_outputs, lgemu_ko],
  depend_files: mstar_utopia_inputs,
  command: kernel_shared + ['--dir', mstar_utopia_dir, '--after', kernel_root, '--after', lgemu_dir, '--depfile', '@DEPFILE@',
            '--input', mstar_utopia_inputs, '--file', mstar_utopia_dir / 'mstar-utopia.ko', '@OUTPUT@', '--',
            kbuild, 'module', sources['linux']['dir'], kernel_obj, get_option('arm64_cross_compile'),
            meson.current_source_dir(), mstar_utopia_dir / 'obj', mstar_utopia_dir / 'mstar-utopia.ko', lgemu_symvers],
  console: true,
  build_by_default: true)
published += [['file', mstar_utopia_ko.full_path(), 'mstar-utopia.ko']]
publish_depends += mstar_utopia_ko

