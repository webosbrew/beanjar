// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The decoder thread: packets in, pictures and PCM chunks out, with download
 * and 10-bit conversion for frames the viewer cannot take as they are. */
#include "decode-priv.h"

#include <libavcodec/avcodec.h>
#include <libavutil/pixdesc.h>
#include <libswresample/swresample.h>
#include <malloc.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

void free_frames(struct stream *s)
{
    for (unsigned i = 0; i < s->nframes; i++)
        av_frame_free(&s->frames[i]);
    s->nframes = 0;
    av_frame_free(&s->current);
    s->current_epoch = 0;
    for (struct chunk *c = s->ahead, *n; c; c = n) {
        n = c->next;
        free(c);
    }
    s->ahead = s->atail = NULL;
    s->aqueued = 0;
}
/* 10-bit 4:2:0 to 8-bit (GLES2 has no 16-bit texture formats to take it as it is). */
AVFrame *to_8bit(AVFrame *f)
{
    AVFrame *o = av_frame_alloc();
    o->format = AV_PIX_FMT_YUV420P;
    o->width = f->width;
    o->height = f->height;
    if (av_frame_get_buffer(o, 0) < 0) {
        av_frame_free(&o);
        return NULL;
    }
    av_frame_copy_props(o, f);
    for (int plane = 0; plane < 3; plane++) {
        int w = plane ? (f->width + 1) / 2 : f->width, h = plane ? (f->height + 1) / 2 : f->height;
        for (int y = 0; y < h; y++) {
            const uint16_t *src =
                (const uint16_t *)(f->data[plane] + (size_t)y * f->linesize[plane]);
            uint8_t *dst = o->data[plane] + (size_t)y * o->linesize[plane];
            for (int x = 0; x < w; x++)
                dst[x] = (uint8_t)(src[x] >> 2);
        }
    }
    return o;
}

/* P010 (a hardware decoder's 10-bit 4:2:0, downloaded) to NV12. */
AVFrame *p010_to_nv12(AVFrame *f)
{
    AVFrame *o = av_frame_alloc();
    o->format = AV_PIX_FMT_NV12;
    o->width = f->width;
    o->height = f->height;
    if (av_frame_get_buffer(o, 0) < 0) {
        av_frame_free(&o);
        return NULL;
    }
    av_frame_copy_props(o, f);
    for (int plane = 0; plane < 2; plane++) {
        int w = plane ? (f->width + 1) / 2 * 2 : f->width,
            h = plane ? (f->height + 1) / 2 : f->height;
        for (int y = 0; y < h; y++) {
            const uint16_t *src =
                (const uint16_t *)(f->data[plane] + (size_t)y * f->linesize[plane]);
            uint8_t *dst = o->data[plane] + (size_t)y * o->linesize[plane];
            for (int x = 0; x < w; x++)
                dst[x] = (uint8_t)(src[x] >> 8);
        }
    }
    return o;
}

static int supported_sw(int format)
{
    switch (format) {
    case AV_PIX_FMT_YUV420P:
    case AV_PIX_FMT_YUVJ420P:
    case AV_PIX_FMT_YUV422P:
    case AV_PIX_FMT_YUVJ422P:
    case AV_PIX_FMT_YUV444P:
    case AV_PIX_FMT_YUVJ444P:
    case AV_PIX_FMT_NV12:
    case AV_PIX_FMT_YUV420P10LE:
        return 1;
    }
    return 0;
}

/* The layout of include/webos-media.h for f's size in format (GstVideoInfo's default): FALSE
 * for a format with no conversion here. */
static int layout(const AVFrame *f, uint32_t format, struct webos_media_picture *p, size_t *bytes)
{
    const AVPixFmtDescriptor *d = av_pix_fmt_desc_get(f->format);
    int planar = d && d->nb_components == 3 && (d->flags & AV_PIX_FMT_FLAG_PLANAR) &&
                 !(d->flags & (AV_PIX_FMT_FLAG_HWACCEL | AV_PIX_FMT_FLAG_RGB)) &&
                 d->comp[0].depth == 8 && d->comp[1].depth == 8 && d->comp[2].depth == 8;
    if (f->width <= 0 || f->height <= 0 || f->width > HOST_MEDIA_MAX_DIMENSION ||
        f->height > HOST_MEDIA_MAX_DIMENSION ||
        !(planar || f->format == AV_PIX_FMT_NV12 || f->format == AV_PIX_FMT_GRAY8))
        return 0;
    p->format = format;
    p->width = (uint32_t)f->width;
    p->height = (uint32_t)f->height;
    if (format == WEBOS_MEDIA_YUY2) {
        p->stride[0] = ((uint32_t)f->width * 2 + 3) & ~3u;
        p->stride[1] = p->offset[0] = p->offset[1] = 0;
        *bytes = (size_t)p->stride[0] * (uint32_t)f->height;
        return 1;
    }
    uint32_t stride = ((uint32_t)f->width + 3) & ~3u, rows = ((uint32_t)f->height + 1) & ~1u;
    p->stride[0] = p->stride[1] = stride;
    p->offset[0] = 0;
    p->offset[1] = stride * rows;
    *bytes = (size_t)stride * rows + (size_t)stride * (rows / 2);
    return 1;
}

