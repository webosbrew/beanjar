// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* /dev/video20: LG's video decoder (v4l2-ext VDEC, the Starfish SDK's videodev2-ext.h and
 * v4l2-controls-ext.h), as legacy-broadcast drives it for live TV. The decoder's input is the
 * demux's video decoder filter (DMX_OUT_DECODER, DMX_PES_VIDEO0), which main.c hands here
 * packet by packet; the pictures are the host's: the packets go on to the host's media decoder
 * through /dev/dtv-soc-av and the initramfs's pump, as the VDEC channel's stream, which the viewer
 * shows in the scaler window connected to that VDEC port.
 *
 * What the service asks of it (W21P, W23P; static and live traces):
 * - open, S_CTRL V4L2_CID_EXT_VDEC_CHANNEL (the port, 0 to 3), SUBSCRIBE_EVENT for
 *   V4L2_EVENT_PRIVATE_EXT_VDEC_EVENT ids FRAME (0x8001) and PICINFO (0x8002), and USERDATA
 *   (0x8003) for captions; a failed subscription closes the decoder. Then S_INPUT, S_OUTPUT, the
 *   S_CTRLs of the stream's mode (display delay, PVR, fast I-frame ...), S_FMT with the codec's
 *   fourcc on the output queue and STREAMON. No buffers: the stream comes from the demux.
 * - its event thread polls for POLLPRI and DQEVENTs. A FRAME event's data is the picture's type
 *   (0 the sequence header, 1 to 3 I, P, B), its 40-bit PTS (bytes 1 to 5) and a hash (6 to 9);
 *   a PICINFO event has no data: the service reads V4L2_CID_EXT_VDEC_PICINFO_EVENT_DATA_EXT with
 *   G_EXT_CTRLS (116 bytes, struct v4l2_ext_picinfo_msg_ext). Video is "good", and the service
 *   lifts its mute, after a FRAME event of a sequence header followed by a PICINFO whose timing
 *   differs from nothing (a PICINFO before any sequence header is dropped).
 * - DECODER_STATUS (24 bytes) now and then, for logs and the stop check.
 * - captions (W23P 33.31.61 and 43.22.76, static reading): a USERDATA event per picture with user
 *   data, in display order. Its data is the user data's length (u32, bytes 0 to 3), the picture's
 *   type (byte 4), repeat_first_field (5), top_field_first (6) and temporal_reference's low byte
 *   (7); the service then reads that many bytes with G_EXT_CTRLS V4L2_CID_EXT_VDEC_USER_EVENT_DATA:
 *   the user_data() after its start code ('GA94', type 3, cc_data for A/53 captions), at most 128
 *   bytes. Its caption task takes 608 or 708 from the cc_data itself.
 * So the decoder reads the stream's MPEG-2 headers as the SoC's decoder does: a sequence header
 * gives the FRAME event of type 0 and, the first time and whenever the picture's format changes,
 * the PICINFO; each picture header a FRAME event of its type; a picture's user data, put back in
 * display order (a B picture's at once, an I or P picture's at the next I or P), a USERDATA. Only
 * MPEG-2 is read; another codec is passed on to the host without events. The 32-bit userland's
 * structures, as the compat ioctls carry them. */
#define pr_fmt(fmt) KBUILD_MODNAME ": vdec: " fmt
#include <linux/cdev.h>
#include <linux/compat.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/module.h>
#include <linux/poll.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/timekeeping.h>
#include <linux/uaccess.h>
#include <linux/unaligned.h>
#include "../../../../include/webos-media.h"
#include "../../../../include/webos-tuner.h"
#include "dtv_soc.h"

#define QUERYCAP    0x80685600
#define G_FMT       0xc0cc5604
#define S_FMT       0xc0cc5605
#define STREAMON    0x40045612
#define STREAMOFF   0x40045613
#define G_CTRL      0xc008561b
#define S_CTRL      0xc008561c
#define G_INPUT     0x80045626
#define S_INPUT     0xc0045627
#define G_OUTPUT    0x8004562e
#define S_OUTPUT    0xc004562f
#define G_EXT       0xc0185647
#define S_EXT       0xc0185648
#define DQEVENT     0x80805659
#define SUBSCRIBE   0x4020565a
#define UNSUBSCRIBE 0x4020565b

