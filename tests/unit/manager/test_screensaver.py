#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Native screensaver traces and console framing; no VM, network or display."""

import base64
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar import cli, commands, core, poweroff, remote, screensaver as ss
from beanjar.commands import forward


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Trace:
    """Runtime-derived Ready -> real builtin window -> Screen Saver -> Active trace."""

    def __init__(self, clock, mode="fallback"):
        self.clock, self.mode = clock, mode
        self.calls = []
        self.sub, self.processing, self.windows = "active", None, []
        self.external_windows = []
        self.launched = self.woke = self.closed = None

    def call(self, uri, payload):
        self.calls.append((uri, payload))
        method = uri.rsplit("/", 1)[-1]
        now = self.clock.now
        if self.launched is not None and now - self.launched >= 0.5 and self.woke is None:
            self.windows = [ss.BUILTIN, *self.external_windows]
            self.sub, self.processing = "screen saver", None
        if self.closed is not None and now - self.closed >= 0.5:
            self.windows = [app for app in self.windows if app != ss.BUILTIN]
        if method == "getPowerState":
            return dict(
                returnValue=True,
                state="ACTIVE",
                **{"sub state": self.sub},
                **({"sub processing": self.processing} if self.processing else {}),
            )
        if method == "getForegroundApps":
            self.assert_payload(payload, {"extraInfo": True})
            return dict(
                returnValue=True,
                foregroundApps=[{"appId": app, "windowType": ss.WINDOW} for app in self.windows],
            )
        if method == "turnOnScreenSaver":
            self.processing = ss.READY
            if self.mode == "native":
                self.launched = now
        elif method == "running":
            return dict(
                returnValue=True, running=[{"id": ss.VIDEOADS}] if self.mode == "alive" else []
            )
        elif method == "launch":
            self.assert_payload(payload, {"id": ss.BUILTIN})
            if self.mode == "denied":
                raise core.Error("permission denied")
            self.launched = now
        elif method == "turnOnScreen":
            self.assert_payload(payload, {"reason": "remoteKey"})
            self.woke = now
            self.sub, self.processing = "active", None
        elif method == "closeByAppId":
            self.assert_payload(payload, {"id": ss.BUILTIN})
            self.closed = now
        else:
            raise AssertionError(method)
        return {"returnValue": True}

    @staticmethod
    def assert_payload(actual, expected):
        assert actual == expected, (actual, expected)

    def methods(self):
        return [uri.rsplit("/", 1)[-1] for uri, _ in self.calls]


class ControlTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.trace = Trace(self.clock)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.vm = SimpleNamespace(name="tv", run_dir=tmp.name)
        for patch in (
            mock.patch.object(ss, "require_running"),
            mock.patch.object(ss, "Native", return_value=self.trace),
            mock.patch.object(ss.time, "monotonic", self.clock.monotonic),
            mock.patch.object(ss.time, "sleep", self.clock.sleep),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def control(self, action, timeout=15):
        return ss.control(self.vm, action, timeout)

    def test_api_default_deadline(self):
        with mock.patch.object(ss, "Native", return_value=self.trace) as native:
            result = ss.control(self.vm, "stop")
        self.assertFalse(result["changed"])
        native.assert_called_once_with(self.vm, 60)

    def test_concurrent_helper_refused_through_launch_completion(self):
        with open(Path(self.vm.run_dir) / "screensaver.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(core.Error, "another screensaver command"):
                self.control("start")
        self.assertEqual(self.trace.calls, [])
        self.assertFalse(self.control("stop")["changed"])

    def test_failed_ad_fallback_then_stop_waits_for_actual_removal(self):
        result = self.control("start")
        self.assertTrue(result["fallback"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["screensaver_windows"], [ss.BUILTIN])
        self.assertGreaterEqual(self.trace.launched, 5)
        self.assertEqual(self.trace.methods().count("launch"), 1)
        result = self.control("stop")
        self.assertEqual(result["screensaver_windows"], [])
        self.assertEqual(result["power"]["sub state"], "active")
        self.assertGreaterEqual(self.clock.now - self.trace.closed, 0.5)
        self.assertEqual(self.trace.methods().count("closeByAppId"), 1)

    def test_stop_recovers_preexisting_ready(self):
        self.trace.processing = ss.READY
        result = self.control("stop")
        self.assertTrue(result["fallback"])
        self.assertIn("turnOnScreen", self.trace.methods())
        self.assertNotIn("turnOnScreenSaver", self.trace.methods())

    def test_stop_noop(self):
        self.assertFalse(self.control("stop")["changed"])
        self.assertEqual(self.trace.methods(), ["getPowerState", "getForegroundApps"])

    def test_stop_cleans_fallback_left_visible_after_home(self):
        self.trace.windows = [ss.BUILTIN]
        result = self.control("stop")
        self.assertTrue(result["changed"])
        self.assertEqual(result["screensaver_windows"], [])
        self.assertEqual(self.trace.methods().count("turnOnScreen"), 1)
        self.assertEqual(self.trace.methods().count("closeByAppId"), 1)
        self.assertGreaterEqual(self.clock.now - self.trace.closed, 0.5)

    def test_owned_wake_waits_through_recorded_screen_on_report(self):
        self.trace.sub, self.trace.windows = "screen saver", [ss.BUILTIN]
        original = self.trace.call
        reports = []

        def call(uri, payload):
            if self.trace.woke is not None:
                if self.clock.now - self.trace.woke < 0.75:
                    self.trace.sub, self.trace.processing = "screen saver", "screen on"
                else:
                    self.trace.sub, self.trace.processing = "active", None
            if uri.endswith("/closeByAppId"):
                self.assertEqual((self.trace.sub, self.trace.processing), ("active", None))
            data = original(uri, payload)
            if uri.endswith("/getPowerState"):
                reports.append(data)
            return data

        with mock.patch.object(self.trace, "call", side_effect=call):
            result = self.control("stop")
        self.assertEqual(result["screensaver_windows"], [])
        self.assertTrue(any(p.get("sub processing") == "screen on" for p in reports))
        self.assertEqual(self.trace.methods().count("turnOnScreen"), 1)
        self.assertNotIn("launch", self.trace.methods())

    def test_screen_on_report_without_our_wake_is_refused(self):
        self.trace.sub, self.trace.processing = "screen saver", "screen on"
        for action in ("start", "stop"):
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(core.Error, "unrelated native power transition"),
            ):
                self.control(action)
        self.assertNotIn("turnOnScreen", self.trace.methods())
        self.assertNotIn("closeByAppId", self.trace.methods())

    def test_unrelated_transition_after_owned_wake_is_still_refused(self):
        self.trace.sub, self.trace.windows = "screen saver", [ss.BUILTIN]
        original = self.trace.call

        def call(uri, payload):
            if self.trace.woke is not None:
                self.trace.processing = "shutdown"
            return original(uri, payload)

        with mock.patch.object(self.trace, "call", side_effect=call):
            with self.assertRaisesRegex(core.Error, "unrelated native power transition"):
                self.control("stop")
        self.assertNotIn("closeByAppId", self.trace.methods())

    def test_start_noop(self):
        self.trace.sub, self.trace.windows = "screen saver", [ss.BUILTIN]
        self.assertFalse(self.control("start")["changed"])
        self.assertNotIn("turnOnScreenSaver", self.trace.methods())

    def test_native_path_delayed_window_does_not_launch_fallback(self):
        self.trace.mode = "native"
        result = self.control("start")
        self.assertFalse(result["fallback"])
        self.assertGreaterEqual(self.clock.now, 0.5)
        self.assertNotIn("launch", self.trace.methods())

    def test_launch_denied(self):
        self.trace.mode = "denied"
        with self.assertRaisesRegex(core.Error, "permission denied"):
            self.control("start")
        self.assertEqual(self.trace.methods().count("launch"), 1)
        self.assertNotIn("turnOnScreen", self.trace.methods())

    def test_live_ad_never_taken_over(self):
        self.trace.mode = "alive"
        with self.assertRaisesRegex(core.Error, "timed out"):
            self.control("start", 7)
        self.assertEqual(self.clock.now, 7)
        self.assertIn("running", self.trace.methods())
        self.assertNotIn("launch", self.trace.methods())

    def test_window_already_exists_wait_without_takeover(self):
        self.trace.processing, self.trace.windows = ss.READY, ["native.saver"]
        with self.assertRaisesRegex(core.Error, "timed out"):
            self.control("start", 7)
        self.assertNotIn("running", self.trace.methods())
        self.assertNotIn("launch", self.trace.methods())

    def test_running_builtin_without_window_is_not_launched_again(self):
        original = self.trace.call

        def call(uri, payload):
            data = original(uri, payload)
            if uri.endswith("/running"):
                if self.trace.launched is None:
                    self.trace.launched = self.clock.now
                return dict(returnValue=True, running=[{"id": ss.BUILTIN}])
            return data

        with mock.patch.object(self.trace, "call", side_effect=call):
            result = self.control("start")
        self.assertFalse(result["fallback"])
        self.assertEqual(result["screensaver_windows"], [ss.BUILTIN])
        self.assertNotIn("launch", self.trace.methods())

    def test_real_saver_arrives_during_running_query_no_fallback(self):
        original = self.trace.call

        def call(uri, payload):
            data = original(uri, payload)
            if uri.endswith("/running"):
                self.trace.sub, self.trace.processing = "screen saver", None
                self.trace.windows = ["native.saver"]
            return data

        with mock.patch.object(self.trace, "call", side_effect=call):
            result = self.control("start")
        self.assertFalse(result["fallback"])
        self.assertEqual(result["screensaver_windows"], ["native.saver"])
        self.assertNotIn("launch", self.trace.methods())

    def test_unrelated_transition_during_running_query_no_fallback(self):
        original = self.trace.call

        def call(uri, payload):
            data = original(uri, payload)
            if uri.endswith("/running"):
                self.trace.processing = "shutdown"
            return data

        with mock.patch.object(self.trace, "call", side_effect=call):
            with self.assertRaisesRegex(core.Error, "unrelated native power transition"):
                self.control("start")
        self.assertNotIn("launch", self.trace.methods())

    def test_real_saver_races_accepted_fallback_wait_then_withdraw_only_builtin(self):
        original = self.trace.call

        def call(uri, payload):
            data = original(uri, payload)
            if uri.endswith("/launch"):
                self.trace.external_windows = ["native.saver"]
                self.trace.windows = ["native.saver"]
                self.trace.sub, self.trace.processing = "screen saver", None
            return data

        with mock.patch.object(self.trace, "call", side_effect=call):
            result = self.control("start")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["screensaver_windows"], ["native.saver"])
        self.assertEqual(self.trace.methods().count("launch"), 1)
        self.assertEqual(self.trace.methods().count("closeByAppId"), 1)
        self.assertGreaterEqual(self.trace.closed - self.trace.launched, 0.5)
        self.assertGreaterEqual(self.clock.now - self.trace.closed, 0.5)

    def test_never_close_other_screensaver(self):
        self.trace.sub, self.trace.windows = "screen saver", ["native.saver"]
        with self.assertRaisesRegex(core.Error, "timed out"):
            self.control("stop", 5)
        self.assertIn("turnOnScreen", self.trace.methods())
        self.assertNotIn("closeByAppId", self.trace.methods())

    def test_launch_reply_without_real_window_times_out(self):
        original = self.trace.call

        def call(uri, payload):
            data = original(uri, payload)
            if uri.endswith("/launch"):
                self.trace.launched = None
            return data

        with mock.patch.object(self.trace, "call", call):
            with self.assertRaisesRegex(core.Error, "timed out"):
                self.control("start", 7)
        self.assertEqual(self.trace.methods().count("launch"), 1)

    def test_native_state_without_window_is_not_success(self):
        self.trace.sub = "screen saver"
        with self.assertRaisesRegex(core.Error, "timed out"):
            self.control("start", 5)

    def test_refuse_other_power_states_and_transitions(self):
        for power in (
            {"state": "STANDBY"},
            {"state": "ACTIVE", "sub state": "screen off"},
            {"state": "ACTIVE", "sub state": "active", "sub processing": "shutdown"},
            {"state": "Active"},
            {},
        ):
            with (
                self.subTest(power=power),
                mock.patch.object(self.trace, "call", return_value=power),
            ):
                with self.assertRaisesRegex(core.Error, "native power"):
                    self.control("start")

    def test_schema_failure_before_mutation(self):
        for malformed in (
            None,
            {},
            [None],
            [{"appId": "saver"}],
            [{"appId": "saver", "windowType": 1}],
        ):
            replies = [{"state": "ACTIVE", "sub state": "active"}, {"foregroundApps": malformed}]
            with (
                self.subTest(malformed=malformed),
                mock.patch.object(self.trace, "call", side_effect=replies),
            ):
                with self.assertRaisesRegex(core.Error, "window schema"):
                    self.control("start")


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.vm = mock.Mock(name="vm")
        self.vm.name = "tv"
        self.vm.state.return_value = (core.RUNNING, "")
        self.vm.pid.return_value = 123
        self.vm.power_off_refusal.return_value = None
        for patch in (
            mock.patch.object(core, "read_pid", return_value=None),
            mock.patch.object(poweroff, "qmp_status", return_value="running"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_running(self):
        ss.require_running(self.vm)

    def test_pending_shutdown_shortcut_refused(self):
        with mock.patch.object(core, "read_pid", return_value=123):
            with self.assertRaisesRegex(core.Error, "power-off is pending"):
                ss.require_running(self.vm)

    def test_vm_not_running(self):
        for state in (core.OFF, core.STARTING, core.STOPPING):
            self.vm.state.return_value = state, ""
            with self.subTest(state=state), self.assertRaises(core.Error):
                ss.require_running(self.vm)

    def test_not_ready(self):
        self.vm.power_off_refusal.return_value = "webOS is still starting"
        with self.assertRaisesRegex(core.Error, "still starting"):
            ss.require_running(self.vm)

    def test_qmp_not_running_or_unavailable(self):
        for status in ("standby", "paused", "shutdown", None):
            with (
                self.subTest(status=status),
                mock.patch.object(poweroff, "qmp_status", return_value=status),
            ):
                with self.assertRaises(core.Error):
                    ss.require_running(self.vm)


class FrameTest(unittest.TestCase):
    def frame(self, data, rc=0, nonce="nonce"):
        raw = json.dumps(data).encode()
        return f"BJSS-{nonce}|{rc}|{base64.b64encode(raw).decode()}|END-{nonce}"

    def test_ignore_kernel_and_unrelated_json(self):
        data = {"returnValue": True, "state": "ACTIVE"}
        out = 'kernel: noise\n{"returnValue":false}\n' + self.frame(data) + "\nmore logs"
        self.assertEqual(ss.parse_frame(out, "nonce"), data)

    def test_reject_bad_frames(self):
        good = self.frame({"returnValue": True})
        for output in (
            '{"returnValue":true}',
            good[:-1],
            good + "\n" + good,
            good.replace("END-nonce", "END-other"),
            "BJSS-nonce|0|???|END-nonce",
            "BJSS-nonce|rc|e30=|END-nonce",
            "BJSS-nonce|0|aGVsbG8=|END-nonce",
            self.frame({"returnValue": True}, rc=124),
            self.frame({"returnValue": False, "errorText": "permission denied"}),
            self.frame([]),
        ):
            with self.subTest(output=output), self.assertRaises(core.Error):
                ss.parse_frame(output, "nonce")

    def test_native_shell_quoting_actor_and_encoding_in_namespace(self):
        vm = SimpleNamespace(
            run_dir="/unused", console_log="/unused/log", library=SimpleNamespace(repo=str(TOP))
        )
        payload = {"id": "quote ' ; $(false) `false`", "unicode": "日本語"}

        def send(line, **kwargs):
            words = shlex.split(line)
            self.assertEqual(words[:3], ["webos-enter", "sh", "-c"])
            self.assertEqual(words[-1], "</dev/null")
            script = words[3]
            self.assertIn("/bin/base64", script)
            self.assertIn("</dev/null", script)
            with tempfile.TemporaryDirectory() as tmp:
                luna = Path(tmp) / "luna-send"
                luna.write_text(
                    f"#!{sys.executable}\nimport json, sys\n"
                    f'assert sys.argv[1:5] == ["-n", "1", "-a", {ss.ACTOR!r}]\n'
                    f"assert json.loads(sys.argv[-1]) == {payload!r}\n"
                    'assert sys.stdin.read() == ""\nprint(json.dumps({"returnValue": True}))\n'
                )
                luna.chmod(0o755)
                result = subprocess.run(
                    ["sh", "-c", script],
                    env=dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"]),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            return "interleaved kernel log\n" + result.stdout

        with mock.patch.object(ss.console, "send", side_effect=send):
            self.assertEqual(
                ss.Native(vm, time.monotonic() + 10).call(ss.SAM + "launch", payload),
                {"returnValue": True},
            )

    def test_deadline_exhausted_does_not_send(self):
        with mock.patch.object(ss.console, "send") as send:
            with self.assertRaisesRegex(core.Error, "timed out"):
                ss.Native(mock.Mock(), time.monotonic() + 2).call(ss.POWER + "getPowerState", {})
        send.assert_not_called()

    def test_native_timeout_finishes_shell_and_closes_all_output_pipes(self):
        vm = SimpleNamespace(
            run_dir="/unused", console_log="/unused/log", library=SimpleNamespace(repo=str(TOP))
        )

        def send(line, **kwargs):
            script = shlex.split(line)[3]
            with tempfile.TemporaryDirectory() as tmp:
                luna = Path(tmp) / "luna-send"
                luna.write_text("#!/bin/sh\nexec sleep 10\n")
                luna.chmod(0o755)
                # communicate waits for inherited pipes to close too, so an abandoned
                # child would hold this open beyond the native deadline.
                result = subprocess.run(
                    ["sh", "-c", script],
                    env=dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"]),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=4,
                )
                self.assertEqual(result.returncode, 0)
                return result.stdout

        began = time.monotonic()
        with mock.patch.object(ss.console, "send", side_effect=send):
            with self.assertRaisesRegex(core.Error, "native call exited with status"):
                ss.Native(vm, began + 3.9).call(ss.POWER + "getPowerState", {})
        self.assertLess(time.monotonic() - began, 3)

    def test_console_transport_failure(self):
        vm = SimpleNamespace(
            run_dir="/unused", console_log="/unused/log", library=SimpleNamespace(repo=str(TOP))
        )
        with mock.patch.object(ss.console, "send", side_effect=OSError("no console")):
            with self.assertRaisesRegex(core.Error, "console transport failed"):
                ss.Native(vm, time.monotonic() + 10).call(ss.POWER + "getPowerState", {})


class CliTest(unittest.TestCase):
    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(args))
            except SystemExit as error:
                rc = error.code
        return rc, out.getvalue(), err.getvalue()

    def test_local(self):
        result = dict(
            action="stop", changed=False, fallback=False, power={}, screensaver_windows=[]
        )
        with (
            mock.patch.object(remote, "locate", return_value=(None, "tv")),
            mock.patch.object(commands, "Library") as library,
            mock.patch.object(ss, "control", return_value=result) as control,
        ):
            rc, out, err = self.invoke(
                "screensaver", "stop", "local/tv", "--timeout", "12s", "--json"
            )
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out), result)
        control.assert_called_once_with(library.return_value.vm.return_value, "stop", 12)

    def test_cli_default_timeout(self):
        result = dict(
            action="stop", changed=False, fallback=False, power={}, screensaver_windows=[]
        )
        with (
            mock.patch.object(remote, "locate", return_value=(None, "tv")),
            mock.patch.object(commands, "Library") as library,
            mock.patch.object(ss, "control", return_value=result) as control,
        ):
            rc, out, err = self.invoke("screensaver", "stop", "local/tv", "--json")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out), result)
        control.assert_called_once_with(library.return_value.vm.return_value, "stop", 60)

    def test_remote_rewrite(self):
        for action in ("start", "stop"):
            with (
                mock.patch.object(remote, "locate", return_value=("pi", "tv")),
                mock.patch.object(remote, "hosts", return_value={"pi": "ssh://pi"}),
                mock.patch.object(remote, "identity", return_value=None),
                mock.patch.object(forward, "run_remote", return_value=0) as run,
                mock.patch.object(ss, "control") as control,
            ):
                rc, _, _ = self.invoke(
                    "--library",
                    "/local/lib",
                    "screensaver",
                    action,
                    "--timeout",
                    "18s",
                    "pi/tv",
                    "--json",
                )
            self.assertEqual(rc, 0)
            self.assertEqual(
                run.call_args.args[1:],
                ("pi", ["screensaver", action, "--timeout", "18s", "local/tv", "--json"]),
            )
            control.assert_not_called()

    def test_timeout_validation(self):
        for value in ("nan", "inf", "0s", "-1s", "301s", "four", "12", "4999ms"):
            with self.subTest(value=value):
                rc, _, err = self.invoke("screensaver", "start", "tv", "--timeout", value, "--json")
                self.assertEqual(rc, 2)
                self.assertIn("timeout", err)


if __name__ == "__main__":
    unittest.main()
