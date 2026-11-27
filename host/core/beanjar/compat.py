# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Host-guest compatibility, checked by what the guest payload has rather than by version numbers
(docs/dev/architecture/build-and-boot.md, "Host and guest compatibility").

Before a VM is created or started, components() makes sure the payload has every always-used
component of the firmware's target; before it boots, module_params() makes sure every parameter the host
passes to a guest module (boot.conf's MODULE_PARAMS_*) is one that module's .ko declares in its
.modinfo. The versions (the host's and the guest payload's installation.toml) are information only:
they go into run/vm-info.toml's [beanjar] table for logs and messages, and nothing is refused by them.

The identities (host_identity, guest_identity: a version and the commit it was built from) are what
`beanjar --version --json` reports; drift() compares two beanjars' (this one and another host's),
and a host's beanjar with its guest payload, for the warnings of commands sent to another host and
for the messages of refusals that skew explains. A difference warns; it never refuses.
"""

import functools
import json
import os
import re
import tomllib

from . import semver
from .firmware import targets

MODINFO_CACHE = "modinfo-params.json"


class Error(Exception):
    pass


def _version(path, read):
    try:
        value = read(path)
    except (OSError, ValueError):
        return "unknown"
    return value if isinstance(value, str) and value.strip() else "unknown"


def _toml_version(path):
    with open(path, "rb") as f:
        return tomllib.load(f).get("version")


def _text(path):
    with open(path) as f:
        return f.read().strip()


def _stamp(path):
    """{"version", "commit"} of an installation.toml or a build lane's stamp: "unknown" and None for
    what it lacks or cannot say."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        data = {}
    version, commit = data.get("version"), data.get("commit")
    return {
        "version": version if isinstance(version, str) and version.strip() else "unknown",
        "commit": commit
        if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)
        else None,
    }


def _checkout_identity(tree):
    """A checkout's {"version", "commit", "dirty"}: packaging/identity.py's (git, or the STAMP of a
    regression host's checkout, which has no history), else its host build's stamp
    (build/source-version.toml), else packaging/VERSION with no commit."""
    from importlib.util import module_from_spec, spec_from_file_location

    try:
        spec = spec_from_file_location("beanjar_identity", tree.src("packaging", "identity.py"))
        identity = module_from_spec(spec)
        spec.loader.exec_module(identity)
        data = identity.resolve(tree.top)
        return {"version": data["version"], "commit": data["commit"], "dirty": data["dirty"]}
    except Exception:  # no git, no history, no packaging/: the next best answer
        pass
    stamp = _stamp(tree.build("source-version.toml"))
    if stamp["commit"]:
        return dict(stamp, dirty=False)
    return {
        "version": _version(tree.src("packaging", "VERSION"), _text),
        "commit": None,
        "dirty": False,
    }


@functools.cache
def host_identity():
    """The running host's {"version", "commit", "dirty"}: its installation.toml, or the checkout's
    (_checkout_identity); "unknown" and None for what cannot be known."""
    from . import paths

    tree = paths.TREE
    if tree.installed:
        return dict(_stamp(os.path.join(tree.top, "installation.toml")), dirty=False)
    return _checkout_identity(tree)


def guest_identity(tree):
    """The guest payload's {"version", "commit"}: its installation.toml, or a checkout's
    build/guest-installation.toml."""
    return _stamp(tree.build("guest-installation.toml"))


def host_version():
    """The running host's version (host_identity), or "unknown"."""
    return host_identity()["version"]


def guest_version(tree):
    """The guest payload's version (guest_identity), or "unknown": information only."""
    return guest_identity(tree)["version"]


def run_info(tree):
    """The [beanjar] table of run/vm-info.toml: the versions of this launch, for logs and messages."""
    return {"host_version": host_version(), "guest_version": guest_version(tree)}


