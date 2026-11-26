// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The sinks' end of the webos.media.<n> virtio-serial ports (include/webos-media.h).
 *
 * Built with MEDIAPORT_CORE, it is the part without GStreamer (open, send, close: mediaport.h),
 * for a writer that is not a sink: the HAL's direct decoders (guest/layers/soc/src/libhal_direct).
 *
 * A sink pad takes the first free port when it starts. The port is nonblocking. Whether the
 * host end is connected is the port's POLLHUP (virtio_console reports the host's
 * VIRTIO_CONSOLE_PORT_OPEN; QEMU clears it while no viewer is on the chardev socket): without
 * a host every message is dropped and the sink carries on in real time, as the milestone 1 stub
 * did; when the host end appears again, the session is sent again from OPEN (with the caps and
 * the state) and video waits for a keyframe. A message is written whole: a slow host is
 * backpressure (the sink's streaming thread waits for the port), and unlock() only stops a
 * message from starting, so the host's stream never has a hole in the middle of a message,
 * except when the host goes away or stops reading for WRITE_STALL. */
#define _GNU_SOURCE
#include "mediaport.h"
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/uio.h>
#include <syslog.h>
#include <unistd.h>

#define WRITE_STALL_MS 5000
#define KEYFRAME_WAIT  300 /* buffers: give up waiting for a keyframe flag after these */

/* A pool session (include/webos-media.h, "The pool"): its control extent, mapped once POOL named
 * it. A shared picture's memory gives its slot back on the release ring when it is freed, on
 * whatever thread that is and possibly after its element: it holds this, which keeps the rings
 * while the session that sent the picture lasts. The picture ring is read by the thread waiting
 * for answers only (media_port_answers). */
struct media_shared {
    gint ref;
    GMutex lock;
    struct media_port *p;                /* NULL once that session stopped */
    struct webos_media_control *control; /* NULL: no pool session (yet) */
    gsize control_size;
    guint32 pic_tail, rel_head; /* the guest's own indices */
};

static void control_unmap(struct media_shared *s)
{
    if (s->control)
        munmap(s->control, s->control_size);
    s->control = NULL;
    s->control_size = 0;
}

#ifndef MEDIAPORT_CORE
static struct media_shared *shared_ref(struct media_shared *s)
{
    g_atomic_int_inc(&s->ref);
    return s;
}
#endif

static void shared_unref(struct media_shared *s)
{
    if (g_atomic_int_dec_and_test(&s->ref)) {
        control_unmap(s);
        g_mutex_clear(&s->lock);
        g_free(s);
    }
}

/* The session ends: memory still out there no longer names the port. */
static void shared_detach(struct media_port *p)
{
    if (!p->shared)
        return;
    g_mutex_lock(&p->shared->lock);
    p->shared->p = NULL;
    g_mutex_unlock(&p->shared->lock);
    shared_unref(p->shared);
    p->shared = NULL;
}

void media_port_init(struct media_port *p)
{
    memset(p, 0, sizeof *p);
    g_mutex_init(&p->lock);
    p->fd = -1;
    p->pool_fd = -1;
    p->untimed_next = WEBOS_MEDIA_NONE;
}

void media_port_clear(struct media_port *p)
{
    shared_detach(p);
    if (p->pool_fd >= 0)
        close(p->pool_fd);
    p->pool_fd = -1;
    if (p->fd >= 0)
        close(p->fd);
    p->fd = -1;
    g_free(p->caps);
    p->caps = NULL;
    g_mutex_clear(&p->lock);
}

/* The first /dev/lg/webos.media.<n> this process can open (EBUSY: another sink has it): the
 * initramfs makes them there, where a native app's jail has them too (guest/initramfs/rootfs/init).
 */
static int open_port(int *index)
{
    for (int want = 0; want < WEBOS_MEDIA_PORTS; want++) {
        char dev[64];
        snprintf(dev, sizeof dev, "/dev/lg/" WEBOS_MEDIA_PORT_NAME "%d", want);
        int fd = open(dev, O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (fd >= 0) {
            *index = want;
            return fd;
        }
    }
    return -1;
}

#ifndef MEDIAPORT_CORE
static gint64 pipeline_clock(GstElement *element)
{
    GstClock *clock;
    gint64 now = WEBOS_MEDIA_NONE;
    if (!element)
        return now;
    GST_OBJECT_LOCK(element);
    clock = GST_ELEMENT_CLOCK(element);
    if (clock)
        gst_object_ref(clock);
    GST_OBJECT_UNLOCK(element);
    if (clock) {
        GstClockTime t = gst_clock_get_time(clock);
        if (GST_CLOCK_TIME_IS_VALID(t))
            now = (gint64)t;
        gst_object_unref(clock);
    }
    return now;
}

static gint64 element_clock(gpointer element)
{
    return pipeline_clock(element);
}
#endif

/* The session's clock (webos_media_header.clock) at clk, the caller's: the pipeline's for a sink */
static gint64 port_clock(struct media_port *p, gpointer clk)
{
    return p->clock ? p->clock(clk) : WEBOS_MEDIA_NONE;
}

static int host_gone(int fd)
{
    struct pollfd pfd = {.fd = fd, .events = POLLOUT};
    return poll(&pfd, 1, 0) >= 0 && (pfd.revents & (POLLHUP | POLLERR));
}

/* Writes one message whole; with p->lock held. FALSE: dropped (no host, flushing before the
 * start of an abortable one, i.e. a buffer, or the host went away or stalled in the middle). */
static gboolean send_locked(struct media_port *p, gpointer clk, guint16 type, const void *a,
                            size_t alen, const void *b, size_t blen, gboolean abortable)
{
    struct webos_media_header h = {
        .magic = WEBOS_MEDIA_MAGIC,
        .type = type,
        .size = (guint32)(alen + blen),
        .sequence = ++p->sequence,
    };
    struct iovec iov[3] = {{&h, sizeof h}, {(void *)a, alen}, {(void *)b, blen}};
    size_t total = sizeof h + alen + blen, done = 0;
    gint64 stalled_since = 0;
    h.clock = port_clock(p, clk);
    h.mono = g_get_monotonic_time() * 1000;
    h.check = webos_media_check(&h);
    while (done < total) {
        struct iovec part[3];
        int n = 0;
        size_t skip = done;
        for (int i = 0; i < 3; i++) {
            if (skip >= iov[i].iov_len) {
                skip -= iov[i].iov_len;
                continue;
            }
            part[n].iov_base = (char *)iov[i].iov_base + skip;
            part[n].iov_len = iov[i].iov_len - skip;
            skip = 0;
            n++;
        }
        ssize_t w = writev(p->fd, part, n);
        if (w > 0) {
            done += (size_t)w;
            stalled_since = 0;
            continue;
        }
        if (w < 0 && errno == EINTR)
            continue;
        if (w < 0 && errno != EAGAIN)
            break;
        if (!done && abortable && g_atomic_int_get(&p->flushing))
            break;
        struct pollfd pfd = {.fd = p->fd, .events = POLLOUT};
        if (poll(&pfd, 1, 100) < 0 && errno != EINTR)
            break;
        if (pfd.revents & (POLLHUP | POLLERR)) {
            p->host = FALSE;
            break;
        }
        if (!(pfd.revents & POLLOUT)) {
            gint64 now = g_get_monotonic_time();
            if (!stalled_since)
                stalled_since = now;
            else if (now - stalled_since > WRITE_STALL_MS * 1000) {
                syslog(LOG_WARNING,
                       "media-port %d: host stalled, message %u dropped after %zu of %zu bytes",
                       p->index, h.sequence, done, total);
                p->host = FALSE;
                break;
            }
        }
    }
    if (done < total) {
        p->dropped++;
        if (done)
            p->resync = TRUE; /* the host is resynchronising on the next header anyway */
        return FALSE;
    }
    p->sent++;
    p->bytes += total;
    return TRUE;
}

/* With p->lock held: is a host there to send to? Sends the session preamble first when it has
 * just (re)appeared. */
static gboolean ready_locked(struct media_port *p, gpointer clk)
{
    if (p->fd < 0)
        return FALSE;
    if (host_gone(p->fd)) {
        if (p->host)
            syslog(LOG_INFO, "media-port %d: host end closed", p->index);
        p->host = FALSE;
        p->resync = TRUE;
        return FALSE;
    }
    if (!p->host || p->resync) {
        gboolean again = p->sent > 0 || p->dropped > 0;
        p->host = TRUE;
        p->resync = FALSE;
        if (!send_locked(p, clk, WEBOS_MEDIA_OPEN, &p->open, sizeof p->open, NULL, 0, FALSE)) {
            p->resync = TRUE;
            return FALSE;
        }
        if (p->decode) {
            struct webos_media_decode d = {.format = p->decode,
                                           .flags = p->shared ? WEBOS_MEDIA_DECODE_SHARED : 0};
            send_locked(p, clk, WEBOS_MEDIA_DECODE, &d, sizeof d, NULL, 0, FALSE);
        }
        if (p->caps)
            send_locked(p, clk, WEBOS_MEDIA_CAPS, p->caps, strlen(p->caps) + 1, NULL, 0, FALSE);
        if (p->have_state)
            send_locked(p, clk, WEBOS_MEDIA_STATE, &p->state, sizeof p->state, NULL, 0, FALSE);
        if (again && p->open.kind == WEBOS_MEDIA_VIDEO)
            p->need_keyframe = TRUE;
        syslog(LOG_INFO, "media-port %d: %s %s session to the host", p->index,
               again ? "resent" : "started", p->open.element);
    }
    return TRUE;
}

gboolean media_port_open(struct media_port *p, const char *name, guint32 kind, gint32 channel,
                         guint32 pad, media_clock_fn clock, gpointer clk)
{
    shared_detach(p); /* before p->lock: a release takes the shared lock, then p->lock */
    g_mutex_lock(&p->lock);
    if (p->fd < 0)
        p->fd = open_port(&p->index);
    if (p->fd < 0) {
        g_mutex_unlock(&p->lock);
        syslog(LOG_WARNING, "media-port: no free %s<n> port for %s: its data is dropped",
               WEBOS_MEDIA_PORT_NAME, name);
        return FALSE;
    }
    /* the pool, where the VM has it (include/webos-media.h, "The pool"): claimed for the port */
    if (p->pool_fd < 0) {
        guint32 port = (guint32)p->index;
        p->pool_fd = open(WEBOS_MEDIA_POOL_DEVICE, O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (p->pool_fd >= 0 && ioctl(p->pool_fd, WEBOS_MEDIA_IOC_CLAIM, &port) < 0) {
            syslog(LOG_WARNING, "media-port %d: the pool not claimed: %s", p->index,
                   strerror(errno));
            close(p->pool_fd);
            p->pool_fd = -1;
        }
    }
    if (p->pool_fd >= 0) {
        p->shared = g_new0(struct media_shared, 1);
        p->shared->ref = 1;
        g_mutex_init(&p->shared->lock);
        p->shared->p = p;
    }
    p->shared_pictures = p->inline_pictures = 0;
    memset(&p->open, 0, sizeof p->open);
    p->open.kind = kind;
    p->open.pid = getpid();
    p->open.channel = channel;
    p->open.pad = pad;
    g_strlcpy(p->open.element, name, sizeof p->open.element);
    p->clock = clock;
    p->sequence = 0;
    p->host = FALSE;
    p->resync = TRUE;
    p->have_state = FALSE;
    p->need_keyframe = FALSE;
    p->decode = 0;
    p->sent = p->dropped = p->bytes = 0;
    g_free(p->caps);
    p->caps = NULL;
    ready_locked(p, clk);
    g_mutex_unlock(&p->lock);
    syslog(LOG_INFO, "media-port %d: %s (%s, channel %d, pid %d)", p->index, p->open.element,
           kind == WEBOS_MEDIA_VIDEO ? "video" : "audio", channel, (int)p->open.pid);
    return TRUE;
}

#ifndef MEDIAPORT_CORE
gboolean media_port_start(struct media_port *p, GstElement *element, guint32 kind, gint32 channel,
                          guint32 pad)
{
    return media_port_open(p, GST_OBJECT_NAME(element), kind, channel, pad, element_clock, element);
}
#endif

void media_port_stop(struct media_port *p, gpointer clk)
{
    shared_detach(p); /* before p->lock: a release takes the shared lock, then p->lock */
    g_mutex_lock(&p->lock);
    if (p->fd >= 0) {
        if (ready_locked(p, clk))
            send_locked(p, clk, WEBOS_MEDIA_CLOSE, NULL, 0, NULL, 0, FALSE);
        syslog(LOG_INFO,
               "media-port %d: %s closed: %" G_GUINT64_FORMAT " messages, %" G_GUINT64_FORMAT
               " bytes sent, %" G_GUINT64_FORMAT " dropped",
               p->index, p->open.element, p->sent, p->bytes, p->dropped);
        if (p->decode)
            syslog(LOG_INFO,
                   "media-port %d: %s: %" G_GUINT64_FORMAT " pictures shared, %" G_GUINT64_FORMAT
                   " inline",
                   p->index, p->open.element, p->shared_pictures, p->inline_pictures);
        close(p->fd);
        p->fd = -1;
        if (p->pool_fd >= 0)
            close(p->pool_fd); /* the claim ends: the host takes the session's pool back */
        p->pool_fd = -1;
    }
    g_free(p->caps);
    p->caps = NULL;
    g_mutex_unlock(&p->lock);
}

void media_port_set_output(struct media_port *p, guint32 output)
{
    g_mutex_lock(&p->lock);
    if (p->open.output != output) {
        p->open.output = output;
        p->resync = TRUE;
    }
    g_mutex_unlock(&p->lock);
}

void media_port_set_channel(struct media_port *p, gint32 channel)
{
    g_mutex_lock(&p->lock);
    if (p->fd >= 0 && p->open.channel != channel) {
        p->open.channel = channel;
        p->resync = TRUE;
    }
    g_mutex_unlock(&p->lock);
}

void media_port_caps_string(struct media_port *p, gpointer clk, const char *caps,
                            gint64 bytes_per_second)
{
    g_mutex_lock(&p->lock);
    if (p->caps && !strcmp(p->caps, caps)) {
        g_mutex_unlock(&p->lock);
        return;
    }
    g_free(p->caps);
    /* not g_strdup: the targets' GLib link sets (guest/targets/facts) have g_new0 and g_strlcpy */
    gsize n = strlen(caps) + 1;
    p->caps = g_new0(gchar, n);
    g_strlcpy(p->caps, caps, n);
    p->bytes_per_second = bytes_per_second;
    if (ready_locked(p, clk))
        send_locked(p, clk, WEBOS_MEDIA_CAPS, p->caps, strlen(p->caps) + 1, NULL, 0, FALSE);
    g_mutex_unlock(&p->lock);
}

gboolean media_port_send(struct media_port *p, gpointer clk, const struct webos_media_buffer *b,
                         const void *data, gsize size)
{
    gboolean sent;
    g_mutex_lock(&p->lock);
    if (!ready_locked(p, clk)) {
        p->dropped++;
        g_mutex_unlock(&p->lock);
        return FALSE;
    }
    if (p->need_keyframe) {
        if (!(b->flags & WEBOS_MEDIA_KEYFRAME) && p->dropped % KEYFRAME_WAIT != KEYFRAME_WAIT - 1) {
            p->dropped++;
            g_mutex_unlock(&p->lock);
            return FALSE;
        }
        p->need_keyframe = FALSE;
    }
    sent = send_locked(p, clk, WEBOS_MEDIA_BUFFER, b, sizeof *b, data, size, TRUE);
    g_mutex_unlock(&p->lock);
    return sent;
}

void media_port_send_state(struct media_port *p, gpointer clk, const struct webos_media_state *s)
{
    g_mutex_lock(&p->lock);
    p->state = *s;
    p->have_state = TRUE;
    if (ready_locked(p, clk))
        send_locked(p, clk, WEBOS_MEDIA_STATE, s, sizeof *s, NULL, 0, FALSE);
    g_mutex_unlock(&p->lock);
}

void media_port_event(struct media_port *p, gpointer clk, guint16 type)
{
    g_mutex_lock(&p->lock);
    if (type == WEBOS_MEDIA_FLUSH)
        p->untimed_next = WEBOS_MEDIA_NONE;
    if (ready_locked(p, clk))
        send_locked(p, clk, type, NULL, 0, NULL, 0, FALSE);
    g_mutex_unlock(&p->lock);
}

void media_port_set_flushing(struct media_port *p, gboolean flushing)
{
    g_atomic_int_set(&p->flushing, flushing);
}

#ifndef MEDIAPORT_CORE
/* Raw audio's bytes a second (its rate, channels and sample size), 0 for anything else. */
static gint64 raw_audio_rate(GstCaps *caps)
{
    const GstStructure *st = gst_caps_get_size(caps) ? gst_caps_get_structure(caps, 0) : NULL;
    const gchar *format;
    gint rate = 0, channels = 0, width;
    if (!st || strcmp(gst_structure_get_name(st), "audio/x-raw") ||
        !(format = gst_structure_get_string(st, "format")) ||
        !gst_structure_get_int(st, "rate", &rate) ||
        !gst_structure_get_int(st, "channels", &channels) || rate <= 0 || channels <= 0)
        return 0;
    if (g_str_has_prefix(format, "S8") || g_str_has_prefix(format, "U8"))
        width = 1;
    else if (g_str_has_prefix(format, "S16") || g_str_has_prefix(format, "U16"))
        width = 2;
    else if (g_str_has_prefix(format, "S24_32") || g_str_has_prefix(format, "U24_32") ||
             g_str_has_prefix(format, "S32") || g_str_has_prefix(format, "U32") ||
             g_str_has_prefix(format, "F32"))
        width = 4;
    else if (g_str_has_prefix(format, "S24") || g_str_has_prefix(format, "U24"))
        width = 3;
    else if (g_str_has_prefix(format, "F64"))
        width = 8;
    else
        return 0;
    return (gint64)rate * channels * width;
}

void media_port_caps(struct media_port *p, GstElement *element, GstCaps *caps)
{
    gchar *s = gst_caps_to_string(caps);
    media_port_caps_string(p, element, s, raw_audio_rate(caps));
    g_free(s);
}

/* The lead used to be the sink's ts-offset itself (-WEBOS_MEDIA_LEAD_NS), which is also the
 * pipeline's to set: playbin's playsink sets it on its audio and video sinks (to its av-offset, 0
 * by default) whenever it links them, which took the whole lead away from <video> file playback
 * (W16R: 0 ms ahead instead of 300). Here the property keeps the pipeline's value and GstBaseSink
 * syncs on that less the lead. The class's pspec stands in for GstBaseSink's (a property lookup
 * finds the most derived one), as g_object_class_override_property would, which not every
 * firmware's GObject has been asked for; GValue's int64 is read as g_value_get_int64 does, for
 * the same reason. */
void media_lead_install(GObjectClass *klass, guint prop_id)
{
    g_object_class_install_property(
        klass, prop_id,
        g_param_spec_int64("ts-offset", "TS Offset", "Timestamp offset in nanoseconds", G_MININT64,
                           G_MAXINT64, 0, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
}

void media_lead_init(struct media_port *p, GstBaseSink *sink)
{
    p->ts_offset = 0;
    gst_base_sink_set_ts_offset(sink, -WEBOS_MEDIA_LEAD_NS);
}

static gint64 lead_offset(struct media_port *p)
{
    gint64 v;
    g_mutex_lock(&p->lock);
    v = p->ts_offset;
    g_mutex_unlock(&p->lock);
    return v;
}

void media_lead_set(struct media_port *p, GstBaseSink *sink, const GValue *value)
{
    gint64 v = value->data[0].v_int64;
    g_mutex_lock(&p->lock);
    p->ts_offset = v;
    g_mutex_unlock(&p->lock);
    gst_base_sink_set_ts_offset(sink, v - WEBOS_MEDIA_LEAD_NS);
    GST_DEBUG_OBJECT(sink, "ts-offset %" G_GINT64_FORMAT " ns, with the lead %" G_GINT64_FORMAT, v,
                     (gint64)(v - WEBOS_MEDIA_LEAD_NS));
}

void media_lead_get(struct media_port *p, GValue *value)
{
    g_value_set_int64(value, lead_offset(p));
}

void media_lead_suspend(struct media_port *p, GstBaseSink *sink, gboolean off)
{
    gint64 v = lead_offset(p);
    gst_base_sink_set_ts_offset(sink, off ? v : v - WEBOS_MEDIA_LEAD_NS);
}

gint64 media_running_time(const GstSegment *segment, GstClockTime ts)
{
    if (!GST_CLOCK_TIME_IS_VALID(ts) || segment->format != GST_FORMAT_TIME)
        return WEBOS_MEDIA_NONE;
    gdouble rate = segment->rate < 0 ? -segment->rate : segment->rate;
    gint64 d;
    if (segment->rate > 0)
        d = (gint64)ts - (gint64)(segment->start + segment->offset);
    else if (GST_CLOCK_TIME_IS_VALID(segment->stop))
        d = (gint64)(segment->stop - segment->offset) - (gint64)ts;
    else
        return WEBOS_MEDIA_NONE;
    if (rate != 1.0 && rate > 0)
        d = (gint64)(d / rate);
    return d + (gint64)segment->base;
}

/* A sink with sync off (LG's player factory turns it off on both sinks of NDL DirectMedia, whose
 * apps pass timestamps in units of their own): the stock sinks then show a frame and play a sample
 * as it comes. Video goes without a time, which the host shows at once. Audio, which the host
 * places by time, starts at its arrival in running time and each buffer follows the one before,
 * unless it comes later than that (an underrun). */
static void untimed_locked(struct media_port *p, GstBaseSink *sink, GstBuffer *buf,
                           struct webos_media_buffer *b)
{
    gint64 now = pipeline_clock(GST_ELEMENT(sink)), base, duration = WEBOS_MEDIA_NONE;
    b->pts = b->dts = WEBOS_MEDIA_NONE;
    if (p->open.kind != WEBOS_MEDIA_AUDIO)
        return;
    GST_OBJECT_LOCK(sink);
    base = (gint64)GST_ELEMENT_CAST(sink)->base_time;
    GST_OBJECT_UNLOCK(sink);
    if (now == WEBOS_MEDIA_NONE) {
        p->untimed_next = WEBOS_MEDIA_NONE;
        return;
    }
    now -= base;
    b->pts = p->untimed_next != WEBOS_MEDIA_NONE && p->untimed_next > now ? p->untimed_next : now;
    if (p->bytes_per_second > 0)
        duration = (gint64)gst_buffer_get_size(buf) * GST_SECOND / p->bytes_per_second;
    else if (GST_BUFFER_DURATION_IS_VALID(buf))
        duration = (gint64)GST_BUFFER_DURATION(buf);
    b->duration = duration;
    p->untimed_next = duration != WEBOS_MEDIA_NONE ? b->pts + duration : WEBOS_MEDIA_NONE;
}

void media_port_buffer(struct media_port *p, GstBaseSink *sink, GstBuffer *buf, guint32 flags)
{
    struct webos_media_buffer b = {0};
    GstSegment segment;
    GstMapInfo map;
    gboolean sync;
    GST_OBJECT_LOCK(sink);
    segment = sink->segment;
    GST_OBJECT_UNLOCK(sink);
    b.pts = media_running_time(&segment, GST_BUFFER_PTS(buf));
    b.dts = media_running_time(&segment, GST_BUFFER_DTS(buf));
    b.duration =
        GST_BUFFER_DURATION_IS_VALID(buf) ? (gint64)GST_BUFFER_DURATION(buf) : WEBOS_MEDIA_NONE;
    b.stream_time = WEBOS_MEDIA_NONE;
    if (GST_BUFFER_PTS_IS_VALID(buf) && segment.format == GST_FORMAT_TIME) {
        guint64 st = gst_segment_to_stream_time(&segment, GST_FORMAT_TIME, GST_BUFFER_PTS(buf));
        if (st != (guint64)-1)
            b.stream_time = (gint64)st;
    }
    if (!GST_BUFFER_FLAG_IS_SET(buf, GST_BUFFER_FLAG_DELTA_UNIT))
        flags |= WEBOS_MEDIA_KEYFRAME;
    if (GST_BUFFER_FLAG_IS_SET(buf, GST_BUFFER_FLAG_DISCONT))
        flags |= WEBOS_MEDIA_DISCONT;
    b.flags = flags;
    /* the sink's "sync", as gst_base_sink_get_sync reads it (not every firmware exports that) */
    GST_OBJECT_LOCK(sink);
    sync = sink->sync;
    GST_OBJECT_UNLOCK(sink);
    g_mutex_lock(&p->lock);
    if (!sync && !(flags & WEBOS_MEDIA_DECODE_ONLY)) {
        untimed_locked(p, sink, buf, &b);
    } else {
        p->untimed_next = WEBOS_MEDIA_NONE;
        /* a stock sink renders at its buffer's time + ts-offset: the host presents there */
        if (b.pts != WEBOS_MEDIA_NONE)
            b.pts += p->ts_offset;
        if (b.dts != WEBOS_MEDIA_NONE)
            b.dts += p->ts_offset;
    }
    g_mutex_unlock(&p->lock);
    if (gst_buffer_map(buf, &map, GST_MAP_READ)) {
        media_port_send(p, sink, &b, map.data, map.size);
        gst_buffer_unmap(buf, &map);
    }
}

void media_port_state(struct media_port *p, GstElement *element, gboolean playing)
{
    struct webos_media_state s = {0};
    gint64 now = pipeline_clock(element);
    s.playing = playing;
    GST_OBJECT_LOCK(element);
    s.base_time = (gint64)GST_ELEMENT_CAST(element)->base_time;
    if (GST_IS_BASE_SINK(element))
        s.rate = GST_BASE_SINK(element)->segment.rate;
    GST_OBJECT_UNLOCK(element);
    s.pause_rt = !playing && now != WEBOS_MEDIA_NONE ? now - s.base_time : WEBOS_MEDIA_NONE;
    g_mutex_lock(&p->lock);
    /* A pipeline that never played has no clock yet: a pause there is at running time 0. */
    if (!playing && s.pause_rt == WEBOS_MEDIA_NONE && p->have_state && p->state.playing == 0)
        s.pause_rt = p->state.pause_rt;
    p->state = s;
    p->have_state = TRUE;
    if (ready_locked(p, element))
        send_locked(p, element, WEBOS_MEDIA_STATE, &s, sizeof s, NULL, 0, FALSE);
    g_mutex_unlock(&p->lock);
}

/* ---- decoding for the guest ---- */

void media_port_decode(struct media_port *p, GstElement *element, guint32 format)
{
    /* samples come inline (include/webos-media.h) */
    struct webos_media_decode d = {
        .format = format,
        .flags = p->shared && format != WEBOS_MEDIA_S16LE ? WEBOS_MEDIA_DECODE_SHARED : 0};
    char junk[4096];
    g_mutex_lock(&p->lock);
    /* answers to an earlier session of this port, which nobody read */
    while (p->fd >= 0 && read(p->fd, junk, sizeof junk) > 0)
        ;
    p->decode = format;
    if (ready_locked(p, element))
        send_locked(p, element, WEBOS_MEDIA_DECODE, &d, sizeof d, NULL, 0, FALSE);
    g_mutex_unlock(&p->lock);
    if (p->shared) { /* the host answers this DECODE with a new POOL */
        g_mutex_lock(&p->shared->lock);
        control_unmap(p->shared);
        g_mutex_unlock(&p->shared->lock);
    }
}

guint32 media_port_send_input(struct media_port *p, GstElement *element, GstBuffer *buf)
{
    struct webos_media_buffer b = {0};
    GstMapInfo map;
    guint32 seq = 0;
    if (buf) {
        b.pts = GST_BUFFER_PTS_IS_VALID(buf) ? (gint64)GST_BUFFER_PTS(buf) : WEBOS_MEDIA_NONE;
        b.dts = GST_BUFFER_DTS_IS_VALID(buf) ? (gint64)GST_BUFFER_DTS(buf) : WEBOS_MEDIA_NONE;
        b.duration =
            GST_BUFFER_DURATION_IS_VALID(buf) ? (gint64)GST_BUFFER_DURATION(buf) : WEBOS_MEDIA_NONE;
        b.stream_time = b.pts;
        if (!GST_BUFFER_FLAG_IS_SET(buf, GST_BUFFER_FLAG_DELTA_UNIT))
            b.flags |= WEBOS_MEDIA_KEYFRAME;
        if (GST_BUFFER_FLAG_IS_SET(buf, GST_BUFFER_FLAG_DISCONT))
            b.flags |= WEBOS_MEDIA_DISCONT;
        if (!gst_buffer_map(buf, &map, GST_MAP_READ))
            return 0;
    }
    g_mutex_lock(&p->lock);
    if (!ready_locked(p, element))
        p->dropped++;
    else if (buf ? send_locked(p, element, WEBOS_MEDIA_BUFFER, &b, sizeof b, map.data, map.size,
                               TRUE)
                 : send_locked(p, element, WEBOS_MEDIA_EOS, NULL, 0, NULL, 0, FALSE))
        seq = p->sequence;
    g_mutex_unlock(&p->lock);
    if (buf)
        gst_buffer_unmap(buf, &map);
    return seq;
}

/* n bytes from the port into out (NULL: dropped) by deadline (µs, monotonic); between messages
 * (abortable) a flush stops the wait too. */
static gboolean read_exact(struct media_port *p, void *out, size_t n, gint64 deadline,
                           gboolean abortable)
{
    char junk[4096];
    size_t done = 0;
    while (done < n) {
        size_t want = n - done;
        void *at = out ? (char *)out + done : junk;
        if (!out && want > sizeof junk)
            want = sizeof junk;
        ssize_t r = read(p->fd, at, want);
        p->reads++;
        if (r > 0) {
            done += (size_t)r;
            continue;
        }
        if (r == 0 || (r < 0 && errno != EAGAIN && errno != EINTR))
            return FALSE;
        if (r < 0 && errno == EINTR)
            continue;
        if (!done && abortable && g_atomic_int_get(&p->flushing))
            return FALSE;
        gint64 left = deadline - g_get_monotonic_time();
        if (left <= 0)
            return FALSE;
        struct pollfd pfd = {.fd = p->fd, .events = POLLIN};
        if (poll(&pfd, 1, left > 100000 ? 100 : (int)(left / 1000) + 1) < 0 && errno != EINTR)
            return FALSE;
        if ((pfd.revents & (POLLHUP | POLLERR)) && !(pfd.revents & POLLIN))
            return FALSE;
    }
    return TRUE;
}

/* A shared picture's memory: the slot's dma-buf, mapped. */
struct shared_picture {
    struct media_shared *shared;
    void *data;
    gsize size;
    int fd;
    struct webos_media_release release;
};

/* The slot back to the host on the session's release ring; the host rung only when it waits for
 * one (the flag after a full barrier, as include/webos-media.h says). A ring the host has not
 * emptied, or a session that has ended, keeps it: the host takes the extent back anyway. */
static void send_release(struct media_shared *shared, const struct webos_media_release *r)
{
    g_mutex_lock(&shared->lock);
    struct media_port *p = shared->p;
    struct webos_media_control *c = shared->control;
    if (p && c) {
        guint32 tail = __atomic_load_n(&c->releases.tail, __ATOMIC_ACQUIRE);
        if (shared->rel_head - tail < WEBOS_MEDIA_RING_ENTRIES) {
            c->release[shared->rel_head % WEBOS_MEDIA_RING_ENTRIES] = *r;
            shared->rel_head++;
            __atomic_store_n(&c->releases.head, shared->rel_head, __ATOMIC_RELEASE);
            __atomic_thread_fence(__ATOMIC_SEQ_CST);
            if (__atomic_load_n(&c->releases.waiting, __ATOMIC_RELAXED)) {
                g_mutex_lock(&p->lock);
                if (p->pool_fd >= 0)
                    ioctl(p->pool_fd, WEBOS_MEDIA_IOC_RING);
                g_mutex_unlock(&p->lock);
            }
        }
    }
    g_mutex_unlock(&shared->lock);
}

static void shared_picture_free(gpointer data)
{
    struct shared_picture *sp = data;
    munmap(sp->data, sp->size);
    close(sp->fd);
    send_release(sp->shared, &sp->release);
    shared_unref(sp->shared);
    g_free(sp);
}

/* The bytes of a picture laid out as include/webos-media.h says. */
static gsize picture_bytes(const struct webos_media_picture *pic)
{
    if (pic->format == WEBOS_MEDIA_YUY2)
        return (gsize)pic->stride[0] * pic->height;
    return (gsize)pic->offset[1] + (gsize)pic->stride[1] * ((pic->height + 1) / 2);
}

/* A range of the pool, exported and mapped: its address (*fd the dma-buf), or MAP_FAILED. */
static void *pool_map(struct media_port *p, guint32 offset, gsize size, int *fd)
{
    struct webos_media_export e = {.offset = offset, .size = (guint32)size, .fd = -1};
    void *data = MAP_FAILED;
    *fd = -1;
    if (p->pool_fd >= 0 && size && size <= G_MAXUINT32 &&
        ioctl(p->pool_fd, WEBOS_MEDIA_IOC_EXPORT, &e) == 0) {
        *fd = e.fd;
        data = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, e.fd, 0);
        if (data == MAP_FAILED) {
            close(e.fd);
            *fd = -1;
        }
    }
    return data;
}

/* A shared picture as a buffer over its slot; NULL (the slot given back) if it cannot be mapped. */
static GstBuffer *shared_picture(struct media_port *p, const struct webos_media_picture *pic)
{
    gsize size = picture_bytes(pic);
    struct webos_media_release r = {.slot = pic->slot, .input = pic->input};
    int fd;
    void *data = pool_map(p, pic->slot, size, &fd);
    if (data == MAP_FAILED) {
        static gboolean warned;
        if (!warned) {
            warned = TRUE;
            syslog(LOG_WARNING, "media-port %d: shared picture at %u not mapped: %s", p->index,
                   pic->slot, strerror(errno));
        }
        if (p->shared)
            send_release(p->shared, &r);
        return NULL;
    }
    struct shared_picture *sp = g_new0(struct shared_picture, 1);
    sp->shared = shared_ref(p->shared);
    sp->data = data;
    sp->size = size;
    sp->fd = fd;
    sp->release = r;
    p->shared_pictures++;
    return gst_buffer_new_wrapped_full(0, data, size, 0, size, sp, shared_picture_free);
}

/* POOL: the session's control extent mapped (size 0: none, the answers come inline). */
static void pool_answer(struct media_port *p, const struct webos_media_pool *a)
{
    if (!p->shared)
        return;
    int fd = -1;
    void *c = a->size >= sizeof(struct webos_media_control) ? pool_map(p, a->offset, a->size, &fd)
                                                            : MAP_FAILED;
    if (fd >= 0)
        close(fd); /* the mapping keeps the export */
    if (a->size && c == MAP_FAILED)
        syslog(LOG_WARNING, "media-port %d: the pool's control at %u not mapped: %s", p->index,
               a->offset, strerror(errno));
    g_mutex_lock(&p->shared->lock);
    control_unmap(p->shared);
    if (c != MAP_FAILED) {
        p->shared->control = c;
        p->shared->control_size = a->size;
        p->shared->pic_tail = p->shared->rel_head = 0;
    }
    g_mutex_unlock(&p->shared->lock);
}

/* One answer, from the ring (shared) or the port: to fn if it is input's. TRUE: input's LAST. */
static gboolean take_answer(struct media_port *p, guint32 input,
                            const struct webos_media_picture *pic, GstBuffer *buf,
                            media_picture_fn fn, gpointer data)
{
    gboolean mine = pic->input == input;
    if (pic->flags & WEBOS_MEDIA_PICTURE_SHARED) {
        if (mine && pic->format == p->decode)
            buf = shared_picture(p, pic);
        else if (p->shared) { /* not wanted: the slot goes back at once */
            struct webos_media_release r = {.slot = pic->slot, .input = pic->input};
            send_release(p->shared, &r);
        }
    }
    if (buf)
        fn(data, pic, buf);
    return mine && (pic->flags & WEBOS_MEDIA_PICTURE_LAST);
}

/* The pictures on the session's ring, in order (only the thread waiting for answers takes them).
 * 1: input's LAST came; 0: the ring is empty; -1: no ring. */
static int drain_ring(struct media_port *p, guint32 input, media_picture_fn fn, gpointer data)
{
    struct media_shared *s = p->shared;
    if (!s)
        return -1;
    for (;;) {
        struct webos_media_picture pic;
        g_mutex_lock(&s->lock);
        struct webos_media_control *c = s->control;
        if (!c) {
            g_mutex_unlock(&s->lock);
            return -1;
        }
        guint32 head = __atomic_load_n(&c->pictures.head, __ATOMIC_ACQUIRE);
        if (head == s->pic_tail) {
            g_mutex_unlock(&s->lock);
            return 0;
        }
        if (head - s->pic_tail > WEBOS_MEDIA_RING_ENTRIES) /* never from a sane host */
            s->pic_tail = head - WEBOS_MEDIA_RING_ENTRIES;
        pic = c->picture[s->pic_tail % WEBOS_MEDIA_RING_ENTRIES];
        s->pic_tail++;
        __atomic_store_n(&c->pictures.tail, s->pic_tail, __ATOMIC_RELEASE);
        g_mutex_unlock(&s->lock);
        if (take_answer(p, input, &pic, NULL, fn, data))
            return 1;
    }
}

/* Sleeps until the port has bytes, the pool rang or deadline (at most 100 ms). The ring's
 * waiting flag is set, then the ring looked at again after a full barrier, and only then the
 * sleep, so a picture the host puts there meanwhile is never missed. FALSE: the host went away. */
static gboolean wait_answers(struct media_port *p, gint64 deadline)
{
    struct pollfd pfd[2] = {{.fd = p->fd, .events = POLLIN}, {.fd = -1, .events = POLLIN}};
    struct media_shared *s = p->shared;
    struct webos_media_control *c = NULL;
    if (s) {
        g_mutex_lock(&s->lock);
        c = s->control;
        if (c && p->pool_fd >= 0) {
            guint32 n;
            if (read(p->pool_fd, &n, sizeof n) < 0) { /* the interrupts so far: seen */
            }
            __atomic_store_n(&c->pictures.waiting, 1, __ATOMIC_RELAXED);
            __atomic_thread_fence(__ATOMIC_SEQ_CST);
            if (__atomic_load_n(&c->pictures.head, __ATOMIC_ACQUIRE) != s->pic_tail) {
                __atomic_store_n(&c->pictures.waiting, 0, __ATOMIC_RELAXED);
                g_mutex_unlock(&s->lock);
                return TRUE;
            }
            pfd[1].fd = p->pool_fd;
        }
        g_mutex_unlock(&s->lock);
    }
    gint64 left = deadline - g_get_monotonic_time();
    int r = poll(pfd, 2, left <= 0 ? 0 : left > 100000 ? 100 : (int)(left / 1000) + 1);
    if (c) {
        g_mutex_lock(&s->lock);
        if (s->control == c)
            __atomic_store_n(&c->pictures.waiting, 0, __ATOMIC_RELAXED);
        g_mutex_unlock(&s->lock);
    }
    if (r < 0 && errno != EINTR)
        return FALSE;
    return !((pfd[0].revents & (POLLHUP | POLLERR)) && !(pfd[0].revents & POLLIN));
}

static gboolean port_readable(struct media_port *p)
{
    struct pollfd pfd = {.fd = p->fd, .events = POLLIN};
    return poll(&pfd, 1, 0) > 0 && (pfd.revents & POLLIN);
}

gboolean media_port_answers(struct media_port *p, guint32 input, gint timeout_ms,
                            media_picture_fn fn, gpointer data)
{
    gint64 deadline = g_get_monotonic_time() + (gint64)timeout_ms * 1000;
    struct webos_media_header h;
    if (p->fd < 0)
        return FALSE;
    for (;;) {
        /* the ring first: what is there was put there before anything the port has now */
        if (drain_ring(p, input, fn, data) == 1)
            return TRUE;
        if (!port_readable(p)) {
            if (g_atomic_int_get(&p->flushing) || g_get_monotonic_time() >= deadline)
                return FALSE;
            if (!wait_answers(p, deadline))
                return FALSE;
            continue;
        }
        struct webos_media_picture pic;
        if (!read_exact(p, &h, sizeof h, deadline, TRUE))
            return FALSE;
        /* not on a header (a reader that gave up in the middle of one): to the next valid one */
        while (h.magic != WEBOS_MEDIA_MAGIC || h.check != webos_media_check(&h) ||
               (h.type != WEBOS_MEDIA_PICTURE && h.type != WEBOS_MEDIA_POOL) ||
               (h.type == WEBOS_MEDIA_PICTURE && h.size < sizeof pic) ||
               (h.type == WEBOS_MEDIA_POOL && h.size != sizeof(struct webos_media_pool)) ||
               h.size > WEBOS_MEDIA_MAX_PAYLOAD) {
            memmove(&h, (char *)&h + 1, sizeof h - 1);
            if (!read_exact(p, (char *)&h + sizeof h - 1, 1, deadline, FALSE))
                return FALSE;
        }
        /* a message once started is read whole, so the next read starts on a header */
        gint64 whole = MAX(deadline, g_get_monotonic_time() + WRITE_STALL_MS * 1000);
        if (h.type == WEBOS_MEDIA_POOL) {
            struct webos_media_pool a;
            if (!read_exact(p, &a, sizeof a, whole, FALSE))
                return FALSE;
            pool_answer(p, &a);
            continue;
        }
        if (!read_exact(p, &pic, sizeof pic, whole, FALSE))
            return FALSE;
        gsize bytes = h.size - sizeof pic;
        gboolean mine = pic.input == input;
        GstBuffer *buf = NULL;
        if (!(pic.flags & WEBOS_MEDIA_PICTURE_SHARED) && mine && bytes && pic.format == p->decode) {
            p->inline_pictures++;
            GstMapInfo map;
            buf = gst_buffer_new_allocate(NULL, bytes, NULL);
            if (!buf || !gst_buffer_map(buf, &map, GST_MAP_WRITE)) {
                if (buf)
                    gst_buffer_unref(buf);
                return FALSE;
            }
            gboolean ok = read_exact(p, map.data, bytes, whole, FALSE);
            gst_buffer_unmap(buf, &map);
            if (!ok) {
                gst_buffer_unref(buf);
                return FALSE;
            }
        } else if (bytes && !read_exact(p, NULL, bytes, whole, FALSE))
            return FALSE;
        if (take_answer(p, input, &pic, buf, fn, data))
            return TRUE;
    }
}
#endif
