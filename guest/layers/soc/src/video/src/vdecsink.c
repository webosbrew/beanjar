// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* "vdecsink": LG's GstLXVdecBin (libgstlxvdecbin-1.0.so) for the emulator, video milestones 1
 * and 2.
 *
 * The real element is a bin around omx_lxvideodec + omx_lxvideosink that decodes on the SoC and
 * scans out on the video plane. lpsink (file/HTTP playback) and libpf's ES pipeline (MSE) create
 * it by factory name, set its properties and request "sink_%d" pads. This element keeps the same
 * surface (pad template and caps, the 31 properties, the convert-frame and video-underrun
 * signals), and sends the compressed stream to the host viewer, which decodes and shows it:
 *
 *   - each requested pad is ghosted to its own internal GstBaseSink (sync=TRUE), so preroll,
 *     ASYNC_DONE, position, flushing seeks and EOS are GstBaseSink/GstBin's standard behaviour;
 *   - each internal sink takes a webos.media.<n> port (mediaport.c, include/webos-media.h) and
 *     forwards its caps, buffers (running times, keyframe flags), state (base time, playing or
 *     paused), flushes and EOS. It renders WEBOS_MEDIA_LEAD_NS ahead of the clock (ts-offset), so
 *     the host holds each frame before it is due and shows it at base_time + running time on the
 *     pipeline clock, which is also what the position query reports. EOS still waits for the
 *     last frame's own time. Buffers before the segment (from the keyframe before a seek
 *     target) are sent as decode-only, since GstBaseSink would drop them unseen. Without a host
 *     (no viewer, or no free port) buffers are dropped in real time as in milestone 1;
 *   - codec, size, frame rate, pixel aspect ratio and scan type are taken from the caps (or the
 *     H.264 SPS, see streaminfo.c) and posted as LG's "video-info" application message, which
 *     libpf's Pipeline::handleBusApplication reads with exactly these GValue types. It is one of
 *     the three conditions before the media pipeline reports "playing".
 *
 * The window rectangle does not pass through here: WebKit sends it to tv.display, and tvservice
 * sets it on the video scaler, whose HAL (guest/layers/soc/src/libhal_lg115x) forwards it to the
 * host viewer. */
#include <gst/gst.h>
#include <gst/base/gstbasesink.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <syslog.h>
#include <unistd.h>
#include "props.h"
#include "streaminfo.h"
#include "mediaport.h"
#include "esdec.h"

GST_DEBUG_CATEGORY_STATIC(vdecsink_debug);
#define GST_CAT_DEFAULT vdecsink_debug

/* LG's sink_%d template caps, verbatim */
#define VDEC_CAPS                                                                                  \
    "video/x-divx;video/x-h263;video/x-h264;video/x-h265;video/x-hevc;"                            \
    "video/x-intel-h263;video/mpeg, systemstream = (boolean)FALSE;video/x-wmv,wmvversion = "       \
    "(int)3;"                                                                                      \
    "video/x-msmpeg;video/x-pn-realvideo,rmversion = (int) { 3,4 };video/x-svq,svqversion = "      \
    "(int) { 1,3 };"                                                                               \
    "video/x-ffv;video/x-3ivx;video/x-vp8;video/x-xvid;video/x-flash-video;image/jpeg;video/"      \
    "x-raw;"                                                                                       \
    "audio/mpeg;audio/x-ac3;audio/x-eac3;audio/x-dts;audio/x-raw;video/x-fd;audio/x-fd"

/* 3D_Type: libplayerAPIs maps it to videoInfo.mode3D with a table where 0 is "check_board" and
 * 11 is "2d" (StarfishMediaAPIs, the std::map built next to isLoadCompleted). */
#define VIDEO_2D 11u

static GstStaticPadTemplate sink_template =
    GST_STATIC_PAD_TEMPLATE("sink_%d", GST_PAD_SINK, GST_PAD_REQUEST, GST_STATIC_CAPS(VDEC_CAPS));

