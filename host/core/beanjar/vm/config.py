# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The grouped VM document; internal callers keep their existing setting names.

Only this reader accepts user vm.toml files. Legacy files require the explicit,
backed-up migration; normal reads never translate an old file implicitly.
"""

import os
import re
import tomllib
from collections.abc import Mapping

FIELDS = {
    "fw": ("firmware", "directory"),
    "broadcast": ("firmware", "broadcast"),
    "model": ("firmware", "model"),
    "country": ("firmware", "country"),
    "debug_mode": ("firmware", "debug_mode"),
    "updatable": ("firmware", "updatable"),
    "mem": ("hardware", "memory_mib"),
    "smp": ("hardware", "cpus"),
    "free_page_reporting": ("hardware", "free_page_reporting"),
    "userland": ("hardware", "userland"),
    "guest_kernel": ("hardware", "guest_kernel"),
    "hwdec": ("hardware", "hwdec"),
    "display": ("display", "backend"),
    "gpu": ("display", "gpu"),
    "audio": ("audio", "backend"),
    "link": ("network", "link"),
    "mode": ("network", "mode"),
    "listen": ("network", "listen"),
    "forward": ("network", "forward"),
    "address": ("network", "address"),
    "lan_access": ("network", "lan_access"),
    "lg_services": ("network", "lg_services"),
    "slot": ("network", "slot"),
    "mitm_flows": ("network", "mitm_flows"),
    "mitm_flow_bytes": ("network", "mitm_flow_bytes"),
    "layers": ("layers", "enabled"),
    "mac": ("identity", "mac"),
    "serial": ("identity", "serial"),
    "created": ("metadata", "created"),
    "tags": ("metadata", "tags"),
}
SECTIONS = (
    "firmware",
    "hardware",
    "display",
    "audio",
    "network",
    "layers",
    "identity",
    "advanced",
    "metadata",
)
DEFAULTS = {"smp": 4, "display": "dbus", "link": "nat", "mode": "mitm", "layers": ["normal", "gl"]}
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


def _fail(message, key=None):
    from ..base import Error

    raise Error(message, key)


def _load(path):
    try:
        with open(path, "rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _fail(f"{path}: {exc}")


def _check_tables(doc):
    """Error unless doc's tables and their keys are vm.toml's."""
    if not isinstance(doc, Mapping):
        _fail("vm.toml must contain tables")
    for section, value in doc.items():
        if section not in SECTIONS:
            _fail(f"vm.toml: unknown section {section!r}")
        if not isinstance(value, Mapping):
            _fail(f"vm.toml: {section} must be a table")
        allowed = {field for group, field in FIELDS.values() if group == section}
        if section == "advanced":
            allowed = {"env"}
        for field in value:
            if field not in allowed:
                _fail(f"vm.toml: unknown setting {section}.{field}")


def decode(doc):
    """Validate the external schema and return the manager's internal settings."""
    if hasattr(doc, "unwrap"):
        doc = doc.unwrap()
    _check_tables(doc)
    network = doc.get("network", {})
    if "mode" in network and "link" not in network and network["mode"] == "off":
        _fail(
            'vm.toml: network.mode = "off" is not supported; use network.link = "off"',
            "mode",
        )
    directory = doc.get("firmware", {}).get("directory")
    if not isinstance(directory, str) or not directory.strip():
        _fail("firmware.directory must name the firmware directory", "fw")
    cfg = dict(DEFAULTS, layers=list(DEFAULTS["layers"]))
    for internal, (section, field) in FIELDS.items():
        if field in doc.get(section, {}):
            cfg[internal] = doc[section][field]
    if os.path.normpath(directory) == "firmware":
        cfg.pop("fw", None)
    display = cfg["display"]
    cfg.setdefault("audio", "dbus" if display == "dbus" else "none")
    for key in (
        "free_page_reporting",
        "userland",
        "guest_kernel",
        "hwdec",
        "debug_mode",
        "updatable",
        "display",
        "gpu",
        "audio",
        "link",
        "mode",
        "mac",
        "serial",
        "listen",
        "forward",
        "address",
        "lan_access",
        "lg_services",
        "mitm_flow_bytes",
        "created",
        "broadcast",
        "model",
        "country",
    ):
        if key in cfg and not isinstance(cfg[key], str):
            section, field = FIELDS[key]
            _fail(f"{section}.{field} must be a string", key)
    from ..firmware.targets import BROADCASTS

    if "broadcast" in cfg and cfg["broadcast"] not in BROADCASTS:
        _fail(
            f"firmware.broadcast {cfg['broadcast']!r}: one of {', '.join(BROADCASTS)}", "broadcast"
        )
    if "country" in cfg:
        from ..board.countries import CODE

        if not CODE.fullmatch(cfg["country"]):
            _fail(
                f"firmware.country {cfg['country']!r}: an ISO 3166 alpha-3 code in upper case "
                "(USA, GBR)",
                "country",
            )
    env = doc.get("advanced", {}).get("env", {})
    if not isinstance(env, Mapping):
        _fail("advanced.env must be a table")
    from .settings import CHECKOUT_ONLY, LAUNCHER_OWN, SETTINGS, validate

    for key, value in env.items():
        if not isinstance(key, str) or not ENV_NAME.fullmatch(key):
            _fail(f"advanced.env: invalid environment name {key!r}")
        if key in LAUNCHER_OWN or key in SETTINGS.values() or key in CHECKOUT_ONLY:
            _fail(f"advanced.env.{key} is owned by the launcher")
        if isinstance(value, bool) or not (
            isinstance(value, (str, int, float))
            or isinstance(value, list)
            and all(isinstance(item, str) for item in value)
        ):
            _fail(f"advanced.env.{key} must be a string, number or array of strings")
        cfg[key] = value
    try:
        validate(cfg, saved=True)
    except Exception as exc:
        from ..base import Error

        if not isinstance(exc, Error):
            raise
        if exc.key in FIELDS:
            section, field = FIELDS[exc.key]
            _fail(f"{section}.{field}: {exc}", exc.key)
        raise
    return cfg


