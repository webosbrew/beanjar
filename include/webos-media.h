// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef WEBOS_MEDIA_H
#define WEBOS_MEDIA_H
#ifdef __KERNEL__
#include <linux/ioctl.h>
#include <linux/types.h>
#else
#include <stdint.h>
#include <sys/ioctl.h>
#endif
/* Compressed media from the guest's decoder sinks to the host viewer (video milestone 2). The
 * writers are the soc layer's vdecsink and adecsink (guest/layers/soc/src/video): each sink pad of
 * a media pipeline takes one of the virtio-serial ports webos.media.0 ..
 * webos.media.<WEBOS_MEDIA_PORTS-1> for as long as it runs (READY->PAUSED to PAUSED->READY), the
 * first one that is free (a port is open in one guest process at a time), opened as
 * /dev/lg/webos.media.<n> (the initramfs's nodes, which a native app's jail has too). On the host
 * each port is a QEMU chardev socket, build/media-<n>.sock, read by the viewer, which decodes with
 * libavcodec and shows the video under the OSD at the scaler window of include/webos-video.h.
 *
 * The port is a byte stream of messages, little-endian: a header, then `size` payload bytes. A
 * session starts with OPEN; CAPS, BUFFER, STATE, FLUSH and EOS follow in stream order; CLOSE ends
 * it. A reader that joins late, or loses the connection, scans for the next valid header (magic
 * and check), and the writer starts over with OPEN, CAPS and STATE when the host end reconnects
 * (and a video writer then waits for a keyframe).
 *
 * Timing. Timestamps are running times of the pipeline (the segment already applied), in ns,
 * signed (a buffer decoded only for reference can be before the segment). A frame or sample with
 * running time R is presented at pipeline clock time base_time + R, exactly as the stock sinks
 * would present it; STATE carries base_time and whether the pipeline is PLAYING, and a paused
 * pipeline stays on running time pause_rt. The sinks render WEBOS_MEDIA_LEAD_NS early (their own
 * lead, beside the ts-offset the pipeline sets, which shifts R here as it shifts a stock sink) so
 * the host has the data before it is due, while the pipeline's position,
 * which is clock based, stays what the host shows. Every header carries the guest's
 * CLOCK_MONOTONIC and the pipeline clock at the time it was written: the host maps pipeline clock
 * to its own monotonic clock with them (the minimum of receive time - send time over a window).
 *
 * Decoding for the guest. A guest decoder whose pictures go to a consumer that needs real
 * pixels (an appsink rather than one of the emulator's sinks) sends DECODE right after OPEN: the
 * session's pictures then come back on the same port instead of going to a viewer. Every BUFFER
 * and the EOS of such a session is answered, host to guest, with PICTURE messages in the same
 * framing (a header with the host's own sequence, mono and clock NONE): each decoded picture,
 * then one marked WEBOS_MEDIA_PICTURE_LAST (with or without a picture) when the host is done
 * with that input. The guest waits for the LAST answer to each input before it sends the next,
 * so the host holds at most one input's pictures; it drops answers to an input it no longer waits
 * for (after a flush). In such a session BUFFER's pts and dts are the buffer's own timestamps, not
 * running times, and a PICTURE's pts is the decoded picture's; STATE is not sent. Additions to
 * WSM2: a host without them refuses the session as an invalid message.
 *
 * An audio decoder whose samples go to a sink of the firmware's (W24G spacebuns' playbin3 picks
 * PulseAudio's pulsesink over LG's audiosink, and LG's decoders always push PCM) asks the same
 * with WEBOS_MEDIA_S16LE: each PICTURE then carries interleaved signed 16-bit stereo samples at
 * WEBOS_MEDIA_S16LE_RATE, `width` the frames that follow (4 bytes each), `height` 2, `stride[0]`
 * 4, its pts the first frame's. Audio answers always come inline (no pool).
 *
 * The pool. A picture's bytes would cost the port one interrupt round trip per 4 KiB,
 * so a VM run as a systemd unit has a shared memory pool for them: QEMU's stock ivshmem-doorbell
 * (PCI 1af4:1110, one MSI-X vector) whose memory is a WEBOS_MEDIA_POOL_SIZE file in the unit's
 * RuntimeDirectory, served by the media helper (it is the device's ivshmem server). The host owns
 * the allocator: granule 0 is the directory (struct webos_media_pool_dir), which the host writes
 * and the guest's kernel reads, and every other granule belongs to at most one extent, granted to
 * one port. The guest never assumes a layout: it takes every place from a message or a ring entry.
 *
 * A guest that can use the pool (its WEBOS_MEDIA_POOL_DEVICE claimed for the port) sets
 * WEBOS_MEDIA_DECODE_SHARED, and the host answers every DECODE with POOL: the session's control
 * extent (struct webos_media_control, its two rings), or size 0 for none (no pool, or no room),
 * when pictures come inline as above. In a pool session the PICTURE answers (pictures and LAST
 * markers) are entries of the control's picture ring instead of port messages, each picture
 * WEBOS_MEDIA_PICTURE_SHARED at `slot`, a pool offset inside an extent granted to the port, and
 * the slot is the guest's until it puts {slot, input} on the release ring. The host lays its
 * slots out as it likes (WEBOS_MEDIA_POOL_SLOTS slots of the picture's size, allocated at the
 * first picture and again when a picture outgrows them). When the pool has no room for them the
 * session's answers go inline on the port from then on (never back): a guest takes what is on the
 * ring before what came on the port. The host waits WEBOS_MEDIA_RELEASE_WAIT_MS for a free slot
 * and otherwise drops the picture (the LAST answer still comes).
 *
 * The rings are single-producer single-consumer, indices free-running (an entry is index %
 * WEBOS_MEDIA_RING_ENTRIES). A consumer about to sleep sets `waiting`, issues a full barrier and
 * looks at `head` again; a producer publishes `head` (release), issues a full barrier and rings
 * the other side only if `waiting` is set. The host rings the guest with the device's vector 0;
 * the guest rings the host through WEBOS_MEDIA_IOC_RING (the kernel writes the doorbell with the
 * directory's host_peer). One vector serves every port: a wake-up says only "look at your rings".
 *
 * Extents go back to the host when the session ends (CLOSE, a new OPEN, a re-layout, the claiming
 * file closed, the guest's port gone): the host marks them RETIRING, the guest's kernel refuses new
 * exports of them, and when no dma-buf of an extent is left (the kernel's export count in the
 * directory) the host punches its pages out of the file and frees its granules. A count the guest
 * gets wrong can only delay or leak that VM's own pool. Everything the guest writes (ring indices
 * and entries, counts, the claim counter) is untrusted on the host and checked there.
 *
 * Byte streams (tuner, capture, compressed media) can take the same path later: such a session
 * would allocate a control extent with a byte-ring header and a data extent, granted to its port
 * in the same directory, and use the same doorbell and wait; nothing here is specific to pictures
 * but the ring entries. Without the pool, or without the flag, pictures come inline as above. */
