#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the micom's IR receiver and local key (i2c.c) on the host with a controlled clock and
check the 0xC0 poll replies for remote and local key edges, the keys left at a suspend; and its
front LED latches."""

from pathlib import Path
import subprocess

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/lgemu"
INCLUDE = MODULE.parents[3] / "include"


def test_poll_replies(tmp_path):
    source = (MODULE / "i2c.c").read_text()
    start = source.index("/* ---- the micom's IR receiver ---- */")
    section = source[start : source.index("/* Direct MStar IR controllers", start)]
    root = tmp_path
    runner = root / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "webos-ir.h"
typedef uint8_t u8;
typedef uint64_t u64;
#define NSEC_PER_MSEC 1000000ULL
static u64 clock_ns;
static u64 ktime_get_ns(void) { return clock_ns; }
#define DEFINE_SPINLOCK(name) int name
#define spin_lock(l) ((void)(l))
#define spin_unlock(l) ((void)(l))
#define module_param(name, type, mode)
#define MODULE_PARM_DESC(name, text)
#define pr_info(...) ((void)0)
#define pr_info_ratelimited(...) ((void)0)
"""
        + section
        + r"""
static void poll_is(u8 event, u8 code)
{
    u8 buf[5];
    memset(buf, 0xa5, sizeof(buf));
    ir_poll(buf, sizeof(buf));
    if (!event) { assert(!buf[0] && !buf[1] && !buf[2] && !buf[3] && !buf[4]); return; }
    assert(buf[0] == event && buf[1] == code && buf[2] == (u8)~code && !buf[3] && !buf[4]);
}
static void at(u64 ms) { clock_ns = ms * NSEC_PER_MSEC; }
int main(void)
{
    /* a tap of the one key: nothing while it is down, 10 EA at its release */
    at(1000); poll_is(0, 0);
    spin_lock(&ir_lock); local_edge(true, WEBOS_LOCAL_PRESS); spin_unlock(&ir_lock);
    at(1200); poll_is(0, 0);
    local_edge(false, WEBOS_LOCAL_PRESS);
    poll_is(0x10, 0xea); poll_is(0, 0);
    /* a hold: 04 EB once at local_long_ms, nothing more while held, 0E EB at the release */
    at(2000); local_edge(true, WEBOS_LOCAL_PRESS);
    at(2000 + LOCAL_LONG_MS - 1); poll_is(0, 0);
    at(2000 + LOCAL_LONG_MS); poll_is(0x04, 0xeb);
    at(6000); poll_is(0, 0);
    local_edge(false, WEBOS_LOCAL_PRESS); poll_is(0x0e, 0xeb); poll_is(0, 0);
    /* a joystick direction: 04 down, 05 repeats as the remote's, 0E up */
    at(10000); local_edge(true, WEBOS_LOCAL_UP); poll_is(0x04, 0x00);
    at(10000 + IR_FIRST_REPEAT_MS - 1); poll_is(0, 0);
    at(10000 + IR_FIRST_REPEAT_MS); poll_is(0x05, 0x00);
    at(10000 + IR_FIRST_REPEAT_MS + IR_REPEAT_MS); poll_is(0x05, 0x00);
    local_edge(false, WEBOS_LOCAL_UP); poll_is(0x0e, 0x00); poll_is(0, 0);
    /* a direction while another is down releases it first */
    at(20000); local_edge(true, WEBOS_LOCAL_LEFT); local_edge(true, WEBOS_LOCAL_RIGHT);
    poll_is(0x04, 0x03); poll_is(0x0e, 0x03); poll_is(0x04, 0x02);
    local_edge(false, WEBOS_LOCAL_RIGHT); poll_is(0x0e, 0x02);
    /* the sender gone: a press not yet long is dropped, a long one released */
    at(30000); local_edge(true, WEBOS_LOCAL_PRESS); ir_lost(); poll_is(0, 0);
    at(40000); local_edge(true, WEBOS_LOCAL_PRESS); at(40000 + LOCAL_LONG_MS); poll_is(0x04, 0xeb);
    ir_lost(); poll_is(0x0e, 0xeb); poll_is(0, 0);
    /* the remote's button and the local key are independent */
    at(50000); ir_edge(true, 0x08); local_edge(true, WEBOS_LOCAL_PRESS);
    poll_is(0x02, 0x08);
    local_edge(false, WEBOS_LOCAL_PRESS); poll_is(0x10, 0xea);
    ir_edge(false, 0x08); poll_is(0x0a, 0x08); poll_is(0, 0);
    puts("local key tap, hold, joystick repeat, release on loss and remote independence passed");
    return 0;
}
"""
    )
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-function",
            "-I" + str(INCLUDE),
            str(runner),
            "-o",
            str(root / "runner"),
        ],
        check=True,
        timeout=20,
    )
    subprocess.run([str(root / "runner")], check=True, timeout=5)


