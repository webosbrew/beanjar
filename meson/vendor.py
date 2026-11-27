#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The prebuilt third-party programs of beanjar-vendor that packaging/vendor.toml pins.

    vendor.py tree ARCH PROGRAM...     print the unpacked tree of each, one per line
    vendor.py pinned PROGRAM...        print those of the programs that are pinned (or overridden,
                                       BEANJAR_VENDOR_<PROGRAM>), one per line

Each program is a GitHub release of the pinned repository, tag <program>-<version>, holding
<program>-<version>-<arch>.tar.xz (arch: Debian's amd64 or arm64), whose root is the program's
install prefix with a BUILDINFO file (Debian control syntax) and the licence texts in share/doc.
The tarball is downloaded into build/vendor/download and checked against the pinned SHA-256; a
mismatch stops. It is unpacked into build/vendor/<program>-<version>-<arch>, whose BUILDINFO must
name the program, version and architecture pinned. A tree already unpacked from the pinned
tarball is used as it is, so this runs at every configure without downloading again. There is
no other source of these programs: no system package and no local build stands in for them.

A pin with guest = "<arch>" is a program that runs inside the guest, not on the host: it has one
tarball, for that architecture, which every host architecture takes (qemu-user's qemu-arm: arm64;
the test guest's userland, docs/dev/architecture/test-guest.md: armel). guest = ["<arch>", ...]
pins a tarball for each of the guest architectures named (qemu-user: arm64 for the arm64 guest
kernel, amd64 for the experimental x86-64 one): PROGRAM is the first, PROGRAM:ARCH another one,
which BEANJAR_VENDOR_<PROGRAM>_<ARCH> overrides.

BEANJAR_VENDOR_<PROGRAM> (upper case, '-' as '_'), the path of a tarball of the program, takes it
in place of the pinned release, pinned or not: a build of beanjar-vendor's own (its CI run's
artifact) before it is released. It is unpacked into build/vendor/<file name>, and its BUILDINFO
must name the program; nothing is checked against the pin.
"""

import hashlib
import os
import shutil
import sys
import tarfile
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

TOP = Path(__file__).resolve().parents[1]
PIN = TOP / "packaging/vendor.toml"
CACHE = TOP / "build/vendor"
ARCHS = ("amd64", "arm64")


class Error(Exception):
    pass


def pins(path=PIN):
    """{program: {"repository", "tag", "version", "amd64", "arm64"}} of the pin file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    repository = data.get("repository")
    found = {}
    for program, pin in data.items():
        if not isinstance(pin, dict):
            continue
        tag = pin.get("tag", "")
        if not tag.startswith(program + "-"):
            raise Error(f"{path}: {program}: the tag must be {program}-<version>, not {tag!r}")
        guest = guests(pin)
        if guest is None:
            raise Error(
                f"{path}: {program}: guest is the guest's architectures, not {pin['guest']!r}"
            )
        for arch in guest or ARCHS:
            if len(pin.get(arch, "")) != 64:
                raise Error(f"{path}: {program}: no SHA-256 for {arch}")
        found[program] = dict(pin, repository=repository, version=tag[len(program) + 1 :])
    return found


def guests(pin):
    """The guest architectures of a pin (guest = "<arch>" or a list of them), [] for a host
    program, None for a guest value that is neither."""
    guest = pin.get("guest")
    if guest is None:
        return []
    if isinstance(guest, str):
        return [guest]
    if isinstance(guest, list) and guest and all(isinstance(a, str) for a in guest):
        return guest
    return None


def asset(pin, program, arch):
    """(file name, URL) of program's tarball for arch. The '+' of a tag is %2B in the URL."""
    name = f"{program}-{pin['version']}-{arch}.tar.xz"
    quote = urllib.parse.quote
    return name, (
        f"https://github.com/{pin['repository']}/releases/download/"
        f"{quote(pin['tag'], safe='')}/{quote(name, safe='')}"
    )


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def buildinfo(path):
    """A BUILDINFO file's fields (Debian control syntax, one paragraph): {name: value}, a
    multi-line value with its continuation lines joined by newlines."""
    fields, name = {}, None
    for line in Path(path).read_text().splitlines():
        if line[:1] in (" ", "\t") and name:
            fields[name] += "\n" + line.strip()
        elif ":" in line:
            name, value = line.split(":", 1)
            fields[name] = value.strip()
    return fields


def download(url, dest, expected):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and sha256(dest) == expected:
        return
    part = dest.with_name(dest.name + ".part")
    print(f"vendor.py: downloading {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=60) as response, open(part, "wb") as f:
            shutil.copyfileobj(response, f, 1 << 20)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise Error(f"cannot download {url}: {e}") from None
    got = sha256(part)
    if got != expected:
        part.unlink()
        raise Error(f"{url}: SHA-256 {got}, packaging/vendor.toml pins {expected}")
    os.replace(part, dest)


def unpack(tarball, directory, expected):
    """tarball unpacked as directory, unless it already is (its marker holds expected)."""
    marker = directory.with_name(directory.name + ".sha256")
    if directory.is_dir() and marker.is_file() and marker.read_text().strip() == expected:
        return
    work = directory.with_name(directory.name + f".unpack-{os.getpid()}")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    with tarfile.open(tarball, "r:xz") as t:
        t.extractall(work, filter="data")
    shutil.rmtree(directory, ignore_errors=True)
    os.replace(work, directory)
    marker.write_text(expected + "\n")


def override(program):
    """The tarball BEANJAR_VENDOR_<PROGRAM> names, or None."""
    value = os.environ.get("BEANJAR_VENDOR_" + program.upper().replace("-", "_"))
    return Path(value).resolve() if value else None


def tree(program, arch, pin_path=PIN, cache=CACHE):
    """The unpacked tree of program for arch, downloaded and unpacked first where needed. program
    may be PROGRAM:GUEST, a guest pin's tarball for that guest architecture."""
    program, _, guest_arch = program.partition(":")
    local = override(program + (f"-{guest_arch}" if guest_arch else ""))
    if local:
        if not local.is_file() or not local.name.endswith(".tar.xz"):
            raise Error(f"BEANJAR_VENDOR_{program.upper()}: no tarball {local}")
        directory = cache / local.name.removesuffix(".tar.xz")
        unpack(local, directory, sha256(local))
        info = buildinfo(directory / "BUILDINFO")
        if info.get("Program") != program:
            raise Error(f"{local}: BUILDINFO names {info.get('Program')!r}, not {program!r}")
        if guest_arch and info.get("Architecture") != guest_arch:
            raise Error(
                f"{local}: BUILDINFO's architecture {info.get('Architecture')!r}, not {guest_arch!r}"
            )
        return directory
    all_pins = pins(pin_path)
    if program not in all_pins:
        raise Error(f"{pin_path} pins no {program}")
    if arch not in ARCHS:
        raise Error(f"no prebuilt {program} for {arch} (only {', '.join(ARCHS)})")
    pin = all_pins[program]
    if guest_arch and guest_arch not in guests(pin):
        raise Error(f"{pin_path}: {program} has no guest tarball for {guest_arch}")
    if guests(pin):
        arch = guest_arch or guests(pin)[0]  # it runs in the guest, on every host
    name, url = asset(pin, program, arch)
    expected = pin[arch]
    tarball = cache / "download" / name
    directory = cache / name.removesuffix(".tar.xz")
    marker = directory.with_name(directory.name + ".sha256")
    if not (directory.is_dir() and marker.is_file() and marker.read_text().strip() == expected):
        download(url, tarball, expected)
        unpack(tarball, directory, expected)
    info = buildinfo(directory / "BUILDINFO")
    for field, want in (("Program", program), ("Version", pin["version"]), ("Architecture", arch)):
        if info.get(field) != want:
            raise Error(f"{directory}/BUILDINFO: {field} {info.get(field)!r}, expected {want!r}")
    return directory


def main(argv):
    if len(argv) >= 2 and argv[0] == "pinned":
        try:
            found = pins()
        except Error as e:
            sys.exit(f"vendor.py: {e}")
        for program in argv[1:]:
            name, _, guest_arch = program.partition(":")
            if override(name + (f"-{guest_arch}" if guest_arch else "")) or (
                name in found and (not guest_arch or guest_arch in guests(found[name]))
            ):
                print(program)
        return
    if len(argv) < 3 or argv[0] != "tree":
        sys.exit(__doc__)
    try:
        for program in argv[2:]:
            print(tree(program, argv[1]))
    except Error as e:
        sys.exit(f"vendor.py: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
