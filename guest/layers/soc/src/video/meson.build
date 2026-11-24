# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The media elements of the soc layer (guest/layers/soc): GStreamer elements in place of LG's hardware
# video and audio elements, which send the compressed streams to the host viewer through the
# webos.media.<n> ports (src/mediaport.c, include/webos-media.h). The chip's
# gst_elements (guest/layers/soc/<chip>/meson.build) names the set, after the stock plugins:
#   vdecsink (H13, M14: GStreamer 1.2.4, GLib 2.34; video milestones 1 and 2)
#     <gst_plugin_dir>/libgstlxvdecbin-1.0.so     "vdecsink"                       src/vdecsink.c
#     <gst_plugin_dir>/libgstadecsink-1.0.so      "adecsink"                       src/adecsink.c
#   lx (M16P: GStreamer 1.8.0, GLib 2.40; "Implemented: W17H's media elements")
#     <gst_plugin_dir>/libgstlxvideodec-1.0.so    "omx_lxvideodec", "lxvideodec"   src/lxvideodec.c
#     <gst_plugin_dir>/libgstlxvideosink-1.0.so   "omx_lxvideosink", "lxvideosink" src/lxvideosink.c
#     <gst_plugin_dir>/libgstaudiodec-1.0.so      "audio_dec"                      src/audiodec.c
#     <gst_plugin_dir>/libgstaudiosink-1.0.so     "audio_sink"                     src/audiosink.c
#   mstar-m2, mstar-m3 (M2: GStreamer 1.2.4, GLib 2.40; M3: GStreamer 1.12.0, GLib 2.48;
#   "Implemented: MStar's media elements")
#     <gst_plugin_dir>/libgstomx.so               gstomx.conf's: "omxh264dec", ...,
#                                                 "omxaacdec", ..., "omxaudiosink" src/omx.c
#     <gst_plugin_dir>/libgstmvsink.so            "mvsink"                         src/mvsink.c
#     <gst_plugin_dir>/libgstmasink.so            "masink"                         src/masink.c
#   mstar-lm21a, mstar-lm21an, mstar-lm21ann (W21A: GStreamer 1.16.2, W22A, W23A: 1.18, all held to
#   GLib 2.58; MediaTek's builds of the same three: MSTAR_REL 21, 22, 23)
#     as mstar-m3, and gstomx.conf's "omxaudiopassthroughsink", which LM21's audio decoders feed;
#     W22A and W23A have no masink (none staged there). Their registry is frozen as O22's.
#   rtk-k5lp, rtk-k6lp, rtk-k7lp (W19P: GStreamer 1.14.1, GLib 2.48; W20L: 1.14.4, GLib 2.58;
#   W21P: 1.16.2, GLib 2.58; "Implemented: Realtek's media elements")
#     <gst_plugin_dir>/libgstomx.so               gstomx.conf's: "omxh264dec", ..., "omxaacdec", ...
#                                                                                  src/rtkomx.c
#     <gst_plugin_dir>/libgstdvo.so               "dvovideosink"                   src/dvosink.c
#     <gst_plugin_dir>/libgstrtkalsa.so           "rtkalsasink"                    src/rtkalsa.c
#     <gst_plugin_dir>/libgstrvscale.so           "rvscale" (where the firmware has it: K8)
#                                                                                  src/rvscale.c
# The sinks of lx and mstar share essink.c, each plugin with its own copy of its abstract type.
# gst_plugin_dir is the chip's: mnt/lg/res/lglib/gstreamer-1.0 on H13, usr/lib/gstreamer-1.0 on
# the others. The files keep the stock names and directory (on the rootfs, not in the
# tvservice pak), so the overlay replaces them as a firmware update would; GStreamer notices the
# changed size and mtime against its registry cache and rescans just these files.
#
# The firmware's GStreamer headers come from the pinned upstream tarball (subprojects/gstreamer.wrap,
# gstreamer18.wrap, gstreamer112.wrap, gstreamer1141.wrap, ...), for compiling only; GLib headers from the SDK's sysroot (its pkg-config), held
# to the firmware's GLib API. The plugins link the firmware's libraries, with -z defs: a GLib or
# GStreamer symbol newer than the firmware's fails here, not at dlopen.
gst_set = {
  'vdecsink': {'wrap': 'gstreamer', 'glib': '2_34', 'args': [], 'plugins': {
    'gstlxvdecbin-1.0': ['vdecsink.c', 'esdec.c', 'streaminfo.c', 'mediaport.c'],
    'gstadecsink-1.0': ['adecsink.c', 'esdec.c', 'mediaport.c']}},
  'lx': {'wrap': 'gstreamer18', 'glib': '2_40', 'args': [], 'plugins': {
    'gstlxvideodec-1.0': ['lxvideodec.c', 'esdec.c', 'streaminfo.c', 'mediaport.c'],
    'gstlxvideosink-1.0': ['lxvideosink.c', 'essink.c', 'esdec.c', 'streaminfo.c', 'mediaport.c'],
    'gstaudiodec-1.0': ['audiodec.c', 'esdec.c', 'mediaport.c'],
    'gstaudiosink-1.0': ['audiosink.c', 'essink.c', 'esdec.c', 'streaminfo.c', 'mediaport.c']}},
}
mstar_plugins = {
  'gstomx': ['omx.c', 'omxconf.c', 'essink.c', 'esdec.c', 'streaminfo.c', 'mediaport.c'],
  'gstmvsink': ['mvsink.c', 'essink.c', 'esdec.c', 'streaminfo.c', 'mediaport.c'],
  'gstmasink': ['masink.c', 'essink.c', 'esdec.c', 'streaminfo.c', 'mediaport.c']}
