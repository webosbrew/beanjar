// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Mali native pixmaps (guest/layers/gl/src/mali-pixmap.c) on the host's Mesa, surfaceless: a
 * pixmap filled as libgm-wayland fills it, made an image the way GM_WL_UpdateWindow does
 * (eglGetProcAddress, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR, then glEGLImageTargetTexture2DOES and
 * eglDestroyImageKHR before drawing), with its pixels read back through a framebuffer. Prints "ok"
 * or what differed; exit status 2 when the host has no surfaceless GLES. */
#define MALI_PIXMAP_TEST
#include "../../../guest/layers/gl/src/mali-pixmap.c"
#include <GLES2/gl2ext.h>
#include <stdio.h>

static int mesa_load(void)
{
    if (mesa.proc && mesa.create && mesa.destroy)
        return 1;
    void *h = dlopen("libEGL.so.1", RTLD_NOW | RTLD_LOCAL);
    if (!h)
        return 0;
    mesa.proc = (__typeof__(mesa.proc))dlsym(h, "eglGetProcAddress");
    if (!mesa.proc)
        return 0;
    mesa.create = (PFNEGLCREATEIMAGEKHRPROC)mesa.proc("eglCreateImageKHR");
    mesa.destroy = (PFNEGLDESTROYIMAGEKHRPROC)mesa.proc("eglDestroyImageKHR");
    return mesa.create && mesa.destroy;
}

#define W     37 /* odd sizes: no row of the pixmap is a multiple of the alignment */
#define H     5
#define PITCH (W * 4 + 12)

static int failures;
#define CHECK(c, ...)                                                                              \
    do {                                                                                           \
        if (!(c)) {                                                                                \
            printf(__VA_ARGS__);                                                                   \
            printf("\n");                                                                          \
            failures++;                                                                            \
        }                                                                                          \
    } while (0)

static void put(uint8_t *pixmap, size_t offset, uint32_t v)
{
    memcpy(pixmap + offset, &v, sizeof v);
}

