// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The board's eARC transmitter at /dev/earc0 (LG's v4l2-ext eARC, V4L2_EXT_DEV_PATH_EARC in LG's
 * videodev2-ext.h), and the eARC side of the sound bar the host may plug into the TV's ARC input.
 *
 * Its user is arccontroller: on the 43.x builds through nyxEarc.module (nyx::EarcImpl), before
 * that with its own calls (EarcDriver::v4l2*), both the same way (docs/research/
 * soundbar-cec-earc.md, "How arccontroller drives /dev/earc0"). It opens /dev/earc0 read-write
 * (then /dev/video13, not made here) and issues nothing but the 32-bit VIDIOC_S/G_EXT_CTRLS, class
 * 0x980000, one control through its pointer, always for port 0 (V4L2_EXT_EARC_INPUT_PORT_NONE:
 * the driver's eARC-capable port):
 *   0x98b900  V4L2_CID_EXT_EARC, {u32 port; u32 state}: set, then got back and compared (a
 *             different state is "eARC is not enabled"). The user's eARC Support setting: on, it
 *             enables the transmitter; off, it disables it and arccontroller lets ARC start over
 *             CEC three seconds later.
 *   0x98b901  V4L2_CID_EXT_EARC_CONNECTION_INFO, 268 bytes, {u32 port; u32 status;
 *             u8 capability[256]; u8 erx_latency_req; u8 erx_latency}, got every 50 ms. Status
 *             IDLE1 0, IDLE2 1, DISC1 2, DISC2 3, EARC 4: arccontroller's activation is "eARC" in
 *             4 only; a change into 1 is "ARC_Mode" (eARC discovery failed: it lets ARC start over
 *             CEC), out of 1 takes that back. The capability is the HDMI 2.1 eARC capabilities
 *             data structure (version 1, then block ID 1 with its length and the CTA-861 audio
 *             and speaker allocation data blocks), which it parses into earcSADs, earcSADB and
 *             earcATMOS; with none for 2 s it takes a default (PCM). erx_latency it applies as an
 *             audio delay.
 *   0x98b902  V4L2_CID_EXT_EARC_SET_ERX_LATENCY_REQ, set with the connection info's size: the
 *             request kept, read back in erx_latency_req (sent 0 on every entry into EARC).
 *   0x98b903  V4L2_CID_EXT_EARC_RESET_HDMI_HPD_BIT: accepted (nyxEarc has it, arccontroller never
 *             sends it; its HPD restart goes to the HDMI receiver).
 * As LG's header says: disabled, everything reads zero.
 *
 * The link has three states, with the transmitter enabled: nothing on the port, IDLE1; an eARC
 * bar, EARC with its capability and latency (discovery takes the bar's time, not modelled: the next
 * poll sees it); a bar without eARC (ARC only), IDLE2, discovery having failed, as HDMI 2.1 has a
 * transmitter fall back to ARC. Unplugged, it goes back to IDLE1, which is not ARC_Mode. The bar is
 * the host's (beanjar io soundbar attach and detach), set at run time, not kept across a restart.
 *
 *   earc           make /dev/earc0 (the boards whose firmware has eARC: the K8 family, O22's)
 *   earc_bar       the bar: "PORT,CAPABILITY" (the ARC input's number, the capability data in hex,
 *                  at most 256 bytes, version first), "PORT,arc" for an ARC-only bar, empty or
 *                  "off" for none
 *   earc_status    read only: "enabled=E status=S latency_req=R" (what arccontroller last set and
 *                  what it reads), or "absent" without the node */
#define pr_fmt(fmt) KBUILD_MODNAME ": earc: " fmt
#include <linux/compat.h>
#include <linux/fs.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/unaligned.h>
#include "lgemu.h"
#include "lgemu-drv.h"

#define S_EXT 0xc0185648
#define G_EXT 0xc0185647

static bool earc;
module_param(earc, bool, 0444);
MODULE_PARM_DESC(earc, "earc: the board's eARC transmitter (/dev/earc0)");

/* ---- the eARC link ---- */

#define EARC_BASE      0x0098b900
#define EARC_ENABLE    (EARC_BASE + 0)
#define EARC_CONN      (EARC_BASE + 1)
#define EARC_LATENCY   (EARC_BASE + 2)
#define EARC_RESET_HPD (EARC_BASE + 3)
#define EARC_CAP_BYTES 256
#define EARC_CONN_SIZE                                                                             \
    268 /* {port, status, capability[256], erx_latency_req, erx_latency}, padded */
#define EARC_LAT_REQ (8 + EARC_CAP_BYTES)
#define EARC_LAT     (EARC_LAT_REQ + 1)
#define EARC_BAR_LAT 0 /* the bar's audio latency in ms: none to add */

enum { IDLE1, IDLE2, DISC1, DISC2, EARC_MODE };

struct earc_link {
    u32 enabled;
    u8 latency_req;
    bool bar;     /* a bar on the ARC input */
    bool bar_arc; /* ...that has no eARC */
    u8 bar_port;
    u8 cap[EARC_CAP_BYTES];
    u32 cap_len;
};

static u32 earc_status_of(const struct earc_link *l)
{
    if (!l->enabled || !l->bar)
        return IDLE1;
    return l->bar_arc ? IDLE2 : EARC_MODE;
}

/* the connection info's 268 bytes for l, as arccontroller reads them */
static void earc_conn(const struct earc_link *l, u8 *out)
{
    u32 status = earc_status_of(l);

    memset(out, 0, EARC_CONN_SIZE);
    if (!l->enabled)
        return;
    put_unaligned_le32(l->bar ? l->bar_port : 0, out);
    put_unaligned_le32(status, out + 4);
    if (status == EARC_MODE) {
        memcpy(out + 8, l->cap, l->cap_len);
        out[EARC_LAT] = EARC_BAR_LAT;
    }
    out[EARC_LAT_REQ] = l->latency_req;
}

static int earc_hex(char c)
{
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}

/* parse the earc_bar parameter, "PORT,CAPABILITY", "PORT,arc", "" or "off" (a trailing newline
 * allowed), into l's bar fields; 0 or -EINVAL, l unchanged then */
static int earc_bar_parse(const char *val, struct earc_link *l)
{
    unsigned int len = strlen(val), port = 0, i = 0, n = 0;
    u8 cap[EARC_CAP_BYTES];

    if (len && val[len - 1] == '\n')
        len--;
    if (!len || (len == 3 && !memcmp(val, "off", 3))) {
        l->bar = false;
        return 0;
    }
    for (; i < len && val[i] >= '0' && val[i] <= '9' && i < 2; i++)
        port = port * 10 + val[i] - '0';
    if (!i || port < 1 || port > 15 || i >= len || val[i] != ',')
        return -EINVAL;
    i++;
    if (len - i == 3 && !memcmp(val + i, "arc", 3)) {
        l->bar = l->bar_arc = true;
        l->bar_port = port;
        l->cap_len = 0;
        return 0;
    }
    if ((len - i) % 2 || len - i < 2 || (len - i) / 2 > EARC_CAP_BYTES)
        return -EINVAL;
    for (; i < len; i += 2, n++) {
        int hi = earc_hex(val[i]), lo = earc_hex(val[i + 1]);

        if (hi < 0 || lo < 0)
            return -EINVAL;
        cap[n] = hi << 4 | lo;
    }
    if (cap[0] != 1) /* the capabilities data structure's version */
        return -EINVAL;
    l->bar = true;
    l->bar_arc = false;
    l->bar_port = port;
    memset(l->cap, 0, sizeof(l->cap));
    memcpy(l->cap, cap, n);
    l->cap_len = n;
    return 0;
}

/* the earc_bar parameter's text for l: "PORT,CAPABILITY\n", "PORT,arc\n" or "\n" */
static int earc_bar_format(const struct earc_link *l, char *buf)
{
    static const char digits[] = "0123456789abcdef";
    int n = 0;

    if (l->bar) {
        if (l->bar_port >= 10)
            buf[n++] = '1';
        buf[n++] = '0' + l->bar_port % 10;
        buf[n++] = ',';
        if (l->bar_arc) {
            memcpy(buf + n, "arc", 3);
            n += 3;
        }
        for (unsigned int b = 0; !l->bar_arc && b < l->cap_len; b++) {
            buf[n++] = digits[l->cap[b] >> 4];
            buf[n++] = digits[l->cap[b] & 0xF];
        }
    }
    buf[n++] = '\n';
    buf[n] = 0;
    return n;
}

/* a control of arccontroller's: id, S_EXT or not, its payload of size bytes (read in, written
 * back for a get); 0, or -EINVAL for what LG's driver would refuse */
static int earc_control(struct earc_link *l, u32 id, bool set, u8 *payload, u32 size)
{
    switch (id) {
    case EARC_ENABLE:
        if (size != 8)
            return -EINVAL;
        if (set) {
            u32 state = get_unaligned_le32(payload + 4);

            if (state > 1)
                return -EINVAL;
            if (!state)
                l->latency_req = 0;
            l->enabled = state;
        } else {
            put_unaligned_le32(l->enabled, payload + 4);
        }
        return 0;
    case EARC_CONN:
        if (size != EARC_CONN_SIZE || set)
            return -EINVAL;
        earc_conn(l, payload);
        return 0;
    case EARC_LATENCY:
        if (size != EARC_CONN_SIZE || !set)
            return -EINVAL;
        l->latency_req = payload[EARC_LAT_REQ];
        return 0;
    case EARC_RESET_HPD:
        return set ? 0 : -EINVAL;
    default:
        return -EINVAL;
    }
}

/* ---- end of the eARC link ---- */

static DEFINE_MUTEX(lock);
static struct earc_link link;
static struct lgemu_cdev cdev;
static bool class_added;
static char *earc_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return NULL;
}
static const struct class earc_class = {.name = "lg-earc", .devnode = earc_devnode};
static int seen_show(char *buf, size_t size, const struct lgemu_count *e)
{
    return scnprintf(buf, size, "%s %#x %lu\n", e->what, e->a, e->count);
}
DEFINE_LGEMU_COUNTS(seen, 8, 48, seen_show);

static long extended(unsigned int cmd, void __user *arg)
{
    u32 group[6], control[5], id, size;
    void __user *ptr;
    u8 *payload;
    u32 was;
    long ret;

    if (copy_from_user(group, arg, sizeof(group)))
        return -EFAULT;
    if (group[0] != 0x00980000 || group[1] != 1)
        return -EINVAL;
    if (copy_from_user(control, compat_ptr(group[5]), sizeof(control)))
        return -EFAULT;
    id = control[0];
    size = control[1];
    ptr = compat_ptr(control[3]);
    if (!size || size > EARC_CONN_SIZE)
        return -EINVAL;
    payload = kzalloc(size, GFP_KERNEL);
    if (!payload)
        return -ENOMEM;
    if (copy_from_user(payload, ptr, size)) {
        ret = -EFAULT;
        goto out;
    }
    mutex_lock(&lock);
    was = earc_status_of(&link) | link.enabled << 8;
    ret = earc_control(&link, id, cmd == S_EXT, payload, size);
    if (!ret && (earc_status_of(&link) | link.enabled << 8) != was)
        pr_info("%s, status %u\n", link.enabled ? "enabled" : "disabled", earc_status_of(&link));
    mutex_unlock(&lock);
    if (ret)
        pr_info_ratelimited("%s control %#x (%u bytes): refused\n",
                            cmd == S_EXT ? "S_EXT" : "G_EXT", id, size);
    else if (cmd == G_EXT && copy_to_user(ptr, payload, size))
        ret = -EFAULT;
out:
    kfree(payload);
    return ret;
}

static long earc_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    switch (cmd) {
    case S_EXT:
    case G_EXT:
        return extended(cmd, (void __user *)arg);
    default:
        return lgemu_ioctl_unhandled(&seen, "earc", cmd, -ENOTTY);
    }
}

