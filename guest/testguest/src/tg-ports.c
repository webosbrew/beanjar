// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The test guest's check of the virtio-serial ports the launcher gives every VM (qemu.py):
 *
 *   - every port is there, by name (/sys/class/virtio-ports/<vportNpM>/name);
 *   - webos.media.0: a decode session of include/webos-media.h (OPEN, CAPS, DECODE, EOS), which the
 *     host's media helper answers with a PICTURE marked LAST: a round trip through the port, the
 *     helper and its decoder;
 *   - webos.video: one SYNC frame of include/webos-video.h with no contexts (the protocol is guest
 * to host only, so the write is the check).
 *
 * webos.ir and webos.memory are held by the initramfs's pumps (virtio-ir, virtio-meminfo), and
 * webos.standby and webos.suspended must not be opened (open is the guest's standby, and its SoC
 * down, which the host answers with a stop), so they are only looked for.
 * Prints key=value lines (ports.*); exits 0 when every check passed. */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "webos-media.h"
#include "webos-video.h"

static const char *const expected[] = {
    "webos.cursor",  "webos.video",   "webos.ir",      "webos.standby", "webos.suspended",
    "webos.micom",   "webos.memory",  "webos.media.0", "webos.media.1", "webos.media.2",
    "webos.media.3", "webos.media.4", "webos.media.5", "webos.media.6", "webos.media.7",
};
#define N_EXPECTED (sizeof expected / sizeof expected[0])

static char devs[N_EXPECTED][sizeof "/dev/" + 256]; /* a d_name is up to 255 bytes */

static int64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

/* each expected port's /dev node, by its name; how many were found */
static int find_ports(void)
{
    DIR *d = opendir("/sys/class/virtio-ports");
    int found = 0, total = 0;
    struct dirent *e;
    if (!d)
        return 0;
    while ((e = readdir(d))) {
        char path[300], name[64] = "";
        if (e->d_name[0] == '.')
            continue;
        snprintf(path, sizeof path, "/sys/class/virtio-ports/%s/name", e->d_name);
        FILE *f = fopen(path, "r");
        if (!f)
            continue;
        if (fgets(name, sizeof name, f))
            name[strcspn(name, "\n")] = 0;
        fclose(f);
        total++;
        for (size_t i = 0; i < N_EXPECTED; i++)
            if (!strcmp(name, expected[i]) && !devs[i][0]) {
                snprintf(devs[i], sizeof devs[i], "/dev/%s", e->d_name);
                found++;
            }
    }
    closedir(d);
    printf("ports.total=%d\n", total);
    return found;
}

static const char *dev_of(const char *name)
{
    for (size_t i = 0; i < N_EXPECTED; i++)
        if (!strcmp(expected[i], name))
            return devs[i][0] ? devs[i] : NULL;
    return NULL;
}

static int write_all(int fd, const void *p, size_t n)
{
    const char *c = p;
    while (n) {
        ssize_t w = write(fd, c, n);
        if (w < 0 && errno == EINTR)
            continue;
        if (w <= 0)
            return -1;
        c += w;
        n -= (size_t)w;
    }
    return 0;
}

static uint32_t sequence;

static int send_msg(int fd, uint16_t type, const void *payload, uint32_t size)
{
    struct webos_media_header h = {0};
    h.magic = WEBOS_MEDIA_MAGIC;
    h.type = type;
    h.size = size;
    h.sequence = sequence++;
    h.mono = mono_ns();
    h.clock = WEBOS_MEDIA_NONE;
    h.check = webos_media_check(&h);
    if (write_all(fd, &h, sizeof h))
        return -1;
    return size ? write_all(fd, payload, size) : 0;
}

/* reads exactly n bytes, waiting at most until deadline (ns); 0, or -1 */
static int read_full(int fd, void *p, size_t n, int64_t deadline)
{
    char *c = p;
    while (n) {
        int left = (int)((deadline - mono_ns()) / 1000000);
        struct pollfd pfd = {fd, POLLIN, 0};
        if (left <= 0 || poll(&pfd, 1, left) <= 0)
            return -1;
        ssize_t r = read(fd, c, n);
        if (r < 0 && (errno == EINTR || errno == EAGAIN))
            continue;
        if (r <= 0)
            return -1;
        c += r;
        n -= (size_t)r;
    }
    return 0;
}

