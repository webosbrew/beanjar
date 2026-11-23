#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile mstar-utopia's kernel-mode GPIO (gpio.c) on the host and drive it as W15U's
libkadaptor does: pad levels through commands 1, 2, 3 and 5, and MApi_SWI2C's transactions on a
software I2C bus (a start bit-banged with the pad commands, then command 7 for bytes, 8 for a read
and 9 for the stop), with a recording device behind slave 0x54 on pads 47/48 (soc/lm15u.toml)."""

from pathlib import Path
import subprocess

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/mstar-utopia"

HARNESS = r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
#define module_param(...)
#define module_param_array(...)
#define MODULE_PARM_DESC(...)
#define DEFINE_MUTEX(m) int m
#define mutex_lock(m) ((void)(m))
#define mutex_unlock(m) ((void)(m))
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define __user
#define UTOPIA_STATUS_SUCCESS 0
/* user memory: a 32-bit address is an offset in this arena */
static u8 arena[4096];
#define compat_ptr(x) ((void *)(arena + (x)))
#define copy_from_user(d, s, n) (memcpy((d), (s), (n)), 0)
#define copy_to_user(d, s, n) (memcpy((d), (s), (n)), 0)
#define put_user(v, p) (*(p) = (v), 0)
#define I2C_M_RD 1
struct i2c_msg { u16 addr, flags, len; u8 *buf; };
enum lgemu_i2c_device { LGEMU_I2C_NONE, LGEMU_I2C_AMP };
static int lgemu_parse_u32s(const char *spec, unsigned int base, u32 *out, int n)
{
    return sscanf(spec, "%u:%u", &out[0], &out[1]) == n ? 0 : -EINVAL;
}
static int lgemu_i2c_parse(const char *spec, enum lgemu_i2c_device *dev, unsigned int *n, int count)
{
    *dev = LGEMU_I2C_AMP;
    return sscanf(spec, "amp:%u:%u:%x", &n[0], &n[1], &n[2]) == count ? 0 : -EINVAL;
}
/* the device: records each transfer */
static int xfers, last_num;
static struct i2c_msg last[2];
static u8 last_bytes[2][64];
static int lgemu_i2c_xfer(enum lgemu_i2c_device dev, struct i2c_msg *msgs, int num)
{
    assert(dev == LGEMU_I2C_AMP);
    xfers++;
    last_num = num;
    for (int i = 0; i < num; i++) {
        last[i] = msgs[i];
        if (msgs[i].flags & I2C_M_RD)
            for (int j = 0; j < msgs[i].len; j++)
                msgs[i].buf[j] = 0x10 + j;
        else
            memcpy(last_bytes[i], msgs[i].buf, msgs[i].len);
    }
    return num;
}
"""

MAIN = r"""
enum { ARG = 0, DATA = 64, RESULT = 128 };

static u32 pad(u32 cmd, u32 p)
{
    u32 a[2] = {p, 0x55};
    memcpy(arena + ARG, a, sizeof(a));
    assert(gpio_ioctl(cmd, ARG) == 0);
    memcpy(a, arena + ARG, sizeof(a));
    return a[1];
}

static void start(u32 scl, u32 sda)
{
    pad(3, sda); pad(3, scl);
    assert(pad(5, sda) == 1 && pad(5, scl) == 1);
    pad(2, sda); pad(2, scl);
}

/* command 7 or 8 on the bus: the result byte (1, unless the kernel says NACK) */
static u8 bus(u32 cmd, u32 scl, u32 sda, const u8 *bytes, u16 len)
{
    struct gpio_bus_arg32 a = {.len = len, .scl = scl, .sda = sda, .data = DATA, .result = RESULT};
    if (bytes)
        memcpy(arena + DATA, bytes, len);
    arena[RESULT] = 1;
    memcpy(arena + ARG, &a, sizeof(a));
    assert(gpio_ioctl(cmd, ARG) == 0);
    return arena[RESULT];
}

int main(void)
{
    static char i2c0[] = "amp:47:48:0x54", lvl0[] = "77:1";
    gpio_i2c[0] = i2c0;
    gpio_i2c_count = 1;
    gpio_pad_level[0] = lvl0;
    gpio_pad_level_count = 1;
    assert(gpio_parse() == 0);

    /* levels: a released pad reads its board level, a driven one what it was driven to */
    assert(pad(5, 77) == 1 && pad(5, 20) == 0 && pad(5, 47) == 1 && pad(5, 48) == 1);
    pad(1, 20); assert(pad(5, 20) == 1);
    pad(2, 77); assert(pad(5, 77) == 0);
    pad(3, 77); assert(pad(5, 77) == 1);

    /* a write: address 0x54, register 0x02, data 0xab 0xcd; one device write at the stop */
    start(47, 48);
    u8 w[] = {0x54, 0x02, 0xab, 0xcd};
    assert(bus(7, 47, 48, w, 1) == 1);
    assert(bus(7, 47, 48, w + 1, 3) == 1);
    assert(xfers == 0);
    bus(9, 47, 48, NULL, 0);
    assert(xfers == 1 && last_num == 1 && last[0].addr == 0x2a && last[0].len == 3);
    assert(!memcmp(last_bytes[0], w + 1, 3));

    /* a register read: write 0x05, repeated start, read address, two bytes */
    xfers = 0;
    start(47, 48);
    u8 r[] = {0x54, 0x05, 0x55};
    assert(bus(7, 47, 48, r, 2) == 1);
    start(47, 48);
    assert(bus(7, 47, 48, r + 2, 1) == 1);
    assert(xfers == 0);
    assert(bus(8, 47, 48, NULL, 2) == 1);
    assert(xfers == 1 && last_num == 2 && last[0].len == 1 && last_bytes[0][0] == 0x05);
    assert((last[1].flags & I2C_M_RD) && last[1].len == 2);
    assert(arena[DATA] == 0x10 && arena[DATA + 1] == 0x11);
    bus(9, 47, 48, NULL, 0);
    assert(xfers == 1);

    /* a slave nobody answers on the bus: NACK, nothing passed on */
    xfers = 0;
    start(47, 48);
    u8 x[] = {0x60, 0x01};
    assert(bus(7, 47, 48, x, 2) == 0);
    bus(9, 47, 48, NULL, 0);
    assert(xfers == 0);

    /* a bus with no device of ours: NACK, a read gives 0xff */
    u8 y[] = {0xa0};
    assert(bus(7, 43, 44, y, 1) == 0);
    assert(bus(8, 43, 44, NULL, 2) == 0 && arena[DATA] == 0xff && arena[DATA + 1] == 0xff);
    assert(xfers == 0);
    return 0;
}
"""


def test_pads_and_bus(tmp_path):
    source = (MODULE / "gpio.c").read_text().replace('#include "private.h"\n', "")
    runner = tmp_path / "runner.c"
    runner.write_text(HARNESS + source + MAIN)
    binary = tmp_path / "runner"
    subprocess.run(
        [
            "cc",
            "-std=gnu11",
            "-Wall",
            "-Werror",
            "-Wno-unused-function",
            "-o",
            str(binary),
            str(runner),
        ],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
