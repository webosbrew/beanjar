#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's snapshots (host/core/beanjar/vm/snapshot.py, `beanjar snapshot ...`): create, list, show,
delete and reset of a VM made by hand in a temporary library (no QEMU, no launcher); the refusals;
the reflink and sparse-copy paths; and a reset interrupted before and after its commit."""

import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
from beanjar import base
from beanjar.board import prepare as board_prepare
from beanjar.vm import clone, config, library, sestore, snapshot

FW = "05.06.10.01-HE_DTV_WT1H_AFAAABAA"
SERIAL = "123ABCD45678"
MAC = "a8:23:fe:00:00:01"
MODEL_A = "cJbhRLJFZD9VYKzaaWeU7v1BUOafcEreZkElG7PI3+k="
MODEL_B = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
BIG = 64 << 20


def slurp(path):
    with open(path, "rb") as f:
        return f.read()


def write(path, data, mode=0o644):
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, mode)


def tree(d):
    """{relative path: bytes} of the files under d."""
    return {
        os.path.relpath(os.path.join(p, n), d): slurp(os.path.join(p, n))
        for p, _, ns in os.walk(d)
        for n in ns
    }


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-snapshot-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lib_dir = os.path.join(self.tmp, "lib")
        self.vm_dir = os.path.join(self.lib_dir, "vms", "tv1")
        fw = os.path.join(self.vm_dir, "firmware")
        os.makedirs(fw)
        with open(os.path.join(fw, "manifest.toml"), "w") as f:
            f.write(f'name = "{FW}"\nchip = "h13"\nrelease = "afro"\n')
        config.write(
            os.path.join(self.vm_dir, "vm.toml"), {"mac": MAC, "serial": SERIAL, "slot": 1}
        )
        self.state = os.path.join(self.vm_dir, "state")
        os.makedirs(self.state)
        for n in ("db8", "appstore", "nvram"):
            write(os.path.join(self.state, n + ".img"), b"set up " + n.encode())
        write(os.path.join(self.state, "eeprom.img"), b"\xff" * 64 + SERIAL.encode())
        self.sparse = os.path.join(self.state, "cmn_data.img")
        with open(self.sparse, "wb") as f:  # sparse, as a state disk is: data at 0 and at 32 MiB
            f.write(b"first use done")
            f.seek(32 << 20)
            f.write(b"x" * 4096)
            f.truncate(BIG)
        self.identity = os.path.join(self.vm_dir, "identity")
        os.makedirs(self.identity, 0o700)
        sestore.set_model(self.identity, MODEL_A.encode())
        sestore.ensure_dvr_key(self.identity)
        self.env = dict(
            os.environ,
            PYTHONPATH=os.path.join(TOP, "host", "core"),
            PYTHONDONTWRITEBYTECODE="1",
            BEANJAR_LIBRARY=self.lib_dir,
            XDG_CONFIG_HOME=os.path.join(self.tmp, "config"),
        )
        self.lib = library.Library(self.lib_dir)

    def cli(self, *args, ok=True):
        p = subprocess.run(
            [sys.executable, "-m", "beanjar", *args],
            check=False,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(p.returncode == 0, ok, f"beanjar {' '.join(args)}: {p.stdout}{p.stderr}")
        return p

    def dvr(self):
        return sestore.load(self.identity)[1][sestore.DVR_KEY]

    def model(self):
        _, items = sestore.load(self.identity)
        return {k: v for k, v in items.items() if k in sestore.MODELS}

    def test_create_list_show_delete(self):
        before = tree(self.state)
        r = json.loads(self.cli("snapshot", "create", "tv1", "base", "--json").stdout)
        self.assertEqual((r["name"], r["vm"], r["firmware"]), ("base", "tv1", FW))
        self.assertEqual((r["serial"], r["mac"], r["sestore"]), (SERIAL, MAC, True))
        self.assertIn(r["copy"], ("copy", "reflink", "reflink+copy"))
        self.assertGreaterEqual(r["seconds"], 0)
        d = os.path.join(self.vm_dir, "snapshots", "base")
        self.assertEqual(tree(os.path.join(d, "state")), before)
        self.assertEqual(
            slurp(os.path.join(d, "sestore.toml")),
            slurp(os.path.join(self.identity, "sestore.toml")),
        )
        self.assertEqual(os.stat(os.path.join(d, "sestore.toml")).st_mode & 0o777, 0o600)
        copy = os.path.join(d, "state", "cmn_data.img")
        self.assertEqual(os.path.getsize(copy), BIG)
        self.assertLess(os.stat(copy).st_blocks * 512, 1 << 20, "the holes stay holes")
        self.assertEqual(r["size_bytes"], snapshot.usage(d)[1])
        with open(os.path.join(d, "snapshot.toml"), "rb") as f:
            record = tomllib.load(f)
        self.assertEqual(record["serial"], SERIAL)
        self.assertTrue(record["created"].endswith("Z"))

        self.cli("snapshot", "create", "tv1", "second")
        rows = json.loads(self.cli("snapshot", "list", "tv1", "--json").stdout)
        self.assertEqual([x["name"] for x in rows], ["base", "second"])
        text = self.cli("snapshot", "list", "tv1").stdout
        self.assertIn("SNAPSHOT", text)
        self.assertIn(FW, text)
        shown = self.cli("snapshot", "show", "tv1", "base").stdout
        self.assertIn(f"serial: {SERIAL}", shown)
        self.assertIn("cmn_data.img", shown)
        self.assertEqual(
            json.loads(self.cli("snapshot", "show", "tv1", "base", "--json").stdout)["name"], "base"
        )

        self.cli("snapshot", "delete", "tv1", "second")
        self.assertFalse(os.path.exists(os.path.join(self.vm_dir, "snapshots", "second")))
        self.cli("snapshot", "delete", "tv1", "base")
        self.assertFalse(os.path.exists(os.path.join(self.vm_dir, "snapshots")), "the last one")
        self.assertIn("no snapshots", self.cli("snapshot", "list", "tv1").stdout)
        self.assertEqual(tree(self.state), before, "the VM's own state untouched")

    def test_reset(self):
        """A reset puts back the state and the secure-store items, keeps the model key the VM has
        now and its identity, and removes what came later."""
        before = tree(self.state)
        dvr = self.dvr()
        self.cli("snapshot", "create", "tv1", "base")
        cfg = slurp(os.path.join(self.vm_dir, "vm.toml"))
        # later sessions: a changed disk, a new one, a removed one, a new DVR key and model key
        write(os.path.join(self.state, "db8.img"), b"defaultApps.home = org.example.nothome")
        write(os.path.join(self.state, "efs.img"), b"made later")
        os.unlink(os.path.join(self.state, "nvram.img"))
        sestore.drop_dvr_key(self.identity)
        sestore.ensure_dvr_key(self.identity)
        self.assertNotEqual(self.dvr(), dvr)
        sestore.set_model(self.identity, MODEL_B.encode(), sestore.MODEL + "_a")
        r = json.loads(self.cli("snapshot", "reset", "tv1", "base", "--json").stdout)
        self.assertEqual((r["name"], r["vm"], r["reset"]), ("base", "tv1", True))
        self.assertEqual(tree(self.state), before)
        self.assertLess(os.stat(self.sparse).st_blocks * 512, 1 << 20)
        self.assertEqual(self.dvr(), dvr, "the snapshot's secure-store items")
        self.assertEqual(
            self.model(), {sestore.MODEL + "_a": MODEL_B.encode()}, "today's model key"
        )
        self.assertEqual(slurp(os.path.join(self.vm_dir, "vm.toml")), cfg, "MAC, serial kept")
        self.assertEqual(
            os.stat(os.path.join(self.identity, "sestore.toml")).st_mode & 0o777, 0o600
        )
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.vm_dir, "snapshots"))), ["base"], "no stage left"
        )
        self.assertFalse(os.path.exists(os.path.join(self.vm_dir, "start.lock")))
        # the same model key: the snapshot's file as it is
        sestore.set_model(self.identity, MODEL_A.encode())
        self.cli("snapshot", "reset", "tv1", "base")
        self.assertEqual(
            slurp(os.path.join(self.identity, "sestore.toml")),
            slurp(os.path.join(self.vm_dir, "snapshots", "base", "sestore.toml")),
        )

    def test_reset_without_sestore(self):
        """A snapshot of a VM with no sestore.toml: a reset removes a later one, or keeps only the
        model key."""
        os.unlink(os.path.join(self.identity, "sestore.toml"))
        self.cli("snapshot", "create", "tv1", "bare")
        sestore.ensure_dvr_key(self.identity)
        self.cli("snapshot", "reset", "tv1", "bare")
        self.assertFalse(os.path.exists(os.path.join(self.identity, "sestore.toml")))
        sestore.ensure_dvr_key(self.identity)
        sestore.set_model(self.identity, MODEL_B.encode())
        self.cli("snapshot", "reset", "tv1", "bare")
        self.assertEqual(sestore.load(self.identity)[1], {sestore.MODEL: MODEL_B.encode()})

    def test_refusals(self):
        self.cli("snapshot", "create", "tv1", "base")
        for bad in (".hidden", "a/b", "x" * 65, "a b", ""):
            p = self.cli("snapshot", "create", "tv1", bad, ok=False)
            self.assertIn("invalid snapshot name", p.stderr)
        p = self.cli("snapshot", "create", "tv1", "base", ok=False)
        self.assertIn("snapshot delete tv1 base", p.stderr)
        for verb in ("reset", "show", "delete"):
            p = self.cli("snapshot", verb, "tv1", "nope", ok=False)
            self.assertIn("no snapshot nope", p.stderr)
        p = self.cli("snapshot", "create", "nope", "base", ok=False)
        self.assertIn("no VM named nope", p.stderr)
        p = self.cli("snapshot", "tv1", "create", "base", ok=False)
        self.assertIn("beanjar snapshot create tv1 base", p.stderr)
        p = self.cli("snapshot", ok=False)
        self.assertEqual(p.returncode, 2)

        # running: create and reset refused with the step to take; delete and list allowed
        before = tree(self.state)
        with mock.patch.object(library.VM, "state", return_value=(base.RUNNING, "")):
            for f in (snapshot.create, snapshot.reset):
                with self.assertRaisesRegex(base.Error, "is running.*beanjar stop tv1"):
                    f(
                        self.lib,
                        "tv1",
                        "base" if f is snapshot.reset else "other",
                        out=lambda _: None,
                    )
            self.assertEqual(
                [x["name"] for x in snapshot.list_snapshots(self.lib.vm("tv1"))], ["base"]
            )
        self.assertEqual(tree(self.state), before)
        self.assertFalse(os.path.exists(os.path.join(self.vm_dir, "snapshots", "other")))

        # started meanwhile (its start lock taken by a live process)
        vm = self.lib.vm("tv1")
        with open(vm.lock_path, "w") as f:
            json.dump({"pid": os.getppid()}, f)
        with (
            mock.patch.object(library.VM, "state", return_value=(base.OFF, "")),
            self.assertRaisesRegex(base.Error, "another command"),
        ):
            snapshot.reset(self.lib, "tv1", "base", out=lambda _: None)
        os.unlink(vm.lock_path)

        # another serial (identity reset) or firmware
        cfg = config.read(os.path.join(self.vm_dir, "vm.toml"))
        config.write(os.path.join(self.vm_dir, "vm.toml"), dict(cfg, serial="999XXXX99999"))
        p = self.cli("snapshot", "reset", "tv1", "base", ok=False)
        self.assertIn("of serial " + SERIAL, p.stderr)
        config.write(os.path.join(self.vm_dir, "vm.toml"), cfg)
        manifest = os.path.join(self.vm_dir, "firmware", "manifest.toml")
        with open(manifest, "w") as f:
            f.write('name = "other-fw"\nchip = "h13"\nrelease = "afro"\n')
        p = self.cli("snapshot", "reset", "tv1", "base", ok=False)
        self.assertIn(f"of firmware {FW}, and tv1 boots other-fw", p.stderr)
        self.assertEqual(tree(self.state), before)

    def test_reflink_and_copy_paths(self):
        src = self.sparse
        calls = []

        def ficlone(fd, request, src_fd):
            calls.append(request)
            snapshot.sparse_copy(src_fd, fd)  # what the filesystem would share
            return 0

        dst = os.path.join(self.tmp, "reflinked")
        with (
            mock.patch.object(snapshot.fcntl, "ioctl", ficlone),
            mock.patch.object(snapshot.sys, "platform", "linux"),
        ):
            self.assertEqual(snapshot.copy_file(src, dst), "reflink")
        self.assertEqual(calls, [snapshot.FICLONE])
        self.assertEqual(slurp(dst), slurp(src))

        for code in (errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL, errno.ENOTTY):
            dst = os.path.join(self.tmp, f"copied-{code}")
            with (
                mock.patch.object(
                    snapshot.fcntl, "ioctl", side_effect=OSError(code, os.strerror(code))
                ),
                mock.patch.object(snapshot.sys, "platform", "linux"),
            ):
                self.assertEqual(snapshot.copy_file(src, dst), "copy")
            self.assertEqual(slurp(dst), slurp(src))
            self.assertLess(os.stat(dst).st_blocks * 512, 1 << 20)
            self.assertEqual(os.stat(dst).st_mtime_ns, os.stat(src).st_mtime_ns)
        with (
            mock.patch.object(snapshot.fcntl, "ioctl", side_effect=OSError(errno.EIO, "I/O error")),
            mock.patch.object(snapshot.sys, "platform", "linux"),
            self.assertRaises(OSError),
        ):
            snapshot.copy_file(src, os.path.join(self.tmp, "failed"))
        # a system without FICLONE copies
        with mock.patch.object(snapshot.sys, "platform", "darwin"):
            self.assertEqual(snapshot.copy_file(src, os.path.join(self.tmp, "mac")), "copy")

        # zeros written as data come out as holes; without SEEK_DATA, the same bytes
        dense = os.path.join(self.tmp, "dense")
        write(dense, b"\0" * (4 << 20) + b"end")
        out = os.path.join(self.tmp, "dense-copy")
        with mock.patch.object(snapshot.fcntl, "ioctl", side_effect=OSError(errno.EOPNOTSUPP, "")):
            snapshot.copy_file(dense, out)
        self.assertEqual(slurp(out), slurp(dense))
        self.assertLess(os.stat(out).st_blocks * 512, 1 << 20)
        with open(src, "rb") as f:
            self.assertEqual(list(snapshot.data_ranges(f.fileno(), 0)), [])
        with (
            open(src, "rb") as f,
            mock.patch.object(snapshot.os, "lseek", side_effect=OSError(errno.EINVAL, "")),
        ):
            ranges = list(snapshot.data_ranges(f.fileno(), BIG))
        self.assertEqual(ranges, [(0, BIG)])

        # the record says how the snapshot was made
        with mock.patch.object(snapshot, "reflink", return_value=False):
            r, _ = snapshot.create(self.lib, "tv1", "copied", out=lambda _: None)
        self.assertEqual(r["copy"], "copy")

    def test_interrupted_before_commit(self):
        """Killed while it copies (SIGTERM, Ctrl-C), a reset leaves the VM as it was, and no
        stage or start lock."""
        self.cli("snapshot", "create", "tv1", "base")
        write(os.path.join(self.state, "db8.img"), b"later")
        later, sestore_now = tree(self.state), slurp(os.path.join(self.identity, "sestore.toml"))
        real = snapshot.copy_file
        calls = []

        def interrupted(src, dst):
            calls.append(src)
            if len(calls) == 3:
                raise KeyboardInterrupt("signal 15")
            return real(src, dst)

        lines = []
        with (
            mock.patch.object(snapshot, "copy_file", interrupted),
            self.assertRaises(KeyboardInterrupt),
        ):
            snapshot.reset(self.lib, "tv1", "base", out=lines.append)
        self.assertIn("cancelled: tv1 is unchanged", lines)
        self.assertEqual(tree(self.state), later)
        self.assertEqual(slurp(os.path.join(self.identity, "sestore.toml")), sestore_now)
        self.assertEqual(os.listdir(os.path.join(self.vm_dir, "snapshots")), ["base"])
        self.assertFalse(os.path.exists(self.lib.vm("tv1").lock_path))

        # a create interrupted the same way leaves no snapshot
        with mock.patch.object(snapshot, "copy_file", interrupted):
            calls.clear()
            with self.assertRaises(KeyboardInterrupt):
                snapshot.create(self.lib, "tv1", "half", out=lines.append)
        self.assertEqual(os.listdir(os.path.join(self.vm_dir, "snapshots")), ["base"])

    def killed_after_commit(self):
        """A reset to base killed (SIGKILL) halfway through its swap: state/ moved aside, the
        snapshot's not yet in place. Returns what the state was at the snapshot."""
        before, dvr = tree(self.state), self.dvr()
        self.cli("snapshot", "create", "tv1", "base")
        write(os.path.join(self.state, "db8.img"), b"later")
        sestore.drop_dvr_key(self.identity)
        sestore.ensure_dvr_key(self.identity)
        real_rename = os.rename
        renames = []

        class Killed(BaseException):
            pass

        def rename(a, b):
            renames.append((a, b))
            if os.path.basename(b) == "state":  # the snapshot's state into place: killed first
                raise Killed()
            return real_rename(a, b)

        with mock.patch.object(snapshot.os, "rename", rename), self.assertRaises(Killed):
            snapshot.reset(self.lib, "tv1", "base", out=lambda _: None)
        self.lib.vm("tv1").release()  # a killed process's lock: stale, as lock_owner finds it
        pending = os.path.join(self.vm_dir, "snapshots", ".reset")
        self.assertTrue(os.path.isdir(pending))
        self.assertFalse(os.path.exists(self.state), "half-restored: no state/ at all")
        self.assertNotEqual(self.dvr(), dvr)
        return before, dvr

    def check_finished(self, before, dvr):
        self.assertEqual(tree(self.state), before)
        self.assertEqual(self.dvr(), dvr)
        self.assertEqual(os.listdir(os.path.join(self.vm_dir, "snapshots")), ["base"])

    def test_interrupted_after_commit_finished_by_snapshot_command(self):
        before, dvr = self.killed_after_commit()
        p = self.cli("snapshot", "create", "tv1", "again")
        self.assertIn("snapshot reset finished", p.stderr)
        self.assertEqual(tree(self.state), before)
        self.assertEqual(self.dvr(), dvr)
        self.assertEqual(
            tree(os.path.join(self.vm_dir, "snapshots", "again", "state")),
            before,
            "the snapshot taken after it is of the restored state",
        )

    def test_interrupted_after_commit_finished_by_start(self):
        before, dvr = self.killed_after_commit()
        seen = []

        def launcher(argv, out, cwd=None):
            seen.append(sorted(os.listdir(self.state)))
            return 1

        with (
            mock.patch.object(library, "running_refusal", return_value=None),
            mock.patch.object(library.capacity, "admit"),
            mock.patch.object(library.capacity, "listen_conflict", return_value=None),
            mock.patch.object(library.hostprocs, "run_streamed", launcher),
            self.assertRaisesRegex(base.Error, "did not start"),
        ):
            self.lib.start("tv1", out=lambda _: None)
        self.assertEqual(seen, [sorted(before)], "the launcher saw the whole snapshot's state")
        self.check_finished(before, dvr)

    def test_interrupted_after_commit_finished_by_wipe(self):
        before, dvr = self.killed_after_commit()
        self.lib.wipe("tv1", ["db8"], out=lambda _: None)
        self.assertEqual(
            tree(self.state),
            {k: v for k, v in before.items() if k != "db8.img"},
            "the wipe is of the restored state",
        )
        self.assertEqual(self.dvr(), dvr)

    def test_interrupted_after_commit_finished_by_clone(self):
        before, dvr = self.killed_after_commit()
        lines = []
        with mock.patch.object(
            board_prepare, "prepare_board_data"
        ):  # the fixture's firmware is not real
            clone.clone(self.lib, "tv1", "tv2", out=lines.append, warn=lines.append, pinned=True)
        self.assertIn("tv1: snapshot reset finished", lines)
        self.check_finished(before, dvr)
        copy = os.path.join(self.lib_dir, "vms", "tv2", "state")
        self.assertEqual(
            {k: v for k, v in tree(copy).items() if k != "eeprom.img"},
            {k: v for k, v in before.items() if k != "eeprom.img"},
            "the clone has the snapshot's state",
        )
        self.assertFalse(os.path.exists(os.path.join(self.vm_dir, "snapshots", ".reset")))

    def test_finish_reset_is_idempotent(self):
        """Killed again while it finishes, the next finish_reset completes it."""
        before, dvr = self.killed_after_commit()
        with (
            mock.patch.object(snapshot.os, "replace", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            snapshot.finish_reset(self.vm_dir)
        self.assertEqual(tree(self.state), before)  # the state is in, sestore.toml not yet
        self.assertTrue(snapshot.finish_reset(self.vm_dir))
        self.check_finished(before, dvr)
        self.assertFalse(snapshot.finish_reset(self.vm_dir))

    def test_vm_delete_and_rename_carry_snapshots(self):
        self.cli("snapshot", "create", "tv1", "base")
        self.cli("rename", "tv1", "tv2")
        self.assertEqual(
            [r["name"] for r in json.loads(self.cli("snapshot", "list", "tv2", "--json").stdout)],
            ["base"],
        )
        self.cli("delete", "tv2")
        self.assertFalse(os.path.exists(os.path.join(self.lib_dir, "vms", "tv2")))


if __name__ == "__main__":
    unittest.main()
