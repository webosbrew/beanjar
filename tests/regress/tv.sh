#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The TV feature suite's guest side (scripts/regress --tv; docs/dev/guides/build.md, "TV feature suite"). Runs
# in webOS (tests/regress/guest.sh types `webos-enter sh /mnt/host/tests/regress/tv.sh STEP ...`)
# and gathers facts only, one "name=value" a line; tests/regress/lib/tvsuite.py judges them on the
# host against tests/regress/tv-expect/<target>.toml. Read-only but for the audio step's volume, which it
# puts back.
#   tv.sh luna-info                 the public Luna APIs LG documents for apps, tvservice's own
#                                   info calls, nyx-cmd's OSInfo and DeviceInfo, the board's words
#   tv.sh network URL NAME          the network: eth0's address and routes, the resolver, the
#                                   connection manager's status, NAME resolved, URL fetched
#   tv.sh app-launch ID PARAMS LANE SECONDS [ALSO]  sam's getAppInfo of ID (present=no: not
#                                   launched), its launch (PARAMS: its params, JSON), then
#                                   until it (or ALSO, sam's stand-in for it) is in front (LANE
#                                   graphics: sam's or the compositor's foreground; headless: in
#                                   sam's running list), front_id which, then app-state
#   tv.sh app-state LANE            the foreground (sam's and the compositor's), sam's running
#                                   list, the pids of WebAppMgr and the compositor, crash reports
#   tv.sh app-close ID SECONDS LANE sam's closeByAppId, then until ID's process is gone, app-state
#   tv.sh app-install IPK ID        appinstalld's dev/install of IPK (the devmode layer: developer
#                                   mode on), then sam's getAppInfo of ID
#   tv.sh app-remove ID             dev/remove of ID, then sam's getAppInfo of it
#   tv.sh tee LIB SSTORE TA...      the TEE through the firmware's own client (LIB, its libteec):
#                                   /dev/tee0, tests/regress/tee/tee-probe's sessions of SSTORE and the TAs
#                                   and the secure-store TA's commands (round trips), the crash
#                                   reports and those of a TEE client (TEEC_*, sstore in them)
#   tv.sh histogram SUITE           libvtcapture's histogram calls through
#                                   tests/regress/histogram/hist-probe.so preloaded into SUITE (the
#                                   firmware's vtCaptureTestSuite, which has the library's luna role),
#                                   with /var/luna/preferences/vt_devmode (the calls' caller gate) made
#                                   for the run and removed after if it was not there; the crash reports
#   tv.sh audio DIR SVC N [RB [HW]] the Audio API (SVC: its luna:// URL; RB: the read-back,
#                                   SVC/getVolume or audiod's master/getVolume): getVolume, a tone
#                                   (DIR/native.wav) through paplay, one (DIR/aplay.wav) through
#                                   aplay to ALSA's default and, with HW (pcm_output's hw:C,D, "-"
#                                   for none), one (DIR/hw.wav) straight to that PCM once PulseAudio
#                                   has suspended it; N volumeDown and a tone, setMuted and a tone,
#                                   unmute and N volumeUp and a tone; the host records
#                                   (tests/regress/audio/capture.py) and judges. Restores what it changed
#   tv.sh decode-log CASE           the decode step's page lines of CASE where the test app's
#                                   console reaches PmLog ("TVSUITE decode CASE ..."), the media
#                                   stack's recent error lines (pipeline, uMediaServer, sinks), the
#                                   crash reports
#   tv.sh smp ID SECONDS            the SMP check app (tests/regress/smp, installed as ID): sam's launch,
#                                   up to SECONDS for its summary line, its PmLog lines (smp=),
#                                   sam's closeByAppId, the crash reports
# TIME_SCALE (default 1; 4 under TCG) stretches each call's limit.
scale=${TIME_SCALE:-1}
# busybox's timeout takes -t on the older firmwares, the plain number on the newer ones
if timeout -t 1 true 2>/dev/null; then T="timeout -t"; else T="timeout"; fi
# lq NAME URL PAYLOAD: one luna-send answer on one line
lq() {
	n=$1; shift
	echo "$n=$($T $((20 * scale)) luna-send -n 1 "$@" </dev/null 2>&1 | tr -d '\n')"
}
one() { tr '\n' ' ' | sed 's/ *$//'; }
cmdline() { tr ' ' '\n' </proc/cmdline | sed -n "s/^$1=//p" | head -n 1; }

