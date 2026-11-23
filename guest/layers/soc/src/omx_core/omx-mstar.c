// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libOMX.MStar.so.1 for the emulator: MStar's OpenMAX IL core, whose streams go to the host.
 *
 * On W15U the core's one client is NDL's Esplayer (libndl-directmedia2.so.1, NDL_Esplayer*: the
 * NDL of webOS 2), which links it directly (RPATH /usr/lib/openmax) and drives its components
 * itself; gst-omx, its other user on the TV, is the emulator's own libgstomx.so here (../video),
 * which does not load a core. What the client relies on (a study of libndl-directmedia2.so.1.0.0
 * and the stock core of W15U 04.06.25):
 *
 * - The core entry points are the standard ones with an MS_ prefix. Components are found by role
 *   (MS_OMX_GetComponentsOfRole, then GetHandle on each name until one opens): a video decoder
 *   (video_decoder.avc, .hevc, .mpeg2), an audio decoder (audio_decoder.aac, .mp3, .ac3), the
 *   video renderer (iv_renderer.yuv.overlay: OMX.MS.RENDERER) and the audio renderer
 *   (audio_renderer.local: OMX.MS.ARENDERER). No clock component: the client keeps time itself.
 * - Tunnels are refused (ComponentTunnelRequest answers OMX_ErrorTunnelingUnsupported, as every
 *   stock component does), so the client always takes its non-tunnelled path: it allocates a
 *   decoder's output buffers and gives each to the renderer with UseBuffer (the same memory),
 *   takes each decoded frame or PCM block with FillBufferDone, holds it until it is due by its own
 *   clock and A/V sync, then copies the header's nFilledLen, nTimeStamp and nFlags to the
 *   renderer's header of the same buffer and calls the renderer's EmptyThisBuffer. It never reads
 *   a buffer's bytes.
 * - Ports are fixed: a decoder's input 0, output 1; a renderer's input 0 (the audio renderer's
 *   output 1 is disabled by the client). Buffer counts and sizes are the core's defaults
 *   (nBufferCountActual, nBufferSize), which the client adopts; for a decoder/renderer pair it
 *   sets both to the larger. FeedData copies one access unit into a free input buffer (at most
 *   nBufferSize bytes), with nTimeStamp in microseconds and EOS, and answers "feed full" when no
 *   input buffer is free.
 * - State changes, flushes and port changes complete with OMX_EventCmdComplete; the client waits
 *   1 to 3 s for them. EOS reaches it as OMX_EventBufferFlag from the renderers. Vendor indices it
 *   sets (PTS mode, maximum size, live streaming, resource information, trick mode) are taken and
 *   ignored where they mean nothing here.
 *
 * Here nothing is decoded in the guest: a decoder passes each access unit on unchanged as its
 * "decoded" output (into the output buffer, which is the renderer's buffer too), and the renderers
 * send what they are given, when the client gives it, to the host over the webos.media.<n> ports
 * (../video/src/mediaport.c without GStreamer). The client has already timed it: video goes
 * untimed, which the host shows at once, as the direct decoder does (../libhal_direct); audio is
 * stamped from its arrival on, each buffer after the one before, a buffer after a gap LEAD_MS
 * ahead. The video renderer's channel is the decoder's VDEC port from the resource information
 * the client sets on the decoder. One stream of each at a time, as the TV's player. */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include "omx-il.h"
#include "../video/src/mediaport.h"
#include "../video/src/streaminfo.h"

#define API __attribute__((visibility("default")))

#define NS          1000000000ll
#define LEAD_MS     40 /* an audio buffer after a gap starts this far ahead of its arrival */
#define MAX_BUFFERS 32
#define NAME_LEN    128 /* OMX_MAX_STRINGNAME_SIZE */

/* The vendor indices the client sets (the stock video decoder's names for them) */
#define INDEX_MSTAR_PTS_MODE      0x7F000001u
#define INDEX_MSTAR_RESOURCE_INFO 0x7F000003u /* also the trick mode, by its value */
#define INDEX_MSTAR_LIVE          0x7F000008u
#define INDEX_MSTAR_MAX_WIDTH     0x7F00000Fu
#define INDEX_MSTAR_MAX_HEIGHT    0x7F000010u
#define INDEX_MSTAR_NON_I_START   0x7F000011u
#define INDEX_AUDIO_RESOURCE      0x04100002u /* the audio decoder's and renderer's ADEC port */
#define INDEX_HAVE_AUDIO_DECODER  0x04100001u /* config: the audio renderer is fed decoded audio */
#define CODING_HEVC               0x7F000009u /* the stock core's OMX_VIDEO_CodingHEVC */
#define EVENT_VIDEO_INFO          0x7F000002u /* the video renderer's: the stream's video info */
#define VENDOR_3D_NONE            9u          /* OMX_VENDOR_3D_TYPES' 2D (NDL_ESP's 3D type 0) */

/* The video information event's data (the client's storeVideoInfo reads these), which it passes
 * on to ACB's setMediaVideoData; tvservice keeps the video window muted until it has it. */
struct video_info {
    OMX_U32 size, width, height, fps_n, fps_d, type_3d, par_w, par_h, scan;
};

enum kind { VDEC, ADEC, VREND, AREND };
enum codec { C_NONE, C_H264, C_HEVC, C_MPEG2, C_AAC, C_MP3, C_AC3, C_PCM };

struct desc {
    const char *name, *role;
    enum kind kind;
    enum codec codec;
};
/* The stock core's names and roles for what the client asks for. */
static const struct desc descs[] = {
    {"OMX.MS.AVC.Decoder", "video_decoder.avc", VDEC, C_H264},
    {"OMX.MS.HEVC.Decoder", "video_decoder.hevc", VDEC, C_HEVC},
    {"OMX.MS.MPEG2.Decoder", "video_decoder.mpeg2", VDEC, C_MPEG2},
    {"OMX.MS.AAC.Decoder", "audio_decoder.aac", ADEC, C_AAC},
    {"OMX.MS.MP3.Decoder", "audio_decoder.mp3", ADEC, C_MP3},
    {"OMX.MS.AC3.Decoder", "audio_decoder.ac3", ADEC, C_AC3},
    {"OMX.MS.RENDERER", "iv_renderer.yuv.overlay", VREND, C_NONE},
    {"OMX.MS.ARENDERER", "audio_renderer.local", AREND, C_NONE},
};
#define N_DESCS (sizeof descs / sizeof descs[0])

struct port {
    OMX_PARAM_PORTDEFINITIONTYPE def;
    OMX_BUFFERHEADERTYPE *bufs[MAX_BUFFERS];
    gboolean own[MAX_BUFFERS]; /* AllocateBuffer's: the bytes are ours to free */
    unsigned n;
    gboolean disabling, enabling;
};

enum msg_type { M_COMMAND, M_EMPTY, M_FILL, M_CHECK, M_QUIT };
struct msg {
    enum msg_type type;
    OMX_U32 a, b;
    OMX_BUFFERHEADERTYPE *buf;
    struct msg *next;
};

/* A FIFO of buffer headers */
struct bufq {
    OMX_BUFFERHEADERTYPE *b[MAX_BUFFERS * 2];
    unsigned head, tail;
};

struct comp {
    OMX_COMPONENTTYPE handle; /* first: the handle the client holds */
    const struct desc *desc;
    OMX_CALLBACKTYPE cb;
    OMX_PTR app;
    pthread_mutex_t lock;
    pthread_cond_t cond;
    pthread_t thread;
    struct msg *head, *tail;
    OMX_STATETYPE state, pending; /* pending: a state change waiting for its buffers, or 0 */
    struct port ports[2];
    unsigned nports;
    struct bufq in, out; /* held: input buffers to process, free output buffers */
    /* the stream */
    enum codec codec;
    OMX_AUDIO_PARAM_PCMMODETYPE pcm;
    OMX_U32 resource; /* the client's resource information (VDEC or ADEC port) */
    /* a renderer's media port */
    struct media_port port;
    gboolean open, paused, have_decoder;
    struct stream_info info; /* the video renderer's stream, from its sequence header */
    gboolean have_size, sent_info;
    struct video_info vinfo;
    gint64 base, pause_rt, next, last_ts, last_delta;
    guint64 sent;
};

/* What the decoders tell the renderers: the stream of the player (one at a time). */
static pthread_mutex_t stream_lock = PTHREAD_MUTEX_INITIALIZER;
static enum codec video_codec, audio_codec;
static OMX_U32 video_width, video_height;
static gint32 video_channel;
static int audio_rate, audio_channels;

static gint64 mono_clock(gpointer clk)
{
    (void)clk;
    return g_get_monotonic_time() * 1000;
}

static void q_push(struct bufq *q, OMX_BUFFERHEADERTYPE *b)
{
    q->b[q->head++ % (MAX_BUFFERS * 2)] = b;
}

static OMX_BUFFERHEADERTYPE *q_pop(struct bufq *q)
{
    return q->head == q->tail ? NULL : q->b[q->tail++ % (MAX_BUFFERS * 2)];
}

static gboolean q_empty(const struct bufq *q)
{
    return q->head == q->tail;
}

static struct comp *comp_of(OMX_HANDLETYPE h)
{
    return h ? ((OMX_COMPONENTTYPE *)h)->pComponentPrivate : NULL;
}

static void post(struct comp *c, enum msg_type type, OMX_U32 a, OMX_U32 b,
                 OMX_BUFFERHEADERTYPE *buf)
{
    struct msg *m = g_new0(struct msg, 1);
    m->type = type;
    m->a = a;
    m->b = b;
    m->buf = buf;
    pthread_mutex_lock(&c->lock);
    if (c->tail)
        c->tail->next = m;
    else
        c->head = m;
    c->tail = m;
    pthread_cond_signal(&c->cond);
    pthread_mutex_unlock(&c->lock);
}

/* ---- callbacks, from the component's thread with its lock released ---- */

static void event(struct comp *c, OMX_EVENTTYPE e, OMX_U32 d1, OMX_U32 d2)
{
    if (c->cb.EventHandler)
        c->cb.EventHandler(&c->handle, c->app, e, d1, d2, NULL);
}

static void empty_done(struct comp *c, OMX_BUFFERHEADERTYPE *b)
{
    if (c->cb.EmptyBufferDone)
        c->cb.EmptyBufferDone(&c->handle, c->app, b);
}

static void fill_done(struct comp *c, OMX_BUFFERHEADERTYPE *b)
{
    if (c->cb.FillBufferDone)
        c->cb.FillBufferDone(&c->handle, c->app, b);
}

/* ---- the renderers: to the host ---- */

static gboolean keyframe(enum codec codec, const guint8 *d, gsize n)
{
    for (gsize i = 0; i + 3 < n; i++) {
        if (d[i] || d[i + 1] || d[i + 2] != 1)
            continue;
        guint8 b = d[i + 3];
        if (codec == C_H264 && ((b & 0x1f) == 5 || (b & 0x1f) == 7))
            return TRUE;
        if (codec == C_HEVC) {
            guint8 t = (b >> 1) & 0x3f;
            if ((t >= 16 && t <= 21) || (t >= 32 && t <= 34))
                return TRUE;
        }
        if (codec == C_MPEG2 && b == 0xb3)
            return TRUE;
        i += 2;
    }
    return FALSE;
}

/* ADTS frames of 1024 samples each in an AAC buffer: its play time; NONE without a whole frame */
static gint64 adts_duration(const guint8 *d, gsize n, int *rate_out)
{
    static const int rates[] = {96000, 88200, 64000, 48000, 44100, 32000, 24000,
                                22050, 16000, 12000, 11025, 8000,  7350};
    gint64 frames = 0;
    int rate = 0;
    gsize i = 0;
    while (i + 7 <= n && d[i] == 0xff && (d[i + 1] & 0xf6) == 0xf0) {
        gsize len = (gsize)(d[i + 3] & 3) << 11 | (gsize)d[i + 4] << 3 | d[i + 5] >> 5;
        int sf = (d[i + 2] >> 2) & 0xf;
        if (len < 7 || sf >= 13)
            break;
        rate = rates[sf];
        frames += (d[i + 6] & 3) + 1;
        i += len;
    }
    if (rate_out && rate)
        *rate_out = rate;
    return frames && rate > 0 ? frames * 1024 * NS / rate : WEBOS_MEDIA_NONE;
}

static gint64 running_time(struct comp *c)
{
    return c->paused ? c->pause_rt : mono_clock(NULL) - c->base;
}

static void send_state(struct comp *c)
{
    struct webos_media_state s = {.playing = !c->paused,
                                  .base_time = c->base,
                                  .pause_rt = c->paused ? c->pause_rt : WEBOS_MEDIA_NONE,
                                  .rate = 1.0};
    media_port_send_state(&c->port, NULL, &s);
}

/* An MPEG-2 sequence header's size (ISO/IEC 13818-2 6.2.2.1) */
static gboolean mpeg2_size(struct stream_info *info, const guint8 *d, gsize n)
{
    for (gsize i = 0; i + 7 < n; i++)
        if (!d[i] && !d[i + 1] && d[i + 2] == 1 && d[i + 3] == 0xb3) {
            info->width = (guint)d[i + 4] << 4 | d[i + 5] >> 4;
            info->height = (guint)(d[i + 5] & 0xf) << 8 | d[i + 6];
            return info->width && info->height;
        }
    return FALSE;
}

/* What the video renderer learns of the stream from its access units: the size (and with H.264
 * the frame rate, aspect and scan) from a sequence header; HEVC's is not parsed here. */
static void learn_video(struct comp *c, enum codec codec, const guint8 *d, gsize n)
{
    if (c->have_size)
        return;
    if (codec == C_H264)
        stream_info_from_annexb(&c->info, d, n);
    else if (codec == C_MPEG2)
        mpeg2_size(&c->info, d, n);
    c->have_size = c->info.width && c->info.height;
}

/* The video information event, once the size is known and the frame rate (the SPS's, else from
 * two timestamps). */
static void video_info_event(struct comp *c, gint64 ts)
{
    if (c->sent_info || !c->have_size)
        return;
    if (c->info.fps_n <= 0 && c->last_ts != WEBOS_MEDIA_NONE && ts > c->last_ts) {
        c->info.fps_n = (gint)((NS + (ts - c->last_ts) / 2) / (ts - c->last_ts));
        c->info.fps_d = 1;
    }
    c->last_ts = ts;
    if (c->info.fps_n <= 0)
        return;
    c->vinfo = (struct video_info){.size = sizeof c->vinfo,
                                   .width = c->info.width,
                                   .height = c->info.height,
                                   .fps_n = (OMX_U32)c->info.fps_n,
                                   .fps_d = (OMX_U32)(c->info.fps_d > 0 ? c->info.fps_d : 1),
                                   .type_3d = VENDOR_3D_NONE,
                                   .par_w = c->info.par_n ? c->info.par_n : 1,
                                   .par_h = c->info.par_d ? c->info.par_d : 1,
                                   .scan = (OMX_U32)c->info.scan_type};
    c->sent_info = TRUE;
    syslog(LOG_INFO, "%s: video info %ux%u, %u/%u fps", c->desc->name, c->vinfo.width,
           c->vinfo.height, c->vinfo.fps_n, c->vinfo.fps_d);
    pthread_mutex_unlock(&c->lock);
    if (c->cb.EventHandler)
        c->cb.EventHandler(&c->handle, c->app, EVENT_VIDEO_INFO, 0, 0, &c->vinfo);
    pthread_mutex_lock(&c->lock);
}

/* The renderer's session, on its first buffer: the stream's caps from what the decoder was told. */
static gboolean open_port(struct comp *c, const guint8 *d, gsize n)
{
    char caps[224];
    pthread_mutex_lock(&stream_lock);
    enum codec vc = video_codec, ac = audio_codec;
    OMX_U32 w = video_width, h = video_height;
    gint32 ch = video_channel;
    int rate = audio_rate, channels = audio_channels;
    pthread_mutex_unlock(&stream_lock);
    if (c->desc->kind == VREND) {
        char size[64] = "";
        c->codec = vc;
        learn_video(c, vc, d, n);
        if (c->have_size) {
            w = c->info.width;
            h = c->info.height;
        }
        if (w && h)
            snprintf(size, sizeof size, ", width=(int)%u, height=(int)%u", w, h);
        if (vc == C_MPEG2)
            snprintf(caps, sizeof caps,
                     "video/mpeg, mpegversion=(int)2, systemstream=(boolean)false%s", size);
        else if (vc == C_H264 || vc == C_HEVC)
            snprintf(caps, sizeof caps,
                     "%s, stream-format=(string)byte-stream, alignment=(string)au%s",
                     vc == C_H264 ? "video/x-h264" : "video/x-h265", size);
        else {
            syslog(LOG_WARNING, "OMX.MS.RENDERER: no video decoder told its stream");
            return FALSE;
        }
        if (!media_port_open(&c->port, c->desc->name, WEBOS_MEDIA_VIDEO, ch, 0, mono_clock, NULL))
            return FALSE;
    } else {
        gint64 bytes_per_second = 0;
        c->codec = c->have_decoder ? ac : C_PCM;
        switch (c->codec) {
        case C_AAC:
            adts_duration(d, n, &rate);
            snprintf(caps, sizeof caps,
                     "audio/mpeg, mpegversion=(int)4, stream-format=(string)adts, rate=(int)%d, "
                     "channels=(int)%d",
                     rate > 0 ? rate : 48000, channels > 0 ? channels : 2);
            break;
        case C_MP3:
            snprintf(caps, sizeof caps, "audio/mpeg, mpegversion=(int)1, layer=(int)3");
            break;
        case C_AC3:
            snprintf(caps, sizeof caps, "audio/x-ac3");
            break;
        default:
            c->codec = C_PCM;
            bytes_per_second = (gint64)c->pcm.nSamplingRate * c->pcm.nChannels * 2;
            snprintf(caps, sizeof caps,
                     "audio/x-raw, format=(string)S16LE, layout=(string)interleaved, rate=(int)%u, "
                     "channels=(int)%u",
                     c->pcm.nSamplingRate, c->pcm.nChannels);
            break;
        }
        if (!media_port_open(&c->port, c->desc->name, WEBOS_MEDIA_AUDIO, (gint32)c->resource, 0,
                             mono_clock, NULL))
            return FALSE;
        c->port.bytes_per_second = bytes_per_second;
        media_port_caps_string(&c->port, NULL, caps, bytes_per_second);
        c->base = mono_clock(NULL);
        c->paused = c->state != OMX_StateExecuting;
        c->pause_rt = 0;
        c->next = c->last_ts = c->last_delta = WEBOS_MEDIA_NONE;
        send_state(c);
        c->open = TRUE;
        syslog(LOG_INFO, "%s: %s to the host (ADEC %u)", c->desc->name, caps, c->resource);
        return TRUE;
    }
    media_port_caps_string(&c->port, NULL, caps, 0);
    struct webos_media_state s = {
        .playing = 1, .base_time = 0, .pause_rt = WEBOS_MEDIA_NONE, .rate = 1.0};
    media_port_send_state(&c->port, NULL, &s);
    c->open = TRUE;
    syslog(LOG_INFO, "%s: %s to the host (channel %d)", c->desc->name, caps, ch);
    return TRUE;
}

static void close_port(struct comp *c)
{
    if (!c->open)
        return;
    media_port_stop(&c->port, NULL);
    c->open = FALSE;
    memset(&c->info, 0, sizeof c->info);
    c->have_size = c->sent_info = FALSE;
    c->last_ts = WEBOS_MEDIA_NONE;
    syslog(LOG_INFO, "%s: closed, %llu buffers sent", c->desc->name, (unsigned long long)c->sent);
    c->sent = 0;
}

static void render(struct comp *c, OMX_BUFFERHEADERTYPE *b)
{
    const guint8 *d = b->pBuffer + b->nOffset;
    gsize n = b->nFilledLen;
    if (n && (c->open || open_port(c, d, n))) {
        struct webos_media_buffer m = {.pts = WEBOS_MEDIA_NONE,
                                       .dts = WEBOS_MEDIA_NONE,
                                       .duration = WEBOS_MEDIA_NONE,
                                       .stream_time = WEBOS_MEDIA_NONE};
        if (c->desc->kind == VREND) {
            if (keyframe(c->codec, d, n))
                m.flags |= WEBOS_MEDIA_KEYFRAME;
            learn_video(c, c->codec, d, n);
        } else {
            gint64 now = running_time(c), ts = b->nTimeStamp * 1000, duration = WEBOS_MEDIA_NONE;
            m.flags = WEBOS_MEDIA_KEYFRAME;
            if (c->last_ts != WEBOS_MEDIA_NONE && ts > c->last_ts && ts - c->last_ts < NS)
                c->last_delta = ts - c->last_ts;
            c->last_ts = ts;
            if (c->port.bytes_per_second > 0)
                duration = (gint64)n * NS / c->port.bytes_per_second;
            else if (c->codec == C_AAC)
                duration = adts_duration(d, n, NULL);
            else
                duration = c->last_delta;
            m.pts =
                c->next != WEBOS_MEDIA_NONE && c->next > now ? c->next : now + LEAD_MS * 1000000ll;
            m.duration = duration;
            c->next = duration != WEBOS_MEDIA_NONE ? m.pts + duration : WEBOS_MEDIA_NONE;
        }
        media_port_send(&c->port, NULL, &m, d, n);
        c->sent++;
        if (c->desc->kind == VREND)
            video_info_event(c, b->nTimeStamp * 1000);
    }
    /* nFilledLen stays: the client reads it back after EmptyThisBuffer returns, which may be after
     * this, and takes 0 for a failed write ("write.b buffer:N, error") */
    OMX_U32 flags = b->nFlags;
    pthread_mutex_unlock(&c->lock);
    if (flags & OMX_BUFFERFLAG_EOS)
        event(c, OMX_EventBufferFlag, 0, flags);
    empty_done(c, b);
    pthread_mutex_lock(&c->lock);
}

/* ---- the work, on the component's thread with its lock held ---- */

static void pump(struct comp *c)
{
    if (c->state != OMX_StateExecuting)
        return;
    if (c->desc->kind == VREND || c->desc->kind == AREND) {
        OMX_BUFFERHEADERTYPE *b;
        while ((b = q_pop(&c->in)))
            render(c, b);
        return;
    }
    /* a decoder: each input buffer into a free output buffer, unchanged (the input's nFilledLen
     * left as given, as the renderer's) */
    while (!q_empty(&c->in) && !q_empty(&c->out)) {
        OMX_BUFFERHEADERTYPE *in = q_pop(&c->in), *out = q_pop(&c->out);
        OMX_U32 n = in->nFilledLen < out->nAllocLen ? in->nFilledLen : out->nAllocLen;
        if (n)
            memcpy(out->pBuffer, in->pBuffer + in->nOffset, n);
        out->nFilledLen = n;
        out->nOffset = 0;
        out->nTimeStamp = in->nTimeStamp;
        out->nFlags = in->nFlags;
        pthread_mutex_unlock(&c->lock);
        fill_done(c, out);
        empty_done(c, in);
        pthread_mutex_lock(&c->lock);
    }
}

/* Gives the client back what is held on port p (all ports: OMX_ALL). */
static void return_buffers(struct comp *c, OMX_U32 p)
{
    struct bufq keep_in = {0}, keep_out = {0};
    OMX_BUFFERHEADERTYPE *b;
    while ((b = q_pop(&c->in))) {
        if (p == OMX_ALL || p == 0) {
            b->nFilledLen = 0;
            pthread_mutex_unlock(&c->lock);
            empty_done(c, b);
            pthread_mutex_lock(&c->lock);
        } else
            q_push(&keep_in, b);
    }
    while ((b = q_pop(&c->out))) {
        if (p == OMX_ALL || p == 1) {
            b->nFilledLen = 0;
            b->nFlags = 0;
            pthread_mutex_unlock(&c->lock);
            fill_done(c, b);
            pthread_mutex_lock(&c->lock);
        } else
            q_push(&keep_out, b);
    }
    c->in = keep_in;
    c->out = keep_out;
}

static void complete(struct comp *c, OMX_U32 cmd, OMX_U32 data)
{
    pthread_mutex_unlock(&c->lock);
    event(c, OMX_EventCmdComplete, cmd, data);
    pthread_mutex_lock(&c->lock);
}

static gboolean populated(const struct port *p)
{
    return p->n >= p->def.nBufferCountActual;
}

/* Completes what waited for buffers: Loaded -> Idle (every enabled port populated), Idle ->
 * Loaded (every buffer freed), a port enable (populated) or disable (emptied). */
static void check(struct comp *c)
{
    gboolean all = TRUE, none = TRUE;
    for (unsigned i = 0; i < c->nports; i++) {
        struct port *p = &c->ports[i];
        p->def.bPopulated = populated(p);
        if (p->def.bEnabled && !populated(p))
            all = FALSE;
        if (p->n)
            none = FALSE;
        if (p->disabling && !p->n) {
            p->disabling = FALSE;
            complete(c, OMX_CommandPortDisable, i);
        }
        if (p->enabling && (c->state == OMX_StateLoaded || populated(p))) {
            p->enabling = FALSE;
            complete(c, OMX_CommandPortEnable, i);
        }
    }
    if (c->pending == OMX_StateIdle && all) {
        c->pending = 0;
        c->state = OMX_StateIdle;
        complete(c, OMX_CommandStateSet, OMX_StateIdle);
    } else if (c->pending == OMX_StateLoaded && none) {
        c->pending = 0;
        c->state = OMX_StateLoaded;
        complete(c, OMX_CommandStateSet, OMX_StateLoaded);
    }
}

static void renderer_state(struct comp *c, OMX_STATETYPE from, OMX_STATETYPE to)
{
    if (c->desc->kind != AREND || !c->open)
        return;
    if (to == OMX_StatePause && from == OMX_StateExecuting) {
        c->pause_rt = running_time(c);
        c->paused = TRUE;
        send_state(c);
    } else if (to == OMX_StateExecuting && c->paused) {
        c->base = mono_clock(NULL) - c->pause_rt;
        c->paused = FALSE;
        send_state(c);
    }
}

static void set_state(struct comp *c, OMX_STATETYPE to)
{
    OMX_STATETYPE from = c->state;
    if (to == from) {
        pthread_mutex_unlock(&c->lock);
        event(c, OMX_EventError, OMX_ErrorSameState, 0);
        pthread_mutex_lock(&c->lock);
        return;
    }
    if (from == OMX_StateLoaded && to == OMX_StateIdle) {
        c->pending = OMX_StateIdle;
        check(c);
        return;
    }
    if (to == OMX_StateLoaded) {
        if (c->desc->kind == VREND || c->desc->kind == AREND)
            close_port(c);
        c->pending = OMX_StateLoaded;
        check(c);
        return;
    }
    if (to == OMX_StateIdle) { /* from Executing or Pause */
        return_buffers(c, OMX_ALL);
        if (c->open)
            media_port_event(&c->port, NULL, WEBOS_MEDIA_FLUSH);
        c->next = WEBOS_MEDIA_NONE;
    }
    renderer_state(c, from, to);
    c->state = to;
    complete(c, OMX_CommandStateSet, to);
    pump(c);
}

static void flush(struct comp *c, OMX_U32 p)
{
    return_buffers(c, p);
    if ((c->desc->kind == VREND || c->desc->kind == AREND) && (p == OMX_ALL || p == 0)) {
        if (c->open)
            media_port_event(&c->port, NULL, WEBOS_MEDIA_FLUSH);
        c->next = c->last_ts = c->last_delta = WEBOS_MEDIA_NONE;
    }
    if (p == OMX_ALL) {
        for (unsigned i = 0; i < c->nports; i++)
            complete(c, OMX_CommandFlush, i);
    } else
        complete(c, OMX_CommandFlush, p);
}

static void command(struct comp *c, OMX_COMMANDTYPE cmd, OMX_U32 param)
{
    switch (cmd) {
    case OMX_CommandStateSet:
        set_state(c, param);
        break;
    case OMX_CommandFlush:
        flush(c, param);
        break;
    case OMX_CommandPortDisable:
        for (unsigned i = 0; i < c->nports; i++) {
            if (param != OMX_ALL && param != i)
                continue;
            return_buffers(c, i);
            c->ports[i].def.bEnabled = FALSE;
            c->ports[i].disabling = TRUE;
        }
        check(c);
        break;
    case OMX_CommandPortEnable:
        for (unsigned i = 0; i < c->nports; i++) {
            if (param != OMX_ALL && param != i)
                continue;
            c->ports[i].def.bEnabled = TRUE;
            c->ports[i].enabling = TRUE;
        }
        check(c);
        break;
    default:
        break;
    }
}

static void *worker(void *data)
{
    struct comp *c = data;
    pthread_mutex_lock(&c->lock);
    for (;;) {
        while (!c->head)
            pthread_cond_wait(&c->cond, &c->lock);
        struct msg *m = c->head;
        c->head = m->next;
        if (!c->head)
            c->tail = NULL;
        enum msg_type type = m->type;
        switch (type) {
        case M_COMMAND:
            command(c, m->a, m->b);
            break;
        case M_EMPTY:
            q_push(&c->in, m->buf);
            pump(c);
            break;
        case M_FILL:
            q_push(&c->out, m->buf);
            pump(c);
            break;
        case M_CHECK:
            check(c);
            break;
        case M_QUIT:
            break;
        }
        g_free(m);
        if (type == M_QUIT)
            break;
    }
    pthread_mutex_unlock(&c->lock);
    return NULL;
}

/* ---- the component's functions ---- */

static OMX_ERRORTYPE send_command(OMX_HANDLETYPE h, OMX_COMMANDTYPE cmd, OMX_U32 param, OMX_PTR d)
{
    struct comp *c = comp_of(h);
    (void)d;
    if (!c)
        return OMX_ErrorBadParameter;
    if ((cmd == OMX_CommandFlush || cmd == OMX_CommandPortDisable ||
         cmd == OMX_CommandPortEnable) &&
        param != OMX_ALL && param >= c->nports)
        return OMX_ErrorBadPortIndex;
    post(c, M_COMMAND, cmd, param, NULL);
    return OMX_ErrorNone;
}

static struct port *port_of(struct comp *c, OMX_U32 index)
{
    return index < c->nports ? &c->ports[index] : NULL;
}

static OMX_ERRORTYPE get_parameter(OMX_HANDLETYPE h, OMX_INDEXTYPE index, OMX_PTR data)
{
    struct comp *c = comp_of(h);
    if (!c || !data)
        return OMX_ErrorBadParameter;
    OMX_U32 pi = ((OMX_U32 *)data)[2];
    OMX_ERRORTYPE ret = OMX_ErrorNone;
    pthread_mutex_lock(&c->lock);
    switch (index) {
    case OMX_IndexParamPortDefinition: {
        struct port *p = port_of(c, pi);
        if (p) {
            p->def.bPopulated = populated(p);
            memcpy(data, &p->def, sizeof p->def);
        } else
            ret = OMX_ErrorBadPortIndex;
        break;
    }
    case OMX_IndexParamAudioPcm: {
        OMX_AUDIO_PARAM_PCMMODETYPE *pcm = data;
        memcpy(pcm, &c->pcm, sizeof *pcm);
        pcm->nPortIndex = pi;
        break;
    }
    case OMX_IndexParamAudioAac: {
        OMX_AUDIO_PARAM_AACPROFILETYPE *aac = data;
        OMX_U32 size = aac->nSize;
        memset(aac, 0, sizeof *aac);
        aac->nSize = size;
        aac->nVersion.nVersion = 0x00020101;
        aac->nPortIndex = pi;
        aac->nChannels = 2;
        aac->nSampleRate = 48000;
        aac->eAACProfile = 1;
        aac->eAACStreamFormat = 1;
        break;
    }
    case OMX_IndexParamAudioMp3: {
        OMX_AUDIO_PARAM_MP3TYPE *mp3 = data;
        OMX_U32 size = mp3->nSize;
        memset(mp3, 0, sizeof *mp3);
        mp3->nSize = size;
        mp3->nVersion.nVersion = 0x00020101;
        mp3->nPortIndex = pi;
        mp3->nChannels = 2;
        mp3->nSampleRate = 48000;
        break;
    }
    default:
        ret = OMX_ErrorUnsupportedIndex;
        break;
    }
    pthread_mutex_unlock(&c->lock);
    return ret;
}

static OMX_ERRORTYPE set_parameter(OMX_HANDLETYPE h, OMX_INDEXTYPE index, OMX_PTR data)
{
    struct comp *c = comp_of(h);
    if (!c || !data)
        return OMX_ErrorBadParameter;
    OMX_ERRORTYPE ret = OMX_ErrorNone;
    pthread_mutex_lock(&c->lock);
    switch (index) {
    case OMX_IndexParamPortDefinition: {
        const OMX_PARAM_PORTDEFINITIONTYPE *d = data;
        struct port *p = port_of(c, d->nPortIndex);
        if (!p) {
            ret = OMX_ErrorBadPortIndex;
            break;
        }
        if (d->nBufferCountActual >= p->def.nBufferCountMin && d->nBufferCountActual <= MAX_BUFFERS)
            p->def.nBufferCountActual = d->nBufferCountActual;
        if (d->nBufferSize > p->def.nBufferSize)
            p->def.nBufferSize = d->nBufferSize;
        if (c->desc->kind == VDEC && d->nPortIndex == 0) {
            p->def.format.video.eCompressionFormat = d->format.video.eCompressionFormat;
            p->def.format.video.nFrameWidth = d->format.video.nFrameWidth;
            p->def.format.video.nFrameHeight = d->format.video.nFrameHeight;
            enum codec vc = d->format.video.eCompressionFormat == OMX_VIDEO_CodingAVC ? C_H264
                            : d->format.video.eCompressionFormat == CODING_HEVC       ? C_HEVC
                            : d->format.video.eCompressionFormat == OMX_VIDEO_CodingMPEG2
                                ? C_MPEG2
                                : c->desc->codec;
            pthread_mutex_lock(&stream_lock);
            video_codec = vc;
            video_width = d->format.video.nFrameWidth;
            video_height = d->format.video.nFrameHeight;
            pthread_mutex_unlock(&stream_lock);
        }
        if (c->desc->kind == ADEC && d->nPortIndex == 0) {
            p->def.format.audio.eEncoding = d->format.audio.eEncoding;
            pthread_mutex_lock(&stream_lock);
            audio_codec = c->desc->codec;
            pthread_mutex_unlock(&stream_lock);
        }
        break;
    }
    case OMX_IndexParamAudioPcm: {
        const OMX_AUDIO_PARAM_PCMMODETYPE *pcm = data;
        c->pcm.nChannels = pcm->nChannels ? pcm->nChannels : 2;
        c->pcm.nSamplingRate = pcm->nSamplingRate ? pcm->nSamplingRate : 48000;
        break;
    }
    case OMX_IndexParamAudioAac: {
        const OMX_AUDIO_PARAM_AACPROFILETYPE *aac = data;
        pthread_mutex_lock(&stream_lock);
        audio_codec = C_AAC;
        audio_rate = (int)aac->nSampleRate;
        audio_channels = (int)aac->nChannels;
        pthread_mutex_unlock(&stream_lock);
        break;
    }
    case OMX_IndexParamAudioMp3:
        pthread_mutex_lock(&stream_lock);
        audio_codec = C_MP3;
        pthread_mutex_unlock(&stream_lock);
        break;
    case INDEX_MSTAR_RESOURCE_INFO:
        /* the video decoder's resource information: 9 for VDEC port 0, else 0 (port 1); the
         * same index with 2 or 0 later is the trick mode (ignored) */
        c->resource = *(OMX_U32 *)data;
        if (c->desc->kind == VDEC && (c->resource == 9 || c->resource == 0)) {
            pthread_mutex_lock(&stream_lock);
            video_channel = c->resource == 9 ? 0 : 1;
            pthread_mutex_unlock(&stream_lock);
            syslog(LOG_INFO, "%s: resource information %u: VDEC %d", c->desc->name, c->resource,
                   c->resource == 9 ? 0 : 1);
        }
        break;
    case INDEX_AUDIO_RESOURCE:
        c->resource = *(OMX_U32 *)data;
        break;
    default:
        break; /* PTS mode, maximum size, live streaming, ...: nothing to do here */
    }
    pthread_mutex_unlock(&c->lock);
    return ret;
}

static OMX_ERRORTYPE get_config(OMX_HANDLETYPE h, OMX_INDEXTYPE index, OMX_PTR data)
{
    (void)h;
    (void)index;
    (void)data;
    return OMX_ErrorUnsupportedIndex;
}

static OMX_ERRORTYPE set_config(OMX_HANDLETYPE h, OMX_INDEXTYPE index, OMX_PTR data)
{
    struct comp *c = comp_of(h);
    if (!c || !data)
        return OMX_ErrorBadParameter;
    if (index == INDEX_HAVE_AUDIO_DECODER) {
        pthread_mutex_lock(&c->lock);
        c->have_decoder = *(OMX_U32 *)data != 0;
        pthread_mutex_unlock(&c->lock);
    }
    return OMX_ErrorNone;
}

static OMX_ERRORTYPE get_extension_index(OMX_HANDLETYPE h, OMX_STRING name, OMX_INDEXTYPE *index)
{
    (void)h;
    (void)name;
    (void)index;
    return OMX_ErrorUnsupportedIndex;
}

static OMX_ERRORTYPE get_state(OMX_HANDLETYPE h, OMX_STATETYPE *state)
{
    struct comp *c = comp_of(h);
    if (!c || !state)
        return OMX_ErrorBadParameter;
    pthread_mutex_lock(&c->lock);
    *state = c->state;
    pthread_mutex_unlock(&c->lock);
    return OMX_ErrorNone;
}

static OMX_ERRORTYPE tunnel_request(OMX_HANDLETYPE h, OMX_U32 port, OMX_HANDLETYPE peer,
                                    OMX_U32 peer_port, void *setup)
{
    (void)h;
    (void)port;
    (void)peer;
    (void)peer_port;
    (void)setup;
    return OMX_ErrorTunnelingUnsupported; /* as every stock component */
}

static OMX_ERRORTYPE add_buffer(struct comp *c, OMX_BUFFERHEADERTYPE **out, OMX_U32 pi, OMX_PTR app,
                                OMX_U32 size, OMX_U8 *bytes)
{
    struct port *p = port_of(c, pi);
    if (!p || !out)
        return OMX_ErrorBadParameter;
    OMX_BUFFERHEADERTYPE *b = g_new0(OMX_BUFFERHEADERTYPE, 1);
    gboolean own = bytes == NULL;
    if (own) {
        bytes = g_try_malloc(size ? size : 1);
        if (!bytes) {
            g_free(b);
            return OMX_ErrorInsufficientResources;
        }
    }
    b->nSize = sizeof *b;
    b->nVersion.nVersion = 0x00020101;
    b->pBuffer = bytes;
    b->nAllocLen = size;
    b->pAppPrivate = app;
    if (p->def.eDir == OMX_DirInput) {
        b->nInputPortIndex = pi;
        b->nOutputPortIndex = OMX_ALL;
    } else {
        b->nOutputPortIndex = pi;
        b->nInputPortIndex = OMX_ALL;
    }
    pthread_mutex_lock(&c->lock);
    if (p->n >= MAX_BUFFERS) {
        pthread_mutex_unlock(&c->lock);
        if (own)
            g_free(bytes);
        g_free(b);
        return OMX_ErrorInsufficientResources;
    }
    p->own[p->n] = own;
    p->bufs[p->n++] = b;
    pthread_mutex_unlock(&c->lock);
    *out = b;
    post(c, M_CHECK, 0, 0, NULL);
    return OMX_ErrorNone;
}

static OMX_ERRORTYPE use_buffer(OMX_HANDLETYPE h, OMX_BUFFERHEADERTYPE **out, OMX_U32 port,
                                OMX_PTR app, OMX_U32 size, OMX_U8 *bytes)
{
    struct comp *c = comp_of(h);
    if (!c || !bytes)
        return OMX_ErrorBadParameter;
    return add_buffer(c, out, port, app, size, bytes);
}

static OMX_ERRORTYPE allocate_buffer(OMX_HANDLETYPE h, OMX_BUFFERHEADERTYPE **out, OMX_U32 port,
                                     OMX_PTR app, OMX_U32 size)
{
    struct comp *c = comp_of(h);
    if (!c)
        return OMX_ErrorBadParameter;
    return add_buffer(c, out, port, app, size, NULL);
}

static OMX_ERRORTYPE free_buffer(OMX_HANDLETYPE h, OMX_U32 pi, OMX_BUFFERHEADERTYPE *b)
{
    struct comp *c = comp_of(h);
    struct port *p = c ? port_of(c, pi) : NULL;
    if (!p || !b)
        return OMX_ErrorBadParameter;
    pthread_mutex_lock(&c->lock);
    for (unsigned i = 0; i < p->n; i++) {
        if (p->bufs[i] != b)
            continue;
        if (p->own[i])
            g_free(b->pBuffer);
        g_free(b);
        p->n--;
        p->bufs[i] = p->bufs[p->n];
        p->own[i] = p->own[p->n];
        pthread_mutex_unlock(&c->lock);
        post(c, M_CHECK, 0, 0, NULL);
        return OMX_ErrorNone;
    }
    pthread_mutex_unlock(&c->lock);
    return OMX_ErrorBadParameter;
}

static OMX_ERRORTYPE empty_this_buffer(OMX_HANDLETYPE h, OMX_BUFFERHEADERTYPE *b)
{
    struct comp *c = comp_of(h);
    if (!c || !b)
        return OMX_ErrorBadParameter;
    post(c, M_EMPTY, 0, 0, b);
    return OMX_ErrorNone;
}

static OMX_ERRORTYPE fill_this_buffer(OMX_HANDLETYPE h, OMX_BUFFERHEADERTYPE *b)
{
    struct comp *c = comp_of(h);
    if (!c || !b || c->nports < 2)
        return OMX_ErrorBadParameter;
    post(c, M_FILL, 0, 0, b);
    return OMX_ErrorNone;
}

static OMX_ERRORTYPE set_callbacks(OMX_HANDLETYPE h, OMX_CALLBACKTYPE *cb, OMX_PTR app)
{
    struct comp *c = comp_of(h);
    if (!c || !cb)
        return OMX_ErrorBadParameter;
    pthread_mutex_lock(&c->lock);
    c->cb = *cb;
    c->app = app;
    pthread_mutex_unlock(&c->lock);
    return OMX_ErrorNone;
}

static OMX_ERRORTYPE role_enum(OMX_HANDLETYPE h, OMX_U8 *role, OMX_U32 index)
{
    struct comp *c = comp_of(h);
    if (!c || !role)
        return OMX_ErrorBadParameter;
    if (index)
        return OMX_ErrorNoMore;
    snprintf((char *)role, NAME_LEN, "%s", c->desc->role);
    return OMX_ErrorNone;
}

static OMX_ERRORTYPE component_version(OMX_HANDLETYPE h, OMX_STRING name, OMX_VERSIONTYPE *cv,
                                       OMX_VERSIONTYPE *sv, OMX_U8 (*uuid)[128])
{
    struct comp *c = comp_of(h);
    if (!c)
        return OMX_ErrorBadParameter;
    if (name)
        snprintf(name, NAME_LEN, "%s", c->desc->name);
    if (cv)
        cv->nVersion = 0x00000001;
    if (sv)
        sv->nVersion = 0x00020101;
    if (uuid)
        memset(uuid, 0, sizeof *uuid);
    return OMX_ErrorNone;
}

static void init_port(struct port *p, OMX_U32 index, OMX_U32 dir, OMX_U32 domain, OMX_U32 count,
                      OMX_U32 size)
{
    memset(&p->def, 0, sizeof p->def);
    p->def.nSize = sizeof p->def;
    p->def.nVersion.nVersion = 0x00020101;
    p->def.nPortIndex = index;
    p->def.eDir = dir;
    p->def.nBufferCountActual = count;
    p->def.nBufferCountMin = 2;
    p->def.nBufferSize = size;
    p->def.bEnabled = TRUE;
    p->def.eDomain = domain;
    p->def.nBufferAlignment = 4;
}

static OMX_ERRORTYPE deinit(OMX_HANDLETYPE h);

static struct comp *comp_new(const struct desc *d)
{
    struct comp *c = g_new0(struct comp, 1);
    OMX_COMPONENTTYPE *t = &c->handle;
    t->nSize = sizeof *t;
    t->nVersion.nVersion = 0x00020101;
    t->pComponentPrivate = c;
    t->GetComponentVersion = component_version;
    t->SendCommand = send_command;
    t->GetParameter = get_parameter;
    t->SetParameter = set_parameter;
    t->GetConfig = get_config;
    t->SetConfig = set_config;
    t->GetExtensionIndex = get_extension_index;
    t->GetState = get_state;
    t->ComponentTunnelRequest = tunnel_request;
    t->UseBuffer = use_buffer;
    t->AllocateBuffer = allocate_buffer;
    t->FreeBuffer = free_buffer;
    t->EmptyThisBuffer = empty_this_buffer;
    t->FillThisBuffer = fill_this_buffer;
    t->SetCallbacks = set_callbacks;
    t->ComponentDeInit = deinit;
    t->ComponentRoleEnum = role_enum;
    c->desc = d;
    c->state = OMX_StateLoaded;
    c->codec = d->codec;
    c->pcm.nSize = sizeof c->pcm;
    c->pcm.nVersion.nVersion = 0x00020101;
    c->pcm.nChannels = 2;
    c->pcm.eNumData = 0; /* signed */
    c->pcm.eEndian = 1;  /* little */
    c->pcm.bInterleaved = TRUE;
    c->pcm.nBitPerSample = 16;
    c->pcm.nSamplingRate = 48000;
    c->next = c->last_ts = c->last_delta = WEBOS_MEDIA_NONE;
    /* a video access unit fits a buffer of 2 MiB; audio frames 64 KiB */
    switch (d->kind) {
    case VDEC:
        c->nports = 2;
        init_port(&c->ports[0], 0, OMX_DirInput, OMX_PortDomainVideo, 4, 2u << 20);
        init_port(&c->ports[1], 1, OMX_DirOutput, OMX_PortDomainVideo, 6, 2u << 20);
        break;
    case ADEC:
        c->nports = 2;
        init_port(&c->ports[0], 0, OMX_DirInput, OMX_PortDomainAudio, 8, 64u << 10);
        init_port(&c->ports[1], 1, OMX_DirOutput, OMX_PortDomainAudio, 16, 64u << 10);
        c->ports[1].def.format.audio.eEncoding = OMX_AUDIO_CodingPCM;
        break;
    case VREND:
        c->nports = 1;
        init_port(&c->ports[0], 0, OMX_DirInput, OMX_PortDomainVideo, 6, 2u << 20);
        break;
    case AREND:
        c->nports = 2;
        init_port(&c->ports[0], 0, OMX_DirInput, OMX_PortDomainAudio, 16, 64u << 10);
        c->ports[0].def.format.audio.eEncoding = OMX_AUDIO_CodingPCM;
        init_port(&c->ports[1], 1, OMX_DirOutput, OMX_PortDomainAudio, 2, 4096);
        break;
    }
    pthread_mutex_init(&c->lock, NULL);
    pthread_cond_init(&c->cond, NULL);
    media_port_init(&c->port);
    if (pthread_create(&c->thread, NULL, worker, c)) {
        media_port_clear(&c->port);
        g_free(c);
        return NULL;
    }
    return c;
}

static OMX_ERRORTYPE deinit(OMX_HANDLETYPE h)
{
    struct comp *c = comp_of(h);
    if (!c)
        return OMX_ErrorBadParameter;
    post(c, M_QUIT, 0, 0, NULL);
    pthread_join(c->thread, NULL);
    while (c->head) {
        struct msg *m = c->head;
        c->head = m->next;
        g_free(m);
    }
    close_port(c);
    media_port_clear(&c->port);
    for (unsigned i = 0; i < c->nports; i++)
        for (unsigned j = 0; j < c->ports[i].n; j++) {
            if (c->ports[i].own[j])
                g_free(c->ports[i].bufs[j]->pBuffer);
            g_free(c->ports[i].bufs[j]);
        }
    pthread_cond_destroy(&c->cond);
    pthread_mutex_destroy(&c->lock);
    syslog(LOG_INFO, "%s: freed", c->desc->name);
    g_free(c);
    return OMX_ErrorNone;
}

/* ---- the core ---- */

API OMX_ERRORTYPE MS_OMX_Init(void)
{
    return OMX_ErrorNone;
}

API OMX_ERRORTYPE MS_OMX_DeInit(void)
{
    return OMX_ErrorNone;
}

API OMX_ERRORTYPE MS_OMX_ComponentNameEnum(OMX_STRING name, OMX_U32 len, OMX_U32 index)
{
    if (!name)
        return OMX_ErrorBadParameter;
    if (index >= N_DESCS)
        return OMX_ErrorNoMore;
    snprintf(name, len, "%s", descs[index].name);
    return OMX_ErrorNone;
}

API OMX_ERRORTYPE MS_OMX_GetHandle(OMX_HANDLETYPE *h, OMX_STRING name, OMX_PTR app,
                                   OMX_CALLBACKTYPE *cb)
{
    if (!h || !name)
        return OMX_ErrorBadParameter;
    for (size_t i = 0; i < N_DESCS; i++) {
        if (strcmp(name, descs[i].name))
            continue;
        struct comp *c = comp_new(&descs[i]);
        if (!c)
            return OMX_ErrorInsufficientResources;
        if (cb)
            c->cb = *cb;
        c->app = app;
        c->handle.pApplicationPrivate = app;
        *h = &c->handle;
        syslog(LOG_INFO, "MS_OMX_GetHandle: %s (%s)", name, descs[i].role);
        return OMX_ErrorNone;
    }
    syslog(LOG_WARNING, "MS_OMX_GetHandle: no component %s", name);
    return OMX_ErrorComponentNotFound;
}

API OMX_ERRORTYPE MS_OMX_FreeHandle(OMX_HANDLETYPE h)
{
    return h ? ((OMX_COMPONENTTYPE *)h)->ComponentDeInit(h) : OMX_ErrorBadParameter;
}

/* The standard core's: the output's tunnel request, then the input's (refused here). */
API OMX_ERRORTYPE MS_OMX_SetupTunnel(OMX_HANDLETYPE out, OMX_U32 out_port, OMX_HANDLETYPE in,
                                     OMX_U32 in_port)
{
    if (!out || !in)
        return OMX_ErrorBadParameter;
    return ((OMX_COMPONENTTYPE *)out)->ComponentTunnelRequest(out, out_port, in, in_port, NULL);
}

API OMX_ERRORTYPE MS_OMX_TearDownTunnel(OMX_HANDLETYPE out, OMX_HANDLETYPE in)
{
    (void)out;
    (void)in;
    return OMX_ErrorNone;
}

/* names NULL: the count of components with the role; else up to *count names (128 bytes each). */
API OMX_ERRORTYPE MS_OMX_GetComponentsOfRole(OMX_STRING role, OMX_U32 *count, OMX_U8 **names)
{
    OMX_U32 n = 0;
    if (!role || !count)
        return OMX_ErrorBadParameter;
    for (size_t i = 0; i < N_DESCS; i++) {
        if (strcmp(role, descs[i].role))
            continue;
        if (names) {
            if (n >= *count)
                break;
            snprintf((char *)names[n], NAME_LEN, "%s", descs[i].name);
        }
        n++;
    }
    *count = n;
    return OMX_ErrorNone;
}

API OMX_ERRORTYPE MS_OMX_GetRolesOfComponent(OMX_STRING name, OMX_U32 *count, OMX_U8 **roles)
{
    if (!name || !count)
        return OMX_ErrorBadParameter;
    for (size_t i = 0; i < N_DESCS; i++) {
        if (strcmp(name, descs[i].name))
            continue;
        if (roles && *count)
            snprintf((char *)roles[0], NAME_LEN, "%s", descs[i].role);
        *count = 1;
        return OMX_ErrorNone;
    }
    *count = 0;
    return OMX_ErrorComponentNotFound;
}
