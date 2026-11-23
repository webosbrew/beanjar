#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile mstar-utopia's software I2C slave (swi2c.c) on the host and drive its two pad bytes as
LM14's libapiSWI2C does through libdrvGPIO (open drain: set_low clears OUT then OEN, set_input sets
OEN, get_level reads IN; START, address with up to 3 attempts, bytes MSB first with the ninth
clock's ACK, ReadBytes' repeated START, STOP), with a 32 KiB EEPROM (2-byte big-endian address,
wrapping) behind it at 0xA0, as RELEASE's NVM driver uses it: 32-byte page writes, a page read,
the 1-byte ACK poll at 0xFFFF, a long sequential read, and an address nobody answers; and the
bank's other pads, lines on their pull-ups (swi2c_pullup)."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/mstar-utopia"


def test_bus(tmp_path):
    source = (MODULE / "swi2c.c").read_text()
    start = source.index("/* ---- the software I2C slave ---- */")
    section = source[start : source.index("/* ---- end of the software I2C slave ---- */")]
    runner = tmp_path / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
"""
        + section
        + r"""
/* the EEPROM: lgemu's nvm device (2-byte address, 32 KiB, wraps) */
static u8 rom[32768];
static int xfers;
static int eeprom(void *ctx, struct swi2c_msg *m, int n)
{
    unsigned a;
    xfers++;
    if (m[0].read || m[0].len < 2) return -1;
    a = (m[0].buf[0] << 8 | m[0].buf[1]) % sizeof(rom);
    if (n == 1) { for (unsigned i = 2; i < m[0].len; i++) rom[(a + i - 2) % sizeof(rom)] = m[0].buf[i]; return 0; }
    for (unsigned i = 0; i < m[1].len; i++) m[1].buf[i] = rom[(a + i) % sizeof(rom)];
    return 0;
}

/* the pads as libdrvGPIO sees them: bit 0 OUT, 1 OEN, 2 IN */
static struct swi2c bus;
static u8 sda = 6, scl = 6;
static long accesses;
static void wr(u8 *pad, u8 v) { *pad = v; swi2c_pads(&bus, &sda, &scl); accesses++; }
static int level(u8 *pad) { accesses++; return !!(*pad & 4); }
static void set_low(u8 *pad) { wr(pad, *pad & ~1); wr(pad, *pad & ~2); }
static void set_input(u8 *pad) { wr(pad, *pad | 2); }
static void scl_high(void) { set_input(&scl); for (int i = 0; i < 5000 && !level(&scl); i++); }
static void sda_high(void) { set_input(&sda); for (int i = 0; i < 5000 && !level(&sda); i++); }
static int start(void)
{
    sda_high(); scl_high(); set_input(&scl); set_input(&sda);
    if (!level(&scl) || !level(&sda)) { set_input(&scl); set_input(&sda); return 0; }
    set_low(&sda); set_low(&scl); return 1;
}
static void stop(void) { set_low(&scl); set_low(&sda); set_input(&scl); set_input(&sda); }
static int send(u8 v)
{
    int ack;
    for (int i = 7; i >= 0; i--) { if (v >> i & 1) sda_high(); else set_low(&sda); scl_high(); set_low(&scl); }
    set_input(&sda); scl_high(); ack = !level(&sda); set_low(&scl);
    if (ack) { set_low(&sda); set_input(&sda); } else { set_input(&sda); set_input(&sda); }
    return ack;
}
static u8 get(int last)
{
    u8 v = 0;
    set_input(&sda);
    for (int i = 0; i < 8; i++) { scl_high(); v = v << 1 | level(&sda); set_low(&scl); set_low(&scl); }
    if (last) sda_high(); else set_low(&sda);
    scl_high(); set_low(&scl);
    return v;
}
static int address(u8 a)
{
    for (int i = 0; i < 3; i++) { if (send(a)) return 1; stop(); if (!start()) return 0; }
    return 0;
}
static int write_bytes(u8 slave, const u8 *sub, int sublen, const u8 *buf, int len)
{
    int ok = start() && address(slave & 0xfe);
    for (int i = 0; ok && i < sublen; i++) ok = send(sub[i]);
    for (int i = 0; ok && i < len; i++) ok = send(buf[i]);
    stop();
    return ok;
}
static int read_bytes(u8 slave, const u8 *sub, int sublen, u8 *buf, int len)
{
    int ok = start() && address(slave & 0xfe);
    for (int i = 0; ok && i < sublen; i++) ok = send(sub[i]);
    ok = ok && start() && address(slave | 1);
    for (int i = 0; ok && i < len; i++) buf[i] = get(i == len - 1);
    stop();
    return ok;
}

