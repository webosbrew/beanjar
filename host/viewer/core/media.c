// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#define _GNU_SOURCE
/* Timing, EGL/GL composition and PCM playback of decoded media from the
 * per-VM helper. No codec or resampler lives here. Transport and session
 * state are media-ipc.c's; received frame objects are media-frame.c's. */
#include "media-priv.h"
#include "sdl-audio.h"

#include <EGL/eglext.h>
#include <GLES2/gl2ext.h>
#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

int64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static int64_t offset_get(const struct offset *o)
{
    if (!o->start)
        return NONE;
    return o->cur < o->prev ? o->cur : o->prev;
}

/* The running time due at host time now; NONE when not known (no state yet, paused before the
 * first play). With s->lock held. */
static int64_t due_rt(struct stream *s, int64_t now)
{
    if (!s->have_state)
        return NONE;
    if (!s->state.playing)
        return s->state.pause_rt;
    int64_t off = offset_get(&s->offset);
    if (off == NONE || !s->have_delta)
        return NONE;
    return now - off - s->clock_delta - s->state.base_time;
}

/* ---- audio out ---- */

static void mix(struct stream *s, float *out, int frames, int64_t when)
{
    int64_t rt = due_rt(s, when);
    if (!s->open || !s->have_state || !s->state.playing || rt == NONE)
        return;
    if (!s->ahead) {
        if (!s->eos && s->received)
            s->underruns++;
        return;
    }
    struct chunk *c = s->ahead;
    int64_t cursor = c->rt + (int64_t)c->pos * 1000000000 / OUT_RATE;
    int64_t slip = cursor - rt; /* > 0: the next sample is not due yet */
    int i = 0, nudge = 0;
    if (slip > MAX_AUDIO_SLIP) {
        int64_t gap = slip * OUT_RATE / 1000000000;
        if (gap >= frames)
            return; /* not started yet (the lead before the first sample) */
        i = (int)gap;
        if (s->audio_frames)
            s->resyncs++;
    }
    double ms = slip / 1e6;
    s->aslip_sum += fabs(ms);
    s->aslip_n++;
    if (fabs(ms) > s->aslip_max)
        s->aslip_max = fabs(ms);
    if (slip > MAX_AUDIO_SLIP) {
        /* silence up to the first due sample: above */
    } else if (slip < -MAX_AUDIO_SLIP) {
        int64_t skip = -slip * OUT_RATE / 1000000000;
        while (skip > 0 && s->ahead) {
            c = s->ahead;
            int left = c->frames - c->pos, take = skip < left ? (int)skip : left;
            c->pos += take;
            skip -= take;
            s->aqueued -= (int64_t)take * 1000000000 / OUT_RATE;
            if (c->pos >= c->frames) {
                s->ahead = c->next;
                if (!s->ahead)
                    s->atail = NULL;
                free(c);
            }
        }
        s->resyncs++;
    } else if (slip > MIN_AUDIO_SLIP)
        nudge = 1; /* ahead of the device: repeat */
    else if (slip < -MIN_AUDIO_SLIP)
        nudge = -1; /* behind: skip */
    for (int k = 0; i < frames && s->ahead; i++, k++) {
        c = s->ahead;
        out[2 * i] += c->data[2 * c->pos];
        out[2 * i + 1] += c->data[2 * c->pos + 1];
        int step = 1;
        if (nudge && k % NUDGE == NUDGE - 1) {
            step = nudge > 0 ? 0 : 2;
            s->nudges++;
        }
        for (; step > 0 && s->ahead; step--) {
            c = s->ahead;
            c->pos++;
            s->aqueued -= 1000000000 / OUT_RATE;
            if (c->pos >= c->frames) {
                s->ahead = c->next;
                if (!s->ahead)
                    s->atail = NULL;
                free(c);
            }
        }
        s->audio_frames++;
    }
    if (!s->ahead && !s->eos)
        s->underruns++;
    pthread_cond_broadcast(&s->cond);
}

