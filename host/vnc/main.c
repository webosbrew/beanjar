// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#define _POSIX_C_SOURCE 200809L
/* beanjar-vnc: a VM's TV picture as a VNC server (docs/dev/architecture/vnc.md). The viewer's core
 * (libbeanjar-viewer-core.so, host/viewer/core/core.h) composes the picture, scanout, video
 * windows and LG's cursor, in a surfaceless EGL context, with no display server; neatvnc
 * (its aml loop driven from the core's GLib main loop, one thread) encodes it for the clients,
 * only when it changes. LG's cursor is drawn into the picture at the VNC pointer; with
 * --native-cursor it goes to the clients as their own cursor (the Cursor pseudo-encoding, 1-bit
 * mask) instead. Keys go through the core's PC key map (Super is the remote's Home), the pointer
 * and the wheel to the core as the GTK viewer sends them. With --audio-local the core's mixed
 * audio (the guest's and the media's) goes to an owner-only unix socket as Opus or PCM (audio.c);
 * without it there is no audio. With --video-local the same composed frames go to an owner-only
 * unix socket as H.264 (video.c: NVENC, VA-API or x264 through libavcodec, or a V4L2 encoder),
 * for the web viewer.
 * With --password-file, clients
 * authenticate with RSA-AES (neatvnc's), with any user name; an address other than loopback
 * requires it. The password is read from that file at start, never taken from argv or the
 * environment (beanjar vnc start passes the VM's owner-only identity/vnc.password). With --unix it
 * also serves RFB on a unix socket,
 * behind the same password, which a link lan VM's relay serves on its LAN address. With --local it
 * serves RFB without authentication on an owner-only (0600) unix socket, for this host's own
 * front ends (beanjar vnc pipe, the web viewer): the file's permissions are the access control. */
#include <sys/stat.h>
#include "../viewer/core/core.h"
#include "../viewer/core/pace.h"
#include "rate.h"
#include "audio.h"
#include "keysym.h"
#ifdef HAVE_GBM
#include "frames.h"
#include "v4l2.h"
#endif
#include "thumb.h"
#include "tilediff.h"
#include <GLES2/gl2.h>
#ifdef HAVE_VIDEO
#include "video.h"
#include "yuv.h"
#else
struct video_server;
#endif
#include <EGL/egl.h>
#include <EGL/eglext.h>
#define AML_UNSTABLE_API 1 /* aml 0.3: the API used below, acknowledged as aml.h asks */
#include <aml.h>
#include <neatvnc.h>
#include <pixman.h>
#include <glib.h>
#include <glib-unix.h>
#include <arpa/inet.h>
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* neatvnc 1.0's names for the buffers (frames), under the names this file was written with */
#define nvnc_fb              nvnc_frame
#define nvnc_fb_pool         nvnc_frame_pool
#define nvnc_fb_pool_new     nvnc_frame_pool_new
#define nvnc_fb_pool_acquire nvnc_frame_pool_acquire
#define nvnc_fb_pool_unref   nvnc_frame_pool_unref
#define nvnc_fb_get_addr     nvnc_frame_get_addr
#define nvnc_fb_new          nvnc_frame_new
#define nvnc_fb_ref          nvnc_frame_ref
#define nvnc_fb_unref        nvnc_frame_unref
#define nvnc_close           nvnc_del
#define nvnc_fb_from_gbm_bo  nvnc_frame_from_gbm_bo
#define fb_set_userdata      nvnc_frame_set_userdata
#define fb_get_userdata      nvnc_frame_get_userdata
/* what a pool's buffer holds outlives its frame: 1.0's pool makes a new frame on each acquire */
#define pooled_set_userdata(fb, d, fn) nvnc_buffer_set_userdata(nvnc_frame_get_buffer(fb), d, fn)
#define pooled_get_userdata(fb)        nvnc_buffer_get_userdata(nvnc_frame_get_buffer(fb))
static struct nvnc *listen_on(const char *addr, uint16_t port, bool websocket)
{
    struct nvnc *server = nvnc_new();
    if (server && nvnc_listen_tcp(server, addr, port,
                                  websocket ? NVNC_STREAM_WEBSOCKET : NVNC_STREAM_NORMAL) < 0) {
        int e = errno;
        nvnc_del(server);
        errno = e;
        return NULL;
    }
    return server;
}
static struct nvnc *listen_unix(const char *path)
{
    struct nvnc *server = nvnc_new();
    if (server && nvnc_listen_unix(server, path, NVNC_STREAM_NORMAL) < 0) {
        int e = errno;
        nvnc_del(server);
        errno = e;
        return NULL;
    }
    return server;
}

#define FOURCC_XBGR8888 0x34324258u /* 'XB24': R, G, B, X bytes, as wv_frame's RGBA */
#define FOURCC_ABGR8888 0x34324241u /* 'AB24': R, G, B, A bytes, as wv_cursor's RGBA */
#define CURSOR_MAX      256         /* wv_cursor's largest picture (webos.cursor's) */
#define TILE            16 /* unchanged frames: compared in tiles this many pixels square */
/* The largest cursor sent: browsers drop a CSS cursor (noVNC's) over 128x128, TigerVNC drops the
 * connection over 256. LG's arrow is about 40x48 in a 1280x720 desktop. */
#define CURSOR_SENT_MAX 128

static void die(const char *message)
{
    fprintf(stderr, "beanjar-vnc: %s\n", message);
    exit(1);
}

/* The password clients must give (--password-file's first line), or NULL: no authentication. */
static char *password;

/* The first line of path, without its line end: the password. */
static char *read_password(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        fprintf(stderr, "beanjar-vnc: cannot read the password file %s: %s\n", path,
                strerror(errno));
        exit(1);
    }
    char line[512];
    if (!fgets(line, sizeof line, f))
        line[0] = 0;
    fclose(f);
    line[strcspn(line, "\r\n")] = 0;
    if (!line[0])
        die("the password file is empty");
    return strdup(line);
}

/* An address only this host reaches: 127.0.0.0/8 or ::1, as numbers. A name (localhost included)
 * counts as reachable from elsewhere, so it needs the password like any other. */
static bool loopback(const char *addr)
{
    struct in_addr a4;
    struct in6_addr a6;
    if (inet_pton(AF_INET, addr, &a4) == 1)
        return (ntohl(a4.s_addr) >> 24) == 127;
    if (inet_pton(AF_INET6, addr, &a6) == 1)
        return IN6_IS_ADDR_LOOPBACK(&a6);
    return false;
}

/* The given password against ours, in time independent of where they differ. */
static bool password_ok(const char *given)
{
    if (!given)
        return false;
    size_t n = strlen(password), m = strlen(given);
    unsigned diff = n != m;
    for (size_t i = 0; i < n; i++)
        diff |= (unsigned char)password[i] ^ (unsigned char)(i < m ? given[i] : 0);
    return !diff;
}
static void auth_result(const char *username, bool ok)
{
    if (!ok)
        fprintf(stderr, "beanjar-vnc: authentication failed (user \"%s\")\n",
                username ? username : "");
}
/* RSA-AES's credentials (the user name is not checked) */
static void on_auth(struct nvnc_auth_creds *creds, void *data)
{
    (void)data;
    bool ok = password_ok(nvnc_auth_creds_get_password(creds));
    auth_result(nvnc_auth_creds_get_username(creds), ok);
    if (ok)
        nvnc_auth_creds_accept(creds);
    else
        nvnc_auth_creds_reject(creds, "wrong password");
}

struct server {
    struct wv *v;
    struct aml *aml;
    /* the RFB listener; with --websocket one for browsers (noVNC), with --unix one for a relay,
     * with --local an unauthenticated one; a display belongs to one server, so each has its own,
     * fed the same frames */
    struct nvnc *nvnc[4];
    struct nvnc_display *display[4];
    bool ws[4]; /* the WebSocket listener's (noVNC's) */
    int servers;
    struct nvnc_fb_pool *pool;
#ifdef HAVE_GBM
    /* --frames gbm: the GBM frames (frames.h) composed into, the one whose render's fence is
     * awaited, and the latest one not fed (held's, while all RFB clients are paused) */
    struct frames *gbm;
    struct frame *pending, *held_frame, *last_frame;
    int diffed;       /* the pending frame's tiles were compared with last_frame's on the GPU */
    unsigned starved; /* renders put off: every GBM frame in use */
    gint64 render_us; /* the renders' GPU time, from their submission to their fence (us) */
#endif
    GMainLoop *loop;
    int width, height, clients, dirty;
    /* the frames' times (pace.h: at most fps a second, docs/dev/architecture/vnc.md, "Frame
     * rate"), the last one's start, and whether that rate is kept above 60 (rate.h): the frames'
     * main loop work so far, the encoder's counters as last read */
    struct pace pace;
    gint64 last;
    struct rate rate;
    int fps;
    unsigned long long enc_n, enc_us;
    guint render_source;
    unsigned frames, keys, unmapped, pointer_events;
    /* unchanged frames (docs/dev/architecture/vnc.md, "Unchanged frames"): rendered but not handed
     * on, since no pixel differs from the last one handed on (mtex[!mcur], last_frame); force: the
     * next is handed on anyway (a new client's); fed_last: neatvnc has the last one, so the next is
     * fed with its changed tiles as damage, not as a whole frame; tiles: the changed ones */
    unsigned unchanged;
    int force, fed_last, tw, th;
    unsigned char *tiles;
    /* without GBM frames: the scene composed into mtex[mcur], read back only for the consumers
     * that take RGB, and compared with mtex[!mcur], the last one handed on (mvalid: there is
     * one): on the CPU with last_fb, that one read back, when this one is read back too, else
     * on the GPU (diff) (docs/dev/architecture/vnc.md, "Read backs") */
    GLuint mtex[2], mfbo[2];
    int mcur, mvalid;
    struct tilediff *diff;
    struct nvnc_fb *last_fb;
    unsigned rgb_reads; /* frames read back as RGB */
    /* only the rows that changed are read back: each RGB buffer of the pool keeps the frame it
     * holds (struct held_rows, the pool buffer's userdata) and is brought up to date with the rows
     * changed since, kept for the last BANDS frames handed on (serial: the latest's; 0: none yet)
     */
    unsigned serial;
    struct {
        int y0, y1;
    } bands[16];
    unsigned long long rgb_rows; /* rows read back as RGB */
    /* thumbnails (docs/dev/architecture/vnc.md, "Thumbnails"): Screenshot calls smaller than the
     * desktop, answered after the frame from the last one composed (mtex), or one composed for
     * them into src while nothing else is; ppm: the last one made, of shot_w x shot_h at
     * shot_changes (the core's changes) and the desktop's shot_dw x shot_dh, written again while
     * nothing changed. shots: made; shots_cached: answered with ppm; shot_us: making them */
    struct thumb *thumb;
    GLuint shot_tex, shot_fbo;
    int shot_tw, shot_th, shot_w, shot_h, shot_dw, shot_dh;
    long shot_changes;
    unsigned char *ppm;
    size_t ppm_len;
    guint shot_source;
    unsigned shots, shots_cached;
    gint64 shot_us;
    /* LG's cursor as a VNC cursor (with --native-cursor): wv_cursor's serial last seen, and what
     * was last sent (the pixels, size and hotspot); cursors: the updates sent */
    int native, cursor_seen;
    unsigned cursor_serial, cursors;
    int cursor_w, cursor_h, cursor_hx, cursor_hy;
    uint8_t *cursor_pixels;
    struct video_server *video; /* --video-local's, or NULL */
#ifdef HAVE_VIDEO
    /* the video socket's RGB to NV12 on the GPU (yuv.h; NULL: swscale's, in the encoder), from
     * mtex[mcur] or the GBM frame's texture; nv12: the frames so converted */
    struct yuv *yuv;
    unsigned nv12;
#endif
    /* the --local listener's index (-1: none); its RFB clients paused by beanjar-web (below);
     * feeds: frames fed to neatvnc; held: the latest frame not fed, while all clients are paused */
    int local, paused;
    unsigned feeds;
    struct nvnc_fb *held;
};
/* A client's pointer state: the buttons it holds, for edges; whether beanjar-web paused it. */
struct client {
    struct server *s;
    unsigned buttons;
    bool paused;
};

