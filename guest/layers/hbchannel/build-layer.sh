#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Assemble the generated "hbchannel" layer in build/layers/<chip>-<release>/hbchannel: Homebrew Channel
# (org.webosbrew.hbchannel) as a developer app, elevated, where a rooted TV has it. Select it with
# LAYERS=...,hbchannel,devmode (not with rootssh: both own the guest's port 22); the launcher
# (beanjar/runtime/qemu.py) then forwards host VM_ADDRESS:${SSH_PORT:-6622} to its Dropbear on the guest's 22.
#
#   guest/layers/hbchannel/build-layer.sh
#
# Env: DEST         output directory (default build/layers/<chip>-<release>/hbchannel)
#      HBC          the Homebrew Channel release, a directory of versions/ (default: versions/default)
#      CAPTURE      the webOS release's capture of a real install of it, versions/<HBC>/captured/<name>
#                   (versions/<HBC>/releases names it per release; meson.build), which capture.sh makes
#      IPK          its IPK (default: the one versions/<HBC>/release.env pins, downloaded once into
#                   Meson's package cache, subprojects/packagecache or $MESON_PACKAGE_CACHE_DIR, as
#                   <sha256>-<file name>: release candidates and releases may share a file name)
#      IPK_SHA256   its sha256 (default the pinned one; tests give their own IPK and its hash)
#
# /media and /var are bound from the state disks at mount-readwrite, which hides anything the
# overlay has there. So the layer stages what a real install leaves, at its guest paths, under
# STAGE = usr/share/beanjar/hbchannel, and usr/libexec/beanjar/hbchannel-install copies it into
# place at each boot before sam scans the developer apps:
#   STAGE/media/developer/apps/usr/palm/{applications,services,packages}/...   the IPK's data, as
#                    appinstalld's dev/install unpacks it (the capture's payload.txt lists what it
#                    unpacked; tests/unit/layers/test_hbchannel.py compares)
#   STAGE/media/developer/apps/{var,usr}/lib/opkg/...   opkg's records of the package (info/,
#                    status; usr/lib/opkg from afro to dreadlocks2)
#   STAGE/var/...    the app's and its service's LS2 files, /var/palm/ls2-dev on legacy releases
#                    and /var/luna-service2-dev from dreadlocks on, as appinstalld writes them and
#                    HBC's elevate-service then leaves them: Exec is the app's own run-js-service
#                    wrapper (thirdparty_jail=off: no jailer, the service runs as root,
#                    getuid() == 0), the role allows every name in and out, and the service has
#                    the "all" client group
#   STAGE/ipk.sha256 the IPK's hash: the boot copy is skipped while the installed one matches
# Every file of the last two is the capture's (CAPTURE/files), taken from a scratch VM of the release
# after ares-install of the pinned IPK and elevate-service (capture.sh); nothing here writes them.
#
# The layer is the same for every VM: it holds no key. The host key and root's authorized_keys are
# the VM's own (the rootssh ones, beanjar/vm/keys.py); the launcher puts them into the per-VM layer at
# /etc/emulator/hbchannel/{dropbear_ed25519_host_key,authorized_keys}, and
# usr/libexec/beanjar/hbchannel-start puts them where HBC's Dropbear reads them before it runs
# HBC's startup.sh, as a rooted TV's boot hook does.
#
# Contents besides STAGE (guest/layers/hbchannel/rootfs/):
#   emulator-hbchannel-install   Upstart job and systemd unit, before sam: hbchannel-install
#   emulator-hbchannel           Upstart job and systemd unit, after it: hbchannel-start
set -eu
HERE=$(dirname "$(readlink -f "$0")")
TOP=$(readlink -f "$HERE/../../..")

# The Homebrew Channel release: versions/<HBC>/release.env pins its IPK (VERSION, TAG, URL, SHA256).
HBC=${HBC:-$(cat "$HERE/versions/default")}
[ -f "$HERE/versions/$HBC/release.env" ] || { echo "hbchannel: no versions/$HBC" >&2; exit 1; }
. "$HERE/versions/$HBC/release.env"

DEST=${DEST:-$TOP/build/layers/$("$TOP/host/scripts/vm-launcher" config TARGET)/hbchannel}
CAPTURE=${CAPTURE:?a capture, guest/layers/hbchannel/versions/HBC/captured/RELEASE}
IPK_SHA256=${IPK_SHA256:-$SHA256}
APP=org.webosbrew.hbchannel
SVC=$APP.service
APPS=/media/developer/apps
SVC_DIR=$APPS/usr/palm/services/$SVC

# a capture of another IPK describes another install: capture.sh again after moving the pin
grep -qx "ipk $SHA256" "$CAPTURE/source.txt" 2>/dev/null ||
	{ echo "hbchannel: $CAPTURE is not of $HBC's IPK ($SHA256): run capture.sh" >&2; exit 1; }

sha() { sha256sum "$1" | cut -d' ' -f1; }

if [ -z "${IPK:-}" ]; then
	CACHE=${MESON_PACKAGE_CACHE_DIR:-$TOP/subprojects/packagecache}
	IPK=$CACHE/$SHA256-$(basename "$URL")	# by hash: releases may share a file name
	if [ ! -f "$IPK" ] || [ "$(sha "$IPK")" != "$IPK_SHA256" ]; then
		mkdir -p "$CACHE"
		echo "hbchannel: downloading $URL" >&2
		curl -fsSL --retry 3 -o "$IPK.part" "$URL"
		mv "$IPK.part" "$IPK"
	fi
fi
got=$(sha "$IPK")
if [ "$got" != "$IPK_SHA256" ]; then
	echo "hbchannel: $IPK: sha256 $got, expected $IPK_SHA256" >&2
	exit 1
fi

# An IPK is an ar archive (debian-binary, control.tar.gz, data.tar.gz).
CONTROL=$(ar p "$IPK" control.tar.gz | tar -xzO --wildcards '*control')
printf '%s\n' "$CONTROL" | grep -qx "Package: $APP" || { echo "hbchannel: $IPK is not $APP" >&2; exit 1; }
PKG_VERSION=$(printf '%s\n' "$CONTROL" | sed -n 's/^Version: //p')

rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$HERE/rootfs/." "$DEST/"
STAGE=$DEST/usr/share/beanjar/hbchannel
mkdir -p "$STAGE$APPS"
ar p "$IPK" data.tar.gz | tar -xz --no-same-owner -C "$STAGE$APPS"
[ -x "$STAGE$SVC_DIR/run-js-service" ] && [ -x "$STAGE$SVC_DIR/startup.sh" ] &&
	[ -f "$STAGE$APPS/usr/palm/applications/$APP/appinfo.json" ] ||
	{ echo "hbchannel: $IPK has no app, startup.sh or run-js-service" >&2; exit 1; }
printf '%s\n' "$IPK_SHA256" > "$STAGE/ipk.sha256"

# What the real install wrote besides the IPK's data, as captured, at the same guest paths.
cp -R "$CAPTURE/files/." "$STAGE/"
# The IPK's directories are 0777; nothing of the layer is writable but by its owner.
chmod -R u+w,go-w "$DEST"
echo "hbchannel: $DEST ($APP $PKG_VERSION, files of $(basename "$CAPTURE"))"
