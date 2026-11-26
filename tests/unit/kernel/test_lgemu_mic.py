#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the microphone's air (mic.c: what the host sends, at the times it is heard) on the host
and check it: a sound after silence starts AIR_DELAY_NS later and runs at 48 kHz, a writer is held
AIR_LEAD_NS ahead, a capture at another rate reads it interpolated, and silence before, after and
long past."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/lgemu"


def test_air(tmp_path):
    source = (MODULE / "mic.c").read_text()
    start = source.index("/* ---- the air: what the microphone hears")
    section = source[start : source.index("/* ---- end of the air ---- */")]
    runner = tmp_path / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int16_t s16;
typedef int32_t s32;
typedef int64_t s64;
"""
        + section
        + r"""
#define MS(n) ((s64)(n) * 1000000)
#define FRAME_NS (AIR_NS / AIR_RATE) /* 20833 */
/* the time of frame n after a sound's start, rounded up to a nanosecond */
#define AT(n) (((s64)(n) * AIR_NS + AIR_RATE - 1) / AIR_RATE)
static s16 buf[AIR_RATE * AIR_CHANNELS];

/* n frames: left i * 10, right -(i * 10) */
static const s16 *ramp(u32 n)
{
    for (u32 i = 0; i < n; i++) {
        buf[2 * i] = (s16)(i * 10);
        buf[2 * i + 1] = (s16)-(i * 10);
    }
    return buf;
}

int main(void)
{
    struct mic_air *a = calloc(1, sizeof(*a));
    s64 t0 = MS(5000), base;

    /* nothing sent: silence */
    assert(air_sample(a, t0, 0) == 0);

    /* a sound after silence: heard from AIR_DELAY_NS on */
    assert(air_write(a, t0, ramp(100), 100) == 100);
    base = t0 + AIR_DELAY_NS;
    assert(a->base_ns == base && a->head == 100);
    assert(air_sample(a, base - 1, 0) == 0);
    assert(air_sample(a, base, 0) == 0 && air_sample(a, base, 1) == 0);
    assert(air_sample(a, base + AT(3), 0) == 30);
    assert(air_sample(a, base + AT(3), 1) == -30);
    /* a 16 kHz capture: every third frame; between frames, interpolated */
    assert(air_sample(a, base + AIR_NS / 16000, 0) == 30);
    assert(air_sample(a, base + AT(3) + FRAME_NS / 2, 0) == 34);
    /* a channel the air lacks hears its last */
    assert(air_sample(a, base + AT(3), 4) == -30);
    /* the last frame holds until the end, then silence */
    assert(air_sample(a, base + AT(99) + FRAME_NS / 2, 0) == 990);
    assert(air_sample(a, air_end(a), 0) == 0);

    /* more while it plays: it continues the same sound, no gap */
    assert(air_write(a, t0 + MS(1), ramp(100), 100) == 100);
    assert(a->base_ns == base && a->head == 200);
    assert(air_sample(a, base + AT(100), 0) == 0);
    assert(air_sample(a, base + AT(101), 0) == 10);

    /* the writer is held AIR_LEAD_NS ahead of now: at t0 it may fill up to t0 + lead */
    u32 room = (u32)((t0 + AIR_LEAD_NS - base) * AIR_RATE / AIR_NS) - 200;
    assert(air_write(a, t0, ramp(AIR_RATE), AIR_RATE) == room);
    assert(air_write(a, t0, ramp(10), 10) == 0);
    /* 100 ms later, 100 ms more */
    assert(air_write(a, t0 + MS(100), ramp(AIR_RATE), AIR_RATE) == AIR_RATE / 10);

    /* after the end, silence, and the next sound starts afresh */
    s64 later = air_end(a) + MS(1000);
    assert(air_sample(a, later, 0) == 0);
    assert(air_write(a, later, ramp(10), 10) == 10);
    assert(a->base_ns == later + AIR_DELAY_NS && a->head == 10);
    assert(air_sample(a, later + AIR_DELAY_NS + AT(5), 0) == 50);

    /* a long sound: frames more than the ring behind its head are past, silence */
    memset(a, 0, sizeof(*a));
    s64 now = MS(1000);
    assert(air_write(a, now, ramp(10), 10) == 10);
    base = a->base_ns;
    for (int i = 0; i < 100; i++) {
        now += MS(100);
        while (air_write(a, now, ramp(1000), 1000))
            ;
    }
    assert(a->head > AIR_FRAMES);
    assert(air_sample(a, base + AT(5), 0) == 0);
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