#define WEBOS_MEDIA_MAGIC       0x324d5357u /* "WSM2" */
#define WEBOS_MEDIA_PORTS       8
#define WEBOS_MEDIA_PORT_NAME   "webos.media."
#define WEBOS_MEDIA_MAX_PAYLOAD (16u << 20)
#define WEBOS_MEDIA_LEAD_NS     300000000ll
#define WEBOS_MEDIA_NONE        INT64_MIN /* no timestamp */

enum webos_media_type {
    WEBOS_MEDIA_OPEN = 1, /* struct webos_media_open */
    WEBOS_MEDIA_CAPS,     /* the GstCaps as a string (gst_caps_to_string), NUL-terminated;
                             codec_data and streamheader are in it as hex buffers */
    WEBOS_MEDIA_BUFFER,   /* struct webos_media_buffer, then the buffer's bytes */
    WEBOS_MEDIA_STATE,    /* struct webos_media_state */
    WEBOS_MEDIA_FLUSH,    /* no payload: drop everything queued (flush-start) */
    WEBOS_MEDIA_EOS,      /* no payload: sent when the last buffer is due */
    WEBOS_MEDIA_CLOSE,    /* no payload */
    WEBOS_MEDIA_DECODE,   /* struct webos_media_decode: return this session's pictures */
    WEBOS_MEDIA_PICTURE,  /* host to guest: struct webos_media_picture, then the picture */
    WEBOS_MEDIA_POOL,     /* host to guest: struct webos_media_pool, the answer to DECODE */
};
enum webos_media_kind { WEBOS_MEDIA_VIDEO = 1, WEBOS_MEDIA_AUDIO = 2 };
enum {
    WEBOS_MEDIA_KEYFRAME = 1,    /* buffer flags */
    WEBOS_MEDIA_DECODE_ONLY = 2, /* before the segment: decode for reference, do not show */
    WEBOS_MEDIA_DISCONT = 4,
};
struct webos_media_header {
    uint32_t magic;
    uint16_t type;
    uint16_t flags;
    uint32_t size;     /* payload bytes */
    uint32_t sequence; /* per port, incremented per message */
    int64_t mono;      /* guest CLOCK_MONOTONIC, ns */
    int64_t clock;     /* the pipeline clock, ns, or WEBOS_MEDIA_NONE without one */
    uint32_t check;    /* webos_media_check() of the fields above */
    uint32_t reserved;
};
struct webos_media_open {
    uint32_t kind;    /* enum webos_media_kind */
    int32_t pid;      /* guest process */
    int32_t channel;  /* vdecsink vdec-ch, adecsink index */
    uint32_t pad;     /* the vdecsink request pad's number; 0 for audio */
    char element[28]; /* the element's name */
    uint32_t output;  /* video: the scaler input its pictures reach, WEBOS_MEDIA_OUTPUT(type,
                         port) with the HAL's input types (VDEC 3, HDMI 2); 0: VDEC, the
                         port its channel (vdec-ch) */
};
#define WEBOS_MEDIA_OUTPUT(type, port) (0x80000000u | (uint32_t)(type) << 8 | (uint32_t)(port))
struct webos_media_buffer {
    int64_t pts, dts;    /* running time, WEBOS_MEDIA_NONE if not known */
    int64_t duration;    /* ns, WEBOS_MEDIA_NONE if not known */
    int64_t stream_time; /* the pts as stream time (the media position), for logs */
    uint32_t flags;      /* WEBOS_MEDIA_KEYFRAME, ... */
    uint32_t reserved;
};
struct webos_media_state {
    uint32_t playing; /* 1: PLAYING; 0: PAUSED, frozen at pause_rt */
    uint32_t reserved;
    int64_t base_time; /* the element's base time on the pipeline clock */
    int64_t pause_rt;  /* running time where a paused pipeline stopped, or NONE */
    double rate;       /* segment rate, for logs */
};
/* The pictures' formats, laid out as GstVideoInfo lays them out by default, which the PICTURE
 * repeats: NV12 with the stride the width rounded up to 4 and the CbCr plane after RU2(height)
 * rows; YUY2 (Y0 U Y1 V) in one plane, the stride twice the width rounded up to 4. */
