// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The OSD's frame buffer for NetCast 4 (parameter gop): the emulator's GOP contract
 * (include/mstar-gop.h) on /dev/lg/lx-gop, as mstar-utopia.ko's GOP node gives SimpleSmart and
 * NetCast 4.5. The netcast4 layer's libgal (guest/layers/simplesmart/src/libhalgal, GAL_GX build)
 * draws in software into the pool and says which surface each OSD layer shows; the initramfs's
 * presenter (guest/initramfs/src/gop-present.c) composes the planes. Nothing is drawn here: no
 * model of LG's 2D engine (docs/dev/architecture/netcast4.md, "Graphics").
 *
 * The pool is lgemu.ko's graphics memory (lgemu_gfx_fops: one shmem object), the same pages the gfx
 * node (/dev/lg/gfx0) maps from 0. It reports the physical address pool_pa; a mapping at an offset
 * in [pool_pa, pool_pa + size) is one at that PA, as mstar-utopia's node accepts (procmgr maps the
 * capability's physicalAddrOfBase). Init binds the node over the firmware's /dev/fb0 and its
 * /dev/gfx: LG's GX clients (libafopenapi's HOA_GX_GetDeviceCapability) map the pool from /dev/gfx
 * and find their windows' surfaces in it (the EGL's render targets). */
#define pr_fmt(fmt) KBUILD_MODNAME ": gop: " fmt
#include <linux/compat.h>
#include <linux/fs.h>
#include <linux/list.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include "lgemu-drv.h"
#include "lg115x.h"
#include "../../../../include/mstar-gop.h"

static bool gop;
module_param(gop, bool, 0444);
MODULE_PARM_DESC(gop,
                 "the OSD frame buffer node lx-gop (include/mstar-gop.h) over the graphics memory");
static unsigned int gop_pool_pa = 0x70000000;
module_param(gop_pool_pa, uint, 0444);
MODULE_PARM_DESC(gop_pool_pa, "the physical address the pool reports");

#define POOL_SIZE (192U << 20) /* within LGEMU_GFX_SIZE */
/* The pool's first page is never allocated: LG's GX layer takes a surface's offset in the pool as
 * its handle, and 0 as no surface. */
#define GOP_RESERVED PAGE_SIZE

struct gop_block {
    struct list_head list;
    u32 pa, size;
};

static LIST_HEAD(gop_blocks);
static DEFINE_MUTEX(gop_lock);
static struct mstar_gop_plane gop_planes[MSTAR_GOP_PLANES];
static atomic_t gop_gen = ATOMIC_INIT(1);
static DECLARE_WAIT_QUEUE_HEAD(gop_waitq);
static struct lx_seen gop_seen = LX_SEEN_INIT(gop_seen);

static void gop_bump(void)
{
    atomic_inc(&gop_gen);
    wake_up_interruptible_all(&gop_waitq);
}

static long gop_alloc(struct mstar_gop_alloc *a)
{
    u64 base = (u64)gop_pool_pa + GOP_RESERVED, end = (u64)gop_pool_pa + POOL_SIZE;
    u64 align = a->align ? a->align : 64, at = base, size = ALIGN((u64)a->size, 64);
    struct gop_block *b, *nb;

    if (!a->size || size > end - base || (align & (align - 1)) || align > SZ_16M)
        return -EINVAL;
    nb = kmalloc(sizeof(*nb), GFP_KERNEL);
    if (!nb)
        return -ENOMEM;
    mutex_lock(&gop_lock);
    list_for_each_entry(b, &gop_blocks, list)
    {
        at = ALIGN(at, align);
        if (at + size <= b->pa)
            break;
        at = (u64)b->pa + b->size;
    }
    at = ALIGN(at, align);
    if (at + size > end) {
        mutex_unlock(&gop_lock);
        kfree(nb);
        return -ENOMEM;
    }
    nb->pa = at;
    nb->size = size;
    list_add_tail(&nb->list, &b->list); /* before b, or at the end */
    mutex_unlock(&gop_lock);
    a->pa = at;
    return 0;
}

static long gop_free(u32 pa)
{
    struct gop_block *b;

    mutex_lock(&gop_lock);
    list_for_each_entry(b, &gop_blocks, list)
    {
        if (b->pa == pa) {
            list_del(&b->list);
            mutex_unlock(&gop_lock);
            kfree(b);
            return 0;
        }
    }
    mutex_unlock(&gop_lock);
    return -EINVAL;
}

static long gop_set_plane(const struct mstar_gop_plane *p)
{
    u64 base = gop_pool_pa, end = base + POOL_SIZE;

    if (p->index >= MSTAR_GOP_PLANES)
        return -EINVAL;
    if (p->enabled &&
        (p->pa < base || !p->pitch || !p->height || (u64)p->pa + (u64)p->pitch * p->height > end))
        return -EINVAL;
    mutex_lock(&gop_lock);
    gop_planes[p->index] = *p;
    gop_planes[p->index].gen = atomic_read(&gop_gen) + 1;
    mutex_unlock(&gop_lock);
    gop_bump();
    return 0;
}

