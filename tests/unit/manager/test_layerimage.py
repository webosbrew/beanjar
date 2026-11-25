#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Pruning the layer image cache keeps what a VM uses and touches nothing while a VM may run."""

import types
from pathlib import Path
from unittest import mock

import pytest

from beanjar import layerimage, share, vmunits


class Tree:
    def __init__(self, top):
        self.top = top

    def build(self, *p):
        return str(Path(self.top, "build", *p))

    cache = build


def layer(src):
    return share.Layer("x", str(src), "x")


class Pruned:
    def __init__(self, top):
        self.tree = Tree(str(top))
        self.cache = Path(self.tree.cache("layer-images"))
        self.cache.mkdir(parents=True)
        self.keys = [f"{i:x}" * 64 for i in range(1, 4)]
        for k in self.keys:
            (self.cache / f"{k}.sqfs").write_bytes(b"x")
        (self.cache / "notes.txt").write_text("not an image")
        (self.cache / f"{'a' * 64}.sqfs.123.tmp").write_bytes(b"part")

    def left(self):
        return sorted(p.name for p in self.cache.iterdir() if p.name.endswith(".sqfs"))


@pytest.fixture
def pruned(tmp_path):
    return Pruned(tmp_path)


def test_removes_only_unused_images(pruned):
    removed = layerimage.prune(pruned.tree, lambda: {pruned.keys[0]})
    assert [Path(p).name for p in removed] == [f"{k}.sqfs" for k in pruned.keys[1:]]
    assert pruned.left() == [f"{pruned.keys[0]}.sqfs"]
    assert (pruned.cache / "notes.txt").exists()
    assert len(list(pruned.cache.glob("*.tmp"))) == 1


def test_dry_run_removes_nothing(pruned):
    removed = layerimage.prune(pruned.tree, set, dry_run=True)
    assert len(removed) == 3
    assert len(pruned.left()) == 3


def test_busy_prunes_nothing(pruned):
    def busy():
        raise layerimage.Error("a VM is running")

    with pytest.raises(layerimage.Error):
        layerimage.prune(pruned.tree, busy)
    assert len(pruned.left()) == 3


def test_no_cache_is_nothing_to_prune(pruned):
    assert layerimage.prune(Tree(pruned.tree.top + "/none"), set) == []


def vm(name, layers, owner=None):
    return types.SimpleNamespace(
        name=name,
        config={"layers": layers},
        target=lambda: ("chip", "rel"),
        lock_owner=lambda units=None: owner,
    )


def run_referenced(vms, units=None, resolved=None):
    lib = types.SimpleNamespace(vms=lambda: vms)

    def resolve(tree, target, layers):
        return [layer(f"/l/{n}") for n in layers], []

    with (
        mock.patch.object(vmunits, "vm_units", return_value=units or {}),
        mock.patch.object(share, "resolve_layers", side_effect=resolved or resolve),
        mock.patch.object(layerimage, "layer_digest", side_effect=lambda src, skip=(): src),
    ):
        return layerimage.referenced(None, [lib])


def test_every_vm_pins_its_own_versions():
    got = run_referenced([vm("a", ["hbchannel@0.8.0"]), vm("b", ["hbchannel@0.9.0", "gl"])])
    want = {layerimage.key(f"/l/{n}") for n in ("hbchannel@0.8.0", "hbchannel@0.9.0", "gl")}
    assert got == want


def test_unresolvable_vm_uses_nothing():
    def broken(tree, target, layers):
        raise share.Error("not built")

    assert run_referenced([vm("a", ["gl"])], resolved=broken) == set()


def test_running_unit_refuses():
    with pytest.raises(layerimage.Error, match="running"):
        run_referenced([vm("a", ["gl"])], units={"beanjar-vm-x": {}})


def test_starting_vm_refuses():
    with pytest.raises(layerimage.Error, match="being started"):
        run_referenced([vm("a", ["gl"], owner=42)])
