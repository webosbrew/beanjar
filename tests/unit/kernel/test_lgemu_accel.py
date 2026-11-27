#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the accelerometer's register model (accel.c, the BMA422) on the host and check it as
N21D's msc drives it: CHIP_ID, the init writes, the six data bytes at 0x12 (raw >> 4 lands in
msc's orientation bands for each gravity vector), the range, the soft reset and the vector's
parser."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/lgemu"


def test_registers(tmp_path):
    source = (MODULE / "accel.c").read_text()
    start = source.index("/* ---- the BMA422's registers")
    section = source[start : source.index("/* ---- end of the BMA422's registers ---- */")]
    root = tmp_path
    runner = root / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef int16_t s16;
"""
        + section
        + r"""
/* msc's I2C as DILE_I2C does it: a write of the register, then a read */
static void rd(struct bma4 *c, u8 reg, u8 *buf, unsigned int len) { bma4_write(c, &reg, 1); bma4_read(c, buf, len); }
static void wr(struct bma4 *c, u8 reg, u8 v) { u8 b[2] = {reg, v}; bma4_write(c, b, 2); }
/* getXYZ_BMA422: three little-endian signed words, each >> 4 (rounded towards zero for x < 0) */
static void xyz(struct bma4 *c, int *out)
{
    u8 b[6];
    rd(c, BMA4_DATA_8, b, 6);
    for (int i = 0; i < 3; i++) out[i] = (s16)(b[2 * i] | b[2 * i + 1] << 8) / 16;
}
/* calOrient4BAM422's default mode: 1 landscape, 2 portrait, 3 reversed_landscape,
 * 4 reversed_portrait, 0 keep the current one */
static int orient(int x, int y)
{
    if (x >= -255 && x <= 255) {
        if (y >= -601 && y <= -354) return 1;
        if (y >= 354 && y <= 601) return 3;
        return 0;
    }
    if (y >= -255 && y <= 255) {
        if (x >= -601 && x <= -354) return 2;
        if (x >= 354 && x <= 601) return 4;
    }
    return 0;
}
static int orient_of(struct bma4 *c, int x, int y, int z)
{
    int v[3];
    c->mg[0] = x; c->mg[1] = y; c->mg[2] = z;
    xyz(c, v);
    return orient(v[0], v[1]);
}
int main(void)
{
    struct bma4 c = {0};
    u8 b[8];
    int v[3], mg[3];

    bma4_reset(&c);
    c.mg[0] = 0; c.mg[1] = -1000; c.mg[2] = 0;
    /* the selector: CHIP_ID 0x12 at register 0 */
    rd(&c, BMA4_CHIP_ID, b, 1); assert(b[0] == 0x12);
    /* before PWR_CTRL's acc_en the data reads zero, as the chip's reset */
    rd(&c, BMA4_DATA_8, b, 6); for (int i = 0; i < 6; i++) assert(b[i] == 0);
    rd(&c, BMA4_STATUS, b, 1); assert(b[0] == 0);
    /* reset values */
    rd(&c, BMA4_ACC_CONF, b, 2); assert(b[0] == 0xA8 && b[1] == 0x01);
    rd(&c, BMA4_PWR_CONF, b, 2); assert(b[0] == 0x03 && b[1] == 0x00);
    /* msc's init: PWR_CTRL 4, ACC_CONF 0x88 */
    wr(&c, BMA4_PWR_CTRL, 4); wr(&c, BMA4_ACC_CONF, 0x88);
    rd(&c, BMA4_PWR_CTRL, b, 1); assert(b[0] == 4);
    rd(&c, BMA4_ACC_CONF, b, 1); assert(b[0] == 0x88);
    rd(&c, BMA4_STATUS, b, 1); assert(b[0] == 0x80);
    /* the read-only registers keep their values */
    wr(&c, BMA4_CHIP_ID, 0x55); rd(&c, BMA4_CHIP_ID, b, 1); assert(b[0] == 0x12);
    /* 1 g on -y at ±4 g: -512 counts, the word -8192 (0xE000) */
    rd(&c, BMA4_DATA_8, b, 6);
    assert(b[0] == 0 && b[1] == 0 && b[2] == 0x00 && b[3] == 0xE0 && b[4] == 0 && b[5] == 0);
    xyz(&c, v); assert(v[0] == 0 && v[1] == -512 && v[2] == 0);
    /* the pointer advances across reads: x LSB, then the rest */
    rd(&c, BMA4_DATA_8 + 2, b, 1); bma4_read(&c, b + 1, 1); assert(b[0] == 0x00 && b[1] == 0xE0);
    /* each of the four vectors lands in its orientation's band */
    assert(orient_of(&c, 0, -1000, 0) == 1);
    assert(orient_of(&c, -1000, 0, 0) == 2);
    assert(orient_of(&c, 0, 1000, 0) == 3);
    assert(orient_of(&c, 1000, 0, 0) == 4);
    /* the set lying flat: no change */
    assert(orient_of(&c, 0, 0, 1000) == 0);
    /* ACC_RANGE: ±2 g doubles the counts (1 g = 1024: outside msc's band, as on the chip) */
    wr(&c, BMA4_ACC_RANGE, 0); c.mg[0] = 0; c.mg[1] = -1000; xyz(&c, v); assert(v[1] == -1024);
    wr(&c, BMA4_ACC_RANGE, 3); xyz(&c, v); assert(v[1] == -128);
    /* saturation at the 12-bit limits */
    wr(&c, BMA4_ACC_RANGE, 0); c.mg[1] = 4000; xyz(&c, v); assert(v[1] == 2047);
    c.mg[1] = -4000; xyz(&c, v); assert(v[1] == -2048);
    /* a burst write from the pointer on */
    { u8 w[3] = {BMA4_ACC_CONF, 0xA9, 0x02}; bma4_write(&c, w, 3); }
    rd(&c, BMA4_ACC_CONF, b, 2); assert(b[0] == 0xA9 && b[1] == 0x02);
    /* the soft reset: reset values, acc_en off, the vector kept */
    wr(&c, BMA4_CMD, BMA4_CMD_SOFTRESET);
    rd(&c, BMA4_PWR_CTRL, b, 1); assert(b[0] == 0);
    rd(&c, BMA4_ACC_RANGE, b, 1); assert(b[0] == 1);
    rd(&c, BMA4_DATA_8, b, 6); for (int i = 0; i < 6; i++) assert(b[i] == 0);
    assert(c.mg[1] == -4000);
    /* the parameter: "x,y,z" with or without a newline, within ±16 g */
    assert(!bma4_parse_mg("1,-2,3", mg) && mg[0] == 1 && mg[1] == -2 && mg[2] == 3);
    assert(!bma4_parse_mg("0,-1000,0\n", mg) && mg[1] == -1000);
    assert(bma4_parse_mg("1,2", mg) == -EINVAL);
    assert(bma4_parse_mg("1,2,3,4", mg) == -EINVAL);
    assert(bma4_parse_mg("1,2,3x", mg) == -EINVAL);
    assert(bma4_parse_mg("0,17000,0", mg) == -EINVAL);
    puts("ok");
    return 0;
}
"""
    )
    binary = root / "runner"
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Werror", "-o", str(binary), str(runner)],
        check=True,
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
