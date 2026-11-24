// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* /sys/kernel/mm/low_mem_notify: LG's low-memory notifier, for LG's memchuted.
 *
 * LG's kernel has it built in (mm/low-mem-notify.c, CONFIG_LOW_MEM_NOTIFY, after a Chromium OS
 * notifier). The version here is the WT1H kernel's (3.10.19-32.afro.6, read from its code: the
 * 2014 GPL source, linux-3.10-lg115x, is an earlier one without xreclaimable, ratio, zramswap and
 * force). docs/dev/guides/build.md, "Kernel module lgemu", has the whole account; in short:
 *
 * "Usable" memory, in pages, is
 *     free - reserved + free swap - swap cache - (zramswap ? free swap / 3 : 0)
 *     + file pages + reclaimable slab + xreclaimable - workingset - shmem
 * clamped at 0. A process registers thresholds in pages, each with an eventfd, by writing
 * "<eventfd> <threshold>" to event_ctrl; with ratio set the number is a percentage of total
 * memory, and a negative one means all of it. The thresholds are kept sorted, and "level" is the
 * index of the highest one below usable memory (-1, printed unsigned, below all of them). Each
 * time usable memory crosses a threshold, in either direction, its eventfd is signalled; "force"
 * signals the current one and re-evaluates. "reset" drops every threshold.
 *
 * LG evaluates on every page allocation (a hook in __alloc_pages_nodemask). A module cannot hook
 * the allocator, so this one evaluates every poll_ms (not in a suspend: the work is freezable)
 * and whenever reclaim runs its shrinker, which is when memory is actually short. As an addition
 * to LG's driver, a change of level also wakes poll() on "level" (sysfs_notify). */
#define pr_fmt(fmt) KBUILD_MODNAME ": low_mem_notify: " fmt
#include <linux/eventfd.h>
#include <linux/kernfs.h>
#include <linux/kobject.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/nodemask.h>
#include <linux/shrinker.h>
#include <linux/slab.h>
#include <linux/sort.h>
#include <linux/spinlock.h>
#include <linux/swap.h>
#include <uapi/linux/sysinfo.h>
#include <linux/sysfs.h>
#include <linux/vmstat.h>
#include <linux/workqueue.h>
#include "lgemu.h"

static unsigned int poll_ms = 200;
module_param(poll_ms, uint, 0644);
MODULE_PARM_DESC(poll_ms, "low_mem_notify: evaluate usable memory every this many ms");

struct threshold {
    struct eventfd_ctx *eventfd; /* one reference, owned by the table */
    unsigned long pages;
};

struct thresholds {
    int current_index; /* "level" */
    unsigned int size;
    struct threshold entry[]; /* ascending by pages */
};

/* LG's defaults: a 120 MiB system working set, ratio on (memchute's job turns it off). */
static unsigned long workingset_pages = (120UL << 20) >> PAGE_SHIFT;
static unsigned long xreclaimable_pages;
static bool ratio = true, zramswap, efs_swap;

static struct thresholds *table; /* under lock, which every evaluation takes */
static DEFINE_SPINLOCK(lock);
static DEFINE_MUTEX(update_lock); /* one writer of the table at a time */

static struct kobject *mm_kobject;
static struct shrinker *shrinker;
static struct delayed_work poll_work;
static int notified_level = INT_MIN;

static unsigned long reserved_pages(void)
{
    unsigned long pages = 0;
    int nid;

    for_each_online_node(nid) pages += NODE_DATA(nid)->totalreserve_pages;
    return pages;
}

/* Use the kernel's current accounting, including swapoff in progress. */
static unsigned long total_pages(void)
{
    struct sysinfo info = {};

    si_swapinfo(&info);
    return totalram_pages() + info.totalswap;
}

static long count_usable(void)
{
    long free_swap = get_nr_swap_pages();
    long anon = (long)global_zone_page_state(NR_FREE_PAGES) - (long)reserved_pages() + free_swap -
                (long)total_swapcache_pages() - (zramswap && !efs_swap ? free_swap / 3 : 0);
    long file = (long)global_node_page_state(NR_FILE_PAGES) +
                (long)global_node_page_state_pages(NR_SLAB_RECLAIMABLE_B) +
                (long)xreclaimable_pages - (long)workingset_pages -
                (long)global_node_page_state(NR_SHMEM);
    return max(anon + file, 0L);
}

