# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A qemu-user VM's swap in host RAM (docs/dev/architecture/qemu-user-bridge.md, "Memory").

Under qemu-user every 32-bit process carries a qemu-arm of its own, and most of that (its
translated-code buffer above all) is touched once and then left cold. The guest gets a swap disk
for it: a virtio-blk disk with the serial SERIAL whose backing is a memfd this host creates when
QEMU starts. The memfd is sparse, so host RAM goes only to pages the guest has swapped out (and the
host may swap those in turn); the guest swaps with discard, so a page it frees is a hole punched in
the memfd again. The memfd lives in QEMU's file table only: it goes when QEMU does, crashed or
not, with nothing on disk to clean up.

The disk's -drive opens /dev/fdset/FDSET, the set -add-fd puts the memfd in at the fixed descriptor
FD, read-write, and again at FD_RO, read-only: QEMU takes from the set a descriptor with the
access mode it opens with, and it opens the disk read-only as well as read-write. argv() writes
them; open_fds() makes the memfd for an argv that has them, in the process that
runs QEMU (launch.py, netns.py), just before it does. guest/initramfs/rootfs/init finds the disk
by its serial and runs mkswap and swapon --discard on it before webOS starts.

The memfd is Linux's: a host without one (macOS) needs another anonymous, sparse, hole-punchable
backing in _backing; none is built yet, and open_fds says so."""

import os

from ..base import Error

# the disk's serial, which the guest's init looks for, and the -add-fd opaque string's prefix
SERIAL = "beanjar-swap"
# the descriptor QEMU inherits the memfd at, and the fdset -add-fd puts it in. Fixed, so that
# plan() writes the whole command line; high, above what a launcher process has open.
FD = 200
FD_RO = 201
FDSET = 9


def argv(size_mib):
    """QEMU arguments of the swap disk of size_mib MiB: the memfd (-add-fd, its size in the opaque
    string for open_fds) and the virtio-blk disk on it. cache=unsafe: what the guest writes there
    never needs to reach anything but host RAM; discard=unmap: its swap's discards punch holes."""
    return [
        "-add-fd",
        f"fd={FD},set={FDSET},opaque={SERIAL}:{int(size_mib)}M",
        "-add-fd",
        f"fd={FD_RO},set={FDSET},opaque={SERIAL}-ro",
        "-drive",
        f"if=none,id={SERIAL},format=raw,file=/dev/fdset/{FDSET},cache=unsafe,discard=unmap",
        "-device",
        f"virtio-blk-pci,drive={SERIAL},serial={SERIAL}",
    ]


def size_mib(qemu_argv):
    """The swap disk's size in MiB argv() wrote into qemu_argv, None if it has none."""
    for k, a in zip(qemu_argv, qemu_argv[1:]):
        if k == "-add-fd" and f"opaque={SERIAL}:" in a:
            return int(a.split(f"opaque={SERIAL}:", 1)[1].split(",", 1)[0].rstrip("M"))
    return None


def _backing(size):
    """A new anonymous, sparse file of size bytes (a memfd), its descriptor."""
    if not hasattr(os, "memfd_create"):
        raise Error("hardware: a qemu-user VM's swap needs memfd_create, which this host lacks")
    fd = os.memfd_create(SERIAL, os.MFD_CLOEXEC)
    try:
        os.ftruncate(fd, size)
    except OSError:
        os.close(fd)
        raise
    return fd


def _place(fd, at):
    """fd moved to the descriptor at, inheritable."""
    if fd != at:
        os.dup2(fd, at)  # inheritable
        os.close(fd)
    else:
        os.set_inheritable(fd, True)


def open_fds(qemu_argv):
    """The descriptors QEMU (qemu_argv) inherits: for a command line with argv()'s swap disk, a new
    memfd of its size at FD, read-write, and the same file at FD_RO, read-only, both inheritable
    (for exec, or pass_fds); () for one without. The caller closes them once QEMU has them
    (close_fds)."""
    size = size_mib(qemu_argv)
    if size is None:
        return ()
    for at in (FD, FD_RO):
        try:
            os.fstat(at)
        except OSError:
            continue
        raise Error(f"swap: descriptor {at}, the swap disk's, is already open in the launcher")
    fd = _backing(size << 20)
    try:
        ro = os.open(f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        os.close(fd)
        raise
    _place(fd, FD)
    _place(ro, FD_RO)
    return (FD, FD_RO)


def close_fds(fds):
    """Close the launcher's copies of open_fds' descriptors: QEMU holds the memfd alone."""
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass
