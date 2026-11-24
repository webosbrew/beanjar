#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile video60's histogram controls (lgemu capture-histogram.c) against a controlled kernel
API: the regions match the measured table, the counts come from the captured picture, a preset
takes effect from the scheduled update, and a read before any data or before START fails."""

from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "guest/kernel/modules/lgemu"
DOC = ROOT / "docs/research/video/capture-histogram.md"

FIXTURE = r"""
#ifndef FIXTURE_H
#define FIXTURE_H
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32; typedef uint64_t u64;
#define __user
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define READ_ONCE(x) (x)
#define IS_ENABLED(x) 0
#define GFP_KERNEL 0
#define msecs_to_jiffies(m) ((unsigned long)(m))
extern unsigned long jiffies;
struct mutex {int held;};
#define __MUTEX_INITIALIZER(m) {0}
static inline void mutex_lock(struct mutex *m) {assert(!m->held); m->held = 1;}
static inline void mutex_unlock(struct mutex *m) {assert(m->held); m->held = 0;}
struct work_struct {int unused;};
struct delayed_work {struct work_struct work; void (*fn)(struct work_struct *); int pending; unsigned long delay;};
#define __DELAYED_WORK_INITIALIZER(w, f, flags) {.fn = (f)}
extern void *system_long_wq;
static inline bool queue_delayed_work(void *wq, struct delayed_work *w, unsigned long d)
{if (w->pending) return false; w->pending = 1; w->delay = d; return true;}
static inline bool mod_delayed_work(void *wq, struct delayed_work *w, unsigned long d)
{w->pending = 1; w->delay = d; return true;}
static inline bool cancel_delayed_work(struct delayed_work *w) {int p = w->pending; w->pending = 0; return p;}
static inline bool cancel_delayed_work_sync(struct delayed_work *w) {return cancel_delayed_work(w);}
typedef struct {int unused;} wait_queue_head_t;
#define __WAIT_QUEUE_HEAD_INITIALIZER(w) {0}
static inline void wake_up_all(wait_queue_head_t *w) {}
#define wait_event_interruptible_timeout(w, cond, t) ((cond) ? 1L : 0L)
#define kmalloc(n, f) malloc(n)
#define kfree free
#define compat_ptr(v) ((void *)(uintptr_t)(v))
static inline unsigned long copy_from_user(void *d, const void *s, unsigned long n) {memcpy(d, s, n); return 0;}
static inline unsigned long copy_to_user(void *d, const void *s, unsigned long n) {memcpy(d, s, n); return 0;}
#define get_user(x, p) ((x) = *(p), 0)
static inline u32 get_unaligned_le32(const void *p) {u32 v; memcpy(&v, p, 4); return v;}
static inline void put_unaligned_le16(u16 v, void *p) {memcpy(p, &v, 2);}
int lgemu_capture_frame(u32 mode, u32 width, u32 height, u8 *rgba);
#endif
"""

RUNNER = r"""
#include "fixture.h"
#include "capture-histogram.c"
#include <stdio.h>
unsigned long jiffies;
void *system_long_wq;
static int frames, fail, shade;
static const unsigned expect[4][16] = {%(totals)s};
int lgemu_capture_frame(u32 mode, u32 width, u32 height, u8 *rgba)
{
    assert(mode == WEBOS_CAPTURE_BLENDED && width == 160 && height == 90);
    frames++;
    if (fail)
        return -ENODATA;
    /* left half (x < 80) red at shade, right half (0, 10, 200) */
    for (u32 i = 0; i < width * height; i++) {
        u8 *p = rgba + i * 4;
        int left = i %% width < 80;
        p[0] = left ? shade : 0; p[1] = left ? 0 : 10; p[2] = left ? 0 : 200; p[3] = 255;
    }
    return 0;
}
static void run(void)
{
    assert(hist.work.pending);
    hist.work.pending = 0;
    hist.work.fn(&hist.work.work);
}
static u8 buf[16 * HIST_BLOCK];
static int read_n(bool *started, unsigned n)
{
    for (unsigned b = 0; b < n && b < 16; b++)
        memcpy(buf + b * HIST_BLOCK, &b, 4);
    return lgemu_capture_histogram(started, true, LGEMU_HIST_DATA, n * HIST_BLOCK, (u32)(uintptr_t)buf);
}
static u16 bin(unsigned b, unsigned k) {u16 v; memcpy(&v, buf + b * HIST_BLOCK + 4 + 2 * k, 2); return v;}
static unsigned total(unsigned b, unsigned ch) {unsigned t = 0; for (int k = 0; k < 256; k++) t += bin(b, ch * 256 + k); return t;}
int main(void)
{
    bool a = false, b = false;
    /* before START: no data, no preset */
    assert(read_n(&a, 16) == -EINVAL);
    assert(lgemu_capture_histogram(&a, false, LGEMU_HIST_PRESET, 0, 1) == -EINVAL);
    assert(lgemu_capture_histogram(&a, true, LGEMU_HIST_START, 0, 0) == -EOPNOTSUPP);
    assert(!lgemu_capture_histogram(&a, false, LGEMU_HIST_START, 0, 0));
    assert(hist.users == 1 && hist.work.pending && hist.work.delay == 0);
    assert(read_n(&a, 16) == -ENODATA); /* no frame yet */
    fail = 1; run(); assert(read_n(&a, 16) == -ENODATA); fail = 0;
    assert(hist.work.pending && hist.work.delay == 100); /* the next update, 100 ms on */
    shade = 255;
    for (u32 p = 0; p < 4; p++) {
        /* a preset in place of the pointer (size 0), as libvtcapture sends it */
        assert(!lgemu_capture_histogram(&a, false, LGEMU_HIST_PRESET, 0, p));
        assert(hist.work.delay == 200);
        if (p) { /* the old layout until the scheduled update */
            assert(!read_n(&a, 16));
            assert(total(0, 0) == expect[p - 1][0]);
        }
        run();
        assert(!read_n(&a, 16));
        for (unsigned k = 0; k < 16; k++) {
            unsigned t = expect[p][k];
            assert(total(k, 0) == t && total(k, 1) == t && total(k, 2) == t);
            /* every sample is either the left red or the right blue */
            assert(bin(k, 255) + bin(k, 0) == t && bin(k, 256 + 10) + bin(k, 256) == t);
            assert(bin(k, 512 + 200) == bin(k, 256 + 10) && bin(k, 255) + bin(k, 512 + 200) == t);
        }
    }
    /* preset 0 block 4 is the left half, block 8 the right half */
    assert(!lgemu_capture_histogram(&a, false, LGEMU_HIST_PRESET, 0, 0)); run();
    assert(!read_n(&a, 16));
    assert(bin(4, 255) == 7200 && bin(8, 512 + 200) == 7200 && bin(0, 255) == 41 * 90);
    /* a failed capture keeps the last data */
    fail = 1; run(); fail = 0;
    assert(!read_n(&a, 16) && bin(4, 255) == 7200);
    /* the caller's block numbers choose the blocks; a bad one is refused; sizes are whole blocks */
    u32 idx = 8; memcpy(buf, &idx, 4);
    assert(!lgemu_capture_histogram(&a, true, LGEMU_HIST_DATA, HIST_BLOCK, (u32)(uintptr_t)buf));
    assert(bin(0, 512 + 200) == 7200);
    idx = 16; memcpy(buf, &idx, 4);
    assert(lgemu_capture_histogram(&a, true, LGEMU_HIST_DATA, HIST_BLOCK, (u32)(uintptr_t)buf) == -EINVAL);
    assert(read_n(&a, 0) == -EINVAL && read_n(&a, 17) == -EINVAL);
    assert(lgemu_capture_histogram(&a, true, LGEMU_HIST_DATA, 100, (u32)(uintptr_t)buf) == -EINVAL);
    assert(lgemu_capture_histogram(&a, false, LGEMU_HIST_PRESET, 0, 4) == -EINVAL);
    /* a second client shares the worker; the last STOP (or close) ends it; the preset stays */
    assert(!lgemu_capture_histogram(&b, false, LGEMU_HIST_START, 0, 0) && hist.users == 2);
    assert(!lgemu_capture_histogram(&a, false, LGEMU_HIST_STOP, 0, 0) && hist.users == 1 && hist.work.pending);
    assert(!lgemu_capture_histogram(&a, false, LGEMU_HIST_STOP, 0, 0) && hist.users == 1);
    assert(!lgemu_capture_histogram(&b, false, LGEMU_HIST_PRESET, 0, 3));
    lgemu_capture_histogram_stop(&b);
    assert(!hist.users && !hist.work.pending && hist.preset == 3);
    frames = 0; assert(!lgemu_capture_histogram(&a, false, LGEMU_HIST_START, 0, 0));
    assert(read_n(&a, 14) == -ENODATA); /* old data is not served to a new start */
    run(); assert(frames == 1 && !read_n(&a, 14) && total(13, 0) == expect[3][13]);
    lgemu_capture_histogram_stop(&a);
    hist.work.fn(&hist.work.work); /* one already running when the last client left */
    assert(!hist.work.pending);
    lgemu_capture_histogram_exit();
    puts("ok");
    return 0;
}
"""


def measured_totals():
    """Each block's sample count from the doc's table of totals (measured on the set)."""
    doc = DOC.read_text()
    out = []
    for p in range(4):
        m = re.search(rf"preset {p}: ([\d ]+)[;|]", doc.split("| Totals per block |")[1])
        out.append([int(v) for v in m.group(1).split()])
    return out


def test_histogram_controls(tmp_path):
    deep = tmp_path / "a/b/c/d"
    deep.mkdir(parents=True)
    (tmp_path / "include").mkdir()
    shutil.copy(ROOT / "include/webos-capture.h", tmp_path / "include")
    shutil.copy(MODULE / "capture-histogram.c", deep)
    shutil.copy(MODULE / "lgemu.h", deep)
    (deep / "lgemu-drv.h").write_text("")
    linux = tmp_path / "linux"
    linux.mkdir()
    for name in ("compat", "mutex", "slab", "uaccess", "unaligned", "wait", "workqueue", "types"):
        (linux / f"{name}.h").write_text('#include "fixture.h"\n')
    (tmp_path / "fixture.h").write_text(FIXTURE)
    totals = measured_totals()
    assert [len(t) for t in totals] == [16, 16, 16, 14]
    table = ",".join("{" + ",".join(map(str, t)) + "}" for t in totals)
    (deep / "runner.c").write_text(RUNNER % {"totals": table})
    exe = tmp_path / "runner"
    subprocess.run(
        [
            "cc",
            "-std=gnu11",
            "-Wall",
            "-Werror",
            "-Wno-unused-function",
            "-fsanitize=address,undefined",
            "-no-pie",  # u32 user pointers, as from ARM32
            "-I",
            str(tmp_path),
            "-I",
            str(deep),
            "-o",
            str(exe),
            str(deep / "runner.c"),
        ],
        check=True,
    )
    assert subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout == "ok\n"