static int media_check(const char *dev)
{
    int fd = open(dev, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        printf("ports.media_error=open %s: %s\n", dev, strerror(errno));
        return 1;
    }
    struct webos_media_open o = {0};
    o.kind = WEBOS_MEDIA_VIDEO;
    o.pid = getpid();
    snprintf(o.element, sizeof o.element, "tg-ports");
    static const char caps[] =
        "video/x-h264, stream-format=(string)byte-stream, alignment=(string)au, "
        "width=(int)64, height=(int)64";
    struct webos_media_decode dec = {WEBOS_MEDIA_NV12, 0};
    int64_t t0 = mono_ns();
    uint32_t eos_seq;
    if (send_msg(fd, WEBOS_MEDIA_OPEN, &o, sizeof o) ||
        send_msg(fd, WEBOS_MEDIA_CAPS, caps, sizeof caps) ||
        send_msg(fd, WEBOS_MEDIA_DECODE, &dec, sizeof dec) ||
        (eos_seq = sequence, send_msg(fd, WEBOS_MEDIA_EOS, NULL, 0))) {
        printf("ports.media_error=write: %s\n", strerror(errno));
        close(fd);
        return 1;
    }
    /* the answers: PICTUREs (or a POOL) until the one marked LAST for the EOS */
    int64_t deadline = t0 + 20 * 1000000000ll;
    int ok = 0;
    for (;;) {
        struct webos_media_header h;
        if (read_full(fd, &h, sizeof h, deadline)) {
            printf("ports.media_error=no LAST answer to EOS within 20 s\n");
            break;
        }
        if (h.magic != WEBOS_MEDIA_MAGIC || h.check != webos_media_check(&h) ||
            h.size > WEBOS_MEDIA_MAX_PAYLOAD) {
            printf("ports.media_error=invalid header from the host (type %u)\n", h.type);
            break;
        }
        char *pl = malloc(h.size ? h.size : 1);
        if (!pl || read_full(fd, pl, h.size, deadline)) {
            free(pl);
            printf("ports.media_error=short payload\n");
            break;
        }
        if (h.type == WEBOS_MEDIA_PICTURE && h.size >= sizeof(struct webos_media_picture)) {
            struct webos_media_picture pic;
            memcpy(&pic, pl, sizeof pic);
            if (pic.input == eos_seq && (pic.flags & WEBOS_MEDIA_PICTURE_LAST)) {
                ok = 1;
                free(pl);
                break;
            }
        }
        free(pl);
    }
    if (ok)
        printf("ports.media_rtt_ms=%.3f\n", (mono_ns() - t0) / 1e6);
    send_msg(fd, WEBOS_MEDIA_CLOSE, NULL, 0);
    close(fd);
    return !ok;
}

static int video_check(const char *dev)
{
    int fd = open(dev, O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        printf("ports.video_error=open %s: %s\n", dev, strerror(errno));
        return 1;
    }
    struct webos_video_packet p;
    memset(&p, 0, sizeof p);
    memcpy(p.magic, WEBOS_VIDEO_MAGIC, sizeof WEBOS_VIDEO_MAGIC);
    p.bytes = sizeof p;
    p.event = WEBOS_VIDEO_SYNC;
    p.subject = WEBOS_VIDEO_MAX;
    p.screen_w = 1920;
    p.screen_h = 1080;
    int r = write_all(fd, &p, sizeof p);
    if (r)
        printf("ports.video_error=write: %s\n", strerror(errno));
    close(fd);
    return r ? 1 : 0;
}

int main(void)
{
    int found = find_ports();
    printf("ports.found=%d\n", found);
    printf("ports.expected=%zu\n", N_EXPECTED);
    for (size_t i = 0; i < N_EXPECTED; i++)
        if (!devs[i][0])
            printf("ports.missing=%s\n", expected[i]);
    int fails = found != (int)N_EXPECTED;

    const char *media = dev_of("webos.media.0");
    int m = media ? media_check(media) : 1;
    printf("ports.media=%s\n", m ? "fail" : "pass");
    const char *video = dev_of("webos.video");
    int v = video ? video_check(video) : 1;
    printf("ports.video=%s\n", v ? "fail" : "pass");
    return fails || m || v;
}