static void fill(struct media *m, float *out, int frames, int64_t when)
{
    memset(out, 0, (size_t)frames * 2 * sizeof(float));
    for (int n = 0; n < WEBOS_MEDIA_PORTS; n++) {
        struct stream *s = &m->streams[n];
        if (s->info.kind != WEBOS_MEDIA_AUDIO)
            continue;
        pthread_mutex_lock(&s->lock);
        mix(s, out, frames, when);
        pthread_mutex_unlock(&s->lock);
    }
    /* The virtual AMP: compressed data always flows at standard volume; the
     * master gain and mute apply once, here, to what is heard and recorded,
     * before the sum is clipped. The TV's own master volume and speaker mute
     * (media_tv_gain) apply here too, as the guest's sound card applies them to
     * the TV's own sound; a change ramps over this block instead of stepping. */
    float tv = (float)m->tv_gain / 65536.0f, from = m->tv_applied;
    m->tv_applied = tv;
    if (m->cfg.mute)
        memset(out, 0, (size_t)frames * 2 * sizeof(float));
    else if (from != tv && frames > 0)
        for (int i = 0; i < frames; i++) {
            float g = (float)m->cfg.volume * (from + (tv - from) * (float)(i + 1) / (float)frames);
            out[2 * i] *= g;
            out[2 * i + 1] *= g;
        }
    else if (m->cfg.volume * tv != 1.0)
        for (int i = 0; i < frames * 2; i++)
            out[i] *= (float)m->cfg.volume * tv;
    for (int i = 0; i < frames * 2; i++)
        out[i] = out[i] > 1 ? 1 : out[i] < -1 ? -1 : out[i];
    if (m->probe)
        for (int i = 0; i < frames; i++) {
            int64_t t = when + (int64_t)i * 1000000000 / OUT_RATE;
            if (fabsf(out[2 * i]) > 0.05f) {
                if (m->probe_quiet_since && t - m->probe_quiet_since > 300000000)
                    fprintf(stderr, "probe: beep heard at %.4f s\n", t / 1e9);
                m->probe_quiet_since = 0;
            } else if (!m->probe_quiet_since)
                m->probe_quiet_since = t;
        }
    pthread_mutex_lock(&m->audio_lock);
    if (m->wav && fwrite(out, sizeof(float) * 2, (size_t)frames, m->wav) == (size_t)frames)
        m->wav_frames += (unsigned)frames;
    pthread_mutex_unlock(&m->audio_lock);
}

static void callback(void *data, Uint8 *stream, int len)
{
    struct media *m = data;
    fill(m, (float *)stream, len / 8, mono_ns() + m->latency_ns);
}

void media_fill(struct media *m, float *out, int frames, long long when)
{
    fill(m, out, frames, (int64_t)when + m->latency_ns);
}

void media_tv_gain(struct media *m, unsigned q16)
{
    if (m)
        m->tv_gain = q16 > 0x10000 ? 0x10000 : q16;
}

/* Without an SDL device: consume in real time, 10 ms at a time. */
static void *clock_thread(void *arg)
{
    struct media *m = arg;
    float buf[2 * 4800];
    int64_t next = mono_ns();
    while (!m->quit) {
        fill(m, buf, OUT_RATE / 100, next);
        next += 10000000;
        int64_t wait = next - mono_ns();
        if (wait > 0)
            usleep((useconds_t)(wait / 1000));
    }
    return NULL;
}

static void wav_header(struct media *m)
{
    uint8_t h[44];
    uint32_t data = (uint32_t)(m->wav_frames * 8), v;
    memcpy(h, "RIFF", 4);
    v = 36 + data;
    memcpy(h + 4, &v, 4);
    memcpy(h + 8, "WAVEfmt ", 8);
    v = 16;
    memcpy(h + 16, &v, 4);
    uint16_t fmt = 3, ch = 2, align = 8, bits = 32;
    memcpy(h + 20, &fmt, 2);
    memcpy(h + 22, &ch, 2);
    v = OUT_RATE;
    memcpy(h + 24, &v, 4);
    v = OUT_RATE * 8;
    memcpy(h + 28, &v, 4);
    memcpy(h + 32, &align, 2);
    memcpy(h + 34, &bits, 2);
    memcpy(h + 36, "data", 4);
    memcpy(h + 40, &data, 4);
    fseek(m->wav, 0, SEEK_SET);
    fwrite(h, sizeof h, 1, m->wav);
    fseek(m->wav, 0, SEEK_END);
}

