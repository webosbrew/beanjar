// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#define _POSIX_C_SOURCE 200809L
/* The viewer's core (core.h, docs/dev/architecture/viewer.md): the QEMU D-Bus display listener, the
 * scene (video windows, the OSD cache, LG's cursor) and the sources around it. */
#include "core.h"
#include "input.h"
#include "ir.h"
#include "cursor.h"
#include "audio.h"
#include "pcm-out.h"
#include "video.h"
#include "media.h"
#include "peer.h"
#include "capture.h"
#include "pace.h"
#include "disable.h"
#include <errno.h>
#include "../../../include/webos-ir.h"
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <SDL.h>
#include <gio/gunixfdlist.h>
#include <glib-unix.h>
#include <linux/input-event-codes.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>

#define CONSOLE  "/org/qemu/Display1/Console_0"
#define LISTENER "/org/qemu/Display1/Listener"
#define UNIX_MAP "org.qemu.Display1.Listener.Unix.Map"
static const char listener_xml[] =
    "<node><interface name='org.qemu.Display1.Listener'>"
    "<method name='Scanout'><arg type='u' direction='in'/><arg type='u' direction='in'/>"
    "<arg type='u' direction='in'/><arg type='u' direction='in'/><arg type='ay' "
    "direction='in'/></method>"
    "<method name='Update'><arg type='i' direction='in'/><arg type='i' direction='in'/>"
    "<arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='u' direction='in'/>"
    "<arg type='u' direction='in'/><arg type='ay' direction='in'/></method>"
    "<method name='ScanoutDMABUF'><arg type='h' direction='in'/><arg type='u' direction='in'/>"
    "<arg type='u' direction='in'/><arg type='u' direction='in'/><arg type='u' direction='in'/>"
    "<arg type='t' direction='in'/><arg type='b' direction='in'/></method>"
    "<method name='UpdateDMABUF'><arg type='i' direction='in'/><arg type='i' direction='in'/>"
    "<arg type='i' direction='in'/><arg type='i' direction='in'/></method>"
    "<method name='Disable'/><method name='MouseSet'><arg type='i' direction='in'/>"
    "<arg type='i' direction='in'/><arg type='i' direction='in'/></method>"
    "<method name='CursorDefine'><arg type='i' direction='in'/><arg type='i' direction='in'/>"
    "<arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='ay' "
    "direction='in'/></method>"
    "<property name='Interfaces' type='as' access='read'/></interface>"
    /* Surfaces shared as a memfd. QEMU uses it for GL scanouts it reads back because it cannot
     * export them as dmabufs (a surfaceless EGL display, e.g. Mesa d3d12 on WSL2), and for 2D. */
    "<interface name='" UNIX_MAP "'>"
    "<method name='ScanoutMap'><arg type='h' direction='in'/><arg type='u' direction='in'/>"
    "<arg type='u' direction='in'/><arg type='u' direction='in'/><arg type='u' direction='in'/>"
    "<arg type='u' direction='in'/></method>"
    "<method name='UpdateMap'><arg type='i' direction='in'/><arg type='i' direction='in'/>"
    "<arg type='i' direction='in'/><arg type='i' direction='in'/></method></interface></node>";

struct wv {
    /* options */
    char *addr, *cursor_path, *video_path, *ir_path, *media_prefix, *hwdec, *audio_wav, *media_wav;
    int no_audio, sdl_audio, no_media, media_latency, headless, media_silent, media_mute;
    double media_volume;
    /* callbacks */
    wv_callback make_current, redraw, quit_cb;
    void *data;
    guint source;
    int quit, quit_sent;
    /* display */
    EGLDisplay display;
    EGLContext context;
    EGLImageKHR image;
    PFNEGLCREATEIMAGEKHRPROC create_image;
    PFNEGLDESTROYIMAGEKHRPROC destroy_image;
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC image_target;
    /* EGL_ANDROID_native_fence_sync: a dmabuf update's copy is fenced, and QEMU's call answered
     * when the fence signals (capture_fenced), not after a glFinish */
    PFNEGLCREATESYNCKHRPROC create_sync;
    PFNEGLDESTROYSYNCKHRPROC destroy_sync;
    PFNEGLDUPNATIVEFENCEFDANDROIDPROC dup_fence;
    GSList *replies; /* struct reply: calls answered when their copy's fence signals */
    unsigned fenced;
    /* QEMU's UpdateDMABUF calls, and how long it waited for their answers in all (us) and at
     * most: the guest's GL is held up meanwhile (ScanoutDMABUF it does not wait for) */
    unsigned dmabuf_updates;
    gint64 reply_us, reply_max_us;
    GLuint source_tex, cache, fbo, program;
    GLuint frame_tex[2], frame_fbo[2]; /* wv_frame's targets: the scene, then it turned upright */
    int frame_w, frame_h;
    GLint flip, target; /* target: the framebuffer the scene is drawn into */
    /* the cache texture's size: the output's picture when that is smaller than the scanout
     * (cache_want), else the scanout's. out_*: the size the front end last drew at (wv_render,
     * wv_frame*). recopy: the cache was reallocated and is copied again at the next draw. */
    int cache_w, cache_h, out_w, out_h, recopy;
    unsigned recopies;
    /* the scene changed (what dirty marks, and updates skipped while idle): wv_counter changes */
    unsigned long changes;
    int width, height, y0_top, valid, dma, dirty, straight, osd_dump, bgra, no_osd;
    /* the scanout's uploads through a pixel unpack buffer (tex_upload), on Mesa's d3d12 */
    GLuint pbo;
    size_t pbo_size;
    void *(*map_range)(GLenum, GLintptr, GLsizeiptr, GLbitfield);
    GLboolean (*unmap_buffer)(GLenum);
    /* no-cursor: the front end shows LG's cursor itself (wv_cursor); cursor_key is what
     * cursor_serial last counted */
    int no_cursor;
    int standby; /* wv_standby: the TV is in standby, LG's cursor is not shown */
    /* the output rate (docs/dev/architecture/viewer.md, "Output rate"): panel_hz the board's panel
     * (vm-info.toml's panel_hz, else 60), output_hz the pictures a second the front end makes
     * (option output-hz, else the panel's; 0: unpaced), pace their times */
    int panel_hz, output_hz;
    struct pace pace;
    unsigned long pictures; /* pictures of the output drawn (wv_render, wv_frame*) */
    /* wv_planes: the WV_PLANE_* this front end hides; whole: drawing for the guest's capture or a
     * bus Screenshot, which show every plane */
    int hidden, whole;
    unsigned cursor_serial, cursor_key[10];
    unsigned frames, imports, maps, cursor_draws;
    /* wv_idle: updates acknowledged without a copy (stale: the cache is behind QEMU's surface) */
    int idle, stale;
    /* a dmabuf scanout not yet copied, waiting for the UpdateDMABUF QEMU sends after it (the
     * guest's flush): since when, and the timer that draws it anyway if none comes */
    int awaiting;
    gint64 scanout_at;
    guint scanout_timer;
    unsigned skipped;
    /* a Disable not applied yet (disable.h): a scanout before its deadline cancels it, wv_poll
     * applies it after; disables counts those that came, disables_dropped those a scanout
     * cancelled */
    struct disable disable;
    unsigned disables, disables_dropped;
    /* 2D Scanout/Update (pixels in the message): QEMU's surface as the messages leave it, rows
     * packed (width * 4), in shadow_format; kept so that idle updates need only a copy here */
    unsigned char *shadow;
    unsigned shadow_format;
    /* ScanoutMap: QEMU's shared surface, read on each UpdateMap */
    const unsigned char *map;
    size_t map_len;
    unsigned map_offset, map_stride, map_format;
    int view_w, view_h; /* logical size of the drawing area, before the turn (wv_turn) */
    /* wv_turn: the picture shown turned clockwise by turn quarters (a set turned to portrait),
     * drawn first unturned into turn_tex (turn_w x turn_h, the area's size with width and height
     * swapped for an odd turn) and then turned into the front end's framebuffer; real_w x real_h
     * the area's logical size as the front end gives it (wv_view), whose coordinates the input
     * calls take */
    int turn, real_w, real_h, turn_w, turn_h;
    GLuint turn_tex, turn_fbo;
    GLint turn_matrix;
    struct input input;
    struct ir ir;
    struct cursor cursor;
    int pointer_present;
    struct audio_out audio;
    wv_audio_fn sink;
    void *sink_data;
    volatile int sink_wanted;
    struct pcm_out *pcm;
    struct video video;
    struct capture capture;
    char *capture_path;
    GDBusConnection *peer;
    /* a guest restart (host/core/beanjar/lifecycle.py) ends QEMU and starts a new one on the
     * same bus: restarting is the run directory's marker of one, qemu_owner the unique name of
     * the QEMU shown; lost_at when its display connection closed (0: it did not), and until when
     * the next QEMU is waited for (0: not waiting) */
    char *restarting, *qemu_owner;
    gint64 lost_at, reconnect_until, reconnect_try;
    guint name_id; /* org.beanjar.Display1 (screenshot_start) */
    /* Display1.Ir: the button a bus client holds down and that client's name, watched so the
     * button is released when it goes away (a remote pipe killed with a key held) */
    int ir_bus_held;
    char *ir_owner;
    guint ir_watch;
    /* Display1.Pointer: the buttons a bus client holds (a mask, bit 0 left) and that client,
     * watched as the IR one is so they are released when it leaves the bus */
    unsigned ptr_bus_held;
    char *ptr_owner;
    guint ptr_watch;
    /* Display1.Touch: the client whose touches are down, watched the same way */
    char *touch_owner;
    guint touch_watch;
    /* wv_screenshot_hook: Screenshot calls queued for the front end, the oldest first */
    wv_callback shot_fn;
    void *shot_data;
    GQueue shots;
};
struct shot {
    char *path;
    int width, height;
    GDBusMethodInvocation *call;
};

static volatile sig_atomic_t signalled;
/* SIGINT/SIGTERM: the first ends the loop and shuts down cleanly (held keys released, the
 * connections closed). Some waits never see the flag: start-up's D-Bus calls, SDL opening the
 * audio device (ALSA's pulse plugin waiting on an unresponsive server), joining SDL's audio thread
 * and closing D-Bus connections at exit. So a second signal, or SHUTDOWN_GRACE seconds without
 * the clean exit, ends the process at once. */
