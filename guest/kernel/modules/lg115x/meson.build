# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# lg115x.ko, LG's own kernel drivers (the LG115x kdrivers) for KADP on H13, M14 and M16P:
# built as lgemu.ko is (docs/dev/guides/build.md, "Kernel module
# lgemu"), carried in the initramfs, and loaded by /init when boot.conf's SOC_MODULES names it (the
# chip table, guest/targets/soc/). Published as build/lg115x.ko too.
lg115x_dir = kernel_root / 'lg115x'
lg115x_inputs = kbuild + [files('Kbuild', 'lg115x.h', 'lg115x_main.c', 'bus.c', 'security.c',
                            'logm.c', 'de-histogram.c', 'video.c', 'gop.c', 'audio.c', 'platform.c', 'ucom.c'), lgemu_h]
lg115x_ko = custom_target('lg115x',
  output: 'lg115x.ko',
  depfile: 'lg115x.ko.d',
  depends: [sources['linux']['stamp'], kernel_outputs, lgemu_ko],
  depend_files: lg115x_inputs,
  command: kernel_shared + ['--dir', lg115x_dir, '--after', kernel_root, '--after', lgemu_dir, '--depfile', '@DEPFILE@',
            '--input', lg115x_inputs, '--file', lg115x_dir / 'lg115x.ko', '@OUTPUT@', '--',
            kbuild, 'module', sources['linux']['dir'], kernel_obj, get_option('arm64_cross_compile'),
            meson.current_source_dir(), lg115x_dir / 'obj', lg115x_dir / 'lg115x.ko', lgemu_symvers],
  console: true,
  build_by_default: true)
published += [['file', lg115x_ko.full_path(), 'lg115x.ko']]
publish_depends += lg115x_ko
