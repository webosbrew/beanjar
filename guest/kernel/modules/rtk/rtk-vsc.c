// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Legacy and V4L2 scaler windows, controls and per-open event queues. */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#include <linux/fb.h>
#include <linux/vmalloc.h>
#include <linux/anon_inodes.h>
#include <linux/bitmap.h>
#include <linux/compat.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/kobject.h>
#include <linux/math64.h>
#include <linux/mm.h>
#include <linux/mman.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/poll.h>
#include <linux/proc_fs.h>
#include <linux/sched.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include <linux/workqueue.h>
#include <linux/unaligned.h>

#include "lgemu-drv.h"
#include "lgemu-i2c.h"
#include "rtk-internal.h"
#include "../../../../include/webos-video.h"

/* ---- the scaler's windows: Realtek's VSC, legacy and V4L2 ----
 *
 * The media video window is tvservice's (K5LP) or videooutputd's (K6LP, K7LP), set on the scaler
 * through the stock HAL/KADP or libval-starfish; the windows they set are what lgemu.ko sends the
 * viewer (lgemu_video_window, include/webos-video.h), as mstar-utopia.ko's XC does on MStar.
 *
 * Legacy (vsc=, K5LP's /dev/vscdev; scratch/d4-k5-vsc.md): KADP_VSC_* in libkadaptor issue
 * Realtek's VSC ioctls, {u32 window; ...} with VIDEO_RECT_T {u16 x, y, w, h}: Open, Close,
 * Connect/Disconnect {wid; VSC_INPUT_TYPE type (3 VDEC); attr | resourceIndex << 8 (the VDEC
 * port); output mode}, SetWinBlank {wid; u8 on; colour}, SetInputRegion(Ex) and SetOutputRegion,
 * and the combined SetInputOutputRegion {wid; rotation; in; original in; out} that tvservice's
 * window and aspect paths use (two of them bare numbers without a size: 0x7144 20 bytes, 0x7156
 * 32). The rectangles are in the panel's pixels (tvservice scales the OSD's by panel / graphic).
 *
 * V4L2 (v4l2_vsc=, K6LP/K7LP's /dev/video30 and video31, main and sub; scratch/d4-v4l2-vsc.md):
 * libval-starfish's V4l2vsc sets Realtek's VSC controls (V4L2_CID_USER_EXT_VSC_BASE 0x982900)
 * with VIDIOC_S_EXT_CTRLS, one control whose payload a 32-bit pointer inside the 32-bit
 * v4l2_ext_control (packed, 20 bytes: id, size, reserved, ptr at 12) inside v4l2_ext_controls
 * (24 bytes: class, count, error_idx, reserved[2], controls at 20) points to: WIN_REGION (28 bytes:
 * crop, original input, output rects, rotation), CONNECT {u32 src (4 VDEC); u8 index (the VDEC
 * port); u8 attr; u32 dest}, and blanks with VIDIOC_S_CTRL V4L2_CID_BG_COLOR. Readbacks give the
 * stored values. Events (VIDIOC_SUBSCRIBE_EVENT, DQEVENT; V4L2_EVENT_CTRL) are per open file:
 * applying-done-window-region (u.data: window, x, y, w, h, delay as u16 LE) and active-window
 * after each region, mute-off at an unblank; DQEVENT waits for one. */
#define VSC_WINDOWS    2
#define VSC_INPUT_VDEC 3 /* VSC_INPUT_TYPE_T's, and include/webos-video.h's */

static DEFINE_MUTEX(vsc_lock);
static struct lgemu_video_window vsc_win[VSC_WINDOWS];

/* a window changed (vsc_lock held) */
static void vsc_publish(unsigned int idx, u32 event)
{
    lgemu_video_window(idx, &vsc_win[idx], event);
}

struct vsc_rect {
    u16 x, y, w, h;
};

/* Legacy: KADP_VSC_*'s ioctls (scratch/d4-k5-vsc.md section 3) */
#define VSC_OPEN                0x40047103
#define VSC_CLOSE               0x40047104
#define VSC_CONNECT             0x40107105
#define VSC_DISCONNECT          0x40107106
#define VSC_SET_WIN_BLANK       0x400c7107
#define VSC_GET_INPUT_REGION    0x80087108
#define VSC_SET_INPUT_REGION    0x400c7109
#define VSC_GET_OUTPUT_REGION   0x8008710a
#define VSC_SET_OUTPUT_REGION   0x400c710b
#define VSC_GET_INPUT_ADAPTIVE  0x80087138
#define VSC_SET_INPUT_ADAPTIVE  0x400c7139
#define VSC_SET_INPUT_REGION_EX 0x00007144 /* bare: 20 bytes */
#define VSC_SET_IN_OUT_REGION   0x00007156 /* bare: 32 bytes */

