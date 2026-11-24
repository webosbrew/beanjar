# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The netcast4 layer, build/layers/h13-netcast4/netcast4: NetCast 4's one always-used layer
# (docs/dev/architecture/netcast4.md), stacked over its stock root (root.pak), built with the NetCast SDK
# (meson/cross/lgtv-sdk.ini):
#   rootfs/etc/emulator/board.cmdline   the bootloader's parameters (targets.py's Soc.board_src)
#   mnt/lg/res/lglib/libgal.so.0.0.0    the GAL_GX graphics library on lg115x.ko's OSD frame buffer
#                                       (guest/layers/simplesmart/src/libhalgal built with GAL_GX=1,
#                                       as NetCast 4.5's), with the KADP_* functions procmgr calls
#                                       for its cursor (src/kadp.c); init lays the layer over the
#                                       lglib pak, whose libgal.so links (lginit_app's LD_PRELOAD,
#                                       RELEASE's NEEDED) then reach it. procmgr's KADP_* calls
#                                       reach ours only through that LD_PRELOAD: libkadaptor exports
#                                       the same names
#   mnt/lg/res/lglib/libEGL.so.1.4      NetCast 4.5's hoagx window system over Mesa
#                                       (guest/layers/netcast45/src/libegl-hoagx), in place of the
#                                       PowerVR libEGL and its window system (libpvrNC4_WSEGL: HOA_GX
#                                       windows by the same HOA_GX_* calls, with the same 1064-byte
#                                       surface record); the pak's libEGL.so{,.1} links reach it
#   mnt/lg/res/lglib/libGLESv2.so.2.0, libGLESv1_CM.so.1.1   Mesa's (guest/layers/gl/mesa)
#   mnt/lg/res/lglib/libIMGegl.so, libsrv_um.so   no code: fxui NEEDs them and imports nothing
#                                       of them (the PowerVR libraries have no SONAME)
#   usr/lib/libEGLmesa.so.1, libgallium, libgbm   Mesa's, which libEGL loads
#   usr/share/beanjar/gles-version      OpenGL ES 2.0, the set's own (PowerVR, GLES 2 and 1.1)
gal = shared_library('gal', '../simplesmart/src/libhalgal/halgal.c', '../simplesmart/src/libhalgal/decode.c',
  '../soc/src/libdile_gal/gal-draw.c', 'src/kadp.c',
  include_directories: include_directories('../soc/src/libdile_gal', '../simplesmart/src/libhalgal'),
  c_args: ['-fvisibility=hidden', '-DGAL_GX=1'],
  link_args: ['-lpthread', '-ldl'])
G = 'mnt/lg/res/lglib/'     # over the lglib pak (init's overlay)
netcast4_args = ['--tree', meson.current_source_dir() / 'rootfs', '.',
  '--file', gal, G + 'libgal.so.0.0.0', '755']

mesa_ver = sources['mesa']['version']
egl = shared_library('EGL', '../netcast45/src/libegl-hoagx/egl.c',
  dependencies: mesa_dep,
  c_args: ['-fvisibility=hidden'],
  link_args: ['-Wl,-soname,libEGL.so', '-Wl,--hash-style=both', '-ldl', '-lpthread'],
  link_depends: mesa)
L = 'usr/lib/'
netcast4_args += [
  '--file', egl, G + 'libEGL.so.1.4', '755',
  '--file', mesa_lib / 'libGLESv2.so.2.0.0', G + 'libGLESv2.so.2.0', '755',
  '--file', mesa_lib / 'libGLESv1_CM.so.1.1.0', G + 'libGLESv1_CM.so.1.1', '755',
  '--file', mesa_lib / 'libEGLmesa.so.1.0.0', L + 'libEGLmesa.so.1', '755',
  '--file', mesa_lib / ('libgallium-' + mesa_ver + '.so'), L + 'libgallium-' + mesa_ver + '.so', '755',
  '--file', mesa_lib / 'libgbm.so.1.0.0', L + 'libgbm.so.1', '755',
  '--file', configure_file(input: '../gl/gles-version.in', output: 'gles-version',
    configuration: {'GLES': '2.0'}), 'usr/share/beanjar/gles-version', '644',
]
foreach stub : ['IMGegl', 'srv_um']
  netcast4_args += ['--file', shared_library(stub, '../gl/src/pvr-stub.c',
      link_args: ['-nostdlib', '-Wl,-soname,lib' + stub + '.so']),
    G + 'lib' + stub + '.so', '755']
endforeach

layers += {'netcast4': custom_target('netcast4-layer',
  output: 'netcast4-layer.stamp',
  depfile: 'netcast4-layer.d',
  depends: mesa,
  command: arm_stage + ['--out', layers_dir / 'netcast4', '--stamp', '@OUTPUT@', '--depfile', '@DEPFILE@']
    + netcast4_args,
  build_by_default: true)}
