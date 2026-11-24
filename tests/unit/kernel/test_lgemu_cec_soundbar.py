#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the sound bar of lgemu's CEC adapter (cec.c) on the host and drive it as libcecp does
(W22O 13.30.56, /var/log/cecmessages): a poll and the device queries after it reports its
physical address, Request Short Audio Descriptor for LPCM, AC-3 and E-AC-3, System Audio Mode
Request, the ARC handshake, the volume keys; then the parameter's text both ways and the goodbye
of a bar switched off."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/lgemu"

RUNNER = r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
"""


def section():
    source = (MODULE / "cec.c").read_text()
    start = source.index("/* ---- the sound bar ---- */")
    return source[start : source.index("/* ---- end of the sound bar ---- */")]


def run(tmp_path, body):
    runner = tmp_path / "runner.c"
    runner.write_text(
        RUNNER
        + section()
        + r"""
/* the TV's frame as hex ("05A401020A"), the bar's answers as hex, space-separated */
static struct bar bar;
static char answers[256];
static const char *tv(const char *hex)
{
    u8 msg[16];
    unsigned int len = strlen(hex) / 2;
    struct bar_frame out[3];
    unsigned int n;
    char *p = answers;

    for (unsigned int i = 0; i < len; i++)
        sscanf(hex + 2 * i, "%2hhx", &msg[i]);
    if (!bar_acks(&bar, msg, len) && (msg[0] & 0xF) != 0xF)
        return "NACK";
    n = bar_answer(&bar, msg, len, out);
    *p = 0;
    for (unsigned int i = 0; i < n; i++) {
        for (unsigned int b = 0; b < out[i].len; b++)
            p += sprintf(p, "%02X", out[i].msg[b]);
        if (i + 1 < n)
            *p++ = ' ', *p = 0;
    }
    return answers;
}
static const char *frames(struct bar_frame *out, unsigned int n)
{
    char *p = answers;
    *p = 0;
    for (unsigned int i = 0; i < n; i++) {
        if (!out[i].len)
            p += sprintf(p, "END");
        for (unsigned int b = 0; b < out[i].len; b++)
            p += sprintf(p, "%02X", out[i].msg[b]);
        if (i + 1 < n)
            *p++ = ' ', *p = 0;
    }
    return answers;
}
#define EQ(a, b) do { const char *_a = (a); if (strcmp(_a, b)) { \
    fprintf(stderr, "line %d: %s != %s\n", __LINE__, _a, b); return 1; } } while (0)