gst_set += {
  'mstar-m2': {'wrap': 'gstreamer', 'glib': '2_40', 'args': ['-DMSTAR_REL=16'], 'plugins': mstar_plugins},
  'mstar-m3': {'wrap': 'gstreamer112', 'glib': '2_48', 'args': ['-DMSTAR_REL=18'], 'plugins': mstar_plugins},
  # M2R (W17R): MStar's elements over W17R's GStreamer 1.8.0, GLib 2.40
  'mstar-m2r': {'wrap': 'gstreamer18', 'glib': '2_40', 'args': ['-DMSTAR_REL=16'], 'plugins': mstar_plugins},
  # M3R (W19R): M3's elements over W19R's GStreamer 1.14.1, the same GLib 2.48
  'mstar-m3r': {'wrap': 'gstreamer1141', 'glib': '2_48', 'args': ['-DMSTAR_REL=18'], 'plugins': mstar_plugins},
  # LM15U (W15U): W16R's elements over W15U's GLib 2.34; no masink, as the stock libgstmasink.so
  # fails to load there (it imports msil_audio_ErrorString, which no W15U library defines)
  'mstar-lm15u': {'wrap': 'gstreamer', 'glib': '2_34', 'args': ['-DMSTAR_REL=15'],
                  'plugins': {'gstomx': mstar_plugins['gstomx'], 'gstmvsink': mstar_plugins['gstmvsink']}},
}
rtk_plugins = {
  'gstomx': ['rtkomx.c', 'omxconf.c', 'esdec.c', 'streaminfo.c', 'mediaport.c'],
  'gstdvo': ['dvosink.c', 'essink.c', 'esdec.c', 'streaminfo.c', 'mediaport.c'],
  'gstrtkalsa': ['rtkalsa.c', 'essink.c', 'esdec.c', 'streaminfo.c', 'mediaport.c']}
