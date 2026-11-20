// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The model secret's DILE read. Stock DILE_CRYPTO_ReadMskToSecureStore first runs
 * DILE_CRYPTO_Initialize, whose SESTORE_Init never succeeds in a VM (LG's store is not there), so
 * it returns -1 before reading. This replaces only that function and does what stock does after
 * the initialisation; every other DILE_CRYPTO symbol stays the unchanged stock library's.
 *
 * From kisscurl (W21A, W21P) on, the stock library NEEDs libdile_tvregion: it reads the item of
 * DILE_TVREGION_getTVRegion()'s region (1 ATSC "_a", 2 DVB "_d", 3 ARIB "_r") through
 * SESTORE_ReadData, and the plain item when that fails or for any other region. Here the same
 * calls go to the same libraries: LG's region function and the soc layer's SESTORE_ReadData.
 * Older stock libraries read the plain item, then HAL_CRYPTO; that read comes from the kernel
 * store directly (some of those firmwares have no libsestore shim). */
#define _GNU_SOURCE
#include <dlfcn.h>
#include "../model-key.h"

typedef int (*sestore_read)(const char *, unsigned char *, unsigned int *);

static void *loaded(const char *soname, const char *symbol)
{
    /* Both are the stock library's own dependencies, so already loaded, whatever the scope. */
    void *lib = dlopen(soname, RTLD_NOW | RTLD_NOLOAD);
    return lib ? dlsym(lib, symbol) : NULL;
}

int DILE_CRYPTO_ReadMskToSecureStore(unsigned char *buf, unsigned int *len)
{
    int (*region)(void) =
        (int (*)(void))loaded("libdile_tvregion.so.1", "DILE_TVREGION_getTVRegion");
    if (!region)
        return emulator_read_model_key(buf, len);
    sestore_read read = (sestore_read)loaded("libsestore.so.0", "SESTORE_ReadData");
    if (!read)
        return -1;
    const char *item = NULL;
    switch (region()) {
    case 1:
        item = SESTORE_ITEM_SDP_MODEL_A;
        break;
    case 2:
        item = SESTORE_ITEM_SDP_MODEL_D;
        break;
    case 3:
        item = SESTORE_ITEM_SDP_MODEL_R;
        break;
    }
    if (item && !read(item, buf, len))
        return 0;
    return read(SESTORE_ITEM_SDP_MODEL, buf, len);
}
