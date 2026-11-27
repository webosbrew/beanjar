#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Load the modern LX plugins on the host, per LX_REL, and check the observed stock surfaces.

Evidence: gst-inspect of each firmware's own plugins (W22O 04.40 live, 13.30, 33.31 and 43.21, W23O
13.40 and W24G 33.20 under qemu-arm): lxvideodec primary with H.264, H.265, MPEG and VP9, aac_audiodec
and audiosink secondary; from W24G on a pointer-valued svp-handle action, GeneratingVideoHash, es-bypass
and queue-max-length; W22O ponytail (25) and queue (26) add and drop what LX25 and LX26 list; the
ponytail and queue builds that keep DTS (LX_DTS: W24G 33.31 and 43.21, W23O 44.00, in the guest) keep
dts-seamless and add dts_audiodec. This checks registration and property types, not device playback.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

video = Path(__file__).resolve().parents[1]
top = video.parents[4]
sets = {
    "lxvideodec": ["lxvideodec", "esdec", "streaminfo", "mediaport"],
    "lxvideosink": ["lxvideosink", "essink", "esdec", "streaminfo", "mediaport"],
    "lgaudiodec": ["audiodec", "esdec", "mediaport"],
    "lgaudiosink": ["audiosink", "essink", "esdec", "streaminfo", "mediaport"],
}
flags = subprocess.check_output(
    [
        "pkg-config",
        "--cflags",
        "--libs",
        "gstreamer-1.0",
        "gstreamer-base-1.0",
        "gstreamer-audio-1.0",
        "gstreamer-video-1.0",
    ],
    text=True,
).split()
# tokens that must, and must not, be in each factory's gst-inspect per LX_REL
COMMON = {
    "lxvideodec": [
        "Rank                     primary (256)",
        "is-svp",
        "svp-handle",
        "video/x-h264",
        "video/x-h265",
        "hvc1",
        "video/mpeg",
        "video/x-vp8",
        "video/x-vp9",
        "video/x-av1",
    ],
    "lxvideosink": ["preroll-pts", "window-id"],
    "aac_audiodec": ["Rank                     secondary (128)", "audio/mpeg", "default-prl"],
    "audiosink": ["Rank                     secondary (128)", "pcm-bypass"],
}
W24G = {
    "lxvideodec": ["gpointer arg0", "GeneratingVideoHash"],
    "aac_audiodec": ["es-bypass"],
    "audiosink": ["queue-max-length", "es-bypass-done"],
}
# W22O ponytail (33.31): added, and gone since; W22O queue (43.21): added
LX25 = {
    "lxvideosink": ["variable-speed-playback"],
    "aac_audiodec": ["downmix", "sound-system"],
    "audiosink": ["variable-speed-playback"],
}
LX25_GONE = {
    "lxvideosink": ["interleaving-type", "thumbnail-stretch", '"last-sample"'],
}
# W22O ponytail's audio builds dropped DTS; LX_DTS's keep it
DTS = {"aac_audiodec": ["dts-seamless"]}
LX26 = {
    "lxvideodec": ["enable-corrupted-check-using-av1-order-hint"],
    "lxvideosink": ["transfer-until-pts", "video-sink-offset", "video-frame-info"],
    "audiosink": ["channel-map", "audio-sample-drop"],
}
for rel, dts in (
    (22, False),
    (23, False),
    (24, False),
    (25, False),
    (26, False),
    (25, True),
    (26, True),
):
    with tempfile.TemporaryDirectory(prefix="beanjar-modern-lx-") as directory:
        for plugin, sources in sets.items():
            subprocess.run(
                [
                    "cc",
                    "-shared",
                    "-fPIC",
                    "-DO22N2_MEDIA",
                    f"-DLX_REL={rel}",
                    *(["-DLX_DTS"] if dts else []),
                    '-DESSINK_TYPE_NAME="EsSink-' + plugin + '"',
                    "-Wno-deprecated-declarations",
                    "-I" + str(top / "include"),
                    *[str(video / "src" / (source + ".c")) for source in sources],
                    "-o",
                    directory + "/libgst" + plugin + ".so",
                    *flags,
                ],
                check=True,
            )
        env = dict(os.environ, GST_PLUGIN_PATH=directory, GST_REGISTRY=directory + "/registry.bin")
        for factory, expected in COMMON.items():
            output = subprocess.check_output(["gst-inspect-1.0", factory], env=env, text=True)
            assert directory in output, (rel, factory, "stock plugin was loaded instead")
            for token in expected + (W24G.get(factory, []) if rel >= 24 else []):
                assert token in output, (rel, factory, token)
            if rel < 24:
                for token in W24G.get(factory, []):
                    assert token not in output, (rel, factory, token)
                if factory == "lxvideodec":
                    assert "guint arg0" in output, (rel, factory)
            for table, present in (
                (LX25, rel >= 25),
                (LX25_GONE, rel < 25),
                (DTS, rel < 25 or dts),
                (LX26, rel >= 26),
            ):
                for token in table.get(factory, []):
                    assert (token in output) == present, (rel, factory, token)
        drc = subprocess.check_output(["gst-inspect-1.0", "aac_audiodec"], env=env, text=True)
        default = re.search(r"drc-mode\s*:.*?Default: (\d+)", drc, re.S).group(1)
        assert default == ("2147483647" if rel >= 23 else "0"), (rel, default)
        app_flags = subprocess.check_output(
            ["pkg-config", "--cflags", "--libs", "gstreamer-app-1.0"], text=True
        ).split()
        probe = directory + "/es-preservation"
        subprocess.run(
            ["cc", str(video / "tests/es-preservation.c"), "-o", probe, *app_flags], check=True
        )
        subprocess.run([probe], env=env, timeout=10, check=True)
        output = subprocess.check_output(["gst-inspect-1.0", "lgaudiodec"], env=env, text=True)
        assert ("6 features" if dts else "5 features") in output, output
        # W22O 43.21's other factories: same properties and rank as aac_audiodec, own caps
        aac = subprocess.check_output(["gst-inspect-1.0", "aac_audiodec"], env=env, text=True)
        props = aac[aac.index("Element Properties:") :]
        for factory, caps, name in (
            (
                "ac3_audiodec",
                ["audio/x-ac3", "audio/x-eac3", "audio/x-private1-ac3"],
                "lgaudiodecac3-0",
            ),
            ("opus_audiodec", ["audio/x-opus"], "lgaudiodecopus0"),
            ("vorbis_audiodec", ["audio/x-vorbis"], "lgaudiodecvorbis0"),
            ("flac_audiodec", ["audio/x-flac"], "lgflacaudiodecflac0"),
            *(
                [
                    (
                        "dts_audiodec",
                        [
                            "audio/x-dts",
                            "audio/x-private1-dts",
                            "audio/x-dtsx",
                            "audio/x-dtsh",
                            "audio/x-dtse",
                            "audio/x-dtsl",
                        ],
                        "lgaudiodecdts0",
                    )
                ]
                if dts
                else []
            ),
        ):
            output = subprocess.check_output(["gst-inspect-1.0", factory], env=env, text=True)
            assert directory in output, (rel, factory, "stock plugin was loaded instead")
            assert "Rank                     secondary (128)" in output, (rel, factory)
            for token in caps + [name]:
                assert token in output, (rel, factory, token)
            assert (
                output[output.index("Element Properties:") :].replace(name, "lgaudiodecaac0")
                == props
            ), (rel, factory, "properties differ from aac_audiodec")
print(
    "PASS: four modern plugins (eight factories, nine with DTS) load for LX_REL 22 to 26 and 25 and 26 with DTS; names, ranks and property/action types match"
)
