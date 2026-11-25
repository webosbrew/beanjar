#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""scripts/regress without a VM (`meson test -C build/host --suite regress`, or run directly): the
targets and their firmwares, the arguments, and a dry run that must print every step of each target
in order and touch nothing."""

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
loader = importlib.machinery.SourceFileLoader("regress", os.path.join(TOP, "scripts", "regress"))
regress = importlib.util.module_from_spec(importlib.util.spec_from_loader("regress", loader))
loader.exec_module(regress)

# A run over ssh needs a named VM host and a sync command (neither has a built-in default): the
# tests give them through the environment unless they say otherwise.
_parse = regress.parse


def _parse_with_host(argv, found=None, env=None):
    env = dict(os.environ if env is None else env)
    env.setdefault("REGRESS_HOST", "vmhost")
    env.setdefault("REGRESS_SYNC", "sync-checkout")
    return _parse(argv, found=found, env=env)


regress.parse = _parse_with_host

WT1H, WT1H_OLD = "05.06.10.01-HE_DTV_WT1H_AFAAABAA", "04.00.01.01-HE_DTV_WT1H_AFAAABAA"
W15M, W16R = "04.06.75.01-HE_DTV_W15M_AFAAABAA", "05.70.50.01-HE_DTV_W16R_AFAAABAA"
W17H, W18R = "06.10.65.01-HE_DTV_W17H_AFADABAA", "05.50.70.01-HE_DTV_W18R_AFAAABAA"
W19P, W20L = "05.40.97.01-HE_DTV_W19P_AFADABAA", "04.63.25.01-HE_DTV_W20L_AFAAJAAA"
W21P = "03.70.25.01-HE_DTV_W21P_AFADATAA"
W15U = "04.06.25.01-HE_DTV_W15U_AFADABAA"
W15L = "03.31.00.01-HE_DTV_W15L_AFAAABAA"
W19R = "05.40.97.01-HE_DTV_W19R_AFAAATAA"
W17R = "06.10.60.01-HE_DTV_W17R_AFAAATAA"
W17M = "06.10.55.01-HE_DTV_W17M_AFADATAA"
W18H = "05.50.70.01-HE_DTV_W18H_AFADATAA"
W18M = "05.50.60.01-HE_DTV_W18M_AFADJAAA"
W18A = "05.50.70.01-HE_DTV_W18A_AFADATAA"
W19H = "05.50.00.01-HE_DTV_W19H_AFADATAA"
W19O = "05.50.00.01-HE_DTV_W19O_AFABATAA"
W19K = "05.50.00.01-HE_DTV_W19K_AFADATAA"
W17P = "06.10.75.01-HE_DTV_W17P_AFADABAA"
W16P = "05.70.50.01-HE_DTV_W16P_AFADATAA"
W16K = "05.70.55.01-HE_DTV_W16K_AFADATAA"
W21O = "03.70.05.01-HE_DTV_W21O_AFABATAA"
W21K = "03.70.25.01-HE_DTV_W21K_AFADATAA"
W23M = ("13.40.97", "23.25.55", "33.31.61", "43.22.75")
W23L = "13.50.75.01-HE_DTV_W23L_AFAAATAA"
N21D6, N21D = "04.44.16.01-HE_DTV_N21D_AFAAATAA", "23.22.86.01-HE_DTV_N21D_AFAAATAA"


def fws(*names, d="/nonexistent"):
    """find_firmwares()'s {firmware name: its directory} of names, in the collection d."""
    return {n: os.path.join(d, n) for n in names}


FOUND = {
    "h13-afro": fws(WT1H_OLD, WT1H),
    "m14-beehive": fws(W15M),
    "lm15u-beehive": fws(W15U),
    "mtka5lr-beehive": fws(W15L),
    "m2-dreadlocks": fws(W16R),
    "m16p-dreadlocks2": fws(W17H),
    "m16plite-dreadlocks2": fws(W17M),
    "m16pp-goldilocks": fws(W18H),
    "m16pplite-goldilocks": fws(W18M),
    "m16p3-goldilocks2": fws(W19H),
    "m23-number1": fws(W23M[0] + ".01-HE_DTV_W23M_AFADATAA"),
    "m23-ombre": fws(W23M[1] + ".01-HE_DTV_W23M_AFADATAA"),
    "m23-ponytail": fws(W23M[2] + ".01-HE_DTV_W23M_AFADATAA"),
    "m23-queue": fws(W23M[3] + ".01-HE_DTV_W23M_AFADATAA"),
    "m2r-dreadlocks2": fws(W17R),
    "m3-goldilocks": fws(W18R),
    "m3r-goldilocks2": fws(W19R),
    "lm18a-goldilocks": fws(W18A),
    "k2l-dreadlocks": fws(W16K),
    "k2lp-dreadlocks": fws(W16P),
    "k3lp-dreadlocks2": fws(W17P),
    "k5lp-goldilocks2": fws(W19P),
    "k6lp-jhericurl": fws(W20L),
    "k7lp-kisscurl": fws(W21P),
    "o18-goldilocks2": fws(W19O),
    "o18k-goldilocks2": fws(W19K),
    "o20n-kisscurl": fws(W21O),
    "e60n-kisscurl": fws(W21K),
    "kf23f-number1": fws(W23L),
    "lm21u-kisscurl": fws(N21D6),
    "lm21u-ombre": fws(N21D),
}


CAPABLE = {"arch": "aarch64", "toolchain": True, "render": True, "desktop": True}


def table_lines(text):
    """The summary table's lines, without the wall time line after it."""
    return text.split("\n\nwall ")[0].splitlines()


class SelectTest(unittest.TestCase):
    def test_every_target_by_default(self):
        _, sel = regress.parse([], found=FOUND, env={})
        self.assertEqual([p for p, _ in sel], sorted(FOUND))
        # The supported model table explicitly marks the unfinished bring-up profiles: a target is in
        # bring-up when every model on it is (k7lp-kisscurl is not: W21P is supported, W21Z is not).
        self.assertEqual(
            regress.bring_up_targets(),
            [
                "h13-netcast4",
                "k24-ombre",
                "k24-ponytail",
                "k24-queue",
                "k24n-ponytail",
                "k24n-queue",
                "k25lp-ponytail",
                "k25lp-queue",
                "k6hp-jhericurl",
                "k8ap-mullet",
                "k8hp-mullet",
                "k8hp-number1",
                "k8hp-ombre",
                "k8hp-ponytail",
                "k8hp-queue",
                "k8hpp-number1",
                "k8hpp-ombre",
                "k8hpp-ponytail",
                "k8hpp-queue",
                "k8lp-mullet",
                "k8lpn-number1",
                "k8lpn-ombre",
                "k8lpn-ponytail",
                "k8lpn-queue",
                "k8lpn2-ombre",
                "k8lpn2-ponytail",
                "k8lpn2-queue",
                "lm14-netcast45",
                "lm21a-kisscurl",
                "lm21an-mullet",
                "lm21ann-number1",
                "lm21ann-ombre",
                "lm21ann-ponytail",
                "lm21ann-queue",
                "m2-simplesmart",
                "o22-mullet",
                "o22-number1",
                "o22-ombre",
                "o22-ponytail",
                "o22-queue",
                "o22n-number1",
                "o22n-ombre",
                "o22n-ponytail",
                "o22n-queue",
                "o22n2-ombre",
                "o22n2-ponytail",
                "o22n2-queue",
                "o22n2-spacebuns",
                "o22n3-ponytail",
                "o22n3-queue",
                "o24-ombre",
                "o24-ponytail",
                "o24-queue",
                "o24n-ponytail",
                "o24n-queue",
                "o26-queue",
                "virt-testguest",
            ],
        )
        self.assertEqual(dict(sel)["h13-afro"], WT1H)  # the default firmware among several

    def test_targets_picked(self):
        _, sel = regress.parse(["--target", "m2-dreadlocks,h13-afro"], found=FOUND, env={})
        self.assertEqual(sel, [("h13-afro", WT1H), ("m2-dreadlocks", W16R)])
        _, sel = regress.parse(
            ["--target", "m14-beehive", "--target", "m2-dreadlocks"], found=FOUND, env={}
        )
        self.assertEqual([p for p, _ in sel], ["m14-beehive", "m2-dreadlocks"])

    def test_bring_up_target_only_when_named(self):
        with mock.patch.object(regress.targets, "BRING_UP", regress.targets.BRING_UP | {"W20L"}):
            _, sel = regress.parse([], found=FOUND, env={})
            self.assertNotIn("k6lp-jhericurl", [p for p, _ in sel])
            _, sel = regress.parse(["--target", "k6lp-jhericurl"], found=FOUND, env={})
            self.assertEqual(sel, [("k6lp-jhericurl", W20L)])

    def test_untested_target_only_when_named(self):
        # a target file's tested = false: out of the default set, run only when named (with a warning)
        key = ("k6lp", "jhericurl")
        untested = regress.targets.TARGETS[key]._replace(tested=False)
        with mock.patch.dict(regress.targets.TARGETS, {key: untested}):
            self.assertIn("k6lp-jhericurl", regress.named_only_targets())
            _, sel = regress.parse([], found=FOUND, env={})
            self.assertNotIn("k6lp-jhericurl", [p for p, _ in sel])
            self.assertIn("h13-afro", [p for p, _ in sel])
            _, sel = regress.parse(["--target", "k6lp-jhericurl"], found=FOUND, env={})
            self.assertEqual(sel, [("k6lp-jhericurl", W20L)])

    def test_bring_up_firmware_of_a_supported_target_only_by_fw(self):
        # W21Z boots on W21P's target but is in bring-up: the target's default is W21P, W21Z only with --fw
        w21z = "03.20.25.01-HE_PJT_W21Z_AFADATAA"
        found = dict(FOUND, **{"k7lp-kisscurl": fws(w21z, W21P)})
        self.assertIn("W21Z", regress.targets.BRING_UP)
        _, sel = regress.parse([], found=found, env={})
        self.assertEqual(dict(sel)["k7lp-kisscurl"], W21P)
        _, sel = regress.parse(["--target", "k7lp-kisscurl"], found=found, env={})
        self.assertEqual(sel, [("k7lp-kisscurl", W21P)])
        _, sel = regress.parse(["--fw", w21z], found=found, env={})
        self.assertEqual(dict(sel)["k7lp-kisscurl"], w21z)
        with self.assertRaises(regress.Usage):
            regress.parse(
                ["--target", "k7lp-kisscurl"],
                found=dict(FOUND, **{"k7lp-kisscurl": fws(w21z)}),
                env={},
            )

    def test_viewer_graphics_only(self):
        a, _ = regress.parse(["--viewer", "--tv"], found=FOUND, env={})
        self.assertTrue(a.viewer)
        with self.assertRaises(regress.Usage):
            regress.parse(["--viewer", "--headless"], found=FOUND, env={})

    def test_unknown_target(self):
        with self.assertRaises(regress.Usage):
            regress.parse(["--target", "h13-beehive"], found=FOUND, env={})

    def test_fw_chosen(self):
        _, sel = regress.parse(["--target", "h13-afro", "--fw", WT1H_OLD], found=FOUND, env={})
        self.assertEqual(sel, [("h13-afro", WT1H_OLD)])
        with self.assertRaises(regress.Usage):
            regress.parse(["--fw", "05.00.00.01-HE_DTV_W99X_AFAAABAA"], found=FOUND, env={})

    def test_missing_or_ambiguous_firmware(self):
        with self.assertRaises(regress.Usage):
            regress.parse([], found={k: v for k, v in FOUND.items() if k != "m14-beehive"}, env={})
        with self.assertRaises(regress.Usage):
            regress.select([], [], FOUND, default_fw=W16R)  # two WT1H, neither the default
        # a target left out needs no firmware
        _, sel = regress.parse(["--target", "h13-afro"], found={"h13-afro": fws(WT1H)}, env={})
        self.assertEqual(sel, [("h13-afro", WT1H)])

    def test_several_firmwares_the_default_board(self):
        """Of several firmwares of a target, the one that boots the chip's default board: K8HP's
        US board, so its global firmware over its Japanese one (model_id.json region ARIB); two
        that both boot it stay ambiguous."""
        us, jp = "04.60.85.01-HE_DTV_W22H_AFADATAA", "04.60.85.01-HE_DTV_W22H_AFADJAAA"

        def model_id(fw):
            return {"region": "ARIB" if os.path.basename(fw) == jp else "GLOBAL"}

        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(regress.paths, "model_id", model_id),
        ):
            for n, pak in ((us, "otncabi-global.pak"), (jp, "otncabi-arib.pak")):
                os.makedirs(os.path.join(d, n))
                open(os.path.join(d, n, pak), "w").close()
            found = {"k8hp-mullet": fws(jp, us, d=d)}
            _, sel = regress.parse(["--target", "k8hp-mullet"], found=found, env={})
            self.assertEqual(sel, [("k8hp-mullet", us)])
            _, sel = regress.parse(["--target", "k8hp-mullet", "--fw", jp], found=found, env={})
            self.assertEqual(sel, [("k8hp-mullet", jp)])
            us2 = "04.60.90.01-HE_DTV_W22H_AFADATAA"
            os.makedirs(os.path.join(d, us2))
            with self.assertRaisesRegex(regress.Usage, "several firmwares for k8hp-mullet"):
                regress.parse(
                    ["--target", "k8hp-mullet"],
                    found={"k8hp-mullet": fws(jp, us, us2, d=d)},
                    env={},
                )

    def test_find_firmwares(self):
        """A firmware is listed under the target it resolves to (paths.firmware_target: the release its
        rootfs names); one that resolves to none is not listed."""
        releases = {
            WT1H: "afro",
            W16R: "dreadlocks",
            W17H: "goldilocks",
            "04.63.25.01-HE_DTV_W20L_AFAAJAAA": "jhericurl",
        }

        def release(d):
            if not os.path.isfile(os.path.join(d, "rootfs.pak")):
                return None, "no rootfs.pak"
            return releases[os.path.basename(d)], "rootfs.pak's etc/starfish-release"

        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(regress.paths, "firmware_release", release),
        ):
            for n in (WT1H, W16R, W17H, "04.63.25.01-HE_DTV_W20L_AFAAJAAA", "notes"):
                os.makedirs(os.path.join(d, "a", n))
                open(os.path.join(d, "a", n, "rootfs.pak"), "w").close()
            os.makedirs(os.path.join(d, "b", W15M))  # no rootfs.pak: not a firmware
            # W17H's rootfs names goldilocks, which MODELS has no W17H row for
            self.assertEqual(
                regress.find_firmwares(
                    [os.path.join(d, "a"), os.path.join(d, "b"), "/nonexistent"]
                ),
                {
                    "h13-afro": fws(WT1H, d=os.path.join(d, "a")),
                    "m2-dreadlocks": fws(W16R, d=os.path.join(d, "a")),
                    "k6lp-jhericurl": fws(W20L, d=os.path.join(d, "a")),
                },
            )


