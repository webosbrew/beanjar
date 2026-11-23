// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The "decoder" half of the decoder + sink pairs (W17H's omx_lxvideodec + omx_lxvideosink and
 * audio_dec + audio_sink; MStar's gst-omx decoders + mvsink and omxaudiosink): the stock decoder
 * turns the compressed stream into raw frames on the SoC, which the stock sink then shows or
 * plays. Here nothing is decoded in the guest: the decoder passes the stream's own buffers through
 * unchanged (data, PTS, DTS, duration, flags), under raw caps of its src template whose
 * ES_CAPS_FIELD holds the stream's caps, and the sink sends them to the host (essink.c), which
 * decodes them. The output caps go with the first data (below); every other event and query
 * passes through as the GstPad defaults do it, so the pipeline above (playbin's or playbin3's
 * decodebin and LG's decproxy, or libpf's decoder -> queue -> sink for MSE) links, prerolls,
 * seeks and ends as with the stock pair.
 *
 * A video decoder whose output goes to something else than the emulator's sinks (LG's camera
 * app: appsrc ! omxmjpegdec ! appsink, whose buffers Chromium reads as pixels) cannot pass the
 * stream on: there the host decodes for the guest. Each buffer goes to the media helper
 * in a DECODE session on a webos.media port (include/webos-media.h), and the decoder pushes the
 * pictures that come back (NV12 or YUY2, as its src template and downstream agree:
 * choose_format), as raw video. Which case holds is found from the pads (consumer_of): the sinks
 * the output reaches.
 *
 * Audio decoders do the same: LG's push PCM whatever follows them, and W24G spacebuns' playbin3
 * links them to PulseAudio's pulsesink (its autoaudiosink ranks pulsesink above LG's audiosink),
 * which plays what it gets as samples. There the host decodes, and the decoder pushes stereo S16
 * at 48 kHz (WEBOS_MEDIA_S16LE). */
#include "esdec.h"
#include <string.h>
#include <syslog.h>

static struct es_dec *dec_of(GstObject *parent)
{
    return &((struct es_dec_element *)parent)->dec;
}

static void drop_held(struct es_dec *d)
{
    g_list_free_full(d->held, (GDestroyNotify)gst_event_unref);
    d->held = NULL;
}

/* The output caps, then the events held after them, before data (or EOS) goes on. */
static void push_pending(struct es_dec *d)
{
    GstObject *parent = GST_OBJECT_PARENT(d->srcpad);
    if (d->pending) {
        GstCaps *caps = d->pending;
        d->pending = NULL;
        if (!gst_pad_push_event(d->srcpad, gst_event_new_caps(caps)))
            GST_WARNING_OBJECT(parent, "downstream refused %" GST_PTR_FORMAT, caps);
        gst_caps_unref(caps);
    }
    for (GList *l = d->held; l; l = l->next)
        gst_pad_push_event(d->srcpad, l->data);
    g_list_free(d->held);
    d->held = NULL;
}

static gboolean starve_fire(GstClock *clock, GstClockTime t, GstClockID id, gpointer data)
{
    struct es_starve *w = data;
    gboolean fire;
    GST_OBJECT_LOCK(w->owner);
    fire = id == w->id && !w->fired && !w->ended && GST_STATE(w->owner) == GST_STATE_PLAYING;
    if (fire)
        w->fired = TRUE;
    GST_OBJECT_UNLOCK(w->owner);
    if (fire)
        w->starved(w->owner);
    return TRUE;
}

static void starve_cancel(struct es_starve *w)
{
    if (w->id) {
        gst_clock_id_unschedule(w->id);
        gst_clock_id_unref(w->id);
        w->id = NULL;
    }
}

struct es_starve *es_starve_new(GstElement *owner, void (*starved)(GstElement *owner))
{
    struct es_starve *w = g_new0(struct es_starve, 1);
    w->owner = owner;
    w->starved = starved;
    return w;
}

void es_starve_free(struct es_starve *w)
{
    if (!w)
        return;
    GST_OBJECT_LOCK(w->owner);
    starve_cancel(w);
    GST_OBJECT_UNLOCK(w->owner);
    g_free(w);
}

void es_starve_arm(struct es_starve *w, const GstSegment *segment, GstClockTime pts)
{
    GstClock *clock;
    guint64 rt;
    if (!w || !GST_CLOCK_TIME_IS_VALID(pts) || segment->format != GST_FORMAT_TIME)
        return;
    rt = gst_segment_to_running_time(segment, GST_FORMAT_TIME, pts);
    GST_OBJECT_LOCK(w->owner);
    starve_cancel(w);
    w->fired = w->ended = FALSE;
    clock = GST_ELEMENT_CLOCK(w->owner);
    if (clock && GST_CLOCK_TIME_IS_VALID(rt)) {
        w->id = gst_clock_new_single_shot_id(clock, GST_ELEMENT_CAST(w->owner)->base_time + rt +
                                                        ES_STARVE_NS);
        /* the owner outlives the watch (es_starve_free in its finalize unschedules it) */
        gst_clock_id_wait_async(w->id, starve_fire, w, NULL);
    }
    GST_OBJECT_UNLOCK(w->owner);
}

