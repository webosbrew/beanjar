#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the micom's battery reads (i2c.c battery_read) on the host and check them as N21D's
libmicom_sensor and tvbatteryd read them: 0x0C's state and level, its temperatures in 0.1 K, the
self-diagnosis words of 0x0D and 0x0F, and no answer on a set without a battery."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/lgemu"

MAIN = r"""
int main(void)
{
    u8 buf[10];

    /* no battery: none of its reads is answered */
    assert(battery_read(0x0C, buf, 10) == -EREMOTEIO);
    assert(battery_read(0x0B, buf, 1) == -EREMOTEIO);

    battery = true;
    /* webOS 24's status, 10 bytes: charging, on AC, not full_charged, level 100 */
    memset(buf, 0xa5, sizeof(buf));
    assert(battery_read(0x0C, buf, 10) == 0);
    assert(buf[0] == 1 && buf[1] == 1 && buf[2] == 0 && buf[3] == 100);
    /* readMicomBatteryInfo: big-endian 0.1 K, divided by 10, less 273.0; 25.0 C */
    for (int i = 4; i < 8; i += 2) {
        int t = (buf[i] << 8 | buf[i + 1]);
        assert(t / 10.0 - 273.0 == 25.0);
    }
    assert(!buf[8] && !buf[9]);
    /* webOS 6's status, 8 bytes */
    memset(buf, 0xa5, sizeof(buf));
    assert(battery_read(0x0C, buf, 8) == 0 && buf[3] == 100 && buf[8] == 0xa5);
    assert(battery_read(0x0C, buf, 7) == -EREMOTEIO);
    /* the charging time info: zeros */
    memset(buf, 0xa5, sizeof(buf));
    assert(battery_read(0x09, buf, 10) == 0);
    for (int i = 0; i < 10; i++)
        assert(!buf[i]);
    /* the battery's I2C test: 1, "OK" */
    assert(battery_read(0x0B, buf, 1) == 0 && buf[0] == 1);
    /* the self-diagnosis: 0x0D's voltages in mV, 0x0F's cycle count, protection and operation
     * modes and BMS firmware, each a big-endian word */
    assert(battery_read(0x0D, buf, 10) == 0);
    unsigned int cells = 0;
    for (int i = 1; i < 5; i++) {
        unsigned int mv = buf[2 * i] << 8 | buf[2 * i + 1];
        assert(mv >= 4100 && mv <= 4200);
        cells += mv;
    }
    assert((buf[0] << 8 | buf[1]) == cells);
    assert(battery_read(0x0F, buf, 10) == 0);
    assert((buf[0] << 8 | buf[1]) == 12 && !buf[2] && !buf[3] && !buf[4] && !buf[5]);
    assert((buf[6] << 8 | buf[7]) == 0x0110 && !buf[8] && !buf[9]);
    /* other battery commands (hall IC, quality test) stay unanswered */
    assert(battery_read(0x83, buf, 5) == -EREMOTEIO);
    assert(battery_read(0x07, buf, 1) == -EREMOTEIO);
    assert(battery_read(0x0C, buf, 0) == -EREMOTEIO);
    return 0;
}
"""


def test_battery_reads(tmp_path):
    source = (MODULE / "i2c.c").read_text()
    start = source.index("/* battery=1:")
    section = source[start : source.index("/* micom_trace=1:", start)]
    root = tmp_path
    runner = root / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define module_param(name, type, mode)
#define MODULE_PARM_DESC(name, text)
"""
        + section
        + MAIN
    )
    binary = root / "runner"
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Werror", "-o", str(binary), str(runner)],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
