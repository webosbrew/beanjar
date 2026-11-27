#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""`beanjar io usb` (beanjar/usb.py): the QMP payloads, attach/detach/list against a fake QMP server
(usb-redir devices and disk images), the device lookup in a fake sysfs, the local relay, plug's
sequence, mkimage's steps and the CLI's remote dispatch. No VM, no USB device, no ssh."""

import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "host/core"))
from beanjar import ares, base, cli, lifecycle, remote, usb


class FakeQemu:
    """A QMP server on path with the part of QEMU's peripheral and chardev state usb.py reads.
    greet_delay holds the greeting back (another client holding QMP)."""

    def __init__(self, path, greet_delay=0.0):
        self.path, self.greet_delay = path, greet_delay
        self.chardevs = {"compat_monitor0": "unix:/qmp,server=on", "standby": "null"}
        self.devices = {"balloon": ("virtio-balloon-pci", None)}  # id: (driver, chardev or drive)
        self.nodes = {}  # node-name: {"file", "ro"}
        self.attached, self.fail_device_add = set(), False
        self.commands = []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(path)
        self.server.listen(4)
        threading.Thread(target=self.serve, daemon=True).start()

    def close(self):
        self.server.close()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            try:
                with conn, conn.makefile("rw") as f:  # one client at a time, as QEMU
                    time.sleep(self.greet_delay)
                    f.write('{"QMP": {"version": {}, "capabilities": []}}\n')
                    f.flush()
                    for line in f:
                        for r in self.answer(json.loads(line)):
                            f.write(json.dumps(r) + "\n")
                        f.flush()
            except OSError:
                pass  # the client left

    def answer(self, cmd):
        self.commands.append(cmd)
        ex, args = cmd["execute"], cmd.get("arguments", {})
        if ex == "qom-list":
            return [{"return": [{"name": "type", "type": "string"}]
                     + [{"name": k, "type": f"child<{d}>"} for k, (d, _c) in self.devices.items()]}]  # fmt: skip
        if ex == "query-chardev":
            return [{"return": [{"label": k, "filename": v, "frontend-open": False}
                                for k, v in self.chardevs.items()]}]  # fmt: skip
        if ex == "qom-get":
            dev = args["path"].rsplit("/", 1)[1]
            prop = args["property"]
            return [
                {"return": dev in self.attached if prop == "attached" else self.devices[dev][1]}
            ]
        if ex == "chardev-add":
            path = args["backend"]["data"]["addr"]["data"]["path"]
            if args["id"] in self.chardevs:
                return [{"error": {"class": "GenericError", "desc": "duplicate"}}]
            self.chardevs[args["id"]] = f"disconnected:unix:{path},server=on"
            Path(path).touch()
            return [{"return": {}}]
        if ex == "blockdev-add":
            if args["node-name"] in self.nodes:
                return [{"error": {"class": "GenericError", "desc": "duplicate node name"}}]
            self.nodes[args["node-name"]] = {
                "file": args["file"]["filename"],
                "ro": args["read-only"],
            }
            return [{"return": {}}]
        if ex == "blockdev-del":
            node = args["node-name"]
            if node not in self.nodes or any(c == node for _d, c in self.devices.values()):
                return [{"error": {"class": "GenericError", "desc": "not found or in use"}}]
            del self.nodes[node]
            return [{"return": {}}]
        if ex == "query-named-block-nodes":
            return [{"return": [{"node-name": k, **v} for k, v in self.nodes.items()]}]
        if ex == "device_add":
            link = args.get("chardev") or args.get("drive")
            if self.fail_device_add or link not in (*self.chardevs, *self.nodes):
                return [{"error": {"class": "GenericError", "desc": "no"}}]
            self.devices[args["id"]] = (args["driver"], link)
            if args["driver"] == "usb-storage":  # a stick needs no client
                self.attached.add(args["id"])
            return [{"return": {}}]
        if ex == "device_del":
            if self.devices[args["id"]][0] == "usb-storage":
                self.devices.pop(args["id"])
                return [{"return": {}}, {"event": "DEVICE_DELETED", "data": {"device": args["id"]}}]
            _d, chardev = self.devices.pop(args["id"])
            path = self.chardevs.pop(chardev).split("unix:")[1].split(",")[0]
            os.unlink(path)  # usb-redir's unrealize destroys its chardev
            return [{"return": {}}, {"event": "DEVICE_DELETED", "data": {"device": args["id"]}}]
        if ex == "chardev-remove":
            if args["id"] not in self.chardevs:
                return [{"error": {"class": "GenericError", "desc": "not found"}}]
            del self.chardevs[args["id"]]
            return [{"return": {}}]
        return [{"return": {}}]

    def connect(self, id_):
        """A usbredir client connected and the guest attached the device."""
        cid = usb.chardev_id(id_)
        self.chardevs[cid] = self.chardevs[cid].removeprefix("disconnected:")
        self.attached.add(usb.device_id(id_))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        run = os.path.join(self.tmp, "run")
        os.mkdir(run)
        self.vm = SimpleNamespace(
            name="tv",
            run_dir=run,
            qmp_sock=os.path.join(run, "qmp.sock"),
            pid=lambda: 1,
            unit_name=lambda: None,  # the setsid fallback: the record in run/
        )


class PayloadTest(unittest.TestCase):
    def test_payloads(self):
        self.assertEqual(
            usb.chardev_add("1", "/r/usb-1.sock"),
            {"execute": "chardev-add", "arguments": {"id": "usb-1-chr", "backend": {
                "type": "socket", "data": {"addr": {"type": "unix", "data": {"path": "/r/usb-1.sock"}},
                                           "server": True, "wait": False}}}},
        )  # fmt: skip
        self.assertEqual(
            usb.device_add("kbd"),
            {"execute": "device_add", "arguments": {"driver": "usb-redir", "id": "usb-kbd",
                                                    "bus": "usb-bus.0", "chardev": "usb-kbd-chr"}},
        )  # fmt: skip
        self.assertEqual(
            usb.device_del("2"), {"execute": "device_del", "arguments": {"id": "usb-2"}}
        )
        self.assertEqual(
            usb.chardev_remove("2"), {"execute": "chardev-remove", "arguments": {"id": "usb-2-chr"}}
        )

    def test_image_payloads(self):
        self.assertEqual(
            usb.blockdev_add("3", "/i/ntfs.img", True),
            {"execute": "blockdev-add", "arguments": {"driver": "raw", "node-name": "usb-3-disk",
             "read-only": True, "file": {"driver": "file", "filename": "/i/ntfs.img", "read-only": True}}},
        )  # fmt: skip
        self.assertFalse(usb.blockdev_add("3", "/i", False)["arguments"]["file"]["read-only"])
        self.assertEqual(
            usb.storage_add("3"),
            {"execute": "device_add", "arguments": {"driver": "usb-storage", "id": "usb-3",
                                                    "bus": "usb-bus.0", "drive": "usb-3-disk",
                                                    "removable": True}},
        )  # fmt: skip
        self.assertEqual(
            usb.blockdev_del("3"),
            {"execute": "blockdev-del", "arguments": {"node-name": "usb-3-disk"}},
        )

    def test_ids(self):
        for good in ("1", "kbd", "a_b-c", "x" * 16):
            self.assertEqual(usb.check_id(good), good)
        for bad in ("", "-a", "_a", "a b", "a/b", "a.b", "x" * 17, None):
            with self.subTest(bad), self.assertRaises(base.Error):
                usb.check_id(bad)
        self.assertEqual(usb.free_id(set()), "1")
        self.assertEqual(usb.free_id({"1", "2", "kbd"}), "3")

    def test_tool_argv(self):
        dev = {"bus": 3, "dev": 12}
        self.assertEqual(
            usb.tool_argv(dev, 4000),
            ["usbredirect", "--device", "3-12", "--to", "127.0.0.1:4000", "-k"],
        )
        self.assertEqual(usb.tool_argv(dev, 4000, sudo=True)[:2], ["sudo", "--"])


class QmpTest(Base):
    def qemu(self, **kw):
        q = FakeQemu(self.vm.qmp_sock, **kw)
        self.addCleanup(q.close)
        return q

    def test_attach_list_detach(self):
        q = self.qemu()
        a = usb.attach(self.vm)
        self.assertEqual(a, {"id": "1", "socket": os.path.join(self.vm.run_dir, "usb-1.sock")})
        self.assertTrue(os.path.exists(a["socket"]))
        self.assertEqual(usb.attach(self.vm)["id"], "2")
        self.assertEqual(usb.attach(self.vm, "kbd")["id"], "kbd")
        with self.assertRaisesRegex(base.Error, "already"):
            usb.attach(self.vm, "kbd")
        q.connect("2")
        got = {d["id"]: d for d in usb.devices(self.vm)}
        self.assertEqual(sorted(got), ["1", "2", "kbd"])  # not the balloon
        self.assertEqual(
            got["2"],
            {"id": "2", "kind": "redir", "socket": os.path.join(self.vm.run_dir, "usb-2.sock"),
             "connected": True, "attached": True},
        )  # fmt: skip
        self.assertEqual((got["1"]["connected"], got["1"]["attached"]), (False, False))
        self.assertEqual(got["1"]["socket"], a["socket"])
        self.assertEqual(usb.detach(self.vm, "1"), {"id": "1"})
        self.assertFalse(os.path.exists(a["socket"]))
        self.assertNotIn("usb-1-chr", q.chardevs)
        self.assertEqual([d["id"] for d in usb.devices(self.vm)], ["2", "kbd"])
        with self.assertRaisesRegex(base.Error, "no USB device 1"):
            usb.detach(self.vm, "1")
        self.assertEqual(usb.attach(self.vm)["id"], "1")  # the free number again
        # the chardev, gone with the device, is asked to go anyway and its error tolerated
        self.assertIn({"execute": "chardev-remove", "arguments": {"id": "usb-1-chr"}}, q.commands)

    def image(self, name="stick.img"):
        path = os.path.join(self.tmp, name)
        Path(path).write_bytes(b"\0" * 4096)
        return path

    def test_image_attach_list_detach(self):
        q = self.qemu()
        img = self.image()
        self.assertEqual(usb.attach(self.vm)["id"], "1")  # a usb-redir device beside it
        a = usb.attach_image(self.vm, img)
        self.assertEqual(a, {"id": "2", "image": img, "readonly": False})
        b = usb.attach_image(self.vm, self.image("ro.img"), readonly=True, id_="ro")
        self.assertTrue(b["readonly"])
        with self.assertRaisesRegex(base.Error, "already"):
            usb.attach_image(self.vm, img, id_="ro")
        got = {d["id"]: d for d in usb.devices(self.vm)}
        self.assertEqual(
            got["2"],
            {"id": "2", "kind": "storage", "image": img, "readonly": False, "attached": True},
        )
        self.assertEqual((got["ro"]["kind"], got["ro"]["readonly"]), ("storage", True))
        self.assertEqual(got["1"]["kind"], "redir")
        self.assertEqual(usb.detach(self.vm, "2"), {"id": "2"})
        self.assertNotIn("usb-2-disk", q.nodes)  # the image released
        self.assertNotIn("usb-2", q.devices)
        self.assertEqual(sorted(d["id"] for d in usb.devices(self.vm)), ["1", "ro"])
        usb.detach(self.vm, "ro")
        usb.detach(self.vm, "1")
        self.assertEqual(q.nodes, {})
        self.assertNotIn("chardev-remove", [c["execute"] for c in q.commands
                                            if c.get("arguments", {}).get("id") == "usb-2-chr"])  # fmt: skip

    def test_record(self):
        """What is attached is recorded in attach order for the restart (lifecycle.py), a detach
        takes it out; a restarting VM answers state without QMP."""
        self.qemu()
        img = self.image()
        usb.attach(self.vm)
        usb.attach_image(self.vm, img, readonly=True)
        self.assertEqual(
            lifecycle.read_usb(self.vm.run_dir),
            [
                {"id": "1", "kind": "redir", "socket": os.path.join(self.vm.run_dir, "usb-1.sock")},
                {"id": "2", "kind": "storage", "image": img, "readonly": True},
            ],
        )
        usb.detach(self.vm, "1")
        self.assertEqual([d["id"] for d in lifecycle.read_usb(self.vm.run_dir)], ["2"])
        self.assertEqual(usb.state(self.vm)["restarts"], 0)
        lifecycle.set_restarts(self.vm.run_dir, 2)
        lifecycle.mark_restarting(self.vm.run_dir, True)
        self.vm.pid = lambda: None
        self.assertEqual(usb.state(self.vm), {"restarts": 2, "restarting": True, "devices": []})
        lifecycle.mark_restarting(self.vm.run_dir, False)
        with self.assertRaisesRegex(base.Error, "not running"):
            usb.state(self.vm)

    def test_image_relative_path_is_made_absolute(self):
        self.qemu()
        self.image()
        with contextlib.chdir(self.tmp):
            self.assertEqual(
                usb.attach_image(self.vm, "stick.img")["image"], os.path.join(self.tmp, "stick.img")
            )

    def test_image_checks(self):
        q = self.qemu()
        with self.assertRaisesRegex(base.Error, "no such disk image"):
            usb.attach_image(self.vm, os.path.join(self.tmp, "missing.img"))
        with self.assertRaisesRegex(base.Error, "no such disk image"):
            usb.attach_image(self.vm, self.tmp)  # a directory
        img = self.image()
        os.chmod(img, 0o444)
        if os.access(img, os.W_OK):
            self.skipTest("running as root: every file is writable")
        with self.assertRaisesRegex(base.Error, "--readonly"):
            usb.attach_image(self.vm, img)
        self.assertEqual(usb.attach_image(self.vm, img, readonly=True)["id"], "1")
        self.assertEqual(q.nodes["usb-1-disk"]["ro"], True)
        self.vm.pid = lambda: None
        with self.assertRaisesRegex(base.Error, "not running"):
            usb.attach_image(self.vm, img, True)

    def test_storage_add_failure_removes_the_node(self):
        q = self.qemu()
        q.fail_device_add = True
        with self.assertRaisesRegex(base.Error, "device_add"):
            usb.attach_image(self.vm, self.image())
        self.assertEqual(q.nodes, {})

    def test_device_add_failure_removes_the_chardev(self):
        q = self.qemu()
        q.fail_device_add = True
        with self.assertRaisesRegex(base.Error, "device_add"):
            usb.attach(self.vm)
        self.assertEqual(sorted(q.chardevs), ["compat_monitor0", "standby"])

    def test_not_running_and_long_path(self):
        self.qemu()
        self.vm.pid = lambda: None
        for f in (lambda: usb.attach(self.vm), lambda: usb.detach(self.vm, "1"),
                  lambda: usb.devices(self.vm)):  # fmt: skip
            with self.assertRaisesRegex(base.Error, "not running"):
                f()
        self.vm.pid = lambda: 1
        self.vm.run_dir = "/" + "d" * 110
        with self.assertRaisesRegex(base.Error, "longer than"):
            usb.attach(self.vm)

    def test_busy(self):
        self.qemu(greet_delay=0.6)
        with self.assertRaisesRegex(base.Error, "busy"), usb.Qmp(self.vm.qmp_sock, wait=0.2):
            pass
        time.sleep(0.7)  # the fake's first client goes
        with usb.Qmp(self.vm.qmp_sock, wait=2) as q:  # served once the holder leaves
            self.assertEqual(usb.query(q), [])


class DeviceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sysfs = tmp.name
        self.dev("usb1", 1, 1, "1d6b", "0002")
        self.dev("1-1", 1, 4, "046D", "082d", "HD Pro Webcam C920")
        self.dev("1-1:1.0", None, None, None, None)
        self.dev("3-1", 3, 2, "1d6b", "0104", "kbd")
        self.dev("3-2", 3, 5, "1d6b", "0104")

    def dev(self, name, bus, dev, vid, pid, product=None):
        d = Path(self.sysfs, name)
        d.mkdir()
        for k, v in (("busnum", bus), ("devnum", dev), ("idVendor", vid), ("idProduct", pid),
                     ("product", product)):  # fmt: skip
            if v is not None:
                (d / k).write_text(f"{v}\n")

    def test_find(self):
        d = usb.find_device("046d:082D", self.sysfs)
        self.assertEqual(
            {k: d[k] for k in ("bus", "dev", "vid", "pid", "product")},
            {"bus": 1, "dev": 4, "vid": "046d", "pid": "082d", "product": "HD Pro Webcam C920"},
        )
        self.assertEqual(
            usb.find_device("3-5", self.sysfs)["sysfs"], os.path.join(self.sysfs, "3-2")
        )
        self.assertEqual(usb.node(d), "/dev/bus/usb/001/004")
        with self.assertRaisesRegex(base.Error, r"several.*3-2, 3-5"):
            usb.find_device("1d6b:0104", self.sysfs)
        with self.assertRaisesRegex(base.Error, "no USB device"):
            usb.find_device("dead:beef", self.sysfs)
        for bad in ("046d", "046d:82d", "1:2", "a-b", ""):
            with self.subTest(bad), self.assertRaisesRegex(base.Error, "invalid --device"):
                usb.find_device(bad, self.sysfs)

    def test_present(self):
        d = usb.find_device("3-2", self.sysfs)
        self.assertTrue(usb.present(d))
        Path(self.sysfs, "3-1", "devnum").write_text("9\n")  # replugged: another number
        self.assertFalse(usb.present(d))


class RelayTest(unittest.TestCase):
    def test_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.sock")
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(1)

            def echo():
                conn, _ = server.accept()
                with conn:
                    while data := conn.recv(100):
                        conn.sendall(data.upper())

            threading.Thread(target=echo, daemon=True).start()
            with (
                usb.Relay(path) as port,
                socket.create_connection(("127.0.0.1", port), timeout=5) as c,
            ):
                c.sendall(b"usbredir")
                self.assertEqual(c.recv(100), b"USBREDIR")
            server.close()
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=1)


class FakeProc:
    def __init__(self, exits_after=None):
        self.returncode, self.polls, self.exits_after = None, 0, exits_after
        self.terminated = False

    def poll(self):
        self.polls += 1
        if self.exits_after is not None and self.polls > self.exits_after:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated, self.returncode = True, -15

    def wait(self, timeout=None):
        return self.returncode


class FakeTarget:
    """states: what state() answers after the start's (restarts 0), in turn, the last one then on;
    an Error in it is raised (the VM stopped)."""

    def __init__(self, attach_after=1, states=()):
        self.log, self.attach_after, self.lists = [], attach_after, 0
        self.states, self.state_calls = list(states), 0

    def state(self):
        self.state_calls += 1
        if self.state_calls == 1 or not self.states:
            return {"restarts": 0, "restarting": False, "devices": []}
        st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(st, Exception):
            raise st
        return st

    def attach(self, id_):
        self.log.append(("attach", id_))
        return {"id": id_ or "1", "socket": "/run/usb-1.sock"}

    def detach(self, id_):
        self.log.append(("detach", id_))

    def devices(self):
        self.lists += 1
        return [{"id": "1", "attached": self.lists > self.attach_after, "connected": True}]

    @contextlib.contextmanager
    def way(self, path):
        self.log.append(("way", path))
        yield 4321
        self.log.append(("way closed", path))


class PlugTest(unittest.TestCase):
    DEVICE = {  # noqa: RUF012 (read only)
        "sysfs": "/x",
        "bus": 3,
        "dev": 2,
        "vid": "1d6b",
        "pid": "0104",
        "product": "kbd",
    }

    def run_plug(self, target, proc, present):
        spawned = []

        def spawn(argv, **kw):
            spawned.append(argv)
            return proc

        got = []
        with mock.patch.object(usb.shutil, "which", return_value="/usr/bin/usbredirect"), \
             mock.patch.object(usb.os, "access", return_value=True), \
             mock.patch.object(usb, "present", side_effect=present), \
             mock.patch.object(usb, "POLL", 0):  # fmt: skip
            rc = usb.plug(target, "tv", usb.Usbredirect(self.DEVICE), ready=got.append, spawn=spawn)
        return rc, spawned, got

    def test_restart_starts_the_client_again(self):
        """The client ends with the old QEMU of a guest restart: plug waits while the VM restarts,
        then starts the client again for the new QEMU, which has the device back; ready is said once,
        and the detach comes at the end."""
        procs = [FakeProc(exits_after=2), FakeProc()]
        target = FakeTarget(
            attach_after=0,
            states=[
                {"restarts": 0, "restarting": True, "devices": []},
                {"restarts": 1, "restarting": False, "devices": [{"id": "1", "attached": False}]},
            ],
        )
        spawned, got = [], []

        def spawn(argv, **kw):
            spawned.append(argv)
            return procs[len(spawned) - 1]

        with mock.patch.object(usb.shutil, "which", return_value="/usr/bin/usbredirect"), \
             mock.patch.object(usb.os, "access", return_value=True), \
             mock.patch.object(usb, "present", side_effect=[True] * 6 + [False]), \
             mock.patch.object(usb, "POLL", 0):  # fmt: skip
            rc = usb.plug(target, "tv", usb.Usbredirect(self.DEVICE), ready=got.append, spawn=spawn)
        self.assertEqual(rc, 0)
        self.assertEqual(len(spawned), 2)
        self.assertEqual(len(got), 1)
        self.assertEqual(target.log[-2:], [("way closed", "/run/usb-1.sock"), ("detach", "1")])

    def test_client_end_without_a_restart(self):
        """The client ends and the VM's restart count is the same: it ended on its own, status 1. A
        VM that stopped (state's Error) ends plug the same way."""
        for st in ({"restarts": 0, "restarting": False, "devices": []}, usb.Error("not running")):
            with self.subTest(st):
                target = FakeTarget(attach_after=0, states=[st])
                rc, spawned, _got = self.run_plug(target, FakeProc(exits_after=2), lambda d: True)
                self.assertEqual((rc, len(spawned)), (1, 1))

    def test_until_unplugged(self):
        target, proc = FakeTarget(attach_after=2), FakeProc()
        rc, spawned, got = self.run_plug(target, proc, [True, True, False])
        self.assertEqual(rc, 0)
        self.assertEqual(spawned, [usb.tool_argv(self.DEVICE, 4321)])
        self.assertEqual(
            got,
            [{"name": "tv", "id": "1", "socket": "/run/usb-1.sock", "device": "1d6b:0104",
              "bus_device": "3-2", "product": "kbd"}],
        )  # fmt: skip
        self.assertEqual(
            target.log,
            [("attach", None), ("way", "/run/usb-1.sock"), ("way closed", "/run/usb-1.sock"),
             ("detach", "1")],
        )  # fmt: skip
        self.assertTrue(proc.terminated)

    def test_tool_exits_before_the_guest_has_it(self):
        target = FakeTarget(attach_after=100)
        with self.assertRaisesRegex(base.Error, "exited"):
            self.run_plug(target, FakeProc(exits_after=1), [True] * 10)
        self.assertEqual(target.log[-1], ("detach", "1"))

    def test_tool_exits_later(self):
        target = FakeTarget()
        rc, _s, _g = self.run_plug(target, FakeProc(exits_after=3), [True] * 10)
        self.assertEqual(rc, 1)
        self.assertEqual(target.log[-1], ("detach", "1"))

    def test_interrupted(self):
        target = FakeTarget(attach_after=0)

        def present(_d):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_plug(target, FakeProc(), present)
        self.assertEqual(target.log[-1], ("detach", "1"))

    def test_node_not_writable(self):
        with mock.patch.object(usb.shutil, "which", return_value="/usr/bin/usbredirect"), \
             mock.patch.object(usb.os, "access", return_value=False):  # fmt: skip
            target = FakeTarget()
            with self.assertRaisesRegex(base.Error, "--sudo"):
                usb.plug(target, "tv", usb.Usbredirect(self.DEVICE))
            self.assertEqual(target.log, [])

    def test_camera(self):
        target, proc = FakeTarget(attach_after=1), FakeProc(exits_after=4)
        spawned, got = [], []

        def spawn(argv, **kw):
            spawned.append((argv, kw))
            return proc

        with tempfile.TemporaryDirectory() as d:
            program = os.path.join(d, "beanjar-virtcam")
            with open(program, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(program, 0o755)
            camera = usb.Camera("ball", program)
            with mock.patch.object(usb.shutil, "which", return_value="/usr/bin/gst-launch-1.0"), \
                 mock.patch.object(usb, "POLL", 0):  # fmt: skip
                rc = usb.plug(target, "tv", camera, ready=got.append, spawn=spawn)
        self.assertEqual(rc, 1)  # the camera ended on its own (the VM's socket went)
        argv, kw = spawned[0]
        self.assertEqual(
            argv, [program, "--to", "127.0.0.1:4321", "--pattern", "ball", "--stats", "10"]
        )
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(
            got,
            [{"name": "tv", "id": "1", "socket": "/run/usb-1.sock", "device": "046d:082d",
              "product": "HD Pro Webcam C920 (virtual)", "pattern": "ball"}],
        )  # fmt: skip
        self.assertEqual(target.log[-1], ("detach", "1"))

    def test_camera_checks(self):
        with self.assertRaisesRegex(base.Error, "invalid --pattern"):
            usb.Camera("plaid", "/x")
        target = FakeTarget()
        with self.assertRaisesRegex(base.Error, "no beanjar-virtcam"):
            usb.plug(target, "tv", usb.Camera("smpte", "/nonexistent/beanjar-virtcam"))
        self.assertEqual(target.log, [])


class ForwardTest(unittest.TestCase):
    def test_unix_socket_forward(self):
        calls = []

        def control(uri, op, *args):
            calls.append((op, *args))
            return 0, ""

        with (
            mock.patch.object(remote, "control", control),
            mock.patch.object(remote, "ensure_master"),
            ares.Forward("pi", "ssh://pi", "/lib/vms/tv/run/usb-1.sock", None) as port,
        ):
            spec = f"127.0.0.1:{port}:/lib/vms/tv/run/usb-1.sock"
            self.assertEqual(calls, [("forward", "-L", spec)])
        self.assertEqual(calls[-1], ("cancel", "-L", spec))


class MkimageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.argvs = []

    def run_fake(self, argv, input=None, capture_output=True, text=True):
        self.argvs.append((argv, input))
        target = argv[-1]
        if os.path.basename(argv[0]).startswith("mkfs"):
            with open(target, "r+b") as f:  # a superblock at the start, a mark at the end
                f.write(b"FS" * 256)
                f.seek(-2, os.SEEK_END)
                f.write(b"EN")
        else:  # sfdisk: the MBR's signature
            with open(target, "r+b") as f:
                f.seek(510)
                f.write(b"\x55\xaa")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_size(self):
        self.assertEqual(usb.parse_size("64M"), 64 << 20)
        self.assertEqual(usb.parse_size("1g"), 1 << 30)
        self.assertEqual(usb.parse_size("2048K"), 2 << 20)
        self.assertEqual(usb.parse_size("4MiB"), 4 << 20)
        for bad in ("", "64X", "1M", "1500K", "-4M", "M"):
            with self.subTest(bad), self.assertRaises(base.Error):
                usb.parse_size(bad)

    def test_steps(self):
        out = os.path.join(self.tmp, "ntfs.img")
        with mock.patch.object(usb, "which", side_effect=lambda t: f"/usr/sbin/{t}"):
            data = usb.make_image(out, "ntfs", 4 << 20, "DISK", run=self.run_fake)
        self.assertEqual(data, {"image": out, "fs": "ntfs", "size": 4 << 20, "label": "DISK"})
        (mkfs, _), (sfdisk, script) = self.argvs
        self.assertEqual(mkfs[:-1], ["/usr/sbin/mkfs.ntfs", "-F", "-Q", "-p", "2048", "-L", "DISK"])
        self.assertEqual(
            sfdisk[:-1], ["/usr/sbin/sfdisk", "--quiet", "--no-reread", "--no-tell-kernel"]
        )
        self.assertEqual(script, "label: dos\nstart=2048, type=7\n")
        raw = Path(out).read_bytes()
        self.assertEqual(len(raw), 4 << 20)
        self.assertEqual(raw[510:512], b"\x55\xaa")
        self.assertEqual(raw[1 << 20 : (1 << 20) + 4], b"FSFS")  # the partition from 1 MiB
        self.assertEqual(raw[-2:], b"EN")  # to the end
        self.assertEqual(os.listdir(self.tmp), ["ntfs.img"])  # no file of its own left beside it
        with self.assertRaisesRegex(base.Error, "--force"):
            usb.make_image(out, "ntfs", 4 << 20, run=self.run_fake)
        self.assertEqual(usb.FILESYSTEMS["fat32"][0], 0x0C)

    def test_failure_leaves_nothing(self):
        out = os.path.join(self.tmp, "x.img")

        def fail(argv, **_kw):
            return SimpleNamespace(returncode=1, stdout="", stderr="mkfs.vfat: too small\n")

        with mock.patch.object(usb, "which", side_effect=lambda t: t), \
             self.assertRaisesRegex(base.Error, "mkfs.vfat failed .*too small"):  # fmt: skip
            usb.make_image(out, "fat32", 4 << 20, run=fail)
        self.assertEqual(os.listdir(self.tmp), [])
        with self.assertRaisesRegex(base.Error, "invalid filesystem"):
            usb.make_image(out, "ext4")

    @unittest.skipUnless(
        all(shutil.which(t) or shutil.which(t, path="/usr/sbin:/sbin") for t in ("sfdisk", "mkfs.vfat")),
        "needs sfdisk and mkfs.vfat",
    )  # fmt: skip
    def test_real_fat32(self):
        out = os.path.join(self.tmp, "fat.img")
        usb.make_image(out, "fat32", 40 << 20, "STICK")
        raw = Path(out).read_bytes()
        self.assertEqual(raw[510:512], b"\x55\xaa")
        self.assertEqual(raw[446 + 4], 0x0C)  # the first partition's type
        self.assertEqual(int.from_bytes(raw[446 + 8 : 446 + 12], "little"), 2048)
        boot = raw[1 << 20 :]
        self.assertEqual(boot[82:90], b"FAT32   ")
        self.assertEqual(boot[71:82], b"STICK      ")


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {
            "XDG_CONFIG_HOME": os.path.join(tmp.name, "config"),
            "BEANJAR_LIBRARY": os.path.join(tmp.name, "lib"),
            "XDG_RUNTIME_DIR": tmp.name,
        }
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.calls = []
        patch = mock.patch.object(remote, "transport", self.transport)
        patch.start()
        self.addCleanup(patch.stop)
        remote.add_host("pi", "ssh://pi.example")

    def transport(self, argv, stdin=None, on_stderr=None, timeout=None):
        words = argv[argv.index("--") + 2].split()[1:]
        self.calls.append(words)
        if words[:2] == ["config", "show"]:
            return 0, b"{}", ""
        if words[:3] == ["io", "usb", "attach"]:
            return 0, json.dumps({"name": "tv", "id": "1", "socket": "/l/usb-1.sock"}).encode(), ""
        return 0, b'{"name": "tv", "devices": []}\n', ""

    def beanjar(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        out.buffer = io.BytesIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue() + out.buffer.getvalue().decode(), err.getvalue()

    def test_remote_verbs_run_on_the_host(self):
        self.assertEqual(self.beanjar("io", "usb", "attach", "pi/tv", "--id", "kbd")[0], 0)
        self.assertEqual(self.calls[-1], ["io", "usb", "attach", "local/tv", "--id", "kbd"])
        self.assertEqual(self.beanjar("io", "usb", "detach", "pi/tv", "kbd", "--json")[0], 0)
        self.assertEqual(self.calls[-1], ["io", "usb", "detach", "local/tv", "kbd", "--json"])
        self.assertEqual(self.beanjar("io", "usb", "list", "pi/tv")[0], 0)
        self.assertEqual(self.calls[-1], ["io", "usb", "list", "local/tv"])

    def test_plug_runs_here_against_the_host(self):
        dev = PlugTest.DEVICE
        with mock.patch.object(usb, "find_device", return_value=dev) as find, \
             mock.patch.object(usb, "plug", return_value=0) as plug:  # fmt: skip
            rc, _out, err = self.beanjar(
                "io", "usb", "attach", "pi/tv", "--device", "1d6b:0104", "--id", "k"
            )
        self.assertEqual(rc, 0, err)
        find.assert_called_once_with("1d6b:0104")
        target, name, client, id_ = plug.call_args.args[:4]
        self.assertIsInstance(target, usb.Remote)
        self.assertIsInstance(client, usb.Usbredirect)
        self.assertEqual(
            (target.host, target.name, name, client.device, id_, client.sudo),
            ("pi", "tv", "pi/tv", dev, "k", False),
        )
        # the target's calls: the host's usb verbs on its own VM
        self.calls.clear()
        self.assertEqual(target.attach("k")["socket"], "/l/usb-1.sock")
        target.devices()
        target.detach("k")
        self.assertEqual(
            self.calls,
            [["io", "usb", "attach", "local/tv", "--id", "k", "--json"], ["io", "usb", "list", "local/tv", "--json"],
             ["io", "usb", "detach", "local/tv", "k", "--json"]],
        )  # fmt: skip

    def test_remote_image_attach(self):
        rc, _o, err = self.beanjar(
            "io", "usb", "attach", "pi/tv", "--image", "/srv/ntfs.img", "--readonly"
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            self.calls[-1],
            ["io", "usb", "attach", "local/tv", "--image", "/srv/ntfs.img", "--readonly"],
        )
        self.calls.clear()
        rc, _o, err = self.beanjar("io", "usb", "attach", "pi/tv", "--image", "ntfs.img")
        self.assertEqual(rc, 2)
        self.assertIn("absolute", err)
        rc, _o, err = self.beanjar("io", "usb", "attach", "pi/tv", "--readonly")
        self.assertEqual(rc, 2)
        self.assertIn("--readonly goes with --image", err)
        self.assertEqual(self.calls, [])

    def test_rows(self):
        from beanjar.commands import devices

        text = devices.usb_rows(
            [{"id": "1", "kind": "redir", "socket": "/r/usb-1.sock", "connected": True,
              "attached": True},
             {"id": "2", "kind": "storage", "image": "/i/a.img", "readonly": True,
              "attached": True}]
        )  # fmt: skip
        lines = text.splitlines()
        self.assertEqual(lines[0].split(), ["ID", "KIND", "CLIENT", "GUEST", "SOURCE"])
        self.assertEqual(lines[1].split(), ["1", "redir", "connected", "attached", "/r/usb-1.sock"])
        self.assertEqual(
            lines[2].split(), ["2", "storage", "-", "attached", "/i/a.img", "(read-only)"]
        )

    def test_camera_plug_runs_here_against_the_host(self):
        with mock.patch.object(usb, "plug", return_value=0) as plug:
            rc, _out, err = self.beanjar(
                "io", "usb", "attach", "pi/tv", "--camera", "--pattern", "ball"
            )
        self.assertEqual(rc, 0, err)
        target, name, client = plug.call_args.args[:3]
        self.assertIsInstance(target, usb.Remote)
        self.assertIsInstance(client, usb.Camera)
        self.assertEqual((name, client.pattern), ("pi/tv", "ball"))

    def test_usage(self):

        for words in (["--camera", "--device", "1-2"], ["--camera", "--sudo"],
                      ["--device", "1-2", "--pattern", "ball"]):  # fmt: skip
            self.assertEqual(self.beanjar("io", "usb", "attach", "pi/tv", *words)[0], 2, words)
        self.assertEqual(self.beanjar("io", "usb", "detach", "pi/tv")[0], 2)
        with mock.patch.object(usb, "plug") as plug:
            rc, _o, err = self.beanjar("io", "usb", "attach", "pi/tv", "--device", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("invalid --device", err)
        plug.assert_not_called()


if __name__ == "__main__":
    unittest.main()
