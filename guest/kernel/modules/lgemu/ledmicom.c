// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The LG OLED Flex's gaming LED micom (i2c.c's device "ledmicom"): the sub-micom behind the set's
 * back cover lighting, which the stock lightingcontroller drives. It reaches it through
 * libdile_i2c's GAMING_LED_MICOM row, channel 0 at 8-bit slave 0x12 (7-bit 0x09) in the O22
 * firmwares (W22O, W23O, W24G), on a board whose tool type is LX3Q (targets.led_micom_dev).
 *
 * What lightingcontroller sends (W22O 13.30.56, docs/research/led-lighting.md "The packets on the
 * I2C bus"): every lighting command is one write, the command byte and its payload; the micom
 * runs the animation itself. 0x21 lighting on or off, 0x22 and 0x23 the 40 LEDs' colours of video
 * and sound sync in chunks, 0x24 and 0x25 the one-shot modes, 0x26 to 0x29 the four static
 * modes' colour (R, G, B), 0x2a the brightness (1 to 10), 0x2b the welcome ceremony, 0x2c the
 * factory pattern. The only read is the firmware version: the register 0x45 written, then 7 bytes
 * read, which the service formats as "V%c.%c%c.%c".
 *
 * The model acknowledges every write and keeps, per command byte, how many writes it had and the
 * last payload; /proc/lgemu/led_micom lists them for the host (`beanjar status show`'s lighting,
 * host/core/beanjar/lighting.py decodes them). Nothing else in the stock stack talks to it. */
#define pr_fmt(fmt) KBUILD_MODNAME ": ledmicom: " fmt
#include <linux/i2c.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/string.h>
#include "lgemu.h"
#include "lgemu-i2c.h"

/* ---- the LED micom's registers (tests/unit/kernel/test_lgemu_ledmicom.py compiles this part on
 * the host) ---- */

#define LED_VERSION_REG 0x45
#define LED_VERSION_LEN 7
#define LED_PAYLOAD_MAX 128 /* a 40-LED RGB chunk is 120 bytes */
#define LED_COMMANDS    256

struct led_command {
    u32 count;      /* writes with this command byte */
    u32 order;      /* the micom's write number of the last one (1 on), 0 for none */
    u8 len;         /* the last payload's length, at most LED_PAYLOAD_MAX */
    bool truncated; /* the last payload was longer */
    u8 data[LED_PAYLOAD_MAX];
};

struct led_micom {
    struct led_command cmds[LED_COMMANDS];
    u32 writes, reads;
    u8 ptr; /* the register a read starts at: the last write's command byte */
};

/* The version the micom reports: "V1.00.0" as the service prints it from bytes 0, 2, 3 and 5
 * (INFERRED layout: 7 ASCII bytes, the service's format picks the digits). */
static const u8 led_version[LED_VERSION_LEN] = {'1', '.', '0', '0', '.', '0', 0};

static void led_reset(struct led_micom *c)
{
    memset(c, 0, sizeof(*c));
}

/* A write message: the command byte, then its payload. */
static void led_write(struct led_micom *c, const u8 *buf, unsigned int len)
{
    struct led_command *cmd;
    unsigned int n;

    if (!len)
        return;
    c->ptr = buf[0];
    if (len == 1) /* a register pointer for the read that follows */
        return;
    cmd = &c->cmds[buf[0]];
    n = len - 1 > LED_PAYLOAD_MAX ? LED_PAYLOAD_MAX : len - 1;
    memcpy(cmd->data, buf + 1, n);
    cmd->len = n;
    cmd->truncated = len - 1 > LED_PAYLOAD_MAX;
    cmd->count++;
    cmd->order = ++c->writes;
}

/* A read message: the version at LED_VERSION_REG, zeros elsewhere. */
static void led_read(struct led_micom *c, u8 *buf, unsigned int len)
{
    for (unsigned int i = 0; i < len; i++)
        buf[i] = c->ptr == LED_VERSION_REG && i < LED_VERSION_LEN ? led_version[i] : 0;
    c->reads++;
}

/* ---- end of the LED micom's registers ---- */

static DEFINE_MUTEX(led_lock);
static struct led_micom *chip;

int lgemu_ledmicom_xfer(struct i2c_msg *msgs, int num)
{
    mutex_lock(&led_lock);
    if (!chip) {
        mutex_unlock(&led_lock);
        return -EREMOTEIO;
    }
    for (int i = 0; i < num; i++) {
        if (msgs[i].flags & I2C_M_RD)
            led_read(chip, msgs[i].buf, msgs[i].len);
        else
            led_write(chip, msgs[i].buf, msgs[i].len);
    }
    mutex_unlock(&led_lock);
    return 0;
}

/* /proc/lgemu/led_micom: "writes N", "reads N", then one line per command byte written, in byte
 * order: "cmd 0x21 count 2 order 7 len 1 data 02" (the payload in hex; "truncated" after it when
 * the last payload was longer than LED_PAYLOAD_MAX). */
static int led_show(struct seq_file *m, void *unused)
{
    mutex_lock(&led_lock);
    seq_printf(m, "writes %u\nreads %u\n", chip->writes, chip->reads);
    for (int i = 0; i < LED_COMMANDS; i++) {
        const struct led_command *cmd = &chip->cmds[i];

        if (!cmd->count)
            continue;
        seq_printf(m, "cmd 0x%02x count %u order %u len %u data ", i, cmd->count, cmd->order,
                   cmd->len);
        for (int j = 0; j < cmd->len; j++)
            seq_printf(m, "%02x", cmd->data[j]);
        seq_puts(m, cmd->truncated ? " truncated\n" : "\n");
    }
    mutex_unlock(&led_lock);
    return 0;
}

int lgemu_ledmicom_init(void)
{
    chip = kzalloc(sizeof(*chip), GFP_KERNEL);
    if (!chip)
        return -ENOMEM;
    led_reset(chip);
    /* in snd.c's /proc/lgemu, which an earlier part made */
    if (!proc_create_single("lgemu/led_micom", 0444, NULL, led_show))
        pr_err("cannot add /proc/lgemu/led_micom\n");
    return 0;
}

void lgemu_ledmicom_exit(void)
{
    remove_proc_entry("lgemu/led_micom", NULL);
    mutex_lock(&led_lock);
    kfree(chip);
    chip = NULL;
    mutex_unlock(&led_lock);
}