int main(void)
{
    if (!mesa_load())
        return printf("no libEGL\n"), 2;
    PFNEGLGETPLATFORMDISPLAYEXTPROC platform_display =
        (PFNEGLGETPLATFORMDISPLAYEXTPROC)mesa.proc("eglGetPlatformDisplayEXT");
    EGLDisplay dpy = platform_display ? platform_display(EGL_PLATFORM_SURFACELESS_MESA,
                                                         EGL_DEFAULT_DISPLAY, NULL)
                                      : EGL_NO_DISPLAY;
    if (dpy == EGL_NO_DISPLAY || !eglInitialize(dpy, NULL, NULL))
        return printf("no surfaceless display\n"), 2;
    static const EGLint cfg_attr[] = {EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_SURFACE_TYPE, 0,
                                      EGL_NONE};
    EGLConfig cfg;
    EGLint n = 0;
    static const EGLint ctx_attr[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
    eglBindAPI(EGL_OPENGL_ES_API);
    if (!eglChooseConfig(dpy, cfg_attr, &cfg, 1, &n) || n < 1)
        return printf("no config\n"), 2;
    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attr);
    if (ctx == EGL_NO_CONTEXT || !eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx))
        return printf("no surfaceless GLES context\n"), 2;

    /* The surface: ARGB8888 words, B, G, R, A bytes, a padded pitch */
    static uint8_t pixels[H * PITCH];
    for (int y = 0; y < H; y++)
        for (int x = 0; x < PITCH; x++)
            pixels[y * PITCH + x] = (uint8_t)(x < W * 4 ? 7 * x + 31 * y : 0xEE);
    uint8_t pixmap[108] = {0};
    put(pixmap, 0x00, H);
    put(pixmap, 0x0c, W);
    put(pixmap, 0x18, PITCH);
    put(pixmap, 0x24, 9);
    put(pixmap, 0x30, 9);
    put(pixmap, 0x34, 4);
    uint8_t *data = pixels;
    memcpy(pixmap + 0x38, &data, sizeof data);
    put(pixmap, 0x44, 0); /* data: a pointer of the process (frames 0 and 1) */

    /* libgm-wayland's form, flags 1 and a physical address of the graphics memory: a file stands
     * in for /dev/gfx, the surface at an offset that is not a page's (frame 2 onwards) */
    char gfx[] = "/tmp/check-mali-pixmap-XXXXXX";
    int gfx_fd = mkstemp(gfx);
    const uint32_t gfx_offset = 3 * 4096 + 256;
    if (gfx_fd < 0 || ftruncate(gfx_fd, 64 * 1024) != 0)
        return printf("no temporary file\n"), 2;
    gfx_path = gfx;

    /* GM_WL_UpdateWindow's sequence, with its texture bound */
    PFNEGLCREATEIMAGEKHRPROC create =
        (PFNEGLCREATEIMAGEKHRPROC)eglGetProcAddress("eglCreateImageKHR");
    PFNEGLDESTROYIMAGEKHRPROC destroy =
        (PFNEGLDESTROYIMAGEKHRPROC)eglGetProcAddress("eglDestroyImageKHR");
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC target =
        (PFNGLEGLIMAGETARGETTEXTURE2DOESPROC)eglGetProcAddress("glEGLImageTargetTexture2DOES");
    CHECK(create == (PFNEGLCREATEIMAGEKHRPROC)create_image, "eglCreateImageKHR is not ours");
    CHECK(destroy == (PFNEGLDESTROYIMAGEKHRPROC)destroy_image, "eglDestroyImageKHR is not ours");
    CHECK(target != NULL, "no glEGLImageTargetTexture2DOES");
    if (failures)
        return 1;
    GLuint tex;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 2);
    for (int frame = 0; frame < 4; frame++) {
        pixels[0] = (uint8_t)(10 * frame); /* B of the first pixel: a new image sees it */
        if (frame >= 2) {
            uint32_t phys = WEBOS_GFX_PHYS_BASE + gfx_offset;
            CHECK(pwrite(gfx_fd, pixels, sizeof pixels, gfx_offset) == (ssize_t)sizeof pixels,
                  "frame %d: write", frame);
            memset(&pixmap[0x38], 0, 8);
            put(pixmap, 0x38, phys);
            put(pixmap, 0x44, 1);
        }
        EGLImageKHR image = create(dpy, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR, pixmap, NULL);
        CHECK(eglGetError() == EGL_SUCCESS, "frame %d: an EGL error after the image", frame);
        CHECK(image != EGL_NO_IMAGE_KHR, "frame %d: no image", frame);
        CHECK(glGetError() == GL_NO_ERROR, "frame %d: a GL error after the image", frame);
        GLint bound = 0, align = 0;
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &bound);
        glGetIntegerv(GL_UNPACK_ALIGNMENT, &align);
        CHECK(bound == (GLint)tex, "frame %d: texture binding %d, not %u", frame, bound, tex);
        CHECK(align == 2, "frame %d: unpack alignment %d, not 2", frame, align);
        target(GL_TEXTURE_2D, image);
        CHECK(eglGetError() == EGL_SUCCESS, "frame %d: an EGL error after the target", frame);
        CHECK(glGetError() == GL_NO_ERROR, "frame %d: a GL error after the target", frame);
        CHECK(destroy(dpy, image), "frame %d: destroy failed", frame);

        GLuint fb;
        glGenFramebuffers(1, &fb);
        glBindFramebuffer(GL_FRAMEBUFFER, fb);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
        CHECK(glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE,
              "frame %d: framebuffer incomplete", frame);
        static uint8_t got[H * W * 4];
        glPixelStorei(GL_PACK_ALIGNMENT, 1);
        glReadPixels(0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, got);
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        glDeleteFramebuffers(1, &fb);
        int bad = 0;
        for (int y = 0; y < H && !bad; y++)
            for (int x = 0; x < W && !bad; x++) {
                const uint8_t *s = pixels + y * PITCH + 4 * x, *g = got + (y * W + x) * 4;
                if (g[0] != s[2] || g[1] != s[1] || g[2] != s[0] || g[3] != s[3]) {
                    printf("frame %d: pixel %d,%d is %02x%02x%02x%02x, not %02x%02x%02x%02x\n",
                           frame, x, y, g[0], g[1], g[2], g[3], s[2], s[1], s[0], s[3]);
                    bad = 1;
                }
            }
        failures += bad;
    }
    CHECK(images == NULL, "images left after destroy");

    /* A physical address outside the graphics memory fails, without a fault */
    put(pixmap, 0x38, WEBOS_GFX_PHYS_BASE - 4096);
    EGLImageKHR outside = create(dpy, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR, pixmap, NULL);
    CHECK(outside == EGL_NO_IMAGE_KHR, "a physical address outside the memory made an image");
    CHECK(eglGetError() != EGL_SUCCESS, "outside the memory: no EGL error");
    put(pixmap, 0x38, WEBOS_GFX_PHYS_BASE + gfx_offset);
    close(gfx_fd);
    unlink(gfx);

    /* Other targets are Mesa's, unchanged */
    GLuint src;
    glGenTextures(1, &src);
    glBindTexture(GL_TEXTURE_2D, src);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 4, 4, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    EGLImageKHR image =
        create(dpy, ctx, EGL_GL_TEXTURE_2D_KHR, (EGLClientBuffer)(uintptr_t)src, NULL);
    CHECK(image != EGL_NO_IMAGE_KHR, "Mesa's texture image failed");
    CHECK(images == NULL, "Mesa's image was taken as a pixmap's");
    destroy(dpy, image);

    /* A pixmap this library does not recognise goes to Mesa unchanged, which refuses it with an
     * EGL error, as the caller checks */
    put(pixmap, 0x34, 2);
    image = create(dpy, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR, pixmap, NULL);
    CHECK(image == EGL_NO_IMAGE_KHR, "a 16-bit pixmap made an image");
    CHECK(eglGetError() != EGL_SUCCESS, "a 16-bit pixmap failed without an EGL error");

    eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(dpy, ctx);
    eglTerminate(dpy);
    if (failures)
        return 1;
    printf("ok\n");
    return 0;
}
