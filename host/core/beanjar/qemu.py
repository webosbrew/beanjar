# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The QEMU command line of a VM, from the launcher's environment (plan()), and its state disks.

Two machines, by ACCEL (default kvm on an aarch64 host, tcg elsewhere):
  tcg  the WT1H emulator on an x86_64 host with TCG
  kvm  a Raspberry Pi 5 (Cortex-A76) with KVM and virgl. The A76 has AArch32 at EL0 only, so the
       arm64 kernel runs the 32-bit userland in COMPAT mode under -cpu host.
With GUEST_KERNEL=x86-64 (experimental) the machine is q35 instead of virt, on an x86-64 host, under
KVM when /dev/kvm opens (else TCG), with the x86-64 guest kernel and the ARM userland under qemu-arm
(docs/dev/architecture/qemu-user-bridge.md, "The x86-64 guest kernel").

Environment (both):
  FW        firmware directory with the .pak files (paths.Tree.firmware)
  SLOT      the VM's slot (vm.toml's network.slot; required): its host address VM_ADDRESS is
            ADDRESS_PREFIX.SLOT (keys.address), where every host port of it below is, at the TV's numbers
  ADDRESS_PREFIX  the first three octets of VM_ADDRESS (the VM's library's addresses, set by
            launch.py; default 127.0.1)
  LAYERS    extra layers, highest priority first (default: headless; an empty LAYERS stays empty);
            rootssh adds root SSH on host VM_ADDRESS:SSH_PORT (default 6622, guest/layers/rootssh), devmode
            the jailed ssh (prisoner) on 9922 and the key server on 9991, as ares expects on a TV;
            hbchannel (guest/layers/hbchannel; not with rootssh, both own the guest's 22) forwards
            the same SSH_PORT to Homebrew Channel's Dropbear, and nothing to its telnet (23);
            hbchannel@VERSION picks the Homebrew Channel release (layermeta.py; plain: the default)
            On webOS the webos layer forwards SSAP, the second-screen API, on 3000 (ws) and 3001 (wss)
  LISTEN_ADDRESS  the address the TV-service ports (layermeta.forwards: 3000, 3001, 6622 or SSH_PORT,
            9922, 9991) listen on (vm.toml's listen; default VM_ADDRESS): 0.0.0.0 or one of the
            host's addresses opens them to the LAN. The ports stay the TV's, so two VMs cannot
            share an address there (core.listen_conflict). The inspector and VNC have their own
  FORWARD   the VM's own guest TCP ports on the same address (vm.toml's forward; default none):
            "8080,8081:80", each PORT or HOST:GUEST (layermeta.parse_forward), forwarded as the
            layers' are; a host port a layer has, or 5900 (VNC) or 18083 (the inspector), is refused
  FREE_PAGE_REPORTING  on or off (default; vm.toml's hardware.free_page_reporting): on turns on
            free page reporting on the VM's virtio-balloon (every VM has one, for the guest's memory
            statistics, never inflated, so the guest still has all of MEM), and the host takes back
            the free 4 MiB blocks the guest reports. Opt-in: it has a cost.
  UPDATABLE  on or off (default; vm.toml's firmware.updatable): on attaches a whole-disk
            /dev/mmcblk0 (a dm-linear device the guest builds, bin/beanjar-emmc) so the stock
            firmware updater (/usr/sbin/update) can run its raw partition writes for study. It adds
            one scratch overlay disk (serial emmc-overlay) the updater's inactive-bank writes land
            in, attached snapshot=on so they are discarded at power-off, and attaches partinfo
            snapshot=on too so the bank swap does not persist. Off leaves the VM byte-for-byte
            unchanged.
  USERLAND  auto (default), native or qemu-user (vm.toml's hardware.userland): qemu-user runs the
            firmware's 32-bit ARM programs under qemu-arm in the guest, their system calls through
            the guest kernel's compat table (docs/dev/architecture/qemu-user-bridge.md): the
            launcher puts qemu-arm into the host share and boot.conf's USERLAND has init register
            it with binfmt_misc. auto picks native or qemu-user by the host and the accelerator
            (vmsettings.resolve_userland, host_aarch32_el0) and says which and why. NetCast and
            SimpleSmart always run native (vmsettings.NATIVE_ONLY): where the host cannot, they
            do not start
  GUEST_KERNEL  arm64 (default) or x86-64 (vm.toml's hardware.guest_kernel; experimental): the
            x86-64 guest kernel (build/x86_64) on q35, x86-64 hosts only, userland qemu-user
  QEMU_ARM  that qemu-arm (paths.Tree.qemu_arm): a local build for development; default
            beanjar-vendor's qemu-user, once it is pinned: build/qemu-user/bin/qemu-arm (aarch64)
            for the arm64 kernel, build/qemu-user-amd64/bin/qemu-arm (x86-64) for the x86-64 one
  QEMU_AARCH64  the x86-64 kernel's qemu-aarch64 for the 64-bit ARM programs
            (paths.Tree.qemu_aarch64): a local build for development; default
            build/qemu-user-amd64/bin/qemu-aarch64. Without one those programs do not run; the
            arm64 kernel runs them natively
  QEMU_X86_64  the x86-64 kernel's qemu-system-x86_64 (paths.Tree.qemu_x86_64): a local build for
            development; default the vendored one, build/qemu/bin/qemu-system-x86_64 (amd64)
  DEBUG_MODE  debug, event or release (vm.toml's firmware.debug_mode; default: the board's own
            debugMode=, 3 = debug): the TV's debug status. The guest's /proc/cmdline has that one
            debugMode= (webos.debugmode= for /init, or the board words on the kernel's line,
            Release.board_in_kernel), and nvm.prepare brings the EEPROM's debug status to it
  COUNTRY   the TV's country, an ISO 3166 alpha-3 code (vm.toml's firmware.country): one of the
            board's RMM country group (countries.check), else refused; the per-VM layer's
            etc/emulator/country, which the guest's emulator-country sets through LG's settings
            once per value. Default: none, the board's own
  MEM, SMP  the TV's RAM in MiB (default: the firmware's model's DDR, its board.cmdline's
            hwopt=: nvm.board_ram) / CPUs (default 4). A qemu-user start gives the guest more
            (-m: vmsettings.guest_ram_mib, the TV's and qemu-user's overhead) and a swap disk in
            host RAM as large again (vmsettings.qemu_user_swap_mib, hostswap.py), and says so
  NET_LINK  where the guest's NIC is attached (vm.toml's network.link; docs/dev/architecture/network.md):
              nat     (default) behind this host, on its own 10.0.2.0/24, as NET_MODE says
              lan     on the host's wired LAN (the library's lan_parent), a TV of its own there:
                      mode mitm one namespace further in (netns.lan; direct is refused),
                      its TV-service ports and the inspector on its LAN address, and on
                      VM_ADDRESS for the host itself
              off     no network, like a TV with no cable: the NIC is present with its link down and
                      nothing behind it (netns.net_off). No port forwards. NET_MODE is ignored.
  NET_MODE  mitm (default) or direct (vm.toml's network.mode), with NET_LINK=nat. Both run in a
            rootless network namespace (netns.py; needs unshare, ip, slirp4netns and unprivileged
            user namespaces) with slirp4netns's NAT as its uplink: DHCP (10.0.2.15), real DNS at
            10.0.2.3, the router 10.0.2.2, and the host's loopback out of reach in either mode:
              mitm    every TCP and UDP connection is routed into mitmweb's TUN device (tun
                      mode; the user's own mitmproxy 12.0.1 or newer, mitmtool.py, NETNS_MITMWEB),
                      which records it and forwards it live (host/helpers/network/mitm_addon.py);
                      without a usable mitmweb the guest goes out directly. Inspector
                      on NETSTUB_WEB_HOST:NETSTUB_WEB_PORT (default VM_ADDRESS:18083). The proxy's
                      CA is NETSTUB_CA (default IDENTITY_DIR/ca, the development library's ca/; a --dir
                      VM's is its library's <library>/ca); its certificate goes into the per-VM
                      layer, and the guest adds it to the firmware's trust stores at boot. The
                      proxy does not verify upstream servers (ssl_insecure); a TLS failure has
                      later connections to that endpoint passed through undecrypted (mitm_addon.py).
              direct  the same namespace without mitmweb: nothing is inspected
  NET_LAN_ACCESS  on or off (default; vm.toml's network.lan_access), with NET_LINK=nat: on lets
            the guest reach the LAN (the private ranges, shared address space, link-local and
            multicast) and the host's own non-loopback addresses, which off refuses (netns.py's
            BLOCK_PREF and HOST_PREF rules). NET_LINK=lan is always on. The internet is reached
            either way, the host's loopback never
  NET_LG_SERVICES  mock (default; vm.toml's network.lg_services) or real, with NET_LINK=nat or
            lan: mock refuses LG's telemetry and cloud hosts (host/helpers/network/lg_hosts.py) by
            the namespace's resolver and policy rules, answers LG's online check, SDP and web
            hosts there (host/helpers/network/lg_mock.py, lg_sdp.py; the guest trusts the proxy's
            CA in direct too) and has mitm_addon.py pass no LG host through undecrypted; real
            reaches LG as a TV does
  NET_ADDRESS  NET_LINK=lan only: dhcp (default) or A.B.C.D/NN,GATEWAY (vm.toml's network.address)
  MITM_FLOWS  NET_MODE=mitm only: how many flows the inspector keeps (vm.toml's network.mitm_flows,
            default 5000, 0 for all of them); the oldest finished ones are dropped
            (mitm_addon.py's FlowLimit)
  MITM_FLOW_BYTES  NET_MODE=mitm only: how many bytes of bodies the kept flows may hold
            (vm.toml's network.mitm_flow_bytes: bytes or K, M, G, e.g. 200M; default 0, no limit);
            beyond it the oldest finished flows are dropped as well
            NET_DUMP=file.pcap records the guest's traffic.
  ROOTFS    stock (default) or uncompressed: uncompressed SquashFS copies (fwcache.py, in FW_CACHE,
            default build/firmware-cache) of the paks in UNCOMPRESSED_PAKS (default rootfs); a
            perf experiment of the checkout's VM, not a VM manager setting
  STATE     state directory with the persistent partition images (default RUN_DIR/state/default,
            build/state/default; relative to the tree), or "none": a fresh state, the VM's
            persistent one untouched. In a unit (unit.run) that is FRESH_STATE, the run's one
            temporary state directory (lifecycle.fresh_state, RUN_DIR/state/.fresh), empty at the
            run's start: its disks and EEPROM are made and programmed at the first launch, used as
            they are by every launch after a guest restart, as a set keeps what it wrote across a
            reboot, and removed when the run ends. Without FRESH_STATE (a foreground run, where QEMU
            resets in place) the disks are blank images in RUN_DIR/state/.blank that QEMU never
            writes (snapshot=on; QEMU keeps the writes in an unlinked file in TMPDIR until it exits;
            the launcher programs the blank EEPROM there). The guest formats blank disks with the
            firmware's own mkfs.ext4. QEMU locks the images, so two VMs cannot share a state.
            `host/scripts/vm-launcher reset-state` wipes one.
  MAC, SERIAL  the TV's board MAC (the NIC's, and ethaddr=) and serial number (serialNum=): a
            VM's own (vm.toml's mac and serial) on the kernel cmdline as webos.ethaddr= and
            webos.serial=; unset, board.cmdline's (a8:23:fe:12:34:56, 501RAXX00001) stand
  IDENTITY_DIR  the VM's own files (default: the managed development VM's identity/; a --dir VM's <vm>/identity):
            its SSH keys (beanjar/keys.py) and sestore.toml, its secure-store items. At each launch guest_layer() assembles
            RUN_DIR/share/vm from it, with only what the guest needs (the host keys, root's
            authorized_keys and the plain developer key for the layers in LAYERS; the store bundle;
            the CA's certificate with NET_MODE=mitm), which the guest mounts from the read-only 9p share
            "vm" and stacks as its top layer (guest/initramfs/rootfs/init)
  RUN_DIR   the managed VM runtime directory (INSTANCE selects a development VM). The
            guest's shares are its runtime tree
            RUN_DIR/share (share.py) and nothing else: vm/ (the per-VM layer, read-only 9p "vm"),
            host/ (read-only "host": boot.conf, the layers, binds.conf and the guest-side scripts, assembled
            at each launch; /host, /mnt/host in webOS) and out/ (read-write "out": the guest's
            logs and results; /host/out). The checkout itself is not shared
  QEMU      the qemu-system-aarch64 to run (default paths.Tree.qemu: the vendored, patched QEMU,
            build/qemu or beanjar-qemu's, if it is there, else qemu-system-aarch64 from PATH)
  QEMU_EXTRA  extra QEMU arguments (split at spaces)
  DISPLAY_BACKEND, GPU_RES (default: the model's graphic plane, its board.cmdline's hwopt=:
            nvm.board_graphic; 1920x1080 but on M2 and M3, 1280x720), RENDERNODE, AUDIO (dbus,
            none, off): per machine below
  CONSOLE_SOCK  the serial console on this unix socket instead of stdio; CONSOLE_LOG records it
  WEBOS_LOG, QEMU_PIDFILE, QMP_SOCK, VIEWER_QMP_SOCK, CURSOR_SOCK, VIDEO_SOCK, IR_SOCK, MEDIA_SOCK,
  CAPTURE_SOCK, DBUS_ADDR  runtime files (default under RUN_DIR, WEBOS_LOG under LOG_DIR; both use managed VM directories)
  LIFECYCLE_SOCK  unit.run's listening socket (lifecycle.py): QEMU connects a QMP monitor to it and
            runs with -action reboot=shutdown, so a guest restart ends it; unset (a foreground run),
            QEMU resets in place
  PMLOG     "stock" keeps PmLogDaemon's whitelist; PMLOG_DEBUG "ctx1 ctx2" raises contexts to debug
  KERNEL_ARGS  extra kernel cmdline words (split at spaces), e.g. mstar_utopia.mbx_trace=1 (a module's
            parameter: guest/initramfs/rootfs/init)
tcg only:
  the kernel cmdline gets webos.time_scale=TIME_SCALE["tcg"]: bootd's fallback timers stretched
  TCG_CPU (default max,pauth=off), TCG_THREADS (single, multi), TCG_TB_SIZE
  DISPLAY_BACKEND  none (default: no GPU), or a virgl GPU rendered by the host's GL on RENDERNODE
            (default tcg_rendernode(): the first /dev/dri/renderD*, else surfaceless, which only
            the vendored QEMU supports; the host build builds it where this is surfaceless):
            egl-headless, or dbus (-display dbus,gl=on at DBUS_ADDR, for the viewer). On WSL2 (/dev/dxg)
            GALLIUM_DRIVER defaults to d3d12 on the surfaceless display.
  AUDIO     default dbus with DISPLAY_BACKEND=dbus, else none (a card on -audiodev none; off leaves it out)
kvm only: DISPLAY_BACKEND egl-headless (default), dbus, or none (no GPU); RENDERNODE default
  /dev/dri/renderD128; AUDIO default dbus with dbus, else none.
Either: VIRGL_USE_INTEGER defaults to 1 on a panfrost render node (INTEGER_INPUT_DRIVERS).
  GPU_DEVICE  where a GPU's virgl renders (vm.toml's display.gpu; docs/dev/architecture/gpu-process.md):
            auto (default) is vhost-user-gpu on an aarch64 host with a render node,
            else virtio-gpu-gl; vhost-user-gpu runs it in QEMU's own backend process
            (VHOST_USER_GPU, default the vendored QEMU's libexec/vhost-user-gpu), which needs the
            guest RAM on a shared memfd; virtio-gpu-gl keeps it on QEMU's main loop

Consoles: ttyAMA0 (stdio or CONSOLE_SOCK) has the kernel log and the initramfs debug shell; hvc0
(virtio console) is webOS's /dev/console, written to WEBOS_LOG.
"""

import glob
import os
import platform
import re
import stat
import shutil
import subprocess
import sys
from pathlib import Path

from . import (
    capture_relay,
    fwcache,
    lz4p,
    guestshell,
    hostswap,
    ir_relay,
    keys,
    layerimage,
    layermeta,
    nvm,
    targets,
    partmap,
    paths,
    prepare,
    share,
    tuner,
    vmsettings,
)

MEDIA_PORTS = 8
MEDIA_POOL = "media-pool"  # the media pool's file (WEBOS_MEDIA_POOL_SIZE), the helper's
MEDIA_POOL_SOCK = "media-pool.sock"  # its ivshmem server, the helper on the launcher's socket
# the GPU process's vhost-user socket, the launcher's listening one
GPU_SOCK = "vhost-user-gpu.sock"
# seconds between the balloon's guest memory statistics (the viewer's memory meter)
BALLOON_STATS_S = 2
MEDIA_HELPER = (
    "build/media-helper/media-helper"  # the VM's end of the media ports (host/helpers/media-helper)
)


def media_helper(top, env):
    """The sole decoder, or None for pattern-only MEDIAHUB=0/not-built operation.
    Direct compressed sockets are never decoded by the viewer."""
    path = paths.Tree(top).build("media-helper", "media-helper")
    return path if env.get("MEDIAHUB", "1") != "0" and os.access(path, os.X_OK) else None


MEDIA_STREAMS = "media-streams"  # in RUN_DIR: the media helper's open sessions (--streams)


def qemu_side(path):
    """QEMU's own socket behind a hub-served one: video.sock -> qemu-video.sock."""
    return os.path.join(os.path.dirname(path), "qemu-" + os.path.basename(path))


DBUS_AUDIO = (
    "-audiodev",
    "dbus,id=snd0,out.mixing-engine=on,out.fixed-settings=on,out.frequency=44100,"
    "out.channels=2,out.format=s16,nsamples=441",
    "-device",
    "virtio-sound-pci,audiodev=snd0,streams=1",
)
NONE_AUDIO = ("-audiodev", "none,id=snd0", "-device", "virtio-sound-pci,audiodev=snd0,streams=1")


class Error(Exception):
    pass


KVM_DEVICE = "/dev/kvm"


def default_accel(guest_kernel=vmsettings.DEFAULT_GUEST_KERNEL):
    """kvm on a host of the guest kernel's architecture (aarch64 for arm64, x86_64 for x86-64)
    whose /dev/kvm this user may open, else tcg."""
    machine = "x86_64" if guest_kernel == "x86-64" else "aarch64"
    if platform.machine() == machine and os.access(KVM_DEVICE, os.R_OK | os.W_OK):
        return "kvm"
    return "tcg"


def vm_accel(env):
    """The accelerator a launch with env gets, as plan picks it (ACCEL, else default_accel)."""
    return env.get("ACCEL") or default_accel(
        env.get("GUEST_KERNEL") or vmsettings.DEFAULT_GUEST_KERNEL
    )


# personality(2)'s PER_LINUX32
PER_LINUX32 = 0x0008


def host_aarch32_el0():
    """Whether this host's CPU runs AArch32 at EL0, as its kernel sees it, or None on a host that
    is not arm64. The source is the arm64 kernel's own answer: personality(PER_LINUX32) fails with
    EINVAL unless system_supports_32bit_el0(), the sanitised ID_AA64PFR0_EL1.EL0 of every CPU
    (arch/arm64/kernel/sys.c), which is what KVM offers its guests too. Asked in a child process,
    so the launcher's own personality never changes."""
    if platform.machine() != "aarch64":
        return None
    probe = (
        "import ctypes, sys; libc = ctypes.CDLL(None, use_errno=True); "
        f"sys.exit(0 if libc.personality({PER_LINUX32}) != -1 else 1)"
    )
    return subprocess.run([sys.executable, "-c", probe], check=False).returncode == 0


# QEMU's default translation-block cache under TCG (tb-size), 1 GiB on a 64-bit host.
TCG_TB_DEFAULT_MIB = 1024


def tcg_tb_mib(env):
    """The TB cache a TCG launch with env may hold in MiB (TCG_TB_SIZE, else QEMU's default)."""
    v = env.get("TCG_TB_SIZE")
    return int(v) if v and v.isdigit() and int(v) > 0 else TCG_TB_DEFAULT_MIB


# How much slower than a TV the guest runs, by ACCEL: the factor the guest's fixed fallback timers
# (bootd's signal waits and memchute's heartbeat; webos.time_scale=, guest/initramfs/rootfs/init,
# guest/layers/webos/src/bootd-scale and memchute-scale) and scripts/regress's own deadlines are
# stretched by. KVM runs at TV speed. Under TCG, with four
# VMs on the PC, tvservice's tv-ready came up to 94 s after bootd began its 60 s wait (m3) and 45 s
# into h13's 30 s (scratch/timed-gates-study.md, section 6), i.e. up to 1.6 times the timeout;
# alone, core-boot-done was 1.3 to 2.5 times KVM's (docs/dev/guides/build.md). 4 leaves 2.5 times that margin
# and still ends a real hang within minutes.
TIME_SCALE = {"kvm": 1, "tcg": 4}


def time_scale(accel=None):
    return TIME_SCALE[accel or default_accel()]


size_bytes = targets.size_bytes


class Plan:
    """A launch: argv (QEMU and its arguments), wrap ("netns" or "off": what netns.py puts in
    front of QEMU, netns.run or netns.off), env (the environment to run it in), disks ([(image, size, seed)] to create;
    seed, the file a new one starts with, or None; images to remove first in remove), partmap (the
    firmware's partinfo disk image in the cache; None in a dry plan), identity_dir, layer_dir and ca (the per-VM layer
    that guest_layer() makes; ca, the proxy CA's certificate, None
    unless NET_MODE=mitm or LG's services are mocked in direct), ca_dir (the proxy's CA, NETSTUB_CA), run_dir, host_dir and out_dir (the
    guest's other shares, share.py), tree, fw (the firmware directory) and fw_name (its name),
    target (the firmware's (chip, release), paths.firmware_target)."""

    def __init__(self):
        self.argv, self.wrap, self.env, self.disks, self.remove, self.state_dir = (
            [],
            None,
            {},
            [],
            [],
            None,
        )
        self.identity_dir = self.layer_dir = self.ca = self.ca_dir = None
        self.sestore = None  # (missing policy, configured item bytes), webOS only
        self.run_dir = self.host_dir = self.out_dir = self.tree = self.target = self.fw = (
            self.fw_name
        ) = None
        self.broadcast = None  # the board's broadcast standard (targets.board_broadcast)
        self.country = None  # COUNTRY, checked against the board's (countries.check)
        # USERLAND: "native" or "qemu-user" as resolved (vmsettings.resolve_userland), with
        # qemu_arm the interpreter the guest gets; GUEST_KERNEL: "arm64" or "x86-64", whose
        # qemu-user guest also gets qemu_aarch64 for the 64-bit ARM programs
        self.userland, self.qemu_arm, self.qemu_aarch64 = "native", None, None
        self.guest_kernel = vmsettings.DEFAULT_GUEST_KERNEL
        self.layers, self.ssh_port, self.partmap = [], keys.ROOTSSH_PORT, None
        self.guest_layers = []  # share.resolve_layers': each a read-only disk, layerimage.py
        self.service_host = None
        self.address = None  # the VM's host address (SLOT: keys.address), its ports' own
        self.display_setup = (
            None  # D-Bus address, QMP socket and options for secondary console before boot
        )
        self.hub_argv = None  # the media hub's command (its --pid filled in by the launcher)
        # the media pool's server socket, which the launcher listens on before it starts the hub
        # (passed as --pool-fd) and QEMU; None without the pool
        self.pool_socket = None
        # the GPU's own process (GPU_DEVICE, vhost-user-gpu): its command, to which the launcher
        # adds --fd (beanjar.gpu_backend), and the vhost-user socket it listens on before QEMU starts;
        # both None with virtio-gpu-gl
        self.gpu_backend_argv = None
        self.gpu_socket = None
        # the micom's IR receiver (ir_relay.py), which the launcher starts on ir.sock before QEMU:
        # (ir.sock, QEMU's socket for the webos.ir port, the QMP socket it wakes the guest on)
        self.ir_relay = None
        # the capture relay (capture_relay.py), which the launcher starts on capture.sock before
        # QEMU: (capture.sock, QEMU's socket for the webos.capture port, (viewer, display bus
        # address) for its sink or None); None without the port
        self.capture_relay = None
        # `beanjar shell`'s relay (guestshell.py), which the launcher starts on shell.sock before
        # QEMU: (shell.sock, QEMU's socket for the webos.shell port)
        self.shell_relay = None
        self.prepared = None  # the VM's prepared board data (prepare.Prepared; None in a dry plan)
        # UPDATABLE (firmware.updatable on): the whole-disk /dev/mmcblk0 for the stock updater.
        # emmc_overlay is the scratch overlay disk's serial (writes discarded, snapshot=on);
        # emmc_table is its dm-linear table words (partmap.emmc_table_words), None/[] off or dry.
        # emmc_part_nodes is the overlay-backed per-partition node words (partmap.emmc_part_words):
        # /dev/mmcblk0pN for the partitions the updater touches that have no real backing disk
        # (crc32info, the inactive A/B bank), each a slice of the same overlay.
        self.updatable, self.emmc_overlay, self.emmc_table = False, None, []
        self.emmc_part_nodes = []


def _get(env, k, default=""):
    """${K:-default}"""
    return env.get(k) or default


def vm_mem(env, tree=paths.TREE):
    """The TV's RAM in MiB a launch with env gives the VM, as plan does (MEM, else the firmware's
    model's: nvm.board_ram); None if it is not known. The guest's own is vm_guest_ram's."""
    try:
        if env.get("MEM"):
            return int(env["MEM"])
        fw, fw_name = tree.firmware(env.get("FW"))
        target = paths.firmware_target(fw, fw_name)
        return nvm.board_ram(
            target, tree, targets.board_broadcast(target, fw, env.get("BROADCAST"))
        )
    except (ValueError, OSError, targets.Error):
        return None


def vm_userland(env):
    """The userland (native or qemu-user) a launch with env resolves to, as plan does on this host
    (vmsettings.resolve_userland, without the firmware's refusals); None for a setting that does
    not resolve."""
    guest_kernel = env.get("GUEST_KERNEL") or vmsettings.DEFAULT_GUEST_KERNEL
    accel = vm_accel(env)
    try:
        return vmsettings.resolve_userland(
            env.get("USERLAND") or vmsettings.DEFAULT_USERLAND,
            guest_kernel,
            accel,
            host_aarch32_el0() if accel == "kvm" and guest_kernel == "arm64" else None,
        )[0]
    except vmsettings.Error:
        return None


def vm_guest_ram(env, tree=paths.TREE):
    """The guest's RAM in MiB (QEMU's -m) a launch with env gets: the TV's (vm_mem) with
    qemu-user's overhead on top when its userland resolves to qemu-user
    (vmsettings.guest_ram_mib); None if the TV's is not known."""
    mem = vm_mem(env, tree)
    return None if mem is None else vmsettings.guest_ram_mib(mem, vm_userland(env))


def vm_host_swap(env, tree=paths.TREE):
    """The size in MiB of the swap disk in host RAM (hostswap.py) a launch with env gets: under
    qemu-user vmsettings.qemu_user_swap_mib of the TV's RAM (vm_mem), else 0; 0 too if the TV's
    is not known."""
    mem = vm_mem(env, tree)
    return 0 if mem is None else vmsettings.qemu_user_swap_mib(mem, vm_userland(env))


def plan(env, words=(), accel=None, tree=paths.TREE, say=None, dry=False):
    """The launch of env (the launcher's environment, a mapping), with the kernel cmdline words.
    say(line) gets the notes the launch prints (default: stderr). dry: build nothing (ROOTFS=
    uncompressed images are looked up, not built; the model key is not checked)."""
    say = say or (lambda line: print(line, file=sys.stderr, flush=True))
    guest_kernel = env.get("GUEST_KERNEL") or vmsettings.DEFAULT_GUEST_KERNEL
    if guest_kernel not in vmsettings.GUEST_KERNELS:
        raise Error(f"GUEST_KERNEL={guest_kernel}: one of {', '.join(vmsettings.GUEST_KERNELS)}")
    accel = accel or env.get("ACCEL") or default_accel(guest_kernel)
    if accel not in ("tcg", "kvm"):
        raise Error(f"unknown ACCEL={accel} (tcg or kvm)")
    from . import development

    explicit_ca = "NETSTUB_CA" in env
    env = development.environment(env, tree)
    p = Plan()
    p.env = dict(env)
    if not explicit_ca:
        p.env.pop("NETSTUB_CA", None)
    top = tree.top
    fw, fw_name = tree.firmware(env.get("FW"))
    p.fw, p.fw_name = fw, fw_name
    try:
        p.target = paths.firmware_target(fw, fw_name)  # (chip, release): the layers the guest gets
    except ValueError as e:
        raise Error(str(e)) from None
    try:
        # the board's broadcast: the firmware's (model_id.json), else vm.toml's firmware.broadcast (BROADCAST)
        p.broadcast = targets.board_broadcast(p.target, fw, env.get("BROADCAST"))
    except targets.Error as e:
        raise Error(f"BROADCAST: {e}") from None
    if env.get("COUNTRY"):
        from . import countries

        try:
            p.country = countries.parse(env["COUNTRY"])
            board = countries.check(fw, p.target, p.country, tree, p.broadcast)
        except countries.Error as e:
            raise Error(f"COUNTRY: {e}") from None
        if board and board.names.get(p.country):
            # where the mocked LG server tells the TV it is (netns.py, lg_mock.py --country)
            p.env["LG_MOCK_COUNTRY"] = f"{p.country},{board.names[p.country]},{board.group}"
    p.guest_kernel = guest_kernel
    platform_name = targets.platform(p.target)
    if guest_kernel == "x86-64":
        refusal = vmsettings.guest_kernel_host_refusal(guest_kernel, platform.machine())
        if refusal:
            raise Error(refusal)
        refusal = vmsettings.guest_kernel_package_refusal(guest_kernel, tree)
        if refusal:
            raise Error(refusal)
        say(
            "guest kernel: x86-64 (experimental): q35 under "
            f"{accel.upper()}, the ARM userland under qemu-arm"
        )
    setting = env.get("USERLAND") or vmsettings.DEFAULT_USERLAND
    refusal = vmsettings.userland_refusal(platform_name, setting, guest_kernel)
    if refusal:
        raise Error(refusal)
    try:
        p.userland, why = vmsettings.resolve_userland(
            setting,
            guest_kernel,
            accel,
            host_aarch32_el0() if accel == "kvm" and guest_kernel == "arm64" else None,
            platform_name,
        )
    except vmsettings.Error as e:
        raise Error(str(e)) from None
    say(why)
    if p.userland == "qemu-user":
        p.qemu_arm = tree.qemu_arm(env, guest_kernel)
        if not p.qemu_arm and not dry:
            vendored = "qemu-user-amd64" if guest_kernel == "x86-64" else "qemu-user"
            where = (
                "beanjar-qemu's"
                if tree.installed
                else "beanjar-vendor's qemu-user pinned in packaging/vendor.toml,"
            )
            raise Error(
                f"userland qemu-user needs qemu-arm with the compat bridge: QEMU_ARM=PATH, or "
                f"{where} {tree.build(vendored, 'bin', 'qemu-arm')}"
            )
        p.qemu_aarch64 = tree.qemu_aarch64(env, guest_kernel)
        if guest_kernel == "x86-64":
            say(
                "userland: 64-bit ARM programs under qemu-aarch64"
                if p.qemu_aarch64
                else "userland: no qemu-aarch64 (QEMU_AARCH64=PATH): 64-bit ARM programs do not run"
            )
    # default: the headless layer on webOS; none on SimpleSmart, which has no webOS layers
    layers = env.get(
        "LAYERS",
        "headless" if targets.release(p.target).always_layers == targets.WEBOS_LAYERS else "",
    )
    try:
        tv_mem = int(env.get("MEM") or nvm.board_ram(p.target, tree, p.broadcast))
    except (targets.Error, OSError, ValueError) as e:
        raise Error(str(e)) from None
    # -m: the TV's RAM, and qemu-user's own on top of it (vmsettings.guest_ram_mib). What webOS is
    # told the set has comes from the board's hwopt= (board.cmdline), not from -m.
    mem = str(vmsettings.guest_ram_mib(tv_mem, p.userland))
    note = vmsettings.memory_note(tv_mem, p.userland)
    if note:
        say(note)
    smp = _get(env, "SMP", "4")
    kernel, initrd = tree.guest_kernel(guest_kernel)
    for f in (kernel, initrd, os.path.join(fw, targets.root_pak(p.target) + ".pak")):
        if not os.path.isfile(f):
            raise Error(f"missing {f} (run scripts/build.sh guest)")

    # paks (targets.py: the target's), each attached read-only; the initramfs finds it by its serial
    # (boot.conf)
    rootfs = _get(env, "ROOTFS", "stock")
    if rootfs == "stock":
        uncompressed = []
    elif rootfs == "uncompressed":
        uncompressed = _get(env, "UNCOMPRESSED_PAKS", "rootfs").split()
    else:
        raise Error(f"unknown ROOTFS={rootfs} (stock or uncompressed)")
    cache = env.get("FW_CACHE") or fwcache.default_cache(tree)
    devices = []
    # A pak the firmware mounts itself from its /dev/mmcblk0pN (mount None, not a pak lower:
    # SimpleSmart's lginit_app) is a read-write disk whose writes QEMU discards (snapshot=on), as
    # the set's eMMC partition is writable: its mount(2) passes no MS_RDONLY, which a read-only
    # disk refuses. The others are read-only.
    lowers = {pl.serial for pl in targets.release(p.target).pak_lowers}
    for file, serial, mount, _optional in targets.paks(p.target, fw):
        img = os.path.join(fw, file + ".pak")
        if not os.path.isfile(img):
            continue
        if file in uncompressed:
            try:
                img = (
                    fwcache.image_path(fw, cache, file, record=False)
                    if dry
                    else fwcache.build(fw, cache, file, tree)
                )
            except (fwcache.Error, subprocess.CalledProcessError, OSError) as e:
                raise Error(str(e)) from None
            say(f"pak {file}: uncompressed copy {img}")
        elif mount and lz4p.is_lz4p(img):
            # LG's LZ4P-wrapped XIP cramfs (NetCast 4's lgapp), which init mounts: its SquashFS
            # image, made once per firmware with epk2extract and mksquashfs (lz4p.py)
            try:
                img = (
                    lz4p.image_path(fw, cache, file, record=False)
                    if dry
                    else lz4p.build(fw, cache, file)
                )
            except (lz4p.Error, fwcache.Error, subprocess.CalledProcessError, OSError) as e:
                raise Error(f"pak {file}: {e}") from None
            say(f"pak {file}: SquashFS image {img}")
        access = "snapshot=on" if mount is None and serial not in lowers else "readonly=on"
        devices += [
            "-drive",
            f"if=none,id={serial},format=raw,{access},file={img}",
            "-device",
            f"virtio-blk-pci,drive={serial},serial={serial}",
        ]

    # the guest layers (share.resolve_layers), each a read-only squashfs image (layerimage.py,
    # built once into the cache; dry: only its path) that init mounts in place by its serial
    # layer-<name> (docs/dev/architecture/build-and-boot.md, "Layer images")
    try:
        p.guest_layers, missing = share.resolve_layers(tree, p.target, layers.split(","))
        for layer in p.guest_layers:
            img = layerimage.image(tree, layer.src, layer.skip, make=not dry, say=say)
            serial = layerimage.serial(layer.name)
            devices += [
                "-drive",
                f"if=none,id={serial},format=raw,readonly=on,file={img}",
                "-device",
                f"virtio-blk-pci,drive={serial},serial={serial}",
            ]
    except (share.Error, layerimage.Error) as e:
        raise Error(str(e)) from None
    say("guest layers: " + ", ".join(f"{la.name} from {la.rel}" for la in p.guest_layers))
    if missing:
        say("guest layers not on the host, left out: " + ", ".join(missing))

    # state disks (targets.py: the target's read-write partitions, tvservice's NVM and the eMMC
    # partitions of emmc_disks), one sparse raw image each
    # persistent state uses the managed VM's state directory; blank disks use RUN_DIR
    run_dir = os.path.join(top, _get(env, "RUN_DIR", "build"))
    state = _get(env, "STATE", os.path.join(run_dir, "state", "default"))
    state_opt = ""
    fresh = state == "none" and env.get("FRESH_STATE")
    if fresh:
        state = fresh  # the run's temporary state (unit.run): kept across guest restarts
    elif state == "none":
        state, state_opt = os.path.join(run_dir, "state", ".blank"), ",snapshot=on"
    elif not state.startswith("/"):
        state = os.path.join(top, state)
    p.state_dir = state
    # the eMMC partition map (partmap.py): the firmware's, built into the
    # cache; the disks with no filesystem of ours that back its partitions (Release.emmc_disks) take
    # their sizes from it, and every disk that backs a partition must hold it
    # The map is the VM's prepared board data's (prepare.py: worked out at create, made again
    # here, said, if it is missing or stale).
    sizes = {}
    if not dry:
        try:
            p.prepared = prepare.ensure(prepare.Job(env, tree), say)
            p.partmap = p.prepared.partmap
            if p.partmap:
                sizes = partmap.disk_sizes(partmap.read(p.partmap), p.target)
        except (prepare.Error, nvm.Error) as e:
            raise Error(str(e)) from None
        except (partmap.Error, OSError) as e:
            raise Error(f"partition map: {e}") from None
    # firmware.updatable: the whole-disk /dev/mmcblk0 the stock updater writes into. partinfo is
    # attached snapshot=on so the updater's bank-swap write to it is discarded with the rest, when
    # this QEMU exits: a guest restart included, in a fresh state's run (FRESH_STATE) as in a
    # persistent one. A set keeps the swap across its reboot, but here the bank it points at is the
    # scratch overlay's, which the same exit discards, and the paks stay the firmware's files: a
    # swap kept alone would name partitions no disk holds.
    p.updatable = _get(env, "UPDATABLE", "off")
    if p.updatable not in ("on", "off"):
        raise Error(f"UPDATABLE={p.updatable}: on or off")
    p.updatable = p.updatable == "on"
    if p.updatable and not targets.board_data(p.target):
        raise Error("firmware.updatable: this firmware has no eMMC partition map to present")
    # firmware.updatable: serve the inactive A/B bank widened for the stock updater's pak size check
    # (partmap.UPDATABLE_HEADROOM) from a separate cache image; a non-updatable VM keeps the prepared
    # partinfo byte-for-byte. The whole-disk overlay, its dm-linear table and the overlay-backed pN
    # nodes below all read this same map (partmap.read(p.partmap)), so they stay self-consistent.
    if p.updatable and not dry and p.partmap:
        try:
            p.partmap = partmap.build(fw, p.target, cache, tree, updatable=True)
            sizes = partmap.disk_sizes(partmap.read(p.partmap), p.target)
        except (partmap.Error, OSError) as e:
            raise Error(f"firmware.updatable: {e}") from None
    disks = [(name, size_bytes(size), None) for name, size in targets.state_disks(p.target)]
    for serial in targets.emmc_disks(p.target):
        disks.append((serial, sizes.get(serial), p.partmap if serial == "partinfo" else None))
    for name, size, seed in disks:
        img = os.path.join(state, name + ".img")
        opt = state_opt
        if p.updatable and name == "partinfo" and ",snapshot=on" not in opt:
            opt += ",snapshot=on"
        devices += [
            "-drive",
            f"if=none,id={name},format=raw,file={img},discard=unmap,detect-zeroes=unmap{opt}",
            "-device",
            f"virtio-blk-pci,drive={name},serial={name}",
        ]
        if dry and size is None:  # an eMMC disk with no map: its -drive only
            continue
        if size is None:
            raise Error(f"partition map: no partition for the disk {name}")
        need = sizes.get(name, 0)
        if size < need:
            raise Error(f"{name}: {size} bytes, smaller than its partition's {need}")
        if need and not state_opt and os.path.isfile(img) and os.path.getsize(img) < need:
            raise Error(
                f"{img}: {os.path.getsize(img)} bytes, smaller than its partition's {need} "
                f"(grow it: truncate -s {need} {img})"
            )
        # a blank image is re-created if it is not blank (all zero reads as sparse: no blocks) or
        # not its size, a seeded one each time
        if (
            state_opt
            and os.path.isfile(img)
            and (seed or os.stat(img).st_blocks != 0 or os.path.getsize(img) != size)
        ):
            p.remove.append(img)
            p.disks.append((img, size, seed))
        elif not os.path.isfile(img):
            p.disks.append((img, size, seed))
    if fresh:
        say(
            f"state: none (this run's fresh state in {state}: kept across guest restarts, "
            "discarded when the run ends)"
        )
    else:
        say("state: none (fresh disks, discarded at power-off)" if state_opt else f"state: {state}")
    # firmware.updatable: one scratch overlay disk sized to the whole eMMC, the dm-linear whole-disk
    # /dev/mmcblk0's backing (guest bin/beanjar-emmc). snapshot=on discards every write at power-off,
    # so the updater's raw writes to the inactive A/B bank never persist and never reach a real
    # backing disk; the active partitions keep their own pN nodes and are not stacked under dm.
    if p.updatable:
        p.emmc_overlay = "emmc-overlay"
        img = os.path.join(state, p.emmc_overlay + ".img")
        devices += [
            "-drive",
            f"if=none,id={p.emmc_overlay},format=raw,snapshot=on,file={img},"
            "discard=unmap,detect-zeroes=unmap",
            "-device",
            f"virtio-blk-pci,drive={p.emmc_overlay},serial={p.emmc_overlay}",
        ]
        if not dry:
            try:
                m = partmap.read(p.partmap)
                dev_size = partmap.device_size(m)
                p.emmc_table = partmap.emmc_table_words(m, p.target, p.emmc_overlay)
                p.emmc_part_nodes = partmap.emmc_part_words(m, p.target)
            except (partmap.Error, OSError) as e:
                raise Error(f"firmware.updatable: {e}") from None
            # a sparse zero base of the whole-disk size; QEMU's snapshot layer never writes it, so it
            # stays all-zero and is shared across starts (re-made only if its size changed)
            if not os.path.isfile(img) or os.path.getsize(img) != dev_size:
                if os.path.isfile(img):
                    p.remove.append(img)
                p.disks.append((img, dev_size, None))
            say(
                f"updatable: whole-disk /dev/mmcblk0 over {len(p.emmc_table)} segments"
                + (
                    f", {len(p.emmc_part_nodes)} overlay-backed pN nodes"
                    if p.emmc_part_nodes
                    else ""
                )
                + ", writes discarded"
            )
    # qemu-user's cold overhead: a swap disk on a memfd in host RAM, which the launcher makes when
    # QEMU starts (hostswap.py); a native VM has none
    swap = vmsettings.qemu_user_swap_mib(tv_mem, p.userland)
    if swap:
        devices += hostswap.argv(swap)

    # network: net is what the guest gets, NET_LINK and NET_MODE together (mitm, direct or off)
    if env.get("NET"):
        raise Error(
            "NET is replaced by NET_LINK (nat, lan, off) and NET_MODE (mitm, direct): docs/dev/architecture/network.md"
        )
    link = _get(env, "NET_LINK", "nat")
    if link not in ("nat", "lan", "off"):
        raise Error(f"unknown NET_LINK={link} (nat, lan or off)")
    net = _get(env, "NET_MODE", "mitm")
    if net not in ("mitm", "direct"):
        raise Error(f"unknown NET_MODE={net} (mitm or direct)")
    if link == "off":
        net = "off"
    # the LAN and the host's own addresses: link=lan is on the LAN; link=nat only with
    # NET_LAN_ACCESS=on. The host's loopback is out of reach either way (netns.py).
    lan_access = _get(env, "NET_LAN_ACCESS", "off")
    if lan_access not in vmsettings.ONOFF:
        raise Error(f"NET_LAN_ACCESS={lan_access}: on or off")
    lg_services = _get(env, "NET_LG_SERVICES", vmsettings.DEFAULT_LG_SERVICES)
    if lg_services not in vmsettings.LG_SERVICES:
        raise Error(f"NET_LG_SERVICES={lg_services}: {' or '.join(vmsettings.LG_SERVICES)}")
    lan_direct = vmsettings.lan_direct(link, net)
    if link == "lan":
        lan_access = "on"
        # mitm: the mitm namespace one level in, its uplink a macvlan on the library's lan_parent;
        # direct: the guest's TAP joined to that macvlan, nothing in between (netns.lan)
        if lan_direct and lg_services != "real":
            raise Error(vmsettings.LAN_DIRECT_LG)
        parent = env.get("NET_LAN_PARENT")
        if not parent:
            raise Error(
                "NET_LINK=lan needs NET_LAN_PARENT, the library's lan_parent (beanjar library config set lan_parent=eth0)"
            )
        if env.get("LISTEN_ADDRESS"):
            raise Error(
                "LISTEN_ADDRESS is for NET_LINK=nat: a link=lan VM's ports are on its LAN address"
            )
        if not dry and os.path.exists(f"/sys/class/net/{parent}/wireless"):
            raise Error(f"NET_LAN_PARENT={parent} is wireless; link=lan needs a wired interface")
        if lan_direct and env.get("NET_ADDRESS"):
            raise Error(
                "NET_ADDRESS is for NET_LINK=lan with NET_MODE=mitm: with direct the guest takes its own address"
            )
        if lan_direct and env.get("FORWARD"):
            raise Error(
                "FORWARD is for NET_MODE=mitm with NET_LINK=lan: with direct every guest port is on its own LAN address"
            )
        address = env.get("NET_ADDRESS") or "dhcp"
        try:
            address = keys.lan_address(address)
        except keys.Error as e:
            raise Error(f"NET_ADDRESS: {e}") from None
        helper = tree.lan_helper()
        if not dry and not os.access(helper, os.X_OK):
            raise Error(f"link=lan needs {helper} (scripts/build.sh host)")
        p.env.update(NET_LINK="lan", NET_LAN_PARENT=parent, NET_LAN_HELPER=helper)
        if not lan_direct:
            p.env["NET_ADDRESS"] = address
    fwd = []  # hostport:guestport
    if net in ("mitm", "direct"):
        # both in netns.py's rootless namespace, slirp4netns its uplink with the host's loopback
        # refused (QEMU's own slirp, -netdev user, maps 10.0.2.2 to it); direct without mitmweb
        p.wrap = "netns"
        if not lan_direct and not dry and not os.access(tree.relay(), os.X_OK):
            raise Error(f"NET_LINK={link} needs {tree.relay()} (scripts/build.sh host)")
        p.env["NET_MODE"] = net
        p.env["NET_LAN_ACCESS"] = lan_access
        p.env["NET_LG_SERVICES"] = lg_services
        p.env["NETNS_TAP"] = _get(env, "NETNS_TAP", "tap0")
        if net == "mitm":
            flows = _get(env, "MITM_FLOWS", str(vmsettings.MITM_FLOWS))
            if not flows.isdigit():
                raise Error(f"MITM_FLOWS={flows}: a number, 0 for no limit")
            p.env["MITM_FLOWS"] = str(int(flows))
            try:
                p.env["MITM_FLOW_BYTES"] = str(vmsettings.byte_size(env.get("MITM_FLOW_BYTES", "")))
            except ValueError as e:
                raise Error(f"MITM_FLOW_BYTES={e}") from None
            p.env["NETSTUB_LOG"] = _get(
                env, "NETSTUB_LOG", os.path.join(top, _get(env, "LOG_DIR", "build"), "netstub.log")
            )
        netdev = f"tap,id=net0,ifname={p.env['NETNS_TAP']},script=no,downscript=no"
    else:
        p.wrap = "off"
        netdev = "hubport,id=net0,hubid=0"
    append_net = f" webos.net={net}"
    # the VM's host address: every host port of it is there, at the TV's stock numbers (keys.py)
    if not env.get("SLOT"):
        raise Error("SLOT unset: the VM has no slot, its host address (vm.toml's network.slot)")
    try:
        prefix = (
            keys.parse_addresses(env["ADDRESS_PREFIX"], "ADDRESS_PREFIX")
            if env.get("ADDRESS_PREFIX")
            else None
        )
    except keys.Error as e:
        raise Error(str(e)) from None
    try:
        p.address = keys.address(env["SLOT"], prefix)
    except keys.Error as e:
        raise Error(f"SLOT: {e}") from None
    p.env["VM_ADDRESS"] = p.address
    # the TV-service ports (layermeta.forwards: SSAP, root SSH, developer mode and its key server)
    # listen on LISTEN_ADDRESS (vm.toml's listen; 0.0.0.0 opens them to the LAN), by default the VM's
    # address too; this host reaches them at p.service_host (keys.service_host)
    try:
        listen = (
            keys.listen_address(env["LISTEN_ADDRESS"]) if env.get("LISTEN_ADDRESS") else p.address
        )
    except keys.Error as e:
        raise Error(f"LISTEN_ADDRESS: {e}") from None
    p.env["LISTEN_ADDRESS"] = listen
    p.service_host = keys.service_host(listen, p.address)
    addr = p.service_host

    def forward(host, guest):
        if lan_direct:
            pass  # the guest is on the LAN itself: guest port `guest` is on its own LAN address
        elif net in ("mitm", "direct"):
            fwd.append(f"{host}:{guest}")
        else:
            say(
                f"NET_LINK=off: no forward for host port {listen}:{host} (use NET_LINK=nat to reach the guest)"
            )

    # The VM's files: the identity directory (IDENTITY_DIR, a VM's
    # <vm>/identity; the development VM's identity/), from which guest_layer() assembles the per-VM layer
    # RUN_DIR/share/vm that the guest reads over the read-only share "vm" and stacks on top:
    # the SSH keys of rootssh and devmode (beanjar/keys.py), plus sestore.toml items
    # compiled into the kernel-store snapshot, whatever the network is.
    p.identity_dir = os.path.join(top, env["IDENTITY_DIR"])
    p.tree = tree
    p.run_dir = run_dir
    _share, p.layer_dir, p.host_dir, p.out_dir = share.dirs(p.run_dir)
    # the layers by name (a versioned layer's NAME@VERSION is NAME here; share.resolve_layers
    # took its version's component)
    p.layers = layermeta.bases(layers.split(","))
    ident = lambda name: os.path.join(p.identity_dir, name)  # noqa: E731
    known = f"-o UserKnownHostsFile={ident(keys.KNOWN_HOSTS)} -o StrictHostKeyChecking=yes"
    # The layers' own rules and forwards (guest/layers/<name>/layer.toml, layermeta.py): the TV-service
    # ports (layermeta.forwards, the one list of them: SSAP from the webos layer, root SSH, developer
    # mode), each guest port from its host port on LISTEN_ADDRESS (SSH_PORT for root SSH's 6622).
    # Telnet, which Homebrew Channel also runs (23, unauthenticated root), stays inside the guest.
    try:
        meta = layermeta.load()
        layermeta.check(layers.split(","), meta)
    except layermeta.Error as e:
        raise Error(f"LAYERS: {e}") from None
    notes = {
        "rootssh": lambda: (
            f"root ssh: ssh -p {p.ssh_port} -i {ident(keys.CLIENT)} {known} root@{addr}"
        ),
        "hbchannel": lambda: (
            f"root ssh (Homebrew Channel): ssh -p {p.ssh_port} -i {ident(keys.CLIENT)} {known} root@{addr}"
        ),
        "devmode": lambda: (
            f"developer mode: ssh -p {keys.DEVMODE_PORT} -i {ident(keys.DEVMODE)} {known} prisoner@{addr}, "
            f"key server http://{addr}:{keys.KEYSERVER_PORT}/webos_rsa (encrypted with the guest's passphrase)"
        ),
        # the stock xinetd takes 3000 from localhost only on most releases (W21P has no only_from)
        "webos": lambda: (
            f"SSAP (second screen): wss://{addr}:3001 (ws://{addr}:3000 where the firmware lets it in)"
        ),
    }
    ssh_port = _get(env, "SSH_PORT", keys.ROOTSSH_PORT)
    try:
        extra = layermeta.parse_forward(env.get("FORWARD", ""))
        fwds = layermeta.forwards(
            p.layers + list(targets.always_layers(p.target)), meta, ssh_port, extra
        )
    except layermeta.Error as e:
        raise Error(f"FORWARD: {e}") from None
    if extra:
        notes[layermeta.FORWARD] = lambda: (
            "forward: "
            + ", ".join(f"{addr}:{h}" + ("" if h == g else f" (guest {g})") for h, g in extra)
        )
    stacked = {la.name for la in p.guest_layers}
    for i, (host, guest, name) in enumerate(fwds):
        if (
            (i == 0 or fwds[i - 1][2] != name)
            and name in p.layers
            and name not in stacked
            and not os.path.isdir(tree.layer(p.target, name))
        ):
            raise Error(
                f"LAYERS has {name} but {tree.layer(p.target, name)} is missing: run scripts/build.sh guest"
            )
        if guest == 22:
            p.ssh_port = ssh_port
        forward(host, guest)
        if (
            (i + 1 == len(fwds) or fwds[i + 1][2] != name) and name in notes and not lan_direct
        ):  # a layer's last; with link=lan and direct nothing is on this host's address
            say(notes[name]())
    p.env["NETNS_FWD"] = "".join(" " + f for f in fwd)
    if net != "off":
        say(
            f"network: {link}, {net}, host address {p.address}"
            + (", LAN access" if lan_access == "on" else "")
            + (", real LG services" if lg_services == "real" else "")
            + (f", service ports on {listen}" if listen != p.address else "")
            + (
                f", LAN on {p.env['NET_LAN_PARENT']} ({p.env['NET_ADDRESS']})"
                if link == "lan" and not lan_direct
                else ""
            )
            + (
                f", the guest itself on the LAN of {p.env['NET_LAN_PARENT']} (its own lease and "
                "ports; this host cannot reach it)"
                if lan_direct
                else ""
            )
        )
    # The proxy's CA (keys.ensure_ca): the library's for its VMs, the development library's ca/. Only
    # its certificate reaches the guest (guest_layer). Upstream servers are verified against
    # mitmproxy's default roots, nothing of the firmware's (netns.py). With LG's services mocked
    # the guest trusts it in direct too: lg_mock.py answers LG's SDP and web hosts with
    # certificates it issues (netns.py).
    p.ca_dir = os.path.join(top, _get(env, "NETSTUB_CA", os.path.join(p.identity_dir, keys.CA)))
    if net == "mitm" or (net == "direct" and lg_services == "mock"):
        p.env["NETSTUB_CA"] = p.ca_dir
        p.ca = os.path.join(p.ca_dir, "ca.pem")
        say(f"proxy CA: {p.ca_dir}")
    from . import sestore

    if targets.platform(p.target) == "webOS":
        try:
            p.sestore = sestore.load(p.identity_dir)
        except sestore.Error as error:
            raise Error(str(error)) from None
    # The TV's identity: MAC and SERIAL, a VM's own (vm.toml's mac and
    # serial), replace board.cmdline's ethaddr= and serialNum= in the guest (guest/initramfs/rootfs/init); unset,
    # the checkout's board.cmdline values stand, the same for every checkout VM.
    mac = env.get("MAC")
    if mac:
        mac = mac.lower()
        if not re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac) or int(mac[:2], 16) & 1:
            raise Error(f"MAC={env['MAC']}: six hex octets with colons, unicast")
        append_net += f" webos.ethaddr={mac}"
    else:
        mac = guest_mac(p.target, tree, p.broadcast)
    if env.get("SERIAL"):
        if not re.fullmatch(r"[0-9A-Z]{1,16}", env["SERIAL"]):
            raise Error(f"SERIAL={env['SERIAL']}: 1 to 16 upper-case letters and digits")
        append_net += f" webos.serial={env['SERIAL']}"
    try:
        debug_mode = nvm.debug_mode_number(env.get("DEBUG_MODE"))
    except nvm.Error as e:
        raise Error(str(e)) from None
    if debug_mode is not None:
        append_net += f" webos.debugmode={debug_mode}"
    devices += ["-device", "virtio-net-pci,netdev=net0" + (f",mac={mac}" if mac else "")]
    if link == "lan":
        if not mac:
            raise Error("NET_LINK=lan needs the VM's MAC (MAC, or the board's ethaddr)")
        p.env["NET_LAN_MAC"] = mac  # the macvlan's: the VM is this MAC on the LAN
    if env.get("NET_DUMP"):
        devices += ["-object", f"filter-dump,id=netdump,netdev=net0,file={env['NET_DUMP']}"]

    # the guest's shares (share.py: its runtime tree only), input, the virtio serial ports, the
    # status bar's and the control QMP
    socks = {
        k: _get(env, k, os.path.join(p.run_dir, v))
        for k, v in (
            ("CURSOR_SOCK", "cursor.sock"),
            ("VIDEO_SOCK", "video.sock"),
            ("IR_SOCK", "ir.sock"),
            ("MEDIA_SOCK", "media"),
            ("CAPTURE_SOCK", "capture.sock"),
            ("VIEWER_QMP_SOCK", "viewer-qmp.sock"),
            ("QMP_SOCK", "qmp.sock"),
            ("QEMU_PIDFILE", "qemu.pid"),
        )
    }
    socks["WEBOS_LOG"] = _get(
        env, "WEBOS_LOG", os.path.join(top, _get(env, "LOG_DIR", "build"), "webos-console.log")
    )
    say(f"webOS console (hvc0) -> {socks['WEBOS_LOG']}")
    devices += [
        "-fsdev",
        f"local,id=host,path={p.host_dir},security_model=none,readonly=on",
        "-device",
        "virtio-9p-pci,fsdev=host,mount_tag=host",
        "-fsdev",
        f"local,id=vm,path={p.layer_dir},security_model=none,readonly=on",
        "-device",
        "virtio-9p-pci,fsdev=vm,mount_tag=vm",
        "-fsdev",
        f"local,id=out,path={p.out_dir},security_model=none",
        "-device",
        "virtio-9p-pci,fsdev=out,mount_tag=out",
        "-device",
        "virtio-rng-pci",
        "-device",
        "virtio-keyboard-pci",
        *input_devices(targets.soc(p.target).touch),
        "-chardev",
        f"file,id=webos,path={socks['WEBOS_LOG']}",
        "-device",
        "virtio-serial-pci",
        "-device",
        "virtconsole,chardev=webos",
        # the TV's USB host (its ports empty): /sys/bus/usb, which webOS 5 and 6's native jails
        # mount (jail_native_builtin.conf's `mount ro /sys/bus/usb`)
        "-device",
        "qemu-xhci",
    ]
    fpr = env.get("FREE_PAGE_REPORTING", "off")
    if fpr not in ("on", "off"):
        raise Error(f"FREE_PAGE_REPORTING={fpr}: on or off")
    # a balloon for the guest's memory statistics (the viewer's memory meter: QMP qom-get of
    # /machine/peripheral/balloon's guest-stats), polled every BALLOON_STATS_S; never inflated, so
    # the guest keeps all of MEM. FREE_PAGE_REPORTING=on adds free page reporting to it.
    balloon = f"virtio-balloon-pci,id=balloon,guest-stats-polling-interval={BALLOON_STATS_S}"
    devices += ["-device", balloon + (",free-page-reporting=on" if fpr == "on" else "")]
    # with the media hub (host/helpers/media-helper), QEMU's video and media sockets are the hub's alone and
    # it serves the viewers at the usual names; the viewers' sockets are QEMU's without it
    hub = media_helper(top, env)
    guest_video = qemu_side(socks["VIDEO_SOCK"]) if hub else socks["VIDEO_SOCK"]
    guest_media = qemu_side(socks["MEDIA_SOCK"]) if hub else socks["MEDIA_SOCK"]
    if hub:
        p.hub_argv = [
            hub,
            "--pid",
            "PID",
            "--log",
            os.path.join(p.run_dir, "media-sessions.log"),
            "--decode-log",
            os.path.join(p.run_dir, "media-helper.log"),
            # the sessions open now, for status show's audio (soundout.py)
            "--streams",
            os.path.join(p.run_dir, MEDIA_STREAMS),
            "--video",
            f"{guest_video}:{socks['VIDEO_SOCK']}",
            "--media",
            f"{guest_media}:{socks['MEDIA_SOCK']}",
            "--ports",
            str(MEDIA_PORTS),
            "--hwdec",
            env.get("MEDIA_HWDEC", "auto"),
        ]
    for port, path in (
        ("cursor", socks["CURSOR_SOCK"]),
        ("video", guest_video),
        ("ir", ir_relay.qemu_path(socks["IR_SOCK"])),
    ):
        devices += [
            "-chardev",
            f"socket,id={port},path={path},server=on,wait=off",
            "-device",
            f"virtserialport,chardev={port},name=webos.{port}",
        ]
    # the remote's keys reach the port through the micom's IR receiver on ir.sock (ir_relay.py)
    p.ir_relay = (socks["IR_SOCK"], ir_relay.qemu_path(socks["IR_SOCK"]), socks["QMP_SOCK"])
    # `beanjar shell` (guestshell.py): the relay, started by the launcher on RUN_DIR's shell.sock,
    # holds QEMU's socket for the port; the guest's agent (virtio-shell.c) serves it
    shell_qemu = os.path.join(p.run_dir, guestshell.QEMU_NAME)
    devices += [
        "-chardev",
        f"socket,id=shell,path={shell_qemu},server=on,wait=off",
        "-device",
        "virtserialport,chardev=shell,name=webos.shell",
    ]
    p.shell_relay = (os.path.join(p.run_dir, guestshell.SOCK), shell_qemu)
    # the antenna of a board with our tuner (dtv_soc): the host's tuner (tuner.py, started by the
    # launcher) connects to RUN_DIR's tuner.sock, the guest's pump (virtio-tuner.c) to the port
    if tuner.has_tuner(p.target):
        devices += [
            "-chardev",
            f"socket,id=tuner,path={os.path.join(p.run_dir, tuner.SOCK)},server=on,wait=off",
            "-device",
            "virtserialport,chardev=tuner,name=webos.tuner",
        ]
    # the vendor module's video60 (targets.v4l2_capture) and the other capture models
    # (targets.viewer_capture) read the composed TV frame on demand over webos.capture; the guest's
    # init starts its bridge (virtio-capture) where it is. The front ends answer on capture.sock
    # through the capture relay (capture_relay.py, started by the launcher), which holds QEMU's
    # socket and runs a headless viewer on demand when no front end is there
    if targets.viewer_capture(p.target):
        qemu_capture = capture_relay.qemu_path(socks["CAPTURE_SOCK"])
        devices += [
            "-chardev",
            f"socket,id=capture,path={qemu_capture},server=on,wait=off",
            "-device",
            "virtserialport,chardev=capture,name=webos.capture",
        ]
        p.capture_relay = (socks["CAPTURE_SOCK"], qemu_capture, None)
    # the micom's standby (Quick Start+): the guest's IR pump holds webos.standby open while it
    # lasts, the host reads it as the chardev's frontend-open in QMP query-chardev
    devices += [
        "-chardev",
        "null,id=standby",
        "-device",
        "virtserialport,chardev=standby,name=webos.standby",
    ]
    # the micom's state, a line at each change (the standby, its power state, the front LED's
    # latches: include/webos-ir.h), which the IR pump writes and the viewer's front LED reads
    # beside qemu.pid; QEMU truncates it at each start
    devices += [
        "-chardev",
        f"file,id=micom,path={os.path.join(os.path.dirname(socks['QEMU_PIDFILE']), 'micom-status')}",
        "-device",
        "virtserialport,chardev=micom,name=webos.micom",
    ]
    # the master volume and speaker mute the firmware sets (the gain lgemu.ko's snd.c gives the
    # guest's sound card), a line at each change, which the guest's sound pump writes
    # (guest/initramfs/src/virtio-sound.c) and the viewer core applies to the media audio it plays
    # (host/viewer/core/sound-status.c); beside qemu.pid, QEMU truncates it at each start
    devices += [
        "-chardev",
        f"file,id=sound,path={os.path.join(os.path.dirname(socks['QEMU_PIDFILE']), 'sound-status')}",
        "-device",
        "virtserialport,chardev=sound,name=webos.sound",
    ]
    # a model that turns to portrait (targets.Soc.portrait): the guest's screen rotation, a line at
    # each change of msc's orientation (guest/initramfs/rootfs/bin/beanjar-rotation), which `beanjar
    # rotate` waits for and the viewers read beside qemu.pid (rotation.py); QEMU truncates it at
    # each start
    if targets.soc(p.target).portrait:
        devices += [
            "-chardev",
            f"file,id=rotation,path={os.path.join(os.path.dirname(socks['QEMU_PIDFILE']), 'rotation')}",
            "-device",
            "virtserialport,chardev=rotation,name=webos.rotation",
        ]
    # the guest's swap, a line every 2 s from the initramfs's guest/initramfs/src/virtio-meminfo.c,
    # kept in a ring that the viewer's memory meter drains with QMP ringbuf-read
    devices += [
        "-chardev",
        "ringbuf,id=memory,size=4096",
        "-device",
        "virtserialport,chardev=memory,name=webos.memory",
    ]
    for n in range(MEDIA_PORTS):
        devices += [
            "-chardev",
            f"socket,id=media{n},path={guest_media}-{n}.sock,server=on,wait=off",
            "-device",
            f"virtserialport,chardev=media{n},name=webos.media.{n}",
        ]
    # the media pool (include/webos-media.h, "The pool"): pictures the host decodes for the guest go
    # here instead of through the ports. QEMU's stock ivshmem-doorbell (one MSI-X vector), whose
    # ivshmem server is the media helper: it creates the pool's file in the unit's RuntimeDirectory
    # (a tmpfs that systemd removes with the unit, after a crash too; sparse, and the helper punches
    # a session's pages out when it ends) and hands it to QEMU. The launcher listens on the socket
    # before either starts. Without a unit (the setsid fallback) the pictures come inline.
    if hub and env.get("RUNTIME_DIRECTORY"):
        pool = os.path.join(env["RUNTIME_DIRECTORY"], MEDIA_POOL)
        p.pool_socket = os.path.join(env["RUNTIME_DIRECTORY"], MEDIA_POOL_SOCK)
        devices += [
            "-chardev",
            f"socket,id=mediapool,path={p.pool_socket}",
            "-device",
            "ivshmem-doorbell,chardev=mediapool,vectors=1,id=mediapool",
        ]
        p.hub_argv += ["--pool", pool]
    devices += [
        "-name",
        "webos,debug-threads=on",
        "-pidfile",
        socks["QEMU_PIDFILE"],
        "-qmp",
        f"unix:{socks['VIEWER_QMP_SOCK']},server=on,wait=off",
        "-qmp",
        f"unix:{socks['QMP_SOCK']},server=on,wait=off",
    ]
    if env.get("LIFECYCLE_SOCK"):
        # the unit's own monitor (lifecycle.py): a guest restart ends QEMU, and unit.run starts the
        # VM again cold; a write that hits ENOSPC on the host makes it quit instead of pausing
        devices += [
            "-action",
            "reboot=shutdown",
            "-chardev",
            f"socket,id=lifecycle,path={env['LIFECYCLE_SOCK']}",
            "-mon",
            "chardev=lifecycle,mode=control",
        ]

    if env.get("CONSOLE_SOCK"):
        console = [
            "-monitor",
            "none",
            "-serial",
            "chardev:con",
            "-chardev",
            f"socket,id=con,path={env['CONSOLE_SOCK']},server=on,wait=off,logfile={_get(env, 'CONSOLE_LOG', '/dev/null')}",
        ]
    else:
        console = [
            "-chardev",
            "stdio,id=con,mux=on,signal=off",
            "-serial",
            "chardev:con",
            "-mon",
            "chardev=con",
        ]

    # Kernel cmdline. virtio_snd.pcm_device_base=6 (guest/kernel/patches) makes the virtio-sound PCM
    # hw:0,6, the device the stock /etc/pulse/system.pa opens for pcm_output; pcm_alias_device=0
    # adds hw:0,0 on the same stream, the device alsa-lib's hints test for an output (Chromium's
    # ALSA manager plays nothing without one).
    # the serial console: virt's PL011, q35's 8250
    append = (
        f"console={'ttyS0' if guest_kernel == 'x86-64' else 'ttyAMA0'} rdinit=/init loglevel=3 mem_sleep_default=s2idle virtio_snd.pcm_device_base=6 "
        f"virtio_snd.pcm_alias_device=0{append_net}"
    )
    if time_scale(accel) != 1:
        append += f" webos.time_scale={time_scale(accel)}"
    if env.get("PMLOG"):
        append += f" webos.pmlog={env['PMLOG']}"
    if env.get("PMLOG_DEBUG"):
        append += " webos.pmlog_debug=" + re.sub(" +", ",", env["PMLOG_DEBUG"])
    # then the words the guest layers carry for their userland (share.kernel_args: the soc layer's
    # Qt EGLFS Starfish setup's virtio_gpu.modifiers=1), the caller's and KERNEL_ARGS
    try:
        layer_words = share.kernel_args(p.guest_layers)
    except share.Error as e:
        raise Error(str(e)) from None
    append += " " + " ".join(layer_words + list(words) + env.get("KERNEL_ARGS", "").split())
    # a release whose stock userland reads the kernel's own /proc/cmdline (Release.board_in_kernel:
    # SimpleSmart): the board's parameters first on it, as its bootloader puts them, with the VM's
    # MAC (macadr=, ethaddr=) and serial; then ours. (Its readers take the first 512 bytes.)
    if targets.release(p.target).board_in_kernel:
        try:
            board = nvm.board_words(p.target, p.tree or paths.TREE, p.broadcast, debug_mode)
        except OSError as e:
            raise Error(f"board.cmdline: {e}") from None
        subst = {
            "macadr": env.get("MAC", "").lower(),
            "ethaddr": env.get("MAC", "").lower(),
            "serialNum": env.get("SERIAL", ""),
        }
        board = [
            w.split("=", 1)[0] + "=" + subst[w.split("=", 1)[0]]
            if "=" in w and subst.get(w.split("=", 1)[0])
            else w
            for w in board
        ]
        append = " ".join(board) + " " + append

    try:
        gpu_res = env.get("GPU_RES") or nvm.board_graphic(
            p.target, p.tree or paths.TREE, p.broadcast
        )
    except (targets.Error, OSError) as e:
        raise Error(str(e)) from None
    # edid=off: no EDID (QEMU's is 75 Hz), so the driver's preferred mode is its own CVT mode at
    # 60 Hz, the rate of the UI's vblank (kernel patch 0022; docs/dev/architecture/build-and-boot.md,
    # "The display's refresh")
    gpu = [
        "-device",
        f"virtio-gpu-gl-pci,xres={gpu_res.rsplit('x', 1)[0]},yres={gpu_res.split('x', 1)[-1]},edid=off",
    ]
    if targets.soc(p.target).touch:
        gpu[1] += f",id={GPU_ID}"
    display_backend = env.get("DISPLAY_BACKEND") or ("egl-headless" if accel == "kvm" else "none")
    dbus_addr = _get(env, "DBUS_ADDR", f"unix:path={os.path.join(p.run_dir, 'dbus.sock')}")
    if guest_kernel == "x86-64":
        qemu, qemu_args = tree.qemu_x86_64(env)
        if not qemu:
            raise Error(
                "guest_kernel x86-64 needs qemu-system-x86_64: the vendored QEMU "
                f"({tree.vendored_qemu_x86_64()}) has none; QEMU_X86_64=PATH for a local build"
            )
    else:
        qemu, qemu_args = tree.qemu(env)
    if accel == "tcg":
        threads = _get(env, "TCG_THREADS", "multi")
        if threads not in ("single", "multi"):
            raise Error("TCG_THREADS must be single or multi")
        tcg = f"tcg,thread={threads}"
        if env.get("TCG_TB_SIZE"):
            if not env["TCG_TB_SIZE"].isdigit() or env["TCG_TB_SIZE"] == "0":
                raise Error("TCG_TB_SIZE must be a positive size in MiB")
            tcg += f",tb-size={env['TCG_TB_SIZE']}"
    # the display: under TCG, or on the x86-64 kernel's PC host, a render node or the surfaceless
    # display of a host without one (WSL2); under the arm64 kernel's KVM, the board's render node
    if accel == "tcg" or guest_kernel == "x86-64":
        if display_backend == "none":
            display, gpu = ["-display", "none"], []
        elif display_backend in ("egl-headless", "dbus"):
            # A render node is all a distro QEMU needs (as on the Pi); the surfaceless display of a
            # host without one (WSL2) needs the vendored QEMU's patches.
            node = tcg_rendernode(env)
            if node == "surfaceless" and qemu not in (
                tree.vendored_qemu(),
                tree.vendored_qemu_x86_64(),
                env.get("QEMU_X86_64"),
            ):
                say("note: RENDERNODE=surfaceless needs the patched QEMU (scripts/build.sh host)")
            if node == "surfaceless" and is_char("/dev/dxg") and "GALLIUM_DRIVER" not in env:
                p.env["GALLIUM_DRIVER"] = "d3d12"
            if display_backend == "dbus":
                display = ["-display", f"dbus,gl=on,rendernode={node},addr={dbus_addr}"]
            else:
                display = ["-display", f"egl-headless,rendernode={node}"]
            gd = p.env.get("GALLIUM_DRIVER")
            say(
                f"display: {display_backend}, virgl on {node}"
                + (f" (GALLIUM_DRIVER={gd})" if gd else "")
            )
        else:
            raise Error(f"unknown DISPLAY_BACKEND={display_backend} (none, egl-headless or dbus)")
    else:
        node = _get(env, "RENDERNODE", "/dev/dri/renderD128")
        if display_backend == "none":
            display, gpu = ["-display", "none"], []
        elif display_backend == "egl-headless":
            display = ["-display", f"egl-headless,rendernode={node}"]
        elif display_backend == "dbus":
            display = ["-display", f"dbus,gl=on,rendernode={node},addr={dbus_addr}"]
        else:
            raise Error(f"unknown DISPLAY_BACKEND={display_backend} (none, egl-headless or dbus)")
    if display_backend != "none" and not dry:
        say(gl_renderer_line(node, {**os.environ, **env, **p.env}))
    if p.capture_relay and display_backend == "dbus":
        # the relay's sink: the headless viewer on the display's bus
        viewer = env.get("BEANJAR_VIEWER") or tree.build("viewer", "beanjar-viewer")
        p.capture_relay = (*p.capture_relay[:2], (viewer, dbus_addr))
    if gpu and render_driver(node) in INTEGER_INPUT_DRIVERS:
        p.env.setdefault("VIRGL_USE_INTEGER", "1")
    machine = "q35" if guest_kernel == "x86-64" else "virt"
    memory = []
    # queue's Weston (the gl layer's weston-setup: TV-1 and TV-2 on two virtual heads, K8HP and O22
    # alike) needs a second output. Spacebuns' native one has one, as a set: its TV-2 output takes
    # the overlay plane of the one CRTC (the gl layer's kernel words, guest kernel patch 0028).
    two_outputs = p.target[1] == "queue" and "gl" in p.layers and display_backend == "dbus"
    if gpu and gpu_process(env, node, qemu, tree, say):
        # virgl in QEMU's vhost-user-gpu backend, its own process (docs/dev/architecture/gpu-process.md):
        # the same device to the guest, at the same resolution and id. The backend maps the guest's
        # RAM to read its buffers, so the RAM is a shared memfd.
        p.gpu_socket = os.path.join(env.get("RUNTIME_DIRECTORY") or p.run_dir, GPU_SOCK)
        p.gpu_backend_argv = [gpu_backend(env, qemu, tree), "--virgl", f"--render-node={node}"]
        gpu[1] = gpu[1].replace("virtio-gpu-gl-pci,", "vhost-user-gpu-pci,chardev=vgpu,", 1)
        gpu = ["-chardev", f"socket,id=vgpu,path={p.gpu_socket}", *gpu]
        memory = ["-object", f"memory-backend-memfd,id=ram,size={mem}M,share=on"]
        machine += ",memory-backend=ram"
        say(f"GPU: vhost-user-gpu on {node}, its own process")
    if two_outputs:
        gpu[-1] += ",max_outputs=2"
        # queue's Weston: the primary is the set's TV connector, TV-1, and its heads' copies get names of
        # their own (kernel patch 0020, weston-setup)
        if p.target[1] == "queue":
            append += " virtio_gpu.tv_connector=1"
        p.display_setup = (dbus_addr, socks["QMP_SOCK"])
        if "-S" in env.get("QEMU_EXTRA", "").split():
            p.display_setup += ("--keep-paused",)
    audio_default = "dbus" if display_backend == "dbus" else "none"

    # Audio. The mixing engine with fixed settings gives the D-Bus
    # listener one Init for QEMU's lifetime: 44.1 kHz s16 stereo, 441 frames (10 ms) per Write.
    # streams=1 is playback only.
    audio_mode = _get(env, "AUDIO", audio_default)
    if audio_mode == "dbus":
        if display_backend != "dbus":
            raise Error("AUDIO=dbus needs DISPLAY_BACKEND=dbus")
        audio = list(DBUS_AUDIO)
        display[-1] += ",audiodev=snd0"
    elif audio_mode == "none":
        audio = list(NONE_AUDIO)
    elif audio_mode == "off":
        audio = []
    else:
        raise Error(f"unknown AUDIO={audio_mode} (dbus, none or off)")
    if audio_mode != "off" or accel == "kvm":
        say(f"audio: {audio_mode}")

    boot = [
        *qemu_args,
        "-kernel",
        kernel,
        "-initrd",
        initrd,
        "-append",
        append,
    ]
    extra = env.get("QEMU_EXTRA", "").split()
    if guest_kernel == "x86-64":
        devices = pci_budget(devices, gpu + audio, Q35_ROOT_SLOTS)
        # q35 with the virt machine's devices, which are all PCI and so the same there (virtio,
        # qemu-xhci, the media pool's ivshmem-doorbell), the serial console on its 8250 (ttyS0),
        # and no default VGA, which would take the guest's first display
        p.argv = [
            qemu,
            "-M",
            machine,
            *memory,
            *(
                ["-accel", "kvm", "-cpu", "host"]
                if accel == "kvm"
                else ["-cpu", _get(env, "TCG_CPU", "max"), "-accel", tcg]
            ),
            "-vga",
            "none",
            "-smp",
            smp,
            "-m",
            mem,
            *display,
            *gpu,
            *console,
            *boot,
            "-netdev",
            netdev,
            *devices,
            *audio,
            *extra,
        ]
    elif accel == "tcg":
        devices = pci_budget(devices, gpu + audio)
        p.argv = [
            qemu,
            "-M",
            machine,
            *memory,
            "-cpu",
            _get(env, "TCG_CPU", "max,pauth=off"),
            "-accel",
            tcg,
            "-smp",
            smp,
            "-m",
            mem,
            *display,
            *gpu,
            *console,
            *boot,
            "-netdev",
            netdev,
            *devices,
            *audio,
            *extra,
        ]
    else:
        devices = pci_budget(devices, gpu + audio)
        p.argv = [
            qemu,
            "-M",
            machine,
            *memory,
            "-accel",
            "kvm",
            "-cpu",
            "host",
            "-smp",
            smp,
            "-m",
            mem,
            *display,
            *console,
            *gpu,
            *boot,
            "-netdev",
            netdev,
            *devices,
            *audio,
            *extra,
        ]
    if p.display_setup:
        p.argv.append("-S")
    return p


# The PCI devices the virt machine's root bus has room for (its 32 slots but the host bridge's).
ROOT_SLOTS = 31
# q35's: its 32 slots but the host bridge's (0) and the ICH9 functions' (0x1f: LPC, SATA, SMBus)
Q35_ROOT_SLOTS = 30
# The -device names that are PCI devices: virtio-*-pci, qemu-xhci, and the media pool's
# ivshmem-doorbell (no -pci in its name, but a slot all the same).
PCI_DEVICE_SUFFIXES = ("-pci", "-xhci", "ivshmem-doorbell", "ivshmem-plain")
# The disks a PCI bridge of pci_budget's takes: its slots 1 to 31 (slot 0 is left free)
BRIDGE_SLOTS = 31


# The display's device id, which a touch model's tablet is bound to (input_devices).
GPU_ID = "gpu"


def input_devices(touch):
    """QEMU arguments of the guest's pointing devices: the tablet (the Magic Remote's pointer,
    guest/initramfs/src/virtio-pointer.c) and, for a model with a touch screen (targets.Soc.touch),
    QEMU's virtio-multitouch-pci (LG's touch panel, guest/initramfs/src/virtio-touch.c).
    QEMU gives a button event to the first handler that takes buttons, and both devices do; the
    last one the guest's driver activated comes first. So beside the touch screen the tablet is
    bound to the display (GPU_ID's head 0), whose console's button events go to bound handlers
    first: the viewers' mouse buttons stay the pointer's and touches (multitouch events, which
    the tablet does not take) go to the touch screen. Without a display the binding is void and
    there is no console to send either."""
    if not touch:
        return ["-device", "virtio-tablet-pci"]
    return [
        "-device",
        f"virtio-tablet-pci,display={GPU_ID},head=0",
        "-device",
        "virtio-multitouch-pci",
    ]


def pci_budget(devices, others=(), root_slots=ROOT_SLOTS):
    """devices (QEMU arguments) as they fit the root bus beside others (more arguments, the GPU's
    and the sound card's): as they are if their PCI devices do, else
    with every virtio-blk-pci disk behind PCI bridges of their own (SimpleSmart attaches 15 paks and 9
    state disks, docs/dev/architecture/simplesmart.md; and every VM a disk per layer), each at a slot
    of a bridge in their order, BRIDGE_SLOTS a bridge."""
    args = list(devices) + list(others)
    pci = [
        a
        for k, a in zip(args, args[1:])
        if k == "-device" and a.split(",")[0].endswith(PCI_DEVICE_SUFFIXES)
    ]
    if len(pci) <= root_slots:
        return devices
    if len(devices) % 2:
        raise ValueError("devices: not option-value pairs")
    pairs_ = list(zip(devices[::2], devices[1::2]))
    disks = sum(1 for k, a in pairs_ if k == "-device" and a.startswith("virtio-blk-pci,"))
    out = []
    for n in range(1, -(-disks // BRIDGE_SLOTS) + 1):
        out += ["-device", f"pci-bridge,id={bridge_id(n)},chassis_nr={n},shpc=off"]
    i = 0
    for k, a in pairs_:
        if k == "-device" and a.startswith("virtio-blk-pci,"):
            a += f",bus={bridge_id(i // BRIDGE_SLOTS + 1)},addr=0x{i % BRIDGE_SLOTS + 1:x}"
            i += 1
        out += [k, a]
    return out


def bridge_id(n):
    """The id of pci_budget's disk bridge n (1, 2, ...): disks, disks2, ..."""
    return "disks" if n == 1 else f"disks{n}"


def guest_mac(target, tree=paths.TREE, broadcast=None):
    """The board MAC (the target's board.cmdline's ethaddr=, which tvservice and libdile_crypto
    read; SimpleSmart's MBoot passes it as macadr=) of the target. Comment lines are not the
    cmdline's: one may name webos.ethaddr= in passing."""
    try:
        with open(tree.src(targets.board_cmdline_src(target, broadcast))) as f:
            for line in f:
                if line.lstrip().startswith("#"):
                    continue
                m = re.match(r".*(?:ethaddr|macadr)=([0-9A-Fa-f:]*)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


def is_char(path):
    try:
        return stat.S_ISCHR(os.stat(path).st_mode)
    except OSError:
        return False


def is_vgem(node):
    """Whether a render node is vgem's: a dma-buf stand-in with no GPU (WSL loads it for VA-API,
    docs/dev/architecture/vnc.md), which virgl must not take. Its device is .../vgem (platform or faux)."""
    dev = os.path.realpath(f"/sys/class/drm/{os.path.basename(node)}/device")
    return os.path.basename(dev) == "vgem"


def first_rendernode():
    for n in sorted(glob.glob("/dev/dri/renderD*")):
        if is_char(n) and not is_vgem(n):
            return n
    return None


# Host GPU drivers that convert integer vertex data to float when the shader's input is a float:
# virglrenderer declares every vertex input a vec4 and bit-casts the integer ones back, unless
# VIRGL_USE_INTEGER (or a GL_VENDOR of "ARM") makes it declare them uvec4/ivec4. Panfrost (the
# ROCK Pi 4's Mali-T860) needs it, or a guest's glVertexAttribIPointer data arrives as float bits:
# Flutter's text, tests/regress/virgl/int-attribs.c.
INTEGER_INPUT_DRIVERS = ("panfrost",)


def render_driver(node):
    """The kernel driver of a render node (panfrost, v3d, ...), "" for surfaceless or unknown."""
    dev = f"/sys/class/drm/{os.path.basename(node)}/device/driver"
    return os.path.basename(os.path.realpath(dev)) if os.path.exists(dev) else ""


def gpu_backend(env, qemu, tree=paths.TREE):
    """The vhost-user-gpu backend to run: VHOST_USER_GPU, else the vendored QEMU's (built and
    packaged with it); None for another QEMU, which brings no backend of ours."""
    if env.get("VHOST_USER_GPU"):
        return env["VHOST_USER_GPU"]
    if qemu != tree.vendored_qemu():
        return None
    return tree.build("qemu", "libexec", "vhost-user-gpu")


def gpu_process(env, node, qemu, tree=paths.TREE, say=print):
    """Whether the VM's virgl GPU runs in its own process (GPU_DEVICE): vhost-user-gpu; auto,
    on an aarch64 host whose render node is a GPU's (the Pi, the ROCK Pi 4s; the PC under WSL keeps virtio-gpu-gl, whose read-back it needs). A guest
    with two outputs too: QEMU 10.0's backend aborts when one resource is on both, which a
    two-output guest (queue) does only at its last DRM close, on the way down, and every restart is a new QEMU with a new
    backend (lifecycle.py; docs/dev/architecture/gpu-process.md). Error if vhost-user-gpu is asked for
    and cannot be run."""
    mode = env.get("GPU_DEVICE") or "auto"
    if mode not in vmsettings.GPUS:
        raise Error(f"unknown GPU_DEVICE={mode} ({', '.join(vmsettings.GPUS)})")
    if mode == "virtio-gpu-gl":
        return False
    if mode == "auto" and platform.machine() != "aarch64":
        return False
    backend = gpu_backend(env, qemu, tree)
    usable = node != "surfaceless" and is_char(node) and not is_vgem(node)
    runnable = backend is not None and os.access(backend, os.X_OK)
    if mode == "vhost-user-gpu":
        if not usable:
            raise Error(f"GPU_DEVICE=vhost-user-gpu needs a GPU's render node, not {node}")
        if not runnable:
            raise Error(
                f"GPU_DEVICE=vhost-user-gpu: no backend {backend or '(set VHOST_USER_GPU)'}"
            )
        return True
    if usable and not runnable:
        say(
            f"note: no vhost-user-gpu backend ({backend or qemu + ' has none of ours'}): virtio-gpu-gl"
        )
    return usable and runnable


def parse_eglinfo_renderer(text):
    """The GL_RENDERER string of `eglinfo -B` output: the ES profile's, else the core profile's,
    else the compatibility profile's ("D3D12 (NVIDIA GeForce RTX 3060)" on Mesa's d3d12, naming the
    adapter); None when it has none."""
    found = {}
    for m in re.finditer(
        r"^OpenGL (ES|core|compatibility) profile renderer: (.+?)\s*$", text, re.MULTILINE
    ):
        found.setdefault(m.group(1), m.group(2))
    for profile in ("ES", "core", "compatibility"):
        if profile in found:
            return found[profile]
    return None


def gl_renderer_line(node, env):
    """The launch log's line naming the GL renderer virgl gets on NODE ("surfaceless" or a render
    node): asked of Mesa's EGL with `eglinfo` (mesa-utils) on the same platform, node and Mesa
    environment as QEMU's display (the same device, so the adapter the d3d12 driver picks is the
    one named). Without eglinfo, or for a render node GBM would not pick, it says so."""
    if node == "surfaceless":
        platform_args, platform_env = ["-p", "surfaceless"], {"EGL_PLATFORM": "surfaceless"}
    elif node == first_rendernode():
        # eglinfo's GBM platform opens the first render node, as does QEMU's default
        platform_args, platform_env = ["-p", "gbm"], {}
    else:
        return f"gl renderer: not probed (render node {node})"
    exe = shutil.which("eglinfo", path=env.get("PATH"))
    if not exe:
        return "gl renderer: unknown (eglinfo not installed)"
    try:
        out = subprocess.run(
            [exe, "-B", *platform_args],
            env={**env, **platform_env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "gl renderer: unknown (eglinfo failed)"
    return f"gl renderer: {parse_eglinfo_renderer(out) or 'unknown (eglinfo reported none)'}"


def tcg_rendernode(env=os.environ):
    """The render node of a TCG VM's virgl display: RENDERNODE, else the first /dev/dri/renderD*
    that is not vgem's,
    else "surfaceless", which only the vendored QEMU (build/qemu, beanjar-qemu) has
    (`host/scripts/vm-launcher config RENDERNODE` prints it)."""
    return env.get("RENDERNODE") or first_rendernode() or "surfaceless"


SSG_CERT_DIR = "usr/palm/services/com.webos.service.secondscreen.gateway/certs/cert"


def guest_layer(p):
    """Assemble the plan's per-VM layer (layer_dir, the guest's share "vm") from its identity
    directory, afresh, with what the guest needs of it and nothing else; directories are 0755 (the
    overlay shows the top layer's directories to the guest) unless named below, files 0600:
      etc/emulator/sestore.bin                        host helper's immutable kernel-store snapshot
      etc/emulator/country (0644)                     COUNTRY, the alpha-3 code and a newline, where set
      the release's gateway reads an SSG key (targets.Release.ssg_key):
        usr/palm/services/com.webos.service.secondscreen.gateway/certs/cert/ssg.server.cert.pem (0644)
                                                      keys.ssg_cert_data of it, over LG's certificate
      NET_MODE=mitm, or direct with NET_LG_SERVICES=mock (ca), 0644:
        etc/emulator/netstub-ca.pem                   ca_dir/ca.pem, the proxy CA's certificate
        etc/emulator/netstub-ca.hash                  its OpenSSL subject hash (guest/initramfs/rootfs/init links
                                                      etc/ssl/certs/<hash>.<first free n> to it)
      rootssh or hbchannel (in LAYERS; NAME is the layer's):
        etc/emulator/NAME/dropbear_ed25519_host_key     identity/rootssh_host_key (Homebrew Channel's
                                                      Dropbear takes the same host key, so known_hosts
                                                      pins either at the same port)
        etc/emulator/NAME/authorized_keys               identity/authorized_keys and id_ed25519.pub,
                                                      comments left out (keys.guest_authorized)
        home/root/.ssh/authorized_keys (.ssh 0700)      the same, for the boot before /home is bound
      devmode (in LAYERS), etc/emulator/devmode (0700):
        ssh_host_rsa_key                              identity/devmode_host_key
        developer_rsa, developer_rsa.pub (0644)       identity/devmode_rsa(.pub), unencrypted: the
                                                      guest encrypts it with its own passphrase
    The client key (id_ed25519) and the CA's key never go in. Also rewrites identity/known_hosts for
    the ports."""
    from . import sestore

    store = None
    compiled = None
    if not getattr(p, "target", None) or targets.platform(p.target) == "webOS":
        try:
            if getattr(p, "target", None) and targets.release(p.target).ssg_key:
                sestore.ensure_ssg(p.identity_dir)
            sestore.ensure_dvr_key(p.identity_dir)  # read where the chip's KADP shim answers it
            store = sestore.effective(p.identity_dir)
            compiled = sestore.bundle(*store)
        except (sestore.Error, keys.Error) as error:
            raise Error(str(error)) from None
    items = store[1] if store else {}
    shutil.rmtree(p.layer_dir, ignore_errors=True)
    os.makedirs(p.layer_dir)
    os.chmod(p.layer_dir, 0o755)

    def mkdir(rel, mode=0o755):
        d = p.layer_dir
        for sub in rel.split("/"):
            d = os.path.join(d, sub)
            if not os.path.isdir(d):
                os.mkdir(d)
                os.chmod(d, 0o755)
        os.chmod(d, mode)
        return d

    def put(rel, data, mode=0o600):
        fd = os.open(os.path.join(p.layer_dir, rel), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as out:
            out.write(data)

    def identity(name):
        try:
            with open(os.path.join(p.identity_dir, name), "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise Error(
                f"{os.path.join(p.identity_dir, name)} is missing (made at create, or at "
                "`host/scripts/vm-launcher start`)"
            ) from None

    if store:
        mkdir("etc/emulator")
        put("etc/emulator/sestore.bin", compiled)
    if getattr(p, "country", None):
        mkdir("etc/emulator")
        put("etc/emulator/country", (p.country + "\n").encode(), 0o644)
    if sestore.SSG in items:
        try:
            cert = keys.ssg_cert_data(items[sestore.SSG])
        except keys.Error as e:
            raise Error(f"the second screen gateway's certificate: {e}") from None
        mkdir(SSG_CERT_DIR)
        put(SSG_CERT_DIR + "/ssg.server.cert.pem", cert, 0o644)
    if p.ca:
        try:
            with open(p.ca, "rb") as f:
                cert = f.read()
        except FileNotFoundError:
            raise Error(
                f"{p.ca} is missing (made at create, or at `host/scripts/vm-launcher start`)"
            ) from None
        r = subprocess.run(
            ["openssl", "x509", "-noout", "-subject_hash"],
            input=cert,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        digest = r.stdout.decode().strip()
        if r.returncode != 0 or not re.fullmatch(r"[0-9a-f]{8}", digest):
            raise Error(f"{p.ca}: not a certificate ({r.stderr.decode(errors='replace').strip()})")
        mkdir("etc/emulator")
        put("etc/emulator/netstub-ca.pem", cert, 0o644)
        put("etc/emulator/netstub-ca.hash", (digest + "\n").encode(), 0o644)
    for name in ("rootssh", "hbchannel"):  # they conflict (layer.toml): one at most
        if name not in p.layers:
            continue
        try:
            auth = keys.guest_authorized(p.identity_dir).encode()
        except FileNotFoundError as e:
            raise Error(
                f"{e.filename} is missing (made at create, or at `host/scripts/vm-launcher start`)"
            ) from None
        except keys.Error as e:
            raise Error(str(e)) from None
        mkdir(f"etc/emulator/{name}")
        put(f"etc/emulator/{name}/dropbear_ed25519_host_key", identity(keys.ROOTSSH_HOST))
        put(f"etc/emulator/{name}/authorized_keys", auth)
        mkdir("home/root/.ssh", 0o700)
        put("home/root/.ssh/authorized_keys", auth)
    if "devmode" in p.layers:
        mkdir("etc/emulator/devmode", 0o700)
        put("etc/emulator/devmode/ssh_host_rsa_key", identity(keys.DEVMODE_HOST))
        put("etc/emulator/devmode/developer_rsa", identity(keys.DEVMODE))
        put("etc/emulator/devmode/developer_rsa.pub", identity(keys.DEVMODE + ".pub"), 0o644)
    if os.path.isdir(p.identity_dir):
        keys.known_hosts(p.identity_dir, p.service_host, p.ssh_port)


def make_disks(p):
    """Create the plan's state disks (sparse; a seeded one starts with its seed's bytes), removing the
    blank images to re-create first."""
    os.makedirs(p.state_dir, exist_ok=True)
    for img in p.remove:
        os.unlink(img)
    for img, size, seed in p.disks:
        tmp = img + ".tmp"
        with open(tmp, "wb") as f:
            if seed:
                with open(seed, "rb") as src:
                    shutil.copyfileobj(src, f)
            if f.tell() > size:
                raise Error(f"{seed}: larger than the {size} bytes of {img}")
            f.truncate(size)
        os.replace(tmp, img)


def part_nodes(p):
    """[(N, serial)]: the /dev/mmcblk0pN nodes of the plan's VM, from its own partinfo disk (what
    lgemu.ko reads; the guest may have rewritten it, as a set's update and factory reset do). Error if
    its partitions are not those of the firmware's map, which the disks were made for."""
    if not targets.board_data(p.target):
        return []
    own = os.path.join(p.state_dir, "partinfo.img")
    try:
        m = partmap.read(own)
        if not partmap.same_layout(m, partmap.read(p.partmap)):
            raise partmap.Error(
                f"{own}: its partitions are not the firmware's map's ({p.partmap}): "
                "wipe it (host/scripts/vm-launcher reset-state partinfo)"
            )
        return partmap.nodes(m, p.target)
    except (partmap.Error, OSError) as e:
        raise Error(f"partition map: {e}") from None


def prepare_nvm(p, say):
    """The state's I2C EEPROM image as the VM's board says, before QEMU starts (nvm.prepare: a new
    one becomes the VM's prepared chip, else its tool options and serial are brought in line)."""
    if not targets.release(p.target).nvm:
        return
    img = os.path.join(p.state_dir, targets.release(p.target).nvm.eeprom + ".img")
    try:
        debug_mode = nvm.debug_mode_number(p.env.get("DEBUG_MODE"))
    except nvm.Error as e:
        raise Error(str(e)) from None
    nvm.prepare(
        img,
        p.target,
        p.prepared.chip,
        p.env.get("SERIAL"),
        p.tree,
        say,
        broadcast=p.broadcast,
        debug_mode=debug_mode,
    )


def qemu_users(state_dir):
    """Pids of running QEMUs with a disk in state_dir (their command line has file=<dir>/)."""
    pat = re.compile("qemu-system.*file=" + re.escape(state_dir.rstrip("/") + "/"))
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if pat.search(cmd):
            out.append(int(pid))
    return out


def reset_state(state_dir, parts=(), tree=paths.TREE, out=print):
    """Wipe a state directory: the next boot starts from a factory-fresh TV, with blank disks that
    the guest formats. With parts (db8, cmn_data, appstore; tvservice's nvram and eeprom) only those
    images. A state that a running QEMU has open is refused."""
    if state_dir == "none":
        out("STATE=none has nothing to wipe")
        return
    if not state_dir.startswith("/"):
        state_dir = os.path.join(tree.top, state_dir)
    if not os.path.isdir(state_dir):
        out(f"no state at {state_dir}")
        return
    if qemu_users(state_dir):
        raise Error(
            f"a running QEMU uses {state_dir}; stop it first (host/scripts/vm-launcher stop)"
        )
    if not parts:
        for n in os.listdir(state_dir):
            if n.endswith((".img", ".debugmode")):
                os.unlink(os.path.join(state_dir, n))
        try:
            os.rmdir(state_dir)
        except OSError:
            pass
        out(f"wiped {state_dir}")
    else:
        for part in parts:
            img = os.path.join(state_dir, part + ".img")
            try:
                os.unlink(img)
            except FileNotFoundError:
                pass
            out(f"wiped {img}")
