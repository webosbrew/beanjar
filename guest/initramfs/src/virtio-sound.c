// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The board's master volume and speaker mute, lgemu.ko -> webos.sound, in the arm64 initramfs.
 *
 * The guest's sound card applies the volume and mute the firmware sets as its playback gain
 * (guest/kernel/modules/lgemu/snd.c); the media streams' sound is decoded on the host, past the
 * card, so the viewer applies the same gain to it. A kernel cannot write a virtio port (the port's
 * file has no write_iter), so this pump copies each line /dev/lgemu-sound gives ("level=0x7f00
 * mute=0 gain=65536", the first at once, then one at each change) to the webos.sound port, whose
 * host side is the file sound-status beside qemu.pid. A line the port cannot take now is written
 * again when it can, unless a newer one replaced it. Errors go to stderr
 * (guest/initramfs/rootfs/init: /run/virtio-sound.log). */
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

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

int main(void)
{
    char dev[96], line[128];
    int in, out, failed = 0;
    ssize_t len = 0;

    find_port("webos.sound", dev, sizeof dev);
    if (!dev[0]) {
        fprintf(stderr, "virtio-sound: no webos.sound port, so the media audio has no volume\n");
        return 0;
    }
    in = open("/dev/lgemu-sound", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (in < 0) {
        fprintf(stderr, "virtio-sound: /dev/lgemu-sound: %s (lgemu.ko not loaded)\n",
                strerror(errno));
        return 1;
    }
    out = open(dev, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
    if (out < 0) {
        fprintf(stderr, "virtio-sound: %s: %s\n", dev, strerror(errno));
        return 1;
    }
    pid_t pid = fork();
    if (pid < 0) {
        perror("virtio-sound: fork");
        return 1;
    }
    if (pid > 0)
        return 0;
    setsid();
    fprintf(stderr, "virtio-sound: /dev/lgemu-sound -> %s\n", dev);
    for (;;) {
        /* a line waiting for the port: wait for either; else for the next line */
        struct pollfd p[2] = {{in, POLLIN, 0}, {len ? out : -1, POLLOUT, 0}};
        if (poll(p, 2, -1) < 0) {
            if (errno != EINTR)
                sleep_ms(1000);
            continue;
        }
        if (p[0].revents & POLLIN) {
            ssize_t n = read(in, line, sizeof line);
            if (n > 0) {
                len = n;
            } else if (n < 0 && errno != EAGAIN && errno != EINTR) {
                fprintf(stderr, "virtio-sound: /dev/lgemu-sound: %s\n", strerror(errno));
                sleep_ms(1000);
            }
        }
        if (len) {
            ssize_t k = write(out, line, (size_t)len);
            if (k == len) {
                len = 0;
                failed = 0;
            } else if (k >= 0 || (errno != EAGAIN && errno != EINTR)) {
                /* a short write or an error: this line is lost, the next change brings another */
                if (!failed++)
                    fprintf(stderr, "virtio-sound: %s: %s\n", dev,
                            k < 0 ? strerror(errno) : "short write");
                len = 0;
                sleep_ms(100);
            }
        }
    }
}
