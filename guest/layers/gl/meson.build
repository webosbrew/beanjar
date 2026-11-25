# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The gl layer, build/layers/<chip>-<release>/gl: Mesa (virgl + softpipe/kms_swrast) with Wayland 1.26 clients,
# the compositor's KMS native window, and stubs for the PVR libraries. Nothing firmware-derived goes
# in; the layer only adds files on top of the stock rootfs. All under /usr/lib unless noted:
#   libEGL.so{,.1,.1.4}, libGLESv2.so{,.2,.2.0}  Mesa; the firmware links the unversioned names
#                                               (gl_standard_abi with gl_vendor_mali_pixmap:
#                                               libEGL's names are libEGL-mali-pixmap.so,
#                                               images of Mali native pixmaps (src/mali-pixmap.c)
#                                               over Mesa's, libEGL-mesa.so.1)
#   libGLESv1_CM.so{,.1,.1.1}                   Mesa's GLES 1, under the vendor's names as on a set
#   libgallium-*.so, libgbm.so.1, gbm/dri_gbm.so, libdrm.so.2
#   libffi.so.6{,.0.1}                        our pinned 3.0.13, used by Wayland
#   libwayland-client.so.0                      1.26 for every process; the core protocol tables
#                                               come from the stock server library (1.0.3 or
#                                               1.1, webOS-patched): mesa-stack.sh
#   libwayland-egl.so{,.0,.0.1,.1}              1.26
#                                               (both only where the firmware's Wayland predates
#                                               1.20; else Mesa is built against the firmware's
#                                               version and its own libraries stay: mesa/meson.build)
#   libwayland-server-compat.so.0               newer server API on top of the stock 1.0.3/1.1
#                                               (webOS 1 and 2 only)
#   lib<gl_vendor_gl>.so{,.0,.0.1}              the vendor's one GL library (m2: libmali): empty,
#                                               NEEDing the Mesa libraries gl_vendor_gl_needs names;
#                                               with gl_vendor_window_rgba (m16p) the vendor's
#                                               window-config rule, also as libEGL.so{,.1,.1.4},
#                                               and Mesa's libEGL as libEGL-mesa.so.1; with
#                                               gl_vendor_mali_pixmap (o22) images of Mali
#                                               native pixmaps (src/mali-pixmap.c); with
#                                               gl_vendor_mali_compute (m16p) Mali's UMP and
#                                               OpenCL without a GPU (src/mali-compute.c)
#   libegl-kms.so                               compositor only (LD_PRELOAD): window 0 -> KMS
#   libstarfishsurface.so.0.0.0                 QtWebKit's shared surfaces on gbm/dmabuf
#   libIMGegl.so*, libsrv_um.so*, libglslcompiler.so*   empty stubs
#   /usr/share/beanjar/gles-version             the TV's OpenGL ES version (gl_es_version), at most
#                                               which Mesa gives a context (patches/mesa/0005-*)
#   libjail-gpu.so                              the render node in native app jails, in sam's
#                                               LD_PRELOAD by etc/init/sam.override or
#                                               sam.service.d/zz-beanjar-jail.conf: the common
#                                               layer's file with this library added
#   + everything under rootfs/: the Upstart overrides in etc/init, and their systemd forms
#     (webOS 5) in etc/systemd/system: the compositor drop-in, emulator-gl-devperms.service
# Optional, but built per (chip, release): the vendor names are the soc layer's (gl_vendor_stubs,
# gl_vendor_gl), or gl_standard_abi explicitly enables standard EGL/GLES without vendor stubs.
# The soc table is guest/layers/soc/<chip>/meson.build; Wayland compatibility comes from
# the webos layer (wayland_server_compat, guest/layers/webos/<release>/meson.build).
# A chip without vendor stand-ins or the standard EGL/GLES ABI gets no gl layer (gl_built false; the top-level
# meson.build then leaves out its test programs, and the launcher leaves the layer out with a
# message).
gl_built = false
if gl_vendor_stubs.length() == 0 and gl_vendor_gl == '' and not get_variable('gl_standard_abi', false)
  warning('no gl layer for ' + guest_target + ': soc ' + soc + ' has no graphics ABI configured; the target boots headless only')
  subdir_done()
