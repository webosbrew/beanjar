# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's identity as a TV: identity reset and authorized-keys, clone, and its secure-store keys
(key)."""

import os

from .. import remote
from ..core import Error
from .common import emit, progress, warn


def cmd_identity_reset(lib, a):
    vm, mac, serial = lib.renew_identity(a.name, out=progress)
    has = vm.has_model_key()
    return emit(
        a,
        {"name": vm.name, "mac": mac, "serial": serial, "model_key": has},
        f"{vm.name}: a new TV (mac {mac}, serial {serial}, new SSH and developer keys, a new VNC "
        f"password, state wiped; model key {'kept' if has else 'none'})",
    )


def cmd_clone(lib, a):
    from .. import clone

    r = clone.clone(lib, a.source, a.dest, out=progress, warn=warn, pinned=a.library is not None)
    return emit(
        a,
        r,
        f"{r['source']} copied to {r['host'] or remote.LOCAL}/{r['name']}: mac {r['mac']}, serial "
        f"{r['serial']}, new SSH and developer keys and VNC password; settings, state and "
        "secure-store keys kept",
    )


def cmd_authorized_keys(lib, a):
    """Root SSH's authorized_keys of a VM (keys.py): show what the guest gets, reset it to a new
    VM's (no key but the VM's own), or add the user's own keys, explicitly."""
    from .. import keys

    vm = lib.vm(a.name)
    d = vm.identity_dir
    verb = a.verb
    path = os.path.join(d, keys.AUTHORIZED)
    try:
        if verb == "reset":
            old = keys.reset_authorized(d)
            note = f"{vm.name}: {path} reset: {len(old)} key(s) removed"
        elif verb == "add":
            added = keys.add_mine(d)
            note = f"{vm.name}: {len(added)} of your keys (~/.ssh/*.pub) added to {path}"
        else:
            note = None
        listed = keys.authorized(d)
    except FileNotFoundError as e:
        raise Error(
            f"{e.filename} is missing (made at create, or at the VM's next start)"
        ) from None
    except keys.Error as e:
        raise Error(str(e)) from None
    lines = [f"{k['whose']:5}  {k['type']}  {k['fingerprint']}" for k in listed]
    if verb != "show" and vm.alive():
        lines.append("the guest has the old keys until the VM's next start")
    return emit(
        a,
        {"name": vm.name, "path": path, "keys": listed},
        "\n".join(([note] if note else []) + lines),
    )


def cmd_key_model(lib, a):
    """A VM's model key: whether it has one (show), set it from FILE, or unset it."""
    verb = a.verb
    if verb == "set":
        vm = lib.set_model_key(a.name, a.file)
    elif verb == "unset":
        vm = lib.set_model_key(a.name, None)
    else:
        vm = lib.vm(a.name)
    has = vm.has_model_key()
    from .. import sestore

    try:
        missing, items = sestore.load(vm.identity_dir)
    except sestore.Error as error:
        raise Error(str(error)) from None
    source = os.path.join(vm.identity_dir, sestore.CONFIG)
    names = [n for n in sestore.MODELS if n in items]
    return emit(
        a,
        {
            "name": vm.name,
            "model_key": has,
            "items": names,
            "source": source if has else None,
            "missing": missing,
        },
        f"{vm.name}: "
        + (
            f"provisioned model key {', '.join(names)} ({source}; value omitted)"
            if has
            else "no model key: "
            + ("missing policy is error" if missing == "error" else "the built-in model secret")
        ),
    )


def cmd_key_list(lib, a):
    """The item names in a VM's identity/sestore.toml, never their values."""
    from .. import sestore

    vm = lib.vm(a.name)
    try:
        missing, items = sestore.load(vm.identity_dir)
    except sestore.Error as error:
        raise Error(str(error)) from None
    names = sorted(items)
    return emit(
        a,
        {"name": vm.name, "items": names, "missing": missing},
        "\n".join(names) if names else f"{vm.name}: no items in {sestore.CONFIG}",
    )


def register(cli):
    sub, command, group, vm_name = cli.sub, cli.command, cli.group, cli.vm_name
    identity = group("identity", "a VM's identity as a TV")
    s = command(
        identity,
        "reset",
        "make a VM that is off another TV: its state disks wiped, a new MAC and "
        "serial, new SSH and developer keys, a new VNC password (the model key and the library's "
        "CA kept)",
        cmd_identity_reset,
    )
    vm_name(s)

    s = command(
        sub,
        "clone",
        "make DEST a copy of the VM SRC, which is off, as another TV: SRC's settings, state disks "
        "and secure-store keys (the model key) kept, a new MAC, serial, SSH and developer keys and "
        "VNC password as identity reset makes them, the state not wiped. Either may be on a host "
        "(HOST/NAME; DEST without one is this machine's), this machine relaying the copy; DEST's "
        "host needs SRC's firmware at the same path",
        cmd_clone,
        usage="beanjar clone SRC DEST [--json]",
    )
    s.add_argument(
        "source", metavar="SRC", help="the VM to copy: NAME or HOST/NAME; it must be off"
    )
    s.add_argument(
        "dest", metavar="DEST", help="the new VM: NAME (this machine), local/NAME or HOST/NAME"
    )

    g = group(
        "authorized-keys",
        "the public keys that may log in to the VM as root over SSH (rootssh, hbchannel) and so "
        "go into the guest, from identity/authorized_keys and the VM's own client key, each with "
        "its fingerprint and whose it is (vm, mine: one of your ~/.ssh/*.pub, other). A change "
        "reaches the guest at its next start",
        under=identity,
        dest="verb",
    )
    s = command(g, "show", "list the keys", cmd_authorized_keys)
    vm_name(s)
    s = command(
        g, "reset", "remove every key but the VM's own (what a new VM has)", cmd_authorized_keys
    )
    vm_name(s)
    s = command(
        g,
        "add",
        "add keys: with --mine your own ~/.ssh/*.pub, without their comments (the guest can read "
        "every key it gets, and your keys identify you)",
        cmd_authorized_keys,
        usage="beanjar identity authorized-keys add NAME --mine [--json]",
    )
    vm_name(s, "authkeys-add")
    s.add_argument(
        "--mine", action="store_true", required=True, help="your own ~/.ssh/*.pub (required)"
    )

    key = group("key", "a VM's secure-store keys (identity/sestore.toml)")
    g = group(
        "model",
        "a VM's SDP model key, the item its firmware reads (from kisscurl on the board's regional "
        "1D492999_a, _d or _r, else 1D492999); its value is never printed",
        under=key,
        dest="verb",
    )
    s = command(g, "show", "whether the VM has a model key, and its items", cmd_key_model)
    vm_name(s)
    s = command(
        g,
        "set",
        "set the VM's model key from FILE (44 characters of base64 for 32 bytes) while it is off",
        cmd_key_model,
        usage="beanjar key model set NAME FILE [--json]",
    )
    vm_name(s, "keymodel-set")
    s.add_argument("file", metavar="FILE")
    s = command(
        g,
        "unset",
        "remove the VM's model key while it is off (then it follows the missing policy)",
        cmd_key_model,
    )
    vm_name(s)
    s = command(
        key, "list", "the item names in the VM's sestore.toml, never their values", cmd_key_list
    )
    vm_name(s)
