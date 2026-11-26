// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The built-in microphone's sound, webos.mic -> lgemu.ko, in the initramfs.
 *
 * The host (host/core/beanjar/mic.py) sends what the microphone hears, 48 kHz stereo S16_LE, over
 * the webos.mic virtio-serial port. The kernel cannot read a virtio port itself (the port's file
 * has no read_iter), so this pump copies the port's stream to /dev/lgemu-mic
 * (guest/kernel/modules/lgemu/mic.c), in whole frames; that write blocks while the sound is far
 * enough ahead of real time, which holds the host back through the port. A hang-up of the port
 * means no host program is connected; the port is looked at again a little later.
 *
 * Without the port or the device (a model without the microphone: lgemu's mic=0), it does
 * nothing. Errors go to stderr (guest/initramfs/rootfs/init: /run/virtio-mic.log). */
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define FRAME 4 /* two channels of 16 bits */

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

/* all of buf to fd; 0, or -1 */
static int write_all(int fd, const unsigned char *buf, size_t n)
{
    for (size_t done = 0; done < n;) {
        ssize_t k = write(fd, buf + done, n - done);
        if (k < 0 && errno == EINTR)
            continue;
        if (k <= 0)
            return -1;
        done += (size_t)k;
    }
    return 0;
}

int main(void)
{
    char dev[96];
    unsigned char buf[16384 + FRAME];
    size_t have = 0; /* bytes of a frame the port split, kept for the next read */
    int in, out;

    find_port("webos.mic", dev, sizeof dev);
    if (!dev[0])
        return 0; /* a model without the microphone */
    out = open("/dev/lgemu-mic", O_WRONLY | O_CLOEXEC);
    if (out < 0) {
        fprintf(stderr, "virtio-mic: /dev/lgemu-mic: %s (lgemu.ko without mic=1)\n",
                strerror(errno));
        return 1;
    }
    in = open(dev, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (in < 0) {
        fprintf(stderr, "virtio-mic: %s: %s\n", dev, strerror(errno));
        return 1;
    }
    pid_t pid = fork();
    if (pid < 0) {
        perror("virtio-mic: fork");
        return 1;
    }
    if (pid > 0)
        return 0;
    setsid();
    fprintf(stderr, "virtio-mic: %s -> /dev/lgemu-mic\n", dev);
    for (;;) {
        struct pollfd p = {in, POLLIN, 0};
        if (poll(&p, 1, -1) < 0) {
            if (errno != EINTR)
                sleep_ms(1000);
            continue;
        }
        if (!(p.revents & POLLIN)) { /* a hang-up: no host program on the port */
            have = 0;
            sleep_ms(200);
            continue;
        }
        ssize_t n = read(in, buf + have, sizeof buf - FRAME);
        if (n <= 0) {
            if (n < 0 && errno != EAGAIN && errno != EINTR) {
                fprintf(stderr, "virtio-mic: %s: %s\n", dev, strerror(errno));
                sleep_ms(1000);
            }
            continue;
        }
        size_t total = have + (size_t)n, whole = total - total % FRAME;
        if (whole && write_all(out, buf, whole) < 0) {
            fprintf(stderr, "virtio-mic: /dev/lgemu-mic: %s\n", strerror(errno));
            sleep_ms(1000);
        }
        have = total - whole;
        memmove(buf, buf + whole, have);
    }
}
