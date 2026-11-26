// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* What the SoC vendors' driver modules share (lgemu-drv.h has the interface): the page store,
 * the counts, the parameters' parsing, the character devices and the GPIO chip. No state of its
 * own: each table and store is its module's. Messages carry the calling module's name, as its own
 * would. */
#define pr_fmt(fmt) fmt
#include <linux/compat.h>
#include <linux/gfp.h>
#include <linux/kernel.h>
#include <linux/ratelimit.h>
#include <linux/sched/task_stack.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/usb.h>
#ifdef CONFIG_ARM64
#include <asm/traps.h>
#endif
#include "lgemu-drv.h"

/* ---- the page store ---- */

static struct page *store_page(struct lgemu_store *s, unsigned long index)
{
    struct page *page = xa_load(&s->pages, index), *old;

    if (page)
        return page;
    page = alloc_page(GFP_KERNEL | __GFP_ZERO);
    if (!page)
        return NULL;
    old = xa_cmpxchg(&s->pages, index, NULL, page, GFP_KERNEL);
    if (old) {
        __free_page(page);
        return xa_is_err(old) ? NULL : old;
    }
    return page;
}

void *lgemu_store_ptr(struct lgemu_store *s, u64 addr)
{
    struct page *page = store_page(s, addr >> PAGE_SHIFT);

    return page ? page_address(page) + offset_in_page(addr) : NULL;
}
EXPORT_SYMBOL_GPL(lgemu_store_ptr);

#ifdef CONFIG_ARM64
/* Narrow A32 scalar decoder. No Thumb, conditional instructions, writeback,
 * signed loads, PC operands, or multi/exclusive accesses. Rejection precedes
 * every device side effect. Architectural completion uses the kernel helper,
 * including debugger single-step bookkeeping. */
static vm_fault_t store_mmio_fault(struct lgemu_store *s, struct vm_fault *vmf)
{
    struct pt_regs *r = task_pt_regs(current);
    u32 insn = 0, offset, addr, value, rn, rd, rm;
    unsigned int width;
    bool write;
    u64 pa;

    if (!(vmf->flags & FAULT_FLAG_USER) ||
        (vmf->flags & (FAULT_FLAG_INSTRUCTION | FAULT_FLAG_REMOTE)) ||
        vmf->vma->vm_mm != current->mm || !user_mode(r) || !compat_user_mode(r) ||
        (r->pstate & (PSR_AA32_T_BIT | PSR_AA32_E_BIT)) || (r->pc & 3) ||
        copy_from_user_nofault(&insn, (void __user *)(unsigned long)r->pc, sizeof(insn)))
        goto unsupported;
    if ((insn >> 28) != 14 || !(insn & BIT(24)) || (insn & BIT(21)))
        goto unsupported;
    rn = (insn >> 16) & 15;
    rd = (insn >> 12) & 15;
    if (rn == 15 || rd == 15)
        goto unsupported;
    write = !(insn & BIT(20));
    if ((insn & 0x0c000000) == 0x04000000) {
        width = (insn & BIT(22)) ? 1 : 4;
        if (insn & BIT(25)) {
            /* Register offset, LSL immediate only. */
            if (insn & 0x70)
                goto unsupported;
            rm = insn & 15;
            if (rm == 15)
                goto unsupported;
            offset = (u32)r->regs[rm] << ((insn >> 7) & 31);
        } else {
            offset = insn & 0xfff;
        }
    } else if ((insn & 0x0e0000f0) == 0x000000b0) {
        width = 2;
        if (insn & BIT(22)) {
            offset = ((insn >> 4) & 0xf0) | (insn & 15);
        } else {
            rm = insn & 15;
            if (rm == 15 || (insn & 0xf00))
                goto unsupported;
            offset = r->regs[rm];
        }
    } else {
        goto unsupported;
    }
    addr = (u32)r->regs[rn] + ((insn & BIT(23)) ? offset : -offset);
    if (addr != vmf->real_address || (addr & (width - 1)) ||
        write != !!(vmf->flags & FAULT_FLAG_WRITE) || addr < vmf->vma->vm_start ||
        (u64)addr + width > vmf->vma->vm_end)
        goto unsupported;
    pa = ((u64)vmf->pgoff << PAGE_SHIFT) + offset_in_page(addr);
    if (offset_in_page(addr) + width > PAGE_SIZE)
        goto unsupported;
    value = write ? (u32)r->regs[rd] : 0;
    if (width < 4)
        value &= (1U << (width * 8)) - 1;
    if (!s->access || s->access(pa, width, write, &value))
        goto unsupported;
    if (!write)
        r->regs[rd] = width == 4 ? value : value & ((1U << (width * 8)) - 1);
    arm64_skip_faulting_instruction(r, 4);
    return VM_FAULT_NOPAGE;
unsupported:
    pr_warn_ratelimited(
        "lgemu: unsupported MMIO pid=%d pc=%llx address=%lx instruction=%08x flags=%x\n",
        task_pid_nr(current), r->pc, vmf->real_address, insn, vmf->flags);
    return VM_FAULT_SIGBUS;
}

