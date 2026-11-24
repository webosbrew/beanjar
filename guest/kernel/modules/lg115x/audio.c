// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#include <linux/compat.h>
#include <linux/devmem_window.h>
#include <linux/ctype.h>
#include <linux/fs.h>
#include <linux/jhash.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/spi/spidev.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>
#include <linux/mutex.h>
#include <linux/math64.h>
#include <linux/mm.h>
#include <linux/timekeeping.h>
#include "lgemu-drv.h"
#include "lgemu-i2c.h"

#include "lg115x.h"

/* ---- the audio master: /dev/lg/amst0 ---- */

#define AUD_MAJOR        143
#define AUD_MASTER_MINOR 4 /* adec0, aenc0, acap0, aren0, amst0 */
#define AUD_MASTER_MAGIC 'M'
#define AUD_KEPT_MAX     4
#define AUD_KEPT_SIZE    64
/* AUD_IOWR_READ_AND_WRITE_REG, nr 190 in both generations: LX_AUD_REG_INFO_T (mode, address, the
 * data read, the data to write) */
#define AUD_IOWR_REG _IOWR(AUD_MASTER_MAGIC, 190, struct lx_aud_reg)

struct lx_aud_reg {
    u32 mode, addr, readdata, writedata;
};

/* LX_AUD_EVENT_TYPE_T: the device, its event mask */
struct lx_aud_event {
    u32 dev, msg;
};

/* LX_AUD_DEV_INDEX_CTX_T from a 32-bit process: device type, device, output type, index, context */
struct lx_aud_index_ctx {
    u32 type, dev, out_type, index, ctx;
};

#define AUD_MAS0_2016 23 /* LX_AUD_DEV_MAS0 of the 2016 audio_kapi.h (2014: 20) */

/* AUD_MASTER_IOW_SET_SE_FN's LX_AUD_MASTER_PARAM_SE_FN_T (audio_kapi_master.h, both generations):
 * the sound-engine function, its data option, variable, access, a user pointer to the parameters
 * and their count. Function 9's variables are the OSD (master) volume: {level, volume} */
struct lx_aud_se_fn {
    u32 fn, data, var, access, param, count;
};
#define AUD_SE_FN_009    9
#define AUD_SE_DATA_VARS 1
#define AUD_SE_WRITE     0
/* AUD_MASTER_IOW_SET_MUTE, nr 1 in both: LX_AUD_MASTER_CONFIG_MUTE_T, the output (0: the speaker)
 * and the mute */
struct lx_aud_mute {
    u32 output, mute;
};
#define AUD_IOW_SET_MUTE _IOW(AUD_MASTER_MAGIC, 1, struct lx_aud_mute)
#define AUD_OUT_SPEAKER  0

static char *amst;
module_param(amst, charp, 0444);
MODULE_PARM_DESC(
    amst, "the audio master's ABI, kdrv:event:dev[:set:get...]: AUD_MASTER_IOR_GET_KDRV_HANDLE's "
          "and AUD_MASTER_IORW_GET_EVENT's numbers, LX_AUD_DEV_MAS0, then pairs of a setting's "
          "and its query's numbers whose value the driver keeps (H13 and M14 17:41:20, M16P "
          "27:71:23:66:67); unset: no /dev/lg/amst0");

static unsigned int amst_se;
module_param(amst_se, uint, 0444);
MODULE_PARM_DESC(amst_se,
                 "AUD_MASTER_IOW_SET_SE_FN's number (H13 and M14 16, M16P 60): the sound engine's "
                 "OSD volume (function 9) goes to lgemu's playback gain; 0: not decoded");

/* The S/PDIF output's mode: KADP_AUDIO_SetSpdifOutput's _IOW(AUD_MASTER_MAGIC, 3, int) on WT1H
 * (HAL_AUDIO_SPDIF_SetOutputType passes it; tvservice sets HAL mode 1 for pcm, 2 for auto). The
 * int is kept as the sound output's "AMST SpdifOutput" in lgemu's /proc/lgemu/sound_out, beside
 * the mutes of the outputs other than the speaker ("AMST Mute N"). */
static unsigned int amst_spdif;
module_param(amst_spdif, uint, 0444);
MODULE_PARM_DESC(amst_spdif, "KADP_AUDIO_SetSpdifOutput's number (H13 3): the S/PDIF mode is kept "
                             "in lgemu's sound output values; 0: not decoded");