static long count_free(void)
{
    long free =
        (long)global_zone_page_state(NR_FREE_PAGES) - (long)reserved_pages() + get_nr_swap_pages();
    long file = (long)global_node_page_state(NR_FILE_PAGES) -
                (long)global_node_page_state(NR_SHMEM) - (long)total_swapcache_pages();
    return max(free, file);
}

/* Moves "level" to where usable memory is, signalling every threshold it crosses on the way;
 * with force, the current threshold is signalled first. Returns whether the level changed. */
static bool evaluate_locked(bool force)
{
    struct thresholds *t = table;
    unsigned long usable;
    int i, old;

    lockdep_assert_held(&lock);
    if (!t)
        return false;
    usable = count_usable();
    i = old = t->current_index;
    if (force && i >= 0)
        eventfd_signal(t->entry[i].eventfd);
    while (i >= 0 && usable < t->entry[i].pages)
        eventfd_signal(t->entry[i--].eventfd);
    while (i + 1 < (int)t->size && usable >= t->entry[i + 1].pages)
        eventfd_signal(t->entry[++i].eventfd);
    t->current_index = i;
    return i != old;
}

static bool evaluate(bool force)
{
    bool changed;

    spin_lock(&lock);
    changed = evaluate_locked(force);
    spin_unlock(&lock);
    return changed;
}

/* The periodic evaluation, and the one place that wakes poll() on "level" (sysfs_notify sleeps,
 * so the shrinker and the stores hand a level change over to it). */
static void poll_work_fn(struct work_struct *work)
{
    int level;

    spin_lock(&lock);
    evaluate_locked(false);
    level = table ? table->current_index : INT_MIN;
    spin_unlock(&lock);
    if (level != notified_level) {
        notified_level = level;
        sysfs_notify(mm_kobject, "low_mem_notify", "level");
    }
    queue_delayed_work(system_freezable_wq, &poll_work, msecs_to_jiffies(max(poll_ms, 10U)));
}

static void notify_now(void)
{
    mod_delayed_work(system_freezable_wq, &poll_work, 0);
}

static unsigned long shrinker_count(struct shrinker *s, struct shrink_control *sc)
{
    if (evaluate(false))
        notify_now();
    return SHRINK_EMPTY;
}

static unsigned long shrinker_scan(struct shrinker *s, struct shrink_control *sc)
{
    return SHRINK_STOP;
}

static int compare_thresholds(const void *a, const void *b)
{
    const struct threshold *x = a, *y = b;
    return x->pages < y->pages ? -1 : x->pages > y->pages;
}

static int add_threshold(struct eventfd_ctx *eventfd, unsigned long pages)
{
    struct thresholds *old, *new;
    unsigned int size;
    unsigned long usable;

    mutex_lock(&update_lock);
    old = table; /* only writers replace it, and they hold update_lock */
    size = old ? old->size + 1 : 1;
    new = kmalloc(struct_size(new, entry, size), GFP_KERNEL);
    if (!new) {
        mutex_unlock(&update_lock);
        return -ENOMEM;
    }
    new->size = size;
    if (old)
        memcpy(new->entry, old->entry, (size - 1) * sizeof(new->entry[0]));
    new->entry[size - 1] = (struct threshold){.eventfd = eventfd, .pages = pages};
    sort(new->entry, size, sizeof(new->entry[0]), compare_thresholds, NULL);

    spin_lock(&lock);
    evaluate_locked(false); /* LG signals the crossings of the old table first */
    usable = count_usable();
    new->current_index = -1;
    for (unsigned int i = 0; i < size; i++)
        if (usable > new->entry[i].pages)
            new->current_index++;
    table = new;
    spin_unlock(&lock);

    mutex_unlock(&update_lock);
    kfree(old); /* its eventfd references moved to the new table */
    notify_now();
    return 0;
}

static void reset_thresholds(void)
{
    struct thresholds *old;

    mutex_lock(&update_lock);
    spin_lock(&lock);
    old = table;
    table = NULL;
    spin_unlock(&lock);
    mutex_unlock(&update_lock);
    if (old) {
        for (unsigned int i = 0; i < old->size; i++)
            eventfd_ctx_put(old->entry[i].eventfd);
        kfree(old);
    }
}

