// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* A stream relay: each LISTEN=CONNECT pair accepts connections on LISTEN and joins each one to a
 * new connection to CONNECT, both ways, until both sides are done. mode=mitm's port bridges
 * (host/core/beanjar/netns.py): one relay on the host for the inspector and the forwarded ports
 * (tcp -> the namespace's unix sockets), one in link=lan's LAN namespace, one inside the inner
 * namespace for the other end of each (unix -> tcp).
 *
 *   beanjar-relay LISTEN=CONNECT...      each side tcp:HOST:PORT or unix:PATH
 *
 * A tcp listener binds with SO_REUSEADDR; a unix listener's path is unlinked first if it is a
 * socket. EOF on one side shuts down writing on the other (a half-close) and the pair ends when
 * both directions have; a reset or error on either side ends it.
 *
 * A listener behaves like the port of the service behind it: it is open only while CONNECT accepts
 * connections. While CONNECT refuses, the listener is closed (a tcp client gets a real "connection
 * refused", a unix client ECONNREFUSED or ENOENT), and a probe connection to CONNECT every
 * PROBE_MS opens it again once CONNECT answers; no probe runs while it is open. A connect that
 * fails on a live connection (the service went away) closes the listener too and resets the
 * accepted connection (SO_LINGER 0). The probe connects and closes at once, so the service sees one
 * empty connection each time the listener opens. Once every listener is bound it prints "ready" on
 * stdout (and nothing more there); a listener that cannot bind ends it with status 1 and the reason
 * on stderr, before "ready". SIGTERM or SIGINT closes the listeners and every connection, removes
 * its unix sockets and ends it with status 0. Single-threaded, epoll; it replaced a Python asyncio
 * relay of about 10 MB PSS per process. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <time.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define BUF        (64 * 1024)
#define MAX_LISTEN 64
#define PER_PAIR   4    /* listening sockets one LISTEN can bind (a name with several addresses) */
#define PROBE_MS   250  /* between probes of a CONNECT that refuses */
#define PROBE_MAX  2000 /* a probe connect waiting longer than this is dropped and retried */

enum { LISTENER, SIDE, SIGNALS, PROBE };

struct addr {
    int unix_;
    char path[sizeof(((struct sockaddr_un *)0)->sun_path) + 64]; /* too long: refused at use */
    char host[256], port[8];
    char text[512];
};

struct pair {
    int kind; /* PROBE: what the epoll set reports for the probe socket */
    const struct addr *listen, *connect;
    int up;        /* its listening sockets are open */
    int nfds;      /* listeners[] slots in use, from index * PER_PAIR */
    int probe;     /* the probe's socket, or -1 */
    long probe_at; /* when the probe started, or the next one is due (ms) */
    char made[sizeof(((struct addr *)0)->path)]; /* the unix socket this pair made, if it did */
    dev_t dev;
    ino_t ino;
};

struct listener {
    int kind; /* LISTENER */
    int fd;
    struct pair *pair;
};

struct conn;
struct side {
    int kind;      /* SIDE */
    int fd, index; /* 0: accepted, 1: ours to CONNECT */
    int watched;   /* in the epoll set */
    struct conn *c;
};

/* dir[i]: bytes read from side i, still to be written to side 1 - i */
struct dir {
    char *buf;
    size_t len, off;
    int eof, shut;
};

struct conn {
    struct side s[2];
    struct dir dir[2];
    int connecting, dead;
    struct pair *pair;
    const struct addr *target;
    struct conn *next, *prev;
};

static int ep;
static struct conn *conns,
    *graveyard; /* graveyard: ended in this batch of events, freed after it */
static struct listener listeners[MAX_LISTEN * PER_PAIR];
static struct pair pairs[MAX_LISTEN];
static int npairs;

static void say(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fputs("relay: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

static int parse_addr(const char *s, struct addr *a)
{
    memset(a, 0, sizeof *a);
    snprintf(a->text, sizeof a->text, "%s", s);
    if (!strncmp(s, "unix:", 5) && s[5]) {
        a->unix_ = 1;
        if (strlen(s + 5) >= sizeof a->path)
            return -1;
        strcpy(a->path, s + 5);
        return 0;
    }
    if (strncmp(s, "tcp:", 4))
        return -1;
    const char *rest = s + 4, *colon = strrchr(rest, ':');
    if (!colon || colon == rest || !colon[1] ||
        strspn(colon + 1, "0123456789") != strlen(colon + 1) || strlen(colon + 1) >= sizeof a->port)
        return -1;
    size_t n = (size_t)(colon - rest);
    if (n >= 2 && rest[0] == '[' && rest[n - 1] == ']') {
        rest++;
        n -= 2;
    } /* [::1] */
    if (!n || n >= sizeof a->host)
        return -1;
    memcpy(a->host, rest, n);
    strcpy(a->port, colon + 1);
    snprintf(a->text, sizeof a->text, "tcp:%s:%s", a->host, a->port);
    return 0;
}

static long now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000L + t.tv_nsec / 1000000L;
}

static void unlink_made(struct pair *p)
{
    struct stat st;
    if (p->made[0] && !lstat(p->made, &st) && st.st_dev == p->dev && st.st_ino == p->ino)
        unlink(p->made);
    p->made[0] = 0;
}

static int unix_sockaddr(const struct addr *a, struct sockaddr_un *sa)
{
    memset(sa, 0, sizeof *sa);
    sa->sun_family = AF_UNIX;
    if (strlen(a->path) >= sizeof sa->sun_path) {
        errno = ENAMETOOLONG;
        return -1;
    }
    strcpy(sa->sun_path, a->path);
    return 0;
}

static void watch(int fd, void *ptr, uint32_t events, int op)
{
    struct epoll_event e = {.events = events, .data.ptr = ptr};
    epoll_ctl(ep, op, fd, &e);
}

static int add_listener(int fd, struct pair *p)
{
    if (p->nfds == PER_PAIR) {
        close(fd);
        errno = EMFILE;
        return -1;
    }
    struct listener *x = &listeners[(p - pairs) * PER_PAIR + p->nfds++];
    *x = (struct listener){.kind = LISTENER, .fd = fd, .pair = p};
    watch(fd, x, EPOLLIN, EPOLL_CTL_ADD);
    return 0;
}

/* Close the pair's listening sockets (and remove the unix socket it made). */
static void close_pair(struct pair *p)
{
    for (int i = 0; i < p->nfds; i++) {
        close(listeners[(p - pairs) * PER_PAIR + i].fd);
        listeners[(p - pairs) * PER_PAIR + i].fd = -1;
    }
    p->nfds = 0;
    p->up = 0;
    unlink_made(p);
}

static void probe_cancel(struct pair *p)
{
    if (p->probe >= 0)
        close(p->probe);
    p->probe = -1;
}

/* Bind LISTEN; 0, or -1 with the reason in *why. */
static int listen_on(struct pair *p, const char **why)
{
    const struct addr *l = p->listen;
    if (l->unix_) {
        struct sockaddr_un sa;
        struct stat st;
        if (unix_sockaddr(l, &sa) < 0) {
            *why = strerror(errno);
            return -1;
        }
        if (!lstat(l->path, &st) && S_ISSOCK(st.st_mode))
            unlink(l->path);
        int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (fd < 0 || bind(fd, (struct sockaddr *)&sa, sizeof sa) < 0) {
            *why = strerror(errno);
            if (fd >= 0)
                close(fd);
            return -1;
        }
        if (!stat(l->path, &st)) {
            snprintf(p->made, sizeof p->made, "%s", l->path);
            p->dev = st.st_dev;
            p->ino = st.st_ino;
        }
        if (listen(fd, SOMAXCONN) < 0 || add_listener(fd, p) < 0) {
            *why = strerror(errno);
            return -1;
        }
        p->up = 1;
        return 0;
    }
    struct addrinfo hints = {.ai_family = AF_UNSPEC,
                             .ai_socktype = SOCK_STREAM,
                             .ai_flags = AI_PASSIVE},
                    *res;
    int r = getaddrinfo(l->host, l->port, &hints, &res);
    if (r) {
        *why = gai_strerror(r);
        return -1;
    }
    int n = 0;
    for (struct addrinfo *ai = res; ai; ai = ai->ai_next) {
        int fd = socket(ai->ai_family, ai->ai_socktype | SOCK_NONBLOCK | SOCK_CLOEXEC,
                        ai->ai_protocol),
            one = 1;
        if (fd < 0) {
            *why = strerror(errno);
            n = -1;
            break;
        }
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
        if (ai->ai_family == AF_INET6)
            setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &one, sizeof one);
        if (bind(fd, ai->ai_addr, ai->ai_addrlen) < 0 || listen(fd, SOMAXCONN) < 0) {
            *why = strerror(errno);
            close(fd);
            n = -1;
            break;
        }
        if (add_listener(fd, p) < 0) {
            *why = strerror(errno);
            n = -1;
            break;
        }
        n++;
    }
    freeaddrinfo(res);
    if (n <= 0 && p->nfds)
        close_pair(p);
    if (n > 0)
        p->up = 1;
    return n > 0 ? 0 : -1;
}

static void probe_event(struct pair *p);

/* The pair's CONNECT refuses: close its listeners and probe it until it answers. */
static void pair_down(struct pair *p)
{
    if (!p->up)
        return;
    close_pair(p);
    p->probe_at = now_ms() + PROBE_MS;
}

/* Start a probe connection to the pair's CONNECT; it ends in probe_event(). */
static void probe_start(struct pair *p)
{
    const struct addr *t = p->connect;
    int fd = -1, r = -1;
    if (t->unix_) {
        struct sockaddr_un sa;
        if (unix_sockaddr(t, &sa) == 0 &&
            (fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0)) >= 0)
            r = connect(fd, (struct sockaddr *)&sa, sizeof sa);
    } else {
        struct addrinfo hints = {.ai_family = AF_UNSPEC, .ai_socktype = SOCK_STREAM}, *res;
        if (!getaddrinfo(t->host, t->port, &hints, &res)) {
            if ((fd = socket(res->ai_family, res->ai_socktype | SOCK_NONBLOCK | SOCK_CLOEXEC,
                             res->ai_protocol)) >= 0)
                r = connect(fd, res->ai_addr, res->ai_addrlen);
            freeaddrinfo(res);
        }
    }
    p->probe_at = now_ms();
    if (fd < 0 || (r < 0 && errno != EINPROGRESS)) {
        if (fd >= 0)
            close(fd);
        p->probe_at += PROBE_MS;
        return;
    }
    p->probe = fd;
    if (r == 0) {
        probe_event(p);
        return;
    }
    watch(fd, p, EPOLLOUT, EPOLL_CTL_ADD);
}

/* The probe's connect finished: CONNECT answers, so open the listeners; else try again later. */
static void probe_event(struct pair *p)
{
    int err = 0;
    socklen_t len = sizeof err;
    if (getsockopt(p->probe, SOL_SOCKET, SO_ERROR, &err, &len) < 0)
        err = errno;
    probe_cancel(p);
    const char *why = "";
    if (!err && listen_on(p, &why) < 0) {
        say("cannot listen on %s: %s", p->listen->text, why);
        err = 1;
    }
    if (err)
        p->probe_at = now_ms() + PROBE_MS;
}

/* A reset instead of a close for a connection that never got through: the client sees a refusal
 * as far as TCP allows after the handshake. */
static void reset(int fd)
{
    struct linger lg = {.l_onoff = 1, .l_linger = 0};
    setsockopt(fd, SOL_SOCKET, SO_LINGER, &lg, sizeof lg);
}

static void conn_close(struct conn *c)
{
    for (int i = 0; i < 2; i++) {
        if (c->s[i].fd >= 0)
            close(c->s[i].fd); /* also leaves the epoll set */
        c->s[i].fd = -1;
        free(c->dir[i].buf);
        c->dir[i].buf = NULL;
    }
    if (c->prev)
        c->prev->next = c->next;
    else
        conns = c->next;
    if (c->next)
        c->next->prev = c->prev;
    c->dead = 1;
    c->next = graveyard;
    graveyard = c;
}

static void bury(void)
{
    while (graveyard) {
        struct conn *c = graveyard;
        graveyard = c->next;
        free(c);
    }
}

/* Interest of both sides from the buffers' state. A side with nothing to wait for leaves the epoll
 * set: epoll reports a hang-up whatever the interest, and a side that hung up while what it sent
 * still waits in its buffer (a client that sent, half-closed and was answered) would otherwise
 * report it again and again until the other side has taken that data. */
static void rearm(struct conn *c)
{
    for (int i = 0; i < 2; i++) {
        struct side *s = &c->s[i];
        uint32_t ev = 0;
        if (s->fd < 0)
            continue;
        if (c->connecting)
            ev = i == 1 ? EPOLLOUT : 0; /* nothing is read before it is connected */
        else {
            if (!c->dir[i].eof && !c->dir[i].len)
                ev |= EPOLLIN;
            if (c->dir[1 - i].len)
                ev |= EPOLLOUT;
        }
        if (ev) {
            watch(s->fd, s, ev, s->watched ? EPOLL_CTL_MOD : EPOLL_CTL_ADD);
            s->watched = 1;
        } else if (s->watched) {
            epoll_ctl(ep, EPOLL_CTL_DEL, s->fd, NULL);
            s->watched = 0;
        }
    }
}

/* Write what dir[i] holds to side 1 - i, then pass its EOF on; -1 if the pair must end. */
static int flush(struct conn *c, int i)
{
    struct dir *d = &c->dir[i];
    int to = c->s[1 - i].fd;
    while (d->len) {
        ssize_t n = send(to, d->buf + d->off, d->len - d->off, MSG_NOSIGNAL);
        if (n < 0)
            return errno == EAGAIN || errno == EINTR ? 0 : -1;
        d->off += (size_t)n;
        if (d->off == d->len)
            d->len = d->off = 0;
    }
    if (d->eof && !d->shut) {
        shutdown(to, SHUT_WR);
        d->shut = 1;
    }
    return 0;
}

static int pull(struct conn *c, int i)
{
    struct dir *d = &c->dir[i];
    if (d->eof || d->len)
        return 0;
    ssize_t n = recv(c->s[i].fd, d->buf, BUF, 0);
    if (n < 0)
        return errno == EAGAIN || errno == EINTR ? 0 : -1;
    if (n == 0)
        d->eof = 1;
    else {
        d->len = (size_t)n;
        d->off = 0;
    }
    return flush(c, i);
}

static void nodelay(int fd)
{
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one); /* fails harmlessly on unix */
}

