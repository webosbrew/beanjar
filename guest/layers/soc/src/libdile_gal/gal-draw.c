// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libdile_gal: the 2D engine of the emulator's DILE_GAL, in software: rectangle fills and
 * (stretched) blits between surfaces of the six DILE_GAL pixel formats, with the drawing and
 * blitting flags and blend functions of dile_gal.h (DirectFB's semantics, which the DILE API
 * follows). Scaling samples the nearest pixel.
 *
 * Common cases have their own loops: 1:1 copies within a format, LUT8 onto ARGB through the
 * palette, fills without blending (the colour converted once), and ARGB "source over" (alpha
 * channel blending with SRCALPHA or ONE against INVSRCALPHA). Everything else goes through one
 * per-pixel path in ARGB32, with the nearest-sample source coordinates worked out once a row and a
 * column.
 *
 * Speed is part of the behaviour: callers wait for the display engine with a timeout.
 * legacy-broadcast's SGL (API_SGL_UpdateScreen) waits 300 ms for its update task, which copies the
 * caption window's 1366x768 LUT8 plane and converts it to ARGB on every update; past the timeout
 * it goes on as if the update were done, its caption writer draws into the buffer the update is
 * still presenting, and the captions never show. A divide per pixel (64-bit, a library call on
 * 32-bit ARM) made each of those passes take over 100 ms on a slow host, and clearing the plane
 * (a fill of LUT8 colour 0) searched the palette for every pixel: two seconds. */
#include "gal.h"
#include <stdlib.h>
#include <string.h>

unsigned gal_bpp(unsigned format)
{
    static const unsigned bpp[FMT_COUNT] = {32, 8, 16, 16, 16, 8};
    return format < FMT_COUNT ? bpp[format] : 0;
}

static inline uint32_t expand(unsigned v, unsigned bits)
{
    return bits == 1 ? (v ? 255 : 0) : (v << (8 - bits)) | (v >> (2 * bits - 8));
}

static uint32_t nearest_index(const struct pixmap *p, uint32_t argb)
{
    unsigned best = 0, best_d = ~0u;
    for (unsigned i = 0; i < p->palette_length && i < 256; i++) {
        uint32_t c = p->palette[i];
        unsigned d = 0;
        for (int s = 0; s < 32; s += 8) {
            int e = (int)((c >> s) & 255) - (int)((argb >> s) & 255);
            d += (unsigned)(e * e);
        }
        if (d < best_d) {
            best_d = d;
            best = i;
        }
    }
    return best;
}

static inline uint32_t get_px(const struct pixmap *p, unsigned x, unsigned y)
{
    const uint8_t *row = p->base + (size_t)y * p->pitch;
    uint32_t v;
    switch (p->format) {
    case FMT_ARGB:
        return ((const uint32_t *)row)[x];
    case FMT_LUT8:
        v = row[x];
        return p->palette && v < p->palette_length ? p->palette[v] : 0;
    case FMT_ARGB1555:
        v = ((const uint16_t *)row)[x];
        return expand(v >> 15, 1) << 24 | expand((v >> 10) & 31, 5) << 16 |
               expand((v >> 5) & 31, 5) << 8 | expand(v & 31, 5);
    case FMT_RGB16:
        v = ((const uint16_t *)row)[x];
        return 0xFF000000u | expand(v >> 11, 5) << 16 | expand((v >> 5) & 63, 6) << 8 |
               expand(v & 31, 5);
    case FMT_ARGB4444:
        v = ((const uint16_t *)row)[x];
        return expand(v >> 12, 4) << 24 | expand((v >> 8) & 15, 4) << 16 |
               expand((v >> 4) & 15, 4) << 8 | expand(v & 15, 4);
    case FMT_A8:
        return (uint32_t)row[x] << 24 | 0xFFFFFFu;
    }
    return 0;
}

static inline uint32_t get_raw(const struct pixmap *p, unsigned x, unsigned y)
{
    const uint8_t *row = p->base + (size_t)y * p->pitch;
    switch (gal_bpp(p->format)) {
    case 32:
        return ((const uint32_t *)row)[x];
    case 16:
        return ((const uint16_t *)row)[x];
    default:
        return row[x];
    }
}

static inline void put_px(struct pixmap *p, unsigned x, unsigned y, uint32_t c)
{
    uint8_t *row = p->base + (size_t)y * p->pitch;
    unsigned a = c >> 24, r = (c >> 16) & 255, g = (c >> 8) & 255, b = c & 255;
    switch (p->format) {
    case FMT_ARGB:
        ((uint32_t *)row)[x] = c;
        break;
    case FMT_LUT8:
        row[x] = (uint8_t)nearest_index(p, c);
        break;
    case FMT_ARGB1555:
        ((uint16_t *)row)[x] = (uint16_t)((a >> 7) << 15 | (r >> 3) << 10 | (g >> 3) << 5 | b >> 3);
        break;
    case FMT_RGB16:
        ((uint16_t *)row)[x] = (uint16_t)((r >> 3) << 11 | (g >> 2) << 5 | b >> 3);
        break;
    case FMT_ARGB4444:
        ((uint16_t *)row)[x] = (uint16_t)((a >> 4) << 12 | (r >> 4) << 8 | (g >> 4) << 4 | b >> 4);
        break;
    case FMT_A8:
        row[x] = (uint8_t)a;
        break;
    }
}

