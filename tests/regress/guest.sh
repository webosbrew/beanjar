#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The regression runner's guest side (scripts/regress puts it into the VM's share with `host/scripts/vm-launcher
# put` and types it on the serial console, in the background). Runs in the initramfs debug shell:
#   sh /host/tests/regress/guest.sh boot SECONDS      wait for boot-done: "BOOTDONE <guest uptime>"
#                                                     (boot and ready add /proc/meminfo's totals)
#   sh /host/tests/regress/guest.sh ready SECONDS     headless: wait for core-boot-done from tvservice's
#                                                     tv-ready: "READY <guest uptime> <bootd's time
#                                                     of core-boot-done> <tvservice's pids>"
#                                                     (every RELEASE process, sorted, comma-joined;
#                                                     "none" in a firmware with no TV service)
#   sh /host/tests/regress/guest.sh tv-ready SECONDS  graphics, after boot-done: bootd's log has
#                                                     tvservice's tv-ready: "TVREADY <bootd's time>",
#                                                     or "TVREADY SKIP <why>" where none is sent
#   sh /host/tests/regress/guest.sh tvservice PIDS    tvservice (RELEASE) still processes PIDS (as
#                                                     ready printed them), answering
#   sh /host/tests/regress/guest.sh check-m1 LOGNAME [WAIT]  guest/scripts/check-m1.sh (WAIT for boot-done,
#                                                     default 10), its logs to out/logs/LOGNAME
#   sh /host/tests/regress/guest.sh app-calls [FW]    tests/regress/tvservice/app-calls.sh (put with this file), the firmware name
#   sh /host/tests/regress/guest.sh home-idle SECONDS wait until the compositor shows no Home
#                                                     launcher: "HOMEIDLE <guest uptime> <state>",
#                                                     or "HOMESHOWN ..." if it stays (webOS 4.5)
#   sh /host/tests/regress/guest.sh standby-prep      Quick Start+ on, out of factory state:
#                                                     "STANDBY READY <tvservice's pids> <tvpowerd's
#                                                     pid>", or "STANDBY SKIP ..." where the
#                                                     firmware or the model has no Quick Start+
#   sh /host/tests/regress/guest.sh power-off         tvpower's powerOff (reason remoteKey), a
#                                                     second later, in the background (headless)
#   sh /host/tests/regress/guest.sh resumed PIDS PID [SECONDS]  after the wake: tvpowerd Active
#                                                     again (within SECONDS, default 30), the same
#                                                     tvservice (PIDS) and tvpowerd (PID) answering:
#                                                     "RESUMED <s from the wake line to Active>"
#   sh /host/tests/regress/guest.sh luna-info         scripts/regress --tv: tests/regress/tv.sh luna-info
#                                                     (put with this file), its facts
#   sh /host/tests/regress/guest.sh network URL NAME  scripts/regress --tv: tests/regress/tv.sh network
#   sh /host/tests/regress/guest.sh app-* ARGS...     scripts/regress --tv's apps and draw steps:
#                                                     tests/regress/tv.sh app-* (launch, state,
#                                                     close, install, remove)
#   sh /host/tests/regress/guest.sh tee LIB SSTORE TA...  scripts/regress --tv's tee step:
#                                                     tests/regress/tv.sh tee (with build/tests/regress/tee)
#   sh /host/tests/regress/guest.sh audio DIR SVC N RB HW  scripts/regress --tv's audio step:
#                                                     tests/regress/tv.sh audio (the host records)
#   sh /host/tests/regress/guest.sh decode-log CASE   scripts/regress --tv's decode step: tests/regress/tv.sh
#                                                     decode-log (the page's and the media stack's lines)
#   sh /host/tests/regress/guest.sh smp ID SECONDS    scripts/regress --tv's smp step: tests/regress/tv.sh
#                                                     smp (the SMP check app's run and its lines)
#   sh /host/tests/regress/guest.sh idle-cpu SECONDS WINDOWS  every process's and thread's CPU and
#                                                     context switches, WINDOWS+1 snapshots SECONDS
#                                                     apart (judged by tests/regress/lib/idlecpu.py)
#   sh /host/tests/regress/guest.sh idle-threads TIDS SAMPLES  threads TIDS (comma-joined):
#                                                     state, wchan, syscall, SAMPLES times
#   sh /host/tests/regress/guest.sh checklist [LASTINPUT]  the bring-up checklist's facts, one
#                                                     "name=value" a line, judged on the host
#                                                     (tests/regress/lib/checklist.py); LASTINPUT: the
#                                                     EEPROM's Last_Input_info DB offset (0: none)
# Each step's output goes to /host/out/regress/STEP.txt and its exit status to STEP.rc, written
# last: the host waits for STEP.rc (RUN_DIR/share/out/regress on the VM host).
# TIME_SCALE (the environment, default 1; scripts/regress passes its qemu.TIME_SCALE, 4 under TCG)
# stretches every limit of the step's own (its luna-send timeouts and poll budgets) and goes on to
# check-m1.sh and app-calls.sh; the limits given as arguments come already stretched.
out=/host/out/regress
scale=${TIME_SCALE:-1}
step=$1
. "${0%/*}/lb-exit.sh"  # put with this file (the initramfs busybox has no dirname)
. "${0%/*}/idle-cpu.sh"  # the idle-cpu and idle-threads steps, put with this file too
# checklist's webOS half (checklist-webos, run through webos-enter, where /host is /mnt/host)
[ "$step" = checklist-webos ] || mkdir -p $out
# wt SECS PROG ARGS...: PROG inside webOS under the firmware's busybox timeout, which takes -t SECS
# before busybox 1.30 (webOS 6 and older) and a plain SECS from webOS 7 (busybox 1.31) on. The
# timeout stays inside webos-enter: nsenter -p forks, so one outside would orphan PROG. The
# syntax is found once webOS is up and kept in the initramfs's /run for the steps after.
wt() {
	f=/run/regress-timeout
	[ -s $f ] || { if webos-enter timeout -t 1 true </dev/null >/dev/null 2>&1; then echo "timeout -t" > $f
		elif webos-enter timeout 1 true </dev/null >/dev/null 2>&1; then echo timeout > $f
		else return 1; fi; }
	webos-enter $(cat $f) "$@"
}
up() { cut -d' ' -f1 /proc/uptime; }
# the guest's RAM at that point, after the step's mark line
mem() { grep -E '^(MemTotal|MemFree|MemAvailable|Cached|SwapTotal):' /proc/meminfo; }
boot() { # Normal (LAYERS=normal,...) ends at boot-done; FirstUse never sends it
	end=$(($(up | cut -d. -f1) + $1))
	while [ "$(up | cut -d. -f1)" -lt $end ]; do
		s=$(wt $((10 * scale)) luna-send -n 1 luna://com.webos.bootManager/getBootStatus '{}' </dev/null 2>/dev/null)
		case $s in *'"boot-done":true'*) echo "BOOTDONE $(up)"; mem; return 0 ;; esac
		sleep 1
	done
	echo "no boot-done within $1 s; last status: $s"
	return 1
}
# Headless (scripts/regress --headless): Normal's boot-done comes only after the first app (the
# headless Live TV stand-in, which sends banner-ready at its launch) and the CPU-idle check. So
# headless waits for the first boot signal instead, core-boot-done, and requires that
# tvservice's tv-ready made it rather than bootd's timeout (30 s on webOS 1 and 2, 60 s on 3 and 4):
# bootd logs the Luna generateSignal in /var/log/bootd.log ("service_generateSignal=Payload(
# {"name":"tv-ready"})" on webOS 1 and 2, "Handle-generateSignal/Request({"name":"tv-ready"})" on 3
# and 4). core-boot-done without it fails at once. boot-done itself is check-m1's to wait for.
# webOS 5 (W20L) has no tvservice: its TV service is legacy-broadcast, and its bootd sends
# core-boot-done without waiting for tv-ready (the first app's utp.boot answer, 3 s in, before
# legacy-broadcast's tv-ready at 7 s on KVM); there ready waits for the tv-ready line itself.
in_webos() { wt $((10 * scale)) sh -c "$1" </dev/null 2>/dev/null; }
# every tvservice (RELEASE, webOS 5's legacy-broadcast) process as one token, sorted and
# comma-joined, so the sets compare (sorted in webOS: the initramfs's busybox has no sort)
# (webOS 6's is one of legacy-broadcast-atsc, -dvb, -arib, which legacy-broadcast.sh runs)
LB="legacy-broadcast legacy-broadcast-atsc legacy-broadcast-dvb legacy-broadcast-arib"
release_pids() { in_webos "pidof RELEASE $LB | tr ' ' '\n' | grep . | sort -n" | tr '\n' ',' | sed 's/,$//'; }
# webOS 5 and 6: no tvservice pak, legacy-broadcast in the rootfs (webOS 6: its script)
webos5() { in_webos "test -x /usr/sbin/legacy-broadcast || test -x /etc/systemd/system/scripts/legacy-broadcast.sh"; }
# no TV service at all: neither a tvservice pak nor legacy-broadcast (N21D, the tunerless webOS 24
# StanbyME: tv.systemproperty is tvservicemock, and nothing sends tv-ready, which its bootd does
# not wait for). There core-boot-done is the readiness, and tvservice's check is tvservicemock's answer.
no_tv() { ! webos5 && ! in_webos "ls /mnt/tvservice 2>/dev/null | grep -q ."; }
# a tunerless set's legacy-broadcast (N21D's webOS 6 StanbyME, an NMRM set: tv.hw.supportNMRM, from
# modelopt= digit 8, makes API_FE_IsTunerLessModel true): MAIN_FE_PreInitTask logs this and returns
# before tv_sysonoff_postFeInit, the only post of the condition tStartTune waits on without a
# timeout, so tStartTune and then tPostDbInit's LastInput LS2 init block and legacy-broadcast never
# sends tv-ready, as on the set. Its bootd boots on core-boot-done.
fe_tunerless() { in_webos "grep -q 'PreInitTask. Do Not Create task because it.s tunerless model' /var/log/legacy-broadcast-booting.log"; }
ready() {
	end=$(($(up | cut -d. -f1) + $1))
	while [ "$(up | cut -d. -f1)" -lt $end ]; do
		s=$(wt $((10 * scale)) luna-send -n 1 luna://com.webos.bootManager/getBootStatus '{}' </dev/null 2>/dev/null)
		case $s in *'"core-boot-done":true'*)
			t=$(up)
			# bootd writes the signal's line before it sends core-boot-done; allow for buffering
			i=0; while [ $i -lt $((3 * scale)) ]; do i=$((i + 1))
				log=$(in_webos "grep -E 'generateSignal.*\"name\":\"tv-ready\"|SIGNAL_core-boot-done' /var/log/bootd.log")
				case $log in *tv-ready*) break ;; esac
				sleep 1
			done
			core=$(printf '%s\n' "$log" | sed -n 's/^[A-Z]*( *\([0-9.]*\)).*SIGNAL_core-boot-done.*/\1/p' | head -n 1)
			case $log in *tv-ready*)
				echo "READY $t ${core:-?} $(release_pids)"; mem; return 0 ;;
			esac
			if webos5; then         # tv-ready comes after core-boot-done: wait for it
				while [ "$(up | cut -d. -f1)" -lt $end ]; do
					log=$(in_webos "grep -E 'generateSignal.*\"name\":\"tv-ready\"' /var/log/bootd.log")
					case $log in *tv-ready*)
						tv=$(printf '%s\n' "$log" | sed -n 's/^[A-Z]*( *\([0-9.]*\)).*/\1/p' | head -n 1)
						echo "READY $(up) ${tv:-?} $(release_pids)"; mem; return 0 ;;
					esac
					if fe_tunerless; then
						echo "tunerless: legacy-broadcast's front end did not start (its booting log), so it sends no tv-ready; ready at core-boot-done"
						echo "READY $t ${core:-?} $(release_pids)"; mem; return 0
					fi
					sleep 1
				done
				echo "no tv-ready within $1 s (core-boot-done at ${core:-?} s, bootd's clock)"
				echo "tvservice (legacy-broadcast): $(release_pids | grep . || echo not running)"
				return 1
			fi
			if no_tv; then
				echo "READY $t ${core:-?} none"; mem; return 0
			fi
			echo "core-boot-done at ${core:-?} s (bootd's clock) without tvservice's tv-ready in /var/log/bootd.log: bootd's timeout"
			echo "tvservice (RELEASE): $(release_pids | grep . || echo not running)"
			echo "$s"
			return 1 ;;
		esac
		sleep 1
	done
	echo "no core-boot-done within $1 s; last status: $s"
	return 1
}
# Graphics (scripts/regress's tv-ready step, after boot-done): bootd's log has tvservice's tv-ready,
# within SECONDS: "TVREADY <bootd's time of it>". Headless's ready requires it already. On the same
# evidence as ready, a firmware with no TV service (no_tv) and a tunerless legacy-broadcast
# (fe_tunerless) send none, as on the set: "TVREADY SKIP <why>".
tv_ready() {
	end=$(($(up | cut -d. -f1) + $1))
	while :; do
		log=$(in_webos "grep -E 'generateSignal.*\"name\":\"tv-ready\"' /var/log/bootd.log")
		case $log in *tv-ready*)
			tv=$(printf '%s\n' "$log" | sed -n 's/^[A-Z]*( *\([0-9.]*\)).*/\1/p' | head -n 1)
			echo "TVREADY ${tv:-?}"; return 0 ;;
		esac
		if no_tv; then
			echo "TVREADY SKIP no TV service (neither a tvservice pak nor legacy-broadcast): nothing sends tv-ready"
			return 0
		fi
		if webos5 && fe_tunerless; then
			echo "TVREADY SKIP tunerless: legacy-broadcast's front end did not start (its booting log), so it sends no tv-ready"
			return 0
		fi
		[ "$(up | cut -d. -f1)" -lt $end ] || break
		sleep 2
	done
	echo "no tv-ready in /var/log/bootd.log within $1 s"
	echo "tvservice: $(release_pids | grep . || echo not running)"
	if webos5; then
		echo "legacy-broadcast's booting log ends:"
		in_webos "tail -n 3 /var/log/legacy-broadcast-booting.log"
	fi
	return 1
}
# tvservice still the processes it was at ready (none dead, none respawned) and answering on Luna
# A legacy-broadcast may stop itself: W22O mullet's (04.40.93) tv_sysonoff_checkStopService ends it
# (stopService, exit status 0) about 10 s after tv-ready when the set is idle, and the next call
# starts it again (W23L's runs on demand the same way). lb-exit.sh judges its exits from systemd's
# result for the boot instance and the hub's LSHUB_SRV_EXIT for those it started since; either way
# no crash report may name it.
lb_exits_clean() {
	u=$(in_webos "systemctl show $LB_UNIT -p Result -p ExecMainStatus -p NRestarts -p ActiveState" | tr '\n' ' ')
	echo "$LB_UNIT: ${u:-no answer from systemd}"
	lb_unit_clean "$u" || return 1
	# the hub's log rotates within minutes (messages, messages.0.gz, ...): every file of it
	in_webos 'for f in /var/log/messages*; do case $f in *.gz) zcat $f ;; *) cat $f ;; esac; done | grep LSHUB_SRV_EXIT' |
		lb_hub_exits_clean || return 1
	in_webos "ls /var/log/reports/librdx /var/log/crashd 2>/dev/null" | lb_crash_free
}
lb_call() {
	r=$(wt $((30 * scale)) luna-send -n 1 luna://com.webos.service.legacybroadcast.broadcast/getCurrentChannel '{}' </dev/null 2>&1)
	echo "legacybroadcast.broadcast getCurrentChannel: $r; pids now $(release_pids)"
	echo "$r" | tr -d ' ' | grep -q '"returnValue":' && [ -n "$(release_pids)" ]
}
lb_stopped_itself() { # PIDS
	lb_exits_clean || return 1
	echo "pids $1 stopped themselves (exit status 0); a call starts it again:"
	lb_call
}
# PIDS (at ready) gone and others running: it stopped itself and a call since (app-calls) started
# it again. Only legacy-broadcast does that, never tvservice (RELEASE).
lb_started_again() { # PIDS NOW
	for p in $(echo "$1" | tr ',' ' '); do
		case ",$2," in *",$p,"*) echo "pid $p still runs beside new ones"; return 1 ;; esac
	done
	if [ -n "$(in_webos 'pidof RELEASE')" ]; then
		echo "RELEASE (tvservice) runs as new pids: respawned"; return 1
	fi
	lb_exits_clean || return 1
	echo "pids $1 stopped themselves (exit status 0), started again since as $2:"
	lb_call
}
tvservice() {
	now=$(release_pids)
	echo "tvservice (RELEASE or legacy-broadcast): pids at ready ${1:-?}, now ${now:-none}"
	r=$(wt $((10 * scale)) luna-send -n 1 luna://com.webos.service.tv.systemproperty/getSystemInfo '{"keys":["modelName"]}' </dev/null 2>&1)
	echo "tv.systemproperty getSystemInfo: $r"
	if ! echo "$r" | tr -d ' ' | grep -q '"returnValue":true'; then
		echo "TVSERVICE FAIL"; return 1
	elif [ -n "$now" ] && [ "$now" = "$1" ]; then
		echo "TVSERVICE PASS"
	elif [ "$1" = none ] && [ -z "$now" ] && no_tv; then
		echo "TVSERVICE PASS (no TV service in this firmware; tvservicemock answers)"
	elif [ -z "$now" ] && [ -n "$1" ] && lb_stopped_itself "$1"; then
		echo "TVSERVICE PASS (stopped itself, started again on call)"
	elif [ -n "$now" ] && [ -n "$1" ] && [ "$1" != none ] && lb_started_again "$1" "$now"; then
		echo "TVSERVICE PASS (stopped itself, started again by a call since)"
	else
		echo "TVSERVICE FAIL"; return 1
	fi
}
# Quick Start+ (docs/dev/architecture/suspend.md): the firmware offers it where tvpowerd opens /dev/lgib
# (W15M on; WT1H's has no such string) and the model where tvservice's instantBooting says so. The
# setting is on, and the set out of factory state (systemproperty checkFactoryState: tvpowerd
# refuses the suspend in it).
standby_prep() {
	if ! in_webos "grep -q /dev/lgib /usr/sbin/tvpowerd"; then
		echo "STANDBY SKIP: tvpowerd has no /dev/lgib (no Quick Start+ on this firmware)"; return 0
	fi
	# the model's (ToolOpt7's instant boot, as tvservice gives it to tvpowerd): "quickStartPlus"
	# (webOS 3 on) or "true" (webOS 2); W15M's 42LF652V-ZA says "false"
	ib=$(in_webos "luna-send -n 1 luna://com.webos.service.tv.systemproperty/getProperties '{\"keys\":[\"instantBooting\"]}'")
	case $(echo "$ib" | tr -d ' ') in
	*'"instantBooting":"quickStartPlus"'*|*'"instantBooting":"true"'*) ;;
	*) echo "STANDBY SKIP: the model has no Quick Start+ (instantBooting: $ib)"; return 0 ;;
	esac
	r=$(in_webos "luna-send -n 1 luna://com.webos.settingsservice/setSystemSettings '{\"category\":\"option\",\"settings\":{\"quickStartMode\":\"on\"}}'")
	echo "quickStartMode on: $r"
	f=$(in_webos "luna-send -n 1 luna://com.webos.service.tv.systemproperty/getProperties '{\"keys\":[\"checkFactoryState\"]}'")
	echo "checkFactoryState: $f"
	echo "$r" | tr -d ' ' | grep -q '"returnValue":true' || { echo "STANDBY FAIL: quickStartMode not set"; return 1; }
	echo "$f" | tr -d ' ' | grep -q '"checkFactoryState":"false"' || { echo "STANDBY FAIL: factory state"; return 1; }
	echo "STANDBY READY $(release_pids) $(in_webos 'pidof tvpowerd')"
}
power_off() {
	webos-enter sh -c "(sleep 1; luna-send -n 1 luna://com.webos.service.tvpower/power/powerOff '{\"reason\":\"remoteKey\"}') </dev/null >/dev/null 2>&1 &" </dev/null
	echo "POWEROFF requested"
}
# The resume: from the micom's wake line (lgemu's "power key on the wake line", dmesg) to tvpowerd's
# Suspend -> Active (PmLog's NL_POWER_STATE, or its POWER_STATE "current(Active)" line on webOS 3;
# the last one is the resume's; its [seconds] are the same monotonic clock).
resumed() {
	limit=${3:-30}
	end=$(($(up | cut -d. -f1) + limit))
	while :; do
		s=$(in_webos "luna-send -n 1 luna://com.webos.service.tvpower/power/getPowerState '{}'")
		case $s in *'"Active"'*) break ;; esac
		[ "$(up | cut -d. -f1)" -lt $end ] || { echo "not Active $limit s after the wake: $s"; return 1; }
		sleep 1
	done
	wake=$(dmesg | sed -n -e 's/^[^[]*\[ *\([0-9.]*\)\].*power key on the wake line: the SoC wakes.*/\1/p' -e 's/^[^[]*\[ *\([0-9.]*\)\].*the latched power key wakes the SoC.*/\1/p' | tail -n 1)
	# PmLog writes its file a little after the state changes, and may rotate it (W19P: gzip):
	# give it up to 5 s, and look in the last rotated file too
	i=0; while [ $i -lt $((6 * scale)) ]; do i=$((i + 1))
		active=$(in_webos "grep -a -E 'NL_POWER_STATE.*\"old\":\"Suspend\",\"new\":\"Active\"|tvpowerd POWER_STATE.*current\\(Active\\), processing\\(\\)' /var/log/messages.0 /var/log/messages 2>/dev/null; zcat /var/log/messages.0.gz 2>/dev/null | grep -a -E 'NL_POWER_STATE.*\"new\":\"Active\"'" |
			sed -n 's/^[^[]*\[\([0-9.]*\)\].*/\1/p' |
			awk '$1 + 0 > m + 0 { m = $1 } END { print m }')
		[ -n "$active" ] && awk -v a="$active" -v w="$wake" 'BEGIN { exit !(a + 0 > w + 0) }' && break
		sleep 1
	done
	now=$(release_pids)
	pd=$(in_webos 'pidof tvpowerd')
	r=$(in_webos "luna-send -n 1 luna://com.webos.service.tv.systemproperty/getSystemInfo '{\"keys\":[\"modelName\"]}'")
	echo "power state: $s"
	echo "wake line at ${wake:-?} s, Active at ${active:-?} s (guest clock)"
	echo "tvservice (RELEASE): pids before ${1:-?}, now ${now:-none}; tvpowerd before ${2:-?}, now ${pd:-none}"
	echo "tv.systemproperty getSystemInfo: $r"
	if [ -n "$now" ] && [ "$now" = "$1" ] && [ "$pd" = "$2" ] && echo "$r" | tr -d ' ' | grep -q '"returnValue":true'; then
		echo "RESUMED $(awk -v a="$active" -v w="$wake" 'BEGIN { if (a != "" && w != "") printf "%.2f", a - w; else print "?" }')"
	else
		echo "RESUME FAIL"; return 1
	fi
}
# webOS 3.5's Home Auto Launch shows the launcher by itself for a few seconds after the first app
# (W17H: about 26-34 s after boot), and a HOME key then hides it; the screenshot step presses
# HOME once that has passed (the other targets' compositors answer "none" by then). A compositor
# without getCurrentState gives no state and is not waited for. webOS 4.5's Home (W19P's
# com.webos.app.fullhome) is a window of type HOME over the first app, with the compositor's state
# "none"; it comes up by itself after boot and stays. Still shown at the end of SECONDS:
# "HOMESHOWN", and the host presses HOME twice (hide, show).
home_shown() {
	s=$(wt $((10 * scale)) luna-send -n 1 luna://com.webos.surfacemanager/getCurrentState '{}' </dev/null 2>/dev/null | tr -d ' \n')
	state=$(printf %s "$s" | sed -n 's/.*"currentState":"\([^"]*\)".*/\1/p')
	[ "$state" = home ] && return 0
	w=$(wt $((10 * scale)) luna-send -n 1 luna://com.webos.surfacemanager/getForegroundAppInfo '{}' </dev/null 2>/dev/null)
	case $w in *_WEBOS_WINDOW_TYPE_HOME*) state=home-window; return 0 ;; esac
	return 1
}
home_idle() {
	end=$(($(up | cut -d. -f1) + $1))
	while home_shown; do
		[ "$(up | cut -d. -f1)" -lt $end ] || { echo "HOMESHOWN $(up) $state"; return 0; }
		sleep 1
	done
	echo "HOMEIDLE $(up) ${state:-n/a}"
}
# The bring-up checklist's facts (scripts/regress's checklist step; tests/regress/lib/checklist.py
# judges them on the host, docs/dev/guides/build.md "Bring-up checklist"). Facts only, one "name=value" per
# line, a name repeated for a list. This half runs in the initramfs (its busybox: no wc or sort):
# the kernel, the modules boot.conf loads and their vermagic, the taint, and the kernel log's
# lines that matter (stale modules, Upstart's word on tvservice, mount-readwrite and any job killed
# by a crash signal); then the webOS half below.
checklist() {
	echo "kernel=$(uname -r)"
	echo "tainted=$(cat /proc/sys/kernel/tainted)"
	soc=$(sed -n 's/^SOC_MODULES="\(.*\)"$/\1/p' /host/boot.conf)
	for m in lgemu $soc; do
		name=$(echo $m | tr - _)
		grep -q "^$name " /proc/modules && st=loaded || st=absent
		vm=$(grep -a -o 'vermagic=[^[:cntrl:]]*' /lib/modules/$m.ko 2>/dev/null | head -n 1 | cut -d= -f2 | cut -d' ' -f1)
		echo "module=$m $st ${vm:-none}"
	done
	dmesg | grep -E 'disagrees about version|Unknown symbol|version magic|i2c adapter has no name|i2c_adapters: cannot register|init: tvservice |init: mount-readwrite [a-z-]* *process .*(terminated|killed)|killed by (SEGV|ABRT|BUS|ILL|FPE|TRAP|SYS) signal' |
		sed 's/^\[[^]]*\] *//; s/^/dmesg=/' | head -n 60
	webos-enter env TIME_SCALE=$scale sh /mnt/host/tests/regress/guest.sh checklist-webos "${1:-0}" </dev/null 2>&1
	echo "CHECKLIST FACTS END"
}
# The webOS half (the firmware's busybox, which has dd and od): what tvservice and the settings say
# (model name, broadcast system, country, groups, service country), tvconfig's selectors (webOS 3
# on), bootd's first app, the EEPROM's Last_Input_info DB mark at offset $1 (the disk whose serial
# is "eeprom"), mount-readwrite's end (its flag file, the pm_freeze_timeout it writes, preload/
# where it reads it), crash reports and core files.
checklist_webos() {
	if timeout -t 1 true 2>/dev/null; then T="timeout -t"; else T=timeout; fi
	lq() { $T $((20 * scale)) luna-send -n 1 "$@" </dev/null 2>&1 | tr -d '\n'; }
	echo "sysinfo=$(lq luna://com.webos.service.tv.systemproperty/getSystemInfo \
		'{"keys":["modelName","tvSystemName","contiLangCountry","boardType"]}')"
	echo "settings=$(lq luna://com.webos.settingsservice/getSystemSettings \
		'{"category":"option","keys":["country","countryGroup","smartServiceCountryCode2","smartServiceCountryCode3"]}')"
	echo "bootstatus=$(lq luna://com.webos.bootManager/getBootStatus '{}')"
	# luna-prefs' system properties: all or nothing, storageCapacity from mmcblk0's /proc/partitions line
	echo "sysprops=$(lq luna://com.palm.preferences/systemProperties/getAllSysProperties '{}')"
	# the board name behind boardType: what RELEASE.cfg says, and what nyx makes of it
	echo "main_board=$(sed -n 's/^eMainBoardType,//p' /mnt/lg/model/RELEASE.cfg 2>/dev/null | head -n 1)"
	echo "nyx_board_type=$(nyx-cmd DeviceInfo query board_type 2>&1 | tr '\n' ' ' | sed -n 's/.*\(Error [0-9]* in executing query\).*/\1/p;t;s/^ *\([A-Za-z0-9_]*\) *$/\1/p' | head -n 1)"
	# webOS 5's bootd keeps its first app's id as the template (its log: "Set first action (Input
	# App) appId (${Last Input App})") and getBootStatus answers firstAppId "": the id it launched.
	# An NMRM set's bootd (MODEL_NMRM: NmrmBootSequencer's doNmrmAppLaunching) launches Home without
	# boot params and also answers firstAppId "" ("Set firstApp() Launched."): its log's "Set first
	# action (Nmrm App) appId (com.webos.app.home)"
	echo "firstapp_launch=$({ sed -n 's/.*Request : {"id":"\([^"]*\)",.*"params":{"boot":true.*/\1/p' /var/log/bootd.log
		sed -n 's/.*Set first action (Nmrm App) appId (\([^)]*\)).*/\1/p' /var/log/bootd.log; } 2>/dev/null | head -n 1)"
	for f in continentname rmmgroup rmmcountry; do
		[ -f /var/run/tvconfig/selectors/$f ] && echo "tvconfig_$f=$(cat /var/run/tvconfig/selectors/$f)"
	done
	echo "cmdline_model=$(tr ' ' '\n' </proc/cmdline | sed -n 's/^modelName=//p' | head -n 1)"
	# a global firmware's broadcast line is the board's: nyx-option-gen's (webOS 6)
	n=/tmp/var/run/tvconfig/nyx/tv.nyx.json
	[ -f $n ] && echo "nyx_system=$(sed -n 's/.*"tvBroadcastSystem": *"\([A-Z]*\)".*/\1/p' $n)"
	if [ "${1:-0}" != 0 ]; then
		e=
		for b in /sys/block/vd*; do [ "$(cat $b/serial 2>/dev/null)" = eeprom ] && e=/dev/${b##*/}; done
		mark=$([ -n "$e" ] && dd if=$e bs=1 skip=$(($1)) count=4 2>/dev/null | od -An -tx1 | tr -d ' \n')
		echo "lastinput=$1 ${mark:-unread}"
	fi
	if [ -d /run/systemd/system ]; then  # systemd (webOS 5): the unit's own state, its script
		st=$(systemctl show -p ActiveState --value mount-readwrite.service)/$(systemctl show -p Result --value mount-readwrite.service)
		echo "mrw_status=systemd $st"
		[ "$st" = active/success ] && echo "mrw_flag=mount-readwrite.service yes" || echo "mrw_flag=mount-readwrite.service no"
		conf=/etc/systemd/system/scripts/mount-readwrite.sh
	else
	echo "mrw_status=$(initctl status mount-readwrite 2>&1)"
	flag=$(initctl get-env RWMOUNT_FLAG 2>/dev/null)
	flag=${flag:-/tmp/mount-readwrite.conf.done}
	[ -e "$flag" ] && echo "mrw_flag=$flag yes" || echo "mrw_flag=$flag no"
	conf=/etc/init/mount-readwrite.conf
	fi
	want=$(sed -n 's/.*echo *\([0-9]*\) *> *\/sys\/power\/pm_freeze_timeout.*/\1/p' $conf | head -n 1)
	echo "pm_freeze_timeout=${want:-none} $(cat /sys/power/pm_freeze_timeout 2>/dev/null || echo missing)"
	grep -qE 'APPS_PRELOAD_PATH=|find /mnt/lg/appstore/preload' $conf && need=needed || need=unused
	[ -d /mnt/lg/appstore/preload ] && echo "preload=$need yes" || echo "preload=$need no"
	for d in /var/log/reports/librdx /var/log/crashd /var/spool/rdxd/pending /var/spool/rdxd/uploaded; do
		ls -1 $d 2>/dev/null | sed "s|^|crash=$d/|"
	done
	echo "core_pattern=$(cat /proc/sys/kernel/core_pattern)"
	# not webOS 5's initctl shim's event files (/tmp/<event>: /tmp/core-boot-done)
	ls -1d /core* /tmp/core* /var/log/core* /home/root/core* 2>/dev/null | grep -v '^/tmp/core-boot-done$' | sed 's/^/core=/'
}
if [ "$step" = checklist-webos ]; then
	checklist_webos "$2"
	exit 0
fi
case $step in
boot) boot "${2:-300}" ;;
ready) ready "${2:-300}" ;;
tv-ready) tv_ready "${2:-60}" ;;
tvservice) tvservice "$2" ;;
home-idle) home_idle "${2:-60}" ;;
standby-prep) standby_prep ;;
power-off) power_off ;;
resumed) resumed "$2" "$3" "$4" ;;
checklist) checklist "$2" ;;
idle-cpu) idle_cpu "${2:-30}" "${3:-2}" ;;
idle-threads) idle_threads "$2" "${3:-10}" ;;
check-m1) webos-enter env TIME_SCALE=$scale sh /mnt/host/guest/scripts/check-m1.sh "${3:-10}" "$2" </dev/null ;;
app-calls) webos-enter env TIME_SCALE=$scale sh /mnt/host/tests/regress/tvservice/app-calls.sh "$2" </dev/null ;;
luna-info) webos-enter env TIME_SCALE=$scale sh /mnt/host/tests/regress/tv.sh luna-info </dev/null ;;
network) webos-enter env TIME_SCALE=$scale sh /mnt/host/tests/regress/tv.sh network "$2" "$3" </dev/null ;;
app-*) webos-enter env TIME_SCALE=$scale sh /mnt/host/tests/regress/tv.sh "$@" </dev/null ;;
tee|audio|decode-log|smp) webos-enter env TIME_SCALE=$scale sh /mnt/host/tests/regress/tv.sh "$@" </dev/null ;;
*) echo "unknown step $step"; false ;;
esac > $out/$step.txt 2>&1
echo $? > $out/$step.rc.tmp && mv $out/$step.rc.tmp $out/$step.rc
