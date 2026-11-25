// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The media helper's self-confinement (sandbox.h): no_new_privs, Landlock and a seccomp
 * allowlist, built here without libseccomp. */
#define _GNU_SOURCE
#include "sandbox.h"

#include <stdio.h>
#include <string.h>

#if defined(__linux__)

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <link.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <ucontext.h>
#include <unistd.h>

/* ------------------------------------------------------------------ Landlock */
/* the kernel's ABI (include/uapi/linux/landlock.h), here so that older headers build it */
#ifndef __NR_landlock_create_ruleset
#define __NR_landlock_create_ruleset 444
#define __NR_landlock_add_rule       445
#define __NR_landlock_restrict_self  446
#endif
#define LL_CREATE_RULESET_VERSION 1u
#define LL_RULE_PATH_BENEATH      1
#define LL_EXECUTE                (1ull << 0)
#define LL_WRITE_FILE             (1ull << 1)
#define LL_READ_FILE              (1ull << 2)
#define LL_READ_DIR               (1ull << 3)
#define LL_REMOVE_DIR             (1ull << 4)
#define LL_REMOVE_FILE            (1ull << 5)
#define LL_MAKE_CHAR              (1ull << 6)
#define LL_MAKE_DIR               (1ull << 7)
#define LL_MAKE_REG               (1ull << 8)
#define LL_MAKE_SOCK              (1ull << 9)
#define LL_MAKE_FIFO              (1ull << 10)
#define LL_MAKE_BLOCK             (1ull << 11)
#define LL_MAKE_SYM               (1ull << 12)
#define LL_REFER                  (1ull << 13) /* ABI 2 */
#define LL_TRUNCATE               (1ull << 14) /* ABI 3 */
#define LL_IOCTL_DEV              (1ull << 15) /* ABI 5 */
#define LL_NET_BIND_TCP           (1ull << 0)  /* ABI 4 */
#define LL_NET_CONNECT_TCP        (1ull << 1)
#define LL_SCOPE_ABSTRACT_UNIX    (1ull << 0) /* ABI 6 */
#define LL_SCOPE_SIGNAL           (1ull << 1)
/* what a rule on a file (not a directory) may hold */
#define LL_FILE_RIGHTS (LL_EXECUTE | LL_WRITE_FILE | LL_READ_FILE | LL_TRUNCATE | LL_IOCTL_DEV)

struct ll_ruleset_attr {
    uint64_t handled_access_fs, handled_access_net, scoped;
};
struct ll_path_beneath {
    uint64_t allowed_access;
    int32_t parent_fd;
} __attribute__((packed));

static uint64_t ll_handled;

static void ll_allow(int ruleset, const char *path, uint64_t access)
{
    int fd = open(path, O_PATH | O_CLOEXEC);
    if (fd < 0)
        return; /* not on this host */
    struct stat st;
    if (fstat(fd, &st) == 0 && !S_ISDIR(st.st_mode))
        access &= LL_FILE_RIGHTS;
    struct ll_path_beneath rule = {.allowed_access = access & ll_handled, .parent_fd = fd};
    if (rule.allowed_access)
        syscall(__NR_landlock_add_rule, ruleset, LL_RULE_PATH_BENEATH, &rule, 0);
    close(fd);
}

/* each directory of a colon-separated list */
static void ll_allow_list(int ruleset, const char *list, uint64_t access)
{
    if (!list)
        return;
    char *copy = strdup(list), *save = NULL;
    for (char *p = copy ? strtok_r(copy, ":", &save) : NULL; p; p = strtok_r(NULL, ":", &save))
        if (*p == '/')
            ll_allow(ruleset, p, access);
    free(copy);
}

static int ll_object(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    const char *name = info->dlpi_name;
    const char *slash = name ? strrchr(name, '/') : NULL;
    if (slash && slash != name && name[0] == '/') {
        char dir[4096];
        snprintf(dir, sizeof dir, "%.*s", (int)(slash - name), name);
        ll_allow(*(int *)data, dir, LL_READ_FILE | LL_READ_DIR | LL_EXECUTE);
    }
    return 0;
}

