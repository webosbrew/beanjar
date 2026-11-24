// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libegl-kms.so: native window for the webOS 1 and 2 compositor on Mesa (GBM/KMS).
 *
 * On the TV, PVR's libEGL takes the compositor's window from libpvrGM_WSEGL, which asks libgm
 * for an OSD plane. libqstarfishegl just calls eglGetDisplay(EGL_DEFAULT_DISPLAY) and
 * eglCreateWindowSurface() with native windows 0xFFFF0001 and 0xFFFF0002 (GM window ids:
 * WEBOS and WEBOSFULLSCREEN). This library is LD_PRELOADed into the compositor only
 * (surface-manager-starfish: etc/init/surface-manager.override; webOS 5's surface-manager:
 * etc/systemd/system/surface-manager-daemon.service.d/emulator-gl.conf) and interposes:
 *
 *   eglGetDisplay(EGL_DEFAULT_DISPLAY)  -> eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm) on
 *                                          the KMS device (default /dev/dri/card0)
 *   eglChooseConfig                     -> same configs, ARGB8888 ones first
 *   eglCreateWindowSurface(win)         -> a gbm_surface the size of the KMS mode
 *   eglSwapBuffers                      -> then lock the front bo and show the OSD plane
 *
 * The OSD plane (frame buffer controller 0 of LG's display engine, libdile_gal in
 * guest/layers/soc/src) shows gm's WEBOS window, which on the TV PVR's EGL renders the compositor
 * into (libpvrGM_WSEGL: GM_GetWindowSurfaceInfo, GM_UpdateWindowWithBackSurfaceInfo), or gm's merge
 * of its visible windows (tvwin's factory UI, GM subtitles, teletext, MHEG) with that window's
 * picture among them by Z-order. Here the compositor renders into gbm buffers, so its windows
 * are registered with the display engine as GM's WEBOS (0xFFFF0001) and WEBOSFULLSCREEN
 * (0xFFFF0002) windows, the surfaces libgm reports for them, and each frame's framebuffer is
 * published for read-backs (tv.capture). Then the CRTC shows:
 *   - the plane showing a compositor window: that window's latest buffer, page-flipped as it is
 *     (the usual case, no copy);
 *   - the plane showing gm's merge: a composite in a gbm surface of our own, drawn with GLES in
 *     a context of our own: the merge's part below the compositor, the compositor's latest
 *     buffer (an EGLImage) where gm put its window, and the merge buffer on top, both read from
 *     the graphics memory (/dev/gfx) when gm changes them. A thread redraws it when gm changes
 *     the plane between the compositor's frames; the compositor's own frames redraw it too;
 *   - the plane hidden: nothing (transparent).
 * Without libdile_gal's display engine (no gm, or a libgm that fails) the scanout window is
 * page-flipped as before.
 *
 * Scanout: the gbm surface is ARGB8888 whenever the chosen config has alpha, because the
 * design composites video under the OSD on the host. virtio-gpu's primary plane only accepts
 * XRGB8888, so if the plane does not list ARGB8888 the same buffer is added as an XRGB8888
 * framebuffer: the alpha bytes are still in the scanout resource for a host viewer to use.
 *
 * Without a usable KMS device, or with EGL_KMS_PBUFFER=1, the default
 * display is Mesa's surfaceless platform and each window becomes a pbuffer.
 *
 * Environment:
 *   EGL_KMS_DEVICE   DRM device (default /dev/dri/card0)
 *   EGL_KMS_MODE     WxH to pick among the connector's modes (default: preferred mode)
 *   EGL_KMS_PBUFFER  1 = force the pbuffer fallback
 *   EGL_KMS_SIZE     WxH of the pbuffer fallback (default 1920x1080)
 *   EGL_KMS_DUMP     path prefix; every frame listed in EGL_KMS_DUMP_FRAMES (comma separated,
 *                    default "1") is read back and written as <prefix>w<window>-<n>.ppm, with
 *                    its alpha channel as <prefix>w<window>-<n>-alpha.pgm
 *   EGL_KMS_WINDOW   native window id to scan out (default: the first one created, or native
 *                    window 0, webOS 5's fb0 screen, when it comes)
 *   EGL_KMS_EXIT_AFTER  exit(0) after this many frames (tests)
 *   EGL_KMS_DEBUG    1 = log every swap
 * LD_PRELOAD is removed from the environment at load in the compositor (surface-manager*), so
 * processes it starts do not inherit it; elsewhere (webOS 5's surface-manager.sh on its way to the
 * compositor) the library does nothing at load.
 */
#define _GNU_SOURCE
#define EGL_NO_X11
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <gbm.h>
#include <GLES2/gl2ext.h>
#include <linux/futex.h>
#include <poll.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>

#include "../../../../include/webos-gfx.h"

#define EXPORT   __attribute__((visibility("default")))
#define LOG(...) fprintf(stderr, "[egl-kms] " __VA_ARGS__)
/* the OSD plane's messages go to syslog too (/var/log/messages): the job discards stderr */
#define OSDLOG(...)                                                                                \
    do {                                                                                           \
        LOG(__VA_ARGS__);                                                                          \
        syslog(LOG_INFO, "egl-kms: " __VA_ARGS__);                                                 \
    } while (0)

#define REAL(name)                                                                                 \
    static __typeof__(name) *real_##name;                                                          \
    if (!real_##name)                                                                              \
    real_##name = (__typeof__(name) *)dlsym(RTLD_NEXT, #name)

enum mode { M_NONE, M_KMS, M_PBUFFER };

/* libqstarfishegl creates one window per OSD plane it knows: 0xFFFF0001 (WEBOS, the UI) and
 * 0xFFFF0002 (WEBOSFULLSCREEN, used by its direct-rendering page flipper). Each gets its own
 * surface; the OSD plane says which is shown (display_update), else the scanout window (the
 * first one created, or EGL_KMS_WINDOW). */
#define MAXWIN 4
struct win {
    EGLNativeWindowType native;
    EGLSurface surf;
    EGLConfig cfg;
    struct gbm_surface *gs;
    struct gbm_bo *front; /* the latest frame */
    unsigned frames;
    int slot; /* compositor window slot of the display engine, -1 for none */
    int registered;
};

static struct {
    enum mode mode;
    EGLDisplay dpy;
    int w, h;
    /* KMS */
    int fd;
    struct gbm_device *gbm;
    uint32_t crtc, conn, plane_fmt_argb;
    drmModeModeInfo modeinfo;
    int crtc_set, flip_pending;
    /* windows */
    struct win win[MAXWIN];
    EGLNativeWindowType scanout; /* native id of the window on the CRTC */
    int scanout_set;
    int scanout_env; /* EGL_KMS_WINDOW chose it */
    EGLConfig last_cfg;
    unsigned frames;
    int debug;
} S = {.fd = -1};

static struct win *win_by_surf(EGLSurface surf)
{
    for (int i = 0; surf != EGL_NO_SURFACE && i < MAXWIN; i++)
        if (S.win[i].surf == surf)
            return &S.win[i];
    return NULL;
}

__attribute__((constructor)) static void init(void)
{
    /* Only the compositor takes it out of the environment. On webOS 5 the systemd unit's
     * LD_PRELOAD reaches surface-manager.sh first, a shell that exec()s the compositor after
     * appending libsystrim.so.3 from product.env: the shell (and the tools it runs) leave the
     * variable alone, and the compositor drops it as the Upstart job's does. */
    char exe[256];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    const char *base = exe;
    if (n > 0) {
        exe[n] = '\0';
        base = strrchr(exe, '/') ? strrchr(exe, '/') + 1 : exe;
    }
    if (n <= 0 || strncmp(base, "surface-manager", 15))
        return;
    unsetenv("LD_PRELOAD");
    S.debug = getenv("EGL_KMS_DEBUG") && atoi(getenv("EGL_KMS_DEBUG"));
    const char *w = getenv("EGL_KMS_WINDOW");
    if (w) {
        S.scanout = (EGLNativeWindowType)strtoul(w, NULL, 0);
        S.scanout_set = S.scanout_env = 1;
    }
}

/* ------------------------------------------------------------------------------------ KMS */

static int pick_mode(drmModeConnector *c, drmModeModeInfo *out)
{
    const char *want = getenv("EGL_KMS_MODE");
    int ww = 0, wh = 0, best = -1;
    if (want)
        sscanf(want, "%dx%d", &ww, &wh);
    for (int i = 0; i < c->count_modes; i++) {
        if (ww && c->modes[i].hdisplay == ww && c->modes[i].vdisplay == wh) {
            best = i;
            break;
        }
        if (best < 0 || (c->modes[i].type & DRM_MODE_TYPE_PREFERRED)) {
            if (!(best >= 0 && (c->modes[best].type & DRM_MODE_TYPE_PREFERRED)))
                best = i;
        }
    }
    if (best < 0)
        return -1;
    *out = c->modes[best];
    return 0;
}

static int plane_has_argb(int fd, uint32_t crtc_index)
{
    int ok = 0;
    drmSetClientCap(fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1);
    drmModePlaneRes *pr = drmModeGetPlaneResources(fd);
    for (uint32_t i = 0; pr && i < pr->count_planes && !ok; i++) {
        drmModePlane *p = drmModeGetPlane(fd, pr->planes[i]);
        if (!p)
            continue;
        if (p->possible_crtcs & (1u << crtc_index)) {
            /* primary plane? */
            drmModeObjectProperties *props =
                drmModeObjectGetProperties(fd, p->plane_id, DRM_MODE_OBJECT_PLANE);
            int primary = 0;
            for (uint32_t j = 0; props && j < props->count_props; j++) {
                drmModePropertyRes *pd = drmModeGetProperty(fd, props->props[j]);
                if (pd && !strcmp(pd->name, "type") &&
                    props->prop_values[j] == DRM_PLANE_TYPE_PRIMARY)
                    primary = 1;
                drmModeFreeProperty(pd);
            }
            drmModeFreeObjectProperties(props);
            for (uint32_t f = 0; primary && f < p->count_formats; f++)
                if (p->formats[f] == GBM_FORMAT_ARGB8888)
                    ok = 1;
        }
        drmModeFreePlane(p);
    }
    drmModeFreePlaneResources(pr);
    drmSetClientCap(fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 0);
    return ok;
}

static int kms_open(void)
{
    const char *dev = getenv("EGL_KMS_DEVICE");
    if (!dev)
        dev = "/dev/dri/card0";
    int fd = open(dev, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        LOG("%s: %s\n", dev, strerror(errno));
        return -1;
    }
    drmModeRes *res = drmModeGetResources(fd);
    if (!res) {
        LOG("%s: not a KMS device\n", dev);
        close(fd);
        return -1;
    }
    drmModeConnector *conn = NULL;
    for (int i = 0; i < res->count_connectors && !conn; i++) {
        drmModeConnector *c = drmModeGetConnector(fd, res->connectors[i]);
        if (c && c->connection == DRM_MODE_CONNECTED && c->count_modes > 0)
            conn = c;
        else
            drmModeFreeConnector(c);
    }
    if (!conn || pick_mode(conn, &S.modeinfo)) {
        LOG("%s: no connected connector with a mode\n", dev);
        drmModeFreeConnector(conn);
        drmModeFreeResources(res);
        close(fd);
        return -1;
    }
    /* a CRTC this connector can drive: the current one, else the first possible */
    uint32_t crtc = 0;
    int crtc_index = -1;
    drmModeEncoder *enc = conn->encoder_id ? drmModeGetEncoder(fd, conn->encoder_id) : NULL;
    if (enc && enc->crtc_id)
        crtc = enc->crtc_id;
    drmModeFreeEncoder(enc);
    for (int e = 0; !crtc && e < conn->count_encoders; e++) {
        enc = drmModeGetEncoder(fd, conn->encoders[e]);
        for (int i = 0; enc && i < res->count_crtcs; i++)
            if (enc->possible_crtcs & (1u << i)) {
                crtc = res->crtcs[i];
                break;
            }
        drmModeFreeEncoder(enc);
    }
    for (int i = 0; i < res->count_crtcs; i++)
        if (res->crtcs[i] == crtc)
            crtc_index = i;
    if (!crtc || crtc_index < 0) {
        LOG("%s: no CRTC for connector %u\n", dev, conn->connector_id);
        drmModeFreeConnector(conn);
        drmModeFreeResources(res);
        close(fd);
        return -1;
    }
    S.fd = fd;
    S.crtc = crtc;
    S.conn = conn->connector_id;
    S.w = S.modeinfo.hdisplay;
    S.h = S.modeinfo.vdisplay;
    S.plane_fmt_argb = plane_has_argb(fd, crtc_index);
    LOG("%s: connector %u, crtc %u, mode %s (%dx%d@%u), primary plane %s ARGB8888\n", dev, S.conn,
        crtc, S.modeinfo.name, S.w, S.h, S.modeinfo.vrefresh,
        S.plane_fmt_argb ? "takes" : "does not take");
    drmModeFreeConnector(conn);
    drmModeFreeResources(res);
    return 0;
}

struct fb {
    int fd;
    uint32_t id;
};

static void fb_destroy(struct gbm_bo *bo, void *data)
{
    struct fb *fb = data;
    if (fb->id)
        drmModeRmFB(fb->fd, fb->id);
    free(fb);
}

static uint32_t bo_fb(struct gbm_bo *bo)
{
    struct fb *fb = gbm_bo_get_user_data(bo);
    if (fb)
        return fb->id;
    fb = calloc(1, sizeof *fb);
    fb->fd = S.fd;
    uint32_t fmt = gbm_bo_get_format(bo);
    if (fmt == GBM_FORMAT_ARGB8888 && !S.plane_fmt_argb)
        fmt = GBM_FORMAT_XRGB8888;
    uint32_t handles[4] = {gbm_bo_get_handle(bo).u32}, pitches[4] = {gbm_bo_get_stride(bo)},
             offsets[4] = {0};
    if (drmModeAddFB2(S.fd, gbm_bo_get_width(bo), gbm_bo_get_height(bo), fmt, handles, pitches,
                      offsets, &fb->id, 0)) {
        LOG("drmModeAddFB2(%.4s): %s\n", (char *)&fmt, strerror(errno));
        fb->id = 0;
    }
    gbm_bo_set_user_data(bo, fb, fb_destroy);
    return fb->id;
}

static void flip_handler(int fd, unsigned seq, unsigned sec, unsigned usec, void *data)
{
    S.flip_pending = 0;
}

static void wait_flip(void)
{
    drmEventContext ev = {.version = 2, .page_flip_handler = flip_handler};
    while (S.flip_pending) {
        struct pollfd p = {.fd = S.fd, .events = POLLIN};
        int r = poll(&p, 1, 1000);
        if (r == 0) {
            LOG("page flip timed out\n");
            S.flip_pending = 0;
            break;
        }
        if (r < 0) {
            if (errno == EINTR)
                continue;
            break;
        }
        drmHandleEvent(S.fd, &ev);
    }
}

/* Show a framebuffer on the CRTC: the first time with SetCrtc, then page flips. */
static void kms_show(uint32_t id)
{
    if (!id)
        return;
    if (!S.crtc_set) {
        if (drmModeSetCrtc(S.fd, S.crtc, id, 0, 0, &S.conn, 1, &S.modeinfo))
            LOG("drmModeSetCrtc: %s\n", strerror(errno));
        else
            S.crtc_set = 1;
    } else if (drmModePageFlip(S.fd, S.crtc, id, DRM_MODE_PAGE_FLIP_EVENT, NULL) == 0) {
        S.flip_pending = 1;
        wait_flip();
    } else if (drmModeSetCrtc(S.fd, S.crtc, id, 0, 0, &S.conn, 1, &S.modeinfo)) {
        LOG("page flip and SetCrtc failed: %s\n", strerror(errno));
    }
}

/* --------------------------------------------------------------- the OSD plane (display engine) */

typedef EGLImageKHR (*create_image_fn)(EGLDisplay, EGLContext, EGLenum, EGLClientBuffer,
                                       const EGLint *);
typedef EGLBoolean (*destroy_image_fn)(EGLDisplay, EGLImageKHR);
typedef void (*image_target_fn)(GLenum, GLeglImageOES);

/* The OSD panel's coordinates: gm's display rectangles are on the panel, the graphic output
 * resolution gm sets (webos_gfx_osd's out_width/out_height: a UD panel's 3840x2160 on W18M), else
 * 1920x1080 (DILE_BOARDINFO; board.cmdline's hwopt of H13 and M14 alike). The screen shows the
 * whole panel. */
#define PANEL_W 1920
#define PANEL_H 1080
#define IMAGES  8

static struct {
    pthread_mutex_t lock; /* everything below and the CRTC */
    int on;               /* the display engine's functions and a registered window */
    webos_gfx_compositor_window_fn *reg;
    webos_gfx_compositor_frame_fn *frame;
    webos_gfx_osd_fn *osd;
    volatile uint32_t *seq;
    uint8_t *mem;
    int (*gm_surfaces)(uint32_t id, void *front, void *back);
    uint32_t seen_seq;
    struct webos_gfx_osd cur; /* the plane, as of seen_seq */
    int have_cur;
    int32_t engine; /* gm's pid when the windows were registered */
    /* on screen */
    struct gbm_bo *shown_bo; /* a compositor window's buffer, when that is what is shown */
    int layered;
    struct webos_gfx_osd shown; /* the plane state the composite was drawn for */
    struct gbm_bo *shown_comp;  /* the compositor buffer in it */
    /* the composite */
    EGLContext ctx;
    struct gbm_surface *gs;
    EGLSurface surf;
    struct gbm_bo *front;
    GLuint prog, tex[2]; /* the merge buffer, what is below the compositor */
    uint32_t tex_w[2], tex_h[2], tex_offset[2], tex_updates[2];
    GLint u_top, u_under, u_comp, u_has, u_disp, u_view, u_top_size, u_csrc, u_cdst, u_csize,
        u_calpha, u_screen, u_panel;
    struct {
        struct gbm_bo *bo;
        struct win *w;
        EGLImageKHR img;
        GLuint tex;
    } img[IMAGES];
    create_image_fn create_image;
    destroy_image_fn destroy_image;
    image_target_fn image_target;
    int broken; /* the composite failed to set up: show the compositor instead */
    int thread;
} D = {.lock = PTHREAD_MUTEX_INITIALIZER};

static struct win *win_by_slot(int slot)
{
    for (int i = 0; slot >= 0 && i < MAXWIN; i++)
        if (S.win[i].surf != EGL_NO_SURFACE && S.win[i].slot == slot)
            return &S.win[i];
    return NULL;
}

static struct win *win_by_native(EGLNativeWindowType native)
{
    for (int i = 0; i < MAXWIN; i++)
        if (S.win[i].surf != EGL_NO_SURFACE && S.win[i].native == native)
            return &S.win[i];
    return NULL;
}

static const char *vs_src = "attribute vec2 pos;\n"
                            "void main() { gl_Position = vec4(pos, 0.0, 1.0); }\n";
/* Screen pixel p (top-left origin, panel coordinates) -> the plane's frame buffer q through its
 * display and view rectangles -> the compositor's buffer through the merge's rectangles. gm's
 * layers are straight-alpha B,G,R,A bytes (uploaded as RGBA: swizzled here); the compositor's
 * buffer and the result are premultiplied, like the compositor's own scanout. */
static const char *fs_src = "#ifdef GL_FRAGMENT_PRECISION_HIGH\n"
                            "precision highp float;\n"
                            "#else\n"
                            "precision mediump float;\n"
                            "#endif\n"
                            "uniform sampler2D top, under, comp;\n"
                            "uniform vec3 has;\n" /* top, under, comp present */
                            "uniform vec4 disp, view, csrc, cdst;\n"
                            "uniform vec2 top_size, csize, screen, panel;\n"
                            "uniform float calpha;\n"
                            "vec4 layer(sampler2D t, vec2 uv) {\n"
                            "   vec4 c = texture2D(t, uv).bgra;\n"
                            "   return vec4(c.rgb * c.a, c.a);\n"
                            "}\n"
                            "bool inside(vec2 p, vec4 r) { return p.x >= r.x && p.y >= r.y && p.x "
                            "< r.x + r.z && p.y < r.y + r.w; }\n"
                            "void main() {\n"
                            "   vec2 p = vec2(gl_FragCoord.x, screen.y - gl_FragCoord.y) * panel / "
                            "screen;\n"
                            "   vec4 o = vec4(0.0);\n"
                            "   if (has.x > 0.0 && inside(p, disp)) {\n"
                            "      vec2 q = view.xy + (p - disp.xy) * view.zw / disp.zw;\n"
                            "      vec2 uv = q / top_size;\n"
                            "      if (has.y > 0.0) o = layer(under, uv);\n"
                            "      if (has.z > 0.0 && inside(q, cdst)) {\n"
                            "         vec4 c = texture2D(comp, (csrc.xy + (q - cdst.xy) * csrc.zw "
                            "/ cdst.zw) / csize) * calpha;\n"
                            "         o = c + (1.0 - c.a) * o;\n"
                            "      }\n"
                            "      vec4 t = layer(top, uv);\n"
                            "      o = t + (1.0 - t.a) * o;\n"
                            "   }\n"
                            "   gl_FragColor = o;\n"
                            "}\n";

static GLuint shader(GLenum type, const char *src)
{
    GLuint s = glCreateShader(type);
    GLint ok = 0;
    glShaderSource(s, 1, &src, NULL);
    glCompileShader(s);
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[512] = "";
        glGetShaderInfoLog(s, sizeof log, NULL, log);
        OSDLOG("OSD plane: shader: %s\n", log);
    }
    return s;
}

/* Our context and output surface, current on this thread; the caller restores its own. */
static int composite_begin(void)
{
    REAL(eglCreateContext);
    if (D.broken)
        return -1;
    if (!D.ctx) {
        static const EGLint attr[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
        D.create_image = (create_image_fn)eglGetProcAddress("eglCreateImageKHR");
        D.destroy_image = (destroy_image_fn)eglGetProcAddress("eglDestroyImageKHR");
        D.image_target = (image_target_fn)eglGetProcAddress("glEGLImageTargetTexture2DOES");
        D.gs = gbm_surface_create(S.gbm, S.w, S.h, GBM_FORMAT_ARGB8888,
                                  GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING);
        D.surf =
            D.gs ? eglCreatePlatformWindowSurface(S.dpy, S.last_cfg, D.gs, NULL) : EGL_NO_SURFACE;
        D.ctx = real_eglCreateContext(S.dpy, S.last_cfg, EGL_NO_CONTEXT, attr);
        if (!D.create_image || !D.image_target || D.surf == EGL_NO_SURFACE ||
            D.ctx == EGL_NO_CONTEXT || !eglMakeCurrent(S.dpy, D.surf, D.surf, D.ctx)) {
            OSDLOG("OSD plane: no composite (0x%x): gm's windows are not shown\n", eglGetError());
            D.broken = 1;
            return -1;
        }
        D.prog = glCreateProgram();
        glAttachShader(D.prog, shader(GL_VERTEX_SHADER, vs_src));
        glAttachShader(D.prog, shader(GL_FRAGMENT_SHADER, fs_src));
        glBindAttribLocation(D.prog, 0, "pos");
        glLinkProgram(D.prog);
        GLint ok = 0;
        glGetProgramiv(D.prog, GL_LINK_STATUS, &ok);
        if (!ok) {
            OSDLOG("OSD plane: the composite's program does not link\n");
            D.broken = 1;
            return -1;
        }
        glUseProgram(D.prog);
        glUniform1i(glGetUniformLocation(D.prog, "top"), 0);
        glUniform1i(glGetUniformLocation(D.prog, "under"), 1);
        glUniform1i(glGetUniformLocation(D.prog, "comp"), 2);
        D.u_has = glGetUniformLocation(D.prog, "has");
        D.u_disp = glGetUniformLocation(D.prog, "disp");
        D.u_view = glGetUniformLocation(D.prog, "view");
        D.u_top_size = glGetUniformLocation(D.prog, "top_size");
        D.u_csrc = glGetUniformLocation(D.prog, "csrc");
        D.u_cdst = glGetUniformLocation(D.prog, "cdst");
        D.u_csize = glGetUniformLocation(D.prog, "csize");
        D.u_calpha = glGetUniformLocation(D.prog, "calpha");
        D.u_screen = glGetUniformLocation(D.prog, "screen");
        D.u_panel = glGetUniformLocation(D.prog, "panel");
        glGenTextures(2, D.tex);
        for (int i = 0; i < 2; i++) {
            glBindTexture(GL_TEXTURE_2D, D.tex[i]);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        }
        OSDLOG("OSD plane: composite for gm's merged windows set up\n");
        return 0;
    }
    if (!eglMakeCurrent(S.dpy, D.surf, D.surf, D.ctx)) {
        OSDLOG("OSD plane: eglMakeCurrent: 0x%x\n", eglGetError());
        return -1;
    }
    return 0;
}

/* Surface [offset, pitch x height] of the graphics memory into texture i, when it changed. */
static void upload(int i, uint32_t offset, uint32_t pitch, uint32_t height, uint32_t updates)
{
    uint32_t w = pitch / 4;
    if (D.tex_offset[i] == offset && D.tex_updates[i] == updates && D.tex_w[i] == w &&
        D.tex_h[i] == height)
        return;
    glActiveTexture(GL_TEXTURE0 + i);
    glBindTexture(GL_TEXTURE_2D, D.tex[i]);
    if (D.tex_w[i] != w || D.tex_h[i] != height)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, height, 0, GL_RGBA, GL_UNSIGNED_BYTE,
                     D.mem + offset);
    else
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, height, GL_RGBA, GL_UNSIGNED_BYTE,
                        D.mem + offset);
    D.tex_w[i] = w;
    D.tex_h[i] = height;
    D.tex_offset[i] = offset;
    D.tex_updates[i] = updates;
}

static GLuint image_texture(struct win *w, struct gbm_bo *bo)
{
    int free_slot = -1;
    for (int i = 0; i < IMAGES; i++) {
        if (D.img[i].bo == bo)
            return D.img[i].tex;
        if (!D.img[i].bo && free_slot < 0)
            free_slot = i;
    }
    if (free_slot < 0)
        return 0;
    EGLImageKHR img =
        D.create_image(S.dpy, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR, (EGLClientBuffer)bo, NULL);
    if (img == EGL_NO_IMAGE_KHR) {
        OSDLOG("OSD plane: eglCreateImageKHR: 0x%x\n", eglGetError());
        return 0;
    }
    GLuint tex;
    glGenTextures(1, &tex);
    glActiveTexture(GL_TEXTURE2);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    D.image_target(GL_TEXTURE_2D, (GLeglImageOES)img);
    D.img[free_slot].bo = bo;
    D.img[free_slot].w = w;
    D.img[free_slot].img = img;
    D.img[free_slot].tex = tex;
    return tex;
}

static void rect4(GLint u, struct webos_gfx_rect r, float dw, float dh)
{
    glUniform4f(u, r.x, r.y, r.w ? r.w : dw, r.h ? r.h : dh);
}

/* Draw the plane (o) with the compositor's buffer cbo in it into our surface and show it. */
static void composite(const struct webos_gfx_osd *o, struct win *cw, struct gbm_bo *cbo)
{
    EGLDisplay pd = eglGetCurrentDisplay();
    EGLContext pc = eglGetCurrentContext();
    EGLSurface pdraw = eglGetCurrentSurface(EGL_DRAW), pread = eglGetCurrentSurface(EGL_READ);
    if (composite_begin()) {
        if (pc != EGL_NO_CONTEXT)
            eglMakeCurrent(pd, pdraw, pread, pc);
        return;
    }
    int top = o->visible && o->has_fb && o->format == 0 && o->pitch && o->height &&
              (uint64_t)o->offset + (uint64_t)o->pitch * o->height <= WEBOS_GFX_MEM_SIZE;
    int under = top && o->under &&
                (uint64_t)o->under + (uint64_t)o->pitch * o->height <= WEBOS_GFX_MEM_SIZE;
    GLuint ctex = top && o->comp_window >= 0 && cbo ? image_texture(cw, cbo) : 0;
    glViewport(0, 0, S.w, S.h);
    glClearColor(0, 0, 0, 0);
    glClear(GL_COLOR_BUFFER_BIT);
    if (top) {
        glUseProgram(D.prog);
        upload(0, o->offset, o->pitch, o->height, o->updates);
        if (under)
            upload(1, o->under, o->pitch, o->height, o->updates);
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_2D, D.tex[0]);
        glActiveTexture(GL_TEXTURE1);
        glBindTexture(GL_TEXTURE_2D, D.tex[1]);
        glActiveTexture(GL_TEXTURE2);
        glBindTexture(GL_TEXTURE_2D, ctex);
        glUniform3f(D.u_has, 1, under, ctex != 0);
        float pw = o->out_width && o->out_height ? (float)o->out_width : PANEL_W;
        float ph = o->out_width && o->out_height ? (float)o->out_height : PANEL_H;
        rect4(D.u_disp, o->disp, pw, ph);
        glUniform2f(D.u_panel, pw, ph);
        rect4(D.u_view, o->view, o->width, o->height);
        glUniform2f(D.u_top_size, (float)(o->pitch / 4), (float)o->height);
        rect4(D.u_csrc, o->comp_src, S.w, S.h);
        rect4(D.u_cdst, o->comp_dst, o->width, o->height);
        glUniform2f(D.u_csize, (float)S.w, (float)S.h);
        glUniform1f(D.u_calpha, o->comp_alpha / 255.0f);
        glUniform2f(D.u_screen, (float)S.w, (float)S.h);
        static const GLfloat quad[] = {-1, -1, 1, -1, -1, 1, 1, 1};
        glBindBuffer(GL_ARRAY_BUFFER, 0);
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, quad);
        glEnableVertexAttribArray(0);
        glDisable(GL_BLEND);
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    }
    REAL(eglSwapBuffers);
    real_eglSwapBuffers(S.dpy, D.surf);
    if (pc != EGL_NO_CONTEXT)
        eglMakeCurrent(pd, pdraw, pread, pc);
    else
        eglMakeCurrent(S.dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    struct gbm_bo *bo = gbm_surface_lock_front_buffer(D.gs);
    if (!bo)
        return;
    kms_show(bo_fb(bo));
    if (D.front)
        gbm_surface_release_buffer(D.gs, D.front);
    D.front = bo;
    D.shown_bo = NULL;
    D.layered = 1;
    D.shown = *o;
    D.shown_comp = ctex ? cbo : NULL;
}