static u32 aud_kdrv_nr, aud_event_nr, aud_master_dev;
/* the settings kept as set, for their queries: the numbers, the value (zero before) */
static struct {
    u32 set, get, size;
    u8 value[AUD_KEPT_SIZE];
} aud_kept[AUD_KEPT_MAX];
static unsigned int aud_kept_count;
static DEFINE_MUTEX(aud_lock);
static struct lx_seen aud_seen = LX_SEEN_INIT(aud_seen);

/* KDRV_AUDIO_IoctlMaster: the master's handle (its device, which KADP keeps and passes back), no
 * event (GET_EVENT polls, it does not wait), and every setting (volume, mute, outputs, the sound
 * engine, the DSP image download, the MS12 version) accepted: no DSP is modelled. What the sound
 * engine does after the PCM is lgemu's playback gain (lgemu snd.c): the OSD volume (sound-engine
 * function 9's variables, amst_se) and the speaker output's mute are passed there. Any other
 * query is refused. Each command is logged once. */
static long aud_master_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *uarg = compat_ptr(arg);
    struct lx_aud_event ev;
    struct lx_aud_reg reg;
    unsigned int i;
    long ret;

    if (_IOC_TYPE(cmd) != AUD_MASTER_MAGIC)
        return -ENOTTY;
    if (cmd == AUD_IOWR_REG) { /* the DSP's registers: a read gives 0, a write is dropped */
        if (copy_from_user(&reg, uarg, sizeof(reg)))
            return -EFAULT;
        reg.readdata = 0;
        return copy_to_user(uarg, &reg, sizeof(reg)) ? -EFAULT : 0;
    }
    for (i = 0; i < aud_kept_count; i++) {
        unsigned int size = _IOC_SIZE(cmd);

        if (size > AUD_KEPT_SIZE ||
            (_IOC_NR(cmd) != aud_kept[i].set && _IOC_NR(cmd) != aud_kept[i].get))
            continue;
        mutex_lock(&aud_lock);
        if (_IOC_NR(cmd) == aud_kept[i].set && _IOC_DIR(cmd) == _IOC_WRITE)
            ret = copy_from_user(aud_kept[i].value, uarg, size) ? -EFAULT : 0;
        else if (_IOC_NR(cmd) == aud_kept[i].get && _IOC_DIR(cmd) & _IOC_READ)
            ret = copy_to_user(uarg, aud_kept[i].value, size) ? -EFAULT : 0;
        else
            ret = -ENOTTY;
        mutex_unlock(&aud_lock);
        return ret;
    }
    if (amst_se && cmd == _IOW(AUD_MASTER_MAGIC, amst_se, struct lx_aud_se_fn)) {
        struct lx_aud_se_fn se;
        u32 level;

        if (copy_from_user(&se, uarg, sizeof(se)))
            return -EFAULT;
        if (se.fn == AUD_SE_FN_009 && se.data == AUD_SE_DATA_VARS && se.access == AUD_SE_WRITE &&
            se.count >= 1) {
            if (get_user(level, (u32 __user *)compat_ptr(se.param)))
                return -EFAULT;
            lgemu_snd_osd_level(level & 0xffff);
        }
    } else if (cmd == AUD_IOW_SET_MUTE) {
        struct lx_aud_mute m;

        if (copy_from_user(&m, uarg, sizeof(m)))
            return -EFAULT;
        if (m.output == AUD_OUT_SPEAKER) {
            lgemu_snd_speaker_mute(m.mute != 0);
        } else {
            char name[24];

            snprintf(name, sizeof(name), "AMST Mute %u", m.output);
            lgemu_snd_out_set(name, (long[]){m.mute}, 1);
        }
    } else if (amst_spdif && cmd == _IOW(AUD_MASTER_MAGIC, amst_spdif, int)) {
        int mode;

        if (get_user(mode, (int __user *)uarg))
            return -EFAULT;
        lgemu_snd_out_set("AMST SpdifOutput", (long[]){mode}, 1);
    }
    if (cmd == _IOR(AUD_MASTER_MAGIC, aud_kdrv_nr, u32))
        return put_user(aud_master_dev, (u32 __user *)uarg);
    if (cmd == _IOWR(AUD_MASTER_MAGIC, aud_event_nr, struct lx_aud_event)) {
        if (copy_from_user(&ev, uarg, sizeof(ev)))
            return -EFAULT;
        ev = (struct lx_aud_event){0}; /* LX_AUD_DEV_NONE, LX_AUD_EVENT_MASK_NONE */
        return copy_to_user(uarg, &ev, sizeof(ev)) ? -EFAULT : 0;
    }
    return lx_seen_log(&aud_seen, "amst", cmd, _IOC_DIR(cmd) & _IOC_READ ? -EPERM : 0);
}

