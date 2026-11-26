// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* One per-VM owner of compressed media decoding. QEMU's WSM2 byte streams
 * stay unchanged; viewers receive decoded, leased buffers over local IPC.
 * Slow/disconnected viewers drop complete outputs, never codec references.
 * Only true decoder-ingress overload reconnects the guest port for a clean
 * OPEN/CAPS/STATE and keyframe restart. The VSC table remains a byte stream. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <libgen.h>
#include <malloc.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <sys/eventfd.h>
#include <sys/resource.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "../../../include/webos-media.h"
#include "../../media/decode.h"
#include "../../media/ivserver.h"
#include "../../media/log.h"
#include "../../media/pool.h"
#include "../../media/sandbox.h"
#include "../../../include/webos-video.h"

#include <libavutil/log.h>

#define QUEUE_MAX   (8u << 20)  /* bytes queued for a viewer before messages are dropped */
#define GUEST_MAX   (64u << 20) /* PICTURE answers queued for the guest before they are dropped */
#define VIDEO_FRAME ((int)sizeof(struct webos_video_packet))
#define MAX_PORTS   WEBOS_MEDIA_PORTS
/* RLIMIT_FSIZE above the pool's size: no file the helper writes grows past it */
#define FSIZE_MARGIN (64u << 20)

struct msg {
    struct msg *next;
    size_t len, off;
    unsigned char data[];
};

/* a whole message for the guest (a DECODE session's PICTURE answers), as the decoder made it */
struct gmsg {
    struct gmsg *next;
    unsigned char *data;
    size_t len, off;
};

struct chan {
    int media; /* 1: a webos.media port (WSM2 messages), 0: webos.video */
    int index;
    char qemu[108], viewer[108];
    int gfd, lfd, vfd;
    unsigned char *in;
    size_t inlen, incap;
    struct msg *qhead, *qtail;
    size_t queued;
    /* the session (media) or the last table (video), as received */
    unsigned char *open, *caps, *state, *table;
    size_t open_len, caps_len, state_len;
    int eos;
    unsigned long long msgs, bytes, buffers, dropped, flushes;
    struct gmsg *ghead, *gtail; /* to the guest */
    size_t gqueued;
    unsigned long long gsent, gdropped;
};

/* PICTURE answers from the decoder threads, per port, until the poll loop takes them */
static struct {
    pthread_mutex_t lock;
    struct gmsg *head, *tail;
} replies[MAX_PORTS];
static int wake_fd = -1;

static void on_reply(void *ctx, unsigned port, unsigned char *data, size_t len)
{
    (void)ctx;
    struct gmsg *m = malloc(sizeof *m);
    if (!m || port >= MAX_PORTS) {
        free(m);
        free(data);
        return;
    }
    *m = (struct gmsg){.data = data, .len = len};
    pthread_mutex_lock(&replies[port].lock);
    if (replies[port].tail)
        replies[port].tail->next = m;
    else
        replies[port].head = m;
    replies[port].tail = m;
    pthread_mutex_unlock(&replies[port].lock);
    uint64_t one = 1;
    if (write(wake_fd, &one, sizeof one) < 0) {
        /* the counter is full: the loop is awake anyway */
    }
}

static void free_gmsgs(struct gmsg *m)
{
    while (m) {
        struct gmsg *next = m->next;
        free(m->data);
        free(m);
        m = next;
    }
}

/* What is queued for the guest goes (a new session or none): all but a message half written, which
 * the guest's framing needs whole. */
static void drop_guest_queue(struct chan *c, int keep_partial)
{
    struct gmsg *keep = keep_partial && c->ghead && c->ghead->off ? c->ghead : NULL;
    if (keep) {
        free_gmsgs(keep->next);
        keep->next = NULL;
        c->gtail = keep;
        c->gqueued = keep->len;
        return;
    }
    free_gmsgs(c->ghead);
    c->ghead = c->gtail = NULL;
    c->gqueued = 0;
}

static void take_replies(struct chan *c)
{
    pthread_mutex_lock(&replies[c->index].lock);
    struct gmsg *m = replies[c->index].head;
    replies[c->index].head = replies[c->index].tail = NULL;
    pthread_mutex_unlock(&replies[c->index].lock);
    while (m) {
        struct gmsg *next = m->next;
        m->next = NULL;
        if (c->gfd < 0 || c->gqueued + m->len > GUEST_MAX) {
            c->gdropped++;
            free(m->data);
            free(m);
        } else {
            if (c->gtail)
                c->gtail->next = m;
            else
                c->ghead = m;
            c->gtail = m;
            c->gqueued += m->len;
        }
        m = next;
    }
}

