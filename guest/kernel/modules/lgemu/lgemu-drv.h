/* SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors */
/* SPDX-License-Identifier: GPL-2.0-only */
/* What the SoC vendors' driver modules (lg115x.ko, mstar-utopia.ko, rtk.ko) share, in lgemu.ko
 * (drv.c): each of them emulates a vendor's kernel drivers for the vendor's userland, and they do
 * it with the same few mechanisms. docs/dev/guides/build.md, "Kernel module lgemu".
 *
 * - A page store: zeroed pages by address (a physical or bus address), allocated as they are
 *   first touched and shared by every mapping of the same address.
 * - Counts: what the userland asked for, by (what, a, b), listed in a /proc file; for an ioctl
 *   the module does not know, the first time logged.
 * - Parameters: the chip table's "a:b:c" values (guest/targets/soc/).
 * - Character devices at a vendor's fixed major (or a dynamic one).
 * - GPIO lines: the SoC's pins in the kernel's global numbering, for /sys/class/gpio.
 * - The graphics memory (gfx.c): the fops of the SoC's gfx node.
 * - The secondary screen's frame buffer (fb.c). */
#ifndef LGEMU_DRV_H
#define LGEMU_DRV_H
#include <linux/cdev.h>
#include <linux/fs.h>
#include <linux/gpio/driver.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/spinlock.h>
#include <linux/types.h>
#include <linux/workqueue.h>
#include <linux/xarray.h>

/* ---- the page store ---- */

struct lgemu_store {
    struct xarray pages;  /* index: address >> PAGE_SHIFT */
    struct module *owner; /* held by every mapping: the store is the owner's */
    /* Installed before exposing mappings, immutable for their lifetime. All aliases of
     * selected pages trap. access must serialize its device state; value is masked to
     * width. Unsupported accesses return an error without side effects. */
    bool (*trap_page)(u64 page);
    int (*access)(u64 addr, unsigned int width, bool write, u32 *value);
};

#define LGEMU_STORE_INIT(name) {.pages = XARRAY_INIT((name).pages, 0), .owner = THIS_MODULE}

/* The kernel address of addr in the store (its page allocated, zeroed), or NULL without memory.
 * Valid to the end of addr's page. */
void *lgemu_store_ptr(struct lgemu_store *s, u64 addr);

/* Maps the store into vma from addr (the first page's; vm_pgoff becomes addr >> PAGE_SHIFT),
 * page by page as they fault. Shared mappings only (a COW PFNMAP cannot be): -EINVAL else. The
 * mapping holds the store's owner, which its operations' store is in. */
int lgemu_store_mmap(struct lgemu_store *s, struct vm_area_struct *vma, u64 addr);

/* Frees every page (the owner going: no mapping is left). */
void lgemu_store_free(struct lgemu_store *s);

/* ---- counts ---- */

struct lgemu_count {
    const char *what; /* a device, ...; compared as a string */
    u32 a, b;         /* an ioctl: its command, 0 */
    unsigned long count;
    bool marked; /* an ioctl: not emulated */
};

/* The (a, b) of a table whose callers are logged; off unless on. Written while counting goes on
 * (a debugging parameter): on is cleared before a and b change and set after. */
struct lgemu_trace {
    u32 a, b;
    bool on;
};

struct lgemu_counts {
    spinlock_t lock;
    struct lgemu_count *e;
    size_t size; /* entries; once full, a new (what, a, b) is not counted */
    size_t line; /* the longest line show writes, for reading */
    /* one entry's line into buf (at most size bytes, scnprintf), for the /proc file */
    int (*show)(char *buf, size_t size, const struct lgemu_count *e);
    const char *module;              /* the owner's name, for the log */
    const struct lgemu_trace *trace; /* NULL, or the entry (a, b) whose callers are logged */
};

/* A table of n entries, its lines at most line bytes, show writing them. */
#define DEFINE_LGEMU_COUNTS(name, n, line_, show_)                                                 \
    static struct lgemu_count name##_entries[n];                                                   \
    static struct lgemu_counts name = {                                                            \
        .lock = __SPIN_LOCK_UNLOCKED(name.lock),                                                   \
        .e = name##_entries,                                                                       \
        .size = n,                                                                                 \
        .line = line_,                                                                             \
        .show = show_,                                                                             \
        .module = KBUILD_MODNAME,                                                                  \
    }

