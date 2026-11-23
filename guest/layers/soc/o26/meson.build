# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# O26 keeps O24's boundaries; its boards are its own firmware's.
soc_layer_trees += ['--tree', meson.current_source_dir() / 'rootfs', '.']
# Its media elements are the release's (o22): W26O 43.11.77's are a qianshan build of the same
# gst-plugins-bsp 1.0.9-1376 source as W22O queue's, and its audio decoder has no DTS.
# The OpenGL ES version of the chip's GPU (gl_es_version), at most which the gl layer's Mesa
# gives a context: W26O 43.11.77 ships W24O queue's libmali.so.0.46.0 (2de638302f15), "OpenGL ES 3.2"
gl_es_version = '3.2'
