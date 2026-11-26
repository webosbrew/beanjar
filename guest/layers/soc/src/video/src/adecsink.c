// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* "adecsink": LG's GstAdecSink (libgstadecsink-1.0.so) for the emulator, video milestones 1 and 2.
 *
 * The real element feeds compressed or PCM audio to the SoC's audio decoder and provides the
 * pipeline clock from the hardware (gst_lxclock). lpsink and libpf create it by factory name for
 * every media pipeline with audio. This element keeps its surface (static "sink" pad with the
 * same caps, the 33 properties, audio-underrun) and sends the audio, compressed as it comes, to
 * the host viewer through a webos.media.<n> port (mediaport.c, include/webos-media.h), which
 * decodes it and plays it at base_time + running time on the pipeline clock, like vdecsink's
 * video. It is a GstBaseSink with sync=TRUE that renders WEBOS_MEDIA_LEAD_NS early, and provides
 * a monotonic GstSystemClock as the pipeline clock: the host follows this clock (it maps it to
 * its own), so the pipeline's position is what the host plays. Without a host the audio is
 * dropped in real time, as in milestone 1. */
#include <gst/gst.h>
#include <gst/base/gstbasesink.h>
#include <string.h>
#include <syslog.h>
#include "props.h"
#include "mediaport.h"
#include "esdec.h"

GST_DEBUG_CATEGORY_STATIC(adecsink_debug);
#define GST_CAT_DEFAULT adecsink_debug

/* LG's sink template caps, verbatim */
#define ADEC_CAPS                                                                                  \
    "audio/mpeg; audio/x-dts; audio/x-private1-dts; audio/x-dtsh; audio/x-dtse; "                  \
    "audio/x-dtsl; audio/x-ac3; audio/x-eac3; audio/x-private1-ac3; audio/x-wma; "                 \
    "audio/x-pn-realaudio; audio/x-raw; audio/x-lpcm-1; audio/x-lpcm; audio/x-private-lg-lpcm; "   \
    "audio/x-private1-lpcm; audio/x-private-ts-lpcm; audio/x-adpcm; audio/x-vorbis; audio/AMR; "   \
    "audio/AMR-WB; audio/x-flac; audio/x-mulaw; audio/x-alaw; audio/x-fd;"

static GstStaticPadTemplate sink_template =
    GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS(ADEC_CAPS));

/* Order and types of GstAdecSink's class_init. The last name has no string of its own next to
 * its blurb ("Version of audio sink"); "version", the tail of "mpegversion", is assumed. */
