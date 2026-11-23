// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
#include "private.h"

/* ---- LM14's software I2C bus (swi2c) ----
 *
 * LM14's NVM EEPROM is on a bit-banged bus (MAdp_IIC channel 1, RELEASE.cfg's "[i2c]NVM:2:0xA0":
 * libmadp's sSWI2CCfg bus 0, SCL pad 53, SDA pad 52). libapiSWI2C drives it through libdrvGPIO's
 * pad table as open drain: a pad pulls its line low with its output enabled and 0, and releases it
 * by turning it into an input (OEN set); it never drives high. It reads a line's level back, waits
 * for SCL to read high (clock stretching), and samples ACK and data with SCL high. Pads 52 and 53
 * are one byte each of the RIU word at byte address 0x102b86 (bank 0x102b, PM-based mapping:
 * physical 0x1f000000 + 2 * (r & ~1) + (r & 1)): bit 0 the output level, bit 1 OEN (1: input),
 * bit 2 the line's level. libdrvGPIO accesses them as single bytes.
 *
 * This file models that bus as wires: the master's pads and a slave, the lgemu.ko device swi2c=
 * names at its 8-bit address, which pulls SDA low to acknowledge and to send 0 bits. A line is low
 * when either side pulls it. The slave never stretches the clock. The device gets whole messages,
 * as on the HWI2C model (hwi2c.c): the bytes of a write at its STOP, or at a repeated START that
 * does not read them back; a read when the read address is acknowledged (the write before it, if
 * any, then a prefetch the data bits are clocked out of; with a 2-byte sub-address it continues
 * at the next address, as an EEPROM's sequential read does). A device that refuses the message
 * leaves its address unacknowledged.
 *
 * The section between the markers is plain C (compiled on the host by
 * tests/unit/kernel/test_mstar_utopia_swi2c.py). */

/* ---- the software I2C slave ---- */
#define SWI2C_OUT      64 /* a write's bytes: the sub-address and an EEPROM page */
#define SWI2C_PREFETCH 64 /* a read's bytes per device message */

struct swi2c_msg {
    u8 addr; /* 7-bit */
    bool read;
    u8 *buf;
    unsigned int len;
};

enum swi2c_phase { SWI2C_IDLE, SWI2C_ADDR, SWI2C_WRITE, SWI2C_READ, SWI2C_IGNORE };

struct swi2c {
    /* the device: xfer(ctx, msgs, n) returns < 0 for a refusal */
    int (*xfer)(void *ctx, struct swi2c_msg *msgs, int n);
    void *ctx;
    u8 slave; /* 8-bit write address */
    /* the wires: the master's pads (1 released) and the slave's SDA (0 pulled low) */
    bool m_scl, m_sda, s_sda;
    enum swi2c_phase phase;
    bool ack;     /* in the ninth clock of a byte */
    bool m_nack;  /* the master's answer to a read byte */
    bool pending; /* a write a repeated START has not resolved yet */
    u8 byte;
    unsigned int bits;
    u8 out[SWI2C_OUT];
    unsigned int out_len;
    u8 in[SWI2C_PREFETCH];
    unsigned int in_pos, in_len;
    u8 sub[2]; /* a read's 2-byte sub-address, for the next prefetch */
    bool sub_valid;
    u8 cur;
    u32 starts, stops, messages, refused;
};

static bool swi2c_sda(const struct swi2c *b)
{
    return b->m_sda && b->s_sda;
}

static void swi2c_flush(struct swi2c *b)
{
    struct swi2c_msg m = {.addr = b->slave >> 1, .read = false, .buf = b->out, .len = b->out_len};

    if (b->pending && b->out_len) {
        b->messages++;
        if (b->xfer(b->ctx, &m, 1) < 0)
            b->refused++;
    }
    b->pending = false;
    b->out_len = 0;
}