static void guest_write(struct chan *c)
{
    while (c->ghead && c->gfd >= 0) {
        struct gmsg *m = c->ghead;
        ssize_t w = send(c->gfd, m->data + m->off, m->len - m->off, MSG_NOSIGNAL);
        if (w < 0)
            return; /* EAGAIN; a closed socket shows as a read error */
        m->off += (size_t)w;
        if (m->off < m->len)
            return;
        c->ghead = m->next;
        if (!c->ghead)
            c->gtail = NULL;
        c->gqueued -= m->len;
        c->gsent++;
        free(m->data);
        free(m);
    }
}

/* The sessions log (--log; log.h: printable lines, rotated at its cap, rate-limited per site and
 * port), stamped with the time; the decode engine's is hm_engine_log (--decode-log). */
static struct hm_log sessions = HM_LOG_INIT(1);
static struct decoder *decode;
__attribute__((format(printf, 2, 3))) static void say(int port, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    hm_logv(&sessions, port, fmt, ap);
    va_end(ap);
}

/* The open sessions (--streams FILE): one line per media port with an OPEN since its last CLOSE,
 * "PORT KIND ELEMENT<TAB>CAPS", KIND audio or video, CAPS the last caps (empty before any),
 * rewritten whole (a rename) at each OPEN, new caps and CLOSE. What `beanjar status show` reads
 * for the streams a VM's audio comes from. The guest's element name and caps are made printable
 * (spaces in the name, control and non-ASCII bytes as '?') and cut, so a line stays a line. */
#define STREAMS_CAPS 1024
static const char *streams_path;
static struct chan *streams_ch;
static int streams_n;

static void printable(FILE *f, const unsigned char *s, size_t max, int space_ok)
{
    for (size_t i = 0; i < max && s[i]; i++)
        fputc(s[i] >= 0x20 && s[i] < 0x7f && (space_ok || s[i] != ' ') ? s[i] : '?', f);
}

static void write_streams(void)
{
    if (!streams_path)
        return;
    char tmp[4200];
    snprintf(tmp, sizeof tmp, "%s.tmp", streams_path);
    FILE *f = fopen(tmp, "w");
    if (!f)
        return;
    for (int i = 0; i < streams_n; i++) {
        const struct chan *c = &streams_ch[i];
        if (!c->media || !c->open || c->open_len < sizeof(struct webos_media_header))
            continue;
        struct webos_media_open o = {0};
        size_t n = c->open_len - sizeof(struct webos_media_header);
        memcpy(&o, c->open + sizeof(struct webos_media_header), n < sizeof o ? n : sizeof o);
        fprintf(f, "%d %s ", c->index, o.kind == WEBOS_MEDIA_AUDIO ? "audio" : "video");
        printable(f, (const unsigned char *)o.element, sizeof o.element, 0);
        fputc('\t', f);
        if (c->caps && c->caps_len > sizeof(struct webos_media_header))
            printable(f, c->caps + sizeof(struct webos_media_header),
                      c->caps_len - sizeof(struct webos_media_header) < STREAMS_CAPS
                          ? c->caps_len - sizeof(struct webos_media_header)
                          : STREAMS_CAPS,
                      1);
        fputc('\n', f);
    }
    if (fclose(f) == 0)
        rename(tmp, streams_path);
    else
        unlink(tmp);
}

/* FFmpeg's own messages (a corrupt stream can make one per packet): the engine's log, limited per
 * FFmpeg format string like the engine's lines, instead of the helper's stderr */
static void ffmpeg_line(const char *key, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    hm_logkv(&hm_engine_log, -1, key, fmt, ap);
    va_end(ap);
}

static void ffmpeg_log(void *avcl, int level, const char *fmt, va_list ap)
{
    static _Thread_local int prefix = 1;
    if (level > av_log_get_level())
        return;
    char line[HM_LOG_LINE];
    va_list aq;
    va_copy(aq, ap);
    av_log_format_line2(avcl, level, fmt, aq, line, sizeof line, &prefix);
    va_end(aq);
    ffmpeg_line(fmt, "%s", line);
}

static int unix_connect(const char *path)
{
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    snprintf(a.sun_path, sizeof a.sun_path, "%s", path);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd >= 0 && connect(fd, (struct sockaddr *)&a, sizeof a) < 0 && errno != EINPROGRESS) {
        close(fd);
        return -1;
    }
    return fd;
}

