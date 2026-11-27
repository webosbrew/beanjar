# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar clone SRC DEST: DEST, a copy of the VM SRC that is off, as another TV (host/core/README.md,
"Commands"). Either may be on another host (HOST/NAME); this machine drives the copy and relays it.

What is copied: SRC's vm.toml (every setting but its slot, which DEST's library assigns), its state
disks (first use, region, installed apps), its own firmware/ when it has one (an external firmware
directory is used in place, and must be at the same path on DEST's host) and its identity/ but the
files a new identity replaces: sestore.toml with the model key, tee-huk (the TEE's key, which what the
state's secure store holds is encrypted with), authorized_keys and anything else there. What is new,
as `identity reset` makes it (vmidentity.next_identity, vmidentity.renew_keys): the MAC and serial, every key pair,
the VNC password; known_hosts is written at DEST's first start. The state is not wiped: the guest
brings what it derives from the identity in line at boot (the NDUID from the MAC, the devmode and
Homebrew Channel keys from the per-VM layer), and the EEPROM's serial records, which a start does not
rewrite on LLS's chips (webOS 5 on), are rewritten here (nvm.replace_serial).

Nothing but `beanjar show` (and `config show` once DEST is in place) is asked of a host's
beanjar: the copy is plain sh and GNU tar over the hosts' ssh (remote.shell_argv), so a host needs no
newer beanjar for it. The data goes from SRC's host through this machine to DEST's host as a tar
stream, never to a file here; only the files this machine makes for DEST (vm.toml, the new keys, the
EEPROM) are staged, in this machine's library (vms/.clone-stage-*, owner-only, removed at the end).
DEST is assembled on its host in vms/.clone-DEST-* (hidden: no VM) and renamed into place at the end,
after SRC is seen off again; a failure, Ctrl-C, SIGTERM or SIGHUP removes it, and a clone killed
outright leaves only that hidden directory."""

import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import tomllib

from .. import base, tomlfile
from . import library as vm_library, keys, config, identity
from ..board import nvm
from ..access import remote
from ..base import Error

STAGE = ".clone-stage-"
SCRATCH = ".clone-"
EEPROM = "state/eeprom.img"
# the library a host's beanjar opens without --library (core.default_library)
LIBRARY_SH = 'printf %s "${BEANJAR_LIBRARY:-${XDG_DATA_HOME:-$HOME/.local/share}/beanjar}"'


def q(s):
    return shlex.quote(str(s))


class End:
    """One side of a clone: this machine (host None, its library lib) or a configured host."""

    def __init__(self, host, lib):
        self.host = host
        self.lib = lib
        self.uri = remote.hosts()[host] if host else None

    @property
    def where(self):
        return self.host or remote.LOCAL

    def argv(self, command):
        return remote.shell_argv(self.uri, command) if self.host else ["sh", "-c", command]

    def sh(self, command, stdin=None, ok=(0,)):
        """(status, stdout) of the shell command here or on the host; a status not in ok is an Error."""
        r = subprocess.run(self.argv(command), check=False, input=stdin, capture_output=True)
        if r.returncode not in ok:
            reason = r.stderr.decode(errors="replace").strip() or f"exit status {r.returncode}"
            raise Error(f"{self.where}: {reason}")
        return r.returncode, r.stdout

    def library(self):
        if not self.host:
            return self.lib.root
        path = self.sh(LIBRARY_SH)[1].decode().strip()
        if not path.startswith("/"):
            raise Error(f"{self.where}: no library directory ({path!r})")
        return path

    def vm_state(self, name):
        """(the VM's directory, its state: core.OFF ...)."""
        if not self.host:
            vm = self.lib.vm(name)
            return vm.dir, vm.state()[0]
        data = remote.call_json(self.host, self.uri, ["show", remote.own(name)])
        try:
            return data["dir"], data["state"]
        except (KeyError, TypeError):
            raise remote.HostError(self.host, "its beanjar show has no dir or state") from None

    def identities(self, root):
        """({mac}, {serial}, mac_prefix) of the library at root (its VMs, not the hidden scratch)."""
        if not self.host:
            return *identity.identities(os.path.join(root, "vms")), identity.mac_prefix_of(root)
        text = self.sh(f"cat -- {q(root)}/config.toml 2>/dev/null; true")[1].decode()
        try:
            prefix = (tomllib.loads(text) or {}).get("mac_prefix")
        except tomllib.TOMLDecodeError as e:
            raise Error(f"{self.where}: {root}/config.toml: {e}") from None
        prefix = identity.parse_mac_prefix(prefix or identity.DEFAULT_MAC_PREFIX)
        out = self.sh(
            "grep -h -E '^[[:space:]]*(mac|serial)[[:space:]]*=' "
            f"{q(root)}/vms/*/vm.toml 2>/dev/null; true"
        )[1].decode()
        macs, serials = set(), set()
        for line in out.splitlines():
            try:
                item = tomllib.loads(line)
            except tomllib.TOMLDecodeError:
                continue
            if "mac" in item:
                macs.add(str(item["mac"]).lower())
            if "serial" in item:
                serials.add(str(item["serial"]).upper())
        return macs, serials, prefix


def pipe(src_argv, dst_argv):
    """src_argv's stdout into dst_argv's stdin; an Error naming the side that failed."""
    with tempfile.TemporaryFile() as e1, tempfile.TemporaryFile() as e2:
        p1 = subprocess.Popen(src_argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=e1)
        try:
            p2 = subprocess.Popen(dst_argv, stdin=p1.stdout, stdout=subprocess.DEVNULL, stderr=e2)
        except BaseException:
            p1.kill()
            p1.wait()
            raise
        p1.stdout.close()
        try:
            rc2 = p2.wait()
            rc1 = p1.wait()
        finally:
            for p in (p1, p2):
                if p.poll() is None:
                    p.kill()
                    p.wait()
        for rc, err, what in ((rc1, e1, "reading"), (rc2, e2, "writing")):
            if rc:
                err.seek(0)
                reason = err.read().decode(errors="replace").strip() or f"exit status {rc}"
                raise Error(f"copy failed {what}: {reason}")


def clone(lib, src_spec, dest_spec, out=print, warn=print, pinned=False):
    """DEST (dest_spec: NAME here, local/NAME or HOST/NAME), a copy of the VM src_spec (NAME, looked
    up as other commands do, or HOST/NAME; pinned: a bare NAME is lib's, nobody is asked) that is off. Returns what the CLI prints."""
    from .create import _hold

    src_host, src_name = remote.locate(src_spec, lambda n: n in lib.names(), warn, pinned)
    dst_host, dst_name = remote.split_spec(dest_spec)
    if dst_host == remote.LOCAL:
        dst_host = None
    elif dst_host is not None and dst_host not in remote.hosts():
        raise Error(f"no host named {dst_host} (beanjar host list)")
    if not base.NAME_RE.fullmatch(dst_name or ""):
        raise Error(f"invalid VM name {dst_name!r} (letters, digits, '.', '_', '-')", "name")
    src, dst = End(src_host, lib), End(dst_host, lib)
    src_label, dst_label = f"{src.where}/{src_name}", f"{dst.where}/{dst_name}"
    if (src_host, src_name) == (dst_host, dst_name):
        raise Error(f"{src_label} is the VM to copy: give the copy another name")

    def check_off():
        d, state = src.vm_state(src_name)
        if state != base.OFF:
            raise Error(
                f"VM {src_label} is {state.lower()}: clone copies a VM that is off; stop it first"
            )
        return d

    src_dir = check_off()
    if not src.host:
        # a reset to a snapshot killed after its commit, finished first: a copy of the half-restored
        # state would be of neither (under the start lock, as start, wipe and snapshot take it)
        from . import snapshot

        vm = lib.vm(src_name)
        with snapshot.held(vm):
            snapshot.check_still_off(vm, "a clone")
            snapshot.finish_reset(vm.dir, out)
    text = src.sh(f"cat -- {q(src_dir)}/vm.toml")[1].decode()
    try:
        cfg = config.decode(tomllib.loads(text))
    except tomllib.TOMLDecodeError as e:
        raise Error(f"{src_label}: vm.toml: {e}") from None
    fw = cfg.get("fw") or "firmware"
    members = ["identity", "state"]
    if os.path.isabs(fw):
        rc, _ = dst.sh(f"test -f {q(fw)}/manifest.toml", ok=(0, 1))
        if rc:
            raise Error(
                f"{dst.where} has no firmware {fw} (the one {src_label} boots in place; the shared "
                "collection is at the same path on every host)"
            )
    else:
        rel = os.path.normpath(fw)
        if rel.startswith(".."):
            raise Error(f"{src_label}: its firmware {fw} is outside its folder")
        members.append(rel)  # its own, copied with it

    root = dst.library()
    dest_dir = f"{root}/vms/{dst_name}"
    if dst.sh(f"test -e {q(dest_dir)}", ok=(0, 1))[0] == 0:
        raise Error(f"VM {dst_label} exists (beanjar delete {dst_label} first)", "name")
    macs, serials, prefix = dst.identities(root)
    mac, serial = identity.next_identity(prefix, macs, serials, cfg)
    present = (
        src.sh(
            f"cd {q(src_dir)} && for m in {' '.join(q(m) for m in members)}; do "
            '[ -e "$m" ] && printf "%s\\n" "$m"; done; true'
        )[1]
        .decode()
        .split()
    )

    stage = scratch = None
    old = {}

    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    try:
        for s in (signal.SIGTERM, signal.SIGHUP):
            try:
                old[s] = signal.signal(s, interrupted)
            except ValueError:
                pass
        # what this machine makes for DEST: vm.toml, the new keys and VNC password, the EEPROM
        stage = tempfile.mkdtemp(prefix=STAGE, dir=lib.vms_dir)
        with open(os.path.join(stage, "vm.toml"), "w") as f:
            f.write(text)
        new_cfg = dict(cfg, mac=mac, serial=serial)
        new_cfg.pop("slot", None)  # DEST's library gives it its own
        config.write(os.path.join(stage, "vm.toml"), new_cfg, tomlfile.VM_HEADER)
        ident = os.path.join(stage, "identity")
        os.mkdir(ident, 0o700)
        try:
            made = identity.renew_keys(ident, dst_name, mac)
        except keys.Error as e:
            raise Error(str(e)) from None
        rc, image = src.sh(
            f"if [ -f {q(src_dir)}/{EEPROM} ]; then cat -- {q(src_dir)}/{EEPROM}; else exit 3; fi",
            ok=(0, 3),
        )
        if rc == 0:
            image, count = nvm.replace_serial(image, str(cfg.get("serial") or ""), serial)
            os.mkdir(os.path.join(stage, "state"))
            with open(os.path.join(stage, EEPROM), "wb") as f:
                f.write(image)
            out(f"EEPROM: {count} serial record(s) rewritten")
        out(f"mac {mac}, serial {serial}; new {', '.join(made)}")

        scratch = (
            dst.sh(
                f"mkdir -p {q(root)}/vms && mktemp -d {q(root)}/vms/{SCRATCH}{q(dst_name)}-XXXXXX"
            )[1]
            .decode()
            .strip()
        )
        if not scratch.startswith(f"{root}/vms/{SCRATCH}"):
            raise Error(f"{dst.where}: mktemp gave {scratch!r}")
        exclude = [f"identity/{n}" for n in identity.renewed()] + [EEPROM]
        out(f"copying {', '.join(present) or 'nothing'} from {src_label} to {dst.where}")
        if present:
            pipe(
                src.argv(
                    f"tar -C {q(src_dir)} --sparse "
                    + " ".join(f"--exclude={q(x)}" for x in exclude)
                    + " -cf - -- "
                    + " ".join(q(m) for m in present)
                ),
                dst.argv(f"tar -C {q(scratch)} -xpf -"),
            )
        pipe(
            ["tar", "-C", stage, "-cf", "-", "--", *sorted(os.listdir(stage))],
            dst.argv(f"tar -C {q(scratch)} -xpf -"),
        )
        dst.sh(f"chmod 700 {q(scratch)}/identity 2>/dev/null; true")
        check_off()  # not started meanwhile: what was copied is a stopped VM's
        _hold(True)
        rc, _ = dst.sh(
            f"if [ -e {q(dest_dir)} ]; then exit 3; fi; mv -T -- {q(scratch)} {q(dest_dir)}",
            ok=(0, 3),
        )
        if rc == 3:
            raise Error(f"VM {dst_label} exists (made meanwhile)", "name")
        scratch = None
        # its slot, the lowest free one of its library (as one made before slots gets it)
        if dst.host:
            remote.call_json(dst.host, dst.uri, ["config", "show", remote.own(dst_name)])
            info = remote.call_json(dst.host, dst.uri, ["show", remote.own(dst_name)])
            address = info.get("address")
        else:
            identity.ensure_slot(dest_dir, say=lambda line: None)
            address = vm_library.VM(lib, dst_name).address()
            # its board data, for its own serial (another host's is made at DEST's first start)
            vm_library.prepare_board_data(lib, dest_dir, out)
        out(f"cloned {src_label} to {dst_label}")
        return {
            "name": dst_name,
            "host": dst_host,
            "source": src_label,
            "mac": mac,
            "serial": serial,
            "address": address,
        }
    except BaseException as e:
        _hold(True)
        if isinstance(e, KeyboardInterrupt):
            out("cancelled: no copy was made")
        raise
    finally:
        _hold(True)
        if stage:
            shutil.rmtree(stage, ignore_errors=True)
        if scratch:
            try:
                dst.sh(f"rm -rf -- {q(scratch)}")
            except Error as e:
                warn(f"warning: {e}: remove {scratch} on {dst.where}")
        for s, h in old.items():
            signal.signal(s, h)
        _hold(False)
