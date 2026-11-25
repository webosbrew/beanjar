// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* LG's V4L2 scaler (v4l2-ext VSC) as libval-starfish's V4l2vsc drives it, control-only: MAIN and
 * SUB at /dev/video30 and video31, their VDO routes at video28 and video29. The stock videooutputd
 * sets the regions and VDEC ports; lgemu transports that real state to host decoding. Unsupported
 * image processing and frame readback return errors. LG's own interface, the same calls on every
 * SoC whose VAL uses it (O22..O22N2 over lg115x.ko, LM21AN..LM21ANN over mstar-utopia.ko):
 * v4l2_vsc=1 and the panel's coordinates (vsc_screen) turn it on. */
#define pr_fmt(fmt) KBUILD_MODNAME ": vsc: " fmt
#include <linux/compat.h>
#include <linux/fs.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/poll.h>
#include <linux/slab.h>
#include <linux/timekeeping.h>
#include <linux/uaccess.h>
#include <linux/unaligned.h>
#include "lgemu.h"
#include "lgemu-drv.h"
#include "../../../../include/webos-video.h"

#define WINDOWS        2
#define NODES          (WINDOWS * 2)
#define EVENTS         16
#define SUBS           8
#define S_EXT          0xc0185648
#define G_EXT          0xc0185647
#define S_CTRL         0xc008561c
#define G_CTRL         0xc008561b
#define SUBSCRIBE      0x4020565a
#define UNSUBSCRIBE    0x4020565b
#define DQEVENT        0x80805659
#define REGION         0x00982900
#define CONNECT        0x0098290c
#define ORBIT_WINDOW   0x00982911
#define ORBIT_MOVE     0x00982912
#define PLANE_NUM      0x00982926
#define BG_COLOR       0x00980923
#define VDO_CONNECT    0x00988e02
#define VDO_DISCONNECT 0x00988e03
#define EV_MUTE_OFF    0x0098291e
#define EV_APPLIED     0x00982922

static bool v4l2_vsc;
module_param(v4l2_vsc, bool, 0444);
MODULE_PARM_DESC(v4l2_vsc, "enable the modeled MAIN/SUB V4L2 scaler (video30,video31)");
static unsigned int vsc_screen[2];
static int vsc_screen_count;
module_param_array(vsc_screen, uint, &vsc_screen_count, 0444);
MODULE_PARM_DESC(vsc_screen, "the panel coordinate space, width,height");
static DEFINE_MUTEX(lock);
static struct {
    struct lgemu_video_window window;
    u8 region[36], connect[12];
    u8 orbit[2][32]; /* OLED pixel-orbit window and move: four rects, kept as set */
    unsigned int region_size;
    bool has_region, has_connect;
    bool vsc_connected, vdo_connected;
    u32 vdo_port;
} state[WINDOWS];
struct vsc_file {
    struct list_head link;
    unsigned int index, nsubs, head, count, sequence;
    bool vdo;
    u32 subs[SUBS];
    u8 events[EVENTS][128];
    wait_queue_head_t wait;
};
static LIST_HEAD(files);
static struct lgemu_cdev cdev;
static bool class_added;
static char *vsc_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return NULL; /* video30/video31 live directly under /dev. */
}
static const struct class vsc_class = {.name = "lg-vsc", .devnode = vsc_devnode};
static int seen_show(char *buf, size_t size, const struct lgemu_count *e)
{
    return scnprintf(buf, size, "%s %#x %lu\n", e->what, e->a, e->count);
}
DEFINE_LGEMU_COUNTS(seen, 32, 48, seen_show);

/* Caller holds lock. Events describe completed model changes, never a
 * synthetic frame or a successful operation on missing hardware. */
static void event(unsigned int index, u32 id, const void *data, size_t size)
{
    struct vsc_file *f;
    struct timespec64 now;
    ktime_get_real_ts64(&now);
    list_for_each_entry(f, &files, link)
    {
        unsigned int i;
        u8 *e;
        if (f->index != index || f->vdo)
            continue;
        for (i = 0; i < f->nsubs && f->subs[i] != id; i++)
            ;
        if (i == f->nsubs)
            continue;
        if (f->count == EVENTS) {
            f->head = (f->head + 1) % EVENTS;
            f->count--;
        }
        e = f->events[(f->head + f->count++) % EVENTS];
        memset(e, 0, 128);
        put_unaligned_le32(3, e); /* V4L2_EVENT_CTRL */
        memcpy(e + 8, data, size);
        put_unaligned_le32(f->sequence++, e + 76);
        put_unaligned_le32(now.tv_sec, e + 80);
        put_unaligned_le32(now.tv_nsec, e + 84);
        put_unaligned_le32(id, e + 88);
        wake_up_interruptible(&f->wait);
    }
}