/* a flag: any non-zero number sets it */
static ssize_t store_flag(bool *flag, const char *buf, size_t count)
{
    unsigned long v;

    if (kstrtoul(buf, 10, &v))
        return -EINVAL;
    WRITE_ONCE(*flag, v != 0);
    return count;
}

/* The files, with LG's names, modes and formats. */

static ssize_t free_pages_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    return sysfs_emit(buf, "%ld\n", count_free());
}

static ssize_t usable_pages_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    return sysfs_emit(buf, "%ld\n", count_usable());
}

static ssize_t level_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    ssize_t n = 0;

    spin_lock(&lock);
    if (table)
        n = sysfs_emit(buf, "%u\n", (unsigned int)table->current_index);
    spin_unlock(&lock);
    return n;
}

static ssize_t thresholds_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    ssize_t n = 0;

    spin_lock(&lock);
    if (table) {
        for (unsigned int i = 0; i < table->size; i++)
            n += sysfs_emit_at(buf, n, "%s%lu ", (int)i == table->current_index ? "*" : "",
                               table->entry[i].pages);
        n += sysfs_emit_at(buf, n, "\n");
    }
    spin_unlock(&lock);
    return n;
}

static ssize_t event_ctrl_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    ssize_t n = 0;

    spin_lock(&lock);
    if (table)
        n = sysfs_emit(buf, "%u\n", table->size);
    spin_unlock(&lock);
    return n;
}

/* "<eventfd> <threshold>" */
static ssize_t event_ctrl_store(struct kobject *k, struct kobj_attribute *a, const char *buf,
                                size_t count)
{
    struct eventfd_ctx *eventfd;
    unsigned long total = total_pages(), pages;
    int fd, err;
    long value;

    if (sscanf(buf, "%d %ld", &fd, &value) != 2)
        return -EINVAL;
    if (value < 0)
        pages = total;
    else if (ratio)
        pages = total * value / 100;
    else
        pages = value;
    eventfd = eventfd_ctx_fdget(fd);
    if (IS_ERR(eventfd))
        return PTR_ERR(eventfd);
    err = add_threshold(eventfd, pages);
    if (err) {
        eventfd_ctx_put(eventfd);
        return err;
    }
    return count;
}

static ssize_t reset_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    return 0;
}

static ssize_t reset_store(struct kobject *k, struct kobj_attribute *a, const char *buf,
                           size_t count)
{
    reset_thresholds();
    notify_now();
    return count;
}

/* read in pages, written in bytes (memparse: K, M, G suffixes; 0 refused), as in LG's GPL kernels
 * from webOS 3 to webOS 22: every firmware job that writes it writes "80M" */
static ssize_t workingset_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    return sysfs_emit(buf, "%lu\n", READ_ONCE(workingset_pages));
}

static ssize_t workingset_store(struct kobject *k, struct kobj_attribute *a, const char *buf,
                                size_t count)
{
    unsigned long long bytes = memparse(buf, NULL);

    if (!bytes)
        return -EINVAL;
    WRITE_ONCE(workingset_pages, bytes >> PAGE_SHIFT);
    return count;
}

/* in bytes (memparse: K, M, G suffixes), kept in pages */
static ssize_t xreclaimable_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    return sysfs_emit(buf, "%lu\n", READ_ONCE(xreclaimable_pages) << PAGE_SHIFT);
}

static ssize_t xreclaimable_store(struct kobject *k, struct kobj_attribute *a, const char *buf,
                                  size_t count)
{
    char *end;
    unsigned long long bytes = memparse(buf, &end);

    if (end == buf)
        return -EINVAL;
    WRITE_ONCE(xreclaimable_pages, bytes >> PAGE_SHIFT);
    return count;
}

static ssize_t ratio_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    return sysfs_emit(buf, "%d\n", READ_ONCE(ratio));
}

static ssize_t ratio_store(struct kobject *k, struct kobj_attribute *a, const char *buf,
                           size_t count)
{
    return store_flag(&ratio, buf, count);
}

static ssize_t zramswap_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    return sysfs_emit(buf, "%d\n", READ_ONCE(zramswap));
}