void start_audio(struct media *m)
{
    pthread_mutex_lock(&m->audio_lock);
    if (m->audio_started) {
        pthread_mutex_unlock(&m->audio_lock);
        return;
    }
    m->audio_started = 1;
    if (m->cfg.wav && (m->wav = fopen(m->cfg.wav, "wb")))
        wav_header(m);
    pthread_mutex_unlock(&m->audio_lock);
    if (m->cfg.external_clock) {
        m->latency_ns = m->cfg.audio_latency_ms > 0 ? m->cfg.audio_latency_ms * 1000000ll : 0;
        fprintf(stderr, "media: audio consumed by the front end's audio clock%s%s\n",
                m->wav ? ", recorded to " : "", m->wav ? m->cfg.wav : "");
        return;
    }
    if (m->cfg.sdl_audio) {
        if (sdl_audio_init() == 0) {
            m->sdl_inited = 1;
            SDL_AudioSpec want = {0}, have;
            want.freq = OUT_RATE;
            want.format = AUDIO_F32SYS;
            want.channels = 2;
            want.samples = 1024;
            want.callback = callback;
            want.userdata = m;
            m->dev = sdl_audio_open(&want, &have);
            if (m->dev) {
                m->latency_ns = m->cfg.audio_latency_ms >= 0
                                    ? m->cfg.audio_latency_ms * 1000000ll
                                    : (int64_t)have.samples * 2 * 1000000000 / have.freq;
                fprintf(stderr,
                        "media: audio out SDL %s, %d Hz, %u frames per buffer, latency %.0f ms\n",
                        SDL_GetCurrentAudioDriver(), have.freq, have.samples, m->latency_ns / 1e6);
                SDL_PauseAudioDevice(m->dev, 0);
                return;
            }
            fprintf(stderr, "media: SDL_OpenAudioDevice: %s\n", SDL_GetError());
        } else
            fprintf(stderr, "media: SDL audio unavailable: %s\n", SDL_GetError());
    }
    m->latency_ns = m->cfg.audio_latency_ms > 0 ? m->cfg.audio_latency_ms * 1000000ll : 0;
    fprintf(stderr, "media: audio consumed by a clock thread (no SDL device)%s%s\n",
            m->wav ? ", recorded to " : "", m->wav ? m->cfg.wav : "");
    if (!pthread_create(&m->clock_thread, NULL, clock_thread, m))
        m->clock_running = 1;
}

/* Main GL thread only. EGL images and queued GPU reads retire before RELEASE
 * permits the helper's hardware decoder to recycle the underlying surface.
 * glFinish is the conservative GLES2 fallback; no socket event stands in for
 * GPU completion. Software planes are immutable and follow the same owner. */
static void retire_current(struct stream *s)
{
    if (s->image != EGL_NO_IMAGE_KHR) {
        s->m->destroy_image(s->m->display, s->image);
        s->image = EGL_NO_IMAGE_KHR;
        glFinish();
    }
    frame_free(&s->current);
}

/* ---- presentation ---- */

