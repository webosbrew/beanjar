// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
// The draw step's pattern (scripts/regress --tv; tests/regress/lib/tvsuite.py reads this same file):
// each mode's background and the solid patches, rect = [x, y, width, height] as fractions of the
// window. Keep it one JSON value: the host parses it. histogram: the histogram step's picture
// ({"mode": "histogram"}, canvas 2D; tests/regress/lib/vthist.py), four flat quadrants of colours a
// real set's histogram was checked with, their edges on the histogram's 160 x 90 sample grid.
var PATTERN = {
 "modes": {"canvas": [64, 64, 64], "webgl": [40, 40, 112]},
 "patches": [
  {"name": "red", "rgb": [255, 0, 0], "rect": [0.08, 0.12, 0.22, 0.30]},
  {"name": "green", "rgb": [0, 255, 0], "rect": [0.39, 0.12, 0.22, 0.30]},
  {"name": "blue", "rgb": [0, 0, 255], "rect": [0.70, 0.12, 0.22, 0.30]},
  {"name": "white", "rgb": [255, 255, 255], "rect": [0.08, 0.58, 0.22, 0.30]},
  {"name": "yellow", "rgb": [255, 255, 0], "rect": [0.39, 0.58, 0.22, 0.30]},
  {"name": "cyan", "rgb": [0, 255, 255], "rect": [0.70, 0.58, 0.22, 0.30]}
 ],
 "nowebgl": [255, 0, 255],
 "histogram": {
  "background": [0, 0, 0],
  "patches": [
   {"name": "white", "rgb": [255, 255, 255], "rect": [0, 0, 0.5, 0.5]},
   {"name": "red", "rgb": [255, 0, 0], "rect": [0.5, 0, 0.5, 0.5]},
   {"name": "blue", "rgb": [0, 0, 255], "rect": [0, 0.5, 0.5, 0.5]},
   {"name": "grey", "rgb": [128, 128, 128], "rect": [0.5, 0.5, 0.5, 0.5]}
  ]
 }
};
