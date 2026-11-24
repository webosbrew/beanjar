# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The webos layer of goldilocks, webOS 4.0-4.4 (W18R's 4.4.3 "goldilocks-gorongosa"; 4.5 and later
# are goldilocks2), build/layers/<chip>-goldilocks/webos, always used: how this release's LG
# userland is put together. W18R's jobs
# that dreadlocks overrides and that are the same here (mount-bsp's logic, lginit_app byte for
# byte, the state formatter's mkfs options) are dreadlocks' files, taken from
# guest/layers/webos/dreadlocks/rootfs one by one (4 of its 9: dreadlocks is not this release's base):
#   mount-bsp.override, lginit_app.override, emulator-format-state.conf, emulator-service-country.conf
# and this release's own, in rootfs/etc/init:
#   mount-readonly-volatile.d/00-partinfo.squashfs.fstab
#                                         the paks (no smartkey), which init mounts
#   tvservice.override                    the stock job's script (LD_PRELOAD liblibm and
#                                         libsystrim) with libtvservice-private.so after them
#   PmLogDaemon.override                  the stock job's script, $PMLOG_PRELOAD after its
#                                         libsystrim: PmLog mode "preload-env" (targets.py), with
#   usr/lib/libpmlog-keep-all.so          the PmLog preload (guest/layers/webos/src/pmlogd)
#   sam.override.in (not in rootfs/)      the stock job's script with the jail hooks after its
#                                         libsystrim, as sam_override_in for the common and gl
#                                         layers to fill in
#   emulator-pulseaudio-ptts.conf         system.pa's ptts sink on hw:0,7 as a null sink: the
#                                         virtio-sound card has hw:0,6 only
# The jailer's nodes are the chip's (guest/layers/soc/<chip>): W18R's jail_device.conf has a branch per
# SoC. goldilocks2 (guest/layers/webos/goldilocks2) names this directory its base.
# The gl layer takes wayland_server_compat from here: W18R's Wayland server is 1.11, a superset of
# 1.7's exports, so Mesa links it without the compat library.
wayland_server_compat = false
# libdile_gal's DILE_GAL_DEVICE_CAPABILITY_INFO_T: 76 bytes (W18R's gfx_GetDeviceCapability)
dile_gal_capability_bytes = 76
# sam's job exports LD_PRELOAD (libsystrim) in its script: the jail hooks' sam.override is that
# script with them after it (guest/layers/common/meson.build, jail_sam_in)
sam_override_in = files('sam.override.in')

goldilocks_shared = []
foreach f : ['mount-bsp.override', 'lginit_app.override', 'emulator-format-state.conf',
                      'emulator-service-country.conf']
  goldilocks_shared += ['--file', meson.current_source_dir() / '..' / 'dreadlocks' / 'rootfs' / 'etc' / 'init' / f,
                        'etc/init' / f, '644']
endforeach

webos_layer_args += ['--tree', meson.current_source_dir() / 'rootfs', '.',
  '--file', pmlog_keep_all, 'usr/lib/libpmlog-keep-all.so', '755'] + goldilocks_shared
