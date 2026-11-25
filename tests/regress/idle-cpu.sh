# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Idle CPU: scripts/regress's idle-cpu and idle-threads steps (sourced by tests/regress/guest.sh,
# put with it; tests/regress/lib/idlecpu.py judges the output on the host, and
# tests/unit/regress/test_idlecpu.py runs these on a made-up /proc). They run in the initramfs,
# whose /proc sees every webOS process (with the initramfs's pids). PROC: the /proc to read.
#
# idle_cpu SECONDS WINDOWS: WINDOWS+1 snapshots SECONDS apart of each process's and each thread's
# utime and stime (stat fields 14 and 15) and each thread's voluntary and involuntary context
# switches, each snapshot headed by the guest's uptime. One line a task, trimmed here so that the
# host gets little:
#   P PID UTIME STIME STATE COMM        T PID TID UTIME STIME STATE COMM
#   V PID TID VOLUNTARY                 N PID TID NONVOLUNTARY
# The comm may hold spaces and parentheses: the greedy match takes the stat line's last ") ".
idle_proc() { echo "${PROC:-/proc}"; }
idle_stat() { # TAG GLOB (relative to PROC): the stat lines of the files GLOB matches, trimmed
	(cd "$(idle_proc)" && grep -s -H '' $2) |
		sed -n "s|^\([0-9]*\)/\(task/\([0-9]*\)/\)\{0,1\}stat:[0-9]* (\(.*\)) \(.\) \([^ ]* \)\{10\}\([0-9]*\) \([0-9]*\) .*|$1 \1 \3 \7 \8 \5 \4|p"
}
idle_snap() { # N
	echo "SNAP $1 $(cut -d' ' -f1 "$(idle_proc)/uptime")"
	idle_stat P '[0-9]*/stat' | sed 's/^P \([0-9]*\)  /P \1 /'
	idle_stat T '[0-9]*/task/[0-9]*/stat'
	(cd "$(idle_proc)" && grep -s -H 'ctxt_switches' [0-9]*/task/[0-9]*/status) |
		sed -n -e 's|^\([0-9]*\)/task/\([0-9]*\)/status:voluntary_ctxt_switches:[[:space:]]*\([0-9]*\)$|V \1 \2 \3|p' \
			-e 's|^\([0-9]*\)/task/\([0-9]*\)/status:nonvoluntary_ctxt_switches:[[:space:]]*\([0-9]*\)$|N \1 \2 \3|p'
}
idle_cpu() { # SECONDS WINDOWS
	echo "IDLECPU $(uname -m) $1 $2"
	i=0
	while :; do
		idle_snap $i
		[ $i -lt $2 ] || break
		i=$((i + 1))
		sleep $1
	done
	echo "IDLECPU END"
}
# idle_threads TIDS SAMPLES: threads TIDS (comma-joined: a flagged process's busiest, as idle-cpu
# numbered them), SAMPLES times 0.2 s apart: each thread's state, wchan (the kernel function it
# sleeps in, 0 when running) and the syscall it is in (the first field of /proc/TID/syscall: its
# number, "running", or -1 when in none). Shell builtins only, no process per read: a sample
# costs the guest next to nothing.
#   X SAMPLE TID STATE WCHAN SYSCALL
idle_threads() { # TIDS SAMPLES
	echo "IDLETHREADS $(uname -m) $2"
	tids=$(echo "$1" | tr ',' ' ')
	i=0
	while [ $i -lt $2 ]; do
		i=$((i + 1))
		for t in $tids; do
			for d in "$(idle_proc)"/[0-9]*/task/$t; do :; done  # its process's directory
			st= w= sc=
			read -r st 2>/dev/null < "$d/stat" || [ -n "$st" ] || continue
			st=${st##*) }; st=${st%% *}
			read -r w 2>/dev/null < "$d/wchan"
			read -r sc _ 2>/dev/null < "$d/syscall"
			echo "X $i $t ${st:-?} ${w:-?} ${sc:-?}"
		done
		[ $i -lt $2 ] && sleep 0.2
	done
	echo "IDLETHREADS END"
}
