// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The set's built-in far-field microphone ("Hey LG"): the capture card LG's voice stack opens,
 * which records what the host sends it, or silence.
 *
 * LG's voiceinput_sound (webOS 6 and 7's far-field voice stack: ponytail, queue, ombre) takes a
 * udev "sound" device whose sysname has "card", and keeps it when the card's ALSA short name is one
 * of its predefined built-in devices (usr/share/voiceinput/sound/preDefinedDeviceList.json: LGSIC,
 * Mars) with vendor and product 0xffff. Those come from /proc/asound/cardN/usbid when the card has
 * one, and a parsed usbid can never compare equal, so this is no USB card and has none. It probes
 * hw:N,0 for capture (blocking, S16_LE, interleaved) at the highest rate of its table the card has
 * (48 kHz here) and the fewest channels at it, mixes that down to mono (CSoundUtil::toMono takes
 * stereo only) and resamples to 16 kHz for its preprocessor. Its built-in device's capture class
 * (CCaptureSoundMixLG) reads through nyx's AecFe, LG's libaec-fe, which opens the five PCMs of its
 * preset table in order, hw:1,0 to hw:1,4 (mic, spk, mix, se, bt: test_aec-fe opens hw:1,0, then
 * hw:1,1, and aborts when that is missing); the four after the microphone are the echo canceller's
 * references, the TV's own output, and they record silence here. No control element is used.
 *
 * The card is registered after the built-in virtio-sound card (lgemu.ko loads from the initramfs),
 * so the playback card keeps index 0 and its device numbers. A capture runs at real time; the
 * pointer is the time since its start, and the frames up to it are written when it is read.
 *
 * What the microphone hears comes from the host: the webos.mic virtio-serial port, whose host side
 * is the VM's mic.sock (host/core/beanjar/mic.py), carries 48 kHz stereo S16_LE, and the
 * initramfs's pump (guest/initramfs/src/virtio-mic.c) writes it to /dev/lgemu-mic. That is the air
 * around the set: sound after a silence is heard from AIR_DELAY_NS after it arrives, at real time,
 * and a writer is held back while what it wrote reaches more than AIR_LEAD_NS ahead, so the host
 * may send a file as fast as the port takes it. Every running capture of PCM 0 records the air at
 * its own rate (linear interpolation), channel i from the air's channel i, and silence where none
 * was sent.
 *
 *   mic  the board's model has the microphone (targets.Soc.mic, a fact of its hardware): register
 *        the card and /dev/lgemu-mic; i2c.c's micom then reports the microphone (0x16) and its
 *        switch (0x67), and without it reports none, as a set without one */
#define pr_fmt(fmt) KBUILD_MODNAME ": mic: " fmt
#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/hrtimer.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/string.h>
#include <linux/vmalloc.h>
#include <sound/core.h>
#include <sound/pcm.h>
#include "lgemu.h"

#define NAME       "LGSIC"
#define CHANNELS   2
#define BUFFER_MAX (256 * 1024)

/* ---- the air: what the microphone hears (no kernel API; tests/unit/kernel/test_lgemu_mic.py
 * compiles this part on the host) ---- */
#define AIR_RATE     48000
#define AIR_CHANNELS 2
#define AIR_FRAMES   (4 * AIR_RATE)    /* the ring: 4 s */
#define AIR_LEAD_NS  (500 * 1000000LL) /* how far ahead of now a writer may be */
#define AIR_DELAY_NS (20 * 1000000LL)  /* sound after a silence is heard this much later */
#define AIR_NS       1000000000LL

struct mic_air {
    s16 ring[AIR_FRAMES][AIR_CHANNELS];
    u64 head;    /* the frames of the current sound so far */
    s64 base_ns; /* when its first frame is heard */
};

/* when the current sound ends: the time after its last frame */
static s64 air_end(const struct mic_air *a)
{
    return a->base_ns + (s64)(a->head * AIR_NS / AIR_RATE);
}

/* up to n frames into the air at time now; the frames taken (0: it is AIR_LEAD_NS ahead) */
static u32 air_write(struct mic_air *a, s64 now, const s16 *frames, u32 n)
{
    s64 room;
    u32 i;

    if (now >= air_end(a)) { /* silence: a new sound, heard a little later */
        a->head = 0;
        a->base_ns = now + AIR_DELAY_NS;
    }
    room = (now + AIR_LEAD_NS - a->base_ns) * AIR_RATE / AIR_NS - (s64)a->head;
    if (room <= 0)
        return 0;
    if (n > room)
        n = room;
    for (i = 0; i < n; i++, a->head++)
        memcpy(a->ring[a->head % AIR_FRAMES], frames + i * AIR_CHANNELS, sizeof(a->ring[0]));
    return n;
}

