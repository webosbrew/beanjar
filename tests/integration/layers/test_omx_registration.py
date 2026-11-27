#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The factories of our Realtek libgstomx.so (src/rtkomx.c, src/omxconf.c) against the stock rule.

Loads the built plugin into the firmware's own gst-inspect-1.0 under qemu-arm, once with the
firmware's gstomx.conf and once with each conf below, and compares the factories and ranks with the
stock plugin's registration: one factory per section at the section's rank, and, as Realtek's later
gst-omx adds (gst_omx_dec_bin_register, gst_rtk_codec_bin_register), one per section the pattern
"(omx(.+)dec)1" matches with component-name omx_vdec or omx_adec, named after the match, at 257,
or 0 if the name holds "auto". Run by the guest build lane of a Realtek target (its artefacts.json:
omx_plugin, target; guest/layers/soc/src/video/meson.build), with the target's firmware
(host/scripts/vm-launcher config FW --target); skipped elsewhere or without that firmware.
"""

import configparser
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

ARTEFACTS = Path(os.environ.get("BEANJAR_TEST_ARTEFACTS", "/nonexistent"))
GUEST = json.loads(ARTEFACTS.read_text()) if ARTEFACTS.is_file() else {}
if not GUEST.get("omx_plugin"):
    pytest.skip("not a Realtek guest build lane (no omx_plugin)", allow_module_level=True)
PLUGIN = Path(GUEST["omx_plugin"]).resolve()
_FW = subprocess.run(
    [
        str(Path(__file__).resolve().parents[3] / "host/scripts/vm-launcher"),
        "config",
        "FW",
        "--target",
        GUEST["target"],
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
)
ROOTFS = Path(_FW.stdout.strip() or "/nonexistent").resolve() / "rootfs"

SECTION = """[{name}]
type-name={type}
core-name=/usr/lib/libomx_core.so
component-name={component}
rank={rank}
in-port-index=0
out-port-index=1
"""
# The earlier releases' scheme (W19P, W20L, W21P, W22H 04.60): plain names at their ranks.
EARLIER = [
    ("omxh264dec", "GstOMXH264Dec", "omx_vdec", 257),
    ("omxautovideodec", "GstOMXAutoVideoDec", "omx_vdec", 0),
    ("omxaacdec", "GstOMXAACDec", "omx_adec", 257),
    ("omxflv1dec", "GstOMXFLV1Dec", "omx_vdec", 257),
]
# The later scheme (number1 and on): "1" sections at rank 0, plus sections the rule leaves alone.
LATER = [
    ("omxh264dec1", "GstOMXH264Dec", "omx_vdec", 0),
    ("omxh265dec1", "GstOMXH265Dec", "omx_vdec", 64),
    ("omxautovideodec1", "GstOMXAutoVideoDec", "omx_vdec", 0),
    ("omxautoaudiodec1", "GstOMXAutoAudioDec", "omx_adec", 0),
    ("omxaacdec1", "GstOMXAACDec", "omx_adec", 0),
    ("omxmp3dec1", "GstOMXMP3Dec", "omx_venc", 0),  # not a decoder component
    ("omxvp9dec2", "GstOMXVP9Dec", "omx_vdec", 0),
]  # not "1"


def expected(conf_text):
    conf = configparser.ConfigParser(interpolation=None)
    conf.read_string(conf_text)
    factories = {}
    for section in conf.sections():
        factories[section] = conf.getint(section, "rank")
    for section in conf.sections():
        m = re.search(r"(omx(.+)dec)1", section)
        if m and conf.get(section, "component-name") in ("omx_vdec", "omx_adec"):
            factories[m.group(1)] = 0 if "auto" in m.group(1) else 257
    return factories


def registered(conf_dir):
    """{factory: rank} of the plugin read with conf_dir/gstomx.conf, by the firmware's GStreamer."""
    with tempfile.TemporaryDirectory() as tmp:
        plugins = Path(tmp) / "plugins"
        plugins.mkdir()
        shutil.copy(PLUGIN, plugins / "libgstomx.so")
        env = {
            "HOME": tmp,
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "GST_REGISTRY_1_0": str(Path(tmp) / "registry.bin"),
            "GST_REGISTRY_UPDATE": "yes",
            "GST_PLUGIN_PATH_1_0": str(plugins),
            "GST_PLUGIN_SYSTEM_PATH_1_0": "",
            "GST_PLUGIN_SCANNER_1_0": "/nonexistent",
            "GST_OMX_CONFIG_DIR": str(conf_dir),
        }
        out = subprocess.run(
            ["qemu-arm", "-L", str(ROOTFS), str(ROOTFS / "usr/bin/gst-inspect-1.0"), "-a"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout
    ranks = {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"^(\S+): +Rank +.*\((-?\d+)\)$", out, re.M)
    }
    ours = {
        m.group(1)
        for m in re.finditer(r"^(\S+): +Filename +(\S+)$", out, re.M)
        if Path(m.group(2)).name == "libgstomx.so"
    }
    return {name: rank for name, rank in ranks.items() if name in ours}


def check(case, conf_text, conf_dir=None):
    with tempfile.TemporaryDirectory() as tmp:
        if conf_dir is None:
            conf_dir = Path(tmp)
            (conf_dir / "gstomx.conf").write_text(conf_text)
        got = registered(conf_dir)
    # Our classes are a subset of the stock ones: a section of an unknown type is left out.
    want = expected(conf_text)
    case.assertTrue(got, "no factories registered")
    case.assertLessEqual(set(got), set(want), "factories the stock rule does not register")
    for name in got:
        case.assertEqual(got[name], want[name], name)
    for section, legacy in ((s, re.search(r"(omx(.+)dec)1", s)) for s in got):
        if legacy and legacy.group(1) in want:
            case.assertIn(legacy.group(1), got, f"{section} without its legacy factory")
    return got


@unittest.skipUnless(shutil.which("qemu-arm"), "qemu-arm is not installed")
@unittest.skipUnless(
    (ROOTFS / "usr/bin/gst-inspect-1.0").exists(), "the firmware has no gst-inspect-1.0"
)
class OmxRegistration(unittest.TestCase):
    def conf(self, sections):
        return "\n".join(
            SECTION.format(name=n, type=t, component=c, rank=r) for n, t, c, r in sections
        )

    def test_firmware_conf(self):
        conf_dir = ROOTFS / "etc/xdg"
        check(self, (conf_dir / "gstomx.conf").read_text(), conf_dir)

    def test_earlier_scheme_adds_nothing(self):
        got = check(self, self.conf(EARLIER))
        self.assertEqual(got, {n: r for n, _, _, r in EARLIER})

    def test_later_scheme(self):
        got = check(self, self.conf(LATER))
        self.assertEqual(
            got,
            {
                **{n: r for n, _, _, r in LATER},
                "omxh264dec": 257,
                "omxh265dec": 257,
                "omxaacdec": 257,
                "omxautovideodec": 0,
                "omxautoaudiodec": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