/* /dev entries starting with prefix */
static void ll_allow_dev(int ruleset, const char *prefix, uint64_t access)
{
    DIR *d = opendir("/dev");
    if (!d)
        return;
    struct dirent *e;
    size_t n = strlen(prefix);
    while ((e = readdir(d)))
        if (!strncmp(e->d_name, prefix, n)) {
            char path[300];
            snprintf(path, sizeof path, "/dev/%s", e->d_name);
            ll_allow(ruleset, path, access);
        }
    closedir(d);
}

/* the Landlock ABI applied, 0 for none */
static int landlock(const struct hm_sandbox *s)
{
    int abi = (int)syscall(__NR_landlock_create_ruleset, NULL, 0, LL_CREATE_RULESET_VERSION);
    if (abi < 1)
        return 0;
    struct ll_ruleset_attr attr = {0};
    attr.handled_access_fs = (LL_MAKE_SYM << 1) - 1;
    if (abi >= 2)
        attr.handled_access_fs |= LL_REFER;
    if (abi >= 3)
        attr.handled_access_fs |= LL_TRUNCATE;
    if (abi >= 5)
        attr.handled_access_fs |= LL_IOCTL_DEV;
    if (abi >= 4)
        attr.handled_access_net = LL_NET_BIND_TCP | LL_NET_CONNECT_TCP;
    if (abi >= 6)
        attr.scoped = LL_SCOPE_ABSTRACT_UNIX | (s->scope_signals ? LL_SCOPE_SIGNAL : 0);
    ll_handled = attr.handled_access_fs;
    int ruleset = (int)syscall(__NR_landlock_create_ruleset, &attr, sizeof attr, 0);
    if (ruleset < 0)
        return 0;
    const uint64_t ro = LL_READ_FILE | LL_READ_DIR | LL_EXECUTE;
    const uint64_t rw =
        ro | LL_WRITE_FILE | LL_REMOVE_FILE | LL_MAKE_REG | LL_MAKE_SOCK | LL_TRUNCATE | LL_REFER;
    const uint64_t dev = LL_READ_FILE | LL_READ_DIR | LL_WRITE_FILE | LL_IOCTL_DEV;
    static const char *const system[] = {"/usr", "/lib",  "/lib64", "/lib32", "/libx32",  "/etc",
                                         "/sys", "/proc", "/bin",   "/sbin",  "/run/udev"};
    for (size_t i = 0; i < sizeof system / sizeof *system; i++)
        ll_allow(ruleset, system[i], ro);
    /* its threads' names (pthread_setname_np opens /proc/self/task/N/comm with O_TRUNC; CUDA fails
     * without) */
    ll_allow(ruleset, "/proc/self", LL_READ_FILE | LL_READ_DIR | LL_WRITE_FILE | LL_TRUNCATE);
    dl_iterate_phdr(ll_object, &ruleset);
    ll_allow_list(ruleset, getenv("LD_LIBRARY_PATH"), ro);
    ll_allow_list(ruleset, getenv("LIBVA_DRIVERS_PATH"), ro);
    for (int i = 0; i < 8 && s->rw[i]; i++)
        ll_allow(ruleset, s->rw[i], rw);
    ll_allow(ruleset, "/dev/shm", rw);
    static const char *const null[] = {"/dev/null", "/dev/zero", "/dev/full", "/dev/random",
                                       "/dev/urandom"};
    for (size_t i = 0; i < sizeof null / sizeof *null; i++)
        ll_allow(ruleset, null[i], LL_READ_FILE | LL_WRITE_FILE);
    if (s->devices) {
        ll_allow(ruleset, "/dev/dri", dev);
        ll_allow(ruleset, "/dev/dxg", dev);
        ll_allow(ruleset, "/dev/dma_heap", dev);
        ll_allow_dev(ruleset, "nvidia", dev);
        ll_allow_dev(ruleset, "video", dev);
        ll_allow_dev(ruleset, "media", dev);
    }
    int r = (int)syscall(__NR_landlock_restrict_self, ruleset, 0);
    close(ruleset);
    return r == 0 ? abi : 0;
}