#define CID_CHANNEL        0x00988901
#define CID_USER_EVENT     0x00988965
#define CID_DECODER_STATUS 0x00988967
#define CID_PICINFO_EXT    0x0098896b
#define EVENT_TYPE         0x08008001 /* V4L2_EVENT_PRIVATE_EXT_VDEC_EVENT */
#define EV_FRAME           0x8001
#define EV_PICINFO         0x8002
#define EV_USERDATA        0x8003
#define PICINFO_SIZE       116
#define STATUS_SIZE        24
#define MPG2               0x3247504d /* 'MPG2' */
#define CHANNELS           4
#define EVENTS             32
#define CTRLS              16
#define CARRY              15           /* a start code and the longest header read, less a byte */
#define UD_MAX             128          /* a picture's user data, as the service keeps it */
#define UD_BUF             (UD_MAX + 8) /* and the next start code's bytes that end it */

struct picinfo {
    u32 h, v, aspect, bitrate, num, den;
    bool progressive;
};

/* a picture's user data and what the USERDATA event says of the picture */
struct userdata {
    u8 type, rff, tff, tref;
    u32 pts;
    unsigned int len;
    u8 data[UD_BUF];
};

struct vdec_file {
    struct list_head link;
    wait_queue_head_t wait;
    u32 channel, input, output, fourcc;
    bool streaming;
    u32 subs; /* bit n: event id 0x8001 + n */
    u8 events[EVENTS][128];
    unsigned int head, count, sequence;
    u32 ctrl_id[CTRLS], ctrl_val[CTRLS];
    /* the stream's headers (packet context) */
    u8 carry[CARRY];
    unsigned int carried;
    u64 pts; /* the current PES's, or U64_MAX */
    bool pts_used;
    bool seq_seen, have_info;
    struct picinfo info, sent;
    u32 pictures, sequences, errors;
    /* user data: the current picture's, an I or P picture's held for display order, and those
     * sent in USERDATA events, read in order with G_EXT_CTRLS */
    struct userdata cur, held;
    bool in_picture, collecting, holding;
    struct userdata sent_ud[EVENTS];
    unsigned int ud_head, ud_count;
    u32 userdata;
};

static DEFINE_SPINLOCK(lock); /* the files, their state and events; taken in packet context */
static LIST_HEAD(files);
static struct vdec_file *active; /* the one streaming, which the video filter feeds */
static unsigned long packets, unread;
static dev_t devt;
static struct cdev cdev;
static bool cdev_added;
static struct class *vdec_class;

static char *vdec_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return NULL;
}

/* lock held */
static void event(struct vdec_file *f, u32 id, const u8 *data, size_t size)
{
    struct timespec64 now;
    u8 *e;

    if (!(f->subs & BIT(id - EV_FRAME)))
        return;
    if (f->count == EVENTS) { /* the oldest goes, and its user data if it had some */
        if (get_unaligned_le32(f->events[f->head] + 88) == EV_USERDATA && f->ud_count) {
            f->ud_head = (f->ud_head + 1) % EVENTS;
            f->ud_count--;
        }
        f->head = (f->head + 1) % EVENTS;
        f->count--;
    }
    ktime_get_ts64(&now);
    e = f->events[(f->head + f->count++) % EVENTS];
    memset(e, 0, 128);
    put_unaligned_le32(EVENT_TYPE, e);
    memcpy(e + 8, data, size);
    put_unaligned_le32(f->sequence++, e + 76);
    put_unaligned_le32(now.tv_sec, e + 80);
    put_unaligned_le32(now.tv_nsec, e + 84);
    put_unaligned_le32(id, e + 88);
    wake_up_interruptible(&f->wait);
}

static void frame_event(struct vdec_file *f, u8 type)
{
    u8 d[10] = {type};
    u64 pts = f->pts == U64_MAX || f->pts_used ? 0 : f->pts;

    /* a PES's PTS belongs to its first picture */
    if (type)
        f->pts_used = true;
    for (int i = 0; i < 5; i++)
        d[1 + i] = pts >> (8 * i);
    event(f, EV_FRAME, d, sizeof(d));
}

