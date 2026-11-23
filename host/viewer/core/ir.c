// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#define _POSIX_C_SOURCE 200809L
#include "ir.h"
#include "../../../include/webos-ir.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static void disconnect(struct ir *ir)
{
    if (ir->fd >= 0)
        close(ir->fd);
    ir->fd = -1;
}
static int reconnect(struct ir *ir)
{
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    disconnect(ir);
    if (!ir->path || strlen(ir->path) >= sizeof addr.sun_path)
        return -1;
    strcpy(addr.sun_path, ir->path);
    ir->fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (ir->fd < 0)
        return -1;
    if (connect(ir->fd, (void *)&addr, sizeof addr) < 0) {
        disconnect(ir);
        return -1;
    }
    return 0;
}
void ir_init(struct ir *ir, const char *path)
{
    memset(ir, 0, sizeof *ir);
    ir->fd = -1;
    ir->held = ir->local_held = -1;
    ir->ext_type = IR_EXT_TERRESTRIAL;
    ir->path = path ? strdup(path) : NULL;
    reconnect(ir);
}
/* One record; a connection found closed (the VM restarted) is made again once. */
static int send_record(struct ir *ir, unsigned code, int down, int local)
{
    int ext = !local && code > 0xff;
    struct webos_ir_record r = {local ? WEBOS_LOCAL_MAGIC
                                : ext ? WEBOS_IR_EXT_MAGIC
                                      : WEBOS_IR_MAGIC,
                                down ? 1 : 0, local ? 0 : WEBOS_IR_CUSTOM_LG, (uint8_t)code};
    for (int attempt = 0; attempt < 2; attempt++) {
        if (ir->fd < 0 && reconnect(ir) < 0)
            break;
        ssize_t n;
        do
            n = send(ir->fd, &r, sizeof r, MSG_NOSIGNAL);
        while (n < 0 && errno == EINTR);
        if (n == (ssize_t)sizeof r) {
            ir->sent++;
            return 0;
        }
        disconnect(ir);
    }
    ir->errors++;
    fprintf(stderr, "ir: no connection to %s: %s 0x%02x %s not sent\n",
            ir->path ? ir->path : "(no socket)", local ? "local key" : "button", code,
            down ? "down" : "up");
    return -1;
}
int ir_code_valid(unsigned code)
{
    return code <= 0xff || (code >> 8) == WEBOS_IR_EXT_PREFIX;
}
/* A button edge as it goes out: one button down at a time. */
static int button_edge(struct ir *ir, unsigned code, int down)
{
    if (down) {
        if (ir->held == (int)code)
            return 0;
        if (ir->held >= 0)
            ir_release(ir);
        ir->held = (int)code;
        return send_record(ir, code, 1, 0);
    }
    if (ir->held != (int)code)
        return 0;
    ir->held = -1;
    return send_record(ir, code, 0, 0);
}
int ir_button(struct ir *ir, unsigned code, int down)
{
    if (!ir_code_valid(code))
        return -1;
    if ((code >> 8) != WEBOS_IR_EXT_PREFIX)
        return button_edge(ir, code, down);
    unsigned n = code & 0xff, one_touch = n & 0x0f;
    if (one_touch == 0x0b || one_touch == 0x0c) /* 11 and 12 of the type last selected */
        return button_edge(ir, WEBOS_IR_EXT(ir->ext_type | one_touch), down);
    int r = button_edge(ir, code, down);
    if (!one_touch && down) /* a broadcast-type key: after the release of what was down */
        ir->ext_type = n & 0xf0;
    return r;
}
int ir_local_key(struct ir *ir, unsigned code, int down)
{
    code &= 0xff;
    if (down) {
        if (ir->local_held == (int)code)
            return 0;
        if (ir->local_held >= 0)
            send_record(ir, (unsigned)ir->local_held, 0, 1);
        ir->local_held = (int)code;
        return send_record(ir, code, 1, 1);
    }
    if (ir->local_held != (int)code)
        return 0;
    ir->local_held = -1;
    return send_record(ir, code, 0, 1);
}
void ir_release(struct ir *ir)
{
    if (ir->held >= 0)
        button_edge(ir, (unsigned)ir->held, 0);
    if (ir->local_held >= 0)
        ir_local_key(ir, (unsigned)ir->local_held, 0);
}
void ir_close(struct ir *ir)
{
    ir_release(ir);
    disconnect(ir);
    free(ir->path);
    ir->path = NULL;
}
