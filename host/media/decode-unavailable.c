// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Optional-FFmpeg build: drain the unchanged guest protocol, expose actual
 * lifecycle/clock metadata and explicitly report that no decoder is present. */
#include "decode.h"
#include "clock.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct decoder {
    unsigned ports;
    decoder_reply_fn *reply;
    void *reply_ctx;
    struct {
        int fd, dirty, ready;
        int ret; /* a DECODE session: every input is answered, with no picture */
        uint32_t reply_seq;
        struct offset offset;
        struct host_media_message message;
    } stream[WEBOS_MEDIA_PORTS];
};
struct decoder *decoder_create(unsigned ports, const char *hwdec, int software_only)
{
    (void)hwdec;
    (void)software_only;
    if (ports > WEBOS_MEDIA_PORTS)
        return NULL;
    struct decoder *d = calloc(1, sizeof *d);
    if (!d)
        return NULL;
    d->ports = ports;
    for (unsigned i = 0; i < ports; i++) {
        d->stream[i].fd = -1;
        d->stream[i].message = (struct host_media_message){.magic = HOST_MEDIA_MAGIC,
                                                           .version = HOST_MEDIA_VERSION,
                                                           .type = HM_STATE,
                                                           .generation = 1,
                                                           .flags = HM_DECODER_UNAVAILABLE};
    }
    fprintf(stderr, "decode: built without FFmpeg; decoder unavailable (guest media is drained)\n");
    return d;
}
int decoder_submit(struct decoder *d, unsigned port, const struct webos_media_header *h,
                   const void *data, int64_t received)
{
    if (port >= d->ports || h->size > WEBOS_MEDIA_MAX_PAYLOAD || h->type < WEBOS_MEDIA_OPEN ||
        h->type > WEBOS_MEDIA_DECODE ||
        ((h->type == WEBOS_MEDIA_FLUSH || h->type == WEBOS_MEDIA_EOS ||
          h->type == WEBOS_MEDIA_CLOSE) &&
         h->size) ||
        (h->type == WEBOS_MEDIA_OPEN && h->size != sizeof(struct webos_media_open)) ||
        (h->type == WEBOS_MEDIA_STATE && h->size != sizeof(struct webos_media_state)) ||
        (h->type == WEBOS_MEDIA_DECODE && h->size != sizeof(struct webos_media_decode)) ||
        (h->type == WEBOS_MEDIA_CAPS && (!h->size || ((const char *)data)[h->size - 1])) ||
        (h->type == WEBOS_MEDIA_BUFFER && h->size < sizeof(struct webos_media_buffer)))
        return -1;
    struct host_media_message *m = &d->stream[port].message;
    if (h->type == WEBOS_MEDIA_OPEN || h->type == WEBOS_MEDIA_CLOSE)
        d->stream[port].ret = 0;
    else if (h->type == WEBOS_MEDIA_DECODE && (m->flags & HM_OPEN))
        d->stream[port].ret = 1;
    if (d->stream[port].ret && (h->type == WEBOS_MEDIA_BUFFER || h->type == WEBOS_MEDIA_EOS)) {
        struct webos_media_picture p = {
            .input = h->sequence, .flags = WEBOS_MEDIA_PICTURE_LAST, .pts = WEBOS_MEDIA_NONE};
        size_t len;
        unsigned char *msg = hm_picture(++d->stream[port].reply_seq, &p, 0, &len);
        if (msg && d->reply)
            d->reply(d->reply_ctx, port, msg, len);
        else
            free(msg);
    }
    if (h->type == WEBOS_MEDIA_OPEN && h->size == sizeof m->open) {
        memcpy(&m->open, data, sizeof m->open);
        m->generation++;
        m->session++;
        m->flags = HM_OPEN | HM_DECODER_UNAVAILABLE;
        memset(&m->state, 0, sizeof m->state);
        memset(&d->stream[port].offset, 0, sizeof d->stream[port].offset);
    } else if (!(m->flags & HM_OPEN))
        return 0;
    else if (h->type == WEBOS_MEDIA_STATE && h->size == sizeof m->state) {
        memcpy(&m->state, data, sizeof m->state);
        m->flags |= HM_PLAY_STATE;
    } else if (h->type == WEBOS_MEDIA_CLOSE) {
        m->flags = HM_DECODER_UNAVAILABLE;
        m->generation++;
        m->session++;
    } else if (h->type == WEBOS_MEDIA_FLUSH) {
        m->generation++;
        m->state.playing = 0;
        m->state.pause_rt = WEBOS_MEDIA_NONE;
        m->flags &= ~HM_EOS;
    } else if (h->type == WEBOS_MEDIA_EOS)
        m->flags |= HM_EOS;
    else if (h->type == WEBOS_MEDIA_CAPS && h->size && !((const char *)data)[h->size - 1]) {
        uint32_t kind = !strncmp(data, "audio/", 6) ? WEBOS_MEDIA_AUDIO
                        : !strncmp(data, "video/", 6) || !strncmp(data, "image/", 6)
                            ? WEBOS_MEDIA_VIDEO
                            : m->open.kind;
        if (kind != m->open.kind) {
            m->open.kind = kind;
            m->generation++;
        }
    }
    if (h->mono != WEBOS_MEDIA_NONE && h->mono > 0)
        offset_add(&d->stream[port].offset, received - h->mono, received);
    m->offset = offset_get(&d->stream[port].offset);
    if (h->mono != WEBOS_MEDIA_NONE && h->clock != WEBOS_MEDIA_NONE) {
        m->clock_delta = h->mono - h->clock;
        m->flags |= HM_CLOCK;
    }
    d->stream[port].dirty = 1;
    return 0;
}
void decoder_set_pool(struct decoder *d, struct pool *pool, int (*ring_guest)(void *), void *ctx)
{
    (void)d;
    (void)pool;
    (void)ring_guest;
    (void)ctx;
}
void decoder_doorbell(struct decoder *d)
{
    (void)d;
}
void decoder_set_reply(struct decoder *d, decoder_reply_fn *fn, void *ctx)
{
    d->reply = fn;
    d->reply_ctx = ctx;
}
void decoder_disconnect(struct decoder *d, unsigned port)
{
    struct webos_media_header h = {
        .type = WEBOS_MEDIA_CLOSE, .mono = WEBOS_MEDIA_NONE, .clock = WEBOS_MEDIA_NONE};
    decoder_submit(d, port, &h, NULL, hm_mono_ns());
}
void decoder_attach(struct decoder *d, unsigned port, int fd)
{
    d->stream[port].fd = fd;
    d->stream[port].ready = 0;
    d->stream[port].dirty = 1;
}
void decoder_detach(struct decoder *d, unsigned port)
{
    d->stream[port].fd = -1;
}
int decoder_tick(struct decoder *d, unsigned port)
{
    int fd = d->stream[port].fd;
    if (fd < 0)
        return 0;
    struct host_media_message m;
    int fds[HOST_MEDIA_OBJECTS];
    unsigned nfds;
    int r = hm_receive(fd, &m, fds, &nfds);
    hm_close_fds(fds, nfds);
    if (r < 0 || nfds)
        return -1;
    if (r == 1) {
        if (m.type != HM_HELLO)
            return -1;
        m = d->stream[port].message;
        m.type = HM_ACK;
        m.status = HM_PREFERENCE_INVALID;
        if (hm_send(fd, &m, NULL, 0) != 1)
            return -1;
        d->stream[port].ready = 1;
    }
    if (d->stream[port].ready && d->stream[port].dirty) {
        r = hm_send(fd, &d->stream[port].message, NULL, 0);
        if (r < 0)
            return -1;
        if (r == 1)
            d->stream[port].dirty = 0;
    }
    return 0;
}
void decoder_destroy(struct decoder *d)
{
    free(d);
}