/* a picture's user data out: its USERDATA event, the bytes kept for the service's read */
static void userdata_event(struct vdec_file *f, const struct userdata *u)
{
    u8 d[12];

    if (!u->len || !(f->subs & BIT(EV_USERDATA - EV_FRAME)))
        return;
    if (f->ud_count == EVENTS) { /* cannot be: each has its event, which go first */
        f->ud_head = (f->ud_head + 1) % EVENTS;
        f->ud_count--;
    }
    f->sent_ud[(f->ud_head + f->ud_count++) % EVENTS] = *u;
    put_unaligned_le32(u->len, d);
    d[4] = u->type;
    d[5] = u->rff;
    d[6] = u->tff;
    d[7] = u->tref;
    put_unaligned_le32(u->pts, d + 8);
    f->userdata++;
    event(f, EV_USERDATA, d, sizeof(d));
}

/* ---- the stream's headers ----
 * MPEG-2's sequence header, sequence extension, picture headers, picture coding extensions and the
 * pictures' user data, as the start codes come in the video packets' payload (a start code may be
 * split over two packets, user data over several). */

/* the user data being read ends: at the first start code after it, whose bytes it may hold */
static void userdata_end(struct vdec_file *f)
{
    struct userdata *u = &f->cur;

    if (!f->collecting)
        return;
    f->collecting = false;
    for (unsigned int i = 0; i + 2 < u->len; i++)
        if (!u->data[i] && !u->data[i + 1] && u->data[i + 2] == 1) {
            u->len = i;
            break;
        }
    u->len = min(u->len, (unsigned int)UD_MAX);
}

static void userdata_add(struct vdec_file *f, const u8 *p, unsigned int n)
{
    struct userdata *u = &f->cur;

    n = min(n, (unsigned int)UD_BUF - u->len);
    memcpy(u->data + u->len, p, n);
    u->len += n;
}

/* the current picture ends (the next one starts): its user data in display order */
static void picture_end(struct vdec_file *f)
{
    userdata_end(f);
    if (!f->in_picture)
        return;
    f->in_picture = false;
    if (f->cur.type == 3) { /* B: shown before the I or P picture held */
        userdata_event(f, &f->cur);
        return;
    }
    if (f->holding)
        userdata_event(f, &f->held);
    f->held = f->cur;
    f->holding = true;
}

static const u32 rates[9][2] = {{0, 1},  {24000, 1001}, {24, 1},       {25, 1}, {30000, 1001},
                                {30, 1}, {50, 1},       {60000, 1001}, {60, 1}};

/* a start code's byte b and the bytes after it in the packet's rest (n of them) */
static void start_code(struct vdec_file *f, u8 b, const u8 *p, unsigned int n)
{
    userdata_end(f); /* any start code ends user data */
    if (b == 0xb5 && n >= 4 && p[0] >> 4 == 8 && f->in_picture) { /* picture coding extension */
        f->cur.tff = p[3] >> 7;
        f->cur.rff = p[3] >> 1 & 1;
    }
    if (b == 0xb3 || b == 0xb8) /* a sequence or GOP header: after the picture */
        picture_end(f);
    if (b == 0xb3 && n >= 7) { /* sequence header */
        struct picinfo i = f->info;
        unsigned int rate;

        i.h = p[0] << 4 | p[1] >> 4;
        i.v = (p[1] & 0xf) << 8 | p[2];
        i.aspect = p[3] >> 4;
        rate = p[3] & 0xf;
        if (rate >= ARRAY_SIZE(rates))
            rate = 0;
        i.num = rates[rate][0];
        i.den = rates[rate][1];
        i.bitrate = (p[4] << 10 | p[5] << 2 | p[6] >> 6) * 400 / 1000; /* kbit/s */
        if (!f->seq_seen)
            i.progressive = true; /* MPEG-1, until a sequence extension says */
        f->info = i;
        f->seq_seen = true;
        f->sequences++;
        frame_event(f, 0);
    } else if (b == 0xb5 && n >= 2 && p[0] >> 4 == 1) { /* sequence extension */
        f->info.progressive = p[1] >> 3 & 1;
    } else if (b == 0x00 && n >= 2 && f->seq_seen) { /* picture header */
        u8 type = p[1] >> 3 & 7;

        picture_end(f);
        memset(&f->cur, 0, offsetof(struct userdata, data));
        f->cur.type = type;
        f->cur.tref = p[0] << 2 | p[1] >> 6; /* temporal_reference's low byte */
        f->cur.pts = f->pts == U64_MAX || f->pts_used ? 0 : (u32)f->pts;
        f->in_picture = true;

        /* the sequence's picture info, once complete (its extension read) */
        if (!f->have_info || memcmp(&f->info, &f->sent, sizeof(f->info))) {
            f->sent = f->info;
            f->have_info = true;
            event(f, EV_PICINFO, NULL, 0);
        }
        if (type >= 1 && type <= 3) {
            f->pictures++;
            frame_event(f, type);
        } else {
            f->errors++;
        }
    }
}

