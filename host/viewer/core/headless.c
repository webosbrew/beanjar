// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#define _POSIX_C_SOURCE 200809L
/* viewer-headless: the viewer without a desktop window, for tests and scripts
 * (--headless, --replay, --screenshot; README "Headless validation"). SDL's offscreen video
 * driver provides the EGL/GLES context; the scene, the sources and the input are the core's
 * (core.h). The desktop viewer is the GTK front end (beanjar_ui.viewer); beanjar-viewer runs this
 * program for the options above.
 * --capture-sink: the on-demand render sink of a VM with no front end (beanjar's capture relay,
 * host/core/beanjar/capture_relay.py, starts and stops it): it serves the guest's captures on the
 * capture socket and nothing else. Idle (wv_idle: QEMU's updates are only noted, a capture brings
 * the picture up to date), no output drawn, no audio heard, no IR socket. */
#include "core.h"
#include "ir.h"
#include <SDL.h>
#include <glib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void die(const char *message)
{
    fprintf(stderr, "viewer: %s\n", message);
    exit(1);
}
/* SDL scancodes of the replay files -> Linux evdev codes (the core's PC keyboard map). */
static unsigned evdev(SDL_Scancode s)
{
    static const unsigned short codes[SDL_NUM_SCANCODES] = {
        [SDL_SCANCODE_A] = 30,
        [SDL_SCANCODE_B] = 48,
        [SDL_SCANCODE_C] = 46,
        [SDL_SCANCODE_D] = 32,
        [SDL_SCANCODE_E] = 18,
        [SDL_SCANCODE_F] = 33,
        [SDL_SCANCODE_G] = 34,
        [SDL_SCANCODE_H] = 35,
        [SDL_SCANCODE_I] = 23,
        [SDL_SCANCODE_J] = 36,
        [SDL_SCANCODE_K] = 37,
        [SDL_SCANCODE_L] = 38,
        [SDL_SCANCODE_M] = 50,
        [SDL_SCANCODE_N] = 49,
        [SDL_SCANCODE_O] = 24,
        [SDL_SCANCODE_P] = 25,
        [SDL_SCANCODE_Q] = 16,
        [SDL_SCANCODE_R] = 19,
        [SDL_SCANCODE_S] = 31,
        [SDL_SCANCODE_T] = 20,
        [SDL_SCANCODE_U] = 22,
        [SDL_SCANCODE_V] = 47,
        [SDL_SCANCODE_W] = 17,
        [SDL_SCANCODE_X] = 45,
        [SDL_SCANCODE_Y] = 21,
        [SDL_SCANCODE_Z] = 44,
        [SDL_SCANCODE_1] = 2,
        [SDL_SCANCODE_2] = 3,
        [SDL_SCANCODE_3] = 4,
        [SDL_SCANCODE_4] = 5,
        [SDL_SCANCODE_5] = 6,
        [SDL_SCANCODE_6] = 7,
        [SDL_SCANCODE_7] = 8,
        [SDL_SCANCODE_8] = 9,
        [SDL_SCANCODE_9] = 10,
        [SDL_SCANCODE_0] = 11,
        [SDL_SCANCODE_RETURN] = 28,
        [SDL_SCANCODE_ESCAPE] = 1,
        [SDL_SCANCODE_BACKSPACE] = 14,
        [SDL_SCANCODE_TAB] = 15,
        [SDL_SCANCODE_SPACE] = 57,
        [SDL_SCANCODE_MINUS] = 12,
        [SDL_SCANCODE_EQUALS] = 13,
        [SDL_SCANCODE_LEFTBRACKET] = 26,
        [SDL_SCANCODE_RIGHTBRACKET] = 27,
        [SDL_SCANCODE_BACKSLASH] = 43,
        [SDL_SCANCODE_SEMICOLON] = 39,
        [SDL_SCANCODE_APOSTROPHE] = 40,
        [SDL_SCANCODE_GRAVE] = 41,
        [SDL_SCANCODE_COMMA] = 51,
        [SDL_SCANCODE_PERIOD] = 52,
        [SDL_SCANCODE_SLASH] = 53,
        [SDL_SCANCODE_CAPSLOCK] = 58,
        [SDL_SCANCODE_F1] = 59,
        [SDL_SCANCODE_F2] = 60,
        [SDL_SCANCODE_F3] = 61,
        [SDL_SCANCODE_F4] = 62,
        [SDL_SCANCODE_F5] = 63,
        [SDL_SCANCODE_F6] = 64,
        [SDL_SCANCODE_F7] = 65,
        [SDL_SCANCODE_F8] = 66,
        [SDL_SCANCODE_F9] = 67,
        [SDL_SCANCODE_F10] = 68,
        [SDL_SCANCODE_F11] = 87,
        [SDL_SCANCODE_F12] = 88,
        [SDL_SCANCODE_INSERT] = 110,
        [SDL_SCANCODE_DELETE] = 111,
        [SDL_SCANCODE_HOME] = 102,
        [SDL_SCANCODE_END] = 107,
        [SDL_SCANCODE_PAGEUP] = 104,
        [SDL_SCANCODE_PAGEDOWN] = 109,
        [SDL_SCANCODE_UP] = 103,
        [SDL_SCANCODE_DOWN] = 108,
        [SDL_SCANCODE_LEFT] = 105,
        [SDL_SCANCODE_RIGHT] = 106,
        [SDL_SCANCODE_LCTRL] = 29,
        [SDL_SCANCODE_RCTRL] = 97,
        [SDL_SCANCODE_LSHIFT] = 42,
        [SDL_SCANCODE_RSHIFT] = 54,
        [SDL_SCANCODE_LALT] = 56,
        [SDL_SCANCODE_RALT] = 100,
        [SDL_SCANCODE_LGUI] = 125,
        [SDL_SCANCODE_RGUI] = 126,
        [SDL_SCANCODE_KP_ENTER] = 96,
        [SDL_SCANCODE_MUTE] = 113,
        [SDL_SCANCODE_VOLUMEDOWN] = 114,
        [SDL_SCANCODE_VOLUMEUP] = 115,
    };
    return s > 0 && s < SDL_NUM_SCANCODES ? codes[s] : 0;
}
/* Replay lines: milliseconds after the first scanout, then an operation; coordinates are
 * logical window pixels. irdown/irup CODE: a TV remote button, LG's IR data code (0x44).
 * localdown/localup CODE: the set's own key, its micom code (0xea, include/webos-ir.h). */