static int aud_master_open(struct inode *inode, struct file *file)
{
    return 0;
}

/* the master's events: none comes */
static __poll_t aud_master_poll(struct file *file, struct poll_table_struct *pt)
{
    return 0;
}

static const struct file_operations aud_master_fops = {
    .owner = THIS_MODULE,
    .open = aud_master_open,
    .poll = aud_master_poll,
    .unlocked_ioctl = aud_master_ioctl,
    .compat_ioctl = aud_master_ioctl,
};

static struct lgemu_cdev aud_master_cdev;

void lx_aud_remove(void)
{
    lgemu_cdev_del(&aud_master_cdev);
}

int lx_aud_add(void)
{
    u32 v[3 + 2 * AUD_KEPT_MAX];
    int n, err;

    if (!amst)
        return 0;
    for (n = ARRAY_SIZE(v); n >= 3 && lgemu_parse_u32s(amst, 0, v, n); n -= 2)
        ;
    if (n < 3 || !v[0] || v[0] >= 256 || v[1] >= 256 || v[0] == v[1] || !v[2]) {
        pr_err(
            "amst: amst is kdrv:event:dev[:set:get...], ioctl numbers and the master's device\n");
        return -EINVAL;
    }
    aud_kdrv_nr = v[0];
    aud_event_nr = v[1];
    aud_master_dev = v[2];
    for (aud_kept_count = 0; 3 + 2 * aud_kept_count < n; aud_kept_count++) {
        aud_kept[aud_kept_count].set = v[3 + 2 * aud_kept_count];
        aud_kept[aud_kept_count].get = v[4 + 2 * aud_kept_count];
    }
    err =
        lgemu_cdev_add(&aud_master_cdev, AUD_MAJOR, AUD_MASTER_MINOR, 1, "amst", &aud_master_fops);
    if (!err)
        pr_info("amst: amst0, the handle at nr %u (device %u), events at nr %u, no DSP\n",
                aud_kdrv_nr, aud_master_dev, aud_event_nr);
    return err;
}

/* ---- the audio decoders, renderers and analog audio decoder ---- */

/* The audio driver's modules other than the master (audio_drv_decoder.c, audio_drv_renderer.c,
 * audio_kapi_decoder.h, audio_kapi_renderer.h, 2014 and 2016). A decoder or renderer file is one
 * of the driver's devices once it has a handle: the 2014 driver allocates the first free one at
 * open, the 2016 driver at SET_INDEX_CTX (its index, or 0xff for the first free); here the first
 * of SET_INDEX_CTX and GET_KDRV_HANDLE allocates, which serves both. The devices are the
 * master's numbering (LX_AUD_DEV_T): DEC0-1 from 1, REN0-11 the twelve before MAS0. */
#define AUD_DEC_MINOR     0
#define AUD_REN_MINOR     3
#define AUD_AAAD_MINOR    5    /* the 2016 driver's analog decoder, aaad0 */
#define AUD_DEV_INDEX_ANY 0xff /* DEFAULT_DEV_INDEX */

struct aud_kind {
    const char *name;
    char magic;
    unsigned int count; /* DEV_DEC_NUM, DEV_REN_NUM */
    u32 first;          /* LX_AUD_DEV_DEC0, LX_AUD_DEV_REN0 */
    unsigned long used;
    struct lx_seen seen;
};

static struct aud_kind aud_dec = {
    .name = "adec", .magic = 'D', .count = 2, .first = 1, .seen = LX_SEEN_INIT(aud_dec.seen)};
static struct aud_kind aud_ren = {
    .name = "aren", .magic = 'R', .count = 12, .seen = LX_SEEN_INIT(aud_ren.seen)};
static DEFINE_MUTEX(aud_dev_lock);

