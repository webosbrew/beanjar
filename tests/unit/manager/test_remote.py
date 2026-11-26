#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The CLI's remote hosts (host/core/beanjar/remote.py, cli.py): the hosts table, URIs, name
resolution, the ssh command line, list merging and forwarding. The ssh runner (remote.transport)
is replaced: no network. One test asks OpenSSH for its effective configuration with -G (no
connection). `meson test -C build/host --suite manager`, or run directly."""

import contextlib
import io
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import typing
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
from beanjar import ares, cli, commands, core, framing, hostconfig, inspector, pipe, remote  # noqa: E402
from beanjar.commands import vms as vm_commands  # noqa: E402
import subprocess  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + (1280).to_bytes(4, "big") + (720).to_bytes(4, "big")


def row(name, **kw):
    return {"name": name, "state": "Off", "detail": "", "platform": "webOS 1", "mem": 1, "smp": 4,
            "display": "dbus", "network": "nat+mitm", "address": "127.0.1.1", "lan_address": None,
            "uptime": None, "firmware": "fw", "fw_external": False, "fw": "/fw", "viewer": None,
            "vnc": None, "pid": None, "tag": "t", "gpu": None, **kw}  # fmt: skip


class Fake:
    """remote.transport: answers by host and command; records the calls."""

    def __init__(self, hosts):
        self.hosts = hosts  # {host name: {"vms": [names], "rows": [...], "down": reason}}
        self.calls = []
        self.reply = None  # (rc, out, err) overriding the answer of a non-probe command

    def __call__(self, argv, stdin=None, on_stderr=None, timeout=None):
        host = argv[argv.index("--") + 1].removesuffix(".example")
        remote_argv = argv[argv.index("--") + 2]
        self.calls.append((host, remote_argv, stdin))
        h = self.hosts[host]
        if h.get("down"):
            return 255, b"", f"ssh: {h['down']}\n"
        words = remote_argv.split()
        # a host with hosts of its own ("indirect"): a bare name or a plain list reaches theirs
        if words[1:3] == ["config", "show"]:
            name = words[3].removeprefix("local/")
            found = h.get("vms", []) + (h.get("indirect_vms", []) if name == words[3] else [])
            if name in found:
                return 0, b"{}", ""
            return 1, b"", json.dumps({"error": f"no VM named {name}"}) + "\n"
        if words[1] == "list":
            if "rows" not in h:
                return 0, b"not json", ""
            if h.get("old"):
                return 2, b"", "beanjar: error: unrecognized arguments: --local\n"
            indirect = [] if "--local" in words else h.get("indirect_rows", [])
            return 0, json.dumps(h["rows"] + indirect).encode(), ""
        rc, out, err = self.reply or (0, b'{"ok": true}\n', "progress\n")
        for line in err.splitlines():
            if on_stderr:
                on_stderr(line)
        return rc, out, err


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        env = {
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
            "BEANJAR_LIBRARY": os.path.join(self.tmp, "lib"),
            "XDG_RUNTIME_DIR": self.tmp,
        }
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.fake = Fake({})
        patch = mock.patch.object(remote, "transport", self.fake)
        patch.start()
        self.addCleanup(patch.stop)

    def beanjar(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        out.buffer = io.BytesIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:  # argparse's usage error
                rc = e.code
        return rc, out.getvalue() + out.buffer.getvalue().decode("latin-1"), err.getvalue()

    def add(self, **hosts):
        for name, spec in hosts.items():
            self.fake.hosts[name] = spec
            remote.add_host(name, f"ssh://{name}.example")


class HostsTest(Base):
    def test_uri(self):
        t = remote.parse_uri("ssh://user@vmhost:2222")
        self.assertEqual(tuple(t), ("user", "vmhost", 2222))
        self.assertEqual(tuple(remote.parse_uri("ssh://alias")), (None, "alias", None))
        self.assertEqual(remote.parse_uri("ssh://u@[::1]:22").host, "::1")
        for bad in (
            "http://x", "x", "ssh://", "ssh://-oProxyCommand=x", "ssh://u@h/path", "ssh://h:0",
            "ssh://h:70000", "ssh://-u@h", "ssh://h:x", "ssh://u@h extra", "", None,
        ):  # fmt: skip
            with self.subTest(bad), self.assertRaises(core.Error):
                remote.parse_uri(bad)

    def test_add_remove_list(self):
        rc, out, _ = self.beanjar("host", "list")
        self.assertEqual(rc, 0)
        self.assertIn("no hosts", out)
        self.assertEqual(self.beanjar("host", "add", "pi", "ssh://me@pi")[0], 0)
        self.assertEqual(self.beanjar("host", "add", "b2", "ssh://b")[0], 0)
        rc, out, _ = self.beanjar("host", "list", "--json")
        self.assertEqual(
            json.loads(out),
            [{"name": "pi", "uri": "ssh://me@pi"}, {"name": "b2", "uri": "ssh://b"}],
        )
        self.assertIn("ssh://me@pi", self.beanjar("host", "list")[1])
        # a duplicate, a reserved or invalid name, an invalid URI: refused, nothing written
        for argv in (("add", "pi", "ssh://x"), ("add", "local", "ssh://x"), ("add", "a/b", "ssh://x"),
                     ("add", "_x", "ssh://x"), ("add", "x", "http://x"), ("remove", "nope")):  # fmt: skip
            with self.subTest(argv):
                self.assertEqual(self.beanjar("host", *argv)[0], 1)
        self.assertEqual(len(remote.hosts()), 2)
        self.assertEqual(self.beanjar("host", "remove", "pi")[0], 0)
        self.assertEqual(list(remote.hosts()), ["b2"])

    def test_settings_kept(self):
        """host config keeps the hosts table, and adding a host keeps max_running."""
        self.beanjar("host", "config", "set", "max_running=3")
        self.beanjar("host", "add", "pi", "ssh://pi")
        self.beanjar("host", "config", "set", "max_running=4")
        self.assertEqual(hostconfig.global_config()["max_running"], 4)
        self.assertEqual(list(remote.hosts()), ["pi"])
        self.beanjar("host", "remove", "pi")
        self.assertEqual(hostconfig.global_config(), {"max_running": 4})

    def test_bad_table(self):
        os.makedirs(hostconfig.global_config_dir())
        with open(hostconfig.global_config_path(), "w") as f:
            f.write('[hosts.pi]\nuri = "ftp://x"\n')
        with self.assertRaisesRegex(core.Error, r"\[hosts.pi\]"):
            remote.hosts()


class SshTest(Base):
    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH is not installed")
    def test_shared_effective_config(self):
        """Separate CLI calls use the same expanded socket; remote commands are not its key."""

        def config(words, uri="ssh://viewer@board.example:2222"):
            argv = remote.ssh_argv(uri, words)
            # Ignore workstation configuration and print settings without opening a connection.
            result = subprocess.run(
                [argv[0], "-G", "-F", os.devnull, *argv[1:]],
                capture_output=True,
                text=True,
                check=True,
            )
            return dict(line.split(None, 1) for line in result.stdout.splitlines() if " " in line)

        listing = config(["list", "--json"])
        screenshot = config(["screenshot", "tv", "-", "--frame", "384x216"])
        self.assertEqual(listing["controlmaster"], "auto")
        self.assertEqual(listing["controlpersist"], "60")
        self.assertEqual(listing["controlpath"], screenshot["controlpath"])
        self.assertEqual(os.path.dirname(listing["controlpath"]), remote.control_dir())
        self.assertNotIn("%C", listing["controlpath"], "OpenSSH expands the connection identity")
        other = config(["list"], uri="ssh://viewer@other.example:2222")
        self.assertNotEqual(listing["controlpath"], other["controlpath"])

    def test_argv(self):
        argv = remote.ssh_argv("ssh://me@pi:2222", ["show", "tv 1", "--json"], control="/run/c")
        self.assertEqual(argv[0], "ssh")
        for opt in ("BatchMode=yes", "ConnectTimeout=5", "ControlMaster=auto", "ControlPersist=60",
                    "ControlPath=/run/c/%C"):  # fmt: skip
            self.assertIn(opt, argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[argv.index("-l") + 1], "me")
        self.assertEqual(argv[-3:], ["--", "pi", "beanjar show 'tv 1' --json"])
        plain = remote.ssh_argv("ssh://alias", ["list"], control="/c")
        self.assertNotIn("-p", plain)
        self.assertNotIn("-l", plain)

    def test_control_dir(self):
        path = remote.control_dir()
        self.assertEqual(path, os.path.join(self.tmp, "beanjar-ssh"))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)

    def test_rewrite(self):
        r = remote.rewrite
        self.assertEqual(
            r(["--library", "L", "stop", "pi/tv", "--unplug"], 1, "pi/tv", "tv"),
            ["stop", "local/tv", "--unplug"],
        )
        self.assertEqual(
            r(["vnc", "start", "--port", "1", "pi/tv", "-x"], 2, "pi/tv", "tv"),
            ["vnc", "start", "--port", "1", "local/tv", "-x"],
        )
        self.assertEqual(
            r(["--repo=R", "start", "start"], 1, "start", "other"), ["start", "local/other"]
        )


class LocateTest(Base):
    def locate(self, spec, local=()):
        warnings = []
        return remote.locate(spec, lambda n: n in local, warnings.append), warnings

    def test_no_hosts(self):
        self.assertEqual(self.locate("tv")[0], (None, "tv"))
        self.assertEqual(self.fake.calls, [])

    def test_explicit(self):
        self.add(pi={"vms": []})
        self.assertEqual(self.locate("local/tv")[0], (None, "tv"))
        self.assertEqual(self.locate("pi/tv")[0], ("pi", "tv"))
        self.assertEqual(self.fake.calls, [], "HOST/NAME asks nobody")
        for bad in ("zz/tv", "pi/a/b", "pi/"):
            with self.subTest(bad), self.assertRaises(core.Error):
                self.locate(bad)

    def test_bare(self):
        self.add(pi={"vms": ["remote-only", "both"]}, nuc={"vms": ["both", "nuc-only"]})
        local = ("local-only", "both")
        self.assertEqual(self.locate("local-only", local)[0], (None, "local-only"))
        self.assertEqual(self.locate("remote-only", local)[0], ("pi", "remote-only"))
        self.assertEqual(self.locate("nuc-only", local)[0], ("nuc", "nuc-only"))
        with self.assertRaises(core.Error) as ctx:
            self.locate("both", local)
        self.assertIn("local/both, pi/both, nuc/both", str(ctx.exception))
        with self.assertRaisesRegex(core.Error, "^no VM named nowhere$"):
            self.locate("nowhere", local)
        self.assertEqual(self.locate("bad name!")[0], (None, "bad name!"))

    def test_pinned_library(self):
        self.add(pi={"vms": ["both"]}, nuc={"vms": ["both"]})
        found = remote.locate("both", lambda n: True, lambda _: None, pinned=True)
        self.assertEqual(found, (None, "both"))
        found = remote.locate("pi/both", lambda n: True, print, pinned=True)
        self.assertEqual(found, ("pi", "both"))
        self.assertEqual(self.fake.calls, [], "pinned asks no host")

    def test_host_down(self):
        self.add(pi={"vms": [], "down": "Connection refused"}, nuc={"vms": ["tv"]})
        (found, warnings) = self.locate("tv")
        self.assertEqual(found, ("nuc", "tv"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("host pi: Connection refused", warnings[0])
        with self.assertRaisesRegex(core.Error, r"no VM named x \(could not ask pi\)"):
            self.locate("x")


class CommandsTest(Base):
    def test_forward_status_and_output(self):
        self.add(pi={"vms": ["tv"]})
        rc, out, err = self.beanjar("stop", "pi/tv", "--unplug")
        self.assertEqual((rc, out), (0, '{"ok": true}\n'))
        self.assertEqual(err, "pi: progress\n")
        self.assertEqual(self.fake.calls[-1][:2], ("pi", "beanjar stop local/tv --unplug"))
        rc, out, err = self.beanjar(
            "--library", os.path.join(self.tmp, "lib2"), "stop", "pi/tv", "--json"
        )
        self.assertEqual(self.fake.calls[-1][1], "beanjar stop local/tv --json")
        self.assertEqual(err, "progress\n", "--json: the remote's stderr as it is")
        for status in (1, 2):
            self.fake.reply = (status, b"", '{"error": "nope"}\n')
            self.assertEqual(self.beanjar("wipe", "pi/tv")[0], status)

    def test_snapshot(self):
        """snapshot VERB HOST/NAME [SNAP] runs on the VM's host, where its snapshots are."""
        self.add(pi={"vms": ["tv"]})
        for words, remote_line in (
            (("snapshot", "create", "pi/tv", "base"), "beanjar snapshot create local/tv base"),
            (("snapshot", "list", "pi/tv", "--json"), "beanjar snapshot list local/tv --json"),
            (
                ("--json", "snapshot", "show", "pi/tv", "base"),
                "beanjar snapshot show local/tv base --json",
            ),
            (("snapshot", "reset", "pi/tv", "base"), "beanjar snapshot reset local/tv base"),
            (("snapshot", "delete", "pi/tv", "base"), "beanjar snapshot delete local/tv base"),
        ):
            with self.subTest(words):
                self.assertEqual(self.beanjar(*words)[0], 0)
                self.assertEqual(self.fake.calls[-1][:2], ("pi", remote_line))

    def test_reason(self):
        """start --reason, status set and unset, config set tags= and stop --force on HOST/NAME
        run on that host, the text as it is (the remote shell gets it quoted), --json at the end
        wherever it was given."""
        self.add(pi={"vms": ["tv"]})
        for words, remote_line in (
            (("start", "pi/tv", "--reason", "a b", "--by", "me"),
             "beanjar start local/tv --reason 'a b' --by me"),
            (("status", "set", "pi/tv", "reason=why not"),
             "beanjar status set local/tv 'reason=why not'"),
            (("status", "unset", "pi/tv", "reason"), "beanjar status unset local/tv reason"),
            (("--json", "status", "show", "pi/tv"), "beanjar status show local/tv --json"),
            (("--json", "start", "pi/tv", "--", "quiet"), "beanjar start local/tv --json -- quiet"),
            (("stop", "pi/tv", "--force"), "beanjar stop local/tv --force"),
            (("config", "set", "pi/tv", "tags=keep,lab"),
             "beanjar config set local/tv tags=keep,lab"),
            (("config", "--json", "unset", "pi/tv", "tags"),
             "beanjar config unset local/tv tags --json"),
            (("sensor", "set", "rotate", "pi/tv", "90"), "beanjar sensor set rotate local/tv 90"),
            (("remote", "press", "pi/tv", "HOME", "--hold", "1s"),
             "beanjar remote press local/tv HOME --hold 1s"),
        ):  # fmt: skip
            with self.subTest(words):
                rc, _out, err = self.beanjar(*words)
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.fake.calls[-1][:2], ("pi", remote_line))

    def test_rename(self):
        """rename HOST/OLD NEW runs on that host, NEW a bare name there; NEW with a host is refused
        here, before any host is asked to rename."""
        self.add(pi={"vms": ["tv"]})
        rc, out, _err = self.beanjar("rename", "pi/tv", "tv2")
        self.assertEqual((rc, out), (0, '{"ok": true}\n'))
        self.assertEqual(self.fake.calls[-1][:2], ("pi", "beanjar rename local/tv tv2"))
        self.beanjar("rename", "tv", "tv2", "--json")  # a bare name found on pi alone
        self.assertEqual(self.fake.calls[-1][1], "beanjar rename local/tv tv2 --json")
        calls = len(self.fake.calls)
        for new in ("pi/tv2", "local/tv2", "other/tv2"):
            rc, _out, err = self.beanjar("rename", "pi/tv", new)
            self.assertEqual(rc, 2, err)
            self.assertIn("NEW is a bare name", err)
        self.assertFalse([c for c in self.fake.calls[calls:] if "rename" in c[1]])

    def test_ssh_failures(self):
        self.add(pi={"vms": ["tv"]})
        self.fake.reply = (127, b"", "bash: beanjar: command not found\n")
        rc, _out, err = self.beanjar("start", "pi/tv", "--json")
        self.assertEqual(rc, 1)
        self.assertIn("no beanjar command there", json.loads(err.splitlines()[-1])["error"])
        self.fake.hosts["pi"]["down"] = "Connection timed out"
        rc, _out, err = self.beanjar("start", "pi/tv", "--json")
        self.assertEqual(rc, 1)
        self.assertIn("Connection timed out", err)

    def test_refused_remotely(self):
        self.add(pi={"vms": ["tv"]})
        for argv, text in (
            (("viewer", "open", "pi/tv"), "beanjar vnc start pi/tv"),
            (("viewer", "open", "tv"), "beanjar vnc start pi/tv"),
            (("key", "model", "set", "pi/tv", "F"), "run it on pi"),
            (("create", "pi/tv", "--firmware", "D"), "on this machine only"),
        ):
            with self.subTest(argv):
                rc, _out, err = self.beanjar(*argv)
                self.assertNotEqual(rc, 0)
                self.assertIn(text, err)
        self.assertEqual([c for c in self.fake.calls if "config show" not in c[1]], [])

    def test_info(self):
        self.add(pi={"vms": ["tv"]})
        data = {"name": "tv", "state": "Off", "dir": "/pi/vms/tv", "gpu": {"enabled": False}}
        self.fake.reply = (0, json.dumps(data).encode(), "")
        rc, out, _ = self.beanjar("show", "pi/tv", "--json")
        self.assertEqual(json.loads(out), {"host": "pi", **data})
        self.assertEqual(self.fake.calls[-1][1], "beanjar show local/tv --json")
        rc, out, _ = self.beanjar("show", "tv")
        self.assertIn("host: pi", out)
        self.assertIn("dir: /pi/vms/tv", out)

    def test_screenshot(self):
        self.add(pi={"vms": ["tv"]})
        self.fake.reply = (0, PNG, "")
        target = os.path.join(self.tmp, "shot.png")
        rc, out, _ = self.beanjar("screenshot", "pi/tv", target, "--frame", "800x600", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.calls[-1][1], "beanjar screenshot local/tv - --frame 800x600")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), PNG)
        self.assertEqual(json.loads(out)["width"], 1280)
        self.assertEqual(json.loads(out)["host"], "pi")
        # an old beanjar: no picture
        self.fake.reply = (0, b'{"file": "-"}', "")
        rc, _out, err = self.beanjar("screenshot", "pi/tv", target)
        self.assertEqual(rc, 1)
        self.assertIn("update beanjar on the host", err)
        self.assertEqual(self.beanjar("screenshot", "pi/tv", "-", "--json")[0], 2)

    def test_pipes(self):
        """vnc pipe, vnc audio-pipe and remote pipe run on the host with this process's stdio
        (remote.attach), not buffered; remote press and layout are forwarded."""
        self.add(pi={"vms": ["tv"]})
        attached = []
        fake = lambda host, uri, argv, tty=False: attached.append((host, uri, argv, tty)) or 3
        with mock.patch.object(remote, "attach", fake):
            for words in (["vnc", "pipe"], ["vnc", "audio-pipe"], ["remote", "pipe"]):
                rc, _out, _err = self.beanjar(*words, "pi/tv")
                self.assertEqual(rc, 3)
                self.assertEqual(
                    attached[-1], ("pi", "ssh://pi.example", [*words, "local/tv"], False)
                )
        self.beanjar("remote", "press", "pi/tv", "HOME", "UP")
        self.assertEqual(self.fake.calls[-1][1], "beanjar remote press local/tv HOME UP")
        self.beanjar("remote", "layout", "pi/tv", "--json")
        self.assertEqual(self.fake.calls[-1][1], "beanjar remote layout local/tv --json")

    def test_password_set_via_stdin(self):
        self.add(pi={"vms": ["tv"]})
        path = os.path.join(self.tmp, "pw")
        with open(path, "w") as f:
            f.write("s3cret\n")
        rc, _out, _err = self.beanjar("vnc", "password", "set", "pi/tv", path)
        self.assertEqual(rc, 0)
        host, argv, stdin = self.fake.calls[-1]
        self.assertEqual(argv, "beanjar vnc password set local/tv")
        self.assertEqual(stdin, b"s3cret\n")
        self.assertNotIn("s3cret", argv)

    def test_local_prefix(self):
        """local/NAME is this machine's NAME: the library is asked, not a host."""
        self.add(pi={"vms": []})
        rc, _out, err = self.beanjar("show", "local/nope")
        self.assertEqual((rc, err), (1, "beanjar: no VM named nope\n"))
        self.assertEqual(self.fake.calls, [])


