// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef VIEWER_MEDIA_H
#define VIEWER_MEDIA_H
/* Decoded media from the per-VM helper, scheduled on the guest pipeline clock.
 * The viewer imports native pixel planes or DMA-BUFs, composes GL surfaces and
 * plays 48 kHz float stereo PCM. It contains no compressed-media decoder. */
struct media;
struct media_config {
    const char *prefix;   /* sockets <prefix>-<n>.sock */
    int sdl_audio;        /* play through SDL (else a clock thread consumes the audio) */
    const char *wav;      /* also record the mixed media audio (48 kHz f32 stereo) */
    int audio_latency_ms; /* output latency to compensate, -1: from the SDL buffer size */
    const char *hwdec;    /* helper preference, acknowledged for its next codec-open boundary */
    double volume;        /* the virtual AMP: master gain on the final mix, 1.0 standard
                           * (non-positive also means standard; mute for silence) */
    int mute;             /* the virtual AMP: silence the final mix, still consuming */
    int external_clock;   /* the caller consumes the mix with media_fill on time (pcm-out.c):
                           * no SDL device, no clock thread of media's own */
};
struct media *media_open(const struct media_config *cfg);
/* Main thread, once per loop: moves each video stream to the frame due now. 1 if a picture
 * changed (the window needs a redraw). */
int media_update(struct media *m);
/* Draws the current picture of the video stream reaching the scaler input (input, input_port):
 * the one whose OPEN names that output, else on VDEC the one whose vdec-ch is the port (or, for
 * main, the only such video stream when none matches), into the current GL viewport, stretched to
 * it. Returns 0 when there is no picture. */
int media_draw(struct media *m, unsigned input, unsigned input_port, int main_window);
/* 1 while the helper decodes for this viewer; 0 with decoding off: no media (m NULL, --no-media)
 * or a helper that reports no decoder (built without FFmpeg). A codec the helper cannot decode
 * still counts as decoding on: that window stays black, as on a TV. */
int media_decoding(const struct media *m);
/* With external_clock: the media mix for frames 48 kHz float stereo frames due at host monotonic
 * time when (ns; the configured latency is added), written over out. Any one thread. */
void media_fill(struct media *m, float *out, int frames, long long when);
/* The TV's master volume and speaker mute, as the gain in Q16 (0x10000 unity, 0 silence) the
 * guest's sound card applies to the TV's own sound (sound-status.h), for the media mix too, on
 * top of the virtual AMP's volume and mute. Unity until it is set. Main thread. */
void media_tv_gain(struct media *m, unsigned q16);
void media_close(struct media *m);
#endif