/* f into NV12 at out, laid out as p says. 4:2:0 planes interleave; other planar subsamplings take
 * the chroma sample at each 4:2:0 position (a JPEG's 4:2:2 drops every other row). */
static void to_nv12(const AVFrame *f, uint8_t *out, const struct webos_media_picture *p)
{
    int w = f->width, h = f->height, cw = (w + 1) / 2, ch = (h + 1) / 2;
    uint8_t *y = out + p->offset[0], *uv = out + p->offset[1];
    for (int r = 0; r < h; r++) {
        memcpy(y + (size_t)r * p->stride[0], f->data[0] + (size_t)r * f->linesize[0], (size_t)w);
        memset(y + (size_t)r * p->stride[0] + w, 0, p->stride[0] - (size_t)w);
    }
    if (h & 1) /* the row RU2(height) adds */
        memset(y + (size_t)h * p->stride[0], 0, p->stride[0]);
    for (int r = 0; r < ch; r++) /* padding; the samples are written below */
        memset(uv + (size_t)r * p->stride[1] + (size_t)cw * 2, 0, p->stride[1] - (size_t)cw * 2);
    if (f->format == AV_PIX_FMT_GRAY8) {
        for (int r = 0; r < ch; r++)
            memset(uv + (size_t)r * p->stride[1], 128, (size_t)cw * 2);
        return;
    }
    if (f->format == AV_PIX_FMT_NV12) {
        for (int r = 0; r < ch; r++)
            memcpy(uv + (size_t)r * p->stride[1], f->data[1] + (size_t)r * f->linesize[1],
                   (size_t)cw * 2);
        return;
    }
    const AVPixFmtDescriptor *d = av_pix_fmt_desc_get(f->format);
    int sx = 1 - d->log2_chroma_w, sy = 1 - d->log2_chroma_h; /* 4:2:0 position -> source */
    for (int r = 0; r < ch; r++) {
        int src_row = sy >= 0 ? r << sy : r >> -sy;
        int max_row = (h + (1 << d->log2_chroma_h) - 1) >> d->log2_chroma_h;
        if (src_row >= max_row)
            src_row = max_row - 1;
        const uint8_t *u = f->data[1] + (size_t)src_row * f->linesize[1];
        const uint8_t *v = f->data[2] + (size_t)src_row * f->linesize[2];
        uint8_t *o = uv + (size_t)r * p->stride[1];
        if (sx == 0)
            for (int x = 0; x < cw; x++) {
                o[2 * x] = u[x];
                o[2 * x + 1] = v[x];
            }
        else {
            int max_col = (w + (1 << d->log2_chroma_w) - 1) >> d->log2_chroma_w;
            for (int x = 0; x < cw; x++) {
                int c = sx > 0 ? x << sx : x >> -sx;
                if (c >= max_col)
                    c = max_col - 1;
                o[2 * x] = u[c];
                o[2 * x + 1] = v[c];
            }
        }
    }
}

/* The chroma sample of f's plane (1 or 2) at chroma position (x, y) of a grid subsampled by
 * (sx, sy) (log2), clamped to the plane. NV12: the interleaved plane. GRAY8: 128. */
static inline uint8_t chroma_at(const AVFrame *f, const AVPixFmtDescriptor *d, int plane, int x,
                                int y, int sx, int sy)
{
    if (f->format == AV_PIX_FMT_GRAY8)
        return 128;
    int lw = f->format == AV_PIX_FMT_NV12 ? 1 : d->log2_chroma_w;
    int lh = f->format == AV_PIX_FMT_NV12 ? 1 : d->log2_chroma_h;
    int cx = (x << sx) >> lw, cy = (y << sy) >> lh;
    int mw = (f->width + (1 << lw) - 1) >> lw, mh = (f->height + (1 << lh) - 1) >> lh;
    if (cx >= mw)
        cx = mw - 1;
    if (cy >= mh)
        cy = mh - 1;
    if (f->format == AV_PIX_FMT_NV12)
        return f->data[1][(size_t)cy * f->linesize[1] + 2 * cx + plane - 1];
    return f->data[plane][(size_t)cy * f->linesize[plane] + cx];
}

