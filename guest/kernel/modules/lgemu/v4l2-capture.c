// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* libvtcapture's ARM32 video60 ABI, without the media subsystem: what the SoC driver modules
 * (lg115x.ko, rtk.ko, mstar-utopia.ko) share, each adding the node for its vendor's kernel when
 * its own v4l2_capture parameter says so (lgemu-drv.h). LG-O's (O22N2's and the O22 family's),
 * Realtek's (W20L's libvtcapture 1.0.0-29 on) and MStar LM21's (-38, -46) clients make the same
 * calls; queue's nyxScreenCapture.module adds G_FMT and controls answered EOPNOTSUPP here.
 * Layouts and control IDs: LG W22H v4l2-controls-ext.h and vt_v4l2_cb_api.c.
 * The O22N2 client maps QUERYBUF offsets through video60 (queryBuffer,
 * 0x10f62..0x10f6c), not /dev/mem. These offsets name vmalloc-backed planes;
 * physical-address export is deliberately unsupported.
 * Frames come only from a fresh, successful viewer readback. The histogram controls
 * (0x982a14..17) are capture-histogram.c's. */
#define pr_fmt(fmt) "capture: " fmt
#include <linux/compat.h>
#include <linux/fs.h>
#include <linux/hrtimer.h>
#include <linux/kref.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/sched/signal.h>
#include <linux/timekeeping.h>
#include <linux/uaccess.h>
#include <linux/unaligned.h>
#include <linux/videodev2.h>
#include <linux/version.h>
#include <linux/vmalloc.h>
#include "lgemu.h"
#include "lgemu-drv.h"
#include "../../../../include/webos-capture.h"

#define CAP_QUERYCAP      0x80685600
#define CAP_G_FMT         0xc0cc5604
#define CAP_S_FMT         0xc0cc5605
#define CAP_REQBUFS       0xc0145608
#define CAP_QUERYBUF      0xc0445609
#define CAP_QBUF          0xc044560f
#define CAP_DQBUF         0xc0445611
#define CAP_STREAMON      0x40045612
#define CAP_STREAMOFF     0x40045613
#define CAP_G_EXT         0xc0185647
#define CAP_S_EXT         0xc0185648
#define CAP_G_CTRL        0xc008561b
#define CAP_S_CTRL        0xc008561c
#define CAP_BASE          0x00982a00
#define CAP_BUFFERS       5
#define CAP_PLANES        2
#define CAP_NV12          1 /* LG's pixel-format enum, not a V4L2 fourcc */
#define CAP_NV16          4
#define CAP_MEMORY_LIMIT  (64UL * 1024 * 1024)
#define CAP_OFFSET_STRIDE (4UL * 1024 * 1024)

static atomic_long_t allocated = ATOMIC_LONG_INIT(0);
static struct lgemu_cdev cdev;

static int unknown_show(char *buf, size_t size, const struct lgemu_count *e)
{
    return scnprintf(buf, size, "%s %#x\n", e->what, e->a);
}
/* the ioctls not emulated; .module: the owner's name (lgemu_v4l2_capture_add) */
DEFINE_LGEMU_COUNTS(unknown, 16, 32, unknown_show);

struct capture_buffer {
    struct kref ref;
    void *plane[CAP_PLANES];
    size_t size[CAP_PLANES], charged;
    bool queued, valid, frozen;
    struct timespec64 timestamp;
    u32 sequence;
};
struct capture_file {
    struct mutex lock;
    struct capture_buffer *buffer[CAP_BUFFERS];
    u8 *rgba;
    size_t rgba_size;
    u32 width, height, format, location, requested, count, next, sequence;
    ktime_t next_frame;
    int frozen;
    bool streaming, histogram;
};

static bool charge(size_t bytes)
{
    if (atomic_long_add_return(bytes, &allocated) <= CAP_MEMORY_LIMIT)
        return true;
    atomic_long_sub(bytes, &allocated);
    return false;
}

static void buffer_free(struct kref *ref)
{
    struct capture_buffer *b = container_of(ref, struct capture_buffer, ref);
    vfree(b->plane[0]);
    vfree(b->plane[1]);
    atomic_long_sub(b->charged, &allocated);
    kfree(b);
}

