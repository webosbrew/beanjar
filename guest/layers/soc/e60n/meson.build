# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The LG E60N (lxboot's E60, LG1811), W21K's SoC: O20N's soc layer (base: guest/layers/soc/o20n is
# read first) with W21K's own board and GPU. W21K 03.70.25 has O20N's DILE and HAL libraries (the
# same versions and SONAMEs, the same libhal_lg115x exports) and its kdriver and GPU jobs (galcore
# under chip/e60), so O20N's shims and no-op jobs hold. rootfs/ holds only the board
# (etc/emulator/board-atsc.cmdline).

soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# The GPU userland is another DDK than O20N's: bsppart's libmali.so.0.28.0 (SONAME libmali.so.0,
# "OpenGL ES 3.2", r28p0, O22 mullet's version) with libEGL, libGLESv2, libGLESv1_CM, libOpenCL
# and libwayland-egl.so.1 wrappers NEEDing it; its users NEED libmali.so.0 and libwayland-egl.so.1.
# Nothing NEEDs libOpenCL.
gl_vendor_gl_soname = 'libmali.so.0'
gl_vendor_gl_needs = ['EGL', 'GLESv2', 'GLESv1_CM', 'wayland-egl']
gl_es_version = '3.2'
