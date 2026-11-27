# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The VM lifecycle: its monitor, background power-off and the console (lifecycle.py,
poweroff.py, console.py), without a VM.

ConsoleTest types a line into a fake serial console."""

import contextlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock

import testunits
from beanjar import base
from beanjar.access import console
from beanjar.runtime import launch, lifecycle, poweroff, unit, vmunits
from beanjar.vm import library
from launchkit import TOP


class LifecycleTest(unittest.TestCase):
    """unit.run's restart loop (lifecycle.py) with the fake QEMU as the launcher (RUN): a guest
    restart starts it again in the same unit, a power-off ends the unit, a full disk makes QEMU quit,
    and the last QEMU's helpers have ended before the next one starts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-lifecycle-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run_dir, self.logs = os.path.join(self.tmp, "run"), os.path.join(self.tmp, "logs")
        os.makedirs(self.run_dir)
        os.makedirs(self.logs)

    def unit_run(self, scenario, **extra):
        env = {k: v for k, v in os.environ.items() if k not in launch.RUNTIME_VARS}
        env.pop("RUNTIME_DIRECTORY", None)
        env.update(
            RUN=os.path.join(TOP, "tests", "fakes", "qemu-system-fake"),
            RUN_DIR=self.run_dir,
            LOG_DIR=self.logs,
            FAKE_VM_DIR=self.tmp,
            FAKE_LIFECYCLE=scenario,
            PYTHONPATH=os.path.join(TOP, "host", "core")
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
            **extra,
        )
        p = subprocess.Popen(
            [sys.executable, "-m", "beanjar.runtime.unit", "run"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            rc = p.wait(timeout=60)
        finally:
            if p.poll() is None:
                testunits.end(p.pid, group=True)
                p.wait()
        with open(os.path.join(self.logs, "qemu.out")) as f:
            out = f.read()
        with open(os.path.join(self.run_dir, "fake-runs")) as f:
            runs = int(f.read())
        return rc, runs, out

    def commands(self):
        with open(os.path.join(self.run_dir, "fake-lifecycle.jsonl")) as f:
            return [json.loads(line) for line in f]

    def test_restart_then_power_off(self):
        """Two guest restarts start QEMU twice more; the power-off after them ends the unit, 0. The
        console log of the QEMU before is kept as console.prev.log, and nothing is left marked."""
        rc, runs, out = self.unit_run("reset,reset,poweroff")
        self.assertEqual((rc, runs), (0, 3))
        self.assertEqual(out.count("beanjar: the guest restarted (guest-reset)"), 2, out)
        self.assertIn("restart 2;", out)
        self.assertEqual(out.count("s after the guest's restart"), 2, out)
        self.assertEqual(lifecycle.restarts(self.run_dir), 2)
        self.assertFalse(lifecycle.restarting(self.run_dir))
        self.assertTrue(os.path.exists(os.path.join(self.logs, "console.prev.log")))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, lifecycle.SOCK)))

    def test_runtime_directory_kept(self):
        """The unit's RuntimeDirectory lasts across a restart: its reason, the record of USB devices;
        the network's socket directory in it is the launcher's to remove (netns.py does)."""
        rt = os.path.join(self.tmp, "rt")
        os.makedirs(rt)
        with open(os.path.join(rt, vmunits.REASON_FILE), "w") as f:
            f.write('{"text": "t"}')
        rc, runs, out = self.unit_run("reset,poweroff", RUNTIME_DIRECTORY=rt)
        self.assertEqual((rc, runs), (0, 2), out)
        self.assertTrue(os.path.exists(os.path.join(rt, vmunits.REASON_FILE)))
        self.assertFalse(os.path.exists(os.path.join(rt, "netns")))

    def test_fresh_state_kept_across_restarts(self):
        """STATE=none: one temporary state for the whole run. Every start gets the same
        RUN_DIR/state/.fresh as FRESH_STATE and finds what the starts before it wrote there (as a
        set keeps its writes across a reboot); a leftover of an earlier run is gone before the
        first start, and the run's end removes the directory."""
        fresh = os.path.join(self.run_dir, "state", ".fresh")
        os.makedirs(fresh)
        with open(os.path.join(fresh, "stale.img"), "w") as f:
            f.write("an earlier run's\n")
        rc, runs, out = self.unit_run("reset,reset,poweroff", STATE="none")
        self.assertEqual((rc, runs), (0, 3), out)
        with open(os.path.join(self.run_dir, "fake-fresh.jsonl")) as f:
            seen = [json.loads(line) for line in f]
        self.assertEqual([s["dir"] for s in seen], [fresh] * 3)
        self.assertEqual([s["files"] for s in seen], [[], ["run0.img"], ["run0.img", "run1.img"]])
        self.assertFalse(os.path.exists(fresh), "the run's end discards its temporary state")

    def test_persistent_state_has_no_fresh_state(self):
        state = os.path.join(self.tmp, "state")
        rc, runs, out = self.unit_run("reset,poweroff", STATE=state)
        self.assertEqual((rc, runs), (0, 2), out)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "fake-fresh.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "state", ".fresh")))

    def test_post_discards_a_killed_runs_fresh_state(self):
        """ExecStopPost (`unit post`) removes the temporary state a run killed before its own
        cleanup (SIGKILL) left, and leaves a persistent state alone."""
        fresh = os.path.join(self.run_dir, "state", ".fresh")
        os.makedirs(fresh)
        env = dict(RUN_DIR=self.run_dir, LOG_DIR=self.logs)
        with mock.patch.dict(os.environ, dict(env, STATE=os.path.join(self.tmp, "st"))):
            self.assertEqual(unit.main(["post"]), 0)
        self.assertTrue(os.path.isdir(fresh))
        with mock.patch.dict(os.environ, dict(env, STATE="none")):
            self.assertEqual(unit.main(["post"]), 0)
        self.assertFalse(os.path.exists(fresh))

    def test_power_off(self):
        rc, runs, out = self.unit_run("poweroff")
        self.assertEqual((rc, runs), (0, 1))
        self.assertNotIn("restarted", out)

    def test_disk_full_quits(self):
        """BLOCK_IO_ERROR with nospace: QEMU is told to quit, says why, the unit ends failed and is
        not started again."""
        rc, runs, out = self.unit_run("enospc,poweroff")
        self.assertEqual((rc, runs), (1, 1))
        self.assertIn("beanjar: the host's disk is full: writing state failed", out)
        self.assertIn("QEMU quits instead of pausing the VM", out)
        self.assertEqual([c["execute"] for c in self.commands()], ["qmp_capabilities", "quit"])

    def test_helpers_end_before_the_next_start(self):
        """The last QEMU's helper (an orphan that outlives it by FAKE_ORPHAN s) has ended before the
        next QEMU starts; the wait is said."""
        rc, runs, out = self.unit_run("reset,poweroff", FAKE_ORPHAN="1")
        self.assertEqual((rc, runs), (0, 2))
        m = re.search(r"helpers ended in ([0-9.]+) s\)", out)
        self.assertTrue(m, out)
        self.assertGreaterEqual(float(m[1]), 0.9)

    def test_orphans_past_the_wait_are_killed(self):
        with (
            mock.patch.object(unit, "children", side_effect=[[1234], [1234]]),
            mock.patch.object(unit.os, "waitpid", return_value=(0, 0)),
            mock.patch.object(unit.os, "kill") as kill,
        ):
            self.assertEqual(unit.reap_orphans(timeout=0), [1234])
        kill.assert_called_once_with(1234, signal.SIGKILL)