class NestedHostsTest(Base):
    """A host with hosts of its own (a Pi that manages boards this machine also has as hosts):
    only its own VMs come back, never twice, and a bare name finds nothing of theirs."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(vm_commands, "local_rows", lambda lib, a: [])
        patch.start()
        self.addCleanup(patch.stop)
        board = row("board-tv", host="board")  # as the Pi's own list gives it
        self.add(
            pi={"vms": ["pi-tv"], "rows": [row("pi-tv")], "indirect_vms": ["board-tv"],
                "indirect_rows": [board]},
            board={"vms": ["board-tv"], "rows": [row("board-tv")]},
        )  # fmt: skip

    def test_list_once(self):
        rc, out, err = self.beanjar("list", "--json")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([(r["host"], r["name"]) for r in json.loads(out)],
                         [("pi", "pi-tv"), ("board", "board-tv")])  # fmt: skip
        self.assertTrue(all("--local" in c[1] for c in self.fake.calls))

    def test_bare_name_found_once(self):
        warnings = []
        found = remote.locate("board-tv", lambda n: False, warnings.append)
        self.assertEqual((found, warnings), (("board", "board-tv"), []))
        self.beanjar("stop", "board-tv")
        self.assertEqual(self.fake.calls[-1][:2], ("board", "beanjar stop local/board-tv"))

    def test_older_remote(self):
        """A beanjar without list --local: a usage error, which says to update it."""
        self.fake.hosts["pi"]["old"] = True
        rc, _out, err = self.beanjar("list", "--json")
        self.assertEqual(rc, 0)
        self.assertIn("unrecognized arguments: --local", err)
        self.assertIn("update it on the host", err)


class WorkerScreenshotTest(Base):
    def test_stdout(self):
        """screenshot NAME -: the PNG alone on stdout, the progress on stderr."""

        def shot(self_, name, path, size=None, out=None):
            out("running a headless viewer once")
            with open(path, "wb") as f:
                f.write(PNG)
            return path, (1280, 720), "headless"

        with mock.patch.object(core.Library, "screenshot", shot):
            rc, out, err = self.beanjar("screenshot", "tv", "-")
            self.assertEqual((rc, out), (0, PNG.decode("latin-1")))
            self.assertEqual(err, "running a headless viewer once\n")
            self.assertEqual(self.beanjar("screenshot", "tv", "-", "--json")[0], 2)


class FakeQmp:
    """A QMP server on a unix socket: reports the "standby" chardev open or closed, records the
    commands other than the handshake and queries."""

    def __init__(self, path):
        self.standby = False
        self.commands = []
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(4)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn, conn.makefile("rw") as f:
                f.write('{"QMP": {}}\n')
                f.flush()
                for line in f:
                    cmd = json.loads(line)["execute"]
                    ret = {}
                    if cmd == "query-status":
                        ret = {"status": "running"}
                    elif cmd == "query-chardev":
                        ret = [{"label": "standby", "frontend-open": self.standby}]
                    elif cmd != "qmp_capabilities":
                        self.commands.append(cmd)
                    f.write(json.dumps({"return": ret}) + "\n")
                    f.flush()

    def close(self):
        self.srv.close()


class RemotePowerTest(unittest.TestCase):
    """remote press / pipe: the power key wakes a guest in standby by QMP, not by IR."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sock = os.path.join(tmp.name, "qmp.sock")
        self.qmp = FakeQmp(sock)
        self.addCleanup(self.qmp.close)
        self.ir = []
        r = pipe.Remote.__new__(pipe.Remote)
        r.vm = mock.Mock(qmp_sock=sock)
        r.layout = pipe.vm_layout(mock.Mock(target=mock.Mock(side_effect=core.Error("none"))))
        r.held, r.woke = None, False
        r._call = lambda dest, path, iface, method, signature, values: self.ir.append(values)
        self.remote = r

    def press(self, key):
        code = self.remote.code(key)
        self.remote.edge(code, True)
        self.remote.edge(code, False)

    def test_power_in_standby_wakes(self):
        self.qmp.standby = True
        self.press("POWER")
        self.assertEqual((self.qmp.commands, self.ir), (["system_powerdown"], []))
        self.assertIsNone(self.remote.held)

    def test_power_awake_is_ir(self):
        self.press("POWER")
        self.assertEqual(self.qmp.commands, [])
        self.assertEqual(self.ir, [(8, True), (8, False)])

    def test_other_key_in_standby_is_ir(self):
        self.qmp.standby = True
        self.press("HOME")
        self.assertEqual(self.qmp.commands, [])
        self.assertEqual([d for _, d in self.ir], [True, False])

    def test_up_after_wake_is_dropped_once(self):
        """The up of a waking power key is dropped; a later awake power press is IR again."""
        self.qmp.standby = True
        self.press("POWER")
        self.qmp.standby = False
        self.press("POWER")
        self.assertEqual(self.qmp.commands, ["system_powerdown"])
        self.assertEqual(self.ir, [(8, True), (8, False)])


