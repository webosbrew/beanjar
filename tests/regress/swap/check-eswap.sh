#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Run inside the webOS VM after rest-boot-done, optionally with --pressure.
# Stock scripts and real kernel counters are the expected behavior; no mocked EFS.
set -eu
test "$(systemctl --no-pager show swap-on.service -p Result --value)" = success
test "$(systemctl --no-pager show swap-on.service -p ActiveState --value)" = active
# The mapping is made once, by its own unit; the stock tasks run nothing of ours themselves.
test "$(systemctl --no-pager show beanjar-eswap.service -p Result --value)" = success
test "$(systemctl --no-pager show beanjar-eswap.service -p ActiveState --value)" = active
for unit in efs-on swap-on f2io-on; do
    [ -f "/etc/systemd/system/$unit.service" ] || continue
    ! systemctl --no-pager show "$unit.service" -p ExecStartPre --value | grep -q beanjar-eswap
done
if [ -f /etc/systemd/system/f2io-on.service ]; then
    test "$(systemctl --no-pager show f2io-on.service -p Result --value)" = success
    test -b /dev/f2io-0
    test "$(readlink -f /dev/f2io-0)" = "$(readlink -f /dev/mapper/eswap)"
else
    test "$(systemctl --no-pager show efs-on.service -p Result --value)" = success
fi
test "$(cat /sys/kernel/mm/low_mem_notify/efs_swap)" = 1
test -e /lg/cmn_data/efs/hib.disable
test ! -b /dev/mapper/apphib
awk '$1 ~ /^\/dev\/(mapper\/eswap|f2io-0)$/ && $3 > 0 {found=1} END {exit !found}' /proc/swaps
dmsetup table eswap | awk '$1 == 0 && $2 > 0 && $3 == "linear" {found=1} END {exit !found}'
cat /proc/swaps
echo "PASS: stock EFS tasks activated ordinary disk-backed swap"

if [ "${1:-}" = --pressure ]; then
    file=/run/beanjar-swap-pressure
    trap 'rm -f "$file" "$file.sha256"' EXIT
    trap 'exit 1' HUP INT TERM
    available=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
    free_swap=$(awk '/^SwapFree:/ {print int($2/1024)}' /proc/meminfo)
    room=$(df -k /run | awk 'END {print int($4/1024)}')
    count=$((available + 128))
    # Keep enough swap for the running firmware and enough tmpfs headroom.
    test "$free_swap" -ge 256
    test "$count" -lt "$((room - 32))"
    before=$(awk '$1 == "pswpout" {print $2}' /proc/vmstat)
    dd if=/dev/zero of="$file" bs=1048576 count="$count"
    sha256sum "$file" > "$file.sha256"
    after=$(awk '$1 == "pswpout" {print $2}' /proc/vmstat)
    test "$after" -gt "$before"
    sha256sum -c "$file.sha256"
    printf 'PASS: %s MiB pressure; swap-out pages %s -> %s; data checksum preserved\n' "$count" "$before" "$after"
    cat /proc/swaps
fi
