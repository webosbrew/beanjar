// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* A scaler window's placement (host/viewer/core/video-place.h): without a crop the picture is the
 * window; live TV's overscan ([11,7,682,466] of 704x480) and a 4:3 zoom draw the whole source
 * larger, so that the crop fills the window, clipped to it; a crop outside the source, or the
 * whole source, is no crop; a window partly off the output is clipped to the output. */
#include "../../../host/viewer/core/video-place.h"
#include <stdio.h>
#include <string.h>

static int failures;
static void box(const char *what, struct video_box got, int x, int y, int w, int h)
{
    if (got.x != x || got.y != y || got.w != w || got.h != h) {
        printf("FAIL %s: %d,%d %dx%d, want %d,%d %dx%d\n", what, got.x, got.y, got.w, got.h, x, y,
               w, h);
        failures++;
    }
}
static void yes(int ok, const char *what)
{
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

static struct webos_video_context window(int x, int y, unsigned w, unsigned h)
{
    struct webos_video_context c;
    memset(&c, 0, sizeof c);
    strcpy(c.context, "MAIN");
    c.registered = c.connected = c.has_rect = 1;
    c.x = x;
    c.y = y;
    c.w = w;
    c.h = h;
    return c;
}

int main(void)
{
    struct video_box full = {0, 0, 1920, 1080};

    /* full screen, no crop: picture = window = output */
    struct webos_video_context c = window(0, 0, 1920, 1080);
    c.width = 704;
    c.height = 480;
    struct video_place p = video_place(&c, 1920, 1080, full);
    box("no crop: clip", p.clip, 0, 0, 1920, 1080);
    box("no crop: picture", p.picture, 0, 0, 1920, 1080);
    yes(p.left == 0 && p.top == 1080, "no crop: window corner");

    /* the whole source as the crop is no crop */
    c.crop_w = 704;
    c.crop_h = 480;
    yes(!video_crop(&c), "whole source is no crop");
    box("whole source: picture", video_place(&c, 1920, 1080, full).picture, 0, 0, 1920, 1080);

    /* live TV's overscan: [11,7,682,466] of 704x480 fills the screen; the source is
     * 1920 * 704 / 682 = 1981.9 wide, starting 11 * 1920 / 682 = 31.0 left of the screen;
     * 1080 * 480 / 466 = 1112.4 high, 7 * 1080 / 466 = 16.2 above it (GL: its bottom 16.2 below) */
    c.crop_x = 11;
    c.crop_y = 7;
    c.crop_w = 682;
    c.crop_h = 466;
    yes(video_crop(&c), "overscan is a crop");
    p = video_place(&c, 1920, 1080, full);
    box("overscan: clip", p.clip, 0, 0, 1920, 1080);
    box("overscan: picture", p.picture, -31, -16, 1982, 1112);

    /* the same in a 960x540 output with bars (a window 1280x720 at 320,180 of the drawable) */
    p = video_place(&c, 1920, 1080, (struct video_box){320, 180, 960, 540});
    box("overscan scaled: clip", p.clip, 320, 180, 960, 540);
    box("overscan scaled: picture", p.picture, 305, 172, 990, 556);

    /* a 4:3 zoom of a 1920x1080 source into a quarter-screen window at 480,270 (960x540):
     * crop 240,135 1440x810, so the source is 1280x720 and starts 160,90 above-left */
    c = window(480, 270, 960, 540);
    c.width = 1920;
    c.height = 1080;
    c.crop_x = 240;
    c.crop_y = 135;
    c.crop_w = 1440;
    c.crop_h = 810;
    p = video_place(&c, 1920, 1080, full);
    box("zoom: clip", p.clip, 480, 270, 960, 540);
    /* GL y: the source's top is at guest 270 - 90 = 180, so GL 900; its bottom at guest 900, GL
     * 180 */
    box("zoom: picture", p.picture, 320, 180, 1280, 720);
    yes(p.left == 480 && p.top == 810, "zoom: window corner");

    /* a crop reaching outside the source is no crop */
    c.crop_x = 1000;
    c.crop_w = 1000;
    yes(!video_crop(&c), "crop outside the source");
    box("outside: picture", video_place(&c, 1920, 1080, full).picture, 480, 270, 960, 540);
    /* nor is one without a known source */
    c.crop_x = 240;
    c.crop_w = 1440;
    c.width = 0;
    yes(!video_crop(&c), "crop of an unknown source");

    /* a window partly off the output: the clip stops at the output, the picture does not */
    c = window(-100, 980, 400, 200);
    c.width = 400;
    c.height = 200;
    c.crop_x = 100;
    c.crop_y = 50;
    c.crop_w = 200;
    c.crop_h = 100;
    p = video_place(&c, 1920, 1080, full);
    box("off the output: clip", p.clip, 0, 0, 300, 100);
    /* source 800x400 starting 200,100 above-left of the window: guest -300,880 to 500,1280 */
    box("off the output: picture", p.picture, -300, -200, 800, 400);

    /* a window entirely off the output: nothing to draw */
    c = window(2000, 0, 100, 100);
    p = video_place(&c, 1920, 1080, full);
    yes(!p.clip.w || !p.clip.h, "window off the output");

    if (!failures)
        printf("ok\n");
    return failures != 0;
}
