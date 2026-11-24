// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The board's HDMI CEC adapter, /dev/cec0, on the kernel's CEC framework (kernel patch 0015 shows
 * CEC_CORE's prompt), and the sound bar the host may put on its bus.
 *
 * LG's libcecp (eim's CEC, webOS 6 and later on every SoC) opens /dev/cec0 and drives it through
 * the framework's ioctls: CEC_S_MODE (initiator and follower), CEC_ADAP_S_PHYS_ADDR 0.0.0.0 (the
 * TV), CEC_ADAP_S_LOG_ADDRS, then CEC_TRANSMIT, and a thread that loops on CEC_RECEIVE with a
 * 10 s timeout. Without the node its open fails and that thread issues CEC_RECEIVE on the bad fd
 * in a loop with no pause, a whole CPU from boot. The framework is the ABI those calls
 * expect; the adapter below it is a bus with nobody else on it, unless a sound bar is attached:
 *   - a directed message (and a logical address poll) to anyone but the bar is not acknowledged,
 *     after its header block: CEC_TX_STATUS_NACK, so every address the TV polls for is free and
 *     it claims it;
 *   - a broadcast is not refused by anyone, which on CEC is its acknowledgement: CEC_TX_STATUS_OK
 *     after the whole frame;
 *   - a directed message to the bar (logical address 5, the audio system) is acknowledged, and
 *     the bar's answers are received after it, each after its own time on the bus.
 * The times are the bus's own (CEC 1.4 bit timing: a 4.5 ms start bit, 2.4 ms data bits, ten to
 * a block), after the signal free time the framework asks for. A transmit completes, and a frame
 * of the bar is received, from a work item: the framework calls adap_transmit with its lock held,
 * and cec_transmit_attempt_done and cec_received_msg take that lock. A pending work holds the
 * adapter's device (the adapter, and this board's state with it, is freed with its last
 * reference), and the module's own queue is drained at unload.
 *
 * The sound bar (docs/research/soundbar-cec-earc.md) is an HDMI audio system on the TV's ARC
 * input, physical address PORT.0.0.0, answering as a CEC 1.4 follower: physical address, vendor
 * id, power status, OSD name and CEC version; the ARC handshake (Request ARC Initiation and
 * Termination); System Audio Mode; the audio status and the volume keys; and Request Short Audio
 * Descriptor with the CTA-861 descriptors it was given, those of the formats the TV asks for.
 * Attached, it reports its physical address; detached, it leaves as a bar switched off does (System
 * Audio Mode off, Terminate ARC) and stops acknowledging a second later. It is the host's, set at
 * run time (beanjar io soundbar attach and detach), and not kept across a restart.
 *
 *   cec              register the adapter (O22's family; a SoC whose own module models its CEC
 *                    node, rtk.ko's cec0, leaves it off)
 *   soundbar         the sound bar: "PORT,SADS" (the ARC input's number and the descriptors,
 *                    three bytes each in hex, as eim's arcSADs), empty or "off" for none
 *   soundbar_status  read only: what the bar has agreed with the TV (ARC, System Audio Mode, the
 *                    volume), or "absent" */
#define pr_fmt(fmt) KBUILD_MODNAME ": cec: " fmt
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/workqueue.h>
#include <media/cec.h>
#include "lgemu.h"

#define START_US   4500
#define BIT_US     2400
#define BLOCK_BITS 10
#define FREE_BITS  5 /* a new initiator's signal free time */

static bool cec;
module_param(cec, bool, 0444);
MODULE_PARM_DESC(cec, "cec: register the board's HDMI CEC adapter (/dev/cec0)");

/* ---- the sound bar ---- */

#define BAR_LA        5        /* audio system */
#define BAR_TYPE      5        /* primary device type: audio system */
#define BAR_VENDOR    0x000C03 /* HDMI Licensing, LLC's, as the kernel's vivid test adapter */
#define BAR_VERSION   5        /* CEC 1.4 */
#define BAR_NAME      "Sound Bar"
#define BAR_MAX_SADS  12
#define BAR_REPLY_MAX 4 /* SADs in one Report Short Audio Descriptor */
#define BAR_VOLUME    25

#define OP_FEATURE_ABORT     0x00
#define OP_STANDBY           0x36
#define OP_USER_PRESSED      0x44
#define OP_GIVE_OSD_NAME     0x46
#define OP_SET_OSD_NAME      0x47
#define OP_SAM_REQUEST       0x70
#define OP_GIVE_AUDIO_STATUS 0x71
#define OP_SET_SAM           0x72
#define OP_REPORT_AUDIO      0x7A
#define OP_GIVE_SAM_STATUS   0x7D
#define OP_SAM_STATUS        0x7E
#define OP_GIVE_PHYS_ADDR    0x83
#define OP_REPORT_PHYS_ADDR  0x84
#define OP_DEVICE_VENDOR_ID  0x87
#define OP_GIVE_VENDOR_ID    0x8C
#define OP_GIVE_POWER_STATUS 0x8F
#define OP_REPORT_POWER      0x90
#define OP_CEC_VERSION       0x9E
#define OP_GET_CEC_VERSION   0x9F
#define OP_REPORT_SAD        0xA3
#define OP_REQUEST_SAD       0xA4
#define OP_INITIATE_ARC      0xC0
#define OP_REPORT_ARC_INIT   0xC1
#define OP_REPORT_ARC_TERM   0xC2
#define OP_REQUEST_ARC_INIT  0xC3
#define OP_REQUEST_ARC_TERM  0xC4
#define OP_TERMINATE_ARC     0xC5
#define OP_ABORT             0xFF

#define ABORT_UNRECOGNIZED 0
#define ABORT_INVALID      3
#define ABORT_REFUSED      4

#define UI_VOLUME_UP   0x41
#define UI_VOLUME_DOWN 0x42
#define UI_MUTE        0x43

struct bar {
    bool present;
    bool leaving; /* detached: its goodbye is on the bus, it stops acknowledging after it */
    u8 port;      /* the TV's ARC input, 1 to 15: physical address PORT.0.0.0 */
    u8 nsads;
    u8 sads[BAR_MAX_SADS][3];
    bool arc, sam, mute;
    u8 volume; /* 0 to 100 */
};

/* a frame the bar sends; len 0 is the end of its goodbye, not a frame */
struct bar_frame {
    u8 len;
    u8 msg[16];
};

static unsigned int bar_put(struct bar_frame *out, u8 to, const u8 *bytes, unsigned int n)
{
    out->len = n + 1;
    out->msg[0] = BAR_LA << 4 | to;
    memcpy(out->msg + 1, bytes, n);
    return 1;
}

static unsigned int bar_abort(struct bar_frame *out, u8 to, u8 opcode, u8 reason)
{
    u8 b[3] = {OP_FEATURE_ABORT, opcode, reason};

    return bar_put(out, to, b, 3);
}

static unsigned int bar_report_phys(const struct bar *bar, struct bar_frame *out)
{
    u8 b[4] = {OP_REPORT_PHYS_ADDR, bar->port << 4, 0, BAR_TYPE};

    return bar_put(out, 0xF, b, 4);
}

static unsigned int bar_audio_status(const struct bar *bar, struct bar_frame *out, u8 to)
{
    u8 b[2] = {OP_REPORT_AUDIO, (bar->mute ? 0x80 : 0) | bar->volume};

    return bar_put(out, to, b, 2);
}

/* whether a Request Short Audio Descriptor's operand (Audio Format ID in bits 7-6, code in 5-0)
 * names descriptor sad: ID 0 the descriptor's format code, ID 1 the extension type code of format
 * code 15 (CTA-861's audio coding extension) */
static bool bar_sad_matches(const u8 *sad, u8 operand)
{
    u8 code = sad[0] >> 3 & 0xF, id = operand >> 6, want = operand & 0x3F;

    if (code == 15)
        return id == 1 && want == sad[2] >> 3;
    return id == 0 && want == code;
}

/* whether the bar acknowledges msg (it is there and the message is directed to it) */
static bool bar_acks(const struct bar *bar, const u8 *msg, unsigned int len)
{
    return bar->present && len >= 1 && (msg[0] & 0xF) == BAR_LA;
}

/* the frames the bar sends for msg, which the TV sent and the bar received (directed to it, or a
 * broadcast); their number, at most 2 */
static unsigned int bar_answer(struct bar *bar, const u8 *msg, unsigned int len,
                               struct bar_frame *out)
{
    u8 from = msg[0] >> 4, to = msg[0] & 0xF, op;

    if (!bar->present || bar->leaving || len < 2)
        return 0; /* a poll, or nobody */
    op = msg[1];
    if (to != BAR_LA) {
        if (to == 0xF && op == OP_STANDBY) /* the TV went to standby: the bar follows */
            bar->sam = false;
        return 0;
    }
    if (from == 0xF) /* unregistered: no directed answer */
        return 0;
    switch (op) {
    case OP_GIVE_PHYS_ADDR:
        return bar_report_phys(bar, out);
    case OP_GIVE_VENDOR_ID: {
        u8 b[4] = {OP_DEVICE_VENDOR_ID, BAR_VENDOR >> 16, BAR_VENDOR >> 8 & 0xFF,
                   BAR_VENDOR & 0xFF};
        return bar_put(out, 0xF, b, 4);
    }
    case OP_GIVE_POWER_STATUS: {
        u8 b[2] = {OP_REPORT_POWER, 0}; /* on */
        return bar_put(out, from, b, 2);
    }
    case OP_GIVE_OSD_NAME: {
        u8 b[1 + sizeof(BAR_NAME) - 1] = {OP_SET_OSD_NAME};

        memcpy(b + 1, BAR_NAME, sizeof(BAR_NAME) - 1);
        return bar_put(out, from, b, sizeof(b));
    }
    case OP_GET_CEC_VERSION: {
        u8 b[2] = {OP_CEC_VERSION, BAR_VERSION};
        return bar_put(out, from, b, 2);
    }
    case OP_GIVE_SAM_STATUS: {
        u8 b[2] = {OP_SAM_STATUS, bar->sam};
        return bar_put(out, from, b, 2);
    }
    case OP_SAM_REQUEST: { /* with the active source's physical address: on; without: off */
        u8 b[2] = {OP_SET_SAM, len >= 4};

        bar->sam = len >= 4;
        return bar_put(out, 0xF, b, 2);
    }
    case OP_GIVE_AUDIO_STATUS:
        return bar_audio_status(bar, out, from);
    case OP_USER_PRESSED:
        if (len < 3)
            return bar_abort(out, from, op, ABORT_INVALID);
        if (msg[2] == UI_VOLUME_UP)
            bar->volume = bar->volume < 100 ? bar->volume + 1 : 100;
        else if (msg[2] == UI_VOLUME_DOWN)
            bar->volume = bar->volume ? bar->volume - 1 : 0;
        else if (msg[2] == UI_MUTE)
            bar->mute = !bar->mute;
        else
            return 0;
        return bar_audio_status(bar, out, from);
    case OP_REQUEST_SAD: {
        u8 b[1 + 3 * BAR_REPLY_MAX] = {OP_REPORT_SAD};
        unsigned int n = 0;

        if (len < 3 || len > 6)
            return bar_abort(out, from, op, ABORT_INVALID);
        for (unsigned int i = 2; i < len; i++)
            for (unsigned int s = 0; s < bar->nsads && n < BAR_REPLY_MAX; s++)
                if (bar_sad_matches(bar->sads[s], msg[i]))
                    memcpy(b + 1 + 3 * n++, bar->sads[s], 3);
        if (!n) /* none of the formats asked for */
            return bar_abort(out, from, op, ABORT_INVALID);
        return bar_put(out, from, b, 1 + 3 * n);
    }
    case OP_REQUEST_ARC_INIT: {
        u8 b[1] = {OP_INITIATE_ARC};

        if (from != 0) /* ARC runs to the TV only */
            return bar_abort(out, from, op, ABORT_REFUSED);
        bar->arc = true;
        return bar_put(out, from, b, 1);
    }
    case OP_REQUEST_ARC_TERM: {
        u8 b[1] = {OP_TERMINATE_ARC};

        if (from != 0)
            return bar_abort(out, from, op, ABORT_REFUSED);
        bar->arc = false;
        return bar_put(out, from, b, 1);
    }
    case OP_REPORT_ARC_INIT:
    case OP_REPORT_ARC_TERM:
    case OP_FEATURE_ABORT:
    case 0x45: /* User Control Released */
        return 0;
    case OP_ABORT:
        return bar_abort(out, from, op, ABORT_REFUSED);
    default:
        return bar_abort(out, from, op, ABORT_UNRECOGNIZED);
    }
}

/* the bar plugged in and on: it reports its physical address */
static unsigned int bar_hello(struct bar *bar, struct bar_frame *out)
{
    bar->arc = bar->sam = bar->mute = false;
    bar->volume = BAR_VOLUME;
    bar->leaving = false;
    return bar_report_phys(bar, out);
}

/* the bar switched off and unplugged: System Audio Mode off and Terminate ARC as it goes to
 * standby, then the end (a frame of length 0); at most 3 */
static unsigned int bar_goodbye(struct bar *bar, struct bar_frame *out)
{
    unsigned int n = 0;

    if (bar->sam) {
        u8 b[2] = {OP_SET_SAM, 0};
        n += bar_put(out + n, 0xF, b, 2);
    }
    if (bar->arc) {
        u8 b[1] = {OP_TERMINATE_ARC};
        n += bar_put(out + n, 0, b, 1);
    }
    bar->sam = bar->arc = false;
    bar->leaving = true;
    out[n++].len = 0;
    return n;
}

static int bar_hex(char c)
{
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}

/* parse the soundbar parameter, "PORT,SADS" or "" or "off" (a trailing newline allowed), into
 * cfg's present, port and descriptors; 0 or -EINVAL */
static int bar_parse(const char *val, struct bar *cfg)
{
    unsigned int len = strlen(val), port = 0, i = 0, n = 0;

    if (len && val[len - 1] == '\n')
        len--;
    if (!len || (len == 3 && !memcmp(val, "off", 3))) {
        cfg->present = false;
        return 0;
    }
    for (; i < len && val[i] >= '0' && val[i] <= '9' && i < 2; i++)
        port = port * 10 + val[i] - '0';
    if (!i || port < 1 || port > 15 || i >= len || val[i] != ',')
        return -EINVAL;
    i++;
    if ((len - i) % 6 || !(len - i) || (len - i) / 6 > BAR_MAX_SADS)
        return -EINVAL;
    for (; i < len; i += 2, n++) {
        int hi = bar_hex(val[i]), lo = bar_hex(val[i + 1]);

        if (hi < 0 || lo < 0)
            return -EINVAL;
        cfg->sads[n / 3][n % 3] = hi << 4 | lo;
    }
    for (unsigned int s = 0; s < n / 3; s++) /* format code 0 and byte 0's bit 7 are reserved */
        if (!(cfg->sads[s][0] >> 3 & 0xF) || (cfg->sads[s][0] & 0x80))
            return -EINVAL;
    cfg->present = true;
    cfg->port = port;
    cfg->nsads = n / 3;
    return 0;
}

/* the soundbar parameter's text for bar: "PORT,SADS\n", or "\n" with none */
static int bar_format(const struct bar *bar, char *buf)
{
    static const char digits[] = "0123456789abcdef";
    int n = 0;

    if (bar->present && !bar->leaving) {
        if (bar->port >= 10)
            buf[n++] = '1';
        buf[n++] = '0' + bar->port % 10;
        buf[n++] = ',';
        for (unsigned int s = 0; s < bar->nsads; s++)
            for (unsigned int b = 0; b < 3; b++) {
                buf[n++] = digits[bar->sads[s][b] >> 4];
                buf[n++] = digits[bar->sads[s][b] & 0xF];
            }
    }
    buf[n++] = '\n';
    buf[n] = 0;
    return n;
}

/* ---- end of the sound bar ---- */

#define RX_QUEUE 16

struct board_cec {
    struct cec_adapter *adap;
    struct delayed_work done;
    struct delayed_work rx;
    u8 status;
    bool closing;
};

static struct workqueue_struct *wq;

/* the bar and its frames on the way to the TV, under bar_lock (adap_transmit runs with the
 * framework's lock held: bar_lock is never held while calling into the framework) */
static DEFINE_MUTEX(bar_lock);
static struct bar bar;
static struct bar_frame rx_queue[RX_QUEUE];
static unsigned int rx_head, rx_count;
static struct board_cec *board;

static unsigned long frame_us(unsigned int len)
{
    return FREE_BITS * BIT_US + START_US + len * BLOCK_BITS * BIT_US;
}

static void rx_push(const struct bar_frame *f, unsigned int n)
{
    for (unsigned int i = 0; i < n; i++) {
        if (rx_count == RX_QUEUE) {
            pr_warn("the sound bar's frames overflow: one dropped\n");
            return;
        }
        rx_queue[(rx_head + rx_count++) % RX_QUEUE] = f[i];
    }
}

/* with bar_lock held: start receiving the bar's next frame, if any and not already on its way;
 * the end of a goodbye takes a second, the time a bar takes to be unplugged */
static void rx_kick(struct board_cec *b)
{
    unsigned long us;

    if (!b || b->closing || !rx_count || delayed_work_pending(&b->rx))
        return;
    us = rx_queue[rx_head].len ? frame_us(rx_queue[rx_head].len) : USEC_PER_SEC;
    get_device(&b->adap->devnode.dev);
    if (!queue_delayed_work(wq, &b->rx, usecs_to_jiffies(us)))
        put_device(&b->adap->devnode.dev);
}

static void rx_work(struct work_struct *work)
{
    struct board_cec *b = container_of(to_delayed_work(work), struct board_cec, rx);
    struct cec_adapter *adap = b->adap;
    struct bar_frame f = {};
    bool have = false;

    mutex_lock(&bar_lock);
    if (rx_count) {
        f = rx_queue[rx_head];
        rx_head = (rx_head + 1) % RX_QUEUE;
        rx_count--;
        have = true;
        if (!f.len && bar.leaving) {
            bar.present = bar.leaving = false;
            pr_info("sound bar unplugged\n");
        }
    }
    mutex_unlock(&bar_lock);
    if (have && f.len) {
        struct cec_msg msg = {.len = f.len};

        memcpy(msg.msg, f.msg, f.len);
        cec_received_msg(adap, &msg);
    }
    mutex_lock(&bar_lock);
    rx_kick(b); /* this work is no longer pending: the next one goes */
    mutex_unlock(&bar_lock);
    put_device(&adap->devnode.dev);
}

static void transmit_done(struct work_struct *work)
{
    struct board_cec *b = container_of(to_delayed_work(work), struct board_cec, done);
    struct cec_adapter *adap = b->adap;

    cec_transmit_attempt_done(adap, b->status); /* none pending after a disable: ignored */
    mutex_lock(&bar_lock);
    rx_kick(b); /* the bar answers after the frame */
    mutex_unlock(&bar_lock);
    put_device(&adap->devnode.dev);
}

/* called with the adapter's lock held: a work already running waits for it, and finishes after */
static int adap_enable(struct cec_adapter *adap, bool enable)
{
    struct board_cec *b = cec_get_drvdata(adap);

    if (!enable && cancel_delayed_work(&b->done))
        put_device(&adap->devnode.dev);
    return 0;
}

static int adap_log_addr(struct cec_adapter *adap, u8 logical_addr)
{
    return 0;
}

static int adap_transmit(struct cec_adapter *adap, u8 attempts, u32 signal_free_time,
                         struct cec_msg *msg)
{
    struct board_cec *b = cec_get_drvdata(adap);
    bool broadcast = cec_msg_is_broadcast(msg), acked;
    struct bar_frame out[2];
    unsigned int blocks, n = 0;
    unsigned long us;

    mutex_lock(&bar_lock);
    acked = bar_acks(&bar, msg->msg, msg->len);
    if (acked || broadcast)
        n = bar_answer(&bar, msg->msg, msg->len, out);
    rx_push(out, n);
    mutex_unlock(&bar_lock);
    blocks = broadcast || acked ? msg->len : 1;
    us = signal_free_time * BIT_US + START_US + blocks * BLOCK_BITS * BIT_US;
    b->status = broadcast || acked ? CEC_TX_STATUS_OK : CEC_TX_STATUS_NACK;
    get_device(&adap->devnode.dev);
    if (!queue_delayed_work(wq, &b->done, usecs_to_jiffies(us)))
        put_device(&adap->devnode.dev); /* one transmit at a time: not reached */
    return 0;
}

static void adap_free(struct cec_adapter *adap)
{
    kfree(cec_get_drvdata(adap));
}

static const struct cec_adap_ops ops = {
    .adap_enable = adap_enable,
    .adap_log_addr = adap_log_addr,
    .adap_transmit = adap_transmit,
    .adap_free = adap_free,
};

static int soundbar_set(const char *val, const struct kernel_param *kp)
{
    struct bar cfg = {};
    struct bar_frame out[3];
    unsigned int n = 0;
    int err = bar_parse(val, &cfg);

    if (err)
        return err;
    mutex_lock(&bar_lock);
    if (!board) {
        mutex_unlock(&bar_lock);
        return -ENODEV;
    }
    if (cfg.present) {
        bool was = bar.present && !bar.leaving;

        bar.port = cfg.port;
        bar.nsads = cfg.nsads;
        memcpy(bar.sads, cfg.sads, sizeof(cfg.sads));
        if (!was) { /* plugged in again before it was gone: its end finds it not leaving */
            bar.present = true;
            n = bar_hello(&bar, out);
            pr_info("sound bar plugged into HDMI %u, %u descriptors\n", bar.port, bar.nsads);
        }
    } else if (bar.present && !bar.leaving) {
        n = bar_goodbye(&bar, out);
        pr_info("sound bar switched off\n");
    }
    rx_push(out, n);
    rx_kick(board);
    mutex_unlock(&bar_lock);
    return 0;
}

static int soundbar_get(char *buf, const struct kernel_param *kp)
{
    int n;

    mutex_lock(&bar_lock);
    n = bar_format(&bar, buf);
    mutex_unlock(&bar_lock);
    return n;
}

static const struct kernel_param_ops soundbar_ops = {.set = soundbar_set, .get = soundbar_get};
module_param_cb(soundbar, &soundbar_ops, NULL, 0644);
MODULE_PARM_DESC(soundbar, "cec: the sound bar on the bus, \"PORT,SADS\" (its ARC input; its "
                           "short audio descriptors in hex, 3 bytes each); empty or off: none");

static int soundbar_status_get(char *buf, const struct kernel_param *kp)
{
    int n;

    mutex_lock(&bar_lock);
    if (bar.present && !bar.leaving)
        n = sprintf(buf, "arc=%d system_audio=%d volume=%u mute=%d\n", bar.arc, bar.sam, bar.volume,
                    bar.mute);
    else
        n = sprintf(buf, "absent\n");
    mutex_unlock(&bar_lock);
    return n;
}

static const struct kernel_param_ops soundbar_status_ops = {.get = soundbar_status_get};
module_param_cb(soundbar_status, &soundbar_status_ops, NULL, 0444);
MODULE_PARM_DESC(soundbar_status, "cec: what the sound bar agreed with the TV, or absent");

static int board_cec_probe(struct platform_device *pdev)
{
    struct board_cec *b = kzalloc(sizeof(*b), GFP_KERNEL);
    int err;

    if (!b)
        return -ENOMEM;
    INIT_DELAYED_WORK(&b->done, transmit_done);
    INIT_DELAYED_WORK(&b->rx, rx_work);
    b->adap = cec_allocate_adapter(&ops, b, "lgemu",
                                   CEC_CAP_PHYS_ADDR | CEC_CAP_LOG_ADDRS | CEC_CAP_TRANSMIT |
                                       CEC_CAP_PASSTHROUGH,
                                   CEC_MAX_LOG_ADDRS);
    if (IS_ERR(b->adap)) {
        err = PTR_ERR(b->adap);
        kfree(b);
        return err;
    }
    err = cec_register_adapter(b->adap, &pdev->dev);
    if (err) {
        cec_delete_adapter(b->adap); /* frees b (adap_free) */
        return err;
    }
    platform_set_drvdata(pdev, b);
    mutex_lock(&bar_lock);
    board = b;
    mutex_unlock(&bar_lock);
    return 0;
}

static void board_cec_remove(struct platform_device *pdev)
{
    struct board_cec *b = platform_get_drvdata(pdev);

    mutex_lock(&bar_lock);
    b->closing = true; /* no new receive work */
    board = NULL;
    mutex_unlock(&bar_lock);
    if (cancel_delayed_work_sync(&b->rx))
        put_device(&b->adap->devnode.dev);
    cec_unregister_adapter(b->adap); /* disables it; freed with its last reference */
}

static struct platform_driver driver = {
    .probe = board_cec_probe,
    .remove = board_cec_remove,
    .driver = {.name = "lgemu-cec"},
};

static struct platform_device *device;

int lgemu_cec_init(void)
{
    int err;

    if (!cec)
        return 0;
    wq = alloc_workqueue("lgemu-cec", 0, 1);
    if (!wq)
        return -ENOMEM;
    err = platform_driver_register(&driver);
    if (err)
        goto no_driver;
    device = platform_device_register_simple("lgemu-cec", PLATFORM_DEVID_NONE, NULL, 0);
    if (IS_ERR(device)) {
        err = PTR_ERR(device);
        goto no_device;
    }
    if (!platform_get_drvdata(device)) { /* probed synchronously: it failed */
        platform_device_unregister(device);
        err = -ENODEV;
        goto no_device;
    }
    return 0;

no_device:
    device = NULL;
    platform_driver_unregister(&driver);
no_driver:
    destroy_workqueue(wq);
    return err;
}

void lgemu_cec_exit(void)
{
    if (!device)
        return;
    platform_device_unregister(device);
    device = NULL;
    platform_driver_unregister(&driver);
    destroy_workqueue(wq); /* a work that was running finishes first */
}