class ArgsTest(unittest.TestCase):
    def test_environment(self):
        a, _ = regress.parse(
            [],
            found=FOUND,
            env={"REGRESS_HOST": "host2", "NET_LINK": "off", "LAYERS": "normal,gl,rootssh"},
        )
        self.assertEqual((a.hosts, a.dest), (["host2"], "beanjar"))
        self.assertEqual(
            a.vm_env,
            {
                "LAYERS": "normal,gl,rootssh",
                "DISPLAY_BACKEND": "dbus",
                "NET_LINK": "off",
                "NET_MODE": "direct",
            },
        )
        a, _ = regress.parse(["--local"], found=FOUND, env={"REGRESS_HOST": "host2"})
        self.assertEqual(a.hosts, [])

    def test_vm_host_is_explicit(self):
        # no built-in host name: neither --host, nor REGRESS_HOST, nor --local is an error
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(regress.Usage) as e:
                _parse([], found=FOUND, env={})
            self.assertIn("--host", str(e.exception))
            self.assertIn("REGRESS_HOST", str(e.exception))
            a, _ = _parse(["--host", "tv-host"], found=FOUND, env={"REGRESS_HOST": "other"})
            self.assertEqual(a.hosts, ["tv-host"])
            a, _ = _parse([], found=FOUND, env={"REGRESS_HOST": "other"})
            self.assertEqual(a.hosts, ["other"])
            a, _ = _parse(["--local"], found=FOUND, env={})
            self.assertEqual(a.hosts, [])
            with self.assertRaises(regress.Usage):
                _parse(["--host", "tv-host", "--local"], found=FOUND, env={})

    def test_several_hosts(self):
        a, _ = _parse(
            ["--host", "pi", "--host", "rock1,rock2", "--host", "pi"], found=FOUND, env={}
        )
        self.assertEqual(a.hosts, ["pi", "rock1", "rock2"])  # in order, once each
        a, _ = _parse([], found=FOUND, env={"REGRESS_HOST": "pi, rock1 rock2"})
        self.assertEqual(a.hosts, ["pi", "rock1", "rock2"])
        a, _ = _parse(["--host", "rock1"], found=FOUND, env={"REGRESS_HOST": "pi,rock2"})
        self.assertEqual(a.hosts, ["rock1"])  # --host wins

    def test_sync_command_is_explicit(self):
        a, _ = _parse([], found=FOUND, env={"REGRESS_HOST": "h"})
        self.assertEqual(a.sync, "")
        a, _ = _parse([], found=FOUND, env={"REGRESS_HOST": "h", "REGRESS_SYNC": "my-sync"})
        self.assertEqual(a.sync, "my-sync")

    def test_headless_environment(self):
        a, _ = regress.parse(["--headless"], found=FOUND, env={})
        self.assertEqual(a.mode, "headless")
        self.assertEqual(
            a.vm_env,
            {
                "LAYERS": "normal,headless",
                "DISPLAY_BACKEND": "none",
                "NET_LINK": "nat",
                "NET_MODE": "direct",
            },
        )
        # instances: -j 2 by default; MEM the target's
        a, _ = regress.parse(
            ["--headless", "-j", "1"], found=FOUND, env={}
        )  # one VM, still an instance
        self.assertEqual(
            a.vm_env,
            {
                "LAYERS": "normal,headless",
                "DISPLAY_BACKEND": "none",
                "NET_LINK": "nat",
                "NET_MODE": "direct",
            },
        )
        a, _ = regress.parse(
            ["--headless", "--local"], found=FOUND, env={"ACCEL": "tcg", "NET_LINK": "off"}
        )
        self.assertEqual(
            a.vm_env,
            {
                "LAYERS": "normal,headless",
                "DISPLAY_BACKEND": "none",
                "ACCEL": "tcg",
                "NET_LINK": "off",
                "NET_MODE": "direct",
            },
        )
        self.assertEqual(regress.parse([], found=FOUND, env={})[0].mode, "graphics")

    def test_start_env_leaves_no_setting_to_the_vm(self):
        """Every launcher setting a lane does not choose is given empty (the launcher's default), so a
        target's development VM cannot bring back an earlier run's (AUDIO=dbus from a graphics run)."""
        a, _ = regress.parse(["--headless"], found=FOUND, env={})
        env = regress.start_env(a, WT1H)
        self.assertEqual(
            {k: env[k] for k in regress.LAUNCHER_DEFAULTED},
            dict.fromkeys(regress.LAUNCHER_DEFAULTED, ""),
        )
        self.assertEqual(
            (env["LAYERS"], env["DISPLAY_BACKEND"], env["NET_LINK"], env["NET_MODE"]),
            ("normal,headless", "none", "nat", "direct"),
        )
        self.assertNotIn("MAC", env)  # the VM's identity is kept
        a, _ = regress.parse(["--headless"], found=FOUND, env={"MEM": "2048", "AUDIO": "none"})
        env = regress.start_env(a, WT1H)
        self.assertEqual((env["MEM"], env["AUDIO"], env["SMP"]), ("2048", "none", ""))

    def test_time_scale(self):
        # the run's deadlines take the launcher's factor for the VM host's accelerator: --local is
        # this machine's (TCG on x86), the Pi KVM, ACCEL as given
        scale = regress.qemu.TIME_SCALE
        self.assertEqual(scale, {"kvm": 1, "tcg": 4})
        for argv, env, accel, timeout in (
            (["--local"], {}, regress.qemu.default_accel(), 300),
            (["--local"], {"ACCEL": "tcg"}, "tcg", 300),
            ([], {}, "kvm", 300),
            (["--timeout", "100"], {"ACCEL": "tcg"}, "tcg", 100),
        ):
            a, _ = regress.parse(argv, found=FOUND, env=env)
            self.assertEqual(
                (a.accel, a.scale, a.timeout), (accel, scale[accel], timeout * scale[accel])
            )

    def test_guest_kernel(self):
        """GUEST_KERNEL passes through to the launcher, given empty when not set (a development VM
        never keeps an earlier run's), and a --local run's deadlines take the accelerator the
        launcher picks for it: KVM for the x86-64 kernel on an x86-64 host that can open /dev/kvm."""
        a, _ = regress.parse(["--local", "--headless"], found=FOUND, env={"GUEST_KERNEL": "x86-64"})
        self.assertEqual(regress.start_env(a, WT1H)["GUEST_KERNEL"], "x86-64")
        a, _ = regress.parse(["--local", "--headless"], found=FOUND, env={})
        self.assertEqual(regress.start_env(a, WT1H)["GUEST_KERNEL"], "")
        with (
            mock.patch.object(regress.qemu.platform, "machine", return_value="x86_64"),
            mock.patch.object(regress.qemu.os, "access", return_value=True),
        ):
            a, _ = regress.parse(["--local"], found=FOUND, env={"GUEST_KERNEL": "x86-64"})
            self.assertEqual((a.accel, a.scale), ("kvm", 1))
            a, _ = regress.parse(["--local"], found=FOUND, env={})
            self.assertEqual((a.accel, a.scale), ("tcg", 4))
            a, _ = regress.parse(
                ["--local"], found=FOUND, env={"GUEST_KERNEL": "x86-64", "ACCEL": "tcg"}
            )
            self.assertEqual((a.accel, a.scale), ("tcg", 4))

    def test_bad_values(self):
        for argv in (
            ["--timeout", "0"],
            ["--settle", "-1"],
            ["--run", "../x"],
            ["--run", "a;touch /tmp/oops"],
        ):
            with self.assertRaises(regress.Usage):
                regress.parse(argv, found=FOUND, env={})


