# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A new EEPROM as lowlevelstorage (LLS, webOS 5 on) leaves it on a TV, from LLS's own code:
nvm.py's part for a release whose chip LLS owns
(targets.Nvm.lls).

LLS keeps seven DB regions on the 32 KiB I2C EEPROM and checks each by a 4-byte valid mark only
(LLSController::initPlugInDB's memcmp). A region whose mark does not match gets the DB's default
record, built in code (the DB classes' constructors; the factory DB's from libmodelOpt, which reads
the tvconfig JSONs hw-option-gen and nyx-option-gen make from the board's command line), but only in
factory mode for the factory DB, so a blank chip never gets one in a normal boot. A set leaves the
factory with all seven. Here they are made the same way, by the firmware's own programs run on the
board's command line (the soc layer's board.cmdline):

  1. W20L's nyx-option-gen -A and hw-option-gen -A, as tvconfig-gen.service runs them, write
     tv.nyx.json and tv.hw.json to /tmp/var/run/tvconfig (the firmware's /var/run);
  2. guest/helpers/lls/lls-probe (32-bit ARM, built with the webOS SDK) loads LLS and calls what
     initPlugInDB calls for an invalid region: LLS's table of regions (getAllDBInfo) and per DB
     getDBSize, getInitData, getValidMarkInfo, getValidMarkMagic;
  3. each record is placed as initPlugInDB writes it (0x115f2-0x11678): the record with its mark's
     bytes set to 0xFF at the region's offset, then the mark. The rest of a region and of the chip
     is never written: an erased chip's 0xFF.
  4. the system DB's debugstatus item (record +4, 36 bytes, getItemInfo) as the board's debugMode=
     says: LLSSystemDB::encryptDebugstatus of that number (the default record holds 5, RELEASE;
     convertJsonToDataDebugstatus maps DEBUG, EVENT, RELEASE to 3, 4, 5 and calls it). On a TV
     U-Boot keeps the two in agreement (its DDI_NVM_SetDebugStatus; a set with debugMode=4 exports
     debugstatus "EVENT").
  5. the factory DB's modelname and serialnumber items from the board's modelName= and
     serialNum=, and its toolOption1Value..toolOption6Value from the ToolOpt= words (decimal
     strings), the bytes of LLS's own convertJsonToBinary at getItemInfo's place: what the
     factory line's jig writes through factorymanager (controller_cmd_modelnameSerialNum; the
     tool-option commands' setData, one item a word: scratch/w20l-lls-tooloption-study.md).
     LLS's default record has "WEBOS5.0" and libmodelOpt's default tool options (toolType
     UN73), which configd and factorymanager answer until then; nothing on a normal boot copies
     the command line's into LLS. A VM's own SERIAL is not in it: the image is made per board
     line.
  6. the system DB's instop_completed true (FACTORY_DONE), as the factory's In-Stop leaves every
     set: LLS's own convertJsonToBinary of a JSON boolean, as devicereset's setInstopCompleted
     writes it.

The programs run under qemu-arm in bwrap, in a root of the files they read, extracted from the
firmware's paks with unsquashfs as the VM stacks them (the release's pak lowers over the
rootfs), with the board's command line on /proc/cmdline and a fresh /tmp, in their own pid
namespace, with a PATH of beanjar's stand-ins alone: no host program but the shell runs (the
generators' popen: nyx-cmd replays the firmware's own nyx-cmd's OSInfo answers, initctl does
nothing; _helpers). glibc's MALLOC_PERTURB_=255 makes new heap memory read 0x00: LLSFactoryDB's
constructor leaves three padding bytes of its record unwritten (record +0x69-0x6b), which on a TV
hold what the heap had.
The debugstatus is encrypted with RAND_bytes, so two runs differ there.

