// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#define _GNU_SOURCE
/* The helper transport: one reader thread per stream connects the decoded
 * socket, says hello (with the hwdec preference), and queues video pictures
 * and PCM chunks under the stream lock with session/generation fencing. */
#include "media-priv.h"

#include <poll.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

void free_frames(struct stream *s)
{
    for (unsigned i = 0; i < s->nframes; i++)
        frame_free(&s->frames[i]);
    s->nframes = 0;
    for (struct chunk *c = s->ahead, *n; c; c = n) {
        n = c->next;
        free(c);
    }
    s->ahead = s->atail = NULL;
    s->aqueued = 0;
}

void report(struct stream *s, const char *why)
{
    if (s->info.kind == WEBOS_MEDIA_VIDEO)
        fprintf(stderr,
                "present %d video session=%u%s: received=%llu shown=%llu picked=%llu "
                "late_drops=%llu lateness avg=%.1f max=%.1f ms pts=%.3f..%.3f s\n",
                s->index, s->session, why, s->received, s->shown, s->picked, s->late_drops,
                s->late_n ? s->late_sum / s->late_n : 0, s->late_max,
                s->pts_first == NONE ? -1.0 : s->pts_first / 1e9,
                s->pts_last == NONE ? -1.0 : s->pts_last / 1e9);
    else
        fprintf(stderr,
                "present %d audio session=%u%s: frames=%llu slip avg=%.1f max=%.1f ms resyncs=%u "
                "nudges=%u underruns=%u\n",
                s->index, s->session, why, s->audio_frames,
                s->aslip_n ? s->aslip_sum / s->aslip_n : 0, s->aslip_max, s->resyncs, s->nudges,
                s->underruns);
    s->late_sum = s->late_max = 0;
    s->late_n = 0;
    s->aslip_sum = s->aslip_max = 0;
    s->aslip_n = 0;
}

static void update_snapshot(struct stream *s, const struct host_media_message *msg)
{
    if (s->session != msg->session || s->info.kind != msg->open.kind) {
        if (s->open)
            report(s, (msg->flags & HM_OPEN) ? " (replaced)" : " (closed)");
        s->received = s->shown = s->picked = s->late_drops = s->reported_packets = 0;
        s->audio_frames = 0;
        s->resyncs = s->nudges = s->underruns = 0;
        s->pts_first = s->pts_last = NONE;
        s->late_sum = s->late_max = s->aslip_sum = s->aslip_max = 0;
        s->late_n = s->aslip_n = 0;
        s->session = msg->session;
    }
    if (s->generation != msg->generation || !(msg->flags & HM_OPEN)) {
        free_frames(s);
        s->reset_current = 1;
        s->need_first = 1;
        s->generation = msg->generation;
    }
    s->info = msg->open;
    s->state = msg->state;
    s->open = !!(msg->flags & HM_OPEN);
    s->eos = !!(msg->flags & HM_EOS);
    s->have_state = !!(msg->flags & HM_PLAY_STATE);
    s->have_delta = !!(msg->flags & HM_CLOCK);
    s->clock_delta = msg->clock_delta;
    s->offset.cur = s->offset.prev = msg->offset;
    s->offset.start = s->have_delta ? 1 : 0;
}