void es_starve_reset(struct es_starve *w, gboolean ended)
{
    if (!w)
        return;
    GST_OBJECT_LOCK(w->owner);
    starve_cancel(w);
    w->fired = FALSE;
    w->ended = ended;
    GST_OBJECT_UNLOCK(w->owner);
}

/* MStar's gst-omx video decoders: "push buffer underrun event" on input starvation, a serialized
 * custom downstream event named BUFFER_UNDERRUN (W16R libgstomx.so 0x11b64: gst_structure_new,
 * gst_event_new_custom(0x11806)), which mvsink turns into video-underrun. Pushed only while the
 * streaming thread is idle (it is, when starved). */
static void dec_starved(GstElement *element)
{
    struct es_dec *d = dec_of(GST_OBJECT(element));
    if (!GST_PAD_STREAM_TRYLOCK(d->sinkpad))
        return;
    GST_INFO_OBJECT(element, "no input since the last buffer due: BUFFER_UNDERRUN");
    gst_pad_push_event(d->srcpad, gst_event_new_custom(GST_EVENT_CUSTOM_DOWNSTREAM,
                                                       gst_structure_new_empty("BUFFER_UNDERRUN")));
    GST_PAD_STREAM_UNLOCK(d->sinkpad);
}

void es_dec_report_underrun(struct es_dec_element *self)
{
    self->dec.report_underrun = TRUE;
    self->dec.starve = es_starve_new(GST_ELEMENT(self), dec_starved);
}

void es_dec_length_prefix(struct es_dec_element *self, guint bpf)
{
    self->dec.length_prefix = bpf;
}

/* {u32 LE length; data; zeros to a multiple of bpf}, with buf's timestamps and flags */
static GstBuffer *frame(GstBuffer *buf, guint bpf)
{
    gsize n = gst_buffer_get_size(buf), size = (4 + n + bpf - 1) / bpf * bpf;
    GstBuffer *out = gst_buffer_new_allocate(NULL, size, NULL);
    GstMapInfo m;
    if (!out || !gst_buffer_map(out, &m, GST_MAP_WRITE)) {
        if (out)
            gst_buffer_unref(out);
        return buf;
    }
    GST_WRITE_UINT32_LE(m.data, (guint32)n);
    gst_buffer_extract(buf, 0, m.data + 4, n);
    memset(m.data + 4 + n, 0, size - 4 - n);
    gst_buffer_unmap(out, &m);
    gst_buffer_copy_into(out, buf, GST_BUFFER_COPY_METADATA, 0, -1);
    gst_buffer_unref(buf);
    return out;
}

GstBuffer *es_buffer_unframe(GstBuffer *buf)
{
    guint8 len[4];
    guint32 n;
    if (gst_buffer_extract(buf, 0, len, 4) != 4)
        return gst_buffer_ref(buf);
    n = GST_READ_UINT32_LE(len);
    if (n > gst_buffer_get_size(buf) - 4)
        n = gst_buffer_get_size(buf) - 4;
    GstBuffer *out = gst_buffer_copy_region(buf, GST_BUFFER_COPY_ALL, 4, n);
    /* a region that does not start at 0 keeps no timestamps */
    GST_BUFFER_PTS(out) = GST_BUFFER_PTS(buf);
    GST_BUFFER_DTS(out) = GST_BUFFER_DTS(buf);
    GST_BUFFER_DURATION(out) = GST_BUFFER_DURATION(buf);
    return out;
}

gboolean es_caps_framed(GstCaps *caps)
{
    const GstStructure *s =
        caps && gst_caps_get_size(caps) ? gst_caps_get_structure(caps, 0) : NULL;
    return s && gst_structure_has_field(s, ES_FRAMING_FIELD);
}

/* ---- decoding for a consumer that needs pixels ---- */

/* One of the emulator's sinks (essink.c): each plugin registers its abstract base type as
 * "EsSink-<plugin>". */
static gboolean emulator_sink(GstElement *e)
{
    for (GType t = G_OBJECT_TYPE(e); t; t = g_type_parent(t))
        if (g_str_has_prefix(g_type_name(t), "EsSink-"))
            return TRUE;
    return FALSE;
}