endif
gl_built = true
# gl_vendor_window_rgba (soc table): the formats the vendor's driver makes window configs of, as
# [r, g, b, a] sizes; set, the vendor library (gl_vendor_gl) applies that rule to Mesa's configs.
gl_window_rgba = get_variable('gl_vendor_window_rgba', [])
if gl_window_rgba.length() > 0 and gl_vendor_gl == ''
  error('gl_vendor_window_rgba needs gl_vendor_gl')
endif
# Native compositors own their DRM/GBM windows and stock output policy. Existing
# profiles retain their adapter and Weston compatibility setup by default.
gl_compositor_integration = get_variable('gl_compositor_integration', 'adapted')
if gl_compositor_integration not in ['adapted', 'native']
  error('unknown GL compositor integration: ' + gl_compositor_integration)
endif
gl_adapt_compositor = gl_compositor_integration == 'adapted'
# Live native Spacebuns defines a transparent 64x64 QEMU cursor while libim's
# real 80x95 BO renders through the owned cursor port. Keep this route scoped;
# other native targets retain their original cursor policy.
gl_native_cursor_bridge = not gl_adapt_compositor and guest_target == 'o22n2-spacebuns'
# Native Spacebuns shows LG's own two outputs on one TV connector: the kernel words of
# native-tv-rootfs give the one virtio scanout a TV connector and an overlay plane, as a set's.
gl_native_tv_head = not gl_adapt_compositor and guest_target == 'o22n2-spacebuns'

subdir('mesa')          # libffi, libdrm, Wayland and Mesa (mesa_dep, wl_compat, mesa_lib)
# The OpenGL ES versions a TV's GPU may have, and the template of the file that states it
gl_es_versions = ['2.0', '3.0', '3.1', '3.2']
gl_es_version_in = files('gles-version.in')
if get_variable('gl_es_version', '') not in [''] + gl_es_versions
  error('gl_es_version: not one of ' + ', '.join(gl_es_versions))
endif

# ---- our own libraries ----
# Callbacks and stubs keep the signatures of the APIs they implement.
jail_gpu = shared_library('jail-gpu', 'src/jail-gpu.c',
  link_args: ['-Wl,--hash-style=both', '-ldl'])
if gl_adapt_compositor or gl_native_cursor_bridge
  kms_cursor_compat = shared_library('kms-cursor-compat', 'src/kms-cursor-compat.c',
    dependencies: mesa_dep,
    link_args: ['-Wl,--hash-style=both', '-ldl', '-lpthread'],
    link_depends: mesa)
endif
if gl_adapt_compositor
  egl_kms = shared_library('egl-kms', 'src/egl-kms.c',
    dependencies: mesa_dep,
    c_args: ['-fvisibility=hidden'],
    link_args: ['-Wl,--hash-style=both', '-lEGL', '-lGLESv2', '-lgbm', '-ldrm', '-ldl'],
    link_depends: mesa)
  kms_native_scanout = shared_library('kms-native-scanout', 'src/kms-native-scanout.c',
    dependencies: mesa_dep,
    c_args: ['-fvisibility=hidden'],
    link_args: ['-Wl,--hash-style=both', '-ldrm', '-ldl', '-lpthread'],
    link_depends: mesa)
  kms_native_input = shared_library('kms-native-input', 'src/kms-native-input.c',
    c_args: ['-fvisibility=hidden'],
    link_args: ['-Wl,--hash-style=both', '-ldl', '-lpthread'])
  native_input_qt_files = []
  if soc == 'o22n2'
    add_languages('cpp', native: false)
    native_input_qt_dep = dependency('Qt5Gui', method: 'pkg-config')
    native_input_qpa = native_input_qt_dep.get_variable(pkgconfig: 'includedir') / 'QtGui' / native_input_qt_dep.version() / 'QtGui'
    kms_native_input_qt = shared_library('kms-native-input-qt', 'src/kms-native-input-qt.cpp',
      dependencies: native_input_qt_dep,
      include_directories: include_directories(native_input_qpa),
      cpp_args: ['-fvisibility=hidden', '-fno-exceptions', '-fno-rtti'],
      link_args: ['-Wl,--hash-style=both', '-ldl'])
    native_input_qt_files = ['--file', kms_native_input_qt, 'usr/lib/libkms-native-input-qt.so', '755']
  endif
  starfish = shared_library('starfishsurface', 'src/starfish-surface.c',
    version: '0.0.0', soversion: '0',
    dependencies: mesa_dep,
    c_args: ['-fvisibility=hidden'],
    link_args: ['-Wl,--hash-style=both', '-lEGL', '-lgbm', '-lpthread'],
    link_depends: mesa)
