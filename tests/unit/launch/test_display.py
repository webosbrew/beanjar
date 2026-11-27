# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The display set-up (secondary.py) and qemu.py's GPU backend and GPU process."""

import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


from beanjar.display import secondary
from beanjar import launch, qemu
from beanjar.network import netns

from launchkit import TOP


class DisplaySetupTest(unittest.TestCase):
    def test_stock_console_protocol(self):
        connection = mock.Mock()
        gio = types.SimpleNamespace(
            Cancellable=mock.Mock(),
            DBusConnectionFlags=types.SimpleNamespace(
                AUTHENTICATION_CLIENT=1, MESSAGE_BUS_CONNECTION=2
            ),
            DBusCallFlags=types.SimpleNamespace(NONE=0),
            DBusConnection=mock.Mock(),
        )
        gio.DBusConnection.new_for_address_sync.return_value = connection
        variant = mock.Mock(side_effect=lambda signature, values: (signature, values))
        with mock.patch.dict(
            sys.modules,
            {
                "gi.repository": types.SimpleNamespace(
                    Gio=gio, GLib=types.SimpleNamespace(Variant=variant)
                )
            },
        ):
            secondary.set_ui_info("unix:path=bus", 1)
        call = connection.call_sync.call_args.args
        self.assertEqual(
            call[:4],
            ("org.qemu", "/org/qemu/Display1/Console_1", "org.qemu.Display1.Console", "SetUIInfo"),
        )
        self.assertEqual(call[4], ("(qqiiuu)", (200, 500, 0, 0, 768, 2160)))
        connection.close_sync.assert_called_once()

    def test_network_off_defers_cont_until_display_setup(self):
        class Stream(io.StringIO):
            def readline(self):
                return '{"return": {}}\n'

            def close(self):
                pass

        stream = Stream()
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.makefile.return_value = stream
        events = []

        def setup():
            commands = [json.loads(line)["execute"] for line in stream.getvalue().splitlines()]
            events.extend(commands)
            events.append("display")

        with mock.patch.object(netns.socket, "socket", return_value=sock):
            netns._qmp("qmp", mock.Mock(), before_cont=setup)
        self.assertEqual(events, ["qmp_capabilities", "set_link", "display"])

    def test_explicit_pause_never_resumes(self):
        command = mock.Mock()
        secondary.prepare(
            "bus",
            "qmp",
            1,
            resume=False,
            configure=mock.Mock(),
            command=command,
            alive=lambda: True,
        )
        command.assert_not_called()

    def test_console_before_resume(self):
        events = []

        def configure(address, timeout):
            events.append(("console", address))
            if len(events) == 1:
                raise OSError("console not yet exported")

        def command(sock, cmd, timeout):
            events.append(("qmp", json.loads(cmd)["execute"]))
            return {"return": {}}

        secondary.prepare(
            "bus",
            "qmp",
            1,
            configure=configure,
            command=command,
            alive=lambda: True,
            sleep=lambda _: None,
        )
        self.assertEqual(events, [("console", "bus"), ("console", "bus"), ("qmp", "cont")])

    def test_timeout_never_resumes(self):
        command = mock.Mock()
        ticks = iter((0, 0, 1, 2, 3))
        with self.assertRaises(secondary.Error):
            secondary.prepare(
                "bus",
                "qmp",
                1,
                deadline=2,
                configure=mock.Mock(side_effect=OSError("missing console")),
                command=command,
                alive=lambda: True,
                clock=lambda: next(ticks),
                sleep=lambda _: None,
            )
        command.assert_not_called()

    def test_parent_exit_never_resumes(self):
        command = mock.Mock()
        with self.assertRaises(secondary.Error):
            secondary.prepare("bus", "qmp", 1, command=command, alive=lambda: False)
        command.assert_not_called()

    def test_main_pause(self):
        for options, resume in (([], True), (["--keep-paused"], False)):
            with (
                self.subTest(options=options),
                mock.patch.object(sys, "argv", ["helper", "1", "bus", "qmp", *options]),
                mock.patch.object(secondary, "prepare") as prepare,
            ):
                self.assertEqual(secondary.main(), 0)
                prepare.assert_called_once_with("bus", "qmp", 1, resume=resume)

    def test_failure_quits_paused_vm(self):
        with (
            mock.patch.object(sys, "argv", ["helper", "1", "bus", "qmp"]),
            mock.patch.object(secondary, "prepare", side_effect=secondary.Error("failed")),
            mock.patch.object(launch, "qmp", return_value={"return": {}}) as command,
        ):
            self.assertEqual(secondary.main(), 1)
        self.assertEqual(json.loads(command.call_args.args[1]), {"execute": "quit"})

    def test_failed_quit_terminates_original_parent(self):
        with (
            mock.patch.object(sys, "argv", ["helper", "123", "bus", "qmp"]),
            mock.patch.object(secondary, "prepare", side_effect=secondary.Error("failed")),
            mock.patch.object(launch, "qmp", side_effect=OSError()),
            mock.patch.object(os, "getppid", return_value=123),
            mock.patch.object(os, "kill") as kill,
        ):
            self.assertEqual(secondary.main(), 1)
        kill.assert_called_once_with(123, signal.SIGTERM)