/* Counts (what, a, b) once. With c->trace on and (a, b) its entry, logs the caller first (at most
 * once a second): the thread, its AArch32 pc, lr and the word at sp + 8 (for a caller through
 * glibc's ioctl from a push {r3, lr} wrapper, the wrapper's own return address). */
void lgemu_count(struct lgemu_counts *c, const char *what, u32 a, u32 b);

/* An ioctl cmd of device dev that the module does not know, answered ret: marked in c (as
 * (dev, cmd, 0)) and logged the first time; returns ret. Does not count it (lgemu_count does). */
long lgemu_ioctl_unhandled(struct lgemu_counts *c, const char *dev, unsigned int cmd, long ret);

/* A read of c's list from *pos, for a /proc file of the module's own */
ssize_t lgemu_counts_read(struct lgemu_counts *c, char __user *buf, size_t n, loff_t *pos);

/* A /proc file listing c (proc_create_data's data), one show line an entry in order of arrival */
extern const struct proc_ops lgemu_counts_proc_ops;

/* ---- parameters ---- */

/* "n1:n2:...": exactly n numbers (kstrtou32 in base: 0, or 16 with or without 0x) into out;
 * 0, or -EINVAL (-ENOMEM). */
int lgemu_parse_u32s(const char *spec, unsigned int base, u32 *out, int n);

/* "name:n1:n2:...": the name (at most name_size - 1 bytes, not empty) and then as
 * lgemu_parse_u32s. */
int lgemu_parse_named(const char *spec, char *name, size_t name_size, unsigned int base, u32 *out,
                      int n);

/* ---- character devices ---- */

struct lgemu_cdev {
    struct cdev cdev;
    dev_t dev; /* the first */
    unsigned int count;
    bool added;
};

/* count minors from (major, minor) named name, with fops (whose owner the cdev gets); major 0: a
 * dynamic one (c->dev says which). -EBUSY when the numbers are taken. */
int lgemu_cdev_add(struct lgemu_cdev *c, unsigned int major, unsigned int minor, unsigned int count,
                   const char *name, const struct file_operations *fops);
/* undoes lgemu_cdev_add, if it was added */
void lgemu_cdev_del(struct lgemu_cdev *c);

/* ---- GPIO lines ---- */

/* The SoC's GPIO lines in the kernel's global numbering, which LG's libdile_gpio (webOS 4) drives
 * through /sys/class/gpio (export, then gpio<N>/direction and value) by the numbers of its own pin
 * table: a chip at base 0. An input reads the level the board gives it with nothing connected, 0
 * unless the levels name it; an output keeps what was written, apart from the input level. Each
 * family module has the chip's values as its parameters gpio_lines and gpio_level. */
struct lgemu_gpio_line {
    u8 level, out, is_out;
};

struct lgemu_gpiochip {
    struct gpio_chip chip;
    struct lgemu_gpio_line *line;
    spinlock_t lock; /* gpiolib serialises a line's sysfs accesses, not the chip's */
    bool added;
    /* the USB ports' power switches (lgemu_gpiochip_usb_vbus): their outputs, and the work that
     * powers the ports or takes their power away */
    unsigned int vbus[8];
    int nvbus;
    struct work_struct vbus_work;
};

/* A chip labelled label at base 0 with lines lines (0: none, nothing made), its inputs' levels
 * from levels[0..n) ("line:level", line below lines); owned by the caller (a held line holds it).
 * -EINVAL for a bad level, else gpiochip_add_data's answer; nothing is left on failure. */
#define lgemu_gpiochip_add(g, label, lines, levels, n)                                             \
    __lgemu_gpiochip_add(g, THIS_MODULE, label, lines, levels, n)
int __lgemu_gpiochip_add(struct lgemu_gpiochip *g, struct module *owner, const char *label,
                         unsigned int lines, char *const *levels, int n);
/* undoes lgemu_gpiochip_add, if it made a chip, and powers the USB ports again */
void lgemu_gpiochip_remove(struct lgemu_gpiochip *g);
/* The board's USB power switches: lines (n of them, at most 8, below the chip's lines) are the
 * outputs that switch the USB ports' 5 V (VBUS), which LG's PDM plugin turns on at its start
 * (setUSBVbus through libdile_gpio) and the power-off script webos-usb-unbind.sh turns off. While
 * none of them drives 1, the VM's USB ports have no power: the root hubs of every USB bus (the
 * VM's xHCI, the TV's ports) are without their hub driver, so a stick in a port is not seen; the
 * first line to drive 1 binds them again and the ports enumerate what is in them, with uevents
 * that webOS's udevd and PDM receive, as a stick present at power-on reaches a TV's PDM. The
 * ports lose power at once (n > 0, every output starts at 0). -EINVAL for a bad line, nothing
 * changed. n 0: the ports stay powered, as without this call. */
