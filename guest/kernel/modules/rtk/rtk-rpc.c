// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* RPC rings, answer rules and request accounting. */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#include <linux/fb.h>
#include <linux/vmalloc.h>
#include <linux/anon_inodes.h>
#include <linux/bitmap.h>
#include <linux/compat.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/kobject.h>
#include <linux/math64.h>
#include <linux/mm.h>
#include <linux/mman.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/proc_fs.h>
#include <linux/sched.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include <linux/workqueue.h>
#include <linux/unaligned.h>

#include "lgemu-drv.h"
#include "lgemu-i2c.h"
#include "rtk-internal.h"
#include "../../../../include/webos-video.h"

/* The RPC answers (below, "RPC"). */
static char *rpc_reply[128], *rpc_create[16], *rpc_answer[16], *rpc_word[16];
static int rpc_reply_count, rpc_create_count, rpc_answer_count, rpc_word_count;
static unsigned int rpc_ok = 0x10000000;
/* The sound engine's OSD (master) volume: HAL_AUDIO_LGSE_SetFN009's variables go to the audio
 * CPU as AUDIO_RPC_ToAgent_AO_LGSE_SetFn009_0, whose XDR AUDIO_RPC_LGSE_SETFN009 is instanceID,
 * dataOption (a 2-byte XDR short: 1, the variables), ten dataInit words and dataVar {level,
 * volume}: argument words 12 and 13 (level: LG's volume curve, 0x7F00 at volume 100, 0 silence;
 * the user mute is volume 0). The level goes to lgemu's playback gain (lgemu snd.c), the stand-in
 * for the DSP's volume after the PCM. */
static char *rpc_fn009;
module_param(rpc_fn009, charp, 0444);
MODULE_PARM_DESC(rpc_fn009,
                 "the LGSE FN009 RPC, program:procedure (W19P 201:248); unset: not decoded");
static u32 fn009_rpc[2];
/* The digital sound outputs (W19P's libhal_rtd2872 over libkadaptor): KADP_AUDIO_SendConfig's
 * AUDIO_RPC_ToAgent_AudioConfig_0, whose XDR AUDIO_CONFIG_COMMAND is msg_id and 15 value words:
 *   - msg_id 2, the S/PDIF output's mode, sent for the connected digital output (soundbar on
 *     S/PDIF PCM 0x101, else ARC's code, else S/PDIF's) when a mode is set or an output connects:
 *     value 0 the code, 0x100 + 1 PCM, 2 auto (and half auto), 3 auto AAC, 0xa forced AC-3,
 *     0xb forced E-AC-3, 0xc bypass, 0xd bypass AAC, 0xe and 0xf the E-AC-3 bypasses;
 *   - msg_id 63, ARC: value 0 on (1) or off (0);
 *   - msg_id 62, the channel status: values 2 copy-free (1), 3 the category, 4 0x80 (no copy);
 *   - msg_id 101, a device's mute: value 1 the device (3 the S/PDIF port, also ARC's; 5 the
 *     headphone; 10 S/PDIF-ES), value 4 the mute;
 * and AUDIO_RPC_AO_ToAgent_SPDIFO_Source_Config_0 {instanceID, source}: 3 ARC, 0 S/PDIF.
 * lgemu keeps them as the sound output's values (lgemu snd.c, /proc/lgemu/sound_out). */
static char *rpc_audio_config, *rpc_spdifo_source;
module_param(rpc_audio_config, charp, 0444);
MODULE_PARM_DESC(rpc_audio_config,
                 "the AudioConfig RPC, program:procedure (W19P 201:16); unset: not decoded");
module_param(rpc_spdifo_source, charp, 0444);
MODULE_PARM_DESC(rpc_spdifo_source, "the S/PDIF out source RPC, program:procedure (W19P 201:90); "
                                    "unset: not decoded");
static u32 audio_config_rpc[2], spdifo_source_rpc[2];
#define AUDIO_CONFIG_SPDIF_MODE 2
#define AUDIO_CONFIG_SPDIF_CS   62
#define AUDIO_CONFIG_ARC        63
#define AUDIO_CONFIG_MUTE       101