class GpuBackendTest(unittest.TestCase):
    """beanjar.display.gpu_backend: the launcher's listening vhost-user socket, QEMU's one connection
    handed to the backend as --fd, and no wait past the VM's PID."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-gpu-")
        self.sock = os.path.join(self.tmp, "vhost-user-gpu.sock")
        # a stand-in backend: writes its arguments and what arrives on its --fd
        self.backend = os.path.join(self.tmp, "backend")
        with open(self.backend, "w") as f:
            f.write(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "fd = int(sys.argv[-1].split('=')[1])\n"
                f"open({os.path.join(self.tmp, 'out')!r}, 'w').write(' '.join(sys.argv[1:]) + "
                "' ' + os.read(fd, 16).decode())\n"
            )
        os.chmod(self.backend, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, pid):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.sock)
        listener.listen(1)
        env = dict(os.environ, PYTHONPATH=os.path.join(TOP, "host", "core"))
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "beanjar.display.gpu_backend",
                str(pid),
                str(listener.fileno()),
                self.backend,
                "--virgl",
                "--render-node=/dev/dri/renderD128",
            ],
            env=env,
            pass_fds=(listener.fileno(),),
            stderr=subprocess.PIPE,
        )
        listener.close()
        return child

    def test_qemu_connection_becomes_backend_fd(self):
        child = self.start(os.getpid())
        # QEMU's connect: accepted from the backlog even before the helper runs
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as qemu_end:
            qemu_end.connect(self.sock)
            qemu_end.sendall(b"hello")
            self.assertEqual(child.wait(10), 0)
        with open(os.path.join(self.tmp, "out")) as f:
            words = f.read().split()
        self.assertEqual(words[:2], ["--virgl", "--render-node=/dev/dri/renderD128"])
        self.assertRegex(words[2], r"^--fd=\d+$")
        self.assertEqual(words[3], "hello")

    def test_gives_up_when_the_vm_ends(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        child = self.start(gone.pid)
        self.assertEqual(child.wait(10), 1)
        self.assertIn(b"ended before QEMU connected", child.stderr.read())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out")))


class GpuProcessTest(unittest.TestCase):
    """qemu.gpu_process (GPU_DEVICE): auto on an aarch64 host with a GPU's
    render node, a guest with two outputs too (its abort comes only on the way down, and a
    restart is a new backend); asked for, with an error where it cannot run."""

    def decide(self, env, machine="aarch64", node="/dev/dri/renderD128", backend=True):
        notes = []
        with (
            mock.patch.object(qemu.platform, "machine", return_value=machine),
            mock.patch.object(qemu, "is_char", side_effect=lambda p: p == "/dev/dri/renderD128"),
            mock.patch.object(qemu, "is_vgem", return_value=False),
            mock.patch.object(qemu.os, "access", return_value=backend),
        ):
            got = qemu.gpu_process(
                dict(env, VHOST_USER_GPU="/b/vhost-user-gpu"),
                node,
                "q",
                say=notes.append,
            )
        return got, notes

    def test_auto(self):
        """The default: the GPU process on ARM with a GPU's node, virtio-gpu-gl elsewhere."""
        self.assertEqual(self.decide({}), (True, []))
        self.assertEqual(self.decide({}, machine="x86_64"), (False, []))
        self.assertFalse(self.decide({}, node="surfaceless")[0])
        got, notes = self.decide({}, backend=False)
        self.assertFalse(got)
        self.assertIn("no vhost-user-gpu backend", notes[0])

    def test_forced(self):
        self.assertEqual(self.decide({"GPU_DEVICE": "vhost-user-gpu"}), (True, []))
        self.assertEqual(
            self.decide({"GPU_DEVICE": "vhost-user-gpu"}, machine="x86_64"), (True, [])
        )
        self.assertEqual(self.decide({"GPU_DEVICE": "virtio-gpu-gl"}), (False, []))
        for env, node, backend in (
            ({"GPU_DEVICE": "vhost-user-gpu"}, "surfaceless", True),
            ({"GPU_DEVICE": "vhost-user-gpu"}, "/dev/dri/renderD128", False),
            ({"GPU_DEVICE": "on"}, "/dev/dri/renderD128", True),
        ):
            with self.subTest(env=env, node=node, backend=backend):
                with self.assertRaises(qemu.Error):
                    self.decide(env, node=node, backend=backend)


