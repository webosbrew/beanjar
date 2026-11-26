// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The DLP controller of LG's HU710P projector (i2c.c's device "dlpc"): a TI DLPC6540, which W21Z's
 * panelcontroller calls DPP6540 and reaches through its DILE_I2C row DPP6540, /dev/i2c-3 at 8-bit
 * slave 0x34 (7-bit 0x1a; TI's default address, DLPU110B section 15.2).
 *
 * The protocol is TI's (DLPC6540 Software Programmer's Guide, DLPU110B, chapter 16): a command
 * packet is a header byte (bits 0-2 the destination, bit 3 a 2-byte opcode, bit 4 a 2-byte
 * little-endian length after the opcode, bit 6 reply requested, bit 7 a read), the opcode, the
 * length if present, the data. A read is that packet written, then the response read after a
 * repeated start or in a transfer of its own: the response header (the destination, bit 4 when
 * the response has a length field, bit 6 an error, bit 7 busy), the length if present, the data.
 * The controller pulls its HOST_IRQ/SYSTEM_BUSY GPIO high while it runs a command and low when it
 * is free (DLPU110B sections 4.6, 16.5.1): W21Z's line is GPIO 137, which every DPP6540 access of
 * panelcontroller waits to read 0 (50 ms polls, without a time limit), so the board leaves it
 * low.
 *
 * What panelcontroller sends (W21Z 03.00.27, docs/research/non-dtv-devices.md):
 *   writes   header 04 (destination 4, a write) and an opcode, with the settings as data, and
 *            other headers with two-byte register addresses (edge adjustment, keystone);
 *            acknowledged and dropped.
 *   84 03    Flash Version (destination 4, opcode 03h, DLPU110B table 19-10), read 1 byte
 *            (DPP6540_Get_DPPStatus: only whether the read worked) or 20 (DPP6540_Get_Version:
 *            the first byte 04, the response header, then major, minor, subminor).
 *   81 80    destination 1, opcode 80h, read 4 bytes (DPP6540_Get_InitLEDOnStatus): not a command
 *            of the guide's; panelcontroller logs the light on unless byte 0 is 0 after five tries.
 *   DA FF FE 06 00 85 01 00 04 00 03   a read on destination 2 (extended commands), opcode FEFFh,
 *            six data bytes, its response read alone as 7 bytes (DPP6540_Get_DMDErrorStatus):
 *            byte 3, the first data byte after the header and length, is the DMD error status,
 *            0 on a healthy set; panelcontroller restarts the set after six readings of 1.
 *
 * The model answers every read command with a response of no error and not busy: the header with
 * the command's destination (and its length bit, the length the rest of the read), and data of
 * zeros but the Flash Version's and the light's status. The flash version (1.0.0) is not a
 * measured one. dlpc_trace=1 logs each command and response. */
#define pr_fmt(fmt) KBUILD_MODNAME ": dlpc: " fmt
#include <linux/i2c.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/mutex.h>
#include <linux/string.h>
#include "lgemu.h"
#include "lgemu-i2c.h"

/* ---- the DLP controller's command protocol (tests/unit/kernel/test_lgemu_dlpc.py compiles this
 * part on the host) ---- */

#define DLPC_READ        0x80 /* header bit 7: a read command; in a response, busy */
#define DLPC_REPLY       0x40 /* header bit 6: reply requested; in a response, an error */
#define DLPC_LENGTH      0x10 /* header bit 4: a 2-byte length after the opcode */
#define DLPC_OPCODE16    0x08 /* header bit 3: a 2-byte opcode */
#define DLPC_DEST        0x07
#define DLPC_RESPONSE    32 /* the longest response the model keeps */
#define DLPC_DEST_SYS    4
#define DLPC_FLASH_VER   0x03 /* destination 4: Flash Version (DLPU110B table 19-10) */
#define DLPC_DEST_COMMON 1
#define DLPC_LED_STATUS  0x80 /* destination 1: LG's light status (not in DLPU110B) */
#define DLPC_LED_ON      0xff

struct dlpc {
    u8 response[DLPC_RESPONSE]; /* the response to the last read command */
    u8 response_len;            /* its length so far (header and data); 0: none pending */
    bool length_field;          /* the response carries a length after its header */
    u8 dest;
    u16 opcode;
};

static void dlpc_reset(struct dlpc *c)
{
    memset(c, 0, sizeof(*c));
}

/* A command packet written: a read command sets the response that the next read returns, any
 * other packet drops a pending one (the controller discards a response when new bytes come,
 * DLPU110B 16.2); an empty write (a read's missing subaddress) changes nothing. */
static void dlpc_write(struct dlpc *c, const u8 *buf, unsigned int len)
{
    unsigned int op_len;

    if (!len)
        return;
    c->response_len = 0;
    if (!(buf[0] & DLPC_READ))
        return;
    op_len = buf[0] & DLPC_OPCODE16 ? 2 : 1;
    if (len < 1 + op_len)
        return;
    c->dest = buf[0] & DLPC_DEST;
    c->opcode = buf[1] | (op_len == 2 ? buf[2] << 8 : 0);
    c->length_field = buf[0] & DLPC_LENGTH;
    memset(c->response, 0, sizeof(c->response));
    c->response[0] = c->dest | (c->length_field ? DLPC_LENGTH : 0);
    c->response_len = 1;
    if (c->dest == DLPC_DEST_SYS && c->opcode == DLPC_FLASH_VER) {
        c->response[1] = 1; /* major, minor, subminor: 1.0.0 */
        c->response_len = 4;
    } else if (c->dest == DLPC_DEST_COMMON && c->opcode == DLPC_LED_STATUS) {
        c->response[0] = DLPC_LED_ON;
        c->response_len = 4;
    }
}

/* A read: the pending response, its length field (if it has one) the bytes the read takes after
 * it, zeros past its data; zeros without one. */
static void dlpc_read(const struct dlpc *c, u8 *buf, unsigned int len)
{
    memset(buf, 0, len);
    if (!c->response_len)
        return;
    memcpy(buf, c->response, min_t(unsigned int, len, DLPC_RESPONSE));
    if (c->length_field && len >= 3) {
        unsigned int data = len - 3;

        memmove(buf + 3, buf + 1, min_t(unsigned int, data, DLPC_RESPONSE - 3));
        buf[1] = data & 0xff;
        buf[2] = data >> 8;
    }
}

/* ---- end of the DLP controller's command protocol ---- */

static DEFINE_MUTEX(dlpc_lock);
static struct dlpc chip;
static bool chip_reset;
static bool dlpc_trace;
module_param(dlpc_trace, bool, 0644);
MODULE_PARM_DESC(dlpc_trace, "dlpc: log every command packet to the DLP controller and its reply");

int lgemu_dlpc_xfer(struct i2c_msg *msgs, int num)
{
    mutex_lock(&dlpc_lock);
    if (!chip_reset) {
        dlpc_reset(&chip);
        chip_reset = true;
    }
    for (int i = 0; i < num; i++) {
        if (msgs[i].flags & I2C_M_RD) {
            dlpc_read(&chip, msgs[i].buf, msgs[i].len);
            if (dlpc_trace)
                pr_info("read %u: %*ph\n", msgs[i].len, min_t(int, msgs[i].len, 32), msgs[i].buf);
        } else {
            if (dlpc_trace && msgs[i].len)
                pr_info("write %u: %*ph\n", msgs[i].len, min_t(int, msgs[i].len, 32), msgs[i].buf);
            dlpc_write(&chip, msgs[i].buf, msgs[i].len);
        }
    }
    mutex_unlock(&dlpc_lock);
    return 0;
}