/* A scaler connection is usable only while the VDO mux routes the requested
 * VDEC port to that window. Disconnecting either device retires the route. */
static void publish(unsigned int index, u32 reason)
{
    struct lgemu_video_window *w = &state[index].window;
    w->connected = state[index].vsc_connected && state[index].vdo_connected &&
                   w->input_port == state[index].vdo_port;
    lgemu_video_window(index, w, reason);
}

static long vdo_ioctl(struct vsc_file *f, unsigned int cmd, void __user *arg)
{
    u32 group[6], control[5], data[2];
    void __user *ptr;
    if (cmd != S_EXT && cmd != G_EXT)
        return -ENOTTY;
    if (copy_from_user(group, arg, sizeof(group)))
        return -EFAULT;
    if (group[0] != 0x00980000 || group[1] != 1)
        return -EINVAL;
    if (copy_from_user(control, compat_ptr(group[5]), sizeof(control)))
        return -EFAULT;
    if (control[0] != VDO_CONNECT && control[0] != VDO_DISCONNECT)
        return -EOPNOTSUPP;
    if (control[1] != sizeof(data))
        return -EINVAL;
    ptr = compat_ptr(control[3]);
    if (cmd == G_EXT) {
        if (control[0] != VDO_CONNECT)
            return -EOPNOTSUPP;
        mutex_lock(&lock);
        if (!state[f->index].vdo_connected) {
            mutex_unlock(&lock);
            return -ENODATA;
        }
        data[0] = f->index;
        data[1] = state[f->index].vdo_port;
        mutex_unlock(&lock);
        return copy_to_user(ptr, data, sizeof(data)) ? -EFAULT : 0;
    }
    if (copy_from_user(data, ptr, sizeof(data)))
        return -EFAULT;
    if (data[0] != f->index)
        return -EINVAL;
    if (control[0] == VDO_CONNECT && data[1] > 255)
        return -EOPNOTSUPP; /* VSC's VDEC resource index is one byte. */
    mutex_lock(&lock);
    state[f->index].vdo_connected = control[0] == VDO_CONNECT;
    if (state[f->index].vdo_connected)
        state[f->index].vdo_port = data[1];
    /* Disconnect's second word is not initialized by the native client. */
    publish(f->index, state[f->index].vdo_connected ? WEBOS_VIDEO_CONNECT : WEBOS_VIDEO_DISCONNECT);
    mutex_unlock(&lock);
    return 0;
}

