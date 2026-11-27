# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Guest layers as read-only squashfs images (docs/dev/architecture/build-and-boot.md, "Layer images").

Each layer the guest stacks (share.resolve_layers) is one image, attached read-only as a virtio
disk with the serial layer-<name> (qemu.plan) and mounted by guest/initramfs/rootfs/init as an
overlay lower in place: nothing is copied at boot. The images are made with the official
squashfs-tools (mksquashfs -all-root: every file root's, as the guest needs, while the host files
stay the user's) and named by their content and FORMAT, so that an unchanged layer is never built
again and every VM on the host uses the same file.

  key        sha256 of FORMAT and the layer's digest: a published component variant's own name
             (meson/components.py, which is a digest of its tree), else digest(tree)
  shipped    tree.build("layer-images"): the package's prebuilt images (beanjar-guest's
             /usr/share/beanjar/guest/layer-images, packaging/debian/build.py), looked up first
  cache      tree.cache("layer-images"): the checkout's build/layer-images, the installed user's
             ~/.cache/beanjar/layer-images; images are built there on first use

LZO, by measurement on an RK3399's Cortex-A53 (the docs' table): the fastest of the guest kernel's
decompressors (zlib, lzo, zstd; it has no lz4 or xz), at a size between zstd's and zlib's."""

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess

# what the images are made with; a change here changes every key, so old images are never used
FORMAT = "squashfs 1: lzo, -all-root, -no-xattrs, 128 KiB blocks"
MKSQUASHFS = (
    "-noappend",
    "-all-root",
    "-no-xattrs",
    "-comp",
    "lzo",
    "-b",
    "128K",
    "-mkfs-time",
    "0",
)
MKSQUASHFS_QUIET = ("-quiet", "-no-progress")
VARIANT = re.compile(r"[0-9a-f]{64}")
SERIAL_PREFIX = "layer-"
LOCK = ".lock"  # in the cache directory: images are looked up and built under it shared, pruned under it exclusive
SERIAL_MAX = 20  # virtio-blk's serial (VIRTIO_BLK_ID_BYTES)


class Error(Exception):
    pass


def serial(name):
    """The disk serial of the layer name, by which init finds its image."""
    s = SERIAL_PREFIX + name
    if len(s.encode()) > SERIAL_MAX:
        raise Error(f"layer {name}: its disk serial {s} is longer than {SERIAL_MAX} bytes")
    return s


def digest(tree, skip=()):
    """A digest of the directory tree: each entry's path, type and mode, a symlink's target and a
    file's bytes (not owners or times); skip: names at the top left out."""
    out = hashlib.sha256()
    entries = []
    for root, dirs, files in os.walk(tree):
        dirs.sort()
        for n in dirs + files:
            path = os.path.join(root, n)
            rel = os.path.relpath(path, tree)
            if root == tree and n in skip:
                continue
            entries.append((rel, path))
    for rel, path in sorted(entries):
        st = os.lstat(path)
        out.update(json.dumps([rel, stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode)]).encode())
        out.update(b"\0")
        if stat.S_ISLNK(st.st_mode):
            out.update(os.readlink(path).encode() + b"\0")
        elif stat.S_ISREG(st.st_mode):
            with open(path, "rb") as f:
                while block := f.read(1 << 20):
                    out.update(block)
        elif not stat.S_ISDIR(st.st_mode):
            raise Error(f"special file in layer: {path}")
    return out.hexdigest()


def layer_digest(src, skip=()):
    """The digest a layer directory is known by: a published variant's name (variants/<sha256>),
    else its tree's digest."""
    name = os.path.basename(os.path.normpath(src))
    parent = os.path.basename(os.path.dirname(os.path.normpath(src)))
    if parent == "variants" and VARIANT.fullmatch(name) and not skip:
        return name
    return digest(src, skip)


def key(layer_digest_):
    return hashlib.sha256(f"{FORMAT}\n{layer_digest_}".encode()).hexdigest()


