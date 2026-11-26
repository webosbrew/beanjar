#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# An automatic channel scan by Luna on a set with our tuner, in the guest as root:
#   scan.sh [TIMEOUT_S [WATCH_IDS]]
# Keeps utp.broadcast/open (purpose SCAN) subscribed, starts utp.channel/scan/startAutoScan with
# its broadcastId and follows it until its status says autoScanning false or TIMEOUT_S (default
# 3600). Then asks iepg for the channel list and the current events in the forms utp and Live TV
# use. Prints KEY=VALUE lines (scan_s, channels, events, channel=<number> <name> <id>,
# event=<id> <title>) and the raw answers after "--- raw"; it judges nothing
# (tests/regress/tuner/run-scan.py does).
#
# The passes are fixed by the region (US: terrestrial digital, terrestrial analogue, cable digital,
# cable analogue; legacy-broadcast-atsc ignores setAutoScanChannelType; Japan: terrestrial digital
# alone; Germany: those of the US and satellite). Our lineups are digital, so each other pass is
# skipped as it starts, as the stock app's Skip button does (scan/skipAutoScan): about 40 s instead
# of 20 minutes under TCG. ALL=1 runs every pass; PASSES names the passes kept, the source types
# without INPUTSOURCE_ (default TERRESTRIAL_DIGITAL; run-scan.py adds CABLE_DIGITAL when a cable
# RF, C78, is mapped).
#
# WATCH_IDS: the channelIds watched after the scan, an extended regular expression for a whole
# "channelId":"..." pair; by default each US RF's first service (..._1_0_0_0). A Japanese set (run-scan.py: '"channelId":"1_[^"]*"', terrestrial digital)
# lists its satellite presets too, which are not watched; a DVB set's digital channels are watched
# each ('"channelId":"[13]_[^"]*"'). They are watched in channelId order (the source type first:
# 1 terrestrial, 3 cable), so Live TV is left on the last one.
#
# A service just started answers before it is ready: utp.broadcast/open "No resource" (-106) for
# about 10 s, startAutoScan nothing at all, or a scan that never starts a pass. All are retried
# until READY_WAIT (default 600) s after the start; ready_s is how long it took until a pass
# started, start_tries the startAutoScan calls.
TIMEOUT=${1:-3600}
WATCH_IDS=${2:-'"channelId":"[^"]*_1_0_0_0"'}
# seconds since boot: the set's clock jumps when it takes the broadcast's time (STT)
mono() { cut -d. -f1 /proc/uptime; }
READY_WAIT=${READY_WAIT:-600}
PASSES=${PASSES:-TERRESTRIAL_DIGITAL}
t0=$(mono)
L="luna-send -n 1 -w 30000"
U=luna://com.webos.service.utp.channel/scan
OUT=/tmp/scan-open.out
LOG=/tmp/scan-progress.log
rm -f "$OUT" "$LOG"
# a broadcastId from utp.broadcast/open (purpose SCAN), kept subscribed in the background
open_scan() {
	ID=
	while [ $(($(mono) - t0)) -lt "$READY_WAIT" ]; do
		luna-send -i luna://com.webos.service.utp.broadcast/open \
			'{"subscribe":true,"options":{"appId":"com.webos.app.channelsetting","windowId":"","purpose":"SCAN"}}' \
			</dev/null > "$OUT" 2>&1 &
		OPEN=$!
		sleep 3
		ID=$(sed -n 's/.*"broadcastId": *"\([^"]*\)".*/\1/p' "$OUT" | head -1)
		[ -n "$ID" ] && return 0
		kill $OPEN 2>/dev/null
		sleep 2
	done
	return 1
}
# a scan has started when its first pass is pushed (getAutoScanSourceOrderStatus) and the frontends
# tune (dtv_soc counts their tunes): one started while utp-atsc is still coming up (the open above
# launches it) answers startAutoScan and then stays at autoScanning true with no pass; on a new VM's
# first boot legacy-broadcast-arib, still setting up its database, fails the start ("interface call
# error") after utp has answered it and pushed the pass, which then ends with no tune and no channel.
# Such a start is stopped and made again.
tunes() { sed -n 's/^tunes \([0-9]*\).*/\1/p' /proc/dtv_soc; }
source_type() {
	tr -d '\n ' < /tmp/scan-source.out | grep -o '"currentScanSourceType":"[A-Z_]*"' | tail -1 |
		sed 's/.*:"\(.*\)"/\1/'
}
# Live TV in front keeps the tuner: a Japanese set's scan then runs for minutes and tunes nothing
# (W22O queue's ARIB board, a set that already had a channel), as if the stock channel settings app,
# which takes the front, had not. Closed before each start: the boot launches it while the service
# is still coming up, after a close made at the beginning. It is launched again for the watch below.
close_livetv() {
	$L luna://com.webos.service.applicationmanager/closeByAppId '{"id":"com.webos.app.livetv"}' </dev/null >/dev/null 2>&1
}
tries=0
START=
while :; do
	open_scan || { echo "ready_s=$(($(mono) - t0))"; echo "error=no broadcastId"; cat "$OUT"; exit 1; }
	B="{\"broadcastId\":\"$ID\"}"
	# the pass in progress: pushed by a subscription when it changes (nowNextSourceInfo)
	luna-send -i $U/getAutoScanSourceOrderStatus "{\"broadcastId\":\"$ID\",\"subscribe\":true}" \
		</dev/null > /tmp/scan-source.out 2>&1 &
	SOURCE=$!
	tries=$((tries + 1))
	close_livetv
	tunes0=$(tunes)
	START=$($L $U/startAutoScan "$B" </dev/null 2>&1 | tr -d '\n ')
	scan_start=$(mono)
	if echo "$START" | grep -q '"returnValue":true'; then
		while { [ -z "$(source_type)" ] || [ "$(tunes)" = "$tunes0" ]; } &&
			[ $(($(mono) - scan_start)) -lt "${PASS_WAIT:-90}" ]; do
			sleep 2
		done
		[ -n "$(source_type)" ] && [ "$(tunes)" != "$tunes0" ] && break
		$L $U/stopAutoScan "$B" </dev/null >/dev/null 2>&1
	fi
	kill $SOURCE $OPEN 2>/dev/null
	if [ $(($(mono) - t0)) -ge "$READY_WAIT" ]; then
		echo "ready_s=$(($(mono) - t0))"
		echo "start_tries=$tries"
		echo "error=no scan pass started, or no tune, in $READY_WAIT s: $START"
		exit 1
	fi
	sleep 10
