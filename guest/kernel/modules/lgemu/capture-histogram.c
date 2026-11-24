// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* video60's histogram controls (libvtcapture 1.0.0-56 on: vtCapture_initHistogram and the rest),
 * as measured on a ponytail OLED (docs/research/video/capture-histogram.md, "Region map and bin
 * scale"): START, PRESET, DATA and STOP. While any open file has started it, a worker asks the
 * front end for the composed picture (BLENDED) at 160 x 90, one RGBA value per sample point of
 * the set's 12-pixel grid, every 100 ms, and counts each block's points into 256 bins per
 * channel. Nothing runs while no one has started it. No front end, no data: a read waits for
 * the first update after START (at most HIST_FIRST) and without data gets -ENODATA; a failed
 * capture keeps the last data, as a read between two updates does on the set. */
#define pr_fmt(fmt) "capture: " fmt
#include <linux/compat.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/unaligned.h>
#include <linux/wait.h>
#include <linux/workqueue.h>
#include "lgemu.h"
#include "lgemu-drv.h"
#include "../../../../include/webos-capture.h"

#define HIST_COLS   160
#define HIST_ROWS   90
#define HIST_BLOCKS 16
#define HIST_BINS   (3 * 256)
#define HIST_BLOCK  (4 + HIST_BINS * 2) /* u32 index, then R, G and B as LE u16 counts */
/* every 12 frames of the set's usual 120 Hz panel; 50 Hz content (100 Hz, 120 ms) not modelled */
#define HIST_PERIOD msecs_to_jiffies(100)
#define HIST_SWITCH msecs_to_jiffies(200)  /* PRESET to the next update: the set's 232-246 ms */
#define HIST_FIRST  msecs_to_jiffies(1000) /* how long a read waits for the first data */

/* Each block's rectangle in grid cells (12 UI pixels each): x0, x1, y0, y1, end exclusive.
 * Generated from the doc's region table; every block's cell count is its measured total. */
static const u8 regions[4][HIST_BLOCKS][4] = {
    {
        /* preset 0 */
        {39, 119, 0, 90},
        {32, 128, 72, 90},
        {48, 112, 27, 63},
        {16, 64, 0, 18},
        {0, 80, 0, 90},
        {0, 46, 60, 90},
        {0, 32, 21, 68},
        {0, 48, 0, 33},
        {80, 160, 0, 90},
        {112, 160, 60, 90},
        {128, 160, 21, 68},
        {55, 103, 0, 18},
        {96, 144, 0, 18},
        {112, 160, 0, 33},
        {48, 112, 0, 11},
        {0, 20, 27, 63},
    },
    {
        /* preset 1 */
        {39, 119, 11, 78},
        {32, 128, 65, 78},
        {48, 112, 31, 58},
        {16, 64, 11, 24},
        {0, 80, 11, 78},
        {0, 46, 56, 78},
        {0, 32, 27, 62},
        {0, 48, 11, 36},
        {80, 160, 11, 78},
        {112, 160, 56, 78},
        {128, 160, 27, 62},
        {55, 103, 11, 24},
        {96, 144, 11, 24},
        {112, 160, 11, 36},
        {48, 112, 0, 11},
        {0, 20, 27, 63},
    },
    {
        /* preset 2 */
        {49, 109, 0, 90},
        {44, 115, 72, 90},
        {56, 103, 27, 63},
        {32, 67, 0, 18},
        {20, 80, 0, 90},
        {20, 55, 60, 90},
        {20, 44, 21, 68},
        {20, 56, 0, 33},
        {80, 138, 0, 90},
        {103, 138, 60, 90},
        {115, 138, 21, 68},
        {62, 97, 0, 18},
        {91, 127, 0, 18},
        {103, 138, 0, 33},
        {48, 112, 0, 11},
        {0, 20, 27, 63},
    },
    {
        /* preset 3 */
        {80, 160, 9, 81},
        {48, 112, 0, 90},
        {0, 80, 9, 81},
        {52, 106, 27, 60},
        {36, 122, 66, 90},
        {105, 160, 55, 90},
        {118, 160, 24, 65},
        {112, 160, 0, 33},
        {96, 144, 0, 18},
        {55, 103, 0, 18},
        {16, 64, 0, 18},
        {0, 48, 0, 33},
        {0, 41, 24, 65},
        {0, 54, 55, 90},
    },
};
static const u8 block_count[4] = {16, 16, 16, 14};

static void update(struct work_struct *work);

static struct {
    struct mutex lock; /* everything below but rgba and next, which only the worker uses */
    struct delayed_work work;
    wait_queue_head_t wait;
    unsigned int users;
    u32 preset; /* the last PRESET, which the next update starts with; global, kept across STOP */
    u32 layout; /* the layout of data */
    bool valid, tried; /* data; an update has run since the first START */
    u16 data[HIST_BLOCKS][HIST_BINS];
    u16 next[HIST_BLOCKS][HIST_BINS];
    u8 rgba[HIST_COLS * HIST_ROWS * 4];
} hist = {
    .lock = __MUTEX_INITIALIZER(hist.lock),
    .work = __DELAYED_WORK_INITIALIZER(hist.work, update, 0),
    .wait = __WAIT_QUEUE_HEAD_INITIALIZER(hist.wait),
};

static void count(u32 layout)
{
    unsigned int b, x, y;
    memset(hist.next, 0, sizeof(hist.next));
    for (b = 0; b < block_count[layout]; b++) {
        const u8 *r = regions[layout][b];
        u16 *bins = hist.next[b];
        for (y = r[2]; y < r[3]; y++)
            for (x = r[0]; x < r[1]; x++) {
                const u8 *p = hist.rgba + (y * HIST_COLS + x) * 4;
                bins[p[0]]++;
                bins[256 + p[1]]++;
                bins[512 + p[2]]++;
            }
    }
}

static void update(struct work_struct *work)
{
    unsigned long started = jiffies;
    u32 layout;
    int ret;
    mutex_lock(&hist.lock);
    layout = hist.preset;
    mutex_unlock(&hist.lock);
    ret = lgemu_capture_frame(WEBOS_CAPTURE_BLENDED, HIST_COLS, HIST_ROWS, hist.rgba);
    if (!ret)
        count(layout);
    mutex_lock(&hist.lock);
    hist.tried = true;
    if (!ret) {
        memcpy(hist.data, hist.next, sizeof(hist.data));
        hist.layout = layout;
        hist.valid = true;
    }
    if (hist.users) {
        unsigned long spent = jiffies - started;
        queue_delayed_work(system_long_wq, &hist.work,
                           spent < HIST_PERIOD ? HIST_PERIOD - spent : 0);
    }
    mutex_unlock(&hist.lock);
    wake_up_all(&hist.wait);
}

static void start(bool *started)
{
    if (*started)
        return;
    *started = true;
    mutex_lock(&hist.lock);
    if (!hist.users++) {
        hist.valid = hist.tried = false;
        queue_delayed_work(system_long_wq, &hist.work, 0);
    }
    mutex_unlock(&hist.lock);
}

void lgemu_capture_histogram_stop(bool *started)
{
    if (!*started)
        return;
    *started = false;
    mutex_lock(&hist.lock);
    if (!--hist.users)
        cancel_delayed_work(&hist.work); /* a running update sees no users and stops */
    mutex_unlock(&hist.lock);
}

static int read_blocks(void __user *ptr, u32 size)
{
    unsigned int n = size / HIST_BLOCK, i;
    long left;
    u8 *buf;
    int ret = 0;
    if (!n || size % HIST_BLOCK || n > HIST_BLOCKS)
        return -EINVAL;
    /* only the first update after START is waited for */
    left = wait_event_interruptible_timeout(hist.wait, READ_ONCE(hist.tried), HIST_FIRST);
    if (left < 0)
        return left;
    buf = kmalloc(size, GFP_KERNEL);
    if (!buf)
        return -ENOMEM;
    if (copy_from_user(buf, ptr, size)) {
        ret = -EFAULT;
        goto out;
    }
    mutex_lock(&hist.lock);
    if (!hist.valid)
        ret = -ENODATA;
    for (i = 0; i < n && !ret; i++) {
        u8 *block = buf + i * HIST_BLOCK;
        u32 index = get_unaligned_le32(block); /* the caller numbers the blocks */
        unsigned int k;
        if (index >= HIST_BLOCKS) {
            ret = -EINVAL;
            break;
        }
        for (k = 0; k < HIST_BINS; k++)
            put_unaligned_le16(index < block_count[hist.layout] ? hist.data[index][k] : 0,
                               block + 4 + 2 * k);
    }
    mutex_unlock(&hist.lock);
    if (!ret && copy_to_user(ptr, buf, size))
        ret = -EFAULT;
out:
    kfree(buf);
    return ret;
}

int lgemu_capture_histogram(bool *started, bool get, u32 id, u32 size, u32 value)
{
    void __user *ptr = compat_ptr(value);
    u32 preset;
    switch (id) {
    case LGEMU_HIST_START:
    case LGEMU_HIST_STOP:
        if (get)
            return -EOPNOTSUPP;
        if (id == LGEMU_HIST_START)
            start(started);
        else
            lgemu_capture_histogram_stop(started);
        return 0;
    case LGEMU_HIST_PRESET:
        if (get)
            return -EOPNOTSUPP;
        if (!size)
            preset = value; /* a plain control: the value in place of the pointer */
        else if (size != 4 || get_user(preset, (u32 __user *)ptr))
            return size != 4 ? -EINVAL : -EFAULT;
        if (!*started || preset >= ARRAY_SIZE(block_count))
            return -EINVAL;
        mutex_lock(&hist.lock);
        hist.preset = preset;
        /* reads keep the old layout until the update this schedules, as on the set */
        mod_delayed_work(system_long_wq, &hist.work, HIST_SWITCH);
        mutex_unlock(&hist.lock);
        return 0;
    case LGEMU_HIST_DATA:
        if (!get)
            return -EOPNOTSUPP;
        if (!*started)
            return -EINVAL;
        return read_blocks(ptr, size);
    }
    return -EOPNOTSUPP;
}

void lgemu_capture_histogram_exit(void)
{
    cancel_delayed_work_sync(&hist.work);
}