/* Order and types of GstLXVdecBin's class_init. */
enum {
    P_LOW_DELAY = 1,
    P_DECODED_SIZE,
    P_CURRENT_PTS,
    P_THUMBNAIL_MODE,
    P_ENLARGE_MAX_LATENESS,
    P_INTERLEAVING_TYPE,
    P_UNDECODED_SIZE,
    P_LIPSYNC_OFFSET,
    P_CONSTANT_DELAY,
    P_VDEC_CH,
    P_WINDOW_ID,
    P_WINDOW_WIDTH,
    P_WINDOW_HEIGHT,
    P_WINDOW_POSX,
    P_WINDOW_POSY,
    P_LOW_LATENCY_TEMP,
    P_IS_SVP,
    P_VSINK_ASYNC_HANDLING,
    P_VSINK_SYNC_ON_CLOCK,
    P_APP_TYPE,
    P_STREAM_WIDTH,
    P_STREAM_HEIGHT,
    P_FRAMERATE_NUM,
    P_FRAMERATE_DENOM,
    P_DISH_TRICK,
    P_SVP_MEM_SIZE,
    P_SVP_ALIGN_SIZE,
    P_SVP_BASEADDR,
    P_SVP_READ_PTR,
    P_SVP_WRITE_PTR,
    P_SKIP_SEGMENT,
    N_PROPS = P_SKIP_SEGMENT
};
static const struct prop_def props[N_PROPS] = {
    {"low-delay", "VDEC low delay", G_TYPE_UINT, 0},
    {"decoded-size", "Decoded Data Length", G_TYPE_UINT64, 0},
    {"current-pts", "Get current PTS", G_TYPE_UINT64, 0},
    {"thumbnail-mode", "thumbnail generation mode", G_TYPE_BOOLEAN, 0},
    {"enlarge-max-lateness", "Enable check PTS for WiDi", G_TYPE_BOOLEAN, 0},
    {"interleaving-type", "support vudu 3D", G_TYPE_INT, 0},
    {"undecoded-size", "amount of undecoded data in CPB(bytes)", G_TYPE_UINT64, 0},
    {"lipsync-offset", "Lipsync offset(msec) of video", G_TYPE_UINT, 0},
    {"constant-delay", "constant delay size in Mirracast", G_TYPE_UINT, 0},
    {"vdec-ch", "VDEC channel", G_TYPE_INT, 0},
    {"window-id", "DE window ID", G_TYPE_INT, 0},
    {"window-width", "DE window width", G_TYPE_INT, 0},
    {"window-height", "DE window height", G_TYPE_INT, 0},
    {"window-posx", "DE window x-coordinate position", G_TYPE_INT, 0},
    {"window-posy", "DE window y-coordinate position", G_TYPE_INT, 0},
    {"low-latency-temp", "low latency flag", G_TYPE_INT, 0},
    {"is-svp", "Secure Video Path", G_TYPE_BOOLEAN, 0},
    {"vsink-async-handling", "Asynchoronous state changes mode", G_TYPE_BOOLEAN, 1},
    {"vsink-sync-on-clock", "synchoronize on the clock mode", G_TYPE_BOOLEAN, 1},
    {"app-type", "Set application type", G_TYPE_STRING, 0},
    {"stream-width", "stream-width", G_TYPE_INT, 0},
    {"stream-height", "stream-height", G_TYPE_INT, 0},
    {"framerate-num", "framerate-num", G_TYPE_INT, 0},
    {"framerate-denom", "framerate-denom", G_TYPE_INT, 0},
    {"dish-trick", "dish-trick", G_TYPE_BOOLEAN, 0},
    {"svp-mem-size", "Total Memory Size of CPB", G_TYPE_UINT, 0},
    {"svp-align-size", "Alienment Size", G_TYPE_UINT, 0},
    {"svp-baseaddr", "base address of secured CPB", G_TYPE_UINT, 0},
    {"svp-read-ptr", "current read pointer of secured CPB ", G_TYPE_UINT, 0},
    {"svp-write-ptr", "current write pointer of secured CPB ", G_TYPE_UINT, 0},
    {"skip-segment", "segment skip flag for Netflix audio track change", G_TYPE_BOOLEAN, 0},
};