/* What takes the data a source pad pushes: follows links through bins' ghost pads and through
 * elements in between (queues, LG's decproxy and decbridge, converters) to the sinks. SINK when
 * every sink reached is the emulator's; PIXELS when any other sink is (an appsink, a fakesink,
 * or an element without source pads); UNKNOWN when a link is still missing (decodebin, decproxy
 * before its switch), which leaves the pass-through as it was. Through an element it follows the
 * source pads the sink pad links to inside it (its internal links): one for an element that
 * carries several streams side by side, such as playsink's streamsynchronizer or a multiqueue,
 * whose other source pads are other streams (the subtitles' appsink in LG's tsinkbin), and all of
 * them for a tee or an element without its own answer. */
static gint consumer_of(GstPad *src, int depth)
{
    GstPad *peer = gst_pad_get_peer(src);
    gint result = ES_CONSUMER_UNKNOWN;
    if (!peer || depth > 32) {
        if (peer)
            gst_object_unref(peer);
        return ES_CONSUMER_UNKNOWN;
    }
    if (GST_IS_PROXY_PAD(peer)) {
        /* a bin's sink ghost pad: on to its inside; the inside of a bin's source ghost pad: on
         * from the ghost pad */
        GstPad *next = GST_PAD(gst_proxy_pad_get_internal(GST_PROXY_PAD(peer)));
        if (next) {
            result = consumer_of(next, depth + 1);
            gst_object_unref(next);
        }
        gst_object_unref(peer);
        return result;
    }
    GstElement *e = gst_pad_get_parent_element(peer);
    if (!e) {
        gst_object_unref(peer);
        return ES_CONSUMER_UNKNOWN;
    }
    if (emulator_sink(e))
        result = ES_CONSUMER_SINK;
    else if (GST_OBJECT_FLAG_IS_SET(e, GST_ELEMENT_FLAG_SINK) || !e->numsrcpads) {
        result = ES_CONSUMER_PIXELS;
        syslog(LOG_INFO, "a decoder's output reaches %s (%s), not an emulator sink",
               GST_OBJECT_NAME(e), G_OBJECT_TYPE_NAME(e));
    } else {
        /* the pad's own function, as gst_pad_iterate_internal_links calls it with the parent
         * held (that function is not among the firmware symbols the build links against) */
        GstPadIterIntLinkFunction links = GST_PAD_ITERINTLINKFUNC(peer);
        GstIterator *it = links ? links(peer, GST_OBJECT(e)) : NULL;
        if (!it)
            it = gst_element_iterate_src_pads(e);
        GValue v = G_VALUE_INIT;
        gboolean all_sinks = TRUE, done = FALSE;
        while (!done) {
            switch (gst_iterator_next(it, &v)) {
            case GST_ITERATOR_OK: {
                gint r = consumer_of(g_value_get_object(&v), depth + 1);
                if (r == ES_CONSUMER_PIXELS) {
                    result = ES_CONSUMER_PIXELS;
                    done = TRUE;
                } else if (r != ES_CONSUMER_SINK)
                    all_sinks = FALSE;
                g_value_reset(&v);
                break;
            }
            case GST_ITERATOR_RESYNC:
                gst_iterator_resync(it);
                all_sinks = TRUE;
                break;
            default:
                done = TRUE;
            }
        }
        g_value_unset(&v);
        gst_iterator_free(it);
        if (result != ES_CONSUMER_PIXELS && all_sinks)
            result = ES_CONSUMER_SINK;
    }
    gst_object_unref(e);
    gst_object_unref(peer);
    return result;
}

/* The pictures' format and caps features: the first of the src template's raw video
 * structures, in its order, with a format the host makes (NV12, YUY2) that downstream takes, as
 * the stock decoder's negotiation fixates; those in system memory first, which is what the
 * buffers are. Only a downstream that asks for memory:DMABuf (W22H's camera pipeline filters
 * for it) gets that feature on ordinary memory: LG's camera player maps the buffers
 * (GstDecodedBuffer, libStarfishCameraPlayer). MStar's LM21 MJPEG decoder lists YUY2 before NV12:
 * YUY2, which Chromium's camera player takes packed; Realtek's lists NV12 only. */