static ssize_t zramswap_store(struct kobject *k, struct kobj_attribute *a, const char *buf,
                              size_t count)
{
    return store_flag(&zramswap, buf, count);
}

static ssize_t force_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    return 0;
}

/* The stock swap task selects flash-backed mode here. Our linear backing has
 * neither a RAM compression pool nor a compressed-flash capacity multiplier. */
static ssize_t efs_swap_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
    return sysfs_emit(buf, "%d\n", READ_ONCE(efs_swap));
}

static ssize_t efs_swap_store(struct kobject *k, struct kobj_attribute *a, const char *buf,
                              size_t count)
{
    return store_flag(&efs_swap, buf, count);
}

static ssize_t force_store(struct kobject *k, struct kobj_attribute *a, const char *buf,
                           size_t count)
{
    if (evaluate(true))
        notify_now();
    return count;
}

static struct kobj_attribute free_pages_attr = __ATTR_RO(free_pages);
static struct kobj_attribute usable_pages_attr = __ATTR_RO(usable_pages);
static struct kobj_attribute level_attr = __ATTR_RO(level);
static struct kobj_attribute thresholds_attr = __ATTR_RO(thresholds);
static struct kobj_attribute event_ctrl_attr = __ATTR_RW(event_ctrl);
static struct kobj_attribute reset_attr = __ATTR_RW(reset);
static struct kobj_attribute workingset_attr = __ATTR_RW(workingset);
static struct kobj_attribute xreclaimable_attr = __ATTR_RW(xreclaimable);
static struct kobj_attribute ratio_attr = __ATTR_RW(ratio);
static struct kobj_attribute zramswap_attr = __ATTR_RW(zramswap);
static struct kobj_attribute force_attr = __ATTR_RW(force);
static struct kobj_attribute efs_swap_attr = __ATTR_RW(efs_swap);

static struct attribute *attrs[] = {
    &free_pages_attr.attr,
    &usable_pages_attr.attr,
    &level_attr.attr,
    &thresholds_attr.attr,
    &event_ctrl_attr.attr,
    &reset_attr.attr,
    &workingset_attr.attr,
    &xreclaimable_attr.attr,
    &ratio_attr.attr,
    &zramswap_attr.attr,
    &efs_swap_attr.attr,
    &force_attr.attr,
    NULL,
};

static const struct attribute_group group = {
    .name = "low_mem_notify",
    .attrs = attrs,
};

/* /sys/kernel/mm's kobject: mm_kobj is not exported, so it is found through sysfs (the kernfs
 * directory of a kobject has the kobject as its private data). */
static struct kobject *find_mm_kobject(void)
{
    struct kernfs_node *kn = kernfs_find_and_get(kernel_kobj->sd, "mm");
    struct kobject *kobj = NULL;

    if (!kn)
        return NULL;
    if (kernfs_type(kn) == KERNFS_DIR && kn->priv)
        kobj = kobject_get(kn->priv);
    kernfs_put(kn);
    return kobj;
}

int lgemu_low_mem_notify_init(void)
{
    int err;

    INIT_DELAYED_WORK(&poll_work, poll_work_fn);
    mm_kobject = find_mm_kobject();
    if (!mm_kobject)
        return -ENOENT;
    err = sysfs_create_group(mm_kobject, &group);
    if (err)
        goto put;
    shrinker = shrinker_alloc(0, "lgemu-low-mem-notify");
    if (!shrinker) {
        err = -ENOMEM;
        goto remove;
    }
    shrinker->count_objects = shrinker_count;
    shrinker->scan_objects = shrinker_scan;
    queue_delayed_work(system_freezable_wq, &poll_work, 0);
    shrinker_register(shrinker);
    pr_info("/sys/kernel/mm/low_mem_notify, evaluated every %u ms and on reclaim\n", poll_ms);
    return 0;
remove:
    sysfs_remove_group(mm_kobject, &group);
put:
    kobject_put(mm_kobject);
    return err;
}

void lgemu_low_mem_notify_exit(void)
{
    shrinker_free(shrinker);                /* waits for a running count */
    sysfs_remove_group(mm_kobject, &group); /* waits for running stores */
    cancel_delayed_work_sync(&poll_work);
    reset_thresholds();
    kobject_put(mm_kobject);
}
