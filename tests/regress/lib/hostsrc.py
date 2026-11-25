# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM host's source tree from git (scripts/regress; docs/dev/guides/build.md, "Regression runs").

Each VM host keeps a bare repository, BARE in its home, made on first use. A run pushes the
commit it tests there (git push: only the objects the host lacks), at REF_PREFIX<source commit>,
and checks it out into the host's checkout DEST, exactly: files the commit does not have go,
except what belongs to the host (PROTECTED: build outputs, its VM identity, scratch, what Meson
unpacks and caches under subprojects/). Unchanged files keep their modification times, so the
host's incremental build stays incremental. A push never touches DEST; only a run's checkout does,
under the host-wide lock (lock_cmd) that every writer of DEST takes.

Commands here are shell text for the VM host, run from its home directory over ssh, and argv for
this machine; nothing here runs them.
"""

import shlex

BARE = "beanjar-src.git"  # in the VM host's home
REF_PREFIX = "refs/hosts/"
# ssh for unattended use: no prompts, a host that is down fails in seconds
SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=10"
# What a checkout leaves alone in DEST: the patterns host/scripts/sync-pi.sh excludes from its
# --delete, as git clean exclude patterns (subprojects/packagefiles/ is the tracked part)
PROTECTED = (
    "/.git/",  # a repository a host may keep there of its own (git clean -ff would remove it)
    "/build/",
    "/local/",
    "/scratch/",
    "/subprojects/*/",
    "!/subprojects/packagefiles/",
    "/subprojects/packagecache/",
)


def ensure_cmd():
    """Make the bare repository where it is not there yet."""
    return f"test -d {BARE} || git init -q --bare {BARE}"


def has_cmd(sha):
    """Succeeds where the bare repository has commit sha."""
    return f"git --git-dir={BARE} cat-file -e {shlex.quote(sha)}^{{commit}} 2>/dev/null"


def push_argv(top, host, sha, ref):
    """Push commit sha from the repository top to host's BARE, as ref (REF_PREFIX...)."""
    return [
        "git",
        "-C",
        top,
        "-c",
        f"core.sshCommand={SSH_COMMAND}",
        "push",
        "-q",
        f"{host}:{BARE}",
        f"+{sha}:{ref}",
    ]


def index_name(dest):
    """The index of DEST's checkout, kept in the bare repository: one per checkout."""
    return "index-" + "".join(c if c.isalnum() or c in "-._" else "_" for c in dest)


def checkout_cmd(dest, sha):
    """Make DEST (relative to home) hold exactly the tree of commit sha, PROTECTED excepted:
    checkout -f writes what differs from DEST's index and removes what that index has and sha has
    not; git clean removes the rest that sha has not (untracked and ignored files alike)."""
    d = shlex.quote(dest)
    excludes = " ".join(f"-e {shlex.quote(p)}" for p in PROTECTED)
    return (
        f"mkdir -p {d} && cd {d} && "
        f'export GIT_DIR="$HOME/{BARE}" GIT_WORK_TREE="$PWD" '
        f'GIT_INDEX_FILE="$HOME/{BARE}/{index_name(dest)}" && '
        # refreshed first: a file whose time changed and content did not (touched, or sent again
        # by a --working-tree sync) is not rewritten
        "{ git update-index -q --refresh >/dev/null 2>&1 || true; } && "
        f"git checkout -q -f --detach {shlex.quote(sha)} && git clean -q -ffdx {excludes}"
    )


def lock_path(dest):
    """The host-wide lock of the checkout DEST: beside it, outside the tree a checkout cleans."""
    return f"{dest}.lock"


def lock_cmd(dest, who):
    """Hold DEST's lock while this command's stdin stays open (fail at once if it is held):
    prints LOCKED once held, else BUSY and who holds it. The holder writes who into the lock
    file's .who companion."""
    lock = shlex.quote(lock_path(dest))
    return (
        f"exec 9>>{lock}; if ! flock -n 9; then echo BUSY; cat {lock}.who 2>/dev/null; exit 1; fi; "
        f"printf '%s\\n' {shlex.quote(who)} > {lock}.who; echo LOCKED; cat >/dev/null"
    )
