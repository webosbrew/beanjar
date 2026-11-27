# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's snapshots: `beanjar snapshot create|list|show|delete|reset NAME [SNAP]` (host/core/README.md,
"Snapshots"; the study: docs/research/vm-snapshots.md).

A snapshot is a full copy of what a VM that is off has set up, kept in its own folder:

  <vm>/snapshots/<SNAP>/state/        every file of state/ (the state disks, the EEPROM, ...)
  <vm>/snapshots/<SNAP>/sestore.toml  identity/sestore.toml, the secure-store items, when it has one
  <vm>/snapshots/<SNAP>/snapshot.toml when it was made, the VM's firmware, serial and MAC

The identity stays the VM's: vm.toml (MAC, serial, settings), the keys and the VNC password are never
saved or put back, and a reset keeps the model key the VM has now. A snapshot is refused for a reset
when the VM's serial or firmware is no longer the one it was made with (the EEPROM in the state
carries the serial).

Each file is a reflink (FICLONE) where the filesystem has them (btrfs, XFS), else a sparse copy:
only the data is written, the holes and blocks of zeros left as holes.

Crash safety. create assembles the snapshot in snapshots/.new-* and renames it into place; delete
renames it to snapshots/.drop-* before it removes it: interrupted, either leaves the snapshot whole
or absent, and a hidden directory that the next create, reset or delete removes. reset copies the
snapshot into snapshots/.reset-*, which, once complete, is renamed snapshots/.reset: that rename is
the commit. Then, with SIGINT, SIGTERM and SIGHUP held off, state/ moves into it as old-state, its
state/ moves into the VM's place, its sestore.toml replaces identity's, and it is removed. A reset
killed after the commit (SIGKILL, power loss) is finished by the next start, wipe or snapshot
command (finish_reset): the VM is never started or wiped half-restored. Killed before it, the VM is
as it was. Every step needs the VM off, under its start lock (core.VM.acquire), so that no start
comes between."""

import errno
import fcntl
import os
import shutil
import signal
import sys
import tempfile
import time

from .. import base, tomlfile
from ..base import NAME_RE, Error, read_toml
from . import sestore

SNAPSHOTS = "snapshots"
RECORD = "snapshot.toml"
STATE = "state"
SESTORE = sestore.CONFIG
PENDING = ".reset"  # a committed reset, being finished (finish_reset)
NEW, RESET, DROP, DONE = ".new-", ".reset-", ".drop-", ".done-"
REMOVE_SESTORE = "sestore.remove"  # in PENDING: the VM ends up with no sestore.toml
OLD_STATE = "old-state"
RECORD_HEADER = "A snapshot of a Beanjar VM (beanjar snapshot show). Made by beanjar; do not edit."
RECORD_ORDER = ("created", "firmware", "serial", "mac", "sestore", "copy")

FICLONE = 0x40049409  # linux/fs.h _IOW(0x94, 9, int)
CHUNK = 1 << 20
ZEROS = bytes(CHUNK)
HELD = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


# -- copying --
def reflink(src_fd, dst_fd):
    """dst_fd made a reflink of src_fd (FICLONE): True, or False where the filesystem (or the
    system) has none, the two are on different filesystems, or the file is one it cannot share."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        fcntl.ioctl(dst_fd, FICLONE, src_fd)
        return True
    except OSError as e:
        if e.errno in (
            errno.EOPNOTSUPP,
            errno.ENOTTY,
            errno.EXDEV,
            errno.EINVAL,
            errno.ENOSYS,
            errno.EBADF,
            errno.EPERM,
        ):
            return False
        raise


def data_ranges(fd, size):
    """(start, end) of the parts of the file fd that hold data (SEEK_DATA, SEEK_HOLE); the whole
    file where the system cannot tell."""
    if not hasattr(os, "SEEK_DATA"):
        yield 0, size
        return
    pos = 0
    while pos < size:
        try:
            start = os.lseek(fd, pos, os.SEEK_DATA)
        except OSError as e:
            if e.errno == errno.ENXIO:  # only a hole is left
                return
            if e.errno == errno.EINVAL and pos == 0:  # no SEEK_DATA on this filesystem
                yield 0, size
                return
            raise
        end = min(os.lseek(fd, start, os.SEEK_HOLE), size)
        yield start, end
        pos = end


def sparse_copy(src_fd, dst_fd):
    """src_fd's bytes into the empty dst_fd, its holes and its blocks of zeros left as holes."""
    size = os.fstat(src_fd).st_size
    for start, end in data_ranges(src_fd, size):
        pos = start
        while pos < end:
            chunk = os.pread(src_fd, min(CHUNK, end - pos), pos)
            if not chunk:
                break
            if chunk != ZEROS[: len(chunk)]:
                view = memoryview(chunk)
                done = 0
                while done < len(view):
                    done += os.pwrite(dst_fd, view[done:], pos + done)
            pos += len(chunk)
    os.ftruncate(dst_fd, size)


