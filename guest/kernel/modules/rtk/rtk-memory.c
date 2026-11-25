// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Physical backing, DCUs, PLI/auth, rtkmem and the MD engine. */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#include <linux/file.h>
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
#include <linux/sched/mm.h>
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

static unsigned int rbus_phys, rbus_size;
module_param(rbus_phys, uint, 0444);
MODULE_PARM_DESC(rbus_phys, "the register window's physical address (RBUS_BASE_PHYS)");
module_param(rbus_size, uint, 0444);
MODULE_PARM_DESC(rbus_size, "the register window's size");

/* KADP's PLI address space in a process (/dev/auth): MAP looks for a base from pli_base to pli_max
 * in steps of pli_step, and maps RBUS at base + pli_rbus; ALLOC maps a block at base + offset
 * (offsets within pli_window, pli_pad after each block) and again at base + pli_uncached + offset.
 */
static unsigned int pli_base, pli_max, pli_step, pli_rbus, pli_window, pli_uncached, pli_pad;
module_param(pli_base, uint, 0444);
MODULE_PARM_DESC(pli_base, "the first PLI base MAP tries (DEF_MAP_ADDR)");
module_param(pli_max, uint, 0444);
MODULE_PARM_DESC(pli_max, "the last PLI base MAP tries (MAX_MAP_ADDR)");
module_param(pli_step, uint, 0444);
MODULE_PARM_DESC(pli_step, "the distance between the PLI bases MAP tries");
module_param(pli_rbus, uint, 0444);
MODULE_PARM_DESC(pli_rbus, "RBUS's offset from the PLI base");
module_param(pli_window, uint, 0444);
MODULE_PARM_DESC(pli_window, "the bytes of cached PLI blocks a process can have mapped");
module_param(pli_uncached, uint, 0444);
MODULE_PARM_DESC(pli_uncached, "a block's uncached alias's distance from its cached address");
module_param(pli_pad, uint, 0444);
MODULE_PARM_DESC(pli_pad, "the unused address space after each PLI block");

static unsigned int dcu1_base, dcu1_size, dcu2_base, dcu2_size;
module_param(dcu1_base, uint, 0444);
MODULE_PARM_DESC(dcu1_base, "DCU1's physical address (the memory ALLOC gives by default)");
module_param(dcu1_size, uint, 0444);
MODULE_PARM_DESC(dcu1_size, "DCU1's size (AUTH_IOCQ_DCUSIZE 1)");
module_param(dcu2_base, uint, 0444);
MODULE_PARM_DESC(dcu2_base, "DCU2's physical address (GFP_DCU2)");
module_param(dcu2_size, uint, 0444);
MODULE_PARM_DESC(dcu2_size, "DCU2's size (AUTH_IOCQ_DCUSIZE 2; 0: no DCU2)");

/* ---- the physical store: pages by PA, allocated on first touch ---- */

static struct lgemu_store pa_store = LGEMU_STORE_INIT(pa_store); /* lgemu-drv.h */

static void *rtk_store_ptr(u64 pa)
{
    return lgemu_store_ptr(&pa_store, pa);
}

/* copies between the store and kernel memory */
static int rtk_store_access(u64 pa, void *buf, size_t len, bool write)
{
    while (len) {
        size_t n = min_t(size_t, len, PAGE_SIZE - offset_in_page(pa));
        void *p = rtk_store_ptr(pa);

        if (!p)
            return -ENOMEM;
        if (write)
            memcpy(p, buf, n);
        else
            memcpy(buf, p, n);
        pa += n;
        buf += n;
        len -= n;
    }
    return 0;
}

/* A mapping of the store: the mmap offset is the PA, as /dev/mem's. Every mapping holds the
 * module (lgemu_store_mmap): a mapping outlives the file that made it. */
static int store_mmap(struct file *file, struct vm_area_struct *vma)
{
    return lgemu_store_mmap(&pa_store, vma, (u64)vma->vm_pgoff << PAGE_SHIFT);
}

/* The file the module maps the store with into a process for /dev/auth (MAP, ALLOC), as
 * Realtek's rtktlb does with do_mmap: KADP opens /dev/auth read-only, which could not have a
 * shared writable mapping itself. Not the module's owner: it lives as long as the module. */