/* the read's next bytes from the device; false when it refuses */
static bool swi2c_fetch(struct swi2c *b, bool first)
{
    struct swi2c_msg m[2] = {
        {.addr = b->slave >> 1, .read = false, .buf = b->out, .len = b->out_len},
        {.addr = b->slave >> 1, .read = true, .buf = b->in, .len = sizeof(b->in)},
    };
    int skip = 1;

    if (first)
        b->sub_valid = false;
    if (first && b->pending && b->out_len) {
        skip = 0;
        b->sub_valid = b->out_len == 2;
        if (b->sub_valid)
            memcpy(b->sub, b->out, 2);
    } else if (!first && b->sub_valid) {
        unsigned int next = ((unsigned int)b->sub[0] << 8 | b->sub[1]) + b->in_len;

        b->sub[0] = next >> 8;
        b->sub[1] = next;
        memcpy(b->out, b->sub, 2);
        m[0].len = 2;
        skip = 0;
    } else if (!first) {
        return false; /* a read without a sub-address does not continue */
    }
    b->pending = false;
    b->out_len = 0;
    memset(b->in, 0xff, sizeof(b->in));
    b->messages++;
    if (b->xfer(b->ctx, m + skip, 2 - skip) < 0) {
        b->refused++;
        return false;
    }
    b->in_pos = 0;
    b->in_len = sizeof(b->in);
    return true;
}

static void swi2c_next_byte(struct swi2c *b)
{
    if (b->in_pos >= b->in_len && !swi2c_fetch(b, false)) {
        b->in_pos = 0;
        b->in_len = 0;
        b->cur = 0xff;
    } else {
        b->cur = b->in[b->in_pos++];
    }
    b->bits = 0;
    b->s_sda = b->cur >> 7;
}

static void swi2c_start(struct swi2c *b)
{
    b->starts++;
    if (b->phase == SWI2C_WRITE && b->out_len)
        b->pending = true; /* a repeated START: a read of it may follow */
    else
        b->out_len = 0;
    b->phase = SWI2C_ADDR;
    b->ack = false;
    b->bits = 0;
    b->byte = 0;
    b->s_sda = 1;
}

static void swi2c_stop(struct swi2c *b)
{
    b->stops++;
    if (b->phase == SWI2C_WRITE)
        b->pending = true;
    swi2c_flush(b);
    b->phase = SWI2C_IDLE;
    b->ack = false;
    b->s_sda = 1;
}

/* SCL rises: the receiver samples SDA */
static void swi2c_rise(struct swi2c *b)
{
    if (b->phase == SWI2C_IDLE || b->phase == SWI2C_IGNORE)
        return;
    if (b->ack) {
        if (b->phase == SWI2C_READ)
            b->m_nack = swi2c_sda(b);
        return;
    }
    if (b->phase == SWI2C_READ) {
        b->bits++;
        return;
    }
    b->byte = b->byte << 1 | swi2c_sda(b);
    b->bits++;
}

/* SCL falls: the transmitter sets SDA for the next bit */
static void swi2c_fall(struct swi2c *b)
{
    if (b->phase == SWI2C_IDLE || b->phase == SWI2C_IGNORE)
        return;
    if (b->ack) {
        b->ack = false;
        b->s_sda = 1;
        b->bits = 0;
        b->byte = 0;
        if (b->phase == SWI2C_READ) {
            if (b->m_nack)
                b->phase = SWI2C_IGNORE; /* the last byte: the master STOPs or STARTs */
            else
                swi2c_next_byte(b);
        }
        return;
    }
    if (b->phase == SWI2C_READ) {
        if (b->bits < 8) {
            b->s_sda = b->cur >> (7 - b->bits) & 1;
        } else {
            b->s_sda = 1; /* the master's ACK or NACK */
            b->m_nack = true;
            b->ack = true;
        }
        return;
    }
    if (b->bits < 8)
        return;
    if (b->phase == SWI2C_ADDR) {
        bool ok = (b->byte & 0xfe) == b->slave;

        if (ok && (b->byte & 1)) {
            ok = swi2c_fetch(b, true);
            if (ok) {
                b->phase = SWI2C_READ;
                b->m_nack = false;
            }
        } else if (ok) {
            swi2c_flush(b); /* a repeated START to write again: the earlier write goes */
            b->phase = SWI2C_WRITE;
        }
        if (!ok) {
            b->pending = false;
            b->out_len = 0;
            b->phase = SWI2C_IGNORE;
            return; /* no acknowledgement */
        }
    } else if (b->out_len < sizeof(b->out)) {
        b->out[b->out_len++] = b->byte;
    } else {
        b->phase = SWI2C_IGNORE; /* longer than a page: not acknowledged */
        return;
    }
    b->s_sda = 0;
    b->ack = true;
}

