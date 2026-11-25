# The open test guest

Public CI may not use LG firmware, so without something else it cannot boot a VM. The test guest
is that something else: a small, freely licensed 32-bit ARM userland, booted by the guest kernel
through the normal launcher as a target of its own, which runs a few checks and reports them as
pass or fail with numbers. It is not meant to look like webOS. It covers the launcher, QEMU's
devices and the host side: virtio-gpu with virgl, the virtio-serial ports, the 9p shares, input,
the 32-bit ABI through the kernel's compat table (natively or under qemu-user), a 64-bit ARM program
beside the 32-bit userland as newer firmware ships them, and the guest's own power-off.

## Two parts: the userland and the checks

The test guest is split by how often each part changes.

- **The userland** is beanjar-vendor's program `testguest` (its `testguest/README.md`): a Buildroot
  root filesystem, the sysroot to compile against it, and Buildroot's legal-info, released like the
  other vendored programs. It changes when Buildroot or Mesa does, rarely, and a cold Buildroot
  build, which builds its own GCC, is well over half an hour on a CI runner.
- **The checks** (`guest/testguest`, GPL-2.0-or-later) stay here: they speak `include/webos-media.h`,
  `include/webos-video.h` and the port protocol, so they change with the host. The 32-bit ones are
  compiled against the vendored sysroot with Debian's armel cross compiler, and delivered with the
  64-bit side (below) as the target's one always-used layer, `testguest`, the way every target gets
  its layers.

Decided for the userland (beanjar-vendor's `testguest/defconfig`, Buildroot 2026.02.3, the LTS line):

- **armv7 EABI softfp, glibc, as webOS.** webOS's userland is 32-bit ARM with glibc, the EABI
  soft-float calling convention and VFP hardware: the webOS SDK's `libc.so.6` reads in `readelf -hA`
  as "Version5 EABI, soft-float ABI", `Tag_CPU_name` 7-A, `Tag_FP_arch` VFPv3,
  `Tag_Advanced_SIMD_arch` NEONv1, with the loader `/lib/ld-linux.so.3`, and the SDK's GCC defaults
  to `-mcpu=cortex-a9 -mfpu=neon-fp16 -mfloat-abi=softfp -marm`. The test guest is the same kind of
  binary: `BR2_ARM_EABI`, Cortex-A9 with NEON (`-mfpu=neon`, VFPv3-D32 and NEONv1; Buildroot offers
  no `neon-fp16`, whose half-precision conversions nothing here needs), ARM mode, and so
  `ld-linux.so.3`. That matters for two reasons. A library or program built with the webOS SDK
  loads in the test guest, so a check can use a piece of webOS's own toolchain's output without LG
  firmware. And qemu-arm (`hardware.userland = "qemu-user"`, [qemu-user bridge](qemu-user-bridge.md))
  and the kernel's compat table see the same binaries as on webOS: the floating-point arguments in
  core registers, the same loader name and the same libc ABI, so a pass here says what it would for
  the firmware. (Releases up to `+beanjar1` were armv7 hard-float, `ld-linux-armhf.so.3`: a binary
  the firmware cannot run.) No external toolchain is published for armv7 softfp glibc, so Buildroot
  builds its own (GCC 14.3, glibc 2.42); its Linux headers are 5.10, as glibc takes the headers'
  version as the oldest kernel it runs on. Every host CPU here runs Cortex-A9 code in AArch32 EL0,
  and TCG emulates it.
- **The 32-bit compiler is Debian's** `gcc-arm-linux-gnueabi` (GCC 14, the armel cross compiler,
  soft-float ABI) on the tarball's sysroot, with the CPU options its `BUILDINFO` names
  (`Target-Flags`). Debian's cross compilers are not built for a sysroot: `--sysroot` alone leaves
  their tool directory (`/usr/arm-linux-gnueabi/include` and `lib`, Debian's own armel glibc when
  `libc6-dev-armel-cross` is installed) ahead of it, and a first try linked Debian's `libc.so.6`
  and included its headers. So `scripts/testguest-setup`'s cross file puts the sysroot first by
  hand: `-nostdinc` with the sysroot's `usr/include` and GCC's own include directory after it
  (`-idirafter`: Meson drops an `-isystem` of a directory the compiler searches by default), `-B`
  for the start files, `-L` and `-rpath-link` for the libraries. Every header, start file and
  library is then the sysroot's; only GCC's headers, `libgcc.a`, `crtbegin.o` and `crtend.o` are the
  compiler's. It also undefines `_TIME_BITS`: Debian 13's armel compiler defines it to 64 (and
  `_FILE_OFFSET_BITS` to 64) by default, from Debian's 64-bit `time_t` transition, where webOS's
  userland, like glibc's default, has a 32-bit `time_t`; the checks import no `*time64` symbol. No toolchain is pinned or downloaded, and the guest build runs on arm64 hosts as well
  as x86_64. beanjar-vendor's `test-tarball.sh` checks the same route on every build, from the
  compiler's `-H` and the linker's `--trace`.