static int unix_listen(const char *path, int media)
{
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    snprintf(a.sun_path, sizeof a.sun_path, "%s", path);
    unlink(path);
    int fd =
        socket(AF_UNIX, (media ? SOCK_SEQPACKET : SOCK_STREAM) | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0 || bind(fd, (struct sockaddr *)&a, sizeof a) < 0 || listen(fd, 4) < 0) {
        fprintf(stderr, "media-helper: cannot listen on %s: %s\n", path, strerror(errno));
        exit(1);
    }
    return fd;
}

static void drop_queue(struct chan *c)
{
    while (c->qhead) {
        struct msg *m = c->qhead;
        c->qhead = m->next;
        free(m);
    }
    c->qtail = NULL;
    c->queued = 0;
}

/* a whole message for the viewer, if one is connected and not too far behind */
static void queue(struct chan *c, const unsigned char *data, size_t len, int replay)
{
    if (c->vfd < 0 || !len)
        return;
    if (!replay && c->queued + len > QUEUE_MAX) {
        c->dropped++;
        return;
    }
    struct msg *m = malloc(sizeof *m + len);
    if (!m)
        return;
    m->next = NULL;
    m->len = len;
    m->off = 0;
    memcpy(m->data, data, len);
    if (c->qtail)
        c->qtail->next = m;
    else
        c->qhead = m;
    c->qtail = m;
    c->queued += len;
}

static void keep(unsigned char **slot, size_t *slot_len, const unsigned char *data, size_t len)
{
    free(*slot);
    *slot = NULL;
    if (slot_len)
        *slot_len = 0;
    if (!data)
        return;
    *slot = malloc(len);
    if (!*slot)
        return;
    memcpy(*slot, data, len);
    if (slot_len)
        *slot_len = len;
}

static void viewer_close(struct chan *c, const char *why)
{
    if (c->vfd < 0)
        return;
    if (c->media)
        decoder_detach(decode, c->index);
    close(c->vfd);
    c->vfd = -1;
    drop_queue(c);
    say(c->media ? c->index : -1, "%s %d: viewer %s", c->media ? "media" : "video", c->index, why);
}

/* one complete WSM2 message from the guest: the session it changes, then to the viewer */
static int media_message(struct chan *c, const unsigned char *data, size_t len)
{
    struct webos_media_header h;
    memcpy(&h, data, sizeof h);
    const unsigned char *pl = data + sizeof h;
    c->msgs++;
    c->bytes += len;
    switch (h.type) {
    case WEBOS_MEDIA_OPEN: {
        struct webos_media_open o = {0};
        memcpy(&o, pl, h.size < sizeof o ? h.size : sizeof o);
        o.element[sizeof o.element - 1] = 0;
        keep(&c->open, &c->open_len, data, len);
        keep(&c->caps, &c->caps_len, NULL, 0);
        drop_guest_queue(c, 1);
        keep(&c->state, &c->state_len, NULL, 0);
        c->eos = 0;
        c->buffers = c->dropped = c->flushes = 0;
        say(c->index, "media %d: open %s %s pid %d channel %d pad %u output %#x", c->index,
            o.kind == WEBOS_MEDIA_VIDEO ? "video" : "audio", o.element, o.pid, o.channel, o.pad,
            o.output);
        write_streams();
        break;
    }
    case WEBOS_MEDIA_CAPS:
        /* a string ending in the payload's last byte, or decoder_submit refuses it: never read
         * past the payload (into the next message's bytes) for the log */
        if (!h.size || pl[h.size - 1])
            say(c->index, "media %d: caps of %u bytes, not a string", c->index, h.size);
        else if (!c->caps || c->caps_len != len || memcmp(c->caps + sizeof h, pl, h.size)) {
            say(c->index, "media %d: caps %.200s", c->index, (const char *)pl);
            keep(&c->caps, &c->caps_len, data, len);
            write_streams();
            break;
        }
        keep(&c->caps, &c->caps_len, data, len);
        break;
    case WEBOS_MEDIA_STATE: {
        struct webos_media_state s = {0};
        memcpy(&s, pl, h.size < sizeof s ? h.size : sizeof s);
        keep(&c->state, &c->state_len, data, len);
        say(c->index, "media %d: %s base_time=%.3f s pause_rt=%.3f s rate=%.2f", c->index,
            s.playing ? "playing" : "paused", s.base_time / 1e9,
            s.pause_rt == WEBOS_MEDIA_NONE ? -1.0 : s.pause_rt / 1e9, s.rate);
        break;
    }
    case WEBOS_MEDIA_BUFFER:
        c->buffers++;
        break;
    case WEBOS_MEDIA_FLUSH:
        c->flushes++;
        c->eos = 0;
        say(c->index, "media %d: flush", c->index);
        break;
    case WEBOS_MEDIA_EOS:
        c->eos = 1;
        say(c->index, "media %d: end of stream", c->index);
        break;
    case WEBOS_MEDIA_DECODE:
        say(c->index, "media %d: decode for the guest", c->index);
        break;
    case WEBOS_MEDIA_CLOSE:
        say(c->index, "media %d: close buffers=%llu flushes=%llu dropped-for-viewer=%llu", c->index,
            c->buffers, c->flushes, c->dropped);
        if (c->gsent || c->gdropped)
            say(c->index, "media %d: to the guest: %llu messages, %llu dropped", c->index, c->gsent,
                c->gdropped);
        c->gsent = c->gdropped = 0;
        drop_guest_queue(c, 1);
        keep(&c->open, &c->open_len, NULL, 0);
        keep(&c->caps, &c->caps_len, NULL, 0);
        keep(&c->state, &c->state_len, NULL, 0);
        write_streams();
        break;
    }
    return decoder_submit(decode, c->index, &h, pl, hm_mono_ns());
}

