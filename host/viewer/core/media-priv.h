// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef VIEWER_MEDIA_PRIV_H
#define VIEWER_MEDIA_PRIV_H
/* What the media module's files share (media.c: timing, composition, playback;
 * media-ipc.c: the helper transport and session state; media-frame.c: received
 * frame objects). Not installed; media.h is the public API. */
#include "media.h"
#include "../../../include/webos-media.h"

#include "../../media/ipc.h"

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <SDL.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>

#define NONE               WEBOS_MEDIA_NONE
#define OUT_RATE           48000
#define FRAME_QUEUE        24           /* decoded pictures waiting for their time */
#define PACKET_BYTES_MAX   (64u << 20)  /* compressed data queued before the reader waits */
#define AUDIO_QUEUE_MAX_NS 2000000000ll /* decoded audio queued before the decoder waits */
#define MAX_AUDIO_SLIP     25000000ll   /* ns: beyond this, resynchronise at once */
#define MIN_AUDIO_SLIP     2000000ll    /* ns: beyond this, drift one sample per NUDGE */
#define NUDGE              100          /* one sample in NUDGE: 1 % faster or slower */
#define HW_QUEUE           8            /* pictures queued from a hardware decoder's pool */
#define OFFSET_BUCKET      2000000000ll /* the offset's minimum is over 2-4 s */
#define REPORT_NS          5000000000ll
#define DECODE_ONLY_RING   64
#define HW_TYPES           32 /* above enum AVHWDeviceType's values */

struct received_frame {
    struct host_media_message descriptor;
    int format, width, height, colorspace, color_range, linesize[4];
    int64_t pts;
    uint8_t *data[4];
    void *mapping;
    int fds[HOST_MEDIA_OBJECTS], connection, release_type;
};

struct chunk {
    struct chunk *next;
    int64_t rt;
    int frames, pos;
    float data[];
};

struct offset {
    int64_t cur, prev, start;
};

struct stream {
    struct media *m;
    int index;
    char path[256];
    pthread_t reader;
    int threads;
    pthread_mutex_t lock;
    pthread_cond_t cond;
    /* session (lock) */
    uint64_t generation;
    uint32_t session;
    int open, eos;
    struct webos_media_open info;
    struct offset offset;
    int64_t clock_delta; /* guest mono - pipeline clock */
    int have_delta, have_state;
    struct webos_media_state state;
    struct received_frame *frames[FRAME_QUEUE];
    unsigned nframes;
    int need_first, reset_current;
    struct chunk *ahead, *atail;
    int64_t aqueued; /* ns of audio queued */
    /* main thread only */
    struct received_frame *current;
    int uploaded;
    GLuint tex[3];
    EGLImageKHR image;
    /* statistics */
    /* shown: pictures drawn into a video window (once each); picked: pictures that came due,
     * whether or not a window showed them */
    unsigned long long packets, bytes_in, received, shown, picked, late_drops, skipped_bytes,
        errors;
    double late_sum, late_max;
    unsigned late_n;
    double aslip_sum, aslip_max;
    unsigned aslip_n, resyncs, underruns, nudges;
    unsigned long long audio_frames, reported_packets;
    int64_t last_report;
    int64_t pts_first, pts_last; /* of the pictures drawn since OPEN (NONE: none yet) */
};

struct media {
    struct media_config cfg;
    int probe; /* WEBOS_MEDIA_PROBE=1: log flashes shown and beeps played */
    int probe_bright;
    int64_t probe_quiet_since;
    volatile int quit;
    volatile int no_decoder; /* the helper's latest ACK reported HM_DECODER_UNAVAILABLE */
    struct stream streams[WEBOS_MEDIA_PORTS];
    /* audio out */
    SDL_AudioDeviceID dev;
    int sdl_inited, audio_started;
    int64_t latency_ns;
    pthread_t clock_thread;
    int clock_running;
    FILE *wav;
    unsigned long long wav_frames;
    pthread_mutex_t audio_lock;
    /* the TV's master volume and speaker mute (media_tv_gain): the gain the guest's sound card
     * applies, Q16, set by the main thread; and the gain the mix last ended at, the mixing
     * thread's, from which a change ramps over one block */
    volatile unsigned tv_gain;
    float tv_applied;
    /* GL */
    GLuint yuv, oes;
    GLint yuv_planes, yuv_scale_y, yuv_scale_c, yuv_matrix, yuv_offset, oes_flip;
    int have_oes;
    int64_t opened;
    PFNEGLCREATEIMAGEKHRPROC create_image;
    PFNEGLDESTROYIMAGEKHRPROC destroy_image;
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC image_target;
    EGLDisplay display;
};

int64_t mono_ns(void);
/* media-frame.c: validate, map and own one received frame; sending its release. */
void frame_free(struct received_frame **ptr);
struct received_frame *frame_receive(int fd, const struct host_media_message *msg, int *fds,
                                     unsigned nfds);
/* media-ipc.c: the per-stream reader thread, session snapshots and reports. */
void *reader(void *arg);
void free_frames(struct stream *s);
void report(struct stream *s, const char *why);
/* media.c: start SDL/clock audio out once an audio stream opens. */
void start_audio(struct media *m);
#endif