def _git(top, *args):
    return subprocess.run(
        ["git", "-C", top, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class SourceTest(unittest.TestCase):
    """What a run tests: a commit's public or whole tree, or this checkout as it is."""

    def test_tree_arguments(self):
        for argv, tree, pin in (
            ([], "public", "HEAD"),  # over ssh: HEAD's public tree by default
            (["--commit", "abc123"], "public", "abc123"),
            (["--private-tree"], "private", "HEAD"),
            (["--private-tree", "--commit", "abc123"], "private", "abc123"),
            (["--working-tree"], "working-tree", None),
            (["--local"], None, None),
            (["--no-sync"], None, None),
        ):
            a, _ = regress.parse(argv, found=FOUND, env={})
            self.assertEqual((a.tree, a.pin), (tree, pin), argv)
        for argv in (
            ["--commit", "HEAD", "--working-tree"],
            ["--private-tree", "--working-tree"],
            ["--local", "--commit", "HEAD"],
            ["--local", "--private-tree"],
            ["--local", "--working-tree"],
            ["--no-sync", "--commit", "HEAD"],
            ["--no-sync", "--working-tree"],
            ["--commit", ""],
        ):
            with self.assertRaises(regress.Usage, msg=argv):
                regress.parse(argv, found=FOUND, env={})

    def repo(self, machinery=False):
        """A repository with a private path; machinery: with the publishing machinery (this
        checkout's scripts/publish/sync.py and a paths.txt), as the private repository has."""
        top = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", top])
        _git(top, "init", "-q")
        _git(top, "config", "user.email", "t@example.invalid")
        _git(top, "config", "user.name", "t")
        _git(top, "config", "commit.gpgSign", "false")
        tree = {"pub/a.txt": "a\n", "private/notes.md": "n\n"}
        if machinery:
            if not os.path.exists(os.path.join(TOP, "scripts", "publish", "sync.py")):
                self.skipTest("the public repository: no scripts/publish/sync.py")
            with open(os.path.join(TOP, "scripts", "publish", "sync.py")) as f:
                tree["scripts/publish/sync.py"] = f.read()
            tree[".github/publish/paths.txt"] = "pub/**\n"
        for name, text in tree.items():
            os.makedirs(os.path.dirname(os.path.join(top, name)), exist_ok=True)
            with open(os.path.join(top, name), "w") as f:
                f.write(text)
        _git(top, "add", "-A")
        _git(top, "commit", "-q", "-m", "one")
        return top

    def args(self, **kw):
        a = dict(hosts=["h"], no_sync=False, pin="HEAD", tree="public", dry_run=False)
        a.update(kw)
        return argparse.Namespace(**a)

    def test_source_and_dirty(self):
        top = self.repo()
        head = _git(top, "rev-parse", "HEAD")
        with mock.patch.object(regress, "TOP", top):
            src = regress.source(self.args())
            self.assertEqual(
                src,
                {
                    "commit": head,
                    "tree": "public",
                    "pinned": True,
                    "filtered": False,
                    "host_commit": None,
                    "dirty": False,
                    "hosts": ["h"],
                    "rev": "HEAD",
                },
            )
            with open(os.path.join(top, "new.txt"), "w") as f:
                f.write("x")  # untracked: dirty
            src = regress.source(self.args(pin=None, tree="working-tree"))
            self.assertEqual(
                (src["pinned"], src["dirty"], src["tree"]), (False, True, "working-tree")
            )
            self.assertIn("uncommitted changes", regress.describe(src))
            src = regress.source(self.args(hosts=[], pin=None, tree=None))
            self.assertEqual(src["tree"], "checkout")  # --local: this checkout
            src = regress.source(self.args(no_sync=True, pin=None, tree=None))
            self.assertIsNone(src["commit"])
            self.assertIn("unknown", regress.describe(src))
            with self.assertRaises(RuntimeError):
                regress.source(self.args(pin="no-such-rev"))

    def host_commit(self, top, **kw):
        a = self.args(**kw)
        with mock.patch.object(regress, "TOP", top), contextlib.redirect_stdout(io.StringIO()):
            a.source = regress.source(a)
            regress.make_host_commit(a)
        return a.source

    def test_public_tree_is_filtered(self):
        top = self.repo(machinery=True)
        head = _git(top, "rev-parse", "HEAD")
        src = self.host_commit(top)
        f = src["host_commit"]
        self.assertTrue(src["filtered"])
        self.assertEqual(_git(top, "ls-tree", "-r", "--name-only", f), "pub/a.txt")
        self.assertEqual(_git(top, "rev-parse", f"refs/hosts/{head}"), f)
        self.assertEqual(self.host_commit(top)["host_commit"], f)  # the same each time
        self.assertIn("its public tree", regress.describe(src))

    def test_public_repository_sends_the_commit(self):
        # no publishing machinery: every commit there is public
        top = self.repo()
        src = self.host_commit(top)
        self.assertEqual(
            (src["host_commit"], src["filtered"]), (_git(top, "rev-parse", "HEAD"), False)
        )

    def test_private_tree(self):
        top = self.repo(machinery=True)
        head = _git(top, "rev-parse", "HEAD")
        src = self.host_commit(top, tree="private")
        f = src["host_commit"]
        self.assertFalse(src["filtered"])
        self.assertEqual(
            _git(top, "rev-parse", f"{f}^{{tree}}"), _git(top, "rev-parse", "HEAD^{tree}")
        )
        self.assertEqual(_git(top, "log", "-1", "--format=%P", f), "")  # no history
        self.assertEqual(_git(top, "rev-parse", f"refs/hosts/private/{head}"), f)
        self.assertEqual(self.host_commit(top, tree="private")["host_commit"], f)
        self.assertIn("whole tree, private paths included", regress.describe(src))

    def test_dry_run_makes_nothing(self):
        top = self.repo(machinery=True)
        src = self.host_commit(top, dry_run=True)
        self.assertIsNone(src["host_commit"])
        self.assertEqual(_git(top, "for-each-ref", "refs/hosts/"), "")

    def run_dry(self, argv):
        out = io.StringIO()
        real = regress.parse
        regress.parse = lambda argv: real(argv, found=FOUND, env={})
        try:
            with (
                contextlib.redirect_stdout(out),
                mock.patch.object(regress, "machine", return_value="x86_64"),
            ):
                rc = regress.main(["-n", *argv, "--target", "h13-afro"])
        finally:
            regress.parse = real
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_dry_run_public_tree(self):
        text = self.run_dry([])
        head = _git(TOP, "rev-parse", "HEAD")
        DryRunTest.assertInOrder(
            self,
            text,
            [
                "flock -n 9",  # the host's lock first
                "build: guest h13-afro",
                f"test -d beanjar-src.git || git init -q --bare beanjar-src.git",
                f"cat-file -e refs/hosts/{head}^{{commit}}",
                f"push -q vmhost:beanjar-src.git +refs/hosts/{head}:refs/hosts/{head}",
                "git checkout -q -f --detach",
                "git clean -q -ffdx -e /.git/ -e /build/ -e /local/ -e /scratch/",
                "SRC=none HOST=vmhost DEST=beanjar REGRESS_LOCKED=1 sync-checkout",
                "scripts/build.sh host",
                "vm-launcher start",
            ],
        )

    def test_dry_run_working_tree(self):
        text = self.run_dry(["--working-tree"])
        self.assertNotIn("beanjar-src.git", text)
        self.assertIn(f"SRC={TOP} HOST=vmhost DEST=beanjar REGRESS_LOCKED=1 sync-checkout", text)
        self.assertIn("flock -n 9", text)  # held all the same
        self.assertIn("source: commit ", text)

    def test_busy_host(self):
        class P:
            def __init__(self, *args, **kw):
                self.stdout = io.StringIO("BUSY\nscripts/regress run other (pid 1 on pc)\n")
                self.stdin = io.StringIO()

            def wait(self, timeout=None):
                return 1

        with mock.patch.object(regress.subprocess, "Popen", P):
            with self.assertRaisesRegex(
                regress.HostBusy, "held by another run: scripts/regress run other"
            ):
                with regress.HostLock("pi", "beanjar", "r", False):
                    pass

    def test_sync_script(self):
        # the PC-side sync is private: the public repository has none
        if not os.path.exists(os.path.join(TOP, "host", "scripts", "sync-pi.sh")):
            self.skipTest("no host/scripts/sync-pi.sh")
        with open(os.path.join(TOP, "host", "scripts", "sync-pi.sh")) as f:
            text = f.read()
        self.assertIn("SRC=${SRC:-$TOP}", text)
        self.assertIn('"$SRC/" "$PI:$DEST/"', text)
        self.assertIn('if [ "$SRC" != none ]', text)
        self.assertIn("REGRESS_LOCKED", text)


class DryRunTest(unittest.TestCase):
    def setUp(self):
        # what is built and checked depends on the machine (guest builds on x86_64 only), and a
        # --local run's deadlines on its accelerator: an x86 PC's, TCG
        for pin in (
            mock.patch.object(regress, "machine", return_value="x86_64"),
            mock.patch.object(regress.qemu, "default_accel", return_value="tcg"),
        ):
            pin.start()
            self.addCleanup(pin.stop)

    def dry_run(self, argv):
        out = io.StringIO()
        real = regress.parse
        regress.parse = lambda argv: real(argv, found=FOUND, env={})
        try:
            with contextlib.redirect_stdout(out):
                rc = regress.main(argv)
        finally:
            regress.parse = real
        self.assertEqual(rc, 0)
        return out.getvalue()

    def assertInOrder(self, text, order):
        pos = [text.find(s) for s in order]
        self.assertNotIn(-1, pos, text)
        self.assertEqual(pos, sorted(pos), text)

    def test_dry_run(self):
        text = self.dry_run(["-n", "--run", "t1", "--target", "h13-afro,m2-dreadlocks"])
        self.assertFalse(os.path.exists(os.path.join(TOP, "build", "regress", "t1")))
        self.assertInOrder(
            text,
            [
                f"FW={WT1H} scripts/build.sh guest",
                "git checkout -q -f --detach",
                "vm-launcher config FIRMWARE_DIRS",
                "pgrep -ax",
                # the host's own preparation, before its lanes
                "SRC=none HOST=vmhost DEST=beanjar REGRESS_LOCKED=1 sync-checkout",
                "build: host side on vmhost",
                f"FW={WT1H} STATE=none BUILD=0 LAYERS=normal,gl DISPLAY_BACKEND=dbus",
                "TIME_SCALE=1 sh /host/tests/regress/guest.sh boot 300",
                "guest.sh check-m1 regress-t1-h13-afro-check-m1 10",
                "guest.sh app-calls",
                "guest.sh checklist 0 ",
                "ir-key.py",
                "t1/h13-afro.png",
                "guest.sh idle-cpu 30 2",
                "guest.sh standby-prep",
                "ir-key.py 0x08",
                "standby.py wait 60",
                "standby.py cpu 5",
                "standby.py wake 60",
                "guest.sh resumed",
                "t1/h13-afro-resume.png",
                "host/scripts/vm-launcher stop",
                f"FW={W16R} STATE=none",
            ],
        )
        # one sync, and no firmware sent: the Pi boots from its own collections
        self.assertEqual(text.count("sync-checkout"), 1)
        self.assertNotIn("FW=" + WT1H + " HOST=", text)
        self.assertNotIn("scratch/firmware", text)
        self.assertNotIn("m14-beehive", text)
        self.assertNotIn("k5lp-goldilocks2", text)
        self.assertNotIn("guest.sh ready", text)
        self.assertNotIn("guest.sh tvservice", text)
        lines = table_lines(text)
        self.assertEqual(
            lines[-3].split()[:8],
            [
                "target",
                "mode",
                "boot-done",
                "check-m1",
                "app-calls",
                "checklist",
                "ssh",
                "screenshot",
            ],
        )
        self.assertEqual(lines[-1].split()[:2], ["m2-dreadlocks", "graphics"])
        self.assertIn("t1/m2-dreadlocks.png", lines[-1])

    def test_dry_run_several_hosts(self):
        text = self.dry_run(
            [
                "-n",
                "--headless",
                "--run",
                "t9",
                "--host",
                "pi,rock",
                "--target",
                "h13-afro,m2-dreadlocks,m3-goldilocks",
            ]
        )
        # the same commit to each host, each held and checked on its own
        self.assertInOrder(
            text,
            [
                "[pi] exec 9>>beanjar.lock",
                "[rock] exec 9>>beanjar.lock",
                "push -q pi:beanjar-src.git",
                "[pi] mkdir -p beanjar && cd beanjar",
                "push -q rock:beanjar-src.git",
                "[rock] mkdir -p beanjar && cd beanjar",
                "checking that nothing runs on the VM host pi",
                "checking that nothing runs on the VM host rock",
            ],
        )
        # then each host's own preparation, the hosts side by side
        for host in ("pi", "rock"):
            self.assertInOrder(
                text,
                [
                    f"checking that nothing runs on the VM host {host}",
                    f"HOST={host} DEST=beanjar REGRESS_LOCKED=1 sync-checkout",
                    f"build: host side on {host}",
                ],
            )
        pushed = {line.split()[-1] for line in text.splitlines() if " push -q " in line}
        self.assertEqual(len(pushed), 1)  # the same commit for every host
        for host in ("pi", "rock"):
            self.assertIn(f'[{host}] echo "arch $(uname -m)"', text)  # its traits
            self.assertIn(f"firmware: from {host}'s firmware collections", text)
            self.assertIn(f"checking that nothing runs on the VM host {host}", text)
            self.assertIn(f"{host}: 2 VM(s) at once", text)
        lines = table_lines(text)
        self.assertEqual(lines[-4].split()[-2:], ["host", "result"])
        rows = {line.split()[0]: line.split()[-2] for line in lines[-3:]}
        self.assertEqual(set(rows), {"h13-afro", "m2-dreadlocks", "m3-goldilocks"})
        self.assertTrue(set(rows.values()) <= {"pi", "rock"})
        self.assertIn("4 at once (pi 2, rock 2)", text)

    def test_a_host_that_fails_its_preparation(self):
        # rock's preparation fails: pi runs every target; with pi gone too, the targets say why
        real_side = regress.host_side
        real = regress.parse
        regress.parse = lambda argv: real(argv, found=FOUND, env={})
        try:
            for broken, want in (({"rock"}, 0), ({"pi", "rock"}, 4)):  # said, and under "not run:"
                regress.host_side = lambda a, host, files=(), broken=broken: host not in broken
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = regress.main(
                        [
                            "-n",
                            "--headless",
                            "--host",
                            "pi,rock",
                            "--target",
                            "h13-afro,m3-goldilocks",
                        ]
                    )
                text = out.getvalue()
                self.assertEqual(rc, 1)
                self.assertIn("rock: its preparation failed; no target ran there", text)
                self.assertEqual(text.count("got ready (pi, rock failed)"), want, text[-2000:])
                if not want:
                    rows = {line.split()[0]: line.split()[-2] for line in table_lines(text)[-2:]}
                    self.assertEqual(rows, {"h13-afro": "pi", "m3-goldilocks": "pi"})
        finally:
            regress.host_side, regress.parse = real_side, real

    def test_host_without_toolchain(self):
        a, sel = regress.parse(
            ["-n", "--headless", "--host", "pi,rock", "--target", "h13-afro"], found=FOUND, env={}
        )
        a.source = {"commit": "0" * 40, "tree": "public", "host_commit": "f" * 40}
        a.traits = {"pi": CAPABLE, "rock": dict(CAPABLE, toolchain=False)}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(regress.prepare(a, sel))
            for host in ("pi", "rock"):
                self.assertTrue(regress.host_tree(a, host))
                self.assertTrue(regress.host_side(a, host))
        text = out.getvalue()
        self.assertIn("build: host side on pi", text)
        self.assertIn(
            "build: none on rock (no toolchain): its host side is build/publish-arm64's", text
        )
        self.assertNotIn("[rock] scripts/build.sh host", text)
        self.assertIn("HOST=rock DEST=beanjar REGRESS_LOCKED=1 sync-checkout", text)  # still synced
        # the published host side is arm64's: a host of another kind cannot use it
        a.traits["rock"] = dict(CAPABLE, toolchain=False, arch="x86_64")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(regress.prepare(a, sel))
        self.assertIn("rock has no toolchain", out.getvalue())
        self.assertNotIn("sync-checkout", out.getvalue())  # before any sync

    def test_dry_run_skips_targets_without_gl(self):
        real_gl = regress.gl_expected  # every chip has GPU stand-ins now: pretend k5lp has none
        regress.gl_expected = lambda target, top=TOP: target != "k5lp-goldilocks2"
        try:
            text = self.dry_run(["-n", "--run", "t3"])
        finally:
            regress.gl_expected = real_gl
        self.assertIn("k5lp-goldilocks2: SKIP (no gl layer", text)
        self.assertNotIn(f"FW={W19P}", text)  # neither built, synced nor booted
        self.assertIn(f"FW={W18R} STATE=none", text)
        rows = {
            line.split()[0]: line.split()
            for line in table_lines(text)[-len(regress.all_targets()) :]
        }
        self.assertEqual(rows["k5lp-goldilocks2"][-1], "SKIP")
        self.assertEqual(rows["m3-goldilocks"][-1], "PASS")
        # headless, or graphics without the gl layer, runs it
        text = self.dry_run(["-n", "--headless", "--run", "t4", "--target", "k5lp-goldilocks2"])
        self.assertIn(f"FW={W19P} STATE=none", text)
        self.assertNotIn("SKIP (no gl layer", text)
        real = regress.parse
        regress.parse = lambda argv: real(argv, found=FOUND, env={"LAYERS": "normal"})
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    regress.main(["-n", "--run", "t5", "--target", "k5lp-goldilocks2"]), 0
                )
        finally:
            regress.parse = real
        self.assertIn(f"FW={W19P} STATE=none BUILD=0 LAYERS=normal ", out.getvalue())

    def test_dry_run_headless(self):
        text = self.dry_run(
            [
                "-n",
                "--headless",
                "--local",
                "--no-build",
                "--run",
                "t2",
                "--target",
                "m3-goldilocks",
            ]
        )
        self.assertFalse(os.path.exists(os.path.join(TOP, "build", "regress", "t2")))
        self.assertInOrder(
            text,
            [
                "[local] for p in qemu-system-aar",
                f"FW={W18R} STATE=none BUILD=0 LAYERS=normal,headless DISPLAY_BACKEND=none",
                "timeout 1200 host/scripts/vm-launcher start",
                "TIME_SCALE=4 sh /host/tests/regress/guest.sh ready 1200",
                "guest.sh check-m1 regress-t2-m3-goldilocks-check-m1 600",
                "guest.sh app-calls",
                "guest.sh tvservice",
                "guest.sh checklist 0x1bc8 ",
                "guest.sh idle-cpu 30 2",
                "guest.sh standby-prep",
                "guest.sh power-off",
                "standby.py wait 240",
                "standby.py cpu 5",
                "standby.py wake 240",
                "guest.sh resumed",
                " 120 ",
                "timeout 1200 host/scripts/vm-launcher stop",
            ],
        )  # TCG here: x4
        for graphics in (
            "ir-key.py",
            "dbus-grab.py",
            "home-idle",
            ".png",
            "LAYERS=normal,gl",
            "rsync",
        ):
            self.assertNotIn(graphics, text)
        self.assertIn("[regress] m3-goldilocks: ssh", text)
        lines = table_lines(text)
        self.assertEqual(
            lines[-2].split()[:8],
            [
                "target",
                "mode",
                "core-boot-done",
                "check-m1",
                "app-calls",
                "checklist",
                "ssh",
                "tvservice",
            ],
        )
        self.assertEqual(
            lines[-1].split()[:8],
            ["m3-goldilocks", "headless", "dry", "dry", "dry", "dry", "dry", "dry"],
        )

    def test_dry_run_off_captures_are_per_target(self):
        real = regress.parse
        regress.parse = lambda argv: real(argv, found=FOUND, env={"NET_LINK": "off"})
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    regress.main(
                        [
                            "-n",
                            "--local",
                            "--headless",
                            "--no-build",
                            "--run",
                            "capture-dry",
                            "--target",
                            "h13-afro,m3-goldilocks",
                        ]
                    ),
                    0,
                )
        finally:
            regress.parse = real
        text = out.getvalue()
        for target in ("h13-afro", "m3-goldilocks"):
            pcap = f"build/regress/capture-dry/{target}/net.pcap"
            report = f"build/regress/capture-dry/{target}/pcap-lg.txt"
            self.assertIn(f"rm -f {pcap} {report}", text)
            self.assertIn(f"NET_DUMP={pcap} timeout", text)
            self.assertIn(f"pcap-summary.py {pcap} --lg", text)
            self.assertLess(
                text.index(f"NET_DUMP={pcap} timeout"), text.index(f"pcap-summary.py {pcap} --lg")
            )
        self.assertFalse(os.path.exists(os.path.join(TOP, "build/regress/capture-dry")))

    def test_dry_run_ssh_needs_rootssh_and_network(self):
        # the ssh step reaches the guest through the host's forwarded port: only with the rootssh
        # layer and a network (NET_LINK=off forwards none)
        real = regress.parse

        def ssh_run(env):
            regress.parse = lambda argv: real(argv, found=FOUND, env=env)
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(
                        regress.main(
                            [
                                "-n",
                                "--local",
                                "--headless",
                                "--no-build",
                                "--run",
                                "ssh-dry",
                                "--target",
                                "m3-goldilocks",
                            ]
                        ),
                        0,
                    )
            finally:
                regress.parse = real
            return out.getvalue()

        self.assertIn(
            "check-ssh.sh", ssh_run({"NET_MODE": "direct", "LAYERS": "normal,headless,rootssh"})
        )
        self.assertNotIn(
            "check-ssh.sh", ssh_run({"NET_LINK": "off", "LAYERS": "normal,headless,rootssh"})
        )
        self.assertNotIn(
            "check-ssh.sh", ssh_run({"NET_MODE": "direct", "LAYERS": "normal,headless"})
        )

    def test_dry_run_ssh_address_per_instance(self):
        # lanes at once forward root SSH on the stock port of their own addresses (each instance's
        # slot), and each check asks its instance's VM for its address
        real = regress.parse
        regress.parse = lambda argv: real(
            argv, found=FOUND, env={"NET_MODE": "direct", "LAYERS": "normal,headless,rootssh"}
        )
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    regress.main(
                        [
                            "-n",
                            "--local",
                            "--headless",
                            "--no-build",
                            "-j",
                            "2",
                            "--run",
                            "ssh-ports",
                            "--target",
                            "h13-afro,m3-goldilocks",
                        ]
                    ),
                    0,
                )
        finally:
            regress.parse = real
        text = out.getvalue()
        self.assertNotIn("SSH_PORT", text)
        self.assertFalse(hasattr(regress, "ssh_port"))
        for target in ("h13-afro", "m3-goldilocks"):
            self.assertIn(
                f"[local {target}] IDENT=$(host/scripts/vm-launcher config IDENTITY) && "
                "HOST=$(host/scripts/vm-launcher config ADDRESS) && "
                "HOST=$HOST tests/regress/rootssh/check-ssh.sh",
                text,
            )

    def test_remote_capture_checked_before_copy(self):
        real = regress.parse
        regress.parse = lambda argv: real(argv, found=FOUND, env={"NET_LINK": "off"})
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    regress.main(
                        [
                            "-n",
                            "--headless",
                            "-j",
                            "1",
                            "--no-build",
                            "--no-sync",
                            "--run",
                            "remote-capture",
                            "--target",
                            "h13-afro",
                        ]
                    ),
                    0,
                )
        finally:
            regress.parse = real
        text = out.getvalue()
        capture = "build/regress/remote-capture/h13-afro/net.pcap"
        self.assertIn(f"NET_DUMP={capture}", text)
        self.assertIn(f"pcap-summary.py {capture} --lg", text)
        self.assertIn("rsync -a vmhost:beanjar/build/regress/remote-capture/", text)
        self.assertInOrder(
            text,
            [
                "host/scripts/vm-launcher stop >",
                f"pcap-summary.py {capture} --lg",
                "rsync -a vmhost:beanjar/build/regress/remote-capture/",
            ],
        )
        self.assertNotIn(f"NET_DUMP={TOP}", text)

    def test_dry_run_tv(self):
        """--tv: the boot, then the TV suite's luna-info and network (the VM host's server around
        the latter), and nothing of the boot checks; NET_LINK=nat NET_MODE=direct."""
        text = self.dry_run(
            [
                "-n",
                "--tv",
                "--headless",
                "--local",
                "--no-build",
                "--run",
                "t6",
                "--target",
                "m3-goldilocks",
            ]
        )
        self.assertInOrder(
            text,
            [
                "NET_LINK=nat",
                "NET_MODE=direct",
                "timeout 1200 host/scripts/vm-launcher start",
                "tests/regress/tv.sh",
                "guest.sh ready 1200",
                "guest.sh luna-info",
                "host/scripts/vm-launcher netns-exec python3 tests/regress/http-token.py 10.0.2.254 ",
                "getent ahostsv4 example.com",
                "guest.sh network http://10.0.2.254:",
                "kill $(cat build/regress/t6/m3-goldilocks/http-server.pid)",
                "timeout 1200 host/scripts/vm-launcher stop",
            ],
        )
        for boot in ("check-m1", "app-calls", "checklist", "standby", "guest.sh tvservice"):
            self.assertNotIn(boot, text)
        self.assertEqual(
            table_lines(text)[-2].split()[:5],
            ["target", "mode", "core-boot-done", "luna-info", "network"],
        )
        # headless apps: every app, Settings first (none can stay up there: launched, a look after a
        # pause, closed); no keys, screenshot or test app
        self.assertInOrder(
            text,
            [
                "guest.sh network",
                "guest.sh app-launch com.palm.app.settings",
                "headless 0 ",
                "sleep 20",
                "guest.sh app-state headless",
                "guest.sh app-close com.palm.app.settings",
                "guest.sh app-launch com.webos.app.livetv",
                "guest.sh app-close com.webos.app.livetv",
                "guest.sh app-launch com.webos.app.hdmi1",
                "timeout 1200 host/scripts/vm-launcher stop",
            ],
        )
        for graphics in ("ir-key.py", "dbus-grab.py", "ares-package", "app-install"):
            self.assertNotIn(graphics, text)
        text = self.dry_run(
            ["-n", "--tv", "--run", "t7", "--target", "h13-afro", "--no-build", "--no-sync"]
        )
        self.assertInOrder(
            text,
            [
                "make-clips.sh --decode build/regress/t7/tvapp",
                "ares-package -o build/regress/t7/tvapp-pkg build/regress/t7/tvapp",
                "rsync -a build/regress/t7/tvsuite.ipk vmhost:beanjar/build/regress/t7/tvsuite.ipk",
                "LAYERS=normal,gl,devmode",
                "NET_LINK=nat",
                "NET_MODE=direct",
                "guest.sh boot 300",
                "guest.sh luna-info",
                "guest.sh network",
                "guest.sh app-launch com.palm.app.settings '\"'\"'{}'\"'\"' graphics 40",
                "guest.sh app-close com.palm.app.settings 20",
                "ir-key.py 0x7c",
                "guest.sh app-state",
                "ir-key.py 0x41 0x41 0x6 0x7 0x40",
                "dbus-grab.py",
                "guest.sh app-install /mnt/host/out/regress/tvsuite.ipk com.example.tvsuite",
                "h13-afro: decode",
                "guest.sh app-remove com.example.tvsuite",
                "host/scripts/vm-launcher stop",
            ],
        )
        text = self.dry_run(
            [
                "-n",
                "--tv",
                "--run",
                "t8",
                "--target",
                "h13-afro",
                "--no-build",
                "--no-sync",
                "--tv-steps",
                "draw",
            ]
        )
        self.assertNotIn("guest.sh luna-info", text)
        self.assertInOrder(text, ["app-install", "app-remove"])
        with self.assertRaises(regress.Usage):
            regress.parse(["--tv", "--headless", "--tv-steps", "draw"], found=FOUND, env={})

    def test_dry_run_smp_skips_without_the_api(self):
        """smp: SKIP before installing where the expected table says the firmware's library lacks
        what the app looks up (h13-afro); installed and run where it has it (m2-dreadlocks)."""
        text = self.dry_run(
            [
                "-n",
                "--tv",
                "--run",
                "t9",
                "--target",
                "h13-afro",
                "--no-build",
                "--no-sync",
                "--tv-steps",
                "smp",
            ]
        )
        self.assertNotIn("com.example.smpcheck", text)
        self.assertRegex(
            text,
            r"SKIP smp smp.frameready: no StarfishMediaAPIs as the app calls it: "
            r"rootfs.pak:/usr/lib/libplayerAPIs.so.1.0.0 exports no _ZN17StarfishMediaAPIsC1EPKc",
        )
        text = self.dry_run(
            [
                "-n",
                "--tv",
                "--run",
                "t10",
                "--target",
                "m2-dreadlocks",
                "--no-build",
                "--no-sync",
                "--tv-steps",
                "smp",
            ]
        )
        self.assertInOrder(
            text,
            [
                "guest.sh app-install /mnt/host/out/regress/smpcheck.ipk com.example.smpcheck",
                "guest.sh app-remove com.example.smpcheck",
            ],
        )

    def test_tv_needs_direct(self):
        with self.assertRaises(regress.Usage):
            regress.parse(["--tv"], found=FOUND, env={"NET_MODE": "mitm"})
        with self.assertRaises(regress.Usage):
            regress.parse(["--tv"], found=FOUND, env={"NET_LINK": "off"})
        with self.assertRaisesRegex(regress.Usage, "NET_LINK"):
            regress.parse(["--headless"], found=FOUND, env={"NET": "direct"})
        a, _ = regress.parse(["--tv", "--headless"], found=FOUND, env={"NET_MODE": "direct"})
        self.assertEqual((a.vm_env["NET_LINK"], a.vm_env["NET_MODE"]), ("nat", "direct"))