static const struct file_operations store_fops = {.mmap = store_mmap};
static struct file *store_file;

/* A translated process (qemu-user, docs/dev/architecture/qemu-user-bridge.md, "Mappings a driver
 * places itself"): qemu-arm reserves the whole 4 GiB guest space with PROT_NONE mappings, which
 * MAP and ALLOC map over and an unmap puts back; natively nothing changes. */
static bool translated(void)
{
    return current->mm->task_size > SZ_4G;
}

static bool reservation(struct vm_area_struct *vma)
{
    return vma_is_anonymous(vma) && !vma->anon_vma && (vma->vm_flags & VM_NORESERVE) &&
           !(vma->vm_flags &
             (VM_READ | VM_WRITE | VM_EXEC | VM_SHARED | VM_GROWSDOWN | VM_GROWSUP | VM_LOCKED));
}

/* whether nothing but (when translated) qemu's reservation is mapped in [start, end) */
static bool range_free(unsigned long start, unsigned long end)
{
    struct mm_struct *mm = current->mm;
    bool qemu = translated(), free = true;
    struct vm_area_struct *vma;
    VMA_ITERATOR(vmi, mm, start);

    mmap_read_lock(mm);
    for_each_vma_range(vmi, vma, end)
    {
        if (!qemu || !reservation(vma)) {
            free = false;
            break;
        }
    }
    mmap_read_unlock(mm);
    return free;
}

static unsigned long store_map(unsigned long va, unsigned long size, u64 pa)
{
    unsigned long flags = MAP_SHARED | MAP_FIXED_NOREPLACE;

    if (translated()) {
        if (!range_free(va, va + size))
            return -EEXIST;
        flags = MAP_SHARED | MAP_FIXED;
    }
    return vm_mmap(store_file, va, size, PROT_READ | PROT_WRITE, flags, pa);
}

static void store_unmap(unsigned long va, unsigned long size)
{
    const unsigned long reserve = MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED | MAP_NORESERVE;

    if (!translated() || IS_ERR_VALUE(vm_mmap(NULL, va, size, PROT_NONE, reserve, 0)))
        vm_munmap(va, size);
}

static u64 reg_pa(u32 addr)
{
    return addr >= 0xa0000000 && addr < 0xc0000000 ? addr - 0xa0000000 : addr;
}

u32 *rtk_reg_ptr(u32 addr)
{
    u64 pa = reg_pa(addr);

    if (pa < rbus_phys || pa + 4 > (u64)rbus_phys + rbus_size || pa % 4)
        return NULL;
    return rtk_store_ptr(pa);
}

/* ---- DCU memory: blocks by PA ---- */

#define GFP_DCU1       0x1
#define GFP_DCU2       0x2
#define GFP_DCU1_FIRST 0x4
#define GFP_DCU2_FIRST 0x8

static struct dcu {
    u64 base, size;
    unsigned long *bitmap; /* a bit a page */
} dcus[2];

/* A block of DCU memory, the file's that allocated it: an auth block (va: its cached address in
 * the process), an AV block (ALLOCAV, not mapped) or an rtkmem one (MEM_IOCQALLOC). */
struct block {
    struct list_head list;
    struct file *owner;
    u64 pa;
    unsigned long size, va;
    int dcu;
    bool mapped;
};
static LIST_HEAD(blocks);
static DEFINE_MUTEX(dcu_lock);

int rtk_dcu_setup(void)
{
    u64 b[2] = {dcu1_base, dcu2_base}, s[2] = {dcu1_size, dcu2_size};

    for (int i = 0; i < 2; i++) {
        if (!s[i])
            continue;
        if (!b[i] || b[i] % PAGE_SIZE || s[i] % PAGE_SIZE || b[i] + s[i] > 0x100000000ULL ||
            (b[i] < (u64)rbus_phys + rbus_size && rbus_phys < b[i] + s[i]))
            return -EINVAL;
        dcus[i].base = b[i];
        dcus[i].size = s[i];
        dcus[i].bitmap = bitmap_zalloc(s[i] >> PAGE_SHIFT, GFP_KERNEL);
        if (!dcus[i].bitmap)
            return -ENOMEM;
    }
    return dcus[0].size ? 0 : -EINVAL;
}