/* Show the OSD plane as it is now. trigger: the window that has just swapped, if any. Under D.lock.
 */
static void display_update(struct win *trigger)
{
    struct win *direct = NULL;
    if (D.on) {
        uint32_t seq = *D.seq;
        if (!D.have_cur || seq != D.seen_seq) {
            __sync_synchronize();
            D.have_cur = D.osd(&D.cur) == 0;
            D.seen_seq = seq;
            /* a new gm has new windows: ask libgm again on the next frames */
            if (D.have_cur && D.cur.engine && D.engine && D.cur.engine != D.engine) {
                OSDLOG("OSD plane: gm restarted (pid %d): registering the windows again\n",
                       D.cur.engine);
                for (int i = 0; i < MAXWIN; i++)
                    if (S.win[i].registered == 1)
                        S.win[i].registered = 0;
                D.engine = D.cur.engine;
            }
        }
    }
    const struct webos_gfx_osd *o = &D.cur;
    if (!D.on || !D.have_cur || !o->has_fb || D.broken) {
        direct = win_by_native(S.scanout);
    } else if (o->visible && o->window >= 0) {
        direct = win_by_slot(o->window);
        if (!direct)
            direct = win_by_native(S.scanout);
    }
    if (direct) {
        if (direct->front && direct->front != D.shown_bo) {
            kms_show(bo_fb(direct->front));
            D.shown_bo = direct->front;
            D.layered = 0;
        }
        return;
    }
    struct win *cw = o->comp_window >= 0 ? win_by_slot(o->comp_window) : NULL;
    struct gbm_bo *cbo = cw ? cw->front : NULL;
    if (D.layered && !memcmp(o, &D.shown, sizeof *o) && cbo == D.shown_comp &&
        !(trigger && trigger == cw))
        return;
    composite(o, cw, cbo);
}

