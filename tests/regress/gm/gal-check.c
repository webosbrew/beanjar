// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* guest/layers/soc/h13's DILE_GAL inside webOS: the calls gm, libgm, libim and tvservice make,
 * checked against the pixels in /dev/gfx, and the
 * OSD plane as the display engine reports it to egl-kms (include/webos-gfx.h). Runs next to the
 * live gm and leaves nothing behind:
 *   webos-enter /mnt/host/build/tests/regress/gm/gal-check     (after `host/scripts/vm-launcher put
 * build/tests/regress/gm` on the host) Loads libdile_gal.so.1 at run time, so it is built without
 * the layer. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <glob.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../../../include/webos-gfx.h"

typedef struct {
    uint32_t offset, phys;
    uint16_t pitch, bpp, width, height;
    uint32_t format, palette[256], palette_length, property, vendor;
} surface_t;
typedef struct {
    uint16_t x, y, w, h;
} rect_t;
typedef struct {
    uint32_t src, dst, alpha, color, skey, dkey, mask[3];
} blit_settings_t;
typedef struct {
    uint32_t src, dst, dkey;
} draw_settings_t;
typedef struct {
    uint32_t format;
    rect_t rect;
    uint32_t length, pixel_format;
    uint8_t from_buffer, pad[3];
    const char *path;
    const void *ptr;
} image_t;

static int (*Init)(void);
static int (*GetDeviceCapability)(uint8_t *);
static int (*GetGraphicMemInfo)(uint32_t *);
static int (*CreateSurface)(unsigned, unsigned, unsigned, surface_t *);
static int (*DestroySurface)(surface_t *);
static int (*FillRectangle)(surface_t *, rect_t *, unsigned, uint32_t *, draw_settings_t *);
static int (*Blit)(surface_t *, rect_t *, surface_t *, unsigned, unsigned, uint32_t *,
                   blit_settings_t *);
static int (*StretchBlit)(surface_t *, rect_t *, surface_t *, rect_t *, uint32_t *,
                          blit_settings_t *);
static int (*SetSurfacePalette)(surface_t *, uint32_t *, int);
static int (*DecodeImage)(image_t *, surface_t *, uint32_t);
static int (*GetFrameBufferList)(surface_t **, unsigned *);
static int (*CaptureFrameBuffer)(surface_t *);
static int (*Finalize)(void);

static int fails;
static uint8_t *gfx;

static void check(const char *what, int ok)
{
    printf("%s %s\n", ok ? "PASS" : "FAIL", what);
    fails += !ok;
}

static uint32_t px(const surface_t *s, unsigned x, unsigned y)
{
    return ((uint32_t *)(gfx + s->offset + (size_t)y * s->pitch))[x];
}

static int near(uint32_t a, uint32_t b)
{
    for (int i = 0; i < 32; i += 8) {
        int d = (int)((a >> i) & 255) - (int)((b >> i) & 255);
        if (d < -2 || d > 2)
            return 0;
    }
    return 1;
}

#define LOAD(f) (f = (__typeof__(f))dlsym(lib, hal ? "HAL_GAL_" #f : "DILE_GAL_" #f))