int media_update(struct media *m)
{
    if (!m)
        return 0;
    int changed = 0;
    int64_t now = mono_ns();
    for (int n = 0; n < WEBOS_MEDIA_PORTS; n++) {
        struct stream *s = &m->streams[n];
        struct received_frame *pick = NULL;
        double late = 0;
        pthread_mutex_lock(&s->lock);
        if (s->reset_current) {
            s->reset_current = 0;
            if (s->current)
                changed = 1;
            retire_current(s);
        }
        if (s->info.kind == WEBOS_MEDIA_VIDEO) {
            int64_t rt = due_rt(s, now);
            if (s->need_first && s->nframes) {
                pick = s->frames[0];
                memmove(s->frames, s->frames + 1, --s->nframes * sizeof s->frames[0]);
                s->need_first = 0;
            } else
                while (s->nframes &&
                       (s->frames[0]->pts == NONE || (rt != NONE && s->frames[0]->pts <= rt))) {
                    if (pick) {
                        frame_free(&pick);
                        s->late_drops++;
                    }
                    pick = s->frames[0];
                    memmove(s->frames, s->frames + 1, --s->nframes * sizeof s->frames[0]);
                    if (pick->pts != NONE && rt != NONE && s->state.playing)
                        late = (rt - pick->pts) / 1e6;
                }
            if (pick) {
                s->picked++;
                if (late > 0) {
                    s->late_sum += late;
                    s->late_n++;
                    if (late > s->late_max)
                        s->late_max = late;
                }
                pthread_cond_broadcast(&s->cond);
            }
        }
        if (s->open && s->received && now - s->last_report > REPORT_NS) {
            s->last_report = now;
            if (s->received != s->reported_packets)
                report(s, ""); /* quiet while nothing comes */
            s->reported_packets = s->received;
        }
        pthread_mutex_unlock(&s->lock);
        if (pick && m->probe && pick->format != HM_DRM) {
            /* A/V sync probe: a clip whose frame is white at each whole second (and beeps then) */
            unsigned sum = 0, n = 0;
            for (int y = pick->height / 8; y < pick->height; y += pick->height / 4)
                for (int x = pick->width / 8; x < pick->width; x += pick->width / 4, n++)
                    sum += pick->data[0][(size_t)y * pick->linesize[0] + x];
            int bright = n && sum / n > 180;
            if (bright && !m->probe_bright)
                fprintf(stderr, "probe: flash pts=%.3f s shown at %.4f s\n", pick->pts / 1e9,
                        now / 1e9);
            m->probe_bright = bright;
        }
        if (pick) {
            retire_current(s);
            s->current = pick;
            s->uploaded = 0;
            changed = 1;
        }
    }
    return changed;
}

static GLuint compile(GLenum type, const char *src)
{
    GLuint o = glCreateShader(type);
    glShaderSource(o, 1, &src, NULL);
    glCompileShader(o);
    GLint ok;
    glGetShaderiv(o, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[512];
        glGetShaderInfoLog(o, sizeof log, NULL, log);
        fprintf(stderr, "media shader: %s\n", log);
    }
    return o;
}

static GLuint program(const char *vs, const char *fs)
{
    GLuint p = glCreateProgram(), v = compile(GL_VERTEX_SHADER, vs),
           f = compile(GL_FRAGMENT_SHADER, fs);
    glAttachShader(p, v);
    glAttachShader(p, f);
    glBindAttribLocation(p, 0, "pos");
    glLinkProgram(p);
    glDeleteShader(v);
    glDeleteShader(f);
    GLint ok;
    glGetProgramiv(p, GL_LINK_STATUS, &ok);
    if (!ok) {
        glDeleteProgram(p);
        return 0;
    }
    return p;
}

