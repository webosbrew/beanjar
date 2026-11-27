// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The antenna, webos.tuner <-> dtv_soc.ko, in the arm64 initramfs.
 *
 * The host's tuner (host/core/beanjar/tuner/antenna.py) sends its channel map and the transport
 * stream of the tuned frequency over the webos.tuner virtio-serial port, and learns which frequency
 * frontend 0 is tuned to (include/webos-tuner.h). The kernel cannot read a virtio port itself (the
 * port's file has no read_iter), so this pump copies the port's stream to /dev/dtv-soc
 * (guest/kernel/modules/dtv_soc), whose parser takes it apart, and the device's tune frames to
 * the port. A hang-up of the port means no host program is connected: /dev/dtv-soc is closed then
 * (its parser starts afresh with the next host, which sends its map first, and dtv_soc answers it
 * with the tune), and the port is looked at again a little later.
 *
 * The decoders. dtv_soc's /dev/dtv-soc-av gives what the demuxes hand the SoC's decoders (records,
 * include/webos-tuner.h): the video decoder's and audio decoders' transport stream packets while
 * they run, and the PCRs. This pump is the decoders' way to the host: each one that starts takes a
 * free webos.media.<n> port (include/webos-media.h) for as long as it runs, as the media sinks do,
 * and sends its PES packets as BUFFERs, in the caps of its codec (unparsed: the host's parser
 * splits them). Their times are the stream's own on the PCR's clock, the decoders' STC: a PTS is
 * presented when the guest's CLOCK_MONOTONIC reaches the moment the PCR said that time, plus
 * AV_LEAD_NS for the way to the host (running time on base time 0, the header's clock that
 * monotonic clock); a PCR off that line by more than a second (the stream looped) starts it again.
 * Without a host on a port, or a host that falls behind, that decoder's data is dropped and its
 * session sent again from OPEN, video from its next sequence header.
 *
 * Without dtv_soc.ko (a board with no tuner of ours) or the port, it does nothing. Errors go to
 * stderr (guest/initramfs/rootfs/init: /run/virtio-tuner.log). */
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>
#include "../../../include/webos-media.h"
#include "../../../include/webos-tuner.h"

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

/* ---- the decoders, /dev/dtv-soc-av -> webos.media.<n> ---- */

#define AV_LEAD_NS  300000000ll
#define AV_DECODERS 5 /* video 0, audio 0 to 3 */
#define AV_PES_MAX  (1u << 20)
#define AV_HELD     16  /* PES held for the PCR after a discontinuity, per decoder */
#define AV_STALL_MS 200 /* a host not taking a message for this long drops it */
#define PCR_WRAP    (300ull << 33)
#define REBASE_NS   1000000000ll

struct decoder {
    int running;
    uint8_t kind, index;
    uint32_t channel, codec;
    int fd, host, resync, need_key, have_caps;
    uint32_t sequence;
    char caps[192];
    unsigned char *pes;
    size_t len;
    int64_t pts, dts; /* 90 kHz, or -1 */
    struct held {     /* PES waiting for the PCR after a discontinuity */
        unsigned char *es;
        size_t len;
        int64_t pts, dts;
        uint32_t flags;
    } held[AV_HELD];
    unsigned nheld;
    unsigned long sent, dropped;
};

static struct decoder decoders[AV_DECODERS];
static int have_stc;
static uint64_t stc_pcr; /* 27 MHz */
static int64_t stc_mono; /* ns */

static int64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000ll + ts.tv_nsec;
}

/* the moment a PTS (90 kHz) is presented on the monotonic clock: WEBOS_MEDIA_NONE without a
 * PTS or a PCR yet, HELD when it is not on the PCR's line (the stream looped, and its PCR comes
 * after this PES) */
#define HELD (WEBOS_MEDIA_NONE + 1)
static int64_t present_at(int64_t pts)
{
    if (pts < 0 || !have_stc)
        return WEBOS_MEDIA_NONE;
    int64_t d = (int64_t)(((uint64_t)pts * 300 - stc_pcr) % PCR_WRAP);
    if (d < 0)
        d += PCR_WRAP;
    if (d > (int64_t)(PCR_WRAP / 2))
        d -= PCR_WRAP;
    int64_t at = stc_mono + d * 1000 / 27 + AV_LEAD_NS, now = mono_ns();
    return at < now - REBASE_NS || at > now + 5 * REBASE_NS ? HELD : at;
}