long rtk_vsc_legacy_ioctl(unsigned int cmd, unsigned long arg)
{
    u8 buf[32];
    size_t size = cmd == VSC_SET_INPUT_REGION_EX ? 20
                  : cmd == VSC_SET_IN_OUT_REGION ? 32
                                                 : _IOC_SIZE(cmd);
    struct lgemu_video_window *w;
    struct vsc_rect r;
    u32 wid, v[3];
    long ret = 0;

    switch (cmd) {
    case VSC_OPEN:
    case VSC_CLOSE:
    case VSC_CONNECT:
    case VSC_DISCONNECT:
    case VSC_SET_WIN_BLANK:
    case VSC_GET_INPUT_REGION:
    case VSC_SET_INPUT_REGION:
    case VSC_GET_OUTPUT_REGION:
    case VSC_SET_OUTPUT_REGION:
    case VSC_GET_INPUT_ADAPTIVE:
    case VSC_SET_INPUT_ADAPTIVE:
    case VSC_SET_INPUT_REGION_EX:
    case VSC_SET_IN_OUT_REGION:
        break;
    default:
        return 0; /* the notices, z-order, HDR, ...: accepted, as before */
    }
    if (size > sizeof(buf) || copy_from_user(buf, (void __user *)arg, size))
        return -EFAULT;
    memcpy(&wid, buf, 4);
    if (wid >= VSC_WINDOWS)
        return -EINVAL;
    mutex_lock(&vsc_lock);
    w = &vsc_win[wid];
    switch (cmd) {
    case VSC_OPEN:
        *w = (struct lgemu_video_window){.open = true};
        vsc_publish(wid, WEBOS_VIDEO_REGISTER);
        break;
    case VSC_CLOSE:
        *w = (struct lgemu_video_window){};
        vsc_publish(wid, WEBOS_VIDEO_UNREGISTER);
        break;
    case VSC_CONNECT:
    case VSC_DISCONNECT:
        memcpy(v, buf + 4, 12);
        w->open = true;
        w->connected = cmd == VSC_CONNECT;
        if (w->connected) {
            w->input = v[0];
            w->input_port = (v[1] >> 8) & 0xff;
        }
        vsc_publish(wid, w->connected ? WEBOS_VIDEO_CONNECT : WEBOS_VIDEO_DISCONNECT);
        break;
    case VSC_SET_WIN_BLANK:
        w->mute = buf[4] != 0;
        vsc_publish(wid, WEBOS_VIDEO_MUTE);
        break;
    case VSC_SET_INPUT_REGION:
    case VSC_SET_INPUT_ADAPTIVE:
    case VSC_SET_INPUT_REGION_EX:
        memcpy(&r, buf + 4, sizeof(r));
        w->width = r.w;
        w->height = r.h;
        vsc_publish(wid, WEBOS_VIDEO_INFO);
        break;
    case VSC_SET_OUTPUT_REGION:
        memcpy(&r, buf + 4, sizeof(r));
        goto output;
    case VSC_SET_IN_OUT_REGION:
        memcpy(&r, buf + 8, sizeof(r));
        w->width = r.w;
        w->height = r.h;
        memcpy(&r, buf + 24, sizeof(r));
    output:
        w->x = r.x;
        w->y = r.y;
        w->w = r.w;
        w->h = r.h;
        w->has_rect = true;
        vsc_publish(wid, WEBOS_VIDEO_RECT);
        break;
    default: { /* the readbacks: {wid; u32 rect pointer} */
        u32 ptr;

        memcpy(&ptr, buf + 4, 4);
        r = cmd == VSC_GET_OUTPUT_REGION ? (struct vsc_rect){w->x, w->y, w->w, w->h}
                                         : (struct vsc_rect){0, 0, w->width, w->height};
        if (copy_to_user(compat_ptr(ptr), &r, sizeof(r)))
            ret = -EFAULT;
    }
    }
    mutex_unlock(&vsc_lock);
    return ret;
}

