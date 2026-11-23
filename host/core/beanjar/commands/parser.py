# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The command line's parser: the global options, and each command group's own commands and
options (each module's register functions, called in the order `beanjar --help` lists them)."""

import argparse
import os
import shutil
import textwrap

from .. import core
from . import (
    devices,
    display,
    identity,
    mitm,
    remote_control,
    sensors,
    settings,
    shell,
    vms,
)
from .common import Parser, Usage, parser_class


class Tree:
    """What a module's register(cli) adds its commands with: sub, the top-level subparsers;
    command, group and vm_name."""

    def __init__(self, sub, common):
        self.sub, self.common = sub, common

    def command(self, group, name, help, func, usage=None, **kw):
        s = group.add_parser(
            name, help=help, description=help, usage=usage, parents=[self.common], **kw
        )
        s.set_defaults(func=func)
        return s

    def group(self, name, help, under=None, dest="verb_group", metavar="VERB"):
        g = (under or self.sub).add_parser(name, help=help, description=help)
        return g.add_subparsers(dest=dest, required=True, metavar=metavar)

    @staticmethod
    def vm_name(s, remote="forward"):
        # NAME is the VM here, or HOST/NAME on a configured host (forward.remote_command)
        s.add_argument(
            "name", metavar="NAME", help="the VM: NAME, or HOST/NAME for a remote host's"
        )
        s.set_defaults(remote=remote)


GLOBAL_VALUES = ("--library", "--repo")
JSON = "--json"


def subcommands(p):
    """{word: parser} of a group's commands; None for a command (a leaf)."""
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return None


class NoVerb(Usage):
    """A group given without its verb (or beanjar without a command): main prints the group's
    help on stderr, exit status 2."""

    def __init__(self, path, parser):
        here = " ".join(["beanjar", *path])
        verbs = ", ".join(v for v in subcommands(parser) if v != "ares-TOOL")
        super().__init__(f"{here} needs {'one of' if path else 'a command'}: {verbs}")
        self.parser = parser


def wrong_verb(path, choices, word, rest):
    """The usage error for word where the group path wants one of its verbs, naming the right
    form; None to leave it to argparse (a word no form explains, at the top)."""
    later = [w for w in rest if not w.startswith("-")]
    here = " ".join(["beanjar", *path])
    if later and later[0] in choices:
        # the command's words, as far as they go (vnc tv1 password show: vnc password show)
        words, node, tail = [], choices, list(rest)
        while tail and node and tail[0] in node:
            words.append(tail[0])
            node = subcommands(node[tail.pop(0)])
        return Usage(f"the verb comes first: {' '.join([here, *words, word, *tail])}")
    verbs = [v for v, sp in choices.items() if word in (subcommands(sp) or {})]
    if verbs:
        return Usage(
            f"{here} takes its verb first: {' '.join([here, '|'.join(verbs), word, *rest])}"
        )
    if not path:
        return None
    return Usage(
        f"{here}: no verb {word!r} (one of {', '.join(choices)}); the verb comes before the "
        f"VM: {here} VERB NAME"
    )


class Line:
    """A command line as read_line reads it, before argparse."""

    def __init__(self):
        self.library = self.repo = None
        self.json = False
        self.words = []  # the line without the global options
        self.path = []  # the command's words: start, vnc password show, ...
        self.leaf_at = None  # where in words the command's own arguments begin
        self.help = False


def read_line_rest(words):
    """words without the global options (for an error's right form)."""
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif w in GLOBAL_VALUES:
            skip = True
        elif w != JSON and w.partition("=")[0] not in GLOBAL_VALUES:
            out.append(w)
    return out


def read_line(p, argv):
    """The command line argv as a Line. The global options, --json, --library DIR and --repo DIR
    (or =DIR), are taken wherever they are, but after a `--` and in shell's command (the words
    after its NAME), which are the command's. A group without its verb is NoVerb; a word where a
    verb should be, a Usage naming the right form."""
    line, node = Line(), p
    rest_is_args = shell_name = False
    i = 0
    while i < len(argv):
        w = argv[i]
        i += 1
        if rest_is_args:
            line.words.append(w)
            continue
        if w == "--":
            rest_is_args = True
            line.words.append(w)
            continue
        opt, eq, value = w.partition("=")
        if opt in GLOBAL_VALUES:
            if not eq:
                if i >= len(argv):
                    raise Usage(f"{opt} needs a value: {opt} DIR")
                value = argv[i]
                i += 1
            setattr(line, opt.removeprefix("--"), value)
            continue
        if w == JSON:
            line.json = True
            continue
        line.words.append(w)
        if w in ("-h", "--help"):
            line.help = True
        if w.startswith("-"):
            continue
        if line.leaf_at is None:
            choices = subcommands(node)
            if w not in choices:
                error = wrong_verb(line.path, choices, w, read_line_rest(argv[i:]))
                if error:
                    raise error
                line.leaf_at = len(line.words) - 1  # argparse says what is wrong
                continue
            line.path.append(w)
            node = choices[w]
            if subcommands(node) is None:
                line.leaf_at = len(line.words)
                shell_name = line.path == ["shell"]
        elif shell_name:
            rest_is_args = True  # its NAME: what follows is the command
    if line.leaf_at is None and not line.help:
        raise NoVerb(line.path, node)
    return line


def parse(argv, p=None):
    """argv parsed (read_line, then argparse): the namespace, with path, the command's words, and
    words, the line without its global options (what another host's beanjar is given)."""
    p = p or build()
    line = read_line(p, argv)
    Parser.json_errors = line.json
    at = len(line.words) if line.leaf_at is None else line.leaf_at
    front = ["--library", line.library] if line.library is not None else []
    front += ["--repo", line.repo] if line.repo is not None else []
    json_word = [JSON] if line.json and line.leaf_at is not None else []
    a = p.parse_args([*front, *line.words[:at], *json_word, *line.words[at:]])
    a.path, a.words = line.path, line.words
    check_name(p, a)
    return a


def check_name(p, a):
    """A VM's NAME that is KEY=VALUE, or one of its group's verbs: the order is wrong."""
    name = getattr(a, "name", None)
    if not getattr(a, "remote", None) or not name:
        return
    here = " ".join(["beanjar", *a.path])
    if "=" in name:
        rest = a.words[len(a.path) :]
        vm = next((w for w in rest if not w.startswith("-") and "=" not in w), "NAME")
        right = " ".join([here, vm, *(w for w in rest if w != vm)])
        raise Usage(f"the VM comes before KEY=VALUE: {right}")
    group = p
    for w in a.path[:-1]:
        group = subcommands(group)[w]
    if name in subcommands(group):
        above = " ".join(["beanjar", *a.path[:-1]])
        raise Usage(
            f"{name!r} is a verb of {above}, which comes before the VM: {above} {name} NAME "
            f"(a VM called {name}: local/{name})"
        )


def register_ares(cli):
    """ares-TOOL, for the help: __init__.ares_main runs it before the parser."""
    sub = cli.sub
    s = sub.add_parser(
        "ares-TOOL",
        help="run a webOS SDK tool (ares-install, ares-shell, ares-launch, ...) against the VM "
        "NAME alone, ARGS passed on: beanjar ares-TOOL [--root] -d NAME [ARGS...]",
        description="Run the webOS SDK tool ares-TOOL (the Rust ares-cli or the official CLI, "
        "from PATH or ~/.cargo/bin) against the running VM NAME (it needs the devmode layer), "
        "which is then the tool's only and default device, the developer-mode user prisoner; "
        "with --root it is root over the VM's root SSH instead (it needs a layer that forwards "
        "the guest's port 22: rootssh, or hbchannel with devmode) with the VM's client key. "
        "-d NAME and --root are beanjar's, the other arguments are the tool's (its own help: "
        "beanjar ares-TOOL -d NAME --help). The exit status is the tool's. The tool always runs "
        "on this machine: a VM on a remote host (HOST/NAME) is reached through an ssh port "
        "forward to it, made for the command.",
        usage="beanjar ares-TOOL [--root] -d NAME [ARGS...]",
    )
    s.add_argument("args", nargs="*", metavar="ARGS", help="the tool's arguments")


REGISTER = (
    vms.register_lifecycle,
    display.register_pictures,
    vms.register_vm,
    settings.register_vm_settings,
    display.register,
    remote_control.register,
    identity.register,
    settings.register_config,
    register_ares,
    shell.register,
    sensors.register,
    devices.register,
    mitm.register,
)


def running_as_agent():
    """True when an AI coding agent is driving beanjar (AI_AGENT, or Claude Code's CLAUDECODE),
    so the help can point at the beanjar skill. Vendor-neutral: AI_AGENT comes first."""
    return bool(os.environ.get("AI_AGENT") or os.environ.get("CLAUDECODE"))


def build():
    """The command line's parser."""
    Parser = parser_class()  # noqa: N806 (a class)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        help="print the result as JSON on stdout (an error as one JSON object on stderr)",
    )
    paragraphs = [
        "Commands are VERB NAME or NOUN... VERB NAME: the verb always comes before the VM "
        "(NAME, HOST/NAME, local/NAME); a group without its verb prints its help. The global "
        "options --json, --library and --repo go anywhere on the line (but in shell's command, "
        "in an ares-TOOL's ARGS and after --); every command but shell and ares-TOOL takes "
        "--json. Durations take a unit: 200ms, 1.5s, 2m. Exit status: 0 done, 1 the operation failed, 2 a usage error. Progress and "
        "warnings go to stderr. A VM's setting keys: beanjar config set --help."
    ]
    if running_as_agent():
        paragraphs.append(
            "DID YOU KNOW: there is a 'beanjar' skill with usage patterns and conventions. "
            "Load it with the Skill tool (/beanjar), then come back to --help for the exact, "
            "current options of any command."
        )
    width = max(40, min(shutil.get_terminal_size((80, 24)).columns, 100)) - 2
    epilog = "\n\n".join(textwrap.fill(para, width) for para in paragraphs)
    p = Parser(
        prog="beanjar",
        description="Beanjar: webOS TV firmware in QEMU VMs (host/core/README.md)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    p.add_argument(
        "--library",
        help=f"library directory (default $BEANJAR_LIBRARY or {core.default_library()}); "
        "anywhere on the line",
    )
    p.add_argument(
        "--repo",
        help="the Beanjar checkout (stored in the library's config.toml); anywhere on the line",
    )
    p.add_argument(
        "--version",
        action="store_true",
        help="print the program name and version and exit (with --json, as an object)",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    cli = Tree(sub, common)
    for register in REGISTER:
        register(cli)
    return p