/* ------------------------------------------------------------------ seccomp */
#if defined(__x86_64__)
#define SB_ARCH AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
#define SB_ARCH AUDIT_ARCH_AARCH64
#endif

#ifdef SB_ARCH
/* What the helper calls once it runs: the union of strace -f over the integration tests
 * (tests/integration/media-helper, software decoding and CUDA) and a VM's playback with the
 * viewer attached, the libc's equivalents on aarch64 (no open, poll, stat, ...), and what any
 * glibc program may call in rarer paths (mremap from realloc, the clocks, signal returns). */
static const int allowed[] = {
    /* memory */
    __NR_brk,
    __NR_mmap,
    __NR_munmap,
    __NR_mremap,
    __NR_mprotect,
    __NR_madvise,
    __NR_get_mempolicy,
    __NR_membarrier,
    /* threads and time */
    __NR_futex,
    __NR_set_robust_list,
    __NR_get_robust_list,
    __NR_rseq,
    __NR_sched_yield,
    __NR_sched_getaffinity,
    __NR_sched_get_priority_max,
    __NR_sched_get_priority_min,
    __NR_sched_getparam,
    __NR_sched_getscheduler,
    __NR_nanosleep,
    __NR_clock_nanosleep,
    __NR_clock_gettime,
    __NR_clock_getres,
    __NR_gettimeofday,
    __NR_exit,
    __NR_exit_group,
    __NR_restart_syscall,
    __NR_set_tid_address,
    /* signals: SIGSYS's handler, abort */
    __NR_rt_sigprocmask,
    __NR_rt_sigreturn,
    __NR_sigaltstack,
    /* identity */
    __NR_getpid,
    __NR_gettid,
    __NR_getppid,
    __NR_getuid,
    __NR_geteuid,
    __NR_getgid,
    __NR_getegid,
    __NR_uname,
    __NR_sysinfo,
    __NR_getrandom,
    __NR_getrusage,
    __NR_times,
    __NR_prctl,
    __NR_getcwd,
    /* files: Landlock decides which */
    __NR_openat,
    __NR_close,
    __NR_read,
    __NR_write,
    __NR_readv,
    __NR_writev,
    __NR_pread64,
    __NR_pwrite64,
    __NR_lseek,
    __NR_fstat,
    __NR_newfstatat,
    __NR_statx,
    __NR_statfs,
    __NR_fstatfs,
    __NR_getdents64,
    __NR_readlinkat,
    __NR_faccessat,
    __NR_unlinkat,
    __NR_renameat2,
    __NR_mkdirat,
    __NR_ftruncate,
    __NR_fallocate,
    __NR_fcntl,
    __NR_dup,
    __NR_dup3,
    __NR_flock,
    __NR_fsync,
    __NR_fdatasync,
    __NR_memfd_create,
    __NR_eventfd2,
    __NR_pipe2,
    __NR_ppoll,
    __NR_pselect6,
    __NR_epoll_create1,
    __NR_epoll_ctl,
    __NR_epoll_pwait,
#ifdef __NR_faccessat2
    __NR_faccessat2,
#endif
#ifdef __NR_renameat
    __NR_renameat,
#endif
#ifdef __NR_open
    __NR_open,
    __NR_stat,
    __NR_lstat,
    __NR_access,
    __NR_readlink,
    __NR_unlink,
    __NR_rename,
    __NR_mkdir,
    __NR_poll,
    __NR_select,
    __NR_dup2,
    __NR_pipe,
    __NR_epoll_wait,
    __NR_getdents,
    __NR_arch_prctl,
    __NR_time,
#endif
    /* sockets: AF_UNIX only (socket, socketpair below) */
    __NR_connect,
    __NR_accept4,
    __NR_bind,
    __NR_listen,
    __NR_sendto,
    __NR_recvfrom,
    __NR_sendmsg,
    __NR_recvmsg,
    __NR_shutdown,
    __NR_getsockopt,
    __NR_setsockopt,
    __NR_getsockname,
    __NR_getpeername,
};
/* the process ends: what a decoder bug would want next */
static const int forbidden[] = {
    __NR_execve,
    __NR_execveat,
    __NR_ptrace,
    __NR_process_vm_readv,
    __NR_process_vm_writev,
    __NR_bpf,
    __NR_perf_event_open,
    __NR_userfaultfd,
    __NR_keyctl,
    __NR_add_key,
    __NR_request_key,
    __NR_mount,
    __NR_umount2,
    __NR_unshare,
    __NR_setns,
    __NR_pivot_root,
    __NR_chroot,
    __NR_io_uring_setup,
    __NR_io_uring_enter,
    __NR_io_uring_register,
    __NR_init_module,
    __NR_finit_module,
    __NR_delete_module,
    __NR_kexec_load,
    __NR_open_by_handle_at,
    __NR_name_to_handle_at,
    __NR_fsopen,
    __NR_fsmount,
    __NR_fsconfig,
    __NR_move_mount,
    __NR_open_tree,
    __NR_personality,
    __NR_process_madvise,
    __NR_pidfd_getfd,
    __NR_kcmp,
    __NR_setuid,
    __NR_setgid,
    __NR_setreuid,
    __NR_setregid,
    __NR_setresuid,
    __NR_setresgid,
    __NR_setgroups,
    __NR_capset,
};
#endif

