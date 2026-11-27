#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check the devmode layer's early flag creation without touching host state."""

import configparser
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "guest/layers/devmode/rootfs"
UNITS = ROOT / "etc/systemd/system"
WAIT_KEY = "/usr/libexec/emulator-devmode/wait-key.sh"
START = ROOT / "usr/libexec/emulator-devmode/start-devmode.sh"


class FlagTest(unittest.TestCase):
    def test_mount_and_consumer_order(self):
        unit = configparser.ConfigParser(strict=False)
        unit.read(UNITS / "emulator-devmode-flag.service")
        self.assertEqual(unit["Unit"]["DefaultDependencies"], "no")
        self.assertIn("mount-readwrite.service", unit["Unit"]["Requires"].split())
        self.assertIn("mount-readwrite.service", unit["Unit"]["After"].split())
        for name in ("sam", "appinstalld"):
            consumer = configparser.ConfigParser()
            consumer.read(UNITS / f"{name}.service.d/emulator-devmode.conf")
            for edge in ("Requires", "After"):
                self.assertIn("emulator-devmode-flag.service", consumer["Unit"][edge].split())

    def test_commands_recreate_hidden_flag_and_preserve_existing(self):
        lines = (UNITS / "emulator-devmode-flag.service").read_text().splitlines()
        commands = [
            line.split("=", 1)[1].split() for line in lines if line.startswith("ExecStart=")
        ]
        self.assertEqual(len(commands), 2)
        with tempfile.TemporaryDirectory() as directory:
            for _ in range(2):
                for command in commands:
                    subprocess.run(
                        [part.replace("/var/", directory + "/var/") for part in command], check=True
                    )
                flag = Path(directory) / "var/luna/preferences/devmode_enabled"
                self.assertTrue(flag.is_file())
                if not flag.read_bytes():
                    flag.write_bytes(b"existing user flag")
            self.assertEqual(flag.read_bytes(), b"existing user flag")
        self.assertNotIn("ExecStop=", "\n".join(lines))


class MaskTest(unittest.TestCase):
    """The layer's devmode.service masks the stock unit on systemd releases
    (Upstart's devmode.override is the sysvinit twin) and runs our own
    start-devmode.sh past the store app's signature gate, exactly when the
    flag exists."""

    def test_masks_stock_unit_by_path(self):
        self.assertTrue((UNITS / "devmode.service").is_file())

    def test_no_store_app_signature_gate(self):
        lines = [
            line
            for line in (UNITS / "devmode.service").read_text().splitlines()
            if line and not line.startswith("#")
        ]
        text = "\n".join(lines)
        for banned in ("cryptofs", "palmdts", "openssl dgst", ".sig", "pub.pem", "jailer -D"):
            self.assertNotIn(banned, text)

    def test_ordering_after_flag_and_jail_before_consumers(self):
        unit = configparser.ConfigParser(strict=False)
        unit.read(UNITS / "devmode.service")
        for edge in ("Requires", "After"):
            for dep in ("emulator-devmode-flag.service", "jail.service"):
                self.assertIn(dep, unit["Unit"][edge].split())
        self.assertIn("sam.service", unit["Unit"]["Before"].split())
        self.assertIn("appinstalld.service", unit["Unit"]["Before"].split())

    def test_runs_own_server_only_with_flag(self):
        lines = (UNITS / "devmode.service").read_text().splitlines()
        starts = [line.split("=", 1)[1] for line in lines if line.startswith("ExecStart=")]
        self.assertEqual(len(starts), 1)
        self.assertIn("/usr/libexec/emulator-devmode/start-devmode.sh", starts[0])
        self.assertIn("/var/luna/preferences/devmode_enabled", starts[0])


