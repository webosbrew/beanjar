// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The board's accelerometer, a Bosch BMA422 (i2c.c's device "accel"), for the StanbyME's motion
 * sensor service: N21D's msc (com.webos.service.msc) reads it over I2C channel 3 at 7-bit 0x19
 * (libdile_i2c's ACCELERATION_SENSOR_BMA422, /dev/i2c-3 on lgemu's adapters) and turns its gravity
 * vector into the set's orientation.
 *
 * What msc does with it (N21D 23.22.86, ACCELEROMETER_DRV_I2C_Selector, _checkICbyI2C,
 * getXYZ_BMA422): reads register 0x00 (CHIP_ID) and takes 0x12 for a BMA422, writes PWR_CTRL (0x7D)
 * = 4 and ACC_CONF (0x40) = 0x88, then reads the six data bytes at 0x12 (DATA_8 to DATA_13: x, y, z
 * as little-endian 16-bit words) every 90 ms and shifts them right by 4. ACC_RANGE it leaves at
 * its reset value, ±4 g, 512 counts per g.
 *
 * The model is the chip's register file as the BMA4xx's I2C interface has it: the first byte of a
 * write sets the register pointer and the bytes after it are written from there; a read reads from
 * the pointer on; both advance it. The data registers hold the gravity vector, in the chip's 12-bit
 * left-aligned form at the range ACC_RANGE selects, while PWR_CTRL's acc_en is set, and zero while
 * it is not (the chip's reset). CMD (0x7E) 0xB6 is the soft reset. The rest of the registers keep
 * what is written to them (from 0x40 on) or read their reset value.
 *
 * The host sets the vector, in mg along the chip's axes, through the module parameter accel_mg
 * ("x,y,z", writable at runtime: /sys/module/lgemu/parameters/accel_mg); beanjar sensor set rotate
 * writes the one for the set's angle (host/core/beanjar/rotation.py, gravity()). It starts at
 * the set standing in landscape. */
#define pr_fmt(fmt) KBUILD_MODNAME ": accel: " fmt
#include <linux/i2c.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/mutex.h>
#include <linux/string.h>
#include "lgemu.h"
#include "lgemu-i2c.h"

/* ---- the BMA422's registers (tests/unit/kernel/test_lgemu_accel.py compiles this part on the
 * host) ---- */

#define BMA4_REGS            0x80
#define BMA4_CHIP_ID         0x00
#define BMA4_CHIP_ID_BMA422  0x12
#define BMA4_STATUS          0x03 /* bit 7: drdy_acc */
#define BMA4_DATA_8          0x12 /* x LSB; to DATA_13 (0x17), z MSB */
#define BMA4_INTERNAL_STATUS 0x2A
#define BMA4_ACC_CONF        0x40
#define BMA4_ACC_RANGE       0x41
#define BMA4_INIT_CTRL       0x59
#define BMA4_PWR_CONF        0x7C
#define BMA4_PWR_CTRL        0x7D /* bit 2: acc_en */
#define BMA4_CMD             0x7E
#define BMA4_CMD_SOFTRESET   0xB6
#define BMA4_WRITABLE        0x40  /* registers from here on keep what is written */
#define BMA4_MG_MAX          16000 /* the chip's widest range */

struct bma4 {
    u8 regs[BMA4_REGS];
    u8 ptr;
    int mg[3]; /* the gravity vector along the chip's x, y, z, in mg */
};

static void bma4_reset(struct bma4 *c)
{
    memset(c->regs, 0, sizeof(c->regs));
    c->regs[BMA4_CHIP_ID] = BMA4_CHIP_ID_BMA422;
    c->regs[BMA4_ACC_CONF] = 0xA8;
    c->regs[BMA4_ACC_RANGE] = 0x01; /* ±4 g */
    c->regs[BMA4_PWR_CONF] = 0x03;
    c->ptr = 0;
}

/* One axis as the data registers carry it: 12 bits, left-aligned in the 16-bit word, at the range
 * ACC_RANGE selects (±2, 4, 8, 16 g: 1024, 512, 256, 128 counts per g), saturated. */
static s16 bma4_word(const struct bma4 *c, int mg)
{
    int per_g = 1024 >> (c->regs[BMA4_ACC_RANGE] & 3);
    int counts = mg * per_g / 1000;

    if (counts > 2047)
        counts = 2047;
    if (counts < -2048)
        counts = -2048;
    return (s16)(counts * 16);
}

static u8 bma4_read_reg(const struct bma4 *c, u8 reg)
{
    if (reg >= BMA4_DATA_8 && reg < BMA4_DATA_8 + 6) {
        unsigned int i = reg - BMA4_DATA_8;
        u16 word;

        if (!(c->regs[BMA4_PWR_CTRL] & 0x04))
            return 0;
        word = (u16)bma4_word(c, c->mg[i / 2]);
        return i & 1 ? word >> 8 : word & 0xff;
    }
    if (reg == BMA4_STATUS)
        return c->regs[BMA4_PWR_CTRL] & 0x04 ? 0x80 : 0;
    return reg < BMA4_REGS ? c->regs[reg] : 0;
}

static void bma4_write_reg(struct bma4 *c, u8 reg, u8 value)
{
    if (reg == BMA4_CMD) {
        if (value == BMA4_CMD_SOFTRESET)
            bma4_reset(c);
        return;
    }
    if (reg >= BMA4_WRITABLE && reg < BMA4_REGS)
        c->regs[reg] = value;
}

/* A write message: the register pointer, then data from there. */
static void bma4_write(struct bma4 *c, const u8 *buf, unsigned int len)
{
    if (!len)
        return;
    c->ptr = buf[0] & (BMA4_REGS - 1);
    for (unsigned int i = 1; i < len; i++) {
        u8 reg = c->ptr;

        c->ptr = (c->ptr + 1) & (BMA4_REGS - 1);
        bma4_write_reg(c, reg, buf[i]); /* a soft reset sets ptr to 0 */
    }
}

/* A read message: len bytes from the register pointer on. */
static void bma4_read(struct bma4 *c, u8 *buf, unsigned int len)
{
    for (unsigned int i = 0; i < len; i++) {
        buf[i] = bma4_read_reg(c, c->ptr);
        c->ptr = (c->ptr + 1) & (BMA4_REGS - 1);
    }
}

/* "x,y,z" in mg into mg[3], each within the chip's widest range; -EINVAL otherwise. */
static int bma4_parse_mg(const char *text, int *mg)
{
    int v[3], n;
    char tail[2];

    n = sscanf(text, "%d,%d,%d%c%c", &v[0], &v[1], &v[2], &tail[0], &tail[1]);
    if (n != 3 && !(n == 4 && tail[0] == '\n'))
        return -EINVAL;
    for (int i = 0; i < 3; i++)
        if (v[i] < -BMA4_MG_MAX || v[i] > BMA4_MG_MAX)
            return -EINVAL;
    memcpy(mg, v, sizeof(v));
    return 0;
}

/* ---- end of the BMA422's registers ---- */

static DEFINE_MUTEX(accel_lock);
static struct bma4 chip = {.mg = {0, -1000, 0}};
static bool chip_reset;

static int accel_mg_set(const char *val, const struct kernel_param *kp)
{
    int mg[3], err = bma4_parse_mg(val, mg);

    if (err)
        return err;
    mutex_lock(&accel_lock);
    memcpy(chip.mg, mg, sizeof(mg));
    mutex_unlock(&accel_lock);
    pr_info("gravity %d,%d,%d mg\n", mg[0], mg[1], mg[2]);
    return 0;
}

static int accel_mg_get(char *buf, const struct kernel_param *kp)
{
    int n;

    mutex_lock(&accel_lock);
    n = sprintf(buf, "%d,%d,%d\n", chip.mg[0], chip.mg[1], chip.mg[2]);
    mutex_unlock(&accel_lock);
    return n;
}

static const struct kernel_param_ops accel_mg_ops = {.set = accel_mg_set, .get = accel_mg_get};
module_param_cb(accel_mg, &accel_mg_ops, NULL, 0644);
MODULE_PARM_DESC(accel_mg, "accel: the accelerometer's gravity vector, x,y,z in mg along the "
                           "chip's axes (default 0,-1000,0, the set standing in landscape)");

int lgemu_accel_xfer(struct i2c_msg *msgs, int num)
{
    mutex_lock(&accel_lock);
    if (!chip_reset) { /* power-on: the registers' reset values (the vector is the host's) */
        bma4_reset(&chip);
        chip_reset = true;
    }
    for (int i = 0; i < num; i++) {
        if (msgs[i].flags & I2C_M_RD)
            bma4_read(&chip, msgs[i].buf, msgs[i].len);
        else
            bma4_write(&chip, msgs[i].buf, msgs[i].len);
    }
    mutex_unlock(&accel_lock);
    return 0;
}