enum {
    P_NUM_OF_CHANNEL = 1,
    P_CODEC_TYPE,
    P_CHANNEL,
    P_SAMPLE_RATE,
    P_BITS_PER_SAMPLE,
    P_AVG_BYTES_PER_SEC,
    P_BLOCK_ALIGN,
    P_BUFFERING_THRESHOLD,
    P_PLAY_SPEED,
    P_DECODED_SIZE,
    P_UNDECODED_SIZE,
    P_CURRENT_PTS,
    P_START_PTS,
    P_SERVERSIDE_TRICKPLAY,
    P_ENLARGE_MAX_LATENESS,
    P_LIPSYNC_OFFSET,
    P_DISABLE_LOST_STATE,
    P_DISABLE_GSTSYNC,
    P_LIPSYNC,
    P_NODELAY,
    P_NODELAY_MODE,
    P_NODELAY_SKIP,
    P_NODELAY_FAST,
    P_NODELAY_RECOVER,
    P_NODELAY_FAST_FREQ,
    P_APP_TYPE,
    P_LOW_LATENCY,
    P_PAUSE_BY_FORCE,
    P_MIXER,
    P_INDEX,
    P_AUDIO_ONLY,
    P_SEAMLESS,
    P_VERSION,
    N_PROPS = P_VERSION,
    P_TS_OFFSET /* GstBaseSink's, the lead apart (mediaport.h) */
};
static const struct prop_def props[N_PROPS] = {
    {"num-of-channel", "ADEC Number of Channel", G_TYPE_UINT, 0},
    {"codec-type", "ADEC Codec Type", G_TYPE_UINT, 0},
    {"channel", "ADEC Decoding Channel", G_TYPE_UINT, 0},
    {"sample-rate", "ADEC Sample Rate", G_TYPE_UINT, 0},
    {"bits-per-sample", "ADEC Bits per Sample", G_TYPE_UINT, 0},
    {"avg-bytes-per-sec", "Average Bytes per Sec", G_TYPE_UINT, 0},
    {"block-align", "Block Align", G_TYPE_UINT, 0},
    {"buffering-threshold", "Threshold of Buffering", G_TYPE_UINT, 0},
    {"play-speed", "Play Speed", G_TYPE_INT, 0},
    {"decoded-size", "Decoded Data Length", G_TYPE_UINT64, 0},
    {"undecoded-size", "UnDecoded Data Length", G_TYPE_UINT64, 0},
    {"current-pts", "Get current PTS", G_TYPE_UINT64, 0},
    {"start-pts", "Decoding start PTS", G_TYPE_UINT64, 0},
    {"serverside-trickplay", "Set server's trick play mode", G_TYPE_BOOLEAN, 0},
    {"enlarge-max-lateness", "Enable check PTS for WiDi", G_TYPE_BOOLEAN, 0},
    {"lipsync-offset", "Audio's lipsync offset(msec)", G_TYPE_UINT, 0},
    {"disable-lost-state", "Disable calling lost_state()", G_TYPE_BOOLEAN, 0},
    {"disable-gstsync", "Disable Basesink's Sync", G_TYPE_BOOLEAN, 0},
    {"lipsync", "Set Lipsync Mode", G_TYPE_BOOLEAN, 0},
    {"nodelay", "Set No Delay Mode", G_TYPE_BOOLEAN, 0},
    {"nodelay-mode", "Nodelay Mode (0: CONT, 1: SKIP, 2: HYBRID)", G_TYPE_UINT, 0},
    {"nodelay-skip", "Buffering Threshold of Skipping", G_TYPE_UINT, 0},
    {"nodelay-fast", "Buffering Threshold of Fast", G_TYPE_UINT, 0},
    {"nodelay-recover", "Buffering Threshold of Normal State", G_TYPE_UINT, 0},
    {"nodelay-fast-freq", "Frequency of Fast State", G_TYPE_UINT, 0},
    {"app-type", "Set application type", G_TYPE_STRING, 0},
    {"low-latency", "Set low latency mode", G_TYPE_INT, 0},
    {"pause-by-force", "Pause by force", G_TYPE_BOOLEAN, 0},
    {"mixer", "Set using mixer", G_TYPE_BOOLEAN, 0},
    {"index", "Index of audio resource", G_TYPE_UINT, 0},
    {"audio-only", "This stream has audio only", G_TYPE_BOOLEAN, 0},
    {"seamless", "Seamless audio change", G_TYPE_BOOLEAN, 0},
    {"version", "Version of audio sink", G_TYPE_UINT, 0},
};

typedef struct {
    GstBaseSink parent;
    GValue values[N_PROPS];
    GstClock *clock;
    gboolean logged_caps;
    struct media_port port;
    struct es_starve *starve; /* audio-underrun when no sample follows the last one due */
} GstAdecSink;
typedef struct {
    GstBaseSinkClass parent_class;
    void (*audio_underrun)(GstAdecSink *self);
} GstAdecSinkClass;
static guint underrun_signal;

static GType gst_adec_sink_get_type(void);
G_DEFINE_TYPE(GstAdecSink, gst_adec_sink, GST_TYPE_BASE_SINK)

static void gst_adec_sink_set_property(GObject *obj, guint id, const GValue *value,
                                       GParamSpec *spec)
{
    GstAdecSink *self = (GstAdecSink *)obj;
    if (id == P_TS_OFFSET) {
        media_lead_set(&self->port, GST_BASE_SINK(obj), value);
        return;
    }
    if (id < 1 || id > N_PROPS) {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
        return;
    }
    GST_OBJECT_LOCK(self);
    g_value_copy(value, &self->values[id - 1]);
    GST_OBJECT_UNLOCK(self);
    GST_DEBUG_OBJECT(self, "%s set", spec->name);
}

static void gst_adec_sink_get_property(GObject *obj, guint id, GValue *value, GParamSpec *spec)
{
    GstAdecSink *self = (GstAdecSink *)obj;
    if (id == P_TS_OFFSET) {
        media_lead_get(&self->port, value);
        return;
    }
    if (id < 1 || id > N_PROPS) {
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
        return;
    }
    if (id == P_CURRENT_PTS) {
        /* what the host plays now: the clock-based position */
        gint64 pts = 0;
        if (!gst_element_query_position(GST_ELEMENT(self), GST_FORMAT_TIME, &pts) || pts < 0)
            pts = 0;
        g_value_set_uint64(value, (guint64)pts);
        return;
    }
    GST_OBJECT_LOCK(self);
    switch (id) {
    case P_DECODED_SIZE:
    case P_UNDECODED_SIZE:
        g_value_set_uint64(value, 0);
        break;
    default:
        g_value_copy(&self->values[id - 1], value);
        break;
    }
    GST_OBJECT_UNLOCK(self);
}

