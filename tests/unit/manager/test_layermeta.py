#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The guest layers' layer.toml (beanjar/guestside/layermeta.py): the loader, the one check of a LAYERS
selection, and this tree's own files (every guest/layers/<name>/layer.toml)."""

import os
import shutil

import pytest
from beanjar import paths
from beanjar.guestside import layermeta

TOP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class Layers:
    def __init__(self, top):
        self.top = str(top)
        self.tree = paths.Tree(self.top)

    def put(self, name, text, root=("guest", "layers")):
        d = os.path.join(self.top, *root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, layermeta.FILE), "w") as f:
            f.write(text)

    def layers(self):
        self.put("a", 'description = "A"\nforwards = [{ guest = 22, host = 6622 }]\n')
        self.put("b", 'requires = ["a"]\nconflicts = ["c"]\n')
        self.put("c", "")
        self.put("d", 'requires = ["a", "c"]\n')
        return layermeta.load(self.tree)

    def versioned(self):
        """Layer v built once per version: versions/default and versions/<version>/ in the source
        and in the published components (1.0 only published, 2.0 only in the source)."""
        self.layers()
        self.put("v", 'requires = ["a"]\nforwards = [{ guest = 23, host = 6623 }]\n')
        for root, names in (
            (("guest", "layers"), ("1.1", "2.0")),
            (("build", "components"), ("1.0", "1.1")),
        ):
            for n in names:
                os.makedirs(os.path.join(self.top, *root, "v", "versions", n))
        with open(os.path.join(self.top, "guest/layers/v/versions/default"), "w") as f:
            f.write("2.0\n")
        with open(os.path.join(self.top, "build/components/v/versions/default"), "w") as f:
            f.write("1.1\n")
        return layermeta.load(self.tree)


@pytest.fixture
def lm(tmp_path):
    return Layers(tmp_path)


def test_load(lm):
    meta = lm.layers()
    assert sorted(meta) == ["a", "b", "c", "d"]
    assert (meta["a"].description, meta["a"].forwards) == ("A", ((22, 6622),))
    assert (meta["b"].requires, meta["b"].conflicts) == (("a",), ("c",))
    assert (meta["c"].description, meta["c"].requires, meta["c"].forwards) == ("", (), ())
    # conflicts are symmetric: c conflicts with b though only b says so
    assert (layermeta.conflicts(meta, "b"), layermeta.conflicts(meta, "c")) == (("c",), ("b",))


@pytest.mark.parametrize(
    "names",
    [[], [""], ["a"], ["b", "a"], ["a", "b", ""], ["c", "a", "d"]],
    ids=["empty", "blank", "a", "b_a", "a_b_blank", "c_a_d"],
)
def test_valid(lm, names):
    meta = lm.layers()
    assert layermeta.check(names, meta) == [n for n in names if n]


def test_requires_missing(lm):
    meta = lm.layers()
    with pytest.raises(layermeta.Error, match="^layer b requires a: add a to the layers$"):
        layermeta.check(["b"], meta)
    with pytest.raises(layermeta.Error, match="^layer d requires a and c"):
        layermeta.check(["d"], meta)


@pytest.mark.parametrize(
    "names", [["a", "b", "c"], ["c", "a", "b"], ["c", "b", "a"]], ids=["a_b_c", "c_a_b", "c_b_a"]
)
def test_conflict_either_way_round(lm, names):
    meta = lm.layers()
    with pytest.raises(layermeta.Error, match="^layer b conflicts with c: keep one$"):
        layermeta.check(names, meta)


def test_unknown(lm):
    meta = lm.layers()
    with pytest.raises(
        layermeta.Error, match=r"^unknown layer x \(no layer.toml; layers: a, b, c, d\)$"
    ):
        layermeta.check(["a", "x"], meta)
    # a saved VM's layers may name one this tree lacks; the known ones' rules still hold
    assert layermeta.check(["x", "a"], meta, unknown_ok=True) == ["x", "a"]
    with pytest.raises(layermeta.Error, match="requires a"):
        layermeta.check(["x", "b"], meta, unknown_ok=True)


def test_published(lm):
    """A layer without a source file is found where the guest build publishes it
    (build/components/<name>/layer.toml); a source file wins over a published one."""
    lm.put("a", 'description = "source"\n')
    lm.put("a", 'description = "published"\n', ("build", "components"))
    lm.put("t", 'description = "T"\n', ("build", "components"))
    meta = layermeta.load(lm.tree)
    assert (meta["a"].description, meta["t"].description) == ("source", "T")


