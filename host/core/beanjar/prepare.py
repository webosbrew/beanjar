# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What a VM's start needs from its firmware, its model's board and its board settings, worked out
ahead (docs/dev/architecture/manager.md, "Prepared board data"): when the VM is made (newvm.create),
when a setting it depends on changes (core.Library.configure: fw, broadcast, debug_mode, serial,
country;
identity reset; a local clone), or with `beanjar prepare NAME`. A start only reads it.

It is kept in the VM's directory, <vm>/prepared/ (the parent of its RUN_DIR: the checkout's
development VMs and the regress instances have one too):
  prepared.json   what it was made from (inputs, files) and what start reads besides the images:
                  the viewer's local key (lls.local_key), the panel's refresh (lls.panel_rate),
                  and the chip modules' parameters and host
                  share files from the firmware (targets.derived_params, targets.derived_files), with
                  lgemu's light sensor and ToF motion sensor where the board's tool options have
                  them (targets.light_dev, targets.tof_dev) and its TEE where the firmware has
                  OP-TEE's client (targets.tee_device)
  partinfo.img    the firmware's eMMC partition map as the partinfo disk holds it (partmap.build,
                  whose cache, the VM's FW_CACHE, keeps the original)
  eeprom.img      what a blank EEPROM state disk becomes (nvm.new_chip), when there is a chip
  records.txt     LLS's probe output for the board (webOS 5 on): the debugstatus item a programmed
                  chip takes when the VM's debug mode changes
  macadr.img      the macadr partition as the factory leaves it (macadr.py: the MAC and the
                  identity's LGUDID), where the release's LG code reads the LGUDID (Release.lgudid);
                  the launcher attaches it with its writes discarded at power-off
  webos-verity-certs  the firmware kernel's built-in X.509 certificates (firmware.KERNEL_CERTS), where
                  the firmware has an adaptive partition: the share's /host/webos-verity-certs,
                  which the guest's webos-verity checks the partition's table against

What it depends on, and how a start tells it is stale without reading the firmware: the inputs
(this module's VERSION and lls.VERSION, the firmware's directory, the target, the effective
broadcast standard, the VM's serial, MAC, LGUDID and debug mode, and a digest of the target's table rows), all
cheap to work out; and the size and mtime of every file it came from: each pak of the firmware,
the board's board.cmdline and the helpers whose code ran (lls.py's probe and, where the firmware's
LLS is a nyx module, the guest kernel, initramfs and lgemu.ko, which lls.cache_dir keys its run
on; rpc-table.py, mmap-table.py, mboot-mmap.py and the files they made). A
stat of each, no reads. Paths in the VM's directory are kept relative to it, so a renamed VM's
record holds.

A start (qemu.plan, ensure) whose record is missing or stale prepares it again first, and says so
and why: the development and regress VMs have no create step, and an update of the tree (a
board.cmdline, a guest build) changes the inputs of every VM at once. It is never silent.

  python3 -m beanjar.prepare VM-DIR      prepare a VM of a library (what `beanjar prepare` runs)
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys

from . import firmware, fwcache, lls, macadr, nvm, partmap, paths, targets, vmidentity
from .base import identity_dir

VERSION = 8
DIR = "prepared"
# the panel's refresh where the firmware's own decode of the board is not run (before webOS 5)
DEFAULT_PANEL_HZ = 60
RECORD = "prepared.json"
PARTINFO = "partinfo.img"
EEPROM = "eeprom.img"
RECORDS = "records.txt"
MACADR = "macadr.img"
# the firmware kernel's built-in certificates (firmware.KERNEL_CERTS), /host/webos-verity-certs in the guest
VERITY_CERTS = "webos-verity-certs"


class Error(Exception):
    pass