/* the guest's bytes: whole messages out of them (resynchronising on the magic, as the viewer does)
 */
static void parse(struct chan *c)
{
    size_t pos = 0;
    for (;;) {
        if (!c->media) {
            while (pos + 8 <= c->inlen && memcmp(c->in + pos, WEBOS_VIDEO_MAGIC, 8))
                pos++;
            if (pos + VIDEO_FRAME > c->inlen)
                break;
            keep(&c->table, NULL, c->in + pos, VIDEO_FRAME);
            c->msgs++;
            queue(c, c->in + pos, VIDEO_FRAME, 0);
            pos += VIDEO_FRAME;
            continue;
        }
        if (pos + sizeof(struct webos_media_header) > c->inlen)
            break;
        struct webos_media_header h;
        memcpy(&h, c->in + pos, sizeof h);
        if (h.magic != WEBOS_MEDIA_MAGIC || h.check != webos_media_check(&h) ||
            h.size > WEBOS_MEDIA_MAX_PAYLOAD) {
            pos++;
            continue;
        }
        size_t len = sizeof h + h.size;
        if (pos + len > c->inlen)
            break;
        if (media_message(c, c->in + pos, len) < 0) {
            say(c->index,
                "media %d: ingress overload/invalid message; reconnecting for a new keyframe "
                "session",
                c->index);
            decoder_disconnect(decode, c->index);
            close(c->gfd);
            c->gfd = -1;
            c->inlen = 0;
            drop_guest_queue(c, 0);
            return;
        }
        pos += len;
    }
    memmove(c->in, c->in + pos, c->inlen - pos);
    c->inlen -= pos;
}

static void guest_read(struct chan *c)
{
    /* Bound each turn so a busy guest cannot starve frame retirement/leases. */
    for (unsigned batch = 0; batch < 4; batch++) {
        if (c->incap - c->inlen < 65536) {
            size_t cap = c->incap ? c->incap * 2 : 1 << 20;
            unsigned char *n = realloc(c->in, cap);
            if (!n)
                return;
            c->in = n;
            c->incap = cap;
        }
        ssize_t r = read(c->gfd, c->in + c->inlen, c->incap - c->inlen);
        if (r > 0) {
            c->inlen += (size_t)r;
            parse(c);
            if (c->gfd < 0)
                return;
            continue;
        }
        if (r < 0 && (errno == EAGAIN || errno == EINTR))
            break;
        say(c->media ? c->index : -1, "%s %d: QEMU's socket closed", c->media ? "media" : "video",
            c->index);
        if (c->media)
            decoder_disconnect(decode, c->index);
        close(c->gfd);
        c->gfd = -1;
        c->inlen = 0;
        drop_guest_queue(c, 0);
        return;
    }
    parse(c);
}