#else
/* The A32 decoder needs the faulting task's AArch32 registers: arm64 only. */
static vm_fault_t store_mmio_fault(struct lgemu_store *s, struct vm_fault *vmf)
{
    pr_warn_ratelimited("lgemu: unsupported MMIO pid=%d address=%lx (no A32 decoder here)\n",
                        task_pid_nr(current), vmf->real_address);
    return VM_FAULT_SIGBUS;
}
#endif

static vm_fault_t store_fault(struct vm_fault *vmf)
{
    struct lgemu_store *s = vmf->vma->vm_private_data;
    struct page *page;

    if (s->trap_page && s->trap_page((u64)vmf->pgoff << PAGE_SHIFT))
        return store_mmio_fault(s, vmf);
    page = store_page(s, vmf->pgoff);

    if (!page)
        return VM_FAULT_OOM;
    return vmf_insert_pfn(vmf->vma, vmf->address, page_to_pfn(page));
}

static void store_vm_open(struct vm_area_struct *vma)
{
    __module_get(((struct lgemu_store *)vma->vm_private_data)->owner);
}

static void store_vm_close(struct vm_area_struct *vma)
{
    module_put(((struct lgemu_store *)vma->vm_private_data)->owner);
}

static const struct vm_operations_struct store_vm_ops = {
    .open = store_vm_open,
    .close = store_vm_close,
    .fault = store_fault,
};

int lgemu_store_mmap(struct lgemu_store *s, struct vm_area_struct *vma, u64 addr)
{
    if (!(vma->vm_flags & VM_SHARED))
        return -EINVAL;
    vma->vm_pgoff = addr >> PAGE_SHIFT;
    vma->vm_private_data = s;
    vma->vm_ops = &store_vm_ops;
    vm_flags_set(vma, VM_PFNMAP | VM_DONTEXPAND | VM_DONTDUMP);
    __module_get(s->owner);
    return 0;
}
EXPORT_SYMBOL_GPL(lgemu_store_mmap);

void lgemu_store_free(struct lgemu_store *s)
{
    struct page *page;
    unsigned long index;

    xa_for_each(&s->pages, index, page) __free_page(page);
    xa_destroy(&s->pages);
}
EXPORT_SYMBOL_GPL(lgemu_store_free);

/* ---- counts ---- */

static bool count_used(const struct lgemu_count *e)
{
    return e->count || e->marked;
}

/* the entry of (what, a, b), made on first use; NULL when the table is full. Under c->lock. */
static struct lgemu_count *count_entry(struct lgemu_counts *c, const char *what, u32 a, u32 b)
{
    for (size_t i = 0; i < c->size; i++) {
        struct lgemu_count *e = &c->e[i];

        if (!count_used(e)) {
            *e = (struct lgemu_count){.what = what, .a = a, .b = b};
            return e;
        }
        if (e->a == a && e->b == b &&
            (e->what == what || (e->what && what && !strcmp(e->what, what))))
            return e;
    }
    return NULL;
}

static void count_trace(struct lgemu_counts *c, const char *what, u32 a, u32 b)
{
    static DEFINE_RATELIMIT_STATE(rs, HZ, 1);
    struct pt_regs *regs = task_pt_regs(current);
    u32 word = 0;

    if (!__ratelimit(&rs))
        return;
#ifdef CONFIG_ARM64
    if (compat_user_mode(regs) && get_user(word, (u32 __user *)compat_ptr(regs->compat_sp + 8)))
        word = 0;
    pr_info("%s: trace %s %#x:%u: %s[%d] pc %#llx lr %#llx [sp+8] %#x\n", c->module,
            what ? what : "-", a, b, current->comm, task_pid_nr(current), regs->pc, regs->compat_lr,
            word);
#else
    pr_info("%s: trace %s %#x:%u: %s[%d] pc %#lx [sp+8] %#x\n", c->module, what ? what : "-", a, b,
            current->comm, task_pid_nr(current), (unsigned long)instruction_pointer(regs), word);
#endif
}