u64 rtk_dcu_take(int i, unsigned long pages)
{
    unsigned long n = dcus[i].size >> PAGE_SHIFT, at;

    if (!dcus[i].size)
        return 0;
    at = bitmap_find_next_zero_area(dcus[i].bitmap, n, 0, pages, 0);
    if (at >= n)
        return 0;
    bitmap_set(dcus[i].bitmap, at, pages);
    return dcus[i].base + ((u64)at << PAGE_SHIFT);
}

/* A block of size bytes (a page multiple) for file, from the DCU flag asks for (the other one if
 * that is full, as Realtek's *_FIRST); its PA written into its first word when mark_pa (Realtek's
 * pli_malloc does: KADP reads it through the uncached alias). */
static struct block *block_alloc(struct file *file, unsigned long size, u32 flag, bool mark_pa)
{
    int first = flag & (GFP_DCU2 | GFP_DCU2_FIRST) ? 1 : 0;
    bool only = flag & (GFP_DCU1 | GFP_DCU2);
    struct block *b = kzalloc(sizeof(*b), GFP_KERNEL);
    u32 *word0;

    if (!b || !size)
        goto fail;
    mutex_lock(&dcu_lock);
    b->dcu = first;
    b->pa = rtk_dcu_take(first, size >> PAGE_SHIFT);
    if (!b->pa && !(only && dcus[first].size)) {
        b->dcu = !first;
        b->pa = rtk_dcu_take(!first, size >> PAGE_SHIFT);
    }
    if (b->pa) {
        b->owner = file;
        b->size = size;
        list_add(&b->list, &blocks);
    }
    mutex_unlock(&dcu_lock);
    if (!b->pa) {
        pr_warn_ratelimited("no %lu KiB left in DCU%d\n", size >> 10, first + 1);
        goto fail;
    }
    if (mark_pa) {
        word0 = rtk_store_ptr(b->pa);
        if (word0)
            *word0 = b->pa;
    }
    return b;

fail:
    kfree(b);
    return NULL;
}

/* takes b off the list; dcu_lock held */
static void block_unlink(struct block *b)
{
    list_del(&b->list);
    bitmap_clear(dcus[b->dcu].bitmap, (b->pa - dcus[b->dcu].base) >> PAGE_SHIFT,
                 b->size >> PAGE_SHIFT);
}

/* the block at pa (any file's, when owner is NULL), taken off the list */
static struct block *block_take(struct file *owner, u64 pa, unsigned long va)
{
    struct block *b, *found = NULL;

    mutex_lock(&dcu_lock);
    list_for_each_entry(b, &blocks, list)
    {
        if ((owner && b->owner != owner) || (va ? b->va != va : b->pa != pa || b->mapped))
            continue;
        found = b;
        block_unlink(b);
        break;
    }
    mutex_unlock(&dcu_lock);
    return found;
}

/* a released file's blocks go back (its mappings, if any are left, keep the pages: the store
 * frees nothing before the module goes) */
static void blocks_release(struct file *owner)
{
    struct block *b, *tmp;

    mutex_lock(&dcu_lock);
    list_for_each_entry_safe(b, tmp, &blocks, list)
    {
        if (b->owner != owner)
            continue;
        block_unlink(b);
        kfree(b);
    }
    mutex_unlock(&dcu_lock);
}

/* ---- /dev/auth: PLI, Realtek's auth device (mm/auth.c, mm/tlbmap.c) ---- */

