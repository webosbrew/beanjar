# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The whole build, in two Meson build directories of this tree (docs/dev/guides/build.md):
#
#   build/guest-<chip>-<release>
#                cross (meson/cross/webos-sdk.ini), one per firmware target (-Dsoc, -Dwebos): everything that runs in the guest (32-bit ARM stubs,
#                layers, SSH, Mesa, test programs) and the arm64 kernel, busybox and initramfs,
#                which are built by their own Kbuild/make. These arm64 pieces take nothing of
#                the target (no soc, webos, firmware or glibc_max): they are built once in
#                shared_dir (build/kernel/<key>, build/busybox/<key>; meson/shared.py), as are
#                SSH and the Mesa stack, every target's build copies them from there, and
#                publishes them to the same build/Image, build/initramfs.cpio.gz and build/lgemu.ko
#   build/host   native (x86_64 workstation or aarch64 Pi): the viewer, the VM manager's GUI
#                layouts, the firmware cache, the network helpers, the unit tests, and the
#                prebuilt QEMU, FFmpeg and neatvnc (packaging/vendor.toml) published beside them
#   build/host-arm64
#                the same programs cross-built on the PC for arm64 (meson/cross/debian-arm64.ini)
#                against Debian's arm64 libraries, for the arm64 packages; its tests are built,
#                not run
#
# Each module has its own meson.build; this file only sets up what they share. Each build ends
# by publishing its outputs to publish_dir (default build/), where the launchers use them.
project('beanjar', 'c',
  # The base X.Y.Z; the full source version (X.Y.Z+gitN.g<hash>) is in the summary and the stamps.
  version: files('packaging/VERSION'),
  meson_version: '>= 1.7',
  default_options: [
    'optimization=2', 'debug=false',
    'warning_level=2', 'werror=true',
    # As the firmware's own binaries: every library named on the link line is NEEDED, and shared
    # libraries may leave symbols to the process (the firmware's libraries resolve at run time).
    'b_asneeded=false', 'b_lundef=false',
    'c_std=gnu99',
  ])

fs = import('fs')
top = meson.project_source_root()

# The lane: a cross build for 32-bit ARM is a guest build; a native build, or a cross build for
# aarch64 (meson/cross/debian-arm64.ini: build/host-arm64, the arm64 packages built on the PC), is
# the host build. host_cross: the latter, whose programs are not run here.
host_cross = meson.is_cross_build() and host_machine.cpu_family() == 'aarch64'
guest_lane = meson.is_cross_build() and not host_cross

publish_dir = get_option('publish_dir')
if publish_dir == '' and host_cross
  publish_dir = top / 'build/publish-arm64'   # the arm64 host build: never over this machine's own
elif publish_dir == ''
  publish_dir = top / 'build'
endif
# The firmware: what the host build's firmware cache converts (host/scripts/meson.build), -Dfirmware
# or else FW's (the default firmware without it). The guest build reads none: what it asks of its
# target's firmware is in the tree (guest/targets/meson.build).
firmware = ''
if not guest_lane
  firmware = get_option('firmware')
  if firmware == ''
    firmware = run_command(files('host/scripts/vm-launcher'), 'config', 'FW', check: true).stdout().strip()
  endif
endif

# The build's scripts (python, stage_py, publish_py, layer_script_py, ...) and the unpacked source
# trees of subprojects/*.wrap (sources[name] = {'dir': tree, 'stamp': custom target}).
subdir('meson')

# The build's identity (packaging/identity.py), which each lane publishes beside its artefacts.
identity_py = files('packaging/identity.py')
# The full source version, which the packages and the stamps carry (identity.py resolve). It counts
# commits since packaging/VERSION last changed, so a public clone counts its own commits.
_identity = run_command(python, '-c',
  'import sys; sys.path.insert(0, sys.argv[1]); import identity; print(identity.resolve(sys.argv[2])["version"])',
  top / 'packaging', top, check: false)
summary('version', _identity.returncode() == 0 ? _identity.stdout().strip() : meson.project_version(),
        section: 'Source')

