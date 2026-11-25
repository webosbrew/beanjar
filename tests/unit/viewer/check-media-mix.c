// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The virtual AMP: master gain and mute on the viewer's final media mix.
 * Links the real media.c (plus its frame/ipc companions for the linker) and
 * drives fill() directly: no GL context, no sockets, no threads of its own.
 * Usage is the twin of tests/integration/media-helper's PCM checks, at the other end. */
#include "../../../host/viewer/core/media.c"

#include <assert.h>
#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>

#define FRAMES 8
#define WHEN   1000000000ll

static struct media m;

static void add_stream(int n, float sample)
{
    struct stream *s = &m.streams[n];
    pthread_mutex_init(&s->lock, NULL);
    pthread_cond_init(&s->cond, NULL);
    s->m = &m;
    s->info.kind = WEBOS_MEDIA_AUDIO;
    s->open = 1;
    s->eos = 1;
    s->have_state = 1;
    s->have_delta = 1;
    s->state.playing = 1;
    s->state.base_time = 0;
    s->offset.start = 1;
    struct chunk *chunk = malloc(sizeof *chunk + FRAMES * 2 * sizeof(float));
    assert(chunk);
    chunk->next = NULL;
    chunk->rt = WHEN - 2000; /* 2 us late: within nudge limits, copied straight */
    chunk->frames = FRAMES;
    chunk->pos = 0;
    for (int i = 0; i < FRAMES * 2; i++)
        chunk->data[i] = sample;
    s->ahead = s->atail = chunk;
    s->aqueued = (int64_t)FRAMES * 1000000000 / OUT_RATE;
}

static void setup(double volume, int mute)
{
    memset(&m, 0, sizeof m);
    m.cfg.volume = volume;
    m.cfg.mute = mute;
    m.tv_gain = 0x10000; /* as media_open leaves it */
    m.tv_applied = 1.0f;
    pthread_mutex_init(&m.audio_lock, NULL);
    add_stream(0, 0.5f);
}

static void check(const float *out, float want, const char *what)
{
    for (int i = 0; i < FRAMES * 2; i++)
        if (out[i] != want) {
            fprintf(stderr, "FAIL %s: sample %d is %f, want %f\n", what, i, out[i], want);
            assert(out[i] == want);
        }
    printf("PASS %s\n", what);
}

int main(void)
{
    float out[FRAMES * 2];
    setup(1.0, 0);
    fill(&m, out, FRAMES, WHEN);
    check(out, 0.5f, "unity mix passes PCM through");
    assert(m.streams[0].audio_frames == FRAMES);
    assert(m.streams[0].underruns == 0 && m.streams[0].nudges == 0);

    setup(0.5, 0);
    fill(&m, out, FRAMES, WHEN);
    check(out, 0.25f, "master gain 0.5 halves the mix");

    setup(1.0, 1);
    fill(&m, out, FRAMES, WHEN);
    check(out, 0.0f, "mute silences the mix while consuming");
    assert(m.streams[0].audio_frames == FRAMES);

    /* the TV's volume: the gain the guest's sound card applies (Q16), held from block to block */
    setup(1.0, 0);
    media_tv_gain(&m, 0x8000);
    m.tv_applied = 0.5f; /* already there: no ramp */
    fill(&m, out, FRAMES, WHEN);
    check(out, 0.25f, "TV volume at Q16 0x8000 halves the mix");

    /* on top of the virtual AMP's volume */
    setup(0.5, 0);
    media_tv_gain(&m, 0x8000);
    m.tv_applied = 0.5f;
    fill(&m, out, FRAMES, WHEN);
    check(out, 0.125f, "TV volume and the virtual AMP multiply");

    /* the TV muted (gain 0) silences while still consuming */
    setup(1.0, 0);
    media_tv_gain(&m, 0);
    m.tv_applied = 0.0f;
    fill(&m, out, FRAMES, WHEN);
    check(out, 0.0f, "TV mute silences the mix");
    assert(m.streams[0].audio_frames == FRAMES);

    /* values above unity are unity, as on the guest's card */
    media_tv_gain(&m, 0x20000);
    assert(m.tv_gain == 0x10000);

    /* a change ramps over one block to the new gain, then holds */
    setup(1.0, 0);
    media_tv_gain(&m, 0);
    fill(&m, out, FRAMES, WHEN);
    for (int i = 0; i < FRAMES; i++) {
        float want = 0.5f * (1.0f - (float)(i + 1) / FRAMES);
        assert(fabsf(out[2 * i] - want) < 1e-6f && out[2 * i] == out[2 * i + 1]);
        assert(i == 0 || out[2 * i] < out[2 * (i - 1)]);
    }
    assert(out[2 * FRAMES - 1] == 0.0f && m.tv_applied == 0.0f);
    printf("PASS a TV volume change ramps over one block\n");

    /* the gain applies to the sum before it is clipped: 0.75 + 0.75 at 0.5 is 0.75, not 0.5 */
    memset(&m, 0, sizeof m);
    m.cfg.volume = 0.5;
    m.tv_gain = 0x10000;
    m.tv_applied = 1.0f;
    pthread_mutex_init(&m.audio_lock, NULL);
    add_stream(0, 0.75f);
    add_stream(1, 0.75f);
    fill(&m, out, FRAMES, WHEN);
    check(out, 0.75f, "master gain applies before the clip");
    return 0;
}
