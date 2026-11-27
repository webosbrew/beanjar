# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""This machine's settings, every library's (config.toml in the user's config
directory): max_running, firmware_dirs, memory_headroom_mib, runner."""

import os

from .. import paths
from ..base import Error, read_toml
from .. import tomlfile


# The machine's settings, every library's alike (the development library's, the regress harness's):
# $XDG_CONFIG_HOME/beanjar/config.toml (docs/dev/architecture/manager.md, "Several VMs at once").
# max_running: how many VMs may run at once on this host; unset, no limit. firmware_dirs: the host's
# firmware collections (paths.firmware_dirs), where create --firmware NAME and the launcher look a
# firmware up; unset, /srv/beanjar/firmware. memory_headroom_mib: the memory reserved beyond each
# VM's RAM, shared by admission and its KVM unit's soft memory limit; unset, 1024 MiB. runner: how VMs
# run here (runner.py: systemd or session); unset or auto, systemd where a user systemd runs.
GLOBAL_KEYS = ("max_running", "firmware_dirs", "memory_headroom_mib", "runner")


GLOBAL_HEADER = "Beanjar's settings for this machine, every library's (beanjar host config)."


def global_config_dir():
    return paths.global_config_dir()


def global_config_path():
    return paths.global_config_path()


def parse_max_running(v):
    """max_running: a whole number of VMs, 1 or more; None for empty (no limit)."""
    s = str(v).strip()
    if s == "":
        return None
    if isinstance(v, bool) or not s.isdigit() or int(s) < 1:
        raise Error(
            f"max_running is how many VMs may run at once, a whole number from 1 "
            f"(beanjar host config unset max_running removes the limit), not {s}",
            "max_running",
        )
    return int(s)


def parse_memory_headroom_mib(v):
    """A positive whole number of MiB; empty restores the default."""
    s = str(v).strip()
    if s == "":
        return None
    if isinstance(v, bool) or not s.isdigit() or int(s) < 1:
        raise Error("memory_headroom_mib is a whole number of MiB from 1", "memory_headroom_mib")
    return int(s)


def global_config():
    """The machine's settings as they are set (only the set ones)."""
    cfg = read_toml(global_config_path(), {})
    if "max_running" in cfg:
        try:
            cfg["max_running"] = parse_max_running(cfg["max_running"])
        except Error as e:
            raise Error(f"{global_config_path()}: {e}") from None
    if "firmware_dirs" in cfg:
        try:
            cfg["firmware_dirs"] = paths.parse_firmware_dirs(cfg["firmware_dirs"])
        except ValueError as e:
            raise Error(f"{global_config_path()}: {e}") from None
    if "memory_headroom_mib" in cfg:
        try:
            cfg["memory_headroom_mib"] = parse_memory_headroom_mib(cfg["memory_headroom_mib"])
        except Error as e:
            raise Error(f"{global_config_path()}: {e}") from None
    if "runner" in cfg:
        from . import runner

        try:
            cfg["runner"] = runner.parse(cfg["runner"])
        except Error as e:
            raise Error(f"{global_config_path()}: {e}") from None
    return cfg


def set_global_option(key, value):
    """One setting of the machine's config.toml (the CLI's host config); empty unsets it."""
    if key not in GLOBAL_KEYS:
        raise Error(f"unknown setting {key} for this machine (one of {', '.join(GLOBAL_KEYS)})")
    if key == "max_running":
        value = parse_max_running(value)
    elif key == "memory_headroom_mib":
        value = parse_memory_headroom_mib(value)
    elif key == "runner":
        from . import runner

        value = runner.parse(value)
    else:
        try:
            value = paths.parse_firmware_dirs(value) or None
        except ValueError as e:
            raise Error(str(e), key) from None
    path = global_config_path()
    cfg = read_toml(path, {})
    if value is None:
        cfg.pop(key, None)
    else:
        cfg[key] = value
    write_global_config(cfg)


def write_global_config(cfg):
    """The machine's config.toml from cfg: its settings, then one table per remote host
    ([hosts.NAME], remote.py), which the settings commands keep."""
    path = global_config_path()
    tables = {k: v for k, v in cfg.items() if isinstance(v, dict)}
    flat = {k: v for k, v in cfg.items() if k not in tables}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tail = ""
    for name, table in tables.get("hosts", {}).items():
        tail += f"\n[hosts.{tomlfile.toml_key(name)}]\n"
        tail += "".join(
            f"{tomlfile.toml_key(k)} = {tomlfile.toml_value(v)}\n" for k, v in table.items()
        )
    tomlfile.write_toml(path, flat, GLOBAL_HEADER, GLOBAL_KEYS, tail)


def max_running():
    return global_config().get("max_running")
