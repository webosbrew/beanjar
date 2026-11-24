// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* dtv_soc.ko: the SoC's demodulator output and demux, fed from the host. On a set, the tuner and
 * demodulator chips behind webos_tv.ko's frontends put out a transport stream, which the SoC's
 * demux filters for the software (the stock layout's per-system *_dtv_soc and the SoC's demux
 * module). Here the host has the antenna: it says which frequencies carry a signal (the channel
 * map) and sends one stream, that of the latest tune with a signal (soc_tuned; frontend 0 alone on
 * most boards) (include/webos-tuner.h, through the
 * initramfs's pump, guest/initramfs/src/virtio-tuner.c, and /dev/dtv-soc). This module is
 * webos_tv's signal source (webos_tv.h): a frontend tuned within LOCK_TOLERANCE_HZ of a mapped
 * channel's frequency, with its delivery system, locks.
 *
 * The demuxes are the DVB core's software demux and its device (dvb_demux, dmxdev): pollable
 * /dev/dvb/adapter0/demux<n> and dvr<n> with section filters, the CRC check and immediate start,
 * as legacy-broadcast uses them (it opens demux0 to demux3 at init, and on lock sets a filter per
 * table on demux0 with DMX_SET_BUFFER_SIZE, DMX_SET_FILTER with DMX_CHECK_CRC |
 * DMX_IMMEDIATE_START, and epoll). Every demux gets that one stream.
 *
 * The decoders' input: the demuxes' decoder filters (DMX_OUT_DECODER: the PCR, video and audio
 * PES filters of the channel being watched) are the SoC's path to its decoders, which the DVB core
 * hands to write_to_decoder. Here the video filter's packets go through the video decoder's node
 * (vdec.c, /dev/video20), which reads their headers for its events, and on to the host, as do the
 * audio filters' and the PCRs: all as records of /dev/dtv-soc-av (include/webos-tuner.h), which
 * the initramfs's pump sends to the host's media decoder. A video filter's packets go while the
 * video decoder streams; an audio filter's while it is started (the audio decoder, LG's ALSA
 * "Adec" controls, is not ours: the stream says its codec).
 *
 * /proc/dtv_soc says the map, the tune and what came: frames, packets fed, packets dropped (not
 * of the tuned frequency, or no signal there) and header resyncs; the decoders' records. */
#include <linux/kfifo.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/poll.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <media/dmxdev.h>
#include <media/dvb_demux.h>
#include "../../../../include/webos-media.h"
#include "../../../../include/webos-tuner.h"
#include "dtv_soc.h"
#include "webos_tv.h"

#define DEMUX_MAX         4
#define LOCK_TOLERANCE_HZ 1000000 /* channels are 6 MHz or more apart */
#define FILTERS           32
#define AV_FIFO           (2u << 20) /* about 8 s of the test channels' stream */

static unsigned int demuxes = 4;
module_param(demuxes, uint, 0444);
MODULE_PARM_DESC(demuxes, "demux devices, demux0 to demux<n - 1> (1 to 4)");

/* ---- the host's frames ----
 * A parser of include/webos-tuner.h's stream, one per open file: it gathers a frame's header, a
 * map whole, and transport stream packets whole, and resynchronises a byte at a time on a header
 * that is not one (a bad magic, type or length). */
struct tuner_sink {
    void (*map)(void *ctx, const struct webos_tuner_channel *chans, unsigned int n);
    void (*ts)(void *ctx, u32 freq, const u8 *pkts, unsigned int n);
    void *ctx;
};

struct tuner_parser {
    u8 hdr[sizeof(struct webos_tuner_frame)];
    unsigned int got;     /* header bytes */
    u32 type, freq, left; /* the frame's; payload bytes still to come */
    u8 map[WEBOS_TUNER_MAP_MAX * 8];
    unsigned int mapgot;
    u8 pkt[WEBOS_TUNER_PACKET];
    unsigned int pktgot;
    unsigned long frames, resyncs;
};

static u32 tuner_le32(const u8 *b)
{
    return b[0] | b[1] << 8 | b[2] << 16 | (u32)b[3] << 24;
}

