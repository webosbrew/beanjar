#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""scripts/buildlock.py: the build lock's modes, what a waiter says, and no deadlock for a holder's
own commands (BUILD_LOCKED, an ancestor's flock)."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import buildlock  # noqa: E402

# a process that holds PATH in MODE (flock's) until its stdin closes
HOLD = (
    "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
    "fcntl.flock(fd, fcntl.LOCK_EX if sys.argv[2] == 'exclusive' else fcntl.LOCK_SH); "
    "open(sys.argv[1] + '.who', 'w').write(f'pid {os.getpid()} {sys.argv[2]}: holder\\n'); "
    "print('held', flush=True); sys.stdin.read()"
)
# acquire PATH in MODE below the caller and say whether it was the caller's
CHILD = (
    f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); import buildlock; "
    "l = buildlock.Lock(sys.argv[2], 'child', path=sys.argv[1], say=print)\n"
    "try:\n l.acquire(); print('held' if l.held else 'took')\n"
    "except buildlock.Held as e:\n print('Held', e)"
)


class BuildLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "build", ".build.lock")
        os.makedirs(os.path.dirname(self.path))
        self.env = dict(os.environ)
        self.env.pop(buildlock.ENV, None)
        self.old = os.environ.pop(buildlock.ENV, None)

    def tearDown(self):
        if self.old is not None:
            os.environ[buildlock.ENV] = self.old
        self.tmp.cleanup()

    def holder(self, mode):
        p = subprocess.Popen(
            [sys.executable, "-c", HOLD, self.path, mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(p.stdout.readline().strip(), "held")
        self.addCleanup(lambda: (p.stdin.close(), p.wait()))
        return p

    def test_waiter_says_who_once(self):
        p = self.holder("exclusive")
        said = []
        lock = buildlock.Lock("shared", "regress", path=self.path, say=said.append)
        t = threading.Thread(target=lock.acquire)
        t.start()
        time.sleep(0.5)
        self.assertTrue(t.is_alive())  # waits
        self.assertEqual(len(said), 1)
        self.assertIn(f"held by pid {p.pid} exclusive: holder", said[0])
        p.stdin.close()
        p.wait()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertFalse(lock.held)
        self.assertIn(f"pid {os.getpid()} shared: regress", Path(self.path + ".who").read_text())
        lock.release()

    def test_shared_holders_share(self):
        self.holder("shared")
        said = []
        with buildlock.Lock("shared", path=self.path, say=said.append) as lock:
            self.assertIsNotNone(lock.fd)
        self.assertEqual(said, [])

    def test_downgrade_lets_readers_in(self):
        lock = buildlock.Lock("exclusive", path=self.path, say=lambda s: None).acquire()
        lock.downgrade()
        self.assertEqual(lock.mode, "shared")
        self.holder("shared")  # would block while exclusive
        lock.release()

    def test_build_locked_takes_none(self):
        self.holder("exclusive")
        os.environ[buildlock.ENV] = "1"
        try:
            lock = buildlock.Lock("exclusive", path=self.path).acquire()
        finally:
            del os.environ[buildlock.ENV]
        self.assertTrue(lock.held)
        self.assertIsNone(lock.fd)

    @unittest.skipUnless(shutil.which("flock") and os.path.exists("/proc/locks"), "no flock(1)")
    def test_outer_flock_is_the_callers(self):
        def under(flags, mode):
            return subprocess.run(
                ["flock", *flags, self.path, sys.executable, "-c", CHILD, self.path, mode],
                capture_output=True,
                text=True,
                timeout=20,
                env=self.env,
            ).stdout

        self.assertEqual(under([], "exclusive").strip(), "held")
        self.assertEqual(under([], "shared").strip(), "held")
        self.assertEqual(under(["-s"], "shared").strip(), "took")
        out = under(["-s"], "exclusive")
        self.assertTrue(out.startswith("Held "), out)
        self.assertIn("held shared by this command's caller", out)

    def test_main_execs_with_build_locked(self):
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); import buildlock; "
                "buildlock.LOCK = sys.argv[1]; sys.exit(buildlock.main(sys.argv[2:]))",
                self.path,
                "exclusive",
                "--",
                "sh",
                "-c",
                'echo "$BUILD_LOCKED"; grep -c FLOCK /proc/locks >/dev/null || true',
            ],
            capture_output=True,
            text=True,
            timeout=20,
            env=self.env,
        )
        self.assertEqual(out.stdout.split()[0], "1", out.stderr)
        self.assertIn(" exclusive: sh -c", Path(self.path + ".who").read_text())

    def test_usage(self):
        self.assertEqual(buildlock.main(["both", "--", "true"]), 2)


if __name__ == "__main__":
    unittest.main()
