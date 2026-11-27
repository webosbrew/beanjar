#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The source identity of a build and the compatibility rules of the packages
(packaging/debian/README.md, "Versions and compatibility").

  identity.py source-version --top TOP --out FILE   resolve TOP's identity with git, into FILE
  identity.py source-version --top TOP --rev REV --out -   commit REV's identity, to stdout

packaging/VERSION holds the semver. The version is X.Y.Z at the commit that last changed that file and
X.Y.Z+gitN.g<12 hex> N commits later. The build lanes resolve it once and publish it beside their
artefacts (the host lane's source-version.toml, the guest lanes' guest-installation.toml); packaging
reads those files and never runs git. A FILE is rewritten only when its content changes.

A tree checked out without its repository (a regression VM host's checkout, which git writes from a
bare repository elsewhere: tests/regress/lib/hostsrc.py) has no history to count. The checkout
writes STAMP at its top, the identity of the commit it holds as resolved where the history is
(--rev), and the build reads that instead.

The version's major.minor while the major is 0 (0.2), the major from 1.0 (1), is the host-guest
contract the packages' dependencies express (contract): an apt hint, not a runtime check.
"""

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

TOP = Path(__file__).resolve().parents[1]
VERSION_FILE = "packaging/VERSION"
STAMP = "source-identity.toml"  # at the top of a tree checked out without its repository
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def _semver():
    """host/core/beanjar/semver.py: the version pattern and the contract rule, shared with the manager."""
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("beanjar_semver", TOP / "host/core/beanjar/semver.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


semver = _semver()
VERSION = semver.VERSION


class Error(Exception):
    pass


def base_version(top=TOP):
    """The X.Y.Z of packaging/VERSION."""
    path = Path(top) / VERSION_FILE
    try:
        text = path.read_text()
    except OSError as e:
        raise Error(f"{path}: {e.strerror or e}") from None
    return _semver_of(text, path)


def _semver_of(text, where):
    text = text.strip()
    if not SEMVER.fullmatch(text):
        raise Error(f"{where}: {text!r} is not a <major>.<minor>.<patch> version")
    return text


def version_of(base, count, commit):
    """The package version of base (packaging/VERSION) count commits after the one that set it."""
    return base if count == 0 else f"{base}+git{count}.g{commit[:12]}"


def contract(version):
    """The host-guest contract of a version (semver.contract): 0.2 for 0.2.x, 1 for 1.x."""
    try:
        return semver.contract(version)
    except ValueError as e:
        raise Error(str(e)) from None


def next_contract(version):
    """The first version past version's contract (0.3 for 0.2.x, 2 for 1.x)."""
    return semver.next_contract(version)


def _git(top, *args):
    try:
        r = subprocess.run(
            ["git", "-C", str(top), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise Error(
            "git is not installed: the build lanes resolve the source version with it"
        ) from None
    if r.returncode != 0:
        raise Error(f"git {' '.join(args)}: {r.stderr.strip() or 'failed'}")
    return r.stdout.strip()


def resolve(top=TOP, rev=None):
    """{version, commit, commit_epoch, dirty, shallow} of the checkout at top; with rev, of that
    commit of top's repository (never dirty). A top that is no repository's work tree (no .git)
    and has STAMP: what STAMP says."""
    stamp = Path(top) / STAMP
    if rev is None and not (Path(top) / ".git").exists() and stamp.is_file():
        return read(stamp)
    commit = _git(top, "rev-parse", "--verify", f"{rev or 'HEAD'}^{{commit}}")
    base = (
        base_version(top)
        if rev is None
        else _semver_of(_git(top, "show", f"{commit}:{VERSION_FILE}"), f"{VERSION_FILE} at {rev}")
    )
    last = _git(top, "log", "-1", "--format=%H", commit, "--", VERSION_FILE)
    if not last:
        raise Error(f"{VERSION_FILE} is not committed: commit it, then build")
    return {
        "version": version_of(
            base, int(_git(top, "rev-list", "--count", f"{last}..{commit}")), commit
        ),
        "commit": commit,
        "commit_epoch": int(_git(top, "log", "-1", "--format=%ct", commit)),
        "dirty": rev is None and bool(_git(top, "status", "--porcelain", "--untracked-files=no")),
        "shallow": _git(top, "rev-parse", "--is-shallow-repository") == "true",
    }


def dump(data):
    """TOML of a flat table of strings, integers and booleans."""

    def value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    return "".join(f"{k} = {value(v)}\n" for k, v in data.items())


def write(path, data, header=""):
    """path holds data (dump), rewritten only when it changes."""
    text = header + dump(data)
    path = Path(path)
    try:
        if path.read_text() == text:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def read(path, guest=False):
    """A published source-version.toml (guest: a guest lane's guest-installation.toml, the same
    fields); Error naming the file when it is missing or invalid."""
    what = "guest build stamp" if guest else "source version metadata"
    try:
        data = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        raise Error(
            f"{path}: {what} is missing: build it with this tree's build lane, which publishes it"
        ) from None
    except (OSError, ValueError) as e:
        raise Error(f"{path}: invalid {what}: {e}") from None
    checks = {
        "version": lambda v: isinstance(v, str) and VERSION.fullmatch(v),
        "commit": lambda v: isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}", v),
        "commit_epoch": lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
        "dirty": lambda v: isinstance(v, bool),
        "shallow": lambda v: isinstance(v, bool),
    }
    for key, ok in checks.items():
        if not ok(data.get(key)):
            raise Error(f"{path}: invalid {what}: {key} is missing or malformed")
    return {k: data[k] for k in checks}


def releasable(data, path):
    """Error unless data (read) comes from a clean, complete checkout: packages are made only of those."""
    if data["dirty"]:
        raise Error(
            f"{path}: built from a tree with uncommitted changes: commit, rebuild, then package"
        )
    if data["shallow"]:
        raise Error(
            f"{path}: built from a shallow clone: fetch the full history, rebuild, then package"
        )
    return data


HEADER = "# generated by packaging/identity.py at build time: the source this build was made from\n"


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("source-version")
    s.add_argument("--top", type=Path, default=TOP)
    s.add_argument("--rev", help="a commit of TOP's repository instead of its checkout")
    s.add_argument("--out", required=True, help="FILE, or - for stdout")
    a = p.parse_args(argv)
    try:
        data = resolve(a.top, a.rev)
        if a.out == "-":
            sys.stdout.write(HEADER + dump(data))
        else:
            write(Path(a.out), data, HEADER)
    except Error as e:
        print(f"identity: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
