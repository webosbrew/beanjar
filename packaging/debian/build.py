#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Assemble Debian 13 binary packages from explicit, published build inputs (README.md).

The versions are the ones the build lanes published (packaging/identity.py): the host packages take
the host publish directory's source-version.toml, beanjar-guest the guest publish directory's
guest-installation.toml. Git is never run here. --host-only reads no guest input at all,
--guest-only no host input."""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

TOP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar import paths as beanjar_paths  # noqa: E402
from beanjar.firmware import targets as beanjar_targets  # noqa: E402

PREFIX = Path("usr/lib/beanjar")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")
# Debian architecture -> ELF e_machine
ELF_MACHINE = {"arm64": 183, "amd64": 62}
BUILD_ARCH = None  # this machine's Debian architecture (main)


# The launcher runs the vendored QEMU (beanjar-qemu) when it is installed, else Debian's with its
# OpenGL module (paths.Tree.qemu). A Depends alternative is one package, so each of Debian's two
# packages gets its own beanjar-qemu alternative.
QEMU_DEPENDS = "beanjar-qemu | qemu-system-arm, beanjar-qemu | qemu-system-modules-opengl"

# The prebuilt programs of beanjar-vendor (packaging/vendor.toml), which the host build publishes
# as they are: build/qemu, build/ffmpeg, build/ffmpeg-helper and build/neatvnc (meson/vendor.py). Their packages
# depend on what each tree's BUILDINFO says its binaries need (dpkg-shlibdeps where they were
# built), less the libraries the private copies replace: Debian's FFmpeg, which neatvnc was built
# against and which no Beanjar process may load beside the private one, and neatvnc and aml.
VENDOR_REPOSITORY = "https://github.com/mariotaku/beanjar-vendor"
PRIVATE_LIBRARY_PACKAGES = re.compile(
    r"(libavcodec|libavdevice|libavfilter|libavformat|libavutil|libpostproc|libswresample"
    r"|libswscale|libneatvnc|libaml)[0-9]"
)
# The camera (beanjar io usb attach --camera) runs GStreamer as a child: gst-launch-1.0 with
# videotestsrc and videoconvert (plugins-base), timeoverlay (pango, in gstreamer1.0-x) and jpegenc
# (plugins-good).
CAMERA_DEPENDS = (
    "gstreamer1.0-tools, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-x"
)
# Third-party code the beanjar package carries, with its licence.
HOST_COPYRIGHT = """\
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: beanjar

