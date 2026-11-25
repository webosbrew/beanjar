// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The histogram step's probe (scripts/regress --tv; tests/regress/lib/vthist.py judges it): the
 * firmware's own libvtcapture histogram calls (1.0.0-56 on), run as a preload of the firmware's
 * /usr/bin/vtCaptureTestSuite, the executable the library's luna role is bound to
 * (docs/research/video/capture-histogram.md: a plain binary gets "LSRegisterPubPriv FAILED").
 * Its constructor does the work and exits before the suite's own main runs:
 *
 *   HIST_PROBE=1 [HIST_WAIT_MS=N] LD_PRELOAD=.../hist-probe.so /usr/bin/vtCaptureTestSuite
 *
 * init, then for each preset 1, 2, 3, 0: set it, read until its layout has arrived (block 0's
 * sample total the preset's: the first read after a set returns the old layout), at most
 * HIST_WAIT_MS (default 2000), and print every block's R, G and B bins that are not empty; then
 * finalize. It ends on preset 0, the driver's state at boot (the preset is global to the driver).
 * The caller gate of vtCapture_initHistogram passes only with /var/luna/preferences/vt_devmode,
 * which tests/regress/tv.sh makes for the run and removes. Facts only, one "hist.*" line each.
 * The calls are looked up at run time (dlsym), not linked: the preload also reaches every process
 * on the way to the suite (busybox's or coreutils' timeout), which has no libvtcapture; there the
 * probe does nothing and the process runs on. */
#define _GNU_SOURCE /* RTLD_DEFAULT */
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static void *(*vtCapture_initHistogram)(int (*policy)(void));
static int (*vtCapture_setPresetHistogram)(void *h, int preset);
static int (*vtCapture_getHistogram)(void *h, void *buf);
static int (*vtCapture_finalizeHistogram)(void *h);

#define BLOCK 1540 /* u32 index, then R, G and B as 256 little-endian u16 counts */
static uint8_t buf[16 * BLOCK];
static const int order[4] = {1, 2, 3, 0};
static const int blocks[4] = {16, 16, 16, 14};
/* block 0's sample total per preset, as measured on a set (vthist.TOTALS) */
static const unsigned total0[4] = {7200, 5360, 5400, 5760};

static int policy(void)
{
    return 1;
}

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

static unsigned count(const uint8_t *b, int ch, int bin)
{
    const uint8_t *p = b + 4 + (ch * 256 + bin) * 2;
    return p[0] | p[1] << 8;
}

static unsigned total(const uint8_t *b)
{
    unsigned s = 0;
    for (int i = 0; i < 256; i++)
        s += count(b, 0, i);
    return s;
}

static void print_block(int preset, int i)
{
    const uint8_t *b = buf + i * BLOCK;
    unsigned index = b[0] | b[1] << 8 | b[2] << 16 | (unsigned)b[3] << 24;
    printf("hist.p%d.b%d=%u", preset, i, index);
    for (int ch = 0; ch < 3; ch++) {
        const char *sep = "";
        printf(" %c=", "RGB"[ch]);
        for (int bin = 0; bin < 256; bin++) {
            unsigned n = count(b, ch, bin);
            if (n) {
                printf("%s%dx%u", sep, bin, n);
                sep = ",";
            }
        }
    }
    printf("\n");
}

__attribute__((constructor)) static void probe(void)
{
    if (!getenv("HIST_PROBE"))
        return;
    *(void **)&vtCapture_initHistogram = dlsym(RTLD_DEFAULT, "vtCapture_initHistogram");
    *(void **)&vtCapture_setPresetHistogram = dlsym(RTLD_DEFAULT, "vtCapture_setPresetHistogram");
    *(void **)&vtCapture_getHistogram = dlsym(RTLD_DEFAULT, "vtCapture_getHistogram");
    *(void **)&vtCapture_finalizeHistogram = dlsym(RTLD_DEFAULT, "vtCapture_finalizeHistogram");
    if (!vtCapture_initHistogram || !vtCapture_setPresetHistogram || !vtCapture_getHistogram ||
        !vtCapture_finalizeHistogram)
        return; /* not the suite (timeout on the way to it): leave the process alone */
    const char *w = getenv("HIST_WAIT_MS");
    double wait = w ? atof(w) : 2000;
    setvbuf(stdout, NULL, _IOLBF, 0);
    void *h = vtCapture_initHistogram(policy);
    printf("hist.init=%s\n", h ? "ok" : "null");
    if (!h)
        _exit(1);
    for (int k = 0; k < 4; k++) {
        int p = order[k], rc;
        double t0 = now_ms();
        printf("hist.p%d.set=%d\n", p, vtCapture_setPresetHistogram(h, p));
        int arrived = 0;
        do {
            memset(buf, 0, sizeof buf);
            rc = vtCapture_getHistogram(h, buf);
            if (rc == 0 && total(buf) == total0[p]) {
                arrived = 1;
                break;
            }
            usleep(10000);
        } while (now_ms() - t0 < wait);
        printf("hist.p%d.get=%d\nhist.p%d.wait_ms=%.0f\nhist.p%d.arrived=%s\n", p, rc, p,
               now_ms() - t0, p, arrived ? "yes" : "no");
        for (int i = 0; i < blocks[p]; i++)
            print_block(p, i);
    }
    printf("hist.finalize=%d\n", vtCapture_finalizeHistogram(h));
    fflush(stdout);
    _exit(0);
}
