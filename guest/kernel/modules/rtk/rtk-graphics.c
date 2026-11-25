// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The secondary screen's frame buffer (lgemu's) and the GAL graphics node. */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#include <linux/anon_inodes.h>
#include <linux/bitmap.h>
#include <linux/compat.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/kobject.h>
#include <linux/math64.h>
#include <linux/mm.h>
#include <linux/mman.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/proc_fs.h>
#include <linux/sched.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include <linux/workqueue.h>
#include <linux/unaligned.h>

#include "lgemu-drv.h"
#include "lgemu-i2c.h"
#include "rtk-internal.h"
#include "../../../../include/webos-video.h"

/* The secondary screen's frame buffer, node:widthxheight: the /dev/fb<node> the product's
 * compositor names besides fb0 (W20L's product.env QT_QPA_EGLFS_FB=/dev/fb0:/dev/fb1). Realtek's
 * venusfb (mali_fb.ko, a 4.4 module the VM cannot load) gives the TV fb0, the OSD, and fb1; here it
 * is lgemu's memory frame buffer (lgemu/fb.c). */
static char *fb;
module_param(fb, charp, 0444);
MODULE_PARM_DESC(fb, "the secondary screen's frame buffer, fbnode:widthxheight (32 bpp)");

static bool fb_added;

int rtk_fb_setup(void)
{
    int err = lgemu_fb_add(fb);

    fb_added = !err && fb && *fb;
    return err;
}

void rtk_fb_remove(void)
{
    if (fb_added)
        lgemu_fb_remove();
    fb_added = false;
}

/* ---- the GAL gfx driver: /dev/gfx ---- */

static char *gal_gfx_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return NULL;
}

static const struct class gal_gfx_class = {.name = "gal_gfx", .devnode = gal_gfx_devnode};
static struct lgemu_cdev gal_gfx_cdev;
static bool gal_gfx_made;

void rtk_gal_gfx_remove(void)
{
    if (gal_gfx_made) {
        device_destroy(&gal_gfx_class, gal_gfx_cdev.dev);
        class_unregister(&gal_gfx_class);
    }
    gal_gfx_made = false;
    lgemu_cdev_del(&gal_gfx_cdev);
}

int rtk_gal_gfx_add(void)
{
    struct device *d;
    int err = lgemu_cdev_add(&gal_gfx_cdev, 0, 0, 1, "gal_gfx", &lgemu_gfx_fops);

    if (err)
        return err;
    err = class_register(&gal_gfx_class);
    if (err)
        return err;
    d = device_create(&gal_gfx_class, NULL, gal_gfx_cdev.dev, NULL, "gfx");
    if (IS_ERR(d)) {
        class_unregister(&gal_gfx_class);
        return PTR_ERR(d);
    }
    gal_gfx_made = true;
    return 0;
}
