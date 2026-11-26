# Build and boot architecture

This describes how the build produces the shared boot artifacts and per-target layers. For
commands, packages and validation, see the [build guide](../guides/build.md).

## Layout

Repository source is grouped by its role:

| Directory | Contents |
|---|---|
| `host/core`, `host/ui`, `host/web`, `host/viewer` | The Python packages (`beanjar`, the core and CLI; `beanjar_ui`, the GTK windows and their UI resources; `beanjar_web`), and the native viewer code |
| `host/helpers/` | Native host runtime support: media, networking, SSH key conversion, SquashFS inspection and firmware-derived tables |
| `guest/helpers/` | Guest-architecture programs, including the ARM LLS EEPROM probe |
| `guest/kernel`, `guest/initramfs`, `guest/layers` | Guest kernel, early userspace and firmware adaptation layers |
| `guest/tools/` | Build utilities for guest SSH binaries |
| `host/scripts/` | Host VM launcher and helper scripts |
| `guest/scripts/` | Scripts executed inside the VM |
| `scripts/` | Shared build and regression entry points |

MStar mmap and Realtek RPC table readers belong to `host/helpers/`: launches use them
to prepare firmware-derived inputs. Their analysis origins do not make them
optional research tools. SDP provisioning and capture tools remain development
utilities and are excluded from the distribution. The ARM LLS probe belongs to
`guest/helpers/` even though host-side EEPROM preparation invokes it through
qemu-arm. Execution architecture determines this
boundary: native host executables install in `libexec`, guest executables in
`share/beanjar/guest`. Installed runtime paths are independent of these checkout
directories.