int main(void)
{
    u8 page[32], back[300], sub[2], poll;
    int fails = 0;

    bus.xfer = eeprom;
    bus.slave = 0xa0;
    swi2c_reset(&bus);
    memset(rom, 0xff, sizeof(rom));
    /* a blank chip reads 0xff, as RELEASE's first boot finds it */
    sub[0] = 0x07; sub[1] = 0x04;
    if (!read_bytes(0xa0, sub, 2, back, 4) || memcmp(back, "\xff\xff\xff\xff", 4)) fails++;
    /* InitModelInfoDB's page writes, each within its 32-byte page, then the ACK poll */
    for (int p = 0x700; p < 0x740; p += 32) {
        for (int i = 0; i < 32; i++) page[i] = p + i;
        sub[0] = p >> 8; sub[1] = p;
        if (!write_bytes(0xa0, sub, 2, page, 32)) fails++;
        sub[0] = sub[1] = 0xff;
        if (!read_bytes(0xa0, sub, 2, &poll, 1)) fails++;
    }
    for (int i = 0; i < 64; i++) if (rom[0x700 + i] != (u8)(0x700 + i)) fails++;
    /* a page read back, and a read longer than one prefetch */
    sub[0] = 0x07; sub[1] = 0x20;
    if (!read_bytes(0xa0, sub, 2, back, 32) || memcmp(back, rom + 0x720, 32)) fails++;
    for (int i = 0; i < 300; i++) rom[0x1000 + i] = i * 7;
    sub[0] = 0x10; sub[1] = 0x00;
    if (!read_bytes(0xa0, sub, 2, back, 300) || memcmp(back, rom + 0x1000, 300)) fails++;
    /* a 1-byte write (the group code) and its read */
    sub[0] = 0x07; sub[1] = 0x26; page[0] = 0x04;
    if (!write_bytes(0xa0, sub, 2, page, 1) || rom[0x726] != 4) fails++;
    if (!read_bytes(0xa0, sub, 2, back, 1) || back[0] != 4) fails++;
    /* nobody at 0xA2: not acknowledged, and nothing reaches the device */
    int before = xfers;
    if (write_bytes(0xa2, sub, 2, page, 1) || xfers != before) fails++;
    /* another pad of the bank (the R9531's bus): its pull-up only. Released it reads high at
     * once, driven low it reads low; on such a bus the ACK clock reads SDA high: a NACK */
    u8 other = 0;
    other = swi2c_pullup(other | 2);
    if (!(other & 4)) fails++;
    other = swi2c_pullup(other & ~1);
    other = swi2c_pullup(other & ~2);
    if (other & 4) fails++;
    other = swi2c_pullup(other | 2);
    if (!(other & 4)) fails++;
    /* the bus is idle and released afterwards */
    if (bus.phase != SWI2C_IDLE || !(sda & 4) || !(scl & 4)) fails++;
    printf("fails %d starts %u stops %u messages %u refused %u accesses %ld\n", fails, bus.starts,
           bus.stops, bus.messages, bus.refused, accesses);
    return fails != 0;
}
"""
    )
    exe = tmp_path / "runner"
    subprocess.run(
        [
            "cc",
            "-std=gnu11",
            "-Wall",
            "-Werror",
            "-Wno-unused-function",
            "-o",
            str(exe),
            str(runner),
        ],
        check=True,
    )
    r = subprocess.run([str(exe)], capture_output=True, text=True)
    print(r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr
