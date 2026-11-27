#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the production RTC packet handler against a controlled kernel API provider."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/lgemu"


def test_packet_source_errors_and_context(tmp_path):
    root = tmp_path
    linux = root / "linux"
    linux.mkdir()
    (linux / "types.h").write_text("#include <stdint.h>\ntypedef uint8_t u8;\n")
    (linux / "interrupt.h").write_text(
        "extern int atomic_context, disabled_irqs;\n"
        "#define in_atomic() atomic_context\n#define irqs_disabled() disabled_irqs\n"
    )
    (linux / "rtc.h").write_text(
        "#include <errno.h>\nstruct rtc_device { int unused; };\n"
        "struct rtc_time { int tm_sec, tm_min, tm_hour, tm_mday, tm_mon, tm_year, "
        "tm_wday, tm_yday, tm_isdst; };\n"
        "struct rtc_device *rtc_class_open(const char *);\n"
        "int rtc_read_time(struct rtc_device *, struct rtc_time *);\n"
        "void rtc_class_close(struct rtc_device *);\n"
    )
    # Exercise the genuine dispatch function too, retaining unrelated reply behavior.
    source = (MODULE / "i2c.c").read_text()
    start = source.index("static int micom_answer(", source.index("static int micom_read("))
    dispatch = source[start : source.index("static DECLARE_BITMAP(micom_logged", start)]
    write_start = source.index("static int micom_write(")
    write_dispatch = source[write_start : source.index("/* ---- the NVM EEPROM ----", write_start)]
    runner = root / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <errno.h>
#include <string.h>
#include <stdio.h>
#include <signal.h>
#include <stdbool.h>
#include <linux/rtc.h>
#include "micom-rtc.h"
int atomic_context, disabled_irqs;
static struct rtc_device device;
static struct rtc_time supplied;
static int unavailable, error, opens, reads, closes;
static int power_state = 3;
static bool firmware_ready;
static unsigned int status_changes;
static unsigned int status_changed(void) { return ++status_changes; }
#define xchg(p, v) ({ __typeof__(*(p)) old_ = *(p); *(p) = (v); old_; })
#define pr_info(...) ((void)sizeof(printf(__VA_ARGS__)))
static struct task_struct { char comm[16]; struct task_struct *group_leader; } task = {"tvpowerd", &task};
static struct task_struct *current = &task;
#define POWER_ON_REMOTE_KEY 0
#define READ_ONCE(x) (x)
static void ir_poll(u8 *buf, unsigned int len) { memset(buf, 0, len); }
static bool mic_present;
static bool lgemu_mic_present(void) { return mic_present; }
#define MIC_SWITCH_PIN 6
static int battery_read(u8 cmd, u8 *buf, unsigned int len) { (void)cmd; (void)buf; (void)len; return -EREMOTEIO; }
struct rtc_device *rtc_class_open(const char *name)
{
    assert(!strcmp(name, "rtc0")); assert(!atomic_context && !disabled_irqs);
    opens++; return unavailable ? NULL : &device;
}
int rtc_read_time(struct rtc_device *rtc, struct rtc_time *tm)
{
    assert(rtc == &device); reads++; *tm = supplied; return error;
}
void rtc_class_close(struct rtc_device *rtc) { assert(rtc == &device); closes++; }
"""
        + dispatch
        + r"""
#define WRITE_ONCE(x, value) ((x) = (value))
#define POWER_STATE_WARM 2
#define POWER_STATE_ON 3
static int wake_latched;
static unsigned long micom_logged[4];
static int test_and_set_bit(unsigned int bit, unsigned long *map)
{
    unsigned int word = bit / (8 * sizeof(*map));
    unsigned long mask = 1UL << (bit % (8 * sizeof(*map)));
    int old = !!(map[word] & mask); map[word] |= mask; return old;
}
static void set_power_state(int state, const char *why) { power_state = state; (void)why; }
static void cancel_timer(void) {}
static void arm_timer(void) {}
static void schedule_power(int signal, const char *why) { (void)signal; (void)why; }
static bool led_write(const u8 *data, unsigned int len, const char *hex)
{
    (void)len; (void)hex; return data[0] == 0x6e;
}
"""
        + write_dispatch
        + r"""
static void reset(void)
{
    opens = reads = closes = unavailable = error = atomic_context = disabled_irqs = 0;
    supplied = (struct rtc_time){ .tm_year = 126, .tm_mon = 9, .tm_mday = 8,
                                 .tm_hour = 9, .tm_min = 47, .tm_sec = 58 };
}
static void expect_failure(int expected, unsigned int len)
{
    u8 data[9], command = 0x80;
    memset(data, 0xa5, sizeof(data));
    assert(micom_answer(&command, 1, data + 1, len) == expected);
    for (unsigned int i = 0; i < sizeof(data); i++) assert(data[i] == 0xa5);
}
int main(void)
{
    u8 command = 0x80, data[9];
    reset(); memset(data, 0xa5, sizeof(data));
    assert(micom_answer(&command, 1, data + 1, 7) == 0);
    const u8 expected[] = {20, 26, 10, 8, 9, 47, 58};
    assert(!memcmp(data + 1, expected, 7) && data[0] == 0xa5 && data[8] == 0xa5);
    assert(opens == 1 && reads == 1 && closes == 1);
    /* A second request reads fresh hardware data, never a cached/system clock. */
    supplied.tm_sec = 59;
    assert(micom_answer(&command, 1, data + 1, 7) == 0 && data[7] == 59);
    assert(opens == 2 && reads == 2 && closes == 2);
    supplied = (struct rtc_time){ .tm_year = 100, .tm_mon = 1, .tm_mday = 29 };
    assert(micom_answer(&command, 1, data + 1, 7) == 0);
    const u8 leap[] = {20, 0, 2, 29, 0, 0, 0};
    assert(!memcmp(data + 1, leap, 7));
    for (unsigned int len = 0; len < 16; len++) if (len != 7) {
        reset(); expect_failure(-EREMOTEIO, len); assert(!opens && !reads && !closes);
    }
    reset(); atomic_context = 1; expect_failure(-EWOULDBLOCK, 7); assert(!opens);
    reset(); disabled_irqs = 1; expect_failure(-EWOULDBLOCK, 7); assert(!opens);
    reset(); unavailable = 1; expect_failure(-ENODEV, 7); assert(opens == 1 && !reads && !closes);
    const int errors[] = {-EIO, -EINVAL, -EINTR};
    for (unsigned int i = 0; i < sizeof(errors) / sizeof(errors[0]); i++) {
        reset(); error = errors[i]; expect_failure(error, 7);
        assert(opens == 1 && reads == 1 && closes == 1);
    }
    reset(); supplied.tm_year = 23700; expect_failure(-ERANGE, 7);
    assert(opens == 1 && reads == 1 && closes == 1);
    reset(); supplied.tm_year = -1; expect_failure(-ERANGE, 7);
    reset(); command = 0xff; assert(micom_answer(&command, 1, data, 1) == -EREMOTEIO);
    command = 0x80; assert(micom_answer(&command, 0, data, 7) == -EREMOTEIO);
    assert(micom_answer(&command, 2, data, 7) == -EREMOTEIO && !opens);
    command = 0x9a; assert(micom_answer(&command, 1, data, 1) == 0 && data[0] == POWER_ON_REMOTE_KEY);
    assert(!firmware_ready && !status_changes);
    command = 0xb1; assert(micom_answer(&command, 1, data, 0) != 0 && !firmware_ready);
    command = 0xb1; assert(micom_answer(&command, 1, data, 1) == 0 && data[0] == 3);
    /* the first power-state read marks the firmware ready, once */
    assert(firmware_ready && status_changes == 1);
    command = 0xb1; assert(micom_answer(&command, 1, data, 1) == 0 && status_changes == 1);
    command = 0xc0; memset(data, 0xa5, 3);
    assert(micom_answer(&command, 1, data, 3) == 0 && !data[0] && !data[1] && !data[2]);
    assert(!opens && !reads && !closes);
    /* wake-on-voice support: one byte for every model, 0 without the microphone */
    command = 0x16; memset(data, 0xa5, 9);
    assert(micom_answer(&command, 1, data, 1) == 0 && data[0] == 0 && data[1] == 0xa5);
    assert(micom_answer(&command, 1, data, 2) == -EREMOTEIO);
    /* GPIO status: no answer without the microphone; with it, pin 6 (its switch) on */
    command = 0x67; memset(data, 0xa5, 9);
    assert(micom_answer(&command, 1, data, 7) == -EREMOTEIO && data[0] == 0xa5);
    mic_present = true;
    command = 0x16; memset(data, 0xa5, 9);
    assert(micom_answer(&command, 1, data, 1) == 0 && data[0] == 1 && data[1] == 0xa5);
    command = 0x67; memset(data, 0xa5, 9);
    assert(micom_answer(&command, 1, data + 1, 7) == 0 && data[0] == 0xa5 && data[8] == 0xa5);
    const u8 gpio[] = {0, 0, 0, 0, 0, 0, 1};
    assert(!memcmp(data + 1, gpio, 7));
    const u8 write[] = {0x24, 20, 26, 10, 8, 9, 47, 58, 0};
    for (unsigned int len = 1; len <= sizeof(write); len++)
        assert(micom_write(write, len) == -EREMOTEIO);
    const u8 led[] = {0x6e, 1, 1, 1};
    assert(micom_write(led, sizeof(led)) == 0);
    const u8 cec[] = {0x47}, other[] = {0x99};
    assert(micom_write(cec, sizeof(cec)) == 0 && micom_write(other, sizeof(other)) == 0);
    assert(micom_write(write, 0) == -EREMOTEIO && !opens && !reads && !closes);
    puts("RTC packet, fresh source, errors, atomic context, unsupported write and unrelated dispatch passed");
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
            "-I" + str(root),
            "-I" + str(MODULE),
            str(MODULE / "micom-rtc.c"),
            str(runner),
            "-o",
            str(root / "runner"),
        ],
        check=True,
        timeout=20,
    )
    subprocess.run([str(root / "runner")], check=True, timeout=5)
