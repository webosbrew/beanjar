// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The TEE: /dev/tee0 with the OP-TEE driver's user ABI (linux/tee.h, TEE_IOC_*), which the
 * firmwares' OP-TEE client speaks (libteec, statically linked into Realtek's libkadaptor and into
 * legacy-broadcast-dvb, MStar's libkadaptor and libckteec), and behind it the trusted applications
 * a SoC driver module stands in for (rtk.ko's tee-ta.c), or none.
 *
 * Who adds it (lgemu-tee.h): lgemu.ko itself with tee=1, with no TA, where the firmware has
 * OP-TEE's client (the launcher's targets.tee_device: its rootfs has /usr/bin/tee-supplicant) and
 * no SoC module stands in for a TA; or a SoC driver module with its TAs (rtk.ko's tee=1). A real
 * set's TEE answers OPEN_SESSION of a TA it does not have with TEEC_ERROR_ITEM_NOT_FOUND, and its
 * clients take that cleanly; with no device at all TEEC_InitializeContext fails instead, which some
 * of them do not survive (lm21ann's legacy-broadcast-dvb: its XCAS init then blocks the broadcast
 * service's start for good, docs/research/lm21ann-xcas-tee-stall.md).
 *
 * The device: TEE_IOC_VERSION (OP-TEE, GlobalPlatform; no registered memory, so the client takes
 * its shared memory from SHM_ALLOC and copies registered buffers through it), SHM_ALLOC (an fd
 * whose mapping is the memory; the id lives as long as the fd or a mapping of it), OPEN_SESSION,
 * INVOKE, CANCEL (nothing is ever pending) and CLOSE_SESSION. A TA answers at once; a UUID without
 * one is TEEC_ERROR_ITEM_NOT_FOUND from the TEE, as OP-TEE answers a TA it cannot load.
 *
 * No key material of any set is involved: the TAs' secrets are made here at random (tee-ta.c). */
#define pr_fmt(fmt) KBUILD_MODNAME ": tee: " fmt
#include <linux/anon_inodes.h>
#include <linux/compat.h>
#include <linux/device.h>
#include <linux/file.h>
#include <linux/fs.h>
#include <linux/idr.h>
#include <linux/kref.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/sched.h>
#include <linux/slab.h>
#include <linux/tee.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>

#include "lgemu-drv.h"
#include "lgemu-tee.h"
#include "lgemu.h"

#define SHM_MAX    (16u << 20)
#define PARAMS_MAX 4

static bool tee;
module_param(tee, bool, 0444);
MODULE_PARM_DESC(tee, "/dev/tee0 with no trusted application (a SoC module may add its own)");

/* every context's shared memory and sessions, the TAs' state and which TAs there are, under one
 * lock: a TA answers at once, so nothing waits long */
static DEFINE_MUTEX(tee_lock);
static const struct lgemu_tee_tas *tee_tas;

struct tee_ctx {
    struct kref ref;
    struct idr shms;
    struct idr sessions;
};

struct tee_shm {
    struct tee_ctx *ctx;
    void *va;
    size_t size;  /* the client's */
    size_t alloc; /* page-rounded */
    int id;
};

struct tee_sess {
    const struct lgemu_ta *ta;
    void *state;
};

static void ctx_free(struct kref *ref)
{
    struct tee_ctx *ctx = container_of(ref, struct tee_ctx, ref);

    idr_destroy(&ctx->shms);
    idr_destroy(&ctx->sessions);
    kfree(ctx);
}

/* ---- shared memory ---- */

static int shm_mmap(struct file *file, struct vm_area_struct *vma)
{
    struct tee_shm *shm = file->private_data;
    unsigned long len = vma->vm_end - vma->vm_start;

    if (vma->vm_pgoff || len > shm->alloc)
        return -EINVAL;
    return remap_vmalloc_range(vma, shm->va, 0);
}

static int shm_release(struct inode *inode, struct file *file)
{
    struct tee_shm *shm = file->private_data;

    mutex_lock(&tee_lock);
    idr_remove(&shm->ctx->shms, shm->id);
    mutex_unlock(&tee_lock);
    kref_put(&shm->ctx->ref, ctx_free);
    vfree(shm->va);
    kfree(shm);
    return 0;
}

static const struct file_operations shm_fops = {
    .owner = THIS_MODULE,
    .mmap = shm_mmap,
    .release = shm_release,
};

static long shm_alloc(struct tee_ctx *ctx, void __user *uarg)
{
    struct tee_ioctl_shm_alloc_data data;
    struct tee_shm *shm;
    int fd;

    if (copy_from_user(&data, uarg, sizeof(data)))
        return -EFAULT;
    if (data.flags || data.size > SHM_MAX)
        return -EINVAL;
    shm = kzalloc(sizeof(*shm), GFP_KERNEL);
    if (!shm)
        return -ENOMEM;
    shm->size = data.size;
    shm->alloc = PAGE_ALIGN(max_t(size_t, data.size, 1));
    shm->va = vmalloc_user(shm->alloc);
    if (!shm->va) {
        kfree(shm);
        return -ENOMEM;
    }
    shm->ctx = ctx;
    mutex_lock(&tee_lock);
    shm->id = idr_alloc(&ctx->shms, shm, 1, 0, GFP_KERNEL);
    mutex_unlock(&tee_lock);
    if (shm->id < 0) {
        fd = shm->id;
        goto fail;
    }
    kref_get(&ctx->ref);
    fd = anon_inode_getfd("tee_shm", &shm_fops, shm, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        mutex_lock(&tee_lock);
        idr_remove(&ctx->shms, shm->id);
        mutex_unlock(&tee_lock);
        kref_put(&ctx->ref, ctx_free);
        goto fail;
    }
    data.id = shm->id;
    if (copy_to_user(uarg, &data, sizeof(data))) {
        /* the fd is the caller's now; it goes with the process */
        return -EFAULT;
    }
    return fd;

fail:
    vfree(shm->va);
    kfree(shm);
    return fd;
}

/* ---- parameters: the client's (struct tee_ioctl_param) to a TA's (struct lgemu_ta_param) ---- */

static u32 params_in(struct tee_ctx *ctx, const struct tee_ioctl_param *up, u32 n,
                     struct lgemu_ta_param *p)
{
    memset(p, 0, sizeof(*p) * PARAMS_MAX);
    for (u32 i = 0; i < n; i++) {
        u32 type = up[i].attr & TEE_IOCTL_PARAM_ATTR_TYPE_MASK;
        struct tee_shm *shm;

        if (up[i].attr & ~(u64)TEE_IOCTL_PARAM_ATTR_TYPE_MASK)
            return TEEC_ERROR_BAD_PARAMETERS;
        p[i].type = type;
        switch (type) {
        case TEE_IOCTL_PARAM_ATTR_TYPE_NONE:
            break;
        case TEE_IOCTL_PARAM_ATTR_TYPE_VALUE_INPUT:
        case TEE_IOCTL_PARAM_ATTR_TYPE_VALUE_OUTPUT:
        case TEE_IOCTL_PARAM_ATTR_TYPE_VALUE_INOUT:
            p[i].a = up[i].a;
            p[i].b = up[i].b;
            break;
        case TEE_IOCTL_PARAM_ATTR_TYPE_MEMREF_INPUT:
        case TEE_IOCTL_PARAM_ATTR_TYPE_MEMREF_OUTPUT:
        case TEE_IOCTL_PARAM_ATTR_TYPE_MEMREF_INOUT:
            /* a: offset in the shared memory, b: size, c: its id */
            p[i].size = up[i].b;
            if ((s64)up[i].c < 0 && !up[i].b)
                break; /* a null memref of no size */
            shm = idr_find(&ctx->shms, (int)up[i].c);
            if (!shm || up[i].a > shm->size || up[i].b > shm->size - up[i].a)
                return TEEC_ERROR_BAD_PARAMETERS;
            p[i].buf = (u8 *)shm->va + up[i].a;
            break;
        default:
            return TEEC_ERROR_BAD_PARAMETERS;
        }
    }
    return TEEC_SUCCESS;
}

static void params_out(struct tee_ioctl_param *up, u32 n, const struct lgemu_ta_param *p)
{
    for (u32 i = 0; i < n; i++) {
        switch (p[i].type) {
        case TEE_IOCTL_PARAM_ATTR_TYPE_VALUE_OUTPUT:
        case TEE_IOCTL_PARAM_ATTR_TYPE_VALUE_INOUT:
            up[i].a = p[i].a;
            up[i].b = p[i].b;
            break;
        case TEE_IOCTL_PARAM_ATTR_TYPE_MEMREF_OUTPUT:
        case TEE_IOCTL_PARAM_ATTR_TYPE_MEMREF_INOUT:
            up[i].b = p[i].size;
            break;
        }
    }
}

/* the argument of OPEN_SESSION and INVOKE: a struct with num_params parameters after it */
static void *arg_in(void __user *uarg, size_t head, u32 *nparams, size_t *len,
                    struct tee_ioctl_param **params, u64 __user **ubuf)
{
    struct tee_ioctl_buf_data buf;
    void *arg;
    u32 n;

    if (copy_from_user(&buf, uarg, sizeof(buf)))
        return ERR_PTR(-EFAULT);
    if (buf.buf_len > TEE_MAX_ARG_SIZE || buf.buf_len < head)
        return ERR_PTR(-EINVAL);
    arg = memdup_user(u64_to_user_ptr(buf.buf_ptr), buf.buf_len);
    if (IS_ERR(arg))
        return arg;
    /* num_params is the last u32 of both heads */
    n = *(u32 *)((u8 *)arg + head - sizeof(u32));
    if (n > PARAMS_MAX || buf.buf_len != head + n * sizeof(struct tee_ioctl_param)) {
        kfree(arg);
        return ERR_PTR(-EINVAL);
    }
    *nparams = n;
    *len = buf.buf_len;
    *params = (struct tee_ioctl_param *)((u8 *)arg + head);
    *ubuf = u64_to_user_ptr(buf.buf_ptr);
    return arg;
}

/* a UUID with no TA where no SoC module has TAs: logged once a UUID (as far as the table goes) */
static void ta_unknown(const u8 *uuid)
{
    static u8 seen[16][16];
    static unsigned int n;

    for (unsigned int i = 0; i < n; i++)
        if (!memcmp(seen[i], uuid, 16))
            return;
    if (n < ARRAY_SIZE(seen))
        memcpy(seen[n++], uuid, 16);
    pr_info("no TA %pUb (%s): item not found\n", uuid, current->comm);
}

static long open_session(struct tee_ctx *ctx, void __user *uarg)
{
    struct lgemu_ta_param p[PARAMS_MAX];
    struct tee_ioctl_open_session_arg *arg;
    struct tee_ioctl_param *up;
    struct tee_sess *sess = NULL;
    const struct lgemu_ta *ta;
    u64 __user *ubuf;
    size_t len;
    long err = 0;
    u32 n;

    arg = arg_in(uarg, sizeof(*arg), &n, &len, &up, &ubuf);
    if (IS_ERR(arg))
        return PTR_ERR(arg);
    arg->ret_origin = TEEC_ORIGIN_TEE;
    arg->session = 0;
    mutex_lock(&tee_lock);
    ta = tee_tas ? tee_tas->find(arg->uuid) : NULL;
    if (!ta) {
        arg->ret = TEEC_ERROR_ITEM_NOT_FOUND;
        if (tee_tas)
            tee_tas->unknown(arg->uuid);
        else
            ta_unknown(arg->uuid);
        goto out;
    }
    arg->ret = params_in(ctx, up, n, p);
    if (arg->ret)
        goto out;
    sess = kzalloc(sizeof(*sess), GFP_KERNEL);
    if (!sess) {
        err = -ENOMEM;
        goto out;
    }
    sess->ta = ta;
    arg->ret_origin = TEEC_ORIGIN_TRUSTED_APP;
    arg->ret = ta->open ? ta->open(&sess->state, p) : TEEC_SUCCESS;
    if (arg->ret)
        goto out;
    err = idr_alloc(&ctx->sessions, sess, 1, 0, GFP_KERNEL);
    if (err < 0) {
        if (ta->close)
            ta->close(sess->state);
        goto out;
    }
    arg->session = err;
    err = 0;
    sess = NULL;
    params_out(up, n, p);
out:
    mutex_unlock(&tee_lock);
    kfree(sess);
    if (!err && copy_to_user(ubuf, arg, len))
        err = -EFAULT;
    kfree(arg);
    return err;
}

static long invoke(struct tee_ctx *ctx, void __user *uarg)
{
    struct lgemu_ta_param p[PARAMS_MAX];
    struct tee_ioctl_invoke_arg *arg;
    struct tee_ioctl_param *up;
    struct tee_sess *sess;
    u64 __user *ubuf;
    size_t len;
    long err = 0;
    u32 n;

    arg = arg_in(uarg, sizeof(*arg), &n, &len, &up, &ubuf);
    if (IS_ERR(arg))
        return PTR_ERR(arg);
    mutex_lock(&tee_lock);
    sess = idr_find(&ctx->sessions, arg->session);
    if (!sess) {
        err = -EINVAL;
        goto out;
    }
    arg->ret_origin = TEEC_ORIGIN_TEE;
    arg->ret = params_in(ctx, up, n, p);
    if (arg->ret)
        goto out;
    arg->ret_origin = TEEC_ORIGIN_TRUSTED_APP;
    arg->ret = sess->ta->invoke(sess->state, arg->func, p);
    params_out(up, n, p);
out:
    mutex_unlock(&tee_lock);
    if (!err && copy_to_user(ubuf, arg, len))
        err = -EFAULT;
    kfree(arg);
    return err;
}

static void session_end(struct tee_sess *sess)
{
    if (sess->ta->close)
        sess->ta->close(sess->state);
    kfree(sess);
}

static long close_session(struct tee_ctx *ctx, void __user *uarg)
{
    struct tee_ioctl_close_session_arg arg;
    struct tee_sess *sess;

    if (copy_from_user(&arg, uarg, sizeof(arg)))
        return -EFAULT;
    mutex_lock(&tee_lock);
    sess = idr_remove(&ctx->sessions, arg.session);
    if (sess)
        session_end(sess);
    mutex_unlock(&tee_lock);
    return sess ? 0 : -EINVAL;
}

static long tee_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct tee_ctx *ctx = file->private_data;
    void __user *uarg = (void __user *)arg;

    switch (cmd) {
    case TEE_IOC_VERSION: {
        struct tee_ioctl_version_data v = {
            .impl_id = TEE_IMPL_ID_OPTEE,
            .impl_caps = TEE_OPTEE_CAP_TZ,
            .gen_caps = TEE_GEN_CAP_GP,
        };
        return copy_to_user(uarg, &v, sizeof(v)) ? -EFAULT : 0;
    }
    case TEE_IOC_SHM_ALLOC:
        return shm_alloc(ctx, uarg);
    case TEE_IOC_OPEN_SESSION:
        return open_session(ctx, uarg);
    case TEE_IOC_INVOKE:
        return invoke(ctx, uarg);
    case TEE_IOC_CANCEL:
        return 0;
    case TEE_IOC_CLOSE_SESSION:
        return close_session(ctx, uarg);
    }
    return -EINVAL;
}

static int tee_open(struct inode *inode, struct file *file)
{
    struct tee_ctx *ctx = kzalloc(sizeof(*ctx), GFP_KERNEL);

    if (!ctx)
        return -ENOMEM;
    kref_init(&ctx->ref);
    idr_init(&ctx->shms);
    idr_init(&ctx->sessions);
    file->private_data = ctx;
    return 0;
}

static int tee_release(struct inode *inode, struct file *file)
{
    struct tee_ctx *ctx = file->private_data;
    struct tee_sess *sess;
    int id;

    mutex_lock(&tee_lock);
    idr_for_each_entry(&ctx->sessions, sess, id) session_end(sess);
    mutex_unlock(&tee_lock);
    kref_put(&ctx->ref, ctx_free); /* the shared memory still mapped keeps it */
    return 0;
}

static const struct file_operations tee_fops = {
    .owner = THIS_MODULE,
    .open = tee_open,
    .release = tee_release,
    .unlocked_ioctl = tee_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};

/* ---- the device ---- */

static char *tee_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return NULL;
}