typedef struct {
    GstBin parent;
    GValue values[N_PROPS];
    guint next_pad;
} GstVdecSink;
typedef struct {
    GstBinClass parent_class;
    GstBuffer *(*convert_frame)(GstVdecSink *self, GstCaps *caps);
    void (*video_underrun)(GstVdecSink *self);
} GstVdecSinkClass;
enum { SIGNAL_CONVERT_FRAME, SIGNAL_VIDEO_UNDERRUN, N_SIGNALS };
static guint signals[N_SIGNALS];

/* ---- internal sink: one per sink_%d pad ---- */

typedef struct {
    GstBaseSink parent;
    GstVdecSink *bin; /* not a reference: the bin owns this sink */
    struct stream_info info;
    gboolean posted; /* video-info sent for the current caps */
    struct media_port port;
    guint pad_number;
    GstBuffer *prerolled;     /* sent from preroll(); render() gets it again */
    GstBuffer *before;        /* outside the segment by the probe's reckoning: decode-only unless
                               * preroll() or render() gets it (with the object lock) */
    struct es_starve *starve; /* the bin's video-underrun when no frame follows the last one due */
} VdecStub;
typedef struct {
    GstBaseSinkClass parent_class;
} VdecStubClass;
static GType vdec_stub_get_type(void);
G_DEFINE_TYPE(VdecStub, vdec_stub, GST_TYPE_BASE_SINK)

static void post_video_info(VdecStub *sink)
{
    struct stream_info *i = &sink->info;
    GstVdecSink *bin = sink->bin;
    guint ch = (guint)g_value_get_int(&bin->values[P_VDEC_CH - 1]);
    GstStructure *s = gst_structure_new(
        "video-info", "vdec-ch", G_TYPE_UINT, ch, "codec", G_TYPE_STRING, i->codec, "width",
        G_TYPE_UINT, i->width, "height", G_TYPE_UINT, i->height, "framerate", GST_TYPE_FRACTION,
        i->fps_n, i->fps_n > 0 ? i->fps_d : 1, "3D_Type", G_TYPE_UINT, VIDEO_2D, "PAR_Width",
        G_TYPE_UINT, i->par_n, "PAR_Height", G_TYPE_UINT, i->par_d, "Scan_Type", G_TYPE_INT,
        i->scan_type, "LR_Type", G_TYPE_INT, 0, "crop_left", G_TYPE_UINT, i->crop_left,
        "crop_rigth", G_TYPE_UINT, i->crop_right, /* sic, as libpf and lxvideosink spell it */
        "crop_top", G_TYPE_UINT, i->crop_top, "crop_bottom", G_TYPE_UINT, i->crop_bottom, NULL);
    GST_INFO_OBJECT(bin, "video-info: vdec-ch %u %s %ux%u %d/%d par %u:%u scan %d", ch, i->codec,
                    i->width, i->height, i->fps_n, i->fps_d, i->par_n, i->par_d, i->scan_type);
    syslog(LOG_INFO,
           "vdecsink-stub: video-info vdec-ch=%u codec=%s %ux%u framerate=%d/%d par=%u:%u scan=%d",
           ch, i->codec, i->width, i->height, i->fps_n, i->fps_d, i->par_n, i->par_d, i->scan_type);
    gst_element_post_message(GST_ELEMENT(bin), gst_message_new_application(GST_OBJECT(bin), s));
    sink->posted = TRUE;
}

/* LG's vdecsink (gst_lxvdecbin_probe_chip) sends HEVC to an H13 board's UHD companion (D14) when
 * KADP_UVD_ChipVersion finds one, which on H13 reads /tmp/uvd_chip_version (the soc layer's,
 * the board's companion); its pictures then enter the H13 on the HDMI receiver, port 0, where
 * tvservice connects the window (the pipeline registers HEVC_VDEC + VHDMIRX). */
#define UVD_CHIP_VERSION "/tmp/uvd_chip_version"
#define VSC_INPUT_HDMI   2
static gboolean companion(void)
{
    static int found = -1;
    if (found < 0) {
        char buf[32] = "";
        FILE *f = fopen(UVD_CHIP_VERSION, "r");
        found = f && fgets(buf, sizeof buf, f) && strtol(buf, NULL, 10) != 0;
        if (f)
            fclose(f);
    }
    return found > 0;
}

