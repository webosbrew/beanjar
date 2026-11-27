# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The guest's runtime tree (share.py) and the VM information it shows (vm-info.toml)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from beanjar import paths
from beanjar.firmware import probe, targets
from beanjar.guestside import share
from launchkit import H13_AFRO, TOP, VM, boot_conf


class VmInfoTest(unittest.TestCase):
    """The viewer's window title: what the launcher writes (probe.vm_info) and the viewer builds from it."""

    def test_title(self):
        sys.path.insert(0, os.path.join(TOP, "host", "ui"))
        from beanjar_ui.viewer import main as viewer

        with tempfile.TemporaryDirectory() as d:
            info = probe.vm_info(("k7lp", "kisscurl"), d, "03.34.95.01-HE_DTV_W21P_AFADABAA")
            self.assertEqual(
                info,
                {
                    "platform": "webOS",
                    "version": "6",
                    "board": "W21P",
                    "soc": "k7lp",
                    "release": "kisscurl",
                    "firmware": "03.34.95.01",
                },
            )
            self.assertEqual(
                probe.vm_info(("h13", "afro"), d, "firmware"),
                {"platform": "webOS", "version": "1", "soc": "h13", "release": "afro"},
            )
            with open(
                os.path.join(d, "manifest.toml"), "w"
            ) as f:  # a manager VM's firmware/ names itself
                f.write('name = "05.06.10.01-HE_DTV_WT1H_AFAAABAA"\n')
            self.assertEqual(probe.vm_info(("h13", "afro"), d, "firmware")["board"], "WT1H")
            probe.write_vm_info(d, info)
            got = viewer.vm_info(d)
            self.assertEqual(got, info)
            self.assertEqual(
                viewer.titles("tv1", got),
                (
                    "webOS 6",
                    "tv1 · W21P 03.34.95.01",
                    "tv1 - webOS 6 (kisscurl) - W21P k7lp 03.34.95.01 - Beanjar Viewer",
                ),
            )
            self.assertEqual(
                viewer.titles(
                    None,
                    probe.vm_info(
                        ("k5lp", "goldilocks2"),
                        os.path.join(d, "none"),
                        "04.10.20.01-HE_DTV_W19P_AFADABAA",
                    ),
                ),
                (
                    "webOS 4.5",
                    "W19P 04.10.20.01",
                    "webOS 4.5 (goldilocks2) - W19P k5lp 04.10.20.01 - Beanjar Viewer",
                ),
            )
            # a platform whose version is not known: its name alone
            self.assertEqual(
                viewer.titles("tv2", {"platform": "SimpleSmart", "board": "LB65"})[:2],
                ("SimpleSmart", "tv2 · LB65"),
            )
            self.assertEqual(viewer.vm_info(os.path.join(d, "none")), {})
            self.assertEqual(viewer.titles(None, {}), ("Beanjar Viewer", None, "Beanjar Viewer"))
            self.assertEqual(viewer.titles("tv1", {}), ("tv1", None, "tv1 - Beanjar Viewer"))
            run = os.path.join(d, "vm", "run")
            os.makedirs(run)
            self.assertIsNone(viewer.vm_name(run))
            open(os.path.join(d, "vm", "vm.toml"), "w").close()
            self.assertEqual(viewer.vm_name(run), "vm")

    def test_platform(self):
        """Every target has a platform (LG's names) and the version it is shown with."""
        for target in targets.all_targets():
            self.assertIn(targets.platform(target), targets.PLATFORMS)
        self.assertEqual(targets.platform_title(("k5lp", "goldilocks2")), "webOS 4.5")
        self.assertEqual(targets.platform_title(("m16p", "dreadlocks2")), "webOS 3.5")