static void tuner_map_done(struct tuner_parser *p, const struct tuner_sink *s)
{
    struct webos_tuner_channel chans[WEBOS_TUNER_MAP_MAX];
    unsigned int n = p->mapgot / 8;

    for (unsigned int i = 0; i < n; i++) {
        chans[i].freq_hz = tuner_le32(p->map + 8 * i);
        chans[i].delsys = tuner_le32(p->map + 8 * i + 4);
    }
    s->map(s->ctx, chans, n);
}

/* the header in p->hdr is one: its frame begins */
static bool tuner_header(struct tuner_parser *p)
{
    u32 type = tuner_le32(p->hdr + 4), len = tuner_le32(p->hdr + 12);

    if (tuner_le32(p->hdr) != WEBOS_TUNER_MAGIC)
        return false;
    if (type == WEBOS_TUNER_MAP  ? len % 8 || len > WEBOS_TUNER_MAP_MAX * 8
        : type == WEBOS_TUNER_TS ? len % WEBOS_TUNER_PACKET || len > WEBOS_TUNER_TS_MAX
                                 : true)
        return false;
    p->type = type;
    p->freq = tuner_le32(p->hdr + 8);
    p->left = len;
    p->mapgot = p->pktgot = 0;
    p->frames++;
    return true;
}

static void tuner_feed(struct tuner_parser *p, const struct tuner_sink *s, const u8 *b, size_t n)
{
    while (n) {
        if (p->got < sizeof(p->hdr)) {
            size_t k = min(n, sizeof(p->hdr) - p->got);

            memcpy(p->hdr + p->got, b, k);
            p->got += k;
            b += k;
            n -= k;
            if (p->got < sizeof(p->hdr))
                return;
            if (!tuner_header(p)) { /* look for a header a byte further on */
                memmove(p->hdr, p->hdr + 1, sizeof(p->hdr) - 1);
                p->got--;
                p->resyncs++;
                continue;
            }
            if (!p->left) {
                if (p->type == WEBOS_TUNER_MAP)
                    tuner_map_done(p, s);
                p->got = 0;
            }
            continue;
        }
        if (p->type == WEBOS_TUNER_MAP) {
            size_t k = min_t(size_t, n, p->left);

            memcpy(p->map + p->mapgot, b, k);
            p->mapgot += k;
            p->left -= k;
            b += k;
            n -= k;
            if (!p->left) {
                tuner_map_done(p, s);
                p->got = 0;
            }
            continue;
        }
        /* WEBOS_TUNER_TS: whole packets */
        if (p->pktgot || min_t(size_t, n, p->left) < WEBOS_TUNER_PACKET) {
            size_t k = min3(n, (size_t)p->left, (size_t)(WEBOS_TUNER_PACKET - p->pktgot));

            memcpy(p->pkt + p->pktgot, b, k);
            p->pktgot += k;
            p->left -= k;
            b += k;
            n -= k;
            if (p->pktgot == WEBOS_TUNER_PACKET) {
                s->ts(s->ctx, p->freq, p->pkt, 1);
                p->pktgot = 0;
            }
        } else {
            unsigned int pkts = min_t(size_t, n, p->left) / WEBOS_TUNER_PACKET;

            s->ts(s->ctx, p->freq, b, pkts);
            p->left -= pkts * WEBOS_TUNER_PACKET;
            b += pkts * WEBOS_TUNER_PACKET;
            n -= pkts * WEBOS_TUNER_PACKET;
        }
        if (!p->left)
            p->got = 0;
    }
}

/* whether a channel of map's n has freq within LOCK_TOLERANCE_HZ and the delivery system delsys */
static bool tuner_mapped(const struct webos_tuner_channel *map, unsigned int n, u32 freq,
                         u32 delsys)
{
    for (unsigned int i = 0; i < n && freq; i++)
        if (map[i].delsys == delsys &&
            (freq > map[i].freq_hz ? freq - map[i].freq_hz : map[i].freq_hz - freq) <=
                LOCK_TOLERANCE_HZ)
            return true;
    return false;
}
/* ---- end of the host's frames ---- */

static struct {
    struct dvb_demux demux;
    struct dmxdev dmxdev;
    bool made;
} dmx[DEMUX_MAX];
static struct dvb_adapter *adapter;

