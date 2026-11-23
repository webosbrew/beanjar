// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* mstar-utopia.ko: MStar's kernel drivers, as far as MStar's Utopia userland (KADP: libkadaptor,
 * LG's build of it) needs them to bring itself up. The first user is W16R (M2, MSD93F2G); the
 * interface is MStar's, so the chip's values are parameters, from the chip table
 * (guest/targets/soc/, through boot.conf's SOC_MODULES), not compiled in.
 *
 * In short, the userland reaches memory through two address spaces, both backed here by plain RAM,
 * one page at a time as it is first touched, and shared by every process that maps the same
 * address:
 *
 * - MStar's physical memory: MIU0 from 0, MIU1 from miu_interval (a "PA"), which /dev/malloc maps
 *   after MALLOC_IOC_MPOOL_SET (MIU select + offset + size) and /dev/system's SYS_INFO and LG's
 *   /proc/mmapger describe. The TSP download controller can copy it into QMEM.
 * - The register windows (RIU and the other IP blocks), which /dev/miomap maps after
 *   MIOMAP_IOC_SET_MAP, by bus address. A register reads back what was last written.
 *
 * The default memory layout is the emulator's own (mmapger_layout()): mmap_ids records of
 * LG's mmapger (MStar's E_MMAP_ID_* order), each a slot of mmap_slot bytes (mmap_len's where it
 * gives one), one after another, MIU0 filled first.
 * Only the two that KADP compares with SYS_INFO have a meaning to it, lx_mem_id and lx_mem2_id.
 * mmap_file instead imports the firmware's record addresses and lengths, retaining aliases.
 *
 * The one Utopia module with a model is HWI2C, MStar's I2C master, whose board devices are
 * lgemu.ko's (lgemu-i2c.h), placed by the parameter i2c from the chip's table (below). */
#include "private.h"

/* ---- the chip: parameters, all required ---- */

/* LM14's NetCast root has no makedevs device table. Its driver nodes are made by
 * the module, and unmatched raw driver requests are refused while its ABI is
 * brought up independently of the webOS M2/M3 dialects. */
bool lm14;
module_param(lm14, bool, 0444);
MODULE_PARM_DESC(lm14, "LM14 NetCast driver ABI (own device nodes, unsupported unknown ioctls)");
bool direct_ir;
module_param(direct_ir, bool, 0444);
MODULE_PARM_DESC(direct_ir, "direct LG/MStar IR receiver at major 156 (W21A)");

/* The SoC's GPIO lines for /sys/class/gpio, lgemu's chip (lgemu-drv.h, lgemu_gpiochip_add): LG's
 * libdile_gpio (M3: W18R) drives its pins there by its own pin table, beside Utopia's GPIO module
 * (/dev/utopia). Unset (M2): no chip. */
static struct lgemu_gpiochip gpio_chip;
static unsigned int gpio_lines;
module_param(gpio_lines, uint, 0444);
MODULE_PARM_DESC(gpio_lines,
                 "the GPIO lines of /sys/class/gpio, numbered from 0 (0: no GPIO chip)");
static char *gpio_level[32];
static int gpio_level_count;
module_param_array(gpio_level, charp, &gpio_level_count, 0444);
MODULE_PARM_DESC(gpio_level, "inputs whose level is not 0, line:level (the board with nothing "
                             "connected)");
static unsigned int gpio_usb_vbus[8];
static int gpio_usb_vbus_count;
module_param_array(gpio_usb_vbus, uint, &gpio_usb_vbus_count, 0444);
MODULE_PARM_DESC(gpio_usb_vbus, "outputs that switch the USB ports' 5 V, which LG's PDM turns on "
                                "(lgemu_gpiochip_usb_vbus); none: the ports always have power");

/* ---- /dev/system_lg (247): LG's system driver; opens, every ioctl answers 0 (logged) ---- */