/* gm changing the plane between the compositor's frames */
static void *display_thread(void *unused)
{
    (void)unused;
    for (;;) {
        uint32_t seq = *D.seq;
        pthread_mutex_lock(&D.lock);
        display_update(NULL);
        pthread_mutex_unlock(&D.lock);
        struct timespec t = {1, 0};
        syscall(SYS_futex, D.seq, FUTEX_WAIT, seq, &t, NULL, 0);
    }
    return NULL;
}

/* Tell the display engine that window w is gm's window (libgm's surfaces for its id). Under D.lock.
 */
static void display_register(struct win *w)
{
    /* GM_GetWindowSurfaceInfo: DILE_GAL_SURFACE_INFO_T-like records of 1064 bytes, offset first */
    static uint32_t front[266], back[266];
    if (w->slot < 0 || w->registered || S.mode != M_KMS)
        return;
    if (!D.reg) {
        /* loaded by the starfish platform plugin (libqstarfishegl: libgm, libim -> libdile_gal),
         * in its own scope: found by name, not through the global one */
        void *gal = dlopen("libdile_gal.so.1", RTLD_NOW | RTLD_NOLOAD);
        void *gm = dlopen("libgm.so.1", RTLD_NOW | RTLD_NOLOAD);
        if (!gal || !gm) {
            if (w->frames < 2)
                OSDLOG("OSD plane: libdile_gal and libgm are not loaded yet\n");
            return;
        }
        D.reg = (webos_gfx_compositor_window_fn *)dlsym(gal, "webos_gfx_compositor_window");
        D.frame = (webos_gfx_compositor_frame_fn *)dlsym(gal, "webos_gfx_compositor_frame");
        D.osd = (webos_gfx_osd_fn *)dlsym(gal, "webos_gfx_osd");
        webos_gfx_changes_fn *changes = (webos_gfx_changes_fn *)dlsym(gal, "webos_gfx_changes");
        webos_gfx_memory_fn *memory = (webos_gfx_memory_fn *)dlsym(gal, "webos_gfx_memory");
        D.gm_surfaces = (int (*)(uint32_t, void *, void *))dlsym(gm, "GM_GetWindowSurfaceInfo");
        D.seq = changes ? changes() : NULL;
        D.mem = memory ? memory() : NULL;
        if (!D.reg || !D.frame || !D.osd || !D.seq || !D.mem || !D.gm_surfaces) {
            OSDLOG("OSD plane: no display engine (libdile_gal, libgm): the compositor alone is "
                   "shown\n");
            D.reg = NULL;
            w->registered = -1;
            return;
        }
    }
    memset(front, 0, sizeof front);
    memset(back, 0, sizeof back);
    int rc = D.gm_surfaces((uint32_t)w->native, front, back);
    if (rc || !front[0] || !back[0]) {
        if (w->frames < 2)
            OSDLOG("GM_GetWindowSurfaceInfo(%#lx) = %d: will retry\n", (unsigned long)w->native,
                   rc);
        return;
    }
    D.reg(w->slot, front[0], back[0]);
    w->registered = 1;
    struct webos_gfx_osd o;
    if (!D.engine && D.osd(&o) == 0)
        D.engine = o.engine;
    OSDLOG("window %#lx is gm's window with surfaces 0x%x, 0x%x\n", (unsigned long)w->native,
           front[0], back[0]);
    if (!D.on) {
        pthread_t t;
        D.on = 1;
        if (pthread_create(&t, NULL, display_thread, NULL) == 0)
            pthread_detach(t);
    }
}