static void choose_format(struct es_dec *d)
{
    GstCaps *templ = gst_pad_get_pad_template_caps(d->srcpad), *want = gst_caps_new_empty();
    for (guint n = 0; n < 2 * gst_caps_get_size(templ); n++) {
        guint i = n % gst_caps_get_size(templ);
        GstCapsFeatures *tf = gst_caps_get_features(templ, i);
        gboolean sys =
            !tf || gst_caps_features_is_equal(tf, GST_CAPS_FEATURES_MEMORY_SYSTEM_MEMORY);
        if (sys != (n < gst_caps_get_size(templ)))
            continue;
        GstCaps *one = gst_caps_copy_nth(templ, i);
        GstCaps *host = gst_caps_from_string("video/x-raw, format=(string){ NV12, YUY2 }");
        GstCapsFeatures *f = gst_caps_get_features(templ, i);
        if (f)
            gst_caps_set_features(host, 0, gst_caps_features_copy(f));
        GstCaps *both = gst_caps_intersect_full(one, host, GST_CAPS_INTERSECT_FIRST);
        gst_caps_append(want, both);
        gst_caps_unref(one);
        gst_caps_unref(host);
    }
    gst_caps_unref(templ);
    GstCaps *peer = gst_pad_peer_query_caps(d->srcpad, want);
    GstCaps *pick = peer && !gst_caps_is_empty(peer) ? peer : want;
    const char *format = "NV12";
    if (d->features)
        gst_caps_features_free(d->features);
    d->features = NULL;
    if (!gst_caps_is_empty(pick)) {
        GstCapsFeatures *f = gst_caps_get_features(pick, 0);
        /* the first format the structure lists (intersections keep the template's order) */
        const GValue *v = gst_structure_get_value(gst_caps_get_structure(pick, 0), "format");
        const char *first = v && GST_VALUE_HOLDS_LIST(v) && gst_value_list_get_size(v)
                                ? g_value_get_string(gst_value_list_get_value(v, 0))
                            : v && G_VALUE_HOLDS_STRING(v) ? g_value_get_string(v)
                                                           : NULL;
        if (first && !strcmp(first, "YUY2"))
            format = "YUY2";
        if (f && !gst_caps_features_is_equal(f, GST_CAPS_FEATURES_MEMORY_SYSTEM_MEMORY))
            d->features = gst_caps_features_copy(f);
    }
    d->format = format;
    gchar *str = gst_caps_to_string(pick);
    syslog(LOG_INFO, "%s: pictures as %s (downstream takes %.300s)",
           GST_OBJECT_NAME(GST_OBJECT_PARENT(d->srcpad)), format, str);
    g_free(str);
    if (peer)
        gst_caps_unref(peer);
    gst_caps_unref(want);
}

/* The decoded pictures' caps: the chosen format and features at the picture's size, with the
 * stream's rate and aspect. */
static GstCaps *picture_caps(struct es_dec *d, const struct webos_media_picture *pic)
{
    gint fps_n = 0, fps_d = 1, par_n = 1, par_d = 1;
    const GstStructure *es =
        d->es && gst_caps_get_size(d->es) ? gst_caps_get_structure(d->es, 0) : NULL;
    if (es) {
        gst_structure_get_fraction(es, "framerate", &fps_n, &fps_d);
        gst_structure_get_fraction(es, "pixel-aspect-ratio", &par_n, &par_d);
    }
    GstCaps *caps = gst_caps_new_simple(
        "video/x-raw", "format", G_TYPE_STRING, d->format, "width", G_TYPE_INT, (gint)pic->width,
        "height", G_TYPE_INT, (gint)pic->height, "framerate", GST_TYPE_FRACTION, fps_n, fps_d,
        "pixel-aspect-ratio", GST_TYPE_FRACTION, par_n, par_d, "interlace-mode", G_TYPE_STRING,
        "progressive", NULL);
    if (d->features)
        gst_caps_set_features(caps, 0, gst_caps_features_copy(d->features));
    return caps;
}

static void push_held(struct es_dec *d)
{
    for (GList *l = d->held; l; l = l->next)
        gst_pad_push_event(d->srcpad, l->data);
    g_list_free(d->held);
    d->held = NULL;
}

/* A picture from the host: caps first when its size is new (then the events held after the
 * stream's caps), then the picture, with the input buffer's timestamps when it answers that. */
/* libgstvideo's, declared here: the soc layer compiles against GStreamer core's headers only
 * (gst-headers.sh). GstVideoFormat's values are ABI: YUY2 4, NV12 23. */
extern void *gst_buffer_add_video_meta_full(GstBuffer *buffer, int flags, int format, guint width,
                                            guint height, guint n_planes, gsize offset[4],
                                            gint stride[4]);

/* The picture's layout as a GstVideoMeta: consumers read the planes from it (W22H's camera
 * player, CameraDecodePlayer's new-sample handler, takes plane 1 from the meta and has no
 * pointer to it without one). */
static void add_video_meta(GstBuffer *buf, const struct webos_media_picture *pic)
{
    gsize offset[4] = {pic->offset[0], pic->offset[1], 0, 0};
    gint stride[4] = {(gint)pic->stride[0], (gint)pic->stride[1], 0, 0};
    gboolean nv12 = pic->format == WEBOS_MEDIA_NV12;
    gst_buffer_add_video_meta_full(buf, 0, nv12 ? 23 : 4, pic->width, pic->height, nv12 ? 2 : 1,
                                   offset, stride);
}

