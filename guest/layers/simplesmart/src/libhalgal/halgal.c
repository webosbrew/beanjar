// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libhalgal.so for SimpleSmart SS1A: LG's graphics HAL (HAL_GAL_*, the 35 functions its
 * libhalgal.so.0.1.0 exports) over the emulator's GOP, in software
 * (docs/dev/architecture/simplesmart.md, "Display"; scratch/ss-halgal-abi.md has the ABI's
 * evidence). It stands in for the vendor's library, which drove MStar's GE and GOP through
 * libkadaptor's gfx_*; LG's callers stay stock: procmgr (its graphics manager, which allocates the
 * surfaces of every DirectFB client through GPUDDI), libgmopenapi and libgal/libdile_gal (RELEASE).
 *
 * The hardware is mstar-utopia.ko's GOP node, the firmware's /dev/fb0 (include/mstar-gop.h): the
 * pool (the GOP_FRAMEBUFFER record; LG's DirectFB maps the same pages from /dev/fb0 at its PA),
 * its allocator, and the two GOP controllers' planes, which the initramfs's presenter shows.
 * Drawing (fills, lines, blits with DirectFB's blend semantics) is the emulator's software 2D
 * engine, guest/layers/soc/src/libdile_gal/gal-draw.c. Image decoding (the vendor's GPD) is PNG,
 * with the firmware's own libpng12 (decode.c); JPEG is not offered.
 *
 * Structures are SS1A's, from its libhalgal's and callers' code: the surface 1056 bytes, the
 * capability 72 (no bFBCSupported), the blit settings 36 (with the source mask), the draw
 * settings 12. Each call returns HAL_GAL_OK or one of hal_gal.h's errors; a call the hardware
 * does not have is HAL_GAL_NOT_CALLABLE. HALGAL_TRACE=1 in the environment logs every call to
 * stderr (the firmware's console); without it, only the GOP's set-up calls are logged. */
#include "gal.h"
#include "decode.h"
#include "../../../../../include/mstar-gop.h"
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))
/* GAL_GX=1: NetCast 4.5's libgal.so (LM14's GAL_GX_*, docs/dev/architecture/netcast45.md), the same
 * ABI under its older names, without the functions SimpleSmart's HAL added (SS_ONLY). */
#ifndef GAL_GX
#define GAL_GX 0
#endif
#if GAL_GX
#define API(name)  GAL_GX_##name
#define LOG_PREFIX "[libgal] "
#define TRACE_ENV  "LIBGAL_TRACE"
#else
#define API(name)  HAL_GAL_##name
#define LOG_PREFIX "[halgal] "
#define TRACE_ENV  "HALGAL_TRACE"
#endif
#define SS_ONLY (!GAL_GX)

/* HAL_GAL_STATE_T */
enum {
    HAL_OK = 0,
    HAL_ERROR = -1,
    HAL_INVALID_PARAM = -2,
    HAL_NOT_AVAILABLE = -3,
    HAL_NOT_CALLABLE = -4
};

/* HAL_GAL_BLIT_SETTINGS_T: the draw settings' and the source mask (offset, x, y, flags) */
typedef struct {
    uint32_t srcBlend, dstBlend, alpha, color, srcColorkey, dstColorkey;
    uint32_t maskOffset;
    uint16_t maskX, maskY;
    uint32_t maskFlags;
} hal_blit_settings_t;
_Static_assert(sizeof(hal_blit_settings_t) == 36, "HAL_GAL_BLIT_SETTINGS_T");

typedef struct {
    uint16_t x1, y1, x2, y2;
} line_t; /* HAL_GAL_LINE_T */
typedef struct {
    rect_t view, disp;
} fb_property_t; /* ..._PROPERTY_INFO_T */
typedef struct {
    rect_t view, disp;
    uint8_t stereo, pad[3];
    uint32_t stereoType;
} fb_property_ex_t;
_Static_assert(sizeof(fb_property_ex_t) == 24, "HAL_GAL_FRAMEBUFFER_PROPERTY_EXTENDED_INFO_T");
typedef struct {
    uint32_t index, visible;
} fb_visibility_t; /* ..._VISIBILITY_INFO_T */
typedef struct {
    uint32_t total, allocated, free, largest;
} mem_info_t;

/* HAL_GAL_SUPPORTED_FUNC_MASK_T, HAL_GAL_FRAMEBUFFER_PROPERTY_FLAGS_T */
#define ACCEL_FILLRECTANGLE 0x00000001u
#define ACCEL_DRAWRECTANGLE 0x00000002u
#define ACCEL_DRAWLINE      0x00000004u
#define ACCEL_BLIT          0x00010000u
#define ACCEL_STRETCHBLIT   0x00020000u
#define ACCEL_DECODEIMAGE   0x00100000u
/* what gal-draw.c does of the blit and draw flags */
#define BLIT_FLAGS_DONE                                                                            \
    (BLIT_BLEND_ALPHACHANNEL | BLIT_BLEND_COLORALPHA | BLIT_COLORIZE | BLIT_SRC_COLORKEY |         \
     BLIT_DST_COLORKEY | BLIT_SRC_PREMULTIPLY | BLIT_DST_PREMULTIPLY | BLIT_DEMULTIPLY |           \
     BLIT_SRC_PREMULTCOLOR | BLIT_XOR | BLIT_VERTICAL_MIRROR | BLIT_HORIZONTAL_MIRROR)
#define DRAW_FLAGS_DONE                                                                            \
    (DRAW_BLEND | DRAW_DST_COLORKEY | DRAW_SRC_PREMULTIPLY | DRAW_DST_PREMULTIPLY |                \
     DRAW_DEMULTIPLY | DRAW_XOR)

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static int gop = -1;  /* /dev/fb0 */
static uint8_t *pool; /* the pool, mapped */
static uint32_t pool_pa, pool_size;
static int trace;
/* per process, what the hardware has no register for here */
static uint32_t mirror, osd3d_mode, osd3d_type, rotation __attribute__((unused)) /* SS_ONLY */,
    fb_format[MSTAR_GOP_PLANES], vsync[MSTAR_GOP_PLANES];

static void say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fputs(LOG_PREFIX, stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}
#define TRACE(...)                                                                                 \
    do {                                                                                           \
        if (trace)                                                                                 \
            say(__VA_ARGS__);                                                                      \
    } while (0)

/* the pool of this process, mapped on first use (HAL_GAL_Init, or any call before it) */
static int gop_open(void)
{
    struct mstar_gop_pool p;
    if (gop >= 0)
        return 0;
    trace = getenv(TRACE_ENV) && getenv(TRACE_ENV)[0] == '1';
    int fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        say("/dev/fb0: %s", strerror(errno));
        return -1;
    }
    if (ioctl(fd, MSTAR_GOP_IOC_GET_POOL, &p) < 0) {
        say("/dev/fb0: not the GOP frame buffer (%s)", strerror(errno));
        close(fd);
        return -1;
    }
    void *m = mmap(NULL, p.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (m == MAP_FAILED) {
        say("/dev/fb0: mmap of the pool at 0x%x: %s", p.pa, strerror(errno));
        close(fd);
        return -1;
    }
    pool = m;
    pool_pa = p.pa;
    pool_size = p.size;
    gop = fd;
    say("GOP pool 0x%x, %u KiB (pid %d)", pool_pa, pool_size >> 10, getpid());
    return 0;
}

static int ready(void)
{
    int ok;
    pthread_mutex_lock(&lock);
    ok = gop_open() == 0;
    pthread_mutex_unlock(&lock);
    return ok;
}

static unsigned bpp_of(uint32_t format)
{
    return gal_bpp(format);
}

/* a surface of the pool, whole */
static int surface_ok(const surface_t *s)
{
    return s && ready() && s->pixelFormat < FMT_COUNT && s->bpp == bpp_of(s->pixelFormat) &&
           s->width && s->height && s->pitch >= (unsigned)s->width * s->bpp / 8 &&
           s->physicalAddress >= pool_pa &&
           (uint64_t)s->physicalAddress - pool_pa + (uint64_t)s->pitch * s->height <= pool_size;
}

static void pixmap_of(const surface_t *s, struct pixmap *p)
{
    p->base = pool + (s->physicalAddress - pool_pa);
    p->pitch = s->pitch;
    p->width = s->width;
    p->height = s->height;
    p->format = s->pixelFormat;
    p->palette = s->pixelFormat == FMT_LUT8 && s->paletteLength ? s->palette : NULL;
    p->palette_length = s->paletteLength > 256 ? 256 : s->paletteLength;
}

/* r (NULL: all of s) within s, in out; 0 if nothing is left */
static int clip(const surface_t *s, const rect_t *r, rect_t *out)
{
    unsigned x = r ? r->x : 0, y = r ? r->y : 0, w = r ? r->w : s->width, h = r ? r->h : s->height;
    if (x >= s->width || y >= s->height || !w || !h)
        return 0;
    out->x = (uint16_t)x;
    out->y = (uint16_t)y;
    out->w = (uint16_t)(x + w > s->width ? s->width - x : w);
    out->h = (uint16_t)(y + h > s->height ? s->height - y : h);
    return 1;
}

/* pixels changed: the presenter looks again (it copies what a shown plane holds) */
static void damage(void)
{
    ioctl(gop, MSTAR_GOP_IOC_DAMAGE);
}

/* ---- the device ---- */

#if GAL_GX
/* MStar's system layer, which LM14's vendor libgal brings up once per process before its gfx_Init
 * (the GE and GOP, this library's part), in this order */
extern int MDrv_MMIO_Init(void) __attribute__((weak));
extern int MsOS_Init(void) __attribute__((weak));
extern int MAdp_MPool_Init(void) __attribute__((weak));
extern int MDrv_SEM_Init(void) __attribute__((weak));
extern int MDrv_MIU_Init(void) __attribute__((weak));
extern int MDrv_SYS_Init(void) __attribute__((weak));
/* a layer's own step at the first GAL_GX_Init of a process, where it links one into this library
 * (NetCast 4: guest/layers/netcast4/src/verify.c) */
extern void gal_gx_process_init(void) __attribute__((weak));
#else
extern int MAdp_SYS_Init(void) __attribute__((weak));
#endif

EXPORT int API(Init)(void)
{
#if GAL_GX
    static int system_up;
    int (*const system[])(void) = {MDrv_MMIO_Init, MsOS_Init,     MAdp_MPool_Init,
                                   MDrv_SEM_Init,  MDrv_MIU_Init, MDrv_SYS_Init};
    pthread_mutex_lock(&lock);
    for (unsigned i = 0; !system_up && i < sizeof(system) / sizeof(system[0]); i++)
        if (system[i])
            system[i]();
    if (!system_up && gal_gx_process_init)
        gal_gx_process_init();
    system_up = 1;
    pthread_mutex_unlock(&lock);
#else
    /* the vendor's HAL brought KADP's system layer up first (MAdp_SYS_Init), as each process
     * that uses the HAL expects; its gfx_Init (the GE and GOP) is this library's part */
    if (MAdp_SYS_Init)
        MAdp_SYS_Init();
#endif
    if (!ready())
        return HAL_ERROR;
    say("Init (pid %d)", getpid());
    return HAL_OK;
}

EXPORT int API(Finalize)(void)
{
    TRACE("Finalize");
    return HAL_OK;
}

EXPORT int API(GetDeviceCapability)(capability_t *c)
{
    if (!c || !ready())
        return HAL_INVALID_PARAM;
    memset(c, 0, sizeof(*c));
    c->bAllocatorSupported = 1;
    c->blitFlags = BLIT_FLAGS_DONE;
    c->drawFlags = DRAW_FLAGS_DONE;
    c->supportedFunctions = ACCEL_FILLRECTANGLE | ACCEL_DRAWRECTANGLE | ACCEL_DRAWLINE |
                            ACCEL_BLIT | ACCEL_STRETCHBLIT | ACCEL_DECODEIMAGE;
    c->maxNumOfLayers = 1;          /* SS1A's gfx_GetDeviceCapability's */
    c->maxNumOfFBHWControllers = 2; /* the OSD's and the cursor's: GOP 0 and 2 */
    c->physicalAddrOfBase = pool_pa;
    c->gfxMemorySize = pool_size;
    c->supportedImageFormat = 2; /* HAL_GAL_IMAGE_FORMAT_PNG_SUPPORTED */
    c->hwDecodeMaxLimitWidth = c->hwDecodeMaxLimitHeight = 8192;
    /* the stock limits (gfx_GetDeviceCapability): stretch up to 16x, blits to 4080x4080 */
    c->maxScaleUpFactor = 0x1000;
    c->swBlitArea = 0x00FE0000;
    c->hwBlitLimitWidth = c->hwBlitLimitHeight = 0x0FF0;
    c->hwBlitLimitArea = 1;
    TRACE("GetDeviceCapability: pool 0x%x %u", pool_pa, pool_size);
    return HAL_OK;
}

EXPORT int API(GetGraphicMemInfo)(mem_info_t *m)
{
    struct mstar_gop_mem g;
    if (!m || !ready())
        return HAL_INVALID_PARAM;
    if (ioctl(gop, MSTAR_GOP_IOC_MEM_INFO, &g) < 0)
        return HAL_ERROR;
    m->total = g.total;
    m->allocated = g.allocated;
    m->free = g.free;
    m->largest = g.largest_free;
    return HAL_OK;
}

/* ---- surfaces ---- */

EXPORT int API(CreateSurface)(uint16_t width, uint16_t height, uint32_t format, surface_t *s)
{
    struct mstar_gop_alloc a;
    unsigned bpp = bpp_of(format), w = width;
    if (!s || !width || !height || !bpp)
        return HAL_INVALID_PARAM;
    if (!ready())
        return HAL_ERROR;
    /* the vendor's HAL lays a 1366-pixel line out as 1376 (the GE's 16-pixel pitch) */
    if (w == 1366)
        w = 1376;
    memset(s, 0, sizeof(*s));
    s->pitch = (uint16_t)(w * bpp / 8);
    a.size = (uint32_t)s->pitch * height;
    a.align = 64;
    if (ioctl(gop, MSTAR_GOP_IOC_ALLOC, &a) < 0) {
        say("CreateSurface %ux%u format %u: %u bytes: %s", width, height, format, a.size,
            strerror(errno));
        return HAL_NOT_AVAILABLE;
    }
    s->offset = a.pa - pool_pa;
    s->physicalAddress = a.pa;
    s->bpp = (uint16_t)bpp;
    s->width = width;
    s->height = height;
    s->pixelFormat = format;
    s->property = a.size;                /* the union's length: the bytes allocated */
    memset(pool + s->offset, 0, a.size); /* cleared, as the vendor's HAL fills it with 0 */
    if (width >= 640 || trace)           /* the planes' surfaces, not every glyph's */
        say("CreateSurface %ux%u format %u -> 0x%x pitch %u", width, height, format, a.pa,
            s->pitch);
    return HAL_OK;
}

EXPORT int API(DestroySurface)(surface_t *s)
{
    uint32_t pa;
    if (!s || !ready())
        return HAL_INVALID_PARAM;
    pa = s->physicalAddress;
    TRACE("DestroySurface 0x%x", pa);
    if (ioctl(gop, MSTAR_GOP_IOC_FREE, &pa) < 0)
        return HAL_INVALID_PARAM;
    return HAL_OK;
}

EXPORT int API(GetSurfacePalette)(surface_t *s, uint32_t *palette, int32_t length)
{
    if (!s || !palette || length < 0 || length > 256)
        return HAL_INVALID_PARAM;
    memcpy(palette, s->palette, (size_t)length * 4);
    return HAL_OK;
}

EXPORT int API(SetSurfacePalette)(surface_t *s, uint32_t *palette, int32_t length)
{
    if (!s || !palette || length < 0 || length > 256)
        return HAL_INVALID_PARAM;
    memcpy(s->palette, palette, (size_t)length * 4);
    s->paletteLength = (uint32_t)length;
    return HAL_OK;
}

/* ---- drawing ---- */

EXPORT int API(FillRectangle)(surface_t *dst, rect_t *r, uint32_t color, uint32_t *flags,
                              draw_settings_t *settings)
{
    struct pixmap d;
    rect_t c;
    if (!surface_ok(dst))
        return HAL_INVALID_PARAM;
    TRACE("FillRectangle 0x%x %u,%u %ux%u 0x%08x flags 0x%x", dst->physicalAddress, r ? r->x : 0,
          r ? r->y : 0, r ? r->w : dst->width, r ? r->h : dst->height, color, flags ? *flags : 0);
    if (!clip(dst, r, &c))
        return HAL_OK;
    pixmap_of(dst, &d);
    gal_fill(&d, &c, color, flags ? *flags : 0, settings);
    damage();
    return HAL_OK;
}

EXPORT int API(DrawRectangle)(surface_t *dst, rect_t *r, uint32_t color, uint32_t *flags,
                              draw_settings_t *settings)
{
    struct pixmap d;
    rect_t c, e[4];
    if (!surface_ok(dst) || !r)
        return HAL_INVALID_PARAM;
    if (!r->w || !r->h)
        return HAL_OK;
    pixmap_of(dst, &d);
    e[0] = (rect_t){r->x, r->y, r->w, 1};
    e[1] = (rect_t){r->x, (uint16_t)(r->y + r->h - 1), r->w, 1};
    e[2] = (rect_t){r->x, (uint16_t)(r->y + 1), 1, (uint16_t)(r->h > 2 ? r->h - 2 : 0)};
    e[3] = (rect_t){(uint16_t)(r->x + r->w - 1), (uint16_t)(r->y + 1), 1,
                    (uint16_t)(r->h > 2 ? r->h - 2 : 0)};
    for (int i = 0; i < 4; i++)
        if ((i < 2 || r->w > 1) && clip(dst, &e[i], &c))
            gal_fill(&d, &c, color, flags ? *flags : 0, settings);
    damage();
    return HAL_OK;
}

EXPORT int API(DrawLine)(surface_t *dst, line_t *l, uint32_t color, uint32_t *flags,
                         draw_settings_t *settings)
{
    struct pixmap d;
    if (!surface_ok(dst) || !l)
        return HAL_INVALID_PARAM;
    pixmap_of(dst, &d);
    /* Bresenham, one pixel at a time */
    int x = l->x1, y = l->y1, dx = abs(l->x2 - l->x1), dy = -abs(l->y2 - l->y1);
    int sx = l->x1 < l->x2 ? 1 : -1, sy = l->y1 < l->y2 ? 1 : -1, err = dx + dy;
    for (;;) {
        if (x >= 0 && y >= 0 && x < dst->width && y < dst->height) {
            rect_t p = {(uint16_t)x, (uint16_t)y, 1, 1};
            gal_fill(&d, &p, color, flags ? *flags : 0, settings);
        }
        if (x == l->x2 && y == l->y2)
            break;
        int e2 = 2 * err;
        if (e2 >= dy) {
            err += dy;
            x += sx;
        }
        if (e2 <= dx) {
            err += dx;
            y += sy;
        }
    }
    damage();
    return HAL_OK;
}

/* sr (NULL: all of src) onto dr of dst, scaled; both clipped to their surfaces, dr's clipping
 * taking the same share off sr */
static int blit(surface_t *src, const rect_t *sr, surface_t *dst, const rect_t *dr, uint32_t flags,
                const hal_blit_settings_t *settings)
{
    struct pixmap s, d;
    rect_t sc, dc;
    if (!surface_ok(src) || !surface_ok(dst) || !dr)
        return HAL_INVALID_PARAM;
    if ((flags & ~BLIT_FLAGS_DONE) != 0) {
        TRACE("blit flags 0x%x not offered", flags);
        return HAL_NOT_CALLABLE;
    }
    if (!clip(src, sr, &sc) || !dr->w || !dr->h || dr->x >= dst->width || dr->y >= dst->height)
        return HAL_OK;
    dc = *dr;
    if ((unsigned)dc.x + dc.w > dst->width) {
        unsigned w = dst->width - dc.x;
        sc.w = (uint16_t)((uint64_t)sc.w * w / dc.w);
        dc.w = (uint16_t)w;
    }
    if ((unsigned)dc.y + dc.h > dst->height) {
        unsigned h = dst->height - dc.y;
        sc.h = (uint16_t)((uint64_t)sc.h * h / dc.h);
        dc.h = (uint16_t)h;
    }
    if (!sc.w || !sc.h)
        return HAL_OK;
    pixmap_of(src, &s);
    pixmap_of(dst, &d);
    gal_blit(&s, &sc, &d, &dc, flags, (const blit_settings_t *)settings);
    damage();
    return HAL_OK;
}

EXPORT int API(Blit)(surface_t *src, rect_t *sr, surface_t *dst, uint16_t dx, uint16_t dy,
                     uint32_t *flags, hal_blit_settings_t *settings)
{
    rect_t d;
    if (!src)
        return HAL_INVALID_PARAM;
    d = (rect_t){dx, dy, sr ? sr->w : src->width, sr ? sr->h : src->height};
    TRACE("Blit 0x%x -> 0x%x %u,%u %ux%u flags 0x%x", src->physicalAddress,
          dst ? dst->physicalAddress : 0, dx, dy, d.w, d.h, flags ? *flags : 0);
    return blit(src, sr, dst, &d, flags ? *flags : 0, settings);
}

EXPORT int API(StretchBlit)(surface_t *src, rect_t *sr, surface_t *dst, rect_t *dr, uint32_t *flags,
                            hal_blit_settings_t *settings)
{
    rect_t whole;
    if (!dst)
        return HAL_INVALID_PARAM;
    if (!dr) {
        whole = (rect_t){0, 0, dst->width, dst->height};
        dr = &whole;
    }
    TRACE("StretchBlit 0x%x -> 0x%x %u,%u %ux%u flags 0x%x", src ? src->physicalAddress : 0,
          dst->physicalAddress, dr->x, dr->y, dr->w, dr->h, flags ? *flags : 0);
    return blit(src, sr, dst, dr, flags ? *flags : 0, settings);
}

EXPORT int API(SyncGraphic)(void)
{
    return HAL_OK; /* every operation is done when its call returns */
}

/* HAL_GAL_IMAGE_INFO_T, 32 bytes */
typedef struct {
    uint32_t imageFormat;
    rect_t rect;
    uint32_t imageLength, pixelFormat;
    uint8_t bImageFromBuffer, pad[3];
    const char *imagePath;
    const void *imagePtr;
} image_info_t;
_Static_assert(sizeof(image_info_t) == 32, "HAL_GAL_IMAGE_INFO_T");
#define IMAGE_PNG                1
#define DECODE_PREMULTIPLY       0x001u
#define DECODE_VERTICAL_MIRROR   0x100u
#define DECODE_HORIZONTAL_MIRROR 0x200u

/* the picture into dst, at rect (w, h 0: the picture's size), scaled to it; PNG only (the vendor's
 * GPD did JPEG too: the capability offers PNG alone) */
EXPORT int API(DecodeImage)(image_info_t *info, surface_t *dst, uint32_t flags)
{
    static unsigned logged;
    unsigned w = 0, h = 0;
    uint32_t blit_flags = 0;
    char why[96] = "";
    struct pixmap s, d;
    rect_t sr, dr;
    if (!info || !surface_ok(dst))
        return HAL_INVALID_PARAM;
    if (logged++ < 8 || trace)
        say("DecodeImage format %u from %s %s -> 0x%x %ux%u at %u,%u %ux%u, flags 0x%x",
            info->imageFormat, info->bImageFromBuffer ? "buffer" : "file",
            info->bImageFromBuffer ? ""
            : info->imagePath      ? info->imagePath
                                   : "(null)",
            dst->physicalAddress, dst->width, dst->height, info->rect.x, info->rect.y, info->rect.w,
            info->rect.h, flags);
    if (info->imageFormat != IMAGE_PNG)
        return HAL_NOT_CALLABLE;
    if (info->bImageFromBuffer ? !info->imagePtr || !info->imageLength : !info->imagePath)
        return HAL_INVALID_PARAM;
    uint32_t *pixels =
        info->bImageFromBuffer
            ? decode_png(NULL, info->imagePtr, info->imageLength, &w, &h, why, sizeof why)
            : decode_png(info->imagePath, NULL, 0, &w, &h, why, sizeof why);
    if (!pixels) {
        say("DecodeImage: %s", why);
        return HAL_ERROR;
    }
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
    if (clip(dst, &want, &dr)) {
        /* clipping takes the same share off the picture */
        sr.w = (uint16_t)((uint64_t)w * dr.w / want.w);
        sr.h = (uint16_t)((uint64_t)h * dr.h / want.h);
        pixmap_of(dst, &d);
        if (sr.w && sr.h)
            gal_blit(&s, &sr, &d, &dr, blit_flags, NULL);
        damage();
    }
    free(pixels);
    return HAL_OK;
}

/* ---- the GOP controllers ---- */

static int plane_get(unsigned index, struct mstar_gop_plane *p)
{
    memset(p, 0, sizeof(*p));
    p->index = index;
    return ioctl(gop, MSTAR_GOP_IOC_GET_PLANE, p);
}

static int plane_set(const struct mstar_gop_plane *p)
{
    return ioctl(gop, MSTAR_GOP_IOC_SET_PLANE, p) < 0 ? HAL_INVALID_PARAM : HAL_OK;
}

static void rect_put(uint32_t *out, const rect_t *r)
{
    out[0] = r->x;
    out[1] = r->y;
    out[2] = r->w;
    out[3] = r->h;
}

static int set_fb(surface_t *s, uint32_t index, uint32_t flags, const rect_t *view,
                  const rect_t *disp)
{
    struct mstar_gop_plane p;
    if (!surface_ok(s) || index >= MSTAR_GOP_PLANES)
        return HAL_INVALID_PARAM;
    if (plane_get(index, &p) < 0)
        return HAL_ERROR;
    p.enabled = 1;
    p.pa = s->physicalAddress;
    p.width = s->width;
    p.height = s->height;
    p.pitch = s->pitch;
    p.format = s->pixelFormat;
    if (flags & FB_PROP_VIEWRECT && view)
        rect_put(p.view, view);
    if (flags & FB_PROP_DISPRECT && disp)
        rect_put(p.disp, disp);
    return plane_set(&p);
}

/* the first frame buffers set on each controller are logged (then every flip would be) */
static void fb_log(const char *what, const surface_t *s, uint32_t index, uint32_t flags)
{
    static unsigned count[MSTAR_GOP_PLANES];
    if (index >= MSTAR_GOP_PLANES)
        say("%s: controller %u: no such GOP", what, index);
    else if (count[index] < 4 && s) {
        count[index]++;
        say("%s 0x%x %ux%u format %u -> controller %u (flags 0x%x)", what, s->physicalAddress,
            s->width, s->height, s->pixelFormat, index, flags);
    }
}

EXPORT int API(SetFrameBuffer)(surface_t *s, uint32_t index)
{
    fb_log("SetFrameBuffer", s, index, 0);
    TRACE("SetFrameBuffer 0x%x -> %u", s ? s->physicalAddress : 0, index);
    return set_fb(s, index, 0, NULL, NULL);
}

#if SS_ONLY
EXPORT int API(SetFrameBufferWithProperty)(surface_t *s, uint32_t index, uint32_t flags,
                                           fb_property_ex_t *ex)
{
    fb_log("SetFrameBufferWithProperty", s, index, flags);
    if (flags & FB_PROP_STEREOSCOPE && ex && ex->stereo)
        return HAL_NOT_CALLABLE; /* no stereoscopic OSD */
    return set_fb(s, index, flags, ex ? &ex->view : NULL, ex ? &ex->disp : NULL);
}
#endif

EXPORT int API(SetFrameBufferProperty)(uint32_t index, uint32_t flags, fb_property_t *info)
{
    struct mstar_gop_plane p;
    if (index >= MSTAR_GOP_PLANES || !info || !ready())
        return HAL_INVALID_PARAM;
    TRACE("SetFrameBufferProperty %u flags 0x%x view %u,%u %ux%u disp %u,%u %ux%u", index, flags,
          info->view.x, info->view.y, info->view.w, info->view.h, info->disp.x, info->disp.y,
          info->disp.w, info->disp.h);
    if (plane_get(index, &p) < 0)
        return HAL_ERROR;
    if (flags & FB_PROP_VIEWRECT)
        rect_put(p.view, &info->view);
    if (flags & FB_PROP_DISPRECT)
        rect_put(p.disp, &info->disp);
    return plane_set(&p);
}

EXPORT int API(ResetFrameBuffer)(uint32_t index)
{
    struct mstar_gop_plane p;
    if (index >= MSTAR_GOP_PLANES || !ready())
        return HAL_INVALID_PARAM;
    say("ResetFrameBuffer %u", index);
    if (plane_get(index, &p) < 0)
        return HAL_ERROR;
    p.enabled = 0;
    return plane_set(&p);
}

EXPORT int API(SetFBHWControllerVisibility)(fb_visibility_t *list, uint32_t n)
{
    struct mstar_gop_plane p;
    if (!list || !ready())
        return HAL_INVALID_PARAM;
    for (uint32_t i = 0; i < n; i++) {
        if (list[i].index >= MSTAR_GOP_PLANES)
            return HAL_INVALID_PARAM;
        say("SetFBHWControllerVisibility %u %u", list[i].index, list[i].visible);
        if (plane_get(list[i].index, &p) < 0)
            return HAL_ERROR;
        p.visible = !!list[i].visible;
        if (plane_set(&p))
            return HAL_ERROR;
    }
    return HAL_OK;
}

#if SS_ONLY
EXPORT int API(GetFBHWControllerVisibility)(uint32_t index, uint8_t *visible)
{
    struct mstar_gop_plane p;
    if (index >= MSTAR_GOP_PLANES || !visible || !ready())
        return HAL_INVALID_PARAM;
    if (plane_get(index, &p) < 0)
        return HAL_ERROR;
    *visible = (uint8_t)p.visible;
    return HAL_OK;
}
#endif

#if SS_ONLY
/* the front frame buffer, controller 0's surface: one entry (or none), in a 1056-byte buffer
 * malloc'd at each call that the caller owns and frees (as the vendor's HAL: RELEASE's
 * PQFRWK_CheckOSDSize frees it) */
EXPORT int API(GetFrameBufferList)(surface_t **list, uint32_t *n)
{
    struct mstar_gop_plane p;
    surface_t *s;
    if (!n || !ready())
        return HAL_INVALID_PARAM;
    *n = 0;
    s = calloc(1, sizeof(*s));
    if (!s)
        return HAL_ERROR;
    if (list)
        *list = s;
    else
        free(s);
    if (plane_get(0, &p) < 0)
        return HAL_ERROR;
    if (p.enabled && list) {
        s->offset = p.pa - pool_pa;
        s->physicalAddress = p.pa;
        s->pitch = (uint16_t)p.pitch;
        s->width = (uint16_t)p.width;
        s->height = (uint16_t)p.height;
        s->pixelFormat = p.format;
        s->bpp = (uint16_t)bpp_of(p.format);
        *n = 1;
    }
    return HAL_OK;
}
#endif

EXPORT int API(SetFBHWControllerVsync)(uint32_t index, uint8_t on)
{
    if (index >= MSTAR_GOP_PLANES)
        return HAL_INVALID_PARAM;
    vsync[index] = on;
    return HAL_OK;
}

EXPORT int API(SetFrameBufferPixelFormat)(uint32_t index, uint32_t format)
{
    if (index >= MSTAR_GOP_PLANES)
        return HAL_INVALID_PARAM;
    if (format != 0) /* HAL_GAL_PIXEL_FORMAT_ARGB_EX: no YUV OSD */
        return HAL_NOT_CALLABLE;
    fb_format[index] = format;
    return HAL_OK;
}

EXPORT int API(GetFrameBufferPixelFormat)(uint32_t index, uint32_t *format)
{
    if (index >= MSTAR_GOP_PLANES || !format)
        return HAL_INVALID_PARAM;
    *format = fb_format[index];
    return HAL_OK;
}

EXPORT int API(SetFBHWControllerPathMode)(uint32_t index, uint32_t mode)
{
    if (index >= MSTAR_GOP_PLANES)
        return HAL_INVALID_PARAM;
    TRACE("SetFBHWControllerPathMode %u %u", index, mode);
    return HAL_OK; /* separate or blended: the presenter blends the planes in order either way */
}

EXPORT int API(SetFBHWControllerStereoscope)(uint32_t index, uint8_t on, uint32_t type)
{
    if (index >= MSTAR_GOP_PLANES)
        return HAL_INVALID_PARAM;
    TRACE("SetFBHWControllerStereoscope %u %u %u", index, on, type);
    return on ? HAL_NOT_CALLABLE : HAL_OK;
}

/* the panel's output mode: mirroring (an EPI panel's), 3D, rotation. The emulator's panel is
 * the viewer, which is neither mirrored nor 3D; these are kept as the hardware's settings. */
EXPORT int API(SetScreenMirrorMode)(uint32_t flags)
{
    say("SetScreenMirrorMode 0x%x", flags);
    mirror = flags;
    return HAL_OK;
}

EXPORT int API(Set3DMode)(uint32_t mode, uint32_t type)
{
    TRACE("Set3DMode %u %u", mode, type);
    if (mode && type)
        return HAL_NOT_CALLABLE;
    osd3d_mode = mode;
    osd3d_type = type;
    return HAL_OK;
}

#if SS_ONLY
EXPORT int API(SetOSDRotationMode)(uint32_t type)
{
    TRACE("SetOSDRotationMode %u", type);
    rotation = type;
    return type ? HAL_NOT_CALLABLE : HAL_OK;
}
#endif

#if SS_ONLY
EXPORT int API(ResetBootLogo)(void)
{
    return HAL_OK; /* SS1A's vendor HAL does nothing here either */
}
#endif

#if SS_ONLY
EXPORT int API(SetGraphicOutputResolution)(resolution_t r)
{
    say("SetGraphicOutputResolution %ux%u", r.width, r.height);
    return HAL_OK;
}
#endif

/* the cursor: drawn into a plane by procmgr; its plane's position is not modelled yet */
#if GAL_GX
/* The cursor's plane where the pointer is: the stock GAL_GX_MoveCursor passes the position, the
 * coordinate space (panel width and height), the drawn size and the hotspot to
 * gfx_SetWindowPosition, negative coordinates as 0 and a hotspot past the drawn size refused. Here
 * the plane shows the drawn part of its surface (view) at 1:1, its hotspot at the position (disp),
 * in the output's pixels, which are the coordinate space's (the target's 1920x1080). */
EXPORT int API(MoveCursor)(uint32_t index, cursor_t *c)
{
    static int logged;
    struct mstar_gop_plane p;
    if (!c || index >= MSTAR_GOP_PLANES || !ready())
        return HAL_INVALID_PARAM;
    unsigned x = c->x < 0 ? 0 : (unsigned)c->x, y = c->y < 0 ? 0 : (unsigned)c->y;
    unsigned hx = c->hotX < 0 ? 0 : (unsigned)c->hotX, hy = c->hotY < 0 ? 0 : (unsigned)c->hotY;
    if (hx > c->width || hy > c->height)
        return HAL_ERROR;
    if (!logged++)
        say("MoveCursor %u: %u,%u in %ux%u, drawn %ux%u, hotspot %u,%u", index, x, y, c->panelWidth,
            c->panelHeight, c->width, c->height, hx, hy);
    if (plane_get(index, &p) < 0)
        return HAL_ERROR;
    p.view[0] = p.view[1] = 0;
    p.view[2] = c->width < p.width ? c->width : p.width;
    p.view[3] = c->height < p.height ? c->height : p.height;
    p.disp[0] = x > hx ? x - hx : 0;
    p.disp[1] = y > hy ? y - hy : 0;
    p.disp[2] = p.view[2];
    p.disp[3] = p.view[3];
    if (!p.view[2] || !p.view[3])
        return HAL_INVALID_PARAM;
    return plane_set(&p);
}
#else
EXPORT int API(MoveCursor)(uint32_t index, cursor_t *c)
{
    (void)index;
    (void)c;
    return HAL_OK;
}
#endif

#if SS_ONLY
EXPORT int API(SetCursorPosition)(cursor_position_t *c)
{
    (void)c;
    return HAL_OK;
}
#endif

#if SS_ONLY
EXPORT int API(SetCursorResolution)(resolution_t coordinate, resolution_t image)
{
    TRACE("SetCursorResolution %ux%u %ux%u", coordinate.width, coordinate.height, image.width,
          image.height);
    return HAL_OK;
}
#endif
