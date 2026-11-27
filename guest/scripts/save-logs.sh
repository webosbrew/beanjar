#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Save the guest's logs to the host's output share before the VM goes away: /var/log is on tmpfs.
# Runs in the guest, either in the initramfs debug shell or inside webOS:
#   sh /host/guest/scripts/save-logs.sh [NAME]          (initramfs; enters webOS for the webOS part)
#   sh /mnt/host/guest/scripts/save-logs.sh [NAME]      (inside webOS, e.g. from check-m1.sh)
# Writes out/logs/NAME/ (/host/out, the VM's RUN_DIR/share/out on the host; NAME defaults to the
# guest's UTC time), which the launcher saves to the saved logs (build/logs/NAME for the checkout
# VM, <vm>/logs/saved/NAME for a library VM) once .done is there, and then removes: at stop, at
# start, or with `host/scripts/vm-launcher logs` (host/core/beanjar/guestside/share.py, save). Only regular
# files and directories are saved: a symlink (cp -a keeps /var/log's), FIFO or socket here is
# listed in the saved directory's skipped.txt, with a symlink's target. The launcher adds its own
# webos.log, console.log, qemu.out and netstub.log there; files of those names here are not saved.
#   var-log/           a copy of /var/log (PmLog's messages, bootd.log, ls-hubd.log, crashd, ...)
#   dmesg.txt          kernel log
#   meminfo.txt        /proc/meminfo
#   bootstatus.json    bootManager/getBootStatus
#   jobs.txt, ps.txt   initctl list, processes
#   pmlog-contexts.txt PmLogCtl show (context levels)
#   tmp-logs/          /tmp/*.log (e.g. SM_LOG from /etc/default/egl-kms)
#   cmdline.txt        /proc/cmdline as webOS sees it
#   locksample.txt     the file locks over the boot (webos.locksample=S: guest/initramfs/rootfs/init)
#   device-counters/ request counters and availability inventory (outer/firmware namespaces)
#   mailbox.txt        /proc/mstar-utopia/mailbox, the mailbox trace (M3 with mbx_trace=1 only)
#   teec-lock.txt      MStar's TEE client lock file /dev/shm/file.mstar.TEEC, /dev/shm's mount,
#                      the processes holding the file open and the kernel's file locks (M3 only)
#   .done              written last (by the initramfs part when it runs the webOS part)
# host/scripts/vm-launcher stop calls this through the serial console.
name=${1:-$(date -u +%Y%m%dT%H%M%SZ)}
done=${2:-done}
if [ "$done" = part ] || [ -d /mnt/host/out ]; then
	inside=1; host=/mnt/host
else
	inside=0; host=/host
fi
d=$host/out/logs/$name
mkdir -p "$d" || exit 1
dmesg > "$d/dmesg.txt" 2>&1
cat /proc/meminfo > "$d/meminfo.txt" 2>&1
if [ "$inside" = 0 ]; then scope=outer; else scope=firmware; fi
sh "$host/guest/scripts/save-device-counters.sh" "$d/device-counters/$scope" ||
	echo "request counter collection failed" > "$d/device-counters-error.txt"
[ -s /run/locksample.txt ] && [ $inside = 0 ] && cp /run/locksample.txt "$d/"

if [ $inside = 0 ]; then
	# The webOS part needs the webOS mount namespace (its /var and /tmp are private to it).
	webos-enter sh /mnt/host/guest/scripts/save-logs.sh "$name" part >/dev/null 2>&1 ||
		echo "webOS not running" > "$d/webos-missing.txt"
	: > "$d/.done"
	echo "logs: out/logs/$name"
	exit 0
fi

rm -f "$d/.done"
mkdir -p "$d/var-log" "$d/tmp-logs"
cp -a /var/log/. "$d/var-log/" 2>/dev/null
for f in /tmp/*.log; do [ -f "$f" ] && cp "$f" "$d/tmp-logs/"; done
cat /proc/cmdline > "$d/cmdline.txt"
if [ -x /usr/bin/luna-send ]; then
	# busybox's timeout: -t on the older firmwares, the plain number from webOS 7
	if timeout -t 1 true 2>/dev/null; then T="timeout -t"; else T=timeout; fi
	$T 10 luna-send -n 1 luna://com.webos.bootManager/getBootStatus '{}' > "$d/bootstatus.json" 2>&1
else
	echo "luna-send unavailable (legacy firmware)" > "$d/bootstatus-unavailable.txt"
fi
initctl list > "$d/jobs.txt" 2>&1
ps -eo pid,ppid,user,group,stat,rss,etime,args > "$d/ps.txt" 2>&1 || ps > "$d/ps.txt" 2>&1
PmLogCtl show > "$d/pmlog-contexts.txt" 2>&1
[ -r /proc/mstar-utopia/mailbox ] && cat /proc/mstar-utopia/mailbox > "$d/mailbox.txt" 2>&1
if [ -e /dev/msmailbox ]; then
	{
		ls -l /dev/shm/file.mstar.* 2>&1
		grep ' /dev/shm ' /proc/mounts || echo "/dev/shm: not a mount"
		echo "holders of /dev/shm/file.mstar.TEEC:"
		for l in /proc/[0-9]*/fd/*; do
			[ "$(readlink "$l" 2>/dev/null)" = /dev/shm/file.mstar.TEEC ] || continue
			p=${l#/proc/}; p=${p%%/*}
			echo "$p ${l##*/} $(cat /proc/$p/comm 2>/dev/null) $(stat -L -c 'inode %i' "$l" 2>/dev/null)"
		done
		echo "/proc/locks:"
		cat /proc/locks
	} > "$d/teec-lock.txt" 2>&1
fi
[ "$done" = part ] || : > "$d/.done"
echo "logs: out/logs/$name"
