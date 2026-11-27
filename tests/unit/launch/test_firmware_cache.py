# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The firmware cache and the layer images (cache.py, prepare.py).

FwCacheTest builds a real uncompressed image with squashfs-tools (skipped without them)."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock


from beanjar.firmware import cache as firmware_cache, targets
from beanjar import keys, paths
from beanjar.runtime import qemu
from beanjar.board import lls, nvm, partmap, prepare

from launchkit import fake_partmap_build, fake_tree, FW_NAME, H13_AFRO, net, TRUST_PAK


@unittest.skipUnless(shutil.which("unsquashfs"), "no squashfs-tools")
class SquashfsCatTest(unittest.TestCase):
    """Firmware inspection and LLS file reads through official unsquashfs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-launch-")
        self.fw = os.path.join(self.tmp, "fw")
        os.makedirs(self.fw)
        shutil.copyfile(TRUST_PAK, os.path.join(self.fw, "rootfs.pak"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_squashfs_cat(self):
        sys_ca = paths._squashfs_cat(TRUST_PAK, "etc/ssl/certs/ca-certificates.crt")
        self.assertEqual(sys_ca.count("BEGIN CERTIFICATE"), 1)
        self.assertEqual(paths._squashfs_cat(TRUST_PAK, "/etc/ssl/certs/link.pem"), sys_ca)
        sdp = paths._squashfs_cat(TRUST_PAK, "usr/share/ca-certificates/sdp/sdp-ca.pem")
        self.assertFalse(sdp.endswith("\n"))
        for bad in ("etc/nope", "etc/ssl", "etc/ssl/certs/ca-certificates.crt/x"):
            self.assertIsNone(paths._squashfs_cat(TRUST_PAK, bad), bad)
        names = paths._squashfs_names(TRUST_PAK)
        self.assertIn("/etc/starfish-release", names)
        self.assertIn("/etc/ssl/certs/link.pem", names)
        self.assertEqual(paths._squashfs_names(os.path.join(self.tmp, "missing.pak")), set())

    @unittest.skipUnless(
        shutil.which("mksquashfs") and shutil.which("unsquashfs"), "no squashfs-tools"
    )
    def test_stacked_root(self):
        """LLS reads the pak lower before the root, with binary data and stock symlink behavior."""
        src = os.path.join(self.tmp, "src")
        target = ("o20", "goldilocks2")
        rel = targets.release(target)
        lower = rel.pak_lowers[0]
        lower_src = os.path.join(src, lower.subdir)
        os.makedirs(os.path.join(lower_src, "etc", "ssl", "certs"))
        big = os.urandom(300000)
        with open(os.path.join(lower_src, "etc", "ssl", "certs", "big"), "wb") as f:
            f.write(big)
        with open(os.path.join(lower_src, "etc", "starfish-release"), "wb") as f:
            f.write(b"lower release\n")
        os.symlink("big", os.path.join(lower_src, "etc", "ssl", "certs", "rel"))
        os.symlink("/etc/ssl/certs/../certs/big", os.path.join(lower_src, "etc", "abs"))
        pak = next(p.file for p in rel.paks if p.serial == lower.serial)
        img = os.path.join(self.fw, pak + ".pak")
        subprocess.run(
            ["mksquashfs", src, img, "-comp", "gzip", "-quiet", "-no-progress", "-all-root"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        root = lls.Root(self.fw, target)
        for path in ("/etc/ssl/certs/big", "/etc/ssl/certs/rel"):
            self.assertEqual(root.read(path), big, path)
        self.assertEqual(
            root.read("/etc/starfish-release"), b"lower release\n", "lower takes precedence"
        )
        path = "etc/ssl/certs/ca-certificates.crt"
        expected = paths._squashfs_cat(TRUST_PAK, path).encode()
        self.assertEqual(root.read("/" + path), expected, "falls back to the root pak")
        stock = subprocess.run(
            ["unsquashfs", "-cat", img, lower.subdir + "/etc/abs"], capture_output=True
        )
        self.assertIsNone(root.read("/etc/abs"))
        self.assertNotEqual(stock.returncode, 0)


@unittest.skipUnless(shutil.which("mksquashfs") and shutil.which("unsquashfs"), "no squashfs-tools")
class FwCacheTest(unittest.TestCase):
    def test_build(self):
        tmp = tempfile.mkdtemp(prefix="beanjar-launch-")
        try:
            src = os.path.join(tmp, "src")
            os.makedirs(os.path.join(src, "usr", "lib"))
            with open(os.path.join(src, "usr", "lib", "a.so"), "wb") as f:
                f.write(os.urandom(300000))
            os.setxattr(os.path.join(src, "usr", "lib", "a.so"), "user.beanjar-test", b"preserved")
            os.link(
                os.path.join(src, "usr", "lib", "a.so"), os.path.join(src, "usr", "lib", "b.so")
            )
            os.symlink("a.so", os.path.join(src, "usr", "lib", "c.so"))
            os.symlink("/usr/lib/a.so", os.path.join(src, "usr", "lib", "absolute link.so"))
            os.mkfifo(os.path.join(src, "fifo"))
            fw, cache = os.path.join(tmp, "fw"), os.path.join(tmp, "cache")
            os.makedirs(fw)
            subprocess.run(
                [
                    "mksquashfs",
                    src,
                    os.path.join(fw, "rootfs.pak"),
                    "-comp",
                    "gzip",
                    "-quiet",
                    "-no-progress",
                    "-no-exports",
                    "-root-mode",
                    "1770",
                    "-root-uid",
                    "123",
                    "-root-gid",
                    "456",
                    "-root-time",
                    "1234567890",
                    "-mkfs-time",
                    "1234567891",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            with open(os.devnull, "w") as quiet:
                img = firmware_cache.build(fw, cache, "rootfs", log=quiet)
            self.assertTrue(img.startswith(os.path.join(cache, "rootfs-")) and os.path.isfile(img))
            self.assertEqual(
                firmware_cache.build(fw, cache, "rootfs", log=None), img
            )  # cached: no rebuild
            self.assertEqual(
                sorted(os.listdir(cache)),
                sorted([os.path.basename(img), os.path.basename(img) + ".lock", "rootfs.src"]),
            )
            sb = subprocess.run(["unsquashfs", "-s", img], stdout=subprocess.PIPE, text=True).stdout
            self.assertIn("Data is uncompressed", sb)
            self.assertIn("Filesystem is not exportable via NFS", sb)
            root_line = subprocess.run(
                ["unsquashfs", "-lln", "-full-precision", "-UTC", img],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.splitlines()[0]
            self.assertTrue(root_line.startswith("drwxrwx--T 123/456"), root_line)
            self.assertIn("2009-02-13 23:31:30", root_line)
            self.assertEqual(
                subprocess.run(
                    ["unsquashfs", "-mkfs-time", img], capture_output=True, text=True, check=True
                ).stdout.strip(),
                "1234567891",
            )
            dest = os.path.join(tmp, "extracted")
            subprocess.run(
                ["unsquashfs", "-q", "-n", "-d", dest, img], check=True, stdout=subprocess.DEVNULL
            )
            a, b = (os.path.join(dest, "usr", "lib", name) for name in ("a.so", "b.so"))
            self.assertEqual(os.stat(a).st_ino, os.stat(b).st_ino, "hard links survive repacking")
            self.assertEqual(os.getxattr(a, "user.beanjar-test"), b"preserved")
            with (
                open(a, "rb") as f,
                open(os.path.join(src, "usr", "lib", "a.so"), "rb") as original,
            ):
                self.assertEqual(f.read(), original.read())
            self.assertEqual(
                os.readlink(os.path.join(dest, "usr", "lib", "absolute link.so")), "/usr/lib/a.so"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LayerImageTest(unittest.TestCase):
    """layerimage.py: a layer's read-only squashfs image, root's, named by its content, built once
    into the cache and found there (or among the shipped images) after."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-layerimage-")
        self.top = os.path.join(self.tmp, "top")
        self.src = os.path.join(self.top, "guest", "layers", "normal")
        os.makedirs(os.path.join(self.src, "usr", "sbin"))
        with open(os.path.join(self.src, "usr", "sbin", "tool"), "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(os.path.join(self.src, "usr", "sbin", "tool"), 0o750)
        os.chmod(os.path.join(self.src, "usr", "sbin"), 0o700)
        os.symlink("sbin/tool", os.path.join(self.src, "usr", "tool"))
        with open(os.path.join(self.src, "layer.toml"), "w") as f:
            f.write('description = "n"\n')
        self.tree = paths.Tree(self.top)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_key(self):
        """The key is the content's (not times or owners) and the format's; a published variant's
        name is its digest, so its tree is not read."""
        from beanjar.guestside import layerimage

        k = layerimage.key(layerimage.layer_digest(self.src, ("layer.toml",)))
        os.utime(os.path.join(self.src, "usr", "sbin", "tool"), (1, 1))
        self.assertEqual(k, layerimage.key(layerimage.layer_digest(self.src, ("layer.toml",))))
        with open(os.path.join(self.src, "layer.toml"), "w") as f:
            f.write("skipped\n")
        self.assertEqual(k, layerimage.key(layerimage.layer_digest(self.src, ("layer.toml",))))
        os.chmod(os.path.join(self.src, "usr", "sbin", "tool"), 0o755)
        self.assertNotEqual(k, layerimage.key(layerimage.layer_digest(self.src, ("layer.toml",))))
        variant = os.path.join(self.tmp, "gl", "variants", "ab" * 32)
        os.makedirs(variant)
        with mock.patch.object(layerimage, "digest", side_effect=AssertionError("read")):
            self.assertEqual(layerimage.layer_digest(variant), "ab" * 32)
        self.assertNotEqual(layerimage.key("ab" * 32), "ab" * 32)
        self.assertEqual(layerimage.serial("gl-release"), "layer-gl-release")
        with self.assertRaisesRegex(layerimage.Error, "longer than 20"):
            layerimage.serial("a-very-long-layer")

    def test_installed_dirs(self):
        """Installed: the package's prebuilt images first, then the user's cache."""
        from beanjar.guestside import layerimage

        self.assertEqual(
            layerimage.dirs(self.tree), [os.path.join(self.top, "build", "layer-images")]
        )
        open(os.path.join(self.top, "installation.toml"), "w").close()
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.tmp, "cache")}):
            self.assertEqual(
                layerimage.dirs(self.tree),
                [
                    "/usr/share/beanjar/guest/layer-images",
                    os.path.join(self.tmp, "cache", "beanjar", "layer-images"),
                ],
            )

    @unittest.skipUnless(shutil.which("mksquashfs"), "needs squashfs-tools")
    def test_build_once(self):
        """Built into the cache with root's files and the tree's modes and links, layer.toml left
        out; the second call finds it, a shipped image is taken first."""
        from beanjar.guestside import layerimage

        img = layerimage.image(self.tree, self.src, ("layer.toml",))
        cache = os.path.join(self.top, "build", "layer-images")
        self.assertEqual(os.path.dirname(img), cache)
        # the image and the cache's lock file (layerimage.LOCK, kept: it is flocked, not removed),
        # no partial image left beside them
        self.assertEqual(
            sorted(os.listdir(cache)), sorted([os.path.basename(img), layerimage.LOCK])
        )
        listing = subprocess.run(
            ["unsquashfs", "-lln", img], check=True, stdout=subprocess.PIPE, text=True
        ).stdout
        rows = {
            line.split()[-1] if " -> " not in line else line.split()[-3]: line.split()
            for line in listing.splitlines()
            if line.startswith(("d", "-", "l"))
        }
        self.assertNotIn("squashfs-root/layer.toml", rows)
        self.assertEqual(rows["squashfs-root/usr/sbin/tool"][:2], ["-rwxr-x---", "0/0"])
        self.assertEqual(rows["squashfs-root/usr/sbin"][:2], ["drwx------", "0/0"])
        self.assertEqual(rows["squashfs-root/usr/tool"][0], "lrwxrwxrwx")
        info = subprocess.run(
            ["unsquashfs", "-s", img], check=True, stdout=subprocess.PIPE, text=True
        ).stdout
        self.assertIn("Compression lzo", info)
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("built again")):
            self.assertEqual(layerimage.image(self.tree, self.src, ("layer.toml",)), img)
        shipped = os.path.join(self.tmp, "shipped")
        os.makedirs(shipped)
        shutil.copyfile(img, os.path.join(shipped, os.path.basename(img)))
        with mock.patch.object(layerimage, "dirs", return_value=[shipped, cache]):
            self.assertEqual(
                layerimage.image(self.tree, self.src, ("layer.toml",)),
                os.path.join(shipped, os.path.basename(img)),
            )
        # a dry plan's path: no build
        os.unlink(img)
        self.assertEqual(layerimage.image(self.tree, self.src, ("layer.toml",), make=False), img)
        self.assertFalse(os.path.exists(img))


