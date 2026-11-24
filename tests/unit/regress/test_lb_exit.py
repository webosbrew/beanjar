# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""tests/regress/lb-exit.sh: legacy-broadcast's clean exits (it stops itself when idle and a call
starts it again) told apart from a crash or another abnormal exit, on recorded evidence: systemd's
result for the boot instance, the hub's LSHUB_SRV_EXIT for those it started, the crash reports."""

import shutil
import subprocess
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[3] / "tests/regress/lb-exit.sh"

# W23L (kf23f number1) on the x86-64 guest kernel: the boot instance's unit after it stopped itself,
# and the hub's line for the instance a call started, which stopped itself too
UNIT_CLEAN = "Result=success ExecMainStatus=0 NRestarts=0 ActiveState=inactive"
HUB_CLEAN = (
    "2026-11-24T14:22:14.768774Z [78.849176197] user.info ls-hubd [] ls-hubd LSHUB_SRV_EXIT "
    '{"SERVICE_NAME":"com.webos.service.legacybroadcast","EXE":"/usr/sbin/setcpushares-ls2 '
    '/etc/systemd/system/scripts/legacy-broadcast.sh","STATE":3,"EXIT_CODE":0,"RESPAWN":0,"PID":2756}'
)
OTHER = (
    "2026-11-24T14:22:15.000000Z [79.0] user.info ls-hubd [] ls-hubd LSHUB_SRV_EXIT "
    '{"SERVICE_NAME":"com.webos.service.other","EXE":"/usr/sbin/other","STATE":3,"EXIT_CODE":139,"PID":3000}'
)


@unittest.skipUnless(shutil.which("sh"), "no sh")
class LbExit(unittest.TestCase):
    def run_fn(self, call, stdin="", *args):
        r = subprocess.run(
            ["sh", "-c", f'. "$0"; {call}', str(LIB), *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        return r.returncode, r.stdout

    def unit(self, show):
        return self.run_fn('lb_unit_clean "$1"', "", show)

    def test_unit_clean_exit(self):
        self.assertEqual(self.unit(UNIT_CLEAN)[0], 0)

    def test_unit_abnormal(self):
        for show, why in (
            # a crash: systemd restarts it (Restart=on-failure), so it runs and counts the restart
            ("Result=success ExecMainStatus=0 NRestarts=1 ActiveState=active", "not stopped"),
            ("Result=success ExecMainStatus=0 NRestarts=1 ActiveState=inactive", "was restarted"),
            # killed by SIGSEGV: the result names the signal, the status is its number
            ("Result=signal ExecMainStatus=11 NRestarts=0 ActiveState=failed", "result success"),
            ("Result=core-dump ExecMainStatus=6 NRestarts=0 ActiveState=failed", "result success"),
            ("Result=success ExecMainStatus=1 NRestarts=0 ActiveState=inactive", "status 0"),
            ("", "not stopped"),  # no answer from systemd
        ):
            rc, out = self.unit(show)
            self.assertEqual(rc, 1, show)
            self.assertIn(why, out)

    def test_hub_exits(self):
        self.assertEqual(self.run_fn("lb_hub_exits_clean", HUB_CLEAN + "\n" + OTHER + "\n")[0], 0)
        self.assertEqual(self.run_fn("lb_hub_exits_clean", "")[0], 0)  # none started by the hub
        for code in ("139", "-11", "1", "255"):
            crash = HUB_CLEAN.replace('"EXIT_CODE":0', f'"EXIT_CODE":{code}')
            rc, out = self.run_fn("lb_hub_exits_clean", HUB_CLEAN + "\n" + crash + "\n")
            self.assertEqual(rc, 1, code)
            self.assertIn(f'"EXIT_CODE":{code}', out)

    def test_crash_reports(self):
        self.assertEqual(self.run_fn("lb_crash_free", "/var/log/crashd:\n\n")[0], 0)
        rc, out = self.run_fn("lb_crash_free", "legacy-broadcast-atsc.1131.core.gz\n")
        self.assertEqual(rc, 1)
        self.assertIn("crash report", out)


if __name__ == "__main__":
    unittest.main()
