// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The media helper's self-confinement (host/media/sandbox.h), from inside: run by
 * test_media_sandbox.py as check-sandbox RW_DIR OUTSIDE_FILE [hw]. After hm_sandbox it may write
 * in RW_DIR but not read OUTSIDE_FILE (when the kernel has Landlock), opens no TCP socket, makes
 * no process, signals nothing but itself, and a system call off the list fails with ENOSYS; each
 * result is a line "name: ok" or "name: FAIL ...". Last, execve: the filter ends the process with
 * SIGSYS, which the test expects. Exit 77 on a host without seccomp for this architecture. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "../../../host/media/sandbox.h"

static int failed;

static void check(const char *name, int ok, const char *detail)
{
    if (ok)
        printf("%s: ok\n", name);
    else
        printf("%s: FAIL %s\n", name, detail);
    fflush(stdout);
    failed |= !ok;
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: check-sandbox RW_DIR OUTSIDE_FILE [hw]\n");
        return 2;
    }
    struct hm_sandbox box = {.rw = {argv[1]}, .devices = argc > 3, .scope_signals = 1};
    char what[256];
    if (hm_sandbox(&box, what, sizeof what) < 0) {
        printf("sandbox: FAIL %s\n", what);
        return 1;
    }
    printf("sandbox: %s\n", what);
    if (!strstr(what, "seccomp")) {
        printf("no seccomp filter here\n");
        return 77;
    }
    int landlock = strstr(what, "Landlock ABI") != NULL;

    char path[4200];
    snprintf(path, sizeof path, "%s/written", argv[1]);
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    check("write in its directory", fd >= 0, strerror(errno));
    if (fd >= 0)
        close(fd);

    fd = open(argv[2], O_RDONLY | O_CLOEXEC);
    int e = errno;
    if (landlock)
        check("read outside", fd < 0 && e == EACCES, fd >= 0 ? "opened" : strerror(e));
    if (fd >= 0)
        close(fd);

    fd = open("/usr/lib", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    check("read the system's libraries", fd >= 0, strerror(errno));
    if (fd >= 0)
        close(fd);

    fd = socket(AF_INET, SOCK_STREAM, 0);
    e = errno;
    check("no TCP socket", fd < 0 && e == EAFNOSUPPORT, fd >= 0 ? "made" : strerror(e));
    fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    check("unix socket", fd >= 0, strerror(errno));
    if (fd >= 0)
        close(fd);

    pid_t pid = fork();
    if (pid == 0)
        _exit(0);
    e = errno;
    check("no fork", pid < 0 && (e == EPERM || e == ENOSYS), pid > 0 ? "forked" : strerror(e));

    int r = kill(getppid(), SIGCONT); /* harmless if it went */
    e = errno;
    check("no signal to another process", r < 0 && e == EPERM, r == 0 ? "sent" : strerror(e));
    check("kill(pid, 0) still asks", kill(getpid(), 0) == 0, strerror(errno));

    long l = syscall(__NR_acct, NULL);
    e = errno;
    check("off the list: ENOSYS", l < 0 && e == ENOSYS, l == 0 ? "ran" : strerror(e));

    printf("checks: %s\nexecve: next\n", failed ? "FAIL" : "ok");
    fflush(stdout);
    char *args[] = {"/bin/true", NULL};
    execve(args[0], args, environ);
    printf("execve: FAIL returned: %s\n", strerror(errno));
    return 1;
}