/* the sound output's part of a request's argument words a */
static void rpc_sound_out(u32 program, u32 procedure, const __be32 *a, u32 nargs)
{
    long v[5];

    if (rpc_audio_config && program == audio_config_rpc[0] && procedure == audio_config_rpc[1] &&
        nargs >= 6) {
        for (int i = 0; i < 5; i++)
            v[i] = be32_to_cpu(a[1 + i]);
        switch (be32_to_cpu(a[0])) {
        case AUDIO_CONFIG_SPDIF_MODE:
            lgemu_snd_out_set("AudioConfig SPDIF Mode", v, 1);
            break;
        case AUDIO_CONFIG_ARC:
            lgemu_snd_out_set("AudioConfig ARC", v, 1);
            break;
        case AUDIO_CONFIG_SPDIF_CS:
            lgemu_snd_out_set("AudioConfig SPDIF ChannelStatus", v + 2, 3);
            break;
        case AUDIO_CONFIG_MUTE: {
            char name[32];

            snprintf(name, sizeof(name), "AudioConfig Mute %ld", v[1]);
            lgemu_snd_out_set(name, v + 4, 1);
            break;
        }
        }
    } else if (rpc_spdifo_source && program == spdifo_source_rpc[0] &&
               procedure == spdifo_source_rpc[1] && nargs >= 2) {
        v[0] = be32_to_cpu(a[1]);
        lgemu_snd_out_set("SPDIFO Source", v, 1);
    }
}
#define FN009_DATA_OPTION 1 /* argument word 1's high half */
#define FN009_VARS        1
#define FN009_LEVEL       12 /* argument word */
module_param_array(rpc_reply, charp, &rpc_reply_count, 0444);
MODULE_PARM_DESC(rpc_reply, "result sizes other than 4 bytes, program:procedure:bytes");
module_param_array(rpc_create, charp, &rpc_create_count, 0444);
MODULE_PARM_DESC(rpc_create, "procedures that create an instance, program:procedure (the result: "
                             "S_OK and a new id)");
module_param_array(rpc_answer, charp, &rpc_answer_count, 0444);
MODULE_PARM_DESC(rpc_answer, "results other than S_OK, program:procedure:word:value:result (when "
                             "argument word 'word' is 'value')");
module_param_array(rpc_word, charp, &rpc_word_count, 0444);
MODULE_PARM_DESC(rpc_word, "result words a device states (its capabilities), "
                           "program:procedure:index:value (index 1 on; 0 is the HRESULT)");
module_param(rpc_ok, uint, 0444);
MODULE_PARM_DESC(rpc_ok, "the HRESULT S_OK every other RPC answers (default 0x10000000)");

static u32 rpc_result_size(u32 program, u32 procedure, bool *given);

static int rpc_show(char *buf, size_t size, const struct lgemu_count *e)
{
    bool given;
    u32 bytes = rpc_result_size(e->a, e->b, &given);

    return scnprintf(buf, size, "program %u procedure %u: %lu (%u bytes%s)\n", e->a, e->b, e->count,
                     bytes, given ? "" : ", not given");
}

DEFINE_LGEMU_COUNTS(rpc_counts, 512, 64, rpc_show); /* ("rpc", program, procedure) */

/* ---- RPC: /dev/rpc0..11 and /dev/rpc100 ----
 *
 * Minor m is ring m / 2 in poll (m even) or interrupt (m odd) mode; rings 0/1 are the SCPU's to and
 * from the audio CPU, 2/3 video CPU 1, 4/5 video CPU 2. KADP writes a request to an even ring (one
 * write: the 28-byte RPC_STRUCT, big-endian, and the XDR arguments) and its RPC_thread reads the
 * reply from the next ring in the same mode: RPC_STRUCT (program 99 REPLYID), the request's task
 * id, and the result, which Reply_Handler copies to the request's context (the stub's result
 * buffer, no bounds check) before waking the task.
 *
 * The answer is written at once into the reading file of that ring and mode that the writing
 * process opened, as an idle CPU's "done": the first result word is rpc_ok (or an rpc_answer's),
 * a Create's second one a new id (rpc_create), the words a device states (rpc_word: the VO's photo
 * capability), the rest zero, rpc_reply's number of bytes (4 when not given). A request with no
 * task or no context (KADP's NONBLOCK calls: no result buffer) gets none, since Reply_Handler would
 * copy the result to address 0. Nothing is ever sent unasked. */

#define RPC_REPLYID    99
#define RPC_HEADER     28
#define RPC_QUEUE      16384
#define RPC_MAX_RESULT 4096
#define RPC_MAX_ARGS   16 /* argument words an rpc_answer can test */

#define RPC_IOCTTIMEOUT _IO('k', 0)
#define RPC_IOCQTIMEOUT _IO('k', 1)
#define RPC_IOCTRESET   _IO('k', 2)
#define RPC_IOCTHANDLER _IO('k', 3)
#define RPC_IOSTOP      _IO('k', 4)

