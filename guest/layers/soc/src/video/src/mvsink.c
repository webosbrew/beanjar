// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* "mvsink": MStar's GSTMVSink (libgstmvsink.so, W15U, W16R, W18R and LM21's W21A..W23A) for the
 * emulator. W15U's is W16R's with constant-delay 1 by default.
 *
 * The stock sink takes the gst-omx decoders' frame handles (video/x-raw, vendor=mstar: physical
 * luma/chroma addresses, not pixels) and displays them through MSIL on the MVOP and the XC
 * scaler; it is gstcool.conf's default video sink (playbin's playsink makes it) and the video
 * sink libpf's MSE pipeline picks by rank on W16R. Here the decoders (omx.c) pass the compressed
 * stream through under raw caps that name the stream's own (esdec.c), and this sink sends it to
 * the host (essink.c), on the channel of resource-info's video port. The scaler window is not
 * the sink's: tvservice sets it through the stock HAL and KADP, and mstar-utopia.ko passes it on
 * (MApi_XC_SetWindow).
 *
 * The pipeline reports the video loaded once the sink has posted LG's "media-info" application
 * message, which libpf's Pipeline::handleBusApplication takes by its "type" (1: video) to the
 * stream's track (by "stream-id"); the fields and their types are the stock builder's. */
#include <gst/gst.h>
#include <string.h>
#include <syslog.h>
#include "props.h"
#include "essink.h"

GST_DEBUG_CATEGORY_STATIC(mvsink_debug);
#define GST_CAT_DEFAULT mvsink_debug

/* The stock template: any raw video (the sink then checks vendor=mstar) */
static GstStaticPadTemplate sink_template =
    GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS("video/x-raw"));

/* 3D_Type's "2d" in libplayerAPIs' table: 11 from W16R on; W15U's numbering has 0 for "2d" (and 11
 * for frame_squential, which turns tvservice's 3D on), the stock sink's value for a 2D stream. */
#if MSTAR_REL < 16
#define VIDEO_2D 0u
#else
#define VIDEO_2D 11u
#endif

/* The stock "input" property's enum */
static GType mvsink_mode_get_type(void)
{
    static gsize type;
    static const GEnumValue values[] = {
        {0, "DRAM", "dram"}, {1, "H264", "H264"}, {2, "MVD", "mvd"},          {3, "RVD", "rvd"},
        {4, "CLIP", "clip"}, {5, "JPD", "jpd"},   {-1, "UNKNOWN", "unknown"}, {0, NULL, NULL},
    };
    if (g_once_init_enter(&type))
        g_once_init_leave(&type, g_enum_register_static("GstMVSinkMode", values));
    return type;
}

#define R G_PARAM_READABLE
#define W G_PARAM_WRITABLE
/* As gst-inspect-1.0 lists GSTMVSink's after GstBaseSink's: GstVideoSink's show-preroll-frame,
 * then MStar's (render-delay is its own, in microseconds, in place of GstBaseSink's; its
 * max-lateness, listed among them, is GstBaseSink's here). */
