# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The initramfs's pointer bridge, touch bridge, IR pump, tuner pump, video window pump, sound pump, guest memory pump, shell agent, SimpleSmart's GOP presenter and the boot
# splash
# (README.md): static arm64 programs. The arm64 side of
# the guest is not Meson's cross machine (that is the 32-bit ARM userland), so each single-file
# program is compiled by one explicit command, with the same warnings, and a depfile.
arm64_cc = find_program(get_option('arm64_cross_compile') + 'gcc', native: true)
arm64_cflags = ['-static', '-Os', '-Wall', '-Wextra'] + (get_option('werror') ? ['-Werror'] : [])
virtio_bins = {}
foreach name : ['virtio-pointer', 'virtio-touch', 'virtio-ir', 'virtio-tuner', 'virtio-video', 'virtio-sound', 'virtio-meminfo', 'virtio-capture', 'virtio-shell', 'gop-present', 'splash', 'sestore-load', 'dm-linear']
  virtio_bins += {name: custom_target(name,
    input: name + '.c',
    output: name,
    depfile: name + '.d',
    command: [arm64_cc] + arm64_cflags + ['-MD', '-MF', '@DEPFILE@', '-o', '@OUTPUT@', '@INPUT@'])}
endforeach
# The same programs, static x86-64, for the x86-64 guest kernel's initramfs (x86_64_kernel).
if x86_64_kernel
  x86_64_cc = find_program('gcc', native: true)
  virtio_bins_x86_64 = {}
  foreach name, _bin : virtio_bins
    virtio_bins_x86_64 += {name: custom_target(name + '-x86_64',
      input: name + '.c',
      output: name + '-x86_64',
      depfile: name + '-x86_64.d',
      command: [x86_64_cc] + arm64_cflags + ['-MD', '-MF', '@DEPFILE@', '-o', '@OUTPUT@', '@INPUT@'])}
  endforeach
endif
