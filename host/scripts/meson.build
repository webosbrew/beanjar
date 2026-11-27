# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Uncompressed copies of the firmware paks for ROOTFS=uncompressed (option `firmware_cache`, auto:
# when the firmware is present). `host/scripts/vm-launcher firmware-cache` (host/core/beanjar/firmware/cache.py) keeps
# its own content-keyed cache in build/firmware-cache (the launcher uses it too), so running it on
# every build only stats the paks.
# Conversion needs squashfs-tools 4.6 or later; firmware inspection also uses unsquashfs.
# Not in the arm64 cross build (host_cross): the cache is this machine's, for its own VMs.
firmware_cache = get_option('firmware_cache').require(not host_cross,
  error_message: 'the firmware cache is not built by a cross build')
firmware_cache = firmware_cache.require(fs.exists(firmware / 'rootfs.pak'),
  error_message: 'no ' + firmware / 'rootfs.pak')
foreach prog : ['unsquashfs', 'mksquashfs']
  firmware_cache = firmware_cache.require(
    find_program(prog, native: true, required: firmware_cache).found())
endforeach
if firmware_cache.allowed()
  custom_target('firmware-cache',
    output: 'firmware-cache.list',
    command: ['sh', '-c', 'FW="$1" "$2" firmware-cache > "$3"', 'sh', firmware,
              files('vm-launcher'), '@OUTPUT@'],
    build_always_stale: true,
    build_by_default: true)
endif