static void viewer_accept(struct chan *c)
{
    int fd = accept4(c->lfd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (fd < 0)
        return;
    if (c->vfd >= 0) {
        say(c->media ? c->index : -1, "%s %d: a second viewer refused (one at a time)",
            c->media ? "media" : "video", c->index);
        close(fd);
        return;
    }
    c->vfd = fd;
    if (c->media) {
        say(c->index, "media %d: viewer connected%s", c->index,
            c->open ? ", session replayed" : "");
        decoder_attach(decode, c->index, fd);
    } else {
        say(-1, "video %d: viewer connected", c->index);
        if (c->table)
            queue(c, c->table, VIDEO_FRAME, 1);
    }
}

static void viewer_write(struct chan *c)
{
    while (c->qhead) {
        struct msg *m = c->qhead;
        ssize_t w = send(c->vfd, m->data + m->off, m->len - m->off, MSG_NOSIGNAL);
        if (w < 0) {
            if (errno == EAGAIN || errno == EINTR)
                return;
            viewer_close(c, "gone");
            return;
        }
        m->off += (size_t)w;
        if (m->off < m->len)
            return;
        c->qhead = m->next;
        if (!c->qhead)
            c->qtail = NULL;
        c->queued -= m->len;
        free(m);
    }
}

static int split(char *arg, char *a, char *b)
{
    char *colon = strrchr(arg, ':');
    if (!colon)
        return -1;
    *colon = 0;
    snprintf(a, 108, "%s", arg);
    snprintf(b, 108, "%s", colon + 1);
    return 0;
}

/* The pool (include/webos-media.h, "The pool"): the file in the unit's RuntimeDirectory and this
 * helper as the ivshmem server of QEMU's ivshmem-doorbell for it, on the launcher's listening
 * socket (made before QEMU starts, so its realize never misses the server). */
static struct pool pool;
static struct ivserver server;
static int have_pool;

static int ring_guest(void *ctx)
{
    return ivserver_ring_guest(ctx);
}

static int start_pool(const char *path, int listen_fd, uint32_t size)
{
    char dir[4096];
    snprintf(dir, sizeof dir, "%s", path);
    struct statvfs vfs;
    if (!statvfs(dirname(dir), &vfs) && (unsigned long long)vfs.f_bavail * vfs.f_frsize < size)
        say(-1, "pool: warning: %s has %llu MiB free, less than the pool's %u MiB", dir,
            (unsigned long long)vfs.f_bavail * vfs.f_frsize >> 20, size >> 20);
    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0 || ftruncate(fd, size) < 0) {
        /* QEMU waits for a server whatever happens: a memfd rather than no VM */
        say(-1, "pool: %s: %s: a memfd instead", path, strerror(errno));
        if (fd >= 0)
            close(fd);
        fd = memfd_create("media-pool", MFD_CLOEXEC);
        if (fd < 0 || ftruncate(fd, size) < 0) {
            say(-1, "pool: memfd: %s: pictures inline", strerror(errno));
            if (fd >= 0)
                close(fd);
            return -1;
        }
    }
    if (ivserver_init(&server, listen_fd, fd) < 0 || pool_init(&pool, fd, size, 0) < 0) {
        say(-1, "pool: %s: %s: pictures inline", path, strerror(errno));
        close(fd);
        return -1;
    }
    fcntl(listen_fd, F_SETFL, fcntl(listen_fd, F_GETFL) | O_NONBLOCK);
    have_pool = 1;
    say(-1, "pool: %s, %u KiB, serving the doorbell", path, size >> 10);
    return 0;
}

