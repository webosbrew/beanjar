// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* A pool session's slots laid out again for a bigger picture while the guest holds one of the
 * old ones (host/media/decode/frame.c, take_slot): a sink keeps the last buffer it rendered, so
 * a stream that grows must not wait for every slot to be free. The pictures go through
 * return_picture() into a real pool on a memfd; the guest is modelled here: it exports each
 * shared picture as its kernel does (refused unless the slot lies in a LIVE extent of the port),
 * and, like a sink with its last sample, gives a picture back when it takes the next one. The
 * pictures are synthetic NV12 frames: FFmpeg's own size change is not what is checked. Prints
 * TAP; exit status 0 only if every check passed. */
#define _GNU_SOURCE
#include "../../../host/media/decode/decode-priv.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static int n, failed;
static void ok(int cond, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    printf("%s %d - ", cond ? "ok" : "not ok", ++n);
    vprintf(fmt, ap);
    printf("\n");
    va_end(ap);
    failed += !cond;
}

/* the session's inline answers (the pool had no room) */
static unsigned inline_pictures;
static void reply(void *ctx, unsigned port, unsigned char *m, size_t len)
{
    (void)ctx;
    (void)port;
    if (len > sizeof(struct webos_media_header) + sizeof(struct webos_media_picture)) {
        struct webos_media_picture p;
        memcpy(&p, m + sizeof(struct webos_media_header), sizeof p);
        inline_pictures += p.format != 0;
    }
    free(m);
}

struct held {
    uint32_t slot, input, width;
    int e;
};

struct guest {
    struct decoder *m;
    struct stream *s;
    struct pool pool;
    uint32_t pic_tail, rel_head;
    unsigned taken, refused, by_width[2]; /* exported pictures: small, big */
    struct held held[64];
    unsigned nheld;
};

/* the kernel's EXPORT: the LIVE extent of port 0 holding the range, its count up; -1 refused */
static int export(struct guest *g, uint32_t off, uint32_t size)
{
    struct webos_media_pool_port *pp = &g->pool.dir->port[0];
    for (int e = 0; e < WEBOS_MEDIA_POOL_EXTENTS; e++) {
        struct webos_media_extent *x = &pp->extent[e];
        if (x->state == WEBOS_MEDIA_EXTENT_LIVE && off >= x->offset &&
            off + size <= x->offset + x->size) {
            pp->exports[e]++;
            return e;
        }
    }
    return -1;
}

static void release(struct guest *g, const struct held *h)
{
    struct webos_media_control *c = g->s->rings.c;
    c->release[g->rel_head % WEBOS_MEDIA_RING_ENTRIES] =
        (struct webos_media_release){h->slot, h->input};
    g->rel_head++;
    __atomic_store_n(&c->releases.head, g->rel_head, __ATOMIC_RELEASE);
    if (h->e >= 0)
        g->pool.dir->port[0].exports[h->e]--;
}

/* Takes what is on the picture ring. keep: how many pictures the guest holds afterwards (a sink:
 * 1, the last); 0 for none, -1 to keep every one. */
static void take(struct guest *g, int keep)
{
    struct webos_media_control *c = g->s->rings.c;
    uint32_t head = __atomic_load_n(&c->pictures.head, __ATOMIC_ACQUIRE);
    while (g->pic_tail != head) {
        struct webos_media_picture p = c->picture[g->pic_tail++ % WEBOS_MEDIA_RING_ENTRIES];
        __atomic_store_n(&c->pictures.tail, g->pic_tail, __ATOMIC_RELEASE);
        if (!(p.flags & WEBOS_MEDIA_PICTURE_SHARED))
            continue;
        uint32_t size = p.offset[1] + p.stride[1] * ((p.height + 1) / 2);
        struct held h = {p.slot, p.input, p.width, export(g, p.slot, size)};
        if (h.e < 0) { /* as mediaport.c: not mapped, the slot given back */
            g->refused++;
            release(g, &h);
            continue;
        }
        g->taken++;
        g->by_width[p.width > 640]++;
        if (g->nheld < 64)
            g->held[g->nheld++] = h;
    }
    while (keep >= 0 && g->nheld > (unsigned)keep) {
        release(g, &g->held[0]);
        memmove(g->held, g->held + 1, --g->nheld * sizeof *g->held);
    }
}

