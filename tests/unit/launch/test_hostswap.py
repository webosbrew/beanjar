# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""hostswap.py: a qemu-user VM's swap disk in host RAM, the memfd QEMU inherits at hostswap.FD."""

import fcntl
import os
import subprocess
import sys
import unittest

from beanjar import base
from beanjar.runtime import hostswap


class HostSwapTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(hostswap.close_fds, (hostswap.FD, hostswap.FD_RO))

    def test_open_fds(self):
        """A command line with the swap disk gets a sparse memfd of its size at FD, read-write, and
        at FD_RO, read-only, both inheritable, which a child started with pass_fds sees as the same
        file; one without gets nothing."""
        self.assertEqual(hostswap.open_fds(["qemu", "-m", "2048"]), ())
        argv = ["qemu", *hostswap.argv(64)]
        self.assertEqual(hostswap.size_mib(argv), 64)
        fds = hostswap.open_fds(argv)
        self.assertEqual(fds, (hostswap.FD, hostswap.FD_RO))
        self.assertEqual(fcntl.fcntl(hostswap.FD, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDWR)
        self.assertEqual(fcntl.fcntl(hostswap.FD_RO, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
        self.assertEqual(os.fstat(hostswap.FD_RO).st_ino, os.fstat(hostswap.FD).st_ino)
        self.assertTrue(os.get_inheritable(hostswap.FD_RO))
        st = os.fstat(hostswap.FD)
        self.assertEqual(st.st_size, 64 << 20)
        self.assertEqual(st.st_blocks, 0)  # sparse: no host RAM until the guest swaps
        self.assertTrue(os.get_inheritable(hostswap.FD))
        self.assertIn("memfd:beanjar-swap", os.readlink(f"/proc/self/fd/{hostswap.FD}"))
        child = subprocess.run(
            [sys.executable, "-c", f"import os; print(os.fstat({hostswap.FD}).st_size)"],
            pass_fds=fds,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        self.assertEqual(child.stdout.strip(), str(64 << 20))
        # FD taken (here by the memfd itself): refused, not clobbered
        with self.assertRaisesRegex(base.Error, f"descriptor {hostswap.FD}"):
            hostswap.open_fds(argv)
        hostswap.close_fds(fds)
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_no_memfd(self):
        """A host without memfd_create (macOS) is told so; nothing is opened."""
        argv = ["qemu", *hostswap.argv(64)]
        saved = os.memfd_create
        del os.memfd_create
        try:
            with self.assertRaisesRegex(base.Error, "needs memfd_create"):
                hostswap.open_fds(argv)
        finally:
            os.memfd_create = saved
        with self.assertRaises(OSError):
            os.fstat(hostswap.FD)


if __name__ == "__main__":
    unittest.main()
