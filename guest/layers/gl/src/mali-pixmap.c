// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The vendor's one GL library (gl_vendor_gl) with Mali's native pixmaps: eglCreateImageKHR with
 * target EGL_NATIVE_PIXMAP_KHR and a Mali pixmap, a CPU-visible buffer the caller fills.
 *
 * libgm-wayland (GM_WL_UpdateWindow, the window that hybridtv-manager's BML browser draws in)
 * blits its window into a GAL surface in /dev/gfx memory and then, on each update: eglMakeCurrent;
 * eglCreateImageKHR(dpy, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR, &pixmap, NULL) with the function
 * from eglGetProcAddress; glEGLImageTargetTexture2DOES on its texture; eglDestroyImageKHR; two
 * triangles; eglSwapBuffers. Any EGL or GL error fails the update. Mesa takes native pixmaps on
 * X11 only.
 *
 * Here an image of a Mali pixmap is the image of a GL texture (EGL_GL_TEXTURE_2D_KHR) of the
 * current context, filled from the pixmap's memory when the image is made: libgm-wayland makes a
 * new image of the same pixmap for every update. The texture is kept from one image to the next,
 * with a copy of the pixels it was last given, and only the rows that changed since are converted
 * and uploaded (glTexSubImage2D). An update must be quick: legacy-broadcast's caption window
 * (1366x768) is updated through libgm-wayland by a task its callers wait for only 300 ms
 * (API_SGL_UpdateScreen), and converting and uploading the whole window every time took a good
 * part of that on a slow host; a caption changes a few rows. When the kept texture cannot be used,
 * the image gets a texture of its own, which destroying the image deletes.
 *
 * The pixmap as libgm-wayland fills it (W22O 43.21.89, 108 bytes; height before width, as in
 * Mali's fbdev_pixmap):
 *   +0x00 height  +0x0c width  +0x18 pitch (bytes)  +0x24 9  +0x30 9  +0x34 4 (bytes a pixel)
 *   +0x38 data  +0x44 flags; the rest 0.
 * libgm-wayland sets flags 1 and data to the GAL surface's physical address, not its mapping: the
 * driver maps the memory itself. Here physical addresses are the emulator's graphics memory
 * (include/webos-gfx.h: /dev/gfx at WEBOS_GFX_PHYS_BASE), which this library maps for as long as
 * it copies; with flags 0, data is taken as a pointer of the process.
 * The pixels are the GAL's ARGB8888, little-endian 32-bit words (B, G, R, A bytes).
 *
 * Like the empty stand-in (pvr-stub.c) the library NEEDs Mesa's libraries, so everything else
 * comes from Mesa. It defines eglGetProcAddress, which gives these two for their names and Mesa's
 * for the rest; any other image goes to Mesa unchanged.
 *
 * Built twice by the gl layer: as the vendor's one GL library (o22's libmali.so.0, which the
 * firmware NEEDs), and on chips with the standard EGL ABI (gl_standard_abi) as libEGL.so.1 itself,
 * the name libgm-wayland NEEDs, in front of Mesa's libEGL under another file name
 * (MESA_EGL libEGL-mesa.so.1), as egl-window-formats.c is on m16p. With
 * /etc/default/mali-pixmap-debug present, every pixmap image is logged to syslog (PmLog). */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#include "../../../../include/webos-gfx.h"

#define EXPORT __attribute__((visibility("default")))

/* The fields of a Mali pixmap this library reads (offsets above). */
#define PIXMAP_HEIGHT 0x00
#define PIXMAP_WIDTH  0x0c
#define PIXMAP_PITCH  0x18
#define PIXMAP_BPP    0x34
#define PIXMAP_DATA   0x38
#define PIXMAP_FLAGS  0x44
#define PIXMAP_PHYS   1u    /* flags: data is a physical address */
#define PIXMAP_MAX    16384 /* a sanity bound on either side */

static struct {
    PFNEGLCREATEIMAGEKHRPROC create;
    PFNEGLDESTROYIMAGEKHRPROC destroy;
    __typeof__(eglGetProcAddress) *proc;
} mesa;

static int mesa_load(void); /* the unit test has its own */
#ifndef MALI_PIXMAP_TEST
#ifndef MESA_EGL
#define MESA_EGL "libEGL.so.1"
#endif
/* Mesa's own definitions: dlsym on its handle searches Mesa first (a global lookup would find this
 * library's eglGetProcAddress again). As the libEGL.so.1 wrapper, MESA_EGL is libEGL-mesa.so.1. */
static int mesa_load(void)
{
    if (mesa.proc && mesa.create && mesa.destroy)
        return 1;
    void *h = dlopen(MESA_EGL, RTLD_LAZY | RTLD_LOCAL | RTLD_NOLOAD);
    if (!h)
        return 0;
    mesa.proc = (__typeof__(mesa.proc))dlsym(h, "eglGetProcAddress");
    if (!mesa.proc)
        return 0;
    mesa.create = (PFNEGLCREATEIMAGEKHRPROC)mesa.proc("eglCreateImageKHR");
    mesa.destroy = (PFNEGLDESTROYIMAGEKHRPROC)mesa.proc("eglDestroyImageKHR");
    return mesa.create && mesa.destroy;
}
#endif

static int debug = -1;

static void trace(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void trace(const char *fmt, ...)
{
    if (debug < 0)
        debug = access("/etc/default/mali-pixmap-debug", F_OK) == 0;
    if (!debug)
        return;
    va_list ap;
    va_start(ap, fmt);
    vsyslog(LOG_INFO, fmt, ap);
    va_end(ap);
}

/* The images made here and their textures (0: a kept texture, see below); a handful at most live
 * at once. */
struct pixmap_image {
    EGLImageKHR image;
    EGLDisplay dpy;
    GLuint texture;
    struct pixmap_image *next;
};
static struct pixmap_image *images;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;

/* The textures kept for pixmaps, by context and pixmap (its memory, size and pitch), with the
 * pixmap's pixels as last uploaded (packed rows of 4 * width bytes). libgm-wayland has a pixmap or
 * two a window; the oldest goes when the table is full. */
struct kept {
    EGLContext ctx;
    uintptr_t data;
    uint32_t flags, w, h, pitch;
    GLuint texture;
    uint8_t *shadow;
    unsigned used;
};
#define KEPT 4
static struct kept kept[KEPT];
static unsigned kept_clock;

static uint32_t field(const void *pixmap, size_t offset)
{
    uint32_t v;
    memcpy(&v, (const uint8_t *)pixmap + offset, sizeof v);
    return v;
}

/* B, G, R, A bytes (ARGB8888 words) to R, G, B, A, one packed row after another. */
static void to_rgba(uint8_t *dst, const uint8_t *src, uint32_t w, uint32_t h, uint32_t pitch)
{
    for (uint32_t y = 0; y < h; y++, src += pitch)
        for (uint32_t x = 0; x < w; x++, dst += 4) {
            const uint8_t *p = src + 4 * x;
            dst[0] = p[2];
            dst[1] = p[1];
            dst[2] = p[0];
            dst[3] = p[3];
        }
}

static const char *gfx_path = WEBOS_GFX_PATH; /* the unit test's file instead */

/* The pixels of a pixmap whose data is a physical address: a read-only mapping of the graphics
 * memory that holds them, *base and *len for munmap; NULL if they are not all in it. */
static const uint8_t *map_phys(uint32_t phys, size_t size, void **base, size_t *len)
{
    if (phys < WEBOS_GFX_PHYS_BASE || phys - WEBOS_GFX_PHYS_BASE > WEBOS_GFX_MEM_SIZE ||
        size > WEBOS_GFX_MEM_SIZE - (phys - WEBOS_GFX_PHYS_BASE))
        return NULL;
    uint32_t offset = phys - WEBOS_GFX_PHYS_BASE, page = (uint32_t)sysconf(_SC_PAGESIZE);
    uint32_t start = offset & ~(page - 1);
    int fd = open(gfx_path, O_RDWR | O_CLOEXEC);
    if (fd < 0)
        return NULL;
    *len = size + (offset - start);
    *base = mmap(NULL, *len, PROT_READ, MAP_SHARED, fd, (off_t)start);
    close(fd);
    if (*base == MAP_FAILED)
        return NULL;
    return (const uint8_t *)*base + (offset - start);
}

/* Forget a kept texture; its name is deleted when its context is the current one (else it is the
 * context's until that goes). */
static void drop(struct kept *k, EGLContext current)
{
    if (k->texture && k->ctx == current)
        glDeleteTextures(1, &k->texture);
    free(k->shadow);
    memset(k, 0, sizeof *k);
}

/* Upload rows [y0, y1) of the pixmap (src, pitch) to the bound texture at those rows, converted;
 * GL_NO_ERROR or the first GL error. */
static GLenum upload(const uint8_t *src, uint32_t w, uint32_t y0, uint32_t y1, uint32_t pitch,
                     int whole)
{
    uint8_t *rgba = malloc((size_t)w * (y1 - y0) * 4);
    if (!rgba)
        return GL_OUT_OF_MEMORY;
    to_rgba(rgba, src + (size_t)y0 * pitch, w, y1 - y0, pitch);
    GLint align = 4;
    glGetIntegerv(GL_UNPACK_ALIGNMENT, &align);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 4);
    if (whole)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, (GLsizei)w, (GLsizei)(y1 - y0), 0, GL_RGBA,
                     GL_UNSIGNED_BYTE, rgba);
    else
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, (GLint)y0, (GLsizei)w, (GLsizei)(y1 - y0), GL_RGBA,
                        GL_UNSIGNED_BYTE, rgba);
    glPixelStorei(GL_UNPACK_ALIGNMENT, align);
    free(rgba);
    return glGetError();
}

