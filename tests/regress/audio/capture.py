#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Record the VM's sound card on the VM host: QEMU's own WAV tap (HMP wavcapture, through QMP's
human-monitor-command) on the card's audiodev, whatever backs it (dbus, none), so a run records
what the guest plays with nothing sent to a speaker (docs/dev/guides/build.md, "TV feature suite": audio).

    capture.py start FILE.wav     start the tap (FILE relative to the checkout or absolute)
    capture.py stop               stop every tap (QEMU writes the WAV's header then)

QMP is RUN_DIR/qmp.sock (RUN_DIR default build). The tap is the audiodev's mix at the card's
fixed format, 44100 Hz s16 stereo (beanjar/runtime/qemu.py: the audiodev's fixed settings).
"""

import json
import os
import re
import socket
import sys

TOP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RUN = os.path.join(TOP, os.environ.get("RUN_DIR") or "build")
AUDIODEV = "snd0"


def hmp(*lines):
    """The output of each HMP command line, on one QMP connection."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(10)
        s.connect(os.path.join(RUN, "qmp.sock"))
        f = s.makefile("rw")
        json.loads(f.readline())
        out = []
        for cmd in [{"execute": "qmp_capabilities"}] + [
            {"execute": "human-monitor-command", "arguments": {"command-line": x}} for x in lines
        ]:
            f.write(json.dumps(cmd) + "\n")
            f.flush()
            while True:
                reply = json.loads(f.readline())
                if "return" in reply or "error" in reply:
                    break
            if "error" in reply:
                raise SystemExit(f"capture: {cmd}: {reply['error']}")
            out.append(reply["return"])
        return out[1:]


def taps():
    """The running taps' numbers (HMP info capture: "[N]: ...")."""
    (text,) = hmp("info capture")
    return [int(n) for n in re.findall(r"^\[(\d+)\]", text, re.M)]


def main(argv):
    if argv[:1] == ["start"] and len(argv) == 2:
        path = os.path.abspath(os.path.join(TOP, argv[1]))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        (out,) = hmp(f"wavcapture {path} {AUDIODEV} 44100 16 2")
        if out.strip():
            raise SystemExit(f"capture: wavcapture: {out.strip()}")
        if not taps():
            raise SystemExit("capture: no tap running after wavcapture")
        print(f"capture: recording {path}")
    elif argv == ["stop"]:
        n = taps()
        if n:
            hmp(*[f"stopcapture {i}" for i in sorted(n, reverse=True)])
        print(f"capture: stopped {len(n)}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