static void release_buffers(struct capture_file *f)
{
    unsigned int i;
    f->streaming = false;
    for (i = 0; i < f->count; i++) {
        kref_put(&f->buffer[i]->ref, buffer_free);
        f->buffer[i] = NULL;
    }
    f->count = f->next = 0;
    f->frozen = -1;
    vfree(f->rgba);
    f->rgba = NULL;
    atomic_long_sub(f->rgba_size, &allocated);
    f->rgba_size = 0;
}

static bool valid_size(u32 width, u32 height)
{
    return width >= 2 && height >= 2 && width <= WEBOS_CAPTURE_WIDTH &&
           height <= WEBOS_CAPTURE_HEIGHT && !(width & 1) && !(height & 1);
}

static int request_buffers(struct capture_file *f, void __user *arg)
{
    u32 req[5];
    size_t y_size = f->width * f->height;
    size_t c_size = f->format == CAP_NV12 ? y_size / 2 : y_size;
    unsigned int i;
    int ret = -ENOMEM;
    if (copy_from_user(req, arg, sizeof(req)))
        return -EFAULT;
    if (req[1] != V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE || req[2] != V4L2_MEMORY_MMAP ||
        req[0] > CAP_BUFFERS)
        return -EINVAL;
    if (f->streaming)
        return -EBUSY;
    release_buffers(f);
    if (!req[0])
        return 0;
    if (!charge(PAGE_ALIGN(y_size * 4)))
        return -ENOMEM;
    f->rgba_size = PAGE_ALIGN(y_size * 4);
    f->rgba = vzalloc(f->rgba_size);
    if (!f->rgba)
        goto fail;
    for (i = 0; i < req[0]; i++) {
        struct capture_buffer *b = kzalloc(sizeof(*b), GFP_KERNEL);
        if (!b)
            goto fail;
        kref_init(&b->ref);
        f->buffer[f->count++] = b;
        b->size[0] = y_size;
        b->size[1] = c_size;
        if (!charge(PAGE_ALIGN(y_size) + PAGE_ALIGN(c_size)))
            goto fail;
        b->charged = PAGE_ALIGN(y_size) + PAGE_ALIGN(c_size);
        b->plane[0] = vmalloc_user(PAGE_ALIGN(y_size));
        b->plane[1] = vmalloc_user(PAGE_ALIGN(c_size));
        if (!b->plane[0] || !b->plane[1])
            goto fail;
    }
    req[3] = req[4] = 0;
    if (!copy_to_user(arg, req, sizeof(req)))
        return 0;
    ret = -EFAULT;
fail:
    release_buffers(f);
    return ret;
}

/* No frame is invented when the transport cannot provide RGBA. */
static void convert(struct capture_file *f, struct capture_buffer *b)
{
    lgemu_capture_to_yc(f->rgba, f->width, f->height, b->plane[0], b->plane[1], f->width,
                        f->format == CAP_NV12);
}

static int capture_frame(struct capture_file *f, unsigned int index)
{
    struct capture_buffer *b = f->buffer[index];
    int ret;
    /* This device supplies at most 30 fresh captures/second. The output-rate
     * query describes this enforced cadence, not an invented source FPS. */
    while (ktime_before(ktime_get(), f->next_frame)) {
        set_current_state(TASK_INTERRUPTIBLE);
        schedule_hrtimeout(&f->next_frame, HRTIMER_MODE_ABS);
        if (signal_pending(current))
            return -ERESTARTSYS;
    }
    f->next_frame = ktime_add_ns(ktime_get(), NSEC_PER_SEC / 30);
    b->valid = false;
    ret = lgemu_capture_frame(f->location, f->width, f->height, f->rgba);
    if (ret)
        return ret;
    convert(f, b);
    ktime_get_ts64(&b->timestamp);
    b->sequence = f->sequence++;
    b->valid = true;
    return 0;
}

static u32 plane_offset(unsigned int index, unsigned int plane)
{
    /* Offsets are opaque MMAP tokens, not physical addresses. */
    return (index * CAP_PLANES + plane + 1) * CAP_OFFSET_STRIDE;
}

