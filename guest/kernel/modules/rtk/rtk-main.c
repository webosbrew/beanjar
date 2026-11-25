// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* rtk.ko: Realtek's kernel drivers, as far as Realtek's KADP userland (libkadaptor, LG's build of
 * it) needs them to bring itself up. The first user is W19P (K5LP, RTD2872, webOS 4.10); the
 * interface is Realtek's and the same on K6LP and K8HP, so the chip's values are parameters, from
 * the chip table (guest/targets/soc/, through boot.conf's SOC_MODULES), not
 * compiled in.
 *
 * One physical store backs everything: zeroed pages by physical address ("PA"), allocated as they
 * are first touched and shared by every mapping of the same address. Two parts of it mean
 * something to the userland:
 *
 * - RBUS, the register window (rbus_phys, rbus_size), which /dev/auth's MAP puts into every KADP
 *   process. A register reads back what was last written; reg_init gives power-on values and
 *   clock90k the two registers of the 90 kHz system clock, which the module keeps current.
 * - DCU1 and DCU2, the memory /dev/auth's ALLOC and ALLOCAV and /dev/rtkmem's ALLOC hand out
 *   (dcu1_base ... dcu2_size), each block's PA in its first word as Realtek's allocator leaves it.
 *
 * The audio and video CPUs are not there: the module answers their RPCs itself (/dev/rpc*), with
 * an idle hardware's "done" of the size the caller's stub decodes, sizes given per firmware by the
 * rpc_* parameters. The other Realtek device nodes the userland opens (nodes=) accept every ioctl
 * without touching its argument, counted in /proc/rtk/ioctl, as mstar-utopia's /proc/utopia is;
 * a few of them are modelled (sys-misc's micom, rtkaudio's memory, CEC's wait, the scaler's VT
 * capture, the scaler's windows for the viewer: ucom_node, allocs, waits, vt, vsc, v4l2_vsc).
 *
 * /dev/gfx is Realtek's GAL gfx driver (rtk_kadp_gal_gfx.c: class and region "gal_gfx", a dynamic
 * major, the node "gfx"), here the graphics memory the soc layer's DILE_GAL maps (lgemu.ko's
 * gfx.c); its ioctls (KADP's GAL, which the shim replaces) are not answered.
 *
 * Every ioctl is the 32-bit userland's: the structures are its layouts, and compat_ioctl is the
 * same handler (all 32-bit KADPs so far; W22H's arm64 kernel serves the same). */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#include <linux/fb.h>
#include <linux/vmalloc.h>
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
#include "rtk-tee.h"
#include "../../../../include/webos-video.h"

/* ---- generic node parameters ---- */

static char *nodes[64];
static int nodes_count;
module_param_array(nodes, charp, &nodes_count, 0444);
MODULE_PARM_DESC(nodes, "device nodes whose every ioctl answers 0, counted (rtkaudio, vscdev, "
                        "mcp/core, ...)");

/* Nodes whose driver hands out memory by physical address (rtkaudio's RTKAUDIO_IOC_ALLOC/FREE,
 * which KADP's RTKAudio_Malloc then maps through /dev/rtkmem): node:alloc:free, the two ioctl
 * numbers. The alloc's argument is the size, by value, and it returns the block's PA (DCU1 first),
 * > 0; the free's is the PA. The blocks are the system's, not a file's: they are handed to other
 * processes and the audio CPU, and live until freed. */
static char *allocs[8];
static int allocs_count;
module_param_array(allocs, charp, &allocs_count, 0444);
MODULE_PARM_DESC(allocs, "ioctls that allocate and free memory by PA, node:alloc_cmd:free_cmd");

/* Ioctls that keep what one process sets for the others to get (rtkaudio's
 * RTKAUDIO_IOC_SET_STATUS/GET_STATUS on W17P, _IOW/_IOR('a', 6/7, int): the audio HAL's resource,
 * flow and input states that every KADP client shares, which tvservice's HAL_AUDIO reads back
 * before it walks them): node:get_cmd:set_cmd. The argument is {u32 index (< 8), u32 buffer,
 * u32 size}; get copies what the last set of that index stored, zeros past it (and for an index
 * never set: Realtek's driver starts them zeroed). */
static char *stores[4];
static int stores_count;
module_param_array(stores, charp, &stores_count, 0444);
MODULE_PARM_DESC(stores,
                 "ioctls that keep a blob per index for every process, node:get_cmd:set_cmd");
#define STORE_SLOTS 8
#define STORE_MAX   (64 << 10)

/* Ioctls that wait for an event no emulated hardware ever has (CEC's receive): node:cmd. They sleep
 * until a signal, or fail at once with O_NONBLOCK as Realtek's do when nothing came (-EFAULT). An
 * accept-all answer would hand the caller an empty event and it would ask again at once. */
static char *waits[16];
static int waits_count;
module_param_array(waits, charp, &waits_count, 0444);
MODULE_PARM_DESC(waits, "ioctls that wait for an event that never comes, node:cmd");
/* ARC on or off: KADP_VFE_ARC_Enable's _IOW('w', 0x8f, int) on /dev/vfedev (W19P), whose int
 * lgemu keeps as the sound output's "VFE ARC Enable" (lgemu snd.c, /proc/lgemu/sound_out) */
static char *arc_ioctl;
module_param(arc_ioctl, charp, 0444);
MODULE_PARM_DESC(arc_ioctl, "the ARC enable ioctl, node:cmd (W19P vfedev:0x4004778f); unset: not "
                            "recorded");
static char arc_node[16];
static u32 arc_cmd;

/* the CPUs KADP has RPC rings to (rtk-rpc.c), each a ring there and one back, four minors: three
 * (the audio CPU and the two video CPUs, rpc0..11) on K5LP and later; four on the older chips
 * (W16K, W16P, W17P), whose KADP also opens rpc12..15, the rings of the KCPU's TeeManager
 * (SECURE_SYSTEM, program 501) */
static unsigned int rpc_cpus = 3;
module_param(rpc_cpus, uint, 0444);
MODULE_PARM_DESC(rpc_cpus, "the CPUs with RPC rings, /dev/rpc0 to rpc<4n-1> (3 or 4; default 3)");

/* the TEE (tee.c): /dev/tee0, OP-TEE's user ABI, with the stand-in TAs of tee-ta.c */
static bool tee;
module_param(tee, bool, 0444);
MODULE_PARM_DESC(tee, "make /dev/tee0, the TEE with the stand-in TAs");

static int ioctl_show(char *buf, size_t size, const struct lgemu_count *e)
{
    return scnprintf(buf, size, "%s ioctl %#x: %lu\n", e->what, e->a, e->count);
}

DEFINE_LGEMU_COUNTS(ioctl_counts, 256, 64, ioctl_show);

void rtk_count_ioctl(const char *dev, unsigned int cmd)
{
    lgemu_count(&ioctl_counts, dev, cmd, 0);
}

/* an ioctl a modelled device does not know: counted, and logged the first time, with its answer */
long rtk_unhandled(const char *dev, unsigned int cmd, long ret)
{
    rtk_count_ioctl(dev, cmd);
    return lgemu_ioctl_unhandled(&ioctl_counts, dev, cmd, ret);
}

long rtk_mark_unhandled(const char *dev, unsigned int cmd, long ret)
{
    return lgemu_ioctl_unhandled(&ioctl_counts, dev, cmd, ret);
}

/* ---- the other device nodes: every ioctl 0, its argument untouched, counted ---- */

static const char *node_names[ARRAY_SIZE(nodes)];

static int node_open(struct inode *inode, struct file *file);

static DECLARE_WAIT_QUEUE_HEAD(never);

/* waits= and allocs=, parsed */
static struct {
    char node[NODE_NAME_LEN];
    u32 cmd;
} wait_rules[ARRAY_SIZE(waits)];
static struct {
    char node[NODE_NAME_LEN];
    u32 alloc, free;
} alloc_rules[ARRAY_SIZE(allocs)];
static struct {
    char node[NODE_NAME_LEN];
    u32 get, set;
    struct mutex lock;
    void *blob[STORE_SLOTS];
    u32 size[STORE_SLOTS];
} store_rules[ARRAY_SIZE(stores)];

static int node_rules_parse(void)
{
    if (arc_ioctl && lgemu_parse_named(arc_ioctl, arc_node, sizeof(arc_node), 0, &arc_cmd, 1)) {
        pr_err("arc_ioctl=%s: not node:cmd\n", arc_ioctl);
        return -EINVAL;
    }
    for (int i = 0; i < waits_count; i++) {
        if (lgemu_parse_named(waits[i], wait_rules[i].node, NODE_NAME_LEN, 0, &wait_rules[i].cmd,
                              1)) {
            pr_err("waits=%s: not node:cmd\n", waits[i]);
            return -EINVAL;
        }
    }
    for (int i = 0; i < allocs_count; i++) {
        u32 v[2];

        if (lgemu_parse_named(allocs[i], alloc_rules[i].node, NODE_NAME_LEN, 0, v, 2)) {
            pr_err("allocs=%s: not node:alloc_cmd:free_cmd\n", allocs[i]);
            return -EINVAL;
        }
        alloc_rules[i].alloc = v[0];
        alloc_rules[i].free = v[1];
    }
    for (int i = 0; i < stores_count; i++) {
        u32 v[2];

        if (lgemu_parse_named(stores[i], store_rules[i].node, NODE_NAME_LEN, 0, v, 2)) {
            pr_err("stores=%s: not node:get_cmd:set_cmd\n", stores[i]);
            return -EINVAL;
        }
        store_rules[i].get = v[0];
        store_rules[i].set = v[1];
        mutex_init(&store_rules[i].lock);
    }
    return 0;
}

static void stores_free(void)
{
    for (int i = 0; i < stores_count; i++)
        for (int j = 0; j < STORE_SLOTS; j++)
            kvfree(store_rules[i].blob[j]);
}

/* stores= for node and cmd: the rule's index, *set whether cmd is its set; -1 neither */
static int node_store(const char *node, unsigned int cmd, bool *set)
{
    for (int i = 0; i < stores_count; i++) {
        if (strcmp(store_rules[i].node, node))
            continue;
        if (cmd == store_rules[i].get || cmd == store_rules[i].set) {
            *set = cmd == store_rules[i].set;
            return i;
        }
    }
    return -1;
}

static long node_store_ioctl(int i, bool set, unsigned long arg)
{
    u32 a[3];
    void __user *buf;
    long ret = 0;

    if (copy_from_user(a, compat_ptr(arg), sizeof(a)))
        return -EFAULT;
    if (a[0] >= STORE_SLOTS || a[2] > STORE_MAX)
        return -EINVAL;
    buf = compat_ptr(a[1]);
    mutex_lock(&store_rules[i].lock);
    if (set) {
        void *blob = kvmalloc(a[2] ? a[2] : 1, GFP_KERNEL);

        if (!blob)
            ret = -ENOMEM;
        else if (copy_from_user(blob, buf, a[2])) {
            kvfree(blob);
            ret = -EFAULT;
        } else {
            kvfree(store_rules[i].blob[a[0]]);
            store_rules[i].blob[a[0]] = blob;
            store_rules[i].size[a[0]] = a[2];
        }
    } else {
        u32 n = min(a[2], store_rules[i].size[a[0]]);

        if (n && copy_to_user(buf, store_rules[i].blob[a[0]], n))
            ret = -EFAULT;
        else if (a[2] > n && clear_user(buf + n, a[2] - n))
            ret = -EFAULT;
    }
    mutex_unlock(&store_rules[i].lock);
    return ret;
}

/* whether node's ioctl cmd is one of waits= */
static bool node_waits(const char *node, unsigned int cmd)
{
    for (int i = 0; i < waits_count; i++)
        if (wait_rules[i].cmd == cmd && !strcmp(wait_rules[i].node, node))
            return true;
    return false;
}

/* allocs= for node and cmd: 1 alloc, 2 free, 0 neither */
static int node_alloc_kind(const char *node, unsigned int cmd)
{
    for (int i = 0; i < allocs_count; i++) {
        if (strcmp(alloc_rules[i].node, node))
            continue;
        if (cmd == alloc_rules[i].alloc)
            return 1;
        if (cmd == alloc_rules[i].free)
            return 2;
    }
    return 0;
}

static long node_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    int kind;
    int idx = rtk_vsc_index(file->private_data);

    /* Counts retain the label pointer. V4L2 private_data is freed on close;
     * the read-only module parameter's matching name lives until unload. */
    rtk_count_ioctl(idx >= 0 ? rtk_vsc_counter_name(idx) : file->private_data, cmd);
    if (idx >= 0)
        return rtk_vsc_ioctl(file, cmd, arg);
    if (rtk_vsc_legacy_node(file->private_data))
        return rtk_vsc_legacy_ioctl(cmd, arg);
    kind = node_alloc_kind(file->private_data, cmd);
    if (kind)
        return rtk_node_memory_ioctl(kind, arg);
    {
        bool set;
        int i = node_store(file->private_data, cmd, &set);

        if (i >= 0)
            return node_store_ioctl(i, set, arg);
    }
    if (node_waits(file->private_data, cmd)) {
        if (file->f_flags & O_NONBLOCK)
            return -EFAULT;
        return wait_event_interruptible(never, false);
    }
    if (rtk_vt_node(file->private_data))
        return rtk_vt_ioctl(cmd, arg);
    if (rtk_ucom_command(file->private_data, cmd))
        return rtk_ucom_ioctl(cmd, arg);
    if (arc_ioctl && cmd == arc_cmd && !strcmp(file->private_data, arc_node)) {
        int on;

        if (get_user(on, (int __user *)compat_ptr(arg)))
            return -EFAULT;
        lgemu_snd_out_set("VFE ARC Enable", (long[]){on}, 1);
    }
    return 0;
}

static ssize_t node_write(struct file *file, const char __user *buf, size_t n, loff_t *pos)
{
    return n;
}

/* ---- the devices ---- */

#define MINOR_AUTH   128
#define MINOR_RTKMEM 129
#define MINOR_MD     130
#define MINOR_NODE0  160
#define MINORS       (MINOR_NODE0 + ARRAY_SIZE(nodes))

static const struct file_operations node_fops = {
    .owner = THIS_MODULE,
    .open = node_open,
    .release = rtk_vsc_release,
    .read = rtk_zero_read,
    .write = node_write,
    .unlocked_ioctl = node_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};

static int node_open(struct inode *inode, struct file *file)
{
    const char *name = node_names[iminor(inode) - MINOR_NODE0];
    int idx = rtk_vsc_index(name);

    if (idx >= 0)
        return rtk_vsc_open(file, name, idx);
    file->private_data = (void *)name;
    return 0;
}

/* one character device over all the minors; open picks the device's operations by minor, as
 * Realtek's rpc_poll_open does */
static const struct file_operations *minor_fops(unsigned int minor)
{
    if (minor < 4 * rpc_cpus || minor == RPC_CTRL)
        return &rtk_rpc_fops;
    if (minor == MINOR_AUTH)
        return &rtk_auth_fops;
    if (minor == MINOR_RTKMEM)
        return &rtk_rtkmem_fops;
    if (minor == MINOR_MD)
        return &rtk_md_fops;
    if (minor >= MINOR_NODE0 && minor < MINOR_NODE0 + nodes_count)
        return &node_fops;
    return NULL;
}

static int rtk_open(struct inode *inode, struct file *file)
{
    const struct file_operations *fops = minor_fops(iminor(inode));

    if (!fops)
        return -ENODEV;
    replace_fops(file, fops_get(fops)); /* drops rtk_fops' module reference: take this one's */
    return fops->open(inode, file);
}

static const struct file_operations rtk_fops = {.owner = THIS_MODULE, .open = rtk_open};

static char *rtk_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return NULL;
}

