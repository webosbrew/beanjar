# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The firmware targets this tree boots, and what each axis of a target says: the one statement of the always-used layers, the paks, the state disks and
the firmware paths guest/initramfs/rootfs/init touches. Nothing else in the launcher, the manager, init or the
scripts lists them; they ask here (boot_conf() for the guest, `host/scripts/vm-launcher config PAKS` for scripts).

A target is (chip, release): chip the SoC ("h13", "m14", "m2", "m16p", "m3", "k5lp", "k6lp", "k7lp", "lm14", "lm21a"), release
the webOS release family, the codename of etc/starfish-release ("afro", "beehive", "dreadlocks", "dreadlocks2", "goldilocks", "goldilocks2", "jhericurl", "kisscurl"), or "simplesmart", LG's
SimpleSmart line (docs/dev/architecture/simplesmart.md), or "netcast45", NetCast 4.5
(docs/dev/architecture/netcast45.md); neither is webOS.

The table is data, guest/targets/ (TARGETS_DIR; docs/dev/architecture/firmware-targets.md says how to
add to it), which this module loads and checks once, at import; it keeps the data model, the
loader and the rules that derive values from the rows. One file per target, <chip>-<release>.toml:
the model codes whose firmware boots it, those still being brought up, the target's own flags, and
the target's own rows where its firmware line differs from its chip's or its release's ([soc],
[release]). soc/<chip>.toml is a Soc row (SOCS), what belongs to the SoC; release/<release>.toml a
Release row (RELEASES), what belongs to the release; partmap/<name>.toml an assembled partition
map (PartmapLayout) that release rows name. A row with base is the row base names (a soc file's, a
release file's) with the keys it gives replaced whole; a row without one is stated whole. A webOS
release's base is the release before it in LG's order (WEBOS_ORDER), afro's none.
"""

import hashlib
import os
import re
import subprocess
import sys
import tomllib
from collections import namedtuple

from . import base, memory, paths

# The firmwares this tree boots, by the model code in their name (epk2extract's "..._DTV_<model>_...")
# -> {release: chip}, from the target files' models. One board can ship several release families, so a firmware's target is
# (MODELS[model][release], release) with release the codename family its own rootfs names
# (RELEASE_FILES, release_family), never its version number or model year.
# Import resolves a firmware once (paths.firmware_target) and
# records the target in its manifest.toml. BRING_UP: the models whose target is still being brought up
# (a target file's bring_up): scripts/regress leaves it out of its default set and runs it only when
# named with --target. Remove the model there once its target passes regress --headless.
# A target file's tested = false: the target has never booted (made from its firmware's facts
# alone). It is built and packaged like any other, but regress leaves
# it out of its default set and runs it only when named, and creating or starting a VM of it says
# UNTESTED (untested_warning). Remove the line once the target passes regress --headless.

# LG's platforms, as LG names them: a VM's platform (vm.toml's platform, the viewer's title) is its
# target's release's (Release.platform), and so is the version it is shown with (Release.version: LG's
# marketing version, e.g. webOS 4.5; None where it is not known, the platform's name alone).
# "Test" is not LG's: the open test guest (guest/testguest, docs/dev/architecture/test-guest.md), a
# Buildroot userland from beanjar-vendor that CI boots without LG firmware.
PLATFORMS = ("webOS", "NetCast", "SimpleSmart", "Test")

# The always-used layers of a target, highest priority first (as LAYERS): its release's
# (Release.always_layers). A webOS release's are generated per target from guest/layers/webos/<release>,
# guest/layers/soc/<chip> and guest/layers/common; SimpleSmart's is guest/layers/simplesmart alone, the
# test guest's guest/testguest. Above them the
# VM's optional layers, then the per-VM layer "vm". ALWAYS_LAYERS: every
# name that is some release's always-used layer, never an optional one.
WEBOS_LAYERS = ("webos", "soc", "common")
SIMPLESMART_LAYERS = ("simplesmart",)
NETCAST45_LAYERS = ("netcast45",)
NETCAST4_LAYERS = ("netcast4",)
TESTGUEST_LAYERS = ("testguest",)
ALWAYS_LAYERS = (
    WEBOS_LAYERS + SIMPLESMART_LAYERS + NETCAST45_LAYERS + NETCAST4_LAYERS + TESTGUEST_LAYERS
)
VM_LAYER = "vm"

# serial: the virtio-blk serial the guest finds the disk by; mount: where init mounts it in the
# firmware's root ("/" is the stock rootfs, the overlay's bottom; None: init does not mount it, it is
# attached for a PakLower, or for the firmware to mount itself from its /dev/mmcblk0pN).
# optional: a pak only some builds of the release ship (tpli.pak, the satellite transponder list of
# the European NetCast-line builds): a firmware without it boots without its disk, as a set whose
# partition is empty (release(target, fw) leaves it out).
Pak = namedtuple("Pak", "file serial mount optional", defaults=(None, False))
# A lower layer made from a pak (W16R's bsppart): the directory subdir of the pak serial, stacked
# above the stock rootfs and below the soc layer.
PakLower = namedtuple("PakLower", "serial subdir")
# A kernel module of guest/kernel/modules that the SoC's userland needs besides lgemu.ko: its name (the
# .ko in the initramfs) and its parameters, which carry the chip's values; derived, parameters the
# launcher reads out of the firmware itself when it prepares the VM (derived_params, prepare.py), None if none: "rpc-table",
# rtk.ko's RPC answers from the firmware's libkadaptor (host/helpers/rtk/rpc-table.py). btmtk_usb (m3, k5lp:
# bsppart's MediaTek Bluetooth stack, webos-bluetooth-service) is its driver with no adapter, family
# its netlink family (the firmware driver's btmtk_nl_init). webos_tv (m3, k5lp: bsppart's
# libdvbfrontend, tvservice's tuner) is LG's WDVB tuner driver with no signal, its *_cmd the
# build's property commands of HAL_DVBV5_GetLIBVersion, CheckLockState and TunePostJob.
SocModule = namedtuple("SocModule", "name params derived", defaults=(None,))
# SocModule.derived: "rpc-table" (above); "mstar-mmap", mstar-utopia.ko's mmapger records and record
# IDs out of a NetCast vendor pak's libmadp (host/helpers/mstar/mmap-table.py); "mstar-mboot", its
# mmapger records out of a webOS board's MBoot, the table its TEE memory map agrees with
# (host/helpers/mstar/mboot-mmap.py), which needs the firmware's boot.pak (DERIVED_PAKS).
DERIVED = ("rpc-table", "mstar-mmap", "mstar-mboot")
DERIVED_PAKS = {"mstar-mboot": ("boot",)}
# continent: the model's area as its factory programs it into a new EEPROM's ContinentOptionDB (on
# a release with one, Nvm.continent_offset): (continent index, language-country group, HW setting
# group), the enums' values; None elsewhere. inputs: the model's input ports as its factory programs
# them into a new EEPROM's ToolOptionDB (on a release with Nvm.inputs_field): the bytes there,
# None elsewhere. lgemu: lgemu.ko's parameters for this chip (boot.conf's MODULE_PARAMS_lgemu; init
# adds nvm_disk=), such as the standard Linux I2C adapters webOS 4's DILE_I2C opens.
# board_src: the source directory, in the checkout, of the layer that carries board_cmdline (None:
# the chip's soc layer, guest/layers/soc/<chip>/rootfs); the launcher reads it there (board_cmdline_src).
# pointer_wakeup: the builtin M-RCU requires lginput2's cursor-on event before motion.
# mic: the board's model (the one modelName= of the row's board.cmdline, which every target of the
# row boots) has LG's built-in far-field microphone, a fact of that model's hardware (its service
# manual), never another model's: lgemu.ko's mic=1, its capture card and the micom's answers about
# it. False where nothing shows the model has one; a
# row for another model sets its own.
# broadcast_boards: ((broadcast, board_cmdline), ...), the board of another retail model for a
# firmware of that broadcast standard (board_broadcast: its model_id.json region, or for a
# global firmware the VM's vm.toml firmware.broadcast), in the same layer; the others boot
# board_cmdline. The board's region values must be the firmware's (W22O: the global C2, the US C2
# on the same global firmware, and the Japanese one). Where board_cmdline is one of them, that
# standard is the chip's default (default_broadcast): a chip whose global firmware ships on US
# retail models boots its US model by default (US_BOARD, its broadcast_boards with EU_BOARD).
# model_boards: ((model, broadcast, board_cmdline), ...), the board of another retail model of the
# chip that a VM chooses by name (vm.toml's firmware.model, board_model), in the same layer: a set
# of the same firmware whose hardware differs from the default board's (W22O's OLED Flex,
# 42LX3QPUA, with its tool type LX3Q). Its broadcast standard is the board's region; the VM's
# board_broadcast is then that one. Its values come from the firmware's own tables, as the
# broadcast boards' do.
# touch: the board's model has a touch screen (the StanbyME's USB HID panel), a fact of that
# model's hardware like mic: the launcher attaches QEMU's virtio-multitouch-pci, which the
# initramfs's touch bridge gives webOS as LG's panel (guest/initramfs/src/README.md, "Touch screen").
# portrait: the board's model turns to portrait (the StanbyME: its toolType ART10 makes configd's
# com.webos.service.msc.supportPortrait true, and msc turns the UI by the set's accelerometer,
# lgemu's accel device): `beanjar sensor set rotate` sets the sensor, the launcher adds the webos.rotation port
# that reports msc's orientation back, and the viewers turn the picture to match
# (host/core/beanjar/rotation.py). Other models refuse a rotation.
# light_bus: the I2C channel (lgemu's Linux adapter) of the ambient light sensor, libdile_i2c's
# LIGHT_SENSOR_1 and LIGHT_SENSOR_2 rows (8-bit slave 0x20) in the chip's firmwares; None where
# they have none. Whether a board has the sensor, and which type, is its own tool option
# digitalEye (lls.digital_eye, prepared: light_dev), not the chip's (lgemu light.c).
# thermal: the chip's kernel has /proc/thermalinfo, which fancontroller reads (lgemu thermal.c):
# every chip whose firmware ships fancontroller; IDDQ_RELEASE and later read its IDDQ files.
# tof_bus: the I2C channel of the TMF8821 ToF motion sensor, libdile_i2c's TMF882X_SENSOR_N row
# (8-bit slave 0x82) in the chip's firmwares; None where they have none. Whether a board has the
# sensor is its own tool option supportMotionDetectSensor (lls.motion_sensor, prepared: tof_dev),
# not the chip's (lgemu tof.c).
# led_micom_bus: the I2C channel of the OLED Flex's gaming LED micom, libdile_i2c's GAMING_LED_MICOM
# row (8-bit slave 0x12) in the chip's firmwares; None where they have none. Whether a board has the
# micom is its own tool type (lls.tool_type, prepared: led_micom_dev), not the chip's (lgemu
# ledmicom.c).
# broadcast_params: ((broadcast, module, (words...)), ...): a module's parameters on the board of
# that broadcast standard (board_broadcast; None: the chip's default, default_broadcast), each
# word replacing the module's own word of the same name: the tuners of a US model differ from a
# European one's (webos_tv's frontends= and delsys=).
Soc = namedtuple(
    "Soc",
    "board_cmdline modules continent inputs lgemu board_src pointer_wakeup mic "
    "broadcast_boards touch portrait light_bus thermal tof_bus broadcast_params led_micom_bus "
    "model_boards",
    defaults=(
        None,
        None,
        (),
        None,
        False,
        False,
        (),
        False,
        False,
        None,
        False,
        None,
        (),
        None,
        (),
    ),
)
# Prefer the US model: a chip whose global firmware (model_id.json region GLOBAL) ships on US retail
# models, the models DB's models of its OTA ID, boots a US model of that firmware by default
# (board-atsc.cmdline: ATSC, the US country digit, configd's USA RMM group). Its European board
# (board-dvb.cmdline) stays a VM's choice with vm.toml's firmware.broadcast "dvb"; a firmware that
# names a standard (firmware_broadcast) boots that standard's board (docs/user/board-profiles.md).
US_BOARD = "/etc/emulator/board-atsc.cmdline"
EU_BOARD = "/etc/emulator/board-dvb.cmdline"
Release = namedtuple(
    "Release",
    "paks pak_lowers state_disks nvm_disks nvm init_system pmlog_mode pmlog_job trust_bundles "
    "trust_hash_dir ddr_sizes cap_last_cap partmap partitions emmc_disks platform version always_layers dram "
    "board_in_kernel screen pointer_wakeup ssg_key peripherals init unions",
    defaults=(WEBOS_LAYERS, None, False, "graphic", False, False, (), "/sbin/init", ()),
)
# unions: ((mount point, (lower, ...)), ...), read-only unions of directories in the firmware's root,
# the highest first, which init makes with overlayfs once the paks are mounted: NetCast 4's lginit
# mounts "unionfs" (dirs=/mnt/nsu/patch:/mnt/nsu/base=ro), which LG's kernel has and ours has not,
# and then binds directories of the union where its programs look (/mnt/addon, /mnt/lg/res/xml).
# init: the program webos-start runs as PID 1 of the firmware's namespace: /sbin/init, or NetCast
# 4's lginit, which LG's kernel runs itself (CONFIG_LG_INIT, before /sbin/init).
# peripherals: the guest kernel's own modules (its =m drivers, /lib/modules/<release> in the
# initramfs) that init loads with `modprobe -a` before the firmware starts, their dependencies from
# modules.dep: the USB class drivers and filesystems the release's stock kernel has, built in or as
# a module (its decoded config, modules.builtin and module files; scratch notes summarised in
# docs/dev/architecture/build-and-boot.md, "Peripheral modules"); none where it has none.
# ssg_key: the second screen gateway reads its TLS key from the secure store (getSSGSSL, webOS 5 on):
# the VM's own key and certificate go into its layer (qemu.guest_layer).
# board_in_kernel: the board's parameters (board_cmdline) go on the kernel's command line itself,
# first, rather than onto the namespace's /proc/cmdline only: SimpleSmart's stock lginit_app mounts
# a proc of its own over /proc, which shows the kernel's line (docs/dev/architecture/simplesmart.md, "Board").
# screen: the size of the VM's display (virtio-gpu): "graphic", the graphic (OSD) plane's (webOS:
# its compositor lays out on it), or "panel", the panel's (SimpleSmart: the GOP scans its planes
# out to the panel, and they are placed in the panel's pixels), or a (w, h) of the release's own.
# The eMMC partition map (beanjar/partmap.py builds the VM's partinfo disk from it). PartmapFile: LG's own table, the 12368-byte object `partinfo` of the ELF file path
# in pak (the firmware's factory-init program). PartmapLayout: a table assembled for a release whose
# own is stale: header, the PartmapFile whose table gives the magic, versions and device; parts,
# its entries in order (MapPart), at consecutive offsets from 0; reserved, the last entry's (size,
# used, valid, flags). A MapPart's size is bytes, ("pak", file): that pak's size rounded up to
# 1 MiB, or ("disk", serial): that state disk's size; the other fields are partition_info's.
PartmapFile = namedtuple("PartmapFile", "pak path")
# An EPK's raw partition table (W21A's partinfo.pak), rather than an ELF object.
PartmapImage = namedtuple("PartmapImage", "pak")
PartmapLayout = namedtuple("PartmapLayout", "header parts reserved")
MapPart = namedtuple("MapPart", "name size filename sw_ver used valid flags")
# Release.emmc_disks: the serials of the read-write disks that back eMMC partitions with no
# filesystem of ours, each sized as its partition in the map; a new one is blank (zeros), but
# "partinfo", which starts as the map (partmap.image). sedata starts blank too, not as the
# firmware's sedata pak (what a set's update flashes): libsestore decrypts that through the TEE,
# which the VM has not, and on W20L the failed session then crashed webos-connman-adapter in
# libkadaptor's TEEC_CloseSession; blank, libsestore finds no sedata and goes on.
# The flag bits of LG's u-boot (partcommon.h): FIXED 1, MASTER 2, IDKEY 4, DATA 16, SECURED 32
# The I2C NVM EEPROM (beanjar/nvm.py programs a new one; lgemu.ko serves it), the release's record layouts: eeprom, the NVM disk that is its
# image; records, (pak, path) of the ELF file with the firmware's default records (gModelInfoDB,
# gToolOptionDB, gContinentOptDB); the ModelInfoDB's place in the EEPROM (DIL_NVM_InitModelInfoDB),
# its valid mark and its model name and serial fields as (offset in the record, length); the
# ToolOptionDB's place (_InitToolOptionDB), its tool option words (how many, their size, the first
# one's offset in the record) and its valid mark; continent_offset, where a new EEPROM also gets a
# ContinentOptionDB (the default record's, set to Soc.continent), 0 for none; continent_table, the
# records file's table of continents it takes the option words from, (symbol, entry size);
# lastinput_offset, where a new EEPROM also gets the Last_Input_info DB (gLastInputInfoDB, the
# default record: the TV input), 0 for none; inputs_field, (offset in the ToolOptionDB, length) of
# the input-port bytes a factory programs (Soc.inputs), None where the release has none;
# tool_option_given, how many of the words the bootloader copies onto the command line (ToolOpt=),
# None for all of them (the others keep the default record's); factory_offset, where a new EEPROM
# gets the FactoryDB (gFactoryDB, the default record: substrate mode off at +0x9C), 0 for none.
# An Nvm without records (None, and none of the layout) but with lls, the path of LG's
# lowlevelstorage in the rootfs, is a chip LLS owns (webOS 5 on): a new one gets LLS's own default
# records, made by running the firmware's code on the board's command line (beanjar/lls.py).
# local_key, on a tvservice release: where its tvservice reads the model's local key type
# (tv.model.localKeyType, OSA_MD_GetNewLocalKeyType through tvconfig), a LocalKeyField of the
# board's ToolOpt= words (the 1-based word, the bit offset, the width, the names of the values
# in order, tvservice's DWARF types), or the one type of a release that has no such option. The
# viewer's local key (lls.local_key).
# debug_status_offset, where the EEPROM keeps the TV's debug status as one plain byte, 3 DEBUG,
# 4 EVENT, 5 RELEASE (the system DB's nDebugStatus, tvservice's OSA_MD_GetDebugStatus: webOS 1 to
# 3.9, 0x1A1; W19P's 0x1A8, the plain copy its tvservice and U-Boot check the encrypted block
# against); None where there is none (W18R, whose status is the block only) or the status is
# lowlevelstorage's (webOS 5 on: lls.debug_status_item). A blank or out-of-range byte reads as 5.
# Every start writes the effective mode to it (nvm.sync_debug_status).
# debug_status_values, the byte's values for DEBUG, EVENT and RELEASE where they are not 3, 4, 5
# (NetCast 4.5: 0, 1, 2).
# debug_block_offset, where webOS 4.x (W18R, W19P) keeps the debug status as 32 bytes that the
# SoC's TEE encrypts (DILE_CRYPTO_EncryptDbgData; 0x1C6). The soc layer's cipher for it is the
# identity (guest/layers/soc/src/libdile_crypto/dbg-data.c), and every start writes the clear
# block of the effective mode in the firmware's own encoding (nvm.sync_debug_block,
# beanjar/nvmdebug.py); the new chip's preparation checks the offset, and the plain copy's, against
# the build's tvservice (nvm.check_debug_block) and refuses one that differs. None elsewhere.
# factory, (pak, path) of the ELF file whose code says the fields a factory leaves in a new chip of
# a release without records (NetCast 4.5's RELEASE: beanjar/nvmfactory.py); None: such a chip is
# left all 0xFF for the firmware.
Nvm = namedtuple(
    "Nvm",
    "eeprom records modelinfo_offset modelinfo_mark model_field serial_field "
    "tooloption_offset tool_option_words tool_option_word_bytes tool_option_first "
    "tool_option_mark continent_offset continent_table lastinput_offset inputs_field "
    "tool_option_given factory_offset lls local_key debug_status_offset debug_status_values "
    "factory debug_block_offset",
    defaults=(None,) * 10 + (0, None, 0, None, None, 0, None, None, None, (3, 4, 5), None, None),
)
LocalKeyField = namedtuple("LocalKeyField", "word offset width names")
# tvservice's own records, in every release here (the tvservice pak's lgapp/tvservice)
TVSERVICE = ("tvservice-dvb", "/mnt/lg/tvservice/lgapp/tvservice")


# A target's own facts besides its rows (its target file): models, the model codes whose firmware of
# the release boots it (MODELS); bring_up, those of them still being brought up (BRING_UP: a model
# is, or is not, in every target it has); lg_scaler, (w, h) of the panel whose pixels the regions of
# LG's V4L2 scaler are in, for a target whose stock libval-starfish (W22O queue: its nyxVscaler
# module) drives the scaler (/dev/video28..31) through lgemu.ko's model (v4l2_vsc): O22's UHD sets, and LM21's (UD LCD), whose MStar XC is not where their windows go; the
# StanbyME (lm21u) has an FHD panel (panelResolution=FHD): its
# videooutputd sets the regions in 1920x1080; None elsewhere. de_histogram: the stock
# libpqeca (kadpd's histogram thread) reads /proc/lg/de/v4l2_capture_read and crashes in fscanf
# when it is missing: lg115x.ko's stopped-histogram entry (de_histogram_proc). v4l2_capture: the V4L2 capture
# /dev/video60 (lgemu.ko's v4l2-capture.c, added by the chip's vendor module, CAPTURE_MODULES, with v4l2_capture=1),
# which captureservice's BLENDED and VIDEO read: every LG-O target from mullet to spacebuns, Realtek's from K6LP and
# MStar's LM21 (libvtcapture's VtDriver, and queue's and spacebuns' nyxScreenCapture.module, make the same calls as W24G ombre's client).
# viewer_capture: the launcher's webos.capture port (the viewer's picture on demand, lgemu_capture_frame)
# for a capture model other than video60, which has it anyway: M14's VT frame buffers (lg115x.ko's de0)
# and W16R's DIP (the libkadaptor shim, through video60, so v4l2_capture there).
# tested: False for a target that has never booted (its file's tested = false; absent: True).
# first_boot_restart: the firmware restarts the set once on the first boot of a fresh state, as a set
# of it does at its first setup (W24G spacebuns' google-home-controller switches sdx to its QA2 server
# and eula-service reboots: docs/research/spacebuns-fresh-state-restarts.md); scripts/regress follows
# that one restart and fails any other.
Target = namedtuple(
    "Target",
    "models bring_up lg_scaler de_histogram v4l2_capture viewer_capture tested first_boot_restart",
    defaults=(True, False),
)

INIT_SYSTEMS = ("upstart", "systemd", "sysvinit")
# The broadcast standards. A firmware is built for one, and its model_id.json says which
# (firmware_broadcast; GLOBAL for a global build); its region paks, where it has them, are named for
# it (tvservice-dvb.pak, otncabi-arib.pak, ...; a global build's otncabi-global.pak), but a
# super.pak firmware's are not.
BROADCASTS = ("dvb", "atsc", "arib")


class Error(Exception):
    pass


# ------------------------------------------------------------------------------------- the loader
# Where the table is: guest/targets of the checkout, or beanjar-guest's copy
# (/usr/share/beanjar/guest/targets: each guest build carries the definitions that describe it).
TARGETS_DIR = paths.TREE.src("guest", "targets")


def _fail(where, what):
    raise Error(f"{where}: {what}")


def _str(where, v):
    if not isinstance(v, str) or not v:
        _fail(where, f"expected a non-empty string, not {v!r}")
    return v


def _text(where, v):
    if not isinstance(v, str):
        _fail(where, f"expected a string, not {v!r}")
    return v


def _int(where, v):
    if not isinstance(v, int) or isinstance(v, bool):
        _fail(where, f"expected an integer, not {v!r}")
    return v


def _byte(where, v):
    if _int(where, v) not in range(256):
        _fail(where, f"expected a byte (0 to 255), not {v}")
    return v


def _bool(where, v):
    if not isinstance(v, bool):
        _fail(where, f"expected true or false, not {v!r}")
    return v


def _list(where, v, item, n=None):
    if not isinstance(v, list):
        _fail(where, f"expected an array, not {v!r}")
    if n is not None and len(v) != n:
        _fail(where, f"expected {n} items, not {len(v)}")
    return tuple(item(f"{where}[{i}]", x) for i, x in enumerate(v))


def _strs(where, v):
    return _list(where, v, _str)


def _ints(n=None):
    return lambda where, v: _list(where, v, _int, n)


def _one_of(values):
    def check(where, v):
        if v not in values:
            _fail(where, f"expected one of {', '.join(map(repr, values))}, not {v!r}")
        return v

    return check


_SIZE = re.compile(r"\d+[KMG]?")


def _size(where, v):
    if not isinstance(v, str) or not _SIZE.fullmatch(v):
        _fail(where, f"expected a size such as 512M, not {v!r}")
    return v


def _table(where, v, spec, required=()):
    """v, a TOML table, checked against spec ({key: check(where, value)}): every key known, every
    required one there. {key: checked value}."""
    if not isinstance(v, dict):
        _fail(where, f"expected a table, not {v!r}")
    for k in v:
        if k not in spec:
            _fail(where, f"unknown key {k!r}")
    for k in required:
        if k not in v:
            _fail(where, f"missing key {k!r}")
    sub = (lambda k: f"{where}.{k}") if ": " in where else (lambda k: f"{where}: {k}")
    return {k: spec[k](sub(k), x) for k, x in v.items()}


def _pairs_of(check):
    """A table of name = value as ((name, value), ...), in its order."""

    def conv(where, v):
        if not isinstance(v, dict):
            _fail(where, f"expected a table, not {v!r}")
        return tuple((k, check(f"{where}.{k}", x)) for k, x in v.items())

    return conv


def _pak(where, v):
    t = _table(
        where,
        v,
        {"file": _str, "serial": _str, "mount": _str, "optional": _bool},
        ("file", "serial"),
    )
    return Pak(t["file"], t["serial"], t.get("mount"), t.get("optional", False))


def _pak_lower(where, v):
    return PakLower(**_table(where, v, {"serial": _str, "subdir": _str}, ("serial", "subdir")))


def _module(where, v):
    t = _table(
        where,
        v,
        {"name": _str, "params": _strs, "derived": _one_of(DERIVED)},
        ("name",),
    )
    return SocModule(t["name"], t.get("params", ()), t.get("derived"))


def _broadcast_params(where, v):
    if not isinstance(v, dict):
        _fail(where, f"expected a table, not {v!r}")
    out = []
    for b, mods in v.items():
        _one_of(BROADCASTS)(f"{where}.{b}", b)
        if not isinstance(mods, dict):
            _fail(f"{where}.{b}", f"expected a table, not {mods!r}")
        out += [(b, m, _strs(f"{where}.{b}.{m}", words)) for m, words in mods.items()]
    return tuple(out)


def _broadcast_boards(where, v):
    out = _pairs_of(_str)(where, v)
    for b, _ in out:
        _one_of(BROADCASTS)(f"{where}.{b}", b)
    return out


def _model_boards(where, v):
    if not isinstance(v, dict):
        _fail(where, f"expected a table, not {v!r}")
    out = []
    for m, b in v.items():
        t = _table(
            f"{where}.{m}",
            b,
            {"broadcast": _one_of(BROADCASTS), "board": _str},
            ("broadcast", "board"),
        )
        out.append((m, t["broadcast"], t["board"]))
    return tuple(out)


def _bytes(where, v):
    return bytes(_list(where, v, _byte))


SOC_KEYS = {
    "base": _str,
    "board_cmdline": _str,
    "broadcast_boards": _broadcast_boards,
    "model_boards": _model_boards,
    "board_src": _str,
    "continent": _ints(3),
    "inputs": _bytes,
    "lgemu": _strs,
    "light_bus": _int,
    "tof_bus": _int,
    "led_micom_bus": _int,
    "thermal": _bool,
    "mic": _bool,
    "touch": _bool,
    "portrait": _bool,
    "pointer_wakeup": _bool,
    "modules": lambda w, v: _list(w, v, _module),
    "broadcast_params": _broadcast_params,
}
SOC_REQUIRED = ("board_cmdline", "modules")


def _local_key(where, v):
    if isinstance(v, str):
        return v
    t = _table(
        where,
        v,
        {"word": _int, "offset": _int, "width": _int, "names": _strs},
        ("word", "offset", "width", "names"),
    )
    return LocalKeyField(**t)


def _nvm(where, v):
    t = _table(
        where,
        v,
        {
            "eeprom": _str,
            "records": lambda w, x: tuple(
                _table(w, x, {"pak": _str, "path": _str}, ("pak", "path")).values()
            ),
            "modelinfo_offset": _int,
            "modelinfo_mark": _int,
            "model_field": _ints(2),
            "serial_field": _ints(2),
            "tooloption_offset": _int,
            "tool_option_words": _int,
            "tool_option_word_bytes": _int,
            "tool_option_first": _int,
            "tool_option_mark": _int,
            "continent_offset": _int,
            "continent_table": lambda w, x: tuple(
                _table(w, x, {"symbol": _str, "size": _int}, ("symbol", "size")).values()
            ),
            "lastinput_offset": _int,
            "inputs_field": _ints(2),
            "tool_option_given": _int,
            "factory_offset": _int,
            "lls": _str,
            "local_key": _local_key,
            "debug_status_offset": _int,
            "debug_status_values": lambda w, x: tuple(_ints(3)(w, x)),
            "debug_block_offset": _int,
            "factory": lambda w, x: tuple(
                _table(w, x, {"pak": _str, "path": _str}, ("pak", "path")).values()
            ),
        },
        ("eeprom",),
    )
    return Nvm(**t)


def _partmap_ref(where, v):
    """A release's partmap: the name of a partmap/<name>.toml (resolved once all are loaded), an
    ELF file's table {pak, path}, or an EPK's raw table {image}."""
    if isinstance(v, str):
        return _str(where, v)
    if isinstance(v, dict) and "image" in v:
        return PartmapImage(_table(where, v, {"image": _str}, ("image",))["image"])
    return PartmapFile(**_table(where, v, {"pak": _str, "path": _str}, ("pak", "path")))


def _screen(where, v):
    return v if v in ("graphic", "panel") else _ints(2)(where, v)


RELEASE_KEYS = {
    "base": _str,
    "platform": _one_of(PLATFORMS),
    "version": _str,
    "always_layers": _strs,
    "init_system": _one_of(INIT_SYSTEMS),
    "init": _str,
    "unions": _pairs_of(lambda w, x: tuple(_strs(w, x))),
    "pmlog_mode": _str,
    "pmlog_job": _str,
    "cap_last_cap": _int,
    "ddr_sizes": _ints(),
    "dram": _int,
    "board_in_kernel": _bool,
    "screen": _screen,
    "pointer_wakeup": _bool,
    "ssg_key": _bool,
    "trust_bundles": _strs,
    "trust_hash_dir": _str,
    "peripherals": _strs,
    "paks": lambda w, v: _list(w, v, _pak),
    "pak_lowers": lambda w, v: _list(w, v, _pak_lower),
    "state_disks": _pairs_of(_size),
    "nvm_disks": _pairs_of(_size),
    "emmc_disks": _strs,
    "partmap": _partmap_ref,
    "nvm": _nvm,
    "partitions": lambda w, v: dict(_pairs_of(_str)(w, v)),
}
RELEASE_REQUIRED = (
    "platform", "init_system", "cap_last_cap", "ddr_sizes", "paks", "state_disks", "nvm_disks",
    "emmc_disks", "partitions",
)  # fmt: skip
# what a release row stated whole without these keys has
RELEASE_OPTIONAL = {
    "version": None,
    "pmlog_mode": None,
    "pmlog_job": None,
    "trust_hash_dir": None,
    "trust_bundles": (),
    "pak_lowers": (),
    # a release without LG's board data (the test guest's): no eMMC partition map, no NVM EEPROM
    "partmap": None,
    "nvm": None,
}


def _map_part(where, v):
    def size(w, x):
        if isinstance(x, dict):
            t = _table(w, x, {"pak": _str, "disk": _str})
            if len(t) != 1:
                _fail(w, "expected { pak = ... } or { disk = ... }")
            return next(iter(t.items()))
        return _int(w, x)

    t = _table(
        where,
        v,
        {
            "name": _str,
            "size": size,
            "filename": _text,
            "sw_ver": _int,
            "used": _int,
            "valid": _int,
            "flags": _int,
        },
        ("name", "size", "flags"),
    )
    return MapPart(
        t["name"],
        t["size"],
        t.get("filename", ""),
        t.get("sw_ver", 0),
        t.get("used", 1),
        t.get("valid", 1),
        t["flags"],
    )


def _partmap_layout(where, v):
    t = _table(
        where,
        v,
        {
            "header": lambda w, x: PartmapFile(
                **_table(w, x, {"pak": _str, "path": _str}, ("pak", "path"))
            ),
            "reserved": lambda w, x: tuple(
                _table(
                    w,
                    x,
                    {"size": _int, "used": _int, "valid": _int, "flags": _int},
                    ("size", "used", "valid", "flags"),
                ).values()
            ),
            "parts": lambda w, x: _list(w, x, _map_part),
        },
        ("header", "parts", "reserved"),
    )
    return PartmapLayout(t["header"], t["parts"], t["reserved"])


def _read(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as e:
        raise Error(f"{path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise Error(f"{path}: {e}") from None


def _files(directory):
    try:
        return sorted(n[: -len(".toml")] for n in os.listdir(directory) if n.endswith(".toml"))
    except FileNotFoundError:
        return []


def _resolve(kind, rows, name, make, seen=()):
    """rows[name] (the checked keys of a row) as a row: base's with its keys replaced, or made
    whole (make)."""
    where, t = rows[name]
    if name in seen:
        raise Error(f"{where}: base: a loop ({' -> '.join(seen + (name,))})")
    if "base" not in t:
        return make(where, t)
    if t["base"] not in rows:
        raise Error(f"{where}: base: no {kind} {t['base']!r}")
    base = _resolve(kind, rows, t["base"], make, seen + (name,))
    return base._replace(**{k: v for k, v in t.items() if k != "base"})


def _make_soc(where, t):
    for k in SOC_REQUIRED:
        if k not in t:
            raise Error(f"{where}: missing key {k!r}")
    return Soc(**t)


def _make_release(where, t):
    for k in RELEASE_REQUIRED:
        if k not in t:
            raise Error(f"{where}: missing key {k!r}")
    return Release(**{**RELEASE_OPTIONAL, **t})


TARGET_KEYS = {
    "models": _strs,
    "bring_up": _strs,
    "lg_scaler": _ints(2),
    "de_histogram": _bool,
    "v4l2_capture": _bool,
    "viewer_capture": _bool,
    "tested": _bool,
    "first_boot_restart": _bool,
    "soc": lambda w, v: _table(w, v, SOC_KEYS),
    "release": lambda w, v: _table(w, v, RELEASE_KEYS),
}


def load(directory):
    """The table in directory (TARGETS_DIR's layout) as a dict: MODELS, BRING_UP, SOCS, RELEASES,
    TARGET_SOCS, TARGET_RELEASES, TARGETS ({target: Target}) and WEBOS_ORDER. Error, naming the file and
    the key, for anything the schema does not allow."""

    def rel(*p):
        return os.path.join(directory, *p)

    if not os.path.isdir(directory):
        raise Error(f"{directory}: no such directory (the target table)")

    layouts = {
        n: _partmap_layout(rel("partmap", n + ".toml"), _read(rel("partmap", n + ".toml")))
        for n in _files(rel("partmap"))
    }
    socs = {
        n: (
            rel("soc", n + ".toml"),
            _table(rel("soc", n + ".toml"), _read(rel("soc", n + ".toml")), SOC_KEYS),
        )
        for n in _files(rel("soc"))
    }
    releases = {
        n: (
            rel("release", n + ".toml"),
            _table(rel("release", n + ".toml"), _read(rel("release", n + ".toml")), RELEASE_KEYS),
        )
        for n in _files(rel("release"))
    }
    used = set()

    def layout(where, r):
        if not isinstance(r.partmap, str):
            return r
        if r.partmap not in layouts:
            raise Error(f"{where}: partmap: no {rel('partmap', r.partmap + '.toml')}")
        used.add(r.partmap)
        return r._replace(partmap=layouts[r.partmap])

    SOCS = {n: _resolve("soc", socs, n, _make_soc) for n in socs}
    RELEASES = {
        n: layout(releases[n][0], _resolve("release", releases, n, _make_release)) for n in releases
    }

    # LG's order: afro, then each webOS release the one whose base it is
    webos = [n for n in RELEASES if RELEASES[n].platform == "webOS"]
    first = [n for n in webos if "base" not in releases[n][1]]
    if len(first) != 1:
        raise Error(
            f"{rel('release')}: not one webOS release without base: {', '.join(first) or 'none'}"
        )
    order = first
    while True:
        nxt = [n for n in webos if releases[n][1].get("base") == order[-1]]
        if len(nxt) > 1:
            raise Error(f"{rel('release')}: {' and '.join(nxt)} both have base {order[-1]!r}")
        if not nxt:
            break
        order += nxt
    if set(order) != set(webos):
        rest = ", ".join(sorted(set(webos) - set(order)))
        raise Error(f"{rel('release')}: {rest}: not in LG's order (each base the release before)")

    MODELS, BRING_UP, TARGET_SOCS, TARGET_RELEASES, TARGETS = {}, set(), {}, {}, {}
    status = {}  # model -> (bring up?, the file that says so)
    for n in _files(directory):
        where = rel(n + ".toml")
        t = _table(where, _read(where), TARGET_KEYS, ("models",))
        parts = n.split("-")
        if len(parts) != 2 or not all(parts):
            raise Error(f"{where}: the name is not <chip>-<release>")
        target = tuple(parts)
        if target[1] not in RELEASES:
            raise Error(f"{where}: no {rel('release', target[1] + '.toml')}")
        if "soc" in t:
            TARGET_SOCS[target] = _resolve(
                "soc", {**socs, n: (where + ": soc", t["soc"])}, n, _make_soc
            )
        elif target[0] not in SOCS:
            raise Error(f"{where}: no {rel('soc', target[0] + '.toml')} and no [soc]")
        if "release" in t:
            rows = {**releases, n: (where + ": release", t["release"])}
            TARGET_RELEASES[target] = layout(
                where + ": release", _resolve("release", rows, n, _make_release)
            )
        if not t["models"]:
            raise Error(f"{where}: models: none")
        if t.get("tested") is True:
            raise Error(f"{where}: tested: only tested = false is said (absent: tested)")
        for m in t.get("bring_up", ()):
            if m not in t["models"]:
                raise Error(f"{where}: bring_up: {m} is not one of its models")
        for m in t["models"]:
            if target[1] in MODELS.get(m, {}):
                other = name((MODELS[m][target[1]], target[1]))
                raise Error(f"{where}: models: {m}'s {target[1]} firmware is already {other}'s")
            MODELS.setdefault(m, {})[target[1]] = target[0]
            b = m in t.get("bring_up", ())
            if status.setdefault(m, (b, where))[0] != b:
                said = "in bring_up" if b else "not in bring_up"
                raise Error(f"{where}: {m} is {said} here, but not so in {status[m][1]}")
            if b:
                BRING_UP.add(m)
        TARGETS[target] = Target(
            tuple(t["models"]),
            tuple(t.get("bring_up", ())),
            t.get("lg_scaler"),
            t.get("de_histogram", False),
            t.get("v4l2_capture", False),
            t.get("viewer_capture", False),
            t.get("tested", True),
            t.get("first_boot_restart", False),
        )
    for n in sorted(set(layouts) - used):
        raise Error(f"{rel('partmap', n + '.toml')}: no release names it")
    rank = {r: i for i, r in enumerate(order)}
    MODELS = {
        m: dict(sorted(MODELS[m].items(), key=lambda kv: (rank.get(kv[0], len(rank)), kv[0])))
        for m in sorted(MODELS)
    }
    return {
        "MODELS": MODELS,
        "BRING_UP": BRING_UP,
        "SOCS": SOCS,
        "RELEASES": RELEASES,
        "TARGET_SOCS": TARGET_SOCS,
        "TARGET_RELEASES": TARGET_RELEASES,
        "TARGETS": TARGETS,
        "WEBOS_ORDER": tuple(order),
    }


def name(target):
    """h13-afro: the name of a target's build directory (guest-<name>) and layer directory."""
    return f"{target[0]}-{target[1]}"


class Missing(Error, base.Error):
    """No board definitions: an installed beanjar has them from beanjar-guest, which it depends on
    (no VM boots without it anyway), a checkout from its guest/targets. A targets.Error where one is
    caught, and a base.Error, which the CLI prints as it is."""

    def __init__(self, directory, installed):
        if installed:
            what = "beanjar needs beanjar-guest of its host-guest contract installed"
        else:
            what = "the checkout has no guest/targets"
        super().__init__(f"no board definitions at {directory}: {what}")


class _Absent:
    """A table name where the definitions are missing: importing this module works, so what never
    reads a board (--help, host management, the web and UI front pages) still runs; any use of the
    table raises Missing."""

    def __init__(self, directory, installed):
        self._where = (directory, installed)

    def _raise(self, *_):
        raise Missing(*self._where)

    __getitem__ = __iter__ = __contains__ = __len__ = __bool__ = _raise

    def __getattr__(self, name):
        self._raise()


def _load_or_absent(directory, installed):
    """load(directory), or, where it is not there or has no target files, an _Absent for every name."""
    if _files(directory):
        return load(directory)
    absent = _Absent(directory, installed)
    return dict.fromkeys(
        (
            "MODELS",
            "BRING_UP",
            "SOCS",
            "RELEASES",
            "TARGET_SOCS",
            "TARGET_RELEASES",
            "TARGETS",
            "WEBOS_ORDER",
        ),
        absent,
    )


_table_now = _load_or_absent(TARGETS_DIR, paths.TREE.installed)
MODELS = _table_now["MODELS"]
BRING_UP = _table_now["BRING_UP"]
SOCS = _table_now["SOCS"]
RELEASES = _table_now["RELEASES"]
TARGET_SOCS = _table_now["TARGET_SOCS"]
TARGET_RELEASES = _table_now["TARGET_RELEASES"]
TARGETS = _table_now["TARGETS"]
# The webOS release families in LG's order (their starfish-release majors: number1 8, ombre 9,
# ponytail 10, queue 11, spacebuns 12): each release file's base is the one before it. A guest
# build rule that only a release can decide applies from a release on, so later ones inherit it
# (`vm-launcher config RELEASES`; meson.build's webos_reached, docs/dev/architecture/build-and-boot.md).
WEBOS_ORDER = _table_now["WEBOS_ORDER"]
del _table_now

# The file of a rootfs that names its webOS release: "Rockhopper release 3.4.3-6213
# (dreadlocks-digya)" in etc/starfish-release (every firmware here). The release family is the
# codename before the '-'. etc/os-release, which later firmwares add, carries only the version number, not
# the codename, so it cannot tell a family and is not read.
RELEASE_FILES = ("etc/starfish-release",)
_RELEASE_RE = re.compile(r"\brelease\s+\d+\.\S*\s+\(([a-z0-9]+)-[^)]*\)")


def release_family(text):
    """The webOS release family (codename) of a release file's text (RELEASE_FILES), or None."""
    m = _RELEASE_RE.search(text)
    return m.group(1) if m else None


def all_targets():
    """Every target MODELS names, sorted by name."""
    return sorted(
        {(chip, rel) for releases in MODELS.values() for rel, chip in releases.items()}, key=name
    )


def in_bring_up(target):
    """True if every model of target is in BRING_UP (the target is left out of regress's default set)."""
    chip, rel = target
    models = [m for m, rs in MODELS.items() if rs.get(rel) == chip]
    return bool(models) and all(m in BRING_UP for m in models)


# What creating or starting a VM of an untested target says (untested_warning): a warning, never a refusal.
UNTESTED = "not tested yet: may not boot"


def tested(target):
    """False for a target that has never booted (its file's tested = false), True otherwise."""
    return TARGETS[tuple(target)].tested


def untested_targets():
    """The names of the targets that have never booted (tested), sorted."""
    return [name(p) for p in all_targets() if not tested(p)]


def untested_warning(target):
    """ "<chip>-<release>: not tested yet: may not boot" for an untested target, None otherwise."""
    return None if tested(target) else f"{name(target)}: {UNTESTED}"


def supported(target):
    return tuple(target) in all_targets()


def by_name(target_name):
    """The target named target_name (name()), or None."""
    return next((p for p in all_targets() if name(p) == target_name), None)


def of_firmware(model, release):
    """The target of a firmware of model code model whose rootfs names release (release_family), or
    None if MODELS has no such row."""
    chip = MODELS.get(model, {}).get(release)
    return (chip, release) if chip else None


# The vendor modules that add video60 for their chip (v4l2_capture=1): a chip has one of them.
CAPTURE_MODULES = ("lg115x", "rtk", "mstar-utopia")


def v4l2_capture(target):
    """True for a target whose vendor module serves the V4L2 capture (its file's v4l2_capture):
    the module's video60 and the launcher's webos.capture port that feeds it."""
    return _target(target).v4l2_capture


def first_boot_restart(target):
    """True for a target whose firmware restarts the set once on the first boot of a fresh state
    (its file's first_boot_restart)."""
    return _target(target).first_boot_restart


def viewer_capture(target):
    """True for a target whose guest reads the viewer's picture on demand (the webos.capture port and
    the guest's bridge to lgemu.ko): video60's (v4l2_capture) and the other capture models' (its file's
    viewer_capture)."""
    t = _target(target)
    return t.v4l2_capture or t.viewer_capture


def soc(target):
    return TARGET_SOCS.get(tuple(target)) or SOCS[target[0]]


def board_cmdline(target, broadcast=None, model=None):
    """The guest path of the board.cmdline a VM of the target boots for the broadcast standard
    broadcast (board_broadcast): the board of the retail model model (board_model: one of
    Soc.model_boards) when given, else the one of Soc.broadcast_boards for broadcast, else
    Soc.board_cmdline."""
    s = soc(target)
    if model:
        return _model_board(target, model)[1]
    return (
        dict(s.broadcast_boards).get(broadcast, s.board_cmdline) if broadcast else s.board_cmdline
    )


def board_cmdline_src(target, broadcast=None, model=None):
    """The checkout-relative path of the target's board.cmdline (board_cmdline: for broadcast or model, in the
    layer of Soc.board_src, the chip's soc layer by default), which the launcher reads on the
    host."""
    s = soc(target)
    return os.path.join(
        s.board_src or os.path.join("guest", "layers", "soc", target[0], "rootfs"),
        board_cmdline(target, broadcast, model).lstrip("/"),
    )


_PAK_BROADCAST = re.compile(r"-(dvb|atsc|arib)(?:\.pak)?$")
# a pak named for a region: <stem>-<global or a broadcast standard>
_REGION_PAK = re.compile(r"^(.+)-(global|dvb|atsc|arib)$")


def broadcast_of_paks(names):
    """The broadcast standard of a firmware from its pak file names (or Pak.file stems), or None
    when they name none or several."""
    found = {m.group(1) for n in names for m in [_PAK_BROADCAST.search(n)] if m}
    return found.pop() if len(found) == 1 else None


def firmware_paks(fw):
    """The .pak file names in the firmware directory fw (none for None or a missing directory)."""
    try:
        return sorted(n for n in os.listdir(fw) if n.endswith(".pak")) if fw else []
    except OSError:
        return []


# model_id.json's region (paths.model_id) -> the broadcast standard: the values LG's nyxDeviceInfo
# module and libdile_tvregion compare it with, GLOBAL a global build (which names none)
MODEL_ID_REGIONS = {"ARIB": "arib", "ATSC": "atsc", "DVB": "dvb", "GLOBAL": None}


def firmware_broadcast(fw):
    """The broadcast standard of the firmware directory fw as the firmware itself states it: its
    rootfs's etc/starfish/model_id.json region (paths.model_id; ARIB, ATSC, DVB, or GLOBAL for a
    global build: None), which a super.pak firmware's pak names do not carry; a NetCast-line
    firmware's, its RELEASE.cfg modelName's first word (paths.release_cfg_model). None for a webOS
    firmware without model_id.json (before jhericurl; those chips have one board, and their paks
    are named for their standard, which only picks the paks: _broadcast_release) or without fw. An
    Error for a region LG's code does not name."""
    identity = paths.model_id(fw)
    if identity is None:
        # a NetCast-line firmware (SimpleSmart, NetCast 4.5) states it as its RELEASE.cfg's
        # modelName, the standard before the platform: DVB-M2, ATSC-LM14
        model = paths.release_cfg_model(fw)
        standard = model.split("-", 1)[0] if model else None
        if standard is not None and standard not in MODEL_ID_REGIONS:
            raise Error(f"{fw}: RELEASE.cfg's modelName {model!r} names no broadcast standard")
        return MODEL_ID_REGIONS[standard] if standard else None
    if "region" not in identity:
        return None
    region = identity["region"]
    if region not in MODEL_ID_REGIONS:
        raise Error(
            f"{fw}: model_id.json's region {region!r} is not one of {', '.join(MODEL_ID_REGIONS)}"
        )
    return MODEL_ID_REGIONS[region]


def default_broadcast(target):
    """The standard of the target's default board (Soc.board_cmdline) where the chip names it among
    its broadcast_boards (a chip that prefers its US model: "atsc"), else None."""
    s = soc(target)
    return next((b for b, path in s.broadcast_boards if path == s.board_cmdline), None)


def models(target):
    """The retail models a VM of the target may choose (Soc.model_boards), in the table's order."""
    return tuple(m for m, _, _ in soc(target).model_boards)


def _model_board(target, model):
    """(broadcast, board_cmdline) of the retail model model's board (Soc.model_boards); an Error
    naming the chip's models for one it has no board of."""
    for m, b, path in soc(target).model_boards:
        if m == model:
            return b, path
    have = models(target)
    raise Error(
        f"{name(target)} has no board for model {model!r}"
        + (f" (it has: {', '.join(have)})" if have else " (it has no model to choose)")
    )


def board_model(target, fw=None, broadcast=None, model=None):
    """The retail model whose board a VM of the target boots, or None for the board of its
    broadcast standard (board_broadcast): model, the VM's choice (vm.toml's firmware.model: W22O's
    OLED Flex, "42LX3QPUA"), which must be one of the chip's Soc.model_boards and whose standard
    must be the one the firmware directory fw and broadcast name, where they name one."""
    if not model:
        return None
    b, _ = _model_board(target, model)
    own = firmware_broadcast(fw)
    if own and own != b:
        raise Error(
            f"model {model!r} is a {b} set, but the firmware is {own!r} (its model_id.json)"
        )
    if broadcast and broadcast != b:
        raise Error(f"model {model!r} is a {b} set, not {broadcast}: unset broadcast for it")
    return model


def board_broadcast(target, fw=None, broadcast=None, model=None):
    """The broadcast standard whose board a VM of the target boots (board_cmdline), worked out once
    per launch: the standard of model's board where the VM chooses a model (board_model), else the
    firmware directory fw's own (firmware_broadcast: its model_id.json region), else
    broadcast, the VM's choice for a global firmware (vm.toml's firmware.broadcast: W22O's global
    queue as the global C2, "dvb"), else the chip's default (default_broadcast: "atsc" for a chip
    that boots its US model). broadcast must be one the chip has a board of (Soc.broadcast_boards)
    and, where the firmware names one, that one."""
    if model:
        board_model(target, fw, broadcast, model)
        return _model_board(target, model)[0]
    own = firmware_broadcast(fw)
    if not broadcast:
        return own or default_broadcast(target)
    boards = dict(soc(target).broadcast_boards)
    if broadcast not in boards:
        raise Error(
            f"{name(target)} has no board for broadcast {broadcast!r}"
            + (f" (it has: {', '.join(boards)})" if boards else "")
        )
    if own and own != broadcast:
        raise Error(f"broadcast {broadcast!r}, but the firmware is {own!r} (its model_id.json)")
    return broadcast


def regional_model_key(target):
    """Whether the target's model-key readers (stock DILE_CRYPTO_ReadMskToSecureStore, nyxCrypto)
    ask for the board's regional item before the plain one: from kisscurl on (W21A, W21P; their
    libdile_crypto NEEDs libdile_tvregion), not jhericurl's W20L or older, which read only the
    plain item (docs/research/model-key.md)."""
    order = WEBOS_ORDER
    return target[1] in order and order.index(target[1]) >= order.index("kisscurl")


def boots_default_board(target, fw):
    """Whether a VM of the target boots the chip's default board (Soc.board_cmdline) from the firmware
    directory fw (board_broadcast: by its model_id.json, a global firmware the default board's
    standard)."""
    return board_cmdline(target, board_broadcast(target, fw)) == soc(target).board_cmdline


def _pak_renamed(layout, renames):
    """layout (a PartmapLayout) with the paks its parts are sized by renamed (renames: {old: new})"""
    return layout._replace(
        parts=tuple(
            p._replace(size=("pak", renames.get(p.size[1], p.size[1])))
            if not isinstance(p.size, int) and p.size[0] == "pak"
            else p
            for p in layout.parts
        )
    )


def _broadcast_release(r, fw):
    """r (a Release) for the firmware directory fw: each pak named for a region the firmware does not
    have (otncabi-global) is its sibling of the firmware's broadcast standard where the firmware has
    that (otncabi-arib), in paks and in the partition map's sizes. r itself where the firmware names
    none, or several, or has every pak as r names it."""
    present = firmware_paks(fw)
    broadcast = broadcast_of_paks(present)
    if not broadcast:
        return r
    renames = {}
    for p in r.paks:
        m = _REGION_PAK.match(p.file)
        if m and p.file + ".pak" not in present and f"{m.group(1)}-{broadcast}.pak" in present:
            renames[p.file] = f"{m.group(1)}-{broadcast}"
    if not renames:
        return r
    src = r.partmap
    if isinstance(src, PartmapLayout):
        src = _pak_renamed(src, renames)._replace(
            header=src.header._replace(pak=renames.get(src.header.pak, src.header.pak))
        )
    elif isinstance(src, (PartmapFile, PartmapImage)):
        src = src._replace(pak=renames.get(src.pak, src.pak))
    return r._replace(
        paks=tuple(p._replace(file=renames.get(p.file, p.file)) for p in r.paks), partmap=src
    )


def always_layers(target):
    """The target's always-used layers, highest priority first (Release.always_layers)."""
    return release(target).always_layers


def root_pak(target):
    """The pak file of the target's stock root filesystem (the Pak at /): rootfs, SimpleSmart's root."""
    return next(p.file for p in release(target).paks if p.mount == "/")


def release(target, fw=None):
    """The target's Release; with fw, a firmware directory, its paks as that firmware names them
    (_broadcast_release), without the optional ones it does not ship (Pak.optional)."""
    r = TARGET_RELEASES.get(tuple(target)) or RELEASES[target[1]]
    if not fw:
        return r
    r = _broadcast_release(r, fw)
    present = set(firmware_paks(fw))
    if any(p.optional and p.file + ".pak" not in present for p in r.paks):
        r = r._replace(
            paks=tuple(p for p in r.paks if not p.optional or p.file + ".pak" in present)
        )
    return r


def platform(target):
    """The target's platform (PLATFORMS): vm.toml's platform of its VMs."""
    return release(target).platform


def board_data(target):
    """Whether the target's VMs have LG's board data, an eMMC partition map and an NVM EEPROM
    (prepare.py): every LG release; not the test guest's (Release.partmap None)."""
    return release(target).partmap is not None


def takes_power_off(target):
    """Whether the target's guest can take the power-off signal of `beanjar stop` (tvpowerd's forced powerOff): every webOS release has tvpowerd; NetCast 4.5 and
    SimpleSmart have none. The test guest takes BusyBox's poweroff (poweroff.power_off_call)."""
    return platform(target) in ("webOS", "Test")


def platform_title(target):
    """The target's platform with its version, as the viewer's title shows it: "webOS 4.5", or the
    platform's name alone where its version is not known."""
    r = release(target)
    return f"{r.platform} {r.version}" if r.version else r.platform


# hwopt=: the digit of the DDR size, the same in every release here
HWOPT_DDR_DIGIT = 11


def ram(target, hwopt):
    """The RAM in MiB of the model whose hwopt= (board.cmdline's) is hwopt, on target's release: its DDR
    size, digit HWOPT_DDR_DIGIT read through the release's DDR_SIZE_TYPE_T (Release.ddr_sizes), or
    the release's own (Release.dram, SimpleSmart's). A VM's
    QEMU -m unless MEM says otherwise (nvm.board_ram reads the target's board.cmdline)."""
    if release(target).dram:
        return release(target).dram
    sizes = release(target).ddr_sizes
    try:
        return sizes[int(hwopt[HWOPT_DDR_DIGIT], 16)]
    except (IndexError, ValueError, TypeError):
        raise Error(
            f"{name(target)}: hwopt={hwopt}: digit {HWOPT_DDR_DIGIT} is no DDR size of {len(sizes)}"
        ) from None


HWOPT_GRAPHIC_DIGIT = 14
# libdile_boardinfo's DILE_BOARDINFO_GetGraphicResolution (W16R 0x2e40, W18R 0x29f0): hwopt digit
# 14, 1-6, picks the graphic (OSD) plane's size; 0 and anything else is invalid
GRAPHIC_RESOLUTIONS = {
    1: (1366, 768),
    2: (1920, 1080),
    3: (2560, 1080),
    4: (3840, 2160),
    5: (5120, 2160),
    6: (1280, 720),
}


def graphic_resolution(target, hwopt):
    """(w, h) of the graphic (OSD) plane of the model whose hwopt= (board.cmdline's) is hwopt: digit
    HWOPT_GRAPHIC_DIGIT through LG's table (GRAPHIC_RESOLUTIONS), which tvservice divides the web
    engine's video rectangles by to put them on the panel. The size of the VM's display (virtio-gpu,
    GPU_RES), on which the compositor and the web engine lay out; the viewer scales it to the panel.
    Every board's is 2 (1920x1080) but MStar's M2 and M3 boards' 6 (1280x720, their bootloaders'
    constant)."""
    try:
        return GRAPHIC_RESOLUTIONS[int(hwopt[HWOPT_GRAPHIC_DIGIT], 16)]
    except (IndexError, ValueError, TypeError, KeyError):
        raise Error(
            f"{name(target)}: hwopt={hwopt}: digit {HWOPT_GRAPHIC_DIGIT} is no graphic resolution"
        ) from None


HWOPT_PANEL_DIGIT = 3
# SimpleSmart's libdile_boardinfo (DILE_BOARDINFO_GetDisplayResolution): hwopt digit 3, 0 a
# 1366x768 panel, 1 a 1920x1080 one
PANEL_RESOLUTIONS = {0: (1366, 768), 1: (1920, 1080)}


def panel_resolution(target, hwopt):
    """(w, h) of the panel of the model whose hwopt= is hwopt (digit HWOPT_PANEL_DIGIT)."""
    try:
        return PANEL_RESOLUTIONS[int(hwopt[HWOPT_PANEL_DIGIT], 16)]
    except (IndexError, ValueError, TypeError, KeyError):
        raise Error(
            f"{name(target)}: hwopt={hwopt}: digit {HWOPT_PANEL_DIGIT} is no panel resolution"
        ) from None


def screen_resolution(target, hwopt):
    """(w, h) of the VM's display (Release.screen): the graphic plane's, the panel's, or the
    release's own (a (w, h) target: NetCast 4.5's)."""
    if isinstance(release(target).screen, tuple):
        return release(target).screen
    if release(target).screen == "panel":
        return panel_resolution(target, hwopt)
    return graphic_resolution(target, hwopt)


def paks(target, fw=None):
    """The paks the target's VMs attach, the stock rootfs first (release: as the firmware directory
    fw names them)."""
    return release(target, fw).paks


def pak_files(target, fw=None):
    """The pak files a VM of the target needs from its firmware (fw: that firmware directory's names,
    release): those it attaches (paks()), then those its partition map is made of (Release.partmap:
    its program's pak, an assembled table's paks, whose sizes it takes), then those its modules'
    derived parameters are read from (DERIVED_PAKS)."""
    r = release(target, fw)
    src = r.partmap
    more = (
        []
        if src is None
        else [src.pak]
        if isinstance(src, (PartmapFile, PartmapImage))
        else [src.header.pak]
        + [p.size[1] for p in src.parts if not isinstance(p.size, int) and p.size[0] == "pak"]
    )
    out = [p.file for p in r.paks]
    more += [p for m in soc(target).modules for p in DERIVED_PAKS.get(m.derived, ())]
    for f in more:
        if f not in out:
            out.append(f)
    return tuple(out)


def state_disks(target):
    """[(name, size)]: every read-write disk of a VM, the state partitions then tvservice's NVM."""
    r = release(target)
    return r.state_disks + r.nvm_disks


def size_bytes(s):
    """Bytes of a disk size as the tables give it: 512M, 32K, 2G or a number."""
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    return int(s[:-1]) * units[s[-1]] if s[-1] in units else int(s)


def emmc_disks(target):
    """The serials of the disks of the release's eMMC partitions with no filesystem of ours
    (Release.emmc_disks), sized as the map's partitions."""
    return release(target).emmc_disks


def state_parts(target):
    """Every read-write disk's serial: the state partitions, tvservice's NVM, the eMMC partitions."""
    return tuple(n for n, _ in state_disks(target)) + tuple(emmc_disks(target))


def settings_parts(target):
    """The disks with the TV's settings (tvservice's NVM): a wipe keeps them unless asked."""
    return tuple(n for n, _ in release(target).nvm_disks)


def _words(values):
    """values as one shell-safe word list: no white space or quoting in a word."""
    values = [str(v) for v in values]
    for v in values:
        if not v or any(c.isspace() or c in "\"'\\$`" for c in v):
            raise ValueError(f"boot.conf: {v!r} cannot be a word")
    return " ".join(values)


def payload_modules():
    """The kernel modules the host loads from the guest payload's build/<name>.ko: lgemu and every
    SocModule of the chips (what compat.module_params reads; packaging/debian/build.py and
    the host sync ship exactly these). lgfs and the kernel's own modules load from the
    initramfs's module tree and are not listed."""
    return ["lgemu"] + sorted({m.name for s in SOCS.values() for m in s.modules})


def module_params_var(module):
    """The boot.conf variable with a SOC_MODULES module's parameters (guest/initramfs/rootfs/init reads it)."""
    return "MODULE_PARAMS_" + module.replace("-", "_")


# host/helpers/rtk/rpc-table.py, which reads rtk.ko's RPC answers out of a Realtek firmware's libkadaptor
RPC_TABLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "host",
    "helpers",
    "rtk",
    "rpc-table.py",
)

_installed = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.isfile(os.path.join(_installed, "installation.toml")):
    RPC_TABLE = os.path.join(_installed, "helpers", "rtk", "rpc-table.py")


# host/helpers/mstar/mmap-table.py, which reads an MStar firmware's mmapger records out of its libmadp
MMAP_TABLE = os.path.join(os.path.dirname(RPC_TABLE), "..", "mstar", "mmap-table.py")
# host/helpers/mstar/mboot-mmap.py, which reads a webOS MStar board's mmapger records out of its MBoot
MBOOT_MMAP = os.path.join(os.path.dirname(MMAP_TABLE), "mboot-mmap.py")
# where the host share has the records a "mstar-mmap" or "mstar-mboot" module loads (its mmap_file=)
MMAP_SHARE_NAME = "mstar-mmap.bin"


def _param(m, key):
    return next((w.split("=", 1)[1] for w in m.params if w.startswith(key + "=")), None)


MSTAR_DERIVED = ("mstar-mmap", "mstar-mboot")


def _mstar_mmap(m, fw, fw_name):
    """(path, words) of the module m's mmapger records of the firmware directory fw, then
    mmap_file=. "mstar-mmap": mmap-table.py --cache's build/mstar/<fw_name>/mmap.bin (reused while
    the vendor pak's libmadp is the same) and the module parameters it reads out of libmadp with
    them (mmap_ids, lx_mem_id, lx_mem2_id, gop_fb). "mstar-mboot": mboot-mmap.py's (reused while
    boot.pak and the TEE's mmap.ini are the same) and their count (mmap_ids). Error if it cannot
    say."""
    r = subprocess.run(
        [
            sys.executable,
            MMAP_TABLE if m.derived == "mstar-mmap" else MBOOT_MMAP,
            fw,
            *(["--cache"] if m.derived == "mstar-mmap" else []),
            "--name",
            fw_name,
            "--miu-interval",
            _param(m, "miu_interval"),
            "--miu-length",
            _param(m, "miu_len"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = r.stdout.strip().split("\n")
    if r.returncode != 0 or len(lines) != 2:
        raise Error(f"{m.name}.ko's mmapger records from {fw}: {r.stderr.strip() or r.returncode}")
    return lines[0], tuple(lines[1].split()) + (f"mmap_file=/host/{MMAP_SHARE_NAME}",)


def derived_files(target, fw, fw_name, vm=None):
    """{name: path}: the files of the firmware the chip's modules read from the host share
    (/host/<name>), made from fw rather than kept in the tree. "mstar-mmap", "mstar-mboot":
    _mstar_mmap's mmap.bin, checked with the record IDs it derives along. Error if it cannot make
    one."""
    out = {}
    vm = vm or {}
    for m in soc(target).modules:
        if m.derived not in MSTAR_DERIVED:
            continue
        path, derived = _mstar_mmap(m, fw, fw_name)
        try:
            words = m.params + derived + vm.get(m.name, ())
            if memory.parameters(words).get("mmap_file") != f"/host/{MMAP_SHARE_NAME}":
                raise ValueError(
                    "mmap_file: only the prepared firmware-derived host-share file is supported"
                )
            with open(path, "rb") as f:
                memory.validate(m.name, words, f.read())
        except (OSError, ValueError) as e:
            raise Error(f"{name(target)}: {m.name}.ko memory layout: {e}") from None
        out[MMAP_SHARE_NAME] = path
    return out


def derived_params(target, fw, fw_name):
    """{module: (words...)}: the parameters of the chip's modules that come from the firmware fw
    (a directory with the paks; fw_name its name, the cache's key) rather than the table
    (SocModule.derived). "rpc-table": rpc-table.py --cache (build/rtk/<fw_name>/, reused while
    the library is the same). "mstar-mmap", "mstar-mboot": _mstar_mmap's record IDs and
    mmap_file=, the records derived_files puts into the host share. Error if it cannot say."""
    out = {}
    for m in soc(target).modules:
        if m.derived is None:
            continue
        if m.derived in MSTAR_DERIVED:
            out[m.name] = _mstar_mmap(m, fw, fw_name)[1]
            continue
        if m.derived != "rpc-table":
            raise Error(f"{m.name}: unknown derived parameters {m.derived!r}")
        r = subprocess.run(
            [sys.executable, RPC_TABLE, fw, "--cache", "--name", fw_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if r.returncode != 0:
            raise Error(f"{m.name}.ko's RPC table from {fw}: {r.stderr.strip() or r.returncode}")
        out[m.name] = tuple(r.stdout.split())
    return out


def tee_module(target):
    """The chip's module with the TEE (a SocModule whose parameters have tee=1: rtk.ko's /dev/tee0),
    whose tee_huk= the VM's identity gives (keys.ensure_tee_huk), or None."""
    return next((m.name for m in soc(target).modules if "tee=1" in m.params), None)


# OP-TEE's supplicant: in the rootfs of every firmware whose programs or libraries open /dev/tee%u
# (a libteec, OP-TEE's client), and of no other (the firmware collection, 2026-11-25: every Realtek
# k5lp..k25lp firmware, LM18A, LM21A, LM21AN, LM21ANN and LM21U; none of the M, O, H13, K2/K3
# or E60N ones)
TEE_CLIENT = "usr/bin/tee-supplicant"


def tee_device(target, fw):
    """lgemu.ko's parameters for a firmware fw (a directory with the paks) with OP-TEE's client
    (TEE_CLIENT) on a chip none of whose modules stands in for its TAs (tee_module): ("tee=1",),
    /dev/tee0 with no trusted application, where every session the client opens answers
    TEEC_ERROR_ITEM_NOT_FOUND, as a real set's TEE answers a TA it does not have. Without the
    device the client's TEEC_InitializeContext fails instead, which LM21ANN's legacy-broadcast-dvb
    does not survive (docs/research/lm21ann-xcas-tee-stall.md). () otherwise. Error when the
    rootfs cannot be listed."""
    if tee_module(target) is not None:
        return ()
    try:
        return ("tee=1",) if paths.rootfs_has(fw, TEE_CLIENT) else ()
    except ValueError as e:
        raise Error(f"{name(target)}: whether the firmware has OP-TEE's client: {e}") from None


def card_module(target, broadcast=None):
    """The chip's module with an ARIB set's card device on the board of broadcast (acas=1 among its
    words there: webos_tv's /dev/dvb/adapter0/acas0), whose card_id= the VM's identity gives
    (card_id), or None."""
    return next(
        (
            m.name
            for m in soc(target).modules
            if "acas=1" in board_module_params(target, m.name, broadcast)
        ),
        None,
    )


def card_id(serial):
    """The stub card's 48-bit ID for a TV of serial number serial: 12 hex digits, 0xBE then 40 bits
    of SHA-256 of the serial. Stable while the TV keeps its serial (an identity reset or a clone
    makes a new one, and with it a new card); made here, never read from a card."""
    return "be" + hashlib.sha256(b"beanjar card id\0" + serial.encode()).hexdigest()[:10]


def validate_memory(target, derived=None, vm=None):
    """Check effective module ranges before replacing the launch's host share."""
    derived, vm = derived or {}, vm or {}
    for m in soc(target).modules:
        try:
            words = m.params + derived.get(m.name, ()) + vm.get(m.name, ())
            if (
                m.derived in MSTAR_DERIVED
                and memory.parameters(words).get("mmap_file") != f"/host/{MMAP_SHARE_NAME}"
            ):
                raise ValueError(
                    "mmap_file: expected the prepared firmware-derived host-share file"
                )
            memory.validate(m.name, words)
        except ValueError as e:
            raise Error(f"{name(target)}: {m.name}.ko memory layout: {e}") from None


def _target(target):
    """The target's Target (none of the flags for a target with no target file)."""
    return TARGETS.get(tuple(target)) or Target((), (), None, False, False, False)


# LG's v4l2_vsc names its chip in QUERYCAP's driver by the chip revision's high byte (the O24
# ombre kernel's table; O26's kernels add 0x29)
LG_VSC_SOC = {0x24: "o20", 0x25: "e60", 0x26: "o22", 0x27: "m23", 0x28: "o24", 0x29: "o26"}
# its windows, video30 on: four on O24 and O26 (their kernels register video30 to video33 and
# answer the plane count 4 for these chips), two elsewhere
LG_VSC_WINDOWS = {0x28: 4, 0x29: 4}


def _lg_chip_rev(target):
    """lg115x.ko's chip_rev= of the target's SoC, else None (MStar's LM21)."""
    for m in soc(target).modules:
        if m.name == "lg115x":
            for word in m.params:
                if word.startswith("chip_rev="):
                    return int(word.split("=", 1)[1], 0)
    return None


def _lg_scaler(target):
    """lgemu.ko's V4L2 scaler parameters for the target (Target.lg_scaler), else none."""
    screen = _target(target).lg_scaler
    if not screen:
        return ()
    rev = _lg_chip_rev(target)
    chip = rev >> 8 if rev is not None else None
    name = LG_VSC_SOC.get(chip)
    return (
        f"v4l2_vsc={LG_VSC_WINDOWS.get(chip, 2)}",
        f"vsc_screen={screen[0]},{screen[1]}",
        *((f"vsc_soc={name}",) if name else ()),
    )


# The light sensor's 7-bit address (libdile_i2c's 8-bit slave 0x20) and lgemu's device for each
# value of the board's digitalEye tool option (pqcontroller's DIL_VPQ_SensorCheck: eye_sensor
# takes the first type, rgb_sensor and OTP_eyeSensor the second; off has none).
LIGHT_ADDR = 0x10
LIGHT_TYPES = {"eye_sensor": "light1", "rgb_sensor": "light2", "OTP_eyeSensor": "light2"}
# fancontroller reads /proc/thermalinfo's iddq_core and iddq_scpu from this release on (webOS 23:
# W22O number1's and W24P's), and the plausible currents a set reports there, mA (lgemu iddq=).
IDDQ_RELEASE = "number1"
IDDQ = "150,90"


def light_dev(target, digital_eye):
    """lgemu's i2c_dev entry for the board's ambient light sensor, "light<type>:bus:addr", where
    its tool option digitalEye (lls.digital_eye: the value's name) says the set has one and the
    chip's firmware has the sensor's channel (Soc.light_bus); None otherwise."""
    bus, device = soc(target).light_bus, LIGHT_TYPES.get(digital_eye)
    if bus is None or device is None:
        return None
    return f"{device}:{bus}:0x{LIGHT_ADDR:02x}"


# The ToF motion sensor's 7-bit address (libdile_i2c's TMF882X_SENSOR_N, 8-bit slave 0x82) and
# the values of the board's supportMotionDetectSensor tool option that make msc drive it over I2C
# (MscAdapter::callback_getConfigdData: "ToF_TBD_22Y" initialises DIL_TMF882X; "CAM_ART&R2_21Y"
# and a boolean true the micom-relayed MSENSOR, which is not this chip).
TOF_ADDR = 0x41
TOF_SENSORS = ("ToF_TBD_22Y",)


def tof_dev(target, motion_sensor):
    """lgemu's i2c_dev entry for the board's ToF motion sensor, "tof:bus:addr", where its tool
    option supportMotionDetectSensor (lls.motion_sensor) names the TMF8821 and the chip's firmware
    has its channel (Soc.tof_bus); None otherwise."""
    bus = soc(target).tof_bus
    if bus is None or motion_sensor not in TOF_SENSORS:
        return None
    return f"tof:{bus}:0x{TOF_ADDR:02x}"


# The gaming LED micom's 7-bit address (libdile_i2c's GAMING_LED_MICOM, 8-bit slave 0x12) and the
# tool types whose sets carry it: the 2022 OLED Flex, LX3Q, whose configd profile gamingTV
# (check_product.sh) has ledLighting, which starts lightingcontroller (docs/research/led-lighting.md).
LED_MICOM_ADDR = 0x09
LED_MICOM_TOOL_TYPES = ("LX3Q",)


def led_micom_dev(target, tool_type):
    """lgemu's i2c_dev entry for the board's gaming LED micom, "ledmicom:bus:addr", where its tool
    type (lls.tool_type) is one whose sets carry it and the chip's firmware has its channel
    (Soc.led_micom_bus); None otherwise."""
    bus = soc(target).led_micom_bus
    if bus is None or tool_type not in LED_MICOM_TOOL_TYPES:
        return None
    return f"ledmicom:{bus}:0x{LED_MICOM_ADDR:02x}"


def _thermal(target):
    """lgemu's /proc/thermalinfo parameters for the target (Soc.thermal, IDDQ_RELEASE), else none."""
    if not soc(target).thermal:
        return ()
    rel = target[1]
    iddq = rel in WEBOS_ORDER and WEBOS_ORDER.index(rel) >= WEBOS_ORDER.index(IDDQ_RELEASE)
    return ("thermalinfo=1",) + ((f"iddq={IDDQ}",) if iddq else ())


def _join_i2c_dev(words):
    """lgemu's words with every i2c_dev= list joined into the first one's place: the table's
    devices and those the board's data adds (light_dev), one module parameter."""
    devs = [w[len("i2c_dev=") :] for w in words if w.startswith("i2c_dev=")]
    if len(devs) < 2:
        return tuple(words)
    out, joined = [], False
    for w in words:
        if not w.startswith("i2c_dev="):
            out.append(w)
        elif not joined:
            out.append("i2c_dev=" + ",".join(devs))
            joined = True
    return tuple(out)


def board_module_params(target, module, broadcast=None):
    """The table's parameters of the chip's module on the board of broadcast (None: the chip's
    default): SocModule.params with Soc.broadcast_params' words for that board in place of their
    namesakes."""
    s = soc(target)
    m = next(m for m in s.modules if m.name == module)
    board = broadcast or default_broadcast(target)
    words = list(m.params)
    for b, name_, over in s.broadcast_params:
        if b != board or name_ != module:
            continue
        for w in over:
            key = w.split("=", 1)[0]
            at = [i for i, x in enumerate(words) if x.split("=", 1)[0] == key]
            if at:
                words[at[0]] = w
            else:
                words.append(w)
    return tuple(words)


def module_params(target, derived=None, vm=None, broadcast=None):
    """{module: (words...)}: the parameters boot.conf passes to each guest module (its
    MODULE_PARAMS_<module>), lgemu's included where it has some; derived and vm as boot_conf's
    (derived's lgemu: the board's own devices, such as its light sensor, light_dev), broadcast
    the board's standard (board_module_params). Init adds lgemu's nvm_disk= and partinfo_disk= in
    the guest."""
    s = soc(target)
    derived = derived or {}
    vm = vm or {}
    out = {
        m.name: board_module_params(target, m.name, broadcast)
        + (("de_histogram_proc=1",) if _target(target).de_histogram and m.name == "lg115x" else ())
        + (("v4l2_capture=1",) if v4l2_capture(target) and m.name in CAPTURE_MODULES else ())
        + tuple(derived.get(m.name, ()))
        + tuple(vm.get(m.name, ()))
        for m in s.modules
    }
    lgemu = _join_i2c_dev(
        tuple(s.lgemu or ())
        + tuple(_lg_scaler(target))
        + (("mic=1",) if s.mic else ())
        + _thermal(target)
        + tuple(derived.get("lgemu", ()))
        + tuple(vm.get("lgemu", ()))
    )
    if lgemu:
        out["lgemu"] = lgemu
    return out


def boot_conf(
    target,
    layers,
    derived=None,
    part_nodes=(),
    vm=None,
    fw=None,
    broadcast=None,
    userland="native",
    aarch64=False,
    emmc=None,
    model=None,
):
    """The text of /host/boot.conf, which guest/initramfs/rootfs/init
    sources and carries out: NAME="value" lines, lists space-separated. layers: the layers the
    launcher put into the host share, highest first (without vm); boot.conf lists them bottom to
    top with the per-VM layer vm on top. derived: derived_params(), the firmware's parameters of
    the modules that have some (SocModule.derived), after the table's. part_nodes: [(N, serial)],
    the guest's /dev/mmcblk0pN nodes (qemu.part_nodes, from the VM's partinfo disk). vm: the VM's
    own parameters of a module, {module: (words...)}, after those (the TEE's tee_huk=). fw: the
    firmware directory, whose paks it names (release). broadcast: the board's broadcast standard
    (board_broadcast), which picks the board (board_cmdline), or model, the retail model's
    (board_model). userland: qemu-user adds USERLAND,
    which has init run the 32-bit programs under /host/qemu-arm (docs/dev/architecture/qemu-user-bridge.md);
    aarch64 (the x86-64 guest kernel's) adds USERLAND_AARCH64, the 64-bit ones under /host/qemu-aarch64.
    emmc (firmware.updatable on): (overlay serial, dm-linear table words of the whole-disk
    /dev/mmcblk0 partmap.emmc_table_words, per-partition overlay-node words partmap.emmc_part_words),
    which has init build the whole-disk /dev/mmcblk0 and the overlay-backed /dev/mmcblk0pN nodes the
    updater's crc32info/inactive-bank access needs for the stock updater (bin/beanjar-emmc)."""
    s, r = soc(target), release(target)
    derived = derived or {}
    vm = vm or {}
    params = module_params(target, derived, vm, broadcast)
    missing = [m.name for m in s.modules if m.derived and m.name not in derived]
    if missing:
        raise Error(f"{name(target)}: no firmware parameters for {', '.join(missing)}")
    validate_memory(target, derived, vm)
    root = [p for p in r.paks if p.mount == "/"]
    if len(root) != 1:
        raise ValueError(f"{name(target)}: not one pak at /")
    if r.init_system not in INIT_SYSTEMS:
        raise ValueError(f"{name(target)}: init_system {r.init_system!r}")
    if (r.pmlog_mode is None) != (r.init_system == "sysvinit"):
        raise ValueError(f"{name(target)}: PmLog mode {r.pmlog_mode} with {r.init_system}")
    if (r.pmlog_mode == "preload-systemd") != (r.init_system == "systemd"):
        raise ValueError(f"{name(target)}: PmLog mode {r.pmlog_mode} with {r.init_system}")
    lines = [
        ("TARGET", _words([name(target)]), "the firmware target (chip-release)"),
        (
            "INIT_SYSTEM",
            _words([r.init_system]),
            "the firmware's init: upstart, systemd or sysvinit (SimpleSmart)",
        ),
        *(
            (("INIT", _words([r.init]), "the firmware's PID 1 where it is not /sbin/init"),)
            if r.init != "/sbin/init"
            else ()
        ),
        *(
            (
                (
                    "UNIONS",
                    _words(f"{mp}={':'.join(lowers)}" for mp, lowers in r.unions),
                    "read-only unions in the webOS root, mount point=lower:..., highest first",
                ),
            )
            if r.unions
            else ()
        ),
        (
            "CAP_LAST_CAP",
            _words([str(r.cap_last_cap)]),
            "the firmware's kernel's highest capability: /proc/sys/kernel/cap_last_cap in webOS",
        ),
        (
            "ROOTFS",
            _words([root[0].serial]),
            "the stock rootfs pak's disk serial: the overlay's bottom",
        ),
        (
            "PAKS",
            _words(f"{p.serial}:{p.mount}" for p in r.paks if p.mount not in ("/", None)),
            "the other paks, serial:mount point in the webOS root",
        ),
        (
            "PAK_LOWERS",
            _words(f"{p.serial}:{p.subdir}" for p in r.pak_lowers),
            "lower layers made from a pak, serial:subdirectory, bottom to top, above the rootfs",
        ),
        (
            "LAYERS",
            _words([*reversed(layers), VM_LAYER]),
            'the layers, each the disk with serial layer-<name> (vm: the share "vm"), bottom to top, above those',
        ),
        (
            "STATE_DISKS",
            _words([*(n for n, _ in r.state_disks), *r.emmc_disks]),
            "read-write partitions, by serial",
        ),
        ("NVM_DISKS", _words(n for n, _ in r.nvm_disks), "tvservice's raw NVM disks, by serial"),
        (
            "NVM_EEPROM",
            _words([r.nvm.eeprom] if r.nvm else []),
            "the NVM disk behind lgemu.ko's I2C EEPROM",
        ),
        (
            "PARTINFO_DISK",
            _words(["partinfo"] if board_data(target) else []),
            "the disk with the eMMC partition map, lgemu.ko's /proc/partinfo",
        ),
        (
            "PART_NODES",
            _words(f"{n}:{serial}" for n, serial in part_nodes),
            "the eMMC partitions' nodes, N:serial: /dev/mmcblk0pN is that disk",
        ),
        *(
            (
                (
                    "EMMC_OVERLAY",
                    _words([emmc[0]]),
                    "firmware.updatable: the scratch overlay disk's serial, the dm-linear "
                    "whole-disk /dev/mmcblk0's backing (writes discarded at power-off)",
                ),
                (
                    "EMMC_TABLE",
                    _words(emmc[1]),
                    "firmware.updatable: the dm-linear table of /dev/mmcblk0, start:len:serial:src "
                    "per segment in sectors (bin/beanjar-emmc resolves each serial to its disk)",
                ),
                (
                    "EMMC_PART_NODES",
                    _words(emmc[2] if len(emmc) > 2 else []),
                    "firmware.updatable: the overlay-backed per-partition nodes, N:start:length in "
                    "sectors; bin/beanjar-emmc makes /dev/mmcblk0pN a dm-linear slice of EMMC_OVERLAY "
                    "at start, for the crc32info/inactive-bank partitions that have no backing disk",
                ),
            )
            if emmc
            else ()
        ),
        (
            "PMLOG_MODE",
            _words([r.pmlog_mode] if r.pmlog_mode else []),
            "how PmLogDaemon's whitelist goes for webos.pmlog=full (none: no PmLog)",
        ),
        (
            "PMLOG_JOB",
            _words([r.pmlog_job] if r.pmlog_job else []),
            "PmLogDaemon's Upstart job, or its systemd unit without .service",
        ),
        ("TRUST_BUNDLES", _words(r.trust_bundles), "the CA bundles the proxy's CA is appended to"),
        (
            "TRUST_HASH_DIR",
            _words([r.trust_hash_dir] if r.trust_hash_dir else []),
            "the hashed certificate directory",
        ),
        (
            "BOARD_CMDLINE",
            _words([board_cmdline(target, broadcast, model)]),
            "the bootloader's parameters",
        ),
        (
            "BOARD_IN_KERNEL",
            _words(["1" if r.board_in_kernel else "0"]),
            "1: they are on the kernel's command line already (the launcher's), not appended by init",
        ),
        *(
            (
                (
                    "POINTER_WAKEUP",
                    "1",
                    "send the native M-RCU cursor-on pulse before pointer motion",
                ),
            )
            if s.pointer_wakeup or r.pointer_wakeup
            else ()
        ),
        *(
            (
                (
                    "USERLAND",
                    "qemu-user",
                    "run the 32-bit ARM programs under /host/qemu-arm (binfmt_misc, the compat bridge)",
                ),
            )
            if userland == "qemu-user"
            else ()
        ),
        *(
            (
                (
                    "USERLAND_AARCH64",
                    "qemu-user",
                    "run the 64-bit ARM programs under /host/qemu-aarch64 (binfmt_misc)",
                ),
            )
            if userland == "qemu-user" and aarch64
            else ()
        ),
        ("SOC_MODULES", _words(m.name for m in s.modules), "kernel modules to load after lgemu.ko"),
        (
            "PERIPHERAL_MODULES",
            _words(r.peripherals),
            "the kernel's own modules to load with modprobe: the stock kernel's USB drivers and "
            "filesystems",
        ),
        *(
            (module_params_var(m.name), _words(params[m.name]), f"{m.name}.ko's parameters")
            for m in s.modules
        ),
        *(
            (
                (
                    module_params_var("lgemu"),
                    _words(params["lgemu"]),
                    "lgemu.ko's parameters (init adds nvm_disk=)",
                ),
            )
            if "lgemu" in params
            else ()
        ),
    ]
    out = [
        "# The boot manifest of this launch (host/core/beanjar/targets.py), sourced by guest/initramfs/rootfs/init.\n"
    ]
    for k, v, note in lines:
        out.append(f'# {note}\n{k}="{v}"\n')
    return "".join(out)
