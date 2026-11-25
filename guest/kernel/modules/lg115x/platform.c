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
#include "lgemu-drv.h"
#include "lgemu-i2c.h"

#include "lg115x.h"

/* ---- the GPIO pins: /dev/lg/gpio0 ---- */

#define GPIO_MAJOR     152
#define GPIO_IOC_MAGIC 'g'
#define GPIO_NR_SET    0  /* GPIO_IOW_COMMAND_SET */
#define GPIO_NR_GET    1  /* GPIO_IORW_COMMAND_GET */
#define GPIO_NR_EX_SET 10 /* the ACE's pins: GPIO_IOW_EX_COMMAND_SET */
#define GPIO_NR_EX_GET 11
#define GPIO_EX_PINS   16 /* max_ex_num, every chip's */
#define GPIO_PINS_MAX  256

/* LX_GPIO_COMMAND_T */
enum {
    GPIO_PIN_MUX,
    GPIO_MODE,
    GPIO_VALUE,
    GPIO_INTR_VALUE,
    GPIO_ISR,
    GPIO_INTR_ARRAY_NUM,
    GPIO_INTR_COUNT
};

/* LX_GPIO_PARAM_T from a 32-bit process: these five words, then the callback (H13's, 24 bytes),
 * or an info pointer, the callback and an interrupt count (M14's and M16P's, 36 bytes) */
struct lx_gpio_param32 {
    u32 command, pin, data, value, intr_enable;
    u32 rest[5];
};

static unsigned int gpio_pins;
module_param(gpio_pins, uint, 0444);
MODULE_PARM_DESC(gpio_pins,
                 "the SoC's GPIO pins, max_num of LG's gpio_core.c (H13 and M17 144, M14 136); "
                 "0: no /dev/lg/gpio0");

/* what each pin was set to: its pin mux (as GPIO), mode (LX_GPIO_MODE_T) and output value */
static struct {
    u8 mux, mode, value;
} gpio_state[GPIO_PINS_MAX + GPIO_EX_PINS];
static DEFINE_MUTEX(gpio_lock);
static unsigned long gpio_seen[(GPIO_PINS_MAX + GPIO_EX_PINS) * 2 / BITS_PER_LONG + 1];

/* One command of LG's GPIO_Ioctl: the pin's state is what was set (an input reads the value last
 * written to it, 0 before: nothing on the board drives a pin); no pin interrupts. */
static long gpio_command(struct lx_gpio_param32 *p, bool get, bool ex)
{
    unsigned int max = ex ? GPIO_EX_PINS : gpio_pins, pin = p->pin + (ex ? GPIO_PINS_MAX : 0);

    /* the commands each ioctl takes (M16P's; H13's has no interrupt array or count) */
    u32 known = ex    ? BIT(GPIO_PIN_MUX) | BIT(GPIO_MODE) | BIT(GPIO_VALUE)
                : get ? BIT(GPIO_PIN_MUX) | BIT(GPIO_MODE) | BIT(GPIO_VALUE) |
                            BIT(GPIO_INTR_VALUE) | BIT(GPIO_INTR_COUNT)
                      : BIT(GPIO_PIN_MUX) | BIT(GPIO_MODE) | BIT(GPIO_VALUE) | BIT(GPIO_ISR) |
                            BIT(GPIO_INTR_ARRAY_NUM);

    if (p->command > GPIO_INTR_COUNT || !(known & BIT(p->command)))
        return -EFAULT; /* "unknown", as the stock */
    if (p->command == GPIO_INTR_VALUE || p->command == GPIO_INTR_ARRAY_NUM)
        return 0; /* no pin has interrupted: port and value as given */
    if (p->pin >= max)
        return -EPERM; /* the stock's -1 */
    if (p->command == GPIO_ISR || p->command == GPIO_INTR_COUNT) {
        /* an interrupt (a callback, or a count of edges): the pin is muxed as GPIO and never fires
         */
        gpio_state[pin].mux = 1;
        return 0;
    }
    if (get) {
        if (!test_and_set_bit(pin * 2 + (p->command == GPIO_VALUE), gpio_seen))
            pr_info("gpio: %s%u %s read (%s)\n", ex ? "ex " : "", p->pin,
                    p->command == GPIO_VALUE  ? "value"
                    : p->command == GPIO_MODE ? "mode"
                                              : "pin mux",
                    current->comm);
        p->data = p->command == GPIO_PIN_MUX ? gpio_state[pin].mux
                  : p->command == GPIO_MODE  ? gpio_state[pin].mode
                                             : gpio_state[pin].value;
    } else if (p->command == GPIO_PIN_MUX) {
        gpio_state[pin].mux = !!p->data;
    } else if (p->command == GPIO_MODE) {
        gpio_state[pin].mode = p->data;
    } else {
        gpio_state[pin].value = !!p->data;
    }
    return 0;
}

