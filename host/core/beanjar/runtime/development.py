# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Managed development VMs."""

import fcntl
import hashlib
import os
import re
import tempfile
from pathlib import Path

from .. import base, paths
from ..firmware import probe
from ..vm import config as vm_config
from ..vm import identity as vm_identity
from ..vm import library as vm_library
from ..vm import settings
from . import vmunits


class Error(Exception):
    pass


def locations(env, tree=paths.TREE):
    instance = env.get("INSTANCE") or "default"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", instance):
        raise Error("INSTANCE: lower-case letters, digits and hyphens, at most 40")
    repository = hashlib.sha256(os.path.realpath(tree.top).encode()).hexdigest()[:8]
    name = "dev-" + hashlib.sha256((repository + ":" + instance).encode()).hexdigest()[:8]
    base = (
        env.get("BEANJAR_LIBRARY")
        or os.environ.get("BEANJAR_LIBRARY")
        or os.path.join(
            env.get("XDG_DATA_HOME")
            or os.environ.get("XDG_DATA_HOME")
            or os.path.expanduser("~/.local/share"),
            "beanjar",
        )
    )
    library = os.path.abspath(os.path.join(base, "development-" + repository))
    return library, os.path.join(library, "vms", name)


def environment(env, tree=paths.TREE):
    """Resolve defaults without creating files; explicit runtime paths take precedence."""
    library, directory = locations(env, tree)
    out = dict(env)
    identity = env.get("IDENTITY_DIR") or os.path.join(directory, "identity")
    ca = (
        os.path.join(os.path.abspath(os.path.join(tree.top, identity)), "ca")
        if env.get("IDENTITY_DIR")
        else os.path.join(library, "ca")
    )
    defaults = dict(
        RUN_DIR=os.path.join(directory, "run"),
        LOG_DIR=os.path.join(directory, "logs"),
        SAVED_LOG_DIR=os.path.join(directory, "logs", "saved"),
        STATE=os.path.join(directory, "state"),
        IDENTITY_DIR=identity,
        NETSTUB_CA=ca,
        UNIT=vmunits.vm_unit_name(directory),
    )
    for key, value in defaults.items():
        out[key] = out.get(key) or value
    return out


def _config(env, tree):
    from ..runtime import qemu

    firmware, _ = tree.firmware(env.get("FW"))
    # vm.toml resolves a relative fw against the VM's folder (firmware.vm_firmware), not the caller's
    firmware = os.path.abspath(firmware)
    target = probe.firmware_target(firmware)
    from ..firmware import targets

    display = env.get("DISPLAY_BACKEND") or (
        "egl-headless" if (env.get("ACCEL") or qemu.default_accel()) == "kvm" else "none"
    )
    config = dict(
        fw=firmware,
        smp=4,
        link="nat",
        mode="mitm",
        display=display,
        audio="dbus" if display == "dbus" else "none",
        layers=["headless"]
        if targets.release(target).always_layers == targets.WEBOS_LAYERS
        else [],
    )
    mac = env.get("MAC")
    serial = env.get("SERIAL")
    if mac:
        config["mac"] = mac
    if serial:
        config["serial"] = serial
    for key, variable in settings.SETTINGS.items():
        # empty is "the launcher's default" (scripts/regress gives every setting a lane leaves alone empty)
        if key == "fw" or not env.get(variable):
            continue
        value = env[variable]
        config[key] = (
            int(value)
            if key in ("mem", "smp")
            else settings.as_layers(value)
            if key == "layers"
            else value
        )
    return config


def configured_environment(env, tree=paths.TREE):
    """Read existing development settings without creating a VM (for argv)."""
    library, directory = locations(env, tree)
    config = Path(directory) / "vm.toml"
    stored = settings.vm_launcher_env(str(config)) if config.is_file() else {}
    # the VM's slot, or the one it would be made with
    slot = stored.get("SLOT") or str(vm_identity.free_slot(library))
    return environment(
        dict(stored, **env)
        | {
            "SLOT": slot,
            "ADDRESS_PREFIX": base.addresses_of(library),
            "NET_LAN_PARENT": vm_identity.lan_parent_of(library) or "",
        },
        tree,
    )


def _checkout(library, tree):
    """A development library keeps the checkout it belongs to (its own library, not the shared one)."""
    lib = vm_library.Library(library)
    if lib.config.get("repo") != tree.top:
        lib.set_option("repo", tree.top)


def prepare(env, tree=paths.TREE):
    """Create/read a standard VM config."""
    library, directory = locations(env, tree)
    config = Path(directory) / "vm.toml"
    if not config.exists():
        cfg = _config(env, tree)
        _checkout(library, tree)
        with open(Path(library) / ".development.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not config.exists():
                # its slot, taken with the rename that makes it (vm.identity.ensure_slot)
                with (
                    vm_identity.slot_lock(library),
                    tempfile.TemporaryDirectory(prefix=".new-dev-", dir=library) as temporary,
                ):
                    cfg["slot"] = vm_identity.free_slot(library)
                    vm_config.write(str(Path(temporary) / "vm.toml"), cfg)
                    os.rename(temporary, directory)
    stored = settings.vm_launcher_env(str(config))
    if env.get("FW") and os.path.realpath(env["FW"]) != os.path.realpath(stored["FW"]):
        resolved, _ = tree.firmware(env["FW"])
        if os.path.realpath(resolved) != os.path.realpath(stored["FW"]):
            raise Error("development VM uses another firmware; select a different INSTANCE")
    out = dict(stored, **env)
    vm_identity.ensure_identity(directory, lambda _: None)
    # Persisted identity is supplied when the environment does not override it.
    stored = settings.vm_launcher_env(str(config))
    for key in ("MAC", "SERIAL"):
        out[key] = env.get(key) or stored.get(key, "")
    out["SLOT"] = stored["SLOT"]  # the VM's own, never the caller's
    out["ADDRESS_PREFIX"] = base.addresses_of(library)  # its library's, never the caller's
    out["NET_LAN_PARENT"] = vm_identity.lan_parent_of(library) or ""
    return environment(out, tree)
