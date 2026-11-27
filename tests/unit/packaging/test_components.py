#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Independent components preserve the original overlay and compatibility boundaries."""

import contextlib
import gzip
import importlib.util
import io
import os
from pathlib import Path
import sys
from unittest import mock

import pytest

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar import core, guestlayers, layermeta, paths

spec = importlib.util.spec_from_file_location("component_publish", TOP / "meson/components.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
debian = importlib.util.module_from_spec(spec)
spec.loader.exec_module(debian)
spec = importlib.util.spec_from_file_location("shippable", TOP / "packaging/shippable.py")
shippable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shippable)


def targets_mod():
    from beanjar.firmware import targets

    return targets


class TestComponentsTest:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.top = tmp_path
        self.tree = paths.Tree(self.top)
        self.root = self.top / "build/components"
        self.source = self.top / "gl"
        (self.source / "usr/lib").mkdir(parents=True)
        for name, content in [
            ("libEGL.so.1", "shared"),
            ("libmali.so.0.1", "soc"),
            ("libwayland-server-compat.so.0", "release"),
        ]:
            (self.source / "usr/lib" / name).write_text(content)
        (self.source / "usr/lib/libmali.so").symlink_to("libmali.so.0.1")
        (self.source / "usr/lib/libmali.so.0").symlink_to("libmali.so.0.1")

    def test_split_and_runtime_stack_preserve_files(self):
        publisher.publish(self.root, "k5lp", "goldilocks2", {"gl": self.source}, ["mali"])
        selected = guestlayers.resolve(self.tree, ("k5lp", "goldilocks2"), "gl")
        assert [name for name, _ in selected] == ["gl-release", "gl-soc", "gl"]
        actual = {}
        for _, directory in reversed(selected):
            for file in Path(directory).rglob("*"):
                if file.is_file() or file.is_symlink():
                    actual[str(file.relative_to(directory))] = (
                        ("link", file.readlink())
                        if file.is_symlink()
                        else ("file", file.read_bytes())
                    )
        expected = {
            str(file.relative_to(self.source)): ("link", file.readlink())
            if file.is_symlink()
            else ("file", file.read_bytes())
            for file in self.source.rglob("*")
            if file.is_file() or file.is_symlink()
        }
        assert actual == expected
        assert guestlayers.resolve(self.tree, ("k5lp", "afro"), "gl") == []
        assert not (self.root / "k5lp-goldilocks2").exists()

    def test_identical_features_shared_and_modes_distinguish_variants(self):
        a = publisher.publish(self.root, "k5lp", "goldilocks2", {"rootssh": self.source})[0]
        b = publisher.publish(self.root, "lm21a", "kisscurl", {"rootssh": self.source})[0]
        assert a == b
        (self.source / "usr/lib/libEGL.so.1").chmod(0o755)
        c = publisher.publish(self.root, "k5lp", "goldilocks2", {"rootssh": self.source})[0]
        assert c != a
        assert a.is_dir()  # old running VMs retain their selected payload

    def test_manager_lists_compatible_components_without_checkout_layers(self):
        publisher.publish(
            self.root,
            "k5lp",
            "goldilocks2",
            {"gl": self.source, "soc": self.source, "webos": self.source},
        )
        # The package's layer root contains axis directories and selectors,
        # rather than the source tree scanned by the old manager.
        (self.top / "installation.toml").write_text('version="test"')
        tree = self.tree
        with (
            mock.patch.object(
                tree, "build", side_effect=lambda *parts: str(self.root.joinpath(*parts[1:]))
            ),
            mock.patch.object(
                tree, "src", side_effect=lambda *parts: str(self.root.joinpath(*parts[2:]))
            ),
            mock.patch("beanjar.paths.Tree", return_value=tree),
        ):
            library = core.Library(str(self.top / "library"), repo=str(self.top))
            listed = library.layers(("k5lp", "goldilocks2"))
            assert listed["gl"]["built"]
            assert listed["soc"]["built"]
            assert listed["webos"]["built"]
            assert "release" not in listed
            assert not library.layers(("k5lp", "afro"))["gl"]["built"]

    def test_layer_metadata_published_and_packaged(self):
        """Each layer's layer.toml goes beside its component (build/components/<name>/layer.toml), never
        into a variant (the guest's tree); the package copies it to the path the installed manager
        reads (paths.Tree.src('guest', 'layers', name) when installed)."""
        meta = TOP / "guest/layers/devmode/layer.toml"
        publisher.publish(
            self.root, "k5lp", "goldilocks2", {"devmode": self.source}, metas={"devmode": meta}
        )
        assert (self.root / "devmode/layer.toml").read_bytes() == meta.read_bytes()
        for variant in (self.root / "devmode/variants").iterdir():
            assert not (variant / "layer.toml").exists()
        loaded = layermeta.load(self.tree)
        assert loaded["devmode"].forwards == layermeta.parse("devmode", meta).forwards
        assert loaded["devmode"].versions is None
        out = self.top / "package/layers"
        debian.components(self.root, out)
        assert (out / "devmode/layer.toml").read_bytes() == meta.read_bytes()
        assert (out / "devmode/selections.json").is_file()

    def publish_versions(self):
        """hbchannel at two versions, as guest/layers/hbchannel/meson.build publishes it."""
        old, new = self.top / "hbc-old", self.top / "hbc-new"
        for tree, text in ((old, "0.7.3"), (new, "0.8.0")):
            (tree / "usr/share").mkdir(parents=True)
            (tree / "usr/share/version").write_text(text)
        meta = TOP / "guest/layers/hbchannel/layer.toml"
        for target in (("o22", "queue"), ("o22", "number1")):
            publisher.publish(
                self.root,
                *target,
                {"hbchannel@0.7.3": old, "hbchannel@0.8.0": new},
                metas={"hbchannel@0.7.3": meta, "hbchannel@0.8.0": meta},
                defaults={"hbchannel": "0.8.0"},
            )
        return old, new, meta

    def test_versioned_layer_published_side_by_side(self):
        """NAME@VERSION goes to NAME/versions/VERSION with NAME/versions/default beside them and one
        layer.toml for the layer; the package ships them all and an image of each version."""
        old, new, meta = self.publish_versions()
        assert (self.root / "hbchannel/layer.toml").read_bytes() == meta.read_bytes()
        assert (self.root / "hbchannel/versions/default").read_text() == "0.8.0\n"
        assert not (self.root / "hbchannel/selections.json").exists()
        assert not (self.root / "hbchannel@0.7.3").exists()
        for version, tree in (("0.7.3", old), ("0.8.0", new)):
            selected = guestlayers.select(
                self.root, ("o22", "queue"), guestlayers.versioned("hbchannel", version)
            )
            assert Path(selected).name == publisher.digest(tree)
        out = self.top / "package/guest"
        debian.components(self.root, out / "layers")
        assert (out / "layers/hbchannel/versions/default").read_text() == "0.8.0\n"
        for version in ("0.7.3", "0.8.0"):
            assert (out / f"layers/hbchannel/versions/{version}/selections.json").is_file()
        from beanjar import layerimage

        with mock.patch.object(layerimage, "build") as build:
            (out / "layers/normal").mkdir()
            debian.layer_images(out)
        built = {Path(c.args[0]).name for c in build.call_args_list}
        assert {publisher.digest(old), publisher.digest(new)} <= built

    def test_versioned_layer_resolved_per_vm(self):
        """The manager stacks the version a selection names, the default for plain NAME, under the
        layer's own name (the guest's disk serial); an unknown version is refused, naming them."""
        from beanjar import share

        old, new, _meta = self.publish_versions()
        loaded = layermeta.load(self.tree)
        assert loaded["hbchannel"].versions == layermeta.Versions("0.8.0", ("0.7.3", "0.8.0"))
        target = ("o22", "queue")
        with mock.patch.object(targets_mod(), "always_layers", return_value=()):
            for entry, tree in (("hbchannel@0.7.3", old), ("hbchannel", new)):
                (layer,), missing = share.resolve_layers(self.tree, target, [entry])
                assert (layer.name, Path(layer.src).name) == ("hbchannel", publisher.digest(tree))
                assert missing == []
            with pytest.raises(share.Error) as e:
                share.resolve_layers(self.tree, target, ["hbchannel@0.9.0"])
            assert "no version '0.9.0'" in str(e.value)
            assert "0.7.3, 0.8.0 (plain hbchannel: 0.8.0)" in str(e.value)
            _layers, missing = share.resolve_layers(
                self.tree, ("m14", "beehive"), ["hbchannel@0.7.3"]
            )
            assert missing == ["hbchannel@0.7.3 (not built for m14-beehive)"]
        assert layermeta.versions_of(["normal", "hbchannel"], loaded) == {"hbchannel": "0.8.0"}
        assert layermeta.versions_of(["hbchannel@0.7.3"], loaded) == {"hbchannel": "0.7.3"}

    def test_shippable_check_scans_what_the_package_ships(self):
        """check-shippable scans the variants the packager copies: a stale variant no target selects
        any more is neither shipped nor scanned; a selected one with a private key fails."""
        clean, keyed = self.top / "clean", self.top / "keyed"
        for tree, text in (
            (clean, "clean"),
            (keyed, "-----BEGIN " + "PRIVATE KEY-----\n" + "A" * 64),
        ):
            (tree / "usr/lib").mkdir(parents=True)
            (tree / "usr/lib/libkadaptor.so").write_text(text)
        cpio = b"070701" + b"0" * 8 * 11 + b"0000000B" + b"0" * 8 + b"TRAILER!!!\0"
        self.root.mkdir(parents=True)
        with gzip.open(self.top / "build/initramfs.cpio.gz", "wb") as f:
            f.write(cpio + b"\0" * (-len(cpio) % 4))
        publisher.publish(self.root, "lm21a", "afro", {"soc": keyed})
        stale = publisher.digest(keyed)
        publisher.publish(self.root, "lm21a", "afro", {"soc": clean})  # the target moved on
        assert (self.root / "soc/lm21a/variants" / stale).is_dir()

        def check():
            argv = ["check-shippable", str(self.top / "build")]
            with (
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                return shippable.main(), out.getvalue()

        assert check() == (0, mock.ANY)
        out = self.top / "package/layers"
        debian.components(self.root, out)
        assert not (out / "soc/lm21a/variants" / stale).exists()
        publisher.publish(self.root, "lm21a", "beehive", {"soc": keyed})
        status, text = check()
        assert status == 1
        assert f"soc/lm21a/variants/{stale}/usr/lib/libkadaptor.so: a PEM private key" in text

    def test_invalid_or_missing_variant_refused(self):
        publisher.publish(self.root, "k5lp", "goldilocks2", {"gl": self.source}, ["mali"])
        selection = self.root / "gl/selections.json"
        selection.write_text('{"k5lp-goldilocks2":"../../escape"}')
        with pytest.raises(ValueError, match="invalid component variant"):
            guestlayers.resolve(self.tree, ("k5lp", "goldilocks2"), "gl")
        selection.write_text('{"k5lp-goldilocks2":"' + "0" * 64 + '"}')
        with pytest.raises(ValueError, match="missing component"):
            guestlayers.resolve(self.tree, ("k5lp", "goldilocks2"), "gl")


def test_copy_keeps_systemd_masks_and_refuses_external_links(tmp_path):
    src = tmp_path / "src"
    (src / "etc/systemd/system").mkdir(parents=True)
    (src / "etc/systemd/system/dropbear.service").symlink_to("/dev/null")
    debian.copy(src, tmp_path / "out")
    assert os.readlink(tmp_path / "out/etc/systemd/system/dropbear.service") == "/dev/null"
    (src / "escape").symlink_to("/etc/passwd")
    with pytest.raises(SystemExit, match="external symlink"):
        debian.copy(src, tmp_path / "out2")
