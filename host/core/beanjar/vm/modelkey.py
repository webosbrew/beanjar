# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Validate user-supplied model keys; no derivation or provisioning tools."""

import base64
import os
import stat


def validate(data):
    value = data[:-1] if data.endswith(b"\n") else data
    if len(value) != 44:
        raise ValueError(
            "model key must contain 44 base64 characters, with an optional final newline"
        )
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError:
        raise ValueError("model key must be canonical base64 for 32 bytes") from None
    if len(decoded) != 32 or base64.b64encode(decoded) != value:
        raise ValueError("model key must be canonical base64 for 32 bytes")
    return value


def check(path):
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("model key must be a regular file with owner-only permissions")
        return validate(stream.read(128))