/* The kept texture of this pixmap in the current context, brought up to date with its pixels
 * (src): the texture, or 0 when there is none to keep (caller holds lock). *rows: the rows
 * uploaded. */
static GLuint kept_texture(EGLContext ctx, uintptr_t data, uint32_t flags, uint32_t w, uint32_t h,
                           uint32_t pitch, const uint8_t *src, uint32_t *rows)
{
    struct kept *k = NULL, *oldest = &kept[0];
    for (int i = 0; i < KEPT && !k; i++) {
        struct kept *c = &kept[i];
        if (c->texture && c->ctx == ctx && c->data == data && c->flags == flags && c->w == w &&
            c->h == h && c->pitch == pitch)
            k = c;
        else if (!c->texture || (oldest->texture && c->used < oldest->used))
            oldest = c;
    }
    size_t row = (size_t)w * 4;
    uint32_t y0 = 0, y1 = h;
    int whole = !k;
    if (!k) {
        k = oldest;
        drop(k, ctx);
        k->shadow = malloc(row * h);
        if (!k->shadow)
            return 0;
        glGenTextures(1, &k->texture);
        k->ctx = ctx;
        k->data = data;
        k->flags = flags;
        k->w = w;
        k->h = h;
        k->pitch = pitch;
    } else {
        while (y0 < h && !memcmp(src + (size_t)y0 * pitch, k->shadow + y0 * row, row))
            y0++;
        while (y1 > y0 && !memcmp(src + (size_t)(y1 - 1) * pitch, k->shadow + (y1 - 1) * row, row))
            y1--;
    }
    k->used = ++kept_clock;
    *rows = y1 - y0;
    if (y0 == y1)
        return k->texture;
    GLint bound = 0;
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &bound);
    glBindTexture(GL_TEXTURE_2D, k->texture);
    if (whole) {
        /* complete without mipmaps, as an image's source must be */
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    }
    GLenum error = upload(src, w, y0, y1, pitch, whole);
    glBindTexture(GL_TEXTURE_2D, (GLuint)bound);
    while (glGetError() != GL_NO_ERROR) /* the caller checks glGetError after its own calls */
        ;
    if (error != GL_NO_ERROR) {
        syslog(LOG_ERR, "mali-pixmap: the texture of a %ux%u pixmap: GL error %#x", w, h, error);
        drop(k, ctx);
        return 0;
    }
    for (uint32_t y = y0; y < y1; y++)
        memcpy(k->shadow + y * row, src + (size_t)y * pitch, row);
    return k->texture;
}

