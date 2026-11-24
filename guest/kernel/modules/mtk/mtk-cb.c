// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* /dev/cb: the callback channel of MediaTek's drivers, as W15L's libmtal.so and kdrv_mtk5882.ko
 * use it (ioctl type '7'). libmtal's CB library (CB_InitLibEx, from MTAL_Init's SC_InitLib) starts
 * a thread that waits in CB_GET for the next event a driver queued for its process (a decoder's,
 * a demux section, a front end's lock) and calls the functions the process registered for the
 * event's id; registering (CB_REG) only records the process. The thread loops again at once on a
 * negative answer, with no sleep and possibly at SCHED_RR, so the wait must block.
 *
 * No driver here raises an event: CB_GET sleeps until the process asks the thread to end
 * (CB_SHUTDOWN: the kdriver queues id 0x73, which the thread ends on) or a signal comes, when it
 * answers -EINTR as the kdriver does. Registrations answer 0. */
#include <linux/compat.h>
#include <linux/module.h>
#include <linux/sched/signal.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/wait.h>

#include "mtk.h"

#define CB_REG         0xc00c3700 /* {id, arg, pid} */
#define CB_GET         0xc00c3701 /* a 0xc000-byte buffer: {id, size, resv, data[size]} */
#define CB_UNREG       0xc00c3702 /* 12 bytes */
#define CB_GET_PID     0xc0083703 /* {id, out pid} */
#define CB_GET_COUNT   0xc0043704 /* u32 */
#define CB_SHUTDOWN    0xc0043705 /* u32: id 0x73 to the waiting thread */
#define CB_REG_MULTI   0xc00c3706
#define CB_UNREG_MULTI 0xc00c3707
#define CB_ID_EXIT     0x73

static DECLARE_WAIT_QUEUE_HEAD(cb_wait);

struct cb_file {
    bool shutdown;
};

static int cb_open(struct inode *inode, struct file *file)
{
    file->private_data = kzalloc(sizeof(struct cb_file), GFP_KERNEL);
    return file->private_data ? 0 : -ENOMEM;
}

static int cb_release(struct inode *inode, struct file *file)
{
    kfree(file->private_data);
    return 0;
}

static long cb_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct cb_file *f = file->private_data;
    u32 __user *u = compat_ptr(arg);

    mtk_count_ioctl("cb", cmd);
    switch (cmd) {
    case CB_GET: {
        static const u32 exit_event[4] = {CB_ID_EXIT, 4, 0, 0};

        if (wait_event_interruptible(cb_wait, READ_ONCE(f->shutdown)))
            return -EINTR;
        WRITE_ONCE(f->shutdown, false);
        return copy_to_user(u, exit_event, sizeof(exit_event)) ? -EFAULT : 0;
    }
    case CB_SHUTDOWN:
        WRITE_ONCE(f->shutdown, true);
        wake_up_interruptible_all(&cb_wait);
        return 0;
    case CB_REG:
    case CB_REG_MULTI:
    case CB_UNREG:
    case CB_UNREG_MULTI:
        return 0;
    case CB_GET_COUNT:
        return put_user(0, u);
    case CB_GET_PID:
        return put_user(0, u + 1);
    }
    return mtk_mark_unhandled("cb", cmd, -EINVAL);
}

const struct file_operations mtk_cb_fops = {
    .owner = THIS_MODULE,
    .open = cb_open,
    .release = cb_release,
    .unlocked_ioctl = cb_ioctl,
    .compat_ioctl = cb_ioctl,
};
