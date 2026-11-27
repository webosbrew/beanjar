// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libluna-scale.so: sdx's fixed Luna reply and HTTP timeouts stretched for a slow guest.
 *
 * sdx asks configd, systemprofile and factorymanager for its system properties at start
 * (LunaFacade::set_systemProperties) and waits for each reply up to
 * sdx::handler::Luna_Handle::_TVSERICE_REQUEST_TIMEOUT seconds, 20 in .data, which its executable
 * exports. On a timeout it goes on without the answer, so its continent stays empty and configd's
 * first continent answer then reads as a change of country: outside FirstUse sdx resets the EULA
 * and the set reboots. configd answers during a reconfigure, which runs its selector scripts in a
 * row: 1.3 s on a KVM board, 18 to 44 s on a loaded PC whose userland runs under qemu-user
 * (docs/research/country-setting.md, "sdx's start-up timeout"). A set answers in about 1 s.
 *
 * LD_PRELOADed when the launcher passes webos.time_scale=N, N > 1, with N in LUNA_TIMEOUT_SCALE
 * (guest/initramfs/rootfs/init puts both on the Exec line of sdx's ls-hubd service files), it
 * multiplies those of the variables below that the executable exports, once, before main, and says
 * so in the kernel log; an executable with none of them is left as it is (also said). Nothing else
 * changes: the calls, their payloads and what the daemon does with an answer or a timeout are LG's.
 * The int is the executable's own (a non-PIE ELF reads it directly), so it is found with dlsym and
 * written, not interposed.
 *
 * sdx's HTTP requests to LG's servers have fixed curl timeouts too, immediates in its code: 10 s
 * in all and 5 s to connect for the token requests (CurlDataBuilder::buildCreateTokenObj and
 * buildRefreshTokenObj), CurlData's defaults of 10 and 10 for the rest, initservices among them.
 * Under qemu-user at boot a TLS handshake alone can take longer, and sdx does not send initservices
 * again until its next power or network event, as a set does after a failed request
 * (docs/research/lg-services-block-or-mock.md). The library therefore also interposes
 * curl_easy_setopt, which sdx imports from libcurl, and multiplies the timeout options
 * (CURLOPT_TIMEOUT, CURLOPT_CONNECTTIMEOUT and their _MS forms) by the same factor; every other
 * option, and every call when the factor is 1, goes to libcurl unchanged. Each new scaled value is
 * logged once.
 *
 * eula-service (33.31 on) waits for its Luna replies in LunaCommon::lsCallSync(uri, payload,
 * reply, int seconds), which its executable defines and exports: 10 s on its calls to the settings
 * service, an immediate at each of its callers. lsCallSync waits in steps of one second
 * (std::condition_variable::wait_for, inlined: pthread_cond_clockwait on CLOCK_MONOTONIC, an
 * absolute deadline one second ahead), counts the steps and gives up after `seconds` of them; on
 * resetSystemSettingDesc it then registers a fault with recovery rebootToSuspend, and on a failed
 * getSystemSettings it resets its EULA data and reboots the set. Where the executable exports that
 * symbol, the library also interposes pthread_cond_clockwait and stretches a CLOCK_MONOTONIC
 * deadline by the factor (now + (deadline - now) * N) when the call comes from the executable
 * itself: in every eula-service build the collection has (33.31.61, 33.31.68, 43.21.73, 43.22.76,
 * 02.68.80) its one call of that function is lsCallSync's wait, so each step is N seconds and the
 * call's limit N times its own. sdx's own XpaLunaHandle::lsCallSyncImpl (5 s at nine callers, 10 s
 * at getStringFromSettingCache, immediates in them) waits once on a std::condition_variable
 * wait_until with an absolute deadline; its executable's one pthread_cond_clockwait call (W22O
 * 33.31.68 and M23 33.31.61 alike) is that wait, so the same interposition stretches it when sdx
 * exports lsCallSyncImpl. A wait again on the same deadline (a wake-up before the reply)
 * keeps the stretched one. Calls from the libraries, other clocks and other executables go to libc
 * unchanged.
 */
#include "../scale-preload.h"

#include <dlfcn.h>
#include <link.h>
#include <pthread.h>
#include <stdint.h>
#include <time.h>

#define SELF "/usr/lib/libluna-scale.so"

/* The timeouts, each an int in the executable's .data: its symbol, its unit, the daemon. */
static const struct timeout {
    const char *symbol, *unit, *daemon;
} timeouts[] = {
    /* W22O 33.31.68.01 sdx: 0xd11ac, 20 (s); read by Luna_Handle::call_luna_api */
    {"_ZN3sdx7handler11Luna_Handle25_TVSERICE_REQUEST_TIMEOUTE", "s", "sdx"},
};

/* libcurl's option numbers (curl/curl.h; part of its ABI): a long each */
enum {
    CURLOPT_TIMEOUT = 13,
    CURLOPT_CONNECTTIMEOUT = 78,
    CURLOPT_TIMEOUT_MS = 155,
    CURLOPT_CONNECTTIMEOUT_MS = 156,
};
/* the option classes: below 10000 a long; 30000 to 39999 a curl_off_t (64 bits); the rest
 * (objects, functions, blobs) a pointer */
#define CURLOPTTYPE_OFF_T 30000

typedef int (*setopt_fn)(void *, int, ...);

/* The scaled values already logged: (option, value) pairs, a few distinct ones per daemon (two
 * request threads at once may log one twice). */
static struct {
    int opt;
    long from;
} logged[16];

