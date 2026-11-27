#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Inside webOS: the notifications starfish-media-pipeline sent to uMediaServer (loadCompleted,
# playing, paused, seekDone, currentTime, endOfStream, error ...), one line each with its time.
# Needs PmLog context smp.main at debug level: boot with PMLOG_DEBUG="smp.main" (see
# beanjar/runtime/qemu.py). Passive: a second subscriber to com.webos.media can disturb WebKit.
#   (the VM gets it with `host/scripts/vm-launcher put tests/regress/video/pipeline-events.sh` on the host)
#   webos-enter sh /mnt/host/tests/regress/video/pipeline-events.sh [LINES]
grep -a -h 'sendJsonStringToUMS' /var/log/messages |
	sed -n 's/^[0-9-]*T\([0-9:.]*\)Z.*payload\[\(.*\)\] *$/\1 \2/p' | cut -c1-200 | tail -n "${1:-80}"