int main(void)
{
    int hal = getenv("GAL_HAL") != NULL;
    const char *library = hal ? "libhalgal.so" : "libdile_gal.so.1";
    void *lib = dlopen(library, RTLD_NOW);
    uint8_t cap[80]; /* Current capabilities include a byte at offset 72. */
    uint32_t size, base, mem[4], mem2[4];
    surface_t a, b, lut, img;
    uint32_t none = 0, blend = 1;
    if (!lib || !LOAD(Init) || !LOAD(GetDeviceCapability) || !LOAD(GetGraphicMemInfo) ||
        !LOAD(CreateSurface) || !LOAD(DestroySurface) || !LOAD(FillRectangle) || !LOAD(Blit) ||
        !LOAD(StretchBlit) || !LOAD(SetSurfacePalette) || (!hal && !LOAD(DecodeImage)) ||
        !LOAD(GetFrameBufferList) || (hal && (!LOAD(CaptureFrameBuffer) || !LOAD(Finalize)))) {
        printf("FAIL %s: %s\n", library, dlerror());
        return 1;
    }
    check("Init", Init() == 0);
    check("GetDeviceCapability", GetDeviceCapability(cap) == 0);
    memcpy(&base, cap + 20, 4);
    memcpy(&size, cap + 24, 4);
    check("192 MiB at 0xA8000000", size == 192u << 20 && base == 0xA8000000u);
    int fd = open("/dev/gfx", O_RDWR);
    gfx = fd < 0 ? MAP_FAILED : mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    check("/dev/gfx maps", gfx != MAP_FAILED);
    if (gfx == MAP_FAILED)
        return 1;
    GetGraphicMemInfo(mem);

    check("CreateSurface ARGB 64x32", CreateSurface(64, 32, 0, &a) == 0 && a.offset &&
                                          !(a.offset & 4095) && a.phys == base + a.offset &&
                                          a.pitch >= 256 && a.bpp == 32);
    CreateSurface(64, 32, 0, &b);
    rect_t all = {0, 0, 64, 32}, left = {0, 0, 32, 32};
    FillRectangle(&a, &all, 0xFFFF0000u, &none, NULL);
    check("FillRectangle", px(&a, 5, 5) == 0xFFFF0000u && px(&a, 63, 31) == 0xFFFF0000u);
    draw_settings_t over = {5, 6, 0}; /* SRCALPHA, INVSRCALPHA */
    FillRectangle(&a, &left, 0x800000FFu, &blend, &over);
    check("FillRectangle blend", near(px(&a, 5, 5), 0xC07F0080u) && px(&a, 40, 5) == 0xFFFF0000u);

    FillRectangle(&b, &all, 0, &none, NULL);
    check("FillRectangle 0 (pages given back)", px(&b, 10, 10) == 0);
    rect_t src = {0, 0, 16, 16};
    Blit(&a, &src, &b, 8, 8, &none, NULL);
    check("Blit copy", px(&b, 8, 8) == px(&a, 0, 0) && px(&b, 7, 8) == 0);
    FillRectangle(&b, &all, 0xFF00FF00u, &none, NULL);
    blit_settings_t bs = {5, 6, 255, 0, 0, 0, {0, 0, 0}};
    Blit(&a, &src, &b, 0, 0, &blend, &bs);
    check("Blit alpha channel over", near(px(&b, 3, 3), 0xCF5F4060u));
    rect_t s2 = {32, 0, 2, 2}, d2 = {40, 20, 8, 8};
    StretchBlit(&a, &s2, &b, &d2, &none, NULL);
    check("StretchBlit", px(&b, 47, 27) == 0xFFFF0000u && px(&b, 39, 20) == 0xFF00FF00u);

    uint32_t pal[4] = {0xFF000000u, 0xFF112233u, 0x80445566u, 0xFFFFFFFFu};
    check("CreateSurface LUT8", CreateSurface(16, 16, 1, &lut) == 0 && lut.bpp == 8);
    SetSurfacePalette(&lut, pal, 4);
    memset(gfx + lut.offset, 1, lut.pitch * 16);
    rect_t l = {0, 0, 16, 16};
    Blit(&lut, &l, &b, 0, 0, &none, NULL);
    check("LUT8 through its palette", px(&b, 4, 4) == 0xFF112233u);

    check("CreateSurface 1920x1080", CreateSurface(1920, 1080, 0, &img) == 0);
    if (hal) {
        check("CaptureFrameBuffer rejects NULL", CaptureFrameBuffer(NULL) != 0);
        check("CaptureFrameBuffer reads actual scanout", CaptureFrameBuffer(&img) == 0);
        unsigned lit = 0;
        for (unsigned y = 0; y < 1080; y += 8)
            for (unsigned x = 0; x < 1920; x += 8)
                lit += (px(&img, x, y) & 0xFFFFFF) != 0;
        check("captured scanout contains pixels", lit > 100);
    } else {
        /* gm's boot logo: in /usr/share/gm on some firmwares, in a resolution directory on others
         */
        const char *logo = "/usr/share/gm/SecondBootLogo.png";
        glob_t g;
        if (access(logo, R_OK) != 0 && glob("/usr/share/gm/*/SecondBootLogo.png", 0, NULL, &g) == 0)
            logo = g.gl_pathv[0]; /* g is not freed: im holds the path */
        printf("     logo %s\n", logo);
        image_t im = {1, {0, 0, 0, 0}, 0, 0, 0, {0}, logo, NULL};
        check("DecodeImage PNG", DecodeImage(&im, &img, 0) == 0);
        unsigned lit = 0;
        for (unsigned y = 0; y < 1080; y += 8)
            for (unsigned x = 0; x < 1920; x += 8)
                lit += (px(&img, x, y) & 0xFFFFFF) != 0;
        check("DecodeImage drew the logo", lit > 100);
        im.format = 0;
        check("DecodeImage JPEG refused", DecodeImage(&im, &img, 0) != 0);
    }

    /* another process's surface is the same memory here */
    int pfd[2];
    surface_t c;
    if (pipe(pfd) == 0 && fork() == 0) {
        CreateSurface(8, 8, 0, &c);
        rect_t r = {0, 0, 8, 8};
        FillRectangle(&c, &r, 0xFF123456u, &none, NULL);
        write(pfd[1], &c, sizeof c);
        _exit(0);
    }
    read(pfd[0], &c, sizeof c);
    wait(NULL);
    check("a surface of another process", px(&c, 7, 7) == 0xFF123456u);
    DestroySurface(&c);

    surface_t *fbs = NULL;
    unsigned n = 0;
    if (!hal) {
        check("GetFrameBufferList: gm's OSD frame buffer", GetFrameBufferList(&fbs, &n) == 0 &&
                                                               n >= 1 && fbs[0].width == 1920 &&
                                                               fbs[0].height == 1080);

        /* the display API the compositor's egl-kms scans the OSD plane out with */
        webos_gfx_osd_fn *osd = (webos_gfx_osd_fn *)dlsym(lib, "webos_gfx_osd");
        struct webos_gfx_osd o;
        check("webos_gfx_osd: the OSD plane is gm's frame buffer, shown",
              osd && osd(&o) == 0 && o.has_fb && o.visible && n >= 1 && o.offset == fbs[0].offset &&
                  o.width == 1920 && o.height == 1080);
        if (osd) {
            printf("     the plane shows %s\n", o.window >= 0 ? "the compositor's window"
                                                : o.comp_window >= 0
                                                    ? "gm's merge with the compositor in it"
                                                    : "gm's own windows");
            /* the display rectangle is on the panel (DILE_BOARDINFO's display resolution) */
            printf("     view %u,%u %ux%u, disp %u,%u %ux%u\n", o.view.x, o.view.y, o.view.w,
                   o.view.h, o.disp.x, o.disp.y, o.disp.w, o.disp.h);
        }
        free(fbs);
    }

    GetGraphicMemInfo(mem2);
    check("GetGraphicMemInfo counts the new surfaces", mem2[0] == size && mem2[1] > mem[1]);
    DestroySurface(&a);
    DestroySurface(&b);
    DestroySurface(&lut);
    DestroySurface(&img);
    GetGraphicMemInfo(mem2);
    check("DestroySurface gives the memory back", mem2[1] == mem[1]);
    if (hal)
        check("Finalize", Finalize() == 0);
    printf("GAL %s\n", fails ? "FAIL" : "PASS");
    return fails != 0;
}
