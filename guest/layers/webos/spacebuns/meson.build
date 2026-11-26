# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# W24G spacebuns-sable uses ARM32 systemd255/NYX with a usr-merged root.
# Reuse the existing stock-service BSP/pak mount boundary. The initramfs
# already mounts the normalized logical paks; retain the stock dynamic unit
# and its real dependencies rather than inventing a dm-mod.create table.
# Stock Weston13 owns DRM/GBM windows, atomic mode and configd output policy.
# Select only the Mesa providers and client device permissions from the GL layer.
gl_compositor_integration = 'native'
# The firmware's own Wayland is 1.22 (firmware_wayland, guest/layers/webos/ombre).
firmware_wayland = '1.22.0'

# Replace the model-year clock floor with the real RTC calendar on this supported target.
if soc == 'o22n2'
  webos_layer_args += ['--tree', meson.current_source_dir() / 'rtc-rootfs', '.']
endif

# The W24G AEC module requires card1; this playback-only target has no provider.
if soc == 'o22n2' and webos == 'spacebuns'
  webos_layer_args += ['--tree', meson.current_source_dir() / 'audio-rootfs', '.']
endif

# SAM after the Flutter zygote, which a set's snapshot boot starts first (zygote-rootfs/.../sam.service.d).
webos_layer_args += ['--tree', meson.current_source_dir() / 'zygote-rootfs', '.']