enum rpc_kind { RPC_SIZE, RPC_CREATE, RPC_ANSWER, RPC_WORD };
struct rpc_rule {
    enum rpc_kind kind;
    u32 program, procedure;
    u32 bytes;               /* RPC_SIZE */
    u32 word, value, result; /* RPC_ANSWER; RPC_WORD: result word 'word' is 'value' */
};
static struct rpc_rule *rpc_rules;
static int rpc_rules_count;
static atomic_t rpc_ids = ATOMIC_INIT(0);

static unsigned long rpc_timeout = HZ / 40; /* Realtek's default; RPC_IOCTTIMEOUT sets it */

struct rpc_reader {
    struct list_head list;
    unsigned int minor;
    pid_t tgid; /* the opener's: whose replies it gets */
    wait_queue_head_t wait;
    u8 *buf; /* RPC_QUEUE bytes, circular */
    size_t head, len;
    u32 programs[8]; /* RPC_IOCTHANDLER's */
    unsigned int nprograms;
};
static LIST_HEAD(rpc_readers);
static DEFINE_SPINLOCK(rpc_lock);

int rtk_rpc_rules_parse(void)
{
    int n = 0;

    int total = rpc_reply_count + rpc_create_count + rpc_answer_count + rpc_word_count;

    rpc_rules = kcalloc(total, sizeof(*rpc_rules), GFP_KERNEL);
    if (!rpc_rules && total)
        return -ENOMEM;
    for (int i = 0; i < rpc_reply_count; i++, n++) {
        u32 v[3];

        if (lgemu_parse_u32s(rpc_reply[i], 0, v, 3) || v[2] > RPC_MAX_RESULT) {
            pr_err("rpc_reply=%s: not program:procedure:bytes\n", rpc_reply[i]);
            return -EINVAL;
        }
        rpc_rules[n] = (struct rpc_rule){RPC_SIZE, v[0], v[1], .bytes = v[2]};
    }
    for (int i = 0; i < rpc_create_count; i++, n++) {
        u32 v[2];

        if (lgemu_parse_u32s(rpc_create[i], 0, v, 2)) {
            pr_err("rpc_create=%s: not program:procedure\n", rpc_create[i]);
            return -EINVAL;
        }
        rpc_rules[n] = (struct rpc_rule){RPC_CREATE, v[0], v[1]};
    }
    for (int i = 0; i < rpc_answer_count; i++, n++) {
        u32 v[5];

        if (lgemu_parse_u32s(rpc_answer[i], 0, v, 5) || v[2] >= RPC_MAX_ARGS) {
            pr_err("rpc_answer=%s: not program:procedure:word:value:result\n", rpc_answer[i]);
            return -EINVAL;
        }
        rpc_rules[n] =
            (struct rpc_rule){RPC_ANSWER, v[0], v[1], .word = v[2], .value = v[3], .result = v[4]};
    }
    for (int i = 0; i < rpc_word_count; i++, n++) {
        u32 v[4];

        if (lgemu_parse_u32s(rpc_word[i], 0, v, 4) || !v[2] || v[2] >= RPC_MAX_RESULT / 4) {
            pr_err("rpc_word=%s: not program:procedure:index:value\n", rpc_word[i]);
            return -EINVAL;
        }
        rpc_rules[n] = (struct rpc_rule){RPC_WORD, v[0], v[1], .word = v[2], .value = v[3]};
    }
    rpc_rules_count = n;
    if (rpc_fn009 && lgemu_parse_u32s(rpc_fn009, 0, fn009_rpc, 2)) {
        pr_err("rpc_fn009=%s: not program:procedure\n", rpc_fn009);
        return -EINVAL;
    }
    if (rpc_audio_config && lgemu_parse_u32s(rpc_audio_config, 0, audio_config_rpc, 2)) {
        pr_err("rpc_audio_config=%s: not program:procedure\n", rpc_audio_config);
        return -EINVAL;
    }
    if (rpc_spdifo_source && lgemu_parse_u32s(rpc_spdifo_source, 0, spdifo_source_rpc, 2)) {
        pr_err("rpc_spdifo_source=%s: not program:procedure\n", rpc_spdifo_source);
        return -EINVAL;
    }
    pr_info("rpc: %d result sizes, %d creates, %d answers, %d words\n", rpc_reply_count,
            rpc_create_count, rpc_answer_count, rpc_word_count);
    return 0;
}

/* the result's size: rpc_reply's, 8 for a Create without one, else 4; *given whether a table
 * entry said so */
