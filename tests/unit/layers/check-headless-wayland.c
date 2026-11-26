// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The headless compositor stand-in's Wayland socket (guest/layers/headless/src/surface-manager.c):
 * the stock launcher's --socket=NAME is found, NAME is held as libwayland's
 * wl_display_add_socket_auto would find a running compositor's (its lock taken, so the next
 * compositor moves on to another name; the socket listening, so a client's connect succeeds and
 * waits), group and mode as LG's compositor leaves them; no --socket, or no $XDG_RUNTIME_DIR, holds
 * nothing. Built for the host: the code is plain POSIX. */
#define main surface_manager_main
#include "../../../guest/layers/headless/src/surface-manager.c"
#undef main

static int failures;
static void check(int ok, const char *what)
{
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

/* whether another opener could take PATH's lock, as wl_display_add_socket_auto tries */
static bool lock_free(const char *path)
{
    int fd = open(path, O_CREAT | O_CLOEXEC | O_RDWR, 0660);
    bool free_ = fd >= 0 && !flock(fd, LOCK_EX | LOCK_NB);
    if (fd >= 0)
        close(fd);
    return free_;
}

static bool connects(const char *path)
{
    struct sockaddr_un sa = {.sun_family = AF_UNIX};
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    bool ok;
    snprintf(sa.sun_path, sizeof sa.sun_path, "%s", path);
    ok = fd >= 0 && !connect(fd, (struct sockaddr *)&sa, sizeof sa);
    if (fd >= 0)
        close(fd);
    return ok;
}

int main(void)
{
    char dir[] = "/tmp/check-headless-wayland-XXXXXX", path[256], lockp[256];
    char *with[] = {"surface-manager", "--socket=wayland-0", "-c/usr/share/weston/weston.ini",
                    NULL};
    char *without[] = {"surface-manager", "--socket=", NULL};
    struct stat st;

    check(!strcmp(socket_arg(3, with), "wayland-0"), "--socket=wayland-0 is found");
    check(!socket_arg(2, without), "an empty --socket= names nothing");
    check(!socket_arg(1, with), "no arguments, no socket");

    if (!mkdtemp(dir))
        return perror("mkdtemp"), 1;
    snprintf(path, sizeof path, "%s/wayland-0", dir);
    snprintf(lockp, sizeof lockp, "%s/wayland-0.lock", dir);

    unsetenv("XDG_RUNTIME_DIR");
    hold_wayland_socket("wayland-0");
    check(access(lockp, F_OK) && access(path, F_OK), "no XDG_RUNTIME_DIR: nothing made");

    setenv("XDG_RUNTIME_DIR", dir, 1);
    hold_wayland_socket("../wayland-0");
    check(access(lockp, F_OK), "a name with a slash: nothing made");

    hold_wayland_socket(NULL);
    check(access(lockp, F_OK), "no name: nothing made");

    hold_wayland_socket("wayland-0");
    check(!lock_free(lockp), "the lock is held: another compositor moves on to wayland-1");
    check(!stat(path, &st) && S_ISSOCK(st.st_mode), "the socket is there");
    check((st.st_mode & 0777) == 0660, "the socket is 0660");
    check(connects(path), "a client's connect succeeds (and then waits)");

    /* a second compositor on the same name: the lock is taken, the socket left alone */
    hold_wayland_socket("wayland-0");
    check(connects(path), "a second hold leaves the first socket");

    unlink(path);
    unlink(lockp);
    rmdir(dir);
    if (!failures)
        printf("ok\n");
    return failures != 0;
}
