# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What the guest sees of the host: a per-VM runtime tree, RUN_DIR/share, and nothing else.
The checkout, local/, identity/, the library's CA
and the firmware are not shared; the files the guest needs are linked (or copied) into the tree.

  RUN_DIR/share/vm/    9p "vm", read-only: the per-VM layer (qemu.guest_layer), mounted by the
                       initramfs at /run/vm only, not bound into webOS
  RUN_DIR/share/host/  9p "host", read-only: /host in the initramfs, /mnt/host in webOS. Made afresh
                       at each launch (assemble):
      boot.conf          the boot manifest (targets.boot_conf): the target's paks, layers, state disks
                         and firmware paths, which guest/initramfs/rootfs/init carries out
      layer-sources      "<name> <source>" per line, which init logs beside each layer. The
                         layers themselves are not in the share: each is a read-only disk
                         (layerimage.py, qemu.plan) of resolve_layers' directories
      binds.conf         the active lines of guest/layers/binds.conf; each source at its path below
      guest/scripts/save-logs.sh, guest/scripts/save-device-counters.sh, guest/scripts/check-m1.sh,
      guest/scripts/time-boot.sh   (BASE)
      <path>             what `host/scripts/vm-launcher put PATH` adds while the VM runs (PUT_ALLOWED)
      out/               the mount point of "out"
  RUN_DIR/share/out/   9p "out", read-write: /host/out, /mnt/host/out in webOS. The guest's
                       output: logs/<name>/ (guest/scripts/save-logs.sh, finished when it has .done),
                       which the launcher saves to SAVED_LOG_DIR (save, harvest), and the tests'
                       results. Kept across launches.

Files are hard links where RUN_DIR is on the checkout's filesystem, copies elsewhere. The host share is read-only, so the guest cannot write through a link;
the host side never writes into a linked file either (put unlinks first).

Everything under out/ is the guest's, and the guest is root on a 9p share with security_model=none:
its links (to any host path), FIFOs and sockets there are real host ones. The host reads a log
directory there only through descriptors opened without following links, copies only its regular
files and directories (save), and writes nothing into out/ (the launcher's own logs go into the
saved copy). The guest's other files there are read the same way: one regular file at a time, no
link followed on the way (read, take: `vm-launcher get-out`), and removed without following a
linked directory on the way (remove: `vm-launcher rm-out`)."""

import errno
import os
import re
import shutil
import stat
import sys
from typing import NamedTuple

from ..firmware import targets
from . import layermeta

# The guest-side scripts every VM has (the launcher's save-logs.sh at stop, time-boot.sh, check-m1.sh).
BASE = (
    "guest/scripts/save-logs.sh",
    "guest/scripts/save-device-counters.sh",
    "guest/scripts/check-m1.sh",
    "guest/scripts/time-boot.sh",
)
# What `put` and binds.conf may take from the checkout: the test scripts and tools (the gl layer's
# too), and the guest build's test programs and layers. A directory entry ends in "/". Not local/,
# not the firmware or what is made from it (build/firmware-cache,
# build/tcg-perf/io.ext4), not the checkout VM's state or logs.
PUT_ALLOWED = (
    "tests/",
    "guest/scripts/",
    "guest/layers/gl/test/",
    "build/tests/",
    "build/layers/",
    "build/lgemu.ko",
    "build/mstar-utopia.ko",
    "build/lg115x.ko",
    "build/tcg-perf/perf-arm",
    "build/tcg-perf/io-perf",
    "build/devmode-tests/native-test",
)
LAYER_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


class Error(Exception):
    pass


class MissingError(Error):
    """A path of the out share (read, take, remove) or a directory on its way is not there."""


def dirs(run_dir):
    """(share, vm, host, out) of a runtime directory."""
    share = os.path.join(run_dir, "share")
    return share, os.path.join(share, "vm"), os.path.join(share, "host"), os.path.join(share, "out")


def allowed(rel):
    """Whether a checkout-relative path may be put into the host share."""
    if rel.startswith("/") or ".." in rel.split("/") or rel in (".", ""):
        return False
    return any(
        rel == a.rstrip("/") or rel.startswith(a if a.endswith("/") else a + "/")
        for a in PUT_ALLOWED
    )


def _remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def _link_file(src, dst):
    """dst as a hard link to src (a copy across filesystems), or the same symlink. An existing dst
    is unlinked first, never written into (it may be a link to a checkout file)."""
    _remove(dst)
    if os.path.islink(src):
        os.symlink(os.readlink(src), dst)
        return
    try:
        os.link(src, dst)
    except OSError as e:
        if e.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.EACCES):
            raise
        shutil.copy2(src, dst)


def _mkdirs(path, mode=0o755):
    if not os.path.isdir(path):
        _mkdirs(os.path.dirname(path), mode)
        os.mkdir(path)
        os.chmod(path, mode)


def _link_tree(src, dst, skip=()):
    """The tree src at dst: directories made with src's modes, files linked, symlinks as they are
    (the guest resolves them inside its own view); skip: names at src's top left out."""
    _remove(dst)
    os.mkdir(dst)
    os.chmod(dst, stat.S_IMODE(os.stat(src).st_mode))
    for root, subdirs, files in os.walk(src):
        out = os.path.join(dst, os.path.relpath(root, src))
        for d in list(subdirs):
            s = os.path.join(root, d)
            if os.path.islink(s):
                os.symlink(os.readlink(s), os.path.join(out, d))
                subdirs.remove(d)
            else:
                os.mkdir(os.path.join(out, d))
                os.chmod(os.path.join(out, d), stat.S_IMODE(os.stat(s).st_mode))
        for f in files:
            if root == src and f in skip:
                continue
            _link_file(os.path.join(root, f), os.path.join(out, f))