/* Start the connection to the pair's CONNECT for an accepted fd. */
static void start(int fd, struct pair *pair)
{
    const struct addr *target = pair->connect;
    struct conn *c = calloc(1, sizeof *c);
    if (c) {
        c->dir[0].buf = malloc(BUF);
        c->dir[1].buf = malloc(BUF);
    }
    if (!c || !c->dir[0].buf || !c->dir[1].buf) {
        say("out of memory");
        if (c) {
            free(c->dir[0].buf);
            free(c->dir[1].buf);
            free(c);
        }
        close(fd);
        return;
    }
    c->target = target;
    c->pair = pair;
    c->s[0] = (struct side){.kind = SIDE, .fd = fd, .index = 0, .c = c};
    c->s[1] = (struct side){.kind = SIDE, .fd = -1, .index = 1, .c = c};
    c->next = conns;
    if (conns)
        conns->prev = c;
    conns = c;
    nodelay(fd);
    int out = -1, r = -1;
    if (target->unix_) {
        struct sockaddr_un sa;
        if (unix_sockaddr(target, &sa) == 0 &&
            (out = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0)) >= 0)
            r = connect(out, (struct sockaddr *)&sa, sizeof sa);
    } else {
        struct addrinfo hints = {.ai_family = AF_UNSPEC, .ai_socktype = SOCK_STREAM}, *res;
        int g = getaddrinfo(target->host, target->port, &hints, &res);
        if (g) {
            say("connect %s: %s", target->text, gai_strerror(g));
            reset(fd);
            pair_down(pair);
            conn_close(c);
            return;
        }
        if ((out = socket(res->ai_family, res->ai_socktype | SOCK_NONBLOCK | SOCK_CLOEXEC,
                          res->ai_protocol)) >= 0) {
            nodelay(out);
            r = connect(out, res->ai_addr, res->ai_addrlen);
        }
        freeaddrinfo(res);
    }
    if (out < 0 || (r < 0 && errno != EINPROGRESS)) {
        say("connect %s: %s", target->text, strerror(errno));
        if (out >= 0)
            close(out);
        reset(fd);
        pair_down(pair);
        conn_close(c);
        return;
    }
    c->s[1].fd = out;
    c->connecting = r < 0;
    rearm(c);
}