class ShareTest(unittest.TestCase):
    """The guest's runtime tree (share.py): RUN_DIR/share/host has the resolved layers, binds.conf's
    sources, the base scripts and what put adds, as hard links, and nothing else of the checkout;
    put refuses what is not allowed; finished guest logs are moved out of share/out."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-share-")
        self.top = os.path.join(self.tmp, "top")
        files = {
            "guest/scripts/save-logs.sh": "s",
            "guest/scripts/check-m1.sh": "c",
            "guest/scripts/time-boot.sh": "t",
            "host/scripts/other.sh": "p",
            "tests/regress/boot/guest.sh": "g",
            "local/model-secret": "secret",
            ".git/config": "git",
            "build/state/default/db8.img": "db8",
            "build/tcg-perf/io.ext4": "fw",
            "build/tcg-perf/perf-arm": "elf",
            "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline": "b",
            "guest/layers/soc/h13/meson.build": "",
            "guest/layers/webos/afro/rootfs/etc/w": "w",
            "guest/layers/normal/etc/n": "n",
            "guest/layers/normal/layer.toml": 'description = "n"\n',
            "guest/layers/common/meson.build": "",
            "guest/layers/common/src.c": "src",
            "build/layers/h13-afro/soc/usr/lib/libhal.so": "hal",
            "guest/layers/gl/meson.build": "",
            "guest/layers/rootssh/meson.build": "",
            "build/layers/h13-afro/rootssh/usr/sbin/dropbear": "db",
            "build/layers/h13-afro/common/usr/lib/c.so": "c",
            "build/layers/h13-afro/webos/etc/w": "w",
            "build/layers/m14-beehive/gl/usr/lib/g.so": "g",
            "guest/layers/binds.conf": "# comment\n\nbuild/layers/h13-afro/soc/usr/lib/libhal.so /usr/lib/libhal.so\n"
            "local/model-secret /etc/x\n",
        }
        with open(os.path.join(TOP, "guest/scripts/save-device-counters.sh")) as f:
            files["guest/scripts/save-device-counters.sh"] = f.read()
        for rel, data in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.top, rel)), exist_ok=True)
            with open(os.path.join(self.top, rel), "w") as f:
                f.write(data)
        os.symlink(
            "libhal.so", os.path.join(self.top, "build/layers/h13-afro/soc/usr/lib/libhal.so.1")
        )
        os.symlink("../local/model-secret", os.path.join(self.top, "tests/evil"))
        os.chmod(os.path.join(self.top, "build/layers/h13-afro/rootssh/usr/sbin"), 0o700)
        self.tree = paths.Tree(self.top)
        self.run_dir = os.path.join(self.tmp, "run")
        self.notes = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def listing(self, d):
        return sorted(
            os.path.relpath(os.path.join(r, n), d) for r, ds, fs in os.walk(d) for n in ds + fs
        )

    def test_assemble(self):
        _share, _vm, host, out = share.dirs(self.run_dir)
        # an always-used layer that is not built is refused (guest/initramfs/rootfs/init has no copy of its own)
        webos = os.path.join(self.top, "build/layers/h13-afro/webos")
        os.rename(webos, webos + ".x")
        with self.assertRaisesRegex(
            share.Error, r"layer webos of h13-afro: not built \(no build/layers/h13-afro/webos\)"
        ):
            share.resolve_layers(self.tree, H13_AFRO, [])
        os.rename(webos + ".x", webos)
        layers, missing = share.resolve_layers(
            self.tree, H13_AFRO, ["normal", "rootssh", "gl", "vm", "../x", ""]
        )
        # highest first; a source layer's layer.toml is the host's, left out of its image
        self.assertEqual(
            [(la.name, la.rel, la.skip) for la in layers],
            [
                ("normal", "guest/layers/normal", ("layer.toml",)),
                ("rootssh", "build/layers/h13-afro/rootssh", ("layer.toml",)),
                ("webos", "build/layers/h13-afro/webos", ("layer.toml",)),
                ("soc", "build/layers/h13-afro/soc", ("layer.toml",)),
                ("common", "build/layers/h13-afro/common", ("layer.toml",)),
            ],
        )
        self.assertEqual(layers[0].src, os.path.join(self.top, "guest/layers/normal"))
        self.assertEqual(len(missing), 1)
        self.assertRegex(missing[0], r"^gl \(not built")
        # NetCast's and SimpleSmart's guest builds make no optional layer: the note says so
        for target, layer in (
            (("lm14", "netcast45"), "netcast45"),
            (("m2", "simplesmart"), "simplesmart"),
        ):
            self.assertEqual(
                share.resolve_layer(self.tree, target, "gl"),
                (
                    None,
                    f"{'-'.join(target)} has none: its guest build makes only the {layer} layer",
                ),
            )
        share.assemble(self.tree, H13_AFRO, self.run_dir, layers, self.notes.append)
        # the layers are disks (qemu.plan), not files of the share
        self.assertEqual(
            self.listing(host),
            [
                "binds.conf",
                "boot.conf",
                "build",
                "build/layers",
                "build/layers/h13-afro",
                "build/layers/h13-afro/soc",
                "build/layers/h13-afro/soc/usr",
                "build/layers/h13-afro/soc/usr/lib",
                "build/layers/h13-afro/soc/usr/lib/libhal.so",
                "guest",
                "guest/scripts",
                "guest/scripts/check-m1.sh",
                "guest/scripts/save-device-counters.sh",
                "guest/scripts/save-logs.sh",
                "guest/scripts/time-boot.sh",
                "layer-sources",
                "out",
            ],
        )
        with open(os.path.join(host, "layer-sources")) as f:
            self.assertEqual(
                f.read(),
                "normal guest/layers/normal\nrootssh build/layers/h13-afro/rootssh\n"
                "webos build/layers/h13-afro/webos\nsoc build/layers/h13-afro/soc\n"
                "common build/layers/h13-afro/common\n",
            )
        # the boot manifest: the layers that are there, bottom to top, the per-VM layer on top
        conf = boot_conf(os.path.join(host, "boot.conf"))
        self.assertEqual(conf["LAYERS"], "common soc webos rootssh normal vm")
        with open(os.path.join(host, "binds.conf")) as f:
            self.assertEqual(
                f.read(), "build/layers/h13-afro/soc/usr/lib/libhal.so /usr/lib/libhal.so\n"
            )
        self.assertTrue(
            any("binds.conf: local/model-secret: not something" in n for n in self.notes),
            self.notes,
        )
        self.assertTrue(os.path.isdir(out))
        # afresh at each launch; out/ is kept
        with open(os.path.join(out, "result"), "w") as f:
            f.write("r")
        share.put(self.tree, host, ["tests/regress/boot/guest.sh"])
        share.assemble(self.tree, H13_AFRO, self.run_dir, [], self.notes.append)
        self.assertFalse(os.path.exists(os.path.join(host, "tests")))
        self.assertTrue(os.path.isfile(os.path.join(out, "result")))

    def test_userland(self):
        """A USERLAND=qemu-user launch puts its interpreter at /host/qemu-arm and names it in
        boot.conf, and the x86-64 guest kernel's qemu-aarch64 at /host/qemu-aarch64, named in
        USERLAND_AARCH64; a native one has none of them."""
        host = share.dirs(self.run_dir)[2]
        arm = os.path.join(self.tmp, "qemu-arm")
        with open(arm, "wb") as f:
            f.write(b"\x7fELF")
        share.assemble(self.tree, H13_AFRO, self.run_dir, [], self.notes.append, qemu_arm=arm)
        with open(os.path.join(host, "qemu-arm"), "rb") as f:
            self.assertEqual(f.read(), b"\x7fELF")
        conf = boot_conf(os.path.join(host, "boot.conf"), ["USERLAND", "USERLAND_AARCH64"])
        self.assertEqual(conf["USERLAND"], "qemu-user")
        self.assertFalse(os.path.exists(os.path.join(host, "qemu-aarch64")))
        self.assertEqual(conf["USERLAND_AARCH64"], "")
        # the x86-64 guest kernel's: qemu-aarch64 beside it, named in USERLAND_AARCH64
        aarch64 = os.path.join(self.tmp, "qemu-aarch64")
        with open(aarch64, "wb") as f:
            f.write(b"\x7fELF64")
        share.assemble(
            self.tree,
            H13_AFRO,
            self.run_dir,
            [],
            self.notes.append,
            qemu_arm=arm,
            qemu_aarch64=aarch64,
        )
        with open(os.path.join(host, "qemu-aarch64"), "rb") as f:
            self.assertEqual(f.read(), b"\x7fELF64")
        conf = boot_conf(os.path.join(host, "boot.conf"), ["USERLAND", "USERLAND_AARCH64"])
        self.assertEqual(conf["USERLAND"], "qemu-user")
        self.assertEqual(conf["USERLAND_AARCH64"], "qemu-user")
        share.assemble(self.tree, H13_AFRO, self.run_dir, [], self.notes.append)
        self.assertFalse(os.path.exists(os.path.join(host, "qemu-arm")))
        self.assertEqual(boot_conf(os.path.join(host, "boot.conf"), ["USERLAND"])["USERLAND"], "")
        self.assertFalse(os.path.exists(os.path.join(host, "qemu-aarch64")))

    def test_log_counter_helper_in_normal_share(self):
        # Normal assembly must supply save-logs.sh's dependency without vm put.
        share.assemble(self.tree, H13_AFRO, self.run_dir, [], self.notes.append)
        host = share.dirs(self.run_dir)[2]
        helper = os.path.join(host, "guest/scripts/save-device-counters.sh")
        with (
            open(helper) as f,
            open(os.path.join(TOP, "guest/scripts/save-device-counters.sh")) as src,
        ):
            self.assertEqual(f.read(), src.read())
        proc = os.path.join(self.tmp, "empty-proc")
        os.mkdir(proc)
        output = os.path.join(self.tmp, "counters")
        subprocess.run(["sh", helper, output, proc], check=True)
        with open(os.path.join(output, "inventory.tsv")) as f:
            inventory = f.read().splitlines()
        self.assertEqual(len(inventory), 7)
        self.assertTrue(all(line.split("\t")[1] == "absent" for line in inventory))
        self.assertIn("/proc/utopia\tabsent\tutopia.txt", inventory)

    def test_put(self):
        host = share.dirs(self.run_dir)[2]
        with self.assertRaisesRegex(share.Error, "has not been started"):
            share.put(self.tree, host, ["tests/regress/boot/guest.sh"])
        share.assemble(self.tree, H13_AFRO, self.run_dir, [], self.notes.append)
        self.assertEqual(
            share.put(
                self.tree,
                host,
                [
                    "tests/regress/boot/guest.sh",
                    os.path.join(self.top, "build/tcg-perf/perf-arm"),
                    "build/layers/h13-afro/rootssh",
                ],
            ),
            [
                "tests/regress/boot/guest.sh",
                "build/tcg-perf/perf-arm",
                "build/layers/h13-afro/rootssh",
            ],
        )
        self.assertTrue(
            os.path.samefile(
                os.path.join(self.top, "tests/regress/boot/guest.sh"),
                os.path.join(host, "tests/regress/boot/guest.sh"),
            )
        )
        self.assertTrue(
            os.path.isfile(os.path.join(host, "build/layers/h13-afro/rootssh/usr/sbin/dropbear"))
        )
        for bad in (
            "local/model-secret",
            "local",
            ".git/config",
            "build/state/default/db8.img",
            "build/tcg-perf/io.ext4",
            "build",
            "tests/evil",
            "tests/../local/model-secret",
            "/etc/passwd",
            "guest/layers/normal",
            "tests/none",
        ):
            with self.assertRaises(share.Error, msg=bad):
                share.put(self.tree, host, [bad])
        # a put again replaces the link; the old file (a checkout file) is not written into
        guest = os.path.join(self.top, "tests/regress/boot/guest.sh")
        os.rename(guest, guest + ".old")
        with open(guest, "w") as f:
            f.write("new")
        share.put(self.tree, host, ["tests/regress/boot/guest.sh"])
        with (
            open(os.path.join(host, "tests/regress/boot/guest.sh")) as f,
            open(guest + ".old") as old,
        ):
            self.assertEqual((f.read(), old.read()), ("new", "g"))
        secret = "secret"
        for r, _ds, fs in os.walk(share.dirs(self.run_dir)[0]):
            for n in fs:
                if not os.path.islink(os.path.join(r, n)):
                    with open(os.path.join(r, n)) as f:
                        self.assertNotEqual(f.read(), secret, n)

    def test_harvest(self):
        out = share.dirs(self.run_dir)[3]
        saved = os.path.join(self.tmp, "saved")
        for name, done in (("a", True), ("b", False), ("c", True)):
            os.makedirs(os.path.join(out, "logs", name))
            if done:
                open(os.path.join(out, "logs", name, ".done"), "w").close()
        os.makedirs(os.path.join(saved, "c"))
        self.assertEqual(
            share.harvest(self.run_dir, saved),
            [os.path.join(saved, "a"), os.path.join(saved, "c.2")],
        )
        self.assertEqual(os.listdir(os.path.join(out, "logs")), ["b"])
        self.assertEqual(share.harvest(self.run_dir, saved), [])

    def test_scripts_vm_put_and_logs(self):
        """`host/scripts/vm-launcher put` and `logs` on the checkout, for a RUN_DIR."""
        env = dict(os.environ, RUN_DIR=self.run_dir, SAVED_LOG_DIR=os.path.join(self.tmp, "saved"))
        vm = lambda *a: subprocess.run(
            [sys.executable, VM, *a],
            env=env,
            stdout=subprocess.PIPE,  # noqa: E731
            stderr=subprocess.STDOUT,
            text=True,
            cwd=TOP,
        )
        r = vm("put", "tests/regress/boot/guest.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has not been started", r.stdout)
        share.assemble(paths.TREE, H13_AFRO, self.run_dir, [], self.notes.append)
        r = vm("put", "tests/regress/boot/guest.sh")
        self.assertEqual((r.returncode, r.stdout), (0, "/host/tests/regress/boot/guest.sh\n"))
        self.assertEqual(vm("put", "local/model-secret").returncode, 1)
        os.makedirs(os.path.join(self.run_dir, "share/out/logs/x"))
        open(os.path.join(self.run_dir, "share/out/logs/x/.done"), "w").close()
        r = vm("logs")
        self.assertEqual(r.stdout, f"logs saved: {self.tmp}/saved/x\n")