class PointerMappingTest(unittest.TestCase):
    """A screenshot frame's point to the guest display's pixel (framing.frame_to_source), the frame's
    geometry for --json, and the display size a screenshot's PNG records."""

    def test_box(self):
        self.assertEqual(framing.picture_box((1280, 720), (1920, 1080)), (0, 0, 1280, 720))
        self.assertEqual(framing.picture_box((1280, 720), (1280, 720)), (0, 0, 1280, 720))
        self.assertEqual(framing.picture_box((1280, 720), (1366, 768)), (0, 0, 1280, 719))
        self.assertEqual(framing.picture_box((1280, 720), (1024, 768)), (160, 0, 960, 720))
        self.assertEqual(framing.picture_box((1280, 1024), (1920, 1080)), (0, 152, 1280, 720))

    def test_1080p(self):
        f, s = (1280, 720), (1920, 1080)
        self.assertEqual(framing.frame_to_source(0, 0, f, s), (0, 0))
        self.assertEqual(framing.frame_to_source(1279, 719, f, s), (1919, 1079))
        self.assertEqual(framing.frame_to_source(640, 360, f, s), (960, 540))
        self.assertEqual(framing.frame_to_source(100, 50, f, s), (150, 75))
        self.assertEqual(framing.frame_to_source(640.5, 360.5, f, s), (961, 541))  # a box's centre

    def test_same_size_and_others(self):
        self.assertEqual(framing.frame_to_source(640, 360, (1280, 720), (1280, 720)), (640, 360))
        self.assertEqual(framing.frame_to_source(1279, 719, (1280, 720), (1280, 720)), (1279, 719))
        self.assertEqual(framing.frame_to_source(640, 360, (1280, 720), (3840, 2160)), (1921, 1081))
        self.assertEqual(framing.frame_to_source(1279, 718, (1280, 720), (1366, 768)), (1365, 767))
        # a frame larger than the display (screenshot --frame 3840x2160 of a 1080p one)
        self.assertEqual(
            framing.frame_to_source(3839, 2159, (3840, 2160), (1920, 1080)), (1919, 1079)
        )

    def test_letterbox(self):
        f, s = (1280, 720), (1024, 768)  # 4:3 in 16:9: bars of 160 left and right
        self.assertEqual(framing.frame_to_source(160, 0, f, s), (0, 0))
        self.assertEqual(framing.frame_to_source(1119, 719, f, s), (1023, 767))
        for x, y in ((159, 360), (1120, 360), (1280, 0), (0, 720)):
            with self.assertRaisesRegex(core.Error, "outside the TV picture"):
                framing.frame_to_source(x, y, f, s)

    def test_geometry(self):
        self.assertEqual(
            framing.frame_geometry((1280, 720), (1920, 1080)),
            {"source_width": 1920, "source_height": 1080, "scale": 0.666667, "offset_x": 0,
             "offset_y": 0},
        )  # fmt: skip
        self.assertEqual(framing.frame_geometry((1280, 720), (1024, 768))["offset_x"], 160)
        self.assertEqual(framing.frame_geometry((1280, 720), (1280, 720))["scale"], 1.0)
        self.assertEqual(set(framing.frame_geometry((1280, 720), None).values()), {None})

    def test_png_source(self):
        ppm = b"P6\n2 1\n255\n" + bytes(6)
        self.assertEqual(framing.png_source(framing.ppm_to_png(ppm, (1920, 1080))), (1920, 1080))
        self.assertIsNone(framing.png_source(framing.ppm_to_png(ppm)))
        self.assertIsNone(framing.png_source(PNG))

    def test_wake_point(self):
        self.assertEqual(pipe.wake_point(960, 540, (1920, 1080)), (964, 540))
        self.assertEqual(pipe.wake_point(1918, 540, (1920, 1080)), (1914, 540))


