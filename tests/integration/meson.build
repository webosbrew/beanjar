# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The program check-audio-context.py runs (viewer/test_viewer.py): the viewer core's incoming
# guest audio, independent of the display's default main context, against the stand-in QEMU.
check_audio_context = executable('check-viewer-audio-context', 'viewer/check-audio-context.c',
  link_with: viewer_core, dependencies: [sdl2_dep, dependency('gio-2.0')],
  override_options: ['c_std=c11'])
# The media helper's self-confinement (host/media/sandbox.c) from inside: media-helper/
# test_media_sandbox.py runs it.
check_media_sandbox = executable('check-media-sandbox',
  ['media-helper/check-sandbox.c', '../../host/media/sandbox.c'],
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'])