static long gpio_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *uarg = compat_ptr(arg);
    unsigned int nr = _IOC_NR(cmd), size = _IOC_SIZE(cmd);
    struct lx_gpio_param32 p = {0};
    bool get = nr == GPIO_NR_GET || nr == GPIO_NR_EX_GET;
    long ret;

    /* the stock checks the magic and the number: H13's GET commands are _IOW, M16P's _IOWR */
    if (_IOC_TYPE(cmd) != GPIO_IOC_MAGIC || (nr != GPIO_NR_SET && nr != GPIO_NR_EX_SET && !get) ||
        size < offsetof(struct lx_gpio_param32, rest) || size > sizeof(p))
        return -ENOTTY;
    if (copy_from_user(&p, uarg, size))
        return -EFAULT;
    mutex_lock(&gpio_lock);
    ret = gpio_command(&p, get, nr >= GPIO_NR_EX_SET);
    mutex_unlock(&gpio_lock);
    if (!ret && get && copy_to_user(uarg, &p, size))
        ret = -EFAULT;
    return ret;
}

static int gpio_open(struct inode *inode, struct file *file)
{
    return 0;
}

/* the pin interrupts' poll: none comes */
static __poll_t gpio_poll(struct file *file, struct poll_table_struct *pt)
{
    return 0;
}

static const struct file_operations gpio_fops = {
    .owner = THIS_MODULE,
    .open = gpio_open,
    .poll = gpio_poll,
    .unlocked_ioctl = gpio_ioctl,
    .compat_ioctl = gpio_ioctl,
};

static struct lgemu_cdev gpio_cdev;

void lx_gpio_remove(void)
{
    lgemu_cdev_del(&gpio_cdev);
}

int lx_gpio_add(void)
{
    int err;

    if (!gpio_pins)
        return 0;
    if (gpio_pins > GPIO_PINS_MAX)
        return -EINVAL;
    err = lgemu_cdev_add(&gpio_cdev, GPIO_MAJOR, 0, 1, "gpio", &gpio_fops);
    if (!err)
        pr_info("gpio: gpio0, %u pins and %u of the ACE's, nothing connected\n", gpio_pins,
                GPIO_EX_PINS);
    return err;
}

/* ---- the SoC's GPIO lines for /sys/class/gpio: lgemu's chip ---- */

/* Later LX chips' (O22) libdile_gpio drives its pins through /sys/class/gpio by its own pin table
 * (gGPIMapTable, gGPOMapTable), not through /dev/lg/gpio0. Unset: no chip, every export fails. */
static struct lgemu_gpiochip sysfs_gpio_chip;
static unsigned int gpio_lines;
module_param(gpio_lines, uint, 0444);
MODULE_PARM_DESC(gpio_lines,
                 "the GPIO lines of /sys/class/gpio, numbered from 0 (0: no GPIO chip)");
static char *gpio_level[32];
static int gpio_level_count;
module_param_array(gpio_level, charp, &gpio_level_count, 0444);
MODULE_PARM_DESC(gpio_level, "inputs whose level is not 0, line:level (the board with nothing "
                             "connected)");