# apps call the public APIs as public clients: on the public hub where there are two (webOS 1
# and 2, ls-public.conf), whose methods may differ from the private one's (W15M's connection
# manager: getStatus on the public bus, getstatus on the private); one hub: luna-send
P=luna-send
[ -e /etc/luna-service2/ls-public.conf ] && P=luna-send-pub
# pq NAME URL PAYLOAD: lq on the apps' bus; with two hubs also on the private one (NAME.prv):
# luna-send-pub is a public client without an app's permissions, which settingsservice and
# tvservice's public key table may refuse where the private hub answers
pq() {
	n=$1; shift
	echo "$n=$($T $((20 * scale)) $P -n 1 "$@" </dev/null 2>&1 | tr -d '\n')"
	[ "$P" = luna-send ] || lq "$n.prv" "$@"
}

# the system up: bootd's boot-done (a headless run starts at core-boot-done, before connman and
# others are), at most 150 s (stretched); the time it took is a fact
boot_done() {
	i=0
	while [ $i -lt $((150 * scale)) ]; do
		case $($T $((10 * scale)) luna-send -n 1 luna://com.webos.bootManager/getBootStatus '{}' </dev/null 2>/dev/null) in
		*'"boot-done":true'*) echo "bootdone_wait=$i"; return 0 ;;
		esac
		sleep 1; i=$((i + 1))
	done
	echo "bootdone_wait=none"
}

