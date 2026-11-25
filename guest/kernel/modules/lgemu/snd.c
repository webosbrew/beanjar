// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The board's sound engine after the PCM: the master (OSD) volume and the speaker mute, as one
 * playback gain on the guest's sound card (kernel patch 0008, virtsnd_set_playback_gain).
 *
 * On an LG set PulseAudio's pcm_output (hw:0,6) feeds the SoC's sound engine, which applies the
 * OSD volume (LG's sound-engine function 9, "LGSE FN009": the level from the model's volume
 * curve, main byte dB + 127 and low byte 1/16 dB, 0x7F00 at volume 100, 0 silence) and the
 * speaker mute. The firmware sets them its own way per SoC; each stand-in that receives them
 * passes them here, so every target has the one volume model:
 *   - lg115x.ko: KADP's sound-engine call on the audio master, /dev/lg/amst0 (H13, M14, M16P);
 *   - mstar-utopia.ko: the AUDIO module's ADVSOUND parameter of the kernel-mode Utopia (M2);
 *   - rtk.ko: the audio CPU's LGSE FN009 RPC (K5LP);
 *   - the card's own LG control elements, when snd_ctls=1 (webOS 5 and 6's audio adaptation
 *     layer writes "LGSE Var Fn009" [level, volume] and "Mute Output" [output, mute] on hw:0;
 *     W18R's audio HAL shim writes them too).
 * lgemu_snd_osd_level() and lgemu_snd_speaker_mute() are the stand-ins' entry points; what the
 * firmware selects for the other outputs is kept below (lgemu_snd_out_set, /proc/lgemu/sound_out).
 *
 * The host decodes the media streams' sound itself (include/webos-media.h), past this card, so the
 * same gain goes to it as well: /dev/lgemu-sound gives a line of text at each change, "level=0x7f00
 * mute=0 gain=65536" (the level as the firmware set it, the speaker mute, the gain in Q16 the card
 * applies, 0 when muted). A file's first read has the state as it is; the next waits for a change.
 * The initramfs's pump (guest/initramfs/src/virtio-sound.c) copies the lines to the webos.sound
 * port, and the viewer applies the last one to the media audio.
 */
#define pr_fmt(fmt) KBUILD_MODNAME ": snd: " fmt
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/fs.h>
#include <linux/miscdevice.h>
#include <linux/mutex.h>
#include <linux/poll.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include <sound/core.h>
#include <sound/control.h>
#include "lgemu.h"
#include "lgemu-drv.h"

void virtsnd_set_playback_gain(u32 q16); /* kernel patch 0008 (sound/virtio) */

static bool snd_ctls;
module_param(snd_ctls, bool, 0444);
MODULE_PARM_DESC(snd_ctls, "Add the LG control elements (LGSE Var Fn009, Mute Output) to card 0");

#define LEVEL_UNITY 0x7F00u

static DEFINE_MUTEX(lock);
static u32 osd_level = LEVEL_UNITY; /* unity until the firmware sets its volume */
static bool speaker_mute;
static u32 fn009[2] = {LEVEL_UNITY, 100}, mute_output[2];
static struct snd_card *card;
/* the gain the card applies, and the change it came with for /dev/lgemu-sound (1: the default) */
static u32 gain = 0x10000, changes = 1;
static DECLARE_WAIT_QUEUE_HEAD(readers);

/* 10^(-d/20) and 10^(-f/320) in Q30: whole dB and sixteenths */
static const u32 db_q30[20] = {
    1073741824, 956973408, 852903448, 760150998, 677485290, 603809400, 538145694,
    479622855,  427464319, 380977976, 339546978, 302621563, 269711752, 240380852,
    214239660,  190941298, 170176611, 151670064, 135176087, 120475814,
};
static const u32 fine_q30[16] = {
    1073741824, 1066043361, 1058400094, 1050811628, 1043277569, 1035797527, 1028371116, 1020997950,
    1013677647, 1006409830, 999194121,  992030146,  984917536,  977855921,  970844937,  963884219,
};