class Job:
    """One VM's preparation: its firmware (fw, fw_name, target, broadcast, model: the retail model
    whose board it boots, targets.board_model, None for the broadcast standard's), its board settings
    (serial, debug_mode: DEBUG_MODES' number or None), its FW_CACHE (cache), its directory (vm_dir:
    RUN_DIR's parent) and the tree."""

    def __init__(self, env, tree=paths.TREE):
        self.tree = tree
        self.fw, self.fw_name = tree.firmware(env.get("FW"))
        try:
            self.target = paths.firmware_target(self.fw, self.fw_name)
        except ValueError as e:
            raise Error(str(e)) from None
        try:
            self.model = targets.board_model(
                self.target, self.fw, env.get("BROADCAST"), env.get("MODEL")
            )
            self.broadcast = targets.board_broadcast(
                self.target, self.fw, env.get("BROADCAST"), self.model
            )
            self.debug_mode = nvm.debug_mode_number(env.get("DEBUG_MODE"))
        except (targets.Error, nvm.Error) as e:
            raise Error(str(e)) from None
        self.serial = env.get("SERIAL") or ""
        # the VM's country (countries.py): a new LLS chip's factory countryName
        self.country = (env.get("COUNTRY") or "").strip().upper()
        self.cache = env.get("FW_CACHE") or fwcache.default_cache(tree)
        run_dir = os.path.join(tree.top, env.get("RUN_DIR") or "build")
        self.vm_dir = os.path.dirname(os.path.abspath(run_dir))
        self.dir = os.path.join(self.vm_dir, DIR)
        # the VM's identity directory (IDENTITY_DIR, as the launcher's) and MAC (MAC, else the
        # board's, qemu.guest_mac): the macadr partition's
        ident = env.get("IDENTITY_DIR")
        self.identity_dir = os.path.join(tree.top, ident) if ident else identity_dir(self.vm_dir)
        self.mac = (env.get("MAC") or "").lower()
        if not self.mac and targets.release(self.target).lgudid:
            from . import qemu

            self.mac = qemu.guest_mac(self.target, tree, self.broadcast, self.model).lower()

    def rel(self, path):
        """path relative to the VM's directory if it is in it, else absolute."""
        path = os.path.abspath(path)
        inside = os.path.relpath(path, self.vm_dir)
        return path if inside.startswith("..") else inside

    def abs(self, path):
        return os.path.normpath(os.path.join(self.vm_dir, path))

    def inputs(self):
        """What the record depends on but its files: cheap, nothing of the firmware read."""
        rel = targets.release(self.target, self.fw)
        s = targets.soc(self.target)
        rows = repr(
            (
                rel.partmap,
                rel.nvm,
                rel.paks,
                rel.pak_lowers,
                targets.state_disks(self.target),
                targets.emmc_disks(self.target),
                s.modules,
                s.continent,
                s.inputs,
                s.light_bus,
                s.tof_bus,
                # only where set: the records of the other chips' VMs stay current
                *((s.led_micom_bus,) if s.led_micom_bus is not None else ()),
                *((s.firmware_params,) if s.firmware_params else ()),
            )
        )
        return {
            "version": VERSION,
            "lls": lls.VERSION,
            "firmware": self.rel(self.fw),
            "firmware_name": self.fw_name,
            "target": targets.name(self.target),
            "broadcast": self.broadcast or "",
            "serial": self.serial,
            "debug_mode": self.debug_mode,
            # only where set: the records of VMs without one stay current
            **({"country": self.country} if self.country else {}),
            **(self.lgudid_inputs() if targets.release(self.target).lgudid else {}),
            **({"model": self.model} if self.model else {}),
            "tables": hashlib.sha256(rows.encode()).hexdigest()[:16],
        }

    def lgudid_inputs(self):
        """The macadr partition's inputs: the MAC and the identity's LGUDID ("" while it has none)."""
        ids = vmidentity.read_lgudid(self.identity_dir)
        return {"mac": self.mac, "lgudid": ":".join(ids) if ids else ""}

    def kernel_certs(self):
        """The firmware's stored kernel certificates (its manifest's kernel_certs, a file name in
        its directory): the path, or None where it names none or the file is not there."""
        name = (firmware.manifest(self.fw) or {}).get("kernel_certs")
        if not isinstance(name, str) or not name or os.path.basename(name) != name:
            return None
        path = os.path.join(self.fw, name)
        return path if os.path.isfile(path) else None

    def base_files(self):
        """The files every record depends on: the firmware's paks (and its kernel certificates)
        and the board's board.cmdline."""
        out = [os.path.join(self.fw, n) for n in targets.firmware_paks(self.fw)]
        if targets.adaptive_partition(self.target, self.fw) and self.kernel_certs():
            out.append(self.kernel_certs())
        out.append(
            self.tree.src(targets.board_cmdline_src(self.target, self.broadcast, self.model))
        )
        return out

    def stamps(self, files):
        """{path (rel): [size, mtime_ns]} of files; a missing one is [-1, 0]."""
        out = {}
        for f in files:
            try:
                st = os.stat(f)
                out[self.rel(f)] = [st.st_size, st.st_mtime_ns]
            except OSError:
                out[self.rel(f)] = [-1, 0]
        return out


