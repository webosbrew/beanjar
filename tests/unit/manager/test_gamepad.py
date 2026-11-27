#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""`beanjar io usb gamepad` (beanjar/devices/gamepad.py): attach, press and detach against a fake QMP
server (test_usb.FakeQemu) and a stand-in for beanjar-virtpad that answers on its control socket
as the program does, the records, the device's kind in io usb list, and the command line's
parsing and remote dispatch. No VM, no build."""

import os
import signal
import stat
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "host/core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_usb
from beanjar.devices import gamepad, usb
from test_usb import FakeQemu

# beanjar-virtpad's control side: "state" and "press HOLD_MS CONTROL..." on --control, each
# line it got appended to $FAKE_PAD_LOG; FAKE_PAD_EXIT ends it at once with that status;
# FAKE_PAD_POLLED=0 answers as for a guest that does not poll the pad
FAKE = textwrap.dedent(
    """\
    #!{python}
    import os, signal, socket, sys
    args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    if os.environ.get("FAKE_PAD_EXIT"):
        print("cannot connect", file=sys.stderr)
        sys.exit(int(os.environ["FAKE_PAD_EXIT"]))
    log = open(os.environ["FAKE_PAD_LOG"], "a", buffering=1)
    log.write("argv " + " ".join(sys.argv[1:]) + "\\n")
    signal.signal(signal.SIGTERM, lambda *_: (log.write("unplugged\\n"), os._exit(0)))
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(args["--control"])
    srv.listen(4)
    polled = os.environ.get("FAKE_PAD_POLLED", "1")
    while True:
        c, _ = srv.accept()
        line = c.makefile().readline().strip()
        log.write(line + "\\n")
        words = line.split()
        if words[0] == "state":
            c.sendall(b'ok {{"connected": true, "polled": true}}\\n')
        elif words[0] == "press" and set(words[2:]) <= {{"a", "b", "lt"}}:
            n = 2 * len(words[2:]) if polled == "1" else 0
            c.sendall(f"ok {{n}} {{polled}}\\n".encode())
        else:
            c.sendall(b"error no such control\\n")
        c.close()
    """
)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie child of this test is gone too
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


class GamepadTest(test_usb.Base):
    def setUp(self):
        super().setUp()
        self.qemu = FakeQemu(self.vm.qmp_sock)
        self.addCleanup(self.qemu.close)
        prog = os.path.join(self.tmp, "beanjar-virtpad")
        Path(prog).write_text(FAKE.format(python=sys.executable))
        os.chmod(prog, os.stat(prog).st_mode | stat.S_IXUSR)
        self.log = os.path.join(self.tmp, "pad.log")
        env = {"BEANJAR_VIRTPAD": prog, "FAKE_PAD_LOG": self.log}
        for patch in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(gamepad, "_alive", alive),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.kill_pads)

    def kill_pads(self):
        for rec in gamepad.records(self.vm).values():
            os.kill(rec["pid"], signal.SIGKILL)

    def guest_takes(self, id_):
        """The pad's client connects and the guest attaches it, as soon as the device is there."""

        def run():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if usb.chardev_id(id_) in self.qemu.chardevs:
                    self.qemu.connect(id_)
                    return
                time.sleep(0.02)

        threading.Thread(target=run, daemon=True).start()

    def lines(self):
        return Path(self.log).read_text().splitlines() if os.path.exists(self.log) else []

    def test_attach_press_detach(self):
        self.guest_takes("1")
        data = gamepad.attach(self.vm, wait=5)
        self.assertEqual(
            {k: data[k] for k in ("name", "id", "device", "product")},
            {"name": "tv", "id": "1", "device": "045e:028e", "product": "Controller"},
        )
        sock = os.path.join(self.vm.run_dir, "usb-1.sock")
        self.assertEqual(data["socket"], sock)
        self.assertEqual(data["control"], os.path.join(self.vm.run_dir, "usb-1-pad.sock"))
        self.assertEqual(
            self.lines()[0],
            f"argv --unix {sock} --control {data['control']} --retry {gamepad.RETRY}",
        )
        rec = gamepad.records(self.vm)["1"]
        self.assertTrue(alive(rec["pid"]))
        self.assertEqual([d["kind"] for d in usb.devices(self.vm)], ["gamepad"])
        self.assertEqual(usb.state(self.vm)["devices"][0]["kind"], "gamepad")

        out = gamepad.press(self.vm, ["A", "b"], hold=0.05)
        self.assertEqual(
            out,
            {"name": "tv", "id": "1", "controls": ["a", "b"], "hold": 0.05, "reports": 4,
             "polled": True},
        )  # fmt: skip
        self.assertEqual(self.lines()[-1], "press 50 a b")
        with self.assertRaisesRegex(usb.Error, "no such control"):
            gamepad.press(self.vm, ["z"])
        with self.assertRaisesRegex(usb.Error, "--hold"):
            gamepad.press(self.vm, ["a"], hold=61)

        self.assertEqual(gamepad.detach(self.vm), {"name": "tv", "id": "1"})
        self.assertEqual(self.lines()[-1], "unplugged")
        self.assertFalse(alive(rec["pid"]))
        self.assertEqual(gamepad.records(self.vm), {})
        self.assertEqual(usb.devices(self.vm), [])
        self.assertFalse(os.path.exists(sock))
        with self.assertRaisesRegex(usb.Error, "has no gamepad: beanjar io usb gamepad attach tv"):
            gamepad.press(self.vm, ["a"])

    def test_usb_detach_ends_the_program(self):
        self.guest_takes("p")
        gamepad.attach(self.vm, "p", wait=5)
        pid = gamepad.records(self.vm)["p"]["pid"]
        usb.detach(self.vm, "p")
        self.assertFalse(alive(pid))
        self.assertEqual(gamepad.records(self.vm), {})

    def test_not_polled(self):
        os.environ["FAKE_PAD_POLLED"] = "0"
        self.addCleanup(os.environ.pop, "FAKE_PAD_POLLED")
        self.guest_takes("1")
        gamepad.attach(self.vm, wait=5)
        out = gamepad.press(self.vm, ["lt"])
        self.assertEqual((out["reports"], out["polled"], out["hold"]), (0, False, 0.1))

    def test_two_pads_need_an_id(self):
        for id_ in ("1", "2"):
            self.guest_takes(id_)
            gamepad.attach(self.vm, wait=5)
        with self.assertRaisesRegex(usb.Error, "gamepads 1, 2: say which with --id"):
            gamepad.press(self.vm, ["a"])
        self.assertEqual(gamepad.press(self.vm, ["a"], hold=0.01, id_="2")["id"], "2")
        with self.assertRaisesRegex(usb.Error, "no gamepad 3"):
            gamepad.detach(self.vm, "3")
        gamepad.detach(self.vm, "1")
        self.assertEqual(list(gamepad.records(self.vm)), ["2"])

    def test_program_exits(self):
        os.environ["FAKE_PAD_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_PAD_EXIT")
        with self.assertRaisesRegex(usb.Error, r"exited \(status 1\).*cannot connect"):
            gamepad.attach(self.vm, wait=5)
        self.assertEqual(usb.devices(self.vm), [])  # detached again
        self.assertEqual(gamepad.records(self.vm), {})

    def test_guest_never_takes_it(self):
        with self.assertRaisesRegex(usb.Error, "did not get the pad in 0.5 s"):
            gamepad.attach(self.vm, wait=0.5)
        self.assertEqual(usb.devices(self.vm), [])
        self.assertEqual(gamepad.records(self.vm), {})

    def test_no_program(self):
        os.environ["BEANJAR_VIRTPAD"] = os.path.join(self.tmp, "none")
        with self.assertRaisesRegex(usb.Error, "no beanjar-virtpad"):
            gamepad.attach(self.vm)
        self.assertEqual(usb.devices(self.vm), [])

    def test_stale_record_is_dropped(self):
        path = os.path.join(usb.record_dir(self.vm), "gamepad-9.json")
        Path(path).write_text('{"id": "9", "pid": 999999999, "control": "/x"}')
        self.assertEqual(gamepad.records(self.vm), {})
        self.assertFalse(os.path.exists(path))


