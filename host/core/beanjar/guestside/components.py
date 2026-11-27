# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Independent guest components, selected by firmware compatibility at VM start."""

import json
from pathlib import Path


def component(target, name):
    if name == "soc":
        return f"soc/{target[0]}"
    if name == "webos":
        return f"release/{target[1]}"
    if name in ("netcast45", "netcast4", "simplesmart", "testguest"):
        return f"release/{target[1]}"
    return name


def versioned(name, version):
    """The component of a version of a layer built once per version (meson/components.py)."""
    return f"{name}/versions/{version}"


def select(root, target, component):
    directory = Path(root) / component
    path = directory / "selections.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    variant = data.get("-".join(target))
    if not variant:
        return None
    if (
        not isinstance(variant, str)
        or len(variant) != 64
        or any(c not in "0123456789abcdef" for c in variant)
    ):
        raise ValueError(f"{path}: invalid component variant")
    variant_dir = directory / "variants" / variant
    if not variant_dir.is_dir():
        raise ValueError(f"{path}: missing component {variant}")
    return str(variant_dir)


def resolve(tree, target, name):
    root = tree.build("components")
    base = select(root, target, component(target, name))
    if base is None:
        return []
    if name != "gl":
        return [(name, base)]
    out = []
    # Higher-priority feature adjustments precede the generic GL payload.
    for alias, relative in (
        ("gl-release", f"release/{target[1]}/gl"),
        ("gl-soc", f"soc/{target[0]}/gl"),
    ):
        adjustment = select(root, target, relative)
        if adjustment:
            out.append((alias, adjustment))
    return out + [("gl", base)]