static guint32 output_of(GstCaps *caps)
{
    const GstStructure *st = gst_caps_get_size(caps) ? gst_caps_get_structure(caps, 0) : NULL;
    if (st &&
        (gst_structure_has_name(st, "video/x-h265") ||
         gst_structure_has_name(st, "video/x-hevc")) &&
        companion())
        return WEBOS_MEDIA_OUTPUT(VSC_INPUT_HDMI, 0);
    return 0;
}

static gboolean vdec_stub_set_caps(GstBaseSink *base, GstCaps *caps)
{
    VdecStub *sink = (VdecStub *)base;
    struct stream_info info;
    gboolean complete = stream_info_from_caps(&info, caps);
    GST_DEBUG_OBJECT(sink, "caps %" GST_PTR_FORMAT, caps);
    media_port_set_output(&sink->port, output_of(caps));
    media_port_caps(&sink->port, GST_ELEMENT(base), caps);
    if (sink->posted && !memcmp(&info, &sink->info, sizeof info))
        return TRUE;
    sink->info = info;
    sink->posted = FALSE;
    /* An H.264 stream whose caps lack size or rate waits for the SPS in the first buffer. */
    if (info.video && (complete || !info.h264))
        post_video_info(sink);
    return TRUE;
}

static void inspect(VdecStub *sink, GstBuffer *buf)
{
    if (sink->posted || !sink->info.video)
        return;
    GstMapInfo map;
    if (sink->info.h264 && gst_buffer_map(buf, &map, GST_MAP_READ)) {
        stream_info_from_annexb(&sink->info, map.data, map.size);
        gst_buffer_unmap(buf, &map);
    }
    /* Post what is known by the first buffer, whatever is still missing. */
    post_video_info(sink);
}

/* The probe's out-of-segment buffer: preroll() or render() got it after all (sent there, once). */
static void vdec_stub_take_before(VdecStub *sink, GstBuffer *buf)
{
    GST_OBJECT_LOCK(sink);
    if (sink->before == buf)
        gst_buffer_replace(&sink->before, NULL);
    GST_OBJECT_UNLOCK(sink);
}

/* ... or GstBaseSink dropped it: decode-only, before whatever follows it. */
static void vdec_stub_send_before(VdecStub *sink)
{
    GstBuffer *buf;
    GST_OBJECT_LOCK(sink);
    buf = sink->before;
    sink->before = NULL;
    GST_OBJECT_UNLOCK(sink);
    if (buf) {
        media_port_buffer(&sink->port, GST_BASE_SINK(sink), buf, WEBOS_MEDIA_DECODE_ONLY);
        gst_buffer_unref(buf);
    }
}

static void vdec_stub_drop_before(VdecStub *sink)
{
    GST_OBJECT_LOCK(sink);
    gst_buffer_replace(&sink->before, NULL);
    GST_OBJECT_UNLOCK(sink);
}

static GstFlowReturn vdec_stub_preroll(GstBaseSink *base, GstBuffer *buf)
{
    VdecStub *sink = (VdecStub *)base;
    vdec_stub_take_before(sink, buf);
    inspect(sink, buf);
    /* The host shows the prerolled frame at once (a paused pipeline's picture, e.g. after a
     * seek); render() is called with the same buffer when PLAYING and does not send it again. */
    media_port_buffer(&sink->port, base, buf, 0);
    gst_buffer_replace(&sink->prerolled, buf);
    return GST_FLOW_OK;
}

static GstFlowReturn vdec_stub_render(GstBaseSink *base, GstBuffer *buf)
{
    VdecStub *sink = (VdecStub *)base;
    vdec_stub_take_before(sink, buf);
    inspect(sink, buf);
    if (sink->prerolled == buf)
        gst_buffer_replace(&sink->prerolled, NULL);
    else
        media_port_buffer(&sink->port, base, buf, 0);
    es_starve_arm(sink->starve, &base->segment, GST_BUFFER_PTS(buf));
    return GST_FLOW_OK; /* rendered WEBOS_MEDIA_LEAD_NS early: the host shows it on time */
}