static void kms_present(struct win *w)
{
    struct gbm_bo *bo = gbm_surface_lock_front_buffer(w->gs);
    if (!bo) {
        OSDLOG("gbm_surface_lock_front_buffer failed\n");
        return;
    }
    pthread_mutex_lock(&D.lock);
    if (!w->registered && (w->frames < 3 || w->frames % 64 == 0))
        display_register(w);
    struct gbm_bo *old = w->front;
    w->front = bo;
    uint32_t id = bo_fb(bo);
    if (D.on && w->registered == 1)
        D.frame(w->slot, id);
    display_update(w);
    /* the previous frame is off screen now, and out of the composite */
    if (old && old != bo)
        gbm_surface_release_buffer(w->gs, old);
    pthread_mutex_unlock(&D.lock);
}

/* ------------------------------------------------------------------------------ frame dump */

static int dump_wanted(unsigned n)
{
    const char *l = getenv("EGL_KMS_DUMP_FRAMES");
    if (!l)
        return n == 1;
    for (const char *p = l; *p;) {
        if ((unsigned)atoi(p) == n)
            return 1;
        p = strchr(p, ',');
        if (!p)
            break;
        p++;
    }
    return 0;
}

static void dump_frame(struct win *w, unsigned n)
{
    const char *prefix = getenv("EGL_KMS_DUMP");
    if (!prefix || !dump_wanted(n))
        return;
    unsigned char *px = malloc((size_t)S.w * S.h * 4);
    if (!px)
        return;
    /* the window's own buffer, whatever FBO the scene graph left bound */
    GLint fbo = 0;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &fbo);
    if (fbo)
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glReadPixels(0, 0, S.w, S.h, GL_RGBA, GL_UNSIGNED_BYTE, px);
    if (fbo)
        glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    char name[512];
    int wi = (int)(w - S.win);
    snprintf(name, sizeof name, "%sw%d-%u.ppm", prefix, wi, n);
    FILE *f = fopen(name, "wb");
    if (f) {
        fprintf(f, "P6\n%d %d\n255\n", S.w, S.h);
        for (int y = S.h - 1; y >= 0; y--)
            for (int x = 0; x < S.w; x++)
                fwrite(px + 4 * ((size_t)y * S.w + x), 1, 3, f);
        fclose(f);
        /* alpha as a separate greyscale image: shows what a host compositor would blend */
        snprintf(name, sizeof name, "%sw%d-%u-alpha.pgm", prefix, wi, n);
        if ((f = fopen(name, "wb"))) {
            fprintf(f, "P5\n%d %d\n255\n", S.w, S.h);
            for (int y = S.h - 1; y >= 0; y--)
                for (int x = 0; x < S.w; x++)
                    fputc(px[4 * ((size_t)y * S.w + x) + 3], f);
            fclose(f);
        }
        LOG("window %#lx frame %u written to %sw%d-%u.ppm\n", (unsigned long)w->native, n, prefix,
            wi, n);
    }
    free(px);
}

