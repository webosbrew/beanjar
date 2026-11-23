// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The debug status's cipher, for a chip whose tvservice keeps the status in the EEPROM as 32
 * bytes encrypted by the SoC (W18R, W19P: DIL_NVM_Load_DebugStatus reads EEPROM 0x1C6 and calls
 * DILE_CRYPTO_DecryptDbgData, DIL_NVM_Store_DebugStatus calls DILE_CRYPTO_EncryptDbgData before
 * it writes there). Stock passes both to HAL_CRYPTO_*DbgData, which needs the SoC's TEE: MStar's
 * Customer AES command on M3, an OP-TEE TA on K5LP, neither of which the emulator has, so every
 * decrypt failed and tvservice fell back to RELEASE whatever the VM's debug mode.
 *
 * On the emulator the cipher is the identity: the EEPROM holds the clear block, which beanjar
 * programs in the firmware's own encoding (host/core/beanjar/nvmdebug.py), and the firmware's own
 * Store writes clear blocks back. Decrypt returns what the EEPROM holds, nothing made up.
 * The signatures are the stock library's (W18R's and W19P's libdile_crypto.so.0.1.1, and the
 * Starfish SDK's dile_crypto.h): in and out of DBG_DATA_SIZE bytes, 0 on success, -1 otherwise. */
#include <string.h>

#define DBG_DATA_SIZE 32

int DILE_CRYPTO_EncryptDbgData(unsigned char in[DBG_DATA_SIZE], unsigned char out[DBG_DATA_SIZE])
{
    if (!in || !out)
        return -1;
    memmove(out, in, DBG_DATA_SIZE);
    return 0;
}

int DILE_CRYPTO_DecryptDbgData(unsigned char in[DBG_DATA_SIZE], unsigned char out[DBG_DATA_SIZE])
{
    if (!in || !out)
        return -1;
    memmove(out, in, DBG_DATA_SIZE);
    return 0;
}
