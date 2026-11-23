// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libgstomx.so: MStar's gst-omx plugin (W16R, W18R, LM21's W21A..W23A) for the emulator.
 *
 * The stock plugin wraps the OpenMAX components of libOMX.MStar.so (over libkadaptor and MStar's
 * Utopia drivers): one factory per section of /etc/xdg/gstomx.conf, named after the section, of
 * the class its type-name names, at its rank. Its video decoders hand mvsink MStar frame handles
 * (video/x-raw, vendor=mstar), its audio decoders PCM to omxaudiosink, which plays it on the audio
 * DSP and provides the pipeline clock. playbin's decodebin reaches the decoders through LG's
 * decproxy, libpf's MSE pipeline links decoder -> queue -> sink.
 *
 * Here the factories are registered the same way, from the same file, with the stock classes'
 * metadata, pad templates and properties (omxclasses.h), and nothing is decoded: the decoders
 * pass the compressed stream through under raw caps of their src template that name the stream's
 * own (esdec.c), and omxaudiosink sends what it gets to the host (essink.c); mvsink
 * (mvsink.c) does the same for video. LM21's audio decoders hand the stream on as passthrough
 * (audio/x-raw(mode:passthrough)) to omxaudiopassthroughsink, which sends it the same way.
 * MSTAR_REL is the release (15: W15U, 16: W16R, 18: W18R, 21..23: W21A..W23A). W15U's are W16R's
 * without the video decoders' displayed-frames and dropped-frames and omxaudiosink's fade and
 * fade-volume (gst-inspect-1.0 of W15U 04.06.25's stock plugin). */
#include <gst/gst.h>
#include <string.h>
#include <syslog.h>
#include "props.h"
#include "esdec.h"
#include "essink.h"
#include "streaminfo.h"
#include "omxconf.h"

GST_DEBUG_CATEGORY_STATIC(omx_debug);
#define GST_CAT_DEFAULT omx_debug

/* libgstaudio's, the stock omxaudiosink's base class's enum (gst-plugins-base has no headers here)
 */
GType gst_audio_base_sink_slave_method_get_type(void);

enum omx_kind { OMX_VIDEO_DEC, OMX_AUDIO_DEC, OMX_AUDIO_SINK, OMX_PASSTHROUGH_SINK };
struct omx_class {
    const char *type_name;
    enum omx_kind kind;
    const char *long_name, *description, *author;
    const char *sink_caps, *src_caps;
    const char *klass; /* the stock klass; NULL: Codec/Decoder/Video (Audio), Sink/Audio */
};
static const struct omx_class omx_classes[] = {
#include "omxclasses.h"
};

#define R G_PARAM_READABLE
#define W G_PARAM_WRITABLE

/* ---- the decoders ---- */

/* As gst-inspect-1.0 lists GstOMXVideoDec's (W16R, W18R, W21A: GstVideoDecoder has none; W22A and
 * W23A's GStreamer 1.18 adds GstVideoDecoder's max-errors and qos, W23A's qos-max-skip). */
enum {
#if MSTAR_REL >= 18
    V_CHANGE_BUFFER_META = 1,
    V_IS_SVP,
#else
    V_IS_SVP = 1,
#endif
    V_DECODED_SIZE,
    V_UNDECODED_SIZE,
    V_VDEC_CH,
    V_APP_TYPE,
    V_PORT,
    V_RESOURCE_INFO,
    V_SVP_VERSION,
#if MSTAR_REL < 18
    V_CLIP_MODE,
#endif
#if MSTAR_REL >= 16
    V_DISPLAYED_FRAMES,
    V_DROPPED_FRAMES,
#endif
#if MSTAR_REL >= 18
    V_DECODER_QUEUE_LIMIT,
#endif
#if MSTAR_REL >= 21
    V_TOTAL_DECODE_TIME,
#endif
#if MSTAR_REL == 22
    V_GENERATING_VIDEO_HASH,
#endif
#if MSTAR_REL >= 22
    V_MAX_ERRORS,
    V_QOS,
#endif
#if MSTAR_REL >= 23
    V_QOS_MAX_SKIP,
#endif
    V_END,
    N_VPROPS = V_END - 1
};
static const struct prop_def vprops[N_VPROPS] = {
#if MSTAR_REL >= 18
    {"change-buffer-meta", "If disable, Do not change the meta of buffer such as pts, duration",
     G_TYPE_BOOLEAN, 1},
#endif
    {"is-svp", "is svp, set only", G_TYPE_BOOLEAN, 0, NULL, W},
    {"decoded-size", "decode size, read only", G_TYPE_UINT64, 0},
    {"undecoded-size", "undecoded size, read only", G_TYPE_UINT64, 0},
    {"vdec-ch", "vdec channel, set only", G_TYPE_UINT, 1, NULL, W},
#if MSTAR_REL >= 21
    {"app-type", "Set app type", G_TYPE_STRING, 0, "default_app_type", W},
#else
    {"app-type", "Set app type", G_TYPE_STRING, 0},
#endif
    {"port", "port index (stream id), set only", G_TYPE_UINT, 1, NULL, W},
    {"resource-info", "Hold various information for managing resource", PROP_STRUCTURE, 0, NULL, W},
    {"svp-version", "svp version information", G_TYPE_UINT, 0},
#if MSTAR_REL < 18
    {"clip-mode", "Clip frames which are outside of segment", G_TYPE_BOOLEAN, 0, NULL, W},
#endif
#if MSTAR_REL >= 16
    {"displayed-frames", "the accumulated number of rendered frames", G_TYPE_INT, 0, NULL, R},
    {"dropped-frames", "the accumulated number of dropped frames", G_TYPE_INT, 0, NULL, R},
#endif
#if MSTAR_REL >= 21
    {"decoder-queue-limit", "Video frame queue limit in decoder. (0 = not used)", G_TYPE_UINT, 0,
     NULL, W},
    {"total-decode-time", "the accumulated decode time", G_TYPE_UINT64, 0, NULL, R},
#elif MSTAR_REL >= 18
    {"decoder-queue-limit", "Video frame queue limit in decoder. (0 = not used)", G_TYPE_UINT, 0},
#endif
#if MSTAR_REL == 22
    {"GeneratingVideoHash", "Whether to generate video hash", G_TYPE_BOOLEAN, 0},
#endif
#if MSTAR_REL >= 22
    {"max-errors", "Max consecutive decoder errors before returning flow error", G_TYPE_INT, 10,
     NULL, 0, NULL, -1, G_MAXINT, TRUE},
    {"qos", "Handle Quality-of-Service events from downstream", G_TYPE_BOOLEAN, 1},
#endif
#if MSTAR_REL >= 23
    {"qos-max-skip", "When skipping decode for QoS, maximum amount of video to skip (ns)",
     G_TYPE_UINT64, 500000000},
#endif
};

#if MSTAR_REL >= 21
/* As gst-inspect-1.0 lists LM21's GstOMXAudioPassthroughDec's, with GstAudioDecoder's own three;
 * W22A and W23A take default-prl and drc-mode over from the passthrough sink. */
enum {
    A_MIN_LATENCY = 1,
    A_TOLERANCE,
    A_PLC,
    A_APP_TYPE,
    A_DECODED_SIZE,
    A_DUAL_MONO,
    A_MPEGH_UI_XML,
    A_RESOURCE_INFO,
#if MSTAR_REL >= 22
    A_DEFAULT_PRL,
    A_DRC_MODE,
#endif
    A_END,
    N_APROPS = A_END - 1
};
static const struct prop_def aprops[N_APROPS] = {
    {"min-latency", "Aggregate output data to a minimum of latency time (ns)", G_TYPE_INT64, 0},
    {"tolerance", "Perfect ts while timestamp jitter/imperfection within tolerance (ns)",
     G_TYPE_INT64, 0},
    {"plc", "Perform packet loss concealment (if supported)", G_TYPE_BOOLEAN, 0},
    {"app-type", "Set app type", G_TYPE_STRING, 0, "default_app_type", W},
    {"decoded-size", "Decoded Data Length", G_TYPE_UINT64, 0},
    {"dual-mono", "Set dual mono for jp model", G_TYPE_UINT, 0},
    {"mpegh-ui-xml", "XML description of MPEG-H audio UI", G_TYPE_STRING, 0},
    {"resource-info", "Hold various information for managing resource", PROP_STRUCTURE, 0, NULL, W},
#if MSTAR_REL >= 22
    {"default-prl", "adjust loundness on Apple Music App", G_TYPE_INT, 0, NULL, 0, NULL, 0,
     G_MAXINT, TRUE},
    {"drc-mode", "adjust loundness on Apple Music App", G_TYPE_INT, 0, NULL, 0, NULL, 0, G_MAXINT,
     TRUE},
#endif
};
#else
/* As gst-inspect-1.0 lists GstOMXAudioDec's (both firmwares), after GstAudioDecoder's own three. */
enum {
    A_MIN_LATENCY = 1,
    A_TOLERANCE,
    A_PLC,
    A_RESOURCE_INFO,
    A_SILENT,
    A_DECODED_SIZE,
    A_UNDECODED_SIZE,
    A_CURRENT_PTS,
    A_APP_TYPE,
    A_SERVERSIDE_TRICKPLAY,
    A_DTS_SEAMLESS,
    N_APROPS = A_DTS_SEAMLESS
};
static const struct prop_def aprops[N_APROPS] = {
    {"min-latency", "Aggregate output data to a minimum of latency time (ns)", G_TYPE_INT64, 0},
    {"tolerance", "Perfect ts while timestamp jitter/imperfection within tolerance (ns)",
     G_TYPE_INT64, 0},
    {"plc", "Perform packet loss concealment (if supported)", G_TYPE_BOOLEAN, 0},
    {"resource-info", "Hold various information for managing resource", PROP_STRUCTURE, 0, NULL, W},
    {"silent", "Produce verbose output ?", G_TYPE_BOOLEAN, 0},
    {"decoded-size", "Decoded Data Length", G_TYPE_UINT64, 0},
    {"undecoded-size", "UnDecoded Data Length", G_TYPE_UINT64, 0},
    {"current-pts", "Get current PTS", G_TYPE_UINT64, 0},
    {"app-type", "Set app type", G_TYPE_STRING, 0},
    {"serverside-trickplay", "server side trick play", G_TYPE_BOOLEAN, 0},
    {"dts-seamless", "dts seamless", G_TYPE_BOOLEAN, 0},
};
#endif

#define N_DPROPS ((int)N_VPROPS > (int)N_APROPS ? (int)N_VPROPS : (int)N_APROPS)

typedef struct {
    GstElement parent;
    struct es_dec dec; /* first after the parent: esdec.c */
    GValue values[N_DPROPS];
    struct stream_info info;
} GstOMXDec;
typedef struct {
    GstElementClass parent_class;
    const struct omx_class *omx;
    const struct prop_def *props;
    guint n_props;
    void (*svp_handle)(GstOMXDec *self, gulong arg);
    void (*corrupted_frames)(GstOMXDec *self, gulong arg);
    guint (*acquire_vdec_handle)(GstOMXDec *self, gboolean acquire, GstCaps *caps);
} GstOMXDecClass;
#define OMX_DEC_CLASS(obj) ((GstOMXDecClass *)G_OBJECT_GET_CLASS(obj))

static GstElementClass *dec_parent_class;

/* The decoder's src caps for the stream: its src template's, fixed with what is known of the
 * stream (the sink takes the stream's parameters from its own caps, ES_CAPS_FIELD; these only
 * have to be fixed and acceptable downstream). */
static GstCaps *fixed_src_caps(GstElement *element, GstCaps *want)
{
    GstCaps *templ = gst_pad_get_pad_template_caps(((GstOMXDec *)element)->dec.srcpad);
    GstCaps *caps = gst_caps_intersect(want, templ);
    if (gst_caps_is_empty(caps)) {
        gst_caps_unref(caps);
        caps = gst_caps_copy(templ);
    }
    gst_caps_unref(templ);
    gst_caps_unref(want);
    caps = gst_caps_truncate(caps);
    return gst_caps_fixate(caps);
}

/* gst-omx's caps for its output state (gst_video_info_to_caps of the port's colour format, NV12
 * for the YUV 4:2:0 of MStar's decoders, then vendor=mstar): format and interlace-mode with
 * them. LG's decbridge (W18R's decproxy) refuses raw video caps without a format. */
static GstCaps *video_raw_caps(GstElement *element, GstCaps *es)
{
    GstOMXDec *self = (GstOMXDec *)element;
    struct stream_info info;
    stream_info_from_caps(&info, es);
    GST_OBJECT_LOCK(self);
    self->info = info;
    GST_OBJECT_UNLOCK(self);
    return fixed_src_caps(
        element,
        gst_caps_new_simple("video/x-raw", "format", G_TYPE_STRING, "NV12", "vendor", G_TYPE_STRING,
                            "mstar", "interlace-mode", G_TYPE_STRING, "progressive", "width",
                            G_TYPE_INT, info.width >= 16 ? (gint)info.width : 1920, "height",
                            G_TYPE_INT, info.height >= 16 ? (gint)info.height : 1080, "framerate",
                            GST_TYPE_FRACTION, info.fps_n > 0 ? info.fps_n : 0,
                            info.fps_n > 0 ? info.fps_d : 1, "pixel-aspect-ratio",
                            GST_TYPE_FRACTION, (gint)info.par_n, (gint)info.par_d, NULL));
}

static GstCaps *audio_raw_caps(GstElement *element, GstCaps *es)
{
    gint rate = 0, channels = 0;
    if (gst_caps_get_size(es)) {
        const GstStructure *s = gst_caps_get_structure(es, 0);
        gst_structure_get_int(s, "rate", &rate);
        gst_structure_get_int(s, "channels", &channels);
    }
#if MSTAR_REL >= 21
    /* LM21's audio decoders: the stream as passthrough bytes (their src template's U8) */
    GstCaps *caps =
        gst_caps_new_simple("audio/x-raw", "format", G_TYPE_STRING, "U8", "layout", G_TYPE_STRING,
                            "interleaved", "rate", G_TYPE_INT, rate > 0 ? rate : 48000, "channels",
                            G_TYPE_INT, channels > 0 ? channels : 2, NULL);
    gst_caps_set_features(caps, 0, gst_caps_features_new("mode:passthrough", NULL));
    return fixed_src_caps(element, caps);
#else
    /* The sample format is the src template's (S16LE; W15U's AAC, AC-3 and DTS decoders S32LE),
     * the stream's channels where the template takes them, else the template's (W15U's take one
     * or two): the caps keep layout, which omxaudiosink requires and the template does not have. */
    GstCaps *templ = gst_pad_get_pad_template_caps(((GstOMXDec *)element)->dec.srcpad);
    GstCaps *want = gst_caps_new_simple("audio/x-raw", "layout", G_TYPE_STRING, "interleaved",
                                        "rate", G_TYPE_INT, rate > 0 ? rate : 48000, "channels",
                                        G_TYPE_INT, channels > 0 ? channels : 2, NULL);
    GstCaps *both = gst_caps_intersect(want, templ);
    if (gst_caps_is_empty(both)) {
        gst_caps_unref(want);
        want = gst_caps_new_simple("audio/x-raw", "layout", G_TYPE_STRING, "interleaved", "rate",
                                   G_TYPE_INT, rate > 0 ? rate : 48000, NULL);
    }
    gst_caps_unref(both);
    gst_caps_unref(templ);
    return fixed_src_caps(element, want);
#endif
}

static void omx_dec_set_property(GObject *obj, guint id, const GValue *value, GParamSpec *spec)
{
    GstOMXDec *self = (GstOMXDec *)obj;
    GstOMXDecClass *klass = OMX_DEC_CLASS(obj);
    if (id < 1 || id > klass->n_props) {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
        return;
    }
    GST_OBJECT_LOCK(self);
    g_value_copy(value, &self->values[id - 1]);
    GST_OBJECT_UNLOCK(self);
    if (G_VALUE_HOLDS(value, GST_TYPE_STRUCTURE)) {
        const GstStructure *s = gst_value_get_structure(value);
        gchar *str = s ? gst_structure_to_string(s) : NULL;
        syslog(LOG_INFO, "%s: %s %s", GST_OBJECT_NAME(self), spec->name, str ? str : "(null)");
        g_free(str);
    }
    GST_DEBUG_OBJECT(self, "%s set", spec->name);
}

static void omx_dec_get_property(GObject *obj, guint id, GValue *value, GParamSpec *spec)
{
    GstOMXDec *self = (GstOMXDec *)obj;
    GstOMXDecClass *klass = OMX_DEC_CLASS(obj);
    if (id < 1 || id > klass->n_props) {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
        return;
    }
    if (!strcmp(spec->name, "decoded-size") || !strcmp(spec->name, "undecoded-size") ||
        !strcmp(spec->name, "current-pts")) {
        g_value_set_uint64(value, 0); /* nothing waits in a decoder */
        return;
    }
    if (!strcmp(spec->name, "displayed-frames") || !strcmp(spec->name, "dropped-frames")) {
        g_value_set_int(value, 0);
        return;
    }
    GST_OBJECT_LOCK(self);
    g_value_copy(&self->values[id - 1], value);
    GST_OBJECT_UNLOCK(self);
}

static void svp_handle(GstOMXDec *self, gulong arg)
{
    GST_INFO_OBJECT(self, "svp-handle %lu: no secure video path", arg);
}

#if MSTAR_REL >= 21
static void corrupted_frames(GstOMXDec *self, gulong arg)
{
    GST_INFO_OBJECT(self, "corrupted-frames %lu", arg);
}

/* No decoder instance to hand out: the host decodes. */
static guint acquire_vdec_handle(GstOMXDec *self, gboolean acquire, GstCaps *caps)
{
    syslog(LOG_INFO, "%s: acquire-vdec-handle %d", GST_OBJECT_NAME(self), acquire);
    return 0;
}
#endif

static void omx_dec_finalize(GObject *obj)
{
    es_dec_clear((struct es_dec_element *)obj);
    props_clear(((GstOMXDec *)obj)->values, OMX_DEC_CLASS(obj)->n_props);
    G_OBJECT_CLASS(dec_parent_class)->finalize(obj);
}

/* The abstract base, GstOMXVideoDec or GstOMXAudioDec (class data: the kind) */
static void omx_dec_base_class_init(gpointer g_class, gpointer data)
{
    GObjectClass *gobject = G_OBJECT_CLASS(g_class);
    GstOMXDecClass *klass = g_class;
    dec_parent_class = g_type_class_peek_parent(g_class);
    gobject->set_property = omx_dec_set_property;
    gobject->get_property = omx_dec_get_property;
    gobject->finalize = omx_dec_finalize;
    if (GPOINTER_TO_INT(data) == OMX_VIDEO_DEC) {
        klass->props = vprops;
        klass->n_props = N_VPROPS;
        props_install(gobject, vprops, N_VPROPS);
        klass->svp_handle = svp_handle;
        g_signal_new("svp-handle", G_TYPE_FROM_CLASS(g_class), G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION,
                     G_STRUCT_OFFSET(GstOMXDecClass, svp_handle), NULL, NULL,
                     g_cclosure_marshal_VOID__ULONG, G_TYPE_NONE, 1, G_TYPE_ULONG);
#if MSTAR_REL >= 21
        klass->corrupted_frames = corrupted_frames;
        klass->acquire_vdec_handle = acquire_vdec_handle;
        g_signal_new("corrupted-frames", G_TYPE_FROM_CLASS(g_class),
                     G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION,
                     G_STRUCT_OFFSET(GstOMXDecClass, corrupted_frames), NULL, NULL,
                     g_cclosure_marshal_VOID__ULONG, G_TYPE_NONE, 1, G_TYPE_ULONG);
        g_signal_new("acquire-vdec-handle", G_TYPE_FROM_CLASS(g_class),
                     G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION,
                     G_STRUCT_OFFSET(GstOMXDecClass, acquire_vdec_handle), NULL, NULL,
                     g_cclosure_marshal_generic, G_TYPE_UINT, 2, G_TYPE_BOOLEAN, GST_TYPE_CAPS);
#endif
    } else {
        klass->props = aprops;
        klass->n_props = N_APROPS;
        props_install(gobject, aprops, N_APROPS);
    }
}

/* A class of omx_classes[] (class data: its entry) */
static void omx_dec_class_init(gpointer g_class, gpointer data)
{
    GstElementClass *element = GST_ELEMENT_CLASS(g_class);
    GstOMXDecClass *klass = g_class;
    const struct omx_class *c = data;
    klass->omx = c;
    gst_element_class_add_pad_template(element,
                                       gst_pad_template_new("sink", GST_PAD_SINK, GST_PAD_ALWAYS,
                                                            gst_caps_from_string(c->sink_caps)));
    gst_element_class_add_pad_template(element,
                                       gst_pad_template_new("src", GST_PAD_SRC, GST_PAD_ALWAYS,
                                                            gst_caps_from_string(c->src_caps)));
    gst_element_class_set_metadata(element, c->long_name,
                                   c->klass                   ? c->klass
                                   : c->kind == OMX_VIDEO_DEC ? "Codec/Decoder/Video"
                                                              : "Codec/Decoder/Audio",
                                   c->description, c->author);
}

static void omx_dec_init(GTypeInstance *instance, gpointer g_class)
{
    GstOMXDec *self = (GstOMXDec *)instance;
    GstOMXDecClass *klass = g_class;
    GstElementClass *element = g_class;
    props_init(self->values, klass->props, klass->n_props);
    es_dec_init((struct es_dec_element *)self, gst_element_class_get_pad_template(element, "sink"),
                gst_element_class_get_pad_template(element, "src"),
                klass->omx->kind == OMX_VIDEO_DEC ? video_raw_caps : audio_raw_caps);
    if (klass->omx->kind == OMX_VIDEO_DEC)
        es_dec_report_underrun((struct es_dec_element *)self);
}

static GType omx_dec_base_type(enum omx_kind kind)
{
    static gsize types[2];
    if (g_once_init_enter(&types[kind])) {
        GTypeInfo info = {sizeof(GstOMXDecClass),
                          NULL,
                          NULL,
                          omx_dec_base_class_init,
                          NULL,
                          GINT_TO_POINTER(kind),
                          sizeof(GstOMXDec),
                          0,
                          NULL,
                          NULL};
        GType t = g_type_register_static(GST_TYPE_ELEMENT,
                                         kind == OMX_VIDEO_DEC ? "GstOMXVideoDec" :
#if MSTAR_REL >= 21
                                                               "GstOMXAudioPassthroughDec",
#else
                                                               "GstOMXAudioDec",
#endif
                                         &info, G_TYPE_FLAG_ABSTRACT);
        g_once_init_leave(&types[kind], t);
    }
    return types[kind];
}

/* ---- omxaudiosink, omxaudiopassthroughsink ---- */

/* As gst-inspect-1.0 lists GstOMXAnalogAudioSink's, after GstBaseSink's: GstAudioBaseSink's,
 * then gst-omx's (MStar's; LM21's adds four). */
enum {
    S_BUFFER_TIME = 1,
    S_LATENCY_TIME,
    S_PROVIDE_CLOCK,
    S_SLAVE_METHOD,
    S_CAN_ACTIVATE_PULL,
    S_ALIGNMENT_THRESHOLD,
    S_DRIFT_TOLERANCE,
    S_DISCONT_WAIT,
    S_MUTE,
    S_VOLUME,
    S_RESOURCE_INFO,
    S_SERVERSIDE_TRICKPLAY,
    S_APP_TYPE,
    S_DECODED_SIZE,
    S_UNDECODED_SIZE,
    S_CURRENT_PTS,
    S_LIPSYNC_OFFSET,
    S_NODELAY,
    S_NODELAY_MODE,
    S_NODELAY_SKIP,
    S_NODELAY_FAST,
    S_NODELAY_RECOVER,
    S_NODELAY_FAST_FREQ,
    S_DISABLE_LOST_STATE,
#if MSTAR_REL >= 16
    S_FADE,
    S_FADE_VOLUME,
#endif
#if MSTAR_REL >= 21
    S_AUDIO_BE_DELAY,
    S_CLOCK_SOURCE,
    S_DM_TOTAL_AUDIO_BUFFER_SIZE,
    S_DM_USED_AUDIO_BUFFER_SIZE,
#endif
    S_END,
    N_SPROPS = S_END - 1
};
/* the passthrough sink's directmedia-total-audio-buffer-size default, which W22A's alone changes */
#if MSTAR_REL == 22
#define DM_TOTAL_AUDIO_BUFFER 10240
#else
#define DM_TOTAL_AUDIO_BUFFER 4096
#endif
static const struct prop_def sprops[N_SPROPS] = {
    {"buffer-time",
     "Size of audio buffer in microseconds, this is the minimum latency that the sink reports",
     G_TYPE_INT64, 200000},
    {"latency-time", "The minimum amount of data to write in each iteration in microseconds",
     G_TYPE_INT64, 10000},
    {"provide-clock", "Provide a clock to be used as the global pipeline clock", G_TYPE_BOOLEAN, 0},
    {"slave-method", "Algorithm to use to match the rate of the masterclock", G_TYPE_ENUM, 1, NULL,
     0, gst_audio_base_sink_slave_method_get_type},
    {"can-activate-pull", "Allow pull-based scheduling", G_TYPE_BOOLEAN, 0},
    {"alignment-threshold", "Timestamp alignment threshold in nanoseconds", G_TYPE_UINT64,
     40000000},
    {"drift-tolerance", "Tolerance for clock drift in microseconds", G_TYPE_INT64, 180000},
#if MSTAR_REL >= 18
    {"discont-wait", "Window of time in nanoseconds to wait before creating a discontinuity",
     G_TYPE_UINT64, 0},
#else
    {"discont-wait", "Window of time in nanoseconds to wait before creating a discontinuity",
     G_TYPE_UINT64, 1000000000},
#endif
    {"mute", "mute channel", G_TYPE_BOOLEAN, 0},
    {"volume", "volume factor, 1.0=100%", G_TYPE_DOUBLE, 1},
    {"resource-info", "Hold various information for managing resource", PROP_STRUCTURE, 0, NULL, W},
    {"serverside-trickplay", "server side trick play", G_TYPE_BOOLEAN, 0},
    {"app-type", "Set app type", G_TYPE_STRING, 0},
    {"decoded-size", "Decoded Data Length", G_TYPE_UINT64, 0},
    {"undecoded-size", "UnDecoded Data Length", G_TYPE_UINT64, 0},
    {"current-pts", "Get current PTS", G_TYPE_UINT64, -1},
    {"lipsync-offset", "property works in audio sink and video sink element.", G_TYPE_UINT, 0},
    {"nodelay", "app no delay", G_TYPE_BOOLEAN, 0},
    {"nodelay-mode", "nodelay Mode (0: CONT, 1: SKIP, 2: HYBRID)", G_TYPE_UINT, 0},
    {"nodelay-skip", "set buffering Threshold of Skipping", G_TYPE_UINT, 0},
    {"nodelay-fast", "set buffering Threshold of Fast", G_TYPE_UINT, 0},
    {"nodelay-recover", "set buffering Threshold of Normal State", G_TYPE_UINT, 0},
    {"nodelay-fast-freq", "set frequency of Fast State (unit : Hz)", G_TYPE_UINT, 0},
    {"disable-lost-state", "Disable seek event if TRUE", G_TYPE_BOOLEAN, 0},
#if MSTAR_REL >= 18
    {"fade", "set fade structure", PROP_STRUCTURE, 0},
    {"fade-volume", "get the fade volume", G_TYPE_INT, 100},
#elif MSTAR_REL >= 16
    {"fade", "set fade structure", PROP_STRUCTURE, 0},
    {"fade-volume", "get the fade volume", G_TYPE_INT, 132120},
#endif
#if MSTAR_REL >= 21
    {"audio-be-delay", "property works in audio sink and video sink element.", G_TYPE_UINT, 59},
    {"clock-source", "Set clock source", G_TYPE_STRING, 0},
    {"directmedia-total-audio-buffer-size", "property works in audio sink and video sink element.",
     G_TYPE_INT, 0, NULL, 0, NULL, 0, G_MAXINT, TRUE},
    {"directmedia-used-audio-buffer-size", "property works in audio sink and video sink element.",
     G_TYPE_INT, 0, NULL, 0, NULL, 0, G_MAXINT, TRUE},
#endif
};

#if MSTAR_REL >= 21
/* As gst-inspect-1.0 lists LM21's GstOMXAudioPassthroughSink's, after GstBaseSink's (W21A's has
 * default-prl and drc-mode, which W22A and W23A move to the decoders). */
static const struct prop_def pprops[] = {
    {"app-type", "Set app type", G_TYPE_STRING, 0, "default_app_type", W},
    {"base-playback-threshold", "property works in room-to-room application", G_TYPE_INT64, 0},
    {"clock-source", "Set clock source", G_TYPE_STRING, 0, "default_clock_source", W},
#if MSTAR_REL < 22
    {"default-prl", "adjust loundness on Apple Music App", G_TYPE_INT, 0, NULL, 0, NULL, -31, 0,
     TRUE},
#endif
    {"directmedia-total-audio-buffer-size", "property works in audio sink and video sink element.",
     G_TYPE_INT, DM_TOTAL_AUDIO_BUFFER, NULL, 0, NULL, 0, G_MAXINT, TRUE},
    {"directmedia-used-audio-buffer-size", "property works in audio sink and video sink element.",
     G_TYPE_INT, 0, NULL, 0, NULL, 0, G_MAXINT, TRUE},
#if MSTAR_REL < 22
    {"drc-mode", "adjust loundness on Apple Music App", G_TYPE_INT, 0, NULL, 0, NULL, 0, 1, TRUE},
#endif
    {"fade", "set fade structure", PROP_STRUCTURE, 0, NULL, W},
    {"fade-volume", "get the fade volume", G_TYPE_INT, 100, NULL, 0, NULL, 0, 100, TRUE},
    {"gap-handle", "Set gap handle", G_TYPE_BOOLEAN, 0, NULL, W},
    {"mute", "mute channel", G_TYPE_BOOLEAN, 0},
    {"pcm-level", "get pcm level", G_TYPE_INT, 0, NULL, 0, NULL, 0, G_MAXINT, TRUE},
    {"resource-info", "Hold various information for managing resource", PROP_STRUCTURE, 0, NULL, W},
};
#define N_PPROPS     G_N_ELEMENTS(pprops)
#define N_SINK_PROPS (N_SPROPS > N_PPROPS ? N_SPROPS : N_PPROPS)
#else
#define N_SINK_PROPS N_SPROPS
#endif

typedef struct {
    EsSink parent;
    GValue values[N_SINK_PROPS];
} GstOMXAudioSink;
typedef struct {
    EsSinkClass parent_class;
    const struct prop_def *props;
    guint n_props;
    void (*audio_underrun)(GstOMXAudioSink *self);
} GstOMXAudioSinkClass;
#define OMX_SINK_CLASS(obj) ((GstOMXAudioSinkClass *)G_OBJECT_GET_CLASS(obj))

static GstElementClass *sink_parent_class;

static void omx_sink_set_property(GObject *obj, guint id, const GValue *value, GParamSpec *spec)
{
    GstOMXAudioSink *self = (GstOMXAudioSink *)obj;
    if (id < 1 || id > OMX_SINK_CLASS(obj)->n_props) {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
        return;
    }
    GST_OBJECT_LOCK(self);
    g_value_copy(value, &self->values[id - 1]);
    GST_OBJECT_UNLOCK(self);
    if (!strcmp(spec->name, "resource-info"))
        es_sink_resource_info(ES_SINK(self), value, "audio-port");
}

static void omx_sink_get_property(GObject *obj, guint id, GValue *value, GParamSpec *spec)
{
    GstOMXAudioSink *self = (GstOMXAudioSink *)obj;
    if (id < 1 || id > OMX_SINK_CLASS(obj)->n_props) {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
        return;
    }
    if (!strcmp(spec->name, "current-pts")) {
        g_value_set_uint64(value, es_sink_position(ES_SINK(self)));
        return;
    }
    if (!strcmp(spec->name, "decoded-size") || !strcmp(spec->name, "undecoded-size")) {
        g_value_set_uint64(value, 0);
        return;
    }
    GST_OBJECT_LOCK(self);
    g_value_copy(&self->values[id - 1], value);
    GST_OBJECT_UNLOCK(self);
}

static void omx_sink_finalize(GObject *obj)
{
    props_clear(((GstOMXAudioSink *)obj)->values, OMX_SINK_CLASS(obj)->n_props);
    G_OBJECT_CLASS(sink_parent_class)->finalize(obj);
}

#ifdef MSTAR_N21D
/* N21D (the StanbyME, webOS 6): a departure from the stock template. Its stock omxaacdec (and the
 * other GstOMXAudioPassthroughDec decoders) only ever emit audio/x-raw(mode:passthrough), and its
 * gstcool.conf makes omxaudiosink, the PCM-only GstOMXAnalogAudioSink, playbin's default audio
 * sink, where W21A and W22A (and N21D's webOS 24) name omxaudiopassthroughsink. decproxy picks the
 * decoder by rank and input caps alone and playsink tries only that sink, so the stock pair cannot
 * negotiate and <video> with AAC ends in media error 4 (RE of 04.44.16.01). The stand-in
 * analog sink also takes the passthrough caps while that is the configured default; it sends them
 * to the host as the passthrough sink does (essink.c). */
#define N21D_PASSTHROUGH_CAPS                                                                      \
    "audio/x-raw(mode:passthrough), format=(string)U8, layout=(string)interleaved, "               \
    "rate=(int)[ 1, 2147483647 ], channels=(int)[ 0, 2147483647 ]"
static gboolean analog_takes_passthrough(void)
{
    GKeyFile *conf = g_key_file_new();
    gchar *audio = NULL;
    if (g_key_file_load_from_file(conf, "/etc/gst/gstcool.conf", G_KEY_FILE_NONE, NULL))
        audio = g_key_file_get_string(conf, "default_sink", "audio", NULL);
    gboolean takes = audio && !strcmp(g_strstrip(audio), "omxaudiosink");
    g_free(audio);
    g_key_file_free(conf);
    return takes;
}
#endif

static void omx_sink_class_init(gpointer g_class, gpointer data)
{
    GObjectClass *gobject = G_OBJECT_CLASS(g_class);
    GstElementClass *element = GST_ELEMENT_CLASS(g_class);
    GstOMXAudioSinkClass *klass = g_class;
    const struct omx_class *c = data;
    sink_parent_class = g_type_class_peek_parent(g_class);
    gobject->set_property = omx_sink_set_property;
    gobject->get_property = omx_sink_get_property;
    gobject->finalize = omx_sink_finalize;
    klass->props = sprops;
    klass->n_props = N_SPROPS;
#if MSTAR_REL >= 21
    if (c->kind == OMX_PASSTHROUGH_SINK) {
        klass->props = pprops;
        klass->n_props = N_PPROPS;
    }
#endif
    props_install(gobject, klass->props, klass->n_props);
    klass->parent_class.kind = WEBOS_MEDIA_AUDIO;
    klass->parent_class.underrun_app_type = "mheg-ics";
    g_signal_new("audio-underrun", G_TYPE_FROM_CLASS(g_class), G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION,
                 G_STRUCT_OFFSET(GstOMXAudioSinkClass, audio_underrun), NULL, NULL,
                 g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    GstCaps *sink_caps = gst_caps_from_string(c->sink_caps);
#ifdef MSTAR_N21D
    if (c->kind == OMX_AUDIO_SINK && analog_takes_passthrough())
        gst_caps_append(sink_caps, gst_caps_from_string(N21D_PASSTHROUGH_CAPS));
#endif
    gst_element_class_add_pad_template(
        element, gst_pad_template_new("sink", GST_PAD_SINK, GST_PAD_ALWAYS, sink_caps));
    /* gstomx.conf's empty src-template-caps: the stock sink registers an empty src template too */
    gst_element_class_add_pad_template(
        element, gst_pad_template_new("src", GST_PAD_SRC, GST_PAD_ALWAYS, gst_caps_new_empty()));
    gst_element_class_set_metadata(element, c->long_name, c->klass ? c->klass : "Sink/Audio",
                                   c->description, c->author);
}

static void omx_sink_init(GTypeInstance *instance, gpointer g_class)
{
    GstOMXAudioSink *self = (GstOMXAudioSink *)instance;
    GstOMXAudioSinkClass *klass = g_class;
    props_init(self->values, klass->props, klass->n_props);
    /* The pipeline clock, as the stock sink provides the audio renderer's */
    es_sink_provide_clock(ES_SINK(self), "omxaudiosink-clock");
}

/* ---- registration from gstomx.conf ---- */

static GType class_type(const struct omx_class *c)
{
    GType t = g_type_from_name(c->type_name);
    if (t)
        return t;
    if (c->kind == OMX_AUDIO_SINK || c->kind == OMX_PASSTHROUGH_SINK) {
        GTypeInfo info = {sizeof(GstOMXAudioSinkClass),
                          NULL,
                          NULL,
                          omx_sink_class_init,
                          NULL,
                          c,
                          sizeof(GstOMXAudioSink),
                          0,
                          omx_sink_init,
                          NULL};
        /* the analog sink under gst-omx's abstract GstOMXAudioSink; LM21's passthrough sink is a
         * GstBaseSink */
        GType base = es_sink_get_type();
        if (c->kind == OMX_AUDIO_SINK) {
            base = g_type_from_name("GstOMXAudioSink");
            if (!base)
                base = g_type_register_static_simple(
                    es_sink_get_type(), "GstOMXAudioSink", sizeof(GstOMXAudioSinkClass), NULL,
                    sizeof(GstOMXAudioSink), NULL, G_TYPE_FLAG_ABSTRACT);
        }
        return g_type_register_static(base, c->type_name, &info, 0);
    }
    GTypeInfo info = {
        sizeof(GstOMXDecClass), NULL, NULL, omx_dec_class_init, NULL, c, sizeof(GstOMXDec), 0,
        omx_dec_init,           NULL};
    return g_type_register_static(omx_dec_base_type(c->kind), c->type_name, &info, 0);
}

static GType factory_type(const gchar *factory, const gchar *type_name)
{
    for (guint i = 0; i < G_N_ELEMENTS(omx_classes); i++)
        if (!strcmp(omx_classes[i].type_name, type_name))
            return class_type(&omx_classes[i]);
    return 0;
}

static gboolean plugin_init(GstPlugin *plugin)
{
    GST_DEBUG_CATEGORY_INIT(omx_debug, "omx", 0, "MStar's gst-omx (emulator)");
    omx_conf_register(plugin, factory_type, NULL);
    return TRUE;
}

#define PACKAGE "beanjar"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, omx,
                  "MStar's GStreamer OpenMAX plug-ins for the emulator", plugin_init, "1.0.0",
                  "LGPL", PACKAGE, "Beanjar")
