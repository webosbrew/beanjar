#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The adaptive partition (appackage.pak, the VM's disk with serial "appackage") on the apps
# partition as the update service leaves it after apinstaller's verifyAPBinary, the state a set
# has from its factory or its first update: /mnt/lg/appstore/ap/main/ap.squashfs (the pak without
# its last 32 bytes), main/ap.sha256 (those bytes, the SHA-256 of the rest), the "updated" result
# and the status "update". apinstaller's mount (mount-readwrite.sh's luna call) then copies it to
# backup/, mounts it through the webos-verity target and binds its apps in, as on a set.
# Run once, before mount-readwrite.sh, from its drop-in; a state that has the partition is left
# alone (apinstaller keeps it from then on, and the stock first-mount clean-up keeps ap/).
AP=/mnt/lg/appstore/ap
log() { echo "adaptive-partition: $*" > /dev/kmsg; }

[ -e "$AP/main/ap.squashfs" ] && exit 0
dev=
for b in /sys/block/vd*; do
	[ "$(cat "$b/serial" 2>/dev/null)" = appackage ] && dev=/dev/${b##*/}
done
[ -n "$dev" ] || exit 0
grep -q " /mnt/lg/appstore " /proc/mounts || { log "the apps partition is not mounted"; exit 1; }
# QEMU rounds the disk up to whole sectors: the pak is a 4 KiB-aligned image and its 32-byte hash,
# so the image ends one sector before the disk does and the hash starts there
sectors=$(cat "/sys/block/${dev#/dev/}/size")
image=$((sectors - 1))
mkdir -p "$AP/main" || exit 1
rm -f "$AP/main/ap.squashfs.new" "$AP/main/ap.sha256"
if dd if="$dev" of="$AP/main/ap.squashfs.new" bs=512 count=$image 2>/dev/null &&
	dd if="$dev" of="$AP/main/ap.sha256" bs=32 skip=$((image * 16)) count=1 2>/dev/null &&
	[ "$(wc -c < "$AP/main/ap.sha256")" -eq 32 ] &&
	mv "$AP/main/ap.squashfs.new" "$AP/main/ap.squashfs"; then
	printf updated > "$AP/updated"
	printf update > "$AP/status"
	sync
	log "$dev installed as $AP/main/ap.squashfs ($((image * 512)) bytes)"
else
	rm -f "$AP/main/ap.squashfs.new" "$AP/main/ap.sha256"
	log "$dev could not be installed"
	exit 1
fi