static GstClock *gst_adec_sink_provide_clock(GstElement *element)
{
    GstAdecSink *self = (GstAdecSink *)element;
    return self->clock ? gst_object_ref(self->clock) : NULL;
}

static gboolean gst_adec_sink_set_caps(GstBaseSink *base, GstCaps *caps)
{
    GstAdecSink *self = (GstAdecSink *)base;
    GST_DEBUG_OBJECT(self, "caps %" GST_PTR_FORMAT, caps);
    if (!self->logged_caps && gst_caps_get_size(caps) > 0) {
        GstStructure *s = gst_caps_get_structure(caps, 0);
        int rate = 0, channels = 0;
        gst_structure_get_int(s, "rate", &rate);
        gst_structure_get_int(s, "channels", &channels);
        syslog(LOG_INFO, "adecsink: %s rate=%d channels=%d (to the host)",
               gst_structure_get_name(s), rate, channels);
        self->logged_caps = TRUE;
    }
    media_port_caps(&self->port, GST_ELEMENT(base), caps);
    return TRUE;
}

static GstFlowReturn gst_adec_sink_render(GstBaseSink *base, GstBuffer *buf)
{
    GstAdecSink *self = (GstAdecSink *)base;
    media_port_buffer(&self->port, base, buf, 0);
    es_starve_arm(self->starve, &base->segment, GST_BUFFER_PTS(buf));
    return GST_FLOW_OK; /* rendered WEBOS_MEDIA_LEAD_NS early: the host plays it on time */
}

static gboolean gst_adec_sink_start(GstBaseSink *base)
{
    GstAdecSink *self = (GstAdecSink *)base;
    guint index;
    GST_OBJECT_LOCK(self);
    index = g_value_get_uint(&self->values[P_INDEX - 1]);
    GST_OBJECT_UNLOCK(self);
    media_port_start(&self->port, GST_ELEMENT(base), WEBOS_MEDIA_AUDIO, (gint32)index, 0);
    return TRUE;
}

static gboolean gst_adec_sink_stop(GstBaseSink *base)
{
    GstAdecSink *self = (GstAdecSink *)base;
    media_port_stop(&self->port, GST_ELEMENT(base));
    GST_OBJECT_LOCK(self);
    self->logged_caps = FALSE;
    GST_OBJECT_UNLOCK(self);
    es_starve_reset(self->starve, FALSE);
    return TRUE;
}

static gboolean gst_adec_sink_unlock(GstBaseSink *base)
{
    media_port_set_flushing(&((GstAdecSink *)base)->port, TRUE);
    return TRUE;
}

static gboolean gst_adec_sink_unlock_stop(GstBaseSink *base)
{
    media_port_set_flushing(&((GstAdecSink *)base)->port, FALSE);
    return TRUE;
}

static gboolean gst_adec_sink_event(GstBaseSink *base, GstEvent *event)
{
    if (GST_EVENT_TYPE(event) == GST_EVENT_FLUSH_START) {
        media_port_event(&((GstAdecSink *)base)->port, GST_ELEMENT(base), WEBOS_MEDIA_FLUSH);
        es_starve_reset(((GstAdecSink *)base)->starve, FALSE);
    }
    return GST_BASE_SINK_CLASS(gst_adec_sink_parent_class)->event(base, event);
}

/* EOS at the end of the last sample's own time, not WEBOS_MEDIA_LEAD_NS early. */
static GstFlowReturn gst_adec_sink_wait_event(GstBaseSink *base, GstEvent *event)
{
    GstFlowReturn ret;
    if (GST_EVENT_TYPE(event) != GST_EVENT_EOS)
        return GST_BASE_SINK_CLASS(gst_adec_sink_parent_class)->wait_event(base, event);
    es_starve_reset(((GstAdecSink *)base)->starve, TRUE);
    media_lead_suspend(&((GstAdecSink *)base)->port, base, TRUE);
    ret = GST_BASE_SINK_CLASS(gst_adec_sink_parent_class)->wait_event(base, event);
    media_lead_suspend(&((GstAdecSink *)base)->port, base, FALSE);
    if (ret == GST_FLOW_OK)
        media_port_event(&((GstAdecSink *)base)->port, GST_ELEMENT(base), WEBOS_MEDIA_EOS);
    return ret;
}

