# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The guest layers' own metadata, guest/layers/<name>/layer.toml (docs/dev/architecture/firmware-targets.md,
"Layer metadata"), and the one check of a LAYERS selection against it.

  description = "..."                          the dialogs' line for the layer
  requires = ["devmode"]                       layers that must be in the same selection
  conflicts = ["rootssh"]                      layers that must not be; one side declaring it is enough
  forwards = [{ guest = 22, host = 6622 }]     guest TCP ports the launcher forwards to the VM's
                                               address, or LISTEN_ADDRESS (forwards(); SSH_PORT
                                               replaces host 6622); a VM's own forward= setting
                                               adds ports of its own (parse_forward)

The checkout reads the source files: guest/layers/<name>/, and SOURCES for a layer whose source is
elsewhere. The guest build also publishes each built layer's file as
build/components/<name>/layer.toml, which is where a package has it (packaging/debian/build.py).

A layer built once per version (guest/layers/hbchannel, one per Homebrew Channel release) is named
NAME@VERSION in a selection; plain NAME is its default version. Its directory holds versions/default
(the default's name) and versions/<version>/ for each version: the source tree's
guest/layers/<name>/versions/, the published build/components/<name>/versions/<version>/ (a
component, meson/components.py). Layer.versions lists them (Versions); the rules (requires,
conflicts, forwards) are the layer's whatever its version."""

import os
import re
import tomllib
from typing import NamedTuple

from . import paths

FILE = "layer.toml"
KEYS = {"description", "requires", "conflicts", "forwards"}
# name -> the checkout directory of the layer.toml of a layer not under guest/layers; the same as
# the meson layer_meta entries (tests/regress/tcg/meson.build)
SOURCES = {"tcg-perf": ("tests", "regress", "tcg")}


VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]*")  # NAME@VERSION's VERSION
DEFAULT = "default"  # versions/default: the version plain NAME means


class Error(ValueError):
    pass


class Versions(NamedTuple):
    """A versioned layer's versions: the one plain NAME means, and all, sorted."""

    default: str
    available: tuple


class Layer:
    def __init__(self, name, description="", requires=(), conflicts=(), forwards=(), versions=None):
        self.name, self.description = name, description
        self.requires, self.conflicts, self.forwards = (
            tuple(requires),
            tuple(conflicts),
            tuple(forwards),
        )
        self.versions = versions  # Versions, None for a layer built once

    def __repr__(self):
        return f"Layer({self.name!r})"


_parsed = {}  # (name, path) -> (its stat signature, its Layer)


def parse(name, path):
    """The Layer that path (a layer.toml) declares; Error naming the file if it is malformed. Kept
    until the file changes: every vm.toml read checks its layers (the window reads them each second)."""
    try:
        st = os.stat(path)
        sig = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    except OSError:
        sig = None
    hit = _parsed.get((name, path))
    if sig is not None and hit and hit[0] == sig:
        return hit[1]
    layer = _parse(name, path)
    if sig is not None:
        _parsed[(name, path)] = (sig, layer)
    return layer


def _parse(name, path):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise Error(f"{path}: {e}") from None
    extra = set(data) - KEYS
    if extra:
        raise Error(
            f"{path}: unknown key {', '.join(sorted(extra))} (known: {', '.join(sorted(KEYS))})"
        )
    description = data.get("description", "")
    if not isinstance(description, str):
        raise Error(f"{path}: description is a string")
    for key in ("requires", "conflicts"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(n, str) and n for n in value):
            raise Error(f"{path}: {key} is a list of layer names")
        if name in value:
            raise Error(f"{path}: {key} names the layer itself")
    forwards = []
    if not isinstance(data.get("forwards", []), list):
        raise Error(f"{path}: forwards is a list of {{ guest = PORT, host = PORT }}")
    for fwd in data.get("forwards", []):
        if (
            not isinstance(fwd, dict)
            or set(fwd) != {"guest", "host"}
            or not all(
                isinstance(fwd[k], int) and not isinstance(fwd[k], bool) and 0 < fwd[k] < 65536
                for k in ("guest", "host")
            )
        ):
            raise Error(f"{path}: forwards is a list of {{ guest = PORT, host = PORT }}")
        forwards.append((fwd["guest"], fwd["host"]))
    return Layer(name, description, data.get("requires", []), data.get("conflicts", []), forwards)


def split(entry):
    """(name, version) of a selection's entry NAME or NAME@VERSION; version None for plain NAME."""
    name, at, version = entry.partition("@")
    return name, (version if at else None)


