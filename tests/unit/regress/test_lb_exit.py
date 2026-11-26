# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""tests/regress/lb-exit.sh: legacy-broadcast's clean exits (it stops itself when idle and a call
starts it again) told apart from a crash or another abnormal exit, on recorded evidence: systemd's
result for the boot instance, the hub's LSHUB_SRV_EXIT for those it started, the crash reports. And servicemanager's lifecycle
policy closing it (lb_policy_closed), which guest.sh's ready accepts in place of its tv-ready."""

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


# servicemanager's log lines (/var/log/messages) on o22n2-spacebuns (W24G 02.68.80.01) under TCG,
# the second boot of a fresh state: the policy closes legacy-broadcast with priority_lowest
SM = "2026-11-26T05:06:09.589174Z [{t}] user.info servicemanager [] servicemanager SERVICE_MANAGER_CLOSE "
LOWEST = SM + '{{"isServiceCloseRequired":"yes","reason":"priority_lowest"}} '
STOPPING = SM + '{{"service":"com.webos.service.{svc}","set_status":"stopping"}} '
CHAIN = (
    SM + '{{"where":"add_call_chain","service":"com.webos.service.legacybroadcast.broadcast",'
    '"uri":"luna://com.webos.service.legacybroadcast.broadcast/close"}} '
)
POLICY_CLOSE = "\n".join(
    (
        LOWEST.format(t="515.261023548"),
        LOWEST.format(t="515.261084613"),
        STOPPING.format(t="515.261174164", svc="legacybroadcast.broadcast"),
        CHAIN.format(t="515.261274184"),
    )
)
# the first boot of the same state: bootd launched livetv, one of the policy's dependsOn apps, so
# servicemanager closed nothing; the same priority_lowest closed fancontroller (its own policy)
KEPT = "\n".join(
    (
        LOWEST.format(t="189.258911416"),
        STOPPING.format(t="189.280022930", svc="fancontroller"),
        SM.format(t="189.491421608")
        + '{"isServiceCloseRequired":"no","service_id":"com.webos.service.legacybroadcast.broadcast",'
        '"reason":"dependent_app_is_launching"} ',
    )
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

    def test_policy_closed(self):
        rc, out = self.run_fn("lb_policy_closed", POLICY_CLOSE + "\n")
        self.assertEqual((rc, out.strip()), (0, "515.261174164"))
        # after the fancontroller close, a later lowest-priority close of legacy-broadcast counts
        rc, out = self.run_fn("lb_policy_closed", KEPT + "\n" + POLICY_CLOSE + "\n")
        self.assertEqual((rc, out.strip()), (0, "515.261174164"))

    def test_policy_kept(self):
        for text in (KEPT, "", CHAIN.format(t="1.0")):
            self.assertEqual(self.run_fn("lb_policy_closed", text + "\n")[0], 1, text)
        # a close for another reason (not the priority policy) is not the stock no-channel close
        other = POLICY_CLOSE.replace("priority_lowest", "low_memory")
        self.assertEqual(self.run_fn("lb_policy_closed", other + "\n")[0], 1)
        # the decision line alone, its service another one
        self.assertEqual(
            self.run_fn("lb_policy_closed", "\n".join(KEPT.splitlines()[:2]) + "\n")[0], 1
        )


# bootd's log on m3-goldilocks (W18R) in FirstUse: it waits 15 s for tv-ready, so core-boot-done
# came at 33.0 s by that timeout, and tvservice's tv-ready followed at 38.96 s
FIRSTUSE_BOOTD = """\
DEBUG( 18.056937883) : Event) EventCore waits event(tv-ready)/timeout(15)
PERFORMANCE( 33.004480432) : Boot) SIGNAL_core-boot-done
DEBUG( 38.957258518) : Service) Start Handle-generateSignal/Request({"name":"tv-ready"})
"""


@unittest.skipUnless(shutil.which("sh"), "no sh")
class LateTvReady(unittest.TestCase):
    """late_tv_ready: bootd's log polled each second of a fake clock (one more second per `up`)."""

    def run_late(self, log_text, start, end, grace, scale=1):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "bootd.log").write_text(log_text)
            (Path(d) / "clock").write_text(str(start))
            script = f"""
. "$0"
scale={scale}
up() {{ n=$(cat {d}/clock); echo $((n + 1)) > {d}/clock; echo "$n.0"; }}
in_webos() {{ sh -c "$(echo "$1" | sed 's|/var/log/bootd.log|{d}/bootd.log|')"; }}
sleep() {{ :; }}
late_tv_ready {end} {grace}
echo "rc=$?"
"""
            r = subprocess.run(
                ["sh", "-c", script, str(LIB)], capture_output=True, text=True, check=False
            )
            return r.stdout.split()

    def test_arrives_after_timeout(self):
        # the tv-ready line is in the log by the time it is looked for
        self.assertEqual(self.run_late(FIRSTUSE_BOOTD, 33, 300, 30), ["38.957258518", "rc=0"])

    def test_never_comes(self):
        # no tv-ready line: gives up after the grace
        no_line = "\n".join(FIRSTUSE_BOOTD.splitlines()[:2]) + "\n"
        self.assertEqual(self.run_late(no_line, 33, 300, 30), ["rc=1"])

    def test_bounded_by_end_and_scaled(self):
        # the window closes at END even if the grace is longer
        self.assertEqual(self.run_late("", 33, 36, 30), ["rc=1"])
        # the time scale stretches the grace: the fake clock ticks once per call of up, so the
        # final clock counts the polls (one call for the start, one per poll)
        for scale, polls in ((1, 30), (4, 120)):
            import tempfile

            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "clock").write_text("0")
                script = f"""
. "$0"
scale={scale}
up() {{ n=$(cat {d}/clock); echo $((n + 1)) > {d}/clock; echo "$n.0"; }}
in_webos() {{ echo; }}
sleep() {{ :; }}
late_tv_ready 100000 30
cat {d}/clock
"""
                r = subprocess.run(
                    ["sh", "-c", script, str(LIB)], capture_output=True, text=True, check=False
                )
                self.assertEqual(int(r.stdout.split()[-1]), polls + 1, (scale, r.stdout))


if __name__ == "__main__":
    unittest.main()
