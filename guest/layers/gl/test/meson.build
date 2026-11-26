# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The gl layer's test programs, build/tests/gl: tests/regress/virgl/run.sh and the scripts here run them in
# the guest against the layer's libraries.
guest_programs += {
  'tests/gl/gles2-triangle': executable('gles2-triangle', 'gles2-triangle.c',
    dependencies: mesa_dep, link_args: ['-lEGL', '-lGLESv2']),
  # MSAA cost/feasibility probe for the IMG render-to-texture question
  'tests/gl/msaa-probe': executable('msaa-probe', 'msaa-probe.c',
    dependencies: mesa_dep, link_args: ['-lEGL', '-lGLESv2']),
  # the ES version contexts get, at most the TV's (gles-version)
  'tests/gl/gles-version': executable('gles-version', 'gles-version.c',
    dependencies: mesa_dep, link_args: ['-lEGL', '-lGLESv2']),
  # a window config and surface for each 8888 GBM format (Mesa patch 0006)
  'tests/gl/gbm-configs': executable('gbm-configs', 'gbm-configs.c',
    dependencies: mesa_dep, link_args: ['-lEGL', '-lgbm']),
  'tests/gl/egl-pbuffer-shim.so': shared_module('egl-pbuffer-shim', 'egl-pbuffer-shim.c',
    dependencies: mesa_dep, name_prefix: '', link_args: ['-lEGL', '-lGLESv2', '-ldl']),
}
if gl_adapt_compositor
  guest_programs += {
    # Cross-process test of the adapter library, which native staging omits.
    'tests/gl/starfish-surface-test': executable('starfish-surface-test', 'starfish-surface-test.c',
      # linked by path, not link_with: no build-tree RUNPATH in a published program
      dependencies: mesa_dep, link_args: [starfish.full_path(), '-lEGL', '-lGLESv2'],
      link_depends: starfish),
  }
endif
