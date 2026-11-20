// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Run against the real lgemu store, as ARM32 on arm64 and again after chroot/setuid. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include "../../../include/webos-sestore.h"

int SESTORE_ReadData(const char *, unsigned char *, unsigned int *);
int HAL_CRYPTO_ReadMskToSecureStore(unsigned char *, unsigned int *);
int DILE_CRYPTO_ReadMskToSecureStore(unsigned char *, unsigned int *);
int probe_expose_store(const char *);
void probe_clear_legacy_store(const char *);

static void error_is(int result, int expected)
{
    assert(result == -1 && errno == expected);
}

static void unready(int fd)
{
    struct webos_sestore_info info;
    struct webos_sestore_get get = {.id = "3382732D"};
    error_is(ioctl(fd, WEBOS_SESTORE_INFO, &info), ENODATA);
    error_is(ioctl(fd, WEBOS_SESTORE_GET, &get), ENODATA);
    unsigned char buf[44];
    unsigned int size = sizeof buf;
    assert(HAL_CRYPTO_ReadMskToSecureStore(buf, &size) == -1);
    assert((unsigned int)SESTORE_ReadData("3382732D", buf, &size) == 0xFFFF0002u);
    struct webos_sestore_header bad = {.missing = 2};
    struct webos_sestore_load load = {.data = (uintptr_t)&bad, .bytes = sizeof bad};
    error_is(ioctl(fd, WEBOS_SESTORE_LOAD, &load), EINVAL);
    bad.missing = 0;
    bad.count = WEBOS_SESTORE_MAX_ITEMS + 1;
    error_is(ioctl(fd, WEBOS_SESTORE_LOAD, &load), EINVAL);
    bad.count = 1; /* Missing record must not publish a partial store. */
    error_is(ioctl(fd, WEBOS_SESTORE_LOAD, &load), EINVAL);
    error_is(ioctl(fd, WEBOS_SESTORE_INFO, &info), ENODATA);
    load.data = 1;
    error_is(ioctl(fd, WEBOS_SESTORE_LOAD, &load), EFAULT);
}

static void read_checks(int fd, unsigned int policy)
{
    static const unsigned char expected[] = {0, 255, 128, 10};
    struct webos_sestore_info info;
    assert(!ioctl(fd, WEBOS_SESTORE_INFO, &info));
    assert(info.missing == policy && info.count == 4);
    struct webos_sestore_get get = {.id = "3382732D"};
    assert(!ioctl(fd, WEBOS_SESTORE_GET, &get) && get.bytes == sizeof expected);
    unsigned char buf[48];
    memset(buf, 0xa5, sizeof buf);
    get.data = (uintptr_t)buf;
    get.capacity = 3;
    error_is(ioctl(fd, WEBOS_SESTORE_GET, &get), EMSGSIZE);
    assert(get.bytes == 4 && buf[0] == 0xa5 && buf[3] == 0xa5);
    get.capacity = sizeof buf;
    assert(!ioctl(fd, WEBOS_SESTORE_GET, &get));
    assert(!memcmp(buf, expected, sizeof expected) && buf[4] == 0xa5);
    get.data = 1;
    error_is(ioctl(fd, WEBOS_SESTORE_GET, &get), EFAULT);
    get.data = 0;
    strcpy(get.id, "missing");
    error_is(ioctl(fd, WEBOS_SESTORE_GET, &get), ENOENT);
    strcpy(get.id, "../escape");
    error_is(ioctl(fd, WEBOS_SESTORE_GET, &get), EINVAL);
    memset(get.id, 'a', sizeof get.id);
    error_is(ioctl(fd, WEBOS_SESTORE_GET, &get), EINVAL);
    memset(get.id, 0, sizeof get.id);
    strcpy(get.id, "empty");
    assert(!ioctl(fd, WEBOS_SESTORE_GET, &get) && get.bytes == 0);
    get.reserved[0] = 1;
    error_is(ioctl(fd, WEBOS_SESTORE_GET, &get), EINVAL);
    error_is(ioctl(fd, WEBOS_SESTORE_GET, (void *)1), EFAULT);
    error_is(ioctl(fd, _IO('W', 0x7f), &get), ENOTTY);

    unsigned int size = 0; /* libsestore's input length is not a capacity. */
    memset(buf, 0xa5, sizeof buf);
    assert(!SESTORE_ReadData("3382732D", buf, &size));
    assert(size == sizeof expected && !memcmp(buf, expected, size) && buf[4] == 0xa5);
    assert(!SESTORE_ReadData("3382732D", NULL, &size) && size == 4);
    assert(!SESTORE_ReadData("C1FFB834", buf, &size) && size == 10);
    assert(!memcmp(buf, "dummy;key\0", 10));
    /* Exact names only, whatever the policy: no regional alias, nothing from LG's library. */
    assert((unsigned int)SESTORE_ReadData("1D492999_r", buf, &size) == 0xFFFF0002u);
    assert((unsigned int)SESTORE_ReadData("missing", buf, &size) == 0xFFFF0002u);
    size = sizeof buf;
    assert(!HAL_CRYPTO_ReadMskToSecureStore(buf, &size) && size == 44);
    assert(!memcmp(buf, "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", 44));
    size = 43;
    assert(DILE_CRYPTO_ReadMskToSecureStore(buf, &size) == -1);
    size = sizeof buf;
    assert(!DILE_CRYPTO_ReadMskToSecureStore(buf, &size) && size == 44);
    struct webos_sestore_header header = {0};
    struct webos_sestore_load load = {.data = (uintptr_t)&header, .bytes = sizeof header};
    error_is(ioctl(fd, WEBOS_SESTORE_LOAD, &load), getuid() ? EPERM : EBUSY);
}

