# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's settings (vm.toml): their names, values, help and defaults, the checks of
a whole configuration (validate), and the launcher environment they make (launcher_env)."""

import os
import re

from . import nvm, targets, vmconfig
from .base import (
    Error,
)
from . import vmidentity


DISPLAYS = ("dbus", "egl-headless", "none")


AUDIOS = ("dbus", "none", "off")


# gpu (qemu.py GPU_DEVICE): vhost-user-gpu runs the GPU in its own process; auto is vhost-user-gpu on
# ARM hosts with a render node, else virtio-gpu-gl; docs/dev/architecture/gpu-process.md
GPUS = ("auto", "vhost-user-gpu", "virtio-gpu-gl")


# free_page_reporting (qemu.py FREE_PAGE_REPORTING): off by default
ONOFF = ("on", "off")


# userland (qemu.py USERLAND): how the guest runs the firmware's 32-bit ARM programs. native: the
# CPU runs them in AArch32 EL0 (KVM on a CPU that has it, or TCG); qemu-user: qemu-arm translates
# them inside the guest and hands their system calls to the kernel's compat table (the compat
# bridge, docs/dev/architecture/qemu-user-bridge.md), for hosts without AArch32 at EL0; auto, the
# default, picks one of them at each start (resolve_userland). native and qemu-user override it.
USERLANDS = ("auto", "native", "qemu-user")
DEFAULT_USERLAND = "auto"


# guest_kernel (qemu.py GUEST_KERNEL): the guest kernel's architecture. arm64 is the default and
# the only one that runs the 32-bit programs natively. x86-64 is experimental: an x86-64 kernel on
# the q35 machine (KVM when /dev/kvm opens, else TCG), with the whole ARM userland under qemu-arm
# (docs/dev/architecture/qemu-user-bridge.md, "The x86-64 guest kernel"). It needs an x86-64 host
# and implies userland qemu-user.
GUEST_KERNELS = ("arm64", "x86-64")
DEFAULT_GUEST_KERNEL = "arm64"


# The firmware lines that need native 32-bit execution, whatever the host can do, with the reason.
# NetCast 4.5 (LM14) emulates stores into MMIO windows by decoding the faulting AArch32 instruction
# (lgemu's store_mmio_fault), which a translated program does not have: its HWI2C, OTP and PQ pages
# would SIGBUS; the decoder is arm64-only. SimpleSmart has not been tried under qemu-user.
NATIVE_ONLY = {
    "NetCast": "NetCast's MMIO windows decode the faulting AArch32 instruction (lgemu)",
    "SimpleSmart": "not tried on SimpleSmart",
}


def userland_refusal(platform, userland, guest_kernel=DEFAULT_GUEST_KERNEL):
    """Why the settings userland and guest_kernel cannot run the platform (targets.PLATFORMS), or
    None. The host is not looked at (guest_kernel_host_refusal, resolve_userland)."""
    if guest_kernel == "x86-64":
        if userland == "native":
            return (
                "guest_kernel=x86-64 runs the 32-bit programs under qemu-user: userland=native "
                "is refused with it"
            )
        if platform in NATIVE_ONLY:
            return (
                f"guest_kernel=x86-64: {platform} firmware needs native 32-bit execution "
                f"({NATIVE_ONLY[platform]})"
            )
    if userland == "qemu-user" and platform in NATIVE_ONLY:
        return f"userland=qemu-user: {NATIVE_ONLY[platform]}"
    return None


def guest_kernel_host_refusal(guest_kernel, machine):
    """Why this host (platform.machine()) cannot run guest_kernel, or None."""
    if guest_kernel == "x86-64" and machine not in ("x86_64", "AMD64"):
        return f"guest_kernel=x86-64 needs an x86-64 host, not {machine}"
    return None


def guest_kernel_package_refusal(guest_kernel, tree):
    """Why an installed host (paths.Tree) lacks the kernel of guest_kernel, or None: the x86-64
    one is the optional package beanjar-guest-x86-64. A checkout is not asked: its start says
    which build file is missing."""
    if guest_kernel == "x86-64" and tree.installed:
        if not all(os.path.isfile(f) for f in tree.guest_kernel(guest_kernel)):
            return (
                "guest_kernel=x86-64 needs the beanjar-guest-x86-64 package: "
                "install it (apt install beanjar-guest-x86-64)"
            )
    return None


def resolve_userland(setting, guest_kernel, accel, aarch32_el0, platform=None):
    """(userland, why) a start runs with: native or qemu-user, and the one line the launcher logs.
    setting: vm.toml's userland (auto, native, qemu-user); accel: tcg, kvm or hvf; aarch32_el0:
    whether the host's CPU can run AArch32 at EL0 under the accelerator (True, False, or None where
    the host is not arm64 and the question does not arise). The host rule:
      guest_kernel x86-64            qemu-user: the x86-64 kernel has no AArch32
      arm64 kernel under TCG         native: TCG emulates AArch32 itself
      arm64 kernel under KVM         native where the CPU has AArch32 at EL0, else qemu-user
      arm64 kernel under HVF         qemu-user: Apple silicon has no AArch32 at EL0
    then the firmware's own constraint: a NATIVE_ONLY platform runs native or not at all (Error),
    never falling back to qemu-user. An explicit native or qemu-user is taken as it is (its
    platform refusals are userland_refusal's)."""
    if setting not in USERLANDS:
        raise Error(f"userland={setting}: one of {', '.join(USERLANDS)}", "userland")
    if setting != "auto":
        userland, why = setting, "as set"
    elif guest_kernel == "x86-64":
        userland, why = "qemu-user", "the x86-64 guest kernel has no AArch32"
    elif accel == "tcg":
        userland, why = "native", "TCG emulates AArch32 itself"
    elif accel == "hvf":
        userland, why = "qemu-user", "HVF on Apple silicon has no AArch32 at EL0"
    elif aarch32_el0 is False:
        userland, why = "qemu-user", f"{accel.upper()} on a CPU without AArch32 at EL0"
    else:
        userland, why = "native", f"{accel.upper()} on a CPU with AArch32 at EL0"
    if userland == "qemu-user" and platform in NATIVE_ONLY:
        if setting == "auto":
            raise Error(
                f"{platform} firmware needs native 32-bit execution, which this host cannot "
                f"provide ({why}; {NATIVE_ONLY[platform]})",
                "userland",
            )
        raise Error(f"userland=qemu-user: {NATIVE_ONLY[platform]}", "userland")
    return userland, f"userland: {userland} ({why})"


# qemu-user's own memory in the guest, as a share of the TV's (mem): every 32-bit process runs
# under a qemu-arm of its own, with its heap, its page tracking of the guest program and its
# translated code on top of the program's own memory. mem stays the TV's RAM (what its firmware is
# built for); a qemu-user start gives the guest this much more (guest_ram_mib).
QEMU_USER_OVERHEAD_PERCENT = 50


def qemu_user_overhead_mib(mem, userland):
    """The RAM in MiB a start with userland (native or qemu-user, as resolved) adds to the TV's
    mem MiB for qemu-user's own: QEMU_USER_OVERHEAD_PERCENT of it, rounded down to a whole MiB;
    0 for native."""
    return int(mem) * QEMU_USER_OVERHEAD_PERCENT // 100 if userland == "qemu-user" else 0


def guest_ram_mib(mem, userland):
    """The guest's RAM in MiB (QEMU's -m) for a TV of mem MiB with userland as resolved: mem, and
    qemu-user's overhead on top (qemu_user_overhead_mib)."""
    return int(mem) + qemu_user_overhead_mib(mem, userland)


# Most of qemu-user's overhead is touched once and then cold (one 128 MiB translated-code buffer a
# process): a qemu-user start also gives the guest a swap disk backed by host RAM (hostswap.py),
# this share of the TV's mem, which costs the host only what the guest swaps out.
QEMU_USER_SWAP_PERCENT = 50


def qemu_user_swap_mib(mem, userland):
    """The size in MiB of the host-RAM swap (hostswap.py) a start with userland (native or
    qemu-user, as resolved) gives a TV of mem MiB: QEMU_USER_SWAP_PERCENT of it, rounded down to a
    whole MiB; 0 (none) for native."""
    return int(mem) * QEMU_USER_SWAP_PERCENT // 100 if userland == "qemu-user" else 0


def memory_note(mem, userland):
    """The line a start logs for its RAM when qemu-user adds to it, else None."""
    extra = qemu_user_overhead_mib(mem, userland)
    if not extra:
        return None
    swap = qemu_user_swap_mib(mem, userland)
    return (
        f"memory: {int(mem)} MB for the TV + {extra} MB RAM and {swap} MB host-RAM swap for "
        "qemu-user's overhead"
    )


# mitm_flows (qemu.py MITM_FLOWS): how many flows the mode=mitm inspector keeps, 0 for all of them;
# the default is not written out. A kept flow costs mitmweb about 2.5 MB per 1000 for DNS, 7.5 MB
# per 1000 for HTTP(S) with a few KB of body, 19 MB per 1000 for HTTPS on a new TLS connection each
# (measured with mitmweb 12.3.1): 5000 bounds the last at about 100 MB.
MITM_FLOWS = 5000
# mitm_flow_bytes (qemu.py MITM_FLOW_BYTES): the bytes of bodies those flows may hold, as "200M";
# unset (the default) is no limit


def byte_size(text):
    """A size as vm.toml's network.mitm_flow_bytes writes it: bytes, or a number with K, M or G
    (KiB, MiB, GiB), e.g. 200M; 0 or empty is none. ValueError if it is not one."""
    m = re.fullmatch(r"\s*(\d+)\s*([KMG]?)\s*", str(text).upper())
    if not str(text).strip():
        return 0
    if not m:
        raise ValueError(f"{text!r}: a size such as 200M (bytes, or K, M, G)")
    return int(m.group(1)) << {"": 0, "K": 10, "M": 20, "G": 30}[m.group(2)]


def flow_bytes_label(value):
    """mitm_flow_bytes as the summary shows it: "200 MiB", or "no limit"."""
    n = byte_size(value or "")
    if not n:
        return "no limit"
    for shift, unit in ((30, "GiB"), (20, "MiB"), (10, "KiB")):
        if n >= 1 << shift:
            return f"{n / (1 << shift):g} {unit}"
    return f"{n} bytes"


# The network (docs/dev/architecture/network.md): link, where the guest's NIC is attached (nat: behind
# this host; lan: on lan_parent's wired LAN, a TV of its own there; off: nothing), and mode, whether
# its traffic passes through the proxy (mitm) or not (direct). mode means nothing with link off.
LINKS = ("nat", "lan", "off")


MODES = ("mitm", "direct")


# LG's online services (docs/dev/architecture/network.md, "LG services"): mock, the default, keeps
# the guest from LG's telemetry and cloud hosts (host/helpers/network/lg_hosts.py) and answers its
# online check itself; real lets it reach every LG host as a TV does. One or the other, never mixed.
LG_SERVICES = ("mock", "real")
DEFAULT_LG_SERVICES = "mock"


LAN_NOT_IMPLEMENTED = "link=lan with mode=direct is not implemented yet"


def guest_net(link, mode):
    """What the guest's network is, link and mode together: off, mitm or direct."""
    return "off" if (link or "nat") == "off" else (mode or "mitm")


def network_label(link, mode):
    """link and mode as `beanjar list` and the GUI's summary show them: nat+mitm, lan+direct, off."""
    link = link or "nat"
    return "off" if link == "off" else f"{link}+{mode or 'mitm'}"


def dns_label(name):
    """A VM's name as a DNS label, its DHCP hostname with link lan: lower-case, anything outside
    a-z, 0-9 and "-" ("." and "_" in a name) made "-", none at either end, 63 characters at most."""
    return re.sub(r"[^a-z0-9-]", "-", name.lower())[:63].strip("-")


# The layers. guest/initramfs/rootfs/init stacks the ones LAYERS names, in
# that order, highest priority first (the first overlay lowerdir: its files hide those of the
# layers after it and of the stock rootfs), then targets.ALWAYS_LAYERS, as the launcher's boot.conf
# lists them; a layer named twice is taken at its first place. Above them all is the per-VM layer.
DEFAULT_LAYERS = ("normal", "gl")


# Each layer's description, requires, conflicts and forwards are its own, in guest/layers/<name>/layer.toml
# (layermeta.py). The optional layers' order in the dialogs, after the VM's own; others follow by name.
LAYER_ORDER = ("normal", "gl", "headless", "devmode", "rootssh", "hbchannel")


# debug_mode (qemu.py DEBUG_MODE): the TV's debug status as the board's debugMode=N word has it
DEBUG_MODES = nvm.DEBUG_MODES


DEFAULT_DEBUG_MODE = "debug"


# vm.toml: the VM's settings (these keys, in this order, then the manager's own, e.g. created) and
# the launcher's environment variable each one is (launch.py --dir sets them with vm_launcher_env).
# layers is an array, LAYERS its names joined with commas. Any upper-case key is a launcher
# variable passed through as it is (e.g. PMLOG_DEBUG = "1", TCG_CPU = "max").
SETTINGS = {
    "fw": "FW",
    "broadcast": "BROADCAST",
    "country": "COUNTRY",
    "mem": "MEM",
    "smp": "SMP",
    "free_page_reporting": "FREE_PAGE_REPORTING",
    "userland": "USERLAND",
    "guest_kernel": "GUEST_KERNEL",
    "debug_mode": "DEBUG_MODE",
    "updatable": "UPDATABLE",
    "layers": "LAYERS",
    "link": "NET_LINK",
    "mode": "NET_MODE",
    "display": "DISPLAY_BACKEND",
    "gpu": "GPU_DEVICE",
    "audio": "AUDIO",
    "mac": "MAC",
    "serial": "SERIAL",
    "listen": "LISTEN_ADDRESS",
    "forward": "FORWARD",
    "address": "NET_ADDRESS",
    "lan_access": "NET_LAN_ACCESS",
    "lg_services": "NET_LG_SERVICES",
    "mitm_flows": "MITM_FLOWS",
    "mitm_flow_bytes": "MITM_FLOW_BYTES",
}


# Each setting as `beanjar config set --help` describes it: (its values, its default, what it does,
# what `config unset NAME KEY` brings back, or None where it has no unset). Configure's empty value
# for these keys is that unset; the others refuse an empty value or (layers) take it as no layers.
SETTING_HELP = {
    "fw": (
        "DIR",
        "the VM's own firmware/, which a VM created in place (create --firmware) does not have",
        "an external directory of extracted paks, booted in place and never written",
        "the VM's own firmware/ (none on a VM created in place, which then has no firmware)",
    ),
    "broadcast": (
        "|".join(targets.BROADCASTS),
        "the firmware's own, else the chip's default (US where it ships on US models)",
        "boot a global firmware as that broadcast standard's model",
        "the firmware's own, else the chip's default",
    ),
    "country": (
        "CODE",
        "the board's own (its country group's default; what the TV state holds)",
        "the TV's country, an ISO 3166 alpha-3 code of the board's own country group in the "
        "firmware's configd RMM tables (US board: USA, CAN, MEX); refused where it is not one, "
        "naming them, and before webOS 5; applied at the next start through LG's settings, which "
        "lowlevelstorage and the smart service country follow",
        "the board's own for a new TV state; a state keeps the country it has (wipe for the board's)",
    ),
    "mem": (
        "MIB",
        "the firmware's model's own",
        "the TV's RAM in MiB; a start with userland qemu-user gives the guest half as much again "
        "for qemu-user's own memory",
        None,
    ),
    "smp": ("N", str(vmconfig.DEFAULTS["smp"]), "CPUs", None),
    "free_page_reporting": (
        "|".join(ONOFF),
        "off",
        "hand the guest's free memory back to the host",
        None,
    ),
    "userland": (
        "|".join(USERLANDS),
        DEFAULT_USERLAND,
        "how the firmware's 32-bit ARM programs run: native in AArch32 EL0, or qemu-user, "
        "translated by qemu-arm in the guest with their system calls through the kernel's compat "
        "table; auto picks at each start: native under TCG and where KVM's CPU has AArch32 at "
        "EL0, qemu-user where it has not and with guest_kernel=x86-64. NetCast and SimpleSmart "
        "always run native",
        DEFAULT_USERLAND,
    ),
    "guest_kernel": (
        "|".join(GUEST_KERNELS),
        DEFAULT_GUEST_KERNEL,
        "the guest kernel's architecture; x86-64 is experimental: x86-64 hosts only, q35 with "
        "KVM, the whole ARM userland under qemu-user (userland=native is refused), not NetCast "
        "or SimpleSmart",
        DEFAULT_GUEST_KERNEL,
    ),
    "debug_mode": (
        "|".join(DEBUG_MODES),
        "debug",
        "the TV's debug status, the kernel's debugMode= and the EEPROM's (webOS 1 to 3.9 and 5+; webOS 4.x: the kernel's only): debug is a development "
        "set, with faultmanager's crash popup; event a retail set that keeps event logs, still "
        "with the popup; release a retail set, no popup, and it reboots when tvservice or lginit_app "
        "exits",
        "debug",
    ),
    "updatable": (
        "|".join(ONOFF),
        "off",
        "present a whole-disk /dev/mmcblk0 so the stock firmware updater (/usr/sbin/update) can "
        "run its raw partition writes for study: a dm-linear device over the per-partition disks "
        "and a scratch overlay QEMU discards at power-off, so nothing persists and the inactive "
        "A/B bank and gaps take the writes; off leaves the VM byte-for-byte unchanged",
        None,
    ),
    "layers": (
        "NAME[@VERSION],...",
        ",".join(vmconfig.DEFAULTS["layers"]),
        (
            "guest layers, highest priority first; empty for none. NAME@VERSION picks a versioned "
            "layer's version (hbchannel@0.7.3), plain NAME its default; a newer Homebrew Channel "
            "in the VM's state (one it updated itself to) stays when the layer's is older"
        ),
        None,
    ),
    "link": ("|".join(LINKS), vmconfig.DEFAULTS["link"], "where the NIC is attached", None),
    "mode": (
        "|".join(MODES),
        vmconfig.DEFAULTS["mode"],
        "the traffic inspector or none; ignored with link=off",
        None,
    ),
    "display": ("|".join(DISPLAYS), vmconfig.DEFAULTS["display"], "the display backend", None),
    "gpu": (
        "|".join(GPUS),
        "auto",
        "where the GPU renders: vhost-user-gpu in its own process (ARM hosts with a render node; "
        "a VM run without its unit loses the display at a guest restart), virtio-gpu-gl on QEMU's "
        "main loop; auto is vhost-user-gpu on ARM hosts, else virtio-gpu-gl",
        "auto",
    ),
    "audio": (
        "|".join(AUDIOS),
        "dbus with display=dbus, else none",
        "the audio backend; dbus needs display=dbus",
        None,
    ),
    "mac": ("AA:BB:CC:DD:EE:FF", "made at create", "the board MAC (unicast)", None),
    "serial": ("SERIAL", "made at create", "the serial number, 1 to 16 letters and digits", None),
    "listen": (
        "ADDRESS",
        "the VM's own address",
        "link=nat: where its TV-service ports (SSAP 3000/3001, SSH 6622, developer mode "
        "9922/9991) listen, e.g. 0.0.0.0 for the LAN",
        "the VM's own address",
    ),
    "forward": (
        "PORT[:GUEST],...",
        "none",
        "guest TCP ports relayed on the VM's address as its TV-service ports are (on listen with "
        "link=nat, also on the LAN address with link=lan), e.g. 8080,3000 or 8081:80 for guest 80 "
        "on host 8081; a host port a layer has (SSAP 3000/3001, SSH 6622, 9922, 9991), 5900 (VNC), "
        "18083 (the inspector) or one below 1024 is refused",
        "none",
    ),
    "address": (
        "dhcp|A.B.C.D/NN,GATEWAY",
        "dhcp",
        "link=lan: the VM's LAN address",
        "dhcp",
    ),
    "lan_access": (
        "|".join(ONOFF),
        "off",
        "link=nat: let the guest reach the LAN (private, shared and link-local addresses, "
        "multicast) and this host's own non-loopback addresses; off refuses them. The internet "
        "is reached either way, this host's loopback never. A link=lan VM is on the LAN anyway",
        "off",
    ),
    "lg_services": (
        "|".join(LG_SERVICES),
        DEFAULT_LG_SERVICES,
        "LG's online services: mock refuses LG's telemetry and cloud hosts (crash and analytics "
        "uploads, beacons, ads, ACR, ThinQ and IoT, QuickSet, firmware update, remote diagnostics, "
        "voice) in every mode, answers LG's online check itself and never passes TLS to an LG host "
        "through uninspected; real reaches every LG host as a TV does, for a VM with a real model "
        "key",
        DEFAULT_LG_SERVICES,
    ),
    "mitm_flows": (
        "N",
        str(MITM_FLOWS),
        "how many flows the mode=mitm inspector keeps, 0 for all",
        str(MITM_FLOWS),
    ),
    "mitm_flow_bytes": (
        "SIZE",
        "no limit",
        "the bytes of bodies those flows may hold, e.g. 200M, 0 for no limit",
        "no limit",
    ),
}


assert set(SETTING_HELP) == set(SETTINGS), "every setting has its help"


# The launcher's own variables, which launch.py sets from the VM directory.
LAUNCHER_OWN = {
    "STATE",
    "RUN_DIR",
    "LOG_DIR",
    "SAVED_LOG_DIR",
    "QEMU_PIDFILE",
    "CONSOLE_SOCK",
    "CONSOLE_LOG",
    "WEBOS_LOG",
    "QMP_SOCK",
    "VIEWER_QMP_SOCK",
    "CURSOR_SOCK",
    "VIDEO_SOCK",
    "IR_SOCK",
    "MEDIA_SOCK",
    "CAPTURE_SOCK",
    "DBUS_ADDR",
    "NETSTUB_LOG",
    "NETNS_LOG_DIR",
    "FW_CACHE",
    "BUILD",
    "RUN",
    "IDENTITY_DIR",
    "NETSTUB_CA",
    "SLOT",
    "ADDRESS_PREFIX",
    "VM_ADDRESS",
    "NET_LAN_PARENT",
    "NET_LAN_MAC",
    "NET_LAN_HOSTNAME",
}


# The launcher's variables that are the checkout VM's experiments (host/scripts/vm-launcher), not a VM's: the
# uncompressed rootfs (fwcache.py), whose images a VM does not keep.
CHECKOUT_ONLY = {"ROOTFS", "UNCOMPRESSED_PAKS"}


CONFIG_KEYS = ("repo", "auto_reconnect", "epk2extract", "mac_prefix", "lan_parent", "addresses")


MANIFEST_KEYS = (
    "name",
    "chip",
    "release",
    "epk",
    "epk_size",
    "sha256",
    "source",
    "paks",
    "imported",
)


# A VM's tags (vm.toml's metadata.tags; create --tag, beanjar config set NAME tags=...): short labels for finding and
# sorting VMs (list --tag), kept with its settings and copied by clone. Lower-case letters, digits
# and dashes; conventions only (host/core/README.md): scratch, keep.
TAG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def parse_tag(text):
    """A tag as it is kept: lower-cased; Error unless letters, digits and dashes, at most 32, not
    starting with a dash."""
    tag = str(text).strip().lower()
    if not TAG_RE.fullmatch(tag):
        raise Error(
            f"tag {text!r}: letters, digits and dashes, at most 32, starting with a letter or digit",
            "tags",
        )
    return tag


def as_tags(v):
    """tags as a list without repeats, in the order given: a list, or a string split at commas."""
    if isinstance(v, str):
        v = v.split(",")
    out = []
    for t in v or ():
        t = parse_tag(t)
        if t not in out:
            out.append(t)
    return out


def as_layers(v):
    """layers as a list: a list as it is, a string split at commas (the CLI's --layers)."""
    if isinstance(v, str):
        return [n for n in v.split(",") if n]
    return list(v)


def launcher_env(cfg):
    """vm.toml's settings as the launcher's environment: {variable: value}."""
    env = {}
    for k, v in cfg.items():
        if k in SETTINGS:
            var = SETTINGS[k]
        elif k == "slot":
            var = "SLOT"  # the VM's host address (qemu.py)
        elif re.fullmatch(r"[A-Z][A-Z0-9_]*", k):
            var = k
            if k in LAUNCHER_OWN or k in SETTINGS.values():
                raise Error(f"{k} is set by the VM manager, not in vm.toml", k)
            if k in CHECKOUT_ONLY:
                raise Error(
                    f"{k} is an experiment of the checkout's VM (host/scripts/vm-launcher), not in vm.toml",
                    k,
                )
        else:
            continue  # the manager's own (created)
        if isinstance(v, list) and all(isinstance(i, str) for i in v):
            v = ",".join(v)
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise Error(
                f"{k} must be a string or a number"
                + (" (or, layers, an array of names)" if k == "layers" else ""),
                k,
            )
        env[var] = str(v)
    return env


def validate(cfg, saved=False):
    """Error (with the setting's key) if cfg is not a VM's settings. The layers are checked against
    their layer.toml (layermeta.check); saved: cfg is read back from a vm.toml, whose layers may name
    one this tree lacks (the dialogs show it and keep it until it is switched off)."""
    checks = (
        ("platform", targets.PLATFORMS),
        ("link", LINKS),
        ("mode", MODES),
        ("display", DISPLAYS),
        ("gpu", GPUS),
        ("audio", AUDIOS),
        ("free_page_reporting", ONOFF),
        ("updatable", ONOFF),
        ("lan_access", ONOFF),
        ("lg_services", LG_SERVICES),
        ("userland", USERLANDS),
        ("guest_kernel", GUEST_KERNELS),
        ("debug_mode", tuple(DEBUG_MODES)),
    )
    for key, allowed in checks:
        if key in cfg and cfg[key] not in allowed:
            raise Error(f"{key}={cfg[key]}: one of {', '.join(allowed)}", key)
    for key in ("mem", "smp"):
        if key in cfg and (
            isinstance(cfg[key], bool) or not isinstance(cfg[key], int) or cfg[key] <= 0
        ):
            raise Error(f"{key} must be a positive number", key)
    if "mitm_flows" in cfg and (
        isinstance(cfg["mitm_flows"], bool)
        or not isinstance(cfg["mitm_flows"], int)
        or cfg["mitm_flows"] < 0
    ):
        raise Error("mitm_flows must be a number, 0 for no limit", "mitm_flows")
    if "mitm_flow_bytes" in cfg:
        try:
            byte_size(cfg["mitm_flow_bytes"])
        except ValueError as e:
            raise Error(f"mitm_flow_bytes={e}", "mitm_flow_bytes") from None
    refusal = userland_refusal(
        cfg.get("platform"),
        cfg.get("userland", DEFAULT_USERLAND),
        cfg.get("guest_kernel", DEFAULT_GUEST_KERNEL),
    )
    if refusal:
        raise Error(refusal, refusal.split("=", 1)[0])
    if cfg.get("audio") == "dbus" and cfg.get("display") != "dbus":
        raise Error("audio=dbus needs display=dbus", "audio")
    if "slot" in cfg:
        from . import keys

        if isinstance(cfg["slot"], bool) or cfg["slot"] not in keys.SLOTS:
            raise Error(
                f"slot {cfg['slot']!r}: a number from {keys.SLOTS[0]} to {keys.SLOTS[-1]}", "slot"
            )
    if "mac" in cfg:
        vmidentity.parse_mac(cfg["mac"])
    if "serial" in cfg:
        vmidentity.parse_serial(cfg["serial"])
    if "tags" in cfg:
        tags = cfg["tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise Error("tags must be an array of strings", "tags")
        if as_tags(tags) != tags:
            raise Error(f"tags {tags}: lower-case, each once", "tags")
    link = cfg.get("link", "nat")
    if "listen" in cfg:
        from . import keys

        if link == "lan":
            raise Error(
                "listen is for link=nat: a link=lan VM's ports are on its own LAN address", "listen"
            )
        try:
            keys.listen_address(cfg["listen"])
        except keys.Error as e:
            raise Error(str(e), "listen") from None
    if "forward" in cfg:
        from . import layermeta

        if not isinstance(cfg["forward"], str):
            raise Error("forward must be a string of ports, e.g. 8080,8081:80", "forward")
        try:
            layermeta.parse_forward(cfg["forward"])
        except layermeta.Error as e:
            raise Error(str(e), "forward") from None
    if cfg.get("lan_access") == "on" and link == "lan":
        raise Error("lan_access is for link=nat: a link=lan VM is on the LAN anyway", "lan_access")
    if "address" in cfg:
        from . import keys

        if link != "lan":
            raise Error(f"address is for link=lan only (this VM is link={link})", "address")
        try:
            keys.lan_address(cfg["address"])
        except keys.Error as e:
            raise Error(str(e), "address") from None
    layers = cfg.get("layers", [])
    if not isinstance(layers, list) or not all(
        isinstance(n, str) and re.fullmatch(r"[a-z0-9_-]+(@[0-9A-Za-z][0-9A-Za-z._+-]*)?", n)
        for n in layers
    ):
        raise Error(
            "layers is a list of layer names (a-z, 0-9, '_', '-'), each NAME or NAME@VERSION",
            "layers",
        )
    from . import layermeta

    try:
        layermeta.check(layers, unknown_ok=saved)
    except layermeta.Error as e:
        raise Error(str(e), "layers") from None


def layer_list(layers, available):
    """The rows of the dialogs' layer list: [(name, on)], the names layers has first, in its order
    (on), then the other optional layers of available (Library.layers()), off, in LAYER_ORDER and
    then by name. A layer that guest/initramfs/rootfs/init adds by itself is listed only where layers has it."""
    rows = []
    for n in layers or ():
        if n and n not in (r[0] for r in rows):
            rows.append((n, True))
    auto = set(targets.ALWAYS_LAYERS)
    rank = {n: i for i, n in enumerate(LAYER_ORDER)}
    named = {r[0].partition("@")[0] for r in rows}  # NAME@VERSION is NAME's row
    rest = sorted(
        (n for n in available if n not in auto and n not in named),
        key=lambda n: (rank.get(n, len(rank)), n),
    )
    return rows + [(n, False) for n in rest]
