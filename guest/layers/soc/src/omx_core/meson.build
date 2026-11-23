# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The chip's omx_core (W15U: usr/lib/openmax/libOMX.MStar.so.1.0.0), MStar's OpenMAX IL core in
# place of the stock file, for NDL's Esplayer (libndl-directmedia2.so.1), its one client here
# (omx-mstar.c). It replaces the file and needs nothing of it: the stock core's other user, gst-omx,
# is the soc layer's own libgstomx.so, which loads no core. The streams go to the host over the
# media ports (../video/src/mediaport.c without GStreamer). Built after src/video, whose GLib version
# it shares.
fw_questions += 'path ' + omx_core
if fw_path.get(omx_core, false)
  omx_args = ['-DMEDIAPORT_CORE', '-DSTREAMINFO_CORE', '-Wno-unused-parameter', '-Wno-missing-field-initializers',
    '-DGLIB_VERSION_MIN_REQUIRED=GLIB_VERSION_' + gst_set['glib'],
    '-DGLIB_VERSION_MAX_ALLOWED=GLIB_VERSION_' + gst_set['glib']]
  omx_link = ['-Wl,-z,defs', '-Wl,--allow-shlib-undefined', '-Wl,--as-needed', '-pthread']
  omx_link_depends = []
  fw_questions += 'lib usr/lib/libglib-2.0.so.0'
  if 'usr/lib/libglib-2.0.so.0' in fw_stubs
    omx_link += fw_stubs['usr/lib/libglib-2.0.so.0'].full_path()
    omx_link_depends += fw_stubs['usr/lib/libglib-2.0.so.0']
  endif
  # libOMX.MStar.so.1.0.0, SONAME libOMX.MStar.so.1: the stock file's names
  omx_file = omx_core.split('/')[-1]
  omx_version = omx_file.split('.so.')[1]
  omx_lib = shared_library(omx_file.split('.so')[0].substring(3), 'omx-mstar.c',
    '../video/src/mediaport.c', '../video/src/streaminfo.c',
    c_args: omx_args,
    dependencies: glib_headers,
    version: omx_version,
    soversion: omx_version.split('.')[0],
    gnu_symbol_visibility: 'hidden',
    link_args: omx_link,
    link_depends: omx_link_depends)
  video_layer_args += ['--file', omx_lib, omx_core, '755']
  # What libndl-directmedia2.so.1 imports of the core: every one must be defined here.
  omx_imports = ['MS_OMX_Init', 'MS_OMX_DeInit', 'MS_OMX_GetHandle', 'MS_OMX_FreeHandle',
                 'MS_OMX_SetupTunnel', 'MS_OMX_GetComponentsOfRole']
  test('soc-omx-core-exports', find_program('sh', native: true),
    args: ['-c', 'syms=$("$1" --dyn-syms -W "$2" | awk \'$7 != "UND" { print $8 }\'); shift 2; '
      + 'for s; do echo "$syms" | grep -qx "$s" || { echo "missing $s"; exit 1; }; done',
      'sh', readelf, omx_lib] + omx_imports,
    suite: 'layers')
endif