/* The master's pads after a write: (scl, sda) released (1) or pulled low (0). */
static void swi2c_set(struct swi2c *b, bool scl, bool sda)
{
    bool old_scl = b->m_scl, old_line = swi2c_sda(b);

    b->m_scl = scl;
    b->m_sda = sda;
    if (old_scl && scl && old_line != swi2c_sda(b)) {
        if (old_line)
            swi2c_start(b);
        else
            swi2c_stop(b);
    } else if (!old_scl && scl) {
        swi2c_rise(b);
    } else if (old_scl && !scl) {
        swi2c_fall(b);
    }
}

static void swi2c_reset(struct swi2c *b)
{
    b->m_scl = b->m_sda = b->s_sda = true;
    b->phase = SWI2C_IDLE;
    b->ack = b->pending = false;
    b->out_len = b->in_len = b->in_pos = 0;
}

/* a pad's register byte: bit 0 OUT, bit 1 OEN (1 input), bit 2 the line's level */
#define SWI2C_PAD_OUT 0x01
#define SWI2C_PAD_OEN 0x02
#define SWI2C_PAD_IN  0x04

static bool swi2c_pad_released(u8 reg)
{
    return (reg & SWI2C_PAD_OEN) || (reg & SWI2C_PAD_OUT);
}

/* another pad of the bank, a line with nothing on it but its pull-up: high unless the pad drives
 * it low (libapiSWI2C's other buses: RELEASE.cfg's R9531 HDCP repeater on bus 1, pads 63 and 64,
 * is not there, and its addresses go unacknowledged at once, as with the bus unmodelled) */
static u8 swi2c_pullup(u8 reg)
{
    return (reg & ~SWI2C_PAD_IN) | (swi2c_pad_released(reg) ? SWI2C_PAD_IN : 0);
}

/* the master wrote its pad bytes (sda, scl); returns them with their IN bits */
static void swi2c_pads(struct swi2c *b, u8 *sda, u8 *scl)
{
    swi2c_set(b, swi2c_pad_released(*scl), swi2c_pad_released(*sda));
    *sda = (*sda & ~SWI2C_PAD_IN) | (swi2c_sda(b) ? SWI2C_PAD_IN : 0);
    *scl = (*scl & ~SWI2C_PAD_IN) | (b->m_scl ? SWI2C_PAD_IN : 0);
}
/* ---- end of the software I2C slave ---- */

static char *swi2c;
module_param(swi2c, charp, 0444);
MODULE_PARM_DESC(swi2c, "LM14's software I2C bus: device:slave:sda_reg:scl_reg (lgemu.ko's device "
                        "at its 8-bit address, the GPIO pads' RIU byte addresses, PM-based)");

static struct swi2c swi2c_bus;
static enum lgemu_i2c_device swi2c_dev = LGEMU_I2C_NONE;
static u64 swi2c_sda_pa, swi2c_scl_pa, swi2c_bank_pa;
static DEFINE_MUTEX(swi2c_lock);

static int swi2c_xfer(void *ctx, struct swi2c_msg *msgs, int n)
{
    struct i2c_msg m[2];

    for (int i = 0; i < n; i++)
        m[i] = (struct i2c_msg){.addr = msgs[i].addr,
                                .flags = msgs[i].read ? I2C_M_RD : 0,
                                .len = msgs[i].len,
                                .buf = msgs[i].buf};
    return lgemu_i2c_xfer(swi2c_dev, m, n) < 0 ? -EREMOTEIO : 0;
}

static u64 swi2c_pa(u32 reg)
{
    return 0x1f000000ULL + 2 * (reg & ~1U) + (reg & 1);
}

