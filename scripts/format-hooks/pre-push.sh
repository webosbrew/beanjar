#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Git's pre-push hook: what is pushed is on stdin, for the inherited hook and for pre-commit.
set -eu
hookdir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(git rev-parse --show-toplevel)
export PRE_COMMIT_HOME="$root/build/pre-commit"
input=$(cat)
if [ -x "$hookdir/pre-push.inherited" ]; then
    printf '%s\n' "$input" | "$hookdir/pre-push.inherited" "$@"
fi
# The CI check gates only main going to GitHub, where it is published; source syncs to the
# project's own hosts (regress, netboot) are not held up.
case ${2:-} in *github.com[:/]*) ;; *) exit 0 ;; esac
# the commit pushed to main (not a deletion), for scripts/act-check
unset ACT_CHECK_COMMIT
while read -r _ sha remote_ref remote_sha; do
    case $sha in ''|0000000000000000000000000000000000000000) continue ;; esac
    [ "$remote_ref" = refs/heads/main ] || continue
    export ACT_CHECK_COMMIT=$sha ACT_CHECK_BASE=$remote_sha
    break
done <<EOF
$input
EOF
[ -n "${ACT_CHECK_COMMIT:-}" ] || exit 0
printf '%s\n' "$input" | exec pre-commit hook-impl \
    --config "$root/.pre-commit-config.yaml" --hook-type pre-push \
    --hook-dir "$hookdir" -- "$@"
