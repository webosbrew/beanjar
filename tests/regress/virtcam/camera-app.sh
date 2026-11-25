#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# In the guest, as root (tests/regress/virtcam/run-camera.py --app runs it over beanjar ares-shell --root):
# LG's camera app on the camera's default path, without LG's raw-format override, for SECS
# seconds.
#
#   camera-app.sh APPID SECS KEEP
#
# Removes /var/luna/preferences/webrtc_video_capture_force_raw_format if it is there (the app then
# asks for JPEG, which LG's camera player decodes with appsrc ! omxmjpegdec ! appsink), launches
# the app and prints KEY=VALUE lines: WebAppMgr's pid before and after, and the media elements'
# syslog lines since the launch (the stand-in decoder's choice of consumer and format, and its
# "host decoding" rate lines, guest/layers/soc/src/video/src/esdec.c), and the camera player's and
# camera2's lines as cam= (tests/regress/lib/camclass.py names a failure from them). Camera access is granted to
# the app first, as its prompt's Allow does. KEEP=0 closes the app.
APP=$1 SECS=$2 KEEP=$3
# one run at a time: a run left behind by a timed-out shell would close this run's app at its end
PIDF=/tmp/beanjar-camera-app.pid
old=$(cat "$PIDF" 2>/dev/null)
if [ -n "$old" ] && [ "$old" != $$ ] && kill -0 "$old" 2>/dev/null; then
	pkill -P "$old" 2>/dev/null
	kill "$old" 2>/dev/null
	echo "stale_run_stopped=$old"
fi
echo $$ >"$PIDF"
OVR=/var/luna/preferences/webrtc_video_capture_force_raw_format
# syslog(3) lands in legacy-log where PmLog keeps one (webOS 6), else in messages
LOGS=$(ls /var/log/legacy-log /var/log/messages 2>/dev/null)
if [ -e "$OVR" ]; then
	rm -f "$OVR"
	echo "override_removed=1"
else
	echo "override_removed=0"
fi
# the camera as LG's camera service lists it first: the app picks a camera at its start
n=0
id=""
while [ -z "$id" ] && [ $n -lt 90 ]; do
	id=$(luna-send -n 1 -a com.webos.app.browser luna://com.webos.service.camera2/getCameraList '{}' \
		</dev/null 2>&1 | sed -n 's/.*"id": *"\([^"]*\)".*/\1/p')
	[ -n "$id" ] || { sleep 1; n=$((n + 1)); }
done
echo "camera_id=$id"
# camera access for the app, as its own prompt's Allow sets it (a user setting: at run time)
p='[{"appId":"'$APP'","appTitle":"Camera","permissions":[{"resource":"camera","permission":true},{"resource":"mic","permission":true}]}]'
r=$(luna-send -n 1 luna://com.webos.settingsservice/setSystemSettings \
	'{"category":"option","settings":{"cameraResourcePermission":'"$p"'}}' </dev/null 2>&1 | tr -d '\n')
echo "permission=$r"
# WebAppMgr's main process (its parent is init) and LG's crash reports, before and after
wam() { for p in $(pidof WebAppMgr); do [ "$(awk '{print $4}' /proc/$p/stat)" = 1 ] && echo "$p"; done; }
crashes() { ls /var/log/crashd /var/spool/faultmanager /var/log/reports/librdx 2>/dev/null | grep -c -i -E 'webappmgr|wam'; }
# a fresh start of the app (one left running from before keeps its old camera state)
luna-send -n 1 luna://com.webos.applicationManager/closeByAppId "{\"id\":\"$APP\"}" \
	</dev/null >/dev/null 2>&1 && sleep 5
echo "wam_before=$(wam)"
echo "crashes_before=$(crashes)"
# the log while the app runs (it rotates within minutes)
tail -n 0 -F $LOGS >/tmp/camera-app.log 2>/dev/null &
follow=$!
r=$(luna-send -n 1 -a com.webos.surfacemanager luna://com.webos.applicationManager/launch \
	"{\"id\":\"$APP\"}" </dev/null 2>&1 | tr -d '\n')
echo "launch=$r"
sleep "$SECS"
echo "wam_after=$(wam)"
echo "crashes_after=$(crashes)"
kill $follow
grep -a -E "host decoding|pictures as|pictures from the host|reaches|no answer from the host|the host decodes" \
	/tmp/camera-app.log | sed 's/^/log=/'
# the camera player's and camera2's lines, for run-camera.py to name a failure by
# (tests/regress/lib/camclass.py: its LOG_PATTERN)
grep -a -E "Load, ret|need loaded state_|onLoadComplete|PrintDecodedBuffer|getUserMedia|getProperties" \
	/tmp/camera-app.log | head -n 300 | sed 's/^/cam=/'
rm -f /tmp/camera-app.log
[ "$KEEP" = 1 ] || luna-send -n 1 -a com.webos.surfacemanager luna://com.webos.applicationManager/closeByAppId \
	"{\"id\":\"$APP\"}" </dev/null >/dev/null 2>&1
rm -f "$PIDF"
exit 0
