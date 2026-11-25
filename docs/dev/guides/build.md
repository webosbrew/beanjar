# Building

Everything is built by Meson, in build directories of one source tree: a guest build per firmware
target and the host build:

| Directory     | Machine                         | Builds                                                   |
|---------------|---------------------------------|----------------------------------------------------------|
| `build/guest-<chip>-<release>` (WT1H: `build/guest-h13-afro`) | cross, `meson/cross/webos-sdk.ini` for webOS/SimpleSmart or `meson/cross/lgtv-sdk.ini` for NetCast 4.5 (x86_64 SDKs), `-Dsoc=<chip> -Dwebos=<release>` | target-specific 32-bit ARM layers and tools (webOS: common, soc, webos, gl with Mesa and optional layers; SimpleSmart: simplesmart; NetCast 4.5: netcast45 with Mesa); and the arm64 kernel with its modules (lgemu.ko, lg115x.ko, mstar-utopia.ko, rtk.ko, mtk.ko, btmtk_usb.ko, webos_tv.ko, lgfs.ko), busybox and initramfs |
| `build/host`  | native (x86_64 or the Pi's aarch64) | the viewer, the GTK layouts (viewer, VM manager), the firmware cache, the network helpers, the unit tests; the prebuilt QEMU, FFmpeg and neatvnc published beside them |
| `build/host-arm64` | cross on the x86_64 PC, `meson/cross/debian-arm64.ini` (Debian's aarch64 GCC and Debian's arm64 libraries, multiarch) | the host build's programs for arm64, published to `build/publish-arm64` for the arm64 packages; its tests are built, not run; no firmware cache |

## Commands

From a fresh clone, on the x86_64 workstation, with Debian 13's Meson and Ninja:

```sh
meson setup --cross-file meson/cross/webos-sdk.ini build/guest-h13-afro -Dsoc=h13 -Dwebos=afro
meson setup build/host
meson compile -C build/guest-h13-afro && meson compile -C build/host   # builds and publishes to build/
meson test -C build/host                                         # unit and integration tests (testing.md)
host/scripts/vm-launcher start                                                 # boot the checkout's VM (host/scripts/vm-launcher --help)
```

NetCast 4.5 needs the lgtv SDK cross file on its first setup:

```sh
meson setup --cross-file meson/cross/lgtv-sdk.ini build/guest-lm14-netcast45 \
  -Dsoc=lm14 -Dwebos=netcast45
meson compile -C build/guest-lm14-netcast45
```

`scripts/build.sh guest` initializes new build directories with the webOS cross file;
preconfigure NetCast as above before using that helper. It reuses an existing build directory.
SimpleSmart uses the webOS SDK with `-Dsoc=m2 -Dwebos=simplesmart`.

The open test guest (`virt-testguest`, [test guest](../architecture/test-guest.md)) needs no SDK:
its userland is beanjar-vendor's. `scripts/testguest-setup` fetches it and the toolchain it pins into
`build/vendor`, writes the firmware directory `build/testguest/image` and the cross file
`build/testguest/cross.ini`, and then `meson setup --cross-file build/testguest/cross.ini
build/guest-virt-testguest -Dsoc=virt -Dwebos=testguest` builds its layer (the checks), the kernel
and the initramfs. `scripts/testguest-check` boots it and runs the checks.

The guest build reads no firmware: what it asks of its target's firmware is checked in as the
target's firmware facts (below), so a clone without any firmware builds every target.

**Firmware facts.** `guest/targets/facts/<chip>-<release>.txt`, beside the target's own
`guest/targets/<chip>-<release>.toml`, answers the questions the guest build asks of the target's
firmware, and nothing else: whether its rootfs image has a path a shim rule decides by
(`path usr/lib/nyx/modules/nyxDrm.module no`), the version suffix of a library a rule checks
(`version usr/lib/libwayland-client.so.0 20.0`), whether a stock library of the rootfs or bsppart
image defines a symbol, read from the image itself
(`defines bsppart:bsp/usr/lib/libkadaptor.so:RHAL_SE_Init yes`), and for each firmware library the guest code links
against, its SONAME (`lib usr/lib/libglib-2.0.so.0 libglib-2.0.so.0`) and the symbols the guest code
uses of it, with their default version where the library versions them, their type and a data
symbol's size (`sym usr/lib/libgstreamer-1.0.so.0 _gst_debug_min object 4`). One fact per line,
sorted; `host/core/beanjar/fwfacts.py` has the format. The build links against stub libraries made
from the lib and sym lines (`guest/targets/meson.build` runs `python3 -m beanjar.fwfacts stub`): each has the library's
SONAME and those symbols only, so a plugin or program records the same `NEEDED` entries, dynamic
symbols and version needs as a link against the firmware's own file did.

Each rule adds its question to `fw_questions` before it reads the answer; the setup writes them to
`firmware-questions.txt` in the build directory and stops, naming them, when the facts file does not
answer one. `host/scripts/vm-launcher firmware-facts <target>` answers them from the target's
paks, read in place: paths with `unsquashfs -ls`, libraries in the rootfs image mounted read-only
with `squashfuse` for the run (`--firmware DIR` for another firmware directory: a collection entry,
or an EPK mounted with `epkfs`). Nothing is extracted:

```sh
host/scripts/vm-launcher firmware-facts k8hp-ombre --build build/guest-k8hp-ombre   # write
host/scripts/vm-launcher firmware-facts k8hp-ombre --check                          # report drift
```

With `--build`, the questions are those the build directory asked, and once its objects are compiled
the symbols are those its link steps against a firmware library leave undefined that the library
defines (a new call into GStreamer fails the link with `-z defs` until then: compile, run the
command, compile again). Without it, the facts file's own questions and symbols are looked up again,
which is what `--check` does. A new target: `meson setup` (stops with the questions), the command with
`--build`, `meson setup --wipe` with the same options, `meson compile` (its links fail), the command
again, `meson compile`.

The arm64 packages are cross-built on the PC (`scripts/build.sh host-arm64`, role `host-arm64`
below; packaging/debian/README.md, "arm64 packages from the PC"): an ordinary Meson cross build,

```sh
meson setup --cross-file meson/cross/debian-arm64.ini build/host-arm64
meson compile -C build/host-arm64      # publishes to build/publish-arm64
```

A cross file for aarch64 selects the host lane (`host_cross` in `meson.build`); one for 32-bit
ARM the guest lane. The GTK layouts are compiled with the PC's own `blueprint-compiler` and
`glib-compile-resources`. There opus, GBM and the virtual camera's libraries are required (a
missing one fails the configure instead of dropping `beanjar-vnc` or a part of it), and the
prebuilt programs are the arm64 ones, linked through the cross file's arm64 `pkg-config`. Its
default `publish_dir` is `build/publish-arm64`, never `build/`. Its programs and tests are not run
on the PC.

**The prebuilt programs.** QEMU, FFmpeg and neatvnc (with aml) are not built here: every host
build takes beanjar-vendor's release tarballs for its architecture, pinned in
`packaging/vendor.toml` (packaging/debian/README.md, "The prebuilt programs"). At `meson setup`,
`meson/vendor.py` downloads them into `build/vendor/download` (once: a tarball with the pinned
SHA-256 is kept), checks the hash and unpacks each into `build/vendor/<program>-<version>-<arch>`;
the build fails if one cannot be had. The media helper and `beanjar-vnc` compile and link against
those trees through their own pkg-config files (`vendor_deps` in `meson/meson.build`), which come
before any system `-dev` package's, with RUNPATH `$ORIGIN/../<program>/lib` and
`/usr/lib/beanjar/<program>/lib`; the publish step copies the trees to `build/qemu`,
`build/ffmpeg` and `build/neatvnc`, where the first finds them and the launcher runs QEMU. Taking a
new version is a new pin; the build reconfigures when `packaging/vendor.toml` changes.

The Pi builds only the host side: the same without the guest build's lines. After that,
`meson compile -C build/guest-h13-afro` (or `ninja -C build/guest-h13-afro`) is all a rebuild takes;
Meson reconfigures by itself when a `meson.build`, `meson.options` or wrap changes.

**The firmware target.** A firmware boots as a target (chip, release). For webOS, the SoC layer is built from
`guest/layers/soc/<chip>`, and the release family from `guest/layers/webos/<release>`: the codename of the
firmware's `etc/starfish-release` (`afro`, `beehive`, `dreadlocks`, `dreadlocks2`).
`-Dsoc` and `-Dwebos` choose it, and a guest build needs both: `host/scripts/vm-launcher config TARGET`
prints the default firmware's target, from the one table that knows it (`guest/targets/`, loaded by `host/core/beanjar/targets.py` as `MODELS`: model code → {release: chip}, the release being the codename the firmware's own rootfs names). The
target decides the glibc ceiling (afro: 2.14.1; beehive, dreadlocks and dreadlocks2: 2.19; goldilocks and goldilocks2: 2.24; jhericurl and kisscurl: 2.28; mullet: 2.31) and where the layers are published,
`build/components/{common,soc/<chip>,release/<release>,gl,...}`, whose compatibility selectors the launcher resolves for a VM whose
firmware is that target. The first five webOS targets are: `h13-afro` (WT1H, `build/guest-h13-afro`), `m14-beehive` (W15M,
`build/guest-m14-beehive -Dsoc=m14 -Dwebos=beehive`), `m2-dreadlocks` (W16R,
`build/guest-m2-dreadlocks -Dsoc=m2 -Dwebos=dreadlocks`), `m16p-dreadlocks2` (W17H,
`build/guest-m16p-dreadlocks2 -Dsoc=m16p -Dwebos=dreadlocks2`) and `m3-goldilocks` (W18R,
`build/guest-m3-goldilocks -Dsoc=m3 -Dwebos=goldilocks`); the last three have no
media elements (`gst_plugin_dir` ''). `k5lp-goldilocks2` (W19P), `k6lp-jhericurl` (W20L, webOS 5: systemd) and `k7lp-kisscurl` (W21P, webOS 6, a global build) are the established Realtek targets. `k8lp-mullet` (W22P), `k8ap-mullet` (W22L), and `k8hp-mullet` (W22H), webOS 7,
are explicit bring-up profiles. W22H also has `k8hp-number1` (webOS 23) and
`k8hp-ombre` (webOS 24) upgrade profiles. The kernel, lgemu.ko, busybox and the
initramfs are the same for every target (none of them takes the target) and are published to the same
`build/` paths; each target's build makes them anew from the same inputs.

The legacy targets are `m2-simplesmart` (SS1A), `lm14-netcast45` (NC5U) and `h13-netcast4`
(NC4H, `-Dsoc=h13 -Dwebos=netcast4` with the lgtv cross file), with their own always-used layers
instead of the webOS soc/release layers. Their glibc ceilings are 2.19, 2.16 and 2.14.1. See
[SimpleSmart](../architecture/simplesmart.md), [NetCast 4.5](../architecture/netcast45.md) and
[NetCast 4](../architecture/netcast4.md) for their graphics and service boundaries. The webOS
regression runner's Luna/bootd checks do not apply to these legacy targets.