void lgemu_count(struct lgemu_counts *c, const char *what, u32 a, u32 b)
{
    const struct lgemu_trace *t = c->trace;
    struct lgemu_count *e;

    if (t && READ_ONCE(t->on) && a == t->a && b == t->b)
        count_trace(c, what, a, b);
    spin_lock(&c->lock);
    e = count_entry(c, what, a, b);
    if (e)
        e->count++;
    spin_unlock(&c->lock);
}
EXPORT_SYMBOL_GPL(lgemu_count);

long lgemu_ioctl_unhandled(struct lgemu_counts *c, const char *dev, unsigned int cmd, long ret)
{
    struct lgemu_count *e;
    bool first;

    spin_lock(&c->lock);
    e = count_entry(c, dev, cmd, 0);
    first = e && !e->marked;
    if (e)
        e->marked = true;
    spin_unlock(&c->lock);
    if (first)
        pr_info("%s: %s: ioctl %#x ('%c' %u, %u bytes) not emulated: %ld, first from %s[%d]\n",
                c->module, dev, cmd, _IOC_TYPE(cmd), _IOC_NR(cmd), _IOC_SIZE(cmd), ret,
                current->comm, task_tgid_nr(current));
    else if (!e)
        pr_info_ratelimited("%s: %s: ioctl %#x not emulated: %ld (count table full)\n", c->module,
                            dev, cmd, ret);
    return ret;
}
EXPORT_SYMBOL_GPL(lgemu_ioctl_unhandled);

ssize_t lgemu_counts_read(struct lgemu_counts *c, char __user *buf, size_t n, loff_t *pos)
{
    size_t size = c->size * c->line, len = 0;
    char *text = kmalloc(size, GFP_KERNEL);
    ssize_t ret;

    if (!text)
        return -ENOMEM;
    spin_lock(&c->lock);
    for (size_t i = 0; i < c->size && count_used(&c->e[i]); i++)
        len += c->show(text + len, size - len, &c->e[i]);
    spin_unlock(&c->lock);
    ret = simple_read_from_buffer(buf, n, pos, text, len);
    kfree(text);
    return ret;
}
EXPORT_SYMBOL_GPL(lgemu_counts_read);

static ssize_t counts_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    return lgemu_counts_read(pde_data(file_inode(file)), buf, n, pos);
}

const struct proc_ops lgemu_counts_proc_ops = {
    .proc_read = counts_read,
    .proc_lseek = default_llseek,
};
EXPORT_SYMBOL_GPL(lgemu_counts_proc_ops);

/* ---- parameters ---- */

/* the fields of rest (strsep's), exactly n numbers */
static int parse_rest(char *rest, unsigned int base, u32 *out, int n)
{
    for (int i = 0; i < n; i++) {
        char *field = strsep(&rest, ":");

        if (!field || kstrtou32(field, base, &out[i]))
            return -EINVAL;
    }
    return rest ? -EINVAL : 0;
}

int lgemu_parse_u32s(const char *spec, unsigned int base, u32 *out, int n)
{
    char *copy = kstrdup(spec, GFP_KERNEL);
    int ret;

    if (!copy)
        return -ENOMEM;
    ret = parse_rest(copy, base, out, n);
    kfree(copy);
    return ret;
}
EXPORT_SYMBOL_GPL(lgemu_parse_u32s);

int lgemu_parse_named(const char *spec, char *name, size_t name_size, unsigned int base, u32 *out,
                      int n)
{
    char *copy = kstrdup(spec, GFP_KERNEL), *rest = copy, *field;
    int ret = -EINVAL;

    if (!copy)
        return -ENOMEM;
    field = strsep(&rest, ":");
    if (*field && strscpy(name, field, name_size) > 0 && rest)
        ret = parse_rest(rest, base, out, n);
    kfree(copy);
    return ret;
}
EXPORT_SYMBOL_GPL(lgemu_parse_named);

/* ---- character devices ---- */

