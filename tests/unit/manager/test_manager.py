#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The VM manager's core and CLI (host/core) with a fake epk2extract, launcher and QEMU, and a fake viewer: no QEMU, no GUI. `meson test -C build/host --suite manager`,
or run directly.

Each test gets a temporary library, a temporary, empty "checkout" and a stand-in for the host's
free memory (BEANJAR_MEMINFO, which the start's admission reads). A .epk comes in as --firmware,
through the CLI's create or the GUI's New VM worker, python3 -m beanjar.vm.create (newvm()).

The fake launcher runs the fake QEMU through the real beanjar.runtime.unit: where a user systemd runs,
as real transient units (beanjar-vm-test<pid>-<vm>) with a real private dbus-daemon in each, so the
unit's lifetime, its control group and `stop --unplug`'s SIGKILL are the real ones; without one, and
in SetsidTest always, through the setsid fallback."""

import base64
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import uuid

import pytest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
from beanjar import base, paths, tomlfile  # noqa: E402
from beanjar.vm import library as vm_library, keys, identity as vm_identity, settings as vm_settings  # noqa: E402
from beanjar.runtime import capacity, hostconfig, hostprocs, poweroff, unit, vmunits  # noqa: E402
from beanjar.firmware import install, targets  # noqa: E402
from beanjar.display import framing  # noqa: E402
import testunits  # noqa: E402

SYSTEMD = unit.available()

FW = "05.06.10.01-HE_DTV_WT1H_AFAAABAA"
H13_AFRO = ("h13", "afro")
STATE_PARTS = targets.state_parts(H13_AFRO)
PAKS = [p + ".pak" for p in targets.pak_files(H13_AFRO)]
# trust-rootfs.pak (fake-epk2extract's rootfs.pak) is an afro rootfs (its etc/starfish-release: 1.4.0-2341 (afro-ashley)),
# which import checks against the model's target
TRUST_PAK = os.path.join(TOP, "tests", "fakes", "trust-rootfs.pak")


def rootfs_pak(path, tag):
    """A rootfs.pak of WT1H's release: trust-rootfs.pak, with tag after the image (SquashFS ignores
    what follows it) so that each has its own contents and digest."""
    with open(TRUST_PAK, "rb") as src, open(path, "wb") as f:
        f.write(src.read() + tag)


def jload(path):
    with open(path) as f:
        return json.load(f)


def tload(path):
    if os.path.basename(path) == "vm.toml":
        return vm_library.vm_config.read(path)
    with open(path, "rb") as f:
        return tomllib.load(f)


def slurp(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


ACTIVE = set()  # the temporary directories of the tests that run


def cleanup(tmp):
    """Whatever a test left running in tmp - the units of this test process, the setsid fallback's
    QEMU and viewers - then tmp itself."""
    testunits.stop_ours()
    for root, _dirs, files in os.walk(tmp):
        for n in files:
            if n in ("qemu.pid", "viewer.pid", "vnc.pid", "launcher.pid"):
                pid = base.read_pid(os.path.join(root, n))
                if pid and base.proc_alive(
                    pid, "qemu-system", "beanjar-viewer", "beanjar-vnc", "beanjar.runtime.unit"
                ):
                    testunits.end(pid, group=n == "launcher.pid")  # the session: QEMU, the bus
    shutil.rmtree(tmp, ignore_errors=True)
    ACTIVE.discard(tmp)


@pytest.fixture(autouse=True, scope="module")
def _units():
    """The test units stopped however the run ends (testunits): installed when the tests run, not
    when they are collected."""
    testunits.install(lambda: [cleanup(t) for t in list(ACTIVE)])


# What identity/ has after create (beanjar/vm/keys.py).
KEY_FILES = sorted(
    [
        "authorized_keys",
        "devmode_host_key",
        "devmode_host_key.pub",
        "devmode_rsa",
        "devmode_rsa.pub",
        "id_ed25519",
        "id_ed25519.pub",
        "known_hosts",
        "lgudid.toml",
        "rootssh_host_key",
        "rootssh_host_key.pub",
        "tee-huk",
    ]
)


def guest_payload(repo, target="h13-afro"):
    """What a guest build of target publishes that a VM's create checks (compat.py): the target's
    always-used layers."""
    chip, release = target.split("-")
    for name in targets.always_layers((chip, release)):
        os.makedirs(os.path.join(repo, "build", "layers", target, name), exist_ok=True)


class ManagerTest(unittest.TestCase):
    systemd = SYSTEMD

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-test-")
        ACTIVE.add(self.tmp)
        # the first cleanup runs last: after the test's own (a VM stopped, a directory made
        # writable again), which would otherwise run after tmp is gone and make it again
        self.addCleanup(cleanup, self.tmp)
        self.lib_dir = os.path.join(self.tmp, "lib")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "build"))
        guest_payload(self.repo)
        board = os.path.join(
            "guest", "layers", "soc", "h13", "rootfs", "etc", "emulator", "board.cmdline"
        )
        os.makedirs(os.path.dirname(os.path.join(self.repo, board)))  # the model's hwopt: its RAM
        shutil.copyfile(os.path.join(TOP, board), os.path.join(self.repo, board))
        self.src = os.path.join(self.tmp, "src")  # stands for the user's firmware collection
        os.makedirs(self.src)
        self.epk = os.path.join(self.src, "fw.epk")
        with open(self.epk, "wb") as f:
            f.write(b"EPK2" * 1000)
        self.home = os.path.join(self.tmp, "home")  # the user's ~/.ssh/*.pub seed authorized_keys
        os.makedirs(os.path.join(self.home, ".ssh"))
        for n, line in (
            ("id_rsa.pub", "ssh-rsa AAAArsa me@pc"),
            ("id_ed25519.pub", "ssh-ed25519 AAAAed me@pc"),
            ("id_ed25519-cert.pub", "ssh-ed25519-cert-v01@openssh.com AAAAcert me@pc"),
            ("id_ed25519", "-----BEGIN OPENSSH PRIVATE KEY-----"),  # gitleaks:allow
        ):
            with open(os.path.join(self.home, ".ssh", n), "w") as f:
                f.write(line + "\n")
        self.env = dict(
            os.environ,
            HOME=self.home,
            PYTHONPATH=os.path.join(TOP, "host", "core")
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
            PYTHONDONTWRITEBYTECODE="1",
            BEANJAR_LIBRARY=self.lib_dir,
            BEANJAR_LAUNCHER=os.path.join(TOP, "tests", "fakes", "fake-vm"),
            BEANJAR_VIEWER=os.path.join(TOP, "tests", "fakes", "fake-beanjar-viewer"),
            BEANJAR_VNC=os.path.join(TOP, "tests", "fakes", "fake-beanjar-vnc"),
            BEANJAR_EPK2EXTRACT=os.path.join(TOP, "tests", "fakes", "fake-epk2extract"),
            FAKE_UNIT_PREFIX=testunits.PREFIX,
            BEANJAR_MEMINFO=os.path.join(self.tmp, "meminfo"),
        )  # the host's free memory
        self.mem_available(65536)
        if not self.systemd:
            self.env["VM_SYSTEMD"] = "0"
        for k in (
            "FAKE_EPK_NAME",
            "FAKE_EPK_MISSING",
            "FAKE_EPK_SLEEP",
            "FAKE_EPK_FAIL",
            "FAKE_BOOT_DELAY",
            "FAKE_QMP_STATUS",
        ):
            self.env.pop(k, None)
        self.fw_src = os.path.join(
            self.tmp, "fws", FW
        )  # a directory of extracted paks (a WT1H: h13-afro)
        os.makedirs(self.fw_src)
        rootfs_pak(os.path.join(self.fw_src, "rootfs.pak"), b"rootfs of fwdir")
        with open(os.path.join(self.fw_src, "fonts.pak"), "wb") as f:
            f.write(b"hsqs fonts of fwdir")
        open(os.path.join(self.fw_src, "kernel.pak"), "wb").close()  # not one the guest attaches

    def mem_available(self, mib):
        """What the host has free, as the start's admission reads it (BEANJAR_MEMINFO)."""
        with open(os.path.join(self.tmp, "meminfo"), "w") as f:
            f.write(
                f"MemTotal:       67108864 kB\nMemFree:        1024 kB\nMemAvailable:   {mib * 1024} kB\n"
            )

    def cli(self, *args, ok=True, env=None, timeout=60, input=None):
        p = subprocess.run(
            [sys.executable, "-m", "beanjar", "--repo", self.repo, *args],
            env=env or self.env,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        if ok is not None:
            self.assertEqual(
                p.returncode == 0, ok, f"beanjar {' '.join(args)}: rc={p.returncode}\n{p.stdout}"
            )
        return p.stdout

    def newvm(self, *args, ok=True, env=None):
        """The GUI's New VM worker (python3 -m beanjar.vm.create): create's options."""
        p = subprocess.run(
            [sys.executable, "-m", "beanjar.vm.create", "--repo", self.repo, *args],
            env=env or self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
        if ok is not None:
            self.assertEqual(
                p.returncode == 0,
                ok,
                f"beanjar.vm.create {' '.join(args)}: rc={p.returncode}\n{p.stdout}",
            )
        return p.stdout

    def listing(self):
        return {r["name"]: r for r in self.cli_json("list")}

    def cli_json(self, *args, ok=True, input=None):
        """A command with --json: its stdout parsed (stderr, its progress, apart)."""
        p = subprocess.run(
            [sys.executable, "-m", "beanjar", "--repo", self.repo, *args, "--json"],
            env=self.env,
            input=input,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(p.returncode == 0, ok, f"beanjar {' '.join(args)}: {p.stdout}{p.stderr}")
        try:
            return json.loads(p.stdout) if ok else json.loads(p.stderr)
        except ValueError:
            self.fail(f"beanjar {' '.join(args)} --json: not JSON\n{p.stdout}\n{p.stderr}")

    def info(self, name):
        return self.cli_json("show", name)

    def lib(self):
        return vm_library.Library(self.lib_dir, self.repo)

    # ---------------------------------------------------------------- create, settings, delete
    def test_create_defaults_and_refusals(self):
        out = self.cli("create", "tv1", "--import", "--firmware", self.fw_src)
        self.assertIn("created tv1", out)
        path = os.path.join(self.lib_dir, "vms", "tv1", "vm.toml")
        cfg = tload(path)
        self.assertNotIn("fw", cfg, "the VM's own firmware/ is the default")
        # the directory's paks (not kernel.pak, which the guest does not attach) are the VM's own
        # firmware: hard links on the same filesystem, with a manifest of where they came from
        own = os.path.join(self.lib_dir, "vms", "tv1", "firmware")
        self.assertEqual(sorted(os.listdir(own)), ["fonts.pak", "manifest.toml", "rootfs.pak"])
        for p in ("rootfs.pak", "fonts.pak"):
            self.assertTrue(os.path.samefile(os.path.join(own, p), os.path.join(self.fw_src, p)))
        m = tload(os.path.join(own, "manifest.toml"))
        self.assertEqual(
            (m["name"], m["chip"], m["release"], m["source"], m["paks"]),
            (FW, "h13", "afro", self.fw_src, ["rootfs.pak", "fonts.pak"]),
        )
        self.assertEqual(self.lib().vm("tv1").firmware_dir(), own)
        self.assertEqual(self.listing()["tv1"]["firmware"], FW)
        # a directory of a firmware this tree does not boot is refused before anything is made
        other = os.path.join(self.tmp, "43.22.40.01-HE_DTV_XXXX_AFABJAPU")
        shutil.copytree(self.fw_src, other)
        out = self.cli("create", "tv9", "--import", "--firmware", other, ok=False)
        self.assertIn(
            "XXXX afro firmware might be unsupported: no board definition covers it",
            out,
        )
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "tv9")))
        self.assertFalse(
            os.path.exists(os.path.join(self.lib_dir, "firmware")), "no firmware library"
        )
        self.assertEqual(
            (cfg["layers"], cfg["display"], cfg["audio"], cfg["link"], cfg["mode"], cfg["smp"]),
            (["normal", "gl"], "dbus", "dbus", "nat", "mitm", 4),
        )
        self.assertNotIn("mem", cfg)  # none: the model's RAM at each launch
        self.assertEqual(
            self.lib().vm("tv1").mem(), 3072
        )  # WT1H's 49UB850V, 3 GB (its lxboot's UHD path)
        self.assertEqual(self.listing()["tv1"]["mem"], 3072)
        text = slurp(path)
        self.assertTrue(text.startswith("# "), "no header comment")
        with open(path, "rb") as stream:
            document = tomllib.load(stream)
        self.assertEqual(
            list(document),
            [
                "firmware",
                "hardware",
                "display",
                "audio",
                "network",
                "layers",
                "identity",
                "metadata",
            ],
        )
        self.assertEqual(document["firmware"], {"directory": "firmware"})
        self.assertNotIn("platform", document)
        self.assertEqual(self.lib().vm("tv1").summary()["platform"], "webOS")
        self.assertEqual(self.listing()["tv1"]["platform"], "webOS 1")
        self.assertIn('enabled = ["normal", "gl"]\n', text)
        self.assertFalse(
            os.path.exists(os.path.join(self.lib_dir, "vms", "tv1", "compatibility.toml")),
            "no per-VM marker",
        )
        # a guest payload without the target's components is refused before anything is made
        shutil.rmtree(os.path.join(self.repo, "build", "layers", "h13-afro", "soc"))
        self.assertIn(
            "has no soc component for h13-afro",
            self.cli("create", "tv3", "--import", "--firmware", self.fw_src, ok=False),
        )
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "tv3")))
        guest_payload(self.repo)
        self.assertIn(
            "exists", self.cli("create", "tv1", "--import", "--firmware", self.fw_src, ok=False)
        )
        self.assertIn(
            "invalid VM name",
            self.cli("create", "../x", "--import", "--firmware", self.fw_src, ok=False),
        )
        self.assertIn(
            "no rootfs.pak", self.cli("create", "tv2", "--import", "--firmware", self.tmp, ok=False)
        )
        self.assertIn("give --firmware", self.cli("create", "tv2", ok=False))
        self.assertIn("invalid choice", self.cli("import", self.epk, ok=False))  # the GUI's only
        self.assertIn("invalid choice", self.cli("firmware", ok=False))  # no firmware library
        self.assertIn(
            "unrecognized arguments",
            self.cli("create", "tv2", "--import", "--firmware", self.fw_src, "--copy", ok=False),
        )
        self.cli("create", "tv2", "--import", "--firmware", self.fw_src, "--display", "none")
        self.assertEqual(
            tload(os.path.join(self.lib_dir, "vms", "tv2", "vm.toml"))["audio"], "none"
        )
        self.assertIn(
            "audio=dbus needs",
            self.cli(
                "create",
                "tv3",
                "--import",
                "--firmware",
                self.fw_src,
                "--display",
                "none",
                "--audio",
                "dbus",
                ok=False,
            ),
        )
        self.cli("config", "set", "tv1", "mem=1024", "link=off", "layers=gl,devmode")
        cfg = tload(os.path.join(self.lib_dir, "vms", "tv1", "vm.toml"))
        self.assertEqual(
            (cfg["mem"], cfg["link"], cfg["mode"], cfg["layers"]),
            (1024, "off", "mitm", ["gl", "devmode"]),
        )
        self.assertEqual(self.listing()["tv1"]["network"], "off")
        self.assertIn("one of", self.cli("config", "set", "tv1", "link=bogus", ok=False))
        self.assertIn("one of", self.cli("config", "set", "tv1", "mode=off", ok=False))
        self.assertIn("KEY one of", self.cli("config", "set", "tv1", "net=off", ok=False))
        self.assertIn("KEY one of", self.cli("config", "set", "tv1", "NET_LINK=off", ok=False))
        self.assertIn(
            "address is for link=lan", self.cli("config", "set", "tv1", "address=dhcp", ok=False)
        )
        # lan with direct: LG's services cannot be mocked there, and the guest takes its own address
        self.assertIn(
            "needs lg_services=real",
            self.cli("config", "set", "tv1", "link=lan", "mode=direct", ok=False),
        )
        self.assertIn(
            "address is for link=lan with mode=mitm",
            self.cli(
                "config",
                "set",
                "tv1",
                "link=lan",
                "mode=direct",
                "lg_services=real",
                "address=dhcp",
                ok=False,
            ),
        )
        self.cli("config", "set", "tv1", "link=lan", "mode=direct", "lg_services=real")
        cfg = tload(os.path.join(self.lib_dir, "vms", "tv1", "vm.toml"))
        self.assertEqual(
            (cfg["link"], cfg["mode"], cfg["lg_services"], "address" in cfg),
            ("lan", "direct", "real", False),
        )
        self.assertEqual(self.listing()["tv1"]["network"], "lan+direct")
        self.assertEqual(
            self.lib().vm("tv1").listeners(), [], "lan+direct: nothing of it on this host"
        )
        self.assertIn(
            "listen is for link=nat", self.cli("config", "set", "tv1", "listen=0.0.0.0", ok=False)
        )
        self.assertIn("needs the library's lan_parent", self.cli("start", "tv1", ok=False))
        self.assertEqual(self.listing()["tv1"]["state"], "Off")
        self.cli(
            "config",
            "set",
            "tv1",
            "mode=mitm",
            "lg_services=mock",
            "address=192.168.1.50/24,192.168.1.1",
        )
        cfg = tload(os.path.join(self.lib_dir, "vms", "tv1", "vm.toml"))
        self.assertEqual(
            (cfg["link"], cfg["mode"], cfg["address"]),
            ("lan", "mitm", "192.168.1.50/24,192.168.1.1"),
        )
        self.assertEqual(
            self.lib().vm("tv1").listeners(),
            [("127.0.1.1", p) for p in (3000, 3001, 9922, 9991)],
            "lan: on its own LAN address, and the host side of its relay on its address",
        )
        self.assertIn("needs the library's lan_parent", self.cli("start", "tv1", ok=False))
        self.assertEqual(self.listing()["tv1"]["state"], "Off")
        self.assertIn(
            "address is for link=lan", self.cli("config", "set", "tv1", "link=nat", ok=False)
        )
        self.cli("config", "unset", "tv1", "address")
        self.cli("config", "set", "tv1", "link=nat", "mode=mitm")
        cfg = tload(os.path.join(self.lib_dir, "vms", "tv1", "vm.toml"))
        self.assertEqual((cfg["link"], cfg["mode"], "address" in cfg), ("nat", "mitm", False))
        # the inspector's flow history: vmsettings.MITM_FLOWS by default, in status, the default not written out
        self.assertEqual(self.info("tv1")["mitm_flows"], vm_settings.MITM_FLOWS)
        self.cli("config", "set", "tv1", "mitm_flows=200")
        self.assertEqual(
            tload(os.path.join(self.lib_dir, "vms", "tv1", "vm.toml"))["mitm_flows"], 200
        )
        self.assertEqual(self.info("tv1")["mitm_flows"], 200)
        self.assertIn("0 for no limit", self.cli("config", "set", "tv1", "mitm_flows=-5", ok=False))
        self.cli("config", "set", "tv1", f"mitm_flows={vm_settings.MITM_FLOWS}")
        self.assertNotIn("mitm_flows", tload(os.path.join(self.lib_dir, "vms", "tv1", "vm.toml")))
        self.cli("config", "set", "tv1", "mitm_flows=50")
        self.cli("config", "unset", "tv1", "mitm_flows")
        self.assertNotIn("mitm_flows", tload(os.path.join(self.lib_dir, "vms", "tv1", "vm.toml")))
        self.assertEqual(self.info("tv1")["mitm_flows"], vm_settings.MITM_FLOWS)
        self.assertIn(
            "unset tv1 mitm_flows", self.cli("config", "set", "tv1", "mitm_flows=", ok=False)
        )
        # and its byte budget: off by default, a size, empty or 0 off again
        self.assertEqual(self.info("tv1")["mitm_flow_bytes"], "0")
        self.cli("config", "set", "tv1", "mitm_flow_bytes=200m")
        self.assertEqual(
            tload(os.path.join(self.lib_dir, "vms", "tv1", "vm.toml"))["mitm_flow_bytes"], "200M"
        )
        self.assertEqual(self.info("tv1")["mitm_flow_bytes"], "200M")
        self.assertIn(
            "a size such as 200M",
            self.cli("config", "set", "tv1", "mitm_flow_bytes=lots", ok=False),
        )
        self.cli("config", "unset", "tv1", "mitm_flow_bytes")
        self.assertNotIn(
            "mitm_flow_bytes", tload(os.path.join(self.lib_dir, "vms", "tv1", "vm.toml"))
        )
        self.cli("config", "set", "tv1", "link=off")
        self.assertIn("positive number", self.cli("config", "set", "tv1", "mem=lots", ok=False))
        self.cli("delete", "tv2")
        self.assertEqual(sorted(self.listing()), ["tv1"])

    def test_repo_option_is_for_one_command(self):
        """--repo names the checkout for that command only: config.toml is not written;
        `library config set repo=DIR` is the one way to store it, and without any known checkout the error says so."""
        config = os.path.join(self.lib_dir, "config.toml")
        scratch = os.path.join(self.tmp, "scratch-copy")
        os.makedirs(scratch)
        self.cli("library", "config", "show")  # --repo self.repo, for this command
        self.assertFalse(os.path.exists(config))
        self.cli("library", "config", "set", "mac_prefix=02:BE:A0")
        with open(config, "rb") as f:
            before = f.read()
        out = subprocess.run(
            [sys.executable, "-m", "beanjar", "--repo", scratch, "library", "config", "show"],
            env=self.env, capture_output=True, text=True, timeout=60,
        ).stdout  # fmt: skip
        self.assertNotIn(scratch, out)
        with open(config, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertNotIn("repo", tload(config))
        self.assertEqual(vm_library.Library(self.lib_dir, scratch).repo, scratch)
        with open(config, "rb") as f:
            self.assertEqual(f.read(), before)
        # first use from a checkout: the running tree, in memory only
        self.assertEqual(vm_library.Library(self.lib_dir).repo, vm_library.default_repo())
        with open(config, "rb") as f:
            self.assertEqual(f.read(), before)
        # nothing known at all: the error names the command that stores a checkout
        with mock.patch.object(vm_library, "default_repo", return_value=None):
            with self.assertRaises(base.Error) as e:
                vm_library.Library(self.lib_dir).repo
        self.assertIn("beanjar library config set repo=DIR", str(e.exception))
        self.cli("library", "config", "set", f"repo={scratch}")
        self.assertEqual(tload(config)["repo"], scratch)
        self.assertIn(f"repo: {scratch}", self.cli("library", "config", "show"))
        self.assertEqual(vm_library.Library(self.lib_dir).repo, scratch)

    def test_identity(self):
        """Each VM is a TV of its own: a MAC of the library's mac_prefix and a serial, made at create,
        unique in the library, editable with set; made at start when vm.toml has none."""
        out = self.cli("library", "config", "show")
        self.assertIn("mac_prefix: a8:23:fe", out)  # the default, shown
        self.cli("create", "tv1", "--import", "--firmware", self.fw_src)
        a = self.vm_toml("tv1")
        self.assertRegex(a["mac"], r"^a8:23:fe(:[0-9a-f]{2}){3}$")
        self.assertRegex(a["serial"], r"^[0-9]{3}[A-Z]{4}[0-9]{5}$")
        for bad in ("02:00", "01:00:00", "zz:00:00", "02:00:00:00"):
            self.assertIn(
                "mac_prefix", self.cli("library", "config", "set", f"mac_prefix={bad}", ok=False)
            )
        self.assertIn("KEY one of", self.cli("library", "config", "set", "bogus=1", ok=False))
        self.assertIn(
            "mac_prefix: 02:be:a0", self.cli("library", "config", "set", "mac_prefix=02:BE:A0")
        )
        self.assertEqual(tload(os.path.join(self.lib_dir, "config.toml"))["mac_prefix"], "02:be:a0")
        # lan_parent: named, must exist and be wired (vmidentity.parse_lan_parent's own cases: test-vmconfig.py)
        self.assertIn(
            "no such network interface",
            self.cli("library", "config", "set", "lan_parent=nosuch0", ok=False),
        )
        self.assertIn("loopback", self.cli("library", "config", "set", "lan_parent=lo", ok=False))
        wired = [
            n
            for n in sorted(os.listdir("/sys/class/net"))
            if n != "lo" and not os.path.exists(os.path.join("/sys/class/net", n, "wireless"))
        ]
        if wired:
            self.assertIn(
                f"lan_parent: {wired[0]}",
                self.cli("library", "config", "set", f"lan_parent={wired[0]}"),
            )
            self.assertEqual(
                tload(os.path.join(self.lib_dir, "config.toml"))["lan_parent"], wired[0]
            )
            self.assertNotIn("lan_parent", self.cli("library", "config", "unset", "lan_parent"))
            self.assertNotIn("lan_parent", tload(os.path.join(self.lib_dir, "config.toml")))
        # addresses: the first three octets of the library's VM addresses (default 127.0.1)
        self.assertIn("addresses: 127.0.1", out)
        for bad in ("10.0.2", "127.0.0", "127.0", "127.0.2.1", "127.0.256", "127.x.2"):
            self.assertIn(
                "addresses", self.cli("library", "config", "set", f"addresses={bad}", ok=False)
            )
        slot = self.vm_toml("tv1")["slot"]
        self.assertIn(
            "addresses: 127.0.2", self.cli("library", "config", "set", "addresses=127.000.002")
        )
        self.assertEqual(self.listing()["tv1"]["address"], f"127.0.2.{slot}")
        st = self.info("tv1")
        self.assertEqual(st["address"], f"127.0.2.{slot}")
        dev = os.path.join(self.lib_dir, "development-0123abcd")
        os.makedirs(dev)
        self.assertEqual(
            base.addresses_of(dev), "127.0.2", "a development library takes its library's"
        )
        self.assertIn(
            "with its development libraries",
            self.cli("--library", dev, "library", "config", "set", "addresses=127.0.3", ok=False),
        )
        self.cli("library", "config", "set", "addresses=127.0.1")
        self.newvm("tv2", "--import", "--firmware", self.fw_src)  # the GUI's New VM: the same
        b = self.vm_toml("tv2")
        self.assertRegex(b["mac"], r"^02:be:a0(:[0-9a-f]{2}){3}$")
        self.assertNotEqual(a["serial"], b["serial"])
        lib = self.lib()
        macs, serials = set(), set()
        for _ in range(200):
            mac, serial = lib.new_identity()
            macs.add(mac)
            serials.add(serial)
        self.assertNotIn(b["mac"], macs)
        self.assertNotIn(b["serial"], serials)
        self.assertGreater(len(macs), 190, "random suffixes")
        # set: validated and normalised
        self.cli("config", "set", "tv1", "mac=02:AA:BB:CC:DD:EE", "serial=123abcd45678")
        a = self.vm_toml("tv1")
        self.assertEqual((a["mac"], a["serial"]), ("02:aa:bb:cc:dd:ee", "123ABCD45678"))
        for bad in (
            "mac=02:aa:bb:cc:dd",
            "mac=01:00:5e:00:00:01",
            "mac=nothex",
            "serial=12 3",
            "serial=" + "9" * 17,
            "serial=ab-c",
        ):
            self.assertIn(bad.split("=")[0], self.cli("config", "set", "tv1", bad, ok=False))
        self.assertIn('"mac": "02:aa:bb:cc:dd:ee"', self.cli("show", "tv1", "--json"))
        # missing: made at the start (the launcher, vmidentity.ensure_identity), written back, with a warning
        cfg = self.vm_toml("tv2")
        del cfg["mac"], cfg["serial"]
        vm_library.vm_config.write(
            os.path.join(self.lib_dir, "vms", "tv2", "vm.toml"), cfg, tomlfile.VM_HEADER
        )
        said = []
        vm_identity.ensure_identity(os.path.join(self.lib_dir, "vms", "tv2"), said.append)
        c = self.vm_toml("tv2")
        self.assertRegex(c["mac"], r"^02:be:a0(:[0-9a-f]{2}){3}$")
        self.assertRegex(c["serial"], r"^[0-9]{3}[A-Z]{4}[0-9]{5}$")
        self.assertTrue(
            len(said) == 1 and said[0].startswith("warning:") and "NDUID" in said[0], said
        )
        said.clear()
        vm_identity.ensure_identity(os.path.join(self.lib_dir, "vms", "tv2"), said.append)
        self.assertEqual((said, self.vm_toml("tv2")), ([], c), "kept once made")
        env = vm_library.vm_launcher_env(os.path.join(self.lib_dir, "vms", "tv2", "vm.toml"))
        self.assertEqual((env["MAC"], env["SERIAL"]), (c["mac"], c["serial"]))

    def test_slots(self):
        """Each VM has a slot, its host address 127.0.1.<slot>: the lowest free one of its slot
        domain (the library and the development libraries in it) at create, never another VM's
        while that one exists; a VM made before slots gets one once, written back."""
        self.cli("create", "tv1", "--import", "--firmware", self.fw_src)
        self.newvm("tv2", "--import", "--firmware", self.fw_src)
        self.assertEqual((self.vm_toml("tv1")["slot"], self.vm_toml("tv2")["slot"]), (1, 2))
        self.assertIn("127.0.1.2", self.cli("list"))
        self.assertEqual(self.listing()["tv2"]["address"], "127.0.1.2")
        self.assertIn("slot = 1", slurp(os.path.join(self.lib_dir, "vms", "tv1", "vm.toml")))
        self.cli("delete", "tv1")
        self.cli("create", "tv3", "--import", "--firmware", self.fw_src)
        self.assertEqual(self.vm_toml("tv3")["slot"], 1, "the lowest free one")
        # a development VM in the library's domain holds its slot too
        dev = os.path.join(self.lib_dir, "development-0123abcd", "vms", "dev-0")
        os.makedirs(dev)
        vm_library.vm_config.write(
            os.path.join(dev, "vm.toml"), dict(fw="/x", slot=3), tomlfile.VM_HEADER
        )
        self.cli("create", "tv4", "--import", "--firmware", self.fw_src)
        self.assertEqual(self.vm_toml("tv4")["slot"], 4)
        self.assertEqual(
            vm_identity.free_slot(os.path.dirname(os.path.dirname(dev))),
            5,
            "seen from the development library",
        )
        self.assertEqual(vm_identity.slots_taken(self.lib_dir)[3], dev)
        # not settable, and checked
        self.assertIn("KEY one of", self.cli("config", "set", "tv4", "slot=9", ok=False))
        for bad in (0, 255, True, "2"):
            with self.assertRaisesRegex(base.Error, "slot"):
                vm_settings.validate({"slot": bad})
        # conversion: a vm.toml without a slot gets the lowest free one when the library is opened
        path = os.path.join(self.lib_dir, "vms", "tv2", "vm.toml")
        text = slurp(path)
        with open(path, "w") as f:
            f.write(text.replace("slot = 2\n", ""))
        self.assertNotIn("slot", tload(path))
        self.lib()
        self.assertEqual(tload(path)["slot"], 2)
        # and at the launcher's start (vmidentity.ensure_identity), with a note
        with open(path, "w") as f:
            f.write(text.replace("slot = 2\n", ""))
        said = []
        vm_identity.ensure_identity(os.path.dirname(path), said.append)
        self.assertEqual(tload(path)["slot"], 2)
        self.assertTrue(len(said) == 1 and "slot 2" in said[0] and "127.0.1.2" in said[0], said)
        env = vm_library.vm_launcher_env(path)
        self.assertEqual(env["SLOT"], "2")
        # VMs made at once take different slots
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "beanjar",
                    "--repo",
                    self.repo,
                    "create",
                    f"p{i}",
                    "--import",
                    "--firmware",
                    self.fw_src,
                ],
                env=self.env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            for i in range(4)
        ]
        self.assertEqual([p.wait(60) for p in procs], [0] * 4)
        slots = [self.vm_toml(f"p{i}")["slot"] for i in range(4)]
        self.assertEqual(sorted(slots), [5, 6, 7, 8], slots)

    def test_keys(self):
        """Each VM has SSH keys of its own in identity/, made at create (CLI and New VM alike):
        private keys 0600, the rest 0644; root's authorized_keys with no key (none of the user's
        ~/.ssh/*.pub: identity authorized-keys, test_authorized_keys); the developer key unencrypted PEM; host keys in Dropbear's format;
        known_hosts for 6622 and 9922 on the VM's own address. One missing is made again, with a
        warning (keys.ensure, as the launcher's start runs it). status says how to ssh in, at the
        VM's address, and the ares profile that reaches it."""
        self.cli("create", "tv1", "--import", "--firmware", self.fw_src)
        self.newvm("tv2", "--import", "--firmware", self.fw_src)
        ident = {n: os.path.join(self.lib_dir, "vms", n, "identity") for n in ("tv1", "tv2")}
        addrs = {"tv1": "127.0.1.1", "tv2": "127.0.1.2"}  # each VM's pins its own

        def read(n, f):
            with open(os.path.join(ident[n], f), "rb") as fh:
                return fh.read()

        for n, d in ident.items():
            self.assertEqual(sorted(os.listdir(d)), KEY_FILES, n)
            for f in KEY_FILES:
                want = (
                    0o644
                    if f.endswith(".pub") or f in ("authorized_keys", "known_hosts")
                    else 0o600
                )
                self.assertEqual(os.stat(os.path.join(d, f)).st_mode & 0o777, want, f"{n}/{f}")
            auth = [
                line
                for line in read(n, "authorized_keys").decode().splitlines()
                if not line.startswith("#")
            ]
            self.assertEqual(auth, [])
            self.assertTrue(read(n, "devmode_rsa").startswith(b"-----BEGIN RSA PRIVATE KEY-----\n"))
            self.assertNotIn(b"ENCRYPTED", read(n, "devmode_rsa"))
            self.assertTrue(
                read(n, "devmode_rsa.pub").startswith(b"ssh-rsa ")
                and b"developer@device" in read(n, "devmode_rsa.pub")
            )
            self.assertTrue(
                read(n, "rootssh_host_key").startswith(b"\0\0\0\x0bssh-ed25519"),
                "Dropbear's format",
            )
            self.assertTrue(
                read(n, "devmode_host_key").startswith(b"\0\0\0\x07ssh-rsa"), "Dropbear's format"
            )
            self.assertTrue(
                read(n, "id_ed25519").startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
            )
            kh = read(n, "known_hosts").decode().splitlines()
            addr = addrs[n]
            self.assertEqual(
                kh,
                [
                    f"[{addr}]:6622 {' '.join(read(n, 'rootssh_host_key.pub').decode().split()[:2])}",
                    f"[{addr}]:9922 {' '.join(read(n, 'devmode_host_key.pub').decode().split()[:2])}",
                ],
            )
            self.assertIn(f"beanjar {n}".encode(), read(n, "id_ed25519.pub"))
        for f in ("id_ed25519", "rootssh_host_key", "devmode_host_key", "devmode_rsa", "tee-huk"):
            self.assertNotEqual(read("tv1", f), read("tv2", f), f"{f}: each VM its own")
        self.assertRegex(read("tv1", "tee-huk").decode(), "^[0-9a-f]{64}\n$")
        # status: how to ssh in, for the VM's layers
        self.assertEqual(self.info("tv1")["ssh"], {})
        self.cli("config", "set", "tv1", "layers=normal,rootssh,devmode")
        st = self.info("tv1")
        self.assertEqual(st["identity"], ident["tv1"])
        self.assertIsNone(st["inspector"])  # stopped: no inspector, no password file
        self.assertEqual(
            st["ssh"]["root"],
            f"ssh -p 6622 -i {ident['tv1']}/id_ed25519 -o UserKnownHostsFile="
            f"{ident['tv1']}/known_hosts -o StrictHostKeyChecking=yes root@127.0.1.1",
        )
        self.assertIn(f"-p 9922 -i {ident['tv1']}/devmode_rsa", st["ssh"]["devmode"])
        self.assertTrue(st["ssh"]["devmode"].endswith(" prisoner@127.0.1.1"))
        self.assertEqual(st["ssh"]["devmode_key_server"], "http://127.0.1.1:9991/webos_rsa")
        self.assertEqual(st["ssh"]["ares"], "beanjar ares-TOOL -d tv1 [ARGS...]")
        self.assertEqual((st["slot"], st["address"]), (1, "127.0.1.1"))
        # Homebrew Channel's SSH is root SSH too, at the same port and with the same keys; it needs
        # developer mode (its layer.toml), and set refuses it alone
        self.assertIn(
            "layer hbchannel requires devmode",
            self.cli("config", "set", "tv1", "layers=normal,hbchannel", ok=False),
        )
        self.assertEqual(self.info("tv1")["ssh"], st["ssh"])
        self.cli("config", "set", "tv1", "layers=normal,hbchannel,devmode")
        hb = self.info("tv1")["ssh"]
        self.assertEqual(hb["root"], st["ssh"]["root"])
        self.assertIn("devmode", hb)
        # missing: made again at start, with a warning; the rest is kept
        old = {f: read("tv1", f) for f in KEY_FILES}
        for f in ("id_ed25519.pub", "devmode_rsa", "authorized_keys", "known_hosts"):
            os.unlink(os.path.join(ident["tv1"], f))
        said = []
        made = keys.ensure(ident["tv1"], warn=said.append, address=addrs["tv1"])
        self.assertEqual(made, ["id_ed25519", "devmode_rsa", "authorized_keys"])
        self.assertEqual(len(said), 3)
        self.assertTrue(all(w.startswith("warning:") for w in said), said)
        self.assertIn("ares-novacom", said[1])
        self.assertEqual(sorted(os.listdir(ident["tv1"])), KEY_FILES)
        for f in ("id_ed25519", "devmode_rsa", "devmode_rsa.pub"):
            self.assertNotEqual(read("tv1", f), old[f], f"{f}: a new target")
        for f in (
            "rootssh_host_key",
            "rootssh_host_key.pub",
            "devmode_host_key",
            "devmode_host_key.pub",
            "known_hosts",
        ):
            self.assertEqual(read("tv1", f), old[f], f"{f}: kept")
        self.assertEqual(os.stat(os.path.join(ident["tv1"], "id_ed25519")).st_mode & 0o777, 0o600)
        self.assertEqual(keys.ensure(ident["tv1"], warn=said.append), [], "nothing more to make")

    def test_authorized_keys(self):
        """beanjar identity authorized-keys show|reset|add NAME: what the guest gets, each key with
        whose it is. A new VM has only its own client key; add --mine adds the user's ~/.ssh/*.pub (certificates and
        private keys left out) without their comments; reset takes them out again, as for a VM
        made when their keys went in by default."""
        self.cli("create", "tv1", "--import", "--firmware", self.fw_src)
        ident = os.path.join(self.lib_dir, "vms", "tv1", "identity")
        path = os.path.join(ident, "authorized_keys")

        def show(verb="show", *more):
            return json.loads(self.cli("identity", "authorized-keys", verb, "tv1", *more, "--json"))

        def whose(r):
            return [(k["type"], k["whose"]) for k in r["keys"]]

        self.assertEqual(whose(show()), [("ssh-ed25519", "vm")])
        self.assertEqual(show()["path"], path)
        self.assertEqual(whose(show("add", "--mine")), [
            ("ssh-ed25519", "mine"), ("ssh-rsa", "mine"), ("ssh-ed25519", "vm")
        ])  # fmt: skip
        self.assertEqual(whose(show("add", "--mine")), whose(show()), "each key once")
        with open(path) as f:
            text = f.read()
        self.assertNotIn("me@pc", text)
        self.assertIn("ssh-rsa AAAArsa\n", text)
        self.assertNotIn("AAAAcert", text)
        self.assertNotIn("PRIVATE", text)
        self.assertEqual(whose(show("reset")), [("ssh-ed25519", "vm")])
        with open(path) as f:
            self.assertEqual(f.read(), keys.AUTHORIZED_HEADER)
        # a VM made when the user's keys went in by default, comments and all
        with open(path, "a") as f:
            f.write("ssh-rsa AAAArsa me@pc\nssh-ed25519 AAAAother someone@else\n")
        self.assertEqual(
            whose(show()), [("ssh-rsa", "mine"), ("ssh-ed25519", "other"), ("ssh-ed25519", "vm")]
        )
        guest = keys.guest_authorized(ident)
        self.assertNotIn("@", guest, "no key's comment goes into the guest")
        self.assertIn("beanjar tv1", slurp(os.path.join(ident, "id_ed25519.pub")))
        self.assertIn(
            "reset: 2 key(s) removed", self.cli("identity", "authorized-keys", "reset", "tv1")
        )
        self.assertEqual(whose(show()), [("ssh-ed25519", "vm")])
        # a line that is not a key is refused rather than handed over as it is
        with open(path, "a") as f:
            f.write("me@pc\n")
        self.assertIn(
            "not a public key line",
            self.cli("identity", "authorized-keys", "show", "tv1", ok=False),
        )

    def test_ca(self):
        """The mode=mitm proxy's CA is the library's, <library>/ca (0700; ca.key 0600), made with its
        first VM (CLI and New VM alike) and shared by the others; no VM's identity/ has one. Lost, it
        is made again at the next create, with a warning."""
        self.cli("create", "tv1", "--import", "--firmware", self.fw_src)
        ca = os.path.join(self.lib_dir, "ca")
        self.assertEqual(os.stat(ca).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(ca, "ca.key")).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.join(ca, "mitmproxy-ca.pem")).st_mode & 0o777, 0o600)
        pem = slurp(os.path.join(ca, "ca.pem"))
        self.assertTrue(pem.startswith("-----BEGIN CERTIFICATE-----"))
        dates = subprocess.run(
            [
                "openssl",
                "x509",
                "-noout",
                "-startdate",
                "-enddate",
                "-in",
                os.path.join(ca, "ca.pem"),
            ],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.split()
        self.assertEqual(dates[0], "notBefore=Jan")  # backdated: 2000-01-01
        self.assertIn("2000", dates)
        key = slurp(os.path.join(ca, "ca.key"))
        out = self.newvm("tv2", "--import", "--firmware", self.fw_src)
        self.assertNotIn("CA", out)
        self.assertEqual(
            (slurp(os.path.join(ca, "ca.pem")), slurp(os.path.join(ca, "ca.key"))),
            (pem, key),
            "one CA for the library",
        )
        for n in ("tv1", "tv2"):
            self.assertNotIn("ca", os.listdir(os.path.join(self.lib_dir, "vms", n, "identity")))
        os.unlink(os.path.join(ca, "ca.key"))
        out = self.cli("create", "tv3", "--import", "--firmware", self.fw_src)
        self.assertIn(f"warning: no CA in {ca}; made a new one", out)
        self.assertNotEqual(slurp(os.path.join(ca, "ca.pem")), pem)
        self.assertEqual(os.stat(os.path.join(ca, "ca.key")).st_mode & 0o777, 0o600)

    def test_model_key(self):
        """A VM's model key is its own: identity/sestore.toml (0600, in identity/ of mode 0700), set
        from a file that host/core/beanjar/vm/modelkey.py accepts, cleared, shown; never printed."""
        self.cli("create", "tv1", "--import", "--firmware", self.fw_src)
        self.newvm("tv2", "--import", "--firmware", self.fw_src)
        for n in ("tv1", "tv2"):  # made at create, by the CLI and New VM alike
            d = os.path.join(self.lib_dir, "vms", n, "identity")
            self.assertEqual(
                (sorted(os.listdir(d)), os.stat(d).st_mode & 0o777), (KEY_FILES, 0o700)
            )
        key = base64.b64encode(os.urandom(32))
        src = os.path.join(self.tmp, "key")
        with open(src, "wb") as f:
            f.write(key)  # no final newline, readable by all: accepted
        os.chmod(src, 0o644)
        self.assertIn("no model key: the built-in", self.cli("key", "model", "show", "tv1"))
        out = self.cli("key", "model", "set", "tv1", src)
        self.assertIn("provisioned model key", out)
        self.assertNotIn(key.decode(), out)
        dest = os.path.join(self.lib_dir, "vms", "tv1", "identity", "sestore.toml")
        self.assertEqual(tload(dest)["items"]["1D492999"]["text"], key.decode())
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o600)
        self.assertEqual(
            sorted(os.listdir(os.path.dirname(dest))),
            sorted(KEY_FILES + ["sestore.toml"]),
            "no temporary left",
        )
        self.assertTrue(self.info("tv1")["model_key"])
        self.assertFalse(self.info("tv2")["model_key"])
        self.assertFalse(
            os.path.exists(os.path.join(self.lib_dir, "vms", "tv2", "identity", "model-secret"))
        )
        for bad in (key[:-1], key + b"x", b"!" * 44, base64.b64encode(os.urandom(31))):
            with open(src, "wb") as f:
                f.write(bad)
            out = self.cli("key", "model", "set", "tv1", src, ok=False)
            self.assertIn("model key must", out)
            self.assertNotIn(bad.decode(), out)
        self.assertEqual(
            tload(dest)["items"]["1D492999"]["text"],
            key.decode(),
            "a refused key leaves the old one",
        )
        self.assertIn(
            "No such file", self.cli("key", "model", "set", "tv1", src + "-none", ok=False)
        )
        self.assertIn(
            "unrecognized arguments", self.cli("key", "model", "unset", "tv1", src, ok=False)
        )
        self.assertIn("no model key", self.cli("key", "model", "unset", "tv1"))
        self.assertEqual(tload(dest)["items"], {})
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(dest), "model-secret")))
        self.cli("key", "model", "unset", "tv1")  # clearing none is fine
        self.assertIn("no VM named", self.cli("key", "model", "unset", "nope", ok=False))

    def test_new_identity(self):
        """new-identity makes a VM another TV: its state wiped (every disk, as wipe), a new MAC of the
        library's prefix and serial, every key pair of identity/ and its VNC password new; the model key, the user's
        authorized_keys and the library's CA kept; another VM untouched. Only while off."""
        self.cli("create", "tv1", "--import", "--firmware", self.fw_src)
        self.cli("create", "tv2", "--import", "--firmware", self.fw_src)
        self.cli("library", "config", "set", "mac_prefix=02:42:be")
        src = os.path.join(self.tmp, "key")
        with open(src, "wb") as f:
            f.write(base64.b64encode(os.urandom(32)))
        self.cli("key", "model", "set", "tv1", src)
        vm_dir = os.path.join(self.lib_dir, "vms", "tv1")
        ident = os.path.join(vm_dir, "identity")
        state = os.path.join(vm_dir, "state")
        os.makedirs(state)
        for p in STATE_PARTS:
            with open(os.path.join(state, p + ".img"), "w") as f:
                f.write("old")
        with open(os.path.join(ident, "authorized_keys"), "a") as f:
            f.write("ssh-ed25519 AAAAmine user@edit\n")
        self.cli("vnc", "password", "set", "tv1", input="throwaway-one\n")

        def files(d):
            return {n: slurp(os.path.join(d, n), "rb") for n in os.listdir(d)}

        before, cfg = files(ident), self.vm_toml("tv1")
        other = (files(os.path.join(self.lib_dir, "vms", "tv2", "identity")), self.vm_toml("tv2"))
        ca = files(os.path.join(self.lib_dir, "ca"))
        out = self.cli("identity", "reset", "tv1")
        after, new = files(ident), self.vm_toml("tv1")
        self.assertRegex(new["mac"], r"^02:42:be(:[0-9a-f]{2}){3}$")
        self.assertNotEqual(new["mac"], cfg["mac"])
        self.assertRegex(new["serial"], r"^[0-9]{3}[A-Z]{4}[0-9]{5}$")
        self.assertNotEqual(new["serial"], cfg["serial"])
        self.assertEqual(
            {k: v for k, v in new.items() if k not in ("mac", "serial")},
            {k: v for k, v in cfg.items() if k not in ("mac", "serial")},
            "the other settings kept",
        )
        self.assertIn(f"mac {new['mac']}, serial {new['serial']}", out)
        self.assertIn("model key kept", out)
        self.assertEqual(
            sorted(after), sorted(KEY_FILES + ["sestore.toml", "vnc.password"]), "no temporary left"
        )
        self.assertIn("a new VNC password", out)
        self.assertNotEqual(after["vnc.password"], b"throwaway-one\n", "a new VNC password")
        self.assertRegex(after["vnc.password"].decode(), r"^[2-9a-km-np-z]{20}\n$")
        self.assertEqual(os.stat(os.path.join(ident, "vnc.password")).st_mode & 0o777, 0o600)
        for n in ("id_ed25519", "rootssh_host_key", "devmode_host_key", "devmode_rsa"):
            self.assertNotEqual(after[n], before[n], n)
            self.assertNotEqual(after[n + ".pub"], before[n + ".pub"], n + ".pub")
            self.assertEqual(os.stat(os.path.join(ident, n)).st_mode & 0o777, 0o600, n)
        self.assertTrue(
            after["rootssh_host_key"].startswith(b"\0\0\0\x0bssh-ed25519"), "Dropbear's format"
        )
        self.assertTrue(after["devmode_rsa"].startswith(b"-----BEGIN RSA PRIVATE KEY-----\n"))
        self.assertIn(b"beanjar tv1", after["id_ed25519.pub"])
        self.assertIn(after["rootssh_host_key.pub"].split()[1], after["known_hosts"])
        self.assertNotIn(before["rootssh_host_key.pub"].split()[1], after["known_hosts"])
        for n in ("sestore.toml", "authorized_keys"):
            self.assertEqual(after[n], before[n], f"{n} kept")
        # a new LGUDID, of the new MAC (macadr.new_ids)
        new_ids = vm_library.identity.read_lgudid(ident)
        self.assertNotEqual(after["lgudid.toml"], before["lgudid.toml"])
        self.assertEqual(
            new_ids[0],
            str(uuid.uuid5(uuid.UUID(new_ids[1]), new["mac"].upper())),
        )
        self.assertEqual(files(os.path.join(self.lib_dir, "ca")), ca, "the library's CA kept")
        self.assertFalse(os.path.exists(state) and os.listdir(state), "every state disk wiped")
        self.assertEqual(
            (files(os.path.join(self.lib_dir, "vms", "tv2", "identity")), self.vm_toml("tv2")),
            other,
            "another VM untouched",
        )
        self.assertIn("no VM named", self.cli("identity", "reset", "nope", ok=False))
        # the DVR key is the set's own: a new identity drops it, its next start makes another
        from beanjar.vm import sestore

        sestore.ensure_dvr_key(ident)
        self.assertIn(sestore.DVR_KEY, sestore.load(ident)[1])
        self.cli("identity", "reset", "tv1")
        self.assertNotIn(sestore.DVR_KEY, sestore.load(ident)[1])
        self.assertTrue(sestore.has_model(ident), "the model key kept")

    # ---------------------------------------------------------------- clone
    def clone_source(self, name="tv1"):
        """A VM that is off with what a clone must carry: state disks (an EEPROM holding its serial),
        a model key, an edited authorized_keys, a VNC password. Returns (its folder, the key's value)."""
        self.cli("create", name, "--import", "--firmware", self.fw_src, "--layers", "normal,gl")
        vm_dir = os.path.join(self.lib_dir, "vms", name)
        src = os.path.join(self.tmp, "key")
        model_key = base64.b64encode(os.urandom(32))
        with open(src, "wb") as f:
            f.write(model_key)
        self.cli("key", "model", "set", name, src)
        os.unlink(src)
        from beanjar.vm import sestore

        sestore.ensure_dvr_key(os.path.join(vm_dir, "identity"))  # kept: the state's recordings
        state = os.path.join(vm_dir, "state")
        os.makedirs(state)
        for p in STATE_PARTS:
            with open(os.path.join(state, p + ".img"), "wb") as f:
                f.write(b"state of " + p.encode())
        serial = self.vm_toml(name)["serial"]
        with open(os.path.join(state, "eeprom.img"), "wb") as f:
            f.write(b"\xff" * 100 + serial.encode().ljust(17, b"\0") + b"\xff" * 100)
        with open(os.path.join(state, "cmn_data.img"), "r+b") as f:  # sparse, as a state disk is
            f.truncate(64 << 20)
        with open(os.path.join(vm_dir, "identity", "authorized_keys"), "a") as f:
            f.write("ssh-ed25519 AAAAmine user@edit\n")
        self.cli("vnc", "password", "set", name, input="throwaway-one\n")
        return vm_dir, model_key.decode()

    def check_clone(self, src_dir, dst_dir, out, model_key):
        def files(d):
            return {n: slurp(os.path.join(d, n), "rb") for n in os.listdir(d)}

        cfg, new = (
            vm_library.vm_config.read(os.path.join(src_dir, "vm.toml")),
            vm_library.vm_config.read(os.path.join(dst_dir, "vm.toml")),
        )
        self.assertNotEqual(new["mac"], cfg["mac"])
        self.assertNotEqual(new["serial"], cfg["serial"])
        self.assertIsNotNone(new.get("slot"), "its library gave it a slot")
        same = ("mac", "serial", "slot")
        self.assertEqual(
            {k: v for k, v in new.items() if k not in same},
            {k: v for k, v in cfg.items() if k not in same},
            "every other setting kept",
        )
        before, after = (
            files(os.path.join(src_dir, "identity")),
            files(os.path.join(dst_dir, "identity")),
        )
        for n in ("sestore.toml", "authorized_keys", "tee-huk"):
            self.assertEqual(after[n], before[n], f"{n} kept")
        for n in ("id_ed25519", "rootssh_host_key", "devmode_host_key", "devmode_rsa"):
            self.assertNotEqual(after[n], before[n], n)
            self.assertNotEqual(after[n + ".pub"], before[n + ".pub"], n + ".pub")
            self.assertEqual(os.stat(os.path.join(dst_dir, "identity", n)).st_mode & 0o777, 0o600)
        self.assertNotIn("known_hosts", after, "written at its first start, for its own address")
        self.assertNotEqual(after["vnc.password"], before["vnc.password"])
        lgudid, ns = vm_library.identity.read_lgudid(os.path.join(dst_dir, "identity"))
        self.assertNotEqual(after["lgudid.toml"], before["lgudid.toml"], "a new LGUDID")
        self.assertEqual(lgudid, str(uuid.uuid5(uuid.UUID(ns), new["mac"].upper())))
        self.assertIn(f"beanjar {os.path.basename(dst_dir)}".encode(), after["id_ed25519.pub"])
        self.assertEqual(os.stat(os.path.join(dst_dir, "identity")).st_mode & 0o777, 0o700)
        st_src, st_dst = (
            files(os.path.join(src_dir, "state")),
            files(os.path.join(dst_dir, "state")),
        )
        self.assertEqual(sorted(st_dst), sorted(st_src))
        for n in st_src:
            if n != "eeprom.img":
                self.assertEqual(st_dst[n], st_src[n], f"{n} copied, not wiped")
        self.assertEqual(
            st_dst["eeprom.img"],
            st_src["eeprom.img"].replace(
                cfg["serial"].encode().ljust(17, b"\0"), new["serial"].encode().ljust(17, b"\0")
            ),
        )
        self.assertNotIn(cfg["serial"].encode(), st_dst["eeprom.img"])
        self.assertLess(
            os.stat(os.path.join(dst_dir, "state", "cmn_data.img")).st_blocks * 512,
            1 << 20,
            "sparse",
        )
        self.assertFalse(os.path.exists(os.path.join(dst_dir, "logs")), "logs are SRC's own")
        for secret in (model_key, before["vnc.password"].decode().strip(),
                       after["vnc.password"].decode().strip()):  # fmt: skip
            self.assertNotIn(secret, out, "no secret in the output")
        for n in ("id_ed25519", "devmode_rsa"):
            self.assertNotIn(after[n].decode().splitlines()[1], out)
        self.assertEqual(self.leftovers(), [], "no scratch left")

    def test_clone(self):
        """clone makes DEST a copy of SRC as another TV: settings, state and secure-store keys kept,
        a new identity as identity reset makes one, the state not wiped, the EEPROM's serial rewritten; SRC
        untouched; a DEST that exists refused."""
        src_dir, model_key = self.clone_source()
        os.makedirs(os.path.join(src_dir, "logs"), exist_ok=True)
        with open(os.path.join(src_dir, "logs", "console.log"), "w") as f:
            f.write("old log\n")
        snapshot = {
            os.path.relpath(os.path.join(d, n), src_dir): slurp(os.path.join(d, n), "rb")
            for d, _, ns in os.walk(src_dir)
            for n in ns
        }
        p = subprocess.run(
            [sys.executable, "-m", "beanjar", "--repo", self.repo, "clone", "tv1", "tv2", "--json"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        r = json.loads(p.stdout)
        dst_dir = os.path.join(self.lib_dir, "vms", "tv2")
        self.assertEqual((r["name"], r["host"], r["source"]), ("tv2", None, "local/tv1"))
        self.assertEqual(
            (r["mac"], r["serial"]), (self.vm_toml("tv2")["mac"], self.vm_toml("tv2")["serial"])
        )
        self.check_clone(src_dir, dst_dir, p.stdout + p.stderr, model_key)
        self.assertNotEqual(self.vm_toml("tv2")["slot"], self.vm_toml("tv1")["slot"])
        self.assertEqual(
            {
                os.path.relpath(os.path.join(d, n), src_dir): slurp(os.path.join(d, n), "rb")
                for d, _, ns in os.walk(src_dir)
                for n in ns
            },
            snapshot,
            "SRC untouched",
        )
        self.assertEqual(self.cli_json("key", "model", "show", "tv2")["model_key"], True)
        self.assertIn("exists", self.cli("clone", "tv1", "tv2", ok=False))
        self.assertIn("no VM named", self.cli("clone", "nope", "tv3", ok=False))
        self.assertIn("invalid VM name", self.cli("clone", "tv1", "bad,name", ok=False))
        self.assertIn("give the copy another name", self.cli("clone", "tv1", "tv1", ok=False))

    def test_clone_refusals(self):
        """clone refuses a DEST host without SRC's in-place firmware, and an interrupted copy leaves
        no DEST and no scratch."""
        from beanjar.vm import clone

        src_dir, _ = self.clone_source()
        lib = vm_library.Library(self.lib_dir, self.repo)
        path = os.path.join(src_dir, "vm.toml")
        text = slurp(path)
        with open(path, "w") as f:
            f.write(text.replace('directory = "firmware"', 'directory = "/nonexistent/fw"'))
        with mock.patch.dict(os.environ, HOME=self.home):
            with self.assertRaisesRegex(base.Error, "has no firmware /nonexistent/fw"):
                clone.clone(lib, "tv1", "tv2", out=lambda _: None)
        with open(path, "w") as f:
            f.write(text)
        calls = []
        real = clone.pipe

        def failing(a, b):
            calls.append(a)
            if len(calls) == 2:
                raise KeyboardInterrupt("signal 15")
            return real(a, b)

        lines = []
        with mock.patch.object(clone, "pipe", failing), mock.patch.dict(os.environ, HOME=self.home):
            with self.assertRaises(KeyboardInterrupt):
                clone.clone(lib, "tv1", "tv2", out=lines.append)
        self.assertIn("cancelled: no copy was made", lines)
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "tv2")))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(vm_library.Library(self.lib_dir, self.repo).names(), ["tv1"])

    def test_clone_remote(self):
        """clone between hosts: SRC on one, DEST on another, and from this machine to a host; each host
        is a library of its own reached through its shell (remote.shell_argv) and its beanjar
        (remote.call_json), here stand-ins on this machine."""
        from beanjar.vm import clone
        from beanjar.access import remote as rem

        src_dir, model_key = self.clone_source()
        libs = {"h1": self.lib_dir, "h2": os.path.join(self.tmp, "lib-h2")}
        os.makedirs(os.path.join(libs["h2"], "vms"))
        shells = []

        def shell_argv(uri, command, control=None, tty=False):
            host = uri.removeprefix("ssh://")
            shells.append((host, command))
            return ["env", f"BEANJAR_LIBRARY={libs[host]}", "sh", "-c", command]

        def call_json(host, uri, argv, timeout=None):
            lib = vm_library.Library(
                libs[host], self.repo
            )  # opening it gives a VM without a slot one
            vm = lib.vm(argv[-1].removeprefix("local/"))
            if argv[0] == "show":
                return {"dir": vm.dir, "state": vm.state()[0], "address": vm.address()}
            return {}

        hosts = {"h1": "ssh://h1", "h2": "ssh://h2"}
        with (
            mock.patch.object(rem, "hosts", lambda: hosts),
            mock.patch.object(rem, "shell_argv", shell_argv),
            mock.patch.object(rem, "call_json", call_json),
            mock.patch.dict(os.environ, HOME=self.home),
        ):
            lines = []
            pc = vm_library.Library(os.path.join(self.tmp, "lib-pc"), self.repo)  # this machine's
            r = clone.clone(pc, "h1/tv1", "h2/tv2", out=lines.append)
            self.assertEqual(os.listdir(pc.vms_dir), [], "nothing staged is left")
            self.assertEqual((r["host"], r["source"]), ("h2", "h1/tv1"))
            self.assertEqual(r["address"], "127.0.1.1", "the first slot of h2's library")
            self.check_clone(
                src_dir, os.path.join(libs["h2"], "vms", "tv2"), "\n".join(lines), model_key
            )
            self.assertTrue({h for h, _ in shells} == {"h1", "h2"})
            with self.assertRaisesRegex(base.Error, "exists"):
                clone.clone(pc, "h1/tv1", "h2/tv2", out=lines.append)
            # this machine to a host
            here = vm_library.Library(os.path.join(self.tmp, "lib-here"), self.repo)
            libs["h1"] = here.root
            src2 = os.path.join(here.vms_dir, "tv1")
            subprocess.run(["cp", "-a", "--sparse=always", src_dir, src2], check=True)
            r = clone.clone(here, "local/tv1", "h2/tv3", out=lines.append)
            self.check_clone(
                src2, os.path.join(libs["h2"], "vms", "tv3"), "\n".join(lines), model_key
            )
            self.assertEqual(r["address"], "127.0.1.2")
            self.assertEqual(
                [n for n in os.listdir(os.path.join(libs["h2"], "vms")) if n.startswith(".")], []
            )

    # ---------------------------------------------------------------- New VM from a .epk
    # ---------------------------------------------------------------- rename
    def tree_files(self, d):
        return {
            os.path.relpath(os.path.join(r, n), d): slurp(os.path.join(r, n), "rb")
            for r, _, ns in os.walk(d)
            for n in ns
        }

    def dead_pid(self):
        p = subprocess.Popen(["true"])
        p.wait()
        return p.pid

    def test_rename(self):
        """rename keeps everything that makes the VM the same TV (vm.toml with its identity and slot,
        identity/, state/, firmware/, logs/, the thumbnail) under the new name: only the keys' public
        comments, which name the VM, and the stale run/unit change; with link lan its DHCP hostname
        is the new name's."""
        vm_dir, model_key = self.clone_source("tv1")
        self.cli("create", "tv2", "--import", "--firmware", self.fw_src)
        self.cli(
            "config",
            "set",
            "tv1",
            "link=lan",
            "mode=mitm",
            "address=192.168.1.50/24,192.168.1.1",
        )
        os.makedirs(os.path.join(vm_dir, "logs", "saved", "boot-1"))
        for rel, data in (
            ("logs/console.log", b"old console\n"),
            ("logs/saved/boot-1/messages", b"saved log\n"),
            ("thumbnail.png", b"\x89PNG thumbnail"),
            ("run/unit", b"beanjar-vm-gone-tv1\n"),
        ):
            os.makedirs(os.path.dirname(os.path.join(vm_dir, rel)), exist_ok=True)
            with open(os.path.join(vm_dir, rel), "wb") as f:
                f.write(data)
        before = self.tree_files(vm_dir)
        info = self.info("tv1")
        other = self.tree_files(os.path.join(self.lib_dir, "vms", "tv2"))
        r = self.cli_json("rename", "tv1", "tv-renamed")
        new_dir = os.path.join(self.lib_dir, "vms", "tv-renamed")
        self.assertEqual(
            r,
            {
                "name": "tv-renamed",
                "renamed_from": "tv1",
                "dir": new_dir,
                "lan_hostname": "tv-renamed",
            },
        )
        self.assertFalse(os.path.exists(vm_dir))
        self.assertEqual(self.lib().names(), ["tv-renamed", "tv2"])
        after = self.tree_files(new_dir)
        pubs = {f"identity/{n}.pub" for n in ("id_ed25519", "rootssh_host_key", "devmode_host_key")}
        self.assertEqual(
            {k: v for k, v in after.items() if k not in pubs},
            {k: v for k, v in before.items() if k not in pubs | {"run/unit"}},
            "everything kept but the public comments and run/unit",
        )
        for p in pubs:
            self.assertEqual(after[p].split()[:2], before[p].split()[:2], f"{p}: the same key")
        self.assertTrue(after["identity/id_ed25519.pub"].endswith(b" beanjar tv-renamed\n"))
        self.assertTrue(
            after["identity/rootssh_host_key.pub"].endswith(
                b" beanjar tv-renamed rootssh host key\n"
            )
        )
        self.assertEqual(after["identity/devmode_rsa.pub"], before["identity/devmode_rsa.pub"])
        new_info = self.info("tv-renamed")
        self.assertEqual(new_info["address"], info["address"], "the same slot and address")
        self.assertEqual(new_info["lan_hostname"], "tv-renamed")
        self.assertEqual(new_info["state"], "Off")
        self.assertEqual(self.cli_json("key", "model", "show", "tv-renamed")["model_key"], True)
        self.assertEqual(self.cli("vnc", "password", "show", "tv-renamed"), "throwaway-one\n")
        self.assertNotIn(model_key, json.dumps(r))
        self.assertEqual(self.tree_files(os.path.join(self.lib_dir, "vms", "tv2")), other)
        self.assertEqual(self.leftovers(), [])
        self.assertIn("its LAN hostname is tv1 from", self.cli("rename", "tv-renamed", "tv1"))
        self.assertEqual(
            self.tree_files(vm_dir)["identity/id_ed25519.pub"], before["identity/id_ed25519.pub"]
        )

    def test_rename_refusals(self):
        """rename refuses an unknown VM, an invalid or taken NEW, NEW with a host, a running VM, one
        with a front end open or its unit up, and a development VM; each leaves the VM as it was."""
        self.cli("create", "tv1", "--import", "--firmware", self.fw_src)
        self.cli("create", "tv2", "--import", "--firmware", self.fw_src)
        vm_dir = os.path.join(self.lib_dir, "vms", "tv1")
        before = self.tree_files(vm_dir)
        for args, text in (
            (("nope", "tv3"), "no VM named nope"),
            (("tv1", "tv2"), "VM tv2 exists"),
            (("tv1", "tv1"), "called tv1 already"),
            (("tv1", "_bad"), "invalid VM name"),
            (("tv1", "a b"), "invalid VM name"),
            (("tv1", "x" * 65), "invalid VM name"),
            (("tv1", "pi/tv3"), "NEW is a bare name"),
        ):
            with self.subTest(args):
                self.assertIn(text, self.cli("rename", *args, ok=False))
        self.assertEqual(self.cli_json("rename", "tv1", "tv2", ok=False)["key"], "name")
        os.makedirs(os.path.join(self.lib_dir, "vms", "tv3"))  # a folder, not a VM: still taken
        self.assertIn("VM tv3 exists", self.cli("rename", "tv1", "tv3", ok=False))
        os.rmdir(os.path.join(self.lib_dir, "vms", "tv3"))
        lib = self.lib()
        for attr, text in (
            ("viewer_pid", "a viewer is open for tv1 (pid 4321): beanjar viewer close tv1"),
            ("vnc_pid", "beanjar-vnc is serving tv1 (pid 4321): beanjar vnc stop tv1"),
            ("_stopper", "is being stopped"),
        ):
            with (
                self.subTest(attr),
                mock.patch.object(vm_library.VM, attr, return_value=4321),
                self.assertRaisesRegex(base.Error, re.escape(text)),
            ):
                lib.rename("tv1", "tv3")
        with (
            mock.patch.object(vmunits, "unit_show", return_value={"ActiveState": "active"}),
            self.assertRaisesRegex(base.Error, "is still active: stop it before renaming"),
        ):
            lib.rename("tv1", "tv3")
        self.assertFalse(os.path.exists(os.path.join(vm_dir, "start.lock")), "the lock released")
        self.assertEqual(self.tree_files(vm_dir), before)
        self.cli("start", "tv1")
        self.assertIn(
            "VM tv1 is running; stop it before renaming", self.cli("rename", "tv1", "tv3", ok=False)
        )
        self.cli("stop", "tv1")
        self.assertEqual(self.lib().names(), ["tv1", "tv2"])
        dev = os.path.join(self.tmp, "development-0123abcd")
        self.cli("--library", dev, "create", "dev-0123abcd", "--import", "--firmware", self.fw_src)
        self.assertIn(
            "development VM",
            self.cli("--library", dev, "rename", "dev-0123abcd", "x", ok=False),
        )

    def test_rename_interrupted(self):
        """An interrupted rename leaves one whole VM: under the old name if the folder's rename did
        not happen (the public comments put back), or the new one if it did; a lock left by a killed
        rename is stale, so a retry, a start or a list finds the VM off and usable."""
        self.cli("create", "tv1", "--import", "--firmware", self.fw_src)
        vm_dir = os.path.join(self.lib_dir, "vms", "tv1")
        new_dir = os.path.join(self.lib_dir, "vms", "tv9")
        before = self.tree_files(vm_dir)
        real = os.rename

        def interrupted(a, b, *args, **kw):
            if a == vm_dir:
                raise KeyboardInterrupt
            return real(a, b, *args, **kw)

        lib = self.lib()
        with (
            mock.patch.object(vm_library.os, "rename", interrupted),
            self.assertRaises(KeyboardInterrupt),
        ):
            lib.rename("tv1", "tv9")
        self.assertEqual(self.tree_files(vm_dir), before, "tv1 whole, its comments put back")
        self.assertFalse(os.path.exists(new_dir))
        # killed after the comments were changed, before the folder moved: a dead pid's lock
        relabelled = keys.relabel(os.path.join(vm_dir, "identity"), "tv9")
        self.assertEqual(len(relabelled), 3)
        with open(os.path.join(vm_dir, "start.lock"), "w") as f:
            json.dump({"pid": self.dead_pid()}, f)
        self.assertEqual(self.listing()["tv1"]["state"], "Off")
        with open(os.path.join(vm_dir, "start.lock"), "w") as f:
            json.dump({"pid": self.dead_pid()}, f)
        self.cli("rename", "tv1", "tv9")  # the retry
        self.assertEqual(self.lib().names(), ["tv9"])
        self.assertFalse(os.path.exists(os.path.join(new_dir, "start.lock")))
        self.assertIn(
            b"beanjar tv9", slurp(os.path.join(new_dir, "identity", "id_ed25519.pub"), "rb")
        )
        # killed after the folder moved, before its lock was released
        with open(os.path.join(new_dir, "start.lock"), "w") as f:
            json.dump({"pid": self.dead_pid()}, f)
        self.assertEqual(self.listing()["tv9"]["state"], "Off")
        self.cli("start", "tv9")
        self.assertEqual(self.listing()["tv9"]["state"], "Running")
        self.cli("stop", "tv9")

    def vm_toml(self, name):
        return tload(os.path.join(self.lib_dir, "vms", name, "vm.toml"))

    def leftovers(self):
        """Scratch left in the library: vms/.new-*."""
        return [
            os.path.join(self.lib_dir, "vms", n)
            for n in os.listdir(os.path.join(self.lib_dir, "vms"))
            if n.startswith(".")
        ]

    def test_epk(self):
        before = sorted(os.listdir(self.src))
        out = self.newvm("tv1", "--firmware", self.epk, "--mem", "1024")
        self.assertIn("saved PAK", out)  # streamed
        self.assertEqual(
            sorted(os.listdir(self.src)), before, "the source directory was written to"
        )
        d = os.path.join(self.lib_dir, "vms", "tv1", "firmware")
        paks = PAKS
        self.assertEqual(
            sorted(os.listdir(d)),
            sorted(["manifest.toml"] + paks),
            "the paks and the manifest only",
        )
        self.assertIn("firmware target: h13-afro", out)
        self.assertEqual(self.lib().vm("tv1").summary()["platform"], "webOS")
        m = tload(os.path.join(d, "manifest.toml"))
        self.assertEqual(m["sha256"], hashlib.sha256(slurp(self.epk, "rb")).hexdigest())
        self.assertEqual(
            (m["chip"], m["release"], m["epk"], m["name"], m["epk_size"], m["paks"]),
            ("h13", "afro", "fw.epk", FW, 4000, paks),
        )
        self.assertEqual(self.lib().vm("tv1").target(), H13_AFRO)
        self.assertEqual(self.vm_toml("tv1")["mem"], 1024)
        self.assertNotIn("fw", self.vm_toml("tv1"))
        self.assertEqual(self.lib().vm("tv1").firmware_dir(), d)
        self.assertEqual(self.lib().vm("tv1").firmware_name(), FW)
        self.assertIn(FW, self.cli("list"))
        self.assertEqual(
            sorted(os.listdir(self.lib_dir)),
            [".slots.lock", "ca", "ca.lock", "vms"],
            "no firmware library",
        )
        self.assertEqual(self.leftovers(), [])
        # the same .epk again: extracted again, into that VM's folder (no store shared between VMs)
        out = self.newvm("tv2", "--firmware", self.epk)
        self.assertIn("saved PAK", out)
        d2 = os.path.join(self.lib_dir, "vms", "tv2", "firmware")
        self.assertEqual(sorted(os.listdir(d2)), sorted(["manifest.toml"] + paks))
        self.assertFalse(
            os.path.samefile(os.path.join(d, "rootfs.pak"), os.path.join(d2, "rootfs.pak"))
        )
        # deleting a VM deletes its firmware; the other's is its own
        self.cli("delete", "tv1")
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "tv1")))
        self.assertTrue(all(os.path.isfile(os.path.join(d2, p)) for p in paks))
        self.assertEqual(self.leftovers(), [])
        # --epk is gone: a .epk is --firmware's
        self.assertIn("unrecognized arguments", self.newvm("tv4", "--epk", self.epk, ok=False))

    def test_epk_cli(self):
        """The CLI's create takes a .epk as --firmware, with or without --import (it is always the
        VM's own), and keeps only the paks the VM reads: epk2extract's other paks and kernel.pak's
        intermediates go with the scratch."""
        before = sorted(os.listdir(self.src))
        out = self.cli("create", "tv1", "--firmware", self.epk)
        self.assertIn("saved PAK", out)
        self.assertIn("created tv1", out)
        d = os.path.join(self.lib_dir, "vms", "tv1", "firmware")
        self.assertEqual(sorted(os.listdir(d)), sorted(["manifest.toml"] + PAKS))
        for gone in ("kernel.pak", "tzfw.pak", "lxboot.pak"):
            self.assertNotIn(gone, PAKS)  # written by the fake, not needed by an h13-afro VM
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "tv1", ".import")))
        self.assertEqual(tload(os.path.join(d, "manifest.toml"))["paks"], PAKS)
        self.assertNotIn("fw", self.vm_toml("tv1"))
        self.assertEqual(
            sorted(os.listdir(self.src)), before, "the source directory was written to"
        )
        self.assertEqual(self.leftovers(), [])
        # --import as well; a .epk named without a '/' is a path (here, in the working directory)
        p = subprocess.run(
            [
                sys.executable,
                "-m",
                "beanjar",
                "--repo",
                self.repo,
                "create",
                "tv2",
                "--import",
                "--firmware",
                "fw.epk",
            ],
            env=self.env,
            cwd=self.src,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stdout)
        d2 = os.path.join(self.lib_dir, "vms", "tv2", "firmware")
        self.assertEqual(sorted(os.listdir(d2)), sorted(["manifest.toml"] + PAKS))
        # refusals, and nothing left of them
        missing = os.path.join(self.src, "none.epk")
        self.assertIn(
            f"no file {missing}", self.cli("create", "tv3", "--firmware", missing, ok=False)
        )
        fail = dict(self.env, FAKE_EPK_FAIL="1")
        self.assertIn(
            "epk2extract failed",
            self.cli("create", "tv3", "--firmware", self.epk, env=fail, ok=False),
        )
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "tv3")))
        self.assertEqual(self.leftovers(), [])

    def test_epk_refusals(self):
        # a firmware of a target this tree does not boot is refused, not imported with its layers guessed
        for model in ("XXXX", "YYYY"):
            env = dict(self.env, FAKE_EPK_NAME=f"04.06.75.01-HE_DTV_{model}_AFAAABAA")
            out = self.newvm("a", "--firmware", self.epk, env=env, ok=False)
            self.assertIn(
                f"{model} afro firmware might be unsupported: no board definition covers it",
                out.splitlines()[-2],
            )
            self.assertEqual(out.splitlines()[-1], "beanjar-key: fw")
            self.assertEqual(os.listdir(os.path.join(self.lib_dir, "vms")), [])
        # a known model whose rootfs is of another release (the fake's is afro, W15M is m14-beehive)
        env = dict(self.env, FAKE_EPK_NAME="04.06.75.01-HE_DTV_W15M_AFAAABAA")
        out = self.newvm("a", "--firmware", self.epk, env=env, ok=False)
        self.assertIn(
            "W15M afro firmware might be unsupported: no board definition covers it",
            out.splitlines()[-2],
        )
        self.assertEqual(os.listdir(os.path.join(self.lib_dir, "vms")), [])
        env = dict(self.env, FAKE_EPK_NAME="05.70.50.01-HE_DTV_W16R_AFAAABAA")
        out = self.newvm("a", "--firmware", self.epk, env=env, ok=False)
        self.assertIn(
            "W16R afro firmware might be unsupported: no board definition covers it",
            out.splitlines()[-2],
        )
        self.assertEqual(os.listdir(os.path.join(self.lib_dir, "vms")), [])
        # and a rootfs without a release file
        bare = os.path.join(self.tmp, "05.06.10.01-HE_DTV_WT1H_bare")
        shutil.copytree(self.fw_src, bare)
        with open(os.path.join(bare, "rootfs.pak"), "wb") as f:
            f.write(b"hsqs not a squashfs")
        out = self.cli("create", "b", "--import", "--firmware", bare, ok=False)
        self.assertIn("no etc/starfish-release in rootfs.pak: its webOS release is unknown", out)
        self.newvm("a", "--firmware", self.epk)
        env = dict(self.env, FAKE_EPK_MISSING="smartkey fonts")
        other = os.path.join(self.src, "other.epk")
        with open(other, "wb") as f:
            f.write(b"EPK2 other" * 100)
        out = self.newvm("b", "--firmware", other, env=env, ok=False)
        self.assertEqual(
            out.splitlines()[-2:],
            ["beanjar: the firmware lacks fonts.pak, smartkey.pak", "beanjar-key: fw"],
        )
        out = self.newvm("b", "--firmware", os.path.join(self.src, "none.epk"), ok=False)
        self.assertEqual(
            out.splitlines()[-2:],
            [f"beanjar: no file {os.path.join(self.src, 'none.epk')}", "beanjar-key: fw"],
        )
        self.assertIn(
            "epk2extract failed",
            self.newvm("b", "--firmware", other, env=dict(self.env, FAKE_EPK_FAIL="1"), ok=False),
        )
        self.assertIn("VM a exists", self.newvm("a", "--firmware", other, ok=False))
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib_dir, "vms"))), ["a"])
        self.assertEqual(
            tload(os.path.join(self.lib_dir, "vms", "a", "firmware", "manifest.toml"))["name"], FW
        )
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "firmware")))
        self.assertEqual(self.leftovers(), [])

    def cancel(self, args, env, wait_for_line="sleeping"):
        """Run the New VM worker with args and SIGTERM it at wait_for_line: its output."""
        p = subprocess.Popen(
            [sys.executable, "-m", "beanjar.vm.create", "--repo", self.repo, *args],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines = []
        for line in p.stdout:
            lines.append(line)
            if line.startswith(wait_for_line):
                break
        self.assertTrue(self.leftovers(), "no scratch directory while working")
        p.send_signal(signal.SIGTERM)
        rest = p.stdout.read()
        p.stdout.close()
        self.assertEqual(p.wait(timeout=10), 130, "".join(lines) + rest)
        self.assertIn("cancelled: nothing was created", rest)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(os.listdir(os.path.join(self.lib_dir, "vms")), [])
        return "".join(lines) + rest

    def test_epk_cancel(self):
        self.cancel(["tv1", "--firmware", self.epk], dict(self.env, FAKE_EPK_SLEEP="30"))
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "firmware")))
        self.assertEqual(
            self.own_processes("fake-epk2extract"), [], "epk2extract survived the cancel"
        )

    def own_processes(self, word):
        """The pids of the processes of this test (BEANJAR_LIBRARY, its temporary library, in their
        environment) with word in their command line: not those of another test run next to it."""
        mark = f"BEANJAR_LIBRARY={self.lib_dir}".encode()
        pids = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cmdline = slurp(f"/proc/{pid}/cmdline", "rb")
                environ = slurp(f"/proc/{pid}/environ", "rb")
            except OSError:
                continue
            if word.encode() in cmdline and mark in environ.split(b"\0"):
                pids.append(int(pid))
        return pids

    # ---------------------------------------------------------------- the firmware in the VM's folder
    def fw_dir(self, name, extra=False):
        """A directory of extracted paks, as epk2extract leaves one (with extra, a file that is no pak
        beside them)."""
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        if extra:
            with open(os.path.join(d, "firmware-ca.pem"), "w") as f:
                f.write("-----BEGIN CERTIFICATE-----\n" + name + "\n")
        for p in targets.pak_files(H13_AFRO):
            if p == "rootfs":
                rootfs_pak(os.path.join(d, p + ".pak"), name.encode())
                continue
            with open(os.path.join(d, p + ".pak"), "wb") as f:
                f.write(b"hsqs" + p.encode() + name.encode())
        return d

    def tree(self, d):
        """Every file under d with its inode, size and mtime: what a write would change."""
        out = {}
        for root, dirs, files in os.walk(d):
            for n in dirs + files:
                st = os.lstat(os.path.join(root, n))
                out[os.path.relpath(os.path.join(root, n), d)] = (
                    st.st_ino,
                    st.st_size,
                    st.st_mtime_ns,
                )
        return out

    def collection(self):
        """A firmware collection of this host's config.toml (firmware_dirs) with fw_src in it, as a
        shared collection has it: a manifest.toml, and no write permission."""
        coll = os.path.join(self.tmp, "collection")
        fw = os.path.join(coll, FW)
        os.makedirs(coll)
        os.rename(self.fw_dir(FW), fw)
        with open(os.path.join(fw, "manifest.toml"), "w") as f:
            f.write(
                f'name = "{FW}"\nchip = "h13"\nrelease = "afro"\npaks = {PAKS!r}\n'.replace(
                    "'", '"'
                )
            )
        path = hostconfig.global_config_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.cli("host", "config", "set", f"firmware_dirs={self.tmp}/none:{coll}")
        for d in (fw, coll):
            os.chmod(d, 0o555)
        # writable again before tmp goes (setUp's cleanup runs after this one)
        self.addCleanup(lambda: [os.chmod(d, 0o755) for d in (coll, fw) if os.path.isdir(d)])
        return fw

    def test_firmware_in_place(self):
        """create --firmware NAME|PATH: the firmware used in place, never written: vm.toml names it
        (absolute), the VM has no firmware/ of its own; a name is looked up in the host's
        collections. Without a manifest.toml it is refused (--import still takes it)."""
        fw = self.collection()
        before = self.tree(fw)
        out = self.cli("create", "a", "--firmware", FW)
        self.assertIn(f"the firmware used in place: {fw}", out)
        vm_dir = os.path.join(self.lib_dir, "vms", "a")
        self.assertEqual(tload(os.path.join(vm_dir, "vm.toml"))["fw"], fw)
        self.assertFalse(os.path.exists(os.path.join(vm_dir, "firmware")))
        self.assertEqual(self.lib().vm("a").firmware_dir(), fw)
        self.assertEqual(
            install.vm_firmware(vm_dir, fw), (fw, os.path.join(vm_dir, "firmware-cache"))
        )
        self.assertTrue(self.listing()["a"]["fw_external"])
        # a path (relative here: any value with a '/') is used as given, made absolute
        self.cli("create", "b", "--firmware", os.path.relpath(fw) + "/")
        self.assertEqual(tload(os.path.join(self.lib_dir, "vms", "b", "vm.toml"))["fw"], fw)
        self.assertEqual(self.tree(fw), before, "nothing written in the collection")
        self.assertIn(
            "no firmware nope in the host's firmware collections",
            self.cli("create", "c", "--firmware", "nope", ok=False),
        )
        # without a manifest.toml: refused in place
        out = self.cli("create", "c", "--firmware", self.fw_src, ok=False)
        self.assertIn("no manifest.toml (name, chip, release)", out)
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "c")))
        self.cli("create", "c", "--firmware", self.fw_src, "--import")  # imported as before
        self.assertTrue(os.path.isdir(os.path.join(self.lib_dir, "vms", "c", "firmware")))
        # the GUI's New VM worker takes the same
        self.newvm("d", "--firmware", FW)
        self.assertEqual(self.lib().vm("d").firmware_dir(), fw)

    def test_own_firmware(self):
        src = self.fw_dir(FW, extra=True)
        before = self.tree(src)
        self.cli("create", "a", "--import", "--firmware", src)
        own = os.path.join(self.lib_dir, "vms", "a", "firmware")
        paks = PAKS
        self.assertEqual(
            sorted(os.listdir(own)),
            sorted(["manifest.toml"] + paks),
            "the paks only, no other file",
        )
        for p in paks:
            self.assertTrue(
                os.path.samefile(os.path.join(own, p), os.path.join(src, p)), "hard links"
            )
        self.assertEqual(
            (
                tload(os.path.join(own, "manifest.toml"))["chip"],
                tload(os.path.join(own, "manifest.toml"))["release"],
            ),
            H13_AFRO,
        )
        # another VM's firmware/ is a directory like any other: linked, its manifest kept
        self.newvm("b", "--import", "--firmware", own)
        own_b = os.path.join(self.lib_dir, "vms", "b", "firmware")
        self.assertTrue(
            os.path.samefile(os.path.join(own_b, "rootfs.pak"), os.path.join(src, "rootfs.pak"))
        )
        self.assertEqual(
            (
                tload(os.path.join(own_b, "manifest.toml"))["name"],
                tload(os.path.join(own_b, "manifest.toml"))["source"],
            ),
            (FW, own),
        )
        # an explicit fw: another directory, used in place (its partition map in the VM's firmware-cache/)
        ext_name = "05.06.10.02-HE_DTV_WT1H_AFAAABAA"  # a firmware this tree boots, by its name
        ext = self.fw_dir(ext_name)
        ext_before = self.tree(ext)
        self.cli("config", "set", "a", f"fw={ext}")
        self.assertEqual(self.vm_toml("a")["fw"], ext)
        env = vm_library.vm_launcher_env(os.path.join(self.lib_dir, "vms", "a", "vm.toml"))
        self.assertEqual(
            (env["FW"], env["FW_CACHE"], env.get("FIRMWARE_CA")),
            (ext, os.path.join(self.lib_dir, "vms", "a", "firmware-cache"), None),
        )
        rows = self.listing()
        self.assertEqual(
            (rows["a"]["fw"], rows["a"]["fw_external"], rows["a"]["firmware"]),
            (ext, True, ext_name),
        )
        self.assertEqual(self.lib().vm("a").summary()["platform"], "webOS")
        self.assertIn(f"{ext_name} (in place: {ext})", self.cli("list"))
        # a directory of a firmware this tree does not boot is refused: its platform is not known
        unknown = self.fw_dir("unknown")
        self.assertIn(
            "firmware might be unsupported",
            self.cli("config", "set", "a", f"fw={unknown}", ok=False),
        )
        self.cli("start", "a")
        self.cli("stop", "a")
        self.assertIn("no rootfs.pak", self.cli("config", "set", "a", f"fw={self.tmp}", ok=False))
        self.assertIn(
            "no rootfs.pak",
            self.cli("config", "set", "a", "fw=" + FW, ok=False),
            "no firmware names",
        )
        # back to its own firmware/: fw= (or the path of its firmware/) drops the key
        self.cli("config", "unset", "a", "fw")
        self.assertNotIn("fw", self.vm_toml("a"))
        self.cli("config", "set", "a", f"fw={ext}")
        self.cli("config", "set", "a", f"fw={own}")
        self.assertNotIn("fw", self.vm_toml("a"))
        # deleting a VM deletes its firmware, and nothing it does not own
        self.cli("config", "set", "b", f"fw={ext}")
        self.cli("delete", "b")
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "b")))
        self.cli("delete", "a")
        self.assertEqual(
            (self.tree(src), self.tree(ext)), (before, ext_before), "a source was written"
        )
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "firmware")))
        self.assertEqual(self.leftovers(), [])

    def test_tags(self):
        """Tags: create --tag, tag NAME +T -T (also while it runs), list --tag (all must match),
        list, info and --json, kept in vm.toml's metadata.tags, copied by clone; invalid refused."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src, "--tag", "Scratch",
                 "--tag", "camera", "--tag", "scratch")  # fmt: skip
        self.cli("create", "b", "--import", "--firmware", self.fw_src)
        self.assertEqual(self.vm_toml("a")["tags"], ["scratch", "camera"])
        self.assertNotIn("tags", self.vm_toml("b"))
        with open(os.path.join(self.lib_dir, "vms", "a", "vm.toml")) as f:
            self.assertIn('tags = ["scratch", "camera"]', f.read())
        rows = self.listing()
        self.assertEqual((rows["a"]["tags"], rows["b"]["tags"]), (["scratch", "camera"], []))
        self.assertEqual(self.info("a")["tags"], ["scratch", "camera"])
        self.assertIn("tags: scratch,camera", self.cli("show", "a"))
        self.assertIn("scratch,camera", self.cli("list"))
        names = lambda *w: [r["name"] for r in self.cli_json("list", *w)]
        self.assertEqual(names("--tag", "scratch"), ["a"])
        self.assertEqual(names("--tag", "SCRATCH", "--tag", "camera"), ["a"])
        self.assertEqual(names("--tag", "scratch", "--tag", "keep"), [])
        self.assertIn("tagged keep", self.cli("list", "--tag", "keep"))
        # tags are a config key: shown, set (in place of the ones it has) and unset
        self.assertIn("tags: (default)", self.cli("config", "show", "b"))
        self.assertIsNone(self.cli_json("config", "show", "b")["tags"])
        self.assertEqual(
            self.cli_json("config", "set", "b", "tags=keep,Lab-2,keep")["tags"], ["keep", "lab-2"]
        )
        self.assertIn("tags: camera,keep", self.cli("config", "set", "a", "tags=camera,keep"))
        self.assertEqual(names("--tag", "keep"), ["a", "b"])
        for bad in ("-x", "a_b", "x" * 33, "a,,b"):
            with self.subTest(bad):
                self.assertIn("tag ", self.cli("config", "set", "b", f"tags={bad}", ok=False))
        self.assertIn("unset b tags", self.cli("config", "set", "b", "tags=", ok=False))
        self.assertIn("tag 'a b'", self.cli("create", "c", "--firmware", self.fw_src,
                                             "--tag", "a b", ok=False))  # fmt: skip
        self.assertIn("tag", self.cli("list", "--tag", "-x", ok=False))
        # while it runs, and the launch does not see them
        self.cli("start", "b")
        self.assertEqual(self.cli_json("config", "set", "b", "tags=lab-2")["tags"], ["lab-2"])
        self.assertEqual(self.listing()["b"]["tags"], ["lab-2"])
        self.assertIn("not off", self.cli("config", "set", "b", "tags=x", "smp=2", ok=False))
        self.assertEqual(self.listing()["b"]["tags"], ["lab-2"], "nothing changed")
        self.cli("stop", "b")
        self.assertIsNone(self.cli_json("config", "unset", "b", "tags")["tags"])
        self.assertNotIn("tags", self.vm_toml("b"))
        # clone copies them; a hand-edited vm.toml with a bad tag is refused on reading
        self.cli("clone", "a", "d")
        self.assertEqual(self.listing()["d"]["tags"], ["camera", "keep"])
        path = os.path.join(self.lib_dir, "vms", "d", "vm.toml")
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text.replace('tags = ["camera", "keep"]', 'tags = ["Camera"]'))
        self.assertIn("lower-case", self.cli("show", "d", ok=False))

    def test_reason(self):
        """Why a VM runs (start --reason, status set NAME reason=TEXT): in list, show, status show
        and --json; stop refuses it without --force; it lives in the unit's runtime directory,
        gone with the unit."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        self.assertIn("is off", self.cli("status", "set", "a", "reason=x", ok=False))
        if not self.systemd:
            # the setsid fallback has no unit to hold a reason: refused before the start
            self.assertIn("needs a user systemd", self.cli("start", "a", "--reason", "x", ok=False))
            self.assertEqual(self.listing()["a"]["state"], "Off")
            return
        long = "bisecting the camera's MJPEG path " * 4
        started = self.cli_json("start", "a", "--reason", long, "--by", "agent-7")
        reason = started["reason"]
        self.assertEqual((reason["text"], reason["by"]), (long.strip(), "agent-7"))
        self.assertTrue(reason["user"] and reason["set"])
        unit = self.listing()["a"]["unit"]
        path = os.path.join(vmunits.unit_runtime_dir(unit), vmunits.REASON_FILE)
        self.assertTrue(os.path.isfile(path), "the reason is in the unit's RuntimeDirectory")
        self.assertEqual(self.listing()["a"]["reason"], reason)
        self.assertEqual(self.info("a")["reason"], reason)
        listing = self.cli("list")
        self.assertIn("REASON", listing)
        self.assertIn("bisecting the camera's MJPEG pa\u2026", listing)
        self.assertIn("reason.text: " + long.strip(), self.cli("show", "a"))
        # stop refuses, saying why, and leaves it running
        out = self.cli("stop", "a", ok=False)
        self.assertIn("runs for a reason: " + long.strip(), out)
        self.assertIn("agent-7", out)
        self.assertIn("--force", out)
        self.assertEqual(self.listing()["a"]["state"], "Running")
        # changed, then cleared: stop needs no --force then
        status = self.cli_json("status", "show", "a")
        self.assertEqual((status["name"], status["reason"]), ("a", reason))
        # its audio, read from the guest: this one has no shell relay, so the error says so
        self.assertIn("shell relay", status["audio"]["error"])
        self.assertIsNone(status["audio"]["outputs"])
        self.assertIn("a: keep (set ", self.cli("status", "set", "a", "reason=  keep\n "))
        self.assertIsNone(self.cli_json("status", "unset", "a", "reason")["reason"])
        self.assertIn("a: no reason", self.cli("status", "show", "a"))
        self.assertIn("at most", self.cli("status", "set", "a", "reason=" + "x" * 301, ok=False))
        self.assertIn("status unset a reason", self.cli("status", "set", "a", "reason=", ok=False))
        self.assertIn(
            "KEY=VALUE with KEY one of reason", self.cli("status", "set", "a", "x=1", ok=False)
        )
        self.cli("status", "set", "a", "reason=again", "--by", "agent-8")
        self.assertEqual(self.cli_json("status", "show", "a")["reason"]["by"], "agent-8")
        out = self.cli("stop", "a", "--force")
        self.assertIn("although it runs for a reason: again", out)
        self.assertFalse(os.path.exists(path), "systemd removed it with the unit")
        self.assertIsNone(self.listing()["a"]["reason"])
        self.assertIn("is off", self.cli("status", "set", "a", "reason=x", ok=False))
        # a new start has no reason of the last one
        self.cli("start", "a")
        self.assertIsNone(self.listing()["a"]["reason"])
        self.cli("stop", "a")

    def test_qemu_user_guest_ram(self):
        """A VM whose userland resolves to qemu-user keeps mem as the TV's RAM and gets a fixed
        1536 MiB more for qemu-user's own, and a swap disk in host RAM as large: list shows MEM as
        1024+1536, info its tv_ram, guest_ram and host_swap, and the start's admission asks the
        host for the guest's RAM but not the swap, which is taken only as the guest swaps. Under
        KVM the unit's MemoryHigh allows for the swap too (its memfd is charged to QEMU's
        cgroup), and a running VM's claim on the host leaves the swap out as the admission
        does."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src, "--mem", "1024")
        rows = self.listing()
        self.assertEqual((rows["a"]["mem"], rows["a"]["guest_ram"]), (1024, 1024))
        self.cli("config", "set", "a", "userland=qemu-user")
        rows = self.listing()
        self.assertEqual((rows["a"]["mem"], rows["a"]["guest_ram"]), (1024, 2560))
        self.assertRegex(self.cli("list"), r"\b1024\+1536\b")
        info = self.info("a")
        self.assertEqual((info["mem"], info["tv_ram"], info["guest_ram"]), (1024, 1024, 2560))
        self.assertEqual(info["host_swap"], 1536)
        vm = self.lib().vm("a")
        self.assertEqual(vm.host_swap(), 1536)
        kvm = dict(vm.launch_env(), ACCEL="kvm")
        self.assertEqual(
            unit.resources(2560, kvm),
            [
                f"CPUWeight={unit.CPU_WEIGHT}",
                f"MemoryHigh={capacity.reservation_mib(2560, kvm) + 1536}M",
            ],
        )
        with (
            mock.patch.object(vm, "lock_owner", return_value=os.getpid()),
            mock.patch.object(vm, "unit_name", return_value="running"),
            mock.patch.object(vm, "unit_up", return_value=True),
            mock.patch(
                "beanjar.runtime.vmunits.systemctl",
                return_value=(0, "MemoryHigh=5368709120\nMemoryCurrent=1073741824\n"),
            ),
        ):
            self.assertEqual(capacity.claim_mib(vm), 5120 - 1536 - 1024)
        need = capacity.reservation_mib(2560, vm.launch_env())
        self.mem_available(need - 1)
        out = self.cli("start", "a", ok=False)
        self.assertIn(
            "not enough free memory to start VM a: it needs 2560 MiB (1024 for the TV and 1536 for "
            f"qemu-user's overhead) and {capacity.START_HEADROOM_MIB} MiB of headroom",
            out,
        )
        self.assertEqual(self.listing()["a"]["state"], "Off")

    def test_start_stop_two_vms(self):
        """VMs run side by side, each started once (its start.lock), as long as the host's free
        memory covers the next one's RAM and the headroom (capacity.admission)."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        self.cli("create", "b", "--import", "--firmware", self.fw_src)
        self.assertEqual(self.listing()["a"]["state"], "Off")
        out = self.cli("start", "a", "webos.foo=1")
        self.assertIn("fake-vm: start", out)
        self.assertIn("webos.foo=1", out)
        rows = self.listing()
        self.assertEqual(rows["a"]["state"], "Running")
        self.assertTrue(rows["a"]["pid"])
        lock = os.path.join(self.lib_dir, "vms", "a", "start.lock")
        self.assertTrue(base.proc_alive(jload(lock)["pid"]) or rows["a"]["pid"], "a's start lock")
        self.assertIn("already running", self.cli("start", "a", ok=False))
        # the admission: b's RAM (the model's 1 GiB) and the headroom, against MemAvailable less
        # what a, running, may still claim (its reservation less its use)
        mem = self.lib().vm("b").mem()
        need = capacity.reservation_mib(mem, self.lib().vm("b").launch_env())
        self.mem_available(need)
        out = self.cli("start", "b", ok=False)
        self.assertIn(
            f"not enough free memory to start VM b: it needs {mem} MiB and {capacity.START_HEADROOM_MIB} MiB",
            out,
        )
        self.assertIn("claimed by VMs starting or running (a ", out)
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "b", "start.lock")))
        self.assertEqual(self.listing()["b"]["state"], "Off")
        self.mem_available(
            need
            + capacity.reservation_mib(self.lib().vm("a").mem(), self.lib().vm("a").launch_env())
        )
        self.cli("start", "b")
        rows = self.listing()
        self.assertEqual((rows["a"]["state"], rows["b"]["state"]), ("Running", "Running"))
        self.assertNotEqual(rows["a"]["address"], rows["b"]["address"])
        self.cli("stop", "b")
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "b", "start.lock")))
        self.assertIn("not off", self.cli("wipe", "a", ok=False))
        self.assertIn("not off", self.cli("delete", "a", ok=False))
        self.assertIn("not off", self.cli("config", "set", "a", "mem=1", ok=False))
        self.assertIn("not off", self.cli("key", "model", "unset", "a", ok=False))
        # the library's addresses: changed only while every VM of it (and its development libraries) is off
        self.assertIn(
            "every VM of the library is off: stop a",
            self.cli("library", "config", "set", "addresses=127.0.2", ok=False),
        )
        self.assertIn(
            "addresses: 127.0.1", self.cli("library", "config", "set", "addresses=127.0.1")
        )  # no change: allowed
        mac = self.vm_toml("a")["mac"]
        self.assertIn("not off", self.cli("identity", "reset", "a", ok=False))
        self.assertIn(
            "a is running: clone copies a VM that is off", self.cli("clone", "a", "a2", ok=False)
        )
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "a2")))
        self.assertEqual(self.vm_toml("a")["mac"], mac, "a running VM keeps its identity")
        pid = rows["a"]["pid"]
        out = self.cli("stop", "a")
        # the fake guest has no micom state (its webOS has not started): no power-off signal, so
        # its disks synced and QEMU stopped (the launcher's stop), and the stop says why
        self.assertIn("a: no power-off signal (webOS is still starting); syncing its disks", out)
        self.assertIn("SAVE_LOGS=1", out)
        self.assertFalse(base.proc_alive(pid, "qemu-system"))
        self.assertEqual(self.listing()["a"]["state"], "Off")
        self.assertFalse(os.path.exists(lock))

    def run_cli(self, *args, input=None):
        """A command's (returncode, stdout, stderr), each stream on its own."""
        p = subprocess.run(
            [sys.executable, "-m", "beanjar", "--repo", self.repo, *args],
            env=self.env,
            input=input,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return p.returncode, p.stdout, p.stderr

    def test_cli_conventions(self):
        """The CLI for scripts and agents (host/core/README.md): every reporting command's --json
        stdout parses, progress stays on stderr; exit status 0, 1 for a failed operation, 2 for a
        usage error, and with --json the error is a JSON object on stderr."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        self.assertEqual(
            self.cli_json("create", "b", "--import", "--firmware", self.fw_src)["name"], "b"
        )
        self.assertEqual([r["name"] for r in self.cli_json("list")], ["a", "b"])
        info = self.info("a")
        for k in ("name", "state", "pid", "vnc", "dir", "model_key", "identity", "ssh"):
            self.assertIn(k, info)
        settings = self.cli_json("config", "show", "a")
        self.assertEqual(set(settings), {*vm_settings.SETTINGS, "tags"})
        self.assertIsNone(settings["mem"], "unset: the default")
        self.assertEqual(self.cli_json("config", "set", "a", "smp=2")["smp"], 2)
        self.assertIsNone(self.cli_json("config", "unset", "a", "listen")["listen"])
        # broadcast has an unset: the firmware's own, else the chip's default board
        self.assertIsNone(self.cli_json("config", "unset", "a", "broadcast")["broadcast"])
        # model too: the board of the broadcast standard
        self.assertIsNone(self.cli_json("config", "unset", "a", "model")["model"])
        self.assertIn("mac_prefix", self.cli_json("library", "config", "show"))
        self.assertEqual(
            self.cli_json("host", "config", "show"),
            {
                "max_running": None,
                "firmware_dirs": ["/srv/beanjar/firmware"],
                "memory_headroom_mib": capacity.START_HEADROOM_MIB,
                "runner": "auto",
            },
        )
        where = {"password_file": self.cli_json("show", "a")["vnc_password_file"]}
        self.assertEqual(
            self.cli_json("vnc", "password", "show", "a")["password"],
            slurp(where["password_file"]).strip(),
        )
        self.assertEqual(
            set(self.cli_json("vnc", "password", "reset", "a")), {"name", "password_file"}
        )
        self.assertFalse(self.cli_json("key", "model", "show", "a")["model_key"])
        self.assertEqual(self.cli_json("key", "list", "a")["name"], "a")
        self.assertEqual(self.cli_json("wipe", "a")["parts"], None)
        self.assertEqual(self.cli_json("identity", "reset", "b")["name"], "b")
        started = self.cli_json("start", "a")
        self.assertEqual((started["name"], started["state"]), ("a", "running"))
        self.assertTrue(started["pid"])
        self.assertEqual(self.cli_json("vnc", "stop", "a"), {"name": "a", "stopped": False})
        self.assertEqual(self.cli_json("viewer", "close", "a"), {"name": "a", "closed": False})
        # a failed operation: 1; text on stderr, nothing on stdout
        rc, out, err = self.run_cli("config", "set", "a", "mem=1")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("beanjar: VM a is not off", err)
        error = self.cli_json("config", "set", "a", "mem=1", ok=False)
        self.assertIn("not off", error["error"])
        self.assertNotIn("usage", error)
        self.assertEqual(self.cli_json("stop", "a"), {"name": "a", "state": "off"})
        self.assertIn("no VM named", self.cli_json("show", "nope", ok=False)["error"])
        # usage errors: 2, from argparse and from the commands' own checks alike
        for argv in (
            ["bogus"],
            ["config", "set", "a"],
            ["config", "set", "a", "fw="],
            ["config", "unset", "a", "mem"],
            ["config", "set", "a", "nokey=1"],
            ["vnc", "password", "show", "a", "F"],
            ["key", "model", "set", "a"],
        ):
            with self.subTest(argv):
                rc, out, err = self.run_cli(*argv, "--json")
                self.assertEqual((rc, out), (2, ""), err)
                self.assertTrue(json.loads(err)["usage"])
                rc, out, err = self.run_cli(*argv)
                self.assertEqual((rc, out), (2, ""), err)
        rc, out, err = self.run_cli("config", "set", "a", "fw=")
        self.assertIn("beanjar config unset a fw", err)
        self.cli("delete", "b", "--json")
        self.assertEqual([r["name"] for r in self.cli_json("list")], ["a"])

    def test_global_config(self):
        """The machine's settings ($XDG_CONFIG_HOME/beanjar/config.toml, the test's own: testunits.isolate):
        `host config` shows and sets max_running, a whole number from 1, `unset max_running` removes it;
        no library is read or made for them."""
        cfg_home = os.environ["XDG_CONFIG_HOME"]
        self.assertNotEqual(
            os.path.realpath(cfg_home), os.path.realpath(os.path.expanduser("~/.config"))
        )
        self.assertEqual(self.env["XDG_CONFIG_HOME"], cfg_home)
        path = os.path.join(cfg_home, "beanjar", "config.toml")
        self.assertEqual(hostconfig.global_config_path(), path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertIn("max_running: (not set: no limit)", self.cli("host", "config", "show"))
        self.assertFalse(os.path.exists(self.lib_dir), "host config makes no library")
        self.assertIn("max_running: 2", self.cli("host", "config", "set", "max_running=2"))
        self.assertEqual(tload(path), {"max_running": 2})
        self.assertTrue(slurp(path).startswith("# "), "no header comment")
        self.assertIn("max_running: 2", self.cli("host", "config", "show"))
        self.assertEqual(hostconfig.max_running(), 2)
        for bad in ("0", "-1", "two", "1.5", "true"):
            with self.subTest(bad):
                self.assertIn(
                    f"a whole number from 1 (beanjar host config unset max_running removes the limit), not {bad}",
                    self.cli("host", "config", "set", f"max_running={bad}", ok=False),
                )
        self.assertEqual(tload(path), {"max_running": 2}, "a refused value changes nothing")
        self.assertIn(
            "KEY one of max_running", self.cli("host", "config", "set", "repo=/x", ok=False)
        )
        self.assertIn(
            "KEY one of max_running",
            self.cli("host", "config", "set", "max_running", ok=False),
        )
        self.assertIn(
            "KEY one of repo",
            self.cli("library", "config", "set", "max_running=2", ok=False),
            "not a library setting",
        )
        self.assertIn(
            "max_running: (not set: no limit)", self.cli("host", "config", "unset", "max_running")
        )
        self.assertEqual(tload(path), {})
        self.assertIsNone(hostconfig.max_running())
        with open(path, "w") as f:
            f.write("max_running = 0\n")
        self.assertIn(
            f"{path}: max_running is how many VMs", self.cli("host", "config", "show", ok=False)
        )
        # firmware_dirs: the host's firmware collections, default /srv/beanjar/firmware
        os.unlink(path)
        self.assertIn(
            "firmware_dirs: /srv/beanjar/firmware (not set: the default)",
            self.cli("host", "config", "show"),
        )
        self.assertEqual(paths.firmware_dirs(), ["/srv/beanjar/firmware"])
        self.assertIn(
            "firmware_dirs: /a:/b/c",
            self.cli("host", "config", "set", "firmware_dirs=/a:/b/c/"),
        )
        self.assertEqual(tload(path), {"firmware_dirs": ["/a", "/b/c"]})
        self.assertEqual(paths.firmware_dirs(), ["/a", "/b/c"])
        self.assertIn(
            "not an absolute directory: rel",
            self.cli("host", "config", "set", "firmware_dirs=rel", ok=False),
        )
        self.cli("host", "config", "unset", "firmware_dirs")
        self.assertEqual(tload(path), {})

    def test_memory_headroom(self):
        """A host override governs admission, claims and KVM limits; unset restores 1 GiB."""
        path = hostconfig.global_config_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertEqual(capacity.memory_headroom_mib(), 1024)
        self.assertEqual(self.cli_json("host", "config", "show")["memory_headroom_mib"], 1024)
        hostconfig.write_global_config({"hosts": {"lab": {"uri": "ssh://example.invalid"}}})
        self.cli("host", "config", "set", "memory_headroom_mib=896")
        self.assertEqual(capacity.memory_headroom_mib(), 896)
        self.assertEqual(tload(path)["hosts"], {"lab": {"uri": "ssh://example.invalid"}})
        for bad in ("0", "-1", "true", "1.5", "lots"):
            with self.subTest(bad=bad):
                self.assertIn(
                    "memory_headroom_mib is a whole number of MiB from 1",
                    self.cli("host", "config", "set", f"memory_headroom_mib={bad}", ok=False),
                )
                self.assertEqual(capacity.memory_headroom_mib(), 896)
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        vm = self.lib().vm("a")
        for accel in ("kvm", "tcg"):
            env = {"ACCEL": accel}
            extra = 1024 if accel == "tcg" else 0
            need = vm.mem() + 896 + extra
            self.assertEqual(capacity.reservation_mib(vm.mem(), env), need)
            with (
                mock.patch.object(vm, "launch_env", return_value=env),
                mock.patch.dict(os.environ, BEANJAR_MEMINFO=self.env["BEANJAR_MEMINFO"]),
            ):
                self.mem_available(need - 1)
                self.assertIn("and 896 MiB of headroom", capacity.admission(vm))
                self.mem_available(need)
                self.assertIsNone(capacity.admission(vm))
                with (
                    mock.patch.object(vm, "lock_owner", return_value=os.getpid()),
                    mock.patch.object(vm, "unit_up", return_value=False),
                    mock.patch.object(vm, "pid", return_value=None),
                ):
                    self.assertEqual(capacity.claim_mib(vm), need)
                # Changing the host policy does not shrink an existing unit's reservation.
                with (
                    mock.patch.object(vm, "lock_owner", return_value=os.getpid()),
                    mock.patch.object(vm, "unit_name", return_value="existing"),
                    mock.patch.object(vm, "unit_up", return_value=True),
                    mock.patch(
                        "beanjar.runtime.vmunits.systemctl",
                        return_value=(0, "MemoryHigh=2147483648\nMemoryCurrent=536870912\n"),
                    ),
                ):
                    self.assertEqual(capacity.claim_mib(vm), 1536)
            props = unit.resources(vm.mem(), env)
            self.assertEqual(
                props,
                [f"CPUWeight={unit.CPU_WEIGHT}"]
                + ([f"MemoryHigh={need}M"] if accel == "kvm" else []),
            )
        self.cli("host", "config", "unset", "memory_headroom_mib")
        self.assertEqual(capacity.memory_headroom_mib(), 1024)
        self.assertEqual(tload(path), {"hosts": {"lab": {"uri": "ssh://example.invalid"}}})
        for bad in ("true", "1.5", "0"):
            with open(path, "w") as f:
                f.write(f"memory_headroom_mib = {bad}\n")
            self.assertIn(
                f"{path}: memory_headroom_mib",
                self.cli("host", "config", "show", ok=False),
            )

    def test_max_running(self):
        """The machine's max_running: a start beyond it is refused before anything is launched, naming
        the VMs that run and the limit, and how to raise it; a slot freed by a stop lets it start. A
        VM that runs is not counted against its own restart (core.running_refusal)."""
        if not self.systemd:
            self.skipTest("the count is of the units in beanjar.slice: needs a user systemd")
        path = hostconfig.global_config_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        for n in ("a", "b", "c"):
            self.cli("create", n, "--import", "--firmware", self.fw_src)
        self.cli("host", "config", "set", "max_running=2")
        self.cli("start", "a")
        self.cli("start", "b")
        out = self.cli("start", "c", ok=False)
        self.assertIn(
            f"cannot start VM c: 2 VMs already running and the limit is 2 (max_running): "
            f"a of {self.lib_dir}, b of {self.lib_dir}. "
            "Stop one, or raise the limit: beanjar host config set max_running=3",
            out,
        )
        self.assertNotIn("fake-vm: start", out, "nothing launched")
        c = self.lib().vm("c")
        self.assertFalse(os.path.exists(os.path.join(c.dir, "start.lock")))
        self.assertFalse(os.path.exists(os.path.join(c.dir, "run")), "no run directory")
        self.assertEqual(self.listing()["c"]["state"], "Off")
        # restarting one that runs does not count it twice
        a = self.lib().vm("a")
        self.assertIsNone(vm_library.running_refusal("a", a.run_dir, vmunits.vm_unit_name(a.dir)))
        # a slot freed: c starts
        self.cli("stop", "a")
        self.cli("start", "c")
        rows = self.listing()
        self.assertEqual(
            (rows["a"]["state"], rows["b"]["state"], rows["c"]["state"]),
            ("Off", "Running", "Running"),
        )
        # the limit raised: a starts beside them; unset: no limit
        self.assertIn("max_running=3", self.cli("start", "a", ok=False))
        self.cli("host", "config", "set", "max_running=3")
        self.cli("start", "a")
        self.cli("stop", "a")
        self.cli("host", "config", "set", "max_running=1")
        self.assertIn(
            "cannot start VM a: 2 VMs already running and the limit is 1",
            self.cli("start", "a", ok=False),
        )
        self.cli("host", "config", "unset", "max_running")
        self.cli("start", "a")

    def assertSameAresAnswer(self, name, root):
        """`beanjar _ares local/NAME [--root] --json` prints what the library's reading gives
        (ares.target of Library.vm), and fails as it does."""
        code = (
            "import json, sys\n"
            "from beanjar.access import ares; from beanjar.vm import library as core\n"
            "try:\n"
            "    vm = core.Library(sys.argv[1]).vm(sys.argv[2])\n"
            "    print(json.dumps(ares.target(vm, sys.argv[3] == 'root')))\n"
            "except core.Error as e:\n"
            "    print(json.dumps({'error': str(e)}))\n"
            "    sys.exit(1)\n"
        )
        words = ["_ares", f"local/{name}", *(["--root"] if root else []), "--json"]
        got = subprocess.run(
            [sys.executable, "-m", "beanjar", "--library", self.lib_dir, *words],
            env=self.env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        library = subprocess.run(
            [sys.executable, "-c", code, self.lib_dir, name, "root" if root else ""],
            env=self.env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        self.assertEqual(
            (got.returncode, got.stdout or got.stderr),
            (library.returncode, library.stdout),
            (name, root),
        )

    def test_ares(self):
        """beanjar ares-TOOL -d NAME: the tool runs with HOME a private directory whose device
        lists (the Rust tools' and the official ones') name the VM alone, with a copy of its
        developer key; APPDATA and ARES_DEVICE unset; -d taken out, the rest passed on as it is;
        the tool's exit status is beanjar's. With --root (taken out too) the device is root on
        root SSH with the client key, in a HOME of its own."""
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        seen = os.path.join(self.tmp, "seen.json")
        fake = os.path.join(bin_dir, "ares-fake")
        with open(fake, "w") as f:
            f.write(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "home = os.environ['HOME']\n"
                "files = {}\n"
                "for root, dirs, names in os.walk(home):\n"
                "    for n in dirs + names:\n"
                "        p = os.path.join(root, n)\n"
                "        files[os.path.relpath(p, home)] = os.stat(p).st_mode & 0o777\n"
                f"json.dump({{'argv': sys.argv, 'home': home, 'files': files,"
                " 'appdata': os.environ.get('APPDATA'), 'ares_device': os.environ.get('ARES_DEVICE'),"
                f" 'other': os.environ.get('BEANJAR_OTHER')}}, open({seen!r}, 'w'))\n"
                "sys.exit(7)\n"
            )
        os.chmod(fake, 0o755)
        env = dict(
            self.env,
            PATH=bin_dir + os.pathsep + self.env["PATH"],
            APPDATA="/elsewhere",
            ARES_DEVICE="another",
            BEANJAR_OTHER="kept",
        )
        self.cli(
            "create", "tv1", "--import", "--firmware", self.fw_src, "--layers",
            "normal,devmode,rootssh",
        )  # fmt: skip
        self.cli("create", "tv2", "--import", "--firmware", self.fw_src)
        # refusals before any tool runs
        for args, said in (
            (["ares-fake", "--list"], "say which VM: beanjar ares-fake [--root] -d NAME"),
            (["ares-Fake", "-d", "tv1"], "invalid tool name"),
            (["ares-fake1", "-d", "tv1"], "invalid tool name"),
            (["ares-fake", "-d", "tv1", "-d", "tv2"], "once"),
            (["ares-fake", "-d"], "needs a VM name"),
            (["ares-fake", "-d", "nope"], "no VM named nope"),
            (["ares-fake", "-d", "tv1"], "VM tv1 is not running"),
            (["ares-fake", "-d", "tv2"], "VM tv2 has no devmode layer"),
            (["ares-fake", "--root", "-d", "tv2"], "VM tv2 has no root SSH"),
            (["ares-fake", "--root", "-d", "tv2"], "hbchannel (with devmode) or rootssh."),
            (["ares-fake", "--root", "-d", "tv2"], "config set tv2 layers=normal,gl,rootssh`"),
            (["ares-fake", "--root", "-d", "tv1"], "VM tv1 is not running"),
        ):
            with self.subTest(args):
                self.assertIn(said, self.cli(*args, ok=False, env=env))
        self.assertFalse(os.path.exists(seen))
        # the host query another machine's ares-TOOL -d HOST/NAME asks (ares.remote_vm)
        got = json.loads(self.cli("_ares", "local/tv2", "--root", "--json"))
        tv2_key = slurp(os.path.join(self.lib_dir, "vms", "tv2", "identity", "id_ed25519"))
        self.assertEqual(
            (got["running"], got["port"], got["key"], got["address"]),
            (False, None, tv2_key, "127.0.1.2"),
            "stopped, without root SSH",
        )
        self.assertIn("no VM named nope", self.cli("_ares", "local/nope", "--json", ok=False))
        # link lan with mode direct: this host cannot reach it; another machine's tool is given
        # the guest's own LAN address (run/lan.json, "" while none), root on the guest's port 22
        self.cli("config", "set", "tv2", "link=lan", "mode=direct", "lg_services=real")
        self.assertIn(
            "VM tv2 is link=lan with mode=direct: the guest is on the LAN itself",
            self.cli("ares-fake", "-d", "tv2", ok=False, env=env),
        )
        self.assertEqual(json.loads(self.cli("_ares", "local/tv2", "--json"))["lan"], "")
        run_dir = os.path.join(self.lib_dir, "vms", "tv2", "run")
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "lan.json"), "w") as f:
            json.dump({"address": "192.168.1.77", "prefix": 24, "source": "guest"}, f)
        self.assertEqual(
            json.loads(self.cli("_ares", "local/tv2", "--json"))["lan"], "192.168.1.77"
        )
        self.cli("config", "set", "tv2", "layers=normal,gl,rootssh")
        self.assertEqual(json.loads(self.cli("_ares", "local/tv2", "--root", "--json"))["port"], 22)
        self.assertSameAresAnswer("tv2", True)
        os.unlink(os.path.join(run_dir, "lan.json"))
        self.cli(
            "config", "set", "tv2", "link=nat", "mode=mitm", "lg_services=mock", "layers=normal,gl"
        )
        # the query reads one vm.toml without the library module: the library's answer and errors
        same = self.assertSameAresAnswer
        for name, root in (("tv2", False), ("tv2", True), ("tv1", True), ("nope", False)):
            same(name, root)
        key = os.path.join(self.lib_dir, "vms", "tv2", "identity", "id_ed25519")
        os.rename(key, key + ".aside")
        same("tv2", True)
        self.assertIsNone(json.loads(self.cli("_ares", "local/tv2", "--root"))["key"])
        os.rename(key + ".aside", key)
        self.cli("start", "tv1", env=env)
        self.addCleanup(self.cli, "stop", "tv1", "--unplug", ok=None)
        vm_dir = os.path.join(self.lib_dir, "vms", "tv1")
        for root, port, key in ((False, 9922, "devmode_rsa"), (True, 6622, "id_ed25519")):
            got = json.loads(self.cli("_ares", "local/tv1", *(["--root"] if root else [])))
            self.assertEqual(
                got,
                {"name": "tv1", "layers": ["normal", "devmode", "rootssh"], "running": True,
                 "address": "127.0.1.1", "port": port,
                 "key": slurp(os.path.join(vm_dir, "identity", key)), "lan": None},
            )  # fmt: skip
            same("tv1", root)
        self.assertNotIn("_ares", self.cli("--help"), "internal: not in the help")
        self.assertIn(
            "ares-nothere: not found on PATH", self.cli("ares-nothere", "-d", "tv1", ok=False)
        )
        for dev in (["-d", "tv1"], ["--device", "tv1"], ["-d=tv1"], ["--device=local/tv1"]):
            os.unlink(seen) if os.path.exists(seen) else None
            p = subprocess.run(
                [sys.executable, "-m", "beanjar", "--repo", self.repo, "ares-fake", "--list",
                 *dev, "-x", "a b", "--json", "--", "-d", "kept"],
                env=env, capture_output=True, text=True, timeout=60,
            )  # fmt: skip
            with self.subTest(dev):
                self.assertEqual(p.returncode, 7, p.stderr)
                got = jload(seen)
                self.assertEqual(
                    got["argv"][1:], ["--list", "-x", "a b", "--json", "--", "-d", "kept"]
                )
        vm_dir = os.path.join(self.lib_dir, "vms", "tv1")
        home = os.path.join(vm_dir, "run", "ares-home")
        self.assertEqual(got["home"], home)
        self.assertEqual((got["appdata"], got["ares_device"], got["other"]), (None, None, "kept"))
        self.assertEqual(
            got["files"],
            {".webos": 0o700, ".ssh": 0o700, ".webos/ose": 0o700, ".webos/tv": 0o700,
             ".webos/ose/novacom-devices.json": 0o600, ".webos/tv/novacom-devices.json": 0o600,
             ".ssh/devmode_rsa": 0o600},
        )  # fmt: skip
        self.assertEqual(os.stat(home).st_mode & 0o777, 0o700)
        self.assertEqual(
            slurp(os.path.join(home, ".ssh", "devmode_rsa"), "rb"),
            slurp(os.path.join(vm_dir, "identity", "devmode_rsa"), "rb"),
        )
        for profile in ("ose", "tv"):
            self.assertEqual(
                jload(os.path.join(home, ".webos", profile, "novacom-devices.json")),
                [
                    {
                        "profile": profile,
                        "name": "tv1",
                        "default": True,
                        "description": "Beanjar VM tv1",
                        "host": "127.0.1.1",
                        "port": 9922,
                        "username": "prisoner",
                        "privateKey": {"openSsh": "devmode_rsa"},
                        "passphrase": "",
                        "files": "sftp",
                    }
                ],
            )
        self.assertFalse(os.path.exists(os.path.join(self.home, ".webos")), "the user's untouched")
        # --root: root SSH (6622) with the client key, its own HOME; -- keeps a later --root
        p = subprocess.run(
            [sys.executable, "-m", "beanjar", "--repo", self.repo, "ares-fake", "--root",
             "-d", "tv1", "-r", "id", "--", "--root"],
            env=env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        self.assertEqual(p.returncode, 7, p.stderr)
        got = jload(seen)
        self.assertEqual(got["argv"][1:], ["-r", "id", "--", "--root"])
        root_home = os.path.join(vm_dir, "run", "ares-home-root")
        self.assertEqual(got["home"], root_home)
        self.assertEqual(
            sorted(f for f in got["files"] if f.startswith(".ssh/")), [".ssh/id_ed25519"]
        )
        self.assertEqual(
            slurp(os.path.join(root_home, ".ssh", "id_ed25519"), "rb"),
            slurp(os.path.join(vm_dir, "identity", "id_ed25519"), "rb"),
        )
        for profile in ("ose", "tv"):
            (dev,) = jload(os.path.join(root_home, ".webos", profile, "novacom-devices.json"))
            self.assertEqual(
                (dev["host"], dev["port"], dev["username"], dev["privateKey"]),
                ("127.0.1.1", 6622, "root", {"openSsh": "id_ed25519"}),
            )
            (dev,) = jload(os.path.join(home, ".webos", profile, "novacom-devices.json"))
            self.assertEqual(dev["username"], "prisoner", "the developer-mode HOME kept apart")
        # not on PATH: ~/.cargo/bin, where `cargo install` puts the Rust tools
        cargo = os.path.join(self.home, ".cargo", "bin")
        os.makedirs(cargo)
        shutil.move(fake, cargo)
        os.unlink(seen)
        self.assertEqual(self.cli("ares-fake", "-d", "tv1", "x", ok=None, env=env), "")
        self.assertEqual(jload(seen)["argv"], [os.path.join(cargo, "ares-fake"), "x"])

    def test_listen(self):
        """listen (network.listen, LISTEN_ADDRESS): where every TV-service port of the VM listens
        (VM.listeners, from layermeta.forwards), validated and normalised by set, `listen=` the
        VM's own address again. The ports are the TV's, so a start whose ports overlap a starting or
        running VM's (the same address, or 0.0.0.0 on either side) is refused, naming it, and so is
        one whose port another listener of the host holds, naming that."""
        if hostprocs.port_conflict("127.0.1.201", 9922) or hostprocs.port_conflict(
            "127.0.1.201", 3000
        ):
            self.skipTest("this host listens on 9922 or 3000 on every address")
        for n in "abc":
            self.cli(
                "create", n, "--import", "--firmware", self.fw_src, "--layers", "normal,gl,devmode"
            )
        ssap = [3000, 3001]
        self.assertEqual(
            self.lib().vm("a").listeners(), [("127.0.1.1", p) for p in ssap + [9922, 9991]]
        )
        for bad in ("lan", "::", "1.2.3", "0.0.0.0/0"):
            self.assertIn("listen", self.cli("config", "set", "a", f"listen={bad}", ok=False))
        self.cli("config", "set", "a", "listen= 127.0.1.201")
        path = os.path.join(self.lib_dir, "vms", "a", "vm.toml")
        self.assertIn('listen = "127.0.1.201"', slurp(path))
        self.assertEqual(vm_library.vm_launcher_env(path)["LISTEN_ADDRESS"], "127.0.1.201")
        st = self.info("a")
        self.assertEqual((st["listen"], st["address"]), ("127.0.1.201", "127.0.1.1"))
        self.assertTrue(st["ssh"]["devmode"].endswith("prisoner@127.0.1.201"), st["ssh"])
        self.cli("config", "set", "b", "listen=0.0.0.0")
        st = self.info("b")
        self.assertTrue(
            st["ssh"]["devmode"].endswith("prisoner@127.0.1.2"),
            "0.0.0.0: the VM's own address here",
        )
        self.assertEqual(
            self.lib().vm("b").listeners(), [("0.0.0.0", p) for p in ssap + [9922, 9991]]
        )
        self.cli("config", "set", "c", "link=off", "listen=127.0.1.201")
        self.assertEqual(self.lib().vm("c").listeners(), [], "link=off forwards nothing")
        self.cli("start", "a")
        out = self.cli("start", "b", ok=False)
        self.assertIn("cannot start VM b: VM a listens on 127.0.1.201:3000", out)
        self.assertEqual(self.listing()["b"]["state"], "Off")
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "b", "start.lock")))
        self.cli("start", "c")  # link=off: no ports, no conflict
        self.cli("stop", "c")
        self.cli("stop", "a")
        # another listener of the host
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.1.201", 9922))
            s.listen()
            out = self.cli("start", "a", ok=False)
            self.assertIn(
                "cannot start VM a: its service port 127.0.1.201:9922 (listen): port 9922 is taken",
                out,
            )
        self.cli("config", "unset", "a", "listen")
        self.assertNotIn("listen", slurp(path))
        self.assertEqual(self.lib().vm("a").listeners()[0], ("127.0.1.1", 3000))

    def test_lg_services(self):
        """lg_services (network.lg_services, NET_LG_SERVICES): real written out, mock the default
        and not written, a bad value refused."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        path = os.path.join(self.lib_dir, "vms", "a", "vm.toml")
        self.assertNotIn("lg_services", slurp(path))
        self.cli("config", "set", "a", "lg_services=real")
        self.assertIn('lg_services = "real"', slurp(path))
        self.assertEqual(vm_library.vm_launcher_env(path)["NET_LG_SERVICES"], "real")
        self.assertIn("lg_services", self.cli("config", "set", "a", "lg_services=on", ok=False))
        self.cli("config", "set", "a", "lg_services=mock")
        self.assertNotIn("lg_services", slurp(path))
        self.cli("config", "set", "a", "lg_services=real")
        self.cli("config", "unset", "a", "lg_services")
        self.assertNotIn("lg_services", slurp(path))

    def test_lan_access(self):
        """lan_access (network.lan_access, NET_LAN_ACCESS): on written out, off the default and
        not; `unset` brings off back; a link=lan VM refuses it."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        path = os.path.join(self.lib_dir, "vms", "a", "vm.toml")
        self.assertNotIn("lan_access", slurp(path))
        self.cli("config", "set", "a", "lan_access=on")
        self.assertIn('lan_access = "on"', slurp(path))
        self.assertEqual(vm_library.vm_launcher_env(path)["NET_LAN_ACCESS"], "on")
        self.assertIn("lan_access", self.cli("config", "set", "a", "lan_access=yes", ok=False))
        self.cli("config", "set", "a", "lan_access=off")
        self.assertNotIn("lan_access", slurp(path))
        self.cli("config", "set", "a", "lan_access=on")
        self.cli("config", "unset", "a", "lan_access")
        self.assertNotIn("lan_access", slurp(path))
        self.cli("config", "set", "a", "lan_access=on")
        self.assertIn(
            "lan_access is for link=nat",
            self.cli("config", "set", "a", "link=lan", ok=False),
        )

    def test_forward(self):
        """forward (network.forward, FORWARD): the VM's own guest ports, normalised by set
        and checked against its layers' and beanjar's own ports (also when the layers change), in
        listeners() and so in a start's port check; unset removes them."""
        if hostprocs.port_conflict("127.0.1.1", 8080) or hostprocs.port_conflict("127.0.1.1", 8081):
            self.skipTest("this host listens on 8080 or 8081 on every address")
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        path = os.path.join(self.lib_dir, "vms", "a", "vm.toml")
        self.assertIsNone(self.cli_json("config", "show", "a")["forward"])
        self.assertEqual(
            self.cli_json("config", "set", "a", "forward= 8080, 8081:80")["forward"],
            "8080,8081:80",
        )
        self.assertIn('forward = "8080,8081:80"', slurp(path))
        self.assertEqual(vm_library.vm_launcher_env(path)["FORWARD"], "8080,8081:80")
        self.assertEqual(
            self.lib().vm("a").listeners()[-2:], [("127.0.1.1", 8080), ("127.0.1.1", 8081)]
        )
        for bad, why in (
            ("3000", "host port 3000 is already the webos layer's"),
            ("5900", "VNC"),
            ("18083", "inspector"),
            ("80", "privileged"),
            ("8080,8080", "twice"),
        ):
            with self.subTest(bad=bad):
                self.assertIn(why, self.cli("config", "set", "a", f"forward={bad}", ok=False))
        self.assertIn('forward = "8080,8081:80"', slurp(path), "a refused value changes nothing")
        self.cli("config", "set", "a", "forward=9922")
        self.assertIn(
            "already the devmode layer's",
            self.cli("config", "set", "a", "layers=normal,gl,devmode", ok=False),
        )
        # another listener of the host on a forwarded port refuses the start, naming it
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.1.1", 9922))
            s.listen()
            self.assertIn("127.0.1.1:9922", self.cli("start", "a", ok=False))
        self.assertEqual(self.listing()["a"]["state"], "Off")
        self.cli("config", "unset", "a", "forward")
        self.assertNotIn("forward", slurp(path))
        self.assertEqual([p for _a, p in self.lib().vm("a").listeners()], [3000, 3001])
        self.assertIn("unset a forward", self.cli("config", "set", "a", "forward=", ok=False))

    def test_debug_mode_setting(self):
        """debug_mode: debug is the default and is not written out; event and release are, as
        DEBUG_MODE; unset (or debug) brings the default back; anything else is refused."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        path = os.path.join(self.lib_dir, "vms", "a", "vm.toml")
        self.assertIsNone(self.cli_json("config", "show", "a")["debug_mode"])
        self.assertEqual(
            self.cli_json("config", "set", "a", "debug_mode=release")["debug_mode"], "release"
        )
        self.assertIn('debug_mode = "release"', slurp(path))
        self.assertEqual(vm_library.vm_launcher_env(path)["DEBUG_MODE"], "release")
        self.assertEqual(self.info("a")["debug_mode"], "release")
        self.assertIn("debug_mode", self.cli("config", "set", "a", "debug_mode=quiet", ok=False))
        self.assertIn("unset", self.cli("config", "set", "a", "debug_mode=", ok=False))
        self.assertEqual(
            self.cli_json("config", "set", "a", "debug_mode=event")["debug_mode"], "event"
        )
        self.assertIsNone(self.cli_json("config", "unset", "a", "debug_mode")["debug_mode"])
        self.assertNotIn("debug_mode", slurp(path))
        self.cli("config", "set", "a", "debug_mode=release")
        self.cli("config", "set", "a", "debug_mode=debug")
        self.assertNotIn("debug_mode", slurp(path))

    def test_concurrent_starts_admitted_once(self):
        """Two starts at the same moment with memory for one: the admission lock makes the second
        count the first one's claim, so exactly one starts."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        self.cli("create", "b", "--import", "--firmware", self.fw_src)
        mem = self.lib().vm("a").mem()
        res = capacity.reservation_mib(mem, self.lib().vm("a").launch_env())
        self.mem_available(res + res // 2)
        env = dict(self.env, FAKE_BOOT_DELAY="2")
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "beanjar", "--repo", self.repo, "start", n],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for n in ("a", "b")
        ]
        outs = [p.communicate(timeout=60)[0] for p in procs]
        ok = [p.returncode == 0 for p in procs]
        self.assertEqual(sorted(ok), [False, True], outs)
        loser = outs[ok.index(False)]
        self.assertIn("not enough free memory", loser)
        self.assertIn("claimed by VMs starting or running", loser)
        rows = self.listing()
        self.assertEqual(rows["ab"[ok.index(False)]]["state"], "Off")
        self.assertIn(rows["ab"[ok.index(True)]]["state"], ("Starting", "Running"))
        self.cli("stop", "ab"[ok.index(True)])

    def test_admission_by_accelerator(self):
        """A KVM VM needs its RAM and the headroom; a TCG VM also the TB cache (qemu.TCG_TB_DEFAULT_MIB)."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        mem = self.lib().vm("a").mem()
        tb = __import__("beanjar.runtime.qemu", fromlist=["x"]).TCG_TB_DEFAULT_MIB
        for accel, need in (
            ("kvm", mem + capacity.START_HEADROOM_MIB),
            ("tcg", mem + tb + capacity.START_HEADROOM_MIB),
        ):
            path = os.path.join(self.lib_dir, "vms", "a", "vm.toml")
            with open(path) as f:
                text = f.read()
            if "[advanced.env]" in text:
                text = text[: text.index("[advanced.env]")].rstrip("\n") + "\n"
            with open(path, "w") as f:
                f.write(text + f'\n[advanced.env]\nACCEL = "{accel}"\n')
            self.assertEqual(capacity.reservation_mib(mem, self.lib().vm("a").launch_env()), need)
            self.mem_available(need - 1)
            out = self.cli("start", "a", ok=False)
            self.assertIn("not enough free memory to start VM a", out)
            self.assertEqual("translation cache" in out, accel == "tcg", out)
            self.mem_available(need)
            with mock.patch.dict(os.environ, BEANJAR_MEMINFO=self.env["BEANJAR_MEMINFO"]):
                self.assertIsNone(capacity.admission(self.lib().vm("a")))

    def test_claims(self):
        """A VM that is off claims nothing; one that runs, its reservation less what it holds."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        vm = self.lib().vm("a")
        self.assertEqual(capacity.claim_mib(vm), 0)
        self.cli("start", "a")
        claim = capacity.claim_mib(self.lib().vm("a"))
        self.assertTrue(0 < claim <= capacity.reservation_mib(vm.mem(), vm.launch_env()), claim)
        self.cli("stop", "a")
        self.assertEqual(capacity.claim_mib(self.lib().vm("a")), 0)

    def test_starting_state_and_qmp(self):
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        env = dict(self.env, FAKE_BOOT_DELAY="3", FAKE_QMP_STATUS="paused")
        self.cli("start", "a", env=env)
        row = self.listing()["a"]
        self.assertEqual((row["state"], row["detail"]), ("Starting", "paused"))
        self.assertTrue(wait_for(lambda: self.listing()["a"]["state"] == "Running"))
        self.cli("stop", "a")

    def test_failed_start_releases_lock(self):
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        open(os.path.join(self.lib_dir, "vms", "a", "fail-start"), "w").close()
        self.assertIn("did not start", self.cli("start", "a", ok=False))
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "a", "start.lock")))

    def test_stale_pidfile_and_lock(self):
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        self.cli("create", "b", "--import", "--firmware", self.fw_src)
        dead = subprocess.Popen(["true"])
        dead.wait()
        run = os.path.join(self.lib_dir, "vms", "a", "run")
        os.makedirs(run)
        for n in ("qemu.pid", "viewer.pid"):
            with open(os.path.join(run, n), "w") as f:
                f.write(f"{dead.pid}\n")
        # A live pid that is not a QEMU (ours) is stale too: the pid was reused.
        run_b = os.path.join(self.lib_dir, "vms", "b", "run")
        os.makedirs(run_b)
        with open(os.path.join(run_b, "qemu.pid"), "w") as f:
            f.write(f"{os.getpid()}\n")
        lock = os.path.join(self.lib_dir, "vms", "a", "start.lock")
        with open(lock, "w") as f:
            json.dump({"pid": dead.pid}, f)
        rows = self.listing()
        self.assertEqual((rows["a"]["state"], rows["b"]["state"]), ("Off", "Off"))
        self.assertEqual(os.listdir(run), [])
        self.assertEqual(os.listdir(run_b), [])
        self.assertFalse(os.path.exists(lock))
        with open(lock, "w") as f:
            json.dump({"pid": dead.pid}, f)
        self.cli("start", "a")
        self.assertEqual(jload(lock)["pid"] != dead.pid, True, "a stale lock is taken over")
        self.cli("stop", "a")

    def test_force_stop(self):
        """stop --unplug: QEMU killed at once, without logs or a sync."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        self.cli("start", "a")
        pid = self.listing()["a"]["pid"]
        bus = self.bus_pid("a")
        out = self.cli("stop", "a", "--unplug")
        self.assertIn("SAVE_LOGS=0", out)
        self.assertNotIn("power-off", out)
        self.assertTrue(wait_for(lambda: not base.proc_alive(pid, "qemu-system")))
        self.assertTrue(
            wait_for(lambda: not base.proc_alive(bus, "dbus-daemon")), "the private bus survived"
        )
        self.assertEqual(self.listing()["a"]["state"], "Off")
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "a", "start.lock")))
        self.assertUnitGone("a")

    def test_list_all(self):
        """list --all: the library's VMs, and every other VM up in beanjar.slice - another library's
        (a development VM: shown as its library's VM) and one of no library (a regress target's)."""
        if not self.systemd:
            self.skipTest("no user systemd")
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        other = os.path.join(self.tmp, "other")
        env = dict(self.env, BEANJAR_LIBRARY=other)
        self.cli("create", "b", "--import", "--firmware", self.fw_src, env=env)
        target = os.path.join(
            self.tmp, "instances", "h13-afro"
        )  # a regress target's RUN_DIR's parent
        fake = [sys.executable, os.path.join(TOP, "tests", "fakes", "fake-vm")]
        self.cli("start", "b", env=env)
        try:
            subprocess.run(
                [*fake, "start", "--dir", target],
                env=self.env,
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=60,
            )
            self.assertEqual([r["name"] for r in json.loads(self.cli("list", "--json"))], ["a"])
            rows = {r["name"]: r for r in json.loads(self.cli("list", "--all", "--json"))}
            self.assertLessEqual({"a", "b", "h13-afro"}, set(rows))  # and other sessions' VMs
            self.assertEqual(
                (rows["a"]["tag"], rows["a"]["state"], rows["a"]["unit"]),
                (
                    vmunits.library_tag(self.lib_dir),
                    "Off",
                    vmunits.vm_unit_name(os.path.join(self.lib_dir, "vms", "a")),
                ),
            )
            b = rows["b"]
            self.assertEqual(
                (b["library"], b["tag"], b["unit"], b["platform"]),
                (
                    os.path.abspath(other),
                    vmunits.library_tag(other),
                    testunits.PREFIX + "b",
                    "webOS 1",
                ),
            )
            self.assertTrue(base.proc_alive(b["pid"], "qemu-system"))
            p = rows["h13-afro"]
            self.assertEqual(
                (p["library"], p["tag"], p["unit"], p["run_dir"], p["state"]),
                (
                    None,
                    f"test{os.getpid()}",
                    testunits.PREFIX + "h13-afro",
                    os.path.join(target, "run"),
                    "Running",
                ),
            )
            self.assertEqual(p["pid"], base.read_pid(os.path.join(target, "run", "qemu.pid")))
            text = self.cli("list", "--all")
            self.assertTrue(text.startswith("LIBRARY "), text)
            self.assertRegex(text, rf"(?m)^test{os.getpid()} +h13-afro +Running")
        finally:
            subprocess.run(
                [*fake, "stop", "--dir", target],
                env=self.env,
                stdout=subprocess.DEVNULL,
                timeout=60,
            )
            self.cli("stop", "b", env=env)
        units = {r["unit"] for r in json.loads(self.cli("list", "--all", "--json"))}
        self.assertFalse({testunits.PREFIX + "b", testunits.PREFIX + "h13-afro"} & units)

    def test_guest_power_off(self):
        """QEMU exiting by itself (a guest power-off) takes the VM's other processes with it."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        self.cli("create", "b", "--import", "--firmware", self.fw_src)
        self.cli("start", "a")
        status = self.info("a")
        pid, bus = status["pid"], self.bus_pid("a")
        self.assertTrue(base.proc_alive(bus, "dbus-daemon"))
        if self.systemd:
            unit = status["unit"]
            self.assertEqual(
                (unit["ActiveState"], unit["Unit"]), ("active", testunits.PREFIX + "a")
            )
            self.assertTrue(base.in_unit(pid, unit["Unit"]) and base.in_unit(bus, unit["Unit"]))
        os.kill(pid, signal.SIGTERM)  # the fake exits 0, as QEMU at a power-off
        self.assertTrue(
            wait_for(lambda: not base.proc_alive(bus, "dbus-daemon")), "the private bus survived"
        )
        self.assertTrue(wait_for(lambda: self.listing()["a"]["state"] == "Off"))
        self.assertUnitGone("a")
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "a", "run", "dbus.sock")))
        self.assertIsNone(self.lib().vm("a").lock_owner(), "its start lock is stale")
        self.cli("start", "a")  # the lock went with it
        self.cli("stop", "a")

    def bus_pid(self, name):
        pid = base.read_pid(os.path.join(self.lib_dir, "vms", name, "run", "dbus-daemon.pid"))
        self.assertTrue(base.proc_alive(pid, "dbus-daemon"), "no private bus")
        return pid

    def assertUnitGone(self, name):
        if self.systemd:
            unit = testunits.PREFIX + name
            self.assertTrue(
                wait_for(lambda: vmunits.unit_show(unit).get("ActiveState") == "inactive")
            )
            self.assertNotIn(unit, vmunits.vm_units())

    # ---------------------------------------------------------------- wipe
    def test_board_data_prepared(self):
        """The board data (prepare.py, the launcher's prepare) is made with the VM, before it is
        published, again when a setting it depends on changes or the identity is renewed, and on
        request; other settings leave it. A failed preparation creates nothing."""
        calls = os.path.join(self.lib_dir, "vms", "a", "prepared.calls")

        def seen():
            with open(calls) as f:
                return f.read().split("\n")[:-1]

        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        serial = vm_library.vm_config.read(os.path.join(self.lib_dir, "vms", "a", "vm.toml"))[
            "serial"
        ]
        self.assertEqual(seen(), [f"force {serial}"])
        self.cli("config", "set", "a", "smp=2")
        self.assertEqual(len(seen()), 1)
        self.cli("config", "set", "a", "serial=NEW123")
        self.assertEqual(seen()[-1], "ensure NEW123")
        self.cli("config", "set", "a", "debug_mode=release")
        self.assertEqual(len(seen()), 3)
        self.cli("prepare", "a")
        self.assertEqual(seen()[-1], "force NEW123")
        self.cli("identity", "reset", "a")
        renewed = vm_library.vm_config.read(os.path.join(self.lib_dir, "vms", "a", "vm.toml"))[
            "serial"
        ]
        self.assertEqual(seen()[-1], f"ensure {renewed}")
        open(os.path.join(self.lib_dir, "vms", "a", "fail-prepare"), "w").close()
        self.assertIn("could not be prepared", self.cli("prepare", "a", ok=False))
        # create: refused, and nothing is left of it (the stand-in fails with the library's marker)
        open(os.path.join(self.lib_dir, "fail-prepare"), "w").close()
        self.assertIn(
            "could not be prepared",
            self.cli("create", "b", "--firmware", self.fw_src, "--import", ok=False),
        )
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib_dir, "vms"))), ["a"])

    def test_wipe(self):
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        state = os.path.join(self.lib_dir, "vms", "a", "state")
        os.makedirs(state)

        def fill():
            for p in STATE_PARTS:
                open(os.path.join(state, p + ".img"), "w").close()

        fill()
        self.cli("vnc", "password", "set", "a", input="throwaway-kept\n")
        lgudid = slurp(os.path.join(self.lib_dir, "vms", "a", "identity", "lgudid.toml"))
        self.cli("wipe", "a", "--part", "db8", "--part", "appstore")
        self.assertEqual(
            sorted(os.listdir(state)),
            sorted(p + ".img" for p in STATE_PARTS if p not in ("db8", "appstore")),
        )
        self.assertIn(
            "unknown partition bogus (one of db8, cmn_data, appstore, nvram, eeprom, partinfo, authcxt, "
            "sestore, sedata)",
            self.cli("wipe", "a", "--part", "bogus", ok=False),
        )
        self.cli("wipe", "a")
        self.assertFalse(os.path.exists(state) and os.listdir(state))
        # the state goes, the identity's VNC password and LGUDID stay
        self.assertEqual(self.cli("vnc", "password", "show", "a"), "throwaway-kept\n")
        self.assertEqual(
            slurp(os.path.join(self.lib_dir, "vms", "a", "identity", "lgudid.toml")), lgudid
        )

    # ---------------------------------------------------------------- viewer
    def test_view_unview(self):
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        self.cli("create", "h", "--import", "--firmware", self.fw_src, "--display", "egl-headless")
        self.assertIn("not running", self.cli("viewer", "open", "a", ok=False))
        self.cli("start", "a")
        out = self.cli("viewer", "open", "a", "--remote")
        vpid = int(out.split()[-1])
        self.assertTrue(base.proc_alive(vpid, "beanjar-viewer"))
        self.assertIn("already open", self.cli("viewer", "open", "a", ok=False))
        self.assertTrue(self.listing()["a"]["viewer"])
        self.assertTrue(
            wait_for(
                lambda: (
                    os.path.getsize(os.path.join(self.lib_dir, "vms", "a", "logs", "viewer.log"))
                    > 0
                )
            )
        )
        log = slurp(os.path.join(self.lib_dir, "vms", "a", "logs", "viewer.log"))
        self.assertIn(
            "unix:path=" + os.path.join(self.lib_dir, "vms", "a", "run", "dbus.sock") + " --remote",
            log,
        )
        self.assertIn("viewer closed", self.cli("viewer", "close", "a"))
        self.assertTrue(wait_for(lambda: not base.proc_alive(vpid, "beanjar-viewer")))
        self.assertIn("no viewer open", self.cli("viewer", "close", "a"))
        # stop also closes a viewer that stays
        vpid = int(self.cli("viewer", "open", "a").split()[-1])
        self.cli("stop", "a")
        self.assertTrue(wait_for(lambda: not base.proc_alive(vpid, "beanjar-viewer")))
        self.cli("start", "h")
        self.assertIn("needs dbus", self.cli("viewer", "open", "h", ok=False))
        self.cli("stop", "h")

    def test_vnc_start_stop(self):
        """beanjar-vnc on the VM's own address, never beside the viewer (both take QEMU's single-client
        chardevs), always behind the VM's own password: the owner-only identity/vnc.password, made
        random at the first start and kept across starts, vnc stop and stop; never in its argv.
        status shows where it is and the password's file. It names a listener that holds its port."""

        def free_port(address):
            with socket.socket() as s:
                s.bind((address, 0))
                return s.getsockname()[1]

        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        self.assertIn("not running", self.cli("vnc", "start", "a", ok=False))
        self.cli("start", "a")
        address = self.listing()["a"]["address"]
        self.assertRegex(address, r"^127\.0\.1\.\d+$")
        self.assertIsNone(self.info("a")["vnc"])
        run = os.path.join(self.lib_dir, "vms", "a", "run")
        password_file = os.path.join(self.lib_dir, "vms", "a", "identity", "vnc.password")
        self.assertFalse(os.path.exists(password_file), "made at the first start, not at create")
        port = free_port(address)
        # the port after the name, among beanjar-vnc's own options; a random password
        out = self.cli("vnc", "start", "a", "--port", str(port), "--fps", "15")
        pid = int(out.split()[3].rstrip(","))
        self.assertIn(f"vnc://{address}:{port}, password in {password_file}", out)
        self.assertEqual(os.stat(password_file).st_mode & 0o777, 0o600)
        first = slurp(password_file).strip()
        self.assertRegex(first, r"^[2-9a-hjkmnp-z]{20}$")
        self.assertTrue(base.proc_alive(pid, "beanjar-vnc"))
        self.assertEqual(self.listing()["a"]["vnc"], pid)
        self.assertEqual(
            self.info("a")["vnc"],
            {"pid": pid, "url": f"vnc://{address}:{port}", "password_file": password_file},
        )
        self.assertIn("already serving", self.cli("vnc", "start", "a", ok=False))
        self.assertIn("already serving", self.cli("viewer", "open", "a", ok=False))
        log = os.path.join(self.lib_dir, "vms", "a", "logs", "vnc.log")
        self.assertIn(
            "unix:path="
            + os.path.join(run, "dbus.sock")
            + f" --listen {address} --port {port} --local {run}/rfb-local.sock"
            + f" --audio-local {run}/audio-local.sock --video-local {run}/video-local.sock"
            + f" --password-file {password_file} --name a --fps 15",
            slurp(log),
        )
        self.assertNotIn(first, slurp(log))
        self.assertIn("beanjar-vnc stopped", self.cli("vnc", "stop", "a"))
        self.assertTrue(wait_for(lambda: not base.proc_alive(pid, "beanjar-vnc")))
        self.assertEqual(slurp(password_file).strip(), first, "kept after vnc stop")
        self.assertIn("no beanjar-vnc running", self.cli("vnc", "stop", "a"))
        # the default port, 5900, unless a listener holds it (the Pi desktop's wayvnc on *:5900);
        # the same password each start
        held = hostprocs.port_conflict(address, 5900)
        out = self.cli("vnc", "start", "a", ok=not held)
        if held:
            self.assertIn("is taken", out)
        else:
            self.assertIn(f"vnc://{address}:5900, password in", out)
            self.assertEqual(slurp(password_file).strip(), first)
            self.cli("vnc", "stop", "a")
        # a port another listener holds: named, and nothing started
        with socket.socket() as other:
            other.bind((address, 0))
            other.listen()
            taken = other.getsockname()[1]
            out = self.cli("vnc", "start", "a", "--port", str(taken), ok=False)
        self.assertIn(f"port {taken} is taken: ", out)
        self.assertIn(f"(pid {os.getpid()}) listens on {address}:{taken}", out)
        self.assertIn("--port N", out)
        self.assertIsNone(self.listing()["a"]["vnc"])
        # --port 0, as beanjar-web starts it: any free port, also with 5900 held; info and
        # run/vnc.json say which
        with socket.socket() as other:
            if not held:
                other.bind((address, 5900))
                other.listen()
            out = self.cli("vnc", "start", "a", "--port", "0", "--fps", "15")
        chosen = int(re.search(rf"vnc://{re.escape(address)}:(\d+), password in", out).group(1))
        self.assertNotIn(chosen, (0, 5900))
        self.assertEqual(self.info("a")["vnc"]["url"], f"vnc://{address}:{chosen}")
        self.assertEqual(json.loads(slurp(os.path.join(run, "vnc.json")))["port"], chosen)
        self.assertIn(f"--listen {address} --port {chosen} ", slurp(log))
        self.cli("vnc", "stop", "a")
        # a password set with vnc password, also off loopback; it reaches beanjar-vnc only in the file
        self.cli("vnc", "password", "set", "a", input="throwaway pass\n")
        out = self.cli("vnc", "start", "--listen", "192.0.2.1", "a", "--port", str(port))
        self.assertIn(f"vnc://192.0.2.1:{port}, password in {password_file}", out)
        self.assertEqual(slurp(password_file), "throwaway pass\n")
        vpid = self.listing()["a"]["vnc"]
        self.assertIn(
            f"--listen 192.0.2.1 --port {port} --local {run}/rfb-local.sock "
            f"--audio-local {run}/audio-local.sock --video-local {run}/video-local.sock "
            f"--password-file {password_file} --name a",
            slurp(log),
        )
        self.assertEqual(self.info("a")["vnc"]["url"], f"vnc://192.0.2.1:{port}")
        # a change while it runs: written, and beanjar-vnc keeps its own until it restarts
        out = self.cli("vnc", "password", "reset", "a")
        self.assertIn(f"beanjar-vnc (pid {vpid}) keeps the old password until it restarts", out)
        self.assertIn("takes effect at the next beanjar vnc start", out)
        self.assertTrue(base.proc_alive(vpid, "beanjar-vnc"))
        self.cli("vnc", "stop", "a")
        # an emptied file is made random again, never none
        open(password_file, "w").close()
        self.cli("vnc", "start", "a", "--port", str(port))
        self.assertRegex(slurp(password_file), r"^[2-9a-hjkmnp-z]{20}\n$")
        self.cli("vnc", "stop", "a")
        # the viewer open: no beanjar-vnc
        vpid = int(self.cli("viewer", "open", "a").split()[-1])
        self.assertIn("viewer is already open", self.cli("vnc", "start", "a", ok=False))
        self.cli("viewer", "close", "a")
        self.assertTrue(wait_for(lambda: not base.proc_alive(vpid, "beanjar-viewer")))
        # stop also ends a beanjar-vnc that stays, and keeps its password
        kept = slurp(password_file)
        pid = int(self.cli("vnc", "start", "a", "--port", str(port)).split()[3].rstrip(","))
        self.cli("stop", "a")
        self.assertTrue(wait_for(lambda: not base.proc_alive(pid, "beanjar-vnc")))
        self.assertEqual(slurp(password_file), kept)
        self.assertFalse(os.path.exists(os.path.join(run, "vnc.password")), "no copy in run/")

    def test_vnc_password(self):
        """vnc password: identity/vnc.password (0600), made random the first time it is shown and
        kept; set from stdin or a file (never argv), checked; reset; show prints it alone."""
        self.cli("create", "a", "--import", "--firmware", self.fw_src)
        ident = os.path.join(self.lib_dir, "vms", "a", "identity")
        path = os.path.join(ident, "vnc.password")
        self.assertFalse(os.path.exists(path))
        out = self.cli("show", "a")
        self.assertIn(f"vnc_password_file: {path}\n", out)
        self.assertFalse(os.path.exists(path), "show names it, makes none")
        first = self.cli("vnc", "password", "show", "a")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(slurp(path), first)
        self.assertRegex(first, r"^[2-9a-hjkmnp-z]{20}\n$")
        self.assertNotIn(first.strip(), self.cli("show", "a"), "not printed by show NAME")
        self.assertEqual(self.cli("vnc", "password", "show", "a"), first, "persistent")
        self.assertEqual(slurp(path), first)
        # set from stdin (no FILE, or -) and from a file; a CRLF line end is not part of it
        out = self.cli("vnc", "password", "set", "a", input="Throwaway 1!\n")
        self.assertIn("takes effect at the next beanjar vnc start", out)
        self.assertNotIn("Throwaway", out)
        self.assertEqual(slurp(path), "Throwaway 1!\n")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.cli("vnc", "password", "set", "a", "-", input="throwaway-2\r\n")
        self.assertEqual(self.cli("vnc", "password", "show", "a"), "throwaway-2\n")
        src = os.path.join(self.tmp, "pw")
        with open(src, "w") as f:
            f.write("throwaway-3")
        self.cli("vnc", "password", "set", "a", src)
        self.assertEqual(self.cli("vnc", "password", "show", "a"), "throwaway-3\n")
        # refused, and the password kept: empty, not one line, not printable ASCII, a space at
        # an end, longer than RSA-AES's 255 bytes
        for given, why in (
            ("", "empty"),
            ("\n", "empty"),
            ("one\ntwo\n", "one line"),
            ("caf\u00e9\n", "printable ASCII"),
            ("tab\there\n", "printable ASCII"),
            (" lead\n", "begin or end with a space"),
            ("trail \n", "begin or end with a space"),
            ("x" * 256 + "\n", "RSA-AES takes 255"),
        ):
            self.assertIn(
                why, self.cli("vnc", "password", "set", "a", input=given, ok=False), given
            )
        self.assertIn(
            "cannot read", self.cli("vnc", "password", "set", "a", src + ".none", ok=False)
        )
        self.assertEqual(slurp(path), "throwaway-3\n")
        self.cli("vnc", "password", "set", "a", input="x" * 255 + "\n")
        # reset: a new random one, printed only by show
        out = self.cli("vnc", "password", "reset", "a")
        self.assertIn("a new random VNC password in", out)
        new = slurp(path)
        self.assertRegex(new, r"^[2-9a-hjkmnp-z]{20}\n$")
        self.assertNotIn(new.strip(), out)
        self.cli("vnc", "password", "reset", "a")
        shown = self.cli("vnc", "password", "show", "a")
        self.assertRegex(shown, r"^[2-9a-hjkmnp-z]{20}\n$")
        self.assertNotEqual(shown, new)
        self.assertEqual(slurp(path), shown)
        self.assertIn(
            "unrecognized arguments",
            self.cli("vnc", "password", "reset", "a", "x", ok=False),
        )
        self.assertIn("no VM named", self.cli("vnc", "password", "show", "nope", ok=False))