/* the bytes a header needs after its start code */
static unsigned int header_size(u8 b)
{
    return b == 0xb3 ? 7 : b == 0xb5 ? 4 : b == 0x00 ? 2 : 0;
}

/* user data starts (its start code read): its bytes are the rest of the packet and what follows,
 * up to the next start code */
static void userdata_start(struct vdec_file *f, const u8 *p, unsigned int n)
{
    userdata_end(f);
    if (!f->in_picture || f->cur.len)
        return; /* the sequence's or the GOP's, or a picture's second: not the captions' */
    f->collecting = true;
    userdata_add(f, p, n);
}

/* the payload of a video packet: its start codes, across packets. A header that does not end in
 * the packet is read from the CARRY bytes kept of its end and the next packet's first ones. */
static void scan(struct vdec_file *f, const u8 *p, unsigned int n)
{
    u8 head[2 * CARRY];
    unsigned int k = f->carried, m = k + min(n, (unsigned int)CARRY), keep;

    if (f->collecting)
        userdata_add(f, p, n);
    memcpy(head, f->carry, k);
    memcpy(head + k, p, m - k);
    /* those that begin in the carried bytes and did not end there */
    for (unsigned int i = 0; i < k && i + 3 < m; i++)
        if (!head[i] && !head[i + 1] && head[i + 2] == 1 && i + 4 + header_size(head[i + 3]) > k &&
            i + 4 + header_size(head[i + 3]) <= m) {
            if (head[i + 3] == 0xb2)
                userdata_start(f, p + (i + 4 - k), n - (i + 4 - k));
            else
                start_code(f, head[i + 3], head + i + 4, m - i - 4);
        }
    /* those that end in this one */
    for (unsigned int i = 0; i + 3 < n; i++)
        if (!p[i] && !p[i + 1] && p[i + 2] == 1) {
            if (p[i + 3] == 0xb2)
                userdata_start(f, p + i + 4, n - i - 4);
            else if (i + 4 + header_size(p[i + 3]) <= n)
                start_code(f, p[i + 3], p + i + 4, n - i - 4);
            i += 2;
        }
    /* the last CARRY bytes of all so far */
    keep = min(k + n, (unsigned int)CARRY);
    if (n >= keep) {
        memcpy(f->carry, p + n - keep, keep);
    } else {
        memmove(f->carry, f->carry + k - (keep - n), keep - n);
        memcpy(f->carry + keep - n, p, n);
    }
    f->carried = keep;
}

/* ---- end of the stream's headers ---- */

static u64 pes_ts(const u8 *b)
{
    return (u64)(b[0] >> 1 & 7) << 30 | (u64)b[1] << 22 | (u64)(b[2] >> 1) << 15 | (u64)b[3] << 7 |
           b[4] >> 1;
}

int vdec_packet(const u8 *pkt)
{
    struct vdec_file *f;
    unsigned long flags;
    unsigned int at = 4, n;
    int channel = -1;

    spin_lock_irqsave(&lock, flags);
    f = active;
    if (!f) {
        unread++;
        goto out;
    }
    packets++;
    channel = f->channel;
    if (f->fourcc != MPG2 || !(pkt[3] & 0x10))
        goto out;
    if (pkt[3] & 0x20)
        at += 1 + pkt[4];
    if (at >= 188)
        goto out;
    if (pkt[1] & 0x40) { /* a PES starts: its PTS */
        const u8 *pes = pkt + at;

        n = 188 - at;
        if (n < 9 || pes[0] || pes[1] || pes[2] != 1)
            goto out;
        f->pts = n >= 14 && pes[7] & 0x80 ? pes_ts(pes + 9) : U64_MAX;
        f->pts_used = false;
        at += 9 + pes[8];
        f->carried = 0;
        if (at >= 188)
            goto out;
    }
    scan(f, pkt + at, 188 - at);
out:
    spin_unlock_irqrestore(&lock, flags);
    return channel;
}

