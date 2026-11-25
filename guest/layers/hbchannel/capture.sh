#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Capture what a real install of a Homebrew Channel release leaves on a webOS release, for the
# hbchannel layer to ship (versions/<HBC>/captured/<release>/, build-layer.sh). On a fresh scratch
# VM of FIRMWARE (layers normal,headless,devmode,rootssh) it does what a user of a rooted TV does:
#   1. ares-install of the IPK that versions/HBC/release.env pins, as the developer-mode user:
#      appinstalld's dev/install (com.webos.appInstallService), which unpacks it with opkg under
#      /media/developer/apps and writes the app's and its service's LS2 files under /var
#   2. HBC's own elevate-service as root, as its startup.sh runs it at boot on a rooted TV
# then copies out, with ares-pull, the files under /media/developer/apps and the dev LS2 directories
# (/var/luna-service2-dev, /var/palm/ls2-dev), and lists the other files that changed meanwhile
# (stdout, for review; the developer-mode jail that ares' SSH sets up is left out). It writes OUT
# (default guest/layers/hbchannel/versions/HBC/captured/RELEASE):
#   files/       the LS2 files and opkg's records, at their guest paths, as written
#   payload.txt  the unpacked IPK under /media/developer/apps/usr/palm, one line per entry:
#                "TYPE MODE UID:GID SHA256|- PATH" (the layer unpacks the IPK itself)
#   source.txt   the HBC release, the firmware and the IPK's sha256
# Then the VM is stopped and deleted (KEEP=1 keeps it).
#
#   guest/layers/hbchannel/capture.sh HBC FIRMWARE RELEASE
#
# HBC: a directory of versions/ (0.8.0, 0.7.3, ...); its IPK is downloaded into Meson's package
# cache if it is not there yet. FIRMWARE: a name in the host's firmware collections (beanjar create
# --firmware); it is imported (hard links) into the VM. Env: VM (default hbcap-HBC-RELEASE), OUT, REASON (beanjar start --reason),
# SETTLE, KEEP. Runs on the x86 PC or any host with beanjar and ares-cli.
set -eu
HERE=$(dirname "$(readlink -f "$0")")
TOP=$(readlink -f "$HERE/../../..")
[ $# = 3 ] || { echo "usage: $0 HBC FIRMWARE RELEASE" >&2; exit 2; }
HBC=$1 FW=$2 RELEASE=$3
VM=${VM:-hbcap-$(echo "$HBC" | tr -c 'a-z0-9\n' -)-$RELEASE}
OUT=${OUT:-$HERE/versions/$HBC/captured/$RELEASE}
APP=org.webosbrew.hbchannel
SVC_DIR=/media/developer/apps/usr/palm/services/$APP.service
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"

[ -f "$HERE/versions/$HBC/release.env" ] || { echo "hbcap: no versions/$HBC" >&2; exit 1; }
. "$HERE/versions/$HBC/release.env"
CACHE=${MESON_PACKAGE_CACHE_DIR:-$TOP/subprojects/packagecache}
IPK=$CACHE/$SHA256-$(basename "$URL")	# by hash: releases may share a file name
if [ "$(sha256sum "$IPK" 2>/dev/null | cut -d' ' -f1)" != "$SHA256" ]; then
	mkdir -p "$CACHE"
	curl -fsSL --retry 3 -o "$IPK.part" "$URL"
	mv "$IPK.part" "$IPK"
	[ "$(sha256sum "$IPK" | cut -d' ' -f1)" = "$SHA256" ] || { echo "hbcap: $IPK: not $SHA256" >&2; exit 1; }
fi

T=$(mktemp -d)
made=					# set once this run has made the VM: cleanup touches no other
cleanup() {
	rm -rf "$T"
	[ -n "$made" ] && [ -z "${KEEP:-}" ] || return 0
	beanjar stop --force "$VM" >/dev/null 2>&1 || true
	beanjar delete "$VM" >/dev/null 2>&1 || true
}
trap cleanup EXIT
root() { beanjar ares-shell --root -d "$VM" -r "$1"; }

beanjar create "$VM" --import --firmware "$FW" --layers normal,headless,devmode,rootssh \
	--display none --mode direct --tag scratch >/dev/null
made=1
beanjar start "$VM" --reason "${REASON:-hbchannel capture}" --by hbcap >/dev/null

# Luna up (as root), then the developer-mode SSH that ares-install uses.
n=0
until root 'luna-send -n 1 luna://com.webos.applicationManager/listApps "{}"' 2>/dev/null |
	grep -q '"returnValue": *true'; do
	n=$((n + 1)); [ $n -lt 90 ] || { echo "hbcap: $VM: no Luna" >&2; exit 1; }
	sleep 10
done
LS2='/var/luna-service2-dev /var/palm/ls2-dev'
left=$(root "find /media/developer/apps $LS2 -type f 2>/dev/null; true")
[ -z "$left" ] || { echo "hbcap: $VM is not fresh:" >&2; echo "$left" >&2; exit 1; }
# The rest of the boot settles, so the list of other changes below is the install's. SETTLE (seconds,
# default 60) longer where a busy first boot reacts to the install: spacebuns' eula-service reboots
# the set (tvpower power2/reboot, micomReset) when settingsservice does not answer within 10 s.
sleep "${SETTLE:-60}"
root 'rm -f /tmp/hbcap.*; touch /tmp/hbcap.mark' >/dev/null
sleep 1

n=0
until beanjar ares-install -d "$VM" "$IPK" >"$T/install.log" 2>&1; do
	n=$((n + 1)); [ $n -lt 30 ] || { cat "$T/install.log" >&2; exit 1; }
	sleep 10
done
cat "$T/install.log"
# Its last step is a hub rescan (ls-control scan-services), which can time out under TCG while the
# set is busy after the install; the files are written by then, so only the rescan is repeated.
sleep 20
if ! root "$SVC_DIR/elevate-service" >"$T/elevate.log" 2>&1; then
	cat "$T/elevate.log" >&2
	grep -q 'Refreshing services' "$T/elevate.log" || exit 1
	n=0
	until root 'ls-control scan-services'; do
		n=$((n + 1)); [ $n -lt 5 ] || exit 1
		sleep 10
	done
fi

echo "== other files written since the install (not captured)"
root 'find /var /home /media -newer /tmp/hbcap.mark \( -type f -o -type l \) 2>/dev/null |
	grep -v -E "^/media/developer/apps/|^/var/luna-service2-dev/|^/var/palm/ls2-dev/|^/var/palm/jail/" | sort'
root "cd / && tar cf /tmp/hbcap.tar media/developer/apps \$(for d in $LS2; do [ -d \$d ] && echo \${d#/}; done)" >/dev/null
beanjar ares-pull --root -d "$VM" /tmp/hbcap.tar "$T/" >/dev/null

rm -rf "$OUT"
mkdir -p "$OUT/files"
python3 -I - "$T/hbcap.tar" "$OUT" <<'EOF'
import hashlib, sys, tarfile
from pathlib import Path
tar, out = tarfile.open(sys.argv[1]), Path(sys.argv[2])
PAYLOAD = "media/developer/apps/usr/palm/"
lines = []
for m in tar.getmembers():
    name = m.name.lstrip("./")
    if name.startswith(PAYLOAD) or name + "/" == PAYLOAD:
        kind = "d" if m.isdir() else "f" if m.isfile() else "l" if m.issym() else "?"
        digest = hashlib.sha256(tar.extractfile(m).read()).hexdigest() if m.isfile() else (m.linkname if m.issym() else "-")
        lines.append(f"{kind} {m.mode:04o} {m.uid}:{m.gid} {digest} /{name}")
    elif m.isfile():
        dest = out / "files" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(tar.extractfile(m).read())
(out / "payload.txt").write_text("".join(l + "\n" for l in sorted(lines, key=lambda l: l.split(" ", 4)[4])))
EOF
printf 'hbchannel %s\nfirmware %s\nipk %s\n' "$TAG" "$FW" "$SHA256" > "$OUT/source.txt"
echo "hbcap: $TAG on $RELEASE ($FW) -> $OUT"
find "$OUT/files" -type f | sed "s|^$OUT/files||" | sort
