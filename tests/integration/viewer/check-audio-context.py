#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""PC-only fake QEMU audio, no VM and no display. Own and reap every child."""

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

top = Path(__file__).resolve().parents[3]
with tempfile.TemporaryDirectory(prefix="check-audio-context-") as tmp:
    with open(Path(tmp) / "fake.log", "w+") as log:
        fake = subprocess.Popen(
            ["/usr/bin/python3", str(top / "tests/fakes/fake-qemu.py"), tmp, "--audio"],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            for _ in range(100):
                if (Path(tmp) / "ready").exists():
                    break
                if fake.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read())
                time.sleep(0.05)
            result = subprocess.run(
                [sys.argv[1], f"unix:path={tmp}/dbus.sock"],
                env=dict(os.environ, SDL_AUDIODRIVER="dummy", G_DEBUG="fatal-warnings"),
                timeout=15,
            )
            if result.returncode:
                raise SystemExit(result.returncode)
            (Path(tmp) / "audio-register-hold").touch()
            result = subprocess.run(
                [sys.argv[1], f"unix:path={tmp}/dbus.sock", str(Path(tmp) / "audio-register-held")],
                env=dict(os.environ, G_DEBUG="fatal-warnings"),
                timeout=10,
            )
            if result.returncode:
                raise SystemExit(result.returncode)
        finally:
            if fake.poll() is None:
                fake.terminate()
                try:
                    fake.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(fake.pid, signal.SIGKILL)
                    fake.wait()
            try:
                os.killpg(fake.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