void *reader(void *arg)
{
    struct stream *s = arg;
    int logged = 0;
    while (!s->m->quit) {
        struct sockaddr_un addr = {.sun_family = AF_UNIX};
        if (strlen(s->path) >= sizeof addr.sun_path)
            break;
        memcpy(addr.sun_path, s->path, strlen(s->path) + 1);
        int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
        if (fd < 0 || connect(fd, (void *)&addr, sizeof addr) < 0) {
            if (fd >= 0)
                close(fd);
            if (!logged++)
                fprintf(stderr,
                        "media %d: decoded helper unavailable at %s (compressed direct sockets are "
                        "unsupported)\n",
                        s->index, s->path);
            for (int i = 0; i < 10 && !s->m->quit; i++)
                usleep(50000);
            continue;
        }
        logged = 0;
        struct host_media_message hello = {
            .magic = HOST_MEDIA_MAGIC, .version = HOST_MEDIA_VERSION, .type = HM_HELLO};
        if (s->m->cfg.hwdec)
            snprintf(hello.preference, sizeof hello.preference, "%s", s->m->cfg.hwdec);
        if (hm_send(fd, &hello, NULL, 0) != 1) {
            close(fd);
            continue;
        }
        while (!s->m->quit) {
            struct pollfd p = {.fd = fd, .events = POLLIN};
            if (poll(&p, 1, 100) <= 0)
                continue;
            struct host_media_message msg;
            int fds[HOST_MEDIA_OBJECTS];
            unsigned nfds;
            int r = hm_receive(fd, &msg, fds, &nfds);
            if (r <= 0) {
                if (r < 0)
                    break;
                continue;
            }
            if (msg.type == HM_ACK) {
                msg.preference[sizeof msg.preference - 1] = 0;
                fprintf(stderr, "media %d: helper hwdec %s preference %s%s\n", s->index,
                        msg.preference,
                        msg.status == HM_PREFERENCE_INVALID  ? "rejected"
                        : msg.status == HM_PREFERENCE_LOCKED ? "refused: software only (hwdec=none)"
                        : msg.status == HM_PREFERENCE_DEFERRED ? "acknowledged for next codec open"
                                                               : "acknowledged",
                        msg.status == HM_PREFERENCE_DEFERRED ? "; active decoder unchanged" : "");
                s->m->no_decoder = !!(msg.flags & HM_DECODER_UNAVAILABLE);
                if (msg.flags & HM_DECODER_UNAVAILABLE)
                    fprintf(stderr, "media %d: helper built without FFmpeg; decoder unavailable\n",
                            s->index);
                hm_close_fds(fds, nfds);
                if (nfds)
                    break;
                continue;
            }
            if (msg.type != HM_STATE && msg.type != HM_VIDEO && msg.type != HM_PCM) {
                hm_close_fds(fds, nfds);
                break;
            }
            struct received_frame *f = NULL;
            if (msg.type != HM_STATE && !(f = frame_receive(fd, &msg, fds, nfds))) {
                hm_close_fds(fds, nfds);
                break;
            }
            if (msg.type == HM_STATE && nfds) {
                hm_close_fds(fds, nfds);
                break;
            }
            pthread_mutex_lock(&s->lock);
            update_snapshot(s, &msg);
            if (f && msg.type == HM_VIDEO) {
                s->received++;
                if (s->nframes < FRAME_QUEUE) {
                    s->frames[s->nframes++] = f;
                    f = NULL;
                } else
                    s->late_drops++;
            } else if (f && msg.type == HM_PCM) {
                int64_t duration = (int64_t)msg.samples * 1000000000 / OUT_RATE;
                if (s->aqueued + duration <= AUDIO_QUEUE_MAX_NS) {
                    struct chunk *c = malloc(sizeof *c + (size_t)msg.samples * 8);
                    if (c) {
                        c->next = NULL;
                        c->rt = msg.pts;
                        c->frames = msg.samples;
                        c->pos = 0;
                        memcpy(c->data, f->mapping, (size_t)msg.samples * 8);
                        if (s->atail)
                            s->atail->next = c;
                        else
                            s->ahead = c;
                        s->atail = c;
                        s->aqueued += duration;
                        s->received++;
                    }
                }
            }
            int audio = s->open && s->info.kind == WEBOS_MEDIA_AUDIO;
            pthread_mutex_unlock(&s->lock);
            frame_free(&f);
            if (audio)
                start_audio(s->m);
        }
        if (s->m->quit) {
            /* Current/queued frames hold release-only duplicates. Main-thread
             * GL retirement sends their releases before the final socket close. */
            close(fd);
            break;
        }
        /* Shutdown, rather than merely close, also invalidates release-only
         * duplicate socket FDs retained by frames from this connection. */
        shutdown(fd, SHUT_RDWR);
        close(fd);
        pthread_mutex_lock(&s->lock);
        free_frames(s);
        s->open = 0;
        s->reset_current = 1;
        pthread_mutex_unlock(&s->lock);
    }
    return NULL;
}