class GlTest(unittest.TestCase):
    def test_targets_with_gl(self):
        # From the chip tables: vendor stand-ins or standard EGL ABI; lm14 and the test guest's
        # virt have no soc layer
        self.assertEqual(
            {p: regress.gl_expected(p) for p in regress.all_targets()},
            {p: p not in ("lm14-netcast45", "virt-testguest") for p in regress.all_targets()},
        )

    def test_table_parsed(self):
        with tempfile.TemporaryDirectory() as d:
            for chip, text in (
                ("a", "gl_vendor_stubs = []\ngl_vendor_gl = ''\n"),
                ("b", "gl_vendor_stubs = ['IMGegl',\n  'srv_um']\ngl_vendor_gl = ''\n"),
                ("c", "gl_vendor_stubs = []\ngl_vendor_gl = 'mali'\n"),
                ("d", "soc = 1\n"),
                ("f", "gl_vendor_stubs = []\ngl_vendor_gl = ''\ngl_standard_abi = true\n"),
            ):
                os.makedirs(os.path.join(d, "guest", "layers", "soc", chip))
                with open(os.path.join(d, "guest", "layers", "soc", chip, "meson.build"), "w") as f:
                    f.write(text)
            self.assertEqual(
                [regress.gl_expected(c + "-x", d) for c in "abcf"], [False, True, True, True]
            )
            # a chip whose file does not say takes its base's value
            os.makedirs(os.path.join(d, "guest", "layers", "soc", "e"))
            with open(os.path.join(d, "guest", "layers", "soc", "e", "meson.build"), "w") as f:
                f.write("gl_vendor_gl = ''\n")
            with open(os.path.join(d, "guest", "layers", "soc", "e", "base"), "w") as f:
                f.write("b\n")
            self.assertTrue(regress.gl_expected("e-x", d))
            with self.assertRaises(ValueError):
                regress.gl_expected("d-x", d)

    def test_missing_gl_layer_fails(self):
        # a target that should have a gl layer and has none is not built: the run stops before booting
        # (the layers resolve as the manager's do: build/components/<component>/selections.json)
        with tempfile.TemporaryDirectory() as d:
            v = "0" * 64
            for f in (
                "build/Image",
                "build/initramfs.cpio.gz",
                f"build/components/gl/variants/{v}/x",
                f"build/components/soc/m2/variants/{v}/x",
            ):
                os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
                open(os.path.join(d, f), "w").close()
            for comp, target in (("gl", "h13-afro"), ("soc/m2", "m2-dreadlocks")):
                with open(
                    os.path.join(d, "build", "components", comp, "selections.json"), "w"
                ) as f:
                    json.dump({target: v}, f)
            real = regress.TOP
            regress.TOP = d
            try:
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    a, sel = regress.parse(
                        ["--no-build", "--no-sync", "--target", "h13-afro"], found=FOUND, env={}
                    )
                    self.assertTrue(regress.prepare(a, sel))
                    a, sel = regress.parse(
                        ["--no-build", "--no-sync", "--target", "h13-afro,m2-dreadlocks"],
                        found=FOUND,
                        env={},
                    )
                    self.assertFalse(regress.prepare(a, sel))
            finally:
                regress.TOP = real
            self.assertIn("not built: build/components/gl (m2-dreadlocks)", out.getvalue())


