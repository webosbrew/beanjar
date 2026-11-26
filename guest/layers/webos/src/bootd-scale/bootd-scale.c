// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libbootd-scale.so: bootd's fallback timers stretched for a slow (TCG) guest.
 *
 * bootd waits for each boot signal (tv-ready, banner-ready, powerd-ready, ...) and file
 * (/tmp/initdone, ...) with a fixed timeout, and on expiry sends the next signal anyway: the boot
 * goes on degraded. Every one of those waits is armed with g_timeout_add_seconds (webOS 3 and 4:
 * DynamicEventDB's waitEvent/waitTimeout/waitFile) or g_timeout_add (webOS 1 and 2: the CPU
 * monitor's caps), with values hard-coded in the binary (scratch/timed-gates-study.md, section 2).
 * Under TCG the producers come late because the guest runs slowly, not because anything hangs, and
 * the fallbacks fire. On webOS 3 and 4 its synchronous Luna calls (the first app's launch, among
 * others) wait for the reply with a timeout too, LS::Call::get(ms) of libluna-service2++ (3.17.0 on
 * W16R/W17H, 3.21.2 on W18R/W19P), and on an empty reply bootd logs "No reply in N ms" and retries
 * or gives up (W19P under TCG: the first app's launch, 5000 ms, three times).
 *
 * This library, LD_PRELOADed into bootd by the override guest/initramfs/rootfs/init writes when the
 * launcher passes webos.time_scale=N (N > 1: TCG; under KVM it is not loaded), multiplies every
 * interval of 5 s or more by BOOTD_TIMEOUT_SCALE, and so LS::Call::get's timeout; shorter ones (the
 * 1 s CPU samples and pauses) stay. The constructor takes the library and the variable out of the
 * environment again, so what bootd starts (resourcemon, initctl, luna-send) runs with LG's timing
 * and the stock environment.
 */
#include "../scale-preload.h"

#include <dlfcn.h>

#define SELF        "/usr/lib/libbootd-scale.so"
#define MIN_SECONDS 5

typedef unsigned int guint;
typedef int gboolean;
typedef void *gpointer;
typedef gboolean (*GSourceFunc)(gpointer);
typedef guint add_fn(guint, GSourceFunc, gpointer);

__attribute__((constructor)) static void init(void)
{
    scale_init("BOOTD_TIMEOUT_SCALE", SELF);
}

static guint scaled(guint value, guint unit)
{
    if (value / unit < MIN_SECONDS || value > (guint)-1 / scale)
        return value;
    return value * scale;
}

__attribute__((visibility("default"))) guint g_timeout_add_seconds(guint seconds, GSourceFunc fn,
                                                                   gpointer data)
{
    static add_fn *real;
    if (!real && !(real = (add_fn *)dlsym(RTLD_NEXT, "g_timeout_add_seconds")))
        abort();
    return real(scaled(seconds, 1), fn, data);
}

__attribute__((visibility("default"))) guint g_timeout_add(guint ms, GSourceFunc fn, gpointer data)
{
    static add_fn *real;
    if (!real && !(real = (add_fn *)dlsym(RTLD_NEXT, "g_timeout_add")))
        abort();
    return real(scaled(ms, 1000), fn, data);
}

/* LS::Message LS::Call::get(unsigned long ms), exported by libluna-service2++ 3.17.0 and 3.21.2 and
 * imported by the bootd of W16R, W17H, W18R and W19P (webOS 1 and 2's bootd does not use it). Under
 * the ARM EABI the LS::Message it returns by value (a class with a destructor) comes back through a
 * hidden pointer, so the call is (result, this, ms): passed through as they are, the timeout
 * scaled. Inside, the library waits with g_timeout_source_new on the main loop or
 * pthread_cond_timedwait elsewhere; this covers both. */
typedef void *call_get_fn(void *, void *, unsigned long);

__attribute__((visibility("default"))) void *_ZN2LS4Call3getEm(void *result, void *self,
                                                               unsigned long ms)
{
    static call_get_fn *real;
    if (!real && !(real = (call_get_fn *)dlsym(RTLD_NEXT, "_ZN2LS4Call3getEm")))
        abort();
    real(result, self, ms / 1000 < MIN_SECONDS || ms > (unsigned long)-1 / scale ? ms : ms * scale);
    return result;
}