class PointerTest(unittest.TestCase):
    """remote click and move: the calls to org.beanjar.Display1.Pointer, with the bus replaced."""

    def setUp(self):
        self.calls = []
        test = self

        class FakePointer(pipe.Pointer):
            def __init__(self, vm, timeout=5.0):
                self.vm, self.source, self.buttons = vm, test.source, 0
                self.conn = mock.Mock()

            def _call(self, dest, path, iface, method, signature, values, reply=None):
                test.calls.append((method, values))

        self.source = (1920, 1080)
        patch = mock.patch.object(pipe, "Pointer", FakePointer)
        patch.start()
        self.addCleanup(patch.stop)
        sleep = mock.patch.object(pipe.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_click(self):
        data = pipe.pointer(mock.Mock(), "click", 640, 360)
        self.assertEqual(
            self.calls,
            [("Pointer", (964, 540, 0)), ("Pointer", (960, 540, 0)), ("Pointer", (960, 540, 1)),
             ("Pointer", (960, 540, 0))],
        )  # fmt: skip
        self.assertEqual((data["display_x"], data["display_y"], data["button"]), (960, 540, "left"))
        self.assertEqual((data["source_width"], data["scale"]), (1920, 0.666667))

    def test_right_click_and_move(self):
        pipe.pointer(mock.Mock(), "click", 10, 10, button="right")
        self.assertEqual(self.calls[2], ("Pointer", (15, 15, 4)))
        self.calls.clear()
        data = pipe.pointer(mock.Mock(), "move", 10, 10)
        self.assertEqual(self.calls, [("Pointer", (19, 15, 0)), ("Pointer", (15, 15, 0))])
        self.assertIsNone(data["button"])

    def test_outside_sends_nothing(self):
        self.source = (1024, 768)
        with self.assertRaisesRegex(core.Error, "outside the TV picture"):
            pipe.pointer(mock.Mock(), "click", 100, 360)
        self.assertEqual(self.calls, [])

    def test_frame(self):
        pipe.pointer(mock.Mock(), "move", 1920, 1080, frame=(3840, 2160))
        self.assertEqual(self.calls[-1], ("Pointer", (960, 540, 0)))


class PointerCliTest(Base):
    """remote click / move: argument handling, and forwarding to a host."""

    def test_arguments(self):
        seen = []
        fake = lambda vm, action, x, y, frame, **kw: seen.append((action, x, y, frame, kw)) or {
            "x": x, "y": y, "display_x": 1, "display_y": 2, "source_width": 1920,
            "source_height": 1080}  # fmt: skip
        with (
            mock.patch.object(pipe, "pointer", fake),
            mock.patch.object(commands.Library, "vm", lambda self, name: name),
        ):
            rc, out, _ = self.beanjar("remote", "click", "tv", "640.5", "360", "--button", "right")
            self.assertEqual(rc, 0, out)
            self.assertEqual(
                seen[-1],
                ("click", 640.5, 360, (1280, 720), {"button": "right", "settle": 0.3, "hold": 0.1}),
            )
            self.assertIn("right click at 640.5,360 of 1280x720 -> 1,2 of 1920x1080", out)
            rc, _, _ = self.beanjar("remote", "move", "tv", "1", "2", "--frame", "1920x1080")
            self.assertEqual((rc, seen[-1][:4]), (0, ("move", 1, 2, (1920, 1080))))
            for bad in (["-1", "2"], ["x", "2"], ["1"], ["1", "2", "--button", "back"],
                        ["1", "2", "--frame", "big"], ["1", "2", "--settle", "-5ms"],
                        ["1", "2", "--settle", "300"]):  # fmt: skip
                self.assertEqual(self.beanjar("remote", "click", "tv", *bad)[0], 2, bad)
            self.assertEqual(self.beanjar("remote", "move", "tv", "1", "2", "--hold", "5")[0], 2)

    def test_forwarded(self):
        self.add(pi={"vms": ["tv"]})
        self.beanjar("remote", "click", "pi/tv", "640", "360", "--json")
        self.assertEqual(self.fake.calls[-1][1], "beanjar remote click local/tv 640 360 --json")
        self.beanjar("remote", "move", "pi/tv", "1", "2")
        self.assertEqual(self.fake.calls[-1][1], "beanjar remote move local/tv 1 2")


class ListTest(Base):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(vm_commands, "local_rows", lambda lib, a: [row("loc", host=None)])
        patch.start()
        self.addCleanup(patch.stop)

    def test_merge(self):
        self.add(pi={"rows": [row("a")]}, nuc={"rows": [row("b"), row("c")]})
        rc, out, err = self.beanjar("list", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual([(r["host"], r["name"]) for r in json.loads(out)],
                         [(None, "loc"), ("pi", "a"), ("nuc", "b"), ("nuc", "c")])  # fmt: skip
        self.assertEqual(err, "")
        rc, out, _ = self.beanjar("list")
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("HOST"))
        self.assertEqual([l.split()[:2] for l in lines[1:]], [["local", "loc"], ["pi", "a"], ["nuc", "b"], ["nuc", "c"]])  # fmt: skip

    def test_tag(self):
        """list --tag filters every host's rows here (all tags must match); a row of an older
        beanjar, without tags or reason, has none and lists as such."""
        self.add(pi={"rows": [row("a", tags=["keep", "cam"]), row("b", tags=["cam"])]},
                 nuc={"rows": [row("c")]})  # fmt: skip
        names = lambda *a: [r["name"] for r in json.loads(self.beanjar("list", "--json", *a)[1])]  # noqa: E731
        self.assertEqual(names("--tag", "cam"), ["a", "b"])
        self.assertEqual(names("--tag", "cam", "--tag", "KEEP"), ["a"])
        self.assertEqual(names("--host", "nuc", "--tag", "cam"), [])
        rc, out, _ = self.beanjar("list")
        self.assertEqual(rc, 0)
        self.assertIn("keep,cam", out)

    def test_failing_host(self):
        self.add(pi={"rows": [row("a")]}, bad={"down": "No route to host"}, junk={})
        rc, out, err = self.beanjar("list", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual([r["name"] for r in json.loads(out)], ["loc", "a"])
        self.assertIn("warning: host bad: No route to host", err)
        self.assertIn("warning: host junk: ", err)
        self.assertIn("no JSON", err)

    def test_select(self):
        self.add(pi={"rows": [row("a")]}, bad={"down": "refused"})
        names = lambda *a: [r["name"] for r in json.loads(self.beanjar("list", "--json", *a)[1])]  # noqa: E731
        self.assertEqual(names("--local"), ["loc"])
        self.assertEqual(names("--host", "local"), ["loc"])
        self.assertEqual(names("--host", "pi"), ["a"])
        self.assertEqual(self.fake.calls[-1][1], "beanjar list --local --json")
        rc, out, err = self.beanjar("list", "--host", "bad")
        self.assertEqual((rc, out), (1, ""), "every source failed")
        self.assertIn("refused", err)
        self.assertEqual(self.beanjar("list", "--host", "zz")[0], 1)
        self.assertEqual(self.beanjar("list", "--local", "--host", "pi")[0], 2)
        self.beanjar("list", "--all", "--host", "pi")
        self.assertEqual(self.fake.calls[-1][1], "beanjar list --local --all --json")

    def test_all_hosts_down_is_failure_only_without_local(self):
        self.add(bad={"down": "refused"})
        self.assertEqual(self.beanjar("list")[0], 0, "this machine listed")

    def test_no_hosts_unchanged(self):
        rc, out, _ = self.beanjar("list")
        self.assertEqual(out.splitlines()[0].split()[0], "NAME")
        self.assertEqual(self.fake.calls, [])


FAKE_SSH = r"""#!/usr/bin/env python3
# ssh -O OP standing for the shared connection's master: records its argv; with no master (no
# file "master") check and every other request fail; forward and cancel keep the master's
# forwards in forwards.json. With FAKE_SSH_BUSY set to a counter file a forward fails that many
# times (a port taken), with FAKE_SSH_FAIL always, printing it.
import json, os, sys
d = os.environ["FAKE_DIR"]
argv = sys.argv[1:]
with open(os.path.join(d, "ssh.log"), "a") as f:
    f.write(json.dumps(argv) + "\n")
op = argv[argv.index("-O") + 1]
if not os.path.exists(os.path.join(d, "master")):
    sys.stderr.write("Control socket connect(x): No such file or directory\n")
    sys.exit(255)
if op == "check":
    sys.exit(0)
state = os.path.join(d, "forwards.json")
forwards = json.load(open(state)) if os.path.exists(state) else []
spec = argv[argv.index("-L") + 1]
if op == "forward":
    busy = os.environ.get("FAKE_SSH_BUSY")
    if busy and int(open(busy).read() or 0) > 0:
        open(busy, "w").write(str(int(open(busy).read()) - 1))
        sys.stderr.write("mux_client_forward: forwarding request failed: Port forwarding failed\n")
        sys.exit(255)
    if os.environ.get("FAKE_SSH_FAIL"):
        sys.stderr.write(os.environ["FAKE_SSH_FAIL"] + "\n")
        sys.exit(255)
    forwards.append(spec)
elif spec in forwards:
    forwards.remove(spec)
json.dump(forwards, open(state, "w"))
"""

FAKE_TOOL = r"""#!/usr/bin/env python3
# an ares tool: records its argv, HOME's device list and key, and the master's forwards;
# with FAKE_TOOL_TERM sends SIGTERM to its parent (beanjar) and waits; exits 3.
import json, os, signal, stat, sys, time
d = os.environ["FAKE_DIR"]
home = os.environ["HOME"]
names = [n for n in os.listdir(os.path.join(home, ".ssh"))]
key = os.path.join(home, ".ssh", names[0])
with open(os.path.join(d, "tool.json"), "w") as f:
    json.dump({"argv": sys.argv, "home": home,
               "ose": json.load(open(os.path.join(home, ".webos/ose/novacom-devices.json"))),
               "tv": json.load(open(os.path.join(home, ".webos/tv/novacom-devices.json"))),
               "keys": names, "key": open(key).read(), "mode": stat.S_IMODE(os.stat(key).st_mode),
               "forwards": json.load(open(os.path.join(d, "forwards.json")))}, f)
if os.environ.get("FAKE_TOOL_TERM"):
    os.kill(os.getppid(), signal.SIGTERM)
    time.sleep(60)
sys.exit(3)
"""


class AresRemoteTest(Base):
    """beanjar ares-TOOL -d HOST/NAME: the tool runs here, against 127.0.0.1:PORT, a forward of
    the VM's developer-mode port added to the host's shared connection (ssh -O forward, a fake
    master here) and cancelled afterwards, with HOME a temporary directory holding the key the
    host gave, removed afterwards; also on failure, Ctrl-C and SIGTERM. The tool's status is the
    command's."""

    TARGET: typing.ClassVar = {"name": "tv", "layers": ["devmode"], "running": True,
              "address": "127.0.1.5", "port": 9922, "key": "KEY\n"}  # fmt: skip

    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.tmp, "fake")
        os.makedirs(self.dir)
        for name, text in (("ssh", FAKE_SSH), ("ares-fake", FAKE_TOOL)):
            path = os.path.join(self.dir, name)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o755)
        open(os.path.join(self.dir, "ssh.log"), "w").close()
        open(os.path.join(self.dir, "master"), "w").close()  # the shared connection is up
        patch = mock.patch.dict(
            os.environ, {"PATH": f"{self.dir}:{os.environ['PATH']}", "FAKE_DIR": self.dir}
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.target = dict(self.TARGET)  # the host's answer to _ares (with --root: root_target)
        self.root_target = dict(self.TARGET, port=None, key="CLIENT\n")
        self.reply = None  # (rc, out, err) instead of the answer
        self.helper = []  # the shell commands of the shared connection
        patch = mock.patch.object(remote, "transport", self.transport)
        patch.start()
        self.addCleanup(patch.stop)
        remote.add_host("pi", "ssh://me@pi.example:2222")

    def transport(self, argv, stdin=None, on_stderr=None, timeout=None):
        command = argv[-1]
        self.helper.append(command)
        if command.startswith("beanjar config show "):
            known = command.split()[3] == "local/tv"
            return (0, b"{}", "") if known else (1, b"", '{"error": "no VM named x"}\n')
        if command == "true":  # run to start the shared connection (remote.ensure_master)
            open(os.path.join(self.dir, "master"), "w").close()
            return 0, b"", ""
        if self.reply and command.startswith("beanjar _ares "):
            return self.reply
        if command == "beanjar _ares local/tv --json":
            return 0, json.dumps(self.target).encode(), ""
        if command == "beanjar _ares local/tv --root --json":
            return 0, json.dumps(self.root_target).encode(), ""
        raise AssertionError(command)

    def requests(self, op="forward"):
        """The argv of each ssh -O op."""
        with open(os.path.join(self.dir, "ssh.log")) as f:
            return [a for a in map(json.loads, f) if a[a.index("-O") + 1] == op]

    def forwards(self):
        """The forwards the master has now."""
        try:
            with open(os.path.join(self.dir, "forwards.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return []

    def tool(self):
        with open(os.path.join(self.dir, "tool.json")) as f:
            return json.load(f)

    def assert_clean(self):
        self.assertEqual(self.forwards(), [], "the forward is cancelled")
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith("beanjar-ares-")], [])

    def test_run(self):
        rc, _out, err = self.beanjar("ares-fake", "-d", "pi/tv", "-r", "id -u", "x.ipk")
        self.assertEqual(rc, 3, err)
        got = self.tool()
        self.assertEqual(got["argv"][1:], ["-r", "id -u", "x.ipk"], "arguments as they are")
        self.assertTrue(got["home"].startswith(self.tmp), "under XDG_RUNTIME_DIR")
        (listen,) = got["forwards"]
        port = int(listen.split(":")[1])
        self.assertEqual(listen, f"127.0.0.1:{port}:127.0.1.5:9922")
        (argv,) = self.requests()
        self.assertEqual(argv[argv.index("-L") + 1], listen)
        shared = remote.shell_argv("ssh://me@pi.example:2222", "x")
        control = next(o for o in shared if o.startswith("ControlPath="))
        self.assertIn(control, argv, "the shared connection's master")
        self.assertEqual(argv[-6:], ["-p", "2222", "-l", "me", "--", "pi.example"])
        (cancel,) = self.requests("cancel")
        self.assertEqual(
            cancel, [*argv[: argv.index("-O")], "-O", "cancel", *argv[argv.index("-L") :]]
        )
        self.assertNotIn("true", self.helper, "the master was up: not started")
        for profile in ("ose", "tv"):
            (dev,) = got[profile]
            self.assertEqual((dev["host"], dev["port"], dev["name"]), ("127.0.0.1", port, "tv"))
            self.assertEqual((dev["profile"], dev["username"]), (profile, "prisoner"))
        self.assertEqual((got["key"], got["mode"]), ("KEY\n", 0o600))
        self.assert_clean()
        self.assertEqual(
            [c for c in self.helper if not c.startswith("beanjar config show ")],
            ["beanjar _ares local/tv --json"],
            "one call to the host",
        )
        # a bare name found on the host, at the address the host gives (listen's, here)
        self.target["address"] = "192.168.1.9"
        self.assertEqual(self.beanjar("ares-fake", "-d", "tv")[0], 3)
        argv = self.requests()[-1]
        self.assertTrue(argv[argv.index("-L") + 1].endswith(":192.168.1.9:9922"))
        self.assert_clean()

    def test_lan_direct(self):
        """A VM with link lan and mode direct: the host cannot reach it, so the tool connects to the
        guest's own LAN address (the host's "lan") with no forward, root to the guest's port 22;
        while the host knows no address yet ("") it is refused."""
        self.target["lan"] = "192.168.1.77"
        with open(os.path.join(self.dir, "forwards.json"), "w") as f:
            f.write("[]")  # the fake tool reads the master's forwards: none
        rc, _out, err = self.beanjar("ares-fake", "-d", "pi/tv")
        self.assertEqual(rc, 3, err)
        got = self.tool()
        self.assertEqual(got["forwards"], [])
        for profile in ("ose", "tv"):
            (dev,) = got[profile]
            self.assertEqual((dev["host"], dev["port"]), ("192.168.1.77", 9922))
        self.assertEqual(self.requests(), [], "no forward")
        self.assert_clean()
        self.target["lan"] = ""
        rc, _out, err = self.beanjar("ares-fake", "-d", "pi/tv")
        self.assertEqual(rc, 1)
        self.assertIn("its LAN address is not known yet", err)

    def test_master_started(self):
        """No shared connection running: one is started (a command over it), then the forward
        added to it."""
        os.unlink(os.path.join(self.dir, "master"))
        rc, _out, err = self.beanjar("ares-fake", "-d", "pi/tv")
        self.assertEqual(rc, 3, err)
        self.assertIn("true", self.helper)
        self.assertEqual(len(self.tool()["forwards"]), 1)
        self.assert_clean()

    def test_refused(self):
        for change, message in (
            ({"layers": ["hbchannel"]}, "config set tv layers=hbchannel,devmode`"),
            ({"running": False}, "VM tv is not running"),
            ({"address": None}, "VM tv has no address"),
            ({"key": None}, "VM tv has no developer key"),
        ):
            with self.subTest(message):
                self.target = dict(self.TARGET, **change)
                rc, _out, err = self.beanjar("ares-fake", "-d", "pi/tv")
                self.assertEqual(rc, 1)
                self.assertIn(message, err)
        self.assertEqual(self.requests(), [], "no forward for a VM refused")

    def test_host_errors(self):
        """A host whose beanjar predates the query (a usage error) is named, to update; its own
        error (no such VM) is passed on; an answer it cannot read is refused. No forward."""
        for reply, message in (
            ((2, b"", "beanjar: error: argument COMMAND: invalid choice: '_ares'\n"),
             "host pi: its beanjar has no `_ares` query, which ares-TOOL -d pi/NAME needs: "
             "update beanjar on pi"),
            ((1, b"", '{"error": "no VM named tv"}\n'), "host pi: no VM named tv"),
            ((0, b"{}", ""), "host pi: its beanjar's `_ares` answer is not what this one reads"),
            ((0, b'{"name": "tv", "layers": [], "running": true, "address": "a", "port": "x",'
                 b' "key": "K"}', ""), "is not what this one reads"),
        ):  # fmt: skip
            with self.subTest(message):
                self.reply = reply
                rc, _out, err = self.beanjar("ares-fake", "-d", "pi/tv")
                self.assertEqual(rc, 1)
                self.assertIn(message, err)
        self.assertEqual(self.requests(), [])

    def test_root(self):
        """--root: root over the root SSH port the host gives (SSH_PORT there), with the client
        key; --root is beanjar's, not the tool's."""
        self.root_target.update(layers=["rootssh"], port=6700)
        rc, _out, err = self.beanjar("ares-fake", "-d", "pi/tv", "--root", "-r", "id")
        self.assertEqual(rc, 3, err)
        got = self.tool()
        self.assertEqual(got["argv"][1:], ["-r", "id"])
        (listen,) = got["forwards"]
        port = int(listen.split(":")[1])
        self.assertEqual(listen, f"127.0.0.1:{port}:127.0.1.5:6700")
        for profile in ("ose", "tv"):
            (dev,) = got[profile]
            self.assertEqual((dev["host"], dev["port"]), ("127.0.0.1", port))
            self.assertEqual(
                (dev["username"], dev["privateKey"]), ("root", {"openSsh": "id_ed25519"})
            )
        self.assertEqual(
            (got["keys"], got["key"], got["mode"]), (["id_ed25519"], "CLIENT\n", 0o600)
        )
        self.assert_clean()

    def test_root_refused(self):
        """--root on a VM without root SSH (no port from the host) names the layers to add;
        without its client key it is refused too; no forward either way."""
        rc, _out, err = self.beanjar("ares-fake", "--root", "-d", "pi/tv")
        self.assertEqual(rc, 1)
        self.assertIn("VM tv has no root SSH", err)
        self.assertIn("port 22, hbchannel or rootssh.", err)
        self.assertIn("`beanjar config set tv layers=devmode,rootssh`", err)
        self.root_target.update(port=6622, key=None)
        rc, _out, err = self.beanjar("ares-fake", "--root", "-d", "pi/tv")
        self.assertEqual(rc, 1)
        self.assertIn("VM tv has no client key (id_ed25519)", err)
        self.assertEqual(self.requests(), [])

    def test_forward_failure(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_FAIL": "muxclient: master forward failed"}):
            rc, _out, err = self.beanjar("ares-fake", "-d", "pi/tv")
        self.assertEqual(rc, 1)
        self.assertIn("host pi: forward to 127.0.1.5:9922: muxclient: master forward failed", err)
        self.assertEqual(len(self.requests()), ares.FORWARD_TRIES)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tool.json")))
        self.assert_clean()

    def test_port_taken(self):
        busy = os.path.join(self.dir, "busy")
        with open(busy, "w") as f:
            f.write("1")
        with mock.patch.dict(os.environ, {"FAKE_SSH_BUSY": busy}):
            self.assertEqual(self.beanjar("ares-fake", "-d", "pi/tv")[0], 3)
        self.assertEqual(len(self.requests()), 2, "another port tried")
        self.assert_clean()

    def test_interrupt(self):
        def interrupted(*_a, **_k):
            raise KeyboardInterrupt

        with mock.patch.object(ares, "write_home", interrupted):
            self.assertEqual(self.beanjar("ares-fake", "-d", "pi/tv")[0], 130)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.requests("cancel")), 1)
        self.assert_clean()

    def test_terminated(self):
        before = signal.getsignal(signal.SIGTERM)
        with mock.patch.dict(os.environ, {"FAKE_TOOL_TERM": "1"}):
            self.assertEqual(self.beanjar("ares-fake", "-d", "pi/tv")[0], 128 + signal.SIGTERM)
        self.assert_clean()
        self.assertIs(signal.getsignal(signal.SIGTERM), before, "handler restored")

    def test_stale_home_swept(self):
        """A temporary HOME left by a run that was killed outright is removed by the next run."""
        p = subprocess.Popen(["true"])
        p.wait()
        stale = os.path.join(self.tmp, f"beanjar-ares-{p.pid}-x")
        live = os.path.join(self.tmp, f"beanjar-ares-{os.getppid()}-y")
        for d in (stale, live):
            os.makedirs(d)
        self.assertEqual(self.beanjar("ares-fake", "-d", "pi/tv")[0], 3)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(live), "a running beanjar's is kept")
        os.rmdir(live)

    def test_killed(self):
        """A beanjar killed outright (SIGKILL, no cleanup) leaves its forward in the master; the
        next remote run cancels it, from the record in the HOME it removes."""
        code = (
            "import os, signal, sys, tempfile; sys.path.insert(0, sys.argv[1])\n"
            "from beanjar import ares\n"
            "home = tempfile.mkdtemp(prefix=f'beanjar-ares-{os.getpid()}-', dir=sys.argv[2])\n"
            "with ares.Forward('pi', sys.argv[3], '127.0.1.5', 9922,\n"
            "                  os.path.join(home, ares.FORWARD_RECORD)):\n"
            "    os.kill(os.getpid(), signal.SIGKILL)\n"
        )
        manager = os.path.join(TOP, "host", "core")
        uri = "ssh://me@pi.example:2222"
        p = subprocess.run([sys.executable, "-c", code, manager, self.tmp, uri], check=False)
        self.assertEqual(p.returncode, -signal.SIGKILL)
        (left,) = self.forwards()
        self.assertTrue(left.endswith(":127.0.1.5:9922"), "left in the master")
        self.assertEqual(self.beanjar("ares-fake", "-d", "pi/tv")[0], 3)
        self.assertIn(left, [a[a.index("-L") + 1] for a in self.requests("cancel")])
        self.assert_clean()