int main(int argc, char **argv)
{
    pid_t pid = 0;
    int pid_fd = -1, sandbox = 1, software_only = 0, viewer_fds[MAX_PORTS + 1], nviewer_fds = 0;
    int ports = WEBOS_MEDIA_PORTS;
    char vq[108] = "", vv[108] = "", mq[108] = "", mv[108] = "";
    const char *logpath = NULL, *decode_log = NULL, *pool_path = NULL;
    int pool_listen = -1;
    uint32_t pool_size = WEBOS_MEDIA_POOL_SIZE;
    const char *hwdec = getenv("MEDIA_HWDEC");
    for (int i = 1; i < argc; i += 2) {
        if (!strcmp(argv[i], "--no-sandbox")) {
            sandbox = 0;
            i--;
            continue;
        }
        if (!strcmp(argv[i], "--software-only")) {
            /* a VM's hwdec=none: software decoding, locked against the viewers' preferences,
             * the short codec list (decode.h); needs --hwdec none and the sandbox */
            software_only = 1;
            i--;
            continue;
        }
        if (i + 1 >= argc)
            break;
        if (!strcmp(argv[i], "--pid"))
            pid = atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--pid-fd"))
            pid_fd = atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--viewer-fds")) {
            nviewer_fds = 0;
            for (char *p = argv[i + 1], *end; *p && nviewer_fds <= MAX_PORTS; p = end) {
                viewer_fds[nviewer_fds++] = (int)strtol(p, &end, 10);
                if (*end == ',')
                    end++;
                else if (*end)
                    break;
            }
        } else if (!strcmp(argv[i], "--log"))
            logpath = argv[i + 1];
        else if (!strcmp(argv[i], "--decode-log"))
            decode_log = argv[i + 1];
        else if (!strcmp(argv[i], "--streams"))
            streams_path = argv[i + 1];
        else if (!strcmp(argv[i], "--hwdec"))
            hwdec = argv[i + 1];
        else if (!strcmp(argv[i], "--ports"))
            ports = atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--pool"))
            pool_path = argv[i + 1];
        else if (!strcmp(argv[i], "--pool-fd"))
            pool_listen = atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--pool-size")) /* tests: a smaller pool */
            pool_size = (uint32_t)strtoul(argv[i + 1], NULL, 0);
        else if (!strcmp(argv[i], "--video") && !split(argv[i + 1], vq, vv)) {
        } else if (!strcmp(argv[i], "--media") && !split(argv[i + 1], mq, mv)) {
        } else {
            fprintf(stderr,
                    "usage: media-helper --pid PID|--pid-fd FD [--log FILE] [--decode-log FILE] "
                    "[--streams FILE] --video Q:V --media QPREFIX:VPREFIX [--ports N] "
                    "[--viewer-fds FD,...] [--pool FILE --pool-fd FD] [--hwdec auto|none|TYPE] "
                    "[--software-only] [--no-sandbox]\n");
            return 2;
        }
    }
    if ((!pid && pid_fd < 0) || !*vq || !*mq || strlen(mq) > 99 || strlen(mv) > 99 || ports < 1 ||
        ports > MAX_PORTS || (nviewer_fds && nviewer_fds != ports + 1)) {
        fprintf(stderr, "media-helper: --pid or --pid-fd, --video and --media are needed "
                        "(--viewer-fds: one per port and the video's)\n");
        return 2;
    }
    if (software_only && (!sandbox || !hwdec || strcmp(hwdec, "none"))) {
        fprintf(stderr, "media-helper: --software-only needs --hwdec none and the sandbox "
                        "(no --no-sandbox)\n");
        return 2;
    }
    /* Two malloc arenas for all threads (a poll loop and a decoder per port, FFmpeg's frame threads
     * per session) instead of glibc's 8 per CPU: as fast, and what the decoders free goes back with
     * frame.c's malloc_trim rather than staying spread over many arenas (two 1080p sessions at once
     * left 28 MB with the default, 13 MB with 2). */
    mallopt(M_ARENA_MAX, 2);
    /* A backstop behind the logs' own caps (log.h), for whatever else writes a file here: none
     * grows past the pool file's size and a margin. SIGXFSZ ignored, such a write fails with EFBIG
     * (log.c rotates or counts the line lost) instead of ending the helper and the VM's media. */
    signal(SIGXFSZ, SIG_IGN);
    struct rlimit fsize;
    rlim_t fsize_max = (rlim_t)pool_size + FSIZE_MARGIN;
    if (!getrlimit(RLIMIT_FSIZE, &fsize) &&
        (fsize.rlim_max == RLIM_INFINITY || fsize.rlim_max > fsize_max)) {
        fsize.rlim_cur = fsize.rlim_max = fsize_max;
        setrlimit(RLIMIT_FSIZE, &fsize);
    }
    /* Without --log and --decode-log the lines go to stderr, as limited (the tests, ad-hoc runs);
     * with them stderr keeps only what comes before and the helper's fatal errors. */
    if (logpath && hm_log_open(&sessions, logpath) < 0)
        fprintf(stderr, "media-helper: %s: %s\n", logpath, strerror(errno));
    if (decode_log && hm_log_open(&hm_engine_log, decode_log) < 0)
        fprintf(stderr, "media-helper: %s: %s\n", decode_log, strerror(errno));
    av_log_set_callback(ffmpeg_log);
    signal(SIGPIPE, SIG_IGN);
    for (int i = 0; i < MAX_PORTS; i++)
        pthread_mutex_init(&replies[i].lock, NULL);
    wake_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (wake_fd < 0) {
        fprintf(stderr, "media-helper: eventfd: %s\n", strerror(errno));
        return 1;
    }
    if (!(pool_path && pool_listen >= 0 && start_pool(pool_path, pool_listen, pool_size) == 0) &&
        pool_listen >= 0)
        close(pool_listen); /* QEMU's realize fails without a server: better than a hang */
    struct chan ch[MAX_PORTS + 1];
    int n = ports + 1;
    memset(ch, 0, sizeof ch);
    for (int i = 0; i < n; i++) {
        struct chan *c = &ch[i];
        c->media = i > 0;
        c->index = i > 0 ? i - 1 : 0;
        if (c->media) {
            snprintf(c->qemu, sizeof c->qemu, "%.99s-%c.sock", mq, '0' + c->index);
            snprintf(c->viewer, sizeof c->viewer, "%.99s-%c.sock", mv, '0' + c->index);
        } else {
            snprintf(c->qemu, sizeof c->qemu, "%s", vq);
            snprintf(c->viewer, sizeof c->viewer, "%s", vv);
        }
        c->gfd = c->vfd = -1;
        if (nviewer_fds) {
            /* the launcher's, listening at c->viewer (outside a sandbox's view) */
            c->lfd = viewer_fds[i];
            fcntl(c->lfd, F_SETFL, fcntl(c->lfd, F_GETFL) | O_NONBLOCK);
            fcntl(c->lfd, F_SETFD, FD_CLOEXEC);
        } else
            c->lfd = unix_listen(c->viewer, c->media);
    }
    if (pid_fd >= 0) {
        fcntl(pid_fd, F_SETFD, FD_CLOEXEC);
        /* signal 0 checks that it is a pidfd (EBADF otherwise; ESRCH: QEMU ended already) */
        if (syscall(SYS_pidfd_send_signal, pid_fd, 0, NULL, 0) < 0 && errno == EBADF) {
            fprintf(stderr, "media-helper: --pid-fd %d is not a pidfd\n", pid_fd);
            return 2;
        }
    }
    /* Setup done: the helper confines itself (sandbox.h) before any thread starts, since Landlock
     * holds for the threads made after it. It writes only in its own directories: the logs', the
     * open sessions', the pool's and, when it made them, its sockets'. */
    if (sandbox) {
        struct hm_sandbox box = {
            .devices = !hwdec || strcmp(hwdec, "none") != 0,
            .scope_signals = pid_fd >= 0,
        };
        static char dirs[8][4096];
        const char *files[] = {logpath,
                               decode_log,
                               streams_path,
                               pool_path,
                               nviewer_fds ? NULL : vv,
                               nviewer_fds ? NULL : mv};
        int nd = 0;
        for (size_t i = 0; i < sizeof files / sizeof *files && nd < 7; i++) {
            if (!files[i] || !*files[i])
                continue;
            char tmp[4096];
            snprintf(tmp, sizeof tmp, "%s", files[i]);
            snprintf(dirs[nd], sizeof dirs[nd], "%s", dirname(tmp));
            box.rw[nd] = dirs[nd];
            nd++;
        }
        char what[256];
        if (hm_sandbox(&box, what, sizeof what) < 0) {
            fprintf(stderr, "media-helper: sandbox: %s\n", what);
            return 1;
        }
        say(-1, "sandbox: %s%s", what, software_only ? "; software decoding only" : "");
    } else
        say(-1, "sandbox: off (--no-sandbox)");
    decode = decoder_create(ports, hwdec, software_only);
    if (!decode) {
        fprintf(stderr, "media-helper: cannot create decoder service\n");
        return 1;
    }
    decoder_set_reply(decode, on_reply, NULL);
    if (have_pool)
        decoder_set_pool(decode, &pool, ring_guest, &server);
    streams_ch = ch;
    streams_n = n;
    write_streams();
    say(-1, "hub: %d media ports, QEMU %s / %s-<n>.sock, viewers %s / %s-<n>.sock", ports, vq, mq,
        vv, mv);
    struct timespec last_try = {0};
    for (;;) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec != last_try.tv_sec) {
            last_try = now;
            if (pid_fd < 0 && kill(pid, 0) < 0 && errno == ESRCH)
                break;
            hm_log_tick(&sessions);
            hm_log_tick(&hm_engine_log);
            static uint64_t last_used = 0, last_file = 0;
            if (have_pool) {
                uint64_t used = pool_used_bytes(&pool), file = pool_file_bytes(&pool);
                if (used != last_used || file != last_file)
                    say(-1, "pool: %llu KiB allocated, %llu KiB in the file",
                        (unsigned long long)used >> 10, (unsigned long long)file >> 10);
                last_used = used;
                last_file = file;
            }
            for (int i = 0; i < n; i++)
                if (ch[i].gfd < 0 && (ch[i].gfd = unix_connect(ch[i].qemu)) >= 0)
                    say(ch[i].media ? ch[i].index : -1, "%s %d: QEMU's socket connected",
                        ch[i].media ? "media" : "video", ch[i].index);
        }
        struct pollfd pf[3 * (MAX_PORTS + 1) + 5];
        int map[3 * (MAX_PORTS + 1) + 5][2], k = 0;
        pf[k] = (struct pollfd){wake_fd, POLLIN, 0};
        map[k][0] = 0;
        map[k++][1] = 3;
        if (pid_fd >= 0) {
            /* readable once QEMU (the launcher it became) has ended */
            pf[k] = (struct pollfd){pid_fd, POLLIN, 0};
            map[k][0] = 0;
            map[k++][1] = 7;
        }
        if (have_pool) {
            pf[k] = (struct pollfd){server.listen_fd, POLLIN, 0};
            map[k][0] = 0;
            map[k++][1] = 4;
            pf[k] = (struct pollfd){server.self_ev, POLLIN, 0};
            map[k][0] = 0;
            map[k++][1] = 5;
            if (server.sock >= 0) {
                pf[k] = (struct pollfd){server.sock, POLLIN, 0};
                map[k][0] = 0;
                map[k++][1] = 6;
            }
        }
        for (int i = 0; i < n; i++) {
            struct chan *c = &ch[i];
            if (c->gfd >= 0) {
                pf[k] = (struct pollfd){c->gfd, (short)(POLLIN | (c->ghead ? POLLOUT : 0)), 0};
                map[k][0] = i;
                map[k++][1] = 0;
            }
            pf[k] = (struct pollfd){c->lfd, POLLIN, 0};
            map[k][0] = i;
            map[k++][1] = 1;
            if (c->vfd >= 0) {
                pf[k] = (struct pollfd){c->vfd, (short)(POLLIN | (c->qhead ? POLLOUT : 0)), 0};
                map[k][0] = i;
                map[k++][1] = 2;
            }
        }
        if (poll(pf, (nfds_t)k, 5) < 0 && errno != EINTR)
            break;
        int ended = 0;
        for (int j = 0; j < k; j++)
            ended |= map[j][1] == 7 && pf[j].revents;
        if (ended)
            break;
        for (int j = 0; j < k; j++) {
            struct chan *c = &ch[map[j][0]];
            if (!pf[j].revents)
                continue;
            if (map[j][1] == 4) {
                int64_t id = ivserver_accept(&server);
                if (id >= 0)
                    say(-1, "pool: QEMU connected as peer %lld", (long long)id);
                else
                    say(-1, "pool: a connection refused (QEMU's is the only one)");
                continue;
            }
            if (map[j][1] == 5) {
                if (ivserver_take_doorbell(&server))
                    decoder_doorbell(decode);
                continue;
            }
            if (map[j][1] == 6) {
                if (ivserver_client_event(&server))
                    say(-1, "pool: QEMU's connection closed");
                continue;
            }
            if (map[j][1] == 3) {
                uint64_t count;
                if (read(wake_fd, &count, sizeof count) < 0) {
                    /* EAGAIN: taken already */
                }
                for (int i = 1; i < n; i++) {
                    take_replies(&ch[i]);
                    guest_write(&ch[i]);
                }
                continue;
            }
            if (map[j][1] == 0 && c->gfd == pf[j].fd && (pf[j].revents & POLLOUT))
                guest_write(c);
            if (map[j][1] == 0 && c->gfd == pf[j].fd && (pf[j].revents & ~POLLOUT))
                guest_read(c);
            else if (map[j][1] == 1)
                viewer_accept(c);
            else if (map[j][1] == 2 && c->vfd == pf[j].fd) {
                if (c->media) {
                    /* Drain RELEASE datagrams before treating EOF as an
                     * unclean disconnect. HUP can arrive with queued releases. */
                    if (decoder_tick(decode, c->index) < 0)
                        viewer_close(c, "left");
                    continue;
                }
                if (pf[j].revents & (POLLHUP | POLLERR)) {
                    viewer_close(c, "left");
                    continue;
                }
                if (!c->media && (pf[j].revents & POLLIN)) {
                    char b[256];
                    ssize_t r = recv(c->vfd, b, sizeof b, 0);
                    if (r == 0 || (r < 0 && errno != EAGAIN)) {
                        viewer_close(c, "left");
                        continue;
                    }
                }
                if (!c->media && (pf[j].revents & POLLOUT))
                    viewer_write(c);
            }
        }
        for (int i = 1; i < n; i++)
            if (decoder_tick(decode, ch[i].index) < 0)
                viewer_close(&ch[i], "left");
    }
    say(-1, "hub: the VM ended");
    if (streams_path)
        unlink(streams_path);
    streams_ch = NULL;
    for (int i = 0; i < n; i++) {
        viewer_close(&ch[i], "VM ended");
        if (ch[i].gfd >= 0)
            close(ch[i].gfd);
        close(ch[i].lfd);
        if (!nviewer_fds)
            unlink(ch[i].viewer);
        free(ch[i].in);
        free(ch[i].open);
        free(ch[i].caps);
        free(ch[i].state);
        free(ch[i].table);
        drop_guest_queue(&ch[i], 0);
    }
    decoder_destroy(decode);
    if (have_pool) {
        ivserver_fini(&server);
        pool_fini(&pool);
    }
    hm_log_tick(&sessions);
    hm_log_tick(&hm_engine_log);
    hm_log_close(&sessions);
    hm_log_close(&hm_engine_log);
    return 0;
}
