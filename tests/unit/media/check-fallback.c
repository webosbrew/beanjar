// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* A hardware decoder that cannot take a stream, replaced by software (host/media/decode/frame.c,
 * decode_packet): every picture of a byte-stream whose parameter sets come in its first packet
 * only must still be decoded. The "hardware" here is a software context marked as hardware
 * (hw_format) that gives no pictures (skip_frame), so the check runs on any host: it takes the
 * parameter sets in, as the failing hardware context does. Prints TAP; exit status 0 only if
 * every check passed. */
#include "../../../host/media/decode/decode-priv.h"
#include "fallback-streams.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PICTURES 16

static int n, failed;
static unsigned kept_at_end; /* packets kept for a fallback when the stream ended */
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

/* the stream cut into access units, as an alignment=au source sends them */
static unsigned split(enum AVCodecID id, const unsigned char *data, size_t size, AVPacket **out,
                      unsigned max)
{
    AVCodecParserContext *parser = av_parser_init(id);
    AVCodecContext *ctx = avcodec_alloc_context3(avcodec_find_decoder(id));
    unsigned count = 0;
    const uint8_t *p = data;
    int left = (int)size;
    for (;;) {
        uint8_t *au = NULL;
        int au_size = 0;
        int used = av_parser_parse2(parser, ctx, &au, &au_size, p, left, AV_NOPTS_VALUE,
                                    AV_NOPTS_VALUE, 0);
        p += used;
        left -= used;
        if (au_size && count < max) {
            out[count] = av_packet_alloc();
            av_new_packet(out[count], au_size);
            memcpy(out[count]->data, au, (size_t)au_size);
            count++;
        }
        if (!left && !au_size) {
            if (!p)
                break;
            p = NULL; /* drain the last access unit */
        }
    }
    av_parser_close(parser);
    avcodec_free_context(&ctx);
    return count;
}

enum failure { GET_FORMAT, ERRORS, NONE_WORKS };

/* the stream through a "hardware" context that fails as asked; the pictures decoded */
static unsigned long long run(struct stream *s, const char *caps, AVPacket **aus, unsigned count,
                              enum failure how, unsigned *kept)
{
    AVFrame *f = av_frame_alloc();
    s->caps = strdup(caps);
    s->decoded = s->errors = 0;
    open_codec(s, caps, 0);
    s->hw_format = AV_PIX_FMT_VAAPI; /* as open_codec leaves a hardware context */
    s->queue_max = FRAME_QUEUE;
    if (how != NONE_WORKS)
        s->ctx->skip_frame = AVDISCARD_ALL; /* parses the stream, gives no picture */
    if (how == GET_FORMAT)
        s->hw_fallback = 1; /* as get_format sets it during the first packet */
    *kept = 0;
    for (unsigned i = 0; i < count; i++) {
        if (how == ERRORS && i == 2)
            s->hw_errors = 3; /* the third packet fails too, nothing decoded */
        decode_packet(s, aus[i], f, s->generation);
        if (s->nreplay > *kept)
            *kept = s->nreplay;
    }
    decode_packet(s, NULL, f, s->generation); /* end of stream: the delayed pictures */
    unsigned long long decoded = s->decoded;
    kept_at_end = s->nreplay;
    free_frames(s);
    close_codec(s);
    free(s->caps);
    s->caps = NULL;
    s->hw_banned = 0;
    av_frame_free(&f);
    return decoded;
}

static void codec(const char *name, enum AVCodecID id, const char *caps, const unsigned char *data,
                  size_t size, struct stream *s)
{
    AVPacket *aus[64];
    unsigned count = split(id, data, size, aus, 64);
    unsigned kept;
    ok(count == PICTURES, "%s: %u access units", name, count);
    unsigned long long d = run(s, caps, aus, count, GET_FORMAT, &kept);
    ok(d == PICTURES, "%s: hardware not available at the first packet: %llu of %d pictures", name,
       d, PICTURES);
    d = run(s, caps, aus, count, ERRORS, &kept);
    ok(d == PICTURES, "%s: hardware fails three packets: %llu of %d pictures, %u packets kept",
       name, d, PICTURES, kept);
    d = run(s, caps, aus, count, NONE_WORKS, &kept);
    ok(d == PICTURES && kept <= REPLAY_PACKETS && kept_at_end == 0,
       "%s: hardware that works: %llu of %d pictures, at most %u packets kept, none after", name, d,
       PICTURES, kept);
    for (unsigned i = 0; i < count; i++)
        av_packet_free(&aus[i]);
}

int main(void)
{
    struct decoder *m = calloc(1, sizeof *m);
    m->ports = 1;
    snprintf(m->hwdec, sizeof m->hwdec, "none");
    pthread_mutex_init(&m->hw_lock, NULL);
    struct stream *s = &m->streams[0];
    s->m = m;
    s->viewer = -1;
    s->generation = 1;
    snprintf(s->preference, sizeof s->preference, "none");
    pthread_mutex_init(&s->lock, NULL);
    pthread_cond_init(&s->cond, NULL);
    codec("h264", AV_CODEC_ID_H264,
          "video/x-h264, stream-format=(string)byte-stream, alignment=(string)au", one_h264,
          sizeof one_h264, s);
    codec("hevc", AV_CODEC_ID_HEVC,
          "video/x-h265, stream-format=(string)byte-stream, alignment=(string)au", one_hevc,
          sizeof one_hevc, s);
    printf("1..%d\n", n);
    pthread_mutex_destroy(&m->hw_lock);
    free(m);
    return failed ? 1 : 0;
}