#define CLONE_NAMESPACES                                                                           \
    0x7e020080u /* NEWNS, NEWCGROUP, NEWUTS, NEWIPC, NEWUSER, NEWPID, NEWNET, NEWTIME */

static unsigned char reported[1024 / 8];

static void on_sigsys(int sig, siginfo_t *info, void *context)
{
    (void)sig;
    int nr = info->si_syscall;
    if (nr >= 0 && nr < 1024 &&
        !(__atomic_fetch_or(&reported[nr / 8], (unsigned char)(1u << nr % 8), __ATOMIC_RELAXED) &
          (1u << nr % 8))) {
        char line[64] = "media-helper: seccomp: system call ";
        size_t n = strlen(line);
        char digits[12];
        int k = 0;
        unsigned v = (unsigned)nr;
        do
            digits[k++] = (char)('0' + v % 10);
        while ((v /= 10));
        while (k)
            line[n++] = digits[--k];
        memcpy(line + n, " refused\n", 9);
        if (write(2, line, n + 9) < 0) {
            /* nothing to do */
        }
    }
    ucontext_t *uc = context;
#if defined(__x86_64__)
    uc->uc_mcontext.gregs[REG_RAX] = -ENOSYS;
#elif defined(__aarch64__)
    uc->uc_mcontext.regs[0] = (unsigned long long)-ENOSYS;
#else
    (void)uc;
#endif
}

#ifdef SB_ARCH
#define LOAD_NR     BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr))
#define LOAD_ARG(n) BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[n]))
#define RET(v)      BPF_STMT(BPF_RET | BPF_K, (v))
#define ALLOW       RET(SECCOMP_RET_ALLOW)
#define KILL        RET(SECCOMP_RET_KILL_PROCESS)
#define FAIL(e)     RET(SECCOMP_RET_ERRNO | (e))

static int seccomp_filter(void)
{
    struct sock_filter f[512];
    unsigned n = 0;
#define EMIT(...)                                                                                  \
    do {                                                                                           \
        struct sock_filter x_[] = {__VA_ARGS__};                                                   \
        for (size_t i_ = 0; i_ < sizeof x_ / sizeof *x_; i_++)                                     \
            f[n++] = x_[i_];                                                                       \
    } while (0)
    /* only this architecture's system calls */
    EMIT(BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
         BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SB_ARCH, 1, 0), KILL, LOAD_NR);