/* A texture of its own for one image (when there is no kept one): 0 on a GL error. */
static GLuint own_texture(const uint8_t *src, uint32_t w, uint32_t h, uint32_t pitch)
{
    GLuint texture = 0;
    GLint bound = 0;
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &bound);
    glGenTextures(1, &texture);
    glBindTexture(GL_TEXTURE_2D, texture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    GLenum error = upload(src, w, 0, h, pitch, 1);
    glBindTexture(GL_TEXTURE_2D, (GLuint)bound);
    while (glGetError() != GL_NO_ERROR)
        ;
    if (error != GL_NO_ERROR) {
        syslog(LOG_ERR, "mali-pixmap: the texture of a %ux%u pixmap: GL error %#x", w, h, error);
        glDeleteTextures(1, &texture);
        return 0;
    }
    return texture;
}

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

/* An image of a Mali pixmap; a pixmap this library does not recognise as one goes to Mesa as the
 * caller made the call (ctx, attr), so it fails there with Mesa's own error. */
static EGLImageKHR pixmap_image(EGLDisplay dpy, EGLContext caller_ctx, const void *pixmap,
                                const EGLint *caller_attr)
{
    uint32_t h = field(pixmap, PIXMAP_HEIGHT), w = field(pixmap, PIXMAP_WIDTH);
    uint32_t pitch = field(pixmap, PIXMAP_PITCH), bpp = field(pixmap, PIXMAP_BPP);
    uint32_t flags = field(pixmap, PIXMAP_FLAGS);
    uintptr_t data;
    memcpy(&data, (const uint8_t *)pixmap + PIXMAP_DATA, sizeof data);
    EGLContext ctx = eglGetCurrentContext();
    if (!w || !h || w > PIXMAP_MAX || h > PIXMAP_MAX || bpp != 4 || pitch < 4 * w || !data ||
        ctx == EGL_NO_CONTEXT) {
        syslog(LOG_ERR,
               "mali-pixmap: not a pixmap this library takes: %ux%u pitch %u bpp %u "
               "data %#lx flags %#x context %p",
               w, h, pitch, bpp, (unsigned long)data, flags, (void *)ctx);
        return mesa.create(dpy, caller_ctx, EGL_NATIVE_PIXMAP_KHR, (EGLClientBuffer)pixmap,
                           caller_attr);
    }
    double t0 = now_ms();
    const uint8_t *src = (const uint8_t *)data;
    void *map = NULL;
    size_t map_len = 0;
    if (flags & PIXMAP_PHYS) {
        src = data == (uint32_t)data
                  ? map_phys((uint32_t)data, (size_t)pitch * (h - 1) + 4u * w, &map, &map_len)
                  : NULL;
        if (!src) {
            syslog(LOG_ERR, "mali-pixmap: %ux%u pixmap at physical %#lx: not graphics memory", w, h,
                   (unsigned long)data);
            return mesa.create(dpy, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR, NULL, NULL);
        }
    }
    struct pixmap_image *pi = calloc(1, sizeof *pi);
    if (!pi) {
        if (map)
            munmap(map, map_len);
        return mesa.create(dpy, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR, NULL, NULL);
    }
    static const EGLint attr[] = {EGL_GL_TEXTURE_LEVEL_KHR, 0, EGL_NONE};
    uint32_t rows = h;
    pthread_mutex_lock(&lock);
    GLuint texture = kept_texture(ctx, data, flags, w, h, pitch, src, &rows);
    if (texture) {
        pi->image =
            mesa.create(dpy, ctx, EGL_GL_TEXTURE_2D_KHR, (EGLClientBuffer)(uintptr_t)texture, attr);
        if (pi->image == EGL_NO_IMAGE_KHR) {
            /* not this texture again: an image of a texture of its own */
            syslog(LOG_ERR, "mali-pixmap: the image of a kept %ux%u texture: EGL error %#x", w, h,
                   eglGetError());
            for (int i = 0; i < KEPT; i++)
                if (kept[i].texture == texture && kept[i].ctx == ctx)
                    drop(&kept[i], ctx);
        }
    }
    pthread_mutex_unlock(&lock);
    if (!texture || pi->image == EGL_NO_IMAGE_KHR) {
        rows = h;
        pi->texture = own_texture(src, w, h, pitch);
        if (pi->texture) {
            pi->image = mesa.create(dpy, ctx, EGL_GL_TEXTURE_2D_KHR,
                                    (EGLClientBuffer)(uintptr_t)pi->texture, attr);
            if (pi->image == EGL_NO_IMAGE_KHR) {
                syslog(LOG_ERR, "mali-pixmap: the image of a %ux%u texture: EGL error %#x", w, h,
                       eglGetError());
                glDeleteTextures(1, &pi->texture);
            }
        }
    }
    if (map)
        munmap(map, map_len);
    if (pi->image == EGL_NO_IMAGE_KHR) {
        free(pi);
        return mesa.create(dpy, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR, NULL, NULL);
    }
    trace("mali-pixmap: %ux%u pitch %u data %#lx flags %#x context %p: %u rows in %.1f ms", w, h,
          pitch, (unsigned long)data, flags, (void *)ctx, rows, now_ms() - t0);
    pi->dpy = dpy;
    pthread_mutex_lock(&lock);
    pi->next = images;
    images = pi;
    pthread_mutex_unlock(&lock);
    return pi->image;
}