The top-level `meson.build` sets up what the modules share (options, the firmware target and its
facts, the host build's firmware directory, the guest's glibc ceiling and staging command, the list of what is published) and enters one
directory per module with `subdir()`. For a webOS guest build (`meson.is_cross_build()`), it enters
`guest/tools/ssh`, `guest/layers/soc/<chip>` after its bases (the chip's table: `hal_stock`, `gst_plugin_dir`,
`soc_phys_base`, `gl_vendor_stubs`; and its `rootfs/`), `guest/layers/webos/src/pmlogd`,
`guest/layers/webos/<release>` after its bases (the release's table, its `rootfs/` and files),
`guest/layers/webos` (the webos layer of those), `guest/layers/common` (entering `hal-common` and `tvservice`; before the soc layer, whose shims link its `hal_common`, compiled with the chip's
`soc_phys_base`), `guest/layers/soc` (the soc layer: `guest/layers/soc/src`, one directory per library the
chip's `hal_stock` names and `video`, shared by every chip), `guest/layers/{gl,headless,rootssh,devmode,hbchannel}` (`guest/layers/gl` enters
`guest/layers/gl/mesa`, the Mesa stack),
`kernel` (entering `guest/kernel/modules/{lgemu,mstar-utopia,lg115x,rtk,btmtk_usb,webos_tv}`),
`initramfs` (entering `guest/initramfs/busybox` and `guest/initramfs/src`), and the test programs `tests/{tcg,devmode,virgl,gm}`
and `guest/layers/gl/test`. SimpleSmart instead enters `guest/layers/simplesmart`, `kernel` and
`initramfs`; NetCast enters `guest/layers/gl/mesa`, `guest/layers/netcast45`, `kernel` and `initramfs`.
On the host build it enters `host/viewer`, `host/helpers/kvm`, `host/helpers/media-helper`,
`host/ui` (the GTK windows' layouts),
`host/scripts` (the firmware cache), `host/helpers/network` (the port relay and the LAN helper) and the unit tests
`tests/{netstub,securestore,input,viewer,manager}`. `meson/meson.build` holds the helpers: the build's scripts
(the Python ones share `meson/common.py`: stamps written only when they change, depfiles, locks, tree hashes),
the source trees of the wraps and, in the host build, the prebuilt programs (`vendor`, `vendor_deps`). Layer directories without a `meson.build` (normal)
are used as they are; a `guest/layers/<name>` with one is only the source of a generated layer, and the
launcher never takes it for the layer itself.

The webOS layers (see [firmware targets](firmware-targets.md)):

| Source | Layer | Holds |
|---|---|---|
| `guest/layers/common` | `common`, always | tvservice's private view of the OS (`libtvservice-private.so`), the jail trust hook (`libjail-trust.so`) and sam's preload of it (`sam.override` or a `sam.service` drop-in; the gl layer's copy adds `libjail-gpu.so`), `pulseaudio.override`, `PMLOG_DEBUG`'s `pmlog-debug` (an Upstart job and a systemd unit), `emulator-settings`, the automatic power-off timers and Audio Guidance off once per state and concern (an Upstart job and a systemd unit); `hal_common`, which the shims link |
| `guest/layers/soc/src` + `guest/layers/soc/<chip>` | `soc`, always | the HAL and DILE shims under the chip's stock sonames, the media elements under their stock names, the kernel-driver and GPU job overrides, `board.cmdline` |
| `guest/layers/webos/<release>` | `webos`, always | the release's job overrides and fstabs (state formatter, mounts, `lginit_app`, jail nodes, securitymanager), the PmLog preload (`libpmlog-keep-all.so` from `guest/layers/webos/src/pmlogd`, afro and beehive) or devlog job (`emulator-pmlog-devlog.conf`, dreadlocks), tvservice's and tvwin's jobs and tvwin's LS2 service files |
| `guest/layers/normal` | optional, source-only | as it is |
| `guest/layers/{gl,headless,rootssh,devmode,hbchannel}`, `tests/regress/tcg` | optional, generated per target | `gl` takes the chip's GPU vendor names (`gl_vendor_stubs`, `gl_vendor_gl`) and whether the release needs the Wayland compat library (`wayland_server_compat`); a chip without vendor names gets no gl layer and no `tests/gl` programs; `hbchannel` is built once per HBC release of `hbchannel_versions` (`hbchannel@<version>`, published as `build/components/hbchannel/versions/<version>`), each with the release's capture of a real install of it, as `guest/layers/hbchannel/versions/<version>/releases` names it ([firmware targets](firmware-targets.md#homebrew-channel-hbchannel)) |

guest/initramfs/rootfs/init stacks them webos over soc over common, under the optional ones.
Legacy targets instead have their own always-used `simplesmart` or `netcast45` layer.

### Layer bases

A chip's or release's directory may name its base, another directory of the same axis, in a file
`base` (one line: `h13`). The top-level `meson.build` follows the chain and enters the base's
`meson.build` first, then the directory's own: a table value the directory does not set is the
base's, and the layer gets the base's `rootfs/` and files first and the directory's after them
(stage.py: a later file replaces an earlier one). So a directory holds only what differs. Each
`meson.build` adds its own tree, `soc_layer_trees += ['--tree', meson.current_source_dir() /
'rootfs', '.']` or `webos_layer_args += [...]` (with the release's `--file`s); `guest/layers/soc` and
`guest/layers/webos` make the layer of them. There is no removing a base's file: a directory that wants
fewer of them does not name that base. `scripts/regress` follows `base` too when it reads a chip's
`gl_vendor_*`; `board.cmdline`, which the host reads from `guest/layers/soc/<chip>/rootfs`, is every
chip's own.

| Directory | Base |
|---|---|
| `guest/layers/soc/k7lp` | `k6lp` |
| `guest/layers/soc/m14` | `h13` |
| `guest/layers/webos/dreadlocks2` | `dreadlocks` |
| `guest/layers/webos/goldilocks2` | `goldilocks` |
| `guest/layers/webos/kisscurl` | `jhericurl` |

The release files of the target table (`guest/targets/release/`) also reuse the preceding release
through `base` ([the target table](firmware-targets.md#the-target-table)); that is separate from
source-layer inheritance.

### Choosing the shims

Which shims a target's soc layer gets is decided by what its firmware contains, not by the release's
name, so a release newer than the last one studied gets the shims its consumers need without a
new rule. The build reads no firmware: each rule asks whether the target's rootfs image has a path
(`fw_path`) or whether a stock library defines a symbol (`fw_defines`), answered by the target's checked-in firmware facts, `guest/targets/facts/<chip>-<release>.txt`
([Firmware facts](../guides/build.md#commands); `guest/targets/meson.build`). `host/scripts/vm-launcher
firmware-facts` writes the answers from the image's own listing (`paths.rootfs_entries`, which runs
`unsquashfs -ls` on `rootfs.pak` or `super.pak`'s logical rootfs), never from an extracted `rootfs/`,
so a partial extraction cannot change a selection. `guest/layers/soc/meson.build` then applies:

- A chip's `hal_stock` names the shims its stock libraries may need. One whose stock file is not
  in the rootfs is not built (a configure message names it); a `bsppart` entry is not checked.
  `libhalgal` is served from the `libdile_gal` shim's engine where the rootfs has the stock
  `libdile_gal`. Without one, it carries its own copy of the engine only where bsppart's
  `libkadaptor.so` defines `RHAL_SE_Init` (the fact `defines bsppart:bsp/usr/lib/libkadaptor.so:RHAL_SE_Init`):
  the Realtek KADP whose `KADP_GAL_Init` opens the SE 2D engine the emulator lacks, or where the
  chip's table sets `halgal_stock_fails` (LG's O22 family and MStar's LM21, whose stock
  `HAL_GAL_CreateSurface` fails here; the question is not asked there). Elsewhere the stock HAL
  stays.
- `libsestore`: the stock `libsestore.so.0.1.0` and a direct reader, the gateway's
  `ssl-cert-getter.js` (its TLS key) or `nyxSecureStore.module` (nyx crypto's model secret).
  On a chip whose table sets `sestore_model_items_nyx` (O22 and its successors), a nyx
  reader also turns on the regional model items (`sestore_model_items`).
- O22's `libkadaptor`: on every O22-family firmware, Widevine's `OEMCrypto_Initialize` answered
  `INIT_FAILED` when KADP's TEE client finds no TEE; and its secure clock wherever the rootfs has
  `nyxDrm.module`.

Where only the release can decide, a rule applies from a release on, in LG's order
(`targets.WEBOS_ORDER`, `vm-launcher config RELEASES`): `webos_reached` holds the releases up to and
including the build's, and a rule tests `'queue' in webos_reached`. O22's media element set
(ponytail's, then queue's) and O22N2's Weston13 handling (spacebuns: no Qt bootstrap tree; queue's
media elements) are such rules. Chip-keyed tables (a chip's directory and its `base`) are unchanged.

## One place for each setting

- `meson/cross/webos-sdk.ini`: the webOS and SimpleSmart SDK (`[constants] sdk`, default
  `/opt/arm-webos-linux-gnueabi_sdk-buildroot`) and the 32-bit ARM flags (`-march=armv7-a
  -mfloat-abi=softfp -mfpu=neon`), which reach that guest build's compiles: Meson's own, and through the
  guest build the Mesa stack's inner builds (a cross file generated from the guest compilers and
  `c_args`, `guest/layers/gl/mesa/cross.ini.in`), the SSH autotools builds and libffi. An SDK elsewhere
  is a second cross file that only redefines the constant, given after the first:
  `meson setup --cross-file meson/cross/webos-sdk.ini --cross-file my-sdk.ini build/guest-h13-afro`, with
  `my-sdk.ini` holding `[constants]` and `sdk = '/path/to/sdk'` (Meson reads all the files before
  it evaluates any section, so the later value wins everywhere).
- `meson/cross/lgtv-sdk.ini`: the NetCast SDK and its ARMv7-A softfp flags.
  Configure NetCast explicitly with this file; `scripts/build.sh` does not choose it for a
  fresh build directory. The [build commands](../guides/build.md#commands) show the setup.
- `host/core/beanjar/paths.py`: where the host runtime (`host/scripts/vm-launcher`, the VM manager) finds the
  checkout's files and the build's outputs, and the firmware name and directory (`FW`:
  `<collection>/<name>` in the first of the host's firmware collections that has it,
  `firmware_dirs` in `~/.config/beanjar/config.toml`, default `/srv/beanjar/firmware`), for the
  launcher and as the host build's default (`host/scripts/vm-launcher config FW`, which the host build
  and the shell tools run). `FW=` in the environment overrides it (a directory, or a firmware name
  looked up in the same collections), and `-Dfirmware=DIR` overrides it for the host build's
  firmware cache. The guest build takes no firmware: its target's facts are in the tree
  (`guest/targets/facts/`). `host/scripts/vm-launcher config FW --target <chip>-<release>`
  (`Tree.target_firmware`: the default firmware if it resolves to the target, else the one firmware in
  those collections that does) gives a target's firmware, which `firmware-facts` reads by default.
- `meson.build`: warnings (`warning_level=2`, `werror=true`; the one exception is the video plugins' GStreamer
  headers), `-O2`, `c_std=gnu99` for the guest, and the glibc ceiling (the release's: 2.14.1 on
  afro, 2.19 on beehive, dreadlocks, dreadlocks2 and SimpleSmart, 2.24 on goldilocks and
  goldilocks2, 2.28 on jhericurl and kisscurl, and 2.16 on NetCast):
  every ELF in a layer, `build/ssh/bin` and the published test programs is checked with
  `readelf -V` and the build fails on a newer `GLIBC_` version. Guest code is compiled with
  `-U_FILE_OFFSET_BITS` (Meson defines it to 64): it shares structures with the firmware's
  libraries, built with 32-bit `off_t`.
- `meson.options`: `publish_dir`, `firmware` (the host build's), `soc` and `webos` (the [firmware target](firmware-targets.md), which a guest build needs),
  `arm64_cross_compile` (default
  `aarch64-linux-gnu-`), `uapi_headers`, `firmware_cache` (auto: when the firmware's paks are
  there), `mitmproxy`, `virtcam`, `kernel_debug`. `meson configure build/host -Dmitmproxy=disabled`
  changes one. The prebuilt QEMU, FFmpeg and neatvnc are not options: every host build has them
  (below, "Downloads"). The viewer has no FFmpeg dependency and shows its video-window pattern
  when no decoded picture is available.

The launcher starts one `media-helper` beside each VM. `host/helpers/media-helper/meson.build`
builds it with the `host/media/decode/` engine on the private FFmpeg; `host/viewer/meson.build` links the
presentation code and shared decoded-IPC helpers only. The VM's `hwdec` setting
(`MEDIA_HWDEC=auto|none|TYPE`) selects the helper's initial hardware preference; `none` locks it to
software (`--software-only`). `MEDIAHUB=0` omits the helper and turns decoding off: the
launch writes `media = "off"` to `vm-info.toml`, and the viewer cores then show the video windows'
checkerboard (as with `--no-media`); the viewer cannot decode compressed QEMU sockets directly. See
[the video architecture](video.md#transport-and-session-ownership).

## Downloads

QEMU, FFmpeg and neatvnc (with aml) are not built here: they are beanjar-vendor's prebuilt
tarballs, pinned by release tag and SHA-256 in `packaging/vendor.toml`. The host build's
`meson/vendor.py` downloads the tarballs for its architecture into `build/vendor/download`,
refuses one whose hash differs, unpacks each into `build/vendor` and publishes the trees as
`build/qemu`, `build/ffmpeg`, `build/ffmpeg-helper` (the media helper's own FFmpeg) and
`build/neatvnc` (packaging/debian/README.md, "The prebuilt programs"). The published executable is `build/qemu/bin/qemu-system-aarch64`; the launcher runs it
whenever it is published, and the system QEMU otherwise (`paths.Tree.qemu`).

Every source tarball is a `[wrap-file]` in `subprojects/` with its `source_hash` (sha256), and
Meson's own wrap handling fetches it: `meson subprojects download` puts the tarball in Meson's
package cache, `subprojects/packagecache` (or `$MESON_PACKAGE_CACHE_DIR`), checks it, unpacks it
into `subprojects/<directory>` (one tree for every build directory), and applies the wrap's
patches. Patches are `diff_files` of the wraps, listed in order; `subprojects/packagefiles/*` are
links to `guest/kernel/patches` and `guest/layers/gl/patches/{mesa,wayland}`, so a new patch
is a new file there plus a line in the wrap. The trees of packages that do not build with Meson
(Linux, busybox, Dropbear, OpenSSH, libffi, GStreamer, usbipdcpp) get `patch_directory = source-only`, a
`meson.build` that only marks them as unpacked for Meson (it is never configured; Meson refuses a
subproject tree without one).

`meson subprojects download` leaves a tree that exists alone, Meson only warns about a changed
wrap for subprojects it configures, `meson subprojects update --reset` fails on a missing tree, and
nothing in Meson locks a download against another build doing the same. So each tree has a `fetch-<name>` target (`meson/fetch.py`):
it records a key of the wrap and its patch files in the tree and, when they change, deletes the
tree and has Meson download it again; its depfile names the wrap and the patches. Editing, adding
or removing a patch therefore rebuilds what uses the tree at the next `meson compile`, from a
pristine unpack: a tree is never patched in place, and edits made directly in it are lost at the
next change. Each wrap is locked exclusively while this runs (`subprojects/.<name>.lock`), so
builds of several targets at once wait for each other and find the key current; the key is written
last. A tree is fetched only when something that is built needs it (the host build on the
Pi fetches none).

One pinned input is not a source tarball: Homebrew Channel's release IPKs (ar archives, which
Meson's wrap handling cannot unpack). `guest/layers/hbchannel/versions/<version>/release.env` holds
each one's URL and sha256; `build-layer.sh` downloads it once into the same package cache, as
`<sha256>-<file name>` (a release candidate and its release share a file name), and refuses it if
the hash differs.

None of them is configured as a Meson subproject. They are built by their own build systems from
custom targets, for these reasons:

- **Linux, busybox:** Kbuild, and arm64 (below); Meson's `external_project` needs a configure
  script, and these start from `make defconfig`.
- **Dropbear, OpenSSH, libffi:** autotools; `guest/tools/ssh/build-ssh.sh` and
  `guest/layers/gl/mesa/mesa-stack.sh` build them out of tree. `external_project` would configure
  at setup time, allow one build per source tree (Dropbear is built twice), assume another host
  triple, always install and not strip.
- **usbipdcpp** (the host's virtual camera): CMake, run by
  `host/helpers/virtcam/build-usbipdcpp.sh`. Meson's CMake module compiles a CMake project's
  targets with Meson's own arguments, which gives other libraries than its `Release` build
  (different flags and `RUNPATH`, the build machine's `-I/usr/include` in the arm64 cross build).
- **Wayland, libdrm, Mesa:** a cross build of Wayland needs a native `wayland-scanner` of exactly
  its own version, which a subproject of a cross build cannot build; on webOS 1 and 2 Mesa must link
  `libwayland-server-compat` ahead of Wayland's server library (a Meson target of ours, which
  runs on the firmware's 1.0.3 or 1.1; webOS 3's 1.7 needs none), and the layer's `libwayland-client` is a relink of Wayland's
  objects with a version script. `guest/layers/gl/mesa/mesa-stack.sh` runs libffi, libdrm, a native
  scanner, Wayland, wayland-protocols and Mesa as separate builds into one staged prefix,
  `build/mesa/<key>/stage/usr`, which is also what the GL test programs compile against. The
  targets share it: [Incremental builds](../guides/build.md#incremental-builds) says how it is keyed.

## Host and guest compatibility

The host (`beanjar`) and the guest payload (`beanjar-guest`, or a checkout's `build/`) are built
and updated separately: the Pi builds its host and keeps the PC-built guest. Version numbers are
not trusted to say whether the two work together; the launcher checks what the payload actually
has (`beanjar/compat.py`):

- **Components.** At VM create and start, before LLS, disk creation, EEPROM or key
  synchronisation, or any helper starts (`launch.py`, `newvm.py`), the payload must have every
  always-used component (`soc`, `webos`, `common`, ...) of the firmware's target. An `.epk` import
  checks once its target is known, before keys are made.
- **Module parameters.** Before boot, every parameter the host passes to a guest module (boot.conf's
  `MODULE_PARAMS_*`, `targets.module_params`: `mic=`, `cec=`, the scaler and capture settings, the
  firmware-derived ones, `tee_huk=`) must be declared in that module's `.ko` (`parm=` or
  `parmtype=` in its `.modinfo`, read with pyelftools). A missing one refuses the start, naming the
  module, the parameter and the payload's `.ko`. The declared names are cached by the `.ko`'s
  path, size and mtime (`modinfo-params.json` in the host's cache: `build/` or
  `~/.cache/beanjar/`). Init's own `nvm_disk=` and `partinfo_disk=` are the guest's business.
- **Protocols.** The wire formats identify themselves (`WOSCUR1`, `WOSVID3`, the media helper's
  HELLO/ACK) and stay the guard between viewer and VM; the viewer has no version check.

The versions are information. Each build lane stamps what it publishes (the host lane
`build/source-version.toml`, each guest lane `build/guest-installation.toml`, from
`packaging/VERSION`; the packages carry them in `/usr/lib/beanjar/installation.toml` and
`/usr/share/beanjar/guest/installation.toml`). The launch copies the host's and the guest's into
`run/vm-info.toml`'s `[beanjar]` table (`host_version`, `guest_version`; `unknown` when a stamp is
missing or unreadable) for logs and messages; its `version` key stays the webOS release. The
packages' dependency on a guest of the same `0.Y` is a hint for apt, not a runtime check.

The stamps' commits say where skew explains a failure, without refusing anything by them.
`beanjar --version --json` reports the host's and the guest's version and commit (`compat.host_identity`,
`compat.guest_identity`: the installation.toml files, or for a checkout the commit it holds); a
module parameter refusal whose `beanjar` and guest payload come from different commits names both
and the command that installs or builds the matching guest; and a command forwarded to another
machine warns when that machine's `beanjar` or guest is another commit's (the manager's "Several
machines").

VM directories carry no format or version marker. A VM made by an older Beanjar starts as it is;
if a layout ever changes, the code recognises the old layout by its files at that time. Each start
makes only the disposable boot files afresh (the run's shares, `boot.conf`); EEPROM, keys and disks
are never changed because a version did.

## Two guest architectures

Meson has one cross machine per build directory. The guest's is the 32-bit ARM webOS userland
(the SDK). The arm64 pieces are few and are not Meson code: the kernel (with its module
lgemu.ko) and busybox are built by their own Kbuild with `CROSS_COMPILE=<arm64_cross_compile>`
(`meson/kbuild.sh`, as custom targets), and the initramfs's two input bridges
(`guest/initramfs/src/virtio-{pointer,ir}.c`) are one static `aarch64-linux-gnu-gcc` command each,
with the same warnings and a depfile. A third build directory would add a setup step and a second
cross file for two single-file programs.

## Guest kernel

Linux 6.18.53 (`subprojects/linux.wrap`, patches in `guest/kernel/patches`), configured by
`guest/kernel/configs/webos_virt_defconfig`: a `savedefconfig` file, so it lists only what differs from
Kconfig's defaults (68 options set, 135 default options turned off). `meson/kbuild.sh` copies it
to `arch/arm64/configs/` of the object tree (`build/kernel/<key>/linux-obj`, shared by every target's
build directory: [Incremental builds](../guides/build.md#incremental-builds)), where Kbuild's
`%_defconfig` target finds it, and runs `make webos_virt_defconfig`, then `make Image`. The
`.config` is regenerated only when the defconfig differs from that copy or there is no `.config`;
otherwise make keeps it, and its own `syncconfig` follows a changed source tree.

The build stamp is fixed (`KBUILD_BUILD_TIMESTAMP` 1970-01-01 00:00:00 UTC, `KBUILD_BUILD_USER` and
`_HOST` beanjar, `KBUILD_BUILD_VERSION` 1; the banner reads `beanjar@beanjar ... #1 SMP PREEMPT
1970-01-01 00:00:00 UTC`), and with it the mtime of the built-in default initramfs and the build
id: every target's build makes the same Image byte for byte, so `build/Image` is the same whichever
target built last.

To change an option:

```sh
cd build/kernel/*/linux-obj                      # the one key there is, without kernel_debug
make ARCH=arm64 menuconfig                      # or edit .config, then make ARCH=arm64 olddefconfig
make ARCH=arm64 savedefconfig                   # writes ./defconfig
cp defconfig ../../../../../guest/kernel/configs/webos_virt_defconfig
```

`meson compile -C build/guest-h13-afro` then rebuilds the Image (Kbuild picks up the changed `.config`
itself; a changed defconfig in the repository regenerates it). An edit of `.config` that is not
copied back survives only until the defconfig changes.

The patches (`guest/kernel/patches`, in the wrap's order): `0001` KEY_MAX 0x4ff (LG's input key range),
`0002` virtio-snd's `pcm_device_base` (the PCM as hw:0,6), `0003` LG's `/sys/power/mode`, always
"cold": every LG webOS kernel has it (LG's GPL trees, WT1H to W22H, `guest/kernel/power/main.c`, the
snapshot boot's "making"/"resume"/"cold"), and W19P's securitymanager checks its partitions'
signatures at start unless it reads "cold" or "making", then powers the set off ("verificationFail")
when bspfw, a partition the VM has not, fails. It is in the kernel because a module cannot add a
file to `/sys/power` (`power_kobj` is not exported). `0004` keeps the virtio-pci devices running across
suspend-to-idle (below, `SUSPEND`): their suspend and resume callbacks no longer reset them. `0005` shows `GPIO_SYSFS`'s prompt without
`EXPERT` (below, `GPIOLIB`): `EXPERT` would change far more than it shows, `NAMESPACES` and
`DEBUG_MEMORY_INIT` turning off by default and `DEBUG_KERNEL` selected, with `FTRACE`, `DEBUG_MISC` and
`RCU_TRACE` on by default. `0006` lets a character device have a major up to 4095 (`CHRDEV_MAJOR_MAX`), as
LG's 3.10 and 4.4 kernels do: LG's log manager `/dev/lg/logm` is major 1810 in every LG115x
firmware's device_table (lg115x.ko's `logm_obj`), and since 4.13 Linux refuses majors above 511.
`0007` lets a driver back a window of `/dev/mem` (`linux/devmem_window.h`): an mmap inside it gets
fresh pages the driver fills at the mmap, where the machine has nothing at that physical address
(lg115x.ko's SP804 timer page at 0xfd100000, `timer_hz`, which W15M's bootd reads).
`0010` virtio-snd's `pcm_alias_device` (the launcher sets 0): a second ALSA PCM, `hw:0,0`, on the
card's one playback stream, one opener at a time; alsa-lib's name hints test device 0, and
without an output hint Chromium 68 and 79 (W20L, W21P) play WebAudio nowhere.
`0014` virtio-gpu's `modifiers` parameter: with `virtio_gpu.modifiers=1` its planes list the
linear modifier (an `IN_FORMATS` property, `DRM_CAP_ADDFB2_MODIFIERS`) and ADDFB2 accepts it, the
scanout being linear with the client's stride; upstream leaves modifiers off. LG's stock Qt EGLFS
Starfish integration reads `IN_FORMATS` without a check, so only firmwares whose Qt compositor uses
it get the word: the soc layer's Starfish setup (`guest/layers/soc/o22/meson.build`, chosen by the
firmware's facts) carries it in `usr/share/beanjar/kernel.cmdline`, which the launcher reads from
the guest layers (`share.kernel_args`); every other target sees no modifiers, as before.
`0020` virtio-gpu's `tv_connector` parameter: with `virtio_gpu.tv_connector=1` the first
scanout's connector is a TV connector (TV-1) instead of a Virtual one. Queue's stock Weston names its
primary output and fb0 after it, and copies every connector into a second head named with index 2,
so the copies need names of their own. The launcher sets it for queue's two outputs, and spacebuns'
gl layer for its one (below). QEMU enables the second output only when a UI gives it a size: the
dbus display's setup does (768x2160), egl-headless does not, so there the launcher also forces the
second connector on in the guest, `video=Virtual-1:768x2160e`.
`0021` starts gpiolib's dynamic bases at 1024 instead of 512 (below, `GPIOLIB`): the vendor
module's chip at base 0 serves the firmware's own line numbers, K24's up to 733, and the PL061,
the one chip with a dynamic base, must sit above them.
`0022` gives virtio-gpu's CRTCs DRM's vblank timer (`drm_vblank_helper`, as vkms has): a commit's
event is armed at the flush and sent at the next vblank of the CRTC's mode, after the host's
answer as before, and blocking commits wait for it ([The display's refresh](#the-displays-refresh)).
`0028` virtio-gpu's `overlay_plane` parameter: with `virtio_gpu.overlay_plane=1` each CRTC also has
an overlay plane, with the primary's formats, that may be positioned and clipped. A set's lg1kdrm
has one (primary, overlay, cursor on one CRTC), and spacebuns' Weston puts its second output, TV-2,
on it; without it LSMW's popup window gets no controllers and the compositor faults. A virtio
scanout composes nothing, so the overlay is scanned out only while the primary has no framebuffer
(LG's exclusive output path), at its visible source size, its position not kept. Spacebuns' gl
layer carries the word with `tv_connector=1` (`guest/layers/gl/native-tv-rootfs`); no other target
has it.

**How it was derived** (2026-09-28): `allnoconfig` with the options below forced
(`make allnoconfig KCONFIG_ALLCONFIG=<list>`), booted and compared with the arm64 `defconfig`
kernel it replaced (dmesg, PmLog, Upstart job states), then `savedefconfig`. Options Kconfig
turns on by itself (arch timer, GIC v2/v3 and v2m, PSCI, `MULTIUSER`, futex, epoll, signalfd,
timerfd, eventfd, shmem, `FILE_LOCKING`, `PROC_FS`, `SYSFS`, `VT`, KALLSYMS, ELF core dumps) are not
listed.

| Group | Options | Why |
|---|---|---|
| Platform | `PCI`, `PCI_HOST_GENERIC`, `SERIAL_AMBA_PL011(_CONSOLE)`, `RTC_DRV_PL031`, `RTC_HCTOSYS`, `RTC_INTF_DEV` | QEMU `virt`: ECAM PCI for every virtio device, the ttyAMA0 debug console, the RTC that sets the clock at boot (TLS to LG needs the date) and `/dev/rtc0` (udev's rtc rule, hwclock) |
| | `NO_HZ_IDLE`, `HIGH_RES_TIMERS`, `PREEMPT`, `NR_CPUS=32` | idle vCPUs that do not tick; the tick and preemption model of the kernel it replaced |
| | `ARM64_VA_BITS_48` | 4-level page tables instead of 52-bit VA's 5 levels on TCG's `max` CPU |
| | `PARAVIRT`, `ARM64_USE_LSE_ATOMICS`, `ARM64_HW_AFDBM` | KVM steal time; LSE atomics and hardware access/dirty flags, which the Pi's A76 and TCG's `max` have |
| | `ARM64_ERRATUM_3194386`, `ARM64_ERRATUM_4118414` | the errata that list the Pi 5's Cortex-A76 r4p1 (the other A76 errata stop at r3p1) |
| 32-bit userland | `COMPAT`, `COMPAT_32BIT_TIME`, `BINFMT_ELF`, `BINFMT_SCRIPT` | the ARMv7 softfp userland at EL0 with its 32-bit `time_t` syscalls; `#!` scripts (VFP/NEON for AArch32 needs no option on arm64) |
| | `KUSER_HELPERS`, `COMPAT_ALIGNMENT_FIXUPS`, `ARMV8_DEPRECATED`, `SWP_EMULATION`, `CP15_BARRIER_EMULATION`, `SETEND_EMULATION` | glibc 2.14 and old-gcc atomics use the kuser helpers at 0xffff0fa0; unaligned LDM/LDRD fix-ups (`alignment.conf`); old ARMv6/7 instructions |
| Boot, modules | `BLK_DEV_INITRD`, `RD_GZIP`, `PRINTK_TIME`, `MODULES`, `MODULE_UNLOAD` | the gzip initramfs; timestamps for `tests/regress/boot/timeline.py`; `lgemu.ko` (below, "Kernel module lgemu"), which can be unloaded and reloaded |
| Processes, IPC | `SYSVIPC`, `POSIX_MQUEUE` | LG's daemons (gm's `mqgm_gmDaemonMq`, tvservice's 31 queues; `sysctl.conf` sets `fs.mqueue.msg_max`) |
| | `NAMESPACES`, `UTS_NS`, `IPC_NS`, `PID_NS` | `webos-start`: Upstart as PID 1 of new pid/uts/ipc/mount namespaces (mount namespaces need no option). No user or net namespace: nothing in the firmware (LG's kernel had none) or the initramfs uses them |
| | `CGROUPS`, `CPUSETS`, `CPUSETS_V1` | activitymanager's `/sys/fs/cgroup/cpuset` focused/unfocused groups; `cgroups.conf` mounts one v1 hierarchy per controller in `/proc/cgroups`, whatever they are, and nothing else in the firmware opens a controller file |
| | `INOTIFY_USER`, `UNIX98_PTYS`, `MAGIC_SYSRQ` | inotify (Qt, GIO, snapshot-boot); ptys for ssh; `sysctl.conf` sets `kernel.sysrq`, and the `sysctl` job fails without it |
| | `SUSPEND` (Kconfig's default, no longer turned off) | `PM_SLEEP` and with it `/sys/power/pm_freeze_timeout`, which webOS 4's `mount-readwrite` writes last (`echo 7000`): without it the job ends with status 1 and never emits `mount-readwrite-done` (W18R's `criu_remove_img`, `iepg-integrity`, `broadcast-conf-downloader`). The least of the two ways to `PM_SLEEP` (the other, `HIBERNATE_CALLBACKS`, has no prompt: hibernation, which needs swap). It brings `PM`, `FREEZER` and, through `PM_SLEEP_SMP`, `HOTPLUG_CPU`. `/sys/power/state` offers `freeze mem`, both suspend-to-idle (no PSCI `SYSTEM_SUSPEND` under QEMU; the launcher's `mem_sleep_default=s2idle` says so): Quick Start+'s suspend-to-RAM (docs/dev/architecture/suspend.md; lgemu.ko's `/dev/lgib`, WT1H's and W15M's sleepd with `echo mem`, `rtcwake`). The VM keeps running and the vCPUs idle; the micom's wake line (`GPIO_PL061`, below) or the PL031 alarm ends it. Patch `0004`: virtio-pci's suspend and resume callbacks leave the devices alone, as for PCI PM's No_Soft_Reset, which QEMU sets only behind a PCIe port: a reset lost the 9p shares and the GPU (no restore in their drivers) |
| | `DEVMEM`, `STRICT_DEVMEM` | `/dev/mem` exists on the TV: `WebAppMgr-set-permissions` chgrps it (tvservice's own opens go to its private view of the OS, guest/layers/common/tvservice) |
| virtio | `VIRTIO_PCI`, `VIRTIO_BLK`, `VIRTIO_NET`, `VIRTIO_CONSOLE`, `HW_RANDOM_VIRTIO`, `VIRTIO_INPUT` | the paks and state disks; the NIC; hvc0 and the `webos.*` virtserial ports; entropy; keyboard and tablet |
| | `DRM`, `DRM_VIRTIO_GPU`, `DRM_VIRTIO_GPU_KMS`, `DRM_FBDEV_EMULATION`, `FB_DEVICE` | virgl and KMS scanout (dma-buf and sync_file come with DRM); `/dev/fb0`, which LunaSysService reads for the screen size and tv.capture's `GdCaptureScreen` reads |
| | `SOUND`, `SND`, `SND_VIRTIO` | the virtio-sound card (`pcm_device_base`, `pcm_alias_device`, guest/kernel/patches) |
| | `NET_9P`, `NET_9P_VIRTIO`, `9P_FS` | the VM's shares, `/host`, `/host/out` and `/run/vm` (no POSIX ACLs: mounted `access=client`) |
| GPIO | `GPIOLIB`, `GPIO_SYSFS` (patch `0005`; `GPIO_SYSFS_LEGACY`, `GPIO_CDEV`, `GPIO_CDEV_V1` and `OF_GPIO` come with them), `GPIO_PL061` | QEMU virt's PL061 carries its power button (DT `/gpio-keys/poweroff`, GPIO 3, pulsed by QMP `system_powerdown`): lgemu.ko's micom takes it as its wake line, a wakeup interrupt (no `KEYBOARD_GPIO`, so webOS sees no power-button input); the chip gets a dynamic base (1024, patch `0021`). W19P's (and W18R's) libdile_gpio drives its pins through `/sys/class/gpio`: `export`, then `gpio<N>/direction` and `value`, by the kernel's global numbers. lgemu.ko's chip (`lgemu_gpiochip_add`) serves them at base 0, registered by rtk.ko on k5lp and mstar-utopia.ko on m3; gpiolib warns once that a static base is deprecated (`gpiochip0: Static allocation ...`), and dynamic bases start at 1024 (patch `0021`; upstream 512), above every number a firmware uses (the highest, K24's HDMI detects, 733). No chip on the other targets, where `export` exists and every write fails (`EINVAL`) |
| Input | `INPUT_EVDEV`, `INPUT_MISC`, `INPUT_UINPUT` | libim reads evdev; the initramfs's pointer bridge, tvservice (the remote, `LGE RCU`), lginput2 and libim create uinput devices, with key bits up to 0x4ff (KEY_MAX, guest/kernel/patches; LG's kernels have 0x500) |
| | `HID`, `HIDRAW`, `HID_GENERIC` (`HID_SUPPORT`; `I2C_HID` off) | the `hidraw` class, built in: the native jails of ombre and later mount `/sys/class/hidraw` (`jail_native*.conf`), and the jailer gives up when the source is missing, so O22's browser never started. The transports (`USB_HID`, `UHID`) are modules ("Peripheral modules", below); the VM's own input stays virtio |
| Filesystems | `DEVTMPFS`, `TMPFS`, `TMPFS_XATTR` | `/dev`; the overlay's tmpfs upper needs xattrs for whiteouts and opaque directories |
| | `EXT4_FS`, `BLK_DEV_WRITE_MOUNTED` | the state disks (formatted by the firmware's mkfs.ext4); the second is Kconfig's default, which `allnoconfig` turns off (writes to a mounted block device refused) |
| | `SQUASHFS`, `SQUASHFS_LZO`, `SQUASHFS_XATTR`, `SQUASHFS_FILE_DIRECT`, `SQUASHFS_COMPILE_DECOMP_MULTI_PERCPU` | the six paks are LZO (compressor id 3, 256 KiB blocks) and carry xattr tables; so are the `ROOTFS=uncompressed` copies (same compressor id) |
| | `OVERLAY_FS`, `BLK_DEV_LOOP` | `/webos`; loop devices as on the TV (LG's kernel has them and the firmware ships losetup, though no boot or test here uses one) |
| | `ZRAM` (`ZSMALLOC`), `ZRAM_BACKEND_LZ4`, `ZRAM_BACKEND_LZO` | `/dev/zram0`, built in as in every LG kernel (`modules.builtin`'s `zram.ko`, never a module): W15L's webOS lite `memchute` job sets it up itself (`lzo`, 400 MiB, `mkswap`, `swapon`, no guard) and fails without it, so memchuted never ran; the other releases' `swapon`, `swap-on` and `zram-on` jobs (lz4 first, else lzo) skip it when it is missing and now take it as on the TV. The default compressor is Kconfig's `lzo-rle`; the jobs set theirs. The old per-file statistics the 3.10 memchuted reads (`orig_data_size`, `compr_data_size`, `mem_used_total`) are gone from current zram (`mm_stat`), so those lines of its log come out empty |
| USB | `USB`, `USB_XHCI_HCD` (`USB_SUPPORT`, `USB_PCI` and `USB_XHCI_PCI` come with them) | the TV's USB host, the launcher's `qemu-xhci`: `/sys/bus/usb`, which webOS 5's and 6's native jails mount (`jail_native_builtin.conf`'s `mount ro /sys/bus/usb`; without it the jailer cannot build the browser's jail on W20L and W21P). The class drivers are modules ("Peripheral modules", below). LG's kernels drive their ports with EHCI, never xHCI; what in the firmware names the stock controller's path is under "Peripheral modules". The ports have power while the board's VBUS switches are on (`gpio_usb_vbus`, the GPIO paragraph of the SoC modules below). Patch `0019`: no uevent for a SCSI power-on unit attention, which QEMU's stick reports and LG's 4.4 kernels never send (webOS's `60-block.rules` would turn it into a disk change that drops PDM's `READ_ONLY`) |
| Peripherals (`=m`) | `USB_HID` (`HID_PID`), `UHID`, `INPUT_JOYDEV`, `JOYSTICK_XPAD` (`_FF`), `HID_ACRUX`, `HID_DRAGONRISE`, `HID_LOGITECH`, `HID_MICROSOFT`, `HID_PANTHERLORD`, `HID_SONY`, `HID_PLAYSTATION`, `HID_ZEROPLUS` (each with its `_FF`; `LEDS_CLASS`, `LEDS_CLASS_MULTICOLOR`), `MEDIA_SUPPORT`, `MEDIA_CAMERA_SUPPORT`, `MEDIA_USB_SUPPORT`, `USB_VIDEO_CLASS` (`_INPUT_EVDEV`), `SCSI`, `BLK_DEV_SD`, `USB_STORAGE`, `SND_USB_AUDIO`, `USB_ACM`, `USB_SERIAL` (`_GENERIC`; `_PL2303`, `_OPTION`, `_SIERRAWIRELESS`, `_IPW`, `_FTDI_SIO`, `_CH341`, `_CP210X`, `_WWAN`), `FUSE_FS`, `VFAT_FS`, `EXFAT_FS`, `NTFS3_FS` (without ntfs3's legacy `ntfs`, which is lgfs's), `NLS_CODEPAGE_437`, `NLS_CODEPAGE_949`, `NLS_ASCII`, `NLS_ISO8859_1`, `NLS_UTF8` | the USB class drivers and filesystems LG's own kernels build (their decoded configs and `modules.builtin`), for the peripherals the stock userland handles: PDM's classes (HID, gamepad, storage, video, sound, CDC, whose USB serial check needs `/sys/bus/usb-serial/drivers/pl2303`; its PTP and MTP mounts are fuse), lginput2's HID gamepads (hidraw and uhid), camera2's UVC camera, audiod's USB headset and microphone. Not `HID_APPLE` and `HID_CHICONY`, which `USB_HID` brings by default and no LG kernel has. Loaded per release line, "Peripheral modules" below |
| Network | `NET`, `UNIX`, `INET`, `IP_MULTICAST`, `PACKET`, `IPV6` (Kconfig's default, no longer turned off) | multicast for SSDP/DIAL; connman's DHCP client uses packet sockets. IPv6 as W20L's own kernel has it (its config: `IPV6`, `IPV6_SIT`, `IPV6_NDISC_NODETYPE`; here `IPV6_SIT` is a module, `sit.ko`, which only the releases whose stock kernel has SIT load, "Peripheral modules" below; WT1H's 3.10 had no IPv6): W20L's mdnsd exits without an `AF_INET6` socket. Sockets and link-local only: no NET mode gives the guest IPv6 connectivity (`NET=direct`'s slirp has `ipv6=off`, `NET=mitm`'s slirp4netns runs without `--enable-ipv6`, `NET=off` has the link down), so no router advertisement and no global route. `Image` 9,144,328 to 9,615,368 bytes; Pi KVM, `normal,headless`, h13-afro, three boots each interleaved: `core-boot-done` 19.5 s with and without (one 14.5 s outlier with). No netfilter: the firmware's iptables and connman ran without it on the defconfig kernel too (its netfilter was all modules, never loaded) |

Left out on purpose: the I2C HID transport, the class drivers LG's kernels have that the stock userland has no class for (`USB_HIDDEV`, which nothing in the firmware opens; W22A's CD-ROM `BLK_DEV_SR`; PS/2 keyboards and mice, `INPUT_KEYBOARD`/`INPUT_MOUSE` and `INPUT_MOUSEDEV`, no consumer of `/dev/input/mice`), the StanbyME's own `HID_MULTITOUCH` (its touch panel is virtio here), virtio-fs (nothing mounts it), netfilter,
bridge/VLAN, wireless and Bluetooth (no such devices), ACPI and EFI (QEMU loads the Image
directly with a device tree), SVE/SME/pointer authentication/MTE/BTI (the 32-bit userland cannot use them), seccomp,
audit and LSMs, NUMA, CPU frequency scaling (CPU hotplug comes with `SUSPEND`, above), debug info.

### The display's refresh

A TV's display engine completes a page flip at the panel's vblank, and webOS draws its UI at
60 frames a second. virtio-gpu has no vblank: without `0022` a commit's event went out as soon as
the plane update's resource flush came back from the host, so the compositor drew as fast as the
host answered (above 60 on a fast host), and the mode it chose was QEMU's EDID's, 1920x1080 at
75 Hz (QEMU generates its EDID at 75 Hz unless a display reports another refresh, and neither
D-Bus front end does). Now:

- **The mode is 60 Hz.** The stock driver's preferred mode without an EDID is its own CVT mode
  at the device's size and 60 Hz (the other modes, `drm_add_modes_noedid`'s, are 60 Hz too), so
  the launcher gives virtio-gpu-gl `edid=off`. That is not enough with vhost-user-gpu ([the GPU
  process](gpu-process.md)): there the EDID feature is the backend's, and on the Pi the guest got
  QEMU's 75 Hz EDID with `vhost-user-gpu-pci,...,edid=off` on QEMU's command line. So `0022` also makes that CVT 60 Hz mode the preferred one when there is an
  EDID; the EDID's modes stay, none preferred. In the layers, the shims and the stock firmwares
  looked at (W22P 04.60.90's and W22O 04.40.93's rootfs) nothing reads the connector's EDID: the
  only EDID strings there are the HDMI inputs' (`eim`, `libhdmi-conn-protocol`, `librim_qsp`), and
  no library reads a DRM property blob but libdrm itself.
- **Flips complete at its vblank** (`0022`): every KMS client is paced by it, legacy page flips
  (egl-kms, the starfish QPA) and atomic commits (Qt EGLFS Starfish, Weston, the native scanout)
  alike, and none completes more than 60 a second however fast the host answers. A flip still
  waits for the host's answer to its flush first, so a slow host gives fewer. The UI is timed by
  this 60 Hz vblank of its own whatever the panel's refresh.

Measured 2026-11-12 with a probe of our own (`DRM_IOCTL_WAIT_VBLANK`, and a dumb-buffer page
flip loop as DRM master with the compositor stopped), and the UI's frames as beanjar-vnc's core
counts them (QEMU's updates) while the home screen's app row was navigated with RIGHT and LEFT
every 0.25 s, 15 s:

| | preferred mode | vblanks a second | flip loop, flips a second | UI navigated, frames a second |
|---|---|---|---|---|
| Pi 5, KVM, W22O 04.40.93 (`o22-mullet`, vhost-user-gpu), before | 1920x1080 at 75 | none (`WAIT_VBLANK`: not supported) | 157.1 | 63.7 |
| the same, after | 1920x1080 at 60 | 60.1 | (not run) | 57.6, 57.2 |
| Pi 5, KVM, W22P 04.60.90 (`k8lp-mullet`), after | 1920x1080 at 60 | | | 57.6 |
| x86 PC, TCG, W22P (virtio-gpu-gl), before | 1920x1080 at 75 | | | 29.5 |
| the same, after | 1920x1080 at 60 | 59.7 | 60.0 | 28.4 |
| x86 PC, TCG, W22O, after | 1920x1080 at 60 | 60.2 | 60.1 | |

On TCG the UI's rate is the guest's CPU's, before and after; on the Pi it went above 60 before.

The panel the UI is shown on may be faster: the viewer and beanjar-vnc compose at the board's
panel refresh, whatever it is (120, 144 Hz...), with the video at its own rate and the UI at 60
on its own vblank
([viewer](viewer.md#output-rate), [beanjar-vnc](vnc.md#frame-rate)).

### Peripheral modules

The USB class drivers and the filesystems of USB storage are the kernel's own modules (`=m`),
installed by `meson/kbuild.sh` with `make modules_install` (stripped; the host's `depmod` writes
`modules.dep`, `modules.alias` and `modules.builtin`) and carried in the initramfs as
`/lib/modules/<release>/` (a `tree` line in `guest/initramfs/initramfs.list`). `/init` loads them
before the firmware starts with busybox `modprobe -a` (its applet reads `modules.dep`, so each
module's dependencies come with it: `usb-storage` brings `scsi_mod`, `uvcvideo` `videodev` and
`videobuf2`, `snd-usb-audio` `snd-hwdep` and `snd-rawmidi`), the list being boot.conf's
`PERIPHERAL_MODULES`. The firmware never sees the tree: it runs chrooted in `/webos`, whose
`/lib/modules` is its own kernel's, so its udev's module autoloading and any `modprobe` of its own
find nothing for our release, and everything it would load is loaded already. The out-of-tree
modules that load among them (`lgfs.ko`, below) go into the same tree, in `extra/`, with depmod run
again over it (`kbuild.sh tree`; `guest/kernel/meson.build`, `peripheral_modules`).

Which modules a VM loads is its release's (`peripherals` of its file in `guest/targets/release/`),
what that firmware's stock kernel has, built in or as a module:

| Release | Stock kernel | Loaded | Evidence |
|---|---|---|---|
| afro (WT1H) | 3.10 | usbhid, the gamepad HID drivers (ACRUX, DragonRise, Logitech, PantherLord, Sony, Zeroplus), xpad, uvcvideo, usb-storage, vfat, ntfs, fuse, snd-usb-audio, cdc-acm, USB serial pl2303, option, ftdi_sio (no sierra), lgfs; no uhid, joydev or hid-microsoft | decoded config |
| beehive (W15M), dreadlocks2 (W17H) | 3.16, 4.4 | as afro with uhid and sierra (W17H also input-leds) | decoded configs |
| dreadlocks (W16R), goldilocks (W18R) | 3.10, 4.4 | usbhid, uhid, joydev, HID DragonRise, Logitech, Sony, Zeroplus, xpad, uvcvideo, usb-storage, vfat, ntfs, fuse, snd-usb-audio (a stock module), cdc-acm, USB serial pl2303, option, sierra, ipw, lgfs (W18R also input-leds) | `modules.builtin`, module files |
| goldilocks2 (W19P) to number1 (webOS 23) | 4.4, 5.4 | all of the above with hid-microsoft and exFAT; USB serial pl2303, option, sierra, with ipw on the lm21 chips and ftdi_sio on o22; lgfs | decoded configs (W21A, N21D, W22A), `modules.builtin` and module files of the others |
| ombre (webOS 24) to queue (webOS 26), spacebuns | 5.4, 6.12 | as webOS 23 (lgfs for `ntfs`); queue's 6.12 kernels add ftdi_sio, ch341 and cp210x | `modules.builtin`; spacebuns (W24G 02.68.80, no module tree in its rootfs) as W24G's webOS 24 |
| simplesmart (SS1A) | 3.10 | uvcvideo, usb-storage, cdc-acm, the storage filesystems, USB serial pl2303, option, sierra, lgfs | kernel strings (no HID, xpad or USB audio); its RELEASE insmods lgli.pak's tfat.ko and tntfs.ko |
| netcast45 (NC5U) | 3.10 | vfat and lgfs only | its kernel pak is encrypted; LG's published config builds the class drivers as modules (none ships in its paks), FAT in and no USB serial; lgli.pak ships tfat.ko and tntfs.ko |

`sit` (IPv6-in-IPv4, `IPV6_SIT=m`) is not a peripheral but is loaded the same way: it creates `sit0`, the fallback tunnel device that a kernel with SIT has from the start (`/proc/net/dev`). Every webOS release from beehive on has SIT built in (`modules.builtin` of 37 firmwares' rootfs, W15M 2.2 to W22H 43.22.73, none ships a `sit.ko`), so each loads it; afro's WT1H has no IPv6 at all, and the NetCast line (simplesmart, netcast45) does not load it: there `networkmanager`, a Wi-Fi daemon with no Wi-Fi interface, takes the first device that is not `lo`, `eth0` or `ppp0` for its Wi-Fi interface, and its private ioctl on `sit0` overruns a stack buffer ([research note](../../research/netcast-networkmanager-wifi-country.md); `sit0` cannot be removed, it is `init_net`'s fallback device, and `ipv6.disable=1` does not stop `sit` creating it).

USB serial is the converters and modems of each stock kernel (`usbserial` and `usb_wwan` come with
them). Besides any real converter, PDM needs it from webOS 5 on: its CDC handler lists
`/sys/bus/usb-serial/drivers/pl2303` for every device event without handling an error, and on
webOS 22 the exception drops the event before the HID, sound and storage handlers see it, so every
device list stayed empty. W15M's `zte_ev` is part of `option` in Linux 6.18.

`lgfs.ko` (`guest/kernel/modules/lgfs`) registers LG's (Tuxera's) filesystem names, `tfat` on
vfat, `tntfs` on ntfs3 read-only and, for webOS 24 on, `ntfs` on ntfs3 read-write, and accepts and
drops the options of theirs that PDM passes (`fastmount`, `max_prealloc_size`; tntfs's and ntfs's
`nls` is ntfs3's `iocharset`). PDM probes the
boot sector and mounts FAT as `tfat` and NTFS as `tntfs`, with
`iocharset=utf8,fastmount=1,max_prealloc_size=32m,uid=0,gid=5000,umask=0002` and
`nls=utf8,max_prealloc_size=64m,uid=0,gid=5000,umask=0002`. On webOS 1 to 4.5 it retries a failed
mount as `vfat` or `ntfs` (`PDM_DoReMount`); from webOS 5 on it retries only read-only, after
`EACCES` or `EROFS`, so without `tfat` no USB stick mounted on webOS 6 or 22 (gdb). webOS 23's
PDM has `tntfs` in its mount table and no `tfat` (its `modules.dep` lists `tntfs.ko` only); webOS 24
on mounts `vfat`, `ntfs` and `exfat`, NTFS with `uid=0,gid=5000,umask=0002,nls=utf8` and no fallback
: its kernel's built-in `ntfs` is LG's read-write driver on fs/ntfs (`ntfs: driver 3.8.03
[Flags: R/W]` in W24P 33.20.23, with file create and delete), whose charset option is `nls`, which
ntfs3's legacy `ntfs` rejects. The guest kernel is built with `NTFS3_FS` and not `NTFS_FS`, so the
name `ntfs` is lgfs's on every line that loads it, read-write as the stock webOS 24 driver. exFAT is `exfat` on every line (no `texfat`). The firmware's
own `tfat.ko` and `tntfs.ko` (WT1H and W15M in the rootfs, the others in `modules.dep` and a pak,
loaded by PDM or its plugin with `insmod /mnt/lg/res/lglib/...`) are 32-bit ARM modules for LG's
kernels and cannot load here.

Each type's `init_fs_context` is its backend's, with the backend's context operations copied once
and `parse_param` wrapped, so the superblock is vfat's or ntfs3's in everything but `s_type`:
`/proc/mounts` says `tfat` as on the TV (PDM reports it as the drive's `fsType`; asm and the media
apps compare against `tfat`), statfs, unmount and every file operation are the backend's. lgfs
keeps `SB_RDONLY` on tntfs in `get_tree` and `reconfigure`, as it came up on ntfs3's former legacy
`ntfs`. The backend is looked up at the first mount and held until lgfs unloads; a mounted
type holds lgfs. It is a module of its own rather than part of lgemu: it loads per release line
with the peripheral modules, after vfat and ntfs3, through modprobe, where lgemu is the SoC
emulation every target insmods. Verified on the PC (2026-11-05, QMP hot-plug on `qemu-xhci`): N21D
webOS 6 and W22A webOS 22 mount a FAT32 stick as `tfat` (`MOUNT_OK`, the "browse the file from the
connected device" popup, a file written by the guest read back from the image after unplug), N21D
an NTFS stick as read-only `tntfs`; W22A lists the USB keyboard (HID), USB audio (SOUND) and the
internal `apps` drive.

The guest kernel is Linux 6.18, so some drivers are not the stock ones. The DualShock 4 is
`hid-playstation`'s since Linux 6.2 (`hid-sony` keeps the PS3 pads): a release whose `hid-sony` had
it (Linux 3.15 on: W15M's 3.16 and later, not WT1H's or W16R's 3.10) loads `hid-playstation`. It
names the pad from its USB product string, as `hid-sony` did, but gives it the kernel's gamepad
layout (`BTN_SOUTH`...) and adds "<name> Motion Sensors" and "<name> Touchpad" input devices, where
LG's 4.4 `hid-sony` had one device with the touchpad on it; lginput2 reads such a pad through
hidraw and is not affected. NTFS is ntfs3, as `ntfs3`, and as lgfs's `tntfs` and `ntfs`; the kernel
leaves out ntfs3's read-only legacy `ntfs` (`NTFS_FS`), whose name lgfs takes. Filesystems mount with the
kernel's default `iocharset` (iso8859-1), so `nls_iso8859-1` goes with `vfat` everywhere.

LG's kernels drive the USB ports with EHCI (`EHCI_HCD`, no `XHCI_HCD` in any decoded config); the
VM's host is `qemu-xhci`, so device paths differ: audiod's `86-audiod.rules` (webOS 5 on) sends
`usb-headset-inserted` and `usb-mic-inserted` only for a sound card under the stock controller's
`/devices/platform/soc/3f980000.usb/usb*/1-1/1-1.*/...` path, which no card here has.

Measured (2026-11-05): `Image` 10,219,528 to 10,285,064 bytes, the initramfs 4,413,311 to 5,543,932
bytes (49 modules, 3.8 MB unpacked); `modprobe -a` of WT1H's 20 modules took 0.16 s of the boot
on the PC's TCG (dmesg, from the last SoC module to the end of the modprobe).
With USB serial and lgfs: `Image` unchanged (10,285,064 bytes), the initramfs 5,673,206
bytes (59 modules, 4.5 MB unpacked).

## Kernel module lgemu

`guest/kernel/modules/lgemu` is `lgemu.ko`, an out-of-tree module with the interfaces of LG's own kernel
that the stock firmware uses and mainline Linux does not have. It is built by the kernel's Kbuild
as an external module (`meson/kbuild.sh module`: `make M=guest/kernel/modules/lgemu
MO=build/kernel/<key>/lgemu/obj modules` against `build/kernel/<key>/linux-obj`), after the
`kernel` target, whose `make Image modules` leaves the `Module.symvers` modpost checks the
module's symbols against. So its vermagic always matches the Image. A changed kernel config rebuilds it from clean (`kbuild.sh` compares the
object tree's `autoconf.h` with the one it was built with): Kbuild's own CONFIG_ dependencies of an
`M=`/`MO=` build are paths relative to `MO`, so it never sees one (a `struct device` that grew with
`PM_SLEEP` left lgemu.ko registering nameless I2C adapters, -EINVAL). It needs no option beyond
`MODULES` and `MODULE_UNLOAD` (eventfd, sysfs and shrinkers come with the kernel's defaults).

The MStar, LG115x and Realtek drivers use private subsystem translation units
behind module-local headers. Their Kbuild object lists retain the same published
module identities; splitting ownership introduces no new guest modules or public
ABI. Source-attribution checks use the unit owning each counter or subsystem.
Host census and memory suites cover report provenance and preparation consistency;
they do not establish device behavior or replace live firmware checks.


Its `tee` part (`tee.c`, `lgemu-tee.h`) is `/dev/tee0` with OP-TEE's user ABI, which the firmwares'
libteec speaks. A SoC module that stands in for trusted applications adds it with them (rtk.ko's
`tee=1`, the secure-store TA); lgemu.ko's own `tee=1` adds it with none, so that every session
answers TEEC_ERROR_ITEM_NOT_FOUND, as a real set's TEE answers a TA it does not have. Prepare gives
lgemu.ko `tee=1` (`targets.tee_device`) where the firmware's rootfs has OP-TEE's supplicant,
`usr/bin/tee-supplicant`, and no chip module has TAs: LM18A, LM21A, LM21AN, LM21ANN and LM21U.
Without a device the client's `TEEC_InitializeContext` fails, which LM21ANN's legacy-broadcast-dvb
does not survive (`docs/research/lm21ann-xcas-tee-stall.md`).

The initramfs carries it as `/lib/modules/lgemu.ko`, and `/init` loads it as soon as it has read
`/host/boot.conf`; a failure is logged (`[init] lgemu.ko not loaded: ...`) and the boot goes on,
without the interfaces. After it `/init` loads the modules boot.conf's `SOC_MODULES` names (the chip
table's, `guest/targets/soc/`), each from `/lib/modules/<name>.ko` with the
parameters in `MODULE_PARAMS_<name>` (`-` as `_`): the SoC vendor's kernel drivers, emulated, with
the chip's values as parameters: `guest/kernel/modules/mstar-utopia` (`mstar-utopia.ko`, MStar's drivers under its Utopia userland, for M2) and
`guest/kernel/modules/lg115x` (`lg115x.ko`, LG's own kdrivers, for H13 and M14: "lg115x.ko" there) and
`guest/kernel/modules/rtk` (`rtk.ko`, Realtek's drivers under its KADP, for K5LP: "Implemented: W19P
headless" there; its RPC answers are the firmware's own, read at each start by
`host/helpers/rtk/rpc-table.py`; with `tee=1` also the stand-in TAs of `tee-ta.c` behind lgemu.ko's
`/dev/tee0`: "Implemented: the Realtek TEE"), each built, carried and published (`build/<name>.ko`) as lgemu.ko is;
and on M3 and K5LP `guest/kernel/modules/btmtk_usb` (`btmtk_usb.ko`, MediaTek's Bluetooth driver with no
adapter: its netlink family, `family`, 30 on W18R and 29 on W19P, which webos-bluetooth-service opens)
and `guest/kernel/modules/webos_tv` (`webos_tv.ko`, LG's WDVB DVBv5 tuner driver with no signal: the `wdvb` platform device's frontend sysfs and `/dev/dvb/adapter0/frontend0`, its counts in `/proc/webos_tv/props`). The first three call lgemu.ko's I2C devices
(below, "i2c"): they are built after it, with its `Module.symvers` (`kbuild.sh module ...
SYMVERS`, `KBUILD_EXTRA_SYMBOLS`) and its `lgemu-i2c.h`, and depend on it (`modinfo`: `depends:
lgemu`). They share the rest of their mechanics through it too (`drv.c`, `lgemu-drv.h`; exports
only, no part of its own): the lazy page store by address that their mmaps fault in (each mapping
holds the owning module), the counted-ioctl tables behind their `/proc` lists (first-seen log of
an unknown ioctl, mstar-utopia's `trace_cmd`), the parsing of the chip table's `a:b:c` values and
the character devices at a vendor's major, and the GPIO chip behind `/sys/class/gpio`
(`lgemu_gpiochip_add`: base 0, input levels and output latches; rtk's, mstar-utopia's and lg115x's
`gpio_lines`/`gpio_level`), whose `gpio_usb_vbus` outputs are the USB ports' power switches
(`lgemu_gpiochip_usb_vbus`: the root hubs stay without their hub driver until PDM turns one on); `lgemu_i2c_user_xfer` (`i2c.c`) is the register
access their I2C ioctls make (lg115x's iic, mstar-utopia's HWI2C, rtk's UCOM). It is also published as `build/lgemu.ko` (and synced to the Pi), for a reload from the
initramfs shell: `host/scripts/vm-launcher put build/lgemu.ko` on the host, then `rmmod <the SoC module>;
rmmod lgemu; insmod /host/build/lgemu.ko nvm_disk=...` and the SoC module again with its
parameters (boot.conf's). `core.c` starts the parts in order and stops them in reverse; each part
is one file with an init and an exit function.

### partinfo

`partinfo.c` is LG's `/proc/partinfo`. `partinfo_disk=`, which `/init`
sets to the disk boot.conf's `PARTINFO_DISK` names (the VM's `partinfo.img`, a state disk the
launcher makes from `beanjar/partmap.py`'s image of the firmware's map), is read once at load: the
first 0x1850 bytes, LG's `struct partmap_info` (`block/partitions/lgemmc.c`). With the magic
0x20120716 the part creates `/proc/partinfo` with `proc_partinfo_show`'s text (a header, then
`part%d: %8.8llx %8.8llx "%s"` for every entry but the last); without it there is no file, as on
LG's kernel. The `/dev/mmcblk0pN` nodes are `/init`'s: block nodes of the disks boot.conf's
`PART_NODES` (N:serial) names, which the launcher reads from the VM's own `partinfo.img`.

### low_mem_notify

`/sys/kernel/mm/low_mem_notify` is LG's low-memory notifier (`CONFIG_LOW_MEM_NOTIFY`,
`mm/low-mem-notify.c`, derived from a Chromium OS notifier). Its one user in the firmware is LG's
memchuted. Sources:

- LG's GPL kernel for the LG115x (H13) chips, `linux-3.10-lg115x.tgz` in `kdriver.tar.bz2` of LG's
  webOS 1.x open-source package (`webOS_AM_CO_FI_1.zip`, 2014; unpacked in
  `build/ref/lge-kernel-lg115x/`). Its driver has free_pages, usable_pages, level, thresholds,
  event_ctrl, reset and workingset.
- The WT1H firmware's own kernel (`3.10.19-32.afro.6`, built 2022), read from its code (kallsyms):
  the version the firmware was built against, which adds xreclaimable, ratio, zramswap and force
  and changes the usable-memory formula. The module follows this one, except that `workingset`
  is written in bytes (`memparse`, 0 refused; read back in pages), as in every later LG GPL
  kernel: webOS 3 (`kernel-DM2`, `mm/low-mem-notify.c`), webOS 3.5 (`linux-4.4-lg115x`), webOS
  4 (`kernel-404-glacier`, whose built `drivers/staging/webos/low-mem-notify.c` replaces the
  unbuilt `mm/` copy that still takes pages), webOS 5, webOS 6 and webOS 22
  (`drivers/staging/webos/`). Every firmware that writes it writes `80M`: W15's `memchute` job
  (W15M's and W15U's only in their unused webOS lite branch), W16R's, and the `memchute-ready`
  job or script of W17 and later. WT1H writes none, so the 2014 source's pages never mattered.

| File | Mode | Read | Write |
|---|---|---|---|
| `free_pages` | 0444 | max(free − reserved + free swap, file − shmem − swap cache), pages | |
| `usable_pages` | 0444 | usable memory (below), pages | |
| `level` | 0444 | index of the current threshold, `%u` (so −1 reads 4294967295); empty with no thresholds | |
| `thresholds` | 0444 | the thresholds in pages, ascending, `*` before the current one | |
| `event_ctrl` | 0644 | number of thresholds | `<eventfd> <threshold>`: register one |
| `reset` | 0644 | empty | anything: drop every threshold |
| `workingset` | 0644 | pages | bytes, K/M/G suffixes, not 0 (default 30720 pages, 120 MiB) |
| `xreclaimable` | 0644 | bytes | bytes, K/M/G suffixes (default 0) |
| `ratio` | 0644 | 0 or 1 | a number, non-zero sets it (default 1) |
| `zramswap` | 0644 | 0 or 1 | a number, non-zero sets it (default 0) |
| `efs_swap` | 0644 | 0 or 1 | stock swap task selects ordinary flash-backed mode (default 0) |
| `force` | 0644 | empty | anything: signal the current threshold, then re-evaluate |

Usable memory, in pages, clamped at 0:

```
free - reserved + free swap - swap cache - (zramswap && !efs_swap ? free swap / 3 : 0)
  + file pages + reclaimable slab + xreclaimable - workingset - shmem
```

(`reserved` is the kernel's totalreserve_pages.) A threshold is in pages, or with `ratio` set a
percentage of total memory; a negative one is total memory. The thresholds are kept sorted, and
the current one is the highest below usable memory (−1 below all of them). When usable memory
crosses a threshold, in either direction, that threshold's eventfd is signalled, one signal per
threshold crossed; registering signals the crossings of the thresholds already there first.
Nothing is signalled while usable memory stays between two thresholds. LG evaluates on every page
allocation (a hook in `__alloc_pages_nodemask`); a module cannot hook the allocator, so lgemu
evaluates every `poll_ms` (a module parameter, 200 ms) and each time memory reclaim asks its
shrinker for a count, which is when memory is actually short. The differences from LG's driver:
a change of `level` also wakes `poll()` on it (`sysfs_notify`, from the periodic work);
the eventfd references are released on `reset` and unload (LG's leaked them); `force` below every
threshold signals nothing (LG's read before the table); `xreclaimable` accepts 0.

The job `memchute` writes `reset`, `ratio` 0 and `zramswap` 1, then memchuted registers the ten
numbers of `/etc/memchute.conf` (`enter=0;5956;20847;59563;-1`, `leave=0;2978;11913;26803;74453`;
the second 0 is skipped), which gives nine thresholds: 0 2978 5956 11913 20847 26803 59563 74453
and total memory. It reads `level` and names it `(level + 1) / 2`: 4 Normal above 74453 pages usable
(290.8 MiB), 3 Medium above 26803 (104.7 MiB), 2 Low above 11913 (46.5 MiB), 1 Critical above 2978
(11.6 MiB), 0 Reboot. With `ratio` 0 the thresholds are absolute pages, and the pages are 4 KiB on
the TV and in the guest, so nothing is scaled for the VM's RAM: the same free memory means the
same level as on a TV, and a 1.5 or 2 GiB guest sits at Normal (level 7) when idle. The
`workingset` default is LG's, the firmware's own resident code, which does not depend on RAM
either. Total memory includes active swap through the kernel's `si_swapinfo()` accounting;
patch `0012` exports that existing function for lgemu. In ordinary EFS mode, the zram
compression-pool discount does not apply even if `memchute-ready` writes `zramswap=1`.

### Ordinary flash swap

The kernel enables `SWAP`, `MD` and `BLK_DEV_DM`. Systemd releases from jhericurl onward
attach the firmware's `efs` partition as a VM state disk. Its size comes from the firmware
partition map; W21A provides 600 MiB. Older releases retain their existing disk layout.

The initramfs adds a oneshot `beanjar-eswap.service`, wanted by `sysinit.target` but ordered only
after `mount-readwrite`, which runs `beanjar-eswap` once per boot, early and beside the rest of
the boot: it creates an upstream device-mapper linear mapping at `/dev/mapper/eswap`. Drop-ins make `efs-on`, `f2io-on` and `swap-on` require it and order after it,
so their later starts (memchute starts `swap-on` inside its 40 s watchdog, and `swap-on` starts
`efs-on`) find it active and run nothing of ours. `USE_EFS_SWAP=YES` selects the stock EFS path.
The unmodified firmware scripts then format and activate swap and set `low_mem_notify/efs_swap`;
Beanjar does not call `swapon` itself.

This mapping offers ordinary disk-backed swap, without LG's compression or application
hibernation API. The helper sets the firmware's `hib.disable` flag in VM-owned persistent
storage, so stock scripts omit the unsupported `apphib` mapping. No real TV is changed.

Run `tests/regress/swap/check-eswap.sh` inside webOS after `rest-boot-done` to check stock activation.
Its optional `--pressure` workload allocates tmpfs pages beyond available RAM, verifies real
swap-out counters and data checksums, and removes its temporary allocation on exit.

### i2c

`i2c.c` models the board's I2C devices the firmware needs answered, once for every SoC: the micom
(power-on mode, power state, the event poll with the remote's keys, power off, restart and the
power-off timer), the NVM EEPROM (32 KiB) and the speaker amplifier (writes acknowledged). It
exports `lgemu_i2c_read`/`_write` (`lgemu-i2c.h`); the bus drivers call them for the (bus, slave)
targets their `i2c` parameter places the devices at (the chip table, `guest/targets/soc/`): lg115x.ko's
`/dev/lg/iic<n>` and mstar-utopia.ko's HWI2C. Its parameter `nvm_disk` is the EEPROM's block
device, which `/init` gives it (the state disk boot.conf's `NVM_EEPROM` names); the launcher has
programmed a new one (`host/core/beanjar/nvm.py`). The remote's IR reaches the micom through
`/dev/lgemu-ir`, which the initramfs's pump `virtio-ir` (`guest/initramfs/src/virtio-ir.c`) feeds from the
`webos.ir` port. The micom's power commands end webOS's PID namespace as `reboot(2)` in it does,
which `guest/initramfs/rootfs/bin/webos-start` turns into the VM's power-off or reset. Quick Start+'s side (docs/dev/architecture/suspend.md):
the micom's warm standby (`26 07`, `25 02`; active again on `25 03` or the wake), its wake line
(QEMU virt's PL061 power button, a wakeup interrupt that ends suspend-to-idle; QMP
`system_powerdown` presses it) and the standby state the pump reads back from `/dev/lgemu-ir` and
shows the host as its `webos.standby` port held open.

### gfx

`gfx.c` is the graphics memory behind `/dev/gfx` (`include/webos-gfx.h`): one shmem object of
`LGEMU_GFX_SIZE` bytes (192 MiB of memory and 64 KiB of display engine state), allocated as pages
are written, which every shared mapping of `lgemu_gfx_fops` maps, whichever node it was opened
through; the soc layer's DILE_GAL (`guest/layers/soc/src/libdile_gal/gfx-memory.c`) maps it in gm, every
GM client, the compositor and tvservice, and gives pages back with `MADV_REMOVE`. The node is the
SoC module's, where the vendor's driver puts it: lg115x.ko's LG gfx driver at 151:0 (the firmware's
device_table makes `/dev/lg/gfx0`, its `mknod` job links `/dev/gfx` to it), rtk.ko's Realtek
`gal_gfx` (class and region `gal_gfx`, a dynamic major, the node `gfx`, as
`rtk_kadp_gal_gfx.c`), and mstar-utopia.ko's `gfx` (MStar's is its frame buffer, the device
table's 29:0, which the kernel's fb core holds here: a dynamic major, made before makedevs). A
character device and not a file, because the jailer copies `/dev/gfx` into a native app's jail
(`copynod`, on MStar and Realtek) and refuses anything but a device node.

### lgib

`lgib.c` is `/dev/lgib`, LG's instant-boot device (`CONFIG_LG_INSTANT_BOOT`, `guest/kernel/power/user.c`
in LG's GPL kernels for W16R to W22H): `LGIB_SUSPEND_TO_RAM` (`_IO('i',1)`, needs
`CAP_SYS_ADMIN`) suspends to idle, LG's `pm_suspend(PM_SUSPEND_MEM)` on this kernel, and returns at
the wake. One open at a time (`EBUSY` until it is closed), as LG's. tvpowerd's
`PowerStatesMgr::stateSuspendReady` is its user (Quick Start+'s power-off, W15M on).

## Initramfs

`build/initramfs.cpio.gz` is the root the kernel starts `/init` in (`guest/initramfs/rootfs/init`; it stays the root). QEMU loads it with `-initrd`, separate from the Image, so a
changed script repacks about 1 MB and the kernel is not relinked; the kernel keeps its built-in
default initramfs (`/dev`, `/dev/console`, `/root`), which the external one is unpacked over.

Its sources are in `guest/initramfs/`: `rootfs/` holds the files copied as they are (`init`,
`bin/webos-start`, `bin/webos-enter`, `etc/profile`), `busybox/` the busybox build (below,
"Initramfs busybox"), `src/` the static arm64 programs it carries, the pointer bridge and the IR
pump (`virtio-pointer.c`, `virtio-ir.c`; `guest/initramfs/src/README.md`), the guest memory pump
(`virtio-meminfo.c`, below) and the boot splash
(`splash.c`, below), and `initramfs.list` the
image's contents. The kernel modules it carries stay in `guest/kernel/modules`.

It is packed by the kernel's own `usr/gen_init_cpio`, built with the kernel (the `kernel` target's
second output), from a declarative list: `guest/initramfs/initramfs.list`, in gen_init_cpio's format
(`file`, `dir`, `slink`, `nod` lines with explicit modes and owners). `meson/initramfs.py` replaces
`${NAME}` in it with build outputs (`${BUSYBOX}`, `${VIRTIO_POINTER}`, `${LGEMU_KO}`, ...) or
`guest/initramfs/rootfs/` (`${SRC}`) and writes the result to
`build/guest-h13-afro/guest/initramfs/initramfs.list` and runs `gen_init_cpio -t 0 <list> | gzip -9n`. Nothing
needs root or fakeroot: owners and device nodes come from the list. Every timestamp is 0 and
inodes are numbered in list order, so the same inputs give the same bytes (the guest kernel does
not keep initramfs mtimes anyway: no `INITRAMFS_PRESERVE_MTIME`).

The initramfs holds no layer and nothing of a firmware, a firmware target or an install: it is the
same for every VM, and every target's guest build packs the same one (from the same kernel,
busybox and `lgemu.ko`, built once for all of them: none of them takes `soc`, `webos` or the firmware). What is about one
firmware comes at boot from the launcher: the layers, each a read-only disk (below, "Layer
images"), and, in its host share, the boot manifest `/host/boot.conf` (`host/core/beanjar/targets.py`):
the target's paks and their mount points, the layers bottom to top, the state disks, the firmware's
init (`INIT_SYSTEM`, upstart or systemd), and the firmware paths init touches (the PmLog job, the trust stores, `board.cmdline`). init stops at the
debug shell without it.

- A file added to `guest/initramfs/rootfs/` (copied into the image as it is) must get a line in
  `initramfs.list`; the build fails otherwise.
- Its inputs are the target's dependencies (busybox, the input bridge and IR pump, gen_init_cpio,
  the kernel modules)
  or are in its depfile (the list, every file and directory of `guest/initramfs/rootfs/`), so a change to any
  of them repacks it and nothing else does. The cpio is only
  replaced when its bytes change.
- An out-of-tree kernel module for `/init` to `insmod` (guest/kernel/modules/<name>, below): one
  `'--var', 'NAME', <its target>` in `guest/initramfs/meson.build` and a
  `file /lib/modules/<name>.ko ${NAME} 0644 0 0` line in `initramfs.list`, as lgemu.ko has.
  busybox has `insmod`, `rmmod` and `lsmod`.

### Layer images

The layers are used in place: each is a read-only squashfs image on the host, attached as a disk
and mounted by `/init` as an overlay lower, so nothing of a layer is copied at boot
(`host/core/beanjar/layerimage.py`). Until 2026-11 `/init` copied every layer from
the host's 9p share into tmpfs (`cp -a`, `chown -R 0:0`), which cost 23 s of a 100 s first boot on
an RK3399 board whose files are on NFS (the gl layer's 16 MB alone 14.8 s) and kept the copies in
guest RAM.

- **Images.** `mksquashfs <layer> <image> -all-root -no-xattrs -comp lzo -b 128K` (squashfs-tools,
  never a tool of ours): every file root's, as the guest needs, while the host's files stay the
  user's; modes, symlinks and hard links as in the tree. A source layer's `layer.toml` is left out.
- **Cache by content.** An image is named by the sha256 of `layerimage.FORMAT` (the options
  above) and the layer's digest: a published component variant's own name
  (`build/components/<component>/variants/<sha256>`, a versioned layer's under
  `build/components/<name>/versions/<version>/variants/`, already a digest of its tree:
  `meson/components.py`), else a digest of the tree (paths, types, modes, link targets, bytes; not
  times or owners: `guest/layers/normal`). The launcher looks the image up in
  `tree.build("layer-images")` (the package's prebuilt images, `/usr/share/beanjar/guest/layer-images`)
  and then in `tree.cache("layer-images")` (the checkout's `build/layer-images`, an installed user's
  `~/.cache/beanjar/layer-images`), and builds it there on first use (beside its final name, then
  renamed). An unchanged layer is never built again and every VM of the host shares the file.
  `packaging/debian/build.py` builds the images of every shipped variant (each version of a
  versioned layer too) and of `normal` into
  beanjar-guest, so an installed host builds none; their variant trees are shipped too (the
  launcher's checks and `beanjar layers` read them). Images no VM uses any more are removed by
  `beanjar host prune-layer-images`: it keeps every image a VM of the library (and its development
  libraries) resolves to, a versioned layer's pinned version included, and prunes nothing while a VM
  runs or starts (`layerimage.prune`, under the cache's exclusive `.lock`; a start looks images up
  under the shared one). The shipped images are never removed.
- **Disks.** One `virtio-blk-pci` per layer, `readonly=on`, serial `layer-<name>` (at most 20 bytes,
  virtio-blk's serial; a longer name is refused), in `qemu.plan` beside the paks. `/init` finds
  each by its serial, as it finds the paks, and mounts it at `/run/layers/<name>`, the path the
  overlay over a pak's mount point looks under (`PAKS`). A VM has at most about 10 layers; with the
  paks and state disks a target's disks may pass the root bus's 31 slots, and then
  `qemu.pci_budget` puts them behind PCI bridges, 31 a bridge (SimpleSmart's 15 paks and 9 state
  disks already needed one). A virtio-scsi controller would take no slots, but its disks are
  `sd*` and found by other means; virtio-blk keeps one way of attaching a read-only image.
- **The per-VM layer** (`vm`, `qemu.guest_layer`) stays a copy: it is new at every start and a few
  KB, so an image would cost a `mksquashfs` per start for nothing. `/init` copies the 9p share
  `vm` into tmpfs at `/run/layers/vm`, root's, and the mode=mitm trust steps write the proxy's CA
  into that copy, as before. Nothing else writes into `/run/layers`: `binds.conf` binds files from
  `/host`, not from a layer, and every other layer was only ever read.
- **Compression**, measured on rk-1 (RK3399) for the lm21u-kisscurl gl layer (16.6 MB of
  files), `unsquashfs -p 1` to tmpfs on one core, warm, best of five runs on an A53 and three on an A72 (2026-11-05). The guest kernel
  has zlib, lzo and zstd, not lz4 or xz. LZO decompresses fastest on the A53; its image is 25% of
  the tree against zstd's 20%, which matters less: the images are small and shared, and the reads
  come from the host's page cache on a warm boot.

  | compression | image | A53 time | of which decompression (minus none) | A72 time |
  |---|---|---|---|---|
  | none | 16.6 MB | 130 ms | - | 104 ms |
  | lzo | 4.2 MB | 195 ms | 65 ms | 157 ms |
  | zstd -3 | 4.0 MB | 224 ms | 94 ms | 145 ms |
  | zstd -15 | 3.4 MB | 238 ms | 108 ms | 157 ms |
  | zstd -19 | 3.3 MB | 245 ms | 115 ms | 157 ms |
  | gzip -9 | 3.8 MB | 284 ms | 154 ms | 205 ms |

- **Boot time**, lm21u-kisscurl (webOS 6 StanbyME N21D 04.44.16), `LAYERS=normal,gl`, warm boots
  (the state disks from an earlier boot, the host's page cache warm), 2026-11-05. On rk-1
  (RK3399, KVM, the checkout and firmware on NFS from the Pi), times from the start command, the
  guest's clock against the host's to about 0.5 s; two to four boots each:

  | | copies (before) | images (after) |
  |---|---|---|
  | initramfs layer step (guest time, `state:` to `layer vm`) | 17.6-19.5 s | 1.5 s |
  | start to guest kernel (the host side) | 7.9-8.3 s | 5.2-5.5 s |
  | start to core-boot-done | 39.1-43.5 s | 22.5-22.7 s |
  | start to the Home app's process | 47.9-52.0 s | 30.8-30.9 s |
  | start to boot-done | 63.5-66.9 s | 44.0-44.1 s |
  | guest Shmem at boot-done | 101.8-102.1 MiB | 85.6 MiB |

  The host side lost the share's link tree of every layer (about 2.8 s there); building the seven
  images of a cold cache (the first start after a layer changed) takes 2.1-2.3 s on the board, the
  gl layer's 1.4 s of it, and finding them in a warm one 0.06 s. On the x86 PC (TCG) the layer step
  went from 3.3-3.9 s to 0.6 s (guest time; headless, without gl: 0.7-0.8 s to 0.4-0.5 s). What is
  left of the step is mostly finding each disk by its serial and the per-VM copy.

### Guest memory channel

The viewer's memory meter shows the guest's RAM and swap. RAM
comes from the virtio-balloon every VM has (`qemu.py`: `virtio-balloon-pci,id=balloon,
guest-stats-polling-interval=2`, never inflated; `CONFIG_VIRTIO_BALLOON=y` in every guest kernel),
whose statistics have no swap sizes. Swap comes over a port of its own, `webos.memory`:
`virtio-meminfo` (`guest/initramfs/src/virtio-meminfo.c`), which `/init` starts on every init
system beside the other pumps, keeps `/proc/meminfo` open and writes one line every 2 s,

    swap total=<SwapTotal kB> free=<SwapFree kB>

QEMU's end of the port is a 4 KiB `ringbuf` chardev (`id=memory`): bounded, with no host
connection for the guest to wait on, and kept until a reader drains it with QMP `ringbuf-read`
(the viewer, on `viewer-qmp.sock`, each second). A read may begin inside a line the ring
overwrote, so a reader uses only whole lines. The pump logs to `/run/virtio-meminfo.log` at start
and once per kind of error. A guest without swap (every Upstart release) writes `total=0`.

### Boot splash

Until the firmware's compositor makes its first modeset, a frontend would show QEMU's own "Display
output is not active.". `/init` runs `splash` (`guest/initramfs/src/splash.c`) first,
which shows `/etc/splash.ppm`: the logo (`host/ui/data/icon.svg`) with "beanjar" to its right on `#212121`, after LG's own bootloader logo,
laid out for 1080 lines, rendered at build time by `meson/splash.py` with `rsvg-convert`
(librsvg2-bin, DejaVu Sans) and scaled to the connector's preferred mode in the guest.

- fbdev emulation's `/dev/fb0` is not enough: FBIOPAN_DISPLAY does modeset fbdev's buffer, but it is
  a 2D resource whose pixels never reach a GLES host (on the Pi, a black 1920x1080 scanout). So
  `splash` does one legacy KMS modeset of its own, with a virgl RGBA resource as `gop-present.c` does
  (a dumb buffer without virgl).
- That part runs in `/init`'s foreground, bounded by a 10 s alarm, before anything of the firmware
  starts, and ends with DROP_MASTER: no compositor can race it, and the first one to open the
  device becomes master as it does without the splash. Without a GPU (`--headless`) it exits at once.
- The device stays open in a forked child (closing it would remove the framebuffer and turn the
  output off). It checks the CRTCs once a second and exits when one shows another framebuffer. If no
  compositor ever comes, the splash stays (about 0.5 MB resident, asleep).
- When the compositor exits, the kernel's lastclose restores fbdev's mode: black on the Pi, not the
  splash, until the compositor is back (surface-manager killed on k8hp-ombre: black for ~4 s,
  then Home).

Compared with the `find | cpio -H newc -R 0:0` archive of a staged tree it replaced
(2026-09-28): the same files, links, modes and owners, plus `/dev/console` (the node the kernel's
built-in initramfs already had), with busybox as below, in list order and with zero timestamps.

## Initramfs busybox

busybox 1.37.0 (`subprojects/busybox.wrap`), static, configured by
`guest/initramfs/busybox/configs/webos_initramfs.config`: a config fragment over `allnoconfig`, which lists the
applets and features in use and is commented with what uses them: `guest/initramfs/rootfs/init`, `webos-start`
and `webos-enter`, the commands and scripts run in the initramfs shell over the serial console
(`guest/scripts/save-logs.sh`, `tests/regress/boot/guest.sh`, `tests/regress/input/*`,
`tests/regress/tcg/io.sh`, `guest/layers/gl/test/sendkey.sh`, ...), a few interactive tools (`ls`, `ps`,
`less`, `free`, `top`), `insmod`/`rmmod`/`lsmod`, and `modprobe` with its `modules.dep` ("Peripheral
modules"). Scripts run through `webos-enter` use the
firmware's own busybox, not this one. 53 applets (the `defconfig` build before had 402), 1.2 MB
instead of 2.1 MB.

busybox's Kconfig has no `savedefconfig`, its `configs/*_defconfig` are whole `.config` files, and
its `allnoconfig` turns every bool off even when `KCONFIG_ALLCONFIG` sets it. So
`meson/kbuild.sh` merges the fragment as the kernel's `scripts/kconfig/merge_config.sh -n` does:
`make allnoconfig`, the fragment's lines replace those options in `.config`, `make oldconfig`
settles dependencies, and every `=y` of the fragment must still be set (a typo or a missing
dependency fails the build). That runs when the fragment differs from its copy in the object tree
(`build/busybox/<key>/obj`) or there is no `.config`; otherwise make builds incrementally.
`KCONFIG_NOTIMESTAMP=1` keeps the build time out of the binary (its banner reads
`BusyBox v1.37.0 ()`), so it is reproducible too.

To change it, edit the fragment (a comment saying what needs the applet) and build. To find an
option's name: `make -C subprojects/busybox-1.37.0 O=$(echo $PWD/build/busybox/*/obj) menuconfig`, `?`
on the entry, or `grep` the `Config.src`/`*.c` files for `//config:`. Options the fragment does not
set are off; an option that only exists as a dependency of a listed one is set by `oldconfig`.
