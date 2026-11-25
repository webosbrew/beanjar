// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#define _DEFAULT_SOURCE
#include "video.h"
#include "media.h"
#include "video-place.h"
#include <endian.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

/* With decoding off, a window without a picture gets a checkerboard in squares of 60 guest pixels
 * (32 across a full 1920-pixel screen), anchored at the window's top-left corner. */
#define SQUARE 60.0

static GLuint shader(GLenum type, const char *source)
{
    GLuint obj = glCreateShader(type);
    glShaderSource(obj, 1, &source, NULL);
    glCompileShader(obj);
    GLint ok;
    glGetShaderiv(obj, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[512];
        glGetShaderInfoLog(obj, sizeof log, NULL, log);
        fprintf(stderr, "video shader: %s\n", log);
    }
    return obj;
}

void video_init(struct video *v, const char *path)
{
    memset(v, 0, sizeof *v);
    v->fd = -1;
    v->path = strdup(path);
    GLuint vs =
        shader(GL_VERTEX_SHADER, "attribute vec2 pos; void main(){gl_Position=vec4(pos,0.,1.);}");
    GLuint fs = shader(GL_FRAGMENT_SHADER,
                       "precision mediump float; uniform vec2 origin; uniform float square;"
                       "void main(){vec2 c=floor((gl_FragCoord.xy-origin)/square);"
                       "float odd=mod(c.x+c.y,2.);"
                       "gl_FragColor=mix(vec4(.85,.85,.85,1.),vec4(.12,.12,.40,1.),odd);}");
    v->program = glCreateProgram();
    glAttachShader(v->program, vs);
    glAttachShader(v->program, fs);
    glBindAttribLocation(v->program, 0, "pos");
    glLinkProgram(v->program);
    glDeleteShader(vs);
    glDeleteShader(fs);
    GLint ok;
    glGetProgramiv(v->program, GL_LINK_STATUS, &ok);
    if (!ok)
        fprintf(stderr, "video shader link failed\n");
    v->origin = glGetUniformLocation(v->program, "origin");
    v->square = glGetUniformLocation(v->program, "square");
}

static void disconnect(struct video *v)
{
    if (v->fd >= 0)
        close(v->fd);
    v->fd = -1;
    v->used = 0;
    v->table.count = 0; /* nothing shown without the guest */
}

static int shown(const struct webos_video_context *c)
{
    return c->connected && c->has_rect && !c->av_mute && c->w && c->h;
}

static void describe(const struct webos_video_packet *p)
{
    static const char *const events[] = {"sync", "register", "connect",    "rect",
                                         "info", "mute",     "disconnect", "unregister"};
    fprintf(stderr, "video: #%u %s, %u context(s)\n", p->sequence,
            p->event < 8 ? events[p->event] : "?", p->count);
    for (unsigned i = 0; i < p->count; ++i) {
        const struct webos_video_context *c = &p->contexts[i];
        fprintf(stderr,
                "video:   %s%s registered=%u connected=%u mute=%u window=%d,%d %ux%u%s"
                " stream=%ux%u@%u.%03u input=%u:%u",
                c->context, shown(c) ? " SHOWN" : "", c->registered, c->connected, c->av_mute, c->x,
                c->y, c->w, c->h, c->full_screen ? " (full screen)" : "", c->width, c->height,
                c->fps_x1000 / 1000, c->fps_x1000 % 1000, c->input, c->input_port);
        if (video_crop(c))
            fprintf(stderr, " crop=%u,%u %ux%u", c->crop_x, c->crop_y, c->crop_w, c->crop_h);
        fputc('\n', stderr);
    }
}

