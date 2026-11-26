// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The optional decoder must preserve real lifecycle and clock metadata. */
#include "../../../host/media/decode-unavailable.c"
#include <assert.h>

static unsigned answers, last_input;
static void on_reply(void *ctx, unsigned port, unsigned char *msg, size_t len)
{
    struct webos_media_header h;
    struct webos_media_picture p;
    (void)ctx;
    assert(port == 0 && len == sizeof h + sizeof p);
    memcpy(&h, msg, sizeof h);
    memcpy(&p, msg + sizeof h, sizeof p);
    assert(h.magic == WEBOS_MEDIA_MAGIC && h.type == WEBOS_MEDIA_PICTURE &&
           h.check == webos_media_check(&h) && h.size == sizeof p);
    assert(p.flags == WEBOS_MEDIA_PICTURE_LAST && !p.format);
    last_input = p.input;
    answers++;
    free(msg);
}

int main(void)
{
    struct decoder *d = decoder_create(1, "none", 0);
    assert(d);
    struct webos_media_open open = {.kind = WEBOS_MEDIA_VIDEO};
    struct webos_media_header h = {
        .type = WEBOS_MEDIA_OPEN, .size = sizeof open, .mono = 1000000000, .clock = 900000000};
    assert(!decoder_submit(d, 0, &h, &open, h.mono + 100));
    struct host_media_message *m = &d->stream[0].message;
    assert(m->offset == 100 && m->clock_delta == 100000000 && (m->flags & HM_CLOCK));
    uint64_t gen = m->generation;
    unsigned session = m->session;
    const char caps[] = "audio/x-raw,format=F32LE";
    h.type = WEBOS_MEDIA_CAPS;
    h.size = sizeof caps;
    assert(!decoder_submit(d, 0, &h, caps, h.mono + 900));
    assert(m->generation == gen + 1 && m->open.kind == WEBOS_MEDIA_AUDIO && m->session == session);
    assert(m->offset == 100); /* Extra receipt delay must not replace the minimum. */
    struct webos_media_state state = {
        .playing = 1, .base_time = 42, .pause_rt = WEBOS_MEDIA_NONE, .rate = 1};
    h.type = WEBOS_MEDIA_STATE;
    h.size = sizeof state;
    assert(!decoder_submit(d, 0, &h, &state, h.mono + 50));
    assert(m->offset == 50 && (m->flags & HM_PLAY_STATE));
    h.type = WEBOS_MEDIA_FLUSH;
    h.size = 0;
    assert(!decoder_submit(d, 0, &h, NULL, h.mono + 1000));
    assert(m->generation == gen + 2 && m->session == session);
    assert(!m->state.playing && m->state.pause_rt == WEBOS_MEDIA_NONE);
    h.type = WEBOS_MEDIA_CLOSE;
    assert(!decoder_submit(d, 0, &h, NULL, h.mono + 1000));
    assert(!(m->flags & HM_OPEN) && (m->flags & HM_DECODER_UNAVAILABLE));
    /* A DECODE session: every input is answered, with no picture, so the guest never
     * waits for its timeout. */
    decoder_set_reply(d, on_reply, NULL);
    h.type = WEBOS_MEDIA_OPEN;
    h.size = sizeof open;
    assert(!decoder_submit(d, 0, &h, &open, h.mono));
    struct webos_media_decode dec = {.format = WEBOS_MEDIA_NV12};
    h.type = WEBOS_MEDIA_DECODE;
    h.size = sizeof dec;
    assert(!decoder_submit(d, 0, &h, &dec, h.mono));
    struct {
        struct webos_media_buffer b;
        char data[4];
    } buf = {{0}, "jpg"};
    h.type = WEBOS_MEDIA_BUFFER;
    h.size = sizeof buf;
    h.sequence = 7;
    assert(!decoder_submit(d, 0, &h, &buf, h.mono));
    assert(answers == 1 && last_input == 7);
    h.type = WEBOS_MEDIA_CLOSE;
    h.size = 0;
    assert(!decoder_submit(d, 0, &h, NULL, h.mono));
    h.type = WEBOS_MEDIA_BUFFER; /* not a DECODE session any more */
    h.size = sizeof buf;
    assert(!decoder_submit(d, 0, &h, &buf, h.mono));
    assert(answers == 1);
    decoder_destroy(d);
    puts("PASS optional decoder metadata: mutable kind, session/generation, clock minimum and "
         "flush; DECODE inputs answered");
    return 0;
}
