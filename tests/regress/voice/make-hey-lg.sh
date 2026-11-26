#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The synthetic "Hey LG" of run-hey-lg.py: Festival's HTS voice cmu_us_slt_arctic_hts (Debian's
# festival and festvox-us-slt-hts) says "Hey, L G.", made 16 kHz mono 16-bit, one second of
# silence before it and two after, peak -3 dBFS (sox, without dither, so the same bytes each time),
# as FLAC.
#
#   make-hey-lg.sh [OUT]      default: hey-lg-slt.flac beside this script
#
# Of the Debian voices tried on W22O 43.21.89's Sensory model (hiLG, operating point 7), this one
# fires; espeak-ng's en-us and flite's slt, rms and awb do not (docs/research/microphone-emulation.md).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-$here/hey-lg-slt.flac}
for tool in text2wave sox; do
	command -v "$tool" >/dev/null || { echo "make-hey-lg.sh: needs $tool (apt install festival festvox-us-slt-hts sox)" >&2; exit 1; }
done
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "Hey, L G." | text2wave -eval '(voice_cmu_us_slt_arctic_hts)' -o "$tmp/tts.wav"
sox -D "$tmp/tts.wav" -r 16000 -c 1 -b 16 "$tmp/out.flac" pad 1 2 norm -3
mv "$tmp/out.flac" "$out"
