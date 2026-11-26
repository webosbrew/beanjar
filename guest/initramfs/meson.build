# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The initramfs, build/initramfs.cpio.gz: the files of initramfs.list (/init and the rest of
# rootfs/, copied as they are; static busybox, busybox/; the pointer bridge, the pumps and the boot
# splash, src/, with its picture from host/ui/data/icon.svg;
# the kernel modules, guest/kernel/modules). Nothing in it comes from the firmware,
# one firmware target or one install: it is the same for every VM and user, and every layer comes from the launcher's host share. It is
# packed by the kernel's own usr/gen_init_cpio (guest/kernel/meson.build) from that list, expanded by
# meson/initramfs.py; the expanded list is build/guest-<chip>-<release>/initramfs/initramfs.list. Every input is a
# real dependency (a program, a file of rootfs/ through the depfile), so any of them
# changing repacks it, and nothing else does.
subdir('busybox')
subdir('src')
gzip = find_program('gzip', native: true)
# the boot splash's picture (src/splash.c), rendered from the logo here, never in the guest
rsvg_convert = find_program('rsvg-convert', native: true, required: true)   # librsvg2-bin
splash_ppm = custom_target('splash-ppm',
  input: top / 'host' / 'ui' / 'data' / 'icon.svg',
  output: 'splash.ppm',
  depend_files: files(top / 'meson' / 'splash.py'),
  command: [python, top / 'meson' / 'splash.py', '--icon', '@INPUT@', '--rsvg-convert', rsvg_convert,
            '--out', '@OUTPUT@'])
initramfs_args = [
  '--var', 'BUSYBOX', busybox,
  '--var', 'VIRTIO_POINTER', virtio_bins['virtio-pointer'],
  '--var', 'VIRTIO_TOUCH', virtio_bins['virtio-touch'],
  '--var', 'VIRTIO_IR', virtio_bins['virtio-ir'],
  '--var', 'VIRTIO_TUNER', virtio_bins['virtio-tuner'],
  '--var', 'VIRTIO_VIDEO', virtio_bins['virtio-video'],
  '--var', 'VIRTIO_SOUND', virtio_bins['virtio-sound'],
  '--var', 'VIRTIO_MIC', virtio_bins['virtio-mic'],
  '--var', 'VIRTIO_MEMINFO', virtio_bins['virtio-meminfo'],
  '--var', 'VIRTIO_CAPTURE', virtio_bins['virtio-capture'],
  '--var', 'VIRTIO_SHELL', virtio_bins['virtio-shell'],
  '--var', 'GOP_PRESENT', virtio_bins['gop-present'],
  '--var', 'SPLASH', virtio_bins['splash'],
  '--var', 'SESTORE_LOAD', virtio_bins['sestore-load'],
  '--var', 'DM_LINEAR', virtio_bins['dm-linear'],
  '--var', 'SPLASH_PPM', splash_ppm,
  # the out-of-tree kernel modules (guest/kernel/modules/<name>), each with a `file` line in
  # initramfs.list
  '--var', 'LGEMU_KO', lgemu_ko,
  '--var', 'MEDIA_POOL_KO', media_pool_ko,
  '--var', 'MSTAR_UTOPIA_KO', mstar_utopia_ko,
  '--var', 'LG115X_KO', lg115x_ko,
  '--var', 'RTK_KO', rtk_ko,
  '--var', 'MTK_KO', mtk_ko,
  '--var', 'BTMTK_USB_KO', btmtk_usb_ko,
  '--var', 'WEBOS_TV_KO', webos_tv_ko,
  '--var', 'DTV_SOC_KO', dtv_soc_ko,
  # the kernel's own modules and lgfs.ko with modules.dep (guest/kernel/meson.build,
  # peripheral_modules), a `tree` line in initramfs.list
  '--var', 'KERNEL_MODULES', peripheral_modules,
]
initramfs = custom_target('initramfs',
  input: 'initramfs.list',
  output: ['initramfs.list', 'initramfs.cpio.gz'],
  depfile: 'initramfs.d',
  command: [python, initramfs_py, '--list', '@INPUT@', '--src', meson.current_source_dir() / 'rootfs',
            '--out-list', '@OUTPUT0@', '--cpio', '@OUTPUT1@', '--depfile', '@DEPFILE@',
            '--gen-init-cpio', gen_init_cpio, '--gzip', gzip] + initramfs_args,
  build_by_default: true)
published += [['file', initramfs[1].full_path(), 'initramfs.cpio.gz']]
publish_depends += initramfs

# The experimental x86-64 guest kernel's initramfs, build/x86_64/initramfs.cpio.gz
# (guest/kernel/meson.build, x86_64_kernel): the same list and rootfs/, with the x86-64 builds of
# busybox, the programs of src/ and the kernel modules.
if x86_64_kernel
  initramfs_x86_64_args = ['--var', 'BUSYBOX', busybox_x86_64, '--var', 'SPLASH_PPM', splash_ppm,
                           '--var', 'KERNEL_MODULES', x86_64_peripheral_modules]
  foreach name, bin : virtio_bins_x86_64
    initramfs_x86_64_args += ['--var', name.to_upper().replace('-', '_'), bin]
  endforeach
  foreach name, ko : x86_64_ko
    if name != 'lgfs'   # in KERNEL_MODULES' extra/
      initramfs_x86_64_args += ['--var', name.to_upper().replace('-', '_') + '_KO', ko]
    endif
  endforeach
  initramfs_x86_64 = custom_target('initramfs-x86_64',
    input: 'initramfs.list',
    output: ['initramfs-x86_64.list', 'initramfs-x86_64.cpio.gz'],
    depfile: 'initramfs-x86_64.d',
    command: [python, initramfs_py, '--list', '@INPUT@', '--src', meson.current_source_dir() / 'rootfs',
              '--out-list', '@OUTPUT0@', '--cpio', '@OUTPUT1@', '--depfile', '@DEPFILE@',
              '--gen-init-cpio', gen_init_cpio, '--gzip', gzip] + initramfs_x86_64_args,
    build_by_default: true)
  published += [['file', initramfs_x86_64[1].full_path(), 'x86_64/initramfs.cpio.gz']]
  publish_depends += initramfs_x86_64
endif
