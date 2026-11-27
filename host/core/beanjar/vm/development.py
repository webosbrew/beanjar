# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Managed development VMs and an offline, non-destructive checkout migration."""

import hashlib
import filecmp
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .. import base, paths
from . import library as vm_library, config as vm_config, identity as vm_identity, settings
from ..runtime import vmunits


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


def legacy(env, tree):
    instance = env.get("INSTANCE") or "default"
    run = tree.build() if instance == "default" else tree.build("instances", instance)
    return Path(tree.src("local")), Path(run), Path(run) / "state/default"


def _offline(run, directory):
    for path in (run / "launcher.pid", Path(directory) / "run/launcher.pid"):
        if base.proc_alive(base.read_pid(str(path))):
            raise Error("stop the development launcher before migration")
    for path in (run / "qemu.pid", Path(directory) / "run/qemu.pid"):
        if base.proc_alive(base.read_pid(str(path)), "qemu-system"):
            raise Error("stop the development VM before migration")
    from ..runtime import unit

    for props in vmunits.vm_units().values():
        if props.get("ActiveState") in unit.UP and props.get("Description") in (
            f"webOS VM {run}",
            f"webOS VM {directory}/run",
        ):
            raise Error("stop the development VM service before migration")


def _config(env, tree, preserve_identity=False):
    from ..runtime import qemu

    firmware, _ = tree.firmware(env.get("FW"))
    # vm.toml resolves a relative fw against the VM's folder (firmware.vm_firmware), not the caller's
    firmware = os.path.abspath(firmware)
    target = paths.firmware_target(firmware)
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
    # Preserve the effective legacy board identity rather than generating a different TV.
    mac = env.get("MAC") or (qemu.guest_mac(target, tree) if preserve_identity else None)
    serial = env.get("SERIAL")
    if not serial and preserve_identity:
        from ..firmware import targets

        text = Path(
            tree.src(targets.board_cmdline_src(target, targets.board_broadcast(target, firmware)))
        ).read_text()
        match = re.search(r"\bserialNum=([0-9A-Z]{1,16})(?:\s|$)", text)
        serial = match[1] if match else None
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
    stored = vm_library.vm_launcher_env(str(config)) if config.is_file() else {}
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
    """Create/read a standard VM config; never replace a legacy identity implicitly."""
    local, _, _ = legacy(env, tree)
    library, directory = locations(env, tree)
    config = Path(directory) / "vm.toml"
    if not config.exists():
        if (
            local.exists()
            and any(local.iterdir())
            and not any(Path(library).glob("vms/*/.migrated-local"))
        ):
            raise Error(
                "legacy local/ identity exists; run host/scripts/vm-launcher migrate-local first"
            )
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
    stored = vm_library.vm_launcher_env(str(config))
    if env.get("FW") and os.path.realpath(env["FW"]) != os.path.realpath(stored["FW"]):
        resolved, _ = tree.firmware(env["FW"])
        if os.path.realpath(resolved) != os.path.realpath(stored["FW"]):
            raise Error("development VM uses another firmware; select a different INSTANCE")
    out = dict(stored, **env)
    vm_identity.ensure_identity(directory, lambda _: None)
    # Persisted identity is supplied when the environment does not override it.
    stored = vm_library.vm_launcher_env(str(config))
    for key in ("MAC", "SERIAL"):
        out[key] = env.get(key) or stored.get(key, "")
    out["SLOT"] = stored["SLOT"]  # the VM's own, never the caller's
    out["ADDRESS_PREFIX"] = base.addresses_of(library)  # its library's, never the caller's
    out["NET_LAN_PARENT"] = vm_identity.lan_parent_of(library) or ""
    return environment(out, tree)


def migrate(env, tree=paths.TREE):
    """Copy keys, CA and sparse state offline. Originals and conflicting destinations survive."""
    source, run, state = legacy(env, tree)
    library, directory = locations(env, tree)
    target = Path(directory)
    ca = Path(library) / "ca"
    _offline(run, directory)
    if source.is_symlink() or not source.is_dir():
        raise Error("no legacy local/ directory to migrate")
    if target.exists():
        raise Error("development VM already exists; migration will not overwrite it")
    for path in source.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise Error("legacy identity contains a symlink or special file; migration refused")
    if state.is_symlink():
        raise Error("legacy state is a symlink; migration refused")
    if state.exists():
        for path in state.rglob("*"):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise Error("legacy state contains a symlink or special file; migration refused")
    source_ca = source / "ca"
    same_ca = lambda: all(
        path.is_dir()
        or (
            not (ca / path.relative_to(source_ca)).is_symlink()
            and (ca / path.relative_to(source_ca)).is_file()
            and filecmp.cmp(path, ca / path.relative_to(source_ca), shallow=False)
        )
        for path in source_ca.rglob("*")
        if path.name != ".lock"
    )
    if source_ca.exists() and ca.exists() and not same_ca():
        raise Error("development library CA already exists; migration will not overwrite it")
    cfg = _config(env, tree, preserve_identity=True)
    _checkout(library, tree)
    # Serialize competing preparations and migrations with the library's existing lock.
    with open(Path(library) / ".development.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _offline(run, directory)
        if target.exists() or source_ca.exists() and ca.exists() and not same_ca():
            raise Error("migration destination changed; nothing overwritten")
        new_ca = source_ca.exists() and not ca.exists()
        with (
            vm_identity.slot_lock(library),
            tempfile.TemporaryDirectory(prefix=".migration-", dir=library) as temporary,
        ):
            cfg["slot"] = vm_identity.free_slot(library)
            staged = Path(temporary) / "vm"
            (staged / "identity").mkdir(parents=True, mode=0o700)
            for path in source.iterdir():
                if path.name == "ca":
                    continue
                if not path.is_file():
                    raise Error("unexpected directory in legacy identity; migration refused")
                shutil.copy2(path, staged / "identity" / path.name)
            from . import sestore

            try:
                sestore.migrate(staged / "identity", merge=True)
            except sestore.Error as error:
                raise Error(str(error)) from None
            if state.exists():
                subprocess.run(
                    [
                        "cp",
                        "-a",
                        "--reflink=auto",
                        "--sparse=always",
                        str(state),
                        str(staged / "state"),
                    ],
                    check=True,
                )
            for name in ("state", "run", "logs"):
                (staged / name).mkdir(exist_ok=True)
            (staged / ".migrated-local").write_text("Legacy originals retained in the checkout.\n")
            vm_config.write(str(staged / "vm.toml"), cfg)
            vm_identity.ensure_identity(str(staged), lambda _: None)
            if new_ca:
                shutil.copytree(
                    source_ca, Path(temporary) / "ca", ignore=shutil.ignore_patterns(".lock")
                )
                os.rename(Path(temporary) / "ca", ca)
            try:
                os.rename(staged, target)
            except BaseException:
                if new_ca:
                    shutil.rmtree(ca)
                raise
    return directory