def test_source_outside_guest_layers(lm):
    """A layer whose source is not under guest/layers (SOURCES: tests/regress/tcg's tcg-perf) is found
    in the checkout before any build, and wins over its published copy."""
    lm.put("tcg", 'description = "source"\n', ("tests", "regress"))
    lm.put("tcg-perf", 'description = "published"\n', ("build", "components"))
    assert layermeta.load(lm.tree)["tcg-perf"].description == "source"


def test_versions(lm):
    """A layer's versions are its source's and its published ones; plain NAME is the published
    default (what the build made), else the source's."""
    meta = lm.versioned()
    assert meta["v"].versions == layermeta.Versions("1.1", ("1.0", "1.1", "2.0"))
    assert meta["a"].versions is None
    assert layermeta.split("v@1.0") == ("v", "1.0")
    assert layermeta.split("v") == ("v", None)
    assert layermeta.versions_of(["a", "v"], meta) == {"v": "1.1"}
    assert layermeta.versions_of(["a", "v@2.0"], meta) == {"v": "2.0"}
    shutil.rmtree(os.path.join(lm.top, "build"))
    assert layermeta.load(lm.tree)["v"].versions.default == "2.0"


def test_versioned_check(lm):
    """NAME@VERSION takes the layer's rules; a version it lacks, a version of a layer built
    once, and one layer at two versions are refused."""
    meta = lm.versioned()
    assert layermeta.check(["v@1.0", "a"], meta) == ["v", "a"]
    assert layermeta.check(["a", "v"], meta) == ["a", "v"]
    with pytest.raises(layermeta.Error, match="^layer v requires a"):
        layermeta.check(["v@2.0"], meta)
    with pytest.raises(
        layermeta.Error,
        match=r"^layer v has no version '3\.0'; its versions: 1\.0, 1\.1, 2\.0 \(plain v: 1\.1\)$",
    ):
        layermeta.check(["a", "v@3.0"], meta)
    with pytest.raises(layermeta.Error, match="^layer a has no versions: name it without @1$"):
        layermeta.check(["a@1"], meta)
    with pytest.raises(layermeta.Error, match="^layer v is named twice, as v@1.0 and v: keep one$"):
        layermeta.check(["a", "v@1.0", "v"], meta)
    # a saved VM may name a version this tree no longer has
    assert layermeta.check(["a", "v@0.9"], meta, unknown_ok=True) == ["a", "v"]
    assert [(h, g, n) for h, g, n in layermeta.forwards(["v@1.0"], meta)] == [(6623, 23, "v")]


@pytest.mark.parametrize(
    "text, why",
    [
        ('description = "x"\nowner = "y"\n', "unknown key owner"),
        ('requires = "a"\n', "requires is a list"),
        ("conflicts = [1]\n", "conflicts is a list"),
        ('requires = ["m"]\n', "requires names the layer itself"),
        ("forwards = [{ guest = 22 }]\n", "forwards is a list"),
        ("forwards = [{ guest = 22, host = 70000 }]\n", "forwards is a list"),
        ("forwards = { guest = 22, host = 6622 }\n", "forwards is a list"),
        ("description = \n", "m/layer.toml"),
    ],
    ids=[
        "unknown_key",
        "requires_string",
        "conflicts_ints",
        "requires_self",
        "forward_no_host",
        "forward_port_range",
        "forwards_table",
        "syntax",
    ],
)
def test_malformed(lm, text, why):
    lm.put("m", text)
    with pytest.raises(layermeta.Error, match=why):
        layermeta.load(lm.tree)


def test_every_layer_parses():
    src = os.path.join(TOP, "guest", "layers")
    dirs = sorted(n for n in os.listdir(src) if os.path.isdir(os.path.join(src, n)))
    meta = layermeta.load(paths.Tree(TOP))
    for n in dirs:
        assert os.path.isfile(os.path.join(src, n, layermeta.FILE)), (
            f"guest/layers/{n} has no layer.toml"
        )
        assert meta[n].description, n
        for other in meta[n].requires + meta[n].conflicts:
            assert other in dirs, f"{n} names {other}, which is not under guest/layers"
    assert meta["tcg-perf"].requires == ()  # tests/regress/tcg's, without a build
    # hbchannel is built once per Homebrew Channel release (guest/layers/hbchannel/versions)
    assert "0.7.3" in meta["hbchannel"].versions.available
    assert meta["hbchannel"].versions.default in meta["hbchannel"].versions.available
    layermeta.check(["normal", "devmode", "hbchannel@0.7.3"], meta)
    layermeta.check(["normal", "headless", "tcg-perf"], meta)  # tests/regress/tcg/run.py's LAYERS