/* Samples from the host (WEBOS_MEDIA_S16LE): the caps once, then the samples at their pts. */
static void on_samples(struct es_dec *d, const struct webos_media_picture *pic, GstBuffer *buf)
{
    GstObject *parent = GST_OBJECT_PARENT(d->srcpad);
    if (!d->picture_caps || gst_pad_check_reconfigure(d->srcpad)) {
        if (!d->picture_caps)
            d->picture_caps =
                gst_caps_new_simple("audio/x-raw", "format", G_TYPE_STRING, "S16LE", "layout",
                                    G_TYPE_STRING, "interleaved", "rate", G_TYPE_INT,
                                    WEBOS_MEDIA_S16LE_RATE, "channels", G_TYPE_INT, 2, NULL);
        gchar *str = gst_caps_to_string(d->picture_caps);
        syslog(LOG_INFO, "%s: samples from the host: %s", GST_OBJECT_NAME(parent), str);
        g_free(str);
        if (!gst_pad_push_event(d->srcpad, gst_event_new_caps(gst_caps_ref(d->picture_caps))))
            GST_WARNING_OBJECT(parent, "downstream refused %" GST_PTR_FORMAT, d->picture_caps);
    }
    push_held(d);
    GST_BUFFER_PTS(buf) =
        pic->pts == WEBOS_MEDIA_NONE ? GST_CLOCK_TIME_NONE : (GstClockTime)pic->pts;
    GST_BUFFER_DTS(buf) = GST_CLOCK_TIME_NONE;
    GST_BUFFER_DURATION(buf) = (GstClockTime)pic->width * GST_SECOND / WEBOS_MEDIA_S16LE_RATE;
    if (d->in && !d->in_pictures++ && GST_BUFFER_FLAG_IS_SET(d->in, GST_BUFFER_FLAG_DISCONT))
        GST_BUFFER_FLAG_SET(buf, GST_BUFFER_FLAG_DISCONT);
    d->pictures++;
    if (d->last_flow == GST_FLOW_OK)
        d->last_flow = gst_pad_push(d->srcpad, buf);
    else
        gst_buffer_unref(buf);
}

static void on_picture(gpointer data, const struct webos_media_picture *pic, GstBuffer *buf)
{
    struct es_dec *d = data;
    GstObject *parent = GST_OBJECT_PARENT(d->srcpad);
    if (pic->format == WEBOS_MEDIA_S16LE) {
        on_samples(d, pic, buf);
        return;
    }
    const GstStructure *s = d->picture_caps ? gst_caps_get_structure(d->picture_caps, 0) : NULL;
    gint w = 0, h = 0;
    if (s) {
        gst_structure_get_int(s, "width", &w);
        gst_structure_get_int(s, "height", &h);
    }
    if (!s || w != (gint)pic->width || h != (gint)pic->height ||
        gst_pad_check_reconfigure(d->srcpad)) {
        if (d->picture_caps)
            gst_caps_unref(d->picture_caps);
        d->picture_caps = picture_caps(d, pic);
        GST_INFO_OBJECT(parent, "pictures from the host: %" GST_PTR_FORMAT, d->picture_caps);
        gchar *str = gst_caps_to_string(d->picture_caps);
        syslog(LOG_INFO, "%s: pictures from the host: %s", GST_OBJECT_NAME(parent), str);
        g_free(str);
        if (!gst_pad_push_event(d->srcpad, gst_event_new_caps(gst_caps_ref(d->picture_caps))))
            GST_WARNING_OBJECT(parent, "downstream refused %" GST_PTR_FORMAT, d->picture_caps);
    }
    push_held(d);
    if (d->in && !d->in_pictures++) {
        gst_buffer_copy_into(buf, d->in, GST_BUFFER_COPY_TIMESTAMPS | GST_BUFFER_COPY_FLAGS, 0, -1);
        GST_BUFFER_FLAG_UNSET(buf, GST_BUFFER_FLAG_DELTA_UNIT);
    } else {
        GST_BUFFER_PTS(buf) =
            pic->pts == WEBOS_MEDIA_NONE ? GST_CLOCK_TIME_NONE : (GstClockTime)pic->pts;
        GST_BUFFER_DTS(buf) = GST_CLOCK_TIME_NONE;
    }
    add_video_meta(buf, pic);
    d->pictures++;
    if (d->last_flow == GST_FLOW_OK)
        d->last_flow = gst_pad_push(d->srcpad, buf);
    else
        gst_buffer_unref(buf);
}