static DEFINE_SPINLOCK(state_lock);
static struct webos_tuner_channel map[WEBOS_TUNER_MAP_MAX];
static unsigned int nmap;
static u32 tuned_freq, tuned_delsys;
static bool pending; /* a WEBOS_TUNER_TUNE frame for the host */
static DECLARE_WAIT_QUEUE_HEAD(pending_wait);
static unsigned long fed, dropped, frames, resyncs;
static atomic_t feeds = ATOMIC_INIT(0);
static unsigned long started; /* feeds (section filters, PES or TS) started */
static u16 started_pids[8];   /* the PIDs of the last ones */
/* the decoders' input, /dev/dtv-soc-av's records */
static DEFINE_SPINLOCK(av_lock);
static DECLARE_KFIFO_PTR(av_fifo, u8);
static DECLARE_WAIT_QUEUE_HEAD(av_wait);
static unsigned long av_records, av_lost, av_bytes;
static bool av_made;

static bool soc_signal(u32 freq, u32 delsys)
{
    unsigned long flags;
    bool on;

    spin_lock_irqsave(&state_lock, flags);
    on = tuner_mapped(map, nmap, freq, delsys);
    spin_unlock_irqrestore(&state_lock, flags);
    return on;
}

/* The host gets one stream: the one of the latest tune that has a signal. A tune without one moves
 * it only from the frontend fed now (a scan's), so a second frontend's watch is not taken away by
 * the other tuning to nothing. */
static unsigned int fed_fe;

static void soc_tuned(unsigned int fe, u32 freq, u32 delsys)
{
    unsigned long flags;

    spin_lock_irqsave(&state_lock, flags);
    if (fe != fed_fe && !tuner_mapped(map, nmap, freq, delsys)) {
        spin_unlock_irqrestore(&state_lock, flags);
        return;
    }
    fed_fe = fe;
    tuned_freq = freq;
    tuned_delsys = delsys;
    pending = true;
    spin_unlock_irqrestore(&state_lock, flags);
    wake_up_interruptible(&pending_wait);
}

static const struct webos_tv_source source = {.signal = soc_signal, .tuned = soc_tuned};

static void sink_map(void *ctx, const struct webos_tuner_channel *chans, unsigned int n)
{
    unsigned long flags;

    spin_lock_irqsave(&state_lock, flags);
    memcpy(map, chans, n * sizeof(*chans));
    nmap = n;
    pending = true; /* the host (re)sent its map: tell it the tune */
    spin_unlock_irqrestore(&state_lock, flags);
    wake_up_interruptible(&pending_wait);
    pr_info("dtv_soc: %u channels carry a signal\n", n);
}

static void sink_ts(void *ctx, u32 freq, const u8 *pkts, unsigned int n)
{
    unsigned long flags;
    bool on;

    spin_lock_irqsave(&state_lock, flags);
    on = freq && freq == tuned_freq && tuner_mapped(map, nmap, freq, tuned_delsys);
    if (on)
        fed += n;
    else
        dropped += n;
    spin_unlock_irqrestore(&state_lock, flags);
    if (!on)
        return;
    for (unsigned int i = 0; i < demuxes; i++)
        dvb_dmx_swfilter_packets(&dmx[i].demux, pkts, n);
}

/* ---- /dev/dtv-soc: the pump's end ---- */

struct soc_file {
    struct tuner_parser parser;
    struct tuner_sink sink;
};

static int soc_open(struct inode *inode, struct file *file)
{
    struct soc_file *f = kzalloc(sizeof(*f), GFP_KERNEL);

    if (!f)
        return -ENOMEM;
    f->sink = (struct tuner_sink){.map = sink_map, .ts = sink_ts};
    file->private_data = f;
    return nonseekable_open(inode, file);
}

static int soc_release(struct inode *inode, struct file *file)
{
    struct soc_file *f = file->private_data;
    unsigned long flags;

    spin_lock_irqsave(&state_lock, flags);
    frames += f->parser.frames;
    resyncs += f->parser.resyncs;
    spin_unlock_irqrestore(&state_lock, flags);
    kfree(f);
    return 0;
}

static ssize_t soc_write(struct file *file, const char __user *buf, size_t count, loff_t *pos)
{
    struct soc_file *f = file->private_data;
    u8 chunk[4 * WEBOS_TUNER_PACKET];
    size_t done = 0;

    while (done < count) {
        size_t k = min(count - done, sizeof(chunk));

        if (copy_from_user(chunk, buf + done, k))
            return done ? done : -EFAULT;
        tuner_feed(&f->parser, &f->sink, chunk, k);
        done += k;
    }
    return done;
}