static const struct class rtk_class = {.name = "rtk", .devnode = rtk_devnode};
static struct lgemu_cdev rtk_cdev;
static bool class_registered;
static DECLARE_BITMAP(devices_made, MINORS);

static int device_make(unsigned int minor, const char *name)
{
    struct device *d = device_create(&rtk_class, NULL, rtk_cdev.dev + minor, NULL, "%s", name);

    if (IS_ERR(d))
        return PTR_ERR(d);
    set_bit(minor, devices_made);
    return 0;
}

/* nodes= names; "a/b" is devtmpfs's /dev/a/b, a device named "a!b" */
static int nodes_parse(void)
{
    for (int i = 0; i < nodes_count; i++) {
        char *name = kstrdup(nodes[i], GFP_KERNEL);

        if (!name)
            return -ENOMEM;
        strreplace(name, '/', '!');
        node_names[i] = name;
    }
    return 0;
}

/* ---- /proc/rtk ---- */

static struct proc_dir_entry *proc_dir;

bool rtk_node_exists(const char *name)
{
    for (int i = 0; i < nodes_count; i++)
        if (!strcmp(nodes[i], name))
            return true;
    return false;
}

static bool v4l2_capture;
module_param(v4l2_capture, bool, 0444);
MODULE_PARM_DESC(v4l2_capture, "/dev/video60, the TV picture from the viewer (webos.capture)");