#define SHUTDOWN_GRACE 5
static void forced(int sig)
{
    static const char message[] = "viewer: no clean exit, stopping now\n";
    if (write(2, message, sizeof message - 1) < 0) {
    }
    _exit(128 + sig);
}
static void stop(int sig)
{
    if (signalled)
        forced(sig);
    signalled = 1;
    alarm(SHUTDOWN_GRACE);
}
void wv_signals(void)
{
    struct sigaction sa = {0};
    sigemptyset(&sa.sa_mask);
    sa.sa_handler = stop;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    sa.sa_handler = forced;
    sigaction(SIGALRM, &sa, NULL);
}
static void die(const char *message)
{
    fprintf(stderr, "viewer: %s\n", message);
    exit(1);
}
static void check_error(GError *error)
{
    if (error)
        die(error->message);
}
int wv_abi(void)
{
    return WV_ABI;
}
struct wv *wv_new(void)
{
    struct wv *v = g_new0(struct wv, 1);
    v->image = EGL_NO_IMAGE_KHR;
    v->audio.fd = -1; /* audio_close() closes it, and no-audio never calls audio_connect() */
    v->ir.fd = v->ir.held = v->ir.local_held = -1; /* ir_close() before wv_start() */
    v->ir_bus_held = -1;
    v->media_latency = -1;
    v->media_volume = 1.0;
    v->capture.fd = -1;
    v->display = EGL_NO_DISPLAY;
    v->context = EGL_NO_CONTEXT;
    v->panel_hz = PANEL_HZ_DEFAULT;
    v->output_hz = -1; /* the panel's */
    return v;
}
/* "hide-planes": "none", or a comma-separated list of "video", "ui" and "cursor"; -1 if bad. */
static int planes_parse(const char *value)
{
    static const char *const names[] = {"video", "ui", "cursor"};
    if (!strcmp(value, "none") || !*value)
        return 0;
    int hidden = 0;
    gchar **parts = g_strsplit(value, ",", -1);
    for (gchar **p = parts; *p; p++) {
        size_t i = 0;
        while (i < G_N_ELEMENTS(names) && strcmp(g_strstrip(*p), names[i]))
            i++;
        if (i == G_N_ELEMENTS(names)) {
            hidden = -1;
            break;
        }
        hidden |= 1 << i;
    }
    g_strfreev(parts);
    return hidden;
}
int wv_option(struct wv *v, const char *name, const char *value)
{
    int flag = !value || strcmp(value, "0");
    struct {
        const char *name;
        char **field;
    } strings[] = {
        {"address", &v->addr},
        {"cursor-socket", &v->cursor_path},
        {"video-socket", &v->video_path},
        {"ir-socket", &v->ir_path},
        {"capture-socket", &v->capture_path},
        {"media-socket", &v->media_prefix},
        {"hwdec", &v->hwdec},
        {"audio-wav", &v->audio_wav},
        {"media-wav", &v->media_wav},
    };
    struct {
        const char *name;
        int *field;
    } flags[] = {
        {"straight-alpha", &v->straight}, {"no-media", &v->no_media},
        {"no-audio", &v->no_audio},       {"sdl-audio", &v->sdl_audio},
        {"headless", &v->headless},       {"osd-alpha", &v->osd_dump},
        {"no-osd", &v->no_osd},           {"media-silent", &v->media_silent},
        {"media-mute", &v->media_mute},   {"no-cursor", &v->no_cursor},
    };
    for (size_t i = 0; i < G_N_ELEMENTS(strings); i++)
        if (!strcmp(name, strings[i].name)) {
            if (!value)
                return -1;
            g_free(*strings[i].field);
            *strings[i].field = g_strdup(value);
            return 0;
        }
    for (size_t i = 0; i < G_N_ELEMENTS(flags); i++)
        if (!strcmp(name, flags[i].name)) {
            *flags[i].field = flag;
            return 0;
        }
    if (!strcmp(name, "hide-planes") && value) {
        int hidden = planes_parse(value);
        if (hidden < 0)
            return -1;
        v->hidden = hidden;
        return 0;
    }
    if (!strcmp(name, "output-hz") && value) {
        char tail;
        int hz;
        if (sscanf(value, "%d%c", &hz, &tail) != 1 || hz < 0 || hz > OUTPUT_HZ_LIMIT)
            return -1;
        v->output_hz = hz;
        return 0;
    }
    if (!strcmp(name, "media-latency") && value) {
        v->media_latency = atoi(value);
        return 0;
    }
    if (!strcmp(name, "media-volume") && value) {
        char tail;
        double volume;
        if (sscanf(value, "%lg%c", &volume, &tail) != 1 || !(volume >= 0) || volume > 2)
            return -1;
        v->media_volume = volume ? volume : 1.0;
        return 0;
    }
    if (!strcmp(name, "size") && value) {
        int w, h;
        char tail;
        if (sscanf(value, "%dx%d%c", &w, &h, &tail) != 2 || w < 64 || h < 64 || w > 8192 ||
            h > 8192)
            return -1;
        v->view_w = v->real_w = w;
        v->view_h = v->real_h = h;
        return 0;
    }
    return -1;
}
void wv_callbacks(struct wv *v, wv_callback make_current, wv_callback redraw, wv_callback quit,
                  void *data)
{
    v->make_current = make_current;
    v->redraw = redraw;
    v->quit_cb = quit;
    v->data = data;
}
/* GL calls outside the front end's render (D-Bus callbacks, the cursor upload) need the context
 * current; asking GTK through the callback keeps GDK's idea of the current context right. */
static void current(struct wv *v)
{
    if (!v->make_current)
        return;
    if (v->context != EGL_NO_CONTEXT && eglGetCurrentContext() == v->context)
        return;
    v->make_current(v->data);
}
static GLuint shader(GLenum type, const char *source)
{
    GLuint obj = glCreateShader(type);
    glShaderSource(obj, 1, &source, NULL);
    glCompileShader(obj);
    GLint ok;
    glGetShaderiv(obj, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[1024];
        glGetShaderInfoLog(obj, sizeof(log), NULL, log);
        die(log);
    }
    return obj;
}
static void texture(GLuint tex)
{
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
}
static void graphics(struct wv *v)
{
    v->display = eglGetCurrentDisplay();
    v->context = eglGetCurrentContext();
    v->create_image = (PFNEGLCREATEIMAGEKHRPROC)eglGetProcAddress("eglCreateImageKHR");
    v->destroy_image = (PFNEGLDESTROYIMAGEKHRPROC)eglGetProcAddress("eglDestroyImageKHR");
    v->image_target =
        (PFNGLEGLIMAGETARGETTEXTURE2DOESPROC)eglGetProcAddress("glEGLImageTargetTexture2DOES");
    if (v->display == EGL_NO_DISPLAY)
        fprintf(stderr, "viewer: the GL context is not EGL; dmabuf scanouts cannot be imported\n");
    const char *egl_ext =
        v->display != EGL_NO_DISPLAY ? eglQueryString(v->display, EGL_EXTENSIONS) : NULL;
    if (egl_ext && strstr(egl_ext, "EGL_ANDROID_native_fence_sync")) {
        v->create_sync = (PFNEGLCREATESYNCKHRPROC)eglGetProcAddress("eglCreateSyncKHR");
        v->destroy_sync = (PFNEGLDESTROYSYNCKHRPROC)eglGetProcAddress("eglDestroySyncKHR");
        v->dup_fence =
            (PFNEGLDUPNATIVEFENCEFDANDROIDPROC)eglGetProcAddress("eglDupNativeFenceFDANDROID");
        if (!v->create_sync || !v->destroy_sync)
            v->dup_fence = NULL;
    }
    GLuint vs = shader(GL_VERTEX_SHADER,
                       "attribute vec2 pos; varying vec2 uv; uniform float flip; uniform mat2 turn;"
                       "void main(){gl_Position=vec4(pos,0.,1.); uv=(pos+1.)*.5;"
                       "uv.y=mix(uv.y,1.-uv.y,flip); uv=turn*(uv-.5)+.5;}");
    GLuint fs = shader(GL_FRAGMENT_SHADER,
                       "precision mediump float; varying vec2 uv; uniform sampler2D frame;"
                       "void main(){gl_FragColor=texture2D(frame,uv);}");
    v->program = glCreateProgram();
    glAttachShader(v->program, vs);
    glAttachShader(v->program, fs);
    glBindAttribLocation(v->program, 0, "pos");
    glLinkProgram(v->program);
    GLint ok;
    glGetProgramiv(v->program, GL_LINK_STATUS, &ok);
    if (!ok)
        die("shader link failed");
    glDeleteShader(vs);
    glDeleteShader(fs);
    glUseProgram(v->program);
    v->flip = glGetUniformLocation(v->program, "flip");
    v->turn_matrix = glGetUniformLocation(v->program, "turn");
    glUniform1i(glGetUniformLocation(v->program, "frame"), 0);
    glGenTextures(1, &v->source_tex);
    texture(v->source_tex);
    glGenTextures(1, &v->cache);
    texture(v->cache);
    glGenFramebuffers(1, &v->fbo);
    const char *ext = (const char *)glGetString(GL_EXTENSIONS);
    v->bgra = ext && strstr(ext, "GL_EXT_texture_format_BGRA8888");
    /* Mesa's d3d12 (WSL) on NVIDIA takes about half a second for a 1920x1080 glTexSubImage2D
     * from client memory, and 3 ms from a mapped pixel unpack buffer (GLES 3; on Intel 3 and 5
     * ms): docs/dev/architecture/vnc.md, "GL" */
    const char *renderer = (const char *)glGetString(GL_RENDERER);
    if (renderer && !strncmp(renderer, "D3D12", 5)) {
        v->map_range = (void *(*)(GLenum, GLintptr, GLsizeiptr, GLbitfield))eglGetProcAddress(
            "glMapBufferRange");
        v->unmap_buffer = (GLboolean(*)(GLenum))eglGetProcAddress("glUnmapBuffer");
        if (v->map_range && v->unmap_buffer)
            glGenBuffers(1, &v->pbo);
    }
    fprintf(stderr, "GL %s; %s; %s\n", glGetString(GL_VERSION), glGetString(GL_RENDERER),
            v->display != EGL_NO_DISPLAY ? "EGL" : "not EGL");
}
/* The texture coordinates of a picture turned clockwise by turn quarters, as the column-major
 * matrix that takes a point of the output (centred, GL's y up) to the picture's. */
static const GLfloat TURNS[4][4] = {{1, 0, 0, 1}, {0, 1, -1, 0}, {-1, 0, 0, -1}, {0, -1, 1, 0}};
static void quad_turned(struct wv *v, GLuint tex, int flip, int turn)
{
    static const GLfloat xy[] = {-1, -1, 1, -1, -1, 1, 1, 1};
    glUseProgram(v->program);
    glUniform1f(v->flip, flip);
    glUniformMatrix2fv(v->turn_matrix, 1, GL_FALSE, TURNS[turn & 3]);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, tex);
    glBindBuffer(GL_ARRAY_BUFFER, 0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, xy);
    glEnableVertexAttribArray(0);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
}
static void quad(struct wv *v, GLuint tex, int flip)
{
    quad_turned(v, tex, flip, 0);
}
static struct rect fit(int w, int h, int sw, int sh)
{
    struct rect r = {0, 0, w, h};
    if (sw > 0 && sh > 0) {
        if ((int64_t)w * sh > (int64_t)h * sw)
            r.w = (int)((int64_t)h * sw / sh);
        else
            r.h = (int)((int64_t)w * sh / sw);
        r.x = (w - r.w) / 2;
        r.y = (h - r.h) / 2;
    }
    return r;
}
/* The scene changed: drawn again, and counted (changes). */
static void touch(struct wv *v)
{
    v->dirty = 1;
    v->changes++;
}
/* The scene into v->target at dw x dh device pixels. */
static void refresh(struct wv *v);
static void capture(struct wv *v);
/* The cache's size for an output of dw x dh: the picture's size (fit) when that is smaller than the
 * scanout, which makes the copy of the guest's frame the downscale too; the scanout's otherwise. */
static void cache_want(struct wv *v, int dw, int dh, int *cw, int *ch)
{
    *cw = v->width;
    *ch = v->height;
    if (dw > 0 && dh > 0) {
        struct rect r = fit(dw, dh, v->width, v->height);
        if (r.w < v->width && r.h < v->height) {
            *cw = r.w < 1 ? 1 : r.w;
            *ch = r.h < 1 ? 1 : r.h;
        }
    }
}
/* The cache texture at cw x ch, its contents undefined: copied again (recopy) before it is drawn.
 */
static void cache_alloc(struct wv *v, int cw, int ch)
{
    texture(v->cache);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, cw, ch, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    GLint previous;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &previous);
    glBindFramebuffer(GL_FRAMEBUFFER, v->fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, v->cache, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
        die("cache framebuffer incomplete");
    glBindFramebuffer(GL_FRAMEBUFFER, previous);
    v->cache_w = cw;
    v->cache_h = ch;
    v->recopy = 1;
}
/* After a screenshot or a capture grew the cache past the output's size: back to that size. */
static void cache_restore(struct wv *v)
{
    int cw, ch;
    cache_want(v, v->out_w, v->out_h, &cw, &ch);
    if (cw != v->cache_w || ch != v->cache_h)
        cache_alloc(v, cw, ch);
}
/* The planes left out of what is drawn now: none for the guest's capture or a bus Screenshot. */
static int hidden_now(const struct wv *v)
{
    return v->whole ? 0 : v->hidden;
}
/* primary: the front end's own output (wv_render, wv_frame*), whose size the cache follows; the
 * others (a screenshot, the guest's capture) only make the cache large enough for theirs. */
