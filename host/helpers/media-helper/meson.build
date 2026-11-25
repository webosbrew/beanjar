# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Compressed decoding belongs to this per-VM helper, never to the viewer. It links the private
# FFmpeg (meson/meson.build, packaging/vendor.toml), found through RUNPATH: build/ffmpeg beside
# build/media-helper, /usr/lib/beanjar/ffmpeg once installed.
ffmpeg_deps = [vendor_deps['libavcodec'], vendor_deps['libavutil'], vendor_deps['libswresample']]
# The engine's files live in ../../media/decode/sources.txt (relative to that
# directory) so Meson and ad-hoc trial builds compile the same list.
fs = import('fs')
media_engine_sources = []
foreach s : fs.read('../../media/decode/sources.txt').split(' ')
  st = s.strip()
  if st != ''
    media_engine_sources += '../../media/decode/' + st
  endif
endforeach
# The VM's end of the guest's media ports (media-helper.c),
# published to build/media-helper/. The launcher starts it next to QEMU when it is there.
media_helper = executable('media-helper', ['media-helper.c', '../../media/ipc.c', '../../media/ivserver.c', '../../media/sandbox.c'] + media_engine_sources + coverage_c,
  dependencies: ffmpeg_deps + [dependency('threads')],
  build_rpath: vendor_rpath['ffmpeg'],
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'],
  build_by_default: true)
published += [['file', media_helper.full_path(), 'media-helper/media-helper']]
publish_depends += media_helper
