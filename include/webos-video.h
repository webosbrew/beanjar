// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef WEBOS_VIDEO_H
#define WEBOS_VIDEO_H
#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdint.h>
#endif
/* Little-endian, fixed-size frames on the webos.video virtio-serial port (host side: QEMU
 * chardev socket build/video.sock). One writer per target, below LG's tvservice: on LG115x chips
 * the video scaler HAL of guest/layers/soc/src/libhal_lg115x/hal-video.c, on MStar chips the XC
 * scaler of mstar-utopia.ko through lgemu.ko's /dev/lgemu-video and the initramfs's pump
 * (guest/initramfs/src/virtio-video.c). Every media or TV window tvservice sets up on the scaler is
 * a context, MAIN or SUB (videosinkmanager/connect opens and connects it,
 * tv.display/setDisplayWindow sets its output region, tv.avblock and tv.power/turnOffAv mute it).
 * The reader is the viewer, which draws each shown context under the OSD: the decoded video of
 * the media stream on its input (include/webos-media.h, video milestone 2), or a placeholder
 * pattern while there is none.
 *
 * Every frame carries the whole table of contexts, so a viewer that connects late or missed a
 * frame is in sync with the next one; one is sent on every change (with the event that caused
 * it) and the table is repeated every second while it is not empty. A context is shown when
 * connected && has_rect && !av_mute && w && h. The event names are the media path's; the HAL
 * sends them for: register = the window opened, connect = connected to its input, rect = output
 * region set, info = input region set (the stream size), mute = blanked or unblanked, disconnect,
 * unregister = closed. */
#define WEBOS_VIDEO_MAGIC       "WOSVID3"
#define WEBOS_VIDEO_MAX         8
#define WEBOS_VIDEO_CONTEXT_LEN 48
enum webos_video_event {
    WEBOS_VIDEO_SYNC = 0,   /* periodic repeat, nothing changed */
    WEBOS_VIDEO_REGISTER,   /* videosinkmanager/private/register */
    WEBOS_VIDEO_CONNECT,    /* videosinkmanager/connect */
    WEBOS_VIDEO_RECT,       /* tv.display/setDisplayWindow, setCustomDisplayWindow */
    WEBOS_VIDEO_INFO,       /* tv.display/private/setMediaVideoData */
    WEBOS_VIDEO_MUTE,       /* tv.avblock/startAvMute, stopAvMute */
    WEBOS_VIDEO_DISCONNECT, /* videosinkmanager/disconnect */
    WEBOS_VIDEO_UNREGISTER, /* videosinkmanager/private/unregister: the context is gone */
};
struct webos_video_context {
    char context[WEBOS_VIDEO_CONTEXT_LEN]; /* media context id, NUL-terminated */
    uint32_t registered, connected, av_mute;
    uint32_t has_rect, full_screen;
    int32_t x, y; /* output window in screen pixels (screen_w x screen_h) */
    uint32_t w, h;
    uint32_t width, height; /* stream size from setMediaVideoData, 0 if not known yet */
    uint32_t fps_x1000;     /* frame rate x 1000, 0 if not known */
    uint32_t scan_type;     /* 0 progressive, 1 interlaced (setMediaVideoData scanType) */
    uint32_t input;         /* the scaler input: 0 AVD, 1 ADC, 2 HDMI, 3 VDEC, 4 JPEG */
    uint32_t input_port;    /* its port: for VDEC the pipeline's VDEC resource, which is also
                               the vdecsink's vdec-ch (include/webos-media.h channel) */
    /* The scaler's input region inside the source of width x height: the part of the picture the
     * window shows (overscan, picture size). crop_w or crop_h 0, or a region not inside the
     * source: the whole source. */
    uint32_t crop_x, crop_y, crop_w, crop_h;
};
struct webos_video_packet {
    char magic[8];
    uint32_t bytes;              /* sizeof(struct webos_video_packet) */
    uint32_t sequence;           /* incremented per frame */
    uint32_t event;              /* enum webos_video_event */
    uint32_t subject;            /* index in contexts[] the event is about; for UNREGISTER the
                                    context is already gone and this is WEBOS_VIDEO_MAX */
    uint32_t screen_w, screen_h; /* 1920 x 1080 */
    uint32_t count;              /* valid entries at the start of contexts[] */
    struct webos_video_context contexts[WEBOS_VIDEO_MAX];
};
_Static_assert(sizeof(struct webos_video_context) == 124, "video context wire record");
_Static_assert(sizeof(struct webos_video_packet) == 1028, "video wire frame");
#endif
