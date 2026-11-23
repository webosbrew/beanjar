// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Basic parameters of a video stream, from caps or from the H.264 SPS. See streaminfo.c. */
#ifndef STREAMINFO_H
#define STREAMINFO_H
#ifdef STREAMINFO_CORE
#include <glib.h>
#else
#include <gst/gst.h>
#endif

/* Scan_Type in LG's video-info message: starfish-media-pipeline passes 1 on to tv.display's
 * setMediaVideoData as "scanType":"VIDEO_INTERLACED" (seen on the emulator), so 0 is progressive.
 */
enum { SCAN_PROGRESSIVE = 0, SCAN_INTERLACED = 1 };

struct stream_info {
    gboolean video;      /* video/ or image/ caps */
    gboolean h264;       /* video/x-h264: the SPS can fill in what the caps lack */
    gchar codec[16];     /* "H264", "HEVC", ... */
    guint width, height; /* 0: unknown */
    gint fps_n, fps_d;   /* fps_n 0: unknown */
    guint par_n, par_d;
    gint scan_type;
    guint crop_left, crop_right, crop_top, crop_bottom;
};

#ifndef STREAMINFO_CORE
/* Fills info from caps; returns TRUE when size and frame rate are known. */
gboolean stream_info_from_caps(struct stream_info *info, GstCaps *caps);
#endif
/* Fills in what is still unknown from H.264 SPS NAL units, found in avcC codec_data or in an
 * Annex B byte stream. Return TRUE if an SPS was parsed. */
gboolean stream_info_from_avcc(struct stream_info *info, const guint8 *data, gsize size);
gboolean stream_info_from_annexb(struct stream_info *info, const guint8 *data, gsize size);
gboolean stream_info_complete(const struct stream_info *info);

#endif