/* sixteenths of a dB below 0 dB of a level (0 for 0x7F00 and above) */
static u32 attenuation(u32 level)
{
    int s = ((int)((level >> 8) & 0xff) - 127) * 16 + (int)(level & 0xf);
    return s >= 0 ? 0 : -s;
}

static u32 gain_q16(u32 level)
{
    u32 n = attenuation(level), d = n / 16 % 20, decades = n / 320;
    u64 g = (u64)db_q30[d] * fine_q30[n % 16] >> 30;

    while (decades-- && g)
        g = div_u64(g, 10);
    return (u32)(g >> 14);
}

static void apply(void)
{
    bool mute = speaker_mute || !(osd_level & 0xffff);
    u32 n = attenuation(osd_level);

    gain = mute ? 0 : gain_q16(osd_level);
    virtsnd_set_playback_gain(gain);
    WRITE_ONCE(changes, changes + 1);
    wake_up_interruptible(&readers);
    pr_info("level 0x%04x (-%u.%04u dB)%s\n", osd_level & 0xffff, n / 16, n % 16 * 625,
            mute ? ", muted" : "");
}

void lgemu_snd_osd_level(u32 level)
{
    mutex_lock(&lock);
    if (level != osd_level) {
        osd_level = level;
        apply();
    }
    mutex_unlock(&lock);
}
EXPORT_SYMBOL_GPL(lgemu_snd_osd_level);

void lgemu_snd_speaker_mute(bool on)
{
    mutex_lock(&lock);
    if (on != speaker_mute) {
        speaker_mute = on;
        apply();
    }
    mutex_unlock(&lock);
}
EXPORT_SYMBOL_GPL(lgemu_snd_speaker_mute);

/* ---- the card's LG control elements ---- */

static int ctl_info(struct snd_kcontrol *k, struct snd_ctl_elem_info *info)
{
    info->type = SNDRV_CTL_ELEM_TYPE_INTEGER;
    info->count = 2;
    info->value.integer.min = 0;
    info->value.integer.max = 0x7fffffff;
    return 0;
}

static int ctl_get(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
    u32 *vals = (u32 *)k->private_value;

    mutex_lock(&lock);
    v->value.integer.value[0] = vals[0];
    v->value.integer.value[1] = vals[1];
    mutex_unlock(&lock);
    return 0;
}

static int ctl_put(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
    u32 *vals = (u32 *)k->private_value;
    int changed;

    mutex_lock(&lock);
    changed = vals[0] != v->value.integer.value[0] || vals[1] != v->value.integer.value[1];
    vals[0] = v->value.integer.value[0];
    vals[1] = v->value.integer.value[1];
    mutex_unlock(&lock);
    if (vals == fn009)
        lgemu_snd_osd_level(vals[0]);
    else
        lgemu_snd_speaker_mute(vals[1] != 0);
    return changed;
}

/* ---- the sound output's selection: kept, readable, never acted on ----
 *
 * The card has one output, the speaker PCM; what the firmware selects for the others (optical,
 * ARC, eARC, the digital sound mode) is kept as written, under the name of the call it came
 * through, and listed in /proc/lgemu/sound_out: a "generation N" line that counts the changes,
 * "writes N" every write (a setting the firmware applies again with the same values counts there),
 * "open=" the outputs open and "connected=" their connections (OUTPUT:A:B, from the Sndout Open,
 * Close, Connect and Disconnect events), then one "NAME=V0 V1 ..." line a name in order of the
 * first write. Two ways in:
 *   - the card's Sndout control elements (snd_ctls=1), written by libumi's setControlElement
 *     (webOS 5 to 24) or nyxSoundOut's setAlsaControl (queue and later): names and values as
 *     the firmware writes them, readable back with the element's get;
 *   - lgemu_snd_out_set() from the stand-ins of the HAL-based lines (rtk.ko, mstar-utopia.ko,
 *     lg115x.ko), under a name of their own for the HAL or KADP call.
 * The elements are the union of the firmwares' sets (docs/research/tv-sound-outputs.md, the
 * release x SoC matrix): a firmware writes the names it knows. The Outputtype setters write a
 * 5-integer struct (codec, mix, channels, ...), hence OUT_VALUES 5; a shorter write leaves the
 * rest 0. "Sndout Spk Output" is the amplifier library's (W21A libaudio-adaptation-layer-amplifier,
 * AALAmpAlsaControl::setAmpParam): every amplifier reset, at start-up and in Prepare Resume,
 * writes the speaker output's channel count there (2 above two amplifiers, else 1), and a
 * missing element fails the reset, so audiooutputd answers prepareResume with error 20. */
