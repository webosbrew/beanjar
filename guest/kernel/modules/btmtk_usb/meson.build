# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# btmtk_usb.ko, MediaTek's Bluetooth USB driver as webos-bluetooth-service sees it: its netlink
# family and /sys/module/btmtk_usb, and with controller=1 a controller with no radio at /dev/stpbt
# (docs/research/magic-remote-pairing.md): built as lgemu.ko is (docs/dev/guides/build.md, "Kernel
# module lgemu"), carried in the initramfs, and loaded by /init when boot.conf's SOC_MODULES names
# it (the chip table, guest/targets/soc/: m3, k5lp, no adapter), or with controller=1 where the
# firmware's udev rules name it (webOS 6 on). Published as build/btmtk_usb.ko too.
btmtk_usb_dir = kernel_root / 'btmtk_usb'
btmtk_usb_inputs = kbuild + [files('Kbuild', 'btmtk_usb.c')]
btmtk_usb_ko = custom_target('btmtk_usb',
  output: 'btmtk_usb.ko',
  depfile: 'btmtk_usb.ko.d',
  depends: [sources['linux']['stamp'], kernel_outputs],
  depend_files: btmtk_usb_inputs,
  command: kernel_shared + ['--dir', btmtk_usb_dir, '--after', kernel_root, '--depfile', '@DEPFILE@',
            '--input', btmtk_usb_inputs, '--file', btmtk_usb_dir / 'btmtk_usb.ko', '@OUTPUT@', '--',
            kbuild, 'module', sources['linux']['dir'], kernel_obj, get_option('arm64_cross_compile'),
            meson.current_source_dir(), btmtk_usb_dir / 'obj', btmtk_usb_dir / 'btmtk_usb.ko'],
  console: true,
  build_by_default: true)
published += [['file', btmtk_usb_ko.full_path(), 'btmtk_usb.ko']]
publish_depends += btmtk_usb_ko
