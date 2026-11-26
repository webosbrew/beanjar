// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* RBUS register clock, micom UCOM, PWM/GPIO and board attributes. */
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
#include "../../../../include/webos-video.h"

/* Register addresses are physical, or the legacy MIPS KSEG1 form KADP writes (0xb801b6b8 for
 * 0x1801b6b8): both are accepted. */
static char *reg_init[32];
static int reg_init_count;
module_param_array(reg_init, charp, &reg_init_count, 0444);
MODULE_PARM_DESC(reg_init, "registers' power-on values, address:value (32 bits)");
static unsigned int clock90k[2];
static int clock90k_count;
module_param_array(clock90k, uint, &clock90k_count, 0444);
MODULE_PARM_DESC(clock90k, "the 90 kHz system clock's low and high registers (pli_getPTS)");

/* The internal micom (CONFIG_REALTEK_INT_MICOM: an 8051 in the SoC, which LG's MICOM code reaches
 * through KADP_UCOM_* on /dev/sys-misc instead of I2C): the node whose UCOM ioctls go to lgemu.ko's
 * micom (lgemu-i2c.h), the same model the I2C boards' micom is. */
static char *ucom_node;
module_param(ucom_node, charp, 0444);
MODULE_PARM_DESC(ucom_node,
                 "the node (of nodes=) with Realtek's MISC_UCOM ioctls, to lgemu.ko's micom");
static unsigned int ucom_panel_reg = 0xb8061108;
module_param(ucom_panel_reg, uint, 0444);
MODULE_PARM_DESC(ucom_panel_reg,
                 "the register whose bit 5 answers MISC_UCOM_READ 0xD1 (panel control)");

/* Realtek's PWM outputs (drivers/rtk_kdriver/pwm, rtk_pwm_attr.c): the class rtk-pwm with one
 * device per output, named as libhal_pwm looks for them (pwm-I-<n>, pwm-M-<n>), each with the
 * attributes HAL_PWM reads and writes. Nothing drives a backlight here: an attribute keeps what
 * was last written (a number; pos as Realtek prints it). */
static char *pwms[16];
static int pwms_count;
module_param_array(pwms, charp, &pwms_count, 0444);
MODULE_PARM_DESC(pwms, "the PWM outputs of /sys/class/rtk-pwm (pwm-I-0, pwm-M-0, ...)");
/* the frequency an output starts with (Hz): HAL_PWM_SetParam refuses one below 3, and tvservice
 * sets the duty on what HAL_PWM_GetParam read */
static int pwm_freq = 120;
module_param(pwm_freq, int, 0444);
MODULE_PARM_DESC(pwm_freq, "the PWM outputs' frequency at start (Hz)");

/* The SoC's GPIO lines for /sys/class/gpio, lgemu's chip (lgemu-drv.h, lgemu_gpiochip_add) */
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
static unsigned int gpio_tach[8];
static int gpio_tach_count;
module_param_array(gpio_tach, uint, &gpio_tach_count, 0444);
MODULE_PARM_DESC(gpio_tach, "inputs that are fan tachometers, the fans turning "
                            "(lgemu_gpiochip_tach): a projector's");

/* /sys/realtek_boards/system_parameters: the bootloader's system_parameters_N joined (Realtek's
 * mach init.c), which KADP's _load_system_parameter searches for TP<n>_FE= (the frontend boards).
 * Empty by default: a board without those. */
static char *system_parameters = "";
module_param(system_parameters, charp, 0444);
MODULE_PARM_DESC(system_parameters, "the text of /sys/realtek_boards/system_parameters");

static u32 *clock_lo, *clock_hi;
static struct delayed_work clock_work;
static bool clock_running;
static DEFINE_MUTEX(clock_lock);

/* the 90 kHz clock, refreshed every jiffy (a register window in RAM would freeze time); freezable,
 * so a suspend (s2idle), where no reader runs, lets the guest idle */
static void clock_run(struct work_struct *work)
{
    u64 t = mul_u64_u32_div(ktime_get_ns(), 9, 100000);

    WRITE_ONCE(*clock_lo, (u32)t);
    WRITE_ONCE(*clock_hi, (u32)(t >> 32));
    queue_delayed_work(system_freezable_wq, &clock_work, 1);
}

void rtk_clock_start(void)
{
    mutex_lock(&clock_lock);
    if (clock_lo && !clock_running) {
        clock_running = true;
        clock_run(&clock_work.work);
    }
    mutex_unlock(&clock_lock);
}

