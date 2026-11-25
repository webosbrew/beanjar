// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* Scaler video-to-memory capture and its public VT ioctl contract. */
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

/* The scaler's video-to-memory capture, Realtek's /dev/vivtdev (below, "VT"):
 * node:width:height:fps[:buffers], the node of nodes= whose ioctls are VT's, the capture's largest
 * frame, the display's rate and the most frame buffers the driver has (its VT_MAX_BUFFER_NUM, the
 * addresses in the all-buffer property: 5, the default, from K5LP on; 3 on W17P's K3LP, whose
 * property is 36 bytes). */
static char *vt;
module_param(vt, charp, 0444);
MODULE_PARM_DESC(vt, "the VT capture device, node:max_width:max_height:framerate[:buffers]");

/* ---- VT: the scaler's capture to memory (Realtek's tvscalercontrol/scaler_vtdev.c) ----
 *
 * KADP_VT_* (libkadaptor) under bsppart's libhal_vt, under the rootfs's libdile_vt, under
 * tvservice's tv.capture. A handle (DILE_VT_Create/CreateEx) is InitEx with a buffer count, then
 * the capabilities, the limitation and every frame buffer's property; libhal_vt expands the
 * property's flat addresses into DILE's ppPhysicalAddress[buffer][plane], plane 1 of a semi-planar
 * format at a fixed pa + 0x1FD000. The capture maps those addresses through tvservice's /dev/mem.
 * The model is Realtek's driver with an idle scaler: the stock defaults (NV16, half the largest
 * frame, the display output, enabled and not frozen), its checks of the window and the region, the
 * state as set. Two differences: InitEx with one buffer succeeds without an active input (Realtek's
 * refuses it, and tvservice's capture creates every handle that way), and nothing writes the
 * frames. The buffers are DCU1's, reserved at load for five (the most InitEx takes), each a power
 * of two covering the largest NV16 frame at libhal_vt's plane offset. Each ioctl copies the
 * structure the 32-bit KADP passes (vt_cmd_id.h), not the size its number names, which is stale for
 * some. A refusal is Realtek's -1. */
#define VT_IOC_MAGIC        't'
#define VT_NR_INIT          1
#define VT_NR_FINALIZE      2
#define VT_NR_DEVICE_CAP    3
#define VT_NR_VFB_CAP       4
#define VT_NR_VFOD_CAP      5
#define VT_NR_VFOD_LIMIT    6
#define VT_NR_ALLVFB_PROP   7
#define VT_NR_VFB_INDEX     8
#define VT_NR_GET_STATE     9
#define VT_NR_SET_STATE     10
#define VT_NR_FRAMERATE     11
#define VT_NR_GET_DUMP      12
#define VT_NR_SET_DUMP      13
#define VT_NR_OUTPUT_INFO   14
#define VT_NR_OUTPUT_REGION 15
#define VT_NR_WAIT_VSYNC    16
#define VT_NR_INPUT_VIDEO   17
#define VT_NR_OUTPUT_VIDEO  18
#define VT_NR_MUTE          19
#define VT_NR_SV_STATE      20
#define VT_NR_GET_BLOCK     21
#define VT_NR_SET_BLOCK     22
#define VT_NR_DATASHOW      23
#define VT_NR_INITEX        24
#define VT_NR_FRC_MODE      27

#define VT_BUFFERS_MAX     5
#define VT_PLANE1_OFFSET   0x1FD000 /* libhal_vt's chroma plane */
#define VT_PF_YUV422_SP    4        /* KADP_VT_VIDEO_FRAME_BUFFER_PIXEL_FORMAT_T */
#define VT_PLANES_SEMI     2
#define VT_DISPLAY_OUTPUT  1 /* KADP_VT_DUMP_LOCATION_TYPE_T, 0..2 */
#define VT_PROGRESSIVE     1
#define VT_STATE_ENABLED   0x01
#define VT_STATE_FREEZED   0x02
#define VT_STATE_FR_DIVIDE 0x10
#define VT_MIN_W           60 /* the scale-down limit */
#define VT_MIN_H           34

struct vt_rect {
    u16 x, y, w, h;
};
struct vt_vfod_state {
    u8 enabled, freezed, applied_pq;
    u32 fr_divide;
};
static_assert(sizeof(struct vt_vfod_state) == 8);

