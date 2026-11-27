# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Read LG's dpmeta linear block map, without interpreting a filesystem.

The measured K8LPN2 container has five read-only linear targets, each followed
by a webos-verity target. We copy only the linear ranges; we do not implement
or claim verification of the vendor integrity wrapper. SquashFS readers remain
the official unsquashfs program.
"""

import os
import re
from typing import NamedTuple


class Image(NamedTuple):
    path: str
    offset: int
    size: int


NAMES = {"rootfs", "fonts", "otncabi", "otycabi", "bsppart"}


def images(directory):
    """Logical pak names to bounded raw ranges; empty without a container.

    Refuse formats other than the measured single-device, paired linear/verity
    map. Nothing from the metadata is executed or used as a filesystem path.
    """
    container = os.path.join(directory, "super.pak")
    if not os.path.isfile(container):
        return {}
    try:
        with open(os.path.join(directory, "dpmeta.pak"), "rb") as f:
            data = f.read(65537)
    except OSError as e:
        raise ValueError("super.pak requires readable dpmeta.pak") from e
    if len(data) > 65536:
        raise ValueError("dpmeta.pak exceeds supported metadata size")
    try:
        text = data.split(b"\0", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("dpmeta.pak has no supported textual map") from None
    # Allow whitespace, but no additional commands, shell expansion or targets.
    match = re.fullmatch(
        r'\s*dm-mod.waitfor="(/dev/mmcblk0p[0-9]+)"\s+'
        r'dm-mod.create="([^"]+)"\s+root=/dev/dm-1\s+ro\s+'
        r"rootfstype=squashfs\s*",
        text,
    )
    if not match:
        raise ValueError("unsupported dpmeta boot configuration")
    device, table = match.groups()
    targets = table.split(";")
    if len(targets) % 2:
        raise ValueError("unpaired dpmeta targets")
    result = {}
    limit = os.path.getsize(container)
    for index in range(0, len(targets), 2):
        linear = re.fullmatch(
            r"([a-z]+)-linear,,,ro,\s*0 ([0-9]+) linear "
            r"(/dev/mmcblk0p[0-9]+) ([0-9]+)",
            targets[index],
        )
        if not linear:
            raise ValueError("unsupported dpmeta linear target")
        name, count, backing, start = linear.groups()
        if name not in NAMES or name in result or backing != device:
            raise ValueError("unsupported, duplicate or mismatched dpmeta image")
        if targets[index + 1] != f"{name},,,ro, 0 1 webos-verity /dev/dm-{index}":
            raise ValueError("unsupported dpmeta integrity target")
        image = Image(container, int(start) * 512, int(count) * 512)
        if image.size <= 0 or image.offset + image.size > limit:
            raise ValueError("dpmeta image exceeds super.pak")
        if any(
            image.offset < other.offset + other.size and other.offset < image.offset + image.size
            for other in result.values()
        ):
            raise ValueError("overlapping dpmeta images")
        if os.path.exists(os.path.join(directory, name + ".pak")):
            raise ValueError("ambiguous separate and super.pak image")
        result[name] = image
    if set(result) != NAMES or next(iter(result)) != "rootfs":
        raise ValueError("unsupported dpmeta image set or root target")
    return result


def copy(image, destination):
    """Copy exactly the mapped bytes; remove an incomplete destination on error."""
    with open(image.path, "rb") as src, open(destination, "xb") as dest:
        try:
            src.seek(image.offset)
            remaining = image.size
            while remaining:
                block = src.read(min(remaining, 4 << 20))
                if not block:
                    raise ValueError("truncated super.pak image")
                dest.write(block)
                remaining -= len(block)
        except BaseException:
            os.unlink(destination)
            raise
