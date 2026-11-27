# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""shell: a root shell or one command inside webOS (guestshell.py)."""

import os

from .common import Usage

HELP = (
    "a root shell inside webOS, or one command (beanjar shell NAME -- CMD...): over the VM's own "
    "shell port, with no SSH, developer mode or network"
)
DESCRIPTION = (
    "Run a command inside webOS on the running VM NAME, as root, in the environment of the debug "
    "console's webos-enter (webOS's namespaces and root, PATH and HOME), over the VM's own "
    "virtio-serial port: no SSH, developer mode, network or layer. Without CMD: an interactive "
    "login shell. Everything after NAME is the command (a leading -- is dropped), so beanjar's "
    "options come before NAME. With a terminal on stdin and no command the session gets a "
    "terminal of its own (its size follows this one's; Ctrl-C and the other keys reach the "
    "shell's job); otherwise stdin, stdout and stderr are passed through apart, without a "
    "terminal, and the signals beanjar gets (SIGINT, SIGQUIT, SIGTERM) go on to the command; a "
    "third Ctrl-C gives it up. The exit status is the command's (128+N for its signal N); 255 if "
    "the session ended without one (the VM stopped or restarted, the connection was lost). "
    "Several sessions may run at once. HOST/NAME is reached through a forward of the host's "
    "shared ssh connection. Whoever can open the VM's run directory's shell.sock is root in the "
    "VM, as with its serial console."
)


def command_words(a):
    words = list(a.command)
    if words[:1] == ["--"]:
        words = words[1:]
    return words


def session_tty(a, words):
    if a.json:
        raise Usage("shell takes no --json: its output is the command's")
    try:
        from ..access.guestshell import want_tty

        return want_tty(words, a.tty, a.no_tty, os.isatty(0))
    except ValueError as e:
        raise Usage(f"shell: {e}") from None


def cmd_shell(lib, a):
    from ..access import guestshell

    words = command_words(a)
    tty = session_tty(a, words)
    sock = guestshell.local_socket(lib.vm(a.name))
    with sock:
        return guestshell.run(sock, words, tty)


def remote_shell(a, host, name):
    from ..access import guestshell

    words = command_words(a)
    return guestshell.run_remote(host, name, words, session_tty(a, words))


def register(cli):
    import argparse

    s = cli.command(
        cli.sub,
        "shell",
        HELP,
        cmd_shell,
        usage="beanjar shell [-t | -T] NAME [[--] CMD [ARG...]]",
    )
    s.description = DESCRIPTION
    s.add_argument(
        "-t",
        dest="tty",
        action="store_true",
        help="give the command a terminal (as an interactive shell has): stdout and stderr "
        "together, stdin as typed",
    )
    s.add_argument(
        "-T", dest="no_tty", action="store_true", help="no terminal, not even for a login shell"
    )
    cli.vm_name(s, "shell")
    s.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        metavar="CMD",
        help="the command and its arguments (default: a login shell)",
    )