def _checkout_rel(tree, path):
    """path (absolute, or relative to the checkout) as a checkout-relative path that may be put,
    checked before and after resolving symlinks; Error otherwise."""
    if tree.installed:
        root = os.path.realpath(tree.src("guest"))
        rel = (
            "guest/" + os.path.relpath(path, root)
            if os.path.isabs(path)
            else os.path.normpath(path)
        )
        full = tree.src(rel)
        if not allowed(rel) or os.path.commonpath((root, os.path.realpath(full))) != root:
            raise Error(f"{path}: not a packaged guest runtime file")
        if not os.path.lexists(full):
            raise Error(f"{path}: no such packaged guest file")
        return rel, full
    top = os.path.realpath(tree.top)
    full = os.path.abspath(path if os.path.isabs(path) else os.path.join(tree.top, path))
    rel = os.path.relpath(full, tree.top)
    real = os.path.relpath(os.path.realpath(full), top)
    if not (allowed(rel) and allowed(real)):
        raise Error(
            f"{path}: not something the guest may have (only {', '.join(PUT_ALLOWED)} of the checkout)"
        )
    if not os.path.lexists(full):
        raise Error(f"{path}: no such file in {tree.top}")
    return rel, full


def put(tree, host_dir, paths):
    """Link each of paths (files or directories of the checkout, PUT_ALLOWED) into host_dir at its
    checkout-relative path; returns those paths."""
    if not os.path.isdir(host_dir):
        raise Error(f"no {host_dir}: the VM has not been started")
    done = []
    for path in paths:
        rel, full = _checkout_rel(tree, path)
        dst = os.path.join(host_dir, rel)
        _mkdirs(os.path.dirname(dst))
        if os.path.isdir(full) and not os.path.islink(full):
            _link_tree(full, dst)
        else:
            _link_file(full, dst)
        done.append(rel)
    return done


def resolve_layer(tree, target, name):
    """(directory, checkout-relative source) of a layer for the firmware target (chip, release), or
    (None, why). The always-used layers are only ever generated (guest/layers/soc and guest/layers/webos hold
    one source directory per chip or release, not a layer)."""
    built, src = tree.layer(target, name), tree.src("guest", "layers", name)
    if os.path.isdir(built):
        return built, os.path.relpath(built, tree.top)
    if (
        os.path.isdir(src)
        and (not tree.installed or name == "normal")
        and name not in targets.ALWAYS_LAYERS
        and not os.path.isfile(os.path.join(src, "meson.build"))
    ):
        return src, os.path.relpath(src, tree.top)
    always = targets.always_layers(target)
    if os.path.isdir(src) and always != targets.WEBOS_LAYERS and name not in always:
        # SimpleSmart's, NetCast's and the test guest's builds make their one layer and no optional
        # one (meson.build's guest_modules): NetCast's Mesa is in its netcast45 layer
        return None, (
            f"{targets.name(target)} has none: its guest build makes only "
            f"the {', '.join(always)} layer"
        )
    if os.path.isdir(src):
        return None, f"not built (no {os.path.relpath(built, tree.top)})"
    return None, "not in the checkout"


