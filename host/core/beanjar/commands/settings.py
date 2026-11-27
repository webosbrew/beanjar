# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Config: a VM's (config), the library's (library config) and this machine's (host config,
add, remove, list, prune-layer-images)."""

import argparse
import os

from .. import capacity, core, hostconfig, paths, remote, vmsettings
from ..base import Error
from .common import Usage, emit, layer_versions, progress, table, text_value


def settings_of(a):
    """create's settings (vm.toml's but fw) from its options."""
    keys = ("mem", "smp", "layers", "link", "mode", "display", "audio")
    return {**{k: getattr(a, k) for k in keys}, "tags": a.tag}


def settings_view(vm):
    """The VM's config (SETTINGS' keys, and tags): vm.toml's values, None where it leaves the
    default; and, with a versioned layer, layer_versions: {layer: the version it runs} (plain
    NAME's default named), which is no setting of its own."""
    c = vm.config_unchecked  # a bad broadcast still shows, to be fixed
    out = {k: c.get(k) for k in vmsettings.SETTINGS}
    out["tags"] = c.get("tags") or None
    versions = layer_versions(c.get("layers"))
    if versions:
        out["layer_versions"] = versions
    return out


def key_values(items, keys, cmd):
    """KEY=VALUE words as {KEY: VALUE}, each KEY one of keys."""
    values = {}
    for kv in items:
        k, sep, v = kv.partition("=")
        if not sep or k not in keys:
            raise Usage(f"{kv}: KEY=VALUE with KEY one of {' '.join(keys)} (beanjar {cmd} --help)")
        values[k] = v
    return values


def set_or_unset(a, group, keys, unsettable, apply, name=None):
    """A config command's verb (a.verb): show, set KEY=VALUE... or unset KEY...; apply(values)
    makes the change, an unset being KEY's empty value, which only the keys of unsettable take (set
    refuses it for them, naming unset). group is the command's group (config, library config),
    name the VM's NAME as given, if it has one."""
    verb = a.verb

    def form(v):
        return " ".join([group, v, *([name] if name else [])])

    if verb == "show":
        return
    if verb == "set":
        values = key_values(a.items, keys, form(verb))
        for k, v in values.items():
            if k in unsettable and not v.strip():
                raise Usage(
                    f"{k}= is empty: beanjar {form('unset')} {k} sets it back to the default"
                )
    else:
        for k in a.items:
            if k not in keys:
                raise Usage(f"{k}: KEY one of {' '.join(keys)} (beanjar {form(verb)} --help)")
            if k not in unsettable:
                raise Usage(
                    f"{k} has no unset (only {', '.join(unsettable)}); set it to a value instead"
                )
        values = dict.fromkeys(a.items, "")
    apply(values)


CONFIG_KEYS = (*vmsettings.SETTINGS, "tags")


def cmd_config(lib, a):
    """A VM's config: shown, set or unset (back to the default); its settings while it is off, its
    tags whether it runs or not."""
    unsettable = [k for k, h in vmsettings.SETTING_HELP.items() if h[3]] + ["tags"]
    changed = []

    def apply(values):
        tags = values.pop("tags", None)
        if values:
            changed.append(lib.configure(a.name, out=progress, **values))
        if tags is not None:
            lib.set_tags(a.name, tags or [])

    set_or_unset(a, "config", CONFIG_KEYS, unsettable, apply, a.name)
    vm = changed[0] if changed else lib.vm(a.name)
    emit(a, settings_view(vm), none="(default)")
    if changed and a.verb == "set":
        warn_mitm(vm)
    return 0


def settings_help():
    """The config keys as `beanjar config set --help` lists them, from vmsettings.SETTING_HELP."""
    import textwrap

    def described(text):
        return textwrap.fill(text, width=94, initial_indent="      ", subsequent_indent="      ")

    lines = [
        "Settings change only while the VM is off, tags whether it runs or not. Each KEY, its",
        "values, its default, and what `unset` brings back where it has one:",
        "",
    ]
    for k, (values, default, what, unset) in vmsettings.SETTING_HELP.items():
        lines.append(f"  {k}={values}")
        lines.append(
            described(f"{what}; default {default}" + (f"; unset: {unset}" if unset else ""))
        )
    lines.append("  tags=TAG[,TAG...]")
    lines.append(
        described(
            "the VM's tags (letters, digits, dashes), in place of the ones it has: labels, listed "
            "by list --tag and copied by clone; default none; unset: no tags"
        )
    )
    return "\n".join(lines)


def warn_mitm(vm):
    """Say so when a mode=mitm VM will start without its traffic inspector (mitmtool.py)."""
    from ..network import mitmtool

    if vmsettings.guest_net(vm.config.get("link"), vm.config.get("mode")) == "mitm":
        tool = mitmtool.check()
        if not tool.usable:
            progress(
                f"{vm.name}: the traffic inspector is off, the guest goes out directly: {tool.problem}"
            )


def config_command(a, group, keys, unsettable, setter):
    set_or_unset(
        a, group, keys, unsettable, lambda values: [setter(k, v) for k, v in values.items()]
    )


def cmd_library_config(lib, a):
    """The library's settings (config.toml): shown, set or unset."""
    config_command(a, "library config", vmsettings.CONFIG_KEYS, ("lan_parent",), lib.set_option)
    shown = dict(lib.config, mac_prefix=lib.mac_prefix, addresses=lib.addresses)
    return emit(a, {k: shown[k] for k in vmsettings.CONFIG_KEYS if k in shown})


def cmd_host_config(lib, a):
    """This machine's settings, every library's ($XDG_CONFIG_HOME/beanjar/config.toml)."""
    config_command(
        a,
        "host config",
        hostconfig.GLOBAL_KEYS,
        hostconfig.GLOBAL_KEYS,
        hostconfig.set_global_option,
    )
    shown = hostconfig.global_config()
    data = {
        "max_running": shown.get("max_running"),
        "firmware_dirs": shown.get("firmware_dirs") or list(paths.DEFAULT_FIRMWARE_DIRS),
        "memory_headroom_mib": shown.get("memory_headroom_mib") or capacity.START_HEADROOM_MIB,
        "runner": shown.get("runner") or "auto",
    }
    text = "\n".join(
        [
            f"max_running: {text_value(data['max_running'], '(not set: no limit)')}",
            f"firmware_dirs: {':'.join(data['firmware_dirs'])}"
            + ("" if "firmware_dirs" in shown else " (not set: the default)"),
            f"memory_headroom_mib: {data['memory_headroom_mib']}"
            + ("" if "memory_headroom_mib" in shown else " (not set: the default)"),
            f"runner: {data['runner']}"
            + ("" if "runner" in shown else " (not set: systemd where it runs, else a session)"),
        ]
    )
    return emit(a, data, text)


def cmd_host_add(lib, a):
    remote.add_host(a.host_name, a.uri)
    return emit(a, {"name": a.host_name, "uri": a.uri}, f"added host {a.host_name} ({a.uri})")


def cmd_host_remove(lib, a):
    remote.remove_host(a.host_name)
    return emit(a, {"name": a.host_name, "removed": True}, f"removed host {a.host_name}")


def cmd_host_show(lib, a):
    """host show NAME: the host's beanjar and guest payload (its `beanjar --version --json`, asked
    now) and what differs from this beanjar (compat.drift)."""
    from .. import compat
    from .common import version_info

    hosts = remote.hosts()
    if a.host_name not in hosts:
        raise Error(f"no host named {a.host_name} (beanjar host list)")
    uri = hosts[a.host_name]
    info = remote.call_json(a.host_name, uri, ["--version"])
    if not isinstance(info, dict) or not isinstance(info.get("version"), str):
        raise remote.HostError(a.host_name, "its beanjar --version --json is not an object")
    drift = compat.drift(version_info(), info, a.host_name)
    data = {"name": a.host_name, "uri": uri, "beanjar": info, "drift": drift}
    guest = info.get("guest") if isinstance(info.get("guest"), dict) else {}
    lines = [
        f"name: {a.host_name}",
        f"uri: {uri}",
        f"beanjar: {compat.label(info)}" + (f" ({info['source']})" if info.get("source") else ""),
        f"beanjar-guest: {compat.label(guest) if guest else 'unknown (an older beanjar)'}",
    ]
    lines += [f"drift: {compat.drift_text(d)}" for d in drift] or ["drift: none"]
    return emit(a, data, "\n".join(lines))


def cmd_host_list(lib, a):
    rows = [{"name": n, "uri": u} for n, u in remote.hosts().items()]
    if a.json or not rows:
        return emit(a, rows, "no hosts (beanjar host add NAME ssh://[USER@]HOST[:PORT])")
    return emit(
        a,
        rows,
        table(rows, [("NAME", lambda r: r["name"], False), ("URI", lambda r: r["uri"], False)]),
    )


def cmd_host_prune_layer_images(lib, a):
    """Remove the cached layer images no VM of this library (and its development libraries) uses."""
    from .. import base, layerimage, paths

    libraries = [core.Library(r) for r in base.slot_domain(lib.root)[1] if os.path.isdir(r)]
    try:
        removed = layerimage.prune(
            paths.TREE,
            lambda: layerimage.referenced(paths.TREE, libraries),
            dry_run=a.dry_run,
        )
    except layerimage.Error as exc:
        raise Error(str(exc)) from None
    verb = "would remove" if a.dry_run else "removed"
    return emit(
        a,
        {"removed": removed, "dry_run": a.dry_run},
        "\n".join([f"{verb} {len(removed)} unused layer image(s)"] + removed),
    )


def config_verbs(cli, g, func, what, usage_name, keys_help="", epilog=None, vm=False):
    """A config group's show, set and unset commands (in g), each running func."""
    name = " NAME" if vm else ""
    extra = (
        {"epilog": epilog, "formatter_class": argparse.RawDescriptionHelpFormatter}
        if epilog
        else {}
    )
    for verb, text, usage, nargs, metavar in (
        ("show", f"show {what}", f"show{name}", None, None),
        (
            "set",
            f"set {what}: KEY=VALUE...{keys_help}",
            f"set{name} KEY=VALUE...",
            "+",
            "KEY=VALUE",
        ),
        ("unset", f"unset keys of {what}, back to the default", f"unset{name} KEY...", "+", "KEY"),
    ):
        s = cli.command(
            g, verb, text, func, usage=f"beanjar {usage_name} {usage} [--json]", **extra
        )
        if vm:
            cli.vm_name(s)
        if nargs:
            s.add_argument("items", nargs=nargs, metavar=metavar)
        else:
            s.set_defaults(items=[])
        if usage_name == "host config":
            s.set_defaults(library_needed=False)  # needs no library, and makes none


def register_vm_settings(cli):
    g = cli.group(
        "config",
        "a VM's config, kept in its vm.toml: its settings (changed while it is off) and its tags "
        "(whenever); show it, set KEY=VALUE or unset KEY (back to the default). The keys: "
        "beanjar config set --help",
        dest="verb",
    )
    config_verbs(cli, g, cmd_config, "the VM's config", "config", epilog=settings_help(), vm=True)


def register_config(cli):
    command, group = cli.command, cli.group
    g = group("library", "this library's settings")
    g = group(
        "config",
        "the library's settings (config.toml): show them, set KEY=VALUE "
        f"({', '.join(vmsettings.CONFIG_KEYS)}) or unset lan_parent",
        under=g,
        dest="verb",
    )
    config_verbs(
        cli,
        g,
        cmd_library_config,
        "the library's settings",
        "library config",
        f" ({', '.join(vmsettings.CONFIG_KEYS)})",
    )

    host = group("host", "this machine's settings, every library's")
    g = group(
        "config",
        "this machine's settings ($XDG_CONFIG_HOME/beanjar/config.toml): max_running=N, how "
        "many VMs may run at once on this host (every library's, the development VMs' and the "
        "regress harness's count); unset max_running for no limit (the default). "
        "memory_headroom_mib=N, positive MiB reserved beyond each VM's RAM for admission and "
        "the KVM soft memory limit (default 1024). "
        "firmware_dirs=DIR[:DIR...], the host's firmware collections, where create --firmware NAME, "
        "the launcher and regress look a firmware up (default /srv/beanjar/firmware). "
        "runner=auto|systemd|session, how VMs run here (default auto: a systemd user service "
        "where a user systemd runs, else a session of their own; BEANJAR_RUNNER overrides)",
        under=host,
        dest="verb",
    )
    config_verbs(
        cli,
        g,
        cmd_host_config,
        "this machine's settings",
        "host config",
        f" ({', '.join(hostconfig.GLOBAL_KEYS)})",
    )
    g = host
    s = command(
        g,
        "prune-layer-images",
        "remove the cached layer images (~/.cache/beanjar/layer-images, a checkout's "
        "build/layer-images) that no VM of this library uses, versions pinned in vm.toml "
        "included; refused while any VM runs or starts; the package's shipped images stay",
        cmd_host_prune_layer_images,
        usage="beanjar host prune-layer-images [--dry-run] [--json]",
    )
    s.add_argument("--dry-run", action="store_true", help="list what would be removed")
    s = command(
        g,
        "add",
        "add a remote host: beanjar runs its own beanjar there over ssh (key-based login). URI is "
        "ssh://[USER@]HOST[:PORT]; HOST may be an alias of ~/.ssh/config",
        cmd_host_add,
        usage="beanjar host add NAME URI [--json]",
    )
    s.add_argument(
        "host_name", metavar="NAME", help=f"the host's name ({remote.LOCAL!r} is this machine)"
    )
    s.add_argument("uri", metavar="URI", help="ssh://[USER@]HOST[:PORT]")
    s.set_defaults(library_needed=False)
    s = command(
        g,
        "remove",
        "forget a remote host",
        cmd_host_remove,
        usage="beanjar host remove NAME [--json]",
    )
    s.add_argument("host_name", metavar="NAME")
    s.set_defaults(library_needed=False)
    s = command(
        g,
        "show",
        "a remote host's beanjar and guest payload versions and commits, and what differs from "
        "this beanjar's (drift: each with the command that fixes it)",
        cmd_host_show,
        usage="beanjar host show NAME [--json]",
    )
    s.add_argument("host_name", metavar="NAME")
    s.set_defaults(library_needed=False)
    s = command(g, "list", "the remote hosts: name and URI", cmd_host_list)
    s.set_defaults(library_needed=False)
