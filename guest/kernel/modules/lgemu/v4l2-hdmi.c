// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* LG's HDMI receiver (v4l2-ext HDMI) at /dev/video12, as libhdmi-conn-protocol's
 * HdmiConnectorDrv drives it, with no source on any port. eim downloads each port's EDID
 * through it (HdmiConnector::edid_write → HdmiEdid::write → v4l2_hdmi_write_edid), and only a
 * download that succeeds sets eim's start flag 0x80, without which eim never starts its CEC stack
 *. The EDID RAM is modelled: a port's last write is what a read returns. HPD, power-off,
 * repeater and expert settings are kept as written. Everything that would describe an incoming
 * signal reads as none: no 5 V (but on the input a sound bar of cec.c or earc.c is plugged
 * into, whose cable carries it: libhdmi-conn-protocol's 5 V poll tells eim, and libcecp turns ARC
 * on only with 5 V on its ARC input; bar_5v), and zeroed timing, infoframes and diagnostics. HDCP
 * keys have no receiver to go to and stay unsupported. The controls are LG's v4l2-ext ones (W21P's
 * dvbfrontend/v4l2 headers in LG's GPL sources), V4L2_CID_USER_EXT_HDMI_BASE 0x985900, through
 * the 32-bit S/G_EXT_CTRLS (class 0x980000, one control). v4l2_hdmi=1 turns it on. */
#define pr_fmt(fmt) KBUILD_MODNAME ": hdmi: " fmt
#include <linux/compat.h>
#include <linux/fs.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/unaligned.h>
#include <media/cec.h>
#include "lgemu.h"
#include "lgemu-drv.h"

#define S_EXT          0xc0185648
#define G_EXT          0xc0185647
#define S_CTRL         0xc008561c
#define G_CTRL         0xc008561b
#define S_INPUT        0xc0045627
#define G_INPUT        0x80045626
#define HDMI_BASE      0x00985900
#define EDID           (HDMI_BASE + 7)
#define CONNECTION     (HDMI_BASE + 8)
#define HPD            (HDMI_BASE + 9)
#define POWER_OFF      (HDMI_BASE + 10)
#define HDCP_KEY       (HDMI_BASE + 11)
#define DISCONNECT     (HDMI_BASE + 12)
#define VRR_FREQUENCY  (HDMI_BASE + 13)
#define EMP_INFO       (HDMI_BASE + 14)
#define HDCP_REPEATER  (HDMI_BASE + 15)
#define DIAG_BASE      (HDMI_BASE + 0x100)
#define EXPERT_SETTING (DIAG_BASE + 8)
#define PORTS          5 /* V4L2_EXT_HDMI_INPUT_PORT_1..5 */
#define EDID_MAX       512
#define EXPERT_TYPES   16
#define PAYLOAD_MAX    4096

static bool v4l2_hdmi;
module_param(v4l2_hdmi, bool, 0444);
MODULE_PARM_DESC(v4l2_hdmi, "enable the modeled HDMI receiver with nothing connected (video12)");
static DEFINE_MUTEX(lock);
static struct {
    u8 edid[EDID_MAX];
    u32 edid_len;
    u32 hpd, repeater[3];
    u32 expert[EXPERT_TYPES][3];
} ports[PORTS];
static u32 input, power_off;
static struct lgemu_cdev cdev;
static bool class_added;
static char *hdmi_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return NULL;
}
static const struct class hdmi_class = {.name = "lg-hdmi", .devnode = hdmi_devnode};
static int seen_show(char *buf, size_t size, const struct lgemu_count *e)
{
    return scnprintf(buf, size, "%s %#x %lu\n", e->what, e->a, e->count);
}
DEFINE_LGEMU_COUNTS(seen, 32, 48, seen_show);

