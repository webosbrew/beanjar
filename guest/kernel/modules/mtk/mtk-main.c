// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* mtk.ko: MediaTek's kernel drivers for the MTAL userland of W15L (MT5882, LG's mtka5lr), whose
 * kdrv_mtk5882.ko is a 32-bit ARM module this kernel cannot load. libmtal.so (also
 * libkadaptor.so.0, under LG's libhal_mtka5lr) does every MT* call as one ioctl on /dev/mtal
 * (mtk-mtal.c), waits for the drivers' callbacks on /dev/cb (mtk-cb.c), and maps the frame buffer
 * memory (FBM) pools through /dev/fbm_* and the kernel memory window /dev/kmem2. The firmware's
 * etc/device_table names the nodes (/dev/mtal 241:0, /dev/cli 241:4, /dev/cb 241:8, /dev/kmem2
 * 244:0, /dev/b2r 243:0, /dev/feeder 247:0, /dev/tsrec 245:0, /dev/vomx 238:0, /dev/aomx 237:0,
 * /dev/rmmgr 229:0, /dev/jpg and /dev/fbm_* 251:n); the others libmtal opens (/dev/fbm_mpeg,
 * fbm_dmx, ...) are the kdriver's own. Here every node is a device of one dynamic major that
 * devtmpfs names, as rtk.ko's are: mknod.conf's makedevs then finds each one there and leaves it.
 *
 * /dev/gfx is the soc layer's graphics memory (lgemu.ko's gfx.c), with its own mappings.
 *
 * Every other node's mapping is of zeroed memory that stays while the module is loaded, per node
 * and offset (lgemu.ko's page store): the userland reads back what it wrote. An ioctl a node does
 * not model answers 0 with its argument untouched, and is listed in /proc/mtk/ioctl. */
#include <linux/compat.h>
#include <linux/device.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/uaccess.h>

#include "mtk.h"

struct mtk_node {
    const char *name;
    const struct file_operations *fops; /* NULL: the plain node's */
};

static long node_ioctl(struct file *file, unsigned int cmd, unsigned long arg);
static int node_mmap(struct file *file, struct vm_area_struct *vma);

static const struct file_operations mtal_fops = {
    .owner = THIS_MODULE,
    .unlocked_ioctl = mtk_mtal_ioctl,
    .compat_ioctl = mtk_mtal_ioctl,
    .mmap = node_mmap,
};

/* the nodes, by minor: etc/device_table's and the ones libmtal.so opens besides */
static const struct mtk_node nodes[] = {
    {"mtal", &mtal_fops},
    {"cli"},
    {"cb", &mtk_cb_fops},
    {"kmem2"},
    {"b2r"},
    {"feeder"},
    {"tsrec"},
    {"vomx"},
    {"aomx"},
    {"rmmgr"},
    {"jpg"},
    {"fbm_png"},
    {"fbm_vss"},
    {"fbm_pq_tool"},
    {"fbm_venc"},
    {"fbm_drm"},
    {"fbm_mpeg2"},
    {"fbm_mpeg"},
    {"fbm_mpeg4_c"},
    {"fbm_dmx"},
    {"fbm_mddi_dyn_main"},
    {"fbm_scpos_dyn_main"},
    {"fbm_od_dump"},
    /* the graphics memory the soc layer's DILE_GAL maps (lgemu.ko's gfx.c), which libmtal's
     * MTGFX/MTOSD would reach through the kdriver's OSD planes: /dev/gfx, as mknod.conf's link
     * to /dev/lg/gfx0 would name it */
    {"gfx", &lgemu_gfx_fops},
};
#define MINORS ARRAY_SIZE(nodes)

static int ioctl_show(char *buf, size_t size, const struct lgemu_count *e)
{
    return scnprintf(buf, size, "%s ioctl %#x: %lu%s\n", e->what, e->a, e->count,
                     e->marked ? " (not modelled)" : "");
}

DEFINE_LGEMU_COUNTS(ioctl_counts, 1024, 64, ioctl_show);

void mtk_count_ioctl(const char *dev, unsigned int cmd)
{
    lgemu_count(&ioctl_counts, dev, cmd, 0);
}

long mtk_unhandled(const char *dev, unsigned int cmd, long ret)
{
    mtk_count_ioctl(dev, cmd);
    return lgemu_ioctl_unhandled(&ioctl_counts, dev, cmd, ret);
}

long mtk_mark_unhandled(const char *dev, unsigned int cmd, long ret)
{
    return lgemu_ioctl_unhandled(&ioctl_counts, dev, cmd, ret);
}

static const char *node_name(struct file *file)
{
    unsigned int minor = iminor(file_inode(file));

    return minor < MINORS ? nodes[minor].name : "?";
}

/* ---- memory ---- */

/* Every node's mappings: a node's offset o is the store's address minor << 40 | o, so each node
 * has its own memory and two mappings of one node at one offset share it. */
static struct lgemu_store store = LGEMU_STORE_INIT(store);

static int node_mmap(struct file *file, struct vm_area_struct *vma)
{
    u64 addr = (u64)iminor(file_inode(file)) << 40 | (u64)vma->vm_pgoff << PAGE_SHIFT;

    return lgemu_store_mmap(&store, vma, addr);
}

static long node_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    return mtk_unhandled(node_name(file), cmd, 0);
}