/* the air's channel ch (its last where it has fewer) at time ns, linearly interpolated between
 * its frames; 0 where there is no sound */
static s16 air_sample(const struct mic_air *a, s64 ns, unsigned int ch)
{
    u64 pos, frame;
    s64 frac;
    s32 x, y;

    if (ns < a->base_ns || ns >= air_end(a))
        return 0;
    pos = (u64)(ns - a->base_ns) * AIR_RATE; /* in frames, times 1e9 */
    frame = pos / AIR_NS;
    frac = pos % AIR_NS;
    if (frame + AIR_FRAMES <= a->head) /* overwritten: long past */
        return 0;
    if (ch >= AIR_CHANNELS)
        ch = AIR_CHANNELS - 1;
    x = a->ring[frame % AIR_FRAMES][ch];
    y = frame + 1 < a->head ? a->ring[(frame + 1) % AIR_FRAMES][ch] : x;
    return x + (s32)((y - x) * frac / AIR_NS);
}
/* ---- end of the air ---- */

static bool mic;
module_param(mic, bool, 0444);
MODULE_PARM_DESC(mic, "mic: register the built-in microphone's capture card (LGSIC)");

bool lgemu_mic_present(void)
{
    return mic;
}

static struct mic_air *air;
static DEFINE_SPINLOCK(air_lock); /* taken in the captures' pointer, in atomic context */

/* ---- /dev/lgemu-mic: the air from the host ---- */
#define WRITE_FRAMES 1024

static ssize_t air_dev_write(struct file *file, const char __user *buf, size_t n, loff_t *pos)
{
    s16 *frames;
    size_t done = 0;
    ssize_t err = 0;

    if (n % (AIR_CHANNELS * 2))
        return -EINVAL; /* whole frames only */
    frames = kmalloc_array(WRITE_FRAMES, AIR_CHANNELS * 2, GFP_KERNEL);
    if (!frames)
        return -ENOMEM;
    while (done < n) {
        u32 count = min_t(size_t, (n - done) / (AIR_CHANNELS * 2), WRITE_FRAMES), taken = 0;
        unsigned long flags;

        if (copy_from_user(frames, buf + done, count * AIR_CHANNELS * 2)) {
            err = -EFAULT;
            break;
        }
        while (taken < count) {
            u32 k;

            spin_lock_irqsave(&air_lock, flags);
            k = air_write(air, ktime_get_ns(), frames + taken * AIR_CHANNELS, count - taken);
            spin_unlock_irqrestore(&air_lock, flags);
            taken += k;
            if (k)
                continue;
            if (file->f_flags & O_NONBLOCK) {
                err = -EAGAIN;
                break;
            }
            if (msleep_interruptible(10)) {
                err = -ERESTARTSYS;
                break;
            }
        }
        done += taken * AIR_CHANNELS * 2;
        if (err)
            break;
    }
    kfree(frames);
    return done ? done : err;
}

static const struct file_operations air_fops = {
    .owner = THIS_MODULE,
    .open = stream_open,
    .write = air_dev_write,
};

static struct miscdevice air_device = {
    .minor = MISC_DYNAMIC_MINOR,
    .name = "lgemu-mic",
    .fops = &air_fops,
    .mode = 0200,
};

struct mic_stream {
    struct snd_pcm_substream *substream;
    struct hrtimer timer;
    ktime_t start;
    ktime_t period;
    u64 written; /* frames since the start that are in the buffer */
    bool running;
};

static enum hrtimer_restart period_done(struct hrtimer *timer)
{
    struct mic_stream *s = container_of(timer, struct mic_stream, timer);

    if (!READ_ONCE(s->running))
        return HRTIMER_NORESTART;
    snd_pcm_period_elapsed(s->substream);
    hrtimer_forward_now(timer, s->period);
    return HRTIMER_RESTART;
}

/* PCM 0, the microphone pair: S16_LE for voiceinput_sound's own probe and test_aec-fe's default,
 * and the wider formats libaec-fe's aecfe_configure2 asks for the 32-bit capture voiceinput_sound's
 * CCaptureSoundMixLG always configures: S24_LE at 16 kHz where /proc/lg/base/chip_rev is up to
 * 0x299f (O22's 0x26a0), S32_LE at 48 kHz above it. PCMs 1 to 4, the echo canceller's references
 * (libaec-fe's preset table: spk, mix and se S32_LE, bt S16_LE, up to ten channels), silent */
