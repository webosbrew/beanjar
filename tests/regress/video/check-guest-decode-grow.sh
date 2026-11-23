#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# A guest-decoded H.264 stream that grows mid-stream, 640x360 to 1280x720 to 1920x1080 (8 s each,
# 25 fps, one MPEG-TS with continuous timestamps and in-band SPS), played inside webOS on a running
# VM. The stand-in decoder's output goes to a fakesink, which is not one of ours, so it opens a
# DECODE session and the host hands the pictures back through the media pool
# (docs/dev/architecture/media-standins.md, "Decoding for the guest"); the fakesink keeps its last
# picture, as a display sink does, so each growth needs a new slot layout while the guest still
# holds a slot of the old one.
#   tests/regress/video/check-guest-decode-grow.sh VM [OUT]
# VM is a beanjar VM name (HOST/NAME for a remote host's). The clip is made with the host's ffmpeg
# (libx264) into OUT (default build/media-test/grow, never committed) and copied into the guest's
# /tmp over beanjar shell. DEC is the stand-in decoder's factory (default omxh264dec: MStar and
# Realtek; omx_lxvideodec on LX). PLAY=0 copies the clip and prints the command without playing.
# PASS when the guest's syslog has a "pictures from the host" line for each of the three sizes, in
# order, and the decoder's end line counts 600 pictures with 0 dropped; the media helper's decode
# lines (its run/media-helper.log) then show returned=600 and no slot-drops.
set -eu
VM=${1:?usage: check-guest-decode-grow.sh VM [OUT]}
TOP=$(cd "$(dirname "$0")/../../.." && pwd)
OUT=${2:-$TOP/build/media-test/grow}
DEC=${DEC:-omxh264dec}
CLIP=$OUT/grow.ts
if [ ! -f "$CLIP" ]; then
	mkdir -p "$OUT"
	font=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
	off=0
	for s in 640x360 1280x720 1920x1080; do
		t=
		[ -f $font ] && t=",drawtext=fontfile=$font:text='$s':fontsize=h/8:fontcolor=white:box=1:boxcolor=black@0.6:x=(w-tw)/2:y=(h-th)/2"
		ffmpeg -hide_banner -loglevel error -y -f lavfi -i "testsrc2=size=$s:rate=25:duration=8$t" \
			-c:v libx264 -profile:v high -level 4.0 -pix_fmt yuv420p -g 25 -bf 0 \
			-x264-params repeat-headers=1 -output_ts_offset $off -f mpegts "$OUT/part-$s.ts"
		off=$((off + 8))
	done
	cat "$OUT/part-640x360.ts" "$OUT/part-1280x720.ts" "$OUT/part-1920x1080.ts" >"$CLIP.part"
	rm -f "$OUT"/part-*.ts
	mv "$CLIP.part" "$CLIP"
fi
beanjar shell "$VM" sh -c 'cat > /tmp/grow.ts' <"$CLIP"
PIPE="gst-launch-1.0 filesrc location=/tmp/grow.ts ! tsdemux ! h264parse ! $DEC name=guestdec ! fakesink sync=true"
if [ "${PLAY:-1}" = 0 ]; then
	echo "beanjar shell $VM $PIPE"
	exit 0
fi
# the run's own syslog lines: those after the line count taken before it
LOG=$(beanjar shell "$VM" sh -c "n=\$(wc -l < /var/log/legacy-log); $PIPE >/dev/null 2>&1; echo rc=\$?; tail -n +\$((n + 1)) /var/log/legacy-log | grep -a 'guestdec'")
echo "$LOG" | cut -c1-260
sizes=$(echo "$LOG" | sed -n 's/.*pictures from the host: .*width=(int)\([0-9]*\), height=(int)\([0-9]*\).*/\1x\2/p' | tr '\n' ' ' | sed 's/ $//')
end=$(echo "$LOG" | grep -c 'host decoding ends: 600 pictures, 0 inputs unanswered, 0 dropped' || true)
case "$VM" in */*) ;; *)
	H=${XDG_DATA_HOME:-$HOME/.local/share}/beanjar/vms/$VM/run/media-helper.log
	[ -f "$H" ] && grep -a 'decode [0-9]* video session=.*(closed)' "$H" | tail -n 1 ;;
esac
if [ "$sizes" = "640x360 1280x720 1920x1080" ] && [ "$end" = 1 ]; then
	echo "PASS guest-decode-grow: sizes $sizes; 600 pictures, 0 dropped"
else
	echo "FAIL guest-decode-grow: sizes '$sizes', end line found $end time(s)"
	exit 1
fi
