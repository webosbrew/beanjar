// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The software 2D engine of the soc layer's DILE_GAL (guest/layers/soc/src/libdile_gal/gal-draw.c)
 * on the host: its quick 1:1 paths (a copy within a format, LUT8 onto ARGB through the palette,
 * a copy within one surface that overlaps), its per-pixel path (stretched, mirrored) and its fills
 * give the pixels worked out here one by one. Prints "ok" or what differed. */
#include "../../../guest/layers/soc/src/libdile_gal/gal-draw.c"
#include <stdio.h>

static int failures;
#define CHECK(c, ...)                                                                              \
    do {                                                                                           \
        if (!(c)) {                                                                                \
            printf(__VA_ARGS__);                                                                   \
            printf("\n");                                                                          \
            failures++;                                                                            \
        }                                                                                          \
    } while (0)

#define W     37
#define H     11
#define PITCH 48 /* LUT8 rows with padding */

static uint32_t palette[200];

static void fill_lut8(uint8_t *p)
{
    for (int y = 0; y < H; y++)
        for (int x = 0; x < PITCH; x++)
            p[y * PITCH + x] = (uint8_t)(x < W ? (x * 7 + y * 13) % 256 : 0xEE);
}

int main(void)
{
    for (unsigned i = 0; i < 200; i++)
        palette[i] = 0xFF000000u | i * 0x010203u;
    static uint8_t a[H * PITCH], b[H * PITCH];
    static uint32_t argb[H * W];
    struct pixmap la = {a, PITCH, W, H, FMT_LUT8, palette, 200};
    struct pixmap lb = {b, PITCH, W, H, FMT_LUT8, palette, 200};
    struct pixmap pa = {(uint8_t *)argb, W * 4, W, H, FMT_ARGB, NULL, 0};

    /* LUT8 onto LUT8, 1:1, a part: the indices, the rest untouched (libgm-wayland's copy of the
     * caption rows from one buffer to the other) */
    fill_lut8(a);
    memset(b, 0x11, sizeof b);
    rect_t part = {3, 2, 20, 5};
    CHECK(gal_blit(&la, &part, &lb, &part, 0, NULL) == GAL_OK, "LUT8 copy failed");
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) {
            int in = x >= 3 && x < 23 && y >= 2 && y < 7;
            uint8_t want = in ? a[y * PITCH + x] : 0x11;
            CHECK(b[y * PITCH + x] == want, "LUT8 copy %d,%d: %u, not %u", x, y, b[y * PITCH + x],
                  want);
        }

    /* LUT8 onto ARGB, 1:1, whole: the palette's colours, 0 past its end (the stretch of the
     * caption window to the pixmap it presents) */
    rect_t whole = {0, 0, W, H};
    memset(argb, 0x55, sizeof argb);
    CHECK(gal_blit(&la, &whole, &pa, &whole, 0, NULL) == GAL_OK, "LUT8 to ARGB failed");
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) {
            uint8_t i = a[y * PITCH + x];
            uint32_t want = i < 200 ? palette[i] : 0;
            CHECK(argb[y * W + x] == want, "LUT8 to ARGB %d,%d: %08x, not %08x", x, y,
                  argb[y * W + x], want);
        }
    /* and without a palette, every pixel 0 */
    struct pixmap nopal = la;
    nopal.palette = NULL;
    nopal.palette_length = 0;
    CHECK(gal_blit(&nopal, &whole, &pa, &whole, 0, NULL) == GAL_OK, "LUT8 to ARGB failed");
    for (int i = 0; i < W * H; i++)
        CHECK(argb[i] == 0, "LUT8 without a palette to ARGB %d: %08x", i, argb[i]);

    /* A copy within one surface whose rectangles overlap (moving rows down): the source as it was
     */
    fill_lut8(a);
    static uint8_t orig[H * PITCH];
    memcpy(orig, a, sizeof a);
    rect_t from = {0, 0, W, 8}, to = {0, 3, W, 8};
    CHECK(gal_blit(&la, &from, &la, &to, 0, NULL) == GAL_OK, "overlapping copy failed");
    for (int y = 3; y < 11; y++)
        for (int x = 0; x < W; x++)
            CHECK(a[y * PITCH + x] == orig[(y - 3) * PITCH + x], "overlapping copy %d,%d", x, y);

    /* Stretched and mirrored LUT8 onto ARGB: the per-pixel path, nearest samples */
    fill_lut8(a);
    rect_t src = {2, 1, 10, 4}, dst = {1, 2, 30, 7};
    memset(argb, 0, sizeof argb);
    uint32_t flags = BLIT_HORIZONTAL_MIRROR | BLIT_VERTICAL_MIRROR;
    CHECK(gal_blit(&la, &src, &pa, &dst, flags, NULL) == GAL_OK, "stretched blit failed");
    for (unsigned y = 0; y < dst.h; y++)
        for (unsigned x = 0; x < dst.w; x++) {
            unsigned sx = src.x + (dst.w - 1 - x) * src.w / dst.w;
            unsigned sy = src.y + (dst.h - 1 - y) * src.h / dst.h;
            uint8_t i = a[sy * PITCH + sx];
            uint32_t want = i < 200 ? palette[i] : 0;
            uint32_t got = argb[(dst.y + y) * W + dst.x + x];
            CHECK(got == want, "stretched %u,%u: %08x, not %08x", x, y, got, want);
        }

    /* Fills without blending: the colour once in the surface's format. LUT8: the nearest palette
     * index, also on a plane whose pitch is wider than its rows (legacy-broadcast clearing its
     * 1366-pixel caption plane, 1376 bytes a row), the padding untouched */
    memset(a, 0xEE, sizeof a);
    rect_t all = {0, 0, W, H};
    CHECK(gal_fill(&la, &all, palette[77] + 0x010000, 0, NULL) == GAL_OK, "LUT8 fill failed");
    for (int y = 0; y < H; y++)
        for (int x = 0; x < PITCH; x++)
            CHECK(a[y * PITCH + x] == (x < W ? 77 : 0xEE), "LUT8 fill %d,%d: %u", x, y,
                  a[y * PITCH + x]);
    CHECK(gal_fill(&la, &part, 0, 0, NULL) == GAL_OK, "LUT8 clear failed");
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) {
            int in = x >= 3 && x < 23 && y >= 2 && y < 7;
            CHECK(a[y * PITCH + x] == (in ? 0 : 77), "LUT8 clear %d,%d: %u", x, y,
                  a[y * PITCH + x]);
        }
    /* ARGB and ARGB4444: every pixel of the rectangle the colour */
    memset(argb, 0, sizeof argb);
    CHECK(gal_fill(&pa, &part, 0x80402010u, 0, NULL) == GAL_OK, "ARGB fill failed");
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++) {
            int in = x >= 3 && x < 23 && y >= 2 && y < 7;
            CHECK(argb[y * W + x] == (in ? 0x80402010u : 0), "ARGB fill %d,%d: %08x", x, y,
                  argb[y * W + x]);
        }
    static uint16_t h16[H * W];
    struct pixmap p4444 = {(uint8_t *)h16, W * 2, W, H, FMT_ARGB4444, NULL, 0};
    CHECK(gal_fill(&p4444, &all, 0xF0A05030u, 0, NULL) == GAL_OK, "ARGB4444 fill failed");
    for (int i = 0; i < W * H; i++)
        CHECK(h16[i] == 0xFA53, "ARGB4444 fill %d: %04x", i, h16[i]);

    if (failures)
        return 1;
    printf("ok\n");
    return 0;
}