/* The file's device (AUDIO_AllocDev): the index asked for, or the first free; 0 when taken */
static u32 aud_alloc(struct aud_kind *k, struct file *file, u32 index)
{
    u32 dev = (uintptr_t)file->private_data;

    mutex_lock(&aud_dev_lock);
    if (!dev) {
        if (index == AUD_DEV_INDEX_ANY)
            index = find_first_zero_bit(&k->used, k->count);
        if (index < k->count && !test_and_set_bit(index, &k->used)) {
            dev = k->first + index;
            file->private_data = (void *)(uintptr_t)dev;
        }
    }
    mutex_unlock(&aud_dev_lock);
    return dev;
}

static void aud_free(struct aud_kind *k, struct file *file)
{
    u32 dev = (uintptr_t)file->private_data;

    mutex_lock(&aud_dev_lock);
    if (dev)
        clear_bit(dev - k->first, &k->used);
    file->private_data = NULL;
    mutex_unlock(&aud_dev_lock);
}

/* LX_AUD_DECODED_INFO_T as the decoder's open leaves it (no stream decoded yet): the ES info's
 * codec LX_AUD_CODEC_UNKNOWN, its mode LX_AUD_DECODER_MODE_UNKNOWN, the sampling frequency
 * LX_AUD_SAMPLING_FREQ_NONE, the rest 0 */
#define AUD_DECODER_MODE_UNKNOWN 5
struct lx_aud_decoded_info {
    u32 format, mode, es[3], codec, freq, bitrate, channels, bits;
};
static_assert(sizeof(struct lx_aud_decoded_info) == 40);

/* KDRV_AUDIO_IoctlDecoder: the handle; the queries a decoder with no stream answers (nothing
 * decodable, the open's info, the TP input's zeroed status, no delay, acmod 0); every setting
 * (parameters, modes, start, stop, feeds) accepted with no DSP behind it. */
static long aud_dec_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *uarg = compat_ptr(arg);
    struct lx_aud_decoded_info info = {.mode = AUD_DECODER_MODE_UNKNOWN};
    u32 index, dev;
    long ret;

    if (_IOC_TYPE(cmd) != aud_dec.magic)
        return -ENOTTY;
    switch (cmd) {
    case _IOR('D', 2, u32): /* GET_KDRV_HANDLE */
        dev = aud_alloc(&aud_dec, file, AUD_DEV_INDEX_ANY);
        ret = !dev ? -EPERM : put_user(dev, (u32 __user *)uarg);
        break;
    case _IOW('D', 14, struct lx_aud_index_ctx): /* SET_INDEX_CTX: 2016 nr 14, 2014 nr 15 */
    case _IOW('D', 15, struct lx_aud_index_ctx):
        ret = get_user(index, (u32 __user *)(uarg + offsetof(struct lx_aud_index_ctx, index)));
        if (!ret && !aud_alloc(&aud_dec, file, index))
            ret = -EPERM;
        break;
    case _IO('D', 10): /* CLOSE_DEVICE */
        aud_free(&aud_dec, file);
        ret = 0;
        break;
    case _IOW('D', 8, u32): /* IS_DECODABLE, an _IOW the driver answers */
        ret = put_user(0, (u32 __user *)uarg);
        break;
    case _IOR('D', 9, struct lx_aud_decoded_info): /* GET_INFO */
        ret = copy_to_user(uarg, &info, sizeof(info)) ? -EFAULT : 0;
        break;
    case _IOR('D', 12, struct lx_aud_decoded_info): /* GET_STATUS, 40 bytes too */
    case _IOR('D', 16, u32):                        /* GET_DELAY (2014) */
    case _IOR('D', 18, u32[6]):                     /* GET_ES_INFO (2016): acmod, reserved */
        ret = clear_user(uarg, _IOC_SIZE(cmd)) ? -EFAULT : 0;
        break;
    default:
        ret = _IOC_DIR(cmd) & _IOC_READ ? -EPERM : 0;
    }
    return lx_seen_log(&aud_dec.seen, aud_dec.name, cmd, ret);
}

/* KDRV_AUDIO_IoctlRenderer: the handle, no delay, the presented PTS (a DSP register, 0); the
 * rendered status is not answered (the stock reads a renderer's feed buffer, and none is kept
 * here); every setting accepted. */