static void draw(struct wv *v, int dw, int dh, int primary)
{
    if (primary) {
        v->out_w = dw;
        v->out_h = dh;
    }
    int cw, ch;
    cache_want(v, dw, dh, &cw, &ch);
    if (primary ? (cw != v->cache_w || ch != v->cache_h) : (cw > v->cache_w || ch > v->cache_h))
        cache_alloc(v, primary ? cw : (cw > v->cache_w ? cw : v->cache_w),
                    primary ? ch : (ch > v->cache_h ? ch : v->cache_h));
    refresh(v); /* idle's skipped updates (a screenshot while nobody looks) */
    if (v->recopy && v->valid) {
        v->recopies++;
        capture(v);
    }
    v->input.viewport = fit(v->view_w, v->view_h, v->width, v->height);
    struct rect r = fit(dw, dh, v->width, v->height);
    glBindFramebuffer(GL_FRAMEBUFFER, v->target);
    glViewport(0, 0, dw, dh);
    glDisable(GL_SCISSOR_TEST);
    glDisable(GL_DEPTH_TEST);
    glClearColor(0, 0, 0, 1);
    glClear(GL_COLOR_BUFFER_BIT);
    if (v->valid) {
        int hidden = hidden_now(v);
        /* Video under the OSD: the picture (or placeholder) of each shown media window. */
        if (!(hidden & WV_PLANE_VIDEO))
            video_draw(&v->video, r);
        glViewport(r.x, r.y, r.w, r.h);
        glEnable(GL_BLEND);
        glBlendFunc(v->straight ? GL_SRC_ALPHA : GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
        if (!v->no_osd && !(hidden & WV_PLANE_UI))
            quad(v, v->cache, 0); /* no-osd: the video plane alone */
        /* LG's cursor (webos.cursor) on top, scaled like the picture. While the host pointer is
         * over the image it is drawn where that pointer is, the absolute position the tablet
         * sends, so it moves without a guest round trip; otherwise at libim's position. */
        struct cursor *cursor = &v->cursor;
        struct webos_cursor_header *c = &cursor->h;
        if (!v->no_cursor && !v->standby && !(hidden & WV_PLANE_CURSOR) && c->visible && c->width &&
            c->screen_w && c->screen_h) {
            double sx = (double)r.w / c->screen_w, sy = (double)r.h / c->screen_h;
            unsigned cw = c->draw_w ? c->draw_w : c->width, ch = c->draw_h ? c->draw_h : c->height;
            double x = c->x, y = c->y;
            struct rect logical = v->input.viewport;
            if (v->cursor.inside && logical.w > 0 && logical.h > 0) {
                x = (double)(v->cursor.local_x - logical.x) * c->screen_w / logical.w;
                y = (double)(v->cursor.local_y - logical.y) * c->screen_h / logical.h;
            }
            int left = r.x + (int)floor((x - (double)c->hot_x * cw / c->width) * sx);
            int top = (int)floor((y - (double)c->hot_y * ch / c->height) * sy);
            int pw = (int)(cw * sx + 0.5), ph = (int)(ch * sy + 0.5);
            if (pw < 1)
                pw = 1;
            if (ph < 1)
                ph = 1;
            glEnable(GL_SCISSOR_TEST);
            glScissor(r.x, r.y, r.w, r.h);
            glViewport(left, r.y + r.h - top - ph, pw, ph);
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
            quad(v, cursor->texture, 1);
            glDisable(GL_SCISSOR_TEST);
            v->cursor_draws++;
        }
        glDisable(GL_BLEND);
    }
    v->dirty = 0;
    if (primary) { /* a picture of the output: the next one is paced from it */
        pace_mark(&v->pace, g_get_monotonic_time());
        v->pictures++;
    }
}
static void turn_drop(struct wv *v)
{
    if (!v->turn_w)
        return;
    glDeleteFramebuffers(1, &v->turn_fbo);
    glDeleteTextures(1, &v->turn_tex);
    v->turn_w = v->turn_h = 0;
}
/* The unturned scene's target at w x h, kept between frames. */
static int turn_target(struct wv *v, int w, int h)
{
    if (v->turn_w == w && v->turn_h == h)
        return 0;
    turn_drop(v);
    glGenTextures(1, &v->turn_tex);
    glGenFramebuffers(1, &v->turn_fbo);
    v->turn_w = w;
    v->turn_h = h;
    texture(v->turn_tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    GLint previous;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &previous);
    glBindFramebuffer(GL_FRAMEBUFFER, v->turn_fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, v->turn_tex, 0);
    int bad = glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE;
    glBindFramebuffer(GL_FRAMEBUFFER, previous);
    if (bad)
        turn_drop(v);
    return bad ? -1 : 0;
}
void wv_render(struct wv *v, int width, int height)
{
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &v->target);
    GLint output = v->target;
    int w = v->turn & 1 ? height : width, h = v->turn & 1 ? width : height;
    if (!v->turn || turn_target(v, w, h)) {
        draw(v, width, height, 1);
        return;
    }
    /* unturned into turn_tex, then turned into the output */
    v->target = v->turn_fbo;
    draw(v, w, h, 1);
    v->target = output;
    glBindFramebuffer(GL_FRAMEBUFFER, output);
    glViewport(0, 0, width, height);
    glDisable(GL_BLEND);
    glDisable(GL_SCISSOR_TEST);
    quad_turned(v, v->turn_tex, 0, v->turn);
}
/* A point of the area (logical, as the front end gives it) in the unturned picture's area. */
static void unturn(struct wv *v, int *x, int *y)
{
    int px = *x, py = *y, w = v->real_w, h = v->real_h;
    switch (v->turn) {
    case 1: /* shown turned a quarter clockwise */
        *x = py;
        *y = w - px;
        break;
    case 2:
        *x = w - px;
        *y = h - py;
        break;
    case 3:
        *x = h - py;
        *y = px;
        break;
    }
}
static void pointer_geometry(struct wv *v)
{
    struct rect r = v->input.viewport;
    int x = v->cursor.local_x, y = v->cursor.local_y;
    v->cursor.inside = v->pointer_present && r.w > 0 && r.h > 0 && x >= r.x && x < r.x + r.w &&
                       y >= r.y && y < r.y + r.h;
}
void wv_view(struct wv *v, int width, int height)
{
    v->real_w = width;
    v->real_h = height;
    v->view_w = v->turn & 1 ? height : width;
    v->view_h = v->turn & 1 ? width : height;
    v->input.viewport = fit(v->view_w, v->view_h, v->width, v->height);
    pointer_geometry(v);
}
int wv_picture(struct wv *v, int *x, int *y, int *w, int *h)
{
    struct rect r = fit(v->view_w, v->view_h, v->width, v->height);
    /* as shown: turned (unturn's inverse) */
    struct rect t = r;
    if (v->turn & 1) {
        t.w = r.h;
        t.h = r.w;
    }
    if (v->turn == 1) {
        t.x = v->real_w - r.y - r.h;
        t.y = r.x;
    } else if (v->turn == 2) {
        t.x = v->real_w - r.x - r.w;
        t.y = v->real_h - r.y - r.h;
    } else if (v->turn == 3) {
        t.x = r.y;
        t.y = v->real_h - r.x - r.w;
    }
    *x = t.x;
    *y = t.y;
    *w = t.w;
    *h = t.h;
    return v->valid;
}
static void changed(struct wv *v);
void wv_turn(struct wv *v, int degrees)
{
    int turn = ((degrees % 360 + 360) % 360) / 90;
    if (turn == v->turn)
        return;
    v->turn = turn;
    if (v->real_w > 0 && v->real_h > 0)
        wv_view(v, v->real_w, v->real_h);
    touch(v);
    changed(v);
}
static void size(struct wv *v, unsigned w, unsigned h)
{
    if (w < 1 || h < 1 || w > 8192 || h > 8192)
        die("invalid scanout dimensions");
    if (v->width != (int)w || v->height != (int)h) {
        v->width = w;
        v->height = h;
        v->input.width = w;
        v->input.height = h;
        v->input.viewport = fit(v->view_w, v->view_h, v->width, v->height);
        pointer_geometry(v);
        int cw, ch;
        cache_want(v, v->out_w, v->out_h, &cw, &ch);
        cache_alloc(v, cw, ch);
    }
}
/* A fence for the GL commands so far, as a sync file (flushed); -1 where there is none. */
static int fence_fd(struct wv *v)
{
    if (!v->dup_fence)
        return -1;
    static const EGLint attrs[] = {EGL_SYNC_NATIVE_FENCE_FD_ANDROID, EGL_NO_NATIVE_FENCE_FD_ANDROID,
                                   EGL_NONE};
    EGLSyncKHR sync = v->create_sync(v->display, EGL_SYNC_NATIVE_FENCE_ANDROID, attrs);
    if (sync == EGL_NO_SYNC_KHR)
        return -1;
    glFlush();
    int fd = v->dup_fence(v->display, sync);
    v->destroy_sync(v->display, sync);
    return fd;
}
/* The source (QEMU's dmabuf, or the texture an upload filled) copied into the cache. fenced: the
 * copy's fence as a sync file, returned (-1: none, the copy finished already); otherwise, or
 * without fences, glFinish: the source is free again when this returns. Leaves the framebuffer
 * binding as it was: the front end draws into whatever is bound. */
static int capture_fenced(struct wv *v, int fenced)
{
    GLint previous;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &previous);
    glBindFramebuffer(GL_FRAMEBUFFER, v->fbo);
    glViewport(0, 0, v->cache_w, v->cache_h); /* the downscale to the output, if any */
    glDisable(GL_BLEND);
    glDisable(GL_SCISSOR_TEST);
    quad(v, v->source_tex, !v->y0_top);
    /* QEMU's dmabuf must be read before its update is answered: after a glFinish, or once the
     * fence signals. The local GPU copy also allows safe redraws on resize/expose after QEMU
     * reuses it; later draws from the cache follow the copy in this context. */
    int fd = fenced ? fence_fd(v) : -1;
    if (fd < 0)
        glFinish();
    if (glGetError() != GL_NO_ERROR)
        die("scanout rendering failed");
    v->valid = 1;
    touch(v);
    v->recopy = 0;
    v->frames++;
    glBindFramebuffer(GL_FRAMEBUFFER, previous);
    return fd;
}
static void capture(struct wv *v)
{
    capture_fenced(v, 0);
}
static void drop_image(struct wv *v)
{
    if (v->image != EGL_NO_IMAGE_KHR) {
        v->destroy_image(v->display, v->image);
        v->image = EGL_NO_IMAGE_KHR;
    }
    glDeleteTextures(1, &v->source_tex);
    glGenTextures(1, &v->source_tex);
    texture(v->source_tex);
}
#define SCANOUT_WAIT_US 50000
static void changed(struct wv *v);
static gboolean scanout_late(gpointer data)
{
    struct wv *v = data;
    v->scanout_timer = 0;
    if (v->awaiting) {
        touch(v);
        changed(v);
    }
    return G_SOURCE_REMOVE;
}
static int scanout_dma(struct wv *v, GVariant *params, GDBusMethodInvocation *call)
{
    gint handle;
    guint w, h, stride, format;
    guint64 mod;
    gboolean y0;
    g_variant_get(params, "(huuuutb)", &handle, &w, &h, &stride, &format, &mod, &y0);
    GUnixFDList *fds = g_dbus_message_get_unix_fd_list(g_dbus_method_invocation_get_message(call));
    GError *error = NULL;
    int fd = fds ? g_unix_fd_list_get(fds, handle, &error) : -1;
    check_error(error);
    if (fd < 0)
        die("ScanoutDMABUF missing fd");
    if (v->display == EGL_NO_DISPLAY || !v->create_image || !v->image_target)
        die("dmabuf scanout, but the GL context is not EGL with EGL images (GDK_DEBUG=gl-glx?)");
    size(v, w, h);
    drop_image(v);
    EGLint attrs[24] = {EGL_WIDTH,
                        w,
                        EGL_HEIGHT,
                        h,
                        EGL_LINUX_DRM_FOURCC_EXT,
                        format,
                        EGL_DMA_BUF_PLANE0_FD_EXT,
                        fd,
                        EGL_DMA_BUF_PLANE0_OFFSET_EXT,
                        0,
                        EGL_DMA_BUF_PLANE0_PITCH_EXT,
                        stride};
    int n = 12;
    if (mod != UINT64_C(0x00ffffffffffffff)) {
        attrs[n++] = EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT;
        attrs[n++] = (EGLint)mod;
        attrs[n++] = EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT;
        attrs[n++] = (EGLint)(mod >> 32);
    }
    attrs[n] = EGL_NONE;
    v->image = v->create_image(v->display, EGL_NO_CONTEXT, EGL_LINUX_DMA_BUF_EXT, NULL, attrs);
    close(fd);
    if (v->image == EGL_NO_IMAGE_KHR) {
        fprintf(stderr, "EGL import error 0x%x, format %.4s, modifier 0x%llx\n", eglGetError(),
                (char *)&format, (unsigned long long)mod);
        die("dmabuf import failed (viewer must use QEMU's GPU on the same host)");
    }
    texture(v->source_tex);
    v->image_target(GL_TEXTURE_2D, v->image);
    v->y0_top = y0;
    v->dma = 1;
    v->imports++;
    /* copied once, by the UpdateDMABUF QEMU sends after it (the guest's flush): not here as well,
     * which copied each frame twice. A draw meanwhile shows the last frame (refresh leaves it);
     * if no update comes within SCANOUT_WAIT_US, the scanout is drawn as it is. */
    v->stale = 1;
    v->awaiting = 1;
    v->scanout_at = g_get_monotonic_time();
    if (!v->scanout_timer)
        v->scanout_timer = g_timeout_add(SCANOUT_WAIT_US / 1000, scanout_late, v);
    return -1;
}
#ifndef GL_PIXEL_UNPACK_BUFFER
#define GL_PIXEL_UNPACK_BUFFER 0x88EC
#endif
#ifndef GL_MAP_WRITE_BIT
#define GL_MAP_WRITE_BIT 0x0002
#endif
#ifndef GL_MAP_INVALIDATE_BUFFER_BIT
#define GL_MAP_INVALIDATE_BUFFER_BIT 0x0008
#endif
#ifndef GL_STREAM_DRAW
#define GL_STREAM_DRAW 0x88E0
#endif
/* rows of w 4-byte pixels (packed) into the bound texture: glTexImage2D when full, otherwise
 * glTexSubImage2D at x, y; through v->pbo where there is one. capture's glFinish follows each
 * upload, so the buffer is free again for the next. */