class LifecycleMonitorTest(unittest.TestCase):
    """lifecycle.Monitor against a QEMU stand-in on its socket: the recorded USB devices go back in
    their order before on_ready, a restart's SHUTDOWN is told from a power-off's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-monitor-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "l.sock")

    def qemu(self, events, monitor):
        """Connect as QEMU, answer every command, then send events; the commands seen."""
        c = socket.socket(socket.AF_UNIX)
        c.connect(self.path)
        f = c.makefile("rw")
        f.write(json.dumps({"QMP": {}}) + "\n")
        f.flush()
        seen = []
        c.settimeout(2)
        try:
            while True:
                line = f.readline()
                if not line:
                    break
                seen.append(json.loads(line))
                f.write(json.dumps({"return": {}}) + "\n")
                f.flush()
                if len(seen) == 1 + len(lifecycle.replay_commands(self.devices)):
                    break
        except OSError:
            pass
        for e in events:
            f.write(json.dumps(e) + "\n")
            f.flush()
        time.sleep(0.2)
        c.close()
        monitor.close()
        return seen

    def test_replay_and_reset(self):
        self.devices = [
            {"id": "1", "kind": "storage", "image": "/x/stick.img", "readonly": True},
            {"id": "cam", "kind": "redir", "socket": "/r/usb-cam.sock"},
        ]
        ready, reset = [], []
        m = lifecycle.Monitor(
            self.path,
            lambda _line: None,
            devices=lambda: self.devices,
            on_reset=lambda: reset.append(1),
            on_ready=lambda: ready.append(1),
        ).start()
        seen = self.qemu(
            [{"event": "SHUTDOWN", "data": {"guest": True, "reason": "guest-reset"}}], m
        )
        self.assertEqual(
            [c["execute"] for c in seen],
            ["qmp_capabilities", "blockdev-add", "device_add", "chardev-add", "device_add"],
        )
        self.assertEqual(seen[1]["arguments"]["file"]["filename"], "/x/stick.img")
        self.assertTrue(seen[1]["arguments"]["read-only"])
        self.assertEqual(seen[2]["arguments"]["driver"], "usb-storage")
        self.assertEqual(
            seen[3]["arguments"]["backend"]["data"]["addr"]["data"]["path"], "/r/usb-cam.sock"
        )
        self.assertEqual((ready, reset, m.reset), ([1], [1], "guest-reset"))

    def test_power_off_is_not_a_restart(self):
        self.devices = []
        m = lifecycle.Monitor(self.path, lambda _line: None).start()
        self.qemu([{"event": "SHUTDOWN", "data": {"guest": True, "reason": "guest-shutdown"}}], m)
        self.assertIsNone(m.reset)
        self.assertTrue(m.connected)

    def test_no_qemu(self):
        """QEMU never connected (it failed to start): close() still returns, nothing is seen."""
        m = lifecycle.Monitor(self.path, lambda _line: None).start()
        m.close()
        self.assertFalse(m.connected)
        self.assertFalse(os.path.exists(self.path))


class BgPowerOffTest(unittest.TestCase):
    """The launcher's stop --power-off (Bg.power_off): the call typed on the console and its
    run/power-off sentinel, a console that is gone, a QEMU that exits, and one that does not
    inside the timeout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-power-off-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        run, logs = os.path.join(self.tmp, "run"), os.path.join(self.tmp, "logs")
        os.makedirs(run)
        os.makedirs(logs)
        with open(os.path.join(run, "qemu.pid"), "w") as f:
            f.write("123\n")
        self.bg = launch.Bg(
            dict(os.environ, RUN_DIR=run, LOG_DIR=logs, UNIT="beanjar-vm-test-power-off")
        )
        self.sent = os.path.join(run, poweroff.POWER_OFF_SENT)

    def power_off(self, timeout, alive, reply='{ "returnValue": true }'):
        """Bg.power_off with QEMU (pid 123) alive while `alive` says so, and the console stubbed to
        print reply after the call."""
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(launch.unit, "alive", side_effect=alive),
            mock.patch.object(launch.unit, "comm", return_value="qemu-system-aarch64"),
            mock.patch.object(launch.console, "send", return_value=reply) as send,
            mock.patch.object(launch.time, "sleep"),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            ok = self.bg.power_off(timeout)
        return ok, send, out.getvalue(), err.getvalue()

    def test_no_console(self):
        """QEMU runs but has no serial console: nothing is sent, and the signal is refused."""
        ok, send, out, err = self.power_off(5, [True])
        self.assertEqual(ok, library.STOP_REFUSED)
        send.assert_not_called()
        self.assertEqual(err, "power-off: no serial console\n")
        self.assertFalse(os.path.exists(self.sent), "no call: no sentinel")

    def test_qemu_exits_and_the_call_is_not_repeated(self):
        """The call goes to the console, QEMU exits by itself, and a second stop waits for it
        without sending the call again."""
        srv = socket.socket(socket.AF_UNIX)  # run/console.sock, which console.send would use
        srv.bind(self.bg.sock)
        srv.close()
        ok, send, out, _err = self.power_off(5, [True, True, False])
        self.assertEqual(ok, 0)
        send.assert_called_once_with(poweroff.POWER_OFF_CALL, 10, self.bg.sock, self.bg.log)
        with open(self.sent) as f:
            self.assertEqual(f.read(), "123\n")
        self.assertIn("power-off: sent; tvpowerd:", out)
        self.assertIn("power-off: QEMU exited", out)
        # the same QEMU: wait again, and say so; no second call
        ok, send, out, _err = self.power_off(5, [True, True, False])
        self.assertEqual(ok, 0)
        send.assert_not_called()
        self.assertIn("power-off: sent before; waiting for QEMU to exit", out)

    def test_timeout(self):
        """QEMU still runs at the timeout: STOP_TIMED_OUT, its pid in the sentinel for a later stop."""
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.bg.sock)
        srv.close()
        ok, send, _out, _err = self.power_off(0, lambda *_: True)
        self.assertEqual(ok, library.STOP_TIMED_OUT)
        send.assert_called_once()
        with open(self.sent) as f:
            self.assertEqual(f.read(), "123\n")

    def test_refused_call_falls_back_at_once(self):
        """An explicit failure (returnValue false, an error from luna-send, no webOS) returns at
        once without waiting for QEMU, and leaves no sentinel: nothing is pending."""
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.bg.sock)
        srv.close()
        for reply in (
            '{ "returnValue": false, "errorText": "denied" }',
            '{"errorCode":-1,"errorText":"Service does not exist: com.webos.service.tvpower",'
            '"returnValue":false}',
            "webOS namespace is not running\n",
        ):
            with self.subTest(reply):
                ok, send, out, err = self.power_off(90, lambda *_: True, reply)
                self.assertEqual(ok, library.STOP_REFUSED)
                send.assert_called_once()
                self.assertIn("power-off: webOS refused the call", err)
                self.assertFalse(os.path.exists(self.sent))

    def test_no_reply_is_waited_out(self):
        """No reply, or a luna-send that timed out, is not a refusal: webOS powers off after such
        a call (W22O, 25 s later), so QEMU is waited for, up to the timeout."""
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.bg.sock)
        srv.close()
        for reply in ("", "Terminated\n"):
            with self.subTest(reply):
                ok, send, out, _err = self.power_off(5, [True, True, True, False], reply)
                self.assertEqual(ok, 0, "QEMU exited by itself")
                self.assertIn("power-off: sent; tvpowerd: no reply", out)
                self.assertIn("power-off: QEMU exited", out)
                self.assertTrue(os.path.exists(self.sent), "the signal is pending")
                os.unlink(self.sent)
        ok, send, out, _err = self.power_off(0, lambda *_: True, "")
        self.assertEqual(ok, library.STOP_TIMED_OUT, "no reply and QEMU stays: the timeout")