static int buffer_ioctl(struct file *file, unsigned int cmd, void __user *arg)
{
    struct capture_file *f = file->private_data;
    struct capture_buffer *b;
    u32 words[17], planes[CAP_PLANES][15];
    unsigned int index, i;
    int ret;
    if (copy_from_user(words, arg, sizeof(words)))
        return -EFAULT;
    if (words[1] != V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE || words[12] != V4L2_MEMORY_MMAP ||
        words[14] != CAP_PLANES)
        return -EINVAL;
    if (!access_ok(compat_ptr(words[13]), sizeof(planes)))
        return -EFAULT;
    index = words[0];
    if (cmd == CAP_DQBUF) {
        if (!f->streaming)
            return -EINVAL;
        if (!lgemu_capture_available())
            return -ENODEV;
        if (file->f_flags & O_NONBLOCK)
            return -EOPNOTSUPP; /* The stock client opens O_RDWR. */
        for (i = 0; i < f->count; i++) {
            index = (f->next + i) % f->count;
            if (f->buffer[index]->queued && !f->buffer[index]->frozen)
                break;
        }
        if (i == f->count)
            return -EAGAIN;
        ret = capture_frame(f, index);
        if (ret)
            return ret;
    }
    if (index >= f->count)
        return -EINVAL;
    b = f->buffer[index];
    if (cmd == CAP_QBUF) {
        if (b->queued)
            return -EBUSY;
        b->queued = true;
        return 0;
    }
    memset(planes, 0, sizeof(planes));
    for (i = 0; i < CAP_PLANES; i++) {
        planes[i][0] = b->valid ? b->size[i] : 0;
        planes[i][1] = b->size[i];
        planes[i][2] = plane_offset(index, i);
    }
    words[0] = index;
    words[2] = 0; /* bytesused is per-plane for MPLANE. */
    words[3] = b->queued ? V4L2_BUF_FLAG_QUEUED : 0;
    words[4] = V4L2_FIELD_NONE;
    memset(words + 5, 0, 7 * sizeof(u32));
    if (b->valid) {
        words[3] |= V4L2_BUF_FLAG_DONE | V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC;
        words[5] = b->timestamp.tv_sec;
        words[6] = b->timestamp.tv_nsec / NSEC_PER_USEC;
        words[11] = b->sequence;
    }
    words[15] = words[16] = 0;
    if (cmd == CAP_DQBUF)
        words[3] &= ~V4L2_BUF_FLAG_QUEUED;
    if (copy_to_user(compat_ptr(words[13]), planes, sizeof(planes)) ||
        copy_to_user(arg, words, sizeof(words)))
        return -EFAULT;
    if (cmd == CAP_DQBUF) {
        b->queued = false;
        f->next = (index + 1) % f->count;
    }
    return 0;
}

static void rect(u8 *p, unsigned int width, unsigned int height)
{
    put_unaligned_le16(0, p);
    put_unaligned_le16(0, p + 2);
    put_unaligned_le16(width, p + 4);
    put_unaligned_le16(height, p + 6);
}