int rtk_registers_preset(void)
{
    for (int i = 0; i < reg_init_count; i++) {
        u32 v[2], *reg;

        if (lgemu_parse_u32s(reg_init[i], 0, v, 2) || !(reg = rtk_reg_ptr(v[0]))) {
            pr_err("reg_init=%s: not address:value in RBUS\n", reg_init[i]);
            return -EINVAL;
        }
        *reg = v[1];
    }
    if (clock90k_count == 2) {
        clock_lo = rtk_reg_ptr(clock90k[0]);
        clock_hi = rtk_reg_ptr(clock90k[1]);
        if (!clock_lo || !clock_hi) {
            pr_err("clock90k: not two registers in RBUS\n");
            return -EINVAL;
        }
    } else if (clock90k_count) {
        return -EINVAL;
    }
    return 0;
}

/* sys-misc's UCOM (Realtek's drivers/rtk_kdriver/misc.c, misc.h): {u8 Cmd; u16 DataSize; u32
 * pDataBuf} from the 32-bit process. WRITE sends DataSize bytes, the command and its arguments;
 * READ writes the command byte Cmd and reads DataSize bytes back (do_intMicomShareMemory's write
 * then read). The micom's answer is lgemu.ko's, as an I2C micom's: a write and a read with a
 * repeated start. Realtek's driver returns 0 when the micom does not answer, and leaves the
 * buffer as it was. One read it answers itself, as Realtek's does: Cmd 0xD1 (the panel control's
 * state, DIL_MICOM_GPIO_SetOutputPort's read-back) is bit 5 of the register ucom_panel_reg
 * (0xb8061108), which nothing here drives: 0. */
#define MISC_UCOM_WRITE  _IOW('s', 18, u32)
#define MISC_UCOM_READ   _IOR('s', 19, u32)
#define MISC_UCOM_MAX    256
#define UCOM_PANEL_STATE 0xD1

struct ucom_arg32 {
    u8 cmd;
    u16 size;
    u32 buf;
};
static_assert(sizeof(struct ucom_arg32) == 8);

long rtk_ucom_ioctl(unsigned int cmd, unsigned long arg)
{
    struct ucom_arg32 a;
    bool read;
    long ret;

    if (copy_from_user(&a, (void __user *)arg, sizeof(a)))
        return -EFAULT;
    if (!a.size)
        return 0;
    if (a.size > MISC_UCOM_MAX)
        return -EINVAL;
    if (cmd == MISC_UCOM_READ && a.cmd == UCOM_PANEL_STATE) {
        u32 *reg = rtk_reg_ptr(ucom_panel_reg);
        u8 *data = kzalloc(a.size, GFP_KERNEL);

        if (!data)
            return -ENOMEM;
        data[0] = reg && (READ_ONCE(*reg) & BIT(5)) ? 1 : 0;
        ret = copy_to_user(compat_ptr(a.buf), data, a.size) ? -EFAULT : 0;
        kfree(data);
        return ret;
    }
    read = cmd == MISC_UCOM_READ;
    ret = lgemu_i2c_user_xfer(LGEMU_I2C_MICOM, 0, &a.cmd, read ? 1 : 0, compat_ptr(a.buf), a.size,
                              read);
    return ret == -EFAULT || ret == -ENOMEM ? ret : 0;
}

/* ---- /sys/class/rtk-pwm ---- */

enum {
    PWM_RUN,
    PWM_DUTY,
    PWM_FREQ,
    PWM_VSYNC,
    PWM_POS,
    PWM_POLARITY,
    PWM_ADAPT_FREQ,
    PWM_APPLY,
    PWM_ATTRS
};
static const char *const pwm_attr_names[PWM_ATTRS] = {
    "run", "duty", "freq", "vsync", "pos", "polarity", "adapt_freq", "apply",
};
struct pwm_out {
    struct device *dev;
    int value[PWM_ATTRS];
};
static struct pwm_out pwm_outs[ARRAY_SIZE(pwms)];
static DEFINE_MUTEX(pwm_lock);

static int pwm_attr_index(struct device_attribute *attr)
{
    for (int i = 0; i < PWM_ATTRS; i++)
        if (!strcmp(attr->attr.name, pwm_attr_names[i]))
            return i;
    return -1;
}

