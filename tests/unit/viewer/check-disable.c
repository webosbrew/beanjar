// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* QEMU's display Disable as the core takes it (host/viewer/core/disable.h), driven as core.c does:
 * the D-Bus calls arm and cancel, the 4 ms poll applies. A stray Disable followed by a scanout
 * within the wait leaves the picture and held input alone (no black frame); a Disable alone blanks
 * the picture and releases input once, at the first poll after the wait and not before. */
#include "../../../host/viewer/core/disable.h"
#include <stdio.h>

static int failures;
static void check(int ok, const char *what, long a, long b)
{
    if (!ok) {
        printf("FAIL %s: %ld, %ld\n", what, a, b);
        failures++;
    }
}

/* the core's picture state: valid as core.c keeps it, the times it went black, the releases */
struct core {
    struct disable d;
    int valid;
    long black_at, releases, black_polls;
};
enum { DISABLE, SCANOUT, UPDATE };
static void call(struct core *c, int what, int64_t now)
{
    if (what == DISABLE)
        disable_arm(&c->d, now);
    else if (what == SCANOUT)
        disable_cancel(&c->d);
    else if (what == UPDATE)
        c->valid = 1; /* capture_fenced */
}
static void poll_step(struct core *c, int64_t now)
{
    if (disable_due(&c->d, now)) {
        c->valid = 0;
        c->releases++;
        if (!c->black_at)
            c->black_at = (long)now;
    }
    c->black_polls += !c->valid; /* a picture composed now would be black */
}
/* calls[i] at times[i] (us after start), polled every 4 ms from start for total us */
static struct core run(const int *calls, const long *times, int n, long total)
{
    struct core c = {.valid = 1};
    const int64_t start = 1000000;
    int i = 0;
    for (long t = 0; t <= total; t++) {
        while (i < n && times[i] == t)
            call(&c, calls[i++], start + t);
        if (t % 4000 == 0)
            poll_step(&c, start + t);
    }
    if (c.black_at)
        c.black_at -= start;
    return c;
}

int main(void)
{
    /* the stray one (docs/research/pi-black-flashes.md): Disable, then ScanoutDMABUF 0.6 ms and
     * UpdateDMABUF 2.4 ms later, the Disable just before a poll and the scanout after it */
    {
        int calls[] = {DISABLE, SCANOUT, UPDATE};
        long times[] = {3900, 4500, 6300};
        struct core c = run(calls, times, 3, 200000);
        check(c.black_polls == 0, "stray Disable: no black picture", c.black_polls, 0);
        check(c.releases == 0, "stray Disable: held input kept", c.releases, 0);
        check(c.valid == 1, "stray Disable: picture kept", c.valid, 1);
    }
    /* a scanout late but within the wait still cancels it */
    {
        int calls[] = {DISABLE, SCANOUT, UPDATE};
        long times[] = {1000, 1000 + DISABLE_WAIT_US - 1, 1000 + DISABLE_WAIT_US + 500};
        struct core c = run(calls, times, 3, 200000);
        check(c.black_polls == 0, "late scanout within the wait: no black", c.black_polls, 0);
        check(c.releases == 0, "late scanout within the wait: no release", c.releases, 0);
    }
    /* a real Disable: black at the first poll at or after the wait, never before; released once */
    {
        int calls[] = {DISABLE};
        long times[] = {1000};
        struct core c = run(calls, times, 1, 200000);
        check(c.black_at >= 1000 + DISABLE_WAIT_US, "real Disable: not black before the wait",
              c.black_at, 1000 + DISABLE_WAIT_US);
        check(c.black_at <= 1000 + DISABLE_WAIT_US + 4000, "real Disable: black within a poll",
              c.black_at, 1000 + DISABLE_WAIT_US + 4000);
        check(c.valid == 0, "real Disable: stays black", c.valid, 0);
        check(c.releases == 1, "real Disable: input released once", c.releases, 1);
    }
    /* two Disables, no scanout: the first one's deadline, applied once */
    {
        int calls[] = {DISABLE, DISABLE};
        long times[] = {1000, 30000};
        struct core c = run(calls, times, 2, 200000);
        check(c.black_at <= 1000 + DISABLE_WAIT_US + 4000, "repeated Disable: first deadline",
              c.black_at, 1000 + DISABLE_WAIT_US + 4000);
        check(c.releases == 1, "repeated Disable: released once", c.releases, 1);
    }
    /* a stray one cancelled, then a real one: the real one still blanks, after its own wait */
    {
        int calls[] = {DISABLE, SCANOUT, UPDATE, DISABLE};
        long times[] = {1000, 2000, 3000, 100000};
        struct core c = run(calls, times, 4, 300000);
        check(c.black_at >= 100000 + DISABLE_WAIT_US, "stray then real: not early", c.black_at,
              100000 + DISABLE_WAIT_US);
        check(c.black_at <= 100000 + DISABLE_WAIT_US + 4000, "stray then real: blanks", c.black_at,
              100000 + DISABLE_WAIT_US + 4000);
        check(c.releases == 1, "stray then real: released once", c.releases, 1);
    }
    /* after a real Disable, the display back: a scanout and its update show the picture again */
    {
        int calls[] = {DISABLE, SCANOUT, UPDATE};
        long times[] = {1000, 150000, 151000};
        struct core c = run(calls, times, 3, 200000);
        check(c.valid == 1, "display back after a real Disable", c.valid, 1);
        check(c.releases == 1, "display back: released once", c.releases, 1);
    }
    if (!failures)
        printf("ok\n");
    return failures != 0;
}
