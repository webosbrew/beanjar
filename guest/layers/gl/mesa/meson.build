# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The gl layer's graphics stack: libffi 3.0.13 (also shipped for the Wayland ABI), libdrm, Wayland 1.26
# (+ wayland-protocols) and Mesa, each built by its own build system (mesa-stack.sh; docs/dev/guides/build.md
# says why they are not subprojects) into one /usr prefix, in two steps with libwayland-server-compat
# between them on webOS 1 and 2. They run again when a source tree (a wrap or its patches), the cross
# file below or the script changes; each inner build is incremental.
#
# The stack is built once per distinct configuration and shared by the targets that have it:
# <shared_dir>/mesa/<key>/{work,stage} (default build/mesa), where <key> hashes everything a target can differ in (the cross
# file's compilers and flags without its paths, the Wayland the firmware selects, the platforms and
# extra options, the server compat link) and nothing that names the target. What every target has alike
# (the source trees, mesa-stack.sh) is not in the key: a change to it reaches each shared build
# incrementally, as it did a per-target one. mesa-stack.sh locks the directory while it builds.
mesa_stack = files('mesa-stack.sh')

# firmware_wayland (the webos layer's table, guest/layers/webos/ombre): the release's own Wayland
# where it is new enough for Mesa (1.20 on). Mesa then compiles and links against a Wayland of that
# version (subprojects/wayland<major><minor>.wrap) and the layer ships no Wayland library: every
# process keeps the firmware's, and Mesa's libEGL uses only the client API it has, as LG's vendor EGL
# does. Chromium 120's and 132's libcbe.so bundle and export a Wayland copy of about that age, which
# then serves all of Mesa's calls; with 1.26's API, two calls went to another implementation and an
# EGL surface's event queue was made by one and freed by the other. Older firmware
# (libwayland-client.so.0.1.0 or 0.3.0: before 1.20) gets Wayland 1.26 (subprojects/wayland.wrap), shipped.
fw_wayland = get_variable('firmware_wayland', '')
fw_wl_src = ''
fw_wl_stamp = []
# The firmware's libwayland-client: the version suffix of the file its soname links to (fw_version,
# guest/targets/meson.build), 20.0 for 1.20; none without one. NetCast's Mesa has no Wayland.
fw_wl_have = 'none'
if not netcast45 and not netcast4
  fw_questions += 'version usr/lib/libwayland-client.so.0'
  fw_wl_have = fw_version.get('usr/lib/libwayland-client.so.0', 'none')
endif
if fw_wayland != ''
  fw_wl_ver = fw_wayland.split('.')
  fw_wl_wrap = 'wayland' + fw_wl_ver[0] + fw_wl_ver[1]
  if fw_wl_wrap not in sources
    error('firmware_wayland ' + fw_wayland + ': no subprojects/' + fw_wl_wrap + '.wrap')
  endif
  if fw_wl_have != 'none' and fw_wl_have != fw_wl_ver[1] + '.' + fw_wl_ver[2]
    error('firmware_wayland ' + fw_wayland + ', but the firmware\'s libwayland-client.so.0 is its .' + fw_wl_have)
  endif
  fw_wl_src = sources[fw_wl_wrap]['dir']
  fw_wl_stamp = [sources[fw_wl_wrap]['stamp']]
else
  foreach minor : range(18, 40)
    if fw_wl_have == '@0@.0'.format(minor)
      error('the firmware\'s libwayland-client is 1.@0@: set firmware_wayland in its webos layer'.format(minor))
    endif
  endforeach
endif

# The inner builds' cross file: this build's own compilers and flags (meson/cross/webos-sdk.ini),
# plus kernel uapi headers newer than the SDK's 3.10 ones for Mesa's drm/virgl code, and the stage
# as the only include, library and pkg-config directory (never the SDK's own wayland 1.11,
# libffi.so.8 or EGL). The link flags are the stage's, without the ISA flags, as they have been.
# @ROOT@ stands for the shared directory until its key is known.
add_languages('cpp', native: false)
cxx = meson.get_compiler('cpp')
uapi = get_option('uapi_headers')
if not fs.is_dir(uapi / 'drm')
  error('no kernel uapi headers in ' + uapi + ' (Debian: linux-libc-dev-armel-cross; -Duapi_headers=DIR)')
endif
ini = {}
foreach k, v : {
    'c': cc.cmd_array(), 'cpp': cxx.cmd_array(),
    'ar': [find_program('ar').full_path()], 'strip': [strip.full_path()],
    'readelf': [readelf.full_path()], 'objcopy': [find_program('objcopy').full_path()],
    'nm': [find_program('nm').full_path()],
    'c_args': get_option('c_args') + get_variable('mesa_extra_c_args', []) +
              ['-O2', '-isystem', '@ROOT@/work/uapi', '-I@ROOT@/stage/usr/include'],
    'cpp_args': get_option('cpp_args') + ['-O2', '-isystem', '@ROOT@/work/uapi', '-I@ROOT@/stage/usr/include'],
    'c_link_args': ['-L@ROOT@/stage/usr/lib', '-Wl,-rpath-link,@ROOT@/stage/usr/lib',
                    '-static-libgcc', '-Wl,--hash-style=both'] + get_variable('mesa_extra_link_args', []),
    'cpp_link_args': ['-L@ROOT@/stage/usr/lib', '-Wl,-rpath-link,@ROOT@/stage/usr/lib',
                      '-static-libgcc', '-static-libstdc++', '-Wl,--hash-style=both'] +
                     get_variable('mesa_extra_link_args', []),
  }
  ini += {k: '[\'' + '\', \''.join(v) + '\']'}