class IdentityTest(unittest.TestCase):
    def test_vm_identity(self):
        """The started VM's MAC, serial and identity directory, as its launcher prints them."""

        class H:
            def run(self, cmd, timeout):
                self.cmd = cmd
                return (
                    0,
                    "MAC=02:00:00:00:00:01\nSERIAL=000TEST00001\nIDENTITY=/lib/vms/dev-1/identity\n",
                )

        h, a = H(), argparse.Namespace(dry_run=False)
        self.assertEqual(
            regress.vm_identity(h, a),
            {
                "MAC": "02:00:00:00:00:01",
                "SERIAL": "000TEST00001",
                "IDENTITY": "/lib/vms/dev-1/identity",
            },
        )
        self.assertIn("vm-launcher config $k", h.cmd)
        H.run = lambda self, cmd, timeout: (
            0,
            "MAC=\nSERIAL=\nIDENTITY=/x\n",
        )  # no VM config: the board's
        self.assertEqual(regress.vm_identity(H(), a), {"IDENTITY": "/x"})
        self.assertEqual(regress.vm_identity(H(), argparse.Namespace(dry_run=True)), {})


class HostFirmwareTest(unittest.TestCase):
    """The VM host's firmware: looked up by name in its collections, never copied there."""

    def test_lookup_on_the_host(self):
        # the lookup's own shell command, against a VM host checkout whose launcher lists two collections
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "srv"), os.path.join(d, "other")
            for x in (os.path.join(a, WT1H), os.path.join(b, WT1H), os.path.join(b, W16R)):
                os.makedirs(x)
            os.makedirs(os.path.join(d, "host", "scripts"))
            launcher = os.path.join(d, "host", "scripts", "vm-launcher")
            with open(launcher, "w") as f:
                f.write(
                    f'#!/bin/sh\n[ "$1 $2" = "config FIRMWARE_DIRS" ] && printf "{a}\\n{b}\\n"\n'
                )
            os.chmod(launcher, 0o755)

            class H:
                def run(self, cmd, timeout):
                    r = subprocess.run(["sh", "-c", cmd], cwd=d, capture_output=True, text=True)
                    return r.returncode, r.stdout + r.stderr

            dirs, found = regress.host_firmwares(H(), [WT1H, W16R, W18R])
            self.assertEqual(dirs, [a, b])
            self.assertEqual(found, {WT1H: os.path.join(a, WT1H), W16R: os.path.join(b, W16R)})
            os.remove(launcher)
            with self.assertRaises(RuntimeError):
                regress.host_firmwares(H(), [WT1H])

    def test_missing_firmware_fails_the_target(self):
        class H:
            def __init__(self, *args, **kw):
                pass

            def run(self, cmd, timeout):
                return 0, f"dir /srv/beanjar/firmware\nfw {WT1H} /srv/beanjar/firmware/{WT1H}\n"

        a, sel = regress.parse(["--target", "h13-afro,m2-dreadlocks"], found=FOUND, env={})
        a.traits = {"vmhost": CAPABLE}
        with mock.patch.object(regress, "Host", H), contextlib.redirect_stdout(io.StringIO()):
            where, missing = regress.placement(a, sel)
        self.assertEqual(where, {"h13-afro": ["vmhost"], "m2-dreadlocks": []})
        self.assertEqual(list(missing), ["m2-dreadlocks"])
        r = missing["m2-dreadlocks"]
        self.assertFalse(r.ok)
        self.assertEqual(r.row()[-1], "FAIL")
        why = f"firmware {W16R} not in the host's firmware collections (/srv/beanjar/firmware)"
        self.assertEqual(r.failed, why)
        self.assertIn(f"not run:\n  m2-dreadlocks: {why}", regress.table([r]))
        # --local: the firmware was chosen from this machine's collections
        a, sel = regress.parse(["--local", "--target", "m2-dreadlocks"], found=FOUND, env={})
        a.traits = {None: CAPABLE}
        self.assertEqual(regress.placement(a, sel), ({"m2-dreadlocks": [None]}, {}))

    def test_placement_across_hosts(self):
        # pi has both firmwares and a render node; rock has only WT1H's and no render node
        class H:
            def __init__(self, host, *args, **kw):
                self.host = host

            def run(self, cmd, timeout):
                fws = (WT1H, W16R) if self.host == "pi" else (WT1H,)
                return 0, "dir /srv/fw\n" + "".join(f"fw {n} /srv/fw/{n}\n" for n in fws)

        traits = {"pi": CAPABLE, "rock": dict(CAPABLE, render=False, desktop=False)}
        argv = ["--host", "pi,rock", "--target", "h13-afro,m2-dreadlocks"]
        for extra, want in (
            (["--headless"], {"h13-afro": ["pi", "rock"], "m2-dreadlocks": ["pi"]}),
            ([], {"h13-afro": ["pi"], "m2-dreadlocks": ["pi"]}),  # graphics: the render node
        ):
            a, sel = regress.parse(argv + extra, found=FOUND, env={})
            a.traits = traits
            with mock.patch.object(regress, "Host", H), contextlib.redirect_stdout(io.StringIO()):
                where, missing = regress.placement(a, sel)
            self.assertEqual((where, missing), (want, {}), extra)
        # none can: each host's reason
        traits["pi"] = dict(CAPABLE, render=False)
        a, sel = regress.parse(argv, found=FOUND, env={})
        a.traits = traits
        with mock.patch.object(regress, "Host", H), contextlib.redirect_stdout(io.StringIO()):
            where, missing = regress.placement(a, sel)
        self.assertEqual(
            missing["m2-dreadlocks"].failed,
            "pi: graphics needs the render node /dev/dri/renderD128, which the host has not; "
            f"rock: firmware {W16R} not in the host's firmware collections (/srv/fw)",
        )
        # --viewer: a desktop too
        traits["pi"] = dict(CAPABLE, desktop=False)
        a, sel = regress.parse(argv + ["--viewer"], found=FOUND, env={})
        a.traits = traits
        with mock.patch.object(regress, "Host", H), contextlib.redirect_stdout(io.StringIO()):
            _, missing = regress.placement(a, sel)
        self.assertIn("--viewer needs a desktop", missing["h13-afro"].failed)

    def test_fit_ram(self):
        gb = 1 << 20  # kB
        where = {"big": ["pi", "rock"], "small": ["pi", "rock"], "huge": ["pi", "rock"]}
        ram = {"big": 2560, "small": 1536, "huge": 8192}
        capacity = {"pi": (4, 6 * gb), "rock": (6, 3 * gb)}  # rock: 3 GB less 1 GB of room
        with contextlib.redirect_stdout(io.StringIO()) as out:
            regress.fit_ram(where, ram, capacity)
        self.assertEqual(where, {"big": ["pi"], "small": ["pi", "rock"], "huge": ["pi", "rock"]})
        self.assertIn("big: on pi only", out.getvalue())

    def test_host_traits(self):
        class H:
            host, dry = "board", False

            def __init__(self, rc, out):
                self.r = (rc, out)

            def run(self, cmd, timeout):
                return self.r

        self.assertEqual(
            regress.host_traits(H(0, "arch aarch64\nrender\n")),
            {"arch": "aarch64", "toolchain": False, "render": True, "desktop": False},
        )
        self.assertEqual(
            regress.host_traits(H(0, "arch aarch64\ntoolchain\nrender\ndesktop\n")), CAPABLE
        )
        with self.assertRaises(RuntimeError):
            regress.host_traits(H(255, "ssh: no route"))

    def test_traits_probe_runs(self):
        # the probe's own shell: it says the arch, and nothing it cannot back up
        r = subprocess.run(
            ["sh", "-c", regress.TRAITS], capture_output=True, text=True, check=False
        )
        self.assertEqual(r.returncode, 0)
        lines = r.stdout.split("\n")
        self.assertEqual(lines[0], f"arch {os.uname().machine}")
        self.assertTrue(set(lines[1:]) <= {"toolchain", "render", "desktop", ""}, r.stdout)


class ResultTest(unittest.TestCase):
    def test_skipped(self):
        r = regress.Result("k5lp-goldilocks2", W19P)
        r.skipped = "no gl layer"
        self.assertTrue(r.ok)
        self.assertEqual(r.row()[-1], "SKIP")

    def test_headless_steps(self):
        r = regress.Result("m3-goldilocks", W18R, "headless")
        self.assertEqual(
            tuple(r.steps),
            (
                "start",
                "ready",
                "check-m1",
                "app-calls",
                "tvservice",
                "checklist",
                "ssh",
                "idle-cpu",
                "standby",
                "stop",
                "capture",
            ),
        )
        r.steps.update(start="PASS", ready="FAIL")  # a failed ready: the rest never ran
        self.assertFalse(r.ok)
        r.steps.update({s: "PASS" for s in r.steps})
        self.assertTrue(r.ok)
        self.assertEqual(regress.Result("h13-afro", WT1H).steps["shot"], "-")

    def test_tv_steps(self):
        self.assertEqual(
            tuple(regress.Result("k7lp-kisscurl", W21P, tv=True).steps),
            (
                "start",
                "boot",
                "luna-info",
                "network",
                "apps",
                "tee",
                "draw",
                "audio",
                "decode",
                "smp",
                "stop",
            ),
        )
        r = regress.Result("k7lp-kisscurl", W21P, "headless", tv=True)
        self.assertEqual(
            tuple(r.steps),
            (
                "start",
                "ready",
                "luna-info",
                "network",
                "apps",
                "tee",
                "audio",
                "decode",
                "smp",
                "stop",
            ),
        )
        r.steps.update({s: "PASS" for s in r.steps})
        r.items = {
            "luna-info": [("a", "PASS", ""), ("b", "SKIP", "absent")],
            "network": [("c", "PASS", "")],
        }
        self.assertTrue(r.ok)
        self.assertEqual(r.row()[3:5], ["PASS 1/0/1", "PASS 1/0/0"])
        r.items["network"].append(("d", "FAIL", "no lease"))
        r.steps["network"] = "FAIL"
        self.assertFalse(r.ok)
        text = regress.table([r], "headless", tv=True)
        self.assertIn("k7lp-kisscurl: FAIL network d: no lease", text)

    def test_checklist(self):
        # a checklist WARN keeps the target passing, a FAIL fails it; the table lists what is not PASS
        r = regress.Result("h13-afro", WT1H, "headless")
        r.steps.update({s: "PASS" for s in r.steps})
        r.checklist = [("first-app", "PASS", "ok"), ("tvservice", "WARN", "1 crash report(s): x")]
        r.steps["checklist"] = "WARN"
        self.assertTrue(r.ok)
        text = regress.table([r], "headless")
        self.assertIn("WARN(tvservice)", text.splitlines()[1])
        self.assertIn("h13-afro: WARN tvservice: 1 crash report(s): x", text)
        r.steps["checklist"] = "FAIL"
        self.assertFalse(r.ok)

    def test_idle_cpu(self):
        # a flag WARNs and keeps the target passing; the cell names the process, the notes the numbers
        r = regress.Result("k25lp-queue", W18R, "headless")
        r.steps.update({s: "PASS" for s in r.steps})
        flagged = dict(pid=400, comm="legacy-broadcas", cpu=[86.0, 85.0], why="86-85 %", threads=[])
        r.idle = dict(windows=[30.0, 30.0], processes=[flagged], flagged=[flagged])
        r.idle_item = ("WARN", "legacy-broadcas 86/85 %: 85-86 % of a core (limit 25) [30+30 s]")
        r.steps["idle-cpu"] = "WARN"
        self.assertTrue(r.ok)
        text = regress.table([r], "headless")
        self.assertIn("idle cpu", text.splitlines()[0])
        self.assertIn("WARN(legacy-broadcas)", text.splitlines()[1])
        self.assertIn("k25lp-queue: WARN legacy-broadcas 86/85 %", text)
        quiet = dict(pid=200, comm="tvservice", cpu=[3.0, 2.0], limit=25.0)
        r.idle = dict(windows=[30.0, 30.0], processes=[quiet], flagged=[])
        r.steps["idle-cpu"] = "PASS"
        self.assertIn("PASS tvservice:2%", regress.table([r], "headless").splitlines()[1])
        r.steps["idle-cpu"] = "FAIL"  # the sampler did not finish
        self.assertFalse(r.ok)

    def test_checklist_args(self):
        # the Last_Input_info DB's offset where the release's table has one; the line from the paks
        self.assertEqual(regress.checklist_args("h13-afro", "nonexistent-fw"), ("0", "dvb"))
        self.assertEqual(
            regress.checklist_args("m2-dreadlocks", "nonexistent-fw"), ("0x1b48", "dvb")
        )