void lx_sysfs_gpio_remove(void)
{
    lgemu_gpiochip_remove(&sysfs_gpio_chip);
}

int lx_sysfs_gpio_add(void)
{
    return lgemu_gpiochip_add(&sysfs_gpio_chip, "lg115x", gpio_lines, gpio_level, gpio_level_count);
}

/* ---- the register driver and the video back end's kdrivers: sys0, be0, afe0, ovi0 ---- */

/* A kdriver whose node only needs answering: a setting (no data back) is accepted, a query
 * answered by the device's own function or refused, each command logged once. */
struct lx_kdev {
    const char *name;
    unsigned int major;
    char magic;
    /* cmd answered (0 or an error), or -ENOIOCTLCMD: not a query the device answers */
    long (*query)(unsigned int cmd, void __user *uarg);
    struct lgemu_cdev cdev;
    struct lx_seen seen;
};

static long kdev_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct lx_kdev *k = file->private_data;
    long ret = -ENOIOCTLCMD;

    if (_IOC_TYPE(cmd) != k->magic)
        return -ENOTTY;
    if (k->query)
        ret = k->query(cmd, compat_ptr(arg));
    if (ret == -ENOIOCTLCMD)
        ret = _IOC_DIR(cmd) & _IOC_READ ? -EPERM : 0;
    return lx_seen_log(&k->seen, k->name, cmd, ret);
}

static int kdev_open(struct inode *inode, struct file *file)
{
    file->private_data = container_of(inode->i_cdev, struct lx_kdev, cdev.cdev);
    return 0;
}

static const struct file_operations kdev_fops = {
    .owner = THIS_MODULE,
    .open = kdev_open,
    .unlocked_ioctl = kdev_ioctl,
    .compat_ioctl = kdev_ioctl,
};

/* sys0: LG's core/sys (sys_drv.c, sys_io.c, sys_kapi.h, the same in the 2014 and 2016 kdrivers),
 * KADP_SYS_*'s access to the SoC's registers by physical address, which the stock maps and reads
 * or writes. No SoC is behind them here: a register reads what was last written to it, 0 before. */
#define SYS_MAJOR     163
#define SYS_ARRAY_MAX (64 * 1024)

struct lx_sys_mem { /* LX_SYS_MEM_T */
    u32 addr, value;
};

struct lx_sys_mem_array32 { /* LX_SYS_MEM_ARRAY_T from a 32-bit process */
    u32 addr, size, data;
};

static struct lgemu_store sys_regs = LGEMU_STORE_INIT(sys_regs);
static DEFINE_MUTEX(sys_lock);

/* size bytes at addr to or from buf */
static int sys_access(u32 addr, u8 *buf, u32 size, bool write)
{
    while (size) {
        u32 n = min_t(u32, size, PAGE_SIZE - offset_in_page(addr));
        u8 *p = lgemu_store_ptr(&sys_regs, addr);

        if (!p)
            return -ENOMEM;
        if (write)
            memcpy(p, buf, n);
        else
            memcpy(buf, p, n);
        addr += n;
        buf += n;
        size -= n;
    }
    return 0;
}