static void on_pcr(const struct webos_tuner_pcr *p)
{
    if (have_stc) {
        int64_t d = (int64_t)((p->pcr - stc_pcr) % PCR_WRAP);
        if (d < 0)
            d += PCR_WRAP;
        int64_t want = stc_mono + d * 1000 / 27;
        int64_t off = (int64_t)p->mono_ns - want;
        if (off < REBASE_NS && off > -REBASE_NS)
            return;
        fprintf(stderr, "virtio-tuner: the PCR jumped (%lld ms off): its clock starts again\n",
                (long long)(off / 1000000));
    }
    have_stc = 1;
    stc_pcr = p->pcr;
    stc_mono = (int64_t)p->mono_ns;
}

/* one message whole, or none of it once AV_STALL_MS pass without progress */
static int send_msg(struct decoder *d, uint16_t type, const void *a, size_t alen, const void *b,
                    size_t blen)
{
    struct webos_media_header h = {
        .magic = WEBOS_MEDIA_MAGIC,
        .type = type,
        .size = (uint32_t)(alen + blen),
        .sequence = ++d->sequence,
    };
    h.mono = h.clock = mono_ns();
    h.check = webos_media_check(&h);
    struct iovec iov[3] = {{&h, sizeof h}, {(void *)a, alen}, {(void *)b, blen}};
    size_t total = sizeof h + alen + blen, done = 0;
    while (done < total) {
        struct iovec part[3];
        int n = 0;
        size_t skip = done;
        for (int i = 0; i < 3; i++) {
            if (skip >= iov[i].iov_len) {
                skip -= iov[i].iov_len;
                continue;
            }
            part[n].iov_base = (char *)iov[i].iov_base + skip;
            part[n++].iov_len = iov[i].iov_len - skip;
            skip = 0;
        }
        ssize_t w = writev(d->fd, part, n);
        if (w > 0) {
            done += (size_t)w;
            continue;
        }
        if (w < 0 && errno == EINTR)
            continue;
        if (w < 0 && errno != EAGAIN)
            break;
        struct pollfd p = {d->fd, POLLOUT, 0};
        if (poll(&p, 1, AV_STALL_MS) <= 0 || (p.revents & (POLLHUP | POLLERR)))
            break;
    }
    if (done == total) {
        d->sent++;
        return 0;
    }
    d->dropped++;
    d->host = 0; /* gone, or behind: the session again from OPEN */
    d->resync = 1;
    return -1;
}

static int host_there(int fd)
{
    struct pollfd p = {fd, POLLOUT, 0};
    return poll(&p, 1, 0) >= 0 && !(p.revents & (POLLHUP | POLLERR));
}

/* whether the host takes this decoder's messages now; the session's start sent first after it
 * (re)appeared */
static int ready(struct decoder *d)
{
    if (d->fd < 0 || !d->have_caps)
        return 0;
    if (!host_there(d->fd)) {
        d->host = 0;
        d->resync = 1;
        return 0;
    }
    if (d->host && !d->resync)
        return 1;
    struct webos_media_open o = {.kind = d->kind, .pid = getpid(), .channel = (int32_t)d->channel};
    snprintf(o.element, sizeof o.element, "tuner-%s%u",
             d->kind == WEBOS_MEDIA_VIDEO ? "vdec" : "adec", d->index);
    struct webos_media_state st = {
        .playing = 1, .base_time = 0, .pause_rt = WEBOS_MEDIA_NONE, .rate = 1.0};
    d->host = 1;
    d->resync = 0;
    if (send_msg(d, WEBOS_MEDIA_OPEN, &o, sizeof o, NULL, 0) ||
        send_msg(d, WEBOS_MEDIA_CAPS, d->caps, strlen(d->caps) + 1, NULL, 0) ||
        send_msg(d, WEBOS_MEDIA_STATE, &st, sizeof st, NULL, 0))
        return 0;
    d->need_key = d->kind == WEBOS_MEDIA_VIDEO;
    fprintf(stderr, "virtio-tuner: %s to the host, channel %u: %s\n", o.element, d->channel,
            d->caps);
    return 1;
}

