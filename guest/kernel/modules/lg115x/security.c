// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#include <linux/compat.h>
#include <linux/devmem_window.h>
#include <linux/ctype.h>
#include <linux/fs.h>
#include <linux/jhash.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/spi/spidev.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>
#include <linux/mutex.h>
#include <linux/math64.h>
#include <linux/mm.h>
#include <linux/timekeeping.h>
#include <linux/netlink.h>
#include <net/net_namespace.h>
#include <net/sock.h>
#include "lgemu-drv.h"
#include "lgemu-i2c.h"

#include "lg115x.h"

/* ---- the security engine: /dev/lg/se0 ---- */

#define SE_MAJOR     159
#define SE_IOC_MAGIC 'S'
/* se_kapi.h's numbers, as a 32-bit process computes them (a pointer argument is 4 bytes) */
#define SE_IORW_RUN_TZ_OP   _IOC(_IOC_READ | _IOC_WRITE, SE_IOC_MAGIC, 0, 4)
#define SE_IOR_WAIT         _IOC(_IOC_READ, SE_IOC_MAGIC, 1, 4)
#define SE_IOW_CACHE_CTRL   _IOC(_IOC_READ, SE_IOC_MAGIC, 0x80, 4)
#define SE_IO_CHECK_RESUME  _IO(SE_IOC_MAGIC, 0xfe)
#define SE_IOW_GET_MEM_INFO _IOC(_IOC_READ, SE_IOC_MAGIC, 0xff, 4)
#define SE_TZ_ARGS          8 /* _gSeInfo.tzArgs */
#define SE_CACHE_OPS        3 /* SE_CACHE_MAP, _INVALIDATE, _CLEAN */
#define SE_SEEN_MAX         64

static char *se_mem, *se_tzfw;
module_param(se_mem, charp, 0444);
MODULE_PARM_DESC(se_mem,
                 "the SE memory region, base:size (gMemCfgSE of the chip); unset: no /dev/lg/se0");
module_param(se_tzfw, charp, 0444);
MODULE_PARM_DESC(se_tzfw, "the TZ firmware's region, base:size (gMemCfgSE_TZFW), reported only");
/* Later kadpd (W18H's) forks an "se-kadpd" child that serves the secure world's requests over a
 * netlink family of the kernel's SE driver: it starts its SE thread, which takes KADP's SE lock
 * (sem.kadp-se-sema), then opens socket(AF_NETLINK, SOCK_RAW, family), binds and sends its
 * registration. Without the family the child exits with its thread still holding the lock, which
 * no other process then gets (tvservice's first KADP_SE call waits forever), and kadpd forks it
 * again. With it, the child registers and waits for requests, of which none come. */
static int se_netlink;
module_param(se_netlink, int, 0444);
MODULE_PARM_DESC(se_netlink, "the SE driver's netlink family for kadpd's se-kadpd; 0: none");

/* LX_MEMCFG_T of a 32-bit kernel: name (a pointer, 0 here), base, size */
struct lx_memcfg32 {
    u32 name, base, size;
};

/* SE_CACHE_CTRL_T from a 32-bit process */
struct se_cache_ctrl32 {
    u32 paddr, vaddr, size, operation;
};

static struct lx_memcfg32 se_cfg[2]; /* the SE region, the TZ firmware's */
static void *se_buf;                 /* the SE region's memory */
static DEFINE_MUTEX(se_lock);        /* the stock driver's operation lock */
static u32 se_seen[SE_SEEN_MAX][2];
static struct lx_seen se_ioc_seen = LX_SEEN_INIT(se_ioc_seen); /* the commands not answered */
static unsigned int se_seen_count;

static int se_parse(const char *text, struct lx_memcfg32 *cfg)
{
    u32 v[2];

    if (!text || lgemu_parse_u32s(text, 16, v, 2) || !v[1] || (v[0] | v[1]) & ~PAGE_MASK)
        return -EINVAL;
    cfg->base = v[0];
    cfg->size = v[1];
    return 0;
}

static long se_run_tz_op(void __user *uarg)
{
    u32 args[SE_TZ_ARGS];
    unsigned int i;

    if (copy_from_user(args, uarg, sizeof(args)))
        return -EIO;
    for (i = 0; i < se_seen_count; i++)
        if (se_seen[i][0] == args[0] && se_seen[i][1] == args[1])
            break;
    if (i == se_seen_count && i < SE_SEEN_MAX) {
        se_seen[i][0] = args[0];
        se_seen[i][1] = args[1];
        se_seen_count++;
        pr_info("se: TZ operation %#x %#x (%s), answered success\n", args[0], args[1],
                current->comm);
    }
    return copy_to_user(uarg, args, sizeof(args)) ? -EIO : 0;
}

