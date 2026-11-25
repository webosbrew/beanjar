// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The TV's master volume for the media audio (sound-status.h). */
#define _POSIX_C_SOURCE 200809L
#include "sound-status.h"

#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define LOOK_US 100000 /* how often the file is looked at */
#define TAIL    256    /* the end of the file read for its last line */

int sound_status_parse(const char *text, size_t len, unsigned *gain)
{
    /* the last whole line: up to the last newline, from the newline before it */
    size_t end = len;
    while (end && text[end - 1] != '\n')
        end--;
    while (end) {
        size_t start = end - 1;
        while (start && text[start - 1] != '\n')
            start--;
        char line[TAIL + 1];
        size_t n = end - 1 - start < TAIL ? end - 1 - start : TAIL;
        memcpy(line, text + start, n);
        line[n] = 0;
        const char *g = strstr(line, "gain=");
        unsigned long v;
        char *tail;
        if (g && (g == line || g[-1] == ' ')) {
            v = strtoul(g + 5, &tail, 10);
            if (tail != g + 5 && (*tail == 0 || *tail == ' ')) {
                *gain = v > SOUND_UNITY ? SOUND_UNITY : (unsigned)v;
                return 1;
            }
        }
        end = start; /* a line that is not one of ours: the one before */
    }
    return 0;
}

void sound_status_init(struct sound_status *s, const char *path)
{
    memset(s, 0, sizeof *s);
    s->path = path ? strdup(path) : NULL;
    s->size = s->ino = -1;
    s->gain = SOUND_UNITY;
}

int sound_status_poll(struct sound_status *s, long long now_us)
{
    if (!s->path || now_us < s->next_us)
        return 0;
    s->next_us = now_us + LOOK_US;
    unsigned gain = SOUND_UNITY;
    struct stat st;
    int fd = open(s->path, O_RDONLY | O_CLOEXEC);
    if (fd < 0 || fstat(fd, &st) < 0) {
        if (fd >= 0)
            close(fd);
        if (s->size == 0 && s->ino == 0)
            return 0;
        s->size = s->ino = 0; /* none (yet): unity */
    } else {
        if ((long long)st.st_size == s->size && (long long)st.st_ino == s->ino) {
            close(fd);
            return 0;
        }
        s->size = (long long)st.st_size;
        s->ino = (long long)st.st_ino;
        char tail[TAIL];
        off_t at = st.st_size > TAIL ? st.st_size - TAIL : 0;
        ssize_t n = pread(fd, tail, sizeof tail, at);
        close(fd);
        if (n > 0 && !sound_status_parse(tail, (size_t)n, &gain))
            gain = SOUND_UNITY;
    }
    if (gain == s->gain)
        return 0;
    if (gain)
        fprintf(stderr, "media: the TV's volume, gain %.4f (%.2f dB)\n", gain / 65536.0,
                20 * log10(gain / 65536.0));
    else
        fprintf(stderr, "media: the TV's volume, muted\n");
    s->gain = gain;
    return 1;
}

void sound_status_close(struct sound_status *s)
{
    free(s->path);
    s->path = NULL;
}
