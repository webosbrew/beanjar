// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libkadaptor for the emulator on M2 (W16R): tv.capture's answers, beside kadaptor-xc.c.
 *
 * tv.capture's video capture (HAL_VT, bsppart's libhal_vt) stands on W16R's user-mode XC, and
 * on its DIP, which writes a frame of the scaler's output into MMAP region 66: neither has a
 * picture here. So the answers HAL_VT asks for are given from what the TV set: XC status's capture
 * window from the last SetWindow (HAL_VT compares a source capture's region with it), the display
 * DE window from the panel (a capture of the output: SCREEN, DISPLAY, VIDEO), the main window's
 * display window from the last SetWindow too (HAL_VT places the video in the frame by it), and
 * the DIP's buffer from its window property; a one-frame capture fills that buffer with the
 * viewer's picture of the video, read through video60 (lgemu.ko's capture node, as NV16: DIP's
 * YC422, the Y plane then the interleaved CbCr plane, which tvservice maps through /dev/mem).
 * W15U's KADP and libhal_vt use other functions (and a kernel-mode DIP): not built there. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <pthread.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <syslog.h>
#include <unistd.h>

#include "../../../../../include/webos-capture.h"
#include "kadaptor-xc.h"

static void *stock(const char *name)
{
    void *f = dlsym(RTLD_NEXT, name);
    if (!f)
        syslog(LOG_ERR, "libkadaptor (emulator): stock %s not found", name);
    return f;
}

/* MS_BOOL MAdp_XC_GetStatus(XC_ApiStatus *, SCALER_WIN): XC_ApiStatus (148 bytes) begins as
 * XC_SETWIN_INFO does: the input source, stCapWin {x, y, width, height} at +4, stDispWin at +12,
 * stCropWin at +20 and bInterlace at +28. The user-mode XC works them out from registers that
 * hold nothing here (W16R's: a height of 65521 for SetWindow's 480): they are the last
 * SetWindow's. */
uint32_t MAdp_XC_GetStatus(void *status, uint32_t window)
{
    static uint32_t (*f)(void *, uint32_t);
    uint32_t ret;
    if (!f)
        f = stock(__func__);
    ret = f ? f(status, window) : 0;
    if (!status || window >= 2)
        return ret;
    pthread_mutex_lock(&setwin_lock);
    if (setwin_len[window] >= 29) {
        memcpy((uint8_t *)status + 4, setwin[window] + 4, 8);
        ((uint8_t *)status)[28] = setwin[window][28];
    }
    pthread_mutex_unlock(&setwin_lock);
    return ret;
}

/* void mbrg_SC_get_disp_de_window(MS_WINDOW_TYPE *): the display's DE window {x, y, w, h}, the
 * panel's active area, which the user-mode XC reads from registers too (1x1 here, from their
 * zeros): it is the panel's size (devPanel_WIDTH, devPanel_HEIGHT: the panel data KADP was set
 * up with). */
uint32_t mbrg_SC_get_disp_de_window(uint16_t *rect)
{
    static uint32_t (*f)(uint16_t *);
    static uint32_t (*width)(void), (*height)(void);
    uint32_t ret;
    if (!f) {
        width = stock("devPanel_WIDTH");
        height = stock("devPanel_HEIGHT");
        f = stock(__func__);
    }
    ret = f ? f(rect) : 0;
    if (rect && rect[2] <= 1 && rect[3] <= 1 && width && height) {
        rect[0] = rect[1] = 0;
        rect[2] = (uint16_t)width();
        rect[3] = (uint16_t)height();
    }
    return ret;
}

/* void mbrg_XC_GetDispWinFromReg(MS_WINDOW_TYPE *, SCALER_WIN): the window's display window
 * {x, y, w, h} as the scaler's registers hold it. The stock asks the kernel, which has no
 * registers to read and writes nothing (W16R's caller saw x 112 on a 1920-wide window, past the
 * DE window, and refused the frame: CAPTURE_ERROR_01): it is the last SetWindow's stDispWin. */