/* f into YUY2 at out: each pair of pixels with the chroma sample at its 4:2:2 position. */
static void to_yuy2(const AVFrame *f, uint8_t *out, const struct webos_media_picture *p)
{
    const AVPixFmtDescriptor *d = av_pix_fmt_desc_get(f->format);
    int w = f->width, h = f->height;
    for (int r = 0; r < h; r++) {
        const uint8_t *y = f->data[0] + (size_t)r * f->linesize[0];
        uint8_t *o = out + (size_t)r * p->stride[0];
        int x = 0;
        int c422 = f->format == AV_PIX_FMT_YUV422P || f->format == AV_PIX_FMT_YUVJ422P;
        int c420 = f->format == AV_PIX_FMT_YUV420P || f->format == AV_PIX_FMT_YUVJ420P;
        if (c422 || c420) { /* the common cases: a JPEG's 4:2:2 or 4:2:0 */
            int cr = c420 ? r >> 1 : r;
            const uint8_t *u = f->data[1] + (size_t)cr * f->linesize[1];
            const uint8_t *v = f->data[2] + (size_t)cr * f->linesize[2];
            for (; x + 1 < w; x += 2, o += 4) {
                o[0] = y[x];
                o[1] = u[x >> 1];
                o[2] = y[x + 1];
                o[3] = v[x >> 1];
            }
        }
        for (; x < w; x += 2, o += 4) {
            o[0] = y[x];
            o[1] = chroma_at(f, d, 1, x >> 1, r, 1, 0);
            o[2] = x + 1 < w ? y[x + 1] : y[x];
            o[3] = chroma_at(f, d, 2, x >> 1, r, 1, 0);
        }
        memset(o, 0, p->stride[0] - (size_t)(o - (out + (size_t)r * p->stride[0])));
    }
}

/* A free slot of the session's picture extent for a picture of bytes, for input: 0 with its pool
 * offset in *offset; -1 when none came free within WEBOS_MEDIA_RELEASE_WAIT_MS (or the session
 * moved on); -2 when the session's answers are inline (no pool session, or no room in the pool
 * for its slots: inline from then on). A bigger picture than the slots hold lays them out again
 * at once in a new extent: a sink keeps its last picture until the next one comes, so waiting for
 * every slot would never end. The old extent retires when the guest has none of its slots, or
 * else stays LIVE until it gives the last back (take_releases), so that a picture of it still on
 * the ring can be mapped; the layout before that retires at once (its pictures were taken). */
static int take_slot(struct stream *s, size_t bytes, uint32_t input, unsigned gen, uint32_t *offset)
{
    struct pool *pool = s->m->pool;
    uint64_t need64 = ((uint64_t)bytes + 4095) & ~(uint64_t)4095;
    uint32_t need = need64 > UINT32_MAX ? UINT32_MAX : (uint32_t)need64;
    struct timespec until;
    clock_gettime(CLOCK_MONOTONIC, &until);
    until.tv_nsec += WEBOS_MEDIA_RELEASE_WAIT_MS * 1000000l;
    until.tv_sec += until.tv_nsec / 1000000000l;
    until.tv_nsec %= 1000000000l;
    int r = -1;
    int64_t t0 = mono_ns();
    pthread_mutex_lock(&s->lock);
    for (;;) {
        if (s->m->quit || gen != s->generation)
            break;
        if (!s->shared || s->inline_only || !pool) {
            r = -2;
            break;
        }
        take_releases(s);
        unsigned busy = 0;
        for (unsigned i = 0; i < s->nslots; i++)
            busy += s->slots[i].busy != 0;
        if (s->slot_size < need) {
            retire_old_layout(s);
            if (s->pic_e >= 0 && busy) { /* the guest's yet: the layout before */
                s->old_e = s->pic_e;
                s->old_off = s->pic_off;
                s->old_slot_size = s->slot_size;
                s->old_bytes = s->slot_size * s->nslots;
                s->old_nslots = s->nslots;
                memcpy(s->old_slots, s->slots, sizeof s->slots);
            } else if (s->pic_e >= 0)
                pool_retire(pool, (unsigned)s->index, s->pic_e);
            s->pic_e = -1;
            s->slot_size = 0;
            s->nslots = 0;
            pool_reap(pool);
            uint32_t off = 0;
            int e = -1;
            unsigned n;
            for (n = WEBOS_MEDIA_POOL_SLOTS; n >= 1; n--) {
                if ((uint64_t)need * n + s->old_bytes >
                    WEBOS_MEDIA_POOL_SESSION - WEBOS_MEDIA_POOL_GRANULE)
                    continue;
                if ((e = pool_alloc(pool, (unsigned)s->index, need * n, &off, NULL)) >= 0)
                    break;
            }
            if (e < 0) { /* also a picture bigger than a session may have */
                s->inline_only = 1;
                elog(s->index,
                     "media %d: pool full (%u-byte pictures): the session's pictures "
                     "inline from now on",
                     s->index, need);
                r = -2;
                break;
            }
            s->pic_e = e;
            s->pic_off = off;
            s->slot_size = need;
            s->nslots = n;
            memset(s->slots, 0, sizeof s->slots);
        }
        if (s->slot_size >= need) {
            for (unsigned i = 0; i < s->nslots; i++)
                if (!s->slots[i].busy) {
                    s->slots[i].busy = 1;
                    s->slots[i].input = input;
                    *offset = s->pic_off + i * s->slot_size;
                    r = 0;
                    break;
                }
            if (!r)
                break;
        }
        /* sleep until a release: the flag first, then one more look (the guest rings only when
         * it sees the flag) */
        if (rings_wait_release(&s->rings)) {
            rings_done_waiting(&s->rings);
            continue;
        }
        int t = pthread_cond_timedwait(&s->cond, &s->lock, &until);
        if (s->rings.c)
            rings_done_waiting(&s->rings);
        if (t)
            break;
    }
    if (r == -1)
        s->slot_drops++;
    s->slot_wait_ns += mono_ns() - t0;
    pthread_mutex_unlock(&s->lock);
    return r;
}

