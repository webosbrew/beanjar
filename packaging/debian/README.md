# Debian 13 packages

The Python code is three packages, each with its own `pyproject.toml` and Debian package:
`beanjar` (`host/core`: the core, the CLI and the host runtime, no GTK), `beanjar_ui`
(`host/ui`: the VM manager's and the viewer's GTK windows) and `beanjar_web` (`host/web`).
The native `beanjar` package contains the core, the viewer's native core (`libbeanjar-viewer-core.so`,
`viewer-headless`), the host helpers and the media helper's own FFmpeg, which it decodes with
(`/usr/lib/beanjar/ffmpeg-helper`, below), and recommends `beanjar-ui`.
`beanjar-ui` (architecture all, `/usr/bin/beanjar-ui`) is the windows: `beanjar_ui`, its layouts
(both GResource bundles), the desktop entry and the icon, over GTK 4 and libadwaita for Python.
`beanjar-guest` contains the same ARM guest payload for arm64 and amd64 hosts, and the board
definitions the host reads (`guest/targets`, in `/usr/share/beanjar/guest/targets`;
[firmware targets](../../docs/dev/architecture/firmware-targets.md)).
`beanjar-vnc` (`/usr/bin/beanjar-vnc`, `beanjar vnc start NAME`) serves a VM's TV picture
over VNC with the viewer core of the same `beanjar` and neatvnc 1.0 with aml 1.0, which it carries
in `/usr/lib/beanjar/neatvnc` (Debian trixie has only neatvnc 0.9.1), with the full FFmpeg in
`/usr/lib/beanjar/ffmpeg`, and its sound as Opus (`libopus0`) or PCM.
`beanjar-web` (architecture all, `/usr/bin/beanjar-web`) shows VMs in a browser: Debian's
`novnc`, Debian's `bootstrap-icons` for its buttons, and a popup remote, served by `python3-aiohttp`
([web viewer](../../docs/dev/architecture/web-viewer.md)).
`beanjar-qemu` is the patched QEMU (`/usr/libexec/beanjar/qemu-system-aarch64`, its
`vhost-user-gpu` beside it, and its data in `/usr/share/beanjar/qemu`). `beanjar` depends on it or
on Debian's `qemu-system-arm` and `qemu-system-modules-opengl`; the launcher runs `beanjar-qemu`
whenever it is installed. On amd64 it also carries the QEMU tree's `qemu-system-x86_64`, the
experimental x86-64 guest kernel's, where the tree has one (beanjar-vendor's from QEMU
10.0.2+beanjar2). It carries qemu-user's static `qemu-arm` too, which runs inside the guest
(`hardware.userland = "qemu-user"`): `/usr/share/beanjar/qemu-user/arm64/qemu-arm` for the arm64
guest kernel, and on amd64 `/usr/share/beanjar/qemu-user/amd64/qemu-arm` for the x86-64 one, each
where the host build published it (once `packaging/vendor.toml` pins `qemu-user`, or
`BEANJAR_VENDOR_QEMU_USER` and `BEANJAR_VENDOR_QEMU_USER_AMD64` name tarballs). The
optional `beanjar-guest-x86-64` (amd64; depends on `beanjar-guest` of the same version and on
`beanjar-qemu`; suggested by `beanjar`, never installed by it) carries the x86-64 guest kernel and
its initramfs in `/usr/share/beanjar/guest/x86_64` where the guest lane published them, as it does
on an x86-64 build machine (meson option `x86_64_kernel`); the Guest workflow releases it beside
`beanjar-guest`. Without it `guest_kernel=x86-64` is refused, naming the package.

### The prebuilt programs