/* Buffers GstBaseSink drops as outside the segment: the frames from the keyframe before an
 * accurate seek's target, which the decoder needs. The probe sees each buffer first and keeps the
 * ones its clip (by DTS, else PTS) puts outside; one that preroll() or render() then gets after all
 * is sent there, once, and one GstBaseSink did drop goes as decode-only before whatever follows it.
 * Sending it from the probe as well sent a frame twice whenever the two disagreed, the first
 * frames of an MP4 with B-frames (DTS before the segment, PTS in it). */
static GstPadProbeReturn vdec_stub_probe(GstPad *pad, GstPadProbeInfo *info, gpointer data)
{
    VdecStub *sink = data;
    GstBaseSink *base = data;
    GstBuffer *buf = GST_PAD_PROBE_INFO_BUFFER(info);
    GstClockTime start, end = GST_CLOCK_TIME_NONE;
    GstSegment segment;
    if (!buf)
        return GST_PAD_PROBE_OK;
    vdec_stub_send_before(sink);
    start = GST_BUFFER_DTS(buf);
    if (!GST_CLOCK_TIME_IS_VALID(start))
        start = GST_BUFFER_PTS(buf);
    if (GST_CLOCK_TIME_IS_VALID(start) && GST_BUFFER_DURATION_IS_VALID(buf))
        end = start + GST_BUFFER_DURATION(buf);
    GST_OBJECT_LOCK(base);
    segment = base->segment;
    GST_OBJECT_UNLOCK(base);
    if (segment.format == GST_FORMAT_TIME && GST_CLOCK_TIME_IS_VALID(start) &&
        !gst_segment_clip(&segment, GST_FORMAT_TIME, start, end, NULL, NULL)) {
        GST_OBJECT_LOCK(sink);
        gst_buffer_replace(&sink->before, buf);
        GST_OBJECT_UNLOCK(sink);
    }
    return GST_PAD_PROBE_OK;
}

static gboolean vdec_stub_start(GstBaseSink *base)
{
    VdecStub *sink = (VdecStub *)base;
    gint ch;
    GST_OBJECT_LOCK(sink->bin);
    ch = g_value_get_int(&sink->bin->values[P_VDEC_CH - 1]);
    GST_OBJECT_UNLOCK(sink->bin);
    media_port_start(&sink->port, GST_ELEMENT(base), WEBOS_MEDIA_VIDEO, ch, sink->pad_number);
    return TRUE;
}

static gboolean vdec_stub_stop(GstBaseSink *base)
{
    VdecStub *sink = (VdecStub *)base;
    media_port_stop(&sink->port, GST_ELEMENT(base));
    gst_buffer_replace(&sink->prerolled, NULL);
    vdec_stub_drop_before(sink);
    sink->posted = FALSE;
    es_starve_reset(sink->starve, FALSE);
    return TRUE;
}

static gboolean vdec_stub_unlock(GstBaseSink *base)
{
    media_port_set_flushing(&((VdecStub *)base)->port, TRUE);
    return TRUE;
}

static gboolean vdec_stub_unlock_stop(GstBaseSink *base)
{
    media_port_set_flushing(&((VdecStub *)base)->port, FALSE);
    return TRUE;
}

static gboolean vdec_stub_event(GstBaseSink *base, GstEvent *event)
{
    VdecStub *sink = (VdecStub *)base;
    if (GST_EVENT_TYPE(event) == GST_EVENT_FLUSH_START) {
        vdec_stub_drop_before(sink);
        media_port_event(&sink->port, GST_ELEMENT(base), WEBOS_MEDIA_FLUSH);
        gst_buffer_replace(&sink->prerolled, NULL);
        es_starve_reset(sink->starve, FALSE);
    } else if (GST_EVENT_IS_SERIALIZED(event)) {
        vdec_stub_send_before(sink);
    }
    return GST_BASE_SINK_CLASS(vdec_stub_parent_class)->event(base, event);
}

/* EOS waits for the end of the last frame at its own time, not WEBOS_MEDIA_LEAD_NS early, then
 * tells the host. */
