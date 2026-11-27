# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The TOML and JSON files beanjar writes itself: their headers, and a writer that keeps a
file's key order and comments out (write_toml)."""

import json
import os
import re

from .base import (
    Error,
)

VM_HEADER = "A VM of Beanjar (host/core/README.md). Change it while the VM is off."


CONFIG_HEADER = "Beanjar's library settings (host/core/README.md)."


MANIFEST_HEADER = "A firmware of Beanjar (its New VM): where its paks came from."


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        raise Error(f"{path}: {e}")


_TOML_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def toml_value(v):
    """A TOML value for a str, int, bool or a list of them (the library's files are flat)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return (
            '"'
            + "".join(
                _TOML_ESCAPES.get(c)
                or (f"\\u{ord(c):04x}" if ord(c) < 0x20 or ord(c) == 0x7F else c)
                for c in v
            )
            + '"'
        )
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(i) for i in v) + "]"
    raise TypeError(f"no TOML for {type(v).__name__} {v!r}")


def toml_key(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else toml_value(k)


def write_toml(path, data, header=None, order=(), tail=""):
    """data as TOML: header as a comment, the keys of order first, then the rest by name, then tail
    (tables, as text); written to a temporary file and renamed into place."""
    keys = [k for k in order if k in data] + sorted(k for k in data if k not in order)
    text = "".join(f"# {line}\n" for line in (header.splitlines() if header else ()))
    text += "".join(f"{toml_key(k)} = {toml_value(data[k])}\n" for k in keys)
    text += tail
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