#define WEBOS_MEDIA_NV12       0x3231564eu /* fourcc "NV12" */
#define WEBOS_MEDIA_YUY2       0x32595559u /* fourcc "YUY2" */
#define WEBOS_MEDIA_S16LE      0x454c3631u /* "16LE": an audio session's samples (above) */
#define WEBOS_MEDIA_S16LE_RATE 48000
enum { WEBOS_MEDIA_DECODE_SHARED = 1 }; /* webos_media_decode.flags */
struct webos_media_decode {
    uint32_t format; /* WEBOS_MEDIA_NV12 or WEBOS_MEDIA_YUY2; audio: WEBOS_MEDIA_S16LE */
    uint32_t flags;  /* WEBOS_MEDIA_DECODE_SHARED: the guest takes shared pictures */
};
enum { /* webos_media_picture.flags */
       WEBOS_MEDIA_PICTURE_LAST = 1,
       WEBOS_MEDIA_PICTURE_SHARED = 2,
};
struct webos_media_picture {
    uint32_t input;  /* the header sequence of the BUFFER or EOS this answers */
    uint32_t flags;  /* WEBOS_MEDIA_PICTURE_LAST */
    int64_t pts;     /* the picture's, as the BUFFER's, or WEBOS_MEDIA_NONE */
    uint32_t format; /* as DECODE asked; 0: no picture (then nothing follows) */
    uint32_t width, height;
    uint32_t stride[2]; /* bytes per row of each plane (YUY2: one, the second 0) */
    uint32_t offset[2]; /* of each plane in the bytes that follow (SHARED: from slot) */
    uint32_t slot;      /* SHARED: the picture's place, bytes from the start of the pool */
};
struct webos_media_pool {
    uint32_t offset; /* the session's control extent (struct webos_media_control) in the pool */
    uint32_t size;   /* its bytes; 0: no pool for this session, pictures inline */
};
struct webos_media_release {
    uint32_t slot;  /* a SHARED picture's slot */
    uint32_t input; /* and the input it answered */
};
#define WEBOS_MEDIA_POOL_SIZE       (128u << 20)
#define WEBOS_MEDIA_POOL_GRANULE    (64u << 10)
#define WEBOS_MEDIA_POOL_EXTENTS    4           /* per port */
#define WEBOS_MEDIA_POOL_SLOTS      4           /* pictures a session's slots hold at most */
#define WEBOS_MEDIA_POOL_SESSION    (64u << 20) /* a session's extents together at most */
#define WEBOS_MEDIA_POOL_MAGIC      0x504d5357u /* "WSMP" */
#define WEBOS_MEDIA_POOL_VERSION    1
#define WEBOS_MEDIA_POOL_DEVICE     "/dev/lg/webos.media-pool"
#define WEBOS_MEDIA_RELEASE_WAIT_MS 100
#define WEBOS_MEDIA_RING_ENTRIES    64
enum { /* webos_media_extent.state */
       WEBOS_MEDIA_EXTENT_FREE = 0,
       WEBOS_MEDIA_EXTENT_LIVE = 1,     /* granted: the claiming file may export it */
       WEBOS_MEDIA_EXTENT_RETIRING = 2, /* no new exports; freed when its exports are 0 */
};
struct webos_media_extent { /* written by the host */
    uint32_t offset, size;  /* in the pool, granule aligned */
    uint32_t state;
    uint32_t generation; /* a new one each time the entry is granted */
};
struct webos_media_pool_port {
    struct webos_media_extent extent[WEBOS_MEDIA_POOL_EXTENTS]; /* host */
    uint32_t exports[WEBOS_MEDIA_POOL_EXTENTS]; /* guest kernel: live dma-bufs of each extent */
    uint32_t claim; /* guest kernel: counts claims and their ends, odd while a file has the port */
    uint32_t reserved[3];
};
struct webos_media_pool_dir { /* granule 0 */
    uint32_t magic;           /* host: WEBOS_MEDIA_POOL_MAGIC once the rest is valid */
    uint32_t version;         /* WEBOS_MEDIA_POOL_VERSION */
    uint32_t size;            /* the pool's bytes */
    uint32_t host_peer;       /* the host's ivshmem peer id, which the guest's doorbell names */
    uint32_t reserved[12];
    struct webos_media_pool_port port[WEBOS_MEDIA_PORTS];
};
struct webos_media_ring {
    uint32_t head;    /* producer: entries written */
    uint32_t tail;    /* consumer: entries taken */
    uint32_t waiting; /* consumer: set while it sleeps until head moves */
    uint32_t reserved;
};
struct webos_media_control {
    struct webos_media_ring pictures; /* host to guest */
    struct webos_media_ring releases; /* guest to host */
    uint32_t reserved[8];
    struct webos_media_picture picture[WEBOS_MEDIA_RING_ENTRIES];
    struct webos_media_release release[WEBOS_MEDIA_RING_ENTRIES];
};
/* The guest's device. CLAIM binds the open file to a port (EBUSY while another file has it);
 * EXPORT gives a dma-buf of size bytes at offset in the pool, refused unless the range lies in a
 * LIVE extent granted to the claimed port, which the guest maps (mmap of the dma-buf; CPU access
 * only); RING rings the host. poll() is readable when an interrupt came since the file's last
 * read(), which returns the device's interrupt count (4 bytes) and never blocks. */