Files: usr/lib/beanjar/virtcam/libusbipdcpp.so usr/lib/beanjar/virtcam/libusbipdcpp_virtual_device.so
Copyright: yunsmall and the usbipdcpp contributors
License: LGPL-3.0
Comment: This product uses usbipdcpp (https://github.com/yunsmall/usbipdcpp), licensed under
 LGPLv3. Built unmodified from that source (subprojects/usbipdcpp.wrap names the revision) and
 linked dynamically. On Debian systems the licence is in /usr/share/common-licenses/LGPL-3.

Files: usr/lib/beanjar/ffmpeg-helper/*
Copyright: the FFmpeg developers
License: LGPL-2.1-or-later
Comment: FFmpeg {version} with only the media helper's decoders and the V4L2 Request API
 hwaccels, as beanjar-vendor builds it ({repository}, release {tag}, whose
 ffmpeg-helper-{version}-source.tar.xz is the corresponding source). FFmpeg's licence texts and
 NOTICE are in /usr/share/doc/beanjar/ffmpeg-helper, with the build's BUILDINFO. On Debian
 systems the LGPL is in /usr/share/common-licenses/LGPL-2.1.
"""
# beanjar-vnc's: neatvnc and aml, ISC (not among Debian's common licences: the texts are shipped),
# and the full FFmpeg build (VNC_FFMPEG_COPYRIGHT).
VNC_COPYRIGHT = """\
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: beanjar

Files: usr/lib/beanjar/neatvnc/*
Copyright: Andri Yngvason and the neatvnc and aml contributors
License: ISC
Comment: neatvnc {version} and aml, with Beanjar's V4L2 H.264 patches (ISC), as beanjar-vendor
 builds them ({repository}, release {tag}). Their licence texts, and NOTICE for the code they
 include from elsewhere under BSD licences, are in /usr/share/doc/beanjar-vnc/neatvnc and
 /usr/share/doc/beanjar-vnc/aml, with the build's BUILDINFO.
"""
VNC_FFMPEG_COPYRIGHT = """\

Files: usr/lib/beanjar/ffmpeg/*
Copyright: the FFmpeg developers
License: GPL-2.0-or-later
Comment: FFmpeg {version} with the V4L2 Request API hwaccels, as beanjar-vendor builds it
 ({repository}, release {tag}, whose ffmpeg-{version}-source.tar.xz is the corresponding source).
 Most of FFmpeg is LGPL-2.1-or-later; built with --enable-gpl and linking GPL libraries, the
 libraries as built are GPL-2.0-or-later. FFmpeg's licence texts and NOTICE are in
 /usr/share/doc/beanjar-vnc/ffmpeg, with the build's BUILDINFO. On Debian systems the GPL is in
 /usr/share/common-licenses/GPL-2.
"""
# beanjar-qemu's: QEMU, GPL-2.0-only with parts under compatible licences, and its firmware images.
QEMU_COPYRIGHT = """\
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: QEMU
Source: https://www.qemu.org/

Files: *
Copyright: Fabrice Bellard and the QEMU contributors
License: GPL-2.0-only
Comment: QEMU {version} with Beanjar's display patches, as beanjar-vendor builds it
 ({repository}, release {tag}, which carries the upstream QEMU tarball the binaries are built
 from; the patches are in the tag's source). Some of QEMU's parts are under other compatible
 licences, and the firmware images in /usr/share/beanjar/qemu each under its own: QEMU's
 COPYING, COPYING.LIB, LICENSE, pc-bios-README and NOTICE are in /usr/share/doc/beanjar-qemu,
 with the build's BUILDINFO. On Debian systems the GPL is in /usr/share/common-licenses/GPL-2.
"""
# beanjar-qemu's for each qemu-user tree it carries (stage_qemu).
QEMU_USER_COPYRIGHT = """
Files: usr/share/beanjar/qemu-user/{arch}/*
Copyright: Fabrice Bellard and the QEMU contributors
License: GPL-2.0-only
Comment: QEMU {version}'s linux-user (qemu-arm, and qemu-aarch64 where it is there) with
 Beanjar's compat bridge patches, static, run inside the {arch} guest kernel, as beanjar-vendor
 builds it ({repository}, release {tag}, which carries the upstream QEMU tarball it is built
 from). QEMU's COPYING, COPYING.LIB, LICENSE and NOTICE are in
 /usr/share/doc/beanjar-qemu/qemu-user/{arch}, with the build's BUILDINFO, which names the
 static libraries linked in.
"""


def guest_range(version, identity):
    """The beanjar-guest versions of version's host-guest contract (identity.contract): 0.2.x for a
    0.2 host, any patch version and build of it. An apt hint: the launcher checks what the guest
    payload has, not its version (host/core/beanjar/guestside/compat.py)."""
    return (
        f"beanjar-guest (>= {identity.contract(version)}), "
        f"beanjar-guest (<< {identity.next_contract(version)})"
    )


def host_dependencies(version, libs, identity):
    """The beanjar package's Depends; libs: its shared libraries' packages (dpkg-shlibdeps). Any
    beanjar-guest of the host's contract will do, whatever its patch version. python3-gi is Gio,
    for the VMs' bus (beanjar/display/dbus.py); GTK is beanjar-ui's. mesa-utils is eglinfo, with which the
    launch names the GL renderer of a VM's virgl display (qemu.gl_renderer_line)."""
    return (
        f"{guest_range(version, identity)}, python3 (>= 3.13), python3 (<< 3.14), python3-gi, "
        "python3-tomlkit, python3-pyelftools, python3-capstone, python3-dnspython, "
        f"dbus-daemon, slirp4netns, iproute2, openssh-client, openssl, {QEMU_DEPENDS}, qemu-user, bubblewrap, "
        f"ca-certificates, squashfs-tools, busybox, libcap2-bin, mesa-utils, {CAMERA_DEPENDS}, {libs}"
    )


def copy(src, dst):
    if not src.exists():
        raise SystemExit(f"missing input: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        for link in src.rglob("*"):
            # A link to /dev/null is systemd's way to mask a unit (guest layers ship such masks).
            if (
                link.is_symlink()
                and os.readlink(link) != "/dev/null"
                and (
                    os.path.isabs(os.readlink(link))
                    or not link.resolve().is_relative_to(src.resolve())
                )
            ):
                raise SystemExit(f"external symlink in input: {link}")
        shutil.copytree(src, dst, ignore=IGNORE, symlinks=True)
    else:
        shutil.copy2(src, dst)


# The Python launchers' first lines: bytecode goes to a per-user cache, $XDG_CACHE_HOME/beanjar/
# pycache (default ~/.cache), never next to the sources under /usr/lib, and no bytecode is
# shipped. Without such a directory of this user's own (it cannot be made, or the nearest
# existing one belongs to someone else: root under sudo with the caller's HOME) nothing is
# written, as before.
PYCACHE = """import os,sys
def _pycache():
    base=os.environ.get("XDG_CACHE_HOME","")
    if not os.path.isabs(base):
        base=os.path.join(os.path.expanduser("~"),".cache")
    path=probe=os.path.join(base,"beanjar","pycache")
    try:
        while not os.path.isdir(probe):
            probe=os.path.dirname(probe)
        if os.stat(probe).st_uid!=os.geteuid():
            return None
        os.makedirs(path,mode=0o700,exist_ok=True)
        return path if os.stat(path).st_uid==os.geteuid() else None
    except OSError:
        return None
sys.pycache_prefix=_pycache()
sys.dont_write_bytecode=sys.pycache_prefix is None
del _pycache
"""


def python_launcher(body):
    """A Python launcher's text: PYCACHE, then body."""
    return f"#!/usr/bin/python3\n{PYCACHE}{body}"


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def control(
    root, name, version, arch, depends, description, recommends=None, suggests=None, replaces=None
):
    size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) // 1024
    write(
        root / "DEBIAN/control",
        f"Package: {name}\nVersion: {version}\nArchitecture: {arch}\n"
        f"Maintainer: Mariotaku <git@mariotaku.me>\nSection: misc\nPriority: optional\n"
        f"Installed-Size: {size}\n"
        + (f"Depends: {depends}\n" if depends else "")
        + (f"Recommends: {recommends}\n" if recommends else "")
        + (f"Suggests: {suggests}\n" if suggests else "")
        + (f"Breaks: {replaces}\nReplaces: {replaces}\n" if replaces else "")
        + f"Homepage: https://github.com/webosbrew/beanjar\n"
        f"Description: {description}\n",
    )
    lines = []
    for path in sorted(root.rglob("*")):
        if (
            path.is_file()
            and not path.is_symlink()
            and "DEBIAN" not in path.relative_to(root).parts
        ):
            lines.append(
                f"{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(root)}\n"
            )
    write(root / "DEBIAN/md5sums", "".join(lines))


def package(root, out, commit_epoch):
    """dpkg-deb of root; file dates SOURCE_DATE_EPOCH, else the recorded commit's time (never today)."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH") or str(commit_epoch)
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "-Zxz", "-z3", "--build", str(root), str(out)],
        check=True,
        env=dict(os.environ, TMPDIR=str(root.parent), SOURCE_DATE_EPOCH=epoch),
    )


def shlibs_depends(text, where="dpkg-shlibdeps"):
    """The Depends list in dpkg-shlibdeps' output (-O: the line shlibs:Depends=..., also a
    substvars file's), or a bare list as given."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found = [line.split("=", 1)[1] for line in lines if line.startswith("shlibs:Depends=")]
    if len(found) == 1 and found[0].strip():
        return found[0].strip()
    if not found and len(lines) == 1 and not re.match(r"[\w.-]+:[\w.-]+=", lines[0]):
        return lines[0]
    raise SystemExit(
        f"{where}: expected one shlibs:Depends=... line (dpkg-shlibdeps -O) or a bare dependency list"
    )


def ui_dependencies(version):
    """beanjar-ui's Depends: the core of the same beanjar, and GTK 4 and libadwaita for Python."""
    return (
        f"beanjar (= {version}), python3 (>= 3.13), python3-gi, gir1.2-gtk-4.0 (>= 4.12), "
        "gir1.2-adw-1 (>= 1.7)"
    )


def vnc_dependencies(version, libs):
    """beanjar-vnc's Depends: the viewer core of the same beanjar, and its own libraries."""
    return f"beanjar (= {version}), {libs}"


def web_dependencies(version):
    """beanjar-web's Depends: the CLI of the same beanjar (its pipes and the remote's layouts),
    beanjar-vnc, aiohttp, and Debian's noVNC and Bootstrap Icons, which it serves as they are."""
    return (
        f"beanjar (= {version}), beanjar-vnc, python3 (>= 3.13), python3-aiohttp, novnc, "
        "bootstrap-icons"
    )


# The Python packages (host/<dir>/pyproject.toml): each ships as source in /usr/lib/beanjar/<dir>,
# with a launcher for each of its scripts in /usr/bin (and [tool.beanjar.libexec]'s in
# /usr/libexec/beanjar) that puts that directory, and the core's, on sys.path.
PYTHON_PACKAGES = {"core": "beanjar", "ui": "beanjar_ui", "web": "beanjar_web"}


def pyproject(directory):
    with open(TOP / "host" / directory / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def entry_points(directory):
    """{(bin directory, name): "module:function"} of host/<directory>'s pyproject.toml."""
    meta = pyproject(directory)
    project = meta["project"]
    found = {}
    for table in ("scripts", "gui-scripts"):
        for name, target in project.get(table, {}).items():
            found[("usr/bin", name)] = target
    for name, target in meta.get("tool", {}).get("beanjar", {}).get("libexec", {}).items():
        found[("usr/libexec/beanjar", name)] = target
    return found


def sys_path(*directories):
    """A launcher's lines putting /usr/lib/beanjar/<directory> on sys.path, in order."""
    return "".join(f'sys.path.insert({i},"/{PREFIX / d}")\n' for i, d in enumerate(directories))


def stage_python(root, directory, skip=()):
    """host/<directory>'s package into root's /usr/lib/beanjar/<directory>, with launchers for its
    entry points (but skip's names); the UI's also see the core's package."""
    package = PYTHON_PACKAGES[directory]
    copy(TOP / "host" / directory / package, root / PREFIX / directory / package)
    path = (directory, "core") if directory == "ui" else (directory,)
    for (bindir, name), target in entry_points(directory).items():
        if name in skip:
            continue
        module, function = target.split(":")
        write(
            root / bindir / name,
            python_launcher(
                f"{sys_path(*path)}from {module} import {function}\nsys.exit({function}())\n"
            ),
            0o755,
        )


def stage_web(work):
    """The beanjar-web tree (architecture all): the beanjar_web package and its launcher."""
    web = work / "beanjar-web"
    stage_python(web, "web")
    copy(TOP / "packaging/debian/README.md", web / "usr/share/doc/beanjar-web/README.md")
    return web


def elf_arch(path):
    """The Debian architecture of the ELF at path, from its e_machine."""
    with path.open("rb") as stream:
        header = stream.read(20)
    machine = int.from_bytes(header[18:20], "little")
    if header[:4] == b"\x7fELF":
        for arch, value in ELF_MACHINE.items():
            if value == machine:
                return arch
    raise SystemExit(f"not an ELF of a packaged architecture: {path}")


def native_dependencies(root, arch, libdirs=(), private=()):
    """libdirs: where libraries of another package that root's ELFs need are (dpkg-shlibdeps
    finds them there, and they add no dependency of their own). private: directories of root
    holding prebuilt libraries (vendor_tree), which are not examined (their Depends are their
    BUILDINFO's) but found like libdirs. For an arch other than this machine's (a cross build),
    dpkg-shlibdeps runs with DEB_HOST_ARCH: it reads that architecture's multiarch libraries and
    packages (dpkg --add-architecture) with its triplet's objdump. Where this machine has Debian's
    FFmpeg, neatvnc or aml libraries too, dpkg-shlibdeps takes those, which belong to a package,
    over the private copies, which do not: merge_depends drops them."""
    private = [Path(p) for p in private]
    elfs = []
    for path in root.rglob("*"):
        if (
            path.is_file()
            and not path.is_symlink()
            and "usr/share" not in str(path.relative_to(root))
            and not any(path.is_relative_to(p) for p in private)
        ):
            with path.open("rb") as stream:
                if stream.read(4) == b"\x7fELF":
                    elfs.append(path)
    with tempfile.TemporaryDirectory(dir=root.parent) as work:
        write(
            Path(work) / "debian/control",
            "Source: beanjar\nSection: misc\nPriority: optional\nMaintainer: Mariotaku <git@mariotaku.me>\n\nPackage: beanjar\nArchitecture: any\nDescription: Beanjar runtime\n",
        )
        output = subprocess.check_output(
            [
                "dpkg-shlibdeps",
                "-O",
                "--ignore-missing-info",
                *[
                    "-l" + str(p)
                    for p in sorted({p.parent for p in elfs} | set(libdirs) | set(private))
                ],
                *["-e" + str(p) for p in elfs],
            ],
            cwd=work,
            text=True,
            env=dict(os.environ, DEB_HOST_ARCH=arch),
        )
    return shlibs_depends(output)


def split_depends(depends):
    """A Depends field's groups (comma-separated), stripped, empty ones dropped."""
    return [group.strip() for group in depends.split(",") if group.strip()]


def merge_depends(*fields):
    """One Depends field from several: the groups of all in order, each once, the private
    copies' Debian twins (PRIVATE_LIBRARY_PACKAGES) dropped, and where several give the same
    package as a single ">=" relation (or none), only the strictest."""
    merged, versions = [], {}
    for field in fields:
        for group in split_depends(field):
            if PRIVATE_LIBRARY_PACKAGES.match(group):
                continue
            m = re.fullmatch(r"([a-z0-9][a-z0-9+.:-]*)(?:\s*\(>=\s*([^)\s]+)\s*\))?", group)
            if not m:
                if group not in merged:
                    merged.append(group)
                continue
            name, version = m.groups()
            if name not in versions:
                versions[name] = version
                merged.append(name)
            elif version and (
                versions[name] is None
                or subprocess.run(
                    ["dpkg", "--compare-versions", version, "gt", versions[name]]
                ).returncode
                == 0
            ):
                versions[name] = version
    return ", ".join(f"{g} (>= {versions[g]})" if versions.get(g) else g for g in merged)


def vendor_tree(hp, program, arch, directory=None):
    """The prebuilt program's tree the host build published (hp/<program>, meson/vendor.py; a guest
    pin's other architecture under hp/<directory>, qemu-user-amd64) and its BUILDINFO, checked to
    be for arch."""
    sys.path.insert(0, str(TOP / "meson"))
    import vendor

    tree = hp / (directory or program)
    if not (tree / "BUILDINFO").is_file():
        raise SystemExit(f"missing input: {tree}/BUILDINFO")
    info = vendor.buildinfo(tree / "BUILDINFO")
    if info.get("Program") != program or info.get("Architecture") != arch:
        raise SystemExit(
            f"{tree}: a prebuilt {info.get('Program')} for {info.get('Architecture')}, "
            f"not {program} for {arch}"
        )
    return tree, info


def vendor_licence(text, info):
    """A copyright stanza template filled in with the program's release (BUILDINFO)."""
    return text.format(
        version=info["Version"],
        tag=f"{info['Program']}-{info['Version']}",
        repository=VENDOR_REPOSITORY,
        arch=info.get("Architecture"),
    )


def stage_libraries(tree, destination):
    """The runtime part of a prebuilt library tree's lib/ (the shared libraries and their soname
    links; no development links, pkg-config files or headers) into destination."""
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted((tree / "lib").iterdir()):
        if re.search(r"\.so\.[0-9]", path.name):
            if path.is_symlink():
                target = os.readlink(path)
                if "/" in target:
                    raise SystemExit(f"external symlink in input: {path}")
                (destination / path.name).symlink_to(target)
            else:
                shutil.copy2(path, destination / path.name)
                (destination / path.name).chmod(0o644)  # Debian's mode for a shared library
                count += 1
    if not count:
        raise SystemExit(f"no shared libraries in {tree}/lib")


def components(source, destination):
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("guest_component_builder", TOP / "meson/components.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    # the selections, their selected variants and each layer's layer.toml, which the installed
    # manager reads (beanjar/guestside/layermeta.py); packaging/shippable.py scans the same
    try:
        paths = module.shipped(source)
    except ValueError as e:
        raise SystemExit(str(e))
    if not any(p.parent.name == "variants" for p in paths):
        raise SystemExit("no independent guest components published")
    for relative in paths:
        copy(source / relative, destination / relative)
    return module


def layer_images(g):
    """The read-only image of every shipped layer (each selected variant, and normal) in
    g/layer-images, under the name the installed launcher looks for (host/core/beanjar/guestside/layerimage.py),
    so an installed host builds none."""
    sys.path.insert(0, str(TOP / "host/core"))
    from beanjar.guestside import layerimage, layermeta

    sources = [(p, ()) for p in sorted((g / "layers").rglob("variants/*")) if p.is_dir()]
    sources.append((g / "layers/normal", (layermeta.FILE,)))
    for src, skip in sources:
        name = layerimage.key(layerimage.layer_digest(str(src), skip)) + ".sqfs"
        try:
            layerimage.build(str(src), str(g / "layer-images" / name), skip)
        except layerimage.Error as e:
            raise SystemExit(f"cannot package: {e}") from None


def rebuild_targets(gp):
    """Compile every target the published components select, in its own build directory
    (build/guest-<target>), so the package never carries a target last built before a change to a
    shared source (a Mesa patch, a wrap). Meson and Ninja rebuild what changed, and meson/fetch.py
    unpacks a source tree again when its wrap or patches did. A target that cannot be built stops."""
    import json

    targets = set()
    for selection in (gp / "components").rglob("selections.json"):
        targets |= set(json.loads(selection.read_text()))
    for target in sorted(targets):
        directory = TOP / "build" / f"guest-{target}"
        if not (directory / "build.ninja").is_file():
            raise SystemExit(f"cannot package: {target} is selected but {directory} is not set up")
        print(f"building {target}", flush=True)
        if subprocess.run(["meson", "compile", "-C", str(directory)]).returncode:
            raise SystemExit(f"cannot package: the build of {target} failed")


def unselected_targets(components_dir):
    """The targets (but the test guest, which scripts/build-packages.sh does not package) that no
    published release component selects: their guest build never ran, and beanjar start would refuse
    them on the installed host (NetCast 4.5's build, with the NetCast SDK's cross file, was missing
    so)."""
    import json

    selected = set()
    for selection in (components_dir / "release").glob("*/selections.json"):
        selected |= set(json.loads(selection.read_text()))
    return [
        name
        for name in map(beanjar_targets.name, beanjar_targets.all_targets())
        if name != "virt-testguest" and name not in selected
    ]


def load_identity():
    """packaging/identity.py, which reads the published source versions (it never runs git here)."""
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("beanjar_identity", TOP / "packaging/identity.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def published_identity(identity, path, guest=False):
    """The releasable identity a build lane published at path (identity.read); SystemExit naming it."""
    try:
        return identity.releasable(identity.read(path, guest=guest), path)
    except identity.Error as e:
        raise SystemExit(f"cannot package: {e}") from None


def stage_host(work, hp, arch, ident):
    """The beanjar and beanjar-vnc trees from the host publish directory and the host's sources."""
    host, vnc = work / "beanjar", work / "beanjar-vnc"
    h = host / PREFIX
    # the beanjar launcher is below: without arguments it opens beanjar-ui's window
    stage_python(host, "core", skip=("beanjar",))
    for directory in ("mstar", "rtk", "ssh", "network"):
        source = TOP / "host/helpers" / directory
        for file in source.glob("*.py"):
            copy(file, h / "helpers" / directory / file.name)
    for relative, destination in (
        ("viewer/libbeanjar-viewer-core.so", h / "viewer/libbeanjar-viewer-core.so"),
        ("viewer/viewer-headless", host / "usr/libexec/beanjar/viewer-headless"),
        ("media-helper/media-helper", host / "usr/libexec/beanjar/media-helper"),
        ("network/beanjar-lan-helper", host / "usr/libexec/beanjar/beanjar-lan-helper"),
        ("network/beanjar-relay", host / "usr/libexec/beanjar/beanjar-relay"),
        # the virtual USB camera and its usbipdcpp libraries, found beside it ($ORIGIN)
        ("virtcam/beanjar-virtcam", h / "virtcam/beanjar-virtcam"),
        ("virtcam/libusbipdcpp.so", h / "virtcam/libusbipdcpp.so"),
        ("virtcam/libusbipdcpp_virtual_device.so", h / "virtcam/libusbipdcpp_virtual_device.so"),
        # the virtual USB gamepad
        ("virtpad/beanjar-virtpad", host / "usr/libexec/beanjar/beanjar-virtpad"),
        ("vnc/beanjar-vnc", vnc / "usr/bin/beanjar-vnc"),
    ):
        copy(hp / relative, destination)
        if elf_arch(destination) != arch:
            raise SystemExit(f"wrong native artifact architecture: {relative}")
    write(
        h / "installation.toml",
        installation_toml(version=ident["version"], commit=ident["commit"], architecture=arch),
    )
    # beanjar: the CLI; without arguments, beanjar-ui's window where that is installed
    write(
        host / "usr/bin/beanjar",
        python_launcher(
            f"{sys_path('core', 'ui')}"
            'if len(sys.argv)==1 and os.path.isdir("/usr/lib/beanjar/ui"):\n'
            "    from beanjar_ui.manager.gui import main\n"
            "else:\n"
            "    from beanjar.cli.main import main\n"
            "sys.exit(main())\n"
        ),
        0o755,
    )
    # beanjar-viewer: the headless viewer (screenshots without a window) runs without beanjar-ui;
    # the window needs it
    write(
        host / "usr/bin/beanjar-viewer",
        python_launcher(
            'if any(a in sys.argv for a in ("--headless","--replay","--screenshot","--screenshot-every","--osd-alpha","--no-osd")):\n'
            '    os.execv("/usr/libexec/beanjar/viewer-headless", ["viewer-headless"]+sys.argv[1:])\n'
            f"{sys_path('ui', 'core')}"
            "try:\n"
            "    from beanjar_ui.viewer.main import main\n"
            "except ImportError as e:\n"
            '    sys.exit(f"beanjar-viewer: the window needs beanjar-ui ({e}); --headless runs without")\n'
            "sys.exit(main(sys.argv[1:]))\n"
        ),
        0o755,
    )
    # the media helper's FFmpeg (beanjar), and the full FFmpeg and neatvnc with aml (beanjar-vnc),
    # each found through its programs' RUNPATH /usr/lib/beanjar/<program>/lib, with its licence
    # texts and BUILDINFO in the package's doc
    for root, program in ((host, "ffmpeg-helper"), (vnc, "ffmpeg"), (vnc, "neatvnc")):
        tree, info = vendor_tree(hp, program, arch)
        stage_libraries(tree, root / PREFIX / program / "lib")
        for doc in sorted((tree / "share/doc").iterdir()):
            copy(doc, root / "usr/share/doc" / root.name / doc.name)
        copy(tree / "BUILDINFO", root / "usr/share/doc" / root.name / program / "BUILDINFO")
    for root in (host, vnc):
        copy(TOP / "packaging/debian/README.md", root / "usr/share/doc" / root.name / "README.md")
    write(
        host / "usr/share/doc/beanjar/copyright",
        vendor_licence(HOST_COPYRIGHT, vendor_tree(hp, "ffmpeg-helper", arch)[1]),
    )
    write(
        vnc / "usr/share/doc/beanjar-vnc/copyright",
        vendor_licence(VNC_COPYRIGHT, vendor_tree(hp, "neatvnc", arch)[1])
        + vendor_licence(VNC_FFMPEG_COPYRIGHT, vendor_tree(hp, "ffmpeg", arch)[1]),
    )
    # link = "lan"'s helper (host/helpers/network/lan-helper.c): CAP_NET_ADMIN only, as a file
    # capability, which dpkg cannot carry; without it link=lan fails at start and says so
    write(
        host / "DEBIAN/postinst",
        '#!/bin/sh\nset -e\nif [ "$1" = configure ]; then\n'
        "  setcap cap_net_admin=ep /usr/libexec/beanjar/beanjar-lan-helper || "
        'echo "beanjar: setcap failed: link=lan is unavailable" >&2\nfi\n',
        0o755,
    )
    return host, vnc


def qemu_user_trees(hp, arch):
    """[(guest architecture, directory)] of the qemu-user trees beanjar-qemu ships on arch: the
    arm64 one (hp/qemu-user, for the arm64 guest kernel) on every host, the amd64 one
    (hp/qemu-user-amd64, the experimental x86-64 guest kernel's) on amd64 only. Each only where
    the host build published it, which it does once packaging/vendor.toml pins qemu-user or
    BEANJAR_VENDOR_QEMU_USER(_AMD64) names a tarball (meson/vendor.py)."""
    wanted = [("arm64", "qemu-user")] + ([("amd64", "qemu-user-amd64")] if arch == "amd64" else [])
    return [(guest, directory) for guest, directory in wanted if (hp / directory).is_dir()]


def stage_qemu(work, hp, arch):
    """The beanjar-qemu tree from the prebuilt QEMU the host build published (hp/qemu, already
    stripped), and its Depends, the tree's BUILDINFO's (libepoxy's dlopened libEGL and libOpenGL
    included). On amd64 it also carries the tree's qemu-system-x86_64, the experimental x86-64
    guest kernel's (paths.Tree.qemu_x86_64), where the tree has one, and on every host the
    published qemu-user trees' static programs (qemu_user_trees), which run inside the guest:
    /usr/share/beanjar/qemu-user/<guest architecture>/qemu-arm (paths.Tree.qemu_arm), and the
    amd64 tree's qemu-aarch64 beside it (paths.Tree.qemu_aarch64)."""
    tree, info = vendor_tree(hp, "qemu", arch)
    q = work / "beanjar-qemu"
    programs = ["bin/qemu-system-aarch64", "libexec/vhost-user-gpu"]
    if arch == "amd64":
        if (tree / "bin/qemu-system-x86_64").is_file():
            programs.append("bin/qemu-system-x86_64")
        else:
            print(f"note: {tree} has no qemu-system-x86_64: beanjar-qemu ships none", flush=True)
    for relative in programs:
        # vhost-user-gpu: QEMU's own backend, the VM's GPU in its own process on an ARM host
        # (qemu.gpu_process); its libraries are a subset of qemu-system-aarch64's
        binary = q / "usr/libexec/beanjar" / Path(relative).name
        copy(tree / relative, binary)
        if elf_arch(binary) != arch:
            raise SystemExit(f"wrong QEMU architecture: {tree / relative}")
    copy(tree / "share/qemu", q / "usr/share/beanjar/qemu")
    copy(tree / "share/doc/qemu", q / "usr/share/doc/beanjar-qemu")
    copy(tree / "BUILDINFO", q / "usr/share/doc/beanjar-qemu/BUILDINFO")
    copyright = vendor_licence(QEMU_COPYRIGHT, info)
    for guest, directory in qemu_user_trees(hp, arch):
        user, user_info = vendor_tree(hp, "qemu-user", guest, directory)
        if not (user / "bin/qemu-arm").is_file():
            raise SystemExit(f"missing input: {user}/bin/qemu-arm")
        # qemu-arm, and the amd64 tree's qemu-aarch64 for the x86-64 guest kernel's 64-bit ARM
        # programs (paths.Tree.qemu_aarch64) where it has one: every program of its bin/
        for program in sorted((user / "bin").iterdir()):
            binary = q / "usr/share/beanjar/qemu-user" / guest / program.name
            copy(program, binary)
            if elf_arch(binary) != guest:
                raise SystemExit(f"wrong qemu-user architecture: {program}")
        doc = q / "usr/share/doc/beanjar-qemu/qemu-user" / guest
        copy(user / "share/doc/qemu-user", doc)
        copy(user / "BUILDINFO", doc / "BUILDINFO")
        copyright += vendor_licence(QEMU_USER_COPYRIGHT, dict(user_info, Architecture=guest))
    write(q / "usr/share/doc/beanjar-qemu/copyright", copyright)
    # qemu-user's programs are static: no Depends of their own
    return q, merge_depends(info["Depends"])


def stage_ui(work, hp):
    """The beanjar-ui tree (architecture all): the beanjar_ui package, its launcher, both GResource
    bundles from the host publish directory, the desktop entry and the icon."""
    ui = work / "beanjar-ui"
    stage_python(ui, "ui")
    for relative in ("manager/beanjar.gresource", "viewer/beanjar-viewer.gresource"):
        copy(hp / relative, ui / "usr/share/beanjar/ui" / Path(relative).name)
    write(
        ui / "usr/share/applications/org.webosbrew.beanjar.desktop",
        "[Desktop Entry]\nType=Application\nName=Beanjar\nComment=TV firmware virtual machines\nExec=beanjar-ui\nIcon=org.webosbrew.beanjar\nTerminal=false\nCategories=System;Emulator;\n",
    )
    copy(
        TOP / "host/ui/data/icon.svg",
        ui / "usr/share/icons/hicolor/scalable/apps/org.webosbrew.beanjar.svg",
    )
    copy(TOP / "packaging/debian/README.md", ui / "usr/share/doc/beanjar-ui/README.md")
    return ui


def stage_kernels(gp, g):
    """The guest kernel and initramfs of beanjar-guest (the experimental x86-64 one is
    beanjar-guest-x86-64's, stage_x86_64)."""
    for file in ("Image", "initramfs.cpio.gz"):
        copy(gp / file, g / file)


def stage_x86_64(work, gp):
    """The beanjar-guest-x86-64 tree (amd64): the experimental x86-64 guest kernel and its
    initramfs, with its modules inside, in /usr/share/beanjar/guest/x86_64 (hardware.guest_kernel =
    "x86-64", amd64 hosts only), where the guest lane published them, as it does on an x86-64 build
    machine (guest/kernel/meson.build, x86_64_kernel). None, with a note, where it did not."""
    if not (gp / "x86_64/bzImage").is_file():
        print(f"note: {gp} has no x86_64/bzImage: no beanjar-guest-x86-64", flush=True)
        return None
    root = work / "beanjar-guest-x86-64"
    for file in ("bzImage", "initramfs.cpio.gz"):
        copy(gp / "x86_64" / file, root / "usr/share/beanjar/guest/x86_64" / file)
    copy(
        TOP / "packaging/debian/README.md",
        root / "usr/share/doc/beanjar-guest-x86-64/README.md",
    )
    return root


def board_files():
    """Every target's board files, checkout-relative (targets.board_cmdline_src): its board.cmdline,
    its boards for a broadcast standard (targets.Soc.broadcast_boards) and for a retail model
    (targets.Soc.model_boards), whichever layer carries them (targets.Soc.board_src)."""
    out = set()
    for target in beanjar_targets.all_targets():
        broadcasts = [b for b, _ in beanjar_targets.soc(target).broadcast_boards]
        for broadcast in [None] + broadcasts:
            out.add(beanjar_targets.board_cmdline_src(target, broadcast))
        for model in beanjar_targets.models(target):
            out.add(beanjar_targets.board_cmdline_src(target, model=model))
    return sorted(out)


def stage_boards(g):
    """The board files in g/boards, where the installed launcher reads them (paths.installed_board)."""
    for rel in board_files():
        installed = beanjar_paths.installed_board(rel)
        if not installed:
            raise SystemExit(f"{rel}: not a board file the installed launcher finds")
        copy(TOP / rel, g / "boards" / os.path.relpath(installed, beanjar_paths.BOARDS))


def stage_targets(g):
    """The board definitions, the target table targets.py loads at import (paths.Tree.src's
    guest/targets), in g/targets: beanjar-guest's, so each guest build carries the definitions
    that describe it. Without what only the guest build reads there (its meson.build, the
    targets' firmware facts)."""
    copy(TOP / "guest/targets", g / "targets")
    shutil.rmtree(g / "targets/facts", ignore_errors=True)
    (g / "targets/meson.build").unlink(missing_ok=True)


def stage_guest(work, gp, stamp):
    """The beanjar-guest tree from the guest publish directory (and the guest's source-only files),
    with its installation.toml: the version its build lane stamped, never the host's."""
    guest = work / "beanjar-guest"
    g = guest / "usr/share/beanjar/guest"
    stage_kernels(gp, g)
    for module in beanjar_targets.payload_modules():
        copy(gp / (module + ".ko"), g / "modules" / (module + ".ko"))
    copy(gp / "lls/lls-probe", g / "bin/lls-probe")
    copy(gp / "lls/lls-probe.so", g / "bin/lls-probe.so")
    copy(gp / "ssh/bin", g / "ssh/bin")
    builder = components(gp / "components", g / "layers")
    copy(TOP / "guest/layers/normal", g / "layers/normal")
    layer_images(g)
    copy(TOP / "guest/layers/binds.conf", g / "layers/binds.conf")
    for file in (TOP / "guest/scripts").glob("*.sh"):
        copy(file, g / "scripts" / file.name)
    stage_boards(g)
    stage_targets(g)
    write(
        g / "installation.toml",
        installation_toml(
            version=stamp["version"], commit=stamp["commit"], payload_sha256=builder.digest(g)
        ),
    )
    copy(TOP / "packaging/debian/README.md", guest / "usr/share/doc" / guest.name / "README.md")
    return guest


def installation_toml(**fields):
    """An installation.toml: a flat TOML table (the manager reads the host's, the launcher the guest's)."""
    return "".join(
        f"{k} = {v}\n" if isinstance(v, int) else f'{k} = "{v}"\n' for k, v in fields.items()
    )


def audit_private(*roots):
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("audit", TOP / "packaging/shippable.py")
    audit = module_from_spec(spec)
    spec.loader.exec_module(audit)
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink() and audit.PRIVATE.search(path.read_bytes()):
                raise SystemExit(f"private key in payload: {path.relative_to(root)}")


X86_64_DESCRIPTION = (
    "Beanjar experimental x86-64 guest kernel\n"
    " The x86-64 kernel and initramfs for running ARM webOS firmware on an x86-64 host\n"
    " (guest_kernel=x86-64 in a VM's settings): the ARM userland runs under qemu-arm\n"
    " from beanjar-qemu. Experimental; arm64 hosts do not need it."
)


def package_guest(a, identity, work):
    """beanjar-guest from the guest publish directory, its version the guest lane's."""
    gp = (a.guest_publish or a.publish).resolve()
    if gp == (TOP / "build").resolve() and not a.no_rebuild:
        rebuild_targets(gp)
    missing = unselected_targets(gp / "components")
    if missing:
        raise SystemExit(
            f"cannot package: no guest build published {', '.join(missing)} "
            "(scripts/build-packages.sh's guest lane builds every target)"
        )
    subprocess.run(
        [
            sys.executable,
            str(TOP / "packaging/shippable.py"),
            str(gp),
        ],
        check=True,
    )
    stamp = published_identity(identity, gp / "guest-installation.toml", guest=True)
    subprocess.run(["dpkg", "--validate-version", stamp["version"]], check=True)
    guest = stage_guest(work, gp, stamp)
    audit_private(guest)
    subprocess.run(
        [
            sys.executable,
            str(TOP / "packaging/shippable.py"),
            str(guest / "usr/share/beanjar/guest"),
        ],
        check=True,
    )
    control(
        guest,
        "beanjar-guest",
        stamp["version"],
        "all",
        "",
        "Beanjar virtual TV kernel and guest components",
    )
    package(guest, a.output / f"beanjar-guest_{stamp['version']}_all.deb", stamp["commit_epoch"])
    x86_64 = stage_x86_64(work, gp)
    if x86_64:
        audit_private(x86_64)
        control(
            x86_64,
            "beanjar-guest-x86-64",
            stamp["version"],
            "amd64",
            f"beanjar-guest (= {stamp['version']}), beanjar-qemu",
            X86_64_DESCRIPTION,
        )
        package(
            x86_64,
            a.output / f"beanjar-guest-x86-64_{stamp['version']}_amd64.deb",
            stamp["commit_epoch"],
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", type=Path, default=TOP / "build")
    parser.add_argument(
        "--host-publish", type=Path, help="native host artifacts (default --publish)"
    )
    parser.add_argument(
        "--guest-publish", type=Path, help="shared guest artifacts (default --publish)"
    )
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="do not compile the guest targets first (build/ as it is; the default publish only)",
    )
    parser.add_argument(
        "--architecture",
        choices=tuple(ELF_MACHINE),
        help="the host artifacts' architecture (default: that of the published viewer-headless)",
    )
    parser.add_argument(
        "--host-only",
        action="store_true",
        help="only beanjar, beanjar-ui, beanjar-vnc, beanjar-web and beanjar-qemu; no guest input is read",
    )
    parser.add_argument(
        "--guest-only",
        action="store_true",
        help="only beanjar-guest; no host input is read",
    )
    parser.add_argument(
        "--shlibs-file",
        type=Path,
        help="dpkg-shlibdeps -O's output (shlibs:Depends=...) in the native build lane",
    )
    parser.add_argument("--vnc-shlibs-file", type=Path, help="as --shlibs-file, for beanjar-vnc")
    parser.add_argument("--output", type=Path, default=TOP / "build/packages")
    a = parser.parse_args()
    global BUILD_ARCH
    BUILD_ARCH = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    if BUILD_ARCH not in ELF_MACHINE or sys.version_info[:2] != (3, 13):
        raise SystemExit("Debian 13 and Python 3.13 required")
    if 'VERSION_ID="13"' not in Path("/etc/os-release").read_text():
        raise SystemExit("Debian 13 required")
    if a.host_only and a.guest_only:
        parser.error("--host-only and --guest-only exclude each other")
    identity = load_identity()
    a.output = a.output.resolve()
    if a.guest_only:
        a.output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="beanjar-deb-", dir=a.output) as work:
            package_guest(a, identity, Path(work))
        return
    hp = (a.host_publish or a.publish).resolve()
    # the architecture the host lane built for (build/publish-arm64: the arm64 cross build's)
    arch = a.architecture or elf_arch(hp / "viewer/viewer-headless")
    host_ident = published_identity(identity, hp / "source-version.toml")
    version = host_ident["version"]
    subprocess.run(["dpkg", "--validate-version", version], check=True)
    a.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="beanjar-deb-", dir=a.output) as work:
        work = Path(work)
        host, vnc = stage_host(work, hp, arch, host_ident)
        audit_private(host, vnc)
        helper_ffmpeg = host / PREFIX / "ffmpeg-helper/lib"
        libs = merge_depends(
            (
                shlibs_depends(a.shlibs_file.read_text(), a.shlibs_file)
                if a.shlibs_file
                else native_dependencies(host, arch, private=[helper_ffmpeg])
            ),
            vendor_tree(hp, "ffmpeg-helper", arch)[1]["Depends"],
        )
        control(
            host,
            "beanjar",
            version,
            arch,
            host_dependencies(version, libs, identity),
            "Beanjar TV firmware VMs: the core, its command line and the host runtime",
            recommends=f"beanjar-ui (= {version})",
            # the experimental x86-64 guest kernel (guest lane, amd64 hosts only): never pulled in
            suggests="beanjar-guest-x86-64" if arch == "amd64" else None,
        )
        vnc_libs = merge_depends(
            (
                shlibs_depends(a.vnc_shlibs_file.read_text(), a.vnc_shlibs_file)
                if a.vnc_shlibs_file
                else native_dependencies(
                    vnc,
                    arch,
                    [host / PREFIX / "viewer"],
                    private=[vnc / PREFIX / "ffmpeg/lib", vnc / PREFIX / "neatvnc/lib"],
                )
            ),
            vendor_tree(hp, "ffmpeg", arch)[1]["Depends"],
            vendor_tree(hp, "neatvnc", arch)[1]["Depends"],
        )
        control(
            vnc,
            "beanjar-vnc",
            version,
            arch,
            vnc_dependencies(version, vnc_libs),
            "Beanjar VMs' TV picture as a VNC server (neatvnc), without a desktop",
            # the full FFmpeg, /usr/lib/beanjar/ffmpeg, was beanjar's before the media helper
            # had its own
            replaces=f"beanjar (<< {version})",
        )
        if not a.host_only:
            package_guest(a, identity, work)
        package(host, a.output / f"beanjar_{version}_{arch}.deb", host_ident["commit_epoch"])
        package(vnc, a.output / f"beanjar-vnc_{version}_{arch}.deb", host_ident["commit_epoch"])
        ui = stage_ui(work, hp)
        audit_private(ui)
        control(
            ui,
            "beanjar-ui",
            version,
            "all",
            ui_dependencies(version),
            "Beanjar's desktop windows: the VM manager and the viewer",
        )
        package(ui, a.output / f"beanjar-ui_{version}_all.deb", host_ident["commit_epoch"])
        web = stage_web(work)
        audit_private(web)
        control(
            web,
            "beanjar-web",
            version,
            "all",
            web_dependencies(version),
            "Beanjar VMs in a browser: noVNC and a popup TV remote",
        )
        package(web, a.output / f"beanjar-web_{version}_all.deb", host_ident["commit_epoch"])
        qemu, qemu_depends = stage_qemu(work, hp, arch)
        audit_private(qemu)
        control(qemu, "beanjar-qemu", version, arch, qemu_depends, "Beanjar patched QEMU")
        package(qemu, a.output / f"beanjar-qemu_{version}_{arch}.deb", host_ident["commit_epoch"])


if __name__ == "__main__":
    main()