/* V4L2: the 32-bit userland's structures (scratch/d4-v4l2-vsc.md sections 3-5) */
#define V4L2_S_EXT_CTRLS       0xc0185648
#define V4L2_G_EXT_CTRLS       0xc0185647
#define V4L2_S_CTRL            0xc008561c
#define V4L2_G_CTRL            0xc008561b
#define V4L2_SUBSCRIBE_EVENT   0x4020565a
#define V4L2_UNSUBSCRIBE_EVENT 0x4020565b
#define V4L2_DQEVENT           0x80805659
#define V4L2_EVENT_CTRL        3
#define CID_BG_COLOR           0x00980923
#define CID_WIN_REGION         0x00982900
#define CID_CONNECT            0x0098290c
#define CID_ACTIVE_WIN         0x0098290d
#define EV_MUTE_OFF            0x0098291e
#define EV_ACTIVE_WIN          0x00982920
#define EV_APPLY_DONE          0x00982922
#define VSC_SCALARS            16
#define VSC_SUBS               8
#define VSC_EVENTS             16
#define VSC_EVENT_SIZE         128

static char *v4l2_vsc[VSC_WINDOWS];
static int v4l2_vsc_count;
module_param_array(v4l2_vsc, charp, &v4l2_vsc_count, 0444);
MODULE_PARM_DESC(v4l2_vsc,
                 "the V4L2 scaler nodes (of nodes=), main and sub window (video30,video31)");
static char *vsc;
module_param(vsc, charp, 0444);
MODULE_PARM_DESC(vsc, "the legacy VSC node (of nodes=), KADP_VSC_*'s (vscdev)");
static unsigned int vsc_screen[2];
static int vsc_screen_count;
module_param_array(vsc_screen, uint, &vsc_screen_count, 0444);
MODULE_PARM_DESC(vsc_screen, "the panel the scaler's output rectangles are in, width,height");

/* each window's stored controls, for the readbacks */
static struct {
    u8 region[28], connect[12], prop[12];
    bool has_region, has_connect, has_prop;
    struct {
        u32 id;
        s32 value;
    } scalars[VSC_SCALARS];
} vsc_ctl[VSC_WINDOWS];

/* an open file of a V4L2 scaler node: its name first (node_ioctl's private_data is the name) */
struct vsc_file {
    char name[NODE_NAME_LEN];
    unsigned int idx;
    struct list_head link;
    u32 subs[VSC_SUBS];
    unsigned int nsubs, head, count, sequence;
    u8 events[VSC_EVENTS][VSC_EVENT_SIZE];
    wait_queue_head_t wait;
};
static LIST_HEAD(vsc_files);

int rtk_vsc_index(const char *name)
{
    for (int i = 0; i < v4l2_vsc_count; i++)
        if (!strcmp(v4l2_vsc[i], name))
            return i;
    return -1;
}

/* queue event id with data on every file of window idx that subscribed to it (vsc_lock held) */
static void vsc_event(unsigned int idx, u32 id, const u8 *data, size_t n)
{
    struct vsc_file *f;

    list_for_each_entry(f, &vsc_files, link)
    {
        u8 *e;
        unsigned int i;

        if (f->idx != idx)
            continue;
        for (i = 0; i < f->nsubs && f->subs[i] != id; i++)
            ;
        if (i == f->nsubs || f->count == VSC_EVENTS)
            continue;
        e = f->events[(f->head + f->count++) % VSC_EVENTS];
        memset(e, 0, VSC_EVENT_SIZE);
        put_unaligned_le32(V4L2_EVENT_CTRL, e);
        memcpy(e + 8, data, n);
        put_unaligned_le32(f->sequence++, e + 76);
        put_unaligned_le32(id, e + 88);
        wake_up_interruptible(&f->wait);
    }
}

static s32 *vsc_scalar(unsigned int idx, u32 id, bool make)
{
    for (int i = 0; i < VSC_SCALARS; i++) {
        if (vsc_ctl[idx].scalars[i].id == id)
            return &vsc_ctl[idx].scalars[i].value;
        if (!vsc_ctl[idx].scalars[i].id && make) {
            vsc_ctl[idx].scalars[i].id = id;
            return &vsc_ctl[idx].scalars[i].value;
        }
    }
    return NULL;
}

