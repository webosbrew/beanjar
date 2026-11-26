// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The sink half of the decoder + sink pairs whose stock sink shows decoded frames or plays PCM
 * from the SoC's decoder (W17H's omx_lxvideosink and audio_sink, MStar's mvsink, omxaudiosink
 * and masink). Here the decoders pass the compressed stream through under raw caps that name its
 * own (esdec.c), and this sink sends it to the host as h13's vdecsink and adecsink do
 * (mediaport.c): a GstBaseSink with sync=TRUE that renders WEBOS_MEDIA_LEAD_NS early, sends the
 * frames before the segment (from the keyframe before an accurate seek's target) as decode-only,
 * and the state, flushes and EOS; the host shows each frame and plays each sample at base_time +
 * running time on the pipeline clock. Really raw audio (PCM straight from the demuxer) is sent
 * as it is.
 *
 * Video: the stream's codec, size, frame rate, pixel aspect ratio and scan type come from its
 * caps (or its H.264 SPS: streaminfo.c); the element posts its firmware's messages with them
 * (post_info), which the pipeline needs before it reports the video loaded. Audio: the element
 * may provide a monotonic system clock as the pipeline clock, which the host follows.
 *
 * The elements keep their stock properties (props.h) and call es_sink_set_channel with the
 * decoder port of their resource-info, which is also the port the scaler window is connected to:
 * the host matches the two. */
#include "essink.h"
#include "esdec.h"
#include <string.h>
#include <syslog.h>
#include <unistd.h>

GST_DEBUG_CATEGORY_STATIC(essink_debug);
#define GST_CAT_DEFAULT essink_debug

#ifndef ESSINK_TYPE_NAME
#error "ESSINK_TYPE_NAME: the name of the abstract type in this plugin"
#endif

static GstBaseSinkClass *parent_class;

static const char *kind_name(EsSink *self)
{
    return ES_SINK_GET_CLASS(self)->kind == WEBOS_MEDIA_VIDEO ? "video" : "audio";
}

void es_sink_set_channel(EsSink *self, gint channel)
{
    GST_OBJECT_LOCK(self);
    self->channel = channel;
    GST_OBJECT_UNLOCK(self);
    media_port_set_channel(&self->port, channel);
}

#define FRAMES G_N_ELEMENTS(((EsSink *)0)->frames)

guint64 es_sink_position(EsSink *self)
{
    gint64 pos = 0;
    guint64 shown;
    if (!gst_element_query_position(GST_ELEMENT(self), GST_FORMAT_TIME, &pos) || pos < 0)
        pos = 0;
    if (ES_SINK_GET_CLASS(self)->kind != WEBOS_MEDIA_VIDEO)
        return (guint64)pos;
    /* LG's libpf polls this for the video's position and, on the ES path, for each frame shown
     * (STARFISH_EVENT_FRAMEREADY with its PTS): the frames whose time has come, the last one */
    GST_OBJECT_LOCK(self);
    while (self->count && self->frames[self->head] <= (guint64)pos) {
        self->shown_pts = self->frames[self->head];
        self->head = (self->head + 1) % FRAMES;
        self->count--;
    }
    shown = self->shown_pts;
    GST_OBJECT_UNLOCK(self);
    return shown;
}

/* the frames' bookkeeping, emptied at a flush and a stop (object lock held) */
static void frames_reset(EsSink *self)
{
    self->head = self->count = 0;
    self->eos = FALSE;
}

static gboolean underrun_reported(EsSink *self)
{
    gboolean report;
    GST_OBJECT_LOCK(self);
    report = !self->eos && GST_STATE(self) == GST_STATE_PLAYING;
    GST_OBJECT_UNLOCK(self);
    return report;
}

/* Audio: MStar's omxaudiosink and W17H's audio_sink report audio-underrun from their own stall
 * check: here, no sample after the last one due for ES_STARVE_NS. omxaudiosink's check runs only
 * for an MHEG ICS player (W16R libgstomx.so 0x20db2, W18R's the same strings: g_str_equal of its
 * app-type and "mheg-ics"): underrun_app_type. */
static void audio_starved(GstElement *element)
{
    const char *want = ES_SINK_GET_CLASS(element)->underrun_app_type;
    if (want) {
        gchar *app = NULL;
        gboolean match;
        g_object_get(element, "app-type", &app, NULL);
        match = !g_strcmp0(app, want);
        g_free(app);
        if (!match)
            return;
    }
    GST_INFO_OBJECT(element, "no sample since the last one due: audio-underrun");
    if (g_signal_lookup("audio-underrun", G_OBJECT_TYPE(element)))
        g_signal_emit_by_name(element, "audio-underrun");
}

/* Video: the decoder's BUFFER_UNDERRUN event (esdec.c) is what makes mvsink emit video-underrun
 * ("mvsink emit underrun signal", its event function's only emission). */
static void video_starved(EsSink *self)
{
    if (!underrun_reported(self))
        return;
    GST_INFO_OBJECT(self, "BUFFER_UNDERRUN: video-underrun");
    if (g_signal_lookup("video-underrun", G_OBJECT_TYPE(self)))
        g_signal_emit_by_name(self, "video-underrun");
}

/* A frame or sample sent. Video: its PTS (stream time) is shown when it comes due. Audio: the
 * starvation watch moves to its due time. */
static void note_frame(EsSink *self, GstBuffer *buf)
{
    GstBaseSink *base = GST_BASE_SINK(self);
    GstClockTime pts = GST_BUFFER_PTS(buf), st;
    if (!GST_CLOCK_TIME_IS_VALID(pts))
        return;
    if (ES_SINK_GET_CLASS(self)->kind != WEBOS_MEDIA_VIDEO) {
        es_starve_arm(self->starve, &base->segment, pts);
        return;
    }
    GST_OBJECT_LOCK(self);
    st = gst_segment_to_stream_time(&base->segment, GST_FORMAT_TIME, pts);
    if (GST_CLOCK_TIME_IS_VALID(st)) {
        if (self->count == FRAMES) {
            self->head = (self->head + 1) % FRAMES;
            self->count--;
        }
        self->frames[(self->head + self->count++) % FRAMES] = st;
    }
    GST_OBJECT_UNLOCK(self);
}

void es_sink_provide_clock(EsSink *self, const char *name)
{
    self->clock = g_object_new(GST_TYPE_SYSTEM_CLOCK, "name", name, "clock-type",
                               GST_CLOCK_TYPE_MONOTONIC, NULL);
    GST_OBJECT_FLAG_SET(self, GST_ELEMENT_FLAG_PROVIDE_CLOCK);
}

void es_sink_resource_info(EsSink *self, const GValue *value, const char *field)
{
    gint port;
    const GstStructure *s = gst_value_get_structure(value);
    gchar *str = s ? gst_structure_to_string(s) : NULL;
    syslog(LOG_INFO, "%s: resource-info %s", GST_OBJECT_NAME(self), str ? str : "(null)");
    g_free(str);
    if (resource_info_int(s, field, &port))
        es_sink_set_channel(self, port);
}

static GstClock *es_sink_provide(GstElement *element)
{
    EsSink *self = ES_SINK(element);
    return self->clock ? gst_object_ref(self->clock) : NULL;
}

static gboolean es_sink_set_caps(GstBaseSink *base, GstCaps *caps)
{
    EsSink *self = ES_SINK(base);
    EsSinkClass *klass = ES_SINK_GET_CLASS(self);
    GstCaps *es = es_caps_from(caps);
    self->framed = es && es_caps_framed(caps);
    if (!es)
        es = gst_caps_ref(caps); /* really raw: PCM from the demuxer (video: nothing to show) */
    GST_DEBUG_OBJECT(self, "caps %" GST_PTR_FORMAT, es);
    media_port_caps(&self->port, GST_ELEMENT(base), es);
    if (klass->kind == WEBOS_MEDIA_AUDIO) {
        if (!self->logged_caps && gst_caps_get_size(es) > 0) {
            GstStructure *s = gst_caps_get_structure(es, 0);
            int rate = 0, channels = 0;
            gst_structure_get_int(s, "rate", &rate);
            gst_structure_get_int(s, "channels", &channels);
            syslog(LOG_INFO, "%s: %s rate=%d channels=%d (to the host)", GST_OBJECT_NAME(self),
                   gst_structure_get_name(s), rate, channels);
            self->logged_caps = TRUE;
        }
        gst_caps_unref(es);
        return TRUE;
    }
    struct stream_info info;
    gboolean complete = stream_info_from_caps(&info, es);
    g_strlcpy(self->mime,
              gst_caps_get_size(es) ? gst_structure_get_name(gst_caps_get_structure(es, 0)) : "",
              sizeof self->mime);
    gst_caps_unref(es);
    if (self->posted && !memcmp(&info, &self->info, sizeof info))
        return TRUE;
    self->info = info;
    self->posted = FALSE;
    /* An H.264 stream whose caps lack size or rate waits for the SPS in the first buffer. */
    if ((complete || !info.h264) && klass->post_info) {
        klass->post_info(self);
        self->posted = TRUE;
    }
    return TRUE;
}

static void inspect(EsSink *self, GstBuffer *buf)
{
    EsSinkClass *klass = ES_SINK_GET_CLASS(self);
    GstMapInfo map;
    if (klass->kind != WEBOS_MEDIA_VIDEO || self->posted || !klass->post_info)
        return;
    if (self->info.h264 && gst_buffer_map(buf, &map, GST_MAP_READ)) {
        stream_info_from_annexb(&self->info, map.data, map.size);
        gst_buffer_unmap(buf, &map);
    }
    klass->post_info(self); /* what is known by the first buffer, whatever is still missing */
    self->posted = TRUE;
}

/* Video buffers GstBaseSink drops as outside the segment: the frames from the keyframe before an
 * accurate seek's target, which the decoder needs. The probe sees each buffer first and keeps the
 * ones its clip (by DTS, else PTS) puts outside; one that preroll() or render() then gets after all
 * is sent there, once, and one GstBaseSink did drop goes as decode-only before whatever follows it
 * (the next buffer's probe, a serialized event, EOS). Sending it from the probe as well sent a
 * frame twice whenever the two disagreed, the first frames of an MP4 with B-frames (DTS before
 * the segment, PTS in it), and the decoder took the repeated reference frame. */
static void take_before(EsSink *self, GstBuffer *buf)
{
    GST_OBJECT_LOCK(self);
    if (self->before == buf)
        gst_buffer_replace(&self->before, NULL);
    GST_OBJECT_UNLOCK(self);
}

static void send_before(EsSink *self)
{
    GstBuffer *buf;
    GST_OBJECT_LOCK(self);
    buf = self->before;
    self->before = NULL;
    GST_OBJECT_UNLOCK(self);
    if (buf) {
        media_port_buffer(&self->port, GST_BASE_SINK(self), buf, WEBOS_MEDIA_DECODE_ONLY);
        gst_buffer_unref(buf);
    }
}

static void drop_before(EsSink *self)
{
    GST_OBJECT_LOCK(self);
    gst_buffer_replace(&self->before, NULL);
    GST_OBJECT_UNLOCK(self);
}

static GstFlowReturn es_sink_preroll(GstBaseSink *base, GstBuffer *buf)
{
    EsSink *self = ES_SINK(base);
    if (ES_SINK_GET_CLASS(self)->kind != WEBOS_MEDIA_VIDEO)
        return GST_FLOW_OK;
    take_before(self, buf);
    inspect(self, buf);
    /* The host shows the prerolled frame at once; render() gets the same buffer when PLAYING
     * and does not send it again. */
    media_port_buffer(&self->port, base, buf, 0);
    if (GST_CLOCK_TIME_IS_VALID(GST_BUFFER_PTS(buf))) {
        GST_OBJECT_LOCK(self);
        self->shown_pts =
            gst_segment_to_stream_time(&base->segment, GST_FORMAT_TIME, GST_BUFFER_PTS(buf));
        GST_OBJECT_UNLOCK(self);
    }
    gst_buffer_replace(&self->prerolled, buf);
    return GST_FLOW_OK;
}

static GstFlowReturn es_sink_render(GstBaseSink *base, GstBuffer *buf)
{
    EsSink *self = ES_SINK(base);
    take_before(self, buf);
    inspect(self, buf);
    if (self->prerolled == buf) {
        gst_buffer_replace(&self->prerolled, NULL);
    } else if (self->framed) {
        GstBuffer *es = es_buffer_unframe(buf);
        media_port_buffer(&self->port, base, es, 0);
        gst_buffer_unref(es);
    } else {
        media_port_buffer(&self->port, base, buf, 0);
    }
    note_frame(self, buf);
    GST_OBJECT_LOCK(self);
    self->rendered++;
    GST_OBJECT_UNLOCK(self);
    return GST_FLOW_OK; /* rendered WEBOS_MEDIA_LEAD_NS early: the host presents it on time */
}

static GstPadProbeReturn before_segment(GstPad *pad, GstPadProbeInfo *info, gpointer data)
{
    EsSink *self = data;
    GstBaseSink *base = data;
    GstBuffer *buf = GST_PAD_PROBE_INFO_BUFFER(info);
    GstClockTime start, end = GST_CLOCK_TIME_NONE;
    GstSegment segment;
    if (!buf)
        return GST_PAD_PROBE_OK;
    send_before(self);
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
        GST_OBJECT_LOCK(self);
        gst_buffer_replace(&self->before, buf);
        GST_OBJECT_UNLOCK(self);
    }
    return GST_PAD_PROBE_OK;
}

