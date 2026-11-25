#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Inside webOS: did AcbService's video pipeline time out? Stock AcbService applies an app video's
# area (NDL SetArea, setCustomDisplayWindow) only at the end of a chain of luna calls with a 10 s
# limit each (videosinkmanager/connect first). When one runs out ("Timeout To Receive Luna API
# Response") the area never reaches the scaler and the picture keeps the previous full-screen
# rectangle: a run like that says nothing about the area, so a test of the area must report "no
# result" for it, never a pass or a fail (docs/research/video-resize-and-ndl-zoom.md, "#290 live run").
#   (the VM gets it with `host/scripts/vm-launcher put tests/regress/video/acb-timeout.sh` on the host)
#   webos-enter sh /mnt/host/tests/regress/video/acb-timeout.sh --mark       print the log's next line number
#   webos-enter sh /mnt/host/tests/regress/video/acb-timeout.sh [FIRST_LINE]  check the log from FIRST_LINE (default 1)
# Prints "ACB OK" and exits 0, or "ACB TIMEOUT <n> ..." and exits 3 (no result). LOG overrides the log.
log=${LOG:-/var/log/messages}
if [ "$1" = "--mark" ]; then
	echo $(($(wc -l < "$log") + 1))
	exit 0
fi
n=$(tail -n +"${1:-1}" "$log" | grep -a -i 'acb' | grep -a -c 'Timeout To Receive Luna API Response')
if [ "$n" -gt 0 ]; then
	echo "ACB TIMEOUT $n: AcbService's pipeline gave up on a luna call; the app's area was not applied, so the run has no result"
	exit 3
fi
echo "ACB OK"
