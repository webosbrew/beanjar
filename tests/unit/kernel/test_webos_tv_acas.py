#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile webos_tv.c's card answers (acas_*) on the host and send them the commands
legacy-broadcast-arib's BCAS_EncodeAPDU sends (W22O queue 43.21.89): INT `90 30 00 02 00`, IDI
`90 32 00 01 00` (bare, and in the T=1 block an ACAS build wraps them in), an ECM, the power-on control request (0x80), an EMM, and things that are no B-CAS APDU. Each answer is read the way
BCAS_DecodeExtractInfoFromResponse reads it: 00 | N | IC instruction | return code | data | 90 00,
N + 4 bytes in all; INT's CA system ID 0x0005 and card ID, IDI's one 10-byte entry from byte 7
(maker letter, version, card ID, check code); the ECM not contracted (0xA103), no power-on control (0xA101); no key anywhere."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/webos_tv"

RUNNER = r"""
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
static int hex_to_bin(unsigned char c)
{
    return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10
         : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
}
@SECTION@
static unsigned be16(const u8 *p) { return p[0] << 8 | p[1]; }
/* the firmware's checks: resp[0] 0, resp[1] + 4 the length, SW 90 00 at the end */
static unsigned rc_of(const u8 *r, size_t n)
{
    assert(n >= 8 && r[0] == 0 && r[1] + 4u == n && r[n - 2] == 0x90 && r[n - 1] == 0);
    return be16(r + 4);
}
int main(void)
{
    struct acas_card card;
    u8 rx[0xfe];
    size_t n;
    static const u8 id[6] = {0xbe, 0x12, 0x34, 0x56, 0x78, 0x9a};

    assert(acas_parse_id(&card, "be123456789a") == 0 && !memcmp(card.id, id, 6));
    assert(acas_parse_id(&card, "BE123456789A") == 0 && !memcmp(card.id, id, 6));
    assert(acas_parse_id(&card, "be123456789") == -1);
    assert(acas_parse_id(&card, "be123456789a0") == -1);
    assert(acas_parse_id(&card, "be12345678zz") == -1);
    acas_parse_id(&card, "be123456789a");

    /* INT: CA system ID, card ID, type, partition length, then 32 + 8 zero key bytes */
    static const u8 int_cmd[] = {0x90, 0x30, 0x00, 0x02, 0x00};
    n = acas_answer(&card, int_cmd, sizeof int_cmd, rx, sizeof rx);
    assert(n == 59 && rc_of(rx, n) == 0x2100);
    assert(be16(rx + 6) == 0x0005 && !memcmp(rx + 8, id, 6) && rx[14] == 1 && rx[15] == 0x50);
    for (int i = 16; i < 57; i++) assert(rx[i] == 0);

    /* IDI: one ID; the entry at 7: 'B', 1, the card ID, the check code (CRC-16/CCITT) */
    static const u8 idi_cmd[] = {0x90, 0x32, 0x00, 0x01, 0x00};
    n = acas_answer(&card, idi_cmd, sizeof idi_cmd, rx, sizeof rx);
    assert(n == 19 && rc_of(rx, n) == 0x2100 && rx[6] == 1);
    assert(rx[7] == 'B' && rx[8] == 1 && !memcmp(rx + 9, id, 6));
    assert(be16(rx + 15) == acas_check_code(&card));
    struct acas_card known = {{0x31, 0x32, 0x33, 0x34, 0x35, 0x36}}; /* "123456" */
    assert(acas_check_code(&known) == 0x2ef4); /* CRC-16/CCITT-FALSE of "123456" */

    /* the same INT in a T=1 block (NAD 0, PCB 0, LEN 5, the APDU, LRC), as the ACAS build sends it */
    static const u8 int_block[] = {0x00, 0x00, 0x05, 0x90, 0x30, 0x00, 0x02, 0x00, 0xa7};
    u8 bare[0xfe];
    size_t m = acas_answer(&card, int_cmd, sizeof int_cmd, bare, sizeof bare);
    n = acas_answer(&card, int_block, sizeof int_block, rx, sizeof rx);
    assert(n == m && !memcmp(rx, bare, n));
    static const u8 idi_block[] = {0x00, 0x40, 0x05, 0x90, 0x32, 0x00, 0x01, 0x00, 0xe6};
    n = acas_answer(&card, idi_block, sizeof idi_block, rx, sizeof rx);
    assert(n == 19 && rx[6] == 1 && rx[7] == 'B');

    /* ECM (section from byte 8, Lc = section_length - 9): not contracted, no key */
    u8 ecm[5 + 0x20 + 1] = {0x90, 0x34, 0x00, 0x02, 0x20};
    n = acas_answer(&card, ecm, sizeof ecm, rx, sizeof rx);
    assert(n == 8 && rc_of(rx, n) == 0xa103);
    /* power-on control information, entry 1 (in its T=1 block): none */
    static const u8 poc[] = {0x00, 0x00, 0x07, 0x90, 0x80, 0x00, 0x01, 0x01, 0x01, 0x00, 0x17};
    n = acas_answer(&card, poc, sizeof poc, rx, sizeof rx);
    assert(n == 8 && rc_of(rx, n) == 0xa101);
    /* EMM and the other B-CAS commands: taken, nothing */
    static const u8 emm[] = {0x90, 0x36, 0x00, 0x01, 0x02, 0xaa, 0xbb, 0x00};
    n = acas_answer(&card, emm, sizeof emm, rx, sizeof rx);
    assert(n == 8 && rc_of(rx, n) == 0x2100);

    /* not B-CAS: the status word alone */
    static const u8 bad_cla[] = {0x00, 0xa4, 0x04, 0x00, 0x00};
    n = acas_answer(&card, bad_cla, sizeof bad_cla, rx, sizeof rx);
    assert(n == 2 && rx[0] == 0x6e && rx[1] == 0);
    static const u8 bad_ins[] = {0x90, 0x99, 0x00, 0x00, 0x00};
    n = acas_answer(&card, bad_ins, sizeof bad_ins, rx, sizeof rx);
    assert(n == 2 && rx[0] == 0x6d && rx[1] == 0);
    assert(acas_answer(&card, int_cmd, 3, rx, sizeof rx) == 2);

    /* an answer that does not fit the caller's capacity is a failure (length 0) */
    assert(acas_answer(&card, int_cmd, sizeof int_cmd, rx, 58) == 0);
    assert(acas_answer(&card, bad_ins, sizeof bad_ins, rx, 1) == 0);
    printf("ok\n");
    return 0;
}
"""


def test_acas(tmp_path):
    source = (MODULE / "webos_tv.c").read_text()
    start = source.index("/* ---- the card's answers ----")
    section = source[start : source.index("/* ---- end of the card's answers ---- */")]
    (tmp_path / "runner.c").write_text(RUNNER.replace("@SECTION@", section))
    subprocess.run(
        [
            "cc",
            "-std=gnu11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-function",
            "-o",
            str(tmp_path / "runner"),
            str(tmp_path / "runner.c"),
        ],
        check=True,
    )
    out = subprocess.run([str(tmp_path / "runner")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
