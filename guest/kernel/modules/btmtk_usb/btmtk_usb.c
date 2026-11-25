// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* btmtk_usb.ko: MediaTek's Bluetooth USB driver as the userland sees it, with no radio.
 *
 * The firmware's own btmtk_usb.ko is a module for the stock kernel this one cannot load.
 * webos-bluetooth-service's BluetoothManagerBtpd thread talks to it over a netlink family the
 * driver creates (btmtk_nl_init: netlink_kernel_create(&init_net, family, ...)), MediaTek's own
 * number, which differs per build (W18R 30, W19P and webOS 6 on 29) and so is the parameter
 * family: it opens socket(AF_NETLINK, SOCK_RAW, family), binds, and sends a registration whose
 * payload starts with "btmngr".
 *
 * controller=0 (webOS 4's m3 and k5lp, the chip table's): the driver is loaded, no adapter is.
 * The registration is never answered, so the service never powers Bluetooth on.
 *
 * controller=1 (webOS 6 on, loaded by the initramfs where the firmware's udev rules name this
 * driver for its MediaTek module): a minimal controller behind /dev/stpbt, as the stock stack
 * finds a plugged MT7668 (docs/research/magic-remote-pairing.md, "The controller"):
 *  - the registration is answered as the real driver does (btmtk_nl_recv_msg: the sender's port
 *    remembered, an NLMSG_DONE with {usb_state, fops_state}) with WORKING (4) and INIT (1), from
 *    which the service powers the adapter on (BluetoothManager::adapterPowerOn);
 *  - /dev/stpbt carries HCI H4: libbt-vendor_mtk.so opens it (O_RDWR|O_NONBLOCK) and hands the
 *    descriptor to the stack, which writes one packet per write() and reads the type, the header
 *    and the payload of each event apart. The vendor library sends nothing itself: the patch
 *    download and MediaTek's WMT commands are the kernel driver's on a real set, and there is no
 *    chip here to load them into;
 *  - the commands of the stack's start-up and of its adapter set-up are answered as a controller
 *    with BR/EDR and LE, no connections and nothing in range would answer them (controller.cc's
 *    reads, the writes as success); any other command gets Command Complete with status 0x01,
 *    Unknown HCI Command, and is logged once. ACL and SCO data are dropped: nothing is connected.
 * The adapter's address is the parameter bdaddr (aa:bb:cc:dd:ee:ff), which the guest's
 * emulator-mrcu-pairing sets from the VM's state before the service starts; until then it is
 * zero and the registration is not answered. Nothing leaves the VM: there is no radio. */
#include <linux/kfifo.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/netlink.h>
#include <linux/poll.h>
#include <linux/random.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <net/net_namespace.h>
#include <net/sock.h>

static int family;
module_param(family, int, 0444);
MODULE_PARM_DESC(
    family,
    "the netlink family the firmware's driver creates (after mainline's last, NETLINK_SMC 22)");

static bool controller;
module_param(controller, bool, 0444);
MODULE_PARM_DESC(controller, "a controller behind /dev/stpbt that answers the registration");

static DEFINE_MUTEX(lock);
static struct sock *nl;
static int nl_port = -1; /* the service's port, from its registration */
static u8 addr[6];       /* the adapter's address, as written: addr[0] the most significant */
static bool addr_set;

/* the real driver's states (the service's tables: usb_state 4 WORKING, fops_state 1 INIT) */
#define USB_WORKING 4
#define FOPS_INIT   1

static void nl_state(int usb, int fops)
{
    struct sk_buff *skb;
    struct nlmsghdr *nlh;
    s32 *state;

    if (nl_port < 0)
        return;
    skb = nlmsg_new(8, GFP_KERNEL);
    if (!skb)
        return;
    nlh = nlmsg_put(skb, 0, 0, NLMSG_DONE, 8, 0);
    if (!nlh) {
        kfree_skb(skb);
        return;
    }
    state = nlmsg_data(nlh);
    state[0] = usb;
    state[1] = fops;
    netlink_unicast(nl, skb, nl_port, MSG_DONTWAIT);
}

