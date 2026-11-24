// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libdile_gal: the emulator's graphics device under DILE_GAL: its memory, the surface allocator,
 * and the display engine's frame buffer controllers (include/webos-gfx.h for the layout).
 *
 * On the TV these live in LG's gfx and fbdev kernel drivers, which every process reaches through
 * KADP; here they are one shared memory, the SoC module's gfx node (lgemu.ko's gfx.c: lg115x's
 * /dev/lg/gfx0, rtk's and mstar-utopia's /dev/gfx). /dev/gfx is mapped once per process; the
 * "registers" after the graphics memory hold, for all processes at once:
 *   - the allocator: one entry per surface (offset, size, the creating process). Surfaces are
 *     page-aligned, so tvservice can map one through /dev/mem. A process that dies without
 *     destroying its surfaces loses them when memory runs short, as the driver frees them when
 *     its file is closed;
 *   - LUT8 palettes (DILE_GAL_SetSurfacePalette), kept by surface;
 *   - the frame buffer controllers: the surface each shows, its view and display rectangles,
 *     visibility, vsync and stereoscope; the cursor position (DILE_GAL_MoveCursor, or
 *     SetCursorPosition in the space SetCursorResolution gives); and the resolutions set.
 * A process mutex and an exclusive flock of /dev/gfx serialise changes between threads and
 * processes; the flock goes with a process that dies holding it.
 *
 * The display engine: gm (the process that sets the controllers' frame buffers) runs a thread
 * that shows the cursor plane (controller 2) in the host viewer over the webos.cursor port
 * (cursor-channel.c): its frame buffer, visibility and the position libim moves it to from the
 * compositor's process. The OSD plane (controller 0) is scanned out by the compositor's
 * libegl-kms.so (guest/layers/gl): the compositor's own picture when the plane shows its GM window,
 * else the plane's frame buffer (gm's merge of its visible windows) with the compositor's
 * picture in the place gm merged its window (gfx-merge.c). It reads the plane through the
 * webos_gfx_* functions here (include/webos-gfx.h), and registers the compositor's GM windows
 * and its latest framebuffers in the state. */
#include "gal.h"
#include "cursor-channel.h"
#include "../../../../../include/webos-cursor.h"
#include "../../../../../include/webos-gfx.h"
#include <errno.h>
#include <fcntl.h>
#include <linux/futex.h>
#include <pthread.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define REGS_MAGIC 0x31584647u /* "GFX1" */
#define BLOCKS     1024
#define PALETTES   32
#define PAGE       4096u

struct block {
    uint32_t offset, size;
    int32_t pid;
    uint32_t pad;
};
struct palette {
    uint32_t offset, length;
    uint32_t colors[256];
};
struct regs {
    uint32_t magic, mem_size;
    uint32_t seq;        /* futex word: bumped on every change the display engine shows */
    int32_t display_pid; /* the process running the display engine */
    uint32_t cursor_seq; /* odd while the cursor is being written */
    cursor_t cursor;
    uint32_t mirror;
    struct gfx_controller_state ctrl[GFX_CONTROLLERS];
    struct palette palettes[PALETTES];
    struct block blocks[BLOCKS];
    /* the compositor's GM windows: their two surfaces and the DRM framebuffer of the latest
     * picture, registered by egl-kms in the compositor's process */
    struct {
        uint32_t front, back, fb_id, pad;
    } comp[WEBOS_GFX_COMPOSITOR_WINDOWS];
    resolution_t resolution[RES_COUNT];
};
_Static_assert(sizeof(struct regs) <= WEBOS_GFX_REGS_SIZE, "registers");

static int fd = -1;
static uint8_t *mem;
static struct regs *regs;
static pthread_once_t once = PTHREAD_ONCE_INIT;
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;

static void lock(void)
{
    pthread_mutex_lock(&mutex);
    while (flock(fd, LOCK_EX) < 0 && errno == EINTR)
        ;
}

static void unlock(void)
{
    flock(fd, LOCK_UN);
    pthread_mutex_unlock(&mutex);
}

