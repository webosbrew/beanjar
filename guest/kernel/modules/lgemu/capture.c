// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* A bounded request/reply bridge to the active viewer. No retained image can
 * satisfy a request after timeout/disconnect. The pump owns this device; SoC
 * capture drivers call lgemu_capture_frame for real, fresh RGBA pixels. */
#include <linux/fs.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/poll.h>
#include <linux/sched.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include "lgemu.h"
#include "lgemu-drv.h"
#include "../../../../include/webos-capture.h"

static DEFINE_MUTEX(serial);
static DEFINE_MUTEX(lock);
static DECLARE_WAIT_QUEUE_HEAD(wait);
static bool opened, available, pending, sent, done;
static u32 sequence;
static int result;
static u8 *destination;
static struct webos_capture_header request;

bool lgemu_capture_available(void)
{
    return READ_ONCE(available);
}
EXPORT_SYMBOL_GPL(lgemu_capture_available);

int lgemu_capture_frame(u32 mode, u32 width, u32 height, u8 *rgba)
{
    long left;
    int ret;
    if (!rgba || !width || !height || width > WEBOS_CAPTURE_WIDTH || height > WEBOS_CAPTURE_HEIGHT)
        return -EINVAL;
    if (mode < WEBOS_CAPTURE_VIDEO || mode > WEBOS_CAPTURE_OSD)
        return -EOPNOTSUPP;
    if (mutex_lock_interruptible(&serial))
        return -EINTR;
    mutex_lock(&lock);
    if (!available) {
        mutex_unlock(&lock);
        mutex_unlock(&serial);
        return -ENODEV;
    }
    if (!++sequence)
        ++sequence;
    request = (struct webos_capture_header){.magic = WEBOS_CAPTURE_MAGIC,
                                            .sequence = sequence,
                                            .mode = mode,
                                            .width = width,
                                            .height = height};
    destination = rgba;
    pending = true;
    sent = done = false;
    result = -ETIMEDOUT;
    mutex_unlock(&lock);
    wake_up_interruptible(&wait);
    left = wait_event_interruptible_timeout(wait, READ_ONCE(done), 3 * HZ);
    mutex_lock(&lock);
    ret = left < 0 ? left : done ? result : -ETIMEDOUT;
    destination = NULL;
    pending = sent = done = false;
    mutex_unlock(&lock);
    mutex_unlock(&serial);
    return ret;
}
EXPORT_SYMBOL_GPL(lgemu_capture_frame);

/* Limited-range BT.601, interleaved CbCr. Chroma is averaged over two horizontal pixels (4:2:2)
 * or a 2x2 block (4:2:0). */
void lgemu_capture_to_yc(const u8 *rgba, u32 width, u32 height, u8 *y_plane, u8 *uv_plane,
                         u32 stride, bool yc420)
{
    unsigned int x, y, dy = yc420 ? 2 : 1;
    for (y = 0; y < height; y++) {
        const u8 *p = rgba + y * width * 4;
        u8 *uv = uv_plane + y / dy * stride;
        for (x = 0; x < width; x++, p += 4)
            y_plane[y * stride + x] = ((66 * p[0] + 129 * p[1] + 25 * p[2] + 128) >> 8) + 16;
        if (!(y % dy)) {
            for (x = 0; x + 1 < width; x += 2) {
                unsigned int row, col;
                int r = 0, g = 0, blue = 0;
                for (row = 0; row < dy; row++) {
                    p = rgba + ((y + row) * width + x) * 4;
                    for (col = 0; col < 2; col++, p += 4) {
                        r += p[0];
                        g += p[1];
                        blue += p[2];
                    }
                }
                r /= 2 * dy;
                g /= 2 * dy;
                blue /= 2 * dy;
                *uv++ = ((-38 * r - 74 * g + 112 * blue + 128) >> 8) + 128;
                *uv++ = ((112 * r - 94 * g - 18 * blue + 128) >> 8) + 128;
            }
        }
        cond_resched();
    }
}
EXPORT_SYMBOL_GPL(lgemu_capture_to_yc);

static int capture_open(struct inode *inode, struct file *file)
{
    int ret = 0;
    mutex_lock(&lock);
    if (opened)
        ret = -EBUSY;
    else
        opened = true;
    mutex_unlock(&lock);
    return ret;
}

static int capture_release(struct inode *inode, struct file *file)
{
    mutex_lock(&lock);
    opened = available = false;
    if (pending) {
        result = -ENODEV;
        done = true;
    }
    mutex_unlock(&lock);
    wake_up_interruptible(&wait);
    return 0;
}

static ssize_t capture_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    ssize_t ret = -EAGAIN;
    if (n < sizeof(request))
        return -EINVAL;
    mutex_lock(&lock);
    if (pending && !sent && !done) {
        ret = copy_to_user(buf, &request, sizeof(request)) ? -EFAULT : sizeof(request);
        if (ret > 0)
            sent = true;
    }
    mutex_unlock(&lock);
    return ret;
}

static ssize_t capture_write(struct file *file, const char __user *buf, size_t n, loff_t *pos)
{
    struct webos_capture_header h;
    ssize_t ret = n;
    if (n < sizeof(h) || n > sizeof(h) + WEBOS_CAPTURE_MAX)
        return -EINVAL;
    if (copy_from_user(&h, buf, sizeof(h)))
        return -EFAULT;
    if (h.magic != WEBOS_CAPTURE_MAGIC || h.reserved || h.status > 0 || h.status < -4095 ||
        h.size != n - sizeof(h))
        return -EINVAL;
    mutex_lock(&lock);
    if (!h.sequence) {
        if (h.size || h.mode || h.width || h.height || (h.status && h.status != -ENODEV)) {
            ret = -EINVAL;
        } else {
            available = !h.status;
            if (!available && pending) {
                result = -ENODEV;
                done = true;
            }
        }
    } else if (!pending || !sent || done || h.sequence != request.sequence) {
        ret = -ESTALE;
    } else if (h.mode != request.mode || h.width != request.width || h.height != request.height ||
               h.size != (h.status ? 0 : h.width * h.height * 4)) {
        ret = -EINVAL;
    } else if (h.size && copy_from_user(destination, buf + sizeof(h), h.size)) {
        ret = -EFAULT;
    } else {
        result = h.status;
        done = true;
    }
    mutex_unlock(&lock);
    wake_up_interruptible(&wait);
    return ret;
}

static __poll_t capture_poll(struct file *file, poll_table *table)
{
    poll_wait(file, &wait, table);
    return READ_ONCE(pending) && !READ_ONCE(sent) && !READ_ONCE(done) ? EPOLLIN : 0;
}
static const struct file_operations fops = {
    .owner = THIS_MODULE,
    .open = capture_open,
    .release = capture_release,
    .read = capture_read,
    .write = capture_write,
    .poll = capture_poll,
};
static struct miscdevice device = {
    .minor = MISC_DYNAMIC_MINOR, .name = "lgemu-capture", .fops = &fops, .mode = 0600};
int lgemu_capture_init(void)
{
    return misc_register(&device);
}
void lgemu_capture_exit(void)
{
    lgemu_capture_histogram_exit();
    misc_deregister(&device);
}
