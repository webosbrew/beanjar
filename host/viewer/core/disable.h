// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef VIEWER_DISABLE_H
#define VIEWER_DISABLE_H
/* QEMU's display Disable, taken as provisional (docs/dev/architecture/viewer.md, "Disable").
 *
 * Under load QEMU's D-Bus display sends a stray Disable between two page flips: vhost-user-gpu
 * releases the previous dmabuf on each scanout, the listener turns that release into a Disable
 * that is normally discarded, and sometimes it escapes 1 to 2 ms before the next ScanoutDMABUF
 * (docs/research/pi-black-flashes.md). Applied at once it blanked one composed picture. So a
 * Disable only arms a deadline; any new scanout before it cancels the Disable, and the core
 * applies it (the picture black, held input released) when the deadline passes, from its poll.
 *
 * DISABLE_WAIT_US: far longer than the stray Disable's lead over its scanout, short enough that a
 * real one (the guest's output off) still shows black at once to the eye. A
 * constant of its own: SCANOUT_WAIT_US (a scanout waiting for its update) is a different wait
 * that happens to be as long. Times are monotonic microseconds. */
#include <stdint.h>

#define DISABLE_WAIT_US 50000

struct disable {
    int64_t at; /* when the pending Disable is applied; 0: none pending */
};

/* a Disable came at now: applied at now + DISABLE_WAIT_US unless a scanout comes first; a second
 * Disable while one is pending keeps the first's deadline */
static inline void disable_arm(struct disable *d, int64_t now)
{
    if (!d->at)
        d->at = now + DISABLE_WAIT_US;
}
/* a scanout came: a pending Disable was QEMU's stray one and is dropped */
static inline void disable_cancel(struct disable *d)
{
    d->at = 0;
}
static inline int disable_pending(const struct disable *d)
{
    return d->at != 0;
}
/* from the poll: 1 once, when a pending Disable's deadline has passed and it is to be applied */
static inline int disable_due(struct disable *d, int64_t now)
{
    if (!d->at || now < d->at)
        return 0;
    d->at = 0;
    return 1;
}

#endif
