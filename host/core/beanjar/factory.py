# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The state a set has from LG's factory that no firmware update writes: the apps partition's
pre-built data, made by prepare.py into the VM's prepared board data and given to a new appstore
disk (qemu.plan) as its first contents. INFERRED factory state, from the firmware alone (no set's
partition has been seen):

- The far-field voice's wake-word model. trigger_thinq (W22O 43.21.89) loads its Sensory model
  ("Hey LG", hiLG) and its THF config only from /mnt/lg/appstore/preload/voiceinput/thinq/snsr/
  (CTriggerEngineTHF::loadModels: a "thf_config" file and a .snsr of the language, else "snsr DB is
  wrong"); the firmware's own copy in usr/share/voiceinput/triggerThinq/models/snsr/ is read only in
  the factory's assemble mode. Nothing in the rootfs or the update package fills that directory (no
  unit, script or tmpfiles line names it; the EPK's appackage has no voiceinput data), and the only
  other way in is LG's server (sdx app/appResource, a download decrypted with the set's own key). The
  apps partition is the factory's: mount-readwrite-pre.sh looks for a version.txt under
  /mnt/lg/appstore/preload and grows the filesystem "to support pre-built data", and trigger_thinq
  reads preload/voiceinput/version.txt as the "manufacture DB version" (a value the firmware does
  not name, so none is written). So a model with the microphone (targets.Soc.mic) gets the
  firmware's own pair there.

The image is a small ext4 filesystem made with the firmware's own mke2fs.conf (its e2fsprogs is
1.47, as the host's), owned by root as on a set; the guest's mount-readwrite-pre.sh grows it to the
partition with resize2fs at the first boot, the path the factory's image takes."""

import os
import subprocess
import tempfile

from . import lls, targets

SNSR = "usr/share/voiceinput/triggerThinq/models/snsr"
PRELOAD_SNSR = "preload/voiceinput/thinq/snsr"
DISK = "appstore"  # the state disk of the apps partition (/mnt/lg/appstore)
SIZE = 16 << 20  # the image; the guest grows it to the partition
MKE2FS_CONF = "/etc/mke2fs.conf"


class Error(Exception):
    pass


def needed(target):
    """Whether the target's VMs get a factory apps image: its model has the microphone and its
    release an appstore disk."""
    return targets.soc(target).mic and any(n == DISK for n, _ in targets.state_disks(target))


def _names(root, directory):
    """The file names in directory of the firmware's root (its lowest layer that has it first)."""
    names = []
    for pak, prefix in reversed(root.layers):
        r = subprocess.run(
            ["unsquashfs", "-lc", "-no-wildcards", *root._squashfs(pak), prefix + directory],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if r.returncode:
            continue
        for line in r.stdout.splitlines():
            rest = line.split("/" + directory.strip("/") + "/", 1)
            if len(rest) == 2 and rest[1] and "/" not in rest[1] and rest[1] not in names:
                names.append(rest[1])
    return sorted(names)


def firmware_root(fw, target, tree):
    """The firmware's root as the VM stacks it (lls.Root)."""
    return lls.Root(fw, target, tree)


def apps_image(fw, target, tree, out):
    """Makes the factory apps image of the target's firmware fw at out: (the files it holds)."""
    root = firmware_root(fw, target, tree)
    names = _names(root, SNSR)
    if not any("thf_config" in n for n in names) or not any(n.endswith(".snsr") for n in names):
        raise Error(f"{fw}: no wake-word model and THF config in {SNSR}")
    conf = root.read(MKE2FS_CONF)
    with tempfile.TemporaryDirectory(prefix="beanjar-apps-") as tmp:
        top = os.path.join(tmp, "root")
        snsr = os.path.join(top, PRELOAD_SNSR)
        os.makedirs(snsr)
        for d, _, _ in os.walk(top):
            os.chmod(d, 0o755)
        for n in names:
            data = root.read(f"/{SNSR}/{n}")
            if data is None:
                raise Error(f"{fw}: cannot read {SNSR}/{n}")
            with open(os.path.join(snsr, n), "wb") as f:
                f.write(data)
            os.chmod(os.path.join(snsr, n), 0o644)
        env = dict(os.environ)
        if conf is not None:
            with open(os.path.join(tmp, "mke2fs.conf"), "wb") as f:
                f.write(conf)
            env["MKE2FS_CONFIG"] = os.path.join(tmp, "mke2fs.conf")
        img = out + ".tmp"
        with open(img, "wb") as f:
            f.truncate(SIZE)
        # in a user namespace whose root is this user: the files are root's in the image, as on a set
        r = subprocess.run(
            ["unshare", "--map-root-user", "mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096"]
            + ["-d", top, img],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if r.returncode:
            os.unlink(img)
            raise Error(f"mke2fs: {r.stdout.strip() or r.returncode}")
        os.replace(img, out)
    return [f"{PRELOAD_SNSR}/{n}" for n in names]


def seed(prepared_dir, name):
    """The factory image of the state disk name in a VM's prepared board data, or None."""
    path = os.path.join(prepared_dir, DISK + ".img") if name == DISK else None
    return path if path and os.path.isfile(path) else None
