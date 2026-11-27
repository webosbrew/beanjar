# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The runner seam (runner.py): which runner a start chooses, the setting and BEANJAR_RUNNER that
name one, launch.Bg starting with the chosen one and stopping with every one; and the default
accelerator's /dev/kvm check (qemu.default_accel)."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from beanjar import base, hostconfig, launch, qemu, runner


class ChooseTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(hostconfig.set_global_option, "runner", "")

    def choose(self, systemd, **env):
        with mock.patch.object(
            runner.unit,
            "available",
            side_effect=lambda e: systemd and e.get("VM_SYSTEMD", "1") != "0",
        ):
            return runner.choose(env)

    def test_auto(self):
        self.assertEqual(self.choose(True), runner.SYSTEMD)
        self.assertEqual(self.choose(False), runner.SESSION)
        self.assertEqual(self.choose(True, VM_SYSTEMD="0"), runner.SESSION)
        self.assertEqual(self.choose(True, BEANJAR_RUNNER="auto"), runner.SYSTEMD)

    def test_named(self):
        self.assertEqual(self.choose(True, BEANJAR_RUNNER="session"), runner.SESSION)
        self.assertEqual(self.choose(False, BEANJAR_RUNNER="Session"), runner.SESSION)
        # named systemd is checked as such, VM_SYSTEMD=0 or not
        self.assertEqual(
            self.choose(True, BEANJAR_RUNNER="systemd", VM_SYSTEMD="0"), runner.SYSTEMD
        )
        with self.assertRaisesRegex(base.Error, "no usable user systemd"):
            self.choose(False, BEANJAR_RUNNER="systemd")
        with self.assertRaisesRegex(base.Error, "BEANJAR_RUNNER: docker is not a runner"):
            self.choose(True, BEANJAR_RUNNER="docker")

    def test_setting(self):
        hostconfig.set_global_option("runner", "session")
        self.assertEqual(hostconfig.global_config()["runner"], "session")
        self.assertEqual(self.choose(True), runner.SESSION)
        self.assertEqual(self.choose(True, BEANJAR_RUNNER="systemd"), runner.SYSTEMD)
        hostconfig.set_global_option("runner", "auto")  # auto: unset
        self.assertNotIn("runner", hostconfig.global_config())
        self.assertEqual(self.choose(True), runner.SYSTEMD)
        with self.assertRaisesRegex(base.Error, "is not a runner"):
            hostconfig.set_global_option("runner", "podman")


