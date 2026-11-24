#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the eARC link of lgemu's /dev/earc0 (earc.c) on the host and drive its controls as
arccontroller does (W22H 43.22.73's nyxEarc and W22O 13.30.56's own calls, port 0 every time):
enable set and got back, the 268-byte connection info polled, the ERX latency request; with no
bar, an eARC bar and an ARC-only bar; and the earc_bar parameter's text both ways."""

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
static u32 get_unaligned_le32(const void *p)
{
    const u8 *b = p;
    return b[0] | b[1] << 8 | b[2] << 16 | (u32)b[3] << 24;
}
static void put_unaligned_le32(u32 v, void *p)
{
    u8 *b = p;
    b[0] = v, b[1] = v >> 8, b[2] = v >> 16, b[3] = v >> 24;
}
"""

# a bar with LPCM 2.0, AC-3 5.1, E-AC-3 with Atmos and DTS, speakers 7.1 (soundbar.earc_capability)
CAP = "0101112c0907071507505707013d06c0830f0800"


def section():
    source = (MODULE / "earc.c").read_text()
    start = source.index("/* ---- the eARC link ---- */")
    return source[start : source.index("/* ---- end of the eARC link ---- */")]


def run(tmp_path, body):
    runner = tmp_path / "runner.c"
    runner.write_text(
        RUNNER
        + section()
        + r"""
static struct earc_link l;
static u8 conn[EARC_CONN_SIZE];
static int enable(u32 state)
{
    u8 p[8] = {0};
    put_unaligned_le32(state, p + 4);
    return earc_control(&l, EARC_ENABLE, true, p, 8);
}
static u32 enabled(void)
{
    u8 p[8] = {0};
    assert(earc_control(&l, EARC_ENABLE, false, p, 8) == 0);
    return get_unaligned_le32(p + 4);
}
static u32 poll(void)
{
    memset(conn, 0, sizeof(conn)); /* the caller zeroes it, port 0 */
    assert(earc_control(&l, EARC_CONN, false, conn, EARC_CONN_SIZE) == 0);
    return get_unaligned_le32(conn + 4);
}
static bool zero(const u8 *p, unsigned int n)
{
    for (unsigned int i = 0; i < n; i++)
        if (p[i])
            return false;
    return true;
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
    result = subprocess.run([exe], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_link(tmp_path):
    run(
        tmp_path,
        r"""
    u8 lat[EARC_CONN_SIZE] = {0};

    /* eARC Support off: disabled, everything zero, bar or not */
    assert(enabled() == 0 && poll() == IDLE1 && zero(conn, sizeof(conn)));
    assert(earc_bar_parse("3,%CAP%\n", &l) == 0 && l.bar && !l.bar_arc && l.cap_len == 20);
    assert(poll() == IDLE1 && zero(conn, sizeof(conn)));
    /* eARC Support on: set, then got back the same */
    assert(enable(1) == 0 && enabled() == 1);
    assert(poll() == EARC_MODE);
    assert(get_unaligned_le32(conn) == 3); /* the bar's input */
    assert(conn[8] == 1 && conn[9] == 1 && conn[10] == 0x11); /* version, block 1, its length */
    assert(conn[11] == 0x2c && conn[12] == 0x09 && conn[24] == 0x83);
    assert(zero(conn + 8 + 20, EARC_CAP_BYTES - 20));
    assert(conn[EARC_LAT] == EARC_BAR_LAT && conn[EARC_LAT_REQ] == 0);
    /* arccontroller's latency request on entering EARC, read back */
    lat[EARC_LAT_REQ] = 7;
    assert(earc_control(&l, EARC_LATENCY, true, lat, EARC_CONN_SIZE) == 0);
    assert(poll() == EARC_MODE && conn[EARC_LAT_REQ] == 7);
    assert(earc_control(&l, EARC_RESET_HPD, true, lat, EARC_CONN_SIZE) == 0);
    /* unplugged: IDLE1, not IDLE2, so no ARC fallback */
    assert(earc_bar_parse("off", &l) == 0 && !l.bar);
    assert(poll() == IDLE1 && get_unaligned_le32(conn) == 0 && zero(conn + 8, EARC_CAP_BYTES));
    /* an ARC-only bar: discovery fails, IDLE2 (arccontroller's ARC_Mode), no capability */
    assert(earc_bar_parse("2,arc", &l) == 0 && l.bar && l.bar_arc);
    assert(poll() == IDLE2 && get_unaligned_le32(conn) == 2 && zero(conn + 8, EARC_CAP_BYTES));
    /* disabled again: zero, and the latency request forgotten */
    assert(enable(0) == 0 && enabled() == 0 && poll() == IDLE1 && zero(conn, sizeof(conn)));
    assert(earc_bar_parse(",%CAP%", &l) == -EINVAL);
    /* what LG's driver would refuse */
    assert(enable(2) == -EINVAL);
    assert(earc_control(&l, EARC_ENABLE, true, lat, 4) == -EINVAL);
    assert(earc_control(&l, EARC_CONN, true, conn, EARC_CONN_SIZE) == -EINVAL);
    assert(earc_control(&l, EARC_CONN, false, conn, 264) == -EINVAL);
    assert(earc_control(&l, EARC_LATENCY, false, lat, EARC_CONN_SIZE) == -EINVAL);
    assert(earc_control(&l, EARC_RESET_HPD, false, lat, EARC_CONN_SIZE) == -EINVAL);
    assert(earc_control(&l, EARC_BASE + 4, false, lat, 8) == -EINVAL);
""".replace("%CAP%", CAP),
    )


def test_parameter(tmp_path):
    run(
        tmp_path,
        r"""
    struct earc_link c = {};
    char buf[600];
    char big[2 * 257 + 8];

    assert(earc_bar_parse("3,%CAP%", &c) == 0);
    earc_bar_format(&c, buf);
    EQ(buf, "3,%CAP%\n");
    assert(earc_bar_parse("12,arc\n", &c) == 0 && c.bar_port == 12 && c.bar_arc);
    earc_bar_format(&c, buf);
    EQ(buf, "12,arc\n");
    assert(earc_bar_parse("\n", &c) == 0 && !c.bar);
    earc_bar_format(&c, buf);
    EQ(buf, "\n");
    assert(earc_bar_parse("0,arc", &c) == -EINVAL);
    assert(earc_bar_parse("16,arc", &c) == -EINVAL);
    assert(earc_bar_parse("3", &c) == -EINVAL);
    assert(earc_bar_parse("3,", &c) == -EINVAL);
    assert(earc_bar_parse("3,010", &c) == -EINVAL);   /* not whole bytes */
    assert(earc_bar_parse("3,01zz", &c) == -EINVAL);
    assert(earc_bar_parse("3,0201", &c) == -EINVAL);  /* version 2 */
    /* 256 bytes at most */
    strcpy(big, "3,01");
    for (int i = 0; i < 255; i++)
        strcat(big, "00");
    assert(earc_bar_parse(big, &c) == 0 && c.cap_len == 256);
    strcat(big, "00");
    assert(earc_bar_parse(big, &c) == -EINVAL && c.cap_len == 256);
""".replace("%CAP%", CAP),
    )
