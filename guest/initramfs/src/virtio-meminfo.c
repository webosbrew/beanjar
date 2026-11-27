// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The guest's swap, /proc/meminfo -> webos.memory, in the arm64 initramfs.
 *
 * The viewer's memory meter shows the guest's RAM from the virtio-balloon's statistics, which
 * have no swap sizes (only swap-in and swap-out counts). Some firmwares swap (webOS 6 and later:
 * the stock efs-on and swap-on scripts on the EFS disk, guest/initramfs/rootfs/init), so this pump
 * writes the guest's swap every 2 seconds to the webos.memory port as one line of text:
 *   swap total=<SwapTotal kB> free=<SwapFree kB>
 * QEMU keeps the port's output in a ring buffer (host/core/beanjar/runtime/qemu.py) that the viewer
 * reads with QMP ringbuf-read. /proc/meminfo is opened once and read again from the start each
 * time; a line the port cannot take now is dropped (the next one carries the same). It runs from
 * the initramfs, so the same on every init system. Errors go to stderr
 * (guest/initramfs/rootfs/init: /run/virtio-meminfo.log), each kind once. */
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define INTERVAL_MS 2000

static void sleep_ms(unsigned ms)
{
    struct timespec ts = {ms / 1000, (long)(ms % 1000) * 1000000L};
    while (nanosleep(&ts, &ts) < 0 && errno == EINTR)
        ;
}

/* The device of the virtio port called name, or "" when there is none. */
static void find_port(const char *name, char *dev, size_t size)
{
    glob_t paths;
    char want[64];
    dev[0] = 0;
    snprintf(want, sizeof want, "%s\n", name);
    memset(&paths, 0, sizeof paths);
    if (glob("/sys/class/virtio-ports/*/name", 0, NULL, &paths))
        return;
    for (size_t i = 0; i < paths.gl_pathc && !dev[0]; i++) {
        char line[64], leaf[64];
        FILE *f = fopen(paths.gl_pathv[i], "r");
        if (!f)
            continue;
        if (fgets(line, sizeof line, f) && !strcmp(line, want) &&
            sscanf(paths.gl_pathv[i], "/sys/class/virtio-ports/%63[^/]/name", leaf) == 1)
            snprintf(dev, size, "/dev/%s", leaf);
        fclose(f);
    }
    globfree(&paths);
}

/* The kB of the "name:" line of a /proc/meminfo text, or -1. */
static long field(const char *text, const char *name)
{
    size_t len = strlen(name);
    for (const char *at = text; at && *at; at = strchr(at, '\n') ? strchr(at, '\n') + 1 : NULL)
        if (!strncmp(at, name, len) && at[len] == ':')
            return strtol(at + len + 1, NULL, 10);
    return -1;
}

int main(void)
{
    char dev[96], text[4096], line[96];
    int in, out, failed = 0;

    find_port("webos.memory", dev, sizeof dev);
    if (!dev[0]) {
        fprintf(stderr, "virtio-meminfo: no webos.memory port, so no guest swap figures\n");
        return 0;
    }
    in = open("/proc/meminfo", O_RDONLY | O_CLOEXEC);
    if (in < 0) {
        fprintf(stderr, "virtio-meminfo: /proc/meminfo: %s\n", strerror(errno));
        return 1;
    }
    out = open(dev, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
    if (out < 0) {
        fprintf(stderr, "virtio-meminfo: %s: %s\n", dev, strerror(errno));
        return 1;
    }
    pid_t pid = fork();
    if (pid < 0) {
        perror("virtio-meminfo: fork");
        return 1;
    }
    if (pid > 0)
        return 0;
    setsid();
    fprintf(stderr, "virtio-meminfo: /proc/meminfo -> %s every %d ms\n", dev, INTERVAL_MS);
    for (;; sleep_ms(INTERVAL_MS)) {
        ssize_t n = pread(in, text, sizeof text - 1, 0);
        if (n <= 0) {
            if (!(failed & 1))
                fprintf(stderr, "virtio-meminfo: /proc/meminfo: %s\n",
                        n < 0 ? strerror(errno) : "empty");
            failed |= 1;
            continue;
        }
        text[n] = 0;
        int len = snprintf(line, sizeof line, "swap total=%ld free=%ld\n", field(text, "SwapTotal"),
                           field(text, "SwapFree"));
        ssize_t k = write(out, line, (size_t)len);
        if (k < 0 && errno != EAGAIN && errno != EINTR && !(failed & 2)) {
            fprintf(stderr, "virtio-meminfo: %s: %s\n", dev, strerror(errno));
            failed |= 2;
        }
    }
}
