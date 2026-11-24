// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* procmgr's cursor plane for NetCast 4 (docs/dev/architecture/netcast4.md, "Graphics"): the KADP_*
 * functions of libkadaptor that NC4H's procmgr calls for its cursor (PM_CURSOR_DRAW_*) and its
 * display size (PM_DISPLAY_GetDisplayResolutionCore), on this library's GAL_GX_* and the OSD frame
 * buffer. The stock ones drive LG's fbdev (fb3, a 128x128 double-buffered layer), its 2D engine and
 * its PNG decoder through ioctls of kernel drivers the emulator does not model.
 *
 * They reach procmgr only through lginit_app's LD_PRELOAD of libgal
 * (LD_PRELOAD=/lg/res/lglib/liblibm.so:/mnt/lg/res/lglib/libgal.so): libkadaptor exports the same
 * names, and the preloaded library comes first. Every process gets that preload, and the media
 * elements of mediaframework (lgtssink, lxdesink, the OpenMAX video sink) call KADP_GFX_* too: in
 * any process but procmgr each function here passes its call on to libkadaptor's
 * (dlsym(RTLD_NEXT)), unchanged. KADP_InitSystem is not taken over: procmgr's goes to libkadaptor.
 *
 * The ABI, from 04.28.15's procmgr and libkadaptor: the KADP surface is 48 bytes (fd, type, u16
 * w, h, stride, format, phys, offset, length, vaddr, flags); fbdev's LX_FBDEV_CFG_T 64 bytes (per
 * layer at 8 + 12 i: flags, u16 max_xres, max_yres, max_size) and LX_FBDEV_CONFIG_T 40;
 * MoveCursor's argument six s16 (drawn size, hotspot, position in panel pixels). Here a KADP
 * surface is a GAL surface of the pool; its fd is the slot that holds it (1-based). fb3's two
 * buffers are GAL surfaces shown on GAL controller 2 (the stock libgal's fb3), the one
 * UpdateScreenByIndex names. */
#define _GNU_SOURCE
#include "gal.h"
#include <dlfcn.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))

/* this library's GAL_GX_* (halgal.c) */
int GAL_GX_CreateSurface(uint16_t w, uint16_t h, uint32_t format, surface_t *s);
int GAL_GX_DestroySurface(surface_t *s);
int GAL_GX_FillRectangle(surface_t *dst, rect_t *r, uint32_t color, uint32_t *flags,
                         void *settings);
int GAL_GX_StretchBlit(surface_t *src, rect_t *sr, surface_t *dst, rect_t *dr, uint32_t *flags,
                       void *settings);
int GAL_GX_SetFrameBuffer(surface_t *s, uint32_t index);
int GAL_GX_SetFBHWControllerVisibility(void *list, uint32_t n);
int GAL_GX_MoveCursor(uint32_t index, cursor_t *c);
typedef struct { /* HAL_GAL_IMAGE_INFO_T, as halgal.c's */
    uint32_t imageFormat;
    rect_t rect;
    uint32_t imageLength, pixelFormat;
    uint8_t bImageFromBuffer, pad[3];
    const char *imagePath;
    const void *imagePtr;
} image_info_t;
int GAL_GX_DecodeImage(image_info_t *info, surface_t *dst, uint32_t flags);

typedef struct { /* KADP_SURFACE_T */
    int32_t fd;
    uint32_t type;
    uint16_t w, h;
    uint32_t stride, format, phys, offset, length, vaddr, flags;
    uint32_t reserved[2];
} kadp_surface_t;
_Static_assert(sizeof(kadp_surface_t) == 48, "KADP surface");

typedef struct { /* LX_GFX_SURFACE_SETTING_T */
    int32_t fd;
    uint32_t type, format;
    int16_t w, h;
    uint32_t align, stride, phys, mmap;
} kadp_setting_t;
_Static_assert(sizeof(kadp_setting_t) == 32, "LX_GFX_SURFACE_SETTING_T");

typedef struct {
    uint16_t x, y, w, h;
} rect16_t;

#define KADP_OK      0
#define KADP_ERROR   (-1)
#define KADP_INVALID (-5) /* not opened, or a bad argument */

#define CURSOR_CONTROLLER 2 /* GAL controller of fb3 */
#define CURSOR_SIZE       128
#define FBS               4

/* is this procmgr: else every call goes to libkadaptor's */
static int ours(void)
{
    static int is = -1;
    if (is < 0) {
        char exe[256];
        ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
        exe[n > 0 ? n : 0] = 0;
        const char *base = strrchr(exe, '/');
        is = strcmp(base ? base + 1 : exe, "procmgr") == 0;
    }
    return is;
}

#define PASS(name, ...)                                                                            \
    do {                                                                                           \
        if (!ours()) {                                                                             \
            static void *next;                                                                     \
            if (!next)                                                                             \
                next = dlsym(RTLD_NEXT, #name);                                                    \
            if (!next)                                                                             \
                return KADP_ERROR;                                                                 \
            return ((__typeof__(&name))next)(__VA_ARGS__);                                         \
        }                                                                                          \
    } while (0)

static void say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fputs("[libgal] kadp: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;

/* ---- surfaces: GAL surfaces by slot ---- */

#define SLOTS 64
static surface_t *slot[SLOTS];

/* KADP's pixel formats (LX_GFX_PIXEL_FORMAT) to GAL's */
static int gal_format(uint32_t f)
{
    switch (f) {
    case 0x1e:
        return FMT_ARGB;
    case 0x04:
        return FMT_LUT8;
    case 0x1a:
        return FMT_ARGB1555;
    case 0x19:
        return FMT_RGB16;
    case 0x1b:
        return FMT_ARGB4444;
    case 0x05:
        return FMT_A8;
    default:
        return -1;
    }
}

/* a new GAL surface in a slot, described in k: 0, or an error */
static int surface_new(uint16_t w, uint16_t h, uint32_t format, uint32_t type, kadp_surface_t *k)
{
    int gf = gal_format(format), i;
    surface_t *s;
    if (gf < 0 || !w || !h)
        return KADP_INVALID;
    s = calloc(1, sizeof(*s));
    if (!s)
        return KADP_ERROR;
    if (GAL_GX_CreateSurface(w, h, (uint32_t)gf, s)) {
        free(s);
        return KADP_ERROR;
    }
    pthread_mutex_lock(&lock);
    for (i = 0; i < SLOTS && slot[i]; i++)
        ;
    if (i < SLOTS)
        slot[i] = s;
    pthread_mutex_unlock(&lock);
    if (i == SLOTS) {
        GAL_GX_DestroySurface(s);
        free(s);
        return KADP_ERROR;
    }
    memset(k, 0, sizeof(*k));
    k->fd = i + 1;
    k->type = type;
    k->w = w;
    k->h = h;
    k->stride = s->pitch;
    k->format = format;
    k->phys = s->physicalAddress;
    k->offset = s->offset;
    k->length = (uint32_t)s->pitch * h;
    return KADP_OK;
}

static surface_t *surface_of(const kadp_surface_t *k)
{
    surface_t *s = NULL;
    if (!k || k->fd < 1 || k->fd > SLOTS)
        return NULL;
    pthread_mutex_lock(&lock);
    s = slot[k->fd - 1];
    pthread_mutex_unlock(&lock);
    return s;
}

static surface_t *surface_at(uint32_t phys)
{
    surface_t *s = NULL;
    pthread_mutex_lock(&lock);
    for (int i = 0; i < SLOTS && !s; i++)
        if (slot[i] && slot[i]->physicalAddress == phys)
            s = slot[i];
    pthread_mutex_unlock(&lock);
    return s;
}

/* r as a GAL rect: NULL (the whole surface) for NULL or all zeros */
static rect_t *rect_of(const rect16_t *r, rect_t *out)
{
    if (!r || (!r->x && !r->y && !r->w && !r->h))
        return NULL;
    *out = (rect_t){r->x, r->y, r->w, r->h};
    return out;
}

/* ---- KADP_GFX ---- */

EXPORT int KADP_GFX_Open(void)
{
    PASS(KADP_GFX_Open);
    return KADP_OK;
}

EXPORT int KADP_GFX_MakeSurfaceSettingEx(kadp_setting_t *s, uint32_t type, uint32_t format,
                                         int16_t w, int16_t h, uint32_t align, uint32_t stride,
                                         uint32_t phys, uint32_t mmap)
{
    PASS(KADP_GFX_MakeSurfaceSettingEx, s, type, format, w, h, align, stride, phys, mmap);
    if (!s)
        return KADP_INVALID;
    s->type = type;
    s->format = format;
    s->w = w;
    s->h = h;
    s->align = align;
    s->stride = stride;
    s->phys = phys;
    s->mmap = mmap;
    return KADP_OK;
}

EXPORT int KADP_GFX_AllocSurfaceEx(kadp_surface_t **pp, const kadp_setting_t *s)
{
    PASS(KADP_GFX_AllocSurfaceEx, pp, s);
    kadp_surface_t *k;
    int err;
    if (!pp || !s || s->w <= 0 || s->h <= 0)
        return KADP_INVALID;
    *pp = NULL;
    k = calloc(1, sizeof(*k));
    if (!k)
        return KADP_ERROR;
    err = surface_new((uint16_t)s->w, (uint16_t)s->h, s->format, s->type, k);
    if (err) {
        say("AllocSurfaceEx %dx%d format 0x%x: %d", s->w, s->h, s->format, err);
        free(k);
        return err;
    }
    k->flags = 2; /* the struct is the library's */
    *pp = k;
    return KADP_OK;
}

EXPORT int KADP_GFX_DestroySurface(kadp_surface_t *k)
{
    PASS(KADP_GFX_DestroySurface, k);
    surface_t *s = surface_of(k);
    if (!s)
        return KADP_INVALID;
    pthread_mutex_lock(&lock);
    slot[k->fd - 1] = NULL;
    pthread_mutex_unlock(&lock);
    GAL_GX_DestroySurface(s);
    free(s);
    if (k->flags & 2)
        free(k);
    else
        k->fd = 0;
    return KADP_OK;
}

EXPORT int KADP_GFX_SimpleFillRect(int port, kadp_surface_t *dst, rect16_t *r, uint32_t color)
{
    PASS(KADP_GFX_SimpleFillRect, port, dst, r, color);
    surface_t *s = surface_of(dst);
    uint32_t flags = 0;
    rect_t g;
    if (!s)
        return KADP_INVALID;
    return GAL_GX_FillRectangle(s, rect_of(r, &g), color, &flags, NULL) ? KADP_ERROR : KADP_OK;
}

EXPORT int KADP_GFX_SimpleStretchBlit(int port, kadp_surface_t *src, rect16_t *sr,
                                      kadp_surface_t *dst, rect16_t *dr)
{
    PASS(KADP_GFX_SimpleStretchBlit, port, src, sr, dst, dr);
    surface_t *s = surface_of(src), *d = surface_of(dst);
    uint32_t flags = 0; /* a copy */
    rect_t gs, gd;
    if (!s || !d)
        return KADP_INVALID;
    return GAL_GX_StretchBlit(s, rect_of(sr, &gs), d, rect_of(dr, &gd), &flags, NULL) ? KADP_ERROR
                                                                                      : KADP_OK;
}

/* ---- KADP_PNG ---- */

EXPORT int KADP_PNG_Open(void)
{
    PASS(KADP_PNG_Open);
    return KADP_OK;
}

/* the PNG in png (size bytes) into the surface at out->phys, at the picture's size */
struct png_out {
    uint32_t phys, size;
};

EXPORT int KADP_PNG_DecodeImage(const void *png, uint32_t size, struct png_out *out)
{
    PASS(KADP_PNG_DecodeImage, png, size, out);
    image_info_t info = {.imageFormat = 1, .imageLength = size, .bImageFromBuffer = 1};
    surface_t *s;
    if (!png || !size || !out || !(s = surface_at(out->phys)))
        return KADP_INVALID;
    info.imagePtr = png;
    return GAL_GX_DecodeImage(&info, s, 0) ? KADP_ERROR : KADP_OK;
}

/* ---- KADP_FBDEV ---- */

typedef struct { /* LX_FBDEV_CONFIG_T, NC4H's 40 bytes */
    uint16_t xres, yres, xres_virtual, yres_virtual;
    uint32_t bpp;
    uint16_t input_win[4], output_win[4];
    uint32_t rest[3];
} fb_config_t;
_Static_assert(sizeof(fb_config_t) == 40, "LX_FBDEV_CONFIG_T");

typedef struct { /* LX_FBDEV_CFG_T */
    uint32_t chip, rev;
    struct {
        uint32_t flags;
        uint16_t max_xres, max_yres;
        uint32_t max_size;
    } fb[4];
    uint32_t pad[2];
} fb_cfg_t;
_Static_assert(sizeof(fb_cfg_t) == 64, "LX_FBDEV_CFG_T");
#define FB_BUILTIN_FBMEM (1u << 26)
#define FB_BUF_NUM(n)    ((uint32_t)(n) << 5)

static unsigned opened;           /* fb ids opened */
static fb_config_t config[FBS];   /* each fb's configuration */
static kadp_surface_t fb3_buf[2]; /* fb3's two buffers */
static int fb3_shown = -1, fb3_visible;

/* the panel's size: the GOP plane's output, the target's screen (1920x1080) */
#define PANEL_W 1920
#define PANEL_H 1080

EXPORT int KADP_FBDEV_Open(int id)
{
    PASS(KADP_FBDEV_Open, id);
    if (id < 0 || id >= FBS)
        return KADP_INVALID;
    pthread_mutex_lock(&lock);
    if (!(opened & (1u << id))) {
        uint16_t w = id == 3 ? CURSOR_SIZE : PANEL_W, h = id == 3 ? CURSOR_SIZE : PANEL_H;
        config[id] = (fb_config_t){.xres = w,
                                   .yres = h,
                                   .xres_virtual = w,
                                   .yres_virtual = (uint16_t)(h * 2),
                                   .bpp = 32,
                                   .input_win = {0, 0, w, h},
                                   .output_win = {0, 0, w, h}};
        opened |= 1u << id;
    }
    pthread_mutex_unlock(&lock);
    say("FBDEV_Open %d", id);
    return KADP_OK;
}

EXPORT int KADP_FBDEV_GetCfg(fb_cfg_t *cfg)
{
    PASS(KADP_FBDEV_GetCfg, cfg);
    if (!cfg)
        return KADP_INVALID;
    memset(cfg, 0, sizeof(*cfg));
    for (int i = 0; i < FBS; i++) {
        uint16_t w = i == 3 ? CURSOR_SIZE : PANEL_W, h = i == 3 ? CURSOR_SIZE : PANEL_H;
        cfg->fb[i].flags = FB_BUILTIN_FBMEM | FB_BUF_NUM(1);
        cfg->fb[i].max_xres = w;
        cfg->fb[i].max_yres = h;
        cfg->fb[i].max_size = (uint32_t)w * h * 4 * 2;
    }
    return KADP_OK;
}

EXPORT int KADP_FBDEV_GetConfiguration(int id, fb_config_t *c)
{
    PASS(KADP_FBDEV_GetConfiguration, id, c);
    if (id < 0 || id >= FBS || !(opened & (1u << id)) || !c)
        return KADP_INVALID;
    pthread_mutex_lock(&lock);
    *c = config[id];
    pthread_mutex_unlock(&lock);
    return KADP_OK;
}

EXPORT int KADP_FBDEV_SetConfiguration(int id, fb_config_t *c)
{
    PASS(KADP_FBDEV_SetConfiguration, id, c);
    if (id < 0 || id >= FBS || !(opened & (1u << id)) || !c)
        return KADP_INVALID;
    pthread_mutex_lock(&lock);
    config[id] = *c;
    pthread_mutex_unlock(&lock);
    say("FBDEV_SetConfiguration %d: input %ux%u", id, c->input_win[2], c->input_win[3]);
    return KADP_OK;
}

/* fb3's buffers, made on first use: the cursor's size, ARGB8888 */
static int fb3_ready(void)
{
    for (int i = 0; i < 2; i++)
        if (!fb3_buf[i].fd && surface_new(CURSOR_SIZE, CURSOR_SIZE, 0x1e, 2, &fb3_buf[i]))
            return 0;
    return 1;
}

EXPORT int KADP_FBDEV_GetSurface(int id, int idx, kadp_surface_t *out)
{
    PASS(KADP_FBDEV_GetSurface, id, idx, out);
    if (id != 3 || idx < 0 || idx > 1 || !(opened & (1u << id)) || !out)
        return KADP_INVALID; /* procmgr draws into fb3 alone */
    if (!fb3_ready())
        return KADP_ERROR;
    *out = fb3_buf[idx];
    return KADP_OK;
}

EXPORT int KADP_FBDEV_GetDrawScreenIdx(int id)
{
    PASS(KADP_FBDEV_GetDrawScreenIdx, id);
    if (id != 3 || !(opened & (1u << id)))
        return KADP_INVALID;
    return fb3_shown == 0 ? 1 : 0;
}

static int cursor_visibility(void)
{
    uint32_t v[2] = {CURSOR_CONTROLLER, (uint32_t)(fb3_visible && fb3_shown >= 0)};
    return GAL_GX_SetFBHWControllerVisibility(v, 1);
}

EXPORT int KADP_FBDEV_UpdateScreenByIndex(int id, int idx)
{
    PASS(KADP_FBDEV_UpdateScreenByIndex, id, idx);
    surface_t *s;
    if (id != 3 || idx < 0 || idx > 1 || !(opened & (1u << id)) || !fb3_ready())
        return KADP_INVALID;
    s = surface_of(&fb3_buf[idx]);
    if (!s || GAL_GX_SetFrameBuffer(s, CURSOR_CONTROLLER))
        return KADP_ERROR;
    if (fb3_shown < 0) {
        fb3_shown = idx;
        cursor_visibility();
    }
    fb3_shown = idx;
    return KADP_OK;
}

EXPORT int KADP_FBDEV_SetVisible(int id, int visible)
{
    PASS(KADP_FBDEV_SetVisible, id, visible);
    if (id != 3 || !(opened & (1u << id)))
        return KADP_INVALID;
    fb3_visible = !!visible;
    return cursor_visibility() ? KADP_ERROR : KADP_OK;
}

/* {drawn w, h; hotspot x, y; position x, y}, in panel pixels */
EXPORT int KADP_FBDEV_MoveCursor(int id, const int16_t *p)
{
    PASS(KADP_FBDEV_MoveCursor, id, p);
    cursor_t c;
    if (id != 3 || !(opened & (1u << id)) || !p)
        return KADP_INVALID;
    if (fb3_shown < 0)
        return KADP_OK; /* nothing shown yet */
    c = (cursor_t){.x = p[4],
                   .y = p[5],
                   .panelWidth = PANEL_W,
                   .panelHeight = PANEL_H,
                   .width = (uint32_t)(p[0] > 0 ? p[0] : 0),
                   .height = (uint32_t)(p[1] > 0 ? p[1] : 0),
                   .hotspot = 1,
                   .hotX = p[2],
                   .hotY = p[3]};
    return GAL_GX_MoveCursor(CURSOR_CONTROLLER, &c) ? KADP_ERROR : KADP_OK;
}