class KeyserverTest(unittest.TestCase):
    """The key server's systemd twin of emulator-devmode-keyserver.conf: it
    serves the developer key on 9991 beside the devmode job, in developer
    mode only."""

    def test_runs_tcpsvd_keyserver_after_devmode(self):
        unit = configparser.ConfigParser(strict=False)
        unit.read(UNITS / "emulator-devmode-keyserver.service")
        for edge in ("Requires", "After"):
            self.assertIn("devmode.service", unit["Unit"][edge].split())
        starts = [
            line.split("=", 1)[1]
            for line in (UNITS / "emulator-devmode-keyserver.service").read_text().splitlines()
            if line.startswith("ExecStart=")
        ]
        self.assertEqual(len(starts), 1)
        self.assertIn("tcpsvd", starts[0])
        self.assertIn("9991", starts[0])
        self.assertIn("/usr/libexec/emulator-devmode/keyserver.sh", starts[0])

    def test_developer_mode_only(self):
        text = (UNITS / "emulator-devmode-keyserver.service").read_text()
        self.assertIn("/var/luna/preferences/devmode_enabled", text)

    def test_wanted_by_mbd_target_like_devmode(self):
        # systemd starts only units something pulls in: ship the wants link,
        # as the firmware does (no enable step runs at boot).
        link = UNITS / "webos-mbd.target.wants" / "emulator-devmode-keyserver.service"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), Path("../emulator-devmode-keyserver.service"))