The build needs Meson 1.7 (`meson_version`; the newest feature it uses is from 1.6, Mesa 26.2
needs 1.4), which is Debian 13's, and the system Python with Mesa's modules (`python3-mako`,
`python3-yaml`, `python3-packaging`); the Mesa stack's inner builds run the same Meson. The
initramfs's boot splash is rendered with `rsvg-convert` (`librsvg2-bin`) and DejaVu Sans
(`fonts-dejavu-core`).
`scripts/build.sh [guest|host]` does the steps in one go: `meson setup` of a build directory
that does not exist yet (the guest build of `FW`'s target, `-Dsoc` and `-Dwebos`; the host build's
firmware cache takes `-Dfirmware`, else `FW`'s), then `meson compile`;
`host/scripts/vm-launcher start` runs `scripts/build.sh guest`
before booting (not on the Pi, which does not build the guest, nor for a VM directory, `--dir`).

Each build ends with its `publish` step (`meson/publish.py`), which copies what changed to where
the launchers and any host sync use it: `build/Image`, `build/{lgemu,lg115x,mstar-utopia,rtk,mtk,btmtk_usb,webos_tv,dtv_soc,lgfs}.ko`,
`build/initramfs.cpio.gz`, `build/components/{common,soc/<chip>,release/<release>,gl,...}`, `build/ssh/bin`,
`build/guest-installation.toml` (the guest build's version, information for logs; the host build's is
`build/source-version.toml`; docs/dev/architecture/build-and-boot.md, "Host and guest compatibility"),
`build/tests/{gl,gm}`, `build/tcg-perf/{perf-arm,io-perf}`, `build/devmode-tests/native-test`; from the host build `build/viewer/` (`beanjar-viewer`,
`beanjar_ui/viewer/`, `beanjar-viewer.gresource`, `libbeanjar-viewer-core.so`, `viewer-headless`),
`build/manager/beanjar.gresource`, and the prebuilt `build/qemu`, `build/ffmpeg` and `build/neatvnc`. Files are replaced by a rename and trees by an atomic exchange (the previous tree
waits in `build/.publish-old/`), so a running QEMU or viewer keeps the files it has open.
`-Dpublish_dir=DIR` publishes elsewhere.
This is not `meson install`: an install copies over files in place and never removes one that a
layer no longer has, where a layer must be replaced as a whole.

Useful targets: `kernel`, `lgemu`, `busybox`, `initramfs`, `<name>-layer` (common, soc, webos,
gl, headless, rootssh, devmode, tcg-perf), `mesa`, `ssh-bin`,
`guest-tests`, `fetch-<wrap>`; `beanjar-viewer-core`, `viewer-headless`, `beanjar-viewer-ui`
and `beanjar-ui` (the layouts, compiled), `qemu`,
`firmware-cache`, `check-cursor`. A
target alone does not publish; `publish` (the default build) does. The tests are pytest's, in
tiers: `tests/unit` needs no build, `tests/integration` this build's outputs ([testing](testing.md)).
`meson test --suite unit` (`integration`, or an area: `manager`, `vnc`, `viewer`, `regress`, ...) runs
one group; `meson test -C build/guest-h13-afro` checks the soc layer's shims (each NEEDs the stock
library it wraps) and the guest build's integration tests (`tests/integration/layers`). A third
build directory, `build/coverage`, measures how much of the host programs these tests run
([coverage](coverage.md)).

## Which checkout a library uses

A library's `config.toml` keeps one checkout (`repo`), changed only with
`beanjar library config set repo=DIR`. `beanjar --repo DIR COMMAND` uses `DIR` for that
command alone and writes nothing, so a command run from a scratch worktree cannot repoint
the shared library. A library with no stored checkout uses the checkout the running code comes from, in
memory only; where no checkout is known at all, commands that need one refuse and name
that command. The development library of a checkout (below) stores its own
checkout when it is prepared.

## Development VM data

`host/scripts/vm-launcher start` without `--dir` uses a managed development VM
under `$BEANJAR_LIBRARY/development-<checkout-hash>/vms/<instance-hash>`.
Without `BEANJAR_LIBRARY`, the base is `$XDG_DATA_HOME/beanjar` (normally
`~/.local/share/beanjar`). The development library has its own `ca/`, and each VM
has `vm.toml`, `identity/`, `state/`, `run/` and `logs/`. `INSTANCE` selects a
separate VM; a firmware change requires another instance. Fresh VMs get generated
identities; migrated VMs retain their effective legacy MAC and serial.

Stop the old checkout VM before migrating. Supply the same `FW`, `MAC`, `SERIAL`
and launch settings used previously if they were overridden:

```sh
host/scripts/vm-launcher migrate-local
# For a named legacy instance:
INSTANCE=h13-afro host/scripts/vm-launcher migrate-local
host/scripts/vm-launcher start
```

Migration copies the checkout's `local/` keys and CA and the selected instance's
persistent state, preserving sparse disks. It retains the originals and refuses
active VMs, symlinks or conflicting destinations. An identical existing
development CA can be reused when migrating another instance. Starting with an
unmigrated legacy identity refuses to generate replacement keys. Keep the
original files until the migrated VM has been verified; migration never deletes
these backups. Development configs can be inspected through the manager using
`--library` with the development library path printed by migration.

`argv` reads an existing development config without creating files. Explicit
runtime paths remain available to test harnesses: the regression runner keeps
its temporary captures and runtime files under `build/`, while identity data
uses managed directories. Published guest artifacts remain under `build/`.

Firmware is looked up by name in the host's firmware collections, `firmware_dirs` in
`~/.config/beanjar/config.toml` (`beanjar host config set firmware_dirs=DIR[:DIR...]`; default
`/srv/beanjar/firmware`). Firmware is read in place and never extracted into the checkout: for a
firmware not in a collection, mount its EPK read-only with `epkfs EPK DIR` (a directory of its
paks) and pass `FW=DIR` (or the host build's `-Dfirmware=DIR`; the guest build takes no firmware).
The checkout root has no `firmware/` directory.

## Regression runs

`scripts/regress` runs the smoke checks over every supported target, one VM at a time, from this
machine against a VM host over ssh (`--host HOST` or `REGRESS_HOST`; it has no built-in default; the checkout there is `DEST`, default `beanjar`, in its home; `REGRESS_SYNC` names the executable that copies this checkout to it; `--local` boots here instead):

```sh
scripts/regress --host pi        # build each target's guest, sync, then every target on the host `pi` (not those in bring-up, `targets.BRING_UP`, nor untested targets, `tested = false`: name them with --target)
scripts/regress -n               # dry run: print every command, run nothing
scripts/regress --target m2-dreadlocks --no-build --no-sync --keep-going --timeout 600
scripts/regress --tv --viewer    # graphics: each VM also in the viewer on the VM host's desktop
```

A run over ssh tests a commit, not this checkout's working tree: several agents edit the checkout
at once, and a run that copied it could test someone's half-finished edit without saying so. By
default the VM hosts get `HEAD`'s (`--commit REV`: `REV`'s) public tree: the commit filtered by
`.github/publish/paths.txt`, as the publisher filters it (`scripts/publish/sync.py`, `host_commit`: a
commit with no parent at `refs/hosts/<commit>`, the same id every time), so every run also shows that
the published tree works on its own. In a repository without the publishing machinery (the public one)
the commit itself is sent. `--private-tree` sends the commit's whole tree instead, still without its
history (`refs/hosts/private/<commit>`). A failure that comes from a file the public tree lacks is a
finding about `paths.txt`, not something to work around in the run.

Each VM host keeps a bare repository, `beanjar-src.git` in its home, made on first use. The run
pushes the commit there where the host lacks it (`git push`: only the objects it lacks) and checks it
out into `DEST` exactly (`tests/regress/lib/hostsrc.py`): files the commit does not have go, except the
host's own `.git/` (a repository of its own, if it keeps one there), `build/`, `local/`, `scratch/` and what Meson unpacks or caches under `subprojects/` (what
`host/scripts/sync-pi.sh` never deleted either), and unchanged files keep their modification times, so
the host's build stays incremental. A push never changes `DEST`; `scripts/publish/host-push.py` pushes
ahead of a run from a post-commit hook (`host-push.py install`; commits on `main` only, hosts from `git
config beanjar.hosts`, a host that does not answer skipped with a line in `host-push.log` in the git
directory), which only saves time: the run pushes what is missing itself.

What the build publishes is not in git: `build/Image`, the components and layers and
`build/publish-arm64` still go by the command `REGRESS_SYNC` names (`SRC=none`), from this checkout's
`build/`, which is built from its whole working tree, private parts and uncommitted changes included.
So a public-tree run tests the published host side and scripts against guest outputs built from the
private tree. `--working-tree` sends this checkout as it is instead of a commit, through the same
command (`SRC` is the checkout).