enum {
    P_SHOW_PREROLL_FRAME = 1,
    P_INPUT,
    P_DISP_X,
    P_DISP_Y,
    P_DISP_W,
    P_DISP_H,
    P_TVMODE,
    P_PLANE,
    P_RECTANGLE,
    P_FLUSH_REPEAT_FRAME,
    P_CURRENT_PTS,
    P_INTER_FRAME_DELAY,
    P_SLOW_MODE_RATE,
    P_CONTENTFRAMERATE,
    P_STEPFRAME,
    P_MUTE,
    P_THUMBNAIL_MODE,
    P_WINDOW_ID,
    P_SEAMLESS_PLAY,
    P_APP_TYPE,
    P_LOW_DELAY,
    P_CONSTANT_DELAY,
    P_VSINK_ASYNC_HANDLING,
    P_VSINK_SYNC_ON_CLOCK,
    P_INTERLEAVING_TYPE,
    P_RESOURCE_INFO,
    P_RENDER_DELAY,
#if MSTAR_REL >= 18
    P_RENDERED_FRAME,
    P_DROPPED_FRAMES,
#endif
#if MSTAR_REL >= 21
    P_BASE_PLAYBACK_THRESHOLD,
    P_CLOCK_SOURCE,
    P_DM_FRAME_DROP_THRESHOLD,
    P_DM_RENDER_BUFFER_LENGTH,
    P_DISPLAY_PREROLL,
    P_NF_DISPLAYED_FRAMES,
    P_NF_DROPPED_FRAMES,
    P_PREROLL_PTS,
#endif
#if MSTAR_REL == 22
    P_DISPLAY_PTS,
    P_UNDERRUN_THRESHOLD,
#endif
    P_END,
    N_PROPS = P_END - 1
};
static const struct prop_def props[N_PROPS] = {
    {"show-preroll-frame", "Whether to render video frames during preroll", G_TYPE_BOOLEAN, 1},
    {"input", "Input Select for MVOP", G_TYPE_ENUM, -1, NULL, 0, mvsink_mode_get_type},
    {"disp-x", "X position for display window ", G_TYPE_UINT, 0},
    {"disp-y", "Y position for display window ", G_TYPE_UINT, 0},
    {"disp-w", "Width for display window ", G_TYPE_UINT, 0},
    {"disp-h", "Height for display window ", G_TYPE_UINT, 0},
    {"tvmode", "Define the television mode", G_TYPE_UINT, 0},
    {"plane", "Define the Pixel Plane to be used", G_TYPE_UINT, 0},
    {"rectangle", "The destination rectangle", PROP_UINT_ARRAY, 0},
    {"flush-repeat-frame", "Keep displaying the last frame", G_TYPE_BOOLEAN, 0},
    {"current-pts", "Get the last presented PTS value", G_TYPE_UINT64, 0, NULL, R},
    {"inter-frame-delay", "Enables fixed frame rate mode", G_TYPE_UINT64, 0, NULL, W},
    {"slow-mode-rate", "Define the television mode", G_TYPE_UINT, 0, NULL, W},
    {"contentframerate", "Get the content frame rate", G_TYPE_UINT, 0, NULL, R},
    {"stepframe", "Step frame one by one", G_TYPE_UINT, 0, NULL, W},
    {"mute", "Mute screen", G_TYPE_BOOLEAN, 0},
    {"thumbnail-mode", "thumbnail generation mode", G_TYPE_BOOLEAN, 0, NULL, W},
    {"window-id", "Get window id (stream id)", G_TYPE_UINT, 1, NULL, W},
    {"seamless-play", "seamless-play", G_TYPE_BOOLEAN, 0},
    {"app-type", "Set app type", G_TYPE_STRING, 0},
    {"low-delay", "Define low-delay", G_TYPE_UINT, 0},
#if MSTAR_REL >= 21 || MSTAR_REL < 16
    {"constant-delay", "Define constant-delay", G_TYPE_UINT, 1},
#else
    {"constant-delay", "Define constant-delay", G_TYPE_UINT, 4},
#endif
    {"vsink-async-handling", "vsink-async-handling", G_TYPE_BOOLEAN, 0, NULL, W},
    {"vsink-sync-on-clock", "vsink-sync-on-clock", G_TYPE_BOOLEAN, 0, NULL, W},
    {"interleaving-type", "define interleaving type", G_TYPE_INT, 0},
    {"resource-info", "Hold various information for managing resource", PROP_STRUCTURE, 0, NULL, W},
    {"render-delay", "set render delay to basesink, unit is microsecond", G_TYPE_ULONG, 0},
#if MSTAR_REL >= 18
    {"rendered-frame", "the accumulated number of rendered frames", G_TYPE_INT, 0, NULL, R},
    {"dropped-frames", "Get number of dropped frames", G_TYPE_UINT, 0, NULL, R},
#endif
#if MSTAR_REL >= 21
    {"base-playback-threshold", "R2R Base playback threshold", G_TYPE_INT64, 0},
    {"clock-source", "Set clock source", G_TYPE_STRING, 0},
    {"directmedia-frame-drop-threshold", "threshold for dropping frames", G_TYPE_INT, 1, NULL, W,
     NULL, 0, G_MAXINT, TRUE},
    {"directmedia-render-buffer-length", "property for quering render buffer length", G_TYPE_INT, 0,
     NULL, R, NULL, 0, 8, TRUE},
    {"display-preroll", "show preroll after seek", G_TYPE_BOOLEAN, 0},
    {"non-flushable-displayed-frames",
     "the accumulated number of rendered frames, don't reset with flush", G_TYPE_INT, 0, NULL, R,
     NULL, 0, G_MAXINT, TRUE},
    {"non-flushable-dropped-frames", "Get number of dropped frames, don't reset with flush",
     G_TYPE_UINT, 0, NULL, R},
    {"preroll-pts", "Get the preroll PTS value", G_TYPE_UINT64, 0, NULL, R},
#endif
#if MSTAR_REL == 22
    {"display-pts", "Get the last displayed PTS value", G_TYPE_UINT64, 0, NULL, R},
    {"underrun-threshold", "Underrun threshold", G_TYPE_UINT, 0},
#endif
};

