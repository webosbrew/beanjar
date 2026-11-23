// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef WEBOS_IR_H
#define WEBOS_IR_H
#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdint.h>
#endif
/* The TV remote's infrared on the webos.ir virtio-serial port: from the viewer (host), through the
 * initramfs's pump (guest/initramfs/src/virtio-ir.c) to the micom model in lgemu.ko
 * (guest/kernel/modules/lgemu/ i2c.c), which reports it to tvservice's MICOM_Task as the real micom
 * does.
 *
 * A stream of 4-byte records. Each is one edge of a button on an LG (NEC-coded) remote:
 *   magic   WEBOS_IR_MAGIC
 *   down    1: the button goes down (the remote sends its code frame); 0: it comes up
 *   custom  the NEC custom (address) code: LG TVs answer WEBOS_IR_CUSTOM_LG only
 *   code    the NEC data code, LG's key code (tvservice maps it to a Linux key with _gKeyTable)
 * The viewer sends no repeats: while a button is down the micom model repeats it, as the remote
 * sends NEC repeat frames while a button is held. A closed connection releases a held button. */
#define WEBOS_IR_MAGIC     0x49 /* 'I' */
#define WEBOS_IR_CUSTOM_LG 0x04
struct webos_ir_record {
    uint8_t magic, down, custom, code;
};
_Static_assert(sizeof(struct webos_ir_record) == 4, "IR wire record");

/* A Japanese (ISDB) remote's extended code on the same stream: a record with WEBOS_IR_EXT_MAGIC,
 * custom WEBOS_IR_CUSTOM_LG, and code the code's second byte N. The remote's broadcast-type keys
 * and their one-touch 1-12 send two bytes, 0xDB N, instead of a data code and its complement; the
 * micom reports them as event, 0xDB, N (docs/research/arib-data-broadcasting.md, "JP remote
 * infrared codes"): BS DB 00, CS DB 10, terrestrial DB 30, BS4K DB 40, then 1-12 of each type
 * at + 0x01 to + 0x0C. The host names such a code WEBOS_IR_EXT(N), 0xDB00 | N, beside the data
 * codes 0 to 255. */
#define WEBOS_IR_EXT_MAGIC  0x45 /* 'E' */
#define WEBOS_IR_EXT_PREFIX 0xDB
#define WEBOS_IR_EXT(n)     ((WEBOS_IR_EXT_PREFIX << 8) | (n))

/* The set's own keys (the local key) on the same stream: a record with WEBOS_LOCAL_MAGIC, custom 0,
 * and code the micom's local key code of the physical button. They are not infrared: the micom
 * reads the buttons itself and reports them as its local key events (lgemu's i2c.c, "the local
 * key"), which decides a short press from a long one as the micom's firmware does. The viewer sends
 * the edges only.
 *   WEBOS_LOCAL_PRESS      the one key of a onekey set, the centre of a joystick: the micom reports
 *                          a tap as 0xEA (webOS IR_FRONT_ONEKEY_SHORT, or IR_FRONTKEY_PRESS on a
 *                          joystick) and a hold as 0xEB (_LONG, _LONG_PRESS)
 *   WEBOS_LOCAL_UP..LEFT   a joystick's directions, the micom's codes 0..3, which micomservice
 *                          turns into CH+, CH-, VOL+, VOL- (920-923). Which physical direction
 *                          sends which code is not in the firmware: up/down as channel and
 *                          right/left as volume is inferred from LG's usual joystick layout, not
 *                          verified on a set. */
#define WEBOS_LOCAL_MAGIC 0x4B /* 'K' */
#define WEBOS_LOCAL_PRESS 0xEA
#define WEBOS_LOCAL_LONG  0xEB
#define WEBOS_LOCAL_UP    0x00
#define WEBOS_LOCAL_DOWN  0x01
#define WEBOS_LOCAL_RIGHT 0x02
#define WEBOS_LOCAL_LEFT  0x03

/* The set's standby, the other way: the pump reads /dev/lgemu-ir, one byte each time it changes
 * (guest/kernel/modules/lgemu/i2c.c, "standby and the power line": the SoC suspended, from the
 * kernel's suspend entry to its exit), and holds the webos.standby port open while it is
 * WEBOS_IR_STATUS_STANDBY, which the host sees as the port's frontend-open in QMP
 * query-chardev (chardev "standby"). There the power key is QMP system_powerdown, QEMU virt's power
 * button, which is the micom's wake line. */
#define WEBOS_IR_STATUS_ON      0
#define WEBOS_IR_STATUS_STANDBY 1

/* A read of n >= sizeof(struct webos_micom_status) from /dev/lgemu-ir has the whole state, of
 * which the standby byte is the first (a one-byte read has it alone): the micom's power state,
 * whether the firmware's power stack has started, and the front LED, the last payload the firmware
 * wrote with each LED command (libmicom_led), which the micom keeps; it changes with each of them.
 * The pump writes it to the host as a line of text on the webos.micom port
 * (guest/initramfs/src/virtio-ir.c), the viewer's front LED and the manager's power-off check. */
#define WEBOS_MICOM_LED_CMDS  9
#define WEBOS_MICOM_LED_BYTES 3
struct webos_micom_led {
    uint8_t cmd;    /* the micom command */
    uint8_t len;    /* its argument bytes the last time (0 for a bare command) */
    uint8_t writes; /* how many times written, wrapping; 0: never */
    uint8_t data[WEBOS_MICOM_LED_BYTES];
};
struct webos_micom_status {
    uint8_t standby; /* WEBOS_IR_STATUS_* */
    uint8_t power;   /* the micom's power state, read 0xB1: 3 active, 2 warm standby */
    uint8_t ready;   /* 1 once the firmware has read the power state (0xB1), as tvpowerd does when
                      * it starts: webOS's power stack runs. 0 until then, from the cold boot */
    struct webos_micom_led led[WEBOS_MICOM_LED_CMDS];
};
_Static_assert(sizeof(struct webos_micom_status) == 3 + 6 * WEBOS_MICOM_LED_CMDS, "micom status");

/* The LG data codes the viewer's PC keyboard sends for the remote's own functions
 * (host/viewer/README.md, "PC keyboard"); the remote windows' codes are in
 * beanjar_ui/viewer/remote.py. */
#define WEBOS_IR_VOLUMEUP   0x02
#define WEBOS_IR_VOLUMEDOWN 0x03
#define WEBOS_IR_MUTE       0x09
#define WEBOS_IR_BACK       0x28
#define WEBOS_IR_HOME       0x7C
#endif
