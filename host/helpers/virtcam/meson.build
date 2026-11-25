# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The virtual USB camera, beanjar-virtcam (main.cpp), published
# to build/virtcam/ with the two usbipdcpp libraries beside it (RUNPATH $ORIGIN). usbipdcpp is
# LGPL-3.0 and not in Debian 13: its unmodified source (subprojects/usbipdcpp.wrap) is built by its
# own CMake as shared libraries (build-usbipdcpp.sh), which the program links like any other.
# GStreamer is not linked: the program runs gst-launch-1.0 (gstsource.h).
# The arm64 cross build (host_cross), which makes the packages, requires it unless
# -Dvirtcam=disabled.
virtcam_opt = get_option('virtcam')
virtcam_req = host_cross and virtcam_opt.auto() ? true : virtcam_opt
have_virtcam = false
if not virtcam_opt.disabled() and add_languages('cpp', native: false, required: virtcam_req)
  virtcam_deps = []
  foreach d : ['libusbredirparser-0.5', 'spdlog', 'fmt', 'asio']
    dep = dependency(d, required: virtcam_req)
    if dep.found()
      virtcam_deps += dep
    endif
  endforeach
  cmake = find_program('cmake', required: virtcam_req, native: true)
  have_virtcam = virtcam_deps.length() == 4 and cmake.found()
endif
if have_virtcam
  usbipdcpp_cross = []
  if host_cross
    usbipdcpp_cross = [meson.get_external_property('cross_prefix'), find_program('pkg-config')]
  endif
  usbipdcpp = custom_target('usbipdcpp',
    output: ['usbipdcpp.stamp', 'libusbipdcpp.so', 'libusbipdcpp_virtual_device.so'],
    depends: sources['usbipdcpp']['stamp'],
    depend_files: files('build-usbipdcpp.sh'),
    command: [files('build-usbipdcpp.sh'), sources['usbipdcpp']['dir'],
              meson.current_build_dir() / 'usbipdcpp-obj', meson.current_build_dir() / 'usbipdcpp',
              '@OUTPUT0@'] + usbipdcpp_cross,
    console: true,
    build_by_default: true)
  # The C920's descriptors, byte for byte, from a published lsusb -v of the camera, plus two
  # alternate settings of our own for compressed formats, so MJPEG does not cost QEMU 8000
  # transfers a second: alt 12, 3x1024 bytes every 4th microframe (bInterval 3, 2000 packets/s),
  # and alt 13, 3x1022 bytes every 8th (bInterval 4, 1000 packets/s), which QEMU's xHCI keeps on
  # schedule through a 4 ms late timer where alt 12 slips after 2 ms
  virtcam_descriptors = custom_target('virtcam-descriptors',
    input: files('descriptors/gen.py', 'descriptors/c920-046d-082d-lsusb.txt'),
    output: 'descriptors.h',
    command: [python, '-I', '@INPUT0@', '@INPUT1@', '@OUTDIR@', '--compressed-alt', '0x1400:3',
              '--compressed-alt', '0x13fe:4'],
    capture: false)
  usbipdcpp_dep = declare_dependency(
    sources: [usbipdcpp[0]],
    link_with: [usbipdcpp[1], usbipdcpp[2]],
    compile_args: ['-isystem', meson.current_build_dir() / 'usbipdcpp/usr/include'])
  # Everything but main(): the tests (tests/regress/virtcam) link it too.
  virtcam_core = static_library('virtcam-core',
    ['uvcdesc.cpp', 'camera.cpp', 'gstsource.cpp', 'redir.cpp', virtcam_descriptors],
    dependencies: [usbipdcpp_dep, dependency('threads')] + virtcam_deps,
    override_options: ['cpp_std=c++23', 'warning_level=2', 'werror=true'])
  virtcam_inc = include_directories('.')
  virtcam = executable('beanjar-virtcam', 'main.cpp',
    link_with: virtcam_core,
    dependencies: [usbipdcpp_dep, dependency('threads')] + virtcam_deps,
    build_rpath: '$ORIGIN',
    override_options: ['cpp_std=c++23', 'warning_level=2', 'werror=true'],
    build_by_default: true)
  published += [['file', virtcam.full_path(), 'virtcam/beanjar-virtcam'],
                ['file', usbipdcpp[1].full_path(), 'virtcam/libusbipdcpp.so'],
                ['file', usbipdcpp[2].full_path(), 'virtcam/libusbipdcpp_virtual_device.so']]
  publish_depends += [virtcam, usbipdcpp]
  summary({'Virtual camera': 'built, published to ' + publish_dir / 'virtcam'}, section: 'Host')
else
  summary({'Virtual camera': 'not built (' + (virtcam_opt.disabled() ? '-Dvirtcam=disabled' :
           'needs C++, cmake, libusbredirparser, spdlog, fmt, asio') + ')'}, section: 'Host')
endif
