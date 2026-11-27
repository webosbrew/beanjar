#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the production PM micom peer (mailbox.c pm_take) against a recording micom."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/mstar-utopia"

HARNESS = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define I2C_M_RD 1
struct i2c_msg { u16 flags, len; u8 *buf; };
struct mbx_msg { u32 role, type; u8 ctrl, cls, index, count; u8 params[10]; u8 s0, s1; };
enum { MBX_SUCCESS = 0, MBX_ERR_INVALID_PARAM = 7, MBX_ERR_PEER_CPU_NOT_ALIVE = 9 };
#define MBX_ROLE_PM 2
#define MBX_MSG_NORMAL 0
#define MBX_MSG_INSTANT 1
enum { LGEMU_I2C_MICOM };
static int xfers, fail, arrived;
static u8 written[16], written_len;
static struct mbx_msg reply;
static int lgemu_i2c_xfer(int dev, struct i2c_msg *msgs, int num)
{
    assert(dev == LGEMU_I2C_MICOM);
    xfers++;
    if (fail)
        return -5;
    if (num == 1) {
        assert(!(msgs[0].flags & I2C_M_RD));
        memcpy(written, msgs[0].buf, msgs[0].len);
        written_len = msgs[0].len;
    } else {
        assert(num == 2 && msgs[0].len == 1 && (msgs[1].flags & I2C_M_RD));
        for (int i = 0; i < msgs[1].len; i++)
            msgs[1].buf[i] = msgs[0].buf[0] + i;
    }
    return num;
}
static void mbx_arrive(const struct mbx_msg *msg) { arrived++; reply = *msg; }
"""

MAIN = r"""
/* the message _MAdp_MBX_HK2PM_SendMsg builds, for a read and a write alike */
static u32 send(u8 index, u8 count, const u8 *params)
{
    struct mbx_msg m = {.role = MBX_ROLE_PM, .type = MBX_MSG_INSTANT, .cls = 48, .index = index,
                        .count = count};
    memcpy(m.params, params, count);
    xfers = arrived = written_len = 0;
    return pm_take(&m);
}
static void expect_write(u8 index, u8 count, const u8 *params)
{
    assert(send(index, count, params) == MBX_SUCCESS);
    assert(xfers == 1 && !arrived && written_len == 1 + count && written[0] == index);
    assert(!memcmp(written + 1, params, count));
}
int main(void)
{
    static const u8 off[] = {0x00, 0x01}, restart[] = {0x00, 0x05}, standby[] = {0x07, 0, 0};
    static const u8 one[] = {1}, two[] = {2}, three[] = {3}, warm[] = {0x02, 0x00};

    expect_write(0x25, 2, off);      /* power off */
    expect_write(0x25, 2, warm);     /* Quick Start+'s warm standby */
    expect_write(0x44, 2, restart);  /* restart */
    expect_write(0x26, 3, standby);  /* instant control 26 07 */
    expect_write(0x32, 1, one);      /* the baud rate, a read's shape */
    expect_write(0x27, 0, NULL);     /* the power-off timeout */
    expect_write(0x52, 0, NULL);     /* cancel power-off */
    expect_write(0x70, 1, two);      /* Set_ToolType: not Get_EyeSensorStatus's length */

    /* the served reads: a NORMAL reply with the requested bytes */
    assert(send(0xb1, 1, one) == MBX_SUCCESS && xfers == 1 && arrived == 1);
    assert(reply.role == MBX_ROLE_PM && reply.type == MBX_MSG_NORMAL && reply.cls == 48);
    assert(reply.index == 0xb1 && reply.count == 1 && reply.params[0] == 0xb1);
    assert(send(0xc0, 1, three) == MBX_SUCCESS && arrived == 1 && reply.count == 3);
    assert(reply.params[2] == 0xc2);
    assert(send(0x9a, 1, one) == MBX_SUCCESS && arrived == 1);
    /* the battery's status (N21D webOS 24: 10 bytes, webOS 6: 8), charging time, I2C test and
     * self-diagnosis */
    assert(send(0x0c, 1, (const u8[]){10}) == MBX_SUCCESS && arrived == 1 && reply.count == 10);
    assert(reply.index == 0x0c && reply.params[9] == 0x0c + 9);
    assert(send(0x0c, 1, (const u8[]){8}) == MBX_SUCCESS && reply.count == 8);
    assert(send(0x09, 1, (const u8[]){10}) == MBX_SUCCESS && arrived == 1);
    assert(send(0x0b, 1, one) == MBX_SUCCESS && arrived == 1);
    assert(send(0x0d, 1, (const u8[]){10}) == MBX_SUCCESS && arrived == 1);
    assert(send(0x0f, 1, (const u8[]){10}) == MBX_SUCCESS && arrived == 1);

    /* other reads, and the two commands also read with a length of 1, keep the peer failure */
    assert(send(0xa1, 1, (const u8[]){7}) == MBX_ERR_PEER_CPU_NOT_ALIVE && !xfers && !arrived);
    assert(send(0x70, 1, one) == MBX_ERR_PEER_CPU_NOT_ALIVE && !xfers);
    assert(send(0xf5, 1, one) == MBX_ERR_PEER_CPU_NOT_ALIVE && !xfers);
    /* neither a write nor a read */
    assert(send(0xa1, 0, NULL) == MBX_ERR_INVALID_PARAM && !xfers);
    assert(send(0xb1, 1, (const u8[]){11}) == MBX_ERR_INVALID_PARAM && !xfers);

    /* a micom that fails a write, and another class */
    fail = 1;
    assert(send(0x44, 2, restart) == MBX_ERR_PEER_CPU_NOT_ALIVE && xfers == 1);
    assert(send(0xb1, 1, one) == MBX_ERR_PEER_CPU_NOT_ALIVE && !arrived);
    fail = 0;
    struct mbx_msg other = {.role = MBX_ROLE_PM, .cls = 47, .index = 0x44};
    assert(pm_take(&other) == MBX_ERR_INVALID_PARAM);
    return 0;
}
"""


def test_reads_and_writes(tmp_path):
    source = (MODULE / "mailbox.c").read_text()
    start = source.index("static const u8 pm_writes[]")
    end = source.index("/* SENDMSG (MStar's _MDrv_MBX_SendMsg)", start)
    root = tmp_path
    runner = root / "runner.c"
    runner.write_text(HARNESS + source[start:end] + MAIN)
    binary = root / "runner"
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Werror", "-o", str(binary), str(runner)],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