#define AUTH_IOCQ_MAP       _IOW('k', 1, char[20])
#define AUTH_IOCQ_ALLOC     _IOW('k', 2, struct auth_alloc32)
#define AUTH_IOCQ_FREE      _IO('k', 3)
#define AUTH_IOCQ_FREEALL   _IO('k', 4)
#define AUTH_IOCQ_LISTALL   _IO('k', 5)
#define AUTH_IOCS_FLUSHVIRT _IOW('k', 6, struct auth_region32)
#define AUTH_IOCS_FLUSHPHYS _IOW('k', 7, struct auth_region32)
#define AUTH_IOCQ_ALLOCAV   _IOW('k', 8, struct auth_alloc32)
/* W16P's KADP (K2LP) passes the older mem_alloc_struct, without the name: the same requests */
#define AUTH_IOCQ_ALLOC8     _IOW('k', 2, struct auth_region32)
#define AUTH_IOCQ_ALLOCAV8   _IOW('k', 8, struct auth_region32)
#define AUTH_IOCQ_FREEAV     _IO('k', 9)
#define AUTH_IOCH_DUMPMEM    _IO('k', 10)
#define AUTH_IOCSINITLOGBUF  _IOW('k', 11, struct auth_region32)
#define AUTH_IOCTFREELOGBUF  _IO('k', 12)
#define AUTH_IOCTLOGSTART    _IO('k', 13)
#define AUTH_IOCGLOGSTOP     _IOR('k', 14, struct auth_region32)
#define AUTH_IOCXLOGNAME     _IOWR('k', 15, char[16])
#define AUTH_IOCSTHREADNAME  _IOW('k', 16, char[16])
#define AUTH_IOCTLOGEVENT    _IO('k', 17)
#define AUTH_IOC_HWSEM_INIT  _IO('k', 18) /* W19P's KADP; not in Realtek's public tree */
#define AUTH_IOC_HWSEM_LOCK  _IO('k', 19)
#define AUTH_IOCQ_DCUSIZE    _IO('k', 20)
#define AUTH_IOC_WATCH_SET   _IOW('j', 2, struct auth_region32)
#define AUTH_IOC_WATCH_CLEAR _IO('j', 3)

struct auth_alloc32 { /* mem_alloc_struct from a 32-bit process */
    u32 size, flag;
    char name[32];
};
static_assert(sizeof(struct auth_alloc32) == 0x28);
struct auth_region32 { /* mem_region_struct, sched_log_struct */
    u32 addr, size;
};

struct auth_file {
    struct mutex lock;
    unsigned long base;    /* 0: no MAP yet */
    struct mm_struct *mm;  /* the process MAP mapped RBUS into (mmgrab'd) */
    unsigned long *bitmap; /* the cached window, a bit a page */
};

static int auth_open(struct inode *inode, struct file *file)
{
    struct auth_file *f = kzalloc(sizeof(*f), GFP_KERNEL);

    if (!f)
        return -ENOMEM;
    f->bitmap = bitmap_zalloc(pli_window >> PAGE_SHIFT, GFP_KERNEL);
    if (!f->bitmap) {
        kfree(f);
        return -ENOMEM;
    }
    mutex_init(&f->lock);
    file->private_data = f;
    return 0;
}

/* Release unmaps the RBUS window MAP put in, as Realtek's auth_release does (rtktlb_munmap, in
 * the process closing the file; at exit the address space is already gone). KADP's
 * rhal_rpcdev_exit closes /dev/auth (pli_close) each time its count drops to 0, and the next
 * rhal_rpcdev_init opens and MAPs it again: the window must be free for that, or every cycle
 * takes the next base until none is left, MAP fails, and pli_init sets the PLI base to 0, from
 * which the next register read faults (W22H wpa_supplicant's SStore thread, every 2 s). */
static int auth_release(struct inode *inode, struct file *file)
{
    struct auth_file *f = file->private_data;

    if (f->mm) {
        if (current->mm == f->mm)
            store_unmap(f->base + pli_rbus, rbus_size);
        mmdrop(f->mm);
    }
    blocks_release(file);
    bitmap_free(f->bitmap);
    kfree(f);
    return 0;
}

/* MAP: the first base whose PLI space (the two block windows and RBUS) is unused; RBUS mapped
 * there. Realtek's driver checks only RBUS's place; the blocks go in later with MAP_FIXED. */