enum op { MOTION, DOWN, UP, WHEEL, KEYDOWN, KEYUP, IRDOWN, IRUP, LOCALDOWN, LOCALUP, BLUR, RESIZE };
struct replay {
    unsigned ms;
    enum op op;
    int a, b, x, y;
};
static GArray *load_replay(const char *path)
{
    GArray *events = g_array_new(FALSE, FALSE, sizeof(struct replay));
    FILE *f = fopen(path, "r");
    if (!f)
        die("cannot open replay");
    char line[256], op[32], arg[64];
    unsigned previous = 0;
    int x = 0, y = 0;
    while (fgets(line, sizeof(line), f)) {
        if (line[0] == '#' || line[0] == '\n')
            continue;
        struct replay r = {0};
        if (sscanf(line, "%u %31s", &r.ms, op) != 2 || r.ms < previous)
            die("invalid replay timing");
        previous = r.ms;
        if (!strcmp(op, "motion")) {
            if (sscanf(line, "%u %31s %d %d", &r.ms, op, &x, &y) != 4)
                die("invalid replay motion");
            r.op = MOTION;
            r.a = x;
            r.b = y;
        } else if (!strcmp(op, "resize")) {
            if (sscanf(line, "%u %31s %d %d", &r.ms, op, &r.a, &r.b) != 4 || r.a < 64 || r.b < 64 ||
                r.a > 8192 || r.b > 8192)
                die("invalid replay size");
            r.op = RESIZE;
        } else if (!strcmp(op, "down") || !strcmp(op, "up")) {
            if (sscanf(line, "%u %31s %d", &r.ms, op, &r.a) != 3 || r.a < 1 || r.a > 3)
                die("invalid replay button");
            r.op = !strcmp(op, "down") ? DOWN : UP;
            r.a = r.a - 1; /* SDL buttons 1/2/3: left/middle/right */
            r.x = x;
            r.y = y; /* where the last motion left the pointer */
        } else if (!strcmp(op, "wheel")) {
            if (sscanf(line, "%u %31s %d", &r.ms, op, &r.a) != 3)
                die("invalid replay wheel");
            r.op = WHEEL;
        } else if (!strcmp(op, "keydown") || !strcmp(op, "keyup")) {
            if (sscanf(line, "%u %31s %63[^\n]", &r.ms, op, arg) != 3)
                die("invalid replay key");
            r.op = !strcmp(op, "keydown") ? KEYDOWN : KEYUP;
            r.a = (int)evdev(SDL_GetScancodeFromName(arg));
            if (!r.a)
                die("unknown replay key name");
        } else if (!strcmp(op, "irdown") || !strcmp(op, "irup")) {
            if (sscanf(line, "%u %31s %i", &r.ms, op, &r.a) != 3 || r.a < 0 ||
                !ir_code_valid((unsigned)r.a))
                die("invalid replay IR code");
            r.op = !strcmp(op, "irdown") ? IRDOWN : IRUP;
        } else if (!strcmp(op, "localdown") || !strcmp(op, "localup")) {
            if (sscanf(line, "%u %31s %i", &r.ms, op, &r.a) != 3 || r.a < 0 || r.a > 255)
                die("invalid replay local key code");
            r.op = !strcmp(op, "localdown") ? LOCALDOWN : LOCALUP;
        } else if (!strcmp(op, "blur")) {
            r.op = BLUR;
        } else
            die("unknown replay operation");
        g_array_append_val(events, r);
    }
    fclose(f);
    return events;
}
static const char usage[] =
    "usage: viewer-headless BUS_ADDRESS [--seconds N] [--size WxH] [--straight-alpha]\n"
    "  [--cursor-socket PATH] [--video-socket PATH] [--ir-socket PATH] [--media-socket PREFIX] "
    "[--capture-socket PATH]\n"
    "  [--no-media] [--hwdec auto|none|TYPE] [--media-wav OUT.wav] [--media-latency MS] "
    "[--media-volume 0..2] [--media-mute] [--sound-status PATH]\n"
    "  [--no-audio] [--audio-wav OUT.wav] [--sdl-audio] [--media-silent] [--replay FILE]\n"
    "  [--screenshot OUT.ppm [--screenshot-every MS | --screenshot-after MS] [--osd-alpha]] "
    "[--capture-sink] [--capture-control FILE "
    "--screenshot-every MS] [--no-osd] [--no-cursor] [--hide-planes video,ui,cursor]";
