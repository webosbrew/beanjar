#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile dtv_soc's video decoder's reading of MPEG-2 headers (vdec.c) on the host and feed it
an elementary stream cut into packet payloads at every offset: a sequence header gives a FRAME
event of type 0, the first picture after it the PICINFO (once; again only when the format
changes), each picture header a FRAME event of its type, whether or not a start code is split over
two payloads; the picture info is the sequence header's size, frame rate and bit rate, progressive
as its extension says. A picture's user data (captions' A/53 cc_data) comes out whole, cut at the
next start code, with its picture's type, temporal reference and field flags, in display order:
a B picture's at once, an I or P picture's at the next I or P picture; a sequence's or a GOP's
user data is not a picture's."""

import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/dtv_soc"

RUNNER = r"""
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define min(a, b) ((a) < (b) ? (a) : (b))
#define EV_PICINFO 0x8002
#define CARRY 15
#define UD_MAX 128
#define UD_BUF (UD_MAX + 8)
#define U64_MAX UINT64_MAX
struct picinfo {
    u32 h, v, aspect, bitrate, num, den;
    bool progressive;
};
struct userdata {
    u8 type, rff, tff, tref;
    u32 pts;
    unsigned int len;
    u8 data[UD_BUF];
};
struct vdec_file {
    u8 carry[CARRY];
    unsigned int carried;
    u64 pts;
    bool pts_used;
    bool seq_seen, have_info;
    struct picinfo info, sent;
    u32 pictures, sequences, errors;
    struct userdata cur, held;
    bool in_picture, collecting, holding;
};
static void userdata_event(struct vdec_file *f, const struct userdata *u)
{
    if (!u->len)
        return;
    printf("userdata type %u tref %u tff %u rff %u:", u->type, u->tref, u->tff, u->rff);
    for (unsigned i = 0; i < u->len; i++)
        printf(" %02x", u->data[i]);
    printf("\n");
}
static void event(struct vdec_file *f, u32 id, const u8 *d, size_t n)
{
    printf("picinfo %ux%u %u/%u aspect %u bitrate %u progressive %d\n", f->sent.h, f->sent.v,
           f->sent.num, f->sent.den, f->sent.aspect, f->sent.bitrate, f->sent.progressive);
}
static void frame_event(struct vdec_file *f, u8 type)
{
    printf("frame %u\n", type);
}
@SECTION@
int main(int argc, char **argv)
{
    static u8 buf[1 << 16];
    static struct vdec_file f = {.pts = U64_MAX};
    size_t n = fread(buf, 1, sizeof buf, stdin), cut = (size_t)atoi(argv[1]);
    for (size_t at = 0; at < n; at += cut)
        scan(&f, buf + at, (unsigned)min(cut, n - at));
    printf("sequences %u pictures %u errors %u\n", f.sequences, f.pictures, f.errors);
    return 0;
}
"""


def sequence(w, h, aspect, rate, bitrate_400):
    return bytes(
        [
            0,
            0,
            1,
            0xB3,
            w >> 4,
            (w & 0xF) << 4 | h >> 8,
            h & 0xFF,
            aspect << 4 | rate,
            bitrate_400 >> 10,
            bitrate_400 >> 2 & 0xFF,
            (bitrate_400 & 3) << 6 | 0x20,
            0,
        ]
    )


def extension(progressive):
    return bytes([0, 0, 1, 0xB5, 0x14, 0x82 | progressive << 3, 0x01, 0x00, 0x01, 0x00])


def picture(kind, tref=0, cc=None, tff=1):
    """A picture header, its coding extension (top_field_first) and, with cc, the A/53 user
    data carrying that 608 pair, then a slice."""
    head = bytes([0, 0, 1, 0x00, tref >> 2, (tref & 3) << 6 | kind << 3, 0xFF, 0xF8])
    head += bytes([0, 0, 1, 0xB5, 0x8F, 0xFF, 0xF3, tff << 7 | 0x41, 0x80])
    if cc is not None:
        head += user_data(cc)
    return head + bytes([0, 0, 1, 0x01]) + bytes(40)