def base(entry):
    """The layer's name of a selection's entry (NAME of NAME@VERSION)."""
    return entry.partition("@")[0]


def bases(entries):
    """The layers' names of a selection's entries, in order, empty ones left out."""
    return [base(n) for n in entries or () if n]


def _versions(dirs):
    """Versions from the versions/ directories dirs (the published first): every version
    directory of any of them; the default the first versions/default names. None without one."""
    default, available = None, set()
    for d in dirs:
        try:
            names = os.listdir(d)
        except (FileNotFoundError, NotADirectoryError):
            continue
        if default is None:
            try:
                with open(os.path.join(d, DEFAULT)) as f:
                    default = f.read().strip() or None
            except FileNotFoundError:
                pass
        available.update(
            n for n in names if VERSION.fullmatch(n) and os.path.isdir(os.path.join(d, n))
        )
    if default is None:
        return None
    return Versions(default, tuple(sorted(available | {default})))


def version_text(layer):
    """'0.7.3, 0.8.0 (plain NAME: 0.8.0)': a versioned layer's versions, for messages."""
    v = layer.versions
    return f"{', '.join(v.available)} (plain {layer.name}: {v.default})"


def resolve(entry, meta=None):
    """The version a selection's entry means (NAME@VERSION's, plain NAME's default), None for a
    layer built once or one without metadata."""
    meta = load() if meta is None else meta
    name, version = split(entry)
    layer = meta.get(name)
    if layer is None or layer.versions is None:
        return None
    return version or layer.versions.default


def versions_of(entries, meta=None):
    """{name: version} of the versioned layers among a selection's entries: what each runs."""
    meta = load() if meta is None else meta
    out = {}
    for n in entries or ():
        version = resolve(n, meta) if n else None
        if version is not None:
            out.setdefault(base(n), version)
    return out


def load(tree=None):
    """{name: Layer} of every layer with a layer.toml in tree (default: the manager's own,
    paths.TREE): the source guest/layers/<name>/ or SOURCES, else the published
    build/components/<name>/.
    Error if a file is malformed. A name in requires or conflicts need not be there (a package
    holds the layers its builds made; tests/unit/manager/test_layermeta.py holds the sources to theirs)."""
    tree = tree or paths.TREE
    out = {}
    for name, d in SOURCES.items():
        path = tree.src(*d, FILE)
        if os.path.isfile(path):
            out[name] = parse(name, path)
    for root in (tree.src("guest", "layers"), tree.build("components")):
        try:
            names = sorted(os.listdir(root))
        except FileNotFoundError:
            continue
        for name in names:
            path = os.path.join(root, name, FILE)
            if name not in out and os.path.isfile(path):
                out[name] = parse(name, path)
    for name, layer in out.items():
        found = _versions(
            [
                tree.build("components", name, "versions"),
                tree.src("guest", "layers", name, "versions"),
            ]
        )
        if found != layer.versions:
            # parse's cached Layer is shared: a copy carries this tree's versions
            layer = out[name] = Layer(
                name, layer.description, layer.requires, layer.conflicts, layer.forwards, found
            )
    return out


ROOTSSH_PORT = 6622  # root SSH's host port, which SSH_PORT replaces (keys.ROOTSSH_PORT)


# The host ports beanjar itself takes on a VM's address (and, with link lan, its LAN address), which
# a forward= port may not be: netns.VNC_PORT and the inspector's default NETSTUB_WEB_PORT.
RESERVED = {5900: "VNC's (beanjar vnc)", 18083: "the traffic inspector's"}
FORWARD = "forward"  # the name forwards() gives a forward= port, in place of a layer's
UNPRIVILEGED = 1024  # the lowest host port a user may bind


def parse_forward(text):
    """vm.toml's forward (the launcher's FORWARD): "8080,3000:80", guest TCP ports relayed on the
    VM's address, each PORT (the same port on the host) or HOST:GUEST; [(host, guest)], in order.
    A host port below 1024 (privileged on the host) or one given twice is refused."""
    out = []
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        try:
            if len(parts) > 2:
                raise ValueError
            host, guest = (int(x) for x in (parts * 2)[:2])
        except ValueError:
            raise Error(f"forward {item!r}: PORT or HOST:GUEST, e.g. 8080 or 8081:80") from None
        for port in (host, guest):
            if not 1 <= port <= 65535:
                raise Error(f"forward {item!r}: a port is 1 to 65535")
        if host < UNPRIVILEGED:
            raise Error(
                f"forward {item!r}: host port {host} is privileged on the host; "
                f"give it a host port of {UNPRIVILEGED} or more, e.g. {host + 8000}:{guest}"
            )
        if any(h == host for h, _g in out):
            raise Error(f"forward: host port {host} is given twice")
        out.append((host, guest))
    return out