/* A control that reports what arrives on a port: with no source, nothing. */
static bool signal_info(u32 id)
{
    return (id >= HDMI_BASE && id <= HDMI_BASE + 6) || id == VRR_FREQUENCY || id == EMP_INFO ||
           (id >= DIAG_BASE && id < EXPERT_SETTING);
}

static long port_of(const u8 *payload, u32 size)
{
    u32 port = size >= 4 ? get_unaligned_le32(payload) : 0;
    return port >= 1 && port <= PORTS ? port - 1 : -EINVAL;
}

/* with lock held: whether port has 5 V, which only a sound bar brings (cec.c's or earc.c's): on
 * the input whose EDID (the TV's, as eim wrote it) gives the bar's physical address PORT.0.0.0, as
 * a device on that input takes it from there; the TV numbers its inputs in the EDID, and the
 * hardware's port order is the board's (W22O: HDMI 2's EDID is on port 3) */
static bool bar_5v(long port)
{
    int bar = lgemu_cec_bar_port() ?: lgemu_earc_bar_port();

    return bar && ports[port].edid_len &&
           cec_get_edid_phys_addr(ports[port].edid, ports[port].edid_len, NULL) == bar << 12;
}

static long extended(unsigned int cmd, void __user *arg)
{
    u32 group[6], control[5], id, size;
    void __user *ptr;
    u8 *payload;
    long ret = 0, port;

    if (copy_from_user(group, arg, sizeof(group)))
        return -EFAULT;
    if (group[0] != 0x00980000 || group[1] != 1)
        return -EINVAL;
    if (copy_from_user(control, compat_ptr(group[5]), sizeof(control)))
        return -EFAULT;
    id = control[0];
    size = control[1];
    ptr = compat_ptr(control[3]);
    if (!size || size > PAYLOAD_MAX)
        return -EINVAL;
    payload = kzalloc(size, GFP_KERNEL);
    if (!payload)
        return -ENOMEM;
    if (copy_from_user(payload, ptr, size)) {
        ret = -EFAULT;
        goto out;
    }
    port = port_of(payload, size);
    mutex_lock(&lock);
    if (cmd == G_EXT && signal_info(id) && port >= 0) {
        memset(payload + 4, 0, size - 4);
    } else if (id == EDID && size == 16 && port >= 0 && get_unaligned_le32(payload + 4) <= 2) {
        u32 bytes = 128 << get_unaligned_le32(payload + 4);
        void __user *data = compat_ptr(get_unaligned_le32(payload + 8));

        mutex_unlock(&lock);
        if (cmd == S_EXT) {
            u8 *edid = kmalloc(bytes, GFP_KERNEL);

            if (!edid)
                ret = -ENOMEM;
            else if (copy_from_user(edid, data, bytes))
                ret = -EFAULT;
            if (!ret) {
                u16 pa = cec_get_edid_phys_addr(edid, bytes, NULL);

                mutex_lock(&lock);
                memcpy(ports[port].edid, edid, bytes);
                ports[port].edid_len = bytes;
                mutex_unlock(&lock);
                pr_info("port %ld: EDID of %u bytes, physical address %x.%x.%x.%x\n", port + 1,
                        bytes, pa >> 12, pa >> 8 & 0xF, pa >> 4 & 0xF, pa & 0xF);
            }
            kfree(edid);
        } else {
            u8 *edid = kmalloc(bytes, GFP_KERNEL);

            if (!edid) {
                ret = -ENOMEM;
            } else {
                mutex_lock(&lock);
                memcpy(edid, ports[port].edid, bytes);
                mutex_unlock(&lock);
                if (copy_to_user(data, edid, bytes))
                    ret = -EFAULT;
                kfree(edid);
            }
        }
        goto out;
    } else if (id == CONNECTION && cmd == G_EXT && size >= 5 && port >= 0) {
        payload[4] = bar_5v(port);
    } else if (id == HPD && size == 8 && port >= 0) {
        if (cmd == S_EXT)
            ports[port].hpd = get_unaligned_le32(payload + 4);
        else
            put_unaligned_le32(ports[port].hpd, payload + 4);
    } else if (id == HDCP_REPEATER && size == 12 && port >= 0) {
        if (cmd == S_EXT)
            memcpy(ports[port].repeater, payload, 12);
        else
            memcpy(payload + 4, (u8 *)ports[port].repeater + 4, 8);
    } else if (id == EXPERT_SETTING && size == 20 && port >= 0 &&
               get_unaligned_le32(payload + 4) < EXPERT_TYPES) {
        u32 *v = ports[port].expert[get_unaligned_le32(payload + 4)];

        if (cmd == S_EXT)
            memcpy(v, payload + 8, 12);
        else
            memcpy(payload + 8, v, 12);
    } else if (id == DISCONNECT && cmd == S_EXT) {
        /* nothing is connected to drop */
    } else {
        ret = -EOPNOTSUPP; /* HDCP keys, and what this model does not know */
    }
    mutex_unlock(&lock);
    if (ret)
        pr_info_ratelimited("%s control %#x (%u bytes): not emulated\n",
                            cmd == S_EXT ? "S_EXT" : "G_EXT", id, size);
    else if (cmd == G_EXT && copy_to_user(ptr, payload, size))
        ret = -EFAULT;
out:
    kfree(payload);
    return ret;
}