static long extended(struct vsc_file *f, unsigned int cmd, void __user *arg)
{
    u32 group[6], control[5], id, size;
    u8 payload[36], done[11];
    void __user *ptr;
    struct lgemu_video_window *w = &state[f->index].window;
    long ret = 0;
    if (copy_from_user(group, arg, sizeof(group)))
        return -EFAULT;
    if (group[0] != 0x00980000 || group[1] != 1)
        return -EINVAL;
    if (copy_from_user(control, compat_ptr(group[5]), sizeof(control)))
        return -EFAULT;
    id = control[0];
    size = control[1];
    /* libval-starfish's V4l2vsc::getPlaneNum (webOS 24 and 25) sizes its scalers by this into an
     * uninitialised word, failure or not; nyxVscaler (webOS 26) takes 1 to 6 and falls back to 2.
     * The scaler has WINDOWS windows. */
    if (id == PLANE_NUM) {
        if (cmd != G_EXT || size != 4)
            return -EINVAL;
        return put_user(WINDOWS, (u32 __user *)compat_ptr(control[3])) ? -EFAULT : 0;
    }
    if (id != REGION && id != CONNECT && id != ORBIT_WINDOW && id != ORBIT_MOVE)
        return -EOPNOTSUPP;
    /* The region is 28 bytes, {input, original, output rects (u16 x, y, w, h), u32 rotation}; W22O
     * queue's nyxVscaler appends the output rect in the app's coordinates (36): read the same. */
    if (id == REGION ? size != 28 && size != 36 : id == CONNECT ? size != 12 : size != 32)
        return -EINVAL;
    ptr = compat_ptr(control[3]);
    if (cmd == S_EXT && copy_from_user(payload, ptr, size))
        return -EFAULT;
    /* Rotated images have no emulated producer. An input crop inside the source (live TV's
     * overscan: videooutputd's DTV input [11,7,682,466] of [0,0,704,480]) is accepted and
     * answered back, as rtk-vsc does, but not yet applied: the host shows the whole source, as
     * webos-video.h carries no crop (a follow-up). One outside the source is refused. */
    if (cmd == S_EXT && id == REGION &&
        (get_unaligned_le32(payload + 24) ||
         (get_unaligned_le16(payload + 12) &&
          (get_unaligned_le16(payload) + get_unaligned_le16(payload + 4) >
               get_unaligned_le16(payload + 12) ||
           get_unaligned_le16(payload + 2) + get_unaligned_le16(payload + 6) >
               get_unaligned_le16(payload + 14)))))
        return -EOPNOTSUPP;
    if (cmd == S_EXT && id == CONNECT &&
        ((get_unaligned_le32(payload) != 0 && get_unaligned_le32(payload) != 4) ||
         get_unaligned_le32(payload + 8) > 1 || payload[5]))
        return -EOPNOTSUPP;
    mutex_lock(&lock);
    if (id == ORBIT_WINDOW || id == ORBIT_MOVE) {
        /* Nothing to shift on an emulated panel: keep the payload and report it back. */
        u8 *orbit = state[f->index].orbit[id == ORBIT_MOVE];
        if (cmd == S_EXT) {
            memcpy(orbit, payload, size);
            pr_debug("window %u orbit %s set\n", f->index, id == ORBIT_MOVE ? "move" : "window");
        } else {
            memcpy(payload, orbit, size);
        }
    } else if (cmd == S_EXT && id == REGION) {
        memcpy(state[f->index].region, payload, size);
        state[f->index].region_size = size;
        state[f->index].has_region = true;
        w->open = w->has_rect = true;
        w->width = get_unaligned_le16(payload + 12);
        w->height = get_unaligned_le16(payload + 14);
        w->x = get_unaligned_le16(payload + 16);
        w->y = get_unaligned_le16(payload + 18);
        w->w = get_unaligned_le16(payload + 20);
        w->h = get_unaligned_le16(payload + 22);
        publish(f->index, WEBOS_VIDEO_RECT);
        done[0] = f->index;
        memcpy(done + 1, payload + 16, 8);
        done[9] = done[10] = 0;
        event(f->index, EV_APPLIED, done, sizeof(done));
    } else if (cmd == S_EXT) {
        memcpy(state[f->index].connect, payload, size);
        state[f->index].has_connect = true;
        w->open = true;
        state[f->index].vsc_connected =
            get_unaligned_le32(payload) && get_unaligned_le32(payload + 8);
        if (state[f->index].vsc_connected) {
            w->input = 3; /* VDEC */
            w->input_port = payload[4];
        }
        publish(f->index,
                state[f->index].vsc_connected ? WEBOS_VIDEO_CONNECT : WEBOS_VIDEO_DISCONNECT);
    } else if (id == REGION && state[f->index].has_region && state[f->index].region_size == size) {
        memcpy(payload, state[f->index].region, size);
    } else if (id == CONNECT && state[f->index].has_connect) {
        memcpy(payload, state[f->index].connect, size);
    } else {
        ret = -ENODATA;
    }
    mutex_unlock(&lock);
    if (!ret && cmd == G_EXT && copy_to_user(ptr, payload, size))
        ret = -EFAULT;
    return ret;
}