static long aud_ren_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *uarg = compat_ptr(arg);
    u32 index, dev;
    long ret;

    if (_IOC_TYPE(cmd) != aud_ren.magic)
        return -ENOTTY;
    switch (cmd) {
    case _IOR('R', 16, u32): /* GET_KDRV_HANDLE */
        dev = aud_alloc(&aud_ren, file, AUD_DEV_INDEX_ANY);
        ret = !dev ? -EPERM : put_user(dev, (u32 __user *)uarg);
        break;
    case _IOW('R', 22, struct lx_aud_index_ctx): /* SET_INDEX_CTX */
        ret = get_user(index, (u32 __user *)(uarg + offsetof(struct lx_aud_index_ctx, index)));
        if (!ret && !aud_alloc(&aud_ren, file, index))
            ret = -EPERM;
        break;
    case _IO('R', 17): /* CLOSE_DEVICE */
        aud_free(&aud_ren, file);
        ret = 0;
        break;
    case _IOR('R', 26, u32): /* GET_DELAY (2014) */
    case _IOR('R', 27, u32): /* GET_PRESENTED_PTS */
        ret = put_user(0, (u32 __user *)uarg);
        break;
    default:
        ret = _IOC_DIR(cmd) & _IOC_READ ? -EPERM : 0;
    }
    return lx_seen_log(&aud_ren.seen, aud_ren.name, cmd, ret);
}

static int aud_dec_release(struct inode *inode, struct file *file)
{
    aud_free(&aud_dec, file);
    return 0;
}

static int aud_ren_release(struct inode *inode, struct file *file)
{
    aud_free(&aud_ren, file);
    return 0;
}

static int aud_open(struct inode *inode, struct file *file)
{
    file->private_data = NULL;
    return 0;
}

static const struct file_operations aud_dec_fops = {
    .owner = THIS_MODULE,
    .open = aud_open,
    .release = aud_dec_release,
    .unlocked_ioctl = aud_dec_ioctl,
    .compat_ioctl = aud_dec_ioctl,
};

static const struct file_operations aud_ren_fops = {
    .owner = THIS_MODULE,
    .open = aud_open,
    .release = aud_ren_release,
    .unlocked_ioctl = aud_ren_ioctl,
    .compat_ioctl = aud_ren_ioctl,
};

/* The analog audio decoder (the SIF's A2, NICAM, FM and BTSC demodulator): LG's core/aad
 * (aad_drv.c, aad_coredrv.c, aad_kapi.h), /dev/lg/aad0 (major 140), which H13's and M14's KADP
 * open; and the 2016 audio driver's copy of it (audio_drv_aad.c, audio_kapi_aad.h), aaad0 (143:5),
 * which M16P's KADP_AUDIO_AAD_* open: the same commands from nr 10 on magic 'A', after a handle
 * (nr 1, LX_AUD_DEV_AAD, the device after the master) and start, stop, flush and close. Settings
 * are accepted; with no SIF signal the queries answer as the driver does with no carrier
 * detected: no sound system, no standard (absent), no SIF, the "unknown" mode of the init type's
 * family; the thresholds as set; a register reads 0. */
#define AAD_MAJOR          140
#define AAD_NR_INIT        0
#define AAD_NR_BAND        2
#define AAD_NR_SET_THRES   5
#define AAD_NR_GET_BAND    6
#define AAD_NR_SOUND_MODE  7
#define AAD_NR_GET_THRES   8
#define AAD_NR_ANALOG_MODE 9
#define AAD_NR_SIF_STATUS  10
#define AAD_NR_REG         20
#define AAD_SYSTEM_UNKNOWN 0xf0 /* LX_AAD_SIF_SYSTEM_UNKNOWN */
#define AAD_NOT_AVAILE     20   /* LX_AAD_SIF_NOT_AVAILE */
#define AAD_REG_WRNRD      2

struct lx_aad_system_info { /* LX_AAD_SIF_SYSTEM_INFO_T */
    u32 set, manual, detected, strength, magnitude;
};

struct lx_aad_standard_info { /* LX_AAD_SIF_STANDARD_INFO_T */
    u32 set, detected, availability;
};

struct lx_aad_threshold { /* LX_AAD_SIF_THRESHOLD_T */
    u32 mode;
    u16 car2_mag, car2_hi, car2_lo;
    u8 id_hi, id_lo, nicam_hi, nicam_lo;
};
static_assert(sizeof(struct lx_aad_threshold) == 16);

