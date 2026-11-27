# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Screenshots: the TV picture's box in a frame, frame and source coordinates, and the
PNG a screenshot is (the guest display's size recorded in it)."""

from ..base import Error

# A headless viewer takes its picture this long after the first scanout, for the video windows and
# LG's cursor to arrive.
SCREENSHOT_SETTLE_MS = 1500


def picture_box(frame, source):
    """(x, y, w, h) of the TV picture in a frame of frame = (width, height) pixels, the guest's
    display of source = (width, height) letterboxed into it, as the viewer core lays out a
    screenshot (host/viewer/core/core.c, fit): the largest box of the source's aspect, centred."""
    fw, fh = frame
    sw, sh = source
    w, h = fw, fh
    if fw * sh > fh * sw:
        w = fh * sw // sh
    else:
        h = fw * sh // sw
    return (fw - w) // 2, (fh - h) // 2, w, h


def frame_geometry(frame, source):
    """How a frame (a screenshot's frame = (width, height)) shows the guest's display of source =
    (width, height), for --json: the source's size, the scale (frame pixels per display pixel) and
    where the picture sits in the frame (the letterbox offset). None values for an unknown source."""
    if not source:
        keys = ("source_width", "source_height", "scale", "offset_x", "offset_y")
        return dict.fromkeys(keys)
    x, y, w, _ = picture_box(frame, source)
    return {
        "source_width": source[0],
        "source_height": source[1],
        "scale": round(w / source[0], 6),
        "offset_x": x,
        "offset_y": y,
    }


def frame_to_source(x, y, frame, source):
    """A point of a frame (frame = (width, height), as a screenshot of that size shows the guest's
    display) to the display pixel (source = (width, height)) under it. x, y may be fractions (an
    element's centre): a whole x, y is that pixel's centre, x + 0.5, y + 0.5 its far corner. A point
    outside the picture (in the letterbox or off the frame) is an Error."""
    bx, by, bw, bh = picture_box(frame, source)
    if not (bx <= x < bx + bw and by <= y < by + bh):
        raise Error(
            f"{x:g},{y:g} is outside the TV picture of the {frame[0]}x{frame[1]} frame "
            f"({bw}x{bh} at {bx},{by})"
        )
    sx = int((x + 0.5 - bx) * source[0] // bw)
    sy = int((y + 0.5 - by) * source[1] // bh)
    return min(max(sx, 0), source[0] - 1), min(max(sy, 0), source[1] - 1)


# A PNG tEXt keyword: "WIDTHxHEIGHT" of the guest's display a beanjar screenshot shows, so a
# screenshot taken on another host (screenshot HOST/NAME, elements HOST/NAME) carries it
PNG_SOURCE_KEY = b"beanjar-source"


def png_source(png):
    """(width, height) of the guest's display a beanjar screenshot's PNG records, or None."""
    import re
    import struct

    pos = 8
    while pos + 8 <= len(png):
        length, kind = struct.unpack(">I4s", png[pos : pos + 8])
        if kind == b"IDAT" or kind == b"IEND":
            break
        if kind == b"tEXt":
            key, _, value = png[pos + 8 : pos + 8 + length].partition(b"\0")
            m = re.fullmatch(rb"(\d+)x(\d+)", value)
            if key == PNG_SOURCE_KEY and m and int(m.group(1)) and int(m.group(2)):
                return int(m.group(1)), int(m.group(2))
        pos += 12 + length
    return None


def ppm_to_png(data, source=None):
    """A binary PPM (P6, 8 bits) as a PNG, RGB."""
    import re
    import struct
    import zlib

    m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255\s", data)
    if not m:
        raise Error("the screenshot is not a P6 PPM")
    w, h = int(m.group(1)), int(m.group(2))
    pixels = data[m.end() :]
    if len(pixels) != w * h * 3:
        raise Error("the screenshot is truncated")
    rows = b"".join(b"\0" + pixels[y * w * 3 : (y + 1) * w * 3] for y in range(h))

    def chunk(kind, body):
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + (chunk(b"tEXt", PNG_SOURCE_KEY + b"\0" + b"%dx%d" % tuple(source)) if source else b"")
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )
