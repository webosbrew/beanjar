# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The arm64 guest kernel, build/Image: configs/webos_virt_defconfig (a savedefconfig file; see
# docs/dev/guides/build.md, "Guest kernel"), with the patches of subprojects/linux.wrap. Built by its own
# Kbuild with CROSS_COMPILE from the arm64_cross_compile option (meson/kbuild.sh); it re-runs when
# the unpacked source, the defconfig or the script change, and make keeps the re-run incremental.
# Its second output is the tree's own usr/gen_init_cpio, which packs the initramfs (guest/initramfs/),
# its third the defconfig's own =m modules as modules_install lays them out, with the modules.dep
# that depmod generated (<release>/...), which the initramfs carries in /lib/modules.
# The out-of-tree modules in modules/ are built against its object tree.
#
# Nothing of it depends on the target: it is built once, in <shared_dir>/kernel/<key> (meson/shared.py),
# whose key is the compiler prefix, the defconfig's name and the source tree, and every target's
# build directory takes the result from there: linux-obj/ (the object tree), the outputs, and a
# directory per out-of-tree module. A changed input (the tree's wrap key, the defconfig, the script,
# the compiler's version, a module's sources) runs make again in the same directory, once.
arm64_gcc = get_option('arm64_cross_compile') + 'gcc'
find_program(arm64_gcc, native: true)
# kernel_debug: configs/debug.config over the defconfig, as a defconfig of its own name
kernel_defconfig = files('configs/webos_virt_defconfig')
kernel_defconfig_name = 'webos_virt_defconfig'
if get_option('kernel_debug')
  kernel_defconfig = custom_target('webos_virt_debug_defconfig',
    input: ['configs/webos_virt_defconfig', 'configs/debug.config'],
    output: 'webos_virt_debug_defconfig',
    command: ['sh', '-c', 'cat "$1" "$2" > "$3"', 'sh', '@INPUT0@', '@INPUT1@', '@OUTPUT@'])
  kernel_defconfig_name = 'webos_virt_debug_defconfig'
endif
kernel_root = shared_dir / 'kernel' / run_command(python, shared_py, 'key',
  'cross_compile ' + get_option('arm64_cross_compile'), 'defconfig ' + kernel_defconfig_name,
  'source ' + sources['linux']['dir'], check: true).stdout().strip()
kernel_obj = kernel_root / 'linux-obj'
# shared.py run, for the kernel and each module: the compiler's version is an input of all of them
kernel_shared = [python, shared_py, 'run', '--tool', arm64_gcc]
kernel_outputs = custom_target('kernel',
  output: ['Image', 'gen_init_cpio', 'kernel-modules'],
  depfile: 'kernel.d',
  depends: sources['linux']['stamp'],
  depend_files: [kbuild, files('configs/webos_virt_defconfig', 'configs/debug.config')],
  command: kernel_shared + ['--dir', kernel_root, '--depfile', '@DEPFILE@',
            '--input', sources['linux']['dir'] / '.wrap-key', kbuild, kernel_defconfig,
            '--file', kernel_root / 'Image', '@OUTPUT0@',
            '--file', kernel_root / 'gen_init_cpio', '@OUTPUT1@',
            '--tree', kernel_root / 'modules', '@OUTPUT2@', '--',
            kbuild, 'linux', sources['linux']['dir'], kernel_obj,
            get_option('arm64_cross_compile'), kernel_defconfig, kernel_root / 'Image',
            kernel_root / 'gen_init_cpio', kernel_root / 'modules'],
  console: true,
  build_by_default: true)
kernel = kernel_outputs[0]
gen_init_cpio = kernel_outputs[1]
kernel_modules = kernel_outputs[2]
published += [['file', kernel.full_path(), 'Image']]
publish_depends += kernel_outputs

subdir('modules/lgemu')
subdir('modules/media-pool')
subdir('modules/mstar-utopia')
subdir('modules/lg115x')
subdir('modules/rtk')
subdir('modules/mtk')
subdir('modules/btmtk_usb')
subdir('modules/webos_tv')
subdir('modules/dtv_soc')
subdir('modules/lgfs')

# The modules /init loads with modprobe (boot.conf's PERIPHERAL_MODULES): the kernel's own
# (kernel_modules) with the out-of-tree ones that load among them in extra/, and depmod's
# modules.dep over all of them. The initramfs carries this tree in /lib/modules.
peripheral_modules = custom_target('peripheral-modules',
  output: 'peripheral-modules',
  depends: [kernel_outputs, lgfs_ko],
  depend_files: [kbuild],
  command: [kbuild, 'tree', sources['linux']['dir'], kernel_obj, get_option('arm64_cross_compile'),
            kernel_modules, '@OUTPUT@', lgfs_ko],
  build_by_default: true)


