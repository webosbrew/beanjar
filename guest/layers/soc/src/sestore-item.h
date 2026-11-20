// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Thin clients of the immutable kernel store. 0 = found, 1 = absent, -1 = device/read failure.
 * A device that cannot be opened or is not provisioned is a failure. */
#ifndef EMULATOR_SESTORE_ITEM_H
#define EMULATOR_SESTORE_ITEM_H
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include "../../../../include/webos-sestore.h"

static inline int emulator_sestore_read(const char *id, unsigned char *buf, unsigned int *len,
                                        size_t capacity)
{
    struct webos_sestore_get request = {0};
    size_t n;
    int fd, result, saved;
    if (!id || !len || !(n = strlen(id)) || n > WEBOS_SESTORE_MAX_NAME)
        return -1;
    memcpy(request.id, id, n);
    request.data = (uintptr_t)buf;
    request.capacity =
        capacity > WEBOS_SESTORE_MAX_VALUE ? WEBOS_SESTORE_MAX_VALUE : (unsigned int)capacity;
    fd = open(WEBOS_SESTORE_DEVICE, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -1;
    do {
        result = ioctl(fd, WEBOS_SESTORE_GET, &request);
    } while (result < 0 && errno == EINTR);
    saved = errno;
    close(fd);
    if (result < 0)
        return saved == ENOENT ? 1 : -1;
    *len = request.bytes;
    return 0;
}

#endif
