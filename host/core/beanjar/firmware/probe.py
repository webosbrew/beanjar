# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Probing a firmware: finding it in the host's collections, its model code, its rootfs (paks read
with the official squashfs tools), its identity (model_id.json), release and (chip, release) target,
and the vm-info.toml a VM keeps about the firmware it runs.

firmware_target() is the one place that tells a firmware's target: the chip and release its
manifest.toml records (a manager VM's firmware, resolved at import), else the release its rootfs
names (firmware_release: the codename family of etc/starfish-release) and that release's chip for
the model code in its name (targets.MODELS)."""

import os
import re
import tomllib

from ..runtime import hostconfig

FW_NAME = "05.06.10.01-HE_DTV_WT1H_AFAAABAA"
MODEL_RE = re.compile(
    r"_(?:DTV|LCD|PJT|MNT)_([A-Za-z0-9]+)_"
)  # webOS's _DTV_, SimpleSmart's _LCD_, projectors' _PJT_, monitors' _MNT_
MANIFEST = "manifest.toml"


def find_firmware(name, dirs=None):
    """The directory of the firmware called name in the collections (dirs, default
    hostconfig.firmware_dirs()), the first that has it, or None."""
    for d in hostconfig.firmware_dirs() if dirs is None else dirs:
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
    from . import superpak, targets

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

    from . import targets

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