static long auth_map(struct auth_file *f)
{
    unsigned long addr, ret = 0;

    mutex_lock(&f->lock);
    if (f->base) {
        ret = f->base;
        goto out;
    }
    for (addr = pli_base; addr <= pli_max; addr += pli_step) {
        unsigned long rbus = addr + pli_rbus, got;

        if (!range_free(addr, max(addr + pli_uncached + pli_window, rbus + rbus_size)))
            continue;
        got = store_map(rbus, rbus_size, rbus_phys);
        if (got == rbus) {
            f->base = ret = addr;
            f->mm = current->mm;
            mmgrab(f->mm);
            break;
        }
        if (!IS_ERR_VALUE(got))
            store_unmap(got, rbus_size);
    }
out:
    mutex_unlock(&f->lock);
    if (!ret)
        pr_warn_ratelimited("auth: %s (%d): no room for PLI\n", current->comm, current->pid);
    else
        rtk_clock_start();
    return ret;
}

/* ALLOC: a block mapped twice, cached at base + offset and uncached at pli_uncached above it
 * (the same pages here: both are coherent RAM, and FLUSHVIRT has nothing to do); the cached
 * address, or 0. */
static long auth_alloc(struct file *file, struct auth_alloc32 *a)
{
    struct auth_file *f = file->private_data;
    unsigned long size = PAGE_ALIGN(a->size), pages, at, va = 0;
    struct block *b;

    if (!f->base || !size || size > pli_window)
        return 0;
    b = block_alloc(file, size, a->flag, true);
    if (!b)
        return 0;
    pages = (size + pli_pad) >> PAGE_SHIFT;
    mutex_lock(&f->lock);
    at = bitmap_find_next_zero_area(f->bitmap, pli_window >> PAGE_SHIFT, 0, pages, 0);
    if (at < pli_window >> PAGE_SHIFT) {
        unsigned long cva = f->base + (at << PAGE_SHIFT), got;

        got = store_map(cva, size, b->pa);
        if (got == cva) {
            got = store_map(cva + pli_uncached, size, b->pa);
            if (got == cva + pli_uncached) {
                bitmap_set(f->bitmap, at, pages);
                va = cva;
            } else {
                store_unmap(cva, size);
            }
        }
    }
    mutex_unlock(&f->lock);
    if (!va) {
        pr_warn_ratelimited("auth: %s (%d): no room to map %lu KiB (%.32s)\n", current->comm,
                            current->pid, size >> 10, a->name);
        mutex_lock(&dcu_lock);
        block_unlink(b);
        mutex_unlock(&dcu_lock);
        kfree(b);
        return 0;
    }
    mutex_lock(&dcu_lock);
    b->va = va;
    b->mapped = true;
    mutex_unlock(&dcu_lock);
    return va;
}

static long auth_free(struct file *file, unsigned long va)
{
    struct auth_file *f = file->private_data;
    struct block *b = va ? block_take(file, 0, va) : NULL;

    if (!b)
        return -EINVAL;
    store_unmap(va, b->size);
    store_unmap(va + pli_uncached, b->size);
    mutex_lock(&f->lock);
    bitmap_clear(f->bitmap, (va - f->base) >> PAGE_SHIFT, (b->size + pli_pad) >> PAGE_SHIFT);
    mutex_unlock(&f->lock);
    kfree(b);
    return 0;
}