static const struct snd_pcm_hardware mic_hw = {
    .info = SNDRV_PCM_INFO_INTERLEAVED | SNDRV_PCM_INFO_MMAP | SNDRV_PCM_INFO_MMAP_VALID |
            SNDRV_PCM_INFO_BLOCK_TRANSFER,
    .formats = SNDRV_PCM_FMTBIT_S16_LE | SNDRV_PCM_FMTBIT_S24_LE | SNDRV_PCM_FMTBIT_S32_LE,
    .rates = SNDRV_PCM_RATE_8000_48000,
    .rate_min = 8000,
    .rate_max = 48000,
    .channels_min = CHANNELS,
    .channels_max = CHANNELS,
    .buffer_bytes_max = BUFFER_MAX,
    .period_bytes_min = 64,
    .period_bytes_max = BUFFER_MAX / 2,
    .periods_min = 2,
    .periods_max = 1024,
};

static const struct snd_pcm_hardware ref_hw = {
    .info = SNDRV_PCM_INFO_INTERLEAVED | SNDRV_PCM_INFO_MMAP | SNDRV_PCM_INFO_MMAP_VALID |
            SNDRV_PCM_INFO_BLOCK_TRANSFER,
    .formats = SNDRV_PCM_FMTBIT_S16_LE | SNDRV_PCM_FMTBIT_S32_LE,
    .rates = SNDRV_PCM_RATE_8000_48000,
    .rate_min = 8000,
    .rate_max = 48000,
    .channels_min = 1,
    .channels_max = 10,
    .buffer_bytes_max = BUFFER_MAX,
    .period_bytes_min = 64,
    .period_bytes_max = BUFFER_MAX / 2,
    .periods_min = 2,
    .periods_max = 1024,
};

static int mic_open(struct snd_pcm_substream *substream)
{
    struct mic_stream *s = kzalloc(sizeof(*s), GFP_KERNEL);

    if (!s)
        return -ENOMEM;
    s->substream = substream;
    hrtimer_setup(&s->timer, period_done, CLOCK_MONOTONIC, HRTIMER_MODE_REL_SOFT);
    substream->runtime->private_data = s;
    substream->runtime->hw = substream->pcm->device ? ref_hw : mic_hw;
    return 0;
}

static int mic_close(struct snd_pcm_substream *substream)
{
    struct mic_stream *s = substream->runtime->private_data;

    hrtimer_cancel(&s->timer);
    kfree(s);
    return 0;
}

static int mic_prepare(struct snd_pcm_substream *substream)
{
    struct snd_pcm_runtime *runtime = substream->runtime;
    struct mic_stream *s = runtime->private_data;

    memset(runtime->dma_area, 0, runtime->dma_bytes); /* the silence the reads return */
    s->period = ns_to_ktime(div_u64((u64)runtime->period_size * NSEC_PER_SEC, runtime->rate));
    return 0;
}

static int mic_trigger(struct snd_pcm_substream *substream, int cmd)
{
    struct mic_stream *s = substream->runtime->private_data;

    switch (cmd) {
    case SNDRV_PCM_TRIGGER_START:
    case SNDRV_PCM_TRIGGER_RESUME:
        s->start = ktime_get();
        s->written = 0;
        WRITE_ONCE(s->running, true);
        hrtimer_start(&s->timer, s->period, HRTIMER_MODE_REL_SOFT);
        return 0;
    case SNDRV_PCM_TRIGGER_STOP:
    case SNDRV_PCM_TRIGGER_SUSPEND:
        WRITE_ONCE(s->running, false);
        hrtimer_try_to_cancel(&s->timer); /* atomic here: sync_stop waits for a running one */
        return 0;
    }
    return -EINVAL;
}

static int mic_sync_stop(struct snd_pcm_substream *substream)
{
    struct mic_stream *s = substream->runtime->private_data;

    hrtimer_cancel(&s->timer);
    return 0;
}

/* PCM 0's frames from s->written up to frames since the start: the air at their times, its 16
 * bits the top of the format's sample (S24_LE: the low three bytes of a 32-bit word) */
