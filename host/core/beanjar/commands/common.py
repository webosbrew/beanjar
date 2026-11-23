# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What every command shares: the parser's class and its usage error, and the output helpers.

Conventions (host/core/README.md, "Using the CLI from scripts and agents"): a command's result
goes to stdout, as text or with --json as JSON; progress and warnings go to stderr. Exit status 0
on success, 1 when the operation fails (Error), 2 for a usage error; with --json an error is one
JSON object on stderr, {"error": message, "key": setting or absent, "usage": true or absent}."""

import json
import sys

from .. import core
from ..cli import json_error
from ..core import Error


class Usage(Error):
    """A command given the wrong arguments: exit status 2, as argparse's own usage errors."""


class Parser:
    """The command line parser's class (parser_class): an argparse.ArgumentParser whose usage
    errors are JSON when json_errors (main sets it when --json is among the arguments)."""

    json_errors = False


def parser_class():
    import argparse

    class ArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            if Parser.json_errors:
                self.exit(2, json.dumps({"error": message, "usage": True}) + "\n")
            super().error(message)

    return ArgumentParser


def progress(line):
    print(line, file=sys.stderr, flush=True)


def text_value(v, none="-"):
    if v is None:
        return none
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ",".join(str(i) for i in v)
    return str(v)


def kv_lines(data, none="-", prefix=""):
    """data as `KEY: value` lines, a nested table's keys joined to its own with a dot."""
    lines = []
    for k, v in data.items():
        if isinstance(v, dict) and v:
            lines += kv_lines(v, none, f"{prefix}{k}.")
        else:
            lines.append(f"{prefix}{k}: {text_value(None if isinstance(v, dict) else v, none)}")
    return lines


def emit(a, data, text=None, none="-"):
    """A command's result on stdout: data as JSON with --json, else text (by default data's
    `KEY: value` lines)."""
    if a.json:
        print(json.dumps(data, indent=2))
    else:
        text = "\n".join(kv_lines(data, none)) if text is None else text
        if text:
            print(text)
    return 0


def table(rows, columns):
    """rows as aligned columns, [(HEADER, the row's text, right-aligned)], two spaces apart."""
    cells = [[h for h, _f, _r in columns]] + [[f(r) for _h, f, _r in columns] for r in rows]
    widths = [max(len(c[i]) for c in cells) for i in range(len(columns))]
    return "\n".join(
        "  ".join(
            c[i].rjust(widths[i]) if columns[i][2] else c[i].ljust(widths[i])
            for i in range(len(columns))
        ).rstrip()
        for c in cells
    )


def version_info():
    """{"name", "version"} of this beanjar: the installed package's version, or the checkout's
    packaging/VERSION marked "(checkout)"."""
    from .. import compat, paths

    version = compat.host_version()
    if not paths.TREE.installed and version != "unknown":
        version += " (checkout)"
    return {"name": "beanjar", "version": version}


def print_version(as_json):
    """--version: "beanjar VERSION" on stdout, or with --json the {"name", "version"} object."""
    info = version_info()
    print(json.dumps(info) if as_json else f"{info['name']} {info['version']}")


def late_options(a, args, prog, add=None):
    """The manager's own options among a front end's words after NAME (add's; the global options
    are taken out of the whole line before: parser.split_globals)."""
    import argparse

    after = argparse.ArgumentParser(prog=prog, add_help=False)
    if add:
        add(after)
    return after.parse_known_args(args)


def warn(text):
    progress(f"beanjar: warning: {text}")


def layer_versions(layers):
    """{layer: version} of the versioned layers among layers (layermeta.versions_of); {} when
    the layers' metadata cannot be read."""
    from .. import layermeta

    try:
        return layermeta.versions_of(layers or [])
    except layermeta.Error:
        return {}


def read_new_password(source):
    """A password to set, from source: a one-line file, "-" for stdin; with no source a prompt
    (twice) when stdin is a terminal, else stdin's one line. Never from argv."""
    if source is None and sys.stdin.isatty():
        import getpass

        password = getpass.getpass("New VNC password: ")
        if getpass.getpass("Again: ") != password:
            raise Error("the two passwords differ")
        return password
    try:
        if source in (None, "-"):
            text = sys.stdin.read()
        else:
            with open(source) as f:
                text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise Error(f"cannot read the VNC password: {e}") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the last line's end
    if len(lines) > 1:
        raise Error("the VNC password must be one line")
    return lines[0].removesuffix("\r") if lines else ""


def parse_size(text):
    try:
        w, h = (int(x) for x in text.split("x"))
    except ValueError:
        raise Usage(f"invalid frame {text!r}: WIDTHxHEIGHT, e.g. 1280x720") from None
    return w, h


DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0}
DURATION_HELP = "a number and its unit: ms, s or m (200ms, 1.5s, 2m)"


def duration(text):
    """A duration option's value in seconds: a number (whole or with a fraction) and its unit, ms,
    s or m (200ms, 1.5s, 2m). A number alone is refused: its unit would be a guess."""
    import argparse
    import re

    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)", text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r}: {DURATION_HELP}")
    return float(m.group(1)) * DURATION_UNITS[m.group(2)]


def fail(e, as_json, code):
    if as_json:
        return json_error(e, code)
    core.main_error(e)
    return code
