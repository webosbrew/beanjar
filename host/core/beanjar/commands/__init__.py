# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The VM manager's commands (host/core/README.md), which `python3 -m beanjar` and the
launcher's `beanjar` run through cli.main (cli.py answers the `_ares` query itself, without this
package or the library's). main parses (parser.py: each module's commands and options) and runs
the command here or, for HOST/NAME, on that host (forward.py); ares_main runs an ares-TOOL.

  common.py          the parser's class, Usage, and the output helpers (emit, table, ...)
  vms.py             list, create, start, stop, show, status, delete, rename, prepare, wipe
  settings.py        config, library config, host config, add, remove, list, prune-layer-images
  display.py         screenshot, elements, viewer, vnc, screensaver
  remote_control.py  remote: pipe, press, pointer and touch, layout
  identity.py        identity reset and authorized-keys, clone, key
  snapshot.py        snapshot: create, list, show, delete, reset
  sensors.py         sensor show|set: light, presence, temperature, rotate
  devices.py         io: tuner, usb
  mitm.py            mitm
  shell.py           shell
  forward.py         a command for a VM of another host, run there over ssh

What a command needs beyond the library is imported where it is used (the parser and the command
modules themselves, the GPU, element and screensaver modules, the thread pool, ares), so that the light commands start quickly: an
ares-TOOL run (on a board, every module imported costs tens of milliseconds)."""

import sys

from .. import remote
from ..cli import command_index, global_options
from ..core import Library
from ..base import Error
from .common import Parser, Usage, fail, warn


def main(argv):
    """The command argv (beanjar's arguments) but the `_ares` query (cli.main): its exit status."""
    i = command_index(argv)
    if i < len(argv) and argv[i].startswith("ares-"):
        return ares_main(argv, i)
    if "--version" in argv[:i]:
        from .common import print_version

        print_version("--json" in argv)
        return 0
    from . import parser
    from .forward import remote_command

    Parser.json_errors = "--json" in argv  # until the line is read: an error there
    try:
        a = parser.parse(argv)
    except parser.NoVerb as e:
        if Parser.json_errors:
            return fail(e, True, 2)
        e.parser.print_help(sys.stderr)
        return 2
    except Usage as e:
        return fail(e, Parser.json_errors, 2)
    try:
        library = []

        def local_library():
            if not library:
                library.append(Library(a.library, a.repo))
            return library[0]

        if getattr(a, "remote", None):
            host, name = remote.locate(
                a.name, lambda n: n in local_library().names(), warn, a.library is not None
            )
            if host:
                return remote_command(a, a.words, host, name)
            a.name = name  # local/NAME is NAME here
        lib = local_library() if getattr(a, "library_needed", True) else None
        return a.func(lib, a)
    except Error as e:
        return fail(e, Parser.json_errors, 2 if isinstance(e, Usage) else 1)
    except KeyboardInterrupt:
        return 130


def ares_main(argv, i):
    """beanjar [GLOBAL...] ares-TOOL [--root] -d NAME [ARGS...] (ares.py): the tool's own status; ours on
    an error before it runs, as text (a --json among ARGS is the tool's)."""
    from .. import ares

    tool = argv[i]
    Parser.json_errors = False
    try:
        if not ares.TOOL_RE.fullmatch(tool):
            raise Usage(f"invalid tool name {tool!r}: ares-TOOL, lowercase letters and '-'")
        try:
            spec, root, args = ares.split_device(argv[i + 1 :])
        except Error as e:
            raise Usage(f"{tool}: {e}") from None
        if spec is None:
            if any(w in ("-h", "--help") for w in args):
                from . import parser

                parser.build().parse_args([*argv[:i], "ares-TOOL", "--help"])
            raise Usage(f"{tool}: say which VM: beanjar {tool} [--root] -d NAME [ARGS...]")
        if "--json" in argv[:i]:
            raise Usage(f"{tool} takes no --json: its output is the tool's")
        opts = global_options(argv[:i])
        if opts is None:
            from . import parser

            a = parser.build().parse_args([*argv[:i], "ares-TOOL"])
            opts = {"library": a.library, "repo": a.repo}
        library = []

        def local_library():
            if not library:
                library.append(Library(opts["library"], opts["repo"]))
            return library[0]

        host, name = remote.locate(
            spec, lambda n: n in local_library().names(), warn, opts["library"] is not None
        )
        if host:
            return ares.run_remote(host, name, tool, args, root)
        return ares.run_local(local_library(), name, tool, args, root)
    except Error as e:
        return fail(e, False, 2 if isinstance(e, Usage) else 1)
    except KeyboardInterrupt:
        return 130