static gboolean es_sink_start(GstBaseSink *base)
{
    EsSink *self = ES_SINK(base);
    gint ch;
    GST_OBJECT_LOCK(self);
    ch = self->channel;
    GST_OBJECT_UNLOCK(self);
    media_port_start(&self->port, GST_ELEMENT(base), ES_SINK_GET_CLASS(self)->kind, ch > 0 ? ch : 0,
                     0);
    /* which process: starfish-media-pipeline for <video>, the web engine's for MSE */
    syslog(LOG_INFO, "%s: %s sink started (pid %d, channel %d)", GST_OBJECT_NAME(self),
           kind_name(self), (int)getpid(), ch);
    /* A sink started again (playsink takes it to READY and back) keeps its pad's caps, whose
     * sticky event does not come a second time: the new session needs them. */
    GstCaps *caps = gst_pad_get_current_caps(GST_BASE_SINK_PAD(base));
    if (caps) {
        es_sink_set_caps(base, caps);
        gst_caps_unref(caps);
    }
    return TRUE;
}

static gboolean es_sink_stop(GstBaseSink *base)
{
    EsSink *self = ES_SINK(base);
    media_port_stop(&self->port, GST_ELEMENT(base));
    gst_buffer_replace(&self->prerolled, NULL);
    drop_before(self);
    GST_OBJECT_LOCK(self);
    self->posted = FALSE;
    self->logged_caps = FALSE;
    frames_reset(self);
    self->shown_pts = 0;
    GST_OBJECT_UNLOCK(self);
    es_starve_reset(self->starve, FALSE);
    return TRUE;
}