int swi2c_parse(void)
{
    u32 v[3];

    if (!swi2c)
        return 0;
    if (lgemu_i2c_parse(swi2c, &swi2c_dev, v, 3) || v[0] > 0xff || (v[0] & 1)) {
        swi2c_dev = LGEMU_I2C_NONE;
        goto bad;
    }
    swi2c_sda_pa = swi2c_pa(v[1]);
    swi2c_scl_pa = swi2c_pa(v[2]);
    swi2c_bank_pa = swi2c_pa(v[1] & ~0xffU); /* the pads' bank: 0x100 bytes, 0x200 of the page */
    if ((swi2c_sda_pa & PAGE_MASK) != (swi2c_scl_pa & PAGE_MASK) || swi2c_sda_pa == swi2c_scl_pa ||
        (v[1] & ~0xffU) != (v[2] & ~0xffU) ||
        (swi2c_bank_pa & PAGE_MASK) != (swi2c_sda_pa & PAGE_MASK)) {
        swi2c_dev = LGEMU_I2C_NONE;
        goto bad;
    }
    swi2c_bus.xfer = swi2c_xfer;
    swi2c_bus.slave = v[0];
    swi2c_reset(&swi2c_bus);
    pr_info("swi2c: %s: SDA %#llx SCL %#llx\n", swi2c, swi2c_sda_pa, swi2c_scl_pa);
    return 0;
bad:
    pr_err("swi2c=%s: not device:slave:sda_reg:scl_reg\n", swi2c);
    return -EINVAL;
}

bool swi2c_enabled(void)
{
    return swi2c_dev != LGEMU_I2C_NONE;
}

u64 swi2c_page(void)
{
    return swi2c_sda_pa & PAGE_MASK;
}

/* the pads after reset: inputs (released), their lines high on the pull-ups */
void swi2c_reset_pads(u8 *page)
{
    page[offset_in_page(swi2c_sda_pa)] = SWI2C_PAD_OEN | SWI2C_PAD_IN;
    page[offset_in_page(swi2c_scl_pa)] = SWI2C_PAD_OEN | SWI2C_PAD_IN;
}

/* Caller: lm14_mmio_access, for the pads' page. The bank's other pads are lines on pull-ups
 * (swi2c_pullup) when written; the rest of the page is storage. Every access to the page traps, so
 * a pad that read low for ever would cost libapiSWI2C's 5000-poll waits in traps (seconds a
 * transaction, which let RELEASE's tHDCP22Task outrun the R9531 init it waits 5 s for). */
int swi2c_access(u64 addr, unsigned int width, bool write, u32 *value)
{
    u8 *page = lgemu_store_ptr(mmio_store(), addr & PAGE_MASK);
    unsigned int off = offset_in_page(addr);
    u8 *sda = page + offset_in_page(swi2c_sda_pa), *scl = page + offset_in_page(swi2c_scl_pa);

    if (!page || (width != 1 && width != 2 && width != 4) || off + width > PAGE_SIZE)
        return -EINVAL;
    mutex_lock(&swi2c_lock);
    if (write) {
        memcpy(page + off, value, width);
        for (unsigned int i = 0; i < width; i++) {
            u64 pa = addr + i;

            if (pa >= swi2c_bank_pa && pa < swi2c_bank_pa + 0x200 && pa != swi2c_sda_pa &&
                pa != swi2c_scl_pa)
                page[off + i] = swi2c_pullup(page[off + i]);
        }
        if ((addr <= swi2c_sda_pa && addr + width > swi2c_sda_pa) ||
            (addr <= swi2c_scl_pa && addr + width > swi2c_scl_pa))
            swi2c_pads(&swi2c_bus, sda, scl); /* a pad was written */
    } else {
        /* the IN bits follow the lines (the slave's SDA included) */
        *sda = (*sda & ~SWI2C_PAD_IN) | (swi2c_sda(&swi2c_bus) ? SWI2C_PAD_IN : 0);
        *scl = (*scl & ~SWI2C_PAD_IN) | (swi2c_bus.m_scl ? SWI2C_PAD_IN : 0);
        *value = 0;
        memcpy(value, page + off, width);
    }
    mutex_unlock(&swi2c_lock);
    return 0;
}

static ssize_t swi2c_state_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    char state[160];
    int len;

    mutex_lock(&swi2c_lock);
    len = scnprintf(state, sizeof(state),
                    "phase %d scl %d sda %d starts %u stops %u messages %u refused %u\n",
                    swi2c_bus.phase, swi2c_bus.m_scl, swi2c_sda(&swi2c_bus), swi2c_bus.starts,
                    swi2c_bus.stops, swi2c_bus.messages, swi2c_bus.refused);
    mutex_unlock(&swi2c_lock);
    return simple_read_from_buffer(buf, n, pos, state, len);
}
const struct proc_ops swi2c_state_proc_ops = {
    .proc_read = swi2c_state_read,
    .proc_lseek = default_llseek,
};
