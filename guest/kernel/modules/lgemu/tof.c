// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The board's ToF motion sensor, an AMS TMF8821 (i2c.c's device "tof"): what msc's direct
 * TMF882X driver reads to tell whether someone moves in front of the set, for Always Ready and
 * Life On Screen. msc reaches it through libdile_i2c's TMF882X_SENSOR_N row, channel 1 at 8-bit
 * slave 0x82 (7-bit 0x41) in every O22 firmware, when the board's tool option
 * supportMotionDetectSensor (ToolOpt4 bits 14-15) is "ToF_TBD_22Y".
 *
 * What msc does with it (W22O 43.21.89, W23O, W24G; drv_TMF8821_initialize and
 * tof8801_app0_process_irq):
 *   - writes 0xE0 = 0x01 and polls 0xE0 until it reads 0x41 (PON and CPU ready);
 *   - reads the chip ID 0xE3 and revision 0xE4 (logged only) and the app ID at 0x00 (0x80, the
 *     bootloader, would make it download a RAM patch: the application answers 0x02);
 *   - loads, writes and stores configuration pages with commands at 0x08 (0x16 load common,
 *     0x15 write, 0x20 factory calibration, 0x19 load the calibration page), polling 0x08 until
 *     the status is below 0x10, with the page's data in 0x20 on;
 *   - starts measuring with 0x08 = 0x10 and then, every 100 ms, reads 0xE1; with bit 0 set it
 *     writes the bit back to clear it and reads the result record, 52 bytes at 0x20: a 4-byte
 *     header, the result number, temperature and count of valid results at 0x24-0x26, then
 *     confidence and distance (mm, little-endian) of the nine zones in triplets at 0x38 + 3i;
 *   - 0x08 = 0xFF stops, 0xE0 written again puts it to standby (msc's tof8801_standby_from_wake);
 *   - the firmware version it compares with /usr/sbin/motionsensor_firmware's (20) is the byte
 *     at 0x02: this chip has 20, so msc never downloads that file.
 *
 * msc reports motion (getMotionStatus 1) for one result when a zone, after changing its distance
 * by more than 4 % (2 % from 3 m), holds the new one for 2, 4 or 6 results (the sensitivity:
 * high, medium, low); every other result reports 0. A person who stands still is therefore no
 * motion. The model is a person in front of the set who keeps moving: at the host's distance
 * all nine zones see them, stepping between the distance and an eighth further every
 * TOF_STEP results, so msc sees motion about once a second; with nobody there no zone has an
 * object (confidence 0, distance 0, which msc skips). A new result is ready each time msc reads
 * 0xE1 after clearing the last one.
 *
 * The host sets it through the module parameter presence (mm, 0 for nobody; writable at runtime:
 * /sys/module/lgemu/parameters/presence); `beanjar sensor set presence` writes it
 * (host/core/beanjar/devices/sensors.py). */
#define pr_fmt(fmt) KBUILD_MODNAME ": tof: " fmt
#include <linux/i2c.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/mutex.h>
#include <linux/string.h>
#include "lgemu.h"
#include "lgemu-i2c.h"

/* ---- the TMF8821's registers (tests/unit/kernel/test_lgemu_tof.py compiles this part on the host)
 * ---- */

#define TOF_APP_ID     0x00
#define TOF_APP_MAJOR  0x01
#define TOF_APP_MINOR  0x02 /* msc's SWVersion */
#define TOF_CMD_STAT   0x08
#define TOF_PAGE       0x20 /* config page or result record: cid/rid, tid, size (2), data */
#define TOF_PAGE_END   0xE0
#define TOF_ENABLE     0xE0 /* bit 0 PON, bit 6 CPU ready */
#define TOF_INT_STAT   0xE1
#define TOF_INT_EN     0xE2
#define TOF_ID         0xE3
#define TOF_REV_ID     0xE4
#define TOF_ZONES      9
#define TOF_ZONE0      0x38 /* confidence, distance LSB, distance MSB of zone i at 0x38 + 3i */
#define TOF_STEP       10   /* results between the person's steps */
#define TOF_CONFIDENCE 200
#define TOF_MIN_MM     100 /* msc's DistanceThresh: nearer zones are ignored */
#define TOF_MAX_MM     5000
#define TOF_PRESENT_MM 1500 /* "present": a viewer on the sofa */
#define TOF_PAGES      4    /* stored pages: common, factory calibration, SPAD 1 and 2 */

enum {
    CMD_MEASURE = 0x10,
    CMD_WRITE_CONFIG = 0x15,
    CMD_LOAD_COMMON = 0x16,
    CMD_LOAD_SPAD_1 = 0x17,
    CMD_LOAD_SPAD_2 = 0x18,
    CMD_LOAD_FACTORY = 0x19,
    CMD_FACTORY_CALIBRATION = 0x20,
    CMD_STOP = 0xFF,
    RID_RESULT = 0x10,
    STAT_OK = 0x00,
    STAT_ACCEPTED = 0x01,
};

struct tof {
    u8 regs[256];
    u8 pages[TOF_PAGES][TOF_PAGE_END - TOF_PAGE]; /* what the configuration pages hold */
    u8 ptr;
    bool measuring;
    u8 result; /* the result number */
    unsigned int frames;
    u16 presence; /* the host's: mm of the moving person, 0 for nobody */
};

static void tof_reset(struct tof *c)
{
    memset(c->regs, 0, sizeof(c->regs));
    memset(c->pages, 0, sizeof(c->pages));
    c->regs[TOF_APP_ID] = 0x02; /* the application, not the bootloader (0x80) */
    c->regs[TOF_APP_MAJOR] = 0x04;
    c->regs[TOF_APP_MINOR] = 20; /* motionsensor_firmware's version: no download */
    c->regs[TOF_ENABLE] = 0x41;
    c->regs[TOF_ID] = 0x08;
    c->regs[TOF_REV_ID] = 0x01;
    c->ptr = 0;
    c->measuring = false;
    c->result = 0;
    c->frames = 0;
}

static int page_index(u8 cid)
{
    switch (cid) {
    case CMD_LOAD_COMMON:
        return 0;
    case CMD_LOAD_FACTORY:
        return 1;
    case CMD_LOAD_SPAD_1:
        return 2;
    case CMD_LOAD_SPAD_2:
        return 3;
    }
    return -1;
}

/* The page cid into 0x20 on: its header and what was last written to it. */
static void load_page(struct tof *c, u8 cid)
{
    int i = page_index(cid);

    memset(c->regs + TOF_PAGE, 0, TOF_PAGE_END - TOF_PAGE);
    if (i >= 0)
        memcpy(c->regs + TOF_PAGE, c->pages[i], sizeof(c->pages[i]));
    c->regs[TOF_PAGE] = cid;
    c->regs[TOF_PAGE + 2] = (TOF_PAGE_END - TOF_PAGE - 4) & 0xff;
}

static void command(struct tof *c, u8 cmd)
{
    int i;

    c->regs[TOF_CMD_STAT] = STAT_OK;
    switch (cmd) {
    case CMD_MEASURE:
        c->measuring = true;
        c->regs[TOF_CMD_STAT] = STAT_ACCEPTED;
        break;
    case CMD_STOP:
        c->measuring = false;
        break;
    case CMD_WRITE_CONFIG:
        i = page_index(c->regs[TOF_PAGE]);
        if (i >= 0)
            memcpy(c->pages[i], c->regs + TOF_PAGE, sizeof(c->pages[i]));
        break;
    case CMD_FACTORY_CALIBRATION:
        load_page(c, CMD_LOAD_FACTORY);
        break;
    default:
        if (page_index(cmd) >= 0)
            load_page(c, cmd);
        break;
    }
}

/* The person's distance in the result now: the host's, or an eighth further every other step. */
static u16 zone_mm(const struct tof *c)
{
    if (!c->presence)
        return 0;
    return (c->frames / TOF_STEP) & 1 ? c->presence + c->presence / 8 : c->presence;
}

/* The next measurement result into 0x20 on, and its interrupt. */
static void next_result(struct tof *c)
{
    u16 mm = zone_mm(c);
    u32 tick = c->frames * 100;
    u8 *r = c->regs;

    memset(r + TOF_PAGE, 0, TOF_PAGE_END - TOF_PAGE);
    r[TOF_PAGE] = RID_RESULT;
    r[TOF_PAGE + 1] = c->result;  /* tid */
    r[TOF_PAGE + 2] = 0x30;       /* the record after the header */
    r[0x24] = c->result++;        /* result number */
    r[0x25] = 30;                 /* temperature, C */
    r[0x26] = mm ? TOF_ZONES : 0; /* valid results */
    for (int i = 0; i < 4; i++)
        r[0x34 + i] = tick >> (8 * i); /* system tick, ms */
    for (int i = 0; i < TOF_ZONES; i++) {
        r[TOF_ZONE0 + 3 * i] = mm ? TOF_CONFIDENCE : 0;
        r[TOF_ZONE0 + 3 * i + 1] = mm & 0xff;
        r[TOF_ZONE0 + 3 * i + 2] = mm >> 8;
    }
    c->frames++;
    r[TOF_INT_STAT] |= 0x01;
}

/* A write message: the register, then its value and the next registers'. */
static void tof_write(struct tof *c, const u8 *buf, unsigned int len)
{
    if (!len)
        return;
    c->ptr = buf[0];
    for (unsigned int i = 1; i < len; i++) {
        u8 reg = c->ptr++, v = buf[i];

        if (reg == TOF_ENABLE)
            c->regs[reg] = v & 0x01 ? 0x41 : 0x00; /* power on: the CPU is ready at once */
        else if (reg == TOF_INT_STAT)
            c->regs[reg] &= ~v; /* write 1 to clear */
        else if (reg == TOF_CMD_STAT)
            command(c, v);
        else if (reg != TOF_APP_ID && reg != TOF_ID && reg != TOF_REV_ID)
            c->regs[reg] = v;
    }
}

/* A read message: len bytes from the register on; reading the interrupt status while measuring
 * with the last result taken makes the next one. */
static void tof_read(struct tof *c, u8 *buf, unsigned int len)
{
    if (c->ptr == TOF_INT_STAT && c->measuring && !(c->regs[TOF_INT_STAT] & 0x01))
        next_result(c);
    for (unsigned int i = 0; i < len; i++)
        buf[i] = c->regs[c->ptr++];
}

/* "0" or "absent" for nobody, "present" for TOF_PRESENT_MM, or the mm (100 to 5000), a newline
 * allowed at the end, into *mm; -EINVAL otherwise. */
static int tof_parse(const char *text, u16 *mm)
{
    unsigned int v;
    int len = strlen(text), used = -1;

    if (len && text[len - 1] == '\n')
        len--;
    if (len == 6 && !strncmp(text, "absent", 6)) {
        *mm = 0;
        return 0;
    }
    if (len == 7 && !strncmp(text, "present", 7)) {
        *mm = TOF_PRESENT_MM;
        return 0;
    }
    if (sscanf(text, "%u%n", &v, &used) != 1 || used != len)
        return -EINVAL;
    if (v && (v < TOF_MIN_MM || v > TOF_MAX_MM))
        return -EINVAL;
    *mm = v;
    return 0;
}

/* ---- end of the TMF8821's registers ---- */

static DEFINE_MUTEX(tof_lock);
static struct tof chip; /* nobody in front of the set at a start */
static bool chip_reset;

static int presence_set(const char *val, const struct kernel_param *kp)
{
    u16 mm;
    int err = tof_parse(val, &mm);

    if (err)
        return err;
    mutex_lock(&tof_lock);
    chip.presence = mm;
    mutex_unlock(&tof_lock);
    pr_info("presence %u mm\n", mm);
    return 0;
}

static int presence_get(char *buf, const struct kernel_param *kp)
{
    int n;

    mutex_lock(&tof_lock);
    n = sprintf(buf, "%u\n", chip.presence);
    mutex_unlock(&tof_lock);
    return n;
}

static const struct kernel_param_ops presence_ops = {.set = presence_set, .get = presence_get};
module_param_cb(presence, &presence_ops, NULL, 0644);
MODULE_PARM_DESC(presence, "tof: the person moving in front of the ToF motion sensor, mm "
                           "(100 to 5000; 0 or absent: nobody; present: 1500)");

int lgemu_tof_xfer(struct i2c_msg *msgs, int num)
{
    mutex_lock(&tof_lock);
    if (!chip_reset) { /* power-on (the presence is the host's) */
        u16 mm = chip.presence;

        tof_reset(&chip);
        chip.presence = mm;
        chip_reset = true;
    }
    for (int i = 0; i < num; i++) {
        if (msgs[i].flags & I2C_M_RD)
            tof_read(&chip, msgs[i].buf, msgs[i].len);
        else
            tof_write(&chip, msgs[i].buf, msgs[i].len);
    }
    mutex_unlock(&tof_lock);
    return 0;
}
