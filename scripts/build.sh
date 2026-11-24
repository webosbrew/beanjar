#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Build (docs/dev/guides/build.md), with the system Meson and Ninja:
#
#   scripts/build.sh [guest|host|host-arm64]   default: guest and host on x86_64, host on the Pi
#                                              (the SDK is x86_64)
#
# host-arm64: the host build cross-compiled on the PC for arm64 (build/host-arm64, cross file
# meson/cross/debian-arm64.ini, Debian's arm64 libraries), published to build/publish-arm64 for the
# arm64 packages (packaging/debian/README.md).
#
# `meson setup` of build/guest-<chip>-<release> (cross file meson/cross/webos-sdk.ini, lgtv-sdk.ini for
# NetCast 4.5; the target of
# the firmware, host/scripts/vm-launcher config TARGET, e.g. build/guest-h13-afro -Dsoc=h13 -Dwebos=afro;
# the guest build reads no firmware, its facts are in guest/targets/facts) or build/host when it does
# not exist yet, then `meson compile`, which publishes to build/.
set -eu
TOP=$(cd "$(dirname "$0")/.." && pwd)

build() { # build DIR [SETUP ARGS...]
	d=$TOP/build/$1; shift
	[ -f "$d/build.ninja" ] || meson setup "$@" "$d" "$TOP"
	meson compile -C "$d"
}
what=${1:-}
[ -n "$what" ] || { [ "$(uname -m)" = x86_64 ] && what="guest host" || what=host; }
for w in $what; do
	case $w in
	guest)
		target=$("$TOP/host/scripts/vm-launcher" config TARGET)
		# NetCast 4.5 builds with the NetCast "lgtv" SDK, as scripts/build-packages.sh
		case $target in *-netcast45|*-netcast4) cross=lgtv-sdk.ini ;; *) cross=webos-sdk.ini ;; esac
		build "guest-$target" --cross-file "$TOP/meson/cross/$cross" -Dsoc="${target%-*}" -Dwebos="${target#*-}" ;;
	host) build host ;;
	host-arm64) build host-arm64 --cross-file "$TOP/meson/cross/debian-arm64.ini" ;;
	*) sed -n '2,9p' "$0" >&2; exit 1 ;;
	esac
done