The run holds each VM host's lock, `DEST.lock` in its home (`flock`), from before the checkout to its
end, over its checkout, build and lanes; a second run, or `host/scripts/sync-pi.sh` run by hand, fails
at once and names the holder. The run directory's `source.json` says what was tested: the commit, the
tree (`public`, `private`, `working-tree`; `checkout` with `--local`), whether it was `filtered`, the
`host_commit` the hosts checked out, whether this checkout had uncommitted changes (tracked or
untracked, not ignored: left out of a commit's run, tested by `--working-tree` and `--local`) and the
`hosts`; with `--no-sync` the commit is `null`. `summary.txt` ends with the same in a line, and
`summary.json` has each target's `commit`. The runner itself, its judges and the test apps it packs run
from this checkout.

One run can spread its targets across several KVM hosts: `--host` repeated or comma-separated, or
`REGRESS_HOST` as a comma- or space-separated list (`--host pi,board1,board2`). Every host
gets the same commit, is held and checked for leftovers before and after. The trees go first, host by
host, which is quick; then each host syncs the build's outputs and builds its host side on its own,
side by side with the others, and starts its lanes as soon as its own preparation ends, so a slow first
sync (the components to a board's NFS home) holds back only that host. A host whose preparation fails
runs nothing, and a target that only it could run fails and says so. Each host
has its targets copied back into the one run directory here. At the start the runner reads over
ssh what each host can do (`TRAITS` in `scripts/regress`): its architecture; a toolchain (`meson`,
`ninja`, `cc`, which `scripts/build.sh host` needs); `/dev/dri/renderD128`, the render node the
launcher gives a KVM guest's virgl display (`RENDERNODE`'s default under KVM, `qemu.py`), without which
a graphics start fails; and a desktop session (a Wayland socket in `XDG_RUNTIME_DIR`, or X `:0`), where
the `--viewer` window opens. A graphics target goes only to a host with that render node, with `--viewer`
only to one with a desktop too, and any target only to a host whose firmware collections have its
firmware; a target no host can take fails without booting, with each host's reason under "not run:". A
host without a toolchain (a ROCK Pi 4 board) builds nothing: the sync ships it this checkout's
cross-built `build/publish-arm64` (`scripts/build.sh host-arm64`), which must be there, as `--no-build`
would. The targets go from one queue, in their order, to the first host with a free slot that can run
them; each host takes `-j N` at once (graphics: one), capped by its own CPUs and `MemAvailable` as a
single host's are, and a target whose RAM is more than a host's `MemAvailable` less `HEADROOM_MB` goes to
the hosts it fits where there are any (a 4 GB board ends up with one VM at a time). A failure without
`--keep-going`, or a leftover, starts no further target on any host. The summary of a run over ssh has a `host`
column, and the slots per host on its `wall` line when there are several; `summary.json` has each target's `host` and its host's
`jobs`.

`--viewer` (graphics only) opens `build/viewer/beanjar-viewer` on the VM's D-Bus display on the VM
host's desktop (its Wayland socket in `XDG_RUNTIME_DIR`, else X `:0`) once the VM has started, with
`--no-audio --no-media` and no remote (`ir.sock` takes one client, the run's `ir-key.py`: the viewer's
`--ir-socket` is a path that does not exist), and closes it before the VM stops (output in the target's
`host/viewer.log`). The screenshots, grabs and QEMU's audio recording are taken as without it.

The targets are `targets.MODELS`' (`host/scripts/vm-launcher config TARGETS`), each with the firmware that resolves to
it (its model code and the release its rootfs names) found in the host's firmware collections
(`firmware_dirs`, above; the default firmware first; `--fw NAME`
where there are several), so a target added there is picked up. Before booting it runs
`FW=<fw> scripts/build.sh guest` per target (`--no-build` only checks the published files),
the sync command (`REGRESS_SYNC`) and the VM host's `scripts/build.sh host` (`--no-sync` skips both). The VM host
boots each target's firmware from its own firmware collections (its `firmware_dirs`, by default the
shared read-only `/srv/beanjar/firmware`; `host/scripts/vm-launcher config FIRMWARE_DIRS` there),
looked up by the name chosen here, which is the collection directory's (the manifest's `name`).
Nothing is copied to the VM host: a target whose firmware is not in its collections fails without booting
("firmware X not in the host's firmware collections", under "not run:" in the summary). Then, per target, the checkout VM with `FW=<fw> STATE=none LAYERS=normal,gl
DISPLAY_BACKEND=dbus` (`NET_LINK` and `NET_MODE` as given, nat and direct by default; `LAYERS`,
`DISPLAY_BACKEND`, `NET_LINK`, `NET_MODE`, `AUDIO`,
`ACCEL`, `PMLOG_DEBUG`, `MEM`, `SMP`, `KERNEL_ARGS`, `QEMU_EXTRA`, `GPU_DEVICE`, `GUEST_KERNEL` pass through, and those not given are passed
empty, the launcher's defaults, so a target's development VM never brings back what its `vm.toml` kept from an
earlier run; `KERNEL_ARGS` are extra kernel cmdline words, e.g. `mstar_utopia.mbx_trace=1`, a module parameter `/init` passes on; `QEMU_EXTRA` extra QEMU arguments, e.g. `-trace virtio_snd_*`): boot-done (`tests/regress/guest.sh`, the guest's uptime at
it), `guest/scripts/check-m1.sh`, `tests/regress/tvservice/app-calls.sh`, the bring-up checklist (below), HOME on the remote once the
compositor shows no launcher by itself (`guest.sh home-idle`: webOS 3.5's Home Auto Launch shows it
for a few seconds after the first app, and a HOME then hides it; `tests/regress/ir-key.py`, over
`ir.sock`; webOS 4.5's Home, a HOME window that stays up after boot, gets HOME twice, hide and show) and a `tests/regress/virgl/dbus-grab.py` screenshot (the UI
over an opaque black video plane, as a TV shows it with no video), the guest's idle CPU at Home
(`idle-cpu`, "Bring-up checklist" below; headless: after ssh), Quick Start+'s standby and
resume where the firmware and the model offer it (tvpowerd opens `/dev/lgib`, and tvservice's
`instantBooting` is `quickStartPlus` or `true`; SKIP on WT1H and W15M's 42LF652V-ZA): `guest.sh
standby-prep` turns it on, POWER on the remote (headless: tvpower's `powerOff`, reason
`remoteKey`), `tests/regress/standby.py` waits for the standby (the guest's `webos.standby` port in
QMP), checks QEMU idles in it (`STANDBY_CPU`, 10 % of a host CPU, over 5 s from `STANDBY_SETTLE`, 2 s, after the
standby: the s2idle entry follows the micom's standby) and presses the power key (QMP
`system_powerdown`), `guest.sh resumed` checks tvpowerd is Active with the same tvservice and
tvpowerd answering and times the resume, and a second screenshot after HOME, 12 s after the
resume once the last app is back in front (`<target>-resume.png`), then
`host/scripts/vm-launcher stop`. A key that comes before the s2idle (a loaded host stretches the micom's standby to s2idle gap to seconds)
is not lost: lgemu's micom latches it and wakes the SoC as it suspends. `STANDBY_KEY_EARLY=1` (headless) tests that: `standby.py wake-early`
sends the key the moment the standby shows, and the step passes only if the guest wakes (console: `power key ... latched`,
`the latched power key wakes the SoC as it suspends`). A guest restart (the unit's `run/restarts` and
`run/restarting`) ends the step waiting on it and fails the target, with one exception: a target whose file
says `first_boot_restart = true` (W24G spacebuns: its firmware reboots the set once on the first boot of a
fresh state, as a set of it does at its first setup; `docs/research/spacebuns-fresh-state-restarts.md`) has
that one restart followed. The runner waits for the unit's new QEMU, which keeps the run's state, and runs
the steps again from the boot step; a second restart, or a VM that does not run again, fails. The target's
line and `summary.json` say `restarts=1`. Each step has `--timeout` seconds (300; times 4 under TCG, "Two lanes" below). It starts nothing while a VM, mitmweb,
slirp4netns, a viewer or a regress unit (`beanjar-vm-regress-*`) runs on the VM host, never uses a library VM, and
ends the run if a stopped VM leaves any of them behind. Results, copied back here:
`build/regress/<run>/<target>.png`, `build/regress/<run>/<target>/` (the steps' output, the logs
saved at stop and check-m1's in `saved/`, the launcher's host logs in `host/`) and
`summary.txt`, the table it prints (target, mode, boot-done, check-m1, app-calls, checklist,
screenshot, idle cpu, standby (seconds from POWER to the standby, QEMU's CPU in it, seconds from the power
key to the wake and from the wake line to Active), capture, run window; "Bring-up checklist" below), and `summary.json`; the exit status is 1 if any target failed. A target whose build makes
no gl layer (`guest/layers/gl/meson.build`: its chip's table, `guest/layers/soc/<chip>/meson.build`, has neither
`gl_vendor_stubs` nor `gl_vendor_gl`; no chip at present) is skipped when `LAYERS` has gl, neither built nor
booted, and its row says SKIP; a target that should have one and has none after the build fails the run.

With `NET_LINK=off`, each target starts with a fresh `build/regress/<run>/<target>/net.pcap` from QEMU's
guest NIC filter-dump. After the VM stops and closes the capture, the runner validates the pcap
and runs `tests/regress/netstub/pcap-summary.py --lg`, saving its output in `<target>/pcap-lg.txt`.
The `capture` step fails for a missing, malformed or shortened pcap, an LG contact recognized by
that tool, or an unsuccessful VM
stop; it is SKIP in the other network modes. A valid empty pcap passes: `NET_LINK=off` takes the
guest's NIC link down before boot. This checks traffic visible at that NIC; the link-down
network topology is what prevents egress.

`--headless` is a system sanity run with no GPU at all, on the Pi or with `--local` on this PC
under TCG: `LAYERS=normal,headless DISPLAY_BACKEND=none`, and no HOME or screenshot. Headless,
Normal's boot-done is not about the system: it follows the first app (the headless layer's Live TV
stand-in, which sends bootd's `banner-ready` at its launch as the stock app does at its first
banner) and the CPU-idle check. So the run is ready at `core-boot-done` made
by tvservice's `tv-ready` (`guest.sh ready`: bootd's own `/var/log/bootd.log` has the
`generateSignal` of `tv-ready`, `service_generateSignal=Payload(...)` on webOS 1 and 2,
`Handle-generateSignal/Request(...)` on 3 and 4), which is fast (a few seconds after bootd on
KVM) and means tvservice initialised. A `core-boot-done` by bootd's timeout (30 s on webOS 1
and 2, 60 s on 3 and 4) fails the target at once. On webOS 5 (W20L) there is no tvservice: its TV
service is legacy-broadcast, and bootd sends `core-boot-done` without waiting for it (3 s in, before
legacy-broadcast's `tv-ready`), so ready there waits for the `tv-ready` line itself and its table
shows that line's time; app-calls there calls only the services webOS 5 still has
(tv.systemproperty, sdx, eim, nop, tvpower). Then check-m1, which waits up to 150 s for
boot-done itself and skips its gl checks (no `libegl-kms.so` without the gl layer), app-calls,
and `guest.sh tvservice`: tvservice (`RELEASE`) is the process it was at ready and answers
`tv.systemproperty/getSystemInfo`. A legacy-broadcast (webOS 5 on) stops itself when idle and the
next call starts it again, so it may be gone or another process by then; that passes only on
evidence that every exit was clean (`tests/regress/lb-exit.sh`): `legacy-broadcast.service`
stopped with result success, status 0 and no restart, every `LSHUB_SRV_EXIT` the hub logged for one
it started has `EXIT_CODE` 0, and no crash report names it. Its table has `core-boot-done` (the guest's uptime at it)
and `tvservice` where the default has boot-done and the screenshot:

```sh
scripts/regress --headless --keep-going                  # every target on the Pi
scripts/regress --headless --local --target h13-afro --timeout 900   # this PC, TCG: slow, give it time
scripts/regress --headless --local -j 6 --keep-going --timeout 900 # this PC: six VMs at once
GUEST_KERNEL=x86-64 scripts/regress --headless --local --target kf23f-number1  # this PC: x86-64 kernel under KVM, the ARM userland under qemu-user
```

On an x86-64 PC, `GUEST_KERNEL=x86-64` runs the lanes on the x86-64 guest kernel under KVM with the
whole ARM userland under qemu-user ([the x86-64 guest kernel](../architecture/qemu-user-bridge.md#the-x86-64-guest-kernel-experimental)),
instead of the arm64 kernel under full TCG; a graphics lane then renders with virgl on the host's
render node. It does not apply to NetCast and SimpleSmart, which the launcher refuses with it.

#### Parallel headless runs on this PC

`--local --headless` runs its targets in parallel, `-j N` at once (default 2, each VM at SMP 4; "Two lanes" below). Graphics runs stay one
VM at a time (`-j` is refused). On the Pi the default is `-j 2` (`-j 1` is one VM in the default instance,
as before; see "Parallel headless runs on the Pi" below). Each target boots in an *instance* of its own,
`INSTANCE=<target>` (`host/scripts/vm-launcher --help`, `launch.instance_env`), so that several checkout VMs run
from one checkout without sharing a file:

| | default instance | `INSTANCE=<id>` |
|---|---|---|
| `RUN_DIR`, `LOG_DIR` (sockets, `qemu.pid`, `console.log`, `qemu.out`, `share/{host,vm,out}`) | `build/` | `build/instances/<id>/` |
| state (`STATE`'s default; `STATE=none`'s one temporary state of the unit's run, `.fresh`, kept across guest restarts and removed when the run ends; a foreground run's blank disks, `.blank`) | `build/state/{default,.fresh,.blank}` | `build/instances/<id>/state/{default,.fresh,.blank}` |
| `SAVED_LOG_DIR` | `build/logs` | `build/instances/<id>/logs` |
| systemd user unit | its development VM's, `beanjar-vm-<tag>-dev-<hash>`; regress: `beanjar-vm-regress-default` | its development VM's; regress: `beanjar-vm-regress-<id>` |

Every `host/scripts/vm-launcher` command (`start`, `stop`, `console`, `qmp`, `put`, `put-out`, `get-out`, `rm-out`, `logs`, `reset-state`)
takes `INSTANCE` from the environment; without it (or with `INSTANCE=default`) it is the default
instance, which `host/scripts/vm-launcher`, the regress run on the Pi and the manager's checks of "the checkout's
own VM" use as before. Each instance is a VM with a slot of its own, so its ports are on its own
address, `127.0.1.<slot>`, at the stock numbers (`host/scripts/vm-launcher config ADDRESS`;
docs/dev/architecture/network.md, "Host addresses and ports"): the ssh step reaches each lane's root SSH
there. The parallel run uses `NET_LINK=nat NET_MODE=direct` by default, QEMU's own slirp, private to each QEMU
process; `NET_LINK=off` or `NET_MODE=mitm` (each lane's inspector on its own address) may be given instead. The guest's RAM is its model's, as every VM's (its board.cmdline's hwopt=, `nvm.board_ram`; `MEM` and `SMP` pass through).

`-j` is capped by the VM host (this PC, or the Pi, whose `nproc` and `MemAvailable` it reads over
ssh, at the start of the run): by the host's CPUs over 4 vCPUs per VM (`SMP`; on the Pi, KVM, two vCPUs per CPU),
by the number of targets, and by memory: the RAM of the guests that run together (each target's model RAM,
`targets.ram` through `nvm.board_ram`, or `MEM` for all) must fit in `MemAvailable` less a headroom of 1 GiB
for the host and a GUI (`HEADROOM_MB`). The test is on the worst case: the `-j` largest guests among the targets. The vCPU and the memory caps both
apply, on the Pi and with `--local`, and the run says which one capped. (A Pi running two guests by vCPUs alone has been seen 1.8 GB into swap with a standby 9.3 s late; the memory cap prevents that.)
On this PC (24 threads, 31 GB) that is at most 6. Targets start in `TARGETS` order as slots free up; a failure (without
`--keep-going`) or a leftover process starts no further target, and the running ones finish. Each target's
leftover check covers only its own instance and runs on the VM host (processes whose command line
names its `RUN_DIR` in that host's checkout, its unit there), so the Pi's lane never takes this PC's
same-named instances for its own; after the run the whole host is checked as before. The summary has each target's `total`
seconds (start to stopped) and the run's wall time; `<target>/host/meminfo-{start,ready,end}.txt`
hold the host's `free -m` and `/proc/meminfo` at the start, at ready and before the stop.

#### Parallel headless runs on the Pi

`scripts/regress --headless` runs two KVM VMs at once on the Pi (`-j 2`, the default; `-j 1` is serial), each in an instance of its own
as above (`NET_MODE=direct`, each model's RAM; the instance's directories are under the Pi's checkout). The
cap reads the Pi's `nproc` (4) and `MemAvailable` (about 5.9 GB with a desktop session running): CPUs at
two vCPUs each (KVM's idle vCPUs cost nothing), so `SMP=4` gives 2 VMs and `SMP=2` up to 4, and
each VM's RAM. Targets are started alternately heavy and light (`HEAVY`: h13-afro, m16p-dreadlocks2,
m3-goldilocks, `core-boot-done` about 20 s; the others 3 to 8 s), so two heavy VMs boot side by
side only at the end of the list. Graphics stays one at a time, and one regress run on the Pi at
a time (a `-j 2` run counts as one).

The measured local default is `-j 2` with the VM's default four vCPUs.

#### Two lanes: KVM and TCG

Every change is verified on both lanes: KVM on the Pi
(`scripts/regress --headless`, `-j 2` is fine) and TCG on this PC (`scripts/regress --headless
--local -j N`). A failure that is only a timeout under TCG load (bootd's `core-boot-done` timeout,
a step's `--timeout`, a standby CPU over `STANDBY_CPU`) gets a solo re-run (`--target <target>`, `-j 1`)
before it counts; a failure that stays in the solo run counts.

**bootd's timers under TCG.** bootd gives each boot signal a fixed wait and goes on without it
when the wait ends: under TCG tvservice's `tv-ready`
came after the 30 s or 60 s wait only because the guest is slow. The launcher's
`qemu.TIME_SCALE` (`kvm` 1, `tcg` 4) is the one factor for that. With `tcg`, the kernel cmdline
gets `webos.time_scale=4`. /init then writes a `bootd.override` whose script exports
`LD_PRELOAD=/usr/lib/libbootd-scale.so` before `exec /usr/sbin/bootd`; it is not an `env` stanza,
which would load the library into the job's shell, which drops it before the exec. The library
(`guest/layers/webos/src/bootd-scale`, in every webos layer) multiplies the waits of 5 s and more that
bootd arms with `g_timeout_add_seconds`/`g_timeout_add`, and the reply timeout of its synchronous
Luna calls (`LS::Call::get`, webOS 3 and 4: the first app's launch, 5 s), by 4. Then it drops itself
and the factor from the environment, so resourcemon's CPU windows and what else bootd starts keep LG's
timing. Under KVM nothing is loaded. 4 is from the 4-VM TCG runs: tv-ready came up to 94 s into
m3's 60 s wait and 45 s into h13's 30 s, 1.6 times the wait at most.

`scripts/regress` takes the same factor for the VM host's accelerator (`--local`: the one the launcher
picks here, TCG on x86 for the arm64 kernel, KVM with `GUEST_KERNEL=x86-64`; the Pi: KVM; `ACCEL` as given). Its own deadlines exist to catch hangs, so it stretches
them all by that factor: `--timeout` (each step, start, stop and the ready/boot wait) and the 20 s
past it, check-m1's boot-done wait, home-idle's 60 s, the standby's 60 s waits for the standby and
the wake, and `guest.sh resumed`'s 30 s. The steps get it as `TIME_SCALE=` on the line typed on the
console, and `guest.sh`, `check-m1.sh` and `app-calls.sh` stretch their own luna-send limits and
poll budgets by it. Under KVM it is 1 and nothing changes.

#### NetCast line

The NetCast-line targets (`lm14-netcast45`, `m2-simplesmart`) have no bootd, Luna or tvservice
for these checks to ask, so `scripts/regress` leaves them out (`targets.BRING_UP`).
`tests/regress/netcast/run.py --firmware FW` checks them instead (its README): a headless VM made
through the beanjar CLI must initialise our GAL, start its power stack, draw its stock dashboard
after HOME and change it on RIGHT, within `--timeout`. Under KVM the dashboard must be filled in (a
screenshot with enough non-black pixels and enough distinct colours); under TCG, where FX HOME stays
an empty tile skeleton, only drawn, and RIGHT is skipped. A black screen fails on both.

### Bring-up checklist

Both modes run a `checklist` step after app-calls, before the screenshot in a graphics run. It looks
for the pitfalls that came up again and again in bring-ups. `tests/regress/guest.sh checklist`
gathers facts and does no judging: it prints `name=value` lines into `checklist.txt`. One half runs
in the initramfs (busybox there has no `wc`, `sort` or `dd`). The other half runs in webOS through
`webos-enter`, with the firmware's busybox, which has `dd` and `od`. `tests/regress/lib/checklist.py`
judges the facts here. Each item gets PASS, WARN or FAIL and a one-line reason:

| Item | FAIL | WARN |
|---|---|---|
| `first-app` | bootd has no `firstAppId`, or `firstAppLaunched` is not true; or the EEPROM's Last_Input_info DB at the release's `Nvm.lastinput_offset` is blank (all 0xff or 0) or unreadable. There is no offset on webOS 1 and 2, so only the first app is checked there | |
| `region` | Something disagrees with the firmware's broadcast line: tvservice's `tvSystemName`, `option.country`, `countryGroup`, `contiLangCountry`, tvconfig's `continentname`/`rmmgroup`/`rmmcountry` (webOS 3 on), or the smart service country's prefix. Also FAIL when the smart service country is still `other`, or when there is no country | The line is unknown |
| `mount-readwrite` | The job failed in the kernel log; `$RWMOUNT_FLAG` was not made; or `/sys/power/pm_freeze_timeout` is not the value the job writes (webOS 4) | `preload/` is missing where the job reads it (webOS 3.5 on), or the job is not `stop/waiting` |
| `tvservice` | Upstart restarted or ended tvservice (`init: tvservice` in dmesg), or there is a crash report of tvservice | Crash reports from other processes (`/var/log/reports/librdx`, `/var/log/crashd`, `/var/spool/rdxd`), jobs killed by a crash signal, or core files |
| `sysprops` | luna-prefs' `getAllSysProperties` does not answer, answers an error, or has no `storageCapacity` (it fails as a whole when `/proc/partitions` has no `mmcblk0` line) | |
| `eeprom` | tvservice's `modelName` is empty (the ModelInfoDB was not read), or it differs from the bootloader's `modelName=` | |
| `modules` | lgemu.ko or a `SOC_MODULES` module is not loaded; its vermagic is not `uname -r`; dmesg has a stale-build sign (symbol version, unknown symbol, `i2c_adapters: cannot register`); or the kernel is tainted F (forced load) | |

The broadcast line is the firmware's own: its `model_id.json` region (`targets.firmware_broadcast`);
for a firmware before jhericurl, which has none, its pak names (`-dvb`, `-atsc` or `-arib`, as in
`tvservice-dvb.pak`), read from its directory here, or from the release's `Pak.file` names if the
directory is not here. Each line is matched against a list of
region codes. ATSC is USA, CAN, MEX and KOR (groups US, KR, CA, MX). ARIB is JPN (JP, JA). DVB is
any region that belongs to neither.

A WARN does not fail the target; a FAIL does. The table has a `checklist` column, for example
`WARN(tvservice)`. Below the table, each target's items that are not PASS are listed with their
reasons. `summary.json` has every item of every target under `checklist`.

#### Idle CPU

One more item is judged later, in a step of its own: `idle-cpu`, once Home is up and settled (a
graphics run: after the screenshot; headless, which presses no HOME: after ssh). It looks for a
process that spins on a value our emulation never supplies, as tvservice did on o18 (an HDMI
switch's ready GPIO, polled every 1 ms), legacy-broadcast-atsc on k25lp (a tuner property, read in
a busy loop) and tvservice on k2l (a demux node that always polled readable). The guest's load
average is no use for this: real sets sit at load 15 to 34 while 76 to 90 % idle. So
`tests/regress/idle-cpu.sh` (sourced by `guest.sh`, run in the initramfs, whose `/proc` sees every
webOS process) takes three snapshots 30 s apart of each process's and thread's utime+stime and
each thread's voluntary and involuntary context switches, and `tests/regress/lib/idlecpu.py`
works out the CPU per process (% of one core) and the wakeups per thread for both windows:

| Item | FAIL | WARN |
|---|---|---|
| `idle-cpu` | The sampler did not finish | In both windows, a process above its CPU limit, or a thread waking above the wakeup limit (KVM and emulation alike for now) |

| Limit | KVM | qemu-user (x86-64 guest kernel) | TCG | Evidence |
|---|---|---|---|---|
| process CPU, default | 25 % | 100 % | 100 % | the busiest healthy process on 34 targets under KVM is servicemanager at 10-11 % on three ponytail boards, then pqcontroller at 4 %; k25lp's busy loop was 86 %. Emulation costs several times the CPU (o18's tvservice about 11 % under qemu-user against 2-4 % on KVM) |
| surface-manager | 50 % | 200 % | 200 % | 15-20 % on real sets |
| thread wakeups | 500/s | 500/s | 250/s | the busiest stock thread wakes 142/s (W19P's CCThreadProc, a 5 ms sleep); o18's 1 ms poll woke 640-800/s, and only 290-596/s under TCG: emulation lowers the rate, so it is never scaled up |
| lginput2's threads' wakeups | 4000/s | 4000/s | 4000/s | it polls the input devices: about 1980/s on a real webOS 6 set at idle, 1840-1960/s on k24 and k24n under KVM |

The two windows rule out a one-off burst (the Home page settling, a once-a-minute job); a spin is
steady. 30 s at 100 ticks a second resolves 0.1 % of a core. For each flagged process a second
guest step, `idle-threads`, samples its threads 10 times 0.2 s apart (state, `wchan`, the syscall
from `/proc/TID/syscall`, named for arm or x86-64), and the report lists its busiest threads with
their CPU and wakeups per second: a busy loop holds the CPU with few voluntary switches, a
short-sleep poll has many. The step adds about 65 s to a lane (2 s more for a flagged process).
The numbers of every process above 0.5 % of a core go into `<target>/idle-cpu.json`, the raw
snapshots into `<target>/idle-cpu.txt`; the table's `idle cpu` column names the busiest process
(`PASS tvservice:3%`) or the flagged ones (`WARN(legacy-broadcas)`, the comm as the kernel cuts
it), and the lines under it give each target's numbers; `summary.json` has them under `idle_cpu`.

### TV feature suite

`scripts/regress --tv` tests what the TV offers beyond the boot, separate from the default run so
that one does not get slower. It boots as the default run does (`--headless` too, `-j` and
`--local` as there), then runs its steps in place of the boot checks (`luna-info`, `network`,
`apps`, `tee`, in graphics `draw`, `audio`, `decode` and `smp`; `--tv-steps LIST` runs only some), and stops:

```sh
scripts/regress --tv --headless --keep-going             # every target on the Pi, two at a time
scripts/regress --tv --keep-going                        # graphics: one at a time
scripts/regress --tv --headless --local --keep-going     # this PC, TCG
```

The suite needs `NET_LINK=nat NET_MODE=direct` (the VM's own network namespace: the guest is 10.0.2.15, the router
slirp4netns's 10.0.2.2, DNS the LG services mock's 10.0.2.254; [network.md](../architecture/network.md)), which `--tv`
sets; any other network is a usage error. Its test server, `tests/regress/http-token.py`, runs in that namespace
(`host/scripts/vm-launcher netns-exec`) at 10.0.2.254, beside the mock: only that VM's guest reaches it, and the
guest still never reaches the host's loopback. A graphics run
adds the `devmode` layer (`LAYERS=normal,gl,devmode`, unless `LAYERS` is given) for the test app's
install. luna-info and network only read: no setter is called; the audio step changes the
volume and puts it back.

- `luna-info`: `tests/regress/tv.sh luna-info` (in webOS, through `guest.sh`) calls each method LG
  documents for apps on its own, every documented key in its own call, once bootd has sent
  boot-done (a headless run is ready earlier, before the connection manager is; it waits up to
  150 s, and fails `boot-done` without it). Where there are two hubs
  (webOS 1 and 2, `ls-public.conf`) each call goes to both, the public one as an app's
  (`luna-send-pub`) and the private one, and the check takes the public answer, else the private
  one, naming which: their methods differ (W15M's connection manager has `getStatus` on the public
  hub only), and `luna-send-pub`, a public client without an app's permissions, is refused by
  settingsservice's caption keys and tvservice's public key table where the private hub answers: `tv.systemproperty`
  `getSystemInfo` (`boardType`, `firmwareVersion`, `modelName`, `sdkVersion`, `UHD`, `_3d`),
  `settingsservice` `getSystemSettings` (`option`'s `country`, `smartServiceCountryCode2`,
  `audioGuidance`, the 14 `caption` keys, `localeInfo`), `getStatus` of `com.palm.connectionmanager`
  and `com.webos.service.connectionmanager`, `sm` `deviceid/getIDs`, `systemservice`
  `time/getSystemTime`, `applicationManager` `getAppLoadStatus`, `mrcu` `getAPIVersion` and
  `sensor2/getSensorState`, `camera` `getList`, `blegatt` `getState`. Then tvservice's own identity
  keys (`otaId`, `hardwareVersion`, `serialNumber`, `tvSystemName`, `contiLangCountry`,
  `platformName`) and `getCurrentCountryGroup`, and every query `nyx-cmd -h OSInfo` and `-h
  DeviceInfo` list, each on its own (the first line is the value; anything else the query prints
  is kept apart, as `.err`). `tests/regress/lib/tvsuite.py` checks the documented fields of each answer
  and the values against the target: `modelName` and nyx's `product_id` are the board's model
  (board.cmdline), `firmwareVersion` and nyx's `webos_manufacturing_version` the firmware name's
  version, `otaId` and `hardware_id` the OTA id of the firmware's own files (the guest's `etc/starfish/model_id.json`, `model_id` or the model pak's `RELEASE.cfg`; not the collection's name), `sdkVersion` the major.minor of
  `etc/starfish-release` (webOS 1 to 4 answer `4.10.0` for 4.10.2), `boardType`
  `<chip>_<LINE>_<area>` (the board's chip without its revision, the firmware's broadcast line, an
  area of that line), `UHD` the model name's series letter (`U`), the serial and MAC the VM's,
  `ram_size` the board's RAM, the country, groups and service country of the line (as the
  bring-up checklist's region), nyx's release, codename and build id the firmware's release, and
  `getSystemTime`'s `utc` within 15 minutes of the VM host's clock. Wi-Fi and Bluetooth are
  absent by design (no adapter; out of scope): nyx's `wifi_addr` and `bt_addr` pass when the guest
  reports them not available and fail if they ever answer a value (the table's `design` rows,
  "absent by design: no Wi-Fi/BT hardware (out of scope)"). `nyx.OSInfo.output` and
  `nyx.DeviceInfo.output` fail when any query that answered printed more than its value, on stdout
  after it or on stderr (a library's messages, such as MStar KADP's on W16R and W18R; the
  design-absent queries and the kernel config, which is many lines, aside). The only lines it
  accepts are those the table names for that firmware, exactly, with the code that prints them:
  W16R's KADP runs `MsOS_MPool_Close` (closing the `/dev/semutex` fd) before `MDrv_MMIO_Close` in
  its own `.fini_array`, so every KADP process prints the failed take of `_MMIO_Mutex` as it
  exits, on a real set too (`tv-expect-gen.py` reads the order, the messages and the mutex name
  from `libkadaptor`).
- `network`: `tests/regress/http-token.py` serves a random token in the VM's namespace for the
  step's length. `tv.sh network` reports eth0's address and routes, the resolver, the connection
  manager (`getStatus` under both names; webOS 1 and 2 have only `com.palm`'s `getstatus`), a
  lookup of `example.com` with busybox's `nslookup`, which every firmware has (its reverse lookups
  take up to 20 s; W17H's glibc 2.19 answers `getent ahosts` with nothing where `wget` and `curl`
  resolve) and the token fetched with `wget`. The checks: `dhcp-lease` (eth0 10.0.2.15, default
  route via 10.0.2.2), `connman-wired` (connected on eth0 by DHCP, with that address, gateway and
  DNS 10.0.2.254), `dns` (an address the VM host resolves too), `http` (the token, and the server's
  log of the request from the guest's own 10.0.2.15) and `online` (`onInternet` and `isInternetConnectionAvailable` as connman's
  own check would find from the VM host: an answer from the firmware's `Ipv4StatusUrl` in
  `etc/connman/main.conf`, `http://lgtvonline.lge.com/`, or, with `TakeDnsIpResolutionAsOnline`,
  its name resolving). `dns` and `online` depend on the VM host's own network; the HTTP fetch does
  not.