gst_set += {
  'rtk-k5lp': {'wrap': 'gstreamer1141', 'glib': '2_48', 'args': ['-DRTK_REL=19'], 'plugins': rtk_plugins},
  'rtk-k6lp': {'wrap': 'gstreamer1144', 'glib': '2_58', 'args': ['-DRTK_REL=20'], 'plugins': rtk_plugins},
  'rtk-k7lp': {'wrap': 'gstreamer1162', 'glib': '2_58', 'args': ['-DRTK_REL=21'], 'plugins': rtk_plugins},
}
#   lx-o22, lx-o22n, lx-o22n2, lx-o22-ponytail, lx-o22-queue (W22O mullet and number1, W23O, W24G,
#   W22O ponytail and queue: GStreamer 1.18 to 1.24, held to GLib 2.58; clear H.264, HEVC, MPEG-2,
#   VP9 and AAC); lx-o22-ponytail-dts, lx-o22-queue-dts (the other chips' ponytail and queue)
#     <gst_plugin_dir>/libgstlxvideodec.so       "omx_lxvideodec", "lxvideodec"   src/lxvideodec.c
#     <gst_plugin_dir>/libgstlxvideosink.so      "omx_lxvideosink", "lxvideosink" src/lxvideosink.c
#     <gst_plugin_dir>/libgstlgaudiodec.so       "aac_audiodec"                   src/audiodec.c
#     <gst_plugin_dir>/libgstlgaudiosink.so      "audiosink"                      src/audiosink.c
# The modern LX names are the same on all; LX_REL (22, 23, 24, 25, 26) carries what each
# firmware's own elements add (gst-inspect of each). Compiled
# against the 1.16 baseline, linked to the firmware's 1.18. Their media stack reads a frozen
# registry (81-ums.conf: GST_REGISTRY_UPDATE=no), so the layer lets GStreamer rescan it.
modern_lx = {
  'gstlxvideodec': ['lxvideodec.c', 'esdec.c', 'streaminfo.c', 'mediaport.c'],
  'gstlxvideosink': ['lxvideosink.c', 'essink.c', 'esdec.c', 'streaminfo.c', 'mediaport.c'],
  'gstlgaudiodec': ['audiodec.c', 'esdec.c', 'mediaport.c'],
  'gstlgaudiosink': ['audiosink.c', 'essink.c', 'esdec.c', 'streaminfo.c', 'mediaport.c']}
modern_lx_files = [['90-emulator-media.conf', 'etc/systemd/system.conf.d/90-emulator-media.conf']]
gst_set += {
  'lx-o22': {'wrap': 'gstreamer1162', 'glib': '2_58', 'args': ['-DO22N2_MEDIA', '-DLX_REL=22'],
             'plugins': modern_lx, 'files': modern_lx_files},
  'lx-o22n': {'wrap': 'gstreamer1162', 'glib': '2_58', 'args': ['-DO22N2_MEDIA', '-DLX_REL=23'],
              'plugins': modern_lx, 'files': modern_lx_files},
  'lx-o22n2': {'wrap': 'gstreamer1162', 'glib': '2_58', 'args': ['-DO22N2_MEDIA', '-DLX_REL=24'],
               'plugins': modern_lx, 'files': modern_lx_files},
  'lx-o22-ponytail': {'wrap': 'gstreamer1162', 'glib': '2_58', 'args': ['-DO22N2_MEDIA', '-DLX_REL=25'],
                      'plugins': modern_lx, 'files': modern_lx_files},
  'lx-o22-queue': {'wrap': 'gstreamer1162', 'glib': '2_58', 'args': ['-DO22N2_MEDIA', '-DLX_REL=26'],
                   'plugins': modern_lx, 'files': modern_lx_files},
  # W22O's ponytail and queue audio builds with DTS kept (LX_DTS: dts-seamless, dts_audiodec), as
  # W23O's, W24G's and W24O's of those releases are (gst-inspect of W24G 33.31 and 43.21, W23O 44.00)
  'lx-o22-ponytail-dts': {'wrap': 'gstreamer1162', 'glib': '2_58',
                          'args': ['-DO22N2_MEDIA', '-DLX_REL=25', '-DLX_DTS'],
                          'plugins': modern_lx, 'files': modern_lx_files},
  'lx-o22-queue-dts': {'wrap': 'gstreamer1162', 'glib': '2_58',
                       'args': ['-DO22N2_MEDIA', '-DLX_REL=26', '-DLX_DTS'],
                       'plugins': modern_lx, 'files': modern_lx_files},
}
mstar_lm21n_plugins = {'gstomx': mstar_plugins['gstomx'], 'gstmvsink': mstar_plugins['gstmvsink']}
gst_set += {
  'mstar-lm21a': {'wrap': 'gstreamer1162', 'glib': '2_58', 'args': ['-DMSTAR_REL=21'],
                  'plugins': mstar_plugins, 'files': modern_lx_files},
  # N21D (lm21u): W21A's elements, with its analog omxaudiosink also taking the passthrough caps
  # (omx.c, MSTAR_N21D)
  'mstar-lm21u': {'wrap': 'gstreamer1162', 'glib': '2_58', 'args': ['-DMSTAR_REL=21', '-DMSTAR_N21D'],
                  'plugins': mstar_plugins, 'files': modern_lx_files},
  'mstar-lm21an': {'wrap': 'gstreamer1162', 'glib': '2_58', 'args': ['-DMSTAR_REL=22'],
                   'plugins': mstar_lm21n_plugins, 'files': modern_lx_files},
  'mstar-lm21ann': {'wrap': 'gstreamer1162', 'glib': '2_58', 'args': ['-DMSTAR_REL=23'],
                    'plugins': mstar_lm21n_plugins, 'files': modern_lx_files},
}
gst_set = gst_set[get_variable('gst_elements', 'vdecsink')]
gst_headers = custom_target('gst-headers',
  output: 'gst-headers.h',                 # a stamp; a header, so compiles wait for it
  depends: sources[gst_set['wrap']]['stamp'],
  depend_files: files('gst-headers.sh'),
  env: {'GLIB_MKENUMS': find_program('glib-mkenums', native: true).full_path()},
  command: [files('gst-headers.sh'), sources[gst_set['wrap']]['dir'],
            meson.current_build_dir() / 'gst-include', '@OUTPUT@'])
