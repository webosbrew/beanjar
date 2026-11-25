// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef VIEWER_CORE_H
#define VIEWER_CORE_H
/* libbeanjar-viewer-core.so: the viewer's frame path, shared by the GTK front end (Python,
 * host/ui/beanjar_ui/viewer, through ctypes: tools/gen-ctypes.py generates the binding
 * from the declarations below, so bump WV_ABI on any API change and regenerate)
 * the headless SDL program (core/headless.c) and the VNC server (host/vnc).
 * docs/dev/architecture/viewer.md.
 *
 * The core is a QEMU D-Bus display listener (dmabuf, Unix.Map and 2D scanouts, copied into a
 * cache texture before each reply), LG's cursor (webos.cursor), the video windows (webos.video)
 * with the decoded media streams under the OSD, guest audio and media audio (SDL audio), the
 * keyboard/tablet input to QEMU and the TV remote's IR to the guest's micom (webos.ir). Everything
 * runs on the default GMainContext and in the core's own threads (decoders, audio), which never
 * call back, except the audio clock into wv_audio_sink's function. */
#define WV_ABI 16
struct wv;
typedef void (*wv_callback)(void *data);

int wv_abi(void);
struct wv *wv_new(void);
/* Options by their command-line names (beanjar-viewer --help): "address", "size" (WxH, logical),
 * "straight-alpha", "cursor-socket", "video-socket", "ir-socket", "capture-socket", "media-socket",
 * "no-media", "hwdec", "media-wav", "media-latency", "media-volume" (0 to 2, 1.0 standard),
 * "media-mute", "sound-status" (the TV's master volume and speaker mute for the media mix,
 * sound-status.h; by default beside a unix bus socket, else none: unity), "no-audio", "audio-wav",
 * "sdl-audio", "headless", "osd-alpha", "no-osd", "media-silent", "no-cursor" (the front end shows
 * LG's cursor itself, from wv_cursor: the scene is drawn without it, and neither the pointer nor
 * libim moving it is a change), "hide-planes" (a comma-separated list of "video", "ui" and
 * "cursor", or "none": wv_planes at start), "output-hz" (the pictures a second the redraw callback
 * and wv_poll allow: 0 unpaced; by default the panel's refresh, vm-info.toml's panel_hz, else 60,
 * whatever it is; counters panel_hz, output_hz and pictures, the output's pictures drawn). Flags
 * take NULL or "1". Returns 0, or -1 for an unknown name or a bad value. */
int wv_option(struct wv *v, const char *name, const char *value);
/* make_current: make the GL context the core draws with current (GtkGLArea.make_current); NULL
 * when it always is (headless, VNC). redraw: the composed frame changed (a scanout, the cursor, a
 * video window, a decoded picture, the pointer): render again; it repeats from wv_attach's poll
 * until a wv_render or wv_frame has drawn the change. quit: the display went away or a signal
 * asked to stop. */
void wv_callbacks(struct wv *v, wv_callback make_current, wv_callback redraw, wv_callback quit,
                  void *data);
/* SIGINT/SIGTERM: the first quits cleanly (the quit callback, or wv_poll's return), a second one,
 * or 5 s without a clean exit, ends the process. Install before wv_start. */
void wv_signals(void);
/* The mixed audio for a front end that serves it itself (beanjar-vnc's audio socket): the guest's
 * audio (linearly resampled to 48 kHz) and the media audio, as 48 kHz signed 16-bit stereo,
 * interleaved, in 10 ms blocks, called from the core's audio clock thread; it must not block. Set
 * before wv_start. The core then neither plays nor opens SDL audio: that clock consumes the media
 * mix on time in place of media's own (as with media-silent), whether or not anyone listens, and
 * the guest's audio listener is registered even with no-audio. */
typedef void (*wv_audio_fn)(void *data, const short *pcm, int frames);
void wv_audio_sink(struct wv *v, wv_audio_fn fn, void *data);
/* Whether the sink has listeners: without, the clock skips the guest's conversion, the mix and
 * the call (the media mix is still consumed). Any thread; starts as 0. */
void wv_audio_wanted(struct wv *v, int wanted);
/* With the GL context current: GL objects, the display connection (dies on failure, as QEMU's
 * listener must be registered to show anything), the cursor, video and media sources, audio. */
void wv_start(struct wv *v);
/* GTK: polls the sources from a 4 ms GLib timeout and calls redraw/quit as needed. */
void wv_attach(struct wv *v);
/* Headless: one poll step. Bit 0: redraw; bit 1: quit. */
int wv_poll(struct wv *v);
/* The logical size of the drawing area (input and cursor coordinates). */
void wv_view(struct wv *v, int width, int height);
/* Draws the scene into the currently bound framebuffer, width x height device pixels. */
void wv_render(struct wv *v, int width, int height);
/* The letterboxed picture in logical pixels (as shown, turned); 0 before the first scanout. */
int wv_picture(struct wv *v, int *x, int *y, int *w, int *h);
/* wv_render shows the picture turned clockwise by degrees (0, 90, 180 or 270), as a set's panel
 * turned to portrait shows it (beanjar_ui.viewer: the guest's run/rotation); the area's logical
 * coordinates of wv_motion, wv_button and wv_touch are mapped back into the unturned picture.
 * Screenshots and wv_frame stay unturned (the panel's own pixels). */
void wv_turn(struct wv *v, int degrees);
/* The scene rendered offscreen at width x height, as PPM (guest only); with osd-alpha also the
 * OSD as received, PATH.osd.pam. 0 on success. */