static void btmtk_nl_input(struct sk_buff *skb)
{
    struct nlmsghdr *nlh = nlmsg_hdr(skb);

    if (!controller || skb->len < NLMSG_HDRLEN + 6 || memcmp(nlmsg_data(nlh), "btmngr", 6))
        return;
    mutex_lock(&lock);
    nl_port = nlh->nlmsg_pid;
    if (addr_set)
        nl_state(USB_WORKING, FOPS_INIT);
    mutex_unlock(&lock);
    pr_info("btmtk_usb: btmngr registered (port %d)%s\n", nlh->nlmsg_pid,
            addr_set ? "" : ", no address yet");
}

static int bdaddr_set(const char *val, const struct kernel_param *kp)
{
    u8 a[6];

    if (sscanf(val, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx", &a[0], &a[1], &a[2], &a[3], &a[4], &a[5]) !=
            6 ||
        (a[0] & 1))
        return -EINVAL;
    mutex_lock(&lock);
    memcpy(addr, a, 6);
    if (!addr_set) {
        addr_set = true;
        nl_state(USB_WORKING, FOPS_INIT); /* a registration that came first */
    }
    mutex_unlock(&lock);
    return 0;
}

static int bdaddr_get(char *buf, const struct kernel_param *kp)
{
    return sprintf(buf, "%pM\n", addr);
}

static const struct kernel_param_ops bdaddr_ops = {.set = bdaddr_set, .get = bdaddr_get};
module_param_cb(bdaddr, &bdaddr_ops, NULL, 0644);
MODULE_PARM_DESC(bdaddr, "the adapter's address, aa:bb:cc:dd:ee:ff (unicast)");

/* --- the controller --- */

static DEFINE_KFIFO(events, u8, 8192);
static DECLARE_WAIT_QUEUE_HEAD(readers);
static DEFINE_SPINLOCK(events_lock);
static DECLARE_BITMAP(unknown_seen, 0x10000);
static u8
    host_features; /* LMP page 1 byte 0, as the host wrote it: SSP 0x01, LE 0x02, simul 0x04 */
static u8 iac[1 + 4 * 3] = {1, 0x33, 0x8b, 0x9e}; /* Num_Current_IAC and the LAPs: GIAC at reset */

/* Command Complete: 04 0E len 01 opcode status params */
static void cmd_complete(u16 opcode, u8 status, const void *params, size_t len)
{
    u8 head[7] = {0x04, 0x0e, (u8)(4 + len), 1, opcode & 0xff, opcode >> 8, status};
    unsigned long flags;

    spin_lock_irqsave(&events_lock, flags);
    if (kfifo_avail(&events) >= sizeof(head) + len) {
        kfifo_in(&events, head, sizeof(head));
        if (len)
            kfifo_in(&events, (const u8 *)params, len);
    } else {
        pr_warn_ratelimited("btmtk_usb: event queue full, %04x dropped\n", opcode);
    }
    spin_unlock_irqrestore(&events_lock, flags);
    wake_up_interruptible(&readers);
}

static void command(u16 opcode, const u8 *p, u8 plen)
{
    u8 r[248];

    memset(r, 0, sizeof(r));
    switch (opcode) {
    /* the writes of the start-up and of the adapter's set-up: done, with nothing to do them on */
    case 0x0c03: /* Reset */
        host_features = 0;
        iac[0] = 1;
        iac[1] = 0x33;
        iac[2] = 0x8b;
        iac[3] = 0x9e;
        fallthrough;
    case 0x0c01: /* Set Event Mask */
    case 0x0c13: /* Write Local Name */
    case 0x0c16: /* Write Connection Accept Timeout */
    case 0x0c18: /* Write Page Timeout */
    case 0x0c1a: /* Write Scan Enable */
    case 0x0c1c: /* Write Page Scan Activity */
    case 0x0c1e: /* Write Inquiry Scan Activity */
    case 0x0c24: /* Write Class of Device */
    case 0x0c33: /* Host Buffer Size */
    case 0x0c45: /* Write Inquiry Mode */
    case 0x0c46: /* Write Page Scan Type */
    case 0x0c47: /* Write Inquiry Scan Type */
    case 0x0c52: /* Write Extended Inquiry Response */
    case 0x080f: /* Write Default Link Policy Settings */
    case 0x2001: /* LE Set Event Mask */
    case 0x2005: /* LE Set Random Address */
    case 0x200b: /* LE Set Scan Parameters */
    case 0x200c: /* LE Set Scan Enable: nothing advertises */
    case 0x2010: /* LE Clear White List */
    case 0x2011: /* LE Add Device To White List */
    case 0x2012: /* LE Remove Device From White List */
        cmd_complete(opcode, 0, NULL, 0);
        return;
    case 0x0c56: /* Write Simple Pairing Mode */
        if (plen >= 1 && p[0])
            host_features |= 0x01;
        cmd_complete(opcode, 0, NULL, 0);
        return;
    case 0x0c6d: /* LE Write Host Support */
        if (plen >= 1 && p[0])
            host_features |= 0x02;
        if (plen >= 2 && p[1])
            host_features |= 0x04;
        cmd_complete(opcode, 0, NULL, 0);
        return;
    case 0x0c39: /* Read Current IAC LAP */
        cmd_complete(opcode, 0, iac, 1 + iac[0] * 3);
        return;
    case 0x0c3a: /* Write Current IAC LAP: the LAPs kept (up to 4, as this controller supports) */
        if (plen < 1 || p[0] < 1 || p[0] > 4 || plen < 1 + p[0] * 3) {
            cmd_complete(opcode, 0x12, NULL, 0); /* Invalid HCI Command Parameters */
            return;
        }
        memcpy(iac, p, 1 + p[0] * 3);
        cmd_complete(opcode, 0, NULL, 0);
        return;
    case 0x0c14: /* Read Local Name: the chip's own, empty until the host writes one */
        cmd_complete(opcode, 0, r, 248);
        return;
    case 0x1001: { /* Read Local Version Information: 5.0, MediaTek (0x0046) */
        static const u8 v[] = {0x09, 0x00, 0x00, 0x09, 0x46, 0x00, 0x00, 0x00};
        cmd_complete(opcode, 0, v, sizeof(v));
        return;
    }
    case 0x1002: /* Read Local Supported Commands: Set Event Mask and Reset (octet 5), and Read
                  * Encryption Key Size (octet 20 bit 4), which Fluoride's start-up requires; no
                  * bit that makes the stack send an optional command */
        r[5] = 0xc0;
        r[20] = 0x10;
        cmd_complete(opcode, 0, r, 64);
        return;
    case 0x1004: /* Read Local Extended Features */
        r[0] = plen >= 1 ? p[0] : 0;
        r[1] = 1; /* the last page */
        if (r[0] == 0) {
            r[2] = 0x07; /* 3- and 5-slot packets, encryption */
            r[6] = 0x40; /* LE Supported (Controller) */
            r[8] = 0x0a; /* Secure Simple Pairing, simultaneous LE and BR/EDR (Controller) */
            r[9] = 0x80; /* extended features */
        } else if (r[0] == 1) {
            r[2] = host_features;
        } else {
            cmd_complete(opcode, 0x12, r, 2); /* Invalid HCI Command Parameters */
            return;
        }
        cmd_complete(opcode, 0, r, 10);
        return;
    case 0x1005: { /* Read Buffer Size: ACL 1021 x 8, SCO 64 x 1 */
        static const u8 b[] = {0xfd, 0x03, 0x40, 0x08, 0x00, 0x01, 0x00};
        cmd_complete(opcode, 0, b, sizeof(b));
        return;
    }
    case 0x1009: { /* Read BD_ADDR, least significant octet first */
        u8 a[6];
        int i;

        for (i = 0; i < 6; i++)
            a[i] = addr[5 - i];
        cmd_complete(opcode, 0, a, sizeof(a));
        return;
    }
    case 0x2002: { /* LE Read Buffer Size: 251 x 8 */
        static const u8 b[] = {0xfb, 0x00, 0x08};
        cmd_complete(opcode, 0, b, sizeof(b));
        return;
    }
    case 0x2003: /* LE Read Local Supported Features: LE Encryption only */
        r[0] = 0x01;
        cmd_complete(opcode, 0, r, 8);
        return;
    case 0x200f: /* LE Read White List Size */
        r[0] = 8;
        cmd_complete(opcode, 0, r, 1);
        return;
    case 0x2018: /* LE Rand */
        get_random_bytes(r, 8);
        cmd_complete(opcode, 0, r, 8);
        return;
    case 0x201c: { /* LE Read Supported States */
        static const u8 s[] = {0xff, 0xff, 0xff, 0xff, 0xff, 0x03, 0x00, 0x00};
        cmd_complete(opcode, 0, s, sizeof(s));
        return;
    }
    }
    if (!test_and_set_bit(opcode, unknown_seen))
        pr_info("btmtk_usb: HCI command %04x (%u bytes): unknown command\n", opcode, plen);
    cmd_complete(opcode, 0x01, NULL, 0);
}

static int stpbt_open(struct inode *inode, struct file *file)
{
    unsigned long flags;

    spin_lock_irqsave(&events_lock, flags);
    kfifo_reset(&events);
    spin_unlock_irqrestore(&events_lock, flags);
    return nonseekable_open(inode, file);
}

static ssize_t stpbt_read(struct file *file, char __user *buf, size_t count, loff_t *pos)
{
    u8 tmp[256];
    unsigned long flags;
    unsigned int n;
    int ret;

    for (;;) {
        spin_lock_irqsave(&events_lock, flags);
        n = kfifo_out(&events, tmp, min(count, sizeof(tmp)));
        spin_unlock_irqrestore(&events_lock, flags);
        if (n)
            return copy_to_user(buf, tmp, n) ? -EFAULT : n;
        if (file->f_flags & O_NONBLOCK)
            return -EAGAIN;
        ret = wait_event_interruptible(readers, !kfifo_is_empty(&events));
        if (ret)
            return ret;
    }
}

static ssize_t stpbt_write(struct file *file, const char __user *buf, size_t count, loff_t *pos)
{
    u8 pkt[3 + 255 + 1];

    if (count < 1 || count > sizeof(pkt))
        return count; /* ACL data larger than a command: nothing is connected, dropped */
    if (copy_from_user(pkt, buf, count))
        return -EFAULT;
    if (pkt[0] != 0x01)
        return count; /* ACL, SCO, ISO data: dropped */
    if (count < 4 || count < 4 + pkt[3])
        return -EINVAL; /* the stack writes a whole command at once */
    mutex_lock(&lock);
    command(pkt[1] | pkt[2] << 8, pkt + 4, pkt[3]);
    mutex_unlock(&lock);
    return count;
}

static __poll_t stpbt_poll(struct file *file, poll_table *wait)
{
    poll_wait(file, &readers, wait);
    return EPOLLOUT | EPOLLWRNORM | (kfifo_is_empty(&events) ? 0 : EPOLLIN | EPOLLRDNORM);
}

static long stpbt_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    switch (cmd) {
    case 0xff: /* the USB state, as the real driver answers it */
        return USB_WORKING;
    case 0xc008b000:
        return 0;
    }
    return -EFAULT;
}

