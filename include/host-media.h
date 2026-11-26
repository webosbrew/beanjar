// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef HOST_MEDIA_H
#define HOST_MEDIA_H
#include <stdint.h>
#include "webos-media.h"
/* Local host IPC, version 1. SOCK_SEQPACKET preserves one descriptor and its
 * SCM_RIGHTS objects atomically. Fixed-width fields, native little endian on
 * supported hosts (aarch64/x86_64). No pointers or FFmpeg structures cross IPC.
 * Each connection has its own lease namespace. RELEASE is sent only after all
 * CPU mappings and GPU reads have retired. A generation change never releases
 * leases implicitly: old GPU work can still be using those buffers. */
#define HOST_MEDIA_MAGIC         0x31444d57u
#define HOST_MEDIA_VERSION       1
#define HOST_MEDIA_OBJECTS       4
#define HOST_MEDIA_LAYERS        4
#define HOST_MEDIA_PLANES        4
#define HOST_MEDIA_LEASES        4
#define HOST_MEDIA_MAX_BYTES     (64u << 20)
#define HOST_MEDIA_MAX_DIMENSION 8192

enum host_media_type {
    HM_HELLO = 1,
    HM_ACK,
    HM_STATE,
    HM_VIDEO,
    HM_PCM,
    HM_RELEASE,
    HM_IMPORT_FAILED
};
enum host_media_format { HM_YUV420 = 1, HM_YUV422, HM_YUV444, HM_NV12, HM_DRM, HM_FLOAT_STEREO };
enum { HM_OPEN = 1, HM_PLAY_STATE = 2, HM_CLOCK = 4, HM_EOS = 8, HM_DECODER_UNAVAILABLE = 16 };
/* An ACK's status for the HELLO's preference. LOCKED: the helper decodes in software only
 * (--software-only, a VM's hwdec=none) and refused a preference other than none; the ACK's
 * preference is the one kept, none. */
enum {
    HM_PREFERENCE_APPLIED = 1,
    HM_PREFERENCE_DEFERRED = 2,
    HM_PREFERENCE_INVALID = 3,
    HM_PREFERENCE_LOCKED = 4
};
struct host_media_object {
    uint64_t size, modifier;
};
struct host_media_plane {
    uint32_t object, stride;
    uint64_t offset;
};
struct host_media_layer {
    uint32_t fourcc, planes;
    struct host_media_plane plane[HOST_MEDIA_PLANES];
};
struct host_media_message {
    uint32_t magic, version, type, flags;
    uint64_t generation, token;
    int64_t offset, clock_delta; /* host mono - guest mono, guest mono - pipeline clock */
    struct webos_media_open open;
    struct webos_media_state state;
    uint32_t session, status; /* OPEN/CLOSE lifetime; generation also changes on FLUSH */
    uint32_t format, width, height, objects;
    uint32_t layers, colorspace, color_range, samples;
    int64_t pts;
    struct host_media_object object[HOST_MEDIA_OBJECTS];
    struct host_media_layer layer[HOST_MEDIA_LAYERS];
    char preference[32]; /* HELLO preference; ACK states applied/deferred/invalid/locked */
};
_Static_assert(sizeof(struct host_media_message) == 560, "host media descriptor size");
#endif
