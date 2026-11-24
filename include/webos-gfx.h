// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef WEBOS_GFX_H
#define WEBOS_GFX_H
/* The emulator's graphics memory: what LG's gfx driver gives user space as /dev/gfx
 * (/dev/lg/gfx0), and the physical addresses DILE_GAL reports for it (guest/layers/soc/src,
 * libdile_gal/gfx-memory.c). This header describes shared memory and userspace calls;
 * its structures are not serialized wire records.
 *
 * /dev/gfx is the SoC module's gfx node (guest/kernel/modules: lg115x's /dev/lg/gfx0 at LG's 151:0,
 * linked by the firmware's mknod job; rtk's Realtek gal_gfx and mstar-utopia's /dev/gfx), whose
 * mappings are all of one shmem object in lgemu.ko (gfx.c, LGEMU_GFX_SIZE), allocated as pages
 * are written and shared by every process that maps it:
 *   [0, WEBOS_GFX_MEM_SIZE)          the graphics memory. gm, libgm (every GM client) and
 *                                    tvservice's PQ mmap exactly this much at offset 0 and address
 * a surface as base + DILE_GAL_SURFACE_INFO_T.offset. [WEBOS_GFX_MEM_SIZE, +REGS_SIZE) the display
 * engine's state: the surface allocator, palettes, the frame buffer controllers and the cursor
 * (gfx-memory.c). A surface's physicalAddress is WEBOS_GFX_PHYS_BASE + offset. tvservice maps some
 * through /dev/mem (tv.capture's scaler); guest/layers/common/tvservice's libc-private.c sends
 * those mappings here (guest/layers/common/hal-common/physmem.c). The base lies in the emulator's
 * own window between LG1154's DDR and its registers, after the 64 MiB tvservice's physical memory
 * hands out from 0xA0000000. */
#define WEBOS_GFX_PATH      "/dev/gfx"
#define WEBOS_GFX_PHYS_BASE 0xA8000000u
#define WEBOS_GFX_MEM_SIZE  (192u << 20)
#define WEBOS_GFX_REGS_SIZE (64u << 10)
#define WEBOS_GFX_FILE_SIZE (WEBOS_GFX_MEM_SIZE + WEBOS_GFX_REGS_SIZE)

/* The OSD plane as the display shows it: libdile_gal's display engine state, which the
 * compositor's libegl-kms.so (guest/layers/gl) reads through these functions of libdile_gal.so.1
 * (found with dlsym; the compositor loads libdile_gal through libim and libgm).
 *
 * On the TV, PVR's EGL renders the compositor into the buffers of gm's WEBOS window, and gm
 * shows that window on frame buffer controller 0 alone, or, when other windows are visible
 * (tvwin's factory UI, GM subtitles, teletext, MHEG), their software merge by Z-order in its
 * MERGE window. Here the compositor renders with Mesa into gbm buffers: its GM windows
 * ("compositor windows", registered by egl-kms) hold no pixels, and a merge leaves a
 * placeholder where the compositor's picture goes. */
#include <stdint.h>
#define WEBOS_GFX_COMPOSITOR_WINDOWS 2 /* 0xFFFF0001 (WEBOS), 0xFFFF0002 (WEBOSFULLSCREEN) */
struct webos_gfx_rect {
    uint16_t x, y, w, h;
};
struct webos_gfx_osd {
    uint32_t has_fb, visible;
    uint32_t updates; /* changes whenever the plane's picture may have changed */
    /* the frame buffer controller 0 shows (DILE_GAL_SURFACE_INFO_T fields; ARGB) */
    uint32_t offset, pitch, width, height, format;
    struct webos_gfx_rect view, disp;
    int32_t window; /* >= 0: the frame buffer is that compositor window's own */
    /* else, when it is gm's merge and the compositor's picture is part of it: */
    int32_t comp_window; /* the compositor window merged into it, -1 for none */
    struct webos_gfx_rect comp_src, comp_dst; /* from the window, into the frame buffer */
    uint32_t comp_alpha;                      /* 255, or the merge's constant alpha */
    uint32_t under; /* offset of what lies below it (frame buffer geometry), 0 none */
    int32_t engine; /* the display engine's process (gm): its windows are new if it changes */
    /* the space of disp: the panel, as gm gave it to DILE_GAL_SetGraphicOutputResolution (W18M's
     * 3840x2160 for its UD panel), 0x0 before that (1920x1080) */
    uint32_t out_width, out_height;
};
/* the compositor window slot's two GM surfaces (their offsets) */
typedef int webos_gfx_compositor_window_fn(unsigned slot, uint32_t front, uint32_t back);
/* the DRM framebuffer holding the slot's latest picture (for reads of its surfaces) */
typedef void webos_gfx_compositor_frame_fn(unsigned slot, uint32_t fb_id);
typedef int webos_gfx_osd_fn(struct webos_gfx_osd *out);
/* a word that changes with every display engine change (a futex to wait on) */
typedef volatile uint32_t *webos_gfx_changes_fn(void);
typedef uint8_t *webos_gfx_memory_fn(void);
#endif