static void setup(void)
{
    struct stat st;
    void *p;
    int f = open(WEBOS_GFX_PATH, O_RDWR | O_CLOEXEC);
    if (f < 0) {
        gal_log("%s: %s", WEBOS_GFX_PATH, strerror(errno));
        return;
    }
    if (fstat(f, &st) < 0 || !S_ISCHR(st.st_mode)) {
        gal_log("%s is not the SoC's gfx device", WEBOS_GFX_PATH);
        close(f);
        return;
    }
    p = mmap(NULL, WEBOS_GFX_FILE_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, f, 0);
    if (p == MAP_FAILED) {
        gal_log("mmap %s: %s", WEBOS_GFX_PATH, strerror(errno));
        close(f);
        return;
    }
    fd = f;
    mem = p;
    regs = (struct regs *)(mem + WEBOS_GFX_MEM_SIZE);
    lock();
    if (regs->magic != REGS_MAGIC) {
        memset(regs, 0, sizeof *regs);
        regs->mem_size = WEBOS_GFX_MEM_SIZE;
        regs->magic = REGS_MAGIC;
    }
    unlock();
}

uint8_t *gfx_memory(void)
{
    pthread_once(&once, setup);
    return mem;
}

static void wake(void)
{
    __sync_fetch_and_add(&regs->seq, 1);
    syscall(SYS_futex, &regs->seq, FUTEX_WAKE, 1, NULL, NULL, 0);
}

/* ---- allocator ---- */

/* The blocks in use, gathered under the lock into a process-wide scratch list (not on the stack:
 * DILE_GAL runs on the callers' threads, some with small stacks, tvservice's dbgfrwk). */
static uint16_t used[BLOCKS];
static unsigned n_used;

static void gather(void)
{
    n_used = 0;
    for (int i = 0; i < BLOCKS; i++)
        if (regs->blocks[i].size)
            used[n_used++] = (uint16_t)i;
}

/* Does [at, at + size) overlap a block in use? */
static int overlaps(uint32_t at, uint32_t size)
{
    for (unsigned k = 0; k < n_used; k++) {
        const struct block *b = &regs->blocks[used[k]];
        if (at < b->offset + b->size && b->offset < at + size)
            return 1;
    }
    return 0;
}

/* first fit: the lowest of PAGE and the ends of the blocks in use; ~0u if none */
static uint32_t find_room(uint32_t size, int *slot)
{
    uint32_t best = ~0u;
    *slot = -1;
    for (int i = 0; i < BLOCKS && *slot < 0; i++)
        if (!regs->blocks[i].size)
            *slot = i;
    if (*slot < 0)
        return ~0u;
    gather();
    /* offset 0 stays unused: callers take it for "no surface" */
    for (int k = -1; k < (int)n_used; k++) {
        const struct block *b = k < 0 ? NULL : &regs->blocks[used[k]];
        uint32_t at = b ? b->offset + b->size : PAGE;
        if (at < best && at + size <= WEBOS_GFX_MEM_SIZE && !overlaps(at, size))
            best = at;
    }
    return best;
}

static unsigned reclaim(void)
{
    unsigned n = 0;
    for (int i = 0; i < BLOCKS; i++) {
        struct block *b = &regs->blocks[i];
        if (b->size && kill(b->pid, 0) < 0 && errno == ESRCH) {
            gfx_zero(b->offset, b->size);
            memset(b, 0, sizeof *b);
            n++;
        }
    }
    return n;
}

uint32_t gfx_alloc(uint32_t size)
{
    uint32_t at;
    int slot;
    if (!gfx_memory() || !size)
        return ~0u;
    size = (size + PAGE - 1) & ~(PAGE - 1);
    lock();
    at = find_room(size, &slot);
    if (at == ~0u && reclaim()) {
        gal_log("memory: reclaimed the surfaces of processes that are gone");
        at = find_room(size, &slot);
    }
    if (at != ~0u) {
        regs->blocks[slot].offset = at;
        regs->blocks[slot].size = size;
        regs->blocks[slot].pid = getpid();
    }
    unlock();
    if (at == ~0u)
        gal_log("memory: no room for %u bytes", size);
    return at;
}

int gfx_free(uint32_t offset)
{
    int found = 0;
    if (!gfx_memory())
        return GAL_ERR;
    lock();
    for (int i = 0; i < BLOCKS; i++) {
        struct block *b = &regs->blocks[i];
        if (b->size && b->offset == offset) {
            gfx_zero(b->offset, b->size);
            memset(b, 0, sizeof *b);
            found = 1;
        }
    }
    for (int i = 0; i < PALETTES; i++)
        if (regs->palettes[i].length && regs->palettes[i].offset == offset)
            regs->palettes[i].length = 0;
    unlock();
    return found ? GAL_OK : GAL_ERR;
}

