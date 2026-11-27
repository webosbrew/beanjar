#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Explicit stock-QEMU launcher for ARM64 LM14 bring-up, with per-VM halt polling off."""

import os
import platform
import shutil
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: haltpoll.py QEMU [QEMU arguments...]")
    if platform.system() != "Linux" or platform.machine() != "aarch64":
        raise SystemExit("KVM halt-poll helper currently requires Linux aarch64")
    qemu = shutil.which(sys.argv[1])
    if not qemu or not Path(qemu).name.startswith("qemu-system-"):
        raise SystemExit("KVM halt-poll helper requires a QEMU system executable")
    helper = Path(__file__).resolve().parents[3] / "build/kvm/libkvm-haltpoll.so"
    if not helper.is_file():
        raise SystemExit("KVM halt-poll helper missing; build the native host target first")
    env = os.environ.copy()
    prior = env.get("LD_PRELOAD", "")
    env["LD_PRELOAD"] = str(helper) + (" " + prior if prior else "")
    os.execve(qemu, [qemu, *sys.argv[2:]], env)


if __name__ == "__main__":
    main()
