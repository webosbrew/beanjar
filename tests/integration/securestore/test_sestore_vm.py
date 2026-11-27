# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The secure store with the genuine arm64 kernel and ARM32 clients, in a firmware-free QEMU guest
(check-sestore-vm.py): the guest build's kernel, module, boot helper and busybox, the target SDK and
qemu-system-aarch64; skipped (77) with what is missing."""

import sys
from pathlib import Path


def test_sestore_vm(run_check):
    run_check([sys.executable, Path(__file__).with_name("check-sestore-vm.py")], 120)