endif
pvr_stubs = {}
foreach n : gl_vendor_stubs
  pvr_stubs += {n: shared_library(n, 'src/pvr-stub.c', link_args: ['-nostdlib'])}
endforeach
# The vendor's one GL library: no code, only NEEDED entries (soname lib<name>.so, as the firmware
# NEEDs it). A process linking it gets Mesa's EGL and GLES functions from its global scope, a
# dlopen of it or of a library NEEDing it gets them from that object's dependencies, and a dlsym
# on its handle searches them too.
# Which Mesa libraries it NEEDs is the chip's gl_vendor_gl_needs: libEGL, libGLESv2 and
# libGLESv1_CM (Mali's one library carries GLES 1 too), and libwayland-egl where the vendor library also exports wl_egl_window_* (k5lp's Mali Bifrost). Its
# SONAME is the chip's gl_vendor_gl_soname where the vendor's has one (k7lp's libmali.so.0, which
# the firmware NEEDs), else lib<name>.so (the vendor's had none).
# With the chip's gl_vendor_mali_compute: Mali's UMP and OpenCL functions as the vendor library
# answers them without its GPU driver (src/mali-compute.c; m16p: LG's HAL loads libehsp, which
# imports them from libmali).
vendor_extra_src = get_variable('gl_vendor_mali_compute', false) ? ['src/mali-compute.c'] : []
# Where a library of ours is libEGL.so.1 (gl_vendor_window_rgba, gl_egl_mali_pixmap below), Mesa's
# libEGL is NEEDed as libEGL-mesa.so.1, the SONAME of an empty library linked for that only (the
# layer ships Mesa's file under that name).
mesa_egl_name = shared_library('EGL-mesa', 'src/pvr-stub.c', soversion: '1',
  link_args: ['-nostdlib'], build_by_default: false)