/* p on the session's picture ring, if its answers go there (with s->lock): 1 posted, 0 the
 * session's answers are inline, -1 dropped (the ring stayed full). *ring: the guest sleeps. */
static int post_picture(struct stream *s, const struct webos_media_picture *p, unsigned gen,
                        int *ring)
{
    for (int tries = 0;; tries++) {
        if (s->m->quit || gen != s->generation)
            return -1;
        if (!s->shared || s->inline_only || !s->rings.c)
            return 0;
        if (rings_put_picture(&s->rings, p, ring))
            return 1;
        if (tries >= WEBOS_MEDIA_RELEASE_WAIT_MS)
            return -1;
        struct timespec until;
        clock_gettime(CLOCK_MONOTONIC, &until);
        until.tv_nsec += 1000000l;
        until.tv_sec += until.tv_nsec / 1000000000l;
        until.tv_nsec %= 1000000000l;
        pthread_cond_timedwait(&s->cond, &s->lock, &until);
    }
}

void return_picture(struct stream *s, AVFrame *f, uint32_t input, int last)
{
    struct webos_media_picture p = {
        .input = input, .flags = last ? WEBOS_MEDIA_PICTURE_LAST : 0, .pts = NONE};
    size_t bytes = 0, len;
    int64_t t0 = mono_ns();
    unsigned gen = s->dec_generation;
    if (f && !layout(f, (uint32_t)s->dec_ret, &p, &bytes)) {
        static int warned;
        if (!warned++)
            elog(s->index, "media %d: pixel format %s not returned", s->index,
                 av_get_pix_fmt_name(f->format));
        s->return_errors++;
        f = NULL;
    }
    uint8_t *out = NULL;
    if (f) {
        uint32_t off = 0;
        int r = take_slot(s, bytes, input, gen, &off);
        if (r == -1)
            f = NULL; /* dropped: counted */
        else if (r == 0) {
            p.flags |= WEBOS_MEDIA_PICTURE_SHARED;
            p.slot = off;
            out = s->m->pool->base + off;
            bytes = 0;
        }
    }
    if (!f) {
        memset(&p, 0, sizeof p);
        p.input = input;
        p.flags = last ? WEBOS_MEDIA_PICTURE_LAST : 0;
        p.pts = NONE;
        bytes = 0;
        if (!last)
            return;
    }
    if (f) {
        p.pts = f->pts;
        if (out) {
            if (p.format == WEBOS_MEDIA_YUY2)
                to_yuy2(f, out, &p);
            else
                to_nv12(f, out, &p);
        }
    }
    /* a pool session's answers go on its ring */
    int ring = 0;
    pthread_mutex_lock(&s->lock);
    int posted = post_picture(s, &p, gen, &ring);
    if (posted < 0 && out) /* its slot back: the guest never sees it */
        for (unsigned i = 0; i < s->nslots; i++)
            if (s->slot_size && s->pic_off + i * s->slot_size == p.slot)
                s->slots[i].busy = 0;
    pthread_mutex_unlock(&s->lock);
    if (ring && s->m->ring_guest)
        s->m->ring_guest(s->m->ring_ctx);
    if (posted < 0 || (posted == 0 && out)) {
        /* dropped, or a shared picture whose session ended meanwhile: nothing to send */
        s->return_errors += posted < 0;
        if (posted == 0 && last) {
            memset(&p, 0, sizeof p);
            p.input = input;
            p.flags = WEBOS_MEDIA_PICTURE_LAST;
            p.pts = NONE;
            f = NULL;
            out = NULL;
        } else
            return;
    }
    if (posted == 1) {
        if (f) {
            s->returned++;
            s->return_ns += mono_ns() - t0;
        }
        return;
    }
    unsigned char *m = hm_picture(++s->reply_seq, &p, bytes, &len);
    if (!m) {
        s->return_errors++;
        return;
    }
    if (f) {
        out = m + len - bytes;
        if (p.format == WEBOS_MEDIA_YUY2)
            to_yuy2(f, out, &p);
        else
            to_nv12(f, out, &p);
        s->returned++;
        s->return_ns += mono_ns() - t0;
    }
    if (s->m->reply)
        s->m->reply(s->m->reply_ctx, (unsigned)s->index, m, len);
    else
        free(m);
}