/* beanjar-web's markers on a --local connection (docs/dev/architecture/vnc.md, "Paused RFB
 * clients"): a ClientCutText of exactly these bytes pauses or resumes that connection's picture
 * while its page plays the H.264 stream. They are never clipboard text. */
#define MARK_PAUSE  "beanjar:rfb-pause"
#define MARK_RESUME "beanjar:rfb-resume"
/* beanjar-web's planes (docs/dev/architecture/vnc.md, "Planes"): "beanjar:planes=N" hides the
 * planes of the WV_PLANE_* bits N (0 to 7) for every client, "beanjar:planes?" asks; both are
 * answered on the --local listener with a ServerCutText "beanjar:planes=N", the planes hidden now.
 */
#define MARK_PLANES     "beanjar:planes="
#define MARK_PLANES_ASK "beanjar:planes?"

static gboolean render(gpointer data);
static void shots_after_frame(struct server *s);
#ifndef HAVE_VIDEO
static int video_server_wanted(struct video_server *v)
{
    (void)v;
    return 0;
}
#endif
/* Whether an RFB client wants pictures: one that beanjar-web has not paused. */
static bool rfb_wanted(struct server *s)
{
    return s->clients > s->paused;
}
/* Whether frames are wanted: an RFB client that is not paused, or a video one, is connected (the
 * first frame is made without one, at the start: main). */
static bool idle(struct server *s)
{
    return !rfb_wanted(s) && !video_server_wanted(s->video) && s->frames;
}
/* With no RFB and no video client the core is idle: QEMU's updates are acknowledged but not
 * copied (wv_idle), nothing is composed, read back or encoded, until a client comes; its first
 * frame brings the picture up to date. */
static void update_idle(struct server *s)
{
    wv_idle(s->v, !rfb_wanted(s) && !video_server_wanted(s->video));
}
/* The changed tiles (s->tiles) as a region; empty when none changed. */
static void tiles_region(struct server *s, struct pixman_region16 *r)
{
    pixman_region_init(r);
    for (int ty = 0; ty < s->th; ty++)
        for (int tx = 0; tx < s->tw;) {
            if (!s->tiles[ty * s->tw + tx]) {
                tx++;
                continue;
            }
            int x0 = tx;
            while (tx < s->tw && s->tiles[ty * s->tw + tx])
                tx++;
            pixman_region_union_rect(r, r, x0 * TILE, ty * TILE, (unsigned)(tx - x0) * TILE, TILE);
        }
    pixman_region_intersect_rect(r, r, 0, 0, (unsigned)s->width, (unsigned)s->height);
}
/* fb to every display: with partial, the changed tiles as damage (neatvnc has the last frame),
 * otherwise as a whole frame; neatvnc's damage refinery finds what changed within, for the
 * clients that track it */
static void feed_damage(struct server *s, struct nvnc_fb *fb, int partial)
{
    struct pixman_region16 damage;
    if (partial && s->fed_last)
        tiles_region(s, &damage);
    else
        pixman_region_init_rect(&damage, 0, 0, s->width, s->height);
    nvnc_frame_set_damage(fb, &damage);
    for (int i = 0; i < s->servers; i++)
        nvnc_display_feed_frame(s->display[i], fb);
    pixman_region_fini(&damage);
    s->feeds++;
    s->fed_last = 1;
}
static void feed(struct server *s, struct nvnc_fb *fb)
{
    feed_damage(s, fb, 0);
}
/* Which TILE-square tiles of cur differ from prev (RGBX bytes, width * 4 a row), into s->tiles;
 * how many. */
static int tiles_compare(struct server *s, const unsigned char *cur, const unsigned char *prev)
{
    int changed = 0;
    size_t stride = (size_t)s->width * 4;
    for (int ty = 0; ty < s->th; ty++)
        for (int tx = 0; tx < s->tw; tx++) {
            int x = tx * TILE, w = MIN(TILE, s->width - x), diff = 0;
            for (int y = ty * TILE; !diff && y < MIN((ty + 1) * TILE, s->height); y++)
                diff = memcmp(cur + y * stride + x * 4, prev + y * stride + x * 4, (size_t)w * 4);
            s->tiles[ty * s->tw + tx] = diff != 0;
            changed += diff != 0;
        }
    return changed;
}
static void drop_held(struct server *s)
{
    if (s->held)
        nvnc_fb_unref(s->held);
    s->held = NULL;
#ifdef HAVE_GBM
    if (s->held_frame)
        vnc_frame_unref(&s->held_frame->v);
    s->held_frame = NULL;
#endif
}
#ifdef HAVE_GBM
/* neatvnc let go of a GBM frame it was fed (its last encoder is done, the display has a newer) */
static void gbm_fb_gone(void *data)
{
    struct frame *f = data;
    vnc_frame_unref(&f->v);
}
/* A GBM frame to neatvnc: a frame of its own for the buffer object, holding a reference to f
 * until neatvnc lets it go. */
static void feed_gbm(struct server *s, struct frame *f, int partial)
{
    struct nvnc_fb *fb = nvnc_fb_from_gbm_bo(f->view);
    if (!fb)
        die("neatvnc cannot take a GBM buffer object");
    vnc_frame_ref(&f->v);
    fb_set_userdata(fb, f, gbm_fb_gone);
    feed_damage(s, fb, partial);
    nvnc_fb_unref(fb);
}
/* A sync file for the GL commands so far (flushed), or -1: none (EGL_ANDROID_native_fence_sync
 * missing). */
static int gl_fence(void)
{
    static PFNEGLCREATESYNCKHRPROC create_sync;
    static PFNEGLDESTROYSYNCKHRPROC destroy_sync;
    static PFNEGLDUPNATIVEFENCEFDANDROIDPROC dup_fence;
    static int checked;
    EGLDisplay display = eglGetCurrentDisplay();
    if (!checked) {
        checked = 1;
        const char *ext = eglQueryString(display, EGL_EXTENSIONS);
        if (ext && strstr(ext, "EGL_ANDROID_native_fence_sync")) {
            create_sync = (PFNEGLCREATESYNCKHRPROC)eglGetProcAddress("eglCreateSyncKHR");
            destroy_sync = (PFNEGLDESTROYSYNCKHRPROC)eglGetProcAddress("eglDestroySyncKHR");
            dup_fence =
                (PFNEGLDUPNATIVEFENCEFDANDROIDPROC)eglGetProcAddress("eglDupNativeFenceFDANDROID");
        }
    }
    if (!create_sync || !destroy_sync || !dup_fence)
        return -1;
    static const EGLint attrs[] = {EGL_SYNC_NATIVE_FENCE_FD_ANDROID, EGL_NO_NATIVE_FENCE_FD_ANDROID,
                                   EGL_NONE};
    EGLSyncKHR sync = create_sync(display, EGL_SYNC_NATIVE_FENCE_ANDROID, attrs);
    if (sync == EGL_NO_SYNC_KHR)
        return -1;
    glFlush();
    int fd = dup_fence(display, sync);
    destroy_sync(display, sync);
    return fd;
}
#endif
#ifdef HAVE_VIDEO
/* The frame in tex (the picture's width, tex_h rows, top row first) to the video socket as NV12
 * converted on the GPU, when its encoder takes that: 1; 0: it takes RGB (or the conversion
 * failed, and swscale converts from now on). */
