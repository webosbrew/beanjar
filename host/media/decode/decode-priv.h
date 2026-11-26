// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef HOST_MEDIA_DECODE_PRIV_H
#define HOST_MEDIA_DECODE_PRIV_H
/* What the decode engine's files share (decode/: caps.c parses GstCaps,
 * session.c owns ingress and session state, codec.c opens codecs, frame.c
 * runs the decoder thread, export.c serves the viewer, decoder.c owns the
 * public lifecycle). Not installed; decode.h is the public API. */
#include "../decode.h"

#include "../clock.h"
#include "../log.h"
#include "../pool.h"

#include <libavcodec/avcodec.h>
#include <libavutil/hwcontext.h>
#include <libswresample/swresample.h>
#include <pthread.h>
#include <stdint.h>

#define NONE WEBOS_MEDIA_NONE
/* a line of the engine's log (log.h: printable, rate-limited per site and port) */
#define elog(port, ...)    hm_logf(&hm_engine_log, (port), __VA_ARGS__)
#define OUT_RATE           48000
#define FRAME_QUEUE        24           /* decoded pictures waiting for their time */
#define PACKET_BYTES_MAX   (64u << 20)  /* compressed data queued before the reader waits */
#define AUDIO_QUEUE_MAX_NS 2000000000ll /* decoded audio queued before the decoder waits */
#define MAX_AUDIO_SLIP     25000000ll   /* ns: beyond this, resynchronise at once */
#define MIN_AUDIO_SLIP     2000000ll    /* ns: beyond this, drift one sample per NUDGE */
#define NUDGE              100          /* one sample in NUDGE: 1 % faster or slower */
#define HW_QUEUE           8            /* pictures queued from a hardware decoder's pool */
#define REPORT_NS          5000000000ll
#define DECODE_ONLY_RING   64
#define REPLAY_PACKETS     64 /* kept for a hardware decoder's fallback to software */
#define REPLAY_BYTES       (16u << 20)
#define HW_TYPES           32 /* above enum AVHWDeviceType's values */

struct caps {
    char name[64];
    unsigned n;
    char *key[48], *type[48], *value[48];
};

struct codec_params {
    enum AVCodecID id;
    int width, height, rate, channels, block_align, bit_rate, bits;
    uint8_t *extradata;
    int extradata_size;
    enum AVSampleFormat pcm;
    int split; /* buffers are not whole frames (alignment=nal): parse them */
};

struct item {
    struct item *next;
    uint16_t type;
    uint32_t flags;
    uint32_t sequence; /* the guest's header sequence (a DECODE session's answers name it) */
    int ret;           /* of a DECODE session: its format (pictures go back to the guest) */
    unsigned generation, session;
    int64_t pts, dts, duration, stream_time;
    size_t size;
    uint8_t data[];
};

struct chunk {
    struct chunk *next;
    int64_t rt;
    int frames, pos;
    float data[];
};

struct stream {
    struct decoder *m;
    int index;
    char path[256];
    pthread_t decoder;
    int threads;
    pthread_mutex_t lock;
    pthread_cond_t cond;
    /* session (lock) */
    unsigned session, generation;
    int open, eos;
    int ret; /* a DECODE session's format: its pictures go back to the guest (webos-media.h) */
    /* a pool session (webos-media.h, "The pool"): its answers go on the control's rings */
    int shared;
    int inline_only;         /* the pool had no room: answers inline from then on (sticky) */
    int ctrl_e, pic_e;       /* the session's directory entries, -1: none */
    uint32_t claim;          /* the guest's claim counter when the session began */
    struct pool_rings rings; /* the rings' host ends (rings.c: the control extent) */
    uint32_t pic_off;        /* the picture extent's offset in the pool */
    uint32_t slot_size;      /* bytes per slot of the current layout (0: none yet) */
    unsigned nslots;
    struct slot {
        uint32_t input; /* the input its picture answered */
        int busy;       /* the guest's until it is on the release ring */
    } slots[WEBOS_MEDIA_POOL_SLOTS];
    /* the layout before, while the guest still has a slot of it (frame.c, take_slot): LIVE, so
     * a picture of it still on the ring can be mapped; retired when its last slot comes back */
    int old_e; /* -1: none */
    uint32_t old_off, old_slot_size, old_bytes;
    unsigned old_nslots;
    struct slot old_slots[WEBOS_MEDIA_POOL_SLOTS];
    unsigned long long slot_drops;   /* pictures dropped for want of a free slot */
    unsigned long long bad_releases; /* release entries that named no slot of the session */
    int64_t slot_wait_ns;            /* waiting for a free slot */
    struct webos_media_open info;
    struct offset offset;
    int64_t clock_delta; /* guest mono - pipeline clock */
    int have_delta, have_state;
    struct webos_media_state state;
    struct item *head, *tail;
    size_t bytes;
    AVFrame *frames[FRAME_QUEUE];
    unsigned nframes;
    int need_first, reset_current;
    struct chunk *ahead, *atail;
    int64_t aqueued; /* ns of audio queued */
    char codec_name[32];
    int hw_active, hw_import_failed;
    /* decoder thread only */
    unsigned dec_session, dec_generation;
    int dec_ret;        /* the item being decoded is a DECODE session's: its format */
    uint32_t dec_input; /* and its guest sequence */
    uint32_t reply_seq; /* PICTURE header sequence */
    unsigned long long returned, return_errors;
    int64_t return_ns; /* converting and copying the returned pictures */
    char *caps;
    AVCodecContext *ctx;
    AVCodecParserContext *parser; /* for streams whose buffers are not whole frames */
    enum AVPixelFormat hw_format;
    int hw_errors, hw_banned, hw_fallback, queue_max;
    /* what a hardware context took before its first picture, decoded again in software when it
     * cannot take the stream (frame.c, to_software); whether it gave one, and the last it gave */
    AVPacket *replay[REPLAY_PACKETS];
    unsigned nreplay;
    size_t replay_bytes;
    int hw_gave;
    int64_t hw_last_pts, replay_until;
    struct SwrContext *swr;
    AVChannelLayout swr_layout;
    int swr_rate, swr_format;
    int swr_s16; /* the converter's output: S16 for a DECODE session's samples, else float */
    int64_t next_apts;
    int64_t decode_only[DECODE_ONLY_RING];
    unsigned decode_only_n;
    int64_t decode_ns, wait_ns;
    AVFrame *current;
    uint64_t sent_epoch[FRAME_QUEUE], current_epoch, epoch, next_token;
    int viewer, ready, dirty, eos_sent;
    char preference[32];
    struct {
        AVFrame *frame;
        uint64_t token, generation;
    } leases[HOST_MEDIA_LEASES];
    AVFrame *orphan[HOST_MEDIA_LEASES];
    unsigned norphans;
    int force_download;
    /* statistics */
    /* shown: pictures drawn into a video window (once each); picked: pictures that came due,
     * whether or not a window showed them */
    unsigned long long packets, bytes_in, decoded, shown, picked, late_drops, skipped_bytes, errors;
    double late_sum, late_max;
    unsigned late_n;
    double aslip_sum, aslip_max;
    unsigned aslip_n, resyncs, underruns, nudges;
    unsigned long long audio_frames, reported_packets;
    unsigned report_session; /* the session of these statistics (session moves on at CLOSE) */
    int64_t last_report;
    int64_t pts_first, pts_last; /* of the pictures drawn since OPEN (NONE: none yet) */
};

