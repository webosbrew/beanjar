// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* Run before firmware in a fresh isolated VM. These are /dev/semutex's
 * named-mutex and first-creator shared-memory contracts, independent of a
 * firmware memory table. Build as a static arm64 or AArch32 executable. */
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

#define LOCK         _IO('S', 0)
#define UNLOCK       _IO('S', 1)
#define CREATE_SHM   _IO('S', 2)
#define CREATE_MUTEX _IOR('S', 5, int)
#define DELETE_MUTEX _IOR('S', 6, int)
#define TRY_LOCK     _IOR('S', 7, int)
#define IS_FIRST     _IO('S', 8)
#define LOCK_TIMEOUT _IOR('S', 9, int)

static int a = -1, b = -1;
static volatile uint32_t *x = MAP_FAILED, *y = MAP_FAILED;
static size_t page;

static void cleanup(void)
{
    if (x != MAP_FAILED)
        munmap((void *)x, page);
    if (y != MAP_FAILED)
        munmap((void *)y, page);
    if (a >= 0)
        close(a);
    if (b >= 0)
        close(b);
}

static void check(int ok, const char *why)
{
    if (!ok) {
        fprintf(stderr, "FAIL %s (errno %d)\n", why, errno);
        exit(1);
    }
}

int main(void)
{
    long pagesize = sysconf(_SC_PAGESIZE);
    char name[64];
    int index, timeout[2], keep[2], status;
    uint32_t size;
    pid_t holder;

    check(pagesize > 0 && (unsigned long)pagesize <= UINT32_MAX / 2, "page size");
    page = (size_t)pagesize;
    atexit(cleanup);
    a = open("/dev/semutex", O_RDWR);
    b = open("/dev/semutex", O_RDWR);
    check(a >= 0 && b >= 0, "two independent opens");
    snprintf(name, sizeof(name), "semutex-check-%ld", (long)getpid());
    index = ioctl(a, CREATE_MUTEX, name);
    check(index >= 0xff, "named mutex index");
    check(ioctl(b, CREATE_MUTEX, name) == index, "same name across files");
    check(ioctl(a, TRY_LOCK, &index) == 0, "first try acquires");
    check(ioctl(b, TRY_LOCK, &index) == 1, "held try reports busy");
    timeout[0] = index;
    timeout[1] = 20;
    errno = 0;
    check(ioctl(b, LOCK_TIMEOUT, timeout) == -1 && errno == ETIME, "held lock times out");
    check(ioctl(b, UNLOCK, &index) == 0, "another file can unlock");
    errno = 0;
    check(ioctl(a, UNLOCK, &index) == -1 && errno == EPERM, "unheld unlock rejected");
    check(ioctl(a, LOCK, &index) == 0, "lock acquires after unlock");
    check(close(a) == 0, "close holder");
    a = -1;
    check(ioctl(b, TRY_LOCK, &index) == 0, "holder release returns lock");
    check(ioctl(b, UNLOCK, &index) == 0, "returned lock unlocks");
    /* A process that exits holding a lock gives it back although a child it forked keeps the
     * file open (MStar's flush by tgid); the child keeps it until the pipe closes. */
    check(pipe(keep) == 0, "keeper pipe");
    holder = fork();
    check(holder >= 0, "fork holder");
    if (holder == 0) {
        int c = open("/dev/semutex", O_RDWR);
        char byte;

        if (c < 0 || ioctl(c, LOCK, &index) != 0)
            _exit(1);
        close(keep[1]);
        if (fork() == 0) {
            if (read(keep[0], &byte, 1) < 0)
                _exit(1);
            _exit(0);
        }
        _exit(0);
    }
    close(keep[0]);
    check(waitpid(holder, &status, 0) == holder && WIFEXITED(status) && !WEXITSTATUS(status),
          "holder locks, forks a keeper and exits");
    timeout[0] = index;
    timeout[1] = 1000;
    check(ioctl(b, LOCK_TIMEOUT, timeout) == 0, "exited holder's lock returns, file kept");
    close(keep[1]);
    check(ioctl(b, DELETE_MUTEX, &index) == 0, "delete name while held");
    check(ioctl(b, TRY_LOCK, &index) == 1, "deletion retains held lock");
    check(ioctl(b, UNLOCK, &index) == 0, "deleted holder can unlock");

    a = open("/dev/semutex", O_RDWR);
    check(a >= 0, "reopen shared-memory creator");
    errno = 0;
    x = mmap(NULL, page, PROT_READ | PROT_WRITE, MAP_SHARED, a, 0);
    check(x == MAP_FAILED && errno == EINVAL, "mmap needs shared-memory registration");
    size = (uint32_t)page;
    check(ioctl(a, CREATE_SHM, &size) == 0 && ioctl(a, IS_FIRST) == 1, "first creator");
    size *= 2;
    check(ioctl(b, CREATE_SHM, &size) == 0 && ioctl(b, IS_FIRST) == 0, "later creator");
    check(ioctl(a, CREATE_SHM, &size) == 0 && ioctl(a, IS_FIRST) == 1,
          "repeat creator retains first status");
    void *large = mmap(NULL, page * 2, PROT_READ | PROT_WRITE, MAP_SHARED, b, 0);
    if (large != MAP_FAILED)
        munmap(large, page * 2);
    check(large == MAP_FAILED && errno == EINVAL, "first size retained");
    x = mmap(NULL, page, PROT_READ | PROT_WRITE, MAP_SHARED, a, 0);
    y = mmap(NULL, page, PROT_READ | PROT_WRITE, MAP_SHARED, b, 0);
    check(x != MAP_FAILED && y != MAP_FAILED, "shared mappings");
    check(x[0] == 0 && y[1] == 0, "new memory zeroed");
    x[0] = 0x12345678;
    y[1] = 0x87654321;
    check(y[0] == x[0] && x[1] == y[1], "both files share memory");
    check(munmap((void *)x, page) == 0, "unmap first alias");
    x = MAP_FAILED;
    check(close(a) == 0, "close first shared user");
    a = -1;
    check(y[0] == 0x12345678, "shared memory survives another user's close");
    check(munmap((void *)y, page) == 0, "unmap last alias");
    y = MAP_FAILED;
    check(close(b) == 0, "close last shared user");
    b = -1;
    a = open("/dev/semutex", O_RDWR);
    size = (uint32_t)page;
    check(a >= 0 && ioctl(a, CREATE_SHM, &size) == 0 && ioctl(a, IS_FIRST) == 1,
          "fresh allocation after last release");
    x = mmap(NULL, page, PROT_READ | PROT_WRITE, MAP_SHARED, a, 0);
    check(x != MAP_FAILED && x[0] == 0 && x[1] == 0, "released memory is discarded");
    puts("semutex naming, ownership, exit, timeout and shared memory PASS");
    return 0;
}