class LayersTest(unittest.TestCase):
    """The layers the dialogs list (core.Library.layers, vmsettings.layer_list), for a VM's own target."""

    def test_initramfs_has_no_firmware_lists(self):
        """guest/initramfs/rootfs/init takes the paks, layers, state disks and firmware paths from boot.conf
        (targets.py) and states none of them itself."""
        init = slurp(os.path.join(TOP, "guest", "initramfs", "rootfs", "init"))
        code = "\n".join(line for line in init.splitlines() if not line.lstrip().startswith("#"))
        self.assertIn(". /host/boot.conf", code)
        # appstore now has a device-source alias for stock PDM; it is not a disk list.
        for word in (
            "tvservice:/mnt",
            "smartkey",
            "db8",
            "cmn_data",
            "PmLogDaemon.override",
            "ca-certificates.crt",
            "sdp-ca.pem",
            "board.cmdline",
            "webos soc common",
            "webos.layers",
        ):
            self.assertNotIn(word, code)

    def test_discovery_and_list(self):
        tmp = tempfile.mkdtemp(prefix="beanjar-layers-")
        self.addCleanup(shutil.rmtree, tmp)
        repo = os.path.join(tmp, "repo")
        for d in (
            "guest/layers/normal",
            "guest/layers/gl",
            "guest/layers/rootssh",
            "guest/layers/soc/h13",
            "guest/layers/webos/afro",
            "guest/layers/common",
            "build/layers/h13-afro/gl",
            "build/layers/h13-afro/tcg-perf",
            "build/layers/h13-afro/soc",
            "build/layers/m14-beehive/devmode",
        ):
            os.makedirs(os.path.join(repo, d))
        for d in (
            "guest/layers/gl",
            "guest/layers/rootssh",
            "guest/layers/common",
            "guest/layers/soc/h13",
            "guest/layers/webos/afro",
        ):
            open(os.path.join(repo, d, "meson.build"), "w").close()
        open(os.path.join(repo, "guest", "layers", "binds.conf"), "w").close()
        avail = vm_library.Library(os.path.join(tmp, "lib"), repo).layers(("h13", "afro"))
        self.assertEqual(
            {n: i["built"] for n, i in avail.items()},
            {
                "normal": True,
                "gl": True,
                "rootssh": False,
                "soc": True,
                "webos": False,
                "common": False,
                "tcg-perf": True,
            },
        )
        self.assertEqual(avail["soc"]["path"], "build/layers/h13-afro/soc")
        # another target's build is not listed; without a target only the source layers
        self.assertEqual(
            set(vm_library.Library(os.path.join(tmp, "lib"), repo).layers(("m14", "beehive"))),
            {"normal", "gl", "rootssh", "soc", "webos", "common", "devmode"},
        )
        self.assertEqual(
            {
                n: i["built"]
                for n, i in vm_library.Library(os.path.join(tmp, "lib"), repo).layers(None).items()
            },
            {
                "normal": True,
                "gl": False,
                "rootssh": False,
                "soc": False,
                "webos": False,
                "common": False,
            },
        )
        self.assertEqual(
            vm_settings.layer_list([], avail),
            [("normal", False), ("gl", False), ("rootssh", False), ("tcg-perf", False)],
        )
        self.assertEqual(
            vm_settings.layer_list(["gl", "x", "normal", "gl", "soc"], avail),
            [
                ("gl", True),
                ("x", True),
                ("normal", True),
                ("soc", True),
                ("rootssh", False),
                ("tcg-perf", False),
            ],
        )

    def test_error_keys(self):
        tmp = tempfile.mkdtemp(prefix="beanjar-layers-")
        self.addCleanup(shutil.rmtree, tmp)
        lib = vm_library.Library(os.path.join(tmp, "lib"), tmp)
        for args, key in ((("",), "name"), (("tv1",), "fw")):
            with self.assertRaises(base.Error) as e:
                lib.create(*args)
            self.assertEqual(e.exception.key, key)
        for cfg, key in (
            ({"link": "x"}, "link"),
            ({"mode": "off"}, "mode"),
            ({"mem": 0}, "mem"),
            ({"smp": True}, "smp"),
            ({"layers": ["a b"]}, "layers"),
            ({"layers": "normal,gl"}, "layers"),
            (
                {"layers": ["normal", "hbchannel", "devmode", "rootssh"]},
                "layers",
            ),  # both own guest 22
            ({"layers": ["normal", "hbchannel"]}, "layers"),  # hbchannel requires devmode
            ({"layers": ["normal", "mystery"]}, "layers"),  # no layer.toml
            ({"audio": "dbus", "display": "none"}, "audio"),
        ):
            with self.assertRaises(base.Error) as e:
                vm_settings.validate(cfg)
            self.assertEqual(e.exception.key, key)
        # a saved VM may name a layer this tree lacks (the dialogs keep it); the rules still hold
        vm_settings.validate({"layers": ["normal", "mystery"]}, saved=True)
        with self.assertRaisesRegex(base.Error, "layer hbchannel requires devmode"):
            vm_settings.validate({"layers": ["mystery", "hbchannel"]}, saved=True)
        # a new VM with hbchannel and not devmode is refused
        with self.assertRaisesRegex(base.Error, "layer hbchannel requires devmode") as e:
            lib.vm_config(layers=["normal", "hbchannel"])
        self.assertEqual(e.exception.key, "layers")