static void begin(struct guest *g)
{
    memset(g, 0, sizeof *g);
    int fd = memfd_create("check-relayout", 0);
    if (fd < 0 || ftruncate(fd, WEBOS_MEDIA_POOL_SIZE) ||
        pool_init(&g->pool, fd, WEBOS_MEDIA_POOL_SIZE, 0)) {
        perror("pool");
        exit(2);
    }
    g->m = calloc(1, sizeof *g->m);
    g->m->ports = 1;
    g->m->pool = &g->pool;
    g->m->reply = reply;
    struct stream *s = g->s = &g->m->streams[0];
    s->m = g->m;
    s->generation = s->dec_generation = 1;
    s->dec_ret = (int)WEBOS_MEDIA_NV12;
    s->ctrl_e = s->pic_e = s->old_e = -1;
    pthread_mutex_init(&s->lock, NULL);
    pthread_condattr_t ca; /* as decoder.c: take_slot waits on the monotonic clock */
    pthread_condattr_init(&ca);
    pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
    pthread_cond_init(&s->cond, &ca);
    pthread_condattr_destroy(&ca);
    uint32_t off;
    s->ctrl_e = pool_alloc(&g->pool, 0, sizeof(struct webos_media_control), &off, NULL);
    s->shared = 1;
    rings_init(&s->rings, (struct webos_media_control *)(g->pool.base + off));
    inline_pictures = 0;
}

static void end(struct guest *g)
{
    pthread_mutex_lock(&g->s->lock);
    end_pool_session(g->s);
    pthread_mutex_unlock(&g->s->lock);
    pthread_mutex_destroy(&g->s->lock);
    pthread_cond_destroy(&g->s->cond);
    int fd = g->pool.fd;
    pool_fini(&g->pool);
    close(fd);
    free(g->m);
}

static AVFrame *frame(int w, int h)
{
    AVFrame *f = av_frame_alloc();
    f->format = AV_PIX_FMT_NV12;
    f->width = w;
    f->height = h;
    if (av_frame_get_buffer(f, 0) < 0)
        exit(2);
    memset(f->data[0], 0x40, (size_t)f->linesize[0] * (size_t)h);
    memset(f->data[1], 0x80, (size_t)f->linesize[1] * (size_t)((h + 1) / 2));
    return f;
}

static unsigned port_extents(struct guest *g)
{
    unsigned k = 0;
    for (int e = 0; e < WEBOS_MEDIA_POOL_EXTENTS; e++)
        k += g->pool.dir->port[0].extent[e].state != WEBOS_MEDIA_EXTENT_FREE;
    return k;
}

#define SMALL 8
#define BIG   24