static gboolean es_sink_unlock(GstBaseSink *base)
{
    media_port_set_flushing(&ES_SINK(base)->port, TRUE);
    return TRUE;
}

static gboolean es_sink_unlock_stop(GstBaseSink *base)
{
    media_port_set_flushing(&ES_SINK(base)->port, FALSE);
    return TRUE;
}

static gboolean es_sink_event(GstBaseSink *base, GstEvent *event)
{
    EsSink *self = ES_SINK(base);
    if (GST_EVENT_TYPE(event) == GST_EVENT_FLUSH_START) {
        drop_before(self);
        media_port_event(&self->port, GST_ELEMENT(base), WEBOS_MEDIA_FLUSH);
        gst_buffer_replace(&self->prerolled, NULL);
        GST_OBJECT_LOCK(self);
        frames_reset(self);
        GST_OBJECT_UNLOCK(self);
        es_starve_reset(self->starve, FALSE);
    } else if (GST_EVENT_TYPE(event) == GST_EVENT_CUSTOM_DOWNSTREAM &&
               gst_event_has_name(event, "BUFFER_UNDERRUN")) {
        if (ES_SINK_GET_CLASS(self)->kind == WEBOS_MEDIA_VIDEO)
            video_starved(self);
        gst_event_unref(event);
        return TRUE;
    } else if (GST_EVENT_IS_SERIALIZED(event)) {
        send_before(self);
    }
    return parent_class->event(base, event);
}

