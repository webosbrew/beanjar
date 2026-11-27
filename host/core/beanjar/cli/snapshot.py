# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's snapshots (beanjar/vm/snapshot.py): snapshot create, list, show, delete and reset."""

from .common import emit, kv_lines, progress, table


def size_text(n):
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if n >= 1 << shift:
            return f"{n / (1 << shift):.1f} {unit}"
    return f"{n} B"


def cmd_create(lib, a):
    from ..vm import snapshot

    r, seconds = snapshot.create(lib, a.name, a.snapshot, out=progress)
    r = dict(r, seconds=round(seconds, 2))
    return emit(
        a,
        r,
        f"{a.name}: snapshot {r['name']} made ({size_text(r['disk_bytes'])} on disk, "
        f"{r['copy']}, {seconds:.1f} s)",
    )


def cmd_list(lib, a):
    from ..vm import snapshot

    rows = snapshot.list_snapshots(lib.vm(a.name))
    columns = [
        ("SNAPSHOT", lambda r: r["name"], False),
        ("CREATED", lambda r: r["created"] or "-", False),
        ("DISK", lambda r: size_text(r["disk_bytes"]), True),
        ("FIRMWARE", lambda r: r["firmware"] or "-", False),
    ]
    return emit(a, rows, table(rows, columns) if rows else f"{a.name}: no snapshots")


def cmd_show(lib, a):
    from ..vm import snapshot

    r = snapshot.info(lib.vm(a.name), a.snapshot)
    return emit(a, r, "\n".join(kv_lines(dict(r, disks=", ".join(r["disks"]) or None))))


def cmd_delete(lib, a):
    from ..vm import snapshot

    snapshot.delete(lib, a.name, a.snapshot)
    return emit(
        a,
        {"name": a.snapshot, "vm": a.name, "deleted": True},
        f"{a.name}: snapshot {a.snapshot} deleted",
    )


def cmd_reset(lib, a):
    from ..vm import snapshot

    r, seconds = snapshot.reset(lib, a.name, a.snapshot, out=progress)
    return emit(
        a,
        {"name": r["name"], "vm": a.name, "reset": True, "seconds": round(seconds, 2)},
        f"{a.name}: reset to snapshot {r['name']} of {r['created']} ({seconds:.1f} s)",
    )


def register(cli):
    command, group, vm_name = cli.command, cli.group, cli.vm_name
    g = group(
        "snapshot",
        "a VM's snapshots: saved copies of its state disks and secure-store items, kept in its "
        "folder, to reset it to later; the VM's identity (MAC, serial, keys, settings) is never "
        "saved or put back",
        dest="verb",
    )

    def snap_arg(s):
        s.add_argument(
            "snapshot",
            metavar="SNAP",
            help="the snapshot's name: letters, digits, '.', '_', '-'",
        )

    s = command(
        g,
        "create",
        "save the state of a VM that is off as the snapshot SNAP: every state disk and its "
        "secure-store items (identity/sestore.toml), reflinked where the filesystem can, else "
        "copied sparse",
        cmd_create,
        usage="beanjar snapshot create NAME SNAP [--json]",
    )
    vm_name(s)
    snap_arg(s)
    s = command(
        g,
        "list",
        "the VM's snapshots: when each was made, its space on disk and its firmware",
        cmd_list,
        usage="beanjar snapshot list NAME [--json]",
    )
    vm_name(s)
    s = command(
        g,
        "show",
        "one snapshot: when it was made, the firmware, serial and MAC it is of, its disks and size",
        cmd_show,
        usage="beanjar snapshot show NAME SNAP [--json]",
    )
    vm_name(s)
    snap_arg(s)
    s = command(
        g,
        "delete",
        "remove the snapshot SNAP (the VM may run: its state is not touched)",
        cmd_delete,
        usage="beanjar snapshot delete NAME SNAP [--json]",
    )
    vm_name(s)
    snap_arg(s)
    s = command(
        g,
        "reset",
        "put a VM that is off back to the snapshot SNAP: its state disks and secure-store items "
        "replaced by the snapshot's (the model key it has now kept), everything later gone; "
        "refused for a snapshot of another serial or firmware",
        cmd_reset,
        usage="beanjar snapshot reset NAME SNAP [--json]",
    )
    vm_name(s)
    snap_arg(s)
