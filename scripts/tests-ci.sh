#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The Tests workflow's jobs (.github/workflows/tests.yml, docs/dev/guides/testing.md "In CI").
# Run as root in a throwaway Debian 13 container: it installs packages into the system it runs in.
#
#   scripts/tests-ci.sh lint    the formatting check (scripts/format check), reuse lint and the
#                               formatting tests (tests/unit/formatting), which need those tools
#   scripts/tests-ci.sh unit    the unit tier and the compiled checks of a host build
#
# lint: Debian's clang-format and reuse; ruff and pre-commit at the versions pyproject.toml and
# .pre-commit-config.yaml ask for, with pipx (Debian 13 has no ruff and an older pre-commit).
#
# unit: the prebuilt QEMU, FFmpeg (both builds) and neatvnc of packaging/vendor.toml
# (meson/vendor.py, into build/vendor) and role host's build dependencies (packaging/debian/build-deps.sh), as the
# Packages workflow's build has them; the pinned Homebrew Channel IPKs in Meson's package cache;
# the host build in build/host, then `meson test --suite unit`, the failures' logs printed. Its
# exit status is the tests'.
set -eu
TOP=$(cd "$(dirname "$0")/.." && pwd)
job=${1:-}
case $job in lint|unit) ;; *) sed -n '7,9p' "$0" >&2; exit 1 ;; esac
export DEBIAN_FRONTEND=noninteractive

[ "$(id -u)" = 0 ] || { echo "tests-ci.sh: run as root in a throwaway Debian 13 container" >&2; exit 1; }
grep -q '^VERSION_ID="13"' /etc/os-release || { echo "tests-ci.sh: Debian 13 required" >&2; exit 1; }

step() { echo "== $* ($(date -u +%T))"; }
t0=$(date +%s)

step "base tools"
apt-get update
apt-get install -y --no-install-recommends git ca-certificates python3
# The checkout belongs to another user (the runner's, or the caller's): git refuses it otherwise
git config --global --get-all safe.directory | grep -qxF "$TOP" || git config --global --add safe.directory "$TOP"

lint() {
	apt-get install -y --no-install-recommends clang-format reuse pipx python3-pytest
	# pipx's tools on the PATH whatever HOME is (a container job's is not root's)
	export PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin
	pipx install ruff==0.17.0
	pipx install pre-commit==4.7.0
	t1=$(date +%s)
	# no .ruff_cache left in the checkout, owned by root
	export RUFF_NO_CACHE=true
	rc=0
	step "formatting"
	"$TOP/scripts/format" check || rc=1
	step "licences"
	reuse --root "$TOP" lint -q || { reuse --root "$TOP" lint | tail -40; rc=1; }
	# the hooks' tests skip without the tools, so the unit job skips them: they run here
	step "formatting tests"
	(cd "$TOP" && python3 -m pytest -q --no-header -p no:cacheprovider -ra tests/unit/formatting) || rc=1
	t2=$(date +%s)
	echo "seconds: tools $((t1 - t0)), checks $((t2 - t1)), total $((t2 - t0))"
	return $rc
}

unit() {
	apt-get install -y --no-install-recommends curl xz-utils equivs dpkg-dev
	arch=$(dpkg --print-architecture)
	step "prebuilt programs ($arch)"
	python3 -I "$TOP/meson/vendor.py" tree "$arch" qemu ffmpeg ffmpeg-helper neatvnc

	step "build dependencies (role host)"
	"$TOP/packaging/debian/build-deps.sh" host
	apt-get install -y --no-install-recommends "$TOP"/build/packages/beanjar-build-deps-host_*_all.deb

	# The hbchannel layer's tests build it from each pinned release's IPK when Meson's package cache
	# has it; a guest build downloads it there (guest/layers/hbchannel/build-layer.sh), as here.
	step "Homebrew Channel releases"
	cache=${MESON_PACKAGE_CACHE_DIR:-$TOP/subprojects/packagecache}
	mkdir -p "$cache"
	for env in "$TOP"/guest/layers/hbchannel/versions/*/release.env; do
		(
			. "$env"
			ipk=$cache/$SHA256-${URL##*/}
			[ -f "$ipk" ] && [ "$(sha256sum "$ipk" | cut -d' ' -f1)" = "$SHA256" ] && exit 0
			curl -fsSL --retry 3 -o "$ipk.part" "$URL"
			[ "$(sha256sum "$ipk.part" | cut -d' ' -f1)" = "$SHA256" ] ||
				{ echo "tests-ci.sh: $URL: not sha256 $SHA256" >&2; rm -f "$ipk.part"; exit 1; }
			mv "$ipk.part" "$ipk"
		)
	done
	t1=$(date +%s)

	# There is no firmware for the cache; the virtual camera is built, so check-virtcam runs.
	step "host build"
	[ -f "$TOP/build/host/build.ninja" ] || meson setup "$TOP/build/host" "$TOP" \
		-Dfirmware_cache=disabled -Dvirtcam=enabled
	meson compile -C "$TOP/build/host"
	t2=$(date +%s)

	step "unit tests"
	rc=0
	meson test -C "$TOP/build/host" --suite unit --print-errorlogs || rc=$?
	t3=$(date +%s)
	echo "seconds: packages $((t1 - t0)), host build $((t2 - t1)), tests $((t3 - t2)), total $((t3 - t0))"
	return $rc
}

$job