static long se_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *uarg = compat_ptr(arg);
    struct se_cache_ctrl32 cache;
    long ret = -ENOTTY;

    mutex_lock(&se_lock);
    switch (cmd) {
    case SE_IORW_RUN_TZ_OP:
        ret = se_run_tz_op(uarg);
        break;
    case SE_IOR_WAIT: /* the secure world's completion flag: nothing is pending */
        ret = 0;
        break;
    case SE_IOW_CACHE_CTRL: /* the memory is coherent */
        ret = copy_from_user(&cache, uarg, sizeof(cache)) ? -EIO
              : cache.operation < SE_CACHE_OPS            ? 0
                                                          : -EINVAL;
        break;
    case SE_IO_CHECK_RESUME: /* never resumed from suspend: -ENOTTY, as the stock */
        break;
    case SE_IOW_GET_MEM_INFO:
        ret = copy_to_user(uarg, se_cfg, sizeof(se_cfg)) ? -EIO : 0;
        break;
    }
    mutex_unlock(&se_lock);
    if (ret == -ENOTTY)
        return lx_seen_log(&se_ioc_seen, "se", cmd, -ENOTTY);
    return ret;
}

/* The stock driver maps the physical address it is given (the SE region, by KADP): here the
 * module's pages of that region, shared by every mapping. */
static int se_mmap(struct file *file, struct vm_area_struct *vma)
{
    unsigned long first = se_cfg[0].base >> PAGE_SHIFT, pages = se_cfg[0].size >> PAGE_SHIFT;
    unsigned long len = vma_pages(vma);

    if (vma->vm_pgoff < first || vma->vm_pgoff - first > pages ||
        len > pages - (vma->vm_pgoff - first)) {
        pr_info("se: mmap of %#lx+%#lx outside the SE region (%s)\n", vma->vm_pgoff << PAGE_SHIFT,
                len << PAGE_SHIFT, current->comm);
        return -EINVAL;
    }
    return remap_vmalloc_range(vma, se_buf, vma->vm_pgoff - first);
}

static int se_open(struct inode *inode, struct file *file)
{
    return 0;
}

static const struct file_operations se_fops = {
    .owner = THIS_MODULE,
    .open = se_open,
    .unlocked_ioctl = se_ioctl,
    .compat_ioctl = se_ioctl,
    .mmap = se_mmap,
};

static struct lgemu_cdev se_cdev;
static struct sock *se_nl;

static void se_nl_input(struct sk_buff *skb)
{
    /* se-kadpd's registration (its port id): no secure world will ever send it a request */
}

void lx_se_remove(void)
{
    if (!se_cdev.added)
        return;
    if (se_nl)
        netlink_kernel_release(se_nl);
    se_nl = NULL;
    lgemu_cdev_del(&se_cdev);
    vfree(se_buf);
}

int lx_se_add(void)
{
    int err;

    if (!se_mem)
        return 0;
    if (se_parse(se_mem, &se_cfg[0]) || (se_tzfw && se_parse(se_tzfw, &se_cfg[1]))) {
        pr_err("se: se_mem and se_tzfw are base:size, page-aligned\n");
        return -EINVAL;
    }
    se_buf = vmalloc_user(se_cfg[0].size);
    if (!se_buf)
        return -ENOMEM;
    if (se_netlink) {
        struct netlink_kernel_cfg cfg = {.input = se_nl_input};

        if (se_netlink <= NETLINK_SMC || se_netlink >= MAX_LINKS) {
            pr_err("se: se_netlink=%d is not a vendor netlink family\n", se_netlink);
            vfree(se_buf);
            return -EINVAL;
        }
        se_nl = netlink_kernel_create(&init_net, se_netlink, &cfg);
        if (!se_nl) {
            vfree(se_buf);
            return -ENOMEM;
        }
    }
    err = lgemu_cdev_add(&se_cdev, SE_MAJOR, 0, 1, "se", &se_fops);
    if (err) {
        if (se_nl)
            netlink_kernel_release(se_nl);
        se_nl = NULL;
        vfree(se_buf);
        return err;
    }
    if (se_nl)
        pr_info("se: netlink family %d for se-kadpd\n", se_netlink);
    pr_info("se: se0, SE memory %#x+%#x, TZ firmware %#x+%#x, no secure world\n", se_cfg[0].base,
            se_cfg[0].size, se_cfg[1].base, se_cfg[1].size);
    return 0;
}