def copy_file(src, dst):
    """src copied to dst, a new file with src's mode and times: "reflink" or "copy" (sparse_copy)."""
    st = os.stat(src)
    with open(src, "rb") as fi:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, st.st_mode & 0o7777)
        try:
            if reflink(fi.fileno(), fd):
                how = "reflink"
            else:
                sparse_copy(fi.fileno(), fd)
                how = "copy"
            os.fsync(fd)
        finally:
            os.close(fd)
    os.chmod(dst, st.st_mode & 0o7777)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    return how


def copy_dir(src, dst):
    """The regular files of the directory src (not its subdirectories) copied into dst, made
    here: {"reflink", "copy"} of how they were (copy_file)."""
    os.mkdir(dst, os.stat(src).st_mode & 0o7777)
    hows = set()
    for n in sorted(os.listdir(src)):
        p = os.path.join(src, n)
        if os.path.isfile(p) and not os.path.islink(p):
            hows.add(copy_file(p, os.path.join(dst, n)))
    fsync_dir(dst)
    return hows


def fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def usage(path):
    """(bytes allocated, bytes nominal) of the files under path."""
    used = nominal = 0
    for d, _dirs, files in os.walk(path):
        for n in files:
            try:
                st = os.lstat(os.path.join(d, n))
            except OSError:
                continue
            used += st.st_blocks * 512
            nominal += st.st_size
    return used, nominal


def copy_label(hows):
    return "+".join(h for h in ("reflink", "copy") if h in hows) or "none"


# -- the VM's snapshots --
def snapshots_dir(vm):
    return os.path.join(vm.dir, SNAPSHOTS)


def check_name(snap):
    if not NAME_RE.fullmatch(snap or ""):
        raise Error(
            f"invalid snapshot name {snap!r}: letters, digits, '.', '_' and '-', starting with a "
            "letter or digit, at most 64",
            "snapshot",
        )


def snapshot_dir(vm, snap):
    """The folder of the VM's snapshot snap; Error for an invalid name or none of that name."""
    check_name(snap)
    d = os.path.join(snapshots_dir(vm), snap)
    if not os.path.isfile(os.path.join(d, RECORD)):
        raise Error(
            f"VM {vm.name} has no snapshot {snap} (beanjar snapshot list {vm.name})", "snapshot"
        )
    return d


def names(vm):
    try:
        return sorted(
            n
            for n in os.listdir(snapshots_dir(vm))
            if NAME_RE.fullmatch(n) and os.path.isfile(os.path.join(snapshots_dir(vm), n, RECORD))
        )
    except FileNotFoundError:
        return []


def info(vm, snap):
    """A snapshot as show and list give it."""
    d = snapshot_dir(vm, snap)
    record = read_toml(os.path.join(d, RECORD), {}) or {}
    used, nominal = usage(d)
    state = os.path.join(d, STATE)
    disks = sorted(os.listdir(state)) if os.path.isdir(state) else []
    return {
        "name": snap,
        "vm": vm.name,
        "created": record.get("created"),
        "firmware": record.get("firmware"),
        "serial": record.get("serial"),
        "mac": record.get("mac"),
        "sestore": bool(record.get("sestore")),
        "copy": record.get("copy"),
        "disk_bytes": used,
        "size_bytes": nominal,
        "disks": disks,
        "dir": d,
    }


def list_snapshots(vm):
    return [info(vm, n) for n in names(vm)]


def check_off(vm, doing):
    """An Error unless the VM is off (doing: what needs it off, for the message)."""
    st = vm.state()[0]
    if st != base.OFF:
        raise Error(
            f"VM {vm.name} is {st.lower()}: {doing} needs it off; stop it first: beanjar stop "
            f"{vm.name}"
        )


def check_still_off(vm, doing):
    """check_off under the VM's own start lock (which state() would read as a start)."""
    if vm.pid() is not None or vm.unit_up():
        raise Error(
            f"VM {vm.name} was started meanwhile: {doing} needs it off; stop it first: beanjar "
            f"stop {vm.name}"
        )


class held:
    """With the VM's start lock (no start meanwhile) and SIGTERM, SIGHUP as KeyboardInterrupt (a
    copy is stopped as by Ctrl-C, and its stage removed)."""

    def __init__(self, vm):
        self.vm, self.old = vm, {}

    def __enter__(self):
        try:
            self.vm.acquire()
        except Error:
            raise Error(
                f"VM {self.vm.name} is being started, stopped or changed by another command; "
                "try again once it is done"
            ) from None
        for s in (signal.SIGTERM, signal.SIGHUP):
            try:
                self.old[s] = signal.signal(s, _interrupted)
            except ValueError:  # not the main thread
                pass
        return self

    def __exit__(self, *exc):
        for s, h in self.old.items():
            signal.signal(s, h)
        self.vm.release()


