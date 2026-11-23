// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Compressed ingress and session state: queue items, OPEN/CAPS/STATE/FLUSH/CLOSE,
 * kind changes and reports. Decoding itself is frame.c's. */
#include "decode-priv.h"
#include "../ipc.h"

#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

void free_items(struct stream *s, int keep_caps)
{
    struct item **p = &s->head, *it;
    s->tail = NULL;
    while ((it = *p)) {
        if (keep_caps && it->type == WEBOS_MEDIA_CAPS) {
            s->tail = it;
            p = &it->next;
            continue;
        }
        *p = it->next;
        s->bytes -= sizeof *it + it->size + AV_INPUT_BUFFER_PADDING_SIZE;
        free(it);
    }
}

const char *kind_name(const struct stream *s)
{
    return s->info.kind == WEBOS_MEDIA_VIDEO   ? "video"
           : s->info.kind == WEBOS_MEDIA_AUDIO ? "audio"
                                               : "?";
}

void report(struct stream *s, const char *why)
{
    /* session: the OPEN these counters belong to, as the viewer's present lines name it */
    char more[160];
    int n = 0;
    more[0] = 0;
    if (s->returned || s->return_errors)
        n += snprintf(more + n, sizeof more - (size_t)n,
                      " returned=%llu return-errors=%llu return=%.2f ms/picture%s", s->returned,
                      s->return_errors, s->returned ? s->return_ns / 1e6 / s->returned : 0,
                      s->shared ? " shared" : "");
    if (s->shared && s->returned && n < (int)sizeof more)
        n += snprintf(more + n, sizeof more - (size_t)n, " slot-wait=%.2f ms/picture",
                      s->slot_wait_ns / 1e6 / s->returned);
    if (s->slot_drops && n < (int)sizeof more)
        snprintf(more + n, sizeof more - (size_t)n, " slot-drops=%llu", s->slot_drops);
    elog(s->index,
         "decode %d %s session=%u%s: %s %s packets=%llu decoded=%llu dropped=%llu errors=%llu "
         "decode=%.2f ms/packet%s",
         s->index, kind_name(s), s->report_session, why, s->codec_name, s->hw_active ? "hw" : "sw",
         s->packets, s->decoded, s->late_drops, s->errors,
         s->packets ? s->decode_ns / 1e6 / s->packets : 0, more);
}

/* A pool session's releases off its ring (with s->lock): each frees the slot it names if that is a
 * busy slot of the session's layout answering that input. A release of a slot of an earlier
 * layout (an extent of the port's, retiring) is old news; anything else is the guest's mistake,
 * counted and logged once per session. */
/* r's slot of the layout at off (n slots of size bytes) given back: whether it was one */
static int give_back(struct slot *slots, uint32_t off, uint32_t size, unsigned n,
                     const struct webos_media_release *r)
{
    if (!size || r->slot < off || (r->slot - off) % size)
        return 0;
    uint32_t i = (r->slot - off) / size;
    if (i >= n || !slots[i].busy || slots[i].input != r->input)
        return 0;
    slots[i].busy = 0;
    return 1;
}

/* The layout before the current one back to the host (with s->lock): retiring, freed once the
 * guest maps nothing of it. */
void retire_old_layout(struct stream *s)
{
    if (s->old_e < 0)
        return;
    pool_retire(s->m->pool, (unsigned)s->index, s->old_e);
    pool_reap(s->m->pool);
    s->old_e = -1;
    s->old_off = s->old_slot_size = s->old_bytes = 0;
    s->old_nslots = 0;
    memset(s->old_slots, 0, sizeof s->old_slots);
}

void take_releases(struct stream *s)
{
    if (!s->shared || !s->rings.c)
        return;
    unsigned budget = WEBOS_MEDIA_RING_ENTRIES;
    struct webos_media_release r;
    while (rings_take_release(&s->rings, &r, &budget)) {
        if (give_back(s->slots, s->pic_off, s->slot_size, s->nslots, &r))
            continue;
        if (s->old_e >= 0 &&
            give_back(s->old_slots, s->old_off, s->old_slot_size, s->old_nslots, &r)) {
            unsigned busy = 0;
            for (unsigned i = 0; i < s->old_nslots; i++)
                busy += s->old_slots[i].busy != 0;
            if (!busy)
                retire_old_layout(s);
            continue;
        }
        int old = 0;
        for (int e = 0; e < WEBOS_MEDIA_POOL_EXTENTS && !old; e++)
            old = e != s->pic_e && e != s->ctrl_e &&
                  pool_in_extent(s->m->pool, (unsigned)s->index, e, r.slot, 1);
        if (!old && !s->bad_releases++)
            elog(s->index,
                 "media %d: a release of %#x (input %u) names no slot of the session: "
                 "dropped",
                 s->index, r.slot, r.input);
    }
}

/* The session's extents back to the host (with s->lock): retiring, freed once the guest maps
 * nothing of them. The session's answers are inline from then on. */