static long auth_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *uarg = (void __user *)arg;
    struct auth_alloc32 a;
    struct block *b;

    switch (cmd) {
    case AUTH_IOCQ_MAP: /* the auth string ("Realtek DVR") is not checked */
        return auth_map(file->private_data);
    case AUTH_IOCQ_ALLOC:
    case AUTH_IOCQ_ALLOC8:
        memset(&a, 0, sizeof(a));
        if (copy_from_user(&a, uarg, _IOC_SIZE(cmd)))
            return 0;
        return auth_alloc(file, &a);
    case AUTH_IOCQ_FREE:
        return auth_free(file, arg);
    case AUTH_IOCQ_ALLOCAV: /* a PA, not mapped */
    case AUTH_IOCQ_ALLOCAV8:
        memset(&a, 0, sizeof(a));
        if (copy_from_user(&a, uarg, _IOC_SIZE(cmd)))
            return 0;
        b = block_alloc(file, PAGE_ALIGN(a.size), a.flag, true);
        return b ? b->pa : 0;
    case AUTH_IOCQ_FREEAV: /* any process's: a PA is handed around */
        b = block_take(NULL, arg, 0);
        kfree(b);
        return 0;
    case AUTH_IOCQ_DCUSIZE:
        return arg == GFP_DCU1 ? dcu1_size : arg == GFP_DCU2 ? dcu2_size : 0;
    case AUTH_IOCS_FLUSHVIRT: /* coherent RAM */
    case AUTH_IOCS_FLUSHPHYS:
        return 0;
    /* Realtek's scheduler log, thread names, the hardware semaphore and watchpoints: accepted */
    case AUTH_IOCQ_FREEALL:
    case AUTH_IOCQ_LISTALL:
    case AUTH_IOCH_DUMPMEM:
    case AUTH_IOCSINITLOGBUF:
    case AUTH_IOCTFREELOGBUF:
    case AUTH_IOCTLOGSTART:
    case AUTH_IOCGLOGSTOP:
    case AUTH_IOCXLOGNAME:
    case AUTH_IOCSTHREADNAME:
    case AUTH_IOCTLOGEVENT:
    case AUTH_IOC_HWSEM_INIT:
    case AUTH_IOC_HWSEM_LOCK:
    case AUTH_IOC_WATCH_SET:
    case AUTH_IOC_WATCH_CLEAR:
        rtk_count_ioctl("auth", cmd);
        return 0;
    }
    return rtk_unhandled("auth", cmd, 0);
}

ssize_t rtk_zero_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    return 0;
}

static ssize_t zero_write(struct file *file, const char __user *buf, size_t n, loff_t *pos)
{
    return 0;
}

/* ---- /dev/rtkmem: /dev/mem's minor 14 in Realtek's kernel ---- */

#define MEM_IOCQALLOC _IO('k', 32)
#define MEM_IOCQFREE  _IO('k', 33)

static int rtkmem_open(struct inode *inode, struct file *file)
{
    return 0;
}

static int rtkmem_release(struct inode *inode, struct file *file)
{
    blocks_release(file);
    return 0;
}

/* mmap(pa): the store at that PA. O_SYNC asks for an uncached mapping (kadp_mem_mmap's uncached
 * variant); it is the same cached one, as auth's alias is: the page may be mapped cached elsewhere,
 * and two attributes for one page lose coherency on ARM. */
static int rtkmem_mmap(struct file *file, struct vm_area_struct *vma)
{
    return store_mmap(file, vma);
}

static long rtkmem_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct block *b;

    switch (cmd) {
    case MEM_IOCQALLOC: /* arg: the size; a PA */
        b = block_alloc(file, PAGE_ALIGN(arg), 0, false);
        return b ? b->pa : -ENOMEM;
    case MEM_IOCQFREE:
        b = block_take(file, arg, 0);
        if (!b)
            return -EINVAL;
        kfree(b);
        return 0;
    }
    return rtk_unhandled("rtkmem", cmd, -ENOTTY);
}

/* ---- /dev/md0: the memory DMA engine ----
 *
 * KADP's md_* (md_mars_mmap.cpp) writes 16-byte commands (write(), or MD_IOC_WRITE_ISSUE_CMD) and
 * waits until READ_INST_COUNT reaches READ_ISSUED_INST_COUNT; the newer md_memcpy passes one
 * transaction (MD_IOC_DO_MEMCPY_TRANSACTION) once ABILITY_PROBE says it can. The engine is done at
 * once here: a sequential copy or fill is carried out in the store, and both counts are the number
 * of commands. Addresses are PAs. */
#define MD_IOC_READ_INST_COUNT        _IOR('k', 15, int) /* a u64, though */
#define MD_IOC_WRITE_ISSUE_CMD        _IOW('k', 16, int)
#define MD_IOC_READ_ISSUED_INST_COUNT _IOR('k', 17, int)
#define MD_IOC_COPY_PAGES             _IOW('k', 18, struct md_copy_pages)
#define MD_IOC_WAIT_FOR_COMPLETE      _IO('k', 19)
#define MD_IOC_WRITE_EX_CMD           _IOW('k', 20, struct md_write_ex)
#define MD_IOC_DO_MEMCPY_TRANSACTION  _IOW('k', 21, int)
#define MD_IOC_DO_ABILITY_PROBE       _IOR('k', 22, int)