/* a control set: the window's state and its events (vsc_lock held) */
static void vsc_v4l2_set(unsigned int idx, u32 id, const u8 *p, u32 size)
{
    struct lgemu_video_window *w = &vsc_win[idx];
    u8 data[11];

    switch (id) {
    case CID_WIN_REGION:
        if (size < 28)
            return;
        memcpy(vsc_ctl[idx].region, p, 28);
        vsc_ctl[idx].has_region = true;
        w->open = true;
        w->width = get_unaligned_le16(p + 12);
        w->height = get_unaligned_le16(p + 14);
        w->x = get_unaligned_le16(p + 16);
        w->y = get_unaligned_le16(p + 18);
        w->w = get_unaligned_le16(p + 20);
        w->h = get_unaligned_le16(p + 22);
        w->has_rect = true;
        vsc_publish(idx, WEBOS_VIDEO_RECT);
        /* applied at once: {window, x, y, w, h, delay 0} */
        data[0] = idx;
        memcpy(data + 1, p + 16, 8);
        data[9] = data[10] = 0;
        vsc_event(idx, EV_APPLY_DONE, data, sizeof(data));
        vsc_event(idx, EV_ACTIVE_WIN, data, 1);
        break;
    case CID_CONNECT: {
        u32 src = get_unaligned_le32(p), dest = get_unaligned_le32(p + 8);

        if (size < 12)
            return;
        memcpy(vsc_ctl[idx].connect, p, 12);
        vsc_ctl[idx].has_connect = true;
        w->open = true;
        w->connected = src && dest;
        if (w->connected) {
            w->input = src - 1; /* v4l2_ext_vsc_input_src: 1 AVD ... 4 VDEC */
            w->input_port = p[4];
        }
        vsc_publish(idx, w->connected ? WEBOS_VIDEO_CONNECT : WEBOS_VIDEO_DISCONNECT);
        break;
    }
    default:
        if (id == 0x00982902 && size >= 12) {
            memcpy(vsc_ctl[idx].prop, p, 12);
            vsc_ctl[idx].has_prop = true;
        }
        break;
    }
}

/* a readback into p (size bytes); false: nothing stored, the payload is left as it is */
static bool vsc_v4l2_get(unsigned int idx, u32 id, u8 *p, u32 size)
{
    const struct lgemu_video_window *w = &vsc_win[idx];

    switch (id) {
    case CID_WIN_REGION:
        if (!vsc_ctl[idx].has_region || size < 28)
            return false;
        memcpy(p, vsc_ctl[idx].region, 28);
        return true;
    case CID_CONNECT:
        if (!vsc_ctl[idx].has_connect || size < 12)
            return false;
        memcpy(p, vsc_ctl[idx].connect, 12);
        return true;
    case 0x00982902:
        if (!vsc_ctl[idx].has_prop || size < 12)
            return false;
        memcpy(p, vsc_ctl[idx].prop, 12);
        return true;
    case CID_ACTIVE_WIN: /* the whole input is active: {original, active} */
        if (size < 16 || !w->width)
            return false;
        memset(p, 0, 16);
        put_unaligned_le16(w->width, p + 4);
        put_unaligned_le16(w->height, p + 6);
        put_unaligned_le16(w->width, p + 12);
        put_unaligned_le16(w->height, p + 14);
        return true;
    }
    return false;
}