static int video_nv12(struct server *s, GLuint tex, int tex_h)
{
    struct video_nv12 p;
    if (!s->yuv || !video_server_nv12_get(s->video, &p))
        return 0;
    if (!yuv_convert(s->yuv, tex, s->width, tex_h, p.width, p.height, p.data)) {
        video_server_nv12_put(s->video, &p, 1);
        s->nv12++;
        return 1;
    }
    fprintf(stderr, "beanjar-vnc: video: RGB to NV12 on the GPU failed: swscale from now on\n");
    video_server_nv12_put(s->video, &p, 0);
    yuv_free(s->yuv);
    s->yuv = NULL;
    return 0;
}
#endif
/* mtex and mfbo: two targets at the output size; 0, or -1 if they are no framebuffers. */
static int memory_targets(struct server *s)
{
    if (s->mfbo[0])
        return 0;
    glGenTextures(2, s->mtex);
    glGenFramebuffers(2, s->mfbo);
    GLint previous;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &previous);
    int failed = 0;
    for (int i = 0; i < 2; i++) {
        glBindTexture(GL_TEXTURE_2D, s->mtex[i]);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, s->width, s->height, 0, GL_RGBA, GL_UNSIGNED_BYTE,
                     NULL);
        glBindFramebuffer(GL_FRAMEBUFFER, s->mfbo[i]);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, s->mtex[i], 0);
        failed |= glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE;
    }
    glBindTexture(GL_TEXTURE_2D, 0);
    glBindFramebuffer(GL_FRAMEBUFFER, (GLuint)previous);
    s->diff = tilediff_new(s->width, s->height);
    return failed ? -1 : 0;
}
/* The composed frame (mtex[mcur]) read back into fb, RGBA, width * 4 a row, top row first. */
#define BANDS ((unsigned)G_N_ELEMENTS(((struct server *)0)->bands))
struct held_rows {
    unsigned serial; /* the frame the buffer holds */
};
/* The rows of the composed frame (mtex[mcur], frame s->serial) that fb lacks, read back into it:
 * with all, the whole frame; else those changed since the frame it holds (s->bands up to this
 * one's), or all of it if that is too long ago. */
static void read_rgb(struct server *s, struct nvnc_fb *fb, int all)
{
    struct held_rows *held = pooled_get_userdata(fb);
    if (!held) {
        held = g_new0(struct held_rows, 1);
        pooled_set_userdata(fb, held, g_free);
    }
    int y0 = 0, y1 = s->height;
    if (!all && held->serial && s->serial - held->serial < BANDS) {
        y0 = s->height;
        y1 = 0;
        for (unsigned n = held->serial + 1; n <= s->serial; n++) {
            y0 = MIN(y0, s->bands[n % BANDS].y0);
            y1 = MAX(y1, s->bands[n % BANDS].y1);
        }
    }
    held->serial = s->serial;
    if (y1 <= y0)
        return; /* it holds this frame already */
    unsigned char *pixels = nvnc_fb_get_addr(fb) + (size_t)y0 * s->width * 4;
    GLint previous;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &previous);
    glBindFramebuffer(GL_FRAMEBUFFER, s->mfbo[s->mcur]);
    glPixelStorei(GL_PACK_ALIGNMENT, 4);
    glReadPixels(0, y0, s->width, y1 - y0, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
    glBindFramebuffer(GL_FRAMEBUFFER, (GLuint)previous);
    if (glGetError() != GL_NO_ERROR)
        die("reading the frame back failed");
    s->rgb_reads++;
    s->rgb_rows += (unsigned long long)(y1 - y0);
}
/* A frame was made (or found unchanged), its main loop work cost_us: the rate's measure. */
static void frame_cost(struct server *s, gint64 cost_us)
{
#ifdef HAVE_VIDEO
    unsigned long long n, us;
    video_server_encoded(s->video, &n, &us);
    rate_encoded(&s->rate, (unsigned)(n - s->enc_n), (int64_t)(us - s->enc_us));
    s->enc_n = n;
    s->enc_us = us;
#endif
    int hz = rate_frame(&s->rate, cost_us);
    if (!hz)
        return;
    fprintf(stderr,
            "beanjar-vnc: %d fps not sustained (a frame's main loop work %.1f ms, encoding %.1f "
            "ms, against %.1f): %d fps from now on\n",
            s->fps, s->rate.last_main / 1000.0,
            s->rate.last_enc < 0 ? 0.0 : s->rate.last_enc / 1000.0, 1000.0 / s->fps, hz);
    s->fps = hz;
    gint64 next = s->pace.next;
    pace_set(&s->pace, hz);
    s->pace.next = next;
#ifdef HAVE_VIDEO
    video_server_set_fps(s->video, hz);
#endif
}
/* A frame as soon as the frame rate allows, while frames are wanted and something changed. */
static void schedule(struct server *s)
{
    if (idle(s) || !s->dirty || s->render_source)
        return;
    gint64 wait = pace_wait(&s->pace, g_get_monotonic_time());
    s->render_source =
        wait > 0 ? g_timeout_add((guint)((wait + 999) / 1000), render, s) : g_idle_add(render, s);
}
#ifdef HAVE_GBM
/* The pending GBM frame is complete: to the video socket, and to neatvnc as render does with its
 * frames; the next render may start. */
static void gbm_ready(struct server *s)
{
    struct frame *f = s->pending;
    s->pending = NULL;
    s->render_us += g_get_monotonic_time() - s->last;
    int partial = 0;
    if (s->diffed) {
        s->diffed = 0;
        frames_diff_read(s->gbm, s->tiles);
        int changed = 0;
        for (int i = 0; i < s->tw * s->th; i++)
            changed += !!s->tiles[i];
        if (!changed) { /* nothing to hand on */
            s->unchanged++;
            vnc_frame_unref(&f->v);
            s->frames++;
            frame_cost(s, g_get_monotonic_time() - s->last);
            schedule(s);
            return;
        }
        partial = 1;
    }
#ifdef HAVE_VIDEO
    /* the frame's texture: its buffer's rows, a multiple of 16 */
    if (!video_nv12(s, f->tex, (s->height + 15) & ~15))
#endif
        video_server_gbm_frame(s->video, &f->v);
    if (rfb_wanted(s) || !s->frames) {
        drop_held(s);
        feed_gbm(s, f, partial);
    } else {
        s->fed_last = 0;
        if (s->clients) {
            drop_held(s);
            vnc_frame_ref(&f->v);
            s->held_frame = f;
        }
    }
    if (s->last_frame)
        vnc_frame_unref(&s->last_frame->v);
    s->last_frame = f; /* the caller's reference */
    s->frames++;
    frame_cost(s, g_get_monotonic_time() - s->last);
    schedule(s);
}
static gboolean gbm_fence_ready(gint fd, GIOCondition condition, gpointer data)
{
    (void)condition;
    close(fd);
    gbm_ready(data);
    return G_SOURCE_REMOVE;
}
/* The scene composed by the GPU into a free GBM frame, handed on once its fence signals (the
 * main loop goes on meanwhile: QEMU's updates are answered); one at a time. */
static gboolean render_gbm(struct server *s)
{
    if (s->pending)
        return G_SOURCE_REMOVE; /* gbm_ready schedules the next */
    struct frame *f = frames_acquire(s->gbm);
    if (!f) { /* all with the encoders or neatvnc: again shortly */
        s->starved++;
        s->render_source = g_timeout_add(4, render, s);
        return G_SOURCE_REMOVE;
    }
    if (wv_frame_fbo(s->v, f->fbo, s->width, s->height))
        die("rendering the frame failed");
    if (s->last_frame && !s->force) { /* compared on the GPU, read in gbm_ready */
        frames_diff(s->gbm, f, s->last_frame, &s->tw, &s->th);
        s->diffed = 1;
    }
    s->force = 0;
    s->dirty = 0;
    s->last = g_get_monotonic_time();
    pace_mark(&s->pace, s->last);
    s->pending = f;
    int fd = s->frames ? gl_fence() : -1; /* the first one: complete before "ready" */
    if (fd < 0) {
        glFinish();
        gbm_ready(s);
    } else
        g_unix_fd_add_full(G_PRIORITY_HIGH, fd, G_IO_IN | G_IO_ERR | G_IO_HUP, gbm_fence_ready, s,
                           NULL);
    return G_SOURCE_REMOVE;
}
#endif
/* Thumbnails (docs/dev/architecture/vnc.md, "Thumbnails"). A Screenshot call smaller than the
 * desktop each way is one: drawn small with several samples a pixel from the frame last composed,
 * after the frame, so that it never delays one, and again only once something changed. */
static int is_thumbnail(struct server *s, int w, int h)
{
    return w <= s->width && h <= s->height && (w < s->width || h < s->height);
}
static int cached(struct server *s, int w, int h)
{
    return s->ppm && s->shot_w == w && s->shot_h == h && s->shot_dw == s->width &&
           s->shot_dh == s->height && s->shot_changes == wv_counter(s->v, "changes");
}
static int write_file(const char *path, const unsigned char *data, size_t len)
{
    FILE *f = fopen(path, "wb");
    if (!f)
        return -1;
    int bad = fwrite(data, 1, len, f) != len;
    return fclose(f) || bad ? -1 : 0;
}
/* The thumbnail at w x h into s->ppm: from mtex while it holds the scene as it is (frames are
 * composed, none due), else from the scene composed now into a target of its own. */
static int shot_make(struct server *s, int w, int h)
{
    gint64 start = g_get_monotonic_time();
    long changes = wv_counter(s->v, "changes");
    GLuint src;
    /* planes hidden (beanjar:planes=): the frames handed on lack them, and the bus's Screenshot
     * is the whole scene, so it is composed now with every plane, and the planes hidden again
     * (which makes the clients' next frame, as it is) */
    int hidden = wv_planes(s->v, -1);
    if (!hidden && !idle(s) && !s->dirty && s->mvalid) { /* memory frames: the last one handed on */
        src = s->mtex[s->mcur ^ 1];
    } else {
        if (s->shot_tw != s->width || s->shot_th != s->height) {
            if (s->shot_fbo) {
                glDeleteFramebuffers(1, &s->shot_fbo);
                glDeleteTextures(1, &s->shot_tex);
            }
            glGenTextures(1, &s->shot_tex);
            glBindTexture(GL_TEXTURE_2D, s->shot_tex);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, s->width, s->height, 0, GL_RGBA,
                         GL_UNSIGNED_BYTE, NULL);
            glBindTexture(GL_TEXTURE_2D, 0);
            GLint previous;
            glGetIntegerv(GL_FRAMEBUFFER_BINDING, &previous);
            glGenFramebuffers(1, &s->shot_fbo);
            glBindFramebuffer(GL_FRAMEBUFFER, s->shot_fbo);
            glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, s->shot_tex,
                                   0);
            int bad = glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE;
            glBindFramebuffer(GL_FRAMEBUFFER, (GLuint)previous);
            if (bad)
                return -1;
            s->shot_tw = s->width;
            s->shot_th = s->height;
        }
        wv_planes(s->v, 0);
        int bad = wv_frame_fbo(s->v, s->shot_fbo, s->width, s->height);
        wv_planes(s->v, hidden);
        if (bad)
            return -1;
        /* after it: bringing the core's cache up to date (idle's skipped updates) counts too */
        changes = wv_counter(s->v, "changes");
        src = s->shot_tex;
    }
    if (!s->thumb)
        s->thumb = thumb_new();
    char head[32];
    int n = snprintf(head, sizeof head, "P6\n%d %d\n255\n", w, h);
    size_t len = (size_t)n + (size_t)w * h * 3;
    unsigned char *ppm = g_malloc(len);
    memcpy(ppm, head, (size_t)n);
    if (thumb_draw(s->thumb, src, s->width, s->height, w, h, ppm + n)) {
        g_free(ppm);
        return -1;
    }
    g_free(s->ppm);
    s->ppm = ppm;
    s->ppm_len = len;
    s->shot_w = w;
    s->shot_h = h;
    s->shot_dw = s->width;
    s->shot_dh = s->height;
    s->shot_changes = changes;
    s->shots++;
    s->shot_us += g_get_monotonic_time() - start;
    return 0;
}
/* The calls queued, oldest first: thumbnails (made if need be when gpu), the others to the core's
 * own screenshot (gpu). Without gpu it stops at the first that needs the GPU: 1, else 0. */
