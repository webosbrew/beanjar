#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Boot timing, in the initramfs debug shell right after boot (tests/regress/boot/run.sh puts it into the
# VM's share with `host/scripts/vm-launcher put` and types it):
#   sh /host/tests/regress/boot/guest.sh NAME [APP [SETTLE]]
# Waits for bootd's last signal, reading bootd.log through /proc/<webOS init>/root so that nothing
# runs inside webOS meanwhile: boot-done in Normal and with the normal layer, minimal-boot-done in
# FirstUse without it. With APP it
# then waits SETTLE seconds (default 20), launches APP once and waits up to 120 s for WebAppMgr to
# report the page loaded (WAM APPLOADED; needs the gl layer), then up to 20 s more for the
# compositor to map its surface (LSM "surfaceMapped", the app's first frame; needs a GPU: without
# virgl, e.g. on TCG, web pages cannot create their surfaces).
# Finally it runs check-m1.sh, which saves the logs to out/logs/NAME (build/logs/NAME once the
# VM stops); tests/regress/boot/timeline.py
# turns them into the timeline. Prints "BOOTTEST ..." lines for the host.
name=$1 app=$2 settle=${3:-20}
up() { cut -d' ' -f1 /proc/uptime; }
pid=
while [ -z "$pid" ]; do
	for s in /proc/[0-9]*/status; do
		grep -q "^NSpid:[[:space:]]*[0-9][0-9]*[[:space:]][[:space:]]*1$" "$s" 2>/dev/null &&
			{ pid=${s#/proc/}; pid=${pid%/status}; break; }
	done
	[ -n "$pid" ] || sleep 0.5
done
root=/proc/$pid/root
# /var/log is a symlink to /tmp/var/log inside webOS
logdir=$root/tmp/var/log
log=$logdir/bootd.log
# Normal ends at boot-done, FirstUse without the normal layer at minimal-boot-done
# (see guest/scripts/check-m1.sh).
i=0
until grep -q 'FirstUse=' "$log" 2>/dev/null; do
	sleep 0.5; i=$((i + 1)); [ $i -lt 600 ] || { echo "BOOTTEST FAIL no bootd.log"; break; }
done
# The normal layer completes FirstUse in the same boot (emulator-firstuse-done): boot-done there too.
target=boot-done
grep -q 'FirstUse=YES' "$log" 2>/dev/null && [ ! -e "$root/usr/sbin/emulator-firstuse-done" ] &&
	target=minimal-boot-done
i=0
until grep -q "SIGNAL_$target\$" "$log" 2>/dev/null; do
	sleep 0.5; i=$((i + 1)); [ $i -lt 600 ] || { echo "BOOTTEST FAIL no $target within 300 s"; break; }
done
echo "BOOTTEST target=$target seen_at=$(up)"
if [ -n "$app" ]; then
	sleep "$settle"
	echo "BOOTTEST launch $app at=$(up)"
	webos-enter luna-send -n 1 luna://com.webos.applicationManager/launch "{\"id\":\"$app\"}" </dev/null
	echo
	i=0
	until grep -q "APPLOADED {\"APP_ID\":\"$app\"" "$logdir/messages" 2>/dev/null; do
		sleep 0.2; i=$((i + 1)); [ $i -lt 600 ] || { echo "BOOTTEST FAIL $app not loaded"; break; }
	done
	echo "BOOTTEST loaded $app seen_at=$(up)"
	i=0
	until grep -q "surfaceMapped.*\"$app\"" "$logdir/messages" 2>/dev/null; do
		sleep 0.2; i=$((i + 1)); [ $i -lt 100 ] || { echo "BOOTTEST OPEN no first frame for $app"; break; }
	done
	echo "BOOTTEST mapped $app seen_at=$(up)"
fi
webos-enter sh /mnt/host/guest/scripts/check-m1.sh 10 "$name" > /tmp/check-m1.out 2>&1
grep -E '^(PASS|FAIL|OPEN|INFO|M1) ' /tmp/check-m1.out | sed 's/^/BOOTTEST m1 /'
echo "BOOTTEST done"
