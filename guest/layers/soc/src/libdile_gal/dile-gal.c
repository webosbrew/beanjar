// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libdile_gal.so.1 for the emulator: LG's graphics abstraction (DILE_GAL, the API gm, libgm,
 * libim and tvservice call) on the emulator's graphics device instead of LG's gfx and fbdev
 * drivers.
 *
 * It takes the stock library's place under its soname and NEEDs the stock file (the other
 * HAL shims' pattern); the functions here answer, and the rest of the 33 (DrawLine,
 * DrawRectangle, Finalize-style no-ops nobody calls) stay LG's. Below LG's DILE layer, libhalgal
 * and KADP would open /dev/lg/gfx0 and the fbdev nodes and fail; nothing under DILE runs here.
 *
 * The device (gfx-memory.c): 192 MiB of graphics memory behind the SoC module's /dev/gfx, which gm,
 * libgm and tvservice map themselves; surfaces are allocated in it for every process, so a
 * surface one process creates is the same memory in every other (libgm registers its clients'
 * surfaces with gm, which blits them). The 2D engine is software (gal-draw.c), the image decoder
 * is libpng (gal-image.c). The frame buffer controllers are state the display engine shows:
 * controller 2, the cursor plane, goes to the host viewer (gfx-memory.c); controller 0, the OSD
 * plane, is scanned out by the compositor's egl-kms as the virtio-gpu scanout: the compositor's
 * picture where the plane shows its GM window, with gm's merged windows where gm merges
 * (gfx-merge.c). The compositor's GM windows hold no pixels here, so blits read their picture
 * back instead (osd-readback.c): gm's GM_CaptureGraphicScreen (tvservice's tv.capture GRAPHIC and
 * SCREEN) copies the visible windows' front buffers, the compositor's among them; a blit from the
 * OSD plane's merge buffer reads the scanout, which is that merge with the compositor in it; and
 * gm's own merges leave a placeholder for the display.
 *
 * Reported device (DILE_GAL_GetDeviceCapability): LG's libhalgal values for H13 (allocator,
 * blit/draw flags, stretch blit and image decode, one layer, two frame buffer controllers,
 * decode and blit limits) with the emulator's memory, PNG as the only image format, and the
 * vertical/horizontal mirror blit flags, which tvservice's capture scaler then uses to flip the
 * video plane. The same on every chip: they
 * describe this engine, not the SoC's (M2's libkadaptor offers more blit flags, JPEG and larger
 * limits). */
#include "gal.h"
#include "osd-readback.h"
#include "../../../../../include/webos-gfx.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))

int gal_trace;

