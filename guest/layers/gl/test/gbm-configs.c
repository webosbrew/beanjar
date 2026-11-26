// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Which GBM formats Mesa's GBM EGL platform has window configs for, and whether a window surface
 * of each can be made: LG's compositors ask for theirs (Weston's gbm-format, eglfs_starfish's
 * output format) and fail without one (EGL_BAD_MATCH, "No EGLConfig matches"). Mesa patch 0006
 * keeps the formats' configs apart. Prints each window config's id and EGL_NATIVE_VISUAL_ID, then
 * the first RGBA 8888 config, one line per format, and "GBM-CONFIGS PASS" when ARGB8888, ABGR8888,
 * XRGB8888 and XBGR8888 each have a config whose window surface is made.
 *
 *   gbm-configs [DEVICE]     default /dev/dri/renderD128
 *
 * Built by guest/layers/gl/test/meson.build.
 */
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <fcntl.h>
#include <gbm.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static const struct {
    const char *name;
    EGLint format;
} formats[] = {
    {"ARGB8888", GBM_FORMAT_ARGB8888},
    {"ABGR8888", GBM_FORMAT_ABGR8888},
    {"XRGB8888", GBM_FORMAT_XRGB8888},
    {"XBGR8888", GBM_FORMAT_XBGR8888},
};

int main(int argc, char **argv)
{
    const char *path = argc > 1 ? argv[1] : "/dev/dri/renderD128";
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        perror(path);
        return 1;
    }
    struct gbm_device *gbm = gbm_create_device(fd);
    if (!gbm) {
        fprintf(stderr, "FAIL: gbm_create_device\n");
        return 1;
    }
    PFNEGLGETPLATFORMDISPLAYEXTPROC get_display =
        (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
    EGLDisplay dpy = get_display ? get_display(EGL_PLATFORM_GBM_KHR, gbm, NULL)
                                 : eglGetDisplay((EGLNativeDisplayType)gbm);
    if (dpy == EGL_NO_DISPLAY || !eglInitialize(dpy, NULL, NULL)) {
        fprintf(stderr, "FAIL: eglInitialize (0x%x)\n", eglGetError());
        return 1;
    }
    const EGLint want[] = {EGL_SURFACE_TYPE, EGL_WINDOW_BIT, EGL_RENDERABLE_TYPE,
                           EGL_OPENGL_ES2_BIT, EGL_NONE};
    EGLint n = 0;
    eglChooseConfig(dpy, want, NULL, 0, &n);
    EGLConfig *configs = calloc(n ? n : 1, sizeof(*configs));
    eglChooseConfig(dpy, want, configs, n, &n);
    for (EGLint i = 0; i < n; i++) {
        EGLint id, visual, r, g, b, a;
        eglGetConfigAttrib(dpy, configs[i], EGL_CONFIG_ID, &id);
        eglGetConfigAttrib(dpy, configs[i], EGL_NATIVE_VISUAL_ID, &visual);
        eglGetConfigAttrib(dpy, configs[i], EGL_RED_SIZE, &r);
        eglGetConfigAttrib(dpy, configs[i], EGL_GREEN_SIZE, &g);
        eglGetConfigAttrib(dpy, configs[i], EGL_BLUE_SIZE, &b);
        eglGetConfigAttrib(dpy, configs[i], EGL_ALPHA_SIZE, &a);
        printf("config %d rgba %d%d%d%d visual %.4s\n", id, r, g, b, a, (const char *)&visual);
    }
    /* What a client that takes the first alpha-8 config gets (LG's Qt EGLFS Starfish plugin):
     * the TV driver's format first where the gl layer names it (Mesa patch 0007) */
    for (EGLint i = 0; i < n; i++) {
        EGLint id, visual, r, g, b, a;
        eglGetConfigAttrib(dpy, configs[i], EGL_RED_SIZE, &r);
        eglGetConfigAttrib(dpy, configs[i], EGL_GREEN_SIZE, &g);
        eglGetConfigAttrib(dpy, configs[i], EGL_BLUE_SIZE, &b);
        eglGetConfigAttrib(dpy, configs[i], EGL_ALPHA_SIZE, &a);
        if (r != 8 || g != 8 || b != 8 || a != 8)
            continue;
        eglGetConfigAttrib(dpy, configs[i], EGL_CONFIG_ID, &id);
        eglGetConfigAttrib(dpy, configs[i], EGL_NATIVE_VISUAL_ID, &visual);
        printf("first rgba 8888 config: %d visual %.4s\n", id, (const char *)&visual);
        break;
    }
    int failed = 0;
    for (size_t f = 0; f < sizeof(formats) / sizeof(formats[0]); f++) {
        EGLint first = -1;
        for (EGLint i = 0; i < n && first < 0; i++) {
            EGLint visual;
            eglGetConfigAttrib(dpy, configs[i], EGL_NATIVE_VISUAL_ID, &visual);
            if (visual == formats[f].format)
                first = i;
        }
        if (first < 0) {
            printf("%s: no config\n", formats[f].name);
            failed = 1;
            continue;
        }
        EGLint id;
        eglGetConfigAttrib(dpy, configs[first], EGL_CONFIG_ID, &id);
        struct gbm_surface *gs = gbm_surface_create(gbm, 64, 64, formats[f].format,
                                                    GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING);
        EGLSurface s =
            gs ? eglCreateWindowSurface(dpy, configs[first], (EGLNativeWindowType)gs, NULL)
               : EGL_NO_SURFACE;
        if (s == EGL_NO_SURFACE) {
            printf("%s: config %d, window surface failed (0x%x)\n", formats[f].name, id,
                   eglGetError());
            failed = 1;
        } else {
            printf("%s: config %d, window surface made\n", formats[f].name, id);
            eglDestroySurface(dpy, s);
        }
        if (gs)
            gbm_surface_destroy(gs);
    }
    free(configs);
    eglTerminate(dpy);
    gbm_device_destroy(gbm);
    close(fd);
    puts(failed ? "GBM-CONFIGS FAIL" : "GBM-CONFIGS PASS");
    return failed;
}