static inline unsigned mul8(unsigned a, unsigned b)
{
    unsigned t = a * b + 128;
    return (t + (t >> 8)) >> 8;
}

static inline uint32_t premultiply(uint32_t c)
{
    unsigned a = c >> 24;
    return (uint32_t)a << 24 | mul8((c >> 16) & 255, a) << 16 | mul8((c >> 8) & 255, a) << 8 |
           mul8(c & 255, a);
}

static inline uint32_t demultiply(uint32_t c)
{
    unsigned a = c >> 24;
    if (!a)
        return 0;
    unsigned r = ((c >> 16) & 255) * 255 / a, g = ((c >> 8) & 255) * 255 / a,
             b = (c & 255) * 255 / a;
    return (uint32_t)a << 24 | (r > 255 ? 255 : r) << 16 | (g > 255 ? 255 : g) << 8 |
           (b > 255 ? 255 : b);
}

/* one channel's factor; ch 3 is alpha */
static inline unsigned factor(uint32_t fn, uint32_t s, uint32_t d, int ch)
{
    unsigned sa = s >> 24, da = d >> 24, sc = (s >> (ch * 8)) & 255, dc = (d >> (ch * 8)) & 255;
    switch (fn) {
    case BLEND_ZERO:
        return 0;
    case BLEND_ONE:
        return 255;
    case BLEND_SRCCOLOR:
        return sc;
    case BLEND_INVSRCCOLOR:
        return 255 - sc;
    case BLEND_SRCALPHA:
        return sa;
    case BLEND_INVSRCALPHA:
        return 255 - sa;
    case BLEND_DESTALPHA:
        return da;
    case BLEND_INVDESTALPHA:
        return 255 - da;
    case BLEND_DESTCOLOR:
        return dc;
    case BLEND_INVDESTCOLOR:
        return 255 - dc;
    case BLEND_SRCALPHASAT:
        return ch == 3 ? 255 : (sa < 255 - da ? sa : 255 - da);
    }
    return 0;
}

static inline uint32_t blend(uint32_t s, uint32_t d, uint32_t fs, uint32_t fd)
{
    uint32_t out = 0;
    for (int ch = 0; ch < 4; ch++) {
        unsigned v = mul8((s >> (ch * 8)) & 255, factor(fs, s, d, ch)) +
                     mul8((d >> (ch * 8)) & 255, factor(fd, s, d, ch));
        out |= (uint32_t)(v > 255 ? 255 : v) << (ch * 8);
    }
    return out;
}

static int clip_ok(const struct pixmap *p, const rect_t *r)
{
    return r->w && r->h && (unsigned)r->x + r->w <= p->width && (unsigned)r->y + r->h <= p->height;
}

int gal_fill(struct pixmap *dst, const rect_t *r, uint32_t argb, uint32_t flags,
             const draw_settings_t *settings)
{
    uint32_t fs = BLEND_SRCALPHA, fd = BLEND_INVSRCALPHA, key = 0;
    if (!clip_ok(dst, r))
        return GAL_OK;
    if (settings) {
        if (settings->srcBlend)
            fs = settings->srcBlend;
        if (settings->dstBlend)
            fd = settings->dstBlend;
        key = settings->dstColorkey;
    }
    if (flags & DRAW_SRC_PREMULTIPLY)
        argb = premultiply(argb);
    if (!(flags & (DRAW_BLEND | DRAW_DST_COLORKEY | DRAW_DST_PREMULTIPLY | DRAW_XOR))) {
        /* every pixel the same: the colour in the surface's format once (a LUT8 surface's nearest
         * palette index is a search of the palette), then whole rows */
        uint32_t word = 0; /* aligned for put_px's 16- and 32-bit stores */
        uint8_t *px = (uint8_t *)&word;
        struct pixmap one = *dst;
        unsigned bytes = gal_bpp(dst->format) / 8;
        one.base = px;
        one.pitch = sizeof word;
        put_px(&one, 0, 0, flags & DRAW_DEMULTIPLY ? demultiply(argb) : argb);
        int same = bytes == 1 || !memcmp(px, px + 1, bytes - 1);
        for (unsigned y = r->y; y < (unsigned)r->y + r->h; y++) {
            uint8_t *row = dst->base + (size_t)y * dst->pitch + (size_t)r->x * bytes;
            if (same) {
                memset(row, px[0], (size_t)r->w * bytes);
                continue;
            }
            if (bytes == 4)
                for (unsigned x = 0; x < r->w; x++)
                    ((uint32_t *)row)[x] = word;
            else
                for (unsigned x = 0; x < r->w; x++)
                    ((uint16_t *)row)[x] = (uint16_t)word;
        }
        return GAL_OK;
    }
    for (unsigned y = r->y; y < (unsigned)r->y + r->h; y++)
        for (unsigned x = r->x; x < (unsigned)r->x + r->w; x++) {
            if ((flags & DRAW_DST_COLORKEY) && get_raw(dst, x, y) != key)
                continue;
            uint32_t c = argb;
            if (flags & (DRAW_BLEND | DRAW_DST_PREMULTIPLY | DRAW_XOR)) {
                uint32_t d = get_px(dst, x, y);
                if (flags & DRAW_DST_PREMULTIPLY)
                    d = premultiply(d);
                if (flags & DRAW_BLEND)
                    c = blend(c, d, fs, fd);
                if (flags & DRAW_XOR)
                    c ^= d;
            }
            if (flags & DRAW_DEMULTIPLY)
                c = demultiply(c);
            put_px(dst, x, y, c);
        }
    return GAL_OK;
}

