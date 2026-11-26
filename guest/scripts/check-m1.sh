#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# M1 smoke test. Runs inside webOS, from the initramfs debug shell:
#   webos-enter sh /mnt/host/guest/scripts/check-m1.sh [WAIT [LOGNAME]]
# Hub security stays enabled: [Security] Enabled=true in the release's hub configuration (webOS 1
# and 2: ls-private.conf and ls-public.conf, one per hub; webOS 3: the one ls-hubd.conf), and the
# last call uses a copy of luna-send under another path, which has no role file, and must be
# refused by ls-hubd.
#
# bootd runs in its real-TV modes (hostname LGSmartTV, bootTarget "hardware"); the real-TV
# modes of the WT1H boot. What is expected depends on the mode:
#   The normal layer (LAYERS=normal,gl): a new state boots in FirstUse; the layer's
#     emulator-firstuse-done calls bootManager/firstUseDone as the FirstUse app once bootd has sent
#     minimal-boot-done, and bootd writes ran-firstuse, launches the last input app and goes on to
#     boot-done, bootStatus normal, in the same boot. Later boots are Normal: boot-done. With
#     normal,headless the headless layer makes ran-firstuse before bootd (FirstUse's UI cannot run
#     without a compositor), so every boot is Normal. Either way the check first waits up to WAIT
#     seconds (default 150) for boot-done and wants bootStatus normal.
#   FirstUse (no normal layer): bootd launches the web app com.palm.app.firstuse,
#     waits for it to come to the foreground, waits for the CPU to go idle and sends
#     minimal-boot-done. That is where it stays until the user finishes first use (firstUseDone).
#     So FirstUse checks firstAppLaunched and, with the gl layer, that
#     com.palm.app.firstuse is in the foreground and minimal-boot-done came; boot-done is reported
#     as INFO. Headless the app can never come to the foreground, so minimal-boot-done is OPEN.
# With the gl layer (LAYERS=...gl) it also checks that the compositor and WebAppMgr are running.
# At the end it saves the logs to out/logs/LOGNAME (default <time>-check-m1; guest/scripts/save-logs.sh),
# which the launcher saves to the saved logs (build/logs/LOGNAME for the checkout VM).
# TIME_SCALE (default 1; 4 under TCG, from tests/regress/guest.sh) stretches each call's limit.
scale=${TIME_SCALE:-1}
# busybox's timeout takes -t on the older firmwares (before busybox 1.30), the plain number from webOS 7
if timeout -t 1 true 2>/dev/null; then T="timeout -t"; else T="timeout"; fi
t() { echo "### $*"; $T $((20 * scale)) "$@" 2>&1 | head -c 1200; echo; }
fail=0
check() { # check NAME COMMAND...: print PASS/FAIL for a command's exit status
	name=$1; shift
	if "$@" >/dev/null 2>&1; then echo "PASS $name"; else echo "FAIL $name"; fail=1; fi
}
status() { $T $((20 * scale)) luna-send -n 1 luna://com.webos.bootManager/getBootStatus '{}' 2>&1; }
gl=0; [ -f /usr/lib/libegl-kms.so ] && gl=1
normal=0; [ -e /usr/sbin/emulator-firstuse-done ] && normal=1

# Wait for bootd to get as far as it will: boot-done in Normal and with the normal layer; in
# FirstUse without it minimal-boot-done with the gl layer, the first app headless.
wait=${1:-150}
while [ "$wait" -gt 0 ]; do
	s=$(status)
	case $s in
	*'"boot-done":true'*) break ;;
	*'"bootStatus":"firstUse"'*)
		[ $normal = 1 ] && { sleep 5; wait=$((wait - 5)); continue; }
		if [ $gl = 1 ]; then
			case $s in *'"minimal-boot-done":true'*) break ;; esac
		else
			case $s in *'"firstAppLaunched":true'*) break ;; esac
		fi ;;
	esac
	sleep 5; wait=$((wait - 5))
done

hubconfs=$(ls /etc/luna-service2/ls-private.conf /etc/luna-service2/ls-public.conf \
	/etc/luna-service2/ls-hubd.conf 2>/dev/null)
echo "### hub security"
grep -A1 '^\[Security\]' $hubconfs
echo "### hostname: $(hostname)"
echo "### jobs"
initctl list | grep -E '^(ls-hubd|ls-hubd_private|ls-hubd_public|sam|db8|SettingsService|configurator|filecache|bootd|activitymanager|tvpowerd|PmLogDaemon|surface-manager|WebAppMgr) '

t luna-send -n 1 -f luna://com.webos.bootManager/getBootStatus '{}'
t luna-send -n 1 luna://com.webos.service.tvpower/power/getPowerState '{}'
t luna-send -n 1 luna://com.webos.applicationManager/running '{}'
t luna-send -n 1 -f luna://com.webos.applicationManager/getAppInfo '{"id":"com.webos.app.browser"}'
t luna-send -n 1 -f luna://com.webos.settingsservice/getSystemSettings \
	'{"category":"option","keys":["country","smartServiceCountryCode2"]}'