/* ----------------------------------------------------------------------------- EGL entry */

EXPORT EGLDisplay eglGetDisplay(EGLNativeDisplayType native)
{
    REAL(eglGetDisplay);
    if (native != EGL_DEFAULT_DISPLAY)
        return real_eglGetDisplay(native);
    if (S.mode != M_NONE)
        return S.dpy;

    const char *pb = getenv("EGL_KMS_PBUFFER");
    if (!(pb && atoi(pb)) && kms_open() == 0) {
        S.gbm = gbm_create_device(S.fd);
        S.dpy = S.gbm ? eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, S.gbm, NULL) : EGL_NO_DISPLAY;
        if (S.dpy != EGL_NO_DISPLAY) {
            S.mode = M_KMS;
            return S.dpy;
        }
        LOG("GBM display on the KMS device failed; using a pbuffer\n");
        if (S.gbm)
            gbm_device_destroy(S.gbm);
        S.gbm = NULL;
        close(S.fd);
        S.fd = -1;
    }
    const char *sz = getenv("EGL_KMS_SIZE");
    S.w = 1920;
    S.h = 1080;
    if (sz)
        sscanf(sz, "%dx%d", &S.w, &S.h);
    S.dpy = eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL);
    S.mode = M_PBUFFER;
    LOG("no KMS: surfaceless display, windows are %dx%d pbuffers\n", S.w, S.h);
    return S.dpy;
}

