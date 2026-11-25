# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The boot manifest (targets.boot_conf) and guest/initramfs/rootfs/init that carries it out."""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


from beanjar import (
    core,
    keys,
    nvm,
    targets,
    paths,
    qemu,
)

from launchkit import boot_conf, FW_NAME, H13_AFRO, store_snapshot, text, TOP, TRUST_PAK

HERE = os.path.dirname(os.path.abspath(__file__))
# made-up parameters of an "mstar-mmap" module (targets._mstar_mmap: LM14 04.11.18's record IDs)
MSTAR_DERIVED = (
    "mmap_ids=77",
    "lx_mem_id=0",
    "lx_mem2_id=76",
    "gop_fb=60",
    "mmap_file=/host/mstar-mmap.bin",
)


SYSTEMD_ROW = targets.RELEASES["dreadlocks"]._replace(
    init_system="systemd", pmlog_mode="preload-systemd", pmlog_job="pm-log-daemon"
)


class BootConfTest(unittest.TestCase):
    """targets.boot_conf: the boot manifest guest/initramfs/rootfs/init carries out, from the per-axis tables."""

    def test_lg_scaler_scope(self):
        # Stock O22, O22N, ombre and LM21's libval use video30/31 in UHD panel coordinates, as
        # spacebuns' nyxVscaler does (videooutputd's connect fails without the nodes): the media
        # lanes with our elements get lgemu.ko's model.
        for chip, release in (
            ("o22", "mullet"),
            ("o22", "number1"),
            ("o22n", "number1"),
            ("o22n2", "ombre"),
            ("o22n2", "spacebuns"),
            ("lm21a", "kisscurl"),
            ("lm21an", "mullet"),
            ("lm21ann", "number1"),
            ("lm21u", "kisscurl"),
            ("lm21u", "ombre"),
            ("o24", "ombre"),
        ):
            with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
                f.write(targets.boot_conf((chip, release), []))
                f.flush()
                conf = boot_conf(f.name, ("MODULE_PARAMS_lgemu", "MODULE_PARAMS_lg115x"))
            params = conf["MODULE_PARAMS_lgemu"].split()
            # its windows: four on O24 (its kernel's video30 to video33), two elsewhere
            self.assertIn(f"v4l2_vsc={4 if chip == 'o24' else 2}", params)
            # The scaler's panel space: UHD, but the StanbyME's (lm21u) panel is FHD.
            screen = "1920,1080" if chip == "lm21u" else "3840,2160"
            self.assertIn(f"vsc_screen={screen}", params)
            self.assertFalse(
                any(w.startswith("v4l2_vsc=") for w in conf["MODULE_PARAMS_lg115x"].split())
            )

    def test_v4l2_capture(self):
        """video60 (v4l2_capture=1) is the chip's vendor module's, on every captureservice target
        whose libvtcapture opens it: LG-O's (O20N, E60N and the O22 family), Realtek's from K6LP, MStar's LM21; and
        W16R's, read by its libkadaptor shim's DIP; no other module gets the parameter, and no
        other target."""
        for target in targets.all_targets():
            chip = target[0]
            want = (
                chip.startswith(("o2", "e60n", "m23"))
                or (chip.startswith("k") and chip not in ("k2l", "k2lp", "k3lp", "k5lp"))
                or chip.startswith("lm21")
                or targets.name(target) == "m2-dreadlocks"
            )
            with self.subTest(target=targets.name(target)):
                self.assertEqual(targets.v4l2_capture(target), want)
                params = targets.module_params(target)
                holders = [m for m, words in params.items() if "v4l2_capture=1" in words]
                vendor = [m for m in params if m in targets.CAPTURE_MODULES]
                self.assertEqual(holders, vendor if want else [])
                self.assertLessEqual(len(vendor), 1)

    def test_far_field_mic(self):
        """lgemu.ko's mic=1 is the board's model's own hardware (Soc.mic): the C2's O22 board line
        alone, never another chip's that borrows its row (o22n, o22n2), and never from the
        firmware's generators."""
        for target in targets.all_targets():
            with self.subTest(target=targets.name(target)):
                s = targets.soc(target)
                want = target[0] == "o22"
                self.assertEqual(s.mic, want)
        for target in (
            ("o22", "ponytail"),
            ("o22", "queue"),
            ("k8hp", "queue"),
            ("o22n2", "ombre"),
        ):
            with self.subTest(boot_conf=targets.name(target)):
                params = targets.boot_conf(
                    target, [], {m.name: () for m in targets.soc(target).modules if m.derived}
                )
                lgemu = next(l for l in params.splitlines() if l.startswith("MODULE_PARAMS_lgemu="))
                self.assertEqual("mic=1" in lgemu.split('"')[1].split(), target[0] == "o22")

    def test_wt1h(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(targets.boot_conf(H13_AFRO, ["normal", "gl", "webos", "soc", "common"]))
            f.flush()
            conf = boot_conf(f.name)
        self.assertEqual(
            conf,
            {
                "TARGET": "h13-afro",
                "INIT_SYSTEM": "upstart",
                "ROOTFS": "rootfs",
                "PAKS": "tvservice:/mnt/tvservice otncabi:/mnt/otncabi otycabi:/mnt/otycabi fonts:/mnt/fonts "
                "smartkey:/mnt/smartkey",
                "PAK_LOWERS": "",
                "LAYERS": "common soc webos gl normal vm",
                "STATE_DISKS": "db8 cmn_data appstore partinfo authcxt sestore sedata",
                "NVM_DISKS": "nvram eeprom",
                "NVM_EEPROM": "eeprom",
                "PARTINFO_DISK": "partinfo",
                "PART_NODES": "",
                "PMLOG_MODE": "preload",
                "PMLOG_JOB": "PmLogDaemon",
                "TRUST_BUNDLES": "etc/ssl/certs/ca-certificates.crt usr/share/ca-certificates/sdp/sdp-ca.pem",
                "TRUST_HASH_DIR": "etc/ssl/certs",
                "BOARD_CMDLINE": "/etc/emulator/board.cmdline",
                "SOC_MODULES": "lg115x",
                "MODULE_PARAMS_mstar_utopia": "",
                "MODULE_PARAMS_lg115x": "chip_rev=0x13b0 ace_rev=0x13b0 platform=0x2000 graphic_res=0x07800438 "
                "i2c=micom:1:0x52,nvm:4:0xa0,amp:2:0x54,amp:2:0x56,amp:4:0x54 spi_cs=2 logm_obj=76 "
                "gpio_pins=144 amst=17:41:20 amst_se=16 amst_spdif=3 be=21:19 timer_hz=198000000",
            },
        )

    def test_updatable(self):
        """firmware.updatable: boot.conf carries EMMC_OVERLAY, EMMC_TABLE and EMMC_PART_NODES only
        when the flag is on, so a default VM's boot.conf is unchanged; on, they name the overlay
        disk, the whole-disk dm-linear table and the overlay-backed per-partition nodes init builds
        /dev/mmcblk0 and /dev/mmcblk0pN from."""
        emmc_vars = ("EMMC_OVERLAY", "EMMC_TABLE", "EMMC_PART_NODES")
        layers = ["normal", "gl", "webos", "soc", "common"]
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(targets.boot_conf(H13_AFRO, layers))
            f.flush()
            off = boot_conf(f.name, emmc_vars)
            self.assertNotIn("EMMC_OVERLAY", text(f.name))
        self.assertEqual(
            (off["EMMC_OVERLAY"], off["EMMC_TABLE"], off["EMMC_PART_NODES"]), ("", "", "")
        )
        emmc = (
            "emmc-overlay",
            ["0:2048:emmc-overlay:0", "2048:512:partinfo:0"],
            ["1:0:2048", "8:11264:2048"],
        )
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(targets.boot_conf(H13_AFRO, layers, emmc=emmc))
            f.flush()
            on = boot_conf(f.name, emmc_vars)
        self.assertEqual(on["EMMC_OVERLAY"], "emmc-overlay")
        self.assertEqual(on["EMMC_TABLE"], "0:2048:emmc-overlay:0 2048:512:partinfo:0")
        self.assertEqual(on["EMMC_PART_NODES"], "1:0:2048 8:11264:2048")
        # a two-element emmc tuple (no per-partition nodes) still works: EMMC_PART_NODES is empty
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(targets.boot_conf(H13_AFRO, layers, emmc=(emmc[0], emmc[1])))
            f.flush()
            short = boot_conf(f.name, emmc_vars)
        self.assertEqual(short["EMMC_PART_NODES"], "")

    def test_w15m(self):
        """M14's lg115x.ko carries M14 B0's identity (not KADP's H13 defaults)."""
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(
                targets.boot_conf(("m14", "beehive"), ["normal", "gl", "webos", "soc", "common"])
            )
            f.flush()
            conf = boot_conf(f.name)
        self.assertEqual(conf["SOC_MODULES"], "lg115x")
        self.assertEqual(
            conf["MODULE_PARAMS_lg115x"].split(),
            [
                "chip_rev=0x14b0",
                "ace_rev=0x14b0",
                "platform=0x1000",
                "graphic_res=0x07800438",
                "i2c=micom:1:0x52,nvm:4:0xa0,amp:0:0x54",
                "spi_cs=2",
                "logm_obj=80",
                "gpio_pins=136",
                "amst=17:41:20",
                "amst_se=16",
                "be=22:20",
                "timer_hz=198000000",
                "de_mem=0x7e000000:0x1000000",
                "de_capacity=2:2:2:1920:1080:1920:1080:0:120:2:0:960:540:1:1:1:0:0:0:1:320:240:1",
                "de_abi=49:50:55:86:87:22",
            ],
        )
        self.assertEqual(conf["MODULE_PARAMS_mstar_utopia"], "")
        self.assertEqual((conf["PMLOG_MODE"], conf["PMLOG_JOB"]), ("preload", "PmLogDaemon"))

    def test_w16r(self):
        """bsppart is attached but mounted nowhere: its bsp/ is a lower layer; license is a pak."""
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(targets.boot_conf(("m2", "dreadlocks"), ["headless", "webos", "soc", "common"]))
            f.flush()
            conf = boot_conf(f.name)
        self.assertEqual(
            (conf["TARGET"], conf["PAK_LOWERS"], conf["LAYERS"]),
            ("m2-dreadlocks", "bsppart:bsp", "common soc webos headless vm"),
        )
        self.assertEqual(
            conf["PAKS"],
            "tvservice:/mnt/tvservice otncabi:/mnt/otncabi otycabi:/mnt/otycabi "
            "fonts:/mnt/fonts smartkey:/mnt/smartkey license:/mnt/license",
        )
        self.assertIn("bsppart", targets.pak_files(("m2", "dreadlocks")))
        self.assertEqual(conf["SOC_MODULES"], "mstar-utopia")
        self.assertEqual(conf["MODULE_PARAMS_lg115x"], "")
        # LG's devlog switch, not the preload (guest/layers/webos/dreadlocks's emulator-pmlog-devlog.conf)
        self.assertEqual((conf["PMLOG_MODE"], conf["PMLOG_JOB"]), ("devlog", "PmLogDaemon"))
        self.assertIn("miu_interval=0x80000000", conf["MODULE_PARAMS_mstar_utopia"].split())
        # the micom, the NVM EEPROM and the amplifier on HWI2C, by SlaveCfg (port << 8 | slave)
        self.assertIn(
            "i2c=micom:0x0452,nvm:0x00a0,amp:0x0054", conf["MODULE_PARAMS_mstar_utopia"].split()
        )
        self.assertEqual(conf["NVM_EEPROM"], "eeprom")

    def test_w19p(self):
        """webOS 4.5 on Realtek K5LP: goldilocks' paks, rtk.ko with the table's values and the
        firmware's RPC answers after them, lgemu.ko's five I2C adapters."""
        target = ("k5lp", "goldilocks2")
        derived = {"rtk": ("rpc_reply=100:108:68", "rpc_create=100:10,201:1")}
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(targets.boot_conf(target, ["webos", "soc", "common"], derived))
            f.flush()
            conf = boot_conf(f.name, ("MODULE_PARAMS_lgemu", "MODULE_PARAMS_rtk"))
        self.assertEqual(
            (conf["TARGET"], conf["PAK_LOWERS"], conf["SOC_MODULES"]),
            ("k5lp-goldilocks2", "bsppart:bsp", "rtk btmtk_usb webos_tv"),
        )
        self.assertEqual(
            conf["STATE_DISKS"],
            "db8 cmn_data appstore uhdcp partinfo authcxt sestore wossestore sedata",
        )
        self.assertEqual(conf["PMLOG_MODE"], "preload-env")
        words = conf["MODULE_PARAMS_rtk"].split()
        self.assertIn("rbus_phys=0x18000000", words)
        self.assertEqual(words[-2:], list(derived["rtk"]))
        self.assertEqual(
            conf["MODULE_PARAMS_lgemu"],
            "i2c_buses=0,1,2,3,4 i2c_dev=micom:0:0x29,nvm:0:0x50,amp:2:0x2a,amp:2:0x2b",
        )
        # the firmware's part is required
        with self.assertRaisesRegex(targets.Error, "no firmware parameters for rtk"):
            targets.boot_conf(target, ["webos"])

    def test_tee_huk(self):
        """The TEE (rtk.ko tee=1 on the Realtek targets only) gets the VM's own hardware-unique key
        after the table's and the firmware's words; the key is random per VM, kept, 0600, and made
        again (with a warning) when it is not one."""
        self.assertEqual(
            {
                targets.name(p): targets.tee_module(p)
                for p in targets.all_targets()
                if targets.tee_module(p)
            },
            {
                "k5lp-goldilocks2": "rtk",
                "k6lp-jhericurl": "rtk",
                "k6hp-jhericurl": "rtk",
                "k7lp-kisscurl": "rtk",
                "k8lp-mullet": "rtk",
                "k8ap-mullet": "rtk",
                "k8hp-mullet": "rtk",
                "k8hp-number1": "rtk",
                "k8hp-ombre": "rtk",
                "k8hp-ponytail": "rtk",
                "k8hp-queue": "rtk",
                "k8hpp-number1": "rtk",
                "k8lpn-number1": "rtk",
                "kf23f-number1": "rtk",
                "k8lpn2-ombre": "rtk",
                "k8hpp-ombre": "rtk",
                "k8hpp-ponytail": "rtk",
                "k8hpp-queue": "rtk",
                "k8lpn-ombre": "rtk",
                "k8lpn-ponytail": "rtk",
                "k8lpn-queue": "rtk",
                "k8lpn2-ponytail": "rtk",
                "k8lpn2-queue": "rtk",
                "k25lp-ponytail": "rtk",
                "k25lp-queue": "rtk",
                "k24-ombre": "rtk",
                "k24-ponytail": "rtk",
                "k24-queue": "rtk",
                "k24n-ponytail": "rtk",
                "k24n-queue": "rtk",
            },
        )
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            os.mkdir(a)
            os.mkdir(b)
            said = []
            huk_a, huk_b = keys.ensure_tee_huk(a, warn=said.append), keys.ensure_tee_huk(b)
            self.assertRegex(huk_a, "^[0-9a-f]{64}$")
            self.assertNotEqual(huk_a, huk_b, "two VMs, two keys")
            self.assertEqual(len(said), 1)
            self.assertEqual(keys.ensure_tee_huk(a, warn=said.append), huk_a, "kept")
            self.assertEqual(len(said), 1, "no warning when it is there")
            path = os.path.join(a, keys.TEE_HUK)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path, "w") as f:
                f.write("00\n")
            self.assertNotEqual(
                keys.ensure_tee_huk(a, warn=said.append), huk_a, "not a key: a new one"
            )
            self.assertEqual(len(said), 2)
        target = ("k7lp", "kisscurl")
        derived = {"rtk": ("rpc_reply=100:108:68",)}
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(
                targets.boot_conf(target, ["webos"], derived, (), {"rtk": ("tee_huk=" + huk_a,)})
            )
            f.flush()
            words = boot_conf(f.name, ("MODULE_PARAMS_rtk",))["MODULE_PARAMS_rtk"].split()
        self.assertIn("tee=1", words)
        self.assertEqual(words[-2:], ["rpc_reply=100:108:68", "tee_huk=" + huk_a])

    def test_ssg_key(self):
        """The second screen gateway's TLS key: releases from webOS 5 (jhericurl) read it,
        the earlier ones and the other platforms do not. The VM's key is RSA PEM in TOML, 0600, kept; its certificate is self-signed with LG's
        subject and validity, always the same for the same key, and matches it. The per-VM layer
        has the key inside sestore.bin (0600) and the certificate over the gateway's (0644)."""
        with_key = {targets.name(p) for p in targets.all_targets() if targets.release(p).ssg_key}
        self.assertIn("k6lp-jhericurl", with_key)
        self.assertIn("o22-queue", with_key)
        for name in (
            "k5lp-goldilocks2",
            "m3-goldilocks",
            "h13-afro",
            "m2-simplesmart",
            "lm14-netcast45",
        ):
            self.assertNotIn(name, with_key)
        from beanjar import sestore

        with tempfile.TemporaryDirectory() as d:
            said = []
            sestore.ensure_ssg(d, warn=said.append)
            self.assertEqual(len(said), 1)
            path = os.path.join(d, sestore.CONFIG)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            key = sestore.load(d)[1][sestore.SSG]
            self.assertLessEqual(len(key), keys.SSG_KEY_MAX)
            sestore.ensure_ssg(d, warn=said.append)
            self.assertEqual(sestore.load(d)[1][sestore.SSG], key, "kept")
            self.assertEqual(len(said), 1)
            self.assertFalse(os.path.exists(os.path.join(d, "ssg-key")))
            cert = keys.ssg_cert_data(key)
            self.assertEqual(keys.ssg_cert_data(key), cert, "the same key, the same certificate")
            pem = os.path.join(d, "cert.pem")
            with open(pem, "wb") as f:
                f.write(cert)

            def modulus(kind, value):
                return subprocess.run(
                    ["openssl", kind, "-noout", "-modulus"],
                    input=value,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                ).stdout

            self.assertEqual(modulus("x509", cert), modulus("rsa", key))
            text = subprocess.run(
                ["openssl", "x509", "-in", pem, "-noout", "-subject", "-issuer", "-enddate"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout
            self.assertIn("CN=LGE TV SSG", text)
            self.assertIn("notAfter=Aug 15 01:49:52 2034 GMT", text)
            p = types.SimpleNamespace(
                layer_dir=os.path.join(d, "layer"),
                identity_dir=d,
                layers=["normal"],
                ca=None,
                service_host="127.0.1.1",
                ssh_port=keys.ROOTSSH_PORT,
            )
            qemu.guest_layer(p)
            self.assertEqual(store_snapshot(p.layer_dir)[1][sestore.SSG], key)
            self.assertFalse(os.path.exists(os.path.join(p.layer_dir, "etc/emulator/ssg.key")))
            self.assertEqual(
                os.stat(os.path.join(p.layer_dir, "etc/emulator/sestore.bin")).st_mode & 0o777,
                0o600,
            )
            served = os.path.join(p.layer_dir, qemu.SSG_CERT_DIR, "ssg.server.cert.pem")
            with open(served, "rb") as f:
                self.assertEqual(f.read(), cert)
            self.assertEqual(os.stat(served).st_mode & 0o777, 0o644)
            # Explicit inventories are never silently repaired or replaced.
            Path(path).write_text('missing="error"\n[items]\n')
            sestore.ensure_ssg(d)
            self.assertEqual(sestore.load(d), ("error", {}))

    def test_w18r(self):
        """webOS 4 on M3: no smartkey, bsppart-dvb as the bsppart lower, no uhdcp, the preload-env PmLog
        mode, mstar-utopia without the LX_MEM2 bias and with the TEE, lgemu.ko's Linux I2C adapters."""
        target = ("m3", "goldilocks")
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(targets.boot_conf(target, ["webos", "soc", "common"]))
            f.flush()
            conf = boot_conf(
                f.name, ("MODULE_PARAMS_lgemu", "MODULE_PARAMS_btmtk_usb", "MODULE_PARAMS_webos_tv")
            )
        self.assertEqual((conf["TARGET"], conf["PAK_LOWERS"]), ("m3-goldilocks", "bsppart:bsp"))
        # MediaTek's Bluetooth driver with no adapter, after the SoC's (as on K5LP)
        self.assertEqual(conf["SOC_MODULES"], "mstar-utopia btmtk_usb webos_tv")
        self.assertEqual(conf["MODULE_PARAMS_btmtk_usb"], "family=30")
        # LG's tuner driver: W18R's GetLIBVersion command (W19P's is 0xc1)
        self.assertEqual(
            conf["MODULE_PARAMS_webos_tv"], "version_cmd=0xc0 lock_cmd=0xae postjob_cmd=0xb0"
        )
        self.assertEqual(
            conf["PAKS"],
            "tvservice:/mnt/tvservice otncabi:/mnt/otncabi otycabi:/mnt/otycabi "
            "fonts:/mnt/fonts license:/mnt/license",
        )
        self.assertIn("bsppart-dvb", targets.pak_files(target))
        self.assertEqual(
            conf["STATE_DISKS"], "db8 cmn_data appstore partinfo authcxt sestore wossestore sedata"
        )
        self.assertEqual(conf["PMLOG_MODE"], "preload-env")
        self.assertIn("lx_mem2_bias=0", conf["MODULE_PARAMS_mstar_utopia"].split())
        # the TEE (mmap.ini, the mailbox) on M3 only
        self.assertIn("secure_shm_id=7", conf["MODULE_PARAMS_mstar_utopia"].split())
        self.assertNotIn("secure_shm_id", targets.boot_conf(("m2", "dreadlocks"), ["webos"]))
        self.assertEqual(
            conf["MODULE_PARAMS_lgemu"],
            "i2c_buses=0,1,2,3,4,5,6 i2c_dev=micom:4:0x29,nvm:6:0x50,amp:0:0x2a,amp:0:0x2b",
        )
        # a target without lgemu parameters has no such line (init's default stays empty)
        self.assertNotIn("MODULE_PARAMS_lgemu", targets.boot_conf(("m2", "dreadlocks"), ["webos"]))

    def test_part_nodes(self):
        """The eMMC partitions' nodes go to init as N:serial (qemu.part_nodes makes them)."""
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(
                targets.boot_conf(
                    H13_AFRO, ["webos"], None, [(3, "partinfo"), (4, "partinfo"), (16, "nvram")]
                )
            )
            f.flush()
            conf = boot_conf(f.name)
        self.assertEqual(
            (conf["PARTINFO_DISK"], conf["PART_NODES"]),
            ("partinfo", "3:partinfo 4:partinfo 16:nvram"),
        )

    def test_init_system(self):
        """webOS 1 to 4 run Upstart, webOS 5 and 6 (jhericurl, kisscurl) systemd with the
        preload-systemd PmLog mode, SimpleSmart sysvinit with no PmLog, and a row mixing them is
        refused."""
        for target in targets.all_targets():
            with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
                f.write(
                    targets.boot_conf(
                        target,
                        ["webos"],
                        {
                            m.name: (MSTAR_DERIVED if m.derived == "mstar-mmap" else ())
                            for m in targets.soc(target).modules
                            if m.derived
                        },
                    )
                )
                f.flush()
                conf = boot_conf(f.name)
            systemd = target[1] in (
                "jhericurl",
                "kisscurl",
                "mullet",
                "number1",
                "ombre",
                "ponytail",
                "queue",
                "spacebuns",
            )
            init = (
                "sysvinit"
                if target[1] in ("simplesmart", "netcast45", "netcast4", "testguest")
                else "systemd"
                if systemd
                else "upstart"
            )
            self.assertEqual(conf["INIT_SYSTEM"], init, targets.name(target))
            self.assertEqual(conf["PMLOG_MODE"] == "preload-systemd", systemd, targets.name(target))
            self.assertEqual(conf["PMLOG_MODE"] == "", init == "sysvinit", targets.name(target))
        row = SYSTEMD_ROW
        with mock.patch.dict(targets.RELEASES, {"systemdtest": row}):
            with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
                f.write(targets.boot_conf(("m2", "systemdtest"), ["webos"]))
                f.flush()
                conf = boot_conf(f.name)
            self.assertEqual(
                (conf["INIT_SYSTEM"], conf["PMLOG_MODE"], conf["PMLOG_JOB"]),
                ("systemd", "preload-systemd", "pm-log-daemon"),
            )
        for bad in (
            row._replace(pmlog_mode="preload-env"),
            row._replace(init_system="upstart"),
            row._replace(init_system="sysvinit"),
        ):
            with mock.patch.dict(targets.RELEASES, {"systemdtest": bad}):
                with self.assertRaises(ValueError, msg=bad.init_system + " " + bad.pmlog_mode):
                    targets.boot_conf(("m2", "systemdtest"), ["webos"])

    def test_userland(self):
        """USERLAND="qemu-user" only when asked; init registers /host/qemu-arm for it."""
        layers = ["normal", "gl", "webos", "soc", "common"]
        self.assertNotIn("USERLAND", targets.boot_conf(("h13", "afro"), layers))
        text = targets.boot_conf(("h13", "afro"), layers, userland="qemu-user")
        self.assertIn('USERLAND="qemu-user"\n', text)
        self.assertNotIn("USERLAND_AARCH64", text)
        # the x86-64 guest kernel's qemu-aarch64; never without qemu-user
        text = targets.boot_conf(("h13", "afro"), layers, userland="qemu-user", aarch64=True)
        self.assertIn('USERLAND="qemu-user"\n', text)
        self.assertIn('USERLAND_AARCH64="qemu-user"\n', text)
        self.assertNotIn("USERLAND", targets.boot_conf(("h13", "afro"), layers, aarch64=True))

    def test_pointer_wakeup_release_and_soc(self):
        """Tested targets use the native cursor-on pulse without enabling untested W20H."""
        self.assertFalse(targets.soc(("k8hp", "queue")).pointer_wakeup)
        self.assertTrue(targets.release(("k8hp", "queue")).pointer_wakeup)
        for target, enabled in (
            (("k8hp", "queue"), True),
            (("k8hp", "ponytail"), True),
            (("k8lp", "mullet"), True),
            (("k8ap", "mullet"), True),
            (("lm21a", "kisscurl"), True),
            (("k7lp", "kisscurl"), True),
            (("k6lp", "jhericurl"), True),
            (("k6hp", "jhericurl"), False),
        ):
            with self.subTest(target=target):
                derived = {
                    m.name: ("rpc_reply=100:108:68", "rpc_create=100:10,201:1")
                    for m in targets.soc(target).modules
                    if m.derived
                }
                text = targets.boot_conf(
                    target, ["normal", "gl", "webos", "soc", "common"], derived
                )
                self.assertEqual('POINTER_WAKEUP="1"' in text, enabled)

    def test_peripheral_serial_and_lgfs(self):
        """Each target's USB serial drivers and lgfs.ko are its stock kernel's and PDM's: the
        drivers modules.builtin (or the decoded config) lists, and lgfs on every line: PDM mounts tfat
        and tntfs or the firmware ships them (webOS 1 to 23, SS1A, NC5U), or it mounts ntfs with LG's
        options (webOS 24 on)."""
        serial = {"pl2303", "option", "sierra", "ipw", "ftdi_sio", "ch341", "cp210x"}
        base = {"pl2303", "option", "sierra"}
        expect = {
            "h13-afro": {"ftdi_sio", "pl2303", "option"},
            "m14-beehive": base | {"ftdi_sio"},
            "m2-dreadlocks": base | {"ipw"},
            "m16p-dreadlocks2": base | {"ftdi_sio"},
            "m16plite-dreadlocks2": base | {"ftdi_sio"},
            "m16pp-goldilocks": base | {"ftdi_sio"},
            "m16pplite-goldilocks": base | {"ftdi_sio"},
            "m16p3-goldilocks2": base | {"ftdi_sio"},
            "o18-goldilocks2": base | {"ftdi_sio"},
            "o18k-goldilocks2": base | {"ftdi_sio"},
            "m2r-dreadlocks2": base | {"ipw"},
            "k2l-dreadlocks": base,
            "k2lp-dreadlocks": base,
            "k3lp-dreadlocks2": base | {"ftdi_sio"},
            "m3-goldilocks": base | {"ipw"},
            "m3r-goldilocks2": base | {"ipw"},
            "lm18a-goldilocks": base | {"ipw"},
            "m2-simplesmart": base,
            "lm14-netcast45": set(),
            "h13-netcast4": set(),
            "lm15u-beehive": base | {"ftdi_sio"},
            "mtka5lr-beehive": {"pl2303", "option", "ipw"},
            "lm21a-kisscurl": base | {"ipw"},
            "lm21u-kisscurl": base | {"ipw"},
            "lm21an-mullet": base | {"ipw"},
            "lm21ann-number1": base | {"ipw"},
            "lm21ann-ombre": base | {"ipw"},
            "lm21ann-ponytail": base | {"ipw"},
            "lm21ann-queue": base | {"ipw", "ftdi_sio", "ch341", "cp210x"},
            "lm21u-ombre": base | {"ipw"},
            "m23-number1": base | {"ftdi_sio"},
            "m23-ombre": base | {"ftdi_sio"},
            "m23-ponytail": base | {"ftdi_sio"},
            "m23-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "o20n-kisscurl": base | {"ftdi_sio"},
            "e60n-kisscurl": base | {"ftdi_sio"},
            "o22-mullet": base | {"ftdi_sio"},
            "o22-number1": base | {"ftdi_sio"},
            "o22n-number1": base | {"ftdi_sio"},
            "o22n2-ombre": base | {"ftdi_sio"},
            "o22n2-spacebuns": base | {"ftdi_sio"},
            "o22-ponytail": base | {"ftdi_sio"},
            "o22-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "o22-ombre": base | {"ftdi_sio"},
            "o22n-ombre": base | {"ftdi_sio"},
            "o22n-ponytail": base | {"ftdi_sio"},
            "o22n-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "o22n2-ponytail": base | {"ftdi_sio"},
            "o22n2-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "o24-ombre": base | {"ftdi_sio"},
            "o24-ponytail": base | {"ftdi_sio"},
            "o24-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "o24n-ponytail": base | {"ftdi_sio"},
            "o22n3-ponytail": base | {"ftdi_sio"},
            "o24n-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "o22n3-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "o26-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "k8hp-number1": base,
            "k8hp-ombre": base,
            "k8hp-ponytail": base,
            "k8hp-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "k8hpp-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "k8lpn-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "k8lpn2-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "k25lp-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "k24-queue": base | {"ftdi_sio", "ch341", "cp210x"},
            "k24n-queue": base | {"ftdi_sio", "ch341", "cp210x"},
        }
        for target in targets.all_targets():
            if targets.platform(target) == "Test":
                continue  # the test guest: none of a stock kernel's
            name = targets.name(target)
            mods = targets.release(target).peripherals
            if name in expect:
                want_serial = expect[name]
            else:  # Realtek's webOS 4.5 to 26: the release's own set
                want_serial = base
            with self.subTest(target=name):
                self.assertEqual(len(mods), len(set(mods)))
                self.assertEqual(set(mods) & serial, want_serial)
                self.assertIn("lgfs", mods)
                for backend in ("vfat", "ntfs3"):  # after the filesystems it stands on
                    if backend in mods:
                        self.assertGreater(mods.index("lgfs"), mods.index(backend))

    def test_sit(self):
        """sit.ko (IPv6-in-IPv4, whose fallback device is sit0) loads where the release's stock
        kernel has SIT (every webOS release's but WT1H's and W15L's, which have no IPv6; modules.builtin of the
        firmware's rootfs), and nowhere else: NetCast 4.5's and SimpleSmart's networkmanager takes
        sit0 for its Wi-Fi interface and its private ioctl overruns a stack buffer
        (docs/research/netcast-networkmanager-wifi-country.md)."""
        for target in targets.all_targets():
            name = targets.name(target)
            mods = targets.release(target).peripherals
            if targets.platform(target) == "Test" or name in (
                "h13-afro",
                "h13-netcast4",
                "lm14-netcast45",
                "m2-simplesmart",
                "mtka5lr-beehive",
            ):
                self.assertNotIn("sit", mods, name)
            else:
                self.assertIn("sit", mods, name)

    def test_golden(self):
        """Every target's boot.conf is byte for byte tests/unit/launch/data/boot-conf/<target>.conf (with made-up
        derived parameters where the chip has some): a change to the tables' output is a change there."""
        for target in targets.all_targets():
            derived = {
                m.name: (
                    MSTAR_DERIVED
                    if m.derived == "mstar-mmap"
                    else ("rpc_reply=100:108:68", "rpc_create=100:10,201:1")
                )
                for m in targets.soc(target).modules
                if m.derived
            }
            with open(os.path.join(HERE, "data", "boot-conf", targets.name(target) + ".conf")) as f:
                self.assertEqual(
                    targets.boot_conf(target, ["normal", "gl", "webos", "soc", "common"], derived),
                    f.read(),
                    targets.name(target),
                )

    def test_webos_order(self):
        """Every webOS release the targets use is in WEBOS_ORDER, each after the release it is _after,
        and config RELEASES prints the order (meson.build's webos_reached)."""
        for target in targets.all_targets():
            if target[1] not in ("simplesmart", "netcast45", "netcast4", "testguest"):
                self.assertIn(target[1], targets.WEBOS_ORDER)
        order = targets.WEBOS_ORDER
        for older, newer in (("ombre", "spacebuns"), ("ponytail", "queue"), ("queue", "spacebuns")):
            self.assertLess(order.index(older), order.index(newer))
        r = subprocess.run(
            [sys.executable, os.path.join(TOP, "host/scripts/vm-launcher"), "config", "RELEASES"],
            stdout=subprocess.PIPE, text=True, check=True,
        )  # fmt: skip
        self.assertEqual(tuple(r.stdout.split()), order)

    @unittest.skipUnless(shutil.which("mksquashfs"), "needs squashfs-tools")
    def test_rootfs_entries(self):
        """The shim rules' listing comes from the rootfs pak, not from an extracted tree beside it."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d, "src")
            (root / "usr/lib/nyx/modules").mkdir(parents=True)
            (root / "usr/lib/libsestore.so.0.1.0").write_bytes(b"")
            (root / "usr/lib/nyx/modules/nyxSecureStore.module").write_bytes(b"")
            fw = Path(d, "fw")
            (fw / "rootfs/usr/lib").mkdir(parents=True)
            (fw / "rootfs/usr/lib/extracted-only.so").write_bytes(b"")
            subprocess.run(
                ["mksquashfs", str(root), str(fw / "rootfs.pak"), "-quiet", "-noappend"],
                check=True, stdout=subprocess.DEVNULL,
            )  # fmt: skip
            self.assertEqual(
                paths.rootfs_entries(str(fw), ["usr/lib", "/usr/lib/nyx/modules/"]),
                ["usr/lib/libsestore.so.0.1.0", "usr/lib/nyx", "usr/lib/nyx/modules/nyxSecureStore.module"],
            )  # fmt: skip
            (fw / "rootfs.pak").unlink()
            with self.assertRaises(ValueError):
                paths.rootfs_entries(str(fw), ["usr/lib"])

    def test_tables(self):
        """Every target MODELS names has both axes; the launcher's disks and paks are the tables'; a
        pak lower is an attached pak."""
        for target in targets.all_targets():
            self.assertTrue(
                target[0] in targets.SOCS or tuple(target) in targets.TARGET_SOCS,
                targets.name(target),
            )
            self.assertIn(target[1], targets.RELEASES)
            for disk in targets.emmc_disks(target):
                self.assertIn(
                    disk,
                    targets.release(target).partitions.values(),
                    f"{targets.name(target)}: {disk} needs a partition for sizing and its guest node",
                )
            self.assertEqual([p.mount for p in targets.paks(target)].count("/"), 1)
            for lower in targets.release(target).pak_lowers:
                self.assertIn(lower.serial, [p.serial for p in targets.paks(target)])
        self.assertEqual(
            targets.pak_files(H13_AFRO),
            ("rootfs", "tvservice-dvb", "otncabi-dvb", "otycabi-dvb", "fonts", "smartkey"),
        )
        self.assertEqual(
            targets.state_parts(H13_AFRO),
            (
                "db8",
                "cmn_data",
                "appstore",
                "nvram",
                "eeprom",
                "partinfo",
                "authcxt",
                "sestore",
                "sedata",
            ),
        )
        self.assertEqual(targets.settings_parts(H13_AFRO), ("nvram", "eeprom"))

    @unittest.skipUnless(shutil.which("mksquashfs"), "needs squashfs-tools")
    def test_broadcast(self):
        """A firmware's broadcast standard is its model_id.json region (test_firmware_broadcast.py);
        a pak named for a region (-dvb, -atsc, -arib; none for -global, none for several) picks the
        firmware's sibling of a pak, and the board is the row's for the standard. A firmware whose
        paks are the target's own (every target's, W22O's global, W22H's ARIB) gets exactly what it
        got without a firmware."""
        self.assertEqual(
            targets.broadcast_of_paks(["rootfs.pak", "otncabi-arib.pak", "fonts-arib.pak"]), "arib"
        )
        self.assertEqual(targets.broadcast_of_paks(["tvservice-dvb", "rootfs"]), "dvb")
        self.assertIsNone(targets.broadcast_of_paks(["otncabi-global.pak", "fonts-global.pak"]))
        self.assertIsNone(targets.broadcast_of_paks(["a-dvb.pak", "b-arib.pak"]))
        self.assertIsNone(targets.firmware_broadcast(None))
        self.assertIsNone(targets.firmware_broadcast("/nonexistent"))

        def fw(d, names):
            os.makedirs(d)
            for n in names:
                open(os.path.join(d, n + ".pak"), "w").close()
            return d

        o22q = ("o22", "queue")
        with tempfile.TemporaryDirectory() as tmp:
            for target in targets.all_targets():
                d = fw(os.path.join(tmp, targets.name(target)), targets.pak_files(target))
                self.assertEqual(
                    targets.release(target, d), targets.release(target), targets.name(target)
                )
                self.assertEqual(
                    targets.board_cmdline_src(target, targets.board_broadcast(target, d)),
                    targets.board_cmdline_src(target),
                    targets.name(target),
                )
                if target != ("lm14", "netcast45"):
                    self.assertEqual(
                        targets.boot_conf(
                            target,
                            [],
                            {m.name: () for m in targets.soc(target).modules if m.derived},
                            fw=d,
                            broadcast=targets.board_broadcast(target, d),
                        ),
                        targets.boot_conf(
                            target,
                            [],
                            {m.name: () for m in targets.soc(target).modules if m.derived},
                        ),
                        targets.name(target),
                    )
            jp = fw(
                os.path.join(tmp, "o22-jp"),
                [f.replace("-global", "-arib") for f in targets.pak_files(o22q)],
            )
            # W22O's Japanese queue build: its model_id.json says ARIB
            src = os.path.join(tmp, "o22-jp-root", "etc", "starfish")
            os.makedirs(src)
            with open(os.path.join(src, "model_id.json"), "w") as f:
                f.write('{"otaid": "HE_DTV_W22O_AFABJAPU", "machine": "O22", "region": "ARIB"}')
            os.unlink(os.path.join(jp, "rootfs.pak"))
            subprocess.run(
                ["mksquashfs", os.path.join(tmp, "o22-jp-root"), os.path.join(jp, "rootfs.pak"),
                 "-quiet", "-no-progress"],
                check=True, stdout=subprocess.DEVNULL,
            )  # fmt: skip
            self.assertEqual(targets.firmware_broadcast(jp), "arib")
            self.assertEqual(
                [p.file for p in targets.paks(o22q, jp)],
                ["rootfs", "bsppart", "otncabi-arib", "otycabi-arib", "fonts-arib", "license"],
            )
            self.assertEqual(
                targets.pak_files(o22q, jp),
                tuple(f.replace("-global", "-arib") for f in targets.pak_files(o22q)),
            )
            arib = targets.board_broadcast(o22q, jp)
            self.assertEqual(arib, "arib")
            self.assertEqual(
                targets.board_cmdline_src(o22q, arib),
                "guest/layers/soc/o22/rootfs/etc/emulator/board-arib.cmdline",
            )
            self.assertIn(
                'BOARD_CMDLINE="/etc/emulator/board-arib.cmdline"',
                targets.boot_conf(o22q, [], fw=jp, broadcast=arib),
            )
            words = dict(w.split("=", 1) for w in nvm.board_words(o22q, broadcast=arib) if "=" in w)
            self.assertEqual(
                (words["modelName"], words["countryGrp"], words["tvregion"], words["hwopt"][0]),
                ("OLED55C2PJA", "18", "ARIB", "0"),
            )
            # the same C2 as the global board, with the region's words
            glob = dict(w.split("=", 1) for w in nvm.board_words(o22q, broadcast="dvb") if "=" in w)
            self.assertEqual(words["hwopt"][1:], glob["hwopt"][1:])
            self.assertEqual(words["ToolOpt"], glob["ToolOpt"])
            # a chip with no board for the paks' standard keeps its own
            self.assertEqual(
                targets.board_cmdline_src(
                    ("o22n", "number1"), targets.board_broadcast(("o22n", "number1"), jp)
                ),
                targets.board_cmdline_src(("o22n", "number1")),
            )
            # both standards' paks: none, nothing renamed
            both = fw(
                os.path.join(tmp, "both"),
                list(targets.pak_files(o22q)) + ["otncabi-arib", "tvservice-dvb"],
            )
            self.assertEqual(targets.release(o22q, both), targets.release(o22q))

            # a global firmware boots the US model by default (targets.US_BOARD), and another
            # standard's model with vm.toml's firmware.broadcast (BROADCAST)
            us = fw(os.path.join(tmp, "o22-us"), targets.pak_files(o22q))
            self.assertEqual(targets.default_broadcast(o22q), "atsc")
            self.assertEqual(targets.board_broadcast(o22q, us), "atsc")
            atsc = targets.board_broadcast(o22q, us, "atsc")
            self.assertEqual(atsc, "atsc")
            self.assertEqual(targets.release(o22q, us), targets.release(o22q))
            self.assertEqual(
                targets.board_cmdline_src(o22q, atsc),
                "guest/layers/soc/o22/rootfs/etc/emulator/board-atsc.cmdline",
            )
            self.assertEqual(targets.board_cmdline_src(o22q), targets.board_cmdline_src(o22q, atsc))
            self.assertIn(
                'BOARD_CMDLINE="/etc/emulator/board-atsc.cmdline"',
                targets.boot_conf(o22q, [], fw=us, broadcast=atsc),
            )
            words = dict(w.split("=", 1) for w in nvm.board_words(o22q, broadcast=atsc) if "=" in w)
            self.assertEqual(
                (words["modelName"], words["countryGrp"], words["tvregion"], words["hwopt"][0]),
                ("OLED55C2PUA", "2", "ATSC", "5"),
            )
            self.assertEqual(words["hwopt"][1:], glob["hwopt"][1:])
            dvb = targets.board_broadcast(o22q, us, "dvb")
            self.assertEqual(
                targets.board_cmdline_src(o22q, dvb),
                "guest/layers/soc/o22/rootfs/etc/emulator/board-dvb.cmdline",
            )
            self.assertEqual(
                (glob["modelName"], glob["countryGrp"], glob["tvregion"]),
                ("OLED55C21LA", "4", "DVB"),
            )
            # the paks' standard stands; another is refused, as is one the chip has no board for
            self.assertEqual(targets.board_broadcast(o22q, jp, "arib"), "arib")
            with self.assertRaisesRegex(
                targets.Error, "broadcast 'atsc', but the firmware is 'arib'"
            ):
                targets.board_broadcast(o22q, jp, "atsc")
            with self.assertRaisesRegex(targets.Error, "has no board for broadcast 'arib'"):
                targets.board_broadcast(("o22n", "number1"), us, "arib")
            with self.assertRaisesRegex(
                targets.Error, "lm21ann-number1 has no board for broadcast 'atsc'"
            ):
                targets.board_broadcast(("lm21ann", "number1"), us, "atsc")

    def test_touch_screen(self):
        """Only a model with a touch screen (the StanbyME, lm21u) gets QEMU's virtio-multitouch-pci,
        with the tablet bound to the display so the mouse's buttons stay the pointer's."""
        self.assertEqual(
            [targets.name(p) for p in targets.all_targets() if targets.soc(p).touch],
            ["lm21u-kisscurl", "lm21u-ombre"],
        )
        self.assertEqual(qemu.input_devices(False), ["-device", "virtio-tablet-pci"])
        self.assertEqual(
            qemu.input_devices(True),
            [
                "-device",
                f"virtio-tablet-pci,display={qemu.GPU_ID},head=0",
                "-device",
                "virtio-multitouch-pci",
            ],
        )
        for release, region in (("kisscurl", ("4", "DVB")), ("ombre", ("4", "DVB"))):
            board = dict(w.split("=", 1) for w in nvm.board_words(("lm21u", release)) if "=" in w)
            self.assertEqual((board["modelName"], board["chip"]), ("27ART10AKPL", "LM21UA0"))
            self.assertEqual((board["countryGrp"], board["tvregion"]), region)

    def test_portrait(self):
        """Only a model that turns to portrait (the StanbyME, lm21u) is rotated: the guest's report
        is the last "rotation=N" line, and the viewers turn the panel's picture the other way: the
        compositor draws the UI turned clockwise by the angle, upright on a set turned that far
        counter-clockwise."""
        from beanjar import rotation

        self.assertEqual(
            [targets.name(p) for p in targets.all_targets() if targets.soc(p).portrait],
            ["lm21u-kisscurl", "lm21u-ombre"],
        )
        self.assertEqual([rotation.picture_turn(a) for a in rotation.ANGLES], [0, 270, 180, 90])
        self.assertEqual([rotation.clockwise(a) for a in rotation.ANGLES], [270, 0, 90, 180])
        self.assertEqual(rotation.parse("rotation=0\nrotation=90\njunk\n"), 90)
        self.assertIsNone(rotation.parse("rotation=45\n"))
        self.assertEqual([rotation.angle_value(w) for w in ("off", "0", "270")], [0, 0, 270])
        with self.assertRaises(ValueError):
            rotation.angle_value("45")
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(rotation.current(d))
            Path(d, rotation.STATUS).write_text("rotation=270\n")
            self.assertEqual(rotation.current(d), 270)
        vm = types.SimpleNamespace(
            name="tv", portrait=lambda: False, target=lambda: ("lm21a", "kisscurl")
        )
        with self.assertRaisesRegex(core.Error, "does not turn to portrait"):
            rotation.require(vm)

    def test_portrait_sensor(self):
        """The StanbyME (N21D, lm21u-kisscurl and lm21u-ombre) turns by its accelerometer: the
        sensor is lgemu's accel device at N21D's ACCELERATION_SENSOR_BMA422 (channel 3, 7-bit
        0x19), its vector for each angle lands in msc's band of that orientation (calOrient4BAM422:
        -y landscape, +x reversed_portrait, +y reversed_landscape, -x portrait, 512 counts per g),
        and a start gives it the angle rotate saved."""
        from beanjar import rotation

        self.assertEqual(
            [targets.name(p) for p in targets.all_targets() if rotation.sensor(p)],
            ["lm21u-kisscurl", "lm21u-ombre"],
        )
        for release in ("kisscurl", "ombre"):
            self.assertIn("accel:3:0x19", " ".join(targets.soc(("lm21u", release)).lgemu))

        def orientation(x, y):
            x, y = x * 512 // 1000, y * 512 // 1000
            if abs(x) <= 255:
                return (
                    "landscape"
                    if -601 <= y <= -354
                    else ("reversed_landscape" if 354 <= y <= 601 else None)
                )
            if abs(y) <= 255:
                return (
                    "portrait"
                    if -601 <= x <= -354
                    else ("reversed_portrait" if 354 <= x <= 601 else None)
                )
            return None

        self.assertEqual(
            [orientation(*rotation.gravity(a)[:2]) for a in rotation.ANGLES],
            ["landscape", "reversed_portrait", "reversed_landscape", "portrait"],
        )
        for a in rotation.ANGLES:
            x, y, z = rotation.gravity(a)
            self.assertEqual(x * x + y * y + z * z, 1000 * 1000)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rotation.saved(d), 0)
            self.assertEqual(rotation.launch_params(("lm21u", "ombre"), d), ("accel_mg=0,-1000,0",))
            rotation.save(d, 270)
            self.assertEqual(rotation.saved(d), 270)
            self.assertEqual(rotation.launch_params(("lm21u", "ombre"), d), ("accel_mg=-1000,0,0",))
            self.assertEqual(rotation.launch_params(("lm21a", "kisscurl"), d), ())
            params = targets.module_params(
                ("lm21u", "ombre"), vm={"lgemu": rotation.launch_params(("lm21u", "ombre"), d)}
            )
            self.assertEqual(params["lgemu"][-1], "accel_mg=-1000,0,0")

    def test_light_sensor(self):
        """The ambient light sensor is the board's: its digitalEye names the type, the chip
        the channel (libdile_i2c's LIGHT_SENSOR rows: 5 on LM21/K7/K8, 1 on O22), at 7-bit 0x10;
        a board with digitalEye off, or a chip without the rows, has none. The prepared device
        joins the table's i2c_dev, one parameter."""
        cases = {
            (("lm21a", "kisscurl"), "OTP_eyeSensor"): "light2:5:0x10",
            (("k8lpn2", "ombre"), "OTP_eyeSensor"): "light2:5:0x10",
            (("o22", "mullet"), "eye_sensor"): "light1:1:0x10",
            (("o22n", "number1"), "OTP_eyeSensor"): "light2:1:0x10",
            (("k8hp", "mullet"), "eye_sensor"): "light1:5:0x10",
            (("lm21u", "kisscurl"), "off"): None,
            (("lm21a", "kisscurl"), None): None,
            (("k6lp", "jhericurl"), "eye_sensor"): None,
            (("k5lp", "goldilocks2"), "eye_sensor"): None,
        }
        for (target, eye), want in cases.items():
            self.assertEqual(targets.light_dev(target, eye), want, (target, eye))
        target = ("lm21a", "kisscurl")
        lgemu = targets.module_params(target, {"lgemu": ("i2c_dev=light2:5:0x10",)})["lgemu"]
        devs = [w for w in lgemu if w.startswith("i2c_dev=")]
        self.assertEqual(devs, ["i2c_dev=nvm:4:0x50,amp:3:0x2a,amp:3:0x2b,light2:5:0x10"])
        self.assertEqual(lgemu.index(devs[0]), 1)  # where the table has it, after i2c_buses
        # the StanbyME keeps its accelerometer beside a sensor it would have
        lgemu = targets.module_params(("lm21u", "ombre"), {"lgemu": ("i2c_dev=light2:5:0x10",)})[
            "lgemu"
        ]
        self.assertIn("accel:3:0x19,light2:5:0x10", " ".join(lgemu))

    def test_tof_sensor(self):
        """The ToF motion sensor is the board's: its supportMotionDetectSensor names the
        TMF8821 ("ToF_TBD_22Y"), the chip the channel (libdile_i2c's TMF882X_SENSOR_N row: 1 on
        O22), at 7-bit 0x41; "off", the micom-relayed camera sensor and N21D's one-bit item (a
        number), or a chip without the row, have none. It joins the table's i2c_dev."""
        cases = {
            (("o22n", "number1"), "ToF_TBD_22Y"): "tof:1:0x41",
            (("o22n2", "ombre"), "ToF_TBD_22Y"): "tof:1:0x41",
            (("o22", "mullet"), "ToF_TBD_22Y"): "tof:1:0x41",
            (("o22", "mullet"), "off"): None,
            (("o22n", "number1"), "CAM_ART&R2_21Y"): None,
            (("lm21u", "ombre"), 1): None,
            (("lm21u", "ombre"), "ToF_TBD_22Y"): None,
            (("k8hp", "mullet"), "ToF_TBD_22Y"): None,
            (("o22", "mullet"), None): None,
        }
        for (target, sensor), want in cases.items():
            self.assertEqual(targets.tof_dev(target, sensor), want, (target, sensor))
        derived = {"lgemu": ("i2c_dev=light2:1:0x10", "i2c_dev=tof:1:0x41")}
        lgemu = targets.module_params(("o22n", "number1"), derived)["lgemu"]
        devs = [w for w in lgemu if w.startswith("i2c_dev=")]
        self.assertEqual(devs, ["i2c_dev=nvm:7:0x50,light2:1:0x10,tof:1:0x41"])

    def test_thermalinfo(self):
        """/proc/thermalinfo where the firmware has fancontroller (lm21*, k7, k8*, kf23f, k24*, k25*,
        m23, the O22 family o2*, O20N and E60N),
        with the IDDQ files from webOS 23 (number1) on; not on older chips."""
        for target in targets.all_targets():
            words = targets.module_params(target).get("lgemu", ())
            fan = target[0].startswith(
                ("lm21", "k7", "k8", "kf23f", "k24", "k25", "m23", "o2", "e60n")
            )
            iddq = fan and targets.WEBOS_ORDER.index(target[1]) >= targets.WEBOS_ORDER.index(
                "number1"
            )
            with self.subTest(target=targets.name(target)):
                self.assertEqual("thermalinfo=1" in words, fan)
                self.assertEqual(any(w.startswith("iddq=") for w in words), iddq)

    def test_sensor_commands(self):
        """beanjar sensor light, temperature and presence: their values, the console lines and the
        reports."""
        from beanjar import sensors

        self.assertEqual(sensors.count_value("3000"), 3000)
        self.assertEqual(sensors.celsius_value("-5"), -5)
        for bad in ("-1", "65536", "1.5", "x"):
            with self.assertRaises(ValueError):
                sensors.count_value(bad)
        for bad in ("201", "-41", "45C"):
            with self.assertRaises(ValueError):
                sensors.celsius_value(bad)
        line = sensors.light_command((10, 20))
        self.assertIn("echo 10,20 > /sys/module/lgemu/parameters/light", line)
        self.assertNotIn("parameters/light &&", sensors.light_command())
        # the typed line comes back too: only the guest's own answer counts
        self.assertEqual(sensors.parse_light(line + "\nlight=10,20\n"), (10, 20))
        self.assertIsNone(sensors.parse_light(line + "\nnolight\n"))
        with self.assertRaises(core.Error):
            sensors.parse_light(line)
        for word, mm in (("present", 1500), ("absent", 0), ("2500", 2500), ("100", 100)):
            self.assertEqual(sensors.presence_value(word), mm)
        for bad in ("99", "5001", "-1", "near", "1.5"):
            with self.assertRaises(ValueError):
                sensors.presence_value(bad)
        line = sensors.presence_command(1500)
        self.assertIn("echo 1500 > /sys/module/lgemu/parameters/presence", line)
        self.assertNotIn("parameters/presence &&", sensors.presence_command())
        self.assertEqual(sensors.parse_presence(line + "\npresence=1500\n"), 1500)
        self.assertEqual(sensors.parse_presence(line + "\npresence=0\n"), 0)
        self.assertIsNone(sensors.parse_presence(line + "\nnotof\n"))
        with self.assertRaises(core.Error):
            sensors.parse_presence(line)
        line = sensors.temperature_command(80)
        self.assertIn("echo 80 > /sys/module/lgemu/parameters/soc_temp", line)
        self.assertEqual(sensors.parse_temperature(line + "\nsoc_temperature=80\n"), 80)
        self.assertEqual(sensors.parse_temperature("soc_temperature=-5\n"), -5)
        self.assertIsNone(sensors.parse_temperature(line + "\nnothermal\n"))
        with self.assertRaises(core.Error):
            sensors.parse_temperature(line)

    def test_portrait_report(self):
        """The guest reports msc's orientation, not the sensor's angle (beanjar-rotation --parse:
        rotation.ORIENTATIONS), and rotate returns what it reports: at 180 N21D's webOS 24 msc
        keeps the orientation it had ("don't support reversed_landscape"), its webOS 6 takes it."""
        from beanjar import rotation

        script = os.path.join(TOP, "guest", "initramfs", "rootfs", "bin")
        replies = (
            "".join(
                f'{{"returnValue": true, "orientation": "{o}", "subscribed": true}}\n'
                for o in rotation.ORIENTATIONS
            )
            + '{"returnValue": false, "errorText": "no"}\n'
        )
        out = subprocess.run(
            ["sh", os.path.join(script, "beanjar-rotation"), "--parse"],
            input=replies,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual(
            out.splitlines(), [f"rotation={a}" for a in rotation.ORIENTATIONS.values()]
        )
        self.assertEqual(sorted(rotation.ORIENTATIONS.values()), list(rotation.ANGLES))
        self.assertTrue(rotation.refused(("lm21u", "ombre"), 180))
        self.assertEqual(
            [a for a in rotation.ANGLES if rotation.refused(("lm21u", "kisscurl"), a)], []
        )
        self.assertEqual(
            [a for a in rotation.ANGLES if rotation.refused(("lm21u", "ombre"), a)], [180]
        )
        for target, angle, reported, refused in (
            (("lm21u", "ombre"), 180, 270, True),
            (("lm21u", "kisscurl"), 180, 180, False),
        ):
            with tempfile.TemporaryDirectory() as d:
                status = Path(d, rotation.STATUS)
                status.write_text("rotation=270\n")
                vm = types.SimpleNamespace(
                    name="tv",
                    run_dir=d,
                    state_dir=d,
                    portrait=lambda: True,
                    target=lambda target=target: target,
                    state=lambda: (core.RUNNING, ""),
                    pid=lambda: 1,
                )

                def turn(vm, angle, status=status, refused=refused):
                    if not refused:  # msc takes it and the guest reports it
                        with open(status, "a") as f:
                            f.write(f"rotation={angle}\n")

                with mock.patch.object(rotation, "turn_sensor", turn):
                    data = rotation.rotate(vm, angle, wait=2, settle=0.3)
                self.assertEqual(
                    (data["rotation"], data["refused"], data["changed"]),
                    (reported, refused, not refused),
                )

    def test_us_default_boards(self):
        """Prefer the US model: a chip whose global firmware ships on US retail models boots its US
        board (ATSC, the US area digit, configd's USA RMM group 2) unless the VM chooses dvb; the
        European board is the same hardware with the European region. Chips whose OTA IDs have no
        US model keep their single board."""
        us_chips = {
            "k7lp": "65UP8000PUR",
            "k8lp": "75NANO75UQA",
            "k8ap": "32LQ630BPUA",
            "k8hp": "65QNED85UQA",
            "k8hpp": "86QNED80URA",
            "k8lpn": "75UR8000AUA",
            "k8lpn2": "43UT8000AUA",
            "kf23f": "32LR655BPUA",
            "k25lp": "43UA7500ZUA",
            "k24": "55QNED85TUA",
            "k24n": "55QNED85AUA",
            "lm21an": "70UQ8000AUB",
            "m23": "55UR9000PUA",
            "o22": "OLED55C2PUA",
            "o22n": "OLED77C3PUA",
            "o22n2": "OLED55C4PUA",
            "o22n3": "OLED55C5PUA",
            "o24n": "OLED55G5WUA",
            "o24": "OLED55G4SUB",
            "o26": "OLED55G6WUA",
        }
        seen = set()
        for target in targets.all_targets():
            name = targets.name(target)
            boards = dict(targets.soc(target).broadcast_boards)
            if target[0] not in us_chips:
                self.assertIsNone(targets.default_broadcast(target), name)
                self.assertNotIn("atsc", boards, name)
                continue
            seen.add(target[0])
            self.assertEqual(targets.default_broadcast(target), "atsc", name)
            self.assertEqual(targets.board_cmdline(target), targets.US_BOARD, name)
            # W22H's other board is the Japanese one (no European board is derived)
            other = "arib" if target[0] == "k8hp" else "dvb"
            if other == "dvb":
                self.assertEqual(boards["dvb"], targets.EU_BOARD, name)
            us = dict(w.split("=", 1) for w in nvm.board_words(target) if "=" in w)
            eu = dict(w.split("=", 1) for w in nvm.board_words(target, broadcast=other) if "=" in w)
            self.assertEqual(us["modelName"], us_chips[target[0]], name)
            self.assertEqual(us["countryGrp"], "2", name)
            self.assertEqual(us["hwopt"][0], "5", name)
            self.assertNotEqual(eu["hwopt"][0], "5", name)
            # the same hardware: every other hwopt digit, modelopt, chip, eMMC and RAM
            for k in ("modelopt", "chip", "emmc_size"):
                self.assertEqual(us.get(k), eu.get(k), f"{name} {k}")
            self.assertEqual(us["hwopt"][1:], eu["hwopt"][1:], name)
            self.assertEqual(nvm.board_ram(target), nvm.board_ram(target, broadcast=other), name)
            self.assertEqual(
                nvm.board_graphic(target), nvm.board_graphic(target, broadcast=other), name
            )
            if "tvregion" in eu:
                self.assertEqual((us["tvregion"], eu["tvregion"]), ("ATSC", other.upper()), name)
            self.assertEqual(len(us["ToolOpt"].split(":")), len(eu["ToolOpt"].split(":")), name)
        self.assertEqual(seen, set(us_chips))
        # US-less lines keep one board
        for target in (("lm21ann", "number1"), ("lm21a", "kisscurl")):
            self.assertEqual(targets.soc(target).broadcast_boards, (), targets.name(target))
            self.assertEqual(targets.board_cmdline(target), "/etc/emulator/board.cmdline")

    def test_release_family(self):
        """The release is the codename family of etc/starfish-release; import refuses one this tree
        does not boot and one that is not the model's."""
        for text, family in (
            ("Rockhopper release 1.4.0-2341 (afro-ashley)\n", "afro"),
            ("Rockhopper release 2.2.3-178 (beehive-biscayne)\n", "beehive"),
            ("Rockhopper release 3.4.3-6213 (dreadlocks-digya)\n", "dreadlocks"),
            ("Rockhopper release 3.5.0-1 (dreadlocks2-jhericurl)\n", "dreadlocks2"),
            ("Rockhopper release 4.4.3-22 (goldilocks-gorongosa)\n", "goldilocks"),
            ("Rockhopper release 4.10.2-29 (goldilocks2-grampians)\n", "goldilocks2"),
            ('VERSION_ID="3.4.3"\n', None),
        ):
            self.assertEqual(targets.release_family(text), family)
        from beanjar import firmware

        where = "rootfs.pak's etc/starfish-release"
        with tempfile.TemporaryDirectory(prefix="beanjar-release-") as tmp:
            fw = os.path.join(tmp, FW_NAME)
            os.mkdir(fw)
            with mock.patch.object(paths, "firmware_release", return_value=("afro", where)):
                self.assertEqual(firmware.target_of(fw), H13_AFRO)
            with mock.patch.object(paths, "firmware_release", return_value=("jhericurl", where)):
                with self.assertRaisesRegex(
                    core.Error, "WT1H jhericurl firmware might be unsupported"
                ):
                    firmware.target_of(fw)
            with mock.patch.object(paths, "firmware_release", return_value=("beehive", where)):
                with self.assertRaisesRegex(
                    core.Error, r"WT1H beehive firmware might be unsupported"
                ):
                    firmware.target_of(fw)
            with mock.patch.object(paths, "firmware_release", return_value=(None, "no rootfs.pak")):
                with self.assertRaisesRegex(
                    core.Error, "no rootfs.pak: its webOS release is unknown"
                ):
                    firmware.target_of(fw)
            # the release is read from the rootfs itself
            self.assertEqual(paths.firmware_release(fw), (None, "no rootfs.pak"))
            shutil.copyfile(TRUST_PAK, os.path.join(fw, "rootfs.pak"))
            self.assertEqual(paths.firmware_release(fw), ("afro", where))

    def test_targets_of_models(self):
        """MODELS is model -> {release: chip}: a firmware's target is its release's chip and the release;
        the targets are every row's, each once."""
        self.assertEqual(targets.of_firmware("WT1H", "afro"), H13_AFRO)
        self.assertIsNone(targets.of_firmware("WT1H", "beehive"))
        self.assertEqual(targets.of_firmware("W20L", "jhericurl"), ("k6lp", "jhericurl"))
        self.assertIsNone(targets.of_firmware("W20L", "goldilocks2"))
        self.assertEqual(targets.of_firmware("W21P", "kisscurl"), ("k7lp", "kisscurl"))
        self.assertEqual(targets.of_firmware("N21D", "kisscurl"), ("lm21u", "kisscurl"))
        self.assertEqual(targets.of_firmware("N21D", "ombre"), ("lm21u", "ombre"))
        self.assertIsNone(targets.of_firmware("W21U", "kisscurl"))
        self.assertEqual(
            [targets.name(p) for p in targets.all_targets()],
            [
                "e60n-kisscurl",
                "h13-afro",
                "h13-netcast4",
                "k24-ombre",
                "k24-ponytail",
                "k24-queue",
                "k24n-ponytail",
                "k24n-queue",
                "k25lp-ponytail",
                "k25lp-queue",
                "k2l-dreadlocks",
                "k2lp-dreadlocks",
                "k3lp-dreadlocks2",
                "k5lp-goldilocks2",
                "k6hp-jhericurl",
                "k6lp-jhericurl",
                "k7lp-kisscurl",
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
                "kf23f-number1",
                "lm14-netcast45",
                "lm15u-beehive",
                "lm18a-goldilocks",
                "lm21a-kisscurl",
                "lm21an-mullet",
                "lm21ann-number1",
                "lm21ann-ombre",
                "lm21ann-ponytail",
                "lm21ann-queue",
                "lm21u-kisscurl",
                "lm21u-ombre",
                "m14-beehive",
                "m16p-dreadlocks2",
                "m16p3-goldilocks2",
                "m16plite-dreadlocks2",
                "m16pp-goldilocks",
                "m16pplite-goldilocks",
                "m2-dreadlocks",
                "m2-simplesmart",
                "m23-number1",
                "m23-ombre",
                "m23-ponytail",
                "m23-queue",
                "m2r-dreadlocks2",
                "m3-goldilocks",
                "m3r-goldilocks2",
                "mtka5lr-beehive",
                "o18-goldilocks2",
                "o18k-goldilocks2",
                "o20n-kisscurl",
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
        self.assertEqual(targets.of_firmware("W24G", "spacebuns"), ("o22n2", "spacebuns"))
        self.assertEqual(targets.release(("o22n2", "spacebuns")).version, "12")
        self.assertEqual(targets.of_firmware("SS1A", "simplesmart"), ("m2", "simplesmart"))
        self.assertEqual(targets.of_firmware("NC5U", "netcast45"), ("lm14", "netcast45"))
        self.assertEqual(targets.of_firmware("NC4H", "netcast4"), ("h13", "netcast4"))
        self.assertEqual(targets.by_name("m2-dreadlocks"), ("m2", "dreadlocks"))
        self.assertIsNone(targets.by_name("h13-beehive"))
        self.assertTrue(targets.supported(("k5lp", "goldilocks2")))
        self.assertFalse(targets.supported(("h13", "beehive")))

    def test_ram_per_model(self):
        """Each model's own DDR, its board.cmdline's hwopt digit 11 in its release's DDR_SIZE_TYPE_T:
        '1' 1.25 GB before W17H, '3' 1.5 GB from W17H on (where '1' is 1 GB); WT1H's lxboot prints '4'
        (3 GB) on its UHD path, W15M's straps '2' (1.5 GB)."""
        self.assertEqual(targets.ram(H13_AFRO, "202212001001002000"), 1280)
        self.assertEqual(targets.ram(("m16p", "dreadlocks2"), "202207002001002000"), 1024)
        with self.assertRaisesRegex(targets.Error, "digit 11"):
            targets.ram(H13_AFRO, "202212001009002000")
        with self.assertRaisesRegex(targets.Error, "digit 11"):
            targets.ram(H13_AFRO, None)
        self.assertEqual(
            {targets.name(p): nvm.board_ram(p) for p in targets.all_targets()},
            {
                "h13-afro": 3072,
                "m14-beehive": 1536,
                "lm15u-beehive": 2560,
                "mtka5lr-beehive": 1536,
                "m2-dreadlocks": 1280,
                "m16p-dreadlocks2": 1536,
                "m16plite-dreadlocks2": 1536,
                "m16pp-goldilocks": 1536,
                "m16pplite-goldilocks": 1536,
                "m16p3-goldilocks2": 1536,
                "o18-goldilocks2": 3072,
                "o18k-goldilocks2": 3072,
                "m2r-dreadlocks2": 1280,
                "m3-goldilocks": 1536,
                "m3r-goldilocks2": 1536,
                "lm18a-goldilocks": 1536,
                "k2l-dreadlocks": 1536,
                "k2lp-dreadlocks": 1536,
                "k3lp-dreadlocks2": 1536,
                "k5lp-goldilocks2": 1536,
                "k6lp-jhericurl": 1536,
                "k6hp-jhericurl": 2048,
                "k7lp-kisscurl": 1536,
                "k8lp-mullet": 1536,
                "k8ap-mullet": 1536,
                "k8hp-mullet": 2048,
                "k8hp-number1": 2048,
                "k8hp-ombre": 2048,
                "k8hp-ponytail": 2048,
                "k8hp-queue": 2048,
                "k8hpp-number1": 2048,
                "k8lpn-number1": 1536,
                "kf23f-number1": 1536,
                "k8lpn2-ombre": 1536,
                "m2-simplesmart": 1024,
                "lm14-netcast45": 1024,
                "h13-netcast4": 1024,
                "lm21a-kisscurl": 1536,
                "lm21an-mullet": 1536,
                "lm21ann-number1": 1536,
                "lm21u-kisscurl": 2048,
                "lm21u-ombre": 2048,
                "m23-number1": 1536,
                "m23-ombre": 1536,
                "m23-ponytail": 1536,
                "m23-queue": 1536,
                "o20n-kisscurl": 3072,
                "e60n-kisscurl": 4096,
                "o22-mullet": 2560,
                "o22-number1": 2560,
                "o22-ponytail": 2560,
                "o22-queue": 2560,
                "o22n-number1": 2560,
                "o22n2-ombre": 2560,
                "o22n2-spacebuns": 2560,
                "k8hpp-ombre": 2048,
                "k8hpp-ponytail": 2048,
                "k8hpp-queue": 2048,
                "k8lpn-ombre": 1536,
                "k8lpn-ponytail": 1536,
                "k8lpn-queue": 1536,
                "k8lpn2-ponytail": 1536,
                "k8lpn2-queue": 1536,
                "k25lp-ponytail": 2048,
                "k25lp-queue": 2048,
                "k24-ombre": 2048,
                "k24-ponytail": 2048,
                "k24-queue": 2048,
                "k24n-ponytail": 2048,
                "k24n-queue": 2048,
                "lm21ann-ombre": 1536,
                "lm21ann-ponytail": 1536,
                "lm21ann-queue": 1536,
                "o22-ombre": 2560,
                "o22n-ombre": 2560,
                "o22n-ponytail": 2560,
                "o22n-queue": 2560,
                "o22n2-ponytail": 2560,
                "o22n2-queue": 2560,
                "o24-ombre": 2560,
                "o24-ponytail": 2560,
                "o24-queue": 2560,
                "o24n-ponytail": 2560,
                "o22n3-ponytail": 2560,
                "o24n-queue": 2560,
                "virt-testguest": 512,
                "o22n3-queue": 2560,
                "o26-queue": 3072,
            },
        )

    def test_simplesmart_firmware(self):
        """A SimpleSmart firmware has no rootfs.pak: a sysvinit root.pak and mode.pak's RELEASE.cfg
        with its OTA ID make it simplesmart, its _LCD_ name the model; the root pak is root.pak."""
        cats = {
            ("root.pak", "etc/inittab"): "id:3:initdefault:\n",
            ("mode.pak", "RELEASE.cfg"): "modelName,ATSC-M2\nszOtaid,HE_LCD_SS1A_AFAAAIAA\n",
        }
        vend = {"/libhalgal.so", "/libmadp.so"}
        with tempfile.TemporaryDirectory(prefix="beanjar-ss-") as tmp:
            fw = os.path.join(tmp, "04.10.06.01-HE_LCD_SS1A_AFAAAIAA")
            os.mkdir(fw)
            for f in ("root.pak", "mode.pak", "vend.pak"):
                open(os.path.join(fw, f), "wb").close()
            with (
                mock.patch.object(
                    paths,
                    "_squashfs_cat",
                    lambda pak, path: cats.get((os.path.basename(pak), path)),
                ),
                mock.patch.object(paths, "_squashfs_names", lambda pak: vend),
            ):
                self.assertEqual(paths.firmware_release(fw)[0], "simplesmart")
                self.assertEqual(paths.firmware_target(fw), ("m2", "simplesmart"))
                # NetCast 4.5: the vendor's graphics library is GAL_GX's libgal.so
                vend.clear()
                vend.update({"/libgal.so", "/libmadp.so"})
                self.assertEqual(paths.firmware_release(fw)[0], "netcast45")
                vend.clear()
                self.assertIsNone(paths.firmware_release(fw)[0])
                # NetCast 4 (H13): no vendor pak, GAL_GX's libgal.so in the lglib pak
                os.unlink(os.path.join(fw, "vend.pak"))
                self.assertIsNone(paths.firmware_release(fw)[0])
                open(os.path.join(fw, "lgli.pak"), "wb").close()
                vend.update({"/libgal.so", "/libkadaptor.so"})
                self.assertEqual(paths.firmware_release(fw)[0], "netcast4")
                del cats[("mode.pak", "RELEASE.cfg")]
                self.assertIsNone(paths.firmware_release(fw)[0])
            self.assertEqual(paths.root_pak(fw), os.path.join(fw, "root.pak"))
        self.assertEqual(targets.root_pak(("m2", "simplesmart")), "root")
        self.assertEqual(targets.always_layers(("m2", "simplesmart")), ("simplesmart",))
        self.assertEqual(targets.always_layers(H13_AFRO), ("webos", "soc", "common"))
        # SS1A's own M2 row; W16R's stays the chip's
        self.assertIn("mmap_ids=90", targets.soc(("m2", "simplesmart")).modules[0].params)
        self.assertIn("mmap_ids=89", targets.soc(("m2", "dreadlocks")).modules[0].params)

    def test_pci_budget(self):
        """Devices that fit the root bus stay as they are; past its 31 slots every disk goes
        behind one PCI bridge, at slots 1, 2, ... in order."""
        disks = lambda n: [
            a
            for i in range(n)
            for a in ("-drive", f"id=d{i}", "-device", f"virtio-blk-pci,drive=d{i}")
        ]
        others = ["-device", "virtio-gpu-gl-pci", "-device", "virtio-sound-pci,audiodev=snd0"]
        fits = disks(29)
        self.assertEqual(qemu.pci_budget(fits, others), fits)
        # ivshmem-doorbell has no "-pci" in its name but takes a slot: 29 disks and the three
        # others make 32 with the host bridge's neighbours, so they no longer stay on the root bus
        pool = ["-device", "ivshmem-doorbell,chardev=mediapool,vectors=1,id=mediapool"]
        self.assertEqual(qemu.pci_budget(disks(28), others + pool), disks(28))
        self.assertNotEqual(qemu.pci_budget(disks(29), others + pool), disks(29))
        out = qemu.pci_budget(disks(30), others)
        self.assertEqual(out[:2], ["-device", "pci-bridge,id=disks,chassis_nr=1,shpc=off"])
        self.assertEqual(out[5], "virtio-blk-pci,drive=d0,bus=disks,addr=0x1")
        self.assertEqual(out[-1], "virtio-blk-pci,drive=d29,bus=disks,addr=0x1e")
        # past one bridge's 31 disks, a second bridge, both before the disks
        out = qemu.pci_budget(disks(40), others)
        self.assertEqual(
            out[:4],
            [
                "-device",
                "pci-bridge,id=disks,chassis_nr=1,shpc=off",
                "-device",
                "pci-bridge,id=disks2,chassis_nr=2,shpc=off",
            ],
        )
        blk = [a for a in out if a.startswith("virtio-blk-pci,")]
        self.assertEqual(blk[30], "virtio-blk-pci,drive=d30,bus=disks,addr=0x1f")
        self.assertEqual(blk[31], "virtio-blk-pci,drive=d31,bus=disks2,addr=0x1")
        self.assertEqual(blk[39], "virtio-blk-pci,drive=d39,bus=disks2,addr=0x9")

    def test_refuses_unquotable(self):
        with self.assertRaisesRegex(ValueError, "cannot be a word"):
            targets.boot_conf(H13_AFRO, ["a b"])
        with self.assertRaisesRegex(ValueError, "cannot be a word"):
            targets.boot_conf(H13_AFRO, ["$(x)"])


class InitBootConfTest(unittest.TestCase):
    """guest/initramfs/rootfs/init's PmLog and systemd blocks, run by sh over a scratch root in place of
    /webos (mount, log and the kernel command line stubbed), for each init and PmLog mode."""

    def run_init(self, target, release=None, cmdline="", stock=None):
        init = text(os.path.join(TOP, "guest", "initramfs", "rootfs", "init"))
        block = init[
            init.index("# --- PmLog: keep every message") : init.index(
                "# --- /proc/cmdline as the firmware"
            )
        ]
        tmp = tempfile.mkdtemp(prefix="beanjar-init-")
        self.addCleanup(shutil.rmtree, tmp)
        root = os.path.join(tmp, "webos")
        os.makedirs(os.path.join(root, "etc", "init"))
        os.makedirs(os.path.join(root, "run"))
        for rel, body in (stock or {}).items():  # the firmware's own files, left out of the result
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w") as f:
                f.write(body)
        rows = {target[1]: release} if release else {}
        with mock.patch.dict(targets.RELEASES, rows):
            conf = targets.boot_conf(
                target,
                ["webos"],
                {
                    m.name: (MSTAR_DERIVED if m.derived == "mstar-mmap" else ())
                    for m in targets.soc(target).modules
                    if m.derived
                },
            )
        with open(os.path.join(tmp, "boot.conf"), "w") as f:
            f.write(conf)
        script = (
            f"W={shlex.quote(root)}; T={shlex.quote(tmp)}\n"
            'log() { echo "log $*"; }\nfail() { echo "fail $*"; exit 1; }\n'
            f'arg() {{ for w in {shlex.quote(cmdline)}; do case $w in "$1="*) echo "${{w#*=}}"; return;; esac; done; '
            'echo "$2"; }\n'
            'mount() { echo "mount $*" >> "$T/mounts"; }\n'
            '. "$T/boot.conf"\n' + block.replace("/webos/", "$W/")
        )
        r = subprocess.run(
            ["sh", "-ec", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        self.assertEqual(r.returncode, 0, r.stdout)
        files = {}
        for d, _, names in os.walk(root):
            for n in names:
                files[os.path.relpath(os.path.join(d, n), root)] = text(os.path.join(d, n))
        for rel in stock or ():
            del files[rel]
        mounts = (
            text(os.path.join(tmp, "mounts")) if os.path.exists(os.path.join(tmp, "mounts")) else ""
        )
        return files, mounts, r.stdout

    def test_upstart_preload(self):
        files, mounts, _ = self.run_init(H13_AFRO)
        self.assertEqual(set(files), {"etc/init/PmLogDaemon.override"})
        self.assertIn(
            "env LD_PRELOAD=/usr/lib/libpmlog-keep-all.so", files["etc/init/PmLogDaemon.override"]
        )
        self.assertEqual(mounts, "")  # no /run of its own, no marker: Upstart's own

    def test_upstart_preload_env(self):
        files, mounts, _ = self.run_init(("k5lp", "goldilocks2"))
        self.assertEqual(set(files), {"etc/init/PmLogDaemon.override"})
        self.assertIn(
            "env PMLOG_PRELOAD=/usr/lib/libpmlog-keep-all.so",
            files["etc/init/PmLogDaemon.override"],
        )
        self.assertEqual(mounts, "")

    def test_systemd(self):
        files, mounts, out = self.run_init(("m2", "systemdtest"), SYSTEMD_ROW)
        dropin = "etc/systemd/system/pm-log-daemon.service.d/emulator-pmlog.conf"
        self.assertEqual(set(files), {dropin, "run/emulator/bsp-mounted"})
        self.assertEqual(
            files[dropin].splitlines()[1:],
            [
                "[Service]",
                "Environment=LD_PRELOAD=/usr/lib/libsystrim.so.3:/usr/lib/libpmlog-keep-all.so",
            ],
        )
        self.assertRegex(mounts, r"^mount -t tmpfs -o \S+ tmpfs \S+/webos/run\n$")
        self.assertIn("preload-systemd", out)

    def test_time_scale(self):
        # webos.time_scale=4 (TCG): bootd's override LD_PRELOADs the scaling library; without it
        # (KVM, the tests above) there is no bootd override at all
        for target in (H13_AFRO, ("k5lp", "goldilocks2")):
            files, _, out = self.run_init(target, cmdline="webos.time_scale=4")
            self.assertEqual(
                files["etc/init/bootd.override"].splitlines()[1:],
                [
                    "script",
                    "\texport LD_PRELOAD=/usr/lib/libbootd-scale.so BOOTD_TIMEOUT_SCALE=4",
                    "\texec /usr/sbin/bootd",
                    "end script",
                ],
            )
            self.assertIn("bootd: timeouts x4", out)
        files, _, _ = self.run_init(("m2", "systemdtest"), SYSTEMD_ROW, "webos.time_scale=4")
        self.assertNotIn("etc/init/bootd.override", files)

    def test_time_scale_memchute(self):
        # webos.time_scale=4 on systemd with a memchute unit: a drop-in LD_PRELOADs the heartbeat
        # scaling; time_scale=1 (KVM), or no memchute unit, writes none
        dropin = "etc/systemd/system/memchute.service.d/beanjar-scale.conf"
        unit = {"lib/systemd/system/memchute.service": "[Service]\nExecStart=/usr/sbin/memchute\n"}
        target = ("m2", "systemdtest")
        files, _, out = self.run_init(target, SYSTEMD_ROW, "webos.time_scale=4", unit)
        self.assertEqual(
            files[dropin].splitlines()[1:],
            [
                "[Service]",
                "Environment=LD_PRELOAD=/usr/lib/libmemchute-scale.so MEMCHUTE_HEARTBEAT_SCALE=4",
            ],
        )
        self.assertIn("memchute: heartbeat x4", out)
        files, _, out = self.run_init(target, SYSTEMD_ROW, "webos.time_scale=1", unit)
        self.assertNotIn(dropin, files)
        self.assertNotIn("memchute:", out)
        files, _, _ = self.run_init(target, SYSTEMD_ROW, "webos.time_scale=4")
        self.assertNotIn(dropin, files)
        files, _, _ = self.run_init(H13_AFRO, cmdline="webos.time_scale=4")
        self.assertNotIn(dropin, files)

    def test_systemd_pmlog_stock(self):
        # webos.pmlog=stock: the unit stays stock; the marker is made all the same
        files, _, _ = self.run_init(("m2", "systemdtest"), SYSTEMD_ROW, "webos.pmlog=stock")
        self.assertEqual(set(files), {"run/emulator/bsp-mounted"})


class InitSwapTest(unittest.TestCase):
    """guest/initramfs/rootfs/init's qemu-user swap block (host/core/beanjar/hostswap.py), run by sh
    with disk, mkswap and swapon stubbed and a scratch /sys/block: the disk with the serial
    beanjar-swap gets mkswap and swapon -d (discard); without it nothing runs."""

    def run_block(self, disk, mkswap_status=0):
        init = text(os.path.join(TOP, "guest", "initramfs", "rootfs", "init"))
        block = init[
            init.index("# A qemu-user VM's swap disk") : init.index("# The GL layer's Qt adapter")
        ]
        tmp = tempfile.mkdtemp(prefix="beanjar-init-")
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "vdz"))
        with open(os.path.join(tmp, "vdz", "size"), "w") as f:
            f.write("2097152\n")  # 1 GiB in 512-byte sectors
        script = (
            f"T={shlex.quote(tmp)}\n"
            'log() { echo "log $*"; }\n'
            f'disk() {{ [ "$1" = beanjar-swap ] && echo {shlex.quote(disk)}; }}\n'
            f'mkswap() {{ echo "mkswap $*" >&2; return {mkswap_status}; }}\n'
            'swapon() { echo "swapon $*"; }\n' + block.replace("/sys/block/", "$T/")
        )
        r = subprocess.run(
            ["sh", "-ec", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        self.assertEqual(r.returncode, 0, r.stdout)
        return r.stdout.splitlines()

    def test_swap(self):
        self.assertEqual(
            self.run_block("/dev/vdz"),
            [
                "mkswap /dev/vdz",
                "swapon -d /dev/vdz",
                "log swap: /dev/vdz in host RAM, 1024 MiB, with discard",
            ],
        )

    def test_no_swap(self):
        self.assertEqual(self.run_block(""), [])

    def test_swap_fails(self):
        self.assertEqual(
            self.run_block("/dev/vdz", mkswap_status=1),
            ["mkswap /dev/vdz", "log swap: /dev/vdz could not be turned on"],
        )
