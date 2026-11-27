# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The launcher's transient user units: environment, limits, linger, the running-VM maximum."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


from beanjar import base, capacity, core, hostconfig, launch, qemu, unit, vmsettings, vmunits

import testunits


class LingerTest(unittest.TestCase):
    """vmunits.linger: a user who does not linger gets `loginctl enable-linger`, one who does is left
    alone, and BEANJAR_LINGER=0 (the tests) runs nothing."""

    def calls(self, linger, knob="1", rc=0):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[:2])
            out = linger if cmd[1] == "show-user" else ""
            return subprocess.CompletedProcess(
                cmd, rc if cmd[1] == "enable-linger" else 0, out, "denied"
            )

        with (
            mock.patch.dict(os.environ, BEANJAR_LINGER=knob),
            mock.patch.object(core.subprocess, "run", side_effect=run),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            ok = vmunits.linger()
        return ok, calls, err.getvalue()

    def test_enables(self):
        ok, calls, _ = self.calls("no\n")
        self.assertTrue(ok)
        self.assertEqual(calls, [["loginctl", "show-user"], ["loginctl", "enable-linger"]])

    def test_already(self):
        self.assertEqual(self.calls("yes\n")[:2], (True, [["loginctl", "show-user"]]))

    def test_off(self):
        self.assertEqual(self.calls("no\n", knob="0")[:2], (True, []))

    def test_refused(self):
        ok, _, err = self.calls("no\n", rc=1)
        self.assertFalse(ok)
        self.assertIn("denied", err)


class VmEnvTest(unittest.TestCase):
    SOCKS = (
        "CURSOR_SOCK",
        "VIDEO_SOCK",
        "IR_SOCK",
        "MEDIA_SOCK",
        "CAPTURE_SOCK",
        "VIEWER_QMP_SOCK",
        "QMP_SOCK",
    )

    def test_sockets_are_per_vm(self):
        """Every socket the launcher defaults under RUN_DIR follows the VM directory: a caller's value is
        dropped (two VMs started from one environment never share one) and vm.toml cannot set it."""
        with tempfile.TemporaryDirectory() as d:
            core.vmconfig.write(
                os.path.join(d, "vm.toml"), {"platform": "webOS", "fw": "/fw", "link": "off"}
            )
            env = launch.vm_env(d, {k: "/shared/" + k for k in self.SOCKS})
        for k in self.SOCKS:
            with self.subTest(k):
                self.assertNotIn(k, env)
                with self.assertRaisesRegex(base.Error, "set by the VM manager"):
                    vmsettings.launcher_env({k: "/x"})


class InspectorPasswordTest(unittest.TestCase):
    """Bg.start hands BEANJAR_MITM_PASSWORD to netns.py in an owner-only file, never in the unit's
    environment (systemd-run --setenv is argv)."""

    def start(self, **extra):
        d = tempfile.mkdtemp(prefix="test-inspector-pw-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        env = dict(
            RUN_DIR=f"{d}/run", LOG_DIR=f"{d}/logs", UNIT="beanjar-vm-test-pw", BUILD="0", **extra
        )
        argv = []

        def run(cmd, **kw):
            if cmd[0] == "systemd-run":
                argv.extend(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        with (
            mock.patch.object(launch.unit, "available", return_value=True),
            mock.patch.object(launch.unit.subprocess, "run", side_effect=run),
            mock.patch.object(launch.vmunits, "unit_show", return_value={}),
            mock.patch.object(launch.qemu, "vm_mem", return_value=1024),
            mock.patch.object(launch.time, "sleep"),
        ):
            b = launch.Bg(env)
            open(b.password_file, "w").close()  # a stale one, from a killed run
            with self.assertRaises(launch.Error):  # nothing really starts
                b.start([])
        self.assertTrue(argv, "systemd-run was not run")
        seen = dict(
            a.removeprefix("--setenv=").partition("=")[::2]
            for a in argv
            if a.startswith("--setenv=")
        )
        return b, argv, seen

    def test_given_password_goes_in_a_file(self):
        b, argv, seen = self.start(BEANJAR_MITM_PASSWORD="given-pw")
        self.assertNotIn("given-pw", " ".join(argv))
        self.assertEqual(seen["NETSTUB_WEB_PASSWORD_FILE"], b.password_file)
        with open(b.password_file) as f:
            self.assertEqual(f.read(), "given-pw\n")
        self.assertEqual(os.stat(b.password_file).st_mode & 0o777, 0o600)

    def test_no_password_leaves_it_to_netns(self):
        b, argv, seen = self.start()
        self.assertNotIn("NETSTUB_WEB_PASSWORD_FILE", seen)
        self.assertFalse(os.path.exists(b.password_file), "a stale password is not reused")


class UnitEnvTest(unittest.TestCase):
    """The VM unit gets only its allowlist (unit.env_for), the caller's keep and the VM's vm.toml
    keys, never another variable of the caller's environment: not in systemd-run's argv, not in
    the environment of what the unit runs. Sentinel values only."""

    SECRETS = {
        "BEANJAR_VNC_PASSWORD": "sentinel-vnc-0x5e",
        "BEANJAR_MITM_PASSWORD": "sentinel-mitm-0x5e",
        "UNRELATED_API_TOKEN": "sentinel-token-0x5e",
        "AWS_SECRET_ACCESS_KEY": "sentinel-aws-0x5e",
    }
    ALLOWED = {
        "FW": "/sentinel/fw",
        "PMLOG_DEBUG": "sentinel-ctx",
        "MESA_D3D12_DEFAULT_ADAPTER_NAME": "sentinel-gpu",
        "GALLIUM_DRIVER": "sentinel-driver",
        "LC_ALL": "C.UTF-8",
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test-unit-env-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run_dir, self.log_dir = os.path.join(self.tmp, "run"), os.path.join(self.tmp, "logs")
        os.makedirs(self.run_dir)
        os.makedirs(self.log_dir)
        # The launcher stand-in: records whether each name of interest is in its environment, and
        # the value of the expected ones; never the environment as a whole.
        self.out = os.path.join(self.tmp, "seen.json")
        self.launcher = os.path.join(self.tmp, "launcher")
        names = sorted({*self.SECRETS, *self.ALLOWED, "KEPT_BY_CALLER"})
        with open(self.launcher, "w") as f:
            f.write(
                f"#!{sys.executable}\nimport json, os\n"
                f"names = {names!r}\n"
                f"seen = {{k: os.environ.get(k) for k in names}}\n"
                f"with open({self.out!r} + '.tmp', 'w') as f:\n    json.dump(seen, f)\n"
                f"os.rename({self.out!r} + '.tmp', {self.out!r})\n"
            )
        os.chmod(self.launcher, 0o700)

    def env(self):
        return dict(
            PATH="/usr/bin:/bin",
            RUN_DIR=self.run_dir,
            LOG_DIR=self.log_dir,
            RUN=self.launcher,
            KEPT_BY_CALLER="sentinel-kept",
            **self.SECRETS,
            **self.ALLOWED,
        )

    def expected(self):
        return {
            **{k: None for k in self.SECRETS},
            **self.ALLOWED,
            "KEPT_BY_CALLER": "sentinel-kept",
        }

    def test_env_for(self):
        env = unit.env_for(self.env(), keep=("KEPT_BY_CALLER",))
        for k in self.SECRETS:
            self.assertNotIn(k, env)
        for k, v in self.ALLOWED.items():
            self.assertEqual(env.get(k), v)
        self.assertEqual(env["KEPT_BY_CALLER"], "sentinel-kept")
        self.assertNotIn("KEPT_BY_CALLER", unit.env_for(self.env()))
        self.assertNotIn("BAD-NAME", unit.env_for({"BAD-NAME": "x"}, keep=("BAD-NAME",)))

    def test_systemd_run_argv(self):
        argv = []

        def run(cmd, **kw):
            argv.extend(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(unit.subprocess, "run", side_effect=run):
            self.assertTrue(unit.start("beanjar-vm-test-env", self.env(), keep=("KEPT_BY_CALLER",)))
        text = "\0".join(argv)
        for k, v in self.SECRETS.items():
            self.assertNotIn(v, text)
            self.assertNotIn(k, text)
        for k, v in {**self.ALLOWED, "KEPT_BY_CALLER": "sentinel-kept"}.items():
            self.assertIn(f"--setenv={k}={v}", argv)

    def wait_seen(self):
        end = time.monotonic() + 20
        while not os.path.exists(self.out):
            self.assertLess(time.monotonic(), end, "the launcher stand-in did not run")
            time.sleep(0.05)
        with open(self.out) as f:
            return json.load(f)

    def test_spawn_child_environment(self):
        """The setsid fallback: the launcher's environment."""
        pid = unit.spawn(self.env(), keep=("KEPT_BY_CALLER",))
        try:
            self.assertEqual(self.wait_seen(), self.expected())
        finally:
            if unit.alive(pid):
                testunits.end(pid, group=True)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    @unittest.skipUnless(unit.available(), "no user systemd")
    def test_unit_child_environment(self):
        """A real unit: the launcher's environment there."""
        name = testunits.PREFIX + "env"
        self.addCleanup(testunits.stop_ours)
        self.assertTrue(unit.start(name, self.env(), keep=("KEPT_BY_CALLER",)))
        self.assertEqual(self.wait_seen(), self.expected())

    def test_vm_toml_keys_pass(self):
        """A VM's vm.toml upper-case keys are launcher variables passed through (vmsettings.SETTINGS):
        Bg keeps them for the unit."""
        vm_dir = os.path.join(self.tmp, "vm")
        os.makedirs(vm_dir)
        core.vmconfig.write(
            os.path.join(vm_dir, "vm.toml"),
            {"platform": "webOS", "fw": "/fw", "link": "off", "SENTINEL_KNOB": "1"},
        )
        self.assertIn("SENTINEL_KNOB", launch.settings_vars(vm_dir))
        self.assertEqual(launch.settings_vars(self.tmp), ())


class MaxRunningTest(unittest.TestCase):
    """The launcher's start refuses a VM beyond the machine's max_running (in the test's own
    XDG_CONFIG_HOME: testunits.isolate) before anything changes, and asks again under its lock as it
    starts the unit; the VM it restarts is not counted. The host's units: a stand-in."""

    OTHER = {
        "beanjar-vm-lib_abcdef-other": {
            "Id": "beanjar-vm-lib_abcdef-other.service",
            "ActiveState": "active",
            "Description": "webOS VM /data/lib/vms/other/run",
        }
    }

    def setUp(self):
        hostconfig.set_global_option("max_running", "1")
        self.addCleanup(hostconfig.set_global_option, "max_running", "")
        self.tmp = tempfile.mkdtemp(prefix="test-max-running-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_dir_start_refused_before_anything(self):
        d = os.path.join(self.tmp, "lib", "vms", "x")
        os.makedirs(d)
        with open(os.path.join(d, "vm.toml"), "w") as f:
            f.write("")
        err = io.StringIO()
        with (
            mock.patch.object(launch.vmunits, "vm_units", return_value=self.OTHER),
            mock.patch.object(launch, "bg_start") as bg,
            mock.patch.object(sys, "stderr", err),
        ):
            self.assertEqual(launch.main(["start", "--dir", d]), 1)
        bg.assert_not_called()
        self.assertIn(
            "vm start: cannot start VM x: 1 VM already running and the limit is 1 (max_running): "
            "other of /data/lib. Stop one, or raise the limit: beanjar host config set max_running=2",
            err.getvalue(),
        )
        self.assertEqual(os.listdir(d), ["vm.toml"], "nothing made")
        with open(os.path.join(d, "vm.toml")) as f:
            self.assertEqual(f.read(), "", "no identity written")

    def bg_start(self, units):
        env = dict(
            RUN_DIR=f"{self.tmp}/run",
            LOG_DIR=f"{self.tmp}/logs",
            UNIT="beanjar-vm-test-max",
            BUILD="0",
        )
        with (
            mock.patch.object(launch.unit, "available", return_value=True),
            mock.patch.object(launch.unit, "start", return_value=False) as start,
            mock.patch.object(launch.vmunits, "unit_show", return_value={}),
            mock.patch.object(launch.vmunits, "vm_units", return_value=units),
            mock.patch.object(launch.qemu, "vm_mem", return_value=1024),
        ):
            with self.assertRaises(launch.Error) as e:
                launch.Bg(env).start([])
        return start, str(e.exception)

    def test_unit_start_under_the_lock(self):
        start, why = self.bg_start(self.OTHER)
        start.assert_not_called()
        self.assertIn("the limit is 1 (max_running)", why)
        self.assertTrue(
            os.path.exists(
                os.path.join(os.environ["XDG_CONFIG_HOME"], "beanjar", core.RUNNING_LOCK)
            )
        )
        # its own unit, still up (a restart), or another unit running its RUN_DIR: not counted
        own = {
            "beanjar-vm-test-max": dict(
                self.OTHER["beanjar-vm-lib_abcdef-other"], Description="webOS VM /x"
            ),
            "beanjar-vm-elsewhere": dict(
                self.OTHER["beanjar-vm-lib_abcdef-other"], Description=f"webOS VM {self.tmp}/run"
            ),
        }
        start, why = self.bg_start(own)
        start.assert_called_once()
        self.assertIn("could not start beanjar-vm-test-max.service", why)


class UnitResourcesTest(unittest.TestCase):
    def test_resources(self):
        """CPUWeight always; under KVM MemoryHigh at the RAM and the start's headroom where the RAM is
        known; under TCG no MemoryHigh."""
        kvm, tcg = {"ACCEL": "kvm"}, {"ACCEL": "tcg"}
        self.assertEqual(unit.resources(None, kvm), [f"CPUWeight={unit.CPU_WEIGHT}"])
        self.assertEqual(
            unit.resources(1536, kvm),
            [f"CPUWeight={unit.CPU_WEIGHT}", f"MemoryHigh={1536 + capacity.START_HEADROOM_MIB}M"],
        )
        self.assertEqual(unit.resources(1536, tcg), [f"CPUWeight={unit.CPU_WEIGHT}"])
        self.assertEqual(unit.resources(None, tcg), [f"CPUWeight={unit.CPU_WEIGHT}"])

    def test_reservation(self):
        """The admission's claim: RAM and headroom under KVM; under TCG also the TB cache."""
        h = capacity.START_HEADROOM_MIB
        self.assertEqual(capacity.reservation_mib(2048, {"ACCEL": "kvm"}), 2048 + h)
        self.assertEqual(
            capacity.reservation_mib(2048, {"ACCEL": "tcg"}), 2048 + qemu.TCG_TB_DEFAULT_MIB + h
        )
        self.assertEqual(
            capacity.reservation_mib(2048, {"ACCEL": "tcg", "TCG_TB_SIZE": "512"}), 2048 + 512 + h
        )
        self.assertEqual(
            capacity.reservation_mib(2048, {"ACCEL": "kvm", "TCG_TB_SIZE": "512"}), 2048 + h
        )

    def test_vm_mem(self):
        self.assertEqual(qemu.vm_mem({"MEM": "3000"}), 3000)
        self.assertIsNone(qemu.vm_mem({"FW": "/nonexistent/fw"}))