def test_declared_rules():
    meta = layermeta.load(paths.Tree(TOP))
    assert (meta["hbchannel"].requires, layermeta.conflicts(meta, "hbchannel")) == (
        ("devmode",),
        ("rootssh",),
    )
    assert layermeta.conflicts(meta, "rootssh") == ("hbchannel",)
    assert {n: m.forwards for n, m in meta.items() if m.forwards} == {
        "rootssh": ((22, 6622),),
        "hbchannel": ((22, 6622),),
        "devmode": ((9922, 9922), (9991, 9991)),
        "webos": ((3000, 3000), (3001, 3001)),
    }
    with pytest.raises(layermeta.Error, match="layer hbchannel requires devmode"):
        layermeta.check(["normal", "gl", "hbchannel"], meta)
    layermeta.check(["normal", "gl", "hbchannel", "devmode"], meta)
    layermeta.check(
        ["normal", "gl", "headless", "rootssh", "devmode"], meta
    )  # gl and headless stack


def test_forwards():
    """layermeta.forwards: the one list of a VM's TV-service ports, layers by their lowest guest
    port, each once, 6622 replaced by SSH_PORT; SSAP is every webOS VM's (the webos layer)."""
    meta = layermeta.load(paths.Tree(TOP))
    names = ["normal", "devmode", "rootssh", "devmode", "", "webos", "soc", "common"]
    assert layermeta.forwards(names, meta) == [
        (6622, 22, "rootssh"),
        (3000, 3000, "webos"),
        (3001, 3001, "webos"),
        (9922, 9922, "devmode"),
        (9991, 9991, "devmode"),
    ]
    assert layermeta.forwards(["hbchannel", "devmode"], meta, "7722")[0] == (7722, 22, "hbchannel")
    assert layermeta.forwards(["normal", "gl", "simplesmart"], meta) == []


def test_parse_forward():
    """forward=: PORT or HOST:GUEST, comma-separated, in order; spaces and empty items
    ignored; anything else, a host port below 1024 or one given twice refused."""
    assert layermeta.parse_forward(" 8080, 3000 ,8081:80,,") == [
        (8080, 8080),
        (3000, 3000),
        (8081, 80),
    ]
    assert layermeta.parse_forward("") == []
    assert layermeta.forward_text([(8080, 8080), (8081, 80)]) == "8080,8081:80"
    for bad, why in (
        ("http", "PORT or HOST:GUEST"),
        ("1:2:3", "PORT or HOST:GUEST"),
        ("8080:", "PORT or HOST:GUEST"),
        ("70000", "1 to 65535"),
        ("8080:0", "1 to 65535"),
        ("80", "privileged on the host.*8080:80"),
        ("8080,8080:81", "host port 8080 is given twice"),
    ):
        with pytest.raises(layermeta.Error, match=why):
            layermeta.parse_forward(bad)


def test_forwards_extra():
    """A VM's own forward= ports follow its layers' as layer "forward"; a host port a layer has
    (SSH_PORT's too) or beanjar's own (VNC 5900, the inspector 18083) is refused, naming it.
    A guest port a layer forwards may be forwarded again from another host port."""
    meta = layermeta.load(paths.Tree(TOP))
    names = ["devmode", "webos"]
    assert layermeta.forwards(names, meta, None, [(8080, 8080), (2222, 9922)])[-2:] == [
        (8080, 8080, "forward"),
        (2222, 9922, "forward"),
    ]
    for host, why in (
        (3000, "already the webos layer's \\(guest 3000\\)"),
        (9922, "already the devmode layer's"),
        (5900, "VNC"),
        (18083, "inspector"),
    ):
        with pytest.raises(layermeta.Error, match=why):
            layermeta.forwards(names, meta, None, [(host, 80)])
    with pytest.raises(layermeta.Error, match="rootssh layer's"):
        layermeta.forwards(["rootssh"], meta, "7722", [(7722, 80)])
    assert layermeta.forwards(["rootssh"], meta, "7722", [(6622, 80)])[-1][0] == 6622
