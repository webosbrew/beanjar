#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Build Beanjar's Debian packages from a clean, full-history checkout, one build lane at a time.
# Run as root in a throwaway Debian 13 container (the Packages workflow, .github/workflows/packages.yml,
# for host, and the Guest workflow, .github/workflows/guest.yml, for guest): it installs packages
# into the system it runs in.
#
#   scripts/build-packages.sh host [OUT]    default OUT: build/ci-packages
#   scripts/build-packages.sh guest [OUT]
#
# host: beanjar, beanjar-ui, beanjar-vnc, beanjar-web and beanjar-qemu for this machine's
# architecture, amd64 or arm64. The prebuilt QEMU, FFmpeg (both builds) and neatvnc of vendor.toml
# downloaded and checked (meson/vendor.py, into build/vendor); role host's build dependencies
# (packaging/debian/build-deps.sh) installed; the host build in OUT/host, published to
# OUT/publish with the prebuilt trees; packaging/debian/build.py --host-only into OUT/debs.
#
# guest: beanjar-guest (architecture all), on amd64 only: the SDKs are x86_64. Role guest's build
# dependencies and ccache installed; the two SDKs (pinned below) downloaded to OUT/sdk, checked and
# unpacked in /opt, where the cross files look; then every target (guest/targets, the table
# host/core/beanjar/firmware/targets.py loads) built in OUT/guest-<target>, published to OUT/guest-publish
# and its build directory removed, with ccache in OUT/ccache. What targets have alike (the kernel,
# busybox, SSH, each Mesa configuration) is built once, in the checkout's build/<component>/<key>
# (meson/shared.py), which the loop keeps; packaging/debian/build.py --guest-only into OUT/debs. The guest build reads no firmware:
# each target's firmware facts are in guest/targets/facts.
set -eu
TOP=$(cd "$(dirname "$0")/.." && pwd)
lane=${1:-}
case $lane in host|guest) ;; *) sed -n '9,10p' "$0" >&2; exit 1 ;; esac
OUT=$(mkdir -p "${2:-$TOP/build/ci-packages}" && cd "${2:-$TOP/build/ci-packages}" && pwd)
export DEBIAN_FRONTEND=noninteractive

[ "$(id -u)" = 0 ] || { echo "build-packages.sh: run as root in a throwaway Debian 13 container" >&2; exit 1; }
grep -q '^VERSION_ID="13"' /etc/os-release || { echo "build-packages.sh: Debian 13 required" >&2; exit 1; }
arch=$(dpkg --print-architecture)
case $lane-$arch in host-amd64|host-arm64|guest-amd64) ;;
*) echo "build-packages.sh: no $lane lane on $arch" >&2; exit 1 ;; esac

step() { echo "== $* ($(date -u +%T))"; }
t0=$(date +%s)

step "base tools"
apt-get update
apt-get install -y --no-install-recommends git ca-certificates curl equivs dpkg-dev python3
# The checkout belongs to another user (the runner's, or the caller's): git refuses it otherwise,
# and the version (packaging/identity.py) comes from git
git config --global --get-all safe.directory | grep -qxF "$TOP" || git config --global --add safe.directory "$TOP"
[ "$(git -C "$TOP" rev-parse --is-shallow-repository)" = false ] || {
	echo "build-packages.sh: a shallow clone has no version (fetch the full history)" >&2; exit 1; }

host() {
	step "prebuilt programs ($arch)"
	python3 -I "$TOP/meson/vendor.py" tree "$arch" qemu ffmpeg ffmpeg-helper neatvnc
	t1=$(date +%s)

	step "build dependencies (role host)"
	"$TOP/packaging/debian/build-deps.sh" host
	apt-get install -y --no-install-recommends "$TOP"/build/packages/beanjar-build-deps-host_*_all.deb
	t2=$(date +%s)

	# There is no firmware for the cache; the virtual camera is packaged, so it is required.
	step "host build"
	[ -f "$OUT/host/build.ninja" ] || meson setup "$OUT/host" "$TOP" -Dpublish_dir="$OUT/publish" \
		-Dfirmware_cache=disabled -Dvirtcam=enabled
	meson compile -C "$OUT/host"
	t3=$(date +%s)

	step "packages"
	rm -f "$OUT"/debs/beanjar_* "$OUT"/debs/beanjar-ui_* "$OUT"/debs/beanjar-vnc_* "$OUT"/debs/beanjar-web_* \
		"$OUT"/debs/beanjar-qemu_*
	python3 "$TOP/packaging/debian/build.py" --host-only --host-publish "$OUT/publish" --output "$OUT/debs"
	t4=$(date +%s)
	show beanjar beanjar-ui beanjar-vnc beanjar-web beanjar-qemu
	echo "seconds: prebuilt $((t1 - t0)), build-deps $((t2 - t1)), host build $((t3 - t2)), packages $((t4 - t3)), total $((t4 - t0))"
}