static void side_event(struct side *s, uint32_t ev)
{
    struct conn *c = s->c;
    int i = s->index;
    if (i == 1 && c->connecting) {
        int err = 0;
        socklen_t len = sizeof err;
        if (getsockopt(s->fd, SOL_SOCKET, SO_ERROR, &err, &len) < 0)
            err = errno;
        if (err) {
            say("connect %s: %s", c->target->text, strerror(err));
            reset(c->s[0].fd);
            pair_down(c->pair);
            conn_close(c);
            return;
        }
        if (!(ev & (EPOLLOUT | EPOLLERR | EPOLLHUP)))
            return;
        c->connecting = 0;
        rearm(c);
        return;
    }
    if (ev & EPOLLERR) {
        conn_close(c);
        return;
    }
    /* writable (or hung up, which the write then reports): what the other side sent this one */
    if ((ev & (EPOLLOUT | EPOLLHUP)) && c->dir[1 - i].len && flush(c, 1 - i) < 0) {
        conn_close(c);
        return;
    }
    /* readable, or hung up: the rest of what it sent, then its EOF */
    if ((ev & (EPOLLIN | EPOLLHUP)) && pull(c, i) < 0) {
        conn_close(c);
        return;
    }
    if (c->dir[0].shut && c->dir[1].shut) {
        conn_close(c);
        return;
    }
    rearm(c);
}