int main(int argc, char **argv)
{
    assert(argc >= 2);
    int fd = open(WEBOS_SESTORE_DEVICE, O_RDONLY | O_CLOEXEC);
    assert(fd >= 0);
    if (!strcmp(argv[1], "unready")) {
        unready(fd);
        puts("PASS unprovisioned reads and malformed provisioning");
    } else if (!strcmp(argv[1], "blocked")) {
        error_is(probe_expose_store("/blocked"), EEXIST);
        struct stat st;
        assert(!stat("/blocked/dev/lg/beanjar-sestore", &st) && S_ISREG(st.st_mode));
        pid_t child = fork();
        assert(child >= 0);
        if (!child) {
            assert(!chroot("/blocked") && !chdir("/"));
            assert(!setgid(1000) && !setuid(1000));
            unsigned char buf[44];
            unsigned int size = sizeof buf;
            assert(HAL_CRYPTO_ReadMskToSecureStore(buf, &size) == -1);
            assert((unsigned int)SESTORE_ReadData("1D492999", buf, &size) == 0xFFFF0002u);
            _exit(0);
        }
        int status;
        assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status));
        puts("PASS conflicting jail node preserved; device failures cannot enable fallback");
    } else if (!strcmp(argv[1], "expose")) {
        assert(!probe_expose_store("/jail"));
        puts("PASS production device-node hook for a jail without /dev/lg mount");
    } else {
        assert(argc == 3);
        unsigned int policy = (unsigned int)atoi(argv[2]);
        read_checks(fd, policy);
        close(fd);
        fd = -1;
        probe_clear_legacy_store("/jail");
        pid_t child = fork();
        assert(child >= 0);
        if (!child) {
            assert(!chroot("/jail") && !chdir("/"));
            assert(!setgid(1000) && !setuid(1000));
            struct stat st;
            assert(stat("/etc/emulator/youtube.key", &st) < 0 && errno == ENOENT);
            assert(stat("/etc/emulator/sestore/C1FFB834", &st) < 0 && errno == ENOENT);
            assert(stat("/etc/emulator/sestore/.missing", &st) < 0 && errno == ENOENT);
            assert(stat("/etc/emulator/sestore.bin", &st) < 0 && errno == ENOENT);
            int jailed = open(WEBOS_SESTORE_DEVICE, O_RDONLY | O_CLOEXEC);
            assert(jailed >= 0);
            read_checks(jailed, policy);
            close(jailed);
            _exit(0);
        }
        int status;
        assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status));
        puts("PASS exact bytes, size queries, capacity, policy, immutable store and unprivileged "
             "jail");
    }
    if (fd >= 0)
        close(fd);
    return 0;
}