class MitmConnectTest(AresRemoteTest):
    """beanjar mitm connect HOST/NAME: the host's `mitm show local/NAME --password --json` (one
    call), then a forward from 127.0.0.1:PORT here to the inspector there (local_url with link
    lan) on the host's shared connection, printed as one JSON line with the password, held until
    stdin ends and cancelled then, also on SIGTERM; refused without one; a record of it swept
    when the process was killed outright."""

    SHOW: typing.ClassVar = {"name": "tv", "mode": "mitm", "status": "Running", "ready": True,
            "url": "http://127.0.1.5:18083/", "local_url": None, "password_file": "/x",
            "password": "pw"}  # fmt: skip

    def setUp(self):
        super().setUp()
        self.show = dict(self.SHOW)
        self.held = []  # the master's forwards while it holds, and what it printed

    def transport(self, argv, stdin=None, on_stderr=None, timeout=None):
        command = argv[-1]
        if command == "beanjar mitm show local/tv --password --json":
            self.helper.append(command)
            return self.reply or (0, json.dumps(self.show).encode(), "")
        return super().transport(argv, stdin, on_stderr, timeout)

    def connect(self, *words, ticks=(), asks=()):
        """mitm connect, its stdin ending after the ticks (callables run before each check), the
        checks of mitmweb answered by asks; self.held gets the master's forwards then."""
        asks = list(asks)
        ticks = list(ticks)

        class Ended:
            def wait(_self, _timeout):
                if not ticks:
                    self.held.append(self.forwards())
                    return True
                ticks.pop(0)()
                return False

        with mock.patch.object(inspector, "until_eof", Ended), \
                mock.patch.object(inspector, "ask", side_effect=lambda *a, **k: asks.pop(0)):  # fmt: skip
            return self.beanjar("mitm", "connect", *words)

    def lines(self, out):
        return [json.loads(line) for line in out.splitlines()]

    def assert_clean(self):
        super().assert_clean()
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith("beanjar-mitm-")], [])

    def test_connect(self):
        rc, out, err = self.connect("pi/tv", ticks=[lambda: None], asks=[("ok", "c=1")])
        self.assertEqual(rc, 0, err)
        (listen,) = self.held[0]
        port = int(listen.split(":")[1])
        self.assertEqual(listen, f"127.0.0.1:{port}:127.0.1.5:18083")
        self.assertEqual(self.lines(out), [{"url": f"http://127.0.0.1:{port}/", "password": "pw"}],
                         "one JSON line")  # fmt: skip
        (cancel,) = self.requests("cancel")
        self.assertEqual(cancel[cancel.index("-L") + 1], listen)
        self.assertEqual(
            [c for c in self.helper if not c.startswith("beanjar config show ")],
            ["beanjar mitm show local/tv --password --json"],
            "one call to the host",
        )
        self.assert_clean()
        # link lan: the host's own way in, local_url, not the LAN address
        self.show.update(url="http://192.168.1.9:18083/", local_url="http://127.0.1.5:18083/")
        self.assertEqual(self.connect("pi/tv")[0], 0)
        self.assertTrue(self.held[-1][0].endswith(":127.0.1.5:18083"))
        self.assert_clean()

    def test_master_ended(self):
        """The shared connection ended (ControlPersist), taking the forward along: the next check
        is refused, and the same forward is added to a new master; nothing new is printed."""

        def master_ended():
            os.unlink(os.path.join(self.dir, "master"))
            os.unlink(os.path.join(self.dir, "forwards.json"))

        rc, out, err = self.connect("pi/tv", ticks=[master_ended, lambda: None],
                                    asks=[("refused", None), ("ok", "c=1")])  # fmt: skip
        self.assertEqual(rc, 0, err)
        first, again = (a[a.index("-L") + 1] for a in self.requests())
        self.assertEqual(again, first, "the same forward")
        self.assertEqual(self.held, [[first]])
        self.assertIn("true", self.helper, "a new master")
        self.assertEqual(len(self.lines(out)), 1)
        self.assert_clean()

    def test_restarted(self):
        """The VM restarted: mitmweb refuses the old session; the host's mitm show gives the new
        password, printed as a new line once mitmweb takes it; stderr says so."""

        def restarted():
            self.show = dict(self.SHOW, password="new")

        with mock.patch.object(inspector, "RESHOW", 0):
            rc, out, err = self.connect(
                "pi/tv",
                ticks=[lambda: None, restarted, lambda: None],
                asks=[("ok", "c=1"), ("denied", None), ("ok", "c=2")],
            )
        self.assertEqual(rc, 0, err)
        (first, second) = self.lines(out)
        self.assertEqual((first["password"], second["password"]), ("pw", "new"))
        self.assertEqual(first["url"], second["url"], "the same forward")
        self.assertIn("new password", err)
        self.assert_clean()

    def test_stopped(self):
        """The VM is off for a while: stderr says why once, connect waits; then the new one."""
        down = dict(self.SHOW, status="Not running", ready=False)

        def stop():
            self.show = down

        def start():
            self.show = dict(self.SHOW, password="new")

        with mock.patch.object(inspector, "RESHOW", 0):
            rc, out, err = self.connect(
                "pi/tv",
                ticks=[stop, lambda: None, start, lambda: None],
                asks=[("down", None), ("down", None), ("denied", None), ("ok", "c=2")],
            )
        self.assertEqual(rc, 0, err)
        self.assertEqual(err.count("VM tv is not running: waiting for it"), 1, err)
        self.assertEqual([a["password"] for a in self.lines(out)], ["pw", "new"])
        self.assert_clean()

    def test_clear(self):
        """mitm clear HOST/NAME: the host's mitm show, a forward for the one request, cancelled."""
        with mock.patch.object(inspector, "clear", return_value=True) as clear:
            rc, out, err = self.beanjar("mitm", "clear", "pi/tv", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"name": "tv", "cleared": True})
        ((url, password), _kw) = clear.call_args
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertEqual(password, "pw")
        self.assertEqual(len(self.requests()), 1)
        self.assert_clean()
        with mock.patch.object(inspector, "clear", return_value=False):
            rc, _out, err = self.beanjar("mitm", "clear", "pi/tv")
        self.assertEqual(rc, 1)
        self.assertIn("did not clear its flows", err)
        self.assert_clean()

    def test_refused(self):
        for change, message in (
            ({"mode": "direct", "status": "Not running", "ready": False},
             "runs without the traffic inspector (network mode direct"),
            ({"status": "Not running", "ready": False}, "VM tv is not running"),
            ({"status": "Inspector is not ready", "ready": False},
             "VM tv's traffic inspector: Inspector is not ready"),
            ({"url": None}, "host pi: its beanjar mitm show gave no inspector address"),
            ({"ready": "yes"}, "host pi: its beanjar mitm show is not what this one reads"),
        ):  # fmt: skip
            with self.subTest(message):
                self.show = dict(self.SHOW, **change)
                rc, _out, err = self.connect("pi/tv")
                self.assertEqual(rc, 1)
                self.assertIn(message, err)
        self.reply = (2, b"", "beanjar: error: argument VERB: invalid choice: 'show'\n")
        rc, _out, err = self.connect("pi/tv")
        self.assertIn("is it an older beanjar? update it on the host", err)
        self.assertEqual(self.requests(), [], "no forward for a VM refused")
        self.assertEqual(self.held, [])

    def test_terminated(self):
        before = signal.getsignal(signal.SIGTERM)
        rc, _out, err = self.connect("pi/tv", ticks=[lambda: os.kill(os.getpid(), signal.SIGTERM)])
        self.assertEqual(rc, 128 + signal.SIGTERM, err)
        self.assertEqual(len(self.requests("cancel")), 1)
        self.assert_clean()
        self.assertIs(signal.getsignal(signal.SIGTERM), before, "handler restored")

    def test_killed(self):
        """A mitm connect killed outright leaves its forward; the next remote run cancels it."""
        code = (
            "import os, signal, sys, tempfile; sys.path.insert(0, sys.argv[1])\n"
            "from beanjar import ares, inspector\n"
            "home = tempfile.mkdtemp(prefix=f'{inspector.MITM_PREFIX}{os.getpid()}-',\n"
            "                        dir=sys.argv[2])\n"
            "with ares.Forward('pi', sys.argv[3], '127.0.1.5', 18083,\n"
            "                  os.path.join(home, ares.FORWARD_RECORD)):\n"
            "    os.kill(os.getpid(), signal.SIGKILL)\n"
        )
        manager = os.path.join(TOP, "host", "core")
        p = subprocess.run([sys.executable, "-c", code, manager, self.tmp,
                            "ssh://me@pi.example:2222"], check=False)  # fmt: skip
        self.assertEqual(p.returncode, -signal.SIGKILL)
        (left,) = self.forwards()
        self.assertEqual(self.connect("pi/tv")[0], 0)
        self.assertIn(left, [a[a.index("-L") + 1] for a in self.requests("cancel")])
        self.assert_clean()
        self.assertIn(inspector.MITM_PREFIX, ares.RECORD_PREFIXES)

    # the ares tests this class inherits do not apply
    test_run = test_master_started = test_host_errors = test_root = test_root_refused = None
    test_forward_failure = test_port_taken = test_interrupt = test_stale_home_swept = None