/* One wait of at most timeout_ms and what it reports, then the probes that are due; 1 if a signal
 * ended it. */
static int turn(int timeout_ms)
{
    struct epoll_event evs[64];
    int n = epoll_wait(ep, evs, 64, timeout_ms);
    if (n < 0) {
        if (errno != EINTR)
            say("epoll: %s", strerror(errno));
        return errno != EINTR;
    }
    for (int k = 0; k < n; k++) {
        int kind = *(int *)evs[k].data.ptr;
        if (kind == SIGNALS)
            return 1;
        if (kind == PROBE) {
            probe_event(evs[k].data.ptr);
            continue;
        }
        if (kind == LISTENER) {
            struct listener *l = evs[k].data.ptr;
            for (;;) {
                if (l->fd < 0) /* its pair went down in this batch */
                    break;
                int fd = accept4(l->fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
                if (fd < 0) {
                    if (errno != EAGAIN && errno != EINTR && errno != ECONNABORTED)
                        say("accept %s: %s", l->pair->listen->text, strerror(errno));
                    break;
                }
                start(fd, l->pair);
            }
            continue;
        }
        /* a side: its connection may have ended earlier in this batch */
        struct side *s = evs[k].data.ptr;
        if (!s->c->dead)
            side_event(s, evs[k].events);
    }
    bury();
    long t = now_ms();
    for (int i = 0; i < npairs; i++) {
        struct pair *p = &pairs[i];
        if (p->up)
            continue;
        if (p->probe >= 0) {
            if (t - p->probe_at > PROBE_MAX) {
                probe_cancel(p);
                p->probe_at = t;
            }
        } else if (t >= p->probe_at) {
            probe_start(p);
        }
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2 || !strcmp(argv[1], "-h") || !strcmp(argv[1], "--help")) {
        fprintf(stderr, "usage: beanjar-relay LISTEN=CONNECT...  (tcp:HOST:PORT or unix:PATH)\n");
        return 2;
    }
    if (argc - 1 > MAX_LISTEN) {
        say("too many pairs");
        return 1;
    }
    npairs = argc - 1;
    struct addr *addrs = calloc((size_t)npairs * 2, sizeof *addrs);
    if (!addrs)
        return 1;
    for (int i = 0; i < npairs; i++) {
        char *arg = strdup(argv[i + 1]), *eq = arg ? strchr(arg, '=') : NULL;
        if (!eq) {
            say("bad pair '%s' (LISTEN=CONNECT)", argv[i + 1]);
            return 1;
        }
        *eq = 0;
        const char *bad = parse_addr(arg, &addrs[2 * i])          ? arg
                          : parse_addr(eq + 1, &addrs[2 * i + 1]) ? eq + 1
                                                                  : NULL;
        if (bad) {
            say("bad address '%s' (tcp:HOST:PORT or unix:PATH)", bad);
            return 1;
        }
        free(arg);
        pairs[i] = (struct pair){
            .kind = PROBE, .listen = &addrs[2 * i], .connect = &addrs[2 * i + 1], .probe = -1};
    }
    signal(SIGPIPE, SIG_IGN);
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGINT);
    sigprocmask(SIG_BLOCK, &mask, NULL);
    int sfd = signalfd(-1, &mask, SFD_NONBLOCK | SFD_CLOEXEC);
    ep = epoll_create1(EPOLL_CLOEXEC);
    if (sfd < 0 || ep < 0) {
        say("%s", strerror(errno));
        return 1;
    }
    static int signals_kind = SIGNALS;
    watch(sfd, &signals_kind, EPOLLIN, EPOLL_CTL_ADD);
    for (int i = 0; i < npairs; i++) {
        const char *why = "";
        if (listen_on(&pairs[i], &why) < 0) {
            say("cannot listen on %s: %s", addrs[2 * i].text, why);
            for (int j = 0; j <= i; j++)
                unlink_made(&pairs[j]);
            return 1;
        }
        say("%s -> %s", addrs[2 * i].text, addrs[2 * i + 1].text);
    }
    /* Binding worked, so any later failure is the service's. Open only the listeners whose CONNECT
     * answers: probe them all at once and give the answers a moment before "ready". */
    for (int i = 0; i < npairs; i++) {
        close_pair(&pairs[i]);
        probe_start(&pairs[i]);
    }
    for (long t0 = now_ms(); now_ms() - t0 < 200;) {
        int pending = 0;
        for (int i = 0; i < npairs; i++)
            pending += pairs[i].probe >= 0;
        if (!pending)
            break;
        if (turn(10))
            goto done;
    }
    fputs("ready\n", stdout);
    fflush(stdout);
    int null = open("/dev/null", O_WRONLY | O_CLOEXEC);
    if (null >= 0) {
        dup2(null, 1);
        close(null);
    }
    for (;;) {
        int down = 0;
        for (int i = 0; i < npairs; i++)
            down += !pairs[i].up;
        if (turn(down ? PROBE_MS / 5 : -1))
            break;
    }
done:
    for (int i = 0; i < npairs; i++) {
        probe_cancel(&pairs[i]);
        close_pair(&pairs[i]);
    }
    while (conns)
        conn_close(conns);
    bury();
    return 0;
}