#define MD_MODE_MASK 0x30   /* 0: sequential to sequential */
#define MD_CONST     BIT(6) /* the source word is the value to fill with */
#define MD_LEN_MASK  0x3fffffff
#define MD_MAX_LEN   (256U << 20)

struct md_copy_pages {
    u32 op, dst, src, length; /* op 0: copy, 1: fill */
};
struct md_write_ex {
    u32 virt_dst, virt_src, length;
};
struct md_transaction {
    u32 dst, src, len, flags; /* flags bit 0: fill, bit 1: backwards */
};

static u64 md_count;
static DEFINE_MUTEX(md_lock);

static int md_fill(u64 dst, u32 len, u32 value, unsigned int width)
{
    u8 *buf = kmalloc(PAGE_SIZE, GFP_KERNEL);
    int ret = 0;

    if (!buf)
        return -ENOMEM;
    for (size_t i = 0; i < PAGE_SIZE; i += width)
        memcpy(buf + i, &value, width);
    for (u32 done = 0; done < len && !ret; done += PAGE_SIZE) {
        ret = rtk_store_access(dst + done, buf, min_t(u32, len - done, PAGE_SIZE), true);
        cond_resched();
    }
    kfree(buf);
    return ret;
}

static int md_move(u64 dst, u64 src, u32 len)
{
    u8 *buf = kmalloc(PAGE_SIZE, GFP_KERNEL);
    bool back = dst > src && dst < src + len;
    int ret = 0;

    if (!buf)
        return -ENOMEM;
    for (u32 done = 0; done < len && !ret;) {
        u32 n = min_t(u32, len - done, PAGE_SIZE);
        u64 off = back ? len - done - n : done;

        ret = rtk_store_access(src + off, buf, n, false);
        if (!ret)
            ret = rtk_store_access(dst + off, buf, n, true);
        done += n;
        cond_resched();
    }
    kfree(buf);
    return ret;
}

/* one engine command: the cmd word, dst, src, length */
static int md_command(const u32 *w)
{
    u32 len = w[3] & MD_LEN_MASK;
    unsigned int width = (w[0] & 0xf) == 0xd ? 4 : (w[0] & 0xf) == 0x7 ? 2 : 1;
    int ret = 0;

    if ((w[0] & MD_MODE_MASK) || len > MD_MAX_LEN)
        pr_info_ratelimited("md0: command %#x (%u bytes) not emulated\n", w[0], len);
    else if (w[0] & MD_CONST)
        ret = md_fill(w[1], len, w[2], width);
    else
        ret = md_move(w[1], w[2], len);
    md_count++;
    return ret;
}

static int md_commands(const u32 __user *ucmds, size_t len)
{
    int ret = 0;

    mutex_lock(&md_lock);
    for (size_t off = 0; off + 16 <= len && !ret; off += 16) {
        u32 w[4];

        ret =
            copy_from_user(w, (const u8 __user *)ucmds + off, sizeof(w)) ? -EFAULT : md_command(w);
    }
    mutex_unlock(&md_lock);
    return ret;
}

static ssize_t md_write(struct file *file, const char __user *buf, size_t n, loff_t *pos)
{
    int ret = md_commands((const u32 __user *)buf, n);

    return ret ? ret : n;
}

