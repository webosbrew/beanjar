# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The compiled unit checks: C programs against the sources they test, built and run by Meson
# (`meson test -C build/host --suite unit`); `pytest tests/unit` runs the Python ones.

# host/media (the media-helper's): its IPC, the decode engines' leases, the unavailable decoder,
# the media pool's allocator and its ivshmem server, the helper's bounded logs
check_media_ipc = executable('check-media-ipc', 'media/check-ipc.c', '../../host/media/ipc.c',
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
test('check-media-ipc', check_media_ipc, suite: ['unit', 'media'], timeout: 10)
# Same engine list as the helper (host/media/decode/sources.txt).
lease_engine_sources = []
foreach s : fs.read('../../host/media/decode/sources.txt').split(' ')
  st = s.strip()
  if st != ''
    lease_engine_sources += '../../host/media/decode/' + st
  endif
endforeach
check_media_leases = executable('check-media-leases',
  ['media/check-leases.c', '../../host/media/ipc.c'] + lease_engine_sources,
  dependencies: ffmpeg_deps + [dependency('threads')],
  build_rpath: vendor['ffmpeg'] / 'lib',       # the private FFmpeg, unpacked in build/vendor
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
test('check-media-leases', check_media_leases, suite: ['unit', 'media'], timeout: 10)
# a hardware decoder's fallback to software keeps the stream (host/media/decode/frame.c)
check_media_fallback = executable('check-media-fallback',
  ['media/check-fallback.c', '../../host/media/ipc.c'] + lease_engine_sources,
  dependencies: ffmpeg_deps + [dependency('threads')],
  build_rpath: vendor['ffmpeg'] / 'lib',
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
test('check-media-fallback', check_media_fallback, suite: ['unit', 'media'], timeout: 10,
  protocol: 'tap')
# a software-only decoder (a VM's hwdec=none): its short codec list, its lock against the viewer
check_media_software_only = executable('check-media-software-only',
  ['media/check-software-only.c', '../../host/media/ipc.c'] + lease_engine_sources,
  dependencies: ffmpeg_deps + [dependency('threads')],
  build_rpath: vendor['ffmpeg'] / 'lib',
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
test('check-media-software-only', check_media_software_only, suite: ['unit', 'media'],
  timeout: 10, protocol: 'tap')
# a stream that grows while the guest holds a picture lays its slots out again at once
check_media_relayout = executable('check-media-relayout',
  ['media/check-relayout.c', '../../host/media/ipc.c'] + lease_engine_sources,
  dependencies: ffmpeg_deps + [dependency('threads')],
  build_rpath: vendor['ffmpeg'] / 'lib',
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
test('check-media-relayout', check_media_relayout, suite: ['unit', 'media'], timeout: 20,
  protocol: 'tap')
check_media_unavailable = executable('check-media-unavailable', 'media/check-unavailable.c',
  '../../host/media/ipc.c',
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
test('check-media-unavailable', check_media_unavailable, suite: ['unit', 'media'], timeout: 10)
check_media_pool = executable('check-media-pool', 'media/check-pool.c', '../../host/media/pool.c',
  dependencies: dependency('threads'),
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
test('check-media-pool', check_media_pool, suite: ['unit', 'media'], timeout: 10, protocol: 'tap')
check_media_ivserver = executable('check-media-ivserver', 'media/check-ivserver.c',
  '../../host/media/ivserver.c',
  dependencies: dependency('threads'),
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
test('check-media-ivserver', check_media_ivserver, suite: ['unit', 'media'], timeout: 10, protocol: 'tap')
check_media_log = executable('check-media-log', 'media/check-log.c', '../../host/media/log.c',
  dependencies: dependency('threads'),
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
test('check-media-log', check_media_log, suite: ['unit', 'media'], timeout: 10, protocol: 'tap')

# the headless compositor stand-in's Wayland socket (guest/layers/headless/src/surface-manager.c)
check_headless_wayland = executable('check-headless-wayland', 'layers/check-headless-wayland.c',
  link_args: ['-ldl'], override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
test('check-headless-wayland', check_headless_wayland, suite: ['unit', 'layers'], timeout: 10)

# beanjar-vnc's keysym map (host/vnc/keysym.c), where beanjar-vnc is built
if have_vnc
  check_keysym = executable('check-vnc-keysym', 'vnc/check-keysym.c', '../../host/vnc/keysym.c',
    override_options: ['c_std=c11'])
  test('check-vnc-keysym', check_keysym, suite: ['unit', 'vnc'], timeout: 10)
  # its frame rate above 60 (host/vnc/rate.c): kept while a frame's work fits in its time
  check_vnc_rate = executable('check-vnc-rate', 'vnc/check-rate.c', '../../host/vnc/rate.c',
    override_options: ['c_std=c11', 'warning_level=2', 'werror=true'])
  test('check-vnc-rate', check_vnc_rate, suite: ['unit', 'vnc'], timeout: 10)
endif
# the output rate's pacing (host/viewer/core/pace.h): the viewer's and beanjar-vnc's pictures
check_pace = executable('check-pace', 'viewer/check-pace.c',
  override_options: ['c_std=c11', 'warning_level=2', 'werror=true'])
test('check-pace', check_pace, suite: ['unit', 'viewer'], timeout: 10)
# the remote's infrared records (host/viewer/core/ir.c): data codes, a Japanese remote's extended
# codes, its one-touch 11 and 12 per broadcast type
check_ir = executable('check-ir', 'viewer/check-ir.c', '../../host/viewer/core/ir.c',
  override_options: ['c_std=c11', 'warning_level=2', 'werror=true'])
test('check-ir', check_ir, suite: ['unit', 'viewer'], timeout: 10)
# QEMU's display Disable taken as provisional (host/viewer/core/disable.h): a stray one before a
# scanout leaves the picture, a real one blanks it after the wait
check_disable = executable('check-disable', 'viewer/check-disable.c',
  override_options: ['c_std=c11', 'warning_level=2', 'werror=true'])
test('check-disable', check_disable, suite: ['unit', 'viewer'], timeout: 10)
# a scaler window's placement (host/viewer/core/video-place.h): the source crop (overscan, picture
# size) fills the window, the rest of the source clipped
check_video_place = executable('check-video-place', 'viewer/check-video-place.c',
  dependencies: meson.get_compiler('c').find_library('m'),
  override_options: ['c_std=c11', 'warning_level=2', 'werror=true'])
test('check-video-place', check_video_place, suite: ['unit', 'viewer'], timeout: 10)

# host/viewer/core: the virtual AMP (master gain and mute on the final media mix, the real mixer
# driven with synthetic chunks), the capture port's availability (reaching a guest that opens after
# the viewer, never inside fragmented requests or queued frames), SDL audio from two threads on
# SDL's dummy driver, and the host cursor on a private Xvfb display (host/viewer/README.md)
check_media_mix = executable('check-viewer-mix', 'viewer/check-media-mix.c',
  '../../host/viewer/core/media-frame.c',
  '../../host/viewer/core/media-ipc.c', '../../host/viewer/core/sdl-audio.c',
  '../../host/media/ipc.c',
  dependencies: [dependency('sdl2'), dependency('egl'), dependency('glesv2'),
                 dependency('threads'), meson.get_compiler('c').find_library('m')],
  override_options: ['c_std=c11'])
test('check-media-mix', check_media_mix, suite: ['unit', 'viewer'], timeout: 20)
# a stream's software picture after hardware ones (host/viewer/core/media.c): no texture reused
# across targets; a surfaceless EGL GLES 2 context (llvmpipe), skipped without one
check_media_draw = executable('check-viewer-media-draw', 'viewer/check-media-draw.c',
  '../../host/viewer/core/media-frame.c',
  '../../host/viewer/core/media-ipc.c', '../../host/viewer/core/sdl-audio.c',
  '../../host/media/ipc.c',
  dependencies: [dependency('sdl2'), dependency('egl'), dependency('glesv2'),
                 dependency('threads'), meson.get_compiler('c').find_library('m')],
  override_options: ['c_std=c11'])
test('check-media-draw', check_media_draw, suite: ['unit', 'viewer'], timeout: 20,
  env: {'LIBGL_ALWAYS_SOFTWARE': '1'})
check_sound_status = executable('check-viewer-sound-status', 'viewer/check-sound-status.c',
  '../../host/viewer/core/sound-status.c',
  dependencies: meson.get_compiler('c').find_library('m'),
  override_options: ['c_std=c11'])
test('check-sound-status', check_sound_status, suite: ['unit', 'viewer'], timeout: 20)
check_capture = executable('check-viewer-capture',
  'viewer/check-capture.c', '../../host/viewer/core/capture.c',
  dependencies: dependency('glib-2.0'),
  override_options: ['c_std=c11'])
test('check-capture', check_capture, suite: ['unit', 'viewer'], timeout: 20)
check_sdl_audio = executable('check-viewer-sdl-audio',
  'viewer/check-sdl-audio.c', '../../host/viewer/core/sdl-audio.c',
  dependencies: [dependency('sdl2'), dependency('threads')],
  override_options: ['c_std=c11'])
test('check-sdl-audio', check_sdl_audio, suite: ['unit', 'viewer'], timeout: 60)
check_cursor = executable('check-cursor', 'viewer/check-cursor.c', '../../host/viewer/core/cursor.c',
  dependencies: [sdl2_dep, glesv2_dep],
  override_options: ['c_std=c11'],
  build_by_default: false)
if xvfb_run.found()
  test('check-cursor', xvfb_run, args: ['-a', check_cursor],
    env: {'SDL_VIDEO_X11_FORCE_EGL': '1', 'LIBGL_ALWAYS_SOFTWARE': '1'}, suite: ['unit', 'viewer'])
endif

# The virtual camera (host/helpers/virtcam) without a VM: its descriptors and probe negotiation,
# pacing, the payload header, the gst-launch child, and the usbredir front end against a fake QEMU
if have_virtcam
  check_virtcam = executable('check-virtcam', 'virtcam/check-virtcam.cpp',
    link_with: virtcam_core,
    include_directories: virtcam_inc,
    dependencies: [usbipdcpp_dep, dependency('threads')] + virtcam_deps,
    build_rpath: meson.project_build_root() / 'host/helpers/virtcam',
    override_options: ['cpp_std=c++23', 'warning_level=2', 'werror=true'])
  test('check-virtcam', check_virtcam, suite: ['unit', 'virtcam'], timeout: 60)
endif

# The virtual gamepad (host/helpers/virtpad) without a VM: the controller's descriptors and
# requests, the input report, and the usbredir front end against a fake QEMU
if have_virtpad
  check_virtpad = executable('check-virtpad', 'virtpad/check-virtpad.cpp',
    link_with: virtpad_core,
    include_directories: virtpad_inc,
    dependencies: [virtpad_dep],
    override_options: ['cpp_std=c++20', 'warning_level=2', 'werror=true'])
  test('check-virtpad', check_virtpad, suite: ['unit', 'virtpad'], timeout: 60)
endif