static int shots_answer(struct server *s, int gpu)
{
    const char *path;
    int w, h;
    while ((path = wv_screenshot_request(s->v, &w, &h))) {
        if (!is_thumbnail(s, w, h)) {
            if (!gpu)
                return 1;
            wv_screenshot_reply(s->v, -1);
            continue;
        }
        if (cached(s, w, h))
            s->shots_cached++;
        else if (!gpu)
            return 1;
        else if (shot_make(s, w, h)) {
            wv_screenshot_reply(s->v, 1);
            continue;
        }
        wv_screenshot_reply(s->v, write_file(path, s->ppm, s->ppm_len) ? 1 : 0);
    }
    return 0;
}
static gboolean shots_run(gpointer data)
{
    struct server *s = data;
    s->shot_source = 0;
    shots_answer(s, 1);
    return G_SOURCE_REMOVE;
}
/* after a frame, or while none is due: the ones that need the GPU, once nothing else runs */
static void shots_after_frame(struct server *s)
{
    if (!s->shot_source && wv_screenshot_request(s->v, &(int){0}, &(int){0}))
        s->shot_source = g_idle_add_full(G_PRIORITY_LOW, shots_run, s, NULL);
}
/* the core's hook: a Screenshot call queued */
static void shot_wanted(void *data)
{
    struct server *s = data;
    if (shots_answer(s, 0) && !s->render_source)
        shots_after_frame(s);
}
static void render_frame(struct server *s);
/* A frame (render_frame), then the thumbnails asked for meanwhile. */
static gboolean render(gpointer data)
{
    struct server *s = data;
    s->render_source = 0;
    unsigned frames = s->frames;
    int gbm = 0;
#ifdef HAVE_GBM
    gbm = s->gbm != NULL; /* its frames' work ends in gbm_ready */
#endif
    render_frame(s);
    if (!gbm && s->frames != frames)
        frame_cost(s, g_get_monotonic_time() - s->last);
    shots_after_frame(s);
    return G_SOURCE_REMOVE;
}
static void render_frame(struct server *s)
{
    if (idle(s) || !s->dirty)
        return;
#ifdef HAVE_GBM
    if (s->gbm) {
        render_gbm(s);
        return;
    }
#endif
    if (memory_targets(s))
        die("no framebuffers for the frames");
    if (wv_frame_fbo(s->v, s->mfbo[s->mcur], s->width, s->height))
        die("rendering the frame failed");
    s->dirty = 0;
    s->last = g_get_monotonic_time();
    pace_mark(&s->pace, s->last);
    /* the consumers that take RGB alone, read back for them: RFB clients that want pictures (not
     * a video client alone, or with paused RFB clients: neatvnc is not fed, nor its refinery run;
     * the first frame, before "ready"), and a video encoder that takes RGB (NVENC, V4L2, or
     * swscale's conversion) */
    int rfb = rfb_wanted(s) || !s->frames, video_rgb = 0;
#ifdef HAVE_VIDEO
    video_rgb = video_server_rgb_wanted(s->video);
#endif
    /* this frame's serial: the read backs bring buffers up to it (an unchanged one gives it up) */
    s->serial++;
    int compare = s->mvalid && !s->force, partial = 0, changed = 1, read = 0;
    int last = s->bands[(s->serial - 1) % BANDS].y1 - s->bands[(s->serial - 1) % BANDS].y0;
    struct nvnc_fb *fb = NULL;
    /* RGB wanted, and the last changed frame changed most rows (video playing, a page scrolled):
     * read back whole and compared on the CPU, which stops at a difference, as without the
     * comparison on the GPU the read back waits for it; else compared on the GPU first, and only
     * the changed rows read back */
    if ((rfb || video_rgb) && (!compare || (s->last_fb && last * 2 > s->height))) {
        if (!(fb = nvnc_fb_pool_acquire(s->pool)))
            die("out of frame buffers");
        read_rgb(s, fb, 1);
        read = 1;
        if (compare)
            changed = tiles_compare(s, nvnc_fb_get_addr(fb), nvnc_fb_get_addr(s->last_fb));
    } else if (compare) {
        int tw, th;
        tilediff_run(s->diff, s->mtex[s->mcur], s->mtex[!s->mcur], s->height, &tw, &th);
        tilediff_read(s->diff, s->tiles);
        changed = 0;
        for (int i = 0; i < tw * th; i++)
            changed += !!s->tiles[i];
    }
    if (!changed) { /* the next one is composed into the same target */
        s->serial--;
        if (fb) { /* it holds the last frame, which this one equals */
            ((struct held_rows *)pooled_get_userdata(fb))->serial = s->serial;
            nvnc_fb_unref(fb);
        }
        s->unchanged++;
        s->frames++;
        return;
    }
    partial = compare;
    /* this frame's changed rows: those of its changed tiles, or all */
    int y0 = 0, y1 = s->height;
    if (partial) {
        int first = -1, end = 0;
        for (int ty = 0; ty < s->th; ty++)
            for (int tx = 0; tx < s->tw; tx++)
                if (s->tiles[ty * s->tw + tx]) {
                    first = first < 0 ? ty : first;
                    end = ty + 1;
                    break;
                }
        y0 = first * TILE;
        y1 = MIN(s->height, end * TILE);
    }
    s->bands[s->serial % BANDS].y0 = y0;
    s->bands[s->serial % BANDS].y1 = y1;
    if ((rfb || video_rgb) && !read) {
        if (!(fb = nvnc_fb_pool_acquire(s->pool)))
            die("out of frame buffers");
        read_rgb(s, fb, 0);
    }
    s->force = 0;
#ifdef HAVE_VIDEO
    if (!video_nv12(s, s->mtex[s->mcur], s->height) && video_server_rgb_wanted(s->video)) {
        if (!fb) { /* the GPU's conversion failed just now */
            if (!(fb = nvnc_fb_pool_acquire(s->pool)))
                die("out of frame buffers");
            read_rgb(s, fb, 0);
        }
        video_server_frame(s->video, nvnc_fb_get_addr(fb));
    }
#endif
    if (rfb) {
        drop_held(s);
        feed_damage(s, fb, partial);
    } else {
        /* a paused client's resume gets the latest frame read back, or a fresh render */
        s->fed_last = 0;
        drop_held(s);
        if (fb && s->clients) {
            nvnc_fb_ref(fb);
            s->held = fb;
        }
    }
    /* the last one handed on, read back, for the next comparison: the pool hands out another */
    if (s->last_fb)
        nvnc_fb_unref(s->last_fb);
    s->last_fb = fb; /* NULL: this one is not read back */
    s->mcur = !s->mcur;
    s->mvalid = 1;
    s->frames++;
}

/* LG's cursor (wv_cursor) as the clients' cursor: cropped to its visible pixels and the hotspot,
 * scaled like the TV picture into the desktop (as the composed cursor is drawn), straight alpha;
 * hidden, a transparent 1x1 one. neatvnc sends it to every client that takes the Cursor
 * pseudo-encoding, and to clients that connect later. */