def commit_of(ident):
    """The commit of an identity ({"version", "commit"}), as much as it says: its full commit, else
    the 12 hex digits of an X.Y.Z+gitN.g<hex> version, else None."""
    if not isinstance(ident, dict):
        return None
    if isinstance(ident.get("commit"), str) and ident["commit"]:
        return ident["commit"]
    m = (
        semver.VERSION.fullmatch(ident.get("version") or "")
        if isinstance(ident.get("version"), str)
        else None
    )
    return m["hash"] if m and m["hash"] else None


def same_build(a, b):
    """Whether identities a and b come from one commit: their commits, as far as both say (a
    12-digit prefix matches a full one), else their versions. Unknown is never the same."""
    ca, cb = commit_of(a), commit_of(b)
    if ca and cb:
        n = min(len(ca), len(cb))
        return ca[:n] == cb[:n]
    va, vb = (a or {}).get("version"), (b or {}).get("version")
    return bool(va) and va != "unknown" and va == vb


def label(ident):
    """An identity as messages show it: VERSION, with the commit when the version does not carry it."""
    version = (ident or {}).get("version") or "unknown"
    commit = (ident or {}).get("commit")
    m = semver.VERSION.fullmatch(version)
    if commit and not (m and m["hash"]):
        return f"{version} ({commit[:12]})"
    return version


PACKAGES_DOC = "packaging/debian/README.md"


def package_fix(host, version):
    """The command that puts the packages of version on a host (host None: this one)."""
    where = f"on {host}: " if host else ""
    if not semver.valid(version):
        return (
            f"{where}install the beanjar and beanjar-guest packages of one build ({PACKAGES_DOC})"
        )
    return (
        f"{where}sudo apt install ./beanjar{{,-qemu,-vnc}}_{version}_$(dpkg --print-architecture).deb "
        f"./beanjar-{{ui,web,guest}}_{version}_all.deb (python3 packaging/debian/build.py makes "
        f"them; {PACKAGES_DOC})"
    )


def guest_fix(host, ident, installed):
    """The command that gives a beanjar of identity ident the guest payload of its own commit."""
    where = f"on {host}: " if host else ""
    if installed:
        if not semver.valid(ident.get("version")):
            return f"{where}install the beanjar-guest of this beanjar's build ({PACKAGES_DOC})"
        return (
            f"{where}sudo apt install ./beanjar-guest_{ident['version']}_all.deb "
            f"(python3 packaging/debian/build.py makes it; {PACKAGES_DOC})"
        )
    return f"{where}FW=<fw> scripts/build.sh guest in the checkout, or sync its build/ again"


def drift(own, other, host):
    """What differs between this beanjar (own: `beanjar --version --json`'s object) and host's
    (other: the same, from an older beanjar perhaps only {"name", "version"}), and between host's
    beanjar and its own guest payload: a list of {"host", "package", "found", "expected", "fix"}
    (found and expected {"version", "commit"}), empty when they agree or cannot be compared."""
    found = []
    if not isinstance(other, dict) or not isinstance(other.get("version"), str):
        return found  # nothing to compare with
    theirs = {"version": other.get("version"), "commit": other.get("commit")}
    if isinstance(own, dict) and not same_build(own, theirs):
        source = other.get("source")
        found.append(
            {
                "host": host,
                "package": "beanjar",
                "found": theirs,
                "expected": {"version": own.get("version"), "commit": own.get("commit")},
                "fix": f"on {host}: check out {label(own)} and build it (scripts/regress syncs it)"
                if source == "checkout"
                else package_fix(host, own.get("version")),
            }
        )
    guest = other.get("guest")
    if isinstance(guest, dict) and guest.get("version") not in (None, "unknown"):
        if not same_build(guest, theirs):
            found.append(
                {
                    "host": host,
                    "package": "beanjar-guest",
                    "found": {"version": guest.get("version"), "commit": guest.get("commit")},
                    "expected": theirs,
                    "fix": guest_fix(host, theirs, other.get("source") != "checkout"),
                }
            )
    return found


