# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The firmware a VM boots: a directory of the
paks that qemu.py attaches (its firmware target's, targets.py). Every VM has its own, in its folder: <vm>/firmware/
(OWN) with the paks, a manifest.toml (where they came from) and cache/, where the launcher keeps its
partition map (partmap.py). The one exception is a vm.toml whose fw names another directory, which
is used in place and never written; its map goes into the VM's firmware-cache/ (firmware.vm_firmware;
the launcher resolves it the same way).

For measured super.pak/dpmeta.pak firmware, import copies the described linear
block ranges into owned named paks (superpak.py), without unpacking SquashFS.
The supplied directory is never normalized in place.

New VM (newvm.py) fills firmware/ from a directory of extracted paks, hard-linked where it is on
the filesystem of the library (nothing writes a pak), else copied, or from a .epk: epk2extract -c
-n runs in a scratch directory inside the new VM's own scratch (so a rename moves the paks into
firmware/) on a symlink to the .epk, so nothing is written next to the source. -n keeps it from
unpacking rootfs/: the guest boots from the paks, and nothing reads an extracted rootfs. A VM
reads only its target's paks (targets.pak_files: those it attaches and those its partition map is
made of) and manifest.toml from firmware/; only those paks are moved there, and everything else (the other paks, such as kernel, tzfw and lxboot, and
kernel.pak's unpacked intermediates) is dropped with the scratch.
"""

import errno
import hashlib
import os
import re
import shutil
import signal
import subprocess
import time

from . import kernelcerts, paths, superpak, targets
from .base import Error, read_toml

# epk2extract names its output directory "<version>-<otaID>", e.g. 05.06.10.01-HE_DTV_WT1H_AFAAABAA
FW_DIR_RE = re.compile(r"\d\d\.\d\d\.\d\d(\.\d\d)?-[A-Za-z0-9_.-]+")
OWN_FW = "firmware"  # the VM's own firmware, <vm>/firmware: vm.toml without fw
OWN = OWN_FW
EXTERNAL_CACHE = "firmware-cache"  # <vm>/firmware-cache: the partition map of an external fw


def vm_firmware(vm_dir, fw=None):
    """The firmware of the VM in vm_dir, as vm.toml's fw names it: (FW, FW_CACHE). No fw (or one that
    is the VM's own firmware/): <vm>/firmware, whose cache/ has what the launcher derives from it,
    the partition map (partmap.py). Any other directory (an absolute path, or one relative to the
    VM's folder) is used in place and never written: its map goes into the VM's firmware-cache/."""
    own = os.path.join(vm_dir, OWN_FW)
    d = os.path.normpath(os.path.join(vm_dir, os.path.expanduser(str(fw)))) if fw else own
    return (
        (own, os.path.join(own, "cache")) if d == own else (d, os.path.join(vm_dir, EXTERNAL_CACHE))
    )


# the firmware kernel's built-in X.509 certificates, one DER list (kernelcerts.py), kept beside the
# paks of a firmware that has an adaptive partition; its manifest's kernel_certs names the file
KERNEL_CERTS = "kernel-certs.der"


def find_epk2extract(library):
    env = os.environ.get("BEANJAR_EPK2EXTRACT") or library.config.get("epk2extract")
    if env:
        return env
    found = shutil.which("epk2extract")
    if found:
        return found
    local = os.path.expanduser("~/.local/bin/epk2extract")
    return local if os.access(local, os.X_OK) else None


def target_of(fw_dir, name=None):
    """(chip, release) of a firmware being imported (paths.firmware_target: the manifest of a directory
    that has one, else the release its rootfs names with that release's chip for the model code of
    name, default the directory's name); an Error about fw for a firmware this tree does not boot."""
    try:
        return paths.firmware_target(fw_dir, name)
    except ValueError as e:
        raise Error(str(e), "fw") from None


def paks_in(d, target):
    """The target's paks that directory d has, as file names."""
    logical = logical_images(d)
    return [
        p + ".pak"
        for p in targets.pak_files(target, d)
        if p in logical or os.path.isfile(os.path.join(d, p + ".pak"))
    ]


def logical_images(d):
    try:
        return superpak.images(d)
    except ValueError as e:
        raise Error(str(e), "fw") from None


def has_root(d):
    return bool(paths.root_pak(d) or logical_images(d).get("rootfs"))


def paks_size(d, target):
    logical = logical_images(d)
    return sum(
        logical[p[:-4]].size if p[:-4] in logical else os.path.getsize(os.path.join(d, p))
        for p in paks_in(d, target)
    )


def copy_size(d, target, dest_dir):
    """Storage needed at import: mapped ranges are copied even on the same device."""
    if not linkable(d, dest_dir):
        return paks_size(d, target)
    logical = logical_images(d)
    return sum(
        image.size for name, image in logical.items() if name in targets.pak_files(target, d)
    )


def manifest(d):
    return read_toml(os.path.join(d, "manifest.toml"), None) if d else None


def linkable(src, dest_dir):
    """Whether src can be hard-linked into dest_dir: the same filesystem."""
    return os.stat(src).st_dev == os.stat(dest_dir).st_dev


def sha256_file(path, out=None):
    h = hashlib.sha256()
    size = os.path.getsize(path)
    done, last = 0, time.monotonic()
    with open(path, "rb") as f:
        while True:
            b = f.read(4 << 20)
            if not b:
                break
            h.update(b)
            done += len(b)
            if out and time.monotonic() - last > 2:
                last = time.monotonic()
                out(f"sha256: {done * 100 // max(size, 1)}%")
    return h.hexdigest()


def run_streamed(cmd, out, **kw):
    """cmd in a session of its own, each output line to out; its whole process group is killed if
    we are interrupted (KeyboardInterrupt: Ctrl-C, or SIGTERM through newvm's handler)."""
    p = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        **kw,
    )
    try:
        for raw in p.stdout:
            out(raw.decode(errors="replace").rstrip("\n"))
        return p.wait()
    except BaseException:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        raise
    finally:
        p.stdout.close()


def extract(library, epk, work, out):
    """epk2extract on epk in the scratch directory work: (the directory with the paks, the
    firmware's name from epk2extract's output directory, or None)."""
    tool = find_epk2extract(library)
    if not tool:
        raise Error("epk2extract not found (PATH, ~/.local/bin, or BEANJAR_EPK2EXTRACT)", "fw")
    link = os.path.join(work, os.path.basename(epk))
    os.symlink(epk, link)
    out(f"$ {tool} -c -n {os.path.basename(epk)}")
    rc = run_streamed([tool, "-c", "-n", link], out, cwd=work)
    if rc != 0:
        raise Error(f"epk2extract failed (exit {rc})", "fw")
    os.unlink(link)
    # Its output directory, "<version>-<otaID>"; paks directly in the scratch dir otherwise.
    dirs = [
        d
        for d in os.listdir(work)
        if os.path.isdir(os.path.join(work, d)) and FW_DIR_RE.fullmatch(d)
    ]
    if len(dirs) == 1:
        return os.path.join(work, dirs[0]), dirs[0]
    return work, None


def check_paks(d, target):
    present = set(paks_in(d, target))
    missing = [p + ".pak" for p in targets.pak_files(target, d) if p + ".pak" not in present]
    if missing:
        raise Error("the firmware lacks " + ", ".join(missing), "fw")


def place(src, dest, target, how, out):
    """The target's paks of src into the new directory dest: "move" (a rename: the scratch of an
    extraction) or "link" (hard links, as nothing writes a pak; a copy where dest is on another
    filesystem)."""
    logical = logical_images(src)
    for p in paks_in(src, target):
        a, b = os.path.join(src, p), os.path.join(dest, p)
        if p[:-4] in logical:
            image = logical[p[:-4]]
            out(f"{p}: copying mapped image ({fmt_size(image.size)})")
            try:
                superpak.copy(image, b)
            except ValueError as e:
                raise Error(str(e), "fw") from None
            continue
        if how == "move":
            os.rename(a, b)
            continue
        if how == "link":
            try:
                os.link(a, b)
                out(f"{p}: linked")
                continue
            except OSError as e:
                if e.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP):
                    raise
        out(f"{p}: copying {fmt_size(os.path.getsize(a))}")
        shutil.copyfile(a, b)
        shutil.copystat(a, b)


def place_kernel_certs(src, dest, target, how, out):
    """The certificates of the firmware src into the new directory dest, where it has an adaptive
    partition: the file the entry keeps, else read from src's kernel.pak (epk2extract decodes a
    W22O one, so this is import's job, never a VM's start). The file's name for the manifest, or
    None for a firmware without the partition."""
    if not targets.adaptive_partition(target, src):
        return None
    a, b = os.path.join(src, KERNEL_CERTS), os.path.join(dest, KERNEL_CERTS)
    if how == "link" and os.path.isfile(a):
        shutil.copyfile(a, b)
        out(f"{KERNEL_CERTS}: copied")
        return KERNEL_CERTS
    try:
        n = kernelcerts.write(src, b)
    except (kernelcerts.Error, OSError) as e:
        raise Error(f"the kernel's certificates for the adaptive partition: {e}", "fw") from None
    out(f"{KERNEL_CERTS}: {n} certificates read from kernel.pak")
    return KERNEL_CERTS


def fmt_size(n):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" or n >= 100 else f"{n:.1f} {unit}"
        n /= 1000
