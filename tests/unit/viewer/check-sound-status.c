// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The TV's master volume for the media mix (host/viewer/core/sound-status.c): the lines the
 * guest's sound pump writes to sound-status (guest/kernel/modules/lgemu/snd.c's format,
 * "level=0x7f00 mute=0 gain=65536"), read from the file's end as QEMU appends them and truncates it
 * at a start. */
#define _POSIX_C_SOURCE 200809L
#include "../../../host/viewer/core/sound-status.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void parses(const char *text, int found, unsigned want, const char *what)
{
    unsigned gain = 12345;
    int r = sound_status_parse(text, strlen(text), &gain);
    if (r != found || (found && gain != want) || (!found && gain != 12345)) {
        fprintf(stderr, "FAIL %s: %d, gain %u\n", what, r, gain);
        abort();
    }
    printf("PASS %s\n", what);
}

static void append(const char *path, const char *text, const char *mode)
{
    FILE *f = fopen(path, mode);
    assert(f && fputs(text, f) >= 0 && fclose(f) == 0);
}

int main(void)
{
    parses("level=0x7f00 mute=0 gain=65536\n", 1, 65536, "unity line");
    parses("level=0x5c00 mute=0 gain=1165\n", 1, 1165, "volume 10 (-35 dB)");
    parses("level=0x7f00 mute=1 gain=0\n", 1, 0, "muted");
    parses("level=0x7f00 mute=0 gain=65536\nlevel=0x7300 mute=0 gain=16422\n", 1, 16422,
           "the last line counts");
    parses("level=0x7300 mute=0 gain=16422\nlevel=0x7f00 mute=1 ga", 1, 16422,
           "a partial last line is left out");
    parses("level=0x7f00 mute=0 gain=", 0, 0, "no whole line");
    parses("", 0, 0, "empty");
    parses("level=0x7f00 mute=0 gain=99999\n", 1, 65536, "above unity is unity");
    parses("gain=40000\nsomething else\n", 1, 40000, "a line not of ours is skipped");
    parses("level=0x7f00 mute=0 again=5\n", 0, 0, "only the gain= word");
    parses("level=0x7f00 mute=0 gain=12x\n", 0, 0, "a number only");

    char dir[] = "/tmp/check-sound-status-XXXXXX";
    assert(mkdtemp(dir));
    char path[64];
    snprintf(path, sizeof path, "%s/sound-status", dir);
    struct sound_status s;
    long long t = 1000000;

    /* no file yet (QEMU not started): unity, no change */
    sound_status_init(&s, path);
    assert(!sound_status_poll(&s, t) && s.gain == SOUND_UNITY);
    /* the guest's first line at its own default, then a volume */
    append(path, "level=0x7f00 mute=0 gain=65536\n", "w");
    assert(!sound_status_poll(&s, t += 100000) && s.gain == SOUND_UNITY);
    append(path, "level=0x7300 mute=0 gain=16422\n", "a");
    assert(!sound_status_poll(&s, t + 50000)); /* looked at no more than every 100 ms */
    assert(sound_status_poll(&s, t += 100000) && s.gain == 16422);
    assert(!sound_status_poll(&s, t += 100000)); /* unchanged */
    /* a line written in two parts: the whole line when it is there */
    append(path, "level=0x7f00 mute=1 ", "a");
    assert(!sound_status_poll(&s, t += 100000) && s.gain == 16422);
    append(path, "gain=0\n", "a");
    assert(sound_status_poll(&s, t += 100000) && s.gain == 0);
    /* many changes: the end of the file is read */
    for (int i = 0; i < 100; i++)
        append(path, i % 2 ? "level=0x7f00 mute=0 gain=65536\n" : "level=0x7f00 mute=1 gain=0\n",
               "a");
    append(path, "level=0x6f00 mute=0 gain=10362\n", "a");
    assert(sound_status_poll(&s, t += 100000) && s.gain == 10362);
    /* QEMU's next start truncates it: unity again until the guest writes */
    append(path, "", "w");
    assert(sound_status_poll(&s, t += 100000) && s.gain == SOUND_UNITY);
    sound_status_close(&s);
    printf("PASS the file followed as QEMU writes it\n");

    /* no path (a bus address that is not a unix socket): always unity */
    sound_status_init(&s, NULL);
    assert(!sound_status_poll(&s, t) && s.gain == SOUND_UNITY);
    sound_status_close(&s);
    unlink(path);
    rmdir(dir);
    return 0;
}
