// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The board's ambient light sensor (i2c.c's devices "light1" and "light2"), the "eye sensor" the
 * picture quality service dims the backlight by. pqcontroller reads it over I2C at 8-bit
 * slave 0x20 (7-bit 0x10), on the channel the firmware's libdile_i2c table gives LIGHT_SENSOR_1
 * and LIGHT_SENSOR_2 (channel 5 on the LM21, K7 and K8 lines, channel 1 on O22's), when the board's
 * tool option digitalEye (ToolOpt6 bits 0-1) says the set has one.
 *
 * What pqcontroller does with it (W21A, W22O 04.40.93, W24P; DIL_VPQ_SensorCheck, SensorDataInit,
 * SensorDataRead): reads two bytes at 0x09 and takes byte 0 = 0x35 for the second sensor type,
 * anything else for the first, and uses the sensor only if that type is the one digitalEye names
 * (eye_sensor: type 1; rgb_sensor and OTP_eyeSensor: type 2). Type 2 it initialises with three
 * writes at subaddress 0x00 (01 81, 00 01, then 40 21, or 40 39 for country group 2 with tool type
 * 23) and then reads two little-endian bytes at 0x05 (its luminance) and at 0x04 (its visible
 * luminance) every 0.7 s or so; type 1 with three writes without a subaddress (00 01, 00 41,
 * 00 6e) and then reads 0x50 and 0x51 the same way.
 *
 * The model is the protocol as pqcontroller drives it: the first byte of a write is the register
 * (the command), the bytes after it its value; a read reads the register's 16-bit word,
 * little-endian, and the next registers' if it reads on. The data registers hold the host's two
 * channels, the ID register the type's ID, and the rest keep the last word written to them. Which
 * chip LG fitted is not known, so nothing beyond what pqcontroller reads is modelled.
 *
 * The host sets the light, raw counts of the chip's two channels, through the module parameter
 * light ("ch0,ch1", writable at runtime: /sys/module/lgemu/parameters/light); `beanjar sensor
 * light` writes it (host/core/beanjar/devices/sensors.py). ch0 is what pqcontroller reports as
 * luminance, ch1 as visibleLuminance. */
#define pr_fmt(fmt) KBUILD_MODNAME ": light: " fmt
#include <linux/i2c.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/mutex.h>
#include <linux/string.h>
#include "lgemu.h"
#include "lgemu-i2c.h"

/* ---- the light sensor's registers (tests/unit/kernel/test_lgemu_light.py compiles this part on
 * the host) ---- */

#define EYE_REGS       0x80
#define EYE_ID         0x09
#define EYE_ID_TYPE2   0x0035 /* byte 0 0x35: the second type */
#define EYE_ID_TYPE1   0x0000 /* anything but 0x35 in byte 0 */
#define EYE2_LUMINANCE 0x05   /* type 2: ch0 */
#define EYE2_VISIBLE   0x04   /* type 2: ch1 */
#define EYE1_CH0       0x50   /* type 1 */
#define EYE1_CH1       0x51
#define EYE_COUNT_MAX  0xFFFF

struct eye {
    int type; /* 1 or 2 */
    u16 regs[EYE_REGS];
    u8 ptr;
    u16 ch[2]; /* the host's light, the chip's two channels in raw counts */
};

static void eye_reset(struct eye *c)
{
    memset(c->regs, 0, sizeof(c->regs));
    c->ptr = 0;
}

static u16 eye_word(const struct eye *c, u8 reg)
{
    if (reg == EYE_ID)
        return c->type == 2 ? EYE_ID_TYPE2 : EYE_ID_TYPE1;
    if (c->type == 2 && reg == EYE2_LUMINANCE)
        return c->ch[0];
    if (c->type == 2 && reg == EYE2_VISIBLE)
        return c->ch[1];
    if (c->type == 1 && reg == EYE1_CH0)
        return c->ch[0];
    if (c->type == 1 && reg == EYE1_CH1)
        return c->ch[1];
    return reg < EYE_REGS ? c->regs[reg] : 0;
}

/* A write message: the register, then its value (little-endian; one byte sets the low byte). */
static void eye_write(struct eye *c, const u8 *buf, unsigned int len)
{
    if (!len)
        return;
    c->ptr = buf[0] & (EYE_REGS - 1);
    if (len >= 2)
        c->regs[c->ptr] = buf[1] | (len >= 3 ? buf[2] << 8 : 0);
}

/* A read message: len bytes of the words from the register on, each little-endian. */
static void eye_read(const struct eye *c, u8 *buf, unsigned int len)
{
    for (unsigned int i = 0; i < len; i++) {
        u16 word = eye_word(c, (c->ptr + i / 2) & (EYE_REGS - 1));

        buf[i] = i & 1 ? word >> 8 : word & 0xff;
    }
}

/* "ch0,ch1" in raw counts, or one value for both, a newline allowed at the end, into ch[2];
 * -EINVAL otherwise. */
static int eye_parse(const char *text, u16 *ch)
{
    unsigned int v[2];
    int len = strlen(text), used = -1;

    if (len && text[len - 1] == '\n')
        len--;
    if (sscanf(text, "%u,%u%n", &v[0], &v[1], &used) != 2 || used != len) {
        used = -1;
        if (sscanf(text, "%u%n", &v[0], &used) != 1 || used != len)
            return -EINVAL;
        v[1] = v[0];
    }
    if (v[0] > EYE_COUNT_MAX || v[1] > EYE_COUNT_MAX)
        return -EINVAL;
    ch[0] = v[0];
    ch[1] = v[1];
    return 0;
}

/* ---- end of the light sensor's registers ---- */

static DEFINE_MUTEX(light_lock);
/* a bright room, where pqcontroller keeps the user's backlight as it did with no sensor: W22O's
 * (type 1, energy saving auto) settles at 48-49 for 0 and 400 counts, 55 for 1500, 60 for 3000,
 * and climbs to 100 for 6000 */
static struct eye chip = {.ch = {6000, 6000}};
static bool chip_reset;

static int light_set(const char *val, const struct kernel_param *kp)
{
    u16 ch[2];
    int err = eye_parse(val, ch);

    if (err)
        return err;
    mutex_lock(&light_lock);
    memcpy(chip.ch, ch, sizeof(ch));
    mutex_unlock(&light_lock);
    pr_info("%u,%u\n", ch[0], ch[1]);
    return 0;
}

static int light_get(char *buf, const struct kernel_param *kp)
{
    int n;

    mutex_lock(&light_lock);
    n = sprintf(buf, "%u,%u\n", chip.ch[0], chip.ch[1]);
    mutex_unlock(&light_lock);
    return n;
}

static const struct kernel_param_ops light_ops = {.set = light_set, .get = light_get};
module_param_cb(light, &light_ops, NULL, 0644);
MODULE_PARM_DESC(light, "light: the ambient light sensor's two channels, ch0,ch1 in raw counts "
                        "(pqcontroller's luminance and visibleLuminance; one value sets both)");

int lgemu_light_xfer(int type, struct i2c_msg *msgs, int num)
{
    mutex_lock(&light_lock);
    if (!chip_reset || chip.type != type) { /* power-on (the light is the host's) */
        eye_reset(&chip);
        chip.type = type;
        chip_reset = true;
    }
    for (int i = 0; i < num; i++) {
        if (msgs[i].flags & I2C_M_RD)
            eye_read(&chip, msgs[i].buf, msgs[i].len);
        else
            eye_write(&chip, msgs[i].buf, msgs[i].len);
    }
    mutex_unlock(&light_lock);
    return 0;
}