static GstFlowReturn vdec_stub_wait_event(GstBaseSink *base, GstEvent *event)
{
    VdecStub *sink = (VdecStub *)base;
    GstFlowReturn ret;
    vdec_stub_send_before(sink);
    if (GST_EVENT_TYPE(event) != GST_EVENT_EOS)
        return GST_BASE_SINK_CLASS(vdec_stub_parent_class)->wait_event(base, event);
    es_starve_reset(sink->starve, TRUE);
    media_lead_suspend(&sink->port, base, TRUE);
    ret = GST_BASE_SINK_CLASS(vdec_stub_parent_class)->wait_event(base, event);
    media_lead_suspend(&sink->port, base, FALSE);
    if (ret == GST_FLOW_OK)
        media_port_event(&sink->port, GST_ELEMENT(base), WEBOS_MEDIA_EOS);
    return ret;
}

static GstStateChangeReturn vdec_stub_change_state(GstElement *element, GstStateChange transition)
{
    VdecStub *sink = (VdecStub *)element;
    if (transition == GST_STATE_CHANGE_PAUSED_TO_PLAYING)
        media_port_state(&sink->port, element, TRUE);
    else if (transition == GST_STATE_CHANGE_PLAYING_TO_PAUSED)
        media_port_state(&sink->port, element, FALSE);
    return GST_ELEMENT_CLASS(vdec_stub_parent_class)->change_state(element, transition);
}

static void vdec_stub_finalize(GObject *obj)
{
    VdecStub *sink = (VdecStub *)obj;
    gst_buffer_replace(&sink->prerolled, NULL);
    gst_buffer_replace(&sink->before, NULL);
    media_port_clear(&sink->port);
    es_starve_free(sink->starve);
    G_OBJECT_CLASS(vdec_stub_parent_class)->finalize(obj);
}

/* ts-offset: the pipeline's, the lead apart (mediaport.h) */
enum { STUB_PROP_TS_OFFSET = 1 };

static void vdec_stub_set_property(GObject *obj, guint id, const GValue *value, GParamSpec *spec)
{
    if (id == STUB_PROP_TS_OFFSET)
        media_lead_set(&((VdecStub *)obj)->port, GST_BASE_SINK(obj), value);
    else
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
}

static void vdec_stub_get_property(GObject *obj, guint id, GValue *value, GParamSpec *spec)
{
    if (id == STUB_PROP_TS_OFFSET)
        media_lead_get(&((VdecStub *)obj)->port, value);
    else
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
}

static void vdec_stub_class_init(VdecStubClass *klass)
{
    GstBaseSinkClass *base = GST_BASE_SINK_CLASS(klass);
    G_OBJECT_CLASS(klass)->finalize = vdec_stub_finalize;
    G_OBJECT_CLASS(klass)->set_property = vdec_stub_set_property;
    G_OBJECT_CLASS(klass)->get_property = vdec_stub_get_property;
    media_lead_install(G_OBJECT_CLASS(klass), STUB_PROP_TS_OFFSET);
    GST_ELEMENT_CLASS(klass)->change_state = vdec_stub_change_state;
    gst_element_class_add_pad_template(GST_ELEMENT_CLASS(klass),
                                       gst_pad_template_new("sink", GST_PAD_SINK, GST_PAD_ALWAYS,
                                                            gst_caps_from_string(VDEC_CAPS)));
    gst_element_class_set_static_metadata(GST_ELEMENT_CLASS(klass), "vdecsink output", "Sink/Video",
                                          "Sends video to the host viewer in real time (emulator)",
                                          "Beanjar");
    base->set_caps = vdec_stub_set_caps;
    base->preroll = vdec_stub_preroll;
    base->render = vdec_stub_render;
    base->start = vdec_stub_start;
    base->stop = vdec_stub_stop;
    base->unlock = vdec_stub_unlock;
    base->unlock_stop = vdec_stub_unlock_stop;
    base->event = vdec_stub_event;
    base->wait_event = vdec_stub_wait_event;
}

/* LG's vdecsink reports video-underrun when the decoder runs dry: here, no frame after the last
 * one due for ES_STARVE_NS. */
static void vdec_stub_starved(GstElement *element)
{
    VdecStub *sink = (VdecStub *)element;
    GST_INFO_OBJECT(element, "no frame since the last one due: video-underrun");
    if (sink->bin)
        g_signal_emit(sink->bin, signals[SIGNAL_VIDEO_UNDERRUN], 0);
}