luna_info() {
	boot_done
	for w in modelName serialNum ethaddr chip webos.serial webos.ethaddr; do
		echo "cmdline.$w=$(cmdline $w)"
	done
	echo "uname=$(uname -r)"
	echo "bus=$P"
	echo "starfish_release=$(head -n 1 /etc/starfish-release 2>/dev/null)"
	# a global firmware's broadcast line is the board's: nyx-option-gen's (webOS 6)
	n=/tmp/var/run/tvconfig/nyx/tv.nyx.json
	[ -f $n ] && echo "nyx_system=$(sed -n 's/.*"tvBroadcastSystem": *"\([A-Z]*\)".*/\1/p' $n)"
	# the SoC name the firmware's hw-option-gen gives configd (K8LPN2 on chip=K8LPNN), which webOS 5+
	# builds boardType from
	h=/tmp/var/run/tvconfig/hwoption/tv.hw.json
	[ -f $h ] && echo "soc_chip=$(sed -n 's/.*"SoCChipType": *"\([A-Za-z0-9_]*\)".*/\1/p' $h)"
	# the OTA id the firmware's own files carry, which tvservice's otaId and nyx's hardware_id answer
	# (never the firmware collection's name, which may be another build's: W22O's AFABATPU). The newest
	# format first: etc/starfish/model_id.json (otaid; W20H on), etc/starfish/model_id (W16R to W19P),
	# and where there is neither (W15M, WT1H) the model pak's RELEASE.cfg (szOtaid) that
	# DILE_BOARDINFO_GetOTAID reads; later releases keep a stale RELEASE.cfg (W16R's says W15U), so it
	# never comes first
	if [ -f /etc/starfish/model_id.json ]; then
		echo "ota_id=$(sed -n 's/.*"otaid": *"\([A-Za-z0-9_]*\)".*/\1/p' /etc/starfish/model_id.json)"
	elif [ -f /etc/starfish/model_id ]; then
		echo "ota_id=$(head -n 1 /etc/starfish/model_id)"
	elif [ -f /mnt/lg/model/RELEASE.cfg ]; then
		echo "ota_id=$(sed -n 's/^szOtaid,\([A-Za-z0-9_]*\).*/\1/p' /mnt/lg/model/RELEASE.cfg)"
	fi
	S=luna://com.webos.service.tv.systemproperty
	# TV Device Information: each documented key on its own, then all of them in one call
	for k in boardType firmwareVersion modelName sdkVersion UHD _3d; do
		pq "sysinfo.$k" $S/getSystemInfo "{\"keys\":[\"$k\"]}"
	done
	# tvservice's own identity keys (sdx, FirstUse and the settings app read them)
	for k in otaId hardwareVersion serialNumber tvSystemName contiLangCountry platformName; do
		lq "tvsp.getSystemInfo.$k" $S/getSystemInfo "{\"keys\":[\"$k\"]}"
	done
	lq tvsp.getCurrentCountryGroup $S/getCurrentCountryGroup '{}'
	# Settings Service: each documented key of each category, and localeInfo
	G=luna://com.webos.settingsservice/getSystemSettings
	for k in country smartServiceCountryCode2 audioGuidance; do
		pq "settings.option.$k" $G "{\"category\":\"option\",\"keys\":[\"$k\"]}"
	done
	for k in captionEnable captionAnalog captionDigital captionStyle captionSize captionFont \
		captionTextColor captionTextOpacity captionBgColor captionBgOpacity captionEdgeType \
		captionEdgeColor captionWindowColor captionWindowOpacity; do
		pq "settings.caption.$k" $G "{\"category\":\"caption\",\"keys\":[\"$k\"]}"
	done
	pq settings.localeInfo $G '{"keys":["localeInfo"]}'
	# Connection Manager, both names
	pq connman.palm luna://com.palm.connectionmanager/getStatus '{}'
	pq connman.webos luna://com.webos.service.connectionmanager/getStatus '{}'
	# Device Unique ID
	pq sm.getIDs luna://com.webos.service.sm/deviceid/getIDs '{"idType":["LGUDID"]}'
	# System Service, Application Manager, Magic Remote, Camera, BLE GATT: their read-only getters
	pq systime luna://com.palm.systemservice/time/getSystemTime '{}'
	pq sam.getAppLoadStatus luna://com.webos.applicationManager/getAppLoadStatus '{"appId":"com.webos.app.livetv"}'
	pq mrcu.getAPIVersion luna://com.webos.service.mrcu/getAPIVersion '{}'
	pq mrcu.getSensorState luna://com.webos.service.mrcu/sensor2/getSensorState '{}'
	pq camera.getList luna://com.webos.service.camera/getList '{}'
	pq blegatt.getState luna://com.webos.service.blegatt/getState '{}'
	# nyx: every key its help lists, each queried on its own
	for d in OSInfo DeviceInfo; do
		keys=$(nyx-cmd -h $d 2>&1 | sed -n '/^QUERY *$/,$p' | sed -n 's/^  \([a-z_0-9]*\)  .*/\1/p')
		echo "nyx.$d.keys=$(echo $keys)"
		for k in $keys; do
			# stdout (the value: its first line; .more the rest) and stderr (.err) apart
			v=$($T $((10 * scale)) nyx-cmd $d query $k </dev/null 2>/tmp/tv-nyx.err); rc=$?
			echo "nyx.$d.$k=rc=$rc $(printf '%s\n' "$v" | head -n 1)"
			m=$(printf '%s\n' "$v" | sed 1d | one)
			[ -n "$m" ] && echo "nyx.$d.$k.more=$m"
			e=$(one </tmp/tv-nyx.err)
			[ -n "$e" ] && echo "nyx.$d.$k.err=$e"
		done
	done
	rm -f /tmp/tv-nyx.err
	echo "TV FACTS END"
}

network() {
	url=$1 name=$2
	echo "addr=$( (ip -4 addr show eth0 || ifconfig eth0) 2>&1 | one)"
	echo "route=$(cat /proc/net/route | one)"
	for f in /etc/resolv.conf /var/run/resolv.conf /tmp/resolv.conf; do
		[ -e $f ] && echo "resolv=$f $(grep -v '^#' $f | one)"
	done
	# the connection manager under each name and spelling it has had (webOS 1 and 2: com.palm's
	# getstatus)
	lq connman.webos.getStatus luna://com.webos.service.connectionmanager/getStatus '{}'
	lq connman.palm.getStatus luna://com.palm.connectionmanager/getStatus '{}'
	lq connman.palm.getstatus luna://com.palm.connectionmanager/getstatus '{}'
	# the guest's resolver (connman's proxy on the loopback, resolv.conf's), with busybox's
	# nslookup, which every firmware has (its reverse lookups take up to 20 s; getent is not on
	# webOS 1 to 3.4, and W17H's glibc 2.19 answers `getent ahosts` with nothing where wget and
	# curl resolve)
	v=$($T $((60 * scale)) nslookup "$name" </dev/null 2>&1); rc=$?
	echo "dns=nslookup rc=$rc $(printf %s "$v" | one)"
	v=$($T $((20 * scale)) wget -q -O - "$url" </dev/null 2>&1); rc=$?
	echo "http=rc=$rc $(printf %s "$v" | one)"
	echo "TV FACTS END"
}