static void tex_upload(struct wv *v, int full, int x, int y, int w, int h, GLenum format,
                       const void *data)
{
    size_t size = (size_t)w * h * 4;
    void *mapped = NULL;
    if (v->pbo) {
        glBindBuffer(GL_PIXEL_UNPACK_BUFFER, v->pbo);
        if (v->pbo_size < size) {
            glBufferData(GL_PIXEL_UNPACK_BUFFER, size, NULL, GL_STREAM_DRAW);
            v->pbo_size = size;
        }
        mapped = v->map_range(GL_PIXEL_UNPACK_BUFFER, 0, size,
                              GL_MAP_WRITE_BIT | GL_MAP_INVALIDATE_BUFFER_BIT);
        if (mapped) {
            memcpy(mapped, data, size);
            v->unmap_buffer(GL_PIXEL_UNPACK_BUFFER);
            data = NULL; /* offset 0 in the buffer */
        } else
            glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0);
    }
    if (full)
        glTexImage2D(GL_TEXTURE_2D, 0, format, w, h, 0, format, GL_UNSIGNED_BYTE, data);
    else
        glTexSubImage2D(GL_TEXTURE_2D, 0, x, y, w, h, format, GL_UNSIGNED_BYTE, data);
    if (mapped)
        glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0);
}
static void upload(struct wv *v, int x, int y, int w, int h, unsigned stride, unsigned fmt,
                   const unsigned char *bytes, size_t len, int full)
{
    unsigned type = (fmt >> 16) & 255;
    if ((fmt >> 24) != 32 || (type != 2 && type != 3) || (fmt & 0xfff) != 0x888 || x < 0 || y < 0 ||
        w < 1 || h < 1 || w > v->width || h > v->height || x > v->width - w || y > v->height - h ||
        stride < (unsigned)w * 4 || len < (uint64_t)stride * (h - 1) + (unsigned)w * 4)
        die("unsupported/invalid 2D scanout");
    texture(v->source_tex);
    /* a8r8g8b8 is BGRA in memory: upload it as it is where GLES can (whole rows, since GLES 2
     * has no unpack row length). Other formats, and x8 ones whose padding byte is not alpha,
     * are converted below. */
    if (v->bgra && fmt == 0x20028888 && (full || (x == 0 && w == v->width)) &&
        stride == (unsigned)w * 4) {
        tex_upload(v, full, 0, y, w, h, GL_BGRA_EXT, bytes);
        v->y0_top = 0;
        v->dma = 0;
        capture(v);
        return;
    }
    if (full && v->bgra) {
        /* the texture's format follows its last full upload; keep one format for sub-updates */
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
        full = 0;
    }
    unsigned char *rgba = g_malloc((size_t)w * h * 4);
    for (int row = 0; row < h; row++)
        for (int col = 0; col < w; col++) {
            const unsigned char *s = bytes + (size_t)row * stride + col * 4;
            unsigned char *d = rgba + ((size_t)row * w + col) * 4;
            d[0] = s[type == 2 ? 2 : 0];
            d[1] = s[1];
            d[2] = s[type == 2 ? 0 : 2];
            d[3] = ((fmt >> 12) & 15) ? s[3] : 255;
        }
    texture(v->source_tex);
    tex_upload(v, full, x, y, w, h, GL_RGBA, rgba);
    g_free(rgba);
    v->y0_top = 0;
    v->dma = 0;
    capture(v);
}
static void pixels(struct wv *v, int x, int y, int w, int h, unsigned stride, unsigned fmt,
                   GVariant *data, int full)
{
    gsize len;
    const unsigned char *bytes = g_variant_get_fixed_array(data, &len, 1);
    upload(v, x, y, w, h, stride, fmt, bytes, len, full);
}
static void drop_shadow(struct wv *v)
{
    g_free(v->shadow);
    v->shadow = NULL;
}
/* A 2D Scanout's or Update's pixels into the shadow (a Scanout: a new one; an Update in another
 * format than the shadow's: none from then on, each update uploaded as it comes). The rectangle
 * is checked as upload checks it. */
static void shadow_put(struct wv *v, int x, int y, int w, int h, unsigned stride, unsigned fmt,
                       GVariant *data, int full)
{
    gsize len;
    const unsigned char *bytes = g_variant_get_fixed_array(data, &len, 1);
    if (full) {
        drop_shadow(v);
        v->shadow = g_malloc((size_t)v->width * v->height * 4);
        v->shadow_format = fmt;
    } else if (!v->shadow || fmt != v->shadow_format) {
        drop_shadow(v);
        return;
    }
    if (x < 0 || y < 0 || w < 1 || h < 1 || x > v->width - w || y > v->height - h ||
        stride < (unsigned)w * 4 || len < (uint64_t)stride * (h - 1) + (unsigned)w * 4)
        die("unsupported/invalid 2D scanout");
    for (int row = 0; row < h; row++)
        memcpy(v->shadow + ((size_t)(y + row) * v->width + x) * 4, bytes + (size_t)row * stride,
               (size_t)w * 4);
}
static void unmap(struct wv *v)
{
    if (v->map)
        munmap((void *)v->map, v->map_len);
    v->map = NULL;
    v->map_len = 0;
}
/* Unix.Map: QEMU's surface in shared memory, top row first. Updates are read from it before
 * the reply, since QEMU reads the next frame into the same memory once we have returned. */