static void vdec_stub_init(VdecStub *sink)
{
    sink->starve = es_starve_new(GST_ELEMENT(sink), vdec_stub_starved);
    media_port_init(&sink->port);
    gst_base_sink_set_sync(GST_BASE_SINK(sink), TRUE);
    media_lead_init(&sink->port, GST_BASE_SINK(sink));
    /* A display drops late frames; with no QoS upstream this only keeps render cheap. */
    gst_base_sink_set_qos_enabled(GST_BASE_SINK(sink), FALSE);
}

/* ---- the bin ---- */

static GType gst_vdec_sink_get_type(void);
G_DEFINE_TYPE(GstVdecSink, gst_vdec_sink, GST_TYPE_BIN)

static void gst_vdec_sink_set_property(GObject *obj, guint id, const GValue *value,
                                       GParamSpec *spec)
{
    GstVdecSink *self = (GstVdecSink *)obj;
    if (id < 1 || id > N_PROPS) {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
        return;
    }
    GST_OBJECT_LOCK(self);
    g_value_copy(value, &self->values[id - 1]);
    GST_OBJECT_UNLOCK(self);
    GST_DEBUG_OBJECT(self, "%s set", spec->name);
}

static void gst_vdec_sink_get_property(GObject *obj, guint id, GValue *value, GParamSpec *spec)
{
    GstVdecSink *self = (GstVdecSink *)obj;
    if (id < 1 || id > N_PROPS) {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
        return;
    }
    switch (id) {
    case P_CURRENT_PTS: {
        /* The stream time being shown: lpsink answers position queries with it at |rate| >= 4.
         * The host shows the frame due on the pipeline clock, which is the sink's position. */
        gint64 pts = 0;
        GstElement *video = NULL;
        GST_OBJECT_LOCK(self);
        for (GList *l = GST_BIN_CHILDREN(self); l; l = l->next)
            if (((VdecStub *)l->data)->info.video)
                video = gst_object_ref(l->data);
        GST_OBJECT_UNLOCK(self);
        if (video) {
            if (!gst_element_query_position(video, GST_FORMAT_TIME, &pts) || pts < 0)
                pts = 0;
            gst_object_unref(video);
        }
        g_value_set_uint64(value, (guint64)pts);
        return;
    }
    case P_DECODED_SIZE:
    case P_UNDECODED_SIZE:
        g_value_set_uint64(value, 0); /* nothing is ever queued in a decoder */
        return;
    default:
        GST_OBJECT_LOCK(self);
        g_value_copy(&self->values[id - 1], value);
        GST_OBJECT_UNLOCK(self);
    }
}

static GstPad *gst_vdec_sink_request_new_pad(GstElement *element, GstPadTemplate *templ,
                                             const gchar *name, const GstCaps *caps)
{
    GstVdecSink *self = (GstVdecSink *)element;
    gchar *padname;
    GST_OBJECT_LOCK(self);
    padname = name ? g_strdup(name) : g_strdup_printf("sink_%u", self->next_pad);
    self->next_pad++;
    GST_OBJECT_UNLOCK(self);

    VdecStub *sink = g_object_new(vdec_stub_get_type(), NULL);
    gchar *sinkname = g_strdup_printf("%s-out", padname);
    gst_object_set_name(GST_OBJECT(sink), sinkname);
    g_free(sinkname);
    sink->bin = self;
    sink->pad_number = self->next_pad - 1;
    if (!gst_bin_add(GST_BIN(self), GST_ELEMENT(sink))) {
        gst_object_unref(sink);
        g_free(padname);
        return NULL;
    }
    GstPad *target = gst_element_get_static_pad(GST_ELEMENT(sink), "sink");
    gst_pad_add_probe(target, GST_PAD_PROBE_TYPE_BUFFER, vdec_stub_probe, sink, NULL);
    GstPad *ghost = gst_ghost_pad_new_from_template(padname, target, templ);
    gst_object_unref(target);
    g_free(padname);
    g_object_set_data(G_OBJECT(ghost), "vdecsink-output", sink);
    if (GST_STATE(self) > GST_STATE_NULL)
        gst_pad_set_active(ghost, TRUE);
    gst_element_add_pad(element, ghost);
    gst_element_sync_state_with_parent(GST_ELEMENT(sink));
    GST_INFO_OBJECT(self, "requested pad %s:%s", GST_DEBUG_PAD_NAME(ghost));
    /* which process builds the pipeline (starfish-media-pipeline for files; the web process
     * itself for WebKit's in-process MSE pipeline) */
    syslog(LOG_INFO, "vdecsink-stub: %s pad %s (pid %d)", GST_OBJECT_NAME(self),
           GST_PAD_NAME(ghost), (int)getpid());
    return ghost;
}

