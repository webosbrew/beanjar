#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Windowed <video> check (video milestone 1), on the QEMU host against a VM booted with
# LAYERS=normal,gl,devmode DISPLAY_BACKEND=dbus (host/scripts/vm-launcher start):
#   1. packs a looping web app (tests/regress/video/app/, ares-package) around VIDEO, an H.264 MP4 made at test
#      time and never committed, e.g. on a machine with libx264:
#        ffmpeg -f lavfi -i testsrc2=size=640x360:rate=25 -f lavfi -i sine=frequency=440 \
#          -t 20 -c:v libx264 -profile:v main -pix_fmt yuv420p -c:a aac -shortest video.mp4
#   2. installs and launches it the way ares does (developer mode: sftp, dev/install, launch);
#   3. after WAIT seconds, captures with the viewer (--osd-alpha) and checks the hole at the
#      video element's rect, 320,180 960x540 (tests/regress/video/check-hole.py);
#   4. closes and removes the app.
#   tests/regress/video/check-video-app.sh VIDEO.mp4
# Env: HOST (the development VM's address, `host/scripts/vm-launcher config ADDRESS`), WAIT (8), OUT (build/video-app: captures), KEY as check-localstorage.sh
set -u
TOP=$(cd "$(dirname "$0")/../../.." && pwd)
VIDEO=${1:?usage: check-video-app.sh VIDEO.mp4}
if [ -z "${HOST:-}" ]; then
	HOST=$("$TOP/host/scripts/vm-launcher" config ADDRESS) || exit 1
fi
WAIT=${WAIT:-8}
OUT=${OUT:-$TOP/build/video-app}
KEY=${KEY:-$("$TOP/host/scripts/vm-launcher" config IDENTITY)/devmode_rsa}
APP_ID=com.example.videotest
RECT=320,180,960,540
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
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
b=$(basename "$VIDEO"); ext=${b##*.}; [ "$ext" = "$b" ] && ext=mp4   # the clip keeps its extension
cp "$VIDEO" "$A/video.$ext" || exit 1
sed -i -e "s|@SRC@|video.$ext|" -e "s|@LOOP@| loop|" -e "s|@SCRIPT@||" "$A/index.html"
mkdir -p "$T/pkg"; ares-package -o "$T/pkg" "$A" >/dev/null || { fail "ares-package $A"; exit 1; }
mv "$T"/pkg/*.ipk "$T/app.ipk"
ipk=/media/developer/temp/ares_install_video.ipk
printf 'put %s %s\n' "$T/app.ipk" "$ipk" | sftp $O -o BatchMode=yes -i "$T/key" -P 9922 -b - prisoner@"$HOST" >/dev/null 2>&1
dm "luna-send-pub -i luna://com.webos.appInstallService/dev/install '{\"id\":\"com.ares.defaultName\",\"ipkUrl\":\"$ipk\",\"subscribe\":true}' </dev/null >/tmp/inst.out 2>&1 & p=\$!
	n=0; until grep -q -E '\"state\":\"(installed|install failed)\"|\"returnValue\":false' /tmp/inst.out || [ \$n -ge 120 ]; do sleep 1; n=\$((n+1)); done
	kill \$p; cat /tmp/inst.out; rm -f /tmp/inst.out $ipk" > "$T/install.out"
grep -q '"state":"installed"' "$T/install.out" && pass "installed $APP_ID" || { fail "install: $(tail -n 1 "$T/install.out")"; exit 1; }
r=$(luna luna://com.webos.applicationManager/launch "{\"id\":\"$APP_ID\"}")
echo "$r" | grep -q '"returnValue":true' && pass "launched" || fail "launch: $r"

sleep "$WAIT"
mkdir -p "$OUT"
# --no-media: the window keeps the milestone 1 pattern, which check-hole.py looks for (decoded
# pictures there are tests/regress/video/check-media.sh's)
"$TOP/build/viewer/beanjar-viewer" "unix:path=$TOP/build/dbus.sock" --headless --seconds 4 --size 960x540 \
	--no-audio --no-media --screenshot "$OUT/shot.ppm" --osd-alpha > "$OUT/viewer.log" 2>&1
grep -E '^video:' "$OUT/viewer.log" | tail -n 3
grep -q "SHOWN.*window=320,180 960x540" "$OUT/viewer.log" && pass "viewer: context shown at 320,180 960x540" ||
	fail "viewer: no context shown at the video element's rect"
if python3 "$TOP/tests/regress/video/check-hole.py" "$OUT/shot.ppm" "$RECT" | tee "$OUT/hole.txt" | grep -q PASS; then
	pass "hole at $RECT: $(grep -E 'alpha 0 inside' "$OUT/hole.txt")"
else fail "hole at $RECT"; cat "$OUT/hole.txt"; fi
# The bar below the video is opaque page content: alpha 255 there, not the pattern.
python3 - "$OUT/shot.ppm.osd.pam" <<'PY' && pass "OSD opaque on the page's bar (outside the rect)" || fail "OSD not opaque on the bar"
import sys
d = open(sys.argv[1], "rb").read()
w = int(d.split(b"WIDTH ")[1].split(b"\n")[0]); body = d[d.index(b"ENDHDR\n") + 7:]
a = [body[((y * w) + x) * 4 + 3] for y in range(910, 1010, 10) for x in range(100, 1800, 100)]
sys.exit(0 if min(a) == 255 else 1)
PY

r=$(luna luna://com.webos.applicationManager/dev/closeByAppId "{\"id\":\"$APP_ID\"}")
echo "$r" | grep -q '"returnValue":true' && pass "closed" || fail "close: $r"
dm "luna-send-pub -i luna://com.webos.appInstallService/dev/remove '{\"id\":\"$APP_ID\",\"subscribe\":true}' </dev/null >/tmp/rm.out 2>&1 & p=\$!
	n=0; until grep -q -E '\"state\":\"(removed|remove failed)\"|\"returnValue\":false' /tmp/rm.out || [ \$n -ge 60 ]; do sleep 1; n=\$((n+1)); done
	kill \$p; cat /tmp/rm.out; rm -f /tmp/rm.out" | grep -q '"state":"removed"' && pass "removed" || fail "remove"
echo "captures: $OUT"
[ "$fails" = 0 ]