- **BusyBox init**, as NetCast 4.5's: `INIT_SYSTEM=sysvinit`, so the initramfs's existing sysvinit
  path starts it in the firmware's namespace like any other target; Buildroot's `rcS` runs the
  layer's `/etc/init.d/S99testguest`.
- **Mesa 26.0 with the virgl gallium driver only**, EGL, GLES and GBM; no LLVM, Vulkan, X11 or
  Wayland. A check draws offscreen on the render node; nothing is scanned out.

### The 64-bit side

Newer firmware ships 64-bit ARM programs beside its 32-bit userland: queue and spacebuns carry an
AArch64 multilib in `/usr/lib64` (glibc 2.39 with the loader `/usr/lib64/ld-linux-aarch64.so.1`,
linked from `/usr/lib/ld-linux-aarch64.so.1`, and ipset, libipset and libmnl), the 32-bit side
staying in `/lib` and `/usr/lib`. The test guest has the same layout, from the layer rather than
the userland: `guest/testguest/arm64` stages Debian 13's AArch64 glibc (2.41, `libc6-arm64-cross`,
the guest build's own aarch64 cross compiler's: `ld-linux-aarch64.so.1`, `libc`, `libm`, the
`libpthread`, `libdl`, `librt` and `libutil` stubs, `libresolv`, and GCC's `libgcc_s`) in
`/usr/lib64`, the `/usr/lib/ld-linux-aarch64.so.1` link, and the check `tg-arm64`, a dynamically
linked AArch64 program with that interpreter. Debian's loader searches `/lib/aarch64-linux-gnu`,
`/usr/lib/aarch64-linux-gnu`, `/lib` and `/usr/lib`, so `tg-arm64` carries `RUNPATH /usr/lib64`.
Taken from Debian's packages rather than built in beanjar-vendor: the guest build already depends
on them, and a second Buildroot configuration (and a second GCC) would be needed for a glibc that
nothing else in the userland uses.