static int node_open(struct inode *inode, struct file *file)
{
    unsigned int minor = iminor(inode);

    if (minor >= MINORS)
        return -ENODEV;
    if (nodes[minor].fops) {
        replace_fops(file, nodes[minor].fops);
        if (file->f_op->open)
            return file->f_op->open(inode, file);
    }
    return 0;
}

static const struct file_operations node_fops = {
    .owner = THIS_MODULE,
    .open = node_open,
    .unlocked_ioctl = node_ioctl,
    .compat_ioctl = node_ioctl,
    .mmap = node_mmap,
};

/* ---- the nodes ---- */

static char *mtk_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return NULL;
}

static const struct class mtk_class = {.name = "mtk", .devnode = mtk_devnode};
static struct lgemu_cdev mtk_cdev;
static bool class_registered;
static DECLARE_BITMAP(devices_made, MINORS);
static struct proc_dir_entry *proc_dir;

static void mtk_exit(void)
{
    for (unsigned int m = 0; m < MINORS; m++)
        if (test_and_clear_bit(m, devices_made))
            device_destroy(&mtk_class, mtk_cdev.dev + m);
    if (class_registered)
        class_unregister(&mtk_class);
    class_registered = false;
    lgemu_cdev_del(&mtk_cdev);
    proc_remove(proc_dir);
    proc_dir = NULL;
    lgemu_store_free(&store);
}

static int __init mtk_init(void)
{
    int err = mtk_mtal_setup();

    if (err) {
        pr_err("the chip's parameters are missing or do not fit (%d)\n", err);
        return err;
    }
    err = -ENOMEM;
    proc_dir = proc_mkdir("mtk", NULL);
    if (!proc_dir ||
        !proc_create_data("ioctl", 0444, proc_dir, &lgemu_counts_proc_ops, &ioctl_counts))
        goto fail;
    err = lgemu_cdev_add(&mtk_cdev, 0, 0, MINORS, "mtk", &node_fops);
    if (err)
        goto fail;
    err = class_register(&mtk_class);
    if (err)
        goto fail;
    class_registered = true;
    for (unsigned int m = 0; m < MINORS; m++) {
        struct device *d =
            device_create(&mtk_class, NULL, mtk_cdev.dev + m, NULL, "%s", nodes[m].name);

        if (IS_ERR(d)) {
            err = PTR_ERR(d);
            goto fail;
        }
        set_bit(m, devices_made);
    }
    return 0;

fail:
    mtk_exit();
    return err;
}

static void __exit mtk_unload(void)
{
    mtk_exit();
}

module_init(mtk_init);
module_exit(mtk_unload);
MODULE_LICENSE("GPL");
MODULE_VERSION("emulator");
MODULE_DESCRIPTION("MediaTek MTAL kernel drivers for LG's mtka5lr userland (beanjar)");