static int extended(struct file *file, unsigned int cmd, void __user *arg)
{
    struct capture_file *f = file->private_data;
    u32 group[6], control[5], id, size;
    u8 data[40] = {0};
    void __user *ptr;
    bool get = cmd == CAP_G_EXT;
    int ret;
    if (copy_from_user(group, arg, sizeof(group)))
        return -EFAULT;
    if (group[0] != V4L2_CTRL_CLASS_USER || group[1] != 1)
        return -EINVAL;
    if (copy_from_user(control, compat_ptr(group[5]), sizeof(control)))
        return -EFAULT;
    id = control[0];
    size = control[1];
    ptr = compat_ptr(control[3]);
    if (id >= LGEMU_HIST_START && id <= LGEMU_HIST_STOP)
        return lgemu_capture_histogram(&f->histogram, get, id, size, control[3]);
    switch (id) {
    case CAP_BASE:
        size = 40;
        break;
    case CAP_BASE + 1:
        size = 20;
        break;
    case CAP_BASE + 2:
        size = 28;
        break;
    case CAP_BASE + 3:
        size = 16;
        break;
    case CAP_BASE + 4:
        size = 8;
        break;
    default:
        return -EOPNOTSUPP;
    }
    if (control[1] != size)
        return -EINVAL;
    if (!get && copy_from_user(data, ptr, size))
        return -EFAULT;
    switch (id) {
    case CAP_BASE:
        if (!get)
            return -EOPNOTSUPP;
        put_unaligned_le32(8, data); /* scale down; no hardware deinterlacing */
        put_unaligned_le32(2, data + 12);
        put_unaligned_le32(2, data + 16);
        put_unaligned_le32(CAP_BUFFERS, data + 20);
        rect(data + 24, WEBOS_CAPTURE_WIDTH, WEBOS_CAPTURE_HEIGHT);
        put_unaligned_le32(CAP_PLANES, data + 32);
        put_unaligned_le32(f->format, data + 36);
        break;
    case CAP_BASE + 1:
        if (!get)
            return -EOPNOTSUPP;
        put_unaligned_le32(f->width, data);
        rect(data + 4, f->width, f->height);
        rect(data + 12, f->width, f->height);
        break;
    case CAP_BASE + 2:
        if (!get)
            return -EOPNOTSUPP;
        if (!lgemu_capture_available())
            return -ENODEV;
        {
            struct lgemu_video_window main;
            u32 panel_w, panel_h;
            lgemu_video_capture_geometry(&main, &panel_w, &panel_h);
            /* Progressive rendered surfaces in the modeled panel coordinate
             * space; the requested capture dimensions are independent. */
            put_unaligned_le32(1, data);
            rect(data + 20, panel_w, panel_h);
            if (f->location == WEBOS_CAPTURE_VIDEO) {
                if (!main.open || !main.connected || !main.has_rect || !main.width ||
                    !main.height || !main.w || !main.h)
                    return -ENODATA;
                rect(data + 4, main.width, main.height);
                rect(data + 12, main.w, main.h);
                put_unaligned_le16(main.x, data + 12);
                put_unaligned_le16(main.y, data + 14);
            } else {
                rect(data + 4, panel_w, panel_h);
                rect(data + 12, panel_w, panel_h);
            }
        }
        break;
    case CAP_BASE + 3:
        if (!get) {
            u32 location = get_unaligned_le32(data);
            u32 width = get_unaligned_le16(data + 8);
            u32 height = get_unaligned_le16(data + 10);
            u32 count = get_unaligned_le32(data + 12);
            if (f->count || f->streaming)
                return -EBUSY;
            if (location < WEBOS_CAPTURE_VIDEO || location > WEBOS_CAPTURE_OSD ||
                get_unaligned_le32(data + 4))
                return -EOPNOTSUPP;
            if (!valid_size(width, height) || !count || count > CAP_BUFFERS)
                return -EINVAL;
            f->location = location;
            f->width = width;
            f->height = height;
            f->requested = count;
        } else {
            put_unaligned_le32(f->location, data);
            rect(data + 4, f->width, f->height);
            put_unaligned_le32(f->count ? f->count : f->requested, data + 12);
        }
        break;
    case CAP_BASE + 4:
        if (get) {
            put_unaligned_le32(f->frozen, data);
            data[4] = f->frozen >= 0;
        } else {
            u32 index = get_unaligned_le32(data);
            if (!f->streaming || index >= f->count || data[4] > 1)
                return -EINVAL;
            if (data[4]) {
                if (file->f_flags & O_NONBLOCK)
                    return -EOPNOTSUPP;
                ret = capture_frame(f, index);
                if (ret)
                    return ret;
                if (f->frozen >= 0)
                    f->buffer[f->frozen]->frozen = false;
                f->buffer[index]->frozen = true;
                f->frozen = index;
            } else {
                f->buffer[index]->frozen = false;
                if (f->frozen == index)
                    f->frozen = -1;
            }
        }
        break;
    }
    return get && copy_to_user(ptr, data, size) ? -EFAULT : 0;
}