static void gl_init(struct media *m)
{
    static const char vs[] =
        "attribute vec2 pos; varying vec2 uv;"
        "void main(){gl_Position=vec4(pos,0.,1.); uv=vec2((pos.x+1.)*.5,(1.-pos.y)*.5);}";
    m->yuv = program(vs, "precision mediump float; varying vec2 uv; uniform sampler2D ty, tu, tv; "
                         "uniform int planes;"
                         "uniform vec2 sy, sc; uniform mat3 matrix; uniform vec3 offset;"
                         "void main(){ vec3 c; c.x=texture2D(ty,uv*sy).r;"
                         " if(planes==2){ vec4 t=texture2D(tu,uv*sc); c.yz=vec2(t.r,t.a);} else "
                         "{c.y=texture2D(tu,uv*sc).r; c.z=texture2D(tv,uv*sc).r;}"
                         " gl_FragColor=vec4(clamp(matrix*(c-offset),0.,1.),1.);}");
    glUseProgram(m->yuv);
    glUniform1i(glGetUniformLocation(m->yuv, "ty"), 0);
    glUniform1i(glGetUniformLocation(m->yuv, "tu"), 1);
    glUniform1i(glGetUniformLocation(m->yuv, "tv"), 2);
    m->yuv_planes = glGetUniformLocation(m->yuv, "planes");
    m->yuv_scale_y = glGetUniformLocation(m->yuv, "sy");
    m->yuv_scale_c = glGetUniformLocation(m->yuv, "sc");
    m->yuv_matrix = glGetUniformLocation(m->yuv, "matrix");
    m->yuv_offset = glGetUniformLocation(m->yuv, "offset");
    const char *ext = (const char *)glGetString(GL_EXTENSIONS);
    m->display = eglGetCurrentDisplay();
    m->create_image = (PFNEGLCREATEIMAGEKHRPROC)eglGetProcAddress("eglCreateImageKHR");
    m->destroy_image = (PFNEGLDESTROYIMAGEKHRPROC)eglGetProcAddress("eglDestroyImageKHR");
    m->image_target =
        (PFNGLEGLIMAGETARGETTEXTURE2DOESPROC)eglGetProcAddress("glEGLImageTargetTexture2DOES");
    const char *eglext =
        m->display != EGL_NO_DISPLAY ? eglQueryString(m->display, EGL_EXTENSIONS) : NULL;
    if (ext && strstr(ext, "GL_OES_EGL_image_external") && eglext &&
        strstr(eglext, "EGL_EXT_image_dma_buf_import") && m->create_image && m->image_target) {
        m->oes =
            program(vs, "#extension GL_OES_EGL_image_external : require\n"
                        "precision mediump float; varying vec2 uv; uniform samplerExternalOES t;"
                        "void main(){gl_FragColor=vec4(texture2D(t,uv).rgb,1.);}");
        m->have_oes = m->oes != 0;
    }
    glUseProgram(0);
}

static void matrix_for(const struct received_frame *f, GLfloat mat[9], GLfloat off[3])
{
    int full = f->color_range == 2;
    int bt709 = f->colorspace == 1 || (f->colorspace == 2 && f->height >= 720);
    double kr = bt709 ? 0.2126 : 0.299, kb = bt709 ? 0.0722 : 0.114, kg = 1 - kr - kb;
    double ys = full ? 1 : 255.0 / 219, cs = full ? 1 : 255.0 / 224;
    /* column-major: r = y + (2-2kr) v; g = y - ...; b = y + (2-2kb) u */
    GLfloat m[9] = {
        (GLfloat)ys,
        (GLfloat)ys,
        (GLfloat)ys,
        0,
        (GLfloat)(-cs * (2 - 2 * kb) * kb / kg),
        (GLfloat)(cs * (2 - 2 * kb)),
        (GLfloat)(cs * (2 - 2 * kr)),
        (GLfloat)(-cs * (2 - 2 * kr) * kr / kg),
        0,
    };
    memcpy(mat, m, sizeof m);
    off[0] = full ? 0 : 16 / 255.0f;
    off[1] = off[2] = 128 / 255.0f;
}

static void plane(GLuint tex, int unit, GLenum format, int w, int h, const void *data)
{
    glActiveTexture(GL_TEXTURE0 + unit);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    if (data)
        glTexImage2D(GL_TEXTURE_2D, 0, format, w, h, 0, format, GL_UNSIGNED_BYTE, data);
}