void gfx_mem_info(uint32_t *total, uint32_t *allocated, uint32_t *free_total, uint32_t *largest)
{
    uint32_t sum = 0, big = 0;
    *total = WEBOS_GFX_MEM_SIZE;
    if (!gfx_memory()) {
        *allocated = *free_total = *largest = 0;
        return;
    }
    lock();
    gather();
    /* the free run after PAGE and after each block: up to the nearest block start above it */
    for (int k = -1; k < (int)n_used; k++) {
        const struct block *b = k < 0 ? NULL : &regs->blocks[used[k]];
        uint32_t at = b ? b->offset + b->size : PAGE, end = WEBOS_GFX_MEM_SIZE;
        if (b)
            sum += b->size;
        if (overlaps(at, 1))
            continue;
        for (unsigned j = 0; j < n_used; j++) {
            const struct block *c = &regs->blocks[used[j]];
            if (c->offset >= at && c->offset < end)
                end = c->offset;
        }
        if (end - at > big)
            big = end - at;
    }
    unlock();
    *allocated = sum;
    *free_total = WEBOS_GFX_MEM_SIZE - sum;
    *largest = big;
}

void gfx_zero(uint32_t offset, uint32_t length)
{
    uint32_t first = (offset + PAGE - 1) & ~(PAGE - 1), end = offset + length,
             last = end & ~(PAGE - 1);
    if (!mem)
        return;
    if (first >= last) {
        memset(mem + offset, 0, length);
        return;
    }
    memset(mem + offset, 0, first - offset);
    memset(mem + last, 0, end - last);
    /* MADV_REMOVE: the device's pages (shmem, lgemu.ko's gfx.c) go back to the kernel and read 0 */
    if (madvise(mem + first, last - first, MADV_REMOVE) < 0)
        memset(mem + first, 0, last - first);
}

int gfx_palette_set(uint32_t offset, const uint32_t *colors, unsigned length)
{
    int slot = -1;
    if (!gfx_memory() || length > 256)
        return GAL_ERR;
    lock();
    for (int i = 0; i < PALETTES && slot < 0; i++)
        if (regs->palettes[i].length && regs->palettes[i].offset == offset)
            slot = i;
    for (int i = 0; i < PALETTES && slot < 0; i++)
        if (!regs->palettes[i].length)
            slot = i;
    if (slot >= 0) {
        regs->palettes[slot].offset = offset;
        memcpy(regs->palettes[slot].colors, colors, length * 4);
        regs->palettes[slot].length = length ? length : 1;
    }
    unlock();
    if (slot < 0)
        gal_log("palette: no room for the palette of the surface at 0x%x", offset);
    return slot < 0 ? GAL_ERR : GAL_OK;
}

const uint32_t *gfx_palette(uint32_t offset, unsigned *length)
{
    const uint32_t *colors = NULL;
    if (!gfx_memory())
        return NULL;
    lock();
    for (int i = 0; i < PALETTES && !colors; i++)
        if (regs->palettes[i].length && regs->palettes[i].offset == offset) {
            colors = regs->palettes[i].colors;
            *length = regs->palettes[i].length;
        }
    unlock();
    return colors;
}

/* ---- controllers ---- */

void gfx_controller_set_fb(unsigned index, const surface_t *fb, uint32_t flags, const rect_t *view,
                           const rect_t *disp)
{
    struct gfx_controller_state merge;
    if (!gfx_memory() || index >= GFX_CONTROLLERS)
        return;
    gfx_merge_lookup(fb->offset, &merge);
    lock();
    struct gfx_controller_state *c = &regs->ctrl[index];
    c->fb = *fb;
    c->has_fb = 1;
    c->updates++;
    c->comp_window = merge.comp_window;
    c->comp_src = merge.comp_src;
    c->comp_dst = merge.comp_dst;
    c->comp_alpha = merge.comp_alpha;
    c->under = merge.under;
    if (flags & FB_PROP_VIEWRECT && view)
        c->view = *view;
    if (flags & FB_PROP_DISPRECT && disp)
        c->disp = *disp;
    unlock();
    wake();
}

void gfx_controller_set_view(unsigned index, uint32_t flags, const rect_t *view, const rect_t *disp)
{
    if (!gfx_memory() || index >= GFX_CONTROLLERS)
        return;
    lock();
    if (flags & FB_PROP_VIEWRECT && view)
        regs->ctrl[index].view = *view;
    if (flags & FB_PROP_DISPRECT && disp)
        regs->ctrl[index].disp = *disp;
    unlock();
    wake();
}

void gfx_controller_set_visible(unsigned index, uint32_t visible)
{
    if (!gfx_memory() || index >= GFX_CONTROLLERS)
        return;
    lock();
    regs->ctrl[index].visible = !!visible;
    unlock();
    wake();
}