void mbrg_XC_GetDispWinFromReg(uint16_t *rect, uint32_t window)
{
    static void (*f)(uint16_t *, uint32_t);
    if (!f)
        f = stock(__func__);
    if (f)
        f(rect, window);
    if (!rect || window >= 2)
        return;
    pthread_mutex_lock(&setwin_lock);
    if (setwin_len[window] >= 20)
        memcpy(rect, setwin[window] + 12, 8);
    pthread_mutex_unlock(&setwin_lock);
}

/* ST_XC_DIP_WINPROPERTY */
struct dip_window {
    uint8_t count;
    uint16_t width, height, pitch;
    uint32_t start, end, source;
};
/* BUFFER_INFO, 68 bytes: the DIP's buffers' Y and C planes, MIU addresses */
struct dip_buffers {
    uint8_t count;
    uint32_t y[8], c[8];
};
/* The bus address of a MIU0 address: M2's MIU0 is at 0x20000000 (HAL_VT reports the planes so) */
#define MIU0_BUS 0x20000000u

static pthread_mutex_t dip_lock = PTHREAD_MUTEX_INITIALIZER;
static struct dip_window dip;
static uint32_t dip_done; /* the interrupt status's frame-done bit (0) after a captured frame */

/* E_APIXC_ReturnValue MAdp_XC_DIP_SetDIPWinProperty(ST_XC_DIP_WINPROPERTY *, SCALER_DIP_WIN) */
uint32_t MAdp_XC_DIP_SetDIPWinProperty(struct dip_window *p, uint32_t window)
{
    static uint32_t (*f)(struct dip_window *, uint32_t);
    if (!f)
        f = stock(__func__);
    if (p && !window) {
        pthread_mutex_lock(&dip_lock);
        dip = *p;
        pthread_mutex_unlock(&dip_lock);
    }
    return f ? f(p, window) : 0;
}

/* BUFFER_INFO *MAdp_XC_DIP_GetBufInfo(SCALER_DIP_WIN) (HAL_VT copies 68 bytes from it): the one
 * buffer of the window property, YC422's Y plane at its start and the C plane after it. */
struct dip_buffers *MAdp_XC_DIP_GetBufInfo(uint32_t window)
{
    static struct dip_buffers answer;
    struct dip_buffers b;
    memset(&b, 0, sizeof b);
    pthread_mutex_lock(&dip_lock);
    if (!window && dip.start && dip.width && dip.height) {
        b.count = 1;
        b.y[0] = dip.start;
        b.c[0] = dip.start + (uint32_t)dip.width * dip.height;
    }
    pthread_mutex_unlock(&dip_lock);
    answer = b;
    return &answer;
}

static int xioctl(int fd, unsigned long cmd, void *arg)
{
    int r;
    do
        r = ioctl(fd, cmd, arg);
    while (r < 0 && errno == EINTR);
    return r;
}

#ifndef V4L2_PIX_FMT_NV16M
#define V4L2_PIX_FMT_NV16M v4l2_fourcc('N', 'M', '1', '6')
#endif

