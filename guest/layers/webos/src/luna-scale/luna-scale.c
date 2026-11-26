// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libluna-scale.so: a daemon's fixed Luna reply timeouts stretched for a slow guest.
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
 * eula-service's Luna timeouts (10 s on its calls to the settings service, 33.31 on) are not here:
 * they are immediates in the instructions (LunaCommon::lsCallSync(..., int seconds), called
 * directly inside the executable), with no variable or imported function to scale.
 */
#include "../scale-preload.h"

#include <dlfcn.h>

#define SELF "/usr/lib/libluna-scale.so"

/* The timeouts, each an int in the executable's .data: its symbol, its unit, the daemon. */
static const struct timeout {
    const char *symbol, *unit, *daemon;
} timeouts[] = {
    /* W22O 33.31.68.01 sdx: 0xd11ac, 20 (s); read by Luna_Handle::call_luna_api */
    {"_ZN3sdx7handler11Luna_Handle25_TVSERICE_REQUEST_TIMEOUTE", "s", "sdx"},
};

__attribute__((constructor)) static void init(void)
{
    size_t i, found = 0;
    scale_init("LUNA_TIMEOUT_SCALE", SELF);
    if (scale <= 1)
        return;
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
