#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Git's pre-push hook: what is pushed is on stdin, for the inherited hook and the stamp check.
set -eu
hookdir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(git rev-parse --show-toplevel)
input=$(cat)
if [ -x "$hookdir/pre-push.inherited" ]; then
    printf '%s\n' "$input" | "$hookdir/pre-push.inherited" "$@"
fi
# Only main going to GitHub, where it is published, needs the CI check; source syncs to the
# project's own hosts (regress, netboot) are not held up. The check takes minutes, longer than
# GitHub keeps the connection git has already opened, so scripts/push-main runs it before the push
# and leaves a stamp; the hook only looks for the stamp.
case ${2:-} in *github.com[:/]*) ;; *) exit 0 ;; esac
while read -r _ sha remote_ref _; do
    case $sha in ''|0000000000000000000000000000000000000000) continue ;; esac
    [ "$remote_ref" = refs/heads/main ] || continue
    [ -f "$root/build/act/passed/$sha" ] && continue
    echo "pre-push: $sha has not passed the CI check: push it with scripts/push-main (or --no-verify)" >&2
    exit 1
done <<EOF
$input
EOF