class ConsoleTest(unittest.TestCase):
    def test_send(self):
        warnings.simplefilter("ignore", ResourceWarning)  # the bridge outlives its Popen by design
        tmp = tempfile.mkdtemp(prefix="beanjar-launch-")
        sock, log = os.path.join(tmp, "console.sock"), os.path.join(tmp, "console.log")
        with open(log, "w") as f:
            f.write("before\n")
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(sock)
        srv.listen(1)

        def serve():  # a serial console with a shell: the echo, then `echo`'s output
            c, _ = srv.accept()
            buf = b""
            with c, open(log, "a") as out:
                while data := c.recv(4096):
                    buf += data
                    while b"\n" in buf:
                        ln, buf = buf.split(b"\n", 1)
                        ln = ln.decode()
                        out.write(f"/ # {ln}\r\n")
                        if ln == "echo hi":  # a background job's notice before the output
                            out.write("[1]+  Done                       sleep 1\r\n")
                        if ln.startswith("echo "):
                            out.write(ln[5:].replace('"', "") + "\r\n")
                        out.flush()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            t0 = time.monotonic()
            self.assertEqual(console.send("echo hi", 5, sock, log), "hi\n")
            self.assertLess(time.monotonic() - t0, 4, "returns at the end marker, not after WAIT")
            bridge = base.read_pid(sock + ".in.pid")
            self.assertTrue(base.proc_alive(bridge, "beanjar.access.console"))
            self.assertEqual(console.send("second", 5, sock, log), "")
            self.assertEqual(base.read_pid(sock + ".in.pid"), bridge, "one bridge per console")
        finally:
            srv.close()
            bridge = base.read_pid(sock + ".in.pid")
            if bridge:
                testunits.end(bridge, group=True)
            shutil.rmtree(tmp, ignore_errors=True)
