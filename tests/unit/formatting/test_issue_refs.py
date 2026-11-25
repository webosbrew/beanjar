#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Published files carry no bare issue numbers.

A reference such as "#123" or a link to an issue of this project means nothing to a reader of
the published tree. Comments and documents say what and why in words instead.
"""

from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
PATHS = ROOT / ".github" / "publish" / "paths.txt"

# "#123" not part of a word, an entity, an anchor, a path or a longer run of "#"s
BARE = re.compile(r"(?<![\w&/#])#\d{1,4}\b")
LINK = re.compile(r"github\.com/[\w.-]+/beanjar/(?:issues|pull)/\d+")

# Files whose "#<digits>" are colours
SKIPPED_SUFFIXES = (".css", ".scss", ".html", ".js", ".svg")

# What "#<digits>" means besides an issue: ARM immediates and register lists, an RFC name, a
# kernel banner. Each is matched against the text before the number.
NOT_AN_ISSUE = (
    re.compile(r"\[[^\]]*$"),
    re.compile(r"\b(?:mov\w*|cmp|svc|add|sub|lsl|lsr|asr|ror|r\d+|sp|lr|PKCS)\b[^()]*$"),
)
NOT_AN_ISSUE_LINES = ("SMP PREEMPT",)


def public_files():
    # In the public repository the publish list is absent: every tracked file there is public.
    if not PATHS.exists():
        return tracked([])
    specs = []
    for raw in PATHS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(":"):
            specs.append(line)
        elif line.startswith("!"):
            specs.append(":(glob,exclude)" + line[1:])
        else:
            specs.append(":(glob)" + line)
    return tracked(specs)


def tracked(specs):
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", *specs],
        capture_output=True,
        check=True,
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def path_is_self(name):
    # its own examples and explanations spell out what it looks for
    return (ROOT / name).resolve() == Path(__file__).resolve()


def references(text):
    """Yield (line number, line) of every issue reference in text."""
    for number, line in enumerate(text.splitlines(), 1):
        found = bool(LINK.search(line))
        if not any(skip in line for skip in NOT_AN_ISSUE_LINES):
            for match in BARE.finditer(line):
                before = line[: match.start()]
                if not any(rule.search(before) for rule in NOT_AN_ISSUE):
                    found = True
        if found:
            yield number, line


def test_references_are_found():
    assert list(references("fixed in #12 (see\nissue #3)\n")) == [
        (1, "fixed in #12 (see"),
        (2, "issue #3)"),
    ]
    assert list(references("https://github.com/x/beanjar/issues/9")) != []
    assert list(references("mov r0, #8\nldr r1, [r0, #4]\nPKCS #1\n")) == []
    assert list(references("a #include <x.h>\nsee file.md#12\n")) == []


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_published_files_have_no_issue_numbers():
    hits = []
    for name in public_files():
        if name.endswith(SKIPPED_SUFFIXES) or path_is_self(name):
            continue
        path = ROOT / name
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{name}:{number}: {line.strip()[:100]}" for number, line in references(text)]
    assert not hits, "issue references in published files:\n" + "\n".join(hits)