#define OUT_VALUES 5
#define OUT_MAX    48
static const char *const kept_names[] = {
    "Mute Input",
    "Sndout MainAudio Output",
    "Sndout Open",
    "Sndout Close",
    "Sndout Connect",
    "Sndout Disconnect",
    "Sndout Spdif OutputType",
    "Sndout OPTIC Outputtype",
    "Sndout ARC Outputtype",
    "Sndout EARC Outputtype",
    "Sndout WOWCAST Outputtype",
    "Sndout ARC OnOff",
    "Sndout EARC OnOff",
    "Sndout WOWCAST OnOff",
    "Sndout EARC MS12 SP50 OnOff",
    "Sndout eAC3 ATMOS Encode OnOff",
    "Sndout Spdif CopyProtectionInfo",
    "Sndout Spdif CategoryCode",
    "Sndout Optic LG",
    "Sndout Optic LightOnOff",
    "Sndout Spk Output",
    "Sndout Spk Max Output Channel",
    "Sndout Spk Sampling Frequency",
    "Sndout Capture Output Resampling",
    "Sndout Capture Input TimeClock",
    "Sndout Center Extraction OnOff",
    "Decoder Spk Configuration",
    "Input Downmix Enable",
    "Adec Dolby OTTMode",
};

struct out_entry {
    char name[SNDRV_CTL_ELEM_ID_NAME_MAXLEN];
    long v[OUT_VALUES];
    bool written;
    u32 order; /* of the first write */
};
static struct out_entry outs[OUT_MAX];
static size_t n_outs;
static u32 generation, writes, n_written;
static struct proc_dir_entry *proc_dir;

/* "Sndout Open" and "Close" [output] and "Connect" and "Disconnect" [output, input, ...] are
 * events, not states: the outputs open and their connections are kept as well, in order. */
#define OPEN_MAX    8
#define CONN_MAX    24
#define CONN_VALUES 3
static long opened[OPEN_MAX], conns[CONN_MAX][CONN_VALUES];
static size_t n_opened, n_conns;

/* drop element i of an array of n elements of size bytes */
static void drop(void *base, size_t *n, size_t i, size_t size)
{
    memmove((char *)base + i * size, (char *)base + (i + 1) * size, (*n - i - 1) * size);
    (*n)--;
}

/* the routing events' effect on opened and conns: whether they changed; under lock */
static bool out_route(const char *name, const long *v)
{
    bool open = !strcmp(name, "Sndout Open"), conn = !strcmp(name, "Sndout Connect");
    size_t i;

    if (open || !strcmp(name, "Sndout Close")) {
        for (i = 0; i < n_opened && opened[i] != v[0]; i++)
            ;
        if (i < n_opened && !open)
            drop(opened, &n_opened, i, sizeof(opened[0]));
        else if (i == n_opened && open && n_opened < OPEN_MAX)
            opened[n_opened++] = v[0];
        else
            return false;
        return true;
    } else if (conn || !strcmp(name, "Sndout Disconnect")) {
        for (i = 0; i < n_conns && memcmp(conns[i], v, sizeof(conns[0])); i++)
            ;
        if (i < n_conns && !conn)
            drop(conns, &n_conns, i, sizeof(conns[0]));
        else if (i == n_conns && conn && n_conns < CONN_MAX)
            memcpy(conns[n_conns++], v, sizeof(conns[0]));
        else
            return false;
        return true;
    }
    return false;
}

/* the entry of name, a new one if there is room; under lock */
static struct out_entry *out_find(const char *name)
{
    for (size_t i = 0; i < n_outs; i++)
        if (!strcmp(outs[i].name, name))
            return &outs[i];
    if (n_outs == OUT_MAX)
        return NULL;
    strscpy(outs[n_outs].name, name, sizeof(outs[0].name));
    return &outs[n_outs++];
}

