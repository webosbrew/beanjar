// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef HOST_MEDIA_SANDBOX_H
#define HOST_MEDIA_SANDBOX_H
/* The media helper's own confinement, applied by itself once its setup is done (its logs, the
 * pool, its sockets): whatever started it, bubblewrap or not. Linux only; elsewhere nothing is
 * applied and hm_sandbox says so.
 *
 * - no_new_privs.
 * - Landlock, at the ABI the kernel has: read and execute on the system's files (/usr, /lib*,
 *   /etc, /sys, /proc), the directories of the libraries loaded at the time and those on
 *   LD_LIBRARY_PATH and LIBVA_DRIVERS_PATH (what FFmpeg opens later: a GPU driver); read and write
 *   on rw[] (the helper's own directories) and /dev/shm, the device nodes for hardware decoding
 *   when devices is set (/dev/dri, /dev/dxg, /dev/nvidia*, /dev/video*, /dev/media*,
 *   /dev/dma_heap); nothing else. No TCP (ABI 4), no abstract unix sockets out of it and, with
 *   scope_signals, no signals out of it (ABI 6; the helper then watches QEMU with a pidfd).
 * - seccomp: an allowlist of the system calls the helper makes (measured with strace over the
 *   integration tests and a VM's playback, software and hardware decoding). execve, ptrace,
 *   process_vm_*, bpf, perf_event_open, userfaultfd, keys, mount and namespaces, io_uring, module
 *   loading and the like end the process; sockets other than AF_UNIX, fork (clone without
 *   CLONE_THREAD), signals to other processes, TIOCSTI and a new SIGSYS handler fail; any other
 *   call not on the list fails with ENOSYS and is reported once on stderr ("seccomp: system call N
 *   refused"), so a library's rarer path degrades instead of killing the VM's media. x86_64 and
 *   aarch64; other architectures get no filter. */
#include <stddef.h>

struct hm_sandbox {
    const char *rw[8]; /* directories the helper writes in; NULL ends */
    int devices;       /* hardware decoding: its device nodes */
    int scope_signals; /* no signals out of the domain (Landlock ABI 6) */
};

/* Applies what this host has of the above. 0, or -1 when no_new_privs or the filter could not be
 * set (nothing to trust then); what was applied is described in what (one line). */
int hm_sandbox(const struct hm_sandbox *s, char *what, size_t len);

#endif