struct aad_device {
    const char *name;
    char magic;
    unsigned int base; /* the nr of INIT_MODULE */
    struct lx_seen seen;
};

static struct aad_device aad_h = {
    .name = "aad", .magic = 'a', .base = 0, .seen = LX_SEEN_INIT(aad_h.seen)};
static struct aad_device aad_a = {
    .name = "aaad", .magic = 'A', .base = 10, .seen = LX_SEEN_INIT(aad_a.seen)};
static DEFINE_MUTEX(aad_lock);
static u32 aad_init_type; /* LX_AAD_INIT_TYPE_T, NONE before INIT_MODULE */
static struct lx_aad_threshold aad_thres;

/* AAD_SIF_GetCurAnalogMode with neither carrier: the unknown state of the family */
static u32 aad_analog_mode(void)
{
    switch (aad_init_type) {
    case 1:
    case 8:          /* ATSC A2, DVB AJJA (NTSC M/N) */
        return 0x13; /* LX_ATV_GET_NTSC_A2_UNKNOWN */
    case 2:
    case 7:          /* BTSC */
        return 0x18; /* LX_ATV_GET_NTSC_BTSC_UNKNOWN */
    default:
        return 0x06; /* LX_ATV_GET_PAL_UNKNOWN */
    }
}

static long aad_command(struct aad_device *a, unsigned int cmd, void __user *uarg)
{
    struct lx_aad_system_info sys;
    struct lx_aad_standard_info std;
    struct lx_aad_threshold th;
    struct lx_aud_reg reg;
    unsigned int nr = _IOC_NR(cmd);

    if (a == &aad_a && cmd == _IOR('A', 1, u32)) /* GET_KDRV_HANDLE */
        return put_user(aud_master_dev + 1, (u32 __user *)uarg);
    if (a == &aad_a && _IOC_DIR(cmd) == _IOC_NONE && nr >= 2 && nr <= 5)
        return 0; /* start, stop, flush, close */
    if (nr < a->base)
        return -ENOTTY;
    nr -= a->base;
    switch (_IOC_DIR(cmd) << 8 | nr) {
    case _IOC_WRITE << 8 | AAD_NR_INIT:
        return get_user(aad_init_type, (u32 __user *)uarg);
    case _IOC_WRITE << 8 | AAD_NR_SET_THRES:
        return copy_from_user(&aad_thres, uarg, sizeof(aad_thres)) ? -EFAULT : 0;
    case (_IOC_READ | _IOC_WRITE) << 8 | AAD_NR_GET_THRES:
        if (copy_from_user(&th, uarg, sizeof(th)))
            return -EFAULT;
        if (th.mode & 0x01)
            th.car2_mag = aad_thres.car2_mag;
        if (th.mode & 0x02)
            th.car2_hi = aad_thres.car2_hi;
        if (th.mode & 0x04)
            th.car2_lo = aad_thres.car2_lo;
        if (th.mode & 0x08) {
            th.id_hi = aad_thres.id_hi;
            th.id_lo = aad_thres.id_lo;
        }
        if (th.mode & 0x10) {
            th.nicam_hi = aad_thres.nicam_hi;
            th.nicam_lo = aad_thres.nicam_lo;
        }
        return copy_to_user(uarg, &th, sizeof(th)) ? -EFAULT : 0;
    case (_IOC_READ | _IOC_WRITE) << 8 | AAD_NR_GET_BAND:
        if (copy_from_user(&sys, uarg, sizeof(sys)))
            return -EFAULT;
        sys.detected = AAD_SYSTEM_UNKNOWN;
        sys.strength = sys.magnitude = 0;
        return copy_to_user(uarg, &sys, sizeof(sys)) ? -EFAULT : 0;
    case (_IOC_READ | _IOC_WRITE) << 8 | AAD_NR_SOUND_MODE:
        if (copy_from_user(&std, uarg, sizeof(std)))
            return -EFAULT;
        std.detected = AAD_NOT_AVAILE;
        std.availability = 0; /* LX_AAD_SIF_ABSENT */
        return copy_to_user(uarg, &std, sizeof(std)) ? -EFAULT : 0;
    case _IOC_READ << 8 | AAD_NR_ANALOG_MODE:
        return put_user(aad_analog_mode(), (u32 __user *)uarg);
    case _IOC_READ << 8 | AAD_NR_SIF_STATUS:
        return put_user(0, (u32 __user *)uarg);
    case (_IOC_READ | _IOC_WRITE) << 8 | AAD_NR_REG:
        if (copy_from_user(&reg, uarg, sizeof(reg)))
            return -EFAULT;
        if (reg.mode > AAD_REG_WRNRD)
            return -EPERM; /* the array and dump modes */
        reg.readdata = 0;
        return copy_to_user(uarg, &reg, sizeof(reg)) ? -EFAULT : 0;
    default:
        return _IOC_DIR(cmd) & _IOC_READ ? -EPERM : 0;
    }
}