class JobsTest(unittest.TestCase):
    def test_jobs_arguments(self):
        a, _ = regress.parse(["--local", "--headless"], found=FOUND, env={})
        self.assertEqual((a.instances, a.jobs), (True, regress.JOBS))
        self.assertEqual(regress.JOBS, 2)
        self.assertEqual(
            (a.vm_env["NET_LINK"], a.vm_env["NET_MODE"]), ("nat", "direct")
        )  # private slirp per VM
        self.assertNotIn("MEM", a.vm_env)  # each target's model's
        a, _ = regress.parse(
            ["--local", "--headless", "-j", "6"],
            found=FOUND,
            env={"MEM": "2048", "NET_LINK": "off"},
        )
        self.assertEqual((a.jobs, a.vm_env["MEM"], a.vm_env["NET_LINK"]), (6, "2048", "off"))
        a, _ = regress.parse(
            ["--local", "--headless", "-j", "1"], found=FOUND, env={"NET_MODE": "mitm"}
        )
        self.assertEqual((a.jobs, a.vm_env["NET_MODE"]), (1, "mitm"))
        # NET_MODE=mitm side by side: each instance's inspector is on its own address
        a, _ = regress.parse(
            ["--local", "--headless", "-j", "2"], found=FOUND, env={"NET_MODE": "mitm"}
        )
        self.assertEqual((a.jobs, a.vm_env["NET_MODE"]), (2, "mitm"))
        a, _ = regress.parse(["--headless", "-j", "2"], found=FOUND, env={"NET_MODE": "mitm"})
        self.assertEqual((a.jobs, a.vm_env["NET_MODE"]), (2, "mitm"))
        a, _ = regress.parse(["--headless"], found=FOUND, env={})  # the Pi: two at once
        self.assertEqual((a.instances, a.jobs), (True, regress.PI_JOBS))
        self.assertEqual(regress.PI_JOBS, 2)
        # graphics: one at a time, each target in its own instance (a development VM keeps its firmware)
        a, _ = regress.parse([], found=FOUND, env={})
        self.assertEqual((a.instances, a.jobs, a.vm_env["NET_MODE"]), (True, 1, "direct"))
        self.assertNotIn("MEM", a.vm_env)
        a, _ = regress.parse(["--headless", "-j", "1"], found=FOUND, env={})
        self.assertEqual((a.instances, a.jobs), (True, 1))
        a, _ = regress.parse(
            ["--headless", "-j", "2"], found=FOUND, env={"SMP": "2"}
        )  # the Pi, two at once
        self.assertEqual(
            (a.instances, a.jobs, a.vm_env["NET_MODE"], a.vm_env["SMP"]), (True, 2, "direct", "2")
        )
        for argv, env in (
            (["-j", "2"], {}),
            (["--local", "-j", "2"], {}),
            (["--headless", "-j", "0"], {}),
            (["--local", "--headless", "-j", "0"], {}),
        ):
            with self.assertRaises(regress.Usage, msg=argv):
                regress.parse(argv, found=FOUND, env=env)

    def test_cap(self):
        gb = 1024 * 1024
        self.assertEqual(regress.cap_jobs(4, 6, 24, 24 * gb, 1536), (4, ""))
        self.assertEqual(regress.cap_jobs(8, 6, 24, 24 * gb, 1536)[0], 6)  # the targets
        n, why = regress.cap_jobs(8, 8, 24, 24 * gb, 1536)
        self.assertEqual(n, 6)  # 24 CPUs / 4 vCPUs
        self.assertIn("24 CPUs", why)
        n, why = regress.cap_jobs(6, 6, 24, 4 * gb, 1536)
        self.assertEqual(n, 2)  # 4 GB / 1.5 GB
        self.assertIn("MemAvailable", why)
        self.assertEqual(regress.cap_jobs(6, 6, 24, 4 * gb, 1024)[0], 3)  # 3 GB after the headroom
        self.assertEqual(regress.cap_jobs(6, 6, 8, 24 * gb, 1536, smp=2)[0], 4)
        self.assertEqual(regress.cap_jobs(4, 6, 2, 1, 1536)[0], 1)  # never none

    def test_cap_sum_of_ram(self):
        gb = 1024 * 1024
        # the Pi's six-kvm-1: 4.5 GB free, one 1.5 GB guest among 1 GB ones: the largest running together
        mems = [1536, 1024, 1024, 1024, 1024, 1024]
        self.assertEqual(
            regress.cap_jobs(6, 6, 24, 4567 * 1024, mems)[0], 2
        )  # 1536+1024 <= 3543, +1024 is not
        n, why = regress.cap_jobs(6, 6, 24, 4567 * 1024, mems)
        self.assertIn("headroom", why)
        self.assertEqual(
            regress.cap_jobs(2, 6, 24, 3 * gb, [1536, 1536])[0], 1
        )  # 3 GB - 1 GB = 2 GB
        self.assertEqual(regress.cap_jobs(2, 6, 24, 3 * gb, [1536, 1536], headroom_mb=0)[0], 2)
        self.assertEqual(regress.cap_jobs(2, 6, 24, 0, [1536, 1536])[0], 1)  # never none
        self.assertEqual(
            regress.cap_jobs(3, 6, 24, 3584 * 1024, [512, 512, 2048])[0], 2
        )  # 2560 left: 2048+512, +512 no

    def test_remote_instance_paths(self):
        # on the Pi the instance's directories are under its own checkout, not this one's
        argv = regress.Host("pi", "dest", False, regress.Instance("h13-afro")).argv("true")[2]
        self.assertIn('RUN_DIR="$PWD"/build/instances/h13-afro', argv)
        self.assertNotIn(regress.TOP, argv)

    def test_interleave(self):
        items = [
            (p, "fw")
            for p in (
                "h13-afro",
                "k5lp-goldilocks2",
                "m14-beehive",
                "m16p-dreadlocks2",
                "m2-dreadlocks",
                "m3-goldilocks",
            )
        ]
        self.assertEqual(
            [p for p, _ in regress.interleave(items)],
            [
                "h13-afro",
                "k5lp-goldilocks2",
                "m16p-dreadlocks2",
                "m14-beehive",
                "m3-goldilocks",
                "m2-dreadlocks",
            ],
        )
        self.assertEqual(
            regress.interleave([("m2-dreadlocks", "x"), ("m14-beehive", "x")]),
            [("m2-dreadlocks", "x"), ("m14-beehive", "x")],
        )
        self.assertEqual(regress.interleave([]), [])

    def test_host_capacity(self):
        class H:
            host = "pi"

            def __init__(self, rc, out):
                self.r = (rc, out)

            def run(self, cmd, timeout):
                return self.r

        self.assertEqual(
            regress.host_capacity(H(0, "4\nMemAvailable:    5000000 kB\n")), (4, 5000000)
        )
        with self.assertRaises(RuntimeError):
            regress.host_capacity(H(255, "ssh: no route"))
        n, _ = regress.cap_jobs(2, 6, 4, 5000000, 1536, smp=4)  # the Pi: 4 CPUs / SMP 4
        self.assertEqual(n, 1)
        self.assertEqual(
            regress.cap_jobs(3, 6, 4, 5000000, 1536, smp=4, oversub=2)[0], 2
        )  # KVM: 4 x 2 / 4
        self.assertEqual(regress.cap_jobs(4, 6, 4, 9000000, 1536, smp=2, oversub=2)[0], 4)
        self.assertEqual(
            regress.cap_jobs(4, 6, 4, 5000000, 1536, smp=2, oversub=2)[0], 2
        )  # (4.8 GB - 1 GB) / 1.5 GB

    def test_meminfo(self):
        with tempfile.NamedTemporaryFile("w") as f:
            f.write("MemTotal:       32645000 kB\nMemAvailable:   25000000 kB\n")
            f.flush()
            self.assertEqual(regress.meminfo_kb(path=f.name), 25000000)
            with self.assertRaises(ValueError):
                regress.meminfo_kb("Nope", f.name)


class ScheduleTest(unittest.TestCase):
    def run_items(self, items, jobs, stop=lambda r: False, delay=0.05):
        lock, live, peak, started = threading.Lock(), [0], [0], []

        def work(x):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
                started.append(x)
            time.sleep(delay(x) if callable(delay) else delay)
            with lock:
                live[0] -= 1
            return x * 10

        return regress.schedule(items, jobs, work, stop), peak[0], started

    def test_at_most_jobs_in_order(self):
        out, peak, started = self.run_items(list(range(6)), 4)
        self.assertEqual(out, [(i, i * 10) for i in range(6)])  # the items' order
        self.assertEqual(peak, 4)
        self.assertEqual(started, list(range(6)))  # started in order
        out, peak, _ = self.run_items(list(range(3)), 1)
        self.assertEqual((len(out), peak), (3, 1))
        out, peak, _ = self.run_items([1, 2], 8)
        self.assertEqual((len(out), peak), (2, 2))
        self.assertEqual(regress.schedule([], 4, lambda x: x), [])

    def test_stop_starts_no_more(self):
        out, _, started = self.run_items(list(range(6)), 1, stop=lambda r: r == 20)
        self.assertEqual(out, [(0, 0), (1, 10), (2, 20)])
        self.assertEqual(started, [0, 1, 2])
        # the ones already running finish; nothing new starts after the stop. Coordinated, not
        # timed: all three jobs start (the barrier) before item 0 ends and asks for the stop, and
        # items 1 and 2 end only once that stop has been seen (stop() runs under the scheduler's
        # lock, with which it records the halt), so the free workers find it halted.
        started, lock = [], threading.Lock()
        all_started, stop_seen = threading.Barrier(3, timeout=30), threading.Event()

        def work(x):
            with lock:
                started.append(x)
            all_started.wait()
            if x != 0:
                self.assertTrue(stop_seen.wait(30), "item 0's stop was never seen")
            return x * 10

        def stop(r):
            if r == 0:
                stop_seen.set()
                return True
            return False

        out = regress.schedule(list(range(8)), 3, work, stop)
        self.assertEqual(sorted(started), [0, 1, 2])
        self.assertEqual(out, [(0, 0), (1, 10), (2, 20)])

    def test_across_hosts(self):
        # each host at most its own slots at once, each item only on a host that can run it
        lock, live, peak, ran = threading.Lock(), {}, {}, {}

        def work(x, host):
            with lock:
                live[host] = live.get(host, 0) + 1
                peak[host] = max(peak.get(host, 0), live[host])
                ran[x] = host
            time.sleep(0.05)
            with lock:
                live[host] -= 1
            return x * 10

        def can(x, host):
            return host == "pi" if x % 3 == 0 else True  # every third: the big host only

        out = regress.schedule_hosts(
            list(range(9)), [("pi", 2), ("rock1", 1), ("rock2", 1)], work, can
        )
        self.assertEqual(out, [(i, i * 10) for i in range(9)])
        self.assertEqual({x for x, h in ran.items() if h != "pi"} & {0, 3, 6}, set())
        self.assertLessEqual(peak["pi"], 2)
        self.assertLessEqual(peak.get("rock1", 0), 1)
        self.assertGreater(len(set(ran.values())), 1)  # spread
        # an item no host can run is left out
        out = regress.schedule_hosts([1, 2], [("a", 1)], work, lambda x, h: x == 1)
        self.assertEqual(out, [(1, 10)])

    def test_hosts_start_when_each_is_ready(self):
        # "fast" is ready at once; "slow" only after fast has run its first items; "broken" never.
        # A slow host holds back only itself, and a failed one runs nothing: its items go to the
        # others where they can run them, and an item only it could run is left out.
        lock, ran, order = threading.Lock(), {}, []
        slow_ready = threading.Event()

        def work(x, host):
            with lock:
                ran[x] = host
                order.append((x, host))
                if len([1 for _x, h in order if h == "fast"]) == 2:
                    slow_ready.set()  # the slow host's sync ends while the fast one is at work
            time.sleep(0.05)
            return x

        ready = {
            "fast": lambda: True,
            "slow": lambda: slow_ready.wait(10) and True,
            "broken": lambda: False,
        }
        out = regress.schedule_hosts(
            list(range(8)),
            [("fast", 1), ("slow", 1), ("broken", 2)],
            work,
            can=lambda x, h: (h == "broken") if x == 7 else True,
            ready=ready,
        )
        self.assertEqual([x for x, _ in out], list(range(7)))  # 7: only the broken host could
        self.assertEqual(order[:2], [(0, "fast"), (1, "fast")])  # before slow was ready
        self.assertIn("slow", ran.values())  # and then side by side
        self.assertNotIn("broken", ran.values())


