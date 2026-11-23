// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* gop-present: the GOP's planes on virtio-gpu, for LG SimpleSmart
 * (docs/dev/architecture/simplesmart.md, "Display"). The firmware's graphics HAL
 * (guest/layers/simplesmart/src/libhalgal) draws into mstar-utopia.ko's GOP pool and says which
 * surface each GOP controller shows (include/mstar-gop.h); on a set the GOP scans those out over
 * the video plane. Here this program, in the initramfs (outside the firmware's namespace), owns
 * virtio-gpu's KMS output: it maps the pool, waits for the planes or their pixels to change, and
 * composes the shown planes, controller 0 then 1, each scaled from its view rectangle to its
 * display rectangle, over black (the video plane with no signal) into a dumb buffer it scans out,
 * then flushes it (DIRTYFB), which QEMU's display (and so the viewer) gets.
 *
 * Pixels are composed when a plane changes or the HAL drew (the device's generation), at most 60
 * times a second, and twice a second besides: DirectFB may write a shown surface without the HAL.
 * The OSD's alpha is straight (the GOP's), so a pixel is its colour times its alpha over black.
 * Logs to stdout (the initramfs's /run/gop-present.log). */
#include <drm/drm.h>
#include <drm/drm_mode.h>
#include <drm/virtgpu_drm.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
#include "../../../include/mstar-gop.h"

#define GOP_NODE "/dev/mstar-gop-fb"
#define DRM_NODE "/dev/dri/card0"

static int drm, gop;
static uint8_t *pool;
static uint32_t pool_pa, pool_size;
static uint32_t *screen, scr_w, scr_h, scr_pitch, fb_id, crtc_id, conn_id;
/* with virgl (virtio-gpu-gl): the scanout is a 3D resource in R8G8B8A8, which every GL host can
 * upload to (a dumb buffer's BGRX did not reach the Pi's GLES host), sent with a transfer;
 * without: a dumb buffer (XRGB8888) */
static int virgl;
static uint32_t bo_handle;

#define VIRGL_FORMAT_R8G8B8A8_UNORM 67
#define VIRGL_BIND_RENDER_TARGET    (1 << 1)
#define VIRGL_BIND_SCANOUT          (1 << 18)
#define PIPE_TEXTURE_2D             2
static struct drm_mode_modeinfo mode;

static double now(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static int open_retry(const char *path, double seconds)
{
    double end = now() + seconds;
    for (;;) {
        int fd = open(path, O_RDWR | O_CLOEXEC);
        if (fd >= 0 || now() > end)
            return fd;
        usleep(100000);
    }
}

/* the first connected connector, its preferred mode, a CRTC for it, and a dumb buffer shown */
static int kms_setup(void)
{
    struct drm_mode_card_res res = {0};
    uint32_t conns[16], crtcs[16], encs[16];
    if (ioctl(drm, DRM_IOCTL_MODE_GETRESOURCES, &res) < 0)
        return -1;
    if (res.count_connectors > 16 || res.count_crtcs > 16 || res.count_encoders > 16)
        return -1;
    res.connector_id_ptr = (uintptr_t)conns;
    res.crtc_id_ptr = (uintptr_t)crtcs;
    res.encoder_id_ptr = (uintptr_t)encs;
    res.count_fbs = 0;
    if (ioctl(drm, DRM_IOCTL_MODE_GETRESOURCES, &res) < 0)
        return -1;
    for (unsigned i = 0; i < res.count_connectors; i++) {
        struct drm_mode_get_connector c = {.connector_id = conns[i]};
        uint32_t cencs[8];
        if (ioctl(drm, DRM_IOCTL_MODE_GETCONNECTOR, &c) < 0 || c.connection != 1 || !c.count_modes)
            continue;
        if (c.count_encoders > 8)
            c.count_encoders = 8;
        /* room for every mode: the kernel copies none into a shorter array (it only reports the
         * count), and virtio-gpu without EDID lists every standard mode up to its maximum */
        uint32_t want = c.count_modes;
        struct drm_mode_modeinfo *modes = calloc(want, sizeof(*modes));
        if (!modes)
            return -1;
        c.modes_ptr = (uintptr_t)modes;
        c.encoders_ptr = (uintptr_t)cencs;
        c.count_props = 0;
        if (ioctl(drm, DRM_IOCTL_MODE_GETCONNECTOR, &c) < 0 || !c.count_modes ||
            c.count_modes > want) {
            free(modes); /* failed, or the modes changed between the calls */
            continue;
        }
        mode = modes[0];
        for (unsigned m = 0; m < c.count_modes; m++)
            if (modes[m].type & DRM_MODE_TYPE_PREFERRED) {
                mode = modes[m];
                break;
            }
        free(modes);
        conn_id = c.connector_id;
        /* more encoders than cencs holds: none were copied */
        for (unsigned e = 0; e < c.count_encoders && c.count_encoders <= 8 && !crtc_id; e++) {
            struct drm_mode_get_encoder enc = {.encoder_id = cencs[e]};
            if (ioctl(drm, DRM_IOCTL_MODE_GETENCODER, &enc) < 0)
                continue;
            for (unsigned k = 0; k < res.count_crtcs; k++)
                if (enc.possible_crtcs & (1u << k)) {
                    crtc_id = crtcs[k];
                    break;
                }
        }
        break;
    }
    if (!conn_id || !crtc_id)
        return -1;
    scr_w = mode.hdisplay;
    scr_h = mode.vdisplay;
    uint64_t has_3d = 0, size;
    struct drm_virtgpu_getparam gp = {.param = VIRTGPU_PARAM_3D_FEATURES,
                                      .value = (uintptr_t)&has_3d};
    virgl = ioctl(drm, DRM_IOCTL_VIRTGPU_GETPARAM, &gp) == 0 && has_3d;
    uint32_t handle;
    off_t map_offset;
    if (virgl) {
        struct drm_virtgpu_resource_create rc = {.target = PIPE_TEXTURE_2D,
                                                 .format = VIRGL_FORMAT_R8G8B8A8_UNORM,
                                                 .bind =
                                                     VIRGL_BIND_RENDER_TARGET | VIRGL_BIND_SCANOUT,
                                                 .width = scr_w,
                                                 .height = scr_h,
                                                 .depth = 1,
                                                 .array_size = 1,
                                                 .size = scr_w * scr_h * 4,
                                                 .stride = scr_w * 4};
        if (ioctl(drm, DRM_IOCTL_VIRTGPU_RESOURCE_CREATE, &rc) < 0)
            return -1;
        handle = bo_handle = rc.bo_handle;
        scr_pitch = scr_w * 4;
        size = rc.size;
        struct drm_virtgpu_map vm = {.handle = handle};
        if (ioctl(drm, DRM_IOCTL_VIRTGPU_MAP, &vm) < 0)
            return -1;
        map_offset = (off_t)vm.offset;
    } else {
        struct drm_mode_create_dumb cd = {.width = scr_w, .height = scr_h, .bpp = 32};
        if (ioctl(drm, DRM_IOCTL_MODE_CREATE_DUMB, &cd) < 0)
            return -1;
        handle = cd.handle;
        scr_pitch = cd.pitch;
        size = cd.size;
        struct drm_mode_map_dumb md = {.handle = cd.handle};
        if (ioctl(drm, DRM_IOCTL_MODE_MAP_DUMB, &md) < 0)
            return -1;
        map_offset = (off_t)md.offset;
    }
    /* XRGB8888, virtio-gpu's one primary format; with virgl the host reads the resource as the
     * RGBA it is */
    struct drm_mode_fb_cmd fb = {.width = scr_w,
                                 .height = scr_h,
                                 .pitch = scr_pitch,
                                 .bpp = 32,
                                 .depth = 24,
                                 .handle = handle};
    if (ioctl(drm, DRM_IOCTL_MODE_ADDFB, &fb) < 0)
        return -1;
    fb_id = fb.fb_id;
    screen = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, drm, map_offset);
    if (screen == MAP_FAILED)
        return -1;
    memset(screen, 0, size);
    struct drm_mode_crtc crtc = {.crtc_id = crtc_id,
                                 .fb_id = fb_id,
                                 .set_connectors_ptr = (uintptr_t)&conn_id,
                                 .count_connectors = 1,
                                 .mode = mode,
                                 .mode_valid = 1};
    if (ioctl(drm, DRM_IOCTL_MODE_SETCRTC, &crtc) < 0)
        return -1;
    return 0;
}

static inline uint32_t expand(unsigned v, unsigned bits)
{
    return bits == 1 ? (v ? 255 : 0) : (v << (8 - bits)) | (v >> (2 * bits - 8));
}

/* a pixel of a plane as ARGB32; 0 (transparent) for a format the GOP's OSD here does not show */
static inline uint32_t texel(const uint8_t *row, unsigned x, unsigned format)
{
    uint32_t v;
    switch (format) {
    case MSTAR_GOP_ARGB8888:
        return ((const uint32_t *)row)[x];
    case MSTAR_GOP_ARGB1555:
        v = ((const uint16_t *)row)[x];
        return expand(v >> 15, 1) << 24 | expand((v >> 10) & 31, 5) << 16 |
               expand((v >> 5) & 31, 5) << 8 | expand(v & 31, 5);
    case MSTAR_GOP_RGB565:
        v = ((const uint16_t *)row)[x];
        return 0xFF000000u | expand(v >> 11, 5) << 16 | expand((v >> 5) & 63, 6) << 8 |
               expand(v & 31, 5);
    case MSTAR_GOP_ARGB4444:
        v = ((const uint16_t *)row)[x];
        return expand(v >> 12, 4) << 24 | expand((v >> 8) & 15, 4) << 16 |
               expand((v >> 4) & 15, 4) << 8 | expand(v & 15, 4);
    }
    return 0;
}

static inline uint32_t over(uint32_t s, uint32_t d)
{
    unsigned a = s >> 24;
    if (a == 255)
        return s | 0xFF000000u;
    if (!a)
        return d;
    uint32_t out = 0xFF000000u;
    for (int sh = 0; sh < 24; sh += 8) {
        unsigned c = (((s >> sh) & 255) * a + ((d >> sh) & 255) * (255 - a) + 127) / 255;
        out |= c << sh;
    }
    return out;
}

static void compose_plane(const struct mstar_gop_plane *p, int first)
{
    unsigned vx = p->view[0], vy = p->view[1], vw = p->view[2] ? p->view[2] : p->width,
             vh = p->view[3] ? p->view[3] : p->height;
    unsigned dx = p->disp[0], dy = p->disp[1], dw = p->disp[2] ? p->disp[2] : scr_w,
             dh = p->disp[3] ? p->disp[3] : scr_h;
    if (vx + vw > p->width || vy + vh > p->height || !vw || !vh || p->pa < pool_pa ||
        (uint64_t)p->pa - pool_pa + (uint64_t)p->pitch * p->height > pool_size)
        return;
    if (p->format == MSTAR_GOP_LUT8 || p->format == MSTAR_GOP_A8)
        return;
    const uint8_t *base = pool + (p->pa - pool_pa);
    for (unsigned y = 0; y < dh && dy + y < scr_h; y++) {
        const uint8_t *row = base + (size_t)(vy + (uint64_t)y * vh / dh) * p->pitch;
        uint32_t *out = (uint32_t *)((uint8_t *)screen + (size_t)(dy + y) * scr_pitch);
        for (unsigned x = 0; x < dw && dx + x < scr_w; x++) {
            uint32_t s = texel(row, vx + (unsigned)((uint64_t)x * vw / dw), p->format);
            out[dx + x] = first ? over(s, 0xFF000000u) : over(s, out[dx + x]);
        }
    }
}

static void present(void)
{
    if (virgl) { /* the last transfer done before the buffer is written again */
        struct drm_virtgpu_3d_wait w = {.handle = bo_handle};
        ioctl(drm, DRM_IOCTL_VIRTGPU_WAIT, &w);
    }
    static uint32_t seen[MSTAR_GOP_PLANES];
    static unsigned logged;
    struct mstar_gop_plane p[MSTAR_GOP_PLANES];
    int first = 1;
    for (unsigned i = 0; i < MSTAR_GOP_PLANES; i++) {
        memset(&p[i], 0, sizeof(p[i]));
        p[i].index = i;
        ioctl(gop, MSTAR_GOP_IOC_GET_PLANE, &p[i]);
        if (p[i].gen != seen[i] && logged < 40) { /* a plane set: say what it is */
            unsigned shown = 0;
            seen[i] = p[i].gen;
            logged++;
            /* how much of it is not transparent */
            if (p[i].enabled && p[i].format == MSTAR_GOP_ARGB8888 && p[i].pa >= pool_pa &&
                (uint64_t)p[i].pa - pool_pa + (uint64_t)p[i].pitch * p[i].height <= pool_size)
                for (unsigned y = 0; y < p[i].height; y++)
                    for (unsigned x = 0; x < p[i].width; x++)
                        shown += pool[p[i].pa - pool_pa + (size_t)y * p[i].pitch + x * 4 + 3] != 0;
            printf("gop-present: plane %u: %s%s 0x%x %ux%u pitch %u format %u view %u,%u %ux%u "
                   "disp %u,%u %ux%u, "
                   "%u pixels not transparent\n",
                   i, p[i].enabled ? "set" : "unset", p[i].visible ? ", visible" : ", hidden",
                   p[i].pa, p[i].width, p[i].height, p[i].pitch, p[i].format, p[i].view[0],
                   p[i].view[1], p[i].view[2], p[i].view[3], p[i].disp[0], p[i].disp[1],
                   p[i].disp[2], p[i].disp[3], shown);
        }
    }
    /* below the first shown plane: black, where it does not cover */
    int covers = 0;
    for (unsigned i = 0; i < MSTAR_GOP_PLANES; i++)
        if (p[i].enabled && p[i].visible) {
            covers = !p[i].disp[2] || (p[i].disp[0] == 0 && p[i].disp[1] == 0 &&
                                       p[i].disp[2] >= scr_w && p[i].disp[3] >= scr_h);
            break;
        }
    if (!covers) {
        for (unsigned y = 0; y < scr_h; y++)
            memset((uint8_t *)screen + (size_t)y * scr_pitch, 0, (size_t)scr_w * 4);
        first = 0;
    }
    for (unsigned i = 0; i < MSTAR_GOP_PLANES; i++)
        if (p[i].enabled && p[i].visible) {
            compose_plane(&p[i], first);
            first = 0;
        }
    if (first) /* nothing shown */
        for (unsigned y = 0; y < scr_h; y++)
            memset((uint8_t *)screen + (size_t)y * scr_pitch, 0, (size_t)scr_w * 4);
    if (virgl) {
        /* ARGB as composed to the resource's R, G, B, A bytes, then to the host */
        for (unsigned y = 0; y < scr_h; y++) {
            uint32_t *row = (uint32_t *)((uint8_t *)screen + (size_t)y * scr_pitch);
            for (unsigned x = 0; x < scr_w; x++) {
                uint32_t c = row[x];
                row[x] = (c & 0xFF00FF00u) | (c >> 16 & 0xFF) | (c & 0xFF) << 16;
            }
        }
        struct drm_virtgpu_3d_transfer_to_host t = {
            .bo_handle = bo_handle,
            .box = {.w = scr_w, .h = scr_h, .d = 1}}; /* stride 0: the resource's own */
        if (ioctl(drm, DRM_IOCTL_VIRTGPU_TRANSFER_TO_HOST, &t) < 0) {
            static int said;
            if (!said++)
                printf("gop-present: TRANSFER_TO_HOST: %s\n", strerror(errno));
        }
    }
    struct drm_clip_rect clip = {0, 0, (unsigned short)scr_w, (unsigned short)scr_h};
    struct drm_mode_fb_dirty_cmd dirty = {
        .fb_id = fb_id, .num_clips = 1, .clips_ptr = (uintptr_t)&clip};
    if (ioctl(drm, DRM_IOCTL_MODE_DIRTYFB, &dirty) < 0) {
        static int said;
        if (!said++)
            printf("gop-present: DIRTYFB: %s\n", strerror(errno));
    }
}

/* SIGUSR1: the composed picture as /run/gop-present.ppm (a check of what is scanned out) */
static volatile sig_atomic_t dump_wanted;
static void on_usr1(int sig)
{
    (void)sig;
    dump_wanted = 1;
}

static void dump(void)
{
    FILE *f = fopen("/run/gop-present.ppm", "wb");
    if (!f)
        return;
    fprintf(f, "P6\n%u %u\n255\n", scr_w, scr_h);
    for (unsigned y = 0; y < scr_h; y++) {
        const uint32_t *row = (const uint32_t *)((uint8_t *)screen + (size_t)y * scr_pitch);
        for (unsigned x = 0; x < scr_w; x++) {
            uint32_t c = row[x];
            unsigned char px[3] = {(unsigned char)(virgl ? c : c >> 16), (unsigned char)(c >> 8),
                                   (unsigned char)(virgl ? c >> 16 : c)};
            fwrite(px, 1, 3, f);
        }
    }
    fclose(f);
    printf("gop-present: dumped /run/gop-present.ppm\n");
}

int main(void)
{
    signal(SIGUSR1, on_usr1);
    struct mstar_gop_pool pp;
    setvbuf(stdout, NULL, _IOLBF, 0);
    gop = open(GOP_NODE, O_RDWR | O_CLOEXEC);
    if (gop < 0 || ioctl(gop, MSTAR_GOP_IOC_GET_POOL, &pp) < 0) {
        printf("gop-present: %s: %s\n", GOP_NODE, strerror(errno));
        return 1;
    }
    pool = mmap(NULL, pp.size, PROT_READ, MAP_SHARED, gop, 0);
    if (pool == MAP_FAILED) {
        printf("gop-present: mmap of the pool: %s\n", strerror(errno));
        return 1;
    }
    pool_pa = pp.pa;
    pool_size = pp.size;
    drm = open_retry(DRM_NODE, 10);
    if (drm < 0) {
        printf("gop-present: %s: %s (no display)\n", DRM_NODE, strerror(errno));
        return 1;
    }
    if (kms_setup() < 0) {
        printf("gop-present: no KMS output: %s\n", strerror(errno));
        return 1;
    }
    printf("gop-present: pool 0x%x %u KiB; %ux%u on CRTC %u, connector %u (%s)\n", pool_pa,
           pool_size >> 10, scr_w, scr_h, crtc_id, conn_id,
           virgl ? "virgl resource" : "dumb buffer");
    struct mstar_gop_wait w = {.gen = 0};
    double last = 0, shown_log = 0;
    unsigned frames = 0;
    for (;;) {
        w.timeout_ms = 500;
        if (ioctl(gop, MSTAR_GOP_IOC_WAIT, &w) < 0 && errno != EINTR) {
            printf("gop-present: wait: %s\n", strerror(errno));
            return 1;
        }
        double t = now();
        if (t - last < 1.0 / 60)
            usleep((useconds_t)((1.0 / 60 - (t - last)) * 1e6));
        present();
        if (dump_wanted) {
            dump_wanted = 0;
            dump();
        }
        last = now();
        frames++;
        if (last - shown_log > 60) {
            printf("gop-present: %u frames (generation %u)\n", frames, w.gen);
            shown_log = last;
        }
    }
}