Every check is PASS or FAIL; there is no WARN. A check is SKIP only where the expected table,
`tests/regress/tv-expect/<target>.toml`, says that firmware does not have the API, method, key or nyx query,
and the SKIP cites the table's evidence. A check the table does not list fails. The table is
generated, not written: `tests/regress/tv-expect-gen.py` reads each target's firmware (the one the
run picks) with `unsquashfs` and records the release, the paks' broadcast line, connman's online
check and, for each check, `present` and its `source`. It claims absence only on evidence:

| Absent when | Source cited |
|---|---|
| no role (`usr/share/ls2/roles`, `luna-service2/roles.d`: `allowedNames`) or `services.d` file provides the service (`*` of luna-send and node does not count; `com.webos.service.tv.*` is tvservice's where the firmware has `tvservice-dvb.pak`; a `com.palm.` name ls-hubd maps is its `com.webos.` namesake's) | the directories searched, and the LG API reference's word for that webOS version |
| the method is not in the LSMethod tables the executable's code registers for the service's name and the method's category on either hub (each `LSRegister`/`LSRegisterPubPriv` call's name, handle and bus, and each `LSRegisterCategory`/`...Append`/`LSPalmServiceRegisterCategory` call's handle, category and tables, followed through the constants the code loads; where the code does not show them, as in tvservice and C++ services, the string test below) | the tables and the executable |
| the method's name is not a string of the service's executable nor of the firmware libraries it NEEDs | the executable |
| a `getSystemInfo` key is not a string of that executable; a settings key is in no record of its category in `etc/palm/defaultSettings.json` | the file |
| nyx-cmd's plugin (`libnyxcmdModule<M>.so`) has no such query; the DeviceInfo module's `device_info_query` sends the query type to the case of the battery's two types (`NYX_DEVICE_INFO_BATT_CH`, `BATT_RSP`: not implemented), read from its jump table (ARM `addls pc`, Thumb `tbb` or `ldr.w` forms); or the module runs `hcitool`, which the firmware lacks | the plugin or module (rootfs or the `otycabi` pak) |