static long sys_query(unsigned int cmd, void __user *uarg)
{
    struct lx_sys_mem m;
    struct lx_sys_mem_array32 a;
    u8 *buf;
    long ret;

    switch (cmd) {
    case _IOW('a', 2, struct lx_sys_mem):  /* WRITE_MEM */
    case _IOWR('a', 3, struct lx_sys_mem): /* READ_MEM */
        if (copy_from_user(&m, uarg, sizeof(m)))
            return -EFAULT;
        mutex_lock(&sys_lock);
        ret = sys_access(m.addr, (u8 *)&m.value, sizeof(m.value), _IOC_NR(cmd) == 2);
        mutex_unlock(&sys_lock);
        if (!ret && _IOC_NR(cmd) == 3 && put_user(m.value, (u32 __user *)(uarg + 4)))
            ret = -EFAULT;
        return ret;
    case _IOW('a', 4, struct lx_sys_mem_array32):  /* WRITE_MEM_ARRAY */
    case _IOWR('a', 5, struct lx_sys_mem_array32): /* READ_MEM_ARRAY */
        if (copy_from_user(&a, uarg, sizeof(a)))
            return -EFAULT;
        if (!a.size || !a.data || a.size > SYS_ARRAY_MAX)
            return -EFAULT;
        buf = kmalloc(a.size, GFP_KERNEL);
        if (!buf)
            return -ENOMEM;
        ret = _IOC_NR(cmd) == 4 && copy_from_user(buf, compat_ptr(a.data), a.size) ? -EFAULT : 0;
        mutex_lock(&sys_lock);
        if (!ret)
            ret = sys_access(a.addr, buf, a.size, _IOC_NR(cmd) == 4);
        mutex_unlock(&sys_lock);
        if (!ret && _IOC_NR(cmd) == 5 && copy_to_user(compat_ptr(a.data), buf, a.size))
            ret = -EFAULT;
        kfree(buf);
        return ret;
    case _IOWR('a', 10, u32[2]): /* DIAGNOSIS: the stock prints its report, answers nothing */
        return 0;
    default:
        return -ENOIOCTLCMD;
    }
}

static struct lx_kdev sys_kdev = {.name = "sys",
                                  .major = SYS_MAJOR,
                                  .magic = 'a',
                                  .query = sys_query,
                                  .seen = LX_SEEN_INIT(sys_kdev.seen)};

/* be0: the back end (LG's core/be: be_drv.c, be_top_drv.c, be_reg.c, led_drv.c, be_kapi.h;
 * 170:0, hdp0 shares it), the BE firmware's FRC, local dimming, HDR, WCG and PWM. Its numbers
 * moved with each kdriver (H13, M14 and M16P's KADPs each have their own), so the two queries
 * answered are the parameter's: GET_FIRMWARE_INFO_ADDR answers no shared firmware block (address
 * and size 0, the GPL driver's answer without a chip function; KADP_BE_Initialize then maps
 * nothing), and REG_ACCESS reads a BE register as last written (0 before: no firmware). With
 * be_led, LED_GET_REG_INFO, at the number be_led gives (it moved too), answers the chip's LED
 * register block (base and offsets, constants of the stock). Every setting (the firmware download,
 * FRC, LED, PWM, PQ) is accepted. */
#define BE_MAJOR     170
#define BE_LED_WORDS 14

struct lx_be_reg { /* BE_REG_RW_T */
    u32 is_read, type, addr, data;
};

struct lx_be_fw_info { /* BE_FIRMWARE_INFO_T */
    u32 addr, size;
};

static char *be;
module_param(be, charp, 0444);
MODULE_PARM_DESC(
    be, "the BE's ABI, fwinfo:reg: BE_IOR_GET_FIRMWARE_INFO_ADDR's and BE_IORW_REG_ACCESS's "
        "numbers (H13 21:19, M14 22:20, M16P 24:22; fwinfo 0 where the kdrivers have none, M19 "
        "0:16); unset: be0 answers no query");
static char *be_led;
module_param(be_led, charp, 0444);
MODULE_PARM_DESC(
    be_led, "BE_IOW_LED_GET_REG_INFO's number (an _IOR of 56 bytes: M16P 53, M16PP 61, O20 45), "
            "then its LX_BE_LED_REG_INFO_T: the LED block's CPU and MCU bases and the 12 register "
            "offsets; unset: not answered");

static u32 be_fwinfo_nr, be_reg_nr, be_led_nr, be_led_info[BE_LED_WORDS];
static bool be_led_given;
static DEFINE_XARRAY(be_regs); /* index: type << 32 | address */