The result is cached in build/lls/<firmware>/<key>/ (eeprom.img, the probe's records.txt, the two
JSONs, the command line), key the sha256 of this module's VERSION, the command line and every file
read. Installed hosts use the packaged probe. On 4 KiB hosts preparation needs qemu-arm
(qemu-user) and bwrap; larger-page hosts use the packaged 4 KiB kernel and initramfs in a
bounded, network-free qemu-system-aarch64 guest (paths.Tree.qemu's). Source builds need the SDK for the probe.
"""

import hashlib
import os
import platform
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile

from . import nvm, targets, paths

VERSION = 10
SIZE = 0x8000
SDK_GCC = "/opt/arm-webos-linux-gnueabi_sdk-buildroot/bin/arm-webos-linux-gnueabi-gcc"
# the programs run, in the firmware's root
GENERATORS = ("/usr/bin/nyx-option-gen", "/usr/bin/hw-option-gen")
TVCONFIG = "/tmp/var/run/tvconfig"
# the files they read besides their libraries (open() under qemu-arm -strace): the model's ids
# (libdile_boardinfo, nyx-option-gen) and libmodelOpt's country table
DATA = ("/etc/starfish/model_id.json", "/etc/configd/data/languageCountry.json")
FACTORY_DATA = tuple(
    "/etc/factorymanager/db/" + name
    for name in ("optionTable.json", "exportOptions.json", "exportLegacyOptions.json")
)
LIB_DIRS = ("/lib", "/usr/lib")
NYX_LLS = "/usr/lib/nyx/modules/nyxLlsDb.module"
# Dynamic modules opened by the stock storage service and option generators.
NYX_MODULES = tuple(
    "/usr/lib/nyx/modules/nyx" + name + ".module"
    for name in ("LlsDb", "I2c", "Crypto", "DeviceInfo", "BroadcastInfo", "OSInfo", "Ucom")
)


# the systemd mount tables of webOS 5-8 roots: their binds put /mnt/otycabi's nyx modules on
# /usr/lib/nyx/modules
BIND_FSTABS = "/etc/systemd/system/mount-readonly-volatile.d"
# The generators popen "nyx-cmd OSInfo query <key>" and put each answer in tv.nyx.json
# (firmwareVersion, platformVersion, platformCode; none where the command printed nothing). The
# OSInfo module has these built in; the firmware's own nyx-cmd answers them in the sandbox and a
# stand-in replays that to the generators (_helpers).
NYX_CMD = "/usr/bin/nyx-cmd"
NYX_CMD_OSINFO = "/usr/lib/nyx/nyxcmd/libnyxcmdModuleOSInfo.so"  # webOS 5-8's nyx-cmd plugin
OSINFO_MODULES = tuple(
    "/usr/lib/nyx/modules/" + name for name in ("nyxOSInfoMain.module", "nyxOSInfo.module")
)
OSINFO_KEYS = ("webos_manufacturing_version", "webos_release", "webos_release_platformcode")


class Error(Exception):
    pass


# ---------------------------------------------------------------------------- the firmware's root
class Root:
    """The firmware's root as the VM stacks it: the release's pak lowers (targets.PakLower: the pak's
    subdirectory) above the rootfs pak, read-only."""

    def __init__(self, fw, target, tree=paths.TREE):
        rel = targets.release(target, fw)
        files = {p.serial: p.file for p in rel.paks}
        root = [p for p in rel.paks if p.mount == "/"][0]
        self.layers = []  # (pak path, prefix), highest first
        for low in reversed(rel.pak_lowers):
            self.layers.append((os.path.join(fw, files[low.serial] + ".pak"), "/" + low.subdir))
        self.layers.append((os.path.join(fw, root.file + ".pak"), ""))
        # the paks mounted elsewhere ({mount point: pak path}), for the root's bind mounts
        self.mounts = {
            p.mount: os.path.join(fw, p.file + ".pak")
            for p in rel.paks
            if p.mount and p.mount != "/"
        }
        self._binds = None

    @staticmethod
    def _cat(pak, path):
        r = subprocess.run(
            ["unsquashfs", "-cat", "-no-wildcards", pak, path.lstrip("/")],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return r.stdout if r.returncode == 0 else None

    def binds(self):
        """[(source, target)] of the root's systemd bind mounts (BIND_FSTABS), as its
        mount-readonly-volatile applies them: W21P's nyx modules from /mnt/otycabi."""
        if self._binds is None:
            self._binds = []
            for pak, prefix in self.layers:
                r = subprocess.run(
                    ["unsquashfs", "-l", "-no-wildcards", pak, (prefix + BIND_FSTABS).lstrip("/")],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                names = sorted(
                    {ln.rsplit("/", 1)[1] for ln in r.stdout.split() if ln.endswith(".fstab")}
                )
                for name in names:
                    text = self._cat(pak, prefix + BIND_FSTABS + "/" + name) or b""
                    for line in text.decode(errors="replace").splitlines():
                        f = line.split()
                        if len(f) >= 4 and not f[0].startswith("#") and "bind" in f[3].split(","):
                            self._binds.append((f[0], f[1]))
                if names:
                    break
        return self._binds

    def read(self, path):
        """The bytes of path from the first layer unsquashfs can read, else through the root's bind
        mounts from a pak mounted at the bind's source, or None."""
        for pak, prefix in self.layers:
            blob = self._cat(pak, prefix + path)
            if blob is not None:
                return blob
        for source, target in self.binds():
            if path.startswith(target.rstrip("/") + "/"):
                source += path[len(target.rstrip("/")) :]
                for mount, pak in self.mounts.items():
                    if source.startswith(mount + "/"):
                        blob = self._cat(pak, source[len(mount) :])
                        if blob is not None:
                            return blob
        return None


def elf_deps(data):
    """(interpreter or None, [NEEDED]) of a 32-bit little-endian ELF file."""
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise Error("not a 32-bit little-endian ELF file")
    e_phoff = struct.unpack_from("<I", data, 28)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 42)
    loads, dyn, interp = [], None, None
    for k in range(e_phnum):
        p_type, p_offset, p_vaddr, _, p_filesz, _ = struct.unpack_from(
            "<IIIIII", data, e_phoff + k * e_phentsize
        )
        if p_type == 1:
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == 2:
            dyn = (p_offset, p_filesz)
        elif p_type == 3:
            interp = data[p_offset : p_offset + p_filesz].split(b"\0")[0].decode()
    if not dyn:
        return interp, []
    entries = []
    for off in range(dyn[0], dyn[0] + dyn[1], 8):
        tag, val = struct.unpack_from("<iI", data, off)
        if tag == 0:
            break
        entries.append((tag, val))
    strtab = next(v for t, v in entries if t == 5)

    def file_off(addr):
        for vaddr, offset, filesz in loads:
            if vaddr <= addr < vaddr + filesz:
                return offset + addr - vaddr
        raise Error(f"address 0x{addr:x} in no segment")

    base = file_off(strtab)
    needed = [data[base + v : data.index(b"\0", base + v)].decode() for t, v in entries if t == 1]
    return interp, needed


def extract(root, programs, data_files, dest):
    """Write programs, their libraries (NEEDED, transitively, in LIB_DIRS) and data_files into
    dest, as regular files at their paths; {path: sha256} of everything written."""
    out = {}

    def put(path, blob):
        p = os.path.join(dest, path.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(blob)
        os.chmod(p, 0o755)
        out[path] = hashlib.sha256(blob).hexdigest()

    todo, seen = list(programs), set()
    libs = {}
    while todo:
        path = todo.pop(0)
        if path in seen:
            continue
        seen.add(path)
        blob = root.read(path)
        if blob is None:
            raise Error(f"no {path} in the firmware")
        put(path, blob)
        interp, needed = elf_deps(blob)
        if interp:
            todo.append(interp)
        for name in needed:
            if name in libs:
                continue
            found = next(
                (d + "/" + name for d in LIB_DIRS if root.read(d + "/" + name) is not None), None
            )
            if not found:
                raise Error(f"{path}: no {name} in {' or '.join(LIB_DIRS)}")
            libs[name] = found
            todo.append(found)
    for path in data_files:
        blob = root.read(path)
        if blob is None:
            raise Error(f"no {path} in the firmware")
        put(path, blob)
    return out


# ---------------------------------------------------------------------------- running LLS's code
def probe(tree=paths.TREE, preload=False):
    """build/lls/lls-probe, built from guest/helpers/lls/lls-probe.c with the webOS SDK when missing or
    older than its source."""
    src = tree.src("guest", "helpers", "lls", "lls-probe.c")
    exe = tree.build("lls", "lls-probe.so" if preload else "lls-probe")
    if os.path.isfile(exe) and (tree.installed or os.path.getmtime(exe) >= os.path.getmtime(src)):
        return exe
    if tree.installed:
        raise Error(f"packaged ARM LLS probe missing: {exe}")
    if not os.access(SDK_GCC, os.X_OK):
        raise Error(f"no {SDK_GCC} to build guest/helpers/lls/lls-probe.c")
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    r = subprocess.run(
        [
            SDK_GCC,
            "-O2",
            "-Wall",
            *(["-shared", "-fPIC", "-DLLS_PRELOAD"] if preload else []),
            "-o",
            exe + ".new",
            src,
            "-ldl",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if r.returncode:
        raise Error(f"lls-probe.c: {r.stdout.strip()}")
    os.replace(exe + ".new", exe)
    return exe


def _run_guest(sysroot, cmdline, tmp, argv, tree, extra_env=None):
    """Run unchanged ARM firmware under our 4 KiB kernel on larger-page hosts."""
    guest_argv = [a.removeprefix(sysroot) if a.startswith(sysroot + "/") else a for a in argv]
    command = "export LANG=C MALLOC_PERTURB_=255 LD_LIBRARY_PATH=/lib:/usr/lib\n"
    for key, value in (extra_env or {}).items():
        command += "export " + key + "=" + shlex.quote(value) + "\n"
    command += "chroot /root " + shlex.join(guest_argv) + "\n"
    return _guest_request(sysroot, cmdline, tmp, command, tree)


def _guest_request(sysroot, cmdline, tmp, command, tree, eeprom=None):
    """Execute one bounded preparation transaction; optional EEPROM uses actual guest I2C."""
    for name in ("proc", "dev", "tmp", "var/run", "var/log"):
        os.makedirs(os.path.join(sysroot, name), exist_ok=True)
    os.makedirs(os.path.join(tmp, "var/run"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "var/log"), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=os.path.dirname(tmp)) as request:
        shutil.copyfile(cmdline, os.path.join(request, "cmdline"))
        with open(os.path.join(request, "command"), "w") as f:
            f.write(command)
        accel = (
            "kvm"
            if platform.machine() == "aarch64" and os.access("/dev/kvm", os.R_OK | os.W_OK)
            else "tcg"
        )
        qemu, qemu_args = tree.qemu()
        cmd = [
            qemu,
            *qemu_args,
            "-machine",
            "virt,accel=" + accel,
            "-cpu",
            "host" if accel == "kvm" else "cortex-a57",
            "-m",
            "256",
            "-smp",
            "1",
            "-display",
            "none",
            "-nic",
            "none",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-kernel",
            tree.build("Image"),
            "-initrd",
            tree.build("initramfs.cpio.gz"),
            "-append",
            "console=ttyAMA0 beanjar.prepare=1",
        ]
        if eeprom:
            cmd += [
                "-drive",
                f"if=none,id=prepare-eeprom,format=raw,file={eeprom}",
                "-device",
                "virtio-blk-pci,drive=prepare-eeprom",
            ]
        for tag, directory, readonly in (
            ("prepare-root", sysroot, True),
            ("prepare-request", request, False),
            ("prepare-tmp", tmp, False),
        ):
            cmd += [
                "-virtfs",
                f"local,path={directory},mount_tag={tag},security_model=none"
                + (",readonly=on" if readonly else ""),
            ]
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120
        )
        status = os.path.join(request, "status")
        if r.returncode or not os.path.isfile(status):
            raise Error("firmware preparation guest failed: " + r.stdout[-2000:])
        with open(status) as f:
            rc = int(f.read())
        with open(os.path.join(request, "stdout")) as f:
            out = f.read()
        with open(os.path.join(request, "stderr")) as f:
            err = f.read()
        return rc, out, err


def _helpers(bindir, osinfo=None):
    """bindir, the generators' whole PATH: initctl, which does nothing (the TV's upstart events
    nyxconfig-data-ready and hwconfig-data-ready have no consumer here), and where osinfo
    ({key: (exit status, output)} of the firmware's own nyx-cmd, _osinfo) is given, a nyx-cmd that
    replays those answers and fails for any other query. Host shells only, no other host program:
    the firmware's root has no /bin/sh, so qemu-arm runs the host's, which with PATH set looks in
    PATH alone."""
    shutil.rmtree(bindir, ignore_errors=True)
    os.makedirs(bindir)
    scripts = {"initctl": "exit 0\n"}
    if osinfo is not None:
        text = 'case "$*" in\n'
        for key, (rc, out) in sorted(osinfo.items()):
            pattern = shlex.quote("OSInfo query " + key)
            text += f"{pattern}) printf %s {shlex.quote(out)}; exit {rc} ;;\n"
        text += 'esac\necho "nyx-cmd $*: not answered in firmware preparation" >&2\nexit 1\n'
        scripts["nyx-cmd"] = text
    for name, body in scripts.items():
        p = os.path.join(bindir, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(p, 0o755)


def _osinfo_files(root):
    """Of the firmware's root, the files its nyx-cmd needs to answer OSINFO_KEYS: nyx-cmd, its
    OSInfo plugin where it has one, the OSInfo module (none if it has no nyx-cmd)."""
    if root.read(NYX_CMD) is None:
        return ()
    plugin = (NYX_CMD_OSINFO,) if root.read(NYX_CMD_OSINFO) is not None else ()
    module = next((p for p in OSINFO_MODULES if root.read(p) is not None), None)
    return (NYX_CMD, *plugin, *((module,) if module else ()))


def _osinfo(sysroot, cmdline, tmp, bindir, tree=paths.TREE):
    """{key: (exit status, output)} of the firmware's own nyx-cmd OSInfo query for each OSINFO_KEYS,
    as a TV's tvconfig-gen gets them; None where the firmware has no nyx-cmd."""
    nyx_cmd = os.path.join(sysroot, NYX_CMD.lstrip("/"))
    if not os.path.isfile(nyx_cmd):
        return None
    _helpers(bindir)
    out = {}
    for key in OSINFO_KEYS:
        rc, text, _ = _run(
            sysroot, cmdline, tmp, [nyx_cmd, "OSInfo", "query", key], tree, bindir=bindir
        )
        out[key] = (rc, text)
    return out


def _run(sysroot, cmdline, tmp, argv, tree=paths.TREE, extra_env=None, bindir=None, timeout=120):
    """argv (paths in the firmware's root) under qemu-arm in bwrap: /proc/cmdline cmdline, /tmp
    the directory tmp, PATH bindir alone (_helpers; by default bin beside sysroot, made with
    initctl if missing); (exit status, stdout, stderr). Everything it starts dies with it."""
    if os.sysconf("SC_PAGE_SIZE") > 4096:
        return _run_guest(sysroot, cmdline, tmp, argv, tree, extra_env)
    tools = {t: shutil.which(t) for t in ("bwrap", "qemu-arm")}
    for tool, path in tools.items():
        if not path:
            raise Error(f"{tool} not found (qemu-user, bubblewrap)")
    if bindir is None:
        bindir = os.path.join(os.path.dirname(sysroot), "bin")
        if not os.path.isdir(bindir):
            _helpers(bindir)
    # Ombre's generator writes literal /var/run as well as the -d output path.
    # QEMU path translation and the host /var/run -> /run alias both see private data.
    runtime = os.path.join(tmp, "var/run")
    os.makedirs(runtime, exist_ok=True)
    os.makedirs(os.path.join(sysroot, "var/run"), exist_ok=True)
    cmd = [
        tools["bwrap"],
        # a pid namespace: what the program leaves running dies with it (bwrap's init)
        "--unshare-pid",
        "--die-with-parent",
        "--bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        "--proc",
        "/proc",
        "--bind",
        cmdline,
        "/proc/cmdline",
        "--bind",
        tmp,
        "/tmp",
        "--bind",
        runtime,
        "/run",
        "--bind",
        runtime,
        os.path.join(sysroot, "var/run"),
        tools["qemu-arm"],
        "-L",
        sysroot,
        "-E",
        "MALLOC_PERTURB_=255",
        "-E",
        "LD_LIBRARY_PATH=" + sysroot + "/lib:" + sysroot + "/usr/lib",
    ]
    for key, value in (extra_env or {}).items():
        cmd += ["-E", key + "=" + value]
    cmd += argv
    # qemu-arm hands its environment to the program: none of the host's (LD_LIBRARY_PATH)
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": os.path.abspath(bindir), "LANG": "C"},
        start_new_session=True,
    )
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
        name = os.path.basename(argv[0]) if argv else "?"
        tail = err.strip()[-1000:] or "(none)"
        raise Error(f"{name}: no exit in {timeout} s, stopped; its stderr: {tail}") from None
    return p.returncode, out, err


def parse_records(text):
    """[{name, offset, region, flag, size, mark_offset, mark_size, record, magic}] of the probe's
    'db' lines."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if not f or f[0] != "db":
            continue
        if len(f) != 10:
            raise Error(f"probe: {line!r}")
        out.append(
            dict(
                name=f[1],
                offset=int(f[2]),
                region=int(f[3]),
                flag=int(f[4]),
                size=int(f[5]),
                mark_offset=int(f[6]),
                mark_size=int(f[7]),
                record=bytes.fromhex(f[8]) if f[8] != "-" else b"",
                magic=bytes.fromhex(f[9]) if f[9] != "-" else b"",
            )
        )
    if not out:
        raise Error("probe: no DBs")
    return out


def place(records):
    """The 32 KiB image: an erased chip (0xFF) with each DB written as initPlugInDB writes a
    default (0x115f2-0x11678): the record, its valid mark's bytes 0xFF, at the region's offset,
    then the mark there. The checks are initPlugInDB's (a record no larger than its region, the
    mark inside the record)."""
    image = bytearray(b"\xff" * SIZE)
    spans = []
    for d in records:
        if len(d["record"]) != d["size"] or len(d["magic"]) != d["mark_size"]:
            raise Error(f"{d['name']}: the record or mark is not its size")
        if d["size"] > d["region"] or d["mark_offset"] + d["mark_size"] > d["size"]:
            raise Error(
                f"{d['name']}: record {d['size']} B, region {d['region']} B, mark at "
                f"{d['mark_offset']}+{d['mark_size']}"
            )
        if d["offset"] + d["region"] > SIZE:
            raise Error(f"{d['name']}: region 0x{d['offset']:x}+0x{d['region']:x} past the chip")
        for name, a, b in spans:
            if d["offset"] < b and a < d["offset"] + d["region"]:
                raise Error(f"{d['name']} overlaps {name}")
        spans.append((d["name"], d["offset"], d["offset"] + d["region"]))
        rec = bytearray(d["record"])
        rec[d["mark_offset"] : d["mark_offset"] + d["mark_size"]] = b"\xff" * d["mark_size"]
        image[d["offset"] : d["offset"] + d["size"]] = rec
        at = d["offset"] + d["mark_offset"]
        image[at : at + d["mark_size"]] = d["magic"]
    return bytes(image)


def parse_items(text):
    """[{db, item, offset, size, data}] of the probe's 'item' lines"""
    out = []
    for line in text.splitlines():
        f = line.split()
        if not f or f[0] != "item":
            continue
        if len(f) != 6:
            raise Error(f"probe: {line!r}")
        out.append(
            dict(db=f[1], item=f[2], offset=int(f[3]), size=int(f[4]), data=bytes.fromhex(f[5]))
        )
    return out


def place_items(image, records, items):
    """image with each item written over its DB's record at the DB's offset + the item's (as LLS's
    setData writes one item, LLSController::getItem's region base + item offset), inside the
    record the default wrote"""
    image = bytearray(image)
    dbs = {d["name"]: d for d in records}
    for it in items:
        d = dbs.get(it["db"])
        if (
            not d
            or len(it["data"]) != it["size"]
            or it["offset"] < d["mark_offset"] + d["mark_size"]
            or it["offset"] + it["size"] > d["size"]
        ):
            raise Error(
                f"{it['db']}.{it['item']}: item at {it['offset']}+{it['size']} is not in the record"
            )
        at = d["offset"] + it["offset"]
        image[at : at + it["size"]] = it["data"]
    return bytes(image)


def board_cmdline(target, tree=paths.TREE, broadcast=None, debug_mode=None):
    """The board's words as guest/initramfs/rootfs/init appends them to /proc/cmdline (comment lines
    left out, one line): the board of the broadcast standard broadcast (targets.board_cmdline), its
    debugMode= the number debug_mode when given (the VM's debug_mode setting, nvm.DEBUG_MODES)."""
    try:
        return " ".join(nvm.board_words(target, tree, broadcast, debug_mode))
    except nvm.Error as e:
        raise Error(str(e)) from None


# The factory DB's items a factory line programs from the board's own words (factorymanager's
# controller_cmd_modelnameSerialNum, the jig's command, sets both): the board line's word, the item
FACTORY_STRINGS = (("modelName", "modelname"), ("serialNum", "serialnumber"))
# Items the factory's last station leaves on every set, whatever the board: the In-Stop is
# done. devicereset's setInstopCompleted writes system instop_completed through LLS's setData as
# a JSON boolean. A blank chip's default record says false, a set whose In-Stop is still to come,
# and notificationmgr, tts and fancontroller act on it.
FACTORY_DONE = (("bool", "system", "instop_completed", "true"),)


def _board_items(cmdline):
    """Arguments for the firmware's item conversions, shared by both storage interfaces."""
    words = dict(w.split("=", 1) for w in cmdline.split() if "=" in w)
    mode = words.get("debugMode", "")
    if not mode.isdigit():
        raise Error("the board's command line has no debugMode=N")
    args = ["debugstatus", mode]
    for word, item in FACTORY_STRINGS:
        if words.get(word):
            args += ["string", "factory", item, words[word]]
    for n, value in enumerate(
        words.get("ToolOpt", "").split(":") if words.get("ToolOpt") else (), 1
    ):
        args += ["string", "factory", f"toolOption{n}Value", str(int(value))]
    for done in FACTORY_DONE:
        args += list(done)
    return args


def generate_nyx(fw, target, cmdline, work, tree=paths.TREE, say=print):
    """Stock NYX LLS initializes an erased EEPROM through the existing guest I2C driver.

    The module probe enumerates and converts board items after stock initialization.
    Its metadata validates the disk; no firmware offsets or defaults are supplied here.
    """
    sysroot, tmp = os.path.join(work, "root"), os.path.join(work, "tmp")
    for name in ("var/run/tvconfig", "var/log"):
        os.makedirs(os.path.join(tmp, name), exist_ok=True)
    os.makedirs(os.path.join(sysroot, "probe"), exist_ok=True)
    shutil.copy(probe(tree), os.path.join(sysroot, "probe", "lls-probe"))
    cmdfile = os.path.join(work, "cmdline")
    with open(cmdfile, "w") as f:
        # Stock checkFactoryMode enables factory defaults only with this token.
        f.write(cmdline + " factory=1\n")
    eeprom = os.path.join(work, "eeprom.img")
    with open(eeprom, "wb") as f:
        f.write(b"\xff" * SIZE)
    params = [p for p in targets.soc(target).lgemu if p.startswith(("i2c_buses=", "i2c_dev="))]
    if not any(p.startswith("i2c_dev=") and "nvm:" in p for p in params):
        raise Error("NYX preparation needs the board's Linux I2C EEPROM parameters")
    command = "set -e\nexport LANG=C MALLOC_PERTURB_=255 LD_LIBRARY_PATH=/lib:/usr/lib\n"
    command += "insmod /lib/modules/lgemu.ko nvm_disk=/dev/vda " + shlex.join(params) + "\n"
    for g in GENERATORS:
        command += "timeout 30 chroot /root " + shlex.join([g, "-A", "-d", TVCONFIG]) + " >&2\n"
    command += """chroot /root /usr/sbin/lowlevelstorage >/prepare-tmp/lowlevelstorage.stdout 2>&1 &
lls_pid=$!
cleanup() { kill -TERM "$lls_pid" 2>/dev/null || true; sleep 1; kill -KILL "$lls_pid" 2>/dev/null || true; wait "$lls_pid" 2>/dev/null || true; }
trap cleanup EXIT
count=0
while ! grep -q 'initConfigdData:.*] end' /prepare-tmp/var/log/lowlevelstorage.log 2>/dev/null; do
    if ! kill -0 "$lls_pid" 2>/dev/null || [ "$count" -ge 40 ]; then
        cat /prepare-tmp/lowlevelstorage.stdout >&2
        cat /prepare-tmp/var/log/lowlevelstorage.log >&2 2>/dev/null || true
        exit 1
    fi
    sleep 1
    count=$((count + 1))
done
cleanup
trap - EXIT
"""
    command += (
        "timeout 30 chroot /root "
        + shlex.join(["/probe/lls-probe", NYX_LLS, *_board_items(cmdline)])
        + "\n"
    )
    rc, out, err = _guest_request(sysroot, cmdfile, tmp, command, tree, eeprom=eeprom)
    if rc:
        raise Error(f"NYX preparation: exit {rc}: {err.strip()[-1500:]}")
    records = parse_records(out)
    place(records)  # Check firmware-owned region and mark bounds before accepting the disk.
    with open(eeprom, "rb") as f:
        image = f.read()
    if len(image) != SIZE:
        raise Error("NYX preparation produced an invalid EEPROM size")
    for d in records:
        at = d["offset"] + d["mark_offset"]
        if image[at : at + d["mark_size"]] != d["magic"]:
            raise Error(f"NYX preparation did not initialize {d['name']}")
    jsons = {}
    for rel in ("nyx/tv.nyx.json", "hwoption/tv.hw.json"):
        with open(os.path.join(tmp, "var/run/tvconfig", rel)) as f:
            jsons[os.path.basename(rel)] = f.read()
    return place_items(image, records, parse_items(out)), out, jsons


def generate(fw, target, cmdline, work, tree=paths.TREE, say=print):
    """Run the generators and the probe in work (a scratch directory); (image, probe output, the
    tvconfig JSONs {name: text})."""
    sysroot = os.path.join(work, "root")
    tmp = os.path.join(work, "tmp")
    os.makedirs(os.path.join(tmp, TVCONFIG.removeprefix("/tmp/")), exist_ok=True)
    os.makedirs(os.path.join(sysroot, "probe"), exist_ok=True)
    shutil.copy(probe(tree), os.path.join(sysroot, "probe", "lls-probe"))
    cmdfile = os.path.join(work, "cmdline")
    with open(cmdfile, "w") as f:
        f.write(cmdline + "\n")
    lls = targets.release(target).nvm.lls
    bindir = os.path.join(work, "bin")
    _helpers(bindir, _osinfo(sysroot, cmdfile, tmp, bindir, tree))
    for g in GENERATORS:
        rc, out, err = _run(
            sysroot,
            cmdfile,
            tmp,
            [os.path.join(sysroot, g.lstrip("/")), "-A", "-d", TVCONFIG],
            tree,
            bindir=bindir,
        )
        if rc:
            raise Error(f"{g}: exit {rc}: {(err or out).strip()[-400:]}")
    jsons = {}
    for rel in ("nyx/tv.nyx.json", "hwoption/tv.hw.json"):
        p = os.path.join(tmp, TVCONFIG.removeprefix("/tmp/"), rel)
        if not os.path.isfile(p):
            raise Error(f"no {TVCONFIG}/{rel} from the generators")
        with open(p) as f:
            jsons[os.path.basename(rel)] = f.read()
    mode = next((w.split("=", 1)[1] for w in cmdline.split() if w.startswith("debugMode=")), None)
    if mode is None or not mode.isdigit():
        raise Error("the board's command line has no debugMode=N")
    words = dict(w.split("=", 1) for w in cmdline.split() if "=" in w)
    strings = []
    for word, item in FACTORY_STRINGS:
        if words.get(word):
            strings += ["string", "factory", item, words[word]]
    # the jig's tool options: each ToolOpt= word, as a decimal string, in toolOption<n>Value
    for n, value in enumerate(
        words.get("ToolOpt", "").split(":") if words.get("ToolOpt") else (), 1
    ):
        strings += ["string", "factory", f"toolOption{n}Value", str(int(value))]
    for done in FACTORY_DONE:
        strings += list(done)
    rc, out, err = _run(
        sysroot,
        cmdfile,
        tmp,
        [os.path.join(sysroot, "probe", "lls-probe"), lls, "debugstatus", mode, *strings],
        tree,
        bindir=bindir,
    )
    if rc and "cannot dynamically load position-independent executable" in err:
        shutil.copy(probe(tree, preload=True), os.path.join(sysroot, "probe", "lls-probe.so"))
        rc, out, err = _run(
            sysroot,
            cmdfile,
            tmp,
            [os.path.join(sysroot, lls.lstrip("/")), lls, "debugstatus", mode, *strings],
            tree,
            extra_env={"LD_PRELOAD": "/probe/lls-probe.so"},
            bindir=bindir,
        )
    if rc:
        raise Error(f"lls-probe: exit {rc}: {err.strip()[-400:]}")
    records = parse_records(out)
    return place_items(place(records), records, parse_items(out)), out, jsons


def tvconfig(fw, target, cmdlines, tree=paths.TREE, jobs=8, groups=False):
    """[(tv.hw.json, tv.nyx.json, the generators' output)] of each command line in cmdlines, as
    W20L's tvconfig-gen.service makes them (nyx-option-gen -A, then hw-option-gen -A), the JSONs
    as dicts (None where a generator wrote none). The LLS-era board values are decoded
    with it. groups: each tuple also has the country group the board's
    libdile_boardinfo gives that line (lls-probe group; None if it gave none)."""
    import json
    from concurrent.futures import ThreadPoolExecutor

    os.makedirs(tree.cache("lls"), exist_ok=True)
    work = tempfile.mkdtemp(prefix="work-", dir=tree.cache("lls"))
    try:
        sysroot = os.path.join(work, "root")
        # the probe NEEDs libdl, which the generators do not
        root = Root(fw, target, tree)
        modules = (
            tuple(p for p in NYX_MODULES if root.read(p) is not None) if root.read(NYX_LLS) else ()
        )
        extract(
            root,
            (
                *GENERATORS,
                *modules,
                *_osinfo_files(root),
                *(("/lib/libdl.so.2",) if groups else ()),
            ),
            DATA,
            sysroot,
        )
        if groups:
            os.makedirs(os.path.join(sysroot, "probe"), exist_ok=True)
            shutil.copy(probe(tree), os.path.join(sysroot, "probe", "lls-probe"))

        def one(k):
            d = os.path.join(work, str(k))
            tmp = os.path.join(d, "tmp")
            os.makedirs(os.path.join(tmp, TVCONFIG.removeprefix("/tmp/")))
            cmdfile = os.path.join(d, "cmdline")
            with open(cmdfile, "w") as f:
                f.write(cmdlines[k] + "\n")
            text = ""
            bindir = os.path.join(d, "bin")
            _helpers(bindir, _osinfo(sysroot, cmdfile, tmp, bindir, tree))
            for g in GENERATORS:
                _, out, err = _run(
                    sysroot,
                    cmdfile,
                    tmp,
                    [os.path.join(sysroot, g.lstrip("/")), "-A", "-d", TVCONFIG],
                    tree,
                    bindir=bindir,
                )
                text += out + err
            got = []
            for rel, key in (("hwoption/tv.hw.json", "tv.hw"), ("nyx/tv.nyx.json", "tv.nyx")):
                p = os.path.join(tmp, TVCONFIG.removeprefix("/tmp/"), rel)
                try:
                    with open(p) as f:
                        got.append(json.load(f).get(key))
                except (OSError, ValueError):
                    got.append(None)
            if not groups:
                return got[0], got[1], text
            _, out, err = _run(
                sysroot,
                cmdfile,
                tmp,
                [os.path.join(sysroot, "probe", "lls-probe"), "group"],
                tree,
                bindir=bindir,
            )
            f = out.split()
            grp = int(f[1]) if len(f) == 3 and f[0] == "group" and f[2] == "0" else None
            return got[0], got[1], text + out + err, grp

        with ThreadPoolExecutor(jobs) as ex:
            return list(ex.map(one, range(len(cmdlines))))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def image(
    fw, fw_name, target, tree=paths.TREE, say=print, serial=None, broadcast=None, debug_mode=None
):
    """The programmed 32 KiB image (bytes) for a new chip of target's board on the firmware fw
    (fw_name its name, the cache's directory): build/lls/<fw_name>/<key>/eeprom.img, made if
    missing. broadcast: the board's broadcast standard (targets.board_broadcast)."""
    with open(
        os.path.join(
            cache_dir(
                fw,
                fw_name,
                target,
                tree,
                say,
                serial=serial,
                broadcast=broadcast,
                debug_mode=debug_mode,
            ),
            "eeprom.img",
        ),
        "rb",
    ) as f:
        return f.read()


def debug_status_item(image, text):
    """image (a programmed chip) with its system DB's debugstatus item from text, the probe's
    output for the board line with the wanted debugMode= (records.txt of cache_dir; the VM's
    prepared board data keeps it): the item LLS's own LLSSystemDB::encryptDebugstatus makes, placed
    as place_items places an item. Nothing else of the chip changes."""
    items = [it for it in parse_items(text) if (it["db"], it["item"]) == ("system", "debugstatus")]
    if len(items) != 1:
        raise Error("the probe's output has no system debugstatus item")
    return place_items(image, parse_records(text), items)


def default_records(fw, fw_name, target, tree=paths.TREE, say=print, broadcast=None):
    """LLS's own default records of target's board (parse_records of the probe's output): what
    initPlugInDB writes to a blank region, before the items the factory line writes."""
    with open(
        os.path.join(cache_dir(fw, fw_name, target, tree, say, broadcast=broadcast), "records.txt")
    ) as f:
        return parse_records(f.read())


OPTION_TABLE = "/etc/factorymanager/db/optionTable.json"


def tool_option(fw, target, llsname, tree=paths.TREE, broadcast=None):
    """The board's value of the tool option item llsname (for example "localKeyType"), decoded as
    factorymanager decodes it: the firmware's own optionTable.json gives the item's word
    (toolOption<llsidx>), bit offset, width and value names (itemlist), and the board's ToolOpt=
    the words. The value's name, its number if the item has no names; None if the firmware has no
    such table or item, or the board no such word."""
    import json
    import re

    blob = Root(fw, target, tree).read(OPTION_TABLE)
    if blob is None:
        return None
    # factorymanager's JSON has a comma before each closing brace
    table = json.loads(re.sub(r",(\s*[}\]])", r"\1", blob.decode()))
    words = dict(
        w.split("=", 1) for w in board_cmdline(target, tree, broadcast).split() if "=" in w
    ).get("ToolOpt", "")
    words = [int(w) for w in words.split(":") if w]
    for word in table.get("lls", {}).get("tooloptions", ()):
        for item in word.get("subitems", ()):
            if item.get("llsname") != llsname:
                continue
            index = word["llsidx"] - 1
            if not 0 <= index < len(words):
                return None
            value = (words[index] >> item["offset"]) & ((1 << item["size"]) - 1)
            names = item.get("itemlist") or []
            return names[value] if value < len(names) else value
    return None


TOOLOPTION_LAYOUT = "/etc/factorymanager/db/factorymanager.db.tooloption.json"
FACTORYMANAGER = "/usr/sbin/factorymanager"


def elf_str_enum(data, name):
    """{name: value} of the 32-bit ELF data's dynamic symbol name, a {const char *name, u32 value}
    array ending in a NULL name (factorymanager's *_STRING_ENUM tables); None without the symbol."""
    (shoff,) = struct.unpack_from("<I", data, 32)
    shentsize, shnum = struct.unpack_from("<HH", data, 46)
    sections = [
        struct.unpack_from("<IIIIIIIIII", data, shoff + k * shentsize) for k in range(shnum)
    ]
    (e_phoff,) = struct.unpack_from("<I", data, 28)
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 42)
    loads = [struct.unpack_from("<IIIIII", data, e_phoff + k * e_phentsize) for k in range(e_phnum)]

    def at(addr):
        for p_type, p_offset, p_vaddr, _, p_filesz, _ in loads:
            if p_type == 1 and p_vaddr <= addr < p_vaddr + p_filesz:
                return p_offset + addr - p_vaddr
        raise Error(f"address 0x{addr:x} in no segment")

    def cstr(off):
        return data[off : data.index(b"\0", off)].decode()

    for sh in sections:
        if sh[1] != 11:  # SHT_DYNSYM
            continue
        strtab = sections[sh[6]][4]
        for off in range(sh[4], sh[4] + sh[5], 16):
            st_name, st_value, st_size = struct.unpack_from("<III", data, off)
            if cstr(strtab + st_name) != name:
                continue
            out, base = {}, at(st_value)
            for k in range(0, st_size - 7, 8):
                ptr, value = struct.unpack_from("<II", data, base + k)
                if not ptr:
                    break
                out[cstr(at(ptr))] = value
            return out
    return None


def local_key(fw, target, tree=paths.TREE, broadcast=None):
    """The board's model's local key (tv.model.localKeyType: "joystick", "onekey", "ninekey",
    "sevenkey"), decoded from the board's ToolOpt= as the firmware's factorymanager does: with its
    option table (optionTable.json, tool_option) where it has one, else (webOS 5 and 6) with its
    tool option layout (factorymanager.db.tooloption.json: toolOpt<N>'s localKey offset and mask)
    and the names of its LOCALKEY_TYPE_STRING_ENUM; on a tvservice release (webOS 1 to 4) as its
    tvservice does, with the release's Nvm.local_key. None where the firmware has none of these."""
    words = dict(
        w.split("=", 1) for w in board_cmdline(target, tree, broadcast).split() if "=" in w
    ).get("ToolOpt", "")
    words = [int(w) for w in words.split(":") if w]
    spec = targets.release(target).nvm.local_key
    if isinstance(spec, str):
        return spec
    if spec:
        if not 0 < spec.word <= len(words):
            return None
        value = (words[spec.word - 1] >> spec.offset) & ((1 << spec.width) - 1)
        return spec.names[value] if value < len(spec.names) else None
    value = tool_option(fw, target, "localKeyType", tree, broadcast)
    if value is not None:
        return value if isinstance(value, str) else None
    return _layout_option(fw, target, words, "localKey", "LOCALKEY_TYPE_STRING_ENUM", tree)


def _layout_option(fw, target, words, field, enum, tree=paths.TREE):
    """The board's value (words: its ToolOpt= words) of the tool option field as webOS 5 and 6's
    factorymanager decodes it: toolOpt<N>'s field offset and mask in its tool option layout
    (factorymanager.db.tooloption.json), named by its enum table (elf_str_enum). None where the
    firmware has neither, or the board not the word."""
    import json
    import re

    root = Root(fw, target, tree)
    blob = root.read(TOOLOPTION_LAYOUT)
    if blob is None:
        return None
    layout = json.loads(re.sub(r",(\s*[}\]])", r"\1", blob.decode()))
    found = next(
        (
            (int(word[len("toolOpt") :]), f[field])
            for word, f in layout.items()
            if word.startswith("toolOpt") and isinstance(f, dict) and field in f
        ),
        None,
    )
    elf = root.read(FACTORYMANAGER)
    names = elf_str_enum(elf, enum) if found and elf else None
    if not names:
        return None
    index, spec = found
    if not 0 < index <= len(words):
        return None
    value = (words[index - 1] >> spec["offset"]) & spec["mask"]
    return next((n for n, v in names.items() if v == value), None)


def digital_eye(fw, target, tree=paths.TREE, broadcast=None):
    """The board's model's ambient light sensor option (tv.model.digitalEye: "off",
    "eye_sensor", "rgb_sensor" or "OTP_eyeSensor"), decoded from the board's ToolOpt= as the
    firmware's factorymanager does (as local_key: its optionTable.json, else its tool option layout
    and DIGITALEYE_STRING_ENUM). None where the firmware has neither (before webOS 5)."""
    value = tool_option(fw, target, "digitalEye", tree, broadcast)
    if value is not None:
        return value if isinstance(value, str) else None
    words = dict(
        w.split("=", 1) for w in board_cmdline(target, tree, broadcast).split() if "=" in w
    ).get("ToolOpt", "")
    words = [int(w) for w in words.split(":") if w]
    return _layout_option(fw, target, words, "DigitalEye", "DIGITALEYE_STRING_ENUM", tree)


def motion_sensor(fw, target, tree=paths.TREE, broadcast=None):
    """The board's model's motion sensor option (tv.model.supportMotionDetectSensor: "off",
    "ToF_TBD_22Y" or "CAM_ART&R2_21Y"; a number where the firmware's item is one bit, as N21D's),
    decoded from the board's ToolOpt= by the firmware's optionTable.json (tool_option). None where
    the firmware has no such item."""
    return tool_option(fw, target, "supportMotionDetectSensor", tree, broadcast)


def panel_rate(cdir):
    """The panel's refresh in Hz of the board whose run cache_dir made cdir: tv.hw.json's
    panelOutputFrameRate, as the firmware's own hw-option-gen decodes the board's hwopt= (the digit
    and its table differ between releases: d4 on webOS 5 and 6, d9 later, with 100 to 240 Hz).
    None where the JSON or the field is missing, or the value is no "<n>Hz"."""
    import json
    import re

    try:
        with open(os.path.join(cdir, "tv.hw.json")) as f:
            hw = json.load(f)
    except (OSError, ValueError):
        return None
    value = (hw.get("tv.hw") or {}).get("panelOutputFrameRate") if isinstance(hw, dict) else None
    m = re.fullmatch(r"(\d+)Hz", value) if isinstance(value, str) else None
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def cache_dir(
    fw, fw_name, target, tree=paths.TREE, say=print, serial=None, broadcast=None, debug_mode=None
):
    """build/lls/<fw_name>/<key>, with eeprom.img, records.txt, the command line and the JSONs,
    made if missing; the board is the one for the broadcast standard broadcast
    (targets.board_broadcast), its debugMode= the number debug_mode when given."""
    cmdline = board_cmdline(target, tree, broadcast, debug_mode)
    if serial:
        cmdline = (
            " ".join(w for w in cmdline.split() if not w.startswith("serialNum="))
            + " serialNum="
            + serial
        )
    root = Root(fw, target, tree)
    lls = targets.release(target).nvm.lls
    # not under /tmp, which the runs get a fresh one of
    os.makedirs(tree.cache("lls"), exist_ok=True)
    work = tempfile.mkdtemp(prefix="work-", dir=tree.cache("lls"))
    try:
        data_files = DATA + tuple(p for p in FACTORY_DATA if root.read(p) is not None)
        # The SDK-built probe needs libdl.so.2 even when newer stock libc
        # provides dlopen directly and LLS no longer records that dependency.
        nyx = root.read(NYX_LLS) is not None
        modules = tuple(p for p in NYX_MODULES if root.read(p) is not None) if nyx else ()
        digests = extract(
            root,
            (lls, *GENERATORS, "/lib/libdl.so.2", *modules, *(() if nyx else _osinfo_files(root))),
            data_files,
            os.path.join(work, "root"),
        )
        if nyx:
            # These helpers execute the stock code; their changes must invalidate preparation.
            for name, path in (
                ("lls-probe", probe(tree)),
                ("Image", tree.build("Image")),
                ("initramfs", tree.build("initramfs.cpio.gz")),
                ("lgemu", tree.build("lgemu.ko")),
            ):
                with open(path, "rb") as f:
                    digests["beanjar/" + name] = hashlib.sha256(f.read()).hexdigest()
        h = hashlib.sha256(f"{VERSION}\n{cmdline}\n".encode())
        for p in sorted(digests):
            h.update(f"{p} {digests[p]}\n".encode())
        key = h.hexdigest()[:16]
        cdir = tree.cache("lls", fw_name, key)
        if os.path.isfile(os.path.join(cdir, "eeprom.img")):
            return cdir
        # made once under its lock (<key>.lock beside it), as VMs of one firmware may start at once
        with paths.file_lock(cdir + ".lock"):
            if os.path.isfile(os.path.join(cdir, "eeprom.img")):
                return cdir
            say(
                f"EEPROM: LLS's default records from {fw_name} (firmware generators), cached in {os.path.relpath(cdir, tree.top)}"
            )
            data, out, jsons = (generate_nyx if nyx else generate)(
                fw, target, cmdline, work, tree, say
            )
            shutil.rmtree(cdir + ".new", ignore_errors=True)
            os.makedirs(cdir + ".new")
            for name, text in (("records.txt", out), ("cmdline", cmdline + "\n"), *jsons.items()):
                with open(os.path.join(cdir + ".new", name), "w") as f:
                    f.write(text)
            with open(os.path.join(cdir + ".new", "eeprom.img"), "wb") as f:
                f.write(data)
            shutil.rmtree(cdir, ignore_errors=True)
            os.replace(cdir + ".new", cdir)
            return cdir
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    """python3 -m beanjar.lls FIRMWARE_DIR: make (or find) the cached image of FIRMWARE_DIR's target
    and print its directory's records."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 -m beanjar.lls FIRMWARE_DIR", file=sys.stderr)
        return 2
    fw = os.path.abspath(argv[0])
    name = os.path.basename(fw.rstrip("/"))
    target = paths.firmware_target(fw, name)
    if not targets.release(target).nvm.lls:
        print(f"{targets.name(target)}: the EEPROM is not LLS's", file=sys.stderr)
        return 1
    try:
        data = image(fw, name, target, say=lambda s: print(s, file=sys.stderr))
    except Error as e:
        print(f"lls: {e}", file=sys.stderr)
        return 1
    print(f"{len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
