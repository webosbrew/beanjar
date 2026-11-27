# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""This machine's settings, every library's (config.toml in the user's config
directory): max_running, firmware_dirs, memory_headroom_mib, runner."""

import os
import tomllib

from .. import tomlfile
from ..base import Error, read_toml

# The machine's settings, every library's alike (the development library's, the regress harness's):
# $XDG_CONFIG_HOME/beanjar/config.toml (docs/dev/architecture/manager.md, "Several VMs at once").
# max_running: how many VMs may run at once on this host; unset, no limit. firmware_dirs: the host's
# firmware collections (firmware_dirs), where create --firmware NAME and the launcher look a
# firmware up; unset, /srv/beanjar/firmware. memory_headroom_mib: the memory reserved beyond each
# VM's RAM, shared by admission and its KVM unit's soft memory limit; unset, 1024 MiB. runner: how VMs
# run here (runner.py: systemd or session); unset or auto, systemd where a user systemd runs.
GLOBAL_KEYS = ("max_running", "firmware_dirs", "memory_headroom_mib", "runner")


GLOBAL_HEADER = "Beanjar's settings for this machine, every library's (beanjar host config)."


def global_config_dir():
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "beanjar"
    )


def global_config_path():
    return os.path.join(global_config_dir(), "config.toml")


# firmware_dirs: the host's firmware collections, where a bare firmware name is looked up, in order;
# each firmware a directory <collection>/<name> of its paks (and, for create --firmware, a
# manifest.toml).
DEFAULT_FIRMWARE_DIRS = ("/srv/beanjar/firmware",)


def parse_firmware_dirs(v):
    """firmware_dirs as a list of absolute directories: a list as it is, a string split at ':'
    (the CLI's host config set firmware_dirs=A:B); ~ expanded. ValueError for anything else, or
    for a relative directory."""
    items = v.split(":") if isinstance(v, str) else v
    if not isinstance(items, (list, tuple)) or not all(isinstance(d, str) for d in items):
        raise ValueError("firmware_dirs is a list of directories")
    out = [os.path.normpath(os.path.expanduser(d.strip())) for d in items if d.strip()]
    bad = [d for d in out if not os.path.isabs(d)]
    if bad:
        raise ValueError(f"firmware_dirs: not an absolute directory: {', '.join(bad)}")
    return out


def firmware_dirs():
    """The host's firmware collections, in order: config.toml's firmware_dirs, else
    DEFAULT_FIRMWARE_DIRS. ValueError for a config.toml that cannot be read or a bad value."""
    path = global_config_path()
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from None
    if "firmware_dirs" not in cfg:
        return list(DEFAULT_FIRMWARE_DIRS)
    try:
        return parse_firmware_dirs(cfg["firmware_dirs"])
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None


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
            cfg["firmware_dirs"] = parse_firmware_dirs(cfg["firmware_dirs"])
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
            value = parse_firmware_dirs(value) or None
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
