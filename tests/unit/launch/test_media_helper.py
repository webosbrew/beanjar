# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The media helper's start: its command line (qemu.plan's hub_argv: the two logs it owns, in its
own directory) and its spawn (launch.start_media_helper: a minimal environment, its sockets, the
pool's socket and QEMU's pidfd passed open, stdout and stderr to RUN_DIR/media-helper.out), and its
bubblewrap sandbox (mediasandbox.command)."""

import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from beanjar import launch, mediasandbox, paths, qemu

from launchkit import fake_tree

# what a desktop session hands its programs, the launcher among them
SESSION = {
    "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.sock",
    "GPG_AGENT_INFO": "/run/user/1000/gnupg/S.gpg-agent:0:1",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "GITHUB_TOKEN": "ghp_secret",
    "DISPLAY": ":0",
    "WAYLAND_DISPLAY": "wayland-0",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "PYTHONPATH": "/somewhere",
    "SHELL": "/bin/bash",
    "USER": "someone",
    "NETSTUB_WEB_PASSWORD_FILE": "/home/someone/.netstub-password",
    "MEDIA_HWDEC": "auto",
    # what the helper's libraries read
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/someone",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LD_LIBRARY_PATH": "/opt/driver/lib",
    "XDG_DATA_DIRS": "/usr/local/share:/usr/share",
    "XDG_CACHE_HOME": "/home/someone/.cache",
    "LIBVA_DRIVER_NAME": "d3d12",
    "GALLIUM_DRIVER": "d3d12",
    "MESA_D3D12_DEFAULT_ADAPTER_NAME": "NVIDIA",
    "VK_ICD_FILENAMES": "/usr/share/vulkan/icd.d/nvidia_icd.json",
    "CUDA_VISIBLE_DEVICES": "0",
}
KEPT = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LD_LIBRARY_PATH",
    "XDG_DATA_DIRS",
    "XDG_CACHE_HOME",
    "LIBVA_DRIVER_NAME",
    "GALLIUM_DRIVER",
    "MESA_D3D12_DEFAULT_ADAPTER_NAME",
    "VK_ICD_FILENAMES",
    "CUDA_VISIBLE_DEVICES",
}


class EnvironmentTest(unittest.TestCase):
    def test_only_what_the_decoders_read(self):
        env = launch.media_helper_environment(SESSION)
        self.assertEqual(env, {k: SESSION[k] for k in KEPT})

    def test_a_path_without_one(self):
        env = launch.media_helper_environment({"HOME": "/home/someone"})
        self.assertEqual(env, {"HOME": "/home/someone", "PATH": os.defpath})

    def test_not_changed(self):
        env = dict(SESSION)
        launch.media_helper_environment(env)
        self.assertEqual(env, SESSION)


class SpawnTest(unittest.TestCase):
    def setUp(self):
        self.run_dir = tempfile.mkdtemp(prefix="beanjar-launch-")
        self.addCleanup(shutil.rmtree, self.run_dir, ignore_errors=True)

    def plan(self, pool, env=None):
        media = os.path.join(self.run_dir, "media")
        return SimpleNamespace(
            run_dir=self.run_dir,
            env=dict(SESSION, **(env or {})),
            pool_socket=os.path.join(self.run_dir, "pool.sock") if pool else None,
            hub_dirs=[media],
            hub_viewers=[
                (os.path.join(self.run_dir, "video.sock"), "stream"),
                (os.path.join(self.run_dir, "media-0.sock"), "seqpacket"),
            ],
            hub_argv=[
                "/b/media-helper",
                "--pid",
                "PID",
                "--log",
                os.path.join(media, "L"),
                "--hwdec",
                "auto",
            ],
        )

    def spawn(self, p, bwrap=None, problem=""):
        seen = {}

        def popen(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            seen["fds"] = {}
            # what is passed is open: listening sockets and a pidfd
            for fd in kw["pass_fds"]:
                try:
                    s = socket.socket(fileno=os.dup(fd))
                except OSError:
                    seen["fds"][fd] = "pidfd"
                    continue
                seen["fds"][fd] = (s.type, s.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
                s.close()
            seen["out"] = kw["stdout"].name
            return mock.Mock()

        with (
            mock.patch.object(launch.subprocess, "Popen", side_effect=popen),
            mock.patch.object(launch.mediasandbox, "find", return_value=bwrap),
            mock.patch.object(launch.mediasandbox, "problem", return_value=problem),
        ):
            launch.start_media_helper(p)
        return seen

    def test_spawn(self):
        """Without bwrap: the helper itself, its sockets and QEMU's pidfd passed open, its
        directory made, the reason on media-helper.out."""
        p = self.plan(pool=True)
        seen = self.spawn(p)
        argv, kw = seen["argv"], seen["kw"]
        video, media, pool, pidfd = kw["pass_fds"]
        self.assertEqual(
            argv,
            [
                "/b/media-helper",
                "--pid-fd",
                str(pidfd),
                "--log",
                os.path.join(self.run_dir, "media", "L"),
                "--hwdec",
                "auto",
                "--viewer-fds",
                f"{video},{media}",
                "--pool-fd",
                str(pool),
            ],
        )
        self.assertEqual(seen["fds"][video], (socket.SOCK_STREAM, 1))
        self.assertEqual(seen["fds"][media], (socket.SOCK_SEQPACKET, 1))
        self.assertEqual(seen["fds"][pool], (socket.SOCK_STREAM, 1))
        self.assertEqual(seen["fds"][pidfd], "pidfd")
        self.assertEqual(kw["env"], {k: SESSION[k] for k in KEPT})
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertIs(kw["stdout"], kw["stderr"])
        self.assertEqual(seen["out"], os.path.join(self.run_dir, "media-helper.out"))
        self.assertTrue(kw["close_fds"])
        self.assertTrue(os.path.exists(p.pool_socket))
        for path, _ in p.hub_viewers:
            self.assertTrue(os.path.exists(path))
        self.assertEqual(os.stat(p.hub_dirs[0]).st_mode & 0o777, 0o700)
        for fd in kw["pass_fds"]:
            with self.assertRaises(OSError):
                os.fstat(fd)  # the launcher's copies closed once the child has them
        with open(seen["out"]) as f:
            self.assertIn("media sandbox: no bubblewrap (bubblewrap (bwrap) not found)", f.read())

    def test_spawn_without_a_pool(self):
        seen = self.spawn(self.plan(pool=False))
        self.assertNotIn("--pool-fd", seen["argv"])
        self.assertEqual(len(seen["kw"]["pass_fds"]), 3)

    def test_in_bwrap(self):
        """With a usable bwrap the helper runs in it, HOME a private /tmp."""
        p = self.plan(pool=True)
        seen = self.spawn(p, bwrap="/usr/bin/bwrap")
        argv, env = seen["argv"], seen["kw"]["env"]
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        helper = argv.index("--") + 1
        self.assertEqual(argv[helper], "/b/media-helper")
        self.assertIn("--unshare-net", argv[:helper])
        self.assertIn("--pid-fd", argv[helper:])
        self.assertNotIn("--no-sandbox", argv)
        self.assertEqual(env["HOME"], "/tmp")
        self.assertNotIn("XDG_CACHE_HOME", env)
        with open(seen["out"]) as f:
            self.assertEqual(f.read(), "")

    def test_bwrap_unusable(self):
        seen = self.spawn(self.plan(pool=False), bwrap="/usr/bin/bwrap", problem="no userns")
        self.assertEqual(seen["argv"][0], "/b/media-helper")
        with open(seen["out"]) as f:
            self.assertIn("no bubblewrap (no userns): its own confinement only", f.read())

    def test_off(self):
        seen = self.spawn(self.plan(pool=False, env={"MEDIA_SANDBOX": "off"}), bwrap="/bwrap")
        self.assertEqual(seen["argv"][0], "/b/media-helper")
        self.assertEqual(seen["argv"][-1], "--no-sandbox")

    def test_bad_setting(self):
        with self.assertRaises(launch.Error):
            self.spawn(self.plan(pool=False, env={"MEDIA_SANDBOX": "yes"}))


class SandboxTest(unittest.TestCase):
    """mediasandbox.command: what the helper's bwrap binds."""

    def command(self, hwdec="auto", env=None, nodes=("/dev/dri/renderD128", "/dev/dxg")):
        def fake_glob(pattern):
            return [n for n in nodes if n.startswith(pattern.rstrip("*"))]

        with mock.patch.object(mediasandbox.glob, "glob", side_effect=fake_glob):
            return mediasandbox.command(
                "/usr/bin/bwrap",
                "/usr/libexec/beanjar/media-helper",
                ["/home/someone/vm/run/media", "/run/user/1000/beanjar-x/media"],
                env or {"PATH": "/usr/bin"},
                hwdec,
            )

    def binds(self, cmd, option):
        return [cmd[i + 2] for i, a in enumerate(cmd) if a == option]

    def test_writes_only_its_directories(self):
        cmd = self.command()
        self.assertEqual(
            self.binds(cmd, "--bind"),
            ["/home/someone/vm/run/media", "/run/user/1000/beanjar-x/media"],
        )
        self.assertEqual(cmd[-1], "--")
        for option in ("--unshare-net", "--unshare-pid", "--unshare-ipc", "--new-session"):
            self.assertIn(option, cmd)
        # nothing of the home directory or the user's runtime directory besides those
        for target in self.binds(cmd, "--ro-bind") + self.binds(cmd, "--ro-bind-try"):
            self.assertFalse(target.startswith(("/home", "/run/user")), target)

    def test_hardware_nodes(self):
        cmd = self.command()
        self.assertEqual(self.binds(cmd, "--dev-bind"), ["/dev/dri/renderD128", "/dev/dxg"])
        self.assertIn("/sys", self.binds(cmd, "--ro-bind"))

    def test_software_only(self):
        cmd = self.command(hwdec="none")
        self.assertEqual(self.binds(cmd, "--dev-bind"), [])
        self.assertNotIn("/sys", self.binds(cmd, "--ro-bind"))
        self.assertIn("/sys/devices/system/cpu", self.binds(cmd, "--ro-bind-try"))

    def test_driver_paths(self):
        """What the decoders' drivers are told to load from, read-only; /usr's is there already."""
        tmp = tempfile.mkdtemp(prefix="beanjar-sandbox-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = {"LD_LIBRARY_PATH": f"{tmp}:/usr/lib/x", "VK_ICD_FILENAMES": f"{tmp}/icd.json"}
        cmd = self.command(env=env)
        ro = self.binds(cmd, "--ro-bind-try")
        self.assertIn(tmp, ro)
        self.assertIn(f"{tmp}/icd.json", ro)
        self.assertNotIn("/usr/lib/x", self.binds(cmd, "--ro-bind-try"))

    def test_checkout_ffmpeg(self):
        """A checkout's helper: the binary and build/ffmpeg-helper beside it (its RUNPATH), not
        the full build/ffmpeg."""
        tmp = tempfile.mkdtemp(prefix="beanjar-sandbox-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        helper = os.path.join(tmp, "build/media-helper/media-helper")
        os.makedirs(os.path.dirname(helper))
        os.makedirs(os.path.join(tmp, "build/ffmpeg-helper/lib"))
        os.makedirs(os.path.join(tmp, "build/ffmpeg/lib"))
        cmd = mediasandbox.command("/bwrap", helper, [], {}, "none")
        binds = self.binds(cmd, "--ro-bind-try")
        self.assertIn(helper, binds)
        self.assertIn(os.path.join(tmp, "build/ffmpeg-helper"), binds)
        self.assertNotIn(os.path.join(tmp, "build/ffmpeg"), binds)

    def test_environment(self):
        env = mediasandbox.environment(launch.media_helper_environment(SESSION))
        self.assertEqual(env["HOME"], "/tmp")
        for key in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            self.assertNotIn(key, env)
        self.assertEqual(env["LIBVA_DRIVER_NAME"], "d3d12")


class ArgvTest(unittest.TestCase):
    def plan(self, **extra):
        """qemu.plan of a fake tree with a media helper, env extra added: (plan, top, run)."""
        tmp = tempfile.mkdtemp(prefix="beanjar-launch-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        top, fw, state = fake_tree(tmp)
        helper = os.path.join(top, "build/media-helper/media-helper")
        os.makedirs(os.path.dirname(helper))
        with open(helper, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(helper, 0o755)
        run = os.path.join(top, "build")
        env = {
            "FW": fw,
            "STATE": state,
            "RUN_DIR": run,
            "LOG_DIR": run,
            "IDENTITY_DIR": os.path.join(top, "local"),
            "SLOT": "1",
            **extra,
        }
        with (
            mock.patch.object(qemu, "first_rendernode", return_value=None),
            mock.patch.object(qemu, "is_char", side_effect=lambda p: p == "/dev/dxg"),
            mock.patch.object(qemu, "is_vgem", return_value=False),
            mock.patch.object(qemu, "render_driver", return_value=""),
            mock.patch.object(qemu.platform, "machine", return_value="x86_64"),
            mock.patch.object(qemu, "host_aarch32_el0", return_value=None),
        ):
            p = qemu.plan(env, [], accel="tcg", tree=paths.Tree(top), say=lambda m: None, dry=True)
        return p, helper, run

    def test_owned_logs(self):
        """The helper is given its two logs in its own RUN_DIR/media: the sessions' and the
        decoder's."""
        p, helper, run = self.plan()
        argv = p.hub_argv
        media = os.path.join(run, "media")
        self.assertEqual(argv[0], helper)
        self.assertEqual(argv[argv.index("--log") + 1], os.path.join(media, "media-sessions.log"))
        self.assertEqual(
            argv[argv.index("--decode-log") + 1], os.path.join(media, "media-helper.log")
        )
        self.assertEqual(argv[argv.index("--streams") + 1], os.path.join(media, "media-streams"))
        # QEMU's sockets for its ports are in its directory, the viewers' where they always were
        self.assertEqual(
            argv[argv.index("--video") + 1],
            f"{media}/qemu-video.sock:{run}/video.sock",
        )
        self.assertEqual(argv[argv.index("--media") + 1], f"{media}/qemu-media:{run}/media")
        self.assertEqual(p.hub_dirs, [media])
        self.assertEqual(
            p.hub_viewers,
            [(f"{run}/video.sock", "stream")]
            + [(f"{run}/media-{n}.sock", "seqpacket") for n in range(qemu.MEDIA_PORTS)],
        )

    def test_hwdec_auto(self):
        """auto, the default: --hwdec auto, not locked."""
        argv = self.plan()[0].hub_argv
        self.assertEqual(argv[argv.index("--hwdec") + 1], "auto")
        self.assertNotIn("--software-only", argv)
        argv = self.plan(MEDIA_HWDEC="vaapi")[0].hub_argv
        self.assertEqual(argv[argv.index("--hwdec") + 1], "vaapi")
        self.assertNotIn("--software-only", argv)

    def test_hwdec_none_is_software_only(self):
        """hwdec=none: --hwdec none and --software-only, the sandbox on or by default."""
        for sandbox in ({}, {"MEDIA_SANDBOX": "on"}):
            argv = self.plan(MEDIA_HWDEC="none", **sandbox)[0].hub_argv
            self.assertEqual(argv[argv.index("--hwdec") + 1], "none")
            self.assertIn("--software-only", argv)

    def test_hwdec_none_refuses_the_sandbox_off(self):
        with self.assertRaisesRegex(qemu.Error, "MEDIA_SANDBOX=off is refused"):
            self.plan(MEDIA_HWDEC="none", MEDIA_SANDBOX="off")
        self.plan(MEDIA_HWDEC="auto", MEDIA_SANDBOX="off")  # only with none

    def test_hwdec_unknown(self):
        with self.assertRaisesRegex(qemu.Error, "MEDIA_HWDEC=d3d11va: one of"):
            self.plan(MEDIA_HWDEC="d3d11va")


if __name__ == "__main__":
    unittest.main()