int video_poll(struct video *v)
{
    if (v->fd < 0) {
        struct sockaddr_un addr = {.sun_family = AF_UNIX};
        if (strlen(v->path) >= sizeof addr.sun_path)
            return 0;
        strcpy(addr.sun_path, v->path);
        v->fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (v->fd < 0)
            return 0;
        if (connect(v->fd, (void *)&addr, sizeof addr) < 0) {
            disconnect(v);
            return 0;
        }
    }
    int changed = 0;
    for (unsigned count = 0; count < 16; count++) {
        ssize_t n = read(v->fd, v->buffer + v->used, sizeof v->buffer - v->used);
        if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) {
            int had = v->table.count > 0;
            disconnect(v);
            return had;
        }
        if (n < 0)
            break;
        v->used += (size_t)n;
        while (v->used >= sizeof(struct webos_video_packet)) {
            struct webos_video_packet p;
            memcpy(&p, v->buffer, sizeof p);
            /* Wire is little-endian: header words after the magic, then per context the words
             * after the id. */
            uint32_t *words = &p.bytes;
            for (unsigned i = 0; i < 7; ++i)
                words[i] = le32toh(words[i]);
            for (unsigned i = 0; i < WEBOS_VIDEO_MAX; ++i) {
                uint32_t *cw = &p.contexts[i].registered;
                for (unsigned k = 0; k < 19; ++k)
                    cw[k] = le32toh(cw[k]);
                p.contexts[i].context[WEBOS_VIDEO_CONTEXT_LEN - 1] = 0;
            }
            if (memcmp(p.magic, WEBOS_VIDEO_MAGIC, 8) || p.bytes != sizeof p ||
                p.count > WEBOS_VIDEO_MAX || !p.screen_w || !p.screen_h || p.screen_w > 8192 ||
                p.screen_h > 8192) {
                fprintf(stderr, "invalid video packet\n");
                disconnect(v);
                return 1;
            }
            v->packets++;
            /* SYNC repeats the table: only a difference is news. */
            if (p.event != WEBOS_VIDEO_SYNC || p.count != v->table.count ||
                memcmp(p.contexts, v->table.contexts, sizeof p.contexts)) {
                v->changes++;
                describe(&p);
                changed = 1;
            }
            v->table = p;
            v->used -= sizeof p;
            memmove(v->buffer, v->buffer + sizeof p, v->used);
        }
    }
    return changed;
}

void video_draw(struct video *v, struct rect r)
{
    static const GLfloat xy[] = {-1, -1, 1, -1, -1, 1, 1, 1};
    const struct webos_video_packet *p = &v->table;
    if (!p->count || r.w <= 0 || r.h <= 0)
        return;
    double sy = (double)r.h / p->screen_h;
    glDisable(GL_BLEND);
    glEnable(GL_SCISSOR_TEST);
    glUseProgram(v->program);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, xy);
    glEnableVertexAttribArray(0);
    for (unsigned i = 0; i < p->count; ++i) {
        const struct webos_video_context *c = &p->contexts[i];
        if (!shown(c))
            continue;
        /* the window, and the whole source around it so that its crop fills the window */
        struct video_place at =
            video_place(c, p->screen_w, p->screen_h, (struct video_box){r.x, r.y, r.w, r.h});
        if (!at.clip.w || !at.clip.h)
            continue;
        glScissor(at.clip.x, at.clip.y, at.clip.w, at.clip.h);
        glViewport(at.picture.x, at.picture.y, at.picture.w, at.picture.h);
        v->drawn++;
        /* VDEC 3: the picture of the media stream whose vdec-ch is this input's port; HDMI 2:
         * a stream whose pictures arrive there (an H13's UHD companion), else nothing (no
         * external source is ever connected) */
        if ((c->input == 3 || c->input == 2) &&
            media_draw(v->media, c->input, c->input_port, !strcmp(c->context, "MAIN"))) {
            v->pictures++;
            continue;
        }
        /* No picture. With decoding on (the helper decodes for this viewer) a TV's video plane
         * is black: before the first frame, between streams, for a codec the helper cannot
         * decode, and on HDMI (no external source is ever connected). The checkerboard marks
         * "video here, not decoded" only when decoding is off: --no-media (also MEDIAHUB=0
         * launches) or a helper built without FFmpeg. */
        if (c->input == 2 || media_decoding(v->media)) {
            glClearColor(0, 0, 0, 1);
            glClear(GL_COLOR_BUFFER_BIT);
            continue;
        }
        glUseProgram(v->program);
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, xy);
        glEnableVertexAttribArray(0);
        glUniform2f(v->origin, (float)at.left, (float)at.top);
        glUniform1f(v->square, (float)(SQUARE * sy));
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    }
    glDisable(GL_SCISSOR_TEST);
}

void video_close(struct video *v)
{
    fprintf(stderr, "video_packets=%u video_changes=%u video_draws=%u video_pictures=%u\n",
            v->packets, v->changes, v->drawn, v->pictures);
    disconnect(v);
    free(v->path);
    glDeleteProgram(v->program);
}