static ssize_t pwm_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct pwm_out *o = dev_get_drvdata(dev);
    int i = pwm_attr_index(attr), v;

    mutex_lock(&pwm_lock);
    v = o->value[i];
    mutex_unlock(&pwm_lock);
    if (i == PWM_POS)
        return sysfs_emit(buf, "(%d, %08x)\n", v, 0);
    return sysfs_emit(buf, "%d\n", v);
}

static ssize_t pwm_store(struct device *dev, struct device_attribute *attr, const char *buf,
                         size_t n)
{
    struct pwm_out *o = dev_get_drvdata(dev);
    int v;

    if (kstrtoint(skip_spaces(buf), 0, &v))
        return -EINVAL;
    mutex_lock(&pwm_lock);
    o->value[pwm_attr_index(attr)] = v;
    mutex_unlock(&pwm_lock);
    return n;
}

#define PWM_ATTR(name) static DEVICE_ATTR(name, 0664, pwm_show, pwm_store)
PWM_ATTR(run);
PWM_ATTR(duty);
PWM_ATTR(freq);
PWM_ATTR(vsync);
PWM_ATTR(pos);
PWM_ATTR(polarity);
PWM_ATTR(adapt_freq);
PWM_ATTR(apply);
static struct attribute *pwm_attrs[] = {
    &dev_attr_run.attr,        &dev_attr_duty.attr,  &dev_attr_freq.attr,
    &dev_attr_vsync.attr,      &dev_attr_pos.attr,   &dev_attr_polarity.attr,
    &dev_attr_adapt_freq.attr, &dev_attr_apply.attr, NULL,
};
ATTRIBUTE_GROUPS(pwm);
static const struct class pwm_class = {.name = "rtk-pwm", .dev_groups = pwm_groups};
static bool pwm_class_registered;

int rtk_pwms_make(void)
{
    int err;

    if (!pwms_count)
        return 0;
    err = class_register(&pwm_class);
    if (err)
        return err;
    pwm_class_registered = true;
    for (int i = 0; i < pwms_count; i++) {
        struct device *d;

        pwm_outs[i].value[PWM_FREQ] = pwm_freq;
        d = device_create(&pwm_class, NULL, 0, &pwm_outs[i], "%s", pwms[i]);

        if (IS_ERR(d))
            return PTR_ERR(d);
        pwm_outs[i].dev = d;
    }
    return 0;
}

void rtk_pwms_remove(void)
{
    for (int i = 0; i < ARRAY_SIZE(pwm_outs); i++) {
        if (pwm_outs[i].dev)
            device_unregister(pwm_outs[i].dev);
        pwm_outs[i].dev = NULL;
    }
    if (pwm_class_registered)
        class_unregister(&pwm_class);
    pwm_class_registered = false;
}

/* ---- /sys/realtek_boards ---- */

static ssize_t system_parameters_show(struct kobject *kobj, struct kobj_attribute *attr, char *page)
{
    return sysfs_emit(page, "%s\n", system_parameters);
}

static struct kobj_attribute system_parameters_attr = __ATTR_RO(system_parameters);
static struct kobject *boards_kobj;

void rtk_clock_prepare(void)
{
    INIT_DELAYED_WORK(&clock_work, clock_run);
}

void rtk_clock_stop(void)
{
    cancel_delayed_work_sync(&clock_work);
}

int rtk_boards_add(void)
{
    boards_kobj = kobject_create_and_add("realtek_boards", NULL);
    if (!boards_kobj)
        return -ENOMEM;
    return sysfs_create_file(boards_kobj, &system_parameters_attr.attr);
}

void rtk_boards_remove(void)
{
    kobject_put(boards_kobj);
    boards_kobj = NULL;
}

int rtk_gpio_add(void)
{
    int err = lgemu_gpiochip_add(&gpio_chip, "rtk", gpio_lines, gpio_level, gpio_level_count);

    if (!err)
        err = lgemu_gpiochip_usb_vbus(&gpio_chip, gpio_usb_vbus, gpio_usb_vbus_count);
    if (!err)
        err = lgemu_gpiochip_tach(&gpio_chip, gpio_tach, gpio_tach_count);
    if (err)
        lgemu_gpiochip_remove(&gpio_chip);
    return err;
}

void rtk_gpio_remove(void)
{
    lgemu_gpiochip_remove(&gpio_chip);
}

bool rtk_ucom_command(const char *name, unsigned int cmd)
{
    return ucom_node && !strcmp(name, ucom_node) &&
           (cmd == MISC_UCOM_WRITE || cmd == MISC_UCOM_READ);
}