class MitmShowTest(unittest.TestCase):
    """mitm show's answer from inspector.snapshot and probe; the password only when asked."""

    def test_show(self):
        vm = mock.Mock(config={})
        vm.name = "tv"
        state = inspector.State("mitm", "Checking", "http://127.0.1.5:18083/", password="pw",
                                password_file="/run/p")  # fmt: skip
        with mock.patch.object(inspector, "snapshot", return_value=state), \
                mock.patch.object(inspector, "probe",
                                  side_effect=lambda s: inspector.dataclasses.replace(
                                      s, status="Running", ready=True)):  # fmt: skip
            data = inspector.show(vm)
            self.assertEqual(data, {"name": "tv", "mode": "mitm", "status": "Running",
                                    "ready": True, "url": "http://127.0.1.5:18083/",
                                    "local_url": None, "password_file": "/run/p",
                                    "mitmweb": None})  # fmt: skip
            self.assertEqual(inspector.show(vm, password=True)["password"], "pw")
        with mock.patch.object(inspector, "snapshot",
                               return_value=inspector.State("direct", "Not running")):  # fmt: skip
            data = inspector.show(vm)
        self.assertEqual((data["mode"], data["ready"], data["url"]), ("direct", False, None))
        with self.assertRaises(core.Error) as e:
            inspector.check_ready("tv", data)
        self.assertIn("network mode direct", str(e.exception))

    def test_connect_local(self):
        """A local VM's: its own address (local_url with link lan), no forward, until stdin ends."""
        vm = mock.Mock()
        vm.name = "tv"
        data = {"mode": "mitm", "status": "Running", "ready": True, "password": "pw",
                "url": "http://192.168.1.9:18083/", "local_url": "http://127.0.1.5:18083/"}  # fmt: skip
        out = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(b"anything\n"))
        with mock.patch.object(inspector, "show", return_value=data), \
                mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out):  # fmt: skip
            self.assertEqual(inspector.connect_local(vm), 0)
        self.assertEqual(json.loads(out.getvalue()),
                         {"url": "http://127.0.1.5:18083/", "password": "pw"})  # fmt: skip


