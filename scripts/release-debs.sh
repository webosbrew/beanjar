#!/bin/bash
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Put .deb files into the GitHub release of a tag v<version>, which the Packages and the Guest
# workflows both fill (packaging/debian/README.md, "Built on GitHub"):
#
#   GH_REPO=OWNER/NAME scripts/release-debs.sh v<version> FILE.deb...
#
# Each FILE is a package as the build names it, <package>_<version>_<arch>.deb of the tag's version,
# and goes into the release under that name.
# The two run at the same time, so either may come first. The release is created unless it exists;
# a create that fails because the other workflow has just created it falls through to the upload.
# The files are uploaded with --clobber (a rerun replaces them). Then one SHA256SUMS of every .deb
# in the release is written from the SHA-256 digests GitHub lists for its assets (each of this
# run's own files checked against its local sum first) and uploaded, and the release is listed
# again: when another .deb has appeared, or SHA256SUMS is not the one written (the other workflow
# wrote its own meanwhile), it is written again. Whichever writes last has seen every .deb, so the
# file ends complete. The release is created not Latest, and marked Latest once it holds both the
# host packages of both architectures and beanjar-guest. Needs gh, logged in with contents: write
# (GH_TOKEN), and GH_REPO; GITHUB_SHA, when set, goes into the notes.
set -euo pipefail

die() {
	echo "release-debs: $*" >&2
	exit 1
}

