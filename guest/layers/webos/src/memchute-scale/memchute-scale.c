// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libmemchute-scale.so: memchute's main-loop heartbeat stretched for a slow (TCG) guest.
 *
 * memchute registers libfm's bus-lock watchdog right after making its main loop,
 * startHeartBeat(context, 40, 4, 1): if the loop does not run libfm's 1 s source for 40 s,
 * faultmanager kills memchute with SIGABRT and systemd restarts it. Its startMemNotifier handler
 * runs `systemctl start swap-on.service` synchronously on that loop, and under TCG that chain takes
 * 30 to more than 40 s of a saturated first boot (docs/research/memchute-heartbeat-abort.md). The
 * call is the same in every firmware that has it, one site each: 40 s in kisscurl from W21A on,
 * mullet and number1; 150 s in ombre, ponytail and queue (scaled, those reach libfm's limit). The
 * memchute of jhericurl, N21D's kisscurl and the Upstart releases imports no startHeartBeat.
 *
 * This library, LD_PRELOADed into memchute by the drop-in guest/initramfs/rootfs/init writes when
 * the launcher passes webos.time_scale=N (N > 1: TCG; under KVM it is not loaded), multiplies the
 * timeout by MEMCHUTE_HEARTBEAT_SCALE, at most to libfm's own limit (startHeartBeat refuses
 * anything outside 11 to 359 s), and says so in the kernel log (the firmware's journald keeps no
 * files, so memchute's stderr is lost; dmesg has the initramfs's own line beside it). The
 * constructor takes the library and the variable out of the environment again, so what memchute
 * starts (systemctl, the swap scripts) runs with the stock environment.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define SELF        "/usr/lib/libmemchute-scale.so"
#define MAX_SECONDS 359 /* libfm's startHeartBeat: timeout_sec - 11 > 348 unsigned is refused */

static unsigned long scale = 1;

/* Out of LD_PRELOAD, in place (as guest/layers/webos/src/bootd-scale's library does). */
static void leave_preload(void)
{
    char *v = getenv("LD_PRELOAD"), *p;
    size_t n = strlen(SELF);
    if (!v)
        return;
    for (p = v; (p = strstr(p, SELF)); p++) {
        if ((p == v || p[-1] == ' ' || p[-1] == ':') && (!p[n] || p[n] == ' ' || p[n] == ':')) {
            char *from = p + n;
            if (p > v && !*from)
                p--;
            else if (*from)
                from++;
            memmove(p, from, strlen(from) + 1);
            break;
        }
    }
    if (!v[strspn(v, " :")])
        unsetenv("LD_PRELOAD");
}

__attribute__((constructor)) static void init(void)
{
    const char *v = getenv("MEMCHUTE_HEARTBEAT_SCALE");
    char *end;
    unsigned long n = v ? strtoul(v, &end, 10) : 0;
    if (v && *v && !*end && n > 1 && n <= 100)
        scale = n;
    unsetenv("MEMCHUTE_HEARTBEAT_SCALE");
    leave_preload();
}

/* int startHeartBeat(GMainContext *context, int timeout_sec, int action, bool flag), the C API
 * libfm.so.1 exports and memchute imports (W22O 13.30.56: memchute 0x40bd6, (context, 40, 4, 1);
 * action 4 is libfm's "respawn"). The timeout counts the FM-watchdog thread's sleep(1) ticks. */
typedef int start_fn(void *, int, int, int);

__attribute__((visibility("default"))) int startHeartBeat(void *context, int timeout_sec,
                                                          int action, int flag)
{
    static start_fn *real;
    int t = timeout_sec;
    if (!real && !(real = (start_fn *)dlsym(RTLD_NEXT, "startHeartBeat")))
        abort();
    if (scale > 1 && t > 0 && t < MAX_SECONDS) {
        t = (unsigned long)t * scale > MAX_SECONDS ? MAX_SECONDS : t * (int)scale;
        /* One write(): /dev/kmsg takes a record per write, and dprintf fails on it (EINVAL, from
         * the seek glibc does when it attaches a stream to the descriptor). */
        char line[96];
        int n = snprintf(line, sizeof line,
                         "<6>libmemchute-scale: startHeartBeat timeout %d s -> %d s (x%lu)\n",
                         timeout_sec, t, scale);
        int fd = open("/dev/kmsg", O_WRONLY | O_CLOEXEC);
        if (fd >= 0) {
            if (n > 0 && (size_t)n < sizeof line)
                (void)!write(fd, line, (size_t)n);
            close(fd);
        }
    }
    return real(context, t, action, flag);
}