class CacheLockTest(unittest.TestCase):
    """The shared caches VMs starting at once use (the firmware cache's partition map and
    uncompressed images, the LLS cache, a library's CA) are made once, under the lock beside each,
    however many ask for them together."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-locks-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def together(self, fn, n=4):
        """fn() in n threads at once: their results (an exception raised is the result)."""
        out, start = [None] * n, threading.Barrier(n)

        def run(i):
            start.wait()
            try:
                out[i] = fn()
            except Exception as e:  # noqa: BLE001
                out[i] = e

        threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        return out

    def slow(self, calls, result):
        def made(*_a, **_k):
            calls.append(threading.get_ident())
            time.sleep(0.3)
            return result() if callable(result) else result

        return made

    def test_partition_map(self):
        path, calls = os.path.join(self.tmp, "cache", "partinfo-0123.img"), []
        os.makedirs(os.path.dirname(path))
        with (
            mock.patch.object(partmap, "image_path", return_value=path),
            mock.patch.object(partmap, "table", self.slow(calls, None)),
            mock.patch.object(partmap, "image", return_value=b"map"),
        ):
            got = self.together(lambda: partmap.build("fw", H13_AFRO, os.path.dirname(path)))
        self.assertEqual((got, len(calls)), ([path] * 4, 1))
        self.assertEqual(
            sorted(os.listdir(os.path.dirname(path))),
            ["partinfo-0123.img", "partinfo-0123.img.lock"],
        )

    def test_uncompressed_image(self):
        cache, calls = os.path.join(self.tmp, "cache"), []
        img = os.path.join(cache, "rootfs-0123.img")

        def build(fw, cache, pak, img, log):
            calls.append(pak)
            time.sleep(0.3)
            open(img, "w").close()
            return img

        with (
            mock.patch.object(firmware_cache, "image_path", return_value=img),
            mock.patch.object(firmware_cache, "_build", build),
        ):
            got = self.together(lambda: firmware_cache.build("fw", cache, "rootfs"))
        self.assertEqual((got, calls), ([img] * 4, ["rootfs"]))

    def test_lls(self):
        calls, tree = [], paths.Tree(self.tmp)
        root = mock.Mock()
        root.read.return_value = None  # no nyx LLS, no factory data
        with (
            mock.patch.object(lls, "board_cmdline", return_value="model=X"),
            mock.patch.object(lls, "Root", return_value=root),
            mock.patch.object(lls, "extract", return_value={"/usr/sbin/lls": "0" * 64}),
            mock.patch.object(lls, "_large_pages", return_value=False),
            mock.patch.object(lls, "generate", self.slow(calls, (b"eeprom", "records\n", {}))),
        ):
            got = self.together(
                lambda: lls.cache_dir(
                    "fw", "FWNAME", ("k6lp", "jhericurl"), tree, say=lambda _: None
                )
            )
        self.assertEqual(len(set(got)), 1, got)
        self.assertEqual(len(calls), 1)
        with open(os.path.join(got[0], "eeprom.img"), "rb") as f:
            self.assertEqual(f.read(), b"eeprom")
        self.assertEqual(
            sorted(os.listdir(os.path.dirname(got[0]))),
            sorted([os.path.basename(got[0]), os.path.basename(got[0]) + ".lock"]),
        )

    def test_ca(self):
        ca = os.path.join(self.tmp, "lib", "ca")
        got = self.together(lambda: keys.ensure_ca(ca))
        self.assertEqual(sorted(got), [False, False, False, True], "made once")
        key = subprocess.run(
            ["openssl", "pkey", "-pubout", "-in", os.path.join(ca, "ca.key")],
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        cert = subprocess.run(
            ["openssl", "x509", "-pubkey", "-noout", "-in", os.path.join(ca, "ca.pem")],
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        self.assertEqual(key, cert, "the certificate is the key's")
        self.assertTrue(os.path.isfile(ca + ".lock"))


class PrepareTest(unittest.TestCase):
    """beanjar/board/prepare.py: a VM's board data worked out ahead (at create, after a settings change),
    stale when what it came from changes, and a start's plan that only reads it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-prepare-")
        self.top, self.fw, self.state = fake_tree(os.path.join(self.tmp, "t"))
        self.tree = paths.Tree(self.top)
        self.run = os.path.join(self.tmp, "vm", "run")
        self.env = dict(
            FW=self.fw, STATE=self.state, **net("direct"), SLOT="1", RUN_DIR=self.run, SERIAL="S1"
        )
        self.said = []
        self.calls = []
        build = fake_partmap_build(os.path.join(self.tmp, "map.img"))

        def counted(*a, **k):
            self.calls.append("partmap")
            return build(*a, **k)

        def chip(*a, **k):
            self.calls.append("chip")
            return nvm.Chip(b"\xff" * nvm.SIZE, None, None)

        for patch in (
            mock.patch.object(partmap, "build", counted),
            mock.patch.object(nvm, "new_chip", chip),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, **env):
        return prepare.Job(dict(self.env, **env), self.tree)

    def plan(self):
        return qemu.plan(dict(self.env), [], accel="tcg", tree=self.tree, say=self.said.append)

    def test_made_beside_the_run_directory(self):
        prepared = prepare.make(self.job(), self.said.append)
        d = os.path.join(self.tmp, "vm", "prepared")
        self.assertEqual(prepared.dir, d)
        self.assertEqual(sorted(os.listdir(d)), ["eeprom.img", "partinfo.img", "prepared.json"])
        with open(os.path.join(d, "prepared.json")) as f:
            record = json.load(f)
        self.assertEqual(record["inputs"]["serial"], "S1")
        self.assertIn("rootfs.pak", " ".join(record["files"]))
        self.assertIsNone(prepare.stale(self.job()))
        self.assertEqual(self.calls, ["partmap", "chip"])

    def test_stale(self):
        prepare.make(self.job(), self.said.append)
        self.assertEqual(prepare.stale(self.job(SERIAL="S2")), "made with another serial")
        self.assertEqual(
            prepare.stale(self.job(DEBUG_MODE="release")), "made with another debug_mode"
        )
        cmdline = os.path.join(self.top, "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline")
        with open(cmdline, "a") as f:
            f.write("# changed\n")
        self.assertIn("board.cmdline", prepare.stale(self.job()))
        prepare.make(self.job(), self.said.append)
        with open(os.path.join(self.fw, "fonts.pak"), "a") as f:
            f.write("more")
        self.assertIn("fonts.pak", prepare.stale(self.job()))
        prepare.make(self.job(), self.said.append)
        open(os.path.join(self.fw, "extra.pak"), "w").close()
        self.assertIn("extra.pak", prepare.stale(self.job()))
        prepare.make(self.job(), self.said.append)
        os.unlink(os.path.join(self.tmp, "vm", "prepared", "eeprom.img"))
        self.assertEqual(prepare.stale(self.job()), "missing its eeprom.img")
        shutil.rmtree(os.path.join(self.tmp, "vm", "prepared"))
        self.assertEqual(prepare.stale(self.job()), "missing")

    def test_renamed_vm_stays_current(self):
        """Paths in the VM's directory are kept relative to it (its own firmware/)."""
        vm = os.path.join(self.tmp, "vm")
        shutil.copytree(self.fw, os.path.join(vm, "firmware"))
        with open(os.path.join(vm, "firmware", "manifest.toml"), "w") as f:
            f.write(f'name = "{FW_NAME}"\nchip = "h13"\nrelease = "afro"\n')
        env = {"FW": os.path.join(vm, "firmware")}
        prepare.make(self.job(**env), self.said.append)
        os.rename(vm, os.path.join(self.tmp, "renamed"))
        moved = os.path.join(self.tmp, "renamed")
        job = self.job(FW=os.path.join(moved, "firmware"), RUN_DIR=os.path.join(moved, "run"))
        self.assertIsNone(prepare.stale(job))

    def test_start_reads_and_refreshes_only_when_stale(self):
        """A start (qemu.plan) whose board data is current reads it and works nothing out; a
        missing or stale one is made again first, and the start says so and why."""
        prepare.make(self.job(), self.said.append)
        self.calls.clear()
        p = self.plan()
        self.assertEqual(self.calls, [])
        self.assertEqual(p.partmap, os.path.join(self.tmp, "vm", "prepared", "partinfo.img"))
        self.assertFalse(any("prepared board data" in line for line in self.said))
        qemu.make_disks(p)
        with (
            mock.patch.object(nvm, "default_records", side_effect=AssertionError("read")),
            mock.patch.object(lls, "cache_dir", side_effect=AssertionError("made")),
        ):
            qemu.prepare_nvm(p, self.said.append)
        self.env["SERIAL"] = "S2"
        self.plan()
        self.assertEqual(self.calls, ["partmap", "chip"])
        self.assertIn("prepared board data made with another serial: preparing it again", self.said)
        shutil.rmtree(os.path.join(self.tmp, "vm", "prepared"))
        self.said.clear()
        self.plan()
        self.assertTrue(
            any(x.startswith("prepared board data missing: preparing") for x in self.said)
        )

    def test_failure_leaves_the_old_record(self):
        prepare.make(self.job(), self.said.append)
        with (
            mock.patch.object(nvm, "new_chip", side_effect=nvm.Error("no qemu-arm")),
            self.assertRaisesRegex(nvm.Error, "no qemu-arm"),
        ):
            prepare.make(self.job(SERIAL="S2"), self.said.append)
        self.assertIsNone(prepare.stale(self.job()))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "vm", "prepared.new")))