static long be_query(unsigned int cmd, void __user *uarg)
{
    struct lx_be_fw_info fw = {0};
    struct lx_be_reg r;
    void *v;

    if (be && be_fwinfo_nr && cmd == _IOR('D', be_fwinfo_nr, struct lx_be_fw_info))
        return copy_to_user(uarg, &fw, sizeof(fw)) ? -EFAULT : 0;
    if (be && cmd == _IOWR('D', be_reg_nr, struct lx_be_reg)) {
        if (copy_from_user(&r, uarg, sizeof(r)))
            return -EFAULT;
        if (!r.is_read)
            return xa_err(
                xa_store(&be_regs, (u64)r.type << 32 | r.addr, xa_mk_value(r.data), GFP_KERNEL));
        v = xa_load(&be_regs, (u64)r.type << 32 | r.addr);
        r.data = v ? xa_to_value(v) : 0;
        return copy_to_user(uarg, &r, sizeof(r)) ? -EFAULT : 0;
    }
    if (be_led_given && cmd == _IOR('D', be_led_nr, u32[BE_LED_WORDS]))
        return copy_to_user(uarg, be_led_info, sizeof(be_led_info)) ? -EFAULT : 0;
    return -ENOIOCTLCMD;
}

static struct lx_kdev be_kdev = {.name = "be",
                                 .major = BE_MAJOR,
                                 .magic = 'D',
                                 .query = be_query,
                                 .seen = LX_SEEN_INIT(be_kdev.seen)};

static int lx_be_parse(void)
{
    u32 v[2];

    if (be) {
        if (lgemu_parse_u32s(be, 0, v, 2) || v[0] >= 256 || !v[1] || v[1] >= 256 || v[0] == v[1]) {
            pr_err("be: be is fwinfo:reg, two ioctl numbers (fwinfo 0: none)\n");
            return -EINVAL;
        }
        be_fwinfo_nr = v[0];
        be_reg_nr = v[1];
    }
    if (be_led) {
        u32 led[1 + BE_LED_WORDS];

        if (lgemu_parse_u32s(be_led, 0, led, ARRAY_SIZE(led)) || !led[0] || led[0] >= 256) {
            pr_err("be: be_led is the ioctl number and 14 numbers\n");
            return -EINVAL;
        }
        be_led_nr = led[0];
        memcpy(be_led_info, &led[1], sizeof(be_led_info));
        be_led_given = true;
    }
    return 0;
}

/* afe0: the analog front end (LG's core/afe: the ADC and the CVD), 142:0 */
#define AFE_MAJOR 142

static long afe_query(unsigned int cmd, void __user *uarg)
{
    switch (cmd) {
    case _IOR('a', 63, u32): /* CVD_SET_3DCOMB_PARAM and SET_CVD_BLACK_LEVEL: settings */
    case _IOR('a', 75, u32): /* the header declares _IOR */
        return 0;
    default:
        return -ENOIOCTLCMD;
    }
}

static struct lx_kdev afe_kdev = {.name = "afe",
                                  .major = AFE_MAJOR,
                                  .magic = 'a',
                                  .query = afe_query,
                                  .seen = LX_SEEN_INIT(afe_kdev.seen)};

/* ovi0: the output interface (LG's core/ovi: LVDS/V-by-One and the TCON's gamma, colour
 * temperature, ODC and dithering), 173:0 */
#define OVI_MAJOR 173

static struct lx_kdev ovi_kdev = {
    .name = "ovi", .major = OVI_MAJOR, .magic = 'o', .seen = LX_SEEN_INIT(ovi_kdev.seen)};

static struct lx_kdev *const lx_kdevs[] = {&sys_kdev, &be_kdev, &afe_kdev, &ovi_kdev};

void lx_kdevs_remove(void)
{
    for (size_t i = 0; i < ARRAY_SIZE(lx_kdevs); i++)
        lgemu_cdev_del(&lx_kdevs[i]->cdev);
    lgemu_store_free(&sys_regs);
    xa_destroy(&be_regs);
}