static void scanout_map(struct wv *v, GVariant *params, GDBusMethodInvocation *call)
{
    gint handle;
    guint offset, w, h, stride, fmt;
    g_variant_get(params, "(huuuuu)", &handle, &offset, &w, &h, &stride, &fmt);
    GUnixFDList *fds = g_dbus_message_get_unix_fd_list(g_dbus_method_invocation_get_message(call));
    GError *error = NULL;
    int fd = fds ? g_unix_fd_list_get(fds, handle, &error) : -1;
    check_error(error);
    if (fd < 0)
        die("ScanoutMap missing fd");
    if (h < 1 || stride < 4 || stride > 65536)
        die("invalid ScanoutMap geometry");
    unmap(v);
    v->map_len = (size_t)offset + (size_t)stride * h;
    void *map = mmap(NULL, v->map_len, PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (map == MAP_FAILED)
        die("ScanoutMap mmap failed");
    v->map = map;
    v->map_offset = offset;
    v->map_stride = stride;
    v->map_format = fmt;
    size(v, w, h);
    drop_image(v);
    v->maps++;
    upload(v, 0, 0, w, h, stride, fmt, v->map + offset, v->map_len - offset, 1);
}
static void update_map(struct wv *v, GVariant *params)
{
    gint x, y, w, h;
    g_variant_get(params, "(iiii)", &x, &y, &w, &h);
    if (!v->map || v->dma)
        return;
    if (v->idle) { /* read from the surface when drawn next (refresh) */
        v->stale = 1;
        v->skipped++;
        v->changes++;
        return;
    }
    if (x < 0 || y < 0 || w < 1 || h < 1 || x > v->width - w || y > v->height - h)
        die("invalid UpdateMap rectangle");
    size_t start = v->map_offset + (size_t)y * v->map_stride;
    if (v->bgra && v->map_format == 0x20028888 && v->map_stride == (unsigned)v->width * 4) {
        x = 0;
        w = v->width; /* whole rows (see upload) */
    } else {
        start += (size_t)x * 4;
    }
    upload(v, x, y, w, h, v->map_stride, v->map_format, v->map + start, v->map_len - start, 0);
}
/* The cache brought up to QEMU's surface after updates skipped while idle: the whole shared
 * surface (or the 2D shadow) uploaded, or the dmabuf copied as it is now. */
static void refresh(struct wv *v)
{
    if (!v->stale || (v->awaiting && g_get_monotonic_time() - v->scanout_at < SCANOUT_WAIT_US))
        return; /* a scanout whose update is about to copy it */
    v->stale = 0;
    v->awaiting = 0;
    if (v->dma)
        capture(v);
    else if (v->map)
        upload(v, 0, 0, v->width, v->height, v->map_stride, v->map_format, v->map + v->map_offset,
               v->map_len - v->map_offset, 0);
    else if (v->shadow)
        upload(v, 0, 0, v->width, v->height, (unsigned)v->width * 4, v->shadow_format, v->shadow,
               (size_t)v->width * v->height * 4, 0);
}
void wv_idle(struct wv *v, int idle)
{
    v->idle = !!idle;
    if (!v->idle && v->stale) {
        current(v);
        refresh(v);
    }
}
/* A call answered once the fence of its copy signals (QEMU blocks the guest's GL until then). */
struct reply {
    struct wv *v;
    GDBusMethodInvocation *call;
    int fd;
    guint source;
    gint64 since; /* when the call came */
};
static void reply_waited(struct wv *v, gint64 since)
{
    gint64 us = g_get_monotonic_time() - since;
    v->reply_us += us;
    if (us > v->reply_max_us)
        v->reply_max_us = us;
}
static void reply_send(struct reply *r)
{
    r->v->replies = g_slist_remove(r->v->replies, r);
    reply_waited(r->v, r->since);
    g_dbus_method_invocation_return_value(r->call, NULL);
    close(r->fd);
    g_free(r);
}
static gboolean reply_ready(gint fd, GIOCondition condition, gpointer data)
{
    (void)fd;
    (void)condition;
    reply_send(data);
    return G_SOURCE_REMOVE;
}
static void reply_later(struct wv *v, GDBusMethodInvocation *call, int fd, gint64 since)
{
    struct reply *r = g_new0(struct reply, 1);
    r->v = v;
    r->since = since;
    r->call = call;
    r->fd = fd;
    r->source = g_unix_fd_add_full(G_PRIORITY_HIGH, fd, G_IO_IN | G_IO_ERR | G_IO_HUP, reply_ready,
                                   r, NULL);
    v->replies = g_slist_prepend(v->replies, r);
    v->fenced++;
}
static void replies_flush(struct wv *v)
{
    while (v->replies) {
        struct reply *r = v->replies->data;
        g_source_remove(r->source);
        reply_send(r);
    }
}
static void changed(struct wv *v)
{
    if (v->dirty && v->redraw)
        v->redraw(v->data);
}
static void method(GDBusConnection *conn, const gchar *sender, const gchar *path,
                   const gchar *iface, const gchar *name, GVariant *params,
                   GDBusMethodInvocation *call, gpointer data)
{
    (void)conn;
    (void)sender;
    (void)path;
    (void)iface;
    /* QEMU's fixed-size cursor is not our display source. Acknowledge its
     * listener methods without inspecting pixels, allocating a texture,
     * changing cursor/pointer state or requesting a redraw. */
    if (!strcmp(name, "CursorDefine") || !strcmp(name, "MouseSet")) {
        g_dbus_method_invocation_return_value(call, NULL);
        return;
    }
    struct wv *v = data;
    gint64 since = g_get_monotonic_time();
    int dmabuf = !strcmp(name, "UpdateDMABUF");
    v->dmabuf_updates += dmabuf;
    current(v);
    int fence = -1; /* the reply waits for it */
    if (disable_pending(&v->disable) && (!strcmp(name, "ScanoutDMABUF") ||
                                         !strcmp(name, "ScanoutMap") || !strcmp(name, "Scanout"))) {
        disable_cancel(&v->disable); /* QEMU's stray Disable before a flip (disable.h) */
        v->disables_dropped++;
    }
    if (!strcmp(name, "ScanoutDMABUF")) {
        unmap(v);
        drop_shadow(v);
        fence = scanout_dma(v, params, call);
    } else if (!strcmp(name, "ScanoutMap")) {
        drop_shadow(v);
        scanout_map(v, params, call);
    } else if (!strcmp(name, "UpdateMap"))
        update_map(v, params);
    else if (!strcmp(name, "UpdateDMABUF")) {
        v->awaiting = 0;
        if (v->dma && v->idle) {
            v->stale = 1;
            v->skipped++;
            v->changes++;
        } else if (v->dma) {
            v->stale = 0;
            fence = capture_fenced(v, 1);
        }
    } else if (!strcmp(name, "Scanout")) {
        guint w, h, stride, fmt;
        GVariant *bytes;
        g_variant_get(params, "(uuuu@ay)", &w, &h, &stride, &fmt, &bytes);
        unmap(v);
        size(v, w, h);
        drop_image(v);
        shadow_put(v, 0, 0, w, h, stride, fmt, bytes, 1);
        pixels(v, 0, 0, w, h, stride, fmt, bytes, 1);
        v->stale = 0; /* the whole picture, uploaded now */
        g_variant_unref(bytes);
    } else if (!strcmp(name, "Update")) {
        gint x, y, w, h;
        guint stride, fmt;
        GVariant *bytes;
        g_variant_get(params, "(iiiiuu@ay)", &x, &y, &w, &h, &stride, &fmt, &bytes);
        if (!v->dma) {
            if (!v->map)
                shadow_put(v, x, y, w, h, stride, fmt, bytes, 0);
            if (v->idle && v->shadow) { /* uploaded from the shadow when drawn next (refresh) */
                v->stale = 1;
                v->skipped++;
                v->changes++;
            } else
                pixels(v, x, y, w, h, stride, fmt, bytes, 0);
        }
        g_variant_unref(bytes);
    } else if (!strcmp(name, "Disable")) {
        /* provisional: the picture and held input stay until disable_poll applies it */
        disable_arm(&v->disable, since);
        v->disables++;
    }
    if (fence >= 0) {
        reply_later(v, call, fence, since);
    } else {
        g_dbus_method_invocation_return_value(call, NULL);
        if (dmabuf)
            reply_waited(v, since);
    }
    changed(v);
}
static GVariant *property(GDBusConnection *conn, const gchar *sender, const gchar *path,
                          const gchar *iface, const gchar *prop, GError **error, gpointer data)
{
    (void)conn;
    (void)sender;
    (void)path;
    (void)iface;
    (void)prop;
    (void)error;
    (void)data;
    /* QEMU reads this to decide which optional listener interfaces to use. */
    static const char *const interfaces[] = {UNIX_MAP};
    return g_variant_new_strv(interfaces, G_N_ELEMENTS(interfaces));
}
static void closed(GDBusConnection *conn, gboolean remote, GError *error, gpointer data)
{
    (void)conn;
    (void)remote;
    struct wv *v = data;
    fprintf(stderr, "display disconnected%s%s\n", error ? ": " : "", error ? error->message : "");
    v->quit = 1;
}
/* QEMU's end of the display connection closed: the VM stopped, or its guest restarted and a new
 * QEMU comes (reconnect_poll decides) */
static void display_lost(GDBusConnection *conn, gboolean remote, GError *error, gpointer data)
{
    (void)conn;
    (void)remote;
    struct wv *v = data;
    fprintf(stderr, "display disconnected%s%s\n", error ? ": " : "", error ? error->message : "");
    if (!v->reconnect_until)
        v->lost_at = g_get_monotonic_time();
}
static char *name_owner(GDBusConnection *bus, const char *name)
{
    GVariant *reply = g_dbus_connection_call_sync(
        bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "GetNameOwner", g_variant_new("(s)", name), G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE,
        1000, NULL, NULL);
    if (!reply)
        return NULL;
    char *owner = NULL;
    g_variant_get(reply, "(s)", &owner);
    g_variant_unref(reply);
    return owner;
}
/* Registers as the listener of QEMU's console on the bus: v->peer, QEMU's connection to us. */
static int attach_display(struct wv *v, GError **error)
{
    GDBusConnection *bus = v->input.bus;
    GVariant *reply = g_dbus_connection_call_sync(
        bus, "org.qemu", CONSOLE, "org.freedesktop.DBus.Properties", "Get",
        g_variant_new("(ss)", "org.qemu.Display1.Mouse", "IsAbsolute"), G_VARIANT_TYPE("(v)"),
        G_DBUS_CALL_FLAGS_NONE, 5000, NULL, error);
    if (!reply)
        return 0;
    GVariant *value;
    g_variant_get(reply, "(v)", &value);
    if (!g_variant_get_boolean(value))
        die("QEMU mouse is not absolute; attach virtio-tablet-pci");
    g_variant_unref(value);
    g_variant_unref(reply);
    g_free(v->qemu_owner);
    v->qemu_owner = name_owner(bus, "org.qemu");
    int pair[2];
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair) < 0)
        die("socketpair failed");
    GUnixFDList *fds = g_unix_fd_list_new();
    int handle = g_unix_fd_list_append(fds, pair[1], error);
    if (handle < 0)
        die((*error)->message);
    reply = g_dbus_connection_call_with_unix_fd_list_sync(
        bus, "org.qemu", CONSOLE, "org.qemu.Display1.Console", "RegisterListener",
        g_variant_new("(h)", handle), NULL, G_DBUS_CALL_FLAGS_NONE, 5000, fds, NULL, NULL, error);
    g_object_unref(fds);
    close(pair[1]);
    if (!reply) {
        close(pair[0]);
        return 0;
    }
    g_variant_unref(reply);
    v->peer = peer_connect(pair[0], error);
    if (!v->peer)
        return 0;
    g_dbus_connection_set_exit_on_close(v->peer, FALSE);
    GDBusNodeInfo *info = g_dbus_node_info_new_for_xml(listener_xml, error);
    check_error(*error);
    static const GDBusInterfaceVTable table = {.method_call = method, .get_property = property};
    for (int i = 0; info->interfaces[i]; i++) {
        g_dbus_connection_register_object(v->peer, LISTENER, info->interfaces[i], &table, v, NULL,
                                          error);
        check_error(*error);
    }
    g_dbus_node_info_unref(info);
    g_signal_connect(v->peer, "closed", G_CALLBACK(display_lost), v);
    g_dbus_connection_start_message_processing(v->peer);
    return 1;
}
static void connect_display(struct wv *v, const char *addr)
{
    GError *error = NULL;
    GDBusConnection *bus =
        g_dbus_connection_new_for_address_sync(addr,
                                               G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT |
                                                   G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
                                               NULL, NULL, &error);
    check_error(error);
    v->input.bus = bus;
    g_dbus_connection_set_exit_on_close(bus, FALSE);
    attach_display(v, &error);
    check_error(error);
    /* the bus gone: the VM's unit ended */
    g_signal_connect(bus, "closed", G_CALLBACK(closed), v);
}
static void start_audio(struct wv *v)
{
    /* Audio plays through SDL with a window; headless only records (audio-wav) and counts,
     * unless sdl-audio (e.g. with SDL_AUDIODRIVER=disk). no-audio skips the listener. */
    if (v->sink)
        audio_connect(&v->audio, v->input.bus, 0, v->audio_wav, pcm_out_guest, v->pcm);
    else if (!v->no_audio)
        audio_connect(&v->audio, v->input.bus, !v->headless || v->sdl_audio, v->audio_wav, NULL,
                      NULL);
}
#define RESTART_GRACE_US 2000000   /* how long a lost display waits for the restart's marker */
#define RESTART_WAIT_US  180000000 /* how long the next QEMU is waited for */
#define RESTART_RETRY_US 500000
/* From wv_poll: a lost display ends the front end (quit), unless the run directory says the guest
 * restarts; then the old QEMU's listener goes, and once another QEMU owns org.qemu the display is
 * attached to it, with the audio. */
static void reconnect_poll(struct wv *v)
{
    gint64 now = g_get_monotonic_time();
    if (v->lost_at) {
        if (v->restarting && g_file_test(v->restarting, G_FILE_TEST_EXISTS)) {
            fprintf(stderr, "display: the guest restarts; waiting for its new QEMU\n");
            v->lost_at = 0;
            v->reconnect_until = now + RESTART_WAIT_US;
            v->reconnect_try = 0;
            if (v->peer) {
                g_signal_handlers_disconnect_by_data(v->peer, v);
                g_object_unref(v->peer);
                v->peer = NULL;
            }
            audio_close(&v->audio);
        } else if (now - v->lost_at > RESTART_GRACE_US) {
            v->quit = 1;
        }
        return;
    }
    if (!v->reconnect_until || now < v->reconnect_try)
        return;
    if (now > v->reconnect_until) {
        fprintf(stderr, "display: no new QEMU came\n");
        v->quit = 1;
        return;
    }
    v->reconnect_try = now + RESTART_RETRY_US;
    char *owner = name_owner(v->input.bus, "org.qemu");
    int fresh = owner && (!v->qemu_owner || strcmp(owner, v->qemu_owner));
    g_free(owner);
    if (!fresh)
        return;
    GError *error = NULL;
    if (!attach_display(v, &error)) {
        fprintf(stderr, "display: the new QEMU: %s\n", error ? error->message : "failed");
        g_clear_error(&error);
        g_clear_object(&v->peer);
        return;
    }
    v->reconnect_until = 0;
    start_audio(v);
    fprintf(stderr, "display: connected to the new QEMU\n");
    touch(v);
}
/* vm-info.toml in dir (host/core/beanjar/launch.py): its top-level lines, before any table */
static char **vm_info_lines(const char *dir)
{
    char *path = g_build_filename(dir, "vm-info.toml", NULL), *info = NULL;
    char **lines = NULL;
    if (g_file_get_contents(path, &info, NULL, NULL)) {
        lines = g_strsplit(info, "\n", -1);
        for (char **l = lines; *l; l++)
            if (*g_strstrip(*l) == '[') { /* a table: the rest is not top-level */
                g_free(*l);
                for (char **r = l + 1; *r; r++)
                    g_free(*r);
                *l = NULL;
                break;
            }
    }
    g_free(info);
    g_free(path);
    return lines;
}
/* A launch without the media helper (MEDIAHUB=0, or none built) has decoding off: its
 * vm-info.toml says media = "off", and the viewer then runs as with --no-media, the video
 * windows checkerboards. */