# What publish_dir receives: [kind ('file', 'tree'), source path, relative path]
published = []
publish_depends = []

if guest_lane
  # ---------------------------------------------------------------- guest ----------------------
  if host_machine.cpu_family() != 'arm'
    error('the guest build is 32-bit ARM: meson setup --cross-file meson/cross/webos-sdk.ini ' +
          '(an arm64 host build: --cross-file meson/cross/debian-arm64.ini)')
  endif
  cc = meson.get_compiler('c')
  strip = find_program('strip')
  readelf = find_program('readelf')
  # The firmware target this build is for: the SoC (guest/layers/soc/<soc>) and the webOS release
  # family, the codename (guest/layers/webos/<webos>), both given (scripts/build.sh gives FW's target,
  # host/scripts/vm-launcher config TARGET).
  soc = get_option('soc')
  webos = get_option('webos')
  if '' in [soc, webos]
    error('the guest build is of one firmware target: give -Dsoc and -Dwebos (docs/dev/guides/build.md)')
  endif
  # LG's SimpleSmart line (webos 'simplesmart', docs/dev/architecture/simplesmart.md) is not webOS: its one
  # always-used layer is guest/layers/simplesmart, beside the arm64 pieces every target builds the same.
  simplesmart = webos == 'simplesmart'
  # LG's NetCast 4.5 (webos 'netcast45', docs/dev/architecture/netcast45.md): its one always-used layer is
  # guest/layers/netcast45, built with the NetCast SDK (meson/cross/lgtv-sdk.ini); it links nothing of
  # the firmware.
  netcast45 = webos == 'netcast45'
  # The open test guest (webos 'testguest', docs/dev/architecture/test-guest.md): its userland is
  # beanjar-vendor's, so this build makes its one layer, the checks of guest/testguest built against
  # the userland's sysroot, and the arm64 pieces every target shares, with Debian's armel cross
  # compiler on that sysroot as the 32-bit one (build/testguest/cross.ini, scripts/testguest-setup):
  # CI's way to a bootable guest without LG's SDK.
  testguest = webos == 'testguest'
  guest_target = soc + '-' + webos
  # What the shim rules and links ask of the target's firmware (guest/targets/meson.build): fw_path
  # (a rootfs path: whether the firmware's rootfs image has it), fw_version, fw_stubs (a firmware
  # library: its link-time stub), fw_defines (whether a stock library defines a symbol), from guest/targets/facts/<target>.txt. Each rule adds its question
  # to fw_questions; an unanswered one stops the setup once every module is read.
  subdir('guest/targets')
  # What the shim rules decide by (guest/layers/soc/meson.build, docs/dev/architecture/build-and-boot.md
  # "Choosing the shims"). webos_reached: the webOS releases up to and including this one, in LG's
  # order (targets.WEBOS_ORDER), so a rule only a release can decide is "'queue' in webos_reached" and
  # later releases inherit it.
  webos_reached = []
  if not simplesmart and not netcast45 and not testguest
    foreach r : run_command(files('host/scripts/vm-launcher'), 'config', 'RELEASES',
                            check: true).stdout().strip().split('\n')
      if webos not in webos_reached
        webos_reached += r
      endif
    endforeach
    if webos_reached[-1] != webos
      error('webOS release ' + webos + ' is not in targets.WEBOS_ORDER')
    endif
  endif
  # A chip's or release's directory may name its base, another of the same axis, in a file 'base'
  # (docs/dev/guides/build.md, "Layer bases"): the base's meson.build is read first (its table, its rootfs/
  # and files in the layer), then the directory's own, which set what differs and add or replace
  # files. soc_dirs and webos_dirs: the chain, the base first.
  axis_dirs = {}
  foreach axis, leaf : (simplesmart or netcast45 or testguest) ? {} : {'soc': soc, 'webos': webos}
    chain = ['guest/layers' / axis / leaf]
    foreach i : range(8)
      if fs.is_file(top / chain[0] / 'base')
        chain = ['guest/layers' / axis / fs.read(top / chain[0] / 'base').strip()] + chain
      endif
    endforeach
    foreach d : chain
      if not fs.is_file(top / d / 'meson.build')
        error(d + ' does not exist: ' + soc + '-' + webos + ' is not supported yet')
      endif
    endforeach
    axis_dirs += {axis: chain}
  endforeach
  soc_dirs = axis_dirs.get('soc', [])
  webos_dirs = axis_dirs.get('webos', [])
  if fs.name(meson.project_build_root()) != 'guest-' + guest_target
    warning('the build directory of ' + guest_target + ' is build/guest-' + guest_target + ' (docs/dev/guides/build.md)')
  endif
  # Meson defines _FILE_OFFSET_BITS=64 for every C file. Guest code shares structures with the
  # firmware's libraries, which are built with the 32-bit off_t glibc 2.12 defaults.
  add_project_arguments('-U_FILE_OFFSET_BITS', language: 'c')
  # The release's glibc: no guest ELF may need a newer symbol.
  glibc_max = {'afro': '2.14.1', 'beehive': '2.19', 'dreadlocks': '2.19', 'dreadlocks2': '2.19',
               'goldilocks': '2.24', 'goldilocks2': '2.24', 'jhericurl': '2.28',
               'kisscurl': '2.28', 'mullet': '2.31', 'number1': '2.31', 'ombre': '2.35',
               'ponytail': '2.35', 'queue': '2.39', 'spacebuns': '2.39', 'simplesmart': '2.19', 'netcast45': '2.16',
               'testguest': '2.42'}[webos]
  # stage.py for 32-bit ARM trees: strip, and check the glibc ceiling.
  arm_stage = [python, stage_py, '--strip', strip, '--readelf', readelf, '--glibc-max', glibc_max]
  layers_dir = meson.project_build_root() / 'stage/layers'   # the assembled layer trees
  layers = {}                      # name -> custom target of <layers_dir>/<name>
  # name -> its layer.toml (host/core/beanjar/layermeta.py), for a layer whose source is not
  # guest/layers/<name>; published beside the layer's component
  layer_meta = {}
  # name -> the version plain <name> means, for a layer built once per version as <name>@<version>
  # (guest/layers/hbchannel); published as build/components/<name>/versions/default
  layer_defaults = {}
  # Test programs the test scripts run in the guest: published path -> target (tests/*,
  # guest/layers/gl/test). build/tcg-perf, ... also hold the tests' results, so they
  # are published file by file.
  guest_programs = {}
  # What this lane's integration tests read (tests/integration/layers; the `artefact` fixture of
  # tests/conftest.py), by name, and the targets that make them
  guest_test_artefacts = {'target': guest_target, 'build_root': meson.project_build_root()}
  guest_test_depends = []

  soc_layer_trees = []              # the chain's rootfs/ trees, stage.py ops (guest/layers/soc/<chip>)
  webos_layer_args = []             # the chain's rootfs/ trees and files (guest/layers/webos/<release>)
  # NetCast 4.5's layer takes Mesa from the gl layer's stack (guest/layers/gl/mesa), without Wayland
  if netcast45
    mesa_platforms = ''
    wayland_server_compat = false
    # The NetCast SDK's glibc carries 3.0 kernel headers, without SYS_kcmp, and Mesa's
    # os_same_file_description has no complete path without it: the ARM EABI number of the
    # syscall (the uapi headers' __NR_kcmp, 378), which the guest kernel has.
    mesa_extra_c_args = ['-DSYS_kcmp=378']
    # Mesa's libEGL as libEGLmesa.so.1 (its own soname), which guest/layers/netcast45's libEGL loads:
    # a dlopen of libEGL.so.1 must never find Mesa's by its soname
    mesa_extra_args = ['-Degl-lib-suffix=mesa']
    # libdrm inside Mesa's libraries: chrome finds its own old libdrm.so.2 (/webbrowser/lib) first
    drm_extra_args = ['--default-library=static']
    mesa_extra_link_args = ['-Wl,--exclude-libs,libdrm.a']       # and not exported from them
  endif
  guest_modules = (['guest/tools/ssh', 'guest/helpers/lls'] +
      soc_dirs +                   # the chip's table (hal_stock, soc_phys_base, ...) for these:
      ['guest/layers/webos/src/pmlogd'] +  # libpmlog-keep-all, which the preload releases' webos layers take
      ['guest/layers/webos/src/bootd-scale'] +  # libbootd-scale, in every webos layer
      ['guest/layers/webos/src/memchute-scale'] +  # libmemchute-scale, in every webos layer
      webos_dirs + [               # the release's jobs and fstabs, and its table (nvm_*, ...)
      'guest/layers/webos',              # the webos layer of webos_layer_args
      'guest/layers/common',             # what every firmware gets; hal_common, which the soc layer links
      'guest/layers/soc',                # the HAL and DILE shims, media elements, board.cmdline
      'guest/layers/gl',                 # + guest/layers/gl/mesa: libffi, libdrm, Wayland, Mesa
      'guest/layers/headless',           # the headless layer, built per target
      'guest/layers/rootssh', 'guest/layers/devmode', 'guest/layers/hbchannel',
      'guest/kernel',
      'guest/initramfs',                 # the same for every target: no layers in it; busybox, src/
      'tests/regress/tcg', 'tests/regress/devmode', 'tests/regress/virgl', 'tests/regress/gm', 'tests/regress/tee', 'tests/regress/smp', 'guest/layers/gl/test',
    ])
  if simplesmart
    guest_modules = ['guest/layers/simplesmart', 'guest/kernel', 'guest/initramfs']
  elif netcast45
    guest_modules = ['guest/layers/gl/mesa', 'guest/layers/netcast45', 'guest/kernel', 'guest/initramfs']
  elif testguest
    guest_modules = ['guest/testguest', 'guest/kernel', 'guest/initramfs']
  endif
  foreach module : guest_modules
    if module in ['guest/layers/gl/test', 'tests/regress/virgl'] and not gl_built
      continue                     # against the gl layer's Mesa, which this target has not
    endif
    subdir(module)
  endforeach
  # Every question the modules asked, for host/scripts/vm-launcher firmware-facts --build; one the
  # facts file does not answer stops the setup here.
  run_command(fwfacts, 'questions', meson.project_build_root() / 'firmware-questions.txt',
              fw_questions, env: fwfacts_env, check: true)
  fw_unanswered = []
  foreach q : fw_questions
    w = q.split(' ')
    if w[1] not in {'path': fw_path, 'version': fw_version, 'lib': fw_stubs, 'defines': fw_defines}[w[0]] and q not in fw_unanswered
      fw_unanswered += q
    endif
  endforeach
  if fw_unanswered.length() > 0 or not fs.is_file(fw_facts)
    error('guest/targets/facts/' + guest_target + '.txt is missing or does not answer:\n  ' +
          '\n  '.join(fw_unanswered) + '\nhost/scripts/vm-launcher firmware-facts ' + guest_target +
          ' --build ' + meson.project_build_root() + ' writes it from the target\'s firmware ' +
          '(docs/dev/guides/build.md, "Firmware facts")')
  endif
  # The guest build's integration tests (docs/dev/guides/testing.md): those of tests/integration/layers
  # whose artefacts this target has; the others skip.
  configure_file(output: 'artefacts.json', configuration: guest_test_artefacts, output_format: 'json')
  test('integration-layers', python,
    args: ['-m', 'pytest', '-q', '--no-header', meson.current_source_dir() / 'tests/integration/layers'],
    env: {'PYTHONDONTWRITEBYTECODE': '1', 'BEANJAR_TEST_MESON': '1',
          'BEANJAR_TEST_ARTEFACTS': meson.current_build_dir() / 'artefacts.json'},
    depends: guest_test_depends, timeout: 180, suite: ['integration', 'layers'])
  component_args = []
  component_targets = []
  component_meta = []
  foreach name, target : layers
    component_args += ['--layer', name + '=' + layers_dir / name]
    component_targets += target
    meta = layer_meta.get(name, meson.current_source_dir() / 'guest/layers' / name / 'layer.toml')
    if fs.is_file(meta)
      component_args += ['--meta', name + '=' + meta]
      component_meta += meta
    endif
  endforeach
  foreach name, version : layer_defaults
    component_args += ['--default', name + '=' + version]
  endforeach
  foreach vendor : get_variable('gl_vendor_stubs', []) + [get_variable('gl_vendor_gl', '')]
    if vendor != ''
      component_args += ['--vendor', vendor]
    endif
  endforeach
  publish_depends += custom_target('guest-components',
    output: 'guest-components.stamp',
    command: [python, files('meson/components.py'), '--dest', publish_dir / 'components',
              '--soc', soc, '--release', webos, '--stamp', '@OUTPUT@'] + component_args,
    depends: component_targets, depend_files: component_meta, build_by_default: true)
  # The test programs, stripped and held to the firmware's glibc like the layers.
  tests_args = []
  foreach rel, exe : guest_programs
    tests_args += ['--file', exe, rel, '755']
    published += [['file', meson.current_build_dir() / 'stage/tests' / rel, rel]]
  endforeach
  publish_depends += custom_target('guest-tests',
    output: 'guest-tests.stamp',
    depfile: 'guest-tests.d',
    command: arm_stage + ['--out', meson.current_build_dir() / 'stage/tests', '--stamp', '@OUTPUT@',
                          '--depfile', '@DEPFILE@'] + tests_args,
    build_by_default: true)
  # The guest build lane's stamp (packaging/identity.py): the source version of what it published,
  # which the launcher checks and packaging puts in beanjar-guest
  publish_depends += custom_target('guest-stamp',
    output: 'guest-installation.toml',
    command: [python, identity_py, 'source-version', '--top', top, '--out', '@OUTPUT@'],
    build_always_stale: true, build_by_default: true)
  published += [['file', meson.project_build_root() / 'guest-installation.toml', 'guest-installation.toml']]
  summary({'soc': soc, 'webos': webos, 'glibc_max': glibc_max, 'components': publish_dir / 'components'},
    section: 'Firmware target')