def test_extended_codes(tmp_path):
    """A Japanese remote's extended code (WEBOS_IR_EXT): down, repeats and up as event DB N, no
    check byte; a plain code after it is code ~code again."""
    source = (MODULE / "i2c.c").read_text()
    start = source.index("/* ---- the micom's IR receiver ---- */")
    section = source[start : source.index("/* Direct MStar IR controllers", start)]
    runner = tmp_path / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "webos-ir.h"
typedef uint8_t u8;
typedef uint64_t u64;
#define NSEC_PER_MSEC 1000000ULL
static u64 clock_ns;
static u64 ktime_get_ns(void) { return clock_ns; }
#define DEFINE_SPINLOCK(name) int name
#define spin_lock(l) ((void)(l))
#define spin_unlock(l) ((void)(l))
#define module_param(name, type, mode)
#define MODULE_PARM_DESC(name, text)
#define pr_info(...) ((void)0)
#define pr_info_ratelimited(...) ((void)0)
"""
        + section
        + r"""
static void poll_is(u8 a, u8 b, u8 c)
{
    u8 buf[3];
    ir_poll(buf, sizeof(buf));
    assert(buf[0] == a && buf[1] == b && buf[2] == c);
}
static void at(u64 ms) { clock_ns = ms * NSEC_PER_MSEC; }
int main(void)
{
    /* terrestrial (DB 30): down, a repeat while held, up */
    at(1000); ir_edge(true, WEBOS_IR_EXT(0x30)); poll_is(0x02, 0xdb, 0x30); poll_is(0, 0, 0);
    at(1000 + IR_FIRST_REPEAT_MS); poll_is(0x03, 0xdb, 0x30);
    ir_edge(false, WEBOS_IR_EXT(0x30)); poll_is(0x0a, 0xdb, 0x30); poll_is(0, 0, 0);
    /* a plain code pressed over an extended one releases it; d (0x9F) keeps its check byte */
    at(5000); ir_edge(true, WEBOS_IR_EXT(0x3b)); ir_edge(true, 0x9f);
    poll_is(0x02, 0xdb, 0x3b); poll_is(0x0a, 0xdb, 0x3b); poll_is(0x02, 0x9f, 0x60);
    ir_edge(false, 0x9f); poll_is(0x0a, 0x9f, 0x60);
    /* the sender gone with an extended key down: released */
    ir_edge(true, WEBOS_IR_EXT(0x40)); poll_is(0x02, 0xdb, 0x40);
    ir_lost(); poll_is(0x0a, 0xdb, 0x40); poll_is(0, 0, 0);
    puts("extended codes passed");
    return 0;
}
"""
    )
    subprocess.run(
        ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
         "-I" + str(INCLUDE), str(runner), "-o", str(tmp_path / "runner")],
        check=True,
        timeout=20,
    )  # fmt: skip
    subprocess.run([str(tmp_path / "runner")], check=True, timeout=5)


def test_keys_suspend(tmp_path):
    """Key events left in the queue when the SoC suspends: dropped, as one read after the wake would
    act again; a local key or the remote's POWER among them latches the wake, the remote's other
    keys do not."""
    source = (MODULE / "i2c.c").read_text()
    start = source.index("/* ---- the micom's IR receiver ---- */")
    section = source[start : source.index("/* Direct MStar IR controllers", start)]
    fstart = source.index("/* The SoC suspends (PM_SUSPEND_PREPARE)")
    function = source[fstart : source.index("static ssize_t ir_write", fstart)]
    root = tmp_path
    runner = root / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "webos-ir.h"
typedef uint8_t u8;
typedef uint64_t u64;
#define NSEC_PER_MSEC 1000000ULL
static u64 clock_ns;
static u64 ktime_get_ns(void) { return clock_ns; }
#define DEFINE_SPINLOCK(name) int name
#define spin_lock(l) ((void)(l))
#define spin_unlock(l) ((void)(l))
#define module_param(name, type, mode)
#define MODULE_PARM_DESC(name, text)
#define pr_info(...) ((void)0)
#define pr_info_ratelimited(...) ((void)0)
#define WRITE_ONCE(x, v) ((x) = (v))
static bool wake_latched;
"""
        + section
        + function
        + r"""
static void poll_is(u8 event, u8 code)
{
    u8 buf[5];
    ir_poll(buf, sizeof(buf));
    if (!event) { assert(!buf[0]); return; }
    assert(buf[0] == event && buf[1] == code);
}
int main(void)
{
    /* the remote's other keys: dropped, no wake */
    ir_edge(true, 0x7c); ir_edge(false, 0x7c);
    keys_suspend(); poll_is(0, 0); assert(!wake_latched);
    /* the remote's POWER unread: dropped, and it wakes the SoC */
    ir_edge(true, 0x08); ir_edge(false, 0x08);
    keys_suspend(); poll_is(0, 0); assert(wake_latched);
    wake_latched = false;
    /* POWER read, its release still queued: no second wake */
    ir_edge(true, 0x08); poll_is(0x02, 0x08); ir_edge(false, 0x08);
    keys_suspend(); poll_is(0, 0); assert(!wake_latched);
    /* POWER held: it wakes, and its release later queues nothing */
    ir_edge(true, 0x08); poll_is(0x02, 0x08);
    keys_suspend(); assert(wake_latched); ir_edge(false, 0x08); poll_is(0, 0);
    wake_latched = false;
    /* a local key unread wakes, as before */
    local_edge(true, WEBOS_LOCAL_PRESS); local_edge(false, WEBOS_LOCAL_PRESS);
    keys_suspend(); poll_is(0, 0); assert(wake_latched);
    puts("keys left at the suspend: dropped, POWER and the local key wake");
    return 0;
}
"""
    )
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-function",
            "-I" + str(INCLUDE),
            str(runner),
            "-o",
            str(root / "runner"),
        ],
        check=True,
        timeout=20,
    )
    subprocess.run([str(root / "runner")], check=True, timeout=5)