static int vm_info_media_off(const char *dir)
{
    char **lines = vm_info_lines(dir);
    int off = 0;
    for (char **l = lines; l && *l; l++)
        off |= !strcmp(*l, "media = \"off\"");
    g_strfreev(lines);
    return off;
}
/* vm-info.toml's integer key (key = N), or -1 */
static int vm_info_int(const char *dir, const char *key)
{
    char **lines = vm_info_lines(dir);
    int value = -1;
    for (char **l = lines; l && *l; l++) {
        size_t n = strlen(key);
        int x;
        char tail;
        if (!strncmp(*l, key, n) && (*l)[n] == ' ' && sscanf(*l + n, " = %d%c", &x, &tail) == 1 &&
            x >= 0)
            value = x;
    }
    g_strfreev(lines);
    return value;
}
void wv_start(struct wv *v)
{
    if (!v->addr)
        die("supply a bus address");
    SDL_SetHint(SDL_HINT_NO_SIGNAL_HANDLERS, "1");
    graphics(v);
    /* The launcher's sockets sit next to the D-Bus socket (build/). Another bus address needs
     * the required ones given; the capture socket is optional (none: no capture). */
    char *default_cursor = NULL, *default_video = NULL, *default_ir = NULL, *default_media = NULL,
         *default_capture = NULL;
    const char *prefix = "unix:path=";
    if (strncmp(v->addr, prefix, strlen(prefix))) {
        GString *missing = g_string_new(NULL);
        if (!v->cursor_path)
            g_string_append(missing, " --cursor-socket");
        if (!v->video_path)
            g_string_append(missing, " --video-socket");
        if (!v->ir_path)
            g_string_append(missing, " --ir-socket");
        if (!v->media_prefix && !v->no_media)
            g_string_append(missing, " --media-socket (or --no-media)");
        if (missing->len) {
            char *message = g_strdup_printf("supply%s for this bus address", missing->str);
            die(message);
        }
        g_string_free(missing, TRUE);
    } else if (!v->cursor_path || !v->video_path || !v->ir_path || !v->capture_path ||
               (!v->media_prefix && !v->no_media)) {
        char *buspath = g_uri_unescape_string(v->addr + strlen(prefix), NULL);
        char *comma = strchr(buspath, ',');
        if (comma)
            *comma = 0;
        char *dir = g_path_get_dirname(buspath);
        default_cursor = g_build_filename(dir, "cursor.sock", NULL);
        default_video = g_build_filename(dir, "video.sock", NULL);
        default_ir = g_build_filename(dir, "ir.sock", NULL);
        default_media = g_build_filename(dir, "media", NULL);
        default_capture = g_build_filename(dir, "capture.sock", NULL);
        v->restarting = g_build_filename(dir, "restarting", NULL);
        /* A launch without the media helper (MEDIAHUB=0, or none built) has decoding off: its
         * vm-info.toml says media = "off" (host/core/beanjar/launch.py), and the viewer then
         * runs as with --no-media, the video windows checkerboards. */
        if (!v->media_prefix && !v->no_media && vm_info_media_off(dir)) {
            v->no_media = 1;
            fprintf(stderr, "media: decoding off (the launch runs no media helper)\n");
        }
        g_free(dir);
        g_free(buspath);
    }
    /* the board's panel, from the launch's vm-info.toml beside the bus socket */
    if (!strncmp(v->addr, prefix, strlen(prefix))) {
        char *buspath = g_uri_unescape_string(v->addr + strlen(prefix), NULL);
        char *comma = strchr(buspath, ',');
        if (comma)
            *comma = 0;
        char *dir = g_path_get_dirname(buspath);
        int hz = vm_info_int(dir, "panel_hz");
        if (hz > 0)
            v->panel_hz = hz;
        g_free(dir);
        g_free(buspath);
    }
    if (v->output_hz < 0)
        v->output_hz = v->panel_hz < OUTPUT_HZ_LIMIT ? v->panel_hz : OUTPUT_HZ_LIMIT;
    pace_set(&v->pace, v->output_hz);
    fprintf(stderr, "output: %d Hz panel, %d pictures a second%s\n", v->panel_hz, v->output_hz,
            v->output_hz ? "" : " (unpaced)");
    cursor_init(&v->cursor, v->cursor_path ? v->cursor_path : default_cursor);
    video_init(&v->video, v->video_path ? v->video_path : default_video);
    ir_init(&v->ir, v->ir_path ? v->ir_path : default_ir);
    /* The media streams (media.c): decoded pictures in the video windows, media audio through a
     * second SDL device (or, headless without sdl-audio or with media-silent, a clock thread and
     * media-wav: the media audio recorded, never heard). */
    if (!v->no_media) {
        struct media_config mc = {.prefix = v->media_prefix ? v->media_prefix : default_media,
                                  .sdl_audio = (!v->headless || v->sdl_audio) && !v->media_silent,
                                  .wav = v->media_wav,
                                  .audio_latency_ms = v->media_latency,
                                  .hwdec = v->hwdec,
                                  .volume = v->media_volume,
                                  .mute = v->media_mute,
                                  .external_clock = v->sink != NULL};
        v->video.media = media_open(&mc);
    }
    g_free(default_cursor);
    g_free(default_video);
    g_free(default_ir);
    g_free(default_media);
    capture_init(&v->capture, v->capture_path ? v->capture_path : default_capture);
    g_free(default_capture);
    connect_display(v, v->addr);
    /* wv_audio_sink: the mix goes to the front end, nothing to SDL */
    if (v->sink)
        v->pcm = pcm_out_start(v->video.media, v->sink, v->sink_data, v->sink_wanted);
    start_audio(v);
}
void wv_audio_sink(struct wv *v, wv_audio_fn fn, void *data)
{
    v->sink = fn;
    v->sink_data = data;
}
void wv_audio_wanted(struct wv *v, int wanted)
{
    v->sink_wanted = wanted;
    pcm_out_wanted(v->pcm, wanted);
}
static int capture_render_frame(void *, const struct webos_capture_header *, unsigned char **);
/* LG's cursor as wv_cursor gives it: a new serial when its picture, hotspot, visibility or size in
 * the view changed. Returns 1 then. */
static int cursor_count(struct wv *v)
{
    const struct webos_cursor_header *c = &v->cursor.h;
    struct rect r = fit(v->view_w, v->view_h, v->width, v->height);
    unsigned key[10] = {c->visible && !v->standby && !(hidden_now(v) & WV_PLANE_CURSOR),
                        v->cursor.revision,
                        c->width,
                        c->height,
                        c->draw_w,
                        c->draw_h,
                        c->hot_x,
                        c->hot_y,
                        c->screen_w ? (unsigned)r.w * 65536 / c->screen_w : 0,
                        c->screen_h ? (unsigned)r.h * 65536 / c->screen_h : 0};
    if (v->cursor_serial && !memcmp(key, v->cursor_key, sizeof key))
        return 0;
    memcpy(v->cursor_key, key, sizeof key);
    v->cursor_serial++;
    return 1;
}
unsigned wv_cursor(struct wv *v, void *rgba, int *width, int *height, int *hot_x, int *hot_y,
                   int *view_w, int *view_h)
{
    cursor_count(v);
    const struct webos_cursor_header *c = &v->cursor.h;
    if (v->standby || (hidden_now(v) & WV_PLANE_CURSOR) || !c->visible || !c->width || !c->height ||
        !c->screen_w || !c->screen_h)
        return 0;
    if (rgba) {
        struct rect r = fit(v->view_w, v->view_h, v->width, v->height);
        unsigned cw = c->draw_w ? c->draw_w : c->width, ch = c->draw_h ? c->draw_h : c->height;
        memcpy(rgba, v->cursor.rgba, (size_t)c->width * c->height * 4);
        *width = (int)c->width;
        *height = (int)c->height;
        *hot_x = (int)c->hot_x;
        *hot_y = (int)c->hot_y;
        *view_w = MAX(1, (int)((double)cw * r.w / c->screen_w + 0.5));
        *view_h = MAX(1, (int)((double)ch * r.h / c->screen_h + 0.5));
    }
    return v->cursor_serial;
}
/* From wv_poll: a Disable no scanout cancelled within DISABLE_WAIT_US is real (the guest turned its
 * output off): the picture goes black, and keys and buttons held through the display
 * are released, as nothing shown is left for them to act on. A stray Disable keeps both: it came
 * between two flips of a display that never went away, and releasing then would cut a long press
 * or a drag short under load. */
static void disable_poll(struct wv *v)
{
    if (!disable_due(&v->disable, g_get_monotonic_time()))
        return;
    v->valid = 0;
    touch(v);
    input_release(&v->input);
}
int wv_poll(struct wv *v)
{
    current(v);
    disable_poll(v);
    /* no-cursor: only what the front end's cursor shows is a change, not libim's moves */
    if (cursor_poll(&v->cursor) && (!v->no_cursor || cursor_count(v)))
        touch(v);
    if (video_poll(&v->video))
        touch(v);
    if (media_update(v->video.media))
        touch(v);
    capture_poll(&v->capture, capture_render_frame, v);
    reconnect_poll(v);
    if (signalled)
        v->quit = 1;
    /* a change is reported when the output rate allows a picture (pace) */
    int due = v->dirty && !pace_wait(&v->pace, g_get_monotonic_time());
    return (due ? 1 : 0) | (v->quit ? 2 : 0);
}
static gboolean tick(gpointer data)
{
    struct wv *v = data;
    int r = wv_poll(v);
    if ((r & 1) && v->redraw)
        v->redraw(v->data);
    if ((r & 2) && !v->quit_sent) {
        v->quit_sent = 1;
        if (v->quit_cb)
            v->quit_cb(v->data);
    }
    return G_SOURCE_CONTINUE;
}
/* org.beanjar.Display1 on the VM's bus, served by the front end that owns the display (the GTK
 * viewer or beanjar-vnc), on the main loop. The name is requested without queueing: one front end
 * serves a VM.
 * - Screenshot(path, width, height): the scene saved as PPM (wv_screenshot, with LG's cursor even
 *   for a front end that shows it itself).
 * - Ir(code, down): a TV remote button edge, LG's IR data code (0 to 255), as wv_ir: one button is
 *   down at a time, a press of another releases it. A button a client holds is released when that
 *   client leaves the bus (beanjar remote pipe, docs/dev/architecture/web-viewer.md).
 * - Pointer(x, y, buttons): the tablet to x, y in the guest's display (scanout) pixels, then the
 *   buttons to the mask (bit 0 left, 1 middle, 2 right), as a VNC pointer event: the edges are what
 *   changed. Buttons a client holds are released when it leaves the bus (beanjar remote click).
 * - Touch(kind, slot, x, y): the touch screen (input_touch_abs: kind 0 begin, 1 move, 2 end,
 *   3 cancel; slot 0 to 9) at x, y in the guest's display pixels, for a VM whose model has one
 *   (targets.Soc.touch; elsewhere QEMU drops it). Touches a client holds end when it leaves the bus
 *   (beanjar remote touch, the web viewer's touch events through beanjar remote pipe). */
#define SCREENSHOT_NAME "org.beanjar.Display1"
#define SCREENSHOT_PATH "/org/beanjar/Display1"
static const char screenshot_xml[] = "<node><interface name='" SCREENSHOT_NAME "'>"
                                     "<method name='Screenshot'><arg type='s' direction='in'/>"
                                     "<arg type='u' direction='in'/><arg type='u' direction='in'/>"
                                     "</method><method name='Ir'><arg type='u' direction='in'/>"
                                     "<arg type='b' direction='in'/></method>"
                                     "<method name='Pointer'><arg type='u' direction='in'/>"
                                     "<arg type='u' direction='in'/><arg type='u' direction='in'/>"
                                     "</method><method name='Touch'><arg type='u' direction='in'/>"
                                     "<arg type='u' direction='in'/><arg type='u' direction='in'/>"
                                     "<arg type='u' direction='in'/></method>"
                                     "</interface></node>";
