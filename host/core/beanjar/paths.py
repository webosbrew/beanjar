# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Where the host runtime finds things: the one place that knows the layout (the checkout today).

Tree(top) is a checkout: its source files (src: the guest-side scripts shared with the guest over
9p, the layers, host/helpers/network) and the build's published artefacts under
build/ (build: the kernel, initramfs, layers, patched QEMU, firmware cache, and the
checkout VM's runtime files). TREE is the checkout this package lives in; the launcher, the unit
and the network wrappers take everything from it, so moving to an install prefix is a change here.

The firmware a launch boots (firmware()): FW_NAME by default, found in the host's firmware
collections (firmware_dirs(): the machine's config.toml, firmware_dirs, default
/srv/beanjar/firmware), the first that has it. FW in the environment wins: a directory (any path
with a '/', e.g. a manager VM's <vm>/firmware), or a bare firmware name looked up there. The host build
takes -Dfirmware=DIR for its firmware cache; target_firmware() is a target's firmware (the one
`vm-launcher firmware-facts` reads).

A firmware boots as a (chip, release) target: the SoC, whose
layer is guest/layers/soc/<chip>, and the webOS release family (its codename), guest/layers/webos/<release>.
firmware_target() is the one place that tells a firmware's target: the chip and release its manifest.toml
records (a manager VM's firmware, resolved at import), else the release its rootfs names
(firmware_release: the codename family of etc/starfish-release) and that release's chip for the
model code in its name (targets.MODELS). The guest build of a target is build/guest-<chip>-<release> and publishes its layers to
build/layers/<chip>-<release> (Tree.layer).
"""

import os
import re
import tomllib

FW_NAME = "05.06.10.01-HE_DTV_WT1H_AFAAABAA"
MODEL_RE = re.compile(
    r"_(?:DTV|LCD|PJT)_([A-Za-z0-9]+)_"
)  # webOS's _DTV_, SimpleSmart's _LCD_, projectors' _PJT_
MANIFEST = "manifest.toml"


class file_lock:
    """fcntl.flock (exclusive) on the file path, made if missing, while the with block runs: the
    lock next to a shared cache (the firmware cache's images, the LLS cache's entries, a library's
    CA) under which a VM checks for, stages and renames in what it needs, so VMs starting at once
    do not race on the same staging files."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        import fcntl

        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.f = open(self.path, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.f.close()


# The machine's settings, every library's: $XDG_CONFIG_HOME/beanjar/config.toml (core.global_config
# has the rest). firmware_dirs: the host's firmware collections, where a bare firmware name is
# looked up, in order; each firmware a directory <collection>/<name> of its paks (and, for
# create --firmware, a manifest.toml).
DEFAULT_FIRMWARE_DIRS = ("/srv/beanjar/firmware",)


def global_config_dir():
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "beanjar"
    )


def global_config_path():
    return os.path.join(global_config_dir(), "config.toml")


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


def find_firmware(name, dirs=None):
    """The directory of the firmware called name in the collections (dirs, default
    firmware_dirs()), the first that has it, or None."""
    for d in firmware_dirs() if dirs is None else dirs:
        if os.path.isdir(os.path.join(d, name)):
            return os.path.join(d, name)
    return None


def model_of(name):
    """The model code in a firmware's name (WT1H), or None."""
    m = MODEL_RE.search(name or "")
    return m.group(1) if m else None


def default_candidates(target, fws):
    """Of fws ({firmware name: its directory}), the names of the firmwares of target that may be its
    default choice: all of them if the whole target is in bring-up, else those not of a bring-up model
    (targets.BRING_UP; W21Z on W21P's k7lp-kisscurl); of several, the one whose paks boot the chip's
    default board (targets.boots_default_board: K8HP's US -global firmware over its Japanese -arib
    one) where exactly one does. The others boot only when named (regress --fw, FW)."""
    from . import targets

    target = tuple(target)
    names = list(fws)
    if not targets.in_bring_up(target):
        names = [n for n in names if model_of(n) not in targets.BRING_UP]
    if len(names) > 1:
        board = [n for n in names if targets.boots_default_board(target, fws[n])]
        if len(board) == 1:
            return board
    return names


def manifest_target(manifest):
    """(chip, release) a manifest.toml (a dict) records; ValueError without them or for a target this
    tree does not boot."""
    from . import targets

    chip, release = manifest.get("chip"), manifest.get("release")
    if not isinstance(chip, str) or not isinstance(release, str):
        raise ValueError("no chip and release")
    if not targets.supported((chip, release)):
        raise ValueError(
            f"{chip}-{release} firmware might be unsupported: no board definition covers it"
        )
    return chip, release


def root_pak(fw_dir):
    """The path of fw_dir's stock root pak: rootfs.pak (webOS), else root.pak (SimpleSmart, whose
    EPK2's paks have four-letter names), or None without either."""
    for name in ("rootfs.pak", "root.pak"):
        if os.path.isfile(os.path.join(fw_dir, name)):
            return os.path.join(fw_dir, name)
    return None


def expanded_image(fw_dir, image="rootfs"):
    """The directory of fw_dir's stock image (rootfs, or bsppart and the like) when fw_dir already
    shows it as files: an epkfs 0.3+ mount has <image>/ beside <image>.pak (root/ beside root.pak
    for SimpleSmart's rootfs) and a webOS 23+ super.pak's partitions under super/. None where fw_dir
    holds only paks (a firmware collection, epkfs --no-expand), so the image is read with unsquashfs
    and squashfuse."""
    if not fw_dir:
        return None
    sup = os.path.join(fw_dir, "super", image)
    if os.path.isfile(os.path.join(fw_dir, "super.pak")) and os.path.isdir(sup):
        return sup
    for name in ("rootfs", "root") if image == "rootfs" else (image,):
        if os.path.isfile(os.path.join(fw_dir, name + ".pak")) and os.path.isdir(
            os.path.join(fw_dir, name)
        ):
            return os.path.join(fw_dir, name)
    return None


def _squashfs_cat(pak, path, offset=0):
    import subprocess

    r = subprocess.run(
        ["unsquashfs", "-cat", "-no-wildcards", "-o", str(offset), pak, path.lstrip("/")],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return r.stdout.decode(errors="replace") if r.returncode == 0 else None


def _squashfs_names(pak):
    """The paths in a squashfs image (unsquashfs -ls), an empty set if it cannot be read."""
    import subprocess

    r = subprocess.run(
        ["unsquashfs", "-ls", "-d", "", pak], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    return (
        {line for line in r.stdout.decode(errors="replace").splitlines() if line.startswith("/")}
        if r.returncode == 0
        else set()
    )


def rootfs_entries(fw_dir, directories):
    """The entries directly in each of directories (paths in the root, without a leading "/") of
    fw_dir's stock rootfs image, listed with unsquashfs -ls: rootfs.pak, or super.pak's logical
    rootfs; a directory that shows the image as files (expanded_image) is listed as it is. The guest
    build decides its shims by these (guest/layers/soc/meson.build). ValueError without an image
    unsquashfs can list."""
    import subprocess
    from . import superpak

    wanted = {d.strip("/") for d in directories}
    root = expanded_image(fw_dir)
    if root:
        return sorted(
            os.path.join(d, n).lstrip("/")
            for d in wanted
            if os.path.isdir(os.path.join(root, d))
            for n in os.listdir(os.path.join(root, d))
        )
    src, offset = os.path.join(fw_dir, "rootfs.pak"), 0
    logical = superpak.images(fw_dir)
    if logical:
        src, offset, _size = logical["rootfs"]
    if not os.path.isfile(src):
        raise ValueError(f"{fw_dir}: no rootfs.pak or super.pak rootfs")
    r = subprocess.run(
        ["unsquashfs", "-ls", "-d", "", "-o", str(offset), src],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        raise ValueError(f"{src}: unsquashfs cannot list it")
    return sorted(
        name
        for name in (line.lstrip("/") for line in r.stdout.decode(errors="replace").splitlines())
        if name and os.path.dirname(name) in wanted
    )


def rootfs_has(fw_dir, path):
    """Whether fw_dir's stock rootfs image (rootfs.pak, or super.pak's logical rootfs) has path (in
    the root, with or without a leading "/"), by unsquashfs's listing of that one path. ValueError
    without an image unsquashfs can list."""
    import subprocess
    from . import superpak

    root = expanded_image(fw_dir)
    if root:
        return os.path.lexists(os.path.join(root, path.strip("/")))
    src, offset = os.path.join(fw_dir, "rootfs.pak"), 0
    logical = superpak.images(fw_dir)
    if logical:
        src, offset, _size = logical["rootfs"]
    if not os.path.isfile(src):
        raise ValueError(f"{fw_dir}: no rootfs.pak or super.pak rootfs")
    path = path.strip("/")
    r = subprocess.run(
        ["unsquashfs", "-l", "-no-wildcards", "-d", "", "-o", str(offset), src, path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        raise ValueError(f"{src}: unsquashfs cannot list it")
    return any(line.strip("/") == path for line in r.stdout.decode(errors="replace").splitlines())


MODEL_ID = "etc/starfish/model_id.json"
_model_ids = {}


def model_id(fw_dir):
    """The firmware's identity, its rootfs's etc/starfish/model_id.json as a dict (otaid, machine,
    region: LG's nyxDeviceInfo module and libdile_tvregion read the broadcast standard from it), read
    with unsquashfs from rootfs.pak or super.pak's logical rootfs; None without one (every firmware
    before jhericurl, a missing or unreadable rootfs). Kept per image while it is unchanged."""
    import json

    from . import superpak

    if not fw_dir:
        return None
    try:
        logical = superpak.images(fw_dir)
    except ValueError:
        return None
    src, offset = (
        (logical["rootfs"].path, logical["rootfs"].offset)
        if logical
        else (os.path.join(fw_dir, "rootfs.pak"), 0)
    )
    try:
        st = os.stat(src)
    except OSError:
        return None
    key = (os.path.realpath(src), offset, st.st_size, st.st_mtime_ns)
    if key not in _model_ids:
        try:
            identity = json.loads(_squashfs_cat(src, MODEL_ID, offset) or "null")
        except ValueError:
            identity = None
        _model_ids[key] = identity if isinstance(identity, dict) else None
    return _model_ids[key]


def release_cfg_model(fw_dir):
    """The modelName of a NetCast-line firmware's mode.pak RELEASE.cfg (SimpleSmart's ATSC-M2 or
    DVB-M2, NetCast 4.5's ATSC-LM14 or DVB-LM14: the broadcast standard, then the platform), read
    with unsquashfs; None without one (every webOS firmware)."""
    mode = os.path.join(fw_dir, "mode.pak") if fw_dir else None
    if not mode or not os.path.isfile(mode):
        return None
    m = re.search(r"^modelName,(\S+)", _squashfs_cat(mode, "RELEASE.cfg") or "", re.M)
    return m.group(1) if m else None


def firmware_release(fw_dir):
    """(the release family, where it was read) of the firmware in fw_dir, read with
    unsquashfs; (None, why) without one. webOS: the codename of
    the first of targets.RELEASE_FILES its rootfs.pak has. SimpleSmart ("simplesmart"), NetCast 4.5
    ("netcast45") and NetCast 4 ("netcast4"), which have no rootfs.pak and no release file: a sysvinit
    root.pak (etc/inittab) and a model pak, mode.pak, whose RELEASE.cfg names the firmware's OTA ID
    (szOtaid), told apart by the vendor pak's graphics library, or NetCast 4's lglib pak's where it
    has no vendor pak."""
    from . import targets, superpak

    src = os.path.join(fw_dir, "rootfs.pak")
    offset = 0
    logical = superpak.images(fw_dir)
    if logical:
        src, offset, _size = logical["rootfs"]
    if os.path.isfile(src):
        for path in targets.RELEASE_FILES:
            text = _squashfs_cat(src, path, offset)
            if text is not None:
                family = targets.release_family(text)
                image = "super.pak's rootfs" if logical else "rootfs.pak"
                return family, f"{image}'s {path}" + ("" if family else " names no release")
        return None, f"no {' or '.join(targets.RELEASE_FILES)} in {os.path.basename(src)}"
    root, mode = os.path.join(fw_dir, "root.pak"), os.path.join(fw_dir, "mode.pak")
    if not os.path.isfile(root):
        return None, "no rootfs.pak"
    if _squashfs_cat(root, "etc/inittab") is None:
        return None, "root.pak has no etc/inittab"
    cfg = _squashfs_cat(mode, "RELEASE.cfg") if os.path.isfile(mode) else None
    ota = re.search(r"^szOtaid,(\S+)", cfg or "", re.M)
    if not ota:
        return None, "root.pak, but no mode.pak with RELEASE.cfg's szOtaid"
    # The vendor's graphics library tells the two lines apart (both RELEASE.cfgs set bSupportNC45):
    # SimpleSmart's is the webOS-style HAL, libhalgal.so; NetCast 4.5's is GAL_GX's libgal.so.
    vend = os.path.join(fw_dir, "vend.pak")
    names = _squashfs_names(vend) if os.path.isfile(vend) else set()
    if "/libhalgal.so" in names:
        return (
            "simplesmart",
            f"root.pak's etc/inittab, vend.pak's libhalgal.so and mode.pak's RELEASE.cfg (OTA ID {ota.group(1)})",
        )
    if "/libgal.so" in names:
        return (
            "netcast45",
            f"root.pak's etc/inittab, vend.pak's libgal.so and mode.pak's RELEASE.cfg (OTA ID {ota.group(1)})",
        )
    if not os.path.isfile(vend):
        # NetCast 4 (H13) ships no vendor pak: its GAL_GX libgal.so is in the lglib pak
        lgli = os.path.join(fw_dir, "lgli.pak")
        if os.path.isfile(lgli) and "/libgal.so" in _squashfs_names(lgli):
            return (
                "netcast4",
                f"root.pak's etc/inittab, no vend.pak, lgli.pak's libgal.so and mode.pak's RELEASE.cfg (OTA ID {ota.group(1)})",
            )
        return None, "root.pak and mode.pak, but no vend.pak and no libgal.so in lgli.pak"
    return None, "root.pak and mode.pak, but vend.pak has neither libhalgal.so nor libgal.so"


VM_INFO = "vm-info.toml"


def vm_info(target, fw_dir, fw_name):
    """What the viewer's title shows of a launch: {platform, version, board, soc, release, firmware}.
    platform and version are the target's release's (targets.PLATFORMS, Release.version: "webOS", "4.5";
    version left out where it is not known); board is the OTA model code (W21P) and firmware the
    version ("03.34.95.01") of the firmware's name ("<version>-HE_DTV_<model>_<id>": its manifest's
    name for a manager VM, else fw_name). A value the name does not carry is left out."""
    from . import targets

    name = fw_name
    try:
        with open(os.path.join(fw_dir, MANIFEST), "rb") as f:
            name = str(tomllib.load(f).get("name") or fw_name)
    except (OSError, ValueError):
        pass
    r = targets.release(target)
    info = {
        "platform": r.platform,
        "version": r.version,
        "board": model_of(name),
        "soc": target[0],
        "release": target[1],
        "firmware": name.split("-", 1)[0] if re.match(r"\d+(\.\d+)+-", name) else None,
    }
    return {k: v for k, v in info.items() if v is not None}


def write_vm_info(run_dir, info):
    """RUN_DIR/vm-info.toml, which the viewer reads next to the D-Bus socket: a TOML table of strings
    (and the layers' array), and the tables of info's dict values ([beanjar]: the launch's host and guest versions, for logs and
    messages; compat.run_info)."""
    import json

    text = "".join(f"{k} = {json.dumps(v)}\n" for k, v in info.items() if not isinstance(v, dict))
    for k, v in info.items():
        if isinstance(v, dict):
            text += f"\n[{k}]\n" + "".join(f"{kk} = {json.dumps(vv)}\n" for kk, vv in v.items())
    path = os.path.join(run_dir, VM_INFO)
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def firmware_target(fw_dir, name=None):
    """(chip, release) of the firmware in fw_dir: its manifest.toml's, if it has one, else the
    release its rootfs names (firmware_release) with that release's chip for the model code of name
    (default: fw_dir's own name). ValueError for a firmware this tree does not boot."""
    import json

    from . import targets, superpak

    path = os.path.join(fw_dir, MANIFEST)
    try:
        with open(path, "rb") as f:
            manifest = tomllib.load(f)
    except FileNotFoundError:
        manifest = None
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from None
    if manifest is not None:
        try:
            return manifest_target(manifest)
        except ValueError as e:
            raise ValueError(f"{path}: {e}") from None
    model = model_of(name or os.path.basename(fw_dir.rstrip("/")))
    release, source = firmware_release(fw_dir)
    if model not in targets.MODELS:
        read = " ".join(x for x in (model, release) if x) or "unknown-model"
        raise ValueError(
            f"{fw_dir}: {read} firmware might be unsupported: no board definition covers it"
        )
    if release is None:
        raise ValueError(f"{fw_dir}: {source}: its webOS release is unknown")
    target = targets.of_firmware(model, release)
    if target is None:
        raise ValueError(
            f"{fw_dir}: {model} {release} firmware might be unsupported: no board definition covers it"
        )
    # W24P also names NT25L firmware. Its model code alone cannot select K8LPN2.
    if model == "W24P":
        machine = (model_id(fw_dir) or {}).get("machine")
        if machine != "K8LPN2":
            raise ValueError(
                f"{fw_dir}: W24P machine {machine or 'unknown'} is not K8LPN2: refused"
            )
    return target


BOARDS = "/usr/share/beanjar/guest/boards"
_BOARD_FILE = re.compile(r"board(-[\w-]+)?\.cmdline")


def installed_board(*p):
    """Where beanjar-guest ships the board file at the checkout-relative path p (as Tree.src takes
    it; a targets.board_cmdline_src), or None for another file. A chip's soc layer's are in
    BOARDS/<chip>/, where every released host reads them; any other board source's
    (targets.Soc.board_src: guest/layers/netcast45/rootfs, ...) at its checkout path in BOARDS
    (BOARDS/guest/layers/netcast45/rootfs/etc/emulator/board.cmdline), beside the chips' and never
    one of them, so two board sources never share a file (packaging/debian/build.py's stage_boards
    puts them there)."""
    p = tuple(part for item in p for part in os.fspath(item).split("/"))
    if p[:1] != ("guest",) or "rootfs" not in p or not _BOARD_FILE.fullmatch(p[-1]):
        return None
    if p[1:3] == ("layers", "soc") and p[4:-1] == ("rootfs", "etc", "emulator"):
        return os.path.join(BOARDS, p[3], p[-1])
    return os.path.join(BOARDS, *p)


class Tree:
    def __init__(self, top):
        self.top = os.path.abspath(top)

    def src(self, *p):
        """A file of the source tree (also the guest's /host over 9p)."""
        if self.installed:
            p = tuple(part for item in p for part in os.fspath(item).split("/"))
            if p[:2] == ("host", "core"):
                return os.path.join(self.top, "core", *p[2:])
            if p[:2] == ("host", "helpers"):
                return os.path.join(self.top, "helpers", *p[2:])
            if p == ("host", "scripts", "vm-launcher"):
                return "/usr/libexec/beanjar/vm-launcher"
            if p[:2] == ("guest", "scripts"):
                return os.path.join("/usr/share/beanjar/guest/scripts", *p[2:])
            board = installed_board(*p)
            if board:
                return board
            if p[:2] == ("guest", "layers"):
                return os.path.join("/usr/share/beanjar/guest/layers", *p[2:])
            if p[:1] == ("guest",):
                return os.path.join("/usr/share/beanjar/guest", *p[1:])
        return os.path.join(self.top, *p)

    def build(self, *p):
        """A published artefact, or the checkout VM's runtime file, under build/."""
        if self.installed:
            if p[:1] == ("components",):
                return os.path.join("/usr/share/beanjar/guest/layers", *p[1:])
            if p == ("guest-installation.toml",):
                return "/usr/share/beanjar/guest/installation.toml"
            if p[:1] in [
                ("Image",),
                ("initramfs.cpio.gz",),
                ("layers",),
                ("layer-images",),
                ("ssh",),
            ]:
                return os.path.join("/usr/share/beanjar/guest", *p)
            if len(p) == 1 and p[0].endswith(".ko"):
                return os.path.join("/usr/share/beanjar/guest/modules", *p)
            if p in [("lls", "lls-probe"), ("lls", "lls-probe.so")]:
                return "/usr/share/beanjar/guest/bin/" + p[1]
            # the experimental x86-64 guest kernel and its initramfs (beanjar-guest)
            if p in [("x86_64", "bzImage"), ("x86_64", "initramfs.cpio.gz")]:
                return os.path.join("/usr/share/beanjar/guest", *p)
            # qemu-user's qemu-arm (beanjar-qemu), by the guest kernel's architecture it runs on:
            # arm64's on every host, amd64's on amd64 hosts
            if p == ("qemu-user", "bin", "qemu-arm"):
                return "/usr/share/beanjar/qemu-user/arm64/qemu-arm"
            if p == ("qemu-user-amd64", "bin", "qemu-arm"):
                return "/usr/share/beanjar/qemu-user/amd64/qemu-arm"
            if p == ("qemu-user-amd64", "bin", "qemu-aarch64"):
                return "/usr/share/beanjar/qemu-user/amd64/qemu-aarch64"
            if p[:2] in [("qemu", "bin"), ("qemu", "libexec")]:
                return os.path.join("/usr/libexec/beanjar", *p[2:])
            if p[:2] == ("qemu", "share"):
                return os.path.join("/usr/share/beanjar", *p[2:])
            if p == ("viewer", "beanjar-viewer"):
                return "/usr/bin/beanjar-viewer"
            if p == ("viewer", "libbeanjar-viewer-core.so"):
                return os.path.join(self.top, *p)
            if p == ("vnc", "beanjar-vnc"):
                return "/usr/bin/beanjar-vnc"
            if p == ("media-helper", "media-helper"):
                return "/usr/libexec/beanjar/media-helper"
            if p == ("network", "beanjar-lan-helper"):
                return "/usr/libexec/beanjar/beanjar-lan-helper"
            if p == ("network", "beanjar-relay"):
                return "/usr/libexec/beanjar/beanjar-relay"
            if p == ("virtpad", "beanjar-virtpad"):
                return "/usr/libexec/beanjar/beanjar-virtpad"
            if p[:1] == ("virtcam",):
                return os.path.join("/usr/lib/beanjar/virtcam", *p[1:])
            if p == ("manager", "beanjar.gresource") or p == ("viewer", "beanjar-viewer.gresource"):
                return os.path.join("/usr/share/beanjar/ui", p[1])
            return self.cache(*p)
        return os.path.join(self.top, "build", *p)

    def vendored_qemu(self):
        """The tree's own patched qemu-system-aarch64 (build/qemu, or beanjar-qemu's when installed)."""
        return self.build("qemu", "bin", "qemu-system-aarch64")

    def qemu(self, env=None):
        """(binary, args): the qemu-system-aarch64 every launch runs and the arguments it needs.
        QEMU in env (os.environ by default) wins; else the vendored QEMU if it is there, else the
        system's from PATH. An installed vendored QEMU takes its firmware from -L."""
        env = os.environ if env is None else env
        vendored = self.vendored_qemu()
        qemu = env.get("QEMU") or (
            vendored
            if os.path.isfile(vendored) and os.access(vendored, os.X_OK)
            else "qemu-system-aarch64"
        )
        args = (
            ["-L", self.build("qemu", "share", "qemu")]
            if self.installed and qemu == vendored
            else []
        )
        return qemu, args

    def vendored_qemu_x86_64(self):
        """The tree's own patched qemu-system-x86_64 (build/qemu, amd64 only), for the x86-64
        guest kernel."""
        return self.build("qemu", "bin", "qemu-system-x86_64")

    def qemu_x86_64(self, env=None):
        """(binary, args) of the experimental x86-64 guest kernel's launch, or (None, []) when there
        is none: QEMU_X86_64 in env (os.environ by default), a local build for development, else
        the vendored one. Never a system QEMU, which lacks the display patches."""
        env = os.environ if env is None else env
        vendored = self.vendored_qemu_x86_64()
        qemu = env.get("QEMU_X86_64") or (
            vendored if os.path.isfile(vendored) and os.access(vendored, os.X_OK) else None
        )
        args = (
            ["-L", self.build("qemu", "share", "qemu")]
            if qemu and self.installed and qemu == vendored
            else []
        )
        return qemu, args

    def qemu_arm(self, env=None, guest_kernel="arm64"):
        """The static qemu-arm with the compat bridge that a USERLAND=qemu-user guest runs its
        32-bit programs with (docs/dev/architecture/qemu-user-bridge.md), or None: QEMU_ARM in env
        (os.environ by default), a local build for development, else beanjar-vendor's qemu-user
        (published once packaging/vendor.toml pins it): build/qemu-user, the aarch64 one, for the
        arm64 guest kernel, build/qemu-user-amd64, the x86-64 one, for the x86-64 guest kernel."""
        env = os.environ if env is None else env
        vendored = "qemu-user-amd64" if guest_kernel == "x86-64" else "qemu-user"
        path = env.get("QEMU_ARM") or self.build(vendored, "bin", "qemu-arm")
        return path if os.path.isfile(path) else None

    def qemu_aarch64(self, env=None, guest_kernel="arm64"):
        """The static qemu-aarch64 that runs the 64-bit ARM programs of a USERLAND=qemu-user guest
        on the x86-64 guest kernel, or None: QEMU_AARCH64 in env (os.environ by default), else the
        one beside the vendored x86-64 qemu-arm (build/qemu-user-amd64). None on the arm64 guest
        kernel, which runs them natively."""
        if guest_kernel != "x86-64":
            return None
        env = os.environ if env is None else env
        path = env.get("QEMU_AARCH64") or self.build("qemu-user-amd64", "bin", "qemu-aarch64")
        return path if os.path.isfile(path) else None

    def guest_kernel(self, guest_kernel="arm64"):
        """(kernel, initramfs) of the guest kernel's architecture: build/Image and
        build/initramfs.cpio.gz, or the x86-64 kernel's build/x86_64/bzImage and
        build/x86_64/initramfs.cpio.gz (guest/kernel/meson.build)."""
        if guest_kernel == "x86-64":
            return self.build("x86_64", "bzImage"), self.build("x86_64", "initramfs.cpio.gz")
        return self.build("Image"), self.build("initramfs.cpio.gz")

    @property
    def installed(self):
        return os.path.isfile(os.path.join(self.top, "installation.toml"))

    def cache(self, *p):
        """Generated host data: checkout build/, or the installed user's XDG cache."""
        if self.installed:
            root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
            return os.path.join(root, "beanjar", *p)
        return self.build(*p)

    def lan_helper(self):
        """link=lan's CAP_NET_ADMIN helper (host/helpers/network/lan-helper.c; BEANJAR_LAN_HELPER: a
        stand-in)."""
        return os.environ.get("BEANJAR_LAN_HELPER") or self.build("network", "beanjar-lan-helper")

    def relay(self):
        """mode=mitm's port relay (host/helpers/network/relay.c; BEANJAR_RELAY: another build's)."""
        return os.environ.get("BEANJAR_RELAY") or self.build("network", "beanjar-relay")

    def virtcam(self):
        """The virtual USB camera (host/helpers/virtcam; BEANJAR_VIRTCAM: another build's)."""
        return os.environ.get("BEANJAR_VIRTCAM") or self.build("virtcam", "beanjar-virtcam")

    def virtpad(self):
        """The virtual USB gamepad (host/helpers/virtpad; BEANJAR_VIRTPAD: another build's)."""
        return os.environ.get("BEANJAR_VIRTPAD") or self.build("virtpad", "beanjar-virtpad")

    def layer(self, target, *p):
        """A layer the guest build of target published: build/layers/<chip>-<release>/..."""
        from . import targets

        if p:
            from . import guestlayers

            selected = guestlayers.select(
                self.build("components"), target, guestlayers.component(target, p[0])
            )
            if selected:
                return os.path.join(selected, *p[1:])
            if self.installed:
                return os.path.join(self.build("components"), ".missing", targets.name(target), *p)
        return self.build("layers", targets.name(target), *p)

    @property
    def pkg_parent(self):
        """The directory to put on PYTHONPATH for `python3 -m beanjar...`."""
        return self.src("host", "core")

    def firmware(self, fw=None):
        """(FW, FW_NAME) for FW as the environment gives it (None or "": the default)."""
        name = FW_NAME
        if fw and "/" in fw:
            name = os.path.basename(fw.rstrip("/"))
            # Managed copies all live in a directory called "firmware". Preserve
            # their import name so native LLS caches also work on hosts without
            # the SDK/qemu-arm used to generate them.
            try:
                with open(os.path.join(fw, MANIFEST), "rb") as f:
                    name = str(tomllib.load(f).get("name") or name)
            except (OSError, ValueError):
                pass
            return fw, name
        if fw:
            name = fw
        candidates = [os.path.join(d, name) for d in self.firmware_dirs()]
        fallback = candidates[-1] if candidates else os.path.join(DEFAULT_FIRMWARE_DIRS[0], name)
        return next((d for d in candidates if os.path.isdir(d)), fallback), name

    def firmware_dirs(self):
        """Where a bare firmware name is looked up, in order: the host's collections
        (firmware_dirs)."""
        return firmware_dirs()

    def target_firmware(self, target):
        """(FW, FW_NAME) of a target's firmware: the default firmware if it resolves to the target
        (firmware_target), else the one firmware in firmware_dirs() that does (the first directory
        that has one), leaving out a bring-up model's on a supported target and, of several, preferring
        the one that boots the chip's default board (default_candidates).
        ValueError for none or several."""
        from . import targets

        def of_target(fw, name):
            try:
                return firmware_target(fw, name) == tuple(target)
            except ValueError:
                return False

        default = self.firmware()
        if of_target(*default):
            return default
        for d in self.firmware_dirs():
            try:
                names = default_candidates(
                    target,
                    {
                        n: os.path.join(d, n)
                        for n in sorted(os.listdir(d))
                        if model_of(n) in targets.MODELS
                        and os.path.isdir(os.path.join(d, n))
                        and of_target(os.path.join(d, n), n)
                    },
                )
            except OSError:
                continue
            if len(names) > 1:
                raise ValueError(
                    f"several firmwares of {targets.name(target)} in {d}: {', '.join(names)}"
                )
            if names:
                return os.path.join(d, names[0]), names[0]
        raise ValueError(
            f"no firmware of {targets.name(target)} in {' or '.join(self.firmware_dirs())}"
        )

    def target(self, fw=None):
        """(chip, release) of FW as the environment gives it (firmware_target; ValueError if unknown)."""
        return firmware_target(*self.firmware(fw))


# host/core/beanjar/paths.py -> the checkout
def runtime_tree():
    here = os.path.dirname(os.path.realpath(__file__))
    d = here
    while True:
        if os.path.isfile(os.path.join(d, "installation.toml")):
            return Tree(d)
        parent = os.path.dirname(d)
        if parent == d:
            return Tree(os.path.dirname(os.path.dirname(os.path.dirname(here))))
        d = parent


TREE = runtime_tree()
