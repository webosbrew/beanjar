#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Publish independent guest components, deduplicating exact compatible outputs.

A layer built once per version, NAME@VERSION (guest/layers/hbchannel), is published as the
component NAME/versions/VERSION, and --default NAME=VERSION as NAME/versions/default: the version
plain NAME means (host/core/beanjar/guestside/layermeta.py reads both)."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path


def digest(tree):
    out = hashlib.sha256()
    for path in sorted(tree.rglob("*")):
        st = path.lstat()
        out.update(
            json.dumps(
                [str(path.relative_to(tree)), stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode)]
            ).encode()
            + b"\0"
        )
        if path.is_symlink():
            out.update(os.readlink(path).encode() + b"\0")
        elif path.is_file():
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    out.update(block)
        elif not path.is_dir():
            raise ValueError(f"special file in layer: {path}")
    return out.hexdigest()


def shipped(root):
    """What a package ships from the published components at root, relative to it: each
    selections.json, each variant a selection names (a directory, checked against its digest)
    and each layer's layer.toml. Variants no target selects any more are left out."""
    root = Path(root)
    out = []
    for selection in sorted(root.rglob("selections.json")):
        out.append(selection.relative_to(root))
        for variant in sorted(set(json.loads(selection.read_text()).values())):
            if not re.fullmatch(r"[0-9a-f]{64}", variant):
                raise ValueError(f"invalid component variant: {selection}")
            tree = selection.parent / "variants" / variant
            if digest(tree) != variant:
                raise ValueError(f"component digest mismatch: {tree.relative_to(root)}")
            out.append(tree.relative_to(root))
    out += [meta.relative_to(root) for meta in sorted(root.glob("*/layer.toml"))]
    out += [d.relative_to(root) for d in sorted(root.glob("*/versions/default"))]
    return out


def store(root, relative, target, source):
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=True)
    variant = digest(source)
    variant_dir = directory / "variants" / variant
    if not variant_dir.exists():
        variant_dir.parent.mkdir(parents=True, exist_ok=True)
        staged = variant_dir.with_name("." + variant)
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(source, staged, symlinks=True)
        staged.rename(variant_dir)
    selection = directory / "selections.json"
    data = json.loads(selection.read_text()) if selection.exists() else {}
    data[target] = variant
    tmp = selection.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(selection)
    return variant_dir


def copy_files(source, dest, keep):
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if path.is_dir() and not path.is_symlink():
            continue
        if not keep(str(rel)):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target, follow_symlinks=False)


def publish_meta(root, name, source):
    """A layer's layer.toml (host/core/beanjar/guestside/layermeta.py) as <root>/<name>/layer.toml, beside its
    component's selections: the same for every target, so the last target's copy stands."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "layer.toml.tmp"
    shutil.copyfile(source, tmp)
    tmp.replace(directory / "layer.toml")
    return directory / "layer.toml"


def publish_default(root, name, version):
    """<root>/<name>/versions/default: the version plain name means (one for every target)."""
    directory = root / name / "versions"
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "default.tmp"
    tmp.write_text(version + "\n")
    tmp.replace(directory / "default")
    return directory / "default"


def component_path(name, soc, release):
    """Where a layer's component goes under the root: soc/<chip>, release/<release>,
    NAME/versions/VERSION for NAME@VERSION, else its name."""
    if name == "soc":
        return f"soc/{soc}"
    if name in ("webos", "netcast45", "netcast4", "simplesmart", "testguest"):
        return f"release/{release}"
    base, at, version = name.partition("@")
    return f"{base}/versions/{version}" if at else name


def publish(root, soc, release, layers, vendors=(), metas=None, defaults=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = f"{soc}-{release}"
    outputs = []
    with (root / ".publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name, source in (metas or {}).items():
            outputs.append(publish_meta(root, name.partition("@")[0], source))
        for name, version in (defaults or {}).items():
            outputs.append(publish_default(root, name, version))
        for name, source in layers.items():
            relative = component_path(name, soc, release)
            source = Path(source)
            if name != "gl":
                outputs.append(store(root, relative, target, source))
                continue
            # Generated vendor stand-ins are SoC data, server compatibility is release data.
            vendor_paths = {
                f"usr/lib/lib{v}.so{suffix}" for v in vendors for suffix in ("", ".0", ".0.1")
            }
            compat = "usr/lib/libwayland-server-compat.so.0"
            with tempfile.TemporaryDirectory(prefix=".split-", dir=root) as temporary:
                for relative, predicate in (
                    ("gl", lambda p: p not in vendor_paths and p != compat),
                    (f"soc/{soc}/gl", lambda p: p in vendor_paths),
                    (f"release/{release}/gl", lambda p: p == compat),
                ):
                    out = Path(temporary) / relative
                    out.mkdir(parents=True)
                    copy_files(source, out, predicate)
                    outputs.append(store(root, relative, target, out))
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--soc", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--layer", action="append", default=[])
    parser.add_argument("--vendor", action="append", default=[])
    parser.add_argument("--meta", action="append", default=[], help="NAME=PATH of a layer.toml")
    parser.add_argument(
        "--default", action="append", default=[], help="NAME=VERSION of a versioned layer"
    )
    parser.add_argument("--stamp", required=True)
    args = parser.parse_args()
    outputs = publish(
        args.dest,
        args.soc,
        args.release,
        dict(item.split("=", 1) for item in args.layer),
        args.vendor,
        dict(item.split("=", 1) for item in args.meta),
        dict(item.split("=", 1) for item in args.default),
    )
    Path(args.stamp).write_text("\n".join(str(path) for path in outputs) + "\n")


if __name__ == "__main__":
    main()
