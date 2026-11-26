// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The guest sinks' lead (guest/layers/soc/src/video/src/mediaport.c, media_lead_*): a sink renders
 * WEBOS_MEDIA_LEAD_NS before a buffer is due, and its ts-offset property keeps what the pipeline
 * sets (playsink sets it on every link), shifting the sink, and the times its port sends for the
 * host to present at, by exactly that on top of the lead;
 * an EOS waits for its own time. A GstBaseSink wired as the guest's sinks are, against this
 * machine's GStreamer, on a GstTestClock: each check reads the clock time the sink waits for.
 * Prints TAP; exit status 0 only if every check passed. */
#include "../../../guest/layers/soc/src/video/src/mediaport.h"

#include <gst/app/gstappsrc.h>
#include <gst/check/gsttestclock.h>
#include <stdarg.h>
#include <stdio.h>

static int n, failed;
static void ok(int cond, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    printf("%s %d - ", cond ? "ok" : "not ok", ++n);
    vprintf(fmt, ap);
    printf("\n");
    va_end(ap);
    failed += !cond;
}

/* ---- a sink with the lead, as essink.c and vdecsink.c set it up ---- */
typedef struct {
    GstBaseSink parent;
    struct media_port port; /* never started: only its ts-offset is used */
} LeadSink;
typedef struct {
    GstBaseSinkClass parent_class;
} LeadSinkClass;
static GType lead_sink_get_type(void);
G_DEFINE_TYPE(LeadSink, lead_sink, GST_TYPE_BASE_SINK)

static void lead_sink_set_property(GObject *obj, guint id, const GValue *value, GParamSpec *spec)
{
    if (id == 1)
        media_lead_set(&((LeadSink *)obj)->port, GST_BASE_SINK(obj), value);
    else
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
}

static void lead_sink_get_property(GObject *obj, guint id, GValue *value, GParamSpec *spec)
{
    if (id == 1)
        media_lead_get(&((LeadSink *)obj)->port, value);
    else
        G_OBJECT_WARN_INVALID_PROPERTY_ID(obj, id, spec);
}

static GstFlowReturn lead_sink_wait_event(GstBaseSink *base, GstEvent *event)
{
    GstFlowReturn ret;
    if (GST_EVENT_TYPE(event) != GST_EVENT_EOS)
        return GST_BASE_SINK_CLASS(lead_sink_parent_class)->wait_event(base, event);
    media_lead_suspend(&((LeadSink *)base)->port, base, TRUE);
    ret = GST_BASE_SINK_CLASS(lead_sink_parent_class)->wait_event(base, event);
    media_lead_suspend(&((LeadSink *)base)->port, base, FALSE);
    return ret;
}

static void lead_sink_finalize(GObject *obj)
{
    media_port_clear(&((LeadSink *)obj)->port);
    G_OBJECT_CLASS(lead_sink_parent_class)->finalize(obj);
}

static GstStaticPadTemplate any =
    GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);

static void lead_sink_class_init(LeadSinkClass *klass)
{
    G_OBJECT_CLASS(klass)->finalize = lead_sink_finalize;
    G_OBJECT_CLASS(klass)->set_property = lead_sink_set_property;
    G_OBJECT_CLASS(klass)->get_property = lead_sink_get_property;
    media_lead_install(G_OBJECT_CLASS(klass), 1);
    gst_element_class_add_static_pad_template(GST_ELEMENT_CLASS(klass), &any);
    gst_element_class_set_static_metadata(GST_ELEMENT_CLASS(klass), "lead sink", "Sink", "test",
                                          "Beanjar");
    GST_BASE_SINK_CLASS(klass)->wait_event = lead_sink_wait_event;
}

static void lead_sink_init(LeadSink *self)
{
    gst_base_sink_set_sync(GST_BASE_SINK(self), TRUE);
    media_port_init(&self->port);
    media_lead_init(&self->port, GST_BASE_SINK(self));
}

/* ---- the checks ---- */

#define PTS      (1 * GST_SECOND)
#define DURATION (40 * GST_MSECOND)

/* One buffer at PTS, then EOS, with the sink's ts-offset set to offset (unset: none): the clock
 * times the sink waits for, as running times (less base_time). */
