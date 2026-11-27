# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""python3 -m beanjar, and the launcher's `beanjar ARGS`: the VM manager's command line
(host/core/README.md). The commands are the rest of this package's (dispatch.py).

This front reads only where the command word is, so that the `_ares` query another machine's
remote ares-TOOL asks at every run (ares.remote_vm) is answered without loading the commands or
the library module (library.py), which import most of the manager: on a board every module imported
costs tens of milliseconds. The query reads the one VM's vm.toml and key (base.target)."""

import os
import sys

from .. import base


def command_index(argv):
    """Where the command word is in argv, after the global options."""
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("--library", "--repo") else 1
    return i


def global_options(words):
    """{"library", "repo"} of the global options words (--library DIR, --repo DIR, or with =DIR),
    read without the full parser (a light command's); None if there is any other word, for the
    full parser to read or refuse."""
    found = {"library": None, "repo": None}
    i = 0
    while i < len(words):
        opt, eq, value = words[i].partition("=")
        if opt not in ("--library", "--repo"):
            return None
        if not eq:
            if i + 1 >= len(words):
                return None
            i += 1
            value = words[i]
        found[opt.removeprefix("--")] = value
        i += 1
    return found


def json_error(e, code):
    """Error e as --json reports it, one JSON object on stderr ({"error": message, "key": setting
    or absent, "usage": true or absent, "drift": [...] or absent}, common.py's conventions): code, the exit status."""
    import json

    error = {"error": str(e)}
    if getattr(e, "key", None):
        error["key"] = e.key
    if code == 2:
        error["usage"] = True
    if getattr(e, "drift", None):
        error["drift"] = e.drift  # a command sent to another host: compat.drift's items
    print(json.dumps(error), file=sys.stderr)
    return code


def plain(cfg):
    """Whether base.target answers for a VM with settings cfg (vm.config.peek) as the library does:
    a slot (the library gives a VM without one its slot), and layers, slot and listen of the types
    vm.config.read would let through. For any other the query asks the library, which gives read's
    Error."""
    slot, layers, listen = cfg.get("slot"), cfg.get("layers"), cfg.get("listen")
    if isinstance(slot, bool) or slot not in base.SLOTS:
        return False
    if not isinstance(layers, list) or not all(isinstance(n, str) for n in layers):
        return False
    if listen is None:
        return True
    if not isinstance(listen, str):
        return False
    import ipaddress

    try:
        ipaddress.IPv4Address(listen)
    except ValueError:
        return False
    return True


def ares_query(argv, i):
    """beanjar [GLOBAL...] _ares local/NAME [--root] [--json] (base.ARES_QUERY): base.target of a
    VM of this machine as JSON, for another machine's ares-TOOL -d HOST/NAME (ares.remote_vm).
    An internal query, left out of the help; it reads the VM and asks no host. A word it does not
    take is a usage error (status 2, which ares.remote_vm reads as a beanjar to update).

    It reads only that VM's vm.toml (vm.config.peek) and key, without the library module; a vm.toml
    the light reading cannot answer for (plain) is read by the library (ares.target), as any other
    command reads it."""
    try:
        opts = global_options(argv[:i])
        words = argv[i + 1 :]
        flags = {w for w in words if w.startswith("-")}
        names = [w for w in words if not w.startswith("-")]
        if opts is None or flags - {base.ROOT_OPTION, "--json"} or len(names) != 1:
            usage = f"usage: beanjar {base.ARES_QUERY} local/NAME [--root] [--json]"
            return json_error(base.Error(usage), 2)
        host, name = base.split_spec(names[0])
        if host not in (None, base.LOCAL):
            usage = f"{base.ARES_QUERY} takes a VM of this machine: local/NAME"
            return json_error(base.Error(usage), 2)
        root = base.ROOT_OPTION in flags
        vm_dir = base.vm_dir(base.library_root(opts["library"]), name)
        from ..vm import config

        cfg = config.peek(os.path.join(vm_dir, base.CONFIG), base.TARGET_SETTINGS)
        if cfg is not None and plain(cfg):
            answer = base.target(name, vm_dir, cfg, root)
        else:
            from ..access import ares
            from ..vm import library

            answer = ares.target(library.Library(opts["library"], opts["repo"]).vm(name), root)
        import json

        print(json.dumps(answer))
        return 0
    except base.Error as e:
        return json_error(e, 1)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    i = command_index(argv)
    if i < len(argv) and argv[i] == base.ARES_QUERY:
        return ares_query(argv, i)
    from . import dispatch

    return dispatch.main(argv)


if __name__ == "__main__":
    sys.exit(main())
