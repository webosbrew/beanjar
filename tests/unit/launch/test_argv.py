# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The QEMU command line: qemu.plan() on a fake checkout against launch-argv.json.

ArgvTest pins the QEMU command line: qemu.plan() on a fake checkout (fake_tree) must give, for each
case of launch-argv.json, the argv, the wrapper (mode=mitm's namespace, link=off's link-down), the
wrapper's environment and the notes printed. Those were recorded from the shell launchers this code
replaced (scripts/run-x86-tcg.sh, run-pi-kvm.sh and qemu-common.sh) on the same fake checkout, on a
WSL host (no /dev/dri, a /dev/dxg), which ArgvTest stands in for; a change to the command line is a
change to launch-argv.json. `host/scripts/vm-launcher argv` prints the same without building anything."""

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from beanjar import (
    hostswap,
    netns,
    keys,
    lls,
    nvm,
    targets,
    partmap,
    paths,
    qemu,
    share,
    tomlfile,
    vmsettings,
)

from launchkit import (
    fake_partmap_build,
    fake_tree,
    FW_NAME,
    H13_AFRO,
    KEY,
    net,
    store_snapshot,
    TOP,
    TRUST_PAK,
    VM,
)

HERE = os.path.dirname(os.path.abspath(__file__))


WORDS = ["foo=1", "bar"]


WRAP_ENV = ("GALLIUM_DRIVER", "NETNS_FWD", "NETNS_TAP", "NETSTUB_LOG", "NETSTUB_CA", "VM_ADDRESS")


def without_dvr_key(snapshot):
    """A secure-store snapshot minus its dvr_key item, which every webOS VM start adds (16 random
    bytes, like ssg's), after checking its length."""
    missing, items = snapshot
    items = dict(items)
    assert len(items.pop("dvr_key")) == 16
    return missing, items


def cases(top, fw):
    """(id, accel, environment) of each pinned command line."""
    bg = lambda r: {
        "CONSOLE_SOCK": f"{r}/console.sock",
        "CONSOLE_LOG": f"{r}/console.log",  # noqa: E731
        "WEBOS_LOG": f"{r}/webos.log",
        "QEMU_PIDFILE": f"{r}/qemu.pid",
        "CURSOR_SOCK": f"{r}/cursor.sock",
        "VIDEO_SOCK": f"{r}/video.sock",
        "IR_SOCK": f"{r}/ir.sock",
        "MEDIA_SOCK": f"{r}/media",
        "CAPTURE_SOCK": f"{r}/capture.sock",
        "VIEWER_QMP_SOCK": f"{r}/viewer-qmp.sock",
        "QMP_SOCK": f"{r}/qmp.sock",
        "DBUS_ADDR": f"unix:path={r}/dbus.sock",
        "NETSTUB_LOG": f"{r}/netstub.log",
        "NETNS_LOG_DIR": r,
    }
    out = []
    for display in ("none", "egl-headless", "dbus"):
        for n in ("mitm", "direct", "off"):
            out.append(
                (f"tcg-{display}-{n}", "tcg", dict(bg("/run"), DISPLAY_BACKEND=display, **net(n)))
            )
    for display in ("none", "egl-headless", "dbus"):
        for n in ("mitm", "direct", "off"):
            out.append(
                (f"kvm-{display}-{n}", "kvm", dict(bg("/run"), DISPLAY_BACKEND=display, **net(n)))
            )
    for accel in ("tcg", "kvm"):
        out.append((f"{accel}-foreground", accel, {}))
        for n in ("mitm", "direct", "off"):
            out.append(
                (
                    f"{accel}-forwards-{n}",
                    accel,
                    dict(**net(n), LAYERS="normal,gl,rootssh,devmode", SSH_PORT="7722"),
                )
            )
        # another VM: the same stock ports on its own address
        for n in ("mitm", "direct"):
            out.append(
                (
                    f"{accel}-slot2-{n}",
                    accel,
                    dict(**net(n), LAYERS="normal,gl,rootssh,devmode", SLOT="2"),
                )
            )
    out += [
        ("tcg-state-none", "tcg", dict(STATE="none", **net("direct"))),
        (
            "tcg-identity",
            "tcg",
            dict(**net("direct"), MAC="02:ab:cd:00:11:22", SERIAL="123ABCD45678"),
        ),
        ("kvm-identity-mitm", "kvm", dict(MAC="a8:23:fe:9a:bc:de", SERIAL="501RAXX00002")),
        (
            "tcg-vm-dir",
            "tcg",
            dict(**net("direct"), RUN_DIR="/vms/t1/run", IDENTITY_DIR="/vms/t1/identity"),
        ),
        ("tcg-model-key", "tcg", dict(**net("direct"), IDENTITY_DIR=os.path.join(top, "identity"))),
        (
            "tcg-library-ca",
            "tcg",
            dict(
                RUN_DIR="/lib/vms/t1/run", IDENTITY_DIR="/lib/vms/t1/identity", NETSTUB_CA="/lib/ca"
            ),
        ),
        ("tcg-bad-mac", "tcg", dict(**net("direct"), MAC="01:00:5e:00:00:01")),
        ("tcg-bad-serial", "tcg", dict(**net("direct"), SERIAL="50 1")),
        (
            "tcg-uncompressed",
            "tcg",
            dict(
                ROOTFS="uncompressed",
                UNCOMPRESSED_PAKS="rootfs fonts",
                FW_CACHE=os.path.join(fw, "cache"),
                **net("direct"),
            ),
        ),
        (
            "tcg-knobs",
            "tcg",
            dict(
                **net("direct"),
                PMLOG="stock",
                PMLOG_DEBUG="WAM  LSM*",
                TCG_TB_SIZE="512",
                TCG_THREADS="single",
                TCG_CPU="max",
                MEM="3000",
                SMP="2",
                NET_DUMP="/tmp/n.pcap",
                QEMU_EXTRA="-device virtio-gpu-pci  -s",
                LAYERS="",
                KERNEL_ARGS=" mstar_utopia.mbx_trace=1  x.y=2",
            ),
        ),
        (
            "tcg-dbus-audio-none",
            "tcg",
            dict(
                DISPLAY_BACKEND="dbus",
                AUDIO="none",
                GPU_RES="1280x720",
                **net("direct"),
                RENDERNODE="/dev/dri/renderD129",
                GALLIUM_DRIVER="llvmpipe",
            ),
        ),
        (
            "tcg-dbus-system-qemu",
            "tcg",
            dict(DISPLAY_BACKEND="dbus", QEMU="qemu-system-aarch64", **net("direct")),
        ),
        ("tcg-audio-off", "tcg", dict(AUDIO="off", **net("direct"))),
        (
            "kvm-audio-off",
            "kvm",
            dict(AUDIO="off", RENDERNODE="/dev/dri/renderD129", **net("direct")),
        ),
        ("tcg-bad-link", "tcg", dict(NET_LINK="bogus")),
        ("tcg-bad-mode", "tcg", dict(NET_MODE="off")),
        # lan with direct and LG's services mocked: nothing to mock with
        ("tcg-lan-direct-mock", "tcg", dict(NET_LINK="lan", NET_MODE="direct")),
        ("tcg-bad-audio", "tcg", dict(DISPLAY_BACKEND="egl-headless", AUDIO="dbus")),
        ("kvm-bad-display", "kvm", dict(DISPLAY_BACKEND="vnc")),
        ("tcg-no-slot", "tcg", dict(**net("direct"), SLOT="")),
        ("tcg-bad-slot", "tcg", dict(**net("direct"), SLOT="255")),
        # opt-in: off, the default, adds nothing
        ("kvm-free-page-reporting", "kvm", dict(FREE_PAGE_REPORTING="on")),
        ("tcg-free-page-reporting", "tcg", dict(**net("direct"), FREE_PAGE_REPORTING="on")),
        ("tcg-free-page-reporting-off", "tcg", dict(**net("direct"), FREE_PAGE_REPORTING="off")),
        ("tcg-bad-free-page-reporting", "tcg", dict(**net("direct"), FREE_PAGE_REPORTING="yes")),
        # the TV's debug status: webos.debugmode= for /init, nothing for the default
        ("tcg-debug-mode-release", "tcg", dict(**net("direct"), DEBUG_MODE="release")),
        ("tcg-bad-debug-mode", "tcg", dict(**net("direct"), DEBUG_MODE="quiet")),
        # firmware.updatable: on adds the scratch overlay disk and attaches partinfo snapshot=on;
        # off, the default, adds nothing; a bad value errors
        ("tcg-updatable", "tcg", dict(**net("direct"), UPDATABLE="on")),
        ("tcg-updatable-off", "tcg", dict(**net("direct"), UPDATABLE="off")),
        ("tcg-bad-updatable", "tcg", dict(**net("direct"), UPDATABLE="yes")),
        # the GPU in its own process on an ARM host with a render node ("_HOST": "arm",
        # planned()): asked for; auto takes it too; none without a
        # GPU; errors without a render node or a backend
        (
            "arm-kvm-dbus-gpu-process",
            "kvm",
            dict(bg("/run"), DISPLAY_BACKEND="dbus", GPU_DEVICE="vhost-user-gpu", _HOST="arm"),
        ),
        (
            "arm-kvm-egl-headless-gpu-process-unit",
            "kvm",
            dict(
                **net("direct"),
                DISPLAY_BACKEND="egl-headless",
                MEM="1536",
                RUNTIME_DIRECTORY="/run/user/1000/beanjar-t1",
                GPU_DEVICE="vhost-user-gpu",
                _HOST="arm",
            ),
        ),
        (
            "arm-tcg-dbus-gpu-process",
            "tcg",
            dict(**net("direct"), DISPLAY_BACKEND="dbus", GPU_DEVICE="vhost-user-gpu", _HOST="arm"),
        ),
        ("arm-kvm-auto", "kvm", dict(**net("direct"), _HOST="arm")),
        (
            "arm-kvm-virtio-gpu-gl",
            "kvm",
            dict(**net("direct"), GPU_DEVICE="virtio-gpu-gl", _HOST="arm"),
        ),
        (
            "arm-kvm-gpu-process-system-qemu",
            "kvm",
            dict(
                **net("direct"),
                QEMU="qemu-system-aarch64",
                GPU_DEVICE="vhost-user-gpu",
                _HOST="arm",
            ),
        ),
        (
            "arm-kvm-display-none",
            "kvm",
            dict(**net("direct"), DISPLAY_BACKEND="none", GPU_DEVICE="vhost-user-gpu", _HOST="arm"),
        ),
        (
            "tcg-gpu-process-surfaceless",
            "tcg",
            dict(**net("direct"), DISPLAY_BACKEND="dbus", GPU_DEVICE="vhost-user-gpu"),
        ),
        (
            "tcg-bad-gpu-device",
            "tcg",
            dict(**net("direct"), DISPLAY_BACKEND="dbus", GPU_DEVICE="yes"),
        ),
        # under unit.run (lifecycle.py): a guest restart ends QEMU, which the unit starts
        # again; its own monitor sees a restart's SHUTDOWN and a full disk's BLOCK_IO_ERROR
        (
            "kvm-dbus-lifecycle",
            "kvm",
            dict(
                bg("/run"), DISPLAY_BACKEND="dbus", LIFECYCLE_SOCK="/run/user/1000/u/lifecycle.sock"
            ),
        ),
        (
            "tcg-mitm-lifecycle",
            "tcg",
            dict(**net("mitm"), LIFECYCLE_SOCK="/run/lifecycle.sock"),
        ),
        # the inspector's limits: a count and a size
        ("tcg-bad-mitm-flows", "tcg", dict(**net("mitm"), MITM_FLOWS="many")),
        ("tcg-bad-mitm-flow-bytes", "tcg", dict(**net("mitm"), MITM_FLOW_BYTES="lots")),
    ]
    return out


def placeholders(s, top, fw, state):
    """s with the fake tree's paths as placeholders, and each layer image's key (layerimage.key: of
    the tree's modes, which the umask decides) as @KEY@."""
    s = s.replace(top, "@TOP@").replace(fw, "@FW@").replace(state, "@STATE@")
    return re.sub(r"layer-images/[0-9a-f]{64}\.sqfs", "layer-images/@KEY@.sqfs", s)


def planned(accel, env, top, fw, state):
    """What qemu.plan makes of env on the fake tree, as launch-argv.json records it."""
    env = dict(env, FW=fw)
    env.setdefault("STATE", state)
    # Fixed explicit fixture paths retain the recorded QEMU argument contract;
    # development defaults are exercised by test-development.py.
    env.setdefault("RUN_DIR", os.path.join(top, "build"))
    env.setdefault("LOG_DIR", os.path.join(top, "build"))
    env.setdefault("IDENTITY_DIR", os.path.join(top, "local"))
    env.setdefault("SLOT", "1")
    notes = []
    # the recording host: no /dev/dri render node, WSL's /dev/dxg; "_HOST": "arm" an aarch64 host
    # with a GPU's render node (the Pi's v3d), where the GPU runs in its own process
    arm = env.pop("_HOST", None) == "arm"
    node = "/dev/dri/renderD128"
    with (
        mock.patch.object(qemu, "first_rendernode", return_value=node if arm else None),
        mock.patch.object(
            qemu, "is_char", side_effect=lambda p: p == (node if arm else "/dev/dxg")
        ),
        mock.patch.object(qemu, "is_vgem", return_value=False),
        mock.patch.object(qemu, "render_driver", return_value="v3d" if arm else ""),
        mock.patch.object(qemu.platform, "machine", return_value="aarch64" if arm else "x86_64"),
        # the Pi's Cortex-A76 has AArch32 at EL0; the x86 host is not asked
        mock.patch.object(qemu, "host_aarch32_el0", return_value=True if arm else None),
    ):
        try:
            p = qemu.plan(env, WORDS, accel=accel, tree=paths.Tree(top), say=notes.append, dry=True)
        except qemu.Error:
            return {"error": True}
    ph = lambda s: placeholders(s, top, fw, state)  # noqa: E731
    return {
        "wrap": p.wrap,
        "argv": ph(shlex.join(p.argv)),
        "env": {k: ph(p.env[k]) for k in WRAP_ENV if k in p.env},
        "notes": [ph(n) for n in notes],
    }


class ArgvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beanjar-launch-")
        self.top, self.fw, self.state = fake_tree(self.tmp)
        self.plans = {}  # qemu_plan's, by id of their argv (forwards)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_argv(self):
        with open(os.path.join(HERE, "data", "launch-argv.json")) as f:
            expected = json.load(f)
        got = {}
        for name, accel, env in cases(self.top, self.fw):
            got[name] = planned(accel, env, self.top, self.fw, self.state)
            with self.subTest(name):
                self.assertEqual(got[name], expected[name])
        self.assertEqual(sorted(got), sorted(expected))
        self.assertFalse(os.path.exists(self.state), "a dry plan made the state disks")

    def test_two_vms_own_addresses(self):
        """Two VMs (slots 1 and 2) forward the same stock ports, each on its own address, under
        mode=direct and mode=mitm alike (netns.py's relay on VM_ADDRESS)."""
        layers = "normal,gl,rootssh,devmode"
        plans = [
            self.qemu_plan("tcg", **net("direct"), LAYERS=layers, SLOT=slot)[0]
            for slot in ("1", "2")
        ]
        fwds = [self.forwards(argv) for argv in plans]
        self.assertEqual(
            fwds,
            [
                [
                    f"127.0.1.{n}:{p}-:{g}"
                    for p, g in (
                        ("6622", "22"),
                        ("3000", "3000"),
                        ("3001", "3001"),
                        ("9922", "9922"),
                        ("9991", "9991"),
                    )
                ]
                for n in (1, 2)
            ],
        )
        for slot in ("1", "2"):
            with mock.patch.object(qemu, "first_rendernode", return_value=None):
                p = qemu.plan(
                    dict(FW=self.fw, STATE="none", **net("mitm"), LAYERS=layers, SLOT=slot),
                    [],
                    accel="tcg",
                    tree=paths.Tree(self.top),
                    say=lambda _: None,
                    dry=True,
                )
            self.assertEqual(
                (p.env["VM_ADDRESS"], p.env["NETNS_FWD"]),
                (f"127.0.1.{slot}", " 6622:22 3000:3000 3001:3001 9922:9922 9991:9991"),
            )

    def test_address_prefix(self):
        """ADDRESS_PREFIX (the library's addresses) moves the VM's address, so its forwards, the
        relay and the inspector, to <prefix>.<slot>; a prefix outside 127.0.0.0/8, or 127.0.0, is refused."""
        layers = "normal,gl,rootssh,devmode"
        argv, notes = self.qemu_plan(
            "tcg", **net("direct"), LAYERS=layers, SLOT="3", ADDRESS_PREFIX="127.0.2"
        )
        fwds = self.forwards(argv)
        self.assertEqual(
            fwds,
            [
                f"127.0.2.3:{p}-:{g}"
                for p, g in (
                    ("6622", "22"),
                    ("3000", "3000"),
                    ("3001", "3001"),
                    ("9922", "9922"),
                    ("9991", "9991"),
                )
            ],
        )
        self.assertIn("network: nat, direct, host address 127.0.2.3", notes)
        with mock.patch.object(qemu, "first_rendernode", return_value=None):
            p = qemu.plan(
                dict(
                    FW=self.fw,
                    STATE="none",
                    **net("mitm"),
                    LAYERS=layers,
                    SLOT="3",
                    ADDRESS_PREFIX="127.0.2",
                ),
                [],
                accel="tcg",
                tree=paths.Tree(self.top),
                say=lambda _: None,
                dry=True,
            )
        self.assertEqual(p.env["VM_ADDRESS"], "127.0.2.3")
        relay = netns.relay_pairs(
            dict(p.env, NETSTUB_WEB_HOST=p.env["VM_ADDRESS"], NETSTUB_WEB_PORT="18083"), "/r"
        )
        self.assertEqual(
            [x.split("=")[0] for x in relay],
            [f"tcp:127.0.2.3:{n}" for n in (18083, 6622, 3000, 3001, 9922, 9991)],
        )
        for bad in ("10.0.2", "127.0.0", "127.0.256", "127.0"):
            with self.subTest(bad=bad), self.assertRaisesRegex(qemu.Error, "ADDRESS_PREFIX"):
                self.qemu_plan("tcg", ADDRESS_PREFIX=bad)

    def test_listen_address(self):
        """LISTEN_ADDRESS (vm.toml's listen) moves every TV-service port (layermeta.forwards: SSAP
        3000/3001 from the webos layer, root SSH, developer mode) and nothing else: the relay of
        mode=direct and mode=mitm (netns.relay_pairs; the inspector stays on NETSTUB_WEB_HOST). The
        notes give the address this host reaches them at: the one address, or the VM's for 0.0.0.0."""
        layers = "normal,gl,rootssh,devmode"
        ports = (
            ("6622", "22"),
            ("3000", "3000"),
            ("3001", "3001"),
            ("9922", "9922"),
            ("9991", "9991"),
        )
        for listen, reach in (("0.0.0.0", "127.0.1.3"), (" 192.0.2.7", "192.0.2.7")):
            argv, notes = self.qemu_plan(
                "tcg", **net("direct"), LAYERS=layers, SLOT="3", LISTEN_ADDRESS=listen
            )
            self.assertEqual(
                self.forwards(argv),
                [f"{listen.strip()}:{p}-:{g}" for p, g in ports],
            )
            self.assertTrue(
                any(
                    n.startswith("root ssh: ssh -p 6622 ") and n.endswith(f" root@{reach}")
                    for n in notes
                ),
                notes,
            )
            self.assertIn(
                f"SSAP (second screen): wss://{reach}:3001 (ws://{reach}:3000 where the firmware lets it in)",
                notes,
            )
            self.assertIn(
                f"network: nat, direct, host address 127.0.1.3, service ports on {listen.strip()}",
                notes,
            )
        with mock.patch.object(qemu, "first_rendernode", return_value=None):
            p = qemu.plan(
                dict(
                    FW=self.fw,
                    STATE="none",
                    **net("mitm"),
                    LAYERS=layers,
                    SLOT="2",
                    LISTEN_ADDRESS="0.0.0.0",
                ),
                [],
                accel="tcg",
                tree=paths.Tree(self.top),
                say=lambda _: None,
                dry=True,
            )
        self.assertEqual((p.env["VM_ADDRESS"], p.env["LISTEN_ADDRESS"]), ("127.0.1.2", "0.0.0.0"))
        env = dict(p.env, NETSTUB_WEB_HOST="127.0.1.2", NETSTUB_WEB_PORT="18083")
        self.assertEqual(
            netns.relay_pairs(env, "/r"),
            ["tcp:127.0.1.2:18083=unix:/r/mitmweb.sock"]
            + [f"tcp:0.0.0.0:{p}=unix:/r/fwd-{p}.sock" for p, _g in ports],
        )
        del env["LISTEN_ADDRESS"]
        self.assertEqual(
            netns.relay_pairs(env, "/r")[1],
            "tcp:127.0.1.2:6622=unix:/r/fwd-6622.sock",
            "default: VM_ADDRESS",
        )
        # unset: the VM's address, as before
        argv, _ = self.qemu_plan("tcg", **net("direct"), LAYERS="normal", SLOT="4")
        self.assertEqual(
            self.forwards(argv),
            ["127.0.1.4:3000-:3000", "127.0.1.4:3001-:3001"],
        )
        for bad in ("lan", "::", "1.2.3", "300.0.0.1"):
            with self.assertRaisesRegex(qemu.Error, "LISTEN_ADDRESS: listen"):
                self.qemu_plan("tcg", LISTEN_ADDRESS=bad)

    def test_forward(self):
        """FORWARD (vm.toml's forward): the VM's own ports join the layers' forwards on the
        same path: NETNS_FWD on LISTEN_ADDRESS in mode=direct and mode=mitm, and so the
        host-side relay and link=lan's relay in the LAN namespace, HOST:GUEST as given; a note
        names them. A host port a layer has, or beanjar's own, is refused."""
        argv, notes = self.qemu_plan(
            "tcg", **net("direct"), LAYERS="normal", SLOT="4", FORWARD="8080,8081:80"
        )
        self.assertEqual(
            self.forwards(argv),
            [
                "127.0.1.4:3000-:3000",
                "127.0.1.4:3001-:3001",
                "127.0.1.4:8080-:8080",
                "127.0.1.4:8081-:80",
            ],
        )
        self.assertIn("forward: 127.0.1.4:8080, 127.0.1.4:8081 (guest 80)", notes)
        argv, _ = self.qemu_plan(
            "tcg",
            **net("direct"),
            LAYERS="normal",
            SLOT="4",
            FORWARD="8080",
            LISTEN_ADDRESS="0.0.0.0",
        )
        self.assertIn("0.0.0.0:8080-:8080", self.forwards(argv))
        with mock.patch.object(qemu, "first_rendernode", return_value=None):
            p = qemu.plan(
                dict(
                    FW=self.fw,
                    STATE="none",
                    **net("mitm"),
                    LAYERS="normal",
                    SLOT="2",
                    FORWARD="8080,8081:80",
                ),
                [],
                accel="tcg",
                tree=paths.Tree(self.top),
                say=lambda _: None,
                dry=True,
            )
        self.assertEqual(p.env["NETNS_FWD"], " 3000:3000 3001:3001 8080:8080 8081:80")
        env = dict(p.env, NETSTUB_WEB_HOST="127.0.1.2", NETSTUB_WEB_PORT="18083")
        self.assertEqual(
            netns.relay_pairs(env, "/r")[-2:],
            [
                "tcp:127.0.1.2:8080=unix:/r/fwd-8080.sock",
                "tcp:127.0.1.2:8081=unix:/r/fwd-8081.sock",
            ],
        )
        self.assertEqual(
            netns.lan_relay_pairs(env, "/r")[-2:],
            ["tcp:0.0.0.0:8080=unix:/r/fwd-8080.sock", "tcp:0.0.0.0:8081=unix:/r/fwd-8081.sock"],
        )
        for bad, why in (
            ("3000", "webos layer's"),
            ("5900", "VNC"),
            ("80", "privileged"),
            ("x", "PORT"),
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(qemu.Error, f"FORWARD: .*{why}"):
                self.qemu_plan("tcg", LAYERS="normal", FORWARD=bad)

    def test_hbchannel_forwards(self):
        """hbchannel: Homebrew Channel's Dropbear owns the guest's 22, forwarded from SSH_PORT (6622) on
        the VM's address as rootssh's is, with mode=direct and mitm; its telnet (23) is never forwarded.
        LAYERS with both rootssh and hbchannel is refused (their layer.toml), and so are hbchannel without
        devmode, an unknown layer and hbchannel without its built layer."""
        for slot in ("1", "3"):
            argv, notes = self.qemu_plan(
                "tcg", **net("direct"), LAYERS="normal,hbchannel,devmode", SLOT=slot
            )
            self.assertEqual(
                self.forwards(argv),
                [
                    f"127.0.1.{slot}:{p}-:{g}"
                    for p, g in (
                        ("6622", "22"),
                        ("3000", "3000"),
                        ("3001", "3001"),
                        ("9922", "9922"),
                        ("9991", "9991"),
                    )
                ],
            )
            self.assertNotIn(":23", " ".join(self.forwards(argv)))
            self.assertTrue(
                any(
                    n.startswith("root ssh (Homebrew Channel): ssh -p 6622 ")
                    and n.endswith(f" root@127.0.1.{slot}")
                    for n in notes
                ),
                notes,
            )
        argv, _ = self.qemu_plan(
            "tcg", **net("direct"), LAYERS="hbchannel,devmode", SSH_PORT="7722"
        )
        self.assertIn("127.0.1.1:7722-:22", self.forwards(argv))
        with mock.patch.object(qemu, "first_rendernode", return_value=None):
            p = qemu.plan(
                dict(FW=self.fw, STATE="none", **net("mitm"), LAYERS="devmode,hbchannel", SLOT="2"),
                [],
                accel="tcg",
                tree=paths.Tree(self.top),
                say=lambda _: None,
                dry=True,
            )
        self.assertEqual(
            (p.env["VM_ADDRESS"], p.env["NETNS_FWD"]),
            ("127.0.1.2", " 6622:22 3000:3000 3001:3001 9922:9922 9991:9991"),
        )
        for layers in ("normal,rootssh,hbchannel,devmode", "hbchannel,devmode,rootssh"):
            with self.assertRaisesRegex(
                qemu.Error, "LAYERS: layer hbchannel conflicts with rootssh"
            ):
                self.qemu_plan("tcg", LAYERS=layers)
        for layers in (
            "hbchannel",
            "normal,hbchannel",
        ):  # Homebrew Channel installs through developer mode
            with self.assertRaisesRegex(qemu.Error, "LAYERS: layer hbchannel requires devmode"):
                self.qemu_plan("tcg", LAYERS=layers)
        with self.assertRaisesRegex(qemu.Error, "LAYERS: unknown layer mystery"):
            self.qemu_plan("tcg", LAYERS="normal,mystery")
        shutil.rmtree(os.path.join(self.top, "build/layers/h13-afro/hbchannel"))
        with self.assertRaisesRegex(qemu.Error, "LAYERS has hbchannel but .* is missing"):
            self.qemu_plan("tcg", LAYERS="hbchannel,devmode")

    def test_secondary_display_plan(self):
        for accel in ("kvm", "tcg"):
            with self.subTest(accel=accel):
                self.check_secondary_display_plan(accel)

    def test_capture_port(self):
        """webos.capture, the port the vendor module's video60 reads the viewer through, is on every
        target whose file sets v4l2_capture (the O22 family's, mullet to spacebuns;
        Realtek's from K6LP; LM21's), and on W15M's and W16R's for de0's VT frames (viewer_capture), on no
        other."""
        for target in (
            ("o22", "ombre"),
            ("o22n", "ombre"),
            ("o22n2", "ombre"),
            ("o24", "ombre"),
            ("o22n2", "spacebuns"),
            ("o22", "ponytail"),
            ("o22", "queue"),
            ("o22", "mullet"),
            ("lm21u", "ombre"),
            ("k8hp", "ombre"),
            ("k5lp", "goldilocks2"),
            ("m16p", "dreadlocks2"),
            ("m14", "beehive"),
        ):
            with (
                self.subTest(target=target),
                mock.patch.object(paths, "firmware_target", return_value=target),
                mock.patch.object(nvm, "board_ram", return_value=2048),
                mock.patch.object(nvm, "board_graphic", return_value="1920x1080"),
                mock.patch.object(share, "resolve_layers", return_value=([], [])),
            ):
                p = qemu.plan(
                    dict(FW=self.fw, STATE="none", **net("direct"), SLOT="1", LAYERS="normal"),
                    accel="tcg",
                    tree=paths.Tree(self.top),
                    say=lambda _: None,
                    dry=True,
                )
                want = target[1] not in ("goldilocks2", "dreadlocks2", "beehive")
                self.assertEqual(targets.v4l2_capture(target), want)
                port = want or target in (("m14", "beehive"), ("m16p", "dreadlocks2"))
                self.assertEqual(targets.viewer_capture(target), port)
                self.assertEqual("name=webos.capture" in " ".join(p.argv), port)

    def test_layer_kernel_words(self):
        """The kernel words a guest layer carries (share.kernel_args: the soc layer's Qt EGLFS
        Starfish setup) go on the kernel's command line, lowest layer first, before KERNEL_ARGS."""
        with tempfile.TemporaryDirectory() as d:
            layers = []
            for name, text in (
                ("gl", "# a comment\ngl.word=1\n"),
                ("soc", "virtio_gpu.modifiers=1\n"),
            ):
                src = os.path.join(d, name)
                os.makedirs(os.path.join(src, os.path.dirname(share.KERNEL_CMDLINE)))
                with open(os.path.join(src, share.KERNEL_CMDLINE), "w") as f:
                    f.write(text)
                layers.append(share.Layer(name, src, name))
            layers.append(share.Layer("common", os.path.join(d, "common"), "common"))
            self.assertEqual(share.kernel_args(layers), ["virtio_gpu.modifiers=1", "gl.word=1"])
            with (
                mock.patch.object(paths, "firmware_target", return_value=("o22n", "ponytail")),
                mock.patch.object(nvm, "board_ram", return_value=2048),
                mock.patch.object(nvm, "board_graphic", return_value="1920x1080"),
                mock.patch.object(share, "resolve_layers", return_value=(layers, [])),
            ):
                p = qemu.plan(
                    dict(
                        FW=self.fw,
                        STATE="none",
                        **net("direct"),
                        SLOT="1",
                        LAYERS="normal",
                        KERNEL_ARGS="extra=1",
                    ),
                    accel="tcg",
                    tree=paths.Tree(self.top),
                    say=lambda _: None,
                    dry=True,
                )
            append = p.argv[p.argv.index("-append") + 1].split()
            self.assertEqual(append[-3:], ["virtio_gpu.modifiers=1", "gl.word=1", "extra=1"])

    def test_secondary_display_gpu_process(self):
        """Two outputs in the GPU's own process: auto no longer leaves them out, and the
        backend's device keeps max_outputs=2 and the second output's setup."""
        with (
            mock.patch.object(paths, "firmware_target", return_value=("o22", "queue")),
            mock.patch.object(nvm, "board_ram", return_value=2048),
            mock.patch.object(nvm, "board_graphic", return_value="1920x1080"),
            mock.patch.object(share, "resolve_layers", return_value=([], [])),
            mock.patch.object(qemu, "gpu_process", return_value=True) as decide,
            mock.patch.object(qemu, "gpu_backend", return_value="/b/vhost-user-gpu"),
        ):
            p = qemu.plan(
                dict(
                    FW=self.fw,
                    STATE="none",
                    **net("direct"),
                    SLOT="1",
                    LAYERS="normal,gl",
                    DISPLAY_BACKEND="dbus",
                ),
                accel="kvm",
                tree=paths.Tree(self.top),
                say=lambda _: None,
                dry=True,
            )
        self.assertNotIn("two_outputs", decide.call_args.kwargs)
        device = next(a for a in p.argv if a.startswith("vhost-user-gpu-pci,"))
        self.assertIn("max_outputs=2", device)
        self.assertTrue(p.display_setup)

    def check_secondary_display_plan(self, accel):
        for target, layers, backend, enabled in (
            (("k8hp", "queue"), "normal,gl", "dbus", True),
            (("k8hp", "queue"), "headless", "dbus", False),
            (("k8hp", "queue"), "normal,gl", "none", False),
            (("k8hp", "queue"), "normal,gl", "egl-headless", False),
            (("k8hp", "ponytail"), "normal,gl", "dbus", False),
            (("o22", "queue"), "normal,gl", "dbus", True),
            (("o22", "ponytail"), "normal,gl", "dbus", False),
            # spacebuns has one output, as a set (its TV-2 takes the CRTC's overlay plane)
            (("o22n2", "spacebuns"), "normal,gl", "dbus", False),
            (("o22n2", "spacebuns"), "headless", "dbus", False),
            (("o22n2", "spacebuns"), "normal,gl", "none", False),
            (("o22n2", "spacebuns"), "normal,gl", "egl-headless", False),
            (("o22n2", "ombre"), "normal,gl", "dbus", False),
            (("k8hp", "spacebuns"), "normal,gl", "dbus", False),
        ):
            with (
                self.subTest(target=target, layers=layers, backend=backend),
                mock.patch.object(paths, "firmware_target", return_value=target),
                mock.patch.object(nvm, "board_ram", return_value=2048),
                mock.patch.object(nvm, "board_graphic", return_value="1920x1080"),
                # the fake tree has h13-afro's layers only
                mock.patch.object(share, "resolve_layers", return_value=([], [])),
            ):
                p = qemu.plan(
                    dict(
                        FW=self.fw,
                        STATE="none",
                        **net("direct"),
                        SLOT="1",
                        LAYERS=layers,
                        DISPLAY_BACKEND=backend,
                    ),
                    accel=accel,
                    tree=paths.Tree(self.top),
                    say=lambda _: None,
                    dry=True,
                )
                self.assertEqual(
                    "name=webos.capture" in " ".join(p.argv), targets.viewer_capture(target)
                )
                # kernel words come from the layers (share.kernel_args), none here: not the chip's
                self.assertNotIn(
                    "virtio_gpu.modifiers=1", p.argv[p.argv.index("-append") + 1].split()
                )
                # queue's primary output is a TV connector, no other target's
                self.assertEqual(
                    "virtio_gpu.tv_connector=1" in p.argv[p.argv.index("-append") + 1].split(),
                    enabled and target[1] == "queue",
                )
                self.assertEqual(bool(p.display_setup), enabled)
                self.assertEqual("-S" in p.argv, enabled)
                self.assertEqual("max_outputs=2" in " ".join(p.argv), enabled)
                if enabled:
                    self.assertEqual(
                        p.display_setup,
                        (f"unix:path={p.run_dir}/dbus.sock", f"{p.run_dir}/qmp.sock"),
                    )
                    paused = qemu.plan(
                        dict(
                            FW=self.fw,
                            STATE="none",
                            **net("direct"),
                            SLOT="1",
                            LAYERS=layers,
                            DISPLAY_BACKEND=backend,
                            QEMU_EXTRA="-S",
                        ),
                        accel=accel,
                        tree=paths.Tree(self.top),
                        say=lambda _: None,
                        dry=True,
                    )
                    self.assertEqual(paused.display_setup, (*p.display_setup, "--keep-paused"))

    def qemu_plan(self, accel, tree=None, **env):
        env = dict(dict(FW=self.fw, STATE="none", **net("direct"), SLOT="1"), **env)
        notes = []
        with mock.patch.object(qemu, "first_rendernode", return_value=None):
            p = qemu.plan(
                env, [], accel=accel, tree=tree or paths.Tree(self.top), say=notes.append, dry=True
            )
        self.plans[id(p.argv)] = p
        return p.argv, notes

    def forwards(self, argv):
        """The host ports of the plan qemu_plan gave argv, as LISTEN:HOST-:GUEST: netns.py's
        relay (NETNS_FWD on LISTEN_ADDRESS); QEMU's own -netdev has none (no hostfwd)."""
        p = self.plans[id(argv)]
        self.assertNotIn("hostfwd", " ".join(argv))
        return [
            f"{p.env['LISTEN_ADDRESS']}:{h}-:{g}"
            for h, g in (f.split(":", 1) for f in p.env["NETNS_FWD"].split())
        ]

    def test_userland(self):
        """USERLAND=qemu-user changes -m, where the guest gets half the TV's RAM again for
        qemu-user's own (vmsettings.guest_ram_mib), and adds the swap disk in host RAM, as large
        (hostswap.argv: the memfd's -add-fd, its -drive and its virtio-blk disk), after the state
        disks; the rest is native's, and the start says so; the plan carries the interpreter
        (QEMU_ARM, else build/qemu-user/bin/qemu-arm) for the host share. An unknown value, and
        qemu-user on a refused platform, are errors; native is the default and has no swap."""
        native, native_notes = self.qemu_plan("tcg")
        arm = os.path.join(self.tmp, "qemu-arm")
        with open(arm, "w") as f:
            f.write("")
        bridged, notes = self.qemu_plan("tcg", USERLAND="qemu-user", QEMU_ARM=arm)
        m = native.index("-m") + 1
        self.assertEqual(native[m], "1280")  # the fake model's DDR
        self.assertEqual(bridged[m], "1920")
        swap = [
            "-add-fd",
            "fd=200,set=9,opaque=beanjar-swap:640M",
            "-add-fd",
            "fd=201,set=9,opaque=beanjar-swap-ro",
            "-drive",
            "if=none,id=beanjar-swap,format=raw,file=/dev/fdset/9,cache=unsafe,discard=unmap",
            "-device",
            "virtio-blk-pci,drive=beanjar-swap,serial=beanjar-swap",
        ]
        self.assertEqual(hostswap.argv(640), swap)
        s = bridged.index("-add-fd")
        self.assertEqual(bridged[s : s + 8], swap)
        last_state = max(
            i
            for i, a in enumerate(native)
            if a.startswith("if=none,id=") and ",discard=unmap,detect-zeroes=unmap" in a
        )
        self.assertEqual(s, last_state + 3)  # right after the last state disk's -device
        self.assertEqual(
            bridged[:m] + bridged[m + 1 : s] + bridged[s + 8 :], native[:m] + native[m + 1 :]
        )
        self.assertNotIn("-add-fd", native)
        self.assertNotIn(f"serial={hostswap.SERIAL}", " ".join(native))
        self.assertIn(
            "memory: 1280 MB for the TV + 640 MB RAM and 640 MB host-RAM swap for qemu-user's "
            "overhead",
            notes,
        )
        self.assertFalse([n for n in native_notes if n.startswith("memory:")])
        # an explicit MEM is the TV's too
        bridged, notes = self.qemu_plan("tcg", USERLAND="qemu-user", QEMU_ARM=arm, MEM="2048")
        self.assertEqual(bridged[m], "3072")
        self.assertEqual(hostswap.size_mib(bridged), 1024)
        self.assertIn(
            "memory: 2048 MB for the TV + 1024 MB RAM and 1024 MB host-RAM swap for qemu-user's "
            "overhead",
            notes,
        )
        self.assertEqual(self.qemu_plan("tcg", MEM="2048")[0][m], "2048")
        self.assertIsNone(hostswap.size_mib(self.qemu_plan("tcg", MEM="2048")[0]))
        env = dict(FW=self.fw, STATE="none", **net("direct"), SLOT="1")

        def plan(**more):
            with mock.patch.object(qemu, "first_rendernode", return_value=None):
                return qemu.plan(
                    dict(env, **more),
                    [],
                    accel="tcg",
                    tree=paths.Tree(self.top),
                    say=lambda _: None,
                    dry=True,
                )

        self.assertEqual((plan().userland, plan().qemu_arm), ("native", None))
        p = plan(USERLAND="qemu-user", QEMU_ARM=arm)
        self.assertEqual((p.userland, p.qemu_arm), ("qemu-user", arm))
        # dry: a missing interpreter is not looked for; a real launch refuses it
        self.assertIsNone(plan(USERLAND="qemu-user", QEMU_ARM=arm + ".missing").qemu_arm)
        with self.assertRaisesRegex(qemu.Error, "userland=x86"):
            plan(USERLAND="x86")
        with mock.patch.object(qemu.targets, "platform", return_value="NetCast"):
            with self.assertRaisesRegex(qemu.Error, "userland=qemu-user: NetCast"):
                plan(USERLAND="qemu-user", QEMU_ARM=arm)
        tree = paths.Tree(self.top)
        self.assertIsNone(tree.qemu_arm({}))
        vendored = os.path.join(self.top, "build/qemu-user/bin/qemu-arm")
        os.makedirs(os.path.dirname(vendored))
        with open(vendored, "w") as f:
            f.write("")
        self.assertEqual(tree.qemu_arm({}), vendored)
        self.assertEqual(tree.qemu_arm({"QEMU_ARM": arm}), arm)
        # the arm64 guest kernel runs 64-bit ARM programs natively: no qemu-aarch64
        self.assertIsNone(p.qemu_aarch64)
        self.assertIsNone(tree.qemu_aarch64({"QEMU_AARCH64": arm}))

    def test_userland_auto(self):
        """USERLAND=auto, the default, resolves at each start by the host and the accelerator
        (vmsettings.resolve_userland) and says so in one note: native under TCG and where KVM's
        CPU has AArch32 at EL0 (host_aarch32_el0), qemu-user where it has not. NetCast and
        SimpleSmart then run native or not at all."""
        arm = os.path.join(self.tmp, "qemu-arm")
        with open(arm, "w") as f:
            f.write("")
        env = dict(FW=self.fw, STATE="none", **net("direct"), SLOT="1", QEMU_ARM=arm)

        def plan(accel, aarch32, platform="webOS", **more):
            notes = []
            with (
                mock.patch.object(qemu, "first_rendernode", return_value=None),
                mock.patch.object(qemu, "host_aarch32_el0", return_value=aarch32),
                mock.patch.object(qemu.targets, "platform", return_value=platform),
            ):
                p = qemu.plan(
                    dict(env, **more),
                    [],
                    accel=accel,
                    tree=paths.Tree(self.top),
                    say=notes.append,
                    dry=True,
                )
            return p.userland, [n for n in notes if n.startswith("userland:")]

        self.assertEqual(
            plan("tcg", None), ("native", ["userland: native (TCG emulates AArch32 itself)"])
        )
        self.assertEqual(
            plan("kvm", True), ("native", ["userland: native (KVM on a CPU with AArch32 at EL0)"])
        )
        self.assertEqual(
            plan("kvm", False),
            ("qemu-user", ["userland: qemu-user (KVM on a CPU without AArch32 at EL0)"]),
        )
        # an explicit value is taken as it is
        self.assertEqual(plan("kvm", False, USERLAND="native")[0], "native")
        self.assertEqual(plan("tcg", None, USERLAND="qemu-user")[0], "qemu-user")
        for platform in ("NetCast", "SimpleSmart"):
            self.assertEqual(plan("kvm", True, platform)[0], "native")
            self.assertEqual(plan("tcg", None, platform)[0], "native")
            with self.assertRaisesRegex(
                qemu.Error, f"{platform} firmware needs native 32-bit execution, which this host"
            ):
                plan("kvm", False, platform)

    def test_guest_ram(self):
        """The guest's RAM (-m) is the TV's (MEM, else the model's) with half as much again for
        qemu-user's own when the userland resolves to qemu-user, whatever made it so; native
        starts get the TV's alone. vm_userland and vm_guest_ram, which the host's memory check and
        the unit's MemoryHigh use, resolve it as plan does."""
        self.assertEqual(vmsettings.guest_ram_mib(2048, "qemu-user"), 3072)
        self.assertEqual(vmsettings.guest_ram_mib(1537, "qemu-user"), 1537 + 768)
        self.assertEqual(vmsettings.guest_ram_mib(2048, "native"), 2048)
        self.assertEqual(vmsettings.guest_ram_mib(2048, None), 2048)
        self.assertEqual(
            vmsettings.memory_note(2048, "qemu-user"),
            "memory: 2048 MB for the TV + 1024 MB RAM and 1024 MB host-RAM swap for qemu-user's "
            "overhead",
        )
        self.assertIsNone(vmsettings.memory_note(2048, "native"))
        self.assertEqual(vmsettings.qemu_user_swap_mib(2048, "qemu-user"), 1024)
        self.assertEqual(vmsettings.qemu_user_swap_mib(1537, "qemu-user"), 768)
        self.assertEqual(vmsettings.qemu_user_swap_mib(2048, "native"), 0)

        def resolved(aarch32=None, **env):
            with mock.patch.object(qemu, "host_aarch32_el0", return_value=aarch32):
                env = dict(MEM="2048", **env)
                return qemu.vm_userland(env), qemu.vm_guest_ram(env), qemu.vm_host_swap(env)

        self.assertEqual(resolved(ACCEL="tcg"), ("native", 2048, 0))
        self.assertEqual(resolved(True, ACCEL="kvm"), ("native", 2048, 0))
        self.assertEqual(resolved(False, ACCEL="kvm"), ("qemu-user", 3072, 1024))
        self.assertEqual(resolved(ACCEL="kvm", GUEST_KERNEL="x86-64"), ("qemu-user", 3072, 1024))
        self.assertEqual(resolved(ACCEL="tcg", USERLAND="qemu-user"), ("qemu-user", 3072, 1024))
        self.assertEqual(resolved(True, ACCEL="kvm", USERLAND="native"), ("native", 2048, 0))
        self.assertEqual(resolved(ACCEL="tcg", USERLAND="bogus"), (None, 2048, 0))
        self.assertIsNone(qemu.vm_guest_ram({"FW": "/nonexistent/fw"}))
        self.assertEqual(qemu.vm_host_swap({"FW": "/nonexistent/fw"}), 0)

    def test_x86_64(self):
        """GUEST_KERNEL=x86-64 (experimental): q35 on an x86-64 host, under KVM when /dev/kvm
        opens, else TCG; -vga none, the 8250 console, build/x86_64's kernel and initramfs, the
        vendored qemu-system-x86_64 (or QEMU_X86_64), never one from PATH; the userland qemu-user
        with the x86-64 qemu-arm (build/qemu-user-amd64). Refused on another host, with
        userland=native, on NetCast and SimpleSmart, and without its QEMU."""
        tree = paths.Tree(self.top)
        for f in ("build/x86_64/bzImage", "build/x86_64/initramfs.cpio.gz"):
            os.makedirs(os.path.dirname(os.path.join(self.top, f)), exist_ok=True)
            with open(os.path.join(self.top, f), "w") as out:
                out.write("")
        arm = os.path.join(self.top, "build/qemu-user-amd64/bin/qemu-arm")
        os.makedirs(os.path.dirname(arm))
        with open(arm, "w") as f:
            f.write("")
        env = dict(FW=self.fw, STATE="none", **net("direct"), SLOT="1", GUEST_KERNEL="x86-64")
        kvm_device = os.path.join(self.tmp, "kvm")
        with open(kvm_device, "w") as f:
            f.write("")

        def plan(accel=None, machine="x86_64", kvm=True, platform="webOS", **more):
            notes = []
            with (
                mock.patch.object(qemu, "first_rendernode", return_value=None),
                mock.patch.object(qemu.platform, "machine", return_value=machine),
                # /dev/kvm: a file this user may open, or none
                mock.patch.object(qemu, "KVM_DEVICE", kvm_device if kvm else kvm_device + ".none"),
                mock.patch.object(qemu.targets, "platform", return_value=platform),
            ):
                p = qemu.plan(
                    dict(env, **more), [], accel=accel, tree=tree, say=notes.append, dry=True
                )
            return p, notes

        with self.assertRaisesRegex(qemu.Error, "needs qemu-system-x86_64: the vendored QEMU"):
            plan()
        fake = os.path.join(self.tmp, "qemu-system-x86_64")
        with open(fake, "w") as f:
            f.write("")
        p, notes = plan(QEMU_X86_64=fake)
        argv = p.argv
        self.assertEqual(
            argv[:12],
            [fake, "-M", "q35", "-accel", "kvm", "-cpu", "host", "-vga", "none", "-smp", "4", "-m"],
        )
        self.assertNotIn("virt", argv)
        self.assertEqual(argv[argv.index("-kernel") + 1], tree.build("x86_64", "bzImage"))
        self.assertEqual(argv[argv.index("-initrd") + 1], tree.build("x86_64", "initramfs.cpio.gz"))
        append = argv[argv.index("-append") + 1].split()
        self.assertEqual(append[0], "console=ttyS0")
        self.assertNotIn("webos.time_scale=4", append)
        self.assertEqual((p.guest_kernel, p.userland, p.qemu_arm), ("x86-64", "qemu-user", arm))
        # no qemu-aarch64 yet: said, not refused
        self.assertIsNone(p.qemu_aarch64)
        self.assertIn(
            "userland: no qemu-aarch64 (QEMU_AARCH64=PATH): 64-bit ARM programs do not run", notes
        )
        aarch64 = os.path.join(self.top, "build/qemu-user-amd64/bin/qemu-aarch64")
        with open(aarch64, "w") as f:
            f.write("")
        p2, notes = plan(QEMU_X86_64=fake)
        self.assertEqual(p2.qemu_aarch64, aarch64)
        self.assertIn("userland: 64-bit ARM programs under qemu-aarch64", notes)
        self.assertEqual(p2.argv, argv)  # the command line is the same
        local = os.path.join(self.tmp, "qemu-aarch64")
        with open(local, "w") as f:
            f.write("")
        self.assertEqual(plan(QEMU_X86_64=fake, QEMU_AARCH64=local)[0].qemu_aarch64, local)
        self.assertIn(
            "guest kernel: x86-64 (experimental): q35 under KVM, the ARM userland under qemu-arm",
            notes,
        )
        self.assertIn("userland: qemu-user (the x86-64 guest kernel has no AArch32)", notes)
        self.assertEqual(argv[12], "1920")  # the fake model's 1280 and qemu-user's 640
        self.assertIn(
            "memory: 1280 MB for the TV + 640 MB RAM and 640 MB host-RAM swap for qemu-user's overhead",
            notes,
        )
        # the vendored one, without QEMU_X86_64
        vendored = tree.vendored_qemu_x86_64()
        os.makedirs(os.path.dirname(vendored), exist_ok=True)
        shutil.copy(fake, vendored)
        os.chmod(vendored, 0o755)
        self.assertEqual(plan()[0].argv[0], vendored)
        # no /dev/kvm: TCG, -cpu max
        argv = plan(kvm=False)[0].argv
        self.assertEqual(
            argv[1:9], ["-M", "q35", "-cpu", "max", "-accel", "tcg,thread=multi", "-vga", "none"]
        )
        self.assertIn("webos.time_scale=4", argv[argv.index("-append") + 1].split())
        with self.assertRaisesRegex(qemu.Error, "guest_kernel=x86-64 needs an x86-64 host"):
            plan(machine="aarch64")
        with self.assertRaisesRegex(qemu.Error, "userland=native is refused"):
            plan(USERLAND="native")
        for platform in ("NetCast", "SimpleSmart"):
            with self.assertRaisesRegex(
                qemu.Error, f"{platform} firmware needs native 32-bit execution"
            ):
                plan(platform=platform)
        self.assertEqual(qemu.vm_accel({"GUEST_KERNEL": "x86-64"}), qemu.default_accel("x86-64"))

    def test_virgl_use_integer(self):
        """A panfrost render node gets VIRGL_USE_INTEGER=1 (integer vertex inputs), another
        driver or no GPU none, and a value the environment sets stays."""
        env = dict(FW=self.fw, STATE="none", **net("direct"), SLOT="1")

        def plan(driver, accel="kvm", **more):
            with (
                mock.patch.object(qemu, "first_rendernode", return_value="/dev/dri/renderD128"),
                mock.patch.object(qemu, "render_driver", return_value=driver),
            ):
                return qemu.plan(
                    dict(env, **more),
                    [],
                    accel=accel,
                    tree=paths.Tree(self.top),
                    say=[].append,
                    dry=True,
                ).env

        self.assertEqual(plan("panfrost").get("VIRGL_USE_INTEGER"), "1")
        self.assertEqual(plan("panfrost", "tcg", DISPLAY_BACKEND="dbus")["VIRGL_USE_INTEGER"], "1")
        self.assertNotIn("VIRGL_USE_INTEGER", plan("v3d"))
        self.assertNotIn("VIRGL_USE_INTEGER", plan("panfrost", DISPLAY_BACKEND="none"))
        self.assertEqual(plan("panfrost", VIRGL_USE_INTEGER="x")["VIRGL_USE_INTEGER"], "x")

    def test_network_link_and_mode(self):
        """NET_LINK=off ignores NET_MODE; lan with mode direct needs NET_LG_SERVICES=real and takes
        no NET_ADDRESS or FORWARD; lan with
        mode mitm is the mitm plan with the LAN's settings for netns.lan, LAN access always on;
        nat with mode direct is the same namespace without the inspector; NET_LAN_ACCESS is on or
        off (default); other values are errors."""
        self.assertEqual(
            self.qemu_plan("tcg", NET_LINK="off", NET_MODE="direct")[0],
            self.qemu_plan("tcg", NET_LINK="off")[0],
        )
        self.assertIn(
            "webos.net=off", " ".join(self.qemu_plan("tcg", NET_LINK="off", NET_MODE="direct")[0])
        )
        with self.assertRaisesRegex(qemu.Error, r"needs lg_services=real"):
            self.qemu_plan("tcg", NET_LINK="lan", NET_MODE="direct", NET_LAN_PARENT="eth0")
        for more, why in (
            (
                dict(NET_LAN_PARENT="eth0", NET_ADDRESS="dhcp"),
                "NET_ADDRESS is for NET_LINK=lan with",
            ),
            (dict(NET_LAN_PARENT="eth0", FORWARD="8080"), "FORWARD is for NET_MODE=mitm"),
            (dict(), "needs NET_LAN_PARENT"),
        ):
            with self.subTest(more=more), self.assertRaisesRegex(qemu.Error, why):
                self.qemu_plan(
                    "tcg", NET_LINK="lan", NET_MODE="direct", NET_LG_SERVICES="real", **more
                )
        with self.assertRaisesRegex(qemu.Error, "needs NET_LAN_PARENT"):
            self.qemu_plan("tcg", NET_LINK="lan", NET_MODE="mitm")
        with self.assertRaisesRegex(qemu.Error, "LISTEN_ADDRESS is for NET_LINK=nat"):
            self.qemu_plan(
                "tcg",
                NET_LINK="lan",
                NET_MODE="mitm",
                NET_LAN_PARENT="eth0",
                LISTEN_ADDRESS="0.0.0.0",
            )
        with self.assertRaisesRegex(qemu.Error, "NET_ADDRESS"):
            self.qemu_plan(
                "tcg",
                NET_LINK="lan",
                NET_MODE="mitm",
                NET_LAN_PARENT="eth0",
                NET_ADDRESS="10.0.0.5",
            )

        def plan(**env):
            with mock.patch.object(qemu, "first_rendernode", return_value=None):
                return qemu.plan(
                    dict(FW=self.fw, STATE="none", SLOT="2", LAYERS="normal,devmode", **env),
                    [],
                    accel="tcg",
                    tree=paths.Tree(self.top),
                    say=lambda _: None,
                    dry=True,
                )

        nat = plan(**net("mitm"))
        for address in ("", "dhcp", "192.168.1.50/24,192.168.1.1"):
            lan = plan(
                NET_LINK="lan",
                NET_MODE="mitm",
                NET_LAN_PARENT="eth0",
                NET_ADDRESS=address,
                MAC="02:ab:cd:00:11:22",
            )
            self.assertEqual(
                (
                    lan.wrap,
                    lan.env["NET_LINK"],
                    lan.env["NET_LAN_PARENT"],
                    lan.env["NET_ADDRESS"],
                    lan.env["NET_LAN_MAC"],
                    lan.env["VM_ADDRESS"],
                    lan.env["NETNS_FWD"],
                ),
                (
                    "netns",
                    "lan",
                    "eth0",
                    address or "dhcp",
                    "02:ab:cd:00:11:22",
                    "127.0.1.2",
                    nat.env["NETNS_FWD"],
                ),
            )
            self.assertTrue(lan.env["NET_LAN_HELPER"].endswith("network/beanjar-lan-helper"))
            self.assertEqual(lan.env["NET_LAN_ACCESS"], "on")  # on the LAN by definition
        # mode=direct: the same namespace, the same guest NIC and forwards, without the inspector
        direct = plan(**net("direct"), NET_LG_SERVICES="real")
        self.assertEqual(
            (direct.wrap, direct.env["NET_MODE"], direct.env["NETNS_FWD"], direct.ca),
            ("netns", "direct", nat.env["NETNS_FWD"], None),
        )
        self.assertEqual(
            direct.argv[direct.argv.index("-netdev") + 1], nat.argv[nat.argv.index("-netdev") + 1]
        )
        self.assertNotIn("MITM_FLOWS", direct.env)
        # LAN access: off unless NET_LAN_ACCESS=on, for both modes; a bad value is refused
        for mode in ("mitm", "direct"):
            self.assertEqual(plan(**net(mode)).env["NET_LAN_ACCESS"], "off")
            self.assertEqual(plan(**net(mode), NET_LAN_ACCESS="on").env["NET_LAN_ACCESS"], "on")
        with self.assertRaisesRegex(qemu.Error, "NET_LAN_ACCESS=yes: on or off"):
            plan(**net("direct"), NET_LAN_ACCESS="yes")
        # LG services: mock unless NET_LG_SERVICES=real, for both modes; a bad value is refused
        for mode in ("mitm", "direct"):
            self.assertEqual(plan(**net(mode)).env["NET_LG_SERVICES"], "mock")
            real = plan(**net(mode), NET_LG_SERVICES="real")
            self.assertEqual(real.env["NET_LG_SERVICES"], "real")
        # the mock's SDP answers carry certificates of the proxy's CA: the guest trusts it in
        # direct too, unless LG is real there
        self.assertTrue(plan(**net("direct")).ca.endswith("ca.pem"))
        self.assertEqual(plan(**net("direct"), NET_LG_SERVICES="real").ca, None)
        with self.assertRaisesRegex(qemu.Error, "NET_LG_SERVICES=on: mock or real"):
            plan(**net("mitm"), NET_LG_SERVICES="on")
        notes = []
        with mock.patch.object(qemu, "first_rendernode", return_value=None):
            qemu.plan(
                dict(FW=self.fw, STATE="none", SLOT="2", NET_LAN_ACCESS="on", **net("direct")),
                [],
                accel="tcg",
                tree=paths.Tree(self.top),
                say=notes.append,
                dry=True,
            )
        self.assertIn("network: nat, direct, host address 127.0.1.2, LAN access", notes)
        # lan with direct: the same guest NIC and TAP, in the LAN namespace with the macvlan's MAC;
        # no forwards, no address setting, no inspector or CA, the layers' host-address notes left out
        notes = []
        with mock.patch.object(qemu, "first_rendernode", return_value=None):
            lan_direct = qemu.plan(
                dict(
                    FW=self.fw,
                    STATE="none",
                    SLOT="2",
                    LAYERS="normal,devmode",
                    NET_LINK="lan",
                    NET_MODE="direct",
                    NET_LG_SERVICES="real",
                    NET_LAN_PARENT="eth0",
                    MAC="02:ab:cd:00:11:22",
                ),
                [],
                accel="tcg",
                tree=paths.Tree(self.top),
                say=notes.append,
                dry=True,
            )
        self.assertEqual(
            (
                lan_direct.wrap,
                lan_direct.env["NET_LINK"],
                lan_direct.env["NET_MODE"],
                lan_direct.env["NET_LAN_PARENT"],
                lan_direct.env["NET_LAN_MAC"],
                lan_direct.env["NETNS_FWD"],
                lan_direct.env["NET_LAN_ACCESS"],
                lan_direct.env["NET_LG_SERVICES"],
                lan_direct.ca,
            ),
            ("netns", "lan", "direct", "eth0", "02:ab:cd:00:11:22", "", "on", "real", None),
        )
        self.assertNotIn("NET_ADDRESS", lan_direct.env)
        self.assertNotIn("MITM_FLOWS", lan_direct.env)
        self.assertTrue(lan_direct.env["NET_LAN_HELPER"].endswith("network/beanjar-lan-helper"))
        self.assertEqual(
            lan_direct.argv[lan_direct.argv.index("-netdev") + 1],
            nat.argv[nat.argv.index("-netdev") + 1],
        )
        self.assertIn("mac=02:ab:cd:00:11:22", " ".join(lan_direct.argv))
        self.assertEqual([n for n in notes if "127.0.1.2" in n and "ssh" in n], [])
        self.assertTrue(any("the guest itself on the LAN of eth0" in n for n in notes), notes)
        # the guest's side is the mitm plan's: the same command line but its MAC
        mac = plan(NET_LINK="lan", NET_MODE="mitm", NET_LAN_PARENT="eth0")
        self.assertEqual(mac.argv, nat.argv)
        self.assertEqual(mac.env["NET_LAN_MAC"], qemu.guest_mac(mac.target, paths.Tree(self.top)))
        for env, message in (
            (dict(NET_LINK="bridge"), "NET_LINK=bridge"),
            (dict(NET_MODE="off"), "NET_MODE=off"),
        ):
            with self.subTest(env=env), self.assertRaisesRegex(qemu.Error, message):
                self.qemu_plan("tcg", **env)

    def test_vendored_qemu_preferred(self):
        """The vendored QEMU for every accel and display while it is there, else the system's from
        PATH; QEMU= always wins, and surfaceless notes only a QEMU that is not the vendored one."""
        vendored = os.path.join(self.top, "build", "qemu", "bin", "qemu-system-aarch64")
        surfaceless = "note: RENDERNODE=surfaceless needs the patched QEMU (scripts/build.sh host)"
        cases = [
            ("kvm", dict(DISPLAY_BACKEND="none")),
            ("kvm", dict(DISPLAY_BACKEND="egl-headless")),
            ("kvm", dict(DISPLAY_BACKEND="dbus")),
            ("tcg", dict(DISPLAY_BACKEND="none")),
            ("tcg", dict(DISPLAY_BACKEND="dbus", RENDERNODE="/dev/dri/renderD129")),
            ("tcg", dict(DISPLAY_BACKEND="egl-headless")),
        ]
        for accel, env in cases:
            with self.subTest(accel=accel, **env):
                argv, notes = self.qemu_plan(accel, **env)
                self.assertEqual(argv[0], vendored)
                self.assertNotIn("-L", argv, "a checkout's QEMU finds its own firmware")
                self.assertNotIn(surfaceless, notes)
                argv, notes = self.qemu_plan(accel, QEMU="/opt/q/qemu-system-aarch64", **env)
                self.assertEqual(argv[0], "/opt/q/qemu-system-aarch64")
                self.assertEqual(
                    surfaceless in notes,
                    accel == "tcg" and env["DISPLAY_BACKEND"] == "egl-headless",
                )
        os.chmod(vendored, 0o644)  # there, but not executable: not taken
        for accel, env in cases:
            with self.subTest(accel=accel, vendored=False, **env):
                argv, notes = self.qemu_plan(accel, **env)
                self.assertEqual(argv[0], "qemu-system-aarch64")
                self.assertEqual(
                    surfaceless in notes,
                    accel == "tcg" and env["DISPLAY_BACKEND"] == "egl-headless",
                )
        os.remove(vendored)
        self.assertEqual(self.qemu_plan("kvm")[0][0], "qemu-system-aarch64")

    def test_installed_vendored_qemu_takes_its_firmware(self):
        """An installed tree's vendored QEMU (beanjar-qemu) gets -L for its firmware, before the
        kernel; an explicit system QEMU does not."""
        installed = "/usr/libexec/beanjar/qemu-system-aarch64"
        real_isfile, real_access = os.path.isfile, os.access
        tree = paths.Tree(self.top)
        layout = {
            "Image": os.path.join(self.top, "build", "Image"),
            "initramfs.cpio.gz": os.path.join(self.top, "build", "initramfs.cpio.gz"),
        }
        build = tree.build
        with (
            mock.patch.object(
                paths.Tree, "installed", new_callable=mock.PropertyMock, return_value=True
            ),
            mock.patch.object(
                tree, "build", side_effect=lambda *p: layout.get("/".join(p)) or build(*p)
            ),
            mock.patch("os.path.isfile", side_effect=lambda f: f == installed or real_isfile(f)),
            mock.patch("os.access", side_effect=lambda f, m: f == installed or real_access(f, m)),
        ):
            self.assertEqual(tree.qemu({}), (installed, ["-L", "/usr/share/beanjar/qemu"]))
            self.assertEqual(
                tree.qemu({"QEMU": "qemu-system-aarch64"}), ("qemu-system-aarch64", [])
            )
            self.assertEqual(
                tree.qemu({"QEMU": installed}), (installed, ["-L", "/usr/share/beanjar/qemu"])
            )
        # whether or not this host has beanjar-qemu installed
        with (
            mock.patch.object(
                paths.Tree, "installed", new_callable=mock.PropertyMock, return_value=True
            ),
            mock.patch("os.path.isfile", side_effect=lambda f: f != installed and real_isfile(f)),
        ):
            self.assertEqual(
                tree.qemu({}), ("qemu-system-aarch64", []), "no beanjar-qemu: the system's"
            )
        for accel in ("kvm", "tcg"):
            with (
                self.subTest(accel=accel),
                mock.patch.object(
                    tree, "qemu", return_value=(installed, ["-L", "/usr/share/beanjar/qemu"])
                ),
            ):
                argv, _ = self.qemu_plan(accel, tree=tree)
                self.assertEqual(argv[0], installed)
                i = argv.index("-L")
                self.assertEqual(argv[i : i + 3], ["-L", "/usr/share/beanjar/qemu", "-kernel"])

    def test_lls_guest_runs_the_chosen_qemu(self):
        """Firmware preparation's guest (larger-page hosts) runs the same QEMU as a launch."""
        tree = paths.Tree(self.top)
        with (
            mock.patch.object(tree, "qemu", return_value=("/q/qemu", ["-L", "/q/share"])),
            mock.patch.object(lls.subprocess, "run", side_effect=OSError("stop")) as run,
        ):
            sysroot, tmp = os.path.join(self.tmp, "sysroot"), os.path.join(self.tmp, "lls", "tmp")
            os.makedirs(tmp)
            cmdline = os.path.join(self.tmp, "cmdline")
            open(cmdline, "w").close()
            with self.assertRaises(OSError):
                lls._guest_request(sysroot, cmdline, tmp, "true", tree)
        self.assertEqual(run.call_args.args[0][:3], ["/q/qemu", "-L", "/q/share"])

    def test_dry_run_makes_nothing(self):
        cache = os.path.join(self.tmp, "cache2")
        os.makedirs(cache)
        env = dict(
            FW=self.fw,
            STATE=self.state,
            ROOTFS="uncompressed",
            FW_CACHE=cache,
            **net("direct"),
            SLOT="1",
        )
        p = qemu.plan(env, [], accel="tcg", tree=paths.Tree(self.top), say=lambda _: None, dry=True)
        self.assertIn(f"file={cache}/rootfs-", " ".join(p.argv))
        self.assertEqual(os.listdir(cache), [])
        self.assertFalse(os.path.exists(self.state))
        # make_disks: sparse images of the sizes of the target's state disks (targets.py)
        qemu.make_disks(p)
        self.assertEqual(
            sorted(os.listdir(self.state)),
            sorted(n + ".img" for n, _ in targets.state_disks(H13_AFRO)),
        )
        st = os.stat(os.path.join(self.state, "appstore.img"))
        self.assertEqual((st.st_size, st.st_blocks), (2 << 30, 0))

    def test_youtube_key(self):
        """Opt-in YouTube TOML item, one line "<part1>;<part2>" under the stock
        reader's 100-byte buffer, goes in the kernel-store bundle (0600, NUL-terminated);
        no file, no entry; a malformed one stops the launch without naming its value."""
        ident = os.path.join(self.tmp, "vm", "identity")
        run = os.path.join(self.tmp, "vm", "run")
        os.makedirs(ident, mode=0o700)
        env = dict(
            FW=self.fw,
            STATE=self.state,
            **net("direct"),
            NET_LG_SERVICES="real",  # no CA in the layer (direct with the mock has one)
            SLOT="1",
            IDENTITY_DIR=ident,
            RUN_DIR=run,
        )
        layer = os.path.join(run, "share", "vm")

        def launch():
            with mock.patch.object(
                partmap, "build", fake_partmap_build(os.path.join(self.tmp, "partinfo.img"))
            ):
                p = qemu.plan(
                    dict(env), [], accel="tcg", tree=paths.Tree(self.top), say=lambda _: None
                )
            qemu.guest_layer(p)
            return p

        def provide(data):
            # Exact item bytes: optional legacy newlines are handled only by migration.
            path = Path(ident, "sestore.toml")
            path.write_text('[items]\n"C1FFB834"={hex="' + data.hex() + '",nul_terminated=true}\n')
            path.chmod(0o600)

        launch()
        self.assertNotIn("C1FFB834", store_snapshot(layer)[1])
        self.assertFalse(os.path.exists(os.path.join(layer, "etc/emulator/youtube.key")))
        value = b"scope-test;" + b"A" * 43 + b"="
        provide(value)
        launch()
        self.assertEqual(store_snapshot(layer)[1]["C1FFB834"], value + b"\0")
        self.assertFalse(os.path.exists(os.path.join(layer, "etc/emulator/youtube.key")))
        self.assertEqual(
            os.stat(os.path.join(layer, "etc/emulator/sestore.bin")).st_mode & 0o777, 0o600
        )
        for bad in (b"", b"no-separator", b"a;" + b"b" * 98, b"a;b c", b"a;b\n\n"):
            provide(bad)
            with self.assertRaises(qemu.Error) as e:
                launch()
            self.assertNotIn("scope-test", str(e.exception))

    def test_sestore_configuration(self):
        from beanjar import sestore

        """TOML is authoritative; archived legacy inputs are never runtime fallback."""
        ident = Path(self.tmp) / "configured/identity"
        ident.mkdir(parents=True, mode=0o700)
        run = Path(self.tmp) / "configured/run"
        env = dict(
            FW=self.fw,
            STATE=self.state,
            **net("direct"),
            NET_LG_SERVICES="real",
            SLOT="1",
            IDENTITY_DIR=str(ident),
            RUN_DIR=str(run),
        )
        for name in ("model-secret", "youtube-key", "ssg-key"):
            (ident / name).write_bytes(b"invalid legacy input")
            (ident / name).chmod(0o600)
        config = ident / "sestore.toml"
        config.write_text('missing="error"\n[items]\n"browser_secret"={hex="00ff"}\n')
        config.chmod(0o600)

        def launch():
            with mock.patch.object(
                partmap, "build", fake_partmap_build(os.path.join(self.tmp, "partinfo.img"))
            ):
                p = qemu.plan(
                    dict(env), [], accel="tcg", tree=paths.Tree(self.top), say=lambda _: None
                )
            qemu.guest_layer(p)
            return p

        p = launch()
        emulator = Path(p.layer_dir) / "etc/emulator"
        self.assertEqual(store_snapshot(p.layer_dir), ("error", {"browser_secret": b"\0\xff"}))
        self.assertFalse((emulator / "sdp-model.key").exists())
        self.assertFalse((emulator / "youtube.key").exists())
        self.assertFalse((emulator / "ssg.key").exists())

        config.write_text('missing="fallback"\n[items]\n')
        p = launch()
        self.assertEqual(
            without_dvr_key(store_snapshot(p.layer_dir)),
            ("fallback", {"1D492999": sestore.DEFAULT_MODEL}),
        )

        # A valid configured key supersedes even an invalid legacy source.
        value = base64.b64encode(bytes(range(32)))
        config.write_text(
            'missing="fallback"\n[items]\n"1D492999"={text="'
            + value.decode()
            + '"}\n"C1FFB834"={text="dummy;key",nul_terminated=true}\n'
        )
        p = launch()
        self.assertEqual(
            without_dvr_key(store_snapshot(p.layer_dir)),
            ("fallback", {"1D492999": value, "C1FFB834": b"dummy;key\0"}),
        )
        self.assertFalse((emulator / "youtube.key").exists())
        self.assertEqual((ident / "model-secret").read_bytes(), b"invalid legacy input")
        self.assertEqual((emulator / "sestore.bin").stat().st_mode & 0o777, 0o600)
        config.write_text('[items]\n"1D492999"={text="malformed"}\n')
        with self.assertRaisesRegex(qemu.Error, "1D492999"):
            launch()

    def test_guest_layer_country(self):
        """COUNTRY: checked against the board's country group (countries.check, here a stand-in)
        and put in the per-VM layer as etc/emulator/country (0644), upper-cased; a refusal or a
        value that is not three letters stops the launch; none, no file."""
        from beanjar import countries

        ident = os.path.join(self.tmp, "vm", "identity")
        run = os.path.join(self.tmp, "vm", "run")
        os.makedirs(ident, mode=0o700)
        env = dict(
            FW=self.fw,
            STATE=self.state,
            **net("direct"),
            NET_LG_SERVICES="real",
            SLOT="1",
            IDENTITY_DIR=ident,
            RUN_DIR=run,
        )
        country = os.path.join(run, "share", "vm", "etc", "emulator", "country")
        checked = []

        def check(fw, target, code, tree=None, broadcast=None):
            checked.append(code)
            if code != "CAN":
                raise countries.Error(f"country {code!r} is not one of this board's: CAN, USA")

        def launch(**more):
            with (
                mock.patch.object(
                    partmap, "build", fake_partmap_build(os.path.join(self.tmp, "partinfo.img"))
                ),
                mock.patch.object(countries, "check", check),
            ):
                p = qemu.plan(
                    dict(env, **more),
                    [],
                    accel="tcg",
                    tree=paths.Tree(self.top),
                    say=lambda _: None,
                )
            qemu.guest_layer(p)
            return p

        launch()
        self.assertFalse(os.path.exists(country))
        self.assertEqual(launch(COUNTRY="can").country, "CAN")
        with open(country) as f:
            self.assertEqual(f.read(), "CAN\n")
        self.assertEqual(os.stat(country).st_mode & 0o777, 0o644)
        with self.assertRaisesRegex(qemu.Error, "COUNTRY: country 'GBR' is not one of"):
            launch(COUNTRY="GBR")
        with self.assertRaisesRegex(qemu.Error, "COUNTRY: country 'US': an ISO 3166 alpha-3"):
            launch(COUNTRY="US")
        self.assertEqual(checked, ["CAN", "GBR"])

    def test_guest_layer(self):
        """The per-VM layer (the guest's share "vm"), made afresh from the identity directory at
        each launch: the kernel-store bundle, and for rootssh and devmode in
        LAYERS the VM's host keys, root's authorized_keys (with the client key's public half) and
        the unencrypted developer key; files 0600 (the developer key's .pub 0644), directories
        0755 but .ssh and etc/emulator/devmode 0700. Nothing else of identity/, never the client
        private key. A key that model_key.py refuses, a private key in authorized_keys or a
        missing host key stops the launch. known_hosts follows the address (SLOT) and SSH_PORT."""
        ident = os.path.join(self.tmp, "vm", "identity")
        run = os.path.join(self.tmp, "vm", "run")
        os.makedirs(ident, mode=0o700)
        env = dict(
            FW=self.fw,
            STATE=self.state,
            **net("direct"),
            NET_LG_SERVICES="real",  # no CA in the layer (direct with the mock has one)
            SLOT="1",
            IDENTITY_DIR=ident,
            RUN_DIR=run,
        )
        layer = os.path.join(run, "share", "vm")

        def launch(**more):
            with mock.patch.object(
                partmap, "build", fake_partmap_build(os.path.join(self.tmp, "partinfo.img"))
            ):
                p = qemu.plan(
                    dict(env, **more),
                    [],
                    accel="tcg",
                    tree=paths.Tree(self.top),
                    say=lambda _: None,
                )
            qemu.guest_layer(p)
            self.assertIn(f"local,id=vm,path={layer},security_model=none,readonly=on", p.argv)
            return p

        def listing():
            return sorted(
                os.path.relpath(os.path.join(r, n), layer)
                for r, ds, fs in os.walk(layer)
                for n in ds + fs
            )

        def read(*p):
            with open(os.path.join(*p), "rb") as f:
                return f.read()

        def mode(rel):
            return os.stat(os.path.join(layer, rel)).st_mode & 0o777

        launch()
        self.assertEqual(listing(), ["etc", "etc/emulator", "etc/emulator/sestore.bin"])
        self.assertEqual(
            store_snapshot(layer)[1]["1D492999"],
            base64.b64encode(
                hashlib.sha256(b"webos-hypervisor emulator SDP_MODEL secret for 42LB650V").digest()
            ),
        )
        self.assertEqual(mode("."), 0o755)
        from beanjar import sestore

        sestore.set_model(ident, KEY)
        open(os.path.join(ident, "other"), "w").close()  # only what the layer is made of goes in
        launch()
        self.assertEqual(listing(), ["etc", "etc/emulator", "etc/emulator/sestore.bin"])
        self.assertEqual(without_dvr_key(store_snapshot(layer)), ("fallback", {"1D492999": KEY}))
        self.assertEqual(mode("etc/emulator/sestore.bin"), 0o600)
        for d in ("etc", "etc/emulator"):
            self.assertEqual(mode(d), 0o755)

        # the SSH layers without their keys: refused
        with self.assertRaisesRegex(qemu.Error, "is missing"):
            launch(LAYERS="normal,rootssh")
        with self.assertRaisesRegex(qemu.Error, "is missing"):
            launch(LAYERS="normal,devmode")
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".ssh"))
        with open(os.path.join(home, ".ssh", "id_user.pub"), "w") as f:
            f.write("ssh-ed25519 AAAAuser user@host\n")
        self.assertEqual(len(keys.ensure(ident)), 5)
        # by default the guest gets the VM's own client key alone, without its comment
        launch(LAYERS="normal,rootssh,devmode", SSH_PORT="7722")
        client = " ".join(read(ident, "id_ed25519.pub").decode().split()[:2])
        self.assertEqual(
            read(layer, "etc/emulator/rootssh/authorized_keys").decode().splitlines(), [client]
        )
        # the user's own keys only when added explicitly, and without their comments
        self.assertEqual(keys.add_mine(ident, home=home), ["ssh-ed25519 AAAAuser"])
        self.assertEqual(keys.add_mine(ident, home=home), [])
        launch(LAYERS="normal,rootssh,devmode", SSH_PORT="7722")
        self.assertEqual(
            listing(),
            [
                "etc",
                "etc/emulator",
                "etc/emulator/devmode",
                "etc/emulator/devmode/developer_rsa",
                "etc/emulator/devmode/developer_rsa.pub",
                "etc/emulator/devmode/ssh_host_rsa_key",
                "etc/emulator/rootssh",
                "etc/emulator/rootssh/authorized_keys",
                "etc/emulator/rootssh/dropbear_ed25519_host_key",
                "etc/emulator/sestore.bin",
                "home",
                "home/root",
                "home/root/.ssh",
                "home/root/.ssh/authorized_keys",
            ],
        )
        for rel, m in (
            ("etc/emulator/devmode", 0o700),
            ("etc/emulator/devmode/developer_rsa", 0o600),
            ("etc/emulator/devmode/developer_rsa.pub", 0o644),
            ("etc/emulator/devmode/ssh_host_rsa_key", 0o600),
            ("etc/emulator/rootssh", 0o755),
            ("etc/emulator/rootssh/authorized_keys", 0o600),
            ("etc/emulator/rootssh/dropbear_ed25519_host_key", 0o600),
            ("home", 0o755),
            ("home/root", 0o755),
            ("home/root/.ssh", 0o700),
            ("home/root/.ssh/authorized_keys", 0o600),
        ):
            self.assertEqual(mode(rel), m, rel)
        self.assertEqual(
            read(layer, "etc/emulator/rootssh/dropbear_ed25519_host_key"),
            read(ident, "rootssh_host_key"),
        )
        self.assertEqual(
            read(layer, "etc/emulator/devmode/ssh_host_rsa_key"), read(ident, "devmode_host_key")
        )
        self.assertEqual(
            read(layer, "etc/emulator/devmode/developer_rsa"), read(ident, "devmode_rsa")
        )
        rsa_header = b"-----BEGIN RSA PRIVATE KEY-----\n"  # gitleaks:allow
        self.assertTrue(read(ident, "devmode_rsa").startswith(rsa_header))
        self.assertNotIn(b"ENCRYPTED", read(ident, "devmode_rsa"))
        auth = read(layer, "etc/emulator/rootssh/authorized_keys").decode().splitlines()
        self.assertEqual(auth, ["ssh-ed25519 AAAAuser", client])
        self.assertEqual(
            read(layer, "home/root/.ssh/authorized_keys"),
            read(layer, "etc/emulator/rootssh/authorized_keys"),
        )
        client = read(ident, "id_ed25519")
        for r, _, fs in os.walk(layer):
            for n in fs:
                self.assertNotEqual(read(r, n), client, "the client key is in the layer")
                self.assertNotIn(b"OPENSSH PRIVATE KEY", read(r, n), n)
        kh = read(ident, "known_hosts").decode().splitlines()
        self.assertEqual([line.split()[0] for line in kh], ["[127.0.1.1]:7722", "[127.0.1.1]:9922"])
        self.assertEqual(
            kh[0].split()[1:], read(ident, "rootssh_host_key.pub").decode().split()[:2]
        )
        # the pins follow the VM's address (its slot), at the stock ports
        launch(LAYERS="normal,rootssh,devmode", SLOT="7")
        self.assertEqual(
            [line.split()[0] for line in read(ident, "known_hosts").decode().splitlines()],
            ["[127.0.1.7]:6622", "[127.0.1.7]:9922"],
        )
        # and its library's addresses (ADDRESS_PREFIX), rewritten at the next launch: no old pin kept
        launch(LAYERS="normal,rootssh,devmode", SLOT="7", ADDRESS_PREFIX="127.0.2")
        self.assertEqual(
            [line.split()[0] for line in read(ident, "known_hosts").decode().splitlines()],
            ["[127.0.2.7]:6622", "[127.0.2.7]:9922"],
        )
        # hbchannel: the same keys under its own name, and root's authorized_keys; nothing of rootssh
        launch(LAYERS="normal,hbchannel,devmode", SSH_PORT="7722")
        self.assertEqual(
            listing(),
            [
                "etc",
                "etc/emulator",
                "etc/emulator/devmode",
                "etc/emulator/devmode/developer_rsa",
                "etc/emulator/devmode/developer_rsa.pub",
                "etc/emulator/devmode/ssh_host_rsa_key",
                "etc/emulator/hbchannel",
                "etc/emulator/hbchannel/authorized_keys",
                "etc/emulator/hbchannel/dropbear_ed25519_host_key",
                "etc/emulator/sestore.bin",
                "home",
                "home/root",
                "home/root/.ssh",
                "home/root/.ssh/authorized_keys",
            ],
        )
        for rel, m in (
            ("etc/emulator/hbchannel", 0o755),
            ("etc/emulator/hbchannel/authorized_keys", 0o600),
            ("etc/emulator/hbchannel/dropbear_ed25519_host_key", 0o600),
            ("home/root/.ssh", 0o700),
            ("home/root/.ssh/authorized_keys", 0o600),
        ):
            self.assertEqual(mode(rel), m, rel)
        self.assertEqual(
            read(layer, "etc/emulator/hbchannel/dropbear_ed25519_host_key"),
            read(ident, "rootssh_host_key"),
        )
        self.assertEqual(
            read(layer, "etc/emulator/hbchannel/authorized_keys").decode().splitlines(), auth
        )
        self.assertEqual(
            read(layer, "home/root/.ssh/authorized_keys"),
            read(layer, "etc/emulator/hbchannel/authorized_keys"),
        )
        self.assertEqual(
            read(ident, "known_hosts").decode().splitlines()[0].split()[:3],
            ["[127.0.1.1]:7722"] + read(ident, "rootssh_host_key.pub").decode().split()[:2],
        )
        launch(LAYERS="normal,devmode")
        self.assertFalse(
            os.path.exists(os.path.join(layer, "etc/emulator/rootssh")),
            "rootssh's keys without rootssh",
        )
        self.assertFalse(
            os.path.exists(os.path.join(layer, "etc/emulator/hbchannel")), "hbchannel's without it"
        )
        self.assertFalse(os.path.exists(os.path.join(layer, "home")))
        with open(os.path.join(ident, "authorized_keys"), "a") as f:
            f.write("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        with self.assertRaisesRegex(qemu.Error, "private key"):
            launch(LAYERS="rootssh")

        config = Path(ident, "sestore.toml")
        config.chmod(0o644)
        with self.assertRaisesRegex(qemu.Error, "owner-only"):
            launch()
        config.chmod(0o600)
        config.write_text('[items]\n"1D492999"={text="malformed"}\n')
        with self.assertRaisesRegex(qemu.Error, "1D492999"):
            launch()
        config.write_text("[items]\n")
        sestore.set_model(ident, None)
        launch()
        self.assertEqual(listing(), ["etc", "etc/emulator", "etc/emulator/sestore.bin"])
        self.assertEqual(
            store_snapshot(layer)[1]["1D492999"],
            base64.b64encode(
                hashlib.sha256(b"webos-hypervisor emulator SDP_MODEL secret for 42LB650V").digest()
            ),
        )

        # mode=mitm: the proxy CA's certificate (NETSTUB_CA, here a library's) and its subject hash,
        # 0644; never its key. Without the CA the launch stops.
        ca = os.path.join(self.tmp, "lib", "ca")
        with self.assertRaisesRegex(qemu.Error, "ca.pem is missing"):
            launch(**net("mitm"), NETSTUB_CA=ca)
        self.assertTrue(keys.ensure_ca(ca))
        self.assertFalse(keys.ensure_ca(ca), "made once")
        p = launch(**net("mitm"), NETSTUB_CA=ca)
        self.assertEqual((p.env["NETSTUB_CA"], p.ca), (ca, os.path.join(ca, "ca.pem")))
        self.assertEqual(
            listing(),
            [
                "etc",
                "etc/emulator",
                "etc/emulator/netstub-ca.hash",
                "etc/emulator/netstub-ca.pem",
                "etc/emulator/sestore.bin",
            ],
        )
        self.assertEqual(read(layer, "etc/emulator/netstub-ca.pem"), read(ca, "ca.pem"))
        digest = subprocess.run(
            ["openssl", "x509", "-noout", "-subject_hash", "-in", os.path.join(ca, "ca.pem")],
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        self.assertEqual(read(layer, "etc/emulator/netstub-ca.hash"), digest)
        for rel in ("etc/emulator/netstub-ca.pem", "etc/emulator/netstub-ca.hash"):
            self.assertEqual(mode(rel), 0o644, rel)
        ca_key = read(ca, "ca.key")
        for r, _, fs in os.walk(layer):
            for n in fs:
                self.assertNotIn(b"PRIVATE KEY", read(r, n), n)
                self.assertNotIn(ca_key.splitlines()[1], read(r, n), n)
        self.assertNotIn(
            "FIRMWARE_CA", p.env, "the proxy has mitmproxy's own roots, nothing of the firmware's"
        )
        launch(**net("direct"))
        self.assertFalse(os.path.exists(os.path.join(layer, "etc/emulator/netstub-ca.pem")))
        self.assertFalse(os.path.exists(os.path.join(layer, "etc/emulator/netstub-ca.hash")))

    def test_state_none_recreates_written_blank(self):
        blank = os.path.join(self.top, "build", "state", ".blank")
        os.makedirs(blank)
        with open(os.path.join(blank, "db8.img"), "wb") as f:
            f.write(b"x" * 4096)
        p = qemu.plan(
            dict(
                FW=self.fw,
                STATE="none",
                **net("direct"),
                SLOT="1",
                RUN_DIR=os.path.join(self.top, "build"),
            ),
            [],
            accel="tcg",
            tree=paths.Tree(self.top),
            say=lambda _: None,
            dry=True,
        )
        qemu.make_disks(p)
        self.assertEqual(os.stat(os.path.join(blank, "db8.img")).st_blocks, 0)

    def test_state_none_in_a_run_reuses_its_fresh_state(self):
        """STATE=none with the run's FRESH_STATE (unit.run): the disks are that directory's, read-write
        (no snapshot=on), made at the first launch and used as the guest left them by the next one,
        a guest restart's; nothing goes to .blank."""
        fresh = os.path.join(self.top, "build", "state", ".fresh")
        os.makedirs(fresh)
        said = []

        def plan():
            return qemu.plan(
                dict(
                    FW=self.fw,
                    STATE="none",
                    FRESH_STATE=fresh,
                    **net("direct"),
                    SLOT="1",
                    RUN_DIR=os.path.join(self.top, "build"),
                ),
                [],
                accel="tcg",
                tree=paths.Tree(self.top),
                say=said.append,
                dry=True,
            )

        p = plan()
        self.assertEqual(p.state_dir, fresh)
        drives = [a for a in p.argv if a.startswith("if=none,id=db8,")]
        self.assertEqual(len(drives), 1)
        self.assertIn(f"file={fresh}/db8.img,", drives[0])
        self.assertNotIn("snapshot=on", drives[0])
        self.assertIn(os.path.join(fresh, "db8.img"), [img for img, _, _ in p.disks])
        qemu.make_disks(p)
        with open(os.path.join(fresh, "db8.img"), "r+b") as f:
            f.write(b"x" * 4096)  # the first boot's writes
        p = plan()
        self.assertEqual((p.remove, p.disks), ([], []), "the restart's launch makes nothing")
        qemu.make_disks(p)
        with open(os.path.join(fresh, "db8.img"), "rb") as f:
            self.assertEqual(f.read(4096), b"x" * 4096)
        self.assertFalse(os.path.exists(os.path.join(self.top, "build", "state", ".blank")))
        self.assertTrue(any(s.startswith("state: none (this run's fresh state") for s in said))

    def test_scripts_vm_argv(self):
        """`host/scripts/vm-launcher argv`: the checkout's command, printed; nothing made."""
        state = os.path.join(self.tmp, "st")
        env = dict(
            os.environ,
            FW=self.fw,
            STATE=state,
            **net("direct"),
            DISPLAY_BACKEND="none",
            ACCEL="tcg",
        )
        r = subprocess.run(
            [sys.executable, VM, "argv", "a=1"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if "missing" in r.stderr:
            self.skipTest("the checkout has no guest build")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = shlex.split(r.stdout.splitlines()[-1])
        self.assertEqual(argv[:3], [paths.TREE.qemu(env)[0], "-M", "virt"])
        self.assertIn(" a=1", argv[argv.index("-append") + 1])
        self.assertFalse(os.path.exists(state))

    def test_first_rendernode_skips_vgem(self):
        """vgem's node (a dma-buf stand-in WSL loads for VA-API) is no GPU for virgl: the first other
        render node, else none."""
        devs = {
            "/sys/class/drm/renderD128/device": "/sys/devices/faux/vgem",
            "/sys/class/drm/renderD129/device": "/sys/devices/pci0000:00/0000:00:02.0",
        }
        nodes = ["/dev/dri/renderD128", "/dev/dri/renderD129"]
        with (
            mock.patch.object(qemu.glob, "glob", return_value=nodes),
            mock.patch.object(qemu, "is_char", return_value=True),
            mock.patch.object(qemu.os.path, "realpath", side_effect=lambda p: devs.get(p, p)),
        ):
            self.assertEqual(qemu.first_rendernode(), "/dev/dri/renderD129")
            devs["/sys/class/drm/renderD129/device"] = "/sys/devices/platform/vgem"
            self.assertIsNone(qemu.first_rendernode())

    def test_tcg_rendernode(self):
        """The rule the host build's `qemu` option asks (`host/scripts/vm-launcher config RENDERNODE`): RENDERNODE,
        else the first render node, else surfaceless, as the TCG display takes it."""
        with mock.patch.object(qemu, "first_rendernode", return_value=None):
            self.assertEqual(qemu.tcg_rendernode({}), "surfaceless")
            self.assertEqual(
                qemu.tcg_rendernode({"RENDERNODE": "/dev/dri/renderD129"}), "/dev/dri/renderD129"
            )
        with mock.patch.object(qemu, "first_rendernode", return_value="/dev/dri/renderD128"):
            self.assertEqual(qemu.tcg_rendernode({}), "/dev/dri/renderD128")
            self.assertEqual(qemu.tcg_rendernode({"RENDERNODE": "surfaceless"}), "surfaceless")
        r = subprocess.run(
            [sys.executable, VM, "config", "RENDERNODE"],
            stdout=subprocess.PIPE,
            text=True,
            env=dict(os.environ, RENDERNODE="/dev/dri/renderD130"),
            check=True,
        )
        self.assertEqual(r.stdout, "/dev/dri/renderD130\n")

    def test_config_identity(self):
        """`vm-launcher config IDENTITY` prints the identity directory the launcher uses: IDENTITY_DIR when
        set (relative to the tree), else the managed development VM's identity/."""

        def config(**env):
            return subprocess.run(
                [sys.executable, VM, "config", "IDENTITY"],
                stdout=subprocess.PIPE,
                text=True,
                env=dict(os.environ, **env),
                check=True,
            ).stdout

        with tempfile.TemporaryDirectory() as d:
            env = {k: v for k, v in os.environ.items() if k != "IDENTITY_DIR"}
            env.update(BEANJAR_LIBRARY=d)
            r = subprocess.run(
                [sys.executable, VM, "config", "IDENTITY"],
                stdout=subprocess.PIPE,
                text=True,
                env=env,
                check=True,
            )
            self.assertTrue(r.stdout.startswith(d + "/development-"), r.stdout)
            self.assertTrue(r.stdout.endswith("/identity\n"), r.stdout)
            self.assertEqual(
                config(BEANJAR_LIBRARY=d, IDENTITY_DIR="/custom/identity"), "/custom/identity\n"
            )
            self.assertEqual(
                config(BEANJAR_LIBRARY=d, IDENTITY_DIR="local"), os.path.join(TOP, "local") + "\n"
            )
            # MAC and SERIAL: the environment's, else the development VM's; none before it is made
            for var in ("MAC", "SERIAL"):
                r = subprocess.run(
                    [sys.executable, VM, "config", var],
                    stdout=subprocess.PIPE,
                    text=True,
                    env={k: v for k, v in env.items() if k not in ("MAC", "SERIAL")},
                    check=True,
                )
                self.assertEqual(r.stdout, "\n")
                self.assertEqual(os.listdir(d), [])  # nothing made
            r = subprocess.run(
                [sys.executable, VM, "config", "MAC"],
                stdout=subprocess.PIPE,
                text=True,
                env=dict(env, MAC="02:00:00:00:00:09"),
                check=True,
            )
            self.assertEqual(r.stdout, "02:00:00:00:00:09\n")

    def test_firmware_target(self):
        """paths.firmware_target: the one place a firmware's (chip, release) comes from; the launcher, the
        build (host/scripts/vm-launcher config TARGET) and the layers follow it."""
        fw = os.path.join(self.tmp, FW_NAME)
        self.assertEqual(paths.firmware_target(fw), H13_AFRO)
        own = os.path.join(self.tmp, "vm", "firmware")  # a manager VM's own copy: its manifest
        os.makedirs(own)
        with self.assertRaisesRegex(ValueError, "unknown-model firmware might be unsupported"):
            paths.firmware_target(own)
        # the target import recorded, and nothing else: a manifest without it is refused, not guessed
        for text, error in (
            (f'name = "{FW_NAME}"\nchip = "WT1H"\n', "manifest.toml: no chip and release"),
            (
                f'name = "{FW_NAME}"\nchip = "k6hp"\nrelease = "qilian"\n',
                "k6hp-qilian firmware might be unsupported: no board definition covers it",
            ),
            (f'name = "{FW_NAME}"\nchip = "h13"\nrelease = 1\n', "no chip and release"),
            (f'name = "{FW_NAME}"\nchip = "h13"\nmajor = 1\n', "no chip and release"),
        ):
            with open(os.path.join(own, "manifest.toml"), "w") as f:
                f.write(text)
            with self.assertRaisesRegex(ValueError, error):
                paths.firmware_target(own)
        with open(os.path.join(own, "manifest.toml"), "w") as f:
            f.write('name = "renamed"\nchip = "h13"\nrelease = "afro"\n')
        self.assertEqual(paths.firmware_target(own), H13_AFRO)
        # Imported firmware retains the collection's LLS cache name even though
        # its managed directory is simply called "firmware".
        self.assertEqual(paths.Tree(self.tmp).firmware(own), (own, "renamed"))
        with self.assertRaisesRegex(
            ValueError, "W99X firmware might be unsupported: no board definition covers it"
        ):
            paths.firmware_target(os.path.join(self.tmp, "43.22.40.01-HE_DTV_W99X_AFABJAPU"))
        # without a manifest: the release its rootfs names, that release's chip for the model
        named = os.path.join(self.tmp, "renamed")
        os.makedirs(named)
        with self.assertRaisesRegex(ValueError, "no rootfs.pak: its webOS release is unknown"):
            paths.firmware_target(named, FW_NAME)
        shutil.copyfile(TRUST_PAK, os.path.join(named, "rootfs.pak"))  # afro
        self.assertEqual(paths.firmware_target(named, FW_NAME), H13_AFRO)
        with self.assertRaisesRegex(
            ValueError,
            r"W15M afro firmware might be unsupported: no board definition covers it",
        ):
            paths.firmware_target(named, "04.06.75.01-HE_DTV_W15M_AFAAABAA")
        self.assertEqual(
            paths.Tree("/t").layer(H13_AFRO, "soc", "x"), "/t/build/layers/h13-afro/soc/x"
        )
        r = subprocess.run(
            [sys.executable, VM, "config", "TARGET"],
            stdout=subprocess.PIPE,
            text=True,
            env=dict(os.environ, FW=own),
            check=True,
        )
        self.assertEqual(r.stdout, "h13-afro\n")
        r = subprocess.run(
            [sys.executable, VM, "config", "TARGET"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, FW=self.tmp),
        )
        self.assertEqual(r.returncode, 1)
        with self.assertRaisesRegex(qemu.Error, "firmware might be unsupported"):
            qemu.plan(
                dict(FW=self.tmp, **net("direct"), SLOT="1"),
                [],
                accel="tcg",
                tree=paths.Tree(self.top),
                say=lambda _: None,
                dry=True,
            )

    def firmware_config(self, *dirs):
        """An XDG_CONFIG_HOME whose beanjar/config.toml lists dirs as the host's firmware_dirs."""
        cfg = os.path.join(self.tmp, "config")
        os.makedirs(os.path.join(cfg, "beanjar"), exist_ok=True)
        with open(os.path.join(cfg, "beanjar", "config.toml"), "w") as f:
            f.write(f"firmware_dirs = {tomlfile.toml_value(list(dirs))}\n")
        return cfg

    def test_target_firmware(self):
        """A target's firmware (host/scripts/vm-launcher config FW --target, what firmware-facts
        reads): the default firmware if it resolves to the target, else the one that does, the host's
        first collection first (config.toml's firmware_dirs). A firmware is the target its rootfs's
        release resolves to, not its model's."""
        home, top = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "top")
        fws = os.path.join(home, "Projects", "webos-firmwares")
        cfg = self.firmware_config(os.path.join(top, "scratch", "firmware"), fws)
        w17h, w17h_b = "06.10.65.01-HE_DTV_W17H_AFADABAA", "06.20.00.01-HE_DTV_W17H_AFADABAA"
        w18r = "05.50.70.01-HE_DTV_W18R_AFAAABAA"
        releases = {
            FW_NAME: "afro",
            w17h: "dreadlocks2",
            w17h_b: "dreadlocks2",
            w18r: "dreadlocks2",
            "04.63.25.01-HE_DTV_W20L_AFAAJAAA": "jhericurl",
        }
        for n in (FW_NAME, w17h, w18r, "04.63.25.01-HE_DTV_W20L_AFAAJAAA"):
            os.makedirs(os.path.join(fws, n))
        os.makedirs(os.path.join(top, "scratch", "firmware", w17h_b))
        # A stale checkout-root directory must not participate in discovery.
        os.makedirs(os.path.join(top, "firmware", w17h))

        def release(d):
            if not os.path.isdir(d):
                return None, "no rootfs.pak"
            return releases[os.path.basename(d)], "rootfs.pak's etc/starfish-release"

        tree, m16p = paths.Tree(top), ("m16p", "dreadlocks2")
        with (
            mock.patch.dict(os.environ, HOME=home, XDG_CONFIG_HOME=cfg),
            mock.patch.object(paths, "firmware_release", release),
        ):
            self.assertEqual(tree.target_firmware(H13_AFRO), (os.path.join(fws, FW_NAME), FW_NAME))
            self.assertEqual(
                tree.target_firmware(m16p),
                (os.path.join(top, "scratch", "firmware", w17h_b), w17h_b),
            )
            os.rename(os.path.join(top, "scratch", "firmware", w17h_b), os.path.join(fws, w17h_b))
            with self.assertRaisesRegex(ValueError, "several firmwares of m16p-dreadlocks2"):
                tree.target_firmware(m16p)
            # W18R's rootfs names dreadlocks2, a release MODELS has no W18R row for: of no target
            with self.assertRaisesRegex(ValueError, "no firmware of m3-goldilocks"):
                tree.target_firmware(("m3", "goldilocks"))
            # one model, two releases: each firmware is the target of its own release
            models = dict(targets.MODELS, W17H={"dreadlocks2": "m16p", "goldilocks": "m3"})
            releases[w17h_b] = "goldilocks"
            with mock.patch.object(targets, "MODELS", models):
                self.assertEqual(tree.target_firmware(m16p), (os.path.join(fws, w17h), w17h))
                self.assertEqual(
                    tree.target_firmware(("m3", "goldilocks")), (os.path.join(fws, w17h_b), w17h_b)
                )
                self.assertEqual(
                    paths.firmware_target(os.path.join(fws, w17h_b)), ("m3", "goldilocks")
                )
            os.rename(os.path.join(fws, FW_NAME), os.path.join(fws, "x"))  # no default firmware
            with self.assertRaisesRegex(ValueError, "no firmware of h13-afro"):
                tree.target_firmware(H13_AFRO)
        r = subprocess.run(
            [sys.executable, VM, "config", "TARGET", "--target", "h13-afro"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(r.returncode, 1)

    def test_target_firmware_leaves_out_bring_up_model(self):
        """W21Z boots on W21P's supported target but is in bring-up: the target's firmware is W21P's, as in regress's
        select(); W21Z alone is no firmware of the target (it boots only when named, FW)."""
        home, top = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "top")
        fws = os.path.join(home, "Projects", "webos-firmwares")
        w21p, w21z = "03.21.10.01-HE_DTV_W21P_AFADATAA", "03.20.25.01-HE_PJT_W21Z_AFADATAA"
        self.assertIn("W21Z", targets.BRING_UP)
        os.makedirs(os.path.join(fws, w21z))
        os.makedirs(os.path.join(top, "scratch", "firmware"))
        cfg = self.firmware_config(os.path.join(top, "scratch", "firmware"), fws)

        def release(d):
            return (
                ("kisscurl", "rootfs.pak's etc/starfish-release")
                if os.path.isdir(d)
                else (None, "no rootfs.pak")
            )

        tree, k7lp = paths.Tree(top), ("k7lp", "kisscurl")
        with (
            mock.patch.dict(os.environ, HOME=home, XDG_CONFIG_HOME=cfg),
            mock.patch.object(paths, "firmware_release", release),
        ):
            with self.assertRaisesRegex(ValueError, "no firmware of k7lp-kisscurl"):
                tree.target_firmware(k7lp)
            os.makedirs(os.path.join(fws, w21p))
            self.assertEqual(tree.target_firmware(k7lp), (os.path.join(fws, w21p), w21p))
            # an explicit firmware still resolves to the target
            self.assertEqual(tree.target(os.path.join(fws, w21z)), k7lp)
        self.assertEqual(
            paths.default_candidates(k7lp, {w21z: os.path.join(fws, w21z), w21p: None}), [w21p]
        )
        # a target wholly in bring-up keeps its bring-up firmware
        w22p = "03.30.00.01-HE_DTV_W22P_AFADATAA"
        self.assertTrue(targets.in_bring_up(("k8lp", "mullet")))
        self.assertEqual(paths.default_candidates(("k8lp", "mullet"), {w22p: None}), [w22p])

    def test_target_firmware_prefers_default_board(self):
        """Of several firmwares of a target, the one that boots the chip's default board
        (targets.boots_default_board): K8HP's and O22's US board, so a global firmware over a
        Japanese ARIB one (its model_id.json region, here by the OTA ID's J). None or several such
        stay ambiguous."""
        self.enterContext(
            mock.patch.object(
                paths,
                "model_id",
                lambda d: {
                    "region": "ARIB" if "_AFA" in d and "J" in d.rsplit("_", 1)[-1] else "GLOBAL"
                },
            )
        )
        home, top = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "top")
        fws = os.path.join(home, "Projects", "webos-firmwares")
        os.makedirs(os.path.join(top, "scratch", "firmware"))
        cfg = self.firmware_config(os.path.join(top, "scratch", "firmware"), fws)
        us, jp = "43.22.73.01-HE_DTV_W22H_AFABATPU", "43.22.40.01-HE_DTV_W22H_AFABJAPU"
        for n, pak in ((us, "otncabi-global.pak"), (jp, "otncabi-arib.pak")):
            os.makedirs(os.path.join(fws, n))
            open(os.path.join(fws, n, pak), "w").close()

        def release(d):
            return (
                ("queue", "rootfs.pak's etc/starfish-release") if os.path.isdir(d) else (None, "")
            )

        tree, k8hp = paths.Tree(top), ("k8hp", "queue")
        self.assertEqual(targets.default_broadcast(k8hp), "atsc")
        with (
            mock.patch.dict(os.environ, HOME=home, XDG_CONFIG_HOME=cfg),
            mock.patch.object(paths, "firmware_release", release),
        ):
            self.assertEqual(tree.target_firmware(k8hp), (os.path.join(fws, us), us))
            # two global firmwares: both boot the default board
            us2 = "43.22.80.01-HE_DTV_W22H_AFABATPU"
            os.makedirs(os.path.join(fws, us2))
            with self.assertRaisesRegex(ValueError, "several firmwares of k8hp-queue"):
                tree.target_firmware(k8hp)
        # O22's queue: its US global over its Japanese ARIB build
        o22 = ("o22", "queue")
        o22_us, o22_jp = "43.21.89.01-HE_DTV_W22O_AFABATPU", "43.21.89.01-HE_DTV_W22O_AFABJAPU"
        os.makedirs(os.path.join(self.tmp, o22_jp))
        open(os.path.join(self.tmp, o22_jp, "otncabi-arib.pak"), "w").close()
        self.assertEqual(
            paths.default_candidates(
                o22, {o22_jp: os.path.join(self.tmp, o22_jp), o22_us: os.path.join(fws, o22_us)}
            ),
            [o22_us],
        )
        # neither boots the default board: both stay
        jp2 = "04.60.85.01-HE_DTV_W22H_AFADJAAA"
        os.makedirs(os.path.join(fws, jp2))
        open(os.path.join(fws, jp2, "otycabi-arib.pak"), "w").close()
        self.assertEqual(
            paths.default_candidates(
                ("k8hp", "mullet"), {jp: os.path.join(fws, jp), jp2: os.path.join(fws, jp2)}
            ),
            [jp, jp2],
        )