static void mic_write(struct mic_stream *s, struct snd_pcm_runtime *runtime, u64 frames)
{
    s64 start = ktime_to_ns(s->start);
    unsigned long flags;
    u64 k = s->written;

    if (frames > k + runtime->buffer_size)
        k = frames - runtime->buffer_size; /* the older ones are overwritten anyway */
    spin_lock_irqsave(&air_lock, flags);
    for (; k < frames; k++) {
        u64 slot = k;
        void *frame =
            runtime->dma_area + frames_to_bytes(runtime, do_div(slot, runtime->buffer_size));
        s64 at = start + div_u64(k * NSEC_PER_SEC, runtime->rate);
        unsigned int ch;

        for (ch = 0; ch < runtime->channels; ch++) {
            s32 v = air_sample(air, at, ch);

            if (runtime->format == SNDRV_PCM_FORMAT_S16_LE)
                ((s16 *)frame)[ch] = v;
            else if (runtime->format == SNDRV_PCM_FORMAT_S24_LE)
                ((s32 *)frame)[ch] = v * 256; /* sign-extended, as a codec gives it */
            else
                ((s32 *)frame)[ch] = v * 65536;
        }
    }
    spin_unlock_irqrestore(&air_lock, flags);
    s->written = frames;
}

/* the frames recorded since the start, at the stream's rate */
static snd_pcm_uframes_t mic_pointer(struct snd_pcm_substream *substream)
{
    struct snd_pcm_runtime *runtime = substream->runtime;
    struct mic_stream *s = runtime->private_data;
    u64 frames =
        div_u64((u64)ktime_to_ns(ktime_sub(ktime_get(), s->start)) * runtime->rate, NSEC_PER_SEC);

    if (!substream->pcm->device)
        mic_write(s, runtime, frames);
    return do_div(frames, runtime->buffer_size);
}

static const struct snd_pcm_ops ops = {
    .open = mic_open,
    .close = mic_close,
    .prepare = mic_prepare,
    .trigger = mic_trigger,
    .sync_stop = mic_sync_stop,
    .pointer = mic_pointer,
};

/* libaec-fe's preset table, in its device order */
static const char *const pcm_names[] = {NAME, "spk", "mix", "se", "bt"};

static int mic_probe(struct platform_device *pdev)
{
    struct snd_card *card;
    struct snd_pcm *pcm;
    unsigned int i;
    int err;

    /* -1: the first free index, after the virtio-sound card */
    err = snd_card_new(&pdev->dev, -1, NAME, THIS_MODULE, 0, &card);
    if (err)
        return err;
    strscpy(card->driver, NAME, sizeof(card->driver));
    strscpy(card->shortname, NAME, sizeof(card->shortname)); /* what snd_card_get_name reads */
    strscpy(card->longname, NAME " built-in microphone (lgemu)", sizeof(card->longname));
    for (i = 0; i < ARRAY_SIZE(pcm_names); i++) {
        err = snd_pcm_new(card, pcm_names[i], i, 0, 1, &pcm);
        if (err)
            goto fail;
        strscpy(pcm->name, pcm_names[i], sizeof(pcm->name));
        snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_CAPTURE, &ops);
        snd_pcm_set_managed_buffer_all(pcm, SNDRV_DMA_TYPE_VMALLOC, NULL, 0, 0);
    }
    err = snd_card_register(card);
    if (err)
        goto fail;
    platform_set_drvdata(pdev, card);
    pr_info("card %d: %s\n", card->number, card->shortname);
    return 0;

fail:
    snd_card_free(card);
    return err;
}

static void mic_remove(struct platform_device *pdev)
{
    snd_card_free(platform_get_drvdata(pdev));
}

static struct platform_driver driver = {
    .probe = mic_probe,
    .remove = mic_remove,
    .driver = {.name = "lgemu-mic"},
};

static struct platform_device *device;

int lgemu_mic_init(void)
{
    int err;

    if (!mic)
        return 0;
    air = vzalloc(sizeof(*air));
    if (!air)
        return -ENOMEM;
    err = platform_driver_register(&driver);
    if (err)
        goto no_driver;
    device = platform_device_register_simple("lgemu-mic", PLATFORM_DEVID_NONE, NULL, 0);
    if (IS_ERR(device)) {
        err = PTR_ERR(device);
        goto no_device;
    }
    if (!platform_get_drvdata(device)) { /* probed synchronously: it failed */
        platform_device_unregister(device);
        err = -ENODEV;
        goto no_device;
    }
    err = misc_register(&air_device);
    if (err) {
        platform_device_unregister(device);
        goto no_device;
    }
    return 0;

no_device:
    device = NULL;
    platform_driver_unregister(&driver);
no_driver:
    vfree(air);
    air = NULL;
    return err;
}

void lgemu_mic_exit(void)
{
    if (!device)
        return;
    misc_deregister(&air_device);
    platform_device_unregister(device);
    device = NULL;
    platform_driver_unregister(&driver);
    vfree(air);
    air = NULL;
}