The pieces are kept apart: staged as `stage/testguest-arm64` with the aarch64 `strip` and `readelf`
(held to their own glibc's 2.41) and taken into the `testguest` layer as they are (`stage.py
--built-tree`), so a guest whose kernel cannot run AArch64 natively can take that tree under
qemu-aarch64 instead.

### Where it comes from

`packaging/vendor.toml` pins it like QEMU: a table `[testguest]` with the release tag
(`testguest-<version>`) and `guest = "armel"`, the guest's architecture, with the SHA-256 of its one
tarball, which every host takes (`meson/vendor.py`). Before a release, or to try another build,
`BEANJAR_VENDOR_TESTGUEST=<path to testguest-<version>-armel.tar.xz>` takes that tarball instead,
pinned or not: for example the artifact of a beanjar-vendor CI run,
`gh run download <run> -R mariotaku/beanjar-vendor -n beanjar-testguest-armel`. A new release is a pin
change only.

`scripts/testguest-setup` takes the userland into `build/vendor` and writes:

| Path | What |
|---|---|
| `build/testguest/image` | `rootfs.pak` (the userland's root filesystem) and `manifest.toml` naming the target: a firmware directory for `beanjar create --firmware` |
| `build/testguest/cross.ini` | the cross file of the guest build of this target: Debian's `arm-linux-gnueabi-gcc`, `--sysroot` and the CPU options of `BUILDINFO`, and pkg-config into the sysroot |

The guest build of the target (`-Dsoc=virt -Dwebos=testguest`, `meson.build`'s `testguest`) enters
`guest/testguest`, `guest/kernel` and `guest/initramfs`: the `testguest` layer and the arm64 pieces
every target shares (Debian's aarch64 GCC). The Image and initramfs it publishes are those of any
other target's build. `scripts/build-packages.sh guest` skips the target: nothing of it is packaged.

The `testguest` layer (`build/components/release/testguest`) holds the checks, `tg-gpu`, `tg-ports`,
`tg-input` and `tg-bridge` in `/usr/bin` (stripped, held to the userland's glibc 2.42 like every
layer), the 64-bit side (`tg-arm64` and `/usr/lib64`), and `guest/testguest/rootfs`: the runner
`tg-run`, its init script and the board file.

## The target `virt-testguest`

`guest/targets/virt-testguest.toml` and `guest/targets/release/testguest.toml`: chip `virt`, release
`testguest`, platform `Test`, model `TESTGUEST` (a name, not LG's; in `bring_up`, so `scripts/regress`
leaves it out of its default set: its runner is `scripts/testguest-check`). The release has one pak,
`rootfs` at `/`, one always-used layer, `testguest`, no state disks, 512 MiB (`dram`), a 1280x720 display (`screen`), and
neither `partmap` nor `nvm`: it has no LG board data. Its board file is
`guest/testguest/rootfs/etc/board.cmdline`.

The special cases are these, each keyed on the release having no board data
(`targets.board_data`) or on the platform `Test`:

- `prepare.py` makes no partition map and no EEPROM (no firmware code runs at create), and its
  record says `board_data = false`; `qemu.plan` attaches no partinfo disk and `prepare_nvm` does
  nothing; `boot.conf` has empty `NVM_EEPROM` and `PARTINFO_DISK`, which the initramfs already skips.
- `newvm` gives a VM of the platform no optional layers unless `--layers` says otherwise (the
  default `normal,gl` are webOS's).
- Power-off (`targets.takes_power_off`, `poweroff.power_off_call`): `beanjar stop` types
  `timeout 30 webos-enter poweroff` on the initramfs console instead of tvpowerd's call, and does
  not wait for the micom's `ready=1`, which only webOS's power stack sets. BusyBox's init then runs
  its shutdown and calls `reboot(2)` inside the PID namespace; the kernel ends the namespace's init
  with SIGINT and `webos-start` powers the machine off, so QEMU exits by itself: the same path a
  webOS shutdown takes. Chosen over QMP `system_powerdown` (the virt machine's power button is the
  micom's wake line here, and the guest has no ACPI) and over stopping QEMU, which would not test
  the guest's side. The launcher picks the call by the running launch's `vm-info.toml` platform.

## The checks

`/etc/init.d/S99testguest` starts `tg-run` once at the end of the boot. It runs each check, collects
their `key=value` lines and writes them to the out share as `testguest/results.txt` (written as
`results.tmp` and renamed, so the host never reads half a file) and to the console (`webos.log`).
Each check adds `<check>=pass` or `fail`; `result=pass` when all passed.

| Check | What it does | Numbers |
|---|---|---|
| `gpu` (`tg-gpu.c`) | GBM on `/dev/dri/renderD128`, an EGL GLES 2 context (surfaceless), a 256x256 offscreen framebuffer cleared and drawn 100 times, the centre pixel read back; passes when it is the colour drawn and the renderer is virgl | `gpu.renderer`, `gpu.frame_ms` (draw and `glFinish`) |
| `ports` (`tg-ports.c`) | every port of `qemu.py` by name (14); on `webos.media.0` a decode session of `include/webos-media.h` (OPEN, CAPS, DECODE, EOS) that the host's media helper answers with a PICTURE marked LAST; one SYNC frame of `include/webos-video.h` on `webos.video`, which is guest to host only | `ports.found`, `ports.media_rtt_ms` |
| `shares` (`tg-run`) | the read-only `host` share's `boot.conf` read and a write there refused; 32 MiB written to the `out` share, read back and compared | `shares.out_write_ms`, `shares.out_read_ms` |
| `input` (`tg-input.c`) | the evdev node of QEMU's virtio keyboard; once open, it creates `testguest/input-ready` and waits up to 60 s for a key | `input.key`, `input.wait_ms` |
| `bridge` (`tg-bridge.c`, the same program as `tests/integration/bridge/bridgetest.c`) | the 32-bit ABI through the kernel's compat table: uinput and evdev, DRM and virtio-gpu ioctls, a robust mutex, signals, fork, exec and wait, `/proc/<pid>/exe`; after `input`, as it creates an input device of its own | `bridge.userland` (`native`, or `qemu-user` under qemu-arm), `bridge.failed` |
| `arm64` (`arm64/tg-arm64.c`) | a 64-bit ARM program on the AArch64 glibc in `/usr/lib64`: 8-byte pointers, `uname` says aarch64, `clock_gettime`, a file written to the out share, read back and removed, a thread, fork and wait, and DRM's version ioctl on the render node naming `virtio_gpu`; it runs natively on the arm64 guest kernel whatever `hardware.userland` says, and under qemu-aarch64 on the x86-64 one | `arm64.machine`, `arm64.failed` |

`webos.ir` and `webos.memory` are held by the initramfs's pumps and `webos.standby` must not be
opened (holding it open is the guest's standby), so the check only finds them. Times in the guest
are its own clock, which the launcher scales under TCG (`webos.time_scale`).

## The test command

`scripts/testguest-check` is what CI runs, and works the same on a workstation: it creates a VM on
the image (`--display egl-headless --link off`: the checks need no network, and a CI container has no
unprivileged user namespaces for a NAT; default name `testguest-check`, `--library` for
a library of its own), starts it, sends keys over QMP `input-send-event` (the keyboard the viewer
and VNC reach) once `input-ready` appears, waits for `results.txt`, then adds the host's own checks:

- `host.micom_port`: `run/micom-status` has the line the initramfs's IR pump writes on
  `webos.micom` at boot (the IR pump's other direction).
- `host.poweroff`: `beanjar stop --no-fallback` returns with "QEMU exited ... after the call", the
  guest's own power-off and nothing else; `host.poweroff_s` its time.

`--userland qemu-user` sets the VM's `hardware.userland`, so the guest's 32-bit programs run under
qemu-arm with the compat bridge ([qemu-user bridge](qemu-user-bridge.md); qemu-arm from `QEMU_ARM`),
which the `bridge` check reports as `bridge.userland=qemu-user`.
`--guest-kernel x86-64` sets `hardware.guest_kernel` (x86-64 hosts): the experimental x86-64
guest kernel, the 32-bit programs under qemu-arm and `tg-arm64` under qemu-aarch64 (`QEMU_ARM`,
`QEMU_AARCH64` and `QEMU_X86_64` name local builds).

It prints every result, deletes the VM (unless `--keep`; `--logs DIR` keeps its logs and the guest's
output) and exits 0 when all passed, 1 when one failed, 2 when the run itself failed. One run on the
x86 PC (TCG, virgl on the d3d12 renderer, 2026-11-15, on the hard-float userland of `+beanjar1`,
before the `arm64` check):

```
boot.uptime_ms=2520
gpu.renderer=virgl (D3D12 (the PC's GPU))
gpu.gl_version=OpenGL ES 3.0 Mesa 26.0.1
gpu.pixel=3399ccff
gpu.frame_ms=4.686
ports.found=14
ports.media_rtt_ms=231.603
shares.out_write_ms=1400
shares.out_read_ms=4070
input.key=30
input.wait_ms=202.2
bridge.userland=native
bridge.failed=0
host.start_s=5.0
host.results_s=20.1
host.poweroff_s=6.5
result=pass
```

The whole command took 32 s.

## CI

`.github/workflows/testguest.yml` runs `scripts/testguest-ci.sh` on GitHub's `ubuntu-24.04` runner
(x86_64, TCG) in a `debian:trixie` container, as the other workflows do, on pushes to `main` and
pull requests that touch the guest, the launcher, the wire formats or the build, and by hand. The
script installs the `guest` and `host` build-dependency roles, runs `scripts/testguest-setup`, builds
the testguest layer, the kernel and initramfs, and the host build (the prebuilt QEMU, whose
surfaceless EGL display gives virgl Mesa's software rasteriser on a runner without a GPU, and the
media helper), then runs the check in a library under `build/testguest`. It builds no Buildroot:
that is beanjar-vendor's job. The workflow caches the vendored downloads (`build/vendor/download`:
the userland and QEMU, keyed by `packaging/vendor.toml`) and Meson's package cache,
and keeps the results and the VM's logs as the run's artifact.

The script was run in a fresh `debian:trixie` container on the PC (2026-11-15; no GPU passed in, as
on the runner; the userland the softfp `+beanjar2` build named by `BEANJAR_VENDOR_TESTGUEST`, QEMU's
tarball already downloaded): packages 113 s, the userland 1 s (no toolchain to fetch any more), the
layer, kernel and initramfs 133 s, the host build 3 s, the check 28 s: 278 s in all. Every check
passed there on virgl over llvmpipe (`gpu.renderer=virgl (LLVMPIPE (LLVM 19.1.7, 256 bits))`,
OpenGL ES 3.2, `gpu.frame_ms=10.723`, `ports.media_rtt_ms=2.439`, `bridge.failed=0`,
`arm64.failed=0` with `arm64.drm-version=pass (virtio_gpu 0.1)`, `host.poweroff_s=6.0`). A program
built with the webOS SDK's compiler (`arm-webos-linux-gnueabi-gcc`, calling `pow` and `sqrt` in
libm with `double` arguments), added to the layer for one run, ran in the guest on its glibc 2.42
and printed the right result.

## Licensing

Everything in the root filesystem is built by Buildroot from upstream releases (BusyBox, glibc,
Mesa, libdrm, expat, zlib and the toolchain's runtime); the layer adds our checks and Debian's
AArch64 glibc and `libgcc_s` (`libc6-arm64-cross`, `libgcc-s1-arm64-cross`: LGPL-2.1-or-later, and
GPL-3.0-or-later with the GCC Runtime Library Exception; their sources are Debian's `glibc` and
`gcc-14` source packages). No LG file, font or key. beanjar-vendor releases Buildroot's legal-info beside the userland
(`testguest-<version>-legal-info.tar`: each package's licence and source); a copy of the image
passed on should come with it.
