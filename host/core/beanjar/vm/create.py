# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A new VM and the firmware it boots: the one
way a VM is made, by the CLI's create, the GUI's New VM and Library.create.

The firmware (check()) is, by default, a firmware used in place (kind "place"): vm.toml's
[firmware] directory names it (absolute), it is never written, and what the launcher derives from it
(the partition map, the uncompressed images' stamps) goes into the VM's firmware-cache/
(firmware.vm_firmware). Its board data is worked out before it is published (prepare.py: the partition
map, the new EEPROM chip, the local key, the chip modules' parameters), so its first start only
reads it. It is a name in the host's firmware collections (paths.firmware_dirs: the
machine's config.toml, firmware_dirs) or a path (any value with a '/'), and needs a manifest.toml
with its name, chip and release, so nothing is read from its rootfs to tell its target.
Imported (--import), the firmware becomes the VM's own, in its folder (firmware.OWN, <vm>/firmware),
from one of:
- a directory of extracted paks (kind "dir"): its paks hard-linked (the same filesystem; nothing
  writes a pak) or else copied;
- a .epk (kind "epk": --firmware names a file; always imported, --import or not), extracted now
  (firmware.extract) in the scratch: the target's paks (targets.pak_files) are moved into the VM's
  firmware/ and everything else epk2extract wrote (the other paks, kernel.pak's unpacked
  intermediates) is deleted with the scratch before the VM is published.
The firmware is resolved to its target (chip, release) first (firmware.target_of) and refused if it is
not one this tree boots. manifest.toml records the target and where the paks came from;
the platform is derived from that target.
Every new VM is a TV of its own: a MAC (the library's mac_prefix, three random octets) and a
serial number, random, that no other VM of the library has (core.Library.new_identity), and SSH keys
of its own in identity/ (keys.ensure; root's authorized_keys with no key of the user's), and a
slot, its host address (vm.identity.ensure_slot), taken with the rename that publishes it. The
library's CA for the mode=mitm proxy, <library>/ca, which its VMs share, is made with its first VM
(keys.ensure_ca).

Everything is made in a scratch directory of the library (vms/.new-<name>-*) and published by a
rename at the end, with SIGINT, SIGTERM and SIGHUP held back for it. Any failure or cancellation
(Ctrl-C, SIGTERM or SIGHUP: the GUI's Cancel) kills epk2extract and removes the scratch: nothing is left behind, and nothing is written outside it.

python3 -m beanjar.vm.create is the GUI's New VM as a process of its own (gui.Operation, so that Cancel
is a SIGTERM): the CLI's create's options. It is not a command for users. A refusal ends with
"beanjar: <message>" and, if it is about one setting, "beanjar-key: <vm.toml key>" (the dialog's
page to show).
"""

import argparse
import os
import shutil
import signal
import sys
import tempfile
import threading
import time

from ..runtime import capacity
from . import keys, config, identity
from .. import paths
from ..guestside import compat
from ..firmware import install, targets
from ..board import nvm
from .library import VM, Library, prepare_board_data
from ..base import NAME_RE, Error
from ..firmware.install import vm_firmware
from ..tomlfile import MANIFEST_HEADER, VM_HEADER, write_toml
from .identity import free_slot, make_identity_dir, slot_lock
from .settings import MANIFEST_KEYS

SIGNALS = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


def check_name(lib, name):
    """A new VM's name: valid, and no VM's yet (refused first: it is the dialog's first row)."""
    if not NAME_RE.fullmatch(name or ""):
        raise Error(
            f"invalid VM name {name!r} (letters, digits, '.', '_', '-'; at most 64)", "name"
        )
    if os.path.exists(VM(lib, name).dir):
        raise Error(f"VM {name} exists", "name")


def resolve(fw):
    """The path of --firmware's value fw: a path (any value with a '/', or ending in .epk) as given,
    made absolute, or a name in the host's firmware collections (paths.firmware_dirs), the first
    that has it."""
    v = os.path.expanduser(str(fw).strip())
    if "/" in v or v.endswith(".epk"):
        return os.path.abspath(v)
    try:
        dirs = paths.firmware_dirs()
    except ValueError as e:
        raise Error(str(e), "fw") from None
    found = paths.find_firmware(v, dirs) if v not in ("", ".", "..") else None
    if not found:
        raise Error(
            f"no firmware {v} in the host's firmware collections ({':'.join(dirs) or 'none'}; "
            "beanjar host config set firmware_dirs=DIR[:DIR...]); a directory is given as a path",
            "fw",
        )
    return found


def collection():
    """The firmwares of the host's collections (paths.firmware_dirs) that can be used in place:
    [(name, directory)], each with a manifest.toml and a root pak, by name; a name in two
    collections is the first one's."""
    try:
        dirs = paths.firmware_dirs()
    except ValueError:
        return []
    found = {}
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            fw = os.path.join(d, n)
            if (
                n not in found
                and os.path.isfile(os.path.join(fw, paths.MANIFEST))
                and paths.root_pak(fw)
            ):
                found[n] = fw
    return sorted(found.items())


def check_in_place(fw_dir):
    """The refusals of a firmware used in place: its target (chip, release), from its manifest.toml."""
    if not os.path.isdir(fw_dir):
        raise Error(f"firmware {fw_dir}: no such directory", "fw")
    if not paths.root_pak(fw_dir):
        if logical_root(fw_dir):
            raise Error(
                f"firmware {fw_dir}: its rootfs is mapped in super.pak, which a VM cannot use in "
                "place: give --import",
                "fw",
            )
        raise Error(
            f"firmware {fw_dir}: no rootfs.pak or root.pak (a directory of extracted paks)", "fw"
        )
    m = install.manifest(fw_dir)
    if not m:
        raise Error(
            f"firmware {fw_dir}: no manifest.toml (name, chip, release), which a firmware used in "
            "place needs; add one, or give --import",
            "fw",
        )
    if not isinstance(m.get("name"), str) or not m["name"]:
        raise Error(f"firmware {fw_dir}: its manifest.toml names no firmware (name)", "fw")
    try:
        target = paths.manifest_target(m)
    except ValueError as e:
        raise Error(f"firmware {fw_dir}: manifest.toml: {e}", "fw") from None
    install.check_paks(fw_dir, target)
    return target


def logical_root(d):
    try:
        return install.has_root(d)
    except Error:
        return False


def is_epk(path):
    """Whether --firmware's resolved path is a firmware file to extract: a file, or a name ending
    in .epk (refused later if there is no such file), not a directory of paks."""
    return os.path.isfile(path) or (path.endswith(".epk") and not os.path.isdir(path))


def check(lib, name, fw=None, place=True, **settings):
    """The refusals of create, before anything is made (the dialog runs it first): a dict of
    kind ("place": used in place; "dir": imported; "epk": a .epk, always imported), fw_dir (the
    directory's absolute path; None for a .epk), epk (its absolute path) and cfg (vm.toml's
    settings; fw for "place"). Raises an Error naming its key."""
    check_name(lib, name)
    cfg = lib.vm_config(**settings)
    if not fw:
        raise Error(
            "give --firmware, a firmware's name in the host's collections, its directory or a .epk",
            "fw",
        )
    path, epk = resolve(fw), None
    if is_epk(path):
        if not os.path.isfile(path):
            raise Error(f"no file {path}", "fw")
        if not install.find_epk2extract(lib):
            raise Error("epk2extract not found (PATH, ~/.local/bin, or BEANJAR_EPK2EXTRACT)", "fw")
        kind, fw_dir, epk = "epk", None, path
    elif place:
        kind, fw_dir = "place", path
        check_in_place(fw_dir)
        cfg["fw"] = fw_dir
    else:
        kind, fw_dir = "dir", path
        if not install.has_root(fw_dir):
            raise Error(
                f"firmware {fw_dir}: no rootfs.pak, root.pak or mapped rootfs (a directory of extracted paks)",
                "fw",
            )
        install.target_of(fw_dir)
    # the test guest (targets platform "Test") takes none of the webOS layers by default
    if (
        settings.get("layers") is None
        and fw_dir
        and targets.platform(install.target_of(fw_dir)) == "Test"
    ):
        cfg["layers"] = []
    cfg["mac"], cfg["serial"] = lib.new_identity()  # the TV's own
    return {"kind": kind, "fw_dir": fw_dir, "epk": epk, "cfg": cfg}


def slow(lib, plan):
    """Whether create(plan) takes more than a moment (the GUI shows its progress then): always, as
    preparing the board data runs the firmware's own programs; an extraction or a copy (another
    filesystem) take longer still."""
    return True  # every kind: its board data is prepared (prepare.py), which runs firmware code


def space(lib, fw=None, vm_dir=None, place=False):
    """What the choice of firmware costs, for the dialogs: {"free": bytes free in the library,
    "epk": the .epk's size (its paks take about as much), "paks": the paks' size, "copy": what
    they take in the new VM's folder (0: hard links, or used in place)}; what is not known is
    missing. fw: New VM's firmware as chosen (resolve; a .epk too), or (vm_dir: Settings of that
    VM) vm.toml's fw."""
    out = {"free": shutil.disk_usage(lib.root).free}
    if vm_dir:
        d, _cache = vm_firmware(vm_dir, fw)
    elif fw:
        try:
            d = resolve(fw)
        except Error:
            return out
        if is_epk(d):
            if os.path.isfile(d):
                out["epk"] = os.path.getsize(d)
            return out
    else:
        return out
    if not os.path.isdir(d) or not install.has_root(d):
        return out
    try:
        target = install.target_of(d)
    except Error:
        return out
    out["paks"] = install.paks_size(d, target)
    if not vm_dir:
        out["copy"] = 0 if place else install.copy_size(d, target, lib.vms_dir)
    return out


def _hold(on):
    """Hold SIGINT, SIGTERM and SIGHUP back (on), or let them through again, dropping one that
    came meanwhile: the work it would have cancelled is done, or being undone, by then."""
    if threading.current_thread() is not threading.main_thread():
        return
    if on:
        signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        return
    handlers = {s: signal.signal(s, signal.SIG_IGN) for s in SIGNALS}
    signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)
    for s, h in handlers.items():
        signal.signal(s, h)


def cap_memory(lib, tree, target, fw_dir, cfg, out):
    """Keeps a VM's default RAM within the host (this one: create is local). With no mem in cfg the
    VM has its firmware's model's RAM (nvm.board_ram); if that is more than the host's MemTotal less
    what admission would accept with nothing else running (capacity.host_ram_cap_mib), cfg["mem"] is set to that cap, and out says so.
    A mem the user gave is never changed, only warned about."""
    cap = capacity.host_ram_cap_mib(lib)
    if cap is None:
        return
    given = cfg.get("mem")
    if given:
        if given > cap:
            out(f"warning: --mem {given} MiB is more than this host can give ({cap} MiB)")
        return
    try:
        model = cfg.get("model")
        broadcast = targets.board_broadcast(target, fw_dir, cfg.get("broadcast"), model)
        default = nvm.board_ram(target, tree, broadcast, model)
    except (targets.Error, OSError):
        return
    if default and default > cap:
        cfg["mem"] = cap
        out(
            f"warning: the firmware's model has {default} MiB of RAM, more than this host can give: "
            f"the VM gets {cap} MiB (what the host has free, less {capacity.memory_headroom_mib()} MiB headroom; "
            "beanjar config set NAME mem= changes it)"
        )


def warn_untested(target, out):
    """A warning, not a refusal, for a target that has never booted (targets.untested_warning)."""
    warning = targets.untested_warning(target)
    if warning:
        out(f"warning: {warning}")


def create(lib, name, fw=None, place=True, out=None, **settings):
    """The new VM (a core.VM): fw used in place (place) or imported, or, a .epk, extracted into it.
    out(line) gets the progress, line by line (default: stdout)."""
    out = out or (lambda line: print(line, flush=True))
    plan = check(lib, name, fw=fw, place=place, **settings)
    cfg, kind, fw_dir = plan["cfg"], plan["kind"], plan["fw_dir"]
    # the guest has the components the target needs, before anything is made (compat.py; an .epk's
    # target is known after its extraction)
    tree = paths.Tree(lib.repo)
    target = install.target_of(fw_dir) if kind in ("dir", "place") else None
    if target:
        try:
            compat.components(tree, target, name)
        except compat.Error as e:
            raise Error(str(e)) from None
        warn_untested(target, out)
    vm = VM(lib, name)
    scratch = None
    old = {}

    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    try:
        for s in (signal.SIGTERM, signal.SIGHUP):
            try:
                old[s] = signal.signal(s, interrupted)
            except ValueError:  # not the main thread: the caller handles cancellation
                pass
        scratch = tempfile.mkdtemp(prefix=f".new-{name}-", dir=lib.vms_dir)
        own = os.path.join(scratch, install.OWN)
        if kind == "epk":
            epk = plan["epk"]
            out(f"import: {epk}")
            out("hashing the .epk")
            digest = install.sha256_file(epk, out)
            work = os.path.join(scratch, ".import")
            os.mkdir(work)
            src, fw_name = install.extract(lib, epk, work, out)
            if not fw_name or fw_name.startswith("."):
                raise Error("could not tell the firmware's name from epk2extract's output", "fw")
            target = install.target_of(src, fw_name)
            install.check_paks(src, target)
            try:
                compat.components(tree, target, name)
            except compat.Error as e:
                raise Error(str(e)) from None
            out(f"firmware target: {target[0]}-{target[1]}")
            warn_untested(target, out)
            os.mkdir(own)
            # before the paks move out of src: the certificates are read from its kernel.pak
            certs = install.place_kernel_certs(src, own, target, "move", out)
            install.place(src, own, target, "move", out)
            shutil.rmtree(work)
            m = {
                "name": fw_name,
                "chip": target[0],
                "release": target[1],
                "epk": os.path.basename(epk),
                "epk_size": os.path.getsize(epk),
                "sha256": digest,
                "paks": install.paks_in(own, target),
                "kernel_certs": certs,
                "imported": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            m = {k: v for k, v in m.items() if v is not None}
            write_toml(os.path.join(own, "manifest.toml"), m, MANIFEST_HEADER, MANIFEST_KEYS)
            out(f"extracted {fw_name} into the VM's folder")
        elif kind == "place":
            out(f"the firmware used in place: {fw_dir}")
        else:
            os.mkdir(own)
            out(f"the firmware into the VM's folder: {os.path.join(vm.dir, install.OWN)}")
            install.place(fw_dir, own, target, "link", out)
            certs = install.place_kernel_certs(fw_dir, own, target, "link", out)
            m = install.manifest(fw_dir) or {"name": os.path.basename(fw_dir.rstrip("/"))}
            m = {
                k: v
                for k, v in dict(
                    m,
                    chip=target[0],
                    release=target[1],
                    source=fw_dir,
                    paks=install.paks_in(own, target),
                    kernel_certs=certs,
                ).items()
                if v is not None
            }
            m.setdefault("imported", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            write_toml(os.path.join(own, "manifest.toml"), m, MANIFEST_HEADER, MANIFEST_KEYS)
        cap_memory(lib, tree, target, fw_dir if kind == "place" else own, cfg, out)
        cfg["created"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        config.write(os.path.join(scratch, "vm.toml"), cfg, VM_HEADER)
        try:
            keys.ensure(make_identity_dir(scratch), label=name)  # its SSH keys (beanjar/vm/keys.py)
            keys.ensure_tee_huk(make_identity_dir(scratch))  # its TEE's key, made here
            identity.ensure_lgudid(make_identity_dir(scratch), cfg["mac"])  # its LGUDID
            # the library's CA (shared by its VMs), made with the first VM; again if it was lost
            keys.ensure_ca(os.path.join(lib.root, keys.CA), warn=out if lib.names() else None)
        except keys.Error as e:
            raise Error(str(e)) from None
        # what its start needs from the firmware and the board, worked out now (prepare.py):
        # the partition map, the new EEPROM chip, the local key and the modules' parameters
        prepare_board_data(lib, scratch, out, force=True)
        _hold(True)
        # its slot (vm.identity.ensure_slot): the lowest free one, taken under the slot domain's lock
        # with the rename that makes the VM, so two made at once never share one
        with slot_lock(lib.root):
            cfg["slot"] = free_slot(lib.root)
            config.write(os.path.join(scratch, "vm.toml"), cfg, VM_HEADER)
            keys.known_hosts(make_identity_dir(scratch), keys.address(cfg["slot"], lib.addresses))
            if os.path.exists(vm.dir):
                raise Error(f"VM {name} exists", "name")
            os.rename(scratch, vm.dir)
        scratch = None
        out(f"created {name}")
        return vm
    except BaseException as e:
        _hold(True)
        if isinstance(e, KeyboardInterrupt):
            out("cancelled: nothing was created")
        raise
    finally:
        _hold(True)
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
        for s, h in old.items():
            signal.signal(s, h)
        _hold(False)


def main(argv=None):
    from ..cli.settings import settings_of
    from ..cli.vms import create_arguments

    p = argparse.ArgumentParser(
        prog="beanjar.vm.create", description="the GUI's New VM (not a user command)"
    )
    p.add_argument("--library")
    p.add_argument("--repo")
    create_arguments(p)
    a = p.parse_args(argv)
    try:
        lib = Library(a.library, a.repo)
        create(lib, a.name, fw=a.firmware, place=not a.import_, **settings_of(a))
    except Error as e:
        print(f"beanjar: {e}", flush=True)
        if e.key:
            print(f"beanjar-key: {e.key}", flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