int main(void)
{
"""
        + body
        + "\n    return 0;\n}\n"
    )
    exe = tmp_path / "runner"
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Werror", "-Wno-unused-function", "-o", exe, runner],
        check=True,
    )
    result = subprocess.run([exe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_follower(tmp_path):
    run(
        tmp_path,
        r"""
    struct bar_frame out[3];
    char buf[128];

    EQ(tv("05"), "NACK"); /* nobody at address 5 yet */
    assert(bar_parse("2,150750550700\n", &bar) == 0 && bar.present && bar.nsads == 2);
    bar.present = false; /* plugged in below */
    EQ(tv("0583"), "NACK");
    bar.present = true;
    EQ(frames(out, bar_hello(&bar, out)), "5F84200005");
    EQ(tv("05"), ""); /* a poll: acknowledged, no answer */
    EQ(tv("00"), "NACK"); /* the TV's own address stays free */
    EQ(tv("0583"), "5F84200005");
    EQ(tv("058C"), "5F87000C03");
    EQ(tv("058F"), "509000");
    EQ(tv("0546"), "5047536F756E6420426172");
    EQ(tv("059F"), "509E05");
    EQ(tv("057D"), "507E00");
    /* libcecp asks for LPCM, AC-3 and E-AC-3; the bar has AC-3 and E-AC-3 */
    EQ(tv("05A401020A"), "50A3150750550700");
    EQ(tv("05A401"), "5000A403"); /* none of them: Feature Abort, invalid operand */
    EQ(tv("05A4"), "5000A403");
    EQ(tv("05700000"), "5F7201"); /* System Audio Mode Request with the source: on */
    assert(bar.sam);
    EQ(tv("057D"), "507E01");
    EQ(tv("0571"), "507A19");
    EQ(tv("05C3"), "50C0"); /* Request ARC Initiation: Initiate ARC */
    assert(bar.arc);
    EQ(tv("05C1"), ""); /* Report ARC Initiated */
    EQ(tv("4FC3"), ""); /* a broadcast is not the bar's to answer */
    EQ(tv("45C3"), "5400C304"); /* ARC with anyone but the TV: refused */
    EQ(tv("04C3"), "NACK");
    EQ(tv("054441"), "507A1A"); /* volume up */
    EQ(tv("054443"), "507A9A"); /* mute */
    EQ(tv("0545"), "");
    EQ(tv("0589AA"), "50008900"); /* a vendor command: unrecognized */
    EQ(tv("05FF"), "5000FF04");
    EQ(tv("05C4"), "50C5"); /* Request ARC Termination */
    assert(!bar.arc);
    EQ(tv("0570"), "5F7200"); /* System Audio Mode Request without operand: off */
    EQ(tv("05C3"), "50C0");
    EQ(tv("05700000"), "5F7201");

    /* parameter text */
    bar_format(&bar, buf);
    EQ(buf, "2,150750550700\n");

    /* switched off: SAM off, Terminate ARC, then gone; still acknowledging until then */
    EQ(frames(out, bar_goodbye(&bar, out)), "5F7200 50C5 END");
    assert(bar.present && bar.leaving && !bar.arc && !bar.sam);
    EQ(tv("05C2"), "");
    EQ(tv("05C3"), ""); /* leaving: no new ARC */
    bar_format(&bar, buf);
    EQ(buf, "\n");
    /* a bar with neither ARC nor SAM on just goes */
    bar.leaving = false;
    EQ(frames(out, bar_goodbye(&bar, out)), "END");
""",
    )


def test_extension_and_parse(tmp_path):
    run(
        tmp_path,
        r"""
    struct bar cfg = {};
    char buf[128];

    /* AAC LC as CTA-861 format code 6 and as extension type 6 (code 15): the request's
     * Audio Format ID 0 names the first, ID 1 (0x46) the second */
    assert(bar_parse("1,350750f907302", &cfg) == -EINVAL); /* not whole descriptors */
    assert(bar_parse("1,350750790730", &cfg) == 0 && cfg.nsads == 2 && cfg.port == 1);
    bar = cfg;
    EQ(tv("05A406"), "50A3350750");
    EQ(tv("05A446"), "50A3790730");
    EQ(tv("05A40646"), "50A3350750790730");
    bar_format(&bar, buf);
    EQ(buf, "1,350750790730\n");
    assert(bar_parse("12,150750", &cfg) == 0 && cfg.port == 12);
    bar_format(&cfg, buf);
    EQ(buf, "12,150750\n");
    assert(bar_parse("off\n", &cfg) == 0 && !cfg.present);
    assert(bar_parse("", &cfg) == 0 && !cfg.present);
    assert(bar_parse("0,150750", &cfg) == -EINVAL);
    assert(bar_parse("16,150750", &cfg) == -EINVAL);
    assert(bar_parse("2", &cfg) == -EINVAL);
    assert(bar_parse("2,", &cfg) == -EINVAL);
    assert(bar_parse("2,15075g", &cfg) == -EINVAL);
    assert(bar_parse("2,000750", &cfg) == -EINVAL); /* format code 0 */
    assert(bar_parse("2,950750", &cfg) == -EINVAL); /* bit 7 */
    assert(bar_parse("2,150750150750150750150750150750150750150750150750150750150750150750150750",
                     &cfg) == 0 && cfg.nsads == 12);
    assert(bar_parse("2,150750150750150750150750150750150750150750150750150750150750150750150750"
                     "150750", &cfg) == -EINVAL);
    /* at most four descriptors in one Report Short Audio Descriptor */
    assert(bar_parse("2,150750150750150750150750150750", &bar) == 0);
    EQ(tv("05A402"), "50A3150750150750150750150750");
""",
    )