int lgemu_gpiochip_usb_vbus(struct lgemu_gpiochip *g, const unsigned int *lines, int n);

/* ---- the graphics memory ---- */

/* include/webos-gfx.h's WEBOS_GFX_MEM_SIZE + WEBOS_GFX_REGS_SIZE: the memory and the display
 * engine's state after it */
#define LGEMU_GFX_SIZE ((192UL << 20) + (64UL << 10))

/* The fops of the SoC's gfx node (lgemu_cdev_add): every mapping (shared, within LGEMU_GFX_SIZE)
 * is of the one graphics memory, whichever node it came through. */
extern const struct file_operations lgemu_gfx_fops;

/* ---- the sound engine after the PCM (snd.c) ---- */

/* The master (OSD) volume as LG's sound-engine function 9 takes it: the volume curve's level,
 * main byte dB + 127 and low byte 1/16 dB (0x7F00: 0 dB, volume 100; 0: silence) */
void lgemu_snd_osd_level(u32 level);
/* The speaker output's mute */
void lgemu_snd_speaker_mute(bool on);
/* What the firmware selected for the sound outputs through a call named name (n values, at most
 * 5 kept): stored and listed in /proc/lgemu/sound_out, never acted on */
void lgemu_snd_out_set(const char *name, const long *vals, unsigned int n);

/* ---- the scaler's video windows, for the host viewer (video.c) ---- */

/* A window as include/webos-video.h's context carries it (0 MAIN, 1 SUB) */
struct lgemu_video_window {
    bool open, connected, mute, has_rect;
    s32 x, y; /* the output, in the screen's pixels (lgemu_video_screen) */
    u32 w, h;
    u32 width, height;     /* the input (the stream's size), 0 if not known */
    u32 input, input_port; /* webos-video.h's input (3: VDEC) and its port */
};
/* The screen the outputs are in (1920x1080 until set) */
void lgemu_video_screen(u32 w, u32 h);
/* The MAIN window and the screen it is in, as the SoC's scaler model last published them, for a
 * capture of what it shows. */
void lgemu_video_capture_geometry(struct lgemu_video_window *main, u32 *width, u32 *height);
/* Window idx is now w; event: what changed it (enum webos_video_event) */
void lgemu_video_window(unsigned int idx, const struct lgemu_video_window *w, u32 event);

/* Fresh host-rendered capture; only the active provider can satisfy it. */
bool lgemu_capture_available(void);
int lgemu_capture_frame(u32 mode, u32 width, u32 height, u8 *rgba);
/* A capture's RGBA (width x height, packed) as semi-planar YCbCr, the luma at y_plane, the
 * interleaved CbCr at uv_plane, both stride bytes a row: 4:2:0 (yc420) or 4:2:2. width and height
 * even. */
void lgemu_capture_to_yc(const u8 *rgba, u32 width, u32 height, u8 *y_plane, u8 *uv_plane,
                         u32 stride, bool yc420);

/* ---- the V4L2 capture node (v4l2-capture.c) ---- */

/* /dev/video60, libvtcapture's V4L2 capture of the TV picture (lgemu_capture_frame), added by the
 * SoC driver module whose vendor's kernel has it (its v4l2_capture parameter; the launcher's
 * targets.v4l2_capture) and owned by it. -EBUSY when it is there already. */
#define lgemu_v4l2_capture_add() __lgemu_v4l2_capture_add(THIS_MODULE)
int __lgemu_v4l2_capture_add(struct module *owner);
/* undoes lgemu_v4l2_capture_add, if it added the node */
void lgemu_v4l2_capture_remove(void);

/* ---- the secondary screen's frame buffer (fb.c) ---- */

/* A memory frame buffer at /dev/fb<node>, "fbnode:widthxheight" (32 bpp), for the compositor's
 * second display: lgemu's own `fb`, or a SoC driver module's. -EBUSY when one is there already;
 * 0 without registering when the kernel gives another node. */
int lgemu_fb_add(const char *spec);
/* undoes lgemu_fb_add */
void lgemu_fb_remove(void);

#endif
