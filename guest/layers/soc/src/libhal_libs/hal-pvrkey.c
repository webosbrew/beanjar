// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libhal_libs.so for the emulator on MStar's LM21 family: the DVR's secure key.
 *
 * The recording middleware reads the DVR's 16-byte key with HAL_PVR_CRYPTO_GetSecureKey(?, buf).
 * On a set that read goes through MStar's TEE; the VM has no TEE, so the stock read fails. This
 * definition answers it from the VM's secure-store item "dvr_key"
 * (host/core/beanjar/vm/sestore.py): 0 and the 16 bytes, or -1, the stock failure, without the
 * item. Every other symbol is the stock library's, which this one NEEDs by its full name. On M2 and
 * M3 the same key is given by src/libkadaptor/kadaptor-pvrkey.c. */
#include <string.h>
#include "../../../../../include/webos-sestore-items.h"
#include "../sestore-item.h"

#define DVR_KEY_BYTES 16

int HAL_PVR_CRYPTO_GetSecureKey(int unused, unsigned char *key)
{
    unsigned char value[DVR_KEY_BYTES];
    unsigned int size = 0;
    (void)unused;
    if (!key || emulator_sestore_read(SESTORE_ITEM_DVR_KEY, value, &size, sizeof value) ||
        size != DVR_KEY_BYTES)
        return -1;
    memcpy(key, value, DVR_KEY_BYTES);
    return 0;
}
