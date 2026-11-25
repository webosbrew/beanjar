#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Real decoding through LG's media pipeline (video milestone 2), on the QEMU
# host against a VM booted with LAYERS=normal,gl,devmode DISPLAY_BACKEND=dbus (no other viewer
# connected: the media, video and cursor sockets take one client each):
#   1. packs the windowed web app of tests/regress/video/app/ (ares-package) around CLIP (made at test time,
#      never committed; e.g. build/media-test/, see tests/regress/video/make-clips.sh), playing it once
#      with SCRIPT (pause and seek actions, default "pause@6:3,seek@12:4"; "none": no actions);
#   2. starts a headless viewer (--screenshot-every 1000, --media-wav), then installs and
#      launches the app the way ares does;
#   3. after SECONDS, checks the viewer's log and captures (tests/regress/video/check-media.py): decoded
#      pictures shown at the window 320,180 960x540 instead of the pattern, the media audio's
#      level and timing, the pause (a frozen picture) and the seek;
#   4. closes and removes the app.
#   tests/regress/video/check-media.sh CLIP [SCRIPT]
# Env: HOST (the development VM's address, `host/scripts/vm-launcher config ADDRESS`), SECONDS_RUN (40), OUT (build/media-check), HWDEC (auto), KEY as
# check-localstorage.sh, VIEWER (build/viewer/beanjar-viewer; "none": play with the viewer already
# running, e.g. a windowed one with sound, and check nothing)
set -u
TOP=$(cd "$(dirname "$0")/../../.." && pwd)
CLIP=${1:?usage: check-media.sh CLIP [SCRIPT]}
SCRIPT=${2-pause@6:3,seek@12:4}
if [ -z "${HOST:-}" ]; then
	HOST=$("$TOP/host/scripts/vm-launcher" config ADDRESS) || exit 1
fi
RUN=${SECONDS_RUN:-40}
OUT=${OUT:-$TOP/build/media-check}
VIEWER=${VIEWER:-$TOP/build/viewer/beanjar-viewer}
KEY=${KEY:-$TOP/local/devmode_rsa}
HELPER_LOG=${HELPER_LOG:-$TOP/build/media/media-helper.log}
APP_ID=com.example.mediatest
T=$(mktemp -d)
viewer=
trap 'rm -rf "$T"; [ -n "$viewer" ] && kill $viewer 2>/dev/null' EXIT
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
O="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10"
cp "$KEY" "$T/key"; chmod 600 "$T/key"
dm() { timeout 150 ssh $O -o BatchMode=yes -i "$T/key" -p 9922 prisoner@"$HOST" "$@"; }
luna() { dm "luna-send-pub -n 1 '$1' '$2' </dev/null"; }

A=$TOP/build/test-apps/$APP_ID
rm -rf "$A"; mkdir -p "$TOP/build/test-apps"; cp -r "$TOP/tests/regress/video/app" "$A"
jq --arg id "$APP_ID" '.id=$id' "$TOP/tests/regress/video/app/appinfo.json" > "$A/appinfo.json"
b=$(basename "$CLIP"); ext=${b##*.}; [ "$ext" = "$b" ] && ext=mp4   # the clip keeps its extension
cp "$CLIP" "$A/video.$ext" || exit 1
if [ -n "$SCRIPT" ]; then loop= script=$SCRIPT; else loop=' loop' script=; fi
sed -i -e "s|@SRC@|video.$ext|" -e "s|@LOOP@|$loop|" -e "s|@SCRIPT@|$script|" "$A/index.html"
mkdir -p "$T/pkg"; ares-package -o "$T/pkg" "$A" >/dev/null || { fail "ares-package $A"; exit 1; }
mv "$T"/pkg/*.ipk "$T/app.ipk"
ipk=/media/developer/temp/ares_install_media.ipk
printf 'put %s %s\n' "$T/app.ipk" "$ipk" | sftp $O -o BatchMode=yes -i "$T/key" -P 9922 -b - prisoner@"$HOST" >/dev/null 2>&1
dm "luna-send-pub -i luna://com.webos.appInstallService/dev/install '{\"id\":\"com.ares.defaultName\",\"ipkUrl\":\"$ipk\",\"subscribe\":true}' </dev/null >/tmp/inst.out 2>&1 & p=\$!
	n=0; until grep -q -E '\"state\":\"(installed|install failed)\"|\"returnValue\":false' /tmp/inst.out || [ \$n -ge 120 ]; do sleep 1; n=\$((n+1)); done
	kill \$p; cat /tmp/inst.out; rm -f /tmp/inst.out $ipk" > "$T/install.out"
grep -q '"state":"installed"' "$T/install.out" && pass "installed $APP_ID ($(basename "$CLIP"))" ||
	{ fail "install: $(tail -n 1 "$T/install.out")"; exit 1; }

rm -rf "$OUT"; mkdir -p "$OUT"
helper_start=$(stat -c %s "$HELPER_LOG" 2>/dev/null || echo 0)
[ "$VIEWER" = none ] || "$VIEWER" "unix:path=$TOP/build/dbus.sock" --headless --seconds "$RUN" --size 960x540 --no-audio \
	${HWDEC:+--hwdec $HWDEC} --media-wav "$OUT/media.wav" --screenshot "$OUT/shot.ppm" \
	--screenshot-every 1000 > "$OUT/viewer.log" 2>&1 &
[ "$VIEWER" = none ] || viewer=$!
sleep 2
start=$(date +%s)
r=$(luna luna://com.webos.applicationManager/launch "{\"id\":\"$APP_ID\"}")
echo "$r" | grep -q '"returnValue":true' && pass "launched" || fail "launch: $r"
if [ "$VIEWER" = none ]; then
	sleep "$RUN"
else
	wait $viewer; viewer=
	echo "viewer ran $(( $(date +%s) - start )) s after the launch"
	python3 "$TOP/tests/regress/lib/tvsuite.py" slice "$HELPER_LOG" "$helper_start" "$OUT/media-helper.log"
	case $SCRIPT in *pause*) checks= ;; *) checks=--no-pause ;; esac
	python3 "$TOP/tests/regress/video/check-media.py" $checks "$OUT" && pass "media checks" || fail "media checks"
fi

r=$(luna luna://com.webos.applicationManager/dev/closeByAppId "{\"id\":\"$APP_ID\"}")
echo "$r" | grep -q '"returnValue":true' && pass "closed" || fail "close: $r"
dm "luna-send-pub -i luna://com.webos.appInstallService/dev/remove '{\"id\":\"$APP_ID\",\"subscribe\":true}' </dev/null >/tmp/rm.out 2>&1 & p=\$!
	n=0; until grep -q -E '\"state\":\"(removed|remove failed)\"|\"returnValue\":false' /tmp/rm.out || [ \$n -ge 60 ]; do sleep 1; n=\$((n+1)); done
	kill \$p; cat /tmp/rm.out; rm -f /tmp/rm.out" | grep -q '"state":"removed"' && pass "removed" || fail "remove"
echo "captures and logs: $OUT"
[ "$fails" = 0 ]