static const struct file_operations fops = {
    .owner = THIS_MODULE,
    .unlocked_ioctl = earc_ioctl,
    .compat_ioctl = earc_ioctl,
};

static int earc_bar_set(const char *val, const struct kernel_param *kp)
{
    int err;

    if (!cdev.added)
        return -ENODEV;
    mutex_lock(&lock);
    err = earc_bar_parse(val, &link);
    if (!err && link.bar)
        pr_info("sound bar on HDMI %u: %s\n", link.bar_port, link.bar_arc ? "ARC only" : "eARC");
    else if (!err)
        pr_info("sound bar unplugged\n");
    mutex_unlock(&lock);
    return err;
}

static int earc_bar_get(char *buf, const struct kernel_param *kp)
{
    int n;

    mutex_lock(&lock);
    n = earc_bar_format(&link, buf);
    mutex_unlock(&lock);
    return n;
}

static const struct kernel_param_ops earc_bar_ops = {.set = earc_bar_set, .get = earc_bar_get};
module_param_cb(earc_bar, &earc_bar_ops, NULL, 0644);
MODULE_PARM_DESC(earc_bar, "earc: the sound bar on the eARC port, \"PORT,CAPABILITY\" (its ARC "
                           "input; its eARC capability data in hex) or \"PORT,arc\" (no eARC); "
                           "empty or off: none");

