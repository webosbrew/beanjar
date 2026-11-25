# Guest and media audio

The viewer has two PCM inputs: guest audio from QEMU's D-Bus audio service and media audio
decoded/resampled by the per-VM helper from the [compressed media transport](video.md). The GTK UI uses the shared C
viewer core.

## Guest PCM

The guest uses virtio-sound. QEMU's D-Bus audio backend delivers PCM to the viewer's audio
listener, which handles stream setup, writes and teardown and plays it through the host audio
device. Headless capture can record PCM without a window. The firmware's PulseAudio path and
the emulator's ALSA card remain distinct from FFmpeg decoding on the host.

LG's master volume and mute are represented by the emulator's audio controls and applied to
the resulting output. The gain follows the PCM instead of assuming that every guest application already
scaled its samples.

## Host-decoded media

`host/media/decode/` in `media-helper` owns compressed audio decoding and resampling through
FFmpeg. It sends timestamped 48 kHz interleaved float-stereo PCM in sealed memfds over the
decoded host IPC. The viewer maps/copies those samples, releases the storage, and owns mixing,
playback and drift correction against the helper's snapshot of the guest pipeline clock.
Without a sound device the headless path consumes and records samples in real time.

LG's master volume and speaker mute apply to media audio as they do to guest PCM, at one place
on the host: the viewer's final media mix (`core/media.c`). The guest's card scales its own PCM
in the guest kernel, so the same value has to leave the guest: lgemu.ko's `snd.c` gives the gain
it sets on the card (Q16, 0 when muted) on `/dev/lgemu-sound` as a line at each change, the
initramfs's `virtio-sound` pump copies it to the `webos.sound` port, and QEMU writes it to
`sound-status` beside `qemu.pid`. The core (`core/sound-status.c`) reads the file's last whole
line every 100 ms, beside the bus socket by default or `--sound-status PATH`; a missing or empty
file is unity, as the card is before the firmware sets a volume. A change ramps over one mixing
block. The viewer's own virtual AMP (`--media-volume`, `--media-mute`) multiplies on top. A viewer
whose bus address is not a unix socket in the run directory, with no `--sound-status`, plays
media at unity. Which firmware lines reach the card's gain at all is listed in
[viewer audio research](../../research/viewer-audio.md#implemented-the-volume-after-the-pcm-2026-10-04).

The helper continues decoding and retiring expired output without a viewer; the viewer has no
codec or resampler dependency. `MEDIAHUB=0` supplies no media decoder. See
[the video architecture](video.md#transport-and-session-ownership)
for bounds, clock mapping, generations and FD ownership. Media audio and guest PCM remain
separate inputs, so a successful test of one does not establish the other.

## Listener ownership

`core/audio.c` owns the guest AudioOutListener on a dedicated thread and GMainContext.
It initiates the asynchronous RegisterOutListener call there, authenticates the returned
peer there, and registers the method vtable on that context before starting message
processing. Guest Writes therefore keep arriving while the display's default context
composes or reads back a frame. `pcm-out.c` keeps its separate 10 ms mixing clock and
locked guest FIFO; this change does not alter its buffer sizes or the wire formats.

Init, Write, SetVolume, SetEnabled and Fini remain ordered on one listener context.
The tap and its data are installed before the thread starts. SDL device/queue handling
and its underrun poll also belong to that thread, with the shared SDL lifecycle guard
still coordinating with media audio. Optional WAV capture stays synchronous on its
owner thread; slow recording storage can still delay that listener and is not a new
unbounded recording queue.

Shutdown first cancels async registration and the peer authentication's blocking I/O.
A source attached explicitly to the worker context requests its stop; pending
registration must finish before that loop exits. The worker unregisters the object,
closes the peer and finalizes WAV/SDL state. The caller joins it before releasing its
context or freeing the tap's PCM sink. A viewer that closes immediately after starting
therefore has no callback left referring to freed viewer state.

`check-audio-context` runs against fake QEMU without ever dispatching the default main
context, checks Writes and their maximum gap, then exercises immediate teardown and
cancellation against a peer that never answers authentication and a manager that never
replies to registration. The VNC tests continue
to check unchanged WAV/PCM/Opus delivery; the GTK and SDL tests cover the shared
desktop lifecycle.

## Over VNC and in a browser

The GTK viewer plays the two inputs on two SDL devices and lets the desktop's sound server mix
them. beanjar-vnc has no sound device: the core mixes them itself on a 10 ms clock (`pcm-out.c`,
`wv_audio_sink`), the guest's PCM linearly resampled from 44.1 to 48 kHz, and beanjar-vnc serves
the mix as Opus or PCM on an owner-only socket, which the web viewer's speaker plays
([vnc](vnc.md#audio), [web viewer](web-viewer.md#sound)). Opus is libopus's, in beanjar-vnc; the
core still links no codec.

## Validation

Use the [TV feature suite](../guides/build.md#tv-feature-suite) and the viewer's capture options
to check the relevant path. A recorded waveform verifies delivered samples; it does not by
itself verify acoustic output or a host device's latency.