static int import_drm(struct media *m, struct stream *s, const struct received_frame *f)
{
    const struct host_media_message *d = &f->descriptor;
    if (!m->have_oes || !d || d->layers != 1 || d->layer[0].planes > 3)
        return 0;
    const struct host_media_layer *l = &d->layer[0];
    static const EGLint fd_a[3] = {EGL_DMA_BUF_PLANE0_FD_EXT, EGL_DMA_BUF_PLANE1_FD_EXT,
                                   EGL_DMA_BUF_PLANE2_FD_EXT};
    static const EGLint off_a[3] = {EGL_DMA_BUF_PLANE0_OFFSET_EXT, EGL_DMA_BUF_PLANE1_OFFSET_EXT,
                                    EGL_DMA_BUF_PLANE2_OFFSET_EXT};
    static const EGLint pitch_a[3] = {EGL_DMA_BUF_PLANE0_PITCH_EXT, EGL_DMA_BUF_PLANE1_PITCH_EXT,
                                      EGL_DMA_BUF_PLANE2_PITCH_EXT};
    static const EGLint lo_a[3] = {EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT,
                                   EGL_DMA_BUF_PLANE1_MODIFIER_LO_EXT,
                                   EGL_DMA_BUF_PLANE2_MODIFIER_LO_EXT};
    static const EGLint hi_a[3] = {EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT,
                                   EGL_DMA_BUF_PLANE1_MODIFIER_HI_EXT,
                                   EGL_DMA_BUF_PLANE2_MODIFIER_HI_EXT};
    EGLint a[64];
    int n = 0;
    a[n++] = EGL_WIDTH;
    a[n++] = f->width;
    a[n++] = EGL_HEIGHT;
    a[n++] = f->height;
    a[n++] = EGL_LINUX_DRM_FOURCC_EXT;
    a[n++] = (EGLint)l->fourcc;
    for (unsigned i = 0; i < l->planes; i++) {
        const struct host_media_plane *p = &l->plane[i];
        const struct host_media_object *o = &d->object[p->object];
        a[n++] = fd_a[i];
        a[n++] = f->fds[p->object];
        a[n++] = off_a[i];
        a[n++] = (EGLint)p->offset;
        a[n++] = pitch_a[i];
        a[n++] = (EGLint)p->stride;
        if (o->modifier != 0x00ffffffffffffffull) {
            a[n++] = lo_a[i];
            a[n++] = (EGLint)(o->modifier & 0xffffffff);
            a[n++] = hi_a[i];
            a[n++] = (EGLint)(o->modifier >> 32);
        }
    }
    a[n++] = EGL_YUV_COLOR_SPACE_HINT_EXT;
    a[n++] = f->colorspace == 9                         ? EGL_ITU_REC2020_EXT
             : f->colorspace == 5 || f->colorspace == 6 ? EGL_ITU_REC601_EXT
                                                        : EGL_ITU_REC709_EXT;
    a[n++] = EGL_SAMPLE_RANGE_HINT_EXT;
    a[n++] = f->color_range == 2 ? EGL_YUV_FULL_RANGE_EXT : EGL_YUV_NARROW_RANGE_EXT;
    a[n++] = EGL_NONE;
    if (s->image != EGL_NO_IMAGE_KHR)
        m->destroy_image(m->display, s->image);
    s->image = m->create_image(m->display, EGL_NO_CONTEXT, EGL_LINUX_DMA_BUF_EXT, NULL, a);
    if (s->image == EGL_NO_IMAGE_KHR) {
        fprintf(stderr,
                "media %d: EGL import of %.4s modifier 0x%llx failed (0x%x): downloading frames "
                "instead\n",
                s->index, (const char *)&l->fourcc, (unsigned long long)d->object[0].modifier,
                eglGetError());
        return 0;
    }
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, s->tex[0]);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    m->image_target(GL_TEXTURE_EXTERNAL_OES, s->image);
    return glGetError() == GL_NO_ERROR;
}