int lx_kdevs_add(void)
{
    int err = lx_be_parse();

    if (err)
        return err;
    for (size_t i = 0; i < ARRAY_SIZE(lx_kdevs); i++) {
        err = lgemu_cdev_add(&lx_kdevs[i]->cdev, lx_kdevs[i]->major, 0, 1, lx_kdevs[i]->name,
                             &kdev_fops);

        if (err) {
            lx_kdevs_remove();
            return err;
        }
    }
    pr_info("sys0 (registers as written), be0 (firmware info at nr %u, registers at nr %u, LED "
            "registers at nr %u), afe0, ovi0\n",
            be_fwinfo_nr, be_reg_nr, be_led_nr);
    return 0;
}

/* ---- the timers: /dev/mem at 0xfd100000 ---- */

/* The SoC's ARM SP804 dual timer (LG's arch/arm/mach-lg115x, resource-lg115x.h TIMER_BASE,
 * time.c): timer 1 the clock events, timer 2 at +0x20 the free-running clocksource and
 * sched_clock, which the kernel starts at boot counting down from 0xffffffff, 32-bit periodic, at
 * the AMBA bus clock (CONFIG_AMBA_CLOCK_MHZ 198 on H13 and M14, lg1154_ and lg1311_defconfig).
 * bootd's Logger::getHWTime (W15M; WT1H's bootd has it but never calls it, W17H's bootd.json does
 * not list m16p) maps the page through /dev/mem, reads timer 2's value (+0x24) and takes ~value /
 * 198000000 as the seconds since boot. Here the page is a /dev/mem window (kernel patch 0007): each
 * mmap gets a page of the registers as they read at that moment, the counter from the kernel's
 * monotonic clock at timer_hz. A reader that keeps its mapping sees the value of its mmap; LG's
 * reader maps, reads one word and unmaps. Timer 1 reads stopped (its interrupts are not
 * modelled). */
static unsigned int timer_hz;
module_param(timer_hz, uint, 0444);
MODULE_PARM_DESC(timer_hz, "the SP804 timers' clock at 0xfd100000 (/dev/mem); 0: no timer page");

#define LX_TIMER_BASE  0xfd100000
#define SP804_LOAD     0x00
#define SP804_VALUE    0x04
#define SP804_CTRL     0x08
#define SP804_BGLOAD   0x18
#define SP804_TIMER2   0x20
#define SP804_CTRL_32  0x02 /* TIMER_CTRL_32BIT | PERIODIC | ENABLE: as time.c sets timer 2 */
#define SP804_CTRL_RUN (SP804_CTRL_32 | 0x40 | 0x80)

static void lx_timer_fill(const struct devmem_window *w, void *page, phys_addr_t pa)
{
    static const u8 ids[8] = {0x04, 0x18, 0x14, 0x00, 0x0d, 0xf0, 0x05, 0xb1}; /* PrimeCell SP804 */
    u32 *reg = page, *t2 = page + SP804_TIMER2;
    u32 ticks = (u32)mul_u64_u32_div(ktime_get_ns(), timer_hz, NSEC_PER_SEC);

    t2[SP804_LOAD / 4] = 0xffffffff;
    t2[SP804_VALUE / 4] = ~ticks;
    t2[SP804_CTRL / 4] = SP804_CTRL_RUN;
    t2[SP804_BGLOAD / 4] = 0xffffffff;
    reg[SP804_CTRL / 4] = SP804_CTRL_32;
    for (int i = 0; i < 8; i++)
        reg[(0xfe0 >> 2) + i] = ids[i];
}

static struct devmem_window lx_timer_window = {
    .base = LX_TIMER_BASE,
    .size = PAGE_SIZE,
    .fill = lx_timer_fill,
};

int lx_timer_add(void)
{
    int err;

    if (!timer_hz)
        return 0;
    err = devmem_window_register(&lx_timer_window);
    if (!err)
        pr_info("timers at %#x (/dev/mem), timer 2 counting at %u Hz\n", LX_TIMER_BASE, timer_hz);
    return err;
}

void lx_timer_remove(void)
{
    if (timer_hz)
        devmem_window_unregister(&lx_timer_window);
}
