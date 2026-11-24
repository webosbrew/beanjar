// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef WEBOS_TUNER_H
#define WEBOS_TUNER_H
#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdint.h>
#endif
/* The antenna on the webos.tuner virtio-serial port: between the host's tuner
 * (host/core/beanjar/tuner.py) and dtv_soc.ko (guest/kernel/modules/dtv_soc), through the
 * initramfs's pump (guest/initramfs/src/virtio-tuner.c), which copies both ways between the port
 * and /dev/dtv-soc (the kernel cannot read a virtio port itself).
 *
 * Both ways a stream of frames, each a header and len bytes, all words little-endian:
 *   magic    WEBOS_TUNER_MAGIC
 *   type     below
 *   freq_hz  the frequency the frame is about
 *   len      the payload's bytes
 * host -> guest:
 *   WEBOS_TUNER_MAP  the channels that carry a signal, the whole map: len / 8 struct
 *                    webos_tuner_channel (at most WEBOS_TUNER_MAP_MAX), freq_hz 0. A frontend
 *                    tuned near freq_hz with that delivery system locks: an ATSC (8VSB) stream
 *                    does not lock a cable (QAM) tune on the same frequency
 *   WEBOS_TUNER_TS   transport stream packets of freq_hz, len a multiple of 188 (at most
 *                    WEBOS_TUNER_TS_MAX): fed to the demuxes while the set is tuned to freq_hz
 * guest -> host:
 *   WEBOS_TUNER_TUNE the set is tuned to freq_hz (0: to nothing yet); payload one word, the
 *                    delivery system (enum fe_delivery_system). Sent at each tune and after each
 *                    WEBOS_TUNER_MAP, so a host that (re)connects and sends its map learns it.
 *                    With two frontends it is the latest tune with a signal (dtv_soc's soc_tuned):
 *                    the host sends one stream. */
#define WEBOS_TUNER_MAGIC   0x4e555457u /* "WTUN" */
#define WEBOS_TUNER_MAP     1u
#define WEBOS_TUNER_TS      2u
#define WEBOS_TUNER_TUNE    3u
#define WEBOS_TUNER_MAP_MAX 128u
#define WEBOS_TUNER_TS_MAX  (188u * 512u)
#define WEBOS_TUNER_PACKET  188u
struct webos_tuner_frame {
    uint32_t magic, type, freq_hz, len;
};
struct webos_tuner_channel {
    uint32_t freq_hz, delsys; /* Hz; enum fe_delivery_system */
};
_Static_assert(sizeof(struct webos_tuner_frame) == 16, "tuner wire header");
_Static_assert(sizeof(struct webos_tuner_channel) == 8, "tuner wire channel");

/* The decoders' input, inside the guest: what the demuxes hand the SoC's decoders (their
 * DMX_OUT_DECODER PES filters), read from dtv_soc.ko's /dev/dtv-soc-av by the same pump, which
 * sends each decoder's stream to the host's media decoder on a webos.media.<n> port
 * (include/webos-media.h). Each read() returns whole records, a header and len bytes, in the
 * guest's byte order:
 *   WEBOS_TUNER_AV_START  a decoder starts: kind (WEBOS_MEDIA_VIDEO, WEBOS_MEDIA_AUDIO),
 *                         decoder (its index of that kind), channel (video: the VDEC channel,
 *                         the scaler's VDEC port; audio: the decoder's index), codec (video:
 *                         the V4L2 fourcc the service set; audio: 0, the stream says)
 *   WEBOS_TUNER_AV_TS     transport stream packets of that decoder's PID, len a multiple of 188
 *   WEBOS_TUNER_AV_STOP   the decoder stops
 *   WEBOS_TUNER_AV_PCR    a PCR of the PCR filter: struct webos_tuner_pcr; kind and decoder 0
 * A record that does not fit is dropped (counted in /proc/dtv_soc), never a part of one. */
#define WEBOS_TUNER_AV_MAGIC 0x56415457u /* "WTAV" */
enum {
    WEBOS_TUNER_AV_START = 1,
    WEBOS_TUNER_AV_TS,
    WEBOS_TUNER_AV_STOP,
    WEBOS_TUNER_AV_PCR,
};
struct webos_tuner_av {
    uint32_t magic;
    uint16_t type;
    uint8_t kind, decoder;
    uint32_t channel, codec, len, reserved;
};
struct webos_tuner_pcr {
    uint64_t pcr;     /* 27 MHz */
    uint64_t mono_ns; /* CLOCK_MONOTONIC when its packet reached the demux */
};
_Static_assert(sizeof(struct webos_tuner_av) == 24, "tuner av record");
_Static_assert(sizeof(struct webos_tuner_pcr) == 16, "tuner av pcr");
#endif
