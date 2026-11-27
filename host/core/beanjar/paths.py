# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Where the host runtime finds things: the one place that knows the layout (the checkout today).

Tree(top) is a checkout: its source files (src: the guest-side scripts shared with the guest over
9p, the layers, host/helpers/network) and the build's published artefacts under
build/ (build: the kernel, initramfs, layers, patched QEMU, firmware cache, and the
checkout VM's runtime files). TREE is the checkout this package lives in; the launcher, the unit
and the network wrappers take everything from it, so moving to an install prefix is a change here.

Where a launch finds its firmware is Tree.firmware(): FW_NAME by default (firmware/probe.py), found in
the host's firmware collections (runtime/hostconfig.py firmware_dirs), the first that has it. FW in the
environment wins: a directory (any path with a '/', e.g. a manager VM's <vm>/firmware), or a bare
firmware name looked up there. The host build takes -Dfirmware=DIR for its firmware cache;
Tree.target_firmware() is a target's firmware (the one `vm-launcher firmware-facts` reads) and
Tree.target() its (chip, release); the probing is in firmware/probe.py. The guest build of a target is
build/guest-<chip>-<release> and publishes its layers to build/layers/<chip>-<release> (Tree.layer).
"""

import os
import re
import tomllib


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
        from .firmware import targets

        if p:
            from .guestside import components

            selected = components.select(
                self.build("components"), target, components.component(target, p[0])
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
        from .firmware import probe

        name = probe.FW_NAME
        if fw and "/" in fw:
            name = os.path.basename(fw.rstrip("/"))
            # Managed copies all live in a directory called "firmware". Preserve
            # their import name so native LLS caches also work on hosts without
            # the SDK/qemu-arm used to generate them.
            try:
                with open(os.path.join(fw, probe.MANIFEST), "rb") as f:
                    name = str(tomllib.load(f).get("name") or name)
            except (OSError, ValueError):
                pass
            return fw, name
        if fw:
            name = fw
        from .runtime import hostconfig

        candidates = [os.path.join(d, name) for d in self.firmware_dirs()]
        fallback = (
            candidates[-1]
            if candidates
            else os.path.join(hostconfig.DEFAULT_FIRMWARE_DIRS[0], name)
        )
        return next((d for d in candidates if os.path.isdir(d)), fallback), name

    def firmware_dirs(self):
        """Where a bare firmware name is looked up, in order: the host's collections
        (hostconfig.firmware_dirs)."""
        from .runtime import hostconfig

        return hostconfig.firmware_dirs()

    def target_firmware(self, target):
        """(FW, FW_NAME) of a target's firmware: the default firmware if it resolves to the target
        (probe.firmware_target), else the one firmware in firmware_dirs() that does (the first directory
        that has one), leaving out a bring-up model's on a supported target and, of several, preferring
        the one that boots the chip's default board (probe.default_candidates).
        ValueError for none or several."""
        from .firmware import probe, targets

        def of_target(fw, name):
            try:
                return probe.firmware_target(fw, name) == tuple(target)
            except ValueError:
                return False

        default = self.firmware()
        if of_target(*default):
            return default
        for d in self.firmware_dirs():
            try:
                names = probe.default_candidates(
                    target,
                    {
                        n: os.path.join(d, n)
                        for n in sorted(os.listdir(d))
                        if probe.model_of(n) in targets.MODELS
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
        """(chip, release) of FW as the environment gives it (probe.firmware_target; ValueError if unknown)."""
        from .firmware import probe

        return probe.firmware_target(*self.firmware(fw))


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