static struct {
    char node[NODE_NAME_LEN];
    u32 max_w, max_h, fps;
    u32 nbuf; /* the most buffers, at most VT_BUFFERS_MAX */
    u64 pa;   /* nbuf buffers of unit bytes */
    unsigned long unit;
    struct mutex lock;
    bool inited;
    u32 buffers, location, block;
    u16 cap_w, cap_h;
    struct vt_vfod_state state;
} vtd = {.lock = __MUTEX_INITIALIZER(vtd.lock)};

static void vt_defaults(u32 buffers)
{
    vtd.buffers = buffers;
    vtd.location = VT_DISPLAY_OUTPUT;
    vtd.cap_w = vtd.max_w / 2;
    vtd.cap_h = vtd.max_h / 2;
    vtd.state = (struct vt_vfod_state){.enabled = 1, .applied_pq = 1};
}

int rtk_vt_setup(void)
{
    u32 v[4] = {0, 0, 0, VT_BUFFERS_MAX};

    if (!vt)
        return 0;
    if ((lgemu_parse_named(vt, vtd.node, NODE_NAME_LEN, 0, v, 4) &&
         lgemu_parse_named(vt, vtd.node, NODE_NAME_LEN, 0, v, 3)) ||
        !v[0] || !v[1] || !v[2] || v[0] > U16_MAX || v[1] > U16_MAX || !v[3] ||
        v[3] > VT_BUFFERS_MAX) {
        pr_err("vt=%s: not node:max_width:max_height:framerate[:buffers]\n", vt);
        return -EINVAL;
    }
    vtd.nbuf = v[3];
    if (!rtk_node_exists(vtd.node)) {
        pr_err("vt=%s: %s is not one of nodes=\n", vt, vtd.node);
        return -EINVAL;
    }
    vtd.max_w = v[0];
    vtd.max_h = v[1];
    vtd.fps = v[2];
    vtd.unit = roundup_pow_of_two(VT_PLANE1_OFFSET + (unsigned long)v[0] * v[1]);
    vtd.pa = rtk_dcu_take(0, (vtd.nbuf * vtd.unit) >> PAGE_SHIFT);
    if (!vtd.pa) {
        pr_err("vt: no %lu KiB in DCU1 for the frame buffers\n", vtd.nbuf * vtd.unit >> 10);
        return -ENOMEM;
    }
    vt_defaults(min(3u, vtd.nbuf)); /* Realtek's VtBufferNum before the first InitEx */
    pr_info("vt: %s, up to %ux%u, %u buffers at %#llx\n", vtd.node, vtd.max_w, vtd.max_h, vtd.nbuf,
            vtd.pa);
    return 0;
}

/* copies the caller's structure in, and the answer out when ret is 0 */
#define VT_IN(s)                                                                                   \
    do {                                                                                           \
        if (copy_from_user(&(s), uarg, sizeof(s)))                                                 \
            return -EFAULT;                                                                        \
    } while (0)
#define VT_OUT(s) (copy_to_user(uarg, &(s), sizeof(s)) ? -EFAULT : 0)