class GamepadCliTest(unittest.TestCase):
    # test_usb.CliTest's set-up without its tests: a configured host pi, its transport faked
    setUp = test_usb.CliTest.setUp
    transport = test_usb.CliTest.transport
    beanjar = test_usb.CliTest.beanjar

    def test_remote_runs_on_the_host(self):
        rc, _o, err = self.beanjar("io", "usb", "gamepad", "attach", "pi/tv", "--id", "pad")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            self.calls[-1], ["io", "usb", "gamepad", "attach", "local/tv", "--id", "pad"]
        )
        rc, _o, err = self.beanjar(
            "io", "usb", "gamepad", "press", "pi/tv", "a", "b", "--hold", "50ms"
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            self.calls[-1],
            ["io", "usb", "gamepad", "press", "local/tv", "a", "b", "--hold", "50ms"],
        )
        rc, _o, err = self.beanjar("io", "usb", "gamepad", "detach", "pi/tv", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls[-1], ["io", "usb", "gamepad", "detach", "local/tv", "--json"])

    def test_usage(self):
        self.assertEqual(self.beanjar("io", "usb", "gamepad")[0], 2)
        self.assertEqual(self.beanjar("io", "usb", "gamepad", "press", "pi/tv")[0], 2)
        rc, _o, err = self.beanjar("io", "usb", "gamepad", "press", "pi/tv", "a", "z")
        self.assertEqual(rc, 2)
        self.assertIn("no such control: z", err)
        rc, _o, err = self.beanjar("io", "usb", "gamepad", "press", "pi/tv", "a", "--hold", "100")
        self.assertEqual(rc, 2)
        self.assertIn("invalid duration", err)
        self.assertEqual(self.calls, [])  # refused here, before the host

    def test_rows(self):
        from beanjar.cli import devices

        text = devices.usb_rows(
            [{"id": "1", "kind": "gamepad", "socket": "/r/usb-1.sock", "connected": True,
              "attached": True}]
        )  # fmt: skip
        self.assertEqual(
            text.splitlines()[1].split(), ["1", "gamepad", "connected", "attached", "/r/usb-1.sock"]
        )


if __name__ == "__main__":
    unittest.main()