class LightTest(Base):
    """ares-TOOL and the _ares query start quickly: the parser, the GPU, element and screensaver
    modules and the thread pool are not imported; the query's words are its own."""

    def test_imports(self):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); from beanjar import cli\n"
            "rc = cli.main(sys.argv[2:])\n"
            "heavy = ('argparse', 'concurrent.futures', 'beanjar.gpu', 'beanjar.elements',\n"
            "         'beanjar.screensaver')\n"
            "print(rc, [m for m in heavy if m in sys.modules])\n"
        )
        manager = os.path.join(TOP, "host", "core")
        for words, rc in (
            (["--library", os.path.join(self.tmp, "lib"), "_ares", "local/tv", "--json"], 1),
            (["ares-shell", "-d", "local/tv", "-r", "true"], 1),
        ):
            with self.subTest(words[0]):
                p = subprocess.run(
                    [sys.executable, "-c", code, manager, *words],
                    capture_output=True, text=True, check=True,
                )  # fmt: skip
                self.assertEqual(p.stdout.split("\n")[-2], f"{rc} []", p.stderr)
                self.assertIn("no VM named tv", p.stderr)

    def test_query_words(self):
        """_ares takes local/NAME, --root and --json; anything else is a usage error (2), which a
        newer caller reads as a beanjar to update."""
        for words in (
            ["_ares"],
            ["_ares", "local/tv", "--other"],
            ["_ares", "local/tv", "local/tv2"],
            ["_ares", "pi/tv"],
            ["--bogus", "_ares", "local/tv"],
        ):
            with self.subTest(words):
                rc, _out, err = self.beanjar(*words)
                self.assertEqual(rc, 2)
                self.assertTrue(json.loads(err.splitlines()[-1])["usage"])
        rc, _out, err = self.beanjar("--repo=/nowhere", "_ares", "tv", "--root")
        self.assertEqual((rc, json.loads(err)["error"]), (1, "no VM named tv"))

    def test_query_imports(self):
        """The query answers from base.py and the one VM's vm.toml: neither the library module
        (core.py) nor the commands, ares, keys or remote modules are imported, with --root (which
        reads the layers' metadata) or without."""
        lib = os.path.join(self.tmp, "lib")
        write_vm(lib, "tv", '[layers]\nenabled = ["normal", "devmode", "rootssh"]\n')
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); from beanjar import cli\n"
            "rc = cli.main(sys.argv[2:])\n"
            "heavy = ('beanjar.core', 'beanjar.commands', 'beanjar.ares', 'beanjar.keys',\n"
            "         'beanjar.remote', 'beanjar.targets', 'argparse', 'shutil', 'subprocess')\n"
            "print(rc, [m for m in heavy if m in sys.modules])\n"
        )
        manager = os.path.join(TOP, "host", "core")
        for root in ([], ["--root"]):
            with self.subTest(root=root):
                p = subprocess.run(
                    [sys.executable, "-c", code, manager, "--library", lib, "_ares", "local/tv",
                     *root, "--json"],
                    capture_output=True, text=True, check=True,
                )  # fmt: skip
                self.assertEqual(p.stdout.split("\n")[-2], "0 []", p.stderr)
                self.assertEqual(json.loads(p.stdout.split("\n")[0])["name"], "tv")

    def test_global_options(self):
        self.assertEqual(
            cli.global_options(["--library", "/l", "--repo=/r"]), {"library": "/l", "repo": "/r"}
        )
        self.assertEqual(cli.global_options([]), {"library": None, "repo": None})
        for words in (["--library"], ["-h"], ["--lib", "/l"], ["--json"]):
            self.assertIsNone(cli.global_options(words), words)