static EGLImageKHR create_image(EGLDisplay dpy, EGLContext ctx, EGLenum target,
                                EGLClientBuffer buffer, const EGLint *attr)
{
    if (!mesa_load())
        return EGL_NO_IMAGE_KHR;
    if (target == EGL_NATIVE_PIXMAP_KHR && buffer)
        return pixmap_image(dpy, ctx, buffer, attr);
    return mesa.create(dpy, ctx, target, buffer, attr);
}

static EGLBoolean destroy_image(EGLDisplay dpy, EGLImageKHR image)
{
    if (!mesa_load())
        return EGL_FALSE;
    struct pixmap_image *pi = NULL;
    pthread_mutex_lock(&lock);
    for (struct pixmap_image **p = &images; *p; p = &(*p)->next)
        if ((*p)->image == image && (*p)->dpy == dpy) {
            pi = *p;
            *p = pi->next;
            break;
        }
    pthread_mutex_unlock(&lock);
    EGLBoolean ok = mesa.destroy(dpy, image);
    if (pi) {
        /* the image keeps its storage; with no context current the texture name is the
         * context's until it goes */
        if (pi->texture && eglGetCurrentContext() != EGL_NO_CONTEXT)
            glDeleteTextures(1, &pi->texture);
        free(pi);
    }
    return ok;
}

EXPORT __eglMustCastToProperFunctionPointerType eglGetProcAddress(const char *name)
{
    if (name && !strcmp(name, "eglCreateImageKHR"))
        return (__eglMustCastToProperFunctionPointerType)create_image;
    if (name && !strcmp(name, "eglDestroyImageKHR"))
        return (__eglMustCastToProperFunctionPointerType)destroy_image;
    return mesa_load() ? mesa.proc(name) : NULL;
}
