// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Codec opens: native decoder selection, hardware device setup with software
 * fallback, thread counts and NAL parsing. */
#include "decode-priv.h"

#include <libavcodec/avcodec.h>
#include <libavutil/hwcontext.h>
#include <libavutil/opt.h>
#include <libavutil/pixdesc.h>
#include <libswresample/swresample.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static enum AVPixelFormat get_format(AVCodecContext *ctx, const enum AVPixelFormat *list)
{
    struct stream *s = ctx->opaque;
    for (const enum AVPixelFormat *p = list; *p != AV_PIX_FMT_NONE; p++)
        if (*p == s->hw_format)
            return *p;
    for (const enum AVPixelFormat *p = list; *p != AV_PIX_FMT_NONE; p++) {
        const AVPixFmtDescriptor *d = av_pix_fmt_desc_get(*p);
        if (d && !(d->flags & AV_PIX_FMT_FLAG_HWACCEL)) {
            s->hw_fallback = 1; /* the decoder thread reopens it in software, with threads */
            return *p;
        }
    }
    return list[0];
}

static AVBufferRef *hw_device(struct decoder *m, enum AVHWDeviceType type)
{
    pthread_mutex_lock(&m->hw_lock);
    if (!m->hw_tried[type]) {
        m->hw_tried[type] = 1;
        int r = av_hwdevice_ctx_create(&m->hw_device[type], type, NULL, NULL, 0);
        elog(-1, "media: hw device %s: %s", av_hwdevice_get_type_name(type),
             r < 0 ? av_err2str(r) : "opened");
    }
    AVBufferRef *ref = m->hw_device[type] ? av_buffer_ref(m->hw_device[type]) : NULL;
    pthread_mutex_unlock(&m->hw_lock);
    return ref;
}

void drop_replay(struct stream *s)
{
    for (unsigned i = 0; i < s->nreplay; i++)
        av_packet_free(&s->replay[i]);
    s->nreplay = 0;
    s->replay_bytes = 0;
}

void close_codec(struct stream *s)
{
    if (s->parser)
        av_parser_close(s->parser);
    s->parser = NULL;
    avcodec_free_context(&s->ctx);
    drop_replay(s);
    swr_free(&s->swr);
    av_channel_layout_uninit(&s->swr_layout);
    s->decode_only_n = 0;
    s->next_apts = NONE;
    s->hw_gave = 0;
    s->hw_last_pts = s->replay_until = NONE;
}

/* The codec context for caps into s->ctx (none when it cannot open); its buffers need parsing
 * into frames (the caps' alignment) or not. */