if gl_vendor_gl != ''
  # by the stage's unversioned names, which every version has (a -l would make Meson record the
  # file it resolves to when configuring, such as libwayland-egl.so.1.26.0, as an input)
  vendor_needs = []
  foreach n : gl_vendor_gl_needs
    vendor_needs += mesa_lib / 'lib' + n + '.so'
  endforeach
  vendor_soname = get_variable('gl_vendor_gl_soname', '')
  if gl_window_rgba.length() == 0 and get_variable('gl_vendor_mali_pixmap', false)
    # With the chip's gl_vendor_mali_pixmap: eglCreateImageKHR of Mali's native pixmaps
    # (src/mali-pixmap.c), which libgm-wayland draws its windows with; NEEDing the same Mesa
    # libraries.
    if 'EGL' not in gl_vendor_gl_needs or 'GLESv2' not in gl_vendor_gl_needs
      error('gl_vendor_mali_pixmap: needs EGL and GLESv2 in gl_vendor_gl_needs')
    endif
    if vendor_soname != ''
      vendor_needs += '-Wl,-soname,' + vendor_soname
    endif
    pvr_stubs += {gl_vendor_gl: shared_library(gl_vendor_gl, 'src/mali-pixmap.c', vendor_extra_src,
      dependencies: mesa_dep,
      c_args: ['-fvisibility=hidden'],
      link_args: ['-Wl,--hash-style=both', '-ldl', '-lpthread',
                  '-L' + mesa_lib, '-Wl,--no-as-needed'] + vendor_needs,
      link_depends: mesa)}
  elif gl_window_rgba.length() == 0
    if vendor_soname != ''
      vendor_needs += '-Wl,-soname,' + vendor_soname
    endif
    pvr_stubs += {gl_vendor_gl: shared_library(gl_vendor_gl, 'src/pvr-stub.c', vendor_extra_src,
      link_args: ['-nostdlib', '-L' + mesa_lib, '-Wl,--no-as-needed'] + vendor_needs,
      link_depends: mesa)}
  else
    # With the chip's gl_vendor_window_rgba: the vendor driver's window-config rule
    # (src/egl-window-formats.c), and the library stands in for the vendor's libEGL wrappers too.
    # Mesa's libEGL is NEEDed as libEGL-mesa.so.1 (mesa_egl_name), as libEGL.so.1 is this library.
    if vendor_soname != '' or 'EGL' not in gl_vendor_gl_needs
      error('gl_vendor_window_rgba: needs EGL in gl_vendor_gl_needs and no gl_vendor_gl_soname')
    endif
    other_needs = []
    foreach n : gl_vendor_gl_needs
      if n != 'EGL'
        other_needs += mesa_lib / 'lib' + n + '.so'
      endif
    endforeach
    rgba = []
    foreach f : gl_window_rgba
      rgba += '{@0@,@1@,@2@,@3@}'.format(f[0], f[1], f[2], f[3])
    endforeach
    pvr_stubs += {gl_vendor_gl: shared_library(gl_vendor_gl, 'src/egl-window-formats.c', vendor_extra_src,
      dependencies: mesa_dep,
      c_args: ['-fvisibility=hidden', '-DWINDOW_RGBA=' + ','.join(rgba)],
      link_with: mesa_egl_name,
      link_args: ['-Wl,--hash-style=both', '-Wl,-soname,libEGL.so.1', '-ldl',
                  '-L' + mesa_lib, '-Wl,--no-as-needed'] + other_needs,
      link_depends: mesa)}
  endif
endif

# Chips with the standard EGL ABI (gl_standard_abi, no vendor library) and the soc table's
# gl_vendor_mali_pixmap: libgm-wayland, the window of the broadcast services' captions and of
# hybridtv-manager's data broadcast, makes an EGLImage of a Mali native pixmap on each update
# (GM_WL_UpdateWindow, W23P 33.31.61), with the eglCreateImageKHR of eglGetProcAddress, which Mesa
# refuses. It NEEDs libEGL.so.1 and no vendor library, so this library is libEGL.so.1:
# src/mali-pixmap.c, the o22 vendor library's code, in front of Mesa's libEGL (libEGL-mesa.so.1,
# mesa_egl_name), as m16p's window-format rule is. It defines only eglGetProcAddress and takes only
# EGL_NATIVE_PIXMAP_KHR images of a Mali pixmap; every other call is Mesa's unchanged
# (docs/research/tuner-emulation.md, "Captions on the standard-ABI chips").
gl_egl_mali_pixmap = (gl_vendor_gl == '' and get_variable('gl_standard_abi', false)
                      and get_variable('gl_vendor_mali_pixmap', false))
if gl_egl_mali_pixmap
  egl_mali_pixmap = shared_library('EGL-mali-pixmap', 'src/mali-pixmap.c',
    dependencies: mesa_dep,
    c_args: ['-fvisibility=hidden', '-DMESA_EGL="libEGL-mesa.so.1"'],
    link_with: mesa_egl_name,
    link_args: ['-Wl,--hash-style=both', '-Wl,-soname,libEGL.so.1', '-ldl', '-lpthread',
                '-L' + mesa_lib, '-Wl,--no-as-needed', mesa_lib / 'libGLESv2.so'],
    link_depends: mesa)