/* store n values of e (the rest 0); whether they changed; under lock */
static bool out_store(struct out_entry *e, const long *vals, unsigned int n)
{
    long v[OUT_VALUES] = {0};
    bool changed;

    memcpy(v, vals, min_t(unsigned int, n, OUT_VALUES) * sizeof(v[0]));
    writes++;
    changed = !e->written || memcmp(e->v, v, sizeof(v));
    memcpy(e->v, v, sizeof(v));
    if (!e->written) {
        e->written = true;
        e->order = n_written++;
    }
    if (out_route(e->name, v))
        changed = true;
    if (changed) {
        generation++;
        pr_info("out: %s = %ld %ld %ld %ld %ld\n", e->name, v[0], v[1], v[2], v[3], v[4]);
    }
    return changed;
}

void lgemu_snd_out_set(const char *name, const long *vals, unsigned int n)
{
    struct out_entry *e;

    mutex_lock(&lock);
    e = out_find(name);
    if (e)
        out_store(e, vals, n);
    else
        pr_warn_once("out: no room for %s\n", name);
    mutex_unlock(&lock);
}
EXPORT_SYMBOL_GPL(lgemu_snd_out_set);

static int sound_out_show(struct seq_file *m, void *unused)
{
    mutex_lock(&lock);
    seq_printf(m, "generation %u\nwrites %u\nopen=", generation, writes);
    for (size_t i = 0; i < n_opened; i++)
        seq_printf(m, "%s%ld", i ? " " : "", opened[i]);
    seq_puts(m, "\nconnected=");
    for (size_t i = 0; i < n_conns; i++)
        seq_printf(m, "%s%ld:%ld:%ld", i ? " " : "", conns[i][0], conns[i][1], conns[i][2]);
    seq_putc(m, '\n');
    for (u32 k = 0; k < n_written; k++)
        for (size_t i = 0; i < n_outs; i++) {
            struct out_entry *e = &outs[i];

            if (!e->written || e->order != k)
                continue;
            seq_printf(m, "%s=%ld %ld %ld %ld %ld\n", e->name, e->v[0], e->v[1], e->v[2], e->v[3],
                       e->v[4]);
        }
    mutex_unlock(&lock);
    return 0;
}

static int kept_info(struct snd_kcontrol *k, struct snd_ctl_elem_info *info)
{
    info->type = SNDRV_CTL_ELEM_TYPE_INTEGER;
    info->count = OUT_VALUES;
    info->value.integer.min = 0;
    info->value.integer.max = 0x7fffffff;
    return 0;
}

static int kept_get(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
    mutex_lock(&lock);
    memcpy(v->value.integer.value, outs[k->private_value].v, sizeof(outs[0].v));
    mutex_unlock(&lock);
    return 0;
}

static int kept_put(struct snd_kcontrol *k, struct snd_ctl_elem_value *v)
{
    bool changed;

    mutex_lock(&lock);
    changed = out_store(&outs[k->private_value], v->value.integer.value, OUT_VALUES);
    mutex_unlock(&lock);
    return changed;
}

static struct snd_kcontrol_new ctls[] = {
    {.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
     .name = "LGSE Var Fn009",
     .info = ctl_info,
     .get = ctl_get,
     .put = ctl_put,
     .private_value = (unsigned long)fn009},
    {.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
     .name = "Mute Output",
     .info = ctl_info,
     .get = ctl_get,
     .put = ctl_put,
     .private_value = (unsigned long)mute_output},
};

static int add_kept(void)
{
    for (size_t i = 0; i < ARRAY_SIZE(kept_names); i++) {
        struct snd_kcontrol_new k = {.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
                                     .name = kept_names[i],
                                     .info = kept_info,
                                     .get = kept_get,
                                     .put = kept_put};
        struct out_entry *e;
        int err;

        mutex_lock(&lock);
        e = out_find(kept_names[i]);
        mutex_unlock(&lock);
        if (!e)
            return -ENOSPC;
        k.private_value = e - outs;
        err = snd_ctl_add(card, snd_ctl_new1(&k, NULL));

        if (err)
            return err;
    }
    return 0;
}

