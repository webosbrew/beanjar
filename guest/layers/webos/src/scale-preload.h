// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* What the webos layer's timeout-scaling preloads share (bootd-scale, memchute-scale, luna-scale):
 * guest/initramfs/rootfs/init LD_PRELOADs one into its daemon when the launcher passes
 * webos.time_scale=N, N > 1 (a TCG guest, or a userland under qemu-user; under a native KVM guest
 * none is loaded), with N in the library's own variable. The constructor reads N (2 to 100, else
 * 1) and takes the library and the variable out of the environment again, so what the daemon starts
 * runs with LG's timing and the stock environment. Each library is one source file that includes
 * this header once. */
#ifndef SCALE_PRELOAD_H
#define SCALE_PRELOAD_H

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static unsigned long scale = 1;

/* self out of LD_PRELOAD, in place (as pmlogd's libpmlog-keep-all.so does). */
__attribute__((unused)) static void scale_leave_preload(const char *self)
{
    char *v = getenv("LD_PRELOAD"), *p;
    size_t n = strlen(self);
    if (!v)
        return;
    for (p = v; (p = strstr(p, self)); p++) {
        if ((p == v || p[-1] == ' ' || p[-1] == ':') && (!p[n] || p[n] == ' ' || p[n] == ':')) {
            char *from = p + n;
            if (p > v && !*from)
                p--;
            else if (*from)
                from++;
            memmove(p, from, strlen(from) + 1);
            break;
        }
    }
    if (!v[strspn(v, " :")])
        unsetenv("LD_PRELOAD");
}

/* scale from the variable var, then var and self out of the environment: call from the
 * library's constructor. */
__attribute__((unused)) static void scale_init(const char *var, const char *self)
{
    const char *v = getenv(var);
    char *end;
    unsigned long n = v ? strtoul(v, &end, 10) : 0;
    if (v && *v && !*end && n > 1 && n <= 100)
        scale = n;
    unsetenv(var);
    scale_leave_preload(self);
}

/* A line in the kernel log (the firmware's journald keeps no files, so a daemon's stderr is lost;
 * dmesg has the initramfs's own line beside it). One write(): /dev/kmsg takes a record per write,
 * and dprintf fails on it (EINVAL, from the seek glibc does when it attaches a stream to the
 * descriptor). */
__attribute__((unused, format(printf, 1, 2))) static void scale_log(const char *fmt, ...)
{
    char line[160];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    int fd = open("/dev/kmsg", O_WRONLY | O_CLOEXEC);
    if (fd >= 0) {
        if (n > 0 && (size_t)n < sizeof line)
            (void)!write(fd, line, (size_t)n);
        close(fd);
    }
}

#endif