int main(void)
{
    struct guest g;
    AVFrame *small = frame(640, 360), *big = frame(1280, 720);
    uint32_t input = 1;

    /* 1. A sink holds its last picture while the stream grows: every bigger picture must come. */
    begin(&g);
    for (unsigned i = 0; i < SMALL; i++, input++) {
        return_picture(g.s, small, input, 0);
        take(&g, 1);
    }
    int64_t t0 = mono_ns();
    for (unsigned i = 0; i < BIG; i++, input++) {
        return_picture(g.s, big, input, 0);
        take(&g, 1);
    }
    double ms = (double)(mono_ns() - t0) / 1e6;
    ok(g.by_width[0] == SMALL, "640x360 pictures before the growth: %u of %d", g.by_width[0],
       SMALL);
    ok(g.by_width[1] == BIG && g.s->slot_drops == 0,
       "1280x720 pictures with one 640x360 slot held: %u of %d, %llu dropped, %.1f ms waiting "
       "for slots",
       g.by_width[1], BIG, g.s->slot_drops, (double)g.s->slot_wait_ns / 1e6);
    ok(ms < BIG * 10.0, "the bigger pictures took %.1f ms (at most %d ms)", ms, BIG * 10);
    pthread_mutex_lock(&g.s->lock);
    take_releases(g.s);
    pthread_mutex_unlock(&g.s->lock);
    pool_reap(&g.pool);
    ok(port_extents(&g) == 2 && g.pool.frees >= 1,
       "the old slots freed once given back: %u extents in use (control and slots), %llu freed",
       port_extents(&g), g.pool.frees);
    ok(g.s->bad_releases == 0, "no release named no slot: %llu", g.s->bad_releases);
    end(&g);

    /* 2. A picture of the old size on the ring, not yet taken, when the bigger one comes: the
     * guest can still map it. */
    begin(&g);
    return_picture(g.s, small, input++, 0);
    take(&g, 1);
    return_picture(g.s, small, input++, 0); /* on the ring */
    return_picture(g.s, big, input++, 0);
    take(&g, 1);
    ok(g.refused == 0 && g.by_width[0] == 2 && g.by_width[1] == 1,
       "a 640x360 picture posted before the growth and taken after: %u refused, %u small and %u "
       "big mapped",
       g.refused, g.by_width[0], g.by_width[1]);
    end(&g);

    /* 3. Growing four times, a sink holding its last picture: no drop, at most four extents. */
    begin(&g);
    static const int sizes[][2] = {{320, 180}, {640, 360}, {1280, 720}, {1920, 1080}, {3840, 2160}};
    unsigned most = 0, posted = 0;
    for (unsigned k = 0; k < 5; k++) {
        AVFrame *f = frame(sizes[k][0], sizes[k][1]);
        for (unsigned i = 0; i < 6; i++, input++, posted++) {
            return_picture(g.s, f, input, 0);
            take(&g, 1);
            unsigned x = port_extents(&g);
            most = x > most ? x : most;
        }
        av_frame_free(&f);
    }
    ok(g.taken == posted && g.s->slot_drops == 0 && !g.s->inline_only,
       "320x180 to 3840x2160 in four steps: %u of %u pictures shared, %llu dropped", g.taken,
       posted, g.s->slot_drops);
    ok(most <= WEBOS_MEDIA_POOL_EXTENTS, "at most %u extents in use", most);
    end(&g);

    /* 4. A guest that keeps a picture of every size: no stall when the extents run out, the
     * pictures inline from then on. */
    begin(&g);
    posted = 0;
    t0 = mono_ns();
    for (unsigned k = 0; k < 5; k++) {
        AVFrame *f = frame(sizes[k][0], sizes[k][1]);
        for (unsigned i = 0; i < 2; i++, input++, posted++) {
            return_picture(g.s, f, input, 0);
            take(&g, -1);
            /* gives back all but the first picture of each size */
            while (g.nheld > k + 1) {
                release(&g, &g.held[g.nheld - 1]);
                g.nheld--;
            }
        }
        av_frame_free(&f);
    }
    ms = (double)(mono_ns() - t0) / 1e6;
    ok(g.taken + inline_pictures == posted && g.s->slot_drops == 0,
       "a picture of every size held: %u shared and %u inline of %u, %llu dropped, %.1f ms",
       g.taken, inline_pictures, posted, g.s->slot_drops, ms);
    ok(port_extents(&g) <= WEBOS_MEDIA_POOL_EXTENTS, "%u extents in use", port_extents(&g));
    end(&g);

    av_frame_free(&small);
    av_frame_free(&big);
    printf("1..%d\n", n);
    return failed ? 1 : 0;
}