static void fill_picinfo(struct vdec_file *f, u8 *b)
{
    const struct picinfo *i = &f->sent;

    memset(b, 0, PICINFO_SIZE);
    b[0] = f->channel;
    put_unaligned_le16(i->aspect, b + 2);
    put_unaligned_le16(i->h, b + 4);
    put_unaligned_le16(i->v, b + 6);
    put_unaligned_le16(min(i->bitrate, 0xffffu), b + 8);
    put_unaligned_le16(i->progressive, b + 12);
    put_unaligned_le16(i->progressive, b + 14);
    put_unaligned_le16(i->h, b + 20); /* active window: the whole picture */
    put_unaligned_le16(i->v, b + 22);
    put_unaligned_le16(i->h, b + 24); /* display size */
    put_unaligned_le16(i->v, b + 26);
    put_unaligned_le32(i->num, b + 108);
    put_unaligned_le32(i->den, b + 112);
}

static void fill_status(struct vdec_file *f, u8 *b)
{
    memset(b, 0, STATUS_SIZE);
    b[0] = f->streaming ? 2 : 1; /* V4L2_EXT_VDEC_STATE_PLAYING, _INIT */
    b[1] = f->fourcc == MPG2 ? 1 : 0;
    put_unaligned_le32(f->pictures, b + 12);
    put_unaligned_le32(f->errors, b + 16);
    put_unaligned_le32(f->streaming, b + 20);
}

/* lock held */
static void stop(struct vdec_file *f)
{
    if (!f->streaming)
        return;
    f->streaming = false;
    if (active == f)
        active = NULL;
}

static long extended(struct vdec_file *f, unsigned int cmd, void __user *arg)
{
    u32 group[6], control[5];
    u8 data[UD_BUF];
    unsigned int size;
    unsigned long flags;
    long ret = 0;

    if (copy_from_user(group, arg, sizeof(group)))
        return -EFAULT;
    if (group[1] != 1)
        return -EINVAL;
    if (copy_from_user(control, compat_ptr(group[5]), sizeof(control)))
        return -EFAULT;
    if (cmd == S_EXT)
        return 0; /* the stream's modes: kept by the service, nothing here to change */
    size = control[1];
    spin_lock_irqsave(&lock, flags);
    if (control[0] == CID_PICINFO_EXT && control[1] == PICINFO_SIZE && f->have_info) {
        fill_picinfo(f, data);
    } else if (control[0] == CID_DECODER_STATUS && control[1] == STATUS_SIZE) {
        fill_status(f, data);
    } else if (control[0] == CID_USER_EVENT && f->ud_count) {
        /* the oldest USERDATA event's bytes: the service reads each after its event */
        const struct userdata *u = &f->sent_ud[f->ud_head];

        size = min(size, u->len);
        memcpy(data, u->data, size);
        f->ud_head = (f->ud_head + 1) % EVENTS;
        f->ud_count--;
    } else {
        ret = control[0] == CID_USER_EVENT ? -ENODATA : -EINVAL;
    }
    spin_unlock_irqrestore(&lock, flags);
    if (!ret && copy_to_user(compat_ptr(control[3]), data, size))
        ret = -EFAULT;
    return ret;
}