/* the first free webos.media.<n> port */
static int open_media_port(void)
{
    for (int i = 0; i < WEBOS_MEDIA_PORTS; i++) {
        char name[32], dev[96];
        snprintf(name, sizeof name, WEBOS_MEDIA_PORT_NAME "%d", i);
        find_port(name, dev, sizeof dev);
        if (!dev[0])
            continue;
        int fd = open(dev, O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (fd >= 0)
            return fd;
    }
    return -1;
}

static void stop_decoder(struct decoder *d)
{
    if (!d->running)
        return;
    if (d->fd >= 0) {
        if (d->host && !d->resync)
            send_msg(d, WEBOS_MEDIA_CLOSE, NULL, 0, NULL, 0);
        close(d->fd);
    }
    fprintf(stderr, "virtio-tuner: %s decoder %u stopped: %lu messages sent, %lu dropped\n",
            d->kind == WEBOS_MEDIA_VIDEO ? "video" : "audio", d->index, d->sent, d->dropped);
    for (unsigned i = 0; i < d->nheld; i++)
        free(d->held[i].es);
    free(d->pes);
    memset(d, 0, sizeof *d);
    d->fd = -1;
}

static void start_decoder(struct decoder *d, const struct webos_tuner_av *r)
{
    stop_decoder(d);
    d->running = 1;
    d->kind = r->kind;
    d->index = r->decoder;
    d->channel = r->channel;
    d->codec = r->codec;
    d->pes = malloc(AV_PES_MAX);
    d->pts = d->dts = -1;
    d->resync = 1;
    d->fd = open_media_port();
    if (d->fd < 0)
        fprintf(stderr, "virtio-tuner: no free media port: decoder %u's stream is dropped\n",
                r->decoder);
    if (d->kind == WEBOS_MEDIA_VIDEO) {
        const char *c = d->codec == 0x34363248   ? "video/x-h264, stream-format=(string)byte-stream"
                        : d->codec == 0x43564548 ? "video/x-h265, stream-format=(string)byte-stream"
                                                 : "video/mpeg, mpegversion=(int)2, "
                                                   "systemstream=(boolean)false";
        snprintf(d->caps, sizeof d->caps, "%s, parsed=(boolean)false", c);
        d->have_caps = 1;
    }
}

/* audio: the codec its first PES's payload starts with */
static void audio_caps(struct decoder *d, const unsigned char *b, size_t n)
{
    if (n < 2)
        return;
    if (b[0] == 0x0b && b[1] == 0x77)
        snprintf(d->caps, sizeof d->caps, "audio/x-ac3, parsed=(boolean)false");
    else if (b[0] == 0xff && (b[1] & 0xf6) == 0xf0)
        snprintf(d->caps, sizeof d->caps,
                 "audio/mpeg, mpegversion=(int)4, stream-format=(string)adts, "
                 "parsed=(boolean)false");
    else if (b[0] == 0xff && (b[1] & 0xe0) == 0xe0)
        snprintf(d->caps, sizeof d->caps, "audio/mpeg, mpegversion=(int)1, parsed=(boolean)false");
    else
        return;
    d->have_caps = 1;
}

static int has_sequence_header(const unsigned char *b, size_t n)
{
    for (size_t i = 0; i + 3 < n; i++)
        if (!b[i] && !b[i + 1] && b[i + 2] == 1 &&
            (b[i + 3] == 0xb3 || b[i + 3] == 0x67 || (b[i + 3] & 0x7e) == 0x40))
            return 1;
    return 0;
}

/* one PES's elementary stream as a BUFFER, or held until its PCR comes; with pts and dts as
 * the PES has them (90 kHz, or -1) */
static void send_pes(struct decoder *d, const unsigned char *es, size_t len, int64_t pts,
                     int64_t dts, uint32_t flags, int may_hold)
{
    struct webos_media_buffer b = {
        .pts = present_at(pts),
        .dts = present_at(dts),
        .duration = WEBOS_MEDIA_NONE,
        .stream_time = pts >= 0 ? pts * 100000 / 9 : WEBOS_MEDIA_NONE,
        .flags = flags,
    };
    if (b.pts == HELD || b.dts == HELD) {
        if (!may_hold || d->nheld == AV_HELD)
            return;
        struct held *h = &d->held[d->nheld];
        if (!(h->es = malloc(len)))
            return;
        memcpy(h->es, es, len);
        h->len = len;
        h->pts = pts;
        h->dts = dts;
        h->flags = flags;
        d->nheld++;
        return;
    }
    send_msg(d, WEBOS_MEDIA_BUFFER, &b, sizeof b, es, len);
}

/* the held PES, once a PCR has come: in time, or dropped */
static void send_held(struct decoder *d)
{
    for (unsigned i = 0; i < d->nheld; i++) {
        if (ready(d))
            send_pes(d, d->held[i].es, d->held[i].len, d->held[i].pts, d->held[i].dts,
                     d->held[i].flags, 0);
        free(d->held[i].es);
    }
    d->nheld = 0;
}

/* the PES gathered so far, as one BUFFER */
static void flush_pes(struct decoder *d)
{
    size_t n = d->len;
    d->len = 0;
    if (n < 9 || d->pes[0] || d->pes[1] || d->pes[2] != 1)
        return;
    size_t at = 9 + d->pes[8];
    if (at >= n)
        return;
    const unsigned char *es = d->pes + at;
    size_t len = n - at;
    uint32_t flags = 0;
    if (!d->have_caps && d->kind == WEBOS_MEDIA_AUDIO)
        audio_caps(d, es, len);
    if (!ready(d))
        return;
    if (d->kind == WEBOS_MEDIA_VIDEO) {
        if (has_sequence_header(es, len))
            flags |= WEBOS_MEDIA_KEYFRAME;
        if (d->need_key && !(flags & WEBOS_MEDIA_KEYFRAME))
            return;
        d->need_key = 0;
    }
    send_pes(d, es, len, d->pts, d->dts, flags, 1);
}

static int64_t pes_ts(const unsigned char *b)
{
    return (int64_t)(b[0] >> 1 & 7) << 30 | (int64_t)b[1] << 22 | (int64_t)(b[2] >> 1) << 15 |
           (int64_t)b[3] << 7 | b[4] >> 1;
}

static void on_packet(struct decoder *d, const unsigned char *pkt)
{
    size_t at = 4;
    if (pkt[0] != 0x47 || !(pkt[3] & 0x10))
        return;
    if (pkt[3] & 0x20)
        at += 1 + pkt[4];
    if (at >= 188)
        return;
    if (pkt[1] & 0x40) { /* a PES starts: the one before is whole */
        flush_pes(d);
        const unsigned char *h = pkt + at;
        d->pts = d->dts = -1;
        if (188 - at >= 14 && !h[0] && !h[1] && h[2] == 1 && (h[7] & 0x80)) {
            d->pts = d->dts = pes_ts(h + 9);
            if ((h[7] & 0x40) && 188 - at >= 19)
                d->dts = pes_ts(h + 14);
        }
    } else if (!d->len) {
        return; /* the middle of a PES whose start was not seen */
    }
    size_t n = 188 - at;
    if (d->len + n > AV_PES_MAX) {
        d->len = 0;
        return;
    }
    memcpy(d->pes + d->len, pkt + at, n);
    d->len += n;
}

static struct decoder *decoder_of(const struct webos_tuner_av *r)
{
    unsigned i = r->kind == WEBOS_MEDIA_VIDEO ? 0 : 1 + r->decoder;
    return r->decoder < 4 && i < AV_DECODERS ? &decoders[i] : NULL;
}

/* the records of one read */
static void on_records(const unsigned char *b, size_t n)
{
    while (n >= sizeof(struct webos_tuner_av)) {
        struct webos_tuner_av r;
        memcpy(&r, b, sizeof r);
        if (r.magic != WEBOS_TUNER_AV_MAGIC || r.len > n - sizeof r) {
            fprintf(stderr, "virtio-tuner: a bad record from /dev/dtv-soc-av\n");
            return;
        }
        const unsigned char *data = b + sizeof r;
        struct decoder *d = decoder_of(&r);
        if (r.type == WEBOS_TUNER_AV_PCR && r.len == sizeof(struct webos_tuner_pcr)) {
            struct webos_tuner_pcr p;
            memcpy(&p, data, sizeof p);
            on_pcr(&p);
            for (unsigned i = 0; i < AV_DECODERS; i++)
                if (decoders[i].nheld)
                    send_held(&decoders[i]);
        } else if (d && r.type == WEBOS_TUNER_AV_START) {
            start_decoder(d, &r);
        } else if (d && r.type == WEBOS_TUNER_AV_STOP) {
            stop_decoder(d);
        } else if (d && d->running && r.type == WEBOS_TUNER_AV_TS) {
            for (uint32_t k = 0; k + 188 <= r.len; k += 188)
                on_packet(d, data + k);
        }
        b += sizeof r + r.len;
        n -= sizeof r + r.len;
    }
}

int main(void)
{
    static unsigned char buf[65536], avbuf[256 << 10];
    char dev[96];
    int port, soc = -1, av = -1, host = 1, failed = 0;

    find_port("webos.tuner", dev, sizeof dev);
    if (!dev[0])
        return 0;
    if (access("/dev/dtv-soc", F_OK)) {
        fprintf(stderr, "virtio-tuner: no /dev/dtv-soc (dtv_soc.ko not loaded), so no signal\n");
        return 0;
    }
    pid_t pid = fork();
    if (pid < 0) {
        perror("virtio-tuner: fork");
        return 1;
    }
    if (pid > 0)
        return 0;
    setsid();
    while ((port = open(dev, O_RDWR | O_CLOEXEC)) < 0) {
        if (!failed++)
            fprintf(stderr, "virtio-tuner: %s: %s; retrying\n", dev, strerror(errno));
        sleep_ms(1000);
    }
    fprintf(stderr, "virtio-tuner: %s <-> /dev/dtv-soc\n", dev);
    for (unsigned i = 0; i < AV_DECODERS; i++)
        decoders[i].fd = -1;
    av = open("/dev/dtv-soc-av", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (av < 0)
        fprintf(stderr, "virtio-tuner: /dev/dtv-soc-av: %s: no decoders\n", strerror(errno));
    for (;;) {
        if (soc < 0 && (soc = open("/dev/dtv-soc", O_RDWR | O_NONBLOCK | O_CLOEXEC)) < 0) {
            fprintf(stderr, "virtio-tuner: /dev/dtv-soc: %s\n", strerror(errno));
            sleep_ms(1000);
            continue;
        }
        /* without a host program the port only hangs up: look at it again 200 ms later */
        struct pollfd p[3] = {{soc, POLLIN, 0}, {host ? port : -1, POLLIN, 0}, {av, POLLIN, 0}};
        if (poll(p, 3, host ? -1 : 200) < 0) {
            if (errno != EINTR)
                sleep_ms(1000);
            continue;
        }
        if (p[2].revents & POLLIN) {
            ssize_t n = read(av, avbuf, sizeof avbuf);
            if (n > 0)
                on_records(avbuf, (size_t)n);
        }
        if (p[0].revents & POLLIN) {
            ssize_t n = read(soc, buf, sizeof buf);
            /* the tune, for the host; lost if none is there (it learns it with its map) */
            if (n > 0 && host)
                write_all(port, buf, (size_t)n);
        }
        if (!host) {
            host = 1;
            continue;
        }
        if (!(p[1].revents & (POLLIN | POLLHUP | POLLERR)))
            continue;
        ssize_t n = (p[1].revents & POLLIN) ? read(port, buf, sizeof buf) : 0;
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0) {
            close(soc); /* the host went away: its next stream starts afresh */
            soc = -1;
            host = 0;
            continue;
        }
        if (write_all(soc, buf, (size_t)n) < 0)
            fprintf(stderr, "virtio-tuner: /dev/dtv-soc: %s\n", strerror(errno));
    }
}
