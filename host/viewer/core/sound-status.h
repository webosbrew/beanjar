// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef VIEWER_SOUND_STATUS_H
#define VIEWER_SOUND_STATUS_H
#include <stddef.h>
/* The TV's master volume and speaker mute for the media audio: the file sound-status beside
 * qemu.pid (host/core/beanjar/qemu.py), where QEMU writes what the guest's sound pump sends on the
 * webos.sound port, a line at each change: "level=0x7f00 mute=0 gain=65536". The gain is the one
 * the guest's sound card applies to its own PCM (guest/kernel/modules/lgemu/snd.c), in Q16, 0 when
 * muted. QEMU truncates the file at each start; an empty or missing file is unity, as the card is
 * until the firmware sets its volume. */
#define SOUND_UNITY 0x10000u
struct sound_status {
    char *path;          /* NULL: none, always unity */
    long long size, ino; /* the file as last read (-1: not read yet) */
    long long next_us;   /* the next look (monotonic) */
    unsigned gain;       /* Q16, SOUND_UNITY at most */
};
/* The gain of the last whole line of text (len bytes, a partial last line left out); 0 and *gain
 * unchanged when there is none. Values above unity are unity. */
int sound_status_parse(const char *text, size_t len, unsigned *gain);
void sound_status_init(struct sound_status *s, const char *path);
/* At most every 100 ms (now_us: monotonic): reads the file again if it changed. 1 when the gain
 * changed. */
int sound_status_poll(struct sound_status *s, long long now_us);
void sound_status_close(struct sound_status *s);
#endif