static void push_frame(struct stream *s, AVFrame *f, unsigned gen)
{
    int64_t pts = f->best_effort_timestamp != AV_NOPTS_VALUE ? f->best_effort_timestamp : f->pts;
    for (unsigned i = 0; i < s->decode_only_n && pts != AV_NOPTS_VALUE; i++)
        if (s->decode_only[i] == pts)
            return; /* before the segment: reference only */
    AVFrame *out = av_frame_clone(f);
    if (!out)
        return;
    out->pts = pts == AV_NOPTS_VALUE ? NONE : pts;
    if (out->format == s->hw_format && s->hw_format != AV_PIX_FMT_NONE) {
        pthread_mutex_lock(&s->lock);
        int download = s->dec_ret || s->hw_import_failed || s->force_download ||
                       out->format != AV_PIX_FMT_DRM_PRIME;
        pthread_mutex_unlock(&s->lock);
        if (download) {
            AVFrame *sw = av_frame_alloc();
            if (av_hwframe_transfer_data(sw, out, 0) < 0) {
                av_frame_free(&sw);
                av_frame_free(&out);
                s->errors++;
                return;
            }
            av_frame_copy_props(sw, out);
            av_frame_free(&out);
            out = sw;
        }
    }
    if (out->format == AV_PIX_FMT_YUV420P10LE || out->format == AV_PIX_FMT_P010LE ||
        out->format == AV_PIX_FMT_P016LE) {
        AVFrame *o = out->format == AV_PIX_FMT_YUV420P10LE ? to_8bit(out) : p010_to_nv12(out);
        av_frame_free(&out);
        if (!(out = o))
            return;
    }
    if (s->dec_ret) { /* a DECODE session: back to the guest, not to a viewer */
        if (gen == s->generation)
            return_picture(s, out, s->dec_input, 0);
        av_frame_free(&out);
        return;
    }
    if (out->format != AV_PIX_FMT_DRM_PRIME && !supported_sw(out->format)) {
        static int warned;
        if (!warned++)
            elog(s->index, "media %d: pixel format %s not shown", s->index,
                 av_get_pix_fmt_name(out->format));
        av_frame_free(&out);
        return;
    }
    pthread_mutex_lock(&s->lock);
    int64_t t0 = mono_ns();
    /* A full output queue drains as its pictures come due (decoder_tick), so wait for
     * space until this picture is due itself; drop it only once it is late. References
     * have already been decoded, so dropping a complete output cannot damage the GOP.
     * Flush, close, a kind change and quit end the wait; without a known deadline
     * (no state yet, paused) it waits as the viewer's decoder used to. */
    while (s->nframes >= (unsigned)s->queue_max && gen == s->generation && !s->m->quit) {
        int64_t deadline = out->pts == NONE ? NONE : due_mono(s, out->pts);
        if (deadline == NONE) {
            pthread_cond_wait(&s->cond, &s->lock);
            continue;
        }
        if (mono_ns() >= deadline)
            break;
        struct timespec ts = {deadline / 1000000000, deadline % 1000000000};
        pthread_cond_timedwait(&s->cond, &s->lock, &ts);
    }
    s->wait_ns += mono_ns() - t0;
    if (gen == s->generation && s->nframes < (unsigned)s->queue_max) {
        s->sent_epoch[s->nframes] = 0;
        s->frames[s->nframes++] = out;
        out = NULL;
    } else if (gen == s->generation && !s->m->quit)
        s->late_drops++; /* not a discarded session */
    pthread_mutex_unlock(&s->lock);
    av_frame_free(&out);
}

/* A DECODE session's samples (n stereo S16 frames from pts) to the guest, inline, as a PICTURE
 * of WEBOS_MEDIA_S16LE (include/webos-media.h); the input's LAST answer follows as for video. */