int main(int argc, char **argv)
{
    int w = 1280, h = 720;
    double seconds = 0;
    unsigned shot_every = 0;
    long shot_after = -1; /* --screenshot-after MS */
    const char *shot = NULL, *replay_path = NULL, *ctl = NULL;
    int sink = 0;
    wv_signals();
    struct wv *v = wv_new();
    wv_option(v, "headless", NULL);
    static const char *const valued[] = {"cursor-socket",
                                         "video-socket",
                                         "ir-socket",
                                         "media-socket",
                                         "capture-socket",
                                         "hwdec",
                                         "media-wav",
                                         "media-latency",
                                         "media-volume",
                                         "audio-wav",
                                         "hide-planes",
                                         "sound-status",
                                         NULL};
    static const char *const flags[] = {"straight-alpha", "no-media", "no-audio",     "sdl-audio",
                                        "osd-alpha",      "no-osd",   "media-silent", "media-mute",
                                        "no-cursor",      NULL};
    const char *addr = NULL;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i], *name = a[0] == '-' && a[1] == '-' ? a + 2 : "";
        int done = 0;
        if (!strcmp(a, "--headless"))
            continue;
        if (!strcmp(a, "--capture-sink")) {
            sink = 1;
            continue;
        }
        if (!strcmp(a, "--seconds") && i + 1 < argc) {
            seconds = g_ascii_strtod(argv[++i], NULL);
            continue;
        }
        if (!strcmp(a, "--screenshot") && i + 1 < argc) {
            shot = argv[++i];
            continue;
        }
        if (!strcmp(a, "--screenshot-after") && i + 1 < argc) {
            shot_after = atol(argv[++i]);
            continue;
        }
        if (!strcmp(a, "--screenshot-every") && i + 1 < argc) {
            shot_every = (unsigned)atoi(argv[++i]);
            continue;
        }
        if (!strcmp(a, "--capture-control") && i + 1 < argc) {
            ctl = argv[++i];
            continue;
        }
        if (!strcmp(a, "--replay") && i + 1 < argc) {
            replay_path = argv[++i];
            continue;
        }
        if (!strcmp(a, "--size") && i + 1 < argc) {
            char tail;
            if (sscanf(argv[++i], "%dx%d%c", &w, &h, &tail) != 2 || w < 64 || h < 64 || w > 8192 ||
                h > 8192)
                die("invalid window size");
            continue;
        }
        for (int k = 0; !done && a[0] == '-' && valued[k]; k++)
            if (!strcmp(name, valued[k]) && i + 1 < argc) {
                if (wv_option(v, name, argv[++i]))
                    die(usage);
                done = 1;
            }
        for (int k = 0; !done && a[0] == '-' && flags[k]; k++)
            if (!strcmp(name, flags[k])) {
                wv_option(v, name, NULL);
                done = 1;
            }
        if (done)
            continue;
        if (a[0] != '-' && !addr) {
            addr = a;
            continue;
        }
        die(usage);
    }
    if (!addr || seconds < 0)
        die(usage);
    if (sink && (shot || replay_path || ctl))
        die("--capture-sink takes no --screenshot, --replay or --capture-control");
    if (sink) {
        wv_option(v, "no-ir", NULL);
        wv_option(v, "no-audio", NULL);
        wv_option(v, "media-silent", NULL);
    }
    if (shot_every && !shot && !ctl)
        die("--screenshot-every needs --screenshot OUT.ppm or --capture-control FILE");
    wv_option(v, "address", addr);
    SDL_SetHint(SDL_HINT_VIDEODRIVER, "offscreen");
    SDL_SetHint(SDL_HINT_OPENGL_ES_DRIVER, "1");
    SDL_SetHint(SDL_HINT_NO_SIGNAL_HANDLERS, "1");
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS) < 0)
        die(SDL_GetError());
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_ES);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 2);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 0);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 1);
    SDL_Window *window = SDL_CreateWindow(
        "Beanjar Viewer", SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, w, h,
        SDL_WINDOW_OPENGL | SDL_WINDOW_RESIZABLE | SDL_WINDOW_ALLOW_HIGHDPI | SDL_WINDOW_HIDDEN);
    if (!window)
        die(SDL_GetError());
    SDL_GLContext context = SDL_GL_CreateContext(window);
    if (!context)
        die(SDL_GetError());
    SDL_GL_SetSwapInterval(0);
    fprintf(stderr, "SDL %s\n", SDL_GetCurrentVideoDriver());
    GArray *replay = replay_path ? load_replay(replay_path) : NULL;
    wv_view(v, w, h);
    wv_start(v);
    if (sink)
        wv_idle(v, 1);
    gint64 start = g_get_monotonic_time(), first = 0;
    guint next = 0;
    unsigned shot_next = 0, ctl_next = 0;
    gint64 ctl_checked = 0, ctl_start = 0;
    char ctl_out[512] = "";
    int quit = 0, dw, dh;
    while (!quit) {
        while (g_main_context_iteration(NULL, FALSE)) {
        }
        gint64 now = g_get_monotonic_time();
        if (wv_counter(v, "valid") && !first)
            first = now;
        int r = wv_poll(v);
        if (r & 2)
            quit = 1;
        SDL_GetWindowSize(window, &w, &h);
        SDL_GL_GetDrawableSize(window, &dw, &dh);
        wv_view(v, w, h);
        /* --capture-control FILE: while FILE exists, its first line names OUT.ppm; every
         * --screenshot-every MS, OUT-<ms since it appeared>.ppm at 480x270 (scripts/regress's
         * decode cases, one viewer for many) */
        if (ctl && shot_every && first && now - ctl_checked >= 100000) {
            char line[512] = "";
            FILE *f = fopen(ctl, "r");
            ctl_checked = now;
            if (f) {
                if (!fgets(line, sizeof line, f))
                    line[0] = 0;
                fclose(f);
            }
            line[strcspn(line, "\n")] = 0;
            if (strcmp(line, ctl_out)) {
                snprintf(ctl_out, sizeof ctl_out, "%s", line);
                ctl_start = now;
                ctl_next = 0;
            }
        }
        if (ctl_out[0] && (now - ctl_start) / 1000 >= ctl_next) {
            char *base = g_strdup(ctl_out), *dot = strrchr(base, '.');
            if (dot && !strchr(dot, '/'))
                *dot = 0;
            char *name = g_strdup_printf("%s-%06u.ppm", base, ctl_next);
            if (wv_screenshot(v, name, 480, 270))
                fprintf(stderr, "viewer: capture %s failed\n", name);
            g_free(name);
            g_free(base);
            ctl_next += shot_every;
        }
        /* --screenshot-every: OUT-<ms after the first scanout>.ppm as well as the final OUT. */
        if (first && shot && shot_every && (now - first) / 1000 >= shot_next) {
            char *base = g_strdup(shot), *dot = strrchr(base, '.');
            if (dot && !strchr(dot, '/'))
                *dot = 0;
            char *name = g_strdup_printf("%s-%06u.ppm", base, shot_next);
            if (wv_screenshot(v, name, dw, dh))
                die("screenshot failed");
            g_free(name);
            g_free(base);
            shot_next += shot_every;
        }
        /* --screenshot-after MS: OUT once, MS after the first scanout, then exit */
        if (first && shot && shot_after >= 0 && (now - first) / 1000 >= shot_after) {
            if (wv_screenshot(v, shot, dw, dh))
                die("screenshot failed");
            shot = NULL;
            break;
        }
        if (first && replay)
            while (next < replay->len) {
                struct replay *e = &g_array_index(replay, struct replay, next);
                if ((now - first) / 1000 < e->ms)
                    break;
                switch (e->op) {
                case MOTION:
                    wv_motion(v, e->a, e->b);
                    break;
                case DOWN:
                case UP:
                    wv_button(v, (unsigned)e->a, e->op == DOWN, e->x, e->y);
                    break;
                case WHEEL:
                    wv_wheel(v, e->a);
                    break;
                case KEYDOWN:
                case KEYUP:
                    wv_key_evdev(v, (unsigned)e->a, e->op == KEYDOWN);
                    break;
                case IRDOWN:
                case IRUP:
                    wv_ir(v, (unsigned)e->a, e->op == IRDOWN);
                    break;
                case LOCALDOWN:
                case LOCALUP:
                    wv_local_key(v, (unsigned)e->a, e->op == LOCALDOWN);
                    break;
                case BLUR:
                    wv_release(v);
                    break;
                case RESIZE:
                    SDL_SetWindowSize(window, e->a, e->b);
                    SDL_GetWindowSize(window, &w, &h);
                    SDL_GL_GetDrawableSize(window, &dw, &dh);
                    wv_view(v, w, h);
                    r |= 1;
                    break;
                }
                next++;
            }
        if ((r & 1) && !sink) {
            wv_render(v, dw, dh);
            SDL_GL_SwapWindow(window);
        }
        SDL_Event event;
        while (SDL_PollEvent(&event))
            if (event.type == SDL_QUIT)
                quit = 1;
        if (seconds > 0 && (now - start) / 1000000.0 >= seconds)
            break;
        SDL_Delay(sink ? 5 : 2);
    }
    SDL_GetWindowSize(window, &w, &h);
    SDL_GL_GetDrawableSize(window, &dw, &dh);
    if (shot && wv_counter(v, "valid"))
        wv_screenshot(v, shot, dw, dh);
    char *extra = g_strdup_printf(" replay=%u/%u", next, replay ? replay->len : 0);
    int failed = wv_finish(v, extra) || (replay && next < replay->len);
    g_free(extra);
    if (replay)
        g_array_unref(replay);
    SDL_GL_DeleteContext(context);
    SDL_DestroyWindow(window);
    SDL_Quit();
    return failed ? 1 : 0;
}
