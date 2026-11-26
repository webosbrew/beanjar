// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
#include "private.h"

/* ---- /dev/semutex (181): MStar's named mutexes and the one shared memory ----
 *
 * KADP's MsOS_CreateNamedMutex, _LockMutex, _UnlockMutex, _DeleteNamedMutexbyIndex and
 * _Mapping_SharedMem, with the driver's semantics (drivers/mstar2/drv/semutex): a mutex is a
 * counting semaphore of 1 by name, the same index (from 255) for the name in every process; any
 * thread of any process may unlock it (KADP locks and unlocks from different threads); a process
 * that exits gives back the mutexes it holds, so a daemon that dies holding one does not wedge the
 * others: by its tgid as it closes the file (flush), as the driver does, and by the file when that
 * is released. The file outlives the holder when a child it forked keeps it open. The shared memory
 * is one block, created at the first CREATE_SHAREMEMORY with its size (QUERY_ISFIRST tells that
 * caller), mapped by mmap, and freed when the last file that created or found it is released. The
 * ioctl numbers are W16R's KADP's (MsOS_* in libkadaptor): the older numbering, LOCK and UNLOCK as
 * _IO. */

#define MDRV_SEMUTEX_LOCK                      _IO('S', 0)
#define MDRV_SEMUTEX_UNLOCK                    _IO('S', 1)
#define MDRV_SEMUTEX_CREATE_SHAREMEMORY        _IO('S', 2)
#define MDRV_SEMUTEX_CREATE_MUTEX              _IOR('S', 5, int)
#define MDRV_SEMUTEX_DEL_MUTEX                 _IOR('S', 6, int)
#define MDRV_SEMUTEX_TRY_LOCK                  _IOR('S', 7, int)
#define MDRV_SEMUTEX_QUERY_ISFIRST_SHAREMEMORY _IO('S', 8)
#define MDRV_SEMUTEX_LOCK_WITHTIMEOUT          _IOR('S', 9, int)
#define MDRV_SEMUTEX_QUERY_SHAREMEMORY_SIZE    _IOR('S', 11, int)

/* W21A's MsOS_GetSHMSize queries this before allocating. UP7560's driver
 * reports 10 MiB; older firmwares have no capacity query (leave at zero). */
static unsigned int semutex_shm_capacity;
module_param(semutex_shm_capacity, uint, 0444);
MODULE_PARM_DESC(semutex_shm_capacity,
                 "shared-memory capacity returned by the newer semutex query, in bytes");

#define SEMUTEX_INDEX_BEGIN 0xff
#define SEMUTEX_MUTEXES     320
#define SEMUTEX_NAME_LEN    64

static struct semutex_mutex {
    char name[SEMUTEX_NAME_LEN];
    struct semaphore sem;
    struct file *locked_file; /* NULL: not locked */
    pid_t tgid, pid;          /* the holder, for the trace */
    u64 since;                /* when it locked (boot time ns) */
} semutex_mutexes[SEMUTEX_MUTEXES];

/* the mailbox trace's semutex lines (below, "The mailbox trace") */
static void sem_ev(u8 op, int i, int result, s32 aux)
{
    struct semutex_mutex *m = &semutex_mutexes[i];

    mailbox_sem_event(op, SEMUTEX_INDEX_BEGIN + i, result, aux, m->locked_file ? m->tgid : 0,
                      m->locked_file ? m->pid : 0, m->name);
}
static DEFINE_MUTEX(semutex_lock); /* the table's names and owners */

static void *semutex_shm; /* vmalloc_user */
static size_t semutex_shm_size;
static unsigned int semutex_shm_users;
static DEFINE_MUTEX(semutex_shm_lock);

struct semutex_file {
    bool shm_user, shm_first;
};

static int semutex_open(struct inode *inode, struct file *file)
{
    struct semutex_file *f = kzalloc(sizeof(*f), GFP_KERNEL);

    if (!f)
        return -ENOMEM;
    file->private_data = f;
    return 0;
}

static int semutex_release(struct inode *inode, struct file *file)
{
    struct semutex_file *f = file->private_data;

    mutex_lock(&semutex_lock);
    for (int i = 0; i < SEMUTEX_MUTEXES; i++) {
        struct semutex_mutex *m = &semutex_mutexes[i];

        if (m->locked_file == file) {
            sem_ev(SEM_OP_RELEASE, i, 0,
                   div_u64(ktime_get_boottime_ns() - m->since, NSEC_PER_MSEC));
            m->locked_file = NULL;
            up(&m->sem);
        }
    }
    mutex_unlock(&semutex_lock);
    mutex_lock(&semutex_shm_lock);
    if (f->shm_user && !--semutex_shm_users) {
        vfree(semutex_shm);
        semutex_shm = NULL;
        semutex_shm_size = 0;
    }
    mutex_unlock(&semutex_shm_lock);
    kfree(f);
    return 0;
}