void gal_log(const char *fmt, ...)
{
    char line[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    syslog(LOG_INFO, "gal: %s", line);
}

__attribute__((constructor)) static void init(void)
{
    /* every call in syslog (/var/log/legacy-log), for processes started after the file exists */
    gal_trace = getenv("GAL_TRACE") != NULL || access("/tmp/gal-trace", F_OK) == 0;
}

#define TRACE(...)                                                                                 \
    do {                                                                                           \
        if (gal_trace)                                                                             \
            gal_log(__VA_ARGS__);                                                                  \
    } while (0)

/* ---- surfaces ---- */

static int surface_ok(const surface_t *s)
{
    return s && gfx_memory() && s->pixelFormat < FMT_COUNT && s->bpp == gal_bpp(s->pixelFormat) &&
           s->width && s->height && s->pitch >= (unsigned)s->width * s->bpp / 8 && s->offset &&
           (uint64_t)s->offset + (uint64_t)s->pitch * s->height <= WEBOS_GFX_MEM_SIZE;
}

/* The pixels of a surface. Stack use stays small here and below: DILE_GAL runs on the callers'
 * threads, and tvservice's dbgfrwk hands some of them small stacks. */
static void pixmap_of(const surface_t *s, struct pixmap *p)
{
    unsigned length = 0;
    const uint32_t *palette;
    p->base = gfx_memory() + s->offset;
    p->pitch = s->pitch;
    p->width = s->width;
    p->height = s->height;
    p->format = s->pixelFormat;
    p->palette = NULL;
    p->palette_length = 0;
    if (s->pixelFormat != FMT_LUT8)
        return;
    palette = gfx_palette(s->offset, &length);
    if (palette) {
        p->palette = palette;
        p->palette_length = length;
    } else if (s->paletteLength) {
        p->palette = s->palette;
        p->palette_length = s->paletteLength > 256 ? 256 : s->paletteLength;
    }
}

/* the part of r (the whole surface when NULL) inside the surface */
static int clip(const surface_t *s, const rect_t *r, rect_t *out)
{
    unsigned x = r ? r->x : 0, y = r ? r->y : 0, w = r ? r->w : s->width, h = r ? r->h : s->height;
    if (x >= s->width || y >= s->height || !w || !h)
        return 0;
    out->x = x;
    out->y = y;
    out->w = x + w > s->width ? s->width - x : w;
    out->h = y + h > s->height ? s->height - y : h;
    return 1;
}

EXPORT int DILE_GAL_Init(void)
{
    static int logged;
    if (!gfx_memory())
        return GAL_ERR;
    if (!logged++)
        gal_log("graphics memory: %u MiB at 0x%08x (%s)", WEBOS_GFX_MEM_SIZE >> 20,
                WEBOS_GFX_PHYS_BASE, WEBOS_GFX_PATH);
    return GAL_OK;
}

/* The release's DILE_GAL_DEVICE_CAPABILITY_INFO_T is capability_t and, from webOS 4 on, a byte at
 * +72 that the stand-in leaves 0 (W18R's gfx_GetDeviceCapability clears 76 bytes): the whole of
 * it is cleared, and no more than the caller's (dile_gal_capability_bytes of the release). */
#ifndef GAL_CAPABILITY_BYTES
#define GAL_CAPABILITY_BYTES 72
#endif
_Static_assert(GAL_CAPABILITY_BYTES >= sizeof(capability_t),
               "the release's capability is capability_t and more");

EXPORT int DILE_GAL_GetDeviceCapability(capability_t *cap)
{
    if (!cap)
        return GAL_ERR;
    memset(cap, 0, GAL_CAPABILITY_BYTES);
    cap->bAllocatorSupported = 1;
    cap->blitFlags = 0x003006EFu | BLIT_VERTICAL_MIRROR | BLIT_HORIZONTAL_MIRROR;
    cap->drawFlags = 0x3D;
    cap->supportedFunctions = 0x00130001u; /* fill, blit, stretch blit, image decode */
    cap->maxNumOfLayers = 1;
    cap->maxNumOfFBHWControllers = 2;
    cap->physicalAddrOfBase = WEBOS_GFX_PHYS_BASE;
    cap->gfxMemorySize = WEBOS_GFX_MEM_SIZE;
    cap->supportedImageFormat = 2; /* PNG */
    cap->hwDecodeMinLimitWidth = cap->hwDecodeMinLimitHeight = 496;
    cap->hwDecodeMaxLimitWidth = cap->hwDecodeMaxLimitHeight = 8192;
    cap->maxScaleUpFactor = cap->minScaleDownFactor = 32;
    cap->hwBlitLimitWidth = 1920;
    cap->hwBlitLimitHeight = 1080;
    return GAL_OK;
}

EXPORT int DILE_GAL_GetGraphicMemInfo(uint32_t *info)
{
    if (!info || !gfx_memory())
        return GAL_ERR;
    gfx_mem_info(&info[0], &info[1], &info[2], &info[3]);
    return GAL_OK;
}

EXPORT int DILE_GAL_CreateSurface(unsigned width, unsigned height, unsigned format, surface_t *out)
{
    unsigned bpp, pitch;
    uint32_t offset;
    width &= 0xffff;
    height &= 0xffff;
    bpp = gal_bpp(format);
    if (!out || !width || !height || !bpp) {
        gal_log("CreateSurface(%ux%u, format %u): not a surface this engine makes", width, height,
                format);
        return GAL_ERR;
    }
    pitch = (width * bpp / 8 + 15) & ~15u;
    offset = gfx_alloc(pitch * height);
    if (offset == ~0u)
        return GAL_ERR;
    memset(out, 0, sizeof *out);
    out->offset = offset;
    out->physicalAddress = WEBOS_GFX_PHYS_BASE + offset;
    out->pitch = (uint16_t)pitch;
    out->bpp = (uint16_t)bpp;
    out->width = (uint16_t)width;
    out->height = (uint16_t)height;
    out->pixelFormat = format;
    TRACE("CreateSurface(%ux%u, format %u) = 0x%x", width, height, format, offset);
    return GAL_OK;
}

EXPORT int DILE_GAL_DestroySurface(surface_t *s)
{
    if (!s)
        return GAL_ERR;
    TRACE("DestroySurface(0x%x)", s->offset);
    return gfx_free(s->offset);
}

EXPORT int DILE_GAL_FillRectangle(surface_t *s, rect_t *r, unsigned color, uint32_t *flags,
                                  draw_settings_t *settings)
{
    struct pixmap p;
    rect_t c;
    if (!surface_ok(s))
        return GAL_ERR;
    TRACE("FillRectangle(0x%x %ux%u f%u, %u,%u %ux%u, 0x%08x, flags 0x%x, blend %u/%u)", s->offset,
          s->width, s->height, s->pixelFormat, r ? r->x : 0, r ? r->y : 0, r ? r->w : 0,
          r ? r->h : 0, color, flags ? *flags : 0, settings ? settings->srcBlend : 0,
          settings ? settings->dstBlend : 0);
    if (!clip(s, r, &c))
        return GAL_OK;
    if (gfx_display_process() && !c.x && !c.y && c.w == s->width && c.h == s->height &&
        !(flags && (*flags & DRAW_BLEND)))
        gfx_merge_write(s, color ? MERGE_REPLACED : MERGE_CLEARED);
    /* clearing whole rows of pixels to 0 gives the pages back: gm clears its window buffers */
    if (!color && (!flags || !*flags) && c.x == 0 && c.w == s->width &&
        s->pitch * 8u == s->width * s->bpp) {
        gfx_zero(s->offset + (uint32_t)c.y * s->pitch, (uint32_t)c.h * s->pitch);
        return GAL_OK;
    }
    pixmap_of(s, &p);
    return gal_fill(&p, &c, color, flags ? *flags : 0, settings);
}

/* The pixels a surface holds on screen, w x h of the whole surface, read back (osd-readback.c):
 * a compositor window's picture (its latest framebuffer), or the OSD plane's frame buffer as
 * scanned out; NULL for any other surface, and for gm composing its own surfaces. */
static uint32_t *screen_pixels(const surface_t *s, uint32_t dst_offset, unsigned w, unsigned h)
{
    struct gfx_controller_state osd;
    uint32_t *pixels, fb_id = 0;
    int slot = gfx_compositor_slot(s->offset);
    if (s->pixelFormat != FMT_ARGB)
        return NULL;
    if (slot >= 0)
        fb_id = gfx_compositor_fb(slot);
    else if (gfx_controller_get(GFX_OSD, &osd) || !osd.has_fb || !osd.visible ||
             osd.fb.offset != s->offset)
        return NULL;
    if (gfx_composing(dst_offset))
        return NULL;
    pixels = malloc((size_t)w * h * 4);
    if (pixels && osd_readback_argb(fb_id, (uint8_t *)pixels, (int)w, (int)h)) {
        free(pixels);
        return NULL;
    }
    return pixels;
}

static int blit(surface_t *src, rect_t *sr, surface_t *dst, rect_t *dr, uint32_t *flags,
                blit_settings_t *settings)
{
    struct pixmap s, d;
    uint32_t *screen;
    rect_t cs, cd;
    int rc;
    if (!surface_ok(src) || !surface_ok(dst))
        return GAL_ERR;
    TRACE("Blit(0x%x %ux%u f%u %u,%u %ux%u -> 0x%x %ux%u f%u %u,%u %ux%u, flags 0x%x, blend %u/%u "
          "a%u)",
          src->offset, src->width, src->height, src->pixelFormat, sr ? sr->x : 0, sr ? sr->y : 0,
          sr ? sr->w : 0, sr ? sr->h : 0, dst->offset, dst->width, dst->height, dst->pixelFormat,
          dr->x, dr->y, dr->w, dr->h, flags ? *flags : 0, settings ? settings->srcBlend : 0,
          settings ? settings->dstBlend : 0, settings ? settings->alpha : 0);
    if (!clip(src, sr, &cs) || !clip(dst, dr, &cd))
        return GAL_OK;
    /* gm merging the compositor's window: a placeholder for the display (gfx-merge.c) */
    int slot = gfx_compositor_slot(src->offset);
    if (slot >= 0 && gfx_composing(dst->offset) &&
        gfx_merge_placeholder(slot, &cs, dst, &cd, flags ? *flags : 0, settings) == GAL_OK)
        return GAL_OK;
    if (gfx_display_process() && !cd.x && !cd.y && cd.w == dst->width && cd.h == dst->height &&
        !(flags && (*flags & (BLIT_BLEND_ALPHACHANNEL | BLIT_BLEND_COLORALPHA))))
        gfx_merge_write(dst, MERGE_REPLACED);
    pixmap_of(src, &s);
    pixmap_of(dst, &d);
    /* the whole plane, scaled: read back at the destination's size, filtered as the display
     * engine's scaler would (area average); a part: at the plane's size, then sampled */
    int whole = !cs.x && !cs.y && cs.w == src->width && cs.h == src->height;
    screen = screen_pixels(src, dst->offset, whole ? cd.w : src->width, whole ? cd.h : src->height);
    if (screen) {
        s.base = (uint8_t *)screen;
        if (whole) {
            s.pitch = cd.w * 4u;
            s.width = cd.w;
            s.height = cd.h;
            cs = (rect_t){0, 0, cd.w, cd.h};
        } else {
            s.pitch = src->width * 4u;
        }
        TRACE("Blit: source 0x%x is on screen: read back", src->offset);
    }
    struct timespec t0, t1;
    if (gal_trace)
        clock_gettime(CLOCK_MONOTONIC, &t0);
    rc = gal_blit(&s, &cs, &d, &cd, flags ? *flags : 0, settings);
    free(screen);
    if (gal_trace) {
        clock_gettime(CLOCK_MONOTONIC, &t1);
        gal_log("Blit: %.1f ms", (t1.tv_sec - t0.tv_sec) * 1e3 + (t1.tv_nsec - t0.tv_nsec) / 1e6);
    }
    return rc;
}

/* NYX HAL capture supplies an already allocated destination. Read the actual
 * displayed GPU picture, then convert/scale through the software engine;
 * missing scanout and invalid destinations remain errors. */
EXPORT int DILE_GAL_CaptureFrameBuffer(surface_t *dst)
{
    struct pixmap src, dest;
    uint32_t *pixels;
    rect_t whole;
    int rc;
    if (!surface_ok(dst))
        return GAL_ERR;
    pixels = malloc((size_t)dst->width * dst->height * 4);
    if (!pixels)
        return GAL_ERR;
    rc = osd_readback_argb(0, (uint8_t *)pixels, dst->width, dst->height);
    if (!rc) {
        src = (struct pixmap){
            (uint8_t *)pixels, dst->width * 4u, dst->width, dst->height, FMT_ARGB, NULL, 0};
        pixmap_of(dst, &dest);
        whole = (rect_t){0, 0, dst->width, dst->height};
        rc = gal_blit(&src, &whole, &dest, &whole, 0, NULL);
    }
    free(pixels);
    return rc ? GAL_ERR : GAL_OK;
}

EXPORT int DILE_GAL_StretchBlit(surface_t *src, rect_t *sr, surface_t *dst, rect_t *dr,
                                uint32_t *flags, blit_settings_t *settings)
{
    rect_t whole;
    if (!dr) {
        if (!dst)
            return GAL_ERR;
        whole = (rect_t){0, 0, dst->width, dst->height};
        dr = &whole;
    }
    return blit(src, sr, dst, dr, flags, settings);
}

EXPORT int DILE_GAL_Blit(surface_t *src, rect_t *sr, surface_t *dst, unsigned dx, unsigned dy,
                         uint32_t *flags, blit_settings_t *settings)
{
    rect_t d;
    if (!src)
        return GAL_ERR;
    d.x = (uint16_t)dx;
    d.y = (uint16_t)dy;
    d.w = sr ? sr->w : src->width;
    d.h = sr ? sr->h : src->height;
    return blit(src, sr, dst, &d, flags, settings);
}

EXPORT int DILE_GAL_SetSurfacePalette(surface_t *s, uint32_t *palette, int length)
{
    if (!s || !palette || length < 0 || length > 256)
        return GAL_ERR;
    TRACE("SetSurfacePalette(0x%x, %d)", s->offset, length);
    return gfx_palette_set(s->offset, palette, (unsigned)length);
}

EXPORT int DILE_GAL_GetSurfacePalette(surface_t *s, uint32_t *palette, int length)
{
    unsigned n = 0;
    const uint32_t *colors;
    if (!s || !palette || length < 0 || length > 256)
        return GAL_ERR;
    colors = gfx_palette(s->offset, &n);
    if (!colors) {
        colors = s->palette;
        n = s->paletteLength > 256 ? 256 : s->paletteLength;
    }
    memset(palette, 0, (size_t)length * 4);
    memcpy(palette, colors, (size_t)(n < (unsigned)length ? n : (unsigned)length) * 4);
    return GAL_OK;
}

/* DILE_GAL_IMAGE_INFO_T */
typedef struct {
    uint32_t imageFormat;
    rect_t rect;
    uint32_t imageLength, pixelFormat;
    uint8_t bImageFromBuffer, pad[3];
    const char *imagePath;
    const void *imagePtr;
} image_info_t;
_Static_assert(sizeof(image_info_t) == 32, "DILE_GAL_IMAGE_INFO_T");
#define IMAGE_JPEG               0
#define IMAGE_PNG                1
#define DECODE_PREMULTIPLY       0x001u
#define DECODE_VERTICAL_MIRROR   0x100u
#define DECODE_HORIZONTAL_MIRROR 0x200u

EXPORT int DILE_GAL_DecodeImage(image_info_t *info, surface_t *dst, uint32_t flags)
{
    unsigned w = 0, h = 0;
    uint32_t *pixels, blit_flags = 0;
    struct pixmap s, d;
    rect_t sr, dr;
    if (!info || !surface_ok(dst))
        return GAL_ERR;
    TRACE("DecodeImage(format %u, %u,%u %ux%u, length %u, pixel format %u, %s %s -> 0x%x %ux%u, "
          "flags 0x%x)",
          info->imageFormat, info->rect.x, info->rect.y, info->rect.w, info->rect.h,
          info->imageLength, info->pixelFormat, info->bImageFromBuffer ? "buffer" : "file",
          info->bImageFromBuffer ? "" : (info->imagePath ? info->imagePath : "(null)"), dst->offset,
          dst->width, dst->height, flags);
    if (info->imageFormat != IMAGE_PNG) {
        TRACE("DecodeImage: format %u not offered", info->imageFormat);
        return GAL_ERR;
    }
    pixels = info->bImageFromBuffer
                 ? gal_decode_png(NULL, info->imagePtr, info->imageLength, &w, &h)
                 : gal_decode_png(info->imagePath, NULL, 0, &w, &h);
    if (!pixels)
        return GAL_ERR;
    if (flags & DECODE_PREMULTIPLY)
        blit_flags |= BLIT_SRC_PREMULTIPLY;
    if (flags & DECODE_VERTICAL_MIRROR)
        blit_flags |= BLIT_VERTICAL_MIRROR;
    if (flags & DECODE_HORIZONTAL_MIRROR)
        blit_flags |= BLIT_HORIZONTAL_MIRROR;
    s = (struct pixmap){(uint8_t *)pixels, w * 4, w, h, FMT_ARGB, NULL, 0};
    sr = (rect_t){0, 0, (uint16_t)w, (uint16_t)h};
    rect_t want = {info->rect.x, info->rect.y, info->rect.w ? info->rect.w : (uint16_t)w,
                   info->rect.h ? info->rect.h : (uint16_t)h};
    int rc = GAL_OK;
    if (clip(dst, &want, &dr)) {
        pixmap_of(dst, &d);
        rc = gal_blit(&s, &sr, &d, &dr, blit_flags, NULL);
    }
    free(pixels);
    return rc;
}

/* ---- the display engine's controllers ---- */

EXPORT int DILE_GAL_SetFrameBuffer(surface_t *s, unsigned index)
{
    if (!surface_ok(s) || index >= GFX_CONTROLLERS)
        return GAL_ERR;
    TRACE("SetFrameBuffer(0x%x %ux%u, controller %u)", s->offset, s->width, s->height, index);
    gfx_display_start();
    gfx_controller_set_fb(index, s, 0, NULL, NULL);
    return GAL_OK;
}

/* DILE_GAL_FRAMEBUFFER_PROPERTY_EXTENDED_INFO_T */
typedef struct {
    rect_t view, disp;
    uint8_t stereo, pad[3];
    uint32_t stereoType;
} fb_property_ex_t;

EXPORT int DILE_GAL_SetFrameBufferWithProperty(surface_t *s, unsigned index, uint32_t flags,
                                               fb_property_ex_t *ex)
{
    if (!surface_ok(s) || index >= GFX_CONTROLLERS)
        return GAL_ERR;
    TRACE("SetFrameBufferWithProperty(0x%x %ux%u, controller %u, flags 0x%x, view %u,%u %ux%u, "
          "disp %u,%u %ux%u)",
          s->offset, s->width, s->height, index, flags, ex ? ex->view.x : 0, ex ? ex->view.y : 0,
          ex ? ex->view.w : 0, ex ? ex->view.h : 0, ex ? ex->disp.x : 0, ex ? ex->disp.y : 0,
          ex ? ex->disp.w : 0, ex ? ex->disp.h : 0);
    gfx_display_start();
    gfx_controller_set_fb(index, s, ex ? flags : 0, ex ? &ex->view : NULL, ex ? &ex->disp : NULL);
    if (ex && (flags & FB_PROP_STEREOSCOPE))
        gfx_controller_set_stereo(index, ex->stereo, ex->stereoType);
    return GAL_OK;
}

EXPORT int DILE_GAL_SetFrameBufferProperty(unsigned index, uint32_t flags, rect_t *info)
{
    if (index >= GFX_CONTROLLERS || !info)
        return GAL_ERR;
    TRACE("SetFrameBufferProperty(controller %u, flags 0x%x, view %u,%u %ux%u, disp %u,%u %ux%u)",
          index, flags, info[0].x, info[0].y, info[0].w, info[0].h, info[1].x, info[1].y, info[1].w,
          info[1].h);
    gfx_controller_set_view(index, flags, &info[0], &info[1]);
    return GAL_OK;
}

EXPORT int DILE_GAL_SetFBHWControllerVisibility(uint32_t *list, unsigned count)
{
    if (!list)
        return GAL_ERR;
    gfx_display_start();
    for (unsigned i = 0; i < count; i++) {
        TRACE("SetFBHWControllerVisibility(controller %u, %u)", list[2 * i], list[2 * i + 1]);
        if (list[2 * i] < GFX_CONTROLLERS)
            gfx_controller_set_visible(list[2 * i], list[2 * i + 1]);
    }
    return GAL_OK;
}

EXPORT int DILE_GAL_GetFBHWControllerVisibility(unsigned index, uint32_t *visible)
{
    struct gfx_controller_state c;
    if (!visible || gfx_controller_get(index, &c))
        return GAL_ERR;
    *visible = c.visible;
    return GAL_OK;
}

EXPORT int DILE_GAL_SetFBHWControllerVsync(unsigned index, unsigned vsync)
{
    if (index >= GFX_CONTROLLERS)
        return GAL_ERR;
    gfx_controller_set_vsync(index, vsync & 0xff);
    return GAL_OK;
}

EXPORT int DILE_GAL_SetFBHWControllerStereoscope(unsigned index, unsigned on, uint32_t type)
{
    if (index >= GFX_CONTROLLERS)
        return GAL_ERR;
    TRACE("SetFBHWControllerStereoscope(controller %u, %u, %u)", index, on & 0xff, type);
    gfx_controller_set_stereo(index, on & 0xff, type);
    return GAL_OK;
}

/* webOS 4's frame buffer sharpness (libgm imports both): W18R's DILE passes them to HAL_GAL_*,
 * which only logs HAL_GAL_NOT_CALLABLE and answers -4, which DILE turns into -1. The same here. */
EXPORT int DILE_GAL_GetFBHWControllerSharpness(unsigned index, void *sharpness)
{
    (void)index;
    (void)sharpness;
    return GAL_ERR;
}

EXPORT int DILE_GAL_SetFBHWControllerSharpness(unsigned index, void *sharpness)
{
    (void)index;
    (void)sharpness;
    return GAL_ERR;
}

/* The frame buffers the OSD controllers show, as a malloc'd array the caller frees (tvservice's
 * PQ_CheckOSDSize reads the first one's pixels to find the OSD's extent). */
EXPORT int DILE_GAL_GetFrameBufferList(surface_t **list, unsigned *count)
{
    struct gfx_controller_state c;
    unsigned n = 0;
    if (!list || !count)
        return GAL_ERR;
    *list = calloc(2, sizeof **list);
    if (!*list)
        return GAL_ERR;
    for (unsigned i = 0; i < 2; i++)
        if (!gfx_controller_get(i, &c) && c.has_fb)
            (*list)[n++] = c.fb;
    *count = n;
    if (!n) {
        free(*list);
        *list = NULL;
    }
    return GAL_OK;
}

EXPORT int DILE_GAL_SetScreenMirrorMode(uint32_t flags)
{
    TRACE("SetScreenMirrorMode(0x%x)", flags);
    return GAL_OK;
}

EXPORT int DILE_GAL_ResetBootLogo(void)
{
    TRACE("ResetBootLogo()");
    return GAL_OK;
}

EXPORT int DILE_GAL_MoveCursor(unsigned index, cursor_t *c)
{
    if (index != GFX_CURSOR || !c)
        return GAL_ERR;
    TRACE("MoveCursor(%d,%d, panel %ux%u, size %ux%u, hotspot %u %d,%d)", c->x, c->y, c->panelWidth,
          c->panelHeight, c->width, c->height, c->hotspot, c->hotX, c->hotY);
    gfx_cursor_move(c);
    return GAL_OK;
}

/* ---- webOS 3: the graphic output and cursor resolutions, the cursor by position ---- */

/* gm's GM_WINDOW_Initialize, which fails without it. Recorded: the OSD plane's size is the frame
 * buffer's here, which the compositor scans out as it is. */
EXPORT int DILE_GAL_SetGraphicOutputResolution(resolution_t r)
{
    if (!r.width || !r.height || !gfx_memory())
        return GAL_ERR;
    gal_log("graphic output resolution %ux%u", r.width, r.height);
    gfx_resolution_set(RES_OUTPUT, r);
    return GAL_OK;
}

/* libim: the space its cursor positions are in, and the one its images are drawn for. */
EXPORT int DILE_GAL_SetCursorResolution(resolution_t coordinate, resolution_t image)
{
    if (!coordinate.width || !coordinate.height || !gfx_memory())
        return GAL_ERR;
    gal_log("cursor resolution: coordinates %ux%u, images %ux%u", coordinate.width,
            coordinate.height, image.width, image.height);
    gfx_resolution_set(RES_CURSOR_COORDINATE, coordinate);
    gfx_resolution_set(RES_CURSOR_IMAGE, image);
    return GAL_OK;
}

/* libim's cursor move: MoveCursor's position with the panel size from SetCursorResolution (the
 * output resolution, else 1920x1080, before it). Checked as LG's HAL checks it: a negative
 * position or hotspot counts as 0, a hotspot outside the drawn size is refused. */
EXPORT int DILE_GAL_SetCursorPosition(cursor_position_t *p)
{
    resolution_t panel;
    cursor_t c;
    if (!p)
        return GAL_ERR;
    c.x = p->x < 0 ? 0 : p->x;
    c.y = p->y < 0 ? 0 : p->y;
    c.hotX = p->hotX < 0 ? 0 : p->hotX;
    c.hotY = p->hotY < 0 ? 0 : p->hotY;
    if (c.hotX > p->width || c.hotY > p->height)
        return GAL_ERR;
    panel = gfx_resolution(RES_CURSOR_COORDINATE);
    if (!panel.width || !panel.height)
        panel = gfx_resolution(RES_OUTPUT);
    if (!panel.width || !panel.height)
        panel = (resolution_t){1920, 1080};
    c.panelWidth = panel.width;
    c.panelHeight = panel.height;
    c.width = p->width;
    c.height = p->height;
    c.hotspot = p->hotspot;
    TRACE("SetCursorPosition(%d,%d, panel %ux%u, size %ux%u, hotspot %u %d,%d)", c.x, c.y,
          c.panelWidth, c.panelHeight, c.width, c.height, c.hotspot, c.hotX, c.hotY);
    gfx_cursor_move(&c);
    return GAL_OK;
}