endforeach
ini += {
  'PKG_CONFIG': '@ROOT@/work/pkg-config',
  'SYSTEM': host_machine.system(), 'CPU_FAMILY': host_machine.cpu_family(),
  'CPU': host_machine.cpu(), 'ENDIAN': host_machine.endian(),
}

mesa_env = {
  'MESON': meson_cmd.full_path(),
  'UAPI': uapi,
  # libffi's configure and the relink of libwayland-client
  'GUEST_CC': ' '.join(cc.cmd_array()),
  'ARM_FLAGS': ' '.join(get_option('c_args')),
  'FFI_SRC': sources['libffi']['dir'], 'DRM_SRC': sources['libdrm']['dir'],
  'WL_SRC': sources['wayland']['dir'], 'WLP_SRC': sources['wayland-protocols']['dir'],
  'FW_WL_SRC': fw_wl_src,
  'MESA_SRC': sources['mesa']['dir'],
  # the EGL window systems: Wayland, but none for NetCast 4.5 (its window system is our libEGL's,
  # guest/layers/netcast45, over Mesa's surfaceless platform)
  'MESA_PLATFORMS': get_variable('mesa_platforms', 'wayland'),
  'MESA_EXTRA_ARGS': ' '.join(get_variable('mesa_extra_args', [])),
  'DRM_EXTRA_ARGS': ' '.join(get_variable('drm_extra_args', [])),
}

# The key: the cross file's values and the environment above, and whether Mesa links
# libwayland-server-compat (built below from one source with this cross file, so the same for
# every target that has it).
mesa_key_lines = ['wayland_server_compat=@0@'.format(wayland_server_compat)]
foreach k, v : ini + mesa_env
  mesa_key_lines += k + '=' + v
endforeach
mesa_key = run_command(python, shared_py, 'key', mesa_key_lines, check: true).stdout().strip()
mesa_root = shared_dir / 'mesa' / mesa_key
mesa_stage = mesa_root / 'stage'
mesa_work = mesa_root / 'work'
cross_conf = {}
foreach k, v : ini
  cross_conf += {k: v.replace('@ROOT@', mesa_root)}
endforeach
mesa_cross = configure_file(input: 'cross.ini.in', output: 'cross.ini', configuration: cross_conf)
mesa_env += {'CROSS': mesa_cross.full_path()}

mesa_deps = custom_target('mesa-deps',
  output: 'mesa-deps.stamp',
  depfile: 'mesa-deps.stamp.d',
  depends: [sources['libffi']['stamp'], sources['libdrm']['stamp'], sources['wayland']['stamp'],
            sources['wayland-protocols']['stamp']] + fw_wl_stamp,
  depend_files: [mesa_stack, mesa_cross],
  env: mesa_env,
  command: [mesa_stack, 'deps', mesa_work, mesa_stage, '@OUTPUT@', '@DEPFILE@'],
  console: true)

# On webOS 1 and 2 (wayland_server_compat): the seven libwayland-server >= 1.2 functions Mesa's
# EGL_WL_bind_wayland_display needs, on top of the firmware's libwayland-server 1.0.3 or 1.1
# (../src/wayland-server-compat.c; it uses no 1.26 API). mesa-stack.sh puts it ahead of
# libwayland-server on Mesa's link line; the layer ships it. webOS 3's 1.7 has them itself.
mesa_compat_args = []
mesa_compat_deps = []
if wayland_server_compat
  wl_compat = shared_library('wayland-server-compat', '../src/wayland-server-compat.c',
    soversion: '0',
    c_args: ['-fvisibility=hidden'],
    link_args: ['-Wl,--hash-style=both', mesa_stage / 'usr/lib/libwayland-server.so', '-lpthread'],
    link_depends: mesa_deps)
  mesa_compat_args = [wl_compat]
  mesa_compat_deps = [wl_compat]
endif

mesa = custom_target('mesa',
  output: 'mesa-stage.h',                  # a stamp; a header, so compiles wait for it
  depfile: 'mesa-stage.h.d',
  depends: [mesa_deps, sources['mesa']['stamp']] + mesa_compat_deps,
  depend_files: [mesa_stack, mesa_cross],
  env: mesa_env,
  command: [mesa_stack, 'mesa', mesa_work, mesa_stage, '@OUTPUT@', '@DEPFILE@'] + mesa_compat_args,
  console: true,
  build_by_default: true)

# What the gl layer's own libraries and the GL test programs compile and link against.
mesa_lib = mesa_stage / 'usr/lib'
mesa_dep = declare_dependency(sources: mesa,
  compile_args: ['-I' + mesa_stage / 'usr/include', '-I' + mesa_stage / 'usr/include/libdrm',
                 '-Wno-unused-parameter'],
  link_args: ['-L' + mesa_lib, '-Wl,--rpath-link,' + mesa_lib])