static void log_once(int opt, long from, long to)
{
    size_t i;
    for (i = 0; i < sizeof logged / sizeof *logged; i++) {
        if (logged[i].opt == opt && logged[i].from == from)
            return;
        if (!logged[i].opt) {
            logged[i].opt = opt, logged[i].from = from;
            break;
        }
    }
    scale_log("<6>libluna-scale: curl option %d: %ld -> %ld (x%lu)\n", opt, from, to, scale);
}

__attribute__((visibility("default"))) int curl_easy_setopt(void *curl, int opt, ...)
{
    static setopt_fn next;
    va_list ap;
    if (!next)
        next = (setopt_fn)dlsym(RTLD_NEXT, "curl_easy_setopt");
    if (!next)
        return 2; /* CURLE_FAILED_INIT */
    va_start(ap, opt);
    if (opt >= CURLOPTTYPE_OFF_T && opt < CURLOPTTYPE_OFF_T + 10000) {
        int64_t v = va_arg(ap, int64_t);
        va_end(ap);
        return next(curl, opt, v);
    }
    if (opt < 10000) {
        long v = va_arg(ap, long);
        va_end(ap);
        if (scale > 1 && v > 0 && (unsigned long)v <= 0x7fffffffUL / scale &&
            (opt == CURLOPT_TIMEOUT || opt == CURLOPT_CONNECTTIMEOUT || opt == CURLOPT_TIMEOUT_MS ||
             opt == CURLOPT_CONNECTTIMEOUT_MS)) {
            long to = (long)(v * scale);
            log_once(opt, v, to);
            v = to;
        }
        return next(curl, opt, v);
    }
    void *p = va_arg(ap, void *);
    va_end(ap);
    return next(curl, opt, p);
}

/* eula-service's lsCallSync and sdx's lsCallSyncImpl, exported by their executables: where one is
 * found, the executable's CLOCK_MONOTONIC condition waits are stretched (main_map: the
 * executable's link map). */
static const struct {
    const char *symbol, *daemon;
} callsyncs[] = {
    {"_ZN10LunaCommon10lsCallSyncEPKcS1_RN7pbnjson6JValueEi", "eula-service"},
    {"_ZN3sdx8mainbody13XpaLunaHandle14lsCallSyncImplEPKcS3_RNSt7__cxx1112basic_stringIcSt11char_"
     "traitsIcESaIcEEEi",
     "sdx"},
};
static struct link_map *main_map;

typedef int (*clockwait_fn)(pthread_cond_t *, pthread_mutex_t *, clockid_t,
                            const struct timespec *);

__attribute__((visibility("default"))) int pthread_cond_clockwait(pthread_cond_t *cond,
                                                                  pthread_mutex_t *mutex,
                                                                  clockid_t clock,
                                                                  const struct timespec *abstime)
{
    static clockwait_fn next;
    /* the deadline last stretched on this thread, and what it became */
    static __thread struct timespec from, to;
    struct link_map *caller = NULL;
    Dl_info info;
    if (!next)
        next = (clockwait_fn)dlsym(RTLD_NEXT, "pthread_cond_clockwait");
    if (!next)
        return 22; /* EINVAL */
    if (main_map && clock == CLOCK_MONOTONIC &&
        dladdr1(__builtin_return_address(0), &info, (void **)&caller, RTLD_DL_LINKMAP) &&
        caller == main_map) {
        if (abstime->tv_sec != from.tv_sec || abstime->tv_nsec != from.tv_nsec) {
            struct timespec now;
            int64_t left;
            clock_gettime(CLOCK_MONOTONIC, &now);
            left = (int64_t)(abstime->tv_sec - now.tv_sec) * 1000000000 +
                   (abstime->tv_nsec - now.tv_nsec);
            from = *abstime;
            to = *abstime;
            if (left > 0 && left <= INT64_MAX / 100) {
                int64_t at = (int64_t)now.tv_nsec + left * (int64_t)scale;
                to.tv_sec = now.tv_sec + (time_t)(at / 1000000000);
                to.tv_nsec = (long)(at % 1000000000);
            }
        }
        abstime = &to;
    }
    return next(cond, mutex, clock, abstime);
}

__attribute__((constructor)) static void init(void)
{
    size_t i, found = 0;
    scale_init("LUNA_TIMEOUT_SCALE", SELF);
    if (scale <= 1)
        return;
    for (i = 0; i < sizeof callsyncs / sizeof *callsyncs && !main_map; i++) {
        struct link_map *m = NULL;
        void *self;
        if (!dlsym(RTLD_DEFAULT, callsyncs[i].symbol))
            continue;
        self = dlopen(NULL, RTLD_NOW | RTLD_NOLOAD);
        if (self && !dlinfo(self, RTLD_DI_LINKMAP, &m) && m) {
            main_map = m;
            found++;
            scale_log("<6>libluna-scale: %s: Luna call waits x%lu\n", callsyncs[i].daemon, scale);
        }
    }
    for (i = 0; i < sizeof timeouts / sizeof *timeouts; i++) {
        const struct timeout *t = &timeouts[i];
        int *v = (int *)dlsym(RTLD_DEFAULT, t->symbol);
        if (!v)
            continue;
        found++;
        if (*v <= 0 || (unsigned long)*v > 0x7fffffffUL / scale) {
            scale_log("<4>libluna-scale: %s: %s is %d, left as it is\n", t->daemon, t->symbol, *v);
            continue;
        }
        scale_log("<6>libluna-scale: %s: Luna reply timeout %d %s -> %lu %s (x%lu)\n", t->daemon,
                  *v, t->unit, *v * scale, t->unit, scale);
        *v = (int)(*v * scale);
    }
    if (!found)
        scale_log("<6>libluna-scale: no known Luna timeout in this executable: nothing scaled\n");
}