static void return_samples(struct stream *s, const uint8_t *data, int n, int64_t pts,
                           uint32_t input)
{
    struct webos_media_picture p = {.input = input,
                                    .pts = pts,
                                    .format = WEBOS_MEDIA_S16LE,
                                    .width = (uint32_t)n,
                                    .height = 2,
                                    .stride = {4, 0}};
    size_t bytes = (size_t)n * 4, len;
    int64_t t0 = mono_ns();
    unsigned char *m = hm_picture(++s->reply_seq, &p, bytes, &len);
    if (!m) {
        s->return_errors++;
        return;
    }
    memcpy(m + len - bytes, data, bytes);
    s->returned++;
    s->return_ns += mono_ns() - t0;
    if (s->m->reply)
        s->m->reply(s->m->reply_ctx, (unsigned)s->index, m, len);
    else
        free(m);
}

static void push_audio(struct stream *s, AVFrame *f, unsigned gen)
{
    int64_t pts = f->best_effort_timestamp != AV_NOPTS_VALUE ? f->best_effort_timestamp : f->pts;
    if (pts == AV_NOPTS_VALUE)
        pts = s->next_apts;
    if (pts != NONE && f->sample_rate > 0)
        s->next_apts = pts + (int64_t)f->nb_samples * 1000000000 / f->sample_rate;
    if (pts == NONE)
        return;
    /* a DECODE session: stereo S16 at WEBOS_MEDIA_S16LE_RATE back to the guest */
    int s16 = s->dec_ret == (int)WEBOS_MEDIA_S16LE;
    if (!s->swr || f->sample_rate != s->swr_rate || f->format != s->swr_format ||
        s16 != s->swr_s16 || av_channel_layout_compare(&f->ch_layout, &s->swr_layout)) {
        AVChannelLayout stereo = AV_CHANNEL_LAYOUT_STEREO;
        swr_free(&s->swr);
        s->swr_s16 = s16;
        if (swr_alloc_set_opts2(&s->swr, &stereo, s16 ? AV_SAMPLE_FMT_S16 : AV_SAMPLE_FMT_FLT,
                                s16 ? WEBOS_MEDIA_S16LE_RATE : OUT_RATE, &f->ch_layout, f->format,
                                f->sample_rate, 0, NULL) < 0 ||
            swr_init(s->swr) < 0) {
            swr_free(&s->swr);
            s->errors++;
            return;
        }
        av_channel_layout_uninit(&s->swr_layout);
        av_channel_layout_copy(&s->swr_layout, &f->ch_layout);
        s->swr_rate = f->sample_rate;
        s->swr_format = f->format;
    }
    int max = swr_get_out_samples(s->swr, f->nb_samples);
    if (max <= 0 || max > (int)(HOST_MEDIA_MAX_BYTES / 8))
        return;
    struct chunk *c = malloc(sizeof *c + (size_t)max * 2 * sizeof(float));
    if (!c) {
        s->errors++;
        return;
    }
    uint8_t *out[1] = {(uint8_t *)c->data};
    int n = swr_convert(s->swr, out, max, (const uint8_t **)f->extended_data, f->nb_samples);
    if (n <= 0) {
        free(c);
        return;
    }
    if (s16) {
        if (gen == s->generation)
            return_samples(s, (const uint8_t *)c->data, n, pts, s->dec_input);
        free(c);
        return;
    }
    c->next = NULL;
    c->rt = pts;
    c->frames = n;
    c->pos = 0;
    int64_t dur = (int64_t)n * 1000000000 / OUT_RATE;
    pthread_mutex_lock(&s->lock);
    int64_t t0 = mono_ns();
    /* Output chunks are bounded independently of decoder ingress. */
    s->wait_ns += mono_ns() - t0;
    if (gen == s->generation && s->aqueued + dur <= AUDIO_QUEUE_MAX_NS) {
        if (s->atail)
            s->atail->next = c;
        else
            s->ahead = c;
        s->atail = c;
        s->aqueued += dur;
        c = NULL;
    }
    pthread_mutex_unlock(&s->lock);
    free(c);
}