static long vsc_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct vsc_file *f = file->private_data;
    void __user *ptr = (void __user *)arg;
    u32 values[8];
    unsigned int i;
    long ret;
    if (f->vdo)
        return vdo_ioctl(f, cmd, ptr);
    switch (cmd) {
    case S_EXT:
    case G_EXT:
        return extended(f, cmd, ptr);
    case S_CTRL:
    case G_CTRL:
        if (copy_from_user(values, ptr, 8))
            return -EFAULT;
        if (values[0] != BG_COLOR)
            return -EOPNOTSUPP;
        mutex_lock(&lock);
        if (cmd == S_CTRL) {
            bool was = state[f->index].window.mute;
            u8 index = f->index;
            state[f->index].window.mute = values[1] != 0;
            publish(f->index, WEBOS_VIDEO_MUTE);
            if (was && !values[1])
                event(f->index, EV_MUTE_OFF, &index, 1);
        } else {
            values[1] = state[f->index].window.mute;
        }
        mutex_unlock(&lock);
        return cmd == G_CTRL && copy_to_user(ptr, values, 8) ? -EFAULT : 0;
    case SUBSCRIBE:
    case UNSUBSCRIBE:
        if (copy_from_user(values, ptr, sizeof(values)))
            return -EFAULT;
        if (values[0] != 3 || (values[1] != EV_APPLIED && values[1] != EV_MUTE_OFF))
            return -EINVAL;
        if (values[2])
            return -EOPNOTSUPP; /* no SEND_INITIAL or feedback events */
        mutex_lock(&lock);
        for (i = 0; i < f->nsubs && f->subs[i] != values[1]; i++)
            ;
        ret = 0;
        if (cmd == SUBSCRIBE && i == f->nsubs) {
            if (f->nsubs == SUBS)
                ret = -ENOSPC;
            else
                f->subs[f->nsubs++] = values[1];
        } else if (cmd == UNSUBSCRIBE && i < f->nsubs) {
            f->subs[i] = f->subs[--f->nsubs];
        }
        mutex_unlock(&lock);
        return ret;
    case DQEVENT:
        for (;;) {
            u8 e[128];
            mutex_lock(&lock);
            if (f->count) {
                memcpy(e, f->events[f->head], sizeof(e));
                f->head = (f->head + 1) % EVENTS;
                f->count--;
                put_unaligned_le32(f->count, e + 72);
                mutex_unlock(&lock);
                return copy_to_user(ptr, e, sizeof(e)) ? -EFAULT : 0;
            }
            mutex_unlock(&lock);
            if (file->f_flags & O_NONBLOCK)
                return -EAGAIN;
            ret = wait_event_interruptible(f->wait, READ_ONCE(f->count));
            if (ret)
                return ret;
        }
    default:
        return lgemu_ioctl_unhandled(&seen, "v4l2-vsc", cmd, -ENOTTY);
    }
}

static int vsc_open(struct inode *inode, struct file *file)
{
    unsigned int index = iminor(inode) - MINOR(cdev.dev);
    struct vsc_file *f;
    if (index >= NODES)
        return -ENODEV;
    f = kzalloc(sizeof(*f), GFP_KERNEL);
    if (!f)
        return -ENOMEM;
    f->index = index % WINDOWS;
    f->vdo = index >= WINDOWS;
    init_waitqueue_head(&f->wait);
    mutex_lock(&lock);
    list_add(&f->link, &files);
    mutex_unlock(&lock);
    file->private_data = f;
    return 0;
}

static int vsc_release(struct inode *inode, struct file *file)
{
    struct vsc_file *f = file->private_data;
    mutex_lock(&lock);
    list_del(&f->link);
    mutex_unlock(&lock);
    kfree(f);
    return 0;
}

static __poll_t vsc_poll(struct file *file, poll_table *wait)
{
    struct vsc_file *f = file->private_data;
    poll_wait(file, &f->wait, wait);
    return READ_ONCE(f->count) ? EPOLLPRI : 0;
}

static const struct file_operations fops = {
    .owner = THIS_MODULE,
    .open = vsc_open,
    .release = vsc_release,
    .unlocked_ioctl = vsc_ioctl,
    .compat_ioctl = vsc_ioctl,
    .poll = vsc_poll,
};

int lgemu_vsc_init(void)
{
    int err;
    if (!v4l2_vsc)
        return 0;
    if (vsc_screen_count != 2 || !vsc_screen[0] || !vsc_screen[1])
        return -EINVAL;
    err = class_register(&vsc_class);
    if (err)
        return err;
    class_added = true;
    err = lgemu_cdev_add(&cdev, 0, 0, NODES, "v4l2-vsc", &fops);
    if (err) {
        class_unregister(&vsc_class);
        class_added = false;
        return err;
    }
    for (unsigned int i = 0; i < NODES; i++) {
        struct device *dev = device_create(&vsc_class, NULL, cdev.dev + i, NULL, "video%u",
                                           i < WINDOWS ? 30 + i : 28 + i - WINDOWS);
        if (IS_ERR(dev)) {
            while (i--)
                device_destroy(&vsc_class, cdev.dev + i);
            lgemu_cdev_del(&cdev);
            class_unregister(&vsc_class);
            class_added = false;
            return PTR_ERR(dev);
        }
    }
    lgemu_video_screen(vsc_screen[0], vsc_screen[1]);
    return 0;
}

void lgemu_vsc_exit(void)
{
    if (!cdev.added)
        return;
    for (unsigned int i = 0; i < NODES; i++)
        device_destroy(&vsc_class, cdev.dev + i);
    lgemu_cdev_del(&cdev);
    if (class_added) {
        class_unregister(&vsc_class);
        class_added = false;
    }
}
