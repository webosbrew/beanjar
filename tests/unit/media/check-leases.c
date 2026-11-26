// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Internal ownership test using ordinary reference-counted AVFrame buffers.
 * This tests generation fences and quarantine state; it is NOT a DMA-BUF,
 * hardware-decoder, EGL or GPU-completion qualification. */
#include "../../../host/media/decode/decode-priv.h"
#include <assert.h>
#include <sys/socket.h>
#include <unistd.h>

static unsigned freed;
static void counted_free(void *opaque, uint8_t *data)
{
    (void)opaque;
    freed++;
    av_free(data);
}
static AVFrame *reference(void)
{
    AVFrame *f = av_frame_alloc();
    assert(f);
    f->buf[0] = av_buffer_create(av_malloc(32), 32, counted_free, NULL, 0);
    assert(f->buf[0]);
    return f;
}
int main(void)
{
    struct decoder *d = decoder_create(1, "none", 0);
    assert(d);
    int pair[2];
    assert(!socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair));
    decoder_attach(d, 0, pair[0]);
    struct stream *s = &d->streams[0];
    s->leases[0].frame = reference();
    s->leases[0].token = 1;
    s->leases[0].generation = 9;
    struct host_media_message msg = {.magic = HOST_MEDIA_MAGIC,
                                     .version = HOST_MEDIA_VERSION,
                                     .type = HM_RELEASE,
                                     .token = 1,
                                     .generation = 8};
    assert(hm_send(pair[1], &msg, NULL, 0) == 1);
    assert(!decoder_tick(d, 0));
    assert(!freed && s->leases[0].frame); /* Stale generation cannot release a live lease. */
    msg.generation = 9;
    assert(hm_send(pair[1], &msg, NULL, 0) == 1);
    shutdown(pair[1], SHUT_WR); /* Release followed immediately by EOF. */
    assert(decoder_tick(d, 0) == -1);
    decoder_detach(d, 0);
    assert(freed == 1 && !s->norphans && !s->force_download);
    close(pair[0]);
    close(pair[1]);
    for (unsigned i = 0; i < HOST_MEDIA_LEASES; i++) {
        s->leases[i].frame = reference();
        s->leases[i].token = i + 10;
    }
    decoder_detach(d, 0);
    assert(freed == 1 && s->norphans == HOST_MEDIA_LEASES && s->force_download);
    struct webos_media_open open = {.kind = WEBOS_MEDIA_VIDEO};
    struct webos_media_header h = {
        .type = WEBOS_MEDIA_OPEN, .size = sizeof open, .mono = NONE, .clock = NONE};
    assert(!decoder_submit(d, 0, &h, &open, mono_ns()));
    const char caps[] = "video/x-h264,stream-format=(string)byte-stream";
    h.type = WEBOS_MEDIA_CAPS;
    h.size = sizeof caps;
    assert(!decoder_submit(d, 0, &h, caps, mono_ns()));
    h.type = WEBOS_MEDIA_FLUSH;
    h.size = 0;
    assert(!decoder_submit(d, 0, &h, NULL, mono_ns()));
    assert(s->force_download && s->norphans == HOST_MEDIA_LEASES && freed == 1);
    /* Repeated software-only disconnects do not grow the quarantine. */
    for (unsigned i = 0; i < 20; i++)
        decoder_detach(d, 0);
    assert(s->norphans == HOST_MEDIA_LEASES && freed == 1);
    decoder_destroy(d);
    assert(freed == HOST_MEDIA_LEASES + 1);
    puts("PASS clean release, stale-generation rejection and bounded persistent crash quarantine "
         "(ownership fixture only)");
    return 0;
}
