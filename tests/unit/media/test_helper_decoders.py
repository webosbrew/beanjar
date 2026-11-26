#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The media helper's own FFmpeg (packaging/vendor.toml's ffmpeg-helper) has the decoders the
helper's code can open: scripts/media-helper-decoders derives them from host/media, and the pinned
build's BUILDINFO lists the codecs and decoders it was configured with (its Codecs field, from
beanjar-vendor's ffmpeg-helper/decoders). A codec added to caps_codec() without a new build fails
here."""

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import sys

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "meson"))
import vendor  # noqa: E402


def load_script():
    path = TOP / "scripts/media-helper-decoders"
    loader = SourceFileLoader("media_helper_decoders", str(path))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


script = load_script()


def test_caps_codec_ids_from_its_body_only():
    source = """
static int other(void) { return AV_CODEC_ID_PRORES; }
void caps_codec(const char *string, struct codec_params *p)
{
    p->id = AV_CODEC_ID_NONE;
    if (a)
        p->id = AV_CODEC_ID_H264;
    else
        p->id = mpeg == 4 ? AV_CODEC_ID_MPEG4 : AV_CODEC_ID_MPEG2VIDEO;
}

int caps_software_only(enum AVCodecID id) { return id == AV_CODEC_ID_VC1; }
"""
    assert script.caps_codec_ids(source) == ["h264", "mpeg2video", "mpeg4"]


def test_the_helpers_codecs():
    ids = script.caps_codec_ids()
    for codec in ("h264", "hevc", "av1", "aac", "pcm_s16le", "wmv3"):
        assert codec in ids
    assert "none" not in ids
    # the software-only path asks for AV1 through dav1d by name (host/media/decode/codec.c)
    assert "libdav1d" in script.named_decoders()


def test_pinned_build_has_the_helpers_decoders():
    arch = script.host_arch()
    full = vendor.tree("ffmpeg", arch)
    helper = vendor.tree("ffmpeg-helper", arch)
    info = vendor.buildinfo(helper / "BUILDINFO")
    built = sorted(tuple(line.split()) for line in info["Codecs"].split("\n") if line.strip())
    assert built == script.decoders(full / "lib"), (
        "the pinned ffmpeg-helper was built for other codecs than host/media's: regenerate "
        "beanjar-vendor's ffmpeg-helper/decoders with scripts/media-helper-decoders and release it"
    )
    # and nothing the helper does not use
    assert info["License"] == "LGPL-2.1-or-later"
    assert set(info["Sonames"].split()) == {
        "libavcodec.so.61",
        "libavutil.so.59",
        "libswresample.so.5",
    }