class GlRendererLineTest(unittest.TestCase):
    """qemu.parse_eglinfo_renderer and gl_renderer_line: the launch log's GL renderer line."""

    D3D12 = (
        "Surfaceless platform:\n"
        "OpenGL core profile renderer: D3D12 (Intel(R) UHD Graphics 770)\n"
        "OpenGL compatibility profile renderer: D3D12 (Intel(R) UHD Graphics 770)\n"
        "OpenGL ES profile renderer: D3D12 (Intel(R) UHD Graphics 770)\n"
    )

    def test_parse_names_the_adapter(self):
        self.assertEqual(
            qemu.parse_eglinfo_renderer(self.D3D12), "D3D12 (Intel(R) UHD Graphics 770)"
        )

    def test_parse_falls_back_to_core_profile(self):
        text = "OpenGL core profile renderer: llvmpipe (LLVM 19.1.7, 256 bits)\n"
        self.assertEqual(qemu.parse_eglinfo_renderer(text), "llvmpipe (LLVM 19.1.7, 256 bits)")

    def test_parse_none(self):
        self.assertIsNone(qemu.parse_eglinfo_renderer("eglinfo: eglInitialize failed\n"))

    def test_line_runs_eglinfo_on_the_surfaceless_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "eglinfo")
            with open(exe, "w") as f:
                f.write(
                    f"#!{sys.executable}\nimport os, sys\n"
                    "assert sys.argv[1:] == ['-B', '-p', 'surfaceless'], sys.argv\n"
                    "assert os.environ['EGL_PLATFORM'] == 'surfaceless'\n"
                    f"print({self.D3D12!r})\n"
                )
            os.chmod(exe, 0o755)
            line = qemu.gl_renderer_line("surfaceless", {"PATH": tmp})
        self.assertEqual(line, "gl renderer: D3D12 (Intel(R) UHD Graphics 770)")

    def test_line_without_eglinfo(self):
        self.assertEqual(
            qemu.gl_renderer_line("surfaceless", {"PATH": "/nonexistent"}),
            "gl renderer: unknown (eglinfo not installed)",
        )

    def test_line_for_another_render_node(self):
        with mock.patch.object(qemu, "first_rendernode", return_value="/dev/dri/renderD128"):
            self.assertEqual(
                qemu.gl_renderer_line("/dev/dri/renderD129", {}),
                "gl renderer: not probed (render node /dev/dri/renderD129)",
            )