def layer_names(layers, target):
    """LAYERS (highest priority first; NAME or NAME@VERSION) and then the target's always-used layers,
    each layer once, as init stacks them; the reserved name vm and names that are not plain names
    left out."""
    out = []
    for n in list(layers) + list(targets.always_layers(target)):
        name, version = layermeta.split(n or "")
        if (
            name
            and name != targets.VM_LAYER
            and LAYER_NAME.fullmatch(name)
            and (version is None or layermeta.VERSION.fullmatch(version))
            and name not in [layermeta.base(o) for o in out]
        ):
            out.append(n)
    return out


class Layer(NamedTuple):
    """A layer the guest stacks: its name in boot.conf, its directory, that directory relative to
    the checkout (layer-sources), and the names at its top left out of its image."""

    name: str
    src: str
    rel: str
    skip: tuple = ()


KERNEL_CMDLINE = os.path.join("usr", "share", "beanjar", "kernel.cmdline")


def kernel_args(layers):
    """The words the layers (Layers, highest first) ask for on the guest kernel's own command line,
    lowest layer first: each one's usr/share/beanjar/kernel.cmdline ('#' starts a comment line),
    which a layer carries for its own userland, such as the soc layer's Qt EGLFS Starfish setup
    (guest/layers/soc/o22/meson.build). The guest build decides which layers have one, by the
    firmware's facts; the launcher only reads it."""
    words = []
    for layer in reversed(layers):
        try:
            with open(os.path.join(layer.src, KERNEL_CMDLINE)) as f:
                text = f.read()
        except FileNotFoundError:
            continue
        except OSError as e:
            raise Error(f"{layer.rel}/{KERNEL_CMDLINE}: {e}") from None
        words += [w for line in text.splitlines() if not line.startswith("#") for w in line.split()]
    return words


def resolve_layers(tree, target, layers):
    """(layers, missing) for LAYERS (highest priority first; NAME@VERSION picks a versioned layer's
    version, plain NAME its default) and the firmware target's always-used layers: the Layers,
    highest first, and the optional layers the host does not have, as notes. Error if an
    always-used layer is not built, or for a version the layer does not have."""
    from . import components as guestside_components

    out, missing = [], []
    meta = None
    for entry in layer_names(layers, target):
        name, version = layermeta.split(entry)
        if meta is None:
            meta = layermeta.load(tree)
        versions = meta[name].versions if name in meta else None
        if version is not None and versions is None:
            raise Error(f"layer {name} has no versions: name it without @{version}")
        if versions is not None:
            # a layer built once per version (layermeta.py): only its versions' components; the
            # guest knows it by its name
            version = version or versions.default
            if version not in versions.available:
                raise Error(
                    f"layer {name} has no version {version!r}; its versions: "
                    f"{layermeta.version_text(meta[name])}"
                )
            try:
                src = guestside_components.select(
                    tree.build("components"), target, guestside_components.versioned(name, version)
                )
            except (ValueError, OSError) as exc:
                raise Error(str(exc)) from exc
            if src is None:
                missing.append(f"{name}@{version} (not built for {targets.name(target)})")
            else:
                out.append(Layer(name, src, os.path.relpath(src, tree.top)))
            continue
        try:
            components = guestside_components.resolve(tree, target, name)
        except (ValueError, OSError) as exc:
            raise Error(str(exc)) from exc
        if components:
            out += [Layer(alias, src, os.path.relpath(src, tree.top)) for alias, src in components]
            continue
        src, rel = resolve_layer(tree, target, name)
        if src is None:
            if name in targets.always_layers(target):
                raise Error(
                    f"layer {name} of {targets.name(target)}: {rel} (run scripts/build.sh guest)"
                )
            missing.append(f"{name} ({rel})")
            continue
        # a source layer's own layer.toml (layermeta.py) is the host's, not a file of the guest's /
        out.append(Layer(name, src, rel, (layermeta.FILE,)))
    return out, missing