# The SDKs, openlgtv/buildroot-nc4's release assets: the webOS Buildroot SDK
# (meson/cross/webos-sdk.ini) and the NetCast "lgtv" one (meson/cross/lgtv-sdk.ini)
SDKS="https://github.com/openlgtv/buildroot-nc4/releases/download/webos-36b9236/linux-intel-webos-sdk.tar.gz f5673fb8a4586bb11b736eb57283049369cb748f6d1d5bc65b749b8a074eba71
https://github.com/openlgtv/buildroot-nc4/releases/download/2026.08-webos/arm-lgtv-linux-gnueabi_sdk-buildroot-x86_64.tar.gz a8d82460b3a96a96cb2f3847df19d2c641672a9d8facf7141502c29dd86f9000"

guest() {
	step "build dependencies (role guest)"
	"$TOP/packaging/debian/build-deps.sh" guest
	apt-get install -y --no-install-recommends "$TOP"/build/packages/beanjar-build-deps-guest_*_all.deb ccache
	t1=$(date +%s)

	step "SDKs"
	mkdir -p "$OUT/sdk"
	echo "$SDKS" | while read -r url sum; do
		file=$OUT/sdk/${url##*/}
		echo "$sum  $file" | sha256sum -c --status 2>/dev/null || {
			curl -fsSL --retry 3 -o "$file.part" "$url"
			mv "$file.part" "$file"
			echo "$sum  $file" | sha256sum -c --quiet; }
		dir=/opt/$(tar tzf "$file" | head -n 1 | cut -d/ -f1)
		rm -rf "$dir"
		tar xzf "$file" -C /opt
		"$dir/relocate-sdk.sh" >/dev/null
	done
	t2=$(date +%s)

	step "guest builds"
	export CCACHE_DIR="$OUT/ccache"
	targets=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1])
from beanjar.firmware.install import targets
print(*(targets.name(t) for t in targets.all_targets()))' "$TOP/host/core")
	for target in $targets; do
		# the test guest is not packaged: its userland is beanjar-vendor's and its build CI's
		# (scripts/testguest-ci.sh)
		[ "$target" != virt-testguest ] || continue
		step "guest $target"
		case $target in *-netcast45) cross=lgtv-sdk.ini ;; *) cross=webos-sdk.ini ;; esac
		dir=$OUT/guest-$target
		[ -f "$dir/build.ninja" ] || meson setup --cross-file "$TOP/meson/cross/$cross" "$dir" "$TOP" \
			-Dsoc="${target%-*}" -Dwebos="${target#*-}" -Dpublish_dir="$OUT/guest-publish"
		meson compile -C "$dir"
		rm -rf "${dir:?}"
	done
	t3=$(date +%s)

	step "packages"
	rm -f "$OUT"/debs/beanjar-guest_* "$OUT"/debs/beanjar-guest-x86-64_*
	python3 "$TOP/packaging/debian/build.py" --guest-only --guest-publish "$OUT/guest-publish" \
		--output "$OUT/debs"
	t4=$(date +%s)
	show beanjar-guest beanjar-guest-x86-64
	echo "seconds: build-deps $((t1 - t0)), SDKs $((t2 - t1)), guest builds $((t3 - t2)), packages $((t4 - t3)), total $((t4 - t0))"
}

show() { # show PACKAGE...: the lane's .deb files and their dependencies
	for name; do
		for deb in "$OUT/debs/${name}"_*.deb; do
			[ -f "$deb" ] || continue
			echo "$(basename "$deb"): $(du -h "$deb" | cut -f1), Depends: $(dpkg-deb -f "$deb" Depends)"
		done
	done
}

$lane
