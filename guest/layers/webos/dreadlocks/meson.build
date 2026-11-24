# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The webos layer of dreadlocks, webOS 3.0-3.4 (e.g. W16R's 3.4.3 "dreadlocks-digya"; 3.5 and later are
# dreadlocks2, another release),
# build/layers/<chip>-dreadlocks/webos, always used: how this release's LG userland is put together.
# Derived from W16R's Upstart jobs (Upstart
# 1.10, one ls-hubd whose ls-hubd_public/_private jobs only emit their events, bootd with
# /etc/palm/bootd.json and its bootd-mode job; new jobs tvconfig-gen, configd, emmcd, nopd, swap-on).
# Only files so far, all under rootfs/etc/init:
#   mount-bsp.override                    the bsppart pivot, which init has done by stacking the
#                                         pak's bsp/ as a lower layer (targets.py, PAK_LOWERS)
#   emulator-format-state.conf            formats a blank state disk before mount-readwrite, with
#                                         this release's mkfs options (as webOS 2's)
#   emulator-service-country.conf         in Normal, the board's own smart service country (US, USA)
#                                         where the stock default is "other", as FirstUse's Next writes it
#   mount-readonly-volatile.d/00-partinfo.squashfs.fstab
#                                         the paks, which init mounts (license at /mnt/license)
#   lginit_app.override                   as webOS 2's
#   emulator-jail-nodes.conf              the nodes of the jailer's "m2" branch
#   emulator-pmlog-devlog.conf            PmLog mode "devlog" (targets.py, RELEASES): LG's devlog
#                                         switch on over the bus, no preload; guest/initramfs/rootfs/init gives
#                                         it its start condition for webos.pmlog=full
#   tvservice.override                    the stock job's script with libtvservice-private.so
#                                         (guest/layers/common/tvservice) LD_PRELOADed after dbgfrwk's
#                                         liblibm
# securitymanager's and PmLogDaemon's jobs keep their stock names and scripts (guest/initramfs/rootfs/init's
# override for PmLogDaemon). dreadlocks2 (guest/layers/webos/dreadlocks2) names this directory its base.
# The gl layer (guest/layers/gl) takes wayland_server_compat from here. W16R's Wayland server is 1.7
# (libwayland-server.so.0.1.0), which exports every server function Mesa's libEGL, libgallium and
# dri_gbm import (wl_global_create, wl_resource_create, ...), so Mesa links it without the compat
# library.
wayland_server_compat = false

webos_layer_args += ['--tree', meson.current_source_dir() / 'rootfs', '.']