done
echo "broadcastId=$ID"
echo "ready_s=$(($(mono) - t0))"
echo "start_tries=$tries"
echo "start=$START"
skipped=
while :; do
	sleep 5
	now=$(mono)
	A=$($L $U/getAutoScanStatus "$B" </dev/null | tr -d '\n ')
	src=$(source_type)
	echo "t=$((now - scan_start)) $A $src" >> "$LOG"
	echo "$A" | grep -q '"autoScanning":false' && [ $((now - scan_start)) -gt 10 ] && break
	[ $((now - scan_start)) -ge "$TIMEOUT" ] && { echo "timeout=1"; break; }
	if [ -z "$ALL" ] && [ -n "$src" ] && ! echo " $PASSES " | grep -q " ${src#INPUTSOURCE_} " &&
		[ "$src" != "$skipped" ]; then
		echo "skip $src at t=$((now - scan_start)): $($L $U/skipAutoScan "$B" </dev/null | tr -d '\n ')" >> "$LOG"
		echo "skipped=$src"
		skipped=$src
	fi
done
echo "scan_s=$(( $(mono) - scan_start ))"
echo "status=$A"
kill $SOURCE $OPEN 2>/dev/null
sleep 5
I=luna://com.webos.service.iepg
CH=$($L $I/getChannelList '{"channelGroup":"All","channelMode":"Tuner","dataType":0,"sort":0}' </dev/null | tr -d '\n')
# channelNumber, channelName and channelId of each entry, in the order iepg gives them
echo "$CH" | sed 's/},{"channelNumber"/}\n{"channelNumber"/g' | sed -n \
	's/.*"channelNumber":"\([^"]*\)".*"channelName":"\([^"]*\)".*"channelId":"\([^"]*\)".*/channel=\1 \2 \3/p'
IDS=$(echo "$CH" | grep -o '"channelId":"[^"]*"' | sed 's/"channelId"://' | sort -u | tr '\n' ',' | sed 's/,$//')
echo "channels=$(echo "$CH" | grep -o '"channelId":"[^"]*"' | sort -u | wc -l)"
# A set reads the guide (EIT/ETT) only of the RF channel it is tuned to, and a scan reads no
# guide: watch the first channel of each RF in Live TV, as someone flicking through would, until
# iepg has its current event (legacy-broadcast puts them into com.webos.epgdb). A watch needs
# Live TV in the foreground (tvrm grants the resources to the foreground app only).
$L luna://com.webos.service.applicationmanager/launch '{"id":"com.webos.app.livetv"}' </dev/null >/dev/null
sleep 10
luna-send -i luna://com.webos.service.utp.broadcast/open \
	'{"subscribe":true,"options":{"appId":"com.webos.app.livetv","windowId":"","purpose":"WATCH"}}' \
	</dev/null > /tmp/scan-watch.out 2>&1 &
WATCH=$!
sleep 5
WID=$(sed -n 's/.*"broadcastId": *"\([^"]*\)".*/\1/p' /tmp/scan-watch.out | head -1)
for cid in $(echo "$CH" | grep -oE "$WATCH_IDS" | sed 's/"channelId":"\(.*\)"/\1/' | sort -u); do
	w0=$(mono)
	r=$($L luna://com.webos.service.utp.broadcast/changeChannel "{\"broadcastId\":\"$WID\",\"channelId\":\"$cid\"}" </dev/null | tr -d '\n ')
	while [ $(($(mono) - w0)) -lt "${EPG_WAIT:-120}" ]; do
		sleep 5
		$L luna://com.webos.service.iepg/getCurrentEventList "{\"channelId\":[\"$cid\"],\"dataType\":0,\"sort\":0}" </dev/null |
			grep -q '"programName"' && break
	done
	echo "watched=$cid $(($(mono) - w0))s $(echo "$r" | sed -n 's/.*"returnValue":\([a-z]*\).*/\1/p')"
done
kill $WATCH 2>/dev/null
EV=$($L $I/getCurrentEventList "{\"channelId\":[$IDS],\"dataType\":0,\"sort\":0}" </dev/null | tr -d '\n')
# each entry's programName follows its channelId (a Japanese set's is empty: its signalChannelId,
# ONID_SVCID_TSID, the tail of the channel's channelId)
echo "$EV" | grep -oE '"(channelId|signalChannelId|programName)":"[^"]*"' |
	awk -F'"' '$2 == "channelId" { c = $4 } $2 == "signalChannelId" { s = $4 }
		$2 == "programName" { print "event=" (c != "" ? c : s) " " $4 }'
echo "events=$(echo "$EV" | sed -n 's/.*"programListCount":\([0-9]*\).*/\1/p')"
echo "--- raw"
echo "channel_list=$(echo "$CH" | cut -c1-6000)"
echo "event_list=$(echo "$EV" | cut -c1-6000)"
