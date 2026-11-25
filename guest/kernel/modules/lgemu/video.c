// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The video windows of the SoC's scaler, for the host viewer (include/webos-video.h).
 *
 * The viewer draws the decoded media under the OSD at the scaler's windows. On LG115x chips those
 * reach it from the scaler HAL's shim in tvservice
 * (guest/layers/soc/src/libhal_lg115x/hal-video.c); on chips whose stock HAL and KADP run over a
 * driver of ours, the driver sees the windows where the SoC's scaler driver would and passes them
 * here: mstar-utopia.ko's XC (MApi_XC_SetWindow, the black-video mute). lgemu_video_window() is
 * their entry point.
 *
 * The table goes to the host on the webos.video virtio-serial port, which a kernel cannot write
 * (the port's file has no write_iter), so the initramfs's pump (guest/initramfs/src/virtio-video.c)
 * copies it from /dev/lgemu-video: each read() returns one whole frame (struct
 * webos_video_packet), the table as each change left it, oldest first, waiting for the next;
 * while a window is open, a read that waits a second returns the table again (WEBOS_VIDEO_SYNC),
 * so a viewer that connects late is in step within a second. Until a driver passes a window
 * nothing is readable, and the pump never opens the port (where the HAL shim writes it). */
#define pr_fmt(fmt) KBUILD_MODNAME ": video: " fmt
#include <linux/fs.h>
#include <linux/jiffies.h>
#include <linux/miscdevice.h>
#include <linux/poll.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/sched.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include "lgemu.h"
#include "lgemu-drv.h"
#include "../../../../include/webos-video.h"

#define QUEUE   16
#define WINDOWS 2

static DEFINE_MUTEX(lock);
static DECLARE_WAIT_QUEUE_HEAD(readers);
static struct lgemu_video_window windows[WINDOWS];
static u32 screen_w = 1920, screen_h = 1080;
/* frames: sequence s is queue[s % QUEUE], for the last QUEUE sequences up to newest */
static struct webos_video_packet *queue;
static u32 newest; /* 0: none yet */

static const char *const names[WINDOWS] = {"MAIN", "SUB"};

/* the table now, as the frame of sequence seq (lock held) */
static void make(struct webos_video_packet *p, u32 seq, u32 event, unsigned subject)
{
    memset(p, 0, sizeof(*p));
    memcpy(p->magic, WEBOS_VIDEO_MAGIC, sizeof(WEBOS_VIDEO_MAGIC));
    p->bytes = sizeof(*p);
    p->sequence = seq;
    p->event = event;
    p->subject = WEBOS_VIDEO_MAX;
    p->screen_w = screen_w;
    p->screen_h = screen_h;
    for (unsigned i = 0; i < WINDOWS; i++) {
        const struct lgemu_video_window *w = &windows[i];
        struct webos_video_context *c = &p->contexts[p->count];

        if (!w->open)
            continue;
        if (i == subject)
            p->subject = p->count;
        strscpy(c->context, names[i], sizeof(c->context));
        c->registered = 1;
        c->connected = w->connected;
        c->av_mute = w->mute;
        c->has_rect = w->has_rect;
        c->x = w->x;
        c->y = w->y;
        c->w = w->w;
        c->h = w->h;
        c->width = w->width;
        c->height = w->height;
        c->input = w->input;
        c->input_port = w->input_port;
        c->crop_x = w->crop_x;
        c->crop_y = w->crop_y;
        c->crop_w = w->crop_w;
        c->crop_h = w->crop_h;
        p->count++;
    }
}

void lgemu_video_screen(u32 w, u32 h)
{
    mutex_lock(&lock);
    screen_w = w;
    screen_h = h;
    mutex_unlock(&lock);
}
EXPORT_SYMBOL_GPL(lgemu_video_screen);

void lgemu_video_capture_geometry(struct lgemu_video_window *main, u32 *width, u32 *height)
{
    mutex_lock(&lock);
    *main = windows[0];
    *width = screen_w;
    *height = screen_h;
    mutex_unlock(&lock);
}
EXPORT_SYMBOL_GPL(lgemu_video_capture_geometry);

void lgemu_video_window(unsigned int idx, const struct lgemu_video_window *w, u32 event)
{
    if (idx >= WINDOWS || !queue)
        return;
    mutex_lock(&lock);
    if (!memcmp(&windows[idx], w, sizeof(*w))) {
        mutex_unlock(&lock);
        return;
    }
    windows[idx] = *w;
    newest++;
    make(&queue[newest % QUEUE], newest, event, idx);
    pr_debug("%s %s%s%s %d,%d %ux%u crop %u,%u %ux%u of %ux%u input %u:%u event %u\n", names[idx],
             w->open ? "open" : "closed", w->connected ? " connected" : "", w->mute ? " muted" : "",
             w->x, w->y, w->w, w->h, w->crop_x, w->crop_y, w->crop_w, w->crop_h, w->width,
             w->height, w->input, w->input_port, event);
    mutex_unlock(&lock);
    wake_up_interruptible(&readers);
}
EXPORT_SYMBOL_GPL(lgemu_video_window);

/* each open file: the last sequence it read */
static int video_open(struct inode *inode, struct file *file)
{
    u32 *seen = kzalloc(sizeof(*seen), GFP_KERNEL);

    if (!seen)
        return -ENOMEM;
    file->private_data = seen;
    return stream_open(inode, file);
}

static int video_release(struct inode *inode, struct file *file)
{
    kfree(file->private_data);
    return 0;
}

static bool pending(u32 seen)
{
    u32 n = READ_ONCE(newest);

    return n && n != seen;
}

static ssize_t video_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    u32 *seen = file->private_data;
    struct webos_video_packet *p;
    ssize_t ret = sizeof(*p);
    long left;

    if (n < sizeof(*p))
        return -EINVAL;
    if (file->f_flags & O_NONBLOCK) {
        if (!pending(*seen))
            return -EAGAIN;
    } else {
        left = wait_event_interruptible_timeout(readers, pending(*seen), HZ);
        if (left < 0)
            return left;
    }
    p = kmalloc(sizeof(*p), GFP_KERNEL);
    if (!p)
        return -ENOMEM;
    mutex_lock(&lock);
    if (pending(*seen)) {
        /* the next frame, or the oldest kept for a reader that is new or fell behind */
        u32 oldest = newest > QUEUE ? newest - QUEUE + 1 : 1, next = *seen + 1;

        if (next < oldest || next > newest)
            next = oldest;
        *p = queue[next % QUEUE];
        *seen = next;
    } else if (windows[0].open || windows[1].open) {
        make(p, newest, WEBOS_VIDEO_SYNC, WEBOS_VIDEO_MAX); /* a second without a change */
    } else {
        ret = 0;
    }
    mutex_unlock(&lock);
    if (ret && copy_to_user(buf, p, sizeof(*p)))
        ret = -EFAULT;
    kfree(p);
    if (!ret)
        return -EAGAIN; /* nothing yet: the pump reads again */
    return ret;
}

static __poll_t video_poll(struct file *file, poll_table *wait)
{
    poll_wait(file, &readers, wait);
    return pending(*(u32 *)file->private_data) ? EPOLLIN | EPOLLRDNORM : 0;
}

static const struct file_operations video_fops = {
    .owner = THIS_MODULE,
    .open = video_open,
    .release = video_release,
    .read = video_read,
    .poll = video_poll,
};

static struct miscdevice video_device = {
    .minor = MISC_DYNAMIC_MINOR,
    .name = "lgemu-video",
    .fops = &video_fops,
    .mode = 0400,
};

int lgemu_video_init(void)
{
    queue = kcalloc(QUEUE, sizeof(*queue), GFP_KERNEL);
    if (!queue)
        return -ENOMEM;
    return misc_register(&video_device);
}

void lgemu_video_exit(void)
{
    misc_deregister(&video_device);
    kfree(queue);
    queue = NULL;
}