#if defined(__x86_64__)
    EMIT(BPF_JUMP(BPF_JMP | BPF_JGE | BPF_K, 0x40000000u, 0, 1), KILL); /* x32 */
#endif
    for (size_t i = 0; i < sizeof allowed / sizeof *allowed; i++)
        EMIT(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (unsigned)allowed[i], 0, 1), ALLOW);
    for (size_t i = 0; i < sizeof forbidden / sizeof *forbidden; i++)
        EMIT(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (unsigned)forbidden[i], 0, 1), KILL);
    unsigned self = (unsigned)getpid();
    /* socket, socketpair: AF_UNIX only */
    EMIT(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_socket, 1, 0),
         BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_socketpair, 0, 4), LOAD_ARG(0),
         BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AF_UNIX, 0, 1), ALLOW, FAIL(EAFNOSUPPORT));
    /* clone: threads only, in no new namespace */
    EMIT(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone, 0, 6), LOAD_ARG(0),
         BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, CLONE_NAMESPACES, 0, 1), KILL,
         BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, CLONE_THREAD, 0, 1), ALLOW, FAIL(EPERM));
    /* clone3's flags are behind a pointer: the libc falls back to clone */
    EMIT(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone3, 0, 1), FAIL(ENOSYS));
    /* signals to this process only (abort, pthread_kill); kill(pid, 0) asks if pid lives
     * (--pid) */
    EMIT(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_tgkill, 0, 4), LOAD_ARG(0),
         BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, self, 0, 1), ALLOW, FAIL(EPERM));
    EMIT(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_kill, 0, 6), LOAD_ARG(0),
         BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, self, 2, 0), LOAD_ARG(1),
         BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, 1), ALLOW, FAIL(EPERM));
    /* prlimit64 on itself only */
    EMIT(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_prlimit64, 0, 4), LOAD_ARG(0),
         BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, 1), ALLOW, FAIL(EPERM));
    /* SIGSYS stays this file's */
    EMIT(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_rt_sigaction, 0, 4), LOAD_ARG(0),
         BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SIGSYS, 0, 1), FAIL(EPERM), ALLOW);
    /* ioctl: devices yes, a terminal's input no */
    EMIT(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_ioctl, 0, 5), LOAD_ARG(1),
         BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, TIOCSTI, 1, 0),
         BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, TIOCLINUX, 0, 1), FAIL(EPERM), ALLOW);
    EMIT(RET(SECCOMP_RET_TRAP));
#undef EMIT
    struct sock_fprog prog = {.len = (unsigned short)n, .filter = f};
    struct sigaction sa = {.sa_sigaction = on_sigsys, .sa_flags = SA_SIGINFO | SA_NODEFER};
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGSYS, &sa, NULL) < 0)
        return -1;
    sigset_t set;
    sigemptyset(&set);
    sigaddset(&set, SIGSYS);
    pthread_sigmask(SIG_UNBLOCK, &set, NULL);
    return (int)syscall(__NR_seccomp, SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC, &prog);
}
#endif

int hm_sandbox(const struct hm_sandbox *s, char *what, size_t len)
{
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0) {
        snprintf(what, len, "no_new_privs: %s", strerror(errno));
        return -1;
    }
    int abi = landlock(s);
#ifdef SB_ARCH
    if (seccomp_filter() != 0) {
        snprintf(what, len, "seccomp: %s", strerror(errno));
        return -1;
    }
    const char *filter = "seccomp";
#else
    const char *filter = "no seccomp filter for this architecture";
#endif
    if (abi)
        snprintf(what, len, "no_new_privs, Landlock ABI %d, %s", abi, filter);
    else
        snprintf(what, len, "no_new_privs, no Landlock, %s", filter);
    return 0;
}

#else

int hm_sandbox(const struct hm_sandbox *s, char *what, size_t len)
{
    (void)s;
    snprintf(what, len, "none on this system");
    return 0;
}

#endif