#define ANSWER_MS 3000      /* the host's answer to one input */
#define REPORT_US 5000000ll /* a rate line every 5 s */

/* in (NULL: EOS) to the host, and its pictures downstream */
static GstFlowReturn decode_input(struct es_dec *d, GstBuffer *in)
{
    GstElement *element = GST_ELEMENT(GST_OBJECT_PARENT(d->srcpad));
    if (!d->decoding) {
        if (!media_port_start(&d->port, element, d->video ? WEBOS_MEDIA_VIDEO : WEBOS_MEDIA_AUDIO,
                              -1, 0)) {
            d->dropped++;
            return GST_FLOW_OK;
        }
        if (d->video) {
            choose_format(d);
            media_port_decode(&d->port, element,
                              !strcmp(d->format, "YUY2") ? WEBOS_MEDIA_YUY2 : WEBOS_MEDIA_NV12);
        } else
            media_port_decode(&d->port, element, WEBOS_MEDIA_S16LE);
        if (d->es)
            media_port_caps(&d->port, element, d->es);
        d->decoding = TRUE;
        syslog(LOG_INFO, "%s: the host decodes its stream (media port %d)",
               GST_OBJECT_NAME(element), d->port.index);
    }
    gint64 t0 = g_get_monotonic_time();
    guint32 seq = media_port_send_input(&d->port, element, in);
    d->send_us += g_get_monotonic_time() - t0;
    if (!seq) {
        d->dropped++;
        return GST_FLOW_OK;
    }
    d->input = seq;
    d->in = in;
    d->in_pictures = 0;
    d->last_flow = GST_FLOW_OK;
    if (!media_port_answers(&d->port, seq, ANSWER_MS, on_picture, d)) {
        if (d->unanswered++ < 5 || d->unanswered % 100 == 0)
            syslog(LOG_WARNING,
                   "%s: no answer from the host to input %u (%" G_GUINT64_FORMAT " so far)",
                   GST_OBJECT_NAME(element), seq, d->unanswered);
    }
    d->in = NULL;
    gint64 now = g_get_monotonic_time();
    d->wait_us += now - t0;
    /* the rate, for tests/regress/virtcam/run-camera.py --app: pictures pushed downstream */
    if (!d->report_us) {
        d->report_us = now;
        d->report_pictures = d->pictures;
        d->report_inputs = d->buffers;
        d->report_wait_us = d->wait_us;
        d->report_send_us = d->send_us;
        d->report_reads = d->port.reads;
    } else if (now - d->report_us >= REPORT_US) {
        guint64 inputs = d->buffers - d->report_inputs;
        syslog(LOG_INFO,
               "%s: host decoding: %" G_GUINT64_FORMAT " pictures pushed of %" G_GUINT64_FORMAT
               " inputs in %.1f s (%.2f/s), %.1f ms per input (%.1f sending, %.0f reads), "
               "%" G_GUINT64_FORMAT " unanswered, flow %s",
               GST_OBJECT_NAME(element), d->pictures - d->report_pictures, inputs,
               (now - d->report_us) / 1e6,
               (d->pictures - d->report_pictures) * 1e6 / (now - d->report_us),
               inputs ? (d->wait_us - d->report_wait_us) / 1000.0 / inputs : 0.0,
               inputs ? (d->send_us - d->report_send_us) / 1000.0 / inputs : 0.0,
               inputs ? (double)(d->port.reads - d->report_reads) / inputs : 0.0, d->unanswered,
               gst_flow_get_name(d->last_flow));
        d->report_us = now;
        d->report_pictures = d->pictures;
        d->report_inputs = d->buffers;
        d->report_wait_us = d->wait_us;
        d->report_send_us = d->send_us;
        d->report_reads = d->port.reads;
    }
    return d->last_flow;
}

static void decode_stop(struct es_dec *d)
{
    GstElement *element = GST_ELEMENT(GST_OBJECT_PARENT(d->srcpad));
    if (!d->decoding)
        return;
    syslog(LOG_INFO,
           "%s: host decoding ends: %" G_GUINT64_FORMAT " pictures, %" G_GUINT64_FORMAT
           " inputs unanswered, %" G_GUINT64_FORMAT " dropped, %.1f ms per input",
           GST_OBJECT_NAME(element), d->pictures, d->unanswered, d->dropped,
           d->buffers ? d->wait_us / 1000.0 / d->buffers : 0.0);
    media_port_stop(&d->port, element);
    d->decoding = FALSE;
    d->report_us = 0;
    if (d->picture_caps)
        gst_caps_unref(d->picture_caps);
    d->picture_caps = NULL;
}