/* EOS waits for the end of the last frame or sample at its own time, not WEBOS_MEDIA_LEAD_NS early.
 */
static GstFlowReturn es_sink_wait_event(GstBaseSink *base, GstEvent *event)
{
    EsSink *self = ES_SINK(base);
    GstFlowReturn ret;
    send_before(self);
    if (GST_EVENT_TYPE(event) != GST_EVENT_EOS)
        return parent_class->wait_event(base, event);
    GST_OBJECT_LOCK(self);
    self->eos = TRUE;
    GST_OBJECT_UNLOCK(self);
    es_starve_reset(self->starve, TRUE);
    media_lead_suspend(&self->port, base, TRUE);
    ret = parent_class->wait_event(base, event);
    media_lead_suspend(&self->port, base, FALSE);
    if (ret == GST_FLOW_OK)
        media_port_event(&self->port, GST_ELEMENT(base), WEBOS_MEDIA_EOS);
    return ret;
}

static GstStateChangeReturn es_sink_change_state(GstElement *element, GstStateChange transition)
{
    EsSink *self = ES_SINK(element);
    if (transition == GST_STATE_CHANGE_PAUSED_TO_PLAYING)
        media_port_state(&self->port, element, TRUE);
    else if (transition == GST_STATE_CHANGE_PLAYING_TO_PAUSED)
        media_port_state(&self->port, element, FALSE);
    return GST_ELEMENT_CLASS(parent_class)->change_state(element, transition);
}