endif

# ---- the layer ----
mesa_ver = sources['mesa']['version']                  # 26.2.3
drm_ver = sources['libdrm']['version'].split('.')      # 2.4.134
wl_ver = sources['wayland']['version'].split('.')      # 1.26.0
drm_so = 'libdrm.so.2.@0@.0'.format(drm_ver[2])
# libffi 3.0.13's libtool ABI is 6.0.1, as installed by the pinned dependency build.
ffi_so = 'libffi.so.6.0.1'
wl_egl_so = 'libwayland-egl.so.1.@0@.@1@'.format(wl_ver[1], wl_ver[2])
wl_client_so = 'libwayland-client.so.0.@0@.@1@'.format(wl_ver[1], wl_ver[2])
L = 'usr/lib/'
gl_layer_args = []
if gl_adapt_compositor
  gl_layer_args += ['--tree', meson.current_source_dir() / 'rootfs', '.']
else
  # No scanout/input preload, window adapter or weston-setup: the latter's presence
  # enables the initramfs compatibility rewrite of atomic/output settings.
  # The native helper selects the measured virtio format. Custom user ini files take precedence.
  gl_layer_args += ['--tree', meson.current_source_dir() / 'native-rootfs', '.']
  native_profile_data = configuration_data()
  native_profile_data.set('TARGET', guest_target)
  native_profile = configure_file(input: 'native-weston-target.in',
    output: 'native-weston-target', configuration: native_profile_data)
  gl_layer_args += ['--file', native_profile, 'usr/share/beanjar/native-weston-target', '644']
  if gl_native_cursor_bridge
    gl_layer_args += ['--tree', meson.current_source_dir() / 'native-cursor-rootfs', '.']
  endif
  if gl_native_tv_head
    gl_layer_args += ['--tree', meson.current_source_dir() / 'native-tv-rootfs', '.']
  endif
  foreach f : ['etc/init/gl-devperms.conf',
               'etc/systemd/system/basic.target.d/emulator-gl-devperms.conf',
               'etc/systemd/system/emulator-gl-devperms.service']
    gl_layer_args += ['--file', meson.current_source_dir() / 'rootfs' / f, f, '644']
  endforeach
endif
# Wayland 1.26's client and egl libraries only where the firmware's Wayland is too old for Mesa;
# with firmware_wayland (mesa/meson.build) the firmware's own serve every process.
foreach f : ['libEGL.so.1.0.0', 'libGLESv2.so.2.0.0', 'libGLESv1_CM.so.1.1.0', 'libgallium-' + mesa_ver + '.so', 'libgbm.so.1.0.0',
             drm_so, ffi_so, 'gbm/dri_gbm.so'] + (fw_wayland == '' ? [wl_egl_so] : [])
  gl_layer_args += ['--file', mesa_lib / f, L + f, '755']
endforeach
if fw_wayland == ''
  gl_layer_args += ['--file', mesa_lib / 'webos/libwayland-client.so.0', L + wl_client_so, '755']
endif
gl_layer_args += ['--file', jail_gpu, L + 'libjail-gpu.so', '755']
# usr/share/beanjar/gles-version: the OpenGL ES version of the TV's own GPU, the chip's
# gl_es_version (soc table), at most which the guest's Mesa gives a context
# (patches/mesa/0005-*); none where the chip states none
gl_es_version = get_variable('gl_es_version', '')
if gl_es_version != ''
  gl_layer_args += ['--file', configure_file(input: gl_es_version_in, output: 'gles-version',
    configuration: {'GLES': gl_es_version}), 'usr/share/beanjar/gles-version', '644']
endif
# sam's preload with libjail-gpu in front of the common layer's libjail-trust: the same path as the
# common layer's file (guest/layers/common/meson.build, jail_sam_in), which this one replaces
jail_sam_gl = configure_file(input: jail_sam_in, output: 'jail-sam-gl',
  configuration: {'JAIL_PRELOAD': '/usr/lib/libjail-gpu.so:/usr/lib/libjail-trust.so'})