def _interrupted(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")


def clean_stale(vm):
    """The stages a killed create, reset or delete left (under the start lock, none is in use)."""
    d = snapshots_dir(vm)
    try:
        entries = os.listdir(d)
    except FileNotFoundError:
        return
    for n in entries:
        if n.startswith((NEW, RESET, DROP, DONE)):
            shutil.rmtree(os.path.join(d, n), ignore_errors=True)


def finish_reset(vm_dir, out=None):
    """A reset killed after its commit (snapshots/.reset complete) finished: the VM's state and
    sestore.toml put in place, the old state removed. True if there was one. Called under the
    VM's start lock, or for a VM that is off (start, wipe, the snapshot commands)."""
    d = os.path.join(vm_dir, SNAPSHOTS)
    pending = os.path.join(d, PENDING)
    if not os.path.isdir(pending):
        return False
    state = os.path.join(vm_dir, STATE)
    identity = base.identity_dir(vm_dir)
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, HELD)
    try:
        new_state = os.path.join(pending, STATE)
        if os.path.isdir(new_state):
            old = os.path.join(pending, OLD_STATE)
            if os.path.lexists(state):
                if os.path.lexists(old):
                    shutil.rmtree(old)
                os.rename(state, old)
            os.rename(new_state, state)
        new_sestore = os.path.join(pending, SESTORE)
        if os.path.isfile(new_sestore):
            os.makedirs(identity, 0o700, exist_ok=True)
            os.replace(new_sestore, os.path.join(identity, SESTORE))
        elif os.path.exists(os.path.join(pending, REMOVE_SESTORE)):
            try:
                os.unlink(os.path.join(identity, SESTORE))
            except FileNotFoundError:
                pass
            os.unlink(os.path.join(pending, REMOVE_SESTORE))
        fsync_dir(vm_dir)
        fsync_dir(identity)
        done = tempfile.mkdtemp(prefix=DONE, dir=d)
        os.rmdir(done)
        os.rename(pending, done)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
    shutil.rmtree(done, ignore_errors=True)
    if out:
        out(f"{os.path.basename(vm_dir)}: snapshot reset finished")
    return True


def create(lib, name, snap, out=print):
    """Snapshot snap of the VM name, which is off: its state/ and identity/sestore.toml copied into
    its snapshots/snap. Returns info() and the seconds it took."""
    vm = lib.vm(name)
    check_name(snap)
    check_off(vm, "a snapshot")
    root = snapshots_dir(vm)
    dest = os.path.join(root, snap)
    if os.path.lexists(dest):
        raise Error(
            f"VM {name} has a snapshot {snap} already (beanjar snapshot delete {name} {snap} "
            "first)",
            "snapshot",
        )
    cfg = vm.config
    t0 = time.monotonic()
    stage = None
    with held(vm):
        check_still_off(vm, "a snapshot")
        finish_reset(vm.dir, out)
        os.makedirs(root, exist_ok=True)
        clean_stale(vm)
        try:
            stage = tempfile.mkdtemp(prefix=NEW + snap + "-", dir=root)
            state = vm.state_dir
            if os.path.isdir(state):
                hows = copy_dir(state, os.path.join(stage, STATE))
            else:
                os.mkdir(os.path.join(stage, STATE))  # a VM never started: blank disks
                hows = set()
            src = os.path.join(vm.identity_dir, SESTORE)
            has_sestore = os.path.isfile(src)
            if has_sestore:
                hows.add(copy_file(src, os.path.join(stage, SESTORE)))
            record = {
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "firmware": vm.firmware_name(),
                "serial": str(cfg.get("serial") or ""),
                "mac": str(cfg.get("mac") or ""),
                "sestore": has_sestore,
                "copy": copy_label(hows),
            }
            tomlfile.write_toml(
                os.path.join(stage, RECORD), record, RECORD_HEADER, order=RECORD_ORDER
            )
            fsync_dir(stage)
            if os.path.lexists(dest):
                raise Error(f"VM {name} has a snapshot {snap} already (made meanwhile)", "snapshot")
            os.rename(stage, dest)
            stage = None
            fsync_dir(root)
        except OSError as e:
            raise Error(f"cannot make snapshot {snap} of {name}: {e.strerror or e}") from None
        except KeyboardInterrupt:
            out("cancelled: no snapshot was made")
            raise
        finally:
            if stage:
                shutil.rmtree(stage, ignore_errors=True)
    return info(vm, snap), time.monotonic() - t0