static EGLint cfg_attr(EGLDisplay dpy, EGLConfig c, EGLint a)
{
    EGLint v = 0;
    eglGetConfigAttrib(dpy, c, a, &v);
    return v;
}

EXPORT EGLBoolean eglChooseConfig(EGLDisplay dpy, const EGLint *attr, EGLConfig *cfgs, EGLint size,
                                  EGLint *n)
{
    REAL(eglChooseConfig);
    if (S.mode == M_NONE || dpy != S.dpy)
        return real_eglChooseConfig(dpy, attr, cfgs, size, n);

    EGLint a[128];
    int i = 0;
    for (; attr && attr[i] != EGL_NONE && i < 126; i += 2) {
        a[i] = attr[i];
        a[i + 1] = attr[i + 1];
        if (attr[i] == EGL_SURFACE_TYPE && S.mode == M_PBUFFER && (attr[i + 1] & EGL_WINDOW_BIT))
            a[i + 1] = (attr[i + 1] & ~EGL_WINDOW_BIT) | EGL_PBUFFER_BIT;
    }
    a[i] = EGL_NONE;

    /* all matches, then ARGB8888 (native visual on GBM; 8888 elsewhere) first, stable */
    EGLint total = 0;
    if (!real_eglChooseConfig(dpy, a, NULL, 0, &total))
        return EGL_FALSE;
    EGLConfig *all = calloc(total ? total : 1, sizeof *all);
    if (!real_eglChooseConfig(dpy, a, all, total, &total)) {
        free(all);
        return EGL_FALSE;
    }
    EGLint k = 0;
    for (int pass = 0; pass < 2; pass++)
        for (EGLint j = 0; j < total; j++) {
            int argb = S.mode == M_KMS
                           ? cfg_attr(dpy, all[j], EGL_NATIVE_VISUAL_ID) == GBM_FORMAT_ARGB8888
                           : cfg_attr(dpy, all[j], EGL_ALPHA_SIZE) == 8 &&
                                 cfg_attr(dpy, all[j], EGL_RED_SIZE) == 8;
            if (argb == !pass) {
                if (cfgs && k < size)
                    cfgs[k] = all[j];
                k++;
            }
        }
    if (cfgs && k > size)
        k = size;
    *n = k;
    if (cfgs && k > 0) {
        EGLint vis = cfg_attr(dpy, cfgs[0], EGL_NATIVE_VISUAL_ID);
        LOG("eglChooseConfig: %d configs, first r%dg%db%da%d visual %.4s\n", k,
            cfg_attr(dpy, cfgs[0], EGL_RED_SIZE), cfg_attr(dpy, cfgs[0], EGL_GREEN_SIZE),
            cfg_attr(dpy, cfgs[0], EGL_BLUE_SIZE), cfg_attr(dpy, cfgs[0], EGL_ALPHA_SIZE),
            vis ? (char *)&vis : "none");
    }
    free(all);
    return EGL_TRUE;
}

