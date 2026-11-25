# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The common layer, build/layers/<chip>-<release>/common, always used: what every firmware gets.
# It is compiled per build like the others, held to
# the build's glibc_max. Lowest of the three always-used layers (guest/initramfs/rootfs/init: webos, soc,
# common), so a soc or webos layer can replace one of its files.
#   hal-common/   hal_common, the static library the soc layer's shims and tvservice-private
#                 share (hal_log, the emulator's physical memory), and the shims' link-time
#                 stand-in; nothing of it is in the layer by itself
#   tvservice/    usr/lib/libtvservice-private.so: LG's tvservice's private view of the OS
#   rootfs/       etc/init/pulseaudio.override: LG's stock system.pa on the virtio-sound card, made
#                 hw:0,6 by virtio_snd.pcm_device_base=6 (guest/kernel/patches); the job stops itself
#                 without a card
#                 usr/libexec/beanjar/pmlog-debug: webos.pmlog_debug's contexts at debug level, run by
#                 etc/init/emulator-pmlog-debug.conf (Upstart) and etc/systemd/system/
#                 emulator-pmlog-debug.service (systemd, with libPmLogLib's dev mode on)
#                 usr/libexec/beanjar/emulator-mrcu-pairing: a generated paired Magic Remote's
#                 record for lginput2 where the state has none, run before it by
#                 etc/init/emulator-mrcu-pairing.conf and etc/systemd/system/
#                 emulator-mrcu-pairing.service (lginput2.service.wants)
#   src/          usr/lib/libjail-trust.so (jail-trust.c); usr/libexec/beanjar/pmlog-devmode
#                 (pmlog-devmode.c), PmLogSetDevMode(1) for pmlog-debug --devmode
#   jail/         the jail hooks' preload in sam, which starts LG's jailer: on systemd
#                 etc/systemd/system/sam.service.d/zz-beanjar-jail.conf, on Upstart
#                 etc/init/sam.override (an env stanza, or the release's sam_override_in where the
#                 stock script exports LD_PRELOAD itself: goldilocks); here with libjail-trust,
#                 the gl layer's copy (guest/layers/gl/meson.build) replaces it with libjail-gpu too
subdir('hal-common')
subdir('tvservice')

jail_trust = shared_library('jail-trust', 'src/jail-trust.c',
  link_args: ['-Wl,--hash-style=both', '-ldl'])
pmlog_devmode = executable('pmlog-devmode', 'src/pmlog-devmode.c', link_args: ['-ldl'])

# The jail hooks' preload in the job that runs sam, per init system (jail/): jail_sam(PRELOAD)
# is this release's file and where it goes; the gl layer makes its own with its hook in front.
if get_variable('init_system', 'upstart') == 'systemd'
  jail_sam_in = files('jail/zz-beanjar-jail.conf.in')
  jail_sam_path = 'etc/systemd/system/sam.service.d/zz-beanjar-jail.conf'
else
  jail_sam_in = get_variable('sam_override_in', files('jail/sam.override.in'))
  jail_sam_path = 'etc/init/sam.override'
endif
jail_sam_common = configure_file(input: jail_sam_in, output: 'jail-sam-common',
  configuration: {'JAIL_PRELOAD': '/usr/lib/libjail-trust.so'})

layers += {'common': custom_target('common-layer',
  output: 'common-layer.stamp',
  depfile: 'common-layer.d',
  command: arm_stage + ['--out', layers_dir / 'common', '--stamp', '@OUTPUT@', '--depfile', '@DEPFILE@',
    '--tree', meson.current_source_dir() / 'rootfs', '.',
    '--file', tvservice_private, 'usr/lib/libtvservice-private.so', '755',
    '--file', jail_trust, 'usr/lib/libjail-trust.so', '755',
    '--file', jail_sam_common, jail_sam_path, '644',
    '--file', pmlog_devmode, 'usr/libexec/beanjar/pmlog-devmode', '755'],
  build_by_default: true)}
