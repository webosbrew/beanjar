// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The remote's infrared records (host/viewer/core/ir.c, include/webos-ir.h) on a socket that
 * stands in for ir.sock: a data code is a WEBOS_IR_MAGIC record, a Japanese remote's extended
 * code (0xDB00 | N) a WEBOS_IR_EXT_MAGIC record with N; codes that are neither are refused; the
 * one-touch 11 and 12 follow the broadcast type last selected, terrestrial at first, and a type
 * key pressed while 11 is down releases that 11 as it was sent. */
#define _POSIX_C_SOURCE 200809L
#include "../../../host/viewer/core/ir.h"
#include "../../../include/webos-ir.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static int failures;
static int peer;

static void expect(const char *what, int magic, int down, int code)
{
    struct webos_ir_record r;
    ssize_t n = recv(peer, &r, sizeof r, MSG_DONTWAIT);
    if (n != (ssize_t)sizeof r || r.magic != magic || r.down != down || r.code != code ||
        r.custom != WEBOS_IR_CUSTOM_LG) {
        printf("FAIL %s: got %zd bytes %02x %d %02x %02x, want %02x %d 04 %02x\n", what, n,
               n == 4 ? r.magic : 0, n == 4 ? r.down : 0, n == 4 ? r.custom : 0,
               n == 4 ? r.code : 0, magic, down, code);
        failures++;
    }
}
static void nothing(const char *what)
{
    char b[4];
    if (recv(peer, b, sizeof b, MSG_DONTWAIT) > 0) {
        printf("FAIL %s: a record was sent\n", what);
        failures++;
    }
}
static void press(struct ir *ir, unsigned code)
{
    ir_button(ir, code, 1);
    ir_button(ir, code, 0);
}

int main(void)
{
    char dir[] = "/tmp/check-ir-XXXXXX", path[64];
    if (!mkdtemp(dir))
        return 2;
    snprintf(path, sizeof path, "%s/ir.sock", dir);
    int l = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    strcpy(a.sun_path, path);
    if (l < 0 || bind(l, (void *)&a, sizeof a) || listen(l, 1))
        return 2;
    struct ir ir;
    ir_init(&ir, path);
    peer = accept(l, NULL, NULL);
    if (peer < 0)
        return 2;

    press(&ir, 0x9f); /* d: a plain data code */
    expect("d down", WEBOS_IR_MAGIC, 1, 0x9f);
    expect("d up", WEBOS_IR_MAGIC, 0, 0x9f);
    if (ir_button(&ir, 0x1ff, 1) != -1 || ir_button(&ir, 0xdc00, 1) != -1) {
        printf("FAIL codes that are neither a data code nor 0xDBxx are accepted\n");
        failures++;
    }
    nothing("invalid codes");
    if (!ir_code_valid(0xdb4c) || !ir_code_valid(0xff) || ir_code_valid(0x100)) {
        printf("FAIL ir_code_valid\n");
        failures++;
    }
    press(&ir, WEBOS_IR_EXT(0x3b)); /* 11 before any type key: terrestrial */
    expect("11 down (terrestrial)", WEBOS_IR_EXT_MAGIC, 1, 0x3b);
    expect("11 up (terrestrial)", WEBOS_IR_EXT_MAGIC, 0, 0x3b);
    press(&ir, WEBOS_IR_EXT(0x00)); /* BS */
    expect("BS down", WEBOS_IR_EXT_MAGIC, 1, 0x00);
    expect("BS up", WEBOS_IR_EXT_MAGIC, 0, 0x00);
    press(&ir, WEBOS_IR_EXT(0x3c)); /* the layout's 12: now BS 12 */
    expect("12 down (BS)", WEBOS_IR_EXT_MAGIC, 1, 0x0c);
    expect("12 up (BS)", WEBOS_IR_EXT_MAGIC, 0, 0x0c);
    press(&ir, 0x15); /* 5: a plain digit, whatever the type */
    expect("5 down", WEBOS_IR_MAGIC, 1, 0x15);
    expect("5 up", WEBOS_IR_MAGIC, 0, 0x15);
    /* BS4K pressed while 11 (BS 11) is down: that 11 comes up as sent, then BS4K goes down */
    ir_button(&ir, WEBOS_IR_EXT(0x3b), 1);
    expect("11 down (BS)", WEBOS_IR_EXT_MAGIC, 1, 0x0b);
    ir_button(&ir, WEBOS_IR_EXT(0x40), 1);
    expect("BS 11 released", WEBOS_IR_EXT_MAGIC, 0, 0x0b);
    expect("BS4K down", WEBOS_IR_EXT_MAGIC, 1, 0x40);
    ir_button(&ir, WEBOS_IR_EXT(0x40), 0);
    expect("BS4K up", WEBOS_IR_EXT_MAGIC, 0, 0x40);
    press(&ir, WEBOS_IR_EXT(0x3b));
    expect("11 down (BS4K)", WEBOS_IR_EXT_MAGIC, 1, 0x4b);
    expect("11 up (BS4K)", WEBOS_IR_EXT_MAGIC, 0, 0x4b);
    /* held at close: released */
    ir_button(&ir, WEBOS_IR_EXT(0x10), 1);
    expect("CS down", WEBOS_IR_EXT_MAGIC, 1, 0x10);
    ir_release(&ir);
    expect("CS released", WEBOS_IR_EXT_MAGIC, 0, 0x10);
    nothing("the end");

    ir_close(&ir);
    close(peer);
    close(l);
    unlink(path);
    rmdir(dir);
    if (!failures)
        puts("IR records: data codes, extended codes, 11 and 12 per broadcast type passed");
    return failures != 0;
}