struct webos_media_export {
    uint32_t offset, size; /* in the pool */
    int32_t fd;            /* out: the dma-buf */
    uint32_t reserved;
};
#define WEBOS_MEDIA_IOC_CLAIM  _IOW('W', 1, uint32_t)
#define WEBOS_MEDIA_IOC_EXPORT _IOWR('W', 2, struct webos_media_export)
#define WEBOS_MEDIA_IOC_RING   _IO('W', 3)
_Static_assert(sizeof(struct webos_media_header) == 40, "media header");
_Static_assert(sizeof(struct webos_media_decode) == 8, "media decode");
_Static_assert(sizeof(struct webos_media_picture) == 48, "media picture");
_Static_assert(sizeof(struct webos_media_release) == 8, "media release");
_Static_assert(sizeof(struct webos_media_pool) == 8, "media pool");
_Static_assert(sizeof(struct webos_media_pool_dir) == 64 + WEBOS_MEDIA_PORTS * 96, "pool dir");
_Static_assert(sizeof(struct webos_media_control) == 64 + 64 * 56, "pool control");
_Static_assert(sizeof(struct webos_media_control) <= WEBOS_MEDIA_POOL_GRANULE, "control size");
_Static_assert(sizeof(struct webos_media_export) == 16, "media export");
_Static_assert(sizeof(struct webos_media_open) == 48, "media open");
_Static_assert(sizeof(struct webos_media_buffer) == 40, "media buffer");
_Static_assert(sizeof(struct webos_media_state) == 32, "media state");

static inline uint32_t webos_media_check(const struct webos_media_header *h)
{
    uint32_t words[8] = {h->magic,
                         (uint32_t)h->type | (uint32_t)h->flags << 16,
                         h->size,
                         h->sequence,
                         (uint32_t)h->mono,
                         (uint32_t)((uint64_t)h->mono >> 32),
                         (uint32_t)h->clock,
                         (uint32_t)((uint64_t)h->clock >> 32)};
    uint32_t x = 0x9e3779b9u;
    for (unsigned i = 0; i < 8; i++) {
        x ^= words[i];
        x *= 0x01000193u;
        x ^= x >> 15;
    }
    return x;
}
#endif