# The experimental x86-64 guest kernel (vm.toml's hardware.guest_kernel = "x86-64", the q35 machine
# under KVM; docs/dev/architecture/qemu-user-bridge.md), build/x86_64/bzImage: the same source
# tree and the same defconfig with configs/x86_64.config over it, built with ARCH=x86_64 by the
# build machine's own compiler, and the out-of-tree modules above built against it, each from the same sources. On
# an x86-64 build machine only (option x86_64_kernel), never with kernel_debug; once in
# <shared_dir>/kernel/<key> as the arm64 kernel is. guest/initramfs makes its initramfs from these.
x86_64_kernel = get_option('x86_64_kernel').disable_auto_if(
  build_machine.cpu_family() != 'x86_64' or get_option('kernel_debug')).allowed()
if x86_64_kernel
  x86_64_defconfig = custom_target('webos_virt_x86_64_defconfig',
    input: ['configs/webos_virt_defconfig', 'configs/x86_64.config'],
    output: 'webos_virt_x86_64_defconfig',
    command: ['sh', '-c', 'cat "$1" "$2" > "$3"', 'sh', '@INPUT0@', '@INPUT1@', '@OUTPUT@'])
  x86_64_gcc = 'gcc'
  find_program(x86_64_gcc, native: true)
  x86_64_env = {'GUEST_ARCH': 'x86_64'}
  x86_64_root = shared_dir / 'kernel' / run_command(python, shared_py, 'key',
    'arch x86_64', 'cross_compile ', 'defconfig webos_virt_x86_64_defconfig',
    'source ' + sources['linux']['dir'], check: true).stdout().strip()
  x86_64_obj = x86_64_root / 'linux-obj'
  x86_64_shared = [python, shared_py, 'run', '--tool', x86_64_gcc]
  x86_64_outputs = custom_target('kernel-x86_64',
    output: ['bzImage', 'gen_init_cpio-x86_64', 'kernel-modules-x86_64'],
    depfile: 'kernel-x86_64.d',
    depends: sources['linux']['stamp'],
    depend_files: [kbuild, files('configs/webos_virt_defconfig', 'configs/x86_64.config')],
    env: x86_64_env,
    command: x86_64_shared + ['--dir', x86_64_root, '--depfile', '@DEPFILE@',
              '--input', sources['linux']['dir'] / '.wrap-key', kbuild, x86_64_defconfig,
              '--file', x86_64_root / 'bzImage', '@OUTPUT0@',
              '--file', x86_64_root / 'gen_init_cpio', '@OUTPUT1@',
              '--tree', x86_64_root / 'modules', '@OUTPUT2@', '--',
              kbuild, 'linux', sources['linux']['dir'], x86_64_obj, '', x86_64_defconfig,
              x86_64_root / 'bzImage', x86_64_root / 'gen_init_cpio', x86_64_root / 'modules'],
    console: true,
    build_by_default: true)
  published += [['file', x86_64_outputs[0].full_path(), 'x86_64/bzImage']]
  publish_depends += x86_64_outputs
  # the out-of-tree modules: [name, its sources (the arm64 build's inputs), the modules whose
  # Module.symvers it needs], in build order
  x86_64_ko = {}
  foreach m : [['lgemu', lgemu_inputs, []],
               ['media-pool', media_pool_inputs, []],
               ['mstar-utopia', mstar_utopia_inputs, ['lgemu']],
               ['lg115x', lg115x_inputs, ['lgemu']],
               ['rtk', rtk_inputs, ['lgemu']],
               ['mtk', mtk_inputs, ['lgemu']],
               ['btmtk_usb', btmtk_usb_inputs, []],
               ['webos_tv', webos_tv_inputs, ['lgemu']],
               ['dtv_soc', dtv_soc_inputs, ['webos_tv']],
               ['lgfs', lgfs_inputs, []]]
    name = m[0]
    after = ['--after', x86_64_root]
    symvers = []
    depends = [sources['linux']['stamp'], x86_64_outputs]
    foreach dep : m[2]
      after += ['--after', x86_64_root / dep]
      symvers += x86_64_root / dep / 'obj' / 'Module.symvers'
      depends += x86_64_ko[dep]
    endforeach
    x86_64_ko += {name: custom_target(name + '-x86_64',
      output: name + '-x86_64.ko',
      depfile: name + '-x86_64.ko.d',
      depends: depends,
      depend_files: m[1],
      env: x86_64_env,
      command: x86_64_shared + ['--dir', x86_64_root / name] + after + ['--depfile', '@DEPFILE@',
                '--input', m[1], '--file', x86_64_root / name / (name + '.ko'), '@OUTPUT@', '--',
                kbuild, 'module', sources['linux']['dir'], x86_64_obj, '',
                meson.current_source_dir() / 'modules' / name, x86_64_root / name / 'obj',
                x86_64_root / name / (name + '.ko')] + symvers,
      console: true,
      build_by_default: true)}
  endforeach
  x86_64_peripheral_modules = custom_target('peripheral-modules-x86_64',
    output: 'peripheral-modules-x86_64',
    depends: [x86_64_outputs, x86_64_ko['lgfs']],
    depend_files: [kbuild],
    env: x86_64_env,
    command: [kbuild, 'tree', sources['linux']['dir'], x86_64_obj, '', x86_64_outputs[2], '@OUTPUT@',
              x86_64_ko['lgfs']],
    build_by_default: true)
endif
