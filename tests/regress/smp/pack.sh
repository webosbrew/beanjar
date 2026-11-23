#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Pack the SMP check app (app/, build/tests/regress/smp/smpcheck, an 8 s 720p30 H.264 clip with access
# unit delimiters) with ares-package into OUT_DIR, for the guest's dev/install:
#   tests/regress/smp/pack.sh OUT_DIR      (after scripts/build.sh guest; prints the .ipk's path)
set -e
top=$(cd "$(dirname "$0")/../../.." && pwd)
out=$1
[ -n "$out" ] || { echo "usage: $0 OUT_DIR" >&2; exit 2; }
tool=$(command -v ares-package || echo "$HOME/.cargo/bin/ares-package")
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp -r "$top/tests/regress/smp/app" "$tmp/app"
cp "$top/build/tests/regress/smp/smpcheck" "$tmp/app/"
ffmpeg -loglevel error -f lavfi -i testsrc2=size=1280x720:rate=30 -t 8 -c:v libx264 -profile:v main \
	-pix_fmt yuv420p -g 30 -bf 0 -bsf:v h264_metadata=aud=insert -f h264 "$tmp/app/clip.h264"
mkdir -p "$out"
"$tool" -o "$out" "$tmp/app" >&2
ls "$out"/com.example.smpcheck_*.ipk