static long gop_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *u = (void __user *)arg;
    long ret;

    switch (cmd) {
    case MSTAR_GOP_IOC_GET_POOL: {
        struct mstar_gop_pool p = {.pa = gop_pool_pa, .size = POOL_SIZE};

        ret = copy_to_user(u, &p, sizeof(p)) ? -EFAULT : 0;
        break;
    }
    case MSTAR_GOP_IOC_ALLOC: {
        struct mstar_gop_alloc a;

        if (copy_from_user(&a, u, sizeof(a))) {
            ret = -EFAULT;
            break;
        }
        ret = gop_alloc(&a);
        if (!ret && copy_to_user(u, &a, sizeof(a))) {
            gop_free(a.pa);
            ret = -EFAULT;
        }
        break;
    }
    case MSTAR_GOP_IOC_FREE: {
        u32 pa;

        ret = get_user(pa, (u32 __user *)u) ? -EFAULT : gop_free(pa);
        break;
    }
    case MSTAR_GOP_IOC_SET_PLANE: {
        struct mstar_gop_plane p;

        ret = copy_from_user(&p, u, sizeof(p)) ? -EFAULT : gop_set_plane(&p);
        break;
    }
    case MSTAR_GOP_IOC_GET_PLANE: {
        struct mstar_gop_plane p;
        u32 index;

        if (get_user(index, (u32 __user *)u) || index >= MSTAR_GOP_PLANES) {
            ret = -EINVAL;
            break;
        }
        mutex_lock(&gop_lock);
        p = gop_planes[index];
        mutex_unlock(&gop_lock);
        p.index = index; /* a plane never set has all zeros */
        ret = copy_to_user(u, &p, sizeof(p)) ? -EFAULT : 0;
        break;
    }
    case MSTAR_GOP_IOC_MEM_INFO: {
        struct mstar_gop_mem m = {.total = POOL_SIZE - GOP_RESERVED};
        u32 at = gop_pool_pa + GOP_RESERVED, end = gop_pool_pa + POOL_SIZE;
        struct gop_block *b;

        mutex_lock(&gop_lock);
        list_for_each_entry(b, &gop_blocks, list)
        {
            m.allocated += b->size;
            m.largest_free = max(m.largest_free, b->pa - at);
            at = b->pa + b->size;
        }
        mutex_unlock(&gop_lock);
        m.largest_free = max(m.largest_free, end - at);
        m.free = m.total - m.allocated;
        ret = copy_to_user(u, &m, sizeof(m)) ? -EFAULT : 0;
        break;
    }
    case MSTAR_GOP_IOC_DAMAGE:
        gop_bump();
        return 0;
    case MSTAR_GOP_IOC_WAIT: {
        struct mstar_gop_wait w;
        long left;

        if (copy_from_user(&w, u, sizeof(w)))
            return -EFAULT;
        left = wait_event_interruptible_timeout(gop_waitq, (u32)atomic_read(&gop_gen) != w.gen,
                                                msecs_to_jiffies(min(w.timeout_ms, 1000U)));
        if (left < 0)
            return left;
        w.gen = atomic_read(&gop_gen);
        return copy_to_user(u, &w, sizeof(w)) ? -EFAULT : 0;
    }
    default:
        ret = -ENOTTY; /* fbdev's and LG's own (KADP_FBDEV, which the layer's libgal replaces) */
        break;
    }
    return lx_seen_log(&gop_seen, "gop", cmd, ret);
}

/* the pool from its start: a file offset within its size, or a PA within it */
static int gop_mmap(struct file *file, struct vm_area_struct *vma)
{
    u64 off = (u64)vma->vm_pgoff << PAGE_SHIFT, len = vma->vm_end - vma->vm_start;

    if (off >= gop_pool_pa && off - gop_pool_pa + len <= POOL_SIZE)
        vma->vm_pgoff = (off - gop_pool_pa) >> PAGE_SHIFT;
    else if (off + len > POOL_SIZE)
        return -EINVAL;
    return lgemu_gfx_fops.mmap(file, vma);
}

static const struct file_operations gop_fops = {
    .owner = THIS_MODULE,
    .open = nonseekable_open,
    .mmap = gop_mmap,
    .unlocked_ioctl = gop_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};

static struct lgemu_cdev gop_cdev;
static struct device *gop_dev;

void lx_gop_remove(void)
{
    struct gop_block *b, *n;

    if (!IS_ERR_OR_NULL(gop_dev))
        device_destroy(&lx_class, gop_dev->devt);
    gop_dev = NULL;
    lgemu_cdev_del(&gop_cdev);
    list_for_each_entry_safe(b, n, &gop_blocks, list)
    {
        list_del(&b->list);
        kfree(b);
    }
}

int lx_gop_add(void)
{
    int err;

    if (!gop)
        return 0;
    err = lgemu_cdev_add(&gop_cdev, 0, 0, 1, "lx-gop", &gop_fops);
    if (err)
        return err;
    gop_dev = device_create(&lx_class, NULL, gop_cdev.dev, NULL, "lx-gop");
    if (IS_ERR(gop_dev)) {
        err = PTR_ERR(gop_dev);
        lx_gop_remove();
        return err;
    }
    pr_info("lx-gop: pool %#x, %u MiB of the graphics memory\n", gop_pool_pa, POOL_SIZE >> 20);
    return 0;
}