/* 1:1 blits a display engine does in a moment: a copy within a format, LUT8 onto ARGB through the
 * palette (as get_px: an index past the palette is 0), and ARGB onto ARGB with the alpha channel
 * blend (the common UI blits) */
static int fast_blit(const struct pixmap *src, const rect_t *sr, struct pixmap *dst,
                     const rect_t *dr, uint32_t flags, uint32_t fs, uint32_t fd)
{
    if (sr->w != dr->w || sr->h != dr->h)
        return 0;
    if (!flags && src->format == dst->format) {
        /* memmove per row, in the direction that survives an overlap within one surface */
        size_t bytes = gal_bpp(src->format) / 8;
        int up = src->base == dst->base && sr->y < dr->y;
        for (unsigned i = 0; i < dr->h; i++) {
            unsigned y = up ? dr->h - 1 - i : i;
            memmove(dst->base + (size_t)(dr->y + y) * dst->pitch + (size_t)dr->x * bytes,
                    src->base + (size_t)(sr->y + y) * src->pitch + (size_t)sr->x * bytes,
                    (size_t)dr->w * bytes);
        }
        return 1;
    }
    if (!flags && src->format == FMT_LUT8 && dst->format == FMT_ARGB && src->base != dst->base) {
        uint32_t lut[256] = {0};
        unsigned n = src->palette ? (src->palette_length < 256 ? src->palette_length : 256) : 0;
        if (n)
            memcpy(lut, src->palette, n * sizeof *lut);
        for (unsigned y = 0; y < dr->h; y++) {
            const uint8_t *s = src->base + (size_t)(sr->y + y) * src->pitch + sr->x;
            uint32_t *d = (uint32_t *)(dst->base + (size_t)(dr->y + y) * dst->pitch) + dr->x;
            for (unsigned x = 0; x < dr->w; x++)
                d[x] = lut[s[x]];
        }
        return 1;
    }
    if (src->format != FMT_ARGB || dst->format != FMT_ARGB)
        return 0;
    if (flags != BLIT_BLEND_ALPHACHANNEL || fd != BLEND_INVSRCALPHA ||
        (fs != BLEND_SRCALPHA && fs != BLEND_ONE) || src->base == dst->base)
        return 0;
    for (unsigned y = 0; y < dr->h; y++) {
        const uint32_t *s =
            (const uint32_t *)(src->base + (size_t)(sr->y + y) * src->pitch) + sr->x;
        uint32_t *d = (uint32_t *)(dst->base + (size_t)(dr->y + y) * dst->pitch) + dr->x;
        for (unsigned x = 0; x < dr->w; x++) {
            uint32_t c = s[x], a = c >> 24;
            if (!a && fs == BLEND_ONE && !(c & 0xFFFFFF))
                continue;
            if (!a && fs == BLEND_SRCALPHA)
                continue;
            if (a == 255) {
                d[x] = c;
                continue;
            }
            d[x] = blend(c, d[x], fs, fd);
        }
    }
    return 1;
}