static void run(gboolean set, gint64 offset)
{
    GstClock *clock = gst_test_clock_new();
    GstElement *pipeline = gst_pipeline_new(NULL);
    GstElement *src = gst_element_factory_make("appsrc", NULL);
    GstElement *sink = g_object_new(lead_sink_get_type(), NULL);
    gint64 read = -1, want = set ? offset : 0;
    GstClockID id;
    g_object_set(src, "format", GST_FORMAT_TIME, NULL);
    gst_bin_add_many(GST_BIN(pipeline), src, sink, NULL);
    gst_element_link(src, sink);
    gst_pipeline_use_clock(GST_PIPELINE(pipeline), clock);
    if (set)
        g_object_set(sink, "ts-offset", offset, NULL);
    g_object_get(sink, "ts-offset", &read, NULL);
    ok(read == want,
       "ts-offset %" G_GINT64_FORMAT " ms reads back as set (%" G_GINT64_FORMAT " ms)",
       want / GST_MSECOND, read / GST_MSECOND);
    ok(((LeadSink *)sink)->port.ts_offset == want,
       "ts-offset %" G_GINT64_FORMAT " ms: the host presents that much later too",
       want / GST_MSECOND);
    GstBuffer *buf = gst_buffer_new_allocate(NULL, 16, NULL);
    GST_BUFFER_PTS(buf) = PTS;
    GST_BUFFER_DURATION(buf) = DURATION;
    gst_app_src_push_buffer(GST_APP_SRC(src), buf);
    gst_app_src_end_of_stream(GST_APP_SRC(src));
    gst_element_set_state(pipeline, GST_STATE_PLAYING);

    gst_test_clock_wait_for_next_pending_id(GST_TEST_CLOCK(clock), &id);
    GstClockTimeDiff t = (GstClockTimeDiff)gst_clock_id_get_time(id) -
                         (GstClockTimeDiff)gst_element_get_base_time(sink);
    GstClockTimeDiff due = (GstClockTimeDiff)PTS + want - WEBOS_MEDIA_LEAD_NS;
    ok(t == due,
       "ts-offset %" G_GINT64_FORMAT " ms: the buffer renders at %" G_GINT64_FORMAT
       " ms, its time + ts-offset - the lead (%" G_GINT64_FORMAT " ms)",
       want / GST_MSECOND, t / GST_MSECOND, due / GST_MSECOND);
    gst_clock_id_unref(id);
    gst_test_clock_set_time(GST_TEST_CLOCK(clock),
                            gst_element_get_base_time(sink) + (GstClockTime)due);
    gst_test_clock_crank(GST_TEST_CLOCK(clock));

    gst_test_clock_wait_for_next_pending_id(GST_TEST_CLOCK(clock), &id);
    t = (GstClockTimeDiff)gst_clock_id_get_time(id) -
        (GstClockTimeDiff)gst_element_get_base_time(sink);
    due = (GstClockTimeDiff)(PTS + DURATION) + want;
    ok(t == due,
       "ts-offset %" G_GINT64_FORMAT " ms: EOS waits for the end at its own time + "
       "ts-offset (%" G_GINT64_FORMAT " ms, want %" G_GINT64_FORMAT " ms)",
       want / GST_MSECOND, t / GST_MSECOND, due / GST_MSECOND);
    gst_clock_id_unref(id);
    gst_element_set_state(pipeline, GST_STATE_NULL);
    read = -1;
    g_object_get(sink, "ts-offset", &read, NULL);
    ok(read == want, "ts-offset still reads %" G_GINT64_FORMAT " ms after the EOS",
       read / GST_MSECOND);
    gst_object_unref(pipeline);
    gst_object_unref(clock);
}

int main(int argc, char **argv)
{
    gst_init(&argc, &argv);
    run(FALSE, 0);
    run(TRUE, 0);                 /* playsink's default av-offset */
    run(TRUE, 100 * GST_MSECOND); /* an A/V offset delaying this sink */
    run(TRUE, -50 * GST_MSECOND);
    printf("1..%d\n", n);
    return failed ? 1 : 0;
}