static const struct class tee_class = {.name = "tee", .devnode = tee_devnode};
static struct lgemu_cdev tee_cdev;
static bool tee_class_registered, tee_made;
static unsigned int tee_users; /* lgemu's own tee=1 and each SoC module's add */

static void tee_device_remove(void)
{
    if (tee_made)
        device_destroy(&tee_class, tee_cdev.dev);
    tee_made = false;
    if (tee_class_registered)
        class_unregister(&tee_class);
    tee_class_registered = false;
    lgemu_cdev_del(&tee_cdev);
}

static int tee_device_add(void)
{
    struct device *d;
    int err;

    err = lgemu_cdev_add(&tee_cdev, 0, 0, 1, "tee", &tee_fops);
    if (!err)
        err = class_register(&tee_class);
    if (err)
        goto fail;
    tee_class_registered = true;
    d = device_create(&tee_class, NULL, tee_cdev.dev, NULL, "tee0");
    if (IS_ERR(d)) {
        err = PTR_ERR(d);
        goto fail;
    }
    tee_made = true;
    return 0;
fail:
    tee_device_remove();
    return err;
}

int lgemu_tee_add(const struct lgemu_tee_tas *tas)
{
    int err = 0;

    mutex_lock(&tee_lock);
    if (tas && tee_tas) {
        err = -EBUSY;
        goto out;
    }
    if (!tee_users)
        err = tee_device_add();
    if (err)
        goto out;
    tee_users++;
    if (tas)
        tee_tas = tas;
out:
    mutex_unlock(&tee_lock);
    return err;
}
EXPORT_SYMBOL_GPL(lgemu_tee_add);

void lgemu_tee_remove(const struct lgemu_tee_tas *tas)
{
    mutex_lock(&tee_lock);
    if (tas && tee_tas == tas)
        tee_tas = NULL;
    if (tee_users && !--tee_users)
        tee_device_remove();
    mutex_unlock(&tee_lock);
}
EXPORT_SYMBOL_GPL(lgemu_tee_remove);

int lgemu_tee_init(void)
{
    int err;

    if (!tee)
        return 0;
    err = lgemu_tee_add(NULL);
    if (!err)
        pr_info("/dev/tee0, no trusted application: every session is item not found\n");
    return err;
}

void lgemu_tee_exit(void)
{
    if (tee)
        lgemu_tee_remove(NULL);
}
