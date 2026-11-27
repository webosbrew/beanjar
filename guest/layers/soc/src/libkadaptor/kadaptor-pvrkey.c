// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libkadaptor.so for the emulator on M2 (W16R, beside kadaptor-xc.c) and M3 (W18R): the DVR's
 * secure key.
 *
 * The board HAL reads the DVR's 16-byte key with MAdp_Secure_GetSecureDataEMMC("/tmp/dvr_key", ...)
 * (M3: "/tmp/96B14BF8"). On a set that read goes through MStar's TEE; the VM has no TEE, so the
 * stock read fails. This definition answers those two names from the VM's secure-store item
 * "dvr_key" (host/core/beanjar/vm/sestore.py): TRUE, the 16 bytes and their length. Every other
 * item is the stock function's, and so is every other symbol (on M2 but kadaptor-xc.c's). */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <string.h>
#include <syslog.h>
#include "../../../../../include/webos-sestore-items.h"
#include "../sestore-item.h"

#define DVR_KEY_NAME    "/tmp/dvr_key"  /* M2's HAL */
#define DVR_KEY_NAME_M3 "/tmp/96B14BF8" /* M3's */
#define DVR_KEY_BYTES   16

int MAdp_Secure_GetSecureDataEMMC(const char *name, void *buf, uint32_t *len)
{
    static int (*stock)(const char *, void *, uint32_t *);
    if (name && (!strcmp(name, DVR_KEY_NAME) || !strcmp(name, DVR_KEY_NAME_M3))) {
        unsigned char key[DVR_KEY_BYTES];
        unsigned int size = 0;
        static int said;
        if (!buf || !len || *len < DVR_KEY_BYTES ||
            emulator_sestore_read(SESTORE_ITEM_DVR_KEY, key, &size, sizeof key) ||
            size != DVR_KEY_BYTES) {
            syslog(LOG_ERR, "libkadaptor (emulator): no DVR key for %s in the VM's store", name);
            return 0;
        }
        memcpy(buf, key, DVR_KEY_BYTES);
        *len = DVR_KEY_BYTES;
        if (!said++)
            syslog(LOG_INFO, "libkadaptor (emulator): DVR key %s from the VM's store", name);
        return 1;
    }
    if (!stock)
        stock = (int (*)(const char *, void *, uint32_t *))dlsym(RTLD_NEXT, __func__);
    return stock ? stock(name, buf, len) : 0;
}