/* What the output goes to (ES_CONSUMER_*), found again after a relink. */
static gint es_dec_consumer(struct es_dec *d, gboolean relinked)
{
    if (relinked || d->consumer == ES_CONSUMER_UNKNOWN)
        d->consumer = consumer_of(d->srcpad, 0);
    return d->consumer;
}

static GstFlowReturn es_dec_chain(GstPad *pad, GstObject *parent, GstBuffer *buf)
{
    struct es_dec *d = dec_of(parent);
    gboolean relinked = gst_pad_check_reconfigure(d->srcpad);
    gint consumer = es_dec_consumer(d, relinked);
    /* An audio decoder's output pad is linked on to the sink only once its caps are out
     * (decodebin3 exposes it then): the stream's caps, and then where they went. */
    if (consumer == ES_CONSUMER_UNKNOWN && !d->video && d->pending) {
        push_pending(d);
        consumer = es_dec_consumer(d, TRUE);
    }
    if (consumer == ES_CONSUMER_PIXELS) {
        GstFlowReturn r;
        /* the stream's caps never go downstream here: the pictures' do, with the first one */
        if (d->pending)
            gst_caps_unref(d->pending);
        d->pending = NULL;
        d->buffers++;
        d->bytes += gst_buffer_get_size(buf);
        es_starve_arm(d->starve, &d->segment, GST_BUFFER_PTS(buf));
        r = decode_input(d, buf);
        gst_buffer_unref(buf);
        return r;
    }
    if (d->decoding) { /* relinked to an emulator sink: the stream again */
        decode_stop(d);
        if (!d->pending && d->raw)
            d->pending = gst_caps_ref(d->raw);
    }
    /* Relinked downstream (decproxy's switch from its fake decoder to this one, which drops
     * caps until it is done): the caps again, as a decoder renegotiates on reconfigure. */
    if (relinked && !d->pending)
        d->pending = gst_pad_get_current_caps(d->srcpad);
    push_pending(d);
    d->buffers++;
    d->bytes += gst_buffer_get_size(buf);
    es_starve_arm(d->starve, &d->segment, GST_BUFFER_PTS(buf));
    if (d->length_prefix)
        buf = frame(buf, d->length_prefix);
    return gst_pad_push(d->srcpad, buf);
}

/* As a decoder base class does it (GstVideoDecoder, GstAudioDecoder, which the stock decoders
 * are), the output caps go downstream with the first data after them, and the serialized events
 * that follow them (segment, tags) are held until then: a decoder bin that switches decoders on
 * the fly (LG's decproxy deploys a fake one until the resources arrive) loses caps pushed at once,
 * and the sink then gets buffers without them. */
static gboolean es_dec_sink_event(GstPad *pad, GstObject *parent, GstEvent *event)
{
    struct es_dec *d = dec_of(parent);
    switch (GST_EVENT_TYPE(event)) {
    case GST_EVENT_CAPS: {
        GstCaps *es;
        gst_event_parse_caps(event, &es);
        GstCaps *raw = d->raw_caps(GST_ELEMENT(parent), es);
        gchar *s = gst_caps_to_string(es);
        gst_caps_set_simple(raw, ES_CAPS_FIELD, G_TYPE_STRING, s, NULL);
        if (d->length_prefix)
            gst_caps_set_simple(raw, ES_FRAMING_FIELD, G_TYPE_STRING, "length-prefixed", NULL);
        GST_INFO_OBJECT(parent, "%s -> %" GST_PTR_FORMAT, s, raw);
        syslog(LOG_INFO, "%s: %.200s (passed to the sink)", GST_OBJECT_NAME(parent), s);
        g_free(s);
        if (d->es)
            gst_caps_unref(d->es);
        d->es = gst_caps_ref(es);
        if (d->decoding)
            media_port_caps(&d->port, GST_ELEMENT(parent), es);
        gst_event_unref(event);
        if (d->raw)
            gst_caps_unref(d->raw);
        d->raw = gst_caps_ref(raw);
        if (d->pending)
            gst_caps_unref(d->pending);
        d->pending = raw;
        return TRUE;
    }
    case GST_EVENT_FLUSH_START:
        media_port_set_flushing(&d->port, TRUE);
        break;
    case GST_EVENT_FLUSH_STOP:
        media_port_set_flushing(&d->port, FALSE);
        if (d->decoding)
            media_port_event(&d->port, GST_ELEMENT(parent), WEBOS_MEDIA_FLUSH);
        drop_held(d);
        es_starve_reset(d->starve, FALSE);
        gst_segment_init(&d->segment, GST_FORMAT_UNDEFINED);
        break;
    case GST_EVENT_SEGMENT:
        gst_event_copy_segment(event, &d->segment);
        goto hold;
    case GST_EVENT_EOS:
        es_starve_reset(d->starve, TRUE);
        if (d->decoding) { /* the host's last pictures, then what was held */
            if (d->pending)
                gst_caps_unref(d->pending);
            d->pending = NULL;
            decode_input(d, NULL);
            push_held(d);
            break;
        }
        push_pending(d);
        break;
    case GST_EVENT_GAP:
        if (d->decoding)
            break; /* held until the pictures' caps */
        push_pending(d);
        break;
    default:
    hold:
        if ((d->pending || d->held || (d->decoding && !d->picture_caps)) &&
            GST_EVENT_IS_SERIALIZED(event) && GST_EVENT_TYPE(event) != GST_EVENT_STREAM_START) {
            d->held = g_list_append(d->held, event);
            return TRUE;
        }
        break;
    }
    return gst_pad_event_default(pad, parent, event);
}