long rtk_vsc_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct vsc_file *f = file->private_data;
    unsigned int idx = f->idx;
    void __user *uarg = (void __user *)arg;
    long ret = 0;

    switch (cmd) {
    case V4L2_S_EXT_CTRLS:
    case V4L2_G_EXT_CTRLS: {
        u32 ctrls[6], ctrl[5];
        u8 payload[64];

        if (copy_from_user(ctrls, uarg, sizeof(ctrls)))
            return -EFAULT;
        if (ctrls[1] != 1) /* V4l2vsc sets one control at a time */
            return 0;
        if (copy_from_user(ctrl, compat_ptr(ctrls[5]), sizeof(ctrl)))
            return -EFAULT;
        if (!ctrl[1] || ctrl[1] > sizeof(payload))
            return 0; /* a scalar in the union, or not one of the scaler's */
        if (copy_from_user(payload, compat_ptr(ctrl[3]), ctrl[1]))
            return -EFAULT;
        mutex_lock(&vsc_lock);
        if (cmd == V4L2_S_EXT_CTRLS) {
            vsc_v4l2_set(idx, ctrl[0], payload, ctrl[1]);
        } else if (vsc_v4l2_get(idx, ctrl[0], payload, ctrl[1]) &&
                   copy_to_user(compat_ptr(ctrl[3]), payload, ctrl[1])) {
            ret = -EFAULT;
        }
        mutex_unlock(&vsc_lock);
        return ret;
    }
    case V4L2_S_CTRL:
    case V4L2_G_CTRL: {
        s32 c[2], *v;

        if (copy_from_user(c, uarg, sizeof(c)))
            return -EFAULT;
        mutex_lock(&vsc_lock);
        if (cmd == V4L2_S_CTRL) {
            v = vsc_scalar(idx, c[0], true);
            if (v)
                *v = c[1];
            if (c[0] == CID_BG_COLOR) {
                bool was = vsc_win[idx].mute;
                u8 wid = idx;

                vsc_win[idx].mute = c[1] != 0;
                vsc_publish(idx, WEBOS_VIDEO_MUTE);
                if (was && !c[1])
                    vsc_event(idx, EV_MUTE_OFF, &wid, 1);
            }
        } else {
            v = vsc_scalar(idx, c[0], false);
            if (v) {
                c[1] = *v;
                if (copy_to_user(uarg, c, sizeof(c)))
                    ret = -EFAULT;
            }
        }
        mutex_unlock(&vsc_lock);
        return ret;
    }
    case V4L2_SUBSCRIBE_EVENT:
    case V4L2_UNSUBSCRIBE_EVENT: {
        u32 sub[2];
        unsigned int i;

        if (copy_from_user(sub, uarg, sizeof(sub)))
            return -EFAULT;
        mutex_lock(&vsc_lock);
        for (i = 0; i < f->nsubs && f->subs[i] != sub[1]; i++)
            ;
        if (cmd == V4L2_SUBSCRIBE_EVENT && i == f->nsubs && f->nsubs < VSC_SUBS)
            f->subs[f->nsubs++] = sub[1];
        else if (cmd == V4L2_UNSUBSCRIBE_EVENT && i < f->nsubs)
            f->subs[i] = f->subs[--f->nsubs];
        mutex_unlock(&vsc_lock);
        return 0;
    }
    case V4L2_DQEVENT: {
        u8 e[VSC_EVENT_SIZE];

        for (;;) {
            mutex_lock(&vsc_lock);
            if (f->count) {
                memcpy(e, f->events[f->head], sizeof(e));
                f->head = (f->head + 1) % VSC_EVENTS;
                f->count--;
                put_unaligned_le32(f->count, e + 72); /* pending */
                mutex_unlock(&vsc_lock);
                return copy_to_user(uarg, e, sizeof(e)) ? -EFAULT : 0;
            }
            mutex_unlock(&vsc_lock);
            if (file->f_flags & O_NONBLOCK)
                return -ENOENT;
            ret = wait_event_interruptible(f->wait, READ_ONCE(f->count));
            if (ret)
                return ret;
        }
    }
    }
    return 0;
}

int rtk_vsc_open(struct file *file, const char *name, int idx)
{
    struct vsc_file *f = kzalloc(sizeof(*f), GFP_KERNEL);

    if (!f)
        return -ENOMEM;
    strscpy(f->name, name, sizeof(f->name));
    f->idx = idx;
    init_waitqueue_head(&f->wait);
    mutex_lock(&vsc_lock);
    list_add(&f->link, &vsc_files);
    mutex_unlock(&vsc_lock);
    file->private_data = f;
    return 0;
}

/* A scaler file's poll, as V4L2's for events (v4l2_event_pending): EPOLLPRI while one is queued,
 * nothing otherwise, woken by vsc_event. libval-starfish's Val_Thread epolls video30 and video31
 * for IN or PRI and calls DQEVENT only once its events are on; an always-ready answer made it spin
 * in epoll_wait. */
__poll_t rtk_vsc_poll(struct file *file, struct poll_table_struct *wait)
{
    struct vsc_file *f = file->private_data;

    poll_wait(file, &f->wait, wait);
    return READ_ONCE(f->count) ? EPOLLPRI : 0;
}

int rtk_vsc_release(struct inode *inode, struct file *file)
{
    struct vsc_file *f = file->private_data;

    if (rtk_vsc_index(file->private_data) < 0)
        return 0;
    mutex_lock(&vsc_lock);
    list_del(&f->link);
    mutex_unlock(&vsc_lock);
    kfree(f);
    return 0;
}

int rtk_vsc_setup(void)
{
    if (vsc_screen_count == 2 && vsc_screen[0] && vsc_screen[1])
        lgemu_video_screen(vsc_screen[0], vsc_screen[1]);
    if (vsc || v4l2_vsc_count)
        pr_info("scaler windows: %s%s%s%s, screen %ux%u\n", vsc ? vsc : "",
                v4l2_vsc_count ? " " : "", v4l2_vsc_count ? v4l2_vsc[0] : "",
                v4l2_vsc_count > 1 ? " and sub" : "", vsc_screen[0], vsc_screen[1]);
    return 0;
}

const char *rtk_vsc_counter_name(int idx)
{
    /* module_param_array is read-only while loaded; this outlives per-open state. */
    return v4l2_vsc[idx];
}

bool rtk_vsc_legacy_node(const char *name)
{
    return vsc && !strcmp(name, vsc);
}
