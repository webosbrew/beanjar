#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Select Python sources, including non-executable scripts, from pre-commit's file batch, and
format them: `ruff check --fix` for the import rules (pyproject.toml's [tool.ruff.lint]: sorted
imports, no unused ones), then `ruff format`. With --check nothing is changed and both must pass."""

import subprocess
import sys
from pathlib import Path


def main():
    args = sys.argv[1:]
    check = bool(args and args[0] == "--check")
    if check:
        args.pop(0)
    sources = []
    for name in args:
        path = Path(name)
        if path.suffix == ".py":
            sources.append(name)
        elif not path.suffix:
            with path.open("rb") as stream:
                first = stream.readline()
            if first.startswith(b"#!") and b"python" in first:
                sources.append(name)
    if not sources:
        return 0
    # the import rules first: removing an import can leave a different layout for the formatter
    lint = ["ruff", "check"] if check else ["ruff", "check", "--fix"]
    fmt = ["ruff", "format", "--check"] if check else ["ruff", "format"]
    codes = [subprocess.run([*command, "--", *sources]).returncode for command in (lint, fmt)]
    return max(codes)


if __name__ == "__main__":
    raise SystemExit(main())