void es_dec_clear(struct es_dec_element *self)
{
    struct es_dec *d = &self->dec;
    if (d->pending)
        gst_caps_unref(d->pending);
    d->pending = NULL;
    drop_held(d);
    es_starve_free(d->starve);
    d->starve = NULL;
    if (d->raw)
        gst_caps_unref(d->raw);
    if (d->es)
        gst_caps_unref(d->es);
    if (d->picture_caps)
        gst_caps_unref(d->picture_caps);
    d->raw = d->es = d->picture_caps = NULL;
    if (d->features)
        gst_caps_features_free(d->features);
    d->features = NULL;
    media_port_clear(&d->port);
}

/* Push mode on and off with the element's READY<->PAUSED: a DECODE session ends with it (the
 * streaming thread, which may wait for the host, is out first). */
static gboolean es_dec_activate_mode(GstPad *pad, GstObject *parent, GstPadMode mode,
                                     gboolean active)
{
    struct es_dec *d = dec_of(parent);
    if (active) {
        media_port_set_flushing(&d->port, FALSE);
        d->consumer = ES_CONSUMER_UNKNOWN;
        return TRUE;
    }
    media_port_set_flushing(&d->port, TRUE);
    GST_PAD_STREAM_LOCK(pad);
    decode_stop(d);
    d->consumer = ES_CONSUMER_UNKNOWN;
    GST_PAD_STREAM_UNLOCK(pad);
    return TRUE;
}

void es_dec_init(struct es_dec_element *self, GstPadTemplate *sink, GstPadTemplate *src,
                 GstCaps *(*raw_caps)(GstElement *element, GstCaps *es))
{
    struct es_dec *d = &self->dec;
    d->raw_caps = raw_caps;
    gst_segment_init(&d->segment, GST_FORMAT_UNDEFINED);
    media_port_init(&d->port);
    GstCaps *templ = gst_pad_template_get_caps(src);
    d->video = gst_caps_get_size(templ) &&
               g_str_has_prefix(gst_structure_get_name(gst_caps_get_structure(templ, 0)), "video/");
    gst_caps_unref(templ);
    d->sinkpad = gst_pad_new_from_template(sink, "sink");
    gst_pad_set_chain_function(d->sinkpad, es_dec_chain);
    gst_pad_set_activatemode_function(d->sinkpad, es_dec_activate_mode);
    gst_pad_set_event_function(d->sinkpad, es_dec_sink_event);
    gst_element_add_pad(GST_ELEMENT(self), d->sinkpad);
    d->srcpad = gst_pad_new_from_template(src, "src");
    gst_pad_use_fixed_caps(d->srcpad);
    gst_element_add_pad(GST_ELEMENT(self), d->srcpad);
}

GstCaps *es_caps_from(GstCaps *caps)
{
    const GstStructure *s =
        caps && gst_caps_get_size(caps) ? gst_caps_get_structure(caps, 0) : NULL;
    const gchar *es = s ? gst_structure_get_string(s, ES_CAPS_FIELD) : NULL;
    return es ? gst_caps_from_string(es) : NULL;
}

gboolean resource_info_int(const GstStructure *s, const char *field, gint *out)
{
    const GValue *v = s ? gst_structure_get_value(s, field) : NULL;
    GValue i = G_VALUE_INIT;
    if (!v)
        return FALSE;
    if (G_VALUE_HOLDS_STRING(v)) {
        const gchar *str = g_value_get_string(v);
        gchar *end;
        gint64 n = str ? g_ascii_strtoll(str, &end, 10) : 0;
        if (!str || end == str)
            return FALSE;
        *out = (gint)n;
        return TRUE;
    }
    g_value_init(&i, G_TYPE_INT);
    if (!g_value_type_transformable(G_VALUE_TYPE(v), G_TYPE_INT) || !g_value_transform(v, &i))
        return FALSE;
    *out = g_value_get_int(&i);
    return TRUE;
}