static long hdmi_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *ptr = (void __user *)arg;
    u32 values[2];

    switch (cmd) {
    case S_EXT:
    case G_EXT:
        return extended(cmd, ptr);
    case S_CTRL:
    case G_CTRL:
        if (copy_from_user(values, ptr, sizeof(values)))
            return -EFAULT;
        if (values[0] != POWER_OFF)
            return -EINVAL;
        mutex_lock(&lock);
        if (cmd == S_CTRL)
            power_off = values[1];
        else
            values[1] = power_off;
        mutex_unlock(&lock);
        return cmd == G_CTRL && copy_to_user(ptr, values, sizeof(values)) ? -EFAULT : 0;
    case S_INPUT:
    case G_INPUT:
        if (cmd == S_INPUT) {
            if (copy_from_user(values, ptr, 4))
                return -EFAULT;
            if (values[0] > PORTS + 1) /* V4L2_EXT_HDMI_INPUT_PORT_ALL */
                return -EINVAL;
        }
        mutex_lock(&lock);
        if (cmd == S_INPUT)
            input = values[0];
        else
            values[0] = input;
        mutex_unlock(&lock);
        return cmd == G_INPUT && copy_to_user(ptr, values, 4) ? -EFAULT : 0;
    default:
        return lgemu_ioctl_unhandled(&seen, "v4l2-hdmi", cmd, -ENOTTY);
    }
}

static const struct file_operations fops = {
    .owner = THIS_MODULE,
    .unlocked_ioctl = hdmi_ioctl,
    .compat_ioctl = hdmi_ioctl,
};

int lgemu_hdmi_init(void)
{
    struct device *dev;
    int err;

    if (!v4l2_hdmi)
        return 0;
    err = class_register(&hdmi_class);
    if (err)
        return err;
    class_added = true;
    err = lgemu_cdev_add(&cdev, 0, 0, 1, "v4l2-hdmi", &fops);
    if (err)
        goto fail;
    dev = device_create(&hdmi_class, NULL, cdev.dev, NULL, "video12");
    if (IS_ERR(dev)) {
        err = PTR_ERR(dev);
        lgemu_cdev_del(&cdev);
        goto fail;
    }
    return 0;
fail:
    class_unregister(&hdmi_class);
    class_added = false;
    return err;
}

void lgemu_hdmi_exit(void)
{
    if (cdev.added) {
        device_destroy(&hdmi_class, cdev.dev);
        lgemu_cdev_del(&cdev);
    }
    if (class_added) {
        class_unregister(&hdmi_class);
        class_added = false;
    }
}