static ssize_t soc_read(struct file *file, char __user *buf, size_t count, loff_t *pos)
{
    u8 out[sizeof(struct webos_tuner_frame) + 4];
    struct webos_tuner_frame h = {.magic = WEBOS_TUNER_MAGIC, .type = WEBOS_TUNER_TUNE, .len = 4};
    unsigned long flags;
    u32 delsys;
    int err;

    if (count < sizeof(out))
        return -EINVAL;
    for (;;) {
        spin_lock_irqsave(&state_lock, flags);
        if (pending) {
            pending = false;
            h.freq_hz = tuned_freq;
            delsys = tuned_delsys;
            spin_unlock_irqrestore(&state_lock, flags);
            break;
        }
        spin_unlock_irqrestore(&state_lock, flags);
        if (file->f_flags & O_NONBLOCK)
            return -EAGAIN;
        err = wait_event_interruptible(pending_wait, READ_ONCE(pending));
        if (err)
            return err;
    }
    memcpy(out, &h, sizeof(h)); /* little-endian, as the guest */
    memcpy(out + sizeof(h), &delsys, 4);
    return copy_to_user(buf, out, sizeof(out)) ? -EFAULT : sizeof(out);
}

static __poll_t soc_poll(struct file *file, poll_table *wait)
{
    poll_wait(file, &pending_wait, wait);
    return EPOLLOUT | EPOLLWRNORM | (READ_ONCE(pending) ? EPOLLIN | EPOLLRDNORM : 0);
}

static const struct file_operations soc_fops = {
    .owner = THIS_MODULE,
    .open = soc_open,
    .release = soc_release,
    .read = soc_read,
    .write = soc_write,
    .poll = soc_poll,
};

static struct miscdevice soc_misc = {
    .minor = MISC_DYNAMIC_MINOR, .name = "dtv-soc", .fops = &soc_fops, .mode = 0600};
static bool misc_made;

static int stats_show(struct seq_file *m, void *v)
{
    unsigned long flags;

    spin_lock_irqsave(&state_lock, flags);
    seq_printf(m, "map");
    for (unsigned int i = 0; i < nmap; i++)
        seq_printf(m, " %u/%u", map[i].freq_hz, map[i].delsys);
    seq_printf(m, "\ntuned %u delsys %u signal %d\n", tuned_freq, tuned_delsys,
               tuner_mapped(map, nmap, tuned_freq, tuned_delsys));
    seq_printf(m, "packets fed %lu dropped %lu\nframes %lu resyncs %lu (closed files)\n", fed,
               dropped, frames, resyncs);
    seq_printf(m, "started %lu, the last PIDs", started);
    for (unsigned long i = started > ARRAY_SIZE(started_pids) ? started - ARRAY_SIZE(started_pids)
                                                              : 0;
         i < started; i++)
        seq_printf(m, " %#x", started_pids[i % ARRAY_SIZE(started_pids)]);
    seq_printf(m, "\n");
    spin_unlock_irqrestore(&state_lock, flags);
    seq_printf(m, "feeds %d\n", atomic_read(&feeds));
    spin_lock_irqsave(&av_lock, flags);
    seq_printf(m, "av records %lu bytes %lu lost %lu queued %u\n", av_records, av_bytes, av_lost,
               kfifo_len(&av_fifo));
    spin_unlock_irqrestore(&av_lock, flags);
    vdec_show(m);
    return 0;
}

/* ---- /dev/dtv-soc-av: the decoders' input ---- */

void dtv_soc_av(u16 type, u8 kind, u8 decoder, u32 channel, u32 codec, const void *data, u32 len)
{
    struct webos_tuner_av h = {.magic = WEBOS_TUNER_AV_MAGIC,
                               .type = type,
                               .kind = kind,
                               .decoder = decoder,
                               .channel = channel,
                               .codec = codec,
                               .len = len};
    unsigned long flags;

    spin_lock_irqsave(&av_lock, flags);
    if (kfifo_avail(&av_fifo) < sizeof(h) + len) {
        av_lost++;
    } else {
        kfifo_in(&av_fifo, (const u8 *)&h, sizeof(h));
        if (len)
            kfifo_in(&av_fifo, (const u8 *)data, len);
        av_records++;
        av_bytes += len;
    }
    spin_unlock_irqrestore(&av_lock, flags);
    wake_up_interruptible(&av_wait);
}