QEMU, FFmpeg (twice: the full build and the media helper's) and neatvnc with aml are not built
here: they are the prebuilt tarballs that
[beanjar-vendor](https://github.com/mariotaku/beanjar-vendor) releases for Debian 13 on amd64 and
arm64, with their patches and build scripts. `packaging/vendor.toml` pins each one's release tag
and the SHA-256 of each architecture's tarball. The host build (`meson/vendor.py`) downloads them
into `build/vendor/download`, checks them against the pin and unpacks them into `build/vendor`,
links the libraries through their own pkg-config files and publishes the trees as they are
(`build/qemu`, `build/ffmpeg`, `build/ffmpeg-helper`, `build/neatvnc`); `build.py` packages them from the host publish
directory. There is no other source: no system package or local build stands in for them. A new
version is a new beanjar-vendor release and a new pin: its tag and the two hashes from the
release's `SHA256SUMS`, checked against a download; the host build reconfigures by itself. If the
release's `BUILDINFO` names different runtime libraries (a codec library's new soname), update
roles `host` and `host-arm64` (`packaging/debian/build-deps`) to match.

- `beanjar` ships the media helper's FFmpeg (beanjar-vendor's `ffmpeg-helper`: libavcodec,
  libavutil and libswresample with only the decoders the helper can open, LGPL) in
  `/usr/lib/beanjar/ffmpeg-helper/lib`; the media helper links it through that RUNPATH. Its
  decoder list is the helper's own (`scripts/media-helper-decoders`;
  `tests/unit/media/test_helper_decoders.py` checks the pinned build against the code).
- `beanjar-vnc` ships the full FFmpeg in `/usr/lib/beanjar/ffmpeg/lib` and `libneatvnc` and
  `libaml` in `/usr/lib/beanjar/neatvnc/lib`, beside Debian's `libneatvnc0` and `libaml0t64`
  (Weston's). It links the full FFmpeg through RUNPATH `/usr/lib/beanjar/ffmpeg/lib`, and each
  FFmpeg library `libneatvnc` needs too, whose own RUNPATH is only its directory. It replaces an
  older `beanjar`, which had the full FFmpeg.
- Each FFmpeg copy keeps FFmpeg's sonames and symbol versions, the same as Debian's, so no process
  may load two of them: the media helper only ever loads its own, `beanjar-vnc` only the full one.
- `beanjar-qemu` is the QEMU tree, already stripped, and the qemu-user trees' `qemu-arm`, with
  their licence texts and `BUILDINFO` in `/usr/share/doc/beanjar-qemu/qemu-user/<guest arch>`.

Each package depends on what its own binaries need (dpkg-shlibdeps, with the private libraries
on its search path and adding no dependency) and on what the prebuilt tree's `BUILDINFO` says the
private libraries need, less Debian's FFmpeg, neatvnc and aml libraries that the private copies
replace (dpkg-shlibdeps names those too where the build machine has them installed: it prefers a
library that belongs to a package); `beanjar-qemu`'s Depends is its `BUILDINFO`'s. Each tree's
licence texts, `NOTICE` and `BUILDINFO` go to the package's `/usr/share/doc/<package>/<program>`
(`beanjar-qemu`'s to `/usr/share/doc/beanjar-qemu`), with a stanza in its `copyright` naming the
release, whose assets carry the corresponding source of the GPL and LGPL programs.

Application code is in `/usr/lib/beanjar` (the Python packages in `core/`, `ui/` and `web/`, as
source), native launchers and helpers in
`/usr/libexec/beanjar`, and guest payloads in `/usr/share/beanjar/guest`.
No Python bytecode is shipped or written under `/usr/lib`: the Python launchers
point `sys.pycache_prefix` at the user's `$XDG_CACHE_HOME/beanjar/pycache`
(default `~/.cache`), and write none when that directory cannot be made or is
not the user's own (root under `sudo` with the caller's `HOME`).
`build.py` writes a launcher for each script a package's `pyproject.toml` names
(`[project.scripts]`, `[project.gui-scripts]`; `[tool.beanjar.libexec]` for those in
`/usr/libexec/beanjar`), and two by hand: `beanjar`, the CLI, which without arguments opens
`beanjar-ui`'s window where it is installed, and `beanjar-viewer`, which runs `viewer-headless`
for `--headless` and the like without `beanjar-ui`, and the window with it.
Guest components live separately under `layers/common`, `layers/soc`,
`layers/release` and feature directories. Firmware compatibility selectors
choose component variants at VM start; packages contain no merged target stack.

Build guest components with the guest Meson lane and host artifacts separately
for each architecture, commit, rebuild, then assemble:

```sh
python3 packaging/debian/build.py
sudo apt install ./build/packages/beanjar-guest_<version>_all.deb \
  ./build/packages/beanjar_<version>_$(dpkg --print-architecture).deb \
  ./build/packages/beanjar-ui_<version>_all.deb
# optional: the VNC server, and the web viewer on top of it
sudo apt install ./build/packages/beanjar-vnc_<version>_$(dpkg --print-architecture).deb \
  ./build/packages/beanjar-web_<version>_all.deb
```

With the default publish directory (`build/`) the script first runs `meson compile` in the
build directory of every target the published `selections.json` files name (`build/guest-<target>`),
so `beanjar-guest` never carries a target last built before a change to a shared source, a Mesa
patch for instance (`meson/fetch.py` unpacks a wrap's tree again when the wrap or its patches
change). A selected target without a build directory, or one that fails to build, stops the
package; `--no-rebuild` skips the step. `--guest-publish` elsewhere is taken as it is.

Separate native inputs can be supplied with `--host-publish`, `--architecture`
and a `--shlibs-file` holding `dpkg-shlibdeps -O`'s output (its `shlibs:Depends=...`
line) in that native build lane, and `--vnc-shlibs-file` the same for `beanjar-vnc`.
`--guest-publish` supplies the shared guest input. `beanjar-qemu` is always assembled, from the
host publish directory's `qemu` tree.