static int earc_status_get(char *buf, const struct kernel_param *kp)
{
    int n;

    if (!cdev.added)
        return sprintf(buf, "absent\n");
    mutex_lock(&lock);
    n = sprintf(buf, "enabled=%u status=%u latency_req=%u\n", link.enabled, earc_status_of(&link),
                link.latency_req);
    mutex_unlock(&lock);
    return n;
}

static const struct kernel_param_ops earc_status_ops = {.get = earc_status_get};
module_param_cb(earc_status, &earc_status_ops, NULL, 0444);
MODULE_PARM_DESC(earc_status, "earc: what arccontroller set and reads, or absent");

/* the HDMI input an eARC or ARC sound bar is plugged into, 0 for none (its cable's 5 V, for
 * v4l2-hdmi.c) */
int lgemu_earc_bar_port(void)
{
    int port;

    mutex_lock(&lock);
    port = link.bar ? link.bar_port : 0;
    mutex_unlock(&lock);
    return port;
}

int lgemu_earc_init(void)
{
    struct device *dev;
    int err;

    if (!earc)
        return 0;
    err = class_register(&earc_class);
    if (err)
        return err;
    class_added = true;
    err = lgemu_cdev_add(&cdev, 0, 0, 1, "earc", &fops);
    if (err)
        goto fail;
    dev = device_create(&earc_class, NULL, cdev.dev, NULL, "earc0");
    if (IS_ERR(dev)) {
        err = PTR_ERR(dev);
        lgemu_cdev_del(&cdev);
        goto fail;
    }
    return 0;
fail:
    class_unregister(&earc_class);
    class_added = false;
    return err;
}

void lgemu_earc_exit(void)
{
    if (cdev.added) {
        device_destroy(&earc_class, cdev.dev);
        lgemu_cdev_del(&cdev);
    }
    if (class_added) {
        class_unregister(&earc_class);
        class_added = false;
    }
}
