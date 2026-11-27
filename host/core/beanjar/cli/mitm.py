# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The mode=mitm traffic inspector's tool: mitm show, connect, clear."""

from .common import emit


def cmd_mitm_show(lib, a):
    from ..network import inspector

    data = inspector.show(lib.vm(a.name), a.password)
    return emit(a, data)


def cmd_mitm_connect(lib, a):
    from ..network import inspector

    return inspector.connect_local(lib.vm(a.name))


def cmd_mitm_clear(lib, a):
    from ..network import inspector

    return mitm_cleared(a, inspector.clear_local(lib.vm(a.name)))


def mitm_cleared(a, data):
    if data is None:  # ended by a signal
        return 1
    return emit(a, data, f"{data['name']}: flows cleared")


def register(cli):
    command, group, vm_name = cli.command, cli.group, cli.vm_name
    g = group("mitm", "the mode=mitm traffic inspector's tool")
    s = command(
        g,
        "show",
        "the traffic inspector (mitmweb) of a running mode=mitm VM: its status, whether it is "
        "ready, its URL (with link lan the LAN one, and local_url, the VM's host's way in) and "
        "the file with its password, and mitmweb: the version and path this start found, or the "
        "problem that kept the inspector off (no mitmweb, older than 12.0.1); with --password "
        "the password too",
        cmd_mitm_show,
    )
    vm_name(s)
    s.add_argument("--password", action="store_true", help="print the inspector's password too")
    s = command(
        g,
        "connect",
        "make the VM's traffic inspector reachable from this machine and keep it so until stdin "
        "ends, across the VM's restarts: prints one JSON line, {url, password}, and another "
        "whenever they change (a restarted VM's inspector has a new password and no flows); "
        "stderr says when it is gone and back. For HOST/NAME the URL is a forward on 127.0.0.1 "
        "over the host's shared ssh connection, kept up while it holds and cancelled at the end "
        "(the web viewer's /vm/HOST/NAME/mitm/)",
        cmd_mitm_connect,
    )
    vm_name(s, "mitm-connect")
    s = command(
        g,
        "clear",
        "empty the traffic inspector's flow list, as its page's Clear does",
        cmd_mitm_clear,
    )
    vm_name(s, "mitm-clear")
