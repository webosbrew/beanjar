#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar log show NAME FILE: a VM's host-side log, here and forwarded to another host; no VM,
network or display."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar import cli, commands, remote
from beanjar.commands import forward


class LogShow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        for sub in ("run", "logs"):
            os.mkdir(os.path.join(self.dir, sub))
        self.write("run/media-helper.log", b"decode 3 video session=1 decoded=42\n\xff\n")
        self.write("logs/console.log", b"boot\n")
        self.write("run/mitmweb.json", b'{"password": "secret"}')
        os.symlink("/etc/passwd", os.path.join(self.dir, "run", "link.log"))
        self.vm = SimpleNamespace(name="tv", dir=self.dir)

    def write(self, file, data):
        with open(os.path.join(self.dir, file), "wb") as f:
            f.write(data)

    def invoke(self, *args):
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        err = io.StringIO()
        with (
            mock.patch.object(remote, "locate", return_value=(None, "tv")),
            mock.patch.object(commands, "Library") as library,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            library.return_value.vm.return_value = self.vm
            try:
                rc = cli.main(list(args))
            except SystemExit as error:
                rc = error.code
        out.flush()
        return rc, out.buffer.getvalue(), err.getvalue()

    def test_bytes(self):
        rc, out, err = self.invoke("log", "show", "tv", "run/media-helper.log")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, b"decode 3 video session=1 decoded=42\n\xff\n")

    def test_json(self):
        rc, out, err = self.invoke("log", "show", "tv", "logs/console.log", "--json")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            json.loads(out),
            {
                "name": "tv",
                "file": "logs/console.log",
                "path": os.path.join(self.dir, "logs/console.log"),
                "text": "boot\n",
            },
        )

    def test_json_replaces_invalid_utf8(self):
        rc, out, _err = self.invoke("log", "show", "tv", "run/media-helper.log", "--json")
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["text"].endswith("�\n"))

    def test_not_a_log(self):
        for file in (
            "run/mitmweb.json",
            "identity/vnc.password",
            "run/../identity/x.log",
            "run/.hidden.log",
            "logs/saved/console.log",
            "/etc/x.log",
            "media-helper.log",
        ):
            with self.subTest(file=file):
                rc, out, err = self.invoke("log", "show", "tv", file)
                self.assertEqual((rc, out), (2, b""))
                self.assertIn("FILE is run/NAME or logs/NAME", err)

    def test_missing_or_not_regular(self):
        for file in ("run/tuner.log", "run/link.log"):
            with self.subTest(file=file):
                rc, out, err = self.invoke("log", "show", "tv", file)
                self.assertEqual((rc, out), (1, b""))
                self.assertIn(f"tv has no {file}", err)

    def test_rotated(self):
        self.write("run/media-helper.log.1", b"old\n")
        rc, out, _err = self.invoke("log", "show", "tv", "run/media-helper.log.1")
        self.assertEqual((rc, out), (0, b"old\n"))

    def test_no_verb(self):
        rc, _out, err = self.invoke("log")
        self.assertEqual(rc, 2)
        self.assertIn("show", err)


class LogShowRemote(unittest.TestCase):
    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(args))
            except SystemExit as error:
                rc = error.code
        return rc, out.getvalue(), err.getvalue()

    def test_forwarded(self):
        for extra, words in (
            ((), ["log", "show", "local/tv", "run/media-helper.log"]),
            (("--json",), ["log", "show", "local/tv", "run/media-helper.log", "--json"]),
        ):
            with (
                self.subTest(extra=extra),
                mock.patch.object(remote, "locate", return_value=("pi", "tv")),
                mock.patch.object(forward, "run_remote", return_value=0) as run,
                mock.patch.object(commands, "Library") as library,
            ):
                rc, _out, _err = self.invoke(
                    "--library",
                    "/local/lib",
                    "log",
                    "show",
                    "pi/tv",
                    "run/media-helper.log",
                    *extra,
                )
            self.assertEqual(rc, 0)
            self.assertEqual(run.call_args.args[1:], ("pi", words))
            library.return_value.vm.assert_not_called()

    def test_relayed(self):
        """The host's JSON comes back as it is, over the one transport (remote.transport)."""
        data = {"name": "tv", "file": "run/x.log", "path": "/there/run/x.log", "text": "a\n"}
        with (
            mock.patch.object(remote, "locate", return_value=("pi", "tv")),
            mock.patch.object(remote, "hosts", return_value={"pi": "ssh://pi"}),
            mock.patch.object(
                remote, "transport", return_value=(0, json.dumps(data).encode(), "")
            ) as transport,
        ):
            out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            with contextlib.redirect_stdout(out):
                rc = cli.main(["log", "show", "pi/tv", "run/x.log", "--json"])
            out.flush()
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.buffer.getvalue()), data)
        argv = transport.call_args.args[0]
        self.assertEqual(argv[-1].split()[-5:], ["log", "show", "local/tv", "run/x.log", "--json"])


if __name__ == "__main__":
    unittest.main()
