# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Why LG's camera app did not feed its MJPEG decoder, named from the log lines of one launch
(tests/regress/virtcam/run-camera.py --app; the lines are camera-app.sh's cam= lines).

The classes, and the lines each rests on (docs/research/virtual-camera.md, "Launched 1 to 3 minutes
after boot", and docs/research/camera-decoder-and-viewer-av.md):

  fed                    the camera player's `PrintDecodedBuffer` (or the stand-in decoder's "host
                         decoding" line) is there: the decoder got buffers. No failure to name.
  libcbe-race            LG's own race in libcbe's camera player: `Load, ret:true`, then
                         `Play, need loaded state_:`, then `onLoadComplete, next_state:kInit`. A working
                         launch logs `onLoadComplete` before `Load, ret:true`. The same unlocked code
                         runs on a real TV, so this is a stock result, not a pass.
  getproperties-timeout  the app got as far as `getUserMedia` and camera2 logged `getProperties`, but
                         the player was never loaded (no `Load, ret`): camera2's getProperties passed
                         Chromium's 2 s luna timeout, so the app never opened the preview.
  app-slow               no `getUserMedia` line within the window: the app was too slow after boot.
  unclassified           anything else; the evidence is the lines that were seen.

Only the libcbe lines are recorded verbatim; the other two classes are told by the method names the
research gives (`getUserMedia`, `getProperties`) and the absence of `Load, ret`."""

LOAD_RET = "Load, ret:true"
NEED_LOADED = "Play, need loaded state_:"
LOAD_COMPLETE = "onLoadComplete"
DECODED = "PrintDecodedBuffer"
HOST_DECODING = "host decoding"
USER_MEDIA = "getUserMedia"
GET_PROPERTIES = "getProperties"

# the lines camera-app.sh keeps (its grep), for the log of one launch
LOG_PATTERN = (
    "Load, ret|need loaded state_|onLoadComplete|PrintDecodedBuffer|getUserMedia|getProperties"
)

FED, RACE, GETPROPS, SLOW, OTHER = (
    "fed",
    "libcbe-race",
    "getproperties-timeout",
    "app-slow",
    "unclassified",
)
CLASSES = (FED, RACE, GETPROPS, SLOW, OTHER)
# the result of each class: the test's exit status 0 (pass), 3 (LG's stock race), 1 (fail)
STATUS = {FED: 0, RACE: 3, GETPROPS: 1, SLOW: 1, OTHER: 1}
MAX_EVIDENCE = 8


def first(lines, text, start=0):
    return next((i for i in range(start, len(lines)) if text in lines[i]), None)


def classify(lines):
    """(class, evidence lines) for the log lines of one launch, in the order they were written."""
    lines = [x.strip() for x in lines if x.strip()]
    if any(DECODED in x or HOST_DECODING in x for x in lines):
        return FED, [x for x in lines if DECODED in x or HOST_DECODING in x][:2]
    load = first(lines, LOAD_RET)
    need = first(lines, NEED_LOADED, load) if load is not None else None
    if need is not None:
        done = first(lines, LOAD_COMPLETE, need)
        if done is not None:
            return RACE, [lines[load], lines[need], lines[done]]
    seen = [x for x in lines if any(p in x for p in (LOAD_RET, NEED_LOADED, LOAD_COMPLETE))]
    if load is None:
        if first(lines, USER_MEDIA) is None:
            return SLOW, lines[:MAX_EVIDENCE]
        if first(lines, GET_PROPERTIES) is not None:
            return GETPROPS, [x for x in lines if GET_PROPERTIES in x or USER_MEDIA in x][
                :MAX_EVIDENCE
            ]
    return OTHER, (seen or lines)[:MAX_EVIDENCE]