def dirs(tree):
    """The directories images are looked up in, the shipped first; the last is the cache."""
    out = []
    for d in (tree.build("layer-images"), tree.cache("layer-images")):
        if d not in out:
            out.append(d)
    return out


def build(src, dest, skip=()):
    """The image of the directory src at dest (made beside it and renamed: a reader never sees a
    part of one, and two builders of the same image both leave a whole one)."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = f"{dest}.{os.getpid()}.tmp"
    excl = [a for n in skip for a in ("-e", n)]
    try:
        subprocess.run(
            ["mksquashfs", src, tmp, *MKSQUASHFS, *MKSQUASHFS_QUIET, *excl],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise Error("no mksquashfs: install squashfs-tools") from None
    except subprocess.CalledProcessError as e:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise Error(f"mksquashfs {src}: {(e.stderr or '').strip() or e.returncode}") from None
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)
    return dest


class _lock:
    """flock on the cache directory's .lock: shared for a lookup or build (image), exclusive for a
    prune, so that a start never looks an image up while it is being removed."""

    def __init__(self, tree, mode):
        self.path = os.path.join(tree.cache("layer-images"), LOCK)
        self.mode = mode

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.f = open(self.path, "a")
        fcntl.flock(self.f, self.mode)
        return self

    def __exit__(self, *exc):
        self.f.close()


def image(tree, src, skip=(), make=True, say=None):
    """The image of the layer directory src (skip: names at its top left out): a shipped or cached
    one if there is one, else built into the cache (make=False: only its path)."""
    k = key(layer_digest(src, skip))
    places = [os.path.join(d, k + ".sqfs") for d in dirs(tree)]
    with _lock(tree, fcntl.LOCK_SH):
        for path in places:
            if os.path.isfile(path):
                return path
        if not make:
            return places[-1]
        if say:
            say(f"layer image: building {os.path.relpath(src, tree.top)} into {places[-1]}")
        return build(src, places[-1], skip)


def prune(tree, used, dry_run=False):
    """Remove the images of the cache that used() does not name; the removed paths (those it would
    remove, with dry_run). used() is called under the exclusive lock, so no start looks an image
    up meanwhile, and returns the keys to keep (key()) or raises Error to prune nothing. The shipped
    images (tree.build) are the package's and stay. Only <key>.sqfs files are touched."""
    cache = tree.cache("layer-images")
    if not os.path.isdir(cache):
        return []
    removed = []
    with _lock(tree, fcntl.LOCK_EX):
        keep = used()
        for n in sorted(os.listdir(cache)):
            m = re.fullmatch(r"([0-9a-f]{64})\.sqfs", n)
            if not m or m.group(1) in keep:
                continue
            path = os.path.join(cache, n)
            if not dry_run:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    continue
            removed.append(path)
    return removed


def referenced(tree, libraries):
    """The image keys the VMs of the libraries (core.Library objects) use: each VM's layers resolved
    as qemu.plan resolves them (versioned ones, NAME@VERSION, as its vm.toml pins them). Error if a
    VM is running or being started on this host (any library's, the development VMs' and the regress
    harness's too: vmunits.vm_units, each VM's start lock): nothing is pruned while one may use an image.
    A VM whose layers cannot be resolved (no firmware, a layer not built) uses no image yet."""
    from . import base, share, vmunits

    units = vmunits.vm_units()
    if units:
        raise Error(
            "a VM is running: " + ", ".join(sorted(units)) + "; stop it, or prune when none runs"
        )
    keys = set()
    for lib in libraries:
        for vm in lib.vms():
            if vm.lock_owner(units) is not None:
                raise Error(f"VM {vm.name} is running or being started; prune when none is")
            try:
                cfg = vm.config
                layers, _ = share.resolve_layers(tree, vm.target(), list(cfg.get("layers") or []))
            except (base.Error, share.Error, OSError):
                continue
            keys.update(key(layer_digest(la.src, la.skip)) for la in layers)
    return keys