gl_layer_args += ['--file', jail_sam_gl, jail_sam_path, '644']
if gl_adapt_compositor
  gl_layer_args += [
    '--file', egl_kms, L + 'libegl-kms.so', '755',
    '--file', kms_native_scanout, L + 'libkms-native-scanout.so', '755',
    '--file', kms_native_input, L + 'libkms-native-input.so', '755',
    '--file', starfish, L + 'libstarfishsurface.so.0.0.0', '755',
  ]
  gl_layer_args += native_input_qt_files
endif
if gl_adapt_compositor or gl_native_cursor_bridge
  gl_layer_args += ['--file', kms_cursor_compat, L + 'libkms-cursor-compat.so', '755']
endif
if wayland_server_compat
  gl_layer_args += ['--file', wl_compat, L + 'libwayland-server-compat.so.0', '755']
endif
links = {
  'libEGL.so.1.0.0': ['libEGL.so', 'libEGL.so.1', 'libEGL.so.1.4'],
  'libGLESv2.so.2.0.0': ['libGLESv2.so', 'libGLESv2.so.2', 'libGLESv2.so.2.0'],
  'libGLESv1_CM.so.1.1.0': ['libGLESv1_CM.so', 'libGLESv1_CM.so.1', 'libGLESv1_CM.so.1.1'],
  'libgbm.so.1.0.0': ['libgbm.so.1'],
  drm_so: ['libdrm.so.2'],
  ffi_so: ['libffi.so.6'],
}
if fw_wayland == ''
  links += {
    wl_client_so: ['libwayland-client.so.0'],
    wl_egl_so: ['libwayland-egl.so', 'libwayland-egl.so.0', 'libwayland-egl.so.0.1', 'libwayland-egl.so.1'],
  }
endif
if gl_adapt_compositor
  links += {'libstarfishsurface.so.0.0.0': ['libstarfishsurface.so.0']}
endif
if gl_egl_mali_pixmap
  # every libEGL name but Mesa's own file is the pixmap library (above)
  gl_layer_args += ['--file', egl_mali_pixmap, L + 'libEGL-mali-pixmap.so', '755']
  links += {'libEGL.so.1.0.0': ['libEGL-mesa.so.1'],
            'libEGL-mali-pixmap.so': ['libEGL.so', 'libEGL.so.1', 'libEGL.so.1.4']}
endif
if gl_window_rgba.length() > 0
  # every libEGL name but Mesa's own file is the vendor library (above), as on the set
  vendor_file = 'lib' + gl_vendor_gl + '.so.0.1'
  links += {'libEGL.so.1.0.0': ['libEGL-mesa.so.1'],
            vendor_file: ['libEGL.so', 'libEGL.so.1', 'libEGL.so.1.4']}
endif
foreach n, lib : pvr_stubs
  gl_layer_args += ['--file', lib, L + 'lib' + n + '.so.0.1', '755']
  links += {'lib' + n + '.so.0.1': links.get('lib' + n + '.so.0.1', []) + ['lib' + n + '.so', 'lib' + n + '.so.0']}
endforeach
foreach target, names : links
  foreach n : names
    gl_layer_args += ['--link', target, L + n]
  endforeach
endforeach
layers += {'gl': custom_target('gl-layer',
  output: 'gl-layer.stamp',
  depfile: 'gl-layer.d',
  depends: mesa,
  command: arm_stage + ['--out', layers_dir / 'gl', '--stamp', '@OUTPUT@', '--depfile', '@DEPFILE@']
    + gl_layer_args,
  build_by_default: true)}

# The staged native configuration bootstrap and the adapter configuration unchanged for existing
# compositors: tests/integration/layers/test_gl_integration.py, from this lane's artefacts.json
guest_test_artefacts += {'gl_layer': layers_dir / 'gl', 'gl_integration': gl_compositor_integration}
guest_test_depends += layers['gl']
