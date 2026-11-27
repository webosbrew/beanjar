// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* `beanjar shell`'s agent, webos.shell <-> commands inside webOS, in the arm64 initramfs.
 *
 * The host's relay (host/core/beanjar/access/guestshell.py) sends sessions over the webos.shell
 * virtio-serial port (include/webos-shell.h). Each one runs /bin/webos-enter with its words (none:
 * a login shell), as root in webOS's namespaces and chroot, as the debug console's webos-enter
 * does: on a terminal of its own (a pty of webOS's own devpts, so that it is /dev/pts/N in webOS
 * too; before webOS mounts one, of the initramfs's), or with pipes for stdin, stdout and stderr.
 * Each command leads a session and process group of its own, which the host's signals reach.
 *
 * Nothing of a session waits for another: the port is read all the time (a session takes at most
 * its window of stdin), and a command's output is read only while the host has room for it.
 * A hang-up of the port (no host connected) or a host's HELLO ends every session: their process
 * groups get SIGHUP, and SIGKILL 3 s later if they are still there. Without the port it does
 * nothing. Errors go to stderr (guest/initramfs/rootfs/init: /run/virtio-shell.log). */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include "../../../include/webos-shell.h"

#ifndef TIOCGPTPEER
#define TIOCGPTPEER _IO('T', 0x41)
#endif

#define SESSIONS  32
#define OUT_LIMIT (1u << 20) /* port output held before the commands' output waits */
#define CHUNK     16384u
#define KILL_MS   3000 /* SIGHUP to SIGKILL of a session given up */
#define DRAIN_MS  1000 /* after the command ends, its output's last silence */

struct buf {
    unsigned char *p;
    size_t len, cap;
};

struct session {
    int used, tty, abandoned, in_eof, exited, status;
    uint32_t id, credit;
    pid_t pid; /* until reaped */
    int in, out, err;
    struct buf input;
    int64_t quiet_since, kill_at;
};

static struct session sessions[SESSIONS];
static struct buf ibuf, obuf;
static int port = -1, chld_pipe[2] = {-1, -1};

static int64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void sleep_ms(unsigned ms)
{
    struct timespec ts = {ms / 1000, (long)(ms % 1000) * 1000000L};
    while (nanosleep(&ts, &ts) < 0 && errno == EINTR)
        ;
}

/* The device of the virtio port called name, or "" when there is none. */
static void find_port(const char *name, char *dev, size_t size)
{
    glob_t paths;
    char want[64];
    dev[0] = 0;
    snprintf(want, sizeof want, "%s\n", name);
    memset(&paths, 0, sizeof paths);
    if (glob("/sys/class/virtio-ports/*/name", 0, NULL, &paths))
        return;
    for (size_t i = 0; i < paths.gl_pathc && !dev[0]; i++) {
        char line[64], leaf[64];
        FILE *f = fopen(paths.gl_pathv[i], "r");
        if (!f)
            continue;
        if (fgets(line, sizeof line, f) && !strcmp(line, want) &&
            sscanf(paths.gl_pathv[i], "/sys/class/virtio-ports/%63[^/]/name", leaf) == 1)
            snprintf(dev, size, "/dev/%s", leaf);
        fclose(f);
    }
    globfree(&paths);
}

static int put(struct buf *b, const void *data, size_t n)
{
    if (b->len + n > b->cap) {
        size_t cap = b->cap ? b->cap : 65536;
        while (cap < b->len + n)
            cap *= 2;
        unsigned char *p = realloc(b->p, cap);
        if (!p)
            return -1;
        b->p = p, b->cap = cap;
    }
    memcpy(b->p + b->len, data, n);
    b->len += n;
    return 0;
}

static void take(struct buf *b, size_t n)
{
    memmove(b->p, b->p + n, b->len - n);
    b->len -= n;
}

static void send_frame(uint16_t type, uint32_t id, const void *data, uint32_t len)
{
    struct webos_shell_frame h = {WEBOS_SHELL_MAGIC, type, 0, id, len};
    if (put(&obuf, &h, sizeof h) < 0 || (len && put(&obuf, data, len) < 0))
        fprintf(stderr, "virtio-shell: out of memory for the port\n");
}

static void send_word(uint16_t type, uint32_t id, uint32_t word)
{
    send_frame(type, id, &word, sizeof word);
}

static void send_hello(uint32_t nonce)
{
    uint32_t w[2] = {WEBOS_SHELL_VERSION, nonce};
    send_frame(WEBOS_SHELL_HELLO, 0, w, sizeof w);
}

static void close_fd(int *fd)
{
    if (*fd >= 0)
        close(*fd);
    *fd = -1;
}

static void close_io(struct session *s)
{
    if (s->in == s->out)
        s->in = -1;
    close_fd(&s->in);
    close_fd(&s->out);
    close_fd(&s->err);
    free(s->input.p);
    memset(&s->input, 0, sizeof s->input);
}

static void free_session(struct session *s)
{
    close_io(s);
    memset(s, 0, sizeof *s);
    s->in = s->out = s->err = -1;
}

/* The host gives s up (or is gone): its process group gets SIGHUP, and SIGKILL later. */
static void abandon(struct session *s)
{
    close_io(s);
    if (!s->pid) {
        free_session(s);
        return;
    }
    kill(-s->pid, SIGHUP);
    s->abandoned = 1;
    s->kill_at = now_ms() + KILL_MS;
}

static void abandon_all(void)
{
    for (int i = 0; i < SESSIONS; i++)
        if (sessions[i].used && !sessions[i].abandoned)
            abandon(&sessions[i]);
}

static struct session *find(uint32_t id)
{
    for (int i = 0; i < SESSIONS; i++)
        if (sessions[i].used && !sessions[i].abandoned && sessions[i].id == id)
            return &sessions[i];
    return NULL;
}

/* webOS's init, PID 1 of its namespace (as webos-enter finds it): its pid here, or 0 */
static pid_t webos_init(void)
{
    glob_t g;
    pid_t pid = 0;
    memset(&g, 0, sizeof g);
    if (glob("/proc/[0-9]*/status", 0, NULL, &g))
        return 0;
    for (size_t i = 0; i < g.gl_pathc && !pid; i++) {
        char line[256];
        FILE *f = fopen(g.gl_pathv[i], "r");
        if (!f)
            continue;
        while (fgets(line, sizeof line, f)) {
            long a, b;
            int n;
            if (strncmp(line, "NSpid:", 6))
                continue;
            if (sscanf(line + 6, "%ld %ld%n", &a, &b, &n) == 2 && b == 1 && line[6 + n] == '\n')
                pid = (pid_t)a;
            break;
        }
        fclose(f);
    }
    globfree(&g);
    return pid;
}

/* a pty's master: of webOS's devpts if it has one mounted, else of the initramfs's */
static int open_ptmx(void)
{
    char path[64];
    pid_t init = webos_init();
    if (init) {
        snprintf(path, sizeof path, "/proc/%d/root/dev/pts/ptmx", (int)init);
        int fd = open(path, O_RDWR | O_NOCTTY | O_CLOEXEC);
        if (fd >= 0)
            return fd;
    }
    return open("/dev/pts/ptmx", O_RDWR | O_NOCTTY | O_CLOEXEC);
}

/* its slave, by the master (TIOCGPTPEER: the path is another namespace's), else by name */
static int open_slave(int master)
{
    char name[64];
    int unlock = 0;
    if (ioctl(master, TIOCSPTLCK, &unlock) < 0)
        return -1;
    int fd = ioctl(master, TIOCGPTPEER, O_RDWR | O_NOCTTY | O_CLOEXEC);
    if (fd >= 0 || ptsname_r(master, name, sizeof name))
        return fd;
    return open(name, O_RDWR | O_NOCTTY | O_CLOEXEC);
}

static void nonblock(int fd)
{
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
}

/* Starts session id: "" when it runs, else why not. */
static const char *open_session(uint32_t id, const unsigned char *p, uint32_t len)
{
    static char why[160];
    struct webos_shell_open o;
    struct session *s = NULL;
    if (find(id))
        return "the session is open already";
    if (len < sizeof o)
        return "a short OPEN";
    memcpy(&o, p, sizeof o);
    if (sizeof o + o.term_len > len || (len > sizeof o + o.term_len && p[len - 1]))
        return "a malformed OPEN";
    for (int i = 0; i < SESSIONS && !s; i++)
        if (!sessions[i].used)
            s = &sessions[i];
    if (!s)
        return "too many sessions";
    char term[64] = "";
    if (o.term_len && o.term_len < sizeof term)
        memcpy(term, p + sizeof o, o.term_len);
    /* the words: webos-enter's arguments */
    const unsigned char *w = p + sizeof o + o.term_len, *end = p + len;
    int argc = 1;
    for (const unsigned char *q = w; q < end; q++)
        argc += !*q;
    char **argv = calloc((size_t)argc + 1, sizeof *argv);
    if (!argv)
        return "out of memory";
    argv[0] = "webos-enter";
    for (int i = 1; i < argc; i++) {
        argv[i] = (char *)w;
        w += strlen((const char *)w) + 1;
    }

    int fds[6] = {-1, -1, -1, -1, -1, -1}, master = -1, slave = -1;
    const char *fail = NULL;
    if (o.flags & WEBOS_SHELL_TTY) {
        struct winsize ws = {o.rows, o.cols, 0, 0};
        if ((master = open_ptmx()) < 0 || (slave = open_slave(master)) < 0) {
            snprintf(why, sizeof why, "no pty: %s", strerror(errno));
            fail = why;
        } else
            ioctl(master, TIOCSWINSZ, &ws);
    } else if (pipe2(fds, O_CLOEXEC) || pipe2(fds + 2, O_CLOEXEC) || pipe2(fds + 4, O_CLOEXEC)) {
        fail = "no pipes";
    }
    pid_t pid = fail ? -1 : fork();
    if (pid == 0) {
        sigset_t none;
        sigemptyset(&none);
        sigprocmask(SIG_SETMASK, &none, NULL);
        signal(SIGPIPE, SIG_DFL);
        signal(SIGCHLD, SIG_DFL);
        setsid();
        if (slave >= 0) {
            ioctl(slave, TIOCSCTTY, 0);
            dup2(slave, 0), dup2(slave, 1), dup2(slave, 2);
        } else {
            dup2(fds[0], 0), dup2(fds[3], 1), dup2(fds[5], 2);
        }
        if (term[0])
            setenv("TERM", term, 1);
        else
            unsetenv("TERM");
        execv("/bin/webos-enter", argv);
        dprintf(2, "virtio-shell: /bin/webos-enter: %s\n", strerror(errno));
        _exit(127);
    }
    free(argv);
    if (pid < 0) {
        if (!fail)
            snprintf(why, sizeof why, "fork: %s", strerror(errno));
        for (int i = 0; i < 6; i++)
            close_fd(&fds[i]);
        close_fd(&master);
        close_fd(&slave);
        return fail ? fail : why;
    }
    memset(s, 0, sizeof *s);
    s->used = 1, s->id = id, s->pid = pid, s->credit = WEBOS_SHELL_WINDOW;
    s->tty = !!(o.flags & WEBOS_SHELL_TTY);
    if (s->tty) {
        close(slave);
        s->in = s->out = master;
        s->err = -1;
    } else {
        close(fds[0]), close(fds[3]), close(fds[5]);
        s->in = fds[1], s->out = fds[2], s->err = fds[4];
        nonblock(s->in);
        nonblock(s->err);
    }
    nonblock(s->out);
    return "";
}

static void on_frame(const struct webos_shell_frame *h, const unsigned char *p, int *synced)
{
    uint32_t word = 0;
    if (h->len >= 4)
        memcpy(&word, p, 4);
    if (h->type == WEBOS_SHELL_HELLO) {
        uint32_t nonce = 0;
        if (h->len >= 8)
            memcpy(&nonce, p + 4, 4);
        abandon_all();
        *synced = 1;
        send_hello(nonce);
        return;
    }
    if (!*synced)
        return;
    if (h->type == WEBOS_SHELL_OPEN) {
        const char *why = open_session(h->session, p, h->len);
        if (why[0]) {
            fprintf(stderr, "virtio-shell: session %u: %s\n", h->session, why);
            send_frame(WEBOS_SHELL_CLOSE, h->session, why, (uint32_t)strlen(why));
        }
        return;
    }
    struct session *s = find(h->session);
    if (!s) {
        if (h->type != WEBOS_SHELL_CLOSE && h->type != WEBOS_SHELL_ACK)
            send_frame(WEBOS_SHELL_CLOSE, h->session, "no such session", 15);
        return;
    }
    switch (h->type) {
    case WEBOS_SHELL_STDIN:
        if (!h->len)
            s->in_eof = !s->tty;
        else if (s->in < 0)
            send_word(WEBOS_SHELL_ACK, s->id, h->len); /* the command's stdin is gone */
        else if (s->input.len + h->len > 2 * WEBOS_SHELL_WINDOW || put(&s->input, p, h->len))
            fprintf(stderr, "virtio-shell: session %u: stdin over its window\n", s->id);
        break;
    case WEBOS_SHELL_WINSIZE:
        if (s->tty && h->len >= 4) {
            uint16_t rc[2];
            memcpy(rc, p, 4);
            struct winsize ws = {rc[0], rc[1], 0, 0};
            if (s->out >= 0)
                ioctl(s->out, TIOCSWINSZ, &ws);
        }
        break;
    case WEBOS_SHELL_SIGNAL:
        if (s->pid && (word == WEBOS_SHELL_SIGHUP || word == WEBOS_SHELL_SIGINT ||
                       word == WEBOS_SHELL_SIGQUIT || word == WEBOS_SHELL_SIGKILL ||
                       word == WEBOS_SHELL_SIGTERM))
            kill(-s->pid, (int)word);
        break;
    case WEBOS_SHELL_ACK:
        s->credit = word > WEBOS_SHELL_WINDOW - s->credit ? WEBOS_SHELL_WINDOW : s->credit + word;
        break;
    case WEBOS_SHELL_CLOSE:
        abandon(s);
        break;
    }
}

/* the frames in ibuf; before a HELLO, nothing else counts */
static void parse(int *synced)
{
    for (;;) {
        struct webos_shell_frame h;
        if (ibuf.len < sizeof h)
            return;
        memcpy(&h, ibuf.p, sizeof h);
        if (h.magic != WEBOS_SHELL_MAGIC || h.len > WEBOS_SHELL_MAX) {
            /* lost: look for the next magic */
            uint32_t magic = WEBOS_SHELL_MAGIC;
            unsigned char *m = memmem(ibuf.p + 1, ibuf.len - 1, &magic, sizeof magic);
            take(&ibuf, m ? (size_t)(m - ibuf.p) : ibuf.len - 3);
            continue;
        }
        if (ibuf.len < sizeof h + h.len)
            return;
        on_frame(&h, ibuf.p + sizeof h, synced);
        take(&ibuf, sizeof h + h.len);
    }
}

/* the command's output of fd (out or err) to the host as type */
static void pump_out(struct session *s, int *fd, uint16_t type)
{
    static unsigned char buf[CHUNK];
    size_t want = s->credit < CHUNK ? s->credit : CHUNK;
    ssize_t n = read(*fd, buf, want);
    if (n < 0 && (errno == EAGAIN || errno == EINTR))
        return;
    if (n <= 0) { /* its end (EIO: a pty without a slave open) */
        if (*fd == s->in)
            s->in = -1;
        close_fd(fd);
        return;
    }
    send_frame(type, s->id, buf, (uint32_t)n);
    s->credit -= (uint32_t)n;
    s->quiet_since = now_ms();
}

static void pump_in(struct session *s)
{
    ssize_t n = write(s->in, s->input.p, s->input.len);
    if (n < 0 && (errno == EAGAIN || errno == EINTR))
        return;
    if (n < 0) { /* the command closed its stdin: the rest is taken all the same */
        send_word(WEBOS_SHELL_ACK, s->id, (uint32_t)s->input.len);
        s->input.len = 0;
        if (!s->tty)
            close_fd(&s->in);
        return;
    }
    take(&s->input, (size_t)n);
    send_word(WEBOS_SHELL_ACK, s->id, (uint32_t)n);
}

static void reap(void)
{
    char drain[64];
    while (read(chld_pipe[0], drain, sizeof drain) > 0)
        ;
    for (;;) {
        int st;
        pid_t pid = waitpid(-1, &st, WNOHANG);
        if (pid <= 0)
            return;
        for (int i = 0; i < SESSIONS; i++) {
            struct session *s = &sessions[i];
            if (!s->used || s->pid != pid)
                continue;
            s->pid = 0;
            if (s->abandoned) {
                free_session(s);
            } else {
                s->exited = 1;
                s->status = WIFSIGNALED(st) ? 128 + WTERMSIG(st) : WEXITSTATUS(st);
                s->quiet_since = now_ms();
            }
        }
    }
}

/* a command that ended, once its output is all sent (or silent for DRAIN_MS while the host had
 * room: a process it left behind holds it): its status, and the session is over */
static void finish(struct session *s, int64_t now)
{
    if (!s->exited)
        return;
    if ((s->out >= 0 || s->err >= 0) && (s->credit == 0 || now - s->quiet_since < DRAIN_MS))
        return;
    send_word(WEBOS_SHELL_EXIT, s->id, (uint32_t)s->status);
    free_session(s);
}

static void on_chld(int sig)
{
    (void)sig;
    int e = errno;
    if (write(chld_pipe[1], "c", 1) < 0) {
    }
    errno = e;
}

int main(void)
{
    char dev[96];
    int host = 1, synced = 0, failed = 0;

    find_port("webos.shell", dev, sizeof dev);
    if (!dev[0])
        return 0;
    pid_t pid = fork();
    if (pid < 0) {
        perror("virtio-shell: fork");
        return 1;
    }
    if (pid > 0)
        return 0;
    setsid();
    /* the initramfs's own ptys, for a session before webOS mounts its devpts */
    mkdir("/dev/pts", 0755);
    if (mount("devpts", "/dev/pts", "devpts", MS_NOSUID | MS_NOEXEC, "ptmxmode=0600") &&
        errno != EBUSY)
        fprintf(stderr, "virtio-shell: devpts: %s\n", strerror(errno));
    for (int i = 0; i < SESSIONS; i++)
        sessions[i].in = sessions[i].out = sessions[i].err = -1;
    if (pipe2(chld_pipe, O_CLOEXEC | O_NONBLOCK)) {
        perror("virtio-shell: pipe");
        return 1;
    }
    signal(SIGPIPE, SIG_IGN);
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_chld;
    sa.sa_flags = SA_RESTART | SA_NOCLDSTOP;
    sigaction(SIGCHLD, &sa, NULL);
    while ((port = open(dev, O_RDWR | O_NONBLOCK | O_CLOEXEC)) < 0) {
        if (!failed++)
            fprintf(stderr, "virtio-shell: %s: %s; retrying\n", dev, strerror(errno));
        sleep_ms(1000);
    }
    fprintf(stderr, "virtio-shell: serving %s\n", dev);
    send_hello(0);

    for (;;) {
        struct pollfd p[2 + 3 * SESSIONS];
        struct session *who[2 + 3 * SESSIONS];
        int n = 0, timeout = host ? -1 : 200;
        int64_t now = now_ms();
        p[n++] = (struct pollfd){chld_pipe[0], POLLIN, 0};
        p[n++] = (struct pollfd){host ? port : -1, POLLIN, 0}; /* POLLOUT: below */
        for (int i = 0; i < SESSIONS; i++) {
            struct session *s = &sessions[i];
            if (!s->used)
                continue;
            if (s->abandoned) {
                if (s->kill_at && now >= s->kill_at) {
                    kill(-s->pid, SIGKILL);
                    s->kill_at = 0;
                }
                if (s->kill_at)
                    timeout = timeout < 0 || timeout > 500 ? 500 : timeout;
                continue;
            }
            finish(s, now);
            if (!s->used)
                continue;
            if (s->exited)
                timeout = timeout < 0 || timeout > 200 ? 200 : timeout;
            int room = s->credit > 0 && obuf.len < OUT_LIMIT;
            short in_ev = s->input.len ? POLLOUT : 0;
            if (s->out >= 0) {
                who[n] = s;
                p[n++] = (struct pollfd){s->out, (room ? POLLIN : 0) | (s->tty ? in_ev : 0), 0};
            }
            if (!s->tty && s->in >= 0) {
                if (!s->input.len && s->in_eof)
                    close_fd(&s->in);
                else {
                    who[n] = s;
                    p[n++] = (struct pollfd){s->in, in_ev, 0};
                }
            }
            if (s->err >= 0) {
                who[n] = s;
                p[n++] = (struct pollfd){s->err, room ? POLLIN : 0, 0};
            }
        }
        /* after the sessions, whose ends (finish) may have queued their status */
        if (obuf.len)
            p[1].events |= POLLOUT;
        if (poll(p, (nfds_t)n, timeout) < 0) {
            if (errno != EINTR)
                sleep_ms(100);
            continue;
        }
        if (p[0].revents)
            reap();
        for (int i = 2; i < n; i++) {
            struct session *s = who[i];
            short ev = p[i].revents;
            if (!s->used || s->abandoned || !ev)
                continue;
            if (p[i].fd == s->in && (ev & POLLOUT) && s->input.len)
                pump_in(s);
            if (p[i].fd == s->out && s->out >= 0 && (ev & (POLLIN | POLLHUP | POLLERR)) &&
                s->credit > 0)
                pump_out(s, &s->out, WEBOS_SHELL_STDOUT);
            else if (p[i].fd == s->err && s->err >= 0 && (ev & (POLLIN | POLLHUP | POLLERR)) &&
                     s->credit > 0)
                pump_out(s, &s->err, WEBOS_SHELL_STDERR);
            else if (p[i].fd == s->in && !s->tty && (ev & (POLLERR | POLLHUP)) && s->in >= 0) {
                /* the command closed its stdin */
                send_word(WEBOS_SHELL_ACK, s->id, (uint32_t)s->input.len);
                s->input.len = 0;
                close_fd(&s->in);
            }
        }
        if (!host) {
            /* a host may be back: say so (lost again at the next hang-up if not) */
            host = 1;
            send_hello(0);
            continue;
        }
        short ev = p[1].revents;
        if (ev & POLLIN) {
            unsigned char buf[65536];
            ssize_t k = read(port, buf, sizeof buf);
            if (k > 0) {
                put(&ibuf, buf, (size_t)k);
                parse(&synced);
            } else if (k == 0 || (errno != EAGAIN && errno != EINTR)) {
                ev |= POLLHUP;
            }
        }
        if ((ev & POLLOUT) && obuf.len) {
            ssize_t k = write(port, obuf.p, obuf.len);
            if (k > 0)
                take(&obuf, (size_t)k);
        }
        if (ev & (POLLHUP | POLLERR)) {
            /* no host: every session ends, and the next host starts afresh */
            if (synced)
                fprintf(stderr, "virtio-shell: the host went away\n");
            abandon_all();
            synced = 0;
            ibuf.len = obuf.len = 0;
            host = 0;
        }
    }
}