void end_pool_session(struct stream *s)
{
    if (s->m->pool && (s->ctrl_e >= 0 || s->pic_e >= 0)) {
        pool_retire(s->m->pool, (unsigned)s->index, -1);
        pool_reap(s->m->pool);
    }
    s->shared = 0;
    s->rings.c = NULL;
    s->ctrl_e = s->pic_e = s->old_e = -1;
    s->pic_off = 0;
    memset(s->slots, 0, sizeof s->slots);
    s->slot_size = 0;
    s->nslots = 0;
    s->old_off = s->old_slot_size = s->old_bytes = 0;
    s->old_nslots = 0;
    memset(s->old_slots, 0, sizeof s->old_slots);
}

/* DECODE asked for shared pictures: the session's control extent, answered with POOL (size 0:
 * none, pictures inline). With s->lock. */
static void begin_pool_session(struct stream *s)
{
    struct pool *pool = s->m->pool;
    struct webos_media_pool answer = {0, 0};
    uint32_t claim = pool ? pool_claim(pool, (unsigned)s->index) : 0;
    if (!pool)
        ;
    else if (!(claim & 1))
        elog(s->index, "media %d: the guest has not claimed the port: pictures inline", s->index);
    else {
        uint32_t off, size;
        int e =
            pool_alloc(pool, (unsigned)s->index, sizeof(struct webos_media_control), &off, &size);
        if (e < 0)
            elog(s->index, "media %d: pool full: pictures inline", s->index);
        else {
            s->ctrl_e = e;
            s->claim = claim;
            s->shared = 1;
            s->inline_only = 0;
            s->bad_releases = 0;
            rings_init(&s->rings, (struct webos_media_control *)(pool->base + off));
            answer = (struct webos_media_pool){off, size};
        }
    }
    size_t len;
    unsigned char *m = hm_guest_message(WEBOS_MEDIA_POOL, 0, &answer, sizeof answer, &len);
    if (m && s->m->reply)
        s->m->reply(s->m->reply_ctx, (unsigned)s->index, m, len);
    else
        free(m);
}

static int enqueue(struct stream *s, const struct webos_media_header *h, const uint8_t *payload)
{
    size_t extra =
        h->type == WEBOS_MEDIA_BUFFER ? h->size - sizeof(struct webos_media_buffer) : h->size;
    struct item *it = calloc(1, sizeof *it + extra + AV_INPUT_BUFFER_PADDING_SIZE);
    if (!it)
        return -1;
    it->type = h->type;
    it->sequence = h->sequence;
    it->ret = s->ret;
    it->generation = s->generation;
    it->session = s->session;
    it->pts = it->dts = it->duration = it->stream_time = NONE;
    if (h->type == WEBOS_MEDIA_BUFFER) {
        struct webos_media_buffer b;
        memcpy(&b, payload, sizeof b);
        it->pts = b.pts;
        it->dts = b.dts;
        it->duration = b.duration;
        it->stream_time = b.stream_time;
        it->flags = b.flags;
        payload += sizeof b;
    }
    memcpy(it->data, payload, extra);
    it->size = extra;
    /* A viewer never provides decoder backpressure. True compressed ingress
     * overload is an explicit session discontinuity handled by the caller. */
    size_t charge = sizeof *it + extra + AV_INPUT_BUFFER_PADDING_SIZE;
    if (charge > PACKET_BYTES_MAX || s->bytes > PACKET_BYTES_MAX - charge) {
        free(it);
        return -1;
    }
    if (s->tail)
        s->tail->next = it;
    else
        s->head = it;
    s->tail = it;
    s->bytes += charge;
    if (h->type == WEBOS_MEDIA_BUFFER) {
        s->packets++;
        s->bytes_in += it->size;
    }
    pthread_cond_broadcast(&s->cond);
    return 0;
}