static void premultiply(uint8_t *p, size_t n, int un)
{
    for (size_t i = 0; i < n; i++, p += 4) {
        unsigned a = p[3];
        for (int k = 0; k < 3; k++)
            p[k] = un ? (a ? (uint8_t)MIN(255u, (p[k] * 255u + a / 2) / a) : 0)
                      : (uint8_t)((p[k] * a + 127) / 255);
    }
}
/* Sends it, unless it is what was last sent. */
static void set_cursor(struct server *s, struct nvnc_fb *fb, int w, int h, int hx, int hy,
                       const char *lg)
{
    size_t bytes = (size_t)w * h * 4;
    const uint8_t *pixels = nvnc_fb_get_addr(fb);
    if (s->cursor_pixels && w == s->cursor_w && h == s->cursor_h && hx == s->cursor_hx &&
        hy == s->cursor_hy && !memcmp(pixels, s->cursor_pixels, bytes))
        return;
    g_free(s->cursor_pixels);
    s->cursor_pixels = g_memdup2(pixels, bytes);
    /* The WebSocket listener's clients are noVNC, which (1.7.0 and master, core/rfb.js
     * _handleCursor, "Decode from BGRX pixels") reads a Cursor's pixels as BGRX though it asks for
     * RGBX (red-shift 0), so it shows red and blue swapped: that listener gets them swapped first.
     * Another WebSocket client sees them swapped. */
    struct nvnc_fb *swapped = NULL;
    bool any_ws = false;
    for (int i = 0; i < s->servers; i++)
        any_ws |= s->ws[i];
    if (any_ws) {
        swapped = nvnc_fb_new((uint16_t)w, (uint16_t)h, FOURCC_ABGR8888, (uint16_t)w);
        if (!swapped)
            die("out of memory for the cursor");
        uint8_t *q = nvnc_fb_get_addr(swapped);
        for (size_t i = 0; i < bytes; i += 4) {
            q[i] = pixels[i + 2];
            q[i + 1] = pixels[i + 1];
            q[i + 2] = pixels[i];
            q[i + 3] = pixels[i + 3];
        }
    }
    for (int i = 0; i < s->servers; i++) {
        struct nvnc_fb *f = s->ws[i] ? swapped : fb;
        nvnc_set_cursor(s->nvnc[i], f, (uint16_t)hx, (uint16_t)hy, true);
    }
    if (swapped)
        nvnc_fb_unref(swapped);
    fprintf(stderr, "beanjar-vnc: cursor %dx%d hotspot %d,%d%s\n", w, h, hx, hy, lg);
    s->cursor_w = w;
    s->cursor_h = h;
    s->cursor_hx = hx;
    s->cursor_hy = hy;
    s->cursors++;
}
static void update_cursor(struct server *s)
{
    if (!s->native || !s->servers)
        return;
    unsigned serial = wv_cursor(s->v, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
    if (s->cursor_seen && serial == s->cursor_serial)
        return;
    s->cursor_serial = serial;
    s->cursor_seen = 1;
    static uint8_t raw[CURSOR_MAX * CURSOR_MAX * 4];
    int w = 0, h = 0, hx = 0, hy = 0, vw = 0, vh = 0;
    /* the visible pixels' box, and the hotspot */
    int x0 = INT_MAX, y0 = INT_MAX, x1 = -1, y1 = -1;
    if (serial && wv_cursor(s->v, raw, &w, &h, &hx, &hy, &vw, &vh))
        for (int y = 0; y < h; y++)
            for (int x = 0; x < w; x++)
                if (raw[((size_t)y * w + x) * 4 + 3]) {
                    x0 = MIN(x0, x);
                    y0 = MIN(y0, y);
                    x1 = MAX(x1, x);
                    y1 = MAX(y1, y);
                }
    if (x1 < 0) { /* hidden, or nothing visible */
        struct nvnc_fb *fb = nvnc_fb_new(1, 1, FOURCC_ABGR8888, 1);
        if (!fb)
            die("out of memory for the cursor");
        memset(nvnc_fb_get_addr(fb), 0, 4);
        set_cursor(s, fb, 1, 1, 0, 0, serial ? " (LG's: nothing visible)" : " (LG's hidden)");
        nvnc_fb_unref(fb);
        return;
    }
    int bx = x0, by = y0, bw = x1 - x0 + 1, bh = y1 - y0 + 1; /* the visible box, for the log */
    x0 = MIN(x0, hx);
    y0 = MIN(y0, hy);
    x1 = MAX(x1, MIN(hx, w - 1));
    y1 = MAX(y1, MIN(hy, h - 1));
    int cw = x1 - x0 + 1, ch = y1 - y0 + 1;
    double sx = (double)vw / w, sy = (double)vh / h;
    /* larger desktops (from about 2900 pixels wide): smaller than the TV picture's, to fit */
    double fit = MIN(1.0, (double)CURSOR_SENT_MAX / MAX(cw * sx, ch * sy));
    sx *= fit;
    sy *= fit;
    int ow = MIN(CURSOR_SENT_MAX, MAX(1, (int)(cw * sx + 0.5))),
        oh = MIN(CURSOR_SENT_MAX, MAX(1, (int)(ch * sy + 0.5)));
    struct nvnc_fb *fb = nvnc_fb_new((uint16_t)ow, (uint16_t)oh, FOURCC_ABGR8888, (uint16_t)ow);
    if (!fb)
        die("out of memory for the cursor");
    uint8_t *out = nvnc_fb_get_addr(fb);
    memset(out, 0, (size_t)ow * oh * 4);
    /* scaled with premultiplied alpha (pixman's), so no colour bleeds from transparent pixels */
    premultiply(raw, (size_t)w * h, 0);
    pixman_image_t *src = pixman_image_create_bits(
        PIXMAN_a8b8g8r8, cw, ch, (uint32_t *)(raw + ((size_t)y0 * w + x0) * 4), w * 4);
    pixman_image_t *dst =
        pixman_image_create_bits(PIXMAN_a8b8g8r8, ow, oh, (uint32_t *)out, ow * 4);
    if (!src || !dst)
        die("out of memory for the cursor");
    struct pixman_transform t;
    pixman_transform_init_scale(&t, pixman_double_to_fixed((double)cw / ow),
                                pixman_double_to_fixed((double)ch / oh));
    pixman_image_set_transform(src, &t);
    pixman_image_set_filter(src, ow == cw && oh == ch ? PIXMAN_FILTER_NEAREST : PIXMAN_FILTER_GOOD,
                            NULL, 0);
    pixman_image_set_repeat(src, PIXMAN_REPEAT_PAD); /* the crop's edges, not transparency */
    pixman_image_composite32(PIXMAN_OP_SRC, src, NULL, dst, 0, 0, 0, 0, 0, 0, ow, oh);
    pixman_image_unref(src);
    pixman_image_unref(dst);
    premultiply(out, (size_t)ow * oh, 1);
    int ohx = MIN(ow - 1, (int)((hx - x0) * sx)), ohy = MIN(oh - 1, (int)((hy - y0) * sy));
    char lg[96];
    snprintf(lg, sizeof lg,
             " (LG's: %dx%d, visible %dx%d at %d,%d, hotspot %d,%d, %dx%d in the desktop)", w, h,
             bw, bh, bx, by, hx, hy, vw, vh);
    set_cursor(s, fb, ow, oh, ohx, ohy, lg);
    nvnc_fb_unref(fb);
}

/* the core's callbacks */
static void redraw(void *data)
{
    struct server *s = data;
    update_cursor(s);
    s->dirty = 1;
    schedule(s);
}
#ifdef HAVE_VIDEO
/* the video clients changed (one starts: a frame for it, as for a new RFB client; the last
 * left: idle again, unless RFB clients remain) */
static void video_wake(void *data)
{
    struct server *s = data;
    update_idle(s);
    s->dirty = 1;
    s->force = 1;
    schedule(s);
}
#endif
static void quit(void *data)
{
    struct server *s = data;
    g_main_loop_quit(s->loop);
}

/* neatvnc's callbacks, all on the main loop's thread (aml_dispatch below) */
static struct server *server_of(struct nvnc_client *client)
{
    return nvnc_get_userdata(nvnc_client_get_server(client));
}
static void key(struct server *s, unsigned evdev, bool down)
{
    if (evdev && wv_key_evdev(s->v, evdev, down))
        s->keys++;
    else if (down)
        s->unmapped++;
}
static void on_keysym(struct nvnc_client *client, uint32_t keysym, bool down)
{
    key(server_of(client), keysym_evdev(keysym), down);
}
/* QEMU's extended key events carry the key itself (neatvnc: as an evdev code) */
static void on_keycode(struct nvnc_client *client, uint32_t evdev, bool down)
{
    key(server_of(client), evdev, down);
}
static void on_pointer(struct nvnc_client *client, uint16_t x, uint16_t y,
                       enum nvnc_button_mask mask)
{
    struct server *s = server_of(client);
    struct client *c = nvnc_client_get_userdata(client);
    unsigned pressed = mask & ~c->buttons;
    s->pointer_events++;
    wv_motion(s->v, x, y);
    /* buttons 1, 2, 3 (left, middle, right) are the core's 0, 1, 2 */
    for (unsigned b = 0; b < 3; b++)
        if ((mask ^ c->buttons) & (1u << b))
            wv_button(s->v, b, !!(mask & (1u << b)), x, y);
    /* the wheel's buttons 4 (up) and 5 (down) are clicks: one step each press */
    if (pressed & NVNC_SCROLL_UP)
        wv_wheel(s->v, 1);
    if (pressed & NVNC_SCROLL_DOWN)
        wv_wheel(s->v, -1);
    c->buttons = mask;
}
/* a client's userdata freed: it disconnected (neatvnc 1.0 has no cleanup function besides) */
static void client_gone(void *data)
{
    struct client *c = data;
    struct server *s = c->s;
    s->paused -= c->paused; /* a closed connection is not paused any more */
    /* the buttons it held; with the last client also the keys and the pointer */
    for (unsigned b = 0; b < 3; b++)
        if (c->buttons & (1u << b))
            wv_button(s->v, b, 0, 0, 0);
    if (--s->clients == 0) {
        wv_release(s->v);
        wv_leave(s->v);
        drop_held(s);
    }
    update_idle(s);
    fprintf(stderr, "beanjar-vnc: client left (%d connected)\n", s->clients);
    g_free(c);
}
static void on_client(struct nvnc_client *client)
{
    struct server *s = server_of(client);
    struct client *c = g_new0(struct client, 1);
    c->s = s;
    nvnc_client_set_userdata(client, c, client_gone);
    s->clients++;
    update_idle(s);
    fprintf(stderr, "beanjar-vnc: client connected (%d connected)\n", s->clients);
    /* a frame for it: neatvnc sends what it holds, which is stale while no one was connected */
    s->dirty = 1;
    s->force = 1;
    schedule(s);
}

/* A --local client paused or resumed (beanjar-web: its page's H.264 stream started or ended).
 * Resumed, neatvnc gets the latest frame at once, as a whole frame, before the request for the
 * whole screen that follows the marker; then a fresh one. */
static void set_paused(struct server *s, struct client *c, bool paused)
{
    if (c->paused == paused)
        return;
    c->paused = paused;
    s->paused += paused ? 1 : -1;
    fprintf(stderr, "beanjar-vnc: RFB client %s (%d of %d paused, vnc_feeds=%u)\n",
            paused ? "paused" : "resumed", s->paused, s->clients, s->feeds);
    update_idle(s);
    if (paused)
        return;
    int fed = 0;
    if (s->held) {
        feed(s, s->held);
        drop_held(s);
        fed = 1;
    }
#ifdef HAVE_GBM
    if (s->held_frame) {
        feed_gbm(s, s->held_frame, 0);
        drop_held(s);
        fed = 1;
    }
#endif
    s->dirty = 1;
    if (!fed) { /* nothing read back while paused: a whole frame, rendered now */
        s->force = 1;
        if (s->render_source)
            g_source_remove(s->render_source);
        s->render_source = 0;
        render(s);
        return;
    }
    schedule(s);
}
/* ClientCutText, on the --local listener only (registered there alone): beanjar-web's markers.
 * Anything else is not taken; clipboard handling added here must filter the markers out first,
 * and never act on them from another listener. */
static void on_cut_text(struct nvnc_client *client, const char *text, uint32_t len)
{
    struct server *s = server_of(client);
    if (s->local < 0 || nvnc_client_get_server(client) != s->nvnc[s->local])
        return;
    bool pause = len == strlen(MARK_PAUSE) && !memcmp(text, MARK_PAUSE, len),
         resume = len == strlen(MARK_RESUME) && !memcmp(text, MARK_RESUME, len);
    if (pause || resume)
        set_paused(s, nvnc_client_get_userdata(client), pause);
    size_t n = strlen(MARK_PLANES);
    bool ask = len == strlen(MARK_PLANES_ASK) && !memcmp(text, MARK_PLANES_ASK, len),
         set = len == n + 1 && !memcmp(text, MARK_PLANES, n) && text[n] >= '0' && text[n] <= '7';
    if (!ask && !set)
        return;
    if (set) {
        int before = wv_planes(s->v, -1), hidden = wv_planes(s->v, text[n] - '0');
        if (hidden != before)
            fprintf(stderr, "beanjar-vnc: planes hidden %d (video %s, UI %s, cursor %s)\n", hidden,
                    hidden & WV_PLANE_VIDEO ? "off" : "on", hidden & WV_PLANE_UI ? "off" : "on",
                    hidden & WV_PLANE_CURSOR ? "off" : "on");
    }
    char reply[32];
    int r = snprintf(reply, sizeof reply, MARK_PLANES "%d", wv_planes(s->v, -1));
    nvnc_send_cut_text(s->nvnc[s->local], reply, (uint32_t)r);
}

/* aml's events: its fd is readable while it has any (sockets, timers, the encoders' workers) */
static gboolean aml_ready(gint fd, GIOCondition condition, gpointer data)
{
    (void)fd;
    (void)condition;
    struct server *s = data;
    aml_poll(s->aml, 0);
    aml_dispatch(s->aml);
    return G_SOURCE_CONTINUE;
}

/* --gl-adapter: the GL the picture is composed with, chosen before EGL and any thread. On WSL
 * (/dev/dxg) Mesa 25 gives llvmpipe unless GALLIUM_DRIVER names d3d12, which composes on a
 * Windows GPU for a sixth of the CPU (docs/dev/architecture/vnc.md, "GL"); so it is set there,
 * unless GALLIUM_DRIVER or LIBGL_ALWAYS_SOFTWARE is set already. adapter (NULL: none given): d3d12
 * on the GPU whose name contains it (MESA_D3D12_DEFAULT_ADAPTER_NAME), whatever the environment
 * says; without one, d3d12's default adapter (QEMU's too). "software": llvmpipe, anywhere. */
static void gl_driver(const char *adapter)
{
    int wsl = !access("/dev/dxg", F_OK);
    if (adapter && !strcmp(adapter, "software")) {
        setenv("LIBGL_ALWAYS_SOFTWARE", "1", 1);
        setenv("GALLIUM_DRIVER", "llvmpipe", 1);
        fprintf(stderr, "beanjar-vnc: GL: llvmpipe (--gl-adapter software)\n");
        return;
    }
    if (!wsl) {
        if (adapter)
            fprintf(stderr, "beanjar-vnc: GL: --gl-adapter %s ignored: not WSL\n", adapter);
        return;
    }
    const char *driver = getenv("GALLIUM_DRIVER");
    if (!adapter && (driver || getenv("LIBGL_ALWAYS_SOFTWARE"))) {
        fprintf(stderr, "beanjar-vnc: GL: as the environment has it (%s%s)\n",
                driver ? "GALLIUM_DRIVER=" : "LIBGL_ALWAYS_SOFTWARE", driver ? driver : "");
        return;
    }
    setenv("GALLIUM_DRIVER", "d3d12", 1);
    unsetenv("LIBGL_ALWAYS_SOFTWARE");
    if (adapter)
        setenv("MESA_D3D12_DEFAULT_ADAPTER_NAME", adapter, 1);
    adapter = getenv("MESA_D3D12_DEFAULT_ADAPTER_NAME");
    fprintf(stderr, "beanjar-vnc: GL: Mesa's d3d12 (WSL), %s%s\n",
            adapter ? "adapter " : "the default adapter", adapter ? adapter : "");
}

/* A GLES 2 context on Mesa's surfaceless platform: a render node, no window system. */
static void egl(void)
{
    PFNEGLGETPLATFORMDISPLAYEXTPROC platform_display =
        (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
    EGLDisplay display = platform_display ? platform_display(EGL_PLATFORM_SURFACELESS_MESA,
                                                             EGL_DEFAULT_DISPLAY, NULL)
                                          : EGL_NO_DISPLAY;
    if (display == EGL_NO_DISPLAY || !eglInitialize(display, NULL, NULL))
        die("no surfaceless EGL display (EGL_MESA_platform_surfaceless)");
    if (!eglBindAPI(EGL_OPENGL_ES_API))
        die("no OpenGL ES");
    const char *ext = eglQueryString(display, EGL_EXTENSIONS);
    EGLConfig config = (EGLConfig)0; /* EGL_NO_CONFIG_KHR */
    if (!ext || !strstr(ext, "EGL_KHR_no_config_context")) {
        static const EGLint attrs[] = {EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_SURFACE_TYPE, 0,
                                       EGL_NONE};
        EGLint n = 0;
        if (!eglChooseConfig(display, attrs, &config, 1, &n) || n < 1)
            die("no EGL config for OpenGL ES 2");
    }
    static const EGLint context_attrs[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
    EGLContext context = eglCreateContext(display, config, EGL_NO_CONTEXT, context_attrs);
    if (context == EGL_NO_CONTEXT)
        die("cannot create an OpenGL ES 2 context");
    if (!eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, context))
        die("cannot make the context current without a surface (EGL_KHR_surfaceless_context)");
}

#ifdef HAVE_VIDEO
#define VIDEO_USAGE                                                                                \
    "  [--video-local PATH] [--video-encoder auto|nvenc|vaapi|v4l2|x264] [--vaapi-adapter NAME]\n"
#define VIDEO_HELP                                                                                 \
    "--video-local PATH: the composed picture as H.264 on a unix socket at PATH, mode 0600\n"      \
    "(beanjar vnc video-pipe); --video-encoder: auto (the default: NVENC, then VA-API, then\n"     \
    "a V4L2 encoder, then x264) or one of them. On WSL vaapi is Mesa's d3d12 driver over vgem's\n" \
    "render node (modprobe vgem; the node readable by this user); --vaapi-adapter: the GPU "       \
    "whose\n"                                                                                      \
    "name contains NAME, e.g. NVIDIA or Intel (MESA_D3D12_DEFAULT_ADAPTER_NAME).\n"
#else
#define VIDEO_USAGE ""
#define VIDEO_HELP  ""
#endif
static const char usage[] =
    "usage: beanjar-vnc BUS_ADDRESS --listen ADDRESS [--port N] [--websocket N] [--size WxH] "
    "[--fps N]\n"
    "  [--name NAME] [--native-cursor] [--password-file PATH] [--unix PATH] [--local PATH]\n"
    "  [--cursor-socket PATH] [--video-socket PATH] [--ir-socket PATH] [--media-socket PREFIX]\n"
    "  [--capture-socket PATH] [--no-media] [--hwdec auto|none|TYPE] [--no-osd] "
    "[--audio-local PATH]\n  [--gl-adapter NAME|software] [--frames auto|gbm|memory] "
    "[--hide-planes video,ui,cursor]\n" VIDEO_USAGE
    "BUS_ADDRESS: the VM's D-Bus display (unix:path=VM/run/dbus.sock; the other sockets beside "
    "it).\n"
    "ADDRESS: the VM's own, 127.0.1.N (beanjar vnc start gives it). Defaults: port 5900, "
    "1280x720,\n"
    "the VM's panel's refresh (vm-info.toml's panel_hz, else 60) in fps, any rate; above 60 "
    "only\nwhile a frame's work fits in its time, else what it allows for the session.\n"
    "--websocket N: also RFB over WebSocket on ADDRESS:N, for noVNC in a browser (0: none).\n"
    "--password-file PATH: clients authenticate with the password on its first line (RSA-AES).\n"
    "Required unless ADDRESS is loopback (127.0.0.0/8, ::1), and with --unix. Not with "
    "--websocket.\n"
    "--unix PATH: also RFB on a unix socket at PATH, for a relay to serve elsewhere (beanjar vnc\n"
    "start: a link lan VM's LAN address).\n"
    "--local PATH: also RFB without authentication on a unix socket at PATH, mode 0600, for this\n"
    "host's own clients (beanjar vnc pipe): its permissions are the access control.\n"
    "--audio-local PATH: the guest's and the media's audio, mixed, on a unix socket at PATH, mode\n"
    "0600, as Opus or 48 kHz s16 PCM, as each client asks (beanjar vnc audio-pipe).\n" VIDEO_HELP
    "--native-cursor: LG's cursor sent as the clients' own cursor (the VNC Cursor pseudo-encoding, "
    "with\n"
    "a 1-bit mask: no translucency), instead of drawn into the picture at the VNC pointer.\n"
    "--gl-adapter NAME: on WSL the picture is composed with Mesa's d3d12 on the GPU whose\n"
    "name contains NAME (e.g. NVIDIA, Intel). Without it, d3d12's default adapter unless\n"
    "GALLIUM_DRIVER or LIBGL_ALWAYS_SOFTWARE is set; software: llvmpipe (anywhere).\n"
    "--frames: gbm composes into GBM buffers handed on as dma-bufs (neatvnc's Open H.264, the\n"
    "video socket's V4L2 encoders), memory reads each frame back; auto (the default): gbm where\n"
    "a V4L2 encoder makes H.264 from RGB, memory elsewhere.\n"
    "--no-osd: the video plane alone (diagnostics).\n"
    "--hide-planes LIST: the planes left out of the picture at start (video, ui, cursor); the\n"
    "web viewer's More menu changes them for every client (beanjar:planes= on --local).";
/* SIGUSR1: the counters so far on one line (docs/dev/architecture/vnc.md, "Counters"), for a
 * measurement to take before and after: the time, what QEMU handed over and how long it waited,
 * the renders, the unchanged ones, the frames fed to neatvnc, the video socket's units. */
static gboolean print_stats(gpointer data)
{
    struct server *s = data;
    long render_us = 0;
#ifdef HAVE_GBM
    render_us = (long)s->render_us;
#endif
    unsigned long long units = 0;
    unsigned nv12 = 0;
#ifdef HAVE_VIDEO
    units = video_server_units(s->video);
    nv12 = s->nv12;
#endif
    fprintf(stderr,
            "beanjar-vnc: stats t_us=%lld updates=%ld scanouts=%ld copies=%ld reply_us=%ld "
            "reply_max_us=%ld skipped=%ld renders=%u unchanged=%u render_us=%ld feeds=%u "
            "video_units=%llu video_nv12=%u rgb_reads=%u rgb_rows=%llu thumbnails=%u "
            "thumbnails_cached=%u thumbnail_us=%lld cache_w=%ld cache_h=%ld recopies=%ld fps=%d "
            "frame_main_us=%lld frame_encode_us=%lld disables=%ld disables_dropped=%ld\n",
            (long long)g_get_monotonic_time(), wv_counter(s->v, "dmabuf_updates"),
            wv_counter(s->v, "imports"), wv_counter(s->v, "frames"), wv_counter(s->v, "reply_us"),
            wv_counter(s->v, "reply_max_us"), wv_counter(s->v, "skipped"), s->frames, s->unchanged,
            render_us, s->feeds, units, nv12, s->rgb_reads, s->rgb_rows, s->shots, s->shots_cached,
            (long long)s->shot_us, wv_counter(s->v, "cache_w"), wv_counter(s->v, "cache_h"),
            wv_counter(s->v, "recopies"), s->fps, (long long)s->rate.last_main,
            (long long)s->rate.last_enc, wv_counter(s->v, "disables"),
            wv_counter(s->v, "disables_dropped"));
    return G_SOURCE_CONTINUE;
}
int main(int argc, char **argv)
{
    struct server s = {.width = 1280, .height = 720, .local = -1};
    const char *addr = NULL, *listen = NULL, *name = "Beanjar", *password_file = NULL,
               *unix_path = NULL, *local_path = NULL, *audio_path = NULL, *video_path = NULL,
               *video_encoder = "auto", *vaapi_adapter = NULL, *gl_adapter = NULL,
               *frames_mode = "auto";
    int port = 5900, fps = 0 /* the panel's */, websocket = 0;
    wv_signals();
    signal(SIGPIPE, SIG_IGN);
    s.v = wv_new();
    /* nothing played here: the media's decoded against a clock (the core's own, or with
     * --audio-local the audio socket's), the guest's audio listened to only for that socket */
    wv_option(s.v, "headless", NULL);
    wv_option(s.v, "media-silent", NULL);
    static const char *const valued[] = {
        "cursor-socket",  "video-socket", "ir-socket",   "media-socket",
        "capture-socket", "hwdec",        "hide-planes", NULL};
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i], *option = a[0] == '-' && a[1] == '-' ? a + 2 : "";
        const char *value = i + 1 < argc ? argv[i + 1] : NULL;
        char tail;
        int done = 0;
        if (!strcmp(a, "--help") || !strcmp(a, "-h")) {
            puts(usage);
            return 0;
        }
        if (!strcmp(option, "listen") && value) {
            listen = value;
            i++;
            continue;
        }
        if (!strcmp(option, "name") && value) {
            name = value;
            i++;
            continue;
        }
        if (!strcmp(option, "password-file") && value) {
            password_file = value;
            i++;
            continue;
        }
        if (!strcmp(option, "unix") && value) {
            unix_path = value;
            i++;
            continue;
        }
        if (!strcmp(option, "local") && value) {
            local_path = value;
            i++;
            continue;
        }
        if (!strcmp(option, "audio-local") && value) {
            audio_path = value;
            i++;
            continue;
        }
        if (!strcmp(option, "video-local") && value) {
            video_path = value;
            i++;
            continue;
        }
        if (!strcmp(option, "video-encoder") && value) {
            video_encoder = value;
            i++;
            continue;
        }
        if (!strcmp(option, "frames") && value) {
            frames_mode = value;
            i++;
            continue;
        }
        if (!strcmp(option, "gl-adapter") && value) {
            gl_adapter = value;
            i++;
            continue;
        }
        if (!strcmp(option, "vaapi-adapter") && value) {
            vaapi_adapter = value;
            i++;
            continue;
        }
        if (!strcmp(option, "port") && value) {
            if (sscanf(value, "%d%c", &port, &tail) != 1 || port < 1 || port > 65535)
                die("invalid port");
            i++;
            continue;
        }
        if (!strcmp(option, "websocket") && value) {
            if (sscanf(value, "%d%c", &websocket, &tail) != 1 || websocket < 0 || websocket > 65535)
                die("invalid websocket port");
            i++;
            continue;
        }
        if (!strcmp(option, "fps") && value) {
            if (sscanf(value, "%d%c", &fps, &tail) != 1 || fps < 1 || fps > OUTPUT_HZ_LIMIT)
                die("invalid frame rate");
            i++;
            continue;
        }
        if (!strcmp(option, "size") && value) {
            if (sscanf(value, "%dx%d%c", &s.width, &s.height, &tail) != 2 || s.width < 64 ||
                s.height < 64 || s.width > 4096 || s.height > 4096)
                die("invalid size");
            i++;
            continue;
        }
        if (!strcmp(option, "native-cursor")) {
            s.native = 1;
            continue;
        }
        if (!strcmp(option, "no-media") || !strcmp(option, "no-osd")) {
            wv_option(s.v, option, NULL);
            continue;
        }
        for (int k = 0; !done && option[0] && valued[k]; k++)
            if (!strcmp(option, valued[k]) && value) {
                if (wv_option(s.v, option, value))
                    die(usage);
                i++;
                done = 1;
            }
        if (done)
            continue;
        if (a[0] != '-' && !addr) {
            addr = a;
            continue;
        }
        die(usage);
    }
    if (!addr || !listen)
        die(usage);
    if (websocket == port)
        die("--port and --websocket are the same port");
    if (password_file) {
        password = read_password(password_file);
        if (strlen(password) > 255)
            die("the password is longer than 255 bytes (RSA-AES's limit)");
        if (!nvnc_has_auth())
            die("neatvnc is built without authentication: --password-file cannot be used");
    }
    /* neatvnc's RSA-AES replaces a WebSocket stream with a TCP one (stream_upgrade_to_rsa_eas):
     * no authentication over WebSocket, so no WebSocket listener with a password */
    if (password && websocket)
        die("--websocket cannot be used with a password: neatvnc cannot authenticate WebSocket "
            "clients (its RSA-AES drops the WebSocket framing), so a browser (noVNC) would get in "
            "without "
            "the password or not at all. Use a VNC client on --port, or noVNC on loopback without "
            "a password.");
    if (unix_path && local_path && !strcmp(unix_path, local_path))
        die("--unix and --local are the same path");
    if (audio_path && ((unix_path && !strcmp(audio_path, unix_path)) ||
                       (local_path && !strcmp(audio_path, local_path))))
        die("--audio-local is the path of another socket");
    if (video_path && ((unix_path && !strcmp(video_path, unix_path)) ||
                       (local_path && !strcmp(video_path, local_path)) ||
                       (audio_path && !strcmp(video_path, audio_path))))
        die("--video-local is the path of another socket");
#ifdef HAVE_VIDEO
    if (!video_encoder_known(video_encoder))
        die("--video-encoder is auto, nvenc, vaapi, v4l2 or x264");
    if (vaapi_adapter && strcmp(video_encoder, "vaapi"))
        die("--vaapi-adapter is for --video-encoder vaapi");
#else
    (void)video_encoder;
    (void)vaapi_adapter;
    if (video_path)
        die("built without libavcodec and libswscale: no --video-local");
#endif
    if (strcmp(frames_mode, "auto") && strcmp(frames_mode, "gbm") && strcmp(frames_mode, "memory"))
        die("--frames is auto, gbm or memory");
#ifndef HAVE_GBM
    if (!strcmp(frames_mode, "gbm"))
        die("built without GBM: no --frames gbm");
#endif
    if (!password && unix_path)
        die("--unix needs a --password-file: a relay serves it elsewhere");
    if (!password && !loopback(listen)) {
        fprintf(stderr,
                "beanjar-vnc: %s is not a loopback address: give a --password-file "
                "(clients then authenticate with RSA-AES)\n",
                listen);
        return 1;
    }
    wv_option(s.v, "address", addr);
    wv_option(s.v, "output-hz", "0"); /* paced here (s.pace), not by the core */
    if (s.native)
        wv_option(s.v, "no-cursor", NULL);

#ifdef HAVE_VIDEO
    /* before EGL and any thread: the environment it sets for the VA driver (WSL) */
    if (video_path && !strcmp(video_encoder, "vaapi"))
        video_vaapi_prepare(vaapi_adapter);
#endif
    gl_driver(gl_adapter); /* after the VA driver's: its variables are its own */

    struct audio_server *audio = NULL;
    if (audio_path) {
        audio = audio_server_new(audio_path, s.v);
        if (!audio) {
            fprintf(stderr, "beanjar-vnc: cannot listen on %s: %s\n", audio_path, strerror(errno));
            return 1;
        }
    } else
        wv_option(s.v, "no-audio", NULL);

    egl();
    struct vnc_frame *gbm = NULL; /* a GBM frame, for its geometry; NULL: read back into memory */
#ifdef HAVE_GBM
    if (!strcmp(frames_mode, "auto")) {
        /* GBM frames where a V4L2 encoder makes H.264 from them (neatvnc's Open H.264, the video
         * socket's v4l2); elsewhere the read back as before */
        char node[96];
        int fd = v4l2_find_m2m(V4L2_PIX_FMT_XBGR32, V4L2_PIX_FMT_H264, node, sizeof node);
        if (fd >= 0) {
            close(fd);
            frames_mode = "gbm";
            fprintf(stderr, "beanjar-vnc: frames: GBM, for the H.264 encoder %s\n", node);
        }
    }
    if (!strcmp(frames_mode, "gbm")) {
        /* the display's, each encoder's slot and work, neatvnc's encoders, one being composed */
        s.gbm = frames_new(s.width, s.height, 8);
        if (s.gbm) {
            struct frame *f = frames_acquire(s.gbm);
            gbm = &f->v;
            vnc_frame_unref(gbm);
            fprintf(stderr, "beanjar-vnc: frames: GBM buffers on %s, %u bytes a row, %zu bytes\n",
                    frames_node(s.gbm), gbm->stride, gbm->size);
        }
    }
#endif
#ifdef HAVE_VIDEO
    /* RGB to NV12 on the GPU for the encoders that take YUV; BEANJAR_VNC_SWSCALE (set): swscale
     * as before, for the fallback's tests and for comparisons */
    if (video_path && strcmp(video_encoder, "nvenc") && strcmp(video_encoder, "v4l2")) {
        const char *why = "BEANJAR_VNC_SWSCALE is set";
        const char *env = getenv("BEANJAR_VNC_SWSCALE");
        if (!env || !*env || !strcmp(env, "0"))
            s.yuv = yuv_new(&why);
        if (!s.yuv)
            fprintf(stderr,
                    "beanjar-vnc: video: no RGB to NV12 on the GPU (%s): swscale converts\n", why);
    }
    if (video_path) {
        s.video = video_server_new(video_path, video_encoder, s.width, s.height,
                                   fps ? fps : RATE_FLOOR, gbm, s.yuv != NULL, video_wake, &s);
        if (!s.video) {
            fprintf(stderr, "beanjar-vnc: cannot listen on %s: %s\n", video_path, strerror(errno));
            return 1;
        }
    }
#else
    (void)gbm;
#endif
    wv_view(s.v, s.width, s.height);
    wv_callbacks(s.v, NULL, redraw, quit, &s);
    wv_screenshot_hook(s.v, shot_wanted, &s);
    wv_start(s.v);
    /* the frame rate: --fps, else the VM's panel's refresh (vm-info.toml's panel_hz, which
     * wv_start read), whatever it is; above 60 only while it is sustained (frame_cost) */
    if (!fps) {
        long panel = wv_counter(s.v, "panel_hz");
        fps = panel < 1 ? RATE_FLOOR : panel > OUTPUT_HZ_LIMIT ? OUTPUT_HZ_LIMIT : (int)panel;
    }
    s.fps = fps;
    pace_set(&s.pace, fps);
    rate_init(&s.rate, fps);
#ifdef HAVE_VIDEO
    video_server_set_fps(s.video, fps);
#endif

    s.aml = aml_new();
    if (!s.aml)
        die("cannot create the aml loop");
    aml_set_default(s.aml);
    /* the listeners: TCP, then the WebSocket one, the relay's unix socket, the local one */
    enum { TCP, WEBSOCKET, UNIX, LOCAL } kinds[4];
    int n = 0;
    kinds[n++] = TCP;
    if (websocket)
        kinds[n++] = WEBSOCKET;
    if (unix_path)
        kinds[n++] = UNIX;
    if (local_path)
        kinds[n++] = LOCAL;
    for (int i = 0; i < n; i++) {
        struct nvnc *server;
        if (kinds[i] == UNIX || kinds[i] == LOCAL) {
            const char *path = kinds[i] == UNIX ? unix_path : local_path;
            unlink(path); /* a previous run's */
            /* the local socket is owner-only from its bind on: no moment another user can connect
             */
            mode_t mask = kinds[i] == LOCAL ? umask(0177) : 0;
            server = listen_unix(path);
            int e = errno;
            if (kinds[i] == LOCAL)
                umask(mask);
            if (!server) {
                fprintf(stderr, "beanjar-vnc: cannot listen on %s: %s\n", path, strerror(e));
                return 1;
            }
        } else {
            server = kinds[i] == WEBSOCKET ? listen_on(listen, (uint16_t)websocket, true)
                                           : listen_on(listen, (uint16_t)port, false);
        }
        s.ws[i] = kinds[i] == WEBSOCKET;
        if (!server) {
            int e = errno;
            bool w = kinds[i] == WEBSOCKET;
            fprintf(stderr, "beanjar-vnc: cannot listen on %s port %d%s: %s%s\n", listen,
                    w ? websocket : port, w ? " (websocket)" : "", strerror(e),
                    e == EADDRINUSE ? " (another server holds the port, perhaps on all addresses; "
                                      "choose another with "
                                      "--port or --websocket)"
                                    : "");
            return 1;
        }
        /* the local socket never authenticates: its file mode (0600) is the access control */
        if (password && kinds[i] != LOCAL &&
            nvnc_enable_auth(server, NVNC_AUTH_REQUIRE_AUTH | NVNC_AUTH_REQUIRE_ENCRYPTION, on_auth,
                             &s) < 0)
            die("cannot enable authentication");
        nvnc_set_userdata(server, &s, NULL);
        nvnc_set_name(server, name);
        nvnc_set_key_fn(server, on_keysym);
        nvnc_set_key_code_fn(server, on_keycode);
        nvnc_set_pointer_fn(server, on_pointer);
        nvnc_set_new_client_fn(server, on_client);
        /* beanjar-web's pause markers come on the local socket alone; the other listeners' cut
         * text is not taken (no clipboard), so a marker there does nothing */
        if (kinds[i] == LOCAL) {
            nvnc_set_cut_text_fn(server, on_cut_text);
            s.local = i;
        }
        s.display[i] = nvnc_display_new(0, 0);
        if (!s.display[i])
            die("cannot create the display");
        nvnc_add_display(server, s.display[i]);
        s.nvnc[i] = server;
        s.servers = i + 1;
    }
#ifdef HAVE_GBM
    if (!s.gbm)
#endif
    {
        s.pool = nvnc_fb_pool_new((uint16_t)s.width, (uint16_t)s.height, FOURCC_XBGR8888,
                                  (uint16_t)s.width);
        if (!s.pool)
            die("cannot create the frame pool");
    }
    s.tw = (s.width + TILE - 1) / TILE;
    s.th = (s.height + TILE - 1) / TILE;
    s.tiles = g_malloc0((size_t)s.tw * s.th);
    /* the first frame before "ready": neatvnc (0.9 src/server.c on_connection) closes a
     * connection at once while its display has no buffer, and the loop would accept one before
     * an idle render */
    s.dirty = 1;
    render(&s);
    update_idle(&s); /* no client yet */
    /* what is bound, for beanjar vnc to report: "listening" lines, then "ready" */
    const char *open_ = strchr(listen, ':') ? "[" : "", *close_ = *open_ ? "]" : "";
    fprintf(stderr, "beanjar-vnc: listening vnc://%s%s%s:%d\n", open_, listen, close_, port);
    if (websocket)
        fprintf(stderr, "beanjar-vnc: listening ws://%s%s%s:%d\n", open_, listen, close_,
                websocket);
    if (unix_path)
        fprintf(stderr, "beanjar-vnc: listening unix:%s\n", unix_path);
    if (local_path)
        fprintf(stderr, "beanjar-vnc: listening unix:%s (local, no password)\n", local_path);
    if (audio_path)
        fprintf(stderr, "beanjar-vnc: listening unix:%s (audio, opus or pcm)\n", audio_path);
    if (video_path)
        fprintf(stderr, "beanjar-vnc: listening unix:%s (video, h264, encoder %s)\n", video_path,
                video_encoder);
    fprintf(stderr, "beanjar-vnc: ready, %dx%d, up to %d fps (the panel's %ld Hz%s), %s\n", s.width,
            s.height, fps, wv_counter(s.v, "panel_hz"), fps > RATE_FLOOR ? ", while sustained" : "",
            password ? "password required (RSA-AES)" : "no password");

    update_cursor(&s); /* native: none until LG shows one, rather than the clients' own */
    s.loop = g_main_loop_new(NULL, FALSE);
    guint aml_source = g_unix_fd_add(aml_get_fd(s.aml), G_IO_IN, aml_ready, &s);
    guint stats_source = g_unix_signal_add(SIGUSR1, print_stats, &s);
    wv_attach(s.v);
    g_main_loop_run(s.loop);

    g_source_remove(aml_source);
    g_source_remove(stats_source);
    if (s.render_source)
        g_source_remove(s.render_source);
    for (int i = 0; i < s.servers; i++) {
        nvnc_remove_display(s.nvnc[i], s.display[i]);
        nvnc_display_unref(s.display[i]);
        nvnc_close(s.nvnc[i]);
    }
    if (unix_path)
        unlink(unix_path);
    if (local_path)
        unlink(local_path);
    drop_held(&s);
    g_free(s.tiles);
    if (s.pool)
        nvnc_fb_pool_unref(s.pool);
    aml_unref(s.aml);
    char *extra = g_strdup_printf(
        " vnc_frames=%u vnc_unchanged=%u vnc_feeds=%u keys=%u unmapped_keys=%u pointer_events=%u "
        "vnc_cursors=%u",
        s.frames, s.unchanged, s.feeds, s.keys, s.unmapped, s.pointer_events, s.cursors);
    if (s.last_fb)
        nvnc_fb_unref(s.last_fb);
    tilediff_free(s.diff);
    if (s.shot_source)
        g_source_remove(s.shot_source);
    thumb_free(s.thumb);
    if (s.shot_fbo) {
        glDeleteFramebuffers(1, &s.shot_fbo);
        glDeleteTextures(1, &s.shot_tex);
    }
    g_free(s.ppm);
    if (s.mfbo[0]) {
        glDeleteFramebuffers(2, s.mfbo);
        glDeleteTextures(2, s.mtex);
    }
#ifdef HAVE_VIDEO
    video_server_free(s.video);
    yuv_free(s.yuv);
    if (s.nv12)
        fprintf(stderr, "beanjar-vnc: video: %u frames converted to NV12 on the GPU\n", s.nv12);
#endif
#ifdef HAVE_GBM
    if (s.pending) /* its fence's watch is not dispatched any more */
        vnc_frame_unref(&s.pending->v);
    if (s.last_frame)
        vnc_frame_unref(&s.last_frame->v);
    if (s.gbm)
        fprintf(stderr, "beanjar-vnc: frames: GBM, %u renders put off with every buffer in use\n",
                s.starved);
    frames_free(s.gbm);
#endif
    int failed = wv_finish(s.v, extra);
    audio_server_free(audio);
    g_free(extra);
    g_main_loop_unref(s.loop);
    free(password);
    g_free(s.cursor_pixels);
    return failed;
}