static void draw_stream(struct media *m, struct stream *s)
{
    static const GLfloat xy[] = {-1, -1, 1, -1, -1, 1, 1, 1};
    struct received_frame *f = s->current;
    int first = !s->uploaded; /* this picture's first draw */
    if (!s->tex[0])
        glGenTextures(3, s->tex);
    if (f->format == HM_DRM) {
        if (!s->uploaded && !import_drm(m, s, f)) {
            pthread_mutex_lock(&s->lock);
            if (s->current)
                s->current->release_type = HM_IMPORT_FAILED;
            pthread_mutex_unlock(&s->lock);
            retire_current(s);
            return;
        }
        s->uploaded = 1;
        glUseProgram(m->oes);
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_EXTERNAL_OES, s->tex[0]);
    } else {
        int nv12 = f->format == HM_NV12;
        int cw = f->format == HM_YUV444 ? f->width : (f->width + 1) / 2;
        int ch = f->format == HM_YUV420 || nv12 ? (f->height + 1) / 2 : f->height;
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
        plane(s->tex[0], 0, GL_LUMINANCE, f->linesize[0], f->height,
              s->uploaded ? NULL : f->data[0]);
        if (nv12)
            plane(s->tex[1], 1, GL_LUMINANCE_ALPHA, f->linesize[1] / 2, ch,
                  s->uploaded ? NULL : f->data[1]);
        else {
            plane(s->tex[1], 1, GL_LUMINANCE, f->linesize[1], ch, s->uploaded ? NULL : f->data[1]);
            plane(s->tex[2], 2, GL_LUMINANCE, f->linesize[2], ch, s->uploaded ? NULL : f->data[2]);
        }
        glPixelStorei(GL_UNPACK_ALIGNMENT, 4);
        s->uploaded = 1;
        GLfloat mat[9], off[3];
        matrix_for(f, mat, off);
        glUseProgram(m->yuv);
        glUniform1i(m->yuv_planes, nv12 ? 2 : 3);
        glUniform2f(m->yuv_scale_y, (GLfloat)f->width / f->linesize[0], 1);
        glUniform2f(m->yuv_scale_c, (GLfloat)cw / (nv12 ? f->linesize[1] / 2 : f->linesize[1]), 1);
        glUniformMatrix3fv(m->yuv_matrix, 1, GL_FALSE, mat);
        glUniform3fv(m->yuv_offset, 1, off);
    }
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, xy);
    glEnableVertexAttribArray(0);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glActiveTexture(GL_TEXTURE0);
    if (first) {
        pthread_mutex_lock(&s->lock);
        s->shown++;
        if (f->pts != NONE) {
            if (s->pts_first == NONE)
                s->pts_first = f->pts;
            s->pts_last = f->pts;
        }
        pthread_mutex_unlock(&s->lock);
    }
}

int media_draw(struct media *m, unsigned input, unsigned input_port, int main_window)
{
    if (!m)
        return 0;
    struct stream *pick = NULL;
    int videos = 0;
    /* the stream whose pictures reach this input: the output its OPEN names (an H13's UHD
     * companion: HDMI 0), else VDEC with the port its vdec-ch */
    for (int n = 0; n < WEBOS_MEDIA_PORTS; n++) {
        struct stream *s = &m->streams[n];
        if (s->info.kind != WEBOS_MEDIA_VIDEO || !s->current)
            continue;
        uint32_t out =
            s->info.output ? s->info.output : WEBOS_MEDIA_OUTPUT(3, (uint32_t)s->info.channel);
        if (!s->info.output)
            videos++;
        if (out == WEBOS_MEDIA_OUTPUT(input, input_port)) {
            pick = s;
            break;
        }
    }
    if (!pick && input == 3 && main_window && videos == 1)
        for (int n = 0; n < WEBOS_MEDIA_PORTS && !pick; n++)
            if (m->streams[n].info.kind == WEBOS_MEDIA_VIDEO && m->streams[n].current &&
                !m->streams[n].info.output)
                pick = &m->streams[n];
    if (!pick)
        return 0;
    draw_stream(m, pick);
    return pick->current != NULL;
}

int media_decoding(const struct media *m)
{
    return m && !m->no_decoder;
}