def forward_text(pairs):
    """forward's canonical text: "8080,3000:80" (PORT where host and guest are the same)."""
    return ",".join(str(h) if h == g else f"{h}:{g}" for h, g in pairs)


def forwards(names, meta, ssh_port=None, extra=()):
    """[(host, guest, layer)]: the TV-service ports a VM with the layers names (LAYERS and its target's
    always-used layers) has on the host, from their layer.toml's forwards: layers in the order of
    their lowest guest port, host 6622 replaced by ssh_port; then extra, the VM's own forward=
    (parse_forward's pairs), as layer FORWARD. The one list of them: qemu.plan forwards these on
    LISTEN_ADDRESS and core.VM.listeners checks them at a start; a new service port joins with its
    layer's forwards (SSAP's 3000 and 3001 are the webos layer's). An extra host port that a layer
    already has, or that beanjar takes itself (RESERVED), is refused."""
    out = []
    chosen = [n for n in dict.fromkeys(bases(names)) if n in meta and meta[n].forwards]
    for name in sorted(chosen, key=lambda n: min(g for g, _h in meta[n].forwards)):
        for guest, host in meta[name].forwards:
            if host == ROOTSSH_PORT and ssh_port:
                host = int(ssh_port)
            out.append((host, guest, name))
    taken = {h: f"the {n} layer's (guest {g})" for h, g, n in out}
    for host, guest in extra:
        why = taken.get(host) or RESERVED.get(host)
        if why:
            raise Error(
                f"forward {host}: host port {host} is already {why}; pick another host port"
            )
        taken[host] = f"forward's (guest {guest})"
        out.append((host, guest, FORWARD))
    return out


def root_layers(meta=None):
    """The layers whose layer.toml forwards the guest's 22 (root SSH)."""
    meta = load() if meta is None else meta
    return sorted(n for n, layer in meta.items() if any(g == 22 for g, _h in layer.forwards))


def root_port(layers, ssh_port=None, meta=None):
    """The host port of root SSH of a VM with layers (SSH_PORT ssh_port), as text, or None without
    a layer that forwards the guest's 22 (the same test as `info`'s ssh.root, commands.ssh_commands,
    and ares-TOOL --root's)."""
    if not set(bases(layers)) & set(root_layers(meta)):
        return None
    return str(ssh_port or ROOTSSH_PORT)


def conflicts(meta, name):
    """The layers name conflicts with, declared on either side."""
    own = meta[name].conflicts if name in meta else ()
    return tuple(sorted(set(own) | {n for n, layer in meta.items() if name in layer.conflicts}))


def check(names, meta=None, unknown_ok=False):
    """Error if the selection names (LAYERS, any order; empty names ignored; NAME or NAME@VERSION)
    is not one the layers allow: a name without metadata or a version the layer does not have
    (unless unknown_ok, for a saved VM that is read back), a version of a layer built once, one
    layer at two versions, a required layer left out, or two layers that conflict."""
    meta = load() if meta is None else meta
    entries = [n for n in names if n]
    seen = {}
    for n in entries:
        name, version = split(n)
        if name in seen and seen[name] != n:
            raise Error(f"layer {name} is named twice, as {seen[name]} and {n}: keep one")
        seen[name] = n
        if name not in meta:
            if not unknown_ok:
                raise Error(f"unknown layer {n} (no {FILE}; layers: {', '.join(sorted(meta))})")
            continue
        if version is None:
            continue
        if meta[name].versions is None:
            raise Error(f"layer {name} has no versions: name it without @{version}")
        if not VERSION.fullmatch(version) or (
            version not in meta[name].versions.available and not unknown_ok
        ):
            raise Error(
                f"layer {name} has no version {version!r}; its versions: {version_text(meta[name])}"
            )
    names = [base(n) for n in entries]
    chosen = set(names)
    for n in names:
        if n not in meta:
            continue
        missing = [r for r in meta[n].requires if r not in chosen]
        if missing:
            raise Error(
                f"layer {n} requires {' and '.join(missing)}: add {' and '.join(missing)} to the layers"
            )
        for other in meta[n].conflicts:
            if other in chosen:
                raise Error(f"layer {n} conflicts with {other}: keep one")
    return names
