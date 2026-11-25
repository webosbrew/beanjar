# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The histogram step of scripts/regress --tv (tests/regress/lib/tvsuite.py): libvtcapture's histogram
calls (vtCapture_initHistogram and the rest, 1.0.0-56 on) run in webOS by
tests/regress/histogram/hist-probe.so inside the firmware's own vtCaptureTestSuite (the executable
its luna role is bound to), on the test app's histogram picture (tvapp/pattern.js, PATTERN.histogram:
four flat quadrants), and judged here against what a real set measured
(docs/research/video/capture-histogram.md, "Region map and bin scale", an OLED C2 on ponytail):

  layout   every preset's block count (16, 16, 16, 14), the blocks' indices in order, and each
           block's sample total (the same in R, G and B) the C2's own (TOTALS)
  picture  every block's R, G and B bins exactly the picture's, counted over the C2's region map
           (REGIONS) on its 160 x 90 sample grid (one point per 12 x 12 UI pixels): the quadrants'
           edges lie on the grid, so no point mixes two colours; the VM counts the plain sRGB codes
           (no gamut conversion, as lgemu's capture-histogram.c says), and in the run that checked
           it on o22-ponytail flat pictures were bin for bin identical to the screenshot

The probe reads the presets in PRESET_ORDER, each once its layout has arrived (block 0's total the
preset's), and ends on preset 0, the driver's state at boot.
"""

import re

COLS, ROWS, PITCH = 160, 90, 12  # the sample grid, in UI pixels of a 1920 x 1080 page
BLOCK_COUNT = (16, 16, 16, 14)
PRESET_ORDER = (1, 2, 3, 0)  # what the probe reads; it ends on 0
# The C2's per-preset block totals (sample points per block, the same in R, G and B)
TOTALS = (
    (7200, 1728, 2304, 864, 7200, 1380, 1504, 1584, 7200, 1440, 1504, 864, 864, 1584, 704, 720),
    (5360, 1248, 1728, 624, 5360, 1012, 1120, 1200, 5360, 1056, 1120, 624, 624, 1200, 704, 720),
    (5400, 1278, 1692, 630, 5400, 1050, 1128, 1188, 5220, 1050, 1081, 630, 648, 1155, 704, 720),
    (5760, 5760, 5760, 1782, 2064, 1925, 1722, 1584, 864, 864, 864, 1584, 1681, 1890),
)
# The C2's blocks: x0, x1, y0, y1 in UI pixels (end exclusive), per preset, as the doc's table
REGIONS = (
    (
        (468, 1428, 0, 1080),
        (384, 1536, 864, 1080),
        (576, 1344, 324, 756),
        (192, 768, 0, 216),
        (0, 960, 0, 1080),
        (0, 552, 720, 1080),
        (0, 384, 252, 816),
        (0, 576, 0, 396),
        (960, 1920, 0, 1080),
        (1344, 1920, 720, 1080),
        (1536, 1920, 252, 816),
        (660, 1236, 0, 216),
        (1152, 1728, 0, 216),
        (1344, 1920, 0, 396),
        (576, 1344, 0, 132),
        (0, 240, 324, 756),
    ),
    (
        (468, 1428, 132, 936),
        (384, 1536, 780, 936),
        (576, 1344, 372, 696),
        (192, 768, 132, 288),
        (0, 960, 132, 936),
        (0, 552, 672, 936),
        (0, 384, 324, 744),
        (0, 576, 132, 432),
        (960, 1920, 132, 936),
        (1344, 1920, 672, 936),
        (1536, 1920, 324, 744),
        (660, 1236, 132, 288),
        (1152, 1728, 132, 288),
        (1344, 1920, 132, 432),
        (576, 1344, 0, 132),
        (0, 240, 324, 756),
    ),
    (
        (588, 1308, 0, 1080),
        (528, 1380, 864, 1080),
        (672, 1236, 324, 756),
        (384, 804, 0, 216),
        (240, 960, 0, 1080),
        (240, 660, 720, 1080),
        (240, 528, 252, 816),
        (240, 672, 0, 396),
        (960, 1656, 0, 1080),
        (1236, 1656, 720, 1080),
        (1380, 1656, 252, 816),
        (744, 1164, 0, 216),
        (1092, 1524, 0, 216),
        (1236, 1656, 0, 396),
        (576, 1344, 0, 132),
        (0, 240, 324, 756),
    ),
    (
        (960, 1920, 108, 972),
        (576, 1344, 0, 1080),
        (0, 960, 108, 972),
        (624, 1272, 324, 720),
        (432, 1464, 792, 1080),
        (1260, 1920, 660, 1080),
        (1416, 1920, 288, 780),
        (1344, 1920, 0, 396),
        (1152, 1728, 0, 216),
        (660, 1236, 0, 216),
        (192, 768, 0, 216),
        (0, 576, 0, 396),
        (0, 492, 288, 780),
        (0, 648, 660, 1080),
    ),
)
CHANNELS = "RGB"


def cells(rect):
    """(x0, x1, y0, y1) of a block in grid cells."""
    x0, x1, y0, y1 = rect
    return x0 // PITCH, x1 // PITCH, y0 // PITCH, y1 // PITCH


def picture_cells(pat):
    """{(col, row): (r, g, b)} of PATTERN.histogram (rects as fractions of the window) on the grid:
    the background, then each patch over it. A patch edge off the grid is an error: its points
    would mix two colours."""
    out = {(c, r): tuple(pat["background"]) for c in range(COLS) for r in range(ROWS)}
    for p in pat["patches"]:
        x, y, w, h = p["rect"]
        edges = (x * COLS, (x + w) * COLS, y * ROWS, (y + h) * ROWS)
        if any(abs(e - round(e)) > 1e-9 for e in edges):
            raise ValueError(
                f"patch {p['name']}: edges {edges} are not on the {COLS} x {ROWS} grid"
            )
        c0, c1, r0, r1 = (round(e) for e in edges)
        for c in range(c0, c1):
            for r in range(r0, r1):
                out[(c, r)] = tuple(p["rgb"])
    return out


def expected(pat, preset):
    """[(index, total, [{bin: count} for R, G, B])] of a preset: the picture counted over the region
    map."""
    pic = picture_cells(pat)
    out = []
    for i, rect in enumerate(REGIONS[preset]):
        c0, c1, r0, r1 = cells(rect)
        bins = [{}, {}, {}]
        for c in range(c0, c1):
            for r in range(r0, r1):
                for k in range(3):
                    v = pic[(c, r)][k]
                    bins[k][v] = bins[k].get(v, 0) + 1
        out.append((i, (c1 - c0) * (r1 - r0), bins))
    return out


def parse_bins(text):
    """{bin: count} of a probe channel ("0x7200,255x3" -> {0: 7200, 255: 3})."""
    out = {}
    for item in filter(None, text.split(",")):
        b, _, n = item.partition("x")
        out[int(b)] = int(n)
    return out


def parse(text):
    """The probe's facts: {"init", "finalize", preset: {"rc", "wait_ms", "arrived", "blocks":
    [(index, {R,G,B bins})]}}. Its lines (tests/regress/histogram/hist-probe.c):
      hist.init=ok|null
      hist.p<P>.set=RC  hist.p<P>.get=RC  hist.p<P>.wait_ms=N  hist.p<P>.arrived=yes|no
      hist.p<P>.b<I>=INDEX R=BIN xCOUNT,... G=... B=...
      hist.finalize=RC"""
    out = {"presets": {}}
    for line in text.splitlines():
        m = re.match(r"^hist\.(init|finalize)=(\S*)", line)
        if m:
            out[m.group(1)] = m.group(2)
            continue
        m = re.match(r"^hist\.p(\d)\.(set|get|wait_ms|arrived)=(\S*)", line)
        if m:
            out["presets"].setdefault(int(m.group(1)), {"blocks": []})[m.group(2)] = m.group(3)
            continue
        m = re.match(r"^hist\.p(\d)\.b(\d+)=(\d+) R=(\S*) G=(\S*) B=(\S*)$", line)
        if m:
            p = out["presets"].setdefault(int(m.group(1)), {"blocks": []})
            p["blocks"].append((int(m.group(3)), [parse_bins(m.group(k)) for k in (4, 5, 6)]))
    return out


def _bins_text(b):
    return ",".join(f"{k}x{b[k]}" for k in sorted(b)) or "none"


def judge(text, pat):
    """[(check, PASS/FAIL, why)] of the probe's output: histogram.calls (init, every preset's set
    and get, finalize), histogram.layout (TOTALS, BLOCK_COUNT, indices) and histogram.picture
    (every block's bins the picture's)."""
    f = parse(text)
    if f.get("init") != "ok":
        why = f"vtCapture_initHistogram gave {f.get('init', 'nothing')}"
        tail = "; ".join(ln for ln in text.splitlines()[-4:] if ln.strip())[-200:]
        return [("histogram.calls", "FAIL", f"{why}: {tail}" if tail else why)]
    bad_calls, bad_layout, bad_pic = [], [], []
    waits = []
    for p in PRESET_ORDER:
        r = f["presets"].get(p)
        if not r:
            bad_calls.append(f"preset {p}: not read")
            continue
        if r.get("set") != "0" or r.get("get") != "0":
            bad_calls.append(f"preset {p}: set {r.get('set')}, get {r.get('get')}")
        if r.get("arrived") != "yes":
            bad_calls.append(f"preset {p}: its layout did not arrive in {r.get('wait_ms')} ms")
        else:
            waits.append(int(r.get("wait_ms", 0)))
        want = expected(pat, p)
        got = r["blocks"]
        if len(got) != BLOCK_COUNT[p]:
            bad_layout.append(f"preset {p}: {len(got)} blocks, not {BLOCK_COUNT[p]}")
        for i, (index, bins) in enumerate(got[: len(want)]):
            totals = [sum(b.values()) for b in bins]
            if index != i:
                bad_layout.append(f"preset {p} block {i}: index {index}")
            if totals != [TOTALS[p][i]] * 3:
                bad_layout.append(f"preset {p} block {i}: totals {totals}, not {TOTALS[p][i]}")
            for k in range(3):
                if bins[k] != want[i][2][k]:
                    bad_pic.append(
                        f"preset {p} block {i} {CHANNELS[k]}: {_bins_text(bins[k])}, "
                        f"not {_bins_text(want[i][2][k])}"
                    )
    if f.get("finalize") != "0":
        bad_calls.append(f"finalize gave {f.get('finalize')}")
    n_blocks = sum(BLOCK_COUNT[p] for p in PRESET_ORDER)
    out = [
        (
            "histogram.calls",
            "FAIL" if bad_calls else "PASS",
            "; ".join(bad_calls)
            if bad_calls
            else f"init, 4 presets set and read, finalize; layout arrived in "
            f"{min(waits)}..{max(waits)} ms",
        ),
        (
            "histogram.layout",
            "FAIL" if bad_layout else "PASS",
            "; ".join(bad_layout[:6])
            + (f" (+{len(bad_layout) - 6} more)" if len(bad_layout) > 6 else "")
            if bad_layout
            else f"{n_blocks} blocks over 4 presets: counts, indices and totals the C2's",
        ),
        (
            "histogram.picture",
            "FAIL" if bad_pic else "PASS",
            f"{len(bad_pic)} block channels differ: " + "; ".join(bad_pic[:4])
            if bad_pic
            else f"{n_blocks} blocks x R, G, B: every bin the picture's on the C2's region map",
        ),
    ]
    return out