int lgemu_cdev_add(struct lgemu_cdev *c, unsigned int major, unsigned int minor, unsigned int count,
                   const char *name, const struct file_operations *fops)
{
    int err;

    if (major) {
        c->dev = MKDEV(major, minor);
        err = register_chrdev_region(c->dev, count, name);
    } else {
        err = alloc_chrdev_region(&c->dev, minor, count, name);
    }
    if (err)
        return err;
    cdev_init(&c->cdev, fops);
    c->cdev.owner = fops->owner;
    err = cdev_add(&c->cdev, c->dev, count);
    if (err) {
        unregister_chrdev_region(c->dev, count);
        return err;
    }
    c->count = count;
    c->added = true;
    return 0;
}
EXPORT_SYMBOL_GPL(lgemu_cdev_add);

void lgemu_cdev_del(struct lgemu_cdev *c)
{
    if (!c->added)
        return;
    cdev_del(&c->cdev);
    unregister_chrdev_region(c->dev, c->count);
    c->added = false;
}
EXPORT_SYMBOL_GPL(lgemu_cdev_del);

/* ---- GPIO lines ---- */

static struct lgemu_gpio_line *gpio_line(struct gpio_chip *gc, unsigned int n)
{
    return &((struct lgemu_gpiochip *)gpiochip_get_data(gc))->line[n];
}

static int line_get_direction(struct gpio_chip *gc, unsigned int n)
{
    return READ_ONCE(gpio_line(gc, n)->is_out) ? GPIO_LINE_DIRECTION_OUT : GPIO_LINE_DIRECTION_IN;
}

/* ---- the USB ports' power (lgemu_gpiochip_usb_vbus) ---- */

/* the root hubs' driver (usbcore's hub), kept from the first unbinding to bind them again */
static const struct device_driver *vbus_hub_driver;

/* whether the ports have power: no switches, or one of them driving 1 */
static bool vbus_powered(struct lgemu_gpiochip *g)
{
    if (!READ_ONCE(g->nvbus))
        return true;
    for (int i = 0; i < g->nvbus; i++) {
        struct lgemu_gpio_line *l = &g->line[g->vbus[i]];

        if (READ_ONCE(l->is_out) && READ_ONCE(l->out))
            return true;
    }
    return false;
}

static bool is_vbus(struct lgemu_gpiochip *g, unsigned int n)
{
    for (int i = 0; i < READ_ONCE(g->nvbus); i++)
        if (g->vbus[i] == n)
            return true;
    return false;
}

struct root_hubs {
    struct usb_device *hub[8];
    int n;
};

static int collect_root_hub(struct usb_device *udev, void *data)
{
    struct root_hubs *r = data;

    if (!udev->parent && r->n < ARRAY_SIZE(r->hub))
        r->hub[r->n++] = usb_get_dev(udev);
    return 0;
}

/* Brings every root hub's driver in line with the switches: unbound without power (its ports'
 * devices disconnected), bound with it (hub_probe enumerates what the ports hold). The driver
 * core takes the root hub's lock and the interface's. */
static void vbus_apply(struct lgemu_gpiochip *g)
{
    bool on = vbus_powered(g);
    struct root_hubs r = {.n = 0};

    usb_for_each_dev(&r, collect_root_hub);
    for (int i = 0; i < r.n; i++) {
        struct usb_device *udev = r.hub[i];
        struct usb_interface *intf = udev->actconfig ? udev->actconfig->interface[0] : NULL;

        if (intf && on && !intf->dev.driver && vbus_hub_driver) {
            if (device_driver_attach(vbus_hub_driver, &intf->dev) == 0)
                pr_info("%s: USB VBUS on: %s's ports powered\n", g->chip.label,
                        dev_name(&udev->dev));
        } else if (intf && !on && intf->dev.driver) {
            vbus_hub_driver = intf->dev.driver;
            device_release_driver(&intf->dev);
            pr_info("%s: USB VBUS off: %s's ports without power\n", g->chip.label,
                    dev_name(&udev->dev));
        }
        usb_put_dev(udev);
    }
}

static void vbus_work(struct work_struct *w)
{
    vbus_apply(container_of(w, struct lgemu_gpiochip, vbus_work));
}

static void vbus_changed(struct gpio_chip *gc, unsigned int n)
{
    struct lgemu_gpiochip *g = gpiochip_get_data(gc);

    if (is_vbus(g, n))
        schedule_work(&g->vbus_work);
}