struct decoder {
    unsigned ports;
    char hwdec[32];
    int software_only; /* decoder_create: locked to software, the short codec list */
    volatile int quit;
    struct stream streams[WEBOS_MEDIA_PORTS];
    AVBufferRef *hw_device[HW_TYPES];
    int hw_tried[HW_TYPES];
    pthread_mutex_t hw_lock;
    enum AVHWDeviceType hw_only;
    int hw_none;
    decoder_reply_fn *reply;
    void *reply_ctx;
    struct pool *pool; /* decoder_set_pool */
    int (*ring_guest)(void *ctx);
    void *ring_ctx;
};

int64_t mono_ns(void);
/* decoder.c: the host monotonic time running time rt is due (NONE: not known). */
int64_t due_mono(struct stream *s, int64_t rt);
/* caps.c: GstCaps strings to codec parameters. */
void caps_codec(const char *string, struct codec_params *p);
/* caps.c: whether a software-only decoder (decoder_create's software_only) takes the codec. */
int caps_software_only(enum AVCodecID id);
/* session.c: ingress queue, lifecycle handling, kind names and reports. */
void free_items(struct stream *s, int keep_caps);
const char *kind_name(const struct stream *s);
void report(struct stream *s, const char *why);
int handle(struct stream *s, const struct webos_media_header *h, const uint8_t *payload,
           int64_t now);
/* codec.c: codec selection, hardware setup and opens. */
void close_codec(struct stream *s);
void open_codec(struct stream *s, const char *caps, int allow_hw);
/* the codec context again from s->caps in software; the parser and its state stay */
void software_codec(struct stream *s);
/* the packets kept for a fallback to software, freed */
void drop_replay(struct stream *s);
/* frame.c: decoded frame/audio queues and the decoder thread. */
void free_frames(struct stream *s);
void *decoder_thread(void *arg);
/* one packet to the codec, its pictures queued; a hardware context that cannot take the stream
 * is replaced by a software one here, given again what the hardware took */
void decode_packet(struct stream *s, AVPacket *pkt, AVFrame *f, unsigned gen);
/* 10-bit conversion for frames the viewer cannot take as they are. */
AVFrame *to_8bit(AVFrame *f);
AVFrame *p010_to_nv12(AVFrame *f);
/* frame.c: a DECODE session's answer to the guest: the picture f (NULL: none) for input, LAST
 * or not. */
void return_picture(struct stream *s, AVFrame *f, uint32_t input, int last);
/* session.c: a pool session's releases taken off its ring (with s->lock); and its extents given
 * back (with s->lock), the session then inline. */
void take_releases(struct stream *s);
/* session.c: the layout before the current one retired (with s->lock) */
void retire_old_layout(struct stream *s);
void end_pool_session(struct stream *s);
/* export.c: viewer snapshots and object/lease export. */
struct host_media_message snapshot(struct stream *s, unsigned type);
int export_frame(struct stream *s, AVFrame *f);
int export_audio(struct stream *s, struct chunk *c);
#endif