/* whole records, as many as fit */
static ssize_t av_read(struct file *file, char __user *buf, size_t count, loff_t *pos)
{
    static DEFINE_MUTEX(readers);
    u8 *chunk;
    size_t done = 0;
    int err;

    for (;;) {
        if (!kfifo_is_empty(&av_fifo))
            break;
        if (file->f_flags & O_NONBLOCK)
            return -EAGAIN;
        err = wait_event_interruptible(av_wait, !kfifo_is_empty(&av_fifo));
        if (err)
            return err;
    }
    chunk = kmalloc(min_t(size_t, count, 256u << 10), GFP_KERNEL);
    if (!chunk)
        return -ENOMEM;
    mutex_lock(&readers);
    while (done < min_t(size_t, count, 256u << 10)) {
        struct webos_tuner_av h;
        unsigned long flags;
        size_t need;

        spin_lock_irqsave(&av_lock, flags);
        if (kfifo_out_peek(&av_fifo, (u8 *)&h, sizeof(h)) != sizeof(h)) {
            spin_unlock_irqrestore(&av_lock, flags);
            break;
        }
        need = sizeof(h) + h.len;
        if (done + need > min_t(size_t, count, 256u << 10)) {
            spin_unlock_irqrestore(&av_lock, flags);
            break;
        }
        if (kfifo_out(&av_fifo, chunk + done, need) != need) { /* cannot happen: written whole */
            kfifo_reset_out(&av_fifo);
            spin_unlock_irqrestore(&av_lock, flags);
            break;
        }
        spin_unlock_irqrestore(&av_lock, flags);
        done += need;
    }
    mutex_unlock(&readers);
    if (!done)
        err = count < sizeof(struct webos_tuner_av) ? -EINVAL : -EAGAIN;
    else
        err = copy_to_user(buf, chunk, done) ? -EFAULT : 0;
    kfree(chunk);
    return err ? err : done;
}

static __poll_t av_poll(struct file *file, poll_table *wait)
{
    poll_wait(file, &av_wait, wait);
    return kfifo_is_empty(&av_fifo) ? 0 : EPOLLIN | EPOLLRDNORM;
}

static const struct file_operations av_fops = {
    .owner = THIS_MODULE,
    .read = av_read,
    .poll = av_poll,
    .llseek = noop_llseek,
};

static struct miscdevice av_misc = {
    .minor = MISC_DYNAMIC_MINOR, .name = "dtv-soc-av", .fops = &av_fops, .mode = 0600};

/* the audio decoders: index by the filter's pes_type (DMX_PES_AUDIO0..3) */
static int audio_index(int pes_type)
{
    switch (pes_type) {
    case DMX_PES_AUDIO0:
        return 0;
    case DMX_PES_AUDIO1:
        return 1;
    case DMX_PES_AUDIO2:
        return 2;
    case DMX_PES_AUDIO3:
        return 3;
    }
    return -1;
}

static bool is_video(int pes_type)
{
    return pes_type == DMX_PES_VIDEO0;
}

static bool is_pcr(int pes_type)
{
    return pes_type == DMX_PES_PCR0;
}

static int write_to_decoder(struct dvb_demux_feed *feed, const u8 *pkt, size_t len)
{
    int a = audio_index(feed->pes_type);

    if (len != 188)
        return 0;
    if (is_video(feed->pes_type)) {
        int channel = vdec_packet(pkt);

        if (channel >= 0)
            dtv_soc_av(WEBOS_TUNER_AV_TS, WEBOS_MEDIA_VIDEO, 0, channel, 0, pkt, 188);
    } else if (a >= 0) {
        dtv_soc_av(WEBOS_TUNER_AV_TS, WEBOS_MEDIA_AUDIO, a, a, 0, pkt, 188);
    } else if (is_pcr(feed->pes_type) && pkt[3] & 0x20 && pkt[4] >= 7 && pkt[5] & 0x10) {
        const u8 *b = pkt + 6;
        struct webos_tuner_pcr pcr = {
            .pcr = ((u64)b[0] << 25 | b[1] << 17 | b[2] << 9 | b[3] << 1 | b[4] >> 7) * 300 +
                   ((b[4] & 1) << 8 | b[5]),
            .mono_ns = ktime_get_ns()};

        dtv_soc_av(WEBOS_TUNER_AV_PCR, 0, 0, 0, 0, &pcr, sizeof(pcr));
    }
    return 0;
}

