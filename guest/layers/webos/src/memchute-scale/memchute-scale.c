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
#include "../scale-preload.h"

#include <dlfcn.h>

#define SELF        "/usr/lib/libmemchute-scale.so"
#define MAX_SECONDS 359 /* libfm's startHeartBeat: timeout_sec - 11 > 348 unsigned is refused */

__attribute__((constructor)) static void init(void)
{
    scale_init("MEMCHUTE_HEARTBEAT_SCALE", SELF);
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
        scale_log("<6>libmemchute-scale: startHeartBeat timeout %d s -> %d s (x%lu)\n", timeout_sec,
                  t, scale);
    }
    return real(context, t, action, flag);
}