/* An exiting process closes the file: it gives back the mutexes it holds, as MStar's driver does
 * (_MDrv_SEMUTEX_Flush, by tgid). The file may live on in a child that the holder forked, so its
 * release does not come then, and the holder is gone. */
static int semutex_flush(struct file *file, fl_owner_t id)
{
    if (!(current->flags & PF_EXITING))
        return 0;
    mutex_lock(&semutex_lock);
    for (int i = 0; i < SEMUTEX_MUTEXES; i++) {
        struct semutex_mutex *m = &semutex_mutexes[i];

        if (m->locked_file && m->tgid == current->tgid) {
            pr_info("semutex: %s[%d] exits holding %s: given back\n", current->comm, current->tgid,
                    m->name);
            sem_ev(SEM_OP_RELEASE, i, 0,
                   div_u64(ktime_get_boottime_ns() - m->since, NSEC_PER_MSEC));
            m->locked_file = NULL;
            up(&m->sem);
        }
    }
    mutex_unlock(&semutex_lock);
    return 0;
}

/* the mutex of a user's index, or NULL */
static struct semutex_mutex *semutex_mutex_at(void __user *uarg, int *index)
{
    if (get_user(*index, (int __user *)uarg))
        return NULL;
    if (*index < SEMUTEX_INDEX_BEGIN || *index >= SEMUTEX_INDEX_BEGIN + SEMUTEX_MUTEXES)
        return NULL;
    return &semutex_mutexes[*index - SEMUTEX_INDEX_BEGIN];
}

static void semutex_locked(struct semutex_mutex *m, struct file *file, u8 op, s32 aux)
{
    mutex_lock(&semutex_lock);
    m->locked_file = file;
    m->tgid = current->tgid;
    m->pid = current->pid;
    m->since = ktime_get_boottime_ns();
    if (aux) /* only what waited: a trace of every lock would overflow */
        sem_ev(op, m - semutex_mutexes, 0, aux);
    mutex_unlock(&semutex_lock);
}

/* a lock that has to wait: traced with its holder before it does */
static void semutex_wait_ev(struct semutex_mutex *m)
{
    mutex_lock(&semutex_lock);
    if (m->locked_file) /* aux: how long the holder has had it, ms */
        sem_ev(SEM_OP_WAIT, m - semutex_mutexes, 0,
               div_u64(ktime_get_boottime_ns() - m->since, NSEC_PER_MSEC));
    mutex_unlock(&semutex_lock);
}