/* ---- /dev/lgemu-sound: the gain for the host's media audio ---- */

/* each open file: the change it read last (0: none yet) */
static int sound_open(struct inode *inode, struct file *file)
{
    u32 *seen = kzalloc(sizeof(*seen), GFP_KERNEL);

    if (!seen)
        return -ENOMEM;
    file->private_data = seen;
    return stream_open(inode, file);
}

static int sound_release(struct inode *inode, struct file *file)
{
    kfree(file->private_data);
    return 0;
}

static ssize_t sound_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    u32 *seen = file->private_data;
    char line[64];
    int len;

    if (file->f_flags & O_NONBLOCK) {
        if (READ_ONCE(changes) == *seen)
            return -EAGAIN;
    } else if (wait_event_interruptible(readers, READ_ONCE(changes) != *seen)) {
        return -ERESTARTSYS;
    }
    mutex_lock(&lock);
    len = scnprintf(line, sizeof(line), "level=0x%04x mute=%d gain=%u\n", osd_level & 0xffff,
                    speaker_mute, gain);
    if (n < len) {
        mutex_unlock(&lock);
        return -EINVAL;
    }
    *seen = changes;
    mutex_unlock(&lock);
    return copy_to_user(buf, line, len) ? -EFAULT : len;
}

static __poll_t sound_poll(struct file *file, poll_table *wait)
{
    poll_wait(file, &readers, wait);
    return READ_ONCE(changes) != *(u32 *)file->private_data ? EPOLLIN | EPOLLRDNORM : 0;
}

static const struct file_operations sound_fops = {
    .owner = THIS_MODULE,
    .open = sound_open,
    .release = sound_release,
    .read = sound_read,
    .poll = sound_poll,
};

static struct miscdevice sound_device = {
    .minor = MISC_DYNAMIC_MINOR,
    .name = "lgemu-sound",
    .fops = &sound_fops,
    .mode = 0400,
};
static bool sound_registered;

int lgemu_snd_init(void)
{
    int err;

    proc_dir = proc_mkdir("lgemu", NULL);
    if (!proc_dir || !proc_create_single("sound_out", 0444, proc_dir, sound_out_show))
        pr_err("cannot add /proc/lgemu/sound_out\n");
    err = misc_register(&sound_device);
    if (err)
        pr_err("cannot add /dev/lgemu-sound (%d)\n", err);
    sound_registered = !err;
    if (!snd_ctls)
        return 0;
    card = snd_card_ref(0);
    if (!card) {
        pr_info("no sound card: no LG control elements\n");
        return 0;
    }
    for (size_t i = 0; i < ARRAY_SIZE(ctls); i++) {
        err = snd_ctl_add(card, snd_ctl_new1(&ctls[i], NULL));
        if (err) {
            pr_err("cannot add %s (%d)\n", ctls[i].name, err);
            snd_card_unref(card);
            card = NULL;
            return 0;
        }
    }
    err = add_kept();
    if (err)
        pr_err("cannot add the sound output's elements (%d)\n", err);
    pr_info("card 0: LG control elements %s, %s and %zu of the sound output\n", ctls[0].name,
            ctls[1].name, ARRAY_SIZE(kept_names));
    return 0;
}

void lgemu_snd_exit(void)
{
    if (card) {
        for (size_t i = 0; i < ARRAY_SIZE(ctls); i++) {
            struct snd_ctl_elem_id id = {.iface = ctls[i].iface};

            strscpy(id.name, ctls[i].name, sizeof(id.name));
            snd_ctl_remove_id(card, &id);
        }
        for (size_t i = 0; i < ARRAY_SIZE(kept_names); i++) {
            struct snd_ctl_elem_id id = {.iface = SNDRV_CTL_ELEM_IFACE_MIXER};

            strscpy(id.name, kept_names[i], sizeof(id.name));
            snd_ctl_remove_id(card, &id);
        }
        snd_card_unref(card);
        card = NULL;
    }
    if (proc_dir)
        proc_remove(proc_dir);
    proc_dir = NULL;
    if (sound_registered)
        misc_deregister(&sound_device);
    sound_registered = false;
    virtsnd_set_playback_gain(0x10000);
}
