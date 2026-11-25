// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef VIEWER_VIDEO_PLACE_H
#define VIEWER_VIDEO_PLACE_H
#include "../../../include/webos-video.h"
#include <math.h>
/* Where a scaler window's picture goes in the output (video.c, the one place the viewer, its
 * screenshots and beanjar-vnc place video). The window is in the guest screen's pixels (screen_w x
 * screen_h); the output is the viewport the screen fills (GL pixels, origin at the bottom left).
 *
 * The scaler shows its input region (crop_*) of the source (width x height) in the window, as a
 * real scaler does with overscan and the picture-size modes: the whole decoded picture is drawn
 * into a larger rectangle (picture), so that the crop lands exactly on the window, and the
 * scissor (clip: the window inside the output) cuts the rest. Without a crop the picture is the
 * window. */
struct video_box {
    int x, y, w, h; /* GL pixels, y at the bottom */
};
struct video_place {
    struct video_box clip;    /* the window inside the output; w or h 0: nothing to draw */
    struct video_box picture; /* the whole source; may reach outside the output */
    int left, top;            /* the window's own top-left corner (a pattern's origin) */
};

/* The crop of c inside its source, or 0 when the whole source is shown */
static inline int video_crop(const struct webos_video_context *c)
{
    return c->crop_w && c->crop_h && c->width && c->height &&
           (uint64_t)c->crop_x + c->crop_w <= c->width &&
           (uint64_t)c->crop_y + c->crop_h <= c->height &&
           (c->crop_x || c->crop_y || c->crop_w != c->width || c->crop_h != c->height);
}

static inline int video_place_round(double v)
{
    return (int)floor(v + 0.5);
}

static inline struct video_place video_place(const struct webos_video_context *c, unsigned screen_w,
                                             unsigned screen_h, struct video_box out)
{
    struct video_place p = {{0, 0, 0, 0}, {0, 0, 0, 0}, 0, 0};
    double sx = (double)out.w / screen_w, sy = (double)out.h / screen_h;
    /* the window, then the source rectangle around it, in guest screen pixels */
    double wx = c->x, wy = c->y, ww = c->w, wh = c->h;
    double px = wx, py = wy, pw = ww, ph = wh;
    if (video_crop(c)) {
        double kx = ww / c->crop_w, ky = wh / c->crop_h;
        px = wx - c->crop_x * kx;
        py = wy - c->crop_y * ky;
        pw = c->width * kx;
        ph = c->height * ky;
    }
    /* guest y grows downwards; GL's upwards from the bottom of the output */
    int left = out.x + video_place_round(wx * sx),
        right = out.x + video_place_round((wx + ww) * sx);
    int top = out.y + out.h - video_place_round(wy * sy);
    int bottom = out.y + out.h - video_place_round((wy + wh) * sy);
    p.left = left;
    p.top = top;
    int pl = out.x + video_place_round(px * sx), pr = out.x + video_place_round((px + pw) * sx);
    int pt = out.y + out.h - video_place_round(py * sy);
    int pb = out.y + out.h - video_place_round((py + ph) * sy);
    p.picture = (struct video_box){pl, pb, pr - pl, pt - pb};
    int cl = left < out.x ? out.x : left, cr = right > out.x + out.w ? out.x + out.w : right;
    int cb = bottom < out.y ? out.y : bottom, ct = top > out.y + out.h ? out.y + out.h : top;
    if (cr > cl && ct > cb)
        p.clip = (struct video_box){cl, cb, cr - cl, ct - cb};
    return p;
}
#endif