/* One frame of the viewer's video picture, width x height NV16, from video60 into the planes */
static int capture_nv16(uint32_t width, uint32_t height, uint8_t *y_plane, uint8_t *c_plane)
{
    /* video60's capture parameters (LG's V4L2_CID_EXT_CAPTURE_PLANE_PROP): the location (the video
     * plane), 0, width and height (u16 at +8 and +10), the buffer count */
    struct {
        uint32_t location, reserved;
        uint16_t width, height;
        uint32_t count;
    } prop = {WEBOS_CAPTURE_VIDEO, 0, (uint16_t)width, (uint16_t)height, 1};
    struct v4l2_ext_control ctrl = {.id = 0x00982a03, .size = sizeof prop};
    struct v4l2_ext_controls ctrls = {
        .ctrl_class = V4L2_CTRL_CLASS_USER, .count = 1, .controls = &ctrl};
    struct v4l2_format fmt = {.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE};
    struct v4l2_requestbuffers req = {
        .count = 1, .type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, .memory = V4L2_MEMORY_MMAP};
    struct v4l2_plane planes[2];
    struct v4l2_buffer buf = {.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
                              .memory = V4L2_MEMORY_MMAP,
                              .m.planes = planes,
                              .length = 2};
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, ret = -1;
    void *map[2] = {MAP_FAILED, MAP_FAILED};
    size_t size = (size_t)width * height;
    int fd = open("/dev/video60", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        return -1;
    ctrl.string = (char *)&prop; /* the payload's pointer (the SDK's header predates .ptr) */
    fmt.fmt.pix_mp.width = width;
    fmt.fmt.pix_mp.height = height;
    fmt.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV16M;
    memset(planes, 0, sizeof planes);
    if (xioctl(fd, VIDIOC_S_EXT_CTRLS, &ctrls) || xioctl(fd, VIDIOC_S_FMT, &fmt) ||
        xioctl(fd, VIDIOC_REQBUFS, &req) || xioctl(fd, VIDIOC_QUERYBUF, &buf) ||
        planes[0].length < size || planes[1].length < size)
        goto out;
    for (int i = 0; i < 2; i++) {
        map[i] = mmap(NULL, planes[i].length, PROT_READ, MAP_SHARED, fd, planes[i].m.mem_offset);
        if (map[i] == MAP_FAILED)
            goto out;
    }
    if (xioctl(fd, VIDIOC_QBUF, &buf) || xioctl(fd, VIDIOC_STREAMON, &type) ||
        xioctl(fd, VIDIOC_DQBUF, &buf))
        goto out;
    memcpy(y_plane, map[0], size);
    memcpy(c_plane, map[1], size);
    ret = 0;
    xioctl(fd, VIDIOC_STREAMOFF, &type);
out:
    for (int i = 0; i < 2; i++)
        if (map[i] != MAP_FAILED)
            munmap(map[i], planes[i].length);
    close(fd);
    return ret;
}

/* The DIP's frame: the window property's buffer, written through /dev/mem (in tvservice, its
 * private physical memory, which its capture maps the planes from) */
static void dip_fill(void)
{
    struct dip_window w;
    uint32_t bus, size, off;
    uint8_t *mem;
    int fd;
    pthread_mutex_lock(&dip_lock);
    w = dip;
    pthread_mutex_unlock(&dip_lock);
    size = (uint32_t)w.width * w.height;
    if (!size || w.width & 1 || w.end - w.start < 2 * size)
        return;
    bus = w.start + MIU0_BUS;
    off = bus & 4095;
    fd = open("/dev/mem", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        return;
    mem = mmap(NULL, off + 2 * size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, bus - off);
    close(fd);
    if (mem == MAP_FAILED)
        return;
    if (capture_nv16(w.width, w.height, mem + off, mem + off + size)) {
        syslog(LOG_INFO, "libkadaptor (emulator): DIP: no video picture from video60 (%m)");
        memset(mem + off, 16, size); /* black */
        memset(mem + off + size, 128, size);
    }
    munmap(mem, off + 2 * size);
}

/* E_APIXC_ReturnValue MAdp_XC_DIP_CapOneFrame(SCALER_DIP_WIN): the frame, done at once */
uint32_t MAdp_XC_DIP_CapOneFrame(uint32_t window)
{
    static uint32_t (*f)(uint32_t);
    uint32_t ret;
    if (!f)
        f = stock(__func__);
    ret = f ? f(window) : 0;
    if (!window) {
        dip_fill();
        __atomic_store_n(&dip_done, 1, __ATOMIC_RELEASE);
    }
    return ret;
}

/* MS_U16 MAdp_XC_DIP_GetIntStatus(SCALER_DIP_WIN): the stock reads the DIP's interrupt register
 * (0x133614); bit 0, the frame done, once CapOneFrame filled it */
uint32_t MAdp_XC_DIP_GetIntStatus(uint32_t window)
{
    static uint32_t (*f)(uint32_t);
    uint32_t ret;
    if (!f)
        f = stock(__func__);
    ret = f ? f(window) : 0;
    if (!window && __atomic_load_n(&dip_done, __ATOMIC_ACQUIRE))
        ret |= 1;
    return ret;
}

/* Mbrg_XC_DIP_ClearInt(MS_U16 mask, SCALER_DIP_WIN) */
uint32_t Mbrg_XC_DIP_ClearInt(uint32_t mask, uint32_t window)
{
    static uint32_t (*f)(uint32_t, uint32_t);
    if (!f)
        f = stock(__func__);
    if (!window && mask & 1)
        __atomic_store_n(&dip_done, 0, __ATOMIC_RELEASE);
    return f ? f(mask, window) : 0;
}