struct media *media_open(const struct media_config *cfg)
{
    struct media *m = calloc(1, sizeof *m);
    m->cfg = *cfg;
    if (!(m->cfg.volume > 0))
        m->cfg.volume = 1.0; /* non-positive: standard volume */
    m->tv_gain = 0x10000;    /* unity until the TV's volume is known */
    m->tv_applied = 1.0f;
    m->cfg.prefix = strdup(cfg->prefix);
    m->cfg.wav = cfg->wav ? strdup(cfg->wav) : NULL;
    pthread_mutex_init(&m->audio_lock, NULL);
    m->probe = getenv("WEBOS_MEDIA_PROBE") && atoi(getenv("WEBOS_MEDIA_PROBE"));
    gl_init(m);
    m->opened = mono_ns();
    fprintf(stderr, "media: decoded helper transport, %s-<n>.sock, EGL dma-buf import %s\n",
            m->cfg.prefix, m->have_oes ? "yes" : "no");
    for (int n = 0; n < WEBOS_MEDIA_PORTS; n++) {
        struct stream *s = &m->streams[n];
        s->m = m;
        s->index = n;
        s->image = EGL_NO_IMAGE_KHR;
        s->pts_first = s->pts_last = NONE;
        snprintf(s->path, sizeof s->path, "%s-%d.sock", m->cfg.prefix, n);
        pthread_mutex_init(&s->lock, NULL);
        pthread_cond_init(&s->cond, NULL);
        if (!pthread_create(&s->reader, NULL, reader, s))
            s->threads |= 1;
    }
    return m;
}

void media_close(struct media *m)
{
    if (!m)
        return;
    m->quit = 1;
    for (int n = 0; n < WEBOS_MEDIA_PORTS; n++) {
        struct stream *s = &m->streams[n];
        pthread_mutex_lock(&s->lock);
        pthread_cond_broadcast(&s->cond);
        pthread_mutex_unlock(&s->lock);
    }
    if (m->dev) {
        sdl_audio_close(m->dev);
        m->dev = 0;
    }
    if (m->sdl_inited)
        sdl_audio_quit();
    if (m->clock_running)
        pthread_join(m->clock_thread, NULL);
    for (int n = 0; n < WEBOS_MEDIA_PORTS; n++) {
        struct stream *s = &m->streams[n];
        if (s->threads & 1)
            pthread_join(s->reader, NULL);
        pthread_mutex_lock(&s->lock);
        pthread_cond_broadcast(&s->cond);
        pthread_mutex_unlock(&s->lock);
        if (s->info.kind && (s->received || s->open))
            report(s, " (exit)");
        free_frames(s);
        retire_current(s);
        if (s->image != EGL_NO_IMAGE_KHR)
            m->destroy_image(m->display, s->image);
        if (s->tex[0])
            glDeleteTextures(3, s->tex);
        if (s->skipped_bytes)
            fprintf(stderr, "media %d: %llu bytes skipped to resynchronise\n", n, s->skipped_bytes);
    }
    if (m->wav) {
        wav_header(m);
        fclose(m->wav);
        fprintf(stderr, "media: wav %s: %.2f s\n", m->cfg.wav, m->wav_frames / (double)OUT_RATE);
    }
    struct rusage ru;
    if (!getrusage(RUSAGE_SELF, &ru)) {
        double cpu = ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6 + ru.ru_stime.tv_sec +
                     ru.ru_stime.tv_usec / 1e6;
        double wall = (mono_ns() - m->opened) / 1e9;
        fprintf(stderr, "media: viewer cpu %.1f s in %.1f s (%.0f %% of one core, all threads)\n",
                cpu, wall, wall > 0 ? 100 * cpu / wall : 0);
    }
    if (m->yuv)
        glDeleteProgram(m->yuv);
    if (m->oes)
        glDeleteProgram(m->oes);
    free((char *)m->cfg.prefix);
    free((char *)m->cfg.wav);
    free(m);
}