static long vdec_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct vdec_file *f = file->private_data;
    void __user *ptr = (void __user *)arg;
    unsigned long flags;
    u32 v[8];
    long ret = 0;

    switch (cmd) {
    case QUERYCAP: {
        u8 cap[104] = {};

        strscpy(cap, "lg-vdec", 16);
        strscpy(cap + 16, "VDEC", 32);
        strscpy(cap + 48, "platform:vdec", 32);
        put_unaligned_le32(0x84200800, cap + 84); /* M2M, STREAMING, DEVICE_CAPS */
        put_unaligned_le32(0x04200800, cap + 88);
        return copy_to_user(ptr, cap, sizeof(cap)) ? -EFAULT : 0;
    }
    case S_FMT:
    case G_FMT:
        if (copy_from_user(v, ptr, 16))
            return -EFAULT;
        spin_lock_irqsave(&lock, flags);
        if (cmd == S_FMT)
            f->fourcc = v[3];
        v[3] = f->fourcc;
        spin_unlock_irqrestore(&lock, flags);
        return cmd == G_FMT && copy_to_user(ptr, v, 16) ? -EFAULT : 0;
    case STREAMON:
    case STREAMOFF: {
        bool start = cmd == STREAMON, was;

        spin_lock_irqsave(&lock, flags);
        was = f->streaming;
        if (start && !was) {
            if (active && active != f) {
                spin_unlock_irqrestore(&lock, flags);
                return -EBUSY; /* one decoder of ours at a time */
            }
            f->streaming = true;
            f->seq_seen = f->have_info = false;
            f->carried = 0;
            f->pts = U64_MAX;
            f->pictures = f->sequences = f->errors = f->userdata = 0;
            f->in_picture = f->collecting = f->holding = false;
            f->ud_count = 0;
            memset(&f->info, 0, sizeof(f->info));
            active = f;
        } else if (!start) {
            stop(f);
        }
        spin_unlock_irqrestore(&lock, flags);
        if (start && !was)
            dtv_soc_av(WEBOS_TUNER_AV_START, WEBOS_MEDIA_VIDEO, 0, f->channel, f->fourcc, NULL, 0);
        else if (!start && was)
            dtv_soc_av(WEBOS_TUNER_AV_STOP, WEBOS_MEDIA_VIDEO, 0, f->channel, 0, NULL, 0);
        return 0;
    }
    case S_CTRL:
    case G_CTRL:
        if (copy_from_user(v, ptr, 8))
            return -EFAULT;
        spin_lock_irqsave(&lock, flags);
        if (v[0] == CID_CHANNEL && cmd == S_CTRL) {
            if (v[1] >= CHANNELS)
                ret = -EINVAL;
            else
                f->channel = v[1];
        } else if (v[0] == CID_CHANNEL) {
            v[1] = f->channel;
        } else {
            unsigned int i;

            for (i = 0; i < CTRLS && f->ctrl_id[i] && f->ctrl_id[i] != v[0]; i++)
                ;
            if (cmd == S_CTRL && i < CTRLS) {
                f->ctrl_id[i] = v[0];
                f->ctrl_val[i] = v[1];
            } else if (cmd == G_CTRL) {
                v[1] = i < CTRLS && f->ctrl_id[i] ? f->ctrl_val[i] : 0;
            }
        }
        spin_unlock_irqrestore(&lock, flags);
        return !ret && cmd == G_CTRL && copy_to_user(ptr, v, 8) ? -EFAULT : ret;
    case S_INPUT:
    case S_OUTPUT:
    case G_INPUT:
    case G_OUTPUT:
        if (copy_from_user(v, ptr, 4))
            return -EFAULT;
        if (cmd == S_INPUT)
            f->input = v[0];
        else if (cmd == S_OUTPUT)
            f->output = v[0];
        else
            v[0] = cmd == G_INPUT ? f->input : f->output;
        return (cmd == G_INPUT || cmd == G_OUTPUT) && copy_to_user(ptr, v, 4) ? -EFAULT : 0;
    case S_EXT:
    case G_EXT:
        return extended(f, cmd, ptr);
    case SUBSCRIBE:
    case UNSUBSCRIBE:
        if (copy_from_user(v, ptr, sizeof(v)))
            return -EFAULT;
        if (v[0] != EVENT_TYPE || v[1] < EV_FRAME || v[1] > EV_USERDATA) {
            pr_info_ratelimited("event type %#x id %#x flags %#x not ours\n", v[0], v[1], v[2]);
            return -EINVAL;
        }
        spin_lock_irqsave(&lock, flags);
        if (cmd == SUBSCRIBE)
            f->subs |= BIT(v[1] - EV_FRAME);
        else
            f->subs &= ~BIT(v[1] - EV_FRAME);
        spin_unlock_irqrestore(&lock, flags);
        return 0;
    case DQEVENT:
        for (;;) {
            u8 e[128];

            spin_lock_irqsave(&lock, flags);
            if (f->count) {
                memcpy(e, f->events[f->head], sizeof(e));
                f->head = (f->head + 1) % EVENTS;
                f->count--;
                put_unaligned_le32(f->count, e + 72);
                spin_unlock_irqrestore(&lock, flags);
                return copy_to_user(ptr, e, sizeof(e)) ? -EFAULT : 0;
            }
            spin_unlock_irqrestore(&lock, flags);
            if (file->f_flags & O_NONBLOCK)
                return -ENOENT; /* V4L2: no event pending */
            ret = wait_event_interruptible(f->wait, READ_ONCE(f->count));
            if (ret)
                return ret;
        }
    default:
        pr_info_ratelimited("ioctl %#x not handled\n", cmd);
        return -ENOTTY;
    }
}

