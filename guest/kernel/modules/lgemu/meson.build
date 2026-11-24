# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# lgemu.ko, LG kernel interfaces for the stock firmware (docs/dev/guides/build.md, "Kernel module lgemu"):
# built by the kernel's own Kbuild as an external module (`make M=` with this directory as the
# source and MO= in the build directory) against the kernel's object tree, so it always matches
# the Image it is loaded into. guest/initramfs/rootfs/init loads it from the initramfs; it is published as
# build/lgemu.ko too, for a manual rmmod/insmod from /host.
lgemu_dir = kernel_root / 'lgemu'
lgemu_inputs = kbuild + [files('Kbuild', 'core.c', 'lgemu.h', 'partinfo.c', 'low_mem_notify.c', 'i2c.c', 'accel.c', 'light.c', 'tof.c', 'thermal.c', 'micom-rtc.c', 'micom-rtc.h', 'lgib.c', 'lgemu-i2c.h',
                             'i2c_adapters.c', 'drv.c', 'lgemu-drv.h', 'snd.c', 'mic.c', 'video.c', 'v4l2-vsc.c', 'v4l2-hdmi.c', 'capture.c', 'capture-histogram.c', 'v4l2-capture.c', 'cec.c', 'sestore.c',
                             '../../../../include/webos-sestore.h',
                             '../../../../include/webos-capture.h', '../../../../include/webos-media.h', '../../../../include/webos-ir.h', '../../../../include/webos-video.h')]
lgemu_ko = custom_target('lgemu',
  output: 'lgemu.ko',
  depfile: 'lgemu.ko.d',
  depends: [sources['linux']['stamp'], kernel_outputs],
  depend_files: lgemu_inputs,
  command: kernel_shared + ['--dir', lgemu_dir, '--after', kernel_root, '--depfile', '@DEPFILE@',
            '--input', lgemu_inputs, '--file', lgemu_dir / 'lgemu.ko', '@OUTPUT@', '--',
            kbuild, 'module', sources['linux']['dir'], kernel_obj, get_option('arm64_cross_compile'),
            meson.current_source_dir(), lgemu_dir / 'obj', lgemu_dir / 'lgemu.ko'],
  console: true,
  build_by_default: true)
published += [['file', lgemu_ko.full_path(), 'lgemu.ko']]
# What the SoC modules (lg115x.ko, mstar-utopia.ko, rtk.ko) build against: lgemu's I2C devices
# (lgemu-i2c.h), the driver helpers (lgemu-drv.h) and its exports for modpost.
lgemu_h = files('lgemu-i2c.h', 'lgemu-drv.h')
lgemu_symvers = lgemu_dir / 'obj' / 'Module.symvers'
publish_depends += lgemu_ko

