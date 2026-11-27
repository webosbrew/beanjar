# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The real launcher (host/scripts/vm-launcher start|status|stop --dir) against the fake QEMU.

LaunchTest runs it against tests/fakes/qemu-system-fake (RUN), as a transient user unit where a
user systemd runs and through the setsid fallback always (SetsidLaunchTest), and checks that nothing
is left behind: no unit, no process, no private bus. It needs the guest build installed in the
checkout (skipped without it)."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import pytest
import testunits
from beanjar import base, capacity, compat, core, launch, paths, unit, vmunits
from launchkit import FW_NAME, H13_AFRO, TOP, TRUST_PAK, VM


def guest_build_missing():
    """Why the checkout's guest payload cannot start h13-afro (compat.components, the launcher's
    own check), or None."""
    try:
        compat.components(paths.TREE, H13_AFRO, "x")
    except compat.Error as e:
        return str(e)
    return None


_missing = guest_build_missing()
pytestmark = pytest.mark.skipif(bool(_missing), reason=_missing or "")


class LaunchTest(unittest.TestCase):
    """The real launcher (`host/scripts/vm-launcher ... --dir`) with the fake QEMU as RUN."""

    systemd = unit.available()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-launch-")
        # the VM is one of a library (its CA), named so that its unit is this process's (testunits)
        self.lib = os.path.join(self.tmp, f"test{os.getpid()}")
        self.dir = os.path.join(self.lib, "vms", "x")
        os.makedirs(self.dir)
        self.fw = fw = os.path.join(
            self.tmp, FW_NAME
        )  # an external fw: used in place, never written
        os.makedirs(fw)
        shutil.copyfile(TRUST_PAK, os.path.join(fw, "rootfs.pak"))
        core.vmconfig.write(
            os.path.join(self.dir, "vm.toml"),
            # the fake QEMU's FAKE_VM_DIR: a vm.toml key, which the unit's environment takes
            {
                "platform": "webOS",
                "fw": fw,
                "display": "dbus",
                "link": "off",
                "FAKE_VM_DIR": self.dir,
            },
        )
        self.env = {k: v for k, v in os.environ.items() if k not in launch.RUNTIME_VARS}
        self.env.update(RUN=os.path.join(TOP, "tests", "fakes", "qemu-system-fake"), SAVE_LOGS="0")
        if not self.systemd:
            self.env["VM_SYSTEMD"] = "0"

    def tearDown(self):
        testunits.stop_ours()
        for n in ("launcher.pid", "qemu.pid", "dbus-daemon.pid"):
            pid = base.read_pid(os.path.join(self.dir, "run", n))
            if pid and base.proc_alive(pid, "qemu-system", "beanjar.unit", "dbus-daemon"):
                testunits.end(pid)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def vm(self, *args, ok=True):
        r = subprocess.run(
            [sys.executable, VM, *args],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )
        if ok is not None:
            self.assertEqual(
                r.returncode == 0,
                ok,
                f"host/scripts/vm-launcher {' '.join(args)}: rc={r.returncode}\n{r.stdout}",
            )
        return r

    def test_start_status_stop(self):
        unit_name = vmunits.vm_unit_name(self.dir)
        tag = vmunits.library_tag(self.lib)
        self.assertEqual(unit_name, f"beanjar-vm-{tag}-x")
        self.assertRegex(tag, rf"^test{os.getpid()}_[0-9a-f]{{6}}$")
        self.assertTrue(testunits.OURS.match(unit_name))
        out = self.vm("start", "--dir", self.dir, "w=1").stdout
        # a vm.toml without the TV's identity gets one (outside a library: the default prefix)
        cfg = core.vmconfig.read(os.path.join(self.dir, "vm.toml"))
        self.assertRegex(cfg.get("mac", ""), r"^a8:23:fe(:[0-9a-f]{2}){3}$")
        self.assertRegex(cfg.get("serial", ""), r"^[0-9]{3}[A-Z]{4}[0-9]{5}$")
        self.assertIn("warning:", out)
        self.assertEqual(os.stat(os.path.join(self.dir, "identity")).st_mode & 0o777, 0o700)
        # and the SSH keys it lacks, with a warning each (keys.ensure)
        for name in (
            "id_ed25519",
            "rootssh_host_key",
            "devmode_host_key",
            "devmode_rsa",
            "authorized_keys",
        ):
            self.assertIn(f"had no {name}", out)
        self.assertEqual(
            os.stat(os.path.join(self.dir, "identity", "id_ed25519")).st_mode & 0o777, 0o600
        )
        # and the library's CA (keys.ensure_ca), which is not in the VM's identity/
        ca = os.path.join(self.lib, "ca")
        self.assertIn(f"warning: no CA in {ca}", out)
        self.assertEqual(os.stat(ca).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(ca, "ca.key")).st_mode & 0o777, 0o600)
        self.assertNotIn("ca", os.listdir(os.path.join(self.dir, "identity")))
        st = self.vm("status", "--dir", self.dir).stdout.split()
        self.assertEqual(st[0], "running")
        pid = int(st[1])
        # the external fw as it is; its partition map goes into the VM's folder
        with open(f"/proc/{pid}/environ", "rb") as f:
            qenv = dict(kv.decode().partition("=")[::2] for kv in f.read().split(b"\0") if kv)
        self.assertEqual(
            (qenv.get("FW"), qenv.get("FW_CACHE"), qenv.get("FIRMWARE_CA"), qenv.get("ROOTFS")),
            (self.fw, os.path.join(self.dir, "firmware-cache"), None, None),
        )
        bus = base.read_pid(os.path.join(self.dir, "run", "dbus-daemon.pid"))
        self.assertTrue(base.proc_alive(bus, "dbus-daemon"), "no private bus")
        if self.systemd:
            self.assertEqual(vmunits.unit_show(unit_name).get("ActiveState"), "active")
            self.assertTrue(base.in_unit(pid, unit_name) and base.in_unit(bus, unit_name))
            self.assertEqual(
                vmunits.unit_show(unit_name).get("Description"), f"webOS VM {self.dir}/run"
            )
            # in its library's slice, in beanjar.slice; found by vm_units, and its VM by unit_vm
            rc, props = vmunits.systemctl(
                "show", "--property=Slice", "--property=ControlGroup", f"{unit_name}.service"
            )
            self.assertIn(f"Slice=beanjar-{tag}.slice", props.split())
            self.assertIn(f"/beanjar.slice/beanjar-{tag}.slice/{unit_name}.service", props)
            self.assertIn(unit_name, vmunits.vm_units())
            # a directory without config.toml is no library (unit_vm makes none); with one, its VM
            self.assertIsNone(core.unit_vm(vmunits.vm_units()[unit_name]))
            core.Library(self.lib, repo=TOP)
            self.assertEqual(core.unit_vm(vmunits.vm_units()[unit_name]).dir, self.dir)
            # its resources: a lower CPU weight, and memory held to the VM's RAM and the headroom
            rc, props = vmunits.systemctl(
                "show", "--property=CPUWeight", "--property=MemoryHigh", f"{unit_name}.service"
            )
            mem = launch.qemu.vm_mem(dict(self.env, FW=self.fw))
            qemu_accel = launch.qemu.vm_accel(dict(os.environ, **self.env))
            self.assertTrue(mem)
            self.assertIn(f"CPUWeight={unit.CPU_WEIGHT}", props.split())
            if qemu_accel == "kvm":
                self.assertIn(
                    f"MemoryHigh={capacity.reservation_mib(mem, dict(os.environ, **self.env)) * 1024 * 1024}",
                    props.split(),
                )
            else:
                self.assertNotIn(
                    f"MemoryHigh={capacity.reservation_mib(mem, dict(os.environ, **self.env)) * 1024 * 1024}",
                    props.split(),
                )
                self.assertIn("MemoryHigh=infinity", props.split())
        else:
            self.assertTrue(
                base.proc_alive(base.read_pid(os.path.join(self.dir, "run", "launcher.pid")))
            )
        self.assertIn("already running", self.vm("start", "--dir", self.dir, ok=False).stdout)
        self.vm("stop", "--dir", self.dir)
        self.assertEqual(self.vm("status", "--dir", self.dir, ok=False).returncode, 3)
        for p in (pid, bus):
            self.assertFalse(
                base.proc_alive(p, "qemu-system", "dbus-daemon"), f"pid {p} survived the stop"
            )
        self.assertNotIn(unit_name, testunits.units())
        run = os.listdir(os.path.join(self.dir, "run"))
        for n in ("qemu.pid", "launcher.pid", "dbus-daemon.pid", "dbus.sock"):
            self.assertNotIn(n, run)
        self.vm("stop", "--dir", self.dir)  # stopping what is off is fine
        self.assertEqual(os.listdir(self.fw), ["rootfs.pak"], "the external fw was written")
        # the CA is kept; one that is lost is made again at the next start, with a warning
        pem = os.path.join(ca, "ca.pem")
        with open(pem, "rb") as f:
            old = f.read()
        self.assertNotIn("no CA", self.vm("start", "--dir", self.dir).stdout)
        self.vm("stop", "--dir", self.dir)
        with open(pem, "rb") as f:
            self.assertEqual(f.read(), old)
        os.unlink(pem)
        self.assertIn(f"warning: no CA in {ca}", self.vm("start", "--dir", self.dir).stdout)
        self.vm("stop", "--dir", self.dir)
        with open(pem, "rb") as f:
            self.assertNotEqual(f.read(), old)

    def test_address_prefix_pins(self):
        """The library's addresses: the launcher's environment (ADDRESS_PREFIX, which qemu.plan takes;
        a launch's rewrite of the pins is ArgvTest's), its address (config ADDRESS) and the new VM's pins."""
        with open(os.path.join(self.lib, "config.toml"), "w") as f:
            f.write('addresses = "127.0.2"\n')
        self.vm("start", "--dir", self.dir)
        with open(
            f"/proc/{int(self.vm('status', '--dir', self.dir).stdout.split()[1])}/environ", "rb"
        ) as f:
            qenv = dict(kv.decode().partition("=")[::2] for kv in f.read().split(b"\0") if kv)
        self.vm("stop", "--dir", self.dir)
        self.assertEqual(qenv.get("ADDRESS_PREFIX"), "127.0.2")
        self.assertEqual(
            self.vm("config", "ADDRESS", "--dir", self.dir).stdout.strip(), "127.0.2.1"
        )
        with open(os.path.join(self.dir, "identity", "known_hosts")) as f:
            self.assertEqual(
                sorted(line.split()[0] for line in f), ["[127.0.2.1]:6622", "[127.0.2.1]:9922"]
            )

    def test_power_off(self):
        """QEMU exiting by itself ends the unit, the bus and the launcher."""
        unit_name = vmunits.vm_unit_name(self.dir)
        self.vm("start", "--dir", self.dir)
        pid = int(self.vm("status", "--dir", self.dir).stdout.split()[1])
        bus = base.read_pid(os.path.join(self.dir, "run", "dbus-daemon.pid"))
        os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + 10
        while time.monotonic() < end and (
            base.proc_alive(bus, "dbus-daemon") or unit_name in testunits.units()
        ):
            time.sleep(0.1)
        self.assertFalse(base.proc_alive(bus, "dbus-daemon"))
        self.assertNotIn(unit_name, testunits.units())
        self.assertEqual(self.vm("status", "--dir", self.dir, ok=False).returncode, 3)

    def test_stop_power_off_without_console(self):
        """stop --power-off with no serial console: exit 5, leave the VM running for the caller
        (core.Library.stop falls back), and a later plain stop ends it."""
        self.vm("start", "--dir", self.dir)
        pid = int(self.vm("status", "--dir", self.dir).stdout.split()[1])
        p = self.vm("stop", "--dir", self.dir, "--power-off", "1", ok=False)
        self.assertEqual(p.returncode, core.STOP_REFUSED)
        self.assertIn("power-off: no serial console", p.stdout)
        self.assertTrue(base.proc_alive(pid, "qemu-system"), "the VM is left running")
        self.vm("stop", "--dir", self.dir)
        self.assertEqual(self.vm("status", "--dir", self.dir, ok=False).returncode, 3)

    def test_unit_main_ends_by_sigterm(self):
        """A stop (SIGTERM to every process, as systemd's KillMode=control-group sends it) ends the
        unit's main process by that SIGTERM, which systemd counts as clean, after QEMU's output."""
        run, logs = os.path.join(self.dir, "run"), os.path.join(self.dir, "logs")
        os.makedirs(run)
        os.makedirs(logs)
        env = dict(
            self.env,
            RUN_DIR=run,
            LOG_DIR=logs,
            DISPLAY_BACKEND="dbus",
            FAKE_VM_DIR=self.dir,
            PYTHONPATH=os.path.join(TOP, "host", "core")
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        )
        p = subprocess.Popen(
            [sys.executable, "-m", "beanjar.unit", "run"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            end = time.monotonic() + 10
            while not base.proc_alive(base.read_pid(os.path.join(run, "qemu.pid")), "qemu-system"):
                self.assertLess(time.monotonic(), end, "the fake QEMU did not start")
                time.sleep(0.05)
            bus = base.read_pid(os.path.join(run, "dbus-daemon.pid"))
            os.killpg(p.pid, signal.SIGTERM)
            self.assertEqual(p.wait(timeout=20), -signal.SIGTERM)
            self.assertFalse(base.proc_alive(bus, "dbus-daemon"))
            self.assertFalse(os.path.exists(os.path.join(run, "dbus.sock")))
        finally:
            if p.poll() is None:
                testunits.end(p.pid, group=True)
                p.wait()

    def test_refusals(self):
        self.assertIn(
            "no VM directory", self.vm("start", "--dir", self.dir + "-none", ok=False).stdout
        )
        os.unlink(os.path.join(self.dir, "vm.toml"))
        self.assertIn("vm.toml", self.vm("start", "--dir", self.dir, ok=False).stdout)
        self.assertEqual(self.vm("status", ok=False).returncode, 3)


class SetsidLaunchTest(LaunchTest):
    """The same without a user systemd (VM_SYSTEMD=0)."""

    systemd = False