static long vt_ioctl_locked(unsigned int nr, void __user *uarg)
{
    switch (nr) {
    case VT_NR_INIT:
    case VT_NR_INITEX: {
        u32 n = vtd.nbuf;

        if (nr == VT_NR_INITEX)
            VT_IN(n);
        if (!n || n > vtd.nbuf)
            n = vtd.nbuf;
        if (!vtd.inited) { /* a second InitEx keeps the first's count */
            vt_defaults(n);
            vtd.inited = true;
        }
        return 0;
    }
    case VT_NR_FINALIZE:
        if (!vtd.inited)
            return -1;
        vtd.inited = false;
        vt_defaults(vtd.nbuf);
        vtd.state.enabled = 0;
        return 0;
    case VT_NR_DEVICE_CAP: {
        u32 windows = 1;

        return VT_OUT(windows);
    }
    case VT_NR_VFB_CAP: {
        struct {
            s32 wid;
            u32 buffers, planes;
        } a;

        VT_IN(a);
        if (a.wid)
            return -1;
        a.buffers = vtd.buffers;
        a.planes = VT_PLANES_SEMI;
        return VT_OUT(a);
    }
    case VT_NR_VFOD_CAP: {
        struct {
            s32 wid;
            u8 enabled, freezed, fr_divide, pq;
        } a;

        VT_IN(a);
        if (a.wid)
            return -1;
        a.enabled = a.freezed = 1;
        a.fr_divide = a.pq = 0;
        return VT_OUT(a);
    }
    case VT_NR_VFOD_LIMIT: {
        struct {
            s32 wid;
            struct vt_rect max;
            u8 left_top, in_deint, disp_deint, up;
            u32 up_w, up_h;
            u8 down;
            u32 down_w, down_h;
        } a;
        static_assert(sizeof(a) == 36);

        VT_IN(a);
        if (a.wid)
            return -1;
        memset((u8 *)&a + 4, 0, sizeof(a) - 4);
        a.max = (struct vt_rect){0, 0, vtd.max_w, vtd.max_h};
        a.down = 1;
        a.down_w = VT_MIN_W;
        a.down_h = VT_MIN_H;
        return VT_OUT(a);
    }
    case VT_NR_ALLVFB_PROP: {
        /* wid, format, stride, width, height, pa[nbuf], buffers: 44 bytes with five */
        u32 a[5 + VT_BUFFERS_MAX + 1];
        size_t n = (5 + vtd.nbuf + 1) * sizeof(u32);

        if (copy_from_user(a, uarg, n))
            return -EFAULT;
        if (a[0] || !vtd.inited)
            return -1;
        a[1] = VT_PF_YUV422_SP;
        a[2] = roundup(vtd.cap_w, 96);
        a[3] = vtd.cap_w;
        a[4] = vtd.cap_h;
        for (u32 i = 0; i < vtd.nbuf; i++)
            a[5 + i] = i < vtd.buffers ? vtd.pa + i * vtd.unit : 0;
        a[5 + vtd.nbuf] = vtd.buffers;
        return copy_to_user(uarg, a, n) ? -EFAULT : 0;
    }
    case VT_NR_VFB_INDEX: { /* the frozen buffer: the first */
        struct {
            s32 wid;
            u32 index;
        } a;

        VT_IN(a);
        if (a.wid || !vtd.inited)
            return -1;
        vtd.state.freezed = 1;
        a.index = 0;
        return VT_OUT(a);
    }
    case VT_NR_GET_STATE: {
        struct {
            s32 wid;
            struct vt_vfod_state s;
        } a;

        VT_IN(a);
        if (a.wid || !vtd.inited)
            return -1;
        a.s = vtd.state;
        return VT_OUT(a);
    }
    case VT_NR_SET_STATE: {
        struct {
            s32 wid;
            u32 flags;
            struct vt_vfod_state s;
        } a;

        VT_IN(a);
        if (a.wid)
            return -1;
        if (!a.flags)
            return 0;
        if (!vtd.inited)
            return -1;
        if (a.flags & VT_STATE_ENABLED)
            vtd.state.enabled = a.s.enabled;
        if (a.flags & VT_STATE_FR_DIVIDE)
            vtd.state.fr_divide = a.s.fr_divide;
        if (a.flags & VT_STATE_FREEZED)
            vtd.state.freezed = a.s.freezed;
        return 0;
    }
    case VT_NR_FRAMERATE: {
        struct {
            s32 wid;
            u32 fps;
        } a;

        VT_IN(a);
        if (a.wid || !vtd.inited)
            return -1;
        a.fps = vtd.fps;
        return VT_OUT(a);
    }
    case VT_NR_GET_DUMP: {
        struct {
            s32 wid;
            u32 location;
        } a;

        VT_IN(a);
        if (a.wid || !vtd.inited)
            return -1;
        a.location = vtd.location;
        return VT_OUT(a);
    }
    case VT_NR_SET_DUMP: {
        struct {
            s32 wid;
            u32 location;
        } a;

        VT_IN(a);
        if (a.wid || a.location > 2 || !vtd.inited)
            return -1;
        vtd.location = a.location;
        vtd.state.applied_pq = a.location != 0;
        return 0;
    }
    case VT_NR_OUTPUT_INFO: { /* the frame's size; the video fills it */
        struct {
            s32 wid;
            u32 location, stride;
            struct vt_rect max, active;
        } a;
        static_assert(sizeof(a) == 28);

        VT_IN(a);
        if (a.wid)
            return -1;
        if (!vtd.inited || a.location != vtd.location) {
            memset(&a.stride, 0, sizeof(a) - 8);
            return VT_OUT(a) ?: -1;
        }
        a.stride = vtd.cap_w;
        a.max = a.active = (struct vt_rect){0, 0, vtd.cap_w, vtd.cap_h};
        return VT_OUT(a);
    }
    case VT_NR_OUTPUT_REGION: {
        struct {
            s32 wid;
            u32 location;
            struct vt_rect r;
        } a;

        VT_IN(a);
        if (a.wid || a.r.w > vtd.max_w || a.r.h > vtd.max_h || !vtd.inited || a.location > 2)
            return -1;
        if (a.location == vtd.location && a.r.w == vtd.cap_w && a.r.h == vtd.cap_h)
            return 0;
        if (a.r.x || a.r.y || a.r.w < VT_MIN_W || a.r.h < VT_MIN_H)
            return -1;
        vtd.cap_w = a.r.w;
        vtd.cap_h = a.r.h;
        vtd.location = a.location;
        vtd.state.applied_pq = a.location != 0;
        return 0;
    }
    case VT_NR_INPUT_VIDEO: { /* no input: an empty region, as Realtek's */
        struct {
            s32 wid;
            u32 type;
            struct vt_rect r;
            u8 is3d;
        } a;
        static_assert(sizeof(a) == 20);

        VT_IN(a);
        if (a.wid)
            return -1;
        memset((u8 *)&a + 4, 0, sizeof(a) - 4);
        a.type = VT_PROGRESSIVE;
        return VT_OUT(a) ?: (vtd.inited ? 0 : -1);
    }
    case VT_NR_OUTPUT_VIDEO: { /* the display's active area, the main window full screen */
        struct {
            s32 wid;
            u32 type;
            struct vt_rect max, active;
        } a;
        static_assert(sizeof(a) == 24);

        VT_IN(a);
        if (a.wid)
            return -1;
        memset((u8 *)&a + 4, 0, sizeof(a) - 4);
        a.type = VT_PROGRESSIVE;
        if (!vtd.inited)
            return VT_OUT(a) ?: -1;
        a.max = a.active = (struct vt_rect){0, 0, vtd.max_w, vtd.max_h};
        return VT_OUT(a);
    }
    case VT_NR_MUTE:     /* no video: the scaler's output is muted */
    case VT_NR_SV_STATE: /* not secure video */
    case VT_NR_GET_BLOCK: {
        struct {
            s32 wid;
            u8 on;
        } a;

        VT_IN(a);
        if (a.wid)
            return -1;
        a.on = nr == VT_NR_MUTE ? 1 : nr == VT_NR_GET_BLOCK ? vtd.block : 0;
        return VT_OUT(a) ?: (vtd.inited || nr != VT_NR_MUTE ? 0 : -1);
    }
    case VT_NR_SET_BLOCK: {
        struct {
            s32 wid;
            u8 on;
        } a;

        VT_IN(a);
        if (a.wid)
            return -1;
        vtd.block = a.on;
        return 0;
    }
    case VT_NR_DATASHOW:
    case VT_NR_FRC_MODE:
        return 0;
    }
    return -ENOTTY;
}

long rtk_vt_ioctl(unsigned int cmd, unsigned long arg)
{
    unsigned int nr = _IOC_NR(cmd);
    long ret;

    if (_IOC_TYPE(cmd) != VT_IOC_MAGIC)
        return rtk_mark_unhandled(vtd.node, cmd, -ENOTTY);
    if (nr == VT_NR_WAIT_VSYNC) { /* a frame of the display's rate, as Realtek's */
        s32 wid;

        if (copy_from_user(&wid, (void __user *)arg, sizeof(wid)))
            return -EFAULT;
        if (wid || !READ_ONCE(vtd.inited))
            return -1;
        msleep_interruptible(1000 / vtd.fps);
        return 0;
    }
    mutex_lock(&vtd.lock);
    ret = vt_ioctl_locked(nr, (void __user *)arg);
    mutex_unlock(&vtd.lock);
    if (ret == -ENOTTY)
        return rtk_mark_unhandled(vtd.node, cmd, -ENOTTY);
    return ret;
}

bool rtk_vt_node(const char *name)
{
    return vt && !strcmp(name, vtd.node);
}