static void receive(struct stream *s, AVFrame *f, unsigned gen)
{
    for (;;) {
        int r = avcodec_receive_frame(s->ctx, f);
        if (r == AVERROR(EAGAIN) || r == AVERROR_EOF)
            return;
        if (r < 0) {
            s->errors++;
            s->hw_errors++;
            return;
        }
        int64_t pts = f->best_effort_timestamp;
        if (s->replay_until != NONE && pts != AV_NOPTS_VALUE) {
            if (pts <= s->replay_until) { /* given by the hardware before the fallback */
                av_frame_unref(f);
                continue;
            }
            s->replay_until = NONE;
        }
        if (s->hw_format != AV_PIX_FMT_NONE) {
            s->hw_gave = 1;
            if (pts != AV_NOPTS_VALUE)
                s->hw_last_pts = pts;
        }
        pthread_mutex_lock(&s->lock);
        if (gen == s->generation)
            s->decoded++;
        pthread_mutex_unlock(&s->lock);
        if (s->ctx->codec_type == AVMEDIA_TYPE_VIDEO)
            push_frame(s, f, gen);
        else
            push_audio(s, f, gen);
        av_frame_unref(f);
    }
}

/* A hardware context that cannot take the stream: software from here on, given again what the
 * hardware took. A byte-stream without codec data may carry its parameter sets in the first
 * packet only, and the software decoder needs them, and the pictures since the keyframe, too. */
static void to_software(struct stream *s, AVPacket *pkt, AVFrame *f, unsigned gen)
{
    elog(s->index, "media %d: hardware decoding %s, software instead (%u packets again)", s->index,
         s->hw_fallback ? "not available for this stream" : "fails", s->nreplay + 1);
    s->hw_banned = 1;
    int64_t last = s->hw_last_pts;
    software_codec(s);
    s->replay_until = last;
    for (unsigned i = 0; i < s->nreplay && s->ctx; i++)
        decode_packet(s, s->replay[i], f, gen);
    if (s->ctx)
        decode_packet(s, pkt, f, gen);
    drop_replay(s);
}

void decode_packet(struct stream *s, AVPacket *pkt, AVFrame *f, unsigned gen)
{
    int r = avcodec_send_packet(s->ctx, pkt);
    if (r == AVERROR(EAGAIN)) {
        receive(s, f, gen);
        r = avcodec_send_packet(s->ctx, pkt);
    }
    if (r < 0 && r != AVERROR(EAGAIN)) {
        s->errors++;
        s->hw_errors++;
    }
    receive(s, f, gen);
    if (s->hw_format == AV_PIX_FMT_NONE || !pkt)
        return;
    if (s->hw_fallback || (s->hw_errors >= 3 && s->decoded == 0)) {
        to_software(s, pkt, f, gen);
        return;
    }
    /* kept until the hardware gives a picture: the first REPLAY_PACKETS, which hold the
     * parameter sets and the keyframe */
    if (!s->hw_gave && s->nreplay < REPLAY_PACKETS &&
        s->replay_bytes + (size_t)pkt->size <= REPLAY_BYTES) {
        AVPacket *copy = av_packet_alloc();
        if (copy && av_packet_ref(copy, pkt) >= 0) {
            s->replay[s->nreplay++] = copy;
            s->replay_bytes += (size_t)pkt->size;
        } else
            av_packet_free(&copy);
    } else if (s->hw_gave && s->nreplay)
        drop_replay(s);
}

/* A session's codec (its frame threads and their pictures) and caps go when the session does: at
 * the first item of the next one, or at CLOSE with nothing queued. What every thread freed is then
 * given back to the system: glibc keeps it in the threads' arenas otherwise, over 100 MB after one
 * 1080p session. malloc_trim walks every arena. */
static void end_session(struct stream *s, unsigned session)
{
    int had = s->ctx || s->caps;
    close_codec(s);
    free(s->caps);
    s->caps = NULL;
    s->hw_banned = 0;
    s->dec_session = session;
    if (had)
        malloc_trim(0);
}

