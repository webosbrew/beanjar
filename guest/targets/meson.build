# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# What the guest build of a target asks of its firmware, answered by facts/<chip>-<release>.txt beside
# the target's own file (host/core/beanjar/firmware/facts.py has the format; docs/dev/guides/build.md,
# "Firmware facts"). The build reads no firmware: the facts are generated from one and checked in.
#
#   fw_path      {rootfs path: bool}: whether the firmware's rootfs image has it (the shim rules)
#   fw_version   {rootfs path: version suffix}: of the file a library name resolves to, none without
#   fw_defines   {IMAGE:PATH:NAME: bool}: whether a stock library of the rootfs or bsppart image
#                defines a symbol (a rule's question about what a stock library reaches)
#   fw_exports   {PATH:NAME=VALUE: bool}: whether a shell file of the rootfs image exports NAME as
#                VALUE (a rule's question about what the firmware's environment selects)
#   fw_gbmformats {IMAGE:PATH: [fourcc]}: the formats the GPU driver library PATH of the rootfs or
#                bsppart image makes GBM window configs of, in its order ([] for none)
#   fw_stubs     {rootfs path: custom target}: a firmware library's link-time stub, with its SONAME
#                and the symbols the guest code uses of it, so a link against it records what a
#                link against the firmware's own file did
#   fw_questions what the modules asked ("KIND PATH"); each rule adds its own before reading the
#                answer (a missing one reads as no), and the top-level meson.build stops the setup
#                when the file does not answer one
#
# facts.py's own command line makes the stubs and writes the questions: fwfacts (the command) and
# fwfacts_env (its package on the module path; -P keeps the working directory off it).
fwfacts = [python, '-P', '-m', 'beanjar.firmware.facts']
fwfacts_env = {'PYTHONPATH': top / 'host/core'}
fw_facts = meson.current_source_dir() / 'facts' / guest_target + '.txt'
fw_path = {}
fw_version = {}
fw_lib = {}
fw_defines = {}
fw_exports = {}
fw_gbmformats = {}
fw_questions = []
if fs.is_file(fw_facts)
  foreach line : fs.read(fw_facts).split('\n')
    w = line.split(' ')
    if w[0] == 'path'
      fw_path += {w[1]: w[2] == 'yes'}
    elif w[0] == 'version'
      fw_version += {w[1]: w[2]}
    elif w[0] == 'lib'
      fw_lib += {w[1]: w[2]}
    elif w[0] == 'defines'
      fw_defines += {w[1]: w[2] == 'yes'}
    elif w[0] == 'exports'
      fw_exports += {w[1]: w[2] == 'yes'}
    elif w[0] == 'gbmformats'
      fw_gbmformats += {w[1]: w[2] == 'none' ? [] : w[2].split(',')}
    endif
  endforeach
endif
fw_stubs = {}
fw_stub_cc = meson.get_compiler('c').cmd_array() + get_option('c_args')
foreach lib, soname : fw_lib
  fw_stubs += {lib: custom_target('fwstub-' + fs.name(lib),
    input: fw_facts,
    output: fs.name(lib),
    command: fwfacts + ['stub', '@INPUT@', lib, '@OUTPUT@', '--'] + fw_stub_cc,
    env: fwfacts_env,
    depend_files: files('../../host/core/beanjar/firmware/facts.py'))}
endforeach