def write_vm(lib, name, toml="", slot=3, keys=("devmode_rsa", "id_ed25519")):
    """A VM folder of library lib, made by hand: vm.toml with slot (None: none) and toml's tables,
    and the identity keys named in keys."""
    d = os.path.join(lib, "vms", name)
    os.makedirs(os.path.join(d, "identity"))
    network = "" if slot is None else f"\n[network]\nslot = {slot}\n"
    with open(os.path.join(d, "vm.toml"), "w") as f:
        f.write(f'[firmware]\ndirectory = "firmware"\n{network}\n{toml}')
    for key in keys:
        with open(os.path.join(d, "identity", key), "w") as f:
            f.write(f"{name} {key}\n")
    return d


class QueryTest(Base):
    """The `_ares` query (cli.ares_query) reads one vm.toml without the library module; its answer,
    errors and exit status are those of the library's reading (ares.target of Library.vm)."""

    def setUp(self):
        super().setUp()
        self.lib = os.path.join(self.tmp, "lib")
        os.makedirs(os.path.join(self.lib, "vms"))

    def library(self, name, root):
        """The query's output as the library gives it: (status, stdout, stderr)."""
        try:
            vm = core.Library(self.lib).vm(name)
            return 0, json.dumps(ares.target(vm, root)) + "\n", ""
        except core.Error as e:
            error = {"error": str(e), **({"key": e.key} if e.key else {})}
            return 1, "", json.dumps(error) + "\n"

    def check(self, name, root, **expect):
        got = self.beanjar("_ares", f"local/{name}", *(["--root"] if root else []), "--json")
        self.assertEqual(got, self.library(name, root))
        if expect:
            answer = json.loads(got[1])
            self.assertEqual({k: answer[k] for k in expect}, expect)
        return got

    def run_vm(self, vm_dir):
        """A process standing in for the VM's QEMU (its command line names qemu-system), its pid
        in run/qemu.pid."""
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "qemu-system-aarch64"]
        )
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        os.makedirs(os.path.join(vm_dir, "run"))
        with open(os.path.join(vm_dir, "run", "qemu.pid"), "w") as f:
            f.write(f"{p.pid}\n")

    def test_running_and_stopped(self):
        layers = '[layers]\nenabled = ["normal", "devmode", "rootssh"]\n'
        self.run_vm(write_vm(self.lib, "on", layers))
        write_vm(self.lib, "off", layers, slot=4)
        for root, port, key in ((False, 9922, "devmode_rsa"), (True, 6622, "id_ed25519")):
            with self.subTest(root=root):
                self.check(
                    "on", root, running=True, address="127.0.1.3", port=port, key=f"on {key}\n"
                )
                self.check("off", root, running=False, address="127.0.1.4", port=port)

    def test_root_ssh(self):
        """--root: the root SSH port of a layer that forwards the guest's 22 (SSH_PORT replacing
        6622), None without one."""
        write_vm(self.lib, "plain")  # the default layers, normal and gl
        write_vm(self.lib, "hb", '[layers]\nenabled = ["devmode", "hbchannel"]\n', slot=5)
        write_vm(
            self.lib, "moved",
            '[layers]\nenabled = ["rootssh"]\n\n[advanced.env]\nSSH_PORT = "2222"\n', slot=6,
        )  # fmt: skip
        self.check("plain", True, layers=["normal", "gl"], port=None)
        self.check("plain", False, port=9922)
        self.check("hb", True, port=6622)
        self.check("moved", True, port=2222)

    def test_listen(self):
        write_vm(self.lib, "one", slot=None, toml='[network]\nslot = 7\nlisten = "10.0.0.2"\n')
        write_vm(self.lib, "all", slot=None, toml='[network]\nslot = 8\nlisten = "0.0.0.0"\n')
        self.check("one", False, address="10.0.0.2")
        self.check("all", False, address="127.0.1.8")

    def test_missing_vm_and_key(self):
        write_vm(self.lib, "bare", keys=())
        self.check("bare", False, key=None)
        self.check("bare", True, key=None)
        for name in ("nope", "bad name", ""):
            with self.subTest(name=name):
                rc, out, err = self.check(name, False)
                self.assertEqual((rc, out), (1, ""))
        self.assertEqual(json.loads(self.check("nope", True)[2]), {"error": "no VM named nope"})

    def test_library_reads_the_rest(self):
        """A vm.toml the light reading does not answer for goes to the library: a VM without a
        slot is given one, a setting of the wrong type is read()'s error."""
        write_vm(self.lib, "a")
        write_vm(self.lib, "new", slot=None)
        rc, out, err = self.beanjar("_ares", "local/new", "--json")
        self.assertEqual((rc, out), self.library("new", False)[:2])
        self.assertIn("vms/new/vm.toml had no slot; it has slot 1 now", err)
        self.assertEqual(json.loads(out)["address"], "127.0.1.1")
        for name, toml in (
            ("slot", '[network]\nslot = "x"\n'),
            ("layers", '[layers]\nenabled = "devmode"\n'),
            ("listen", '[network]\nslot = 9\nlisten = "nowhere"\n'),
            ("table", "[network]\nslot = 10\n\n[other]\nx = 1\n"),
        ):
            with self.subTest(name):
                write_vm(self.lib, name, toml, slot=None)
                rc, _out, err = self.check(name, False)
                self.assertEqual(rc, 1)
                self.assertIn(f"vms/{name}/vm.toml", json.loads(err)["error"])
        with open(os.path.join(self.lib, "vms", "a", "vm.toml"), "a") as f:
            f.write("[broken\n")
        rc, _out, err = self.check("a", False)
        self.assertIn("vms/a/vm.toml", json.loads(err)["error"])


if __name__ == "__main__":
    unittest.main()
