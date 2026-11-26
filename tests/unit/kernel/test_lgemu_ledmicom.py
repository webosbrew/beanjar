#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the gaming LED micom's model (ledmicom.c) on the host and drive it as the stock
lightingcontroller does (docs/research/led-lighting.md, "The packets on the I2C bus"): each
command one write of the command byte and its payload, kept per command with its count and the
order of the last one; the version read at 0x45; an LED array longer than the kept payload marked
truncated."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/lgemu"


def test_registers(tmp_path):
    source = (MODULE / "ledmicom.c").read_text()
    start = source.index("/* ---- the LED micom's registers")
    section = source[start : source.index("/* ---- end of the LED micom's registers ---- */")]
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
typedef uint32_t u32;
"""
        + section
        + r"""
/* sendArrayData: DILE_I2C_Write(handle, 1, &cmd, n, payload): one message, the command first */
static void send(struct led_micom *c, u8 cmd, const u8 *p, unsigned int n)
{
    u8 b[300];
    b[0] = cmd;
    memcpy(b + 1, p, n);
    led_write(c, b, n + 1);
}

int main(void)
{
    static struct led_micom c;
    u8 v[7];

    led_reset(&c);
    /* GetGamingMicomVersion: 0x45 written, 7 bytes read */
    { u8 reg = 0x45; led_write(&c, &reg, 1); led_read(&c, v, 7); }
    assert(v[0] == '1' && v[2] == '0' && v[3] == '0' && v[5] == '0');
    assert(c.writes == 0 && c.reads == 1);
    /* another register reads zero */
    { u8 reg = 0x10, b[2] = {0xff, 0xff}; led_write(&c, &reg, 1); led_read(&c, b, 2);
      assert(b[0] == 0 && b[1] == 0); }

    /* setLightingEnable: 0x21 0x02, twice 20 ms apart */
    send(&c, 0x21, (u8[]){0x02}, 1);
    send(&c, 0x21, (u8[]){0x02}, 1);
    assert(c.cmds[0x21].count == 2 && c.cmds[0x21].len == 1 && c.cmds[0x21].data[0] == 0x02);
    assert(c.cmds[0x21].order == 2);
    /* setBrightness 8, then a static colour */
    send(&c, 0x2a, (u8[]){8}, 1);
    send(&c, 0x26, (u8[]){0xff, 0x40, 0x00}, 3);
    assert(c.cmds[0x2a].data[0] == 8 && c.cmds[0x2a].order == 3);
    assert(c.cmds[0x26].len == 3 && c.cmds[0x26].data[1] == 0x40 && c.cmds[0x26].order == 4);
    /* the last payload replaces the one before */
    send(&c, 0x2a, (u8[]){3}, 1);
    assert(c.cmds[0x2a].data[0] == 3 && c.cmds[0x2a].count == 2 && c.cmds[0x2a].order == 5);
    assert(c.writes == 5);
    /* a sync chunk: 40 LEDs' RGB fits */
    { u8 p[120]; for (int i = 0; i < 120; i++) p[i] = i;
      send(&c, 0x22, p, 120);
      assert(c.cmds[0x22].len == 120 && !c.cmds[0x22].truncated && c.cmds[0x22].data[119] == 119); }
    /* a longer payload is kept up to LED_PAYLOAD_MAX and marked */
    { u8 p[200] = {0}; p[127] = 0x7f;
      send(&c, 0x23, p, 200);
      assert(c.cmds[0x23].len == LED_PAYLOAD_MAX && c.cmds[0x23].truncated);
      assert(c.cmds[0x23].data[127] == 0x7f); }
    /* an empty message changes nothing */
    led_write(&c, NULL, 0);
    assert(c.writes == 7);
    led_reset(&c);
    assert(c.writes == 0 && !c.cmds[0x21].count);
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