class BgTest(unittest.TestCase):
    """launch.Bg starts with the runner chosen and nothing else; stops with every runner."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test-runner-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {
            "RUN_DIR": f"{self.tmp}/run",
            "LOG_DIR": f"{self.tmp}/logs",
            "UNIT": "beanjar-vm-test-runner",
            "BUILD": "0",
        }

    def start(self, **extra):
        with (
            mock.patch.object(launch.unit, "available", return_value=True),
            mock.patch.object(launch.unit, "start", return_value=True) as start,
            mock.patch.object(launch.unit, "spawn", return_value=os.getpid()) as spawn,
            mock.patch.object(launch.vmunits, "unit_show", return_value={}),
            mock.patch.object(launch.vmunits, "vm_units", return_value={}),
            mock.patch.object(launch.qemu, "vm_mem", return_value=1024),
            mock.patch.object(launch.time, "sleep"),
            mock.patch.object(launch.Bg, "running", return_value=True),
            mock.patch.object(launch, "booted", return_value=True),
        ):
            b = launch.Bg(dict(self.env, **extra))
            b.start(["quiet"])
        return b, start, spawn

    def test_systemd(self):
        b, start, spawn = self.start()
        start.assert_called_once()
        spawn.assert_not_called()
        self.assertEqual(start.call_args.args[2], ["quiet"])
        with open(os.path.join(b.run_dir, "unit")) as f:
            self.assertEqual(f.read(), "beanjar-vm-test-runner\n")
        self.assertFalse(os.path.exists(os.path.join(b.run_dir, "launcher.pid")))

    def test_session_named(self):
        b, start, spawn = self.start(BEANJAR_RUNNER="session")
        start.assert_not_called()
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[1], ["quiet"])
        with open(os.path.join(b.run_dir, "launcher.pid")) as f:
            self.assertEqual(int(f.read()), os.getpid())
        self.assertFalse(os.path.exists(os.path.join(b.run_dir, "unit")))

    def test_named_but_missing(self):
        with (
            mock.patch.object(launch.unit, "available", return_value=False),
            mock.patch.object(launch.unit, "start") as start,
            mock.patch.object(launch.unit, "spawn") as spawn,
            self.assertRaisesRegex(launch.Error, "runner systemd: there is no usable user systemd"),
        ):
            launch.Bg(dict(self.env, BEANJAR_RUNNER="systemd")).start([])
        start.assert_not_called()
        spawn.assert_not_called()

    def test_stop_and_running_ask_every_runner(self):
        b = launch.Bg(dict(self.env))
        with (
            mock.patch.object(runner.Systemd, "stop") as s1,
            mock.patch.object(runner.Session, "stop") as s2,
            mock.patch.object(runner.Systemd, "running", return_value=False),
            mock.patch.object(runner.Session, "running", return_value=True),
        ):
            b.stop()
            self.assertTrue(b.running())
        s1.assert_called_once_with(b)
        s2.assert_called_once_with(b)


class RegressLaneTest(unittest.TestCase):
    """A stop by INSTANCE alone reaches the instance's regress lane, which runs under its own
    run directory and unit (scripts/regress), not the development VM's."""

    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.top)
        self.tree = mock.Mock(top=self.top)
        self.tree.build = lambda *p: os.path.join("build", *p)

    def test_lane_of_an_instance(self):
        os.makedirs(os.path.join(self.top, "build", "instances", "k25lp-ponytail"))
        env = launch.regress_lane_env({"INSTANCE": "k25lp-ponytail"}, self.tree)
        self.assertEqual(env["RUN_DIR"], "build/instances/k25lp-ponytail")
        self.assertEqual(env["LOG_DIR"], "build/instances/k25lp-ponytail")
        self.assertEqual(env["UNIT"], "beanjar-vm-regress-k25lp-ponytail")

    def test_no_lane(self):
        os.makedirs(os.path.join(self.top, "build", "instances", "t1"))
        self.assertIsNone(launch.regress_lane_env({}, self.tree))
        self.assertIsNone(launch.regress_lane_env({"INSTANCE": "default"}, self.tree))
        self.assertIsNone(launch.regress_lane_env({"INSTANCE": "other"}, self.tree))  # no directory
        # an explicit RUN_DIR is the caller's own choice
        self.assertIsNone(launch.regress_lane_env({"INSTANCE": "t1", "RUN_DIR": "x"}, self.tree))


class DefaultAccelTest(unittest.TestCase):
    def accel(self, machine, kvm):
        with tempfile.TemporaryDirectory() as d:
            dev = os.path.join(d, "kvm")
            if kvm is not None:
                open(dev, "w").close()
                os.chmod(dev, kvm)
            with (
                mock.patch.object(qemu.platform, "machine", return_value=machine),
                mock.patch.object(qemu, "KVM_DEVICE", dev),
            ):
                return qemu.default_accel()

    def test_kvm_only_with_a_usable_device(self):
        self.assertEqual(self.accel("aarch64", 0o666), "kvm")
        self.assertEqual(self.accel("aarch64", None), "tcg")  # no /dev/kvm (a container, no KVM)
        self.assertEqual(self.accel("x86_64", 0o666), "tcg")
        if os.getuid() != 0:  # root opens anything
            self.assertEqual(self.accel("aarch64", 0o600 & 0), "tcg")


if __name__ == "__main__":
    unittest.main()
