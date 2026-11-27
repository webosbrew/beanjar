# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The traffic inspector's mitmweb: the user's own mitmproxy, MIN_VERSION or newer.

Beanjar ships no mitmproxy. Debian 13 has no package of it; the documented install is
`pipx install mitmproxy` (INSTALL). mitmweb is ENV (NETNS_MITMWEB) if set, else `mitmweb` on PATH,
else ~/.local/bin/mitmweb, pipx's default, which a systemd user unit's PATH often lacks.

MIN_VERSION is what beanjar-web's login needs (`Authorization: Bearer` with web_password,
host/web/beanjar_web/mitm.py) and what mitm_addon.py and `--mode tun` are known to work with.
Below it, or without mitmweb, a mode=mitm VM still starts and goes out directly (netns.py); the
reason (problem()) is in the VM's log, RUN_DIR/mitmweb.json, `beanjar mitm show` and the manager.

mitmweb runs only in its sandbox (netns.mitmweb_sandbox: bubblewrap, mediasandbox.find); without bwrap it is
not usable either (check() says so), and the VM goes out directly as without mitmweb.

The version comes from the mitmproxy package's metadata beside mitmweb (a venv's, pipx's or a pip
--user install's: no Python start), else from `mitmweb --version`; either is kept per path and
mtime (check() runs every few seconds in the manager).
"""

import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from ..runtime import mediasandbox

ENV = "NETNS_MITMWEB"
MIN_VERSION = "12.0.1"
INSTALL = f"Install mitmproxy {MIN_VERSION} or newer: pipx install mitmproxy"
SANDBOX_PROBLEM = (
    "bubblewrap (bwrap) not found: mitmweb runs only in its sandbox. Install bubblewrap"
)


def find(env=None):
    """mitmweb's path: ENV, else `mitmweb` on PATH or in ~/.local/bin; None if there is none."""
    env = os.environ if env is None else env
    if env.get(ENV):
        return env[ENV]
    path = os.pathsep.join(x for x in (env.get("PATH"), os.path.expanduser("~/.local/bin")) if x)
    return shutil.which("mitmweb", path=path)


def parse(version):
    """A version's leading numbers as a tuple ("12.3.1" -> (12, 3, 1), "12.0.0.dev1" ->
    (12, 0, 0)); () if it has none."""
    m = re.match(r"\d+(?:\.\d+)*", (version or "").strip())
    return tuple(int(x) for x in m.group().split(".")) if m else ()


def _metadata_version(mitmweb):
    """The version of the mitmproxy package installed beside mitmweb, from its dist-info
    (PREFIX/lib/python*/{site,dist}-packages/mitmproxy-VERSION.dist-info, PREFIX the directory
    above mitmweb's bin/), or ""."""
    top = os.path.dirname(os.path.dirname(os.path.realpath(mitmweb)))
    found = []
    for packages in ("site-packages", "dist-packages"):
        pattern = os.path.join(top, "lib", "python*", packages, "mitmproxy-*.dist-info")
        found += glob.glob(pattern)
    versions = [os.path.basename(d)[len("mitmproxy-") : -len(".dist-info")] for d in found]
    return max(versions, key=parse, default="")


def _command_version(mitmweb, env=None):
    """mitmweb's version from `mitmweb --version` ("Mitmproxy: 12.3.1"), or ""."""
    try:
        out = subprocess.run(
            [mitmweb, "--version"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "mitmproxy":
            return value.strip()
    return ""


_versions = {}  # (realpath, mtime): version


def version(mitmweb, env=None):
    """mitmweb's mitmproxy version, or "" if it cannot be told."""
    try:
        key = (os.path.realpath(mitmweb), os.stat(mitmweb).st_mtime_ns)
    except OSError:
        return ""
    if key not in _versions:
        _versions[key] = _metadata_version(mitmweb) or _command_version(mitmweb, env)
    return _versions[key]


def python(mitmweb):
    """The interpreter mitmweb runs with, from its shebang (a pipx or venv script names its venv's
    python), or None."""
    try:
        with open(mitmweb, "rb") as f:
            line = f.readline(4096).decode(errors="replace").strip()
    except OSError:
        return None
    if not line.startswith("#!"):
        return None
    words = line[2:].split()
    if not words:
        return None
    if os.path.basename(words[0]) == "env" and len(words) > 1:
        return shutil.which(words[1])
    return words[0] if os.access(words[0], os.X_OK) else None


@dataclass(frozen=True)
class Tool:
    """What check() found: path ("" if none), version ("" if unknown) and problem ("" when it can
    be used, else why not and what to do)."""

    path: str
    version: str
    problem: str

    @property
    def usable(self):
        return not self.problem

    def describe(self):
        """One line for the manager: "mitmproxy 12.3.1" or the problem."""
        return self.problem or f"mitmproxy {self.version}"

    def brief(self):
        """The problem in a few words, for a status line ("mitmproxy not installed",
        "mitmproxy 11.1.3 too old", "bubblewrap not installed", "mitmweb unusable"), or "" when
        it can be used."""
        if not self.problem:
            return ""
        if self.problem == SANDBOX_PROBLEM:
            return "bubblewrap not installed"
        if not self.path or not os.path.exists(self.path):
            return "mitmproxy not installed"
        if self.version and parse(self.version) < parse(MIN_VERSION):
            return f"mitmproxy {self.version} too old"
        return "mitmweb unusable"


def problem(path, found_version):
    """Why mitmweb at path with found_version cannot serve, or ""."""
    if not path:
        return f"mitmweb not found. {INSTALL}"
    if not os.path.exists(path):
        return f"no mitmweb at {path}. {INSTALL}"
    if not os.access(path, os.X_OK):
        return f"mitmweb at {path} is not an executable. {INSTALL}"
    if not found_version:
        return f"could not tell the version of mitmweb at {path}. {INSTALL}"
    if parse(found_version) < parse(MIN_VERSION):
        return f"mitmproxy {found_version} at {path} is too old. {INSTALL}"
    return ""


def check(env=None):
    """The mitmweb a mode=mitm VM started with env would run: a Tool."""
    path = find(env) or ""
    found_version = version(path, env) if path and os.access(path, os.X_OK) else ""
    why = problem(path, found_version)
    if not why and not mediasandbox.find(env):
        why = SANDBOX_PROBLEM
    return Tool(path, found_version, why)