static long md_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *uarg = (void __user *)arg;
    u64 c;
    int ret;

    switch (cmd) {
    case MD_IOC_READ_INST_COUNT:
    case MD_IOC_READ_ISSUED_INST_COUNT:
        mutex_lock(&md_lock);
        c = md_count;
        mutex_unlock(&md_lock);
        return copy_to_user(uarg, &c, sizeof(c)) ? -EFAULT : 0;
    case MD_IOC_WRITE_ISSUE_CMD: { /* u32 length, then the commands */
        u32 len;

        if (get_user(len, (u32 __user *)uarg) || !len)
            return -EFAULT;
        return md_commands((const u32 __user *)(uarg + 4), len);
    }
    case MD_IOC_DO_MEMCPY_TRANSACTION: {
        struct md_transaction t;
        u32 w[4];

        if (copy_from_user(&t, uarg, sizeof(t)))
            return -EFAULT;
        if (!t.len)
            return -EINVAL;
        w[0] = 0x05 | (t.flags & 1 ? MD_CONST : 0);
        w[1] = t.dst;
        w[2] = t.src;
        w[3] = t.len;
        mutex_lock(&md_lock);
        ret = md_command(w);
        mutex_unlock(&md_lock);
        return ret;
    }
    case MD_IOC_COPY_PAGES: {
        struct md_copy_pages p;
        u32 w[4];

        if (copy_from_user(&p, uarg, sizeof(p)))
            return -EFAULT;
        if (!p.length)
            return -EINVAL;
        w[0] = 0x05 | (p.op ? MD_CONST : 0);
        w[1] = p.dst;
        w[2] = p.src;
        w[3] = p.length;
        mutex_lock(&md_lock);
        ret = md_command(w);
        mutex_unlock(&md_lock);
        return ret;
    }
    case MD_IOC_DO_ABILITY_PROBE: {
        int yes = 1;

        return copy_to_user(uarg, &yes, sizeof(yes)) ? -EFAULT : 0;
    }
    case MD_IOC_WAIT_FOR_COMPLETE:
        return 0;
    }
    return rtk_unhandled("md0", cmd, -ENOTTY); /* WRITE_EX_CMD (user addresses): not yet seen */
}

int rtk_memory_params_check(void)
{
    if (!rbus_phys || !rbus_size || rbus_phys % PAGE_SIZE || rbus_size % PAGE_SIZE || !pli_base ||
        pli_max < pli_base || !pli_step || !pli_window || !pli_rbus || pli_base % PAGE_SIZE ||
        pli_step % PAGE_SIZE || pli_window % PAGE_SIZE || pli_uncached % PAGE_SIZE ||
        pli_pad % PAGE_SIZE || pli_rbus % PAGE_SIZE || pli_uncached < pli_window ||
        pli_rbus < pli_uncached + pli_window || (u64)pli_max + pli_rbus + rbus_size > 0xffff0000ULL)
        return -EINVAL;
    return 0;
}

const struct file_operations rtk_auth_fops = {
    .owner = THIS_MODULE,
    .open = auth_open,
    .release = auth_release,
    .read = rtk_zero_read,
    .write = zero_write,
    .unlocked_ioctl = auth_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};
const struct file_operations rtk_rtkmem_fops = {
    .owner = THIS_MODULE,
    .open = rtkmem_open,
    .release = rtkmem_release,
    .mmap = rtkmem_mmap,
    .unlocked_ioctl = rtkmem_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
    .fop_flags = FOP_UNSIGNED_OFFSET,
};
const struct file_operations rtk_md_fops = {
    .owner = THIS_MODULE,
    .open = rtkmem_open,
    .write = md_write,
    .unlocked_ioctl = md_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};

int rtk_store_file_create(void)
{
    store_file = anon_inode_getfile("[rtk-pa]", &store_fops, NULL, O_RDWR);
    if (IS_ERR(store_file)) {
        int err = PTR_ERR(store_file);
        store_file = NULL;
        return err;
    }
    return 0;
}

void rtk_store_file_close(void)
{
    if (store_file)
        fput(store_file);
    store_file = NULL;
}

void rtk_memory_cleanup(void)
{
    for (int i = 0; i < 2; i++) {
        bitmap_free(dcus[i].bitmap);
        dcus[i].bitmap = NULL;
    }
    lgemu_store_free(&pa_store);
}

/* Generic node allocations use the same system-owned DCU blocks as before. */
long rtk_node_memory_ioctl(int kind, unsigned long arg)
{
    struct block *b;

    if (kind == 1) {
        b = block_alloc(NULL, PAGE_ALIGN(arg), 0, false);
        return b ? b->pa : -ENOMEM;
    }
    b = block_take(NULL, arg, 0);
    if (!b)
        return -EINVAL;
    kfree(b);
    return 0;
}

void rtk_memory_log(int nodes_count)
{
    pr_info("RBUS %#x+%#x, DCU1 %#x+%#x, DCU2 %#x+%#x, %d other nodes\n", rbus_phys, rbus_size,
            dcu1_base, dcu1_size, dcu2_base, dcu2_size, nodes_count);
}