static void ir_unwatch(struct wv *v)
{
    if (v->ir_watch)
        g_bus_unwatch_name(v->ir_watch);
    v->ir_watch = 0;
    g_free(v->ir_owner);
    v->ir_owner = NULL;
}
static void ir_owner_gone(GDBusConnection *conn, const gchar *name, gpointer data)
{
    (void)conn;
    struct wv *v = data;
    if (!v->ir_owner || strcmp(name, v->ir_owner))
        return;
    if (v->ir_bus_held >= 0 && v->ir.held == v->ir_bus_held) {
        fprintf(stderr, "ir: %s left with 0x%02x down: released\n", name, v->ir_bus_held);
        ir_button(&v->ir, (unsigned)v->ir_bus_held, 0);
    }
    v->ir_bus_held = -1;
    ir_unwatch(v);
}
static void ir_call(struct wv *v, const char *sender, GVariant *params, GDBusMethodInvocation *call)
{
    guint code;
    gboolean down;
    g_variant_get(params, "(ub)", &code, &down);
    if (code > 0xff) {
        g_dbus_method_invocation_return_dbus_error(call, SCREENSHOT_NAME ".Error.InvalidArgs",
                                                   "an IR data code of 0 to 255");
        return;
    }
    if (down) {
        if (sender && (!v->ir_owner || strcmp(sender, v->ir_owner))) {
            ir_unwatch(v);
            v->ir_owner = g_strdup(sender);
            v->ir_watch = g_bus_watch_name_on_connection(
                v->input.bus, sender, G_BUS_NAME_WATCHER_FLAGS_NONE, NULL, ir_owner_gone, v, NULL);
        }
        v->ir_bus_held = (int)code;
    } else if (v->ir_bus_held == (int)code)
        v->ir_bus_held = -1;
    wv_ir(v, code, down);
    g_dbus_method_invocation_return_value(call, NULL);
}
static void ptr_unwatch(struct wv *v)
{
    if (v->ptr_watch)
        g_bus_unwatch_name(v->ptr_watch);
    v->ptr_watch = 0;
    g_free(v->ptr_owner);
    v->ptr_owner = NULL;
}
static void ptr_owner_gone(GDBusConnection *conn, const gchar *name, gpointer data)
{
    (void)conn;
    struct wv *v = data;
    if (!v->ptr_owner || strcmp(name, v->ptr_owner))
        return;
    for (unsigned b = 0; b < 3; b++)
        if (v->ptr_bus_held & (1u << b)) {
            fprintf(stderr, "pointer: %s left with button %u down: released\n", name, b);
            input_button_edge(&v->input, b, 0);
        }
    v->ptr_bus_held = 0;
    ptr_unwatch(v);
}
static void pointer_call(struct wv *v, const char *sender, GVariant *params,
                         GDBusMethodInvocation *call)
{
    guint x, y, buttons;
    g_variant_get(params, "(uuu)", &x, &y, &buttons);
    if (v->input.width < 1 || v->input.height < 1) {
        g_dbus_method_invocation_return_dbus_error(call, SCREENSHOT_NAME ".Error.NoPicture",
                                                   "no picture yet (the guest has not drawn)");
        return;
    }
    if (x >= (guint)v->input.width || y >= (guint)v->input.height || buttons > 7) {
        char *text = g_strdup_printf("a position inside the %dx%d display and buttons 0 to 7",
                                     v->input.width, v->input.height);
        g_dbus_method_invocation_return_dbus_error(call, SCREENSHOT_NAME ".Error.InvalidArgs",
                                                   text);
        g_free(text);
        return;
    }
    if (buttons && sender && (!v->ptr_owner || strcmp(sender, v->ptr_owner))) {
        ptr_unwatch(v);
        v->ptr_owner = g_strdup(sender);
        v->ptr_watch = g_bus_watch_name_on_connection(
            v->input.bus, sender, G_BUS_NAME_WATCHER_FLAGS_NONE, NULL, ptr_owner_gone, v, NULL);
    }
    input_abs(&v->input, x, y);
    for (unsigned b = 0; b < 3; b++)
        input_button_edge(&v->input, b, !!(buttons & (1u << b)));
    v->ptr_bus_held = buttons;
    g_dbus_method_invocation_return_value(call, NULL);
}
static void touch_unwatch(struct wv *v)
{
    if (v->touch_watch)
        g_bus_unwatch_name(v->touch_watch);
    v->touch_watch = 0;
    g_free(v->touch_owner);
    v->touch_owner = NULL;
}
static void touch_owner_gone(GDBusConnection *conn, const gchar *name, gpointer data)
{
    (void)conn;
    struct wv *v = data;
    if (!v->touch_owner || strcmp(name, v->touch_owner))
        return;
    if (v->input.touches)
        fprintf(stderr, "touch: %s left with touches down: ended\n", name);
    input_touch_release(&v->input);
    touch_unwatch(v);
}
static void touch_call(struct wv *v, const char *sender, GVariant *params,
                       GDBusMethodInvocation *call)
{
    guint kind, slot, x, y;
    g_variant_get(params, "(uuuu)", &kind, &slot, &x, &y);
    if (v->input.width < 1 || v->input.height < 1) {
        g_dbus_method_invocation_return_dbus_error(call, SCREENSHOT_NAME ".Error.NoPicture",
                                                   "no picture yet (the guest has not drawn)");
        return;
    }
    if (kind > INPUT_TOUCH_CANCEL || slot >= INPUT_TOUCH_SLOTS || x >= (guint)v->input.width ||
        y >= (guint)v->input.height) {
        char *text = g_strdup_printf("a kind of 0 to 3, a slot of 0 to %d and a position inside "
                                     "the %dx%d display",
                                     INPUT_TOUCH_SLOTS - 1, v->input.width, v->input.height);
        g_dbus_method_invocation_return_dbus_error(call, SCREENSHOT_NAME ".Error.InvalidArgs",
                                                   text);
        g_free(text);
        return;
    }
    if (kind == INPUT_TOUCH_BEGIN && sender &&
        (!v->touch_owner || strcmp(sender, v->touch_owner))) {
        /* one client's touches at a time: another's that are down end */
        input_touch_release(&v->input);
        touch_unwatch(v);
        v->touch_owner = g_strdup(sender);
        v->touch_watch = g_bus_watch_name_on_connection(
            v->input.bus, sender, G_BUS_NAME_WATCHER_FLAGS_NONE, NULL, touch_owner_gone, v, NULL);
    }
    input_touch_abs(&v->input, kind, slot, x, y);
    g_dbus_method_invocation_return_value(call, NULL);
}
void wv_touch(struct wv *v, unsigned kind, unsigned slot, int x, int y)
{
    unturn(v, &x, &y);
    input_touch(&v->input, kind, slot, x, y);
}
static void screenshot_call(GDBusConnection *conn, const gchar *sender, const gchar *path,
                            const gchar *iface, const gchar *name, GVariant *params,
                            GDBusMethodInvocation *call, gpointer data)
{
    (void)conn;
    (void)path;
    (void)iface;
    struct wv *v = data;
    if (!strcmp(name, "Ir")) {
        ir_call(v, sender, params, call);
        return;
    }
    if (!strcmp(name, "Pointer")) {
        pointer_call(v, sender, params, call);
        return;
    }
    if (!strcmp(name, "Touch")) {
        touch_call(v, sender, params, call);
        return;
    }
    const char *file;
    guint w, h;
    g_variant_get(params, "(&suu)", &file, &w, &h);
    if (!v->valid && v->stale) {
        /* idle since before the first copy (a front end with no consumer: beanjar-vnc with no
         * client): the guest's frame is there, only not copied yet */
        current(v);
        refresh(v);
    }
    if (file[0] != '/' || w < 1 || h < 1 || w > 16384 || h > 16384)
        g_dbus_method_invocation_return_dbus_error(call, SCREENSHOT_NAME ".Error.InvalidArgs",
                                                   "an absolute path and a size of 1 to 16384");
    else if (!v->valid)
        g_dbus_method_invocation_return_dbus_error(call, SCREENSHOT_NAME ".Error.NoPicture",
                                                   "no picture yet (the guest has not drawn)");
    else if (v->shot_fn) {
        struct shot *shot = g_new0(struct shot, 1);
        shot->path = g_strdup(file);
        shot->width = (int)w;
        shot->height = (int)h;
        shot->call = call;
        g_queue_push_tail(&v->shots, shot);
        v->shot_fn(v->shot_data);
    } else {
        int no_cursor = v->no_cursor;
        v->no_cursor = 0;
        v->whole = 1; /* the VM's picture, whatever this front end hides */
        int bad = wv_screenshot(v, file, (int)w, (int)h);
        v->whole = 0;
        v->no_cursor = no_cursor;
        if (bad)
            g_dbus_method_invocation_return_dbus_error(call, SCREENSHOT_NAME ".Error.Failed",
                                                       "cannot render or write the screenshot");
        else
            g_dbus_method_invocation_return_value(call, NULL);
    }
}
void wv_screenshot_hook(struct wv *v, wv_callback fn, void *data)
{
    v->shot_fn = fn;
    v->shot_data = data;
}
const char *wv_screenshot_request(struct wv *v, int *width, int *height)
{
    struct shot *shot = g_queue_peek_head(&v->shots);
    if (!shot)
        return NULL;
    *width = shot->width;
    *height = shot->height;
    return shot->path;
}
void wv_screenshot_reply(struct wv *v, int failed)
{
    struct shot *shot = g_queue_pop_head(&v->shots);
    if (!shot)
        return;
    if (failed < 0) { /* the core's own, as without the hook */
        int no_cursor = v->no_cursor;
        v->no_cursor = 0;
        v->whole = 1;
        failed = wv_screenshot(v, shot->path, shot->width, shot->height);
        v->whole = 0;
        v->no_cursor = no_cursor;
    }
    if (failed)
        g_dbus_method_invocation_return_dbus_error(shot->call, SCREENSHOT_NAME ".Error.Failed",
                                                   "cannot render or write the screenshot");
    else
        g_dbus_method_invocation_return_value(shot->call, NULL);
    g_free(shot->path);
    g_free(shot);
}
static void screenshot_lost(GDBusConnection *conn, const gchar *name, gpointer data)
{
    (void)conn;
    (void)data;
    fprintf(stderr, "screenshots: not served, %s is %s\n", name,
            conn ? "owned by another client" : "unavailable");
}
static void screenshot_start(struct wv *v)
{
    GError *error = NULL;
    if (!v->input.bus || v->name_id)
        return;
    GDBusNodeInfo *info = g_dbus_node_info_new_for_xml(screenshot_xml, &error);
    check_error(error);
    static const GDBusInterfaceVTable table = {.method_call = screenshot_call};
    g_dbus_connection_register_object(v->input.bus, SCREENSHOT_PATH, info->interfaces[0], &table, v,
                                      NULL, &error);
    g_dbus_node_info_unref(info);
    check_error(error);
    v->name_id = g_bus_own_name_on_connection(v->input.bus, SCREENSHOT_NAME,
                                              G_BUS_NAME_OWNER_FLAGS_DO_NOT_QUEUE, NULL,
                                              screenshot_lost, v, NULL);
}
void wv_attach(struct wv *v)
{
    if (!v->source)
        /* the poll: often enough for the output rate, or the panel's where the front end paces */
        v->source = g_timeout_add(pace_poll_ms(v->output_hz ? v->output_hz : v->panel_hz), tick, v);
    screenshot_start(v);
}
/* The PC keyboard's keys for the remote's own functions go to the micom as the remote's buttons
 * (host/viewer/README.md, "PC keyboard"); every other key is a keyboard key. */
