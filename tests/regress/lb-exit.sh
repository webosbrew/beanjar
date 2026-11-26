# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# legacy-broadcast's exits, judged from what systemd and the Luna hub recorded (sourced by
# tests/regress/guest.sh, put with it; tests/unit/regress/test_lb_exit.py runs them on recorded
# text). legacy-broadcast stops itself when idle (exit status 0), and the next Luna call starts it
# again, so a new process is normal; only a crash or another abnormal exit fails.
#
# It starts twice over: at boot as legacy-broadcast.service (systemd: Type=simple,
# Restart=on-failure), and afterwards on demand by the hub as a dynamic service (EXE
# "setcpushares-ls2 .../legacy-broadcast.sh"), outside the unit, whose every exit the hub logs as
# LSHUB_SRV_EXIT with its EXIT_CODE.

LB_UNIT=legacy-broadcast.service

# lb_unit_clean SHOW: SHOW is `systemctl show $LB_UNIT -p Result -p ExecMainStatus -p NRestarts -p
# ActiveState` on one line. The unit's process ended by itself: stopped, result success, exit status
# 0 (a signal's number otherwise), and systemd never restarted it (Restart=on-failure would have).
lb_unit_clean() {
	case " $1 " in
	*" ActiveState=inactive "*|*" ActiveState=failed "*) ;;
	*) echo "$LB_UNIT is not stopped"; return 1 ;;
	esac
	case " $1 " in
	*" Result=success "*) ;;
	*) echo "$LB_UNIT did not finish with result success"; return 1 ;;
	esac
	case " $1 " in
	*" ExecMainStatus=0 "*) ;;
	*) echo "$LB_UNIT's main process did not exit with status 0"; return 1 ;;
	esac
	case " $1 " in
	*" NRestarts=0 "*) ;;
	*) echo "$LB_UNIT was restarted"; return 1 ;;
	esac
}

# lb_hub_exits_clean: on stdin, the hub's log lines (/var/log/messages). Every exit the hub logged of
# a legacy-broadcast it started has EXIT_CODE 0.
lb_hub_exits_clean() {
	bad=$(grep 'LSHUB_SRV_EXIT' | grep -E 'legacybroadcast|legacy-broadcast' | grep -v -E '"EXIT_CODE": *0[,}]')
	[ -z "$bad" ] && return 0
	echo "the hub logged an abnormal exit: $bad"
	return 1
}

# lb_crash_free: on stdin, the names in the crash report directories; none names legacy-broadcast.
lb_crash_free() {
	if grep -q legacy-broadcast; then
		echo "a crash report names legacy-broadcast"; return 1
	fi
}

# lb_policy_closed: on stdin, /var/log/messages (its rotated files first). servicemanager's
# lifecycle policy closed legacy-broadcast: a SERVICE_MANAGER_CLOSE decision with "reason":
# "priority_lowest", then that close's "set_status":"stopping" line for the legacy-broadcast service
# (one decision line per policy that matched, then the service it stops). The stock policy
# (/etc/palm/servicemanager/policy/lifecycle/com.webos.service.legacybroadcast.broadcast.json) gives
# the service priority lowest while iepg's channel list is empty and none of its tuner apps runs, from
# rest-boot-done on; the service then exits before the end of its init, where it sends tv-ready.
# Prints the first such close's log time (the line's [uptime]); false without one.
lb_policy_closed() {
	awk '
	/SERVICE_MANAGER_CLOSE/ && /"isServiceCloseRequired"/ { lowest = /"reason":"priority_lowest"/; next }
	/SERVICE_MANAGER_CLOSE/ && /"set_status":"stopping"/ {
		if (lowest && /"service":"com\.webos\.service\.legacybroadcast\.broadcast"/) {
			t = $0; sub(/^[^[]*\[/, "", t); sub(/\].*/, "", t); print t; found = 1; exit
		}
		lowest = 0
	}
	END { exit !found }'
}

# late_tv_ready END GRACE: bootd reached core-boot-done by its own tv-ready timeout, which stock
# bootd does in FirstUse (goldilocks: "EventCore waits event(tv-ready)/timeout(15)", a normal boot
# waits 60 s), so tvservice's tv-ready may still come after it. Wait for the tv-ready line in
# bootd's log until GRACE s (times the time scale) from now or the uptime END, whichever is first:
# prints bootd's time of the line and returns 0, or returns 1 if it never came. Needs up, scale
# and in_webos from guest.sh.
late_tv_ready() {
	stop=$(($(up | cut -d. -f1) + $2 * scale))
	[ "$stop" -gt "$1" ] && stop=$1
	while :; do
		log=$(in_webos "grep -E 'generateSignal.*\"name\":\"tv-ready\"' /var/log/bootd.log")
		case $log in *tv-ready*)
			printf '%s\n' "$log" | sed -n 's/^[A-Z]*( *\([0-9.]*\)).*/\1/p' | head -n 1
			return 0 ;;
		esac
		[ "$(up | cut -d. -f1)" -ge "$stop" ] && return 1
		sleep 1
	done
}