`--host-only` builds only `beanjar`, `beanjar-ui`, `beanjar-vnc`, `beanjar-web` and `beanjar-qemu`, from the host publish directory
(the native artifacts, the prebuilt trees, both GResource bundles and `source-version.toml`) and the host's
sources in the checkout. It reads no guest input: it never looks at the guest publish
directory, the component builder, a guest tree or a guest package, and it runs no git.
The private-key audit, the native architecture check and the shared-library dependencies
still apply. `--guest-only` is its counterpart: only `beanjar-guest`, from the guest publish
directory and the guest's sources in the checkout, with no host input.

### Built on GitHub

The Packages workflow (`.github/workflows/packages.yml`) builds `beanjar`, `beanjar-ui`,
`beanjar-vnc`, `beanjar-web` and `beanjar-qemu` for amd64 and arm64, each natively on GitHub's
hosted runner of that architecture in a `debian:trixie` container, with
`scripts/build-packages.sh host`: the prebuilt programs downloaded and checked, role `host`'s
build dependencies, the host build, then `build.py --host-only`.
Pushes to `main` and pull requests run it as a check; a manual run (Actions, Packages, "Run
workflow") also uploads the `.deb` files as the run's artifacts `beanjar-debs-amd64` (with the
host's architecture-all packages) and `beanjar-debs-arm64`.

`beanjar-guest` takes hours to build, so the Packages workflow does not build it. The Guest
workflow (`.github/workflows/guest.yml`) does, in the public repository (webosbrew/beanjar) only.
It runs `scripts/build-packages.sh guest` on the amd64 runner: role `guest`'s build dependencies
and ccache; the webOS and NetCast SDKs, openlgtv/buildroot-nc4 release assets pinned by SHA-256 in
the script, unpacked in `/opt` where the cross files look; every target of `guest/targets` built in
turn, from its checked-in firmware facts, into one publish directory (each build directory removed
once it has published; ccache makes every target's kernel after the first a cache hit); then
`build.py --guest-only`, which also makes the optional `beanjar-guest-x86-64` on that amd64
machine. The wraps download from their upstreams, checked by their hashes. A manual run of the
workflow only checks that the guest builds and keeps the `.deb` files as a run artifact.

A release is one tag `v<version>` on the public repository, which both workflows build. A
release normally tags the commit that bumps `packaging/VERSION` to `X.Y.Z` (its subject
"Beanjar X.Y.Z"), where the version is plain `X.Y.Z`: for example
`git tag v0.5.0 <commit> && git push <public remote> v0.5.0`. That commit also updates the
`version=` lines of the install commands in `README.md` and `docs/user/getting-started.md`,
which `tests/unit/packaging/test_install_version.py` holds equal to `packaging/VERSION`. Any other
commit can be released too, as the version `packaging/identity.py` gives it there
(`X.Y.Z+gitN.g<12 hex>`; "Versions and compatibility", from the public repository's own history,
which differs from the private one's). Each workflow checks that the tag names the version its
build produced, else it fails and names the right tag; in any other repository a tag only builds
and checks. Both then put their packages, as built (`beanjar_<version>_amd64.deb`, ...), into the
one release `v<version>` with `scripts/release-debs.sh`, which also writes the release's notes
with the install commands for that version:

- The workflows run at the same time and either may finish first (the guest takes far longer). The
  first creates the release, not marked Latest; a create that fails because the other has just
  created it falls through, and both upload their files with `--clobber`, so a rerun replaces them.
- One `SHA256SUMS` covers every `.deb` in the release. After its upload, each workflow writes it
  from the SHA-256 digests GitHub lists for the release's assets (its own files checked against
  their local sums), uploads it and lists the release again; when a `.deb` has appeared or
  `SHA256SUMS` is no longer the one it wrote (the other workflow wrote one meanwhile), it writes it
  again. Whoever writes last has seen every `.deb`, so the file ends complete.
- The workflow that finds the release holding `beanjar` for amd64 and arm64 and `beanjar-guest`
  marks it Latest, so `releases/latest` only moves to a release that has both halves. Until then
  the release is visible on the releases page but incomplete, and `releases/latest` stays on the
  previous one.

A failed half is rerun from its workflow run; its release job uploads into the existing release.
Release whenever the host-guest contract's minor changes, since no host of a new `0.Y` has a guest
until then, and otherwise when changes should reach users.

A manual Packages run fetches the guest that pairs with its host (`packaging/guest_release.py`):
of the public repository's `v<version>` releases that hold `beanjar-guest`, the newest of the
host's `0.Y` (the range `beanjar` depends on, below), checked against the release's `SHA256SUMS`,
uploaded as `beanjar-debs-guest`. Without one the run fails,
naming the tag to push. Pushes and pull requests build only the host.

The same script builds the packages locally as root in a throwaway trixie container, for example
`docker run --rm -v "$PWD:/src" debian:trixie /src/scripts/build-packages.sh host` (or `guest`)
from a clean clone with its full history (the version comes from git).

### Versions and compatibility

Package versions are semver. `packaging/VERSION` holds `X.Y.Z`; the build lanes derive the
package version from it and git, nobody types one in. At the commit that last changed
`packaging/VERSION` the version is `X.Y.Z`; N commits later it is `X.Y.Z+gitN.g<12 hex>`
(N: the commits since that change, the hex: HEAD's). Debian sorts both after the hand-numbered
`0.1.0~pre*` builds, and builds in commit order before the next version
(`tests/unit/packaging/test_identity.py` checks it with `dpkg --compare-versions`).

Version numbers are not trusted at run time: the launcher checks what the guest payload has (its
components, and the module parameters its `.ko` files declare; docs/dev/architecture/build-and-boot.md,
"Host and guest compatibility"). The version still tells apt which guest to pair with a host:
while the major is 0, the minor marks the host-guest contract, and from 1.0 the major does. Bump
the minor in the same commit as any change to the host-guest contract:

- the virtio wire formats in `include/`;
- module parameters;
- `boot.conf`;
- the layer metadata schema (`layer.toml`, `selections.json`);
- the LLS probe's interface;
- a guest script the host relies on.

Bump the patch for everything else, on either side.

`packaging/identity.py` resolves the identity once per build lane, when the build prepares its
inputs, and the lane publishes it beside its artifacts: the host lane's `source-version.toml`
and each guest lane's `guest-installation.toml` (version, full commit, commit time, and whether
the tree was dirty or shallow). Packaging reads those files and never runs git; a missing or
invalid file is a named error, and a dirty or shallow build is refused: commit, rebuild, then
package. File dates are `SOURCE_DATE_EPOCH`'s, else the recorded commit time, never today's.
The host and VNC packages take the host lane's version, `beanjar-guest` its guest lane's:
packaging never relabels a guest with the host's version.

`beanjar` depends on `beanjar-guest (>= 0.Y), beanjar-guest (<< 0.(Y+1))`, the guests of its
`0.Y`: a hint for apt, not a runtime check. `beanjar-vnc` depends on the exact `beanjar` version,
whose viewer core it links. `beanjar-ui` depends on the exact `beanjar` (it imports the core
package) and on `python3-gi`, `gir1.2-gtk-4.0` and `gir1.2-adw-1`, which `beanjar` itself does not
need. `beanjar-web` depends on the exact `beanjar` too (the pipes and
the remote's layouts it asks the CLI for; it imports nothing of it), on `beanjar-vnc`, `python3-aiohttp`, `novnc` and `bootstrap-icons`.

`/usr/lib/beanjar/installation.toml` records the host's version, commit and architecture;
`/usr/share/beanjar/guest/installation.toml` the guest's version, commit and the SHA-256 of its
payload. Both are information: the launcher copies the versions into a run's `vm-info.toml`
for logs and messages. VM directories carry no version or format marker.

### arm64 packages from the PC

The arm64 packages are cross-built on the x86_64 PC against Debian's own arm64 libraries
(installed with multiarch) and the arm64 prebuilt programs, so they install on plain Debian (a Rock
Pi 4) as well as on the Pi (Raspberry Pi OS). A Pi-built package would not: Raspberry Pi OS's
libraries carry their own versions (its FFmpeg has epoch 8, `8:7.1.5-...+rpt2`), which plain
Debian's never satisfy. With role `host-arm64` (docs/dev/guides/build.md, "Host packages")
installed:

```sh
scripts/build.sh host-arm64            # build/host-arm64, published to build/publish-arm64
python3 packaging/debian/build.py --host-publish build/publish-arm64
```

The architecture is the published host artifacts' (their ELF machine; `--architecture` may name
it, and must agree). When it is not the PC's own, dpkg-shlibdeps runs with `DEB_HOST_ARCH` set to
it, so it reads the arm64 libraries and their packages from multiarch with
`aarch64-linux-gnu-objdump`.
`beanjar-guest`, `beanjar-ui` and `beanjar-web` are architecture all and the same as the amd64 build's.

On a plain Debian 13 Rock Pi 4A (2026-10-26) `apt install` of the five packages resolved
everything from Debian (129 packages). The packages add nobody to a group: a user runs VMs with KVM only
in group `kvm` (`/dev/kvm`), GPU rendering needs group `render` (`/dev/dri/renderD*`), and the
media helper's V4L2 request decoding group `video` (`/dev/media*`, `/dev/video*`). The postinst's
`setcap` fails on a root without extended attributes (that board's NFS root); only `link = "lan"`
is lost.

### Updating the Pi

The packages come from the PC's arm64 cross build ("arm64 packages from the PC" above), not from
a build on the Pi: the packaging requires `beanjar-virtcam` (`build/virtcam/`), which the Pi's
native `scripts/build.sh host` skips for want of cmake, spdlog, fmt, asio and libusbredirparser
(`python3 packaging/debian/build.py --architecture arm64 --host-only` there stops with `missing
input: build/virtcam/beanjar-virtcam`). The cross build also refuses a tree with uncommitted
changes (`source-version.toml`), so build from a clean checkout of the commit, for example a
`git worktree` of it. On the PC:

```sh
scripts/build.sh host-arm64
python3 packaging/debian/build.py --host-publish build/publish-arm64 --host-only --output <dir>
scp <dir>/beanjar{,-vnc,-qemu}_<version>_arm64.deb <dir>/beanjar-{ui,web}_<version>_all.deb <pi-host>:/tmp/
```

and on the Pi:

```sh
sudo apt install /tmp/beanjar_<version>_arm64.deb /tmp/beanjar-vnc_<version>_arm64.deb \
  /tmp/beanjar-qemu_<version>_arm64.deb /tmp/beanjar-ui_<version>_all.deb /tmp/beanjar-web_<version>_all.deb
```

All together: `beanjar-ui`, `beanjar-vnc` and `beanjar-web` depend on the exact `beanjar` version.
`beanjar-vnc` and the media helper use their own neatvnc and FFmpeg, not Raspberry Pi OS's. Keep the Pi's own checkout
current with `git push pi main` (a tree changed by a file sync has to be stashed first:
the Pi's `receive.denyCurrentBranch` is `updateInstead`). The debs are not kept after installing.

The installed `beanjar-guest` satisfies the dependency while it has the host's `0.Y`. When the
minor changes, build `beanjar-guest` on the PC and install it on the Pi with the host. Whatever
apt allows, the launcher refuses a start only when the guest payload lacks a component the target
needs or a module parameter the host passes.

`beanjar` (or `beanjar-ui`) opens the manager; `beanjar --help` lists CLI commands.
`beanjar-viewer` starts the viewer. Libraries, firmware, identities and state
stay user-owned. Package installation never starts VMs or downloads Python
packages. The manager uses installed helpers even when an existing library's
configuration retains its old checkout path.

The package's postinst gives `/usr/libexec/beanjar/beanjar-lan-helper` the file capability
`cap_net_admin=ep` (`setcap`, from `libcap2-bin`), the only privilege Beanjar has: it puts a
`link = "lan"` VM's macvlan on the library's wired `lan_parent`
(docs/dev/architecture/network.md). The lease is taken with `busybox udhcpc`.

The traffic inspector of `mode = "mitm"` needs mitmproxy 12.0.1 or newer, which the packages do not
ship or depend on (Debian 13 has no mitmproxy package): install it with `pipx install mitmproxy`.
Beanjar runs `NETNS_MITMWEB`, else `mitmweb` on `PATH`, else `~/.local/bin/mitmweb`. Without it a
mitm VM still starts, its guest going out directly, and `beanjar start`, `beanjar mitm show` and
the manager say why ([inspect emulator traffic](../../docs/user/inspect-emulator-traffic.md#install-mitmproxy)).

Users supply extracted firmware. EPK import additionally needs their configured
`epk2extract`. Guest execution requires no source checkout or webOS SDK. The
packaged ARM LLS probe uses qemu-arm and bubblewrap; generated data goes to the
user's XDG cache. No firmware, keys, user data, SDP derivation tools, Python
venvs or mitmproxy are packaged.

These are preparation packages. Public release requires review of the project's
licensing and corresponding source/license material for bundled components.
The repository currently has no project license file; packaging does not invent one.

Build dependencies are metapackages, `beanjar-build-deps-<role>` (roles `host`,
`guest`, `readers`), from `packaging/debian/build-deps/*.control`. Build them with
`packaging/debian/build-deps.sh [role...]` into `build/packages/` and install with `apt`, so
`apt autoremove` keeps the dependencies (docs/dev/guides/build.md, "Host packages").