class ChildExitedTest(unittest.TestCase):
    """vnc start's liveness check (hostprocs.child_exited): by the child's exit, not its name. Started
    through taskset on big.LITTLE hosts, beanjar-vnc's pid is taskset's until the exec, and its
    cmdline reads empty mid-exec, which once made a healthy start "did not start"."""

    def test_running_child_under_another_name(self):
        p = subprocess.Popen(["sleep", "5"])
        try:
            self.assertFalse(base.proc_alive(p.pid, "beanjar-vnc"), "named otherwise")
            self.assertFalse(hostprocs.child_exited(p.pid))
        finally:
            p.kill()
        self.assertTrue(wait_for(lambda: hostprocs.child_exited(p.pid)))
        self.assertTrue(hostprocs.child_exited(p.pid), "reaped: no longer a child, and gone")


class AresStdinTest(unittest.TestCase):
    """ares-shell -r gets /dev/null as stdin when there is no stdin to forward (luna-send
    exits silently when its stdin hangs up); data or an open stdin is forwarded as before."""

    CMD = "luna-send -n 1 luna://x/y '{}'"
    DETACHED = f"exec </dev/null; {CMD}"

    def test_no_stdin_detached(self):
        from beanjar.access import ares

        with open(os.devnull) as null:
            self.assertTrue(ares.stdin_empty(null.fileno()))
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.assertFalse(ares.stdin_empty(r), "a writer is still open: data may come")
        os.write(w, b"x")
        self.assertFalse(ares.stdin_empty(r), "data queued")
        os.close(w)
        self.assertFalse(ares.stdin_empty(r), "data queued, writer gone")
        os.read(r, 1)
        self.assertTrue(ares.stdin_empty(r), "writer gone, nothing left")
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.assertFalse(ares.stdin_empty(a.fileno()))
        b.close()
        self.assertTrue(ares.stdin_empty(a.fileno()), "peer closed")
        with tempfile.TemporaryFile() as f:
            self.assertFalse(ares.stdin_empty(f.fileno()), "a file is forwarded")

    def test_args(self):
        from beanjar.access import ares

        for args, want in (
            (["-r", self.CMD], ["-r", self.DETACHED]),
            (["--run", self.CMD], ["--run", self.DETACHED]),
            ([f"--run={self.CMD}"], [f"--run={self.DETACHED}"]),
        ):
            with self.subTest(args):
                self.assertEqual(ares.detach_stdin("ares-shell", args, empty=True), want)
                self.assertEqual(ares.detach_stdin("ares-shell", args, empty=False), args)
        for tool, args in (
            ("ares-shell", []),
            ("ares-shell", ["-r"]),
            ("ares-install", ["-r", "x"]),
        ):
            with self.subTest(tool, args=args):  # the interactive shell, other tools: as given
                self.assertEqual(ares.detach_stdin(tool, args, empty=True), args)

    def test_long_command(self):
        from beanjar.access import ares
        from beanjar.base import Error

        ok = "x" * (ares.MAX_COMMAND - 1 - len("exec </dev/null; "))
        self.assertEqual(ares.detach_stdin("ares-shell", ["-r", ok], empty=True)[1][-1], "x")
        for empty in (True, False):
            with self.subTest(empty=empty), self.assertRaises(Error) as cm:
                ares.detach_stdin("ares-shell", ["-r", "x" * ares.MAX_COMMAND], empty=empty)
        self.assertIn("ares-push", str(cm.exception))
        ares.detach_stdin("ares-install", ["-r", "x" * 20000], empty=True)  # other tools: no limit