EXPORT EGLSurface eglCreateWindowSurface(EGLDisplay dpy, EGLConfig cfg, EGLNativeWindowType win,
                                         const EGLint *attr)
{
    REAL(eglCreateWindowSurface);
    if (S.mode == M_NONE || dpy != S.dpy)
        return real_eglCreateWindowSurface(dpy, cfg, win, attr);
    struct win *w = NULL;
    for (int i = 0; i < MAXWIN && !w; i++)
        if (S.win[i].surf == EGL_NO_SURFACE && !S.win[i].gs)
            w = &S.win[i];
    if (!w) {
        LOG("eglCreateWindowSurface(win=%#lx): too many windows\n", (unsigned long)win);
        return EGL_NO_SURFACE;
    }
    /* The first window, unless EGL_KMS_WINDOW says; on webOS 5 the QPA's native windows are its
     * screens' QT_QPA_EGLFS_FB indexes, and fb0's (0) is the OSD whichever it creates first */
    if (!S.scanout_env && (!S.scanout_set || win == (EGLNativeWindowType)0)) {
        S.scanout = win;
        S.scanout_set = 1;
    }
    w->native = win;
    /* gm's public ids of its WEBOS and WEBOSFULLSCREEN windows (libgm's GM_IsWindowIDValid) */
    w->slot = (unsigned long)win == 0xFFFF0001ul ? 0 : (unsigned long)win == 0xFFFF0002ul ? 1 : -1;
    if (S.mode == M_KMS) {
        uint32_t fmt = cfg_attr(dpy, cfg, EGL_NATIVE_VISUAL_ID);
        w->gs = gbm_surface_create(S.gbm, S.w, S.h, fmt, GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING);
        if (!w->gs) {
            LOG("gbm_surface_create(%dx%d %.4s) failed\n", S.w, S.h, (char *)&fmt);
            return EGL_NO_SURFACE;
        }
        w->surf = eglCreatePlatformWindowSurface(dpy, cfg, w->gs, NULL);
        LOG("eglCreateWindowSurface(win=%#lx) -> gbm surface %dx%d %.4s%s: %p\n",
            (unsigned long)win, S.w, S.h, (char *)&fmt, win == S.scanout ? " (scanout)" : "",
            w->surf);
    } else {
        const EGLint pattr[] = {EGL_WIDTH, S.w, EGL_HEIGHT, S.h, EGL_NONE};
        w->surf = eglCreatePbufferSurface(dpy, cfg, pattr);
        LOG("eglCreateWindowSurface(win=%#lx) -> pbuffer %dx%d%s: %p\n", (unsigned long)win, S.w,
            S.h, win == S.scanout ? " (scanout)" : "", w->surf);
    }
    if (w->surf == EGL_NO_SURFACE) {
        LOG("surface creation failed: 0x%x\n", eglGetError());
        if (w->gs)
            gbm_surface_destroy(w->gs);
        memset(w, 0, sizeof *w);
        return EGL_NO_SURFACE;
    }
    w->cfg = S.last_cfg = cfg;
    return w->surf;
}