static void rtk_exit(void);

static int __init rtk_init(void)
{
    int err;

    rtk_clock_prepare();
    if (rpc_cpus < 3 || rpc_cpus > RPC_CPUS_MAX) {
        pr_err("rpc_cpus=%u: not 3 or 4\n", rpc_cpus);
        return -EINVAL;
    }
    err = rtk_memory_params_check();
    if (!err)
        err = rtk_dcu_setup();
    if (!err)
        err = rtk_registers_preset();
    if (!err)
        err = rtk_rpc_rules_parse();
    if (!err)
        err = nodes_parse();
    if (!err)
        err = node_rules_parse();
    if (!err)
        err = rtk_vt_setup();
    if (!err)
        err = rtk_fb_setup();
    if (!err)
        err = rtk_vsc_setup();
    if (err) {
        pr_err("the chip's parameters are missing or do not fit (%d)\n", err);
        goto fail;
    }

    err = rtk_store_file_create();
    if (err)
        goto fail;
    err = -ENOMEM;
    proc_dir = proc_mkdir("rtk", NULL);
    if (!proc_dir ||
        !proc_create_data("ioctl", 0444, proc_dir, &lgemu_counts_proc_ops, &ioctl_counts) ||
        !rtk_rpc_proc_add(proc_dir))
        goto fail;

    err = rtk_boards_add();
    if (err)
        goto fail;

    err = rtk_pwms_make();
    if (err)
        goto fail;
    err = rtk_gpio_add();
    if (err)
        goto fail;

    err = rtk_gal_gfx_add();
    if (err)
        goto fail;
    if (tee) {
        err = rtk_tee_add(proc_dir);
        if (err)
            goto fail;
    }
    if (v4l2_capture) {
        err = lgemu_v4l2_capture_add();
        if (err)
            goto fail;
    }
    err = lgemu_cdev_add(&rtk_cdev, 0, 0, MINORS, "rtk", &rtk_fops);
    if (err)
        goto fail;
    err = class_register(&rtk_class);
    if (err)
        goto fail;
    class_registered = true;

    for (unsigned int m = 0; m < 4 * rpc_cpus && !err; m++) {
        char name[8];

        snprintf(name, sizeof(name), "rpc%u", m);
        err = device_make(m, name);
    }
    if (!err)
        err = device_make(RPC_CTRL, "rpc100");
    if (!err)
        err = device_make(MINOR_AUTH, "auth");
    if (!err)
        err = device_make(MINOR_RTKMEM, "rtkmem");
    if (!err)
        err = device_make(MINOR_MD, "md0");
    for (int i = 0; i < nodes_count && !err; i++)
        err = device_make(MINOR_NODE0 + i, node_names[i]);
    if (err)
        goto fail;

    rtk_memory_log(nodes_count);
    return 0;

fail:
    rtk_exit();
    return err;
}

static void rtk_exit(void)
{
    unsigned int m;

    stores_free();
    rtk_fb_remove();
    lgemu_v4l2_capture_remove();
    rtk_tee_remove();
    rtk_gal_gfx_remove();
    for_each_set_bit(m, devices_made, MINORS) device_destroy(&rtk_class, rtk_cdev.dev + m);
    bitmap_zero(devices_made, MINORS);
    if (class_registered)
        class_unregister(&rtk_class);
    class_registered = false;
    lgemu_cdev_del(&rtk_cdev);
    proc_remove(proc_dir);
    proc_dir = NULL;
    rtk_boards_remove();
    rtk_gpio_remove();
    rtk_pwms_remove();
    rtk_clock_stop();
    rtk_store_file_close();
    for (size_t i = 0; i < ARRAY_SIZE(node_names); i++) {
        kfree(node_names[i]);
        node_names[i] = NULL;
    }
    rtk_rpc_cleanup();
    rtk_memory_cleanup();
}

static void __exit rtk_unload(void)
{
    rtk_exit();
}

module_init(rtk_init);
module_exit(rtk_unload);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Realtek's kernel drivers for its KADP userland, for the webOS emulator");