static int format_ioctl(struct capture_file *f, unsigned int cmd, void __user *arg)
{
    u8 data[204];
    u32 width, height, format;
    if (copy_from_user(data, arg, sizeof(data)))
        return -EFAULT;
    if (get_unaligned_le32(data) != V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
        return -EINVAL;
    if (cmd == CAP_S_FMT) {
        if (f->count || f->streaming)
            return -EBUSY;
        width = get_unaligned_le32(data + 4);
        height = get_unaligned_le32(data + 8);
        format = get_unaligned_le32(data + 12);
        /* The stock setFMT sends zero pixelformat and the requested size. */
        if (!format)
            format = f->format;
        else if (format == V4L2_PIX_FMT_NV12M)
            format = CAP_NV12;
        else if (format == V4L2_PIX_FMT_NV16M)
            format = CAP_NV16;
        else
            return -EOPNOTSUPP;
        if (!valid_size(width, height))
            return -EINVAL;
        f->width = width;
        f->height = height;
        f->format = format;
    }
    memset(data + 4, 0, sizeof(data) - 4);
    put_unaligned_le32(f->width, data + 4);
    put_unaligned_le32(f->height, data + 8);
    put_unaligned_le32(f->format == CAP_NV12 ? V4L2_PIX_FMT_NV12M : V4L2_PIX_FMT_NV16M, data + 12);
    put_unaligned_le32(V4L2_FIELD_NONE, data + 16);
    put_unaligned_le32(V4L2_COLORSPACE_SMPTE170M, data + 20);
    put_unaligned_le32(f->width * f->height, data + 24);
    put_unaligned_le32(f->width, data + 28);
    put_unaligned_le32(f->width * f->height / (f->format == CAP_NV12 ? 2 : 1), data + 44);
    put_unaligned_le32(f->width, data + 48);
    data[184] = CAP_PLANES;
    data[186] = V4L2_YCBCR_ENC_601;
    data[187] = V4L2_QUANTIZATION_LIM_RANGE;
    return copy_to_user(arg, data, sizeof(data)) ? -EFAULT : 0;
}

static long capture_ioctl_locked(struct file *file, unsigned int cmd, void __user *arg)
{
    struct capture_file *f = file->private_data;
    u32 type;
    unsigned int i;
    switch (cmd) {
    case CAP_QUERYCAP: {
        struct v4l2_capability cap = {0};
        strscpy(cap.driver, "lg-capture", sizeof(cap.driver));
        strscpy(cap.card, "Viewer capture", sizeof(cap.card));
        strscpy(cap.bus_info, "platform:lgemu", sizeof(cap.bus_info));
        cap.version = KERNEL_VERSION(1, 0, 0);
        cap.device_caps = V4L2_CAP_VIDEO_CAPTURE_MPLANE | V4L2_CAP_STREAMING;
        cap.capabilities = cap.device_caps | V4L2_CAP_DEVICE_CAPS;
        return copy_to_user(arg, &cap, sizeof(cap)) ? -EFAULT : 0;
    }
    case CAP_G_FMT:
    case CAP_S_FMT:
        return format_ioctl(f, cmd, arg);
    case CAP_REQBUFS:
        return request_buffers(f, arg);
    case CAP_QUERYBUF:
    case CAP_QBUF:
    case CAP_DQBUF:
        return buffer_ioctl(file, cmd, arg);
    case CAP_G_EXT:
    case CAP_S_EXT:
        return extended(file, cmd, arg);
    case CAP_STREAMON:
    case CAP_STREAMOFF:
        if (copy_from_user(&type, arg, sizeof(type)))
            return -EFAULT;
        if (type != V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
            return -EINVAL;
        if (cmd == CAP_STREAMON) {
            if (f->streaming)
                return 0;
            if (!f->count)
                return -EINVAL;
            if (!lgemu_capture_available())
                return -ENODEV;
            for (i = 0; i < f->count && !f->buffer[i]->queued; i++)
                ;
            if (i == f->count)
                return -EINVAL;
            f->streaming = true;
            f->next_frame = 0;
        } else {
            f->streaming = false;
            f->frozen = -1;
            for (i = 0; i < f->count; i++) {
                f->buffer[i]->queued = false;
                f->buffer[i]->frozen = false;
                f->buffer[i]->valid = false;
            }
        }
        return 0;
    case CAP_G_CTRL: {
        u32 control[2];
        if (copy_from_user(control, arg, sizeof(control)))
            return -EFAULT;
        if (control[0] != CAP_BASE + 7)
            return -EOPNOTSUPP;
        control[1] = 30;
        return copy_to_user(arg, control, sizeof(control)) ? -EFAULT : 0;
    }
    case CAP_S_CTRL:
        return -EOPNOTSUPP; /* No divider or VTV processing is advertised. */
    default:
        return lgemu_ioctl_unhandled(&unknown, "video60", cmd, -ENOTTY);
    }
}

static long capture_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct capture_file *f = file->private_data;
    long ret;
    if (!in_compat_syscall())
        return -ENOTTY; /* Explicit ARM32 ABI; never reinterpret native pointers. */
    if (mutex_lock_interruptible(&f->lock))
        return -ERESTARTSYS;
    ret = capture_ioctl_locked(file, cmd, compat_ptr(arg));
    mutex_unlock(&f->lock);
    return ret;
}

static void mapping_open(struct vm_area_struct *vma)
{
    struct capture_buffer *b = vma->vm_private_data;
    kref_get(&b->ref);
}

static void mapping_close(struct vm_area_struct *vma)
{
    struct capture_buffer *b = vma->vm_private_data;
    kref_put(&b->ref, buffer_free);
}

static const struct vm_operations_struct capture_vm_ops = {
    .open = mapping_open,
    .close = mapping_close,
};

static int capture_mmap(struct file *file, struct vm_area_struct *vma)
{
    struct capture_file *f = file->private_data;
    unsigned long offset = vma->vm_pgoff << PAGE_SHIFT;
    unsigned int index, plane;
    struct capture_buffer *b;
    int ret = -EINVAL;
    if (!(vma->vm_flags & VM_SHARED) ||
        vma->vm_pgoff > (CAP_BUFFERS * CAP_PLANES * CAP_OFFSET_STRIDE) >> PAGE_SHIFT || !offset ||
        offset % CAP_OFFSET_STRIDE)
        return -EINVAL;
    index = offset / CAP_OFFSET_STRIDE - 1;
    plane = index % CAP_PLANES;
    index /= CAP_PLANES;
    mutex_lock(&f->lock);
    if (index >= f->count)
        goto out;
    b = f->buffer[index];
    if (vma->vm_end - vma->vm_start > PAGE_ALIGN(b->size[plane]))
        goto out;
    ret = remap_vmalloc_range(vma, b->plane[plane], 0);
    if (ret)
        goto out;
    vma->vm_ops = &capture_vm_ops;
    vma->vm_private_data = b;
    vm_flags_set(vma, VM_DONTEXPAND | VM_DONTDUMP);
    mapping_open(vma);
out:
    mutex_unlock(&f->lock);
    return ret;
}

static int capture_open(struct inode *inode, struct file *file)
{
    struct capture_file *f = kzalloc(sizeof(*f), GFP_KERNEL);
    if (!f)
        return -ENOMEM;
    mutex_init(&f->lock);
    f->width = WEBOS_CAPTURE_WIDTH;
    f->height = WEBOS_CAPTURE_HEIGHT;
    f->format = CAP_NV12;
    f->location = WEBOS_CAPTURE_BLENDED;
    f->requested = CAP_BUFFERS;
    f->frozen = -1;
    file->private_data = f;
    return 0;
}

static int capture_release(struct inode *inode, struct file *file)
{
    struct capture_file *f = file->private_data;
    lgemu_capture_histogram_stop(&f->histogram);
    release_buffers(f);
    kfree(f);
    return 0;
}

static struct file_operations fops = {
    .open = capture_open,
    .release = capture_release,
    .unlocked_ioctl = capture_ioctl,
    .compat_ioctl = capture_ioctl,
    .mmap = capture_mmap,
};

static char *capture_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return NULL;
}
static const struct class capture_class = {.name = "lg-capture", .devnode = capture_devnode};

int __lgemu_v4l2_capture_add(struct module *owner)
{
    struct device *dev;
    int ret;
    if (cdev.added)
        return -EBUSY;
    fops.owner = owner;
    unknown.module = module_name(owner);
    ret = class_register(&capture_class);
    if (ret)
        return ret;
    ret = lgemu_cdev_add(&cdev, 0, 0, 1, "v4l2-capture", &fops);
    if (ret)
        goto fail_class;
    dev = device_create(&capture_class, NULL, cdev.dev, NULL, "video60");
    if (!IS_ERR(dev)) {
        pr_info("video60 for %s\n", module_name(owner));
        return 0;
    }
    ret = PTR_ERR(dev);
    lgemu_cdev_del(&cdev);
fail_class:
    class_unregister(&capture_class);
    return ret;
}
EXPORT_SYMBOL_GPL(__lgemu_v4l2_capture_add);

void lgemu_v4l2_capture_remove(void)
{
    if (!cdev.added)
        return;
    device_destroy(&capture_class, cdev.dev);
    lgemu_cdev_del(&cdev);
    class_unregister(&capture_class);
}
EXPORT_SYMBOL_GPL(lgemu_v4l2_capture_remove);