int wv_screenshot(struct wv *v, const char *path, int width, int height);
/* A front end's own answers to the bus's Screenshot calls (beanjar-vnc's thumbnails), on the main
 * loop: with fn set, a valid call is queued instead of answered with wv_screenshot, and fn(data)
 * called. wv_screenshot_request gives the oldest queued call's path and size (NULL: none), and
 * wv_screenshot_reply answers it, then or later: failed 0 written, > 0 an error, < 0 taken by
 * wv_screenshot (with LG's cursor) as without the hook. */
void wv_screenshot_hook(struct wv *v, wv_callback fn, void *data);
const char *wv_screenshot_request(struct wv *v, int *width, int *height);
void wv_screenshot_reply(struct wv *v, int failed);
/* The scene composed offscreen at width x height (as wv_render: video, OSD, cursor) and read into
 * pixels: RGBA bytes, width*4 per row, top row first. It is the render the redraw callback asked
 * for (wv_screenshot is not). 0 on success. */
int wv_frame(struct wv *v, void *pixels, int width, int height);
/* The same render into the framebuffer object fbo (its colour attachment at least width x height),
 * top row first in its memory, as wv_frame's pixels: no read back and no wait (beanjar-vnc's GBM
 * buffers, handed on as dma-bufs). Leaves the framebuffer binding as it was. 0 on success. */
int wv_frame_fbo(struct wv *v, unsigned fbo, int width, int height);
/* The planes of the scene: the video windows (decoded media, under the UI), the UI (the guest's
 * OSD, the scanout) and LG's cursor. */
#define WV_PLANE_VIDEO  1
#define WV_PLANE_UI     2
#define WV_PLANE_CURSOR 4
/* The planes this front end hides (WV_PLANE_* bits), a view setting: wv_render, wv_frame*,
 * wv_screenshot and wv_cursor leave them out (a hidden video window is black, as the background;
 * a hidden UI leaves the video alone). The guest's capture and the bus's Screenshot calls always
 * get the whole scene. hidden < 0 only reads. Returns the planes hidden now. */
int wv_planes(struct wv *v, int hidden);
/* LG's cursor (webos.cursor), for a front end that shows it itself ("no-cursor"). Returns 0 while
 * it is hidden or has no picture, otherwise a serial that stays the same while what follows does.
 * With rgba (room for 256*256*4 bytes) it also gives that: the picture, width x height RGBA with
 * straight alpha, width*4 per row, top row first; the hotspot in its pixels; and view_w x view_h,
 * the logical pixels it covers in the view, scaled like the TV picture (as the core draws it). */
unsigned wv_cursor(struct wv *v, void *rgba, int *width, int *height, int *hot_x, int *hot_y,
                   int *view_w, int *view_h);
/* Input. key_evdev: a PC key (Linux code): the remote's own functions (Super/Home, Escape, the
 * volume keys) as the remote's IR buttons, the rest to the virtio keyboard; returns 0 if it has no
 * mapping. ir: a TV remote button, LG's IR data code (include/webos-ir.h), to the guest's micom
 * over webos.ir. local_key: the set's own key (WEBOS_LOCAL_*) to the micom, which tells a short
 * press from a long one. Coordinates are logical, in the drawing area. button: 0 left, 1 middle, 2
 * right. */
int wv_key_evdev(struct wv *v, unsigned evdev, int down);
void wv_ir(struct wv *v, unsigned code, int down);
void wv_local_key(struct wv *v, unsigned code, int down);
void wv_motion(struct wv *v, int x, int y);
void wv_leave(struct wv *v);
void wv_button(struct wv *v, unsigned button, int down, int x, int y);
void wv_wheel(struct wv *v, int steps);
/* A real touch (a touchscreen's, not the mouse's) to the guest's touch screen: kind 0 begin, 1
 * move, 2 end, 3 cancel (input.h's INPUT_TOUCH_*), slot 0 to 9, logical coordinates. */
void wv_touch(struct wv *v, unsigned kind, unsigned slot, int x, int y);
void wv_release(struct wv *v);
/* 1 while the host pointer is inside the displayed TV picture, independent of
 * webos.cursor visibility/shape/connection. The front end counts each transition. */
int wv_pointer_blank(struct wv *v);
void wv_pointer_blank_set(struct wv *v);
/* The TV is in standby (QMP webos.standby): LG's cursor is not drawn (nor given by wv_cursor),
 * as the guest is frozen and never sends its hide. After the wake it is what the guest last sent.
 */
void wv_standby(struct wv *v, int standby);
/* Nobody looks (beanjar-vnc without clients): QEMU's updates are acknowledged without being
 * uploaded or copied (UpdateMap, UpdateDMABUF; a 2D Update only copied into the core's own copy
 * of the surface), only noted; the picture is brought up to date from the shared surface, that
 * copy or the dmabuf when it is drawn next (wv_render, wv_frame, a screenshot) or when idle ends.
 * Scanouts, the cursor, the video windows and the media go on as before (a decoded picture is
 * uploaded only when drawn anyway). "skipped" counts the updates not uploaded. */
void wv_idle(struct wv *v, int idle);
/* Counters: "frames", "fenced" (dmabuf updates answered when their copy's fence signals, not
 * after a glFinish), "imports", "maps", "cursor_draws", "input_calls", "input_errors",
 * "input_pending", "ir_sent", "ir_errors", "valid", "width", "height", "dma",
 * "cursor_blank_sets", "standby", "skipped", "dmabuf_updates" (UpdateDMABUF calls from
 * QEMU), "reply_us" and "reply_max_us" (how long QEMU waited for their answers, in all and at
 * most, in microseconds). */
long wv_counter(struct wv *v, const char *name);
/* Releases what is held, waits up to 3 s for pending input calls, prints the counters (and
 * extra), closes everything and frees v. Returns 1 if the run failed (no frame, input errors). */
int wv_finish(struct wv *v, const char *extra);
#endif