void *decoder_thread(void *arg)
{
    struct stream *s = arg;
    AVPacket *pkt = av_packet_alloc();
    AVFrame *f = av_frame_alloc();
    for (;;) {
        pthread_mutex_lock(&s->lock);
        while (!s->head && !s->m->quit &&
               !(!s->open && s->dec_session != s->session && (s->ctx || s->caps)))
            pthread_cond_wait(&s->cond, &s->lock);
        if (s->m->quit) {
            pthread_mutex_unlock(&s->lock);
            break;
        }
        if (!s->head) { /* closed, nothing queued */
            unsigned session = s->session;
            pthread_mutex_unlock(&s->lock);
            end_session(s, session);
            continue;
        }
        struct item *it = s->head;
        s->head = it->next;
        if (!s->head)
            s->tail = NULL;
        s->bytes -= sizeof *it + it->size + AV_INPUT_BUFFER_PADDING_SIZE;
        unsigned session = it->session, gen = it->generation;
        pthread_cond_broadcast(&s->cond);
        pthread_mutex_unlock(&s->lock);
        if (session != s->dec_session)
            end_session(s, session);
        if (gen != s->dec_generation) {
            if (s->ctx)
                avcodec_flush_buffers(s->ctx);
            drop_replay(s);
            s->hw_gave = 0;
            s->hw_last_pts = s->replay_until = NONE;
            if (s->swr)
                swr_free(&s->swr);
            s->decode_only_n = 0;
            s->next_apts = NONE;
            s->dec_generation = gen;
        }
        s->dec_ret = it->ret;
        s->dec_input = it->sequence;
        if (it->type == WEBOS_MEDIA_CAPS) {
            it->data[it->size ? it->size - 1 : 0] = 0;
            if (!s->caps || strcmp(s->caps, (char *)it->data)) {
                struct codec_params a, b;
                int same = 0;
                if (s->caps && s->ctx) {
                    caps_codec(s->caps, &a);
                    caps_codec((char *)it->data, &b);
                    /* same codec and codec data: a new size or rate alone is in the stream */
                    same = a.id == b.id && a.extradata_size == b.extradata_size &&
                           (!a.extradata_size ||
                            !memcmp(a.extradata, b.extradata, (size_t)a.extradata_size)) &&
                           a.rate == b.rate && a.channels == b.channels;
                    av_free(a.extradata);
                    av_free(b.extradata);
                }
                free(s->caps);
                s->caps = strdup((char *)it->data);
                if (!same)
                    open_codec(s, s->caps, !s->hw_banned);
            }
        } else if (it->type == WEBOS_MEDIA_BUFFER && s->ctx) {
            av_packet_unref(pkt);
            pkt->data = it->data;
            pkt->size = (int)it->size;
            pkt->pts = it->pts == NONE ? AV_NOPTS_VALUE : it->pts;
            pkt->dts = it->dts == NONE ? AV_NOPTS_VALUE : it->dts;
            pkt->duration = it->duration == NONE ? 0 : it->duration;
            if (it->flags & WEBOS_MEDIA_KEYFRAME)
                pkt->flags |= AV_PKT_FLAG_KEY;
            if ((it->flags & WEBOS_MEDIA_DECODE_ONLY) && it->pts != NONE)
                s->decode_only[s->decode_only_n++ % DECODE_ONLY_RING] = it->pts;
            if (s->decode_only_n > DECODE_ONLY_RING)
                s->decode_only_n = DECODE_ONLY_RING;
            int64_t t0 = mono_ns(), w0 = s->wait_ns;
            if (s->parser) {
                /* NAL-aligned byte-stream: the parser assembles access units, with the
                 * timestamps of the buffer each one starts in */
                const uint8_t *data = it->data;
                int left = (int)it->size;
                int64_t pts = pkt->pts, dts = pkt->dts;
                while (left > 0 && s->ctx) {
                    uint8_t *out = NULL;
                    int out_size = 0;
                    int used = av_parser_parse2(s->parser, s->ctx, &out, &out_size, data, left, pts,
                                                dts, 0);
                    if (used < 0 || (!used && !out_size))
                        break;
                    data += used;
                    left -= used;
                    pts = dts = AV_NOPTS_VALUE;
                    if (!out_size)
                        continue;
                    AVPacket *frame = av_packet_alloc();
                    frame->data = out;
                    frame->size = out_size;
                    frame->pts = s->parser->pts;
                    frame->dts = s->parser->dts;
                    if (s->parser->key_frame == 1)
                        frame->flags |= AV_PKT_FLAG_KEY;
                    decode_packet(s, frame, f, gen);
                    frame->data = NULL;
                    av_packet_free(&frame);
                }
            } else
                decode_packet(s, pkt, f, gen);
            s->decode_ns += mono_ns() - t0 - (s->wait_ns - w0);
            pkt->data = NULL;
            pkt->size = 0;
        } else if (it->type == WEBOS_MEDIA_EOS && s->ctx) {
            avcodec_send_packet(s->ctx, NULL);
            receive(s, f, gen);
            avcodec_flush_buffers(s->ctx);
            pthread_mutex_lock(&s->lock);
            if (gen == s->generation) {
                s->eos = 1;
                s->dirty = 1;
            }
            pthread_mutex_unlock(&s->lock);
            elog(s->index, "media %d: end of stream (%s)", s->index, kind_name(s));
        }
        /* every input of a DECODE session is answered, with or without a picture */
        if (it->ret && (it->type == WEBOS_MEDIA_BUFFER || it->type == WEBOS_MEDIA_EOS)) {
            pthread_mutex_lock(&s->lock);
            int current = gen == s->generation; /* else flushed or closed: nobody waits */
            pthread_mutex_unlock(&s->lock);
            if (current)
                return_picture(s, NULL, it->sequence, 1);
        }
        free(it);
    }
    close_codec(s);
    free(s->caps);
    av_packet_free(&pkt);
    av_frame_free(&f);
    return NULL;
}