SAM=luna://com.webos.applicationManager
# app_state LANE: the facts every app step ends with (headless: no compositor to ask; the
# stand-in does not answer)
app_state() {
	lq sam_fg $SAM/getForegroundAppInfo '{}'
	if [ "$1" != headless ]; then
		lq lsm_fg luna://com.webos.surfacemanager/getForegroundAppInfo '{}'
		lq lsm_state luna://com.webos.surfacemanager/getCurrentState '{}'
	fi
	lq running $SAM/running '{}'
	# WebAppMgr: its browser process (the one without Chromium's --type=; its renderers, zygote and
	# short-lived utility processes carry the same name), and every one's parent and command line
	wam=
	for p in $(pidof WebAppMgr); do
		c=$(tr '\0' ' ' </proc/$p/cmdline 2>/dev/null)
		case $c in *--type=*) ;; *) wam="$wam${wam:+ }$p" ;; esac
		echo "proc.wam.$p=ppid $(sed -n 's/^PPid:[[:space:]]*//p' /proc/$p/status 2>/dev/null) $(printf %s "$c" | cut -c1-200)"
	done
	echo "pid.wam=$wam"
	# the compositor (webOS 1's runs under another name than its binary's: pidof misses it)
	lsm=
	for p in /proc/[0-9]*; do
		c=$(tr '\0' ' ' <$p/cmdline 2>/dev/null) || continue
		case $c in
		*/surface-manager*|surface-manager*) lsm="$lsm${lsm:+ }${p#/proc/}" ;;
		esac
	done 2>/dev/null
	echo "pid.lsm=$lsm"
	# crash reports so far (crashd's librdx: RDXDErr3_<process>__Linux Crash ...), and what the
	# test app logged to the console
	echo "crashes=$(ls /var/log/reports/librdx 2>/dev/null | sed -n 's/^RDXDErr[0-9]*_\([^_]*\)__.*\.\([0-9][0-9]*\)\.[^.]*\.gz$/\1.\2/p' | tr '\n' ' ')"
	# and each one's signature: "crashsig.<pid>=<process> <signal> <frame>...", the frames
	# (symbol+offset) of its first backtrace, the crashing thread's, for tvsuite's EXPECTED_CRASHES
	for f in /var/log/reports/librdx/RDXDErr*.gz; do
		[ -f "$f" ] || continue
		id=$(basename "$f" | sed -n 's/^RDXDErr[0-9]*_\([^_]*\)__.*\.\([0-9][0-9]*\)\.[^.]*\.gz$/\2 \1/p')
		[ -n "$id" ] || continue
		echo "crashsig.${id%% *}=${id#* } $(gzip -dc "$f" 2>/dev/null | awk '
			/^Signal:/ && s == "" { s = $2 }
			/^Backtrace: tid/ { n++ }
			n == 1 && /\(.*\)/ && k++ < 32 { sub(/^[^(]*\(/, ""); sub(/\).*/, ""); fr = fr " " $0 }
			END { print s fr }')"
	done
	echo "console=$(grep -h TVSUITE /var/log/messages 2>/dev/null | tail -n 2 | tr '\n' ' ' | cut -c1-400)"
	echo "uptime=$(cut -d' ' -f1 /proc/uptime)"
	echo "TV FACTS END"
}
# in_front ID LANE: graphics, sam's foreground app or a compositor window of ID's (overlays such
# as Home are the compositor's only); headless (no compositor), in sam's running list
in_front() {
	if [ "$2" = headless ]; then
		$T $((10 * scale)) luna-send -n 1 $SAM/running '{}' </dev/null 2>/dev/null | grep -q "\"id\": *\"$1\""
		return
	fi
	$T $((10 * scale)) luna-send -n 1 $SAM/getForegroundAppInfo '{}' </dev/null 2>/dev/null |
		grep -q "\"appId\": *\"$1\"" && return 0
	$T $((10 * scale)) luna-send -n 1 luna://com.webos.surfacemanager/getForegroundAppInfo '{}' </dev/null 2>/dev/null |
		grep -q "\"$1\""
}
close_alerts() {
	for a in $( (zcat /var/log/messages.*.gz 2>/dev/null; cat /var/log/messages) |
			grep '"alertAction":"open"' | grep -o '"alertId":"[^"]*"' | cut -d'"' -f4 | sort -u); do
		r=$($T $((10 * scale)) luna-send -n 1 luna://com.webos.notification/closeAlert "{\"alertId\":\"$a\"}" </dev/null 2>&1 | tr -d ' \n')
		echo "alert_closed=$a $r"
	done
	# an alert whose id PmLog does not have (webOS 23 Home's EU energy-saving notice: notificationmgr
	# logs no alertId) cannot be closed by id: wait for it to go (that one times out after 120 s), as
	# surface-manager's last visibility line for the alert window says (getCurrentState says
	# alertView only once an app is in front of Home)
	i=0
	while alert_shown; do
		[ $i -ge $((150 * scale)) ] && break
		sleep 2; i=$((i + 2))
	done
	[ $i -eq 0 ] || echo "alert_wait=$i"
}
alert_shown() {
	(zcat /var/log/messages.*.gz 2>/dev/null; cat /var/log/messages) |
		grep '"app_id":"com.webos.app.alert","visible":' | sort | tail -n 1 | grep -q '"visible":true'
}
app_launch() {
	id=$1 params=${2:-\{\}} lane=$3 limit=$4 also=$5
	# installed and visible to sam (a firmware's pak may hold an app its region or model filters
	# out): getAppInfo first; an app sam does not know is not launched
	info=$($T $((20 * scale)) luna-send -n 1 $SAM/getAppInfo "{\"id\":\"$id\"}" </dev/null 2>&1 | tr -d '\n')
	echo "appinfo=$info"
	if ! echo "$info" | grep -q '"returnValue": *true'; then
		echo "present=no"
		app_state "$lane"
		return
	fi
	echo "present=yes"
	# a system alert left open (W22O queue's first boot: surfacemanager's "Parental Controls have
	# been reset", waiting for OK) covers the app the steps capture: closed first, as a viewer would,
	# by the ids PmLog has of the alerts opened (closeAllAlerts closes only the caller's own)
	[ "$lane" = headless ] || close_alerts
	# a web app's launch waits for WebAppMgr, which headless has not: a short wait there
	if [ "$lane" = headless ]; then
		echo "launch=$($T $((5 * scale)) luna-send -n 1 $SAM/launch "{\"id\":\"$id\",\"params\":$params}" </dev/null 2>&1 | tr -d '\n')"
	else
		lq launch $SAM/launch "{\"id\":\"$id\",\"params\":$params}"
	fi
	# ALSO: what sam may open in the app's place (LG's terms screen for an app whose terms the
	# set's user has not agreed to); front_id says which came up
	i=0 front=
	until [ -n "$front" ]; do
		if in_front "$id" "$lane"; then front=$id
		elif [ -n "$also" ] && in_front "$also" "$lane"; then front=$also
		else
			[ $i -ge "$limit" ] && break
			sleep 1; i=$((i + 1))
		fi
	done
	[ -n "$front" ] && echo "front_wait=$i" || echo "front_wait=none"
	echo "front_id=$front"
	app_state "$lane"
}
# entry ID: sam's running list's entry for ID, on one line ("" when not running); id is not
# always its first key (W22H's sam puts webprocessid and defaultWindowType first)
entry() {
	$T $((10 * scale)) luna-send -n 1 $SAM/running '{}' </dev/null 2>/dev/null | tr -d ' \n' |
		grep -o "{[^{}]*\"id\":\"$1\"[^{}]*}"
}
# closed: gone from the running list, or another process of it (Live TV, the TV's base app, comes
# back when nothing else is in front)
app_close() {
	id=$1 limit=$2 lane=$3
	before=$(entry "$id")
	echo "close_before=$before"
	lq close $SAM/closeByAppId "{\"id\":\"$id\"}"
	i=0
	while [ -n "$before" ] && [ "$(entry "$id")" = "$before" ]; do
		[ $i -ge "$limit" ] && break
		sleep 1; i=$((i + 1))
	done
	[ $i -ge "$limit" ] && echo "gone_wait=none" || echo "gone_wait=$i"
	app_state "$lane"
}
# appinstalld's subscription (luna-send -i, as ares-install) until its answer matches $3, at most
# 120 s (stretched); its lines, on one
dev_call() {
	n=$1; shift
	o=/tmp/tv-dev.out
	luna-send -i "$1" "$2" </dev/null >$o 2>&1 &
	p=$!
	i=0
	until grep -q -E "$3" $o || [ $i -ge $((120 * scale)) ]; do sleep 1; i=$((i + 1)); done
	kill $p 2>/dev/null
	echo "$n=$(tr '\n' ' ' <$o)"
	echo "$n.wait=$i"
	rm -f $o
}
app_install() {
	ipk=$1 id=$2
	d=/media/developer/temp
	mkdir -p $d && cp "$ipk" $d/tvsuite.ipk
	echo "devmode_enabled=$([ -e /var/luna/preferences/devmode_enabled ] && echo yes || echo no)"
	dev_call install luna://com.webos.appInstallService/dev/install \
		"{\"id\":\"$id\",\"ipkUrl\":\"$d/tvsuite.ipk\",\"subscribe\":true}" \
		'"state": *"(installed|install failed)"|"returnValue": *false'
	rm -f $d/tvsuite.ipk
	lq appinfo $SAM/getAppInfo "{\"id\":\"$id\"}"
	echo "TV FACTS END"
}
app_remove() {
	id=$1
	dev_call remove luna://com.webos.appInstallService/dev/remove \
		"{\"id\":\"$id\",\"subscribe\":true}" '"state": *"(removed|remove failed)"|"returnValue": *false'
	lq appinfo $SAM/getAppInfo "{\"id\":\"$id\"}"
	echo "TV FACTS END"
}

tee_step() {
	lib=$1 sstore=$2
	shift 2
	probe=/mnt/host/build/tests/regress/tee/tee-probe
	if [ -c /dev/tee0 ]; then echo "tee_dev=char"; else echo "tee_dev=$(ls -l /dev/tee0 2>&1 | cut -c1-60)"; fi
	$T $((60 * scale)) $probe "$lib" open "$sstore" "$@" 2>&1
	echo "probe_open_rc=$?"
	$T $((120 * scale)) $probe "$lib" sstore "$sstore" 2>&1
	echo "probe_sstore_rc=$?"
	echo "crashes=$(ls /var/log/reports/librdx 2>/dev/null | sed -n 's/^RDXDErr[0-9]*_\([^_]*\)__.*\.\([0-9][0-9]*\)\.[^.]*\.gz$/\1.\2/p' | tr '\n' ' ')"
	t=
	for f in /var/log/reports/librdx/*.gz; do
		[ -f "$f" ] || continue
		gzip -dc "$f" 2>/dev/null | grep -q 'TEEC_\|SStore\|sstore' &&
			t="$t${t:+ }$(basename "$f" | sed -n 's/^RDXDErr[0-9]*_\([^_]*\)__.*\.\([0-9][0-9]*\)\.[^.]*\.gz$/\1.\2/p')"
	done
	echo "tee_crashes=$t"
	echo "TV FACTS END"
}

histogram() {
	suite=$1
	probe=/mnt/host/build/tests/regress/histogram/hist-probe.so
	dev=/var/luna/preferences/vt_devmode
	echo "crashes_before=$(ls /var/log/reports/librdx 2>/dev/null | sed -n 's/^RDXDErr[0-9]*_\([^_]*\)__.*\.\([0-9][0-9]*\)\.[^.]*\.gz$/\1.\2/p' | tr '\n' ' ')"
	made=
	if [ -e $dev ]; then echo "vt_devmode=existed"; else touch $dev && made=1; echo "vt_devmode=${made:+made}"; fi
	o=/tmp/tvsuite-histogram.$$
	HIST_PROBE=1 HIST_WAIT_MS=$((2000 * scale)) LD_PRELOAD=$probe $T $((60 * scale)) "$suite" </dev/null >$o 2>&1
	echo "probe_rc=$?"
	grep -E '^hist\.|LSRegister|rror|denied|refused' $o | cut -c1-20000
	rm -f $o
	[ -z "$made" ] || rm -f $dev
	echo "vt_devmode_after=$([ -e $dev ] && echo yes || echo no)"
	echo "crashes=$(ls /var/log/reports/librdx 2>/dev/null | sed -n 's/^RDXDErr[0-9]*_\([^_]*\)__.*\.\([0-9][0-9]*\)\.[^.]*\.gz$/\1.\2/p' | tr '\n' ' ')"
	echo "TV FACTS END"
}

# the audio step: the Audio API's own calls as an app makes them (the public hub where there are
# two), one tone per phase through PulseAudio (paplay, a native client's path), a pause after each
# so the host's recording keeps the phases apart; read-back with getVolume after every change
ap() {
	n=$1; shift
	echo "$n=$($T $((20 * scale)) $P -n 1 "$@" </dev/null 2>&1 | tr -d '\n')"
}
audio_step() {
	d=$1 svc=$2 k=$3 rb=${4:-$2/getVolume} hw=$5
	# the read-back (the expected table's audio readback): webOS 5 on, audiod's master/getVolume for
	# the active sound output, as the Settings app asks it (soundOutput given)
	rbp='{}'
	case $rb in */master/*)
		so=$($T $((20 * scale)) luna-send -n 1 luna://com.webos.service.audio/getSoundOutput '{}' </dev/null 2>&1 |
			sed -n 's/.*"soundOutput":"\([^"]*\)".*/\1/p')
		echo "sound_output=$so"
		rbp="{\"soundOutput\":\"$so\"}" ;;
	esac
	echo "pulse=$(pidof pulseaudio)"
	echo "sinks=$(pactl list short sinks 2>&1 | awk '{print $2}' | tr '\n' ' ')"
	echo "default_sink=$(pactl info 2>&1 | sed -n 's/^Default Sink: //p')"
	play() {
		sleep 1
		o=$($T $((30 * scale)) paplay "$d/$1.wav" 2>&1); r=$?
		echo "play.$1=$(echo "$o" | tr '\n' ' ')rc=$r"
		sleep 1
	}
	# a master volume change first (down and up: the same volume): until one, W18R's and W19P's
	# audiod gives the first app stream 90 % of pdefaultapp's volume (palm-policy's "requested
	# volume is 90"), every later one 100 %
	ap warm.down $svc/volumeDown '{}'
	ap warm.up $svc/volumeUp '{}'
	ap vol.native $rb "$rbp"
	play native
	# the same volume through ALSA: its default (LG's asound.conf: the pulse plugin, the route a
	# browser's ALSA output takes), then pcm_output's own PCM, which PulseAudio holds until its
	# suspend-on-idle lets it go
	sleep 1
	o=$($T $((30 * scale)) aplay -q "$d/aplay.wav" 2>&1); r=$?
	echo "play.aplay=$(echo "$o" | tr '\n' ' ')rc=$r"
	sleep 1
	if [ -n "$hw" ] && [ "$hw" != - ]; then
		i=0
		until pactl list short sinks 2>/dev/null | grep -q "pcm_output.*SUSPENDED"; do
			[ $i -ge $((20 * scale)) ] && break
			sleep 1; i=$((i + 1))
		done
		echo "hw.suspend_wait=$i"
		o=$($T $((30 * scale)) aplay -q -D "$hw" "$d/hw.wav" 2>&1); r=$?
		echo "play.hw=$(echo "$o" | tr '\n' ' ')rc=$r"
		sleep 1
	fi
	i=0; while [ $i -lt $k ]; do ap down.$i $svc/volumeDown '{}'; i=$((i + 1)); done
	ap vol.down $rb "$rbp"
	play down
	ap mute $svc/setMuted '{"muted":true}'
	ap vol.muted $rb "$rbp"
	play muted
	ap unmute $svc/setMuted '{"muted":false}'
	i=0; while [ $i -lt $k ]; do ap up.$i $svc/volumeUp '{}'; i=$((i + 1)); done
	ap vol.restored $rb "$rbp"
	play restored
	echo "TV FACTS END"
}

# decode_log CASE: what the page logged of CASE's last run where its console reaches PmLog (the
# host has its beacons in any case), the media stack's error lines among the last 400 of messages
# (PmLog) and the sinks' among legacy-log's last 40 (their plain syslog), the crash reports
decode_log() {
	c=$1
	awk -v k="TVSUITE decode $c " 'index($0, k "start") { n = 0; split("", l) } index($0, k) { l[n++] = substr($0, index($0, k) + length(k)) }
		END { for (i = 0; i < n; i++) print "page=" l[i] }' /var/log/messages 2>/dev/null
	tail -n 400 /var/log/messages 2>/dev/null | grep -v TVSUITE |
		grep -E '[Ee]rror|ERROR|[Ff]ail|FAIL|not support|NOT_SUPPORT|vsm register' |
		grep -E 'media|pipeline|umediaserver|uMediaServer|gst|GST|vdec|adec|sink|omx|OMX|VDEC|lpbin|MediaPlayer|SourceBuffer' |
		tail -n 30 | cut -c1-300 | sed 's/^/media=/'
	tail -n 40 /var/log/legacy-log 2>/dev/null | grep -E 'vdecsink|adecsink|mediaport' | tail -n 8 | cut -c1-300 | sed 's/^/sink=/'
	echo "crashes=$(ls /var/log/reports/librdx 2>/dev/null | sed -n 's/^RDXDErr[0-9]*_\([^_]*\)__.*\.\([0-9][0-9]*\)\.[^.]*\.gz$/\1.\2/p' | tr '\n' ' ')"
	echo "TV FACTS END"
}

# smp_lines LOG: LOG's SMPCHECK lines, with those of its newest rotated file first: the guest
# rotates /var/log/messages every few minutes, and a rotation inside the check's window would
# otherwise drop its first lines (LOADCOMPLETED among them)
smp_lines() {
	old=$(ls -t "$1".* 2>/dev/null | head -n 1)
	case $old in
	*.gz) zcat "$old" 2>/dev/null ;;
	?*) cat "$old" 2>/dev/null ;;
	esac | grep 'SMPCHECK'
	grep 'SMPCHECK' "$1" 2>/dev/null
}
# the SMP check app (tests/regress/smp/smpcheck.c) logs every line with "SMPCHECK" through PmLog and syslog
# and ends with "SMPCHECK frames=..." (or "SMPCHECK no...": it could not run); PmLog's lines are in
# messages, but webOS 5's client whitelist drops them, and then legacy-log has them (syslog's)
smp() {
	id=$1 secs=$2
	echo "crashes_before=$(ls /var/log/reports/librdx 2>/dev/null | sed -n 's/^RDXDErr[0-9]*_\([^_]*\)__.*\.\([0-9][0-9]*\)\.[^.]*\.gz$/\1.\2/p' | tr '\n' ' ')"
	lq launch $SAM/launch "{\"id\":\"$id\"}"
	i=0 log=
	until [ -n "$log" ] || [ $i -ge $((secs * scale)) ]; do
		for f in /var/log/messages /var/log/legacy-log; do
			smp_lines $f | grep -q -E 'SMPCHECK (frames=|no)' && { log=$f; break; }
		done
		[ -n "$log" ] || { sleep 1; i=$((i + 1)); }
	done
	echo "wait=$i"
	smp_lines ${log:-/var/log/messages} | sed 's/^.* SMPCHECK \({} \)\{0,1\}//; s/^/smp=/' | cut -c1-300 | tail -n 400
	lq close $SAM/closeByAppId "{\"id\":\"$id\"}"
	echo "crashes=$(ls /var/log/reports/librdx 2>/dev/null | sed -n 's/^RDXDErr[0-9]*_\([^_]*\)__.*\.\([0-9][0-9]*\)\.[^.]*\.gz$/\1.\2/p' | tr '\n' ' ')"
	echo "TV FACTS END"
}

case $1 in
luna-info) luna_info ;;
network) network "$2" "$3" ;;
app-launch) app_launch "$2" "$3" "$4" "$5" "$6" ;;
app-state) app_state "$2" ;;
app-close) app_close "$2" "$3" "$4" ;;
app-install) app_install "$2" "$3" ;;
app-remove) app_remove "$2" ;;
tee) shift; tee_step "$@" ;;
histogram) histogram "$2" ;;
audio) audio_step "$2" "$3" "$4" "$5" "$6" ;;
decode-log) decode_log "$2" ;;
smp) smp "$2" "$3" ;;
*) echo "unknown step $1"; exit 1 ;;
esac