class TomlTest(unittest.TestCase):
    """The library's TOML files: tomlfile.write_toml read back by tomllib, and vm.toml as the
    launcher's environment (launch.py --dir)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-toml-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_round_trip(self):
        path = os.path.join(self.tmp, "x.toml")
        data = {
            "s": 'a "quoted" \\ back\tslash\nnew line \x01 \x7f é',
            "i": -3,
            "t": True,
            "f": False,
            "a": ["normal", "gl"],
            "e": [],
            "b-c": "",
            "key with space": "v",
            "Z_UP": "1",
        }
        tomlfile.write_toml(path, data, "line one\nline two", ("i", "s"))
        text = slurp(path)
        self.assertTrue(text.startswith("# line one\n# line two\ni = -3\ns = "))
        self.assertEqual(tload(path), data)
        with self.assertRaises(TypeError):
            tomlfile.write_toml(path, {"x": 1.5})
        with open(path, "w") as f:
            f.write("x = [1\n")
        with self.assertRaises(base.Error):
            base.read_toml(path)

    def test_launcher_env(self):
        cfg = {
            "platform": "webOS",
            "fw": "FW1",
            "mem": 2048,
            "smp": 4,
            "layers": ["normal", "gl", "rootssh"],
            "link": "nat",
            "mode": "mitm",
            "display": "dbus",
            "audio": "dbus",
            "created": "now",
            "PMLOG_DEBUG": "1",
        }
        self.assertEqual(
            vm_settings.launcher_env(cfg),
            {
                "FW": "FW1",
                "MEM": "2048",
                "SMP": "4",
                "LAYERS": "normal,gl,rootssh",
                "NET_LINK": "nat",
                "NET_MODE": "mitm",
                "DISPLAY_BACKEND": "dbus",
                "AUDIO": "dbus",
                "PMLOG_DEBUG": "1",
            },
        )
        # the uncompressed rootfs is the checkout VM's experiment, not a VM's
        for bad in (
            {"RUN_DIR": "/x"},
            {"LAYERS": "gl"},
            {"mem": True},
            {"layers": [1]},
            {"ROOTFS": "uncompressed"},
            {"UNCOMPRESSED_PAKS": "rootfs"},
            {"FW_CACHE": "/x"},
        ):
            with self.assertRaises(base.Error):
                vm_settings.launcher_env(bad)
        # as the launcher (launch.py --dir) resolves it: no fw, the VM's own firmware/ with its cache/
        # (the partition map); any other directory (absolute, or relative to the VM's folder; a bare
        # name is one too, not a lookup) in place, its map in firmware-cache/. No roots for the proxy.
        vm = os.path.join(self.tmp, "lib", "vms", "tv1")
        os.makedirs(vm)

        def launcher(vm_dir, **kw):
            c = dict(cfg, **kw)
            if c["fw"] is None:
                del c["fw"]
            vm_library.vm_config.write(os.path.join(vm_dir, "vm.toml"), c)
            env = vm_library.vm_launcher_env(os.path.join(vm_dir, "vm.toml"))
            self.assertFalse({"FIRMWARE_CA", "ROOTFS"} & set(env), env)
            return f"{env['FW']}|{env['FW_CACHE']}|{env['LAYERS']}|{env['DISPLAY_BACKEND']}"

        own = f"{vm}/firmware|{vm}/firmware/cache|normal,gl,rootssh|dbus"
        for fw in (None, "", "./firmware", "firmware", f"{vm}/firmware/"):
            self.assertEqual(launcher(vm, fw=fw), own, fw)
        self.assertEqual(
            launcher(vm, fw="/it's a dir/x"),
            f"/it's a dir/x|{vm}/firmware-cache|normal,gl,rootssh|dbus",
        )
        self.assertEqual(
            launcher(vm, fw="../../fw/x"),
            f"{self.tmp}/lib/fw/x|{vm}/firmware-cache|normal,gl,rootssh|dbus",
        )
        self.assertEqual(
            launcher(vm, fw="FW1"), f"{vm}/FW1|{vm}/firmware-cache|normal,gl,rootssh|dbus"
        )
        with self.assertRaises(base.Error):
            vm_library.vm_config.write(os.path.join(vm, "vm.toml"), dict(cfg, RUN_DIR="/x"))
            vm_library.vm_launcher_env(os.path.join(vm, "vm.toml"))
        # Platform is derived from the selected firmware and absent from the document.
        self.assertEqual(
            launcher(vm, fw="FW1"), f"{vm}/FW1|{vm}/firmware-cache|normal,gl,rootssh|dbus"
        )


class CliParseTest(unittest.TestCase):
    """The command line's shape (host/core/README.md, "Commands"; AGENTS.md's guideline): each
    form parses to its operation, the verb before the VM; the global options anywhere; a group
    without its verb is its help and exit status 2; a wrong order is an error naming the right
    form; the old names are refused as usage errors (exit status 2)."""

    def parse(self, *argv):
        from beanjar.commands import parser

        return parser.parse(list(argv))

    def run_cli(self, *argv):
        """`python3 -m beanjar ARGV` in a process of its own: (status, stdout, stderr). No VM
        is needed for what these ask: help and usage errors, a library of its own, no hosts."""
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(
                os.environ,
                PYTHONPATH=os.path.join(TOP, "host", "core"),
                XDG_CONFIG_HOME=os.path.join(tmp, "config"),
                BEANJAR_LIBRARY=os.path.join(tmp, "lib"),
            )
            p = subprocess.run(
                [sys.executable, "-m", "beanjar", *argv], env=env, capture_output=True, text=True
            )
        return p.returncode, p.stdout, p.stderr

    def test_forms(self):
        from beanjar.commands import devices, display, identity, mitm, sensors, settings, vms

        for argv, func, attrs in (
            (["list", "--all", "--json"], vms.cmd_list, {"all": True, "json": True}),
            (
                ["create", "tv1", "--import", "--firmware", "D", "--smp", "2"],
                vms.cmd_create,
                {"firmware": "D", "import_": True, "smp": 2},
            ),
            (["start", "tv1", "quiet"], vms.cmd_start, {"name": "tv1", "args": ["quiet"]}),
            (["stop", "tv1"], vms.cmd_stop, {"unplug": False, "no_fallback": False}),
            (["stop", "tv1", "--unplug"], vms.cmd_stop, {"unplug": True}),
            (["stop", "tv1", "--no-fallback"], vms.cmd_stop, {"no_fallback": True}),
            (["stop", "tv1", "--force"], vms.cmd_stop, {"force": True}),
            (
                ["start", "tv1", "--reason", "bisect", "--by", "agent"],
                vms.cmd_start,
                {"reason": "bisect", "by": "agent", "args": []},
            ),
            (["status", "show", "tv1"], vms.cmd_status, {"verb": "show", "items": []}),
            (
                ["status", "set", "pi/tv1", "reason=bisect", "--by", "agent"],
                vms.cmd_status,
                {"verb": "set", "name": "pi/tv1", "items": ["reason=bisect"], "by": "agent"},
            ),
            (["status", "unset", "tv1", "reason"], vms.cmd_status, {"items": ["reason"]}),
            (["list", "--tag", "a", "--tag", "b"], vms.cmd_list, {"tag": ["a", "b"]}),
            (["create", "tv1", "--tag", "x"], vms.cmd_create, {"tag": ["x"]}),
            (["show", "tv1", "--json"], vms.cmd_show, {"name": "tv1", "json": True}),
            (["delete", "tv1"], vms.cmd_delete, {"name": "tv1"}),
            (["wipe", "tv1", "--part", "db8"], vms.cmd_wipe, {"part": ["db8"]}),
            (["config", "show", "tv1"], settings.cmd_config, {"verb": "show", "items": []}),
            (
                ["config", "set", "tv1", "mem=1024", "smp=2", "tags=a,b"],
                settings.cmd_config,
                {"verb": "set", "items": ["mem=1024", "smp=2", "tags=a,b"]},
            ),
            (["config", "unset", "tv1", "fw", "tags"], settings.cmd_config, {"verb": "unset"}),
            (
                ["viewer", "open", "tv1", "--remote"],
                display.cmd_viewer_open,
                {"args": ["--remote"]},
            ),
            (["viewer", "close", "tv1"], display.cmd_viewer_close, {"name": "tv1"}),
            (
                ["vnc", "start", "tv1", "--port", "5901"],
                display.cmd_vnc_start,
                {"name": "tv1"},
            ),
            (["vnc", "stop", "tv1"], display.cmd_vnc_stop, {"name": "tv1"}),
            (["vnc", "password", "show", "tv1"], display.cmd_vnc_password, {"verb": "show"}),
            (["vnc", "password", "set", "tv1", "F"], display.cmd_vnc_password, {"file": "F"}),
            (["vnc", "password", "set", "tv1"], display.cmd_vnc_password, {"file": None}),
            (["vnc", "password", "reset", "tv1"], display.cmd_vnc_password, {"verb": "reset"}),
            (["identity", "reset", "tv1"], identity.cmd_identity_reset, {"name": "tv1"}),
            (
                ["identity", "authorized-keys", "add", "tv1", "--mine"],
                identity.cmd_authorized_keys,
                {"verb": "add", "mine": True},
            ),
            (["key", "model", "show", "tv1"], identity.cmd_key_model, {"verb": "show"}),
            (["key", "model", "set", "tv1", "F"], identity.cmd_key_model, {"file": "F"}),
            (["key", "model", "unset", "tv1"], identity.cmd_key_model, {"verb": "unset"}),
            (["key", "list", "tv1"], identity.cmd_key_list, {"name": "tv1"}),
            (["library", "config", "show"], settings.cmd_library_config, {"verb": "show"}),
            (
                ["library", "config", "set", "mac_prefix=02:00:00"],
                settings.cmd_library_config,
                {"items": ["mac_prefix=02:00:00"]},
            ),
            (
                ["host", "config", "unset", "max_running"],
                settings.cmd_host_config,
                {"verb": "unset"},
            ),
            (["mitm", "show", "tv1"], mitm.cmd_mitm_show, {"name": "tv1"}),
            (["sensor", "show", "rotate", "tv1"], sensors.cmd_rotate, {"angle": None}),
            (["sensor", "set", "rotate", "tv1", "90"], sensors.cmd_rotate, {"angle": 90}),
            (["sensor", "set", "light", "tv1", "5", "6"], sensors.cmd_light, {"values": [5, 6]}),
            (["sensor", "show", "light", "tv1"], sensors.cmd_light, {"values": []}),
            (["io", "tuner", "list", "tv1"], devices.cmd_tuner, {"verb": "list"}),
            (
                ["io", "tuner", "map", "tv1", "7", "14"],
                devices.cmd_tuner,
                {"verb": "map", "channels": ["7", "14"]},
            ),
            (["io", "tuner", "unmap", "tv1", "all"], devices.cmd_tuner, {"channels": ["all"]}),
            (
                ["io", "usb", "attach", "tv1", "--camera", "--pattern", "ball"],
                devices.cmd_usb_attach,
                {"camera": True, "pattern": "ball"},
            ),
            (
                ["io", "usb", "mkimage", "F", "--fs", "fat32", "--overwrite"],
                devices.cmd_usb_mkimage,
                {"overwrite": True},
            ),
            (
                ["io", "soundbar", "attach", "tv1", "--formats", "ac3,dts"],
                devices.cmd_soundbar,
                {"verb": "attach", "formats": "ac3,dts", "arc_only": False},
            ),
            (
                ["io", "soundbar", "attach", "tv1", "--arc-only"],
                devices.cmd_soundbar,
                {"verb": "attach", "formats": None, "arc_only": True},
            ),
            (["io", "soundbar", "detach", "tv1"], devices.cmd_soundbar, {"verb": "detach"}),
            (["io", "soundbar", "show", "tv1"], devices.cmd_soundbar, {"verb": "show"}),
            (["io", "mic", "play", "tv1", "F"], devices.cmd_mic, {"verb": "play", "file": "F"}),
            (["io", "mic", "play", "tv1", "-"], devices.cmd_mic, {"file": "-"}),
            (
                ["screenshot", "tv1", "-", "--frame", "384x216"],
                display.cmd_screenshot,
                {"frame": "384x216"},
            ),
            (["elements", "--from-png", "F"], display.cmd_elements, {"from_png": "F"}),
        ):
            with self.subTest(argv):
                a = self.parse(*argv)
                self.assertIs(a.func, func)
                for k, v in attrs.items():
                    self.assertEqual(getattr(a, k), v, k)
                self.assertIn("json", vars(a), "every command takes --json")

    def test_global_options_anywhere(self):
        """--json, --library and --repo go anywhere on the line: before the command, between a
        group and its verb, after the VM; not in shell's command, nor after --."""
        from beanjar.commands import vms

        for argv in (
            ["--json", "--library", "L", "list"],
            ["list", "--library", "L", "--json"],
            ["list", "--json", "--library=L"],
        ):
            with self.subTest(argv):
                a = self.parse(*argv)
                self.assertIs(a.func, vms.cmd_list)
                self.assertEqual((a.json, a.library), (True, "L"))
        a = self.parse("vnc", "--json", "password", "--repo", "R", "show", "tv1")
        self.assertEqual((a.json, a.repo, a.verb, a.name), (True, "R", "show", "tv1"))
        self.assertEqual(a.path, ["vnc", "password", "show"])
        self.assertEqual(a.words, ["vnc", "password", "show", "tv1"], "the line without them")
        a = self.parse("config", "set", "tv1", "smp=2", "--json")
        self.assertEqual((a.json, a.items), (True, ["smp=2"]))
        a = self.parse("vnc", "start", "tv1", "--fps", "60", "--json", "--library", "L")
        self.assertEqual((a.json, a.library, a.args), (True, "L", ["--fps", "60"]))
        # shell: what follows NAME is the command, its --json and --library its own
        a = self.parse("--library", "L", "shell", "-T", "tv1", "ls", "--json", "--library", "x")
        self.assertEqual((a.json, a.library), (False, "L"))
        self.assertEqual(a.command, ["ls", "--json", "--library", "x"])
        a = self.parse("start", "tv1", "--", "--json")
        self.assertEqual((a.json, a.args), (False, ["--json"]))
        rc, out, err = self.run_cli("host", "list", "--json")
        self.assertEqual((rc, out.strip()), (0, "[]"), err)

    def test_durations(self):
        """Durations carry a unit (ms, s, m); a bare number is refused."""
        import argparse

        from beanjar.commands import common

        for text, seconds in (("200ms", 0.2), ("1.5s", 1.5), ("2m", 120.0), ("0ms", 0.0)):
            self.assertAlmostEqual(common.duration(text), seconds)
        for bad in ("100", "1.5", "", "ms", "-1s", "1h", "1 s", "1.s"):
            with self.subTest(bad), self.assertRaises(argparse.ArgumentTypeError):
                common.duration(bad)
        a = self.parse("remote", "press", "tv1", "HOME", "--hold", "250ms")
        self.assertAlmostEqual(a.hold, 0.25)
        self.assertAlmostEqual(self.parse("remote", "press", "tv1", "HOME").hold, 0.1)
        a = self.parse("remote", "click", "tv1", "1", "2", "--settle", "0s", "--hold", "1s")
        self.assertEqual((a.settle, a.hold), (0.0, 1.0))
        a = self.parse("remote", "swipe", "tv1", "1", "2", "3", "4", "--time", "0.5s")
        self.assertAlmostEqual(a.time, 0.5)
        self.assertEqual(self.parse("screensaver", "stop", "tv1", "--timeout", "2m").timeout, 120)
        self.assertEqual(self.parse("screensaver", "stop", "tv1").timeout, 60)
        rc, _out, err = self.run_cli("remote", "press", "tv1", "HOME", "--hold", "100")
        self.assertEqual(rc, 2)
        self.assertIn("invalid duration '100'", err)
        rc, _out, err = self.run_cli("screensaver", "start", "tv1", "--timeout", "1s")
        self.assertEqual(rc, 2)
        self.assertIn("5 to 300 seconds", err)

    def test_group_without_verb(self):
        """A group without its verb prints its help on stderr and exits 2; with --json one JSON
        error; `beanjar` without a command the same."""
        for argv, verbs in (
            (["config"], "show,set,unset"),
            (["status"], "show,set,unset"),
            (["vnc", "password"], "show,set,reset"),
            (["library", "config"], "show,set,unset"),
            (["host", "config"], "show,set,unset"),
            (["identity", "authorized-keys"], "show,reset,add"),
            (["key", "model"], "show,set,unset"),
            (["io", "tuner"], "list,map,unmap"),
            (["sensor", "show"], "light,presence,temperature,rotate"),
            (["io", "soundbar"], "attach,detach,show"),
            (["io"], "tuner,usb,soundbar"),
            ([], "list,create"),
        ):
            with self.subTest(argv):
                rc, out, err = self.run_cli(*argv)
                self.assertEqual(rc, 2, err)
                self.assertEqual(out, "")
                self.assertIn("usage: beanjar", err)
                for verb in verbs.split(","):
                    self.assertIn(f"\n    {verb} ", err)
        rc, out, err = self.run_cli("vnc", "password", "--json")
        self.assertEqual(rc, 2)
        error = json.loads(err)
        self.assertTrue(error["usage"])
        self.assertIn("beanjar vnc password needs one of: show, set, reset", error["error"])
        rc, out, _err = self.run_cli("config", "--help")
        self.assertEqual(rc, 0, "asked for, the help is no error")
        self.assertIn("usage: beanjar config", out)

    def test_wrong_order(self):
        """A VM where the verb should be, a verb where the VM should be, a KEY=VALUE before the VM:
        a usage error naming the right form."""
        for argv, says in (
            (
                ["vnc", "password", "tv1", "show"],
                "the verb comes first: beanjar vnc password show tv1",
            ),
            (
                ["config", "tv1", "set", "mem=1"],
                "the verb comes first: beanjar config set tv1 mem=1",
            ),
            (["tv1", "start"], "the verb comes first: beanjar start tv1"),
            (
                ["vnc", "tv1", "password", "show"],
                "the verb comes first: beanjar vnc password show tv1",
            ),
            (
                ["sensor", "tv1", "show", "rotate"],
                "the verb comes first: beanjar sensor show rotate tv1",
            ),
            (["--json", "ares-install", "-d", "tv1"], "ares-install takes no --json"),
            (
                ["io", "tuner", "tv1", "map", "7"],
                "the verb comes first: beanjar io tuner map tv1 7",
            ),
            (
                ["key", "model", "tv1", "set", "F"],
                "the verb comes first: beanjar key model set tv1 F",
            ),
            (
                ["sensor", "rotate", "tv1", "90"],
                "beanjar sensor takes its verb first: beanjar sensor show|set rotate tv1 90",
            ),
            (
                ["identity", "authorized-keys", "tv1", "add-mine"],
                "the verb comes before the VM: beanjar identity authorized-keys VERB NAME",
            ),
            (
                ["config", "set", "mem=1", "tv1", "smp=2"],
                "the VM comes before KEY=VALUE: beanjar config set tv1 mem=1 smp=2",
            ),
            (
                ["status", "set", "reason=x", "tv1"],
                "the VM comes before KEY=VALUE: beanjar status set tv1 reason=x",
            ),
            (
                ["vnc", "password", "set", "show", "tv1"],
                "'show' is a verb of beanjar vnc password, which comes before the VM: "
                "beanjar vnc password show NAME",
            ),
        ):
            with self.subTest(argv):
                rc, out, err = self.run_cli(*argv)
                self.assertEqual((rc, out), (2, ""), err)
                self.assertIn(says, err)
        rc, _out, err = self.run_cli("vnc", "password", "tv1", "show", "--json")
        self.assertEqual(json.loads(err), {
            "error": "the verb comes first: beanjar vnc password show tv1", "usage": True
        })  # fmt: skip

    def test_version(self):
        import contextlib
        import io
        import json

        from beanjar import commands

        keys = {"name", "version", "commit", "source", "dirty", "guest"}
        for argv, check, lines in (
            (
                ["--version"],
                lambda t: self.assertRegex(t, r"^beanjar \S+.*\nbeanjar-guest \S+\n$"),
                2,
            ),
            (
                ["--library", "x", "--version"],
                lambda t: self.assertTrue(t.startswith("beanjar ")),
                2,
            ),
            (["--version", "--json"], lambda t: self.assertEqual(json.loads(t).keys(), keys), 1),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(commands.main(argv), 0)
            check(out.getvalue())
            self.assertEqual(out.getvalue().count("\n"), lines)
        info = json.loads(out.getvalue())
        self.assertEqual(info["source"], "checkout")
        self.assertEqual(info["guest"].keys(), {"version", "commit"})

    def test_old_names_refused(self):
        import contextlib
        import io

        from beanjar.commands.common import Usage

        for argv in (
            ["status", "tv1"],
            ["set", "tv1", "mem=1"],
            ["view", "tv1"],
            ["unview", "tv1"],
            ["vnc", "tv1"],
            ["unvnc", "tv1"],
            ["vnc-password", "tv1"],
            ["new-identity", "tv1"],
            ["model-key", "tv1"],
            ["mitm-setup"],
            ["mitm", "setup"],  # mitmproxy is the user's own (mitmtool.py): nothing to set up
            ["config", "--global"],
            ["info", "tv1"],
            ["settings", "tv1"],
            ["reason", "tv1", "x"],
            ["tag", "tv1", "+x"],
            ["identity", "renew", "tv1"],
            ["io", "usb", "plug", "tv1", "--camera"],
            ["vnc", "password", "tv1"],
            ["vnc", "password", "show", "tv1", "--show"],
            ["key", "model", "show", "tv1", "F"],
            ["stop", "tv1", "--unplug", "--no-fallback"],
            ["screenshot", "tv1", "-", "--size", "384x216"],
            ["elements", "--image", "F"],
            ["io", "usb", "mkimage", "F", "--fs", "fat32", "--force"],
            ["identity", "authorized-keys", "add", "tv1"],
        ):
            with self.subTest(argv), contextlib.redirect_stderr(io.StringIO()):
                try:
                    self.parse(*argv)
                except SystemExit as e:
                    self.assertEqual(e.code, 2)
                except Usage:
                    pass
                else:
                    self.fail(f"{argv} parsed")

    def test_ares_help(self):
        """`beanjar --help` lists the ares-TOOL form; `ares-TOOL --help` without -d is beanjar's own
        help of it (with -d the tool's)."""
        env = dict(os.environ, PYTHONPATH=os.path.join(TOP, "host", "core"))
        for argv in (["--help"], ["ares-install", "--help"]):
            p = subprocess.run(
                [sys.executable, "-m", "beanjar", *argv], env=env, capture_output=True, text=True
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn(
                "beanjar ares-TOOL [--root] -d NAME [ARGS...]", " ".join(p.stdout.split())
            )

    def test_config_help(self):
        """`config set --help` lists every setting and tags, from the table that holds them (vmsettings.SETTING_HELP)."""
        p = subprocess.run(
            [sys.executable, "-m", "beanjar", "config", "set", "--help"],
            env=dict(os.environ, PYTHONPATH=os.path.join(TOP, "host", "core")),
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        for k in vm_settings.SETTINGS:
            self.assertIn(f"\n  {k}=", p.stdout)
        self.assertIn("unset: dhcp", p.stdout)
        self.assertIn("\n  tags=TAG[,TAG...]", p.stdout)
        self.assertEqual(set(vm_settings.SETTING_HELP), set(vm_settings.SETTINGS))


class ScreenshotTest(unittest.TestCase):
    """beanjar screenshot: the PNG conversion and the call to a front end's org.beanjar.Display1."""

    def test_png(self):
        import struct
        import zlib

        w, h = 3, 2
        pixels = bytes(range(w * h * 3))
        png = framing.ppm_to_png(b"P6\n3 2\n255\n" + pixels)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        chunks, i = {}, 8
        while i < len(png):
            n = struct.unpack(">I", png[i : i + 4])[0]
            kind, body = png[i + 4 : i + 8], png[i + 8 : i + 8 + n]
            self.assertEqual(
                struct.unpack(">I", png[i + 8 + n : i + 12 + n])[0], zlib.crc32(kind + body)
            )
            chunks[kind] = body
            i += 12 + n
        self.assertEqual(struct.unpack(">IIBBBBB", chunks[b"IHDR"]), (w, h, 8, 2, 0, 0, 0))
        raw = zlib.decompress(chunks[b"IDAT"])
        self.assertEqual(raw, b"".join(b"\0" + pixels[y * 9 : y * 9 + 9] for y in range(h)))
        for bad in (b"P5\n3 2\n255\n", b"P6\n3 2\n255\n" + pixels[:-1]):
            with self.assertRaises(base.Error):
                framing.ppm_to_png(bad)

    def test_ask(self):
        """Screenshot on org.beanjar.Display1 of a private bus: no owner, an owner that saves, an
        owner that fails."""
        import shutil
        import threading

        try:
            from gi.repository import Gio, GLib
        except ImportError:
            self.skipTest("no python3-gi")
        if not shutil.which("dbus-daemon"):
            self.skipTest("no dbus-daemon")
        with tempfile.TemporaryDirectory() as d:
            daemon = unit.start_bus(d)
            self.addCleanup(daemon.wait)
            self.addCleanup(daemon.terminate)
            bus = os.path.join(d, "dbus.sock")
            ask = lambda: vm_library.Library._screenshot_ask(bus, "/x y/shot.ppm", 1280, 720, 5)
            self.assertFalse(ask(), "no owner: the headless path")
            seen = []
            fail = []
            ready = threading.Event()
            stop = threading.Event()

            def owner():
                ctx = GLib.MainContext.new()
                ctx.push_thread_default()
                conn = Gio.DBusConnection.new_for_address_sync(
                    f"unix:path={bus}",
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None,
                    None,
                )
                info = Gio.DBusNodeInfo.new_for_xml(
                    "<node><interface name='org.beanjar.Display1'><method name='Screenshot'>"
                    "<arg type='s' direction='in'/><arg type='u' direction='in'/>"
                    "<arg type='u' direction='in'/></method></interface></node>"
                )

                def call(_c, _s, _p, _i, _m, params, inv):
                    seen.append(params.unpack())
                    if fail:
                        inv.return_dbus_error(
                            "org.beanjar.Display1.Error.NoPicture", "no picture yet"
                        )
                    else:
                        inv.return_value(None)

                conn.register_object("/org/beanjar/Display1", info.interfaces[0], call, None, None)
                Gio.bus_own_name_on_connection(
                    conn, "org.beanjar.Display1", Gio.BusNameOwnerFlags.DO_NOT_QUEUE, None, None
                )
                ready.set()
                while not stop.is_set():
                    ctx.iteration(False)
                    time.sleep(0.01)
                conn.close_sync(None)

            t = threading.Thread(target=owner)
            t.start()
            self.addCleanup(t.join)
            self.addCleanup(stop.set)
            self.assertTrue(ready.wait(5))
            self.assertTrue(wait_for(ask))
            self.assertEqual(seen[-1], ("/x y/shot.ppm", 1280, 720))
            fail.append(1)
            with self.assertRaisesRegex(base.Error, "no picture yet"):
                ask()


class ThumbnailTest(unittest.TestCase):
    """The last thumbnail (a screenshot no larger than Library.THUMBNAIL_MAX) kept in the VM's
    directory and given back by last_thumbnail, also once the VM is off; larger screenshots leave
    it as it was. The front end's call stubbed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-thumbnail-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.lib = vm_library.Library(os.path.join(self.tmp, "lib"), TOP)
        self.vm = vm_library.VM(self.lib, "a")
        os.makedirs(self.vm.run_dir)

    def shot(self, size, grey):
        def ask(bus, ppm, w, h, timeout):
            with open(ppm, "wb") as f:
                f.write(b"P6\n%d %d\n255\n" % (w, h) + bytes([grey]) * w * h * 3)
            return True

        out = os.path.join(self.tmp, "out.png")
        with (
            mock.patch.object(vm_library.Library, "vm", return_value=self.vm),
            mock.patch.object(vm_library.VM, "display", "dbus", create=True),
            mock.patch.object(vm_library.VM, "pid", return_value=1),
            mock.patch.object(vm_library.VM, "viewer_pid", return_value=None),
            mock.patch.object(vm_library.VM, "vnc_pid", return_value=2),
            mock.patch.object(vm_library.Library, "_screenshot_ask", side_effect=ask),
        ):
            self.lib.screenshot("a", out, size)
        return open(out, "rb").read()

    def test_kept(self):
        with mock.patch.object(vm_library.Library, "vm", return_value=self.vm):
            with self.assertRaises(base.Error):
                self.lib.last_thumbnail("a")
        small = self.shot((384, 216), 0x40)
        self.shot((1280, 720), 0x80)  # not a thumbnail: the kept one stays
        with mock.patch.object(vm_library.Library, "vm", return_value=self.vm):
            self.assertEqual(self.lib.last_thumbnail("a"), small)
        self.assertNotIn("thumbnail.png.part", os.listdir(self.vm.dir))


class LanHostnameTest(unittest.TestCase):
    """link=lan sends the VM's name as its DHCP hostname."""

    def test_label(self):
        for name, label in (
            ("tv1", "tv1"),
            ("TV_One.lab", "tv-one-lab"),
            ("w22o-number1", "w22o-number1"),
            ("a.", "a"),
            ("a" * 64, "a" * 63),
            ("a" * 62 + ".b", "a" * 62),
        ):
            with self.subTest(name):
                self.assertEqual(vm_settings.dns_label(name), label)

    def test_udhcpc(self):
        from beanjar.network import netns

        off = netns.dhcp_argv({}, "/s.py")
        self.assertNotIn("-x", off)
        self.assertEqual(off[-2:], ["-s", "/s.py"])
        on = netns.dhcp_argv({"NET_LAN_HOSTNAME": "tv1"}, "/s.py")
        self.assertEqual(on[on.index("-x") : on.index("-x") + 2], ["-x", "hostname:tv1"])
        self.assertEqual(on[-2:], ["-s", "/s.py"])


class PowerOffTest(unittest.TestCase):
    """Whether a guest can take stop's power-off signal (VM.power_off_refusal, targets.takes_power_off)
    and which way Library.stop then stops it, with the launcher, QEMU and the console stubbed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-poweroff-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.lib = vm_library.Library(os.path.join(self.tmp, "lib"), TOP)
        self.vm = vm_library.VM(self.lib, "a")
        os.makedirs(self.vm.run_dir)
        os.makedirs(self.vm.logs_dir)
        with open(self.vm.console_log, "w") as f:
            f.write("Starting the initramfs debug shell\n")

    def micom(self, line):
        """run/micom-status as the guest's IR pump wrote it, the last line being the state now."""
        with open(os.path.join(self.vm.run_dir, poweroff.MICOM_STATUS), "w") as f:
            f.write(line)

    def refusal(self, target=H13_AFRO, pid=123, qmp="running", ready=True):
        self.micom("standby=0 power=3 ready=0\nstandby=0 power=3 ready=1\n" if ready else "")
        from beanjar.access import console

        with (
            mock.patch.object(vm_library.VM, "target", return_value=target),
            mock.patch.object(vm_library.VM, "pid", return_value=pid),
            mock.patch.object(poweroff, "qmp_status", return_value=qmp),
            mock.patch.object(console, "send") as send,
        ):
            why = self.vm.power_off_refusal()
        send.assert_not_called()  # the check never types on the console
        return why

    def test_targets(self):
        for target in targets.all_targets():
            with self.subTest(target):
                self.assertEqual(
                    targets.takes_power_off(target), targets.platform(target) in ("webOS", "Test")
                )
        self.assertTrue(targets.takes_power_off(H13_AFRO))
        self.assertFalse(targets.takes_power_off(("lm14", "netcast45")))
        # the test guest: BusyBox's poweroff, typed as webOS's call is, with no micom to wait for
        self.assertTrue(targets.takes_power_off(("virt", "testguest")))
        self.assertEqual(poweroff.power_off_call("Test"), poweroff.TEST_POWER_OFF_CALL)
        self.assertEqual(poweroff.power_off_call("webOS"), poweroff.POWER_OFF_CALL)
        self.assertFalse(targets.takes_power_off(("m2", "simplesmart")))

    def test_refusals(self):
        self.assertEqual(
            self.refusal(target=("lm14", "netcast45")), "NetCast has no power-off signal"
        )
        self.assertEqual(
            self.refusal(target=("m2", "simplesmart")), "SimpleSmart has no power-off signal"
        )
        self.assertEqual(self.refusal(pid=None), "it is not running")
        self.assertEqual(self.refusal(qmp="standby"), "it is in standby")
        self.assertEqual(self.refusal(qmp="paused"), "QEMU is paused")
        self.assertIsNone(self.refusal())
        self.assertIsNone(self.refusal(qmp=None), "QMP busy: not known to refuse")
        self.assertEqual(self.refusal(ready=False), "webOS is still starting")
        with open(self.vm.console_log, "w") as f:
            f.write("[    0.000000] Booting Linux\n")
        self.assertEqual(self.refusal(), "it is still booting")
        # a signal already sent to this QEMU: a stop waits for it again, whatever the micom says
        with open(self.vm.power_off_file, "w") as f:
            f.write("123\n")
        self.assertIsNone(self.refusal(ready=False))
        self.assertEqual(self.refusal(pid=456), "it is still booting", "another QEMU's")

    def test_micom_ready(self):
        """run/micom-status's last line decides (the file keeps every change since QEMU started)."""
        run = self.vm.run_dir
        self.assertFalse(poweroff.micom_ready(run), "no file")
        for text, ready in (
            ("", False),
            ("standby=0 power=3 ready=0\n", False),
            ("standby=0 power=3 ready=1\n", True),
            ("standby=0 power=3 ready=1 led.6e=010101/1\n", True),
            ("standby=0 power=3 ready=0\nstandby=0 power=3 ready=1 led.6e=01/1\n", True),
            ("standby=0 power=3 ready=1\nstandby=0 power=3 ready=0\n", False),
            ("standby=0 power=3\n", False),  # a guest from before the flag
            ("standby=0 power=3 ready=10\n", False),
        ):
            with self.subTest(text):
                self.micom(text)
                self.assertEqual(poweroff.micom_ready(run), ready)
        self.micom("x" * 5000 + "\nstandby=0 power=3 ready=1\n")
        self.assertTrue(poweroff.micom_ready(run), "a long file is read from its end")

    def test_refused_reply(self):
        """What a call's console text says about the signal: only an explicit failure refuses it."""
        for text, refused in (
            ('{ "returnValue": false, "errorText": "denied" }', True),
            ('{\n "returnValue": false,\n "errorCode": -1\n}', True),
            (
                '{"errorCode":-1,"errorText":"Service does not exist: com.webos.service.tvpower",'
                '"returnValue":false}',
                True,
            ),
            ("webOS namespace is not running\n", True),
            ("luna-send: Service does not exist", True),
            (
                "LUNASERVICE ERROR -1029: Unable to connect to com.palm.hub "
                "(/var/run/luna-service2/com.palm.hub) (_LSTransportConnectClient @ transport.c:1326)",
                True,
            ),
            ('{ "returnValue": true, "state": "Power Off" }', False),
            ("", False),  # no reply: webOS has powered off after one
            ("Terminated\n", False),  # luna-send timed out
            ("[  41.2] kernel: noise\n", False),
        ):
            with self.subTest(text):
                self.assertEqual(poweroff.power_off_refused(text), refused)

    def test_busy_reply(self):
        """Only tvpowerd's "power is busy" refusal is retried; other refusals fall back at once."""
        busy = (
            '{"errorCode":-1,"errorText":"com.webos.service.tvpower/power is busy",'
            '"returnValue":false}'
        )
        self.assertTrue(poweroff.power_off_busy(busy))
        self.assertTrue(
            poweroff.power_off_busy('{ "returnValue": false,\n "errorText": "power is busy" }')
        )
        for text in ('{ "returnValue": false, "errorText": "denied" }', "", "power is busy"):
            self.assertFalse(poweroff.power_off_busy(text))

    def test_busy_retry(self):
        """Bg.power_off repeats a busy call, accepts a later answer, and gives up after the last."""
        from beanjar.access import console
        from beanjar.runtime import launch

        busy = '{"errorText":"power is busy","returnValue":false}'
        for replies, sends, code in (
            ([busy, '{"returnValue":true}'], 2, 0),
            ([busy] * 9, vm_library.POWER_OFF_TRIES, vm_library.STOP_REFUSED),
            (['{"returnValue":false,"errorText":"denied"}'], 1, vm_library.STOP_REFUSED),
        ):
            with self.subTest(replies[0]), tempfile.TemporaryDirectory() as d:
                bg = launch.Bg.__new__(launch.Bg)
                bg.run_dir, bg.pidfile, bg.sock, bg.log = d, d + "/pid", d + "/sock", d + "/log"
                alive = iter([True, True, False])
                with (
                    mock.patch.object(
                        base, "read_pid", side_effect=lambda f: 123 if f == bg.pidfile else None
                    ),
                    mock.patch.object(
                        launch.unit, "alive", side_effect=lambda p: next(alive, False)
                    ),
                    mock.patch.object(launch.unit, "comm", return_value="qemu-system-x86"),
                    mock.patch.object(launch.unit, "is_socket", return_value=True),
                    mock.patch.object(console, "send", side_effect=replies) as send,
                    mock.patch.object(launch.time, "sleep"),
                    mock.patch.object(launch, "say") as say,
                    mock.patch("builtins.print"),
                ):
                    self.assertEqual(bg.power_off(5), code)
                self.assertEqual(send.call_count, sends)

    def stop(
        self, how=vm_library.POWER_OFF, fallback=True, why=None, powers_off=True, refused=False
    ):
        """Library.stop with the VM running (QEMU pid 123) and the launcher stubbed: its stop ends
        QEMU, its stop --power-off does if powers_off, and exits 5 if refused (webOS refused the
        call), else 4 when QEMU still runs. ([launcher calls], [what it said], Error)."""
        calls, said, up = [], [], {"pid": 123}

        def launcher(cmd, out=None, cwd=None, env=None):
            calls.append((cmd[cmd.index("stop") :], (env or {}).get("SAVE_LOGS")))
            if "--power-off" not in cmd or powers_off:
                up["pid"] = None
                return 0
            return vm_library.STOP_REFUSED if refused else vm_library.STOP_TIMED_OUT

        error = None
        with (
            mock.patch.object(vm_library.Library, "launcher", return_value=["vm-launcher"]),
            mock.patch.object(hostprocs, "run_streamed", side_effect=launcher),
            mock.patch.object(vm_library.VM, "pid", side_effect=lambda: up["pid"]),
            mock.patch.object(vm_library.VM, "unit_name", return_value=None),
            mock.patch.object(vm_library.VM, "unit_up", return_value=False),
            mock.patch.object(vm_library.VM, "power_off_refusal", return_value=why) as refusal,
            mock.patch.object(vm_library.Library, "vm", return_value=self.vm),
            mock.patch.object(os, "kill"),
            mock.patch.object(os, "getpgid", return_value=os.getpgid(0)),
        ):
            try:
                self.lib.stop("a", how, fallback=fallback, out=said.append)
            except base.Error as e:
                error = str(e)
        if how == vm_library.POWER_OFF:
            refusal.assert_called_once_with()
        else:
            refusal.assert_not_called()
        self.assertFalse(os.path.exists(self.vm.stopping_file), "the stopping mark is removed")
        return calls, said, error

    def test_stop_selection(self):
        d = self.vm.dir
        power_off = (["stop", "--dir", d, "--power-off", f"{poweroff.POWER_OFF_TIMEOUT}"], None)
        sync_stop = (["stop", "--dir", d], None)
        # the signal, and QEMU exits by itself: nothing else
        self.assertEqual(self.stop(), ([power_off], [], None))
        # it cannot take it: synced and stopped instead, and says why; without fallback, nothing
        calls, said, error = self.stop(why="it is still booting")
        self.assertEqual((calls, error), ([sync_stop], None))
        self.assertEqual(
            said,
            ["a: no power-off signal (it is still booting); syncing its disks and stopping it"],
        )
        calls, said, error = self.stop(why="it is still booting", fallback=False)
        self.assertEqual((calls, said), ([], []))
        self.assertEqual(error, "VM a cannot take the power-off signal: it is still booting")
        # it does not power off in time: synced and stopped (its logs saved before the signal);
        # without fallback left running
        calls, said, error = self.stop(powers_off=False)
        self.assertEqual((calls, error), ([power_off, (sync_stop[0], "0")], None))
        t = poweroff.POWER_OFF_TIMEOUT
        self.assertEqual(
            said,
            [f"VM a has not powered off {t} s after the signal; syncing its disks and stopping it"],
        )
        calls, said, error = self.stop(powers_off=False, fallback=False)
        self.assertEqual(calls, [power_off])
        self.assertEqual(error, f"VM a has not powered off {t} s after the signal; it still runs")
        # webOS refused the call (an explicit failure, not a missing reply): synced and stopped at
        # once, said so; without fallback left running, with that reason
        calls, said, error = self.stop(powers_off=False, refused=True)
        self.assertEqual((calls, error), ([power_off, (sync_stop[0], "0")], None))
        self.assertEqual(
            said, ["VM a: webOS refused the power-off signal; syncing its disks and stopping it"]
        )
        calls, said, error = self.stop(powers_off=False, refused=True, fallback=False)
        self.assertEqual(calls, [power_off])
        self.assertEqual(error, "VM a: webOS refused the power-off signal; it still runs")
        # unplug: QEMU killed (os.kill, stubbed), then the launcher's stop without logs
        calls, said, error = self.stop(vm_library.UNPLUG)
        self.assertEqual((calls, said, error), ([(sync_stop[0], "0")], [], None))
        with self.assertRaises(ValueError):
            self.lib.stop("a", "force")


class SetsidTest(ManagerTest):
    """The fallback without a user systemd (VM_SYSTEMD=0): start and stop, --unplug, a power-off."""

    systemd = False


for _n in dir(ManagerTest):
    if _n.startswith("test_") and _n not in (
        "test_start_stop_single_vm",
        "test_force_stop",
        "test_guest_power_off",
        "test_reason",
    ):
        setattr(SetsidTest, _n, None)

if __name__ == "__main__":
    unittest.main(verbosity=2)
