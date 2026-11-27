#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Web storage check: a developer-mode web app (tests/regress/devmode/app/ with tests/regress/devmode/localstorage.html
# as its page, packed by ares-package) adds one to
# localStorage "count" on every launch; this reads the value back from WebAppMgr's LocalStorage
# database on disk (SQLite, values UTF-16LE). WAM keeps each app's origin apart by its path:
# /var/lib/webappmanager2/localstorage/file_%2Fmedia%2Fdeveloper%2Fapps%2F...%2F<app id>%2F_0.localstorage. Run on the QEMU host with a VM booted with LAYERS=...,devmode, like check-devmode.sh.
#
#   tests/regress/devmode/check-localstorage.sh install        install the app (dev/install over ssh)
#   tests/regress/devmode/check-localstorage.sh launch N       launch, close, expect count N on disk
#   tests/regress/devmode/check-localstorage.sh remove         remove the app
#
# Across an app restart: install, launch 1, launch 2. Across a reboot: boot again with the same
# persistent STATE, then launch 3. Reading the database uses the serial console as root
# (host/scripts/vm-launcher console); SSH and Luna calls go through the jail as ares does.
# Env: KEY (the VM's unencrypted developer key, default the development VM's, `host/scripts/vm-launcher config IDENTITY`/devmode_rsa;
#      a library VM's is <vm>/identity/devmode_rsa), HOST (default the development VM's address,
#      `host/scripts/vm-launcher config ADDRESS`; a library VM's: config ADDRESS --dir <vm>),
#      GRAB (screenshot file, taken while the app shows its count)
set -u
TOP=$(cd "$(dirname "$0")/../../.." && pwd)
KEY=${KEY:-$("$TOP/host/scripts/vm-launcher" config IDENTITY)/devmode_rsa}
if [ -z "${HOST:-}" ]; then
	HOST=$("$TOP/host/scripts/vm-launcher" config ADDRESS) || exit 1
fi
APP_ID=com.example.localstoragetest
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
O="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10"
cp "$KEY" "$T/key"; chmod 600 "$T/key"
dm() { timeout 150 ssh $O -o BatchMode=yes -i "$T/key" -p 9922 prisoner@"$HOST" "$@"; }
subscribe() {
	dm "luna-send-pub -i '$1' '$2' </dev/null >/tmp/sub.out 2>&1 & p=\$!
		n=0; until grep -q -E '$3' /tmp/sub.out || [ \$n -ge 120 ]; do sleep 1; n=\$((n+1)); done
		kill \$p; cat /tmp/sub.out; rm -f /tmp/sub.out"
}
# The count in the on-disk database, via the root console; empty if there is none.
disk_count() {
	# through the VM's output share (/mnt/host/out, build/share/out here)
	rm -f "$TOP/build/share/out/localstorage.db"
	"$TOP/host/scripts/vm-launcher" console 'webos-enter sh -c "sync; cp /var/lib/webappmanager2/localstorage/*localstoragetest*.localstorage /mnt/host/out/localstorage.db"' 4 >/dev/null
	python3 - "$TOP/build/share/out/localstorage.db" <<'PY'
import os, sqlite3, stat, sys, tempfile
# The share is the guest's: its file is read without following a link (O_NOFOLLOW, a regular file
# only) into a copy of the host's, which SQLite opens; never the share's file, whose journal
# beside it (the guest's too) SQLite would roll back into it.
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            sys.exit("no database: not a regular file")
        data = f.read()
    with tempfile.NamedTemporaryFile(suffix=".db") as t:
        t.write(data)
        t.flush()
        rows = dict(sqlite3.connect(t.name).execute("SELECT key, value FROM ItemTable"))
except Exception as e:
    sys.exit(f"no database: {e}")
v = rows.get("count")
print(v.decode("utf-16-le") if isinstance(v, bytes) else v, rows.get("last", b"").decode("utf-16-le"))
PY
}

case "${1:-}" in
install)
	A=$TOP/build/test-apps/$APP_ID
	rm -rf "$A"; mkdir -p "$TOP/build/test-apps"; cp -r "$TOP/tests/regress/devmode/app" "$A"
	cp "$TOP/tests/regress/devmode/localstorage.html" "$A/index.html"
	jq --arg id "$APP_ID" '.id=$id' "$TOP/tests/regress/devmode/app/appinfo.json" > "$A/appinfo.json"
	mkdir -p "$T/pkg"; ares-package -o "$T/pkg" "$A" >/dev/null || { echo "FAIL ares-package $A"; exit 1; }
	mv "$T"/pkg/*.ipk "$T/app.ipk"
	ipk=/media/developer/temp/ares_install_localstorage.ipk
	printf 'put %s %s\n' "$T/app.ipk" "$ipk" | sftp $O -o BatchMode=yes -i "$T/key" -P 9922 -b - prisoner@"$HOST" >/dev/null 2>&1
	r=$(subscribe luna://com.webos.appInstallService/dev/install \
		"{\"id\":\"com.ares.defaultName\",\"ipkUrl\":\"$ipk\",\"subscribe\":true}" '"state":"(installed|install failed)"|"returnValue":false')
	dm "rm -f $ipk" </dev/null
	echo "$r" | grep -q '"state":"installed"' && echo "PASS installed $APP_ID" || { echo "FAIL install: $(echo "$r" | tail -n 1)"; exit 1; }
	;;
launch)
	want=${2:?expected count}
	r=$(dm "luna-send-pub -n 1 luna://com.webos.applicationManager/launch '{\"id\":\"$APP_ID\"}' </dev/null")
	echo "$r" | grep -q '"returnValue":true' || { echo "FAIL launch: $r"; exit 1; }
	sleep 6
	if [ -n "${GRAB:-}" ]; then
		python3 "$TOP/tests/regress/virgl/dbus-grab.py" --bus "unix:path=$TOP/build/dbus.sock" "$GRAB" 3 >/dev/null 2>&1 && echo "screenshot: $GRAB"
	fi
	dm "luna-send-pub -n 1 luna://com.webos.applicationManager/dev/closeByAppId '{\"id\":\"$APP_ID\"}' </dev/null" >/dev/null
	sleep 3
	got=$(disk_count)
	echo "on disk: count $got"
	[ "${got%% *}" = "$want" ] && echo "PASS localStorage count $want" || { echo "FAIL localStorage count: want $want, got '$got'"; exit 1; }
	;;
remove)
	r=$(subscribe luna://com.webos.appInstallService/dev/remove "{\"id\":\"$APP_ID\",\"subscribe\":true}" \
		'"state":"(removed|remove failed)"|"returnValue":false')
	echo "$r" | grep -q '"state":"removed"' && echo "PASS removed" || { echo "FAIL remove: $(echo "$r" | tail -n 1)"; exit 1; }
	;;
*) sed -n '2,20p' "$0"; exit 2 ;;
esac
