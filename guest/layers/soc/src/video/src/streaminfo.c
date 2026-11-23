// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Stream parameters for LG's video-info message: codec, size, frame rate, pixel aspect ratio and
 * scan type. The caps from the demuxer or parser usually carry all of them; when they do not
 * (WebKit's MSE caps), an H.264 SPS from codec_data or from the byte stream fills the gaps.
 * H.264 only: other codecs report what their caps say. With STREAMINFO_CORE (no GStreamer: the
 * OpenMAX core, ../../omx_core), only the SPS part. */
#include "streaminfo.h"
#include <string.h>

#ifndef STREAMINFO_CORE
static const struct {
    const char *mime;
    const char *codec;
} codecs[] = {
    {"video/x-h264", "H264"},    {"video/x-h265", "HEVC"},       {"video/x-hevc", "HEVC"},
    {"video/x-divx", "MPEG4"},   {"video/x-xvid", "MPEG4"},      {"video/x-3ivx", "MPEG4"},
    {"video/x-h263", "H263"},    {"video/x-intel-h263", "H263"}, {"video/x-wmv", "VC1"},
    {"video/x-msmpeg", "MPEG4"}, {"video/x-pn-realvideo", "RV"}, {"video/x-vp8", "VP8"},
    {"video/x-vp9", "VP9"},      {"video/x-svq", "SVQ"},         {"video/x-flash-video", "FLV"},
    {"video/x-ffv", "FFV"},      {"image/jpeg", "MJPEG"},        {"video/x-raw", "RAW"},
};

#endif

gboolean stream_info_complete(const struct stream_info *info)
{
    return info->width && info->height && info->fps_n > 0 && info->fps_d > 0;
}

#ifndef STREAMINFO_CORE
gboolean stream_info_from_caps(struct stream_info *info, GstCaps *caps)
{
    memset(info, 0, sizeof *info);
    info->par_n = info->par_d = 1;
    info->scan_type = SCAN_PROGRESSIVE;
    if (!caps || gst_caps_get_size(caps) < 1)
        return FALSE;
    GstStructure *s = gst_caps_get_structure(caps, 0);
    const char *mime = gst_structure_get_name(s);
    info->video = g_str_has_prefix(mime, "video/") || g_str_has_prefix(mime, "image/");
    info->h264 = !strcmp(mime, "video/x-h264");
    g_strlcpy(info->codec, mime, sizeof info->codec);
    for (unsigned i = 0; i < G_N_ELEMENTS(codecs); ++i)
        if (!strcmp(mime, codecs[i].mime))
            g_strlcpy(info->codec, codecs[i].codec, sizeof info->codec);
    int version;
    if (!strcmp(mime, "video/mpeg") && gst_structure_get_int(s, "mpegversion", &version))
        g_snprintf(info->codec, sizeof info->codec, "MPEG%d", version);

    int w, h, n, d;
    if (gst_structure_get_int(s, "width", &w) && gst_structure_get_int(s, "height", &h) && w > 0 &&
        h > 0) {
        info->width = w;
        info->height = h;
    }
    if (gst_structure_get_fraction(s, "framerate", &n, &d) && n > 0 && d > 0) {
        info->fps_n = n;
        info->fps_d = d;
    }
    if (gst_structure_get_fraction(s, "pixel-aspect-ratio", &n, &d) && n > 0 && d > 0) {
        info->par_n = n;
        info->par_d = d;
    }
    /* "mixed" is a container that does not know (W17H's matroskademux says it of VP9): a decoder
     * would find the frames progressive. */
    const char *mode = gst_structure_get_string(s, "interlace-mode");
    if (mode && (!strcmp(mode, "interleaved") || !strcmp(mode, "fields")))
        info->scan_type = SCAN_INTERLACED;

    const GValue *cd = gst_structure_get_value(s, "codec_data");
    if (info->h264 && cd && G_VALUE_HOLDS(cd, GST_TYPE_BUFFER) && !stream_info_complete(info)) {
        GstMapInfo map;
        GstBuffer *buf = gst_value_get_buffer(cd);
        if (buf && gst_buffer_map(buf, &map, GST_MAP_READ)) {
            stream_info_from_avcc(info, map.data, map.size);
            gst_buffer_unmap(buf, &map);
        }
    }
    return stream_info_complete(info);
}
#endif

/* ---- H.264 sequence parameter set (ITU-T H.264 7.3.2.1.1, E.1.1) ---- */

struct bits {
    const guint8 *p;
    gsize size, pos;
    gboolean overrun;
};

static guint bit(struct bits *b)
{
    if (b->pos >= b->size * 8) {
        b->overrun = TRUE;
        return 0;
    }
    guint v = (b->p[b->pos >> 3] >> (7 - (b->pos & 7))) & 1;
    b->pos++;
    return v;
}
static guint32 bits(struct bits *b, int n)
{
    guint32 v = 0;
    while (n--)
        v = (v << 1) | bit(b);
    return v;
}
static guint32 ue(struct bits *b)
{
    int zeros = 0;
    while (!bit(b) && !b->overrun)
        if (++zeros > 31) {
            b->overrun = TRUE;
            return 0;
        }
    return ((1u << zeros) - 1) + bits(b, zeros);
}
static gint32 se(struct bits *b)
{
    guint32 k = ue(b);
    return (k & 1) ? (gint32)((k + 1) / 2) : -(gint32)(k / 2);
}
static void scaling_list(struct bits *b, int n)
{
    int last = 8, next = 8;
    for (int j = 0; j < n; ++j) {
        if (next)
            next = (last + se(b) + 256) % 256;
        last = next ? next : last;
    }
}

/* Table E-1 */
static const guint8 sar_table[17][2] = {
    {0, 0},   {1, 1},   {12, 11}, {10, 11}, {16, 11},  {40, 33}, {24, 11}, {20, 11}, {32, 11},
    {80, 33}, {18, 11}, {15, 11}, {64, 33}, {160, 99}, {4, 3},   {3, 2},   {2, 1},
};

/* nal: one NAL unit including its header byte, emulation prevention bytes still in. */
static gboolean parse_sps(struct stream_info *info, const guint8 *nal, gsize size)
{
    if (size < 4 || (nal[0] & 0x1f) != 7)
        return FALSE;
    guint8 rbsp[512];
    gsize n = 0;
    for (gsize i = 1; i < size && n < sizeof rbsp; ++i) {
        if (i + 2 < size && nal[i] == 0 && nal[i + 1] == 0 && nal[i + 2] == 3) {
            rbsp[n++] = 0;
            if (n < sizeof rbsp)
                rbsp[n++] = 0;
            i += 2;
            continue;
        }
        rbsp[n++] = nal[i];
    }
    struct bits b = {rbsp, n, 0, FALSE};
    guint profile = bits(&b, 8);
    bits(&b, 16); /* constraint flags, level_idc */
    ue(&b);       /* seq_parameter_set_id */
    guint chroma = 1, separate = 0;
    if (profile == 100 || profile == 110 || profile == 122 || profile == 244 || profile == 44 ||
        profile == 83 || profile == 86 || profile == 118 || profile == 128 || profile == 138 ||
        profile == 139 || profile == 134 || profile == 135) {
        chroma = ue(&b);
        if (chroma == 3)
            separate = bit(&b);
        ue(&b);
        ue(&b);      /* bit depths */
        bit(&b);     /* qpprime_y_zero_transform_bypass_flag */
        if (bit(&b)) /* seq_scaling_matrix_present_flag */
            for (int i = 0; i < (chroma != 3 ? 8 : 12); ++i)
                if (bit(&b))
                    scaling_list(&b, i < 6 ? 16 : 64);
    }
    ue(&b); /* log2_max_frame_num_minus4 */
    guint poc_type = ue(&b);
    if (poc_type == 0) {
        ue(&b);
    } else if (poc_type == 1) {
        bit(&b);
        se(&b);
        se(&b);
        guint cycle = ue(&b);
        if (cycle > 255)
            return FALSE;
        for (guint i = 0; i < cycle; ++i)
            se(&b);
    }
    ue(&b);  /* max_num_ref_frames */
    bit(&b); /* gaps_in_frame_num_value_allowed_flag */
    guint mbs_w = ue(&b) + 1, map_h = ue(&b) + 1;
    guint frame_mbs_only = bit(&b);
    if (!frame_mbs_only)
        bit(&b); /* mb_adaptive_frame_field_flag */
    bit(&b);     /* direct_8x8_inference_flag */
    guint cl = 0, cr = 0, ct = 0, cb = 0;
    if (bit(&b)) {
        cl = ue(&b);
        cr = ue(&b);
        ct = ue(&b);
        cb = ue(&b);
    }
    guint sar_w = 0, sar_h = 0, tick = 0, scale = 0;
    if (bit(&b)) {     /* vui_parameters_present_flag */
        if (bit(&b)) { /* aspect_ratio_info_present_flag */
            guint idc = bits(&b, 8);
            if (idc == 255) {
                sar_w = bits(&b, 16);
                sar_h = bits(&b, 16);
            } else if (idc < G_N_ELEMENTS(sar_table)) {
                sar_w = sar_table[idc][0];
                sar_h = sar_table[idc][1];
            }
        }
        if (bit(&b))
            bit(&b);   /* overscan */
        if (bit(&b)) { /* video_signal_type_present_flag */
            bits(&b, 4);
            if (bit(&b))
                bits(&b, 24); /* colour description */
        }
        if (bit(&b)) {
            ue(&b);
            ue(&b);
        } /* chroma_loc_info */
        if (bit(&b)) { /* timing_info_present_flag */
            tick = bits(&b, 32);
            scale = bits(&b, 32);
        }
    }
    if (b.overrun || mbs_w > 1024 || map_h > 1024)
        return FALSE;
    guint sub_w = (chroma == 1 || chroma == 2) ? 2 : 1, sub_h = chroma == 1 ? 2 : 1;
    guint unit_x = (chroma == 0 || separate) ? 1 : sub_w;
    guint unit_y = ((chroma == 0 || separate) ? 1 : sub_h) * (2 - frame_mbs_only);
    guint w = mbs_w * 16, h = map_h * 16 * (2 - frame_mbs_only);
    if ((cl + cr) * unit_x >= w || (ct + cb) * unit_y >= h)
        return FALSE;
    if (!info->width || !info->height) {
        info->width = w - (cl + cr) * unit_x;
        info->height = h - (ct + cb) * unit_y;
        info->crop_left = cl * unit_x;
        info->crop_right = cr * unit_x;
        info->crop_top = ct * unit_y;
        info->crop_bottom = cb * unit_y;
    }
    if (info->fps_n <= 0 && tick && scale) {
        /* Two ticks per frame (field-based time units), E.2.1 */
        guint64 num = scale, den = 2ull * tick;
        while (num > G_MAXINT || den > G_MAXINT) {
            num >>= 1;
            den >>= 1;
        }
        if (num && den) {
            info->fps_n = num;
            info->fps_d = den;
        }
    }
    if (sar_w && sar_h && info->par_n == 1 && info->par_d == 1) {
        info->par_n = sar_w;
        info->par_d = sar_h;
    }
    if (!frame_mbs_only)
        info->scan_type = SCAN_INTERLACED;
    return TRUE;
}

gboolean stream_info_from_avcc(struct stream_info *info, const guint8 *data, gsize size)
{
    /* AVCDecoderConfigurationRecord: version 1, ..., numOfSequenceParameterSets at [5]. */
    if (size < 7 || data[0] != 1)
        return FALSE;
    guint count = data[5] & 0x1f;
    gsize pos = 6;
    for (guint i = 0; i < count && pos + 2 <= size; ++i) {
        gsize len = (data[pos] << 8) | data[pos + 1];
        pos += 2;
        if (pos + len > size)
            break;
        if (parse_sps(info, data + pos, len))
            return TRUE;
        pos += len;
    }
    return FALSE;
}

gboolean stream_info_from_annexb(struct stream_info *info, const guint8 *data, gsize size)
{
    gsize i = 0;
    while (i + 3 < size) {
        if (data[i] || data[i + 1] || data[i + 2] != 1) {
            ++i;
            continue;
        }
        gsize start = i + 3, end = start;
        while (end + 2 < size && !(data[end] == 0 && data[end + 1] == 0 &&
                                   (data[end + 2] == 1 || data[end + 2] == 0)))
            ++end;
        if (end + 2 >= size)
            end = size;
        if (start < end && (data[start] & 0x1f) == 7 && parse_sps(info, data + start, end - start))
            return TRUE;
        i = end;
    }
    return FALSE;
}