static int vdec_open(struct inode *inode, struct file *file)
{
    struct vdec_file *f = kzalloc(sizeof(*f), GFP_KERNEL);
    unsigned long flags;

    if (!f)
        return -ENOMEM;
    init_waitqueue_head(&f->wait);
    f->pts = U64_MAX;
    spin_lock_irqsave(&lock, flags);
    list_add(&f->link, &files);
    spin_unlock_irqrestore(&lock, flags);
    file->private_data = f;
    return 0;
}

static int vdec_release(struct inode *inode, struct file *file)
{
    struct vdec_file *f = file->private_data;
    unsigned long flags;
    bool was;

    spin_lock_irqsave(&lock, flags);
    was = f->streaming;
    stop(f);
    list_del(&f->link);
    spin_unlock_irqrestore(&lock, flags);
    if (was)
        dtv_soc_av(WEBOS_TUNER_AV_STOP, WEBOS_MEDIA_VIDEO, 0, f->channel, 0, NULL, 0);
    kfree(f);
    return 0;
}

static __poll_t vdec_poll(struct file *file, poll_table *wait)
{
    struct vdec_file *f = file->private_data;

    poll_wait(file, &f->wait, wait);
    return READ_ONCE(f->count) ? EPOLLPRI : 0;
}

static const struct file_operations fops = {
    .owner = THIS_MODULE,
    .open = vdec_open,
    .release = vdec_release,
    .unlocked_ioctl = vdec_ioctl,
    .compat_ioctl = vdec_ioctl,
    .poll = vdec_poll,
};

void vdec_show(struct seq_file *m)
{
    struct vdec_file *f;
    unsigned long flags;

    spin_lock_irqsave(&lock, flags);
    seq_printf(m, "vdec packets %lu, without a decoder %lu\n", packets, unread);
    list_for_each_entry(f, &files, link)
        seq_printf(m,
                   "vdec channel %u %s fourcc %#x subs %#x sequences %u pictures %u errors %u "
                   "picinfo %ux%u %u/%u%s events queued %u userdata %u\n",
                   f->channel, f->streaming ? "streaming" : "stopped", f->fourcc, f->subs,
                   f->sequences, f->pictures, f->errors, f->sent.h, f->sent.v, f->sent.num,
                   f->sent.den, f->sent.progressive ? " progressive" : "", f->count, f->userdata);
    spin_unlock_irqrestore(&lock, flags);
}

int vdec_init(void)
{
    struct device *dev;
    int err = alloc_chrdev_region(&devt, 0, 1, "lg-vdec");

    if (err)
        return err;
    cdev_init(&cdev, &fops);
    cdev.owner = THIS_MODULE;
    err = cdev_add(&cdev, devt, 1);
    if (err)
        goto fail;
    cdev_added = true;
    vdec_class = class_create("lg-vdec");
    if (IS_ERR(vdec_class)) {
        err = PTR_ERR(vdec_class);
        vdec_class = NULL;
        goto fail;
    }
    vdec_class->devnode = vdec_devnode;
    dev = device_create(vdec_class, NULL, devt, NULL, "video20");
    if (IS_ERR(dev)) {
        err = PTR_ERR(dev);
        goto fail;
    }
    return 0;
fail:
    vdec_exit();
    return err;
}

void vdec_exit(void)
{
    if (vdec_class) {
        device_destroy(vdec_class, devt);
        class_destroy(vdec_class);
        vdec_class = NULL;
    }
    if (cdev_added)
        cdev_del(&cdev);
    cdev_added = false;
    if (devt)
        unregister_chrdev_region(devt, 1);
    devt = 0;
}