void gfx_controller_set_vsync(unsigned index, uint32_t vsync)
{
    if (!gfx_memory() || index >= GFX_CONTROLLERS)
        return;
    lock();
    regs->ctrl[index].vsync = vsync;
    unlock();
}

void gfx_controller_set_stereo(unsigned index, uint32_t on, uint32_t type)
{
    if (!gfx_memory() || index >= GFX_CONTROLLERS)
        return;
    lock();
    regs->ctrl[index].stereo = on;
    regs->ctrl[index].stereo_type = type;
    unlock();
}

int gfx_controller_get(unsigned index, struct gfx_controller_state *out)
{
    if (!gfx_memory() || index >= GFX_CONTROLLERS)
        return GAL_ERR;
    lock();
    *out = regs->ctrl[index];
    unlock();
    return GAL_OK;
}

void gfx_cursor_move(const cursor_t *c)
{
    if (!gfx_memory())
        return;
    /* a seqlock: libim moves the cursor on every pointer event, from the compositor */
    __sync_fetch_and_add(&regs->cursor_seq, 1);
    __sync_synchronize();
    regs->cursor = *c;
    __sync_synchronize();
    __sync_fetch_and_add(&regs->cursor_seq, 1);
    wake();
}

void gfx_resolution_set(unsigned which, resolution_t r)
{
    if (!gfx_memory() || which >= RES_COUNT)
        return;
    lock();
    regs->resolution[which] = r;
    unlock();
}

resolution_t gfx_resolution(unsigned which)
{
    /* no lock: libim asks on every pointer event, and each is set once, by one process */
    resolution_t r = {0, 0};
    if (gfx_memory() && which < RES_COUNT)
        r = *(volatile resolution_t *)&regs->resolution[which];
    return r;
}

static int cursor_read(cursor_t *out)
{
    for (int tries = 0; tries < 100; tries++) {
        uint32_t s = regs->cursor_seq;
        __sync_synchronize();
        if (s & 1)
            continue;
        *out = regs->cursor;
        __sync_synchronize();
        if (regs->cursor_seq == s)
            return s != 0;
    }
    return 0;
}

/* ---- the compositor ---- */

int gfx_compositor_slot(uint32_t offset)
{
    if (!gfx_memory() || !offset)
        return -1;
    for (int i = 0; i < WEBOS_GFX_COMPOSITOR_WINDOWS; i++)
        if (regs->comp[i].front == offset || regs->comp[i].back == offset)
            return i;
    return -1;
}

uint32_t gfx_compositor_fb(int slot)
{
    return gfx_memory() && slot >= 0 && slot < WEBOS_GFX_COMPOSITOR_WINDOWS ? regs->comp[slot].fb_id
                                                                            : 0;
}

int gfx_display_process(void)
{
    return gfx_memory() && regs->display_pid == getpid();
}

int gfx_composing(uint32_t dst_offset)
{
    int mine = 0;
    if (!gfx_display_process())
        return 0;
    lock();
    for (int i = 0; i < BLOCKS && !mine; i++)
        if (regs->blocks[i].size && regs->blocks[i].offset == dst_offset)
            mine = regs->blocks[i].pid == getpid();
    unlock();
    return mine;
}

/* The display API for the compositor's egl-kms (include/webos-gfx.h). */
#define EXPORT __attribute__((visibility("default")))

EXPORT int webos_gfx_compositor_window(unsigned slot, uint32_t front, uint32_t back)
{
    if (!gfx_memory() || slot >= WEBOS_GFX_COMPOSITOR_WINDOWS)
        return -1;
    lock();
    regs->comp[slot].front = front;
    regs->comp[slot].back = back;
    unlock();
    gal_log("display engine: the compositor's window %u is the surfaces 0x%x and 0x%x", slot, front,
            back);
    wake();
    return 0;
}

EXPORT void webos_gfx_compositor_frame(unsigned slot, uint32_t fb_id)
{
    if (gfx_memory() && slot < WEBOS_GFX_COMPOSITOR_WINDOWS)
        regs->comp[slot].fb_id = fb_id;
}

