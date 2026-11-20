// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* SESTORE_ReadData over the VM's kernel store: exact names only, as a real store answers. An item
 * the VM does not provide is "no such item" (0xFFFF0002) whatever the missing policy: LG's library
 * never initialises in a VM (SESTORE_Init 0xFFFF0000, no LG-written store), and its ReadData
 * crashes on the second call in a process after that. Every other SESTORE_* stays LG's. */
#include "../sestore-item.h"
#define SESTORE_FAIL 0xFFFF0002u

int SESTORE_ReadData(const char *id, unsigned char *buf, unsigned int *len)
{
    return emulator_sestore_read(id, buf, len, WEBOS_SESTORE_MAX_VALUE) ? (int)SESTORE_FAIL : 0;
}