def drift_text(item):
    """One drift() item as a warning's text."""
    if item["package"] == "beanjar":
        what = (
            f"host {item['host']} runs beanjar {label(item['found'])}, this one is "
            f"{label(item['expected'])}: the command runs {item['host']}'s code"
        )
    else:
        what = (
            f"host {item['host']}'s guest payload is {label(item['found'])}, its beanjar "
            f"{label(item['expected'])}"
        )
    return f"{what}; fix: {item['fix']}"


def components(tree, target, name):
    """Error unless the guest payload has every always-used component of the firmware target."""
    from . import guestlayers, share

    missing = []
    for layer in targets.always_layers(target):
        try:
            if guestlayers.resolve(tree, target, layer):
                continue
        except (ValueError, OSError) as e:
            raise Error(f"Cannot start '{name}': guest component {layer}: {e}") from None
        if share.resolve_layer(tree, target, layer)[0] is None:
            missing.append(layer)
    if missing:
        what = "beanjar-guest" if tree.installed else "the guest build (scripts/build.sh guest)"
        raise Error(
            f"Cannot start '{name}': the guest payload has no {', '.join(missing)} component for "
            f"{targets.name(target)}: install {what} with this firmware target"
        )


# ---------------------------------------------------------------- module parameters
def declared_params(ko):
    """The parameter names a kernel module declares: its .modinfo's parm= and parmtype= entries."""
    from elftools.elf.elffile import ELFFile

    with open(ko, "rb") as f:
        section = ELFFile(f).get_section_by_name(".modinfo")
        data = section.data() if section is not None else b""
    names = set()
    for entry in data.split(b"\0"):
        key, _, value = entry.partition(b"=")
        if key in (b"parm", b"parmtype") and b":" in value:
            names.add(value.split(b":", 1)[0].decode(errors="replace"))
    return names


def _cached_params(tree, ko):
    """declared_params(ko), cached by the file's path, size and mtime (tree's cache)."""
    st = os.stat(ko)
    key = [st.st_size, st.st_mtime_ns]
    path = tree.cache(MODINFO_CACHE)
    try:
        with open(path) as f:
            cache = json.load(f)
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(ko)
    if isinstance(hit, dict) and hit.get("stat") == key and isinstance(hit.get("params"), list):
        return set(hit["params"])
    params = declared_params(ko)
    cache[ko] = {"stat": key, "params": sorted(params)}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(cache, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass  # a cache only
    return params


def skew(tree):
    """When this host's beanjar and tree's guest payload come from different commits: a sentence
    naming both and the fix, for a refusal that skew explains; else ""."""
    host, guest = host_identity(), guest_identity(tree)
    if guest["version"] == "unknown" or same_build(host, guest):
        return ""
    return (
        f"this host's beanjar is {label(host)} and its guest payload {label(guest)}, from different "
        f"commits; fix: {guest_fix(None, host, tree.installed)}"
    )


def module_params(tree, params, name):
    """Error unless each guest module of params ({module: words}, what boot.conf's MODULE_PARAMS_*
    pass, targets.module_params) declares every parameter it is given: the guest's .ko, read where the
    payload has it (tree.build("<module>.ko"))."""
    for module, words in params.items():
        wanted = [w.split("=", 1)[0] for w in words]
        if not wanted:
            continue
        ko = tree.build(f"{module}.ko")
        try:
            declared = _cached_params(tree, ko)
        except FileNotFoundError:
            raise Error(
                f"Cannot start '{name}': the guest payload has no {ko}, whose parameters "
                f"{', '.join(wanted)} this host passes"
            ) from None
        except Exception as e:  # not an ELF file, a broken one
            raise Error(f"Cannot start '{name}': cannot read {ko}'s .modinfo: {e}") from None
        for param in wanted:
            if param not in declared:
                raise Error(
                    f"Cannot start '{name}': {module}.ko has no parameter {param}, which this host "
                    f"passes ({ko}): "
                    + (
                        skew(tree)
                        or "install a guest payload built with it, or a host that does not pass it"
                    )
                )