int plain_open(struct inode *inode, struct file *file)
{
    return 0;
}

long system_lg_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    ioctl_count("system_lg", cmd);
    return unhandled("system_lg", cmd, 0);
}

/* The majors are W16R's /etc/device_table's (W18R's has the same), whose nodes makedevs creates;
 * /dev/malloc (the memory pool driver's own name, not in the table) is made here, on devtmpfs.
 * The mailbox is there only with a TEE (secure_shm_id). /dev/gfx, the graphics memory the soc
 * layer's DILE_GAL maps (lgemu.ko's gfx.c), is MStar's frame buffer on the TV (the table's 29:0,
 * fbdev's major): here the kernel's fb core has that major (virtio-gpu's fbdev), so the node has
 * a dynamic one and is made here, mode 0666 (every GM client maps it), before makedevs, whose
 * 29:0 then finds it in place. */
static struct chardev {
    const char *name;
    unsigned int major;
    const struct file_operations *fops;
    bool node, mailbox, gop; /* mailbox: with a peer; gop: with gop_fb only (and gfx without) */
    bool ir;
    struct lgemu_cdev cdev;
} chardevs[] = {
    {"system", 148, &system_fops},
    {"malloc", 158, &mpool_fops, true},
    {"msmailbox", 175, &mbx_fops, false, true},
    {"miomap", 176, &miomap_fops},
    {"semutex", 181, &semutex_fops},
    {"system_lg", 247, &system_lg_fops},
    {"gfx", 0, &lgemu_gfx_fops, true},
    {"mstar-gop-fb", 0, &gop_fops, true, false, true},
    {.name = "ir", .major = 156, .fops = &ir_fops, .node = true, .ir = true},
};

static char *mstar_devnode(const struct device *dev, umode_t *mode)
{
    if (mode && (!strcmp(dev_name(dev), "gfx") || !strcmp(dev_name(dev), "mstar-gop-fb")))
        *mode = 0666;
    return NULL;
}

static bool v4l2_capture;
module_param(v4l2_capture, bool, 0444);
MODULE_PARM_DESC(v4l2_capture, "/dev/video60, the TV picture from the viewer (webos.capture)");

static struct class *mstar_class;
static struct proc_dir_entry *mmapger_dir, *utopia_entry, *counts_dir, *msos_dir;
static void mstar_utopia_exit(void);