static const struct file_operations stpbt_fops = {
    .owner = THIS_MODULE,
    .open = stpbt_open,
    .read = stpbt_read,
    .write = stpbt_write,
    .poll = stpbt_poll,
    .unlocked_ioctl = stpbt_ioctl,
    .compat_ioctl = stpbt_ioctl,
};

static struct miscdevice stpbt = {
    .minor = MISC_DYNAMIC_MINOR,
    .name = "stpbt",
    .fops = &stpbt_fops,
    .mode = 0660,
};

static int __init btmtk_init(void)
{
    struct netlink_kernel_cfg cfg = {.input = btmtk_nl_input};
    int ret;

    if (family <= NETLINK_SMC || family >= MAX_LINKS) {
        pr_err("btmtk_usb: family=%d is not a vendor netlink family\n", family);
        return -EINVAL;
    }
    nl = netlink_kernel_create(&init_net, family, &cfg);
    if (!nl)
        return -ENOMEM;
    if (controller) {
        ret = misc_register(&stpbt);
        if (ret) {
            netlink_kernel_release(nl);
            return ret;
        }
    }
    pr_info("btmtk_usb: netlink family %d, %s\n", family,
            controller ? "a controller at /dev/stpbt" : "no adapter");
    return 0;
}

static void __exit btmtk_exit(void)
{
    if (controller)
        misc_deregister(&stpbt);
    netlink_kernel_release(nl);
}

module_init(btmtk_init);
module_exit(btmtk_exit);
MODULE_LICENSE("GPL");
MODULE_VERSION("emulator");
MODULE_DESCRIPTION(
    "MediaTek Bluetooth USB driver's netlink family, and a controller with no radio (beanjar)");
