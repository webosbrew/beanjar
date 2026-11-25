// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* mmcblk0: the eMMC's whole-disk device, as LG's kernel lists it in /proc/partitions and
 * /sys/block (the MMC block driver's 179:0, "mmc" in /proc/devices), sized as the partition map
 * says the eMMC is (lgemu_partinfo_capacity, partinfo.c). Only where the VM has a map: without
 * /proc/partinfo there is no eMMC either.
 *
 * Clients read its size, not its contents: libluna-prefs's getAllSysProperties takes
 * storageCapacity from `grep 'mmcblk0\s*$' /proc/partitions` (the blocks column << 10, in bytes)
 * and gives no properties at all when the line is missing; memchute's EFS and
 * snapshot-boot-manager read /sys/block/mmcblk0 (size, stat, queue). The partitions' contents are
 * on their own disks, the /dev/mmcblk0pN nodes guest/initramfs/rootfs/init makes; so this disk has
 * no partitions of its own (two names for one disk), and its own blocks read as zeros and cannot
 * be written (read-only). firmware.updatable replaces the /dev/mmcblk0 node with its writable
 * dm-linear device (bin/beanjar-emmc); /proc/partitions keeps this one. */
#define pr_fmt(fmt) KBUILD_MODNAME ": emmc: " fmt
#include <linux/bio.h>
#include <linux/blkdev.h>
#include <linux/major.h>
#include <linux/module.h>
#include "lgemu.h"

static struct gendisk *emmc;
static bool major_registered;

static void emmc_submit_bio(struct bio *bio)
{
    if (bio_op(bio) == REQ_OP_READ) {
        zero_fill_bio(bio);
        bio_endio(bio);
    } else {
        bio_io_error(bio);
    }
}

static const struct block_device_operations emmc_fops = {
    .owner = THIS_MODULE,
    .submit_bio = emmc_submit_bio,
};

int lgemu_emmc_init(void)
{
    struct queue_limits lim = {.features = BLK_FEAT_SYNCHRONOUS};
    u64 size = lgemu_partinfo_capacity();
    int err;

    if (!size) {
        pr_info("no partition map: no mmcblk0\n");
        return 0;
    }
    /* /proc/devices's "179 mmc", as on a set; the disk's own numbers do not depend on it */
    major_registered = register_blkdev(MMC_BLOCK_MAJOR, "mmc") == 0;
    emmc = blk_alloc_disk(&lim, NUMA_NO_NODE);
    if (IS_ERR(emmc)) {
        err = PTR_ERR(emmc);
        emmc = NULL;
        goto out_major;
    }
    emmc->major = MMC_BLOCK_MAJOR;
    emmc->first_minor = 0;
    emmc->minors = 1;
    emmc->flags |= GENHD_FL_NO_PART;
    emmc->fops = &emmc_fops;
    strscpy(emmc->disk_name, "mmcblk0", DISK_NAME_LEN);
    set_capacity(emmc, size >> SECTOR_SHIFT);
    set_disk_ro(emmc, true);
    err = add_disk(emmc);
    if (err) {
        put_disk(emmc);
        emmc = NULL;
        goto out_major;
    }
    pr_info("mmcblk0: %llu KiB\n", size >> 10);
    return 0;

out_major:
    if (major_registered)
        unregister_blkdev(MMC_BLOCK_MAJOR, "mmc");
    major_registered = false;
    /* the rest of the module does not need it: webOS starts, only without the disk */
    pr_warn("cannot add mmcblk0 (%d)\n", err);
    return 0;
}

void lgemu_emmc_exit(void)
{
    if (emmc) {
        del_gendisk(emmc);
        put_disk(emmc);
        emmc = NULL;
    }
    if (major_registered)
        unregister_blkdev(MMC_BLOCK_MAJOR, "mmc");
    major_registered = false;
}