static void es_sink_finalize(GObject *obj)
{
    EsSink *self = ES_SINK(obj);
    gst_buffer_replace(&self->prerolled, NULL);
    gst_buffer_replace(&self->before, NULL);
    media_port_clear(&self->port);
    if (self->clock)
        gst_object_unref(self->clock);
    es_starve_free(self->starve);
    G_OBJECT_CLASS(parent_class)->finalize(obj);
}

/* ts-offset: the pipeline's, the lead apart (mediaport.h) */
enum { ES_PROP_TS_OFFSET = 1 };

static void es_sink_set_property(GObject *obj, guint id, const GValue *value, GParamSpec *spec)
{
    if (id == ES_PROP_TS_OFFSET)
        media_lead_set(&ES_SINK(obj)->port, GST_BASE_SINK(obj), value);
    else
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
}

static void es_sink_get_property(GObject *obj, guint id, GValue *value, GParamSpec *spec)
{
    if (id == ES_PROP_TS_OFFSET)
        media_lead_get(&ES_SINK(obj)->port, value);
    else
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
}

static void es_sink_class_init(gpointer g_class, gpointer data)
{
    GObjectClass *gobject = G_OBJECT_CLASS(g_class);
    GstElementClass *element = GST_ELEMENT_CLASS(g_class);
    GstBaseSinkClass *base = GST_BASE_SINK_CLASS(g_class);
    parent_class = g_type_class_peek_parent(g_class);
    gobject->finalize = es_sink_finalize;
    gobject->set_property = es_sink_set_property;
    gobject->get_property = es_sink_get_property;
    media_lead_install(gobject, ES_PROP_TS_OFFSET);
    element->provide_clock = es_sink_provide;
    element->change_state = es_sink_change_state;
    base->set_caps = es_sink_set_caps;
    base->preroll = es_sink_preroll;
    base->render = es_sink_render;
    base->start = es_sink_start;
    base->stop = es_sink_stop;
    base->unlock = es_sink_unlock;
    base->unlock_stop = es_sink_unlock_stop;
    base->event = es_sink_event;
    base->wait_event = es_sink_wait_event;
}

/* The subclass's class_init has set kind by the time an instance exists. */
static void es_sink_init(GTypeInstance *instance, gpointer g_class)
{
    EsSink *self = ES_SINK(instance);
    self->channel = -1;
    media_port_init(&self->port);
    gst_base_sink_set_sync(GST_BASE_SINK(self), TRUE);
    media_lead_init(&self->port, GST_BASE_SINK(self));
    if (((EsSinkClass *)g_class)->kind == WEBOS_MEDIA_VIDEO) {
        gst_base_sink_set_qos_enabled(GST_BASE_SINK(self), FALSE);
        gst_pad_add_probe(GST_BASE_SINK_PAD(self), GST_PAD_PROBE_TYPE_BUFFER, before_segment, self,
                          NULL);
    } else {
        self->starve = es_starve_new(GST_ELEMENT(self), audio_starved);
    }
}

GType es_sink_get_type(void)
{
    static gsize type;
    if (g_once_init_enter(&type)) {
        GST_DEBUG_CATEGORY_INIT(essink_debug, "essink", 0, "the sinks' sending half (emulator)");
        GType t = g_type_register_static_simple(GST_TYPE_BASE_SINK, ESSINK_TYPE_NAME,
                                                sizeof(EsSinkClass), es_sink_class_init,
                                                sizeof(EsSink), es_sink_init, G_TYPE_FLAG_ABSTRACT);
        g_once_init_leave(&type, t);
    }
    return type;
}