EXPORT EGLContext eglCreateContext(EGLDisplay dpy, EGLConfig cfg, EGLContext share,
                                   const EGLint *attr)
{
    REAL(eglCreateContext);
    /* Qt may choose the context's config separately from the surface's; Mesa then refuses
     * eglMakeCurrent with EGL_BAD_MATCH if they differ in a way it cares about. Use the config
     * of the window surfaces. */
    if (S.mode != M_NONE && dpy == S.dpy && S.last_cfg)
        cfg = S.last_cfg;
    return real_eglCreateContext(dpy, cfg, share, attr);
}

EXPORT EGLBoolean eglSwapBuffers(EGLDisplay dpy, EGLSurface surf)
{
    REAL(eglSwapBuffers);
    struct win *w = S.mode == M_NONE ? NULL : win_by_surf(surf);
    if (!w)
        return real_eglSwapBuffers(dpy, surf);
    w->frames++;
    if (w->frames == 1)
        LOG("first frame from window %#lx%s\n", (unsigned long)w->native,
            w->native == S.scanout ? " (the scanout window)" : "");
    if (S.mode == M_PBUFFER || getenv("EGL_KMS_DUMP"))
        dump_frame(w, w->frames);
    EGLBoolean ok = real_eglSwapBuffers(dpy, surf);
    if (ok && S.mode == M_KMS)
        kms_present(w);
    if (S.debug)
        LOG("swap window %#lx frame %u ok=%d\n", (unsigned long)w->native, w->frames, ok);
    const char *ex = getenv("EGL_KMS_EXIT_AFTER");
    if (w->native == S.scanout && ex && w->frames >= (unsigned)atoi(ex)) {
        LOG("exiting after %u frames (EGL_KMS_EXIT_AFTER)\n", w->frames);
        fflush(stderr);
        _exit(0);
    }
    return ok;
}

EXPORT EGLBoolean eglDestroySurface(EGLDisplay dpy, EGLSurface surf)
{
    REAL(eglDestroySurface);
    EGLBoolean ok = real_eglDestroySurface(dpy, surf);
    struct win *w = S.mode == M_NONE ? NULL : win_by_surf(surf);
    if (w) {
        pthread_mutex_lock(&D.lock);
        for (int i = 0; i < IMAGES; i++)
            if (D.img[i].w == w) {
                if (D.destroy_image)
                    D.destroy_image(S.dpy, D.img[i].img);
                memset(&D.img[i], 0, sizeof D.img[i]); /* the texture stays: no context here */
            }
        if (D.shown_bo == w->front)
            D.shown_bo = NULL;
        if (D.shown_comp == w->front)
            D.shown_comp = NULL;
        if (w->front)
            gbm_surface_release_buffer(w->gs, w->front);
        if (w->gs)
            gbm_surface_destroy(w->gs);
        memset(w, 0, sizeof *w);
        w->slot = -1;
        pthread_mutex_unlock(&D.lock);
    }
    return ok;
}
