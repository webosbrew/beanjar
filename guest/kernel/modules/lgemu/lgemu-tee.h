/* SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors */
/* SPDX-License-Identifier: GPL-2.0-only */
/* lgemu.ko's TEE (tee.c: /dev/tee0, OP-TEE's user ABI) and the trusted applications a SoC driver
 * module stands in for behind it (rtk.ko's tee-ta.c). */
#ifndef LGEMU_TEE_H
#define LGEMU_TEE_H

#include <linux/types.h>

/* GlobalPlatform's TEE Client API values */
#define TEEC_SUCCESS              0x00000000u
#define TEEC_ERROR_GENERIC        0xFFFF0000u
#define TEEC_ERROR_BAD_PARAMETERS 0xFFFF0006u
#define TEEC_ERROR_ITEM_NOT_FOUND 0xFFFF0008u
#define TEEC_ERROR_NOT_SUPPORTED  0xFFFF000Au
#define TEEC_ERROR_OUT_OF_MEMORY  0xFFFF000Cu
#define TEEC_ERROR_SHORT_BUFFER   0xFFFF0010u
#define TEEC_ORIGIN_TEE           3
#define TEEC_ORIGIN_TRUSTED_APP   4

/* a parameter as a TA sees it: type is TEE_IOCTL_PARAM_ATTR_TYPE_*; a value's a and b; a memref's
 * buffer (in the client's shared memory) and size, which an output memref sets to what it wrote
 * (or, with TEEC_ERROR_SHORT_BUFFER, to what it needs) */
struct lgemu_ta_param {
    u32 type;
    u32 a, b;
    void *buf;
    size_t size;
};

struct lgemu_ta {
    const char *name;
    u8 uuid[16];
    /* called under tee.c's lock; *state is the session's */
    u32 (*open)(void **state, struct lgemu_ta_param *p);
    u32 (*invoke)(void *state, u32 cmd, struct lgemu_ta_param *p);
    void (*close)(void *state);
};

/* a SoC driver module's TAs: find gives the TA of a UUID, or NULL; unknown is told of a UUID
 * without one (which the client gets TEEC_ERROR_ITEM_NOT_FOUND for), both under tee.c's lock */
struct lgemu_tee_tas {
    const struct lgemu_ta *(*find)(const u8 *uuid);
    void (*unknown)(const u8 *uuid);
};

/* /dev/tee0, made by the first add and gone with the last remove; tas NULL: none of a module's
 * own (lgemu's tee=1). One module's TAs at a time: -EBUSY for a second. */
int lgemu_tee_add(const struct lgemu_tee_tas *tas);
void lgemu_tee_remove(const struct lgemu_tee_tas *tas);

#endif
