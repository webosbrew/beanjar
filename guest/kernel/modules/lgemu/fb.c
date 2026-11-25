// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The secondary screen's frame buffer: a memory frame buffer the product's compositor opens as its
 * second display besides fb0. From webOS 5 on, product.env of several 8K and Realtek sets gives
 * the Starfish QPA two screens (WEBOS_COMPOSITOR_DISPLAYS=2, QT_QPA_EGLFS_FB=/dev/fb0:/dev/fb1)
 * and an extra 512x2160 window on fb1; without /dev/fb1 the QPA's second screen has no native
 * window and the compositor never draws a frame (W20L's, W20K's).
 *
 * On a set it is the vendor's: Realtek's venusfb (mali_fb.ko) gives fb0, the OSD, and fb1. Here
 * fb0 is virtio-gpu's (the DRM fbdev emulation, registered at its probe, before the modules), and
 * this is fb1: memory, 32 bpp ARGB, which the compositor may draw into and nothing shows. Its
 * private ioctls, venusfb's FBEXTIO_PUT_DISPLAYPOSITION/VISIBLE/REGION (_IOC(0, 'n', 1..3, 4): the
 * starfish QPA's 0x46e01..3), are accepted. Registered only when it gets the node asked for.
 *
 * The board turns it on with lgemu's `fb` (guest/targets/soc/<chip>.toml), or a SoC module with its
 * own (rtk.ko's `fb`), through lgemu_fb_add(): "fbnode:widthxheight". One at a time. */
#define pr_fmt(fmt) KBUILD_MODNAME ": fb: " fmt
#include <linux/fb.h>
#include <linux/kernel.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/mutex.h>
#include <linux/vmalloc.h>
#include "lgemu.h"
#include "lgemu-drv.h"

static char *fb;
module_param(fb, charp, 0444);
MODULE_PARM_DESC(fb, "the secondary screen's frame buffer, fbnode:widthxheight (32 bpp)");

static DEFINE_MUTEX(fb_lock);
static struct fb_info *fb_info;
static void *fb_mem;

static int fb_mmap_mem(struct fb_info *info, struct vm_area_struct *vma)
{
    return remap_vmalloc_range(vma, info->screen_buffer, vma->vm_pgoff);
}

static int fb_ext_ioctl(struct fb_info *info, unsigned int cmd, unsigned long arg)
{
    if ((cmd & ~0x3u) == 0x46e00 && (cmd & 0x3))
        return 0;
    return -ENOTTY;
}

static int fb_check(struct fb_var_screeninfo *var, struct fb_info *info)
{
    *var = info->var;
    return 0;
}

static const struct fb_ops fb_mem_ops = {
    .owner = THIS_MODULE,
    __FB_DEFAULT_SYSMEM_OPS_RDWR,
    __FB_DEFAULT_SYSMEM_OPS_DRAW,
    .fb_check_var = fb_check,
    .fb_ioctl = fb_ext_ioctl,
    .fb_mmap = fb_mmap_mem,
};

static void fb_free(void)
{
    if (fb_info)
        framebuffer_release(fb_info);
    fb_info = NULL;
    vfree(fb_mem);
    fb_mem = NULL;
}

int lgemu_fb_add(const char *spec)
{
    unsigned int node, w, h;
    size_t size;
    int err = 0;

    if (!spec || !*spec)
        return 0;
    if (sscanf(spec, "%u:%ux%u", &node, &w, &h) != 3 || !w || !h || w > 8192 || h > 8192) {
        pr_err("%s: fbnode:widthxheight\n", spec);
        return -EINVAL;
    }
    mutex_lock(&fb_lock);
    if (fb_info || fb_mem) {
        pr_err("%s: a secondary frame buffer is already there\n", spec);
        err = -EBUSY;
        goto out;
    }
    size = PAGE_ALIGN((size_t)w * h * 4);
    fb_mem = vmalloc_user(size);
    fb_info = framebuffer_alloc(0, NULL);
    if (!fb_mem || !fb_info) {
        fb_free();
        err = -ENOMEM;
        goto out;
    }
    fb_info->fbops = &fb_mem_ops;
    fb_info->flags = FBINFO_VIRTFB;
    fb_info->screen_buffer = fb_mem;
    fb_info->screen_size = size;
    snprintf(fb_info->fix.id, sizeof(fb_info->fix.id), "lgemu fb%u", node);
    fb_info->fix.type = FB_TYPE_PACKED_PIXELS;
    fb_info->fix.visual = FB_VISUAL_TRUECOLOR;
    fb_info->fix.line_length = w * 4;
    fb_info->fix.smem_len = size;
    fb_info->var.xres = fb_info->var.xres_virtual = w;
    fb_info->var.yres = fb_info->var.yres_virtual = h;
    fb_info->var.bits_per_pixel = 32;
    fb_info->var.red = (struct fb_bitfield){16, 8, 0};
    fb_info->var.green = (struct fb_bitfield){8, 8, 0};
    fb_info->var.blue = (struct fb_bitfield){0, 8, 0};
    fb_info->var.transp = (struct fb_bitfield){24, 8, 0};
    fb_info->var.activate = FB_ACTIVATE_NOW;
    err = register_framebuffer(fb_info);
    if (err) {
        fb_free();
        goto out;
    }
    if (fb_info->node != node) {
        pr_info("got /dev/fb%d, not fb%u (no frame buffer before it?): not kept\n", fb_info->node,
                node);
        unregister_framebuffer(fb_info);
        fb_free();
        goto out;
    }
    pr_info("fb%u: %ux%u, 32 bpp, shown nowhere\n", node, w, h);
out:
    mutex_unlock(&fb_lock);
    return err;
}
EXPORT_SYMBOL_GPL(lgemu_fb_add);

void lgemu_fb_remove(void)
{
    mutex_lock(&fb_lock);
    if (fb_info)
        unregister_framebuffer(fb_info);
    fb_free();
    mutex_unlock(&fb_lock);
}
EXPORT_SYMBOL_GPL(lgemu_fb_remove);

int lgemu_fb_init(void)
{
    return lgemu_fb_add(fb);
}

void lgemu_fb_exit(void)
{
    lgemu_fb_remove();
}