static u32 rpc_result_size(u32 program, u32 procedure, bool *given)
{
    u32 bytes = 4;

    *given = false;
    for (int i = 0; i < rpc_rules_count; i++) {
        struct rpc_rule *r = &rpc_rules[i];

        if (r->program != program || r->procedure != procedure)
            continue;
        if (r->kind == RPC_SIZE) {
            *given = true;
            return r->bytes;
        }
        if (r->kind == RPC_CREATE)
            bytes = 8;
    }
    return bytes;
}

/* the result words: S_OK, an answer's, a Create's id, a device's stated words */
static void rpc_result(u32 program, u32 procedure, const __be32 *args, u32 nargs, __be32 *res,
                       u32 bytes)
{
    u32 first = rpc_ok;

    for (int i = 0; i < rpc_rules_count; i++) {
        struct rpc_rule *r = &rpc_rules[i];

        if (r->program != program || r->procedure != procedure)
            continue;
        if (r->kind == RPC_ANSWER && r->word < nargs && be32_to_cpu(args[r->word]) == r->value)
            first = r->result;
        if (r->kind == RPC_CREATE && bytes >= 8)
            res[1] = cpu_to_be32(atomic_inc_return(&rpc_ids));
        if (r->kind == RPC_WORD && bytes >= 4 * (r->word + 1))
            res[r->word] = cpu_to_be32(r->value);
    }
    if (bytes >= 4)
        res[0] = cpu_to_be32(first);
}

/* rpc_lock held; n a multiple of 4 */
static bool rpc_queue(struct rpc_reader *r, const u8 *data, size_t n)
{
    size_t tail = (r->head + r->len) % RPC_QUEUE, first = min(n, RPC_QUEUE - tail);

    if (RPC_QUEUE - r->len < n)
        return false;
    memcpy(r->buf + tail, data, first);
    memcpy(r->buf, data + first, n - first);
    r->len += n;
    return true;
}

static void rpc_deliver(unsigned int minor, const u8 *msg, size_t n, u32 program, u32 procedure)
{
    struct rpc_reader *r, *found = NULL;
    bool queued = false;

    spin_lock(&rpc_lock);
    list_for_each_entry(r, &rpc_readers, list)
    {
        if (r->minor == minor && r->tgid == current->tgid) {
            found = r;
            queued = rpc_queue(r, msg, n);
            break;
        }
    }
    spin_unlock(&rpc_lock);
    if (queued)
        wake_up_interruptible(&found->wait);
    else
        pr_warn_ratelimited("rpc: %s (%d): no %s /dev/rpc%u for the reply to %u:%u\n",
                            current->comm, current->tgid, found ? "room in" : "reader on", minor,
                            program, procedure);
}

static ssize_t rpc_write(struct file *file, const char __user *buf, size_t n, loff_t *pos)
{
    unsigned int minor = iminor(file_inode(file)), ring = minor / 2;
    __be32 req[RPC_HEADER / 4 + RPC_MAX_ARGS];
    u32 program, procedure, task, size, context, nargs, bytes;
    size_t len;
    bool given;
    u8 *msg;
    __be32 *w;

    if (ring % 2 || n < RPC_HEADER)
        return n; /* a reply to the other CPU, or not a request: nobody reads */
    len = min_t(size_t, n, sizeof(req));
    if (copy_from_user(req, buf, len))
        return -EFAULT;
    program = be32_to_cpu(req[0]);
    procedure = be32_to_cpu(req[2]);
    task = be32_to_cpu(req[3]);
    size = be32_to_cpu(req[5]);
    context = be32_to_cpu(req[6]);
    nargs = min_t(u32, size, len - RPC_HEADER) / 4;
    if (program == RPC_REPLYID)
        return n;
    if (rpc_fn009 && program == fn009_rpc[0] && procedure == fn009_rpc[1] && nargs > FN009_LEVEL &&
        be32_to_cpu(req[RPC_HEADER / 4 + FN009_DATA_OPTION]) >> 16 == FN009_VARS)
        lgemu_snd_osd_level(be32_to_cpu(req[RPC_HEADER / 4 + FN009_LEVEL]) & 0xffff);
    rpc_sound_out(program, procedure, req + RPC_HEADER / 4, nargs);
    lgemu_count(&rpc_counts, "rpc", program, procedure);
    bytes = rpc_result_size(program, procedure, &given);
    if (!given)
        pr_debug("rpc: %u:%u has no size given: %u bytes\n", program, procedure, bytes);
    if (!task || !context)
        return n;

    msg = kzalloc(RPC_HEADER + 4 + round_up(bytes, 4), GFP_KERNEL);
    if (!msg)
        return -ENOMEM;
    w = (__be32 *)msg;
    w[0] = w[1] = cpu_to_be32(RPC_REPLYID);
    w[5] = cpu_to_be32(4 + bytes);
    w[6] = req[6];
    w[7] = req[3];
    rpc_result(program, procedure, req + RPC_HEADER / 4, nargs, w + 8, bytes);
    rpc_deliver((ring + 1) * 2 + minor % 2, msg, RPC_HEADER + 4 + round_up(bytes, 4), program,
                procedure);
    kfree(msg);
    return n;
}

