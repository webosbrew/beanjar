# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The webos layer of kisscurl, webOS 6.x (W21P's 6.6.0 "kisscurl-kolovesi"),
# build/layers/<chip>-kisscurl/webos, always used. Its base is jhericurl (base: guest/layers/webos/jhericurl
# is read first), and adds only accountmanager.service.d/emulator-restart.conf (rootfs/, which mullet and later inherit): every stock unit the jhericurl layer drops in for is W20L's
# byte for byte in W21P (bsp-mounted.path, mount-bsp.service, surface-manager-daemon.service,
# airplay-adaptor.service, settings-service.service; scratch/w21p-bringup-study.md, section 1),
# with the same conditions, and its squashfs fstab names the same partitions at the same mount
# points (p14, p30-p32, p34). The table is jhericurl's too: systemd, W21P's Wayland server is
# 1.11 as W20L's (libwayland-server.so.0.1.0), and its bsppart libhalgal's
# HAL_GAL_GetDeviceCapability still clears 76 bytes.
webos_layer_args += ['--tree', meson.current_source_dir() / 'rootfs', '.']