/* ---- the demuxes ---- */

/* an audio decoder filter starts or stops its decoder's stream */
static void audio_feed(struct dvb_demux_feed *feed, u16 type)
{
    int a = audio_index(feed->pes_type);

    if (feed->type == DMX_TYPE_TS && feed->ts_type & TS_DECODER && a >= 0)
        dtv_soc_av(type, WEBOS_MEDIA_AUDIO, a, a, 0, NULL, 0);
}

static int start_feed(struct dvb_demux_feed *feed)
{
    unsigned long flags;

    atomic_inc(&feeds);
    spin_lock_irqsave(&state_lock, flags);
    started_pids[started++ % ARRAY_SIZE(started_pids)] = feed->pid;
    spin_unlock_irqrestore(&state_lock, flags);
    audio_feed(feed, WEBOS_TUNER_AV_START);
    return 0;
}

static int stop_feed(struct dvb_demux_feed *feed)
{
    atomic_dec(&feeds);
    audio_feed(feed, WEBOS_TUNER_AV_STOP);
    return 0;
}

static struct proc_dir_entry *proc;

static void dtv_soc_cleanup(void)
{
    proc_remove(proc);
    vdec_exit();
    if (av_made)
        misc_deregister(&av_misc);
    av_made = false;
    if (misc_made)
        misc_deregister(&soc_misc);
    if (adapter)
        webos_tv_detach();
    for (unsigned int i = DEMUX_MAX; i--;)
        if (dmx[i].made) {
            dvb_dmxdev_release(&dmx[i].dmxdev);
            dvb_dmx_release(&dmx[i].demux);
            dmx[i].made = false;
        }
    adapter = NULL;
}

static int __init dtv_soc_init(void)
{
    struct dvb_adapter *a;
    int err;

    if (!demuxes || demuxes > DEMUX_MAX) {
        pr_err("dtv_soc: demuxes=%u: 1 to %u\n", demuxes, DEMUX_MAX);
        return -EINVAL;
    }
    err = kfifo_alloc(&av_fifo, AV_FIFO, GFP_KERNEL);
    if (err)
        return err;
    a = webos_tv_attach(&source);
    if (!a) {
        pr_err("dtv_soc: webos_tv has no adapter, or a source already\n");
        kfifo_free(&av_fifo);
        return -ENODEV;
    }
    adapter = a;
    for (unsigned int i = 0; i < demuxes; i++) {
        struct dvb_demux *d = &dmx[i].demux;

        d->priv = &dmx[i];
        d->filternum = FILTERS;
        d->feednum = FILTERS;
        d->start_feed = start_feed;
        d->stop_feed = stop_feed;
        d->write_to_decoder = write_to_decoder;
        d->dmx.capabilities = DMX_TS_FILTERING | DMX_SECTION_FILTERING;
        err = dvb_dmx_init(d);
        if (err)
            goto fail;
        dmx[i].dmxdev.filternum = FILTERS;
        dmx[i].dmxdev.demux = &d->dmx;
        dmx[i].dmxdev.capabilities = 0;
        err = dvb_dmxdev_init(&dmx[i].dmxdev, adapter);
        if (err) {
            dvb_dmx_release(d);
            goto fail;
        }
        dmx[i].made = true;
    }
    err = misc_register(&soc_misc);
    if (err)
        goto fail;
    misc_made = true;
    err = misc_register(&av_misc);
    if (err)
        goto fail;
    av_made = true;
    err = vdec_init();
    if (err)
        goto fail;
    proc = proc_create_single("dtv_soc", 0444, NULL, stats_show);
    pr_info("dtv_soc: %u demux(es) on adapter %d, the signal from /dev/dtv-soc\n", demuxes,
            adapter->num);
    return 0;
fail:
    dtv_soc_cleanup();
    kfifo_free(&av_fifo);
    return err;
}

static void __exit dtv_soc_exit(void)
{
    dtv_soc_cleanup();
    kfifo_free(&av_fifo);
}

module_init(dtv_soc_init);
module_exit(dtv_soc_exit);
MODULE_LICENSE("GPL");
MODULE_VERSION("emulator");
MODULE_DESCRIPTION("The SoC's demodulator output and software demux, fed from the host "
                   "(beanjar)");