int gal_blit(const struct pixmap *src, const rect_t *sr, struct pixmap *dst, const rect_t *dr,
             uint32_t flags, const blit_settings_t *settings)
{
    uint32_t fs = BLEND_SRCALPHA, fd = BLEND_INVSRCALPHA, alpha = 255, color = 0xFFFFFFFFu;
    uint32_t skey = 0, dkey = 0;
    struct pixmap copy;
    uint8_t *tmp = NULL;
    if (!clip_ok(src, sr) || !clip_ok(dst, dr))
        return GAL_OK;
    if (settings) {
        if (settings->srcBlend)
            fs = settings->srcBlend;
        if (settings->dstBlend)
            fd = settings->dstBlend;
        alpha = settings->alpha & 255;
        color = settings->color;
        skey = settings->srcColorkey;
        dkey = settings->dstColorkey;
    }
    if (fast_blit(src, sr, dst, dr, flags, fs, fd))
        return GAL_OK;
    /* overlapping source and destination: read from a copy of the source rectangle */
    if (src->base == dst->base) {
        unsigned bytes = (gal_bpp(src->format) + 7) / 8;
        tmp = malloc((size_t)sr->w * sr->h * bytes);
        if (!tmp)
            return GAL_ERR;
        for (unsigned y = 0; y < sr->h; y++)
            memcpy(tmp + (size_t)y * sr->w * bytes,
                   src->base + (size_t)(sr->y + y) * src->pitch + (size_t)sr->x * bytes,
                   (size_t)sr->w * bytes);
        copy = *src;
        copy.base = tmp;
        copy.pitch = sr->w * bytes;
        copy.width = sr->w;
        copy.height = sr->h;
        rect_t r0 = {0, 0, sr->w, sr->h};
        int rc = gal_blit(&copy, &r0, dst, dr, flags, settings);
        free(tmp);
        return rc;
    }
    /* LUT8 to LUT8 without blending keeps the indices */
    int index_copy = src->format == FMT_LUT8 && dst->format == FMT_LUT8 &&
                     !(flags & ~(BLIT_VERTICAL_MIRROR | BLIT_HORIZONTAL_MIRROR | BLIT_SRC_COLORKEY |
                                 BLIT_DST_COLORKEY));
    /* the source column of each destination column, once */
    unsigned *column = malloc((size_t)dr->w * sizeof *column);
    if (!column)
        return GAL_ERR;
    for (unsigned x = 0; x < dr->w; x++) {
        unsigned tx = flags & BLIT_HORIZONTAL_MIRROR ? dr->w - 1 - x : x;
        column[x] = sr->x + (unsigned)((uint64_t)tx * sr->w / dr->w);
    }
    for (unsigned y = 0; y < dr->h; y++) {
        unsigned ty = flags & BLIT_VERTICAL_MIRROR ? dr->h - 1 - y : y;
        unsigned sy = sr->y + (unsigned)((uint64_t)ty * sr->h / dr->h);
        for (unsigned x = 0; x < dr->w; x++) {
            unsigned sx = column[x];
            unsigned dx = dr->x + x, dy = dr->y + y;
            if ((flags & BLIT_SRC_COLORKEY) && get_raw(src, sx, sy) == skey)
                continue;
            if ((flags & BLIT_DST_COLORKEY) && get_raw(dst, dx, dy) != dkey)
                continue;
            if (index_copy) {
                dst->base[(size_t)dy * dst->pitch + dx] = src->base[(size_t)sy * src->pitch + sx];
                continue;
            }
            uint32_t s = get_px(src, sx, sy);
            if (flags & BLIT_BLEND_COLORALPHA)
                s = (s & 0xFFFFFF) |
                    (flags & BLIT_BLEND_ALPHACHANNEL ? mul8(s >> 24, alpha) : alpha) << 24;
            if (flags & BLIT_COLORIZE)
                s = (s & 0xFF000000u) | mul8((s >> 16) & 255, (color >> 16) & 255) << 16 |
                    mul8((s >> 8) & 255, (color >> 8) & 255) << 8 | mul8(s & 255, color & 255);
            if (flags & BLIT_SRC_PREMULTCOLOR)
                s = (s & 0xFF000000u) | mul8((s >> 16) & 255, alpha) << 16 |
                    mul8((s >> 8) & 255, alpha) << 8 | mul8(s & 255, alpha);
            if (flags & BLIT_SRC_PREMULTIPLY)
                s = premultiply(s);
            if (flags & (BLIT_BLEND_ALPHACHANNEL | BLIT_BLEND_COLORALPHA | BLIT_DST_PREMULTIPLY |
                         BLIT_XOR)) {
                uint32_t d = get_px(dst, dx, dy);
                if (flags & BLIT_DST_PREMULTIPLY)
                    d = premultiply(d);
                if (flags & (BLIT_BLEND_ALPHACHANNEL | BLIT_BLEND_COLORALPHA))
                    s = blend(s, d, fs, fd);
                if (flags & BLIT_XOR)
                    s ^= d;
            }
            if (flags & BLIT_DEMULTIPLY)
                s = demultiply(s);
            put_px(dst, dx, dy, s);
        }
    }
    free(column);
    return GAL_OK;
}