class KeyWaitTest(unittest.TestCase):
    """devmode counts as started before start-devmode.sh has written the key, so both key server
    twins wait for it, bounded, before serving (an early `ares-novacom --getkey` got a 404)."""

    def run_wait(self, key, seconds):
        start = time.monotonic()
        subprocess.run(
            ["sh", str(ROOT / WAIT_KEY.lstrip("/")), str(seconds)],
            env={"KEY": str(key), "PATH": "/usr/bin:/bin"},
            check=True,
            timeout=seconds + 10,
            stderr=subprocess.DEVNULL,
        )
        return time.monotonic() - start

    def test_both_units_wait_bounded_before_serving(self):
        lines = (UNITS / "emulator-devmode-keyserver.service").read_text().splitlines()
        pre = [line.split("=", 1)[1] for line in lines if line.startswith("ExecStartPre=")]
        self.assertEqual(pre, [f"{WAIT_KEY} 30"])
        conf = (ROOT / "etc/init/emulator-devmode-keyserver.conf").read_text()
        pre_start = conf.split("pre-start script", 1)[1].split("end script", 1)[0]
        self.assertIn(f"{WAIT_KEY} 30", pre_start)
        # the bound stays under systemd's default start timeout (90 s), which would fail the unit
        self.assertNotIn("TimeoutStartSec", "\n".join(lines))
        self.assertIn("/var/luna/preferences/webos_rsa", (ROOT / WAIT_KEY.lstrip("/")).read_text())
        self.assertIn(
            "/var/luna/preferences/webos_rsa",
            (ROOT / "usr/libexec/emulator-devmode/keyserver.sh").read_text(),
        )

    def test_wait_returns_when_the_key_appears_and_gives_up_in_time(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "webos_rsa"
            self.assertGreaterEqual(self.run_wait(key, 2), 2)  # never written: bounded, exit 0
            writer = threading.Timer(1, key.write_bytes, (b"key",))
            writer.start()
            self.assertLess(self.run_wait(key, 20), 5)  # written later: serves then
            writer.join()
            self.assertLess(self.run_wait(key, 20), 1)  # already there: no wait


class KeyWriteTest(unittest.TestCase):
    """start-devmode.sh keeps the encrypted developer key across boots and makes it again only
    when it is missing or its stamp (the public key and passphrase) differs; a new served key
    replaces the old by a rename, and a failed encryption leaves no key and no stamp. Runs the
    script's key section with stub nyx-cmd and openssl and /var in a temporary directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.preferences = self.dir / "var/luna/preferences"
        self.preferences.mkdir(parents=True)
        self.ssh = self.dir / "var/lib/devmode/ssh"
        (self.dir / "keys").mkdir()
        (self.dir / "keys/developer_rsa.pub").write_text("ssh-rsa AAAA one\n")
        self.calls = self.dir / "calls"

    def boot(self, nduid="0123abcdef", openssl_ok=True):
        text = START.read_text()
        start = text.index("KEY=/var/")
        end = text.index("unset nduid KEY SERVED_KEY")
        stubs = self.dir / "bin"
        shutil.rmtree(stubs, ignore_errors=True)
        stubs.mkdir()
        (stubs / "nyx-cmd").write_text(f"#!/bin/sh\necho {nduid}\n")
        # dgst hashes stdin; rsa is logged, with whether the old served key was still there.
        (stubs / "openssl").write_text(
            "#!/bin/sh\n"
            '[ "$1" = dgst ] && exec sha256sum\n'
            f'if [ -e "{self.preferences}/webos_rsa" ]; then echo rsa-old >>"{self.calls}"; '
            f'else echo rsa >>"{self.calls}"; fi\n'
            + (
                'while [ $# -gt 1 ]; do [ "$1" = -out ] && echo "key $P $(cat $KEYS/developer_rsa.pub)" >"$2"; '
                "shift; done\nexit 0\n"
                if openssl_ok
                else "exit 1\n"
            )
        )
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        section = (
            text[start:end].replace("/usr/bin/", f"{stubs}/").replace("/var/", f"{self.dir}/var/")
        )
        self.calls.write_text("")
        subprocess.run(
            ["sh", "-euc", f"export KEYS={self.dir}/keys\n{section}"],
            check=True,
            env={"PATH": os.environ["PATH"]},
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        return self.calls.read_text().split()

    def served(self):
        return self.preferences / "webos_rsa"

    def assert_made(self, text):
        self.assertEqual(self.served().read_text(), text)
        self.assertEqual(self.served().stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.ssh / "webos_rsa").read_text(), text)
        self.assertTrue((self.ssh / "webos_rsa.stamp").read_text().strip())
        self.assertNotIn("0123AB", (self.ssh / "webos_rsa.stamp").read_text())
        self.assertEqual(sorted(p.name for p in self.preferences.iterdir()), ["webos_rsa"])

    def test_first_boot_makes_key_and_stamp(self):
        self.assertEqual(self.boot(), ["rsa"])
        self.assert_made("key 0123AB ssh-rsa AAAA one\n")

    def test_second_boot_keeps_the_key(self):
        self.boot()
        before = self.served().stat()
        self.assertEqual(self.boot(), [], "openssl rsa not run again")
        after = self.served().stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assert_made("key 0123AB ssh-rsa AAAA one\n")

    def test_identity_or_nduid_change_remakes_the_key_by_rename(self):
        self.boot()
        (self.dir / "keys/developer_rsa.pub").write_text("ssh-rsa BBBB two\n")
        self.assertEqual(self.boot(), ["rsa-old"], "the old key served while openssl runs")
        self.assert_made("key 0123AB ssh-rsa BBBB two\n")
        self.assertEqual(self.boot(nduid="fedcba9876"), ["rsa-old"])
        self.assert_made("key FEDCBA ssh-rsa BBBB two\n")
        self.served().unlink()  # a missing key is made again
        self.assertEqual(self.boot(nduid="fedcba9876"), ["rsa"])
        self.assert_made("key FEDCBA ssh-rsa BBBB two\n")

    def test_failure_leaves_no_key_stamp_or_temporary(self):
        self.boot()
        (self.preferences / "webos_rsa.new").write_text("stale temporary\n")
        (self.dir / "keys/developer_rsa.pub").write_text("ssh-rsa BBBB two\n")
        self.boot(openssl_ok=False)
        self.assertEqual(list(self.preferences.iterdir()), [])
        self.assertEqual(list(self.ssh.iterdir()), [])

    def test_no_nduid_leaves_no_key_or_stamp(self):
        self.boot()
        self.assertEqual(self.boot(nduid=""), [])
        self.assertEqual(list(self.preferences.iterdir()), [])
        self.assertFalse((self.ssh / "webos_rsa.stamp").exists())


if __name__ == "__main__":
    unittest.main()