def peek(path, names):
    """{name: value} of the settings names of the vm.toml at path as read() gives them, defaults
    applied (advanced.env's entries by their names), without read()'s checks, which need the
    library module (library.py): for a query that reads a few settings and must start quickly (the
    `_ares` answer, cli.ares_query). A setting the file does not give and that has no default is
    left out. None if there is no file, or if its tables are not vm.toml's (read() tells why); a
    file that is not TOML is read()'s Error."""
    from ..base import Error

    doc = _load(path)
    if doc is None:
        return None
    try:
        _check_tables(doc)
    except Error:
        return None
    out = {}
    for name in names:
        if name in FIELDS:
            section, field = FIELDS[name]
            table = doc.get(section, {})
        else:
            table, field = doc.get("advanced", {}).get("env", {}), name
        if isinstance(table, Mapping) and field in table:
            out[name] = table[field]
        elif name in DEFAULTS:
            out[name] = list(DEFAULTS[name]) if name == "layers" else DEFAULTS[name]
    return out


def read(path, default=None, check_broadcast=True):
    """vm.toml's settings. check_broadcast False leaves out the check of firmware.broadcast and
    firmware.model against the firmware (check_firmware_broadcast), so a VM holding a bad value can
    still be read and fixed."""
    doc = _load(path)
    if doc is None:
        return default
    try:
        cfg = decode(doc)
        if (cfg.get("broadcast") or cfg.get("model")) and check_broadcast:
            _check_broadcast(path, cfg)
        return cfg
    except Exception as exc:
        from ..base import Error

        if isinstance(exc, Error):
            _fail(f"{path}: {exc}", exc.key)
        raise


def _check_broadcast(path, cfg):
    from ..firmware.install import vm_firmware

    fw = vm_firmware(os.path.dirname(os.path.abspath(path)), cfg.get("fw"))[0]
    check_firmware_broadcast(fw, cfg.get("broadcast"), cfg.get("model"))


def check_firmware_broadcast(fw, broadcast, model=None):
    """firmware.broadcast and firmware.model against the firmware directory fw
    (targets.board_broadcast, targets.board_model): a standard its chip has a board for, a retail
    model its chip has a board of (Soc.model_boards) of that standard, and the firmware's own
    standard where it names one (its model_id.json). A firmware whose target its manifest does not
    tell is left to the launcher, which checks the same."""
    from ..firmware import targets
    from .. import paths

    if not os.path.isfile(os.path.join(fw, paths.MANIFEST)):
        return  # telling the target would read the rootfs: every read of vm.toml must stay cheap
    try:
        target = paths.firmware_target(fw)
    except (ValueError, OSError):
        return
    if model:
        try:
            targets.board_model(target, fw, broadcast, model)
        except targets.Error as e:
            _fail(f"firmware.model: {e}", "model")
    try:
        targets.board_broadcast(target, fw, broadcast, model)
    except targets.Error as e:
        _fail(f"firmware.broadcast: {e}", "broadcast")


def _editor():
    try:
        import tomlkit

        return tomlkit
    except ImportError:
        _fail("Editing VM configuration requires python3-tomlkit")


def _parse(tk, text, path):
    try:
        return tk.parse(text)
    except ValueError as exc:
        _fail(f"{path}: {exc}")


def _atomic(path, text):
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "w", newline="") as stream:
            stream.write(text)
        if os.path.exists(path):
            import shutil

            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def write(path, cfg, header=None):
    """Edit values in place in the syntax tree, preserving unrelated user text."""
    tk = _editor()
    try:
        with open(path, newline="") as stream:
            original = stream.read()
    except FileNotFoundError:
        original = None
    except OSError as exc:
        _fail(f"{path}: {exc}")
    doc = _parse(tk, original, path) if original is not None else tk.document()
    if original is not None:
        decode(doc)
    elif header:
        for line in header.splitlines():
            doc.add(tk.comment(line))
        doc.add(tk.nl())
    for key in cfg:
        if key not in FIELDS and key != "platform" and not ENV_NAME.fullmatch(key):
            _fail(f"unknown VM setting {key!r}")
    for section in SECTIONS:
        for internal, (group, field) in FIELDS.items():
            if group != section:
                continue
            current = doc.get(section, {})
            if internal == "fw":
                value = cfg.get("fw") or "firmware"
            elif internal in cfg:
                value = cfg[internal]
                # A no-op edit must not expand omitted defaults into the document.
                default = DEFAULTS.get(internal)
                if internal == "audio":
                    default = "dbus" if cfg.get("display", "dbus") == "dbus" else "none"
                if original is not None and field not in current and value == default:
                    continue
            else:
                if field in current:
                    del doc[section][field]
                continue
            if field in current and current[field] == value:
                continue
            if section not in doc:
                doc.add(section, tk.table())
            doc[section][field] = value
    env = {key: value for key, value in cfg.items() if ENV_NAME.fullmatch(key)}
    old_env = doc.get("advanced", {}).get("env", {})
    if env or old_env:
        if "advanced" not in doc:
            doc.add("advanced", tk.table())
        if "env" not in doc["advanced"]:
            doc["advanced"]["env"] = tk.table()
        target = doc["advanced"]["env"]
        for key in list(target):
            if key not in env:
                del target[key]
        for key, value in env.items():
            if key not in target or target[key] != value:
                target[key] = value
    decode(doc)  # Never replace a good file with an invalid document.
    text = tk.dumps(doc)
    if text != original:
        _atomic(path, text)