int lgemu_gpiochip_usb_vbus(struct lgemu_gpiochip *g, const unsigned int *lines, int n)
{
    if (n <= 0)
        return 0;
    if (!g->added || n > ARRAY_SIZE(g->vbus))
        return -EINVAL;
    for (int i = 0; i < n; i++)
        if (lines[i] >= g->chip.ngpio)
            return -EINVAL;
    memcpy(g->vbus, lines, n * sizeof(*lines));
    WRITE_ONCE(g->nvbus, n);
    vbus_apply(g);
    return 0;
}
EXPORT_SYMBOL_GPL(lgemu_gpiochip_usb_vbus);

int lgemu_gpiochip_tach(struct lgemu_gpiochip *g, const unsigned int *lines, int n)
{
    if (n <= 0)
        return 0;
    if (!g->added)
        return -EINVAL;
    for (int i = 0; i < n; i++)
        if (lines[i] >= g->chip.ngpio)
            return -EINVAL;
    spin_lock(&g->lock);
    for (int i = 0; i < n; i++)
        g->line[lines[i]].tach = 1;
    spin_unlock(&g->lock);
    return 0;
}
EXPORT_SYMBOL_GPL(lgemu_gpiochip_tach);

static int line_direction_input(struct gpio_chip *gc, unsigned int n)
{
    WRITE_ONCE(gpio_line(gc, n)->is_out, 0);
    vbus_changed(gc, n);
    return 0;
}

static int line_direction_output(struct gpio_chip *gc, unsigned int n, int v)
{
    struct lgemu_gpiochip *g = gpiochip_get_data(gc);

    spin_lock(&g->lock);
    g->line[n].out = !!v;
    g->line[n].is_out = 1;
    spin_unlock(&g->lock);
    vbus_changed(gc, n);
    return 0;
}

static int line_get(struct gpio_chip *gc, unsigned int n)
{
    struct lgemu_gpiochip *g = gpiochip_get_data(gc);
    int v;

    spin_lock(&g->lock);
    if (!g->line[n].is_out && g->line[n].tach)
        g->line[n].level ^= 1; /* a turning fan: each read sees the next edge */
    v = g->line[n].is_out ? g->line[n].out : g->line[n].level;
    spin_unlock(&g->lock);
    return v;
}

static int line_set(struct gpio_chip *gc, unsigned int n, int v)
{
    WRITE_ONCE(gpio_line(gc, n)->out, !!v);
    vbus_changed(gc, n);
    return 0;
}

int __lgemu_gpiochip_add(struct lgemu_gpiochip *g, struct module *owner, const char *label,
                         unsigned int lines, char *const *levels, int n)
{
    int err;

    if (!lines)
        return 0;
    g->line = kcalloc(lines, sizeof(*g->line), GFP_KERNEL);
    if (!g->line)
        return -ENOMEM;
    for (int i = 0; i < n; i++) {
        u32 v[2];

        if (lgemu_parse_u32s(levels[i], 0, v, 2) || v[0] >= lines) {
            pr_err("%s: gpio_level=%s: not line:level below gpio_lines\n", module_name(owner),
                   levels[i]);
            err = -EINVAL;
            goto fail;
        }
        g->line[v[0]].level = !!v[1];
    }
    spin_lock_init(&g->lock);
    INIT_WORK(&g->vbus_work, vbus_work);
    g->nvbus = 0;
    g->chip = (struct gpio_chip){
        .label = label,
        .owner = owner,
        .base = 0,
        .ngpio = lines,
        .get_direction = line_get_direction,
        .direction_input = line_direction_input,
        .direction_output = line_direction_output,
        .get = line_get,
        .set = line_set,
    };
    err = gpiochip_add_data(&g->chip, g);
    if (err)
        goto fail;
    g->added = true;
    return 0;
fail:
    kfree(g->line);
    g->line = NULL;
    return err;
}
EXPORT_SYMBOL_GPL(__lgemu_gpiochip_add);

void lgemu_gpiochip_remove(struct lgemu_gpiochip *g)
{
    if (g->added && g->nvbus) {
        WRITE_ONCE(g->nvbus, 0);
        cancel_work_sync(&g->vbus_work);
        vbus_apply(g);
    }
    if (g->added)
        gpiochip_remove(&g->chip);
    g->added = false;
    kfree(g->line);
    g->line = NULL;
}
EXPORT_SYMBOL_GPL(lgemu_gpiochip_remove);