def assemble(
    tree,
    target,
    run_dir,
    layers,
    say,
    derived=None,
    part_nodes=(),
    vm=None,
    files=None,
    fw=None,
    broadcast=None,
    qemu_arm=None,
    qemu_aarch64=None,
    emmc=None,
    model=None,
):
    """Make RUN_DIR/share/host afresh and RUN_DIR/share/out if it is missing (the module's doc), with
    the boot.conf of the firmware target (chip, release) and its layers (resolve_layers' Layers,
    highest first; derived: the firmware's own module parameters, targets.derived_params;
    part_nodes: the eMMC partitions' nodes, qemu.part_nodes; vm: the VM's own module parameters,
    targets.boot_conf's; files: {name: path}, the files made from the firmware that the modules read
    as /host/<name>, targets.derived_files; fw, broadcast: the firmware directory and the board's
    broadcast standard, targets.boot_conf's; model: the retail model whose board the VM boots, if
    it chose one; qemu_arm: the interpreter of a USERLAND=qemu-user guest, paths.Tree.qemu_arm, put as /host/qemu-arm and named in boot.conf's USERLAND;
    qemu_aarch64: its 64-bit ARM one on the x86-64 guest kernel, paths.Tree.qemu_aarch64, put as
    /host/qemu-aarch64 and named in USERLAND_AARCH64; emmc: firmware.updatable's whole-disk
    /dev/mmcblk0 plan, targets.boot_conf's)."""
    _share, _vm, host, out = dirs(run_dir)
    _remove(host)
    _mkdirs(os.path.join(host, "out"))
    _mkdirs(out)
    put(tree, host, BASE)
    for name, path in (files or {}).items():
        shutil.copyfile(path, os.path.join(host, name))
    if qemu_arm:
        shutil.copyfile(qemu_arm, os.path.join(host, "qemu-arm"))
        if qemu_aarch64:
            shutil.copyfile(qemu_aarch64, os.path.join(host, "qemu-aarch64"))
    with open(os.path.join(host, "layer-sources"), "w") as f:
        f.write("".join(f"{layer.name} {layer.rel}\n" for layer in layers))
    with open(os.path.join(host, "boot.conf"), "w") as f:
        f.write(
            targets.boot_conf(
                target,
                [layer.name for layer in layers],
                derived,
                part_nodes,
                vm,
                fw,
                broadcast,
                "qemu-user" if qemu_arm else "native",
                bool(qemu_arm and qemu_aarch64),
                emmc,
                model,
            )
        )
    binds = []
    conf = tree.src("guest", "layers", "binds.conf")
    if os.path.isfile(conf):
        with open(conf) as f:
            for line in f:
                words = line.split()
                if not words or words[0].startswith("#"):
                    continue
                if len(words) != 2:
                    say(f"binds.conf: not '<source> <target>': {line.strip()}")
                    continue
                try:
                    put(tree, host, [words[0]])
                except Error as e:
                    say(f"binds.conf: {e}")
                    continue
                binds.append(f"{os.path.normpath(words[0])} {words[1]}\n")
    if binds:
        with open(os.path.join(host, "binds.conf"), "w") as f:
            f.write("".join(binds))


def _free(dest):
    n, d = 1, dest
    while os.path.lexists(d):
        n += 1
        d = f"{dest}.{n}"
    return d


# Saving the guest's logs. Everything under out/ is the guest's: as root it makes real host symlinks
# (to any path), FIFOs and sockets there through 9p (security_model=none). So the host never
# follows a name it finds there, never opens anything there but a regular file, and never writes
# there: a finished log directory is copied, regular files and directories only, into a directory
# of its own under the saved logs, through descriptors opened without following links; what is
# left out is listed in SKIPPED; the launcher's own logs (HOST_LOGS) go into that copy, never into
# the share; the guest's directory is removed afterwards without following links.

SKIPPED = "skipped.txt"  # in a saved log directory: what of the guest's was not saved
HOST_LOGS = ("webos.log", "console.log", "qemu.out", "netstub.log")  # the launcher's, at stop
MAX_DEPTH = 32  # directories below a log directory that are copied; deeper ones are listed
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _say(line):
    print(line, file=sys.stderr, flush=True)


def _plain(name):
    """A name of one directory entry, nothing that walks elsewhere."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\0" not in name


def _kind(mode):
    for test, kind in (
        (stat.S_ISREG, "file"),
        (stat.S_ISDIR, "directory"),
        (stat.S_ISLNK, "symlink"),
        (stat.S_ISFIFO, "fifo"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISCHR, "character device"),
        (stat.S_ISBLK, "block device"),
    ):
        if test(mode):
            return kind
    return "unknown"


def _shown(rel):
    """A guest's path as one line of text (its newlines, tabs and undecodable bytes escaped)."""
    return rel.encode("unicode_escape", "backslashreplace").decode("ascii")


