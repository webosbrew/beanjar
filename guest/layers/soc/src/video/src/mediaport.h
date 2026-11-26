// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The sinks' end of a webos.media.<n> port (include/webos-media.h): one per sink pad, open from
 * READY->PAUSED to PAUSED->READY. See mediaport.c. With MEDIAPORT_CORE only the part without
 * GStreamer: the session on a clock of the writer's own. */
#ifndef MEDIAPORT_H
#define MEDIAPORT_H
#include <glib.h>
#ifndef MEDIAPORT_CORE
#include <gst/gst.h>
#include <gst/base/gstbasesink.h>
#endif
#include "../../../../../../include/webos-media.h"

#define MEDIAPORT_API __attribute__((visibility("hidden")))

/* The session's clock (webos_media_header.clock), in ns, at clk (the argument every call takes):
 * a sink's pipeline clock; WEBOS_MEDIA_NONE without one. */
typedef gint64 (*media_clock_fn)(gpointer clk);

struct media_port {
    GMutex lock; /* one message at a time: streaming and state-change threads */
    int fd;      /* -1: no free port, or not started */
    int index;
    guint32 sequence;
    gboolean host;   /* the host end was connected at the last write */
    gboolean resync; /* the host end (re)connected: send OPEN, CAPS, STATE again */
    gint flushing;   /* unlock(): do not start waiting for the port */
    struct webos_media_open open;
    gchar *caps; /* the last CAPS, for a resync */
    gboolean have_state;
    struct webos_media_state state;
    gboolean need_keyframe; /* video after a resync or a flush: skip to a keyframe */
    guint32 decode;         /* a DECODE session's format (media_port_decode): sent after OPEN */
    int pool_fd;            /* the pool's device (WEBOS_MEDIA_POOL_DEVICE), claimed for index */
    struct media_shared *shared; /* the session's rings, which a shared picture gives its slot
                                    back on */
    guint64 shared_pictures, inline_pictures;
    guint64 reads; /* read(2) calls for its answers */
    guint64 sent, dropped, bytes;
    gint64 bytes_per_second; /* raw audio's, from its caps; 0 otherwise */
    gint64 untimed_next;     /* sync off: where the next audio buffer starts, or NONE */
    media_clock_fn clock;
    gint64 ts_offset; /* the sink's ts-offset property, the lead apart (media_lead_set) */
};

MEDIAPORT_API void media_port_init(struct media_port *p);
MEDIAPORT_API void media_port_clear(struct media_port *p);
/* Takes a free port and sends OPEN for the writer name, on clock. FALSE if none is free. */
MEDIAPORT_API gboolean media_port_open(struct media_port *p, const char *name, guint32 kind,
                                       gint32 channel, guint32 pad, media_clock_fn clock,
                                       gpointer clk);
MEDIAPORT_API void media_port_stop(struct media_port *p, gpointer clk);
/* The stream's caps as a string (WEBOS_MEDIA_CAPS); raw audio's bytes a second, else 0. */
MEDIAPORT_API void media_port_caps_string(struct media_port *p, gpointer clk, const char *caps,
                                          gint64 bytes_per_second);
/* One BUFFER, b and its bytes as they are; a video session that resynchronised waits for a
 * keyframe. FALSE: dropped. */
MEDIAPORT_API gboolean media_port_send(struct media_port *p, gpointer clk,
                                       const struct webos_media_buffer *b, const void *data,
                                       gsize size);
MEDIAPORT_API void media_port_send_state(struct media_port *p, gpointer clk,
                                         const struct webos_media_state *s);
MEDIAPORT_API void media_port_event(struct media_port *p, gpointer clk, guint16 type);
MEDIAPORT_API void media_port_set_flushing(struct media_port *p, gboolean flushing);
/* Where the pictures go (webos_media_open.output); a change sends the session again. */
MEDIAPORT_API void media_port_set_output(struct media_port *p, guint32 output);
/* The channel (webos_media_open.channel) learnt after the start; a change sends the session again.
 */
MEDIAPORT_API void media_port_set_channel(struct media_port *p, gint32 channel);

#ifndef MEDIAPORT_CORE
/* Takes a free port and sends OPEN. FALSE if none is free (the sink then plays as before,
 * dropping in real time). */
MEDIAPORT_API gboolean media_port_start(struct media_port *p, GstElement *element, guint32 kind,
                                        gint32 channel, guint32 pad);
MEDIAPORT_API void media_port_caps(struct media_port *p, GstElement *element, GstCaps *caps);
/* A buffer, with its times turned into running time with the sink's segment. A sink whose sync
 * is off presents on arrival, as the stock sinks do then: video goes untimed, audio is stamped
 * from its arrival on, one buffer after the other. */
MEDIAPORT_API void media_port_buffer(struct media_port *p, GstBaseSink *sink, GstBuffer *buf,
                                     guint32 flags);
MEDIAPORT_API void media_port_state(struct media_port *p, GstElement *element, gboolean playing);

/* Decoding for the guest (include/webos-media.h): the session's pictures come back on the
 * port in format (WEBOS_MEDIA_NV12, WEBOS_MEDIA_YUY2). After media_port_start; drops whatever the
 * host had sent the port before. */
MEDIAPORT_API void media_port_decode(struct media_port *p, GstElement *element, guint32 format);
/* A DECODE session's input: buf with its own timestamps, or (buf NULL) EOS. Returns the
 * message's sequence, which its answers name; 0: not sent (no host, flushing). */
MEDIAPORT_API guint32 media_port_send_input(struct media_port *p, GstElement *element,
                                            GstBuffer *buf);
/* A picture of the answers: its fields and its bytes (the callee's reference). */
typedef void (*media_picture_fn)(gpointer data, const struct webos_media_picture *pic,
                                 GstBuffer *bytes);
/* Reads the host's answers until the LAST one to input, giving each of input's pictures to fn
 * (answers to other inputs are dropped). FALSE: no LAST answer within timeout_ms (or the host
 * went away, or the port is flushing); then a later call drops the late ones. */
MEDIAPORT_API gboolean media_port_answers(struct media_port *p, guint32 input, gint timeout_ms,
                                          media_picture_fn fn, gpointer data);
/* The sinks' lead (WEBOS_MEDIA_LEAD_NS) is their own, not the pipeline's. A sink class installs its
 * own ts-offset property in place of GstBaseSink's (media_lead_install), whose value its port keeps
 * (0 at first: GstBaseSink's default): what the pipeline sets (playsink's av-offset, an app's A/V
 * offset) shifts the sink, and the times the host presents at, by exactly that, on top of the
 * lead. The instance's init calls media_lead_init after media_port_init, the class's set and get
 * media_lead_set and media_lead_get; an EOS, which waits for its own time, turns the lead off
 * while it waits (media_lead_suspend). */
MEDIAPORT_API void media_lead_install(GObjectClass *klass, guint prop_id);
MEDIAPORT_API void media_lead_init(struct media_port *p, GstBaseSink *sink);
MEDIAPORT_API void media_lead_set(struct media_port *p, GstBaseSink *sink, const GValue *value);
MEDIAPORT_API void media_lead_get(struct media_port *p, GValue *value);
MEDIAPORT_API void media_lead_suspend(struct media_port *p, GstBaseSink *sink, gboolean off);
/* Signed running time of ts in segment (TIME format), WEBOS_MEDIA_NONE if not computable. */
MEDIAPORT_API gint64 media_running_time(const GstSegment *segment, GstClockTime ts);
#endif

#endif