static long aad_ioctl(struct aad_device *a, unsigned int cmd, unsigned long arg)
{
    long ret;

    if (_IOC_TYPE(cmd) != a->magic)
        return -ENOTTY;
    mutex_lock(&aad_lock);
    ret = aad_command(a, cmd, compat_ptr(arg));
    mutex_unlock(&aad_lock);
    return lx_seen_log(&a->seen, a->name, cmd, ret);
}

static long aad_h_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    return aad_ioctl(&aad_h, cmd, arg);
}

static long aad_a_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    return aad_ioctl(&aad_a, cmd, arg);
}

static const struct file_operations aad_h_fops = {
    .owner = THIS_MODULE,
    .open = aud_open,
    .unlocked_ioctl = aad_h_ioctl,
    .compat_ioctl = aad_h_ioctl,
};

static const struct file_operations aad_a_fops = {
    .owner = THIS_MODULE,
    .open = aud_open,
    .unlocked_ioctl = aad_a_ioctl,
    .compat_ioctl = aad_a_ioctl,
};

static struct lgemu_cdev aud_dec_cdev, aud_ren_cdev, aad_h_cdev, aad_a_cdev;
static bool aad_a_node;

void lx_aud_modules_remove(void)
{
    if (aad_a_node)
        device_destroy(&lx_class, MKDEV(AUD_MAJOR, AUD_AAAD_MINOR));
    aad_a_node = false;
    lgemu_cdev_del(&aad_a_cdev);
    lgemu_cdev_del(&aad_h_cdev);
    lgemu_cdev_del(&aud_ren_cdev);
    lgemu_cdev_del(&aud_dec_cdev);
}

/* With the master (amst): the decoders and renderers, the analog decoder at aad0 (each LX
 * kernel has core/aad) and, with the 2016 master (LX_AUD_DEV_MAS0 23), aaad0, which the stock
 * driver makes itself (its node is not in the device table). */
int lx_aud_modules_add(void)
{
    int err;

    if (!amst)
        return 0;
    aud_ren.first = aud_master_dev - aud_ren.count;
    err = lgemu_cdev_add(&aud_dec_cdev, AUD_MAJOR, AUD_DEC_MINOR, 1, "adec", &aud_dec_fops);
    if (!err)
        err = lgemu_cdev_add(&aud_ren_cdev, AUD_MAJOR, AUD_REN_MINOR, 1, "aren", &aud_ren_fops);
    if (!err)
        err = lgemu_cdev_add(&aad_h_cdev, AAD_MAJOR, 0, 1, "aad", &aad_h_fops);
    if (!err && aud_master_dev == AUD_MAS0_2016) {
        err = lgemu_cdev_add(&aad_a_cdev, AUD_MAJOR, AUD_AAAD_MINOR, 1, "aaad", &aad_a_fops);
        if (!err) {
            struct device *d =
                device_create(&lx_class, NULL, MKDEV(AUD_MAJOR, AUD_AAAD_MINOR), NULL, "aaad0");

            err = PTR_ERR_OR_ZERO(d);
            aad_a_node = !err;
        }
    }
    if (err) {
        lx_aud_modules_remove();
        return err;
    }
    pr_info("audio: adec0 (DEC%u-%u), aren0 (REN %u-%u), aad0%s, no DSP\n", aud_dec.first,
            aud_dec.first + aud_dec.count - 1, aud_ren.first, aud_ren.first + aud_ren.count - 1,
            aad_a_node ? " and aaad0" : "");
    return 0;
}