static GstStateChangeReturn gst_adec_sink_change_state(GstElement *element,
                                                       GstStateChange transition)
{
    GstAdecSink *self = (GstAdecSink *)element;
    if (transition == GST_STATE_CHANGE_PAUSED_TO_PLAYING)
        media_port_state(&self->port, element, TRUE);
    else if (transition == GST_STATE_CHANGE_PLAYING_TO_PAUSED)
        media_port_state(&self->port, element, FALSE);
    return GST_ELEMENT_CLASS(gst_adec_sink_parent_class)->change_state(element, transition);
}

static void gst_adec_sink_finalize(GObject *obj)
{
    GstAdecSink *self = (GstAdecSink *)obj;
    props_clear(self->values, N_PROPS);
    media_port_clear(&self->port);
    if (self->clock)
        gst_object_unref(self->clock);
    es_starve_free(self->starve);
    G_OBJECT_CLASS(gst_adec_sink_parent_class)->finalize(obj);
}

static void gst_adec_sink_class_init(GstAdecSinkClass *klass)
{
    GObjectClass *gobject = G_OBJECT_CLASS(klass);
    GstElementClass *element = GST_ELEMENT_CLASS(klass);
    GstBaseSinkClass *base = GST_BASE_SINK_CLASS(klass);
    gobject->set_property = gst_adec_sink_set_property;
    gobject->get_property = gst_adec_sink_get_property;
    gobject->finalize = gst_adec_sink_finalize;
    props_install(gobject, props, N_PROPS);
    media_lead_install(gobject, P_TS_OFFSET);
    underrun_signal = g_signal_new("audio-underrun", G_TYPE_FROM_CLASS(klass), G_SIGNAL_RUN_FIRST,
                                   G_STRUCT_OFFSET(GstAdecSinkClass, audio_underrun), NULL, NULL,
                                   g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    gst_element_class_add_pad_template(element, gst_static_pad_template_get(&sink_template));
    gst_element_class_set_static_metadata(
        element, "audio sink", "Sink/Audio",
        "LG's audio sink for the emulator: provides the clock, sends the audio to the host viewer",
        "Beanjar");
    element->provide_clock = gst_adec_sink_provide_clock;
    element->change_state = gst_adec_sink_change_state;
    base->set_caps = gst_adec_sink_set_caps;
    base->render = gst_adec_sink_render;
    base->start = gst_adec_sink_start;
    base->stop = gst_adec_sink_stop;
    base->unlock = gst_adec_sink_unlock;
    base->unlock_stop = gst_adec_sink_unlock_stop;
    base->event = gst_adec_sink_event;
    base->wait_event = gst_adec_sink_wait_event;
}

/* LG's adecsink reports audio-underrun when the audio runs dry: here, no sample after the last
 * one due for ES_STARVE_NS. */
static void adec_starved(GstElement *element)
{
    GST_INFO_OBJECT(element, "no sample since the last one due: audio-underrun");
    g_signal_emit(element, underrun_signal, 0);
}

static void gst_adec_sink_init(GstAdecSink *self)
{
    self->starve = es_starve_new(GST_ELEMENT(self), adec_starved);
    props_init(self->values, props, N_PROPS);
    media_port_init(&self->port);
    gst_base_sink_set_sync(GST_BASE_SINK(self), TRUE);
    media_lead_init(&self->port, GST_BASE_SINK(self));
    /* The pipeline clock, as LG's sink provides one: a monotonic system clock of our own (not
     * the shared default one, whose type a pipeline may have changed). */
    self->clock = g_object_new(GST_TYPE_SYSTEM_CLOCK, "name", "adecsink-clock", "clock-type",
                               GST_CLOCK_TYPE_MONOTONIC, NULL);
    GST_OBJECT_FLAG_SET(self, GST_ELEMENT_FLAG_PROVIDE_CLOCK);
}

static gboolean plugin_init(GstPlugin *plugin)
{
    GST_DEBUG_CATEGORY_INIT(adecsink_debug, "adecsink", 0, "adecsink stub");
    return gst_element_register(plugin, "adecsink", GST_RANK_PRIMARY, gst_adec_sink_get_type());
}

#define PACKAGE "beanjar"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, adecsink,
                  "LG's adecsink for the emulator (video milestone 2)", plugin_init, "1.0", "LGPL",
                  PACKAGE, "Beanjar")
