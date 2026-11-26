# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# W22H queue-qilian brings NYX LLS and Weston; runtime bring-up uses the stock services.
# NYX GAL calls HAL directly, bypassing the older DILE entry point: the K8 soc layer's libhalgal
# shim serves it (guest/layers/soc/k8lp).
# The firmware's own Wayland is 1.22 (firmware_wayland, guest/layers/webos/ombre).
firmware_wayland = '1.22.0'
# The adaptive partition (appackage.pak): etc/emulator/adaptive-partition.sh, which a drop-in for
# mount-readwrite.service runs first (rootfs/), puts the pak's disk on the apps partition as a set's
# factory or update service leaves it; apinstaller mounts it (guest/targets/release/queue.toml).
webos_layer_args += ['--tree', meson.current_source_dir() / 'rootfs', '.']