A string being there proves only that the check runs: an API the firmware lacks some other way
shows as a FAIL, to be looked at. Regenerate after adding a target or changing a check
(`tests/regress/tv-expect-gen.py`, `--target` for one; it rewrites only the files of the targets it
runs for). A target's file has one line for each check, decode case and part of an app (an inline
table, its keys sorted), so a fix across many targets is a `sed` or a short script and its diff is
small; a check's `source` names an entry of the file's `[sources]`, which holds each evidence
string once, and the suite prints the evidence. Such hand edits are fine, but the generator is the
source of truth: make the same change there. `tests/unit/regress/test_tvsuite.py` tests the
judging, the generator's parsing and the files.

- `apps`: each stock app the table names for the target (the table's `apps`, from the
  firmware's `usr/palm/applications/<id>/appinfo.json`), Settings first: Settings
  (`com.palm.app.settings`), the web browser (`com.webos.app.browser`), Live TV
  (`com.webos.app.livetv`), the input picker (`com.webos.app.inputpicker` on webOS 1,
  `com.webos.app.inputmgr`, Input Hub, on 2 to 4; none from 4.5, SKIP), HDMI1
  (`com.webos.app.hdmi1`), Home (`com.webos.app.mystarter` on 3 to 4, `com.webos.app.home` from
  4.5; on 1 and 2 the compositor's StarfishLauncher, which HOME opens and HOME again hides), Home
  Dashboard (`com.webos.app.homeconnect`, from 4.5) and the User Guide
  (`com.webos.app.tvuserguide`). `tv.sh app-launch` has sam launch it and waits until it is in
  front (sam's `getForegroundAppInfo`, or a compositor window of its: Home is an overlay); the
  remote's DOWN DOWN RIGHT LEFT UP (`ir-key.py`); after `--settle` s it must still be in front in
  the same process (its pid in sam's `running`, WebAppMgr's browser process for a web app, not its
  short-lived Chromium children, the compositor's), and
  its screenshot (`dbus-grab.py`) not black; then `closeByAppId` must end that process (Live TV,
  the base app, comes back as a new one when nothing else is in front). The Home app of webOS
  4.5 on (`com.webos.app.home` with `nativeLifeCycleInterfaceVersion` 2 in its `appinfo.json`:
  the table's `stays`, from `tv-expect-gen.py`) answers `closeByAppId` and stays up, as a real
  webOS 6 set does (checked: the same Home process still running 6 s after a `closeByAppId` that
  answered true); its close must answer true and leave the same pid running. Then our test web app
  (`tests/regress/tvapp`, packed here by `ares-package` into the run directory) is installed as
  `ares-install` does, with appinstalld's `dev/install` (developer mode: the `devmode` layer), and
  launched the same way; it is removed with `dev/remove` once the steps are done (`app.install`,
  `app.remove`). No crash report (crashd's `/var/log/reports/librdx`) of the app's process (its
  executable, or the binary its launcher script execs: the table's `process`, the browser's
  `chrome`; WebAppMgr for a web app, `qml-runner` for a QML app) or the compositor's may appear
  from its launch to its close; other processes' reports are named in the reason. LG's own
  crashes of `tvsuite.EXPECTED_CRASHES` are the exception, each matched by target, lane, signal and
  frames (`tv.sh` prints every report's `crashsig.<pid>`) and named in the PASS reason: W20L's
  browser aborts when closed with its first-use dialog open, and the Realtek browsers segfault
  headless without a display. Headless
  launches every app too, Settings first, with no keys or screenshot, but none can stay up there:
  no WebAppMgr and no compositor, and the layer's stand-in for the input apps' binary ends at once
  (each row's `headless` in the table says why). Each passes when it is not running and sam
  still answers, looked at 5 s after the launch and again every 5 s up to six times (an app that
  ends by crashing stays in sam's list while crashd writes its report, tens of seconds under TCG
  with other VMs), as the Wi-Fi rows do, and fails if it stays up or leaves a crash report that
  is not an expected one.
- `tee`: where the firmware has a TEE client (the table's `tee`: `tv-expect-gen.py` lists
  every ELF file of the target's paks that names `/dev/tee`, the Realtek targets' libkadaptor and
  tee-supplicant, and the TAs in `usr/lib/optee_armtz`; elsewhere one SKIP citing that none of the
  N files does), `tests/regress/tv.sh tee` runs `tests/regress/tee/tee-probe` (`build/tests/regress/tee`) in webOS
  through the firmware's own libteec (libkadaptor's `TEEC_*`, loaded at run time) against rtk.ko's
  `/dev/tee0`. Checked
  (`tvsuite.judge_tee`): `/dev/tee0` is a character device; the secure-store TA's session opens and
  each TA the firmware ships answers TEEC_ERROR_ITEM_NOT_FOUND from the TEE (no stand-in, by
  design); each secure-store command the firmware's clients use answers as designed, with its round
  trip (MakeClearKey/MakeKey to a blob and back, an altered blob refused, Encryption and Decryption
  plain and as a container, the store's MAC and VerifyHMAC, ReEncrypt, MakeNewSecureData) and the
  unmodelled ones (RPMB, DRM key, MFi) TEEC_ERROR_NOT_SUPPORTED; every output is the one
  `tests/regress/lib/teeref.py` makes from the VM's own `identity/tee-huk` (read on the VM host), so
  nothing but that generated key is behind them; and no crash report (crashd's librdx) of a TEE
  client (one naming `TEEC_` or sstore) since the boot.
- `draw` (graphics): the test app draws `tests/regress/tvapp/pattern.js`, six solid patches over a
  background, with canvas 2D and then with WebGL (scissored clears; `{"mode":"webgl"}` in its
  launch parameters, another background colour so a stale frame does not pass), drawing the same
  frame again on every animation frame as an app's render loop does (LG's QtWebKit shows a WebGL canvas one frame late). The screenshot
  must show each patch and the background where the pattern puts them (90 % of the samples of a
  patch's middle within 48 of each channel); a black screen, the UI over black, or the page's
  no-WebGL colour fails. The page logs what it drew to the console (`TVSUITE ...` in the
  guest's messages).

- `audio`: the guest plays tones through LG's own sound path while QEMU records the card on the VM
  host: `tests/regress/audio/capture.py` starts HMP `wavcapture` (over QMP) on the card's audiodev, whatever
  backs it (`dbus` in graphics, `none` headless and under TCG), into the target's `audio/*.wav`, so
  nothing reaches a speaker. `tv.sh audio` (the table's `audio`: the Audio API's service,
  `com.webos.audio`, its `getVolume`, `setMuted`, `volumeUp` and `volumeDown` as audiod registers or
  names them, and `usr/bin/paplay`) first makes one master volume change and its undo
  (`volumeDown`, `volumeUp`: until one, W18R's and W19P's audiod gives the first app stream 90 % of
  `pdefaultapp`'s volume, every later one 100 %), then plays a 2 s, -12 dBFS tone with `paplay`
  (PulseAudio, as a native client) per phase, each phase its own frequency so the recording tells
  them apart: the set's volume (1000 Hz), after five `volumeDown` (750 Hz), muted with `setMuted`
  (1250 Hz), unmuted and five `volumeUp` (500 Hz), each change read back with `getVolume`, the
  public hub's calls where there are two. At the set's volume it also plays one with `aplay` to
  ALSA's `default` (875 Hz; LG's `asound.conf`: the pulse plugin on `pdefaultapp`, the route a
  browser's ALSA output takes) and, once PulseAudio has suspended `pcm_output`, one with `aplay -D
  hw:0,6` straight to that PCM (1125 Hz; `system.pa`'s `module-alsa-sink` device; PulseAudio
  bypassed), both as a webOS 6 set does (its `aplay` plays both routes). In graphics the test app
  (`tvapp`, `{"mode":"audio"}`) then plays a 660 Hz WebAudio oscillator at the same peak level,
  recorded on its own. The VM host
  measures each frequency's level (`tvsuite.py tones`: the windows of 46 ms that carry 80 % of
  their energy in it, their median level) and `tvsuite.judge_audio` checks: `audio.native` (the
  first tone heard), `audio.readback` (getVolume 5 lower, then muted, then back unmuted),
  `audio.volumeDown` (at least 1 dB lower), `audio.muted` (no 1250 Hz above -90 dBFS),
  `audio.restored` (within 0.5 dB of the first tone), `audio.calls` (a setter refused),
  `audio.aplay` (within 1 dB of the native tone), `audio.alsa-hw` (heard) and in graphics
  `audio.webaudio` (heard, within 1 dB of the native tone plus the row's `level_db`: W21P's
  Chromium opens ALSA `feedback`, PulseAudio's `pfeedback`, which audiod keeps at policy level 80,
  palm-policy's -8.08 dB, as on a webOS 6 set; the others open `default`, 0 dB). The volume follows the model's
  own curve in every target (lgemu.ko's playback gain). WebAudio is compiled into every firmware's engine (QtWebKit's
  `AudioDestinationGStreamer`, Chromium's ALSA output, to PulseAudio's `pdefaultapp`, W21P's to
  `pfeedback`); HTML5
  media goes through umediaserver and is the decode step's.

- `decode` (graphics; headless SKIPs every case: no web engine or viewer there): the test app
  plays clips made at test time (`tests/regress/video/make-clips.sh --decode`, packed with a copy of the
  app in the run directory, never committed): 20 s of testsrc2 at 960x540 25 fps with the case's
  name and the media time burned in and a beeping 1 kHz tone. `<video>` cases (`file.*`) play a
  file: H.264+AAC and HEVC+AAC in MP4, VP9, VP9+Opus and AV1+Opus in WebM, MPEG-2+AAC in TS; MSE cases (`mse.*`, the
  page's own Media Source player: one SourceBuffer per track, the init segment and 2 s fMP4 or
  WebM segments appended in turn, then `endOfStream`, through whichever API the engine has:
  `MediaSource`, WT1H's prefixed `WebKitMediaSource` with `append`, or the 2012 draft's
  `webkitSource*`) H.264+AAC, HEVC+AAC, VP9, VP9+Opus and AV1+Opus (AV1 decoded by libdav1d on the
  VM host). The VM's media helper decodes what the guest's
  sinks send; one viewer presents its decoded output: the `--viewer` window, which runs with media on from QEMU's
  start to stop, or, without `--viewer`, a headless one for the step. The case records the helper's
  decoder log, the viewer's presentation log and media audio (`--media-silent
  --media-wav`: heard nowhere) and, while the run dir's `regress-capture.ctl` names a case's
  directory, a 480x270 capture a second there. For each case `scripts/regress` notes both logs'
  and the audio's sizes, launches the app with `{"mode":"decode","case":...}`, waits for the page's
  end, closes it, and gives the case its parts of both logs and the audio (`tvsuite.py slice`). The page logs every event (and `canPlayType`/`isTypeSupported` of its MIME
  types, the MSE calls, `error` codes) to the console and, since not every firmware's WebAppMgr puts
  the console in PmLog (WT1H's does not), as image GETs to the suite's server
  in the VM's namespace (`http-token.py`, at 10.0.2.254). `tvsuite.media_facts` (on the VM host) and
  `tvsuite.judge_decode` check, per case: the page's `playing`, `currentTime` to within 1.5 s of
  the clip's end, `ended` and no `error`; the host's decoder for the case's codec, 75 % of the
  clip's pictures shown with their pts spanning the clip, the video's end of stream, a scaler
  window received and pictures drawn into it; at least 3 captures with a decoded picture at the
  page's video rect (colourful, not black, not the milestone 1 pattern; frames during the app's
  open transition simply do not count) and 2 changes between consecutive ones, and at least 3 with
  the whole clip picture filling that rect (`tvsuite.whole_picture`: the six testsrc2 bars in
  order, the picture at each edge of the rect, the label box), so a picture drawn too large, too
  small or offset fails; for cases with audio (AAC or Opus) the
  audio decoded for all but 2 s of the clip and recorded above -40 dBFS; no new crash report.
  Neither `ended` nor the end of stream is wanted where the case's row has `"ended": {"expected":
  false}`: W16R's engine (Chromium 38's `TvRenderer`, `libcbe.so`) pushes the pipeline's end of
  stream and reports the end only after the audio stream's, so an MSE stream without audio plays
  to its last frame and the page gets `waiting` (`tv-expect-gen.py`'s `mse_audio_eos_of` finds
  that gate in the engine's code). A case is SKIP where the table's `decode` row says the firmware's stack does not take it,
  citing why: `tv-expect-gen.py` reads the stock decoder elements' claim (gst-omx's
  `/etc/xdg/gstomx.conf` codec sections on MStar and Realtek, else the sink pad caps of LG's
  `vdecsink`/`omx_lxvideodec` and `adecsink`/`audio_dec`), umediaserver's
  `device_codec_capability_config.json` where there is one (it must list the codec too), the
  container's demuxer, and for MSE the engine's own strings (the MSE API, the container's MIME type
  and the codec string of each source buffer: `avc1`, `hvc1`/`hev1`, `vp09`/`vp9`, `av01`, and
  `audio/webm` with `opus`; the AAC buffers are not looked up). Opus and AV1 are SKIP wherever the
  stock elements (`omxopusdec`/`omxav1dec` sections, `audio/x-opus`/`video/x-av1` pad caps) or
  umediaserver's table (`OPUS`, `AV1`) do not have them, which leaves out WT1H, W15M, W16R, W17H,
  W18R and the LM21A sets (no `OPUS` in their table; every new case has Opus), and W19P's AV1. Each case's files are in the target's
  `decode/<case>/` (media-helper.log, viewer.log, media.wav, and of its captures only `sheet-<ms>.png`, the one about
  10 s after its first decoded picture) and `decode/<case>.*.txt`; `decode-sheet.png` in the run
  directory puts them side by side, labelled target and case, for a look at what played.
- `smp` (graphics; headless SKIP): a native app's own media pipeline, as moonlight-tv's (ss4s)
  plays its stream. `tests/regress/smp/pack.sh` packs the SMP check app (`tests/regress/smp`: `smpcheck`, the
  guest build's `build/tests/regress/smp/smpcheck`, with an 8 s 720p30 H.264 clip made at test time) with
  `ares-package`; it is installed with `dev/install` and sam launches it (`tv.sh smp`). It opens a
  full-screen transparent window as SDL's webOS window does (the app is then the foreground one),
  registers with ACB (`libAcbAPI`: media id, full-screen display window, sink, states) and loads
  `libplayerAPIs`' StarfishMediaAPIs with a BUFFERSTREAM H.264 payload, feeds the clip's access
  units at 30 fps, then starves 3 s, pushes EOS and unloads, logging every callback to PmLog
  (`SMPCHECK`). `tvsuite.judge_smp`: LOADCOMPLETED, FRAMEREADY (type 0, the shown frame's PTS)
  at least twice a second of feeding, never going back and reaching the clip's last frame within
  0.5 s, the viewer showing 75 % of the clip's frames in a window, no crash report. SKIP, without
  installing, where the table's `smp` row says the firmware's `libplayerAPIs.so.1` does not
  export a name of each of the app's `SYM()` lookups (`tv-expect-gen.py` reads its `.dynsym`:
  webOS 1's has no `StarfishMediaAPIs(const char *)`, webOS 2's `Load` takes no user data), or
  the app finds no library. Then `dev/remove`. The run's lines are in the target's
  `smp/run.txt`, the viewer's in `smp/viewer.log`, and one capture in `smp/sheet-<ms>.png`. The
  jail a native app runs in has the SoC's device directory `/dev/lg` but no `/dev/vport*`: the
  media ports are `/dev/lg/webos.media.<n>` (the initramfs makes them).

Each target's screenshots, with the facts of each launch, keys and close, are in its result
directory's `apps/` (`<role>.png`, `testapp.png`, `draw-canvas.png`, `draw-webgl.png`), and
`gallery.html` in the run directory shows them all.

The table has, per target, a column per step, each its verdict and PASS/FAIL/SKIP counts
(`FAIL 61/3/12`), and below it every FAIL with its reason; each target's `luna-info.txt`,
`network.txt`, `network-host.txt` (the VM host's own lookups) and `http-server.txt` are in its
result directory, and `summary.json` has every check under `tv`.

## Build and boot internals

The [build and boot architecture](../architecture/build-and-boot.md) describes the source
layout, layer inheritance, settings ownership, downloads, two guest architectures, kernel
interfaces and initramfs. The commands and package requirements remain in this guide.

## Incremental builds

- Compiled code: Meson/Ninja as usual. Layers, `build/ssh/bin` and the test programs are
  assembled by `meson/stage.py`, which writes a depfile of every input (source directories
  included, so an added file counts) and a stamp that changes only with the result; so touching
  one stub source rebuilds that program and its layer, and nothing else. The initramfs itself is packed from its sources and build outputs
  directly (above, "Initramfs"): touching `guest/initramfs/rootfs/init` repacks only it.
- External builds (kernel, busybox, SSH, the Mesa stack, QEMU) re-run when their unpacked tree,
  defconfig or config fragment, options header, cross file or build script changes; inside,
  make/Ninja are incremental, and installed files are synced by content, so Mesa is not relinked
  by a re-run.
- What does not depend on the whole target is built once and shared by the build directories
  that would build it the same, in `<shared_dir>/<component>/<key>` (the option `shared_dir`, default
  `build`): the arm64 kernel with its out-of-tree modules (`kernel/<key>`: `linux-obj` and a
  directory per module), busybox (`busybox/<key>`), the SSH programs (`ssh/<key>`) and the Mesa
  stack (`mesa/<key>`). Each key hashes what the targets sharing it have alike and nothing that
  names a target, chip or release: the kernel's and busybox's the arm64 compiler prefix, the source
  tree and (the kernel) the defconfig's name, so every target has the same one; SSH's the cross
  file's host, compiler, flags and strip, so every webOS target has the same one (NetCast 4.5 and
  SimpleSmart build no SSH). A target's build directory copies the results from there and builds
  nothing of them.
- `meson/shared.py` runs those builds except Mesa's: it locks the directory, and skips the build
  when the directory already holds the result of the same inputs (the tree's wrap key, the
  defconfig or fragment, the options headers, the module sources, the build script, the compiler's
  `--version`, and for a module the kernel's and `lgemu`'s inputs). A changed input runs make again
  in the same directory, incrementally, once; the other build directories then find it done. A
  depfile on the directory's `.inputs` lets a build directory notice a result another one changed,
  or a removed directory. In the guest package lane (`scripts/build-packages.sh guest`, 4 CPUs,
  2026-11-11) this made an ordinary target's build directory 6 to 7 s instead of 82 s: before, each
  one configured and built Dropbear twice and OpenSSH (39 s), ran Kbuild over the kernel and its
  nine modules (29 s, compiler hits from ccache) and built busybox (10 s), one after another. The 33
  targets took 18 min instead of 57: the first target (the kernel without ccache, 8 min) and the
  first target of each Mesa key (about 105 s each) are most of what is left.
- The Mesa stack (libffi, libdrm, Wayland, wayland-protocols, Mesa) is built by `mesa-stack.sh`,
  in `<shared_dir>/mesa/<key>/{work,stage}`. Its key
  (`guest/layers/gl/mesa/meson.build`, made by `meson/shared.py key` as the others are) hashes what a target can differ in: the cross file's compilers
  and flags with its paths left out, the Wayland it builds against (the firmware's own 1.20 or 1.22,
  else 1.26), whether Mesa links `libwayland-server-compat` (webOS 1 and 2), the EGL platforms and
  extra options (NetCast 4.5). What every target has alike (the
  source trees, `mesa-stack.sh`) is not in the key: a change to it is built incrementally in each
  shared directory, once, and the other targets then find nothing to do (each target still runs
  the inner builds' no-op, about 2 s). `mesa-stack.sh` holds a lock
  on the directory, so targets built in parallel wait for each other, and each target's Mesa steps
  depend on the directory's manifest (a depfile), so a target notices a stage another target changed or
  a removed `build/mesa`. On 2026-11-03 the 30 targets with
  Mesa had five keys: Wayland 1.26 (19 targets), 1.26 with the server compat library (`h13-afro`,
  `m14-beehive`), 1.22 (3), 1.20 (5) and NetCast 4.5.
- A key no target uses any more stays until its directory is removed, which costs one rebuild of
  each key still in use.
- rootssh, devmode and hbchannel are made by their own scripts in `guest/layers/<name>/`
  (`meson/layer-script.py` runs them and writes the layer's stamp, rewritten only when its tree
  changes, and a depfile of the script's inputs). They hold no keys: they are the same for every VM, and each VM's SSH keys reach the
  guest through its per-VM layer.
- `publish` runs every time but only stats files when nothing changed.

## What may be distributed

The published build outputs are the same for every VM and every user of a firmware family.
Everything that makes an install, a VM or a user itself lives outside
them. That line is what a package of Beanjar follows. The list below was audited on 2026-09-29
against the x86 checkout's `build/` and the tracked files.

**Shippable:** built from this tree and the wraps only.

- `build/Image`, `build/{lgemu,lg115x,mstar-utopia,rtk,mtk,btmtk_usb,webos_tv,dtv_soc,lgfs}.ko`, `build/initramfs.cpio.gz`.
- `build/components/{common,soc/<chip>,release/<release>,gl,...}` (common, soc, webos, gl, rootssh, devmode, hbchannel, tcg-perf),
  each layer's `build/components/<name>/layer.toml`, and
  `build/ssh/bin`. The layers are linked against the firmware's libraries but contain none of
  them. The soc layer's `libhal_crypto.so.0` has the emulator's own model secret compiled in, which
  is not LG's (a SHA-256 of a fixed string, `guest/layers/soc/src/libhal_crypto/hal-crypto.c`).
  The hbchannel component is the exception to "this tree and the wraps": it carries Homebrew
  Channel's pinned release IPK unpacked (MIT; its prebuilt ARM tools and Mozilla's public CA
  bundle, `certs/cacert-*.pem`, which `packaging/shippable.py` allows by that path).
- The published test programs, but not the results beside them: `build/tests/{gl,gm}`,
  `build/tcg-perf/{perf-arm,io-perf}`,
  `build/devmode-tests/native-test`.
- The host build's `build/viewer/`, `build/manager/beanjar.gresource`, the prebuilt
  `build/qemu`, `build/ffmpeg` and `build/neatvnc` (beanjar-vendor's, with their licences), and
  runtime sources: `host/core`, `host/ui`, `host/helpers`,
  `host/scripts/vm-launcher`, `guest/scripts/` and source-only guest layers.
  Development build and research tools are excluded from the distribution.
- The tracked test fixtures, e.g. `tests/fakes/trust-rootfs.pak` (a SquashFS of two test
  certificates, not the firmware's).

Reviewed README screenshots in `docs/assets/screenshots/` may be committed to this repository. The
firmware exclusion concerns LG binaries and paks, not those images; raw captures stay out of packages.

**Never in a package:**

| What | Where | Why |
|---|---|---|
| Legacy checkout migration backups | `local/`: `model-secret`, `ca/` (the proxy CA's key), `*_host_key`, `devmode_rsa`, `id_ed25519`, `authorized_keys`, `known_hosts` | secrets, and the user's keys |
| A library | `~/.local/share/beanjar` (or `BEANJAR_LIBRARY`): `vms/<name>/{identity,state,logs,run,firmware}`, `ca/` | per-VM keys and model keys, TV state, the user's firmware (each VM's own) |
| The user's firmware and what is made from it | the host's firmware collections (`firmware_dirs`), `build/firmware-cache` (uncompressed images, partition maps), a library VM's `vms/<name>/firmware/{*.pak,cache}` (or `vms/<name>/firmware-cache` for an external `fw`) | LG's files |
| The checkout VM's state and logs | `build/state`, `build/logs`, `build/share` (its runtime tree: the per-VM layer with its keys, and what the guest wrote), `build/console.log`, `build/webos.log`, `build/qemu.out`, `build/*.log` | TV state (NDUID, accounts, db8), keys, and logs that quote them |
| Captures | `*.pcap` (`build/intercept-*.pcap`, `build/net-tcg.pcap`), `build/netstub*.log`, `build/tvsvc-observe`, `build/localstorage.db`, `build/sdp-source-review` | LG's traffic, a real TV's, apps' data |
| Raw screenshots and recordings | `build/*.png`, `*.jpg`, `*.ppm`, `*.wav`, `*.bin` (input recordings), `build/tmp`, `build/vm-osd`, `build/walk`, `build/media-check`, `build/video-app` | unreviewed captures of the firmware's screens and sounds |
| Test keys and leftovers | `build/test-keys`, `build/authorized_keys.test-bak`, `build/fake`, `build/devmode-tests/*.log`, `build/boot-times`, `build/tcg-perf/<run>` | keys, or of one machine |
| References and scratch | `build/ref` (cloned repos), `scratch/`, the old `build/securestore`, `build/video` | not this project's, or not built any more |

The netproxy layer, the one guest artefact made from a CA of one install, is gone from the build
and the initramfs. `packaging/shippable.py` (in `meson test -C build/host --suite packaging`) checks the
published initramfs and what the package ships of `build/components`: the variants a target
selects, not stale ones left behind (the packager's selection, `meson/components.py`). It looks for a netproxy layer, a file named like a key or
secret (`*.key`, `*secret*`, `authorized_keys`, `*host_key*`, `*.pem`, ...) and a PEM private key
in any file. It skips when no initramfs is published. It cannot tell a file copied from the firmware,
because the paks are not in the checkout. That was checked by hand for the initramfs of 2026-09-29:
none of its files of 64 bytes or more is identical to a file of the WT1H `rootfs.pak` or
`tvservice-dvb.pak`.

## Debian packages

`packaging/debian/build.py` assembles `beanjar`, `beanjar-ui`, `beanjar-vnc`, `beanjar-web`,
`beanjar-qemu` and `beanjar-guest` from what the
build lanes published, after the tree is committed and rebuilt (for the guest the script does the
rebuild itself: it runs `meson compile` in every `build/guest-<target>` that the published
`selections.json` name, so no target ships from before a change to a shared source such as a Mesa
patch; a target without a build directory or that fails to build stops the package, and
`--no-rebuild` skips this; `meson/fetch.py` unpacks Mesa again when `subprojects/mesa.wrap` or its
patches change, so "rebuilt" means current sources); their versions come from
`packaging/VERSION` (packaging/debian/README.md, "Versions and compatibility"). The Pi builds only
its host and packages it with `--host-only`, which reads no guest input and needs no git, then
installs `beanjar`, `beanjar-ui`, `beanjar-vnc` and `beanjar-qemu` with apt beside the PC-built `beanjar-guest` of the same `0.Y`
(packaging/debian/README.md, "Updating the Pi"). The arm64 packages for other boards (and the
Pi) are cross-built on the PC against Debian's own arm64 libraries (`build/host-arm64`, above),
so their dependencies are Debian's, not Raspberry Pi OS's (whose FFmpeg has epoch 8). That pairing is apt's hint; at each start the
launcher checks what the guest payload has: the target's components and the module parameters its
`.ko` files declare (docs/dev/architecture/build-and-boot.md, "Host and guest compatibility").
The host packages for amd64 and arm64 are also built by the Packages workflow on GitHub, on every
push to `main` and pull request as a check, and on a manual run with the `.deb` files as the run's
artifacts, beside the released `beanjar-guest` of the host's `0.Y`. `beanjar-guest`, from every
target, is built by the Guest workflow. A tag `v<version>` of the public repository, pushed at the
commit to release, releases both: the two workflows put the host and the guest packages into one
release `v<version>` (packaging/debian/README.md, "Built on GitHub").

## Host packages

Both machines run Debian 13 (trixie), and every host dependency but the prebuilt QEMU, FFmpeg and
neatvnc (above, "The prebuilt programs") is one of its packages: the build bundles nothing else
(the wraps in `subprojects/` are sources of the guest, plus the virtual camera's usbipdcpp). A fresh machine is set up with the metapackages below, built from the control files in
`packaging/debian/build-deps/` (the one list of host packages; `equivs` builds them). Because an
installed package depends on them, `apt autoremove` cannot strip these dependencies. `meson setup`
reports a missing program or library by name.

Role `host`, both machines, for the host build (`build/host`) and to run VMs:

```sh
packaging/debian/build-deps.sh host
sudo apt install ./build/packages/beanjar-build-deps-host_*_all.deb
```

- Build: Meson 1.7 and Ninja; `patch` applies the wraps' `diff_files`.
- The viewer: SDL2, GLib, EGL and GLES. The per-VM media helper uses the private FFmpeg 7.1 for
  video/audio decoding and resampling; the role has the Debian libraries its `BUILDINFO` names
  (the codec libraries, VA-API, VDPAU and the rest), which linking against it and running it need,
  but no FFmpeg `-dev` package.
- The GTK windows (the viewer's and the VM manager's, `host/ui`): GTK 4.18 and libadwaita
  1.7.6 for Python (the minimum is GTK 4.12 and libadwaita 1.7; without them `beanjar-viewer`
  runs headless only and the manager has its CLI only), and `blueprint-compiler` (0.16.0) for
  their layouts. It checks every class and property against the installed typelibs, so a layout
  that needs a newer GTK or libadwaita than the host's fails to build.
- Tests: `python3-pytest` runs them ([testing](testing.md)); `python3-cryptography`
  (`test_device_id.py`); `xvfb` and the X Test library for the GTK checks (`check-gtk.py`,
  `check-gui.py`); Qt 5's public and private (QPA) headers for `test_native_input_qt.py`.
- Running: `dbus-daemon` (the D-Bus display's private bus), and for `NET_MODE=mitm` the network
  namespace's `slirp4netns` and `ip` (`host/core/beanjar/netns.py`),
  plus `python3-dnspython` for LAN SSDP/mDNS discovery independently of the optional proxy,
  `ssh`/`ssh-keygen` and `openssl` (the VMs' keys and the proxy CA). `squashfs-tools` for
  firmware inspection and extracting files used to prepare a VM. The optional
  `ROOTFS=uncompressed` cache also needs `mksquashfs` and `unsquashfs` 4.6 or later
  (without the tools the `auto` firmware cache is skipped).

`beanjar-vnc` is built on the private neatvnc 1.0 and aml 1.0; its pkg-config file names the
libraries neatvnc links in `Requires.private`, so the role has their `-dev` packages (GnuTLS,
nettle, GMP, libjpeg-turbo, zlib, libdrm, GBM, pixman), and libopus for its audio.

Role `host` has the prebuilt QEMU's runtime libraries (its `BUILDINFO`'s `Depends`), so every host
runs the published `build/qemu`. Debian's own QEMU (`qemu-system-arm qemu-system-modules-opengl`)
runs a VM only where `build/qemu` is not there; the `beanjar` package depends on it unless
`beanjar-qemu` is installed.

Role `host-arm64`, the x86_64 workstation, for the arm64 cross build (`build/host-arm64`):
the aarch64 cross compiler and binutils, `pkgconf:arm64` (its `aarch64-linux-gnu-pkg-config`),
`glib-compile-resources` (`libglib2.0-dev-bin`) and the GTK and Blueprint tools for the layouts,
`dpkg-dev` (dpkg-shlibdeps), and the host build's libraries as `:arm64` -dev packages, with the
arm64 runtime libraries the private FFmpeg links, installed beside the amd64 ones with Debian's
multiarch. On Debian 13 all of them co-install with their amd64
versions (no removals; apt only moved a few amd64 libraries to the same security update as
their arm64 twins):

```sh
sudo dpkg --add-architecture arm64 && sudo apt update
packaging/debian/build-deps.sh host-arm64
sudo apt install ./build/packages/beanjar-build-deps-host-arm64_*_all.deb
```

The x86_64 workstation also builds the guest (`build/guest-<chip>-<release>`), role `guest`:

```sh
packaging/debian/build-deps.sh guest
sudo apt install ./build/packages/beanjar-build-deps-guest_*_all.deb
```

Role `readers`, the firmware readers that run on the workstation only (`host/helpers/rtk/rpc-table.py`, whose cache a host that cannot run it needs copied):

```sh
packaging/debian/build-deps.sh readers
sudo apt install ./build/packages/beanjar-build-deps-readers_*_all.deb
```

- The guest: the webOS SDK in `/opt/arm-webos-linux-gnueabi_sdk-buildroot` for webOS and
  SimpleSmart, or the [lgtv SDK](https://github.com/openlgtv/buildroot-nc4) in
  `/opt/arm-lgtv-linux-gnueabi_sdk-buildroot` for NetCast 4.5 (neither is a Debian package; both
  are openlgtv/buildroot-nc4 release assets, whose pins `scripts/build-packages.sh` names);
  the arm64 cross compiler for the kernel, busybox and the input bridges, uapi headers
  for Mesa, Mesa's Python modules, `flex` and `bison` for Kconfig, `qemu-user` (devmode's tests),
  `glib-mkenums`.
- QEMU (the prebuilt one): its runtime libraries, among them Debian's virglrenderer (1.1.0),
  libslirp (4.8.0), libfdt, libusbredirparser (0.15, for `usb-redir`) and libusb (1.0.28, for
  `usb-host`), which forward a real USB device to a VM's `qemu-xhci`.

What is not a package, and why:

- **mitmproxy** (`mode = "mitm"`) is the developer's or user's own, 12.0.1 or newer, neither
  built nor vendored: Debian 13 has no mitmproxy package (`apt-cache policy mitmproxy` shows no
  candidate on either machine), so it is `pipx install mitmproxy` (12.3.1 on 2026-11-13).
  `host/core/beanjar/mitmtool.py` finds it and checks the version; mitmweb runs in its own venv's
  interpreter, never mixed with Debian's packages. Without it a mitm VM goes out directly, and the
  tests that need it skip ([testing](testing.md)).
- **QEMU** 10.0.2, for its patches (below), **FFmpeg** 7.1.5, for the V4L2 Request API
  hwaccels Debian's lacks, and **neatvnc** 1.0.4 with **aml** 1.0, which trixie does not have:
  beanjar-vendor's prebuilt tarballs (above, "The prebuilt programs"). Their patches, build
  scripts and the reasons for each are in [beanjar-vendor](https://github.com/mariotaku/beanjar-vendor).
- Everything in the guest: the SDK, and the wraps built with it (busybox, Dropbear, OpenSSH,
  libffi, libdrm, Wayland, wayland-protocols, Mesa, GStreamer's headers, the kernel). They are
  32-bit ARM builds against the firmware's ABI, or arm64 for the guest kernel, not host
  dependencies. The two host programs made from them are part of those builds: Wayland's native
  `wayland-scanner`, which must be exactly the Wayland being cross-built (Debian's is 1.23), and
  the kernel's `gen_init_cpio`.

Debian's own QEMU (10.0.13) runs a VM wherever there is a DRM render node, but it keeps sending
every frame to a D-Bus display listener whose connection closed (each `beanjar screenshot` with no
viewer running leaves one), logging `Failed to call update` per frame per dead listener into
`qemu.out` (a Pi VM reached 43 MB); patch 0004 drops such a listener. On the x86 WSL box Debian's
QEMU does not render at all: it needs a render node for GL, which WSL does not have.
So every host runs the patched QEMU, beanjar-vendor's, which every host build publishes as
`build/qemu`. The launcher (`paths.Tree.qemu`) runs `QEMU` when it is set, else that QEMU,
`build/qemu` (`beanjar-qemu`'s when installed), for every VM, KVM or TCG, whenever it is there,
else Debian's QEMU from `PATH` (with `qemu-system-modules-opengl`).

On WSL, Mesa 25's software winsys takes llvmpipe unless
`GALLIUM_DRIVER=d3d12`; the launcher (`host/core/beanjar/qemu.py`, for QEMU) and the viewer set it when `/dev/dxg`
exists and `/dev/dri` does not.

A build directory set up by another Meson (the pinned 1.8.5 venv this build used on Debian 12)
cannot be reused by 1.7: remove it (or `meson setup --wipe`) and let `scripts/build.sh` set it
up again.