typedef struct {
    EsSink parent;
    GValue values[N_PROPS];
} GstMVSink;
typedef struct {
    EsSinkClass parent_class;
    void (*video_underrun)(GstMVSink *self);
    GstBuffer *(*convert_frame)(GstMVSink *self, GstCaps *caps);
    GstBuffer *(*last_sample)(GstMVSink *self, GstCaps *caps);
    gboolean (*render_prerolled_frame)(GstMVSink *self);
} GstMVSinkClass;

static GType gst_mvsink_get_type(void);
G_DEFINE_TYPE(GstMVSink, gst_mvsink, es_sink_get_type())

/* The stock sink's one message, as its builder makes it: stream-id, type (1: video), codec
 * strings and ints; framerate a fraction; 3D_Type, PAR_* and Scan_Type uint; W18R's adds afd
 * (uint) and hdr-type. libpf reads width and height as int. */
static void post_info(EsSink *self)
{
    struct stream_info *i = &self->info;
    gchar *stream_id = gst_pad_get_stream_id(GST_BASE_SINK_PAD(self));
    GstStructure *media = gst_structure_new(
        "media-info", "stream-id", G_TYPE_STRING, stream_id, "type", G_TYPE_INT, 1, "codec",
        G_TYPE_STRING, i->codec, "width", G_TYPE_INT, (gint)i->width, "height", G_TYPE_INT,
        (gint)i->height, "framerate", GST_TYPE_FRACTION, i->fps_n, i->fps_n > 0 ? i->fps_d : 1,
        "3D_Type", G_TYPE_UINT, VIDEO_2D, "PAR_Width", G_TYPE_UINT, i->par_n, "PAR_Height",
        G_TYPE_UINT, i->par_d, "Scan_Type", G_TYPE_UINT, (guint)i->scan_type, NULL);
#if MSTAR_REL >= 18
    gst_structure_set(media, "afd", G_TYPE_UINT, 0u, "hdr-type", G_TYPE_STRING, "none", NULL);
#endif
    syslog(LOG_INFO,
           "mvsink: media-info channel %d %s (%s) %ux%u framerate=%d/%d par=%u:%u scan=%d "
           "stream-id=%s",
           self->channel, self->mime, i->codec, i->width, i->height, i->fps_n, i->fps_d, i->par_n,
           i->par_d, i->scan_type, stream_id ? stream_id : "-");
    g_free(stream_id);
    gst_element_post_message(GST_ELEMENT(self),
                             gst_message_new_application(GST_OBJECT(self), media));
}

static void gst_mvsink_set_property(GObject *obj, guint id, const GValue *value, GParamSpec *spec)
{
    GstMVSink *self = (GstMVSink *)obj;
    if (id < 1 || id > N_PROPS) {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
        return;
    }
    GST_OBJECT_LOCK(self);
    g_value_copy(value, &self->values[id - 1]);
    GST_OBJECT_UNLOCK(self);
    if (id == P_RESOURCE_INFO)
        es_sink_resource_info(ES_SINK(self), value, "video-port");
}

static void gst_mvsink_get_property(GObject *obj, guint id, GValue *value, GParamSpec *spec)
{
    GstMVSink *self = (GstMVSink *)obj;
    if (id < 1 || id > N_PROPS) {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
        return;
    }
    switch (id) {
    case P_CURRENT_PTS:
        g_value_set_uint64(value, es_sink_position(ES_SINK(self)));
        return;
    case P_CONTENTFRAMERATE: {
        const struct stream_info *i = &ES_SINK(self)->info;
        g_value_set_uint(value,
                         i->fps_n > 0 && i->fps_d > 0 ? (guint)(i->fps_n * 1000LL / i->fps_d) : 0);
        return;
    }
#if MSTAR_REL >= 18
    case P_RENDERED_FRAME:
        g_value_set_int(value, (gint)ES_SINK(self)->rendered);
        return;
    case P_DROPPED_FRAMES:
        g_value_set_uint(value, 0);
        return;
#endif
#if MSTAR_REL >= 21
    case P_NF_DISPLAYED_FRAMES:
        g_value_set_int(value, (gint)ES_SINK(self)->rendered);
        return;
#endif
#if MSTAR_REL == 22
    case P_DISPLAY_PTS:
        g_value_set_uint64(value, es_sink_position(ES_SINK(self)));
        return;
#endif
    }
    GST_OBJECT_LOCK(self);
    g_value_copy(&self->values[id - 1], value);
    GST_OBJECT_UNLOCK(self);
}