static int evdev_ir(unsigned evdev)
{
    switch (evdev) {
    case KEY_LEFTMETA:
    case KEY_RIGHTMETA:
    case KEY_HOME:
        return WEBOS_IR_HOME;
    case KEY_ESC:
        return WEBOS_IR_BACK;
    case KEY_VOLUMEUP:
        return WEBOS_IR_VOLUMEUP;
    case KEY_VOLUMEDOWN:
        return WEBOS_IR_VOLUMEDOWN;
    case KEY_MUTE:
        return WEBOS_IR_MUTE;
    }
    return -1;
}
int wv_key_evdev(struct wv *v, unsigned evdev, int down)
{
    int code = evdev_ir(evdev);
    if (code >= 0) {
        ir_button(&v->ir, (unsigned)code, down);
        return 1;
    }
    unsigned q = input_evdev_qnum(evdev);
    if (q)
        input_key(&v->input, q, down);
    return q != 0;
}
void wv_ir(struct wv *v, unsigned code, int down)
{
    ir_button(&v->ir, code, down);
}
void wv_local_key(struct wv *v, unsigned code, int down)
{
    ir_local_key(&v->ir, code, down);
}
void wv_motion(struct wv *v, int x, int y)
{
    unturn(v, &x, &y);
    input_position(&v->input, x, y);
    struct rect r = v->input.viewport;
    v->pointer_present = 1;
    v->cursor.inside = x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h;
    v->cursor.local_x = x;
    v->cursor.local_y = y;
    if (v->no_cursor)
        return; /* the picture does not follow the pointer */
    touch(v);
    changed(v);
}
void wv_leave(struct wv *v)
{
    v->pointer_present = v->cursor.inside = 0;
    if (v->no_cursor)
        return;
    touch(v);
    changed(v);
}
void wv_button(struct wv *v, unsigned button, int down, int x, int y)
{
    unturn(v, &x, &y);
    input_button(&v->input, button, down, x, y);
}
void wv_wheel(struct wv *v, int steps)
{
    input_wheel(&v->input, steps);
}
void wv_release(struct wv *v)
{
    input_release(&v->input);
    ir_release(&v->ir);
}
int wv_pointer_blank(struct wv *v)
{
    return v->valid && cursor_blank(&v->cursor);
}
void wv_pointer_blank_set(struct wv *v)
{
    v->cursor.blank_sets++;
}
int wv_planes(struct wv *v, int hidden)
{
    if (hidden >= 0 && (hidden &= WV_PLANE_VIDEO | WV_PLANE_UI | WV_PLANE_CURSOR) != v->hidden) {
        v->hidden = hidden;
        touch(v);
        changed(v);
    }
    return v->hidden;
}
void wv_standby(struct wv *v, int standby)
{
    if (v->standby == !!standby)
        return;
    v->standby = !!standby;
    touch(v);
    changed(v);
}
long wv_counter(struct wv *v, const char *name)
{
    struct {
        const char *name;
        long value;
    } counters[] = {
        {"frames", v->frames},
        {"fenced", v->fenced},
        {"imports", v->imports},
        {"maps", v->maps},
        {"skipped", v->skipped},
        {"cursor_draws", v->cursor_draws},
        {"input_calls", v->input.calls},
        {"input_errors", v->input.errors},
        {"input_pending", v->input.pending},
        {"ir_sent", v->ir.sent},
        {"ir_errors", v->ir.errors},
        {"valid", v->valid},
        {"disables", v->disables},
        {"disables_dropped", v->disables_dropped},
        {"width", v->width},
        {"height", v->height},
        {"cache_w", v->cache_w},
        {"cache_h", v->cache_h},
        {"recopies", v->recopies},
        {"changes", (long)v->changes},
        {"dma", v->dma},
        {"cursor_blank_sets", v->cursor.blank_sets},
        {"standby", v->standby},
        {"hidden", v->hidden},
        {"dmabuf_updates", v->dmabuf_updates},
        {"reply_us", (long)v->reply_us},
        {"reply_max_us", (long)v->reply_max_us},
        {"panel_hz", v->panel_hz},
        {"output_hz", v->output_hz},
        {"pictures", (long)v->pictures},
    };
    for (size_t i = 0; i < G_N_ELEMENTS(counters); i++)
        if (!strcmp(name, counters[i].name))
            return counters[i].value;
    return -1;
}
static int write_ppm(const char *path, int w, int h)
{
    unsigned char *rgba = g_malloc((size_t)w * h * 4);
    glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, rgba);
    int ok = glGetError() == GL_NO_ERROR;
    FILE *f = ok ? fopen(path, "wb") : NULL;
    if (f) {
        fprintf(f, "P6\n%d %d\n255\n", w, h);
        for (int y = h - 1; y >= 0 && ok; y--)
            for (int x = 0; x < w && ok; x++)
                ok = fwrite(rgba + ((size_t)y * w + x) * 4, 1, 3, f) == 3;
        ok = !fclose(f) && ok;
    } else
        ok = 0;
    g_free(rgba);
    if (!ok)
        fprintf(stderr, "viewer: cannot write the screenshot %s\n", path);
    return ok ? 0 : -1;
}
static int capture_render_frame(void *data, const struct webos_capture_header *request,
                                unsigned char **pixels)
{
    struct wv *v = data;
    if (request->mode < WEBOS_CAPTURE_VIDEO || request->mode > WEBOS_CAPTURE_OSD)
        return -EOPNOTSUPP;
    current(v);
    /* idle since before the first copy (beanjar-vnc with no client): the guest's frame is there,
     * only not copied yet, as for a bus Screenshot */
    if (!v->valid && v->stale)
        refresh(v);
    if (!v->valid || v->quit)
        return -ENODATA;
    int w = request->width, h = request->height;
    GLint previous;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &previous);
    GLuint tex, fbo;
    glGenTextures(1, &tex);
    texture(tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glGenFramebuffers(1, &fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
    int dirty = v->dirty, no_osd = v->no_osd, no_cursor = v->no_cursor, result = -EIO;
    GLint target = v->target;
    uint32_t count = v->video.table.count;
    uint32_t visible = v->cursor.h.visible;
    v->no_cursor =
        0;        /* the guest's capture has LG's cursor as before, whoever shows it on the host */
    v->whole = 1; /* and every plane, whatever this front end hides */
    if (request->mode == WEBOS_CAPTURE_VIDEO) {
        v->no_osd = 1;
        v->cursor.h.visible = 0;
    } else if (request->mode == WEBOS_CAPTURE_OSD) {
        v->no_osd = 0;
        v->video.table.count = 0;
    } else {
        v->no_osd = 0;
    }
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE) {
        v->target = fbo;
        unsigned drawn = v->video.drawn, pictures = v->video.pictures;
        draw(v, w, h, 0);
        /* A window without a decoded picture (black, or the checkerboard with decoding off) is
         * not captured video. Require an actual decoded picture for every shown window. */
        int missing = v->video.drawn - drawn != v->video.pictures - pictures ||
                      (request->mode == WEBOS_CAPTURE_VIDEO && v->video.pictures == pictures);
        if (missing) {
            result = -ENODATA;
        } else {
            unsigned char *raw = malloc((size_t)w * h * 4);
            unsigned char *top = malloc((size_t)w * h * 4);
            if (raw && top) {
                glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, raw);
                if (glGetError() == GL_NO_ERROR) {
                    for (int y = 0; y < h; y++)
                        memcpy(top + (size_t)y * w * 4, raw + (size_t)(h - 1 - y) * w * 4,
                               (size_t)w * 4);
                    *pixels = top;
                    top = NULL;
                    result = 0;
                }
            } else
                result = -ENOMEM;
            free(raw);
            free(top);
        }
    }
    v->dirty = dirty;
    v->target = target;
    v->whole = 0;
    v->no_osd = no_osd;
    v->no_cursor = no_cursor;
    v->video.table.count = count;
    v->cursor.h.visible = visible;
    cache_restore(v);
    glBindFramebuffer(GL_FRAMEBUFFER, previous);
    glDeleteFramebuffers(1, &fbo);
    glDeleteTextures(1, &tex);
    return result;
}
int wv_screenshot(struct wv *v, const char *path, int w, int h)
{
    if (w < 1 || h < 1 || w > 16384 || h > 16384)
        return -1;
    current(v);
    GLint previous;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &previous);
    GLuint tex, fbo;
    glGenTextures(1, &tex);
    texture(tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glGenFramebuffers(1, &fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
    int dirty = v->dirty, result = -1;
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE) {
        v->target = fbo;
        if (v->osd_dump && (v->cache_w < v->width || v->cache_h < v->height))
            cache_alloc(v, v->width, v->height); /* the OSD dump is at the scanout's size */
        draw(v, w, h, 0);
        result = write_ppm(path, w, h);
    }
    v->dirty = dirty;
    glBindFramebuffer(GL_FRAMEBUFFER, previous);
    glDeleteFramebuffers(1, &fbo);
    glDeleteTextures(1, &tex);
    if (result || !v->osd_dump || !v->valid) {
        cache_restore(v);
        return result;
    }
    /* osd-alpha: the guest's OSD as received (the cache texture, before compositing), with its
     * alpha, at the scanout's size: PATH.osd.pam (RGB_ALPHA, top row first). Where the OSD is
     * alpha 0 the video shows through. */
    w = v->cache_w;
    h = v->cache_h;
    unsigned char *rgba = g_malloc((size_t)w * h * 4);
    glBindFramebuffer(GL_FRAMEBUFFER, v->fbo);
    glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, rgba);
    glBindFramebuffer(GL_FRAMEBUFFER, previous);
    if (glGetError() != GL_NO_ERROR)
        die("OSD readback failed");
    char *name = g_strdup_printf("%s.osd.pam", path);
    FILE *f = fopen(name, "wb");
    if (!f)
        die("cannot open OSD dump");
    fprintf(f, "P7\nWIDTH %d\nHEIGHT %d\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n", w, h);
    for (int y = h - 1; y >= 0; y--)
        if (fwrite(rgba + (size_t)y * w * 4, 4, w, f) != (size_t)w)
            die("OSD dump write failed");
    if (fclose(f))
        die("OSD dump close failed");
    g_free(name);
    g_free(rgba);
    cache_restore(v);
    return 0;
}
static void frame_drop(struct wv *v)
{
    if (!v->frame_w)
        return;
    glDeleteFramebuffers(2, v->frame_fbo);
    glDeleteTextures(2, v->frame_tex);
    v->frame_w = v->frame_h = 0;
}
/* wv_frame's two render targets at w x h, kept between frames. */
static int frame_targets(struct wv *v, int w, int h)
{
    if (v->frame_w == w && v->frame_h == h)
        return 0;
    frame_drop(v);
    glGenTextures(2, v->frame_tex);
    glGenFramebuffers(2, v->frame_fbo);
    v->frame_w = w;
    v->frame_h = h;
    for (int i = 0; i < 2; i++) {
        texture(v->frame_tex[i]);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
        glBindFramebuffer(GL_FRAMEBUFFER, v->frame_fbo[i]);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, v->frame_tex[i],
                               0);
        if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
            frame_drop(v);
            return -1;
        }
    }
    return 0;
}
/* The scene into the first target, then drawn flipped into fbo: top row first in its memory. */
static int frame_into(struct wv *v, GLuint fbo, int w, int h)
{
    if (frame_targets(v, w, h))
        return -1;
    v->target = v->frame_fbo[0];
    draw(v, w, h, 1);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glViewport(0, 0, w, h);
    glDisable(GL_BLEND);
    glDisable(GL_SCISSOR_TEST);
    quad(v, v->frame_tex[0], 1);
    return 0;
}
int wv_frame_fbo(struct wv *v, unsigned fbo, int w, int h)
{
    if (w < 1 || h < 1 || w > 16384 || h > 16384)
        return -1;
    current(v);
    while (glGetError() != GL_NO_ERROR) {
    }
    GLint previous;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &previous);
    int result = frame_into(v, fbo, w, h) || glGetError() != GL_NO_ERROR ? -1 : 0;
    glBindFramebuffer(GL_FRAMEBUFFER, previous);
    return result;
}
/* Into the second target, so that glReadPixels (bottom row first) returns the top row first
 * without a copy on the CPU. */
int wv_frame(struct wv *v, void *pixels, int w, int h)
{
    if (!pixels || w < 1 || h < 1 || w > 16384 || h > 16384)
        return -1;
    current(v);
    while (glGetError() != GL_NO_ERROR) {
    }
    GLint previous;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &previous);
    int result = -1;
    if (!frame_targets(v, w, h) && !frame_into(v, v->frame_fbo[1], w, h)) {
        glPixelStorei(GL_PACK_ALIGNMENT, 4);
        glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
        result = glGetError() == GL_NO_ERROR ? 0 : -1;
    }
    glBindFramebuffer(GL_FRAMEBUFFER, previous);
    return result;
}
int wv_finish(struct wv *v, const char *extra)
{
    if (v->scanout_timer) {
        g_source_remove(v->scanout_timer);
        v->scanout_timer = 0;
    }
    if (v->source) {
        g_source_remove(v->source);
        v->source = 0;
    }
    replies_flush(v);
    while (!g_queue_is_empty(&v->shots))
        wv_screenshot_reply(v, 1);
    ir_unwatch(v);
    ptr_unwatch(v);
    touch_unwatch(v);
    if (v->name_id) {
        g_bus_unown_name(v->name_id);
        v->name_id = 0;
    }
    input_release(&v->input);
    ir_release(&v->ir);
    gint64 deadline = g_get_monotonic_time() + 3000000;
    while (v->input.pending && g_get_monotonic_time() < deadline) {
        while (g_main_context_iteration(NULL, FALSE)) {
        }
        g_usleep(2000);
    }
    fprintf(stderr,
            "frames=%u fenced=%u imports=%u maps=%u skipped=%u cursor_draws=%u input_calls=%u"
            " input_errors=%u ir_sent=%u ir_errors=%u pictures=%lu output_hz=%d%s\n",
            v->frames, v->fenced, v->imports, v->maps, v->skipped, v->cursor_draws, v->input.calls,
            v->input.errors, v->ir.sent, v->ir.errors, v->pictures, v->output_hz,
            extra ? extra : "");
    int failed = !v->frames || v->input.errors || v->input.pending;
    current(v);
    audio_close(&v->audio);
    pcm_out_stop(v->pcm); /* before media_close: its clock calls media_fill */
    v->pcm = NULL;
    capture_close(&v->capture);
    /* v is freed below: no "closed" callback may reach it */
    if (v->peer) {
        g_signal_handlers_disconnect_by_data(v->peer, v);
        g_dbus_connection_close_sync(v->peer, NULL, NULL);
        g_object_unref(v->peer);
    }
    if (v->input.bus) {
        g_signal_handlers_disconnect_by_data(v->input.bus, v);
        g_dbus_connection_close_sync(v->input.bus, NULL, NULL);
        g_object_unref(v->input.bus);
    }
    cursor_close(&v->cursor);
    ir_close(&v->ir);
    media_close(v->video.media);
    v->video.media = NULL;
    video_close(&v->video);
    frame_drop(v);
    turn_drop(v);
    unmap(v);
    drop_shadow(v);
    drop_image(v);
    glDeleteTextures(1, &v->source_tex);
    glDeleteTextures(1, &v->cache);
    glDeleteFramebuffers(1, &v->fbo);
    if (v->pbo)
        glDeleteBuffers(1, &v->pbo);
    glDeleteProgram(v->program);
    g_free(v->capture_path);
    g_free(v->restarting);
    g_free(v->qemu_owner);
    g_free(v->addr);
    g_free(v->cursor_path);
    g_free(v->video_path);
    g_free(v->ir_path);
    g_free(v->media_prefix);
    g_free(v->hwdec);
    g_free(v->audio_wav);
    g_free(v->media_wav);
    g_free(v);
    return failed;
}
