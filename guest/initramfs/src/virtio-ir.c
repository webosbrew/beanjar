// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The TV remote's infrared, webos.ir -> lgemu.ko's micom, in the arm64 initramfs.
 *
 * The viewer writes button edges (include/webos-ir.h) to the webos.ir virtio-serial port. The
 * micom model that reports them to tvservice is in the kernel (guest/kernel/modules/lgemu/i2c.c),
 * which cannot read a virtio port itself (the port's file has no read_iter), so this pump copies
 * the port's stream to /dev/lgemu-ir. A hang-up of the port means no host program is connected
 * (none yet, or one went away): the copy's /dev/lgemu-ir is closed then, which releases a held
 * button, and the port is looked at again a little later, as the micom model in userland did
 * before.
 *
 * The other way it passes the micom's standby on to the host: it reads the state from
 * /dev/lgemu-ir and holds the webos.standby port open while the SoC is suspended (from the
 * kernel's suspend entry to its exit), which the host sees in QMP query-chardev. Before a suspend
 * the micom model waits for this pump's next poll, so the host knows before the pump is frozen.
 * The whole state (struct webos_micom_status: the standby, the micom's power state, its front LED
 * latches) it writes as a line of text to the webos.micom port at each change (the host's file
 * micom-status, the viewer's front LED):
 *   standby=<0|1> power=<0xB1's state> ready=<0|1> led.<cmd>=<argument bytes in hex>/<writes> ...
 * with one led. word for each LED command the firmware has written ("led.6e=010101/2").
 * At its start it hands lgemu the webos.suspended port (WEBOS_IR_SUSPENDED_PORT, an O_PATH
 * descriptor): the kernel opens that one itself once the SoC is down, when this pump is frozen,
 * and the host then stops the VM.
 * Errors go to stderr (guest/initramfs/rootfs/init: /run/virtio-ir.log). */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>
#include "../../../include/webos-ir.h"

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

/* The micom's state as the line for webos.micom. */
static void status_line(const struct webos_micom_status *ms, char *line, size_t size)
{
    size_t at = (size_t)snprintf(line, size, "standby=%u power=%u ready=%u", ms->standby, ms->power,
                                 ms->ready);
    for (int i = 0; i < WEBOS_MICOM_LED_CMDS && at < size; i++) {
        const struct webos_micom_led *l = &ms->led[i];
        if (!l->writes)
            continue;
        at += (size_t)snprintf(line + at, size - at, " led.%02x=", l->cmd);
        for (int k = 0; k < l->len && k < WEBOS_MICOM_LED_BYTES && at < size; k++)
            at += (size_t)snprintf(line + at, size - at, "%02x", l->data[k]);
        if (at < size)
            at += (size_t)snprintf(line + at, size - at, "/%u", l->writes);
    }
    if (at < size)
        snprintf(line + at, size - at, "\n");
}

/* The micom's state changed (struct webos_micom_status from /dev/lgemu-ir): the webos.standby port
 * is open while the standby lasts; the state's line goes to webos.micom when it differs from the
 * last one written. */
static void pass_status(int status, const char *standby_dev, int *standby, int micom)
{
    static char last[512];
    struct webos_micom_status ms;
    char line[512];
    ssize_t n = read(status, &ms, sizeof ms);
    if (n != (ssize_t)sizeof ms)
        return;
    unsigned char st = ms.standby;
    if (micom >= 0) {
        status_line(&ms, line, sizeof line);
        if (strcmp(line, last) && write(micom, line, strlen(line)) == (ssize_t)strlen(line))
            snprintf(last, sizeof last, "%s", line);
    }
    if (st == WEBOS_IR_STATUS_STANDBY && *standby < 0 && standby_dev[0]) {
        *standby = open(standby_dev, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
        if (*standby < 0)
            fprintf(stderr, "virtio-ir: %s: %s\n", standby_dev, strerror(errno));
    } else if (st != WEBOS_IR_STATUS_STANDBY && *standby >= 0) {
        close(*standby);
        *standby = -1;
    }
    fprintf(stderr, "virtio-ir: micom %s\n", st == WEBOS_IR_STATUS_STANDBY ? "standby" : "on");
}

int main(void)
{
    char dev[96], standby_dev[96], micom_dev[96], buf[256];
    int in, out = -1, status, standby = -1, micom = -1, failed = 0, host = 1;

    find_port("webos.ir", dev, sizeof dev);
    if (!dev[0]) {
        fprintf(stderr, "virtio-ir: no webos.ir port, so no remote keys\n");
        return 0;
    }
    find_port("webos.standby", standby_dev, sizeof standby_dev);
    find_port("webos.micom", micom_dev, sizeof micom_dev);
    if (micom_dev[0] && (micom = open(micom_dev, O_WRONLY | O_NONBLOCK | O_CLOEXEC)) < 0)
        fprintf(stderr, "virtio-ir: %s: %s\n", micom_dev, strerror(errno));
    status = open("/dev/lgemu-ir", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (status < 0) {
        fprintf(stderr, "virtio-ir: /dev/lgemu-ir: %s (lgemu.ko not loaded)\n", strerror(errno));
        return 1;
    }
    char suspended_dev[96];
    find_port("webos.suspended", suspended_dev, sizeof suspended_dev);
    if (suspended_dev[0]) {
        int port = open(suspended_dev, O_PATH | O_CLOEXEC);
        if (port < 0 || ioctl(status, WEBOS_IR_SUSPENDED_PORT, port) < 0)
            fprintf(stderr, "virtio-ir: %s to lgemu: %s\n", suspended_dev, strerror(errno));
        if (port >= 0)
            close(port);
    }
    pid_t pid = fork();
    if (pid < 0) {
        perror("virtio-ir: fork");
        return 1;
    }
    if (pid > 0)
        return 0;
    setsid();
    while ((in = open(dev, O_RDONLY | O_CLOEXEC)) < 0) {
        if (!failed++)
            fprintf(stderr, "virtio-ir: %s: %s; retrying\n", dev, strerror(errno));
        sleep_ms(1000);
    }
    fprintf(stderr, "virtio-ir: %s -> /dev/lgemu-ir%s%s\n", dev,
            standby_dev[0] ? ", standby -> " : "", standby_dev);
    for (;;) {
        /* without a host program the port only hangs up: look at it again 200 ms later */
        struct pollfd p[2] = {{status, POLLIN, 0}, {host ? in : -1, POLLIN, 0}};
        if (poll(p, 2, host ? -1 : 200) < 0) {
            if (errno != EINTR)
                sleep_ms(1000);
            continue;
        }
        if (p[0].revents & POLLIN)
            pass_status(status, standby_dev, &standby, micom);
        if (!host) {
            host = 1;
            continue;
        }
        if (!(p[1].revents & (POLLIN | POLLHUP | POLLERR)))
            continue;
        ssize_t n = (p[1].revents & POLLIN) ? read(in, buf, sizeof buf) : 0;
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0) {
            if (out >= 0)
                close(out); /* the sender went away: a held button comes up */
            out = -1;
            host = 0;
            continue;
        }
        if (out < 0 && (out = open("/dev/lgemu-ir", O_WRONLY | O_CLOEXEC)) < 0) {
            fprintf(stderr, "virtio-ir: /dev/lgemu-ir: %s\n", strerror(errno));
            sleep_ms(1000);
            continue;
        }
        for (ssize_t done = 0; done < n;) {
            ssize_t k = write(out, buf + done, n - done);
            if (k < 0 && errno == EINTR)
                continue;
            if (k <= 0) {
                fprintf(stderr, "virtio-ir: /dev/lgemu-ir: %s\n", strerror(errno));
                break;
            }
            done += k;
        }
    }
}
