// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* RELEASE's partition verification for NetCast 4, inactive: the one place the emulator changes a
 * value inside one of LG's programs (docs/dev/architecture/netcast4.md, "Secure boot").
 *
 * MAIN_Root always starts the init task tVerifyInit (DDM_VerifyTask), which checks the signatures
 * of base, patch, opensrc, lglib, emanual, estreamer and extra with sb_verify_application,
 * RELEASE's own code: it needs the bootloader's sbkey= (the address of a key blob the set's secure
 * boot decrypts, per set) and fails without it, and after a second failure the task shuts the set
 * down (API_DDM_POWER_Shutdown_ByVerifyFail, micom 25 00 43, "VERIFY FAIL"). No stock state leaves
 * the task inactive; its only other exit is the exported gFullVerify: nonzero, the task marks the
 * verification ended (gVerifyEnd) and returns. Nothing in RELEASE writes it.
 *
 * So RELEASE's gFullVerify is set to 1, and nothing else. Not in a constructor: RELEASE's start-up
 * zeroes its bss after the preloaded libraries' constructors have run (a value set there read 0
 * about 300 ms later), but at its first GAL_GX_Init (halgal.c's gal_gx_process_init), in its first
 * init task (tGFXInit), before MAIN_Root creates its last, tVerifyInit. Any other process: nothing.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))

EXPORT void gal_gx_process_init(void)
{
    char exe[64];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n < 8)
        return;
    exe[n] = 0;
    if (strcmp(exe + n - 8, "/RELEASE"))
        return;
    uint32_t *full = dlsym(RTLD_DEFAULT, "gFullVerify");
    if (full)
        *full = 1;
}