def _read(job):
    try:
        with open(os.path.join(job.dir, RECORD)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def stale(job):
    """Why the VM's prepared board data cannot be used as it is (a short phrase), or None."""
    record = _read(job)
    if not record:
        return "missing"
    old = record.get("inputs") or {}
    new = job.inputs()
    changed = [k for k in new if old.get(k) != new[k]]
    if changed:
        names = {"tables": "the target's table rows", "lls": "lls.py's VERSION"}
        return "made with another " + ", ".join(names.get(k, k) for k in changed)
    recorded = record.get("files") or {}
    now = job.stamps([job.abs(p) for p in recorded] + job.base_files())
    if now != recorded:
        diff = sorted(set(now) ^ set(recorded)) or sorted(
            p for p in now if now[p] != recorded.get(p)
        )
        return "made from other files (" + ", ".join(os.path.basename(p) for p in diff[:3]) + ")"
    own = [PARTINFO] if targets.board_data(job.target) else []
    own += [VERITY_CERTS] if VERITY_CERTS in (record.get("derived_files") or {}) else []
    for name in own + [n for n in (EEPROM, RECORDS, MACADR) if record.get(n)]:
        if not os.path.isfile(os.path.join(job.dir, name)):
            return f"missing its {name}"
    return None


class Prepared:
    """A VM's prepared board data, as a start reads it."""

    def __init__(self, job, record):
        self.dir = job.dir
        # None for a target without LG's board data (the test guest's): no partition map
        self.partmap = (
            None if record.get("board_data") is False else os.path.join(job.dir, PARTINFO)
        )
        self.local_key = record.get("local_key")
        self.panel_hz = record.get("panel_hz") or DEFAULT_PANEL_HZ
        self.derived_params = {k: tuple(v) for k, v in (record.get("derived_params") or {}).items()}
        # a relative path is one in the prepared directory (VERITY_CERTS), which a rename moves
        self.derived_files = {
            k: os.path.join(job.dir, v) for k, v in (record.get("derived_files") or {}).items()
        }
        image = records = None
        if record.get(EEPROM):
            with open(os.path.join(job.dir, EEPROM), "rb") as f:
                image = f.read()
        if record.get(RECORDS):
            with open(os.path.join(job.dir, RECORDS)) as f:
                records = f.read()
        self.chip = nvm.Chip(image, records, record.get("eeprom_note"))
        # the macadr partition's image (macadr.py), None where the release reads no LGUDID
        self.macadr = os.path.join(job.dir, MACADR) if record.get(MACADR) else None


def _helpers(job, nyx):
    """The tree's files whose code ran to make the record (a change makes it stale)."""
    out = []
    if targets.release(job.target).nvm.lls:
        out.append(job.tree.build("lls", "lls-probe"))
        if nyx:
            out += [
                job.tree.build("Image"),
                job.tree.build("initramfs.cpio.gz"),
                job.tree.build("lgemu.ko"),
            ]
    derived = {m.derived for m in targets.soc(job.target).modules if m.derived}
    if "rpc-table" in derived:
        out.append(targets.RPC_TABLE)
    if "mstar-mmap" in derived:
        out.append(targets.MMAP_TABLE)
    if "mstar-mboot" in derived:
        out.append(targets.MBOOT_MMAP)
    return out


def make(job, say=print):
    """Work out the VM's board data from its firmware and store it in job.dir (replacing what was
    there as a whole). Error if a start could not use it either."""
    os.makedirs(job.vm_dir, exist_ok=True)
    with paths.file_lock(job.dir + ".lock"):
        new = job.dir + ".new"
        shutil.rmtree(new, ignore_errors=True)
        os.makedirs(new)
        try:
            record = _make(job, new, say)
            with open(os.path.join(new, RECORD), "w") as f:
                json.dump(record, f, indent=1, sort_keys=True)
                f.write("\n")
            shutil.rmtree(job.dir, ignore_errors=True)
            os.replace(new, job.dir)
        finally:
            shutil.rmtree(new, ignore_errors=True)
    return Prepared(job, record)


def _verity_certs(job, new, record):
    """The firmware kernel's built-in certificates, which its webos-verity checks the adaptive
    partition's table against, into new: the file the firmware keeps (firmware.KERNEL_CERTS, read
    at its import; nothing here decodes kernel.pak), where it has the partition."""
    if not targets.adaptive_partition(job.target, job.fw):
        return
    src = job.kernel_certs()
    if not src:
        raise Error(
            f"the firmware {job.fw_name} has an adaptive partition but keeps no kernel "
            f"certificates ({firmware.KERNEL_CERTS}, named by its manifest's kernel_certs): "
            "import it again where epk2extract is installed"
        )
    try:
        shutil.copyfile(src, os.path.join(new, VERITY_CERTS))
    except OSError as e:
        raise Error(f"the kernel's certificates: {e}") from None
    record["derived_files"][VERITY_CERTS] = VERITY_CERTS  # in the VM's directory


def _make(job, new, say):
    if not targets.board_data(job.target):
        # the test guest: no partition map, no EEPROM, nothing derived from a firmware
        return {
            "board_data": False,
            "panel_hz": DEFAULT_PANEL_HZ,
            "inputs": job.inputs(),
            "files": job.stamps(job.base_files()),
        }
    try:
        src = partmap.build(job.fw, job.target, job.cache, job.tree)
        shutil.copyfile(src, os.path.join(new, PARTINFO))
    except (partmap.Error, fwcache.Error, OSError) as e:
        raise Error(f"partition map: {e}") from None
    chip = nvm.new_chip(
        job.fw,
        job.target,
        job.serial,
        job.tree,
        say,
        job.fw_name,
        broadcast=job.broadcast,
        debug_mode=job.debug_mode,
        country=job.country or None,
        cache=job.cache,
        model=job.model,
    )
    record = {"eeprom_note": chip.note}
    # the panel's refresh (viewer and beanjar-vnc output rate, vm-info.toml's panel_hz): the
    # firmware's own decode of the board (webOS 5 on), else a 60 Hz panel
    record["panel_hz"] = chip.panel_hz or DEFAULT_PANEL_HZ
    if chip.image is not None:
        with open(os.path.join(new, EEPROM), "wb") as f:
            f.write(chip.image)
        record[EEPROM] = True
    if chip.records is not None:
        with open(os.path.join(new, RECORDS), "w") as f:
            f.write(chip.records)
        record[RECORDS] = True
    rel = targets.release(job.target)
    if rel.nvm.lls or rel.nvm.local_key:
        try:
            record["local_key"] = lls.local_key(
                job.fw, job.target, job.tree, broadcast=job.broadcast, model=job.model
            )
        except (lls.Error, OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
            raise Error(f"the model's local key option: {e}") from None
    try:
        record["derived_params"] = {
            k: list(v) for k, v in targets.derived_params(job.target, job.fw, job.fw_name).items()
        }
        files = targets.derived_files(job.target, job.fw, job.fw_name)
    except targets.Error as e:
        raise Error(str(e)) from None
    record["derived_files"] = dict(files)
    _verity_certs(job, new, record)
    # /dev/tee0 with no TA where the firmware has OP-TEE's client and no chip module has its TAs
    try:
        tee = targets.tee_device(job.target, job.fw)
    except targets.Error as e:
        raise Error(str(e)) from None
    if tee:
        record["derived_params"].setdefault("lgemu", []).extend(tee)
    # the parameters of this firmware line's own board (targets.firmware_module_params: W21Z's DLP
    # controller on W21P's K7LP)
    for module, words in targets.firmware_module_params(job.target, job.fw_name).items():
        record["derived_params"].setdefault(module, []).extend(words)
    # the board's own light sensor: where its tool options say the set has one (targets.light_dev)
    if targets.soc(job.target).light_bus is not None:
        try:
            eye = lls.digital_eye(
                job.fw, job.target, job.tree, broadcast=job.broadcast, model=job.model
            )
        except (lls.Error, OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
            raise Error(f"the model's light sensor option: {e}") from None
        light = targets.light_dev(job.target, eye)
        if light:
            record["derived_params"].setdefault("lgemu", []).append("i2c_dev=" + light)
    # the board's own ToF motion sensor: where its tool options name one (targets.tof_dev)
    if targets.soc(job.target).tof_bus is not None:
        try:
            sensor = lls.motion_sensor(
                job.fw, job.target, job.tree, broadcast=job.broadcast, model=job.model
            )
        except (lls.Error, OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
            raise Error(f"the model's motion sensor option: {e}") from None
        tof = targets.tof_dev(job.target, sensor)
        if tof:
            record["derived_params"].setdefault("lgemu", []).append("i2c_dev=" + tof)
    # the board's own gaming LED micom: where its tool type is one whose sets carry it
    # (targets.led_micom_dev)
    if targets.soc(job.target).led_micom_bus is not None:
        try:
            tool = lls.tool_type(
                job.fw, job.target, job.tree, broadcast=job.broadcast, model=job.model
            )
        except (lls.Error, OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
            raise Error(f"the model's tool type: {e}") from None
        micom = targets.led_micom_dev(job.target, tool)
        if micom:
            record["derived_params"].setdefault("lgemu", []).append("i2c_dev=" + micom)
    # the macadr partition, where the release reads the LGUDID (Release.lgudid)
    if targets.release(job.target).lgudid:
        macadr_image(job, os.path.join(new, PARTINFO), os.path.join(new, MACADR), say)
        record[MACADR] = True
    # lls.cache_dir keys its run on these helpers where the firmware's LLS is a nyx module
    nyx = bool(rel.nvm.lls) and lls.Root(job.fw, job.target, job.tree).read(lls.NYX_LLS) is not None
    record["inputs"] = job.inputs()
    record["files"] = job.stamps(job.base_files() + _helpers(job, nyx) + list(files.values()))
    return record


def macadr_image(job, partinfo, out, say=print):
    """Write to out the macadr partition as the factory leaves it (macadr.image): its size the map's
    (partinfo, the prepared map's image), the MAC job's, the LGUDID its identity's (made now if it
    has none, said). Error if the map has the partition at a node the firmware does not open."""
    lgudid = targets.release(job.target).lgudid
    try:
        ids = vmidentity.ensure_lgudid(job.identity_dir, job.mac, say)
        entry = partmap.first(partmap.read(partinfo), lgudid.partition)
        if entry is None:
            raise macadr.Error(f"the partition map has no {lgudid.partition} partition")
        if entry[0] != lgudid.number:
            raise macadr.Error(
                f"the partition map has it as p{entry[0]}; the firmware opens "
                f"/dev/mmcblk0p{lgudid.number}"
            )
        data = macadr.image(entry[1].size, *ids, job.mac, lgudid.offset)
        with open(out, "wb") as f:
            f.write(data)
    except (macadr.Error, partmap.Error, OSError) as e:
        raise Error(f"the {lgudid.partition} partition: {e}") from None


def ensure(job, say=print, why=None):
    """The VM's Prepared board data: as stored if it is current, else made again first, with a line
    to say so and why (why: the caller's own reason, e.g. "the firmware changed")."""
    reason = stale(job)
    if reason is None:
        return Prepared(job, _read(job))
    if reason != "missing" or why:
        say(f"prepared board data {why or reason}: preparing it again")
    else:
        say("prepared board data missing: preparing it (the firmware's partition map and EEPROM)")
    return make(job, say)


def vm_env(vm_dir):
    """The environment a library VM's preparation needs (its vm.toml's FW, FW_CACHE, SERIAL,
    BROADCAST, MODEL, DEBUG_MODE, and its RUN_DIR)."""
    from . import base, core

    try:
        env = core.vm_launcher_env(os.path.join(vm_dir, "vm.toml"))
    except base.Error as e:
        raise Error(str(e)) from None
    env["RUN_DIR"] = os.path.join(os.path.abspath(vm_dir), "run")
    return env


def vm(vm_dir, say=print, tree=paths.TREE, force=False):
    """Prepare the library VM in vm_dir if it needs it (force: always); its Prepared."""
    job = Job(vm_env(vm_dir), tree)
    if force:
        say("preparing the board data (the firmware's partition map and EEPROM)")
        return make(job, say)
    return ensure(job, say)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 -m beanjar.prepare VM-DIR", file=sys.stderr)
        return 2
    try:
        vm(argv[0], lambda line: print(line, file=sys.stderr, flush=True), force=True)
    except (Error, nvm.Error) as e:
        print(f"prepare: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
