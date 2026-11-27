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
printf '%s\n' "$input" | exec pre-commit hook-impl \
    --config "$root/.pre-commit-config.yaml" --hook-type pre-push \
    --hook-dir "$hookdir" -- "$@"