int handle(struct stream *s, const struct webos_media_header *h, const uint8_t *payload,
           int64_t now)
{
    int result = 0;
    pthread_mutex_lock(&s->lock);
    switch (h->type) {
    case WEBOS_MEDIA_OPEN:
        if (h->size < sizeof s->info)
            break;
        if (s->open)
            report(s, " (replaced)");
        free_items(s, 0);
        free_frames(s);
        s->session++;
        s->generation++;
        s->report_session = s->session;
        memcpy(&s->info, payload, sizeof s->info);
        s->info.element[sizeof s->info.element - 1] = 0;
        s->open = 1;
        s->ret = 0;
        end_pool_session(s);
        s->inline_only = 0;
        s->slot_drops = 0;
        s->slot_wait_ns = 0;
        s->eos = 0;
        s->have_state = 0;
        s->have_delta = 0;
        s->need_first = 1;
        s->reset_current = 1;
        memset(&s->offset, 0, sizeof s->offset);
        strcpy(s->codec_name, "?");
        s->hw_active = s->hw_import_failed = 0;
        s->packets = s->bytes_in = s->decoded = s->shown = s->picked = s->late_drops = s->errors =
            0;
        s->resyncs = s->underruns = s->nudges = 0;
        s->audio_frames = 0;
        s->returned = s->return_errors = 0;
        s->return_ns = 0;
        s->pts_first = s->pts_last = NONE;
        s->last_report = now;
        elog(s->index, "media %d: open %s %s (pid %d, channel %d, pad %u%s)", s->index,
             kind_name(s), s->info.element, s->info.pid, s->info.channel, s->info.pad,
             s->info.output == WEBOS_MEDIA_OUTPUT(2, 0) ? ", to HDMI 0" : "");
        break;
    case WEBOS_MEDIA_CAPS:
    case WEBOS_MEDIA_BUFFER:
    case WEBOS_MEDIA_EOS:
        if (!s->open)
            break;
        if (h->type == WEBOS_MEDIA_BUFFER && h->size < sizeof(struct webos_media_buffer))
            break;
        if (h->type == WEBOS_MEDIA_CAPS) {
            /* The caps decide the kind: lpsink links a file's audio to a vdecsink pad too (LG's
             * caps for it list audio/mpeg, ...), and a sink pad can change media type. */
            uint32_t kind = !strncmp((const char *)payload, "audio/", 6) ? WEBOS_MEDIA_AUDIO
                            : !strncmp((const char *)payload, "video/", 6) ||
                                    !strncmp((const char *)payload, "image/", 6)
                                ? WEBOS_MEDIA_VIDEO
                                : s->info.kind;
            elog(s->index, "media %d: caps (%s) %.*s", s->index,
                 kind == WEBOS_MEDIA_AUDIO ? "audio" : "video",
                 (int)(h->size > 300 ? 300 : h->size), payload);
            if (kind != s->info.kind) {
                s->info.kind = kind;
                s->reset_current = 1;
                free_frames(s);
                av_frame_free(&s->current);
                s->generation++;
            }
        }
        result = enqueue(s, h, payload);
        break;
    case WEBOS_MEDIA_STATE:
        if (h->size < sizeof s->state || !s->open)
            break;
        memcpy(&s->state, payload, sizeof s->state);
        s->have_state = 1;
        elog(s->index, "media %d: %s %s base_time=%.3f s pause_rt=%.3f s rate=%.2f", s->index,
             kind_name(s), s->state.playing ? "playing" : "paused", s->state.base_time / 1e9,
             s->state.pause_rt == NONE ? -1.0 : s->state.pause_rt / 1e9, s->state.rate);
        break;
    case WEBOS_MEDIA_FLUSH:
        if (!s->open)
            break;
        free_items(s, 1);
        free_frames(s);
        s->generation++;
        for (struct item *it = s->head; it; it = it->next)
            it->generation = s->generation;
        s->need_first = 1;
        s->eos = 0;
        /* GstBaseSink loses its state on flush-start (async): it is PAUSED until the pipeline
         * plays it again with a new base time, which comes as STATE. Until then nothing is due. */
        s->state.playing = 0;
        s->state.pause_rt = NONE;
        elog(s->index, "media %d: flush %s", s->index, kind_name(s));
        break;
    case WEBOS_MEDIA_DECODE: {
        struct webos_media_decode d;
        if (!s->open || h->size < sizeof d)
            break;
        memcpy(&d, payload, sizeof d);
        int audio = d.format == WEBOS_MEDIA_S16LE;
        if (d.format != WEBOS_MEDIA_NV12 && d.format != WEBOS_MEDIA_YUY2 && !audio) {
            elog(s->index, "media %d: decode to format %#x refused (NV12, YUY2, 16LE)", s->index,
                 d.format);
            break;
        }
        s->ret = (int)d.format;
        end_pool_session(s);
        /* an audio session's samples come inline (include/webos-media.h) */
        if ((d.flags & WEBOS_MEDIA_DECODE_SHARED) && !audio)
            begin_pool_session(s);
        elog(s->index, "media %d: %s's pictures go back to the guest (%.4s, %s)", s->index,
             s->info.element, (const char *)&d.format, s->shared ? "shared" : "inline");
        break;
    }
    case WEBOS_MEDIA_CLOSE:
        if (!s->open)
            break;
        report(s, " (closed)");
        s->ret = 0;
        end_pool_session(s);
        free_items(s, 0);
        free_frames(s);
        s->session++;
        s->generation++;
        s->open = 0;
        s->reset_current = 1;
        elog(s->index, "media %d: close %s", s->index, kind_name(s));
        break;
    }
    /* offsets after OPEN has reset them */
    if (h->mono != NONE && h->mono > 0)
        offset_add(&s->offset, now - h->mono, now);
    if (h->clock != NONE && h->mono != NONE) {
        s->clock_delta = h->mono - h->clock;
        s->have_delta = 1;
    }
    pthread_cond_broadcast(&s->cond);
    /* BUFFER receipts refine the clock minimum too. Coalesced snapshots carry
     * the helper receipt mapping, never the viewer socket latency. */
    s->dirty = 1;
    pthread_mutex_unlock(&s->lock);
    return result;
}