def delete(lib, name, snap):
    """The VM's snapshot snap removed (the VM may run: its state is not touched)."""
    vm = lib.vm(name)
    d = snapshot_dir(vm, snap)
    root = snapshots_dir(vm)
    try:
        drop = tempfile.mkdtemp(prefix=DROP + snap + "-", dir=root)
        os.rmdir(drop)
        os.rename(d, drop)  # the commit: from here it is no snapshot
    except OSError as e:
        raise Error(f"cannot delete snapshot {snap} of {name}: {e.strerror or e}") from None
    shutil.rmtree(drop, ignore_errors=True)
    for n in os.listdir(root):
        if n.startswith(DROP):
            shutil.rmtree(os.path.join(root, n), ignore_errors=True)
    try:
        os.rmdir(root)  # the last one
    except OSError:
        pass


def merged_sestore(snap_dir, identity):
    """What identity/sestore.toml becomes at a reset to the snapshot in snap_dir: the snapshot's,
    with the model key the VM has now (whatever item it is in). ("copy", path): the snapshot's file
    as it is; ("write", document); ("remove", None): no file; ("keep", None): the VM's as it is."""
    saved = os.path.join(snap_dir, SESTORE)
    snap_doc = sestore._document(saved) if os.path.isfile(saved) else None
    cur_doc = sestore.document(identity)

    def models(doc):
        return {k: v for k, v in ((doc or {}).get("items") or {}).items() if k in sestore.MODELS}

    now = models(cur_doc)
    if snap_doc is None:
        if cur_doc is None:
            return "keep", None
        if not now:
            return "remove", None
        return "write", {"missing": "fallback", "items": now}
    if models(snap_doc) == now:
        return "copy", saved
    items = {k: v for k, v in (snap_doc.get("items") or {}).items() if k not in sestore.MODELS}
    items.update(now)
    return "write", {"missing": snap_doc.get("missing", "fallback"), "items": items}


def reset(lib, name, snap, out=print):
    """The VM name, which is off, put back to its snapshot snap: its state/ replaced by the
    snapshot's, its identity/sestore.toml by the snapshot's with its current model key. Returns
    info() and the seconds it took."""
    from ..runtime import qemu

    vm = lib.vm(name)
    d = snapshot_dir(vm, snap)
    check_off(vm, "a reset to a snapshot")
    snap_info = info(vm, snap)
    cfg = vm.config
    serial = str(cfg.get("serial") or "")
    if snap_info["serial"] is not None and snap_info["serial"] != serial:
        raise Error(
            f"snapshot {snap} is of serial {snap_info['serial']}, and {name} is now {serial} "
            f"(identity reset, or config set serial): its state is another TV's; delete it: "
            f"beanjar snapshot delete {name} {snap}",
            "snapshot",
        )
    fw = vm.firmware_name()
    if snap_info["firmware"] is not None and snap_info["firmware"] != fw:
        raise Error(
            f"snapshot {snap} is of firmware {snap_info['firmware']}, and {name} boots {fw} now: "
            f"set the firmware back (beanjar config set {name} fw=...), or delete the snapshot: "
            f"beanjar snapshot delete {name} {snap}",
            "snapshot",
        )
    t0 = time.monotonic()
    root = snapshots_dir(vm)
    stage = None
    with held(vm):
        check_still_off(vm, "a reset to a snapshot")
        if os.path.isdir(vm.state_dir) and qemu.qemu_users(vm.state_dir):
            raise Error(f"a running QEMU uses {vm.state_dir}; stop it first")
        finish_reset(vm.dir, out)
        clean_stale(vm)
        try:
            stage = tempfile.mkdtemp(prefix=RESET, dir=root)
            copy_dir(os.path.join(d, STATE), os.path.join(stage, STATE))
            how, what = merged_sestore(d, vm.identity_dir)
            if how == "copy":
                copy_file(what, os.path.join(stage, SESTORE))
            elif how == "write":
                sestore._write(stage, what)
            elif how == "remove":
                open(os.path.join(stage, REMOVE_SESTORE), "w").close()
            fsync_dir(stage)
            os.rename(stage, os.path.join(root, PENDING))  # the commit
            stage = None
            fsync_dir(root)
        except sestore.Error as e:
            raise Error(f"cannot reset {name} to snapshot {snap}: {e}") from None
        except OSError as e:
            raise Error(
                f"cannot reset {name} to snapshot {snap}: {e.strerror or e}; {name} is unchanged"
            ) from None
        except KeyboardInterrupt:
            out(f"cancelled: {name} is unchanged")
            raise
        finally:
            if stage:
                shutil.rmtree(stage, ignore_errors=True)
        finish_reset(vm.dir)
    return snap_info, time.monotonic() - t0