EXPORT int webos_gfx_osd(struct webos_gfx_osd *out)
{
    struct gfx_controller_state c;
    if (!out || gfx_controller_get(GFX_OSD, &c))
        return -1;
    memset(out, 0, sizeof *out);
    out->has_fb = c.has_fb;
    out->visible = c.visible;
    out->updates = c.updates;
    out->offset = c.fb.offset;
    out->pitch = c.fb.pitch;
    out->width = c.fb.width;
    out->height = c.fb.height;
    out->format = c.fb.pixelFormat;
    memcpy(&out->view, &c.view, sizeof out->view);
    memcpy(&out->disp, &c.disp, sizeof out->disp);
    out->window = c.has_fb ? gfx_compositor_slot(c.fb.offset) : -1;
    out->comp_window = out->window < 0 ? c.comp_window : -1;
    memcpy(&out->comp_src, &c.comp_src, sizeof out->comp_src);
    memcpy(&out->comp_dst, &c.comp_dst, sizeof out->comp_dst);
    out->comp_alpha = c.comp_alpha;
    out->under = out->comp_window >= 0 ? c.under : 0;
    out->engine = regs->display_pid;
    resolution_t panel = gfx_resolution(RES_OUTPUT);
    out->out_width = panel.width;
    out->out_height = panel.height;
    return 0;
}

EXPORT volatile uint32_t *webos_gfx_changes(void)
{
    return gfx_memory() ? &regs->seq : NULL;
}

EXPORT uint8_t *webos_gfx_memory(void)
{
    return gfx_memory();
}

/* ---- the display engine ---- */

/* The cursor plane: its visibility, and the picture it shows, which is the size DILE_GAL_MoveCursor
 * last gave (libim draws each image at the top left of the plane's 256x256 frame buffer), from the
 * view rectangle's origin; the whole view rectangle (or frame buffer) before any move. */
static void show_cursor_plane(struct gfx_controller_state *last, cursor_t *last_cursor, int first)
{
    struct gfx_controller_state c;
    cursor_t cur;
    memset(&cur, 0, sizeof cur);
    cursor_read(&cur);
    gfx_controller_get(GFX_CURSOR, &c);
    if (first || c.visible != last->visible)
        cursor_channel_visible(c.visible);
    int same = !first && c.updates == last->updates &&
               !memcmp(&c.view, &last->view, sizeof c.view) && cur.width == last_cursor->width &&
               cur.height == last_cursor->height;
    *last = c;
    *last_cursor = cur;
    if (same || !c.has_fb || c.fb.pixelFormat != FMT_ARGB)
        return;
    rect_t v = c.view;
    if (!v.w || !v.h)
        v = (rect_t){0, 0, c.fb.width, c.fb.height};
    if (cur.width && cur.height && cur.width <= v.w && cur.height <= v.h) {
        v.w = (uint16_t)cur.width;
        v.h = (uint16_t)cur.height;
    }
    if (v.w <= WEBOS_CURSOR_MAX && v.h <= WEBOS_CURSOR_MAX && (unsigned)v.x + v.w <= c.fb.width &&
        (unsigned)v.y + v.h <= c.fb.height &&
        c.fb.offset + (uint64_t)c.fb.pitch * c.fb.height <= WEBOS_GFX_MEM_SIZE)
        cursor_channel_image(mem + c.fb.offset + (size_t)v.y * c.fb.pitch + (size_t)v.x * 4, v.w,
                             v.h, c.fb.pitch);
}

static void *display(void *unused)
{
    struct gfx_controller_state last;
    cursor_t last_cursor;
    uint32_t seen_cursor = 0;
    (void)unused;
    memset(&last, 0, sizeof last);
    memset(&last_cursor, 0, sizeof last_cursor);
    show_cursor_plane(&last, &last_cursor, 1);
    for (;;) {
        uint32_t seq = regs->seq;
        cursor_t c;
        /* libim draws each new image in place and gm sets the frame buffer again: the picture is
         * taken again after every update */
        show_cursor_plane(&last, &last_cursor, 0);
        if (regs->cursor_seq != seen_cursor && cursor_read(&c)) {
            seen_cursor = regs->cursor_seq;
            cursor_channel_move(&c);
        }
        struct timespec t = {1, 0};
        syscall(SYS_futex, &regs->seq, FUTEX_WAIT, seq, &t, NULL, 0);
    }
    return NULL;
}

static void display_start(void)
{
    pthread_t t;
    int mine;
    lock();
    mine = !regs->display_pid || regs->display_pid == getpid() ||
           (kill(regs->display_pid, 0) < 0 && errno == ESRCH);
    if (mine)
        regs->display_pid = getpid();
    unlock();
    if (!mine)
        return;
    if (pthread_create(&t, NULL, display, NULL) == 0) {
        pthread_detach(t);
        gal_log("display engine: the cursor plane goes to the viewer from this process");
    }
}

void gfx_display_start(void)
{
    static pthread_once_t display_once = PTHREAD_ONCE_INIT;
    if (gfx_memory())
        pthread_once(&display_once, display_start);
}