def user_data(cc):
    return bytes([0, 0, 1, 0xB2]) + b"GA94\x03\x42\xff\xfc" + cc + b"\xfd\x80\x80\xff"


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    source = (MODULE / "vdec.c").read_text()
    start = source.index("/* ---- the stream's headers ----")
    section = source[start : source.index("/* ---- end of the stream's headers ---- */")]
    root = tmp_path_factory.mktemp("dtv_soc_vdec")
    (root / "runner.c").write_text(RUNNER.replace("@SECTION@", section))
    subprocess.run(
        [
            "cc",
            "-std=gnu11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-function",
            "-Wno-unused-parameter",
            "-o",
            str(root / "runner"),
            str(root / "runner.c"),
        ],
        check=True,
    )
    return root / "runner"


def run(runner, data, cut):
    out = subprocess.run([str(runner), str(cut)], input=data, capture_output=True, check=True)
    return out.stdout.decode().splitlines()


def test_headers_any_cut(runner):
    gop = sequence(704, 480, 2, 4, 3750) + extension(1) + picture(1) + picture(3) + picture(2)
    data = gop + gop + sequence(1920, 1080, 3, 4, 3750) + extension(0) + picture(1)
    want = [
        "frame 0",
        "picinfo 704x480 30000/1001 aspect 2 bitrate 1500 progressive 1",
        "frame 1",
        "frame 3",
        "frame 2",
        "frame 0",
        "frame 1",
        "frame 3",
        "frame 2",
        "frame 0",
        "picinfo 1920x1080 30000/1001 aspect 3 bitrate 1500 progressive 0",
        "frame 1",
        "sequences 3 pictures 7 errors 0",
    ]
    for cut in (len(data), 184, 100, 13, 7, 5, 4, 3, 2, 1):
        assert run(runner, data, cut) == want, cut


def test_no_picture_before_a_sequence(runner):
    data = picture(1) + picture(2) + sequence(704, 480, 2, 4, 3750) + picture(1)
    assert run(runner, data, 184) == [
        "frame 0",
        "picinfo 704x480 30000/1001 aspect 2 bitrate 1500 progressive 1",
        "frame 1",
        "sequences 1 pictures 1 errors 0",
    ]


def test_user_data_in_display_order_any_cut(runner):
    """Coded I0 P3 B1 B2 P6 B4 B5 comes out displayed: 0 1 2 3 4 5, then 6 at the next GOP's I.
    A picture without user data has no event.
    The sequence's and the GOP's own user data are not a picture's."""
    gop = (
        sequence(704, 480, 2, 4, 3750)
        + extension(0)
        + user_data(b"\x11\x11")  # the sequence's
        + bytes([0, 0, 1, 0xB8, 0, 8, 0, 0x40])  # GOP header
        + user_data(b"\x22\x22")  # the GOP's
    )
    coded = [(1, 0), (2, 3), (3, 1), (3, 2), (2, 6), (3, 4), (3, 5)]
    data = gop + b"".join(picture(k, t, bytes([0x40 + t, 0x80])) for k, t in coded)
    # P6 is shown before the next I, so it goes once that I has ended: at the picture after it
    data += gop + picture(1, 0, bytes([0x50, 0x80]), tff=0) + picture(2, 3)

    def ud(kind, tref, b, tff=1):
        return f"userdata type {kind} tref {tref} tff {tff} rff 0: 47 41 39 34 03 42 ff fc {b:02x} 80 fd 80 80 ff"

    want = [ud(k, t, 0x40 + t) for k, t in sorted(coded, key=lambda c: c[1])][:6]
    want += [ud(2, 6, 0x46)]  # P6, at the next I
    for cut in (len(data), 184, 100, 13, 7, 5, 4, 3, 2, 1):
        got = [line for line in run(runner, data, cut) if line.startswith("userdata")]
        assert got == want, cut
