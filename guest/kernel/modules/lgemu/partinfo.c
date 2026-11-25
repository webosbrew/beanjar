// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* /proc/partinfo: LG's eMMC partition map, as LG's kernel prints it.
 *
 * On a set the built-in partition parser block/partitions/lgemmc.c (W20L's linux-4.4.3, the same in
 * every LG kernel here) reads struct partmap_info from the eMMC's partinfo partition at boot,
 * registers mmcblk0p1..p(npartition-1) and creates /proc/partinfo, which webOS's userland reads to
 * find a partition by name (update, securitymanager, tvservice, libsestore, lowlevelstorage, the
 * mount scripts). Here the map is on the VM's partinfo disk (partinfo_disk=, which
 * guest/initramfs/rootfs/init sets to the disk boot.conf's PARTINFO_DISK names), read once at load
 * as LG's parser reads it once at boot: the first sizeof(struct partmap_info) bytes, the 64 slots
 * of lgemmc.c's PARTITION_MAX (lines 41-75). Without the magic 0x20120716 there is no file, as on
 * LG's kernel. The /dev/mmcblk0pN nodes are guest/initramfs/rootfs/init's (the partitions' own
 * disks).
 *
 * The text is proc_partinfo_show's (lgemmc.c lines 86-93): a header line, then
 * "part%d: %8.8llx %8.8llx \"%s\"" for slots 1 to npartition-1 (the last entry, reserved, is not
 * printed). */
#define pr_fmt(fmt) KBUILD_MODNAME ": partinfo: " fmt
#include <linux/fs.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include "lgemu.h"

#define PARTMAP_MAGIC 0x20120716
#define STR_LEN_MAX   32
#define PARTITION_MAX 64

/* lgemmc.c's structures, field for field */
struct device_info {
    char name[STR_LEN_MAX];
    unsigned long long size;
    unsigned long long phys;
    unsigned int virt;
    unsigned int cached;
    int bankwidth;
    unsigned int used;
};

struct partition_info {
    char name[STR_LEN_MAX];
    unsigned long long offset;
    unsigned long long size;
    char filename[STR_LEN_MAX];
    unsigned int filesize;
    unsigned int sw_ver;
    unsigned char used;
    unsigned char valid;
    unsigned int mask_flags;
};

struct partmap_info {
    unsigned int magic;
    unsigned int cur_epk_ver;
    unsigned int old_epk_ver;
    unsigned char npartition;
    struct device_info dev;
    struct partition_info partition[PARTITION_MAX];
};

static_assert(sizeof(struct partition_info) == 0x60);
static_assert(sizeof(struct partmap_info) == 0x1850);

static char *partinfo_disk;
module_param(partinfo_disk, charp, 0444);
MODULE_PARM_DESC(partinfo_disk,
                 "partinfo: the block device with LG's partition map (none: no /proc/partinfo)");

static struct partmap_info *partinfo;
static int nb_parts;
static struct proc_dir_entry *proc;

static int partinfo_show(struct seq_file *m, void *v)
{
    int slot;

    seq_puts(m, "dev:    offset   size  name\n");
    for (slot = 1; slot < nb_parts; slot++) {
        struct partition_info *pi = &partinfo->partition[slot - 1];
        seq_printf(m, "part%d: %8.8llx %8.8llx \"%s\"\n", slot, pi->offset, pi->size, pi->name);
    }
    return 0;
}

int lgemu_partinfo_init(void)
{
    struct file *f;
    loff_t pos = 0;
    ssize_t n;
    int slot;

    if (!partinfo_disk || !*partinfo_disk) {
        pr_info("no partinfo disk: no /proc/partinfo\n");
        return 0;
    }
    f = filp_open(partinfo_disk, O_RDONLY | O_LARGEFILE, 0);
    if (IS_ERR(f)) {
        pr_warn("%s: %ld: no /proc/partinfo\n", partinfo_disk, PTR_ERR(f));
        return 0;
    }
    partinfo = kzalloc(sizeof(*partinfo), GFP_KERNEL);
    n = partinfo ? kernel_read(f, partinfo, sizeof(*partinfo), &pos) : -ENOMEM;
    filp_close(f, NULL);
    if (n != sizeof(*partinfo) || partinfo->magic != PARTMAP_MAGIC) {
        pr_warn("%s: no partition map (%zd bytes, magic 0x%08x): no /proc/partinfo\n",
                partinfo_disk, n, n > 0 ? partinfo->magic : 0);
        kfree(partinfo);
        partinfo = NULL;
        return 0;
    }
    pr_info("LG EMMC PARTITION MAP! (%s)\n", partinfo_disk);
    /* LG's loop reads partition[slot - 1] for slot < npartition: no more than its 64 slots here */
    nb_parts = min_t(int, partinfo->npartition, PARTITION_MAX + 1);
    for (slot = 1; slot < nb_parts; slot++)
        partinfo->partition[slot - 1].name[STR_LEN_MAX - 1] = '\0';
    proc = proc_create_single("partinfo", 0, NULL, partinfo_show);
    if (!proc) {
        kfree(partinfo);
        partinfo = NULL;
        return -ENOMEM;
    }
    return 0;
}

/* The eMMC's size in bytes, for emmc.c's mmcblk0: the map's device size (device_info.size, the
 * eMMC's user area on a set: 0x1d2000000, 7634944 KiB, on UP7560's 8 GB part), or the end of the
 * last partition it prints if that lies further out. 0 without a map. */
u64 lgemu_partinfo_capacity(void)
{
    u64 size;
    int slot;

    if (!partinfo)
        return 0;
    size = partinfo->dev.size;
    for (slot = 1; slot < nb_parts; slot++) {
        struct partition_info *pi = &partinfo->partition[slot - 1];
        size = max(size, pi->offset + pi->size);
    }
    return size;
}

void lgemu_partinfo_exit(void)
{
    proc_remove(proc);
    proc = NULL;
    kfree(partinfo);
    partinfo = NULL;
}
