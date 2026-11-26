#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the DLP controller's command protocol (dlpc.c) on the host and check it with the
transfers W21Z's panelcontroller makes: the Flash Version read (Get_DPPStatus, Get_Version), the
light status read, the DMD error status read whose response is read alone, and the writes."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/lgemu"


def test_commands(tmp_path):
    source = (MODULE / "dlpc.c").read_text()
    start = source.index("/* ---- the DLP controller's command protocol")
    section = source[start : source.index("/* ---- end of the DLP controller's command protocol")]
    runner = tmp_path / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
"""
        + section
        + r"""
/* DPP6540_ReadRegisters: the header and opcode as the subaddress, then len bytes read after a
 * repeated start */
static void rd(struct dlpc *c, u8 h, u8 op, u8 *buf, unsigned int len)
{
    u8 sub[2] = {h, op};
    dlpc_write(c, sub, 2);
    dlpc_read(c, buf, len);
}
int main(void)
{
    struct dlpc c;
    u8 b[20];

    dlpc_reset(&c);
    /* nothing pending: zeros */
    memset(b, 0xaa, sizeof(b));
    dlpc_read(&c, b, 4);
    assert(!b[0] && !b[1] && !b[2] && !b[3]);

    /* Get_Version: 84 03, 20 bytes; the first 04 is the header, then major, minor, subminor */
    rd(&c, 0x84, 0x03, b, 20);
    assert(b[0] == 0x04 && b[1] == 1 && b[2] == 0 && b[3] == 0);
    for (int i = 4; i < 20; i++)
        assert(b[i] == 0);
    /* Get_DPPStatus: the same command, one byte */
    rd(&c, 0x84, 0x03, b, 1);
    assert(b[0] == 0x04);

    /* Get_InitLEDOnStatus: 81 80, 4 bytes, byte 0 0xff (the light on) */
    rd(&c, 0x81, 0x80, b, 4);
    assert(b[0] == 0xff);

    /* Get_DMDErrorStatus: the command packet written with a 3-byte subaddress, the response read
     * in a transfer of its own (an empty subaddress first changes nothing): byte 3 is 0 */
    {
        u8 cmd[11] = {0xda, 0xff, 0xfe, 0x06, 0x00, 0x85, 0x01, 0x00, 0x04, 0x00, 0x03};
        dlpc_write(&c, cmd, sizeof(cmd));
        dlpc_write(&c, cmd, 0);
        memset(b, 0xaa, sizeof(b));
        dlpc_read(&c, b, 7);
        assert(b[0] == 0x12); /* destination 2 with a length field, no error, not busy */
        assert(b[1] == 4 && b[2] == 0);
        assert(b[3] == 0 && b[4] == 0 && b[5] == 0 && b[6] == 0);
    }

    /* a write command drops the pending response, and is not answered */
    rd(&c, 0x84, 0x03, b, 1);
    {
        u8 w[4] = {0x04, 0x49, 0x01, 0x02};
        dlpc_write(&c, w, sizeof(w));
    }
    memset(b, 0xaa, sizeof(b));
    dlpc_read(&c, b, 4);
    assert(!b[0] && !b[1] && !b[2] && !b[3]);

    /* another read command: the header with its destination, data zeros */
    rd(&c, 0x84, 0x06, b, 5);
    assert(b[0] == 0x04 && !b[1] && !b[2] && !b[3] && !b[4]);
    puts("ok");
    return 0;
}
"""
    )
    binary = tmp_path / "runner"
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Werror", "-o", str(binary), str(runner)],
        check=True,
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