static void gst_vdec_sink_release_pad(GstElement *element, GstPad *pad)
{
    GstVdecSink *self = (GstVdecSink *)element;
    GstElement *sink = g_object_get_data(G_OBJECT(pad), "vdecsink-output");
    GST_INFO_OBJECT(self, "releasing pad %s:%s", GST_DEBUG_PAD_NAME(pad));
    gst_object_ref(pad);
    gst_pad_set_active(pad, FALSE);
    gst_element_remove_pad(element, pad);
    if (sink) {
        gst_element_set_locked_state(sink, TRUE);
        gst_element_set_state(sink, GST_STATE_NULL);
        gst_bin_remove(GST_BIN(self), sink);
    }
    gst_object_unref(pad);
}

/* Action signal: a snapshot of the current frame. There is no decoded picture. */
static GstBuffer *gst_vdec_sink_convert_frame(GstVdecSink *self, GstCaps *caps)
{
    GST_INFO_OBJECT(self, "convert-frame: the decoded frames are on the host");
    return NULL;
}

static void gst_vdec_sink_finalize(GObject *obj)
{
    props_clear(((GstVdecSink *)obj)->values, N_PROPS);
    G_OBJECT_CLASS(gst_vdec_sink_parent_class)->finalize(obj);
}

static void gst_vdec_sink_class_init(GstVdecSinkClass *klass)
{
    GObjectClass *gobject = G_OBJECT_CLASS(klass);
    GstElementClass *element = GST_ELEMENT_CLASS(klass);
    gobject->set_property = gst_vdec_sink_set_property;
    gobject->get_property = gst_vdec_sink_get_property;
    gobject->finalize = gst_vdec_sink_finalize;
    props_install(gobject, props, N_PROPS);
    klass->convert_frame = gst_vdec_sink_convert_frame;
    /* As registered by GstLXVdecBin: convert-frame is RUN_LAST|ACTION, GstBuffer (GstCaps);
     * video-underrun RUN_FIRST, no arguments. */
    signals[SIGNAL_CONVERT_FRAME] =
        g_signal_new("convert-frame", G_TYPE_FROM_CLASS(klass), G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION,
                     G_STRUCT_OFFSET(GstVdecSinkClass, convert_frame), NULL, NULL,
                     g_cclosure_marshal_generic, GST_TYPE_BUFFER, 1, GST_TYPE_CAPS);
    signals[SIGNAL_VIDEO_UNDERRUN] =
        g_signal_new("video-underrun", G_TYPE_FROM_CLASS(klass), G_SIGNAL_RUN_FIRST,
                     G_STRUCT_OFFSET(GstVdecSinkClass, video_underrun), NULL, NULL,
                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    gst_element_class_add_pad_template(element, gst_static_pad_template_get(&sink_template));
    gst_element_class_set_static_metadata(
        element, "LXVdecBin", "Sink/Image",
        "LG's vdec sink bin for the emulator: stream info, and the stream to the host viewer",
        "Beanjar");
    element->request_new_pad = gst_vdec_sink_request_new_pad;
    element->release_pad = gst_vdec_sink_release_pad;
}

static void gst_vdec_sink_init(GstVdecSink *self)
{
    props_init(self->values, props, N_PROPS);
}

static gboolean plugin_init(GstPlugin *plugin)
{
    GST_DEBUG_CATEGORY_INIT(vdecsink_debug, "vdecsink", 0, "vdecsink stub");
    return gst_element_register(plugin, "vdecsink", GST_RANK_PRIMARY, gst_vdec_sink_get_type());
}

#define PACKAGE "beanjar"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, vdecsink,
                  "LG's vdecsink for the emulator (video milestone 2)", plugin_init, "1.0", "LGPL",
                  PACKAGE, "Beanjar")