class InstanceTest(unittest.TestCase):
    def test_launcher_env(self):
        launch = regress.launch
        e = launch.instance_env({"X": "1"})
        self.assertEqual(e["X"], "1")
        self.assertEqual(e["RUN_DIR"], launch.instance_env({"INSTANCE": "default"})["RUN_DIR"])
        self.assertNotEqual(e["RUN_DIR"], os.path.join(TOP, "build"))
        other = launch.instance_env({"INSTANCE": "m3-goldilocks"})
        self.assertNotEqual(e["IDENTITY_DIR"], other["IDENTITY_DIR"])
        self.assertEqual(os.path.basename(other["RUN_DIR"]), "run")
        self.assertEqual(os.path.basename(other["LOG_DIR"]), "logs")
        e = launch.instance_env(
            {"INSTANCE": "a1", "SAVED_LOG_DIR": "/x", "UNIT": "u"}
        )  # given ones stay
        self.assertEqual((e["SAVED_LOG_DIR"], e["UNIT"]), ("/x", "u"))
        for bad in ("A", "-a", "a/b", "../x", "a" * 41, "a b"):
            with self.assertRaises(launch.Error, msg=bad):
                launch.instance_env({"INSTANCE": bad})
        # two instances share nothing
        a, b = (launch.instance_env({"INSTANCE": i}) for i in ("p1", "p2"))
        self.assertFalse({a[k] for k in ("RUN_DIR", "UNIT")} & {b[k] for k in ("RUN_DIR", "UNIT")})

    def test_regress_instance(self):
        i = regress.Instance("h13-afro")
        self.assertEqual(
            (i.run_dir, i.log_dir, i.unit),
            ("build/instances/h13-afro", "build/instances/h13-afro", "beanjar-vm-regress-h13-afro"),
        )
        self.assertEqual(i.guest_out, "build/instances/h13-afro/share/out/regress")
        self.assertEqual(i.env["INSTANCE"], "h13-afro")
        self.assertEqual(i.env["RUN_DIR"], os.path.join(TOP, "build", "instances", "h13-afro"))
        d = regress.Instance()
        self.assertEqual(
            (d.env, d.run_dir, d.unit, d.guest_out),
            (
                {"RUN_DIR": "build", "LOG_DIR": "build", "UNIT": "beanjar-vm-regress-default"},
                "build",
                "beanjar-vm-regress-default",
                "build/share/out/regress",
            ),
        )
        # its leftover check is the regress harness's units, not another library's VM on the host
        self.assertIn("'beanjar-vm-regress-*'", d.orphans)
        self.assertEqual(regress.vmunits.unit_slice(i.unit), "beanjar-regress.slice")

    def test_orphans_of_instance(self):
        # a process whose command line names the instance's RUN_DIR is its leftover; another's is not
        i = regress.Instance("orphan-test")
        h = regress.Host(None, "dest", False, i)
        nap = [sys.executable, "-c", "import time; time.sleep(30)"]
        mine = subprocess.Popen(nap + [os.path.join(TOP, i.run_dir) + "/qemu.pid"])
        other = subprocess.Popen(nap + [os.path.join(TOP, "build/instances/other") + "/qemu.pid"])
        try:
            _, left = h.run(i.orphans, 30)
            self.assertIn(f"{mine.pid} ", left)
            self.assertNotIn(str(other.pid), left)
        finally:
            for p in (mine, other):
                p.kill()
                p.wait()
        self.assertEqual(h.run(i.orphans, 30), (0, ""))

    def test_orphans_on_the_vm_host(self):
        # the check runs on the VM host, against that host's checkout: the same-named instance of
        # another checkout (this PC's TCG lane beside the Pi's) is not this run's leftover
        i = regress.Instance("orphan-test")
        argv = regress.Host("pi", "dest", False, i).argv(i.orphans)
        self.assertEqual(argv[:2], ["ssh", "pi"])
        self.assertNotIn(regress.TOP, argv[2])
        self.assertIn("beanjar-vm-regress-orphan-test.service", argv[2])
        with tempfile.TemporaryDirectory() as there:
            i.env = dict(i.env, RUN_DIR=os.path.join(there, i.run_dir))  # the VM host's checkout
            nap = [sys.executable, "-c", "import time; time.sleep(30)"]
            here = subprocess.Popen(nap + [os.path.join(TOP, i.run_dir) + "/qemu.pid"])
            mine = subprocess.Popen(nap + [os.path.join(there, i.run_dir) + "/qemu.pid"])
            try:
                _, left = regress.Host(None, "dest", False, i).run(i.orphans, 30)
                self.assertIn(f"{mine.pid} ", left)
                self.assertNotIn(str(here.pid), left)
            finally:
                for p in (here, mine):
                    p.kill()
                    p.wait()

    def test_dry_run_parallel(self):
        out = io.StringIO()
        real = regress.parse
        regress.parse = lambda argv: real(argv, found=FOUND, env={})
        try:
            # a --local run's capacity and accelerator are this machine's: pinned to an x86 PC's
            # with room for both VMs
            with (
                contextlib.redirect_stdout(out),
                mock.patch.object(regress, "machine", return_value="x86_64"),
                mock.patch.object(regress, "host_capacity", return_value=(16, 32 << 20)),
                mock.patch.object(regress.qemu, "default_accel", return_value="tcg"),
            ):
                rc = regress.main(
                    [
                        "-n",
                        "--local",
                        "--headless",
                        "--no-build",
                        "--run",
                        "t6",
                        "-j",
                        "2",
                        "--target",
                        "h13-afro,m3-goldilocks",
                    ]
                )
        finally:
            regress.parse = real
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("2 VM(s) at once", text)
        for target in ("h13-afro", "m3-goldilocks"):
            self.assertIn(f"[local {target}] FW=", text)
            self.assertIn(f"rm -rf build/instances/{target}/share/out/regress", text)
            self.assertIn(f"build/regress/t6/{target}/host/meminfo-start.txt", text)
            self.assertIn(f"build/regress/t6/{target}/host/meminfo-end.txt", text)
            self.assertIn(f"is-active beanjar-vm-regress-{target}.service", text)
            # MEM: each target's model's; no SSH_PORT: the instance's ports are on its own address
            self.assertIn(
                "NET_LINK=nat NET_MODE=direct AUDIO='' MEM='' SMP='' ACCEL='' PMLOG_DEBUG='' KERNEL_ARGS='' QEMU_EXTRA='' GPU_DEVICE='' GUEST_KERNEL='' NET_DUMP='' timeout",
                text,
            )
        self.assertNotRegex(text, r"MEM=[0-9]")  # empty: the launcher takes the model's
        self.assertNotIn("build/share/out", text)
        self.assertIn("wall 0 s, 2 at once", text)


class ReadyLineTest(unittest.TestCase):
    def test_ready_after_a_job_notice(self):
        # a loaded TCG guest's shell may print "Terminated" (luna-send's timeout) before READY
        class FakeHost:
            inst = regress.Instance("fake")
            host = None

            def run(self, cmd, timeout):
                return 0, ""

        def step(h, a, r, name, arg, out):
            if name == "ready":
                r.text = "Terminated\nTerminated\nREADY 79.89 68.633748348 229"
                return 0
            r.text = ""
            return 1

        a, _ = regress.parse(["--local", "--headless", "--run", "t7"], found=FOUND, env={})
        real = regress.guest_step
        regress.guest_step = step
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                r = regress.run_target(FakeHost(), a, "k5lp-goldilocks2", W19P)
        finally:
            regress.guest_step = real
        self.assertEqual((r.steps["ready"], r.boot_done, r.tvservice), ("PASS", "79.9 s", "229"))