def test_front_led_latches(tmp_path):
    source = (MODULE / "i2c.c").read_text()
    start = source.index("/* ---- the front LED ----")
    section = source[start : source.index("/* ---- the micom ---- */", start)]
    root = tmp_path
    runner = root / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "webos-ir.h"
typedef uint8_t u8;
#define DEFINE_SPINLOCK(name) int name
#define spin_lock(l) ((void)(l))
#define spin_unlock(l) ((void)(l))
#define pr_info_ratelimited(...) ((void)sizeof(printf(__VA_ARGS__)))
#define READ_ONCE(x) (x)
#define min(a, b) ((a) < (b) ? (a) : (b))
static u8 power_state = 3;
static bool firmware_ready;
static unsigned int changes;
static unsigned int status_changed(void) { return ++changes; }
static u8 standby_status(void) { return 0; }
"""
        + section
        + r"""
int main(void)
{
    struct webos_micom_status st;
    const u8 on[] = {0x6e, 1, 1, 1}, off[] = {0x6e, 1, 0, 1}, pwm[] = {0x69, 4}, wov[] = {0xdf};
    const u8 other[] = {0x25, 2, 0};
    micom_status(&st);
    assert(st.power == 3 && !st.standby && !st.ready);
    for (unsigned int i = 0; i < WEBOS_MICOM_LED_CMDS; i++) assert(st.led[i].cmd && !st.led[i].writes);
    assert(!led_write(other, sizeof(other), "") && !changes);
    assert(led_write(on, sizeof(on), "") && changes == 1);
    assert(led_write(on, sizeof(on), "") && changes == 1);         /* unchanged: no wake */
    assert(led_write(off, sizeof(off), "") && changes == 2);
    assert(led_write(pwm, sizeof(pwm), "") && changes == 3);
    assert(led_write(wov, sizeof(wov), "") && changes == 4);
    assert(led_write(wov, sizeof(wov), "") && changes == 4);
    micom_status(&st);
    assert(st.led[0].cmd == 0x6e && st.led[0].len == 3 && st.led[0].writes == 3);
    assert(st.led[0].data[0] == 1 && st.led[0].data[1] == 0 && st.led[0].data[2] == 1);
    assert(st.led[4].cmd == 0x69 && st.led[4].len == 1 && st.led[4].data[0] == 4 && st.led[4].writes == 1);
    assert(st.led[8].cmd == 0xdf && st.led[8].len == 0 && st.led[8].writes == 2);
    assert(!st.ready);
    firmware_ready = true;
    micom_status(&st);
    assert(st.ready && st.led[0].writes == 3);
    puts("front LED latches, change wakes, ready flag and status passed");
    return 0;
}
"""
    )
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-function",
            "-I" + str(INCLUDE),
            str(runner),
            "-o",
            str(root / "runner"),
        ],
        check=True,
        timeout=20,
    )
    subprocess.run([str(root / "runner")], check=True, timeout=5)