static GstBuffer *convert_frame(GstMVSink *self, GstCaps *caps)
{
    GST_INFO_OBJECT(self, "vdec-convert-frame: the decoded frames are on the host");
    return NULL;
}

/* The frames are drawn on the host as they come: nothing held back to show. */
static gboolean render_prerolled_frame(GstMVSink *self)
{
    GST_INFO_OBJECT(self, "render-prerolled-frame: shown on the host");
    return TRUE;
}

static void gst_mvsink_finalize(GObject *obj)
{
    props_clear(((GstMVSink *)obj)->values, N_PROPS);
    G_OBJECT_CLASS(gst_mvsink_parent_class)->finalize(obj);
}

static void gst_mvsink_class_init(GstMVSinkClass *klass)
{
    GObjectClass *gobject = G_OBJECT_CLASS(klass);
    GstElementClass *element = GST_ELEMENT_CLASS(klass);
    gobject->set_property = gst_mvsink_set_property;
    gobject->get_property = gst_mvsink_get_property;
    gobject->finalize = gst_mvsink_finalize;
    props_install(gobject, props, N_PROPS);
    klass->parent_class.kind = WEBOS_MEDIA_VIDEO;
    klass->parent_class.post_info = post_info;
    klass->convert_frame = convert_frame;
    klass->last_sample = convert_frame;
    g_signal_new("last-sample", G_TYPE_FROM_CLASS(klass), G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION,
                 G_STRUCT_OFFSET(GstMVSinkClass, last_sample), NULL, NULL,
                 g_cclosure_marshal_generic, GST_TYPE_BUFFER, 1, GST_TYPE_CAPS);
    g_signal_new("vdec-convert-frame", G_TYPE_FROM_CLASS(klass),
                 G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION,
                 G_STRUCT_OFFSET(GstMVSinkClass, convert_frame), NULL, NULL,
                 g_cclosure_marshal_generic, GST_TYPE_BUFFER, 1, GST_TYPE_CAPS);
#if MSTAR_REL >= 21
    klass->render_prerolled_frame = render_prerolled_frame;
    g_signal_new("render-prerolled-frame", G_TYPE_FROM_CLASS(klass),
                 G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION,
                 G_STRUCT_OFFSET(GstMVSinkClass, render_prerolled_frame), NULL, NULL,
                 g_cclosure_marshal_generic, G_TYPE_BOOLEAN, 0);
#else
    (void)render_prerolled_frame;
#endif
    g_signal_new("video-underrun", G_TYPE_FROM_CLASS(klass), G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION,
                 G_STRUCT_OFFSET(GstMVSinkClass, video_underrun), NULL, NULL,
                 g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    gst_element_class_add_pad_template(element, gst_static_pad_template_get(&sink_template));
#if MSTAR_REL >= 21
    gst_element_class_set_static_metadata(element, "mtk video sink", "Sink/Video/Hardware",
                                          "mtk videosink", "gavin.luo <gavin.luo@mediatek.com>");
#else
    gst_element_class_set_static_metadata(element, "mstar video sink", "Sink/Video",
                                          "mstar videosink",
                                          "elendil.luo <elendil.luo@mstarsemi.com>");
#endif
}

static void gst_mvsink_init(GstMVSink *self)
{
    props_init(self->values, props, N_PROPS);
}

static gboolean plugin_init(GstPlugin *plugin)
{
    GST_DEBUG_CATEGORY_INIT(mvsink_debug, "mvsink", 0, "MStar video sink (emulator)");
    return gst_element_register(plugin, "mvsink", GST_RANK_PRIMARY + 1, gst_mvsink_get_type());
}

#define PACKAGE "beanjar"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, mvsink, "mstar video sink for the emulator",
                  plugin_init, "0.0.0.0", "LGPL", PACKAGE, "Beanjar")