[ $# -ge 2 ] || die "usage: GH_REPO=OWNER/NAME $0 v<version> FILE.deb..."
[ -n "${GH_REPO:-}" ] || die "GH_REPO is not set"
tag=$1
shift
version=${tag#v}
[ "$tag" != "$version" ] && [ -n "$version" ] || die "$tag is not a tag v<version>"
for f; do
	[ -f "$f" ] || die "no file $f"
done

# how often a step that may lose a race with the other workflow is tried, and the pause between
TRIES=${RELEASE_TRIES:-10}
PAUSE=${RELEASE_PAUSE:-6}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# This run's files, of the tag's version
for f; do
	name=${f##*/}
	IFS=_ read -r package built arch rest <<<"${name%.deb}"
	[ "$name" = "${package}_${built}_${arch}.deb" ] && [ -z "$rest" ] ||
		die "$f is not named <package>_<version>_<arch>.deb"
	[ "$built" = "$version" ] || die "$f is version $built, the tag $tag names $version"
done

exists() {
	gh release view "$tag" --json tagName >/dev/null 2>&1
}

notes() {
	local built=""
	[ -z "${GITHUB_SHA:-}" ] || built=", built from $GITHUB_SHA"
	cat <<EOF
Beanjar $version for Debian 13$built.

Install:

\`\`\`sh
mkdir beanjar-debs && cd beanjar-debs
version=$version
arch=\$(dpkg --print-architecture)
url=https://github.com/$GH_REPO/releases/download/v\$version
wget \$url/beanjar_\${version}_\$arch.deb \$url/beanjar-qemu_\${version}_\$arch.deb \$url/beanjar-ui_\${version}_all.deb \$url/beanjar-guest_\${version}_all.deb
sudo apt-get install ./beanjar_\${version}_\$arch.deb ./beanjar-qemu_\${version}_\$arch.deb ./beanjar-ui_\${version}_all.deb ./beanjar-guest_\${version}_all.deb
\`\`\`

The optional packages, one line each in the same shell:

\`\`\`sh
wget \$url/beanjar-vnc_\${version}_\$arch.deb && sudo apt-get install ./beanjar-vnc_\${version}_\$arch.deb
wget \$url/beanjar-web_\${version}_all.deb && sudo apt-get install ./beanjar-web_\${version}_all.deb
wget \$url/beanjar-guest-x86-64_\${version}_amd64.deb && sudo apt-get install ./beanjar-guest-x86-64_\${version}_amd64.deb
\`\`\`

- \`beanjar\`: the core, the command line and the VM launcher.
- \`beanjar-qemu\`: Beanjar's patched QEMU.
- \`beanjar-ui\`: the VM manager and viewer windows.
- \`beanjar-vnc\` (optional): a VM's TV picture over VNC.
- \`beanjar-web\` (optional): VMs in a web browser.
- \`beanjar-guest\`: the guest side, the same for every host: the guest kernel and what runs beside the TV's programs.
- \`beanjar-guest-x86-64\` (amd64, optional): the experimental x86-64 guest kernel. It does nothing until a VM is switched to it with \`beanjar config set <vm> guest_kernel=x86-64\`.

\`SHA256SUMS\` lists the SHA-256 of every package. The release gets the host and the guest packages from two workflows; until both have finished it is incomplete and not marked Latest.
EOF
}

# The release
if ! exists; then
	notes >"$work/notes"
	if ! gh release create "$tag" --verify-tag --latest=false \
		--title "Beanjar $version" --notes-file "$work/notes"; then
		exists || die "could not create the release $tag"
		echo "release-debs: $tag was created meanwhile; uploading into it"
	fi
fi

# This run's files
gh release upload "$tag" --clobber -- "$@"
sha256sum -- "$@" | sed 's|  .*/|  |' >"$work/own"

# "<sha256>  <name>" of every .deb in the release, by name, and SHA256SUMS's sha256 (or nothing),
# from one listing
list() {
	gh release view "$tag" --json assets --jq '.assets[]
		| if .name == "SHA256SUMS" then "SUMS \(.digest // "")"
		  elif (.name | endswith(".deb")) then "\(.digest // "")  \(.name)"
		  else empty end' | sed 's/sha256://' >"$work/listing"
	grep -v '^SUMS ' "$work/listing" | LC_ALL=C sort -k2 >"$work/listed" || true
	sed -n 's/^SUMS //p' "$work/listing" >"$work/listed-sums"
}

written=""
for ((try = 1; ; try++)); do
	[ "$try" -le "$TRIES" ] || die "SHA256SUMS did not settle after $TRIES tries"
	[ "$try" = 1 ] || sleep "$PAUSE"
	list
	if [ -n "$written" ] && cmp -s "$work/listed" "$work/SHA256SUMS" &&
		[ "$(cat "$work/listed-sums")" = "$written" ]; then
		break
	fi
	# a digest GitHub has not listed yet: ask again
	if grep -q '^  ' "$work/listed"; then
		continue
	fi
	# this run's files as uploaded, or not listed yet: ask again
	missing=""
	while read -r sum name; do
		listed=$(awk -v n="$name" '$2 == n { print $1 }' "$work/listed")
		[ -n "$listed" ] || missing=1
		[ -z "$listed" ] || [ "$listed" = "$sum" ] || die "$name: the release lists $listed, built $sum"
	done <"$work/own"
	[ -z "$missing" ] || continue
	cp "$work/listed" "$work/SHA256SUMS"
	# both workflows replace it; an upload that collides with the other's is tried again
	if gh release upload "$tag" --clobber -- "$work/SHA256SUMS"; then
		written=$(sha256sum <"$work/SHA256SUMS" | cut -d' ' -f1)
	else
		written=""
	fi
done
echo "release-debs: $tag SHA256SUMS:"
cat "$work/SHA256SUMS"

# Latest once both halves are in
cut -d' ' -f3 "$work/SHA256SUMS" >"$work/names"
if grep -qxF "beanjar_${version}_amd64.deb" "$work/names" &&
	grep -qxF "beanjar_${version}_arm64.deb" "$work/names" &&
	grep -qxF "beanjar-guest_${version}_all.deb" "$work/names"; then
	gh release edit "$tag" --latest >/dev/null
	echo "release-debs: $tag has the host and the guest packages: marked Latest"
else
	echo "release-debs: $tag is waiting for the other workflow's packages; not marked Latest yet"
fi