static int __init mstar_utopia_init(void)
{
    int err;

    mmio_work_init();
    diagnostics_init();
    utopia_counts_init();
    err = mmapger_layout();
    if (!err)
        err = tsp_registers();
    if (!err)
        err = adc_idac_registers();
    if (!err)
        err = swi2c_parse();
    if (!err)
        err = lm14_mmio_registers();
    if (!err)
        err = registers_preset();
    if (!err)
        err = hwi2c_parse();
    if (!err)
        err = adv_fn009_parse();
    if (!err)
        err = audio_info_parse();
    if (!err)
        err = gpio_parse();
    /* XC's windows are in the panel's pixels */
    utopia_screen_init();
    if (err) {
        pr_err("the chip's parameters are missing or do not fit (%d)\n", err);
        goto fail;
    }
    err = -ENOMEM;
    semutex_init();
    mailbox_init();

    mmapger_dir = proc_mkdir("mmapger", NULL);
    if (!mmapger_dir || !proc_create("info.bin", 0444, mmapger_dir, &info_proc_ops) ||
        !proc_create("mmap.bin", 0444, mmapger_dir, &mmap_proc_ops))
        goto fail;
    utopia_entry = proc_create("utopia", 0666, NULL, &utopia_proc_ops);
    if (!utopia_entry)
        goto fail;
    if (utopia_has_pmlog() && (!(msos_dir = proc_mkdir("MsOS", NULL)) ||
                               !proc_create("PMLog", 0666, msos_dir, &pmlog_proc_ops)))
        goto fail;
    counts_dir = proc_mkdir("mstar-utopia", NULL);
    if (!counts_dir || !diagnostics_proc_create(counts_dir))
        goto fail;
    if (mmio_has_tsp() && !proc_create("tsp", 0444, counts_dir, &tsp_state_proc_ops))
        goto fail;
    if (mmio_has_pq() && !proc_create("pq_y_sram", 0444, counts_dir, &pq_y_state_proc_ops))
        goto fail;
    if (hwi2c_enabled() && !proc_create("hwi2c", 0444, counts_dir, &hwi2c_state_proc_ops))
        goto fail;
    if (swi2c_enabled() && !proc_create("swi2c", 0444, counts_dir, &swi2c_state_proc_ops))
        goto fail;
    if (memory_has_tee() && !proc_create("mmap.ini", 0444, mmapger_dir, &mmap_ini_proc_ops))
        goto fail;
    if (mailbox_enabled() && mailbox_proc_create(counts_dir))
        goto fail;

    mstar_class = class_create("mstar");
    if (IS_ERR(mstar_class)) {
        err = PTR_ERR(mstar_class);
        mstar_class = NULL;
        goto fail;
    }
    mstar_class->devnode = mstar_devnode;
    for (size_t i = 0; i < ARRAY_SIZE(chardevs); i++) {
        struct chardev *c = &chardevs[i];

        if (c->mailbox && !mailbox_enabled())
            continue;
        if (c->ir && !direct_ir)
            continue;
        /* with the GOP node, the frame buffer is the GOP's pool, not lgemu's gfx store */
        if (c->gop != (memory_gop_id() >= 0) && (c->gop || !strcmp(c->name, "gfx")))
            continue;
        /* one that cannot have its number is left out, not the whole part */
        err = lgemu_cdev_add(&c->cdev, c->major, 0, 1, c->name, c->fops);
        if (err) {
            if (err == -EBUSY)
                pr_warn("%s: major %u is taken\n", c->name, c->major);
            else
                pr_warn("%s: cannot add\n", c->name);
            continue;
        }
        if (c->node || lm14)
            device_create(mstar_class, NULL, c->cdev.dev, NULL, "%s", c->name);
    }
    err = lgemu_gpiochip_add(&gpio_chip, "mstar", gpio_lines, gpio_level, gpio_level_count);
    if (err)
        goto fail;
    err = lgemu_gpiochip_usb_vbus(&gpio_chip, gpio_usb_vbus, gpio_usb_vbus_count);
    if (err)
        goto fail;
    if (v4l2_capture) {
        err = lgemu_v4l2_capture_add();
        if (err)
            goto fail;
    }
    memory_log_layout();
    return 0;

fail:
    mstar_utopia_exit();
    return err;
}

static void mstar_utopia_exit(void)
{
    mmio_work_cancel();
    lgemu_v4l2_capture_remove();
    lgemu_gpiochip_remove(&gpio_chip);
    for (size_t i = ARRAY_SIZE(chardevs); i--;) {
        struct chardev *c = &chardevs[i];

        if (c->cdev.added && (c->node || lm14))
            device_destroy(mstar_class, c->cdev.dev);
        lgemu_cdev_del(&c->cdev);
    }
    if (mstar_class)
        class_destroy(mstar_class);
    mstar_class = NULL;
    proc_remove(counts_dir);
    proc_remove(utopia_entry);
    proc_remove(mmapger_dir);
    proc_remove(msos_dir);
    counts_dir = utopia_entry = mmapger_dir = msos_dir = NULL;
    gop_exit();
    memory_store_free();
    mmio_free();
    memory_layout_free();
    mailbox_free();
}

static void __exit mstar_utopia_unload(void)
{
    mstar_utopia_exit();
}

module_init(mstar_utopia_init);
module_exit(mstar_utopia_unload);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("MStar's kernel drivers for its Utopia userland (KADP), for the webOS emulator");