else
  # ---------------------------------------------------------------- host -----------------------
  foreach module : [
      'host/viewer',
      'host/vnc',                  # beanjar-vnc, over the viewer's core
      'host/helpers/kvm',                 # opt-in per-VM halt polling on the Pi
      'host/helpers/media-helper',            # the VM's end of the media ports
      'host/helpers/virtcam',             # the virtual USB camera (beanjar io usb attach --camera)
      'host/ui',                   # the GTK windows: beanjar_ui and its layouts
      'host/scripts',              # the firmware cache
      'host/helpers/network',             # the port relay and the LAN helper
      'tests',                     # the unit and integration tiers (docs/dev/guides/testing.md)
    ]
    subdir(module)
  endforeach
  # The prebuilt programs (meson/meson.build), as they are: the launcher runs build/qemu, and
  # build/ffmpeg and build/neatvnc are the libraries the programs above find through their RUNPATH
  foreach program : vendor.keys()
    published += [['tree', vendor[program], program]]
  endforeach
  # The host build lane's source version (packaging/identity.py), which packaging reads
  publish_depends += custom_target('source-version',
    output: 'source-version.toml',
    command: [python, identity_py, 'source-version', '--top', top, '--out', '@OUTPUT@'],
    build_always_stale: true, build_by_default: true)
  published += [['file', meson.project_build_root() / 'source-version.toml', 'source-version.toml']]
endif

publish_args = []
foreach p : published
  publish_args += ['--' + p[0], p[1], p[2]]
endforeach
custom_target('publish',
  output: 'publish.state',
  depends: publish_depends,
  command: [python, publish_py, '--dest', publish_dir, '--state', '@OUTPUT@'] + publish_args,
  build_always_stale: true,
  build_by_default: true)