static long semutex_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct semutex_file *f = file->private_data;
    void __user *uarg = (void __user *)arg;
    struct semutex_mutex *m;
    int index, ret;

    ioctl_count("semutex", cmd);

    ret = semutex_new_ioctl(cmd, uarg);
    if (ret != -ENOIOCTLCMD)
        return ret;

    switch (cmd) {
    case MDRV_SEMUTEX_CREATE_MUTEX: { /* arg: the name */
        char name[SEMUTEX_NAME_LEN];
        int free = -1;
        long n = strncpy_from_user(name, uarg, sizeof(name));

        if (n <= 0)
            return n < 0 ? n : -EINVAL;
        name[sizeof(name) - 1] = 0;
        mutex_lock(&semutex_lock);
        for (int i = 0; i < SEMUTEX_MUTEXES; i++) {
            if (!strcmp(semutex_mutexes[i].name, name)) {
                mutex_unlock(&semutex_lock);
                return SEMUTEX_INDEX_BEGIN + i;
            }
            if (free < 0 && !semutex_mutexes[i].name[0])
                free = i;
        }
        if (free >= 0)
            strscpy(semutex_mutexes[free].name, name, SEMUTEX_NAME_LEN);
        mutex_unlock(&semutex_lock);
        return free < 0 ? -ENOSPC : SEMUTEX_INDEX_BEGIN + free;
    }
    case MDRV_SEMUTEX_DEL_MUTEX: /* the name goes; a holder keeps its lock */
        m = semutex_mutex_at(uarg, &index);
        if (!m)
            return -EINVAL;
        mutex_lock(&semutex_lock);
        m->name[0] = 0;
        mutex_unlock(&semutex_lock);
        return 0;
    case MDRV_SEMUTEX_LOCK:
        m = semutex_mutex_at(uarg, &index);
        if (!m)
            return -EINVAL;
        if (down_trylock(&m->sem)) {
            u64 t = ktime_get_boottime_ns();

            semutex_wait_ev(m);
            ret = down_interruptible(&m->sem); /* KADP retries on EINTR */
            if (ret)
                return ret;
            semutex_locked(m, file, SEM_OP_LOCK,
                           max_t(s32, 1, div_u64(ktime_get_boottime_ns() - t, NSEC_PER_MSEC)));
        } else {
            semutex_locked(m, file, SEM_OP_LOCK, 0);
        }
        return 0;
    case MDRV_SEMUTEX_TRY_LOCK:
        m = semutex_mutex_at(uarg, &index);
        if (!m)
            return -EINVAL;
        if (down_trylock(&m->sem))
            return 1;
        semutex_locked(m, file, SEM_OP_TRY, 0);
        return 0;
    case MDRV_SEMUTEX_LOCK_WITHTIMEOUT: { /* arg: index, ms */
        int lockarg[2];

        if (copy_from_user(lockarg, uarg, sizeof(lockarg)))
            return -EFAULT;
        m = semutex_mutex_at(uarg, &index);
        if (!m)
            return -EINVAL;
        if (down_trylock(&m->sem)) {
            u64 t = ktime_get_boottime_ns();

            semutex_wait_ev(m);
            if (down_timeout(&m->sem, msecs_to_jiffies(lockarg[1]))) {
                mutex_lock(&semutex_lock);
                sem_ev(SEM_OP_TIMEOUT, m - semutex_mutexes, -ETIME, lockarg[1]);
                mutex_unlock(&semutex_lock);
                return -ETIME;
            }
            semutex_locked(m, file, SEM_OP_LOCK,
                           max_t(s32, 1, div_u64(ktime_get_boottime_ns() - t, NSEC_PER_MSEC)));
        } else {
            semutex_locked(m, file, SEM_OP_LOCK, 0);
        }
        return 0;
    }
    case MDRV_SEMUTEX_UNLOCK:
        m = semutex_mutex_at(uarg, &index);
        if (!m)
            return -EINVAL;
        mutex_lock(&semutex_lock);
        if (!m->locked_file) {
            sem_ev(SEM_OP_UNLOCK, m - semutex_mutexes, -EPERM, 0);
            mutex_unlock(&semutex_lock);
            return -EPERM;
        }
        if (!list_empty(&m->sem.wait_list)) /* only one somebody waits for; aux: held ms */
            sem_ev(SEM_OP_UNLOCK, m - semutex_mutexes, 0,
                   div_u64(ktime_get_boottime_ns() - m->since, NSEC_PER_MSEC));
        m->locked_file = NULL;
        mutex_unlock(&semutex_lock);
        up(&m->sem);
        return 0;
    case MDRV_SEMUTEX_CREATE_SHAREMEMORY: { /* arg: the size */
        u32 size;

        if (get_user(size, (u32 __user *)uarg))
            return -EFAULT;
        if (!size || (semutex_shm_capacity && size > semutex_shm_capacity))
            return -EINVAL;
        mutex_lock(&semutex_shm_lock);
        if (!f->shm_user) {
            if (!semutex_shm) {
                semutex_shm = vmalloc_user(PAGE_ALIGN(size));
                if (!semutex_shm) {
                    mutex_unlock(&semutex_shm_lock);
                    return -ENOMEM;
                }
                semutex_shm_size = PAGE_ALIGN(size);
                f->shm_first = true;
            }
            f->shm_user = true;
            semutex_shm_users++;
        }
        mutex_unlock(&semutex_shm_lock);
        return 0;
    }
    case MDRV_SEMUTEX_QUERY_ISFIRST_SHAREMEMORY:
        return f->shm_first;
    case MDRV_SEMUTEX_QUERY_SHAREMEMORY_SIZE:
        if (!semutex_shm_capacity)
            return -ENOTTY;
        return put_user(semutex_shm_capacity, (u32 __user *)uarg);
    }
    return unhandled("semutex", cmd, -ENOTTY);
}

static int semutex_mmap(struct file *file, struct vm_area_struct *vma)
{
    struct semutex_file *f = file->private_data;
    int ret = -EINVAL;

    mutex_lock(&semutex_shm_lock);
    if (f->shm_user && semutex_shm && vma->vm_pgoff == 0 &&
        vma->vm_end - vma->vm_start <= semutex_shm_size)
        ret = remap_vmalloc_range(vma, semutex_shm, 0);
    mutex_unlock(&semutex_shm_lock);
    return ret;
}

const struct file_operations semutex_fops = {
    .owner = THIS_MODULE,
    .open = semutex_open,
    .flush = semutex_flush,
    .release = semutex_release,
    .mmap = semutex_mmap,
    .unlocked_ioctl = semutex_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};

void semutex_init(void)
{
    semutex_new_init();
    for (int i = 0; i < SEMUTEX_MUTEXES; i++)
        sema_init(&semutex_mutexes[i].sem, 1);
}
