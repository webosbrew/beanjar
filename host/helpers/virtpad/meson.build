# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The virtual USB gamepad, beanjar-virtpad (main.cpp): a wired Xbox 360 controller over usbredir,
# published to build/virtpad/. Built where libusbredirparser is found; the arm64 cross build, which
# makes the packages, requires it.
have_virtpad = false
if add_languages('cpp', native: false, required: host_cross)
  virtpad_dep = dependency('libusbredirparser-0.5', required: host_cross)
  have_virtpad = virtpad_dep.found()
endif
if have_virtpad
  # Everything but main(): the unit test (tests/unit/virtpad) links it too.
  virtpad_core = static_library('virtpad-core', ['pad.cpp', 'redir.cpp'],
    dependencies: [virtpad_dep],
    override_options: ['cpp_std=c++20', 'warning_level=2', 'werror=true'])
  virtpad_inc = include_directories('.')
  virtpad = executable('beanjar-virtpad', 'main.cpp',
    link_with: virtpad_core,
    dependencies: [virtpad_dep],
    override_options: ['cpp_std=c++20', 'warning_level=2', 'werror=true'],
    build_by_default: true)
  published += [['file', virtpad.full_path(), 'virtpad/beanjar-virtpad']]
  publish_depends += [virtpad]
  summary({'Virtual gamepad': 'built, published to ' + publish_dir / 'virtpad'}, section: 'Host')
else
  summary({'Virtual gamepad': 'not built (needs C++ and libusbredirparser)'}, section: 'Host')
endif