def _open_log_dir(logs, name):
    """A descriptor of logs/name, a real directory, or OSError (ELOOP, ENOTDIR for anything else):
    neither logs nor name is followed if it is a link."""
    if not _plain(name):
        raise OSError(errno.EINVAL, "not a plain name")
    top = os.open(logs, _DIR_FLAGS)
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=top)
    finally:
        os.close(top)


def _done(fd):
    """Whether the directory fd has .done, a regular file (a link to one is not)."""
    try:
        return stat.S_ISREG(os.stat(".done", dir_fd=fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def finished(logs, name):
    """Whether logs/name (logs: the guest's out/logs) is a finished log directory: None while it or
    its .done is not there, else "" if it is one (a real directory with a regular .done), else why
    it is refused (a link, or a .done that is not a regular file)."""
    try:
        fd = _open_log_dir(logs, name)
    except FileNotFoundError:
        return None
    except OSError:
        try:
            st = os.lstat(logs)
            if not stat.S_ISDIR(st.st_mode):
                return f"{logs}: a {_kind(st.st_mode)}, not a directory"
            st = os.lstat(os.path.join(logs, name))
        except FileNotFoundError:
            return None
        except OSError as e:
            return f"{os.path.join(logs, name)}: {e.strerror}"
        return f"{os.path.join(logs, name)}: a {_kind(st.st_mode)}, not a directory"
    try:
        try:
            st = os.stat(".done", dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return f"{os.path.join(logs, name, '.done')}: a {_kind(st.st_mode)}, not a file"
        return ""
    finally:
        os.close(fd)


def _copy_file(src_fd, name, dest):
    """The regular file name of the directory src_fd as the new file dest; False (nothing made) if
    it is not a regular file by the time it is opened."""
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=src_fd
        )
    except OSError:
        return False
    with open(fd, "rb") as src:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False
        with open(os.open(dest, _NEW_FILE, 0o600), "wb") as out:
            shutil.copyfileobj(src, out)
            os.fchmod(out.fileno(), stat.S_IMODE(st.st_mode) & 0o777 | 0o600)
        os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
    return True


def _copy_tree(src_fd, dest, rel, skipped, reserved=(), depth=0):
    """The entries of the directory src_fd into the new directory dest: regular files and
    directories, walked without following links; each other entry as (relative path, kind, link
    target) in skipped, and reserved names at the top (the host's own) as shadowed."""
    with os.scandir(src_fd) as it:
        names = sorted(e.name for e in it)
    for name in names:
        r = f"{rel}/{name}" if rel else name
        if not _plain(name):
            skipped.append((r, "bad name", None))
            continue
        try:
            st = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        except OSError:
            continue  # gone meanwhile
        kind = _kind(st.st_mode)
        if not rel and name in reserved:
            skipped.append((r, f"{kind}, shadowed by the host's own", None))
        elif stat.S_ISREG(st.st_mode):
            if not _copy_file(src_fd, name, os.path.join(dest, name)):
                skipped.append((r, "changed while saved", None))
        elif stat.S_ISDIR(st.st_mode) and depth < MAX_DEPTH:
            try:
                sub = os.open(name, _DIR_FLAGS, dir_fd=src_fd)
            except OSError:
                skipped.append((r, "changed while saved", None))
                continue
            try:
                os.mkdir(os.path.join(dest, name), 0o700)
                _copy_tree(sub, os.path.join(dest, name), r, skipped, (), depth + 1)
                os.chmod(os.path.join(dest, name), stat.S_IMODE(st.st_mode) & 0o777 | 0o700)
            finally:
                os.close(sub)
        elif stat.S_ISDIR(st.st_mode):
            skipped.append((r, "directory, too deep", None))
        elif stat.S_ISLNK(st.st_mode):
            try:
                target = os.readlink(name, dir_fd=src_fd)
            except OSError:
                target = None
            skipped.append((r, kind, target))
        else:
            skipped.append((r, kind, None))


def _write_new(path, data):
    with open(os.open(path, _NEW_FILE, 0o644), "wb") as f:
        f.write(data)


def save(logs, name, saved, host=None, say=_say):
    """Save the finished log directory logs/name (logs: the guest's out/logs) into saved as
    saved/name (a name taken there gets .2, .3 ...) and remove it from logs; returns where it went,
    or None if it is not a finished log directory (finished: then it is left as it is, with a note).
    host: {name: path} of the launcher's own logs (HOST_LOGS), copied in after the guest's files;
    each of those names is the host's in the saved directory, whether or not the host has that
    file: a guest's file of the same name is not saved but listed in SKIPPED, as is everything of
    the guest's that is not a regular file or a directory."""
    try:
        fd = _open_log_dir(logs, name)
    except OSError:
        fd = None
    if fd is None or not _done(fd):
        if fd is not None:
            os.close(fd)
        why = finished(logs, name) or f"{os.path.join(logs, name)}: not a finished log directory"
        say(f"logs: {why}; left alone")
        return None
    try:
        os.makedirs(saved, exist_ok=True)
        while True:
            dest = _free(os.path.join(saved, name))
            try:
                os.mkdir(dest, 0o700)
                break
            except FileExistsError:
                continue
        skipped = []
        _copy_tree(fd, dest, "", skipped, set(HOST_LOGS) | {SKIPPED})
    finally:
        os.close(fd)
    for f, path in (host or {}).items():
        if f not in HOST_LOGS:
            raise ValueError(f"{f}: not one of the launcher's logs")
        if path and os.path.isfile(path):
            with (
                open(path, "rb") as src,
                open(os.open(os.path.join(dest, f), _NEW_FILE, 0o644), "wb") as out,
            ):
                shutil.copyfileobj(src, out)
    if skipped:
        _write_new(
            os.path.join(dest, SKIPPED),
            (
                "# What of the guest's log directory was not saved (only its regular files and\n"
                "# directories are): path, kind, a symlink's target.\n"
                + "".join(
                    f"{_shown(r)}\t{kind}" + (f"\t-> {_shown(t)}" if t is not None else "") + "\n"
                    for r, kind, t in skipped
                )
            ).encode(),
        )
    os.chmod(dest, 0o755)
    _discard(logs, name, say)
    return dest


def _discard(logs, name, say):
    """Remove logs/name, a directory of the guest's, without following links (a link is unlinked)."""
    try:
        top = os.open(logs, _DIR_FLAGS)
    except OSError as e:
        say(f"logs: {logs}: {e.strerror}; {name} not removed")
        return
    try:
        if stat.S_ISDIR(os.stat(name, dir_fd=top, follow_symlinks=False).st_mode):
            shutil.rmtree(name, dir_fd=top)
        else:
            os.unlink(name, dir_fd=top)
    except FileNotFoundError:
        pass
    except OSError as e:
        say(f"logs: {os.path.join(logs, name)}: not removed: {e}")
    finally:
        os.close(top)


def logs_dir(run_dir):
    """RUN_DIR/share/out/logs, the guest's log directories."""
    return os.path.join(dirs(run_dir)[3], "logs")


def harvest(run_dir, saved, say=_say):
    """Save (save) every finished log directory of RUN_DIR/share/out/logs into saved; returns
    where they went. A directory without .done is left for later; any other entry, or a logs that
    is not a real directory, is left alone with a note."""
    logs = logs_dir(run_dir)
    try:
        top = os.open(logs, _DIR_FLAGS)
    except FileNotFoundError:
        return []
    except OSError as e:
        say(f"logs: {logs}: {e.strerror}; left alone")
        return []
    try:
        names = sorted(os.listdir(top))
        kinds = {}
        for n in names:
            try:
                kinds[n] = os.stat(n, dir_fd=top, follow_symlinks=False).st_mode
            except OSError:
                pass
    finally:
        os.close(top)
    out = []
    for n, mode in kinds.items():
        if not stat.S_ISDIR(mode):
            say(f"logs: {os.path.join(logs, n)}: a {_kind(mode)}, not a directory; left alone")
        elif finished(logs, n) is not None:
            d = save(logs, n, saved, say=say)
            if d:
                out.append(d)
    return out


def _out_parts(rel):
    parts = rel.split("/")
    if not all(_plain(p) for p in parts):
        raise Error(f"{rel}: not a plain path in the out share")
    return parts


def _out_dir(out, parts, make=False):
    """A descriptor of the directory out/parts[:-1] (out: RUN_DIR/share/out), each directory on the
    way opened without following a link (made first with make); Error if one is missing (Error's
    errno ENOENT) or not a real directory."""
    try:
        fd = os.open(out, _DIR_FLAGS)
    except OSError as e:
        raise Error(f"{out}: {e.strerror}") from None
    try:
        for i, p in enumerate(parts[:-1]):
            if make:
                try:
                    os.mkdir(p, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                sub = os.open(p, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise MissingError(f"{'/'.join(parts[: i + 1])}: not in the out share") from None
            except OSError:
                raise Error(
                    f"{'/'.join(parts[: i + 1])}: not a directory in the out share"
                ) from None
            os.close(fd)
            fd = sub
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_out_file(out, rel):
    """A descriptor of the regular file rel of the guest's out share, opened without following a
    link at any step and without blocking on a FIFO; MissingError if it is not there, Error if it or
    a directory on the way is anything but a real file or directory."""
    parts = _out_parts(rel)
    dfd = _out_dir(out, parts)
    try:
        try:
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
            )
        except FileNotFoundError:
            raise MissingError(f"{rel}: not in the out share") from None
        except OSError:
            try:
                kind = _kind(os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False).st_mode)
            except OSError:
                kind = "unknown"
            raise Error(f"{rel}: a {kind}, not a regular file in the out share") from None
    finally:
        os.close(dfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise Error(f"{rel}: a {_kind(st.st_mode)}, not a regular file in the out share")
    return fd


def read(out, rel):
    """The bytes of the regular file rel (a path relative to out, RUN_DIR/share/out), read without
    following any link the guest made there, never from a FIFO, socket or device; MissingError if
    it is not there, Error if it is not a regular file or a directory on the way is not a real one."""
    with open(_open_out_file(out, rel), "rb") as f:
        return f.read()


def take(out, rel, dest):
    """Copy the regular file rel of the guest's out share (read's rules) to the host path dest, a
    file (replaced if it is there) or an existing directory (dest/<rel's name>), or "-" for
    standard output; returns where it went. The copy's mode is 0644."""
    with open(_open_out_file(out, rel), "rb") as src:
        if dest == "-":
            shutil.copyfileobj(src, sys.stdout.buffer)
            sys.stdout.buffer.flush()
            return dest
        if os.path.isdir(dest):
            dest = os.path.join(dest, rel.rsplit("/", 1)[-1])
        with open(
            os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644), "wb"
        ) as o:
            shutil.copyfileobj(src, o)
    return dest


def remove(out, rel):
    """Remove the entry rel of the guest's out share, whatever it is but a directory (a link is
    unlinked, not followed), with no link followed on the way; whether there was one. Nothing is
    removed if a directory on the way is missing (False) or is not a real directory (Error), or if
    rel names a directory (Error)."""
    parts = _out_parts(rel)
    try:
        dfd = _out_dir(out, parts)
    except MissingError:
        return False
    try:
        os.unlink(parts[-1], dir_fd=dfd)
        return True
    except FileNotFoundError:
        return False
    except IsADirectoryError:
        raise Error(f"{rel}: a directory in the out share") from None
    except OSError as e:
        if e.errno == errno.EPERM and stat.S_ISDIR(
            os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False).st_mode
        ):
            raise Error(f"{rel}: a directory in the out share") from None
        raise Error(f"{rel}: {e.strerror}") from None
    finally:
        os.close(dfd)


def place(out, rel, src):
    """Copy the host file src into the guest's out share (out: RUN_DIR/share/out) at rel, a path
    relative to it, without following any link the guest made there: each directory on the way is
    made, or must be a real one; the file is written under a new temporary name and renamed over
    whatever entry rel names (a link is replaced, never written through). Returns the path; Error
    if rel is not a plain relative path or a directory on the way is not a real one."""
    parts = _out_parts(rel)
    fd = _out_dir(out, parts, make=True)
    try:
        name = parts[-1]
        tmp = f".{name}.{os.getpid()}.{os.urandom(4).hex()}"
        with open(src, "rb") as f, open(os.open(tmp, _NEW_FILE, 0o644, dir_fd=fd), "wb") as o:
            shutil.copyfileobj(f, o)
        try:
            os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        except OSError as e:
            os.unlink(tmp, dir_fd=fd)
            raise Error(f"{rel}: {e.strerror}") from None
    finally:
        os.close(fd)
    return os.path.join(out, rel)