glib_headers = dependency('gobject-2.0').partial_dependency(compile_args: true)
video_args = ['-Wno-unused-parameter', '-Wno-deprecated-declarations', '-Wno-missing-field-initializers',
  '-I' + meson.current_build_dir() / 'gst-include/gstreamer-1.0',
  '-DGLIB_VERSION_MIN_REQUIRED=GLIB_VERSION_' + gst_set['glib'],
  '-DGLIB_VERSION_MAX_ALLOWED=GLIB_VERSION_' + gst_set['glib'],
  '-DGST_DISABLE_DEPRECATED']
video_args += gst_set['args']
# The firmware's GStreamer and GLib: their stubs (fw_stubs, guest/targets/meson.build).
video_link = ['-Wl,-z,defs', '-Wl,--allow-shlib-undefined', '-Wl,--as-needed']
video_link_depends = []
foreach lib : ['libgstaudio-1.0.so.0', 'libgstvideo-1.0.so.0', 'libgstbase-1.0.so.0', 'libgstreamer-1.0.so.0',
               'libgobject-2.0.so.0', 'libglib-2.0.so.0']
  fw_questions += 'lib usr/lib/' + lib
  if 'usr/lib/' + lib in fw_stubs
    video_link += fw_stubs['usr/lib/' + lib].full_path()
    video_link_depends += fw_stubs['usr/lib/' + lib]
  endif
endforeach
# Plugins are loaded with dlopen: modules, no SONAME (as GStreamer's own). In the soc layer
# (../meson.build stages them).
video_layer_args = []
foreach name, srcs : gst_set['plugins']
  paths = []
  foreach f : srcs
    paths += 'src' / f
  endforeach
  plugin = shared_module(name, files(paths), gst_headers,
    c_args: video_args + ['-DESSINK_TYPE_NAME="EsSink-' + name.replace('.', '_') + '"'], link_args: video_link,
    link_depends: video_link_depends, dependencies: glib_headers)
  video_layer_args += ['--file', plugin, gst_plugin_dir / 'lib' + name + '.so', '755']
  # Realtek's gst-omx: the factories and ranks of the stock registration, read by the firmware's
  # GStreamer under qemu-arm (tests/integration/layers/test_omx_registration.py; skipped without it)
  if srcs.contains('rtkomx.c')
    guest_test_artefacts += {'omx_plugin': plugin.full_path()}
    guest_test_depends += plugin
  endif
endforeach
# K8's Realtek rvscale (W22H: the camera player's PTZ pipeline; src/rvscale.c), where the
# firmware has the stock plugin: its instance init asserts in se_open without the SoC's scaler.
if gst_set['plugins'] == rtk_plugins
  fw_questions += 'path ' + gst_plugin_dir / 'libgstrvscale.so'
endif
if gst_set['plugins'] == rtk_plugins and fw_path.get(gst_plugin_dir / 'libgstrvscale.so', false)
  plugin = shared_module('gstrvscale', files('src/rvscale.c'), gst_headers,
    c_args: video_args, link_args: video_link, link_depends: video_link_depends, dependencies: glib_headers)
  video_layer_args += ['--file', plugin, gst_plugin_dir / 'libgstrvscale.so', '755']
endif
foreach f : gst_set.get('files', [])
  video_layer_args += ['--file', files(f[0]), f[1], '644']
endforeach
