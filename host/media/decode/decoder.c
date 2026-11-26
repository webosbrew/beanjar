// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The public decoder: lifecycle, submit/disconnect, viewer attach/detach,
 * preference handshake, retirement and export tick. */
#include "decode-priv.h"

#include <libavcodec/avcodec.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>

int64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}
/* The running time due at host time now; NONE when not known (no state yet, paused before the
 * first play). With s->lock held. */
static int64_t due_rt(struct stream *s, int64_t now)
{
    if (!s->have_state)
        return NONE;
    if (!s->state.playing)
        return s->state.pause_rt;
    int64_t off = offset_get(&s->offset);
    if (off == NONE || !s->have_delta)
        return NONE;
    return now - off - s->clock_delta - s->state.base_time;
}
/* The host monotonic time at which running time rt is due; NONE when not known (no state
 * yet, paused). The inverse of due_rt. With s->lock held. */
int64_t due_mono(struct stream *s, int64_t rt)
{
    if (!s->have_state || !s->state.playing || !s->have_delta)
        return NONE;
    int64_t off = offset_get(&s->offset);
    if (off == NONE)
        return NONE;
    return rt + s->state.base_time + s->clock_delta + off;
}
struct decoder *decoder_create(unsigned ports, const char *hwdec, int software_only)
{
    if (ports > WEBOS_MEDIA_PORTS)
        return NULL;
    struct decoder *m = calloc(1, sizeof *m);
    if (!m)
        return NULL;
    m->ports = ports;
    snprintf(m->hwdec, sizeof m->hwdec, "%s", hwdec ? hwdec : "auto");
    if (strcmp(m->hwdec, "auto") && strcmp(m->hwdec, "none") &&
        av_hwdevice_find_type_by_name(m->hwdec) == AV_HWDEVICE_TYPE_NONE) {
        elog(-1, "decode: invalid hwdec %s", m->hwdec);
        free(m);
        return NULL;
    }
    if (software_only && strcmp(m->hwdec, "none")) {
        elog(-1, "decode: software only with hwdec %s", m->hwdec);
        free(m);
        return NULL;
    }
    m->software_only = !!software_only;
    pthread_mutex_init(&m->hw_lock, NULL);
    for (unsigned i = 0; i < ports; i++) {
        struct stream *s = &m->streams[i];
        s->m = m;
        s->index = i;
        s->viewer = -1;
        s->generation = 1;
        s->ctrl_e = s->pic_e = s->old_e = -1;
        snprintf(s->preference, sizeof s->preference, "%s", m->hwdec);
        /* monotonic: the decoder thread waits for queue space until a picture is due */
        pthread_condattr_t ca;
        pthread_condattr_init(&ca);
        pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
        pthread_mutex_init(&s->lock, NULL);
        pthread_cond_init(&s->cond, &ca);
        pthread_condattr_destroy(&ca);
        if (!pthread_create(&s->decoder, NULL, decoder_thread, s))
            s->threads = 1;
        else {
            decoder_destroy(m);
            return NULL;
        }
    }
    return m;
}
int decoder_submit(struct decoder *m, unsigned port, const struct webos_media_header *h,
                   const void *data, int64_t received)
{
    if (port >= m->ports || h->size > WEBOS_MEDIA_MAX_PAYLOAD || h->type < WEBOS_MEDIA_OPEN ||
        h->type > WEBOS_MEDIA_DECODE ||
        ((h->type == WEBOS_MEDIA_FLUSH || h->type == WEBOS_MEDIA_EOS ||
          h->type == WEBOS_MEDIA_CLOSE) &&
         h->size))
        return -1;
    if ((h->type == WEBOS_MEDIA_OPEN && h->size != sizeof(struct webos_media_open)) ||
        (h->type == WEBOS_MEDIA_STATE && h->size != sizeof(struct webos_media_state)) ||
        (h->type == WEBOS_MEDIA_DECODE && h->size != sizeof(struct webos_media_decode)) ||
        (h->type == WEBOS_MEDIA_CAPS && (!h->size || ((const char *)data)[h->size - 1])) ||
        (h->type == WEBOS_MEDIA_BUFFER && h->size < sizeof(struct webos_media_buffer)))
        return -1;
    return handle(&m->streams[port], h, data, received);
}
void decoder_set_reply(struct decoder *m, decoder_reply_fn *fn, void *ctx)
{
    m->reply = fn;
    m->reply_ctx = ctx;
}
void decoder_set_pool(struct decoder *m, struct pool *pool, int (*ring_guest)(void *), void *ctx)
{
    m->pool = pool;
    m->ring_guest = ring_guest;
    m->ring_ctx = ctx;
}
void decoder_doorbell(struct decoder *m)
{
    for (unsigned i = 0; i < m->ports; i++) {
        struct stream *s = &m->streams[i];
        pthread_mutex_lock(&s->lock);
        if (s->shared) {
            /* the claiming file closed (the decoder's process gone): its pool goes back */
            if (pool_claim(m->pool, (unsigned)s->index) != s->claim) {
                elog(s->index, "media %d: the guest's claim ended: pool session over", s->index);
                end_pool_session(s);
            } else
                take_releases(s);
        }
        pthread_cond_broadcast(&s->cond);
        pthread_mutex_unlock(&s->lock);
    }
    if (m->pool)
        pool_reap(m->pool);
}
void decoder_disconnect(struct decoder *m, unsigned port)
{
    struct webos_media_header h = {.type = WEBOS_MEDIA_CLOSE, .mono = NONE, .clock = NONE};
    handle(&m->streams[port], &h, NULL, mono_ns());
}
void decoder_attach(struct decoder *m, unsigned port, int fd)
{
    struct stream *s = &m->streams[port];
    pthread_mutex_lock(&s->lock);
    s->viewer = fd;
    s->epoch++;
    s->dirty = 1;
    s->ready = 0;
    pthread_mutex_unlock(&s->lock);
}
void decoder_detach(struct decoder *m, unsigned port)
{
    struct stream *s = &m->streams[port];
    pthread_mutex_lock(&s->lock);
    s->viewer = -1;
    s->ready = 0;
    for (unsigned i = 0; i < HOST_MEDIA_LEASES; i++) {
        if (s->leases[i].frame) {
            /* Direct export is disabled before another attachment can lease
             * hardware. Thus only this first crash can populate quarantine. */
            if (s->norphans < HOST_MEDIA_LEASES)
                s->orphan[s->norphans++] = s->leases[i].frame;
            s->force_download = 1;
            s->hw_import_failed = 1;
        }
        memset(&s->leases[i], 0, sizeof s->leases[i]);
    }
    if (s->force_download)
        elog(s->index,
             "decode %d: %u crash leases quarantined until helper exit; immutable software "
             "export thereafter",
             s->index, s->norphans);
    pthread_mutex_unlock(&s->lock);
}
int decoder_tick(struct decoder *m, unsigned port)
{
    struct stream *s = &m->streams[port];
    int result = 0;
    pthread_mutex_lock(&s->lock);
    if (s->reset_current) {
        av_frame_free(&s->current);
        s->current_epoch = 0;
        s->reset_current = 0;
    }
    if (s->viewer >= 0) {
        for (;;) {
            struct host_media_message msg;
            int fds[HOST_MEDIA_OBJECTS];
            unsigned nfds;
            int r = hm_receive(s->viewer, &msg, fds, &nfds);
            hm_close_fds(fds, nfds);
            if (r <= 0) {
                if (r < 0)
                    result = -1;
                break;
            }
            if (nfds) {
                result = -1;
                break;
            }
            if (msg.type == HM_HELLO) {
                msg.preference[sizeof msg.preference - 1] = 0;
                int valid = !*msg.preference || !strcmp(msg.preference, "auto") ||
                            !strcmp(msg.preference, "none") ||
                            av_hwdevice_find_type_by_name(msg.preference) != AV_HWDEVICE_TYPE_NONE;
                /* software only: no preference but none, so that no device is ever opened
                 * (nor its user-space driver loaded) in this process */
                int locked =
                    s->m->software_only && *msg.preference && strcmp(msg.preference, "none");
                struct host_media_message ack = snapshot(s, HM_ACK);
                ack.status = locked    ? HM_PREFERENCE_LOCKED
                             : !valid  ? HM_PREFERENCE_INVALID
                             : s->open ? HM_PREFERENCE_DEFERRED
                                       : HM_PREFERENCE_APPLIED;
                if (locked)
                    elog(s->index, "media %d: viewer's hwdec %.31s refused: software only",
                         s->index, msg.preference);
                else if (valid && *msg.preference)
                    snprintf(s->preference, sizeof s->preference, "%s", msg.preference);
                snprintf(ack.preference, sizeof ack.preference, "%s", s->preference);
                if (hm_send(s->viewer, &ack, NULL, 0) != 1) {
                    result = -1;
                    break;
                }
                s->ready = 1;
                s->dirty = 1;
            } else if (msg.type == HM_RELEASE || msg.type == HM_IMPORT_FAILED) {
                for (unsigned i = 0; i < HOST_MEDIA_LEASES; i++)
                    if (s->leases[i].token == msg.token &&
                        s->leases[i].generation == msg.generation) {
                        if (msg.type == HM_IMPORT_FAILED) {
                            s->hw_import_failed = 1;
                            s->current_epoch = 0;
                            memset(s->sent_epoch, 0, sizeof s->sent_epoch);
                            elog(s->index,
                                 "decode %d: viewer import failed; downloading future frames",
                                 s->index);
                        }
                        av_frame_free(&s->leases[i].frame);
                        memset(&s->leases[i], 0, sizeof s->leases[i]);
                        break;
                    }
            } else {
                result = -1;
                break;
            }
        }
    }
    int64_t now = mono_ns(), rt = due_rt(s, now);
    unsigned queued = s->nframes;
    /* A helper owns playback retirement even without a viewer, so a stalled or absent
     * viewer cannot hold a decoder thread waiting for queue space. */
    while (s->nframes && (s->need_first || s->frames[0]->pts == NONE ||
                          (rt != NONE && s->frames[0]->pts <= rt))) {
        av_frame_free(&s->current);
        s->current = s->frames[0];
        s->current_epoch = s->sent_epoch[0];
        s->need_first = 0;
        s->nframes--;
        memmove(s->frames, s->frames + 1, s->nframes * sizeof *s->frames);
        memmove(s->sent_epoch, s->sent_epoch + 1, s->nframes * sizeof *s->sent_epoch);
    }
    if (s->nframes < queued)
        pthread_cond_broadcast(&s->cond);
    if (!result && s->viewer >= 0 && s->ready) {
        if (s->dirty) {
            struct host_media_message msg = snapshot(s, HM_STATE);
            int r = hm_send(s->viewer, &msg, NULL, 0);
            if (r == 1)
                s->dirty = 0;
            if (r < 0)
                result = -1;
        }
        if (!s->dirty && !result && s->current && s->current_epoch != s->epoch) {
            int r = export_frame(s, s->current);
            if (r == 1)
                s->current_epoch = s->epoch;
            if (r < 0)
                result = -1;
        }
        for (unsigned i = 0; !s->dirty && !result && i < s->nframes; i++) {
            if (s->sent_epoch[i] == s->epoch)
                continue;
            int r = export_frame(s, s->frames[i]);
            if (r == 1)
                s->sent_epoch[i] = s->epoch;
            if (r < 0)
                result = -1;
            if (r <= 0)
                break;
        }
    }
    while (s->ahead) {
        struct chunk *c = s->ahead;
        int expired = rt != NONE && c->rt + (int64_t)c->frames * 1000000000 / OUT_RATE <= rt;
        int r =
            !result && s->viewer >= 0 && s->ready && !s->dirty && !expired ? export_audio(s, c) : 0;
        if (r < 0)
            result = -1;
        if (!expired && r != 1)
            break;
        s->ahead = c->next;
        if (!s->ahead)
            s->atail = NULL;
        s->aqueued -= (int64_t)c->frames * 1000000000 / OUT_RATE;
        free(c);
    }
    if (s->packets && now - s->last_report > REPORT_NS) {
        report(s, "");
        s->last_report = now;
    }
    pthread_mutex_unlock(&s->lock);
    return result;
}
void decoder_destroy(struct decoder *m)
{
    if (!m)
        return;
    m->quit = 1;
    for (unsigned i = 0; i < m->ports; i++) {
        struct stream *s = &m->streams[i];
        if (!s->threads)
            continue;
        pthread_mutex_lock(&s->lock);
        pthread_cond_broadcast(&s->cond);
        pthread_mutex_unlock(&s->lock);
        pthread_join(s->decoder, NULL);
        report(s, " (exit)");
        free_items(s, 0);
        free_frames(s);
        for (unsigned j = 0; j < HOST_MEDIA_LEASES; j++) {
            av_frame_free(&s->leases[j].frame);
            av_frame_free(&s->orphan[j]);
        }
        pthread_cond_destroy(&s->cond);
        pthread_mutex_destroy(&s->lock);
    }
    for (unsigned i = 0; i < HW_TYPES; i++)
        av_buffer_unref(&m->hw_device[i]);
    pthread_mutex_destroy(&m->hw_lock);
    free(m);
}