/* A read takes what is there up to n bytes, and the ring moves on to the next word, as Realtek's
 * does. Interrupt mode waits (rpc_timeout, then 0), poll mode does not. */
static ssize_t rpc_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    struct rpc_reader *r = file->private_data;
    size_t got, first;
    u8 *bounce;

    if (!r || !n)
        return 0;
    if (!READ_ONCE(r->len)) {
        if (r->minor % 2 == 0 || file->f_flags & O_NONBLOCK)
            return 0;
        if (wait_event_interruptible_timeout(r->wait, READ_ONCE(r->len), rpc_timeout) <= 0)
            return 0; /* Realtek's answer to a timeout and a signal alike */
    }
    bounce = kmalloc(min_t(size_t, n, RPC_QUEUE), GFP_KERNEL);
    if (!bounce)
        return -ENOMEM;
    spin_lock(&rpc_lock);
    got = min(n, r->len);
    first = min(got, RPC_QUEUE - r->head);
    memcpy(bounce, r->buf + r->head, first);
    memcpy(bounce + first, r->buf, got - first);
    first = min(round_up(got, 4), r->len);
    r->head = (r->head + first) % RPC_QUEUE;
    r->len -= first;
    spin_unlock(&rpc_lock);
    if (copy_to_user(buf, bounce, got))
        got = -EFAULT;
    kfree(bounce);
    return got;
}

static int rpc_open(struct inode *inode, struct file *file)
{
    unsigned int minor = iminor(inode);
    struct rpc_reader *r;

    if (minor == RPC_CTRL || (minor / 2) % 2 == 0)
        return 0; /* the control node and the request rings: nothing to keep */
    r = kzalloc(sizeof(*r), GFP_KERNEL);
    if (!r)
        return -ENOMEM;
    r->buf = kmalloc(RPC_QUEUE, GFP_KERNEL);
    if (!r->buf) {
        kfree(r);
        return -ENOMEM;
    }
    r->minor = minor;
    r->tgid = current->tgid;
    init_waitqueue_head(&r->wait);
    spin_lock(&rpc_lock);
    list_add(&r->list, &rpc_readers);
    spin_unlock(&rpc_lock);
    file->private_data = r;
    return 0;
}

static int rpc_release(struct inode *inode, struct file *file)
{
    struct rpc_reader *r = file->private_data;

    if (!r)
        return 0;
    spin_lock(&rpc_lock);
    list_del(&r->list);
    spin_unlock(&rpc_lock);
    kfree(r->buf);
    kfree(r);
    return 0;
}

static long rpc_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct rpc_reader *r = file->private_data;

    switch (cmd) {
    case RPC_IOCTTIMEOUT:
        WRITE_ONCE(rpc_timeout, arg);
        return 0;
    case RPC_IOCQTIMEOUT:
        return READ_ONCE(rpc_timeout);
    case RPC_IOCTRESET: /* pli_resetRPC on rpc100: the rings start empty */
        spin_lock(&rpc_lock);
        list_for_each_entry(r, &rpc_readers, list) r->head = r->len = 0;
        spin_unlock(&rpc_lock);
        return 0;
    case RPC_IOCTHANDLER: /* the process serves program arg (the other CPU's requests) */
        if (r) {
            spin_lock(&rpc_lock);
            if (r->nprograms < ARRAY_SIZE(r->programs))
                r->programs[r->nprograms++] = arg;
            spin_unlock(&rpc_lock);
        }
        return 0;
    case RPC_IOSTOP:
        return 0;
    }
    return rtk_unhandled("rpc", cmd, -ENOTTY);
}

const struct file_operations rtk_rpc_fops = {
    .owner = THIS_MODULE,
    .open = rpc_open,
    .release = rpc_release,
    .read = rpc_read,
    .write = rpc_write,
    .unlocked_ioctl = rpc_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};

bool rtk_rpc_proc_add(struct proc_dir_entry *dir)
{
    return proc_create_data("rpc", 0444, dir, &lgemu_counts_proc_ops, &rpc_counts) != NULL;
}

void rtk_rpc_cleanup(void)
{
    kfree(rpc_rules);
    rpc_rules = NULL;
}