static int open_context(struct stream *s, const char *caps, int allow_hw)
{
    struct codec_params p;
    caps_codec(caps, &p);
    const AVCodec *codec = NULL;
    if (s->m->software_only) {
        /* the short list only, and AV1 through libdav1d (never FFmpeg's own av1, which decodes
         * through a hardware device only) */
        if (p.id != AV_CODEC_ID_NONE && !caps_software_only(p.id))
            elog(s->index, "media %d: %s refused: hwdec=none decodes a short list of codecs",
                 s->index, avcodec_get_name(p.id));
        else if (p.id == AV_CODEC_ID_AV1)
            codec = avcodec_find_decoder_by_name("libdav1d");
        else if (p.id != AV_CODEC_ID_NONE)
            codec = avcodec_find_decoder(p.id);
    } else if (p.id != AV_CODEC_ID_NONE)
        codec = avcodec_find_decoder(p.id);
    if (!codec) {
        elog(s->index, "media %d: no decoder for %.120s", s->index, caps);
        av_free(p.extradata);
        pthread_mutex_lock(&s->lock);
        snprintf(s->codec_name, sizeof s->codec_name, "none");
        pthread_mutex_unlock(&s->lock);
        return 0;
    }
    AVCodecContext *ctx = avcodec_alloc_context3(codec);
    ctx->opaque = s;
    ctx->max_pixels = HOST_MEDIA_MAX_BYTES / 4;
    ctx->pkt_timebase = (AVRational){1, 1000000000};
    ctx->extradata = p.extradata;
    ctx->extradata_size = p.extradata_size;
    if (p.width && p.height) {
        ctx->width = p.width;
        ctx->height = p.height;
    }
    if (codec->type == AVMEDIA_TYPE_AUDIO) {
        ctx->sample_rate = p.rate;
        if (p.channels)
            av_channel_layout_default(&ctx->ch_layout, p.channels);
        ctx->block_align = p.block_align;
        ctx->bit_rate = p.bit_rate;
        ctx->bits_per_coded_sample = p.bits;
    }
    char preference[32];
    pthread_mutex_lock(&s->lock);
    memcpy(preference, s->preference, sizeof preference);
    pthread_mutex_unlock(&s->lock);
    int hw_none = s->m->software_only || !strcmp(preference, "none");
    enum AVHWDeviceType hw_only = !strcmp(preference, "auto")
                                      ? AV_HWDEVICE_TYPE_NONE
                                      : av_hwdevice_find_type_by_name(preference);
    int hw = 0;
    s->hw_format = AV_PIX_FMT_NONE;
    if (codec->type == AVMEDIA_TYPE_VIDEO && allow_hw && !hw_none) {
        for (int i = 0;; i++) {
            const AVCodecHWConfig *cfg = avcodec_get_hw_config(codec, i);
            if (!cfg)
                break;
            if (!(cfg->methods & AV_CODEC_HW_CONFIG_METHOD_HW_DEVICE_CTX))
                continue;
            if (hw_only != AV_HWDEVICE_TYPE_NONE && cfg->device_type != hw_only)
                continue;
            AVBufferRef *dev = hw_device(s->m, cfg->device_type);
            if (!dev)
                continue;
            ctx->hw_device_ctx = dev;
            s->hw_format = cfg->pix_fmt;
            ctx->get_format = get_format;
            ctx->extra_hw_frames = HW_QUEUE + HOST_MEDIA_LEASES + 4;
            hw = 1;
            elog(s->index, "media %d: %s through %s (%s)", s->index, codec->name,
                 av_hwdevice_get_type_name(cfg->device_type), av_get_pix_fmt_name(cfg->pix_fmt));
            break;
        }
    }
    if (!hw && codec->type == AVMEDIA_TYPE_VIDEO) {
        long cpus = sysconf(_SC_NPROCESSORS_ONLN);
        ctx->thread_count = cpus > 4 ? 4 : cpus > 0 ? (int)cpus : 1;
        /* frame threads delay every picture by a packet per thread: not for a guest that waits
         * for each picture (a DECODE session) */
        ctx->thread_type = s->dec_ret ? FF_THREAD_SLICE : FF_THREAD_FRAME | FF_THREAD_SLICE;
    }
    int r = avcodec_open2(ctx, codec, NULL);
    if (r < 0) {
        elog(s->index, "media %d: cannot open %s: %s", s->index, codec->name, av_err2str(r));
        avcodec_free_context(&ctx);
        return hw ? open_context(s, caps, 0) : 0;
    }
    s->ctx = ctx;
    s->hw_errors = 0;
    s->hw_fallback = 0;
    s->queue_max = hw ? HW_QUEUE : FRAME_QUEUE;
    pthread_mutex_lock(&s->lock);
    snprintf(s->codec_name, sizeof s->codec_name, "%s", codec->name);
    s->hw_active = hw;
    pthread_mutex_unlock(&s->lock);
    elog(s->index, "media %d: decoder %s (%s)%s", s->index, codec->name,
         hw ? "hardware" : "software", p.extradata_size ? ", with codec data" : "");
    return p.split;
}

void open_codec(struct stream *s, const char *caps, int allow_hw)
{
    close_codec(s);
    if (open_context(s, caps, allow_hw) && s->ctx && (s->parser = av_parser_init(s->ctx->codec_id)))
        elog(s->index, "media %d: buffers are not whole frames: parsed into frames", s->index);
}

void software_codec(struct stream *s)
{
    avcodec_free_context(&s->ctx);
    s->hw_format = AV_PIX_FMT_NONE;
    s->hw_fallback = 0;
    if (s->caps)
        open_context(s, s->caps, 0);
}