t luna-send -n 1 -a com.webos.settingsservice luna://com.palm.db/find \
	'{"query":{"from":"com.webos.settings.system:1","limit":1}}'
t luna-send -n 1 -f luna://com.palm.configurator/run '{"types":["dbkinds"]}'

cp /usr/bin/luna-send /tmp/not-luna-send
t /tmp/not-luna-send -n 1 luna://com.webos.bootManager/getBootStatus '{}'

echo "### summary"
status=$(status)
[ "$hubconfs" ] || { echo "FAIL no hub configuration in /etc/luna-service2"; fail=1; }
for f in $hubconfs; do
	check "hub security enabled in ${f##*/}" sh -c "grep -A1 '^\[Security\]' $f | grep -q '^Enabled=true'"
done
check "hostname is not an emulator's" sh -c '! hostname | grep -q qemu'
check "bootTarget hardware" sh -c "echo '$status' | grep -q '\"bootTarget\":\"hardware\"'"
if [ $normal = 1 ]; then
	check "boot-done" sh -c "echo '$status' | grep -q '\"boot-done\":true'"
	check "bootStatus normal" sh -c "echo '$status' | grep -q '\"bootStatus\":\"normal\"'"
elif echo "$status" | grep -q '"bootStatus":"firstUse"'; then
	check "firstUse: com.palm.app.firstuse launched" sh -c "echo '$status' | grep -q '\"firstAppLaunched\":true'"
	if [ $gl = 1 ]; then
		check "firstUse: com.palm.app.firstuse in the foreground" sh -c "$T $((20 * scale)) luna-send -n 1 luna://com.webos.applicationManager/getForegroundAppInfo '{}' | grep -q '\"appId\":\"com.palm.app.firstuse\"'"
		check "firstUse: minimal-boot-done" sh -c "echo '$status' | grep -q '\"minimal-boot-done\":true'"
	elif echo "$status" | grep -q '"minimal-boot-done":true'; then
		echo "PASS firstUse: minimal-boot-done"
	else
		echo "OPEN firstUse: minimal-boot-done (headless, com.palm.app.firstuse cannot come to the foreground)"
	fi
	echo "INFO boot-done is not sent in firstUse (only in Normal, after first use is completed)"
else
	check "boot-done" sh -c "echo '$status' | grep -q '\"boot-done\":true'"
fi
check "tvpower Active" sh -c "$T $((20 * scale)) luna-send -n 1 luna://com.webos.service.tvpower/power/getPowerState '{}' | tr -d ' ' | grep -q '\"state\":\"Active\"'"
check "unknown exe refused" sh -c "$T $((20 * scale)) /tmp/not-luna-send -n 1 luna://com.webos.bootManager/getBootStatus '{}' 2>&1 | grep -q -- -1027"
if [ $gl = 1 ]; then
	if [ -d /run/systemd/system ]; then   # systemd (webOS 5): the units
		check "gl: surface-manager running" sh -c "systemctl is-active -q surface-manager-daemon.service"
		check "gl: WebAppMgr running" sh -c "systemctl is-active -q webapp-mgr.service"
	else
		check "gl: surface-manager running" sh -c "initctl status surface-manager | grep -q start/running"
		check "gl: WebAppMgr running" sh -c "initctl status WebAppMgr | grep -q start/running"
	fi
fi
# webOS 6 and later: eim's libcecp opens the kernel's CEC adapter, and without it its receive
# thread spins on the failed descriptor.
if grep -q /dev/cec0 /usr/lib/libcecp.so.1* 2>/dev/null; then
	check "CEC: /dev/cec0 for libcecp" test -c /dev/cec0
fi
# The far-field voice stack (webOS 6 and 7's ponytail, queue, ombre): the firmware's own probe
# (hw-option-gen's supportVoiceLongDistance, from the micom's 0x16) and lgemu's LGSIC capture card
# both follow the model's own fact (targets.Soc.mic, lgemu mic=1), so they must agree: with
# the microphone voiceinput_sound takes the card, which has no usbid; without it there is none.
if [ -x /usr/sbin/voiceinput_sound ]; then
	if grep -q '"supportVoiceLongDistance": *true' /var/run/tvconfig/hwoption/tv.hw.json 2>/dev/null; then
		check "mic: LGSIC capture card for voiceinput_sound" sh -c \
			'n=$(readlink /proc/asound/LGSIC) && test -c /dev/snd/pcmC${n#card}D0c && ! test -e /proc/asound/LGSIC/usbid'
	else
		check "mic: no LGSIC card on a model without the microphone" sh -c '! test -e /proc/asound/LGSIC'
	fi
fi
rm -f /tmp/not-luna-send
[ $fail = 0 ] && echo "M1 PASS" || echo "M1 FAIL"
sh /mnt/host/guest/scripts/save-logs.sh "${2:-$(date -u +%Y%m%dT%H%M%SZ)-check-m1}"