class FirstBootRestartTest(unittest.TestCase):
    """A guest restart during the steps: followed once for a target whose firmware restarts on its
    first boot (targets.first_boot_restart), the steps run again from the boot; any other fails."""

    W24G = "02.68.80.01-HE_DTV_W24G_AFABATAA"

    def run_steps(self, target, fw, restarts, back=0, off=False):
        """run_target with guest steps that pass, the guest restarting during check-m1 the first
        `restarts` times; back: the status of the wait for the VM's new QEMU. (Result, steps run)."""
        ran = []

        class FakeHost:
            inst = regress.Instance("fake")
            host = None

            def run(self, cmd, timeout):
                return (back, "") if "until n=" in cmd else (0, "")

        def step(h, a, r, name, arg, out):
            ran.append(name)
            if name == "check-m1" and ran.count("check-m1") <= restarts:
                raise regress.GuestRestarted(name, off=off)
            r.text = {
                "ready": "READY 79.89 68.6 229",
                "check-m1": "M1 PASS",
                "app-calls": "APP-CALLS PASS",
                "tvservice": "TVSERVICE PASS",
                "standby-prep": "STANDBY SKIP no Quick Start+",
            }.get(name, "")
            return 0

        a, _ = regress.parse(["--local", "--headless", "--run", "t8"], found=FOUND, env={})
        with (
            mock.patch.object(regress, "guest_step", step),
            mock.patch.object(regress.checklist, "judge", lambda text, broadcast: []),
            mock.patch.object(regress.checklist, "verdict", lambda items: "PASS"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            r = regress.run_target(FakeHost(), a, target, fw)
        return r, ran

    def test_one_first_boot_restart_is_followed(self):
        self.assertTrue(regress.targets.first_boot_restart(regress.target_of("o22n2-spacebuns")))
        r, ran = self.run_steps("o22n2-spacebuns", self.W24G, 1)
        self.assertEqual((r.restarts, r.restart), (1, ""))
        self.assertEqual(ran[:3], ["ready", "check-m1", "ready"])
        self.assertEqual(ran[3], "check-m1")
        self.assertEqual((r.steps["ready"], r.steps["check-m1"]), ("PASS", "PASS"))
        self.assertEqual(r.boot_done, "79.9 s")

    def test_second_restart_fails(self):
        r, ran = self.run_steps("o22n2-spacebuns", self.W24G, 2)
        self.assertEqual(r.restarts, 1)
        self.assertIn("after 1 followed restart", r.restart)
        self.assertFalse(r.ok)
        self.assertEqual(ran.count("ready"), 2)

    def test_vm_not_back_fails(self):
        r, _ = self.run_steps("o22n2-spacebuns", self.W24G, 1, back=124)
        self.assertIn("did not run again", r.restart)
        self.assertFalse(r.ok)

    def test_power_off_fails(self):
        # a first boot that ends in a power-off is not the firmware's restart
        r, ran = self.run_steps("o22n2-spacebuns", self.W24G, 1, off=True)
        self.assertEqual((r.restarts, r.restart), (0, "the VM powered off during check-m1"))
        self.assertEqual(ran.count("ready"), 1)
        self.assertFalse(r.ok)

    def test_other_targets_fail_on_a_restart(self):
        self.assertFalse(regress.targets.first_boot_restart(regress.target_of("k5lp-goldilocks2")))
        r, ran = self.run_steps("k5lp-goldilocks2", W19P, 1)
        self.assertEqual((r.restarts, r.restart), (0, "the guest restarted during check-m1"))
        self.assertEqual(ran.count("ready"), 1)
        self.assertFalse(r.ok)


class GuestStepTest(unittest.TestCase):
    """guest_step on a local VM host, the guest's side planted: STEP.txt and STEP.rc are copied
    out of its out share (RUN_DIR/share/out/regress) as regular files only, never through a link
    the guest made there (the user's files would go into the results) or from its FIFO (a hang),
    and the last .rc is never removed through a linked directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.run_dir = os.path.join(self.tmp, "run")
        self.gout = os.path.join(self.run_dir, "share", "out", "regress")
        os.makedirs(self.gout)
        # a running VM: QEMU's pidfile (it removes it as it exits)
        open(os.path.join(self.run_dir, "qemu.pid"), "w").close()
        self.results = os.path.join(self.tmp, "results")
        os.mkdir(self.results)
        # what the guest must never get the host to read or remove: a user's secret key, a directory
        os.makedirs(os.path.join(self.tmp, "home", ".ssh"))
        self.secret = os.path.join(self.tmp, "home", ".ssh", "id_ed25519")
        with open(self.secret, "w") as f:
            f.write("SECRET-KEY\n")
        self.elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(self.elsewhere)
        for n, text in (("boot.rc", "0\n"), ("boot.txt", "the user's\n")):
            with open(os.path.join(self.elsewhere, n), "w") as f:
                f.write(text)

    def step(self, guest):
        """guest_step("boot") with guest() as the guest's answer to the console line; (its status,
        r.text, the results' files and their contents, every command's output), or None if it does
        not return within 15 s."""
        inst = object.__new__(regress.Instance)
        inst.iid, inst.run_dir, inst.guest_out = None, self.run_dir, self.gout
        inst.env = {"RUN_DIR": self.run_dir}
        said = []

        class H(regress.Host):
            def run(self, cmd, timeout):
                console = " && host/scripts/vm-launcher console "
                if console in cmd:
                    rc, text = super().run(cmd.split(console)[0], timeout)
                    if not rc:
                        guest()
                else:
                    rc, text = super().run(cmd, timeout)
                said.append(text)
                return rc, text

        a = argparse.Namespace(scale=0.1, timeout=1, dry_run=False)  # waits 3 s for the .rc
        r = argparse.Namespace(text=None)
        done = []

        def call():
            try:
                return regress.guest_step(H(None, TOP, False, inst), a, r, "boot", "", self.results)
            except regress.GuestRestarted as e:
                return e

        t = threading.Thread(target=lambda: done.append(call()), daemon=True)
        t.start()
        t.join(15)
        if t.is_alive():
            # let a reader stuck on the guest's FIFO go
            fifo = os.path.join(self.gout, "boot.txt")
            with contextlib.suppress(OSError):
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            t.join(30)
            return None
        files = {}
        for n in sorted(os.listdir(self.results)):
            with open(os.path.join(self.results, n), "rb") as f:
                files[n] = f.read()
        return done[0], r.text, files, "".join(said)

    def plant(self, txt=None, rc="0\n"):
        def guest():
            if txt is not None:
                txt(os.path.join(self.gout, "boot.txt"))
            if rc is not None:
                with open(os.path.join(self.gout, "boot.rc"), "w") as f:
                    f.write(rc)

        return guest

    def write(self, text):
        def make(path):
            with open(path, "w") as f:
                f.write(text)

        return make

    def assertNoSecret(self, got):
        self.assertIsNotNone(got, "guest_step hung")
        self.assertNotIn("SECRET", repr(got))

    def restart(self, n=1, under_way=False):
        """The unit's record of a guest restart in RUN_DIR (unit.run's restarts and restarting)."""
        with open(os.path.join(self.run_dir, "restarts"), "w") as f:
            f.write(f"{n}\n")
        if under_way:
            open(os.path.join(self.run_dir, "restarting"), "w").close()

    def test_guest_restart_ends_the_wait(self):
        # the step's process went with the old boot: no .rc ever comes
        got = self.step(lambda: self.restart(1))
        self.assertIsInstance(got[0], regress.GuestRestarted)
        self.assertEqual(got[0].step, "boot")

    def test_restart_under_way_ends_the_wait(self):
        got = self.step(lambda: self.restart(0, under_way=True))
        self.assertIsInstance(got[0], regress.GuestRestarted)

    def test_power_off_ends_the_wait(self):
        got = self.step(lambda: os.unlink(os.path.join(self.run_dir, "qemu.pid")))
        self.assertIsInstance(got[0], regress.GuestRestarted)
        self.assertTrue(got[0].off)

    def test_restart_after_a_killed_step(self):
        # the guest's shutdown killed the step, which wrote its .rc before the reset showed
        def guest():
            self.plant(self.write("Killed\n"), "137\n")()
            self.restart(1)

        self.assertIsInstance(self.step(guest)[0], regress.GuestRestarted)

    def test_no_restart_recorded(self):
        self.restart(0)
        status, text, _, _ = self.step(self.plant(self.write("hello\n"), "0\n"))
        self.assertEqual((status, text), (0, "hello"))

    def test_regular_files(self):
        status, text, files, _ = self.step(self.plant(self.write("hello\n"), "3\n"))
        self.assertEqual((status, text), (3, "hello"))
        self.assertEqual(files, {"boot.rc": b"3\n", "boot.txt": b"hello\n"})

    def test_link_to_a_user_file(self):
        got = self.step(self.plant(lambda p: os.symlink(self.secret, p)))
        self.assertNoSecret(got)
        self.assertNotEqual(got[0], 0)
        self.assertNotIn("boot.txt", got[2])

    def test_rc_link_to_a_user_file(self):
        def guest():
            self.write("hello\n")(os.path.join(self.gout, "boot.txt"))
            os.symlink(self.secret, os.path.join(self.gout, "boot.rc"))

        got = self.step(guest)
        self.assertNoSecret(got)
        self.assertNotEqual(got[0], 0)

    def test_fifo_does_not_hang(self):
        got = self.step(self.plant(os.mkfifo))
        self.assertIsNotNone(got, "guest_step hung on the guest's FIFO")
        self.assertNotEqual(got[0], 0)

    def test_linked_directory_not_removed_through(self):
        os.rmdir(self.gout)
        os.symlink(self.elsewhere, self.gout)
        got = self.step(lambda: None)
        self.assertNoSecret(got)
        self.assertNotEqual(got[0], 0)
        self.assertEqual(sorted(os.listdir(self.elsewhere)), ["boot.rc", "boot.txt"])
        self.assertNotIn("boot.txt", got[2])


class LbStoppedItselfTest(unittest.TestCase):
    """guest.sh's tvservice step on a legacy-broadcast that is not the process it was at ready: it
    passes only on evidence of clean exits (tests/regress/lb-exit.sh): systemd's
    legacy-broadcast.service finished with result success, status 0 and no restart, every exit the
    hub logged of one it started has EXIT_CODE 0, and no crash report names it. Gone (lb_stopped_itself)
    or started again since by a call (lb_started_again), never any new pid."""

    UNIT = "Result=success\nExecMainStatus=0\nNRestarts=0\nActiveState=inactive\n"
    HUB = (
        "ls-hubd LSHUB_SRV_EXIT "
        '{"SERVICE_NAME":"com.webos.service.legacybroadcast","EXE":"/usr/sbin/setcpushares-ls2 '
        '/etc/systemd/system/scripts/legacy-broadcast.sh","STATE":3,"EXIT_CODE":0,"RESPAWN":0,"PID":2756}\n'
    )

    def run_case(self, call, unit=UNIT, crash="", hub="", release=""):
        import re

        with open(os.path.join(TOP, "tests", "regress", "guest.sh")) as f:
            text = f.read()
        funcs = "".join(
            re.search(rf"^{name}\(\) \{{.*?\n\}}\n", text, re.M | re.S).group(0)
            for name in ("lb_exits_clean", "lb_call", "lb_stopped_itself", "lb_started_again")
        )
        stubs = (
            f". {os.path.join(TOP, 'tests', 'regress', 'lb-exit.sh')}\nscale=1\n"
            'in_webos() { case "$1" in systemctl*) printf "%s" "$UNIT";; ls*) printf "%s" "$CRASH";;'
            ' for*) printf "%s" "$HUB";; pidof*) printf "%s" "$RELEASE";; esac; }\n'
            "wt() { echo '{\"returnValue\":true}'; }\nrelease_pids() { echo 77; }\n"
        )
        return subprocess.run(
            ["sh", "-c", stubs + funcs + call],
            env=dict(os.environ, UNIT=unit, CRASH=crash, HUB=hub, RELEASE=release),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def test_clean_exit_passes(self):
        r = self.run_case("lb_stopped_itself 5")
        self.assertEqual(r.returncode, 0, r.stdout)
        # W23L: the boot instance (1131) stopped itself, a call started 8891, which runs at the step
        r = self.run_case("lb_started_again 1131 8891", hub=self.HUB)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("started again since as 8891", r.stdout)

    def test_everything_else_fails(self):
        ok = {
            "Result": "success",
            "ExecMainStatus": "0",
            "NRestarts": "0",
            "ActiveState": "inactive",
        }
        for key, value in (
            ("Result", "exit-code"),
            ("Result", "signal"),
            ("ExecMainStatus", "9"),
            ("NRestarts", "1"),
            ("ActiveState", "active"),
        ):
            unit = "".join(f"{k}={value if k == key else v}\n" for k, v in ok.items())
            for call in ("lb_stopped_itself 5", "lb_started_again 5 77"):
                self.assertNotEqual(self.run_case(call, unit=unit).returncode, 0, (call, unit))
        crashed = self.HUB.replace('"EXIT_CODE":0', '"EXIT_CODE":139')
        for call in ("lb_stopped_itself 5", "lb_started_again 5 77"):
            self.assertNotEqual(self.run_case(call, crash="legacy-broadcast.crash").returncode, 0)
            self.assertNotEqual(self.run_case(call, unit="").returncode, 0)
            r = self.run_case(call, hub=self.HUB + crashed)
            self.assertNotEqual(r.returncode, 0, call)
            self.assertIn("abnormal exit", r.stdout)
        # an old pid still beside the new one, or tvservice (RELEASE) itself under new pids: no
        self.assertNotEqual(self.run_case("lb_started_again 5,6 6,77").returncode, 0)
        self.assertNotEqual(self.run_case("lb_started_again 5 77", release="77").returncode, 0)


class DeviceCensusIntegrationTest(unittest.TestCase):
    def test_two_non_dry_targets_keep_snapshot_and_report_separate(self):
        class FakeHost:
            inst = regress.Instance("fake")
            host = None

            def __init__(self, root, target):
                self.root, self.target = root, target

            def run(self, cmd, timeout):
                out = os.path.join(self.root, "build/regress/census-integration", self.target)
                os.makedirs(os.path.join(out, "host"), exist_ok=True)
                if "host/scripts/vm-launcher start" in cmd:
                    return 1, "fake start stops before guest actions"
                if "host/scripts/vm-launcher stop" in cmd:
                    # Model the launcher's real saved snapshot location after stop.
                    snapshot = os.path.join(out, "saved", "20261006-stop")
                    os.makedirs(snapshot, exist_ok=True)
                    open(os.path.join(snapshot, ".done"), "w").close()
                    for folder in ("var-log", "tmp-logs"):
                        os.makedirs(os.path.join(snapshot, folder))
                    path = "/dev/first" if self.target == "h13-afro" else "/dev/second"
                    with open(os.path.join(snapshot, "dmesg.txt"), "w") as f:
                        f.write(f"open {path} failed: No such file or directory\n")
                return 0, ""

        a, _ = regress.parse(
            ["--local", "--headless", "--run", "census-integration"], found=FOUND, env={}
        )
        self.assertFalse(a.dry_run)
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(regress, "TOP", root),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            results = [
                regress.run_target(FakeHost(root, target), a, target, fw)
                for target, fw in (("h13-afro", WT1H), ("m3-goldilocks", W18R))
            ]
            run = os.path.join(root, "build/regress/census-integration")
            self.assertFalse(os.path.exists(os.path.join(run, "device-census.json")))
            for result, path in zip(results, ("/dev/first", "/dev/second")):
                report = result.device_census["report"]
                self.assertEqual(report, f"{result.target}/device-census.json")
                with open(os.path.join(run, report)) as f:
                    evidence = json.load(f)
                self.assertEqual(evidence["snapshot"], "20261006-stop")
                self.assertEqual(evidence["target"], result.target)
                self.assertEqual([r["path"] for r in evidence["failed_device_opens"]], [path])
                self.assertEqual(result.device_census["failed_open_groups"], 1)


class CaptureTest(unittest.TestCase):
    def test_capture_verdict_even_when_start_or_stop_fails(self):
        class FakeHost:
            inst = regress.Instance("fake")
            host = None

            def __init__(self, stop_rc, capture_rc):
                self.stop_rc, self.capture_rc, self.commands = stop_rc, capture_rc, []

            def run(self, cmd, timeout):
                self.commands.append(cmd)
                if "host/scripts/vm-launcher start" in cmd:
                    return 1, "start failed"
                if "host/scripts/vm-launcher stop" in cmd:
                    return self.stop_rc, "stop failed"
                if "pcap-summary.py" in cmd:
                    return (
                        self.capture_rc,
                        "LG contact: none" if not self.capture_rc else "capture invalid",
                    )
                return 0, ""

        a, _ = regress.parse(
            ["--local", "--headless", "--run", "capture-test"], found=FOUND, env={"NET_LINK": "off"}
        )
        for stop_rc, capture_rc, expected in ((0, 0, "PASS"), (0, 1, "FAIL"), (1, 0, "FAIL")):
            host = FakeHost(stop_rc, capture_rc)
            with contextlib.redirect_stdout(io.StringIO()):
                r = regress.run_target(host, a, "h13-afro", WT1H)
            self.assertEqual(
                (r.steps["start"], r.steps["capture"], r.ok), ("FAIL", expected, False)
            )
            self.assertEqual(sum("pcap-summary.py" in c for c in host.commands), 1)
            self.assertLess(
                next(
                    i for i, c in enumerate(host.commands) if "host/scripts/vm-launcher stop" in c
                ),
                next(i for i, c in enumerate(host.commands) if "pcap-summary.py" in c),
            )
        check_cmd = next(c for c in host.commands if "pcap-summary.py" in c)
        with tempfile.TemporaryDirectory() as d:
            target_dir = os.path.join(d, "build/regress/capture-test/h13-afro")
            os.makedirs(target_dir)
            p = subprocess.run(["sh", "-c", check_cmd], cwd=d, capture_output=True)
            self.assertEqual(p.returncode, 1)
            with open(os.path.join(target_dir, "pcap-lg.txt"), "rb") as f:
                self.assertEqual(
                    f.read(), b"capture missing: build/regress/capture-test/h13-afro/net.pcap\n"
                )
        remote, _ = regress.parse(
            ["--headless", "-j", "1", "--run", "capture-remote-fail"],
            found=FOUND,
            env={"NET_LINK": "off"},
        )
        # the local copy of the remote run goes under regress.TOP: a directory of the test's own
        with (
            tempfile.TemporaryDirectory() as top,
            mock.patch.object(regress, "TOP", top),
            mock.patch.object(regress, "local", return_value=1),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            host = FakeHost(0, 0)
            host.host, host.dest = "vmhost", "beanjar"  # its results copied back from there
            r = regress.run_target(host, remote, "h13-afro", WT1H)
        self.assertEqual(r.steps["capture"], "FAIL")
        self.assertFalse(r.ok)

    def test_pcap_summary_valid_empty_truncated_and_lg(self):
        script = os.path.join(TOP, "tests/regress/netstub/pcap-summary.py")

        def header(endian="<", nanoseconds=False):
            magic = 0xA1B23C4D if nanoseconds else 0xA1B2C3D4
            return struct.pack(endian + "IHHIIII", magic, 2, 4, 0, 0, 65536, 1)

        def packet(name):
            qname = b"".join(bytes((len(x),)) + x.encode() for x in name.split(".")) + b"\0"
            dns = struct.pack(">HHHHHH", 1, 0, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
            udp = struct.pack(">HHHH", 12345, 53, 8 + len(dns), 0) + dns
            ip = (
                b"\x45\0"
                + struct.pack(">H", 20 + len(udp))
                + b"\0\0\0\0\x40\x11\0\0"
                + bytes((10, 0, 2, 15, 10, 0, 2, 3))
            )
            return b"\0" * 12 + b"\x08\0" + ip + udp

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "net.pcap")

            def check(content):
                with open(path, "wb") as f:
                    f.write(content)
                return subprocess.run(
                    [sys.executable, script, path, "--lg"], capture_output=True, text=True
                )

            for e, ns in (("<", False), (">", False), ("<", True), (">", True)):
                r = check(header(e, ns))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("LG contact: none", r.stdout)
            good = packet("example.com")
            for e, ns in (("<", False), (">", False), ("<", True), (">", True)):
                record = struct.pack(e + "IIII", 0, 0, len(good), len(good)) + good
                r = check(header(e, ns) + record)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("example.com", r.stdout)
            lg = packet("service.lge.com")
            record = struct.pack("<IIII", 0, 0, len(lg), len(lg)) + lg
            r = check(header() + record)
            self.assertEqual(r.returncode, 1)
            self.assertIn("LG contact: DNS query for service.lge.com", r.stdout)
            shortened = header() + struct.pack("<IIII", 0, 0, len(good), len(good) + 1) + good
            self.assertNotEqual(check(shortened).returncode, 0)
            r = subprocess.run([sys.executable, script, path], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            for broken in (
                b"",
                header()[:23],
                header() + b"\0" * 15,
                header() + struct.pack("<IIII", 0, 0, len(good), len(good)) + good[:-1],
                header() + struct.pack("<IIII", 0, 0, 65537, 65537),
                header()[:20] + struct.pack("<I", 101),
            ):
                r = check(broken)
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
